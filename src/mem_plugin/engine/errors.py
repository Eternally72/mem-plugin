"""Memory errors independent of storage drivers and external protocols."""


class MemoryError(Exception):
    code = "memory_error"


class InvalidMemoryInput(MemoryError):
    code = "invalid_input"


class MemoryNotFound(MemoryError):
    code = "not_found"


class MemoryStorageError(MemoryError):
    code = "storage_error"


class MemoryConflict(MemoryError):
    code = "conflict"


class MemoryAccessDenied(MemoryError):
    code = "access_denied"


class ModelProviderError(MemoryError):
    code = "provider_error"

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        failure_kind: str = "response",
        http_status: int | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.failure_kind = failure_kind
        self.http_status = http_status


class TaskLeaseLost(MemoryError):
    code = "lease_lost"


class ArchiveError(MemoryError):
    code = "archive_error"
