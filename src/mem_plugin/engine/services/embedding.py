"""Embedding job business logic; the worker only manages delivery and attempts."""

from uuid import uuid4

from mem_plugin.engine.errors import ModelProviderError, TaskLeaseLost
from mem_plugin.engine.models import ClaimedTask, MemoryTask
from mem_plugin.engine.policies import embedding_hash
from mem_plugin.engine.ports import EmbeddingProvider, UnitOfWorkFactory
from mem_plugin.engine.services.graph import current_note
from mem_plugin.engine.services.tasks import enqueue, task_trace
from mem_plugin.observability.models import AuditEvent
from mem_plugin.observability.tracing import Tracer


class EmbeddingService:
    def __init__(
        self,
        uow: UnitOfWorkFactory,
        provider: EmbeddingProvider,
        tracer: Tracer,
        model: str = "qwen3.7-text-embedding-flash",
        *,
        association_enabled: bool = False,
        consolidation_enabled: bool = False,
    ):
        self._uow, self._provider, self._tracer, self._model = uow, provider, tracer, model
        self._association_enabled = association_enabled
        self._consolidation_enabled = consolidation_enabled

    def _current(self, node, task):
        return current_note(node, task)

    def _associate(self, uow, node, task):
        if (
            self._consolidation_enabled
            and node.level < 3
            and (node.level > 0 or not self._association_enabled)
        ):
            enqueue(uow, node, task_trace(task), "consolidation.requested")
        if self._association_enabled and node.level == 0:
            from mem_plugin.observability.context import current_trace

            trace = current_trace.get()
            uow.outbox.append(
                MemoryTask(
                    uuid4(),
                    node.id,
                    node.version,
                    embedding_hash(node.content),
                    task.trace.trace_id,
                    trace.span_id if trace else task.trace.span_id,
                    "graph.requested",
                )
            )

    def execute(self, task: ClaimedTask) -> str:
        with self._uow(task.access) as uow:
            if not uow.outbox.lock_claim(task):
                raise TaskLeaseLost("task ownership was lost")
            node = uow.nodes.get(task.node_id)
            if not self._current(node, task) or uow.vectors.matches(
                task.node_id, task.node_version, task.content_hash
            ):
                if self._current(node, task):
                    self._associate(uow, node, task)
                if not uow.outbox.complete(task):
                    raise TaskLeaseLost("task lease expired")
                uow.commit()
                return "skipped"
        with self._tracer.span(
            "embedding.document", task.access.user_id, task.access.namespace, task.access.agent_id
        ) as span:
            span.attributes.update(provider=self._provider.provider, model=self._provider.model)
            result = self._provider.embed(node.content)
            span.attributes.update(
                provider=result.provider, model=result.model, input_tokens=result.input_tokens
            )
            if result.model != self._model:
                raise ModelProviderError(
                    "embedding model does not match the configured space", retryable=False
                )
        with self._uow(task.access) as uow:
            node = uow.nodes.get(task.node_id, lock=True)
            if not uow.outbox.lock_claim(task):
                raise TaskLeaseLost("task ownership was lost")
            if self._current(node, task):
                uow.vectors.save(task.node_id, task.node_version, task.content_hash, result.vector)
                uow.audit.append(
                    AuditEvent(
                        uuid4(),
                        task.node_id,
                        task.access.agent_id,
                        "embedded",
                        task.node_version,
                        task.node_version,
                        "embedding generated",
                        task.trace.trace_id,
                    )
                )
                outcome = "completed"
                self._associate(uow, node, task)
            else:
                outcome = "skipped"
            if not uow.outbox.complete(task):
                raise TaskLeaseLost("task lease expired")
            uow.commit()
            return outcome

    def failure(self, task: ClaimedTask, error_type: str, retry_delay: float | None) -> str:
        with self._uow(task.access) as uow:
            node = uow.nodes.get(task.node_id, lock=True)
            if not uow.outbox.lock_claim(task):
                raise TaskLeaseLost("task ownership was lost")
            if (
                retry_delay is None
                and self._current(node, task)
                and uow.nodes.embedding_failed(task.node_id, task.node_version)
            ):
                uow.audit.append(
                    AuditEvent(
                        uuid4(),
                        task.node_id,
                        task.access.agent_id,
                        "embedding_failed",
                        task.node_version,
                        task.node_version,
                        error_type,
                        task.trace.trace_id,
                    )
                )
            if not uow.outbox.fail(task, error_type, retry_delay):
                raise TaskLeaseLost("task lease expired")
            uow.commit()
        return "failed" if retry_delay is None else "retry"
