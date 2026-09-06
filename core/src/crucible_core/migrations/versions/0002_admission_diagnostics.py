"""Add Core admission and diagnostic state.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-06
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


UPGRADE_STATEMENTS = (
    "ALTER TABLE sessions ADD COLUMN adapter_version TEXT",
    "ALTER TABLE sessions ADD COLUMN workspace_path TEXT",
    "ALTER TABLE tasks ADD COLUMN started_at TEXT",
    "ALTER TABLE tasks ADD COLUMN baseline_head TEXT",
    "ALTER TABLE tasks ADD COLUMN baseline_status TEXT",
    "ALTER TABLE tasks ADD COLUMN baseline_branch TEXT",
    "ALTER TABLE tasks ADD COLUMN baseline_index_manifest BLOB",
    "ALTER TABLE tasks ADD COLUMN failure_code TEXT",
    "ALTER TABLE tasks ADD COLUMN failure_message TEXT",
    "ALTER TABLE inputs ADD COLUMN outcome TEXT",
    "ALTER TABLE inputs ADD COLUMN admission_hash TEXT",
    "ALTER TABLE admission_candidates ADD COLUMN status TEXT",
    "ALTER TABLE admission_candidates ADD COLUMN baseline_head TEXT",
    "ALTER TABLE admission_candidates ADD COLUMN baseline_status TEXT",
    "ALTER TABLE admission_candidates ADD COLUMN baseline_branch TEXT",
    "ALTER TABLE admission_candidates ADD COLUMN baseline_index_manifest BLOB",
    "ALTER TABLE admission_candidates ADD COLUMN outcome TEXT",
    "ALTER TABLE admission_candidates ADD COLUMN created_at TEXT",
    "ALTER TABLE admission_candidates ADD COLUMN failure_code TEXT",
    "ALTER TABLE admission_candidates ADD COLUMN failure_message TEXT",
    "ALTER TABLE inbound_events ADD COLUMN event_type TEXT",
    "ALTER TABLE inbound_events ADD COLUMN received_at TEXT",
    "ALTER TABLE inbound_events ADD COLUMN outcome TEXT",
    "ALTER TABLE inbound_events ADD COLUMN input_id TEXT",
    "ALTER TABLE inbound_events ADD COLUMN task_id TEXT",
    "ALTER TABLE inbound_events ADD COLUMN failure_code TEXT",
    "ALTER TABLE inbound_events ADD COLUMN failure_message TEXT",
    "ALTER TABLE task_baseline_files ADD COLUMN path TEXT",
    "ALTER TABLE task_baseline_files ADD COLUMN status TEXT",
    "ALTER TABLE task_baseline_files ADD COLUMN sha256 TEXT",
    "ALTER TABLE task_baseline_files ADD COLUMN size INTEGER",
    "ALTER TABLE task_baseline_files ADD COLUMN is_binary INTEGER",
    "ALTER TABLE task_baseline_files ADD COLUMN content BLOB",
    "UPDATE tasks SET started_at = COALESCE(started_at, datetime('now'))",
    "CREATE INDEX tasks_started_at_id ON tasks(started_at DESC, id DESC)",
    "CREATE INDEX inputs_task_id_id ON inputs(task_id, id)",
    "CREATE INDEX baseline_files_task_id ON task_baseline_files(task_id)",
)


def upgrade() -> None:
    for statement in UPGRADE_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("Admission state cannot be removed safely.")
