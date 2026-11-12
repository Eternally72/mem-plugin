"""The public memory-engine contract consumed by interfaces and direct callers."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from mem_plugin.engine.models import (
    AccessContext,
    FeedbackResult,
    MemoryNode,
    MemoryVersion,
    RecallResult,
    Scope,
    SearchFilters,
    SemanticRelation,
)


class NoteAPI(Protocol):
    """The single memory contract used by MCP and direct Python callers."""

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
    ) -> MemoryNode: ...
    def get(self, access: AccessContext, node_id: UUID | str) -> MemoryNode: ...
    def archive(
        self, access: AccessContext, node_id: UUID | str, *, repair: bool = False
    ) -> MemoryNode: ...
    def restore(self, access: AccessContext, node_id: UUID | str) -> MemoryNode: ...
    def relations(
        self, access: AccessContext, node_id: UUID | str
    ) -> tuple[SemanticRelation, ...]: ...
    def search(self, access: AccessContext, query: str, limit: int = 20) -> list[MemoryNode]: ...
    def recall(
        self,
        access: AccessContext,
        query: str,
        limit: int = 20,
        filters: SearchFilters | None = None,
        mode: str = "hybrid",
    ) -> RecallResult: ...
    def feedback(
        self,
        access: AccessContext,
        node_id: UUID | str,
        action: str,
        content: str | None = None,
        idempotency_key: str | None = None,
    ) -> FeedbackResult: ...
    def forget(self, access: AccessContext, node_id: UUID | str) -> bool: ...
    def forget_all(self, access: AccessContext) -> int:
        """Operator-only: forget all visible nodes in this exact namespace, in batches."""
        ...

    def link_hierarchy(
        self, access: AccessContext, child_id: UUID | str, parent_id: UUID | str
    ) -> None: ...
    def link_semantic(
        self,
        access: AccessContext,
        source_id: UUID | str,
        target_id: UUID | str,
        relation: str = "semantic",
    ) -> None: ...
    def derive(
        self,
        access: AccessContext,
        content: str,
        level: int,
        source_ids: tuple[UUID, ...],
        *,
        scope: Scope = Scope.AGENT_PRIVATE,
        domain: str = "general",
    ) -> MemoryNode: ...
    def publish(
        self,
        access: AccessContext,
        node_id: UUID | str,
        expected_version: int,
        content: str,
        source_ids: tuple[UUID, ...],
        reason: str,
    ) -> MemoryNode: ...
    def version(
        self, access: AccessContext, node_id: UUID | str, version: int
    ) -> MemoryVersion: ...
