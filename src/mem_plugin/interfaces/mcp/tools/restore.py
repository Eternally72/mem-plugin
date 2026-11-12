"""Restore selected memory through the public engine contract."""

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from mem_plugin.engine.api import NoteAPI
from mem_plugin.engine.errors import MemoryError
from mem_plugin.interfaces.mcp.context import ToolIdentity
from mem_plugin.interfaces.mcp.schemas import MemoryResponse


def register(server: FastMCP, memory: NoteAPI, identity: ToolIdentity) -> None:
    @server.tool(name="memory.restore")
    def memory_restore(memory_id: str, namespace: str | None = None) -> MemoryResponse:
        """Restore verified archived memory; expired or invalid facts remain stale."""
        try:
            return MemoryResponse.from_memory(memory.restore(identity.access(namespace), memory_id))
        except MemoryError as exc:
            raise ToolError(f"{exc.code}: {exc}") from exc
