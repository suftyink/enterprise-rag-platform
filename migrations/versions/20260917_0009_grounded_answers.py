"""add safe answer traces and feedback

Revision ID: 20260917_0009
Revises: 20260916_0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260917_0009"
down_revision: str | None = "20260916_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_answer_run",
        sa.Column("trace_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("data_classification", sa.String(32), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(64)),
        sa.Column("model", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("trace_id"),
    )
    op.create_table(
        "knowledge_answer_feedback",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("trace_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("answer_decision", sa.String(32), nullable=False),
        sa.Column("rating", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["trace_id"], ["knowledge_answer_run.trace_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trace_id", "subject_id", name="uq_answer_feedback_actor"),
    )


def downgrade() -> None:
    op.drop_table("knowledge_answer_feedback")
    op.drop_table("knowledge_answer_run")
