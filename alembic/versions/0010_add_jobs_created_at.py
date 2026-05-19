"""add created_at column to core.jobs

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-19
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add created_at with a server default of now().
    # Existing rows (if any) will receive now() at migration time, which is the
    # best approximation available without a real historical timestamp.
    op.execute(
        """
        ALTER TABLE core.jobs
            ADD COLUMN created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE core.jobs DROP COLUMN IF EXISTS created_at")
