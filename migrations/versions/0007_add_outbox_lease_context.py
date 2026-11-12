"""Lease fencing and a trusted access-context snapshot for worker execution."""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE outbox_events ADD COLUMN agent_id text")
    op.execute(
        "ALTER TABLE outbox_events ADD COLUMN authorized_domains jsonb NOT NULL DEFAULT '[]'"
    )
    op.execute("ALTER TABLE outbox_events ADD COLUMN lease_token uuid")
    op.execute("""UPDATE outbox_events e SET agent_id=n.agent_id,
        authorized_domains=CASE WHEN n.scope='domain_shared'
            THEN jsonb_build_array(n.domain) ELSE '[]'::jsonb END
        FROM memory_nodes n WHERE n.id=e.node_id
            AND n.user_id=e.user_id AND n.namespace=e.namespace""")
    op.execute("ALTER TABLE outbox_events ALTER COLUMN agent_id SET NOT NULL")


def downgrade():
    raise RuntimeError(
        "Stopping all workers and reviewing pending jobs is required before rollback"
    )
