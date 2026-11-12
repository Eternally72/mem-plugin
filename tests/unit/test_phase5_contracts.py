"""File adapter and policy validation without a database or model SDK."""

import os
from pathlib import Path
from uuid import uuid4

import pytest

from mem_plugin.config.settings import ConfigurationError, load_lifecycle_settings
from mem_plugin.engine.errors import ArchiveError, InvalidMemoryInput
from mem_plugin.engine.models import LifecyclePolicy, TypeLifecyclePolicy
from mem_plugin.infrastructure.archive.file_store import FileArchiveStore
from mem_plugin.scheduler.runner import Scheduler


def test_private_atomic_file_and_temp_purge(tmp_path):
    store = FileArchiveStore(tmp_path / "private")
    key = uuid4().hex
    store.write(key, b'{"test":true}\n')
    assert store.read(key) == b'{"test":true}\n'
    assert (store.root / (key + ".jsonl")).stat().st_mode & 0o777 == 0o600
    assert store.root.stat().st_mode & 0o777 == 0o700
    temporary = store.root / ("." + key + "." + uuid4().hex + ".tmp")
    temporary.write_bytes(b"partial")
    store.delete(key)
    assert list(store.root.iterdir()) == []
    store.delete(key)


@pytest.mark.parametrize("key", ["../secret", "a/b", "A" * 32, "", "a" * 31])
def test_storage_key_cannot_escape_root(tmp_path, key):
    store = FileArchiveStore(tmp_path)
    for operation in (store.read, store.delete):
        with pytest.raises(ArchiveError):
            operation(key)
    with pytest.raises(ArchiveError):
        store.write(key, b"body")


def test_symlinks_fifo_size_and_missing_files_are_errors(tmp_path):
    store = FileArchiveStore(tmp_path / "private", max_bytes=5)
    key = uuid4().hex
    with pytest.raises(ArchiveError):
        store.read(key)
    with pytest.raises(ArchiveError):
        store.write(key, b"toolong")
    store.root.mkdir()
    file = store.root / (key + ".jsonl")
    file.symlink_to(tmp_path / "outside")
    with pytest.raises(ArchiveError):
        store.read(key)
    store.delete(key)
    os.mkfifo(file)
    with pytest.raises(ArchiveError):
        store.read(key)
    store.delete(key)
    link = tmp_path / "link"
    link.symlink_to(store.root, target_is_directory=True)
    with pytest.raises(ArchiveError):
        FileArchiveStore(link).write(key, b"body")


@pytest.mark.parametrize(
    "value",
    [
        "[]",
        '{"event":{"unknown":1}}',
        '{"event":{"archive_after_days":true}}',
        '{"event":{"retention_action":"drop"}}',
        '{"event":{"review_after_days":-1}}',
        '{"event":{}}',
    ],
)
def test_invalid_policy_configuration(value):
    with pytest.raises(ConfigurationError):
        load_lifecycle_settings(environ={"LIFECYCLE_TYPE_POLICIES": value})


def test_policy_config_and_defaults():
    settings = load_lifecycle_settings(
        environ={
            "ARCHIVE_DIR": "private",
            "LIFECYCLE_TYPE_POLICIES": '{"event":{"archive_after_days":100}}',
        }
    )
    assert settings.archive_dir == Path("private")
    assert settings.type_policies["event"]["archive_after_days"] == 100
    policy = LifecyclePolicy()
    assert policy.for_type("preference").archive_after_days == 0
    assert policy.for_type("unknown").confidence_half_life_days == 0
    assert all(rule.retention_action == "archive" for rule in policy.rules)
    with pytest.raises(InvalidMemoryInput):
        LifecyclePolicy((TypeLifecyclePolicy("event", retention_action="drop"),))


def test_scheduler_lifecycle_has_independent_interval():
    from types import SimpleNamespace

    calls = []
    life = SimpleNamespace(schedule=lambda ctx, limit: calls.append((ctx, limit)) or 1)
    scheduler = Scheduler(None, ("ctx",), (1, 2, 3), 7, consolidation_enabled=False, lifecycle=life)
    assert scheduler.run_once() == 1
    assert scheduler.run_once() == 0
    assert scheduler.run_once(force=True) == 1
    assert calls == [("ctx", 7), ("ctx", 7)]
