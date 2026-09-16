"""add immutable security audit events

Revision ID: 20260918_0010
Revises: 20260917_0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260918_0010"
down_revision: str | None = "20260917_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "security_audit_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.String(128)),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(128), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_security_audit_event_trace_id", "security_audit_event", ["trace_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_security_audit_event_trace_id", table_name="security_audit_event")
    op.drop_table("security_audit_event")

