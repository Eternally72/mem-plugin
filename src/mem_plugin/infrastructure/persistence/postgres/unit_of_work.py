"""Explicit commit; every uncommitted exit rolls back and closes the connection."""

from sqlalchemy.exc import SQLAlchemyError

from mem_plugin.engine.errors import MemoryStorageError
from mem_plugin.engine.models import AccessContext
from mem_plugin.infrastructure.jobs.postgres_outbox import OutboxRepository
from mem_plugin.infrastructure.observability.postgres_store import AuditRepository
from mem_plugin.infrastructure.persistence.postgres.consolidation import ConsolidationRepository
from mem_plugin.infrastructure.persistence.postgres.database import Database
from mem_plugin.infrastructure.persistence.postgres.lifecycle import LifecycleRepository
from mem_plugin.infrastructure.persistence.postgres.repositories import (
    GraphRepository,
    NodeRepository,
    VersionRepository,
)
from mem_plugin.infrastructure.vector.pgvector_store import VectorRepository


class PostgresUnitOfWork:
    def __init__(
        self,
        database: Database,
        access: AccessContext,
        embedding_model: str = "qwen3.7-text-embedding-flash",
        embedding_dimension: int = 1024,
    ):
        self.database, self.access = database, access
        self.embedding_model, self.embedding_dimension = embedding_model, embedding_dimension

    def __enter__(self):
        try:
            self.connection = self.database.engine.connect()
            self.transaction = self.connection.begin()
        except SQLAlchemyError as exc:
            if hasattr(self, "connection"):
                self.connection.close()
            raise MemoryStorageError("unable to begin memory transaction") from exc
        self.nodes = NodeRepository(self.connection, self.access)
        self.lifecycle = LifecycleRepository(self.connection, self.access, self.nodes)
        self.consolidation = ConsolidationRepository(self.connection, self.access)
        self.graph = GraphRepository(self.connection, self.access, self.nodes)
        self.versions = VersionRepository(self.connection, self.access, self.nodes)
        self.audit = AuditRepository(self.connection, self.access)
        self.outbox = OutboxRepository(self.connection, self.access)
        self.vectors = VectorRepository(
            self.connection, self.access, self.nodes, self.embedding_model, self.embedding_dimension
        )
        return self

    def commit(self):
        try:
            self.transaction.commit()
        except SQLAlchemyError as exc:
            raise MemoryStorageError("memory transaction could not be committed") from exc

    def __exit__(self, exc_type, exc, traceback):
        try:
            if self.transaction.is_active:
                self.transaction.rollback()
        finally:
            self.connection.close()
        if isinstance(exc, SQLAlchemyError):
            raise MemoryStorageError("memory storage operation failed") from exc
