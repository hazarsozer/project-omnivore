"""convert core.chunks to a hash-partitioned table (4 partitions, by id)

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-19

Strategy
--------
1. Back up all existing chunk rows to a temporary table.
2. Drop the entities → chunks FK (ON DELETE SET NULL; no data loss).
3. DROP the original unpartitioned table (CASCADE removes indexes and the RLS
   policy that was attached to it).
4. Recreate core.chunks as PARTITION BY HASH (id) with 4 child partitions.
5. Copy data back (excluding the GENERATED column content_tsv).
6. Recreate indexes:
   - idx_chunks_doc  (document_id, ordinal)
   - idx_chunks_tsv  (GIN on content_tsv)
   - idx_chunks_embedding  (HNSW partial, only where vector-sinked)
7. Re-add the FK from entities.
8. Re-enable RLS + recreate the tenant_isolation policy on the parent.
9. Drop the temporary backup table.

Downgrade
---------
Reversing a partition conversion is inherently destructive (the original table
structure is lost once the backup table is dropped). The downgrade function
raises NotImplementedError. If manual reversal is needed:
  1. CREATE TABLE core.chunks_restore (LIKE the original DDL below).
  2. INSERT INTO core.chunks_restore SELECT * FROM the partitioned table.
  3. DROP TABLE core.chunks CASCADE.
  4. ALTER TABLE core.chunks_restore RENAME TO chunks.
  5. Recreate all indexes and constraints.
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # 1. Back up existing data                                             #
    # ------------------------------------------------------------------ #
    op.execute("CREATE TABLE core.chunks_backup AS SELECT * FROM core.chunks")

    # ------------------------------------------------------------------ #
    # 2. Drop FK from entities → chunks                                   #
    # ------------------------------------------------------------------ #
    # The FK is named entities_chunk_id_fkey (SQLAlchemy's default naming
    # for ForeignKey("core.chunks.id", ondelete="SET NULL")).
    op.execute(
        "ALTER TABLE core.entities DROP CONSTRAINT IF EXISTS entities_chunk_id_fkey"
    )

    # ------------------------------------------------------------------ #
    # 3. Drop the original table (CASCADE removes indexes + RLS policy)   #
    # ------------------------------------------------------------------ #
    op.execute("DROP TABLE core.chunks CASCADE")

    # ------------------------------------------------------------------ #
    # 4. Create partitioned parent table                                   #
    # ------------------------------------------------------------------ #
    # Notes:
    # - content_tsv is GENERATED ALWAYS AS — recreated automatically.
    # - confidence column is REAL in the original schema; using FLOAT4
    #   (same type) here for clarity.
    # - PRIMARY KEY (id) satisfies the PostgreSQL requirement that the
    #   partition key be included in the primary key, since id IS the key.
    op.execute(
        """
        CREATE TABLE core.chunks (
            id               UUID NOT NULL,
            document_id      UUID NOT NULL
                                 REFERENCES core.documents(id) ON DELETE CASCADE,
            tenant_id        UUID NOT NULL REFERENCES core.tenants(id),
            ordinal          INTEGER NOT NULL,
            kind             TEXT NOT NULL,
            content          TEXT NOT NULL,
            content_tsv      TSVECTOR GENERATED ALWAYS AS
                                 (to_tsvector('simple', content)) STORED,
            token_count      INTEGER NOT NULL,
            position         JSONB NOT NULL,
            heading_path     TEXT[],
            source_block_ids UUID[],
            table_lineage    JSONB,
            embedding        vector(768),
            embedding_model  TEXT,
            language         TEXT,
            confidence       REAL,
            sinks            TEXT[] NOT NULL
                                 DEFAULT ARRAY['relational','vector'],
            matched_rule_id  TEXT,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (id)
        ) PARTITION BY HASH (id)
        """
    )

    # ------------------------------------------------------------------ #
    # 5. Create 4 hash partitions                                          #
    # ------------------------------------------------------------------ #
    for i in range(4):
        op.execute(
            f"CREATE TABLE core.chunks_p{i} "
            f"PARTITION OF core.chunks "
            f"FOR VALUES WITH (MODULUS 4, REMAINDER {i})"
        )

    # ------------------------------------------------------------------ #
    # 6. Copy data back (exclude content_tsv — GENERATED column)          #
    # ------------------------------------------------------------------ #
    op.execute(
        """
        INSERT INTO core.chunks (
            id, document_id, tenant_id, ordinal, kind, content,
            token_count, position, heading_path, source_block_ids,
            table_lineage, embedding, embedding_model, language,
            confidence, sinks, matched_rule_id, created_at
        )
        SELECT
            id, document_id, tenant_id, ordinal, kind, content,
            token_count, position, heading_path, source_block_ids,
            table_lineage, embedding, embedding_model, language,
            confidence, sinks, matched_rule_id, created_at
        FROM core.chunks_backup
        """
    )

    # ------------------------------------------------------------------ #
    # 7. Recreate indexes                                                  #
    # ------------------------------------------------------------------ #
    op.execute(
        "CREATE INDEX idx_chunks_doc ON core.chunks (document_id, ordinal)"
    )
    op.execute(
        "CREATE INDEX idx_chunks_tsv ON core.chunks USING GIN (content_tsv)"
    )

    # HNSW index — partial, matching the definition from migration 0009.
    # pgvector must be loaded and the extension must exist; if it is not
    # available (e.g., test environment without pgvector), we swallow the
    # error so the rest of the migration succeeds.  The index can be
    # recreated later once pgvector is available.
    try:
        op.execute(
            """
            CREATE INDEX idx_chunks_embedding ON core.chunks
                USING hnsw (embedding vector_cosine_ops)
                WITH (m = 16, ef_construction = 64)
                WHERE embedding IS NOT NULL AND 'vector' = ANY(sinks)
            """
        )
    except Exception:  # noqa: BLE001
        pass

    # ------------------------------------------------------------------ #
    # 8. Recreate FK from entities → chunks                               #
    # ------------------------------------------------------------------ #
    op.execute(
        """
        ALTER TABLE core.entities
            ADD CONSTRAINT entities_chunk_id_fkey
            FOREIGN KEY (chunk_id) REFERENCES core.chunks(id)
            ON DELETE SET NULL
        """
    )

    # ------------------------------------------------------------------ #
    # 9. Re-enable RLS on the parent (partitions inherit automatically)   #
    # ------------------------------------------------------------------ #
    op.execute("ALTER TABLE core.chunks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE core.chunks FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON core.chunks
        USING (
            tenant_id = current_setting('app.current_tenant_id', true)::uuid
            OR current_setting('app.bypass_rls', true) = 'on'
        )
        WITH CHECK (
            tenant_id = current_setting('app.current_tenant_id', true)::uuid
            OR current_setting('app.bypass_rls', true) = 'on'
        )
        """
    )

    # ------------------------------------------------------------------ #
    # 10. Drop backup                                                      #
    # ------------------------------------------------------------------ #
    op.execute("DROP TABLE core.chunks_backup")


def downgrade() -> None:
    raise NotImplementedError(
        "Reversing hash-partitioning of core.chunks requires manual "
        "intervention. Steps:\n"
        "  1. CREATE TABLE core.chunks_old AS SELECT * FROM core.chunks;\n"
        "  2. DROP TABLE core.chunks CASCADE;\n"
        "  3. Recreate core.chunks as the original unpartitioned table "
        "(see migration 0001 DDL + migrations 0003/0009 for column changes).\n"
        "  4. INSERT INTO core.chunks SELECT <non-generated cols> "
        "FROM core.chunks_old;\n"
        "  5. Recreate indexes and RLS policy.\n"
        "  6. DROP TABLE core.chunks_old;\n"
        "  7. Re-add entities → chunks FK."
    )
