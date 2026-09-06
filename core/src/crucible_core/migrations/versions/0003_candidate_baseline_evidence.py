"""Persist candidate baseline evidence before admission.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-06
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE admission_candidates ADD COLUMN admission_hash TEXT"
    )
    op.execute("ALTER TABLE admission_candidates ADD COLUMN event_id TEXT")
    op.execute(
        "CREATE TABLE candidate_baseline_files ("
        "id TEXT PRIMARY KEY NOT NULL, "
        "candidate_id TEXT NOT NULL REFERENCES admission_candidates(id), "
        "path TEXT NOT NULL, status TEXT NOT NULL, sha256 TEXT, "
        "size INTEGER, is_binary INTEGER, content BLOB)"
    )
    op.execute(
        "CREATE INDEX candidate_baseline_files_candidate_id "
        "ON candidate_baseline_files(candidate_id)"
    )


def downgrade() -> None:
    raise NotImplementedError("Candidate evidence cannot be removed safely.")
