"""separate processing and publication lifecycle

Revision ID: 20260915_0004
Revises: 20260915_0003
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "20260915_0004"
down_revision: str | None = "20260915_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_document",
        sa.Column(
            "publication_status",
            sa.String(32),
            nullable=False,
            server_default="UNPUBLISHED",
        ),
    )
    op.add_column(
        "knowledge_document",
        sa.Column(
            "retention_status",
            sa.String(32),
            nullable=False,
            server_default="RETAINED",
        ),
    )
    op.add_column(
        "knowledge_document",
        sa.Column("governance_revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "knowledge_document",
        sa.Column(
            "index_artifacts_present",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "knowledge_document",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE knowledge_document
            SET publication_status = CASE
                    WHEN active = true AND status = 'READY' THEN 'PUBLISHED'
                    WHEN status = 'SUPERSEDED' THEN 'SUPERSEDED'
                    WHEN status = 'READY' THEN 'REVIEW_PENDING'
                    ELSE 'UNPUBLISHED'
                END,
                index_artifacts_present = CASE
                    WHEN status IN ('READY', 'SUPERSEDED') THEN true
                    ELSE false
                END,
                published_at = CASE
                    WHEN active = true AND status = 'READY'
                    THEN COALESCE(indexed_at, updated_at)
                    ELSE NULL
                END,
                status = CASE WHEN status = 'SUPERSEDED' THEN 'READY' ELSE status END
            """
        )
    )
    with op.batch_alter_table("knowledge_document") as batch:
        batch.drop_constraint("uq_knowledge_document_hash", type_="unique")
        batch.create_unique_constraint(
            "uq_knowledge_document_hash",
            ["scope", "scope_id", "document_key", "sha256"],
        )
    op.drop_index("ix_knowledge_document_key_active", table_name="knowledge_document")
    op.create_index(
        "ix_knowledge_document_key_active",
        "knowledge_document",
        ["scope", "scope_id", "document_key"],
        unique=True,
        postgresql_where=sa.text("active"),
        sqlite_where=sa.text("active = 1"),
    )

    op.create_table(
        "knowledge_corpus_head",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.String(64), nullable=False),
        sa.Column("document_key", sa.String(128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("published_document_id", sa.Uuid(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["published_document_id"], ["knowledge_document.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope", "scope_id", "document_key", name="uq_knowledge_corpus_head"),
    )
    _seed_published_heads()
    op.create_table(
        "knowledge_lifecycle_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("document_version", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_document.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_lifecycle_event_document",
        "knowledge_lifecycle_event",
        ["document_id", "created_at"],
    )
    op.create_table(
        "knowledge_cleanup_request",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("artifacts", sa.JSON(), nullable=False),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_document.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_knowledge_cleanup_idempotency"),
    )
    op.create_index(
        "ix_knowledge_cleanup_document",
        "knowledge_cleanup_request",
        ["document_id", "created_at"],
    )


def _seed_published_heads() -> None:
    connection = op.get_bind()
    documents = sa.table(
        "knowledge_document",
        sa.column("id", sa.Uuid()),
        sa.column("scope", sa.String()),
        sa.column("scope_id", sa.String()),
        sa.column("document_key", sa.String()),
        sa.column("publication_status", sa.String()),
    )
    heads = sa.table(
        "knowledge_corpus_head",
        sa.column("id", sa.Uuid()),
        sa.column("scope", sa.String()),
        sa.column("scope_id", sa.String()),
        sa.column("document_key", sa.String()),
        sa.column("revision", sa.Integer()),
        sa.column("published_document_id", sa.Uuid()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    rows = connection.execute(
        sa.select(
            documents.c.id,
            documents.c.scope,
            documents.c.scope_id,
            documents.c.document_key,
        ).where(documents.c.publication_status == "PUBLISHED")
    ).all()
    now = datetime.now(UTC)
    if rows:
        connection.execute(
            heads.insert(),
            [
                {
                    "id": uuid4(),
                    "scope": row.scope,
                    "scope_id": row.scope_id,
                    "document_key": row.document_key,
                    "revision": 0,
                    "published_document_id": row.id,
                    "updated_at": now,
                }
                for row in rows
            ],
        )


def downgrade() -> None:
    op.drop_index("ix_knowledge_cleanup_document", table_name="knowledge_cleanup_request")
    op.drop_table("knowledge_cleanup_request")
    op.drop_index("ix_knowledge_lifecycle_event_document", table_name="knowledge_lifecycle_event")
    op.drop_table("knowledge_lifecycle_event")
    op.drop_table("knowledge_corpus_head")
    op.drop_index("ix_knowledge_document_key_active", table_name="knowledge_document")
    op.create_index(
        "ix_knowledge_document_key_active",
        "knowledge_document",
        ["scope", "scope_id", "document_key", "active"],
    )
    op.execute(
        sa.text(
            """
            UPDATE knowledge_document
            SET status = 'SUPERSEDED'
            WHERE publication_status = 'SUPERSEDED'
            """
        )
    )
    with op.batch_alter_table("knowledge_document") as batch:
        batch.drop_constraint("uq_knowledge_document_hash", type_="unique")
        batch.create_unique_constraint(
            "uq_knowledge_document_hash", ["scope", "scope_id", "sha256"]
        )
    op.drop_column("knowledge_document", "published_at")
    op.drop_column("knowledge_document", "index_artifacts_present")
    op.drop_column("knowledge_document", "governance_revision")
    op.drop_column("knowledge_document", "retention_status")
    op.drop_column("knowledge_document", "publication_status")
