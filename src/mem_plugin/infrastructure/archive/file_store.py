"""Private, bounded JSONL blobs with atomic replacement and per-key temp cleanup."""

import os
import re
import stat
from pathlib import Path
from uuid import uuid4

from mem_plugin.engine.errors import ArchiveError


class FileArchiveStore:
    def __init__(self, root, max_bytes=8388608):
        self.root = Path(root).absolute()
        self.max_bytes = max_bytes

    def _name(self, key):
        if not isinstance(key, str) or not re.fullmatch("[0-9a-f]{32}", key):
            raise ArchiveError("invalid archive storage key")
        return key + ".jsonl"

    def _open_root(self, create=False):
        if create:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def write(self, key, payload):
        name = self._name(key)
        if not isinstance(payload, bytes) or not 0 < len(payload) <= self.max_bytes:
            raise ArchiveError("archive exceeds configured size limit")
        directory = None
        temporary = "." + key + "." + uuid4().hex + ".tmp"
        try:
            directory = self._open_root(create=True)
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            with os.fdopen(fd, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        except OSError:
            raise ArchiveError("archive storage write failed") from None
        finally:
            if directory is not None:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except OSError:
                    pass
                finally:
                    os.close(directory)

    def read(self, key):
        name = self._name(key)
        directory = None
        try:
            directory = self._open_root()
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ArchiveError("archive is not a regular file")
                payload = source.read(self.max_bytes + 1)
            if len(payload) > self.max_bytes:
                raise ArchiveError("archive exceeds configured size limit")
            return payload
        except OSError:
            raise ArchiveError("archive storage read failed") from None
        finally:
            if directory is not None:
                os.close(directory)

    def delete(self, key):
        name = self._name(key)
        directory = None
        try:
            try:
                directory = self._open_root()
            except FileNotFoundError:
                return
            for candidate in os.listdir(directory):
                if candidate == name or (
                    candidate.startswith("." + key + ".") and candidate.endswith(".tmp")
                ):
                    os.unlink(candidate, dir_fd=directory)
            os.fsync(directory)
        except OSError:
            raise ArchiveError("archive storage deletion failed") from None
        finally:
            if directory is not None:
                os.close(directory)
