"""Operator interface for issuing, rotating and revoking scoped bearer credentials."""

import argparse
import json
from pathlib import Path

from mem_plugin.config.private_file import write_private_json
from mem_plugin.config.settings import ConfigurationError, load_settings
from mem_plugin.engine.errors import MemoryStorageError
from mem_plugin.infrastructure.persistence.postgres.credentials import CredentialStore
from mem_plugin.infrastructure.persistence.postgres.database import Database


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("issue", "rotate"):
        command = commands.add_parser(name)
        command.add_argument("--label", required=True)
        command.add_argument("--output", required=True, type=Path)
        command.add_argument("--ttl-hours", type=int, default=168)
    revoke = commands.add_parser("revoke")
    revoke.add_argument("--label", required=True)
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
        database = Database(settings.database_url)
        try:
            database.check_schema()
            store = CredentialStore(database.engine)
            if args.command == "revoke":
                print(json.dumps({"revoked": store.revoke(args.label)}))
                return
            if args.output.is_symlink():
                raise ValueError("connection file cannot be a symlink")
            if args.output.exists():
                if args.command == "issue":
                    raise ValueError("connection file already exists")
                if json.loads(args.output.read_text()).get("label") != args.label:
                    raise ValueError("connection file belongs to a different label")
            args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            connection = store.issue(
                args.label, settings.mcp_url, args.ttl_hours, rotate=args.command == "rotate"
            )
            write_private_json(args.output, connection)
            print(json.dumps({k: v for k, v in connection.items() if k != "headers"}))
        finally:
            database.close()
    except (ConfigurationError, MemoryStorageError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
    except OSError:
        parser.exit(1, "unable to write the private MCP connection file\n")
