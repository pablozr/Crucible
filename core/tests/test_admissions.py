from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import uuid

from fastapi.testclient import TestClient

from crucible_core.main import app


def initialized_repository(tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "config",
            "user.email",
            "test@example.com",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        check=True,
    )
    directory = tmp_path / ".crucible"
    directory.mkdir()
    project_id = str(uuid.uuid4())
    (directory / "project.json").write_text(
        json.dumps({"project_id": project_id}), encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "--quiet", "-m", "initial"],
        check=True,
    )
    return project_id


def candidate(project_id, root, event_id=None):
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "event_type": "input_candidate",
        "occurred_at": "2026-09-06T00:00:00Z",
        "payload_version": 1,
        "adapter": "test-adapter",
        "adapter_version": "1.0",
        "agent_session_id": "session-1",
        "input_id": "input-1",
        "execution_id": "execution-1",
        "project_id": project_id,
        "git_root": str(root),
        "workspace_path": str(root),
        "payload": {"delivery": "new"},
    }


def test_admits_candidate_before_exposing_running_task(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    project_id = initialized_repository(tmp_path / "repo")
    with TestClient(app) as client:
        response = client.post(
            "/v1/events", json=candidate(project_id, tmp_path / "repo")
        )
        assert response.status_code == 200, response.text
        envelope = response.json()
        assert envelope["status"] == "ok"
        assert envelope["message"] == "Event received."
        result = envelope["data"]["event"]
        assert result["outcome"] == "admitted"
        assert result["task_id"]
        assert (
            client.get(f"/v1/events/{result['event_id']}").json()["data"][
                "event"
            ]["task_id"]
            == result["task_id"]
        )
        tasks = client.get("/v1/tasks").json()["data"]["tasks"]
        assert tasks == [
            {
                "id": result["task_id"],
                "status": "running",
                "started_at": tasks[0]["started_at"],
                "worktree": str(tmp_path / "repo"),
                "project_id": project_id,
                "branch": "master",
                "failure_code": None,
                "failure_message": None,
            }
        ]
        detail = client.get(f"/v1/tasks/{result['task_id']}").json()["data"][
            "task"
        ]
        assert detail["input_ids"] == ["input-1"]
        assert detail["baseline_index_manifest"]
        assert detail["baseline_files"] == []
        assert client.get("/").status_code == 404


def test_event_replay_is_idempotent_and_conflicts_are_rejected(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    project_id = initialized_repository(tmp_path / "repo")
    event = candidate(project_id, tmp_path / "repo")
    with TestClient(app) as client:
        first = client.post("/v1/events", json=event)
        replay = client.post("/v1/events", json=event)
        event["payload"] = {"delivery": "new", "prompt": "changed"}
        conflict = client.post("/v1/events", json=event)
    assert replay.json() == first.json()
    assert conflict.status_code == 409
    assert conflict.json()["status"] == "error"
    assert conflict.json()["data"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_input_admission_is_idempotent_across_event_retries(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    project_id = initialized_repository(tmp_path / "repo")
    with TestClient(app) as client:
        first = client.post(
            "/v1/events", json=candidate(project_id, tmp_path / "repo")
        )
        retry = client.post(
            "/v1/events",
            json=candidate(project_id, tmp_path / "repo", str(uuid.uuid4())),
        )
    assert (
        retry.json()["data"]["event"]["task_id"]
        == first.json()["data"]["event"]["task_id"]
    )


def test_dirty_baseline_is_frozen_before_task_admission(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    dirty_file = root / "dirty.txt"
    dirty_file.write_text("before agent", encoding="utf-8")
    with TestClient(app) as client:
        response = client.post("/v1/events", json=candidate(project_id, root))
        task = client.get(
            f"/v1/tasks/{response.json()['data']['event']['task_id']}"
        ).json()["data"]["task"]
    assert response.status_code == 200
    assert task["baseline_status"]
    assert task["baseline_files"] == [
        {
            "path": "dirty.txt",
            "status": "??",
            "sha256": task["baseline_files"][0]["sha256"],
            "size": len("before agent"),
            "is_binary": False,
            "content": task["baseline_files"][0]["content"],
            "mode": None,
            "gitlink_oid": None,
        }
    ]


def test_rejected_candidate_is_durable_diagnostic(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    event = candidate(project_id, root)
    event["project_id"] = str(uuid.uuid4())
    with TestClient(app) as client:
        response = client.post("/v1/events", json=event)
        diagnostic = client.get(f"/v1/events/{event['event_id']}")
    assert response.status_code == 200
    assert response.json()["data"]["event"]["dispatch_authorized"] is False
    assert diagnostic.json()["data"]["event"]["status"] == "rejected"


def test_semantic_retry_bypasses_git_after_worktree_mutation(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        first = client.post("/v1/events", json=candidate(project_id, root))
        (root / "mutated.txt").write_text("later", encoding="utf-8")
        retry = client.post(
            "/v1/events", json=candidate(project_id, root, str(uuid.uuid4()))
        )
    assert (
        retry.json()["data"]["event"]["task_id"]
        == first.json()["data"]["event"]["task_id"]
    )


def test_invalid_cursor_is_problem_json(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    with TestClient(app) as client:
        response = client.get("/v1/tasks?cursor=not-a-cursor")
    assert response.status_code == 400
    assert response.json()["status"] == "error"
    assert response.json()["data"]["code"] == "INVALID_CURSOR"


def test_incomplete_candidate_is_expired_on_startup(monkeypatch, tmp_path):
    data_directory = tmp_path / "data"
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(data_directory))
    with TestClient(app):
        pass
    connection = sqlite3.connect(data_directory / "crucible.db")
    connection.executescript(
        """
        INSERT INTO projects VALUES ('project', '/repo');
        INSERT INTO working_trees (id, project_id, git_root)
        VALUES ('tree', 'project', '/repo');
        INSERT INTO sessions (id, working_tree_id, adapter, agent_session_id)
        VALUES ('session', 'tree', 'adapter', 'session');
        INSERT INTO admission_candidates
        (id, session_id, native_input_id, status, outcome, created_at)
        VALUES ('candidate', 'session', 'input', 'captured', 'candidate', 'now');
        INSERT INTO inbound_events
        (id, payload_hash, status, event_type, received_at, outcome)
        VALUES ('event', 'hash', 'processing', 'input_candidate', 'now', 'candidate');
        """
    )
    connection.commit()
    connection.close()
    with TestClient(app):
        pass
    connection = sqlite3.connect(data_directory / "crucible.db")
    candidate_status = connection.execute(
        "SELECT status, outcome FROM admission_candidates WHERE id = 'candidate'"
    ).fetchone()
    event_status = connection.execute(
        "SELECT status, failure_code FROM inbound_events WHERE id = 'event'"
    ).fetchone()
    connection.close()
    assert candidate_status == ("expired", "CANDIDATE_EXPIRED")
    assert event_status == ("rejected", "CANDIDATE_EXPIRED")


def test_invalid_cursor_logs_each_caught_exception(
    monkeypatch, tmp_path, caplog
):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    with caplog.at_level(logging.WARNING, logger="crucible_core"):
        with TestClient(app) as client:
            response = client.get("/v1/tasks?cursor=not-a-cursor")
    assert response.status_code == 400
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "INVALID_CURSOR" in record.getMessage()
    ]
    assert len(warnings) == 3
    assert "not-a-cursor" not in warnings[0].getMessage()


def test_project_failure_logs_each_caught_exception(
    monkeypatch, tmp_path, caplog
):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    plain = tmp_path / "plain"
    plain.mkdir()
    event = candidate(str(uuid.uuid4()), plain)
    with caplog.at_level(logging.WARNING, logger="crucible_core"):
        with TestClient(app) as client:
            response = client.post("/v1/events", json=event)
    assert response.status_code == 200
    assert response.json()["data"]["event"]["outcome"] == "rejected"
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.name.startswith("crucible_core")
    ]
    assert len(warnings) == 3
    assert event["event_id"] in warnings[-1].getMessage()
