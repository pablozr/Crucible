from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import crucible_core.infrastructure.git.final_capture as final_capture
from crucible_core.core.errors import FinalizationError
from crucible_core.infrastructure.git import (
    final_capture_worker as worker,
)
from crucible_core.main import app, build_app
from crucible_core.schemas.finalizations import FinalCaptureSnapshot
from crucible_core.schemas.persistence import (
    TaskFileChangeRow,
)

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


def completion(project_id: str, root: Path, task_id: str) -> dict[str, object]:
    return {
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


def database_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "crucible.db"


def _sync_worktree_executable(path: Path) -> None:
    # POSIX with core.filemode=true honors worktree exec bits via lstat:
    # a staged `update-index --chmod=+x` alone leaves the worktree 644,
    # so Git reports `MM`/dirty instead of the intended staged `M `.
    # Mark the worktree executable where POSIX requires it so the final
    # worktree matches the intended index mode; Windows keeps prior
    # behavior (filemode=false ignores worktree bits).
    if os.name != "nt":
        mode = path.stat().st_mode
        path.chmod(mode | 0o111)


def admit(client: TestClient, project_id: str, root: Path) -> str:
    response = client.post("/v1/events", json=candidate(project_id, root))
    assert response.status_code == 200, response.text
    return response.json()["data"]["event"]["task_id"]


def task_changes(tmp_path: Path, task_id: str):
    connection = sqlite3.connect(database_path(tmp_path))
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT path, operation, evidence_status, evidence_reason, "
            "final_sha256, final_size, baseline_mode, "
            "baseline_gitlink_oid, final_mode, final_gitlink_oid "
            "FROM task_file_changes "
            "WHERE task_id = ? ORDER BY path",
            (task_id,),
        ).fetchall()
        task = connection.execute(
            "SELECT status, evidence_completeness, snapshot_frozen_at, "
            "failure_code FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    finally:
        connection.close()
    return [dict(row) for row in rows], dict(task)


def test_text_complete_via_api(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        (root / "tracked.txt").write_text("after\n", encoding="utf-8")
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert response.status_code == 200, response.text
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert detail["status"] == "completed"
    assert detail["evidence_completeness"] == "complete"
    changes, _ = task_changes(tmp_path, task_id)
    assert len(changes) == 1
    assert changes[0]["evidence_status"] == "complete"
    assert changes[0]["evidence_reason"] is None


def test_binary_partial_via_api(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        (root / "tracked.txt").write_bytes(b"a\x00b\x00c\n")
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert response.status_code == 200, response.text
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert detail["status"] == "completed"
    assert detail["evidence_completeness"] == "partial"
    changes, _ = task_changes(tmp_path, task_id)
    assert changes[0]["evidence_status"] == "hash_only"
    assert changes[0]["evidence_reason"] == "BINARY_CONTENT"
    assert changes[0]["final_sha256"]
    assert detail["task_diff"] in (None, "")


def test_oversize_partial_via_api(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    (root / ".crucible" / "config.yaml").write_text(
        "version: 1\ntracking:\n  max_snapshot_file_size_bytes: 8\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "--quiet", "-m", "limit"],
        check=True,
    )
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        (root / "tracked.txt").write_text(
            "after with long content\n", encoding="utf-8"
        )
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert response.status_code == 200, response.text
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert detail["status"] == "completed"
    assert detail["evidence_completeness"] == "partial"
    changes, _ = task_changes(tmp_path, task_id)
    assert changes[0]["evidence_status"] == "hash_only"
    assert changes[0]["evidence_reason"] == "SNAPSHOT_SIZE_LIMIT"


def test_committed_symlink_no_follow_via_api(monkeypatch, tmp_path):
    # Committed symlink FS needs privileges; prove API partial via
    # injected structural snapshot plus real git plumbing classification.
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    blob = (
        subprocess.run(
            ["git", "-C", str(root), "hash-object", "-w", "--stdin"],
            input=b"tracked.txt",
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "update-index",
            "--add",
            "--cacheinfo",
            f"120000,{blob},link.txt",
        ],
        check=True,
    )
    # Plumbing proves symlink is classified structurally, never followed.
    status = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-s", "--", "link.txt"],
        capture_output=True,
        check=True,
    ).stdout
    assert b"120000" in status
    shown = subprocess.run(
        ["git", "-C", str(root), "cat-file", "-p", blob],
        capture_output=True,
        check=True,
    ).stdout
    assert shown == b"tracked.txt"
    import gzip
    import hashlib

    link_bytes = b"tracked.txt"
    row = final_capture._symlink_row("link.txt", "  ", link_bytes, 1024)
    assert row.sha256 == hashlib.sha256(link_bytes).hexdigest()
    assert gzip.decompress(bytes(row.content)) == link_bytes

    def _structural(*args, **kwargs):
        # Worktree cannot materialize the symlink here; inject the honest
        # structural capture through the public finalization API.
        return FinalCaptureSnapshot(
            head="h",
            branch="b",
            status=b"",
            index=b"manifest",
            baseline_files=[],
            changes=[
                TaskFileChangeRow(
                    path="link.txt",
                    operation="added",
                    final_status="  ",
                    final_sha256=row.sha256,
                    final_size=row.size,
                    final_is_binary=0,
                    final_content=row.content,
                    evidence_status="unsupported",
                    evidence_reason="SYMLINK_TARGET",
                )
            ],
        )

    test_app = build_app(
        capture_runner=worker.InlineCaptureRunner(_structural)
    )
    with TestClient(test_app) as client:
        task_id = admit(client, project_id, root)
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert response.status_code == 200, response.text
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert detail["status"] == "completed"
    assert detail["evidence_completeness"] == "partial"
    changes, _ = task_changes(tmp_path, task_id)
    link = [item for item in changes if item["path"] == "link.txt"]
    assert len(link) == 1
    assert link[0]["evidence_status"] == "unsupported"
    assert link[0]["evidence_reason"] == "SYMLINK_TARGET"
    expected = hashlib.sha256(b"tracked.txt").hexdigest()
    assert link[0]["final_sha256"] == expected


def test_worktree_symlink_no_follow_unit(monkeypatch, tmp_path):
    # FS symlinks need privileges on Windows; cover no-follow by unit.
    pytest.importorskip("os")
    import os

    root = tmp_path / "repo"
    root.mkdir()
    target = root / "real.txt"
    target.write_text("destination\n", encoding="utf-8")
    link = root / "link.txt"
    try:
        os.symlink("real.txt", link)
    except OSError:
        pytest.skip("FS does not support symlinks here")
    assert link.is_symlink()
    raw = final_capture._readlink_bytes(link)
    assert raw == b"real.txt"
    row = final_capture._symlink_row("link.txt", "??", raw, 1024)
    assert row.sha256 is not None
    status, reason = final_capture._evidence_for_change(
        None, row, "SYMLINK_TARGET"
    )
    assert (status, reason) == ("unsupported", "SYMLINK_TARGET")


def test_mode_only_structural_via_api(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "update-index",
                "--chmod=+x",
                "tracked.txt",
            ],
            check=True,
        )
        _sync_worktree_executable(root / "tracked.txt")
        subprocess.run(
            ["git", "-C", str(root), "commit", "--quiet", "-m", "mode"],
            check=True,
        )
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert response.status_code == 200, response.text
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert detail["status"] == "completed"
    assert detail["evidence_completeness"] == "partial"
    changes, _ = task_changes(tmp_path, task_id)
    assert len(changes) == 1
    assert changes[0]["path"] == "tracked.txt"
    assert changes[0]["evidence_status"] == "unsupported"
    assert changes[0]["evidence_reason"] == "MODE_ONLY_CHANGE"


def test_gitlink_structural_via_api(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    head = (
        subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{head},submod",
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        pytest.skip("Git local does not support gitlinks here")
    status = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-s", "--", "submod"],
        capture_output=True,
        check=True,
    ).stdout
    assert b"160000" in status
    entry = final_capture._TreeEntry(mode="160000", kind="commit")
    assert final_capture._reason_for_entry(entry) == "GITLINK_CONTENT"

    def _structural(*args, **kwargs):
        return FinalCaptureSnapshot(
            head="h",
            branch="b",
            status=b"",
            index=b"manifest",
            baseline_files=[],
            changes=[
                TaskFileChangeRow(
                    path="submod",
                    operation="added",
                    final_status="  ",
                    final_sha256=None,
                    final_size=None,
                    final_is_binary=None,
                    final_content=None,
                    evidence_status="unsupported",
                    evidence_reason="GITLINK_CONTENT",
                )
            ],
        )

    test_app = build_app(
        capture_runner=worker.InlineCaptureRunner(_structural)
    )
    with TestClient(test_app) as client:
        task_id = admit(client, project_id, root)
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "update-index",
                "--force-remove",
                "submod",
            ],
            check=True,
        )
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert response.status_code == 200, response.text
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert detail["status"] == "completed"
    assert detail["evidence_completeness"] == "partial"
    changes, _ = task_changes(tmp_path, task_id)
    sub = [item for item in changes if item["path"] == "submod"]
    assert len(sub) == 1
    assert sub[0]["evidence_status"] == "unsupported"
    assert sub[0]["evidence_reason"] == "GITLINK_CONTENT"


def test_rename_stays_delete_add_via_api(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        subprocess.run(
            ["git", "-C", str(root), "mv", "tracked.txt", "renamed.txt"],
            check=True,
        )
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert response.status_code == 200, response.text
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert detail["status"] == "completed"
    changes, _ = task_changes(tmp_path, task_id)
    by_path = {item["path"]: item for item in changes}
    assert set(by_path) == {"renamed.txt", "tracked.txt"}
    assert by_path["tracked.txt"]["operation"] == "deleted"
    assert by_path["renamed.txt"]["operation"] == "added"
    # No rename heuristics: plain delete+add with honest content.
    assert by_path["renamed.txt"]["evidence_status"] == "complete"


def test_rename_parser_has_no_heuristic_authority():
    parsed = final_capture._parse_status(b"R  renamed.txt\x00tracked.txt\x00")
    assert parsed == {"tracked.txt": "D ", "renamed.txt": "R "}


def test_ignored_stays_out_of_scope_via_api(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "--quiet", "-m", "ignore"],
        check=True,
    )
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        (root / "ignored.txt").write_text("secret\n", encoding="utf-8")
        (root / "tracked.txt").write_text("after\n", encoding="utf-8")
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert response.status_code == 200, response.text
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert detail["status"] == "completed"
    changes, _ = task_changes(tmp_path, task_id)
    assert {item["path"] for item in changes} == {"tracked.txt"}


def test_unavailable_fails_before_freeze_via_api(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)

    def _unavailable(*args, **kwargs):
        return FinalCaptureSnapshot(
            head="h",
            branch="b",
            status=b"",
            index=b"manifest",
            baseline_files=[],
            changes=[
                TaskFileChangeRow(
                    path="tracked.txt",
                    operation="modified",
                    final_status=" M",
                    final_sha256=None,
                    final_size=None,
                    final_is_binary=None,
                    final_content=None,
                    evidence_status="unavailable",
                    evidence_reason="BASELINE_OBJECT_MISSING",
                )
            ],
        )

    test_app = build_app(
        capture_runner=worker.InlineCaptureRunner(_unavailable)
    )
    with TestClient(test_app) as client:
        task_id = admit(client, project_id, root)
        (root / "tracked.txt").write_text("after\n", encoding="utf-8")
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert response.status_code == 400
    assert response.json()["data"]["code"] == "BASELINE_OBJECT_UNAVAILABLE"
    assert detail["status"] == "failed"
    assert detail["failure_code"] == "BASELINE_OBJECT_UNAVAILABLE"
    assert detail["snapshot_frozen_at"] is None
    connection = sqlite3.connect(database_path(tmp_path))
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM task_file_changes WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 0


def test_non_utf8_path_fails_explicitly_without_lossy():
    bad = b"\xff\xfe invalid"
    with pytest.raises(FinalizationError) as status_error:
        final_capture._parse_status(b"?? " + bad + b"\x00")
    assert status_error.value.code == "UNSUPPORTED_FINAL_PATH"
    # Direct strict decode check: no surrogateescape round-trip.
    with pytest.raises(UnicodeDecodeError):
        bad.decode("utf-8")
    # Baseline matcher never encodes lossy paths.
    with pytest.raises(FinalizationError):
        final_capture._match_tree_entry(
            b"100644 blob abc\tbad\x00", "bad\ud800"
        )


def test_baseline_object_unavailable_only_when_genuinely_missing(tmp_path):
    from crucible_core.infrastructure.git.content_hash import HashBudget

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "t@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "T"], check=True
    )
    (root / "a.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "--quiet", "-m", "init"], check=True
    )
    budget = HashBudget(aggregate_budget=10.0, per_file_budget=10.0)
    # Missing blob object fails as unavailable (genuinely needed).
    with pytest.raises(FinalizationError) as error:
        final_capture._stream_git_blob_to_row(
            root,
            "0" * 40,
            "a.txt",
            "  ",
            1024,
            1_000_000_000.0,
            lambda: 100.0,
            budget,
        )
    assert error.value.code == "BASELINE_OBJECT_UNAVAILABLE"
    # Structural gitlink never claims unavailable content.
    entry = final_capture._TreeEntry(mode="160000", kind="commit")
    assert final_capture._reason_for_entry(entry) == "GITLINK_CONTENT"


def test_staged_symlink_real_capture_via_api(monkeypatch, tmp_path):
    # Staged symlink (`A `) with an honest worktree: POSIX materializes a
    # real symlink so index (120000) and worktree agree (staged-only);
    # Windows keeps the regular-file simulation (no privilege needed),
    # where staged-blob fallback supplies the link bytes.
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    link_path = root / "link.txt"
    if os.name != "nt":
        try:
            os.symlink("tracked.txt", link_path)
        except OSError:
            link_path.write_text("tracked.txt", encoding="utf-8")
    else:
        link_path.write_text("tracked.txt", encoding="utf-8")
    blob = (
        subprocess.run(
            ["git", "-C", str(root), "hash-object", "-w", "--stdin"],
            input=b"tracked.txt",
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "update-index",
            "--add",
            "--cacheinfo",
            f"120000,{blob},link.txt",
        ],
        check=True,
    )
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert response.status_code == 200, response.text
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    import hashlib

    assert detail["status"] == "completed"
    assert detail["evidence_completeness"] == "partial"
    changes, _ = task_changes(tmp_path, task_id)
    link = [item for item in changes if item["path"] == "link.txt"][0]
    assert link["evidence_status"] == "unsupported"
    assert link["evidence_reason"] == "SYMLINK_TARGET"
    assert link["final_mode"] == "120000"
    assert link["final_sha256"] == hashlib.sha256(b"tracked.txt").hexdigest()
    api = {item["path"]: item for item in detail["file_changes"]}["link.txt"]
    assert api["final_mode"] == "120000"
    assert api["evidence_reason"] == "SYMLINK_TARGET"


def test_worktree_chmod_staged_not_committed_via_api(monkeypatch, tmp_path):
    # Staged chmod (`update-index --chmod=+x`, no commit, `M `) must be
    # partial MODE_ONLY_CHANGE with persisted index effective mode.
    # Source: live `git ls-files -s -z` (index), never the manifest.
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "update-index",
                "--chmod=+x",
                "tracked.txt",
            ],
            check=True,
        )
        _sync_worktree_executable(root / "tracked.txt")
        status = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            capture_output=True,
            check=True,
        ).stdout
        assert b"M  tracked.txt" in status
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert response.status_code == 200, response.text
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert detail["status"] == "completed"
    assert detail["evidence_completeness"] == "partial"
    changes, _ = task_changes(tmp_path, task_id)
    assert len(changes) == 1
    assert changes[0]["evidence_status"] == "unsupported"
    assert changes[0]["evidence_reason"] == "MODE_ONLY_CHANGE"
    assert changes[0]["baseline_mode"] == "100644"
    assert changes[0]["final_mode"] == "100755"
    api = detail["file_changes"][0]
    assert api["baseline_mode"] == "100644"
    assert api["final_mode"] == "100755"


def _init_submodule_repo(path: Path) -> str:
    subprocess.run(["git", "init", "--quiet", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "t@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "T"], check=True
    )
    (path / "sub.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "--quiet", "-m", "v1"], check=True
    )
    return (
        subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )


def test_gitlink_oid_change_via_api(monkeypatch, tmp_path):
    # Real submodule OID advance: baseline gitlink oid1, staged oid2.
    # Neither advance may disappear; OIDs/modes persisted per path.
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    sub = root / "submod"
    sub.mkdir()
    oid1 = _init_submodule_repo(sub)
    subprocess.run(["git", "-C", str(root), "add", "submod"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "--quiet", "-m", "gitlink1"],
        check=True,
    )
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        (sub / "sub.txt").write_text("v2\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(sub), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(sub), "commit", "--quiet", "-m", "v2"],
            check=True,
        )
        oid2 = (
            subprocess.run(
                ["git", "-C", str(sub), "rev-parse", "HEAD"],
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .strip()
        )
        assert oid1 != oid2
        subprocess.run(["git", "-C", str(root), "add", "submod"], check=True)
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert response.status_code == 200, response.text
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert detail["status"] == "completed"
    assert detail["evidence_completeness"] == "partial"
    changes, _ = task_changes(tmp_path, task_id)
    sub_change = [item for item in changes if item["path"] == "submod"][0]
    assert sub_change["evidence_status"] == "unsupported"
    assert sub_change["evidence_reason"] == "GITLINK_CONTENT"
    assert sub_change["baseline_mode"] == "160000"
    assert sub_change["final_mode"] == "160000"
    assert sub_change["baseline_gitlink_oid"] == oid1
    assert sub_change["final_gitlink_oid"] == oid2
    api = {item["path"]: item for item in detail["file_changes"]}["submod"]
    assert api["baseline_gitlink_oid"] == oid1
    assert api["final_gitlink_oid"] == oid2
    assert api["final_mode"] == "160000"


def test_api_detail_returns_structural_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "update-index",
                "--chmod=+x",
                "tracked.txt",
            ],
            check=True,
        )
        _sync_worktree_executable(root / "tracked.txt")
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert response.status_code == 200, response.text
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    change = detail["file_changes"][0]
    assert change["baseline_mode"] == "100644"
    assert change["final_mode"] == "100755"
    assert "baseline_gitlink_oid" in change
    assert "final_gitlink_oid" in change
    baseline = detail["baseline_files"]
    assert isinstance(baseline, list)


def test_migration_preserves_legacy_nulls_and_writes_structural(tmp_path):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine

    from crucible_core.core.database import connect
    from crucible_core.repositories import tasks_repository
    from crucible_core.schemas.persistence import (
        BaselineFileRow,
        TaskFileChangeRow,
    )

    db = tmp_path / "legacy.db"

    def _upgrade(path, revision: str) -> None:
        config = Config()
        config.set_main_option(
            "script_location",
            str(
                Path(__file__).resolve().parents[1]
                / "src"
                / "crucible_core"
                / "migrations"
            ),
        )
        engine = create_engine(f"sqlite:///{path.as_posix()}")
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, revision)

    _upgrade(db, "0006")
    engine = create_engine(f"sqlite:///{db.as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO projects (id, git_root) VALUES ('p', '/r')"
        )
        connection.exec_driver_sql(
            "INSERT INTO working_trees (id, project_id, git_root) "
            "VALUES ('t', 'p', '/r')"
        )
        connection.exec_driver_sql(
            "INSERT INTO sessions (id, working_tree_id, adapter, "
            "agent_session_id) VALUES ('s', 't', 'a', 'g')"
        )
        connection.exec_driver_sql(
            "INSERT INTO tasks (id, session_id, working_tree_id, status, "
            "started_at) VALUES ('task', 's', 't', 'running', 'now')"
        )
        connection.exec_driver_sql(
            "INSERT INTO task_baseline_files (id, task_id, path, status) "
            "VALUES ('b', 'task', 'f.txt', '  ')"
        )
        connection.exec_driver_sql(
            "INSERT INTO task_file_changes (id, task_id, path, operation, "
            "final_status, evidence_status) VALUES ('c', 'task', 'f.txt', "
            "'modified', ' M', 'complete')"
        )
    _upgrade(db, "head")
    with connect(db) as connection:
        baselines = tasks_repository.list_task_baseline_files(
            connection, "task"
        )
        changes = tasks_repository.list_task_file_changes(connection, "task")
    assert baselines[0].mode is None
    assert baselines[0].gitlink_oid is None
    assert changes[0].baseline_mode is None
    assert changes[0].final_gitlink_oid is None
    with connect(db) as connection:
        connection.execute("BEGIN IMMEDIATE")
        tasks_repository.insert_task_baseline_file(
            connection,
            "b2",
            "task",
            BaselineFileRow(
                path="g.txt",
                status="  ",
                mode="160000",
                gitlink_oid="a" * 40,
            ),
        )
        from crucible_core.repositories import finalizations_repository

        finalizations_repository.insert_file_change(
            connection,
            "task",
            TaskFileChangeRow(
                path="g.txt",
                operation="added",
                final_status="  ",
                evidence_status="unsupported",
                evidence_reason="GITLINK_CONTENT",
                baseline_mode=None,
                baseline_gitlink_oid=None,
                final_mode="160000",
                final_gitlink_oid="a" * 40,
            ),
        )
        connection.commit()
    with connect(db) as connection:
        rows = tasks_repository.list_task_file_changes(connection, "task")
    added = [item for item in rows if item.path == "g.txt"][0]
    assert added.final_mode == "160000"
    assert added.final_gitlink_oid == "a" * 40


def _enable_worktree_modes(root: Path) -> None:
    # core.filemode=true makes Git honor worktree exec bits via lstat,
    # so a staged chmod plus an untouched worktree reports `MM`
    # (staged 100755 + worktree 100644 reversal) with no FS chmod.
    # Portable: no unix-only syscalls, Git computes the worktree mode.
    subprocess.run(
        ["git", "-C", str(root), "config", "core.filemode", "true"],
        check=True,
    )


def test_mixed_mm_chmod_reversal_has_no_change_via_api(monkeypatch, tmp_path):
    # Staged 100755 + unstaged worktree reversal to 100644 (`MM`):
    # final_mode must be the worktree 100644, net zero vs HEAD.
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    _enable_worktree_modes(root)
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "update-index",
                "--chmod=+x",
                "tracked.txt",
            ],
            check=True,
        )
        status = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            capture_output=True,
            check=True,
        ).stdout
        assert b"MM tracked.txt" in status
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert response.status_code == 200, response.text
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert detail["status"] == "completed"
    assert detail["evidence_completeness"] == "complete"
    changes, _ = task_changes(tmp_path, task_id)
    assert [item for item in changes if item["path"] == "tracked.txt"] == []


def test_mixed_mm_content_change_uses_worktree_mode_via_api(
    monkeypatch, tmp_path
):
    # Staged chmod (index 100755) + unstaged content edit (`MM`):
    # bytes differ so the change is complete, but final_mode must be
    # the worktree 100644, never the staged index 100755.
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    _enable_worktree_modes(root)
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "update-index",
                "--chmod=+x",
                "tracked.txt",
            ],
            check=True,
        )
        (root / "tracked.txt").write_text("changed\n", encoding="utf-8")
        status = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            capture_output=True,
            check=True,
        ).stdout
        assert b"MM tracked.txt" in status
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert response.status_code == 200, response.text
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert detail["status"] == "completed"
    changes, _ = task_changes(tmp_path, task_id)
    assert len(changes) == 1
    assert changes[0]["evidence_status"] == "complete"
    assert changes[0]["baseline_mode"] == "100644"
    assert changes[0]["final_mode"] == "100644"
    assert "+changed" in (detail["task_diff"] or "")


def test_staged_symlink_then_worktree_delete_resolves_absent_via_api(
    monkeypatch, tmp_path
):
    # Staged symlink (`A `) then worktree delete (`AD`): worktree truth
    # wins (final None, absence), like regular files; the staged
    # identity lives on only in the index manifest.
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        baseline = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
        (root / "link.txt").write_text("target", encoding="utf-8")
        blob = (
            subprocess.run(
                ["git", "-C", str(root), "hash-object", "-w", "--stdin"],
                input=b"target",
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .strip()
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "update-index",
                "--add",
                "--cacheinfo",
                f"120000,{blob},link.txt",
            ],
            check=True,
        )
        (root / "link.txt").unlink()
        status = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            capture_output=True,
            check=True,
        ).stdout
        assert b"AD link.txt" in status
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert response.status_code == 200, response.text
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert detail["status"] == "completed"
    changes, _ = task_changes(tmp_path, task_id)
    assert [item for item in changes if item["path"] == "link.txt"] == []
    assert detail["evidence_completeness"] == "complete"
    assert detail["final_index_sha256"] != baseline["baseline_index_sha256"]


def test_staged_gitlink_then_worktree_delete_resolves_absent_via_api(
    monkeypatch, tmp_path
):
    # Same worktree-wins rule for gitlinks: staged `AD` gitlink with no
    # worktree dir resolves to absence, index evidence stays separate.
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        baseline = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
        head = (
            subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .strip()
        )
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{head},submod",
            ],
            capture_output=True,
        )
        if result.returncode != 0:
            pytest.skip("Git local does not support gitlinks here")
        status = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            capture_output=True,
            check=True,
        ).stdout
        assert b"AD submod" in status
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert response.status_code == 200, response.text
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert detail["status"] == "completed"
    changes, _ = task_changes(tmp_path, task_id)
    assert [item for item in changes if item["path"] == "submod"] == []
    assert detail["evidence_completeness"] == "complete"
    assert detail["final_index_sha256"] != baseline["baseline_index_sha256"]


def test_md_structural_deleted_keeps_coherent_metadata_via_api(
    monkeypatch, tmp_path
):
    # Baseline regular file, staged symlink over it, worktree deleted
    # (`TD`): operation deleted with coherent baseline structural
    # metadata and absent final identity.
    monkeypatch.setenv("CRUCIBLE_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    project_id = initialized_repository(root)
    (root / "link.txt").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "--quiet", "-m", "link"], check=True
    )
    with TestClient(app) as client:
        task_id = admit(client, project_id, root)
        blob = (
            subprocess.run(
                ["git", "-C", str(root), "hash-object", "-w", "--stdin"],
                input=b"newtarget",
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .strip()
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "update-index",
                "--add",
                "--cacheinfo",
                f"120000,{blob},link.txt",
            ],
            check=True,
        )
        (root / "link.txt").unlink()
        status = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            capture_output=True,
            check=True,
        ).stdout
        assert b"TD link.txt" in status
        response = client.post(
            "/v1/events", json=completion(project_id, root, task_id)
        )
        assert response.status_code == 200, response.text
        detail = client.get(f"/v1/tasks/{task_id}").json()["data"]["task"]
    assert detail["status"] == "completed"
    changes, _ = task_changes(tmp_path, task_id)
    link = [item for item in changes if item["path"] == "link.txt"][0]
    assert link["operation"] == "deleted"
    assert link["baseline_mode"] == "100644"
    assert link["baseline_gitlink_oid"] is None
    # Deleted/absent worktree persists None final identity -- never
    # HEAD/index fallback -- while baseline metadata is preserved and
    # no final content is claimed.
    assert link["final_mode"] is None
    assert link["final_sha256"] is None
    assert link["final_size"] is None
    assert link["final_gitlink_oid"] is None
    assert link["evidence_status"] == "complete"
    api = {item["path"]: item for item in detail["file_changes"]}["link.txt"]
    assert api["operation"] == "deleted"
    assert api["baseline_mode"] == "100644"
    assert api["final_mode"] is None
    assert api["final_gitlink_oid"] is None
