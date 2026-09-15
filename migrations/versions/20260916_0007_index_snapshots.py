"""version knowledge chunks by index snapshot

Revision ID: 20260916_0007
Revises: 20260915_0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260916_0007"
down_revision: str | None = "20260915_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_chunk",
        sa.Column("index_identity", sa.String(255), nullable=False, server_default="legacy"),
    )
    with op.batch_alter_table("knowledge_chunk") as batch:
        batch.drop_constraint("uq_knowledge_chunk_position", type_="unique")
        batch.create_unique_constraint(
            "uq_knowledge_chunk_position",
            ["document_id", "index_identity", "chunk_index"],
        )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DELETE FROM knowledge_chunk
            WHERE id IN (
                SELECT id
                FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY document_id, chunk_index
                               ORDER BY created_at DESC, id DESC
                           ) AS snapshot_rank
                    FROM knowledge_chunk
                ) AS ranked_chunks
                WHERE snapshot_rank > 1
            )
            """
        )
    )
    with op.batch_alter_table("knowledge_chunk") as batch:
        batch.drop_constraint("uq_knowledge_chunk_position", type_="unique")
        batch.create_unique_constraint(
            "uq_knowledge_chunk_position",
            ["document_id", "chunk_index"],
        )
        batch.drop_column("index_identity")
