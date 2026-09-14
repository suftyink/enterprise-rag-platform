"""Create knowledge document, chunk and indexing job tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260803_0002"
down_revision: str | None = "20260802_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_document",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("scope_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("document_key", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("parsed_object_key", sa.String(length=512), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("parse_warnings", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
        sa.UniqueConstraint("scope", "scope_id", "sha256", name="uq_knowledge_document_hash"),
        sa.UniqueConstraint(
            "scope",
            "scope_id",
            "document_key",
            "version",
            name="uq_knowledge_document_version",
        ),
    )
    op.create_index(
        "ix_knowledge_document_scope_status",
        "knowledge_document",
        ["scope", "scope_id", "status"],
    )
    op.create_index(
        "ix_knowledge_document_key_active",
        "knowledge_document",
        ["scope", "scope_id", "document_key", "active"],
    )
    op.create_table(
        "knowledge_chunk",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("scope_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.Column("section_title", sa.String(length=255), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("embedding_model", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_document.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_knowledge_chunk_position"),
    )
    op.create_index("ix_knowledge_chunk_scope", "knowledge_chunk", ["scope", "scope_id"])
    op.create_index("ix_knowledge_chunk_document", "knowledge_chunk", ["document_id"])
    op.create_table(
        "knowledge_index_job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("celery_task_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_document.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_index_job_document",
        "knowledge_index_job",
        ["document_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_index_job_document", table_name="knowledge_index_job")
    op.drop_table("knowledge_index_job")
    op.drop_index("ix_knowledge_chunk_document", table_name="knowledge_chunk")
    op.drop_index("ix_knowledge_chunk_scope", table_name="knowledge_chunk")
    op.drop_table("knowledge_chunk")
    op.drop_index("ix_knowledge_document_key_active", table_name="knowledge_document")
    op.drop_index("ix_knowledge_document_scope_status", table_name="knowledge_document")
    op.drop_table("knowledge_document")
