"""Adopt the existing SQL migration 003_native_transfer_calls.sql."""

from src.migration_support import apply_sql

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade():
    apply_sql("003_native_transfer_calls.sql")


def downgrade():
    raise NotImplementedError(
        "These history-preserving migrations are forward-only; restore a backup."
    )
