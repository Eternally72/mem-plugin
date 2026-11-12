from pathlib import Path

import pytest

from mem_plugin.config.settings import ConfigurationError, load_settings

TOKEN = "unit-test-token-" * 3


def test_cli_overrides_invalid_environment(tmp_path: Path):
    settings = load_settings(
        host="127.0.0.1",
        port=8001,
        environ={
            "DATABASE_URL": "postgresql://unit@localhost/test",
            "MEM_PLUGIN_PORT": "invalid",
            "MEM_PLUGIN_HOST": "",
            "MEM_PLUGIN_MCP_TOKEN": TOKEN,
        },
    )
    assert settings.database_url == "postgresql://unit@localhost/test"
    assert settings.host == "127.0.0.1"
    assert settings.port == 8001


def test_environment_and_defaults(tmp_path: Path):
    settings = load_settings(
        environ={
            "DATABASE_URL": "postgresql://unit@localhost/test",
            "MEM_PLUGIN_MCP_TOKEN": TOKEN,
        }
    )
    assert settings.database_url == "postgresql://unit@localhost/test"
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000


@pytest.mark.parametrize(
    "environ",
    [
        {"MEM_PLUGIN_PORT": "not-a-number"},
        {"MEM_PLUGIN_PORT": "0"},
        {"MEM_PLUGIN_PORT": "65536"},
        {"MEM_PLUGIN_HOST": " "},
        {"DATABASE_URL": " "},
    ],
)
def test_invalid_configuration_is_rejected(environ):
    with pytest.raises(ConfigurationError):
        load_settings(
            environ={
                "DATABASE_URL": "postgresql://unit@localhost/test",
                "MEM_PLUGIN_MCP_TOKEN": TOKEN,
                **environ,
            }
        )


def test_dotenv_loads_and_environment_takes_precedence(tmp_path: Path):
    path = tmp_path / ".env"
    path.write_text(
        f"MEM_PLUGIN_MCP_TOKEN={TOKEN}\n"
        "MEM_PLUGIN_MCP_URL=https://example.com/agent/mcp\n"
        "MEM_PLUGIN_PORT=8001\n"
    )
    settings = load_settings(
        environ={"MEM_PLUGIN_PORT": "8002", "DATABASE_URL": "postgresql://unit@localhost/test"},
        env_file=path,
    )
    assert settings.port == 8002
    assert settings.mcp_url == "https://example.com/agent/mcp"
    assert settings.mcp_path == "/agent/mcp"
    assert settings.mcp_token == TOKEN
    assert TOKEN not in repr(settings)


@pytest.mark.parametrize(
    "url",
    [
        "not-a-url",
        "ftp://example.com/mcp",
        "http://localhost",
        "http://localhost/health",
        "http://localhost/mcp?token=secret",
        "http://user:password@localhost/mcp",
        "http://localhost/mcp#fragment",
    ],
)
def test_invalid_mcp_urls(url: str):
    with pytest.raises(ConfigurationError, match="MEM_PLUGIN_MCP_URL"):
        load_settings(
            environ={
                "DATABASE_URL": "postgresql://unit@localhost/test",
                "MEM_PLUGIN_MCP_TOKEN": TOKEN,
                "MEM_PLUGIN_MCP_URL": url,
            }
        )


@pytest.mark.parametrize("token", ["", "short", "contains spaces " * 3, "密钥" * 20])
def test_invalid_or_missing_token_fails_closed(token: str):
    with pytest.raises(ConfigurationError, match="MEM_PLUGIN_MCP_TOKEN"):
        load_settings(
            environ={
                "DATABASE_URL": "postgresql://unit@localhost/test",
                "MEM_PLUGIN_MCP_TOKEN": token,
            }
        )


def test_binding_is_independent_from_public_url():
    settings = load_settings(
        environ={
            "DATABASE_URL": "postgresql://unit@localhost/test",
            "MEM_PLUGIN_HOST": "0.0.0.0",
            "MEM_PLUGIN_PORT": "8000",
            "MEM_PLUGIN_MCP_URL": "https://agent.example.com/mcp",
            "MEM_PLUGIN_MCP_TOKEN": TOKEN,
        }
    )
    assert settings.host == "0.0.0.0"
    assert settings.port == 8000
    assert settings.mcp_url == "https://agent.example.com/mcp"
