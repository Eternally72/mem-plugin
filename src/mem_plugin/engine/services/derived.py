"""One atomic derived write path for explicit publication and automatic consolidation."""

from uuid import UUID, uuid4

from mem_plugin.engine.errors import InvalidMemoryInput, MemoryAccessDenied, MemoryNotFound
from mem_plugin.engine.models import MemoryVersion, Scope
from mem_plugin.engine.services.tasks import dirty, enqueue
from mem_plugin.observability.models import AuditEvent


def can_derive(child, parent):
    if child.scope == Scope.USER_GLOBAL:
        return True
    if parent.scope == Scope.USER_GLOBAL:
        return False
    if child.scope == Scope.AGENT_PRIVATE:
        return parent.scope == Scope.AGENT_PRIVATE and child.agent_id == parent.agent_id
    return parent.scope == Scope.AGENT_PRIVATE or child.domain == parent.domain


def persist_derived(
    uow, node, source_ids, reason, trace, actor, *, old=None, conflicts="", schedule=False
):
    try:
        ids = tuple(dict.fromkeys(UUID(str(value)) for value in source_ids))
    except ValueError as exc:
        raise InvalidMemoryInput("invalid memory ID") from exc
    if not ids:
        raise InvalidMemoryInput("derived memory requires sources")
    if old is None:
        uow.nodes.insert(node)
    else:
        uow.nodes.publish(node, old.version)
        node = uow.nodes.get(node.id)
    snapshots = []
    for source_id in sorted(ids):
        source = uow.nodes.get(source_id, lock=True)
        if source is None:
            raise MemoryNotFound("memory not found in access scope")
        if source.level + 1 != node.level:
            raise InvalidMemoryInput("hierarchy connects only adjacent ascending levels")
        if not can_derive(source, node):
            raise MemoryAccessDenied("derived memory cannot widen source visibility")
        uow.graph.hierarchy(source, node)
        snapshots.append((source.id, source.version))
    # Current membership follows the newly published sources; immutable snapshots retain history.
    uow.graph.replace_sources(node.id, ids)
    uow.versions.append(
        MemoryVersion(
            node.id,
            node.version,
            node.content,
            ids,
            reason,
            tuple(snapshots),
            confidence=node.confidence,
            conflict_resolution=conflicts,
        )
    )
    uow.audit.append(
        AuditEvent(
            uuid4(),
            node.id,
            actor,
            "consolidated",
            old.version if old else None,
            node.version,
            "derived memory published",
            trace.trace_id,
        )
    )
    if node.embedding_status != "ready":
        enqueue(uow, node, trace, "embedding.requested")
    elif schedule and node.level < 3:
        enqueue(uow, node, trace, "consolidation.requested")
    ancestors = tuple(n for n in uow.graph.ancestors((node.id,)) if n != node.id)
    if ancestors:
        dirty(uow, ancestors, actor, trace, schedule=schedule)
    return node
