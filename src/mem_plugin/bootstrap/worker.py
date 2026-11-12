"""Independent polling process; signals stop after the current bounded request."""

import argparse
import logging
import signal
from collections.abc import Sequence
from threading import Event

from mem_plugin.bootstrap.container import create_worker
from mem_plugin.config.logging import configure_logging
from mem_plugin.config.settings import (
    ConfigurationError,
    load_database_settings,
    load_worker_settings,
)
from mem_plugin.engine.errors import MemoryStorageError

logger = logging.getLogger("mem_plugin.bootstrap.worker")


def main(argv: Sequence[str] | None = None, *, environ=None, task_access=None) -> None:
    parser = argparse.ArgumentParser(description="Process PostgreSQL memory tasks")
    parser.add_argument("--once", action="store_true", help="Claim at most one available task")
    args = parser.parse_args(argv)
    configure_logging()
    stop = Event()
    previous = {}
    try:
        database, settings = (
            load_database_settings(environ=environ),
            load_worker_settings(environ=environ),
        )
        with create_worker(database.url, environ=environ, task_access=task_access) as worker:
            logger.info(
                "memory_worker_started poll_seconds=%s namespace=%s",
                settings.poll_seconds,
                task_access.namespace if task_access else "configured",
            )
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, lambda *_: stop.set())
            while not stop.is_set():
                try:
                    outcome = worker.run_once()
                    if outcome:
                        logger.info("task_outcome=%s", outcome)
                except MemoryStorageError:
                    logger.error("worker_storage_unavailable")
                    if args.once:
                        parser.exit(1, "Unable to access task storage\n")
                    outcome = None
                if args.once:
                    break
                if outcome is None:
                    stop.wait(settings.poll_seconds)
    except (ConfigurationError, MemoryStorageError) as exc:
        parser.exit(1, f"Unable to initialize worker: {exc}\n")
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    main()
