"""Populate missing local settings and a private token without printing credential values."""

import secrets
from pathlib import Path
from urllib.parse import quote

from dotenv import dotenv_values, set_key, unset_key


def initialize_env(directory: Path) -> list[str]:
    template = dotenv_values(directory / ".env.example", interpolate=False)
    target = directory / ".env"
    target.touch(exist_ok=True)
    target.chmod(0o600)
    current = dotenv_values(target, interpolate=False)
    if "MEM_PLUGIN_DATABASE" in current:
        unset_key(target, "MEM_PLUGIN_DATABASE")
    lines = target.read_text().splitlines(keepends=True)
    target.write_text(
        "".join(
            line
            for line in lines
            if not (line.lstrip().startswith("#") and "sqlite" in line.lower())
        )
    )
    added = []
    for key, default in template.items():
        generated = {
            "MEM_PLUGIN_MCP_TOKEN",
            "POSTGRES_PASSWORD",
            "DATABASE_URL",
            "DATABASE_URL_DOCKER",
        }
        if current.get(key) or (key in current and default == "" and key not in generated):
            continue
        if key in {"MEM_PLUGIN_MCP_TOKEN", "POSTGRES_PASSWORD"}:
            value = secrets.token_urlsafe(32)
        elif key in {"DATABASE_URL", "DATABASE_URL_DOCKER"}:
            user = quote(current.get("POSTGRES_USER") or "mem_plugin", safe="")
            password = quote(current.get("POSTGRES_PASSWORD") or "", safe="")
            database = quote(current.get("POSTGRES_DB") or "mem_plugin", safe="")
            port = current.get("MEM_PLUGIN_POSTGRES_PORT") or "55433"
            address = "postgres:5432" if key == "DATABASE_URL_DOCKER" else f"127.0.0.1:{port}"
            value = f"postgresql+psycopg://{user}:{password}@{address}/{database}"
        else:
            value = default
        if value is None:
            continue
        set_key(target, key, value, quote_mode="auto")
        current[key] = value
        added.append(key)
    target.chmod(0o600)
    return added


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    added = initialize_env(root)
    print("Initialized .env; credential values are hidden.")
    print("Added keys:", ", ".join(added) if added else "none (existing values preserved)")
