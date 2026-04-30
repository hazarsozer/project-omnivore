"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-04-30
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS core")
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute("""
        CREATE TABLE core.tenants (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            slug        TEXT UNIQUE NOT NULL,
            config      JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE core.documents (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           UUID NOT NULL REFERENCES core.tenants(id),
            parent_document_id  UUID REFERENCES core.documents(id),
            sha256              BYTEA NOT NULL,
            filename            TEXT NOT NULL,
            mime_type           TEXT NOT NULL,
            size_bytes          BIGINT NOT NULL,
            storage_uri         TEXT NOT NULL,
            handler_name        TEXT,
            handler_version     TEXT,
            status              TEXT NOT NULL CHECK (status IN (
                                    'queued','extracting','enriching','indexed','failed','duplicate')),
            error               JSONB,
            metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
            routing_decision    JSONB,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            indexed_at          TIMESTAMPTZ,
            UNIQUE (tenant_id, sha256)
        )
    """)

    op.execute("CREATE INDEX idx_documents_tenant_status ON core.documents(tenant_id, status)")
    op.execute("CREATE INDEX idx_documents_metadata ON core.documents USING GIN(metadata jsonb_path_ops)")
    op.execute("CREATE INDEX idx_documents_created ON core.documents(tenant_id, created_at DESC)")

    op.execute("""
        CREATE TABLE core.chunks (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            document_id     UUID NOT NULL REFERENCES core.documents(id) ON DELETE CASCADE,
            tenant_id       UUID NOT NULL REFERENCES core.tenants(id),
            ordinal         INT NOT NULL,
            kind            TEXT NOT NULL,
            content         TEXT NOT NULL,
            content_tsv     TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
            token_count     INT NOT NULL,
            position        JSONB NOT NULL,
            heading_path    TEXT[],
            source_block_ids UUID[],
            table_lineage   JSONB,
            embedding       vector(1536),
            embedding_model TEXT,
            language        TEXT,
            confidence      REAL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE INDEX idx_chunks_embedding ON core.chunks
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE embedding IS NOT NULL
    """)
    op.execute("CREATE INDEX idx_chunks_tsv ON core.chunks USING GIN(content_tsv)")
    op.execute("CREATE INDEX idx_chunks_doc ON core.chunks(document_id, ordinal)")

    op.execute("""
        CREATE TABLE core.entities (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            document_id UUID NOT NULL REFERENCES core.documents(id) ON DELETE CASCADE,
            tenant_id   UUID NOT NULL REFERENCES core.tenants(id),
            chunk_id    UUID REFERENCES core.chunks(id) ON DELETE SET NULL,
            label       TEXT NOT NULL,
            value       TEXT NOT NULL,
            normalized  TEXT,
            confidence  REAL,
            metadata    JSONB DEFAULT '{}'::jsonb,
            UNIQUE (document_id, label, normalized)
        )
    """)

    op.execute("""
        CREATE TABLE core.extracted_tables (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            document_id UUID NOT NULL REFERENCES core.documents(id) ON DELETE CASCADE,
            tenant_id   UUID NOT NULL REFERENCES core.tenants(id),
            name        TEXT NOT NULL,
            schema      JSONB NOT NULL,
            row_count   INT NOT NULL
        )
    """)

    op.execute("""
        CREATE TABLE core.extracted_rows (
            table_id UUID NOT NULL REFERENCES core.extracted_tables(id) ON DELETE CASCADE,
            ordinal  INT NOT NULL,
            data     JSONB NOT NULL,
            PRIMARY KEY (table_id, ordinal)
        )
    """)

    op.execute("CREATE INDEX idx_rows_data ON core.extracted_rows USING GIN(data jsonb_path_ops)")

    op.execute("""
        CREATE TABLE core.jobs (
            id          UUID PRIMARY KEY,
            document_id UUID REFERENCES core.documents(id) ON DELETE CASCADE,
            stage       TEXT NOT NULL,
            status      TEXT NOT NULL,
            attempts    INT NOT NULL DEFAULT 0,
            error       JSONB,
            started_at  TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            duration_ms INT
        )
    """)

    op.execute("""
        CREATE TABLE core.outbox (
            id           BIGSERIAL PRIMARY KEY,
            aggregate_id UUID NOT NULL,
            event_type   TEXT NOT NULL,
            payload      JSONB NOT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            published_at TIMESTAMPTZ
        )
    """)

    op.execute("CREATE INDEX idx_outbox_unpublished ON core.outbox(id) WHERE published_at IS NULL")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS core.outbox CASCADE")
    op.execute("DROP TABLE IF EXISTS core.jobs CASCADE")
    op.execute("DROP TABLE IF EXISTS core.extracted_rows CASCADE")
    op.execute("DROP TABLE IF EXISTS core.extracted_tables CASCADE")
    op.execute("DROP TABLE IF EXISTS core.entities CASCADE")
    op.execute("DROP TABLE IF EXISTS core.chunks CASCADE")
    op.execute("DROP TABLE IF EXISTS core.documents CASCADE")
    op.execute("DROP TABLE IF EXISTS core.tenants CASCADE")
    op.execute("DROP SCHEMA IF EXISTS core CASCADE")
