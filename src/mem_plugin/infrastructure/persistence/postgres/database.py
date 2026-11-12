"""Connection pool ownership and read-only schema readiness checks."""

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError

from mem_plugin.engine.errors import MemoryStorageError

SCHEMA_REVISION = "0010"


class Database:
    def __init__(self, url: str):
        parsed = make_url(url)
        if parsed.drivername not in {"postgresql", "postgresql+psycopg"}:
            raise ValueError("PostgreSQL is required")
        self.engine: Engine = create_engine(
            parsed.set(drivername="postgresql+psycopg"),
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            connect_args={"connect_timeout": 5},
            hide_parameters=True,
        )

    def check_schema(self) -> None:
        try:
            with self.engine.connect() as connection:
                revisions = (
                    connection.execute(text("SELECT version_num FROM alembic_version"))
                    .scalars()
                    .all()
                )
                if revisions != [SCHEMA_REVISION]:
                    raise MemoryStorageError(
                        "database schema is incompatible; run the independent migrations"
                    )
        except SQLAlchemyError as exc:
            raise MemoryStorageError(
                "database is not ready; check connection and migrations"
            ) from exc

    def close(self) -> None:
        self.engine.dispose()
