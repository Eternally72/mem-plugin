"""Native FTS and trigram indexes for bounded lexical retrieval."""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""ALTER TABLE memory_nodes ADD COLUMN search_vector tsvector
        GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, content)) STORED""")
    op.execute("CREATE INDEX memory_nodes_fts ON memory_nodes USING gin(search_vector)")
    op.execute("""CREATE INDEX memory_nodes_trgm ON memory_nodes
        USING gin(lower(content) gin_trgm_ops)""")


def downgrade():
    op.execute("DROP INDEX memory_nodes_trgm")
    op.execute("DROP INDEX memory_nodes_fts")
    op.execute("ALTER TABLE memory_nodes DROP COLUMN search_vector")
