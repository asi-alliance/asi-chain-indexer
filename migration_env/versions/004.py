"""Track the shipped uint256 fee-price widening without changing historical units."""

from src.migration_support import apply_sql

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade():
    apply_sql("004_uint256_phlo_prices.sql")


def downgrade():
    raise NotImplementedError(
        "These history-preserving migrations are forward-only; restore a backup."
    )
