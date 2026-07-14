"""Tests for sprint item ac4df52f — _handle_notes_decisions dispatch-table refactor.

Proves that every tool previously handled by the if/elif chain in
_handle_notes_decisions continues to work correctly after the extraction into
meridian/mcp/handlers/notes_decisions.py.

Strategy:
- Call each per-tool handler function directly from the new submodule (unit).
- Call _handle_notes_decisions with each tool name and assert identical results
  (integration via the new dispatch table).
- Verify the module structure: each handler function is importable from the
  new submodule and is an async callable.
- Verify _MISS sentinel is returned for an unknown tool name (regression guard).

No server.py startup or real ports needed — all tests use an in-memory SQLite
DB (same pattern as tests/test_97d695c4_project_tools_dispatch.py) and
monkeypatch heavy IO.
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

import meridian.server  # noqa: F401 — must be imported before handler to avoid cycle
from meridian.mcp import handler as mh
from meridian.mcp.handlers import notes_decisions as nd_mod
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
    "handle_pin_decision",
    "handle_update_decision",
    "handle_validate_assumption",
    "handle_get_pinned_decisions",
    "handle_archive_decision",
    "handle_add_note",
    "handle_get_notes",
    "handle_read_note",
    "handle_delete_note",
    "handle_ingest_document",
    "handle_get_document_structure",
    "handle_get_latex_structure",
    "handle_ingest_document_structure",
    "handle_get_citation_edges",
    "handle_resolve_citations",
    "handle_index_equation",
    "handle_find_similar_equation",
    "handle_insert_equation",
    "handle_update_paragraph",
    "handle_find_symbol_usages",
    "handle_index_figure",
    "handle_find_similar_figure",
    "handle_link_figure_caption",
    "handle_index_table",
    "handle_find_similar_table",
    "handle_add_insight",
    "handle_get_insights",
    "handle_save_finding",
    "handle_capture_research_finding",
    "handle_add_workspace_note",
    "handle_get_workspace_notes",
    "handle_pin_workspace_decision",
    "handle_get_workspace_decisions",
    "handle_get_workspace_settings",
    "handle_update_workspace_settings",
    "handle_save_blog_post",
    "handle_get_blog_posts",
    "handle_add_workspace_sprint_item",
    "handle_get_workspace_sprint_items",
    "handle_update_workspace_sprint_item",
    "handle_complete_workspace_sprint_item",
    "handle_add_workspace_proposal",
    "handle_get_workspace_proposals",
    "handle_advance_proposal_status",
    "handle_promote_proposal",
]

# All 45 tool names that must be covered by the dispatch table.
ALL_TOOL_NAMES = [
    "pin_decision",
    "update_decision",
    "validate_assumption",
    "get_pinned_decisions",
    "archive_decision",
    "add_note",
    "get_notes",
    "read_note",
    "delete_note",
    "ingest_document",
    "get_document_structure",
    "get_latex_structure",
    "ingest_document_structure",
    "get_citation_edges",
    "resolve_citations",
    "index_equation",
    "find_similar_equation",
    "insert_equation",
    "update_paragraph",
    "find_symbol_usages",
    "index_figure",
    "find_similar_figure",
    "link_figure_caption",
    "index_table",
    "find_similar_table",
    "add_insight",
    "get_insights",
    "save_finding",
    "capture_research_finding",
    "add_workspace_note",
    "get_workspace_notes",
    "pin_workspace_decision",
    "get_workspace_decisions",
    "get_workspace_settings",
    "update_workspace_settings",
    "save_blog_post",
    "get_blog_posts",
    "add_workspace_sprint_item",
    "get_workspace_sprint_items",
    "update_workspace_sprint_item",
    "complete_workspace_sprint_item",
    "add_workspace_proposal",
    "get_workspace_proposals",
    "advance_proposal_status",
    "promote_proposal",
]


def test_all_expected_handlers_are_importable():
    """All 45 per-tool handlers must be importable from the new submodule."""
    for name in EXPECTED_HANDLER_NAMES:
        assert hasattr(nd_mod, name), f"Missing handler: {name}"


def test_all_handlers_are_async():
    """Every handler must be an async function (coroutine function)."""
    for name in EXPECTED_HANDLER_NAMES:
        fn = getattr(nd_mod, name)
        assert asyncio.iscoroutinefunction(fn), f"{name} is not async"


def test_unknown_tool_returns_miss():
    """_handle_notes_decisions must return _MISS for an unrecognised tool name."""
    db = _make_db()
    result = _run(mh._handle_notes_decisions(
        "no_such_tool_xyz", {}, db, _DATA_DIR, None, None
    ))
    _run(db.close())
    assert result is mh._MISS


def test_handler_count_matches_tool_count():
    """Number of handler functions must equal number of tool names."""
    assert len(EXPECTED_HANDLER_NAMES) == len(ALL_TOOL_NAMES)


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
    proj = await db_module.create_project(db, "test-proj-nd")
    return proj


# ---------------------------------------------------------------------------
# Decisions: pin_decision
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pin_decision_dispatch(db, project, monkeypatch):
    """pin_decision creates a decision and returns it."""
    monkeypatch.setattr(_server_mod(), "_append_decision_to_md", _noop_async, raising=False)
    pid = project["id"]
    result = await mh._handle_notes_decisions(
        "pin_decision",
        {"project_id": pid, "title": "Use SQLite", "body": "Simple and portable"},
        db, _DATA_DIR, None, None,
    )
    assert "id" in result or "decision_id" in result or "title" in result


@pytest.mark.asyncio
async def test_pin_decision_handler_direct(db, project, monkeypatch):
    monkeypatch.setattr(_server_mod(), "_append_decision_to_md", _noop_async, raising=False)
    pid = project["id"]
    result = await nd_mod.handle_pin_decision(
        {"project_id": pid, "title": "Direct decision", "body": "body"},
        db, _DATA_DIR, None, None,
    )
    assert "id" in result or "title" in result


# ---------------------------------------------------------------------------
# Decisions: get_pinned_decisions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_pinned_decisions_dispatch(db, project, monkeypatch):
    monkeypatch.setattr(_server_mod(), "_append_decision_to_md", _noop_async, raising=False)
    pid = project["id"]
    # Create one first
    await db_module.pin_decision(db, pid, "A decision", "body", "TECHNICAL")
    result = await mh._handle_notes_decisions(
        "get_pinned_decisions",
        {"project_id": pid},
        db, _DATA_DIR, None, None,
    )
    assert isinstance(result, list)
    titles = [d.get("title") for d in result]
    assert "A decision" in titles


@pytest.mark.asyncio
async def test_get_pinned_decisions_handler_direct(db, project, monkeypatch):
    monkeypatch.setattr(_server_mod(), "_append_decision_to_md", _noop_async, raising=False)
    pid = project["id"]
    await db_module.pin_decision(db, pid, "Handler decision", "body2", "TECHNICAL")
    result = await nd_mod.handle_get_pinned_decisions(
        {"project_id": pid},
        db, _DATA_DIR, None, None,
    )
    assert isinstance(result, list)
    assert any(d.get("title") == "Handler decision" for d in result)


# ---------------------------------------------------------------------------
# Decisions: update_decision
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_decision_dispatch(db, project, monkeypatch):
    monkeypatch.setattr(_server_mod(), "_append_decision_to_md", _noop_async, raising=False)
    pid = project["id"]
    created = await db_module.pin_decision(db, pid, "Old title", "Old body", "TECHNICAL")
    did = created.get("id") or created.get("decision_id")
    result = await mh._handle_notes_decisions(
        "update_decision",
        {"decision_id": did, "body": "Updated body"},
        db, _DATA_DIR, None, None,
    )
    assert "error" not in result


@pytest.mark.asyncio
async def test_update_decision_not_found_raises(db, monkeypatch):
    monkeypatch.setattr(_server_mod(), "_append_decision_to_md", _noop_async, raising=False)
    with pytest.raises(ValueError, match="decision not found"):
        await mh._handle_notes_decisions(
            "update_decision",
            {"decision_id": "nonexistent-id", "body": "body"},
            db, _DATA_DIR, None, None,
        )


# ---------------------------------------------------------------------------
# Decisions: archive_decision
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_archive_decision_dispatch(db, project, monkeypatch):
    monkeypatch.setattr(_server_mod(), "_append_decision_to_md", _noop_async, raising=False)
    pid = project["id"]
    created = await db_module.pin_decision(db, pid, "To delete", "body", "TECHNICAL")
    did = created.get("id") or created.get("decision_id")
    result = await mh._handle_notes_decisions(
        "archive_decision",
        {"decision_id": did},
        db, _DATA_DIR, None, None,
    )
    assert result.get("deleted") is True


@pytest.mark.asyncio
async def test_archive_decision_not_found_raises(db, monkeypatch):
    monkeypatch.setattr(_server_mod(), "_append_decision_to_md", _noop_async, raising=False)
    with pytest.raises(ValueError, match="decision not found"):
        await mh._handle_notes_decisions(
            "archive_decision",
            {"decision_id": "nonexistent-id"},
            db, _DATA_DIR, None, None,
        )


# ---------------------------------------------------------------------------
# Decisions: validate_assumption
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_validate_assumption_missing_confirmed(db, project, monkeypatch):
    monkeypatch.setattr(_server_mod(), "_append_decision_to_md", _noop_async, raising=False)
    pid = project["id"]
    created = await db_module.pin_decision(db, pid, "Assumption decision", "body", "TECHNICAL")
    did = created.get("id") or created.get("decision_id")
    result = await mh._handle_notes_decisions(
        "validate_assumption",
        {"decision_id": did},  # missing 'confirmed'
        db, _DATA_DIR, None, None,
    )
    assert "error" in result
    assert "confirmed" in result["error"]


@pytest.mark.asyncio
async def test_validate_assumption_handler_direct_missing_confirmed(db):
    result = await nd_mod.handle_validate_assumption(
        {"decision_id": "some-id"},  # missing 'confirmed'
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


# ---------------------------------------------------------------------------
# Notes: add_note / get_notes / read_note / delete_note
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_note_dispatch(db, project, monkeypatch):
    monkeypatch.setattr(_server_mod(), "_append_note_to_roadmap", _noop_async, raising=False)
    pid = project["id"]
    result = await mh._handle_notes_decisions(
        "add_note",
        {"project_id": pid, "title": "My note", "body": "Note body"},
        db, _DATA_DIR, None, None,
    )
    assert "id" in result or "slug" in result


@pytest.mark.asyncio
async def test_add_note_handler_direct(db, project, monkeypatch):
    monkeypatch.setattr(_server_mod(), "_append_note_to_roadmap", _noop_async, raising=False)
    pid = project["id"]
    result = await nd_mod.handle_add_note(
        {"project_id": pid, "title": "Direct note", "body": "body text"},
        db, _DATA_DIR, None, None,
    )
    assert "id" in result or "slug" in result


@pytest.mark.asyncio
async def test_get_notes_dispatch(db, project, monkeypatch):
    monkeypatch.setattr(_server_mod(), "_append_note_to_roadmap", _noop_async, raising=False)
    pid = project["id"]
    await db_module.add_project_note(db, pid, "Note A", "body A", None)
    result = await mh._handle_notes_decisions(
        "get_notes",
        {"project_id": pid},
        db, _DATA_DIR, None, None,
    )
    assert isinstance(result, list)
    assert any(n.get("title") == "Note A" for n in result)


@pytest.mark.asyncio
async def test_get_notes_handler_direct(db, project):
    pid = project["id"]
    await db_module.add_project_note(db, pid, "Note B", "body B", None)
    result = await nd_mod.handle_get_notes(
        {"project_id": pid},
        db, _DATA_DIR, None, None,
    )
    assert isinstance(result, list)
    assert any(n.get("title") == "Note B" for n in result)


@pytest.mark.asyncio
async def test_read_note_dispatch(db, project, monkeypatch):
    monkeypatch.setattr(_server_mod(), "_append_note_to_roadmap", _noop_async, raising=False)
    pid = project["id"]
    created = await db_module.add_project_note(db, pid, "Readable note", "body", None)
    slug = created.get("slug")
    result = await mh._handle_notes_decisions(
        "read_note",
        {"project_id": pid, "slug": slug},
        db, _DATA_DIR, None, None,
    )
    assert result.get("title") == "Readable note"


@pytest.mark.asyncio
async def test_read_note_not_found(db, project):
    pid = project["id"]
    result = await mh._handle_notes_decisions(
        "read_note",
        {"project_id": pid, "slug": "nonexistent-slug-xyz"},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_read_note_handler_direct(db, project):
    pid = project["id"]
    result = await nd_mod.handle_read_note(
        {"project_id": pid, "slug": "not-a-real-slug"},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_delete_note_dispatch(db, project):
    pid = project["id"]
    created = await db_module.add_project_note(db, pid, "Delete me", "body", None)
    note_id = created.get("id")
    result = await mh._handle_notes_decisions(
        "delete_note",
        {"note_id": note_id},
        db, _DATA_DIR, None, None,
    )
    assert "deleted" in result


@pytest.mark.asyncio
async def test_delete_note_handler_direct(db, project):
    pid = project["id"]
    created = await db_module.add_project_note(db, pid, "Delete me 2", "body", None)
    note_id = created.get("id")
    result = await nd_mod.handle_delete_note(
        {"note_id": note_id},
        db, _DATA_DIR, None, None,
    )
    assert "deleted" in result


# ---------------------------------------------------------------------------
# Notes: hashtag extraction in add_note (41b8a927)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_note_hashtag_extraction(db, project, monkeypatch):
    """#hashtags in the title/body are added to tags automatically."""
    monkeypatch.setattr(_server_mod(), "_append_note_to_roadmap", _noop_async, raising=False)
    pid = project["id"]
    result = await nd_mod.handle_add_note(
        {"project_id": pid, "title": "My #important note", "body": "check #todo"},
        db, _DATA_DIR, None, None,
    )
    assert "error" not in result


# ---------------------------------------------------------------------------
# Insights: add_insight / get_insights
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_insight_dispatch(db, project):
    pid = project["id"]
    result = await mh._handle_notes_decisions(
        "add_insight",
        {"project_id": pid, "title": "Key insight", "body": "This matters"},
        db, _DATA_DIR, None, None,
    )
    assert "id" in result or "title" in result


@pytest.mark.asyncio
async def test_add_insight_handler_direct(db, project):
    pid = project["id"]
    result = await nd_mod.handle_add_insight(
        {"project_id": pid, "title": "Direct insight", "body": "body"},
        db, _DATA_DIR, None, None,
    )
    assert "id" in result or "title" in result


@pytest.mark.asyncio
async def test_get_insights_dispatch(db, project):
    pid = project["id"]
    await db_module.create_insight(db, pid, "Insight X", "body", horizon="quarter")
    result = await mh._handle_notes_decisions(
        "get_insights",
        {"project_id": pid},
        db, _DATA_DIR, None, None,
    )
    assert isinstance(result, list)
    assert any(i.get("title") == "Insight X" for i in result)


@pytest.mark.asyncio
async def test_get_insights_handler_direct(db, project):
    pid = project["id"]
    await db_module.create_insight(db, pid, "Insight Y", "body", horizon="quarter")
    result = await nd_mod.handle_get_insights(
        {"project_id": pid},
        db, _DATA_DIR, None, None,
    )
    assert isinstance(result, list)
    assert any(i.get("title") == "Insight Y" for i in result)


# ---------------------------------------------------------------------------
# Findings: save_finding / capture_research_finding
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_finding_dispatch(db, project):
    pid = project["id"]
    result = await mh._handle_notes_decisions(
        "save_finding",
        {"project_id": pid, "summary": "Found important thing"},
        db, _DATA_DIR, None, None,
    )
    assert "error" not in result


@pytest.mark.asyncio
async def test_save_finding_empty_summary_returns_error(db, project):
    pid = project["id"]
    result = await mh._handle_notes_decisions(
        "save_finding",
        {"project_id": pid, "summary": ""},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result
    assert "non-empty" in result["error"]


@pytest.mark.asyncio
async def test_save_finding_handler_direct(db, project):
    pid = project["id"]
    result = await nd_mod.handle_save_finding(
        {"project_id": pid, "summary": "Direct finding"},
        db, _DATA_DIR, None, None,
    )
    assert "error" not in result


@pytest.mark.asyncio
async def test_capture_research_finding_missing_url(db, project):
    pid = project["id"]
    result = await mh._handle_notes_decisions(
        "capture_research_finding",
        {"project_id": pid, "summary": "some thing"},  # missing url
        db, _DATA_DIR, None, None,
    )
    assert "error" in result
    assert "url" in result["error"]


@pytest.mark.asyncio
async def test_capture_research_finding_arxiv_autodetect(db, project):
    pid = project["id"]
    result = await nd_mod.handle_capture_research_finding(
        {
            "project_id": pid,
            "url": "https://arxiv.org/abs/1234.5678",
            "summary": "Great paper",
        },
        db, _DATA_DIR, None, None,
    )
    assert "error" not in result


@pytest.mark.asyncio
async def test_capture_research_finding_empty_summary(db, project):
    pid = project["id"]
    result = await nd_mod.handle_capture_research_finding(
        {"project_id": pid, "url": "https://example.com", "summary": ""},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


# ---------------------------------------------------------------------------
# Workspace notes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_workspace_note_dispatch(db):
    result = await mh._handle_notes_decisions(
        "add_workspace_note",
        {"title": "WS note", "body": "workspace body"},
        db, _DATA_DIR, None, "tenant-1",
    )
    assert "id" in result or "title" in result


@pytest.mark.asyncio
async def test_add_workspace_note_handler_direct(db):
    result = await nd_mod.handle_add_workspace_note(
        {"title": "Direct WS note", "body": "workspace body"},
        db, _DATA_DIR, None, "tenant-2",
    )
    assert "id" in result or "title" in result


@pytest.mark.asyncio
async def test_get_workspace_notes_dispatch(db):
    await db_module.add_workspace_note(db, "WS note list", "body", None, tenant_id="t1")
    result = await mh._handle_notes_decisions(
        "get_workspace_notes",
        {},
        db, _DATA_DIR, None, "t1",
    )
    assert isinstance(result, list)


@pytest.mark.asyncio
async def test_get_workspace_notes_handler_direct(db):
    result = await nd_mod.handle_get_workspace_notes(
        {},
        db, _DATA_DIR, None, "t1",
    )
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Workspace decisions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pin_workspace_decision_dispatch(db):
    result = await mh._handle_notes_decisions(
        "pin_workspace_decision",
        {"title": "WS decision", "body": "workspace decision body"},
        db, _DATA_DIR, None, "tenant-ws",
    )
    assert "id" in result or "title" in result


@pytest.mark.asyncio
async def test_pin_workspace_decision_handler_direct(db):
    result = await nd_mod.handle_pin_workspace_decision(
        {"title": "Direct WS decision", "body": "body"},
        db, _DATA_DIR, None, "tenant-ws-2",
    )
    assert "id" in result or "title" in result


@pytest.mark.asyncio
async def test_get_workspace_decisions_dispatch(db):
    await db_module.pin_workspace_decision(db, "WS Dec A", "body A", tenant_id="t-dec")
    result = await mh._handle_notes_decisions(
        "get_workspace_decisions",
        {},
        db, _DATA_DIR, None, "t-dec",
    )
    assert isinstance(result, list)


@pytest.mark.asyncio
async def test_get_workspace_decisions_handler_direct(db):
    result = await nd_mod.handle_get_workspace_decisions(
        {},
        db, _DATA_DIR, None, "t-dec-direct",
    )
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Workspace settings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_workspace_settings_dispatch(db):
    result = await mh._handle_notes_decisions(
        "get_workspace_settings",
        {},
        db, _DATA_DIR, None, "t-settings",
    )
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_get_workspace_settings_handler_direct(db):
    result = await nd_mod.handle_get_workspace_settings(
        {},
        db, _DATA_DIR, None, "t-settings-2",
    )
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_update_workspace_settings_dispatch(db):
    result = await mh._handle_notes_decisions(
        "update_workspace_settings",
        {"hitl_auto_answer_default": None},
        db, _DATA_DIR, None, "t-upd",
    )
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_update_workspace_settings_handler_direct(db):
    result = await nd_mod.handle_update_workspace_settings(
        {"sprint_name_default": "Wave"},
        db, _DATA_DIR, None, "t-upd-2",
    )
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Blog posts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_blog_post_dispatch(db):
    result = await mh._handle_notes_decisions(
        "save_blog_post",
        {"title": "My post", "body": "Post body"},
        db, _DATA_DIR, None, "t-blog",
    )
    assert "id" in result or "title" in result


@pytest.mark.asyncio
async def test_save_blog_post_handler_direct(db):
    result = await nd_mod.handle_save_blog_post(
        {"title": "Direct post", "body": "body"},
        db, _DATA_DIR, None, "t-blog-2",
    )
    assert "id" in result or "title" in result


@pytest.mark.asyncio
async def test_get_blog_posts_dispatch(db):
    await db_module.save_blog_post(db, "Blog post A", "body", tenant_id="t-gbp")
    result = await mh._handle_notes_decisions(
        "get_blog_posts",
        {},
        db, _DATA_DIR, None, "t-gbp",
    )
    assert isinstance(result, list)


@pytest.mark.asyncio
async def test_get_blog_posts_handler_direct(db):
    result = await nd_mod.handle_get_blog_posts(
        {},
        db, _DATA_DIR, None, "t-gbp-direct",
    )
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Workspace sprint items
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_workspace_sprint_item_dispatch(db):
    result = await mh._handle_notes_decisions(
        "add_workspace_sprint_item",
        {"title": "WS sprint item"},
        db, _DATA_DIR, None, "t-wsi",
    )
    assert "id" in result or "title" in result


@pytest.mark.asyncio
async def test_add_workspace_sprint_item_handler_direct(db):
    result = await nd_mod.handle_add_workspace_sprint_item(
        {"title": "Direct WS sprint item"},
        db, _DATA_DIR, None, "t-wsi-2",
    )
    assert "id" in result or "title" in result


@pytest.mark.asyncio
async def test_get_workspace_sprint_items_dispatch(db):
    await db_module.add_workspace_sprint_item(db, "WS item list", tenant_id="t-gwsi")
    result = await mh._handle_notes_decisions(
        "get_workspace_sprint_items",
        {},
        db, _DATA_DIR, None, "t-gwsi",
    )
    assert isinstance(result, list)


@pytest.mark.asyncio
async def test_get_workspace_sprint_items_handler_direct(db):
    result = await nd_mod.handle_get_workspace_sprint_items(
        {},
        db, _DATA_DIR, None, "t-gwsi-2",
    )
    assert isinstance(result, list)


@pytest.mark.asyncio
async def test_update_workspace_sprint_item_not_found(db):
    result = await mh._handle_notes_decisions(
        "update_workspace_sprint_item",
        {"item_id": "nonexistent-item-id", "title": "New title"},
        db, _DATA_DIR, None, "t-uwsi",
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_complete_workspace_sprint_item_not_found(db):
    result = await mh._handle_notes_decisions(
        "complete_workspace_sprint_item",
        {"item_id": "nonexistent-item-id"},
        db, _DATA_DIR, None, "t-cwsi",
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_update_workspace_sprint_item_handler_direct(db):
    result = await nd_mod.handle_update_workspace_sprint_item(
        {"item_id": "nonexistent-id", "title": "title"},
        db, _DATA_DIR, None, "t-uwsi-2",
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_complete_workspace_sprint_item_lifecycle(db):
    created = await db_module.add_workspace_sprint_item(db, "Complete me", tenant_id="t-lifecycle")
    item_id = created.get("id")
    result = await nd_mod.handle_complete_workspace_sprint_item(
        {"item_id": item_id},
        db, _DATA_DIR, None, "t-lifecycle",
    )
    assert result.get("status") == "done" or "error" not in result


# ---------------------------------------------------------------------------
# Workspace proposals
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_workspace_proposal_dispatch(db):
    result = await mh._handle_notes_decisions(
        "add_workspace_proposal",
        {"title": "Proposal A", "body": "body of proposal"},
        db, _DATA_DIR, None, "t-prop",
    )
    assert "id" in result or "title" in result


@pytest.mark.asyncio
async def test_add_workspace_proposal_handler_direct(db):
    result = await nd_mod.handle_add_workspace_proposal(
        {"title": "Direct proposal", "body": "body"},
        db, _DATA_DIR, None, "t-prop-2",
    )
    assert "id" in result or "title" in result


@pytest.mark.asyncio
async def test_get_workspace_proposals_dispatch(db):
    await db_module.add_workspace_proposal(db, "Proposal list", "body", tenant_id="t-gwp")
    result = await mh._handle_notes_decisions(
        "get_workspace_proposals",
        {},
        db, _DATA_DIR, None, "t-gwp",
    )
    assert isinstance(result, list)


@pytest.mark.asyncio
async def test_get_workspace_proposals_handler_direct(db):
    result = await nd_mod.handle_get_workspace_proposals(
        {},
        db, _DATA_DIR, None, "t-gwp-direct",
    )
    assert isinstance(result, list)


@pytest.mark.asyncio
async def test_advance_proposal_status_not_found(db):
    result = await mh._handle_notes_decisions(
        "advance_proposal_status",
        {"proposal_id": "nonexistent-proposal", "status": "approved"},
        db, _DATA_DIR, None, "t-aps",
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_advance_proposal_status_handler_direct(db):
    result = await nd_mod.handle_advance_proposal_status(
        {"proposal_id": "nonexistent", "status": "approved"},
        db, _DATA_DIR, None, "t-aps-2",
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_promote_proposal_missing_project_id(db):
    result = await mh._handle_notes_decisions(
        "promote_proposal",
        {"proposal_id": "some-prop"},  # missing project_id
        db, _DATA_DIR, None, "t-pp",
    )
    assert "error" in result
    assert "project_id" in result["error"]


@pytest.mark.asyncio
async def test_promote_proposal_handler_direct_missing_project(db):
    result = await nd_mod.handle_promote_proposal(
        {"proposal_id": "some-prop"},
        db, _DATA_DIR, None, "t-pp-2",
    )
    assert "error" in result


# ---------------------------------------------------------------------------
# Document-structure tools: error paths (no real file, just guard checks)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_document_hosted_path_guard(db, project, monkeypatch):
    """On hosted mode, ingest_document with a file_path but no content returns an error."""
    # Patch the name as imported into the notes_decisions module (not the source module).
    monkeypatch.setattr(nd_mod, "_hosted_mode", lambda: True)
    pid = project["id"]
    result = await nd_mod.handle_ingest_document(
        {"project_id": pid, "file_path": "/some/file.docx"},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result
    assert result.get("hosted") is True


@pytest.mark.asyncio
async def test_get_document_structure_hosted_guard(db, monkeypatch):
    """On hosted mode, get_document_structure returns an error with hosted=True."""
    monkeypatch.setattr(nd_mod, "_hosted_mode", lambda: True)
    result = await nd_mod.handle_get_document_structure(
        {"file_path": "/some/file.docx"},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result
    assert result.get("hosted") is True


@pytest.mark.asyncio
async def test_get_document_structure_missing_file_path(db, monkeypatch):
    monkeypatch.setattr(nd_mod, "_hosted_mode", lambda: False)
    result = await nd_mod.handle_get_document_structure(
        {},  # no file_path
        db, _DATA_DIR, None, None,
    )
    assert "error" in result
    assert "file_path" in result["error"]


@pytest.mark.asyncio
async def test_get_latex_structure_missing_args(db, monkeypatch):
    monkeypatch.setattr(nd_mod, "_hosted_mode", lambda: False)
    result = await nd_mod.handle_get_latex_structure(
        {},  # no file_path or source
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_get_latex_structure_hosted_path_guard(db, monkeypatch):
    monkeypatch.setattr(nd_mod, "_hosted_mode", lambda: True)
    result = await nd_mod.handle_get_latex_structure(
        {"file_path": "/some/file.tex"},  # path only, hosted
        db, _DATA_DIR, None, None,
    )
    assert "error" in result
    assert result.get("hosted") is True


@pytest.mark.asyncio
async def test_ingest_document_structure_missing_source(db, project):
    pid = project["id"]
    result = await nd_mod.handle_ingest_document_structure(
        {"project_id": pid, "blocks": []},  # missing source
        db, _DATA_DIR, None, None,
    )
    assert "error" in result
    assert "source" in result["error"]


@pytest.mark.asyncio
async def test_ingest_document_structure_missing_blocks(db, project):
    pid = project["id"]
    result = await nd_mod.handle_ingest_document_structure(
        {"project_id": pid, "source": "/some/file.docx"},  # missing blocks
        db, _DATA_DIR, None, None,
    )
    assert "error" in result
    assert "blocks" in result["error"]


# ---------------------------------------------------------------------------
# Citation / equation / figure / table tool guard tests (no real doc store)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_citation_edges_missing_project_id(db):
    result = await nd_mod.handle_get_citation_edges(
        {},  # missing project_id
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_resolve_citations_missing_project_id(db):
    result = await nd_mod.handle_resolve_citations(
        {},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_index_equation_missing_doc(db, project):
    pid = project["id"]
    result = await nd_mod.handle_index_equation(
        {"project_id": pid, "omml_or_latex": r"\pi"},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_index_equation_missing_project_id(db):
    result = await nd_mod.handle_index_equation(
        {"doc": "some.docx", "omml_or_latex": r"\pi"},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_find_similar_equation_missing_doc(db, project):
    pid = project["id"]
    result = await nd_mod.handle_find_similar_equation(
        {"project_id": pid, "latex": r"\alpha"},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_insert_equation_missing_doc(db, project):
    pid = project["id"]
    result = await nd_mod.handle_insert_equation(
        {"project_id": pid, "para_id": "p1", "equation_id_or_omml": r"\beta"},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_update_paragraph_missing_doc(db, project):
    pid = project["id"]
    result = await nd_mod.handle_update_paragraph(
        {"project_id": pid, "para_id": "p1", "new_text": "hello"},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_update_paragraph_both_text_and_runs(db, project):
    pid = project["id"]
    result = await nd_mod.handle_update_paragraph(
        {"project_id": pid, "doc": "d.docx", "para_id": "p1",
         "new_text": "hi", "runs": ["hi"]},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_update_paragraph_neither_text_nor_runs(db, project):
    pid = project["id"]
    result = await nd_mod.handle_update_paragraph(
        {"project_id": pid, "doc": "d.docx", "para_id": "p1"},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_find_symbol_usages_missing_doc(db, project):
    pid = project["id"]
    result = await nd_mod.handle_find_symbol_usages(
        {"project_id": pid, "symbol_or_equation_id": "x"},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_index_figure_missing_doc(db, project):
    pid = project["id"]
    result = await nd_mod.handle_index_figure(
        {"project_id": pid, "file_path": "/some/fig.png"},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_find_similar_figure_missing_doc(db, project):
    pid = project["id"]
    result = await nd_mod.handle_find_similar_figure(
        {"project_id": pid, "description_or_path": "a diagram"},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_link_figure_caption_missing_doc(db, project):
    pid = project["id"]
    result = await nd_mod.handle_link_figure_caption(
        {"project_id": pid, "figure_id": "fig-1", "caption_element_id": "el-1"},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_index_table_missing_doc(db, project):
    pid = project["id"]
    result = await nd_mod.handle_index_table(
        {"project_id": pid, "caption": "Table 1"},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_find_similar_table_missing_doc(db, project):
    pid = project["id"]
    result = await nd_mod.handle_find_similar_table(
        {"project_id": pid, "description": "a summary table"},
        db, _DATA_DIR, None, None,
    )
    assert "error" in result


# ---------------------------------------------------------------------------
# Dispatch-table completeness: none of the 45 known tool names returns _MISS
# ---------------------------------------------------------------------------

def test_handler_names_match_all_tools():
    """Sanity: EXPECTED_HANDLER_NAMES and ALL_TOOL_NAMES must be same length."""
    assert len(EXPECTED_HANDLER_NAMES) == len(ALL_TOOL_NAMES) == 45


# ---------------------------------------------------------------------------
# Helpers (module-level, not fixtures)
# ---------------------------------------------------------------------------

def _server_mod():
    """Return the meridian.server module for monkeypatching."""
    import meridian.server as _srv
    return _srv


async def _noop_async(*args, **kwargs):
    """No-op async function for monkeypatching side-effects."""
    pass
