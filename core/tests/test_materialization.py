from __future__ import annotations

import gzip
import sqlite3
import threading
import uuid

import pytest

from crucible_core.application.finalizations import (
    FinalizationCoordinator,
)
from crucible_core.core.database import upgrade
from crucible_core.core.errors import FinalizationError


def _seed_frozen(
    path,
    task_id="task-1",
    *,
    before=b"before\n",
    after=b"after\n",
    evidence="complete",
    final_content=None,
    baseline_content=None,
    extra_changes=(),
):
    upgrade(path)
    final_blob = (
        final_content if final_content is not None else gzip.compress(after)
    )
    base_blob = (
        baseline_content
        if baseline_content is not None
        else gzip.compress(before)
    )
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        INSERT INTO projects VALUES ('project', '/missing');
        INSERT INTO working_trees (id, project_id, git_root)
        VALUES ('tree', 'project', '/missing');
        INSERT INTO sessions (id, working_tree_id, adapter,
          agent_session_id)
        VALUES ('session', 'tree', 'opencode-v1', 'agent');
        """
    )
    connection.execute(
        "INSERT INTO tasks (id, session_id, working_tree_id,"
        " status, started_at, snapshot_frozen_at,"
        " capture_generation, evidence_completeness)"
        " VALUES (?, 'session', 'tree', 'finalizing',"
        " 'now', 'frozen', 1, 'complete')",
        (task_id,),
    )
    connection.execute(
        "INSERT INTO task_baseline_files (id, task_id, path,"
        " status, sha256, size, is_binary, content)"
        " VALUES (?, ?, 'file.txt', '  ', 'a', 7, 0, ?)",
        (str(uuid.uuid4()), task_id, base_blob),
    )
    connection.execute(
        "INSERT INTO task_file_changes (id, task_id, path,"
        " operation, final_status, final_sha256, final_size,"
        " final_is_binary, final_content, evidence_status)"
        " VALUES (?, ?, 'file.txt', 'modified', ' M', 'b',"
        " 6, 0, ?, ?)",
        (str(uuid.uuid4()), task_id, final_blob, evidence),
    )
    for name, blob_before, blob_after in extra_changes:
        connection.execute(
            "INSERT INTO task_baseline_files (id, task_id,"
            " path, status, sha256, size, is_binary, content)"
            " VALUES (?, ?, ?, '  ', 'a', 7, 0, ?)",
            (str(uuid.uuid4()), task_id, name, blob_before),
        )
        connection.execute(
            "INSERT INTO task_file_changes (id, task_id, path,"
            " operation, final_status, final_sha256, final_size,"
            " final_is_binary, final_content, evidence_status)"
            " VALUES (?, ?, ?, 'modified', ' M', 'b', 6, 0,"
            " ?, 'complete')",
            (str(uuid.uuid4()), task_id, name, blob_after),
        )
    connection.execute(
        "INSERT INTO inbound_events (id, payload_hash, status,"
        " event_type, received_at, outcome, task_id)"
        " VALUES ('completion', 'hash', 'processing',"
        " 'task_completed', 'now', 'finalizing', ?)",
        (task_id,),
    )
    connection.commit()
    connection.close()


def _task_state(path, task_id="task-1"):
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT status, failure_code, task_diff FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        patch = connection.execute(
            "SELECT patch FROM task_file_changes"
            " WHERE task_id = ? AND path = 'file.txt'",
            (task_id,),
        ).fetchone()
        event = connection.execute(
            "SELECT status, outcome FROM inbound_events"
            " WHERE id = 'completion'"
        ).fetchone()
    finally:
        connection.close()
    return row, patch, event


def test_writer_free_during_patch_compute(tmp_path):
    path = tmp_path / "crucible.db"
    _seed_frozen(path)
    entered = threading.Event()
    proceed = threading.Event()
    calls = {"count": 0}

    def _hook():
        calls["count"] += 1
        if calls["count"] == 1:
            entered.set()
            assert proceed.wait(timeout=10)

    coordinator = FinalizationCoordinator(path, patch_hook=_hook)
    worker = threading.Thread(target=coordinator.materialize, args=("task-1",))
    worker.start()
    try:
        assert entered.wait(timeout=10)
        # No writer held during compute: a second
        # connection can take BEGIN IMMEDIATE + write.
        second = sqlite3.connect(path, timeout=5)
        try:
            second.execute("BEGIN IMMEDIATE")
            second.execute(
                "INSERT INTO inbound_events (id, payload_hash,"
                " status, event_type, received_at, outcome,"
                " task_id) VALUES (?, 'h', 'processing',"
                " 'task_completed', 'now', 'finalizing',"
                " 'task-1')",
                (str(uuid.uuid4()),),
            )
            second.rollback()
        finally:
            second.close()
    finally:
        proceed.set()
        worker.join(timeout=20)
    assert not worker.is_alive()
    row, patch, _ = _task_state(path)
    assert row[0] == "completed"
    assert patch[0]


def test_corrupt_gzip_fails_definitively(tmp_path):
    path = tmp_path / "crucible.db"
    _seed_frozen(path, final_content=b"not-gzip-at-all")

    def _boom(*args, **kwargs):
        raise AssertionError("materialize must not read Git")

    from crucible_core.infrastructure.git import (
        final_capture_worker as worker,
    )

    coordinator = FinalizationCoordinator(
        path, capture_runner=worker.InlineCaptureRunner(_boom)
    )
    with pytest.raises(FinalizationError) as caught:
        coordinator.materialize("task-1")
    assert caught.value.code == "FINAL_MATERIALIZATION_FAILED"
    row, patch, _ = _task_state(path)
    assert row[0] == "finalizing"
    assert patch[0] is None
    assert row[2] is None
    coordinator.recover()
    row, patch, _ = _task_state(path)
    assert row[0] == "failed"
    assert row[1] == "FINAL_MATERIALIZATION_FAILED"
    assert patch[0] is None
    assert row[2] is None


def test_large_content_never_truncates(tmp_path):
    path = tmp_path / "crucible.db"
    before = b"A" * 1_500_000 + b"\n"
    after = b"B" * 1_500_000 + b"\n"
    second_before = b"C" * 1_500_000 + b"\n"
    second_after = b"D" * 1_500_000 + b"\n"
    _seed_frozen(
        path,
        before=before,
        after=after,
        extra_changes=[
            (
                "second.txt",
                gzip.compress(second_before),
                gzip.compress(second_after),
            )
        ],
    )
    FinalizationCoordinator(path).materialize("task-1")
    connection = sqlite3.connect(path)
    try:
        patches = connection.execute(
            "SELECT path, patch FROM task_file_changes"
            " WHERE task_id = 'task-1' ORDER BY path"
        ).fetchall()
        diff = connection.execute(
            "SELECT status, task_diff FROM tasks WHERE id = 'task-1'"
        ).fetchone()
    finally:
        connection.close()
    assert diff[0] == "completed"
    assert len(patches) == 2
    for _, patch in patches:
        assert patch is not None
        assert len(patch) > 1_048_576
    assert diff[1] is not None
    assert len(diff[1]) > 5_242_880
    assert diff[1] == "".join(patch for _, patch in patches if patch)


def test_recover_retries_without_git(tmp_path):
    path = tmp_path / "crucible.db"
    _seed_frozen(path)

    def _boom(*args, **kwargs):
        raise AssertionError("recovery must not read Git")

    from crucible_core.infrastructure.git import (
        final_capture_worker as worker,
    )

    coordinator = FinalizationCoordinator(
        path, capture_runner=worker.InlineCaptureRunner(_boom)
    )
    coordinator.recover()
    row, patch, event = _task_state(path)
    assert row[0] == "completed"
    assert patch[0]
    assert event == ("accepted", "completed")


def test_transient_lock_retries_without_failure(tmp_path):
    path = tmp_path / "crucible.db"
    _seed_frozen(path)
    holder = sqlite3.connect(path)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE tasks SET failed_at = 'probe' WHERE id = 'task-1'")
    try:
        with pytest.raises(Exception):
            FinalizationCoordinator(path).materialize("task-1")
    finally:
        holder.rollback()
        holder.close()
    row, patch, _ = _task_state(path)
    # A busy writer never converts into corruption or
    # a definitive failure: the task stays finalizing.
    assert row[0] == "finalizing"
    assert row[1] is None
    assert patch[0] is None

    def _boom(*args, **kwargs):
        raise AssertionError("retry must not read Git")

    from crucible_core.infrastructure.git import (
        final_capture_worker as worker,
    )

    FinalizationCoordinator(
        path, capture_runner=worker.InlineCaptureRunner(_boom)
    ).recover()
    row, patch, _ = _task_state(path)
    assert row[0] == "completed"
    assert patch[0]


def test_concurrent_materializations_idempotent(tmp_path):
    path = tmp_path / "crucible.db"
    _seed_frozen(path)
    coordinator = FinalizationCoordinator(path)
    threads = [
        threading.Thread(target=coordinator.materialize, args=("task-1",))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not any(thread.is_alive() for thread in threads)
    first = _task_state(path)
    coordinator.materialize("task-1")
    coordinator.recover()
    second = _task_state(path)
    assert first[0][0] == "completed"
    assert first == second
    assert first[0][2] == second[0][2]


def test_identity_change_blocks_completion(tmp_path):
    path = tmp_path / "crucible.db"
    _seed_frozen(path)
    state = {"done": False}

    def _hook():
        if state["done"]:
            return
        state["done"] = True
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "UPDATE tasks SET evidence_completeness ="
                " 'partial' WHERE id = 'task-1'"
            )
            connection.commit()
        finally:
            connection.close()

    coordinator = FinalizationCoordinator(path, patch_hook=_hook)
    with pytest.raises(FinalizationError) as caught:
        coordinator.materialize("task-1")
    assert caught.value.code == "STALE_CAPTURE_GENERATION"
    row, patch, _ = _task_state(path)
    assert row[0] != "completed"


def test_failed_publish_leaves_nothing_visible(tmp_path):
    path = tmp_path / "crucible.db"
    _seed_frozen(path)
    state = {"done": False}

    def _hook():
        if state["done"]:
            return
        state["done"] = True
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "UPDATE tasks SET status = 'failed',"
                " failure_code = 'OTHER',"
                " failure_message = 'OTHER',"
                " failed_at = 'now' WHERE id = 'task-1'"
            )
            connection.commit()
        finally:
            connection.close()

    coordinator = FinalizationCoordinator(path, patch_hook=_hook)
    with pytest.raises(FinalizationError):
        coordinator.materialize("task-1")
    connection = sqlite3.connect(path)
    try:
        visible = connection.execute(
            "SELECT COUNT(*) FROM task_file_changes"
            " WHERE task_id = 'task-1' AND patch IS NOT NULL"
        ).fetchone()[0]
        diff = connection.execute(
            "SELECT task_diff FROM tasks WHERE id = 'task-1'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert visible == 0
    assert diff is None


def test_invalid_utf8_gzip_fails_definitively(tmp_path):
    path = tmp_path / "crucible.db"
    _seed_frozen(
        path,
        after=b"after\n",
        final_content=gzip.compress(b"\xff\xfe\n\x80bad\n"),
    )
    coordinator = FinalizationCoordinator(path)
    with pytest.raises(FinalizationError) as caught:
        coordinator.materialize("task-1")
    assert caught.value.code == "FINAL_MATERIALIZATION_FAILED"
    row, patch, _ = _task_state(path)
    assert row[0] == "finalizing"
    assert patch[0] is None
    assert row[2] is None
    coordinator.recover()
    row, patch, _ = _task_state(path)
    assert row[0] == "failed"
    assert row[1] == "FINAL_MATERIALIZATION_FAILED"
    assert patch[0] is None
    assert row[2] is None


def test_final_head_mutation_blocks_completion(tmp_path):
    path = tmp_path / "crucible.db"
    _seed_frozen(path)
    state = {"done": False}

    def _hook():
        if state["done"]:
            return
        state["done"] = True
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "UPDATE tasks SET final_head = 'tampered' WHERE id = 'task-1'"
            )
            connection.commit()
        finally:
            connection.close()

    coordinator = FinalizationCoordinator(path, patch_hook=_hook)
    with pytest.raises(FinalizationError) as caught:
        coordinator.materialize("task-1")
    assert caught.value.code == "STALE_CAPTURE_GENERATION"
    row, patch, _ = _task_state(path)
    assert row[0] != "completed"
    assert patch[0] is None
