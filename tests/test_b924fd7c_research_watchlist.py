"""Tests for sprint item b924fd7c — recurring research watchlist on top of
paper_search/github_search/social_search.

Covers the scoped subset actually shipped:
- save_watchlist_query / list_watchlist_queries / delete_watchlist_query
  (project-notes-backed saved queries, no new table).
- run_watchlist_query: re-run + diff-against-prior-findings + auto-capture via
  the EXISTING save_finding path, tagged for the next diff.
- Per-source identity-key extraction (arxiv_id/openalex_id/s2_id/pmid/sha/
  repo/hn_id).
- MCP registration: tool schemas, read-only classification, category/
  role_relevance tags, and dispatch-table wiring in handler.py.
- agent_defaults.py: standard version bump + RESEARCH ROUTING PROTOCOL
  mentions the new mechanism and the previously-unmentioned github_search/
  social_search tools.

Deliberately deferred (see meridian/mcp/handlers/research_watchlist.py's own
module docstring + AGENTS.md's new "Research watchlists" section): scheduled/
recurring execution (a host-level CronCreate/schedule-skill pairing, not new
Meridian server code) and a cross-project aggregated view.
"""
from __future__ import annotations

import json

import pytest
import pytest_asyncio

import meridian.server  # noqa: F401 — must be imported before handler to avoid cycle
from meridian.mcp import handler as mh
from meridian.mcp.handlers import research_watchlist as rw_mod
from meridian import db as db_module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db():
    conn = await db_module.init_db(":memory:")
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def project(db):
    return await db_module.create_project(db, "test-proj-watchlist")


_DATA_DIR = "/tmp/meridian-test"


async def _save(db, project_id, **kw):
    args = {"project_id": project_id, "source_type": "arxiv", "query": "transformers"}
    args.update(kw)
    return await rw_mod.handle_save_watchlist_query(args, db, _DATA_DIR, None, None)


# ---------------------------------------------------------------------------
# save_watchlist_query
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_watchlist_query_success(db, project):
    out = await _save(db, project["id"], name="Transformer papers")
    assert "error" not in out
    assert out["source_type"] == "arxiv"
    assert out["query"] == "transformers"
    assert out["limit"] == 10
    assert out["sort_by"] == "relevance"
    assert out["watchlist_id"] == out["note"]["id"]
    assert "research_watchlist" in out["note"]["tags"]
    assert "arxiv" in out["note"]["tags"]


@pytest.mark.asyncio
async def test_save_watchlist_query_requires_project_id():
    out = await rw_mod.handle_save_watchlist_query(
        {"source_type": "arxiv", "query": "x"}, None, _DATA_DIR, None, None,
    )
    assert "error" in out
    assert "project_id" in out["error"]


@pytest.mark.asyncio
async def test_save_watchlist_query_invalid_source_type(db, project):
    out = await rw_mod.handle_save_watchlist_query(
        {"project_id": project["id"], "source_type": "twitter", "query": "x"},
        db, _DATA_DIR, None, None,
    )
    assert "error" in out
    assert "source_type" in out["error"]


@pytest.mark.asyncio
async def test_save_watchlist_query_empty_query(db, project):
    out = await rw_mod.handle_save_watchlist_query(
        {"project_id": project["id"], "source_type": "arxiv", "query": "   "},
        db, _DATA_DIR, None, None,
    )
    assert "error" in out
    assert "non-empty" in out["error"]


@pytest.mark.asyncio
async def test_save_watchlist_query_clamps_limit_and_sort_by(db, project):
    out = await _save(db, project["id"], limit=999, sort_by="bogus")
    assert out["limit"] == 50
    assert out["sort_by"] == "relevance"


# ---------------------------------------------------------------------------
# list_watchlist_queries
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_watchlist_queries_empty(db, project):
    out = await rw_mod.handle_list_watchlist_queries(
        {"project_id": project["id"]}, db, _DATA_DIR, None, None,
    )
    assert out == {"count": 0, "watchlists": []}


@pytest.mark.asyncio
async def test_list_watchlist_queries_lists_and_filters(db, project):
    pid = project["id"]
    await _save(db, pid, query="arxiv q")
    await _save(db, pid, source_type="hn", query="hn q")

    all_out = await rw_mod.handle_list_watchlist_queries(
        {"project_id": pid}, db, _DATA_DIR, None, None,
    )
    assert all_out["count"] == 2

    filtered = await rw_mod.handle_list_watchlist_queries(
        {"project_id": pid, "source_type": "hn"}, db, _DATA_DIR, None, None,
    )
    assert filtered["count"] == 1
    assert filtered["watchlists"][0]["query"] == "hn q"


@pytest.mark.asyncio
async def test_list_watchlist_queries_requires_project_id():
    out = await rw_mod.handle_list_watchlist_queries(
        {}, None, _DATA_DIR, None, None,
    )
    assert "error" in out


# ---------------------------------------------------------------------------
# delete_watchlist_query
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_watchlist_query_success(db, project):
    pid = project["id"]
    saved = await _save(db, pid)
    out = await rw_mod.handle_delete_watchlist_query(
        {"project_id": pid, "watchlist_id": saved["watchlist_id"]},
        db, _DATA_DIR, None, None,
    )
    assert out == {"deleted": True}
    listing = await rw_mod.handle_list_watchlist_queries(
        {"project_id": pid}, db, _DATA_DIR, None, None,
    )
    assert listing["count"] == 0


@pytest.mark.asyncio
async def test_delete_watchlist_query_wrong_project_not_found(db, project):
    pid = project["id"]
    other = await db_module.create_project(db, "other-proj")
    saved = await _save(db, pid)
    out = await rw_mod.handle_delete_watchlist_query(
        {"project_id": other["id"], "watchlist_id": saved["watchlist_id"]},
        db, _DATA_DIR, None, None,
    )
    assert "error" in out
    assert "not found" in out["error"]


@pytest.mark.asyncio
async def test_delete_watchlist_query_missing_id_not_found(db, project):
    out = await rw_mod.handle_delete_watchlist_query(
        {"project_id": project["id"], "watchlist_id": "does-not-exist"},
        db, _DATA_DIR, None, None,
    )
    assert "error" in out


@pytest.mark.asyncio
async def test_delete_watchlist_query_refuses_a_non_watchlist_note(db, project):
    """A plain note (no research_watchlist tag) must never be deletable through
    this tool even if the id happens to be known/guessed."""
    pid = project["id"]
    note = await db_module.add_project_note(db, pid, "Plain note", "body", tags="misc")
    out = await rw_mod.handle_delete_watchlist_query(
        {"project_id": pid, "watchlist_id": note["id"]}, db, _DATA_DIR, None, None,
    )
    assert "error" in out
    still_there = await db_module.get_project_note(db, note["id"])
    assert still_there is not None


# ---------------------------------------------------------------------------
# run_watchlist_query
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_watchlist_query_not_found(db, project):
    out = await rw_mod.handle_run_watchlist_query(
        {"project_id": project["id"], "watchlist_id": "nope"}, db, _DATA_DIR, None, None,
    )
    assert "error" in out
    assert "not found" in out["error"]


@pytest.mark.asyncio
async def test_run_watchlist_query_unsupported_source_type_defends_corrupted_note(db, project):
    """Even if a note's body was corrupted/hand-edited to an unsupported
    source_type, run_watchlist_query degrades to {error} rather than raising."""
    pid = project["id"]
    note = await db_module.add_project_note(
        db, pid, "Watchlist: bad",
        json.dumps({"source_type": "twitter", "query": "x", "limit": 10, "sort_by": "relevance"}),
        tags="research_watchlist,twitter", kind="reference",
    )
    out = await rw_mod.handle_run_watchlist_query(
        {"project_id": pid, "watchlist_id": note["id"]}, db, _DATA_DIR, None, None,
    )
    assert "error" in out
    assert "unsupported" in out["error"]


@pytest.mark.asyncio
async def test_run_watchlist_query_search_error_degrades(db, project, monkeypatch):
    pid = project["id"]
    saved = await _save(db, pid, source_type="arxiv", query="q")

    async def _fake_arxiv_search(query, limit=10, sort_by="relevance"):
        return {"error": "arxiv search failed: boom", "query": query}

    import meridian.paper_search as ps
    monkeypatch.setattr(ps, "arxiv_search", _fake_arxiv_search)

    out = await rw_mod.handle_run_watchlist_query(
        {"project_id": pid, "watchlist_id": saved["watchlist_id"]}, db, _DATA_DIR, None, None,
    )
    assert "error" in out
    assert "boom" in out["error"]
    # Nothing should have been captured.
    findings = await db_module.get_project_notes(db, pid, tag="finding")
    assert findings == []


@pytest.mark.asyncio
async def test_run_watchlist_query_first_run_captures_all_as_new(db, project, monkeypatch):
    pid = project["id"]
    saved = await _save(db, pid, source_type="arxiv", query="q")

    async def _fake_arxiv_search(query, limit=10, sort_by="relevance"):
        return {
            "query": query, "count": 2,
            "results": [
                {"arxiv_id": "1111.1111", "title": "Paper A", "summary": "abstract a",
                 "url": "https://arxiv.org/abs/1111.1111"},
                {"arxiv_id": "2222.2222", "title": "Paper B", "summary": "abstract b",
                 "url": "https://arxiv.org/abs/2222.2222"},
            ],
        }

    import meridian.paper_search as ps
    monkeypatch.setattr(ps, "arxiv_search", _fake_arxiv_search)

    out = await rw_mod.handle_run_watchlist_query(
        {"project_id": pid, "watchlist_id": saved["watchlist_id"]}, db, _DATA_DIR, None, None,
    )
    assert out["total_results"] == 2
    assert out["new_count"] == 2
    assert out["already_seen_count"] == 0
    assert len(out["captured"]) == 2

    # Each captured note is a real finding, tagged for the watchlist + item.
    findings = await db_module.get_project_notes(db, pid, tag="finding", bodies=True)
    assert len(findings) == 2
    for f in findings:
        assert "finding" in f["tags"]
        assert "arxiv" in f["tags"]
        assert f"watchlist:{saved['watchlist_id']}" in f["tags"]
        assert "item:arxiv_id:" in f["tags"]


@pytest.mark.asyncio
async def test_run_watchlist_query_second_run_same_results_reports_zero_new(db, project, monkeypatch):
    pid = project["id"]
    saved = await _save(db, pid, source_type="arxiv", query="q")

    async def _fake_arxiv_search(query, limit=10, sort_by="relevance"):
        return {
            "query": query, "count": 1,
            "results": [{"arxiv_id": "1111.1111", "title": "Paper A", "summary": "abstract",
                         "url": "https://arxiv.org/abs/1111.1111"}],
        }

    import meridian.paper_search as ps
    monkeypatch.setattr(ps, "arxiv_search", _fake_arxiv_search)

    first = await rw_mod.handle_run_watchlist_query(
        {"project_id": pid, "watchlist_id": saved["watchlist_id"]}, db, _DATA_DIR, None, None,
    )
    assert first["new_count"] == 1

    second = await rw_mod.handle_run_watchlist_query(
        {"project_id": pid, "watchlist_id": saved["watchlist_id"]}, db, _DATA_DIR, None, None,
    )
    assert second["new_count"] == 0
    assert second["already_seen_count"] == 1
    assert second["captured"] == []

    # Still only ONE finding note exists — the second run captured nothing new.
    findings = await db_module.get_project_notes(db, pid, tag="finding")
    assert len(findings) == 1


@pytest.mark.asyncio
async def test_run_watchlist_query_detects_exactly_the_newly_added_item(db, project, monkeypatch):
    pid = project["id"]
    saved = await _save(db, pid, source_type="arxiv", query="q")

    import meridian.paper_search as ps

    async def _first(query, limit=10, sort_by="relevance"):
        return {"query": query, "count": 1,
                "results": [{"arxiv_id": "1111.1111", "title": "Paper A", "summary": "",
                             "url": "https://arxiv.org/abs/1111.1111"}]}

    monkeypatch.setattr(ps, "arxiv_search", _first)
    await rw_mod.handle_run_watchlist_query(
        {"project_id": pid, "watchlist_id": saved["watchlist_id"]}, db, _DATA_DIR, None, None,
    )

    async def _second(query, limit=10, sort_by="relevance"):
        return {"query": query, "count": 2,
                "results": [
                    {"arxiv_id": "1111.1111", "title": "Paper A", "summary": "",
                     "url": "https://arxiv.org/abs/1111.1111"},
                    {"arxiv_id": "3333.3333", "title": "Paper C (new)", "summary": "",
                     "url": "https://arxiv.org/abs/3333.3333"},
                ]}

    monkeypatch.setattr(ps, "arxiv_search", _second)
    out = await rw_mod.handle_run_watchlist_query(
        {"project_id": pid, "watchlist_id": saved["watchlist_id"]}, db, _DATA_DIR, None, None,
    )
    assert out["new_count"] == 1
    assert out["already_seen_count"] == 1
    assert out["new_results"][0]["arxiv_id"] == "3333.3333"
    assert out["captured"][0]["item_key"] == "arxiv_id:3333.3333"


@pytest.mark.asyncio
async def test_run_watchlist_query_github_code_source(db, project, monkeypatch):
    pid = project["id"]
    saved = await rw_mod.handle_save_watchlist_query(
        {"project_id": pid, "source_type": "github_code", "query": "foo"},
        db, _DATA_DIR, None, None,
    )

    async def _fake_code_search(query, limit=10, sort_by="relevance"):
        return {"query": query, "count": 1,
                "results": [{"path": "a.py", "title": "a.py", "repo": "org/repo",
                             "sha": "deadbeef", "url": "https://github.com/org/repo/blob/x/a.py",
                             "summary": ""}]}

    import meridian.github_search as gs
    monkeypatch.setattr(gs, "github_code_search", _fake_code_search)

    out = await rw_mod.handle_run_watchlist_query(
        {"project_id": pid, "watchlist_id": saved["watchlist_id"]}, db, _DATA_DIR, None, None,
    )
    assert out["new_count"] == 1
    assert out["captured"][0]["item_key"] == "sha:deadbeef"
    findings = await db_module.get_project_notes(db, pid, tag="finding", bodies=True)
    assert "code" in findings[0]["tags"]  # github source_type maps to save_finding's "code"


@pytest.mark.asyncio
async def test_run_watchlist_query_hn_source(db, project, monkeypatch):
    pid = project["id"]
    saved = await rw_mod.handle_save_watchlist_query(
        {"project_id": pid, "source_type": "hn", "query": "rust"}, db, _DATA_DIR, None, None,
    )

    async def _fake_hn_search(query, limit=10, sort_by="relevance"):
        return {"query": query, "count": 1,
                "results": [{"hn_id": "12345", "title": "Rust thing", "summary": "",
                             "url": "https://news.ycombinator.com/item?id=12345"}]}

    import meridian.social_search as ss
    monkeypatch.setattr(ss, "hn_search", _fake_hn_search)

    out = await rw_mod.handle_run_watchlist_query(
        {"project_id": pid, "watchlist_id": saved["watchlist_id"]}, db, _DATA_DIR, None, None,
    )
    assert out["new_count"] == 1
    assert out["captured"][0]["item_key"] == "hn_id:12345"


# ---------------------------------------------------------------------------
# Per-source identity-key extraction (unit-level, no network/db)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "source_type,item,expected",
    [
        ("arxiv", {"arxiv_id": "1234.5678"}, "arxiv_id:1234.5678"),
        ("openalex", {"openalex_id": "W123"}, "openalex_id:W123"),
        ("semantic_scholar", {"s2_id": "abcd"}, "s2_id:abcd"),
        ("pubmed", {"pmid": "999"}, "pmid:999"),
        ("github_code", {"sha": "deadbeef"}, "sha:deadbeef"),
        ("github_repo", {"repo": "org/repo"}, "repo:org/repo"),
        ("hn", {"hn_id": "42"}, "hn_id:42"),
    ],
)
def test_identity_key_uses_correct_field_per_source(source_type, item, expected):
    assert rw_mod._identity_key(source_type, item) == expected


def test_identity_key_falls_back_to_url_when_id_field_blank():
    assert rw_mod._identity_key("arxiv", {"arxiv_id": "", "url": "https://x.example/y"}) == (
        "url:https://x.example/y"
    )


def test_identity_key_falls_back_to_content_hash_when_nothing_stable():
    key = rw_mod._identity_key("arxiv", {"title": "no id or url"})
    assert key.startswith("hash:")
    # Deterministic for the same content.
    assert key == rw_mod._identity_key("arxiv", {"title": "no id or url"})


# ---------------------------------------------------------------------------
# MCP registration
# ---------------------------------------------------------------------------

def test_tools_registered_with_correct_schema_and_classification():
    from meridian import mcp_tools

    names = {t["name"] for t in mcp_tools._MCP_TOOLS_LIST}
    for tool in ("save_watchlist_query", "list_watchlist_queries",
                 "run_watchlist_query", "delete_watchlist_query"):
        assert tool in names, f"{tool} must be advertised in tools/list"
        assert mcp_tools._TOOL_CATEGORY.get(tool) == "research"
        assert mcp_tools._TOOL_ROLE_RELEVANCE.get(tool) == "planner"

    # Only the pure-read tool is read-only; save/run/delete all mutate state.
    assert "list_watchlist_queries" in mcp_tools._READ_ONLY_TOOLS
    assert "save_watchlist_query" not in mcp_tools._READ_ONLY_TOOLS
    assert "run_watchlist_query" not in mcp_tools._READ_ONLY_TOOLS
    assert "delete_watchlist_query" not in mcp_tools._READ_ONLY_TOOLS

    save_entry = next(t for t in mcp_tools._MCP_TOOLS_LIST if t["name"] == "save_watchlist_query")
    props = save_entry["inputSchema"]["properties"]
    assert set(save_entry["inputSchema"]["required"]) == {"source_type", "query"}
    assert set(props["source_type"]["enum"]) == {
        "arxiv", "openalex", "semantic_scholar", "pubmed",
        "github_code", "github_repo", "hn",
    }

    run_entry = next(t for t in mcp_tools._MCP_TOOLS_LIST if t["name"] == "run_watchlist_query")
    assert run_entry["inputSchema"]["required"] == ["watchlist_id"]


def test_handler_dispatch_table_wires_all_four_tools():
    """_handle_session_tools' _standard_dispatch must route all four new tool
    names to their research_watchlist.py handlers (mirrors the existing
    test_handler_dispatch_table_wires_github_search pattern)."""
    import inspect
    src = inspect.getsource(mh._handle_session_tools)
    for tool, handler_name in (
        ("save_watchlist_query", "handle_save_watchlist_query"),
        ("list_watchlist_queries", "handle_list_watchlist_queries"),
        ("run_watchlist_query", "handle_run_watchlist_query"),
        ("delete_watchlist_query", "handle_delete_watchlist_query"),
    ):
        assert f'"{tool}": {handler_name}' in src


@pytest.mark.asyncio
async def test_full_dispatch_via_handle_session_tools(db, project):
    """Integration: a real call through _handle_session_tools (the actual MCP
    dispatch path), not just the handler function directly."""
    pid = project["id"]
    result = await mh._handle_session_tools(
        "save_watchlist_query",
        {"project_id": pid, "source_type": "arxiv", "query": "diffusion models"},
        db, _DATA_DIR, None, None,
    )
    assert "error" not in result
    assert result["watchlist_id"]

    listed = await mh._handle_session_tools(
        "list_watchlist_queries", {"project_id": pid}, db, _DATA_DIR, None, None,
    )
    assert listed["count"] == 1


# ---------------------------------------------------------------------------
# agent_defaults.py protocol + version bump
# ---------------------------------------------------------------------------

def test_agent_instructions_standard_version_bumped_and_marker_matches():
    from meridian.agent_defaults import (
        DEFAULT_AGENT_INSTRUCTIONS,
        AGENT_INSTRUCTIONS_STANDARD_VERSION,
    )
    assert AGENT_INSTRUCTIONS_STANDARD_VERSION >= 19
    assert (
        f"meridian-executor-standard: v{AGENT_INSTRUCTIONS_STANDARD_VERSION}"
        in DEFAULT_AGENT_INSTRUCTIONS
    )


def test_research_protocol_names_watchlist_and_all_three_search_tools():
    from meridian.agent_defaults import DEFAULT_AGENT_INSTRUCTIONS

    assert "run_watchlist_query" in DEFAULT_AGENT_INSTRUCTIONS
    assert "save_watchlist_query" in DEFAULT_AGENT_INSTRUCTIONS
    assert "list_watchlist_queries" in DEFAULT_AGENT_INSTRUCTIONS
    assert "delete_watchlist_query" in DEFAULT_AGENT_INSTRUCTIONS
    # The previously-undocumented gap (github_search/social_search never named
    # in the protocol even though both are real tools) is now filled.
    assert "github_search" in DEFAULT_AGENT_INSTRUCTIONS
    assert "social_search" in DEFAULT_AGENT_INSTRUCTIONS
    # Legacy substrings from the locked test_paper_search.py contract must survive.
    assert "paper_search" in DEFAULT_AGENT_INSTRUCTIONS
    assert "paper-search" in DEFAULT_AGENT_INSTRUCTIONS


def test_paper_search_source_enum_unchanged():
    """Regression guard: this item must NOT widen paper_search's own locked
    'source' enum (tests/test_paper_search.py) — new sources are reached only
    via run_watchlist_query calling the underlying functions directly."""
    from meridian import mcp_tools

    entry = next(t for t in mcp_tools._MCP_TOOLS_LIST if t["name"] == "paper_search")
    assert set(entry["inputSchema"]["properties"]["source"]["enum"]) == {"arxiv", "openalex"}
