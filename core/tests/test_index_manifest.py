from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import subprocess
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from crucible_core.infrastructure.git.index_manifest import (
    INDEX_MANIFEST_VERSION,
    build_canonical_manifest,
    manifest_sha256,
    normalize_stored_manifest,
    parse_canonical_manifest,
    parse_ls_files,
    serialize_manifest,
    sha256_for_stored,
)
from crucible_core.main import app

OID_A = "a" * 40
OID_B = "b" * 40


def _raw(entries: list[tuple[bytes, str, str]]) -> bytes:
    out = b""
    for path, oid, mode in entries:
        out += f"{mode} {oid} 0\t".encode("ascii") + path + b"\0"
    return out


def test_parse_basic_and_roundtrip() -> None:
    raw = _raw([(b"a.txt", OID_A, "100644")])
    entries = parse_ls_files(raw)
    assert entries == [(b"a.txt", OID_A, "100644")]
    canonical = build_canonical_manifest(raw)
    assert parse_canonical_manifest(canonical) == entries


def test_non_utf8_path_is_lossless() -> None:
    raw = _raw([(b"\xff\xfe_name", OID_A, "100644")])
    entries = parse_ls_files(raw)
    assert entries[0][0] == b"\xff\xfe_name"
    canonical = build_canonical_manifest(raw)
    payload = json.loads(canonical.decode("utf-8"))
    assert payload["version"] == INDEX_MANIFEST_VERSION
    assert base64.b64decode(payload["entries"][0]["path_b64"]) == (
        b"\xff\xfe_name"
    )
    assert parse_canonical_manifest(canonical) == entries


def test_determinism_and_hash() -> None:
    first = _raw([(b"b.txt", OID_B, "100644"), (b"a.txt", OID_A, "100755")])
    second = _raw([(b"a.txt", OID_A, "100755"), (b"b.txt", OID_B, "100644")])
    assert build_canonical_manifest(first) == build_canonical_manifest(second)
    canonical = build_canonical_manifest(first)
    assert manifest_sha256(canonical) == hashlib.sha256(canonical).hexdigest()
    assert len(manifest_sha256(canonical)) == 64
    other = build_canonical_manifest(_raw([(b"a.txt", OID_B, "100644")]))
    assert manifest_sha256(other) != manifest_sha256(canonical)
    payload = json.loads(canonical.decode("utf-8"))
    assert [item["path_b64"] for item in payload["entries"]] == sorted(
        [item["path_b64"] for item in payload["entries"]]
    )


def test_rejects_malformed_manifests() -> None:
    try:
        parse_ls_files(b"garbage\0")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
    try:
        parse_canonical_manifest(b'{"version":999,"entries":[]}')
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
    unordered = json.dumps(
        {
            "entries": [
                {
                    "mode": "100644",
                    "oid": OID_B,
                    "path_b64": base64.b64encode(b"b").decode(),
                },
                {
                    "mode": "100644",
                    "oid": OID_A,
                    "path_b64": base64.b64encode(b"a").decode(),
                },
            ],
            "version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    try:
        parse_canonical_manifest(unordered)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
    assert sha256_for_stored(b"not-a-manifest") is None


def test_legacy_raw_normalizes_to_canonical() -> None:
    raw = _raw([(b"a.txt", OID_A, "100644")])
    assert normalize_stored_manifest(raw) == build_canonical_manifest(raw)
    canonical = build_canonical_manifest(raw)
    assert normalize_stored_manifest(canonical) == canonical
    assert sha256_for_stored(raw) == manifest_sha256(canonical)
    assert serialize_manifest([]) == normalize_stored_manifest(b"")


def _git_repo(root: Path) -> str:
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "t@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "T"], check=True
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
        ["git", "-C", str(root), "commit", "--quiet", "-m", "init"],
        check=True,
    )
    return project_id


def _candidate(project_id: str, root: Path) -> dict[str, object]:
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


def test_persistence_and_api_detail_expose_versioned_evidence(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(
        "CRUCIBLE_TERMINAL_MAX_AUTH_WINDOW_SECONDS", "3000000000"
    )
    root = tmp_path / "repo"
    project_id = _git_repo(root)
    with TestClient(app) as client:
        admitted = client.post("/v1/events", json=_candidate(project_id, root))
        assert admitted.status_code == 200, admitted.text
        task_id = admitted.json()["data"]["event"]["task_id"]
        listed = client.get("/v1/tasks").json()["data"]["tasks"][0]
        assert "baseline_index_manifest" not in listed
        assert "baseline_index_sha256" not in listed
        assert "final_index_manifest" not in listed

        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
        assert detail["baseline_index_manifest"]
        assert detail["baseline_index_sha256"]
        canonical = base64.b64decode(detail["baseline_index_manifest"])
        payload = json.loads(canonical.decode("utf-8"))
        assert payload["version"] == 1
        assert all(
            set(item) == {"mode", "oid", "path_b64"}
            for item in payload["entries"]
        )
        assert (
            detail["baseline_index_sha256"]
            == hashlib.sha256(canonical).hexdigest()
        )

        connection = sqlite3.connect(tmp_path / "data" / "crucible.db")
        try:
            stored = connection.execute(
                "SELECT baseline_index_manifest FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        assert stored == canonical

        (root / "tracked.txt").write_text("after\n", encoding="utf-8")
        completed = {
            "event_id": str(uuid.uuid4()),
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
        done = client.post("/v1/events", json=completed)
        assert done.status_code == 200, done.text
        final = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
        assert final["status"] == "completed"
        assert final["final_index_manifest"]
        assert final["final_index_sha256"]
        assert final["final_index_sha256"] == (detail["baseline_index_sha256"])
        assert final["task_diff"] != final["final_index_manifest"]


def test_task_detail_normalizes_legacy_raw_manifest(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(
        "CRUCIBLE_TERMINAL_MAX_AUTH_WINDOW_SECONDS", "3000000000"
    )
    root = tmp_path / "repo"
    project_id = _git_repo(root)
    raw = _raw([(b"b.txt", OID_B, "100644"), (b"a.txt", OID_A, "100755")])
    expected_canonical = build_canonical_manifest(raw)
    expected_sha = hashlib.sha256(expected_canonical).hexdigest()
    with TestClient(app) as client:
        admitted = client.post("/v1/events", json=_candidate(project_id, root))
        assert admitted.status_code == 200, admitted.text
        task_id = admitted.json()["data"]["event"]["task_id"]
        db_path = tmp_path / "data" / "crucible.db"

        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                "UPDATE tasks SET baseline_index_manifest = ?, "
                "final_index_manifest = ? WHERE id = ?",
                (raw, raw, task_id),
            )
            connection.commit()
        finally:
            connection.close()

        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
        assert (
            base64.b64decode(detail["baseline_index_manifest"])
            == expected_canonical
        )
        assert detail["baseline_index_sha256"] == expected_sha
        assert (
            base64.b64decode(detail["final_index_manifest"])
            == expected_canonical
        )
        assert detail["final_index_sha256"] == expected_sha

        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                "UPDATE tasks SET baseline_index_manifest = ?, "
                "final_index_manifest = ? WHERE id = ?",
                (b"not-a-manifest", b"not-a-manifest", task_id),
            )
            connection.commit()
        finally:
            connection.close()

        malformed = client.get(f"/v1/tasks/{task_id}")
        assert malformed.status_code == 200, malformed.text
        malformed_detail = malformed.json()["data"]["task"]
        assert malformed_detail["baseline_index_manifest"] is None
        assert malformed_detail["baseline_index_sha256"] is None
        assert malformed_detail["final_index_manifest"] is None
        assert malformed_detail["final_index_sha256"] is None

        connection = sqlite3.connect(db_path)
        try:
            connection.execute(
                "UPDATE tasks SET baseline_index_manifest = ?, "
                "final_index_manifest = ? WHERE id = ?",
                (None, None, task_id),
            )
            connection.commit()
        finally:
            connection.close()

        absent = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
        assert absent["baseline_index_manifest"] is None
        assert absent["baseline_index_sha256"] is None
        assert absent["final_index_manifest"] is None
        assert absent["final_index_sha256"] is None


def test_index_change_still_rejected_without_slice_72(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(
        "CRUCIBLE_TERMINAL_MAX_AUTH_WINDOW_SECONDS", "3000000000"
    )
    root = tmp_path / "repo"
    project_id = _git_repo(root)
    with TestClient(app) as client:
        admitted = client.post("/v1/events", json=_candidate(project_id, root))
        task_id = admitted.json()["data"]["event"]["task_id"]
        (root / "staged.txt").write_text("staged\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(root), "add", "staged.txt"], check=True
        )
        event = {
            "event_id": str(uuid.uuid4()),
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
        response = client.post("/v1/events", json=event)
    assert response.status_code == 400
    assert response.json()["data"]["code"] == "UNSUPPORTED_INDEX_STATE"
