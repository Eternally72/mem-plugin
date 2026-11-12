"""Local process supervision; business rules remain in the existing three processes."""

import argparse
import asyncio
import fcntl
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from threading import Event
from urllib.parse import urlsplit

from mem_plugin.config.logging import configure_logging
from mem_plugin.config.settings import (
    ConfigurationError,
    load_environment,
    load_settings,
    load_test_runs_directory,
)
from mem_plugin.config.test_runs import _private_json
from mem_plugin.engine.errors import MemoryError

logger = logging.getLogger("mem_plugin.bootstrap.runtime")


def _relay_log(role, source):
    """Forward bounded, newly appended application log lines; SDK output stays private."""
    for _ in range(64):
        line = source.readline(8192)
        if not line:
            break
        if line.startswith("[mem_plugin."):
            print(f"[{role}] {line}", end="", file=sys.stderr, flush=True)


def check_port(url, *, host=None, port=None):
    parsed = urlsplit(url)
    host = parsed.hostname if host is None else host
    port = (parsed.port or (443 if parsed.scheme == "https" else 80)) if port is None else port
    try:
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as sock:
            sock.bind((host, port))
    except OSError:
        raise ConfigurationError(
            "MCP port is already in use or unavailable; no service was stopped"
        ) from None


async def _probe(url, token):
    from fastmcp import Client

    try:
        async with Client(url, auth=token, timeout=1, init_timeout=1) as client:
            names = {tool.name for tool in await client.list_tools()}
            return {
                "memory.record",
                "memory.search",
                "memory.get",
                "memory.feedback",
                "memory.forget",
            } <= names
    except Exception:
        # SDK exceptions may contain request headers. Only a readiness verdict is public.
        return False


def check_database(env):
    from mem_plugin.config.settings import load_database_settings
    from mem_plugin.infrastructure.persistence.postgres.database import Database

    database = Database(load_database_settings(environ=env).url)
    try:
        database.check_schema()
    finally:
        database.close()


def supervise(commands, *, env, directory, url, token, startup_timeout=45, stop=None, listen=None):
    """Own all child processes until interrupted; private status supports Agent preflight."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    configure_logging()
    children, previous, readers = {}, {}, {}
    stop = stop if stop is not None else Event()
    state = "starting"
    with ExitStack() as resources:
        lock = resources.enter_context(
            os.fdopen(
                os.open(directory / "runtime.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600),
                "a",
            )
        )
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigurationError("this memory runtime is already running") from None
        check_port(url, **({"host": listen[0], "port": listen[1]} if listen else {}))

        def status():
            _private_json(
                directory / "runtime.json",
                {
                    "status": state,
                    "pid": os.getpid(),
                    "children": {role: child.pid for role, child in children.items()},
                },
            )

        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, lambda *_: stop.set())
            status()
            logger.info("memory_runtime_starting url=%s logs=%s", url, directory)
            for role, command in commands.items():
                log = resources.enter_context(
                    os.fdopen(
                        os.open(
                            directory / (role + ".log"),
                            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                            0o600,
                        ),
                        "a",
                    )
                )
                readers[role] = resources.enter_context(
                    (directory / (role + ".log")).open(encoding="utf-8", errors="replace")
                )
                readers[role].seek(0, os.SEEK_END)
                children[role] = subprocess.Popen(
                    command,
                    env={**env, "PYTHONUNBUFFERED": "1"},
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                logger.info("memory_process_started role=%s pid=%s", role, children[role].pid)
            deadline = time.monotonic() + startup_timeout
            last_heartbeat = time.monotonic()
            while not stop.is_set():
                for role, reader in readers.items():
                    _relay_log(role, reader)
                for role, child in children.items():
                    if child.poll() is not None:
                        raise ConfigurationError(
                            f"memory {role} exited; inspect {directory / (role + '.log')}"
                        )
                if state == "starting":
                    if asyncio.run(_probe(url, token)):
                        state = "ready"
                        status()
                        logger.info("memory_runtime_ready url=%s", url)
                        print(
                            json.dumps({"status": "ready", "url": url, "logs": str(directory)}),
                            flush=True,
                        )
                    elif time.monotonic() >= deadline:
                        raise ConfigurationError(
                            f"MCP startup timed out; inspect logs in {directory}"
                        )
                if time.monotonic() - last_heartbeat >= 30:
                    logger.info(
                        "memory_runtime_alive status=%s processes=%s", state, ",".join(children)
                    )
                    last_heartbeat = time.monotonic()
                stop.wait(0.25)
            state = "stopped"
        except BaseException:
            state = "failed"
            raise
        finally:
            # Terminate only children created here. Give bounded in-flight jobs time to finish.
            for child in children.values():
                if child.poll() is None:
                    child.terminate()
            deadline = time.monotonic() + 45
            for child in children.values():
                try:
                    child.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            for role, reader in readers.items():
                _relay_log(role, reader)
            status()
            logger.info("memory_runtime_stopped status=%s", state)
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Start local MCP, Worker and Scheduler together")
    parser.parse_args(argv)
    try:
        env = load_environment()
        settings = load_settings(environ=env)
        check_database(env)  # Read only. Migrations remain a separate operator command.
        supervise(
            {
                role: [sys.executable, "-m", "mem_plugin.bootstrap." + role]
                for role in ("api", "worker", "scheduler")
            },
            env=env,
            directory=load_test_runs_directory(environ=env).parent / "runtime",
            url=settings.mcp_url,
            token=settings.mcp_token,
            listen=(settings.host, settings.port),
        )
    except (ConfigurationError, MemoryError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
