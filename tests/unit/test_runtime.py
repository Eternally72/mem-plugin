"""The launcher owns child lifetimes, never another listener or database data."""

import json
import signal
from io import StringIO
from unittest.mock import Mock

import pytest

from mem_plugin.bootstrap import runtime
from mem_plugin.config import logging as log_config
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


def test_terminal_relay_colors_fields_without_touching_source(monkeypatch):
    line = "[mem_plugin.bootstrap.worker] 2026-11-20T19:00:00+0800 WARNING task_outcome=failed\n"
    terminal = StringIO()
    terminal.isatty = lambda: True
    monkeypatch.setattr(log_config.sys, "stderr", terminal)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    source = StringIO(line)
    runtime._relay_log("worker", source)
    rendered = terminal.getvalue()
    assert "\x1b[35m[worker]\x1b[0m" in rendered
    assert "\x1b[33mWARNING\x1b[0m" in rendered
    assert "\x1b[36mtask_outcome\x1b[0m" in rendered
    assert source.getvalue() == line
    monkeypatch.setenv("NO_COLOR", "1")
    assert log_config.terminal_line(line, role="worker") == "[worker] " + line
