"""7273d2fa — zotero_search: Zotero Web API v3 via the same per-source function
pattern established in test_paper_search.py / test_social_search.py.

The network call (zotero_search) is only smoke-checked on the missing-query/
missing-library_id guards so CI never depends on reaching the network; the
parsing is fully unit-tested against fixture payloads, and the network path
is exercised with a mocked httpx client.
"""
from __future__ import annotations

from meridian.zotero_search import parse_zotero_items, zotero_search

_SAMPLE_ITEMS = [
    {
        "key": "ABCD1234",
        "links": {"alternate": {"href": "https://www.zotero.org/users/123/items/ABCD1234"}},
        "data": {
            "key": "ABCD1234",
            "itemType": "journalArticle",
            "title": "  A   Paper   About   Something  ",
            "creators": [
                {"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"},
                {"creatorType": "author", "name": "Some Org"},
            ],
            "abstractNote": "An abstract.",
            "date": "2024-01-02",
            "dateModified": "2024-01-03T00:00:00Z",
            "url": "https://example.com/paper",
            "tags": [{"tag": "computing"}, {"tag": "history"}],
        },
    },
    {
        "key": "EFGH5678",
        "links": {},
        "data": {
            "key": "EFGH5678",
            "itemType": "book",
            "title": "A Book",
            "creators": [],
            "abstractNote": "",
            "date": "",
            "dateModified": "",
            "url": "",
            "tags": [],
        },
    },
    {
        "key": "IJKL0000",
        "data": {"key": "IJKL0000", "itemType": "attachment", "title": "skip-me.pdf"},
    },
]


def test_parse_zotero_items_extracts_fields():
    items = parse_zotero_items(_SAMPLE_ITEMS, "user", "123")
    # the attachment row is filtered out
    assert len(items) == 2
    p = items[0]
    assert p["zotero_key"] == "ABCD1234"
    assert p["title"] == "A Paper About Something"  # whitespace normalized
    assert p["authors"] == ["Ada Lovelace", "Some Org"]
    assert p["summary"] == "An abstract."
    assert p["published"] == "2024-01-02"
    assert p["updated"] == "2024-01-03T00:00:00Z"
    assert p["url"] == "https://example.com/paper"
    assert p["item_type"] == "journalArticle"
    assert p["tags"] == ["computing", "history"]


def test_parse_zotero_items_falls_back_to_web_permalink_when_no_url_or_alternate():
    items = parse_zotero_items(_SAMPLE_ITEMS, "user", "123")
    q = items[1]
    assert q["zotero_key"] == "EFGH5678"
    assert q["authors"] == []
    assert q["url"] == "https://www.zotero.org/users/123/items/EFGH5678"


def test_parse_zotero_items_filters_attachments_and_notes():
    out = parse_zotero_items(
        [{"key": "x", "data": {"key": "x", "itemType": "note", "title": "a note"}}],
        "user", "123",
    )
    assert out == []


def test_parse_zotero_items_group_library_permalink():
    items = parse_zotero_items(
        [{"key": "Z1", "data": {"key": "Z1", "itemType": "document", "title": "T"}}],
        "group", "999",
    )
    assert items[0]["url"] == "https://www.zotero.org/groups/999/items/Z1"


def test_parse_zotero_items_respects_limit():
    assert len(parse_zotero_items(_SAMPLE_ITEMS, "user", "123", limit=1)) == 1


def test_parse_zotero_items_never_raises_on_garbage():
    assert parse_zotero_items(None, "user", "1") == []
    assert parse_zotero_items({}, "user", "1") == []
    assert parse_zotero_items("not a list", "user", "1") == []
    assert parse_zotero_items(["nope", {}, {"data": "also not a dict"}], "user", "1") == []


async def test_zotero_search_empty_query_returns_error():
    out = await zotero_search("   ", library_id="123")
    assert out.get("error")
    assert "results" not in out


async def test_zotero_search_missing_library_id_returns_error():
    out = await zotero_search("something")
    assert out.get("error")
    assert "results" not in out


async def test_zotero_search_hits_user_endpoint_and_parses(monkeypatch):
    """zotero_search must hit the /users/{id}/items endpoint by default, pass
    the query through, and parse the JSON body into results. Mocks httpx so
    CI never touches the network."""
    import httpx
    import meridian.zotero_search as zs

    seen = {}

    class _FakeResp:
        def raise_for_status(self):
            return None
        def json(self):
            return _SAMPLE_ITEMS

    class _FakeClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, params=None, headers=None):
            seen["url"] = url
            seen["params"] = params
            seen["headers"] = headers
            return _FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    out = await zs.zotero_search("something", library_id="123", limit=2)
    assert out["count"] == 2
    assert out["results"][0]["zotero_key"] == "ABCD1234"
    assert seen["url"] == f"{zs._ZOTERO_API_BASE}/users/123/items"
    assert seen["params"]["q"] == "something"
    assert "Zotero-API-Key" not in seen["headers"]  # no key given, none in env


async def test_zotero_search_group_library_hits_groups_endpoint(monkeypatch):
    import httpx
    import meridian.zotero_search as zs

    seen = {}

    class _FakeResp:
        def raise_for_status(self):
            return None
        def json(self):
            return []

    class _FakeClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, params=None, headers=None):
            seen["url"] = url
            return _FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    await zs.zotero_search("x", library_type="group", library_id="999")
    assert seen["url"] == f"{zs._ZOTERO_API_BASE}/groups/999/items"


async def test_zotero_search_passes_explicit_api_key_header(monkeypatch):
    import httpx
    import meridian.zotero_search as zs

    seen = {}

    class _FakeResp:
        def raise_for_status(self):
            return None
        def json(self):
            return []

    class _FakeClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, params=None, headers=None):
            seen["headers"] = headers
            return _FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    await zs.zotero_search("x", library_id="123", api_key="secret-key")
    assert seen["headers"]["Zotero-API-Key"] == "secret-key"


async def test_zotero_search_falls_back_to_env_var_api_key(monkeypatch):
    import httpx
    import meridian.zotero_search as zs

    seen = {}

    class _FakeResp:
        def raise_for_status(self):
            return None
        def json(self):
            return []

    class _FakeClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, params=None, headers=None):
            seen["headers"] = headers
            return _FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    monkeypatch.setenv("ZOTERO_API_KEY", "env-key")
    await zs.zotero_search("x", library_id="123")
    assert seen["headers"]["Zotero-API-Key"] == "env-key"


async def test_zotero_search_date_sort_passes_sort_params(monkeypatch):
    import httpx
    import meridian.zotero_search as zs

    seen = {}

    class _FakeResp:
        def raise_for_status(self):
            return None
        def json(self):
            return []

    class _FakeClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, params=None, headers=None):
            seen["params"] = params
            return _FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    await zs.zotero_search("x", library_id="123", sort_by="date")
    assert seen["params"]["sort"] == "date"
    assert seen["params"]["direction"] == "desc"


async def test_zotero_search_403_degrades_to_helpful_error(monkeypatch):
    import httpx
    import meridian.zotero_search as zs

    class _FakeResp:
        status_code = 403
        def raise_for_status(self):
            raise httpx.HTTPStatusError("forbidden", request=None, response=self)

    class _FakeClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, params=None, headers=None):
            return _FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    out = await zs.zotero_search("x", library_id="123")
    assert "error" in out
    assert "403" in out["error"]
    assert "api_key" in out["error"]


async def test_zotero_search_network_error_degrades_to_error_dict(monkeypatch):
    import httpx
    import meridian.zotero_search as zs

    class _FakeClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, params=None, headers=None):
            raise RuntimeError("boom")

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    out = await zs.zotero_search("anything", library_id="123")
    assert "error" in out
    assert out["query"] == "anything"
    assert "results" not in out


def test_zotero_search_registered_and_read_only():
    from meridian import mcp_tools

    names = {t["name"] for t in mcp_tools._MCP_TOOLS_LIST}
    assert "zotero_search" in names, "zotero_search must be advertised in tools/list"
    assert "zotero_search" in mcp_tools._READ_ONLY_TOOLS
    assert "zotero_search" in mcp_tools._OPEN_WORLD_TOOLS
    entry = next(t for t in mcp_tools._MCP_TOOLS_LIST if t["name"] == "zotero_search")
    props = entry["inputSchema"]["properties"]
    assert "query" in props
    assert "library_id" in props
    assert set(entry["inputSchema"]["required"]) == {"query", "library_id"}


async def test_handle_zotero_search_dispatches_to_zotero_search(monkeypatch):
    from meridian.mcp.handlers.session_tools import handle_zotero_search

    async def _fake_zotero_search(query, library_type="user", library_id="", api_key=None, limit=10, sort_by="relevance"):
        return {"query": query, "count": 0, "results": [], "seen_library_id": library_id}

    import meridian.zotero_search as zs
    monkeypatch.setattr(zs, "zotero_search", _fake_zotero_search)

    out = await handle_zotero_search(
        {"query": "async rust", "library_id": "123"},
        db=None, data_dir="", tenant=None, _mcp_tenant_id=None,
    )
    assert out["query"] == "async rust"
    assert out["seen_library_id"] == "123"
