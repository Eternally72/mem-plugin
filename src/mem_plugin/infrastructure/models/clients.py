"""Small JSON HTTP adapters for Bailian and DeepSeek, with no provider SDK dependency."""

import json
import math
from collections.abc import Callable
from functools import partial
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mem_plugin.engine.errors import InvalidMemoryInput, ModelProviderError
from mem_plugin.engine.models import ChatResult, EmbeddingResult

JsonTransport = Callable[[str, str, dict[str, object]], dict]


def request_json(
    url: str, api_key: str, payload: dict[str, object], *, timeout: float = 30
) -> dict:
    request = Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            value = json.load(response)
    except HTTPError as exc:
        raise ModelProviderError(
            f"model service returned HTTP {exc.code}",
            retryable=exc.code == 429 or exc.code >= 500,
            failure_kind="http",
            http_status=exc.code,
        ) from None
    except TimeoutError:
        raise ModelProviderError("model request timed out", failure_kind="connection") from None
    except URLError as exc:
        message = (
            "model request timed out"
            if isinstance(exc.reason, TimeoutError)
            else "model connection failed"
        )
        raise ModelProviderError(message, failure_kind="connection") from None
    except OSError:
        raise ModelProviderError("model connection failed", failure_kind="connection") from None
    except ValueError:
        raise ModelProviderError("model returned invalid JSON") from None
    if not isinstance(value, dict):
        raise ModelProviderError("model service returned an invalid response")
    return value


class _Client:
    def __init__(
        self,
        provider: str,
        base_url: str,
        api_key: str,
        model: str,
        transport: JsonTransport = request_json,
        *,
        timeout_seconds: float = 30,
    ):
        self.provider, self.base_url, self.model = provider, base_url.rstrip("/"), model
        self._api_key = api_key
        self._transport = (
            partial(request_json, timeout=timeout_seconds)
            if transport is request_json
            else transport
        )

    def _ready(self):
        if not self._api_key.strip() or not self.model.strip():
            raise ModelProviderError(
                f"{self.provider}: model name and API key must be configured", retryable=False
            )


class ChatClient(_Client):
    def __init__(
        self,
        provider,
        base_url,
        api_key,
        model,
        transport=request_json,
        *,
        timeout_seconds=30,
        enable_thinking=None,
        reasoning_effort=None,
    ):
        super().__init__(
            provider, base_url, api_key, model, transport, timeout_seconds=timeout_seconds
        )
        if enable_thinking is not None and type(enable_thinking) is not bool:
            raise InvalidMemoryInput("thinking mode requires an explicit boolean")
        self._thinking = enable_thinking
        if reasoning_effort is not None and (
            not isinstance(reasoning_effort, str) or reasoning_effort not in {"low", "high", "max"}
        ):
            raise InvalidMemoryInput("reasoning_effort must be low, high or max")
        self._reasoning_effort = reasoning_effort

    def complete(
        self, messages: list[dict[str, str]], *, max_output_tokens: int | None = None
    ) -> ChatResult:
        self._ready()
        if not messages or any(
            not isinstance(m, dict)
            or m.get("role") not in {"system", "user", "assistant"}
            or not isinstance(m.get("content"), str)
            for m in messages
        ):
            raise InvalidMemoryInput("chat messages require valid role and content")
        payload = {"model": self.model, "messages": messages, "stream": False}
        if self._reasoning_effort is not None:
            payload["reasoning_effort"] = self._reasoning_effort
        if self._thinking is not None:
            if self.provider == "deepseek":
                payload["thinking"] = {"type": "enabled" if self._thinking else "disabled"}
            else:
                payload["enable_thinking"] = self._thinking
        if max_output_tokens is not None:
            if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 4096:
                raise InvalidMemoryInput("chat output budget must be 1..4096")
            payload["max_tokens"] = max_output_tokens
        response = self._transport(
            self.base_url + "/chat/completions",
            self._api_key,
            payload,
        )
        try:
            text = response["choices"][0]["message"]["content"]
            if response["choices"][0].get("finish_reason") == "length":
                raise ModelProviderError(
                    "chat output exceeded its budget", retryable=False, failure_kind="output_budget"
                )
            if not isinstance(text, str):
                raise ValueError
            usage = response.get("usage") or {}
            if not isinstance(usage, dict):
                raise ValueError
            return ChatResult(
                self.provider,
                self.model,
                text=text,
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
            )
        except (KeyError, IndexError, TypeError, ValueError):
            raise ModelProviderError("invalid chat completion response") from None


class EmbeddingClient(_Client):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        dimension: int,
        transport: JsonTransport = request_json,
    ):
        super().__init__("bailian", base_url, api_key, model, transport)
        self.dimension = dimension

    def embed(self, text: str) -> EmbeddingResult:
        self._ready()
        if not isinstance(text, str) or not text.strip():
            raise InvalidMemoryInput("embedding text must not be blank")
        response = self._transport(
            self.base_url + "/embeddings",
            self._api_key,
            {"model": self.model, "input": [text], "dimensions": self.dimension},
        )
        try:
            raw = response["data"][0]["embedding"]
            if not isinstance(raw, list) or len(raw) != self.dimension:
                raise ValueError
            if any(
                isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
                for x in raw
            ):
                raise ValueError
            if not any(raw):
                raise ValueError
            usage = response.get("usage") or {}
            if not isinstance(usage, dict):
                raise ValueError
            return EmbeddingResult(
                "bailian",
                self.model,
                vector=tuple(float(x) for x in raw),
                input_tokens=usage.get("prompt_tokens", usage.get("total_tokens")),
            )
        except (KeyError, IndexError, TypeError, ValueError):
            raise ModelProviderError(
                "invalid embedding response or vector dimension", retryable=False
            ) from None


class RerankClient(_Client):
    def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        self._ready()
        if (
            not query.strip()
            or not documents
            or any(not isinstance(doc, str) or not doc.strip() for doc in documents)
        ):
            raise InvalidMemoryInput("rerank requires a query and nonempty documents")
        response = self._transport(
            self.base_url + "/services/rerank/text-rerank/text-rerank",
            self._api_key,
            {
                "model": self.model,
                "input": {"query": query, "documents": documents},
                "parameters": {"return_documents": False},
            },
        )
        try:
            results = response["output"]["results"]
            ranks = [(item["index"], item["relevance_score"]) for item in results]
            if any(
                isinstance(i, bool)
                or not isinstance(i, int)
                or not 0 <= i < len(documents)
                or isinstance(s, bool)
                or not isinstance(s, (int, float))
                or not math.isfinite(s)
                for i, s in ranks
            ) or len({i for i, _ in ranks}) != len(ranks):
                raise ValueError
            return ranks
        except (KeyError, TypeError, ValueError):
            raise ModelProviderError("invalid rerank response") from None
