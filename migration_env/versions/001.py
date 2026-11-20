"""Adopt the existing SQL migration 001_bigint_amounts.sql."""

from src.migration_support import apply_sql

revision = "001"
down_revision = "000"
branch_labels = None
depends_on = None


def upgrade():
    apply_sql("001_bigint_amounts.sql")


def downgrade():
    raise NotImplementedError(
        "These history-preserving migrations are forward-only; restore a backup."
    )
