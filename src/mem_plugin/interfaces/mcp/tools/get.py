"""Read a memory by ID through the engine contract."""

from dataclasses import asdict

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from mem_plugin.engine.api import NoteAPI
from mem_plugin.engine.errors import MemoryError
from mem_plugin.interfaces.mcp.context import ToolIdentity
from mem_plugin.interfaces.mcp.schemas import MemoryResponse, RelationResponse


def register(server: FastMCP, memory: NoteAPI, identity: ToolIdentity) -> None:
    @server.tool(name="memory.get")
    def memory_get(memory_id: str, namespace: str | None = None) -> MemoryResponse:
        """Read a memory by its exact ID within a namespace. Missing IDs produce a tool error."""
        try:
            access = identity.access(namespace)
            node = memory.get(access, memory_id)
            version = memory.version(access, node.id, node.version) if node.level else None
            return MemoryResponse.from_memory(node, version=version).model_copy(
                update={
                    "relations": [
                        RelationResponse(**asdict(edge))
                        for edge in memory.relations(access, node.id)
                    ]
                }
            )
        except MemoryError as exc:
            raise ToolError(f"{exc.code}: {exc}") from exc
