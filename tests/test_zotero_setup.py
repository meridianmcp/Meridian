"""Tests for the tray's local-only Zotero connection setup."""
from __future__ import annotations

import json
import urllib.error
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

    monkeypatch.setattr(zotero_setup.urllib.request, "urlopen", open_request)

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

    monkeypatch.setattr(zotero_setup.urllib.request, "urlopen", denied)

    with pytest.raises(zotero_setup.ZoteroSetupError, match="Allow other applications"):
        zotero_setup.list_local_zotero_collections()


def test_list_collections_explains_when_zotero_is_offline(monkeypatch):
    def offline(_request, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(zotero_setup.urllib.request, "urlopen", offline)

    with pytest.raises(zotero_setup.ZoteroSetupError, match="Start Zotero"):
        zotero_setup.list_local_zotero_collections()


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
