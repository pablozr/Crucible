"""Persist canonical semantic hash beside transport hash.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-10
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE inbound_events ADD COLUMN semantic_hash TEXT",
    )


def downgrade() -> None:
    raise NotImplementedError("Semantic hash cannot be removed safely.")
