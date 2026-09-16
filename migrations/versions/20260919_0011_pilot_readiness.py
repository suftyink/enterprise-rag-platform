"""add immutable pilot readiness reports

Revision ID: 20260919_0011
Revises: 20260918_0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260919_0011"
down_revision: str | None = "20260918_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pilot_readiness_report",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("report_version", sa.String(32), nullable=False),
        sa.Column("release_version", sa.String(128), nullable=False),
        sa.Column("release_fingerprint", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_pilot_readiness_report_release_fingerprint",
        "pilot_readiness_report",
        ["release_fingerprint"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pilot_readiness_report_release_fingerprint",
        table_name="pilot_readiness_report",
    )
    op.drop_table("pilot_readiness_report")
