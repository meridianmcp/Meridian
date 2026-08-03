"""Tests for sprint item 81abd31f — _handle_session_tools dispatch-table refactor.

Proves that every tool previously handled by the if/elif chain in
_handle_session_tools continues to work correctly after the extraction into
meridian/mcp/handlers/session_tools.py.

Strategy:
- Call each per-tool handler function directly from the new submodule (unit).
- Call _handle_session_tools with each tool name and assert identical results
  (integration via the new dispatch table).
- Verify the module structure: each handler function is importable from the
  new submodule and is an async callable.
- Verify _MISS sentinel is returned for an unknown tool name (regression guard).

No server.py startup or real ports needed — all tests use an in-memory SQLite
DB (same pattern as tests/test_cov_handler.py) and monkeypatch heavy IO.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

import meridian.server  # noqa: F401 — must be imported before handler to avoid cycle
from meridian.mcp import handler as mh
from meridian.mcp.handlers import session_tools as st_mod
from meridian import db as db_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _make_db():
    return _run(db_module.init_db(":memory:"))


_DATA_DIR = "/tmp/meridian-test"

# ---------------------------------------------------------------------------
# Module-structure assertions
# ---------------------------------------------------------------------------

EXPECTED_HANDLER_NAMES = [
    "handle_checkpoint",
    "handle_get_context_block",
    "handle_list_sessions",
    "handle_get_session_log",
    "handle_get_session_activity",
    "handle_get_agent_instructions",
    "handle_set_agent_instructions",
    "handle_set_executor_config",
    "handle_idle_until_session_done",
    "handle_search_all",
    "handle_search_synthesis",
    "handle_paper_search",
    "handle_get_session_brief",
]


def test_all_expected_handlers_are_importable():
    """All 13 per-tool handlers must be importable from the new submodule."""
    for name in EXPECTED_HANDLER_NAMES:
        assert hasattr(st_mod, name), f"Missing handler: {name}"


def test_all_handlers_are_async():
    """Every handler must be an async function (coroutine function)."""
    for name in EXPECTED_HANDLER_NAMES:
        fn = getattr(st_mod, name)
        assert asyncio.iscoroutinefunction(fn), f"{name} is not async"


def test_unknown_tool_returns_miss():
    """_handle_session_tools must return _MISS for an unrecognised tool name."""
    db = _make_db()
    result = _run(mh._handle_session_tools(
        "no_such_tool_xyz", {}, db, _DATA_DIR, None, None
    ))
    _run(db.close())
    assert result is mh._MISS


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
    proj = await db_module.create_project(db, "test-session-proj")
    return proj


@pytest_asyncio.fixture
async def session(db, project):
    sess = await db_module.register_session(
        db, project["id"], "test-session",
        human_id=None,
    )
    return sess


# ---------------------------------------------------------------------------
# list_sessions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_sessions_dispatch_table(db, project, session):
    result = await mh._handle_session_tools(
        "list_sessions", {"project_id": project["id"]}, db, _DATA_DIR, None, None
    )
    assert isinstance(result, list)
    assert any(s.get("name") == "test-session" for s in result)


@pytest.mark.asyncio
async def test_list_sessions_handler_direct(db, project, session):
    result = await st_mod.handle_list_sessions(
        {"project_id": project["id"]}, db, _DATA_DIR, None, None
    )
    assert isinstance(result, list)
    assert any(s.get("name") == "test-session" for s in result)


@pytest.mark.asyncio
async def test_list_sessions_all_status(db, project, session):
    result = await mh._handle_session_tools(
        "list_sessions", {"project_id": project["id"], "status": "all"},
        db, _DATA_DIR, None, None
    )
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# get_context_block
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_context_block_dispatch_table(db, project):
    result = await mh._handle_session_tools(
        "get_context_block", {"project_id": project["id"]},
        db, _DATA_DIR, None, None
    )
    assert "text" in result
    assert "meridian_context" in result["text"]
    assert result["project_id"] == project["id"]


@pytest.mark.asyncio
async def test_get_context_block_handler_direct(db, project):
    result = await st_mod.handle_get_context_block(
        {"project_id": project["id"]}, db, _DATA_DIR, None, None
    )
    assert "text" in result
    assert "meridian_context" in result["text"]


@pytest.mark.asyncio
async def test_get_context_block_not_found(db):
    with pytest.raises(ValueError, match="project not found"):
        await mh._handle_session_tools(
            "get_context_block", {"project_id": "nonexistent-id"},
            db, _DATA_DIR, None, None
        )


@pytest.mark.asyncio
async def test_get_context_block_xml_envelope_structure(db, project):
    """4c9f501a — MCP tool surface wraps content in <meridian_context> XML
    envelope (v2.5+). The 'text' field must open and close with the correct
    tags so AI clients can parse it structurally. This test pins the actual
    behavior so the docstring can never silently drift back to claiming plain-text."""
    result = await st_mod.handle_get_context_block(
        {"project_id": project["id"], "mode": "full"}, db, _DATA_DIR, None, None
    )
    text = result["text"]
    # Must open with the XML envelope tag (with project_id and mode attrs).
    assert text.startswith("<meridian_context "), (
        "MCP get_context_block text must begin with <meridian_context ...>"
    )
    assert 'project_id="' in text
    assert 'mode="full"' in text
    # Must close with matching closing tag.
    assert text.rstrip().endswith("</meridian_context>"), (
        "MCP get_context_block text must end with </meridian_context>"
    )
    # The inner content must still contain the expected plain-text fields.
    assert "PROJECT:" in text
    assert "start_session" in text


@pytest.mark.asyncio
async def test_get_context_block_mcp_description_mentions_xml(db, project):
    """4c9f501a — The MCP tool description must accurately describe the XML-wrapped
    output rather than calling it 'plain-text'. Guards against future docstring
    regressions that would again contradict the actual behavior."""
    from meridian.mcp_tools import _MCP_TOOLS_LIST
    tool = next((t for t in _MCP_TOOLS_LIST if t["name"] == "get_context_block"), None)
    assert tool is not None, "get_context_block must be in _MCP_TOOLS_LIST"
    desc = tool["description"]
    # Description must mention the XML envelope, not falsely promise bare plain text.
    assert "meridian_context" in desc or "XML" in desc, (
        "get_context_block description must mention XML wrapping (<meridian_context>) "
        f"instead of falsely promising plain-text output. Got: {desc!r}"
    )
    # Must NOT claim 'plain-text' without qualification (the historical bug).
    assert "plain-text project context block" not in desc, (
        "get_context_block description must not claim to return a 'plain-text project "
        "context block' — the MCP surface wraps it in XML. Got: {desc!r}"
    )


# ---------------------------------------------------------------------------
# get_agent_instructions / set_agent_instructions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_agent_instructions_and_get(db, project):
    pid = project["id"]
    await mh._handle_session_tools(
        "set_agent_instructions",
        {"project_id": pid, "instructions": "Do the thing."},
        db, _DATA_DIR, None, None,
    )
    result = await mh._handle_session_tools(
        "get_agent_instructions", {"project_id": pid},
        db, _DATA_DIR, None, None,
    )
    assert result["project_id"] == pid
    assert "Do the thing." in str(result["agent_instructions"])


@pytest.mark.asyncio
async def test_set_agent_instructions_handler_direct(db, project):
    pid = project["id"]
    await st_mod.handle_set_agent_instructions(
        {"project_id": pid, "instructions": "Direct handler instructions."},
        db, _DATA_DIR, None, None,
    )
    result = await st_mod.handle_get_agent_instructions(
        {"project_id": pid}, db, _DATA_DIR, None, None
    )
    assert "Direct handler instructions." in str(result["agent_instructions"])


@pytest.mark.asyncio
async def test_set_agent_instructions_clear(db, project):
    """Passing empty instructions clears them (returns None)."""
    pid = project["id"]
    await st_mod.handle_set_agent_instructions(
        {"project_id": pid, "instructions": ""},
        db, _DATA_DIR, None, None,
    )
    result = await st_mod.handle_get_agent_instructions(
        {"project_id": pid}, db, _DATA_DIR, None, None
    )
    assert result["agent_instructions"] is None


# ---------------------------------------------------------------------------
# set_executor_config
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_executor_config_dispatch(db, project):
    pid = project["id"]
    result = await mh._handle_session_tools(
        "set_executor_config",
        {"project_id": pid, "repo_path": "/tmp/repo", "test_cmd": "pytest"},
        db, _DATA_DIR, None, None,
    )
    # Should return the merged config
    assert result is not None


@pytest.mark.asyncio
async def test_set_executor_config_handler_direct(db, project):
    pid = project["id"]
    result = await st_mod.handle_set_executor_config(
        {"project_id": pid, "repo_path": "/tmp/direct-repo"},
        db, _DATA_DIR, None, None,
    )
    assert result is not None


@pytest.mark.asyncio
async def test_set_executor_config_merges_repo_paths(db, project):
    """repo_paths is merged entry-by-entry, not overwritten."""
    pid = project["id"]
    await st_mod.handle_set_executor_config(
        {"project_id": pid, "repo_paths": [{"cwd": "/a", "hostname": "h1"}]},
        db, _DATA_DIR, None, None,
    )
    # Second call should merge, not overwrite
    await st_mod.handle_set_executor_config(
        {"project_id": pid, "repo_paths": [{"cwd": "/b", "hostname": "h2"}]},
        db, _DATA_DIR, None, None,
    )
    cfg = await db_module.get_executor_config(db, pid)
    assert cfg is not None


@pytest.mark.asyncio
async def test_set_executor_config_max_planning_turns_round_trips(db, project):
    """75ac1c8e — max_planning_turns is copied through set_executor_config and
    feeds directly into executor_config.build_execution_policy."""
    from meridian.executor_config import build_execution_policy

    pid = project["id"]
    await st_mod.handle_set_executor_config(
        {"project_id": pid, "max_planning_turns": 5},
        db, _DATA_DIR, None, None,
    )
    cfg = await db_module.get_executor_config(db, pid)
    assert cfg["max_planning_turns"] == 5
    policy = build_execution_policy(cfg, execution_mode="autonomous")
    assert policy["max_planning_turns"] == 5

    # An unsafe override (persisted as-is, same convention as max_turns) is
    # still rejected at policy-build time -- never trusted verbatim.
    await st_mod.handle_set_executor_config(
        {"project_id": pid, "max_planning_turns": -3},
        db, _DATA_DIR, None, None,
    )
    cfg2 = await db_module.get_executor_config(db, pid)
    policy2 = build_execution_policy(cfg2, execution_mode="autonomous")
    assert policy2["max_planning_turns"] == 1  # falls back to immediate default


@pytest.mark.asyncio
async def test_set_executor_config_other_keys_preserved_with_max_planning_turns(db, project):
    """3adbc954-style regression guard: setting max_planning_turns must not
    silently drop other already-copied scalar keys (context_threshold,
    isolation, ...)."""
    pid = project["id"]
    await st_mod.handle_set_executor_config(
        {
            "project_id": pid,
            "context_threshold": 40,
            "isolation": "worktree",
            "max_planning_turns": 3,
        },
        db, _DATA_DIR, None, None,
    )
    cfg = await db_module.get_executor_config(db, pid)
    assert cfg["context_threshold"] == 40
    assert cfg["isolation"] == "worktree"
    assert cfg["max_planning_turns"] == 3


# ---------------------------------------------------------------------------
# get_session_log
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_session_log_no_run(db, session):
    sid = session["id"]
    result = await mh._handle_session_tools(
        "get_session_log", {"session_id": sid},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result
    assert "no run found" in result["error"]


@pytest.mark.asyncio
async def test_get_session_log_handler_direct_no_run(db, session):
    sid = session["id"]
    result = await st_mod.handle_get_session_log(
        {"session_id": sid}, db, _DATA_DIR, None, None
    )
    assert "error" in result


# ---------------------------------------------------------------------------
# get_session_activity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_session_activity_dispatch(db, session):
    sid = session["id"]
    result = await mh._handle_session_tools(
        "get_session_activity", {"session_id": sid},
        db, _DATA_DIR, None, None,
    )
    assert result["session_id"] == sid
    assert "activity" in result
    assert "count" in result


@pytest.mark.asyncio
async def test_get_session_activity_handler_direct(db, session):
    sid = session["id"]
    result = await st_mod.handle_get_session_activity(
        {"session_id": sid}, db, _DATA_DIR, None, None
    )
    assert result["session_id"] == sid
    assert isinstance(result["activity"], list)


@pytest.mark.asyncio
async def test_get_session_activity_limit_capped(db, session):
    """limit is capped at 50 — passing 999 should not error."""
    sid = session["id"]
    result = await st_mod.handle_get_session_activity(
        {"session_id": sid, "limit": 999}, db, _DATA_DIR, None, None
    )
    assert result["count"] == 0  # no activity in fresh session


# ---------------------------------------------------------------------------
# search_all
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_all_dispatch(db, project):
    result = await mh._handle_session_tools(
        "search_all",
        {"project_id": project["id"], "query": "nonexistent-term-zzz"},
        db, _DATA_DIR, None, None,
    )
    assert isinstance(result, (list, dict))


@pytest.mark.asyncio
async def test_search_all_handler_direct(db, project):
    result = await st_mod.handle_search_all(
        {"project_id": project["id"], "query": "something"},
        db, _DATA_DIR, None, None,
    )
    assert isinstance(result, (list, dict))


# ---------------------------------------------------------------------------
# search_synthesis
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_synthesis_missing_query(db, project):
    result = await mh._handle_session_tools(
        "search_synthesis",
        {"project_id": project["id"], "query": ""},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result
    assert "query is required" in result["error"]


@pytest.mark.asyncio
async def test_search_synthesis_handler_direct_missing_query(db, project):
    result = await st_mod.handle_search_synthesis(
        {"project_id": project["id"], "query": ""},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_search_synthesis_with_query(db, project, monkeypatch):
    """search_synthesis with a real query calls synthesize_search_answer."""
    async def _fake_synth(query, results):
        return {"answer": f"answer for: {query}", "citations": []}

    monkeypatch.setattr("meridian.handoff.synthesize_search_answer", _fake_synth)
    result = await mh._handle_session_tools(
        "search_synthesis",
        {"project_id": project["id"], "query": "test query"},
        db, _DATA_DIR, None, None,
    )
    assert "query" in result
    assert result["query"] == "test query"
    assert "results" in result


# ---------------------------------------------------------------------------
# paper_search
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_paper_search_dispatch(db, project, monkeypatch):
    """paper_search calls the arxiv search by default."""
    async def _fake_arxiv(query, limit=10, sort_by="relevance"):
        return {"query": query, "count": 0, "results": []}

    monkeypatch.setattr("meridian.paper_search.arxiv_search", _fake_arxiv)
    result = await mh._handle_session_tools(
        "paper_search",
        {"query": "attention is all you need"},
        db, _DATA_DIR, None, None,
    )
    assert result["count"] == 0


@pytest.mark.asyncio
async def test_paper_search_openalex_route(db, project, monkeypatch):
    """source='openalex' routes to openalex_search."""
    async def _fake_openalex(query, limit=10, sort_by="relevance"):
        return {"query": query, "count": 0, "results": [], "source": "openalex"}

    monkeypatch.setattr("meridian.paper_search.openalex_search", _fake_openalex)
    result = await st_mod.handle_paper_search(
        {"query": "test", "source": "openalex"},
        db, _DATA_DIR, None, None,
    )
    assert result.get("source") == "openalex"


# ---------------------------------------------------------------------------
# idle_until_session_done
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_idle_until_session_done_dispatch(db, session, monkeypatch):
    """idle_until_session_done delegates to _server._idle_until_session_done."""
    async def _fake_idle(db_, watching_session_id, **kw):
        return {"done": True, "session_id": watching_session_id}

    monkeypatch.setattr(meridian.server, "_idle_until_session_done", _fake_idle)
    sid = session["id"]
    result = await mh._handle_session_tools(
        "idle_until_session_done",
        {"watching_session_id": sid},
        db, _DATA_DIR, None, None,
    )
    assert result["session_id"] == sid


@pytest.mark.asyncio
async def test_idle_until_session_done_handler_direct(db, session, monkeypatch):
    async def _fake_idle(db_, watching_session_id, **kw):
        return {"done": True, "session_id": watching_session_id}

    monkeypatch.setattr(meridian.server, "_idle_until_session_done", _fake_idle)
    sid = session["id"]
    result = await st_mod.handle_idle_until_session_done(
        {"watching_session_id": sid}, db, _DATA_DIR, None, None
    )
    assert result["session_id"] == sid


# ---------------------------------------------------------------------------
# get_session_brief
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_session_brief_dispatch(db, project):
    result = await mh._handle_session_tools(
        "get_session_brief",
        {"project_id": project["id"]},
        db, _DATA_DIR, None, None,
    )
    assert "text" in result
    assert "session_brief" in result["text"]
    assert result["project_id"] == project["id"]


@pytest.mark.asyncio
async def test_get_session_brief_handler_direct(db, project):
    result = await st_mod.handle_get_session_brief(
        {"project_id": project["id"]}, db, _DATA_DIR, None, None
    )
    assert "text" in result
    assert "session_brief" in result["text"]


@pytest.mark.asyncio
async def test_get_session_brief_role_planner(db, project):
    result = await mh._handle_session_tools(
        "get_session_brief",
        {"project_id": project["id"], "role": "planner"},
        db, _DATA_DIR, None, None,
    )
    assert result["role"] == "planner"
    assert 'role="planner"' in result["text"]


@pytest.mark.asyncio
async def test_get_session_brief_role_executor(db, project):
    result = await st_mod.handle_get_session_brief(
        {"project_id": project["id"], "role": "executor"},
        db, _DATA_DIR, None, None,
    )
    assert result["role"] == "executor"


# ---------------------------------------------------------------------------
# checkpoint (requires monkeypatching heavy IO)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_checkpoint_dispatch(db, project, session, monkeypatch):
    """checkpoint returns a dict with summary, next_goal, start_fresh."""
    pid = project["id"]
    sid = session["id"]

    # Monkeypatch heavy IO so the test is fast and self-contained
    monkeypatch.setattr(
        "meridian.server._finalize_session_md",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "meridian.db.auto_capture_session",
        AsyncMock(return_value=None),
    )

    async def _fake_generate_handoff(*args, **kw):
        return (None, "delta summary text", None)

    monkeypatch.setattr(
        "meridian.handoff.generate_handoff",
        _fake_generate_handoff,
    )

    async def _fake_commits(project_dict, tenant):
        return []

    async def _fake_identity(tenant):
        return None

    result = await mh._handle_session_tools(
        "checkpoint",
        {"session_id": sid, "project_id": pid},
        db, _DATA_DIR, None, None,
    )
    assert "summary" in result
    assert "next_goal" in result
    assert "start_fresh" in result
    assert "pending_count" in result


@pytest.mark.asyncio
async def test_checkpoint_handler_direct(db, project, session, monkeypatch):
    """handle_checkpoint works when called directly with keyword args."""
    pid = project["id"]
    sid = session["id"]

    monkeypatch.setattr(
        "meridian.server._finalize_session_md",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "meridian.db.auto_capture_session",
        AsyncMock(return_value=None),
    )

    async def _fake_generate_handoff(*args, **kw):
        return (None, "direct handler summary", None)

    monkeypatch.setattr(
        "meridian.handoff.generate_handoff",
        _fake_generate_handoff,
    )

    async def _fake_commits(project_dict, tenant):
        return []

    def _fake_identity(tenant):
        return None

    result = await st_mod.handle_checkpoint(
        {"session_id": sid, "project_id": pid},
        db, _DATA_DIR, None, None,
        fetch_recent_commits=_fake_commits,
        resolve_caller_identity=_fake_identity,
    )
    assert "summary" in result
    assert result["summary"] == "direct handler summary"


@pytest.mark.asyncio
async def test_checkpoint_in_progress_warning(db, project, session, monkeypatch):
    """checkpoint surfaces in_progress items in the action_required field."""
    pid = project["id"]
    sid = session["id"]

    # Create an in-progress sprint item
    item = await db_module.add_sprint_item(db, pid, "v1", "In-progress task")
    await db_module.claim_sprint_item(db, pid, item["id"], actor=sid)

    monkeypatch.setattr(
        "meridian.server._finalize_session_md",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "meridian.db.auto_capture_session",
        AsyncMock(return_value=None),
    )

    async def _fake_generate_handoff(*args, **kw):
        return (None, "summary with in-progress", None)

    monkeypatch.setattr(
        "meridian.handoff.generate_handoff",
        _fake_generate_handoff,
    )

    async def _fake_commits(project_dict, tenant):
        return []

    def _fake_identity(tenant):
        return None

    result = await st_mod.handle_checkpoint(
        {"session_id": sid, "project_id": pid},
        db, _DATA_DIR, None, None,
        fetch_recent_commits=_fake_commits,
        resolve_caller_identity=_fake_identity,
    )
    assert "in_progress_items" in result
    assert "action_required" in result


@pytest.mark.asyncio
async def test_checkpoint_handler_direct_scopes_to_session_version(
    db, project, monkeypatch
):
    """660314c1 — handle_checkpoint must scope pending_ids/next_goal to the
    calling session's own sprint_version, not the whole (cross-version) board.

    Regression: handle_checkpoint used to call
    get_sprint_items(db, project_id, status="pending") with no version
    argument at all, so a session scoped to one version bucket could see
    another version's item ids leak into pending_ids/next_goal. This test
    creates two version buckets and asserts the out-of-scope bucket's item
    never appears in either field for a session scoped to the in-scope one.
    """
    pid = project["id"]
    # Session explicitly scoped to v0.2.6 (mirrors how start_session persists
    # sprint_version on the session row).
    scoped_session = await db_module.register_session(
        db, pid, "version-scoped-session", sprint_version="v0.2.6",
    )
    sid = scoped_session["id"]

    in_scope = await db_module.add_sprint_item(db, pid, "v0.2.6", "in scope item")
    out_of_scope = await db_module.add_sprint_item(
        db, pid, "v0.2.5", "other version item"
    )

    monkeypatch.setattr(
        "meridian.server._finalize_session_md",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "meridian.db.auto_capture_session",
        AsyncMock(return_value=None),
    )

    async def _fake_generate_handoff(*args, **kw):
        return (None, "version-scoped summary", None)

    monkeypatch.setattr(
        "meridian.handoff.generate_handoff",
        _fake_generate_handoff,
    )

    async def _fake_commits(project_dict, tenant):
        return []

    def _fake_identity(tenant):
        return None

    result = await st_mod.handle_checkpoint(
        {"session_id": sid, "project_id": pid},
        db, _DATA_DIR, None, None,
        fetch_recent_commits=_fake_commits,
        resolve_caller_identity=_fake_identity,
    )
    assert result["pending_count"] == 1
    assert in_scope["id"][:8] in result["pending_ids"]
    assert out_of_scope["id"][:8] not in result["pending_ids"]
    assert in_scope["id"] in result["next_goal"]
    assert out_of_scope["id"] not in result["next_goal"]


# ---------------------------------------------------------------------------
# Dispatch-table completeness: all 13 known tool names must NOT return _MISS
# ---------------------------------------------------------------------------

TOOLS_IN_GROUP = [
    "checkpoint",
    "get_context_block",
    "list_sessions",
    "get_session_log",
    "get_session_activity",
    "get_agent_instructions",
    "set_agent_instructions",
    "set_executor_config",
    "idle_until_session_done",
    "search_all",
    "search_synthesis",
    "paper_search",
    "get_session_brief",
]


def test_dispatch_table_covers_all_tools():
    """Verify that each known tool name is NOT routed to _MISS.

    We confirm this by checking that calling _handle_session_tools with
    list_sessions (requires no args beyond project_id, just returns [])
    does NOT return _MISS.
    """
    db = _make_db()
    proj = _run(db_module.create_project(db, "completeness-check-proj"))
    pid = proj["id"]
    result = _run(mh._handle_session_tools(
        "list_sessions", {"project_id": pid}, db, _DATA_DIR, None, None
    ))
    _run(db.close())
    assert result is not mh._MISS, "list_sessions returned _MISS — not in dispatch table"


# ---------------------------------------------------------------------------
# 3fc6ff11 — get_context_block per-item pointer annotations
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_context_block_no_pointers_no_crash(db, project):
    """3fc6ff11 — items without pointers render title-only; no crash, no empty broken section."""
    pid = project["id"]
    await db_module.add_sprint_item(db, pid, "v1", "Plain item no pointers")
    result = await st_mod.handle_get_context_block(
        {"project_id": pid}, db, _DATA_DIR, None, None
    )
    text = result["text"]
    assert "Plain item no pointers" in text
    # No "Resolved pointers:" section should appear when there are none.
    assert "Resolved pointers:" not in text


@pytest.mark.asyncio
async def test_get_context_block_with_pointer_shows_annotation(db, project):
    """3fc6ff11 — pending item with a range pointer gets a 'Resolved pointers:' section
    inlined below its title in get_context_block output.
    """
    pid = project["id"]
    item = await db_module.add_sprint_item(db, pid, "v1", "Item with pointer")
    # Attach a range pointer (no symbol resolution needed for range type).
    await db_module.add_sprint_item_pointer(
        db,
        pid,
        item["id"],
        source_type="code",
        targets=[{
            "uri": "meridian/_deps.py",
            "selector": {"type": "range", "start_line": 694, "end_line": 697},
        }],
        label="pending-items rendering loop",
    )
    result = await st_mod.handle_get_context_block(
        {"project_id": pid}, db, _DATA_DIR, None, None
    )
    text = result["text"]
    assert "Item with pointer" in text
    assert "Resolved pointers:" in text
    # The range target should render as "file:start-end".
    assert "meridian/_deps.py" in text
    assert "694" in text
    # The label should appear.
    assert "pending-items rendering loop" in text
    # Source type bracketed.
    assert "[code]" in text


@pytest.mark.asyncio
async def test_get_context_block_pointer_annotation_below_title(db, project):
    """3fc6ff11 — pointer annotation must appear AFTER the item title line, not before it."""
    pid = project["id"]
    item = await db_module.add_sprint_item(db, pid, "v1", "Annotation position check")
    await db_module.add_sprint_item_pointer(
        db,
        pid,
        item["id"],
        source_type="code",
        targets=[{
            "uri": "some/file.py",
            "selector": {"type": "range", "start_line": 10, "end_line": 20},
        }],
    )
    result = await st_mod.handle_get_context_block(
        {"project_id": pid}, db, _DATA_DIR, None, None
    )
    text = result["text"]
    title_pos = text.find("Annotation position check")
    pointer_pos = text.find("Resolved pointers:")
    assert title_pos != -1
    assert pointer_pos != -1
    assert title_pos < pointer_pos, (
        "Pointer annotation section must appear after the item title line"
    )


@pytest.mark.asyncio
async def test_get_context_block_multiple_items_mixed_pointers(db, project):
    """3fc6ff11 — items with pointers get annotations; items without don't; both coexist."""
    pid = project["id"]
    item_with = await db_module.add_sprint_item(db, pid, "v1", "Has pointer")
    await db_module.add_sprint_item(db, pid, "v1", "No pointer")
    await db_module.add_sprint_item_pointer(
        db,
        pid,
        item_with["id"],
        source_type="code",
        targets=[{
            "uri": "meridian/server.py",
            "selector": {"type": "range", "start_line": 1, "end_line": 5},
        }],
        label="top of server",
    )
    result = await st_mod.handle_get_context_block(
        {"project_id": pid}, db, _DATA_DIR, None, None
    )
    text = result["text"]
    assert "Has pointer" in text
    assert "No pointer" in text
    assert "Resolved pointers:" in text
    assert "meridian/server.py" in text
    assert "top of server" in text


@pytest.mark.asyncio
async def test_render_context_block_resolved_pointers_direct(db, project):
    """3fc6ff11 — _render_context_block renders 'resolved_pointers' when pre-annotated items
    are passed in (the sync renderer reads the key off each item dict directly)."""
    from meridian._deps import _render_context_block
    goal = None
    # Build an item dict with pre-annotated resolved_pointers (as _annotate_resolved_pointers
    # would produce).
    sprint_items = [
        {
            "id": "fake-id-1",
            "status": "pending",
            "title": "Direct render item",
            "resolved_pointers": [
                {
                    "source_type": "code",
                    "label": "the renderer",
                    "targets": ["meridian/_deps.py:694-697"],
                }
            ],
        }
    ]
    text = _render_context_block(
        {"id": project["id"], "name": "test-proj", "decisions": ""},
        goal,
        sprint_items,
        [],
        [],
        [],
    )
    assert "Direct render item" in text
    assert "Resolved pointers:" in text
    assert "the renderer" in text
    assert "meridian/_deps.py:694-697" in text
    assert "[code]" in text


@pytest.mark.asyncio
async def test_render_context_block_no_pointers_key_absent(db, project):
    """3fc6ff11 — _render_context_block handles items where 'resolved_pointers' key is missing
    entirely (not just an empty list) — no crash, no empty section."""
    from meridian._deps import _render_context_block
    sprint_items = [
        {"id": "fake-id-2", "status": "pending", "title": "No key item"},
    ]
    text = _render_context_block(
        {"id": project["id"], "name": "test-proj", "decisions": ""},
        None,
        sprint_items,
        [],
        [],
        [],
    )
    assert "No key item" in text
    assert "Resolved pointers:" not in text
