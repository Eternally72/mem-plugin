"""Atomic task production and dirty propagation shared by memory write use cases."""

from uuid import uuid4

from mem_plugin.engine.models import MemoryTask
from mem_plugin.engine.policies import embedding_hash
from mem_plugin.observability.context import current_trace
from mem_plugin.observability.models import AuditEvent


def enqueue(uow, node, trace, event_type, generation=0):
    uow.outbox.append(
        MemoryTask(
            uuid4(),
            node.id,
            node.version,
            embedding_hash(node.content),
            trace.trace_id,
            trace.span_id,
            event_type,
            generation,
        )
    )


def dirty(uow, ids, actor, trace, *, schedule=False, bump=True):
    nodes = uow.nodes.mark_dirty(tuple(set(ids)), bump=bump)
    for node in nodes:
        uow.audit.append(
            AuditEvent(
                uuid4(),
                node.id,
                actor,
                "marked_dirty",
                node.version,
                node.version,
                "source evidence changed",
                trace.trace_id,
            )
        )
        if schedule and node.lifecycle_status != "archived":
            enqueue(uow, node, trace, "consolidation.refresh", node.dirty_revision)
    return nodes


def task_trace(task):
    return current_trace.get() or task.trace
