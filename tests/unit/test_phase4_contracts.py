import json
from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from mem_plugin.config.settings import (
    ConfigurationError,
    load_consolidation_settings,
    load_scheduler_settings,
)
from mem_plugin.engine.errors import InvalidMemoryInput, ModelProviderError
from mem_plugin.engine.models import ConsolidationPolicy, MemoryNode, Scope
from mem_plugin.engine.services.consolidation import group_id, parse_groups
from mem_plugin.scheduler.runner import Scheduler


def test_group_identity_is_stable_scoped_and_independent_of_evidence_order():
    node = MemoryNode(user_id="user", agent_id="agent", namespace="ns", content="fact")
    assert group_id(node, 1, "coffee") == group_id(
        replace(node, id=uuid4(), content="other"), 1, "coffee"
    )
    assert group_id(node, 1, "coffee") != group_id(replace(node, agent_id="other"), 1, "coffee")
    assert group_id(node, 1, "coffee") != group_id(node, 2, "coffee")
    global_node = replace(node, scope=Scope.USER_GLOBAL)
    assert group_id(global_node, 1, "coffee") == group_id(
        replace(global_node, agent_id="other"), 1, "coffee"
    )
    assert group_id(node, 1, "coffee") != group_id(replace(node, namespace="other"), 1, "coffee")


def raw_group(target, **updates):
    return {
        "key": "coffee",
        "content": "supported summary",
        "source_ids": [str(target)],
        "confidence": 0.8,
        "reason": "supported",
        "conflict_resolution": "no conflict",
        **updates,
    }


@pytest.mark.parametrize(
    "update",
    [
        {"source_ids": [str(uuid4())]},
        {"source_ids": []},
        {"source_ids": "not a list"},
        {"confidence": True},
        {"confidence": float("nan")},
        {"confidence": 1.2},
        {"key": "../escape"},
        {"key": "x" * 81},
        {"content": ""},
        {"content": "x" * 6001},
        {"reason": ""},
        {"conflict_resolution": {}},
        {"extra": "instruction"},
    ],
)
def test_consolidation_rejects_unsupported_or_malformed_model_fields(update):
    target = uuid4()
    with pytest.raises(ModelProviderError) as error:
        parse_groups(
            json.dumps({"groups": [raw_group(target, **update)]}),
            frozenset({target}),
            ConsolidationPolicy(),
        )
    assert error.value.retryable is False


def test_consolidation_valid_empty_and_duplicate_groups():
    target = uuid4()
    policy = ConsolidationPolicy()
    assert parse_groups('{"groups":[]}', frozenset({target}), policy) == ()
    result = parse_groups(json.dumps({"groups": [raw_group(target)]}), frozenset({target}), policy)
    assert result[0].source_ids == (target,)
    for value in ('{"groups":[],"groups":[]}', json.dumps({"groups": [raw_group(target)] * 2})):
        with pytest.raises(ModelProviderError):
            parse_groups(value, frozenset({target}), policy)


@pytest.mark.parametrize(
    "env",
    [
        {"CONSOLIDATION_ENABLED": "yes"},
        {"CONSOLIDATION_L3_MIN_EVIDENCE": "1"},
        {"CONSOLIDATION_L1_MIN_SOURCES": "0"},
        {"CONSOLIDATION_EVIDENCE_LIMIT": "101"},
        {"CONSOLIDATION_MAX_DISTANCE": "nan"},
        {"CONSOLIDATION_MAX_OUTPUT_TOKENS": "10000"},
    ],
)
def test_consolidation_configuration_rejects_unbounded_values(env):
    with pytest.raises(ConfigurationError):
        load_consolidation_settings(environ=env)


def test_scheduler_namespace_configuration_and_no_model_dependency():
    settings = load_scheduler_settings(
        environ={"MEM_PLUGIN_NAMESPACES": "personal,work", "MEM_PLUGIN_DOMAINS": "life"}
    )
    assert settings.namespaces == ("personal", "work") and settings.intervals == (
        3600,
        86400,
        604800,
    )
    for env in (
        {"SCHEDULER_L1_INTERVAL_SECONDS": "0"},
        {"MEM_PLUGIN_NAMESPACES": ""},
        {"SCHEDULER_BATCH_SIZE": "201"},
    ):
        with pytest.raises(ConfigurationError):
            load_scheduler_settings(environ=env)
    service = Mock()
    service.enqueue_due.return_value = 1
    runner = Scheduler(service, ("one",), (60, 120, 180))
    assert runner.run_once() == 3 and runner.run_once() == 0
    assert service.enqueue_due.call_count == 3
    with pytest.raises(ValueError):
        Scheduler(service, ("one", "two"), (60, 120, 180)).request("some-id")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_sources": (1, 2, 3)},
        {"min_evidence": (2, 3)},
        {"candidate_limit": 1},
        {"evidence_limit": 1000},
    ],
)
def test_core_policy_enforces_fixed_three_transitions_and_budgets(kwargs):
    with pytest.raises(InvalidMemoryInput):
        ConsolidationPolicy(**kwargs)
