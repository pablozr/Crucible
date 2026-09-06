"""Initial Crucible SQLite schema.

Revision ID: 0001
Revises:
Create Date: 2026-09-06
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


UPGRADE_STATEMENTS = (
    "CREATE TABLE projects (id TEXT PRIMARY KEY NOT NULL, git_root TEXT NOT NULL UNIQUE)",
    "CREATE TABLE working_trees (id TEXT PRIMARY KEY NOT NULL, project_id TEXT NOT NULL REFERENCES projects(id), git_root TEXT NOT NULL UNIQUE)",
    "CREATE TABLE sessions (id TEXT PRIMARY KEY NOT NULL, working_tree_id TEXT NOT NULL REFERENCES working_trees(id), adapter TEXT NOT NULL, agent_session_id TEXT NOT NULL, UNIQUE(adapter, agent_session_id))",
    "CREATE TABLE tasks (id TEXT PRIMARY KEY NOT NULL, session_id TEXT NOT NULL REFERENCES sessions(id), working_tree_id TEXT NOT NULL REFERENCES working_trees(id), status TEXT NOT NULL CHECK(status IN ('running', 'finalizing', 'completed', 'failed')))",
    "CREATE UNIQUE INDEX active_task_per_working_tree ON tasks(working_tree_id) WHERE status IN ('running', 'finalizing')",
    "CREATE TRIGGER task_session_working_tree_matches BEFORE INSERT ON tasks WHEN (SELECT working_tree_id FROM sessions WHERE id = NEW.session_id) != NEW.working_tree_id BEGIN SELECT RAISE(ABORT, 'task working tree must match session working tree'); END",
    "CREATE TABLE inputs (id TEXT PRIMARY KEY NOT NULL, session_id TEXT NOT NULL REFERENCES sessions(id), task_id TEXT REFERENCES tasks(id), input_id TEXT NOT NULL, UNIQUE(session_id, input_id))",
    "CREATE TABLE admission_candidates (id TEXT PRIMARY KEY NOT NULL, session_id TEXT NOT NULL REFERENCES sessions(id), native_input_id TEXT NOT NULL, input_id TEXT REFERENCES inputs(id), UNIQUE(session_id, native_input_id))",
    "CREATE TABLE task_baseline_files (id TEXT PRIMARY KEY NOT NULL, task_id TEXT NOT NULL REFERENCES tasks(id))",
    "CREATE TABLE task_file_changes (id TEXT PRIMARY KEY NOT NULL, task_id TEXT NOT NULL REFERENCES tasks(id))",
    "CREATE TABLE inbound_events (id TEXT PRIMARY KEY NOT NULL, payload_hash TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('received', 'processing', 'accepted', 'rejected')))",
)


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in (
        "DROP TABLE inbound_events",
        "DROP TABLE task_file_changes",
        "DROP TABLE task_baseline_files",
        "DROP TABLE admission_candidates",
        "DROP TABLE inputs",
        "DROP TRIGGER task_session_working_tree_matches",
        "DROP INDEX active_task_per_working_tree",
        "DROP TABLE tasks",
        "DROP TABLE sessions",
        "DROP TABLE working_trees",
        "DROP TABLE projects",
    ):
        op.execute(statement)
