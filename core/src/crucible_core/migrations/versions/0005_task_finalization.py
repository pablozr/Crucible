"""Add durable task finalization evidence.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-07
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = (
        "ALTER TABLE working_trees ADD COLUMN capture_generation INTEGER "
        "NOT NULL DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN execution_id TEXT",
        "ALTER TABLE tasks ADD COLUMN terminal_signal TEXT",
        "ALTER TABLE tasks ADD COLUMN terminal_outcome TEXT",
        "ALTER TABLE tasks ADD COLUMN compatibility_profile TEXT",
        "ALTER TABLE tasks ADD COLUMN capture_generation INTEGER "
        "NOT NULL DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN final_head TEXT",
        "ALTER TABLE tasks ADD COLUMN final_branch TEXT",
        "ALTER TABLE tasks ADD COLUMN final_status BLOB",
        "ALTER TABLE tasks ADD COLUMN final_index_manifest BLOB",
        "ALTER TABLE tasks ADD COLUMN snapshot_frozen_at TEXT",
        "ALTER TABLE tasks ADD COLUMN task_diff TEXT",
        "ALTER TABLE tasks ADD COLUMN evidence_completeness TEXT",
        "ALTER TABLE tasks ADD COLUMN completed_at TEXT",
        "ALTER TABLE tasks ADD COLUMN failed_at TEXT",
        "ALTER TABLE task_file_changes ADD COLUMN path TEXT",
        "ALTER TABLE task_file_changes ADD COLUMN operation TEXT",
        "ALTER TABLE task_file_changes ADD COLUMN final_status TEXT",
        "ALTER TABLE task_file_changes ADD COLUMN final_sha256 TEXT",
        "ALTER TABLE task_file_changes ADD COLUMN final_size INTEGER",
        "ALTER TABLE task_file_changes ADD COLUMN final_is_binary INTEGER",
        "ALTER TABLE task_file_changes ADD COLUMN final_content BLOB",
        "ALTER TABLE task_file_changes ADD COLUMN evidence_status TEXT",
        "ALTER TABLE task_file_changes ADD COLUMN evidence_reason TEXT",
        "ALTER TABLE task_file_changes ADD COLUMN patch TEXT",
        "ALTER TABLE task_file_changes ADD COLUMN materialized_at TEXT",
        "CREATE UNIQUE INDEX task_baseline_files_task_path "
        "ON task_baseline_files(task_id, path)",
        "CREATE UNIQUE INDEX task_file_changes_task_path "
        "ON task_file_changes(task_id, path)",
        "CREATE INDEX tasks_finalization_recovery "
        "ON tasks(status, snapshot_frozen_at)",
    )
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError(
        "Finalization evidence cannot be removed safely."
    )
