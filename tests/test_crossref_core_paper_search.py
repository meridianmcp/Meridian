"""9dc630de — Crossref + CORE paper_search sources, and the paper_search dispatch fix.

Before this item, ``handle_paper_search`` routed any source other than 'openalex' to
arXiv, so 'semantic_scholar'/'pubmed' (which already existed in paper_search.py)
silently returned arXiv results. The consistency tests at the bottom pin the tool's
inputSchema enum, the handler's dispatch table, and the watchlist's source tables
to each other so that class of drift fails a test instead of shipping.

All network calls are mocked; CI never reaches Crossref or CORE.
"""
from __future__ import annotations

import httpx
import pytest

import meridian.paper_search as ps
from meridian.mcp.handlers import research_watchlist as rw
from meridian.mcp.handlers import session_tools as st_mod
from meridian.mcp_tools import _MCP_TOOLS_LIST

# Shaped like a live Crossref /works response (probed 2026-09-26): titles carry
# inline JATS markup, abstracts carry block markup plus their own "Abstract" title,
# and date-parts can be partial or null-padded.
_CROSSREF_PAYLOAD = {
    "status": "ok",
    "message": {
        "total-results": 2,
        "items": [
            {
                "DOI": "10.1371/journal.pcbi.1000001",
                "title": ["<scp>DNA</scp> language models detect H<sub>2</sub>O errors"],
                "author": [
                    {"given": "Ada", "family": "Lovelace", "sequence": "first"},
                    {"name": "The Consortium"},
                    {"given": "", "family": ""},
                ],
                "container-title": ["PLOS Computational Biology"],
                "ISSN": ["1553-7358", " ", "1553-734X"],
                "publisher": "Public Library of Science (PLoS)",
                "type": "journal-article",
                "published": {"date-parts": [[2025, 3, 7]]},
                "deposited": {"date-parts": [[2025, 4, 1]]},
                "URL": "https://doi.org/10.1371/journal.pcbi.1000001",
                "link": [
                    {"URL": "https://example.org/full.xml", "content-type": "application/xml"},
                    {"URL": "https://example.org/paper.pdf", "content-type": "application/pdf"},
                ],
                "is-referenced-by-count": 12,
                "abstract": (
                    "<jats:title>Abstract</jats:title><jats:p>First paragraph.</jats:p>"
                    "<jats:p>Second paragraph.</jats:p>"
                ),
            },
            {
                "DOI": "10.1000/partial",
                "title": ["Partial date record"],
                "issued": {"date-parts": [[2021, None]]},
                "is-referenced-by-count": True,
            },
        ],
    },
}

_CORE_PAYLOAD = {
    "totalHits": 2,
    "limit": 2,
    "offset": 0,
    "results": [
        {
            "id": 98765,
            "title": "Open  access   benchmarking",
            "authors": [{"name": "Grace  Hopper"}, {"name": ""}, "not-a-dict"],
            "abstract": "An abstract\nacross lines.",
            "publishedDate": "2024-06-01T00:00:00",
            "updatedDate": "2024-07-02T10:00:00",
            "doi": "10.5555/core.1",
            "downloadUrl": "https://core.ac.uk/download/98765.pdf",
            "links": [
                {"type": "download", "url": "https://core.ac.uk/download/98765.pdf"},
                {"type": "display", "url": "https://core.ac.uk/works/98765"},
            ],
            "journals": [{"title": "Journal of Open Research", "identifiers": ["issn:1234-5678"]}],
            "publisher": "Open Press",
            "fullText": "The entire paper text, which must never be copied into results.",
        },
        {"id": 11, "title": "Year-only record", "yearPublished": 2019, "fullText": "   "},
    ],
}


def _response(url: str, status: int = 200, payload: object | None = None) -> httpx.Response:
    return httpx.Response(status, json=payload if payload is not None else {}, request=httpx.Request("GET", url))


def _client_factory(responses: list[httpx.Response], seen: list[dict]) -> type:
    """A fake httpx.AsyncClient that replays ``responses`` in order and records calls."""

    class _Client:
        def __init__(self, **kwargs):
            seen.append({"client_kwargs": kwargs})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, headers=None):
            seen.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
            return responses.pop(0)

    return _Client


def _calls(seen: list[dict]) -> list[dict]:
    return [entry for entry in seen if "url" in entry]


@pytest.fixture
def no_sleep(monkeypatch):
    delays: list[float] = []

    async def _fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(ps.asyncio, "sleep", _fake_sleep)
    return delays


# ---------------------------------------------------------------------------
# Crossref parsing
# ---------------------------------------------------------------------------

def test_parse_crossref_works_extracts_venue_metadata():
    rows = ps.parse_crossref_works(_CROSSREF_PAYLOAD)
    assert len(rows) == 2
    first = rows[0]
    assert first["doi"] == "10.1371/journal.pcbi.1000001"
    assert first["title"] == "DNA language models detect H2O errors"
    assert first["authors"] == ["Ada Lovelace", "The Consortium"]
    assert first["venue"] == "PLOS Computational Biology"
    assert first["issn"] == ["1553-7358", "1553-734X"]
    assert first["publisher"] == "Public Library of Science (PLoS)"
    assert first["type"] == "journal-article"
    assert first["citation_count"] == 12
    assert first["published"] == "2025-03-07"
    assert first["updated"] == "2025-04-01"
    assert first["pdf_url"] == "https://example.org/paper.pdf"
    assert first["url"] == "https://doi.org/10.1371/journal.pcbi.1000001"
    assert first["summary"] == "First paragraph. Second paragraph."


def test_parse_crossref_works_partial_dates_bools_and_missing_url():
    row = ps.parse_crossref_works(_CROSSREF_PAYLOAD)[1]
    assert row["published"] == "2021"  # null month stops at the last known part
    assert row["citation_count"] == 0  # a bool is not a citation count
    assert row["url"] == "https://doi.org/10.1000/partial"
    assert row["pdf_url"] == "" and row["venue"] == "" and row["issn"] == []


@pytest.mark.parametrize("payload", [None, "garbage", {}, {"message": "x"}, {"message": {"items": "x"}}, [1, "a"]])
def test_parse_crossref_works_malformed_degrades_to_empty(payload):
    assert ps.parse_crossref_works(payload) == []


def test_parse_crossref_works_accepts_bare_list_and_respects_limit():
    items = _CROSSREF_PAYLOAD["message"]["items"]
    assert len(ps.parse_crossref_works(items, limit=1)) == 1


# ---------------------------------------------------------------------------
# Crossref search
# ---------------------------------------------------------------------------

async def test_crossref_search_empty_query_makes_no_request(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory([], seen))
    assert (await ps.crossref_search("   "))["error"] == "query is required"
    assert seen == []


async def test_crossref_search_sends_polite_pool_params_and_parses(monkeypatch):
    seen: list[dict] = []
    responses = [_response(ps._CROSSREF_API, payload=_CROSSREF_PAYLOAD)]
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(responses, seen))
    out = await ps.crossref_search("dna error detection", limit=5)
    assert out["count"] == 2 and out["results"][0]["venue"] == "PLOS Computational Biology"
    call = _calls(seen)[0]
    assert call["url"] == ps._CROSSREF_API
    assert call["params"] == {"query": "dna error detection", "rows": "5", "mailto": ps._CONTACT_EMAIL}
    assert ps._CONTACT_EMAIL in call["headers"]["User-Agent"]


async def test_crossref_search_date_sort(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(
        httpx, "AsyncClient",
        _client_factory([_response(ps._CROSSREF_API, payload=_CROSSREF_PAYLOAD)], seen),
    )
    await ps.crossref_search("x", sort_by="date")
    params = _calls(seen)[0]["params"]
    assert params["sort"] == "published" and params["order"] == "desc"


async def test_crossref_search_retries_429_then_succeeds(monkeypatch, no_sleep):
    seen: list[dict] = []
    responses = [
        _response(ps._CROSSREF_API, status=429),
        _response(ps._CROSSREF_API, status=503),
        _response(ps._CROSSREF_API, payload=_CROSSREF_PAYLOAD),
    ]
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(responses, seen))
    out = await ps.crossref_search("x")
    assert out["count"] == 2
    assert len(_calls(seen)) == 3
    assert no_sleep == list(ps._RETRY_DELAYS)


async def test_crossref_search_gives_up_after_three_429s(monkeypatch, no_sleep):
    seen: list[dict] = []
    responses = [_response(ps._CROSSREF_API, status=429) for _ in range(3)]
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(responses, seen))
    out = await ps.crossref_search("x")
    assert out["error"].startswith("crossref search failed")
    assert len(_calls(seen)) == 3


async def test_crossref_search_does_not_retry_other_http_errors(monkeypatch, no_sleep):
    seen: list[dict] = []
    responses = [_response(ps._CROSSREF_API, status=400), _response(ps._CROSSREF_API, payload=_CROSSREF_PAYLOAD)]
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(responses, seen))
    out = await ps.crossref_search("x")
    assert "error" in out
    assert len(_calls(seen)) == 1 and no_sleep == []


# ---------------------------------------------------------------------------
# CORE parsing
# ---------------------------------------------------------------------------

def test_parse_core_works_normalizes_and_never_copies_full_text():
    rows = ps.parse_core_works(_CORE_PAYLOAD)
    assert len(rows) == 2
    first = rows[0]
    assert first == {
        "core_id": "98765",
        "title": "Open access benchmarking",
        "authors": ["Grace Hopper"],
        "summary": "An abstract across lines.",
        "published": "2024-06-01",
        "updated": "2024-07-02",
        "url": "https://core.ac.uk/works/98765",
        "pdf_url": "https://core.ac.uk/download/98765.pdf",
        "doi": "10.5555/core.1",
        "venue": "Journal of Open Research",
        "publisher": "Open Press",
        "has_full_text": True,
    }
    second = rows[1]
    assert second["published"] == "2019"
    assert second["has_full_text"] is False  # whitespace-only full text doesn't count
    assert second["url"] == "https://core.ac.uk/works/11"
    assert all("fullText" not in row and "full_text" not in row for row in rows)


@pytest.mark.parametrize("payload", [None, 3, {}, {"results": "x"}, ["x", 1]])
def test_parse_core_works_malformed_degrades_to_empty(payload):
    assert ps.parse_core_works(payload) == []


# ---------------------------------------------------------------------------
# CORE search
# ---------------------------------------------------------------------------

async def test_core_search_without_key_fails_closed_without_a_request(monkeypatch):
    monkeypatch.delenv("CORE_API_KEY", raising=False)
    seen: list[dict] = []
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory([], seen))
    out = await ps.core_search("open access")
    assert "CORE_API_KEY" in out["error"]
    assert out["query"] == "open access"
    assert seen == []  # never constructs a client, never falls back elsewhere


async def test_core_search_blank_env_key_counts_as_missing(monkeypatch):
    monkeypatch.setenv("CORE_API_KEY", "   ")
    seen: list[dict] = []
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory([], seen))
    assert "CORE_API_KEY" in (await ps.core_search("x"))["error"]
    assert seen == []


async def test_core_search_sends_key_only_as_bearer_header(monkeypatch):
    monkeypatch.setenv("CORE_API_KEY", "sekret-core-key")
    seen: list[dict] = []
    responses = [_response(ps._CORE_API, payload=_CORE_PAYLOAD)]
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(responses, seen))
    out = await ps.core_search("open access", limit=2)
    assert out["count"] == 2 and out["results"][0]["core_id"] == "98765"
    call = _calls(seen)[0]
    assert call["url"] == ps._CORE_API
    assert call["headers"]["Authorization"] == "Bearer sekret-core-key"
    assert call["params"] == {"q": "open access", "limit": "2"}
    assert "sekret-core-key" not in repr(call["params"])


async def test_core_search_explicit_api_key_overrides_env(monkeypatch):
    monkeypatch.delenv("CORE_API_KEY", raising=False)
    seen: list[dict] = []
    monkeypatch.setattr(
        httpx, "AsyncClient",
        _client_factory([_response(ps._CORE_API, payload=_CORE_PAYLOAD)], seen),
    )
    await ps.core_search("x", api_key="passed-in")
    assert _calls(seen)[0]["headers"]["Authorization"] == "Bearer passed-in"


@pytest.mark.parametrize("status", [401, 403])
async def test_core_search_rejected_key_gives_actionable_error(monkeypatch, status):
    monkeypatch.setenv("CORE_API_KEY", "bad-key")
    seen: list[dict] = []
    monkeypatch.setattr(httpx, "AsyncClient", _client_factory([_response(ps._CORE_API, status=status)], seen))
    out = await ps.core_search("x")
    assert out["error"] == f"CORE rejected the API key (HTTP {status}) -- check CORE_API_KEY"


async def test_core_search_redacts_key_from_error_text(monkeypatch):
    monkeypatch.setenv("CORE_API_KEY", "leaky-key-123")

    class _Boom:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, headers=None):
            raise RuntimeError(f"transport blew up with {headers['Authorization']}")

    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    out = await ps.core_search("x")
    assert out["error"].startswith("core search failed")
    assert "leaky-key-123" not in out["error"]
    assert "***" in out["error"]


async def test_core_search_empty_query(monkeypatch):
    monkeypatch.setenv("CORE_API_KEY", "k")
    assert (await ps.core_search(""))["error"] == "query is required"


# ---------------------------------------------------------------------------
# handle_paper_search dispatch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source", list(st_mod._PAPER_SEARCH_SOURCES))
async def test_handle_paper_search_routes_every_source(monkeypatch, source):
    called: list[tuple] = []
    for fn_name in st_mod._PAPER_SEARCH_SOURCES.values():
        async def _fake(query, limit=10, sort_by="relevance", _name=fn_name):
            called.append((_name, query, limit, sort_by))
            return {"query": query, "count": 0, "results": [], "via": _name}

        monkeypatch.setattr(ps, fn_name, _fake)

    out = await st_mod.handle_paper_search(
        {"query": "q", "source": f"  {source.upper()} ", "limit": 3, "sort_by": "date"},
        None, "", None, None,
    )
    expected = st_mod._PAPER_SEARCH_SOURCES[source]
    assert out["via"] == expected
    assert called == [(expected, "q", 3, "date")]


@pytest.mark.parametrize("args", [{"query": "q"}, {"query": "q", "source": ""}, {"query": "q", "source": None}])
async def test_handle_paper_search_defaults_to_arxiv(monkeypatch, args):
    async def _fake(query, limit=10, sort_by="relevance"):
        return {"via": "arxiv"}

    monkeypatch.setattr(ps, "arxiv_search", _fake)
    assert (await st_mod.handle_paper_search(args, None, "", None, None))["via"] == "arxiv"


async def test_handle_paper_search_unknown_source_errors_instead_of_falling_back(monkeypatch):
    async def _must_not_run(*a, **k):
        raise AssertionError("an unknown source must not fall back to any real search")

    for fn_name in st_mod._PAPER_SEARCH_SOURCES.values():
        monkeypatch.setattr(ps, fn_name, _must_not_run)
    out = await st_mod.handle_paper_search({"query": "q", "source": "google_scholar"}, None, "", None, None)
    assert "unknown paper_search source 'google_scholar'" in out["error"]
    for source in st_mod._PAPER_SEARCH_SOURCES:
        assert source in out["error"]


# ---------------------------------------------------------------------------
# Watchlist wiring
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source,fn_name", [("crossref", "crossref_search"), ("core", "core_search")])
def test_watchlist_resolves_new_sources_at_call_time(monkeypatch, source, fn_name):
    sentinel = object()
    monkeypatch.setattr(ps, fn_name, sentinel)
    assert rw._resolve_search_fn(source) is sentinel


def test_watchlist_identity_keys_for_new_sources():
    assert rw._identity_key("crossref", {"doi": "10.1/x", "url": "u"}) == "doi:10.1/x"
    assert rw._identity_key("core", {"core_id": "42"}) == "core_id:42"
    assert rw._identity_key("core", {"core_id": "", "url": "https://core.ac.uk/works/7"}) == "url:https://core.ac.uk/works/7"


# ---------------------------------------------------------------------------
# Structural consistency: schema enum <-> dispatch table <-> watchlist tables
# ---------------------------------------------------------------------------

def _tool(name: str) -> dict:
    return next(t for t in _MCP_TOOLS_LIST if t["name"] == name)


def test_paper_search_schema_enum_matches_dispatch_table_exactly():
    enum = _tool("paper_search")["inputSchema"]["properties"]["source"]["enum"]
    assert list(enum) == list(st_mod._PAPER_SEARCH_SOURCES)


def test_every_dispatched_paper_search_function_exists():
    for fn_name in st_mod._PAPER_SEARCH_SOURCES.values():
        assert callable(getattr(ps, fn_name)), fn_name


def test_every_paper_search_source_is_watchable():
    for source in st_mod._PAPER_SEARCH_SOURCES:
        assert source in rw._SOURCE_IDENTITY_FIELD, source
        assert rw._resolve_search_fn(source) is getattr(ps, st_mod._PAPER_SEARCH_SOURCES[source])


@pytest.mark.parametrize("tool_name", ["save_watchlist_query", "list_watchlist_queries"])
def test_watchlist_schema_enums_match_identity_table(tool_name):
    enum = _tool(tool_name)["inputSchema"]["properties"]["source_type"]["enum"]
    assert sorted(enum) == sorted(rw._SOURCE_IDENTITY_FIELD)
