"""add governed corpus releases and cohort rollouts

Revision ID: 20260921_0013
Revises: 20260920_0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260921_0013"
down_revision: str | None = "20260920_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "corpus_release",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("release_version", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("data_classification", sa.String(32), nullable=False),
        sa.Column("corpus_generation", sa.Integer(), nullable=False),
        sa.Column("corpus_hash", sa.String(64), nullable=False),
        sa.Column("document_count", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("index_identity", sa.String(255), nullable=False),
        sa.Column("index_build_hash", sa.String(64), nullable=False),
        sa.Column("parser_version", sa.String(128), nullable=False),
        sa.Column("chunk_version", sa.String(128), nullable=False),
        sa.Column("configuration_fingerprint", sa.String(64), nullable=False),
        sa.Column("release_fingerprint", sa.String(64)),
        sa.Column("dataset_version", sa.String(128)),
        sa.Column("dataset_hash", sa.String(64)),
        sa.Column("route_fingerprints", sa.JSON(), nullable=False),
        sa.Column("answer_route_fingerprints", sa.JSON(), nullable=False),
        sa.Column("readiness_report_id", sa.Uuid()),
        sa.Column("previous_release_id", sa.Uuid()),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("approved_by", sa.String(128)),
        sa.Column("approval_reason", sa.Text()),
        sa.Column("pilot_scope", sa.String(255)),
        sa.Column("capacity", sa.String(255)),
        sa.Column("monitoring_owner", sa.String(128)),
        sa.Column("rollback_owner", sa.String(128)),
        sa.Column("degraded_reason", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["profile_id"], ["retrieval_profile.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["readiness_report_id"], ["pilot_readiness_report.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["previous_release_id"], ["corpus_release.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("release_version", name="uq_corpus_release_version"),
    )
    op.create_index(
        "ix_corpus_release_release_fingerprint",
        "corpus_release",
        ["release_fingerprint"],
    )
    op.create_index(
        "uq_corpus_release_active_classification",
        "corpus_release",
        ["data_classification"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
        sqlite_where=sa.text("status = 'ACTIVE'"),
    )
    op.create_table(
        "corpus_release_document",
        sa.Column("release_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_sha256", sa.String(64), nullable=False),
        sa.Column("governance_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["release_id"], ["corpus_release.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_id"], ["knowledge_document.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("release_id", "document_id"),
    )
    op.create_table(
        "pilot_rollout_record",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("release_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("subject_ids", sa.JSON(), nullable=False),
        sa.Column("approved_by", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["release_id"], ["corpus_release.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_pilot_rollout_record_release_id", "pilot_rollout_record", ["release_id"]
    )
    op.create_table(
        "corpus_release_audit",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("release_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["release_id"], ["corpus_release.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_corpus_release_audit_release_id", "corpus_release_audit", ["release_id"]
    )
    op.create_index(
        "ix_corpus_release_audit_trace_id", "corpus_release_audit", ["trace_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_corpus_release_audit_trace_id", table_name="corpus_release_audit")
    op.drop_index("ix_corpus_release_audit_release_id", table_name="corpus_release_audit")
    op.drop_table("corpus_release_audit")
    op.drop_index("ix_pilot_rollout_record_release_id", table_name="pilot_rollout_record")
    op.drop_table("pilot_rollout_record")
    op.drop_table("corpus_release_document")
    op.drop_index(
        "uq_corpus_release_active_classification", table_name="corpus_release"
    )
    op.drop_index("ix_corpus_release_release_fingerprint", table_name="corpus_release")
    op.drop_table("corpus_release")
