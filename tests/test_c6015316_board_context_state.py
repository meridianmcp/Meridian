"""Tests for c6015316 — decision-only projects produce an explicit, bounded
machine-readable handoff signal instead of being indistinguishable from a
genuinely-empty project.

Root cause (see meridian.handoff._generate_goal_only_handoff /
build_board_context_state_for_handoff docstrings): ``mode="goal"`` never
fetches ``get_pinned_decisions``/``get_project_notes`` at all — when the
pending-item list is empty, ``_build_quick_start_goal`` renders the literal
``<executor_directive>Verify remaining work is complete.</executor_directive>``
regardless of whether the project has substantial pinned decisions/notes or
genuinely nothing. This file proves the additive fix:
``build_board_context_state_for_handoff`` — a new, fully-guarded,
project-scoped-only wrapper (mirrors ``build_effective_capability_contract``'s
style exactly) — is emitted as a new sibling ``board_context_state`` field on
every ``generate_handoff`` transport, WITHOUT touching goal-mode's rendered
``content`` (preserving the byte-level contract
tests/test_682005f4_goal_only_handoff.py and tests/test_cov_handoff.py
already assert).

Covers:
  1. Direct unit coverage of build_board_context_state_for_handoff: the three
     states (empty / context_only / has_pending_items), counts-only (never
     decision/note bodies), never-raises guard, cross-project isolation,
     version scoping, and a byte-size bound independent of decision/note body
     size.
  2. Goal-mode text stays byte-for-byte unaffected for a decision-only
     project (explicit goal semantics preserved — never silently turned into
     an executor task list, and decision/note prose never leaks into it).
  3. Transport parity: MCP dispatch, stdio, and both REST endpoints
     (POST /projects/{id}/handoff and GET /projects/{id}/handoff/planner)
     all expose the same board_context_state for the same project state.
"""
from __future__ import annotations

import json

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module
import meridian.server  # noqa: F401 — load the server before handler to avoid its import cycle
from meridian import server as srv


# ---------------------------------------------------------------------------
# 1. build_board_context_state_for_handoff — direct unit coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_only_state_with_decisions_and_notes_zero_items(db):
    """The exact scenario this item exists to fix: pinned decisions + notes,
    zero sprint items of any kind."""
    p = await db_module.create_project(db, "ctx-only-decisions-notes")
    await db_module.pin_decision(
        db, p["id"], "Use psycopg3", "asyncpg has DLL issues", "TECHNICAL"
    )
    await db_module.add_project_note(
        db, p["id"], "Strat note title", "strat note body", "strategy"
    )

    state = await handoff_module.build_board_context_state_for_handoff(db, p["id"])

    assert state is not None
    assert state["state"] == "context_only"
    assert state["pending_item_count"] == 0
    assert state["in_progress_item_count"] == 0
    assert state["pinned_decision_count"] == 1
    assert state["note_count"] == 1
    assert "hint" in state and isinstance(state["hint"], str) and state["hint"]


@pytest.mark.asyncio
async def test_empty_state_genuinely_nothing(db):
    """A project with literally nothing (no decisions, no notes, no items)
    must report "empty", never "context_only" — the two must stay
    distinguishable in both directions."""
    p = await db_module.create_project(db, "genuinely-empty-project")

    state = await handoff_module.build_board_context_state_for_handoff(db, p["id"])

    assert state is not None
    assert state["state"] == "empty"
    assert state["pending_item_count"] == 0
    assert state["in_progress_item_count"] == 0
    assert state["pinned_decision_count"] == 0
    assert state["note_count"] == 0
    assert "hint" not in state


@pytest.mark.asyncio
async def test_has_pending_items_state_with_pending_item(db):
    p = await db_module.create_project(db, "has-pending-item")
    await db_module.pin_decision(db, p["id"], "irrelevant", "body", "TECHNICAL")
    await db_module.add_sprint_item(db, p["id"], "v1", "Do the real work")

    state = await handoff_module.build_board_context_state_for_handoff(db, p["id"])

    assert state is not None
    assert state["state"] == "has_pending_items"
    assert state["pending_item_count"] == 1
    assert state["in_progress_item_count"] == 0
    # Decisions/notes are still counted even when there IS pending work —
    # the field is purely additive, not exclusive.
    assert state["pinned_decision_count"] == 1
    assert "hint" not in state


@pytest.mark.asyncio
async def test_has_pending_items_state_with_in_progress_item_only(db):
    """An item that has been CLAIMED (in_progress, no longer pending) still
    means "there is executable work happening" — must not be misreported as
    context_only just because the pending bucket itself is empty."""
    p = await db_module.create_project(db, "has-in-progress-only")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Claimed work")
    claimed = await db_module.claim_sprint_item(db, p["id"], item["id"])
    assert not (isinstance(claimed, dict) and claimed.get("blocked"))

    state = await handoff_module.build_board_context_state_for_handoff(db, p["id"])

    assert state is not None
    assert state["state"] == "has_pending_items"
    assert state["pending_item_count"] == 0
    assert state["in_progress_item_count"] == 1


@pytest.mark.asyncio
async def test_counts_are_ints_never_bodies(db):
    """Structural leak-proofing: the returned dict must never contain
    decision/note title or body text anywhere, and every count must be a
    plain int (get_project_notes is called with bodies=False, and only
    len() of each list is ever read)."""
    p = await db_module.create_project(db, "no-body-leak")
    secret_decision_title = "UNIQUE-DECISION-TITLE-zzqx91"
    secret_decision_body = "UNIQUE-DECISION-BODY-content-should-never-appear"
    secret_note_title = "UNIQUE-NOTE-TITLE-abcz77"
    secret_note_body = "UNIQUE-NOTE-BODY-content-should-never-appear"
    await db_module.pin_decision(
        db, p["id"], secret_decision_title, secret_decision_body, "TECHNICAL"
    )
    await db_module.add_project_note(
        db, p["id"], secret_note_title, secret_note_body, "strategy"
    )

    state = await handoff_module.build_board_context_state_for_handoff(db, p["id"])

    assert state is not None
    for key in (
        "pending_item_count", "in_progress_item_count",
        "pinned_decision_count", "note_count",
    ):
        assert isinstance(state[key], int)
    serialized = json.dumps(state)
    assert secret_decision_title not in serialized
    assert secret_decision_body not in serialized
    assert secret_note_title not in serialized
    assert secret_note_body not in serialized


@pytest.mark.asyncio
async def test_wrapper_never_raises_on_decisions_failure(db, monkeypatch):
    """Fully guarded, same convention as every other build_*_for_handoff
    wrapper — a DB failure degrades to None rather than breaking the
    mandatory handoff."""
    p = await db_module.create_project(db, "board-context-boom")

    async def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(db_module, "get_pinned_decisions", _boom)
    state = await handoff_module.build_board_context_state_for_handoff(db, p["id"])
    assert state is None


@pytest.mark.asyncio
async def test_wrapper_never_raises_on_sprint_items_failure(db, monkeypatch):
    p = await db_module.create_project(db, "board-context-boom-items")

    async def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(db_module, "get_sprint_items", _boom)
    state = await handoff_module.build_board_context_state_for_handoff(db, p["id"])
    assert state is None


@pytest.mark.asyncio
async def test_cross_project_isolation_decisions_and_notes(db):
    """A decision-only project must NEVER see another project's decisions or
    notes — no workspace-wide bulk injection. Project A pins decisions/notes
    and has zero items (context_only); Project B has a genuinely empty board
    and must report "empty", not be contaminated by A's context."""
    a = await db_module.create_project(db, "isolation-project-a")
    b = await db_module.create_project(db, "isolation-project-b")
    await db_module.pin_decision(db, a["id"], "A's decision", "A's body", "TECHNICAL")
    await db_module.add_project_note(db, a["id"], "A's note", "A's note body", "strategy")

    state_a = await handoff_module.build_board_context_state_for_handoff(db, a["id"])
    state_b = await handoff_module.build_board_context_state_for_handoff(db, b["id"])

    assert state_a["state"] == "context_only"
    assert state_a["pinned_decision_count"] == 1
    assert state_a["note_count"] == 1

    # Project B must be genuinely unaffected by A's decisions/notes.
    assert state_b["state"] == "empty"
    assert state_b["pinned_decision_count"] == 0
    assert state_b["note_count"] == 0


@pytest.mark.asyncio
async def test_cross_project_isolation_pending_items(db):
    """Project A has a pending item; Project B (decision-only) must not be
    reported as having pending work just because SOME project on this
    server does."""
    a = await db_module.create_project(db, "isolation-items-a")
    b = await db_module.create_project(db, "isolation-items-b")
    await db_module.add_sprint_item(db, a["id"], "v1", "A's pending item")
    await db_module.pin_decision(db, b["id"], "B's decision", "B's body", "TECHNICAL")

    state_a = await handoff_module.build_board_context_state_for_handoff(db, a["id"])
    state_b = await handoff_module.build_board_context_state_for_handoff(db, b["id"])

    assert state_a["state"] == "has_pending_items"
    assert state_b["state"] == "context_only"
    assert state_b["pending_item_count"] == 0


@pytest.mark.asyncio
async def test_version_scoping_matches_generate_handoff(db):
    """version= scopes the item counts the same way generate_handoff's own
    goal/full/delta branches scope their pending-item lists — a v2 item must
    not count toward a v1-scoped board_context_state."""
    p = await db_module.create_project(db, "version-scope-context")
    await db_module.pin_decision(db, p["id"], "decision", "body", "TECHNICAL")
    await db_module.add_sprint_item(db, p["id"], "v2", "v2-only item")

    scoped_v1 = await handoff_module.build_board_context_state_for_handoff(
        db, p["id"], version="v1",
    )
    scoped_v2 = await handoff_module.build_board_context_state_for_handoff(
        db, p["id"], version="v2",
    )
    unscoped = await handoff_module.build_board_context_state_for_handoff(db, p["id"])

    assert scoped_v1["state"] == "context_only"
    assert scoped_v1["pending_item_count"] == 0
    assert scoped_v2["state"] == "has_pending_items"
    assert scoped_v2["pending_item_count"] == 1
    assert unscoped["state"] == "has_pending_items"


@pytest.mark.asyncio
async def test_byte_bound_independent_of_decision_note_body_size(db):
    """The field's size must stay small (counts only) regardless of how many
    decisions/notes a project accumulates or how large their bodies are —
    it must never become an unbounded dump the way the pre-248c0bb9 goal
    clauses did."""
    p = await db_module.create_project(db, "byte-bound-context")
    big_body = "x" * 5000
    for i in range(25):
        await db_module.pin_decision(db, p["id"], f"Decision {i}", big_body, "TECHNICAL")
    for i in range(25):
        await db_module.add_project_note(db, p["id"], f"Note {i}", big_body, "strategy")

    state = await handoff_module.build_board_context_state_for_handoff(db, p["id"])

    assert state is not None
    assert state["pinned_decision_count"] == 25
    assert state["note_count"] == 25
    serialized = json.dumps(state)
    # Comfortably bounded — counts + a short hint string, nothing that grows
    # with the number or size of underlying decisions/notes (which here
    # total >250KB of body text alone).
    assert len(serialized) < 1000, f"unexpectedly large board_context_state: {len(serialized)} bytes"


# ---------------------------------------------------------------------------
# 2. Goal semantics preserved — content stays byte-for-byte unaffected.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_goal_mode_content_unchanged_for_context_only_project(db, tmp_path):
    """CRITICAL constraint: this item must NOT silently turn a goal-mode
    render into an executor task list, and must NOT leak decision/note prose
    into the /goal block, for a decision-only project. Mirrors
    tests/test_682005f4_goal_only_handoff.py's own assertions exactly."""
    p = await db_module.create_project(db, "goal-mode-context-only")
    await db_module.pin_decision(
        db, p["id"], "Use psycopg3", "asyncpg has DLL issues", "TECHNICAL"
    )
    await db_module.add_project_note(
        db, p["id"], "Strat note title", "strat note body", "strategy"
    )

    _path, content, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
    )

    # The pre-existing bare-empty-goal literal must survive unchanged.
    assert "<executor_directive>Verify remaining work is complete.</executor_directive>" in content
    # Decision/note prose must never leak into the goal text.
    assert "Use psycopg3" not in content
    assert "asyncpg has DLL issues" not in content
    assert "Strat note title" not in content
    assert "strat note body" not in content

    # The sibling machine-readable field (built separately, exactly like
    # capability_contract) correctly reports context_only for this project.
    state = await handoff_module.build_board_context_state_for_handoff(db, p["id"])
    assert state["state"] == "context_only"


@pytest.mark.asyncio
async def test_goal_mode_content_unchanged_for_genuinely_empty_project(db, tmp_path):
    p = await db_module.create_project(db, "goal-mode-genuinely-empty")

    _path, content, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
    )

    assert "<executor_directive>Verify remaining work is complete.</executor_directive>" in content
    state = await handoff_module.build_board_context_state_for_handoff(db, p["id"])
    assert state["state"] == "empty"


# ---------------------------------------------------------------------------
# 3. Transport parity — MCP dispatch, stdio, and both REST endpoints.
# ---------------------------------------------------------------------------


def _build_stdio_server(monkeypatch, db):
    """Same pattern as tests/test_stdio_handoff_arg_parity.py's helper."""
    import meridian.server as server_module

    async def _return_db(*_a, **_k):
        return db

    monkeypatch.setattr(db_module, "init_db", _return_db)
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.delenv("MERIDIAN_DB_URL", raising=False)
    server, _run_stdio = server_module.build_mcp_server()
    return server


async def _call_generate_handoff_stdio(server, arguments):
    import mcp.types as mcp_types

    call_handler = server.request_handlers[mcp_types.CallToolRequest]
    called = await call_handler(
        mcp_types.CallToolRequest(
            params=mcp_types.CallToolRequestParams(
                name="generate_handoff",
                arguments=arguments,
            )
        )
    )
    return json.loads(called.root.content[0].text)


@pytest.mark.asyncio
async def test_mcp_dispatch_exposes_board_context_state(db, tmp_path):
    p = await db_module.create_project(db, "parity-mcp-board-context")
    await db_module.pin_decision(db, p["id"], "decision", "body", "TECHNICAL")
    await db_module.add_project_note(db, p["id"], "note", "note body", "strategy")

    result = await srv._dispatch_mcp_tool(
        "generate_handoff",
        {"project_id": p["id"], "mode": "goal"},
        db, str(tmp_path),
    )

    assert "board_context_state" in result
    assert result["board_context_state"]["state"] == "context_only"
    assert result["board_context_state"]["pinned_decision_count"] == 1
    assert result["board_context_state"]["note_count"] == 1


@pytest.mark.asyncio
async def test_stdio_matches_mcp_dispatch_board_context_state(db, monkeypatch, tmp_path):
    """The stdio transport was previously missing this whole class of
    sibling fields (capability_contract, profile_binding, ... and now
    board_context_state) — this proves the parity gap is closed for the new
    field specifically, using the SAME db connection for both transports so
    the comparison is against identical board state."""
    p = await db_module.create_project(db, "parity-stdio-board-context")
    await db_module.pin_decision(db, p["id"], "decision", "body", "TECHNICAL")

    via_dispatch = await srv._dispatch_mcp_tool(
        "generate_handoff",
        {"project_id": p["id"], "mode": "goal"},
        db, str(tmp_path),
    )

    server = _build_stdio_server(monkeypatch, db)
    via_stdio = await _call_generate_handoff_stdio(
        server, {"project_id": p["id"], "mode": "goal"},
    )

    assert "board_context_state" in via_stdio
    assert via_stdio["board_context_state"] == via_dispatch["board_context_state"]
    assert via_stdio["board_context_state"]["state"] == "context_only"


@pytest.mark.asyncio
async def test_stdio_board_context_state_for_has_pending_items(db, monkeypatch, tmp_path):
    p = await db_module.create_project(db, "parity-stdio-pending")
    await db_module.add_sprint_item(db, p["id"], "v1", "pending work")

    server = _build_stdio_server(monkeypatch, db)
    via_stdio = await _call_generate_handoff_stdio(
        server, {"project_id": p["id"], "mode": "goal"},
    )
    assert via_stdio["board_context_state"]["state"] == "has_pending_items"


def test_http_post_handoff_exposes_board_context_state(client):
    proj = client.post("/projects", json={"name": "parity-http-board-context"}).json()
    pid = proj["id"]
    client.post(
        f"/projects/{pid}/decisions-pinned",
        json={"title": "d1", "body": "decision body", "category": "TECHNICAL"},
    )
    client.post(
        f"/projects/{pid}/notes", json={"title": "n1", "body": "note body"},
    )

    resp = client.post(f"/projects/{pid}/handoff", json={"mode": "goal"})
    assert resp.status_code == 200
    data = resp.json()
    assert "board_context_state" in data
    assert data["board_context_state"]["state"] == "context_only"
    assert data["board_context_state"]["pinned_decision_count"] == 1
    assert data["board_context_state"]["note_count"] == 1
    # Goal text itself must remain the bare-empty directive — no leak.
    assert "decision body" not in data["content"]
    assert "note body" not in data["content"]


def test_http_post_handoff_board_context_state_empty(client):
    proj = client.post("/projects", json={"name": "parity-http-empty"}).json()
    pid = proj["id"]
    resp = client.post(f"/projects/{pid}/handoff", json={"mode": "goal"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["board_context_state"]["state"] == "empty"


def test_http_post_handoff_board_context_state_has_pending_items(client):
    proj = client.post("/projects", json={"name": "parity-http-pending"}).json()
    pid = proj["id"]
    client.post(
        f"/projects/{pid}/sprint-items", json={"version": "v1", "title": "do it"},
    )
    resp = client.post(f"/projects/{pid}/handoff", json={"mode": "goal"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["board_context_state"]["state"] == "has_pending_items"
    assert data["board_context_state"]["pending_item_count"] == 1


def test_http_planner_endpoint_exposes_board_context_state(client):
    proj = client.post("/projects", json={"name": "parity-http-planner-context"}).json()
    pid = proj["id"]
    client.post(
        f"/projects/{pid}/decisions-pinned",
        json={"title": "d1", "body": "decision body", "category": "TECHNICAL"},
    )
    resp = client.get(f"/projects/{pid}/handoff/planner")
    assert resp.status_code == 200
    data = resp.json()
    assert "board_context_state" in data
    assert data["board_context_state"]["state"] == "context_only"
    assert data["board_context_state"]["pinned_decision_count"] == 1


def test_http_unknown_project_handoff_404_unaffected(client):
    """Sanity: the new field doesn't change the pre-existing 404 contract for
    an unknown project."""
    resp = client.post(
        "/projects/00000000-0000-0000-0000-000000000000/handoff", json={},
    )
    assert resp.status_code == 404
