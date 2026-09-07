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
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT session_id, working_tree_id, status, execution_id "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    return TaskOwnerRef(
        session_id=str(row["session_id"]),
        tree_id=str(row["working_tree_id"]),
        status=str(row["status"]),
        execution_id=(
            str(row["execution_id"])
            if row["execution_id"] is not None
            else None
        ),
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
        "started_at, execution_id, baseline_head, baseline_status, "
        "baseline_branch, "
        "baseline_index_manifest) VALUES "
        "(?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)",
        (
            task.task_id,
            task.session_id,
            task.tree_id,
            task.started_at,
            task.execution_id,
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
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT tasks.id AS id, tasks.status AS status, "
        "tasks.started_at AS started_at, "
        "working_trees.git_root AS git_root, "
        "projects.id AS project_id, "
        "tasks.baseline_branch AS baseline_branch, "
        "tasks.baseline_head AS baseline_head, "
        "tasks.baseline_status AS baseline_status, "
        "tasks.baseline_index_manifest AS baseline_index_manifest, "
        "tasks.failure_code AS failure_code, "
        "tasks.failure_message AS failure_message, "
        "tasks.final_head AS final_head, "
        "tasks.final_branch AS final_branch, "
        "tasks.final_status AS final_status, "
        "tasks.final_index_manifest AS final_index_manifest, "
        "tasks.snapshot_frozen_at AS snapshot_frozen_at, "
        "tasks.task_diff AS task_diff, "
        "tasks.evidence_completeness AS evidence_completeness, "
        "tasks.execution_id AS execution_id, "
        "tasks.terminal_signal AS terminal_signal, "
        "tasks.terminal_outcome AS terminal_outcome, "
        "tasks.compatibility_profile AS compatibility_profile, "
        "tasks.terminal_observed_at AS terminal_observed_at, "
        "tasks.capture_not_after AS capture_not_after "
        "FROM tasks JOIN working_trees ON "
        "working_trees.id = tasks.working_tree_id JOIN projects ON "
        "projects.id = "
        "working_trees.project_id WHERE tasks.id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None

    return TaskDetailRow(
        id=row["id"],
        status=row["status"],
        started_at=row["started_at"],
        worktree=row["git_root"],
        project_id=row["project_id"],
        branch=row["baseline_branch"],
        baseline_head=row["baseline_head"],
        baseline_status=row["baseline_status"],
        baseline_index_manifest=row["baseline_index_manifest"],
        failure_code=row["failure_code"],
        failure_message=row["failure_message"],
        final_head=row["final_head"],
        final_branch=row["final_branch"],
        final_status=row["final_status"],
        final_index_manifest=row["final_index_manifest"],
        snapshot_frozen_at=row["snapshot_frozen_at"],
        task_diff=row["task_diff"],
        evidence_completeness=row["evidence_completeness"],
        execution_id=row["execution_id"],
        terminal_signal=row["terminal_signal"],
        terminal_outcome=row["terminal_outcome"],
        compatibility_profile=row["compatibility_profile"],
        terminal_observed_at=row["terminal_observed_at"],
        capture_not_after=row["capture_not_after"],
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
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT tasks.id AS id, tasks.session_id AS session_id, "
        "tasks.working_tree_id AS tree_id, "
        "tasks.status AS status, working_trees.git_root AS git_root, "
        "projects.id AS project_id, "
        "sessions.adapter AS adapter, "
        "sessions.adapter_version AS adapter_version, "
        "sessions.agent_session_id AS agent_session_id, "
        "sessions.workspace_path AS workspace_path, "
        "tasks.baseline_head AS baseline_head, "
        "tasks.baseline_branch AS baseline_branch, "
        "tasks.baseline_index_manifest AS baseline_index_manifest, "
        "working_trees.capture_generation AS capture_generation, "
        "tasks.execution_id AS execution_id, "
        "tasks.terminal_observed_at AS terminal_observed_at, "
        "tasks.capture_not_after AS capture_not_after "
        "FROM tasks JOIN sessions ON sessions.id = tasks.session_id "
        "JOIN working_trees ON working_trees.id = tasks.working_tree_id "
        "JOIN projects ON projects.id = working_trees.project_id "
        "WHERE tasks.id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    return FinalizationTask(
        id=row["id"],
        session_id=row["session_id"],
        tree_id=row["tree_id"],
        status=row["status"],
        git_root=row["git_root"],
        project_id=row["project_id"],
        adapter=row["adapter"],
        adapter_version=row["adapter_version"],
        agent_session_id=row["agent_session_id"],
        workspace_path=row["workspace_path"],
        baseline_head=row["baseline_head"],
        baseline_branch=row["baseline_branch"],
        baseline_index_manifest=row["baseline_index_manifest"],
        capture_generation=row["capture_generation"],
        execution_id=row["execution_id"],
        terminal_observed_at=row["terminal_observed_at"],
        capture_not_after=row["capture_not_after"],
    )


def list_task_file_changes(
    connection: sqlite3.Connection, task_id: str
) -> list[TaskFileChangeRow]:
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT path, operation, final_status, final_sha256, final_size, "
        "final_is_binary, final_content, evidence_status, "
        "evidence_reason, patch FROM task_file_changes "
        "WHERE task_id = ? ORDER BY path",
        (task_id,),
    ).fetchall()
    return [
        TaskFileChangeRow(
            path=row["path"],
            operation=row["operation"],
            final_status=row["final_status"],
            final_sha256=row["final_sha256"],
            final_size=row["final_size"],
            final_is_binary=row["final_is_binary"],
            final_content=row["final_content"],
            evidence_status=row["evidence_status"],
            evidence_reason=row["evidence_reason"],
            patch=row["patch"],
        )
        for row in rows
    ]
