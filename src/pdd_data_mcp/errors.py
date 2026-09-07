class PddDataMcpError(Exception):
    """Base error for expected service failures."""


class AuthorizationError(PddDataMcpError):
    pass


class ConfigurationError(PddDataMcpError):
    pass


class StorageError(PddDataMcpError):
    pass


class StorageFullError(StorageError):
    pass


class LockBusyError(StorageError):
    pass


class SnapshotNotFoundError(StorageError):
    pass


class SnapshotCorruptError(StorageError):
    pass


class IdempotencyConflictError(StorageError):
    pass


class RequestBusyError(StorageError):
    pass


class ResponseTooLargeError(StorageError):
    pass


class ValidationFailure(PddDataMcpError):
    pass


class CollectionRejected(PddDataMcpError):
    """Expected real-collection outcome that must not create a snapshot."""

    def __init__(self, status: str, error_code: str, *warnings: str) -> None:
        super().__init__(error_code)
        self.status = status
        self.error_code = error_code
        self.warnings = list(warnings)
