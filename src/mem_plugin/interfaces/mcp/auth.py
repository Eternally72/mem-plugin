"""FastMCP verifier for operator and scoped opaque bearer credentials."""

import asyncio
import hashlib
import hmac
from urllib.parse import urlsplit

from fastmcp.server.auth import AccessToken, TokenVerifier

from mem_plugin.engine.errors import MemoryStorageError


class SharedTokenVerifier(TokenVerifier):
    def __init__(
        self,
        token: str,
        public_url: str,
        *,
        user_id: str = "personal-user",
        agent_id: str = "personal-agent",
        resolve_scoped=None,
    ):
        if not token:
            raise ValueError("MCP bearer token must not be empty")
        url = urlsplit(public_url)
        super().__init__(base_url=f"{url.scheme}://{url.netloc}")
        self._expected_digest = hashlib.sha256(token.encode()).digest()
        self._resource = public_url
        self._user_id, self._agent_id = user_id, agent_id
        self._resolve_scoped = resolve_scoped

    async def verify_token(self, token: str) -> AccessToken | None:
        candidate = hashlib.sha256(token.encode()).digest()
        if hmac.compare_digest(candidate, self._expected_digest):
            return AccessToken(
                token=token,
                client_id=self._agent_id,
                scopes=[],
                resource=self._resource,
                subject=self._user_id,
            )
        if self._resolve_scoped is None:
            return None
        try:
            record = await asyncio.to_thread(self._resolve_scoped, token)
        except MemoryStorageError:
            return None
        if record is None:
            return None
        return AccessToken(
            token=token,
            client_id=record["namespace"],
            subject=record["namespace"],
            scopes=[],
            resource=self._resource,
            expires_at=int(record["expires_at"].timestamp()),
            claims={"memory_label": record["label"], "memory_namespace": record["namespace"]},
        )
