"""Bounded lexical/vector retrieval, rank fusion and optional reranking."""

from dataclasses import replace
from uuid import UUID

from mem_plugin.engine.errors import InvalidMemoryInput, MemoryStorageError, ModelProviderError
from mem_plugin.engine.models import (
    AccessContext,
    MemoryNode,
    RecallResult,
    RetrievalPolicy,
    SearchFilters,
    SearchHit,
)
from mem_plugin.engine.policies import structured_fields
from mem_plugin.engine.ports import EmbeddingProvider, RerankProvider, UnitOfWorkFactory
from mem_plugin.observability.tracing import Tracer


def fuse(
    lexical: list[MemoryNode], vector: list[tuple[MemoryNode, float]], k: int
) -> list[SearchHit]:
    nodes: dict[UUID, MemoryNode] = {}
    scores: dict[UUID, float] = {}
    channels: dict[UUID, dict[str, float]] = {}
    for name, items in (
        ("lexical", [(node, float(rank)) for rank, node in enumerate(lexical, 1)]),
        ("vector", vector),
    ):
        seen = set()
        rank = 0
        for node, raw in items:
            if node.id in seen:
                continue
            seen.add(node.id)
            rank += 1
            nodes[node.id] = node
            scores[node.id] = scores.get(node.id, 0) + 1 / (k + rank)
            channels.setdefault(node.id, {})[name] = raw
    return [
        SearchHit(nodes[node_id], scores[node_id], tuple(channels[node_id]), channels[node_id])
        for node_id in sorted(nodes, key=lambda value: (-scores[value], str(value)))
    ]


class RetrievalService:
    def __init__(
        self,
        uow: UnitOfWorkFactory,
        tracer: Tracer,
        *,
        embedding: EmbeddingProvider | None = None,
        reranker: RerankProvider | None = None,
        policy: RetrievalPolicy | None = None,
        l0_only: bool = False,
    ):
        self._uow, self._tracer = uow, tracer
        self._embedding, self._reranker = embedding, reranker
        self._policy = policy or RetrievalPolicy()
        self._l0_only = l0_only

    def recall(
        self,
        access: AccessContext,
        query: str,
        limit: int = 20,
        filters: SearchFilters | None = None,
        mode: str = "hybrid",
    ) -> RecallResult:
        with self._tracer.span(
            "search", access.user_id, access.namespace, access.agent_id
        ) as trace:
            if not isinstance(query, str) or not query.strip():
                raise InvalidMemoryInput("query must not be blank")
            if len(query) > 4000:
                raise InvalidMemoryInput("query is too long")
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
                raise InvalidMemoryInput("limit must be 1..100")
            if mode not in {"literal", "keyword", "hybrid"}:
                raise InvalidMemoryInput("mode must be literal, keyword or hybrid")
            filters = filters or SearchFilters()
            if self._l0_only:
                if filters.level not in (None, 0):
                    raise InvalidMemoryInput("L0-only experiment accepts only level=0 or no level")
                filters = replace(filters, level=0)
            trace.attributes["l0_only"] = self._l0_only
            filters = replace(
                filters,
                attributes=structured_fields(filters.attributes),
                domain_payload=structured_fields(filters.domain_payload),
            )
            query = query.strip()
            budget = max(limit, self._policy.candidate_limit)
            reasons = []
            with self._tracer.span(
                "search.lexical", access.user_id, access.namespace, access.agent_id
            ) as span:
                with self._uow(access) as uow:
                    lexical = (
                        uow.nodes.search(query, budget, filters)
                        if mode == "literal"
                        else uow.nodes.lexical(
                            query, budget, filters, self._policy.trigram_threshold
                        )
                    )
                span.attributes["candidate_count"] = len(lexical)
            vector = []
            actual_mode = "literal" if mode == "literal" else "keyword"
            if mode == "hybrid":
                if self._embedding is None:
                    reasons.append("embedding_not_configured")
                else:
                    try:
                        with self._tracer.span(
                            "embedding.query", access.user_id, access.namespace, access.agent_id
                        ) as span:
                            span.attributes.update(
                                provider=self._embedding.provider, model=self._embedding.model
                            )
                            result = self._embedding.embed(query)
                            if result.model != self._embedding.model:
                                raise ModelProviderError(
                                    "query embedding model mismatch", retryable=False
                                )
                            span.attributes.update(
                                provider=result.provider,
                                model=result.model,
                                input_tokens=result.input_tokens,
                            )
                        with self._tracer.span(
                            "search.vector", access.user_id, access.namespace, access.agent_id
                        ) as span:
                            with self._uow(access) as uow:
                                vector = [
                                    (match.node, match.distance)
                                    for match in uow.vectors.search(result.vector, budget, filters)
                                ]
                            span.attributes["candidate_count"] = len(vector)
                        actual_mode = "hybrid"
                    except (ModelProviderError, MemoryStorageError, InvalidMemoryInput) as exc:
                        reasons.append(
                            "embedding_unavailable"
                            if not isinstance(exc, MemoryStorageError)
                            else "vector_unavailable"
                        )
            with self._tracer.span(
                "search.fusion", access.user_id, access.namespace, access.agent_id
            ) as span:
                hits = fuse(lexical, vector, self._policy.rrf_k)
                span.attributes.update(rrf_k=self._policy.rrf_k, candidate_count=len(hits))
            if self._policy.rerank_enabled:
                if self._reranker is None:
                    reasons.append("rerank_not_configured")
                elif hits:
                    try:
                        with self._tracer.span(
                            "search.rerank", access.user_id, access.namespace, access.agent_id
                        ) as span:
                            span.attributes.update(
                                provider=self._reranker.provider, model=self._reranker.model
                            )
                            ranks = self._reranker.rerank(
                                query, [hit.node.content for hit in hits[:budget]]
                            )
                            if any(not 0 <= index < min(len(hits), budget) for index, _ in ranks):
                                raise ModelProviderError("invalid rerank indices", retryable=False)
                            indices = [
                                index for index, _ in sorted(ranks, key=lambda item: -item[1])
                            ]
                            scores = dict(ranks)
                            reordered = [
                                replace(
                                    hits[index],
                                    channel_scores={
                                        **hits[index].channel_scores,
                                        "rerank": scores[index],
                                    },
                                )
                                for index in dict.fromkeys(indices)
                            ]
                            hits = reordered + [
                                hit for i, hit in enumerate(hits) if i not in indices
                            ]
                            span.attributes["candidate_count"] = min(len(hits), budget)
                    except ModelProviderError:
                        reasons.append("rerank_unavailable")
            if hits:
                with self._uow(access) as uow:
                    current = {
                        node.id: node
                        for node in uow.nodes.eligible(tuple(hit.node.id for hit in hits), filters)
                    }
                hits = [
                    replace(hit, node=current[hit.node.id])
                    for hit in hits
                    if hit.node.id in current and current[hit.node.id].version == hit.node.version
                ]
            trace.attributes.update(
                mode=actual_mode,
                candidate_limit=budget,
                result_count=min(len(hits), limit),
                degraded_reasons=reasons,
            )
            return RecallResult(tuple(hits[:limit]), actual_mode, tuple(reasons))
