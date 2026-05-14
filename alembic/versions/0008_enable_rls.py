"""enable Row Level Security on all tenant-scoped tables

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-14
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

# Tables that isolate by tenant_id
_TENANT_TABLES = [
    "core.documents",
    "core.chunks",
    "core.entities",
    "core.extracted_tables",
    "core.extracted_rows",
    "core.api_keys",
]


def _policy_sql(table: str, short: str) -> str:
    return f"""
        CREATE POLICY tenant_isolation ON {table}
        USING (
            tenant_id = current_setting('app.current_tenant_id', true)::uuid
            OR current_setting('app.bypass_rls', true) = 'on'
        )
        WITH CHECK (
            tenant_id = current_setting('app.current_tenant_id', true)::uuid
            OR current_setting('app.bypass_rls', true) = 'on'
        )
    """


def upgrade() -> None:
    for table in _TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(_policy_sql(table, table.split(".")[1]))

    # tenants table: a tenant sees only its own row
    op.execute("ALTER TABLE core.tenants ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE core.tenants FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_self ON core.tenants
        USING (
            id = current_setting('app.current_tenant_id', true)::uuid
            OR current_setting('app.bypass_rls', true) = 'on'
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_self ON core.tenants")
    op.execute("ALTER TABLE core.tenants DISABLE ROW LEVEL SECURITY")

    for table in _TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
