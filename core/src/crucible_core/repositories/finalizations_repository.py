from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass

from crucible_core.schemas.persistence import (
    BaselineFileRow,
    FinalizationEventRow,
    FrozenTaskState,
    GenerationRef,
    RecoveryTaskRef,
    TaskFileChangeRow,
)


def input_belongs_to_task(
    connection: sqlite3.Connection,
    task_id: str,
    session_id: str,
    input_id: str,
) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM inputs WHERE task_id = ? AND session_id = ? "
            "AND input_id = ?",
            (task_id, session_id, input_id),
        ).fetchone()
        is not None
    )


def _has_semantic_hash(connection: sqlite3.Connection) -> bool:
    saved = connection.row_factory
    try:
        connection.row_factory = None
        rows = connection.execute(
            "PRAGMA table_info(inbound_events)"
        ).fetchall()
    finally:
        connection.row_factory = saved
    return any(row[1] == "semantic_hash" for row in rows)


def get_finalization_event(
    connection: sqlite3.Connection, event_id: str
) -> FinalizationEventRow | None:
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT payload_hash, semantic_hash, status, outcome, "
            "input_id, task_id, failure_code FROM inbound_events "
            "WHERE id = ?",
            (event_id,),
        ).fetchone()
    except sqlite3.OperationalError as error:
        if "semantic_hash" not in str(error):
            raise
        row = connection.execute(
            "SELECT payload_hash, status, outcome, input_id, task_id, "
            "failure_code FROM inbound_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        return FinalizationEventRow(
            payload_hash=row["payload_hash"],
            semantic_hash=None,
            status=row["status"],
            outcome=row["outcome"],
            input_id=row["input_id"],
            task_id=row["task_id"],
            failure_code=row["failure_code"],
        )
    if row is None:
        return None

    return FinalizationEventRow(
        payload_hash=row["payload_hash"],
        semantic_hash=row["semantic_hash"],
        status=row["status"],
        outcome=row["outcome"],
        input_id=row["input_id"],
        task_id=row["task_id"],
        failure_code=row["failure_code"],
    )


def insert_processing_event(
    connection: sqlite3.Connection,
    event_id: str,
    payload_hash: str,
    event_type: str,
    received_at: str,
    outcome: str,
    input_id: str,
    task_id: str,
    semantic_hash: str | None = None,
) -> None:
    if _has_semantic_hash(connection):
        connection.execute(
            "INSERT INTO inbound_events (id, payload_hash, semantic_hash, "
            "status, event_type, received_at, outcome, input_id, task_id) "
            "VALUES (?, ?, ?, 'processing', ?, ?, ?, ?, ?)",
            (
                event_id,
                payload_hash,
                semantic_hash,
                event_type,
                received_at,
                outcome,
                input_id,
                task_id,
            ),
        )
        return
    connection.execute(
        "INSERT INTO inbound_events (id, payload_hash, status, "
        "event_type, received_at, outcome, input_id, task_id) "
        "VALUES (?, ?, 'processing', ?, ?, ?, ?, ?)",
        (
            event_id,
            payload_hash,
            event_type,
            received_at,
            outcome,
            input_id,
            task_id,
        ),
    )


def insert_rejected_event(
    connection: sqlite3.Connection,
    event_id: str,
    payload_hash: str,
    event_type: str,
    received_at: str,
    input_id: str,
    task_id: str,
    code: str,
    semantic_hash: str | None = None,
) -> None:
    if _has_semantic_hash(connection):
        connection.execute(
            "INSERT INTO inbound_events (id, payload_hash, semantic_hash, "
            "status, event_type, received_at, outcome, input_id, task_id, "
            "failure_code, failure_message) VALUES (?, ?, ?, "
            "'rejected', ?, ?, 'rejected', ?, ?, ?, ?)",
            (
                event_id,
                payload_hash,
                semantic_hash,
                event_type,
                received_at,
                input_id,
                task_id,
                code,
                code,
            ),
        )
        return
    connection.execute(
        "INSERT INTO inbound_events (id, payload_hash, status, "
        "event_type, received_at, outcome, input_id, task_id, "
        "failure_code, failure_message) VALUES (?, ?, "
        "'rejected', ?, ?, 'rejected', ?, ?, ?, ?)",
        (
            event_id,
            payload_hash,
            event_type,
            received_at,
            input_id,
            task_id,
            code,
            code,
        ),
    )


def insert_abort_event(
    connection: sqlite3.Connection,
    event_id: str,
    payload_hash: str,
    received_at: str,
    input_id: str,
    task_id: str,
    code: str,
    semantic_hash: str | None = None,
) -> None:
    if _has_semantic_hash(connection):
        connection.execute(
            "INSERT INTO inbound_events (id, payload_hash, semantic_hash, "
            "status, event_type, received_at, outcome, input_id, task_id, "
            "failure_code, failure_message) VALUES (?, ?, ?, "
            "'rejected', 'task_finalization_aborted', ?, 'rejected', "
            "?, ?, ?, ?)",
            (
                event_id,
                payload_hash,
                semantic_hash,
                received_at,
                input_id,
                task_id,
                code,
                code,
            ),
        )
        return
    connection.execute(
        "INSERT INTO inbound_events (id, payload_hash, status, "
        "event_type, received_at, outcome, input_id, task_id, "
        "failure_code, failure_message) VALUES (?, ?, "
        "'rejected', 'task_finalization_aborted', ?, 'rejected', "
        "?, ?, ?, ?)",
        (
            event_id,
            payload_hash,
            received_at,
            input_id,
            task_id,
            code,
            code,
        ),
    )


def get_frozen_task_state(
    connection: sqlite3.Connection, task_id: str
) -> FrozenTaskState | None:
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT status, snapshot_frozen_at FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None

    return FrozenTaskState(
        status=row["status"],
        snapshot_frozen_at=row["snapshot_frozen_at"],
    )


@dataclass(frozen=True)
class MaterializationIdentity:
    capture_generation: int | None
    snapshot_frozen_at: str | None
    evidence_completeness: str | None
    final_head: str | None
    final_branch: str | None
    final_status: bytes | None
    final_index_manifest: bytes | None
    baseline_digest: str
    changes_digest: str


def _digest_text(value: object) -> bytes:
    if value is None:
        return b"\x00none\x00"
    return b"\x00" + str(value).encode("utf-8") + b"\x00"


def _digest_blob(value: bytes | None) -> bytes:
    if value is None:
        return b"\x00none\x00"
    return (
        b"\x00blob:"
        + str(len(value)).encode("ascii")
        + b":"
        + bytes(value)
        + b"\x00"
    )


def baseline_digest(rows: list[BaselineFileRow]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(row.path.encode("utf-8"))
        digest.update(_digest_text(row.status))
        digest.update(_digest_text(row.sha256))
        digest.update(_digest_text(row.size))
        digest.update(_digest_text(row.is_binary))
        digest.update(_digest_blob(row.content))
        digest.update(_digest_text(row.mode))
        digest.update(_digest_text(row.gitlink_oid))
    return digest.hexdigest()


def changes_digest(rows: list[TaskFileChangeRow]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(row.path.encode("utf-8"))
        digest.update(_digest_text(row.operation))
        digest.update(_digest_text(row.final_status))
        digest.update(_digest_text(row.final_sha256))
        digest.update(_digest_text(row.final_size))
        digest.update(_digest_text(row.final_is_binary))
        digest.update(_digest_blob(row.final_content))
        digest.update(_digest_text(row.evidence_status))
        digest.update(_digest_text(row.evidence_reason))
        digest.update(_digest_text(row.baseline_mode))
        digest.update(_digest_text(row.baseline_gitlink_oid))
        digest.update(_digest_text(row.final_mode))
        digest.update(_digest_text(row.final_gitlink_oid))
    return digest.hexdigest()


@dataclass(frozen=True)
class MaterializationSnapshot:
    status: str
    snapshot_frozen_at: str | None
    identity: MaterializationIdentity
    baseline: list[BaselineFileRow]
    changes: list[TaskFileChangeRow]


def get_finalization_task_status(
    connection: sqlite3.Connection, task_id: str
) -> str | None:
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return str(row["status"]) if row is not None else None


def get_materialization_snapshot(
    connection: sqlite3.Connection, task_id: str
) -> MaterializationSnapshot | None:
    from crucible_core.repositories import tasks_repository as _tasks

    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT status, snapshot_frozen_at, capture_generation, "
        "evidence_completeness, final_head, final_branch, "
        "final_status, final_index_manifest "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    baseline = _tasks.list_task_baseline_files(connection, task_id)
    changes = _tasks.list_task_file_changes(connection, task_id)
    final_status = row["final_status"]
    if final_status is not None:
        final_status = bytes(final_status)
    final_index = row["final_index_manifest"]
    if final_index is not None:
        final_index = bytes(final_index)
    return MaterializationSnapshot(
        status=str(row["status"]),
        snapshot_frozen_at=row["snapshot_frozen_at"],
        identity=MaterializationIdentity(
            capture_generation=(
                int(row["capture_generation"])
                if row["capture_generation"] is not None
                else None
            ),
            snapshot_frozen_at=row["snapshot_frozen_at"],
            evidence_completeness=row["evidence_completeness"],
            final_head=row["final_head"],
            final_branch=row["final_branch"],
            final_status=final_status,
            final_index_manifest=final_index,
            baseline_digest=baseline_digest(baseline),
            changes_digest=changes_digest(changes),
        ),
        baseline=baseline,
        changes=changes,
    )


def publish_materialization_locked(
    connection: sqlite3.Connection,
    task_id: str,
    expected: MaterializationIdentity,
    expected_changes: dict[str, tuple[object, object, object]],
    computed: dict[str, str | None],
    task_diff: str,
    now: str,
) -> bool:
    """Revalidate identity and publish atomically.

    Returns True when the task is (now) completed, False when a
    concurrent state/generation/evidence change blocks
    publication. Completed stays idempotent success.
    """
    from crucible_core.repositories import tasks_repository as _tasks

    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT status, snapshot_frozen_at, capture_generation, "
        "evidence_completeness, final_head, final_branch, "
        "final_status, final_index_manifest "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return False
    status = str(row["status"])
    if status == "completed":
        return True
    if status != "finalizing" or row["snapshot_frozen_at"] is None:
        return False
    current_generation = (
        int(row["capture_generation"])
        if row["capture_generation"] is not None
        else None
    )
    current_status = row["final_status"]
    if current_status is not None:
        current_status = bytes(current_status)
    current_index = row["final_index_manifest"]
    if current_index is not None:
        current_index = bytes(current_index)
    if (
        current_generation != expected.capture_generation
        or row["snapshot_frozen_at"] != expected.snapshot_frozen_at
        or row["evidence_completeness"] != expected.evidence_completeness
        or row["final_head"] != expected.final_head
        or row["final_branch"] != expected.final_branch
        or current_status != expected.final_status
        or current_index != expected.final_index_manifest
    ):
        return False
    fresh_baseline = _tasks.list_task_baseline_files(connection, task_id)
    fresh = _tasks.list_task_file_changes(connection, task_id)
    if baseline_digest(fresh_baseline) != expected.baseline_digest:
        return False
    if changes_digest(fresh) != expected.changes_digest:
        return False
    fresh_map = {
        item.path: (
            item.evidence_status,
            item.final_sha256,
            item.final_size,
        )
        for item in fresh
    }
    if fresh_map != expected_changes:
        return False
    for path, patch in computed.items():
        connection.execute(
            "UPDATE task_file_changes SET patch = ?, "
            "materialized_at = ? WHERE task_id = ? AND path = ?",
            (patch, now, task_id, path),
        )
    if expected.capture_generation is None:
        changed = connection.execute(
            "UPDATE tasks SET status = 'completed', "
            "task_diff = ?, completed_at = ? WHERE id = ? "
            "AND status = 'finalizing' "
            "AND snapshot_frozen_at = ? "
            "AND capture_generation IS NULL",
            (
                task_diff,
                now,
                task_id,
                expected.snapshot_frozen_at,
            ),
        ).rowcount
    else:
        changed = connection.execute(
            "UPDATE tasks SET status = 'completed', "
            "task_diff = ?, completed_at = ? WHERE id = ? "
            "AND status = 'finalizing' "
            "AND snapshot_frozen_at = ? "
            "AND capture_generation = ?",
            (
                task_diff,
                now,
                task_id,
                expected.snapshot_frozen_at,
                expected.capture_generation,
            ),
        ).rowcount
    if changed != 1:
        # Never leave partial patch writes visible: the
        # caller holds BEGIN IMMEDIATE, so roll back the
        # whole publication instead of committing patches
        # without the completed task row.
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        return False
    connection.execute(
        "UPDATE inbound_events SET status = 'accepted', "
        "outcome = 'completed' WHERE task_id = ? "
        "AND event_type = 'task_completed' AND status = 'processing'",
        (task_id,),
    )
    return True


def begin_finalization(
    connection: sqlite3.Connection,
    task_id: str,
    tree_id: str,
    execution_id: str,
    terminal_signal: str,
    terminal_outcome: str,
    compatibility_profile: str,
    terminal_observed_at: str,
    capture_not_after: str,
) -> int | None:
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "UPDATE working_trees SET capture_generation = capture_generation + 1 "
        "WHERE id = ? RETURNING capture_generation",
        (tree_id,),
    ).fetchone()
    if row is None:
        return None
    generation = GenerationRef(
        capture_generation=int(row["capture_generation"])
    )
    changed = connection.execute(
        "UPDATE tasks SET status = 'finalizing', execution_id = ?, "
        "terminal_signal = ?, terminal_outcome = ?, "
        "compatibility_profile = ?, terminal_observed_at = ?, "
        "capture_not_after = ?, capture_generation = ? "
        "WHERE id = ? AND status = 'running'",
        (
            execution_id,
            terminal_signal,
            terminal_outcome,
            compatibility_profile,
            terminal_observed_at,
            capture_not_after,
            generation.capture_generation,
            task_id,
        ),
    ).rowcount
    if changed == 1:
        return generation.capture_generation
    return None


def abort_running_task(
    connection: sqlite3.Connection,
    task_id: str,
    code: str,
    failed_at: str,
    terminal_observed_at: str,
    capture_not_after: str,
) -> bool:
    return (
        connection.execute(
            "UPDATE tasks SET status = 'failed', failure_code = ?, "
            "failure_message = ?, failed_at = ?, terminal_observed_at = ?, "
            "capture_not_after = ? WHERE id = ? AND status = 'running'",
            (
                code,
                code,
                failed_at,
                terminal_observed_at,
                capture_not_after,
                task_id,
            ),
        ).rowcount
        == 1
    )


def abort_running_task_with_reason(
    connection: sqlite3.Connection,
    task_id: str,
    code: str,
    failed_at: str,
) -> bool:
    return (
        connection.execute(
            "UPDATE tasks SET status = 'failed', failure_code = ?, "
            "failure_message = ?, failed_at = ? "
            "WHERE id = ? AND status = 'running'",
            (code, code, failed_at, task_id),
        ).rowcount
        == 1
    )


def publication_is_current(
    connection: sqlite3.Connection,
    task_id: str,
    tree_id: str,
    generation: int,
) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM tasks JOIN working_trees ON "
            "working_trees.id = tasks.working_tree_id "
            "WHERE tasks.id = ? AND tasks.working_tree_id = ? "
            "AND tasks.status = 'finalizing' "
            "AND tasks.snapshot_frozen_at IS NULL "
            "AND tasks.capture_generation = ? "
            "AND working_trees.capture_generation = ?",
            (task_id, tree_id, generation, generation),
        ).fetchone()
        is not None
    )


def insert_baseline_file(
    connection: sqlite3.Connection,
    task_id: str,
    row: BaselineFileRow,
) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO task_baseline_files "
        "(id, task_id, path, status, sha256, size, is_binary, content, "
        "mode, gitlink_oid) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(uuid.uuid4()),
            task_id,
            row.path,
            row.status,
            row.sha256,
            row.size,
            row.is_binary,
            row.content,
            row.mode,
            row.gitlink_oid,
        ),
    )


def insert_file_change(
    connection: sqlite3.Connection,
    task_id: str,
    row: TaskFileChangeRow,
) -> None:
    connection.execute(
        "INSERT INTO task_file_changes "
        "(id, task_id, path, operation, final_status, final_sha256, "
        "final_size, final_is_binary, final_content, evidence_status, "
        "evidence_reason, baseline_mode, baseline_gitlink_oid, "
        "final_mode, final_gitlink_oid) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(uuid.uuid4()),
            task_id,
            row.path,
            row.operation,
            row.final_status,
            row.final_sha256,
            row.final_size,
            row.final_is_binary,
            row.final_content,
            row.evidence_status,
            row.evidence_reason,
            row.baseline_mode,
            row.baseline_gitlink_oid,
            row.final_mode,
            row.final_gitlink_oid,
        ),
    )


def list_recovery_tasks(
    connection: sqlite3.Connection,
) -> list[RecoveryTaskRef]:
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT id, snapshot_frozen_at FROM tasks WHERE status = 'finalizing'"
    ).fetchall()
    return [
        RecoveryTaskRef(
            task_id=row["id"],
            snapshot_frozen_at=row["snapshot_frozen_at"],
        )
        for row in rows
    ]


def fail_task(
    connection: sqlite3.Connection,
    task_id: str,
    code: str,
    failed_at: str,
    generation: int | None = None,
) -> bool:
    clause = ""
    values: list[object] = [code, code, failed_at, task_id]
    if generation is not None:
        clause = " AND capture_generation = ?"
        values.append(generation)
    return (
        connection.execute(
            "UPDATE tasks SET status = 'failed', failure_code = ?, "
            "failure_message = ?, failed_at = ? WHERE id = ? "
            "AND status = 'finalizing'" + clause,
            values,
        ).rowcount
        == 1
    )


def fence_finalization(
    connection: sqlite3.Connection, tree_id: str, failed_at: str
) -> int:
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "UPDATE working_trees SET capture_generation = capture_generation + 1 "
        "WHERE id = ? RETURNING capture_generation",
        (tree_id,),
    ).fetchone()
    if row is None:
        raise ValueError("WORKING_TREE_NOT_FOUND")
    generation = GenerationRef(
        capture_generation=int(row["capture_generation"])
    )
    connection.execute(
        "UPDATE tasks SET status = 'failed', "
        "failure_code = 'FINAL_CAPTURE_FENCED_BY_NEXT_INPUT', "
        "failure_message = 'FINAL_CAPTURE_FENCED_BY_NEXT_INPUT', "
        "failed_at = ? WHERE working_tree_id = ? AND status = 'finalizing' "
        "AND snapshot_frozen_at IS NULL",
        (failed_at, tree_id),
    )
    connection.execute(
        "UPDATE inbound_events SET status = 'rejected', "
        "outcome = 'rejected', "
        "failure_code = 'FINAL_CAPTURE_FENCED_BY_NEXT_INPUT', "
        "failure_message = 'FINAL_CAPTURE_FENCED_BY_NEXT_INPUT' "
        "WHERE event_type = 'task_completed' AND status = 'processing' "
        "AND task_id IN (SELECT id FROM tasks WHERE working_tree_id = ? "
        "AND status = 'failed' "
        "AND failure_code = 'FINAL_CAPTURE_FENCED_BY_NEXT_INPUT')",
        (tree_id,),
    )
    return generation.capture_generation
