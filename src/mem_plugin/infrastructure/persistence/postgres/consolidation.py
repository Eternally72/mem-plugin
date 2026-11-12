"""Scoped grouping membership and bounded scheduler scans; no consolidation rules."""

from sqlalchemy import text

from mem_plugin.engine.models import AccessContext, MemoryNode
from mem_plugin.infrastructure.persistence.postgres.repositories import VISIBLE, _node, parameters


class ConsolidationRepository:
    def __init__(self, connection, access: AccessContext):
        self.connection, self.access = connection, access

    def suppress(self, node_id):
        self.connection.execute(
            text("""INSERT INTO memory_forget_tombstones
            (user_id,namespace,memory_id) VALUES (:user_id,:namespace,:id)
            ON CONFLICT DO NOTHING"""),
            {**parameters(self.access), "id": node_id},
        )

    def suppressed(self, node_id):
        return bool(
            self.connection.execute(
                text("""SELECT EXISTS(
            SELECT 1 FROM memory_forget_tombstones WHERE user_id=:user_id
              AND namespace=:namespace AND memory_id=:id)"""),
                {**parameters(self.access), "id": node_id},
            ).scalar_one()
        )

    def members(self, node_id):
        rows = self.connection.execute(
            text(f"""SELECT * FROM memory_nodes WHERE {VISIBLE}
            AND id IN (SELECT child_id FROM memory_hierarchy_edges WHERE user_id=:user_id
                AND namespace=:namespace AND parent_id=:id) ORDER BY id"""),
            {**parameters(self.access), "id": node_id},
        ).mappings()
        return [_node(row) for row in rows]

    def groups(self, seed: MemoryNode, target_level, limit):
        rows = self.connection.execute(
            text(f"""SELECT * FROM memory_nodes WHERE {VISIBLE}
            AND level=:level AND domain=:domain AND scope=:scope
            AND consolidation_key IS NOT NULL AND lifecycle_status IN ('active','stale')
            AND (:scope<>'agent_private' OR agent_id=:owner) ORDER BY updated_at DESC,id
            LIMIT :limit"""),
            {
                **parameters(self.access),
                "level": target_level,
                "domain": seed.domain,
                "scope": str(seed.scope),
                "owner": seed.agent_id,
                "limit": limit,
            },
        ).mappings()
        return [_node(row) for row in rows]

    def due(self, target_level, limit):
        rows = self.connection.execute(
            text(f"""SELECT * FROM memory_nodes n WHERE {VISIBLE}
            AND ((level=:target AND is_dirty AND lifecycle_status IN ('active','stale')) OR
                 (level=:target-1 AND lifecycle_status='active' AND NOT is_dirty
                  AND embedding_status='ready' AND level<3))
            AND NOT EXISTS(SELECT 1 FROM outbox_events e WHERE e.user_id=n.user_id
                AND e.namespace=n.namespace AND e.node_id=n.id AND e.node_version=n.version
                AND e.event_type=CASE WHEN n.level=:target THEN 'consolidation.refresh'
                    ELSE 'consolidation.requested' END
                AND e.generation=CASE WHEN n.level=:target THEN n.dirty_revision ELSE 0 END)
            ORDER BY updated_at,id LIMIT :limit"""),
            {**parameters(self.access), "target": target_level, "limit": limit},
        ).mappings()
        return [_node(row) for row in rows]
