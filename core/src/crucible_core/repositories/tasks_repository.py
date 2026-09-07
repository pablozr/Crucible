from __future__ import annotations

import sqlite3

from crucible_core.schemas.persistence import (
    ActiveTaskRef,
    BaselineFileRow,
    FinalizationTask,
    InputRef,
    NewStoredInput,
    NewTask,
    StoredInput,
    TaskDetailRow,
    TaskFileChangeRow,
    TaskInputLink,
    TaskOwnerRef,
    TaskPageRow,
)


def find_active_task_by_session(
    connection: sqlite3.Connection, session_id: str
) -> str | None:
    row = connection.execute(
        "SELECT id FROM tasks WHERE session_id = ? "
        "AND status IN ('running', 'finalizing') "
        "ORDER BY started_at DESC, id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return row[0] if row else None


def find_running_task_by_session(
    connection: sqlite3.Connection, session_id: str
) -> str | None:
    row = connection.execute(
        "SELECT id FROM tasks WHERE session_id = ? "
        "AND status = 'running' "
        "ORDER BY started_at DESC, id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return row[0] if row else None


def get_task_status(
    connection: sqlite3.Connection, task_id: str
) -> str | None:
    row = connection.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return row[0] if row else None


def get_task_owner(
    connection: sqlite3.Connection, task_id: str
) -> TaskOwnerRef | None:
    row = connection.execute(
        "SELECT session_id, working_tree_id, status FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    session_id, tree_id, status = row
    return TaskOwnerRef(
        session_id=str(session_id),
        tree_id=str(tree_id),
        status=str(status),
    )


def find_active_task_by_tree(
    connection: sqlite3.Connection, tree_id: str
) -> ActiveTaskRef | None:
    row = connection.execute(
        "SELECT id, session_id FROM tasks WHERE working_tree_id = ? "
        "AND status IN ('running', 'finalizing') LIMIT 1",
        (tree_id,),
    ).fetchone()
    if row is None:
        return None
    task_id, task_session_id = row
    return ActiveTaskRef(id=task_id, session_id=task_session_id)


def find_input(
    connection: sqlite3.Connection, session_id: str, input_id: str
) -> StoredInput | None:
    row = connection.execute(
        "SELECT id, task_id, admission_hash FROM inputs "
        "WHERE session_id = ? AND input_id = ?",
        (session_id, input_id),
    ).fetchone()
    if row is None:
        return None

    row_id, task_id, admission_hash = row

    return StoredInput(
        id=row_id, task_id=task_id, admission_hash=admission_hash
    )


def find_input_by_adapter_session(
    connection: sqlite3.Connection,
    adapter: str,
    agent_session_id: str,
    input_id: str,
) -> StoredInput | None:
    row = connection.execute(
        "SELECT inputs.id, inputs.task_id, inputs.admission_hash "
        "FROM inputs "
        "JOIN sessions ON sessions.id = inputs.session_id "
        "WHERE sessions.adapter = ? AND sessions.agent_session_id = ? "
        "AND inputs.input_id = ?",
        (adapter, agent_session_id, input_id),
    ).fetchone()
    if row is None:
        return None

    row_id, task_id, admission_hash = row

    return StoredInput(
        id=row_id, task_id=task_id, admission_hash=admission_hash
    )


def insert_input(
    connection: sqlite3.Connection, record: NewStoredInput
) -> None:
    connection.execute(
        "INSERT INTO inputs (id, session_id, task_id, input_id, "
        "outcome, "
        "admission_hash) VALUES (?, ?, ?, ?, 'admitted', ?)",
        (
            record.row_id,
            record.session_id,
            record.task_id,
            record.input_id,
            record.admission_hash,
        ),
    )


def insert_task(connection: sqlite3.Connection, task: NewTask) -> None:
    connection.execute(
        "INSERT INTO tasks (id, session_id, working_tree_id, "
        "status, "
        "started_at, baseline_head, baseline_status, baseline_branch, "
        "baseline_index_manifest) VALUES "
        "(?, ?, ?, 'running', ?, ?, ?, ?, ?)",
        (
            task.task_id,
            task.session_id,
            task.tree_id,
            task.started_at,
            task.baseline_head,
            task.baseline_status,
            task.baseline_branch,
            task.baseline_index_manifest,
        ),
    )


def list_tasks_page(
    connection: sqlite3.Connection,
    started_at: str | None,
    task_id: str | None,
    limit: int,
) -> list[TaskPageRow]:
    values: list[object] = []
    clause = ""
    if started_at is not None and task_id is not None:
        clause = "WHERE (tasks.started_at, tasks.id) < (?, ?)"
        values.extend([started_at, task_id])
    values.append(limit + 1)
    rows = connection.execute(
        "SELECT tasks.id, tasks.status, tasks.started_at, "
        "working_trees.git_root, "
        "projects.id, tasks.baseline_branch, tasks.failure_code, "
        "tasks.failure_message FROM tasks JOIN working_trees ON "
        "working_trees.id = tasks.working_tree_id JOIN projects ON "
        "projects.id = "
        f"working_trees.project_id {clause} "
        "ORDER BY tasks.started_at DESC, "
        "tasks.id DESC LIMIT ?",
        values,
    ).fetchall()

    return [
        TaskPageRow(
            id=row_id,
            status=status,
            started_at=row_started_at,
            worktree=worktree,
            project_id=project_id,
            branch=branch,
            failure_code=failure_code,
            failure_message=failure_message,
        )
        for (
            row_id,
            status,
            row_started_at,
            worktree,
            project_id,
            branch,
            failure_code,
            failure_message,
        ) in rows
    ]


def list_inputs_by_task_ids(
    connection: sqlite3.Connection, task_ids: list[str]
) -> list[TaskInputLink]:
    if not task_ids:
        return []
    rows = connection.execute(
        "SELECT task_id, input_id FROM inputs WHERE task_id IN ("
        + ",".join("?" for _ in task_ids)
        + ") ORDER BY id",
        task_ids,
    ).fetchall()

    return [
        TaskInputLink(task_id=task_id, input_id=input_id)
        for task_id, input_id in rows
    ]


def get_task_row(
    connection: sqlite3.Connection, task_id: str
) -> TaskDetailRow | None:
    row = connection.execute(
        "SELECT tasks.id, tasks.status, tasks.started_at, "
        "working_trees.git_root, "
        "projects.id, tasks.baseline_branch, tasks.baseline_head, "
        "tasks.baseline_status, tasks.baseline_index_manifest, "
        "tasks.failure_code, "
        "tasks.failure_message, tasks.final_head, tasks.final_branch, "
        "tasks.final_status, tasks.final_index_manifest, "
        "tasks.snapshot_frozen_at, tasks.task_diff, "
        "tasks.evidence_completeness, tasks.execution_id, "
        "tasks.terminal_signal, tasks.terminal_outcome, "
        "tasks.compatibility_profile FROM tasks JOIN working_trees ON "
        "working_trees.id = tasks.working_tree_id JOIN projects ON "
        "projects.id = "
        "working_trees.project_id WHERE tasks.id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None

    (
        row_id,
        status,
        started_at,
        worktree,
        project_id,
        branch,
        head,
        baseline_status,
        index_manifest,
        failure_code,
        failure_message,
        final_head,
        final_branch,
        final_status,
        final_index_manifest,
        snapshot_frozen_at,
        task_diff,
        evidence_completeness,
        execution_id,
        terminal_signal,
        terminal_outcome,
        compatibility_profile,
    ) = row

    return TaskDetailRow(
        id=row_id,
        status=status,
        started_at=started_at,
        worktree=worktree,
        project_id=project_id,
        branch=branch,
        baseline_head=head,
        baseline_status=baseline_status,
        baseline_index_manifest=index_manifest,
        failure_code=failure_code,
        failure_message=failure_message,
        final_head=final_head,
        final_branch=final_branch,
        final_status=final_status,
        final_index_manifest=final_index_manifest,
        snapshot_frozen_at=snapshot_frozen_at,
        task_diff=task_diff,
        evidence_completeness=evidence_completeness,
        execution_id=execution_id,
        terminal_signal=terminal_signal,
        terminal_outcome=terminal_outcome,
        compatibility_profile=compatibility_profile,
    )


def list_input_ids_by_task(
    connection: sqlite3.Connection, task_id: str
) -> list[InputRef]:
    rows = connection.execute(
        "SELECT input_id FROM inputs WHERE task_id = ? ORDER BY id",
        (task_id,),
    ).fetchall()

    return [InputRef(input_id=input_id) for (input_id,) in rows]


def insert_task_baseline_file(
    connection: sqlite3.Connection,
    file_id: str,
    task_id: str,
    baseline_file: BaselineFileRow,
) -> None:
    connection.execute(
        "INSERT INTO task_baseline_files "
        "(id, task_id, path, status, sha256, size, is_binary, "
        "content) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            file_id,
            task_id,
            baseline_file.path,
            baseline_file.status,
            baseline_file.sha256,
            baseline_file.size,
            baseline_file.is_binary,
            baseline_file.content,
        ),
    )


def list_task_baseline_files(
    connection: sqlite3.Connection, task_id: str
) -> list[BaselineFileRow]:
    rows = connection.execute(
        "SELECT path, status, sha256, size, is_binary, content "
        "FROM task_baseline_files WHERE task_id = ? ORDER BY path",
        (task_id,),
    ).fetchall()

    return [
        BaselineFileRow(
            path=path,
            status=status,
            sha256=sha256,
            size=size,
            is_binary=is_binary,
            content=content,
        )
        for path, status, sha256, size, is_binary, content in rows
    ]


def get_finalization_task(
    connection: sqlite3.Connection, task_id: str
) -> FinalizationTask | None:
    row = connection.execute(
        "SELECT tasks.id, tasks.session_id, tasks.working_tree_id, "
        "tasks.status, working_trees.git_root, projects.id, "
        "sessions.adapter, sessions.adapter_version, "
        "sessions.agent_session_id, sessions.workspace_path, "
        "tasks.baseline_head, tasks.baseline_branch, "
        "tasks.baseline_index_manifest, working_trees.capture_generation "
        "FROM tasks JOIN sessions ON sessions.id = tasks.session_id "
        "JOIN working_trees ON working_trees.id = tasks.working_tree_id "
        "JOIN projects ON projects.id = working_trees.project_id "
        "WHERE tasks.id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    return FinalizationTask(
        id=row[0],
        session_id=row[1],
        tree_id=row[2],
        status=row[3],
        git_root=row[4],
        project_id=row[5],
        adapter=row[6],
        adapter_version=row[7],
        agent_session_id=row[8],
        workspace_path=row[9],
        baseline_head=row[10],
        baseline_branch=row[11],
        baseline_index_manifest=row[12],
        capture_generation=row[13],
    )


def list_task_file_changes(
    connection: sqlite3.Connection, task_id: str
) -> list[TaskFileChangeRow]:
    rows = connection.execute(
        "SELECT path, operation, final_status, final_sha256, final_size, "
        "final_is_binary, final_content, evidence_status, "
        "evidence_reason, patch FROM task_file_changes "
        "WHERE task_id = ? ORDER BY path",
        (task_id,),
    ).fetchall()
    return [
        TaskFileChangeRow(
            path=row[0],
            operation=row[1],
            final_status=row[2],
            final_sha256=row[3],
            final_size=row[4],
            final_is_binary=row[5],
            final_content=row[6],
            evidence_status=row[7],
            evidence_reason=row[8],
            patch=row[9],
        )
        for row in rows
    ]
