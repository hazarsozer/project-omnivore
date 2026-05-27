"""add created_at and updated_at to core.entities

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-27
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE core.entities
            ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE core.entities DROP COLUMN IF EXISTS updated_at")
    op.execute("ALTER TABLE core.entities DROP COLUMN IF EXISTS created_at")
