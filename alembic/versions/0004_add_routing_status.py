"""add 'routing' to documents status check constraint

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-07
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE core.documents DROP CONSTRAINT IF EXISTS documents_status_check")
    op.execute(
        "ALTER TABLE core.documents ADD CONSTRAINT documents_status_check "
        "CHECK (status IN ('queued','routing','extracting','enriching','indexed','failed','duplicate'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE core.documents DROP CONSTRAINT IF EXISTS documents_status_check")
    op.execute(
        "ALTER TABLE core.documents ADD CONSTRAINT documents_status_check "
        "CHECK (status IN ('queued','extracting','enriching','indexed','failed','duplicate'))"
    )
