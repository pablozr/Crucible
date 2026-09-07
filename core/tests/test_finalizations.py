from __future__ import annotations

import gzip
import json
import sqlite3
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import crucible_core.services.finalizations as finalization_service
from crucible_core.application.finalizations import FinalizationCoordinator
from crucible_core.core.database import upgrade
from crucible_core.infrastructure.git.final_capture import capture_final
from crucible_core.main import app

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


def test_completion_freezes_and_materializes_exact_task_diff(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
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
    assert gzip.decompress(frozen) == b"before\n"


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
        monkeypatch.setattr(
            finalization_service,
            "_PUBLICATION_HOOK",
            lambda: finalization_service.fence_unfrozen_finalization(
                database_path(tmp_path), tree_id
            ),
        )
        try:
            response = client.post(
                "/v1/events", json=completion(project_id, root, task_id)
            )
        finally:
            monkeypatch.setattr(
                finalization_service, "_PUBLICATION_HOOK", None
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
        ("head", "UNSUPPORTED_HEAD_STATE"),
        ("index", "UNSUPPORTED_INDEX_STATE"),
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
        elif mutation == "head":
            (root / "commit.txt").write_text("commit\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "--quiet", "-m", "next"],
                check=True,
            )
        else:
            (root / "staged.txt").write_text("staged\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(root), "add", "staged.txt"], check=True
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

    coordinator = FinalizationCoordinator(path, capture_final=capture_final)
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
    with TestClient(app) as client:
        task_a = admit(client, project_id, root)
        (root / "tracked.txt").write_text("after\n", encoding="utf-8")
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

        monkeypatch.setattr(finalization_service, "_PUBLICATION_HOOK", _hook)
        try:
            event_a = completion(project_id, root, task_a)
            response = client.post("/v1/events", json=event_a)
        finally:
            monkeypatch.setattr(
                finalization_service, "_PUBLICATION_HOOK", None
            )

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
    with TestClient(app) as client:
        admitted = client.post("/v1/events", json=candidate(project_id, root))
        assert admitted.status_code == 200, admitted.text
        task_id = admitted.json()["data"]["event"]["task_id"]
        input_row_id = admitted.json()["data"]["event"]["input_id"]
        assert input_row_id
        (root / "tracked.txt").write_text("after\n", encoding="utf-8")
        event = completion(project_id, root, task_id)
        captured: dict[str, object] = {}

        def _hook() -> None:
            replay = client.post("/v1/events", json=event)
            captured["status"] = replay.status_code
            captured["body"] = replay.json()

        monkeypatch.setattr(finalization_service, "_PUBLICATION_HOOK", _hook)
        try:
            first = client.post("/v1/events", json=event)
        finally:
            monkeypatch.setattr(
                finalization_service, "_PUBLICATION_HOOK", None
            )

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
    with TestClient(app) as client:
        task_a = admit(client, project_id, root)
        event = completion(project_id, root, task_a)

        def _boom(*args: object, **kwargs: object) -> object:
            raise RuntimeError("boom")

        monkeypatch.setattr(finalization_service, "_capture_final", _boom)
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
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        event = completion(project_id, root, task_id)
        event["execution_id"] = "execution-other"

        def _boom(*args: object, **kwargs: object) -> object:
            raise AssertionError("capture must not run on mismatch")

        monkeypatch.setattr(finalization_service, "_capture_final", _boom)
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
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        event = completion(project_id, root, task_id)
        event["agent_session_id"] = "session-other"

        def _boom(*args: object, **kwargs: object) -> object:
            raise AssertionError("capture must not run on mismatch")

        monkeypatch.setattr(finalization_service, "_capture_final", _boom)
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
    with TestClient(app) as client:
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

        def _boom(*args: object, **kwargs: object) -> object:
            raise AssertionError("capture must not run on invalid window")

        monkeypatch.setattr(finalization_service, "_capture_final", _boom)
        response = client.post("/v1/events", json=event)
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert response.json()["data"]["code"] == code
    assert detail["status"] == "running"


def test_expired_authorization_aborts_without_git_reads(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    monkeypatch.setattr(
        finalization_service,
        "_CLOCK",
        lambda: datetime(2026, 9, 7, 0, 1, 0, tzinfo=UTC),
    )
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        event = completion(project_id, root, task_id)
        event["payload"]["terminal_observed_at"] = "2026-09-07T00:00:00Z"
        event["payload"]["capture_not_after"] = "2026-09-07T00:00:10Z"

        def _boom(*args: object, **kwargs: object) -> object:
            raise AssertionError("expired capture must not read Git")

        monkeypatch.setattr(finalization_service, "_capture_final", _boom)
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
    monkeypatch.setattr(
        finalization_service,
        "_CLOCK",
        lambda: datetime(2026, 9, 7, 0, 1, 0, tzinfo=UTC),
    )
    with TestClient(app) as client:
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
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        (root / "tracked.txt").write_text("after\n", encoding="utf-8")
        event = completion(project_id, root, task_id)
        event["payload"]["terminal_observed_at"] = "2026-09-07T00:00:30Z"
        event["payload"]["capture_not_after"] = "2026-09-07T00:01:00Z"
        monkeypatch.setattr(
            finalization_service,
            "_CLOCK",
            lambda: datetime(2026, 9, 7, 0, 0, 45, tzinfo=UTC),
        )
        first = client.post("/v1/events", json=event)
        assert first.status_code == 200, first.text
        monkeypatch.setattr(
            finalization_service,
            "_CLOCK",
            lambda: datetime(2026, 9, 7, 0, 2, 0, tzinfo=UTC),
        )
        replay = client.post("/v1/events", json=event)
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert replay.json() == first.json()
    assert detail["status"] == "completed"


def test_new_completion_after_expiry_cannot_renew(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    monkeypatch.setattr(
        finalization_service,
        "_CLOCK",
        lambda: datetime(2026, 9, 7, 0, 1, 0, tzinfo=UTC),
    )
    with TestClient(app) as client:
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
    monkeypatch.setattr(
        finalization_service,
        "_CLOCK",
        lambda: datetime(2026, 9, 7, 0, 1, 0, tzinfo=UTC),
    )
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        event = completion(project_id, root, task_id)
        event["payload"]["terminal_observed_at"] = "2026-09-07T00:00:00Z"
        event["payload"]["capture_not_after"] = "2026-09-07T00:00:10Z"

        def _boom_project(*args: object, **kwargs: object) -> object:
            raise AssertionError("expired must not resolve project")

        def _boom_capture(*args: object, **kwargs: object) -> object:
            raise AssertionError("expired must not read Git")

        monkeypatch.setattr(
            "crucible_core.application.finalizations.resolve_project",
            _boom_project,
        )
        monkeypatch.setattr(
            finalization_service, "_capture_final", _boom_capture
        )
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
    monkeypatch.setattr(
        finalization_service,
        "_CLOCK",
        lambda: datetime(2026, 9, 7, 0, 0, 45, tzinfo=UTC),
    )
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        event = completion(project_id, root, task_id)

        def _boom(*args: object, **kwargs: object) -> object:
            raise AssertionError("excessive window must not read Git")

        monkeypatch.setattr(finalization_service, "_capture_final", _boom)
        response = client.post("/v1/events", json=event)
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert response.json()["data"]["code"] == "INVALID_CAPTURE_WINDOW"
    assert detail["status"] == "running"


def test_future_observation_rejects_before_git(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    monkeypatch.setattr(
        finalization_service,
        "_CLOCK",
        lambda: datetime(2026, 9, 7, 0, 0, 0, tzinfo=UTC),
    )
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        event = completion(project_id, root, task_id)
        event["payload"]["terminal_observed_at"] = "2026-09-07T00:01:00Z"
        event["payload"]["capture_not_after"] = "2026-09-07T00:02:00Z"

        def _boom(*args: object, **kwargs: object) -> object:
            raise AssertionError("future observation must not read Git")

        monkeypatch.setattr(finalization_service, "_capture_final", _boom)
        response = client.post("/v1/events", json=event)
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert response.json()["data"]["code"] == "INVALID_TERMINAL_OBSERVED_AT"
    assert detail["status"] == "running"


def test_missing_window_config_is_fail_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv(TERMINAL_WINDOW_ENV, raising=False)
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        event = completion(project_id, root, task_id)
        event["payload"]["terminal_observed_at"] = "2026-09-07T00:00:30Z"
        event["payload"]["capture_not_after"] = "2026-09-07T00:01:00Z"

        def _boom(*args: object, **kwargs: object) -> object:
            raise AssertionError("unconfigured must not read Git")

        monkeypatch.setattr(finalization_service, "_capture_final", _boom)
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
    with TestClient(app) as client:
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

        def _boom(*args: object, **kwargs: object) -> object:
            raise AssertionError("legacy mismatch must not read Git")

        monkeypatch.setattr(finalization_service, "_capture_final", _boom)
        response = client.post("/v1/events", json=event)
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert response.status_code == 409
    assert response.json()["data"]["code"] == "EXECUTION_ID_MISMATCH"
    assert detail["status"] == "running"


def test_expiry_race_in_begin_rejects_without_capture(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        event = completion(project_id, root, task_id)
        event["payload"]["terminal_observed_at"] = "2026-09-07T00:00:30Z"
        event["payload"]["capture_not_after"] = "2026-09-07T00:01:00Z"
        valid_at = datetime(2026, 9, 7, 0, 0, 45, tzinfo=UTC)
        expired_at = datetime(2026, 9, 7, 0, 1, 1, tzinfo=UTC)
        calls = {"count": 0}

        def _race_clock() -> datetime:
            calls["count"] += 1
            if calls["count"] <= 2:
                return valid_at
            return expired_at

        monkeypatch.setattr(finalization_service, "_CLOCK", _race_clock)

        def _boom(*args: object, **kwargs: object) -> object:
            raise AssertionError("race expiry must not read Git")

        monkeypatch.setattr(finalization_service, "_capture_final", _boom)
        monkeypatch.setattr(
            "crucible_core.application.finalizations.resolve_project",
            _boom,
        )
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
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        event = aborted(project_id, root, task_id)

        def _boom(*args: object, **kwargs: object) -> object:
            raise AssertionError("abort must not read Git")

        monkeypatch.setattr(finalization_service, "_capture_final", _boom)
        monkeypatch.setattr(
            "crucible_core.application.finalizations.resolve_project",
            _boom,
        )
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
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        event = aborted(project_id, root, task_id)

        def _boom(*args: object, **kwargs: object) -> object:
            raise AssertionError("replay must not read Git")

        monkeypatch.setattr(finalization_service, "_capture_final", _boom)
        monkeypatch.setattr(
            "crucible_core.application.finalizations.resolve_project",
            _boom,
        )
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
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        event = aborted(project_id, root, task_id, reason="UNKNOWN_REASON")

        def _boom(*args: object, **kwargs: object) -> object:
            raise AssertionError("invalid abort must not read Git")

        monkeypatch.setattr(finalization_service, "_capture_final", _boom)
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
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)

        def _boom(*args: object, **kwargs: object) -> object:
            raise AssertionError("mismatch must not read Git")

        monkeypatch.setattr(finalization_service, "_capture_final", _boom)
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
    with TestClient(app) as client:
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

        def _boom(*args: object, **kwargs: object) -> object:
            raise AssertionError("finalizing abort must not read Git")

        monkeypatch.setattr(finalization_service, "_capture_final", _boom)
        response = client.post(
            "/v1/events", json=aborted(project_id, root, task_id)
        )
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert response.status_code == 409
    assert response.json()["data"]["code"] == "TASK_NOT_RUNNING"
    assert detail["status"] == "finalizing"
