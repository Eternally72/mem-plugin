"""Explicit node writes and graph/version rules, independent of database and transport."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from mem_plugin.engine.errors import (
    ArchiveError,
    InvalidMemoryInput,
    MemoryAccessDenied,
    MemoryConflict,
    MemoryNotFound,
)
from mem_plugin.engine.models import (
    AccessContext,
    FeedbackResult,
    MemoryNode,
    MemoryTask,
    MemoryVersion,
    RecallResult,
    Scope,
    SearchFilters,
    SemanticRelation,
)
from mem_plugin.engine.policies import note_text, structured_fields
from mem_plugin.engine.ports import UnitOfWork, UnitOfWorkFactory
from mem_plugin.engine.services.derived import can_derive, persist_derived
from mem_plugin.engine.services.forget import forget_in_transaction
from mem_plugin.engine.services.graph import refresh_contexts
from mem_plugin.engine.services.retrieval import RetrievalService
from mem_plugin.engine.services.tasks import dirty
from mem_plugin.observability.models import AuditEvent, TraceContext
from mem_plugin.observability.tracing import Tracer


def _identifier(value: UUID | str) -> UUID:
    try:
        return UUID(str(value))
    except ValueError as exc:
        raise InvalidMemoryInput("invalid memory ID") from exc


def _get(uow: UnitOfWork, node_id: UUID | str, *, lock: bool = False) -> MemoryNode:
    node = uow.nodes.get(_identifier(node_id), lock=lock)
    if node is None:
        raise MemoryNotFound("memory not found in access scope")
    return node


class NoteService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        tracer: Tracer,
        *,
        retrieval: RetrievalService | None = None,
        max_note_characters: int = 16000,
        consolidation_enabled: bool = False,
        lifecycle=None,
    ):
        self._uow = uow_factory
        self._tracer = tracer
        self._retrieval = retrieval or RetrievalService(uow_factory, tracer)
        self._max_note_characters = max_note_characters
        self._consolidation_enabled = consolidation_enabled
        self._lifecycle = lifecycle

    def _effects(
        self,
        uow: UnitOfWork,
        node: MemoryNode,
        trace: TraceContext,
        action: str,
        old_version: int | None = None,
    ) -> None:
        uow.audit.append(
            AuditEvent(
                uuid4(),
                node.id,
                node.agent_id,
                action,
                old_version,
                node.version,
                action,
                trace.trace_id,
            )
        )
        if node.embedding_status == "ready":
            return
        uow.outbox.append(
            MemoryTask(
                uuid4(),
                node.id,
                node.version,
                hashlib.sha256(node.content.encode()).hexdigest(),
                trace.trace_id,
                trace.span_id,
            )
        )

    def record(
        self,
        access: AccessContext,
        content: str,
        *,
        scope: Scope = Scope.AGENT_PRIVATE,
        domain: str = "general",
        memory_type: str = "event",
        idempotency_key: str | None = None,
        attributes: dict[str, object] | None = None,
        domain_payload: dict[str, object] | None = None,
        source_ref: dict[str, object] | None = None,
        occurred_at: datetime | None = None,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> MemoryNode:
        with self._tracer.span(
            "record", access.user_id, access.namespace, access.agent_id
        ) as trace:
            content = note_text(content, self._max_note_characters)
            if idempotency_key is not None and (
                not isinstance(idempotency_key, str) or not idempotency_key.strip()
            ):
                raise InvalidMemoryInput("idempotency key must not be blank")
            try:
                scope = Scope(scope)
            except ValueError as exc:
                raise InvalidMemoryInput("invalid scope") from exc
            if scope == Scope.DOMAIN_SHARED and domain not in access.domains:
                raise MemoryAccessDenied("domain is not authorized")
            payload = {
                "content": content.strip(),
                "scope": str(scope),
                "domain": domain,
                "memory_type": memory_type,
            }
            extra = {
                "attributes": structured_fields(attributes),
                "domain_payload": structured_fields(domain_payload),
                "source_ref": structured_fields(source_ref),
            }
            dates = {
                "occurred_at": occurred_at,
                "valid_from": valid_from,
                "valid_until": valid_until,
                "expires_at": expires_at,
            }
            for value in dates.values():
                if value is not None and (
                    not isinstance(value, datetime) or value.utcoffset() is None
                ):
                    raise InvalidMemoryInput("timestamps must include a timezone")
            payload.update({key: value for key, value in extra.items() if value})
            payload.update(
                {key: value.isoformat() for key, value in dates.items() if value is not None}
            )
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            node = MemoryNode(
                user_id=access.user_id,
                agent_id=access.agent_id,
                namespace=access.namespace,
                content=content.strip(),
                scope=scope,
                domain=domain,
                memory_type=memory_type,
                idempotency_key=idempotency_key,
                request_hash=digest,
                **extra,
                **dates,
            )
            with self._uow(access) as uow:
                saved = uow.nodes.insert(node)
                if saved.request_hash != digest:
                    raise MemoryConflict("idempotency key was used for different input")
                if saved.id == node.id:
                    self._effects(uow, saved, trace, "created")
                uow.commit()
                return saved

    def get(self, access: AccessContext, node_id: UUID | str) -> MemoryNode:
        with self._tracer.span("get", access.user_id, access.namespace, access.agent_id):
            with self._uow(access) as uow:
                return _get(uow, node_id)

    def search(self, access: AccessContext, query: str, limit: int = 20) -> list[MemoryNode]:
        with self._tracer.span("search", access.user_id, access.namespace, access.agent_id):
            if not isinstance(query, str) or not query.strip():
                raise InvalidMemoryInput("query must not be blank")
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
                raise InvalidMemoryInput("limit must be 1..100")
            with self._uow(access) as uow:
                return uow.nodes.search(query.strip(), limit)

    def recall(
        self,
        access: AccessContext,
        query: str,
        limit: int = 20,
        filters: SearchFilters | None = None,
        mode: str = "hybrid",
    ) -> RecallResult:
        return self._retrieval.recall(access, query, limit, filters, mode)

    def relations(self, access: AccessContext, node_id: UUID | str) -> tuple[SemanticRelation, ...]:
        with self._tracer.span("relations", access.user_id, access.namespace, access.agent_id):
            with self._uow(access) as uow:
                node = _get(uow, node_id)
                return uow.graph.relations(node.id)

    def archive(self, access, node_id, *, repair=False):
        if self._lifecycle is None:
            raise ArchiveError("archive storage is not configured")
        return self._lifecycle.archive(access, node_id, repair=repair)

    def restore(self, access, node_id):
        if self._lifecycle is None:
            raise ArchiveError("archive storage is not configured")
        return self._lifecycle.restore(access, node_id)

    def feedback(
        self,
        access: AccessContext,
        node_id: UUID | str,
        action: str,
        content: str | None = None,
        idempotency_key: str | None = None,
    ) -> FeedbackResult:
        with self._tracer.span(
            "feedback", access.user_id, access.namespace, access.agent_id
        ) as trace:
            if action not in {"confirm", "reject", "correct"}:
                raise InvalidMemoryInput("feedback action must be confirm, reject or correct")
            if action == "correct" and (not isinstance(content, str) or not content.strip()):
                raise InvalidMemoryInput("correction content must not be blank")
            if action == "correct":
                content = note_text(content, self._max_note_characters)
            if action != "correct" and content is not None:
                raise InvalidMemoryInput("content is only accepted for a correction")
            if idempotency_key is not None and (
                not isinstance(idempotency_key, str) or not idempotency_key.strip()
            ):
                raise InvalidMemoryInput("idempotency key must not be blank")
            with self._uow(access) as uow:
                uow.graph.lock_scope()
                old = _get(uow, node_id, lock=True)
                replacement = None
                if action == "confirm":
                    uow.nodes.confirm(old.id)
                else:
                    if action == "correct":
                        if old.level != 0:
                            raise InvalidMemoryInput(
                                "correct an original fact rather than overwriting a derived summary"
                            )
                        digest = hashlib.sha256(
                            json.dumps(
                                {"corrects": str(old.id), "content": content.strip()},
                                sort_keys=True,
                            ).encode()
                        ).hexdigest()
                        candidate = MemoryNode(
                            user_id=access.user_id,
                            namespace=access.namespace,
                            agent_id=access.agent_id,
                            scope=old.scope,
                            domain=old.domain,
                            memory_type=old.memory_type,
                            content=content.strip(),
                            idempotency_key=idempotency_key,
                            request_hash=digest,
                        )
                        replacement = uow.nodes.insert(candidate)
                        if replacement.request_hash != digest:
                            raise MemoryConflict("idempotency key was used for different input")
                        if replacement.id != candidate.id:
                            return FeedbackResult(action, old, replacement)
                        if old.lifecycle_status != "active":
                            raise MemoryConflict(
                                "correction target is no longer active; read its replacement"
                            )
                        uow.graph.semantic(replacement, old, "supersedes")
                        if self._consolidation_enabled:
                            for parent in uow.graph.parents(old.id):
                                self._hierarchy(uow, replacement, parent)
                        self._effects(uow, replacement, trace, "created")
                    affected = uow.graph.ancestors((old.id,))
                    neighbors = uow.graph.context_neighbors(affected)
                    uow.nodes.invalidate(affected)
                    dirty(
                        uow, affected, access.agent_id, trace, schedule=self._consolidation_enabled
                    )
                    refresh_contexts(
                        uow,
                        tuple(set(neighbors) | set(affected)),
                        access.agent_id,
                        trace.trace_id,
                        schedule=self._consolidation_enabled,
                    )
                uow.audit.append(
                    AuditEvent(
                        uuid4(),
                        old.id,
                        access.agent_id,
                        {"confirm": "confirmed", "reject": "rejected", "correct": "corrected"}[
                            action
                        ],
                        old.version,
                        old.version,
                        action,
                        trace.trace_id,
                    )
                )
                current = _get(uow, old.id)
                uow.commit()
                return FeedbackResult(action, current, replacement)

    def forget(self, access: AccessContext, node_id: UUID | str) -> bool:
        with self._tracer.span(
            "forget", access.user_id, access.namespace, access.agent_id
        ) as trace:
            with self._uow(access) as uow:
                uow.graph.lock_scope()
                target = uow.nodes.get(_identifier(node_id), lock=True)
                if target is None:
                    return False
                forget_in_transaction(
                    uow,
                    target,
                    access,
                    trace,
                    purge=self._lifecycle.purge if self._lifecycle else None,
                    schedule=self._consolidation_enabled,
                )
                uow.commit()
                return True

    def forget_all(self, access: AccessContext) -> int:
        """Explicit operator use; never registered as an Agent tool. No database reset."""
        count = 0
        with self._tracer.span(
            "forget_all", access.user_id, access.namespace, access.agent_id
        ) as trace:
            while True:
                with self._uow(access) as uow:
                    uow.graph.lock_scope()
                    ids = uow.nodes.ids(100)
                    if not ids:
                        break
                    for node_id in ids:
                        node = uow.nodes.get(node_id, lock=True)
                        if node is not None:
                            count += forget_in_transaction(
                                uow,
                                node,
                                access,
                                trace,
                                purge=self._lifecycle.purge if self._lifecycle else None,
                                schedule=False,
                                reason="explicit namespace cleanup",
                            )
                    uow.commit()
            trace.attributes["deleted_count"] = count
        return count

    def link_hierarchy(
        self, access: AccessContext, child_id: UUID | str, parent_id: UUID | str
    ) -> None:
        with self._tracer.span(
            "link_hierarchy", access.user_id, access.namespace, access.agent_id
        ) as trace:
            with self._uow(access) as uow:
                uow.graph.lock_scope()
                child, parent = _get(uow, child_id), _get(uow, parent_id)
                changed = self._hierarchy(uow, child, parent)
                if changed:
                    uow.audit.append(
                        AuditEvent(
                            uuid4(),
                            parent.id,
                            access.agent_id,
                            "linked",
                            None,
                            parent.version,
                            "member_of",
                            trace.trace_id,
                        )
                    )
                    if self._consolidation_enabled:
                        dirty(
                            uow,
                            uow.graph.ancestors((parent.id,)),
                            access.agent_id,
                            trace,
                            schedule=True,
                        )
                uow.commit()

    def _hierarchy(self, uow: UnitOfWork, child: MemoryNode, parent: MemoryNode) -> bool:
        if parent.level != child.level + 1:
            raise InvalidMemoryInput("hierarchy connects only adjacent ascending levels")
        if not can_derive(child, parent):
            raise MemoryAccessDenied("derived memory cannot widen source visibility")
        return uow.graph.hierarchy(child, parent)

    def link_semantic(
        self,
        access: AccessContext,
        source_id: UUID | str,
        target_id: UUID | str,
        relation: str = "semantic",
    ) -> None:
        if relation not in {
            "semantic",
            "same_entity",
            "temporal",
            "causal",
            "contradicts",
            "supersedes",
            "related_event",
        }:
            raise InvalidMemoryInput("invalid semantic relation")
        with self._tracer.span(
            "link_semantic", access.user_id, access.namespace, access.agent_id
        ) as trace:
            with self._uow(access) as uow:
                uow.graph.lock_scope()
                source, target = _get(uow, source_id), _get(uow, target_id)
                changed = uow.graph.semantic(source, target, relation)
                if changed:
                    uow.audit.append(
                        AuditEvent(
                            uuid4(),
                            source.id,
                            access.agent_id,
                            "linked",
                            None,
                            source.version,
                            relation,
                            trace.trace_id,
                        )
                    )
                uow.commit()

    def derive(
        self,
        access: AccessContext,
        content: str,
        level: int,
        source_ids: tuple[UUID, ...],
        *,
        scope: Scope = Scope.AGENT_PRIVATE,
        domain: str = "general",
    ) -> MemoryNode:
        with self._tracer.span(
            "derive", access.user_id, access.namespace, access.agent_id
        ) as trace:
            if isinstance(level, bool) or level not in (1, 2, 3):
                raise InvalidMemoryInput("derived level must be 1..3")
            node = MemoryNode(
                user_id=access.user_id,
                agent_id=access.agent_id,
                namespace=access.namespace,
                content=note_text(content, self._max_note_characters),
                level=level,
                scope=scope,
                domain=domain,
            )
            if node.scope == Scope.DOMAIN_SHARED and domain not in access.domains:
                raise MemoryAccessDenied("domain is not authorized")
            with self._uow(access) as uow:
                uow.graph.lock_scope()
                node = persist_derived(
                    uow,
                    node,
                    source_ids,
                    "initial derivation",
                    trace,
                    access.agent_id,
                    schedule=self._consolidation_enabled,
                )
                uow.commit()
                return node

    def publish(
        self,
        access: AccessContext,
        node_id: UUID | str,
        expected_version: int,
        content: str,
        source_ids: tuple[UUID, ...],
        reason: str,
    ) -> MemoryNode:
        with self._tracer.span(
            "publish", access.user_id, access.namespace, access.agent_id
        ) as trace:
            if (
                isinstance(expected_version, bool)
                or not isinstance(expected_version, int)
                or expected_version < 1
            ):
                raise InvalidMemoryInput("expected version must be a positive integer")
            with self._uow(access) as uow:
                uow.graph.lock_scope()
                old = _get(uow, node_id, lock=True)
                if old.level == 0:
                    raise InvalidMemoryInput(
                        "atomic facts require an explicit correction, not overwrite"
                    )
                if old.lifecycle_status == "archived":
                    raise MemoryConflict("restore archived memory before publishing")
                if old.version != expected_version:
                    raise MemoryConflict("memory version changed")
                if not isinstance(reason, str) or not reason.strip():
                    raise InvalidMemoryInput("change reason is required")
                node = replace(
                    old,
                    content=note_text(content, self._max_note_characters),
                    version=old.version + 1,
                    updated_at=datetime.now(UTC),
                    embedding_status="pending",
                )
                node = persist_derived(
                    uow,
                    node,
                    source_ids,
                    reason,
                    trace,
                    access.agent_id,
                    old=old,
                    schedule=self._consolidation_enabled,
                )
                uow.commit()
                return node

    def version(self, access: AccessContext, node_id: UUID | str, version: int) -> MemoryVersion:
        with self._tracer.span("version", access.user_id, access.namespace, access.agent_id):
            with self._uow(access) as uow:
                node = _get(uow, node_id)
                result = uow.versions.get(node.id, version)
                if result is None:
                    raise MemoryNotFound("memory version not found")
                for source in result.source_ids:
                    _get(uow, source)
                return result
