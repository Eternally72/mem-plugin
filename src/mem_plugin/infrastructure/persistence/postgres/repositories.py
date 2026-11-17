"""Transaction-bound SQL adapters; all reads are scoped before returning rows."""

from dataclasses import asdict
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection

from mem_plugin.engine.errors import MemoryAccessDenied, MemoryConflict
from mem_plugin.engine.models import (
    AccessContext,
    MemoryNode,
    MemoryVersion,
    Scope,
    SearchFilters,
    SemanticRelation,
)
from mem_plugin.infrastructure.search.postgres_search import bind_filters, filter_sql, lexical_rows


def parameters(access: AccessContext) -> dict[str, object]:
    return {
        "user_id": access.user_id,
        "namespace": access.namespace,
        "agent_id": access.agent_id,
        "domains": list(access.domains),
        "l0_only": access.l0_only,
    }


VISIBLE = """user_id=:user_id AND namespace=:namespace
    AND (NOT :l0_only OR level=0) AND (
    scope='user_global' OR (scope='agent_private' AND agent_id=:agent_id)
    OR (scope='domain_shared' AND domain=ANY(CAST(:domains AS text[]))))"""


def _node(row) -> MemoryNode:
    values = dict(row)
    values.pop("node_kind")
    values.pop("search_vector", None)
    return MemoryNode(**values)


class NodeRepository:
    def __init__(self, connection: Connection, access: AccessContext):
        self.connection = connection
        self.access = access

    def ids(self, limit: int) -> tuple[UUID, ...]:
        return tuple(
            self.connection.execute(
                text(f"SELECT id FROM memory_nodes WHERE {VISIBLE} ORDER BY level,id LIMIT :limit"),
                {**parameters(self.access), "limit": limit},
            ).scalars()
        )

    def insert(self, node: MemoryNode) -> MemoryNode:
        if self.access.l0_only and node.level != 0:
            raise MemoryAccessDenied("L0-only access cannot create derived memories")
        if (node.user_id, node.namespace, node.agent_id) != (
            self.access.user_id,
            self.access.namespace,
            self.access.agent_id,
        ):
            raise MemoryAccessDenied("node ownership does not match trusted context")
        if node.scope == Scope.DOMAIN_SHARED and node.domain not in self.access.domains:
            raise MemoryAccessDenied("domain is not authorized")
        values = asdict(node)
        values["scope"] = str(node.scope)
        values["node_kind"] = node.node_kind
        columns = ", ".join(values)
        placeholders = ", ".join(f":{key}" for key in values)
        statement = text(
            f"INSERT INTO memory_nodes ({columns}) VALUES ({placeholders}) "
            "ON CONFLICT ON CONSTRAINT memory_request_unique DO NOTHING RETURNING *"
        )
        statement = statement.bindparams(
            *(bindparam(key, type_=JSONB) for key in ("attributes", "domain_payload", "source_ref"))
        )
        row = self.connection.execute(statement, values).mappings().first()
        if row is None:
            row = (
                self.connection.execute(
                    text(
                        f"SELECT * FROM memory_nodes WHERE {VISIBLE} "
                        "AND agent_id=:agent_id AND idempotency_key=:key"
                    ),
                    {**parameters(self.access), "key": node.idempotency_key},
                )
                .mappings()
                .first()
            )
            if row is None:
                raise MemoryConflict("idempotency request is not visible")
        return _node(row)

    def get(self, node_id: UUID, *, lock: bool = False) -> MemoryNode | None:
        suffix = " FOR UPDATE" if lock else ""
        row = (
            self.connection.execute(
                text(f"SELECT * FROM memory_nodes WHERE {VISIBLE} AND id=:id{suffix}"),
                {**parameters(self.access), "id": node_id},
            )
            .mappings()
            .first()
        )
        return _node(row) if row is not None else None

    def publish(self, node: MemoryNode, expected_version: int) -> None:
        result = self.connection.execute(
            text(f"""UPDATE memory_nodes SET content=:content,
            version=:new_version, updated_at=:updated_at,is_dirty=false,
            confidence=:confidence,lifecycle_status=:lifecycle,
            consolidation_hash=:fingerprint,
            embedding_status=CASE WHEN EXISTS(SELECT 1 FROM memory_embeddings e
                WHERE e.node_id=memory_nodes.id AND e.user_id=memory_nodes.user_id
                AND e.namespace=memory_nodes.namespace
                AND e.content_hash=encode(sha256(convert_to(:content,'UTF8')),'hex'))
                THEN 'ready' ELSE 'pending' END
            WHERE {VISIBLE} AND id=:id AND version=:expected_version AND level>0"""),
            {
                **parameters(self.access),
                "id": node.id,
                "content": node.content,
                "new_version": node.version,
                "updated_at": node.updated_at,
                "expected_version": expected_version,
                "confidence": node.confidence,
                "lifecycle": node.lifecycle_status,
                "fingerprint": node.consolidation_hash,
            },
        )
        if result.rowcount != 1:
            raise MemoryConflict("memory version changed")
        self.connection.execute(
            text("""UPDATE memory_embeddings SET node_version=:version
            WHERE user_id=:user_id AND namespace=:namespace AND node_id=:id
              AND content_hash=encode(sha256(convert_to(:content,'UTF8')),'hex')"""),
            {
                **parameters(self.access),
                "id": node.id,
                "version": node.version,
                "content": node.content,
            },
        )

    def search(
        self, query: str, limit: int, filters: SearchFilters | None = None
    ) -> list[MemoryNode]:
        predicate, values = filter_sql(filters) if filters else ("lifecycle_status='active'", {})
        rows = self.connection.execute(
            bind_filters(
                text(f"""SELECT * FROM memory_nodes
            WHERE {VISIBLE} AND {predicate}
              AND strpos(lower(content),lower(:query))>0
            ORDER BY updated_at DESC,id LIMIT :limit"""),
                values,
            ),
            {**parameters(self.access), **values, "query": query, "limit": limit},
        ).mappings()
        return [_node(row) for row in rows]

    def confirm(self, node_id: UUID) -> None:
        self.connection.execute(
            text(f"""UPDATE memory_nodes SET last_confirmed_at=now(),needs_review=false,
                lifecycle_revision=lifecycle_revision+1
            WHERE {VISIBLE} AND id=:id"""),
            {**parameters(self.access), "id": node_id},
        )

    def evolve_context(self, node: MemoryNode, context: str) -> MemoryNode:
        row = (
            self.connection.execute(
                text(f"""UPDATE memory_nodes SET context=:context,
            context_revision=context_revision+1,updated_at=now()
            WHERE {VISIBLE} AND id=:id AND context_revision=:revision AND version=:version
            RETURNING *"""),
                {
                    **parameters(self.access),
                    "id": node.id,
                    "context": context,
                    "revision": node.context_revision,
                    "version": node.version,
                },
            )
            .mappings()
            .first()
        )
        if row is None:
            raise MemoryConflict("context changed concurrently")
        return _node(row)

    def mark_dirty(self, node_ids: tuple[UUID, ...], *, bump: bool = False) -> list[MemoryNode]:
        rows = self.connection.execute(
            text(f"""UPDATE memory_nodes SET is_dirty=true,dirty_revision=dirty_revision+1
            WHERE {VISIBLE} AND id=ANY(CAST(:ids AS uuid[])) AND level>0
              AND lifecycle_status IN ('active','stale','archived') AND (:bump OR NOT is_dirty)
              RETURNING *"""),
            {**parameters(self.access), "ids": list(node_ids), "bump": bump},
        ).mappings()
        return [_node(row) for row in rows]

    def settle_dirty(self, node_id: UUID, *, blocked: bool = False) -> None:
        self.connection.execute(
            text(f"""UPDATE memory_nodes SET is_dirty=false,
            lifecycle_status=CASE WHEN :blocked THEN 'stale' ELSE lifecycle_status END
            WHERE {VISIBLE} AND id=:id AND level>0"""),
            {**parameters(self.access), "id": node_id, "blocked": blocked},
        )

    def lexical(
        self, query: str, limit: int, filters: SearchFilters, threshold: float
    ) -> list[MemoryNode]:
        return [
            _node(row)
            for row in lexical_rows(
                self.connection, parameters(self.access), VISIBLE, query, limit, filters, threshold
            )
        ]

    def eligible(self, node_ids: tuple[UUID, ...], filters: SearchFilters) -> list[MemoryNode]:
        predicate, values = filter_sql(filters)
        statement = text(
            f"SELECT * FROM memory_nodes WHERE {VISIBLE} AND {predicate} "
            "AND id=ANY(CAST(:ids AS uuid[]))"
        )
        rows = self.connection.execute(
            bind_filters(statement, values),
            {**parameters(self.access), **values, "ids": list(node_ids)},
        ).mappings()
        return [_node(row) for row in rows]

    def embedding_failed(self, node_id: UUID, version: int) -> bool:
        return (
            self.connection.execute(
                text(f"""UPDATE memory_nodes SET embedding_status='failed'
            WHERE {VISIBLE} AND id=:id AND version=:version
                AND lifecycle_status='active' AND embedding_status='pending'"""),
                {**parameters(self.access), "id": node_id, "version": version},
            ).rowcount
            == 1
        )

    def invalidate(self, node_ids: tuple[UUID, ...]) -> None:
        values = {**parameters(self.access), "ids": list(node_ids)}
        self.connection.execute(
            text("""UPDATE memory_nodes SET lifecycle_status=CASE WHEN lifecycle_status='archived'
                THEN 'archived' ELSE 'stale' END,
                archived_status=CASE WHEN lifecycle_status='archived' THEN 'stale'
            ELSE archived_status END,
                lifecycle_revision=lifecycle_revision+1
            WHERE user_id=:user_id AND namespace=:namespace AND id=ANY(CAST(:ids AS uuid[]))
              AND (lifecycle_status='active' OR archived_status='active')"""),
            values,
        )
        self.connection.execute(
            text("""UPDATE outbox_events SET status='failed',
            last_error='source invalidated',locked_by=NULL,locked_until=NULL,lease_token=NULL
            WHERE user_id=:user_id AND namespace=:namespace
              AND node_id=ANY(CAST(:ids AS uuid[])) AND status IN ('pending','running')"""),
            values,
        )

    def clear_hidden_context_dependents(self, node_ids: tuple[UUID, ...]) -> tuple[UUID, ...]:
        # A shared source can appear in another agent's private annotation. Deletion may
        # remove that dependent copy, but must never read or expose the private body.
        return tuple(
            self.connection.execute(
                text(f"""UPDATE memory_nodes SET context='',context_revision=context_revision+1,
                updated_at=now(),is_dirty=CASE WHEN level>0 THEN true ELSE is_dirty END,
                dirty_revision=dirty_revision+CASE WHEN level>0 THEN 1 ELSE 0 END
            WHERE user_id=:user_id AND namespace=:namespace AND NOT ({VISIBLE})
              AND context<>'' AND id IN (
                SELECT unnest(ARRAY[source_id,target_id]) FROM memory_semantic_edges
                WHERE user_id=:user_id AND namespace=:namespace AND created_by_event IS NOT NULL
                  AND (source_id=ANY(CAST(:ids AS uuid[]))
                       OR target_id=ANY(CAST(:ids AS uuid[])))) RETURNING id"""),
                {**parameters(self.access), "ids": list(node_ids)},
            ).scalars()
        )

    def forget(self, node_ids: tuple[UUID, ...]) -> None:
        values = {**parameters(self.access), "ids": list(node_ids)}
        self.connection.execute(
            text("""SELECT id FROM memory_nodes WHERE user_id=:user_id
            AND namespace=:namespace AND id=ANY(CAST(:ids AS uuid[])) ORDER BY id FOR UPDATE"""),
            values,
        )
        # Engine selects the correction family and derived descendants. No content copies remain.
        statements = [
            "DELETE FROM memory_version_sources WHERE node_id=ANY(CAST(:ids AS uuid[])) "
            "OR source_id=ANY(CAST(:ids AS uuid[]))",
            "DELETE FROM memory_versions WHERE node_id=ANY(CAST(:ids AS uuid[]))",
            "DELETE FROM memory_hierarchy_edges WHERE child_id=ANY(CAST(:ids AS uuid[])) "
            "OR parent_id=ANY(CAST(:ids AS uuid[]))",
            "DELETE FROM memory_semantic_edges WHERE source_id=ANY(CAST(:ids AS uuid[])) "
            "OR target_id=ANY(CAST(:ids AS uuid[]))",
            "DELETE FROM memory_embeddings WHERE node_id=ANY(CAST(:ids AS uuid[]))",
            "DELETE FROM outbox_events WHERE node_id=ANY(CAST(:ids AS uuid[]))",
            "DELETE FROM memory_audit_events WHERE memory_id=ANY(CAST(:ids AS uuid[]))",
            "DELETE FROM memory_nodes WHERE id=ANY(CAST(:ids AS uuid[]))",
        ]
        for sql in statements:
            # Parenthesize OR predicates before appending the tenant boundary.
            table, predicate = sql.split(" WHERE ", 1)
            self.connection.execute(
                text(
                    table
                    + " WHERE ("
                    + predicate
                    + ") AND user_id=:user_id AND namespace=:namespace"
                ),
                values,
            )


class GraphRepository:
    def __init__(self, connection: Connection, access: AccessContext, nodes: NodeRepository):
        self.connection, self.access, self.nodes = connection, access, nodes

    def lock_scope(self) -> None:
        # All graph mutations use this short transaction-scoped lock before node/job locks.
        # Structured encoding avoids ambiguity between user and namespace components.
        import json

        self.connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": json.dumps([self.access.user_id, self.access.namespace])},
        )

    def parents(self, node_id: UUID) -> list[MemoryNode]:
        rows = self.connection.execute(
            text(f"""SELECT * FROM memory_nodes WHERE {VISIBLE}
            AND id IN (SELECT parent_id FROM memory_hierarchy_edges WHERE user_id=:user_id
                AND namespace=:namespace AND child_id=:id) ORDER BY id"""),
            {**parameters(self.access), "id": node_id},
        ).mappings()
        return [_node(row) for row in rows]

    def replace_sources(self, node_id: UUID, source_ids: tuple[UUID, ...]) -> None:
        if self.nodes.get(node_id) is None:
            raise MemoryAccessDenied("hierarchy parent is not visible")
        self.connection.execute(
            text("""DELETE FROM memory_hierarchy_edges
            WHERE user_id=:user_id AND namespace=:namespace AND parent_id=:id
              AND relation_type='member_of' AND NOT child_id=ANY(CAST(:ids AS uuid[]))"""),
            {**parameters(self.access), "id": node_id, "ids": list(source_ids)},
        )

    def associate(self, relation: SemanticRelation) -> bool:
        self._visible(relation.source_id, relation.target_id)
        result = self.connection.execute(
            text("""INSERT INTO memory_semantic_edges
            (user_id,namespace,source_id,target_id,relation_type,confidence,context_note,created_by_event)
            VALUES (:user_id,:namespace,:source,:target,:relation,:confidence,:note,:event)
            ON CONFLICT DO NOTHING"""),
            {
                **parameters(self.access),
                "source": relation.source_id,
                "target": relation.target_id,
                "relation": relation.relation_type,
                "confidence": relation.confidence,
                "note": relation.context_note,
                "event": relation.created_by_event,
            },
        )
        return result.rowcount == 1

    def relations(
        self, node_id: UUID, *, active_only: bool = False
    ) -> tuple[SemanticRelation, ...]:
        predicate = (
            "AND s.lifecycle_status='active' AND t.lifecycle_status='active' "
            "AND e.created_by_event IS NOT NULL"
            if active_only
            else ""
        )
        rows = self.connection.execute(
            text(f"""WITH visible AS (
            SELECT * FROM memory_nodes WHERE {VISIBLE})
            SELECT e.source_id,e.target_id,e.relation_type,e.confidence,
                   e.context_note,e.created_by_event
            FROM memory_semantic_edges e JOIN visible s ON e.source_id=s.id
            JOIN visible t ON e.target_id=t.id
            WHERE e.user_id=:user_id AND e.namespace=:namespace
              AND (e.source_id=:id OR e.target_id=:id) {predicate}
            ORDER BY e.source_id,e.target_id,e.relation_type LIMIT 100"""),
            {**parameters(self.access), "id": node_id},
        ).mappings()
        return tuple(SemanticRelation(**row) for row in rows)

    def context_neighbors(self, node_ids: tuple[UUID, ...]) -> tuple[UUID, ...]:
        ids = self.connection.execute(
            text("""SELECT DISTINCT unnest(ARRAY[source_id,target_id])
            FROM memory_semantic_edges WHERE user_id=:user_id AND namespace=:namespace
            AND created_by_event IS NOT NULL
            AND (source_id=ANY(CAST(:ids AS uuid[])) OR target_id=ANY(CAST(:ids AS uuid[])))"""),
            {**parameters(self.access), "ids": list(node_ids)},
        ).scalars()
        return tuple(node_id for node_id in ids if self.nodes.get(node_id) is not None)

    def _visible(self, first: UUID, second: UUID):
        if self.nodes.get(first) is None or self.nodes.get(second) is None:
            raise MemoryAccessDenied("relation endpoint is not visible")

    def hierarchy(self, child: MemoryNode, parent: MemoryNode) -> bool:
        self._visible(child.id, parent.id)
        result = self.connection.execute(
            text("""INSERT INTO memory_hierarchy_edges
            (user_id,namespace,child_id,parent_id) VALUES (:user_id,:namespace,:child,:parent)
            ON CONFLICT DO NOTHING"""),
            {**parameters(self.access), "child": child.id, "parent": parent.id},
        )
        return result.rowcount == 1

    def semantic(self, source: MemoryNode, target: MemoryNode, relation: str) -> bool:
        self._visible(source.id, target.id)
        result = self.connection.execute(
            text("""INSERT INTO memory_semantic_edges
            (user_id,namespace,source_id,target_id,relation_type)
            VALUES (:user_id,:namespace,:source,:target,:relation) ON CONFLICT DO NOTHING"""),
            {
                **parameters(self.access),
                "source": source.id,
                "target": target.id,
                "relation": relation,
            },
        )
        return result.rowcount == 1

    def correction_family(self, node_id: UUID) -> tuple[UUID, ...]:
        return tuple(
            self.connection.execute(
                text("""WITH RECURSIVE links(a,b) AS (
            SELECT source_id,target_id FROM memory_semantic_edges
                WHERE user_id=:user_id AND namespace=:namespace AND relation_type='supersedes'
            UNION SELECT target_id,source_id FROM memory_semantic_edges
                WHERE user_id=:user_id AND namespace=:namespace AND relation_type='supersedes'
        ), family(id) AS (SELECT CAST(:id AS uuid)
            UNION SELECT links.b FROM links JOIN family ON links.a=family.id)
            SELECT id FROM family"""),
                {**parameters(self.access), "id": node_id},
            ).scalars()
        )

    def ancestors(self, node_ids: tuple[UUID, ...]) -> tuple[UUID, ...]:
        return tuple(
            self.connection.execute(
                text("""WITH RECURSIVE links(child,parent) AS (
            SELECT child_id,parent_id FROM memory_hierarchy_edges
                WHERE user_id=:user_id AND namespace=:namespace
            UNION SELECT source_id,node_id FROM memory_version_sources
                WHERE user_id=:user_id AND namespace=:namespace
        ), affected(id) AS (SELECT unnest(CAST(:ids AS uuid[]))
            UNION SELECT links.parent FROM links JOIN affected ON links.child=affected.id)
            SELECT id FROM affected"""),
                {**parameters(self.access), "ids": list(node_ids)},
            ).scalars()
        )


class VersionRepository:
    def __init__(self, connection: Connection, access: AccessContext, nodes: NodeRepository):
        self.connection, self.access, self.nodes = connection, access, nodes

    def append(self, version: MemoryVersion) -> None:
        target = self.nodes.get(version.node_id)
        if target is None or target.level == 0 or target.version != version.version:
            raise MemoryConflict("version does not match current derived memory")
        self.connection.execute(
            text("""INSERT INTO memory_versions
            (user_id,namespace,node_id,version,content,reason,confidence,conflict_resolution)
            VALUES (:user_id,:namespace,:id,:version,:content,:reason,:confidence,:conflicts)"""),
            {
                **parameters(self.access),
                "id": version.node_id,
                "version": version.version,
                "content": version.content,
                "reason": version.reason,
                "confidence": version.confidence,
                "conflicts": version.conflict_resolution,
            },
        )
        for source_id in dict.fromkeys(version.source_ids):
            source = self.nodes.get(source_id, lock=True)
            if source is None:
                raise MemoryAccessDenied("version source is not visible")
            if dict(version.source_versions).get(source_id, source.version) != source.version:
                raise MemoryConflict("source version changed")
            self.connection.execute(
                text("""INSERT INTO memory_version_sources
                (user_id,namespace,node_id,version,source_id,source_version)
                VALUES (:user_id,:namespace,:id,:version,:source,:source_version)"""),
                {
                    **parameters(self.access),
                    "id": version.node_id,
                    "version": version.version,
                    "source": source_id,
                    "source_version": source.version,
                },
            )

    def get(self, node_id: UUID, version: int) -> MemoryVersion | None:
        if self.nodes.get(node_id) is None:
            return None
        values = {**parameters(self.access), "id": node_id, "version": version}
        row = (
            self.connection.execute(
                text("""SELECT content,reason,confidence,conflict_resolution FROM memory_versions
            WHERE user_id=:user_id AND namespace=:namespace
              AND node_id=:id AND version=:version"""),
                values,
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        sources = tuple(
            self.connection.execute(
                text("""SELECT source_id,source_version FROM memory_version_sources
            WHERE user_id=:user_id AND namespace=:namespace AND node_id=:id AND version=:version
            ORDER BY source_id"""),
                values,
            ).all()
        )
        ids = tuple(source[0] for source in sources)
        if any(self.nodes.get(source_id) is None for source_id in ids):
            raise MemoryAccessDenied("version source is not visible")
        snapshots = tuple((source_id, source_version) for source_id, source_version in sources)
        evidence = self.evidence(node_id, version)
        return MemoryVersion(
            node_id,
            version,
            row["content"],
            ids,
            row["reason"],
            snapshots,
            tuple(node.id for node in evidence),
            row["confidence"],
            row["conflict_resolution"],
        )

    def evidence(self, node_id: UUID, version: int) -> tuple[MemoryNode, ...]:
        if self.nodes.get(node_id) is None:
            raise MemoryAccessDenied("evidence root is not visible")
        rows = self.connection.execute(
            text(f"""WITH RECURSIVE snapshot(id,version) AS (
            SELECT CAST(:id AS uuid),CAST(:version AS integer)
            UNION SELECT s.source_id,s.source_version FROM memory_version_sources s
            JOIN snapshot p ON s.node_id=p.id AND s.version=p.version
            WHERE s.user_id=:user_id AND s.namespace=:namespace)
            SELECT * FROM memory_nodes WHERE {VISIBLE} AND level=0
              AND (id,version) IN (SELECT id,version FROM snapshot) ORDER BY id"""),
            {**parameters(self.access), "id": node_id, "version": version},
        ).mappings()
        return tuple(_node(row) for row in rows)
