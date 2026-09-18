"""add recoverable knowledge initialization runs and physical index identity

Revision ID: 20260923_0015
Revises: 20260922_0014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260923_0015"
down_revision: str | None = "20260922_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("retrieval_profile", sa.Column("physical_index_locator", sa.String(255)))
    op.add_column("retrieval_profile", sa.Column("index_schema_fingerprint", sa.String(64)))
    op.add_column(
        "retrieval_profile", sa.Column("embedding_binding_fingerprint", sa.String(64))
    )
    op.create_table(
        "knowledge_initialization_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("data_classification", sa.String(32), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("runtime_key", sa.String(128)),
        sa.Column("configuration_fingerprint", sa.String(64)),
        sa.Column("profile_id", sa.Uuid()),
        sa.Column("candidate_release_id", sa.Uuid()),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.Uuid(), nullable=False),
        sa.Column("failure_code", sa.String(128)),
        sa.Column("failure_message", sa.String(500)),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(128)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["retrieval_profile.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["candidate_release_id"], ["corpus_release.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "data_classification",
            "idempotency_key",
            name="uq_knowledge_initialization_idempotency",
        ),
    )
    op.create_index(
        "ix_knowledge_initialization_run_trace_id",
        "knowledge_initialization_run",
        ["trace_id"],
    )
    op.create_index(
        "uq_knowledge_initialization_nonterminal",
        "knowledge_initialization_run",
        ["data_classification"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING', 'WAITING')"),
        sqlite_where=sa.text("status IN ('QUEUED', 'RUNNING', 'WAITING')"),
    )
    op.create_table(
        "knowledge_initialization_audit",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["knowledge_initialization_run.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_initialization_audit_run_id",
        "knowledge_initialization_audit",
        ["run_id"],
    )
    op.create_index(
        "ix_knowledge_initialization_audit_trace_id",
        "knowledge_initialization_audit",
        ["trace_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_knowledge_initialization_audit_trace_id",
        table_name="knowledge_initialization_audit",
    )
    op.drop_index(
        "ix_knowledge_initialization_audit_run_id",
        table_name="knowledge_initialization_audit",
    )
    op.drop_table("knowledge_initialization_audit")
    op.drop_index(
        "uq_knowledge_initialization_nonterminal",
        table_name="knowledge_initialization_run",
    )
    op.drop_index(
        "ix_knowledge_initialization_run_trace_id",
        table_name="knowledge_initialization_run",
    )
    op.drop_table("knowledge_initialization_run")
    op.drop_column("retrieval_profile", "embedding_binding_fingerprint")
    op.drop_column("retrieval_profile", "index_schema_fingerprint")
    op.drop_column("retrieval_profile", "physical_index_locator")
