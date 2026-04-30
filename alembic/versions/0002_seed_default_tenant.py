"""seed default tenant

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-30
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO core.tenants (id, slug, config)
        VALUES ('00000000-0000-0000-0000-000000000001', 'default', '{}')
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM core.tenants WHERE id = '00000000-0000-0000-0000-000000000001'")
