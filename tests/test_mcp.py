"""Exercise the server through a real MCP client session (in-memory transport)."""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from putio_mcp.errors import CONFIG, PutioError
from putio_mcp.server import create_server
from putio_mcp.service import PutioService

EXPECTED_TOOLS = {
    "list_transfers",
    "add_transfer",
    "cancel_transfer",
    "get_browser_link",
    "list_files",
    "get_file",
    "search_files",
    "create_folder",
    "rename_file",
    "move_files",
}


def _payload(result: Any) -> Any:
    if result.structuredContent is not None:
        sc = result.structuredContent
        return sc["result"] if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(result.content[0].text)


def _text(result: Any) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


@pytest.fixture
def server(fake):
    return create_server(lambda: PutioService(fake))


async def test_tool_discovery(server):
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools.tools}
        assert names == EXPECTED_TOOLS
        by_name = {t.name: t for t in tools.tools}
        move = by_name["move_files"].inputSchema
        assert set(move["required"]) == {"file_ids", "destination_id"}
        assert move["properties"]["file_ids"]["type"] == "array"
        assert move["properties"]["dry_run"]["default"] is False
        assert "destination_id" in by_name["add_transfer"].inputSchema["properties"]
        assert "delete" not in " ".join(names)


async def test_input_validation_at_protocol_level(server):
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        res = await client.call_tool("move_files", {"destination_id": 0})
        assert res.isError and "file_ids" in _text(res)

        res = await client.call_tool("list_files", {"folder_id": "not-an-int"})
        assert res.isError

        res = await client.call_tool("create_folder", {"name": "bad/name", "parent_id": 0})
        assert res.isError and "invalid_request" in _text(res)


async def test_end_to_end_organise_flow(fake, server):
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        a = fake.add_file("a.mkv")
        b = fake.add_file("b.mkv")

        created = _payload(await client.call_tool("create_folder", {"name": "Season 1", "parent_id": 0}))
        folder_id = created["created"]["id"]

        renamed = _payload(
            await client.call_tool("rename_file", {"file_id": folder_id, "new_name": "Season 01"})
        )
        assert renamed["item"]["name"] == "Season 01"

        moved = _payload(
            await client.call_tool("move_files", {"file_ids": [a, b], "destination_id": folder_id})
        )
        assert moved["summary"]["moved"] == 2 and moved["all_succeeded"]

        listing = _payload(await client.call_tool("list_files", {"folder_id": folder_id}))
        assert sorted(i["name"] for i in listing["items"]) == ["a.mkv", "b.mkv"]
        assert listing["folder"]["name"] == "Season 01"


async def test_conflict_surfaces_as_tool_error(fake, server):
    fake.add_folder("Movies")
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        res = await client.call_tool("create_folder", {"name": "movies", "parent_id": 0})
        assert res.isError and "name_conflict" in _text(res)


async def test_missing_token_gives_configuration_error(monkeypatch):
    monkeypatch.delenv("PUTIO_TOKEN", raising=False)
    server = create_server()  # default factory reads the environment
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        res = await client.call_tool("list_transfers", {})
        assert res.isError
        assert CONFIG in _text(res) and "PUTIO_TOKEN" in _text(res)


async def test_tool_errors_never_contain_token(fake, monkeypatch):
    token = "TOPSECRETTOKENVALUE"
    monkeypatch.setenv("PUTIO_TOKEN", token)

    class Leaky(PutioService):
        def list_transfers(self):
            raise PutioError("putio_server_error", f"upstream said: {token}")

    server = create_server(lambda: Leaky(fake))
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        res = await client.call_tool("list_transfers", {})
        assert res.isError
        assert token not in _text(res) and "[REDACTED]" in _text(res)
