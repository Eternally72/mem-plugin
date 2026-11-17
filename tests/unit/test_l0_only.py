from unittest.mock import Mock

import pytest

from mem_plugin.config.settings import (
    ConfigurationError,
    load_consolidation_settings,
    load_engine_settings,
)
from mem_plugin.config.test_runs import runtime_snapshot
from mem_plugin.engine.errors import InvalidMemoryInput
from mem_plugin.engine.models import AccessContext
from mem_plugin.engine.services.notes import NoteService
from mem_plugin.observability.tracing import Tracer


def test_l0_mode_is_effective_and_frozen_in_test_profiles():
    env = {"CONSOLIDATION_ENABLED": "true"}
    assert load_engine_settings(environ=env).l0_only
    assert not load_consolidation_settings(environ=env).enabled
    assert runtime_snapshot(env)["retrieval"]["l0_only"] is True
    env["MEM_PLUGIN_L0_ONLY"] = "false"
    assert load_consolidation_settings(environ=env).enabled
    assert runtime_snapshot(env)["retrieval"]["l0_only"] is False
    with pytest.raises(ConfigurationError):
        load_engine_settings(environ={"MEM_PLUGIN_L0_ONLY": "maybe"})


def test_explicit_hierarchy_writes_are_blocked_before_storage():
    uow = Mock()
    memory = NoteService(uow, Tracer(Mock()), l0_only=True, consolidation_enabled=True)
    access = AccessContext("user", "agent")
    with pytest.raises(InvalidMemoryInput, match="disables hierarchy"):
        memory.derive(access, "summary", 1, ())
    with pytest.raises(InvalidMemoryInput, match="disables hierarchy"):
        memory.link_hierarchy(access, "child", "parent")
    with pytest.raises(InvalidMemoryInput, match="disables hierarchy"):
        memory.publish(access, "summary", 1, "new", (), "refresh")
    uow.assert_not_called()
