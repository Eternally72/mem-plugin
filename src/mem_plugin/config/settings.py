"""Process configuration without dependencies on the engine or any connector."""

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    pass


def _environment(environ: Mapping[str, str] | None, env_file: Path | None) -> dict[str, str]:
    env: dict[str, str] = {}
    if env_file is not None or environ is None:
        path = env_file if env_file is not None else Path.cwd() / ".env"
        if path.is_file():
            from dotenv import dotenv_values

            env.update(
                {
                    key: value
                    for key, value in dotenv_values(path, interpolate=False).items()
                    if value is not None
                }
            )
    env.update(os.environ if environ is None else environ)
    return env


def load_environment(*, environ=None, env_file=None) -> dict[str, str]:
    """Resolve one explicit runtime snapshot for launchers; never mutate os.environ."""
    return _environment(environ, env_file)


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    url: str = field(repr=False)

    def __post_init__(self) -> None:
        try:
            parsed = urlsplit(self.url)
            valid = (
                parsed.scheme in {"postgresql", "postgresql+psycopg"}
                and parsed.hostname
                and parsed.username
                and parsed.path not in {"", "/"}
                and not parsed.fragment
                and (parsed.port is None or 1 <= parsed.port <= 65535)
                and not any(char.isspace() for char in self.url)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ConfigurationError("DATABASE_URL must be a PostgreSQL connection URL")


def load_database_settings(
    *, environ: Mapping[str, str] | None = None, env_file: Path | None = None
) -> DatabaseSettings:
    return DatabaseSettings(_environment(environ, env_file).get("DATABASE_URL", ""))


@dataclass(frozen=True, slots=True)
class EmbeddingSettings:
    model: str = "qwen3.7-text-embedding-flash"
    dimension: int = 1024
    provider: str = "bailian"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key: str = field(default="", repr=False)

    def __post_init__(self):
        if self.provider != "bailian":
            raise ConfigurationError("EMBEDDING_PROVIDER must be bailian")
        _model_url(self.base_url)
        if self.model != "qwen3.7-text-embedding-flash" or self.dimension != 1024:
            raise ConfigurationError(
                "embedding model/dimension differs from schema; a migration is required"
            )


def load_embedding_settings(
    *, environ: Mapping[str, str] | None = None, env_file: Path | None = None
) -> EmbeddingSettings:
    env = _environment(environ, env_file)
    try:
        dimension = int(env.get("EMBEDDING_DIMENSION", "1024"))
    except ValueError as exc:
        raise ConfigurationError("EMBEDDING_DIMENSION must be an integer") from exc
    return EmbeddingSettings(
        env.get("EMBEDDING_MODEL", "qwen3.7-text-embedding-flash"),
        dimension,
        env.get("EMBEDDING_PROVIDER", "bailian"),
        env.get("EMBEDDING_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        env.get("EMBEDDING_API_KEY", ""),
    )


def _model_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and (parsed.port is None or 1 <= parsed.port <= 65535)
            and not any(char.isspace() for char in value)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ConfigurationError("model Base URL must be HTTP(S), without credentials or query")


@dataclass(frozen=True, slots=True)
class ModelSettings:
    provider: str
    base_url: str
    model: str
    api_key: str = field(default="", repr=False)
    timeout_seconds: float = 30
    enable_thinking: bool | None = None
    reasoning_effort: str | None = None
    temperature: float | None = None

    def __post_init__(self):
        if self.provider not in {"bailian", "deepseek", "openai_compatible"}:
            raise ConfigurationError("unsupported LLM provider")
        _model_url(self.base_url)
        if not 1 <= self.timeout_seconds <= 300:
            raise ConfigurationError("model timeout must be 1..300 seconds")
        if self.enable_thinking is not None and type(self.enable_thinking) is not bool:
            raise ConfigurationError("enable_thinking requires a boolean setting")
        if self.reasoning_effort is not None and (
            not isinstance(self.reasoning_effort, str)
            or self.reasoning_effort not in {"low", "high", "max"}
        ):
            raise ConfigurationError("reasoning_effort must be blank, low, high or max")


def load_llm_settings(
    *,
    provider: str | None = None,
    environ: Mapping[str, str] | None = None,
    env_file: Path | None = None,
) -> ModelSettings:
    env = _environment(environ, env_file)
    selected = provider if provider is not None else env.get("LLM_PROVIDER", "deepseek")
    if selected == "openai_compatible":
        return ModelSettings(
            provider=selected,
            base_url=env.get("MEM_PLUGIN_OPENAI_BASE_URL", ""),
            model=env.get("MEM_PLUGIN_OPENAI_MODEL", ""),
            api_key=env.get("MEM_PLUGIN_OPENAI_API_KEY", ""),
            timeout_seconds=_number(env, "MEM_PLUGIN_OPENAI_TIMEOUT_SECONDS", 90, float, 1, 300),
            temperature=_number(env, "MEM_PLUGIN_OPENAI_TEMPERATURE", 0, float, 0, 2),
        )
    if selected not in {"bailian", "deepseek"}:
        raise ConfigurationError("LLM_PROVIDER must be bailian, deepseek or openai_compatible")
    prefix = selected.upper() + "_LLM_"
    default_url = (
        "https://api.deepseek.com"
        if selected == "deepseek"
        else "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    thinking = (
        env.get(prefix + "ENABLE_THINKING", "false" if selected == "deepseek" else "")
        .strip()
        .lower()
    )
    if thinking not in {"", "true", "false"}:
        raise ConfigurationError(prefix + "ENABLE_THINKING must be blank, true or false")
    return ModelSettings(
        selected,
        env.get(prefix + "BASE_URL", default_url),
        env.get(prefix + "MODEL", "deepseek-flash" if selected == "deepseek" else ""),
        env.get(prefix + "API_KEY", ""),
        _number(env, prefix + "TIMEOUT_SECONDS", 90, float, 1, 300),
        None if not thinking else thinking == "true",
        env.get(prefix + "REASONING_EFFORT", "").strip() or None,
    )


def load_rerank_settings(
    *, environ: Mapping[str, str] | None = None, env_file: Path | None = None
) -> ModelSettings:
    env = _environment(environ, env_file)
    if env.get("RERANK_PROVIDER", "bailian") != "bailian":
        raise ConfigurationError("RERANK_PROVIDER must be bailian")
    return ModelSettings(
        "bailian",
        env.get("RERANK_BASE_URL", "https://dashscope.aliyuncs.com/api/v1"),
        env.get("RERANK_MODEL", "qwen3.7-text-rerank"),
        env.get("RERANK_API_KEY", ""),
    )


def _number(env, name, default, kind, minimum, maximum):
    try:
        value = kind(env.get(name, str(default)))
        if not minimum <= value <= maximum:
            raise ValueError
        return value
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be {minimum}..{maximum}") from exc


@dataclass(frozen=True, slots=True)
class EngineSettings:
    max_note_characters: int = 16000
    candidate_limit: int = 50
    rrf_k: int = 60
    trigram_threshold: float = 0.2
    rerank_enabled: bool = False


def load_engine_settings(*, environ=None, env_file=None) -> EngineSettings:
    env = _environment(environ, env_file)
    enabled = env.get("RETRIEVAL_RERANK_ENABLED", "false").lower()
    if enabled not in {"true", "false"}:
        raise ConfigurationError("RETRIEVAL_RERANK_ENABLED must be true or false")
    return EngineSettings(
        _number(env, "MEM_PLUGIN_MAX_NOTE_CHARACTERS", 16000, int, 1, 16000),
        _number(env, "RETRIEVAL_CANDIDATE_LIMIT", 50, int, 1, 200),
        _number(env, "RETRIEVAL_RRF_K", 60, int, 1, 10000),
        _number(env, "RETRIEVAL_TRIGRAM_THRESHOLD", 0.2, float, 0.01, 1),
        enabled == "true",
    )


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    lease_seconds: float = 120
    poll_seconds: float = 1
    max_attempts: int = 3
    retry_seconds: float = 5


def load_worker_settings(*, environ=None, env_file=None) -> WorkerSettings:
    env = _environment(environ, env_file)
    return WorkerSettings(
        _number(env, "WORKER_LEASE_SECONDS", 120, float, 60, 3600),
        _number(env, "WORKER_POLL_SECONDS", 1, float, 0.1, 60),
        _number(env, "WORKER_MAX_ATTEMPTS", 3, int, 1, 20),
        _number(env, "WORKER_RETRY_SECONDS", 5, float, 1, 300),
    )


@dataclass(frozen=True, slots=True)
class GraphSettings:
    enabled: bool = True
    candidate_limit: int = 5
    max_distance: float = 0.35
    min_confidence: float = 0.75
    max_input_characters: int = 12000
    max_output_tokens: int = 1024


def load_graph_settings(*, environ=None, env_file=None) -> GraphSettings:
    env = _environment(environ, env_file)
    enabled = env.get("GRAPH_ENABLED", "true").lower()
    if enabled not in {"true", "false"}:
        raise ConfigurationError("GRAPH_ENABLED must be true or false")
    return GraphSettings(
        enabled == "true",
        _number(env, "GRAPH_CANDIDATE_LIMIT", 5, int, 1, 10),
        _number(env, "GRAPH_MAX_DISTANCE", 0.35, float, 0, 2),
        _number(env, "GRAPH_MIN_CONFIDENCE", 0.75, float, 0, 1),
        _number(env, "GRAPH_MAX_INPUT_CHARACTERS", 12000, int, 1000, 32000),
        _number(env, "GRAPH_MAX_OUTPUT_TOKENS", 1024, int, 128, 4096),
    )


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str = field(repr=False)
    host: str
    port: int
    mcp_url: str
    mcp_token: str = field(repr=False)
    user_id: str = "personal-user"
    agent_id: str = "personal-agent"
    namespaces: tuple[str, ...] = ("default",)
    domains: frozenset[str] = frozenset()
    embedding: EmbeddingSettings = field(default_factory=EmbeddingSettings)
    llm: ModelSettings | None = None
    rerank: ModelSettings | None = None

    def __post_init__(self) -> None:
        DatabaseSettings(self.database_url)
        if not self.user_id.strip() or not self.agent_id.strip() or not self.namespaces:
            raise ConfigurationError("MCP identity and allowed namespaces must not be blank")
        if not self.host.strip():
            raise ConfigurationError("host must not be blank")
        if not 1 <= self.port <= 65535:
            raise ConfigurationError("port must be between 1 and 65535")
        try:
            url = urlsplit(self.mcp_url)
            valid_url = (
                url.scheme in {"http", "https"}
                and url.hostname
                and url.path not in {"", "/", "/health"}
                and not url.username
                and not url.password
                and not url.query
                and not url.fragment
                and (url.port is None or 1 <= url.port <= 65535)
                and not any(char.isspace() for char in self.mcp_url)
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ConfigurationError(
                "MEM_PLUGIN_MCP_URL must be an HTTP(S) endpoint URL such as http://127.0.0.1:8000/mcp"
            )
        if not re.fullmatch(r"[A-Za-z0-9._~+/-]{32,}={0,2}", self.mcp_token):
            raise ConfigurationError(
                "MEM_PLUGIN_MCP_TOKEN must be at least 32 bearer-token characters; "
                "run scripts/init_env.py to initialize .env"
            )

    @property
    def mcp_path(self) -> str:
        return urlsplit(self.mcp_url).path


def load_settings(
    *,
    host: str | None = None,
    port: int | None = None,
    mcp_url: str | None = None,
    environ: Mapping[str, str] | None = None,
    env_file: Path | None = None,
) -> Settings:
    """Resolve CLI > environment > .env > defaults; explicit mappings isolate tests by default."""
    env = _environment(environ, env_file)
    try:
        port_value = port if port is not None else int(env.get("MEM_PLUGIN_PORT", "8000"))
    except ValueError as exc:
        raise ConfigurationError("port must be an integer") from exc
    host_value = host if host is not None else env.get("MEM_PLUGIN_HOST", "127.0.0.1")
    client_host = "127.0.0.1" if host_value == "0.0.0.0" else host_value
    client_host = "::1" if client_host == "::" else client_host
    if ":" in client_host:
        client_host = f"[{client_host}]"
    return Settings(
        database_url=env.get("DATABASE_URL", ""),
        host=host_value,
        port=port_value,
        mcp_url=mcp_url
        if mcp_url is not None
        else env.get("MEM_PLUGIN_MCP_URL", f"http://{client_host}:{port_value}/mcp"),
        mcp_token=env.get("MEM_PLUGIN_MCP_TOKEN", ""),
        user_id=env.get("MEM_PLUGIN_USER_ID", "personal-user"),
        agent_id=env.get("MEM_PLUGIN_AGENT_ID", "personal-agent"),
        namespaces=tuple(
            x.strip() for x in env.get("MEM_PLUGIN_NAMESPACES", "default").split(",") if x.strip()
        ),
        domains=frozenset(
            x.strip() for x in env.get("MEM_PLUGIN_DOMAINS", "").split(",") if x.strip()
        ),
        embedding=load_embedding_settings(environ=env),
        llm=load_llm_settings(environ=env),
        rerank=load_rerank_settings(environ=env),
    )


@dataclass(frozen=True, slots=True)
class ConsolidationSettings:
    enabled: bool = True
    min_sources: tuple[int, int, int] = (2, 2, 2)
    min_evidence: tuple[int, int, int] = (2, 3, 5)
    candidate_limit: int = 12
    evidence_limit: int = 50
    max_distance: float = 0.45
    max_groups: int = 3
    min_confidence: float = 0.75
    max_input_characters: int = 24000
    max_output_tokens: int = 2048


def load_consolidation_settings(*, environ=None, env_file=None):
    env = _environment(environ, env_file)
    enabled = env.get("CONSOLIDATION_ENABLED", "true").lower()
    if enabled not in {"true", "false"}:
        raise ConfigurationError("CONSOLIDATION_ENABLED must be true or false")
    return ConsolidationSettings(
        enabled == "true",
        tuple(
            _number(env, f"CONSOLIDATION_L{level}_MIN_SOURCES", 2, int, 2, 100)
            for level in range(1, 4)
        ),
        tuple(
            _number(env, f"CONSOLIDATION_L{level}_MIN_EVIDENCE", default, int, 2, 100)
            for level, default in enumerate((2, 3, 5), 1)
        ),
        _number(env, "CONSOLIDATION_CANDIDATE_LIMIT", 12, int, 2, 30),
        _number(env, "CONSOLIDATION_EVIDENCE_LIMIT", 50, int, 2, 100),
        _number(env, "CONSOLIDATION_MAX_DISTANCE", 0.45, float, 0, 2),
        _number(env, "CONSOLIDATION_MAX_GROUPS", 3, int, 1, 5),
        _number(env, "CONSOLIDATION_MIN_CONFIDENCE", 0.75, float, 0, 1),
        _number(env, "CONSOLIDATION_MAX_INPUT_CHARACTERS", 24000, int, 1000, 64000),
        _number(env, "CONSOLIDATION_MAX_OUTPUT_TOKENS", 2048, int, 128, 4096),
    )


@dataclass(frozen=True, slots=True)
class SchedulerSettings:
    user_id: str
    agent_id: str
    namespaces: tuple[str, ...]
    domains: frozenset[str]
    intervals: tuple[float, float, float]
    batch_size: int


def load_scheduler_settings(*, environ=None, env_file=None):
    env = _environment(environ, env_file)
    user, agent = (
        env.get("MEM_PLUGIN_USER_ID", "personal-user"),
        env.get("MEM_PLUGIN_AGENT_ID", "personal-agent"),
    )
    namespaces = tuple(
        v.strip() for v in env.get("MEM_PLUGIN_NAMESPACES", "default").split(",") if v.strip()
    )
    if not user.strip() or not agent.strip() or not namespaces:
        raise ConfigurationError("scheduler identity and namespace allowlist are required")
    return SchedulerSettings(
        user,
        agent,
        namespaces,
        frozenset(v.strip() for v in env.get("MEM_PLUGIN_DOMAINS", "").split(",") if v.strip()),
        tuple(
            _number(env, f"SCHEDULER_L{level}_INTERVAL_SECONDS", default, float, 1, 2592000)
            for level, default in enumerate((3600, 86400, 604800), 1)
        ),
        _number(env, "SCHEDULER_BATCH_SIZE", 50, int, 1, 200),
    )


@dataclass(frozen=True, slots=True)
class LifecycleSettings:
    enabled: bool
    archive_dir: Path
    max_bytes: int
    interval_seconds: float
    decay_interval_seconds: int
    restore_grace_days: int
    type_policies: dict[str, dict[str, object]]


def load_lifecycle_settings(*, environ=None, env_file=None):
    env = _environment(environ, env_file)
    enabled = env.get("LIFECYCLE_ENABLED", "true").lower()
    if enabled not in {"true", "false"}:
        raise ConfigurationError("LIFECYCLE_ENABLED must be true or false")
    directory = env.get("ARCHIVE_DIR", "data/archive")
    if not directory.strip():
        raise ConfigurationError("ARCHIVE_DIR must not be blank")
    try:
        policies = json.loads(env.get("LIFECYCLE_TYPE_POLICIES", "{}"))
        if not isinstance(policies, dict):
            raise ValueError
        for name, rule in policies.items():
            if not name.strip() or not isinstance(rule, dict) or not rule:
                raise ValueError
            for key, value in rule.items():
                if key == "retention_action":
                    if value not in {"archive", "forget"}:
                        raise ValueError
                elif (
                    key
                    not in {"review_after_days", "archive_after_days", "confidence_half_life_days"}
                    or type(value) is not int
                    or not 0 <= value <= 36500
                ):
                    raise ValueError
    except (ValueError, TypeError):
        raise ConfigurationError(
            "LIFECYCLE_TYPE_POLICIES must contain valid per-type policies"
        ) from None
    return LifecycleSettings(
        enabled == "true",
        Path(directory),
        _number(env, "ARCHIVE_MAX_BYTES", 8388608, int, 1024, 67108864),
        _number(env, "LIFECYCLE_INTERVAL_SECONDS", 3600, float, 1, 2592000),
        _number(env, "LIFECYCLE_DECAY_INTERVAL_SECONDS", 86400, int, 60, 2592000),
        _number(env, "LIFECYCLE_RESTORE_GRACE_DAYS", 7, int, 0, 36500),
        policies,
    )
