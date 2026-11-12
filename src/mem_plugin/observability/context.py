"""Task-local context; crossing process boundaries requires explicit propagation."""

from contextvars import ContextVar

from mem_plugin.observability.models import TraceContext

current_trace: ContextVar[TraceContext | None] = ContextVar("mem_plugin_trace", default=None)
