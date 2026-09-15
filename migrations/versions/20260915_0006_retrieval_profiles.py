"""add governed retrieval profiles

Revision ID: 20260915_0006
Revises: 20260915_0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260915_0006"
down_revision: str | None = "20260915_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_document",
        sa.Column(
            "data_classification",
            sa.String(32),
            nullable=False,
            server_default="UNCLASSIFIED",
        ),
    )
    op.add_column(
        "knowledge_chunk",
        sa.Column("embedding_dimension", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "knowledge_chunk",
        sa.Column("retrieval_profile", sa.String(128), nullable=False, server_default="legacy"),
    )
    op.add_column("knowledge_index_job", sa.Column("embedding_deployment", sa.String(128)))
    op.add_column("knowledge_index_job", sa.Column("embedding_dimension", sa.Integer()))
    op.add_column("knowledge_index_job", sa.Column("retrieval_profile", sa.String(128)))
    op.create_table(
        "retrieval_profile",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("profile_key", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("data_classification", sa.String(32), nullable=False),
        sa.Column("provider_boundary", sa.String(32), nullable=False),
        sa.Column("embedding_deployment", sa.String(128), nullable=False),
        sa.Column("embedding_model", sa.String(128), nullable=False),
        sa.Column("embedding_dimension", sa.Integer(), nullable=False),
        sa.Column("rerank_deployment", sa.String(128), nullable=False),
        sa.Column("rerank_model", sa.String(128), nullable=False),
        sa.Column("index_identity", sa.String(255), nullable=False),
        sa.Column("parser_version", sa.String(128), nullable=False),
        sa.Column("chunk_version", sa.String(128), nullable=False),
        sa.Column("retrieval_config", sa.JSON(), nullable=False),
        sa.Column("configuration_fingerprint", sa.String(64), nullable=False),
        sa.Column("production_ready", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="STAGING"),
        sa.Column("dataset_hash", sa.String(64)),
        sa.Column("comparison_key", sa.String(64)),
        sa.Column("previous_profile_id", sa.Uuid()),
        sa.Column("evaluated_at", sa.DateTime(timezone=True)),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["previous_profile_id"], ["retrieval_profile.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("profile_key", "version", name="uq_retrieval_profile_version"),
    )
    op.create_index(
        "ix_retrieval_profile_classification_status",
        "retrieval_profile",
        ["data_classification", "status"],
    )
    op.create_index(
        "uq_retrieval_profile_active_classification",
        "retrieval_profile",
        ["data_classification"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
        sqlite_where=sa.text("status = 'ACTIVE'"),
    )


def downgrade() -> None:
    op.drop_index("uq_retrieval_profile_active_classification", table_name="retrieval_profile")
    op.drop_index("ix_retrieval_profile_classification_status", table_name="retrieval_profile")
    op.drop_table("retrieval_profile")
    op.drop_column("knowledge_index_job", "retrieval_profile")
    op.drop_column("knowledge_index_job", "embedding_dimension")
    op.drop_column("knowledge_index_job", "embedding_deployment")
    op.drop_column("knowledge_chunk", "retrieval_profile")
    op.drop_column("knowledge_chunk", "embedding_dimension")
    op.drop_column("knowledge_document", "data_classification")
