"""add append-only pilot access approvals

Revision ID: 20260920_0012
Revises: 20260919_0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260920_0012"
down_revision: str | None = "20260919_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pilot_access_record",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("readiness_report_id", sa.Uuid(), nullable=False),
        sa.Column("release_fingerprint", sa.String(64), nullable=False),
        sa.Column("approved_by", sa.String(128), nullable=False),
        sa.Column("pilot_scope", sa.String(255), nullable=False),
        sa.Column("capacity", sa.String(255), nullable=False),
        sa.Column("monitoring_owner", sa.String(128), nullable=False),
        sa.Column("rollback_owner", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["readiness_report_id"], ["pilot_readiness_report.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_pilot_access_record_release_fingerprint",
        "pilot_access_record",
        ["release_fingerprint"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pilot_access_record_release_fingerprint", table_name="pilot_access_record"
    )
    op.drop_table("pilot_access_record")
