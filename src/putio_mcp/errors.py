"""Error types and credential redaction shared by the client and service layers."""

from __future__ import annotations

from typing import Any

# Error kinds surfaced to the MCP client. Keep these stable: Claude reads them.
AUTH = "authentication_error"
PERMISSION = "permission_denied"
NOT_FOUND = "not_found"
RATE_LIMIT = "rate_limited"
INVALID = "invalid_request"
CONFLICT = "name_conflict"
SERVER = "putio_server_error"
NETWORK = "network_error"
CONFIG = "configuration_error"


class PutioError(Exception):
    """A classified, user-presentable error. Never contains credentials."""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        self.details = details or {}

    def __str__(self) -> str:
        return f"{self.kind}: {self.message}"


def redact(text: Any, secret: str | None) -> str:
    """Return ``text`` as a string with every occurrence of ``secret`` masked."""
    s = str(text)
    if secret and len(secret) >= 4:
        s = s.replace(secret, "[REDACTED]")
    return s
