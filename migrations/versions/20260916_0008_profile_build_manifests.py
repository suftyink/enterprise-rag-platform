"""track vector artifacts and staging build manifests

Revision ID: 20260916_0008
Revises: 20260916_0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260916_0008"
down_revision: str | None = "20260916_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_corpus_generation",
        sa.Column("data_classification", sa.String(32), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("data_classification"),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO knowledge_corpus_generation (
                data_classification,
                generation,
                updated_at
            ) VALUES
                ('UNCLASSIFIED', 0, CURRENT_TIMESTAMP),
                ('ORDINARY', 0, CURRENT_TIMESTAMP),
                ('RESTRICTED', 0, CURRENT_TIMESTAMP)
            """
        )
    )
    op.create_table(
        "knowledge_vector_artifact",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("index_identity", sa.String(255), nullable=False),
        sa.Column("vector_document_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"], ["knowledge_document.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("document_id", "index_identity"),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO knowledge_vector_artifact (
                document_id,
                index_identity,
                vector_document_id,
                created_at,
                updated_at
            )
            SELECT d.id,
                   COALESCE(
                       (
                           SELECT c.index_identity
                           FROM knowledge_chunk AS c
                           WHERE c.document_id = d.id
                           ORDER BY c.created_at DESC, c.id DESC
                           LIMIT 1
                       ),
                       'legacy'
                   ),
                   d.vector_document_id,
                   CURRENT_TIMESTAMP,
                   CURRENT_TIMESTAMP
            FROM knowledge_document AS d
            WHERE d.vector_document_id IS NOT NULL
            """
        )
    )
    op.add_column("retrieval_profile", sa.Column("index_build_hash", sa.String(64)))
    op.add_column(
        "retrieval_profile", sa.Column("index_build_corpus_hash", sa.String(64))
    )
    op.add_column(
        "retrieval_profile", sa.Column("index_build_document_count", sa.Integer())
    )
    op.add_column(
        "retrieval_profile",
        sa.Column("index_build_completed_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_column("retrieval_profile", "index_build_completed_at")
    op.drop_column("retrieval_profile", "index_build_document_count")
    op.drop_column("retrieval_profile", "index_build_corpus_hash")
    op.drop_column("retrieval_profile", "index_build_hash")
    op.drop_table("knowledge_vector_artifact")
    op.drop_table("knowledge_corpus_generation")
