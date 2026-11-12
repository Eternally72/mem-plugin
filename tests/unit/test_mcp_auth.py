import asyncio

import pytest

from mem_plugin.interfaces.mcp.auth import SharedTokenVerifier


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
