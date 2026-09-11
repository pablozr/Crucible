from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from crucible_core.core.database import connect, upgrade
from crucible_core.main import app
from crucible_core.schemas.admissions import EventRequest
from crucible_core.services.projects import ProjectError, resolve_project
from crucible_core.utils.functions import (
    canonical_json_sha256,
    transport_json_sha256,
)


def _repository(root, project_id=None):
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "t@e.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "T"], check=True
    )
    project_id = project_id or str(uuid.uuid4())
    directory = root / ".crucible"
    directory.mkdir(exist_ok=True)
    (directory / "project.json").write_text(
        json.dumps({"project_id": project_id}), encoding="utf-8"
    )
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "--quiet", "-m", "i"],
        check=True,
    )
    return project_id


def _candidate(project_id, root, event_id=None):
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "event_type": "input_candidate",
        "occurred_at": "2026-09-06T00:00:00.123Z",
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


def _post_raw(client, event):
    raw = json.dumps(event).encode()
    return client.post(
        "/v1/events",
        content=raw,
        headers={"Content-Type": "application/json"},
    ), raw


def _lexical_variant(raw_root: str) -> str:
    # Portable lexical spelling that pathlib canonicalizes on both OSes:
    # Windows swaps separators; POSIX injects a redundant `//` inside
    # the absolute path (never the leading `//`, which is impl-defined).
    if os.name == "nt":
        if "\\" in raw_root:
            return raw_root.replace("\\", "/")
        return raw_root.replace("/", "\\")
    assert raw_root.startswith("/") and not raw_root.startswith("//")
    idx = raw_root.find("/", 1)
    if idx == -1:
        variant = raw_root + "//"
    else:
        variant = raw_root[:idx] + "/" + raw_root[idx:]
    assert variant != raw_root
    assert Path(variant) == Path(raw_root)
    return variant


def test_cursor_rejects_non_list_shapes(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    shapes = [
        "null",
        "123",
        "true",
        json.dumps(["only-one"]),
        json.dumps({"a": 1}),
        json.dumps("just-a-string"),
        json.dumps(["", ""]),
        json.dumps(["a", 1]),
    ]
    with TestClient(app) as client:
        for shape in shapes:
            cursor = base64.urlsafe_b64encode(shape.encode()).decode()
            response = client.get(f"/v1/tasks?cursor={cursor}")
            assert response.status_code == 400, shape
            assert response.json()["data"]["code"] == "INVALID_CURSOR"


def test_project_null_metadata_is_invalid(tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    directory = tmp_path / ".crucible"
    directory.mkdir()
    (directory / "project.json").write_text("null", encoding="utf-8")
    try:
        resolve_project(tmp_path)
    except ProjectError as error:
        assert str(error) == "INVALID_PROJECT_METADATA"
    else:
        raise AssertionError("expected INVALID_PROJECT_METADATA")


def test_connect_closes_and_keeps_pragmas(tmp_path):
    path = tmp_path / "db.sqlite"
    upgrade(path)
    with connect(path) as connection:
        connection.execute(
            "INSERT INTO projects (id, git_root) VALUES ('p', '/r')"
        )
        connection.commit()
        row = connection.execute("PRAGMA journal_mode").fetchone()
        assert row["journal_mode"] == "wal"
    try:
        connection.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        pass
    else:
        raise AssertionError("expected closed connection")
    with connect(path) as connection:
        row = connection.execute(
            "SELECT id FROM projects WHERE id = 'p'"
        ).fetchone()
        assert row["id"] == "p"


def test_connect_propagates_deferred_foreign_key_commit_error(tmp_path):
    path = tmp_path / "deferred.sqlite"
    with connect(path) as connection:
        connection.execute("CREATE TABLE parents (id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE children ("
            "parent_id INTEGER REFERENCES parents(id) "
            "DEFERRABLE INITIALLY DEFERRED)"
        )

    with pytest.raises(sqlite3.IntegrityError):
        with connect(path) as failed_connection:
            failed_connection.execute(
                "INSERT INTO children (parent_id) VALUES (1)"
            )

    with pytest.raises(sqlite3.ProgrammingError):
        failed_connection.execute("SELECT 1")

    with connect(path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM children").fetchone()
        assert count[0] == 0


def test_transport_hash_differs_from_legacy_and_persists(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    project_id = _repository(tmp_path / "repo")
    event = _candidate(project_id, tmp_path / "repo")
    request = EventRequest.model_validate(event)
    legacy = canonical_json_sha256(
        request.model_dump(mode="json", exclude_none=True)
    )
    with TestClient(app) as client:
        response, raw = _post_raw(client, event)
        assert response.status_code == 200, response.text
        transport = transport_json_sha256(raw)
        assert transport != legacy
        db = tmp_path / "data" / "crucible.db"
        stored = (
            sqlite3.connect(db)
            .execute(
                "SELECT payload_hash FROM inbound_events WHERE id = ?",
                (event["event_id"],),
            )
            .fetchone()[0]
        )
        assert stored == transport
        assert stored == hashlib.sha256(raw).hexdigest()
        replay, _ = _post_raw(client, event)
        assert replay.json() == response.json()
        event["payload"] = {"delivery": "new", "prompt": "x"}
        conflict, _ = _post_raw(client, event)
        assert conflict.status_code == 409
        assert conflict.json()["data"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_transport_replay_semantic_equality_reconciles(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = _repository(root)
    event = _candidate(project_id, root)
    with TestClient(app) as client:
        first, raw_first = _post_raw(client, event)
        assert first.status_code == 200, first.text

        replay_event = dict(event)
        replay_event["occurred_at"] = "2026-09-06T00:00:00.123+00:00"
        raw_root = str(root)
        lexical_root = _lexical_variant(raw_root)
        replay_event["git_root"] = lexical_root
        replay_event["workspace_path"] = lexical_root

        legacy_first = canonical_json_sha256(
            EventRequest.model_validate(event).model_dump(
                mode="json", exclude_none=True
            )
        )
        legacy_replay = canonical_json_sha256(
            EventRequest.model_validate(replay_event).model_dump(
                mode="json", exclude_none=True
            )
        )
        assert legacy_first == legacy_replay

        second, raw_second = _post_raw(client, replay_event)
        assert raw_second != raw_first
        assert transport_json_sha256(raw_second) != transport_json_sha256(
            raw_first
        )
        assert second.status_code == 200, second.text
        assert second.json() == first.json()

        conflict_event = dict(event)
        conflict_event["payload"] = {"delivery": "new", "prompt": "x"}
        conflict, _ = _post_raw(client, conflict_event)
        assert conflict.status_code == 409
        assert conflict.json()["data"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_join_active_task_semantic_replay_reconciles(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = _repository(root)
    with TestClient(app) as client:
        first, _ = _post_raw(client, _candidate(project_id, root))
        assert first.status_code == 200, first.text
        task_id = first.json()["data"]["event"]["task_id"]

        join = _candidate(project_id, root)
        join["input_id"] = "input-2"
        joined, raw_join = _post_raw(client, join)
        assert joined.status_code == 200, joined.text
        assert joined.json()["data"]["event"]["task_id"] == task_id

        db = tmp_path / "data" / "crucible.db"
        stored = (
            sqlite3.connect(db)
            .execute(
                "SELECT payload_hash, semantic_hash FROM inbound_events "
                "WHERE id = ?",
                (join["event_id"],),
            )
            .fetchone()
        )
        assert stored[0] == transport_json_sha256(raw_join)
        assert stored[1] == canonical_json_sha256(
            EventRequest.model_validate(join).model_dump(
                mode="json", exclude_none=True
            )
        )

        replay = dict(join)
        replay["occurred_at"] = "2026-09-06T00:00:00.123+00:00"
        raw_root = str(root)
        lexical_root = _lexical_variant(raw_root)
        replay["git_root"] = lexical_root
        replay["workspace_path"] = lexical_root
        assert (
            canonical_json_sha256(
                EventRequest.model_validate(replay).model_dump(
                    mode="json", exclude_none=True
                )
            )
            == stored[1]
        )
        second, raw_second = _post_raw(client, replay)
        assert raw_second != raw_join
        assert second.status_code == 200, second.text
        assert second.json() == joined.json()

        conflict = dict(join)
        conflict["payload"] = {"delivery": "new", "prompt": "x"}
        conflicted, _ = _post_raw(client, conflict)
        assert conflicted.status_code == 409
        assert conflicted.json()["data"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_terminal_abort_semantic_replay_reconciles(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = _repository(root)
    candidate = {
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
    with TestClient(app) as client:
        admitted = client.post("/v1/events", json=candidate)
        assert admitted.status_code == 200, admitted.text
        task_id = admitted.json()["data"]["event"]["task_id"]
        abort = {
            "event_id": str(uuid.uuid4()),
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
            "payload": {
                "task_id": task_id,
                "abort_reason": "DISPATCH_FAILED",
            },
        }
        first, raw_first = _post_raw(client, abort)
        assert first.status_code == 200, first.text

        replay = dict(abort)
        replay["occurred_at"] = "2026-09-07T00:01:00+00:00"
        raw_root = str(root)
        lexical_root = _lexical_variant(raw_root)
        replay["git_root"] = lexical_root
        replay["workspace_path"] = lexical_root
        legacy_first = canonical_json_sha256(
            EventRequest.model_validate(abort).model_dump(
                mode="json", exclude_none=True
            )
        )
        legacy_replay = canonical_json_sha256(
            EventRequest.model_validate(replay).model_dump(
                mode="json", exclude_none=True
            )
        )
        assert legacy_first == legacy_replay
        second, raw_second = _post_raw(client, replay)
        assert raw_second != raw_first
        assert second.status_code == 200, second.text
        assert second.json() == first.json()

        detail = client.get(f"/v1/events/{abort['event_id']}")
        assert detail.json()["data"]["event"]["payload_hash"] == (
            transport_json_sha256(raw_first)
        )

        conflict = dict(abort)
        conflict["payload"] = {
            "task_id": task_id,
            "abort_reason": "TERMINAL_SIGNAL_MISMATCH",
        }
        conflicted, _ = _post_raw(client, conflict)
        assert conflicted.status_code == 409
        assert conflicted.json()["data"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_legacy_hash_replay_reconciles(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    project_id = _repository(tmp_path / "repo")
    event = _candidate(project_id, tmp_path / "repo")
    request = EventRequest.model_validate(event)
    legacy = canonical_json_sha256(
        request.model_dump(mode="json", exclude_none=True)
    )
    with TestClient(app) as client:
        first, raw = _post_raw(client, event)
        assert first.status_code == 200, first.text
        assert transport_json_sha256(raw) != legacy
        db = tmp_path / "data" / "crucible.db"
        connection = sqlite3.connect(db)
        connection.execute(
            "UPDATE inbound_events SET payload_hash = ?, "
            "semantic_hash = NULL WHERE id = ?",
            (legacy, event["event_id"]),
        )
        connection.commit()
        connection.close()
        replay, _ = _post_raw(client, event)
        assert replay.status_code == 200, replay.text
        assert replay.json() == first.json()
