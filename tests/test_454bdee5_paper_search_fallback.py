"""454bdee5 — paper_search(source='arxiv') keeps working when arXiv refuses hosted egress.

From the hosted (Fly.io) server arXiv's export API answered HTTP 406 to a request that
returns 200 from a residential machine (same URL, same User-Agent), and OpenAlex answered
429 during a heavy sweep from the shared hosted IP. These tests pin:

- arxiv_search falls back to OpenAlex (then Semantic Scholar) on 403/406, on 429/5xx that
  outlast the backoff, on transport errors and on a non-Atom 200 body, and returns rows in
  the arXiv shape with a real ``arxiv_id``, plus ``fallback_source``/``warning``/
  ``sources_tried``;
- the happy path and the backoff path never touch a fallback;
- when every source fails, the result is the usual ``{error, query}`` plus
  ``sources_tried``;
- openalex_search sends mailto (param + User-Agent), retries 429/5xx, honours
  Retry-After, and sends OPENALEX_API_KEY only as a bearer header;
- research watchlists dedup across an arXiv-answered and a fallback-answered run.

Every network call is mocked (a fake httpx.AsyncClient routed by URL); CI never reaches
arXiv, OpenAlex or Semantic Scholar.

Live evidence behind the OpenAlex fixture (curl from a residential machine, 2026-09-27):

- ``GET https://api.openalex.org/sources/S4306400194`` -> 200,
  ``display_name "arXiv (Cornell University)"``, ``type "repository"``,
  ``homepage_url "https://arxiv.org"``. So S4306400194 is arXiv.
- ``GET /works?search=mechanistic interpretability&filter=locations.source.id:S4306400194
  &per-page=5&mailto=research@usemeridian.us`` -> 200 after one anonymous 429
  (``{"error": "Rate limit exceeded", "message": "Anonymous search is temporarily
  rate-limited while the search cluster is under elevated load. Please retry in 32s, or
  use a free API key ...", "retryAfter": 32}`` with ``Retry-After: 32``).
  ``meta.x_query.oql`` echoed "works where full text has (mechanistic interpretability)
  and any location source is (S4306400194)". Headers: ``X-RateLimit-Limit: 1000``
  (daily, resets at midnight UTC), ``X-RateLimit-Credits-Used: 10`` for a search, 1 for
  a filter-only list.
- Each result is shaped like the fixture below. A preprint-only work has
  ``doi "https://doi.org/10.48550/arxiv.<id>"`` (lowercased) and a primary location
  ``{"id": "pmh:oai:arXiv.org:<id>", "landing_page_url": "http://arxiv.org/abs/<id>",
  "pdf_url": "https://arxiv.org/pdf/<id>", "source": {"id": ".../S4306400194"}}``, plus a
  second arXiv location ``{"id": "doi:10.48550/arxiv.<id>", ...}``. A work that was also
  published elsewhere (e.g. W2439568532, doi 10.1145/3233231) is ONE merged record whose
  ``doi`` and primary location are the journal's; the arXiv id only appears in its other
  ``locations`` (``pmh:oai:arXiv.org:1606.03490``, and a MAG location whose
  landing_page_url is ``https://arxiv.org/pdf/1606.03490.pdf``).
- ``filter=primary_location.source.id:S4306400194`` also works but only matches the
  preprint-only kind: ``locations.source.id:S4306400194,primary_location.source.id:
  !S4306400194,publication_year:2023`` counted 23,829 merged works it would miss.
"""
from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio

import meridian.server  # noqa: F401 — must be imported before the handlers to avoid a cycle
import meridian.paper_search as ps
from meridian import db as db_module
from meridian.mcp.handlers import research_watchlist as rw
from meridian.mcp.handlers import session_tools as st_mod

_ARXIV_ROW_KEYS = {"arxiv_id", "title", "authors", "summary", "published", "updated", "url", "pdf_url"}

_ATOM_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2404.14082v3</id>
    <updated>2024-08-23T10:00:00Z</updated>
    <published>2024-04-22T09:00:00Z</published>
    <title>Mechanistic Interpretability for AI Safety -- A Review</title>
    <summary>A review.</summary>
    <author><name>A. Author</name></author>
    <link href="http://arxiv.org/abs/2404.14082v3" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/2404.14082v3" rel="related" type="application/pdf"/>
  </entry>
</feed>"""

_EMPTY_FEED = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'

_ARXIV_SOURCE = {"id": "https://openalex.org/S4306400194", "display_name": "arXiv (Cornell University)"}

# Trimmed from the live 2026-09-27 response described in the module docstring.
_OPENALEX_ARXIV_PAYLOAD = {
    "meta": {"count": 15782, "page": 1, "per_page": 5},
    "results": [
        {   # preprint-only: arXiv DOI + arXiv primary location
            "id": "https://openalex.org/W4395065622",
            "doi": "https://doi.org/10.48550/arxiv.2404.14082",
            "title": "Mechanistic Interpretability for AI Safety -- A Review",
            "publication_date": "2024-04-22",
            "updated_date": "2026-09-26T08:19:03.415552",
            "authorships": [
                {"author": {"display_name": "A. Author"}},
                {"author": {"display_name": "B. Author"}},
            ],
            "abstract_inverted_index": {"Understanding": [0], "AI": [1], "systems.": [2]},
            "primary_location": {
                "id": "pmh:oai:arXiv.org:2404.14082",
                "landing_page_url": "http://arxiv.org/abs/2404.14082",
                "pdf_url": "https://arxiv.org/pdf/2404.14082",
                "source": _ARXIV_SOURCE,
            },
            "locations": [
                {
                    "id": "pmh:oai:arXiv.org:2404.14082",
                    "landing_page_url": "http://arxiv.org/abs/2404.14082",
                    "pdf_url": "https://arxiv.org/pdf/2404.14082",
                    "source": _ARXIV_SOURCE,
                },
                {
                    "id": "doi:10.48550/arxiv.2404.14082",
                    "landing_page_url": "https://doi.org/10.48550/arxiv.2404.14082",
                    "pdf_url": None,
                    "source": _ARXIV_SOURCE,
                },
            ],
        },
        {   # merged: journal DOI + journal primary location; arXiv only in other locations
            "id": "https://openalex.org/W2439568532",
            "doi": "https://doi.org/10.1145/3233231",
            "title": "The Mythos of Model Interpretability",
            "publication_date": "2018-06-01",
            "updated_date": "2026-09-20T00:00:00",
            "authorships": [{"author": {"display_name": "C. Author"}}],
            "primary_location": {
                "id": "doi:10.1145/3233231",
                "landing_page_url": "https://doi.org/10.1145/3233231",
                "pdf_url": None,
                "source": {"id": "https://openalex.org/S103482838"},
            },
            "locations": [
                {
                    "id": "doi:10.1145/3233231",
                    "landing_page_url": "https://doi.org/10.1145/3233231",
                    "pdf_url": None,
                    "source": {"id": "https://openalex.org/S103482838"},
                },
                {
                    "id": "mag:2439568532",
                    "landing_page_url": "https://arxiv.org/pdf/1606.03490.pdf",
                    "pdf_url": None,
                    "source": _ARXIV_SOURCE,
                },
                {
                    "id": "pmh:oai:arXiv.org:1606.03490",
                    "landing_page_url": "http://arxiv.org/abs/1606.03490",
                    "pdf_url": "https://arxiv.org/pdf/1606.03490",
                    "source": _ARXIV_SOURCE,
                },
            ],
        },
    ],
}

_S2_PAYLOAD = {
    "total": 3,
    "offset": 0,
    "data": [
        {
            "paperId": "abc123",
            "title": "Open  Problems in Mechanistic Interpretability",
            "authors": [{"name": "D. Author"}, {"name": ""}],
            "abstract": "Open\nproblems.",
            "year": 2025,
            "publicationDate": "2025-01-27",
            "externalIds": {"ArXiv": "2501.16496", "DOI": "10.48550/arXiv.2501.16496"},
        },
        {   # no arXiv id: must be dropped
            "paperId": "def456",
            "title": "A journal-only paper",
            "authors": [],
            "year": 2020,
            "externalIds": {"DOI": "10.1000/x"},
        },
        {   # year only
            "paperId": "ghi789",
            "title": "Old preprint",
            "authors": [{"name": "E. Author"}],
            "abstract": None,
            "year": 1999,
            "publicationDate": None,
            "externalIds": {"ArXiv": "hep-th/9901001"},
        },
    ],
}


# ---------------------------------------------------------------------------
# Fake httpx client routed by URL
# ---------------------------------------------------------------------------

def _resp(url: str, status: int = 200, *, text: str | None = None, payload: object | None = None,
          headers: dict[str, str] | None = None) -> httpx.Response:
    request = httpx.Request("GET", url)
    if text is not None:
        return httpx.Response(status, text=text, headers=headers, request=request)
    return httpx.Response(status, json=payload if payload is not None else {}, headers=headers, request=request)


def _arxiv(status: int = 200, *, text: str = _ATOM_FEED, headers: dict[str, str] | None = None):
    return _resp(ps._ARXIV_API, status, text=text if status == 200 else "err", headers=headers)


def _openalex(status: int = 200, payload: object = _OPENALEX_ARXIV_PAYLOAD, headers: dict[str, str] | None = None):
    return _resp(ps._OPENALEX_API, status, payload=payload if status == 200 else {"error": "x"}, headers=headers)


def _s2(status: int = 200, payload: object = _S2_PAYLOAD):
    return _resp(ps._S2_PAPER_API, status, payload=payload if status == 200 else {"message": "Too Many Requests"})


@pytest.fixture
def net(monkeypatch):
    """Install a fake ``httpx.AsyncClient``; ``net.route(url, *responses_or_exceptions)``."""

    class _Net:
        def __init__(self):
            self.routes: dict[str, list] = {}
            self.calls: list[dict] = []
            self.clients: list[dict] = []

        def route(self, url, *items):
            self.routes.setdefault(url, []).extend(items)

        def urls(self):
            return [c["url"] for c in self.calls]

        def calls_to(self, url):
            return [c for c in self.calls if c["url"] == url]

    state = _Net()

    class _Client:
        def __init__(self, **kwargs):
            state.clients.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, headers=None):
            state.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
            queue = state.routes.get(url)
            if not queue:
                raise RuntimeError(f"unexpected request to {url}")
            item = queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return state


@pytest.fixture
def no_sleep(monkeypatch):
    delays: list[float] = []

    async def _fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(ps.asyncio, "sleep", _fake_sleep)
    return delays


@pytest.fixture(autouse=True)
def _no_openalex_key(monkeypatch):
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# arXiv refuses -> OpenAlex fallback
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [406, 403])
async def test_arxiv_refusal_falls_back_to_openalex_with_arxiv_ids(net, no_sleep, status):
    net.route(ps._ARXIV_API, _arxiv(status))
    net.route(ps._OPENALEX_API, _openalex())

    out = await ps.arxiv_search("mechanistic interpretability", limit=5)

    assert "error" not in out
    assert out["fallback_source"] == "openalex"
    assert out["openalex_query_stage"] == "loose"
    assert out["sources_tried"] == ["arxiv", "openalex"]
    assert f"HTTP {status}" in out["warning"] and "OpenAlex" in out["warning"]
    assert out["count"] == 2
    first, merged = out["results"]
    assert set(first) == _ARXIV_ROW_KEYS and set(merged) == _ARXIV_ROW_KEYS
    assert first == {
        "arxiv_id": "2404.14082",
        "title": "Mechanistic Interpretability for AI Safety -- A Review",
        "authors": ["A. Author", "B. Author"],
        "summary": "Understanding AI systems.",
        "published": "2024-04-22",
        "updated": "",  # OpenAlex's updated_date is its own record's, never arXiv's
        "url": "https://arxiv.org/abs/2404.14082",
        "pdf_url": "https://arxiv.org/pdf/2404.14082",
    }
    # the merged journal+arXiv record resolves through its arXiv location, not its DOI
    assert merged["arxiv_id"] == "1606.03490"
    assert merged["url"] == "https://arxiv.org/abs/1606.03490"

    # a refusal is not retried: one arXiv call, one OpenAlex call, no sleeping
    assert net.urls() == [ps._ARXIV_API, ps._OPENALEX_API]
    assert no_sleep == []
    oa = net.calls_to(ps._OPENALEX_API)[0]
    assert oa["params"] == {
        "search": "mechanistic interpretability",
        "per-page": "5",
        "mailto": ps._CONTACT_EMAIL,
        "filter": "locations.source.id:S4306400194",
    }
    assert ps._CONTACT_EMAIL in oa["headers"]["User-Agent"]
    assert "Authorization" not in oa["headers"]


async def test_fallback_date_sort_maps_to_openalex_publication_date(net, no_sleep):
    net.route(ps._ARXIV_API, _arxiv(406))
    net.route(ps._OPENALEX_API, _openalex())
    await ps.arxiv_search("x", sort_by="date")
    assert net.calls_to(ps._OPENALEX_API)[0]["params"]["sort"] == "publication_date:desc"


async def test_arxiv_happy_path_is_unchanged_and_never_touches_a_fallback(net, no_sleep):
    net.route(ps._ARXIV_API, _arxiv(200))
    out = await ps.arxiv_search("mechanistic interpretability", limit=3)
    assert out == {
        "query": "mechanistic interpretability",
        "count": 1,
        "results": ps.parse_arxiv_atom(_ATOM_FEED, 3),
    }
    assert out["results"][0]["arxiv_id"] == "2404.14082v3"
    assert net.urls() == [ps._ARXIV_API]
    call = net.calls[0]
    assert call["params"]["search_query"] == "all:mechanistic interpretability"
    assert call["headers"] == {"User-Agent": "Meridian/paper_search (research routing)"}
    assert net.clients[0]["follow_redirects"] is True
    assert no_sleep == []


async def test_arxiv_429_then_200_recovers_via_backoff_without_fallback(net, no_sleep):
    net.route(ps._ARXIV_API, _arxiv(429), _arxiv(200))
    out = await ps.arxiv_search("x")
    assert out["count"] == 1 and "fallback_source" not in out and "warning" not in out
    assert net.urls() == [ps._ARXIV_API, ps._ARXIV_API]
    assert no_sleep == [ps._RETRY_DELAYS[0]]


async def test_arxiv_5xx_that_outlasts_backoff_falls_back(net, no_sleep):
    net.route(ps._ARXIV_API, _arxiv(503), _arxiv(502), _arxiv(500))
    net.route(ps._OPENALEX_API, _openalex())
    out = await ps.arxiv_search("x")
    assert out["fallback_source"] == "openalex"
    assert "HTTP 500" in out["warning"]
    assert net.urls() == [ps._ARXIV_API] * 3 + [ps._OPENALEX_API]
    assert no_sleep == list(ps._RETRY_DELAYS)


async def test_arxiv_long_retry_after_falls_back_without_waiting(net, no_sleep):
    net.route(ps._ARXIV_API, _arxiv(429, headers={"Retry-After": "60"}))
    net.route(ps._OPENALEX_API, _openalex())
    out = await ps.arxiv_search("x")
    assert out["fallback_source"] == "openalex" and "HTTP 429" in out["warning"]
    assert net.urls() == [ps._ARXIV_API, ps._OPENALEX_API]
    assert no_sleep == []


@pytest.mark.parametrize("exc", [
    httpx.ConnectError("[Errno 11001] getaddrinfo failed"),
    httpx.ReadTimeout("timed out"),
    httpx.TooManyRedirects("Exceeded maximum allowed redirects."),
])
async def test_arxiv_network_errors_fall_back(net, no_sleep, exc):
    net.route(ps._ARXIV_API, exc)
    net.route(ps._OPENALEX_API, _openalex())
    out = await ps.arxiv_search("x")
    assert out["fallback_source"] == "openalex"
    assert type(exc).__name__ in out["warning"]
    assert net.urls() == [ps._ARXIV_API, ps._OPENALEX_API]  # transport errors aren't retried


async def test_arxiv_non_atom_200_body_falls_back(net, no_sleep):
    net.route(ps._ARXIV_API, _resp(ps._ARXIV_API, 200, text="<!DOCTYPE html><html><body>blocked</body></html>"))
    net.route(ps._OPENALEX_API, _openalex())
    out = await ps.arxiv_search("x")
    assert out["fallback_source"] == "openalex"
    assert "not an Atom feed" in out["warning"]


async def test_arxiv_empty_feed_is_a_real_empty_answer_not_a_failure(net, no_sleep):
    net.route(ps._ARXIV_API, _arxiv(200, text=_EMPTY_FEED))
    assert await ps.arxiv_search("zzqx") == {"query": "zzqx", "count": 0, "results": []}
    assert net.urls() == [ps._ARXIV_API]


async def test_arxiv_400_stays_an_error_without_fallback(net, no_sleep):
    net.route(ps._ARXIV_API, _arxiv(400))
    out = await ps.arxiv_search("x")
    assert out["error"].startswith("arxiv search failed: Client error '400 Bad Request'")
    assert out["query"] == "x"
    assert "sources_tried" not in out and "results" not in out
    assert net.urls() == [ps._ARXIV_API]


# ---------------------------------------------------------------------------
# Second fallback + everything failing
# ---------------------------------------------------------------------------

async def test_openalex_failure_falls_through_to_semantic_scholar(net, no_sleep):
    net.route(ps._ARXIV_API, _arxiv(406))
    net.route(ps._OPENALEX_API, _openalex(500), _openalex(500), _openalex(500))
    net.route(ps._S2_PAPER_API, _s2())

    out = await ps.arxiv_search("mech interp", limit=4)

    assert out["fallback_source"] == "semantic_scholar"
    assert out["sources_tried"] == ["arxiv", "openalex", "semantic_scholar"]
    assert "Semantic Scholar" in out["warning"]
    assert "openalex: HTTP 500" in out["warning"]
    assert [r["arxiv_id"] for r in out["results"]] == ["2501.16496", "hep-th/9901001"]
    first = out["results"][0]
    assert set(first) == _ARXIV_ROW_KEYS
    assert first["title"] == "Open Problems in Mechanistic Interpretability"
    assert first["authors"] == ["D. Author"]
    assert first["summary"] == "Open problems."
    assert first["published"] == "2025-01-27"
    assert first["pdf_url"] == "https://arxiv.org/pdf/2501.16496"
    assert out["results"][1]["published"] == "1999"
    s2_call = net.calls_to(ps._S2_PAPER_API)[0]
    assert s2_call["params"]["query"] == "mech interp"
    assert s2_call["params"]["limit"] == "8"  # over-fetch: non-arXiv papers are dropped
    assert "externalIds" in s2_call["params"]["fields"]
    assert ps._CONTACT_EMAIL in s2_call["headers"]["User-Agent"]


async def test_semantic_scholar_fallback_flags_that_it_cannot_date_sort(net, no_sleep):
    net.route(ps._ARXIV_API, _arxiv(406))
    net.route(ps._OPENALEX_API, httpx.ConnectError("down"))
    net.route(ps._S2_PAPER_API, _s2())
    out = await ps.arxiv_search("x", sort_by="date")
    assert out["fallback_source"] == "semantic_scholar"
    assert "cannot sort by date" in out["warning"]


async def test_every_source_failing_returns_error_listing_sources_tried(net, no_sleep):
    net.route(ps._ARXIV_API, _arxiv(406))
    net.route(ps._OPENALEX_API, *[_openalex(429) for _ in range(3)])
    net.route(ps._S2_PAPER_API, *[_s2(429) for _ in range(3)])

    out = await ps.arxiv_search("x")

    assert set(out) == {"error", "query", "sources_tried"}
    assert out["query"] == "x"
    assert out["sources_tried"] == ["arxiv", "openalex", "semantic_scholar"]
    assert out["error"].startswith("arxiv search failed: arXiv was unreachable from this server (HTTP 406")
    assert "openalex: HTTP 429 Too Many Requests" in out["error"]
    assert "OPENALEX_API_KEY" in out["error"]
    assert "semantic_scholar: HTTP 429 Too Many Requests" in out["error"]
    assert net.urls().count(ps._OPENALEX_API) == 3 and net.urls().count(ps._S2_PAPER_API) == 3


async def test_malformed_openalex_json_counts_as_a_failed_fallback(net, no_sleep):
    net.route(ps._ARXIV_API, _arxiv(406))
    net.route(ps._OPENALEX_API, _resp(ps._OPENALEX_API, 200, text="not json"))
    net.route(ps._S2_PAPER_API, _s2())
    out = await ps.arxiv_search("x")
    assert out["fallback_source"] == "semantic_scholar"
    assert "openalex: JSONDecodeError" in out["warning"]


# ---------------------------------------------------------------------------
# openalex_search: polite pool, backoff, API key
# ---------------------------------------------------------------------------

_PLAIN_OPENALEX = {"results": [{"id": "https://openalex.org/W1", "title": "T", "authorships": []}]}


async def test_openalex_search_sends_mailto_and_retries_429(net, no_sleep):
    net.route(
        ps._OPENALEX_API,
        _openalex(429),
        _openalex(200, _PLAIN_OPENALEX),
        _openalex(200, _PLAIN_OPENALEX),
        _openalex(200, _PLAIN_OPENALEX),
    )
    out = await ps.openalex_search("graph neural networks", limit=7)
    assert out["count"] == 1 and out["results"][0]["openalex_id"] == "W1"
    assert out["openalex_query_stage"] == "loose"
    assert net.urls() == [ps._OPENALEX_API] * 4
    assert no_sleep == [ps._RETRY_DELAYS[0]]
    call = net.calls[0]
    assert call["params"] == {
        "search": '"graph neural" AND networks',
        "per-page": "7",
        "mailto": ps._CONTACT_EMAIL,
    }
    assert f"mailto:{ps._CONTACT_EMAIL}" in call["headers"]["User-Agent"]
    assert "Authorization" not in call["headers"]
    assert net.clients[0]["follow_redirects"] is True


async def test_openalex_search_relaxes_queries_and_keeps_strict_results_first(monkeypatch):
    calls = []

    def work(work_id, title):
        return {"id": f"https://openalex.org/{work_id}", "title": title}

    payloads = [
        {"results": [work("W1", "")]},
        {"results": [work("W1", "Coding agent memory"), work("W2", "Rules for agents")]},
        {"results": [work("W2", "Rules for agents"), work("W3", "Hook compliance") ]},
    ]

    async def get_works(query, limit, sort_by, **kwargs):
        calls.append(query)
        return payloads[len(calls) - 1]

    monkeypatch.setattr(ps, "_openalex_get_works", get_works)
    out = await ps.openalex_search("coding agent memory rule compliance hooks", limit=3)

    assert calls == [
        '"coding agent" AND memory AND rule AND compliance AND hooks',
        "coding AND agent AND memory AND rule AND compliance AND hooks",
        "coding agent memory rule compliance hooks",
    ]
    assert out["openalex_query_stage"] == "loose"
    assert [row["openalex_id"] for row in out["results"]] == ["W1", "W2", "W3"]
    assert out["results"][0]["title"] == "Coding agent memory"
    from meridian import mcp_tools

    paper_search = next(t for t in mcp_tools._MCP_TOOLS_LIST if t["name"] == "paper_search")
    assert "openalex_query_stage" in paper_search["description"]


async def test_openalex_search_relaxes_after_a_failed_strict_stage_and_keeps_never_raise(monkeypatch):
    calls = []

    async def get_works(query, limit, sort_by, **kwargs):
        calls.append(query)
        if len(calls) == 1:
            raise ValueError("strict query rejected")
        return {"results": [{"id": "https://openalex.org/W9", "title": "Relevant result"}]}

    monkeypatch.setattr(ps, "_openalex_get_works", get_works)
    out = await ps.openalex_search("coding agent memory rule compliance hooks", limit=1)

    assert len(calls) == 2
    assert out["openalex_query_stage"] == "and"
    assert out["results"][0]["openalex_id"] == "W9"
    assert ps._openalex_query_stages('"quoted phrase" AND tail') == [
        ("loose", '"quoted phrase" AND tail')
    ]
    assert ps._openalex_query_stages("one OR two three") == [("loose", "one OR two three")]


def test_openalex_query_stage_and_row_identity_fallbacks():
    assert ps._openalex_query_stages("one two") == [("loose", "one two")]
    assert ps._openalex_row_identity({"doi": " https://doi.org/10.1234/A "}, 0) == (
        "doi:https://doi.org/10.1234/a"
    )
    assert ps._openalex_row_identity({"title": "  A   Title "}, 0) == "title:a title"
    assert ps._openalex_row_identity({}, 4) == "anonymous:4"


@pytest.mark.asyncio
async def test_openalex_arxiv_rows_compatibility_wrapper(monkeypatch):
    rows = [{"arxiv_id": "2401.12345"}]

    async def staged(*args):
        return rows, "phrase_and"

    monkeypatch.setattr(ps, "_openalex_arxiv_rows_staged", staged)
    assert await ps._openalex_arxiv_rows("query", 5, "relevance") == rows


@pytest.mark.asyncio
async def test_openalex_search_respects_shared_stage_deadline(monkeypatch):
    async def should_not_call(*args, **kwargs):
        raise AssertionError("expired search budget must not start a request")

    monkeypatch.setattr(ps, "_OPENALEX_STAGED_SEARCH_BUDGET_SECONDS", 0)
    monkeypatch.setattr(ps, "_openalex_get_works", should_not_call)
    out = await ps.openalex_search("three word query")

    assert "error" in out
    assert "latency budget" in out["error"]


@pytest.mark.asyncio
async def test_openalex_search_stops_relaxing_on_rate_limit(monkeypatch):
    calls = []

    class RateLimited(Exception):
        response = type("Response", (), {"status_code": 429})()

    async def get_works(query, limit, sort_by, **kwargs):
        calls.append(query)
        raise RateLimited("rate limited")

    monkeypatch.setattr(ps, "_openalex_get_works", get_works)
    out = await ps.openalex_search("coding agent memory rules")

    assert len(calls) == 1
    assert out["error"].startswith("openalex search failed: rate limited")


@pytest.mark.asyncio
async def test_openalex_search_returns_error_when_every_stage_fails(monkeypatch):
    calls = []

    async def get_works(query, limit, sort_by, **kwargs):
        calls.append(query)
        raise ValueError("search unavailable")

    monkeypatch.setattr(ps, "_openalex_get_works", get_works)
    out = await ps.openalex_search("coding agent memory rules")

    assert len(calls) == 3
    assert out["error"].startswith("openalex search failed: search unavailable")


async def test_openalex_search_retries_5xx(net, no_sleep):
    net.route(ps._OPENALEX_API, _openalex(500), _openalex(502), _openalex(200, _PLAIN_OPENALEX))
    out = await ps.openalex_search("x")
    assert out["count"] == 1
    assert no_sleep == list(ps._RETRY_DELAYS)


async def test_openalex_search_honours_retry_after(net, no_sleep):
    net.route(ps._OPENALEX_API, _openalex(429, headers={"Retry-After": "2"}), _openalex(200, _PLAIN_OPENALEX))
    assert (await ps.openalex_search("x"))["count"] == 1
    assert no_sleep == [2.0]


async def test_openalex_search_fails_fast_on_long_retry_after_with_key_hint(net, no_sleep):
    # the live anonymous-search throttle asked for ~30s; retrying at 0.5s/1.5s is pointless
    net.route(ps._OPENALEX_API, _openalex(429, headers={"Retry-After": "32"}))
    out = await ps.openalex_search("x")
    assert out["error"].startswith("openalex search failed")
    assert "OPENALEX_API_KEY" in out["error"]
    assert len(net.calls) == 1 and no_sleep == []


async def test_openalex_search_does_not_retry_4xx(net, no_sleep):
    net.route(ps._OPENALEX_API, _openalex(400))
    out = await ps.openalex_search("x")
    assert "error" in out and "OPENALEX_API_KEY" not in out["error"]
    assert len(net.calls) == 1 and no_sleep == []


async def test_openalex_api_key_goes_only_in_a_bearer_header(net, no_sleep, monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "oa-secret-key")
    net.route(ps._OPENALEX_API, _openalex(200, _PLAIN_OPENALEX))
    await ps.openalex_search("x")
    call = net.calls[0]
    assert call["headers"]["Authorization"] == "Bearer oa-secret-key"
    assert "oa-secret-key" not in json.dumps(call["params"])


async def test_openalex_api_key_is_redacted_from_errors(net, no_sleep, monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "oa-secret-key")
    net.route(ps._OPENALEX_API, httpx.ConnectError("proxy rejected Bearer oa-secret-key"))
    out = await ps.openalex_search("x")
    assert "oa-secret-key" not in out["error"] and "***" in out["error"]

    net.route(ps._ARXIV_API, _arxiv(406))
    net.route(ps._OPENALEX_API, httpx.ConnectError("proxy rejected Bearer oa-secret-key"))
    net.route(ps._S2_PAPER_API, _s2(404))
    out = await ps.arxiv_search("x")
    assert "oa-secret-key" not in out["error"] and "***" in out["error"]


# ---------------------------------------------------------------------------
# _fetch_with_backoff extensions keep the Crossref/CORE contract
# ---------------------------------------------------------------------------

async def test_fetch_with_backoff_default_statuses_unchanged(net, no_sleep):
    net.route(ps._CROSSREF_API, _resp(ps._CROSSREF_API, 500), _resp(ps._CROSSREF_API, payload={}))
    out = await ps.crossref_search("x")
    assert out["error"].startswith("crossref search failed")  # 500 is not in the default set
    assert len(net.calls) == 1 and no_sleep == []


async def test_fetch_with_backoff_budget_stops_retrying(net, no_sleep):
    net.route(ps._ARXIV_API, _arxiv(429), _arxiv(429), _arxiv(200))
    async with httpx.AsyncClient() as http:
        with pytest.raises(httpx.HTTPStatusError):
            # 0.5s fits a 1.0s budget, the following 1.5s wait does not
            await ps._fetch_with_backoff(http, ps._ARXIV_API, {}, {}, retry_statuses=ps._TRANSIENT_STATUSES, budget=1.0)
    assert len(net.calls) == 2 and no_sleep == [0.5]


@pytest.mark.parametrize("raw,expected", [
    ("3", 3.0), (" 1.5 ", 1.5), ("-4", 0.0), ("Sun, 06 Nov 1994 08:49:37 GMT", 0.0),
    ("soon", None), ("", None), (None, None),
])
def test_retry_after_parsing(raw, expected):
    headers = {} if raw is None else {"Retry-After": raw}
    response = httpx.Response(429, headers=headers)
    assert ps._retry_after_seconds(response) == expected


def test_retry_after_http_date_in_the_future():
    response = httpx.Response(429, headers={"Retry-After": "Fri, 01 Jan 2100 00:00:00 GMT"})
    assert ps._retry_after_seconds(response) > ps._MAX_RETRY_AFTER


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def test_parse_openalex_arxiv_works_resolves_every_id_form_and_dedups():
    works = [
        {"doi": "https://doi.org/10.48550/arxiv.hep-th/9901001", "title": "Old style via DOI"},
        {"locations": [{"id": "pmh:oai:arXiv.org:math.GT/0309136"}], "title": "Old style w/ subject class"},
        {"locations": [{"landing_page_url": "http://arxiv.org/abs/2404.14082v2"}], "title": "Versioned URL"},
        {"ids": {"doi": "https://doi.org/10.48550/arxiv.2301.04709"}, "title": "Via ids.doi"},
        {"doi": "https://doi.org/10.1103/physrevx.13.041039", "title": "No arXiv id: dropped"},
        {"doi": "https://doi.org/10.48550/arxiv.2301.04709", "title": "Duplicate: dropped"},
        "not-a-dict",
        {"locations": "not-a-list", "primary_location": None},
    ]
    rows = ps.parse_openalex_arxiv_works({"results": works})
    assert [r["arxiv_id"] for r in rows] == ["hep-th/9901001", "math.GT/0309136", "2404.14082v2", "2301.04709"]
    assert rows[1]["url"] == "https://arxiv.org/abs/math.GT/0309136"
    assert all(set(r) == _ARXIV_ROW_KEYS for r in rows)
    assert len(ps.parse_openalex_arxiv_works({"results": works}, limit=2)) == 2


def test_arxiv_id_prefers_the_pmh_location_over_pdf_url_and_lowercased_doi():
    work = {
        "doi": "https://doi.org/10.48550/arxiv.math.gt/0309136",
        "locations": [
            {"landing_page_url": "https://arxiv.org/pdf/math.GT/0309136.pdf"},
            {"id": "pmh:oai:arXiv.org:math.GT/0309136"},
        ],
    }
    assert ps._arxiv_id_from_openalex_work(work) == "math.GT/0309136"
    # a ".pdf" URL alone still yields the bare id
    assert ps._arxiv_id_from_openalex_work({"locations": [{"landing_page_url": "https://arxiv.org/pdf/1606.03490.pdf"}]}) == "1606.03490"


@pytest.mark.parametrize("payload", [None, 7, "x", {}, {"results": None}, {"results": "x"}])
def test_parse_openalex_arxiv_works_malformed_degrades_to_empty(payload):
    assert ps.parse_openalex_arxiv_works(payload) == []


@pytest.mark.parametrize("payload", [None, 7, "x", {}, {"data": None}, {"data": ["x", {"externalIds": "x"}]}])
def test_parse_semantic_scholar_arxiv_papers_malformed_degrades_to_empty(payload):
    assert ps.parse_semantic_scholar_arxiv_papers(payload) == []


def test_parse_semantic_scholar_arxiv_papers_limit_and_dedup():
    dup = {"data": [_S2_PAYLOAD["data"][0], _S2_PAYLOAD["data"][0], _S2_PAYLOAD["data"][2]]}
    assert [r["arxiv_id"] for r in ps.parse_semantic_scholar_arxiv_papers(dup)] == ["2501.16496", "hep-th/9901001"]
    assert len(ps.parse_semantic_scholar_arxiv_papers(dup, limit=1)) == 1


def test_parse_arxiv_atom_contract_unchanged_for_non_feeds():
    assert ps.parse_arxiv_atom("<html></html>") == []
    assert ps._parse_arxiv_feed("<html></html>") is None
    assert ps._parse_arxiv_feed(_EMPTY_FEED) == []


# ---------------------------------------------------------------------------
# MCP handler + research watchlist
# ---------------------------------------------------------------------------

async def test_handle_paper_search_passes_fallback_fields_through(net, no_sleep):
    net.route(ps._ARXIV_API, _arxiv(406))
    net.route(ps._OPENALEX_API, _openalex())
    out = await st_mod.handle_paper_search({"query": "x", "source": "arxiv", "limit": 5}, None, "", None, None)
    assert out["fallback_source"] == "openalex"
    assert out["results"][0]["arxiv_id"] == "2404.14082"
    assert "warning" in out and out["sources_tried"] == ["arxiv", "openalex"]


@pytest_asyncio.fixture
async def db():
    conn = await db_module.init_db(":memory:")
    yield conn
    await conn.close()


def test_watchlist_identity_key_drops_the_arxiv_version():
    assert rw._identity_key("arxiv", {"arxiv_id": "2404.14082v3"}) == "arxiv_id:2404.14082"
    assert rw._identity_key("arxiv", {"arxiv_id": "hep-th/9901001v2"}) == "arxiv_id:hep-th/9901001"
    assert rw._identity_key("arxiv", {"arxiv_id": "2404.14082"}) == "arxiv_id:2404.14082"
    # other sources are untouched, even when their id happens to end in v<digits>
    assert rw._identity_key("semantic_scholar", {"s2_id": "abcv2"}) == "s2_id:abcv2"


async def test_watchlist_dedups_across_an_arxiv_run_and_a_fallback_run(db, monkeypatch):
    project = await db_module.create_project(db, "proj-454bdee5")
    pid = project["id"]
    saved = await rw.handle_save_watchlist_query(
        {"project_id": pid, "source_type": "arxiv", "query": "mech interp"}, db, "", None, None,
    )
    wid = saved["watchlist_id"]

    # A legacy capture from before this change, tagged with the VERSIONED key.
    await db_module.add_project_note(
        db, pid, "legacy", "legacy finding",
        tags=f"finding,arxiv,watchlist:{wid},item:arxiv_id:1606.03490v3", kind="reference",
    )

    # Run 1: arXiv answers directly (versioned ids).
    async def _direct(query, limit=10, sort_by="relevance"):
        return {"query": query, "count": 1, "results": ps.parse_arxiv_atom(_ATOM_FEED)}

    monkeypatch.setattr(ps, "arxiv_search", _direct)
    first = await rw.handle_run_watchlist_query({"project_id": pid, "watchlist_id": wid}, db, "", None, None)
    assert first["new_count"] == 1
    assert first["captured"][0]["item_key"] == "arxiv_id:2404.14082"

    # Run 2: arXiv unreachable, the OpenAlex fallback answers with bare ids.
    async def _fallback(query, limit=10, sort_by="relevance"):
        rows = ps.parse_openalex_arxiv_works(_OPENALEX_ARXIV_PAYLOAD)
        return {"query": query, "count": len(rows), "results": rows, "fallback_source": "openalex",
                "warning": "w", "sources_tried": ["arxiv", "openalex"]}

    monkeypatch.setattr(ps, "arxiv_search", _fallback)
    second = await rw.handle_run_watchlist_query({"project_id": pid, "watchlist_id": wid}, db, "", None, None)
    # 2404.14082 was seen as v3 in run 1; 1606.03490 matches the legacy versioned tag.
    assert second["total_results"] == 2
    assert second["new_count"] == 0 and second["already_seen_count"] == 2
    assert second["captured"] == []
