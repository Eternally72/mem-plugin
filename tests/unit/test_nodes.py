from dataclasses import replace
from datetime import datetime
from unittest.mock import Mock

import pytest

from mem_plugin.config.settings import (
    ConfigurationError,
    load_database_settings,
    load_embedding_settings,
)
from mem_plugin.engine.errors import InvalidMemoryInput
from mem_plugin.engine.models import AccessContext, MemoryNode
from mem_plugin.observability.context import current_trace
from mem_plugin.observability.tracing import Tracer


@pytest.mark.parametrize("level", [-1, 4, True, 1.5])
def test_node_level_validation(level):
    with pytest.raises(InvalidMemoryInput):
        MemoryNode(user_id="u", agent_id="a", namespace="n", content="fact", level=level)


def test_unknown_fact_times_are_not_invented():
    node = MemoryNode(user_id="u", agent_id="a", namespace="n", content="fact")
    assert node.node_kind == "atomic"
    assert replace(node, level=3).node_kind == "derived"
    assert node.occurred_at is node.valid_from is node.valid_until is node.expires_at is None
    with pytest.raises(InvalidMemoryInput):
        replace(node, occurred_at=datetime(2026, 1, 1))


def test_database_config_is_independent_of_mcp_and_does_not_expose_secrets():
    settings = load_database_settings(
        environ={"DATABASE_URL": "postgresql://user:secret@localhost/test"}
    )
    assert "secret" not in repr(settings)
    with pytest.raises(ConfigurationError):
        load_database_settings(environ={})
    with pytest.raises(ConfigurationError):
        load_database_settings(environ={"DATABASE_URL": "sqlite:///file"})
    assert load_embedding_settings(environ={}).dimension == 1024
    with pytest.raises(ConfigurationError, match="migration"):
        load_embedding_settings(environ={"EMBEDDING_DIMENSION": "768"})


def test_trace_parentage_context_reset_and_failure_visibility(caplog):
    sink = Mock()
    tracer = Tracer(sink)
    with tracer.span("outer", "u", "n", "a") as outer:
        with tracer.span("inner", "u", "n", "a") as inner:
            assert inner.trace_id == outer.trace_id
            assert inner.parent_span_id == outer.span_id
        assert current_trace.get() == outer
    assert current_trace.get() is None
    assert sink.write.call_count == 4
    sink.write.side_effect = RuntimeError("secret must not leak")
    with tracer.span("operation", "u", "n", "a"):
        pass
    assert "trace_persistence_failed" in caplog.text
    assert "secret must not leak" not in caplog.text


def test_context_domains_are_frozen_and_identity_is_required():
    domains = {"work"}
    access = AccessContext("u", "a", domains=domains)
    domains.add("other")
    assert access.domains == frozenset({"work"})
    with pytest.raises(InvalidMemoryInput):
        AccessContext("", "a")
