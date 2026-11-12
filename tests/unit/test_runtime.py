"""The launcher owns child lifetimes, never another listener or database data."""

import json
import signal
from unittest.mock import Mock

import pytest

from mem_plugin.bootstrap import runtime
from mem_plugin.config.settings import ConfigurationError


def test_failed_child_stops_siblings_and_records_safe_status(tmp_path, monkeypatch):
    children = [Mock(pid=1), Mock(pid=2), Mock(pid=3)]
    for child in children:
        child.poll.return_value = None
        child.wait.return_value = 0
    children[1].poll.return_value = 1
    monkeypatch.setattr(runtime.subprocess, "Popen", Mock(side_effect=children))
    monkeypatch.setattr(runtime, "check_port", lambda url: None)
    with pytest.raises(ConfigurationError, match="worker exited"):
        runtime.supervise(
            {role: [role] for role in ("api", "worker", "scheduler")},
            env={"SECRET": "never-persist"},
            directory=tmp_path,
            url="http://127.0.0.1:8101/mcp",
            token="private-token",
        )
    children[0].terminate.assert_called_once()
    children[2].terminate.assert_called_once()
    status = json.loads((tmp_path / "runtime.json").read_text())
    assert status["status"] == "failed"
    assert "never-persist" not in json.dumps(status) and "private-token" not in json.dumps(status)
    assert (tmp_path / "runtime.json").stat().st_mode & 0o777 == 0o600
    assert signal.getsignal(signal.SIGTERM) != runtime.supervise


def test_occupied_port_does_not_spawn_or_terminate_anything(tmp_path, monkeypatch):
    def occupied(url):
        raise ConfigurationError("MCP port is already in use")

    monkeypatch.setattr(runtime, "check_port", occupied)
    spawn = Mock()
    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    with pytest.raises(ConfigurationError, match="already in use"):
        runtime.supervise(
            {}, env={}, directory=tmp_path, url="http://127.0.0.1:8101/mcp", token="private-token"
        )
    spawn.assert_not_called()
