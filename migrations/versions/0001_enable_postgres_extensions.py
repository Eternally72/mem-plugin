"""Enable extensions provided by the pinned local PostgreSQL image."""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector VERSION '0.8.6'")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")


def downgrade():
    raise RuntimeError("Extension removal requires a separate reviewed migration")
