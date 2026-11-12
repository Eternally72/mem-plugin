"""Explicit, private experiment profiles. This module does not open the database."""

import fcntl
import hashlib
import json
import os
import re
import secrets
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from mem_plugin.config.settings import (
    ConfigurationError,
    load_consolidation_settings,
    load_database_settings,
    load_embedding_settings,
    load_engine_settings,
    load_graph_settings,
    load_lifecycle_settings,
    load_llm_settings,
    load_rerank_settings,
    load_scheduler_settings,
    load_settings,
    load_worker_settings,
)


def database_identity(url):
    parsed = urlsplit(url)
    identity = [parsed.hostname, parsed.port or 5432, parsed.path, parsed.username, parsed.query]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def runtime_snapshot(env):
    # Keep effective non-secret settings, not raw environment values or API credentials.
    loaders = {
        "embedding": load_embedding_settings,
        "llm": load_llm_settings,
        "rerank": load_rerank_settings,
        "retrieval": load_engine_settings,
        "graph": load_graph_settings,
        "consolidation": load_consolidation_settings,
        "lifecycle": load_lifecycle_settings,
        "worker": load_worker_settings,
    }
    snapshot = {}
    for name, loader in loaders.items():
        snapshot[name] = {
            k: v
            for k, v in asdict(loader(environ=env)).items()
            if k not in {"api_key", "archive_dir"}
        }
    scheduler = load_scheduler_settings(environ=env)
    snapshot["scheduler"] = {"intervals": scheduler.intervals, "batch_size": scheduler.batch_size}
    return json.loads(json.dumps(snapshot))


@dataclass(frozen=True, slots=True)
class RunProfile:
    run_id: str
    name: str
    created_at: str
    user_id: str
    agent_id: str
    domains: tuple[str, ...]
    database: str
    archive_dir: str
    runtime: dict[str, object]
    mcp_url: str
    token: str = field(repr=False)
    status: str = "active"
    format: int = 1

    @property
    def namespace(self):
        return "test_run_" + self.run_id

    def __post_init__(self):
        if not re.fullmatch("[0-9a-f]{32}", self.run_id):
            raise ConfigurationError("invalid test run ID")
        if self.format != 1 or self.status not in {"active", "cleaning", "cleaned"}:
            raise ConfigurationError("invalid test run profile state")
        if not self.name.strip() or not self.user_id.strip() or not self.agent_id.strip():
            raise ConfigurationError("test run identity must not be empty")
        if not re.fullmatch("[0-9a-f]{64}", self.database):
            raise ConfigurationError("invalid test database identity")
        if not re.fullmatch("[A-Za-z0-9_-]{43}", self.token):
            raise ConfigurationError("invalid test bearer credential")
        url = urlsplit(self.mcp_url)
        if (
            url.scheme != "http"
            or url.hostname != "127.0.0.1"
            or url.path != "/mcp"
            or url.query
            or url.fragment
            or url.username
            or not url.port
        ):
            raise ConfigurationError("test endpoint must be a loopback HTTP /mcp URL")
        if not Path(self.archive_dir).is_absolute():
            raise ConfigurationError("test archive directory must be absolute")


def create_profile(env, name, port):
    if not 1 <= port <= 65535 or not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
        raise ConfigurationError("test name must be 1..120 characters and port must be 1..65535")
    # Reuse normal validation, substituting the newly issued local credential and URL.
    token = secrets.token_urlsafe(32)
    url = f"http://127.0.0.1:{port}/mcp"
    settings = load_settings(
        environ={
            **env,
            "MEM_PLUGIN_MCP_TOKEN": token,
            "MEM_PLUGIN_MCP_URL": url,
            "MEM_PLUGIN_HOST": "127.0.0.1",
            "MEM_PLUGIN_PORT": str(port),
        }
    )
    return RunProfile(
        uuid4().hex,
        name.strip(),
        datetime.now(UTC).isoformat(),
        settings.user_id,
        settings.agent_id,
        tuple(sorted(settings.domains)),
        database_identity(settings.database_url),
        str(load_lifecycle_settings(environ=env).archive_dir.absolute()),
        runtime_snapshot(env),
        url,
        token,
    )


def bind_environment(profile, env, *, allow_cleaning=False, check_runtime=True):
    if profile.status != "active" and not (allow_cleaning and profile.status == "cleaning"):
        raise ConfigurationError("test run is closed; create a new run")
    settings = load_scheduler_settings(environ=env)
    if database_identity(load_database_settings(environ=env).url) != profile.database:
        raise ConfigurationError("test run database mismatch; refusing to connect")
    if (settings.user_id, settings.agent_id, tuple(sorted(settings.domains))) != (
        profile.user_id,
        profile.agent_id,
        profile.domains,
    ):
        raise ConfigurationError("test run user, agent or domain authorization mismatch")
    if str(load_lifecycle_settings(environ=env).archive_dir.absolute()) != profile.archive_dir:
        raise ConfigurationError("test run archive storage mismatch")
    if check_runtime and runtime_snapshot(env) != profile.runtime:
        raise ConfigurationError("test run model or policy settings changed; create a new run")
    return {
        **env,
        "MEM_PLUGIN_USER_ID": profile.user_id,
        "MEM_PLUGIN_AGENT_ID": profile.agent_id,
        "MEM_PLUGIN_NAMESPACES": profile.namespace,
        "MEM_PLUGIN_HOST": "127.0.0.1",
        "MEM_PLUGIN_PORT": str(urlsplit(profile.mcp_url).port),
        "MEM_PLUGIN_MCP_URL": profile.mcp_url,
        "MEM_PLUGIN_MCP_TOKEN": profile.token,
        "ARCHIVE_DIR": profile.archive_dir,
    }


def _private_json(path, data):
    temporary = path.with_name("." + path.name + "." + uuid4().hex + ".tmp")
    try:
        with os.fdopen(
            os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w"
        ) as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class RunStore:
    def __init__(self, root):
        self.root = Path(root).absolute()

    def directory(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch("[0-9a-f]{32}", run_id):
            raise ConfigurationError("invalid test run ID")
        directory = self.root / run_id
        if directory.is_symlink():
            raise ConfigurationError("test run directory cannot be a symlink")
        return directory

    def create(self, profile):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = self.directory(profile.run_id)
        directory.mkdir(mode=0o700)
        self.save(profile)
        _private_json(
            directory / "connection.json",
            {
                "transport": "streamable-http",
                "url": profile.mcp_url,
                "headers": {"Authorization": "Bearer " + profile.token},
            },
        )

    def save(self, profile):
        _private_json(self.directory(profile.run_id) / "run.json", asdict(profile))

    def load(self, run_id):
        path = self.directory(run_id) / "run.json"
        if path.is_symlink():
            raise ConfigurationError("test profile cannot be a symlink")
        try:
            with path.open() as source:
                data = json.load(source)
            data["domains"] = tuple(data["domains"])
            profile = RunProfile(**data)
            if profile.run_id != run_id:
                raise ValueError
            return profile
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise ConfigurationError("test profile is missing or invalid") from None

    def list(self):
        if not self.root.exists():
            return []
        return [
            self.load(p.name)
            for p in sorted(self.root.iterdir())
            if p.is_dir() and re.fullmatch("[0-9a-f]{32}", p.name)
        ]

    @contextmanager
    def lock(self, run_id, *, exclusive=False):
        path = self.directory(run_id) / ".lock"
        try:
            with os.fdopen(
                os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a"
            ) as lock:
                try:
                    fcntl.flock(
                        lock, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
                    )
                except BlockingIOError:
                    raise ConfigurationError(
                        "test run is in use; stop its API/Worker/Scheduler before cleanup"
                    ) from None
                yield
        except OSError:
            raise ConfigurationError("unable to lock test run profile") from None

    def report(self, run_id, data):
        _private_json(self.directory(run_id) / "report.json", data)
