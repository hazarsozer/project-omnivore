"""add sinks and matched_rule_id to chunks for routing enforcement (M-1/M-3)

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-14
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE core.chunks
            ADD COLUMN sinks TEXT[] NOT NULL DEFAULT ARRAY['relational','vector'],
            ADD COLUMN matched_rule_id TEXT
        """
    )
    # Rebuild HNSW partial index to only include vector-sinked chunks
    op.execute("DROP INDEX IF EXISTS core.idx_chunks_embedding")
    op.execute("DROP INDEX IF EXISTS idx_chunks_embedding")
    op.execute(
        """
        CREATE INDEX idx_chunks_embedding ON core.chunks
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE embedding IS NOT NULL AND 'vector' = ANY(sinks)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_chunks_embedding")
    op.execute(
        """
        CREATE INDEX idx_chunks_embedding ON core.chunks
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE embedding IS NOT NULL
        """
    )
    op.execute(
        """
        ALTER TABLE core.chunks
            DROP COLUMN IF EXISTS sinks,
            DROP COLUMN IF EXISTS matched_rule_id
        """
    )
