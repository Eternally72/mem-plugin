"""Independent validity/review metadata and recoverable archive manifests."""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE memory_nodes ADD COLUMN archived_status text "
        "CHECK (archived_status IN ('active','stale'))"
    )
    op.execute("ALTER TABLE memory_nodes ADD COLUMN needs_review boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE memory_nodes ADD COLUMN last_decay_at timestamptz")
    op.execute("ALTER TABLE memory_nodes ADD COLUMN restored_at timestamptz")
    op.execute(
        "ALTER TABLE memory_nodes ADD COLUMN lifecycle_revision integer NOT NULL DEFAULT 0 "
        "CHECK (lifecycle_revision>=0)"
    )
    op.execute("""CREATE TABLE memory_archive_manifests (
        user_id text NOT NULL,namespace text NOT NULL,node_id uuid NOT NULL,
        storage_key text NOT NULL UNIQUE CHECK(storage_key ~ '^[0-9a-f]{32}$'),
        operation_id uuid NOT NULL,checksum text NOT NULL,
        record_count integer NOT NULL CHECK(record_count>0),
        byte_count integer NOT NULL CHECK(byte_count>0),node_version integer NOT NULL,
        source_status text NOT NULL CHECK(source_status IN ('active','stale')),
        status text NOT NULL CHECK(status IN ('writing','complete','invalidated')),
        source_ids uuid[] NOT NULL DEFAULT '{}',
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(user_id,namespace,node_id),
        FOREIGN KEY(user_id,namespace,node_id) REFERENCES memory_nodes(user_id,namespace,id))""")
    op.execute("DROP INDEX memory_nodes_fts")
    op.execute("DROP INDEX memory_nodes_trgm")
    op.execute(
        "CREATE INDEX memory_nodes_fts ON memory_nodes USING gin(search_vector) "
        "WHERE lifecycle_status='active'"
    )
    op.execute(
        "CREATE INDEX memory_nodes_trgm ON memory_nodes USING gin(lower(content) gin_trgm_ops) "
        "WHERE lifecycle_status='active'"
    )
    op.execute(
        "CREATE INDEX memory_lifecycle_scan ON "
        "memory_nodes(user_id,namespace,lifecycle_status,valid_until,expires_at)"
    )


def downgrade():
    raise RuntimeError("Archive manifests and lifecycle state require a reviewed rollback")
