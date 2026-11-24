"""Persist evidence reviews and cross-process trace context."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0004_verification_and_tracing"
down_revision = "0003_retry_gate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workflow_runs", sa.Column("verification", JSONB()))
    op.add_column("workflow_runs", sa.Column("traceparent", sa.String(55)))
    op.add_column("model_attempts", sa.Column("proposal", JSONB()))
    op.add_column("model_attempts", sa.Column("verification", JSONB()))


def downgrade() -> None:
    op.drop_column("model_attempts", "verification")
    op.drop_column("model_attempts", "proposal")
    op.drop_column("workflow_runs", "traceparent")
    op.drop_column("workflow_runs", "verification")
