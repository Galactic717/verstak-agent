"""Persist the retry deadline separately from the dispatch lease."""

import sqlalchemy as sa

from alembic import op

revision = "0003_retry_gate"
down_revision = "0002_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workflow_runs", sa.Column("retry_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("workflow_runs", "retry_at")
