"""Index the native transfer lifecycle view for deploy lookups."""

from src.migration_support import apply_sql

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade():
    apply_sql("006_native_transfer_lifecycle_plans.sql")


def downgrade():
    raise NotImplementedError(
        "These history-preserving migrations are forward-only; restore a backup."
    )
