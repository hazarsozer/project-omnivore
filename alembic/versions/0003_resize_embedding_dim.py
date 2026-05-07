"""resize embedding vector from 1536 to 768 (BAAI/bge-base-en-v1.5)

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-05
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
