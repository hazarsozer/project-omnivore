"""add display_name, status, updated_at to tenants

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-14
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE core.tenants
            ADD COLUMN display_name TEXT NOT NULL DEFAULT 'Unnamed',
            ADD COLUMN status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'suspended', 'deleted')),
            ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION core.bump_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN NEW.updated_at = now(); RETURN NEW; END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_tenants_updated_at
            BEFORE UPDATE ON core.tenants
            FOR EACH ROW EXECUTE FUNCTION core.bump_updated_at()
        """
    )
    op.execute("UPDATE core.tenants SET display_name = 'Default' WHERE slug = 'default'")
    op.execute("ALTER TABLE core.tenants ALTER COLUMN display_name DROP DEFAULT")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_tenants_updated_at ON core.tenants")
    op.execute("DROP FUNCTION IF EXISTS core.bump_updated_at()")
    op.execute(
        """
        ALTER TABLE core.tenants
            DROP COLUMN IF EXISTS display_name,
            DROP COLUMN IF EXISTS status,
            DROP COLUMN IF EXISTS updated_at
        """
    )
