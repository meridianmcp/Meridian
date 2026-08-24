"""Tests for 7479e427 — unify starter/goal/delta/full handoff modes around
one immutable proposal-run scope (proposal id, parent/child items,
dependencies/waves, pointer resolution states, required capabilities, HITL
gates, omitted/deferred items, content hash, truncation state, and
executable/degraded reasons), and enforce "a syntactically incomplete or
contradictory handoff must never be emitted as executable."

Covers:
  * meridian/handoff.py — build_proposal_run_scope,
    _validate_proposal_run_scope_integrity, _build_proposal_scope_clause,
    and their wiring into _build_quick_start_goal/generate_handoff for all
    four executor-facing modes (starter/compact, goal, delta, full).
  * meridian/mcp/handlers/session_tools.py — checkpoint()'s next_goal now
    agreeing with the same executable batch instead of a naive, unfiltered
    pending_items[:8] slice.
  * meridian/mcp/handlers/sprint_tools.py — get_sprint_progress surfacing
    live pending HITL gates (previously only visible in planner-mode prose).
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module
import meridian.server as srv


# ---------------------------------------------------------------------------
# build_proposal_run_scope / _validate_proposal_run_scope_integrity —
# pure, synchronous, no DB. Pins the exact contract shape and the
# never-executable-when-contradictory rules.
# ---------------------------------------------------------------------------


def test_build_proposal_run_scope_basic_shape():
    items = [
        {"id": "a1", "status": "pending", "parent_id": None, "depends_on": None, "wave": "wave-1"},
        {"id": "b2", "status": "pending", "parent_id": "a1", "depends_on": "a1", "wave": "wave-2"},
    ]
    scope = handoff_module.build_proposal_run_scope(
        items, project_id="proj-1", effective_version="v1",
    )
    assert scope["proposal_id"]
    assert scope["mode"] == "goal"
    assert scope["project_id"] == "proj-1"
    assert scope["version"] == "v1"
    assert {i["id"] for i in scope["items"]} == {"a1", "b2"}
    assert scope["waves"] == {"wave-1": ["a1"], "wave-2": ["b2"]}
    assert scope["dependencies"] == {"b2": "a1"}
    assert scope["pointer_resolution_states"] == {"a1": "unknown", "b2": "unknown"}
    assert scope["required_capabilities"] == {}
    assert scope["hitl_gates"] == []
    assert scope["omitted_items"] == []
    assert scope["content_hash"]
    assert scope["truncated"] is False
    assert scope["executable"] is True
    assert scope["degraded"] is False
    assert scope["executable_reasons"] == []


def test_build_proposal_run_scope_content_hash_is_deterministic():
    items = [{"id": "a1", "status": "pending", "parent_id": None, "depends_on": None, "wave": None}]
    s1 = handoff_module.build_proposal_run_scope(items, project_id="p", effective_version="v1")
    s2 = handoff_module.build_proposal_run_scope(items, project_id="p", effective_version="v1")
    assert s1["content_hash"] == s2["content_hash"]
    assert s1["proposal_id"] == s2["proposal_id"]
    # A different item set must hash differently.
    items2 = [{"id": "a2", "status": "pending", "parent_id": None, "depends_on": None, "wave": None}]
    s3 = handoff_module.build_proposal_run_scope(items2, project_id="p", effective_version="v1")
    assert s3["content_hash"] != s1["content_hash"]


def test_build_proposal_run_scope_omitted_items_carry_reason():
    items = [{"id": "a1", "status": "pending", "parent_id": None, "depends_on": None, "wave": None}]
    scope = handoff_module.build_proposal_run_scope(
        items, project_id="p", effective_version=None,
        omitted_items=[{"id": "b2", "reason": "backburner"}, {"id": "c3", "reason": "manual"}],
    )
    assert {"id": "b2", "reason": "backburner"} in scope["omitted_items"]
    assert {"id": "c3", "reason": "manual"} in scope["omitted_items"]
    # Omitted items are never counted as in-scope.
    assert {i["id"] for i in scope["items"]} == {"a1"}


def test_build_proposal_run_scope_pointer_resolution_states_reflect_annotation():
    items = [
        {
            "id": "a1", "status": "pending",
            "pointer_resolution_status": {"strict_satisfied": True, "target_resolved": True},
        },
        {
            "id": "b2", "status": "pending",
            "pointer_resolution_status": {"structural_valid": True, "target_resolved": False},
        },
        {"id": "c3", "status": "pending", "pointer_resolution_status": {}},
        {"id": "d4", "status": "pending"},  # never annotated at all
    ]
    scope = handoff_module.build_proposal_run_scope(items, project_id="p", effective_version=None)
    assert scope["pointer_resolution_states"]["a1"] == "resolved"
    assert scope["pointer_resolution_states"]["b2"] == "structural_only"
    assert scope["pointer_resolution_states"]["c3"] == "unresolved"
    assert scope["pointer_resolution_states"]["d4"] == "unknown"


def test_build_proposal_run_scope_degraded_capability_visible_but_still_executable():
    """2026-08-21 investigation note: a degraded code-intel/graph-search
    signal must remain VISIBLE in executable status, not silently dropped —
    but degraded alone must not by itself make the scope non-executable."""
    items = [{"id": "a1", "status": "pending"}]
    scope = handoff_module.build_proposal_run_scope(
        items, project_id="p", effective_version=None,
        capability_status={"graph_search_availability": {"status": "degraded", "reason": "no tunnel"}},
    )
    assert scope["required_capabilities"]["graph_search_availability"]["status"] == "degraded"
    assert scope["executable"] is True
    assert scope["degraded"] is False
    assert scope["executable_reasons"] == []


# --- "never executable when contradictory" ---------------------------------


def test_proposal_scope_non_executable_when_required_capability_failed():
    items = [{"id": "a1", "status": "pending"}]
    scope = handoff_module.build_proposal_run_scope(
        items, project_id="p", effective_version=None,
        capability_status={"freshness_requery": {"status": "failed", "reason": "boom"}},
    )
    assert scope["executable"] is False
    assert scope["degraded"] is True
    assert any(r.startswith("required_capability_failed:") for r in scope["executable_reasons"])


def test_proposal_scope_non_executable_when_depends_on_omitted_item():
    """The real contradiction this item's acceptance criteria targets: an
    item this render calls executable-in-scope depends on another item THIS
    SAME render explicitly excluded (wave-gate-pending here) — i.e. the
    scope names something as ready while also declaring its own
    prerequisite blocked."""
    items = [{"id": "a1", "status": "pending", "depends_on": "blocked-1"}]
    scope = handoff_module.build_proposal_run_scope(
        items, project_id="p", effective_version=None,
        omitted_items=[{"id": "blocked-1", "reason": "wave_gate_pending"}],
    )
    assert scope["executable"] is False
    assert "depends_on_omitted_item:a1->blocked-1" in scope["executable_reasons"]


def test_proposal_scope_depends_on_a_done_item_not_in_batch_is_not_contradictory():
    """A dependency that is simply absent (e.g. already done, outside
    version scope) is the ORDINARY case — never flagged."""
    items = [{"id": "a1", "status": "pending", "depends_on": "already-done-item"}]
    scope = handoff_module.build_proposal_run_scope(items, project_id="p", effective_version=None)
    assert scope["executable"] is True
    assert scope["executable_reasons"] == []


def test_proposal_scope_non_executable_when_duplicate_item_id():
    items = [
        {"id": "a1", "status": "pending"},
        {"id": "a1", "status": "pending"},
    ]
    scope = handoff_module.build_proposal_run_scope(items, project_id="p", effective_version=None)
    assert scope["executable"] is False
    assert "duplicate_item_id:a1" in scope["executable_reasons"]


def test_proposal_scope_non_executable_when_truncated():
    """Canonical receiver contract: 'Any TRUNCATED marker makes the compact
    handoff non-ready/non-executable.'"""
    items = [{"id": "a1", "status": "pending"}]
    scope = handoff_module.build_proposal_run_scope(
        items, project_id="p", effective_version=None, truncated=True,
    )
    assert scope["executable"] is False
    assert "content_truncated" in scope["executable_reasons"]


def test_validate_proposal_run_scope_integrity_clean_scope_has_no_reasons():
    reasons = handoff_module._validate_proposal_run_scope_integrity(
        [{"id": "a1"}, {"id": "b2"}],
        dependencies={"b2": "a1"},
        omitted_ids=set(),
        capability_status={"x": {"status": "verified"}},
        hitl_gates=[],
        truncated=False,
    )
    assert reasons == []


# ---------------------------------------------------------------------------
# _build_proposal_scope_clause — pure rendering
# ---------------------------------------------------------------------------


def test_build_proposal_scope_clause_empty_when_scope_falsy():
    assert handoff_module._build_proposal_scope_clause(None) == ""
    assert handoff_module._build_proposal_scope_clause({}) == ""


def test_build_proposal_scope_clause_compact_form():
    scope = handoff_module.build_proposal_run_scope(
        [{"id": "a1", "status": "pending"}], project_id="p", effective_version=None,
    )
    clause = handoff_module._build_proposal_scope_clause(scope, compact=True)
    assert "<proposal_scope " in clause
    assert 'executable="true"' in clause
    assert f'proposal_id="{scope["proposal_id"]}"' in clause
    assert "1 item(s) in scope" in clause
    # Compact form never inlines the full per-item detail.
    assert "waves:" not in clause


def test_build_proposal_scope_clause_full_form_includes_hitl_and_omitted_detail():
    scope = handoff_module.build_proposal_run_scope(
        [{"id": "a1", "status": "pending"}], project_id="p", effective_version=None,
        omitted_items=[{"id": "b2", "reason": "manual"}],
        hitl_gates=[{"id": "hitl-123", "urgency": "normal", "question": "Which approach?"}],
    )
    clause = handoff_module._build_proposal_scope_clause(scope, compact=False)
    assert "<proposal_scope " in clause
    assert "b2" in clause and "manual" in clause
    assert "hitl-123"[:8] in clause
    assert "Which approach?" in clause


def test_build_proposal_scope_clause_non_executable_surfaces_reasons():
    scope = handoff_module.build_proposal_run_scope(
        [{"id": "a1", "status": "pending"}], project_id="p", effective_version=None,
        truncated=True,
    )
    clause = handoff_module._build_proposal_scope_clause(scope, compact=True)
    assert 'executable="false"' in clause
    assert "content_truncated" in clause


# ---------------------------------------------------------------------------
# _build_quick_start_goal — proposal_scope_out wiring, no DB. Mirrors the
# existing direct-call convention already used elsewhere in this file's
# sibling tests (test_build_quick_start_goal_flat_batches_have_no_serial_barrier_directive).
# ---------------------------------------------------------------------------


def test_build_quick_start_goal_proposal_scope_out_is_noop_by_default():
    """Every pre-existing call site (proposal_scope_out omitted) sees zero
    behaviour change — the returned string is unaffected either way."""
    items = [{"id": "a1", "version": None}]
    goal = handoff_module._build_quick_start_goal(items)
    assert "<proposal_scope" not in goal


def test_build_quick_start_goal_populates_proposal_scope_out():
    items = [{"id": "a1", "version": None}]
    scope_out: dict = {}
    handoff_module._build_quick_start_goal(
        items, proposal_scope_out=scope_out, handoff_mode="delta",
    )
    assert scope_out["mode"] == "delta"
    assert {i["id"] for i in scope_out["items"]} == {"a1"}
    assert scope_out["executable"] is True


def test_build_quick_start_goal_proposal_scope_out_reports_truncated_when_capped():
    """The 248c0bb9 deterministic per-item cap firing is exactly the
    'truncated' signal the unified scope must react to — NOT the post-token
    byte-budget backstop (per the item's own investigation notes: 'use
    deterministic bounded profiles/continuation rather than post-token
    truncation')."""
    items = [
        {"id": f"item{n}", "version": None, "required_tool": "some_tool"}
        for n in range(3)
    ]
    scope_out: dict = {}
    handoff_module._build_quick_start_goal(
        items, full_contract_max_items=1, proposal_scope_out=scope_out,
    )
    assert scope_out["truncated"] is True
    assert scope_out["executable"] is False
    assert "content_truncated" in scope_out["executable_reasons"]


def test_build_quick_start_goal_proposal_scope_out_not_truncated_when_under_cap():
    items = [{"id": "item0", "version": None, "required_tool": "some_tool"}]
    scope_out: dict = {}
    handoff_module._build_quick_start_goal(
        items, full_contract_max_items=15, proposal_scope_out=scope_out,
    )
    assert scope_out["truncated"] is False
    assert scope_out["executable"] is True


def test_build_quick_start_goal_proposal_scope_out_empty_board():
    """The empty-board branch (item_ids falsy) must still populate a valid,
    executable scope with zero items rather than skipping the out-param."""
    scope_out: dict = {}
    handoff_module._build_quick_start_goal([], proposal_scope_out=scope_out)
    assert scope_out["items"] == []
    assert scope_out["executable"] is True


# ---------------------------------------------------------------------------
# generate_handoff — end-to-end, all four executor-facing modes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_handoff_goal_mode_exposes_proposal_scope(db, tmp_path):
    p = await db_module.create_project(db, "7479e427-goal-scope")
    it = await db_module.add_sprint_item(
        db, p["id"], "v1", "solo item", prospect_bypass=True,
    )
    scope: dict = {}
    _path, content, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
        proposal_scope=scope,
    )
    assert "<proposal_scope " in content
    assert 'executable="true"' in content
    assert scope["items"][0]["id"] == it["id"]
    assert scope["executable"] is True
    assert scope["proposal_id"]


@pytest.mark.asyncio
async def test_generate_handoff_delta_mode_exposes_proposal_scope(db, tmp_path):
    p = await db_module.create_project(db, "7479e427-delta-scope")
    it = await db_module.add_sprint_item(
        db, p["id"], "v1", "solo item", prospect_bypass=True,
    )
    scope: dict = {}
    _path, content, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="delta",
        proposal_scope=scope,
    )
    assert "<proposal_scope " in content
    assert scope["items"][0]["id"] == it["id"]


@pytest.mark.asyncio
async def test_generate_handoff_full_mode_exposes_proposal_scope(db, tmp_path):
    p = await db_module.create_project(db, "7479e427-full-scope")
    it = await db_module.add_sprint_item(
        db, p["id"], "v1", "solo item", prospect_bypass=True,
    )
    scope: dict = {}
    _path, content, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="full",
        proposal_scope=scope,
    )
    assert "<proposal_scope " in content
    assert scope["items"][0]["id"] == it["id"]
    # full's content-cleanliness contract (test_handoff_generates_clean_markdown)
    # must remain intact — the new tag never carries a literal test_cmd string.
    assert "pixi run test" not in content


@pytest.mark.asyncio
async def test_generate_handoff_starter_mode_exposes_proposal_scope_via_outparam_only(db, tmp_path):
    """Starter's hard <=20-non-empty-line budget (test_handoff_starter_mode)
    has no room for a new inline tag (the SAME f471c4b8 constraint that kept
    <project_start_config> out of starter) -- the structured object must
    still be exposed via the out-param."""
    p = await db_module.create_project(db, "7479e427-starter-scope")
    it = await db_module.add_sprint_item(
        db, p["id"], "v1", "solo item", prospect_bypass=True,
    )
    scope: dict = {}
    _path, content, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="starter",
        proposal_scope=scope,
    )
    assert "<proposal_scope" not in content
    assert scope["items"][0]["id"] == it["id"]
    lines = [l for l in content.splitlines() if l]
    assert len(lines) <= 20, f"starter mode must stay <=20 non-empty lines, got {len(lines)}"


@pytest.mark.asyncio
async def test_proposal_scope_agrees_across_starter_goal_delta_full(db, tmp_path):
    """The core unification acceptance criterion: the SAME underlying board
    must produce the SAME in-scope item id set across every mode."""
    p = await db_module.create_project(db, "7479e427-cross-mode-scope")
    pid = p["id"]
    i1 = await db_module.add_sprint_item(db, pid, "v1", "item one", prospect_bypass=True)
    i2 = await db_module.add_sprint_item(db, pid, "v1", "item two", prospect_bypass=True)
    expected = {i1["id"], i2["id"]}

    scopes: dict[str, dict] = {}
    for mode in ("starter", "goal", "delta", "full"):
        scope: dict = {}
        await handoff_module.generate_handoff(
            db, pid, str(tmp_path), skip_ai_summary=True, mode=mode,
            proposal_scope=scope,
        )
        scopes[mode] = scope

    for mode, scope in scopes.items():
        got = {i["id"] for i in scope["items"]}
        assert got == expected, f"mode={mode} disagreed on in-scope items: {got}"


@pytest.mark.asyncio
async def test_proposal_scope_surfaces_pending_hitl_gate(db, tmp_path):
    """Prior to this item, a pending HITL request was only ever visible in
    planner-mode prose. Every executor-facing mode must now carry it too."""
    p = await db_module.create_project(db, "7479e427-hitl-visible")
    pid = p["id"]
    await db_module.add_sprint_item(db, pid, "v1", "solo item", prospect_bypass=True)
    await db_module.request_hitl(db, pid, "Which auth provider should we use?")

    scope: dict = {}
    _path, content, _amended = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="delta",
        proposal_scope=scope,
    )
    assert len(scope["hitl_gates"]) == 1
    assert scope["hitl_gates"][0]["question"] == "Which auth provider should we use?"
    assert "Which auth provider should we use?" in content


@pytest.mark.asyncio
async def test_proposal_scope_omitted_items_include_manual_and_backburner(db, tmp_path):
    p = await db_module.create_project(db, "7479e427-omitted-items")
    pid = p["id"]
    normal = await db_module.add_sprint_item(
        db, pid, "v1", "normal item", prospect_bypass=True,
    )
    manual = await db_module.add_sprint_item(
        db, pid, "v1", "MANUAL: publish blog post",
    )
    backburner = await db_module.add_sprint_item(
        db, pid, "v1", "backburner item", track="backburner",
    )

    scope: dict = {}
    await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
        proposal_scope=scope,
    )
    in_scope_ids = {i["id"] for i in scope["items"]}
    assert normal["id"] in in_scope_ids
    assert manual["id"] not in in_scope_ids
    assert backburner["id"] not in in_scope_ids
    omitted_by_id = {o["id"]: o["reason"] for o in scope["omitted_items"]}
    assert omitted_by_id[manual["id"]] == "manual"
    assert omitted_by_id[backburner["id"]] == "backburner"


# ---------------------------------------------------------------------------
# checkpoint() (meridian/mcp/handlers/session_tools.py) — next_goal agrees
# with the SAME executable batch generate_handoff's own proposal scope used,
# instead of a naive, unfiltered pending_items[:8] slice.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_next_goal_excludes_manual_item(db, tmp_path):
    """Before this fix, checkpoint()'s next_goal was built from a raw
    get_sprint_items(status='pending') slice with NO manual/backburner/
    unprospected/wave-gate exclusion applied -- disagreeing with the SAME
    generate_handoff(mode='delta') call it had just made a few lines above.
    A MANUAL item must never appear in the CLAIMABLE <sprint_items> batch of
    the executor-facing next_goal.

    455cfc36 — next_goal is now the SAME canonical, token-embedded goal
    string generate_handoff renders (see goal_string_out), not an
    independently re-assembled bare id list. That canonical block reports
    excluded items transparently in a separate <exclusions> tag (the same
    omitted_items/reason reporting `scope["omitted_items"]` already carries)
    -- so the manual item's id DOES now legitimately appear somewhere in
    next_goal (inside <exclusions>), it just must never appear inside the
    claimable <sprint_items> section."""
    p = await db_module.create_project(db, "7479e427-checkpoint-manual")
    pid = p["id"]
    normal = await db_module.add_sprint_item(
        db, pid, "v1", "normal item", prospect_bypass=True,
    )
    manual = await db_module.add_sprint_item(
        db, pid, "v1", "MANUAL: publish blog post",
    )
    s = await db_module.register_session(db, pid, "ckpt-manual-session")
    result = await srv._dispatch_mcp_tool(
        "checkpoint", {"session_id": s["id"], "project_id": pid}, db, str(tmp_path),
    )
    next_goal = result["next_goal"]
    assert normal["id"] in next_goal
    _items_start = next_goal.index("<sprint_items>")
    _items_end = next_goal.index("</sprint_items>")
    claimable_block = next_goal[_items_start:_items_end]
    assert normal["id"] in claimable_block
    assert manual["id"] not in claimable_block
    # The manual item is still transparently reported as excluded, same as
    # every other full/delta/goal canonical handoff already does.
    assert "<exclusions>" in next_goal
    assert manual["id"] in next_goal[next_goal.index("<exclusions>"):]
    assert manual["id"][:8] not in result["pending_ids"]
    assert "proposal_scope" in result
    assert result["proposal_scope"]["executable"] is True


@pytest.mark.asyncio
async def test_checkpoint_still_scopes_to_session_version(db, tmp_path):
    """Regression guard: the 660314c1 version-scoping fix (pending_ids/
    next_goal scoped to the calling session's own sprint_version) must
    survive the proposal-scope-based next_goal rewrite."""
    p = await db_module.create_project(db, "7479e427-checkpoint-version")
    pid = p["id"]
    in_scope = await db_module.add_sprint_item(
        db, pid, "v0.2.6", "in scope item", prospect_bypass=True,
    )
    out_of_scope = await db_module.add_sprint_item(
        db, pid, "v0.2.5", "other version item", prospect_bypass=True,
    )
    s = await db_module.register_session(
        db, pid, "ckpt-scoped-session-2", sprint_version="v0.2.6",
    )
    result = await srv._dispatch_mcp_tool(
        "checkpoint", {"session_id": s["id"], "project_id": pid}, db, str(tmp_path),
    )
    assert in_scope["id"] in result["next_goal"]
    assert out_of_scope["id"] not in result["next_goal"]


# ---------------------------------------------------------------------------
# get_sprint_progress (meridian/mcp/handlers/sprint_tools.py) — live HITL
# gate visibility.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_sprint_progress_surfaces_pending_hitl_gate(db, tmp_path):
    p = await db_module.create_project(db, "7479e427-progress-hitl")
    pid = p["id"]
    await db_module.request_hitl(db, pid, "Should we ship v2 now?")
    result = await srv._dispatch_mcp_tool(
        "get_sprint_progress", {"project_id": pid}, db, str(tmp_path),
    )
    assert "hitl_gates" in result
    assert result["hitl_gates"]["count"] == 1
    assert result["hitl_gates"]["items"][0]["question"] == "Should we ship v2 now?"


@pytest.mark.asyncio
async def test_get_sprint_progress_no_hitl_gates_key_when_none_pending(db, tmp_path):
    p = await db_module.create_project(db, "7479e427-progress-no-hitl")
    result = await srv._dispatch_mcp_tool(
        "get_sprint_progress", {"project_id": p["id"]}, db, str(tmp_path),
    )
    assert "hitl_gates" not in result
