"""Only identifiers and safe metadata; no memory content or credentials."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TraceContext:
    trace_id: UUID
    span_id: UUID
    parent_span_id: UUID | None
    correlation_id: UUID
    attributes: dict[str, object] = field(default_factory=dict, compare=False)


@dataclass(frozen=True, slots=True)
class Span:
    context: TraceContext
    user_id: str
    namespace: str
    agent_id: str
    operation: str
    started_at: datetime
    ended_at: datetime | None = None
    duration_ms: float | None = None
    status: str = "running"
    error_type: str | None = None
    process_name: str = "api"


@dataclass(frozen=True, slots=True)
class AuditEvent:
    id: UUID
    memory_id: UUID
    actor: str
    action: str
    old_version: int | None
    new_version: int | None
    reason: str
    trace_id: UUID


class TraceSink(Protocol):
    def write(self, span: Span) -> None: ...
