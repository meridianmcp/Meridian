"""6d1abc98 — github_search: GitHub code/repo search via the same per-source
function pattern established in test_paper_search.py / test_social_search.py.

The network calls (github_code_search / github_repo_search) are only smoke-checked
on the empty-query guard, the endpoint/sort routing, and the network-error path so
CI never depends on reaching the network; the parsing is fully unit-tested against
fixture payloads.
"""
from __future__ import annotations

from meridian.github_search import (
    github_code_search,
    github_repo_search,
    parse_github_code_items,
    parse_github_repo_items,
)

_SAMPLE_CODE_ITEMS = {
    "total_count": 2,
    "items": [
        {
            "name": "widths.py",
            "path": "src/dt/widths.py",
            "sha": "abc123",
            "html_url": "https://github.com/acme/dt-repo/blob/main/src/dt/widths.py",
            "score": 12.5,
            "repository": {
                "full_name": "acme/dt-repo",
                "owner": {"login": "acme"},
            },
        },
        {
            "name": "fallback.py",
            "path": "lib/fallback.py",
            "html_url": "https://github.com/someorg/otherrepo/blob/main/lib/fallback.py",
            "repository": {
                "full_name": "someorg/otherrepo",
                # no explicit "owner" sub-object -> derive login from full_name
            },
        },
    ],
}

_SAMPLE_REPO_ITEMS = {
    "total_count": 2,
    "items": [
        {
            "full_name": "acme/dt-repo",
            "owner": {"login": "acme"},
            "description": "  A   crack-width   estimator.  ",
            "html_url": "https://github.com/acme/dt-repo",
            "created_at": "2020-01-01T00:00:00Z",
            "updated_at": "2024-05-01T00:00:00Z",
            "stargazers_count": 42,
            "forks_count": 7,
            "language": "Python",
            "score": 9.9,
        },
        {
            "full_name": "someorg/otherrepo",
            "description": None,
            "html_url": "https://github.com/someorg/otherrepo",
            "pushed_at": "2023-02-02T00:00:00Z",
            "stargazers_count": None,
            "forks_count": None,
            "language": None,
        },
    ],
}


# ── code search parsing (offline, no network) ───────────────────────────────

def test_parse_github_code_items_extracts_fields():
    rows = parse_github_code_items(_SAMPLE_CODE_ITEMS)
    assert len(rows) == 2
    p = rows[0]
    assert p["path"] == "src/dt/widths.py"
    assert p["title"] == "src/dt/widths.py"
    assert p["repo"] == "acme/dt-repo"
    assert p["authors"] == ["acme"]
    assert p["url"] == "https://github.com/acme/dt-repo/blob/main/src/dt/widths.py"
    assert p["sha"] == "abc123"
    assert p["score"] == 12.5
    # same overall shape as arxiv/openalex/hn rows
    for key in ("title", "authors", "summary", "published", "updated", "url"):
        assert key in p


def test_parse_github_code_items_derives_owner_from_full_name_when_no_owner_obj():
    rows = parse_github_code_items(_SAMPLE_CODE_ITEMS)
    q = rows[1]
    assert q["repo"] == "someorg/otherrepo"
    assert q["authors"] == ["someorg"]
    assert q["sha"] == ""


def test_parse_github_code_items_respects_limit():
    assert len(parse_github_code_items(_SAMPLE_CODE_ITEMS, limit=1)) == 1


def test_parse_github_code_items_never_raises_on_garbage():
    assert parse_github_code_items(None) == []
    assert parse_github_code_items({}) == []
    assert parse_github_code_items({"items": None}) == []
    assert parse_github_code_items("not a dict") == []
    out = parse_github_code_items({"items": ["nope", {}, {"score": "not-a-number"}]})
    assert out == [
        {"path": "", "title": "", "repo": "", "authors": [], "summary": "",
         "published": "", "updated": "", "url": "", "sha": "", "score": 0},
        {"path": "", "title": "", "repo": "", "authors": [], "summary": "",
         "published": "", "updated": "", "url": "", "sha": "", "score": 0},
    ]


def test_parse_github_code_items_accepts_raw_list():
    raw = _SAMPLE_CODE_ITEMS["items"]
    assert len(parse_github_code_items(raw)) == 2


# ── repo search parsing (offline, no network) ───────────────────────────────

def test_parse_github_repo_items_extracts_fields():
    rows = parse_github_repo_items(_SAMPLE_REPO_ITEMS)
    assert len(rows) == 2
    p = rows[0]
    assert p["title"] == "acme/dt-repo"
    assert p["repo"] == "acme/dt-repo"
    assert p["authors"] == ["acme"]
    # description whitespace is normalized to single spaces
    assert p["summary"] == "A crack-width estimator."
    assert p["published"] == "2020-01-01T00:00:00Z"
    assert p["updated"] == "2024-05-01T00:00:00Z"
    assert p["url"] == "https://github.com/acme/dt-repo"
    assert p["stars"] == 42
    assert p["forks"] == 7
    assert p["language"] == "Python"
    assert p["score"] == 9.9
    for key in ("title", "authors", "summary", "published", "updated", "url"):
        assert key in p


def test_parse_github_repo_items_falls_back_to_pushed_at_and_defaults():
    rows = parse_github_repo_items(_SAMPLE_REPO_ITEMS)
    q = rows[1]
    # no owner object, no updated_at -> derive login from full_name, fall back to pushed_at
    assert q["authors"] == ["someorg"]
    assert q["summary"] == ""
    assert q["updated"] == "2023-02-02T00:00:00Z"
    assert q["stars"] == 0
    assert q["forks"] == 0
    assert q["language"] == ""


def test_parse_github_repo_items_respects_limit():
    assert len(parse_github_repo_items(_SAMPLE_REPO_ITEMS, limit=1)) == 1


def test_parse_github_repo_items_never_raises_on_garbage():
    assert parse_github_repo_items(None) == []
    assert parse_github_repo_items({}) == []
    assert parse_github_repo_items({"items": None}) == []
    assert parse_github_repo_items("not a dict") == []
    out = parse_github_repo_items({"items": ["nope", {}]})
    assert out == [
        {"title": "", "repo": "", "authors": [], "summary": "", "published": "",
         "updated": "", "url": "", "stars": 0, "forks": 0, "language": "", "score": 0},
    ]


def test_parse_github_repo_items_accepts_raw_list():
    raw = _SAMPLE_REPO_ITEMS["items"]
    assert len(parse_github_repo_items(raw)) == 2


# ── network smoke tests (empty-query guard; no real network in CI) ─────────

async def test_github_code_search_empty_query_returns_error():
    out = await github_code_search("   ")
    assert out.get("error")
    assert "results" not in out


async def test_github_repo_search_empty_query_returns_error():
    out = await github_repo_search("   ")
    assert out.get("error")
    assert "results" not in out


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, headers=None):
        _FakeClient.seen = {"url": url, "params": params, "headers": headers}
        return _FakeResp(_FakeClient.payload)


async def test_github_code_search_hits_code_endpoint_and_parses(monkeypatch):
    """github_code_search must hit the code-search endpoint by default and parse
    the JSON body into results. Mocks httpx so CI never touches the network."""
    import httpx
    import meridian.github_search as gs

    _FakeClient.payload = _SAMPLE_CODE_ITEMS
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    out = await gs.github_code_search("crack width", limit=2)
    assert out["count"] == 2
    assert out["results"][0]["path"] == "src/dt/widths.py"
    assert _FakeClient.seen["url"] == gs._GITHUB_CODE_API
    assert _FakeClient.seen["params"]["q"] == "crack width"
    assert "sort" not in _FakeClient.seen["params"]
    assert _FakeClient.seen["headers"]["User-Agent"]


async def test_github_code_search_date_sort_adds_indexed_sort_param(monkeypatch):
    import httpx
    import meridian.github_search as gs

    _FakeClient.payload = {"items": []}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    out = await gs.github_code_search("rust", sort_by="date")
    assert out["count"] == 0
    assert _FakeClient.seen["params"]["sort"] == "indexed"
    assert _FakeClient.seen["params"]["order"] == "desc"


async def test_github_repo_search_hits_repo_endpoint_and_parses(monkeypatch):
    import httpx
    import meridian.github_search as gs

    _FakeClient.payload = _SAMPLE_REPO_ITEMS
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    out = await gs.github_repo_search("crack width estimator", limit=2)
    assert out["count"] == 2
    assert out["results"][0]["repo"] == "acme/dt-repo"
    assert _FakeClient.seen["url"] == gs._GITHUB_REPO_API
    assert _FakeClient.seen["params"]["q"] == "crack width estimator"
    assert "sort" not in _FakeClient.seen["params"]


async def test_github_repo_search_date_sort_adds_updated_sort_param(monkeypatch):
    import httpx
    import meridian.github_search as gs

    _FakeClient.payload = {"items": []}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    out = await gs.github_repo_search("rust", sort_by="date")
    assert out["count"] == 0
    assert _FakeClient.seen["params"]["sort"] == "updated"
    assert _FakeClient.seen["params"]["order"] == "desc"


async def test_github_code_search_network_error_degrades_to_error_dict(monkeypatch):
    import httpx
    import meridian.github_search as gs

    class _RaisingClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            raise RuntimeError("boom")

    monkeypatch.setattr(httpx, "AsyncClient", _RaisingClient)
    out = await gs.github_code_search("anything")
    assert "error" in out
    assert out["query"] == "anything"
    assert "results" not in out


async def test_github_repo_search_network_error_degrades_to_error_dict(monkeypatch):
    import httpx
    import meridian.github_search as gs

    class _RaisingClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            raise RuntimeError("boom")

    monkeypatch.setattr(httpx, "AsyncClient", _RaisingClient)
    out = await gs.github_repo_search("anything")
    assert "error" in out
    assert out["query"] == "anything"
    assert "results" not in out


# ── registration tests ───────────────────────────────────────────────────────

def test_github_search_registered_and_read_only():
    from meridian import mcp_tools

    names = {t["name"] for t in mcp_tools._MCP_TOOLS_LIST}
    assert "github_search" in names, "github_search must be advertised in tools/list"
    assert "github_search" in mcp_tools._READ_ONLY_TOOLS
    entry = next(t for t in mcp_tools._MCP_TOOLS_LIST if t["name"] == "github_search")
    props = entry["inputSchema"]["properties"]
    assert "query" in props
    assert entry["inputSchema"]["required"] == ["query"]
    assert "type" in props, "github_search must expose a 'type' param"
    assert set(props["type"]["enum"]) == {"code", "repo"}


def test_github_search_category_and_role_relevance():
    from meridian import mcp_tools

    assert mcp_tools._TOOL_CATEGORY.get("github_search") == "research"
    assert mcp_tools._TOOL_ROLE_RELEVANCE.get("github_search") == "planner"


# ── handler dispatch test ────────────────────────────────────────────────────

async def test_handle_github_search_dispatches_to_code_search_by_default(monkeypatch):
    from meridian.mcp.handlers.research_tools import handle_github_search

    async def _fake_code_search(query, limit=10, sort_by="relevance"):
        return {"query": query, "count": 0, "results": [], "_source": "code"}

    async def _fake_repo_search(query, limit=10, sort_by="relevance"):
        return {"query": query, "count": 0, "results": [], "_source": "repo"}

    import meridian.github_search as gs
    monkeypatch.setattr(gs, "github_code_search", _fake_code_search)
    monkeypatch.setattr(gs, "github_repo_search", _fake_repo_search)

    out = await handle_github_search(
        {"query": "async rust"}, db=None, data_dir="", tenant=None, _mcp_tenant_id=None,
    )
    assert out["_source"] == "code"


async def test_handle_github_search_dispatches_to_repo_search_when_type_repo(monkeypatch):
    from meridian.mcp.handlers.research_tools import handle_github_search

    async def _fake_code_search(query, limit=10, sort_by="relevance"):
        return {"query": query, "count": 0, "results": [], "_source": "code"}

    async def _fake_repo_search(query, limit=10, sort_by="relevance"):
        return {"query": query, "count": 0, "results": [], "_source": "repo"}

    import meridian.github_search as gs
    monkeypatch.setattr(gs, "github_code_search", _fake_code_search)
    monkeypatch.setattr(gs, "github_repo_search", _fake_repo_search)

    out = await handle_github_search(
        {"query": "async rust", "type": "repo"}, db=None, data_dir="", tenant=None,
        _mcp_tenant_id=None,
    )
    assert out["_source"] == "repo"


async def test_handler_dispatch_table_wires_github_search(monkeypatch):
    """meridian/mcp/handler.py's _standard_dispatch must route 'github_search' to
    handle_github_search from research_tools.py."""
    import meridian.server  # noqa: F401 — must be imported before handler to avoid cycle
    from meridian.mcp import handler as handler_module
    from meridian.mcp.handlers.research_tools import handle_github_search

    # _handle_session_tools builds its dispatch table inline; inspect its source
    # rather than invoking a real tool (keeps this test offline).
    import inspect
    src = inspect.getsource(handler_module._handle_session_tools)
    assert '"github_search": handle_github_search' in src
    assert handle_github_search is not None
