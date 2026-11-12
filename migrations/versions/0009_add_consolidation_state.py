"""Consolidation provenance and coalesced generations for derived refresh tasks."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE memory_nodes ADD COLUMN dirty_revision integer NOT NULL DEFAULT 0 "
        "CHECK (dirty_revision>=0)"
    )
    op.execute("UPDATE memory_nodes SET dirty_revision=1 WHERE is_dirty")
    op.execute("ALTER TABLE memory_nodes ADD COLUMN consolidation_key text")
    op.execute("ALTER TABLE memory_nodes ADD COLUMN consolidation_hash text")
    op.execute(
        "ALTER TABLE memory_versions ADD COLUMN confidence double precision "
        "CHECK (confidence BETWEEN 0 AND 1)"
    )
    op.execute(
        "ALTER TABLE memory_versions ADD COLUMN conflict_resolution text NOT NULL DEFAULT ''"
    )
    op.execute(
        "ALTER TABLE outbox_events ADD COLUMN generation integer NOT NULL DEFAULT 0 "
        "CHECK (generation>=0)"
    )
    for constraint in sa.inspect(op.get_bind()).get_unique_constraints("outbox_events"):
        if constraint["column_names"] == [
            "user_id",
            "namespace",
            "node_id",
            "node_version",
            "event_type",
        ]:
            op.drop_constraint(constraint["name"], "outbox_events", type_="unique")
    op.create_unique_constraint(
        "outbox_event_identity",
        "outbox_events",
        ["user_id", "namespace", "node_id", "node_version", "event_type", "generation"],
    )
    op.execute("CREATE INDEX memory_dirty_scan ON memory_nodes(user_id,namespace,level,is_dirty)")
    op.execute("""CREATE TABLE memory_forget_tombstones (
        user_id text NOT NULL,namespace text NOT NULL,memory_id uuid NOT NULL,
        forgotten_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(user_id,namespace,memory_id))""")
    op.execute(
        "CREATE INDEX memory_hierarchy_parent "
        "ON memory_hierarchy_edges(user_id,namespace,parent_id)"
    )


def downgrade():
    raise RuntimeError("Consolidation state and provenance require a reviewed rollback")
