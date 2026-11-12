"""HTTP application factory preserving the MCP app's own lifespan."""

from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.routing import Route

from mem_plugin.engine.api import NoteAPI
from mem_plugin.interfaces.http.health import health
from mem_plugin.interfaces.mcp.auth import SharedTokenVerifier
from mem_plugin.interfaces.mcp.context import ToolIdentity
from mem_plugin.interfaces.mcp.server import create_server


def create_app(memory: NoteAPI, *, identity: ToolIdentity, mcp_url: str, token: str) -> Starlette:
    verifier = SharedTokenVerifier(
        token, mcp_url, user_id=identity.principal.user_id, agent_id=identity.principal.agent_id
    )
    app = create_server(memory, identity=identity, auth=verifier).http_app(
        path=urlsplit(mcp_url).path
    )
    app.routes.append(Route("/health", health, methods=["GET"]))
    return app
