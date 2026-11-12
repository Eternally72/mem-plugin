"""Explicit-note admission and deterministic embedding input, without model calls."""

import hashlib
import json
import re

from mem_plugin.engine.errors import InvalidMemoryInput


def embedding_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def note_text(content: str, max_characters: int = 16000) -> str:
    if not isinstance(content, str) or not content.strip():
        raise InvalidMemoryInput("content must not be blank")
    text = content.strip()
    if len(text) > max_characters:
        raise InvalidMemoryInput("content exceeds the configured note size limit")
    if "-----BEGIN PRIVATE KEY-----" in text or re.search(
        r"\bBearer\s+[A-Za-z0-9._~+/=-]{32,}", text
    ):
        raise InvalidMemoryInput("credentials must not be stored as memory")
    return text


def structured_fields(value: dict[str, object] | None) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InvalidMemoryInput("structured fields must be JSON objects")
    try:
        encoded = json.dumps(value, allow_nan=False, sort_keys=True)
        if len(encoded) > 16000:
            raise InvalidMemoryInput("structured fields exceed the size limit")
        result = json.loads(encoded)
    except (ValueError, TypeError, RecursionError):
        raise InvalidMemoryInput("structured fields require finite JSON values") from None

    def validate(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if (
                    key.lower() in {"api_key", "apikey", "password", "access_token", "secret_key"}
                    and child
                ):
                    raise InvalidMemoryInput("credentials must not be stored in structured memory")
                validate(child)
        elif isinstance(item, list):
            for child in item:
                validate(child)

    validate(result)
    return result
