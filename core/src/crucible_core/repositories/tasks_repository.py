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
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT id AS id FROM tasks WHERE session_id = ? "
        "AND status IN ('running', 'finalizing') "
        "ORDER BY started_at DESC, id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return str(row["id"]) if row else None


def find_running_task_by_session(
    connection: sqlite3.Connection, session_id: str
) -> str | None:
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT id AS id FROM tasks WHERE session_id = ? "
        "AND status = 'running' "
        "ORDER BY started_at DESC, id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return str(row["id"]) if row else None


def get_task_status(
    connection: sqlite3.Connection, task_id: str
) -> str | None:
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT status AS status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return str(row["status"]) if row else None


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
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT id AS id, session_id AS session_id FROM tasks "
        "WHERE working_tree_id = ? "
        "AND status IN ('running', 'finalizing') LIMIT 1",
        (tree_id,),
    ).fetchone()
    if row is None:
        return None
    return ActiveTaskRef(id=str(row["id"]), session_id=str(row["session_id"]))


def find_input(
    connection: sqlite3.Connection, session_id: str, input_id: str
) -> StoredInput | None:
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT id AS id, task_id AS task_id, "
        "admission_hash AS admission_hash FROM inputs "
        "WHERE session_id = ? AND input_id = ?",
        (session_id, input_id),
    ).fetchone()
    if row is None:
        return None

    return StoredInput(
        id=row["id"],
        task_id=row["task_id"],
        admission_hash=row["admission_hash"],
    )


def find_input_by_adapter_session(
    connection: sqlite3.Connection,
    adapter: str,
    agent_session_id: str,
    input_id: str,
) -> StoredInput | None:
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT inputs.id AS id, inputs.task_id AS task_id, "
        "inputs.admission_hash AS admission_hash "
        "FROM inputs "
        "JOIN sessions ON sessions.id = inputs.session_id "
        "WHERE sessions.adapter = ? AND sessions.agent_session_id = ? "
        "AND inputs.input_id = ?",
        (adapter, agent_session_id, input_id),
    ).fetchone()
    if row is None:
        return None

    return StoredInput(
        id=row["id"],
        task_id=row["task_id"],
        admission_hash=row["admission_hash"],
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
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT tasks.id AS id, tasks.status AS status, "
        "tasks.started_at AS started_at, "
        "working_trees.git_root AS worktree, "
        "projects.id AS project_id, tasks.baseline_branch AS branch, "
        "tasks.failure_code AS failure_code, "
        "tasks.failure_message AS failure_message FROM tasks "
        "JOIN working_trees ON "
        "working_trees.id = tasks.working_tree_id JOIN projects ON "
        "projects.id = "
        f"working_trees.project_id {clause} "
        "ORDER BY tasks.started_at DESC, "
        "tasks.id DESC LIMIT ?",
        values,
    ).fetchall()

    return [
        TaskPageRow(
            id=row["id"],
            status=row["status"],
            started_at=row["started_at"],
            worktree=row["worktree"],
            project_id=row["project_id"],
            branch=row["branch"],
            failure_code=row["failure_code"],
            failure_message=row["failure_message"],
        )
        for row in rows
    ]


def list_inputs_by_task_ids(
    connection: sqlite3.Connection, task_ids: list[str]
) -> list[TaskInputLink]:
    if not task_ids:
        return []
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT task_id AS task_id, input_id AS input_id FROM inputs "
        "WHERE task_id IN ("
        + ",".join("?" for _ in task_ids)
        + ") ORDER BY id",
        task_ids,
    ).fetchall()

    return [
        TaskInputLink(task_id=row["task_id"], input_id=row["input_id"])
        for row in rows
    ]


def get_task_row(
    connection: sqlite3.Connection,
    task_id: str,
    include_diff: bool = True,
) -> TaskDetailRow | None:
    connection.row_factory = sqlite3.Row
    diff_select = (
        "tasks.task_diff AS task_diff, "
        if include_diff
        else "NULL AS task_diff, "
    )
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
        f"{diff_select}"
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
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT input_id AS input_id FROM inputs "
        "WHERE task_id = ? ORDER BY id",
        (task_id,),
    ).fetchall()

    return [InputRef(input_id=row["input_id"]) for row in rows]


def insert_task_baseline_file(
    connection: sqlite3.Connection,
    file_id: str,
    task_id: str,
    baseline_file: BaselineFileRow,
) -> None:
    connection.execute(
        "INSERT INTO task_baseline_files "
        "(id, task_id, path, status, sha256, size, is_binary, "
        "content, mode, gitlink_oid) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            file_id,
            task_id,
            baseline_file.path,
            baseline_file.status,
            baseline_file.sha256,
            baseline_file.size,
            baseline_file.is_binary,
            baseline_file.content,
            baseline_file.mode,
            baseline_file.gitlink_oid,
        ),
    )


def _baseline_file_columns(connection: sqlite3.Connection) -> set[str]:
    return {
        item["name"]
        for item in connection.execute(
            "PRAGMA table_info(task_baseline_files)"
        ).fetchall()
    }


def list_task_baseline_files(
    connection: sqlite3.Connection, task_id: str
) -> list[BaselineFileRow]:
    connection.row_factory = sqlite3.Row
    columns = _baseline_file_columns(connection)
    mode_select = "mode AS mode," if "mode" in columns else "NULL AS mode,"
    oid_select = (
        "gitlink_oid AS gitlink_oid"
        if "gitlink_oid" in columns
        else "NULL AS gitlink_oid"
    )
    rows = connection.execute(
        "SELECT path AS path, status AS status, sha256 AS sha256, "
        "size AS size, is_binary AS is_binary, content AS content, "
        f"{mode_select} {oid_select} "
        "FROM task_baseline_files WHERE task_id = ? ORDER BY path",
        (task_id,),
    ).fetchall()

    return [
        BaselineFileRow(
            path=row["path"],
            status=row["status"],
            sha256=row["sha256"],
            size=row["size"],
            is_binary=row["is_binary"],
            content=row["content"],
            mode=row["mode"],
            gitlink_oid=row["gitlink_oid"],
        )
        for row in rows
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


def _file_change_columns(connection: sqlite3.Connection) -> set[str]:
    return {
        item["name"]
        for item in connection.execute(
            "PRAGMA table_info(task_file_changes)"
        ).fetchall()
    }


def list_task_file_changes(
    connection: sqlite3.Connection, task_id: str
) -> list[TaskFileChangeRow]:
    connection.row_factory = sqlite3.Row
    columns = _file_change_columns(connection)
    baseline_mode = (
        "baseline_mode AS baseline_mode,"
        if "baseline_mode" in columns
        else "NULL AS baseline_mode,"
    )
    baseline_oid = (
        "baseline_gitlink_oid AS baseline_gitlink_oid,"
        if "baseline_gitlink_oid" in columns
        else "NULL AS baseline_gitlink_oid,"
    )
    final_mode = (
        "final_mode AS final_mode,"
        if "final_mode" in columns
        else "NULL AS final_mode,"
    )
    final_oid = (
        "final_gitlink_oid AS final_gitlink_oid"
        if "final_gitlink_oid" in columns
        else "NULL AS final_gitlink_oid"
    )
    rows = connection.execute(
        "SELECT path, operation, final_status, final_sha256, final_size, "
        "final_is_binary, final_content, evidence_status, "
        f"evidence_reason, patch, {baseline_mode} {baseline_oid} "
        f"{final_mode} {final_oid} FROM task_file_changes "
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
            baseline_mode=row["baseline_mode"],
            baseline_gitlink_oid=row["baseline_gitlink_oid"],
            final_mode=row["final_mode"],
            final_gitlink_oid=row["final_gitlink_oid"],
        )
        for row in rows
    ]
