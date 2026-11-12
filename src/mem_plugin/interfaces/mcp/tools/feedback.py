"""Explicit confirmation, rejection and append-only correction."""

from typing import Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from mem_plugin.engine.api import NoteAPI
from mem_plugin.engine.errors import MemoryError
from mem_plugin.interfaces.mcp.context import ToolIdentity
from mem_plugin.interfaces.mcp.schemas import FeedbackResponse, MemoryResponse


def register(server: FastMCP, memory: NoteAPI, identity: ToolIdentity) -> None:
    @server.tool(name="memory.feedback")
    def memory_feedback(
        memory_id: str,
        action: Literal["confirm", "reject", "correct"],
        namespace: str | None = None,
        content: str | None = None,
        idempotency_key: str | None = None,
    ) -> FeedbackResponse:
        """Correct creates a new fact; use the returned replacement ID for later operations."""
        try:
            result = memory.feedback(
                identity.access(namespace), memory_id, action, content, idempotency_key
            )
            return FeedbackResponse(
                action=result.action,
                memory=MemoryResponse.from_memory(result.memory),
                replacement=MemoryResponse.from_memory(result.replacement)
                if result.replacement
                else None,
            )
        except MemoryError as exc:
            raise ToolError(f"{exc.code}: {exc}") from exc
