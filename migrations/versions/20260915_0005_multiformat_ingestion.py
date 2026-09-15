"""add multiformat parsing metadata

Revision ID: 20260915_0005
Revises: 20260915_0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260915_0005"
down_revision: str | None = "20260915_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("knowledge_document", sa.Column("block_count", sa.Integer()))
    op.add_column("knowledge_chunk", sa.Column("heading_path", sa.JSON()))
    op.add_column("knowledge_chunk", sa.Column("anchor", sa.String(255)))
    op.add_column("knowledge_chunk", sa.Column("block_kind", sa.String(32)))
    op.add_column("knowledge_chunk", sa.Column("source_locator", sa.JSON()))
    chunks = sa.table(
        "knowledge_chunk",
        sa.column("heading_path", sa.JSON()),
        sa.column("block_kind", sa.String()),
        sa.column("source_locator", sa.JSON()),
    )
    op.get_bind().execute(
        chunks.update().values(heading_path=[], block_kind="PARAGRAPH", source_locator={})
    )
    with op.batch_alter_table("knowledge_chunk") as batch:
        batch.alter_column("page_start", existing_type=sa.Integer(), nullable=True)
        batch.alter_column("page_end", existing_type=sa.Integer(), nullable=True)
        batch.alter_column("heading_path", existing_type=sa.JSON(), nullable=False)
        batch.alter_column("block_kind", existing_type=sa.String(32), nullable=False)
        batch.alter_column("source_locator", existing_type=sa.JSON(), nullable=False)
    op.add_column("knowledge_index_job", sa.Column("parser_version", sa.String(128)))
    op.add_column("knowledge_index_job", sa.Column("chunking_version", sa.String(64)))
    op.add_column(
        "knowledge_index_job",
        sa.Column("quality_signals", sa.JSON(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("knowledge_index_job", "quality_signals")
    op.drop_column("knowledge_index_job", "chunking_version")
    op.drop_column("knowledge_index_job", "parser_version")
    op.execute(
        sa.text(
            """
            UPDATE knowledge_chunk
            SET page_start = COALESCE(page_start, 1),
                page_end = COALESCE(page_end, page_start, 1)
            """
        )
    )
    with op.batch_alter_table("knowledge_chunk") as batch:
        batch.alter_column("page_end", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("page_start", existing_type=sa.Integer(), nullable=False)
    op.drop_column("knowledge_chunk", "source_locator")
    op.drop_column("knowledge_chunk", "block_kind")
    op.drop_column("knowledge_chunk", "anchor")
    op.drop_column("knowledge_chunk", "heading_path")
    op.drop_column("knowledge_document", "block_count")
