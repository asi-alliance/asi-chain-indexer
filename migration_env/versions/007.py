"""Drop the duplicate transfer address indexes."""

from src.migration_support import apply_sql

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade():
    apply_sql("007_transfer_index_names.sql")


def downgrade():
    raise NotImplementedError(
        "These history-preserving migrations are forward-only; restore a backup."
    )
