"""Administrative/periodic enqueueing; this use case never calls a model."""

from uuid import UUID

from mem_plugin.engine.errors import InvalidMemoryInput, MemoryNotFound
from mem_plugin.engine.services.tasks import dirty, enqueue


class SchedulingService:
    def __init__(self, uow, tracer):
        self._uow, self._tracer = uow, tracer

    def enqueue_due(self, access, target_level, limit=50):
        if type(target_level) is not int or not 1 <= target_level <= 3:
            raise InvalidMemoryInput("target level must be 1..3")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise InvalidMemoryInput("schedule batch must be 1..200")
        with self._tracer.span(
            "scheduler.enqueue", access.user_id, access.namespace, access.agent_id
        ) as trace:
            with self._uow(access) as uow:
                uow.graph.lock_scope()
                nodes = uow.consolidation.due(target_level, limit)
                for node in nodes:
                    refresh = node.level == target_level
                    enqueue(
                        uow,
                        node,
                        trace,
                        "consolidation.refresh" if refresh else "consolidation.requested",
                        node.dirty_revision if refresh else 0,
                    )
                uow.commit()
            trace.attributes.update(target_level=target_level, enqueued_count=len(nodes))
            return len(nodes)

    def request(self, access, node_id):
        try:
            node_id = UUID(str(node_id))
        except ValueError as exc:
            raise InvalidMemoryInput("invalid memory ID") from exc
        with self._tracer.span(
            "consolidation.request", access.user_id, access.namespace, access.agent_id
        ) as trace:
            with self._uow(access) as uow:
                uow.graph.lock_scope()
                node = uow.nodes.get(node_id, lock=True)
                if node is None:
                    raise MemoryNotFound("memory not found in access scope")
                if node.level == 0 or node.lifecycle_status not in {"active", "stale"}:
                    raise InvalidMemoryInput("request requires an active or stale derived memory")
                dirty(uow, (node.id,), access.agent_id, trace, schedule=True)
                uow.commit()
