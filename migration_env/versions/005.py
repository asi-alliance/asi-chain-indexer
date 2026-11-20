"""Return empty network metric buckets instead of failing when no data exists."""

from src.migration_support import apply_sql

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade():
    apply_sql("005_network_metrics_empty_range.sql")


def downgrade():
    raise NotImplementedError(
        "These history-preserving migrations are forward-only; restore a backup."
    )
