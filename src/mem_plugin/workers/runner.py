"""One leased task per iteration; multiple independent processes may run safely."""

import logging
from uuid import uuid4

from mem_plugin.engine.errors import InvalidMemoryInput, ModelProviderError, TaskLeaseLost
from mem_plugin.engine.ports import TaskHandler, TaskQueue
from mem_plugin.observability.context import current_trace
from mem_plugin.observability.tracing import Tracer

logger = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        queue: TaskQueue,
        embedding: TaskHandler | None,
        tracer: Tracer,
        *,
        lease_seconds: float = 120,
        max_attempts: int = 3,
        retry_seconds: float = 5,
        worker_id: str | None = None,
        graph: TaskHandler | None = None,
        consolidation: TaskHandler | None = None,
        lifecycle: TaskHandler | None = None,
    ):
        self._queue, self._tracer = queue, tracer
        self._handlers = {"embedding.requested": embedding} if embedding is not None else {}
        if lifecycle is not None:
            self._handlers["lifecycle.maintain"] = lifecycle
        if graph is not None:
            self._handlers["graph.requested"] = graph
        if consolidation is not None:
            self._handlers.update(
                {"consolidation.requested": consolidation, "consolidation.refresh": consolidation}
            )
        self._lease, self._attempts, self._retry = lease_seconds, max_attempts, retry_seconds
        self._id = worker_id or str(uuid4())

    def run_once(self) -> str | None:
        task = self._queue.claim(self._id, self._lease, tuple(self._handlers))
        if task is None:
            return None
        logger.info(
            "memory_task_started task_id=%s trace_id=%s event_type=%s attempt=%s namespace=%s",
            task.id,
            task.trace.trace_id,
            task.event_type,
            task.attempt,
            task.access.namespace,
        )
        token = current_trace.set(task.trace)
        handler = self._handlers[task.event_type]
        operation = "worker." + task.event_type.split(".")[0]
        try:
            with self._tracer.span(
                operation, task.access.user_id, task.access.namespace, task.access.agent_id
            ) as span:
                span.attributes.update(
                    task_id=str(task.id),
                    attempt=task.attempt,
                    event_type=task.event_type,
                    generation=task.generation,
                )
                try:
                    if task.attempt > self._attempts:
                        outcome = handler.failure(task, "attempts_exhausted", None)
                        span.attributes["error_type"] = "AttemptsExhausted"
                    else:
                        outcome = handler.execute(task)
                except TaskLeaseLost:
                    outcome = "lease_lost"
                    span.attributes["error_type"] = "TaskLeaseLost"
                except Exception as exc:
                    if isinstance(exc, ModelProviderError):
                        span.attributes.update(
                            provider_failure_kind=exc.failure_kind,
                            provider_http_status=exc.http_status,
                        )
                    retryable = not isinstance(exc, InvalidMemoryInput) and (
                        not isinstance(exc, ModelProviderError) or exc.retryable
                    )
                    delay = (
                        min(300, self._retry * 2 ** min(task.attempt - 1, 16))
                        if retryable and task.attempt < self._attempts
                        else None
                    )
                    try:
                        outcome = handler.failure(task, type(exc).__name__, delay)
                    except TaskLeaseLost:
                        outcome = "lease_lost"
                    span.attributes["error_type"] = type(exc).__name__
                    logger.error(
                        "memory_task_failed task_id=%s trace_id=%s error_type=%s outcome=%s",
                        task.id,
                        task.trace.trace_id,
                        type(exc).__name__,
                        outcome,
                    )
                span.attributes["outcome"] = outcome
                logger.info(
                    "memory_task_finished task_id=%s trace_id=%s outcome=%s",
                    task.id,
                    task.trace.trace_id,
                    outcome,
                )
                return outcome
        finally:
            current_trace.reset(token)
