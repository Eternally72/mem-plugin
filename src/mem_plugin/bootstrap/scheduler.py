"""Independent scheduler process; no HTTP endpoint and no LLM credentials required."""

import argparse
import logging
import signal
from threading import Event

from mem_plugin.bootstrap.container import create_scheduler
from mem_plugin.config.logging import configure_logging
from mem_plugin.config.settings import ConfigurationError, load_database_settings
from mem_plugin.engine.errors import MemoryError

logger = logging.getLogger("mem_plugin.bootstrap.scheduler")


def main(argv=None, *, environ=None):
    parser = argparse.ArgumentParser(
        description="Enqueue bounded consolidation and lifecycle tasks"
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--namespace", help="Select one configured namespace")
    parser.add_argument("--memory-id", help="Explicitly refresh one L1-L3 memory")
    args = parser.parse_args(argv)
    configure_logging()
    stop = Event()
    handlers = {}
    try:
        with create_scheduler(
            load_database_settings(environ=environ).url, namespace=args.namespace, environ=environ
        ) as scheduler:
            logger.info("memory_scheduler_started")
            if args.memory_id:
                scheduler.request(args.memory_id)
                return
            for sig in (signal.SIGINT, signal.SIGTERM):
                handlers[sig] = signal.signal(sig, lambda *_: stop.set())
            while not stop.is_set():
                try:
                    count = scheduler.run_once(force=args.once)
                    if count:
                        logger.info("memory_tasks_enqueued=%s", count)
                except MemoryError:
                    if args.once:
                        parser.exit(1, "Unable to enqueue memory tasks\n")
                    logger.error("scheduler_storage_unavailable")
                if args.once:
                    break
                stop.wait(1)
    except (ConfigurationError, MemoryError, ValueError) as exc:
        parser.exit(1, f"Unable to initialize scheduler: {exc}\n")
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    main()
