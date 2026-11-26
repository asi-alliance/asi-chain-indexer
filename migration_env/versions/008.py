"""Add deployment actions, grouped heights and public search indexes."""

from src.migration_support import apply_sql

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade():
    apply_sql("008_public_aggregates_search_and_actions.sql")


def downgrade():
    raise NotImplementedError(
        "These history-preserving migrations are forward-only; restore a backup."
    )
