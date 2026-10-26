# Upstream provenance

This project started as a fork of put.io's official MCP server.

| | |
|---|---|
| Upstream repository | https://github.com/putdotio/putio-mcp-server |
| Starting revision | `ed83c0f907cdd14193111c1460d31354301e8c00` (main, 2026-04-19, "chore: remove year from license") |
| Upstream version | 0.1.3 |
| License | MIT (preserved verbatim in `LICENSE`, with a line added for modifications) |
| Forked on | 2026-10-26 |

## What was kept

- Python, the `mcp` SDK's FastMCP server class, and the `putio-py` client library.
- The four transfer tools: `list_transfers`, `add_transfer`, `cancel_transfer`, `get_browser_link`.
- The MIT license and put.io's copyright notice.
- The upstream `.gitignore` and `.python-version`.

## What changed

- `mcp` is pinned to `<2`. The 2.x SDK renamed FastMCP to `MCPServer` and changed
  other APIs; upstream's code (and this fork) targets the 1.x FastMCP API.
- The single `main.py` was split into a package (`src/putio_mcp/`): a thin API client
  with error classification and token redaction, a service layer with validation and
  dry-run previews, and the FastMCP tool definitions.
- `add_transfer` accepts an optional, validated `destination_id`.
- New file/folder tools: `list_files`, `get_file`, `search_files`, `create_folder`,
  `rename_file`, `move_files`.
- The put.io client is created lazily so a missing `PUTIO_TOKEN` yields a clear tool
  error instead of a crash at import time.
- Tests (`tests/`) and a live smoke test (`scripts/live_smoke.py`).

## Syncing with upstream

Upstream is small enough that changes are best ported by hand. To see what changed since
the starting revision:

```sh
git clone https://github.com/putdotio/putio-mcp-server /tmp/putio-upstream
git -C /tmp/putio-upstream log --oneline ed83c0f907cdd14193111c1460d31354301e8c00..main
```
