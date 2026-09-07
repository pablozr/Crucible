"""Add durable terminal capture authorization.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-07
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = (
        "ALTER TABLE tasks ADD COLUMN terminal_observed_at TEXT",
        "ALTER TABLE tasks ADD COLUMN capture_not_after TEXT",
        "UPDATE tasks SET status = 'failed', "
        "failure_code = 'EXECUTION_ID_REQUIRED_LEGACY', "
        "failure_message = 'EXECUTION_ID_REQUIRED_LEGACY', "
        "failed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
        "WHERE status IN ('running', 'finalizing') "
        "AND (execution_id IS NULL OR execution_id = '')",
        "UPDATE inbound_events SET status = 'rejected', "
        "outcome = 'rejected', "
        "failure_code = 'EXECUTION_ID_REQUIRED_LEGACY', "
        "failure_message = 'EXECUTION_ID_REQUIRED_LEGACY' "
        "WHERE event_type = 'task_completed' "
        "AND status = 'processing' AND task_id IN "
        "(SELECT id FROM tasks WHERE status = 'failed' "
        "AND failure_code = 'EXECUTION_ID_REQUIRED_LEGACY')",
    )
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError(
        "Terminal authorization cannot be removed safely."
    )
