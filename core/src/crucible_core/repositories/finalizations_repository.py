from __future__ import annotations

import sqlite3
import uuid

from crucible_core.schemas.persistence import (
    BaselineFileRow,
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


def begin_finalization(
    connection: sqlite3.Connection,
    task_id: str,
    tree_id: str,
    execution_id: str,
    terminal_signal: str,
    terminal_outcome: str,
    compatibility_profile: str,
) -> int | None:
    generation = connection.execute(
        "UPDATE working_trees SET capture_generation = capture_generation + 1 "
        "WHERE id = ? RETURNING capture_generation",
        (tree_id,),
    ).fetchone()
    if generation is None:
        return None
    changed = connection.execute(
        "UPDATE tasks SET status = 'finalizing', execution_id = ?, "
        "terminal_signal = ?, terminal_outcome = ?, "
        "compatibility_profile = ?, capture_generation = ? "
        "WHERE id = ? AND status = 'running'",
        (
            execution_id,
            terminal_signal,
            terminal_outcome,
            compatibility_profile,
            generation[0],
            task_id,
        ),
    ).rowcount
    return int(generation[0]) if changed == 1 else None


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
        "(id, task_id, path, status, sha256, size, is_binary, content) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(uuid.uuid4()),
            task_id,
            row.path,
            row.status,
            row.sha256,
            row.size,
            row.is_binary,
            row.content,
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
        "evidence_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        ),
    )


def list_recovery_tasks(
    connection: sqlite3.Connection,
) -> list[tuple[str, str | None]]:
    return connection.execute(
        "SELECT id, snapshot_frozen_at FROM tasks WHERE status = 'finalizing'"
    ).fetchall()


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
    generation = connection.execute(
        "UPDATE working_trees SET capture_generation = capture_generation + 1 "
        "WHERE id = ? RETURNING capture_generation",
        (tree_id,),
    ).fetchone()
    if generation is None:
        raise ValueError("WORKING_TREE_NOT_FOUND")
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
    return int(generation[0])
