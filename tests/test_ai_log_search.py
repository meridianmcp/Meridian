"""d26b9943 (R2-B) — IMPLEMENT: exact-first scoped AI-log search and
deterministic bounded indexing APIs.

SCOPE: this file tests the NEW surface this sprint item adds on top of the
existing 9e83be4a/ea972129/c0168425/79491e26 ai_log scaffold (already
covered by tests/test_ai_log_contract.py, tests/test_ai_log_retention.py,
tests/test_ai_log_artifacts.py, tests/test_ai_log_timeline.py — none of that
per-module coverage is duplicated here):

  1. meridian.db.ai_log.search_events — exact-match filtering per field
     (project isolation, session_id, tenant_id, correlation_id,
     parent_event_id, actor_kind, actor_id, event_type), inclusive
     occurred_at range bounds, deterministic newest-first ordering with a
     stable tiebreak, bounded/cursored pagination (boundary conditions:
     off-by-one, empty page, cursor past the end), and the honest
     ``index_status`` field.
  2. The "exact identity filters are never weakened" contract: combining
     filters only ever narrows the result set (a guard against a future
     semantic-rerank parameter silently widening or replacing an exact
     match — see search_events' own docstring).
  3. meridian.db.ai_log.AiLogStore.search — the facade delegate.
  4. meridian.mcp.handler._handle_task_tools("search_ai_log", ...) — MCP
     dispatch, filter forwarding, and pagination fields.
  5. A schema-registration smoke test: search_ai_log appears in every
     mcp_tools.py registry a tool needs (schema list, read-only set,
     category map, role-relevance map, workflow-tier map, title overrides)
     and is exposed correctly through the real tools/list surface.

Deliberately DEFERRED (see meridian/db/ai_log.py's "d26b9943" docstring
section and this item's own discovery notes): a lexical (FTS/BM25) or
semantic index of any kind. Nothing in this codebase indexes these rows
today, so this file does NOT test a "resolving"/"building" index_status —
it tests that the field is always, honestly, "exact_only".
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian.db import ai_log as ai_log_module


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


# ---------------------------------------------------------------------------
# 1. Exact-match filtering, per field
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_events_project_isolation(db):
    pid_a = await _project(db, "ai-log-search-scope-a")
    pid_b = await _project(db, "ai-log-search-scope-b")
    await db_module.append_event(db, pid_a, "a.one", "system")
    await db_module.append_event(db, pid_b, "b.one", "system")

    result = await ai_log_module.search_events(db, pid_a)

    assert result["total_count"] == 1
    assert len(result["events"]) == 1
    assert result["events"][0]["event_type"] == "a.one"
    assert result["events"][0]["project_id"] == pid_a


@pytest.mark.asyncio
async def test_search_events_filters_by_session_id(db):
    pid = await _project(db, "ai-log-search-session")
    await db_module.append_event(db, pid, "tool.invoked", "tool", session_id="s1")
    await db_module.append_event(db, pid, "tool.invoked", "tool", session_id="s2")

    result = await ai_log_module.search_events(db, pid, session_id="s1")

    assert result["total_count"] == 1
    assert result["events"][0]["session_id"] == "s1"
    assert result["filters"] == {"session_id": "s1"}


@pytest.mark.asyncio
async def test_search_events_filters_by_tenant_id(db):
    pid = await _project(db, "ai-log-search-tenant")
    await db_module.append_event(db, pid, "a.one", "system", tenant_id="t1")
    await db_module.append_event(db, pid, "a.two", "system", tenant_id="t2")

    result = await ai_log_module.search_events(db, pid, tenant_id="t1")

    assert [e["event_type"] for e in result["events"]] == ["a.one"]


@pytest.mark.asyncio
async def test_search_events_filters_by_correlation_id(db):
    pid = await _project(db, "ai-log-search-correlation")
    await db_module.append_event(db, pid, "tool.invoked", "tool", correlation_id="run-a")
    await db_module.append_event(db, pid, "tool.invoked", "tool", correlation_id="run-b")

    result = await ai_log_module.search_events(db, pid, correlation_id="run-a")

    assert result["total_count"] == 1
    assert result["events"][0]["correlation_id"] == "run-a"


@pytest.mark.asyncio
async def test_search_events_filters_by_parent_event_id(db):
    pid = await _project(db, "ai-log-search-parent")
    parent = await db_module.append_event(db, pid, "tool.invoked", "tool")
    await db_module.append_event(
        db, pid, "tool.completed", "tool", parent_event_id=parent["id"],
    )
    await db_module.append_event(db, pid, "tool.invoked", "tool")

    result = await ai_log_module.search_events(db, pid, parent_event_id=parent["id"])

    assert result["total_count"] == 1
    assert result["events"][0]["event_type"] == "tool.completed"


@pytest.mark.asyncio
async def test_search_events_filters_by_actor_kind(db):
    pid = await _project(db, "ai-log-search-actor-kind")
    await db_module.append_event(db, pid, "a.one", "tool")
    await db_module.append_event(db, pid, "a.two", "human")

    result = await ai_log_module.search_events(db, pid, actor_kind="human")

    assert result["total_count"] == 1
    assert result["events"][0]["event_type"] == "a.two"


@pytest.mark.asyncio
async def test_search_events_filters_by_actor_id(db):
    pid = await _project(db, "ai-log-search-actor-id")
    await db_module.append_event(db, pid, "a.one", "tool", actor_id="get_sprint_items")
    await db_module.append_event(db, pid, "a.two", "tool", actor_id="log_task")

    result = await ai_log_module.search_events(db, pid, actor_id="log_task")

    assert result["total_count"] == 1
    assert result["events"][0]["event_type"] == "a.two"


@pytest.mark.asyncio
async def test_search_events_filters_by_event_type(db):
    pid = await _project(db, "ai-log-search-event-type")
    await db_module.append_event(db, pid, "session.started", "session")
    await db_module.append_event(db, pid, "tool.invoked", "tool")

    result = await ai_log_module.search_events(db, pid, event_type="session.started")

    assert result["total_count"] == 1
    assert result["events"][0]["event_type"] == "session.started"


@pytest.mark.asyncio
async def test_search_events_occurred_at_range_is_inclusive_both_ends(db):
    pid = await _project(db, "ai-log-search-occurred-range")
    await db_module.append_event(
        db, pid, "a.early", "system", occurred_at="2026-01-01T00:00:00.000Z",
    )
    await db_module.append_event(
        db, pid, "a.mid", "system", occurred_at="2026-01-02T00:00:00.000Z",
    )
    await db_module.append_event(
        db, pid, "a.late", "system", occurred_at="2026-01-03T00:00:00.000Z",
    )

    result = await ai_log_module.search_events(
        db, pid,
        since_occurred_at="2026-01-01T00:00:00.000Z",
        until_occurred_at="2026-01-02T00:00:00.000Z",
    )

    assert {e["event_type"] for e in result["events"]} == {"a.early", "a.mid"}


@pytest.mark.asyncio
async def test_search_events_requires_project_id():
    with pytest.raises(ValueError):
        await ai_log_module.search_events(None, "")


# ---------------------------------------------------------------------------
# 2. "Exact identity filters are never weakened" contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_events_filters_always_and_together_never_widen(db):
    """Two identity filters combined must return the INTERSECTION, never the
    union — the concrete, testable form of "adding more filters only ever
    narrows the result" that search_events' own docstring promises as a
    guard against any future fuzzy/semantic parameter silently overriding an
    exact predicate."""
    pid = await _project(db, "ai-log-search-and-guard")
    await db_module.append_event(
        db, pid, "tool.invoked", "tool", session_id="s1", actor_id="log_task",
    )
    await db_module.append_event(
        db, pid, "tool.invoked", "tool", session_id="s1", actor_id="get_tasks",
    )
    await db_module.append_event(
        db, pid, "tool.invoked", "tool", session_id="s2", actor_id="log_task",
    )

    both = await ai_log_module.search_events(
        db, pid, session_id="s1", actor_id="log_task",
    )
    assert both["total_count"] == 1

    session_only = await ai_log_module.search_events(db, pid, session_id="s1")
    assert session_only["total_count"] == 2

    actor_only = await ai_log_module.search_events(db, pid, actor_id="log_task")
    assert actor_only["total_count"] == 2

    # Combined result must be a SUBSET of each individual filter's result —
    # never larger than either, i.e. AND semantics, never OR.
    both_ids = {e["id"] for e in both["events"]}
    assert both_ids <= {e["id"] for e in session_only["events"]}
    assert both_ids <= {e["id"] for e in actor_only["events"]}


# ---------------------------------------------------------------------------
# 3. Deterministic ordering + tiebreak
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_events_orders_newest_recorded_first(db):
    pid = await _project(db, "ai-log-search-order")
    first = await db_module.append_event(db, pid, "a.one", "system")
    second = await db_module.append_event(db, pid, "a.two", "system")
    # recorded_at is DB-assigned at second precision (see db.ai_log's own
    # docstring) — two inserts issued back-to-back in the same test can
    # legitimately land in the same wall-clock second, which would make
    # this assertion a race on the id-tiebreak instead of a check of
    # recorded_at ordering. Pin distinct recorded_at values explicitly
    # (same technique tests/test_ai_log_contract_matrix.py's isolation test
    # already uses) so this test exercises ordering, not timing luck.
    await db.execute(
        "UPDATE ai_log_events SET recorded_at = '2026-01-01 00:00:00' WHERE id = ?",
        (first["id"],),
    )
    await db.execute(
        "UPDATE ai_log_events SET recorded_at = '2026-01-01 00:00:01' WHERE id = ?",
        (second["id"],),
    )
    await db.commit()

    result = await ai_log_module.search_events(db, pid)

    assert [e["id"] for e in result["events"]] == [second["id"], first["id"]]


@pytest.mark.asyncio
async def test_search_events_is_deterministic_under_a_recorded_at_tie(db):
    pid = await _project(db, "ai-log-search-tiebreak")
    e1 = await db_module.append_event(db, pid, "a.tick", "system")
    e2 = await db_module.append_event(db, pid, "a.tick", "system")
    e3 = await db_module.append_event(db, pid, "a.tick", "system")
    # Force an identical recorded_at across all three rows — the realistic
    # same-second-burst case list_events/build_run_timeline already guard
    # against with an `id` tiebreak; search_events must too.
    for row in (e1, e2, e3):
        await db.execute(
            "UPDATE ai_log_events SET recorded_at = '2026-01-01 00:00:00' WHERE id = ?",
            (row["id"],),
        )
    await db.commit()

    first_call = await ai_log_module.search_events(db, pid)
    second_call = await ai_log_module.search_events(db, pid)

    assert [e["id"] for e in first_call["events"]] == [e["id"] for e in second_call["events"]]
    assert len(first_call["events"]) == 3


# ---------------------------------------------------------------------------
# 4. Bounded, cursored pagination
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_events_pagination_has_more_and_next_cursor(db):
    pid = await _project(db, "ai-log-search-page-1")
    for i in range(5):
        await db_module.append_event(db, pid, f"a.tick{i}", "system")

    page1 = await ai_log_module.search_events(db, pid, limit=2, cursor=0)
    assert len(page1["events"]) == 2
    assert page1["has_more"] is True
    assert page1["next_cursor"] == 2
    assert page1["total_count"] == 5

    page2 = await ai_log_module.search_events(db, pid, limit=2, cursor=page1["next_cursor"])
    assert len(page2["events"]) == 2
    assert page2["has_more"] is True
    assert page2["next_cursor"] == 4

    page3 = await ai_log_module.search_events(db, pid, limit=2, cursor=page2["next_cursor"])
    assert len(page3["events"]) == 1
    assert page3["has_more"] is False
    assert page3["next_cursor"] is None

    # Pages are disjoint and together cover every row exactly once.
    seen_ids = [e["id"] for p in (page1, page2, page3) for e in p["events"]]
    assert len(seen_ids) == len(set(seen_ids)) == 5


@pytest.mark.asyncio
async def test_search_events_pagination_cursor_past_end_returns_empty_page(db):
    pid = await _project(db, "ai-log-search-page-past-end")
    await db_module.append_event(db, pid, "a.one", "system")

    result = await ai_log_module.search_events(db, pid, limit=10, cursor=50)

    assert result["events"] == []
    assert result["has_more"] is False
    assert result["next_cursor"] is None
    assert result["total_count"] == 1


@pytest.mark.asyncio
async def test_search_events_empty_project_returns_empty_page(db):
    pid = await _project(db, "ai-log-search-empty")

    result = await ai_log_module.search_events(db, pid)

    assert result["events"] == []
    assert result["has_more"] is False
    assert result["next_cursor"] is None
    assert result["total_count"] == 0


@pytest.mark.asyncio
async def test_search_events_limit_is_clamped_to_valid_range(db):
    pid = await _project(db, "ai-log-search-limit-clamp")
    for i in range(3):
        await db_module.append_event(db, pid, f"a.tick{i}", "system")

    # limit=0 is falsy, so it falls back to the default (50) — same
    # ``limit or 50`` convention list_events/export_events already use, not
    # a special "zero" case.
    zero_limit = await ai_log_module.search_events(db, pid, limit=0)
    assert len(zero_limit["events"]) == 3

    # A genuinely out-of-range (negative) limit clamps up to the floor of 1.
    negative_limit = await ai_log_module.search_events(db, pid, limit=-5)
    assert len(negative_limit["events"]) == 1

    huge_limit = await ai_log_module.search_events(db, pid, limit=10_000)
    assert len(huge_limit["events"]) == 3  # only 3 rows exist; cap of 500 not hit


# ---------------------------------------------------------------------------
# 5. index_status — honest, static "exact_only" (no FTS/semantic layer exists)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_events_index_status_is_exact_only(db):
    pid = await _project(db, "ai-log-search-index-status")
    await db_module.append_event(db, pid, "a.one", "system")

    result = await ai_log_module.search_events(db, pid)

    assert result["index_status"] == "exact_only"


@pytest.mark.asyncio
async def test_search_events_index_status_present_even_with_no_events(db):
    pid = await _project(db, "ai-log-search-index-status-empty")

    result = await ai_log_module.search_events(db, pid)

    assert result["index_status"] == "exact_only"


# ---------------------------------------------------------------------------
# 6. AiLogStore.search facade
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ai_log_store_search_delegates_correctly(db):
    pid = await _project(db, "ai-log-search-facade")
    store = db_module.AiLogStore(db, pid)
    await store.append("tool.invoked", "tool", session_id="s1")
    await store.append("tool.invoked", "tool", session_id="s2")

    result = await store.search(session_id="s1")

    assert result["total_count"] == 1
    assert result["events"][0]["session_id"] == "s1"


# ---------------------------------------------------------------------------
# 7. MCP dispatch — search_ai_log
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_dispatch_search_ai_log_forwards_filters(db, tmp_path):
    import meridian.server  # noqa: F401 — load the server before handler to avoid its import cycle
    from meridian.mcp import handler as mcp_handler

    pid = await _project(db, "ai-log-search-mcp-dispatch")
    await db_module.append_event(db, pid, "tool.invoked", "tool", session_id="s1")
    await db_module.append_event(db, pid, "tool.invoked", "tool", session_id="s2")

    result = await mcp_handler._handle_task_tools(
        "search_ai_log",
        {"project_id": pid, "session_id": "s1"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )

    assert result["total_count"] == 1
    assert result["events"][0]["session_id"] == "s1"
    assert result["index_status"] == "exact_only"


@pytest.mark.asyncio
async def test_mcp_dispatch_search_ai_log_pagination_fields(db, tmp_path):
    import meridian.server  # noqa: F401
    from meridian.mcp import handler as mcp_handler

    pid = await _project(db, "ai-log-search-mcp-pagination")
    for i in range(3):
        await db_module.append_event(db, pid, f"a.tick{i}", "system")

    page1 = await mcp_handler._handle_task_tools(
        "search_ai_log",
        {"project_id": pid, "limit": 2},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    assert len(page1["events"]) == 2
    assert page1["has_more"] is True
    assert page1["next_cursor"] == 2

    page2 = await mcp_handler._handle_task_tools(
        "search_ai_log",
        {"project_id": pid, "limit": 2, "cursor": page1["next_cursor"]},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    assert len(page2["events"]) == 1
    assert page2["has_more"] is False


@pytest.mark.asyncio
async def test_mcp_dispatch_search_ai_log_defaults_cursor_and_limit(db, tmp_path):
    """Omitting cursor/limit entirely (the common case) must not raise —
    the dispatch branch's int(...) coercion only runs when a value is
    actually present."""
    import meridian.server  # noqa: F401
    from meridian.mcp import handler as mcp_handler

    pid = await _project(db, "ai-log-search-mcp-defaults")
    await db_module.append_event(db, pid, "a.one", "system")

    result = await mcp_handler._handle_task_tools(
        "search_ai_log", {"project_id": pid},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )

    assert result["total_count"] == 1
    assert result["next_cursor"] is None
    assert result["has_more"] is False


# ---------------------------------------------------------------------------
# 8. Schema-registration smoke test — search_ai_log is wired into EVERY
#    registry mcp_tools.py needs for a tool to be correctly categorized,
#    documented, and exposed (not just schema-valid).
# ---------------------------------------------------------------------------

def test_search_ai_log_registered_in_every_mcp_tools_registry():
    from meridian import mcp_tools

    names = {t["name"] for t in mcp_tools._MCP_TOOLS_LIST}
    assert "search_ai_log" in names, "missing from _MCP_TOOLS_LIST (the tool schema)"
    assert "search_ai_log" in mcp_tools._TOOL_EXAMPLES
    assert "search_ai_log" in mcp_tools._READ_ONLY_TOOLS
    assert "search_ai_log" not in mcp_tools._DESTRUCTIVE_TOOLS
    assert mcp_tools._TOOL_CATEGORY.get("search_ai_log") == "notes"
    assert mcp_tools._TOOL_ROLE_RELEVANCE.get("search_ai_log") == "both"
    assert mcp_tools._TOOL_WORKFLOW_TIER.get("search_ai_log") == "maintenance-only"
    assert mcp_tools._TITLE_OVERRIDES.get("search_ai_log") == "Search AI Log"


@pytest.mark.asyncio
async def test_search_ai_log_exposed_via_real_tools_list_with_read_only_annotations():
    import meridian.server  # noqa: F401
    from meridian.mcp import handler as mcp_handler

    resp = await mcp_handler._handle_mcp_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        db=None, data_dir="/tmp", tenant=None,
    )
    tools_by_name = {t["name"]: t for t in resp["result"]["tools"]}

    assert "search_ai_log" in tools_by_name, "search_ai_log missing from tools/list"
    tool = tools_by_name["search_ai_log"]
    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["annotations"]["destructiveHint"] is False
    assert tool["annotations"]["idempotentHint"] is True
    assert tool["category"] == "notes"
    assert tool["role_relevance"] == "both"
    assert tool["workflow_tier"] == "maintenance-only"
    assert tool["title"] == "Search AI Log"
