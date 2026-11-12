"""FastMCP adapter for a single preconfigured, opaque bearer credential."""

import hashlib
import hmac
from urllib.parse import urlsplit

from fastmcp.server.auth import AccessToken, TokenVerifier


class SharedTokenVerifier(TokenVerifier):
    def __init__(
        self,
        token: str,
        public_url: str,
        *,
        user_id: str = "personal-user",
        agent_id: str = "personal-agent",
    ):
        if not token:
            raise ValueError("MCP bearer token must not be empty")
        url = urlsplit(public_url)
        super().__init__(base_url=f"{url.scheme}://{url.netloc}")
        self._expected_digest = hashlib.sha256(token.encode()).digest()
        self._resource = public_url
        self._user_id, self._agent_id = user_id, agent_id

    async def verify_token(self, token: str) -> AccessToken | None:
        candidate = hashlib.sha256(token.encode()).digest()
        if not hmac.compare_digest(candidate, self._expected_digest):
            return None
        return AccessToken(
            token=token,
            client_id=self._agent_id,
            scopes=[],
            resource=self._resource,
            subject=self._user_id,
        )
