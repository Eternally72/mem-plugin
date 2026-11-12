"""Archive explicitly selected memory through the public engine contract."""

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from mem_plugin.engine.api import NoteAPI
from mem_plugin.engine.errors import MemoryError
from mem_plugin.interfaces.mcp.context import ToolIdentity
from mem_plugin.interfaces.mcp.schemas import MemoryResponse


def register(server: FastMCP, memory: NoteAPI, identity: ToolIdentity) -> None:
    @server.tool(name="memory.archive")
    def memory_archive(
        memory_id: str, namespace: str | None = None, repair: bool = False
    ) -> MemoryResponse:
        """Archive after verification; repair explicitly rebuilds a damaged copy."""
        try:
            node = memory.archive(identity.access(namespace), memory_id, repair=repair)
            return MemoryResponse.from_memory(node)
        except MemoryError as exc:
            raise ToolError(f"{exc.code}: {exc}") from exc
