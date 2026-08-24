"""Regression tests for aec043cb — P0 HANDOFF: bounded, project-scoped
executor handoffs; no workspace-context leakage; no silent full-mode default.

Covers the authoritative design decision's four intent-based resolution
cases plus the fail-safe:
  * planner session (role='planner')  -> omitted mode resolves to 'planner'
  * executor session (role='executor') -> omitted mode resolves to 'goal'
  * resumed session / continuation     -> omitted mode resolves to 'delta'
  * full                               -> only ever an EXPLICIT request
  * no session/role context at all     -> safe bounded default ('goal'),
                                           NEVER 'full'

...and the two structural leak fixes this item exists to close:
  * workspace decisions/notes (cross-project by design) must NEVER appear
    in an executor-facing handoff (goal/delta/starter/planner) — only an
    explicit mode='full' archival/diagnostic request still includes them.
  * item/project/version scoping is unchanged by the mode-resolution fix —
    a project's handoff never surfaces another project's items.

Also proves the trusted goal_token/body-binding contract (mint_handoff_token
/ verify_handoff_token) still holds for a handoff reached via the new
omitted-mode -> 'goal' default, not just an explicit mode='goal' call.
"""
from __future__ import annotations

import re

import pytest

import meridian.db as db_module
import meridian.server as srv
from meridian import handoff as handoff_module
from meridian.mcp import handler as mh


def _clear_role_state():
    """Best-effort reset of the in-process, per-test-worker session-role
    registries this module owns (mirrors the existing
    ``mh._EXECUTOR_SESSIONS.clear()`` pattern already used elsewhere in the
    suite, e.g. tests/test_core.py's context-refresh tests)."""
    mh._EXECUTOR_SESSIONS.clear()
    mh._PLANNER_SESSIONS.clear()


# ---------------------------------------------------------------------------
# 1. resolve_handoff_mode — pure unit coverage of the intent-based resolution
# ---------------------------------------------------------------------------


def test_resolve_handoff_mode_explicit_always_wins_regardless_of_context():
    """An explicit, recognized mode wins outright, even when session_role or
    a continuation signal would otherwise steer the omitted-mode default
    somewhere else."""
    handoff_module._SESSION_HANDOFF_STATE["sess-explicit-wins"] = "2026-01-01 00:00:00"
    try:
        assert handoff_module.resolve_handoff_mode(
            "full", "sess-explicit-wins", session_role="executor",
        ) == "full"
        assert handoff_module.resolve_handoff_mode(
            "starter", "sess-explicit-wins", session_role="planner",
        ) == "starter"
    finally:
        handoff_module._SESSION_HANDOFF_STATE.pop("sess-explicit-wins", None)


def test_resolve_handoff_mode_executor_role_resolves_to_goal():
    assert handoff_module.resolve_handoff_mode(
        None, "sess-exec-role", session_role="executor",
    ) == "goal"
    # An unrecognized string is treated the same as omission.
    assert handoff_module.resolve_handoff_mode(
        "garbage", "sess-exec-role", session_role="executor",
    ) == "goal"


def test_resolve_handoff_mode_planner_role_resolves_to_planner():
    assert handoff_module.resolve_handoff_mode(
        None, "sess-planner-role", session_role="planner",
    ) == "planner"


def test_resolve_handoff_mode_continuation_beats_role():
    """A genuinely resumed session (already has a handoff this process) wins
    over a role hint — 'delta' is a stronger, more specific signal than a
    role guess, exactly as the pre-existing auto-switch behaved."""
    sid = "sess-continuation-beats-role"
    handoff_module._SESSION_HANDOFF_STATE[sid] = "2026-01-01 00:00:00"
    try:
        assert handoff_module.resolve_handoff_mode(
            None, sid, session_role="executor",
        ) == "delta"
        assert handoff_module.resolve_handoff_mode(
            None, sid, session_role="planner",
        ) == "delta"
    finally:
        handoff_module._SESSION_HANDOFF_STATE.pop(sid, None)


def test_resolve_handoff_mode_unknown_intent_never_full():
    """No session_id, no role: the one non-negotiable constraint — omission
    NEVER silently resolves to 'full' — holds even in the maximally
    ambiguous case."""
    assert handoff_module.resolve_handoff_mode(None) == "goal"
    assert handoff_module.resolve_handoff_mode(None) != "full"
    assert handoff_module.resolve_handoff_mode("unrecognized-mode-string") == "goal"


# ---------------------------------------------------------------------------
# 2. End-to-end MCP dispatch: start_session(role=...) -> generate_handoff
#    (omitted mode) resolves per the session's registered role.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_session_omitted_mode_dispatches_to_goal(db):
    _clear_role_state()
    p = await db_module.create_project(db, "aec043cb-exec-session")
    await db_module.add_sprint_item(db, p["id"], "v1", "Do the thing")
    sess = await srv._dispatch_mcp_tool(
        "start_session", {"project_id": p["id"], "role": "executor"}, db, "/tmp",
    )
    sid = sess["session_id"]
    assert sid in mh._EXECUTOR_SESSIONS

    result = await srv._dispatch_mcp_tool(
        "generate_handoff", {"project_id": p["id"], "session_id": sid}, db, "/tmp",
    )
    assert result["mode"] == "goal"


@pytest.mark.asyncio
async def test_planner_session_omitted_mode_dispatches_to_planner(db):
    _clear_role_state()
    p = await db_module.create_project(db, "aec043cb-planner-session")
    sess = await srv._dispatch_mcp_tool(
        "start_session", {"project_id": p["id"], "role": "planner"}, db, "/tmp",
    )
    sid = sess["session_id"]
    assert sid in mh._PLANNER_SESSIONS
    assert sid not in mh._EXECUTOR_SESSIONS

    result = await srv._dispatch_mcp_tool(
        "generate_handoff", {"project_id": p["id"], "session_id": sid}, db, "/tmp",
    )
    assert result["mode"] == "planner"


@pytest.mark.asyncio
async def test_no_role_session_omitted_mode_dispatches_to_goal_not_full(db):
    """A session that never registered a role (plain register_session, or
    start_session with no role argument) is the 'intent cannot be
    determined' bucket — still never 'full'."""
    _clear_role_state()
    p = await db_module.create_project(db, "aec043cb-no-role-session")
    sess = await db_module.register_session(db, p["id"], "no-role-sess")
    result = await srv._dispatch_mcp_tool(
        "generate_handoff", {"project_id": p["id"], "session_id": sess["id"]}, db, "/tmp",
    )
    assert result["mode"] == "goal"
    assert result["mode"] != "full"


@pytest.mark.asyncio
async def test_executor_session_second_omitted_call_switches_to_delta(db):
    """A session's FIRST omitted-mode handoff resolves to the bounded 'goal'
    default; its SECOND resolves to 'delta' (resumed-session continuation),
    exactly as the pre-aec043cb behavior did when the first call happened to
    be 'full' — proving the continuation contract survived the default
    changing out from under it (the _mark_session_handoff_produced fix)."""
    _clear_role_state()
    p = await db_module.create_project(db, "aec043cb-exec-continuation")
    sess = await srv._dispatch_mcp_tool(
        "start_session", {"project_id": p["id"], "role": "executor"}, db, "/tmp",
    )
    sid = sess["session_id"]

    first = await srv._dispatch_mcp_tool(
        "generate_handoff", {"project_id": p["id"], "session_id": sid}, db, "/tmp",
    )
    assert first["mode"] == "goal"

    second = await srv._dispatch_mcp_tool(
        "generate_handoff", {"project_id": p["id"], "session_id": sid}, db, "/tmp",
    )
    assert second["mode"] == "delta"


@pytest.mark.asyncio
async def test_planner_session_second_omitted_call_switches_to_delta(db):
    """Same continuation guarantee for a planner-rooted session — planner
    mode also now registers _SESSION_HANDOFF_STATE (_mark_session_handoff_produced)."""
    _clear_role_state()
    p = await db_module.create_project(db, "aec043cb-planner-continuation")
    sess = await srv._dispatch_mcp_tool(
        "start_session", {"project_id": p["id"], "role": "planner"}, db, "/tmp",
    )
    sid = sess["session_id"]

    first = await srv._dispatch_mcp_tool(
        "generate_handoff", {"project_id": p["id"], "session_id": sid}, db, "/tmp",
    )
    assert first["mode"] == "planner"

    second = await srv._dispatch_mcp_tool(
        "generate_handoff", {"project_id": p["id"], "session_id": sid}, db, "/tmp",
    )
    assert second["mode"] == "delta"


# ---------------------------------------------------------------------------
# 3. Workspace decisions/notes must not leak into any executor-facing mode.
#    Only an EXPLICIT mode='full' request still includes them.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_records_absent_from_goal_mode(db, tmp_path):
    p = await db_module.create_project(db, "aec043cb-ws-leak-goal")
    await db_module.add_sprint_item(db, p["id"], "v1", "Ship it")
    await db_module.pin_workspace_decision(
        db, "Unrelated interview notes", "candidate feedback body", "STRATEGIC",
    )
    await db_module.add_workspace_note(
        db, "Unrelated thesis finding", "MAT/DSE result body", "research",
    )
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
    )
    assert "Unrelated interview notes" not in content
    assert "Unrelated thesis finding" not in content
    assert "Workspace (applies to all projects)" not in content


@pytest.mark.asyncio
async def test_workspace_records_absent_from_delta_mode(db, tmp_path):
    """Delta/continuation must not bulk-inject workspace records (item notes
    point 2), whether reached explicitly or (as tested above) via the
    resumed-session auto-switch."""
    p = await db_module.create_project(db, "aec043cb-ws-leak-delta")
    await db_module.add_sprint_item(db, p["id"], "v1", "Ship it")
    await db_module.pin_workspace_decision(
        db, "Unrelated interview notes", "candidate feedback body", "STRATEGIC",
    )
    await db_module.add_workspace_note(
        db, "Unrelated thesis finding", "MAT/DSE result body", "research",
    )
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="delta",
    )
    assert "Unrelated interview notes" not in content
    assert "Unrelated thesis finding" not in content
    assert "Workspace (applies to all projects)" not in content


@pytest.mark.asyncio
async def test_workspace_records_absent_from_planner_and_starter_modes(db, tmp_path):
    p = await db_module.create_project(db, "aec043cb-ws-leak-planner-starter")
    await db_module.add_sprint_item(db, p["id"], "v1", "Ship it")
    await db_module.pin_workspace_decision(
        db, "Unrelated interview notes", "candidate feedback body", "STRATEGIC",
    )
    await db_module.add_workspace_note(
        db, "Unrelated thesis finding", "MAT/DSE result body", "research",
    )
    for mode in ("planner", "starter"):
        _, content, _ = await handoff_module.generate_handoff(
            db, p["id"], str(tmp_path), skip_ai_summary=True, mode=mode,
        )
        assert "Unrelated interview notes" not in content, mode
        assert "Unrelated thesis finding" not in content, mode


@pytest.mark.asyncio
async def test_workspace_records_present_in_explicit_full_mode_only(db, tmp_path):
    """The one deliberate, opt-in exception: an EXPLICIT mode='full' request
    (archival/diagnostic) still gets the complete workspace dump — proving
    the leak fix removed the LEAK, not the feature."""
    p = await db_module.create_project(db, "aec043cb-ws-full-explicit")
    await db_module.add_sprint_item(db, p["id"], "v1", "Ship it")
    await db_module.pin_workspace_decision(
        db, "Unrelated interview notes", "candidate feedback body", "STRATEGIC",
    )
    await db_module.add_workspace_note(
        db, "Unrelated thesis finding", "MAT/DSE result body", "research",
    )
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="full",
    )
    assert "Unrelated interview notes" in content
    assert "Unrelated thesis finding" in content
    assert "Workspace (applies to all projects)" in content


@pytest.mark.asyncio
async def test_workspace_records_absent_end_to_end_via_dispatch(db):
    """Same leak proof, but through the REAL dispatch chokepoint
    (start_session(role='executor') -> generate_handoff with mode omitted)
    rather than calling generate_handoff directly — this is the exact shape
    of the originally-reported vulnerability."""
    _clear_role_state()
    p = await db_module.create_project(db, "aec043cb-ws-leak-e2e")
    await db_module.add_sprint_item(db, p["id"], "v1", "Ship it")
    await db_module.pin_workspace_decision(
        db, "Leidos interview notes", "confidential interview body", "STRATEGIC",
    )
    await db_module.add_workspace_note(
        db, "Thesis MAT/DSE finding", "unrelated research body", "research",
    )
    sess = await srv._dispatch_mcp_tool(
        "start_session", {"project_id": p["id"], "role": "executor"}, db, "/tmp",
    )
    result = await srv._dispatch_mcp_tool(
        "generate_handoff",
        {"project_id": p["id"], "session_id": sess["session_id"]},
        db, "/tmp",
    )
    assert result["mode"] == "goal"
    assert "Leidos interview notes" not in result["content"]
    assert "Thesis MAT/DSE finding" not in result["content"]


# ---------------------------------------------------------------------------
# 4. Project/version/item scoping is unchanged: a project's omitted-mode
#    handoff never surfaces another project's items.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_omitted_mode_item_scope_stays_project_local(db):
    _clear_role_state()
    proj_a = await db_module.create_project(db, "aec043cb-scope-a")
    proj_b = await db_module.create_project(db, "aec043cb-scope-b")
    item_a = await db_module.add_sprint_item(db, proj_a["id"], "v1", "Project A item")
    await db_module.add_sprint_item(db, proj_b["id"], "v1", "Project B item")

    sess = await srv._dispatch_mcp_tool(
        "start_session", {"project_id": proj_a["id"], "role": "executor"}, db, "/tmp",
    )
    result = await srv._dispatch_mcp_tool(
        "generate_handoff",
        {"project_id": proj_a["id"], "session_id": sess["session_id"]},
        db, "/tmp",
    )
    assert result["mode"] == "goal"
    assert item_a["id"] in result["content"]
    assert "Project B item" not in result["content"]


@pytest.mark.asyncio
async def test_selected_item_ids_scoping_survives_omitted_mode_default(db, tmp_path):
    """selected_item_ids remains the correct explicit item-scope mechanism
    (item notes' own framing) — still honored when the mode itself is
    reached via the new omitted-mode -> 'goal' resolution, not just an
    explicit mode='goal' call."""
    p = await db_module.create_project(db, "aec043cb-selected-scope")
    keep = await db_module.add_sprint_item(db, p["id"], "v1", "Ship the payment flow")
    drop = await db_module.add_sprint_item(db, p["id"], "v1", "Refactor the CSV importer")

    resolved_mode = handoff_module.resolve_handoff_mode(None, session_role="executor")
    assert resolved_mode == "goal"
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode=resolved_mode,
        selected_item_ids=[keep["id"]],
    )
    assert keep["id"] in content
    assert drop["id"] not in content


# ---------------------------------------------------------------------------
# 5. Trusted goal_token/body-binding contract survives the new default path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_goal_token_binding_holds_for_omitted_mode_default(db, tmp_path):
    """The mint/verify contract (mint_handoff_token / verify_handoff_token)
    is exercised identically whether mode='goal' arrived via an explicit
    request or via the new omitted-mode default — proves the mode-
    resolution change is a pure dispatch-layer concern that never touches
    token minting/binding."""
    p = await db_module.create_project(db, "aec043cb-token-binding")
    await db_module.add_sprint_item(db, p["id"], "v1", "Ship it")

    resolved_mode = handoff_module.resolve_handoff_mode(None, session_role="executor")
    assert resolved_mode == "goal"
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode=resolved_mode,
    )

    token_match = re.search(r"<goal_token>([^<]+)</goal_token>", content)
    assert token_match is not None
    token = token_match.group(1).strip()
    presented_body = handoff_module.strip_goal_token_banner(content)

    result = await handoff_module.verify_handoff_token(
        db, token, p["id"], body=presented_body,
    )
    assert result == {"valid": True, "reason": "ok"}


@pytest.mark.asyncio
async def test_goal_token_wrong_project_still_detected_after_default_change(db, tmp_path):
    """A negative-path sanity check alongside the positive one above: the
    token's project-scoping guarantee (a real token minted for project A
    fails verification against project B) is unaffected by the default-mode
    change."""
    p_a = await db_module.create_project(db, "aec043cb-token-scope-a")
    p_b = await db_module.create_project(db, "aec043cb-token-scope-b")
    await db_module.add_sprint_item(db, p_a["id"], "v1", "Ship it")

    resolved_mode = handoff_module.resolve_handoff_mode(None, session_role="executor")
    _, content, _ = await handoff_module.generate_handoff(
        db, p_a["id"], str(tmp_path), skip_ai_summary=True, mode=resolved_mode,
    )
    token_match = re.search(r"<goal_token>([^<]+)</goal_token>", content)
    assert token_match is not None
    token = token_match.group(1).strip()

    result = await handoff_module.verify_handoff_token(db, token, p_b["id"])
    assert result["valid"] is False
    assert result["reason"] == "wrong_project"


# ---------------------------------------------------------------------------
# 6. checkpoint()/load_handoff() continuation behavior is untouched — the
#    checkpoint MCP tool never calls resolve_handoff_mode at all, so it is
#    unaffected by the omitted-mode default change by construction.
# ---------------------------------------------------------------------------


def test_checkpoint_handler_does_not_use_resolve_handoff_mode():
    """Structural guard: if a future refactor ever routes checkpoint through
    resolve_handoff_mode, this test forces a conscious decision about how
    checkpoint's mode should resolve, rather than silently inheriting
    whatever the executor/planner/goal default happens to be."""
    import inspect

    from meridian.mcp.handlers import session_tools as session_tools_module

    src = inspect.getsource(session_tools_module.handle_checkpoint)
    assert "resolve_handoff_mode" not in src


@pytest.mark.asyncio
async def test_load_handoff_returns_the_stored_omitted_mode_body(db):
    """load_handoff returns exactly what generate_handoff persisted, even
    when that call reached 'goal' via the omitted-mode default rather than
    an explicit request — the storage/retrieval path is mode-agnostic."""
    _clear_role_state()
    p = await db_module.create_project(db, "aec043cb-load-handoff")
    await db_module.add_sprint_item(db, p["id"], "v1", "Ship it")
    sess = await srv._dispatch_mcp_tool(
        "start_session", {"project_id": p["id"], "role": "executor"}, db, "/tmp",
    )
    generated = await srv._dispatch_mcp_tool(
        "generate_handoff",
        {"project_id": p["id"], "session_id": sess["session_id"]},
        db, "/tmp",
    )
    assert generated["mode"] == "goal"

    loaded = await srv._dispatch_mcp_tool(
        "load_handoff", {"project_id": p["id"]}, db, "/tmp",
    )
    assert loaded["handoff"] is not None
    assert loaded["handoff"]["mode"] == "goal"
    assert "<goal_token>" in loaded["handoff"]["content"]
