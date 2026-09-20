"""Domain error types. API handlers map them to HTTP status codes; tools map them to structured tool results."""

from __future__ import annotations


class DomainError(Exception):
    code = "error"
    status = 400

    def __init__(self, message: str, *, detail: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class NotFound(DomainError):
    code = "not_found"
    status = 404


class Conflict(DomainError):
    code = "conflict"
    status = 409


class ValidationFailed(DomainError):
    code = "invalid"
    status = 422


class PermissionDenied(DomainError):
    code = "forbidden"
    status = 403


class Unauthorized(DomainError):
    code = "unauthorized"
    status = 401


class RateLimited(DomainError):
    code = "rate_limited"
    status = 429


class ProviderUnavailable(DomainError):
    code = "provider_unavailable"
    status = 503
