"""Create the durable run, action, budget and evaluation tables."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0001_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant", sa.String(100), nullable=False),
        sa.Column("submitted_by", sa.String(200), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("input_text", sa.Text(), nullable=False),
        sa.Column("document_id", sa.Uuid()),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("max_steps", sa.Integer(), nullable=False),
        sa.Column("budget_micros", sa.BigInteger(), nullable=False),
        sa.Column("cost_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("worker_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("triage", JSONB()),
        sa.Column("approved", sa.Boolean()),
        sa.Column("approved_by", sa.String(200)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.String(80)),
        sa.Column(
            "dispatch_after",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant", "idempotency_key", name="uq_run_idempotency"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'waiting_approval', 'succeeded', 'rejected', 'failed')",
            name="ck_run_status",
        ),
    )
    op.create_index(
        "ix_runs_dispatch",
        "workflow_runs",
        ["dispatch_after"],
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index("ix_runs_tenant_created", "workflow_runs", ["tenant", "created_at"])
    op.create_table(
        "model_attempts",
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("workflow_runs.id"), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("reserved_micros", sa.BigInteger(), nullable=False),
        sa.Column("charged_micros", sa.BigInteger()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "case_actions",
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("workflow_runs.id"), primary_key=True),
        sa.Column("tenant", sa.String(100), nullable=False),
        sa.Column("tool", sa.String(32), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "dead_letters",
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("workflow_runs.id"), primary_key=True),
        sa.Column("error", sa.String(80), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "evaluations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant", sa.String(100), nullable=False),
        sa.Column("report", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_evaluations_tenant_created", "evaluations", ["tenant", "created_at"])


def downgrade() -> None:
    for table in ("evaluations", "dead_letters", "case_actions", "model_attempts", "workflow_runs"):
        op.drop_table(table)
