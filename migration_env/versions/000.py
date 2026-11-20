"""Adopt the existing SQL migration 000_comprehensive_initial_schema.sql."""

from src.migration_support import apply_sql

revision = "000"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    apply_sql("000_comprehensive_initial_schema.sql")


def downgrade():
    raise NotImplementedError(
        "These history-preserving migrations are forward-only; restore a backup."
    )
