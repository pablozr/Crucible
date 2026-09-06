from __future__ import annotations

import sqlite3


def get_working_tree_id(
    connection: sqlite3.Connection, git_root: str
) -> str | None:
    row = connection.execute(
        "SELECT id FROM working_trees WHERE git_root = ?", (git_root,)
    ).fetchone()
    return row[0] if row else None


def upsert_project(
    connection: sqlite3.Connection, project_id: str, git_root: str
) -> None:
    connection.execute(
        "INSERT INTO projects (id, git_root) VALUES (?, ?) "
        "ON CONFLICT(id) DO UPDATE SET git_root = excluded.git_root",
        (project_id, git_root),
    )


def insert_working_tree(
    connection: sqlite3.Connection,
    tree_id: str,
    project_id: str,
    git_root: str,
) -> None:
    connection.execute(
        "INSERT INTO working_trees (id, project_id, git_root) "
        "VALUES (?, ?, ?)",
        (tree_id, project_id, git_root),
    )


def find_session_id(
    connection: sqlite3.Connection, adapter: str, agent_session_id: str
) -> str | None:
    row = connection.execute(
        "SELECT id FROM sessions WHERE adapter = ? AND agent_session_id = ?",
        (adapter, agent_session_id),
    ).fetchone()
    return row[0] if row else None


def insert_session(
    connection: sqlite3.Connection,
    session_id: str,
    tree_id: str,
    adapter: str,
    agent_session_id: str,
    adapter_version: str,
    workspace_path: str,
) -> None:
    connection.execute(
        "INSERT INTO sessions (id, working_tree_id, adapter, "
        "agent_session_id, adapter_version, workspace_path) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            session_id,
            tree_id,
            adapter,
            agent_session_id,
            adapter_version,
            workspace_path,
        ),
    )
