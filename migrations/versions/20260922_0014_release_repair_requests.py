"""record failed release builds and queued repair candidates

Revision ID: 20260922_0014
Revises: 20260921_0013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260922_0014"
down_revision: str | None = "20260921_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "corpus_release", sa.Column("failure_reason_code", sa.String(128))
    )
    op.create_table(
        "corpus_release_repair_request",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("release_id", sa.Uuid(), nullable=False),
        sa.Column("excluded_document_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("trace_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["release_id"], ["corpus_release.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["excluded_document_id"], ["knowledge_document.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "release_id",
            "excluded_document_id",
            name="uq_release_repair_excluded_document",
        ),
    )
    op.create_index(
        "ix_corpus_release_repair_request_release_id",
        "corpus_release_repair_request",
        ["release_id"],
    )
    op.create_index(
        "ix_corpus_release_repair_request_trace_id",
        "corpus_release_repair_request",
        ["trace_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_corpus_release_repair_request_trace_id",
        table_name="corpus_release_repair_request",
    )
    op.drop_index(
        "ix_corpus_release_repair_request_release_id",
        table_name="corpus_release_repair_request",
    )
    op.drop_table("corpus_release_repair_request")
    op.drop_column("corpus_release", "failure_reason_code")
