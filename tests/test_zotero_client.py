"""Coverage for the cross-document Zotero citation resolver (fefb596a).

CI-SAFE: every test injects a MOCK httpx-like client — nothing here ever opens a
socket or talks to a real Zotero instance. The mock records the requests it
received (so we can assert dispatch: a DOI ref does a q= search, a zotero:<key>
ref does a direct item GET) and returns canned response objects.

The resolver's guard contract is exercised exhaustively: 403 "Local API is not
enabled", 404, a transport/connection error, a timeout, a malformed (non-JSON)
body, an empty/None ref, and a DOI whose exact match is absent (only loose text
hits) all collapse to ``None`` — resolve_citation_ref NEVER raises and NEVER
fabricates a link.
"""
from __future__ import annotations

import asyncio

import pytest

from meridian import zotero_client


# ---------------------------------------------------------------------------
# Mock httpx client — records requests, returns canned responses
# ---------------------------------------------------------------------------

class _MockResponse:
    """Stand-in for httpx.Response: carries a status code + a JSON body.

    ``raise_on_json`` simulates a malformed/empty body (``.json()`` throws).
    """

    def __init__(self, status_code=200, json_body=None, raise_on_json=False):
        self.status_code = status_code
        self._json_body = json_body
        self._raise_on_json = raise_on_json

    def json(self):
        if self._raise_on_json:
            raise ValueError("not JSON")
        return self._json_body


class _MockClient:
    """Async client stub. ``handler(url, params)`` returns a _MockResponse (or
    raises to simulate a transport error). Every request is recorded."""

    def __init__(self, handler):
        self._handler = handler
        self.requests: list[tuple[str, dict]] = []

    async def get(self, url, params=None):
        self.requests.append((url, params or {}))
        return self._handler(url, params or {})


def _item(key, *, doi=None, title=None, item_type="journalArticle"):
    """Build a raw Zotero item object as the local API would return it."""
    data: dict = {}
    if doi is not None:
        data["DOI"] = doi
    if title is not None:
        data["title"] = title
    if item_type is not None:
        data["itemType"] = item_type
    return {"key": key, "version": 1, "library": {"type": "user", "id": 0}, "data": data}


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# DOI dispatch — doi:, doi.org URL, and a bare 10.x/y string
# ---------------------------------------------------------------------------

def test_doi_prefix_resolves_case_insensitive_doi_match():
    # Library returns a couple of loose text hits; only the exact (case-insensitive)
    # DOI match is returned.
    def handler(url, params):
        assert url.endswith("/users/0/items")  # DOI dispatch is a q= search
        assert params.get("q") == "10.1000/XYZ"
        return _MockResponse(json_body=[
            _item("AAAA1111", doi="10.9999/other", title="Unrelated"),
            _item("BBBB2222", doi="10.1000/xyz", title="Target paper"),
        ])

    client = _MockClient(handler)
    result = _run(
        zotero_client.resolve_citation_ref("doi:10.1000/XYZ", client=client)
    )
    assert result == {
        "zotero_key": "BBBB2222",
        "doi": "10.1000/xyz",
        "title": "Target paper",
        "item_type": "journalArticle",
    }
    # Exactly one search request was made.
    assert len(client.requests) == 1


def test_doi_org_url_dispatches_as_doi_search():
    captured = {}

    def handler(url, params):
        captured["url"] = url
        captured["q"] = params.get("q")
        return _MockResponse(json_body=[_item("K1", doi="10.5555/aaa", title="T")])

    client = _MockClient(handler)
    result = _run(
        zotero_client.resolve_citation_ref(
            "https://doi.org/10.5555/aaa", client=client
        )
    )
    assert result["zotero_key"] == "K1"
    assert result["doi"] == "10.5555/aaa"
    # The doi.org prefix was stripped: the q= term is the bare DOI.
    assert captured["q"] == "10.5555/aaa"
    assert captured["url"].endswith("/users/0/items")


def test_bare_doi_string_dispatches_as_doi_search():
    def handler(url, params):
        return _MockResponse(json_body=[_item("Z9", doi="10.1234/plain", title="P")])

    client = _MockClient(handler)
    result = _run(
        zotero_client.resolve_citation_ref("10.1234/plain", client=client)
    )
    assert result["zotero_key"] == "Z9"
    assert result["doi"] == "10.1234/plain"
    # A bare DOI hits the search endpoint (not the direct item GET).
    assert client.requests[0][0].endswith("/users/0/items")
    assert client.requests[0][1].get("q") == "10.1234/plain"


def test_doi_absent_from_library_returns_none_no_fabrication():
    # The search returns loose text hits but NO item whose DOI matches exactly.
    # The resolver must NOT fabricate a link from a loose hit.
    def handler(url, params):
        return _MockResponse(json_body=[
            _item("L1", doi="10.0000/loose", title="Mentions 10.1000/xyz in abstract"),
            _item("L2", doi=None, title="No DOI at all"),
        ])

    client = _MockClient(handler)
    result = _run(
        zotero_client.resolve_citation_ref("doi:10.1000/xyz", client=client)
    )
    assert result is None


# ---------------------------------------------------------------------------
# zotero:<key> dispatch — direct item GET
# ---------------------------------------------------------------------------

def test_zotero_key_does_direct_item_get():
    def handler(url, params):
        # Direct item GET, keyed by the item key in the path.
        assert url.endswith("/users/0/items/ABCD1234")
        return _MockResponse(json_body=_item("ABCD1234", doi="10.7/dd", title="Direct"))

    client = _MockClient(handler)
    result = _run(
        zotero_client.resolve_citation_ref("zotero:ABCD1234", client=client)
    )
    assert result == {
        "zotero_key": "ABCD1234",
        "doi": "10.7/dd",
        "title": "Direct",
        "item_type": "journalArticle",
    }
    assert len(client.requests) == 1


def test_zotero_key_direct_get_unwraps_single_element_list():
    # Some servers wrap the single item in a list — the resolver handles both.
    def handler(url, params):
        return _MockResponse(json_body=[_item("WRAP1", title="Wrapped")])

    client = _MockClient(handler)
    result = _run(zotero_client.resolve_citation_ref("zotero:WRAP1", client=client))
    assert result is not None
    assert result["zotero_key"] == "WRAP1"
    assert result["doi"] is None  # no DOI on the item


# ---------------------------------------------------------------------------
# bare citekey dispatch — best-effort q= search top hit
# ---------------------------------------------------------------------------

def test_bare_citekey_does_q_search_top_hit():
    def handler(url, params):
        assert url.endswith("/users/0/items")
        assert params.get("q") == "knuth1984"
        # Top hit is returned even without a DOI match (fuzzy, best-effort).
        return _MockResponse(json_body=[
            _item("TOP1", doi=None, title="The TeXbook", item_type="book"),
            _item("TOP2", doi=None, title="Another"),
        ])

    client = _MockClient(handler)
    result = _run(zotero_client.resolve_citation_ref("knuth1984", client=client))
    assert result == {
        "zotero_key": "TOP1",
        "doi": None,
        "title": "The TeXbook",
        "item_type": "book",
    }


def test_bare_citekey_no_hits_returns_none():
    def handler(url, params):
        return _MockResponse(json_body=[])

    client = _MockClient(handler)
    result = _run(zotero_client.resolve_citation_ref("nonexistent_key", client=client))
    assert result is None


# ---------------------------------------------------------------------------
# item with no DOI -> doi=None (but still resolves)
# ---------------------------------------------------------------------------

def test_item_without_doi_resolves_with_none_doi():
    def handler(url, params):
        return _MockResponse(json_body=_item("NODOI", doi=None, title="No DOI item"))

    client = _MockClient(handler)
    result = _run(zotero_client.resolve_citation_ref("zotero:NODOI", client=client))
    assert result is not None
    assert result["zotero_key"] == "NODOI"
    assert result["doi"] is None
    assert result["title"] == "No DOI item"


def test_blank_doi_field_is_normalized_to_none():
    # A DOI field present but whitespace-only normalizes to None.
    def handler(url, params):
        return _MockResponse(json_body=_item("BLANK", doi="   ", title="Blank DOI"))

    client = _MockClient(handler)
    result = _run(zotero_client.resolve_citation_ref("zotero:BLANK", client=client))
    assert result is not None
    assert result["doi"] is None


# ---------------------------------------------------------------------------
# Guard contract — every failure mode collapses to None, never raises
# ---------------------------------------------------------------------------

def test_403_local_api_not_enabled_returns_none():
    def handler(url, params):
        return _MockResponse(status_code=403, json_body={"message": "Local API is not enabled"})

    client = _MockClient(handler)
    assert _run(zotero_client.resolve_citation_ref("zotero:X", client=client)) is None
    assert _run(zotero_client.resolve_citation_ref("doi:10.1/y", client=client)) is None


def test_404_returns_none():
    def handler(url, params):
        return _MockResponse(status_code=404, json_body={"message": "Not found"})

    client = _MockClient(handler)
    assert _run(zotero_client.resolve_citation_ref("zotero:MISSING", client=client)) is None


def test_transport_error_returns_none():
    def handler(url, params):
        raise ConnectionError("connection refused")

    client = _MockClient(handler)
    assert _run(zotero_client.resolve_citation_ref("zotero:X", client=client)) is None
    assert _run(zotero_client.resolve_citation_ref("doi:10.1/y", client=client)) is None
    assert _run(zotero_client.resolve_citation_ref("citekey_x", client=client)) is None


def test_timeout_returns_none():
    def handler(url, params):
        raise TimeoutError("read timed out")

    client = _MockClient(handler)
    assert _run(zotero_client.resolve_citation_ref("doi:10.1/y", client=client)) is None


def test_malformed_body_returns_none():
    def handler(url, params):
        return _MockResponse(status_code=200, raise_on_json=True)

    client = _MockClient(handler)
    assert _run(zotero_client.resolve_citation_ref("zotero:X", client=client)) is None
    assert _run(zotero_client.resolve_citation_ref("doi:10.1/y", client=client)) is None


def test_doi_search_body_not_a_list_returns_none():
    # A DOI search that returns a JSON object instead of a list -> None (defensive).
    def handler(url, params):
        return _MockResponse(json_body={"unexpected": "shape"})

    client = _MockClient(handler)
    assert _run(zotero_client.resolve_citation_ref("doi:10.1/y", client=client)) is None


def test_empty_and_none_ref_returns_none_without_a_request():
    # An empty or None ref is unclassifiable — returns None and never touches the
    # client (no request recorded).
    for bad in ("", "   ", None):
        client = _MockClient(lambda url, params: _MockResponse(json_body=[]))
        assert _run(zotero_client.resolve_citation_ref(bad, client=client)) is None
        assert client.requests == []


def test_non_string_ref_returns_none():
    client = _MockClient(lambda url, params: _MockResponse(json_body=[]))
    assert _run(zotero_client.resolve_citation_ref(1234, client=client)) is None  # type: ignore[arg-type]
    assert client.requests == []


def test_item_without_key_is_not_resolvable():
    # A search hit with no usable key cannot be resolved -> None.
    def handler(url, params):
        return _MockResponse(json_body=[{"data": {"DOI": "10.1/y", "title": "keyless"}}])

    client = _MockClient(handler)
    assert _run(zotero_client.resolve_citation_ref("doi:10.1/y", client=client)) is None


# ---------------------------------------------------------------------------
# base_url resolution — explicit arg, env override, default
# ---------------------------------------------------------------------------

def test_base_url_arg_overrides_default():
    captured = {}

    def handler(url, params):
        captured["url"] = url
        return _MockResponse(json_body=_item("K", title="t"))

    client = _MockClient(handler)
    _run(zotero_client.resolve_citation_ref(
        "zotero:K", base_url="http://localhost:9999/api", client=client,
    ))
    assert captured["url"].startswith("http://localhost:9999/api/users/0/items/K")


def test_base_url_env_override(monkeypatch):
    monkeypatch.setenv("MERIDIAN_ZOTERO_API_URL", "http://envhost:1234/api/")
    captured = {}

    def handler(url, params):
        captured["url"] = url
        return _MockResponse(json_body=_item("K", title="t"))

    client = _MockClient(handler)
    _run(zotero_client.resolve_citation_ref("zotero:K", client=client))
    # Trailing slash on the env value is stripped.
    assert captured["url"].startswith("http://envhost:1234/api/users/0/items/K")


# ---------------------------------------------------------------------------
# Pure helpers — _normalize_ref / _looks_like_doi
# ---------------------------------------------------------------------------

def test_normalize_ref_classification():
    assert zotero_client._normalize_ref("doi:10.1/abc") == ("doi", "10.1/abc")
    assert zotero_client._normalize_ref("https://doi.org/10.2/def") == ("doi", "10.2/def")
    assert zotero_client._normalize_ref("http://dx.doi.org/10.3/ghi") == ("doi", "10.3/ghi")
    assert zotero_client._normalize_ref("10.4567/bare") == ("doi", "10.4567/bare")
    assert zotero_client._normalize_ref("zotero:ABCD") == ("zotero", "ABCD")
    assert zotero_client._normalize_ref("knuth1984") == ("citekey", "knuth1984")
    assert zotero_client._normalize_ref("") is None
    assert zotero_client._normalize_ref("   ") is None
    assert zotero_client._normalize_ref(None) is None
    # A doi: prefix with an empty payload is unclassifiable.
    assert zotero_client._normalize_ref("doi:") is None
    assert zotero_client._normalize_ref("zotero:") is None


def test_looks_like_doi():
    assert zotero_client._looks_like_doi("10.1000/xyz") is True
    assert zotero_client._looks_like_doi("10.12345/a.b-c_d") is True
    assert zotero_client._looks_like_doi("knuth1984") is False
    # Registrant must be 4+ digits: "10.1/x" fails (1 digit), "10.1000/x" passes.
    assert zotero_client._looks_like_doi("10.1/x") is False
    assert zotero_client._looks_like_doi("10.1000/x") is True
    assert zotero_client._looks_like_doi("10.1234/bare") is True  # 4-digit suffix present
    assert zotero_client._looks_like_doi("10.12345") is False  # no suffix
    assert zotero_client._looks_like_doi(None) is False
    assert zotero_client._looks_like_doi(123) is False


def test_no_injected_client_creates_owned_client(monkeypatch):
    # With no client injected, the resolver imports httpx and opens a short-lived
    # client. Stub httpx.AsyncClient so no real socket is opened, and assert the
    # dispatch still works end-to-end through the owned-client path.
    import sys
    import types

    class _StubAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return _MockClient(
                lambda url, params: _MockResponse(json_body=_item("OWNED", title="t"))
            )

        async def __aexit__(self, *exc):
            return False

    stub_httpx = types.SimpleNamespace(AsyncClient=_StubAsyncClient)
    monkeypatch.setitem(sys.modules, "httpx", stub_httpx)
    result = _run(zotero_client.resolve_citation_ref("zotero:OWNED"))
    assert result is not None
    assert result["zotero_key"] == "OWNED"
