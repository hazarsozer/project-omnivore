"""convert core.jobs to a range-partitioned table (by created_at, monthly)

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-19

Depends on migration 0010 which added created_at to core.jobs.

Strategy
--------
1. Back up all existing job rows.
2. Drop the original unpartitioned jobs table (CASCADE is safe — no other
   table references core.jobs; the documents.id FK goes the other way).
3. Recreate core.jobs as PARTITION BY RANGE (created_at).
4. Create partitions:
   - core.jobs_default   — DEFAULT partition, catches all historical rows
                           and any rows outside explicitly named ranges.
   - core.jobs_2026_06   — June 2026 data [2026-06-01, 2026-07-01).
   New monthly partitions should be added each month before the month starts.
5. Copy data from backup (all rows land in jobs_default since existing rows
   predate any named range, or in the appropriate monthly partition).
6. Recreate indexes on the parent.
7. Drop backup.

Note: core.jobs has NO RLS (it is not in the _TENANT_TABLES list from
migration 0008), so no RLS steps are needed here.

Locking Warning
---------------
This migration holds an ACCESS EXCLUSIVE lock on core.jobs for the full
duration of the data-copy phase (step 5). On a populated production database
this means hard downtime — no reads or writes can proceed against that table
while the copy runs. Plan a maintenance window or use pg_repack for
zero-downtime partitioning on live data.

Downgrade
---------
Reversing a partition conversion is inherently destructive. The downgrade
function raises NotImplementedError.
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # 1. Back up existing data                                             #
    # ------------------------------------------------------------------ #
    op.execute("CREATE TABLE core.jobs_backup AS SELECT * FROM core.jobs")

    # ------------------------------------------------------------------ #
    # 2. Drop original table                                               #
    # ------------------------------------------------------------------ #
    # No other table has a FK to core.jobs, so CASCADE has no side-effects
    # beyond dropping the table itself and its indexes.
    op.execute("DROP TABLE core.jobs CASCADE")

    # ------------------------------------------------------------------ #
    # 3. Create partitioned parent table                                   #
    # ------------------------------------------------------------------ #
    # The primary key must include the partition key (created_at) for
    # range-partitioned tables in PostgreSQL.  We use a composite PK
    # (id, created_at) which is still unique because id is a UUID.
    # Application code that looks up jobs by id alone will need to include
    # created_at in the query or use the parent table (Postgres routes to
    # the correct partition automatically when the PK includes both cols).
    op.execute(
        """
        CREATE TABLE core.jobs (
            id          UUID NOT NULL,
            document_id UUID REFERENCES core.documents(id) ON DELETE CASCADE,
            stage       TEXT NOT NULL,
            status      TEXT NOT NULL,
            attempts    INTEGER NOT NULL DEFAULT 0,
            error       JSONB,
            started_at  TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            duration_ms INTEGER,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at)
        """
    )

    # ------------------------------------------------------------------ #
    # 4. Create partitions                                                 #
    # ------------------------------------------------------------------ #
    # DEFAULT partition — catches all rows that don't match a named range.
    # All historical data written before June 2026 lands here.
    op.execute(
        "CREATE TABLE core.jobs_default PARTITION OF core.jobs DEFAULT"
    )
    # June 2026 monthly partition (current month at migration time).
    op.execute(
        """
        CREATE TABLE core.jobs_2026_06
            PARTITION OF core.jobs
            FOR VALUES FROM ('2026-06-01') TO ('2026-07-01')
        """
    )

    # ------------------------------------------------------------------ #
    # 5. Copy data from backup                                             #
    # ------------------------------------------------------------------ #
    op.execute(
        """
        INSERT INTO core.jobs (
            id, document_id, stage, status, attempts, error,
            started_at, finished_at, duration_ms, created_at
        )
        SELECT
            id, document_id, stage, status, attempts, error,
            started_at, finished_at, duration_ms, created_at
        FROM core.jobs_backup
        """
    )

    # ------------------------------------------------------------------ #
    # 6. Recreate indexes on the parent                                    #
    # ------------------------------------------------------------------ #
    # Index lookups by document_id (common in worker task logging).
    op.execute(
        "CREATE INDEX idx_jobs_document ON core.jobs (document_id)"
    )
    # Index for status-based monitoring / retry queries.
    op.execute(
        "CREATE INDEX idx_jobs_status ON core.jobs (status, created_at DESC)"
    )

    # ------------------------------------------------------------------ #
    # 7. Drop backup                                                       #
    # ------------------------------------------------------------------ #
    op.execute("DROP TABLE core.jobs_backup")


def downgrade() -> None:
    raise NotImplementedError(
        "Reversing range-partitioning of core.jobs requires manual "
        "intervention. Steps:\n"
        "  1. CREATE TABLE core.jobs_old AS SELECT * FROM core.jobs;\n"
        "  2. DROP TABLE core.jobs CASCADE;\n"
        "  3. Recreate core.jobs as the original unpartitioned table "
        "(see migration 0001 DDL, now with the created_at column from 0010).\n"
        "  4. INSERT INTO core.jobs SELECT * FROM core.jobs_old;\n"
        "  5. DROP TABLE core.jobs_old;\n"
        "  6. Recreate any indexes."
    )
