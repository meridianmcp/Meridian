"""Research and experiment tool fixes (branch fix/research-tools).

1. arXiv multi-word queries are sent as an exact phrase; ``raw_query=True`` keeps the
   unquoted form.
2. GitHub search reads an optional server-side ``GITHUB_TOKEN``. Without it the call
   returns ``GitHub token not configured`` and makes no request. With it, the token is
   sent only as a Bearer header and never appears in the output.
3. ``list_research_runs`` and ``list_experiments`` return an explicit error for an
   unknown project id, and an empty list with a ``hint`` for an empty project.
4. ``search_all`` caps each hit's body text and keeps the snippet for the match.
5. A research finding captured with ``capture_research_finding`` reads back through
   ``get_project_notes(tag='finding')``, the documented read path. ``get_findings``
   reads ``session_findings`` only.

Network calls are stubbed. Database tests use a throwaway in-memory SQLite database.
"""
from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio

import meridian.server  # noqa: F401 -- must be imported before handlers to avoid cycle
from meridian import db as db_module
from meridian import github_search as gs
from meridian import paper_search as ps
from meridian.mcp.handlers import experiment_tools as et_mod
from meridian.mcp.handlers import notes_decisions as nd_mod
from meridian.mcp.handlers import research_tools as rt_mod

_DATA_DIR = "/tmp/meridian-test"


@pytest_asyncio.fixture
async def db():
    conn = await db_module.init_db(":memory:")
    yield conn
    await conn.close()


# ---------------------------------------------------------------------------
# 1. arXiv phrase quoting
# ---------------------------------------------------------------------------

class _Text:
    def __init__(self, text: str) -> None:
        self.text = text


_EMPTY_ATOM = '<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'


@pytest.fixture
def arxiv_calls(monkeypatch):
    calls: list[dict] = []

    async def fake_fetch(http, url, params, headers, **kwargs):
        calls.append({"url": url, "params": dict(params)})
        return _Text(_EMPTY_ATOM)

    async def no_fallback(*args, **kwargs):
        raise AssertionError("arXiv fallback must not run when arXiv answers")

    monkeypatch.setattr(ps, "_fetch_with_backoff", fake_fetch)
    monkeypatch.setattr(ps, "_arxiv_fallback_search", no_fallback)
    return calls


@pytest.mark.asyncio
async def test_arxiv_multiword_query_is_sent_as_phrase(arxiv_calls):
    out = await ps.arxiv_search("graph neural networks", limit=3)
    assert arxiv_calls[0]["params"]["search_query"] == 'all:"graph neural networks"'
    # The caller still sees the query as typed.
    assert out["query"] == "graph neural networks"


@pytest.mark.asyncio
async def test_arxiv_single_word_query_is_unquoted(arxiv_calls):
    await ps.arxiv_search("transformers", limit=3)
    assert arxiv_calls[0]["params"]["search_query"] == "all:transformers"


@pytest.mark.asyncio
async def test_arxiv_raw_query_keeps_unquoted_form(arxiv_calls):
    await ps.arxiv_search("graph neural networks", limit=3, raw_query=True)
    assert arxiv_calls[0]["params"]["search_query"] == "all:graph neural networks"


@pytest.mark.asyncio
async def test_arxiv_caller_quoted_phrase_is_not_double_quoted(arxiv_calls):
    await ps.arxiv_search('"graph neural"', limit=3)
    assert arxiv_calls[0]["params"]["search_query"] == 'all:"graph neural"'


# ---------------------------------------------------------------------------
# 2. GitHub token
# ---------------------------------------------------------------------------

_TOKEN = "ghp_test_fixture_token_123"


class _JsonResponse:
    def __init__(self, payload) -> None:
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def github_calls(monkeypatch):
    calls: list[dict] = []
    gs._CACHE.clear()

    async def fake_fetch(http, url, params=None, headers=None):
        calls.append({"url": url, "headers": dict(headers or {})})
        return _JsonResponse({"items": [{
            "path": "a.py",
            "repository": {"full_name": "octo/repo"},
            "html_url": "https://github.com/octo/repo/blob/main/a.py",
            "sha": "abc123",
            "score": 1.0,
        }]})

    monkeypatch.setattr(gs, "_fetch_with_backoff", fake_fetch)
    return calls


@pytest.mark.asyncio
async def test_github_without_token_returns_explicit_error_and_no_request(
        monkeypatch, github_calls):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    code = await gs.github_code_search("token-missing-code")
    repo = await gs.github_repo_search("token-missing-repo")
    for out in (code, repo):
        assert out["error"] == "GitHub token not configured"
        assert "GITHUB_TOKEN" in out["hint"]
    assert github_calls == [], "no GitHub request may be made without a token"


@pytest.mark.asyncio
async def test_github_with_token_sends_bearer_header_and_never_echoes_it(
        monkeypatch, github_calls):
    monkeypatch.setenv("GITHUB_TOKEN", _TOKEN)
    out = await gs.github_code_search("token-present-code")
    assert github_calls[0]["headers"]["Authorization"] == f"Bearer {_TOKEN}"
    assert out["count"] == 1
    assert _TOKEN not in json.dumps(out)


@pytest.mark.asyncio
async def test_github_401_with_token_is_a_clear_error_without_the_token(
        monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", _TOKEN)
    gs._CACHE.clear()
    request = httpx.Request("GET", gs._GITHUB_CODE_API)

    async def rejected(http, url, params=None, headers=None):
        raise httpx.HTTPStatusError(
            "401 Unauthorized", request=request,
            response=httpx.Response(401, request=request))

    monkeypatch.setattr(gs, "_fetch_with_backoff", rejected)
    out = await gs.github_code_search("token-rejected-code")
    assert "401" in out["error"]
    assert "not configured" not in out["error"]
    assert _TOKEN not in json.dumps(out)


# ---------------------------------------------------------------------------
# 3. Empty list vs unknown project (research runs, experiments)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_research_runs_unknown_project_is_explicit_error(db):
    out = await rt_mod.handle_list_research_runs(
        {"project_id": "no-such-project"}, db, _DATA_DIR, None, None)
    assert out == {"error": "project not found", "project_id": "no-such-project"}


@pytest.mark.asyncio
async def test_list_research_runs_empty_project_returns_hint(db):
    p = await db_module.create_project(db, "rr-empty-project")
    out = await rt_mod.handle_list_research_runs(
        {"project_id": p["id"]}, db, _DATA_DIR, None, None)
    assert out["runs"] == []
    assert out["count"] == 0
    assert "Nothing has been captured yet" in out["hint"]


@pytest.mark.asyncio
async def test_list_experiments_unknown_project_is_explicit_error(db):
    out = await et_mod.handle_list_experiments(
        {"project_id": "no-such-project"}, db, _DATA_DIR, None, None)
    assert out == {"error": "project not found", "project_id": "no-such-project"}


@pytest.mark.asyncio
async def test_list_experiments_empty_project_returns_hint(db):
    p = await db_module.create_project(db, "exp-empty-project")
    out = await et_mod.handle_list_experiments(
        {"project_id": p["id"]}, db, _DATA_DIR, None, None)
    assert out["experiments"] == []
    assert out["count"] == 0
    assert "Nothing has been captured yet" in out["hint"]


# ---------------------------------------------------------------------------
# 4. search_all body cap
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_all_caps_long_body_and_keeps_snippet(db):
    p = await db_module.create_project(db, "sa-body-cap")
    long_body = "filler text " * 200 + "needle-token closing words"
    await db_module.add_project_note(db, p["id"], "Long note", long_body)
    await db_module.add_project_note(db, p["id"], "Short note", "needle-token brief")

    result = await db_module.search_all(db, p["id"], "needle-token")
    by_title = {n["title"]: n for n in result["notes"]}

    long_hit = by_title["Long note"]
    assert long_hit["body_truncated"] is True
    assert len(long_hit["body"]) <= db_module._SEARCH_ALL_BODY_CAP + 3
    assert long_hit["body"] != long_body
    # The snippet is built from the full text, so the match still shows.
    assert "needle-token" in long_hit["snippet"]

    short_hit = by_title["Short note"]
    assert short_hit["body_truncated"] is False
    assert short_hit["body"] == "needle-token brief"


# ---------------------------------------------------------------------------
# 5. Findings: write path and documented read path agree
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_capture_then_read_back_through_documented_path(db):
    p = await db_module.create_project(db, "findings-roundtrip")
    cap = await nd_mod.handle_capture_research_finding(
        {"project_id": p["id"], "url": "https://example.org/paper-x",
         "summary": "Method X beats baseline Y on task Z"},
        db, _DATA_DIR, None, None)
    assert "error" not in cap

    rows = await db_module.get_project_notes(db, p["id"], tag="finding", bodies=True)
    assert any(
        "Method X beats baseline Y on task Z" in ((r.get("body") or "") + (r.get("title") or ""))
        for r in rows
    ), "a captured finding must read back through get_project_notes(tag='finding')"


@pytest.mark.asyncio
async def test_get_findings_reads_session_findings_only(db):
    p = await db_module.create_project(db, "findings-split")
    await nd_mod.handle_capture_research_finding(
        {"project_id": p["id"], "url": "https://example.org/paper-y",
         "summary": "Captured research finding"},
        db, _DATA_DIR, None, None)
    # Captured findings are notes, so get_findings (session_findings) is empty here.
    assert await db_module.get_findings(db, p["id"]) == []
    await db_module.store_finding(db, p["id"], "per-task intermediate result")
    rows = await db_module.get_findings(db, p["id"])
    assert [r["content"] for r in rows] == ["per-task intermediate result"]
