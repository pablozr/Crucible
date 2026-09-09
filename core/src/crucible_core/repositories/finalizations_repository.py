from __future__ import annotations

import sqlite3
import uuid

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


def get_finalization_event(
    connection: sqlite3.Connection, event_id: str
) -> FinalizationEventRow | None:
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT payload_hash, status, outcome, input_id, task_id, "
        "failure_code FROM inbound_events WHERE id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        return None

    return FinalizationEventRow(
        payload_hash=row["payload_hash"],
        status=row["status"],
        outcome=row["outcome"],
        input_id=row["input_id"],
        task_id=row["task_id"],
        failure_code=row["failure_code"],
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
