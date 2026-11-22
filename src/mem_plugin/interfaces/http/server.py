"""Run the HTTP application exposing MCP Streamable HTTP and health routes."""

import uvicorn

from mem_plugin.engine.api import NoteAPI
from mem_plugin.interfaces.http.app import create_app
from mem_plugin.interfaces.mcp.context import ToolIdentity


def run_server(
    memory: NoteAPI,
    *,
    mcp_url: str,
    token: str,
    identity: ToolIdentity,
    host: str = "127.0.0.1",
    port: int = 8000,
    resolve_scoped=None,
) -> None:
    """Serve the configured endpoint; every MCP request requires the bearer credential."""
    uvicorn.run(
        create_app(
            memory, identity=identity, mcp_url=mcp_url, token=token, resolve_scoped=resolve_scoped
        ),
        host=host,
        port=port,
    )
