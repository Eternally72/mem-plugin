"""Immutable memory data independent of transport, configuration, and persistence."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from mem_plugin.engine.errors import InvalidMemoryInput
from mem_plugin.observability.models import TraceContext


class Scope(StrEnum):
    USER_GLOBAL = "user_global"
    DOMAIN_SHARED = "domain_shared"
    AGENT_PRIVATE = "agent_private"


@dataclass(frozen=True, slots=True)
class AccessContext:
    """Trusted identity and authorized namespace, supplied by the host, never an LLM."""

    user_id: str
    agent_id: str
    namespace: str = "default"
    domains: frozenset[str] = frozenset()

    def __post_init__(self):
        if not isinstance(self.domains, (frozenset, set, tuple, list)):
            raise InvalidMemoryInput("domains must be a collection of authorized names")
        for value in (self.user_id, self.agent_id, self.namespace, *self.domains):
            if not isinstance(value, str) or not value.strip():
                raise InvalidMemoryInput("identity and access fields must not be blank")
        object.__setattr__(self, "domains", frozenset(self.domains))


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryNode:
    user_id: str
    agent_id: str
    namespace: str
    content: str
    id: UUID = field(default_factory=uuid4)
    scope: Scope = Scope.AGENT_PRIVATE
    level: int = 0
    memory_type: str = "event"
    domain: str = "general"
    context: str = ""
    attributes: dict[str, object] = field(default_factory=dict)
    domain_payload: dict[str, object] = field(default_factory=dict)
    confidence: float | None = None
    importance: float | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    occurred_at: datetime | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    last_confirmed_at: datetime | None = None
    expires_at: datetime | None = None
    lifecycle_status: str = "active"
    version: int = 1
    source_ref: dict[str, object] = field(default_factory=dict)
    embedding_status: str = "pending"
    context_revision: int = 0
    is_dirty: bool = False
    dirty_revision: int = 0
    consolidation_key: str | None = None
    consolidation_hash: str | None = None
    archived_status: str | None = None
    needs_review: bool = False
    last_decay_at: datetime | None = None
    restored_at: datetime | None = None
    lifecycle_revision: int = 0
    idempotency_key: str | None = None
    request_hash: str | None = None

    @property
    def node_kind(self) -> str:
        return "atomic" if self.level == 0 else "derived"

    def __post_init__(self):
        if (
            isinstance(self.level, bool)
            or not isinstance(self.level, int)
            or not 0 <= self.level <= 3
        ):
            raise InvalidMemoryInput("level must be 0..3")
        for value in (
            self.user_id,
            self.agent_id,
            self.namespace,
            self.content,
            self.memory_type,
            self.domain,
        ):
            if not isinstance(value, str) or not value.strip():
                raise InvalidMemoryInput("node text and ownership fields must not be blank")
        try:
            object.__setattr__(self, "scope", Scope(self.scope))
            if not isinstance(self.id, UUID):
                raise ValueError("invalid UUID")
        except ValueError as exc:
            raise InvalidMemoryInput("invalid node identity or scope") from exc
        for value in (self.confidence, self.importance):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 <= value <= 1
            ):
                raise InvalidMemoryInput("confidence and importance must be 0..1")
        for value in (
            self.created_at,
            self.updated_at,
            self.occurred_at,
            self.valid_from,
            self.valid_until,
            self.last_confirmed_at,
            self.expires_at,
            self.last_decay_at,
            self.restored_at,
        ):
            if value is not None and (not isinstance(value, datetime) or value.utcoffset() is None):
                raise InvalidMemoryInput("timestamps must include a timezone")
        if self.valid_from and self.valid_until and self.valid_until < self.valid_from:
            raise InvalidMemoryInput("valid_until precedes valid_from")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise InvalidMemoryInput("version must be positive")
        if self.lifecycle_status not in {"active", "stale", "archived", "deleted"}:
            raise InvalidMemoryInput("invalid lifecycle status")
        if self.embedding_status not in {"pending", "ready", "failed"}:
            raise InvalidMemoryInput("invalid embedding status")
        if type(self.context_revision) is not int or self.context_revision < 0:
            raise InvalidMemoryInput("context revision must be nonnegative")
        if type(self.is_dirty) is not bool:
            raise InvalidMemoryInput("dirty flag must be boolean")
        if type(self.dirty_revision) is not int or self.dirty_revision < 0:
            raise InvalidMemoryInput("dirty revision must be nonnegative")
        if self.archived_status not in {None, "active", "stale"}:
            raise InvalidMemoryInput("invalid archived validity status")
        if (
            type(self.needs_review) is not bool
            or type(self.lifecycle_revision) is not int
            or self.lifecycle_revision < 0
        ):
            raise InvalidMemoryInput("invalid lifecycle metadata")


@dataclass(frozen=True, slots=True)
class TypeLifecyclePolicy:
    memory_type: str
    review_after_days: int = 0
    archive_after_days: int = 0
    confidence_half_life_days: int = 0
    retention_action: str = "archive"


@dataclass(frozen=True, slots=True)
class LifecyclePolicy:
    rules: tuple[TypeLifecyclePolicy, ...] = (
        TypeLifecyclePolicy("temporary_plan", 7, 30, 30),
        TypeLifecyclePolicy("short_term_state", 7, 30, 30),
        TypeLifecyclePolicy("preference", 90, 0, 365),
        TypeLifecyclePolicy("habit", 180, 0, 730),
        TypeLifecyclePolicy("event", 0, 365, 0),
        TypeLifecyclePolicy("investment_event", 0, 730, 0),
        TypeLifecyclePolicy("project", 90, 0, 0),
    )
    decay_interval_seconds: int = 86400
    restore_grace_days: int = 7

    def for_type(self, name):
        return next(
            (rule for rule in self.rules if rule.memory_type == name), TypeLifecyclePolicy(name)
        )

    def __post_init__(self):
        if (
            not 1 <= self.decay_interval_seconds <= 31536000
            or not 0 <= self.restore_grace_days <= 36500
        ):
            raise InvalidMemoryInput("invalid lifecycle timing")
        if len({r.memory_type for r in self.rules}) != len(self.rules):
            raise InvalidMemoryInput("duplicate lifecycle type policy")
        for rule in self.rules:
            if (
                rule.retention_action not in {"archive", "forget"}
                or not rule.memory_type.strip()
                or any(
                    type(n) is not int or not 0 <= n <= 36500
                    for n in (
                        rule.review_after_days,
                        rule.archive_after_days,
                        rule.confidence_half_life_days,
                    )
                )
            ):
                raise InvalidMemoryInput("invalid lifecycle type policy")


@dataclass(frozen=True, slots=True)
class ArchiveManifest:
    node_id: UUID
    storage_key: str
    operation_id: UUID
    checksum: str
    record_count: int
    byte_count: int
    node_version: int
    source_status: str
    status: str
    source_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class MemoryVersion:
    node_id: UUID
    version: int
    content: str
    source_ids: tuple[UUID, ...]
    reason: str
    source_versions: tuple[tuple[UUID, int], ...] = ()
    atomic_source_ids: tuple[UUID, ...] = ()
    confidence: float | None = None
    conflict_resolution: str = ""


@dataclass(frozen=True, slots=True)
class MemoryTask:
    id: UUID
    node_id: UUID
    node_version: int
    content_hash: str
    trace_id: UUID
    parent_span_id: UUID
    event_type: str = "embedding.requested"
    generation: int = 0


@dataclass(frozen=True, slots=True)
class VectorMatch:
    node: MemoryNode
    distance: float


@dataclass(frozen=True, slots=True)
class FeedbackResult:
    action: str
    memory: MemoryNode
    replacement: MemoryNode | None = None


@dataclass(frozen=True, slots=True)
class ChatResult:
    provider: str
    model: str
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    provider: str
    model: str
    vector: tuple[float, ...]
    input_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class SearchFilters:
    scope: Scope | None = None
    domain: str | None = None
    level: int | None = None
    memory_type: str | None = None
    occurred_after: datetime | None = None
    occurred_before: datetime | None = None
    as_of: datetime = field(default_factory=lambda: datetime.now(UTC))
    attributes: dict[str, object] = field(default_factory=dict)
    domain_payload: dict[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.scope is not None:
            try:
                object.__setattr__(self, "scope", Scope(self.scope))
            except ValueError as exc:
                raise InvalidMemoryInput("invalid scope filter") from exc
        if self.as_of is None:
            raise InvalidMemoryInput("as_of must include a timezone")
        if self.level is not None and (
            isinstance(self.level, bool)
            or not isinstance(self.level, int)
            or not 0 <= self.level <= 3
        ):
            raise InvalidMemoryInput("level filter must be 0..3")
        for value in (self.domain, self.memory_type):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise InvalidMemoryInput("text filters must not be blank")
        for value in (self.occurred_after, self.occurred_before, self.as_of):
            if value is not None and (not isinstance(value, datetime) or value.utcoffset() is None):
                raise InvalidMemoryInput("filter timestamps require a timezone")
        if (
            self.occurred_after
            and self.occurred_before
            and self.occurred_after > self.occurred_before
        ):
            raise InvalidMemoryInput("invalid occurrence time range")


@dataclass(frozen=True, slots=True)
class SearchHit:
    node: MemoryNode
    score: float
    channels: tuple[str, ...]
    channel_scores: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RecallResult:
    hits: tuple[SearchHit, ...]
    mode: str
    degraded_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetrievalPolicy:
    candidate_limit: int = 50
    rrf_k: int = 60
    trigram_threshold: float = 0.2
    rerank_enabled: bool = False

    def __post_init__(self):
        if (
            not 1 <= self.candidate_limit <= 200
            or self.rrf_k < 1
            or not 0 < self.trigram_threshold <= 1
        ):
            raise InvalidMemoryInput("invalid retrieval budget or threshold")


@dataclass(frozen=True, slots=True)
class ClaimedTask:
    id: UUID
    access: AccessContext
    node_id: UUID
    node_version: int
    content_hash: str
    trace: TraceContext
    lease_token: UUID
    attempt: int
    event_type: str = "embedding.requested"
    generation: int = 0


@dataclass(frozen=True, slots=True)
class ConsolidationPolicy:
    min_sources: tuple[int, int, int] = (2, 2, 2)
    min_evidence: tuple[int, int, int] = (2, 3, 5)
    candidate_limit: int = 12
    evidence_limit: int = 50
    max_distance: float = 0.45
    max_groups: int = 3
    min_confidence: float = 0.75
    max_input_characters: int = 24000
    max_output_tokens: int = 2048

    def __post_init__(self):
        for counts in (self.min_sources, self.min_evidence):
            if len(counts) != 3 or any(type(n) is not int or not 2 <= n <= 100 for n in counts):
                raise InvalidMemoryInput("three evidence thresholds of 2..100 are required")
        if (
            type(self.candidate_limit) is not int
            or not 2 <= self.candidate_limit <= 30
            or type(self.evidence_limit) is not int
            or not 2 <= self.evidence_limit <= 100
            or not 0 <= self.max_distance <= 2
            or not 0 <= self.min_confidence <= 1
            or type(self.max_groups) is not int
            or not 1 <= self.max_groups <= 5
            or not 1000 <= self.max_input_characters <= 64000
            or not 128 <= self.max_output_tokens <= 4096
        ):
            raise InvalidMemoryInput("invalid consolidation budget")


@dataclass(frozen=True, slots=True)
class ConsolidationProposal:
    key: str
    content: str
    source_ids: tuple[UUID, ...]
    confidence: float
    reason: str
    conflict_resolution: str


@dataclass(frozen=True, slots=True)
class SemanticRelation:
    source_id: UUID
    target_id: UUID
    relation_type: str
    confidence: float | None = None
    context_note: str = ""
    created_by_event: UUID | None = None


@dataclass(frozen=True, slots=True)
class RelationDecision:
    target_id: UUID
    relation_type: str
    confidence: float
    context_note: str


@dataclass(frozen=True, slots=True)
class GraphPolicy:
    candidate_limit: int = 5
    max_distance: float = 0.35
    min_confidence: float = 0.75
    max_input_characters: int = 12000
    max_output_tokens: int = 1024

    def __post_init__(self):
        if (
            type(self.candidate_limit) is not int
            or not 1 <= self.candidate_limit <= 10
            or not 0 <= self.max_distance <= 2
            or not 0 <= self.min_confidence <= 1
            or type(self.max_input_characters) is not int
            or not 1000 <= self.max_input_characters <= 32000
            or type(self.max_output_tokens) is not int
            or not 128 <= self.max_output_tokens <= 4096
        ):
            raise InvalidMemoryInput("invalid graph budget or threshold")
