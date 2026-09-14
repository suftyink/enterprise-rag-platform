"""Create initial task tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260802_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_task",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("trace_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("operator_id", sa.String(length=64), nullable=False),
        sa.Column("department_id", sa.String(length=64), nullable=True),
        sa.Column("store_ids", sa.JSON(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trace_id"),
    )
    op.create_index("ix_ai_task_tenant_status", "ai_task", ["tenant_id", "status"])
    op.create_table(
        "ai_task_message",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["ai_task.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_task_message_task", "ai_task_message", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_ai_task_message_task", table_name="ai_task_message")
    op.drop_table("ai_task_message")
    op.drop_index("ix_ai_task_tenant_status", table_name="ai_task")
    op.drop_table("ai_task")

