"""Tests for the tray's local-only Zotero connection setup."""
from __future__ import annotations

import email.message
import io
import json
import urllib.error
import urllib.parse
import urllib.request
import urllib.response
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from meridian import tunnel_config, zotero_setup


def _response(payload: object, status: int = 200) -> Mock:
    response = Mock()
    response.status = status
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    return response


def test_list_collections_uses_loopback_api_without_credentials(monkeypatch):
    captured = {}

    def open_request(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _response(
            [
                {"key": "b1234567", "data": {"name": "Zulu"}},
                {"key": "a1234567", "data": {"name": "Alpha"}},
                {"data": {"name": "Malformed"}},
            ]
        )

    monkeypatch.setattr(zotero_setup, "_OPENER", SimpleNamespace(open=open_request))

    result = zotero_setup.list_local_zotero_collections()

    request = captured["request"]
    headers = {name.casefold(): value for name, value in request.header_items()}
    assert request.full_url == "http://127.0.0.1:23119/api/users/0/collections?format=json"
    assert headers["zotero-api-version"] == "3"
    assert "authorization" not in headers
    assert captured["timeout"] == 3.0
    assert result == [
        {"key": "A1234567", "name": "Alpha"},
        {"key": "B1234567", "name": "Zulu"},
    ]


def test_list_collections_explains_local_api_permission(monkeypatch):
    def denied(_request, timeout):
        raise urllib.error.HTTPError("http://127.0.0.1", 403, "Forbidden", {}, None)

    monkeypatch.setattr(zotero_setup, "_OPENER", SimpleNamespace(open=denied))

    with pytest.raises(zotero_setup.ZoteroSetupError, match="Allow other applications"):
        zotero_setup.list_local_zotero_collections()


def test_list_collections_explains_when_zotero_is_offline(monkeypatch):
    def offline(_request, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(zotero_setup, "_OPENER", SimpleNamespace(open=offline))

    with pytest.raises(zotero_setup.ZoteroSetupError, match="Start Zotero"):
        zotero_setup.list_local_zotero_collections()


# ---------------------------------------------------------------------------
# The base URL and the redirects of the local API read. Nothing reaches a socket:
# the fake Zotero below sits under the real opener, and the refused base URLs never send.
# ---------------------------------------------------------------------------

_AUTHORIZE_BASE = "http://127.0.0.1:23119/api/local/authorize"
_SPOOF_BASE = "http://evil.example\\@127.0.0.1:23119/api"
_COLLECTIONS_PATH = "/api/users/0/collections"


class _FakeZotero(urllib.request.BaseHandler):
    """A fake local API under the setup opener. It records every request by its full URL.

    ``redirects`` maps a request path to a Location: that path gets a 302 to it. Any other request
    gets a 200 with an empty JSON list. No socket is opened.
    """

    handler_order = 100

    def __init__(self, redirects=None):
        self.redirects = dict(redirects or {})
        self.seen = []

    def http_open(self, req):
        self.seen.append(req.full_url)
        location = self.redirects.get(urllib.parse.urlsplit(req.full_url).path)
        headers = email.message.Message()
        if location is None:
            code, body = 200, b"[]"
            headers["Content-Type"] = "application/json"
        else:
            code, body = 302, b""
            headers["Location"] = location
        response = urllib.response.addinfourl(io.BytesIO(body), headers, req.full_url, code)
        response.msg = "OK" if location is None else "Found"
        return response


@pytest.mark.parametrize(
    "base",
    [
        _AUTHORIZE_BASE,
        "http://127.0.0.1:23119/api/local%2Fauthorize",
        "http://127.0.0.1:23119/api/local;x/authorize",
        _SPOOF_BASE,
        "http://evil.example@127.0.0.1:23119/api",
    ],
)
def test_list_collections_refuses_an_authorize_or_ambiguous_base_before_any_request(monkeypatch, base):
    calls = []

    def open_request(request, timeout):
        calls.append(request.full_url)
        raise AssertionError("no request expected")

    monkeypatch.setattr(zotero_setup, "_OPENER", SimpleNamespace(open=open_request))
    with pytest.raises(zotero_setup.ZoteroSetupError):
        zotero_setup.list_local_zotero_collections(base_url=base)
    assert calls == []


def test_list_collections_refuses_an_authorize_base_read_from_the_environment(monkeypatch):
    calls = []

    def open_request(request, timeout):
        calls.append(request.full_url)
        raise AssertionError("no request expected")

    monkeypatch.setenv("MERIDIAN_ZOTERO_API_URL", _AUTHORIZE_BASE)
    monkeypatch.setattr(zotero_setup, "_OPENER", SimpleNamespace(open=open_request))
    with pytest.raises(zotero_setup.ZoteroSetupError):
        zotero_setup.list_local_zotero_collections()
    assert calls == []


@pytest.mark.parametrize(
    "target",
    [
        _AUTHORIZE_BASE,
        "http://127.0.0.1:23119/api/local\\authorize",
        "http://evil.example\\@127.0.0.1:23119/api/users/0/collections",
    ],
)
def test_list_collections_does_not_follow_a_redirect_to_the_authorize_endpoint_or_an_ambiguous_authority(
    monkeypatch, target,
):
    zotero = _FakeZotero({_COLLECTIONS_PATH: target})
    monkeypatch.setattr(zotero_setup, "_OPENER", zotero_setup._build_opener(zotero))
    with pytest.raises(zotero_setup.ZoteroSetupError):
        zotero_setup.list_local_zotero_collections(base_url="http://127.0.0.1:23119/api")
    assert [urllib.parse.urlsplit(url).path for url in zotero.seen] == [_COLLECTIONS_PATH]
    assert all("evil.example" not in url for url in zotero.seen)


def test_list_collections_still_follows_an_ordinary_loopback_redirect(monkeypatch):
    zotero = _FakeZotero({_COLLECTIONS_PATH: "http://127.0.0.1:23119/api/elsewhere"})
    monkeypatch.setattr(zotero_setup, "_OPENER", zotero_setup._build_opener(zotero))
    assert zotero_setup.list_local_zotero_collections(base_url="http://127.0.0.1:23119/api") == []
    assert len(zotero.seen) == 2


def test_preferences_store_only_non_secret_scope_locally(monkeypatch, tmp_path):
    path = tmp_path / "zotero.json"
    monkeypatch.setattr(tunnel_config, "_zotero_preferences_path", lambda: path)

    tunnel_config.set_zotero_preferences(
        library_id="123456",
        collection_keys=["ab123456", "AB123456", "cd123456"],
    )

    assert tunnel_config.get_zotero_library_id() == "123456"
    assert tunnel_config.get_zotero_collection_keys() == ["AB123456", "CD123456"]
    content = json.loads(path.read_text(encoding="utf-8"))
    assert content == {"library_id": "123456", "collection_keys": ["AB123456", "CD123456"]}
    assert "api_key" not in content and "secret" not in path.read_text(encoding="utf-8")
