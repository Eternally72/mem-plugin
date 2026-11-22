"""Local operator-issued credentials. Only token digests are stored in PostgreSQL."""

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from mem_plugin.engine.errors import MemoryStorageError
from mem_plugin.engine.models import AccessContext


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


class CredentialStore:
    def __init__(self, engine):
        self.engine = engine

    def issue(self, label, url, ttl_hours, *, rotate=False):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,95}", label):
            raise ValueError("invalid credential label")
        if not 1 <= ttl_hours <= 24 * 90:
            raise ValueError("credential lifetime must be 1 to 2160 hours")
        token = "mp_" + secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours)
        params = {
            "label": label,
            "namespace": "client_" + uuid4().hex,
            "digest": _digest(token),
            "expires_at": expires_at,
        }
        if rotate:
            query = """
                UPDATE mcp_credentials SET token_digest=:digest, expires_at=:expires_at,
                    revoked_at=NULL WHERE label=:label RETURNING namespace
            """
        else:
            query = """
                INSERT INTO mcp_credentials(label, namespace, token_digest, expires_at)
                VALUES (:label, :namespace, :digest, :expires_at)
                ON CONFLICT (label) DO NOTHING RETURNING namespace
            """
        try:
            with self.engine.begin() as connection:
                namespace = connection.execute(text(query), params).scalar_one_or_none()
        except SQLAlchemyError:
            raise MemoryStorageError("unable to issue MCP credential") from None
        if namespace is None:
            message = "credential label does not exist" if rotate else "label already exists"
            raise ValueError(message)
        return {
            "label": label,
            "namespace": namespace,
            "transport": "streamable-http",
            "url": url,
            "expires_at": expires_at.isoformat(),
            "headers": {"Authorization": f"Bearer {token}"},
        }

    def revoke(self, label):
        try:
            with self.engine.begin() as connection:
                return (
                    connection.execute(
                        text("""
                    UPDATE mcp_credentials SET revoked_at=clock_timestamp()
                    WHERE label=:label AND revoked_at IS NULL
                """),
                        {"label": label},
                    ).rowcount
                    == 1
                )
        except SQLAlchemyError:
            raise MemoryStorageError("unable to revoke MCP credential") from None

    def resolve(self, token):
        if not re.fullmatch(r"mp_[A-Za-z0-9_-]{43}", token):
            return None
        try:
            with self.engine.connect() as connection:
                return (
                    connection.execute(
                        text("""
                    SELECT label, namespace, expires_at FROM mcp_credentials
                    WHERE token_digest=:digest AND revoked_at IS NULL
                        AND expires_at > clock_timestamp()
                """),
                        {"digest": _digest(token)},
                    )
                    .mappings()
                    .one_or_none()
                )
        except SQLAlchemyError:
            raise MemoryStorageError("unable to validate MCP credential") from None

    def contexts(self):
        try:
            with self.engine.connect() as connection:
                namespaces = (
                    connection.execute(
                        text("""
                    SELECT namespace FROM mcp_credentials
                    WHERE revoked_at IS NULL AND expires_at > clock_timestamp()
                    ORDER BY namespace
                """)
                    )
                    .scalars()
                    .all()
                )
        except SQLAlchemyError:
            raise MemoryStorageError("unable to list MCP namespaces") from None
        return tuple(AccessContext(namespace, namespace, namespace) for namespace in namespaces)
