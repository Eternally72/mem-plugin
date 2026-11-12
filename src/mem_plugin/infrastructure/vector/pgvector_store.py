"""Scoped cosine retrieval and version-checked vector persistence."""

import hashlib
import json
import math
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from mem_plugin.engine.errors import InvalidMemoryInput, MemoryConflict, MemoryNotFound
from mem_plugin.engine.models import AccessContext, MemoryNode, Scope, SearchFilters, VectorMatch
from mem_plugin.infrastructure.persistence.postgres.repositories import (
    VISIBLE,
    NodeRepository,
    _node,
    parameters,
)
from mem_plugin.infrastructure.search.postgres_search import bind_filters, filter_sql


class VectorRepository:
    def __init__(
        self,
        connection: Connection,
        access: AccessContext,
        nodes: NodeRepository,
        model: str,
        dimension: int,
    ):
        self.connection, self.access, self.nodes = connection, access, nodes
        self.model, self.dimension = model, dimension

    def _vector(self, vector: tuple[float, ...]) -> list[float]:
        if len(vector) != self.dimension:
            raise InvalidMemoryInput("vector dimension does not match schema")
        if any(
            isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
            for x in vector
        ):
            raise InvalidMemoryInput("vector must contain finite numbers")
        if not any(vector):
            raise InvalidMemoryInput("cosine vector must be nonzero")
        return list(vector)

    def save(
        self, node_id: UUID, version: int, content_hash: str, vector: tuple[float, ...]
    ) -> None:
        values = self._vector(vector)
        node = self.nodes.get(node_id, lock=True)
        if node is None:
            raise MemoryNotFound("memory not found in access scope")
        if (
            node.version != version
            or hashlib.sha256(node.content.encode()).hexdigest() != content_hash
        ):
            raise MemoryConflict("embedding input is stale")
        statement = text("""INSERT INTO memory_embeddings
            (user_id,namespace,node_id,node_version,embedding_model,embedding_dimension,content_hash,embedding)
            VALUES (:user_id,:namespace,:id,:version,:model,:dimension,:hash,:embedding)
            ON CONFLICT (node_id) DO UPDATE SET node_version=EXCLUDED.node_version,
            content_hash=EXCLUDED.content_hash,embedding=EXCLUDED.embedding,updated_at=now()""")
        statement = statement.bindparams(bindparam("embedding", type_=Vector(self.dimension)))
        self.connection.execute(
            statement,
            {
                **parameters(self.access),
                "id": node_id,
                "version": version,
                "model": self.model,
                "dimension": self.dimension,
                "hash": content_hash,
                "embedding": values,
            },
        )
        self.connection.execute(
            text(
                f"UPDATE memory_nodes SET embedding_status='ready' WHERE {VISIBLE} "
                "AND id=:id AND version=:version"
            ),
            {**parameters(self.access), "id": node_id, "version": version},
        )

    def matches(self, node_id: UUID, version: int, content_hash: str) -> bool:
        if self.nodes.get(node_id) is None:
            return False
        return bool(
            self.connection.execute(
                text("""SELECT EXISTS(SELECT 1 FROM memory_embeddings
            WHERE user_id=:user_id AND namespace=:namespace AND node_id=:id
                AND node_version=:version AND content_hash=:hash
                AND embedding_model=:model AND embedding_dimension=:dimension)"""),
                {
                    **parameters(self.access),
                    "id": node_id,
                    "version": version,
                    "hash": content_hash,
                    "model": self.model,
                    "dimension": self.dimension,
                },
            ).scalar_one()
        )

    def candidates(
        self, node: MemoryNode, limit: int, max_distance: float, *, same_level: bool = False
    ) -> list[VectorMatch]:
        if self.nodes.get(node.id) is None:
            return []
        raw = self.connection.execute(
            text("""SELECT embedding::text FROM memory_embeddings
            WHERE user_id=:user_id AND namespace=:namespace AND node_id=:id
              AND node_version=:version AND content_hash=:hash
              AND embedding_model=:model AND embedding_dimension=:dimension"""),
            {
                **parameters(self.access),
                "id": node.id,
                "version": node.version,
                "hash": hashlib.sha256(node.content.encode()).hexdigest(),
                "model": self.model,
                "dimension": self.dimension,
            },
        ).scalar_one_or_none()
        if raw is None:
            return []
        filters = SearchFilters(
            scope=node.scope,
            level=node.level if same_level else 0,
            domain=node.domain if same_level or node.scope == Scope.DOMAIN_SHARED else None,
        )
        matches = self.search(tuple(json.loads(raw)), limit + 1, filters)
        return [
            match
            for match in matches
            if match.node.id != node.id and match.distance <= max_distance
        ][:limit]

    def search(
        self, vector: tuple[float, ...], limit: int = 20, filters: SearchFilters | None = None
    ) -> list[VectorMatch]:
        values = self._vector(vector)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise InvalidMemoryInput("candidate limit must be 1..200")
        predicate, filter_values = filter_sql(filters or SearchFilters())
        statement = text(f"""SELECT n.*, e.embedding <=> CAST(:embedding AS vector) AS distance
            FROM (SELECT * FROM memory_nodes WHERE {VISIBLE} AND {predicate}) AS n
            JOIN memory_embeddings e ON e.node_id=n.id
                AND e.user_id=n.user_id AND e.namespace=n.namespace
            WHERE n.lifecycle_status='active' AND e.node_version=n.version
              AND e.embedding_model=:model AND e.embedding_dimension=:dimension
              AND e.content_hash=encode(sha256(convert_to(n.content,'UTF8')),'hex')
            ORDER BY distance,n.id LIMIT :limit""").bindparams(
            bindparam("embedding", type_=Vector(self.dimension))
        )
        rows = self.connection.execute(
            bind_filters(statement, filter_values),
            {
                **parameters(self.access),
                **filter_values,
                "embedding": values,
                "limit": limit,
                "model": self.model,
                "dimension": self.dimension,
            },
        ).mappings()
        result = []
        for row in rows:
            data = dict(row)
            distance = data.pop("distance")
            result.append(VectorMatch(_node(data), distance))
        return result
