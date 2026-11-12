"""Trace persistence is independent of the business transaction."""

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from mem_plugin.observability.context import current_trace
from mem_plugin.observability.models import Span, TraceContext, TraceSink

logger = logging.getLogger(__name__)


class Tracer:
    def __init__(self, sink: TraceSink, process_name: str = "api"):
        self._sink = sink
        self._process_name = process_name

    def _write(self, span: Span) -> None:
        try:
            self._sink.write(span)
        except Exception as exc:
            # Sink errors must not change a committed operation's outcome or leak a DSN.
            logger.error(
                "trace_persistence_failed trace_id=%s span_id=%s error_type=%s",
                span.context.trace_id,
                span.context.span_id,
                type(exc).__name__,
            )

    @contextmanager
    def span(
        self, operation: str, user_id: str, namespace: str, agent_id: str
    ) -> Iterator[TraceContext]:
        parent = current_trace.get()
        context = TraceContext(
            trace_id=parent.trace_id if parent else uuid4(),
            span_id=uuid4(),
            parent_span_id=parent.span_id if parent else None,
            correlation_id=parent.correlation_id if parent else uuid4(),
        )
        record = Span(
            context,
            user_id,
            namespace,
            agent_id,
            operation,
            datetime.now(UTC),
            process_name=self._process_name,
        )
        token = current_trace.set(context)
        started = monotonic()
        self._write(record)
        identifiers = {
            "operation": operation,
            "process": self._process_name,
            "trace_id": str(context.trace_id),
            "span_id": str(context.span_id),
            "namespace": namespace,
        }
        logger.info("memory_operation_started %s", json.dumps(identifiers))
        try:
            yield context
        except BaseException as exc:
            record = replace(record, status="failed", error_type=type(exc).__name__)
            raise
        else:
            error_type = context.attributes.get("error_type")
            record = replace(
                record, status="failed" if error_type else "success", error_type=error_type
            )
        finally:
            finished = replace(
                record, ended_at=datetime.now(UTC), duration_ms=(monotonic() - started) * 1000
            )
            self._write(finished)
            # Only explicitly reviewed metadata belongs in diagnostic logs.
            safe = {
                key: value
                for key, value in context.attributes.items()
                if key
                in {
                    "provider",
                    "model",
                    "input_tokens",
                    "output_tokens",
                    "result_count",
                    "candidate_count",
                    "source_count",
                    "unique_evidence_count",
                    "input_characters",
                    "budget_blocked",
                    "budget_reason",
                    "task_id",
                    "attempt",
                    "event_type",
                    "outcome",
                    "provider_failure_kind",
                    "provider_http_status",
                }
                and isinstance(value, (str, int, float, bool, type(None)))
            }
            logger.log(
                logging.WARNING if finished.status == "failed" else logging.INFO,
                "memory_operation_finished %s",
                json.dumps(
                    {
                        **identifiers,
                        **safe,
                        "status": finished.status,
                        "duration_ms": round(finished.duration_ms, 2),
                        "error_type": finished.error_type,
                    }
                ),
            )
            current_trace.reset(token)
