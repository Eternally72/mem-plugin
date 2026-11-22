from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest

from mem_plugin.config.settings import (
    ConfigurationError,
    load_embedding_settings,
    load_llm_settings,
)
from mem_plugin.engine.errors import ModelProviderError
from mem_plugin.infrastructure.models.clients import (
    ChatClient,
    EmbeddingClient,
    RerankClient,
    request_json,
)


def test_two_llm_providers_keep_independent_models_keys_and_urls():
    env = {
        "LLM_PROVIDER": "deepseek",
        "DEEPSEEK_LLM_API_KEY": "deepseek-secret",
        "BAILIAN_LLM_API_KEY": "bailian-secret",
        "BAILIAN_LLM_MODEL": "",
    }
    deepseek = load_llm_settings(environ=env)
    bailian = load_llm_settings(provider="bailian", environ=env)
    assert deepseek.model == "deepseek-flash" and deepseek.api_key == "deepseek-secret"
    assert bailian.model == "" and bailian.api_key == "bailian-secret"
    assert deepseek.base_url == "https://api.deepseek.com"
    assert bailian.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert "deepseek-secret" not in repr(deepseek)
    with pytest.raises(ConfigurationError):
        load_llm_settings(environ={"LLM_PROVIDER": "unsupported"})
    with pytest.raises(ConfigurationError):
        load_llm_settings(environ={"DEEPSEEK_LLM_BASE_URL": "https://user:secret@example.com"})
    assert load_embedding_settings(environ=env).api_key == ""


def test_chat_payload_and_usage_are_correct_without_network_calls():
    transport = Mock(
        return_value={
            "choices": [{"message": {"content": "result"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3},
        }
    )
    client = ChatClient(
        "deepseek", "https://api.deepseek.com/", "secret", "deepseek-flash", transport
    )
    result = client.complete([{"role": "user", "content": "test"}])
    assert result.text == "result" and result.input_tokens == 10 and result.output_tokens == 3
    url, key, payload = transport.call_args.args
    assert url == "https://api.deepseek.com/chat/completions"
    assert key == "secret" and payload["model"] == "deepseek-flash" and payload["stream"] is False


@pytest.mark.parametrize("plugin_key", ["test-only", ""])
def test_memory_chat_uses_plugin_config(tmp_path, monkeypatch, plugin_key):
    from mem_plugin.bootstrap.container import create_model_clients

    plugin = tmp_path / "plugin"
    plugin.mkdir()
    plugin_env = {
        "LLM_PROVIDER": "openai_compatible",
        "MEM_PLUGIN_OPENAI_BASE_URL": "http://memory.example.test/v1",
        "MEM_PLUGIN_OPENAI_MODEL": "memory-model",
        "MEM_PLUGIN_OPENAI_API_KEY": plugin_key,
        "MEM_PLUGIN_OPENAI_TEMPERATURE": "0",
    }
    (plugin / ".env").write_text("".join(f"{k}={v}\n" for k, v in plugin_env.items()))
    (tmp_path / ".env").write_text("OPENAI_API_KEY=benchmark-file-key\n")
    monkeypatch.chdir(plugin)
    for key in plugin_env:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://agent.example.test/v1")
    monkeypatch.setenv("OPENAI_MODEL", "agent-model")
    monkeypatch.setenv("OPENAI_API_KEY", "agent-key")
    monkeypatch.setenv("OPENAI_TEMPERATURE", "1")
    chat = create_model_clients().llm
    transport = Mock(return_value={"choices": [{"message": {"content": "ok"}}]})
    chat._transport = transport
    messages = [{"role": "user", "content": "test"}]
    if not plugin_key:
        with pytest.raises(ModelProviderError, match="configured"):
            chat.complete(messages)
        transport.assert_not_called()
        return
    chat.complete(messages)
    url, key, body = transport.call_args.args
    assert (url, key) == ("http://memory.example.test/v1/chat/completions", plugin_key)
    assert body["model"] == "memory-model" and body["temperature"] == 0
    assert "thinking" not in body and "enable_thinking" not in body


def test_unconfigured_provider_never_sends_a_request():
    transport = Mock()
    for key, model in [("", "deepseek-flash"), ("secret", "")]:
        with pytest.raises(ModelProviderError, match="configured"):
            ChatClient("bailian", "https://example.com", key, model, transport).complete(
                [{"role": "user", "content": "test"}]
            )
    transport.assert_not_called()


def test_embedding_endpoint_dimension_and_usage():
    transport = Mock(
        return_value={
            "data": [{"index": 0, "embedding": [1.0] + [0.0] * 1023}],
            "usage": {"total_tokens": 4},
        }
    )
    client = EmbeddingClient(
        "https://example.com/v1", "embedding-key", "qwen3.7-text-embedding-flash", 1024, transport
    )
    result = client.embed("input")
    assert len(result.vector) == 1024 and result.input_tokens == 4
    url, key, payload = transport.call_args.args
    assert url.endswith("/embeddings") and key == "embedding-key"
    assert payload["dimensions"] == 1024 and payload["input"] == ["input"]


@pytest.mark.parametrize("vector", [[1.0], [float("nan")] * 1024, [0.0] * 1024])
def test_embedding_rejects_malformed_vectors(vector):
    client = EmbeddingClient(
        "https://example.com",
        "key",
        "model",
        1024,
        Mock(return_value={"data": [{"embedding": vector}]}),
    )
    with pytest.raises(ModelProviderError):
        client.embed("input")


def test_reranking_uses_native_endpoint_and_checks_indices():
    transport = Mock(return_value={"output": {"results": [{"index": 1, "relevance_score": 0.9}]}})
    client = RerankClient(
        "bailian", "https://example.com/api/v1", "rerank-key", "qwen3.7-text-rerank", transport
    )
    assert client.rerank("query", ["one", "two"]) == [(1, 0.9)]
    assert transport.call_args.args[0].endswith("/services/rerank/text-rerank/text-rerank")
    transport.return_value = {"output": {"results": [{"index": 99, "relevance_score": 0.9}]}}
    with pytest.raises(ModelProviderError):
        client.rerank("query", ["one", "two"])


def test_http_errors_do_not_leak_provider_body_key_or_url(monkeypatch):
    failure = HTTPError("https://example.com/private-secret", 401, "api-key-secret", {}, None)
    monkeypatch.setattr(
        "mem_plugin.infrastructure.models.clients.urlopen", Mock(side_effect=failure)
    )
    with pytest.raises(ModelProviderError) as error:
        request_json("https://example.com", "api-key-secret", {})
    assert str(error.value) == "model service returned HTTP 401"


@pytest.mark.parametrize("failure", [TimeoutError("secret"), URLError(TimeoutError("secret"))])
def test_transport_distinguishes_timeout_without_leaking_exception(monkeypatch, failure):
    monkeypatch.setattr(
        "mem_plugin.infrastructure.models.clients.urlopen", Mock(side_effect=failure)
    )
    with pytest.raises(ModelProviderError, match="model request timed out") as error:
        request_json("https://example.test", "secret", {})
    assert error.value.failure_kind == "connection"
    assert "secret" not in str(error.value)


def test_chat_timeout_is_configurable_and_passed_to_http(monkeypatch):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b'{"choices":[{"message":{"content":"OK"}}]}'
    http = Mock(return_value=response)
    monkeypatch.setattr("mem_plugin.infrastructure.models.clients.urlopen", http)
    settings = load_llm_settings(environ={"BAILIAN_LLM_TIMEOUT_SECONDS": "75"}, provider="bailian")
    ChatClient(
        "bailian", "https://example.test", "key", "model", timeout_seconds=settings.timeout_seconds
    ).complete([{"role": "user", "content": "test"}])
    assert http.call_args.kwargs["timeout"] == 75
    with pytest.raises(ConfigurationError):
        load_llm_settings(environ={"BAILIAN_LLM_TIMEOUT_SECONDS": "0"}, provider="bailian")


def test_bailian_thinking_is_explicit_optional_and_does_not_affect_deepseek():
    transport = Mock(return_value={"choices": [{"message": {"content": "{}"}}]})
    settings = load_llm_settings(
        provider="bailian", environ={"BAILIAN_LLM_ENABLE_THINKING": "false"}
    )
    client = ChatClient(
        "bailian",
        "https://example.test",
        "key",
        "kimi-k3",
        transport,
        enable_thinking=settings.enable_thinking,
    )
    client.complete([{"role": "user", "content": "JSON"}])
    assert transport.call_args.args[2]["enable_thinking"] is False
    ChatClient("bailian", "https://example.test", "key", "other", transport).complete(
        [{"role": "user", "content": "test"}]
    )
    assert "enable_thinking" not in transport.call_args.args[2]
    assert (
        load_llm_settings(
            provider="deepseek", environ={"BAILIAN_LLM_ENABLE_THINKING": "false"}
        ).enable_thinking
        is False
    )


def test_deepseek_thinking_uses_its_own_protocol_field():
    transport = Mock(return_value={"choices": [{"message": {"content": "{}"}}]})
    for enabled in (False, True):
        client = ChatClient(
            "deepseek",
            "https://example.test",
            "key",
            "deepseek-flash",
            transport,
            enable_thinking=enabled,
        )
        client.complete([{"role": "user", "content": "JSON"}], max_output_tokens=512)
        payload = transport.call_args.args[2]
        assert payload["thinking"] == {"type": "enabled" if enabled else "disabled"}
        assert "enable_thinking" not in payload
    with pytest.raises(ConfigurationError):
        load_llm_settings(provider="bailian", environ={"BAILIAN_LLM_ENABLE_THINKING": "maybe"})


def test_reasoning_effort_is_optional_provider_scoped_and_reaches_chat_payload():
    transport = Mock(return_value={"choices": [{"message": {"content": "{}"}}]})
    env = {"BAILIAN_LLM_REASONING_EFFORT": "low"}
    settings = load_llm_settings(provider="bailian", environ=env)
    assert settings.reasoning_effort == "low"
    assert load_llm_settings(provider="deepseek", environ=env).reasoning_effort is None
    ChatClient(
        "bailian",
        "https://example.test",
        "key",
        "glm-5.3",
        transport,
        enable_thinking=True,
        reasoning_effort=settings.reasoning_effort,
    ).complete([{"role": "user", "content": "JSON"}], max_output_tokens=4096)
    assert transport.call_args.args[2]["reasoning_effort"] == "low"
    assert transport.call_args.args[2]["enable_thinking"] is True
    ChatClient("bailian", "https://example.test", "key", "other", transport).complete(
        [{"role": "user", "content": "JSON"}]
    )
    assert "reasoning_effort" not in transport.call_args.args[2]
    with pytest.raises(ConfigurationError):
        load_llm_settings(provider="bailian", environ={"BAILIAN_LLM_REASONING_EFFORT": "invalid"})


def test_factory_reads_provider_configs_without_network_or_database(tmp_path, monkeypatch):
    from mem_plugin.bootstrap.container import create_model_clients

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_LLM_MODEL", "deepseek-flash")
    monkeypatch.setenv("DEEPSEEK_LLM_API_KEY", "deepseek-secret")
    monkeypatch.setenv("BAILIAN_LLM_MODEL", "")
    monkeypatch.setenv("BAILIAN_LLM_API_KEY", "bailian-secret")
    transport = Mock(side_effect=AssertionError("construction must not send requests"))
    monkeypatch.setattr("mem_plugin.infrastructure.models.clients.urlopen", transport)
    deepseek = create_model_clients()
    bailian = create_model_clients(llm_provider="bailian")
    assert deepseek.llm.model == "deepseek-flash"
    assert not hasattr(deepseek.embedding, "complete")
    with pytest.raises(ModelProviderError, match="configured"):
        bailian.llm.complete([{"role": "user", "content": "no model configured"}])
    transport.assert_not_called()
