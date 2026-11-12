"""Operational logs expose progress and correlation, never memory text or credentials."""

import io
import logging

import pytest

from mem_plugin.bootstrap import runtime
from mem_plugin.config.settings import load_llm_settings
from mem_plugin.observability.tracing import Tracer


def test_trace_logs_progress_safe_metadata_and_sanitized_failure(caplog):
    class Sink:
        def write(self, span):
            pass

    caplog.set_level(logging.INFO)
    tracer = Tracer(Sink())
    with tracer.span("memory.search", "user", "default", "agent") as span:
        span.attributes.update(result_count=3, query="private-query", api_key="private-key")
    with pytest.raises(ValueError), tracer.span("memory.record", "user", "default", "agent"):
        raise ValueError("private-error-body")
    assert "memory_operation_started" in caplog.text
    assert "memory_operation_finished" in caplog.text
    assert str(span.trace_id) in caplog.text
    assert '"result_count": 3' in caplog.text
    assert "ValueError" in caplog.text
    for secret in ("private-query", "private-key", "private-error-body"):
        assert secret not in caplog.text


def test_relay_shows_our_progress_without_replaying_sdk_output(capsys):
    source = io.StringIO(
        "SDK request body: private-secret\n"
        "[mem_plugin.observability.tracing] 2026-11-11 INFO memory_operation_started\n"
    )
    runtime._relay_log("api", source)
    text = capsys.readouterr().err
    assert "[api]" in text and "memory_operation_started" in text
    assert "private-secret" not in text


def test_deepseek_thinking_is_explicit_and_configurable():
    assert load_llm_settings(environ={}).enable_thinking is False
    assert load_llm_settings(environ={"DEEPSEEK_LLM_ENABLE_THINKING": "true"}).enable_thinking
