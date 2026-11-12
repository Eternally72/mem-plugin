"""Experiment configuration is explicit, private and cannot fall back to ordinary memory."""

import json
from dataclasses import replace

import pytest

from mem_plugin.config.settings import ConfigurationError
from mem_plugin.config.test_runs import RunStore, bind_environment, create_profile
from mem_plugin.engine.errors import MemoryAccessDenied
from mem_plugin.engine.models import AccessContext
from mem_plugin.interfaces.mcp.context import ToolIdentity


@pytest.fixture
def env(tmp_path):
    return {
        "DATABASE_URL": "postgresql://owner:private-db-password@localhost/unit",
        "ARCHIVE_DIR": str(tmp_path / "archive"),
        "MEM_PLUGIN_USER_ID": "same-user",
        "MEM_PLUGIN_AGENT_ID": "same-agent",
        "BAILIAN_LLM_API_KEY": "private-model-api-key",
        "LLM_PROVIDER": "bailian",
        "BAILIAN_LLM_MODEL": "kimi-k3",
    }


def test_new_runs_isolate_and_rebind_without_mutating_environment(env, tmp_path):
    original = dict(env)
    a, b = (create_profile(env, "same-agent", port) for port in (8101, 8102))
    assert (a.user_id, a.agent_id) == (b.user_id, b.agent_id)
    assert a.namespace != b.namespace and a.token != b.token
    bound = bind_environment(a, env)
    assert bound["MEM_PLUGIN_NAMESPACES"] == a.namespace
    assert bound["MEM_PLUGIN_MCP_TOKEN"] == a.token
    assert bound["BAILIAN_LLM_API_KEY"] == env["BAILIAN_LLM_API_KEY"]
    assert env == original
    identity = ToolIdentity(
        AccessContext(a.user_id, a.agent_id, a.namespace), frozenset({a.namespace})
    )
    assert identity.access().namespace == a.namespace
    assert identity.access(a.namespace).namespace == a.namespace
    for namespace in ("default", b.namespace, ""):
        with pytest.raises(MemoryAccessDenied):
            identity.access(namespace)
    store = RunStore(tmp_path / "runs")
    store.create(a)
    assert store.load(a.run_id) == a
    assert store.list() == [a]
    assert a.token not in repr(a)
    assert "private-model-api-key" not in json.dumps(a.runtime)
    assert "private-db-password" not in json.dumps(a.runtime)
    for file in store.directory(a.run_id).iterdir():
        assert file.stat().st_mode & 0o777 == 0o600
    assert store.directory(a.run_id).stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "overrides",
    [
        {"DATABASE_URL": "postgresql://owner@localhost/other"},
        {"MEM_PLUGIN_USER_ID": "other"},
        {"MEM_PLUGIN_AGENT_ID": "other"},
        {"MEM_PLUGIN_DOMAINS": "work"},
        {"ARCHIVE_DIR": "another-archive"},
        {"BAILIAN_LLM_MODEL": "other-model"},
        {"BAILIAN_LLM_TIMEOUT_SECONDS": "100"},
        {"BAILIAN_LLM_ENABLE_THINKING": "false"},
        {"RETRIEVAL_CANDIDATE_LIMIT": "20"},
    ],
)
def test_profile_guard_rejects_changed_environment(env, overrides):
    profile = create_profile(env, "test", 8101)
    with pytest.raises(ConfigurationError):
        bind_environment(profile, {**env, **overrides})


def test_key_rotation_is_allowed_and_cleanup_does_not_require_old_model_settings(env):
    profile = create_profile(env, "test", 8101)
    assert bind_environment(profile, {**env, "BAILIAN_LLM_API_KEY": "new-secret"})
    assert bind_environment(profile, {**env, "BAILIAN_LLM_MODEL": "new-model"}, check_runtime=False)


def test_closed_runs_and_invalid_paths_never_fall_back(env, tmp_path):
    profile = create_profile(env, "test", 8101)
    store = RunStore(tmp_path)
    with pytest.raises(ConfigurationError):
        store.load(profile.run_id)
    store.create(profile)
    for run_id in ("default", "../outside", "A" * 32, ""):
        with pytest.raises(ConfigurationError):
            store.load(run_id)
    for status in ("cleaned", "cleaning"):
        with pytest.raises(ConfigurationError):
            bind_environment(replace(profile, status=status), env)
    assert bind_environment(replace(profile, status="cleaning"), env, allow_cleaning=True)
    with pytest.raises(ConfigurationError):
        replace(profile, run_id="default")


def test_cleanup_requires_exclusive_process_lock(env, tmp_path):
    store = RunStore(tmp_path)
    profile = create_profile(env, "test", 8101)
    store.create(profile)
    with store.lock(profile.run_id):
        with store.lock(profile.run_id):
            pass
        with pytest.raises(ConfigurationError, match="in use"):
            with store.lock(profile.run_id, exclusive=True):
                pass
    with store.lock(profile.run_id, exclusive=True):
        with pytest.raises(ConfigurationError, match="in use"):
            with store.lock(profile.run_id):
                pass


def test_corrupt_or_swapped_profile_is_rejected(env, tmp_path):
    store = RunStore(tmp_path)
    a, b = (create_profile(env, "test", p) for p in (8101, 8102))
    store.create(a)
    store.create(b)
    (store.directory(a.run_id) / "run.json").write_bytes(
        (store.directory(b.run_id) / "run.json").read_bytes()
    )
    with pytest.raises(ConfigurationError):
        store.load(a.run_id)
