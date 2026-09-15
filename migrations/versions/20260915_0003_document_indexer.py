"""add document indexer observability fields

Revision ID: 20260915_0003
Revises: 20260803_0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260915_0003"
down_revision: str | None = "20260803_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_document", sa.Column("vector_document_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "knowledge_index_job",
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "knowledge_index_job",
        sa.Column("warnings", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "knowledge_index_job", sa.Column("pipeline_revision", sa.String(64))
    )
    op.add_column("knowledge_index_job", sa.Column("source_hash", sa.String(64)))
    op.add_column("knowledge_index_job", sa.Column("claim_token", sa.String(36)))
    op.add_column(
        "knowledge_index_job", sa.Column("lease_expires_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "knowledge_index_job",
        sa.Column("cleanup_required", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "knowledge_index_job",
        sa.Column("cleanup_artifacts", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("knowledge_index_job", "cleanup_artifacts")
    op.drop_column("knowledge_index_job", "cleanup_required")
    op.drop_column("knowledge_index_job", "lease_expires_at")
    op.drop_column("knowledge_index_job", "claim_token")
    op.drop_column("knowledge_index_job", "source_hash")
    op.drop_column("knowledge_index_job", "pipeline_revision")
    op.drop_column("knowledge_index_job", "warnings")
    op.drop_column("knowledge_index_job", "chunk_count")
    op.drop_column("knowledge_document", "vector_document_id")
