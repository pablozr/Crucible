from __future__ import annotations

import json
import sqlite3
import subprocess
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

import crucible_core.services.admissions as admissions_service
from crucible_core.main import app


def initialized_repository(tmp_path: Path) -> str:
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


def make_event(
    project_id: str,
    root: Path,
    delivery: str = "new",
    session: str = "session-1",
    input_id: str = "input-1",
    event_id: str | None = None,
) -> dict:
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "event_type": "input_candidate",
        "occurred_at": "2026-09-06T00:00:00Z",
        "payload_version": 1,
        "adapter": "test-adapter",
        "adapter_version": "1.0",
        "agent_session_id": session,
        "input_id": input_id,
        "project_id": project_id,
        "git_root": str(root),
        "workspace_path": str(root),
        "payload": {"delivery": delivery},
    }


def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "crucible.db"


def table_count(path: Path, table: str) -> int:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[
            0
        ]
    finally:
        connection.close()


def test_new_creates_task(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    project_id = initialized_repository(tmp_path / "repo")
    with TestClient(app) as client:
        response = client.post(
            "/v1/events",
            json=make_event(project_id, tmp_path / "repo"),
        )
    assert response.status_code == 200
    event = response.json()["data"]["event"]
    assert event["status"] == "accepted"
    assert event["outcome"] == "admitted"
    assert event["dispatch_authorized"] is True
    assert event["task_id"]
    assert table_count(db_path(tmp_path), "tasks") == 1
    assert table_count(db_path(tmp_path), "inputs") == 1


def test_new_joins_same_session_task(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    project_id = initialized_repository(tmp_path / "repo")
    with TestClient(app) as client:
        first = client.post(
            "/v1/events",
            json=make_event(project_id, tmp_path / "repo"),
        ).json()["data"]["event"]
        calls = []

        def _fail(*args, **kwargs):
            calls.append(1)
            raise AssertionError("baseline must not be captured")

        monkeypatch.setattr(admissions_service, "_capture_baseline", _fail)
        second = client.post(
            "/v1/events",
            json=make_event(
                project_id,
                tmp_path / "repo",
                delivery="new",
                input_id="input-2",
            ),
        ).json()["data"]["event"]
        detail = client.get(f"/v1/tasks/{first['task_id']}").json()["data"][
            "task"
        ]
    assert second["task_id"] == first["task_id"]
    assert second["outcome"] == "admitted"
    assert calls == []
    assert sorted(detail["input_ids"]) == ["input-1", "input-2"]
    assert table_count(db_path(tmp_path), "tasks") == 1
    assert table_count(db_path(tmp_path), "admission_candidates") == 1


def test_steer_joins_same_task_without_recapture(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    project_id = initialized_repository(tmp_path / "repo")
    with TestClient(app) as client:
        first = client.post(
            "/v1/events",
            json=make_event(project_id, tmp_path / "repo"),
        ).json()["data"]["event"]
        monkeypatch.setattr(
            admissions_service,
            "_capture_baseline",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("no capture")
            ),
        )
        steered = client.post(
            "/v1/events",
            json=make_event(
                project_id,
                tmp_path / "repo",
                delivery="steer",
                input_id="input-steer",
            ),
        )
    assert steered.status_code == 200
    body = steered.json()["data"]["event"]
    assert body["task_id"] == first["task_id"]
    assert body["outcome"] == "admitted"
    assert body["dispatch_authorized"] is True
    assert table_count(db_path(tmp_path), "tasks") == 1
    assert table_count(db_path(tmp_path), "inputs") == 2
    assert table_count(db_path(tmp_path), "admission_candidates") == 1


def test_steer_without_active_task_is_409(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    project_id = initialized_repository(tmp_path / "repo")
    monkeypatch.setattr(
        admissions_service,
        "_capture_baseline",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no capture")),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/events",
            json=make_event(project_id, tmp_path / "repo", delivery="steer"),
        )
    assert response.status_code == 409
    envelope = response.json()
    assert envelope["status"] == "error"
    assert envelope["data"]["code"] == "STEER_WITHOUT_ACTIVE_TASK"
    assert "steer" in envelope["message"].lower()
    assert table_count(db_path(tmp_path), "tasks") == 0
    assert table_count(db_path(tmp_path), "inputs") == 0
    assert table_count(db_path(tmp_path), "admission_candidates") == 0
    assert table_count(db_path(tmp_path), "candidate_baseline_files") == 0
    connection = sqlite3.connect(db_path(tmp_path))
    try:
        decision = connection.execute(
            "SELECT outcome FROM admission_no_input_decisions"
        ).fetchone()
        rejected = connection.execute(
            "SELECT status, failure_code FROM inbound_events"
        ).fetchone()
    finally:
        connection.close()
    assert decision[0] == "steer_without_active_task"
    assert rejected == ("rejected", "STEER_WITHOUT_ACTIVE_TASK")


def test_cross_session_new_is_released_overlap(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    project_id = initialized_repository(tmp_path / "repo")
    with TestClient(app) as client:
        first = client.post(
            "/v1/events",
            json=make_event(project_id, tmp_path / "repo"),
        ).json()["data"]["event"]
        monkeypatch.setattr(
            admissions_service,
            "_capture_baseline",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("no capture")
            ),
        )
        overlap = client.post(
            "/v1/events",
            json=make_event(
                project_id,
                tmp_path / "repo",
                session="session-2",
                input_id="input-other",
            ),
        )
    assert overlap.status_code == 200
    body = overlap.json()["data"]["event"]
    assert body["status"] == "accepted"
    assert body["outcome"] == "released_overlap"
    assert body["dispatch_authorized"] is False
    assert body["input_id"] is None
    assert body["task_id"] is None
    assert table_count(db_path(tmp_path), "tasks") == 1
    assert table_count(db_path(tmp_path), "inputs") == 1
    assert table_count(db_path(tmp_path), "admission_candidates") == 1
    connection = sqlite3.connect(db_path(tmp_path))
    try:
        decision = connection.execute(
            "SELECT outcome, reference_task_id "
            "FROM admission_no_input_decisions"
        ).fetchone()
    finally:
        connection.close()
    assert decision == ("released_overlap", first["task_id"])


def test_overlap_replay_and_semantic_idempotency(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    project_id = initialized_repository(tmp_path / "repo")
    with TestClient(app) as client:
        client.post(
            "/v1/events", json=make_event(project_id, tmp_path / "repo")
        )
        event = make_event(
            project_id,
            tmp_path / "repo",
            session="session-2",
            input_id="input-other",
        )
        first = client.post("/v1/events", json=event)
        replay = client.post("/v1/events", json=event)
        semantic = client.post(
            "/v1/events",
            json=make_event(
                project_id,
                tmp_path / "repo",
                session="session-2",
                input_id="input-other",
            ),
        )
    assert replay.json() == first.json()
    assert semantic.json()["data"]["event"]["outcome"] == "released_overlap"
    assert table_count(db_path(tmp_path), "tasks") == 1
    assert table_count(db_path(tmp_path), "inputs") == 1


def test_steer_missing_replay_stays_409(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    project_id = initialized_repository(tmp_path / "repo")
    with TestClient(app) as client:
        event = make_event(project_id, tmp_path / "repo", delivery="steer")
        first = client.post("/v1/events", json=event)
        replay = client.post("/v1/events", json=event)
        semantic = client.post(
            "/v1/events",
            json=make_event(project_id, tmp_path / "repo", delivery="steer"),
        )
    assert first.status_code == 409
    assert replay.status_code == 409
    assert replay.json() == first.json()
    assert semantic.status_code == 409


def test_queue_delivery_stays_unsupported(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    project_id = initialized_repository(tmp_path / "repo")
    with TestClient(app) as client:
        response = client.post(
            "/v1/events",
            json=make_event(project_id, tmp_path / "repo", delivery="queue"),
        )
    assert response.status_code == 400
    assert response.json()["data"]["code"] == "UNSUPPORTED_DELIVERY"
    assert table_count(db_path(tmp_path), "tasks") == 0
