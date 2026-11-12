"""Protocol mapping for bounded retrieval; all ranking rules belong to the engine."""

from datetime import UTC, datetime
from typing import Annotated

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from mem_plugin.engine.api import NoteAPI
from mem_plugin.engine.errors import MemoryError
from mem_plugin.engine.models import Scope, SearchFilters
from mem_plugin.interfaces.mcp.context import ToolIdentity
from mem_plugin.interfaces.mcp.schemas import MemoryResponse, SearchResult


def register(server: FastMCP, memory: NoteAPI, identity: ToolIdentity) -> None:
    @server.tool(name="memory.search")
    def memory_search(
        query: str,
        namespace: str | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        mode: str = "hybrid",
        scope: Scope | None = None,
        domain: str | None = None,
        level: int | None = None,
        memory_type: str | None = None,
        occurred_after: datetime | None = None,
        occurred_before: datetime | None = None,
        as_of: datetime | None = None,
        attributes: dict[str, object] | None = None,
        domain_payload: dict[str, object] | None = None,
    ) -> SearchResult:
        """Hybrid semantic/keyword retrieval; missing embedding falls back with a reason.

        Use keyword to avoid query embedding, literal for exact substrings. Time filters
        require timezone offsets; attributes/domain_payload match structured JSON values.
        """
        try:
            access = identity.access(namespace)
            result = memory.recall(
                access,
                query,
                limit,
                SearchFilters(
                    scope=scope,
                    domain=domain,
                    level=level,
                    memory_type=memory_type,
                    occurred_after=occurred_after,
                    occurred_before=occurred_before,
                    as_of=as_of or datetime.now(UTC),
                    attributes=attributes or {},
                    domain_payload=domain_payload or {},
                ),
                mode,
            )
            return SearchResult(
                mode=result.mode,
                degraded_reasons=list(result.degraded_reasons),
                memories=[
                    MemoryResponse.from_memory(
                        hit.node,
                        version=memory.version(access, hit.node.id, hit.node.version)
                        if hit.node.level
                        else None,
                    ).model_copy(
                        update={
                            "score": hit.score,
                            "channels": list(hit.channels),
                            "channel_scores": hit.channel_scores,
                        }
                    )
                    for hit in result.hits
                ],
            )
        except MemoryError as exc:
            raise ToolError(f"{exc.code}: {exc}") from exc
