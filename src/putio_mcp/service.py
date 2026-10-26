"""Business logic for the put.io MCP tools.

Everything here works against the PutioApi protocol so it can be unit-tested
with an in-memory fake. All public methods return plain JSON-serialisable
dicts and raise PutioError for anything the caller should see.
"""

from __future__ import annotations

from typing import Any, Iterable

from .client import MAX_PER_PAGE, SORT_OPTIONS, PutioApi
from .errors import CONFLICT, INVALID, NOT_FOUND, PutioError

ROOT_ID = 0
FOLDER_CONTENT_TYPE = "application/x-directory"
MAX_NAME_LENGTH = 255
MAX_ANCESTOR_DEPTH = 256
DEFAULT_PAGE = 100


# --------------------------------------------------------------------------- #
# Shaping                                                                      #
# --------------------------------------------------------------------------- #


def is_folder(raw: dict[str, Any]) -> bool:
    return raw.get("content_type") == FOLDER_CONTENT_TYPE or raw.get("file_type") == "FOLDER"


def summarize_file(raw: dict[str, Any]) -> dict[str, Any]:
    """Reduce a put.io File object to the fields Claude needs to explain it."""
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "parent_id": raw.get("parent_id"),
        "is_folder": is_folder(raw),
        "file_type": raw.get("file_type"),
        "content_type": raw.get("content_type"),
        "size": raw.get("size"),
        "created_at": raw.get("created_at"),
        "is_shared": bool(raw.get("is_shared", False)),
    }


def root_summary() -> dict[str, Any]:
    return {
        "id": ROOT_ID,
        "name": "Your Files",
        "parent_id": None,
        "is_folder": True,
        "file_type": "FOLDER",
        "content_type": FOLDER_CONTENT_TYPE,
        "size": None,
        "created_at": None,
        "is_shared": False,
    }


def summarize_transfer(raw: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "name",
        "status",
        "type",
        "percent_done",
        "size",
        "downloaded",
        "uploaded",
        "down_speed",
        "up_speed",
        "estimated_time",
        "peers",
        "availability",
        "file_id",
        "save_parent_id",
        "error_message",
        "tracker_message",
        "created_at",
        "finished_at",
        "source",
    )
    return {k: raw.get(k) for k in keys if k in raw}


# --------------------------------------------------------------------------- #
# Validation                                                                   #
# --------------------------------------------------------------------------- #


def validate_name(name: Any, what: str = "name") -> str:
    if not isinstance(name, str):
        raise PutioError(INVALID, f"{what} must be a string.")
    cleaned = name.strip()
    if not cleaned:
        raise PutioError(INVALID, f"{what} must not be empty.")
    if cleaned in (".", ".."):
        raise PutioError(INVALID, f"{what} must not be '.' or '..'.")
    if "/" in cleaned or "\\" in cleaned:
        raise PutioError(INVALID, f"{what} must not contain path separators ('/' or '\\').")
    if "\x00" in cleaned or any(ord(c) < 32 for c in cleaned):
        raise PutioError(INVALID, f"{what} must not contain control characters.")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise PutioError(INVALID, f"{what} must be at most {MAX_NAME_LENGTH} characters.")
    return cleaned


def validate_id(value: Any, what: str, *, allow_root: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PutioError(INVALID, f"{what} must be an integer put.io ID.")
    if value < 0:
        raise PutioError(INVALID, f"{what} must not be negative (got {value}).")
    if value == ROOT_ID and not allow_root:
        raise PutioError(INVALID, f"{what} must not be the root folder (0).")
    return value


def validate_per_page(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PutioError(INVALID, "per_page must be an integer.")
    if value < 1 or value > MAX_PER_PAGE:
        raise PutioError(INVALID, f"per_page must be between 1 and {MAX_PER_PAGE}.")
    return value


def validate_sort(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    upper = value.upper()
    if upper not in SORT_OPTIONS:
        raise PutioError(INVALID, f"sort_by must be one of {', '.join(SORT_OPTIONS)}.")
    return upper


def names_match(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


# --------------------------------------------------------------------------- #
# Service                                                                      #
# --------------------------------------------------------------------------- #


class PutioService:
    def __init__(self, api: PutioApi) -> None:
        self.api = api

    # -- lookups -------------------------------------------------------------

    def _get(self, file_id: int) -> dict[str, Any]:
        if file_id == ROOT_ID:
            return root_summary()
        return summarize_file(self.api.get_file(file_id))

    def _require_folder(self, folder_id: int, what: str) -> dict[str, Any]:
        try:
            folder = self._get(folder_id)
        except PutioError as exc:
            if exc.kind == NOT_FOUND:
                raise PutioError(NOT_FOUND, f"{what} {folder_id} does not exist.") from None
            raise
        if not folder["is_folder"]:
            raise PutioError(
                INVALID, f"{what} {folder_id} is a file named {folder['name']!r}, not a folder."
            )
        return folder

    def _children(self, folder_id: int) -> list[dict[str, Any]]:
        """All direct children of a folder, following cursors to the end."""
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = self.api.list_folder(folder_id, MAX_PER_PAGE, None, cursor)
            items.extend(summarize_file(f) for f in page.get("files", []))
            cursor = page.get("cursor") or None
            if not cursor:
                return items

    def _ancestor_ids(self, folder_id: int) -> list[int]:
        """IDs from ``folder_id`` up to (but excluding) the root, nearest first."""
        chain: list[int] = []
        current = folder_id
        while current not in (ROOT_ID, None):
            if current in chain or len(chain) >= MAX_ANCESTOR_DEPTH:
                raise PutioError(INVALID, "folder hierarchy is too deep or cyclic; aborting.")
            chain.append(current)
            current = self._get(current)["parent_id"]
        return chain

    @staticmethod
    def _find_conflict(
        siblings: Iterable[dict[str, Any]], name: str, *, exclude_id: int | None = None
    ) -> dict[str, Any] | None:
        for item in siblings:
            if item["id"] == exclude_id:
                continue
            if item["name"] and names_match(item["name"], name):
                return item
        return None

    # -- browsing ------------------------------------------------------------

    def list_files(
        self,
        folder_id: int = ROOT_ID,
        per_page: int = DEFAULT_PAGE,
        cursor: str | None = None,
        sort_by: str | None = None,
    ) -> dict[str, Any]:
        folder_id = validate_id(folder_id, "folder_id", allow_root=True)
        per_page = validate_per_page(per_page)
        sort_by = validate_sort(sort_by)
        page = self.api.list_folder(folder_id, per_page, sort_by, cursor or None)
        items = [summarize_file(f) for f in page.get("files", [])]
        result: dict[str, Any] = {
            "folder": None,
            "items": items,
            "count": len(items),
            "total": page.get("total"),
            "next_cursor": page.get("cursor") or None,
        }
        if not cursor:
            parent = page.get("parent")
            result["folder"] = summarize_file(parent) if parent else self._get(folder_id)
        return result

    def get_file(self, file_id: int) -> dict[str, Any]:
        file_id = validate_id(file_id, "file_id", allow_root=True)
        return self._get(file_id)

    def search_files(
        self, query: str, per_page: int = 50, cursor: str | None = None
    ) -> dict[str, Any]:
        if not cursor:
            if not isinstance(query, str) or not query.strip():
                raise PutioError(INVALID, "query must not be empty.")
        per_page = validate_per_page(per_page)
        page = self.api.search(query.strip() if query else "", per_page, cursor or None)
        items = [summarize_file(f) for f in page.get("files", [])]
        return {
            "query": query,
            "items": items,
            "count": len(items),
            "total": page.get("total"),
            "next_cursor": page.get("cursor") or None,
        }

    # -- organisation --------------------------------------------------------

    def create_folder(
        self, name: str, parent_id: int = ROOT_ID, dry_run: bool = False
    ) -> dict[str, Any]:
        name = validate_name(name, "folder name")
        parent_id = validate_id(parent_id, "parent_id", allow_root=True)
        parent = self._require_folder(parent_id, "parent folder")
        existing = self._find_conflict(self._children(parent_id), name)
        if existing:
            kind = "folder" if existing["is_folder"] else "file"
            raise PutioError(
                CONFLICT,
                f"a {kind} named {existing['name']!r} (id {existing['id']}) already exists in "
                f"{parent['name']!r} (id {parent_id}).",
                details={"existing": existing},
            )
        if dry_run:
            return {
                "dry_run": True,
                "would_create": {"name": name, "parent_id": parent_id, "parent_name": parent["name"]},
            }
        created = summarize_file(self.api.create_folder(name, parent_id))
        return {"dry_run": False, "created": created, "parent": parent}

    def rename_file(self, file_id: int, new_name: str, dry_run: bool = False) -> dict[str, Any]:
        file_id = validate_id(file_id, "file_id", allow_root=False)
        new_name = validate_name(new_name, "new_name")
        try:
            item = self._get(file_id)
        except PutioError as exc:
            if exc.kind == NOT_FOUND:
                raise PutioError(NOT_FOUND, f"file {file_id} does not exist.") from None
            raise
        if item["name"] == new_name:
            return {
                "dry_run": dry_run,
                "changed": False,
                "item": item,
                "message": f"{item['name']!r} already has that name; nothing to do.",
            }
        parent_id = item["parent_id"] if item["parent_id"] is not None else ROOT_ID
        conflict = self._find_conflict(self._children(parent_id), new_name, exclude_id=file_id)
        if conflict:
            kind = "folder" if conflict["is_folder"] else "file"
            raise PutioError(
                CONFLICT,
                f"cannot rename {item['name']!r} to {new_name!r}: a {kind} named "
                f"{conflict['name']!r} (id {conflict['id']}) already exists in the same folder "
                f"(id {parent_id}).",
                details={"existing": conflict},
            )
        if dry_run:
            return {
                "dry_run": True,
                "changed": False,
                "item": item,
                "would_rename_to": new_name,
            }
        self.api.rename(file_id, new_name)
        after = dict(item, name=new_name)
        return {"dry_run": False, "changed": True, "previous_name": item["name"], "item": after}

    def move_files(
        self, file_ids: list[int], destination_id: int, dry_run: bool = False
    ) -> dict[str, Any]:
        if not isinstance(file_ids, list) or not file_ids:
            raise PutioError(INVALID, "file_ids must be a non-empty list of put.io IDs.")
        destination_id = validate_id(destination_id, "destination_id", allow_root=True)
        destination = self._require_folder(destination_id, "destination folder")
        forbidden = set(self._ancestor_ids(destination_id))  # destination + its ancestors
        existing_children = self._children(destination_id)
        claimed_names: list[dict[str, Any]] = list(existing_children)

        results: list[dict[str, Any]] = []
        seen: set[int] = set()
        to_move: list[dict[str, Any]] = []

        for raw_id in file_ids:
            try:
                fid = validate_id(raw_id, "file_id", allow_root=False)
            except PutioError as exc:
                results.append(
                    {"file_id": raw_id, "name": None, "status": "failed", "error_kind": exc.kind, "reason": exc.message}
                )
                continue
            if fid in seen:
                results.append(
                    {"file_id": fid, "name": None, "status": "skipped", "reason": "duplicate id in request."}
                )
                continue
            seen.add(fid)

            try:
                item = self._get(fid)
            except PutioError as exc:
                reason = f"file {fid} does not exist." if exc.kind == NOT_FOUND else exc.message
                results.append(
                    {"file_id": fid, "name": None, "status": "failed", "error_kind": exc.kind, "reason": reason}
                )
                continue

            entry = {"file_id": fid, "name": item["name"]}
            if fid in forbidden:
                what = "itself" if fid == destination_id else "its own descendant"
                results.append(
                    dict(
                        entry,
                        status="failed",
                        error_kind=INVALID,
                        reason=f"cannot move folder {item['name']!r} into {what}.",
                    )
                )
                continue
            if item["parent_id"] == destination_id:
                results.append(dict(entry, status="skipped", reason="already in the destination folder."))
                continue
            conflict = self._find_conflict(claimed_names, item["name"])
            if conflict:
                src = "another item in this request" if conflict.get("_from_request") else f"id {conflict['id']}"
                results.append(
                    dict(
                        entry,
                        status="failed",
                        error_kind=CONFLICT,
                        reason=f"an item named {conflict['name']!r} ({src}) is already in the destination.",
                    )
                )
                continue
            claimed_names.append(dict(item, _from_request=True))
            to_move.append(item)
            results.append(dict(entry, status="would_move" if dry_run else "pending", from_parent_id=item["parent_id"]))

        if not dry_run:
            by_id = {r["file_id"]: r for r in results if r["status"] == "pending"}
            for item in to_move:
                r = by_id[item["id"]]
                try:
                    self.api.move(item["id"], destination_id)
                    r["status"] = "moved"
                except PutioError as exc:
                    r["status"] = "failed"
                    r["error_kind"] = exc.kind
                    r["reason"] = exc.message

        summary = {
            "moved": sum(r["status"] == "moved" for r in results),
            "would_move": sum(r["status"] == "would_move" for r in results),
            "skipped": sum(r["status"] == "skipped" for r in results),
            "failed": sum(r["status"] == "failed" for r in results),
        }
        return {
            "dry_run": dry_run,
            "destination": destination,
            "results": results,
            "summary": summary,
            "all_succeeded": summary["failed"] == 0,
        }

    # -- transfers -----------------------------------------------------------

    def list_transfers(self) -> list[dict[str, Any]]:
        return [summarize_transfer(t) for t in self.api.list_transfers()]

    def add_transfer(self, url: str, destination_id: int | None = None) -> dict[str, Any]:
        if not isinstance(url, str) or not url.strip():
            raise PutioError(INVALID, "url must not be empty.")
        url = url.strip()
        lowered = url.lower()
        if not (lowered.startswith(("http://", "https://", "magnet:"))):
            raise PutioError(INVALID, "url must be an http(s) link or a magnet link.")
        destination: dict[str, Any] | None = None
        if destination_id is not None:
            destination_id = validate_id(destination_id, "destination_id", allow_root=True)
            destination = self._require_folder(destination_id, "destination folder")
        transfer = summarize_transfer(self.api.add_transfer(url, destination_id))
        return {"transfer": transfer, "destination": destination}

    def cancel_transfer(self, transfer_id: int) -> dict[str, Any]:
        transfer_id = validate_id(transfer_id, "transfer_id", allow_root=False)
        transfer = summarize_transfer(self.api.get_transfer(transfer_id))
        self.api.cancel_transfer(transfer_id)
        return {"cancelled": transfer}

    def get_browser_link(self, transfer_id: int) -> dict[str, Any]:
        transfer_id = validate_id(transfer_id, "transfer_id", allow_root=False)
        transfer = summarize_transfer(self.api.get_transfer(transfer_id))
        file_id = transfer.get("file_id")
        if not file_id:
            return {
                "transfer": transfer,
                "url": None,
                "message": "Transfer has no files yet; it is not completed. Try again later.",
            }
        return {"transfer": transfer, "url": f"https://app.put.io/files/{file_id}"}
