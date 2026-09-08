from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    CONFIG_ERROR = "CONFIG_ERROR"
    SOURCE_ERROR = "SOURCE_ERROR"

    NAVIGATION_TIMEOUT = "NAVIGATION_TIMEOUT"
    NAVIGATION_FAILED = "NAVIGATION_FAILED"
    HTTP_ERROR = "HTTP_ERROR"
    BROWSER_ERROR = "BROWSER_ERROR"
    WORKER_ERROR = "WORKER_ERROR"
    BLOCKED = "BLOCKED"

    EXTRACTION_ERROR = "EXTRACTION_ERROR"
    NORMALIZATION_ERROR = "NORMALIZATION_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    PAGINATION_ERROR = "PAGINATION_ERROR"
    STORAGE_ERROR = "STORAGE_ERROR"
    EXPORT_ERROR = "EXPORT_ERROR"

    UNKNOWN = "UNKNOWN"


RETRYABLE: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.NAVIGATION_TIMEOUT,
        ErrorCode.NAVIGATION_FAILED,
        ErrorCode.HTTP_ERROR,
        ErrorCode.BROWSER_ERROR,
        ErrorCode.WORKER_ERROR,
    }
)

TERMINAL: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.CONFIG_ERROR,
        ErrorCode.SOURCE_ERROR,
        ErrorCode.BLOCKED,
        ErrorCode.VALIDATION_ERROR,
    }
)


class UparseError(Exception):
    code: ErrorCode = ErrorCode.UNKNOWN

    def __init__(self, message: str, *, url: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.url = url

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE

    def __str__(self) -> str:
        return (
            f"{self.code}: {self.message} ({self.url})"
            if self.url
            else f"{self.code}: {self.message}"
        )


class ConfigError(UparseError):
    code = ErrorCode.CONFIG_ERROR


class SourceError(UparseError):
    code = ErrorCode.SOURCE_ERROR


class AcquisitionError(UparseError):
    code = ErrorCode.NAVIGATION_FAILED


class NavigationTimeoutError(AcquisitionError):
    code = ErrorCode.NAVIGATION_TIMEOUT


class HttpError(AcquisitionError):
    code = ErrorCode.HTTP_ERROR

    def __init__(self, message: str, *, url: str | None = None, status: int | None = None) -> None:
        super().__init__(message, url=url)
        self.status = status


class BrowserError(AcquisitionError):
    code = ErrorCode.BROWSER_ERROR


class WorkerError(AcquisitionError):
    code = ErrorCode.WORKER_ERROR


# Detected only. UniParse never solves or bypasses challenges.
class BlockedError(AcquisitionError):
    code = ErrorCode.BLOCKED


class ExtractionError(UparseError):
    code = ErrorCode.EXTRACTION_ERROR


class NormalizationError(UparseError):
    code = ErrorCode.NORMALIZATION_ERROR


class ValidationError(UparseError):
    code = ErrorCode.VALIDATION_ERROR


class PaginationError(UparseError):
    code = ErrorCode.PAGINATION_ERROR


class StorageError(UparseError):
    code = ErrorCode.STORAGE_ERROR


class ExportError(UparseError):
    code = ErrorCode.EXPORT_ERROR


def code_of(exc: BaseException) -> ErrorCode:
    if isinstance(exc, UparseError):
        return exc.code
    if isinstance(exc, TimeoutError):
        return ErrorCode.NAVIGATION_TIMEOUT
    if isinstance(exc, OSError):
        return ErrorCode.SOURCE_ERROR
    return ErrorCode.UNKNOWN
