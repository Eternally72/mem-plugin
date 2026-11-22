"""Store scoped MCP credential digests and their stable memory namespaces."""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE mcp_credentials (
            label text PRIMARY KEY,
            namespace text NOT NULL UNIQUE,
            token_digest bytea NOT NULL UNIQUE CHECK (octet_length(token_digest) = 32),
            expires_at timestamptz NOT NULL,
            revoked_at timestamptz
        )
    """)


def downgrade():
    op.drop_table("mcp_credentials")
