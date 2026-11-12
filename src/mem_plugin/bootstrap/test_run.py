"""Operator launcher for repeatable Agent tests; no new MCP administration tools."""

import argparse
import json
import socket
import sys
from dataclasses import asdict, replace

from mem_plugin.config.settings import (
    ConfigurationError,
    load_database_settings,
    load_environment,
    load_test_runs_directory,
)
from mem_plugin.config.test_runs import RunStore, bind_environment, create_profile
from mem_plugin.engine.errors import MemoryError
from mem_plugin.engine.models import AccessContext


def _show(store, profile):
    result = asdict(profile)
    result.pop("token")
    result["namespace"] = profile.namespace
    result["connection_file"] = str(store.directory(profile.run_id) / "connection.json")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create, bind and clean isolated Agent test runs")
    parser.add_argument("--runs-dir", help="Private profile directory (default TEST_RUNS_DIR)")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--name", default="agent-test")
    create.add_argument("--port", type=int, help="Loopback port (default: select a free port)")
    commands.add_parser("list")
    for name in ("show", "start", "serve", "worker", "scheduler", "clean"):
        sub = commands.add_parser(name)
        sub.add_argument("run_id")
        if name in {"worker", "scheduler"}:
            sub.add_argument("--once", action="store_true")
        if name == "start":
            sub.add_argument(
                "--api-only", action="store_true", help="Preflight without model tasks"
            )
    args = parser.parse_args(argv)
    try:
        env = load_environment()
        store = RunStore(args.runs_dir or load_test_runs_directory(environ=env))
        if args.command == "create":
            # No database writes. Validate schema before issuing a usable connection profile.
            from mem_plugin.infrastructure.persistence.postgres.database import Database

            port = args.port
            if port is None:
                used = {p.mcp_url for p in store.list() if p.status != "cleaned"}
                for _ in range(50):
                    with socket.socket() as reservation:
                        reservation.bind(("127.0.0.1", 0))
                        port = reservation.getsockname()[1]
                    if f"http://127.0.0.1:{port}/mcp" not in used:
                        break
                else:
                    raise ConfigurationError("unable to select a distinct test endpoint")
            profile = create_profile(env, args.name, port)
            database = Database(load_database_settings(environ=env).url)
            try:
                database.check_schema()
            finally:
                database.close()
            store.create(profile)
            print(json.dumps(_show(store, profile), ensure_ascii=False, indent=2))
            return
        if args.command == "list":
            print(json.dumps([_show(store, p) for p in store.list()], ensure_ascii=False, indent=2))
            return
        if args.command == "show":
            print(json.dumps(_show(store, store.load(args.run_id)), ensure_ascii=False, indent=2))
            return
        with store.lock(args.run_id, exclusive=args.command == "clean"):
            profile = store.load(args.run_id)
            if args.command == "clean" and profile.status == "cleaned":
                print(
                    json.dumps({"run_id": profile.run_id, "status": "cleaned", "deleted_count": 0})
                )
                return
            bound = bind_environment(
                profile,
                env,
                allow_cleaning=args.command == "clean",
                check_runtime=args.command != "clean",
            )
            access = AccessContext(
                profile.user_id, profile.agent_id, profile.namespace, frozenset(profile.domains)
            )
            if args.command == "start":
                from mem_plugin.bootstrap.runtime import check_database, supervise

                check_database(bound)
                supervise(
                    {
                        role: [
                            sys.executable,
                            "-m",
                            "mem_plugin.bootstrap.test_run",
                            "--runs-dir",
                            str(store.root),
                            command,
                            profile.run_id,
                        ]
                        for role, command in (
                            ("api", "serve"),
                            ("worker", "worker"),
                            ("scheduler", "scheduler"),
                        )
                        if not args.api_only or role == "api"
                    },
                    env=bound,
                    directory=store.directory(profile.run_id),
                    url=profile.mcp_url,
                    token=profile.token,
                )
            elif args.command == "serve":
                from mem_plugin.bootstrap.api import main as serve

                serve([], environ=bound)
            elif args.command == "worker":
                from mem_plugin.bootstrap.worker import main as worker

                worker(["--once"] if args.once else [], environ=bound, task_access=access)
            elif args.command == "scheduler":
                from mem_plugin.bootstrap.scheduler import main as scheduler

                scheduler(["--once"] if args.once else [], environ=bound)
            else:
                from mem_plugin.bootstrap.container import create_note_service

                store.save(replace(profile, status="cleaning"))
                with create_note_service(
                    load_database_settings(environ=bound).url, use_models=False, environ=bound
                ) as memory:
                    count = memory.forget_all(access)
                store.save(replace(profile, status="cleaned"))
                result = {"run_id": profile.run_id, "status": "cleaned", "deleted_count": count}
                store.report(profile.run_id, result)
                print(json.dumps(result))
    except (ConfigurationError, MemoryError, OSError):
        # Keep operator diagnostics, but never print raw filesystem/SQL connection exceptions.
        error = sys.exception()
        message = (
            str(error)
            if isinstance(error, (ConfigurationError, MemoryError))
            else "test profile filesystem operation failed"
        )
        parser.exit(1, message + "\n")


if __name__ == "__main__":
    main()
