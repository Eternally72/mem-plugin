"""Public MCP response schemas mapped explicitly from core memory data."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from mem_plugin.engine.models import MemoryNode, MemoryVersion


class RelationResponse(BaseModel):
    source_id: UUID
    target_id: UUID
    relation_type: str
    confidence: float | None
    context_note: str
    created_by_event: UUID | None


class MemoryResponse(BaseModel):
    id: UUID
    namespace: str
    content: str
    scope: str
    level: int
    memory_type: str
    domain: str
    confidence: float | None
    version: int
    lifecycle_status: str
    embedding_status: str
    created_at: datetime
    updated_at: datetime
    occurred_at: datetime | None
    valid_from: datetime | None
    valid_until: datetime | None
    expires_at: datetime | None
    archived_status: str | None
    needs_review: bool
    last_decay_at: datetime | None
    restored_at: datetime | None
    context: str = ""
    context_revision: int = 0
    is_dirty: bool = False
    relations: list[RelationResponse] = Field(default_factory=list)
    source_ids: list[UUID] = Field(default_factory=list)
    atomic_source_ids: list[UUID] = Field(default_factory=list)
    conflict_resolution: str = ""
    attributes: dict[str, object] = Field(default_factory=dict)
    domain_payload: dict[str, object] = Field(default_factory=dict)
    source_ref: dict[str, object] = Field(default_factory=dict)
    score: float | None = None
    channels: list[str] = Field(default_factory=list)
    channel_scores: dict[str, float] = Field(default_factory=dict)

    @classmethod
    def from_memory(
        cls,
        memory: MemoryNode,
        source_ids: tuple[UUID, ...] = (),
        *,
        version: MemoryVersion | None = None,
    ) -> "MemoryResponse":
        excluded = {
            "source_ids",
            "score",
            "channels",
            "channel_scores",
            "relations",
            "atomic_source_ids",
            "conflict_resolution",
        }
        values = {name: getattr(memory, name) for name in cls.model_fields if name not in excluded}
        return cls(
            **values,
            source_ids=list(version.source_ids if version else source_ids),
            atomic_source_ids=list(
                version.atomic_source_ids if version else (memory.id,) if memory.level == 0 else ()
            ),
            conflict_resolution=version.conflict_resolution if version else "",
        )


class SearchResult(BaseModel):
    memories: list[MemoryResponse]
    mode: str
    degraded_reasons: list[str] = Field(default_factory=list)


class FeedbackResponse(BaseModel):
    action: str
    memory: MemoryResponse
    replacement: MemoryResponse | None = None


class DeleteResult(BaseModel):
    deleted: bool
