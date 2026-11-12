import json
from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from mem_plugin.config.settings import ConfigurationError, load_graph_settings
from mem_plugin.engine.errors import InvalidMemoryInput, ModelProviderError
from mem_plugin.engine.models import MemoryNode, Scope
from mem_plugin.engine.services.graph import parse_decisions, same_audience
from mem_plugin.infrastructure.models.clients import ChatClient


def decision(target, **updates):
    return {
        "target_id": str(target),
        "relation_type": "same_entity",
        "confidence": 0.9,
        "context_note": "Explicitly mention the same coffee preference.",
        **updates,
    }


def test_strict_json_contract_accepts_only_known_candidates_and_safe_fields():
    target = uuid4()
    value = json.dumps({"decisions": [decision(target)]})
    assert parse_decisions(value, frozenset({target}))[0].target_id == target
    assert parse_decisions("```json\n" + value + "\n```", frozenset({target}))[0].confidence == 0.9
    assert parse_decisions('{"decisions":[]}', frozenset({target})) == ()


@pytest.mark.parametrize(
    "update",
    [
        {"target_id": str(uuid4())},
        {"relation_type": "supersedes"},
        {"confidence": True},
        {"confidence": float("nan")},
        {"confidence": 1.1},
        {"context_note": "x" * 301},
        {"context_note": ""},
        {"content": "overwrite"},
        {"context_note": "Bearer " + "x" * 40},
        {"relation_type": []},
    ],
)
def test_unsafe_model_batch_is_rejected(update):
    target = uuid4()
    with pytest.raises(ModelProviderError) as error:
        parse_decisions(
            json.dumps({"decisions": [decision(target, **update)]}), frozenset({target})
        )
    assert error.value.retryable is False


def test_duplicate_decisions_keys_and_non_json_are_rejected():
    target = uuid4()
    for value in (
        "not json",
        '{"decisions":[],"decisions":[]}',
        json.dumps({"decisions": [decision(target), decision(target)]}),
        json.dumps({"decisions": [], "content": "rewrite"}),
    ):
        with pytest.raises(ModelProviderError):
            parse_decisions(value, frozenset({target}))


@pytest.mark.parametrize(
    "env",
    [
        {"GRAPH_ENABLED": "yes"},
        {"GRAPH_CANDIDATE_LIMIT": "11"},
        {"GRAPH_MAX_DISTANCE": "nan"},
        {"GRAPH_MIN_CONFIDENCE": "-1"},
        {"GRAPH_MAX_OUTPUT_TOKENS": "100000"},
        {"GRAPH_MAX_INPUT_CHARACTERS": "0"},
    ],
)
def test_graph_configuration_is_bounded(env):
    with pytest.raises(ConfigurationError):
        load_graph_settings(environ=env)


def test_matching_audience_prevents_context_widening():
    node = MemoryNode(user_id="u", namespace="n", agent_id="a", content="fact")
    assert same_audience(node, replace(node, id=uuid4()))
    assert not same_audience(node, replace(node, agent_id="b"))
    assert not same_audience(node, replace(node, scope=Scope.USER_GLOBAL))
    shared = replace(node, scope=Scope.DOMAIN_SHARED, domain="work")
    assert not same_audience(shared, replace(shared, domain="life"))
    assert same_audience(shared, replace(shared, agent_id="b"))


def test_chat_budget_is_sent_and_truncation_is_an_explicit_failure():
    transport = Mock(
        return_value={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
    )
    client = ChatClient("bailian", "https://example.com/v1", "unit-key", "kimi-k3", transport)
    assert (
        client.complete([{"role": "user", "content": "test"}], max_output_tokens=256).text == "{}"
    )
    assert transport.call_args.args[2]["max_tokens"] == 256
    with pytest.raises(InvalidMemoryInput):
        client.complete([{"role": "user", "content": "test"}], max_output_tokens=100000)
    transport.return_value = {
        "choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]
    }
    with pytest.raises(ModelProviderError, match="budget") as error:
        client.complete([{"role": "user", "content": "test"}], max_output_tokens=128)
    assert error.value.retryable is False
