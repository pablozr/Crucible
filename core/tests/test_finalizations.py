from __future__ import annotations

import gzip
import json
import sqlite3
import subprocess
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import crucible_core.services.finalizations as finalization_service
from crucible_core.application.finalizations import FinalizationCoordinator
from crucible_core.core.database import upgrade
from crucible_core.infrastructure.git.final_capture import capture_final
from crucible_core.main import app


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
        followed = client.post("/v1/events", json=next_candidate)
    assert followed.status_code == 200, followed.text
    assert followed.json()["data"]["event"]["outcome"] == "admitted"
    assert followed.json()["data"]["event"]["task_id"] != task_a
