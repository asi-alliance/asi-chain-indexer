"""Adopt the existing SQL migration 002_executed_transfers.sql."""

from src.migration_support import apply_sql

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade():
    apply_sql("002_executed_transfers.sql")


def downgrade():
    raise NotImplementedError(
        "These history-preserving migrations are forward-only; restore a backup."
    )
