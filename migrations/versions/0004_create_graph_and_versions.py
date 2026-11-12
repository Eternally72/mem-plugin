"""Tenant-safe adjacent DAG, independent semantic graph and version provenance."""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE memory_hierarchy_edges (
            user_id TEXT NOT NULL, namespace TEXT NOT NULL,
            child_id UUID NOT NULL, parent_id UUID NOT NULL,
            relation_type TEXT NOT NULL DEFAULT 'member_of'
                CHECK (relation_type IN ('member_of','contributes_to')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id,namespace,child_id,parent_id,relation_type),
            FOREIGN KEY (user_id,namespace,child_id) REFERENCES memory_nodes(user_id,namespace,id),
            FOREIGN KEY (user_id,namespace,parent_id) REFERENCES memory_nodes(user_id,namespace,id)
        )
    """)
    op.execute("""
        CREATE FUNCTION check_memory_hierarchy() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE child memory_nodes%ROWTYPE; parent memory_nodes%ROWTYPE;
        BEGIN
            SELECT * INTO child FROM memory_nodes
                WHERE id=NEW.child_id AND user_id=NEW.user_id AND namespace=NEW.namespace;
            SELECT * INTO parent FROM memory_nodes
                WHERE id=NEW.parent_id AND user_id=NEW.user_id AND namespace=NEW.namespace;
            IF child.id IS NULL OR parent.id IS NULL OR parent.level <> child.level+1
            THEN RAISE EXCEPTION 'hierarchy requires adjacent ascending levels'; END IF;
            IF child.scope='agent_private' AND
                (parent.scope<>'agent_private' OR child.agent_id<>parent.agent_id)
            THEN RAISE EXCEPTION 'hierarchy cannot widen private visibility'; END IF;
            IF child.scope='domain_shared' AND
                (parent.scope='user_global' OR
                (parent.scope='domain_shared' AND parent.domain<>child.domain))
            THEN RAISE EXCEPTION 'hierarchy cannot widen domain visibility'; END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER hierarchy_validate BEFORE INSERT OR UPDATE ON memory_hierarchy_edges "
        "FOR EACH ROW EXECUTE FUNCTION check_memory_hierarchy()"
    )
    op.execute("""
        CREATE TABLE memory_semantic_edges (
            user_id TEXT NOT NULL, namespace TEXT NOT NULL,
            source_id UUID NOT NULL, target_id UUID NOT NULL,
            relation_type TEXT NOT NULL CHECK (relation_type IN
                ('semantic','same_entity','temporal','causal','contradicts','supersedes','related_event')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id,namespace,source_id,target_id,relation_type),
            FOREIGN KEY (user_id,namespace,source_id) REFERENCES memory_nodes(user_id,namespace,id),
            FOREIGN KEY (user_id,namespace,target_id) REFERENCES memory_nodes(user_id,namespace,id)
        )
    """)
    op.execute("""
        CREATE TABLE memory_versions (
            user_id TEXT NOT NULL, namespace TEXT NOT NULL, node_id UUID NOT NULL,
            version INTEGER NOT NULL CHECK (version>0), content TEXT NOT NULL,
            reason TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id,namespace,node_id,version),
            FOREIGN KEY (user_id,namespace,node_id) REFERENCES memory_nodes(user_id,namespace,id)
        )
    """)
    op.execute("""
        CREATE TABLE memory_version_sources (
            user_id TEXT NOT NULL, namespace TEXT NOT NULL, node_id UUID NOT NULL,
            version INTEGER NOT NULL, source_id UUID NOT NULL, source_version INTEGER NOT NULL,
            PRIMARY KEY (user_id,namespace,node_id,version,source_id),
            FOREIGN KEY (user_id,namespace,node_id,version)
                REFERENCES memory_versions(user_id,namespace,node_id,version),
            FOREIGN KEY (user_id,namespace,source_id) REFERENCES memory_nodes(user_id,namespace,id)
        )
    """)


def downgrade():
    raise RuntimeError("Graph and provenance removal requires a reviewed migration")
