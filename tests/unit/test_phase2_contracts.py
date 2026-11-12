from dataclasses import replace
from datetime import datetime
from uuid import UUID

import pytest

from mem_plugin.config.settings import (
    ConfigurationError,
    load_engine_settings,
    load_worker_settings,
)
from mem_plugin.engine.errors import InvalidMemoryInput
from mem_plugin.engine.models import MemoryNode, SearchFilters
from mem_plugin.engine.services.retrieval import fuse


def test_rrf_uses_ranks_deduplicates_and_breaks_ties_deterministically():
    a = MemoryNode(user_id="u", agent_id="a", namespace="n", content="A", id=UUID(int=1))
    b = replace(a, id=UUID(int=2), content="B")
    result = fuse([a, a, b], [(b, 0.001), (a, 0.999)], 60)
    assert [hit.node.id for hit in result] == [a.id, b.id]
    assert result[0].score == result[1].score == 1 / 61 + 1 / 62
    assert result[0].channel_scores["vector"] == 0.999
    assert result[0].channels == ("lexical", "vector")


@pytest.mark.parametrize("value", [None, datetime(2025, 1, 1), "2025-01-01"])
def test_filter_clock_requires_explicit_timezone(value):
    with pytest.raises(InvalidMemoryInput):
        SearchFilters(as_of=value)


@pytest.mark.parametrize(
    "load,env",
    [
        (load_engine_settings, {"RETRIEVAL_CANDIDATE_LIMIT": "201"}),
        (load_engine_settings, {"RETRIEVAL_RERANK_ENABLED": "yes"}),
        (load_engine_settings, {"RETRIEVAL_TRIGRAM_THRESHOLD": "nan"}),
        (load_engine_settings, {"MEM_PLUGIN_MAX_NOTE_CHARACTERS": "0"}),
        (load_worker_settings, {"WORKER_MAX_ATTEMPTS": "0"}),
        (load_worker_settings, {"WORKER_POLL_SECONDS": "infinity"}),
        (load_worker_settings, {"WORKER_LEASE_SECONDS": "30"}),
    ],
)
def test_bounded_settings_reject_invalid_values(load, env):
    with pytest.raises(ConfigurationError):
        load(environ=env)
