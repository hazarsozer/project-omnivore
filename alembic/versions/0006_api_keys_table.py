"""create core.api_keys table

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-14
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE core.api_keys (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   UUID NOT NULL REFERENCES core.tenants(id) ON DELETE CASCADE,
            name        TEXT NOT NULL,
            prefix      TEXT NOT NULL,
            key_hash    TEXT NOT NULL,
            last_used_at TIMESTAMPTZ,
            revoked_at  TIMESTAMPTZ,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at  TIMESTAMPTZ,
            scopes      TEXT[] NOT NULL DEFAULT ARRAY[
                'documents:read','documents:write',
                'chunks:read','entities:read',
                'search:read','handlers:read','tenant:manage'
            ]
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_api_keys_prefix ON core.api_keys(prefix) WHERE revoked_at IS NULL"
    )
    op.execute(
        "CREATE INDEX idx_api_keys_tenant ON core.api_keys(tenant_id) WHERE revoked_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS core.api_keys")
