import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from mem_plugin.bootstrap.api import main


@pytest.mark.parametrize(
    "overrides", [{"MEM_PLUGIN_PORT": "0"}, {"MEM_PLUGIN_MCP_TOKEN": ""}, {"DATABASE_URL": ""}]
)
def test_invalid_configuration_never_opens_a_database(tmp_path, monkeypatch, overrides):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://unit@localhost/test")
    monkeypatch.setenv("MEM_PLUGIN_MCP_TOKEN", "unit-token-" * 4)
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    factory = Mock()
    monkeypatch.setattr("mem_plugin.bootstrap.api.create_note_service", factory)
    with pytest.raises(SystemExit) as error:
        main([])
    assert error.value.code == 2
    factory.assert_not_called()


def test_core_imports_without_frameworks_or_drivers():
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import sys
sys.path.insert(0, sys.argv[1])
from mem_plugin.engine.api import NoteAPI
from mem_plugin.engine.services.notes import NoteService
assert not any(name in sys.modules for name in ('fastmcp','pydantic','sqlalchemy','psycopg'))
assert not any(name.startswith('mem_plugin.infrastructure') for name in sys.modules)
"""
    subprocess.run(
        [sys.executable, "-S", "-c", script, str(source)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_missing_dependencies_have_actionable_error():
    source = Path(__file__).resolve().parents[2] / "src"
    script = (
        "import sys; sys.path.insert(0,sys.argv[1]); "
        "from mem_plugin.bootstrap.api import main; main([])"
    )
    result = subprocess.run(
        [sys.executable, "-S", "-c", script, str(source)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "uv sync --extra mcp --extra postgres" in result.stderr
