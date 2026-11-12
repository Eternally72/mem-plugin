"""One fixed 1024-dimensional Qwen embedding space; cosine HNSW index."""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE memory_embeddings (
            user_id TEXT NOT NULL, namespace TEXT NOT NULL, node_id UUID PRIMARY KEY,
            node_version INTEGER NOT NULL CHECK (node_version>0),
            embedding_model TEXT NOT NULL CHECK (embedding_model='qwen3.7-text-embedding-flash'),
            embedding_dimension INTEGER NOT NULL CHECK (embedding_dimension=1024),
            content_hash TEXT NOT NULL, embedding vector(1024) NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            FOREIGN KEY (user_id,namespace,node_id) REFERENCES memory_nodes(user_id,namespace,id)
        )
    """)
    op.execute(
        "CREATE INDEX memory_embeddings_cosine ON memory_embeddings "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute("CREATE INDEX memory_embeddings_access ON memory_embeddings(user_id,namespace)")


def downgrade():
    raise RuntimeError("Embedding removal requires a reviewed rebuild plan")
