import runpy
from pathlib import Path

from dotenv import dotenv_values

from mem_plugin.config.settings import load_settings


def test_initialization_generates_once_and_preserves_existing_values(tmp_path: Path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "init_env.py"
    initialize_env = runpy.run_path(str(script))["initialize_env"]
    (tmp_path / ".env.example").write_text(
        "MEM_PLUGIN_MCP_URL=http://127.0.0.1:8000/mcp\nMEM_PLUGIN_MCP_TOKEN=\n"
    )
    (tmp_path / ".env").write_text("MEM_PLUGIN_MCP_URL=https://example.com/agent/mcp\n")
    assert initialize_env(tmp_path) == ["MEM_PLUGIN_MCP_TOKEN"]
    settings = load_settings(
        environ={"DATABASE_URL": "postgresql://unit@localhost/test"}, env_file=tmp_path / ".env"
    )
    assert settings.mcp_url == "https://example.com/agent/mcp"
    original_token = settings.mcp_token
    assert initialize_env(tmp_path) == []
    assert dotenv_values(tmp_path / ".env")["MEM_PLUGIN_MCP_TOKEN"] == original_token
    assert (tmp_path / ".env").stat().st_mode & 0o077 == 0


def test_initializer_removes_retired_storage_and_preserves_model_credentials(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "init_env.py"
    initialize_env = runpy.run_path(str(script))["initialize_env"]
    (tmp_path / ".env.example").write_text("DEEPSEEK_LLM_API_KEY=\nBAILIAN_LLM_MODEL=\n")
    (tmp_path / ".env").write_text(
        "MEM_PLUGIN_DATABASE=retired-file\nDEEPSEEK_LLM_API_KEY=private-existing-key\n"
    )
    initialize_env(tmp_path)
    values = dotenv_values(tmp_path / ".env")
    assert "MEM_PLUGIN_DATABASE" not in values
    assert values["DEEPSEEK_LLM_API_KEY"] == "private-existing-key"
    assert values["BAILIAN_LLM_MODEL"] == ""
    assert initialize_env(tmp_path) == []
