"""resize embedding vector from 1536 to 768 (BAAI/bge-base-en-v1.5)

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-05

Data-loss Warning
-----------------
This migration is DESTRUCTIVE IN BOTH DIRECTIONS. pgvector has no in-place cast
to change a vector column's dimensionality, so both upgrade() and downgrade()
DROP the ``core.chunks.embedding`` column (and its HNSW index) and re-ADD it at
the new dimension. Every stored embedding is lost in either direction.

  - upgrade()   : vector(1536) -> vector(768)  (drops all 1536-dim embeddings)
  - downgrade() : vector(768)  -> vector(1536) (drops all 768-dim embeddings)

Do NOT run this against a populated production database without a re-embed plan:
after running, re-embed every chunk (re-ingest, or backfill embeddings for all
rows where ``embedding IS NULL``) — search returns nothing for un-embedded rows.

As a guardrail, upgrade() refuses to run if any non-NULL embeddings exist; clear
them deliberately (``UPDATE core.chunks SET embedding = NULL``) first if intended.
downgrade() has no such guard and will drop data silently.
"""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    count = bind.execute(
        sa.text("SELECT COUNT(*) FROM core.chunks WHERE embedding IS NOT NULL")
    ).scalar()
    if count and count > 0:
        raise RuntimeError(
            f"Migration 0003 would destroy {count} existing embeddings. "
            "Run `UPDATE core.chunks SET embedding = NULL` first if intentional."
        )
    # HNSW index must be dropped before column type change
    op.execute("DROP INDEX IF EXISTS core.idx_chunks_embedding")
    # Drop and re-add: pgvector has no direct resize cast
    op.execute("ALTER TABLE core.chunks DROP COLUMN IF EXISTS embedding")
    op.execute("ALTER TABLE core.chunks ADD COLUMN embedding vector(768)")
    op.execute("""
        CREATE INDEX idx_chunks_embedding ON core.chunks
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE embedding IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS core.idx_chunks_embedding")
    op.execute("ALTER TABLE core.chunks DROP COLUMN IF EXISTS embedding")
    op.execute("ALTER TABLE core.chunks ADD COLUMN embedding vector(1536)")
    op.execute("""
        CREATE INDEX idx_chunks_embedding ON core.chunks
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE embedding IS NOT NULL
    """)
