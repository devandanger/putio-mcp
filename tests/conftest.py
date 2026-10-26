from __future__ import annotations

import itertools
from typing import Any

import pytest

from putio_mcp.errors import INVALID, NOT_FOUND, PERMISSION, PutioError
from putio_mcp.service import FOLDER_CONTENT_TYPE, PutioService


class FakePutio:
    """In-memory stand-in for PutioClient implementing the PutioApi protocol.

    Mirrors the shapes of the real put.io v2 responses (files / parent / total /
    cursor) so the service layer is exercised exactly as in production.
    """

    def __init__(self) -> None:
        self._ids = itertools.count(100)
        self.files: dict[int, dict[str, Any]] = {}
        self.transfers: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fail_move_ids: set[int] = set()
        self.cursors: dict[str, tuple[list[dict[str, Any]], int, int]] = {}

    # -- fixture helpers -----------------------------------------------------

    def add_folder(self, name: str, parent_id: int = 0) -> int:
        fid = next(self._ids)
        self.files[fid] = {
            "id": fid,
            "name": name,
            "parent_id": parent_id,
            "content_type": FOLDER_CONTENT_TYPE,
            "file_type": "FOLDER",
            "size": 0,
            "created_at": "2026-01-01T00:00:00",
        }
        return fid

    def add_file(self, name: str, parent_id: int = 0, size: int = 1234) -> int:
        fid = next(self._ids)
        self.files[fid] = {
            "id": fid,
            "name": name,
            "parent_id": parent_id,
            "content_type": "video/mp4",
            "file_type": "VIDEO",
            "size": size,
            "created_at": "2026-01-01T00:00:00",
        }
        return fid

    def add_transfer_record(self, name: str, status: str = "DOWNLOADING", file_id: int | None = None) -> int:
        tid = next(self._ids)
        self.transfers[tid] = {
            "id": tid,
            "name": name,
            "status": status,
            "percent_done": 42,
            "file_id": file_id,
            "save_parent_id": 0,
        }
        return tid

    def _children(self, parent_id: int) -> list[dict[str, Any]]:
        return sorted((f for f in self.files.values() if f["parent_id"] == parent_id), key=lambda f: f["id"])

    def _paginate(self, items: list[dict[str, Any]], per_page: int, cursor: str | None, key: str) -> dict[str, Any]:
        if cursor:
            if cursor not in self.cursors:
                raise PutioError(INVALID, "put.io rejected the request (HTTP 400) (BadCursor)", status=400)
            items, start, _ = self.cursors.pop(cursor)
        else:
            start = 0
        page = items[start : start + per_page]
        end = start + per_page
        next_cursor = None
        if end < len(items):
            next_cursor = f"{key}-{len(self.cursors)}-{end}"
            self.cursors[next_cursor] = (items, end, per_page)
        return {"files": page, "cursor": next_cursor, "total": len(items)}

    # -- PutioApi ------------------------------------------------------------

    def list_folder(self, parent_id: int, per_page: int, sort_by: str | None, cursor: str | None) -> dict[str, Any]:
        self.calls.append(("list_folder", (parent_id, per_page, sort_by, cursor)))
        if cursor:
            page = self._paginate([], per_page, cursor, "list")
            return {"files": page["files"], "cursor": page["cursor"]}
        if parent_id != 0 and parent_id not in self.files:
            raise PutioError(NOT_FOUND, "put.io has no such file, folder, or transfer (NotFound)", status=404)
        page = self._paginate(self._children(parent_id), per_page, None, "list")
        parent = self.files.get(parent_id) or {
            "id": 0,
            "name": "Your Files",
            "parent_id": None,
            "content_type": FOLDER_CONTENT_TYPE,
            "file_type": "FOLDER",
        }
        page["parent"] = parent
        return page

    def search(self, query: str, per_page: int, cursor: str | None) -> dict[str, Any]:
        self.calls.append(("search", (query, per_page, cursor)))
        if cursor:
            return self._paginate([], per_page, cursor, "search")
        hits = [f for f in self.files.values() if query.lower() in f["name"].lower()]
        return self._paginate(hits, per_page, None, "search")

    def get_file(self, file_id: int) -> dict[str, Any]:
        self.calls.append(("get_file", (file_id,)))
        if file_id not in self.files:
            raise PutioError(NOT_FOUND, "put.io has no such file, folder, or transfer (NotFound)", status=404)
        return dict(self.files[file_id])

    def create_folder(self, name: str, parent_id: int) -> dict[str, Any]:
        self.calls.append(("create_folder", (name, parent_id)))
        return dict(self.files[self.add_folder(name, parent_id)])

    def rename(self, file_id: int, name: str) -> None:
        self.calls.append(("rename", (file_id, name)))
        self.files[file_id]["name"] = name

    def move(self, file_id: int, parent_id: int) -> None:
        self.calls.append(("move", (file_id, parent_id)))
        if file_id in self.fail_move_ids:
            raise PutioError(PERMISSION, "put.io denied this operation for the current account (Forbidden)", status=403)
        self.files[file_id]["parent_id"] = parent_id

    def list_transfers(self) -> list[dict[str, Any]]:
        self.calls.append(("list_transfers", ()))
        return [dict(t) for t in self.transfers.values()]

    def get_transfer(self, transfer_id: int) -> dict[str, Any]:
        self.calls.append(("get_transfer", (transfer_id,)))
        if transfer_id not in self.transfers:
            raise PutioError(NOT_FOUND, "put.io has no such file, folder, or transfer (NotFound)", status=404)
        return dict(self.transfers[transfer_id])

    def add_transfer(self, url: str, parent_id: int | None) -> dict[str, Any]:
        self.calls.append(("add_transfer", (url, parent_id)))
        tid = self.add_transfer_record(url[:20], status="IN_QUEUE")
        self.transfers[tid]["save_parent_id"] = parent_id
        self.transfers[tid]["source"] = url
        return dict(self.transfers[tid])

    def cancel_transfer(self, transfer_id: int) -> None:
        self.calls.append(("cancel_transfer", (transfer_id,)))
        self.transfers.pop(transfer_id)


@pytest.fixture
def fake() -> FakePutio:
    return FakePutio()


@pytest.fixture
def service(fake: FakePutio) -> PutioService:
    return PutioService(fake)
