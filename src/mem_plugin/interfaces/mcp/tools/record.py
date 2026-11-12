"""Save explicit facts through the shared NoteAPI."""

from datetime import datetime

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from mem_plugin.engine.api import NoteAPI
from mem_plugin.engine.errors import MemoryError
from mem_plugin.engine.models import Scope
from mem_plugin.interfaces.mcp.context import ToolIdentity
from mem_plugin.interfaces.mcp.schemas import MemoryResponse


def register(server: FastMCP, memory: NoteAPI, identity: ToolIdentity) -> None:
    @server.tool(name="memory.record")
    def memory_record(
        content: str,
        namespace: str | None = None,
        scope: Scope = Scope.AGENT_PRIVATE,
        domain: str = "general",
        memory_type: str = "event",
        idempotency_key: str | None = None,
        attributes: dict[str, object] | None = None,
        domain_payload: dict[str, object] | None = None,
        source_ref: dict[str, object] | None = None,
        occurred_at: datetime | None = None,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> MemoryResponse:
        """Save one independently usable fact, event or decision with its necessary context.

        Separate independent findings into separate calls, even within the same topic.
        Keep a decision and its rationale together; metrics from one evaluation can be
        one event. This tool preserves the submitted content without automatic splitting.
        Include provenance and observed final results when available, never guessed results.
        Returns after durable commit; embedding may be pending.
        """
        try:
            node = memory.record(
                identity.access(namespace),
                content,
                scope=scope,
                domain=domain,
                memory_type=memory_type,
                idempotency_key=idempotency_key,
                attributes=attributes,
                domain_payload=domain_payload,
                source_ref=source_ref,
                occurred_at=occurred_at,
                valid_from=valid_from,
                valid_until=valid_until,
                expires_at=expires_at,
            )
            return MemoryResponse.from_memory(node)
        except MemoryError as exc:
            raise ToolError(f"{exc.code}: {exc}") from exc
