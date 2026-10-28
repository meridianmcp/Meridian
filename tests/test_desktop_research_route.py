"""GET /desktop/research/search: the Desktop Search tab's read-only research route.

Pins the route contract: loopback-only, sources normalized to one shape, a failing or
unknown source reported on its own, q validated (422), limit capped at 25, and no raw
exception text, token or credential-bearing URL in any response.

Every outbound search call is replaced with a recording fake (monkeypatch on the module
attributes the route reads at call time), so CI never reaches OpenAlex, Semantic
Scholar, arXiv or GitHub.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import meridian.server as server_module
from meridian import github_search, paper_search
from meridian.routes import desktop_research as route

PATH = "/desktop/research/search"
LOOPBACK = ("127.0.0.1", 50000)
FAKE_SECRET = "TESTKEY-desktop-research-0001"
ALL_SOURCES = ("openalex", "semantic_scholar", "arxiv", "github_repos", "github_code")
RESULT_KEYS = {"id", "title", "authors", "year", "url", "source", "abstract"}

# source name -> (module, attribute) the route calls at request time
SEARCH_FUNCTIONS = {
    "openalex": (paper_search, "openalex_search"),
    "semantic_scholar": (paper_search, "semantic_scholar_search"),
    "arxiv": (paper_search, "arxiv_search"),
    "github_repos": (github_search, "github_repo_search"),
    "github_code": (github_search, "github_code_search"),
}


@pytest.fixture(autouse=True)
def _self_hosted(monkeypatch):
    monkeypatch.delenv("MERIDIAN_HOSTED", raising=False)
    monkeypatch.delenv("SITE_PASSWORD", raising=False)


@pytest.fixture
def client():
    return TestClient(server_module.app, client=LOOPBACK, base_url="http://localhost")


@pytest.fixture
def upstream(monkeypatch):
    """Patch every search function. Returns (answers, calls).

    ``answers[source]`` is the dict the fake returns, or an exception it raises. Each call
    is recorded as ``(source, query, limit)`` in ``calls``.
    """
    answers: dict[str, object] = {}
    calls: list[tuple[str, str, int]] = []

    def make_fake(source: str):
        async def fake(query, limit=10, sort_by="relevance"):
            calls.append((source, query, limit))
            answer = answers.get(source, {"query": query, "count": 0, "results": []})
            if isinstance(answer, BaseException):
                raise answer
            return answer

        return fake

    for source, (module, attribute) in SEARCH_FUNCTIONS.items():
        monkeypatch.setattr(module, attribute, make_fake(source))
    return answers, calls


def _get(client, **params):
    return client.get(PATH, params=params)


def _all_text(response) -> str:
    return response.text


# ---------------------------------------------------------------------------
# Normalization to the contract shape
# ---------------------------------------------------------------------------

def test_each_source_is_normalized_to_the_contract(client, upstream):
    answers, _ = upstream
    answers["openalex"] = {"results": [{
        "openalex_id": "W123",
        "title": "  Attention   is all  you need ",
        "authors": ["A. Author", "", "B. Author"],
        "summary": "x" * 500,
        "published": "2017-06-12",
        "url": "http://openalex.org/landing?x=1",
        "pdf_url": "",
        "doi": "https://doi.org/10.1/abc",
    }]}
    answers["semantic_scholar"] = {"results": [{
        "s2_id": "abc",
        "title": "S2 paper",
        "authors": ["C"],
        "summary": "short",
        "published": "2019",
        "url": "https://www.semanticscholar.org/paper/abc",
        "doi": "",
    }]}
    answers["arxiv"] = {"results": [{
        "arxiv_id": "2401.01234v1",
        "title": "Arxiv paper",
        "authors": ["D"],
        "summary": "s",
        "published": "2024-01-05T18:59:00Z",
        "url": "http://arxiv.org/abs/2401.01234v1",
        "pdf_url": "https://arxiv.org/pdf/2401.01234v1",
    }]}
    answers["github_repos"] = {"results": [{
        "title": "owner/repo",
        "repo": "owner/repo",
        "authors": ["owner"],
        "summary": "A   desc",
        "published": "2020-02-02T00:00:00Z",
        "url": "https://github.com/owner/repo",
        "stars": 5,
    }]}
    answers["github_code"] = {"results": [{
        "path": "src/a.py",
        "title": "src/a.py",
        "repo": "owner/repo",
        "authors": ["owner"],
        "url": "https://github.com/owner/repo/blob/main/src/a.py",
        "sha": "abc123",
    }]}

    response = _get(client, q="  attention  ", sources=",".join(ALL_SOURCES))

    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "attention"
    assert list(body["sources"]) == list(ALL_SOURCES)
    for name in ALL_SOURCES:
        answer = body["sources"][name]
        assert answer["ok"] is True
        assert answer["reason"] == ""
        assert len(answer["results"]) == 1
        for row in answer["results"]:
            assert set(row) == RESULT_KEYS
            assert row["source"] == name
            assert row["year"] is None or isinstance(row["year"], int)
            assert row["url"].startswith("https://")
            assert isinstance(row["authors"], list)
            assert all(isinstance(author, str) and author for author in row["authors"])
            assert len(row["abstract"]) <= 400

    openalex = body["sources"]["openalex"]["results"][0]
    assert openalex["id"] == "W123"
    assert openalex["title"] == "Attention is all you need"
    assert openalex["authors"] == ["A. Author", "B. Author"]
    assert openalex["year"] == 2017
    assert openalex["url"] == "https://openalex.org/landing?x=1"
    assert len(openalex["abstract"]) == 400 and openalex["abstract"].endswith("...")

    s2 = body["sources"]["semantic_scholar"]["results"][0]
    assert (s2["id"], s2["year"], s2["abstract"]) == ("abc", 2019, "short")

    arxiv = body["sources"]["arxiv"]["results"][0]
    assert arxiv["id"] == "2401.01234v1"
    assert arxiv["url"] == "https://arxiv.org/abs/2401.01234v1"
    assert arxiv["year"] == 2024

    repo = body["sources"]["github_repos"]["results"][0]
    assert (repo["id"], repo["title"], repo["year"]) == ("owner/repo", "owner/repo", 2020)
    assert repo["abstract"] == "A desc"

    code = body["sources"]["github_code"]["results"][0]
    assert code["id"] == "https://github.com/owner/repo/blob/main/src/a.py"
    assert code["title"] == "owner/repo: src/a.py"
    assert code["year"] is None and code["abstract"] == ""


def test_malformed_rows_are_skipped_and_limit_truncates(client, upstream):
    answers, _ = upstream
    rows = [None, "not a row", {}] + [{"arxiv_id": f"2401.0000{i}", "title": f"T{i}"}
                                      for i in range(30)]
    answers["arxiv"] = {"results": rows}

    body = _get(client, q="x", sources="arxiv", limit=5).json()

    results = body["sources"]["arxiv"]["results"]
    assert body["sources"]["arxiv"]["ok"] is True
    assert [row["title"] for row in results] == ["T0", "T1", "T2", "T3", "T4"]


def test_arxiv_fallback_is_ok_with_a_plain_note_and_no_raw_warning(client, upstream):
    answers, _ = upstream
    answers["arxiv"] = {
        "query": "q",
        "count": 1,
        "results": [{"arxiv_id": "2401.01234v1", "title": "Fallback paper", "authors": [],
                     "summary": "", "published": "2024", "url": "http://arxiv.org/abs/x"}],
        "fallback_source": "openalex",
        "warning": (
            "arXiv was unreachable from this server (HTTP 406) "
            f"https://api.example.test/works?api_key={FAKE_SECRET}"
        ),
        "sources_tried": ["arxiv", "openalex"],
    }

    response = _get(client, q="q", sources="arxiv")

    answer = response.json()["sources"]["arxiv"]
    assert answer["ok"] is True
    assert "OpenAlex" in answer["reason"]
    assert len(answer["results"]) == 1
    assert FAKE_SECRET not in _all_text(response)
    assert "warning" not in _all_text(response)


# ---------------------------------------------------------------------------
# Source selection
# ---------------------------------------------------------------------------

def test_default_sources_when_sources_is_omitted(client, upstream):
    _, calls = upstream

    body = _get(client, q="protein folding").json()

    assert list(body["sources"]) == ["openalex", "semantic_scholar", "arxiv", "github_repos"]
    assert "github_code" not in {source for source, _, _ in calls}


def test_empty_sources_value_means_the_default_list(client, upstream):
    body = _get(client, q="protein folding", sources="  ,  ").json()
    assert list(body["sources"]) == ["openalex", "semantic_scholar", "arxiv", "github_repos"]


def test_unknown_source_is_reported_and_never_called(client, upstream):
    _, calls = upstream

    body = _get(client, q="q", sources="openalex,nope").json()

    assert body["sources"]["nope"] == {"ok": False, "reason": "unknown source", "results": []}
    assert body["sources"]["openalex"]["ok"] is True
    assert {source for source, _, _ in calls} == {"openalex"}


def test_source_names_are_case_insensitive_and_deduplicated(client, upstream):
    body = _get(client, q="q", sources="OpenAlex, openalex ,,arxiv").json()
    assert list(body["sources"]) == ["openalex", "arxiv"]


# ---------------------------------------------------------------------------
# Failure isolation and plain reasons
# ---------------------------------------------------------------------------

def test_a_raising_source_does_not_stop_the_others(client, upstream):
    answers, _ = upstream
    answers["openalex"] = RuntimeError(
        f"boom https://api.openalex.org/works?api_key={FAKE_SECRET} Bearer {FAKE_SECRET}"
    )
    answers["arxiv"] = {"results": [{"arxiv_id": "2401.01234v1", "title": "Still here"}]}

    response = _get(client, q="q", sources="openalex,arxiv")

    assert response.status_code == 200
    body = response.json()
    assert body["sources"]["openalex"] == {
        "ok": False,
        "reason": "OpenAlex search failed; the source did not answer",
        "results": [],
    }
    assert body["sources"]["arxiv"]["ok"] is True
    assert body["sources"]["arxiv"]["results"][0]["title"] == "Still here"
    assert "boom" not in _all_text(response)
    assert FAKE_SECRET not in _all_text(response)


def test_upstream_error_text_becomes_a_plain_reason(client, upstream):
    answers, _ = upstream
    answers["github_repos"] = {
        "error": (
            "github repo search failed: Client error '403 rate limit exceeded' for url "
            f"'https://api.github.com/search/repositories?q=x&token={FAKE_SECRET}'"
        ),
        "query": "x",
    }
    answers["semantic_scholar"] = {
        "error": "semantic scholar search failed: Client error '429 Too Many Requests' "
                 "for url 'https://api.semanticscholar.org/graph/v1/paper/search?query=x'",
        "query": "x",
    }
    answers["openalex"] = {"error": "openalex search failed: timed out", "query": "x"}

    response = _get(client, q="x", sources="github_repos,semantic_scholar,openalex")
    body = response.json()

    assert body["sources"]["github_repos"] == {
        "ok": False,
        "reason": "GitHub rate limit reached for public search; try again in a minute",
        "results": [],
    }
    assert body["sources"]["semantic_scholar"]["reason"] == (
        "Semantic Scholar rate limit reached; try again in a minute"
    )
    assert body["sources"]["openalex"]["reason"] == (
        "OpenAlex did not answer in time; try again shortly"
    )
    for name in ("github_repos", "semantic_scholar", "openalex"):
        assert body["sources"][name]["ok"] is False
    assert "http" not in json.dumps(
        [body["sources"][n]["reason"] for n in ("github_repos", "semantic_scholar")]
    )
    assert FAKE_SECRET not in _all_text(response)


def test_a_source_that_outlasts_the_time_limit_is_reported_as_timed_out(
    client, upstream, monkeypatch,
):
    answers, _ = upstream
    monkeypatch.setattr(route, "SOURCE_TIMEOUT_SECONDS", 0.05)

    async def slow(query, limit=10, sort_by="relevance"):
        await asyncio.sleep(2)
        return {"results": []}

    monkeypatch.setattr(paper_search, "openalex_search", slow)
    answers["arxiv"] = {"results": [{"arxiv_id": "2401.01234v1", "title": "Quick"}]}

    body = _get(client, q="q", sources="openalex,arxiv").json()

    assert body["sources"]["openalex"] == {
        "ok": False,
        "reason": "OpenAlex did not answer in time; try again shortly",
        "results": [],
    }
    assert body["sources"]["arxiv"]["ok"] is True


def test_github_sources_run_without_any_token_configured(client, upstream, monkeypatch):
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT"):
        monkeypatch.delenv(name, raising=False)
    answers, _ = upstream
    answers["github_repos"] = {"results": [{"title": "a/b", "repo": "a/b"}]}

    body = _get(client, q="q", sources="github_repos").json()

    assert body["sources"]["github_repos"]["ok"] is True
    assert body["sources"]["github_repos"]["results"][0]["id"] == "a/b"


# ---------------------------------------------------------------------------
# q and limit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("q", [None, "", "   "])
def test_missing_or_blank_q_is_422(client, upstream, q):
    _, calls = upstream
    params = {} if q is None else {"q": q}

    response = client.get(PATH, params=params)

    assert response.status_code == 422
    assert response.json() == {"detail": "q is required: 1 to 300 characters."}
    assert calls == []


def test_q_longer_than_300_characters_is_422_and_calls_nothing(client, upstream):
    _, calls = upstream

    response = _get(client, q="a" * 301)

    assert response.status_code == 422
    assert response.json() == {"detail": "q is required: 1 to 300 characters."}
    assert calls == []


def test_q_of_exactly_300_characters_is_accepted(client, upstream):
    response = _get(client, q="a" * 300, sources="openalex")
    assert response.status_code == 200


def test_limit_is_capped_at_25_per_source(client, upstream):
    _, calls = upstream

    _get(client, q="q", sources="openalex", limit=100)
    _get(client, q="q", sources="openalex")
    _get(client, q="q", sources="openalex", limit=0)

    assert [limit for _, _, limit in calls] == [25, 10, 1]


# ---------------------------------------------------------------------------
# No secrets, no raw credentials in any response
# ---------------------------------------------------------------------------

def test_no_response_carries_a_key_or_a_credential_url(client, upstream, monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", FAKE_SECRET)
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_SECRET)
    answers, _ = upstream
    answers["openalex"] = {"results": [
        {"openalex_id": "W1", "title": "Keyed link",
         "url": f"https://example.test/p?api_key={FAKE_SECRET}&x=1"},
        {"openalex_id": "W2", "title": "Userinfo link",
         "url": f"https://user:{FAKE_SECRET}@example.test/p"},
        {"openalex_id": "W3", "title": "Clean link", "url": "https://example.test/p?x=1"},
    ]}

    response = _get(client, q="q", sources="openalex,github_repos")
    body = response.json()

    rows = {row["id"]: row for row in body["sources"]["openalex"]["results"]}
    assert rows["W1"]["url"] == ""
    assert rows["W2"]["url"] == ""
    assert rows["W3"]["url"] == "https://example.test/p?x=1"
    text = _all_text(response)
    assert FAKE_SECRET not in text
    assert "api_key" not in text
    assert "user:" not in text


# ---------------------------------------------------------------------------
# Loopback only
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("client_address", [
    ("203.0.113.9", 40000),
    ("10.0.0.5", 40000),
    ("testclient", 50000),
])
def test_non_loopback_callers_are_refused_before_any_search(upstream, client_address):
    answers, calls = upstream
    outsider = TestClient(server_module.app, client=client_address, base_url="http://localhost")

    response = outsider.get(PATH, params={"q": "q", "sources": "openalex"})

    assert response.status_code == 403
    assert calls == []


def test_a_forwarded_request_is_refused_even_from_loopback(upstream):
    _, calls = upstream
    proxied = TestClient(server_module.app, client=LOOPBACK, base_url="http://localhost")

    response = proxied.get(
        PATH, params={"q": "q", "sources": "openalex"},
        headers={"X-Forwarded-For": "203.0.113.9"},
    )

    assert response.status_code == 403
    assert calls == []


@pytest.mark.parametrize("host_header", ["evil.example", "localhost.evil.example"])
def test_a_rebinding_host_header_is_refused_before_any_search(upstream, host_header):
    """D1 (review B2): a DNS-rebinding page reaches the server from loopback but sends its own Host."""
    _, calls = upstream
    rebound = TestClient(server_module.app, client=LOOPBACK, base_url="http://localhost")

    response = rebound.get(
        PATH, params={"q": "q", "sources": "openalex"}, headers={"Host": host_header},
    )

    assert response.status_code == 403
    assert calls == []


@pytest.mark.parametrize("origin", ["https://evil.example", "null"])
def test_a_foreign_origin_is_refused_before_any_search(upstream, origin):
    _, calls = upstream
    cross_origin = TestClient(server_module.app, client=LOOPBACK, base_url="http://localhost")

    response = cross_origin.get(PATH, params={"q": "q", "sources": "openalex"}, headers={"Origin": origin})

    assert response.status_code == 403
    assert calls == []


def test_a_loopback_request_with_a_localhost_host_is_served(upstream):
    """D2 (spec 9.1): the existing loopback path still answers once the client names localhost."""
    _, calls = upstream
    local_client = TestClient(server_module.app, client=LOOPBACK, base_url="http://localhost")

    response = local_client.get(PATH, params={"q": "q", "sources": "openalex"})

    assert response.status_code == 200
    assert calls


@pytest.mark.parametrize("host", ["127.0.0.1", "127.8.9.10", "::1", "::ffff:127.0.0.1"])
def test_loopback_addresses_are_accepted(upstream, host):
    client_for_host = TestClient(server_module.app, client=(host, 50000), base_url="http://localhost")
    response = client_for_host.get(PATH, params={"q": "q", "sources": "openalex"})
    assert response.status_code == 200


@pytest.mark.parametrize("host", ["", "localhost", "192.168.1.20", "fe80::1"])
def test_non_loopback_host_strings_are_not_loopback(host):
    assert route._is_loopback_host(host) is False
