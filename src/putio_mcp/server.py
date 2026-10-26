"""FastMCP server exposing put.io tools to Claude Desktop.

Design notes:
  * The put.io client is created lazily on first tool call so that the server
    can start (and report a clear error) even when PUTIO_TOKEN is missing.
  * All diagnostics go to stderr via ``logging``; stdout is reserved for the
    MCP stdio transport.
  * Every PutioError is converted into a ToolError so Claude receives a
    classified message instead of a stack trace.
"""

from __future__ import annotations

import functools
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .client import PutioClient
from .errors import CONFIG, PutioError, redact
from .service import PutioService

log = logging.getLogger("putio_mcp")

TOKEN_ENV = "PUTIO_TOKEN"
IGNORE_PROXY_ENV = "PUTIO_MCP_IGNORE_PROXY"
ENV_FILE_ENV = "PUTIO_MCP_ENV_FILE"
REPO_ROOT = Path(__file__).resolve().parents[2]

INSTRUCTIONS = """Tools for a single put.io account: list and add download transfers, and
browse / organise files and folders. IDs are put.io integer IDs; folder id 0 is the
root ("Your Files"). Paginated tools return next_cursor when more results exist; pass it
back as cursor to continue. Organisation tools (create_folder, rename_file, move_files)
accept dry_run=true to validate without changing the account. Deletion is intentionally
not available."""


def load_env_file() -> Path | None:
    """Load the first .env file found. Existing environment variables always win.

    Search order: $PUTIO_MCP_ENV_FILE, ./.env, <repo root>/.env. Returns the path
    that was loaded, or None. The file is gitignored; it is the recommended place
    for PUTIO_TOKEN so the token never has to live in claude_desktop_config.json.
    """
    candidates: list[Path] = []
    explicit = os.environ.get(ENV_FILE_ENV, "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(Path.cwd() / ".env")
    candidates.append(REPO_ROOT / ".env")
    for path in candidates:
        if path.is_file():
            load_dotenv(path, override=False)
            return path
    if explicit:
        log.warning("%s points to %s but no such file exists", ENV_FILE_ENV, explicit)
    return None


def configure_logging() -> None:
    level = os.environ.get("PUTIO_MCP_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # putiopy logs raw response bodies at DEBUG; never let that reach a log.
    logging.getLogger("putiopy").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def service_from_env() -> PutioService:
    token = os.environ.get(TOKEN_ENV, "")
    if not token.strip():
        load_env_file()
        token = os.environ.get(TOKEN_ENV, "")
    if not token.strip():
        raise PutioError(
            CONFIG,
            f"{TOKEN_ENV} is not set. Put PUTIO_TOKEN=... in {REPO_ROOT / '.env'} "
            "or in the server's env in claude_desktop_config.json.",
        )
    ignore_proxy = os.environ.get(IGNORE_PROXY_ENV, "").strip().lower() in ("1", "true", "yes")
    return PutioService(PutioClient(token, ignore_proxy=ignore_proxy))


def create_server(service_factory: Callable[[], PutioService] = service_from_env) -> FastMCP:
    mcp = FastMCP("putio", instructions=INSTRUCTIONS)
    cache: dict[str, PutioService] = {}

    def svc() -> PutioService:
        if "service" not in cache:
            cache["service"] = service_factory()
        return cache["service"]

    def guarded(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            token = os.environ.get(TOKEN_ENV)
            try:
                return fn(*args, **kwargs)
            except PutioError as exc:
                log.warning("%s failed: %s", fn.__name__, redact(exc, token))
                raise ToolError(redact(str(exc), token)) from None
            except ToolError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("%s crashed", fn.__name__)
                raise ToolError(
                    f"internal_error: {redact(exc.__class__.__name__, token)} while running {fn.__name__}"
                ) from None

        return wrapper

    # ---------------------------------------------------------------- transfers

    @mcp.tool()
    @guarded
    def list_transfers() -> list[dict[str, Any]]:
        """List the account's transfers (queued, downloading, seeding, completed, errored)."""
        return svc().list_transfers()

    @mcp.tool()
    @guarded
    def add_transfer(url: str, destination_id: int | None = None) -> dict[str, Any]:
        """Add a transfer from an HTTP(S) URL or magnet link.

        destination_id: optional folder id to save the files into (0 = root). When omitted,
        put.io uses the account's default download folder. The folder is validated first."""
        return svc().add_transfer(url, destination_id)

    @mcp.tool()
    @guarded
    def cancel_transfer(transfer_id: int) -> dict[str, Any]:
        """Cancel a transfer. Seeding transfers stop seeding; others are removed from the
        list. Already-downloaded files are NOT deleted."""
        return svc().cancel_transfer(transfer_id)

    @mcp.tool()
    @guarded
    def get_browser_link(transfer_id: int) -> dict[str, Any]:
        """Get an app.put.io link for a completed transfer's files."""
        return svc().get_browser_link(transfer_id)

    # -------------------------------------------------------------------- files

    @mcp.tool()
    @guarded
    def list_files(
        folder_id: int = 0,
        per_page: int = 100,
        cursor: str | None = None,
        sort_by: str | None = None,
    ) -> dict[str, Any]:
        """List a folder's direct children with pagination.

        folder_id: 0 for the root folder. per_page: 1-1000. cursor: value of next_cursor
        from the previous page (folder_id/sort_by are ignored when continuing). sort_by: one
        of NAME_ASC, NAME_DESC, SIZE_ASC, SIZE_DESC, DATE_ASC, DATE_DESC, MODIFIED_ASC,
        MODIFIED_DESC."""
        return svc().list_files(folder_id, per_page, cursor, sort_by)

    @mcp.tool()
    @guarded
    def get_file(file_id: int) -> dict[str, Any]:
        """Get details (name, parent_id, is_folder, size, type) of a file or folder by id."""
        return svc().get_file(file_id)

    @mcp.tool()
    @guarded
    def search_files(query: str, per_page: int = 50, cursor: str | None = None) -> dict[str, Any]:
        """Search the account's files and folders by keyword. Paginated via cursor."""
        return svc().search_files(query, per_page, cursor)

    @mcp.tool()
    @guarded
    def create_folder(name: str, parent_id: int = 0, dry_run: bool = False) -> dict[str, Any]:
        """Create a folder named `name` inside `parent_id` (0 = root).

        Fails with name_conflict if an item with that name already exists there.
        dry_run=true validates without creating anything."""
        return svc().create_folder(name, parent_id, dry_run)

    @mcp.tool()
    @guarded
    def rename_file(file_id: int, new_name: str, dry_run: bool = False) -> dict[str, Any]:
        """Rename a file or folder. Fails with name_conflict if a sibling already has the
        new name. dry_run=true validates without renaming."""
        return svc().rename_file(file_id, new_name, dry_run)

    @mcp.tool()
    @guarded
    def move_files(file_ids: list[int], destination_id: int, dry_run: bool = False) -> dict[str, Any]:
        """Move files/folders into destination_id (0 = root).

        Each id is validated and reported individually (moved / would_move / skipped /
        failed with a reason), so partial failures are visible. Refuses to move a folder
        into itself or its descendants and reports name conflicts. dry_run=true previews
        the outcome without changing the account."""
        return svc().move_files(file_ids, destination_id, dry_run)

    return mcp


def main() -> None:
    configure_logging()
    loaded = load_env_file()
    if loaded:
        log.info("loaded environment from %s", loaded)
    if not os.environ.get(TOKEN_ENV, "").strip():
        log.warning("%s is not set; tools will fail until it is configured.", TOKEN_ENV)
    log.info("starting putio-mcp over stdio")
    create_server().run()


if __name__ == "__main__":
    main()
