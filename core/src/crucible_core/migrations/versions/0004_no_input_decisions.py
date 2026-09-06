"""Persist durable no-input admission decisions.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-06
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE admission_no_input_decisions ("
        "adapter TEXT NOT NULL, "
        "agent_session_id TEXT NOT NULL, "
        "native_input_id TEXT NOT NULL, "
        "admission_hash TEXT NOT NULL, "
        "outcome TEXT NOT NULL "
        "CHECK(outcome IN "
        "('released_overlap', 'steer_without_active_task')), "
        "event_id TEXT NOT NULL, "
        "reference_task_id TEXT, "
        "created_at TEXT NOT NULL, "
        "PRIMARY KEY (adapter, agent_session_id, native_input_id))"
    )
    op.execute(
        "CREATE INDEX admission_no_input_decisions_event_id "
        "ON admission_no_input_decisions(event_id)"
    )


def downgrade() -> None:
    raise NotImplementedError("No-input decisions cannot be removed safely.")
