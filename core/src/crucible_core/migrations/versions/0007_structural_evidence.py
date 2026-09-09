"""Persist structural file identity for honest evidence.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-09
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = (
        "ALTER TABLE task_baseline_files ADD COLUMN mode TEXT",
        "ALTER TABLE task_baseline_files ADD COLUMN gitlink_oid TEXT",
        "ALTER TABLE task_file_changes ADD COLUMN baseline_mode TEXT",
        "ALTER TABLE task_file_changes ADD COLUMN baseline_gitlink_oid TEXT",
        "ALTER TABLE task_file_changes ADD COLUMN final_mode TEXT",
        "ALTER TABLE task_file_changes ADD COLUMN final_gitlink_oid TEXT",
    )
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("Structural evidence cannot be removed safely.")
