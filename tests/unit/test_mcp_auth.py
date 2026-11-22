import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from httpx2 import ASGITransport, AsyncClient

from mem_plugin.engine.models import AccessContext
from mem_plugin.interfaces.http.app import create_app
from mem_plugin.interfaces.mcp.auth import SharedTokenVerifier
from mem_plugin.interfaces.mcp.context import ToolIdentity


def test_verifier_accepts_only_the_configured_credential():
    async def scenario():
        token = "test-credential-" * 3
        verifier = SharedTokenVerifier(token, "https://example.com/mcp")
        assert await verifier.verify_token("wrong-token") is None
        assert await verifier.verify_token("") is None
        assert await verifier.verify_token(token + "x") is None
        identity = await verifier.verify_token(token)
        assert identity.client_id == "personal-agent"
        assert identity.resource == "https://example.com/mcp"
        assert token not in repr(verifier)

    asyncio.run(scenario())


def test_empty_credential_cannot_create_an_unprotected_app():
    with pytest.raises(ValueError, match="must not be empty"):
        SharedTokenVerifier("", "http://127.0.0.1:8000/mcp")


def test_same_mcp_endpoint_binds_tool_calls_to_each_bearer():
    expires = datetime.now(UTC) + timedelta(hours=1)
    records = {
        "first": {"label": "test-a", "namespace": "scope-a", "expires_at": expires},
        "second": {"label": "test-b", "namespace": "scope-b", "expires_at": expires},
    }
    memory = Mock()
    memory.recall.return_value = SimpleNamespace(mode="keyword", degraded_reasons=(), hits=())
    identity = ToolIdentity(AccessContext("owner", "local"), frozenset({"default"}))
    app = create_app(memory, identity=identity, mcp_url="http://testserver/mcp",
                     token="owner-token", resolve_scoped=records.get)

    async def search(client, bearer, namespace=None):
        arguments = {"query": "marker", "mode": "keyword"}
        if namespace is not None:
            arguments["namespace"] = namespace
        return await client.post(
            "/mcp",
            headers={"Authorization": f"Bearer {bearer}",
                     "Accept": "application/json, text/event-stream",
                     "MCP-Protocol-Version": "2025-06-18"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "memory.search", "arguments": arguments}},
        )

    def result(response):
        if response.headers["content-type"].startswith("text/event-stream"):
            return json.loads(next(line[6:] for line in response.text.splitlines()
                                   if line.startswith("data: ")))["result"]
        return response.json()["result"]

    async def scenario():
        async with app.router.lifespan_context(app), AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            assert not result(await search(client, "first", "test-a")).get("isError")
            assert memory.recall.call_args.args[0] == AccessContext("scope-a", "scope-a", "scope-a")
            assert not result(await search(client, "second")).get("isError")
            assert memory.recall.call_args.args[0] == AccessContext("scope-b", "scope-b", "scope-b")
            assert result(await search(client, "second", "test-a")).get("isError")
            assert memory.recall.call_count == 2
            assert not result(await search(client, "owner-token")).get("isError")
            assert memory.recall.call_args.args[0] == AccessContext("owner", "local")
            records.pop("first")
            assert (await search(client, "first")).status_code == 401

    asyncio.run(asyncio.wait_for(scenario(), timeout=10))
