"""Delete a specific memory through the engine contract."""

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from mem_plugin.engine.api import NoteAPI
from mem_plugin.engine.errors import MemoryError
from mem_plugin.interfaces.mcp.context import ToolIdentity
from mem_plugin.interfaces.mcp.schemas import DeleteResult


def register(server: FastMCP, memory: NoteAPI, identity: ToolIdentity) -> None:
    @server.tool(name="memory.forget")
    def memory_delete(memory_id: str, namespace: str | None = None) -> DeleteResult:
        """Erase a correction family and its derived dependents, versions and vectors."""
        try:
            return DeleteResult(deleted=memory.forget(identity.access(namespace), memory_id))
        except MemoryError as exc:
            raise ToolError(f"{exc.code}: {exc}") from exc
