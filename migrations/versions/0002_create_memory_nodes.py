"""Memory nodes with immutable identity, ownership and original L0 content."""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE memory_nodes (
            id UUID PRIMARY KEY,
            user_id TEXT NOT NULL CHECK (btrim(user_id) <> ''),
            namespace TEXT NOT NULL CHECK (btrim(namespace) <> ''),
            agent_id TEXT NOT NULL CHECK (btrim(agent_id) <> ''),
            scope TEXT NOT NULL CHECK (scope IN ('user_global','domain_shared','agent_private')),
            node_kind TEXT NOT NULL CHECK (node_kind IN ('atomic','derived')),
            level SMALLINT NOT NULL CHECK (level BETWEEN 0 AND 3),
            memory_type TEXT NOT NULL CHECK (btrim(memory_type) <> ''),
            domain TEXT NOT NULL CHECK (btrim(domain) <> ''),
            content TEXT NOT NULL CHECK (btrim(content) <> ''),
            context TEXT NOT NULL DEFAULT '',
            attributes JSONB NOT NULL DEFAULT '{}',
            domain_payload JSONB NOT NULL DEFAULT '{}',
            confidence DOUBLE PRECISION CHECK (confidence BETWEEN 0 AND 1),
            importance DOUBLE PRECISION CHECK (importance BETWEEN 0 AND 1),
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            occurred_at TIMESTAMPTZ,
            valid_from TIMESTAMPTZ,
            valid_until TIMESTAMPTZ,
            last_confirmed_at TIMESTAMPTZ,
            expires_at TIMESTAMPTZ,
            lifecycle_status TEXT NOT NULL DEFAULT 'active'
                CHECK (lifecycle_status IN ('active','stale','archived','deleted')),
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            source_ref JSONB NOT NULL DEFAULT '{}',
            embedding_status TEXT NOT NULL DEFAULT 'pending'
                CHECK (embedding_status IN ('pending','ready','failed')),
            idempotency_key TEXT,
            request_hash TEXT,
            UNIQUE (user_id, namespace, id),
            CONSTRAINT memory_request_unique UNIQUE (user_id, namespace, agent_id, idempotency_key),
            CHECK ((level = 0 AND node_kind = 'atomic') OR (level > 0 AND node_kind = 'derived')),
            CHECK (valid_until IS NULL OR valid_from IS NULL OR valid_until >= valid_from)
        )
    """)
    op.execute("CREATE INDEX memory_nodes_access ON memory_nodes(user_id,namespace,scope,agent_id)")
    op.execute("""
        CREATE FUNCTION protect_memory_identity() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF ROW(NEW.id,NEW.user_id,NEW.namespace,NEW.agent_id,
                   NEW.scope,NEW.domain,NEW.level,NEW.node_kind)
               IS DISTINCT FROM ROW(OLD.id,OLD.user_id,OLD.namespace,OLD.agent_id,
                                    OLD.scope,OLD.domain,OLD.level,OLD.node_kind)
            THEN RAISE EXCEPTION 'memory identity and access scope are immutable'; END IF;
            IF OLD.level = 0 AND ROW(NEW.content,NEW.source_ref,NEW.domain_payload,
                                    NEW.occurred_at,NEW.version)
               IS DISTINCT FROM ROW(OLD.content,OLD.source_ref,OLD.domain_payload,
                                    OLD.occurred_at,OLD.version)
            THEN RAISE EXCEPTION 'original atomic facts are immutable'; END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER memory_identity_immutable BEFORE UPDATE ON memory_nodes "
        "FOR EACH ROW EXECUTE FUNCTION protect_memory_identity()"
    )


def downgrade():
    raise RuntimeError("Node removal would delete memory data; use a reviewed migration")
