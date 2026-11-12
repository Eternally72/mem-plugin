"""Durable execution records, atomic audit and producer-side outbox."""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE memory_traces (
            trace_id UUID PRIMARY KEY, user_id TEXT NOT NULL, namespace TEXT NOT NULL,
            agent_id TEXT NOT NULL, correlation_id UUID NOT NULL,
            operation TEXT NOT NULL, process_name TEXT NOT NULL DEFAULT 'engine',
            status TEXT NOT NULL CHECK (status IN ('running','success','failed')),
            started_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ
        )
    """)
    op.execute("""
        CREATE TABLE memory_trace_spans (
            span_id UUID PRIMARY KEY, trace_id UUID NOT NULL REFERENCES memory_traces(trace_id),
            parent_span_id UUID, operation TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('running','success','failed')),
            started_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ,
            duration_ms DOUBLE PRECISION, error_type TEXT,
            attributes JSONB NOT NULL DEFAULT '{}'
        )
    """)
    op.execute("CREATE INDEX memory_spans_trace ON memory_trace_spans(trace_id,parent_span_id)")
    op.execute("CREATE INDEX memory_traces_access ON memory_traces(user_id,namespace,started_at)")
    op.execute("""
        CREATE TABLE memory_audit_events (
            id UUID PRIMARY KEY, user_id TEXT NOT NULL, namespace TEXT NOT NULL,
            memory_id UUID NOT NULL, actor TEXT NOT NULL, actor_type TEXT NOT NULL DEFAULT 'agent',
            action TEXT NOT NULL, old_version INTEGER, new_version INTEGER,
            reason TEXT NOT NULL, trace_id UUID NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX memory_audit_access ON memory_audit_events(user_id,namespace,memory_id)"
    )
    op.execute("""
        CREATE TABLE outbox_events (
            id UUID PRIMARY KEY, user_id TEXT NOT NULL, namespace TEXT NOT NULL,
            node_id UUID NOT NULL, node_version INTEGER NOT NULL,
            event_type TEXT NOT NULL, payload JSONB NOT NULL,
            trace_id UUID NOT NULL, parent_span_id UUID NOT NULL, correlation_id UUID NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','running','completed','failed')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            locked_until TIMESTAMPTZ, locked_by TEXT,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            last_error TEXT, completed_at TIMESTAMPTZ,
            FOREIGN KEY (user_id,namespace,node_id) REFERENCES memory_nodes(user_id,namespace,id),
            UNIQUE (user_id,namespace,node_id,node_version,event_type)
        )
    """)
    op.execute("CREATE INDEX outbox_pending ON outbox_events(status,available_at)")


def downgrade():
    raise RuntimeError("Audit and task removal requires a reviewed retention plan")
