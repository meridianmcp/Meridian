"""d58000c6 — social_search: Hacker News (Algolia HN Search API) via the same
per-source function pattern established in test_paper_search.py.

The network call (hn_search) is only smoke-checked on the empty-query guard so CI
never depends on reaching the network; the parsing is fully unit-tested against
fixture payloads, and the network path is exercised with a mocked httpx client.
"""
from __future__ import annotations

from meridian.social_search import hn_search, parse_hn_hits

_SAMPLE_HITS = {
    "hits": [
        {
            "objectID": "38912345",
            "created_at": "2024-01-02T10:00:00.000Z",
            "title": "Show HN:  A new   static site generator",
            "url": "https://example.com/article",
            "author": "someuser",
            "points": 123,
            "num_comments": 45,
            "story_text": "<p>We built a <b>fast</b> static site generator.</p>",
            "comment_text": None,
            "_tags": ["story", "author_someuser", "story_38912345"],
        },
        {
            "objectID": "38912999",
            "created_at": "2024-01-03T11:00:00.000Z",
            "title": None,
            "story_title": "Ask HN: Fallback title via story_title",
            "url": None,
            "story_url": None,
            "author": "otheruser",
            "points": None,
            "num_comments": None,
            "story_text": None,
            "comment_text": None,
        },
    ],
    "nbHits": 2,
}


def test_parse_hn_hits_extracts_fields():
    items = parse_hn_hits(_SAMPLE_HITS)
    assert len(items) == 2
    p = items[0]
    assert p["hn_id"] == "38912345"
    # title whitespace is normalized to single spaces
    assert p["title"] == "Show HN: A new static site generator"
    assert p["authors"] == ["someuser"]
    # story_text HTML is stripped and whitespace-normalized
    assert p["summary"] == "We built a fast static site generator."
    assert p["published"] == "2024-01-02T10:00:00.000Z"
    assert p["url"] == "https://example.com/article"
    assert p["discussion_url"] == "https://news.ycombinator.com/item?id=38912345"
    assert p["points"] == 123
    assert p["num_comments"] == 45


def test_parse_hn_hits_falls_back_to_story_title_and_discussion_url():
    items = parse_hn_hits(_SAMPLE_HITS)
    q = items[1]
    assert q["hn_id"] == "38912999"
    assert q["title"] == "Ask HN: Fallback title via story_title"
    assert q["authors"] == ["otheruser"]
    assert q["summary"] == ""
    # no url/story_url present -> falls back to the HN discussion permalink
    assert q["url"] == "https://news.ycombinator.com/item?id=38912999"
    assert q["points"] == 0
    assert q["num_comments"] == 0


def test_parse_hn_hits_respects_limit():
    assert len(parse_hn_hits(_SAMPLE_HITS, limit=1)) == 1


def test_parse_hn_hits_never_raises_on_garbage():
    assert parse_hn_hits(None) == []
    assert parse_hn_hits({}) == []
    assert parse_hn_hits({"hits": None}) == []
    assert parse_hn_hits("not a dict") == []
    # malformed rows are skipped or defaulted, never crash
    out = parse_hn_hits({"hits": ["nope", {}, {"points": "not-an-int"}]})
    assert out == [
        {"hn_id": "", "title": "", "authors": [], "summary": "", "published": "",
         "updated": "", "url": "", "discussion_url": "", "points": 0, "num_comments": 0},
        {"hn_id": "", "title": "", "authors": [], "summary": "", "published": "",
         "updated": "", "url": "", "discussion_url": "", "points": 0, "num_comments": 0},
    ]


def test_parse_hn_hits_accepts_raw_list():
    raw = _SAMPLE_HITS["hits"]
    assert len(parse_hn_hits(raw)) == 2


async def test_hn_search_empty_query_returns_error():
    out = await hn_search("   ")
    assert out.get("error")
    assert "results" not in out


async def test_hn_search_hits_relevance_endpoint_and_parses(monkeypatch):
    """hn_search must hit the relevance-ranked endpoint by default, filter to
    'story' tags, and parse the JSON body into results. Mocks httpx so CI never
    touches the network."""
    import httpx
    import meridian.social_search as ss

    seen = {}

    class _FakeResp:
        def raise_for_status(self):
            return None
        def json(self):
            return _SAMPLE_HITS

    class _FakeClient:
        def __init__(self, **kwargs):
            seen["kwargs"] = kwargs
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, params=None, headers=None):
            seen["url"] = url
            seen["params"] = params
            return _FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    out = await ss.hn_search("static site generators", limit=2)
    assert out["count"] == 2
    assert out["results"][0]["hn_id"] == "38912345"
    assert seen["url"] == ss._HN_API
    assert seen["params"]["tags"] == "story"
    assert seen["params"]["query"] == "static site generators"


async def test_hn_search_date_sort_hits_search_by_date_endpoint(monkeypatch):
    import httpx
    import meridian.social_search as ss

    seen = {}

    class _FakeResp:
        def raise_for_status(self):
            return None
        def json(self):
            return {"hits": []}

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
    out = await ss.hn_search("rust", sort_by="date")
    assert out["count"] == 0
    assert seen["url"] == ss._HN_API_BY_DATE


async def test_hn_search_network_error_degrades_to_error_dict(monkeypatch):
    import httpx
    import meridian.social_search as ss

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
    out = await ss.hn_search("anything")
    assert "error" in out
    assert out["query"] == "anything"
    assert "results" not in out


def test_social_search_registered_and_read_only():
    from meridian import mcp_tools

    names = {t["name"] for t in mcp_tools._MCP_TOOLS_LIST}
    assert "social_search" in names, "social_search must be advertised in tools/list"
    assert "social_search" in mcp_tools._READ_ONLY_TOOLS
    entry = next(t for t in mcp_tools._MCP_TOOLS_LIST if t["name"] == "social_search")
    props = entry["inputSchema"]["properties"]
    assert "query" in props
    assert entry["inputSchema"]["required"] == ["query"]
    assert "source" in props, "social_search must expose a 'source' param"
    assert set(props["source"]["enum"]) == {"hn"}


async def test_handle_social_search_dispatches_to_hn_search(monkeypatch):
    from meridian.mcp.handlers.session_tools import handle_social_search

    async def _fake_hn_search(query, limit=10, sort_by="relevance"):
        return {"query": query, "count": 0, "results": []}

    import meridian.social_search as ss
    monkeypatch.setattr(ss, "hn_search", _fake_hn_search)

    out = await handle_social_search(
        {"query": "async rust"}, db=None, data_dir="", tenant=None, _mcp_tenant_id=None,
    )
    assert out == {"query": "async rust", "count": 0, "results": []}
