from __future__ import annotations

import gzip
import json
import sqlite3
import subprocess
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import crucible_core.services.finalizations as finalization_service
from crucible_core.application.finalizations import FinalizationCoordinator
from crucible_core.core.database import upgrade
from crucible_core.core.errors import FinalizationError
from crucible_core.infrastructure.git import final_capture_worker as worker
from crucible_core.infrastructure.git.final_capture import capture_final
from crucible_core.main import app, build_app
from crucible_core.schemas.finalizations import FinalCaptureSnapshot

TERMINAL_WINDOW_ENV = "CRUCIBLE_TERMINAL_MAX_AUTH_WINDOW_SECONDS"
TEST_WINDOW_SECONDS = "3000000000"


@pytest.fixture(autouse=True)
def _terminal_window(monkeypatch):
    monkeypatch.setenv(TERMINAL_WINDOW_ENV, TEST_WINDOW_SECONDS)


def initialized_repository(root: Path) -> str:
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "core.autocrlf", "false"],
        check=True,
    )
    project_id = str(uuid.uuid4())
    directory = root / ".crucible"
    directory.mkdir()
    (directory / "project.json").write_text(
        json.dumps({"project_id": project_id}), encoding="utf-8"
    )
    (root / "tracked.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "--quiet", "-m", "initial"],
        check=True,
    )
    return project_id


def candidate(project_id: str, root: Path) -> dict[str, object]:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "input_candidate",
        "occurred_at": "2026-09-07T00:00:00Z",
        "payload_version": 1,
        "adapter": "opencode-v1",
        "adapter_version": "0.1.0",
        "agent_session_id": "session-1",
        "input_id": "input-1",
        "execution_id": "execution-1",
        "project_id": project_id,
        "git_root": str(root),
        "workspace_path": str(root),
        "payload": {"delivery": "new"},
    }


def completion(
    project_id: str,
    root: Path,
    task_id: str,
    *,
    event_id: str | None = None,
) -> dict[str, object]:
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "event_type": "task_completed",
        "occurred_at": "2026-09-07T00:01:00Z",
        "payload_version": 1,
        "adapter": "opencode-v1",
        "adapter_version": "0.1.0",
        "agent_session_id": "session-1",
        "input_id": "input-1",
        "execution_id": "execution-1",
        "project_id": project_id,
        "git_root": str(root),
        "workspace_path": str(root),
        "payload": {
            "task_id": task_id,
            "terminal_signal": "session_prompt_return",
            "terminal_outcome": "stop",
            "compatibility_profile": (
                "opencode-v1-1.18.28-write-stop-restricted"
            ),
            "terminal_observed_at": "2026-09-07T00:00:30Z",
            "capture_not_after": "2099-01-01T00:00:00Z",
        },
    }


def database_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "crucible.db"


def admit(client: TestClient, project_id: str, root: Path) -> str:
    response = client.post("/v1/events", json=candidate(project_id, root))
    assert response.status_code == 200, response.text
    return response.json()["data"]["event"]["task_id"]


def _test_app(capture=None, **overrides):
    """Build a per-test app with immutable composed dependencies."""
    if capture is not None:
        overrides.setdefault(
            "capture_runner", worker.InlineCaptureRunner(capture)
        )
    return build_app(**overrides)


class _FakeRunner(worker.CaptureRunner):
    """Scripted fake runner injected via dependency override.

    Spawn only delegates to the scripted behavior; wait runs outside
    the boundary lock so fences can land concurrently.
    """

    def __init__(self, spawn, wait, snapshot_keys=None, cancel=None):
        self._spawn = spawn
        self._wait = wait
        self._snapshot_keys = snapshot_keys
        self._cancel = cancel
        self._lock = threading.RLock()

    @property
    def boundary_lock(self):
        return self._lock

    def spawn_capture(self, request, tree_id, generation, task_id):
        return self._spawn(request, tree_id, generation, task_id)

    def wait_capture(self, key, deadline, monotonic=None):
        return self._wait(key, deadline, monotonic)

    def snapshot_tree_keys(self, tree_id):
        if self._snapshot_keys is None:
            return []
        return self._snapshot_keys(tree_id)

    def cancel_capture(self, key):
        if self._cancel is None:
            return False
        return self._cancel(key)

    def cancel_tree_captures(self, tree_id):
        count = 0
        for key in self.snapshot_tree_keys(tree_id):
            try:
                if self.cancel_capture(key):
                    count += 1
            except Exception:
                continue
        return count

    def shutdown(self):
        pass


def test_completion_freezes_and_materializes_exact_task_diff(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    before_bytes = (root / "tracked.txt").read_bytes()
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        (root / "tracked.txt").write_text("after\n", encoding="utf-8")
        (root / "added.txt").write_text("new\n", encoding="utf-8")
        event = completion(project_id, root, task_id)
        first = client.post("/v1/events", json=event)
        replay = client.post("/v1/events", json=event)
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]

    assert first.status_code == 200, first.text
    assert replay.json() == first.json()
    assert detail["status"] == "completed"
    assert detail["execution_id"] == "execution-1"
    assert detail["terminal_signal"] == "session_prompt_return"
    assert detail["terminal_outcome"] == "stop"
    assert detail["snapshot_frozen_at"]
    assert [item["path"] for item in detail["file_changes"]] == [
        "added.txt",
        "tracked.txt",
    ]
    assert "-before" in detail["task_diff"]
    assert "+after" in detail["task_diff"]
    assert "+new" in detail["task_diff"]

    connection = sqlite3.connect(database_path(tmp_path))
    try:
        frozen = connection.execute(
            "SELECT content FROM task_baseline_files "
            "WHERE task_id = ? AND path = 'tracked.txt'",
            (task_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert gzip.decompress(frozen) == before_bytes


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("terminal_signal", "session_idle", "INVALID_TERMINAL_SIGNAL"),
        ("terminal_signal", "message_accepted", "INVALID_TERMINAL_SIGNAL"),
        ("terminal_outcome", "error", "INVALID_TERMINAL_OUTCOME"),
        (
            "compatibility_profile",
            "opencode-v1-unrestricted",
            "UNSUPPORTED_COMPATIBILITY_PROFILE",
        ),
    ],
)
def test_rejects_unproven_terminal_contract(
    monkeypatch, tmp_path, field, value, code
):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        event = completion(project_id, root, task_id)
        event["payload"][field] = value
        response = client.post("/v1/events", json=event)
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert response.status_code == 400
    assert response.json()["data"]["code"] == code
    assert detail["status"] == "running"


def test_requires_execution_and_full_correlation(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        missing = completion(project_id, root, task_id)
        missing.pop("execution_id")
        missing_response = client.post("/v1/events", json=missing)
        mismatch = completion(project_id, root, task_id)
        mismatch["input_id"] = "other-input"
        mismatch_response = client.post("/v1/events", json=mismatch)
    assert missing_response.json()["data"]["code"] == "EXECUTION_ID_REQUIRED"
    assert mismatch_response.status_code == 409
    assert mismatch_response.json()["data"]["code"] == "INPUT_TASK_MISMATCH"


def test_stale_generation_cannot_publish(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        (root / "tracked.txt").write_text("after\n", encoding="utf-8")
        connection = sqlite3.connect(database_path(tmp_path))
        tree_id = connection.execute(
            "SELECT working_tree_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        connection.close()

    def _fence_hook() -> None:
        finalization_service.fence_unfrozen_finalization(
            database_path(tmp_path), tree_id
        )

    with TestClient(build_app(publication_hook=_fence_hook)) as client:
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
    assert response.status_code == 409
    assert response.json()["data"]["code"] == "STALE_CAPTURE_GENERATION"
    connection = sqlite3.connect(database_path(tmp_path))
    try:
        task = connection.execute(
            "SELECT status, failure_code, snapshot_frozen_at FROM tasks "
            "WHERE id = ?",
            (task_id,),
        ).fetchone()
        changes = connection.execute(
            "SELECT COUNT(*) FROM task_file_changes WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
        event = connection.execute(
            "SELECT status, failure_code FROM inbound_events "
            "WHERE event_type = 'task_completed'"
        ).fetchone()
    finally:
        connection.close()
    assert task == ("failed", "FINAL_CAPTURE_FENCED_BY_NEXT_INPUT", None)
    assert changes == 0
    assert event == (
        "rejected",
        "FINAL_CAPTURE_FENCED_BY_NEXT_INPUT",
    )


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("branch", "BRANCH_CHANGED_DURING_TASK"),
        ("detached", "UNSUPPORTED_HEAD_STATE"),
    ],
)
def test_unsupported_git_states_fail_without_freezing(
    monkeypatch, tmp_path, mutation, code
):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        if mutation == "branch":
            subprocess.run(
                ["git", "-C", str(root), "switch", "-c", "other"],
                check=True,
                capture_output=True,
            )
        else:
            subprocess.run(
                ["git", "-C", str(root), "switch", "--detach", "HEAD"],
                check=True,
                capture_output=True,
            )
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )

    assert response.status_code == 400
    assert response.json()["data"]["code"] == code
    connection = sqlite3.connect(database_path(tmp_path))
    try:
        task = connection.execute(
            "SELECT status, failure_code, snapshot_frozen_at FROM tasks "
            "WHERE id = ?",
            (task_id,),
        ).fetchone()
        changes = connection.execute(
            "SELECT COUNT(*) FROM task_file_changes WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert task == ("failed", code, None)
    assert changes == 0


def test_recovery_uses_only_frozen_sqlite_evidence(tmp_path):
    path = tmp_path / "crucible.db"
    upgrade(path)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        INSERT INTO projects VALUES ('project', '/missing');
        INSERT INTO working_trees (id, project_id, git_root)
        VALUES ('tree', 'project', '/missing');
        INSERT INTO sessions (id, working_tree_id, adapter, agent_session_id)
        VALUES ('session', 'tree', 'opencode-v1', 'agent-session');
        INSERT INTO tasks
        (id, session_id, working_tree_id, status, started_at,
         snapshot_frozen_at)
        VALUES ('frozen', 'session', 'tree', 'finalizing', 'now', 'frozen');
        INSERT INTO task_baseline_files
        (id, task_id, path, status, sha256, size, is_binary, content)
        VALUES ('base', 'frozen', 'file.txt', '  ', 'a', 7, 0, NULL);
        INSERT INTO task_file_changes
        (id, task_id, path, operation, final_status, final_sha256,
         final_size, final_is_binary, final_content, evidence_status)
        VALUES ('change', 'frozen', 'file.txt', 'modified', ' M', 'b',
                6, 0, NULL, 'hash_only');
        INSERT INTO inbound_events
        (id, payload_hash, status, event_type, received_at, outcome, task_id)
        VALUES ('completion', 'hash', 'processing', 'task_completed', 'now',
                'finalizing', 'frozen');
        INSERT INTO working_trees (id, project_id, git_root)
        VALUES ('tree-2', 'project', '/missing-2');
        INSERT INTO sessions (id, working_tree_id, adapter, agent_session_id)
        VALUES ('session-2', 'tree-2', 'opencode-v1', 'agent-session-2');
        INSERT INTO tasks
        (id, session_id, working_tree_id, status, started_at)
        VALUES ('unfrozen', 'session-2', 'tree-2', 'finalizing', 'later');
        """
    )
    connection.commit()
    connection.close()

    coordinator = FinalizationCoordinator(
        path, capture_runner=worker.InlineCaptureRunner(capture_final)
    )
    coordinator.recover()

    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT id, status, failure_code FROM tasks ORDER BY id"
        ).fetchall()
        event = connection.execute(
            "SELECT status, outcome FROM inbound_events "
            "WHERE id = 'completion'"
        ).fetchone()
    finally:
        connection.close()
    assert rows == [
        ("frozen", "completed", None),
        ("unfrozen", "failed", "FINAL_SNAPSHOT_NOT_FROZEN"),
    ]
    assert event == ("accepted", "completed")


def test_real_admission_fences_finalizing_before_releasing_overlap(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    event_b = {
        "event_id": str(uuid.uuid4()),
        "event_type": "input_candidate",
        "occurred_at": "2026-09-07T00:00:30Z",
        "payload_version": 1,
        "adapter": "opencode-v1",
        "adapter_version": "0.1.0",
        "agent_session_id": "session-2",
        "input_id": "input-2",
        "execution_id": "execution-2",
        "project_id": project_id,
        "git_root": str(root),
        "workspace_path": str(root),
        "payload": {"delivery": "new"},
    }
    captured: dict[str, object] = {}

    def _hook() -> None:
        # `client` binds below before any request runs; the closure
        # resolves it at call time inside the completion request.
        b_response = client.post("/v1/events", json=event_b)
        captured["b_status"] = b_response.status_code
        captured["b_body"] = b_response.json()
        connection = sqlite3.connect(database_path(tmp_path))
        try:
            captured["a_during_b"] = connection.execute(
                "SELECT status, failure_code, snapshot_frozen_at "
                "FROM tasks WHERE id = ?",
                (task_a,),
            ).fetchone()
            captured["event_during_b"] = connection.execute(
                "SELECT status, failure_code FROM inbound_events "
                "WHERE event_type = 'task_completed'",
            ).fetchone()
        finally:
            connection.close()

    test_app = build_app(publication_hook=_hook)
    with TestClient(test_app) as client:
        task_a = admit(client, project_id, root)
        (root / "tracked.txt").write_text("after\n", encoding="utf-8")
        event_a = completion(project_id, root, task_a)
        response = client.post("/v1/events", json=event_a)
        fenced_replay = client.post("/v1/events", json=event_a)

    assert captured["b_status"] == 200
    b_event = captured["b_body"]["data"]["event"]
    assert b_event["outcome"] == "released_overlap"
    assert captured["a_during_b"] == (
        "failed",
        "FINAL_CAPTURE_FENCED_BY_NEXT_INPUT",
        None,
    )
    assert captured["event_during_b"] == (
        "rejected",
        "FINAL_CAPTURE_FENCED_BY_NEXT_INPUT",
    )
    assert response.status_code == 409
    assert response.json()["data"]["code"] == "STALE_CAPTURE_GENERATION"
    assert fenced_replay.status_code == 409
    assert (
        fenced_replay.json()["data"]["code"]
        == "FINAL_CAPTURE_FENCED_BY_NEXT_INPUT"
    )
    connection = sqlite3.connect(database_path(tmp_path))
    try:
        task = connection.execute(
            "SELECT status, failure_code, snapshot_frozen_at FROM tasks "
            "WHERE id = ?",
            (task_a,),
        ).fetchone()
        changes = connection.execute(
            "SELECT COUNT(*) FROM task_file_changes WHERE task_id = ?",
            (task_a,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert task == ("failed", "FINAL_CAPTURE_FENCED_BY_NEXT_INPUT", None)
    assert changes == 0


def test_processing_replay_is_retryable_and_completed_stable(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    captured: dict[str, object] = {}

    def _hook() -> None:
        # `client` and `event` bind below before any request runs.
        replay = client.post("/v1/events", json=event)
        captured["status"] = replay.status_code
        captured["body"] = replay.json()

    with TestClient(build_app(publication_hook=_hook)) as client:
        admitted = client.post("/v1/events", json=candidate(project_id, root))
        assert admitted.status_code == 200, admitted.text
        task_id = admitted.json()["data"]["event"]["task_id"]
        input_row_id = admitted.json()["data"]["event"]["input_id"]
        assert input_row_id
        (root / "tracked.txt").write_text("after\n", encoding="utf-8")
        event = completion(project_id, root, task_id)
        first = client.post("/v1/events", json=event)
        second = client.post("/v1/events", json=event)

    assert captured["status"] == 409
    assert captured["body"]["data"]["code"] == "FINALIZATION_IN_PROGRESS"
    assert captured["body"]["status"] == "error"
    assert first.status_code == 200, first.text
    assert first.json()["data"]["event"]["outcome"] == "completed"
    assert first.json()["data"]["event"]["input_id"] == input_row_id
    assert second.json() == first.json()
    connection = sqlite3.connect(database_path(tmp_path))
    try:
        row = connection.execute(
            "SELECT input_id, task_id FROM inbound_events WHERE id = ?",
            (event["event_id"],),
        ).fetchone()
    finally:
        connection.close()
    assert row == (input_row_id, task_id)


def test_unexpected_capture_error_terminalizes_and_releases_tree(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)

    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("boom")

    with TestClient(_test_app(_boom)) as client:
        task_a = admit(client, project_id, root)
        event = completion(project_id, root, task_a)
        response = client.post("/v1/events", json=event)

    assert response.status_code == 500
    assert response.json()["data"]["code"] == "FINALIZATION_FAILED"
    connection = sqlite3.connect(database_path(tmp_path))
    try:
        task = connection.execute(
            "SELECT status, failure_code, snapshot_frozen_at FROM tasks "
            "WHERE id = ?",
            (task_a,),
        ).fetchone()
        inbound = connection.execute(
            "SELECT status, failure_code, input_id FROM inbound_events "
            "WHERE id = ?",
            (event["event_id"],),
        ).fetchone()
    finally:
        connection.close()
    assert task == ("failed", "FINALIZATION_FAILED", None)
    assert inbound[0] == "rejected"
    assert inbound[1] == "FINALIZATION_FAILED"
    assert inbound[2]

    with TestClient(app) as client:
        next_candidate = candidate(project_id, root)
        next_candidate["agent_session_id"] = "session-2"
        next_candidate["input_id"] = "input-2"
        next_candidate["execution_id"] = "execution-2"
        followed = client.post("/v1/events", json=next_candidate)
    assert followed.status_code == 200, followed.text
    assert followed.json()["data"]["event"]["outcome"] == "admitted"
    assert followed.json()["data"]["event"]["task_id"] != task_a


def test_completion_execution_mismatch_before_capture(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("capture must not run on mismatch")

    with TestClient(_test_app(_boom)) as client:
        task_id = admit(client, project_id, root)
        event = completion(project_id, root, task_id)
        event["execution_id"] = "execution-other"
        response = client.post("/v1/events", json=event)
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert response.status_code == 409
    assert response.json()["data"]["code"] == "EXECUTION_ID_MISMATCH"
    assert detail["status"] == "running"
    assert detail["execution_id"] == "execution-1"


def test_completion_session_mismatch_before_capture(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("capture must not run on mismatch")

    with TestClient(_test_app(_boom)) as client:
        task_id = admit(client, project_id, root)
        event = completion(project_id, root, task_id)
        event["agent_session_id"] = "session-other"
        response = client.post("/v1/events", json=event)
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert response.status_code == 409
    assert response.json()["data"]["code"] == "TASK_CORRELATION_MISMATCH"
    assert detail["status"] == "running"


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("missing-observed", "INVALID_TERMINAL_SIGNAL"),
        ("bad-observed", "INVALID_TERMINAL_OBSERVED_AT"),
        ("non-utc-observed", "INVALID_TERMINAL_OBSERVED_AT"),
        ("missing-deadline", "INVALID_TERMINAL_SIGNAL"),
        ("bad-deadline", "INVALID_CAPTURE_NOT_AFTER"),
        ("inverted-window", "INVALID_CAPTURE_WINDOW"),
    ],
)
def test_completion_requires_caller_capture_window(
    monkeypatch, tmp_path, mutation, code
):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("capture must not run on invalid window")

    with TestClient(_test_app(_boom)) as client:
        task_id = admit(client, project_id, root)
        event = completion(project_id, root, task_id)
        payload = event["payload"]
        if mutation == "missing-observed":
            del payload["terminal_observed_at"]
        elif mutation == "bad-observed":
            payload["terminal_observed_at"] = "not-a-time"
        elif mutation == "non-utc-observed":
            payload["terminal_observed_at"] = "2026-09-07T00:00:30"
        elif mutation == "missing-deadline":
            del payload["capture_not_after"]
        elif mutation == "bad-deadline":
            payload["capture_not_after"] = "not-a-time"
        else:
            payload["terminal_observed_at"] = "2026-09-07T00:05:00Z"
            payload["capture_not_after"] = "2026-09-07T00:00:00Z"
        response = client.post("/v1/events", json=event)
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert response.json()["data"]["code"] == code
    assert detail["status"] == "running"


def test_expired_authorization_aborts_without_git_reads(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)

    def _fixed_clock() -> datetime:
        return datetime(2026, 9, 7, 0, 1, 0, tzinfo=UTC)

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("expired capture must not read Git")

    test_app = _test_app(_boom, clock=_fixed_clock)
    with TestClient(test_app) as client:
        task_id = admit(client, project_id, root)
        event = completion(project_id, root, task_id)
        event["payload"]["terminal_observed_at"] = "2026-09-07T00:00:00Z"
        event["payload"]["capture_not_after"] = "2026-09-07T00:00:10Z"
        response = client.post("/v1/events", json=event)
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
        stored = client.get(f"/v1/events/{event['event_id']}").json()["data"][
            "event"
        ]
    assert response.status_code == 200, response.text
    body = response.json()["data"]["event"]
    assert body["event_id"] == event["event_id"]
    assert body["status"] == "rejected"
    assert body["outcome"] == "rejected"
    assert body["task_id"] == task_id
    assert body["input_id"]
    assert body["dispatch_authorized"] is False
    assert detail["status"] == "failed"
    assert detail["failure_code"] == "CAPTURE_AUTHORIZATION_EXPIRED"
    assert detail["terminal_observed_at"] == "2026-09-07T00:00:00Z"
    assert detail["capture_not_after"] == "2026-09-07T00:00:10Z"
    assert stored["status"] == "rejected"
    assert stored["failure_code"] == "CAPTURE_AUTHORIZATION_EXPIRED"
    connection = sqlite3.connect(database_path(tmp_path))
    try:
        changes = connection.execute(
            "SELECT COUNT(*) FROM task_file_changes WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert changes == 0


def test_expired_abort_replay_is_stable(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)

    def _fixed_clock() -> datetime:
        return datetime(2026, 9, 7, 0, 1, 0, tzinfo=UTC)

    with TestClient(_test_app(clock=_fixed_clock)) as client:
        task_id = admit(client, project_id, root)
        event = completion(project_id, root, task_id)
        event["payload"]["terminal_observed_at"] = "2026-09-07T00:00:00Z"
        event["payload"]["capture_not_after"] = "2026-09-07T00:00:10Z"
        first = client.post("/v1/events", json=event)
        replay = client.post("/v1/events", json=event)
        conflicted = completion(project_id, root, task_id)
        conflicted["event_id"] = event["event_id"]
        conflicted["payload"]["terminal_observed_at"] = "2026-09-07T00:00:05Z"
        conflicted["payload"]["capture_not_after"] = "2026-09-07T00:00:10Z"
        conflict = client.post("/v1/events", json=conflicted)
    assert first.status_code == 200, first.text
    body = first.json()["data"]["event"]
    assert body["status"] == "rejected"
    assert body["outcome"] == "rejected"
    assert body["task_id"] == task_id
    assert body["dispatch_authorized"] is False
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert conflict.status_code == 409
    assert conflict.json()["data"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_completed_replay_after_window_stays_completed(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)

    def _first_clock() -> datetime:
        return datetime(2026, 9, 7, 0, 0, 45, tzinfo=UTC)

    def _later_clock() -> datetime:
        return datetime(2026, 9, 7, 0, 2, 0, tzinfo=UTC)

    with TestClient(_test_app(clock=_first_clock)) as client:
        task_id = admit(client, project_id, root)
        (root / "tracked.txt").write_text("after\n", encoding="utf-8")
        event = completion(project_id, root, task_id)
        event["payload"]["terminal_observed_at"] = "2026-09-07T00:00:30Z"
        event["payload"]["capture_not_after"] = "2026-09-07T00:01:00Z"
        first = client.post("/v1/events", json=event)
        assert first.status_code == 200, first.text
    # A replay observes stored completion; each app keeps its own clock.
    with TestClient(_test_app(clock=_later_clock)) as client:
        replay = client.post("/v1/events", json=event)
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert replay.json() == first.json()
    assert detail["status"] == "completed"


def test_new_completion_after_expiry_cannot_renew(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)

    def _fixed_clock() -> datetime:
        return datetime(2026, 9, 7, 0, 1, 0, tzinfo=UTC)

    with TestClient(_test_app(clock=_fixed_clock)) as client:
        task_id = admit(client, project_id, root)
        expired = completion(project_id, root, task_id)
        expired["payload"]["terminal_observed_at"] = "2026-09-07T00:00:00Z"
        expired["payload"]["capture_not_after"] = "2026-09-07T00:00:10Z"
        first = client.post("/v1/events", json=expired)
        assert first.status_code == 200, first.text
        assert first.json()["data"]["event"]["status"] == "rejected"
        assert first.json()["data"]["event"]["outcome"] == "rejected"
        assert first.json()["data"]["event"]["task_id"] == task_id
        assert first.json()["data"]["event"]["dispatch_authorized"] is False
        renewed = completion(project_id, root, task_id)
        renewed["event_id"] = str(uuid.uuid4())
        renewed["payload"]["terminal_observed_at"] = "2026-09-07T00:00:50Z"
        renewed["payload"]["capture_not_after"] = "2099-01-01T00:00:00Z"
        second = client.post("/v1/events", json=renewed)
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert second.status_code == 409
    assert second.json()["data"]["code"] == "TASK_NOT_RUNNING"
    assert detail["status"] == "failed"
    assert detail["failure_code"] == "CAPTURE_AUTHORIZATION_EXPIRED"


def test_expired_completion_makes_no_project_or_git_reads(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)

    def _fixed_clock() -> datetime:
        return datetime(2026, 9, 7, 0, 1, 0, tzinfo=UTC)

    def _boom_project(*args: object, **kwargs: object) -> object:
        raise AssertionError("expired must not resolve project")

    def _boom_capture(*args: object, **kwargs: object) -> object:
        raise AssertionError("expired must not read Git")

    monkeypatch.setattr(
        "crucible_core.application.finalizations.resolve_project",
        _boom_project,
    )
    test_app = _test_app(_boom_capture, clock=_fixed_clock)
    with TestClient(test_app) as client:
        task_id = admit(client, project_id, root)
        event = completion(project_id, root, task_id)
        event["payload"]["terminal_observed_at"] = "2026-09-07T00:00:00Z"
        event["payload"]["capture_not_after"] = "2026-09-07T00:00:10Z"
        response = client.post("/v1/events", json=event)
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert response.status_code == 200, response.text
    assert response.json()["data"]["event"]["outcome"] == "rejected"
    assert detail["status"] == "failed"
    assert detail["failure_code"] == "CAPTURE_AUTHORIZATION_EXPIRED"


def test_excessive_window_rejects_before_git(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(TERMINAL_WINDOW_ENV, "60")
    root = tmp_path / "repo"
    project_id = initialized_repository(root)

    def _fixed_clock() -> datetime:
        return datetime(2026, 9, 7, 0, 0, 45, tzinfo=UTC)

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("excessive window must not read Git")

    test_app = _test_app(_boom, clock=_fixed_clock)
    with TestClient(test_app) as client:
        task_id = admit(client, project_id, root)
        event = completion(project_id, root, task_id)
        response = client.post("/v1/events", json=event)
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert response.json()["data"]["code"] == "INVALID_CAPTURE_WINDOW"
    assert detail["status"] == "running"


def test_future_observation_rejects_before_git(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)

    def _fixed_clock() -> datetime:
        return datetime(2026, 9, 7, 0, 0, 0, tzinfo=UTC)

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("future observation must not read Git")

    test_app = _test_app(_boom, clock=_fixed_clock)
    with TestClient(test_app) as client:
        task_id = admit(client, project_id, root)
        event = completion(project_id, root, task_id)
        event["payload"]["terminal_observed_at"] = "2026-09-07T00:01:00Z"
        event["payload"]["capture_not_after"] = "2026-09-07T00:02:00Z"
        response = client.post("/v1/events", json=event)
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert response.json()["data"]["code"] == "INVALID_TERMINAL_OBSERVED_AT"
    assert detail["status"] == "running"


def test_missing_window_config_defaults_to_two_seconds(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv(TERMINAL_WINDOW_ENV, raising=False)
    root = tmp_path / "repo"
    project_id = initialized_repository(root)

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("default window must not read Git")

    with TestClient(_test_app(_boom)) as client:
        task_id = admit(client, project_id, root)
        event = completion(project_id, root, task_id)
        event["payload"]["terminal_observed_at"] = "2026-09-07T00:00:30Z"
        event["payload"]["capture_not_after"] = "2026-09-07T00:01:00Z"
        response = client.post("/v1/events", json=event)
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert response.status_code == 400
    assert response.json()["data"]["code"] == "INVALID_CAPTURE_WINDOW"
    assert detail["status"] == "running"


def test_invalid_window_config_is_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(TERMINAL_WINDOW_ENV, "not-a-number")
    root = tmp_path / "repo"
    project_id = initialized_repository(root)

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("unconfigured must not read Git")

    with TestClient(_test_app(_boom)) as client:
        task_id = admit(client, project_id, root)
        event = completion(project_id, root, task_id)
        event["payload"]["terminal_observed_at"] = "2026-09-07T00:00:30Z"
        event["payload"]["capture_not_after"] = "2026-09-07T00:01:00Z"
        response = client.post("/v1/events", json=event)
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert response.status_code == 500
    assert (
        response.json()["data"]["code"]
        == "TERMINAL_AUTHORIZATION_UNCONFIGURED"
    )
    assert detail["status"] == "running"


def test_legacy_null_execution_id_cannot_complete(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("legacy mismatch must not read Git")

    with TestClient(_test_app(_boom)) as client:
        task_id = admit(client, project_id, root)
        connection = sqlite3.connect(database_path(tmp_path))
        try:
            connection.execute(
                "UPDATE tasks SET execution_id = NULL WHERE id = ?",
                (task_id,),
            )
            connection.commit()
        finally:
            connection.close()
        event = completion(project_id, root, task_id)
        response = client.post("/v1/events", json=event)
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert response.status_code == 409
    assert response.json()["data"]["code"] == "EXECUTION_ID_MISMATCH"
    assert detail["status"] == "running"


def test_expiry_race_in_begin_rejects_without_capture(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    valid_at = datetime(2026, 9, 7, 0, 0, 45, tzinfo=UTC)
    expired_at = datetime(2026, 9, 7, 0, 1, 1, tzinfo=UTC)
    calls = {"count": 0}

    def _race_clock() -> datetime:
        calls["count"] += 1
        if calls["count"] <= 2:
            return valid_at
        return expired_at

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("race expiry must not read Git")

    monkeypatch.setattr(
        "crucible_core.application.finalizations.resolve_project",
        _boom,
    )
    test_app = _test_app(_boom, clock=_race_clock)
    with TestClient(test_app) as client:
        task_id = admit(client, project_id, root)
        event = completion(project_id, root, task_id)
        event["payload"]["terminal_observed_at"] = "2026-09-07T00:00:30Z"
        event["payload"]["capture_not_after"] = "2026-09-07T00:01:00Z"
        response = client.post("/v1/events", json=event)
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
        stored = client.get(f"/v1/events/{event['event_id']}").json()["data"][
            "event"
        ]
        replay = client.post("/v1/events", json=event)

    assert calls["count"] >= 3
    assert response.status_code == 200, response.text
    body = response.json()["data"]["event"]
    assert body["status"] == "rejected"
    assert body["outcome"] == "rejected"
    assert body["task_id"] == task_id
    assert body["input_id"]
    assert body["dispatch_authorized"] is False
    assert detail["status"] == "failed"
    assert detail["failure_code"] == "CAPTURE_AUTHORIZATION_EXPIRED"
    assert detail["terminal_observed_at"] == "2026-09-07T00:00:30Z"
    assert detail["capture_not_after"] == "2026-09-07T00:01:00Z"
    assert stored["status"] == "rejected"
    assert stored["failure_code"] == "CAPTURE_AUTHORIZATION_EXPIRED"
    assert replay.json() == response.json()

    connection = sqlite3.connect(database_path(tmp_path))
    try:
        connection.row_factory = sqlite3.Row
        task_row = connection.execute(
            "SELECT status, failure_code FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        event_row = connection.execute(
            "SELECT status, outcome, failure_code FROM inbound_events "
            "WHERE id = ?",
            (event["event_id"],),
        ).fetchone()
        change_count = connection.execute(
            "SELECT COUNT(*) AS total FROM task_file_changes "
            "WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    finally:
        connection.close()
    assert task_row["status"] == "failed"
    assert task_row["failure_code"] == "CAPTURE_AUTHORIZATION_EXPIRED"
    assert event_row["status"] == "rejected"
    assert event_row["outcome"] == "rejected"
    assert event_row["failure_code"] == "CAPTURE_AUTHORIZATION_EXPIRED"
    assert change_count["total"] == 0


def aborted(
    project_id: str,
    root: Path,
    task_id: str,
    *,
    event_id: str | None = None,
    reason: str = "DISPATCH_FAILED",
) -> dict[str, object]:
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "event_type": "task_finalization_aborted",
        "occurred_at": "2026-09-07T00:01:00Z",
        "payload_version": 1,
        "adapter": "opencode-v1",
        "adapter_version": "0.1.0",
        "agent_session_id": "session-1",
        "input_id": "input-1",
        "execution_id": "execution-1",
        "project_id": project_id,
        "git_root": str(root),
        "workspace_path": str(root),
        "payload": {"task_id": task_id, "abort_reason": reason},
    }


def test_abort_marks_running_failed_without_git_reads(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv(TERMINAL_WINDOW_ENV, raising=False)
    root = tmp_path / "repo"
    project_id = initialized_repository(root)

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("abort must not read Git")

    monkeypatch.setattr(
        "crucible_core.application.finalizations.resolve_project",
        _boom,
    )
    with TestClient(_test_app(_boom)) as client:
        task_id = admit(client, project_id, root)
        event = aborted(project_id, root, task_id)
        response = client.post("/v1/events", json=event)
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
        stored = client.get(f"/v1/events/{event['event_id']}").json()["data"][
            "event"
        ]
    assert response.status_code == 200, response.text
    body = response.json()["data"]["event"]
    assert body["status"] == "rejected"
    assert body["outcome"] == "rejected"
    assert body["task_id"] == task_id
    assert body["input_id"]
    assert body["dispatch_authorized"] is False
    assert detail["status"] == "failed"
    assert detail["failure_code"] == "DISPATCH_FAILED"
    assert detail["failure_message"] == "DISPATCH_FAILED"
    assert detail["terminal_observed_at"] is None
    assert detail["capture_not_after"] is None
    assert detail["file_changes"] == []
    assert stored["status"] == "rejected"
    assert stored["outcome"] == "rejected"
    assert stored["failure_code"] == "DISPATCH_FAILED"
    assert stored["payload_hash"]
    assert stored["task_id"] == task_id
    connection = sqlite3.connect(database_path(tmp_path))
    try:
        changes = connection.execute(
            "SELECT COUNT(*) FROM task_file_changes WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert changes == 0


def test_abort_replay_is_stable_without_git(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("replay must not read Git")

    monkeypatch.setattr(
        "crucible_core.application.finalizations.resolve_project",
        _boom,
    )
    with TestClient(_test_app(_boom)) as client:
        task_id = admit(client, project_id, root)
        event = aborted(project_id, root, task_id)
        first = client.post("/v1/events", json=event)
        replay = client.post("/v1/events", json=event)
        conflicted = aborted(project_id, root, task_id)
        conflicted["event_id"] = event["event_id"]
        conflicted["payload"] = {
            "task_id": task_id,
            "abort_reason": "TERMINAL_SIGNAL_MISMATCH",
        }
        conflict = client.post("/v1/events", json=conflicted)
    assert first.status_code == 200, first.text
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert conflict.status_code == 409
    assert conflict.json()["data"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_abort_rejects_unsupported_reason_without_transition(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("invalid abort must not read Git")

    with TestClient(_test_app(_boom)) as client:
        task_id = admit(client, project_id, root)
        event = aborted(project_id, root, task_id, reason="UNKNOWN_REASON")
        response = client.post("/v1/events", json=event)
        extra = aborted(project_id, root, task_id)
        extra["payload"] = {
            "task_id": task_id,
            "abort_reason": "DISPATCH_FAILED",
            "terminal_observed_at": "2026-09-07T00:00:00Z",
        }
        extra_response = client.post("/v1/events", json=extra)
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert response.json()["data"]["code"] == "INVALID_ABORT_REASON"
    assert extra_response.json()["data"]["code"] == "INVALID_ABORT_REASON"
    assert detail["status"] == "running"


def test_abort_requires_correlation_before_transition(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("mismatch must not read Git")

    with TestClient(_test_app(_boom)) as client:
        task_id = admit(client, project_id, root)
        execution_mismatch = aborted(project_id, root, task_id)
        execution_mismatch["execution_id"] = "execution-other"
        execution_response = client.post("/v1/events", json=execution_mismatch)
        session_mismatch = aborted(project_id, root, task_id)
        session_mismatch["agent_session_id"] = "session-other"
        session_response = client.post("/v1/events", json=session_mismatch)
        input_mismatch = aborted(project_id, root, task_id)
        input_mismatch["input_id"] = "other-input"
        input_response = client.post("/v1/events", json=input_mismatch)
        missing_execution = aborted(project_id, root, task_id)
        missing_execution.pop("execution_id")
        missing_response = client.post("/v1/events", json=missing_execution)
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert execution_response.status_code == 409
    assert execution_response.json()["data"]["code"] == "EXECUTION_ID_MISMATCH"
    assert session_response.status_code == 409
    assert (
        session_response.json()["data"]["code"] == "TASK_CORRELATION_MISMATCH"
    )
    assert input_response.status_code == 409
    assert input_response.json()["data"]["code"] == "INPUT_TASK_MISMATCH"
    assert missing_response.json()["data"]["code"] == "EXECUTION_ID_REQUIRED"
    assert detail["status"] == "running"


def test_abort_and_completion_are_mutually_terminal(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        abort = aborted(project_id, root, task_id)
        first = client.post("/v1/events", json=abort)
        assert first.status_code == 200, first.text
        second_abort = aborted(project_id, root, task_id)
        second = client.post("/v1/events", json=second_abort)
        renewed = completion(project_id, root, task_id)
        renewed["event_id"] = str(uuid.uuid4())
        late_completion = client.post("/v1/events", json=renewed)
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert second.status_code == 409
    assert second.json()["data"]["code"] == "TASK_NOT_RUNNING"
    assert late_completion.status_code == 409
    assert late_completion.json()["data"]["code"] == "TASK_NOT_RUNNING"
    assert detail["status"] == "failed"
    assert detail["failure_code"] == "DISPATCH_FAILED"


def test_abort_after_completion_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        (root / "tracked.txt").write_text("after\n", encoding="utf-8")
        completed = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert completed.status_code == 200, completed.text
        abort = aborted(project_id, root, task_id)
        response = client.post("/v1/events", json=abort)
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert response.status_code == 409
    assert response.json()["data"]["code"] == "TASK_NOT_RUNNING"
    assert detail["status"] == "completed"


def test_abort_on_finalizing_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("finalizing abort must not read Git")

    with TestClient(_test_app(_boom)) as client:
        task_id = admit(client, project_id, root)
        connection = sqlite3.connect(database_path(tmp_path))
        try:
            connection.execute(
                "UPDATE tasks SET status = 'finalizing' WHERE id = ?",
                (task_id,),
            )
            connection.commit()
        finally:
            connection.close()
        response = client.post(
            "/v1/events", json=aborted(project_id, root, task_id)
        )
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert response.status_code == 409
    assert response.json()["data"]["code"] == "TASK_NOT_RUNNING"
    assert detail["status"] == "finalizing"


def test_index_only_empty_diff_with_index_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        baseline = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
        (root / "staged.txt").write_text("staged\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(root), "add", "staged.txt"], check=True
        )
        (root / "staged.txt").unlink()
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert response.status_code == 200, response.text
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert detail["status"] == "completed"
    assert (detail["task_diff"] or "") == ""
    assert detail["file_changes"] == []
    assert (
        detail["baseline_index_sha256"] == (baseline["baseline_index_sha256"])
    )
    assert detail["final_index_sha256"] != (detail["baseline_index_sha256"])
    assert detail["final_head"] == detail["baseline_head"]


def test_staged_plus_unstaged_partial_stage_diff(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        (root / "tracked.txt").write_text("staged\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(root), "add", "tracked.txt"], check=True
        )
        (root / "tracked.txt").write_text(
            "staged plus unstaged\n", encoding="utf-8"
        )
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert response.status_code == 200, response.text
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert detail["status"] == "completed"
    assert [item["path"] for item in detail["file_changes"]] == ["tracked.txt"]
    assert "-before" in detail["task_diff"]
    assert "+staged plus unstaged" in detail["task_diff"]
    assert detail["final_index_sha256"] != (detail["baseline_index_sha256"])


def test_same_branch_commit_final_clean_includes_diff(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        (root / "tracked.txt").write_text("after\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "--quiet", "-m", "task"],
            check=True,
        )
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert response.status_code == 200, response.text
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert detail["status"] == "completed"
    assert detail["final_head"] != detail["baseline_head"]
    assert [item["path"] for item in detail["file_changes"]] == ["tracked.txt"]
    assert "-before" in detail["task_diff"]
    assert "+after" in detail["task_diff"]


def test_commit_preexisting_without_mutation_empty_diff(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    (root / "tracked.txt").write_text("preexisting\n", encoding="utf-8")
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "--quiet", "-m", "pre"],
            check=True,
        )
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert response.status_code == 200, response.text
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert detail["status"] == "completed"
    assert detail["final_head"] != detail["baseline_head"]
    assert (detail["task_diff"] or "") == ""
    assert detail["file_changes"] == []


def test_committed_file_to_directory_reports_delete_plus_add(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        (root / "tracked.txt").unlink()
        (root / "tracked.txt").mkdir()
        (root / "tracked.txt" / "nested.txt").write_text(
            "nested\n", encoding="utf-8"
        )
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "--quiet", "-m", "file to dir"],
            check=True,
        )
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert response.status_code == 200, response.text
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert detail["status"] == "completed"
    assert detail["final_head"] != detail["baseline_head"]
    by_path = {item["path"]: item for item in detail["file_changes"]}
    assert by_path["tracked.txt"]["operation"] == "deleted"
    assert by_path["tracked.txt/nested.txt"]["operation"] == "added"
    assert "tracked.txt" in (detail["task_diff"] or "")


def test_committed_case_only_rename_reports_delete_plus_add(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        renamed = subprocess.run(
            ["git", "-C", str(root), "mv", "tracked.txt", "TRACKED.txt"],
            capture_output=True,
        )
        if renamed.returncode != 0:
            pytest.skip("filesystem does not support case-only rename")
        subprocess.run(
            ["git", "-C", str(root), "commit", "--quiet", "-m", "case rename"],
            check=True,
        )
        probe = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "diff",
                "--name-only",
                "--no-renames",
                "HEAD~1",
                "HEAD",
                "--",
            ],
            capture_output=True,
            check=True,
        )
        names = set(probe.stdout.decode("utf-8").splitlines())
        if {"tracked.txt", "TRACKED.txt"} - names:
            pytest.skip("filesystem does not expose both rename sides")
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert response.status_code == 200, response.text
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert detail["status"] == "completed"
    assert detail["final_head"] != detail["baseline_head"]
    by_path = {item["path"]: item for item in detail["file_changes"]}
    assert by_path["tracked.txt"]["operation"] == "deleted"
    assert by_path["TRACKED.txt"]["operation"] == "added"


def test_committed_directory_to_file_reports_delete_plus_add(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    (root / "target").mkdir()
    (root / "target" / "nested.txt").write_text("nested\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "--quiet", "-m", "baseline dir"],
        check=True,
    )
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        (root / "target" / "nested.txt").unlink()
        (root / "target").rmdir()
        (root / "target").write_text("nowfile\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "--quiet", "-m", "dir to file"],
            check=True,
        )
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert response.status_code == 200, response.text
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert detail["status"] == "completed"
    assert detail["final_head"] != detail["baseline_head"]
    by_path = {item["path"]: item for item in detail["file_changes"]}
    assert by_path["target"]["operation"] == "added"
    assert by_path["target/nested.txt"]["operation"] == "deleted"
    assert "target" in (detail["task_diff"] or "")


def test_fenced_worker_late_snapshot_never_publishes(monkeypatch, tmp_path):
    import threading

    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_a = admit(client, project_id, root)
        connection = sqlite3.connect(database_path(tmp_path))
        try:
            baseline = connection.execute(
                "SELECT baseline_head, baseline_branch, working_tree_id "
                "FROM tasks WHERE id = ?",
                (task_a,),
            ).fetchone()
        finally:
            connection.close()
        baseline_head, baseline_branch, tree_id = baseline
        event_b = {
            "event_id": str(uuid.uuid4()),
            "event_type": "input_candidate",
            "occurred_at": "2026-09-07T00:00:30Z",
            "payload_version": 1,
            "adapter": "opencode-v1",
            "adapter_version": "0.1.0",
            "agent_session_id": "session-2",
            "input_id": "input-2",
            "execution_id": "execution-2",
            "project_id": project_id,
            "git_root": str(root),
            "workspace_path": str(root),
            "payload": {"delivery": "new"},
        }
        entered = threading.Event()
        cancel_called = threading.Event()
        state: dict[str, object] = {
            "spawned": {},
            "fence_durable_before_cancel": False,
            "b_outcome": None,
            "b_after_cancel": False,
        }

        def _fake_spawn(request, spawn_tree_id, generation, spawn_task):
            key = (spawn_tree_id, int(generation))
            state["spawned"][key] = {
                "cancelled": False,
                "task_id": spawn_task,
            }
            return key

        def _fake_snapshot_keys(snapshot_tree_id):
            return [
                key for key in state["spawned"] if key[0] == snapshot_tree_id
            ]

        def _fake_cancel(key):
            connection = sqlite3.connect(database_path(tmp_path))
            try:
                task = connection.execute(
                    "SELECT status, failure_code, snapshot_frozen_at "
                    "FROM tasks WHERE id = ?",
                    (task_a,),
                ).fetchone()
                generation = connection.execute(
                    "SELECT capture_generation FROM working_trees "
                    "WHERE id = ?",
                    (tree_id,),
                ).fetchone()
            finally:
                connection.close()
            # Fence/generation must already be durable before cancel.
            state["fence_durable_before_cancel"] = (
                task[0] == "failed"
                and task[1] == "FINAL_CAPTURE_FENCED_BY_NEXT_INPUT"
                and task[2] is None
                and generation is not None
            )
            if key in state["spawned"]:
                state["spawned"][key]["cancelled"] = True
            cancel_called.set()
            return True

        def _fake_wait(key, deadline, monotonic=None):
            entered.set()
            # Input B fences while A is blocked in the worker.
            b_response = client.post("/v1/events", json=event_b)
            assert b_response.status_code == 200, b_response.text
            body = b_response.json()["data"]["event"]
            state["b_outcome"] = body["outcome"]
            # B response is only produced after cancel/reap ran.
            state["b_after_cancel"] = cancel_called.is_set()
            # Late worker ignores cancel and still returns a snapshot;
            # publication_is_current/fence must refuse to publish it.
            assert state["spawned"][key]["cancelled"] is True
            return FinalCaptureSnapshot(
                head=baseline_head,
                branch=baseline_branch,
                status=b"",
                index=b"",
                baseline_files=[],
                changes=[],
            )

        test_app = build_app(
            capture_runner=_FakeRunner(
                _fake_spawn,
                _fake_wait,
                _fake_snapshot_keys,
                _fake_cancel,
            )
        )

    with TestClient(test_app) as client:
        event_a = completion(project_id, root, task_a)
        response = client.post("/v1/events", json=event_a)

    assert entered.is_set()
    assert state["fence_durable_before_cancel"] is True
    assert state["b_outcome"] == "released_overlap"
    assert state["b_after_cancel"] is True
    assert response.status_code == 409
    assert response.json()["data"]["code"] == "STALE_CAPTURE_GENERATION"
    connection = sqlite3.connect(database_path(tmp_path))
    try:
        task = connection.execute(
            "SELECT status, failure_code, snapshot_frozen_at FROM tasks "
            "WHERE id = ?",
            (task_a,),
        ).fetchone()
        changes = connection.execute(
            "SELECT COUNT(*) FROM task_file_changes WHERE task_id = ?",
            (task_a,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert task == ("failed", "FINAL_CAPTURE_FENCED_BY_NEXT_INPUT", None)
    assert changes == 0


def test_fenced_worker_eof_converts_to_stale(monkeypatch, tmp_path):
    import threading

    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_a = admit(client, project_id, root)
        connection = sqlite3.connect(database_path(tmp_path))
        try:
            tree_id = connection.execute(
                "SELECT working_tree_id FROM tasks WHERE id = ?",
                (task_a,),
            ).fetchone()[0]
        finally:
            connection.close()
        event_b = {
            "event_id": str(uuid.uuid4()),
            "event_type": "input_candidate",
            "occurred_at": "2026-09-07T00:00:30Z",
            "payload_version": 1,
            "adapter": "opencode-v1",
            "adapter_version": "0.1.0",
            "agent_session_id": "session-2",
            "input_id": "input-2",
            "execution_id": "execution-2",
            "project_id": project_id,
            "git_root": str(root),
            "workspace_path": str(root),
            "payload": {"delivery": "new"},
        }
        entered = threading.Event()
        cancel_called = threading.Event()
        state: dict[str, object] = {
            "spawned": {},
            "fence_durable_before_cancel": False,
            "b_outcome": None,
            "b_after_cancel": False,
        }

        def _fake_spawn(request, spawn_tree_id, generation, spawn_task):
            key = (spawn_tree_id, int(generation))
            state["spawned"][key] = {
                "cancelled": False,
                "task_id": spawn_task,
            }
            return key

        def _fake_snapshot_keys(snapshot_tree_id):
            return [
                key for key in state["spawned"] if key[0] == snapshot_tree_id
            ]

        def _fake_cancel(key):
            connection = sqlite3.connect(database_path(tmp_path))
            try:
                task = connection.execute(
                    "SELECT status, failure_code, snapshot_frozen_at "
                    "FROM tasks WHERE id = ?",
                    (task_a,),
                ).fetchone()
                generation = connection.execute(
                    "SELECT capture_generation FROM working_trees "
                    "WHERE id = ?",
                    (tree_id,),
                ).fetchone()
            finally:
                connection.close()
            # Fence/generation must already be durable before cancel.
            state["fence_durable_before_cancel"] = (
                task[0] == "failed"
                and task[1] == "FINAL_CAPTURE_FENCED_BY_NEXT_INPUT"
                and task[2] is None
                and generation is not None
            )
            if key in state["spawned"]:
                state["spawned"][key]["cancelled"] = True
            cancel_called.set()
            return True

        def _fake_wait(key, deadline, monotonic=None):
            entered.set()
            # Input B fences while A is blocked in the worker.
            b_response = client.post("/v1/events", json=event_b)
            assert b_response.status_code == 200, b_response.text
            body = b_response.json()["data"]["event"]
            state["b_outcome"] = body["outcome"]
            # B response is only produced after cancel/reap ran.
            state["b_after_cancel"] = cancel_called.is_set()
            assert state["spawned"][key]["cancelled"] is True
            # The pipe died with the cancelled child: recv raises
            # EOFError, which wait_capture surfaces as the internal
            # IPC code (never a genuine envelope code). The durable
            # fence must supersede it.
            raise FinalizationError(worker.IPC_FAILED_CODE, 500)

        test_app = build_app(
            capture_runner=_FakeRunner(
                _fake_spawn,
                _fake_wait,
                _fake_snapshot_keys,
                _fake_cancel,
            )
        )

    with TestClient(test_app) as client:
        event_a = completion(project_id, root, task_a)
        response = client.post("/v1/events", json=event_a)

    assert entered.is_set()
    assert state["fence_durable_before_cancel"] is True
    assert state["b_outcome"] == "released_overlap"
    assert state["b_after_cancel"] is True
    assert response.status_code == 409
    assert response.json()["data"]["code"] == "STALE_CAPTURE_GENERATION"
    connection = sqlite3.connect(database_path(tmp_path))
    try:
        task = connection.execute(
            "SELECT status, failure_code, snapshot_frozen_at FROM tasks "
            "WHERE id = ?",
            (task_a,),
        ).fetchone()
        changes = connection.execute(
            "SELECT COUNT(*) FROM task_file_changes WHERE task_id = ?",
            (task_a,),
        ).fetchone()[0]
        event = connection.execute(
            "SELECT status, failure_code FROM inbound_events "
            "WHERE event_type = 'task_completed'"
        ).fetchone()
    finally:
        connection.close()
    assert task == ("failed", "FINAL_CAPTURE_FENCED_BY_NEXT_INPUT", None)
    assert changes == 0
    assert event == (
        "rejected",
        "FINAL_CAPTURE_FENCED_BY_NEXT_INPUT",
    )


def test_worker_ipc_failure_without_fence_stays_500(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_a = admit(client, project_id, root)

        def _fake_spawn(request, spawn_tree_id, generation, spawn_task):
            return (spawn_tree_id, int(generation))

        def _fake_wait(key, deadline, monotonic=None):
            # Broken IPC without a fence: internal code maps to the
            # public worker-failure contract, never leaks.
            raise FinalizationError(worker.IPC_FAILED_CODE, 500)

        test_app = build_app(
            capture_runner=_FakeRunner(_fake_spawn, _fake_wait)
        )

    with TestClient(test_app) as client:
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_a)
        )

    assert response.status_code == 500
    assert response.json()["data"]["code"] == "FINALIZATION_FAILED"
    connection = sqlite3.connect(database_path(tmp_path))
    try:
        task = connection.execute(
            "SELECT status, failure_code, snapshot_frozen_at FROM tasks "
            "WHERE id = ?",
            (task_a,),
        ).fetchone()
    finally:
        connection.close()
    assert task == ("failed", "FINALIZATION_FAILED", None)


def test_fenced_worker_preserves_capture_error_code(monkeypatch, tmp_path):
    import threading

    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_a = admit(client, project_id, root)
        event_b = {
            "event_id": str(uuid.uuid4()),
            "event_type": "input_candidate",
            "occurred_at": "2026-09-07T00:00:30Z",
            "payload_version": 1,
            "adapter": "opencode-v1",
            "adapter_version": "0.1.0",
            "agent_session_id": "session-2",
            "input_id": "input-2",
            "execution_id": "execution-2",
            "project_id": project_id,
            "git_root": str(root),
            "workspace_path": str(root),
            "payload": {"delivery": "new"},
        }
        entered = threading.Event()
        state: dict[str, object] = {"spawned": {}}

        def _fake_spawn(request, spawn_tree_id, generation, spawn_task):
            key = (spawn_tree_id, int(generation))
            state["spawned"][key] = True
            return key

        def _fake_snapshot_keys(snapshot_tree_id):
            return [
                key for key in state["spawned"] if key[0] == snapshot_tree_id
            ]

        def _fake_wait(key, deadline, monotonic=None):
            entered.set()
            # Fence lands first, but the genuine capture failure the
            # child already produced must not be masked by it.
            b_response = client.post("/v1/events", json=event_b)
            assert b_response.status_code == 200, b_response.text
            raise FinalizationError("BRANCH_CHANGED_DURING_TASK")

        test_app = build_app(
            capture_runner=_FakeRunner(
                _fake_spawn, _fake_wait, _fake_snapshot_keys
            )
        )

    with TestClient(test_app) as client:
        event_a = completion(project_id, root, task_a)
        response = client.post("/v1/events", json=event_a)

    assert entered.is_set()
    assert response.status_code == 400
    assert response.json()["data"]["code"] == "BRANCH_CHANGED_DURING_TASK"
    connection = sqlite3.connect(database_path(tmp_path))
    try:
        task = connection.execute(
            "SELECT status, failure_code, snapshot_frozen_at FROM tasks "
            "WHERE id = ?",
            (task_a,),
        ).fetchone()
    finally:
        connection.close()
    assert task == ("failed", "FINAL_CAPTURE_FENCED_BY_NEXT_INPUT", None)


def test_fenced_worker_genuine_envelope_failure_stays_500(
    monkeypatch, tmp_path
):
    import threading

    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_a = admit(client, project_id, root)
        event_b = {
            "event_id": str(uuid.uuid4()),
            "event_type": "input_candidate",
            "occurred_at": "2026-09-07T00:00:30Z",
            "payload_version": 1,
            "adapter": "opencode-v1",
            "adapter_version": "0.1.0",
            "agent_session_id": "session-2",
            "input_id": "input-2",
            "execution_id": "execution-2",
            "project_id": project_id,
            "git_root": str(root),
            "workspace_path": str(root),
            "payload": {"delivery": "new"},
        }
        entered = threading.Event()
        state: dict[str, object] = {"spawned": {}}

        def _fake_spawn(request, spawn_tree_id, generation, spawn_task):
            key = (spawn_tree_id, int(generation))
            state["spawned"][key] = True
            return key

        def _fake_snapshot_keys(snapshot_tree_id):
            return [
                key for key in state["spawned"] if key[0] == snapshot_tree_id
            ]

        def _fake_wait(key, deadline, monotonic=None):
            entered.set()
            # Fence lands first, but a genuine envelope failure the
            # child already reported must never convert to STALE.
            b_response = client.post("/v1/events", json=event_b)
            assert b_response.status_code == 200, b_response.text
            raise FinalizationError("FINALIZATION_FAILED", 500)

        test_app = build_app(
            capture_runner=_FakeRunner(
                _fake_spawn, _fake_wait, _fake_snapshot_keys
            )
        )

    with TestClient(test_app) as client:
        event_a = completion(project_id, root, task_a)
        response = client.post("/v1/events", json=event_a)

    assert entered.is_set()
    assert response.status_code == 500
    assert response.json()["data"]["code"] == "FINALIZATION_FAILED"
    connection = sqlite3.connect(database_path(tmp_path))
    try:
        task = connection.execute(
            "SELECT status, failure_code, snapshot_frozen_at FROM tasks "
            "WHERE id = ?",
            (task_a,),
        ).fetchone()
    finally:
        connection.close()
    assert task == ("failed", "FINAL_CAPTURE_FENCED_BY_NEXT_INPUT", None)


def test_fenced_worker_envelope_timeout_preserves_code(monkeypatch, tmp_path):
    import threading

    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_a = admit(client, project_id, root)
        event_b = {
            "event_id": str(uuid.uuid4()),
            "event_type": "input_candidate",
            "occurred_at": "2026-09-07T00:00:30Z",
            "payload_version": 1,
            "adapter": "opencode-v1",
            "adapter_version": "0.1.0",
            "agent_session_id": "session-2",
            "input_id": "input-2",
            "execution_id": "execution-2",
            "project_id": project_id,
            "git_root": str(root),
            "workspace_path": str(root),
            "payload": {"delivery": "new"},
        }
        entered = threading.Event()
        state: dict[str, object] = {"spawned": {}}

        def _fake_spawn(request, spawn_tree_id, generation, spawn_task):
            key = (spawn_tree_id, int(generation))
            state["spawned"][key] = True
            return key

        def _fake_snapshot_keys(snapshot_tree_id):
            return [
                key for key in state["spawned"] if key[0] == snapshot_tree_id
            ]

        def _fake_wait(key, deadline, monotonic=None):
            entered.set()
            # Fence lands first, but a child-side timeout inside a
            # genuine envelope must stay FINAL_SNAPSHOT_TIMEOUT.
            b_response = client.post("/v1/events", json=event_b)
            assert b_response.status_code == 200, b_response.text
            raise FinalizationError("FINAL_SNAPSHOT_TIMEOUT")

        test_app = build_app(
            capture_runner=_FakeRunner(
                _fake_spawn, _fake_wait, _fake_snapshot_keys
            )
        )

    with TestClient(test_app) as client:
        event_a = completion(project_id, root, task_a)
        response = client.post("/v1/events", json=event_a)

    assert entered.is_set()
    assert response.status_code == 400
    assert response.json()["data"]["code"] == "FINAL_SNAPSHOT_TIMEOUT"
    connection = sqlite3.connect(database_path(tmp_path))
    try:
        task = connection.execute(
            "SELECT status, failure_code, snapshot_frozen_at FROM tasks "
            "WHERE id = ?",
            (task_a,),
        ).fetchone()
    finally:
        connection.close()
    assert task == ("failed", "FINAL_CAPTURE_FENCED_BY_NEXT_INPUT", None)


def test_fenced_worker_local_timeout_converts_to_stale(monkeypatch, tmp_path):
    import threading

    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_a = admit(client, project_id, root)
        event_b = {
            "event_id": str(uuid.uuid4()),
            "event_type": "input_candidate",
            "occurred_at": "2026-09-07T00:00:30Z",
            "payload_version": 1,
            "adapter": "opencode-v1",
            "adapter_version": "0.1.0",
            "agent_session_id": "session-2",
            "input_id": "input-2",
            "execution_id": "execution-2",
            "project_id": project_id,
            "git_root": str(root),
            "workspace_path": str(root),
            "payload": {"delivery": "new"},
        }
        entered = threading.Event()
        state: dict[str, object] = {"spawned": {}}

        def _fake_spawn(request, spawn_tree_id, generation, spawn_task):
            key = (spawn_tree_id, int(generation))
            state["spawned"][key] = True
            return key

        def _fake_snapshot_keys(snapshot_tree_id):
            return [
                key for key in state["spawned"] if key[0] == snapshot_tree_id
            ]

        def _fake_wait(key, deadline, monotonic=None):
            entered.set()
            # Fence lands while the parent itself observes its own
            # wait deadline expire: still STALE, as before.
            b_response = client.post("/v1/events", json=event_b)
            assert b_response.status_code == 200, b_response.text
            raise FinalizationError(worker.WAIT_TIMEOUT_CODE)

        test_app = build_app(
            capture_runner=_FakeRunner(
                _fake_spawn, _fake_wait, _fake_snapshot_keys
            )
        )

    with TestClient(test_app) as client:
        event_a = completion(project_id, root, task_a)
        response = client.post("/v1/events", json=event_a)

    assert entered.is_set()
    assert response.status_code == 409
    assert response.json()["data"]["code"] == "STALE_CAPTURE_GENERATION"
    connection = sqlite3.connect(database_path(tmp_path))
    try:
        task = connection.execute(
            "SELECT status, failure_code, snapshot_frozen_at FROM tasks "
            "WHERE id = ?",
            (task_a,),
        ).fetchone()
        changes = connection.execute(
            "SELECT COUNT(*) FROM task_file_changes WHERE task_id = ?",
            (task_a,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert task == ("failed", "FINAL_CAPTURE_FENCED_BY_NEXT_INPUT", None)
    assert changes == 0


def test_frozen_finalizing_replay_retries_materialize_only(
    monkeypatch, tmp_path
):
    """Lock transitório: replay idempotente retenta só materialize.

    O primeiro task_completed congela e falha em materialize com
    lock transitório (sem restart/recover). O replay do mesmo
    evento deve concluir via materialize SQLite, sem nova captura
    Git, e responder ACK accepted.
    """
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    captures = {"count": 0}

    def _counting_capture(*args, **kwargs):
        captures["count"] += 1
        return capture_final(*args, **kwargs)

    test_app = _test_app(_counting_capture)
    original_materialize = FinalizationCoordinator.materialize
    state = {"failed_once": False}

    def _flaky_materialize(self, task_id: str) -> None:
        if not state["failed_once"]:
            state["failed_once"] = True
            raise sqlite3.OperationalError("database is locked")
        return original_materialize(self, task_id)

    monkeypatch.setattr(
        FinalizationCoordinator, "materialize", _flaky_materialize
    )
    with TestClient(test_app) as client:
        task_id = admit(client, project_id, root)
        (root / "tracked.txt").write_text("after\n", encoding="utf-8")
        event = completion(project_id, root, task_id)
        first = client.post("/v1/events", json=event)
        assert first.status_code == 409, first.text
        assert first.json()["data"]["code"] == "FINALIZATION_IN_PROGRESS"
        assert captures["count"] == 1
        connection = sqlite3.connect(database_path(tmp_path))
        try:
            task = connection.execute(
                "SELECT status, snapshot_frozen_at FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            inbound = connection.execute(
                "SELECT status FROM inbound_events WHERE id = ?",
                (event["event_id"],),
            ).fetchone()
        finally:
            connection.close()
        assert task[0] == "finalizing"
        assert task[1] is not None
        assert inbound[0] == "processing"
        # Lock liberado (flaky só falha uma vez): replay síncrono
        # do mesmo evento conclui sem restart/recover global.
        second = client.post("/v1/events", json=event)
        assert second.status_code == 200, second.text
        body = second.json()["data"]["event"]
        assert body["outcome"] == "completed"
        assert body["task_id"] == task_id
        assert captures["count"] == 1
        third = client.post("/v1/events", json=event)
        assert third.json() == second.json()
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
        assert detail["status"] == "completed"


def test_concurrent_frozen_replays_stay_idempotent(monkeypatch, tmp_path):
    """Dois retries concorrentes do mesmo evento permanecem idempotentes."""
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    captures = {"count": 0}

    def _counting_capture(*args, **kwargs):
        captures["count"] += 1
        return capture_final(*args, **kwargs)

    test_app = _test_app(_counting_capture)
    original_materialize = FinalizationCoordinator.materialize
    state = {"failed_once": False}

    def _flaky_materialize(self, task_id: str) -> None:
        if not state["failed_once"]:
            state["failed_once"] = True
            raise sqlite3.OperationalError("database is locked")
        return original_materialize(self, task_id)

    monkeypatch.setattr(
        FinalizationCoordinator, "materialize", _flaky_materialize
    )
    with TestClient(test_app) as client:
        task_id = admit(client, project_id, root)
        (root / "tracked.txt").write_text("after\n", encoding="utf-8")
        event = completion(project_id, root, task_id)
        first = client.post("/v1/events", json=event)
        assert first.status_code == 409, first.text
        results: list = [None, None]

        def _replay(slot: int) -> None:
            results[slot] = client.post("/v1/events", json=event)

        threads = [
            threading.Thread(target=_replay, args=(slot,)) for slot in (0, 1)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not any(thread.is_alive() for thread in threads)
        assert all(response is not None for response in results)
        # Pelo menos um retry conclui; o outro nunca corrompe:
        # ou conclui igual ou observa IN_PROGRESS transitório.
        codes = sorted(
            response.status_code for response in results  # type: ignore[union-attr]
        )
        assert codes in ([200, 200], [200, 409])
        for response in results:  # type: ignore[union-attr]
            if response.status_code == 409:
                assert (
                    response.json()["data"]["code"]
                    == "FINALIZATION_IN_PROGRESS"
                )
            else:
                assert (
                    response.json()["data"]["event"]["outcome"] == "completed"
                )
        stable = client.post("/v1/events", json=event)
        assert stable.status_code == 200, stable.text
        assert stable.json()["data"]["event"]["outcome"] == "completed"
        assert captures["count"] == 1
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
        assert detail["status"] == "completed"


def test_frozen_replay_with_corrupt_evidence_terminalizes(
    monkeypatch, tmp_path
):
    """Replay frozen com evidência corrompida terminaliza como o fluxo inicial.

    Lock transitório congela sem concluir; a corrupção posterior
    (gzip inválido com evidência complete) faz o replay falhar
    definitivo via _fail: task failed + evento rejected, sem nova
    captura Git, e replay seguinte permanece idempotente.
    """
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    captures = {"count": 0}

    def _counting_capture(*args, **kwargs):
        captures["count"] += 1
        return capture_final(*args, **kwargs)

    test_app = _test_app(_counting_capture)
    original_materialize = FinalizationCoordinator.materialize
    state = {"failed_once": False}

    def _flaky_materialize(self, task_id: str) -> None:
        if not state["failed_once"]:
            state["failed_once"] = True
            raise sqlite3.OperationalError("database is locked")
        return original_materialize(self, task_id)

    monkeypatch.setattr(
        FinalizationCoordinator, "materialize", _flaky_materialize
    )
    with TestClient(test_app) as client:
        task_id = admit(client, project_id, root)
        (root / "tracked.txt").write_text("after\n", encoding="utf-8")
        event = completion(project_id, root, task_id)
        first = client.post("/v1/events", json=event)
        assert first.status_code == 409, first.text
        assert first.json()["data"]["code"] == "FINALIZATION_IN_PROGRESS"
        assert captures["count"] == 1
        # Corrompe a evidência congelada: blob final inválido
        # com evidence complete falha definitivo em _patch.
        connection = sqlite3.connect(database_path(tmp_path))
        try:
            connection.execute(
                "UPDATE task_file_changes SET final_content = ?"
                " WHERE task_id = ?",
                (b"not-gzip-at-all", task_id),
            )
            connection.commit()
        finally:
            connection.close()
        second = client.post("/v1/events", json=event)
        assert second.status_code == 500, second.text
        assert (
            second.json()["data"]["code"] == "FINAL_MATERIALIZATION_FAILED"
        )
        assert captures["count"] == 1
        connection = sqlite3.connect(database_path(tmp_path))
        try:
            task = connection.execute(
                "SELECT status, failure_code FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            inbound = connection.execute(
                "SELECT status, outcome, failure_code FROM inbound_events"
                " WHERE id = ?",
                (event["event_id"],),
            ).fetchone()
        finally:
            connection.close()
        assert task == ("failed", "FINAL_MATERIALIZATION_FAILED")
        assert inbound == (
            "rejected",
            "rejected",
            "FINAL_MATERIALIZATION_FAILED",
        )
        third = client.post("/v1/events", json=event)
        assert third.status_code == 500, third.text
        assert third.json()["data"]["code"] == "FINAL_MATERIALIZATION_FAILED"
        assert captures["count"] == 1
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
        assert detail["status"] == "failed"


def test_frozen_replay_fail_lock_stays_retryable_then_terminalizes(
    monkeypatch, tmp_path
):
    """_fail com lock não mascara definitivo como terminal.

    Replay frozen com evidência corrompida cujo _fail sofre
    locked devolve 409 e mantém finalizing/processing; o retry
    seguinte terminaliza failed/rejected e estabiliza idempotente.
    """
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    captures = {"count": 0}

    def _counting_capture(*args, **kwargs):
        captures["count"] += 1
        return capture_final(*args, **kwargs)

    test_app = _test_app(_counting_capture)
    original_materialize = FinalizationCoordinator.materialize
    original_fail = FinalizationCoordinator._fail
    state = {"materialize_calls": 0, "fail_calls": 0}

    def _flaky_materialize(self, task_id: str) -> None:
        state["materialize_calls"] += 1
        if state["materialize_calls"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return original_materialize(self, task_id)

    def _flaky_fail(self, task_id: str, generation, code: str) -> None:
        state["fail_calls"] += 1
        if state["fail_calls"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return original_fail(self, task_id, generation, code)

    monkeypatch.setattr(
        FinalizationCoordinator, "materialize", _flaky_materialize
    )
    monkeypatch.setattr(FinalizationCoordinator, "_fail", _flaky_fail)
    with TestClient(test_app) as client:
        task_id = admit(client, project_id, root)
        (root / "tracked.txt").write_text("after\n", encoding="utf-8")
        event = completion(project_id, root, task_id)
        # Lock no materialize inicial: congela sem concluir.
        first = client.post("/v1/events", json=event)
        assert first.status_code == 409, first.text
        assert first.json()["data"]["code"] == "FINALIZATION_IN_PROGRESS"
        assert captures["count"] == 1
        # Corrompe a evidência congelada: blob final inválido
        # com evidence complete falha definitivo em _patch.
        connection = sqlite3.connect(database_path(tmp_path))
        try:
            connection.execute(
                "UPDATE task_file_changes SET final_content = ?"
                " WHERE task_id = ?",
                (b"not-gzip-at-all", task_id),
            )
            connection.commit()
        finally:
            connection.close()
        # _fail com lock: definitivo não pode mascarar como
        # terminal; mantém 409 retryable e finalizing/processing.
        retry1 = client.post("/v1/events", json=event)
        assert retry1.status_code == 409, retry1.text
        assert retry1.json()["data"]["code"] == "FINALIZATION_IN_PROGRESS"
        connection = sqlite3.connect(database_path(tmp_path))
        try:
            task_row = connection.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            inbound_row = connection.execute(
                "SELECT status FROM inbound_events WHERE id = ?",
                (event["event_id"],),
            ).fetchone()
        finally:
            connection.close()
        assert task_row[0] == "finalizing"
        assert inbound_row[0] == "processing"
        # Lock liberado: retry terminaliza failed/rejected.
        retry2 = client.post("/v1/events", json=event)
        assert retry2.status_code == 500, retry2.text
        assert (
            retry2.json()["data"]["code"] == "FINAL_MATERIALIZATION_FAILED"
        )
        assert captures["count"] == 1
        connection = sqlite3.connect(database_path(tmp_path))
        try:
            task_row = connection.execute(
                "SELECT status, failure_code FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            inbound_row = connection.execute(
                "SELECT status, outcome, failure_code FROM inbound_events"
                " WHERE id = ?",
                (event["event_id"],),
            ).fetchone()
        finally:
            connection.close()
        assert task_row == ("failed", "FINAL_MATERIALIZATION_FAILED")
        assert inbound_row == (
            "rejected",
            "rejected",
            "FINAL_MATERIALIZATION_FAILED",
        )
        retry3 = client.post("/v1/events", json=event)
        assert retry3.status_code == 500, retry3.text
        assert retry3.json()["data"]["code"] == "FINAL_MATERIALIZATION_FAILED"
        assert captures["count"] == 1
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
        assert detail["status"] == "failed"


def test_lifespan_composes_runner_per_lifespan(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    first_app = build_app()
    other = build_app()
    with TestClient(first_app) as client:
        first = first_app.state.capture_runner
        assert isinstance(first, worker.ProcessCaptureRunner)
        assert first is not worker.get_default_runner()
        admitted = client.post("/v1/events", json=candidate(project_id, root))
        assert admitted.status_code == 200, admitted.text
        # Same lifespan shares one runner across requests.
        assert first_app.state.capture_runner is first
        task_id = admitted.json()["data"]["event"]["task_id"]
        (root / "tracked.txt").write_text("after\n", encoding="utf-8")
        completed = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert completed.status_code == 200, completed.text
        assert first_app.state.capture_runner is first
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
        assert detail["status"] == "completed"
    assert first.registry == {}
    with TestClient(other):
        second = other.state.capture_runner
        assert isinstance(second, worker.ProcessCaptureRunner)
        # Distinct lifespans never share a runner/registry, so one
        # shutdown cannot reap the other's captures.
        assert second is not first
        assert second is not worker.get_default_runner()
    assert second.registry == {}


def test_composed_test_apps_are_isolated(monkeypatch, tmp_path):
    """Per-app composition never leaks across apps or lifecycles.

    Each built app holds its own frozen composition and owns its
    runner; no per-app mutable override dict is involved, so one
    app's injections cannot affect concurrent requests on another.
    """
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    first_app = build_app()
    second_app = build_app()
    assert first_app.state.composition is not second_app.state.composition
    assert first_app.dependency_overrides == {}
    assert second_app.dependency_overrides == {}
    with TestClient(first_app):
        first_runner = first_app.state.capture_runner
        assert isinstance(first_runner, worker.ProcessCaptureRunner)
    with TestClient(second_app):
        second_runner = second_app.state.capture_runner
        assert isinstance(second_runner, worker.ProcessCaptureRunner)
    assert first_runner is not second_runner
    assert first_runner.registry == {}
    assert second_runner.registry == {}
