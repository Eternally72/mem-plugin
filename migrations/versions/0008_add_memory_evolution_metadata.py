"""Provenance for controlled context evolution, dirty state and task application."""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE memory_nodes ADD COLUMN context_revision integer NOT NULL DEFAULT 0 "
        "CHECK (context_revision>=0)"
    )
    op.execute("ALTER TABLE memory_nodes ADD COLUMN is_dirty boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE outbox_events ADD COLUMN applied_at timestamptz")
    op.execute("UPDATE outbox_events SET applied_at=completed_at WHERE status='completed'")
    op.execute(
        "ALTER TABLE memory_semantic_edges ADD COLUMN confidence double precision "
        "CHECK (confidence BETWEEN 0 AND 1)"
    )
    op.execute(
        "ALTER TABLE memory_semantic_edges ADD COLUMN context_note text NOT NULL DEFAULT '' "
        "CHECK (length(context_note)<=300)"
    )
    op.execute("ALTER TABLE memory_semantic_edges ADD COLUMN created_by_event uuid")
    op.execute("""ALTER TABLE memory_semantic_edges ADD CONSTRAINT automatic_edge_safe
        CHECK (created_by_event IS NULL OR (source_id<>target_id
          AND relation_type<>'supersedes' AND confidence IS NOT NULL AND context_note<>''))""")
    op.execute(
        "CREATE INDEX memory_semantic_target ON memory_semantic_edges(user_id,namespace,target_id)"
    )


def downgrade():
    raise RuntimeError("Evolution provenance must be reviewed before rollback")
