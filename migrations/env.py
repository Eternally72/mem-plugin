"""Independent migration entry point; runtime processes never import this module."""

from alembic import context
from sqlalchemy import create_engine

from mem_plugin.config.settings import load_database_settings

connection = context.config.attributes.get("connection")


def migrate(active_connection):
    context.configure(connection=active_connection, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("Migrations require a PostgreSQL connection")
elif connection is not None:
    migrate(connection)
else:
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool

    settings = load_database_settings()
    url = make_url(settings.url).set(drivername="postgresql+psycopg")
    engine = create_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        with engine.connect() as active_connection:
            migrate(active_connection)
    finally:
        engine.dispose()
