"""add missing FK indexes on extracted_rows.tenant_id

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-22

extracted_rows.tenant_id was added in migration 0007 with a FK constraint
but no supporting index. RLS filtering and cascade operations on this column
caused full table scans. This migration adds the missing index.
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_extracted_rows_tenant "
        "ON core.extracted_rows (tenant_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS core.idx_extracted_rows_tenant")
