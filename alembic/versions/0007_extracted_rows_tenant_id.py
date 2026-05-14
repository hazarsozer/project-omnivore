"""add tenant_id to extracted_rows (denormalize for RLS)

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-14
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE core.extracted_rows ADD COLUMN tenant_id UUID")
    op.execute(
        """
        UPDATE core.extracted_rows er
        SET tenant_id = et.tenant_id
        FROM core.extracted_tables et
        WHERE er.table_id = et.id
        """
    )
    op.execute(
        """
        ALTER TABLE core.extracted_rows
            ALTER COLUMN tenant_id SET NOT NULL,
            ADD CONSTRAINT extracted_rows_tenant_fk
                FOREIGN KEY (tenant_id) REFERENCES core.tenants(id)
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE core.extracted_rows DROP CONSTRAINT IF EXISTS extracted_rows_tenant_fk"
    )
    op.execute("ALTER TABLE core.extracted_rows DROP COLUMN IF EXISTS tenant_id")
