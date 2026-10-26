"""Error translation and credential redaction for the real client."""

from __future__ import annotations

import json
from unittest import mock

import putiopy
import pytest
import requests

from putio_mcp.client import PutioClient
from putio_mcp.errors import (
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

TOKEN = "SECRETTOKEN1234567890"


def _response(status: int, body: dict | None = None, headers: dict | None = None) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp._content = json.dumps(body if body is not None else {}).encode()
    resp.headers.update(headers or {})
    req = requests.Request("GET", "https://api.put.io/v2/files/1").prepare()
    resp.request = req
    return resp


def _client_with_response(status: int, body: dict | None = None, headers: dict | None = None) -> PutioClient:
    client = PutioClient(TOKEN)
    client._client.session.request = mock.Mock(return_value=_response(status, body, headers))  # type: ignore[method-assign]
    return client


@pytest.mark.parametrize(
    "status,body,kind,needle",
    [
        (401, {"error_type": "invalid_grant", "error_message": "token expired"}, AUTH, "PUTIO_TOKEN"),
        (403, {"error_type": "Forbidden", "error_message": "not yours"}, PERMISSION, "denied"),
        (404, {"error_type": "NotFound", "error_message": "no file"}, NOT_FOUND, "no such file"),
        (429, {"error_type": "TooManyRequests", "error_message": "slow down"}, RATE_LIMIT, "rate limit"),
        (400, {"error_type": "BadRequest", "error_message": "nope"}, INVALID, "HTTP 400"),
        (500, {"error_type": "Internal", "error_message": "boom"}, SERVER, "HTTP 500"),
        (503, {}, SERVER, "HTTP 503"),
    ],
)
def test_http_errors_are_classified(status, body, kind, needle):
    client = _client_with_response(status, body)
    with pytest.raises(PutioError) as e:
        client.get_file(1)
    assert e.value.kind == kind
    assert e.value.status == status
    assert needle in e.value.message
    if body.get("error_type"):
        assert body["error_type"] in e.value.message


def test_rate_limit_includes_retry_after():
    client = _client_with_response(429, {"error_type": "x", "error_message": "y"}, {"Retry-After": "17"})
    with pytest.raises(PutioError) as e:
        client.list_transfers()
    assert e.value.details["retry_after_seconds"] == "17"
    assert "retry after 17s" in e.value.message


def test_network_errors_are_classified():
    client = PutioClient(TOKEN)
    client._client.session.request = mock.Mock(side_effect=requests.exceptions.ConnectTimeout())  # type: ignore[method-assign]
    with pytest.raises(PutioError) as e:
        client.list_transfers()
    assert e.value.kind == NETWORK

    client._client.session.request = mock.Mock(side_effect=requests.exceptions.ConnectionError(TOKEN))  # type: ignore[method-assign]
    with pytest.raises(PutioError) as e:
        client.list_transfers()
    assert e.value.kind == NETWORK and TOKEN not in str(e.value)


def test_token_is_redacted_from_api_error_text():
    body = {"error_type": f"bad token {TOKEN}", "error_message": f"token {TOKEN} is invalid"}
    client = _client_with_response(401, body)
    with pytest.raises(PutioError) as e:
        client.get_file(1)
    text = str(e.value) + json.dumps(e.value.details)
    assert TOKEN not in text
    assert "[REDACTED]" in text


def test_token_sent_only_in_authorization_header():
    client = PutioClient(TOKEN)
    client._client.session.request = mock.Mock(return_value=_response(200, {"file": {"id": 1, "name": "x"}}))  # type: ignore[method-assign]
    client.get_file(1)
    _, kwargs = client._client.session.request.call_args
    assert kwargs["headers"]["Authorization"] == f"token {TOKEN}"
    args, _ = client._client.session.request.call_args
    assert TOKEN not in args[1]  # not in the URL


def test_empty_token_rejected():
    with pytest.raises(PutioError) as e:
        PutioClient("   ")
    assert e.value.kind == AUTH


def test_redact_helper():
    assert redact("abc SECRET def", "SECRET") == "abc [REDACTED] def"
    assert redact("abc", None) == "abc"
    assert redact("ab", "ab") == "ab"  # too short to be a credential; left alone


def test_pagination_calls_continue_endpoint():
    client = PutioClient(TOKEN)
    m = mock.Mock(return_value=_response(200, {"files": [], "cursor": None}))
    client._client.session.request = m  # type: ignore[method-assign]
    client.list_folder(0, 50, "NAME_ASC", None)
    args, kwargs = m.call_args
    assert args[0] == "GET" and args[1].endswith("/files/list")
    assert kwargs["params"] == {"parent_id": "0", "per_page": "50", "sort_by": "NAME_ASC"}

    client.list_folder(0, 50, None, "CURSOR123")
    args, kwargs = m.call_args
    assert args[0] == "POST" and args[1].endswith("/files/list/continue")
    assert kwargs["data"] == {"cursor": "CURSOR123", "per_page": "50"}

    client.search("foo", 25, "C2")
    args, kwargs = m.call_args
    assert args[1].endswith("/files/search/continue") and kwargs["data"]["cursor"] == "C2"


def test_add_transfer_sends_save_parent_id():
    client = PutioClient(TOKEN)
    m = mock.Mock(return_value=_response(200, {"transfer": {"id": 9}}))
    client._client.session.request = m  # type: ignore[method-assign]
    client.add_transfer("magnet:?xt=1", 42)
    assert m.call_args.kwargs["data"] == {"url": "magnet:?xt=1", "save_parent_id": "42"}
    client.add_transfer("magnet:?xt=1", None)
    assert m.call_args.kwargs["data"] == {"url": "magnet:?xt=1"}


def test_apierror_from_putiopy_directly_translates():
    client = PutioClient(TOKEN)
    resp = _response(404, {"error_type": "NotFound", "error_message": "gone"})
    client._client.request = mock.Mock(side_effect=putiopy.ClientError(resp, "NotFound", "gone"))  # type: ignore[method-assign]
    with pytest.raises(PutioError) as e:
        client.rename(5, "x")
    assert e.value.kind == NOT_FOUND


def test_ignore_proxy_disables_environment_proxies():
    assert PutioClient(TOKEN)._client.session.trust_env is True
    assert PutioClient(TOKEN, ignore_proxy=True)._client.session.trust_env is False


def test_service_from_env_honours_ignore_proxy(monkeypatch):
    from putio_mcp.server import service_from_env

    monkeypatch.setenv("PUTIO_TOKEN", TOKEN)
    monkeypatch.delenv("PUTIO_MCP_IGNORE_PROXY", raising=False)
    assert service_from_env().api._client.session.trust_env is True
    monkeypatch.setenv("PUTIO_MCP_IGNORE_PROXY", "1")
    assert service_from_env().api._client.session.trust_env is False
