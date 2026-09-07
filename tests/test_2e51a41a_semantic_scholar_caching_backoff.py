"""2e51a41a — Semantic Scholar paper/author search, PubMed search, and
caching/backoff improvements for github_search and social_search.

Network calls are mocked so CI never reaches the network. Parsing helpers
are tested against fixture payloads, and the async search functions are
tested with monkeypatched httpx.AsyncClient instances. Cache and backoff
behavior is verified by inspecting call counts and asyncio.sleep invocations.
"""
from __future__ import annotations

import asyncio

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SAMPLE_S2_PAYLOAD = {
    "total": 2,
    "offset": 0,
    "data": [
        {
            "paperId": "abc123def456",
            "title": "Deep learning for plant disease detection",
            "authors": [
                {"authorId": "1", "name": "Alice Researcher"},
                {"authorId": "2", "name": "Bob Scientist"},
            ],
            "abstract": "We propose a CNN-based approach for plant disease detection.",
            "year": 2022,
            "citationCount": 87,
            "tldr": {"model": "tldr@v2.0.0", "text": "CNN detects plant diseases."},
            "openAccessPdf": {"url": "https://example.com/paper.pdf", "status": "HYBRID"},
            "externalIds": {"DOI": "10.1234/plant.2022", "ArXiv": "2201.12345"},
        },
        {
            "paperId": "xyz789",
            "title": "Wheat rust resistance",
            "authors": [],
            "abstract": None,
            "year": None,
            "citationCount": None,
            "tldr": {"model": "tldr@v2.0.0", "text": "TLDR only, no abstract."},
            "openAccessPdf": None,
            "externalIds": {},
        },
    ],
}

_SAMPLE_AUTHOR_PAYLOAD = {
    "total": 1,
    "offset": 0,
    "data": [
        {
            "authorId": "auth-001",
            "name": "Alice Researcher",
            "affiliations": ["MIT CSAIL", "Broad Institute"],
            "paperCount": 42,
            "citationCount": 1500,
            "papers": [
                {
                    "paperId": "p1",
                    "title": "First great paper",
                    "year": 2020,
                    "externalIds": {"DOI": "10.1234/first"},
                },
                {
                    "paperId": "p2",
                    "title": "Second paper",
                    "year": None,
                    "externalIds": None,
                },
            ],
        }
    ],
}

_SAMPLE_ESEARCH = {
    "esearchresult": {
        "count": "1",
        "retmax": "10",
        "idlist": ["12345678"],
    }
}

_SAMPLE_PUBMED_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>12345678</PMID>
      <Article>
        <ArticleTitle>Effects of nitrogen on wheat growth</ArticleTitle>
        <Abstract>
          <AbstractText>Nitrogen fertilization significantly increases wheat yield.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author>
            <LastName>Smith</LastName>
            <ForeName>John A</ForeName>
          </Author>
          <Author>
            <LastName>Jones</LastName>
            <ForeName>Jane B</ForeName>
          </Author>
        </AuthorList>
        <Journal>
          <JournalIssue>
            <PubDate>
              <Year>2020</Year>
              <Month>Jan</Month>
            </PubDate>
          </JournalIssue>
        </Journal>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">12345678</ArticleId>
        <ArticleId IdType="doi">10.1234/example.doi</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


# ---------------------------------------------------------------------------
# Helpers shared across multiple tests
# ---------------------------------------------------------------------------

class _JsonResp:
    """Fake httpx response that returns JSON."""

    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _TextResp:
    """Fake httpx response that returns plain text."""

    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class _ErrorResp:
    """Fake httpx response with a status code for HTTPStatusError construction."""

    def __init__(self, status_code: int):
        self.status_code = status_code


# ---------------------------------------------------------------------------
# parse_semantic_scholar_papers
# ---------------------------------------------------------------------------

def test_parse_s2_papers_extracts_fields():
    from meridian.paper_search import parse_semantic_scholar_papers

    papers = parse_semantic_scholar_papers(_SAMPLE_S2_PAYLOAD)
    assert len(papers) == 2

    p = papers[0]
    assert p["s2_id"] == "abc123def456"
    assert p["title"] == "Deep learning for plant disease detection"
    assert p["authors"] == ["Alice Researcher", "Bob Scientist"]
    assert p["summary"] == "We propose a CNN-based approach for plant disease detection."
    assert p["published"] == "2022"
    assert p["updated"] == ""
    assert p["url"] == "https://example.com/paper.pdf"
    assert p["pdf_url"] == "https://example.com/paper.pdf"
    assert p["citation_count"] == 87
    assert p["doi"] == "10.1234/plant.2022"
    assert p["tldr"] == "CNN detects plant diseases."


def test_parse_s2_papers_falls_back_to_tldr_when_no_abstract():
    from meridian.paper_search import parse_semantic_scholar_papers

    papers = parse_semantic_scholar_papers(_SAMPLE_S2_PAYLOAD)
    q = papers[1]
    # no abstract, no pdf, no year, no citation_count
    assert q["s2_id"] == "xyz789"
    assert q["summary"] == "TLDR only, no abstract."
    assert q["published"] == ""
    assert q["citation_count"] == 0
    assert q["doi"] == ""
    # url falls back to S2 page
    assert "semanticscholar.org/paper/xyz789" in q["url"]
    assert q["pdf_url"] == ""


def test_parse_s2_papers_respects_limit():
    from meridian.paper_search import parse_semantic_scholar_papers

    assert len(parse_semantic_scholar_papers(_SAMPLE_S2_PAYLOAD, limit=1)) == 1


def test_parse_s2_papers_never_raises_on_garbage():
    from meridian.paper_search import parse_semantic_scholar_papers

    assert parse_semantic_scholar_papers(None) == []
    assert parse_semantic_scholar_papers({}) == []
    assert parse_semantic_scholar_papers({"data": None}) == []
    assert parse_semantic_scholar_papers("not a dict") == []
    # malformed rows are skipped, never crash
    result = parse_semantic_scholar_papers({"data": ["nope", {}]})
    assert isinstance(result, list)
    assert len(result) == 1  # empty dict → one empty row
    row = result[0]
    assert row["s2_id"] == ""
    assert row["authors"] == []


# ---------------------------------------------------------------------------
# semantic_scholar_search (network mocked)
# ---------------------------------------------------------------------------

async def test_semantic_scholar_search_empty_query_returns_error():
    from meridian.paper_search import semantic_scholar_search

    out = await semantic_scholar_search("   ")
    assert out.get("error")
    assert "results" not in out


async def test_semantic_scholar_search_returns_results(monkeypatch):
    import httpx
    import meridian.paper_search as ps

    class _Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None, headers=None):
            _Client.seen = {"url": url, "params": params}
            return _JsonResp(_SAMPLE_S2_PAYLOAD)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    out = await ps.semantic_scholar_search("plant disease", limit=2)
    assert out["count"] == 2
    assert out["results"][0]["s2_id"] == "abc123def456"
    assert out["query"] == "plant disease"
    assert ps._S2_PAPER_API in _Client.seen["url"]
    assert _Client.seen["params"]["query"] == "plant disease"
    assert "fields" in _Client.seen["params"]


async def test_semantic_scholar_search_degrades_on_network_error(monkeypatch):
    import httpx
    import meridian.paper_search as ps

    class _RaisingClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **kw):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", _RaisingClient)
    out = await ps.semantic_scholar_search("anything")
    assert "error" in out
    assert out["query"] == "anything"
    assert "results" not in out


# ---------------------------------------------------------------------------
# author_search (network mocked)
# ---------------------------------------------------------------------------

async def test_author_search_empty_name_returns_error():
    from meridian.paper_search import author_search

    out = await author_search("   ")
    assert out.get("error")
    assert "results" not in out


async def test_author_search_returns_results(monkeypatch):
    import httpx
    import meridian.paper_search as ps

    class _Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None, headers=None):
            _Client.seen = {"url": url, "params": params}
            return _JsonResp(_SAMPLE_AUTHOR_PAYLOAD)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    out = await ps.author_search("Alice Researcher", limit=3)
    assert out["count"] == 1
    assert out["query"] == "Alice Researcher"
    a = out["results"][0]
    assert a["author_id"] == "auth-001"
    assert a["name"] == "Alice Researcher"
    assert a["affiliations"] == ["MIT CSAIL", "Broad Institute"]
    assert a["paper_count"] == 42
    assert a["citation_count"] == 1500
    assert len(a["papers"]) == 2
    assert a["papers"][0]["title"] == "First great paper"
    assert a["papers"][0]["year"] == 2020
    assert a["papers"][0]["doi"] == "10.1234/first"
    # second paper: no year, no externalIds
    assert a["papers"][1]["year"] is None
    assert a["papers"][1]["doi"] == ""
    assert ps._S2_AUTHOR_API in _Client.seen["url"]


async def test_author_search_degrades_on_network_error(monkeypatch):
    import httpx
    import meridian.paper_search as ps

    class _RaisingClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **kw):
            raise RuntimeError("timeout")

    monkeypatch.setattr(httpx, "AsyncClient", _RaisingClient)
    out = await ps.author_search("Anyone")
    assert "error" in out
    assert out["query"] == "Anyone"


# ---------------------------------------------------------------------------
# parse_pubmed_articles
# ---------------------------------------------------------------------------

def test_parse_pubmed_articles_extracts_fields():
    from meridian.paper_search import parse_pubmed_articles

    papers = parse_pubmed_articles(_SAMPLE_PUBMED_XML)
    assert len(papers) == 1
    p = papers[0]
    assert p["pmid"] == "12345678"
    assert p["title"] == "Effects of nitrogen on wheat growth"
    assert p["authors"] == ["John A Smith", "Jane B Jones"]
    assert p["summary"] == "Nitrogen fertilization significantly increases wheat yield."
    assert p["published"] == "2020-Jan"
    assert p["url"] == "https://pubmed.ncbi.nlm.nih.gov/12345678/"
    assert p["pdf_url"] == ""
    assert p["doi"] == "10.1234/example.doi"
    assert p["updated"] == ""


def test_parse_pubmed_articles_never_raises_on_garbage():
    from meridian.paper_search import parse_pubmed_articles

    assert parse_pubmed_articles("") == []
    assert parse_pubmed_articles("not xml") == []
    assert parse_pubmed_articles("<PubmedArticleSet></PubmedArticleSet>") == []


# ---------------------------------------------------------------------------
# pubmed_search (network mocked, two-step esearch+efetch)
# ---------------------------------------------------------------------------

async def test_pubmed_search_empty_query_returns_error():
    from meridian.paper_search import pubmed_search

    out = await pubmed_search("   ")
    assert out.get("error")
    assert "results" not in out


async def test_pubmed_search_returns_results(monkeypatch):
    import httpx
    import meridian.paper_search as ps

    urls_called = []

    class _Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def get(self, url, params=None, headers=None):
            urls_called.append(url)
            if "esearch" in url:
                return _JsonResp(_SAMPLE_ESEARCH)
            # efetch returns XML text
            return _TextResp(_SAMPLE_PUBMED_XML)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    out = await ps.pubmed_search("wheat nitrogen", limit=5)
    assert out["count"] == 1
    assert out["query"] == "wheat nitrogen"
    assert out["results"][0]["pmid"] == "12345678"
    # both esearch and efetch were called
    assert any("esearch" in u for u in urls_called)
    assert any("efetch" in u for u in urls_called)


async def test_pubmed_search_no_ids_returns_empty(monkeypatch):
    import httpx
    import meridian.paper_search as ps

    class _Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None, headers=None):
            return _JsonResp({"esearchresult": {"idlist": []}})

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    out = await ps.pubmed_search("xyzzy no results")
    assert out["count"] == 0
    assert out["results"] == []
    assert "error" not in out


async def test_pubmed_search_degrades_on_network_error(monkeypatch):
    import httpx
    import meridian.paper_search as ps

    class _RaisingClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **kw):
            raise OSError("network unreachable")

    monkeypatch.setattr(httpx, "AsyncClient", _RaisingClient)
    out = await ps.pubmed_search("biomedical query")
    assert "error" in out
    assert out["query"] == "biomedical query"
    assert "results" not in out


async def test_pubmed_search_sort_by_date_adds_sort_param(monkeypatch):
    import httpx
    import meridian.paper_search as ps

    seen_params = []

    class _Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None, headers=None):
            if "esearch" in url:
                seen_params.append(params or {})
                return _JsonResp({"esearchresult": {"idlist": []}})
            return _JsonResp({})  # unreachable since idlist is empty

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await ps.pubmed_search("plant pathology", sort_by="date")
    assert seen_params and seen_params[0].get("sort") == "pub+date"


# ---------------------------------------------------------------------------
# github_search: caching
# ---------------------------------------------------------------------------

async def test_github_code_search_caches_successful_result(monkeypatch):
    import httpx
    import meridian.github_search as gs

    QUERY = "caching-test-code-2e51a41a"
    LIMIT = 3
    # ensure a clean slate for this specific key
    gs._CACHE.pop(("code", QUERY, LIMIT), None)

    call_count = [0]

    class _CountingResp:
        def raise_for_status(self): pass
        def json(self): return {"items": []}

    class _CountingClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None, headers=None):
            call_count[0] += 1
            return _CountingResp()

    monkeypatch.setattr(httpx, "AsyncClient", _CountingClient)
    out1 = await gs.github_code_search(QUERY, limit=LIMIT)
    out2 = await gs.github_code_search(QUERY, limit=LIMIT)

    assert out1 == out2
    assert call_count[0] == 1  # second call served from cache, no extra HTTP


async def test_github_repo_search_caches_successful_result(monkeypatch):
    import httpx
    import meridian.github_search as gs

    QUERY = "caching-test-repo-2e51a41a"
    LIMIT = 4
    gs._CACHE.pop(("repo", QUERY, LIMIT), None)

    call_count = [0]

    class _CountingResp:
        def raise_for_status(self): pass
        def json(self): return {"items": []}

    class _CountingClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None, headers=None):
            call_count[0] += 1
            return _CountingResp()

    monkeypatch.setattr(httpx, "AsyncClient", _CountingClient)
    out1 = await gs.github_repo_search(QUERY, limit=LIMIT)
    out2 = await gs.github_repo_search(QUERY, limit=LIMIT)

    assert out1 == out2
    assert call_count[0] == 1


async def test_github_code_search_errors_are_not_cached(monkeypatch):
    """A failed request must not be cached; the next call must hit the network again."""
    import httpx
    import meridian.github_search as gs

    QUERY = "error-not-cached-2e51a41a"
    LIMIT = 2
    gs._CACHE.pop(("code", QUERY, LIMIT), None)

    call_count = [0]

    class _RaisingClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None, headers=None):
            call_count[0] += 1
            raise RuntimeError("fail")

    monkeypatch.setattr(httpx, "AsyncClient", _RaisingClient)
    out1 = await gs.github_code_search(QUERY, limit=LIMIT)
    out2 = await gs.github_code_search(QUERY, limit=LIMIT)

    assert "error" in out1
    assert "error" in out2
    assert call_count[0] == 2  # both calls hit the network (not cached)


# ---------------------------------------------------------------------------
# github_search: backoff
# ---------------------------------------------------------------------------

async def test_github_code_search_retries_on_429(monkeypatch):
    """A 429 response must trigger retry with the correct sleep delays."""
    import httpx
    import meridian.github_search as gs

    QUERY = "backoff-429-code-2e51a41a"
    LIMIT = 2
    gs._CACHE.pop(("code", QUERY, LIMIT), None)

    sleep_calls: list[float] = []

    async def _mock_sleep(t: float) -> None:
        sleep_calls.append(t)

    monkeypatch.setattr(asyncio, "sleep", _mock_sleep)

    attempt_count = [0]

    class _429ThenOkResp:
        def raise_for_status(self): pass
        def json(self): return {"items": []}

    class _429ThenOkClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def get(self, url, params=None, headers=None):
            attempt_count[0] += 1
            if attempt_count[0] < 3:
                raise httpx.HTTPStatusError(
                    "rate limited", request=None, response=_ErrorResp(429)
                )
            return _429ThenOkResp()

    monkeypatch.setattr(httpx, "AsyncClient", _429ThenOkClient)
    out = await gs.github_code_search(QUERY, limit=LIMIT)

    assert "results" in out
    assert attempt_count[0] == 3
    # slept before attempt 2 (0.5s) and before attempt 3 (1.5s)
    assert sleep_calls == list(gs._RETRY_DELAYS)


async def test_github_code_search_exhausted_429_degrades(monkeypatch):
    """Three consecutive 429s must degrade to {error} instead of raising."""
    import httpx
    import meridian.github_search as gs

    QUERY = "all-429-2e51a41a"
    LIMIT = 2
    gs._CACHE.pop(("code", QUERY, LIMIT), None)

    async def _mock_sleep(t: float) -> None:
        pass  # instant

    monkeypatch.setattr(asyncio, "sleep", _mock_sleep)

    class _Always429Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None, headers=None):
            raise httpx.HTTPStatusError(
                "rate limited", request=None, response=_ErrorResp(429)
            )

    monkeypatch.setattr(httpx, "AsyncClient", _Always429Client)
    out = await gs.github_code_search(QUERY, limit=LIMIT)
    assert "error" in out
    assert out["query"] == QUERY


async def test_github_code_search_non_429_error_does_not_retry(monkeypatch):
    """A 403 HTTPStatusError must not be retried — degrade immediately."""
    import httpx
    import meridian.github_search as gs

    QUERY = "403-no-retry-2e51a41a"
    LIMIT = 2
    gs._CACHE.pop(("code", QUERY, LIMIT), None)

    sleep_calls: list[float] = []

    async def _mock_sleep(t: float) -> None:
        sleep_calls.append(t)

    monkeypatch.setattr(asyncio, "sleep", _mock_sleep)

    attempt_count = [0]

    class _403Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None, headers=None):
            attempt_count[0] += 1
            raise httpx.HTTPStatusError(
                "forbidden", request=None, response=_ErrorResp(403)
            )

    monkeypatch.setattr(httpx, "AsyncClient", _403Client)
    out = await gs.github_code_search(QUERY, limit=LIMIT)

    assert "error" in out
    assert attempt_count[0] == 1  # only one attempt
    assert sleep_calls == []      # no sleeps


# ---------------------------------------------------------------------------
# social_search: caching and backoff
# ---------------------------------------------------------------------------

async def test_hn_search_caches_successful_result(monkeypatch):
    import httpx
    import meridian.social_search as ss

    QUERY = "caching-test-hn-2e51a41a"
    LIMIT = 3
    ss._CACHE.pop((QUERY, LIMIT), None)

    call_count = [0]

    class _CountingResp:
        def raise_for_status(self): pass
        def json(self): return {"hits": []}

    class _CountingClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None, headers=None):
            call_count[0] += 1
            return _CountingResp()

    monkeypatch.setattr(httpx, "AsyncClient", _CountingClient)
    out1 = await ss.hn_search(QUERY, limit=LIMIT)
    out2 = await ss.hn_search(QUERY, limit=LIMIT)

    assert out1 == out2
    assert call_count[0] == 1


async def test_hn_search_retries_on_503(monkeypatch):
    """A 503 response must trigger retry with the correct sleep delays."""
    import httpx
    import meridian.social_search as ss

    QUERY = "backoff-503-hn-2e51a41a"
    LIMIT = 2
    ss._CACHE.pop((QUERY, LIMIT), None)

    sleep_calls: list[float] = []

    async def _mock_sleep(t: float) -> None:
        sleep_calls.append(t)

    monkeypatch.setattr(asyncio, "sleep", _mock_sleep)

    attempt_count = [0]

    class _503ThenOkResp:
        def raise_for_status(self): pass
        def json(self): return {"hits": []}

    class _503ThenOkClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def get(self, url, params=None, headers=None):
            attempt_count[0] += 1
            if attempt_count[0] < 2:
                raise httpx.HTTPStatusError(
                    "service unavailable", request=None, response=_ErrorResp(503)
                )
            return _503ThenOkResp()

    monkeypatch.setattr(httpx, "AsyncClient", _503ThenOkClient)
    out = await ss.hn_search(QUERY, limit=LIMIT)

    assert "results" in out
    assert attempt_count[0] == 2
    assert sleep_calls == [ss._RETRY_DELAYS[0]]  # one sleep before attempt 2


async def test_hn_search_errors_are_not_cached(monkeypatch):
    import httpx
    import meridian.social_search as ss

    QUERY = "hn-error-not-cached-2e51a41a"
    LIMIT = 2
    ss._CACHE.pop((QUERY, LIMIT), None)

    call_count = [0]

    class _RaisingClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None, headers=None):
            call_count[0] += 1
            raise RuntimeError("connection reset")

    monkeypatch.setattr(httpx, "AsyncClient", _RaisingClient)
    await ss.hn_search(QUERY, limit=LIMIT)
    await ss.hn_search(QUERY, limit=LIMIT)
    assert call_count[0] == 2
