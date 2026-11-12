"""Canonical archive format and validation; filesystem and SQL remain adapters."""

import hashlib
import json
from datetime import datetime
from uuid import UUID

from mem_plugin.engine.errors import ArchiveError
from mem_plugin.engine.services.graph import _json_pairs


def _value(value):
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError("unsupported archive value")


def encode_archive(records, max_bytes):
    try:
        payload = b"".join(
            (
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                    default=_value,
                )
                + "\n"
            ).encode()
            for record in records
        )
    except (ValueError, TypeError, RecursionError):
        raise ArchiveError("archive contains unsupported values") from None
    if not 0 < len(payload) <= max_bytes:
        raise ArchiveError("archive exceeds configured size limit")
    return payload


def checksum(payload):
    return hashlib.sha256(payload).hexdigest()


def verify_archive(payload, manifest, node):
    if len(payload) != manifest.byte_count or checksum(payload) != manifest.checksum:
        raise ArchiveError("archive checksum mismatch")
    try:
        records = [json.loads(line, object_pairs_hook=_json_pairs) for line in payload.splitlines()]
        if len(records) != manifest.record_count or not records or records[0].get("schema") != 1:
            raise ValueError
        first = records[0]
        if first.get("type") != "node":
            raise ValueError
        saved = first["data"]
        expected = {
            "id": str(node.id),
            "user_id": node.user_id,
            "namespace": node.namespace,
            "agent_id": node.agent_id,
            "scope": str(node.scope),
            "domain": node.domain,
            "level": node.level,
            "version": node.version,
            "content": node.content,
            "source_ref": node.source_ref,
            "domain_payload": node.domain_payload,
            "occurred_at": node.occurred_at.isoformat() if node.occurred_at else None,
            "created_at": node.created_at.isoformat(),
        }
        if any(saved.get(key) != value for key, value in expected.items()):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ArchiveError("archive identity, version or record count mismatch") from None
