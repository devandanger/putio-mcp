# putio-mcp

A personal [MCP](https://modelcontextprotocol.io) server that lets Claude Desktop manage a
[put.io](https://put.io) account: watch and add transfers, browse folders, and organise
files. It is a fork of put.io's official
[putio-mcp-server](https://github.com/putdotio/putio-mcp-server) (MIT); see
[docs/UPSTREAM.md](docs/UPSTREAM.md) for provenance.

It runs locally over stdio. There is no hosted service, and nothing is published to PyPI.

## Tools

| Tool | What it does |
|---|---|
| `list_transfers` | List transfers with status, progress, and the folder they save into. |
| `add_transfer` | Add an HTTP(S) or magnet transfer, optionally into a specific folder (`destination_id`). |
| `cancel_transfer` | Cancel a transfer. Downloaded files are kept. |
| `get_browser_link` | Get an `app.put.io` link for a completed transfer's files. |
| `list_files` | List a folder's contents with cursor pagination and optional sorting. |
| `get_file` | Details for one file or folder (name, parent, type, size). |
| `search_files` | Keyword search across the account, paginated. |
| `create_folder` | Create a folder under a parent. Refuses duplicate names. |
| `rename_file` | Rename a file or folder. Refuses names already used by a sibling. |
| `move_files` | Move items into a folder with a per-item outcome report. |

`create_folder`, `rename_file`, and `move_files` accept `dry_run: true`, which runs every
validation (destination exists and is a folder, names are legal, no conflicts, no moving
a folder into itself or a descendant) without changing the account.

Deletion is deliberately not exposed in this version.

## Prerequisites

- macOS or Linux with [Claude Desktop](https://claude.ai/download)
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (manages Python 3.12 and dependencies)
- A put.io account and an OAuth token
  ([how to get one](https://help.put.io/en/articles/5972538-how-to-get-an-oauth-token-from-put-io))

## Install

```sh
git clone git@github.com:devandanger/putio-mcp.git
cd putio-mcp
uv sync
cp .env.example .env
```

Open `.env` and paste your put.io token after `PUTIO_TOKEN=`. The file is gitignored, so
the token stays on this machine and out of `claude_desktop_config.json`.

To get a token: sign in at https://app.put.io/oauth, click **Create App**, give it any
unique name, then click the **key icon** next to the app and copy the token from the
Secrets page. You need the OAuth token, not the client ID or client secret.

Run the server once by hand to confirm it starts (it waits for an MCP client on stdin;
press Ctrl-C to stop):

```sh
uv run putio-mcp
```

## Claude Desktop configuration

Open the config file (Claude Desktop: Settings, Developer, Edit Config), or edit it directly:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Add a server entry, replacing the directory with where you cloned the repo. With the token
in `.env`, no `env` block is needed:

```json
{
  "mcpServers": {
    "putio": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/putio-mcp", "run", "putio-mcp"]
    }
  }
}
```

If you prefer not to use `.env`, pass the token in the config instead. A real environment
variable always takes precedence over `.env`:

```json
{
  "mcpServers": {
    "putio": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/putio-mcp", "run", "putio-mcp"],
      "env": {
        "PUTIO_TOKEN": "<your-putio-api-token>"
      }
    }
  }
}
```

The server looks for `.env` at `$PUTIO_MCP_ENV_FILE`, then the current directory, then the
repo root.

If Claude Desktop cannot find `uv`, use its absolute path (`which uv`, typically
`~/.local/bin/uv` or `/opt/homebrew/bin/uv`) as `command`.

Restart Claude Desktop. The put.io tools appear under the tools menu in a new chat.

The token lives only in `.env` (or the config file) and the server's environment. It is
never written to Git, returned in a tool response, or logged; error messages that could
echo it are redacted.

## Example requests

Browsing:

- "What's in my put.io root folder?"
- "Show me the next page of that folder."
- "Search my put.io files for 'season 2'."
- "What transfers are running right now?"

Organising:

- "Create a folder called 'TV / Severance' inside Shows." (Claude will tell you the slash
  is not allowed and suggest a name.)
- "Preview moving everything matching 'Severance' from the root into that folder."
  (uses `dry_run`)
- "Go ahead and move them, then list the folder so I can check."
- "Rename folder 12345 to 'Severance (2022)'."

Transfers:

- "Add this magnet link and save it into my Movies folder."
- "Cancel transfer 987654."
- "Give me a browser link for the transfer that just finished."

## Troubleshooting

**The tools do not appear in Claude Desktop.** Check the server log:
`~/Library/Logs/Claude/mcp-server-putio.log` on macOS. Common causes are a wrong
`--directory` path, `uv` not on Claude Desktop's PATH (use an absolute `command`), or
invalid JSON in the config file.

**`configuration_error: PUTIO_TOKEN is not set`.** There is no `.env` in the repo root with
`PUTIO_TOKEN=...`, and no `env` block in the config entry. Create one and restart Claude
Desktop.

**`authentication_error`.** The token is invalid, revoked, or expired. Create a new one in
put.io and update the config.

**`rate_limited`.** put.io throttled the request. The message includes a retry-after hint
when put.io provides one.

**`name_conflict`.** An item with that name (ignoring case) already exists in the target
folder. Rename or pick another destination.

**`network_error: could not reach put.io: ProxyError`.** The server honours system and
`HTTPS_PROXY` proxy settings. If a configured proxy is down, add
`"PUTIO_MCP_IGNORE_PROXY": "1"` to the `env` block to connect directly.

**Verbose logs.** Set `"PUTIO_MCP_LOG_LEVEL": "DEBUG"` in the `env` block. Logs go to
stderr (which Claude Desktop captures to the log file above), never to stdout.

## Updating

```sh
cd /path/to/putio-mcp
git pull
uv sync
```

Then restart Claude Desktop so it relaunches the server.

## Development

```sh
uv sync                 # installs dev dependencies (pytest)
uv run pytest           # unit tests + in-memory MCP protocol tests
```

A live smoke test exercises every tool against your real account inside a temporary folder
that it creates and then deletes:

```sh
uv run python scripts/live_smoke.py          # reads PUTIO_TOKEN from .env
# add --keep to leave the folder for inspection,
# or --transfer-url <http-or-magnet-url> to also test add_transfer with a destination
```

Layout:

- `src/putio_mcp/client.py`: thin put.io API wrapper, error classification, redaction
- `src/putio_mcp/service.py`: validation, conflict and ancestry checks, previews, batch moves
- `src/putio_mcp/server.py`: FastMCP tool definitions and entry point
- `tests/`: in-memory fake of the put.io API plus protocol-level tests

## License

MIT. See [LICENSE](LICENSE); the original copyright belongs to put.io.
