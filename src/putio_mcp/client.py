"""Thin wrapper around putiopy.Client.

Responsibilities:
  * call the put.io v2 endpoints the server needs, returning plain dicts
  * expose controllable pagination (putiopy's File.list exhausts all pages)
  * translate putiopy / requests exceptions into classified PutioError values
  * make sure the access token never leaks into error messages
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import putiopy
import requests

from .errors import (
    AUTH,
    INVALID,
    NETWORK,
    NOT_FOUND,
    PERMISSION,
    RATE_LIMIT,
    SERVER,
    PutioError,
    redact,
)

log = logging.getLogger("putio_mcp.client")

MAX_PER_PAGE = 1000
SORT_OPTIONS = (
    "NAME_ASC",
    "NAME_DESC",
    "SIZE_ASC",
    "SIZE_DESC",
    "DATE_ASC",
    "DATE_DESC",
    "MODIFIED_ASC",
    "MODIFIED_DESC",
)


class PutioApi(Protocol):
    """The subset of put.io operations the service layer depends on.

    Implemented by PutioClient (real) and by the in-memory fake in tests.
    """

    def list_folder(
        self, parent_id: int, per_page: int, sort_by: str | None, cursor: str | None
    ) -> dict[str, Any]: ...

    def search(self, query: str, per_page: int, cursor: str | None) -> dict[str, Any]: ...

    def get_file(self, file_id: int) -> dict[str, Any]: ...

    def create_folder(self, name: str, parent_id: int) -> dict[str, Any]: ...

    def rename(self, file_id: int, name: str) -> None: ...

    def move(self, file_id: int, parent_id: int) -> None: ...

    def list_transfers(self) -> list[dict[str, Any]]: ...

    def get_transfer(self, transfer_id: int) -> dict[str, Any]: ...

    def add_transfer(self, url: str, parent_id: int | None) -> dict[str, Any]: ...

    def cancel_transfer(self, transfer_id: int) -> None: ...


class PutioClient:
    """Real implementation of PutioApi backed by putiopy."""

    def __init__(self, token: str, timeout: int = 30) -> None:
        if not token or not token.strip():
            raise PutioError(AUTH, "PUTIO_TOKEN is empty.")
        self._token = token.strip()
        # putiopy's request() always uses self.timeout; its default of 5s is too
        # aggressive for large folder listings.
        self._client = putiopy.Client(self._token, timeout=timeout)

    # -- low level -----------------------------------------------------------

    def _request(self, path: str, method: str = "GET", **kwargs: Any) -> Any:
        try:
            return self._client.request(path, method=method, **kwargs)
        except putiopy.APIError as exc:
            raise self._translate_api_error(exc) from None
        except requests.exceptions.Timeout:
            raise PutioError(NETWORK, "put.io did not respond in time; try again.") from None
        except requests.exceptions.RequestException as exc:
            raise PutioError(
                NETWORK, f"could not reach put.io: {redact(exc.__class__.__name__, self._token)}"
            ) from None

    def _translate_api_error(self, exc: putiopy.APIError) -> PutioError:
        status = exc.response.status_code if exc.response is not None else None
        api_type = redact(exc.type or "", self._token)
        api_msg = redact(exc.message or "", self._token)
        detail = f" ({api_type}: {api_msg})" if api_msg else f" ({api_type})" if api_type else ""
        details: dict[str, Any] = {"putio_error_type": api_type}

        if status == 401:
            return PutioError(
                AUTH,
                "put.io rejected the access token; it is invalid, revoked, or expired. "
                "Generate a new token and update PUTIO_TOKEN." + detail,
                status=status,
                details=details,
            )
        if status == 403:
            return PutioError(
                PERMISSION,
                "put.io denied this operation for the current account" + detail,
                status=status,
                details=details,
            )
        if status == 404:
            return PutioError(
                NOT_FOUND,
                "put.io has no such file, folder, or transfer" + detail,
                status=status,
                details=details,
            )
        if status == 429:
            retry_after = None
            if exc.response is not None:
                retry_after = exc.response.headers.get("Retry-After")
            if retry_after:
                details["retry_after_seconds"] = retry_after
            hint = f"; retry after {retry_after}s" if retry_after else "; wait a moment and retry"
            return PutioError(
                RATE_LIMIT, "put.io rate limit reached" + hint + detail, status=status, details=details
            )
        if status is not None and 400 <= status < 500:
            return PutioError(
                INVALID, f"put.io rejected the request (HTTP {status})" + detail, status=status, details=details
            )
        return PutioError(
            SERVER,
            f"put.io returned a server error (HTTP {status})" + detail,
            status=status,
            details=details,
        )

    # -- files ---------------------------------------------------------------

    def list_folder(
        self, parent_id: int, per_page: int, sort_by: str | None, cursor: str | None
    ) -> dict[str, Any]:
        if cursor:
            return self._request(
                "/files/list/continue",
                method="POST",
                data={"cursor": cursor, "per_page": str(per_page)},
            )
        params: dict[str, Any] = {"parent_id": str(parent_id), "per_page": str(per_page)}
        if sort_by:
            params["sort_by"] = sort_by
        return self._request("/files/list", params=params)

    def search(self, query: str, per_page: int, cursor: str | None) -> dict[str, Any]:
        if cursor:
            return self._request(
                "/files/search/continue",
                method="POST",
                data={"cursor": cursor, "per_page": str(per_page)},
            )
        return self._request("/files/search", params={"query": query, "per_page": str(per_page)})

    def get_file(self, file_id: int) -> dict[str, Any]:
        return self._request(f"/files/{int(file_id)}")["file"]

    def create_folder(self, name: str, parent_id: int) -> dict[str, Any]:
        return self._request(
            "/files/create-folder",
            method="POST",
            data={"name": name, "parent_id": str(parent_id)},
        )["file"]

    def rename(self, file_id: int, name: str) -> None:
        self._request("/files/rename", method="POST", data={"file_id": str(file_id), "name": name})

    def move(self, file_id: int, parent_id: int) -> None:
        self._request(
            "/files/move",
            method="POST",
            data={"file_ids": str(file_id), "parent_id": str(parent_id)},
        )

    # -- transfers -----------------------------------------------------------

    def list_transfers(self) -> list[dict[str, Any]]:
        return self._request("/transfers/list")["transfers"]

    def get_transfer(self, transfer_id: int) -> dict[str, Any]:
        return self._request(f"/transfers/{int(transfer_id)}")["transfer"]

    def add_transfer(self, url: str, parent_id: int | None) -> dict[str, Any]:
        data: dict[str, Any] = {"url": url}
        if parent_id is not None:
            data["save_parent_id"] = str(parent_id)
        return self._request("/transfers/add", method="POST", data=data)["transfer"]

    def cancel_transfer(self, transfer_id: int) -> None:
        self._request("/transfers/cancel", method="POST", data={"transfer_ids": str(transfer_id)})
