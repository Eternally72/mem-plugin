"""HTTP process entry point: configure, compose, then serve the application."""

import argparse
import logging
from collections.abc import Sequence
from contextlib import ExitStack
from importlib import import_module

from mem_plugin.bootstrap.container import create_note_service
from mem_plugin.config.logging import configure_logging
from mem_plugin.config.settings import ConfigurationError, load_settings
from mem_plugin.engine.errors import MemoryStorageError
from mem_plugin.engine.models import AccessContext


def main(argv: Sequence[str] | None = None, *, environ=None) -> None:
    parser = argparse.ArgumentParser(description="Personal memory MCP service over Streamable HTTP")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--mcp-url")
    args = parser.parse_args(argv)
    try:
        for dependency in ("dotenv", "pgvector", "psycopg", "sqlalchemy"):
            import_module(dependency)

        from mem_plugin.infrastructure.persistence.postgres.credentials import CredentialStore
        from mem_plugin.infrastructure.persistence.postgres.database import Database
        from mem_plugin.interfaces.http.server import run_server
        from mem_plugin.interfaces.mcp.context import ToolIdentity
    except ModuleNotFoundError as exc:
        if exc.name not in {
            "dotenv",
            "fastmcp",
            "pydantic",
            "starlette",
            "uvicorn",
            "sqlalchemy",
            "psycopg",
            "pgvector",
        }:
            raise
        parser.exit(1, "Service dependencies missing. Run: uv sync --extra mcp --extra postgres\n")
    try:
        settings = load_settings(
            host=args.host, port=args.port, mcp_url=args.mcp_url, environ=environ
        )
    except ConfigurationError as exc:
        parser.error(str(exc))
    configure_logging()
    try:
        with ExitStack() as resources:
            engine = resources.enter_context(
                create_note_service(settings.database_url, environ=environ)
            )
            database = Database(settings.database_url)
            resources.callback(database.close)
            logging.getLogger("mem_plugin.bootstrap.api").info(
                "memory_api_started url=%s namespaces=%s llm_provider=%s llm_model=%s",
                settings.mcp_url,
                ",".join(settings.namespaces),
                settings.llm.provider if settings.llm else "none",
                settings.llm.model if settings.llm else "none",
            )
            identity = ToolIdentity(
                AccessContext(
                    settings.user_id, settings.agent_id, settings.namespaces[0], settings.domains
                ),
                frozenset(settings.namespaces),
            )
            run_server(
                engine,
                host=settings.host,
                port=settings.port,
                mcp_url=settings.mcp_url,
                token=settings.mcp_token,
                identity=identity,
                resolve_scoped=CredentialStore(database.engine).resolve,
            )
    except MemoryStorageError as exc:
        parser.exit(1, f"Unable to initialize memory service: {exc}\n")


if __name__ == "__main__":
    main()
