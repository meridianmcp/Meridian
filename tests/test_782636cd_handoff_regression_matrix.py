"""Regression matrix for sprint item 782636cd (item_group
handoff-profile-parity), plus dedicated coverage for the two "still open"
addenda on d2fc7465.

PROSPECTING NOTE (verify-before-redoing, per the repo's own
feedback_verify_injected_items lesson): both addenda attached to d2fc7465
described bugs that turned out to be ALREADY FIXED on dev tip by prior real
commits, landed before this session started:

  * "expose selected_item_ids on the public generate_handoff schema" —
    already done by cffb9323/94f48e4d/fb82e51f (see mcp_tools.py's
    ``selected_item_ids`` schema entry, meridian/mcp/handler.py's
    ``_selected_item_ids`` plumbing, and tests/test_handoff_item_selection.py).
  * "generate_handoff(mode='goal') and load_handoff() disagree" —
    already fixed by aec043cb, which wired 'goal' mode into the SAME
    ``_persist_handoff_history_and_pending_goal`` channel full/delta always
    used (see tests/test_aec043cb_handoff_mode_scoping.py::
    test_load_handoff_returns_the_stored_omitted_mode_body).

What genuinely remained open, closed by this sprint item's own code change
(d2fc7465, see meridian/handoff.py's ``selected_scope_outcome`` docstring and
``handoff_mode_is_retrievable``): a caller that requested a PARTIAL
``selected_item_ids`` exclusion (some, not all, requested ids dropped by a
downstream claimability gate) had no structured way to learn which ids were
dropped and why — that signal existed internally
(``_selected_scope_outcome``/``excluded_requested``) but was only ever
surfaced on TOTAL exclusion (``HANDOFF_SCOPE_NON_EXECUTABLE``). And no mode
had an explicit, machine-readable "will load_handoff() actually return this"
signal — a caller had to infer persistence from mode name/source reading.
Both are closed by two new ALWAYS-emitted response fields across all three
transports (MCP HTTP, REST, stdio): ``selected_scope`` and
``retrievable_via_load_handoff``.

Matrix coverage below, cross-referencing existing dedicated test files
rather than duplicating them wholesale:

  * item counts 0/1/3/15/over-budget           -> Section 1 (new) +
                                                    tests/test_4f3bd70c_structural_handoff_budget.py,
                                                    tests/test_handoff_manifest_contract_matrix.py
  * pending + in_progress mixed                 -> Section 1 (new)
  * deferred/superseded exclusions              -> tests/test_handoff_enrichment.py,
                                                    tests/test_core.py (deferred_until /
                                                    blocker_kind=superseded coverage)
  * current-version filtering                   -> tests/test_b8f89491_handoff_version_scope.py,
                                                    tests/test_efaa918a_starter_handoff_version_scope.py
  * pointer subsets/overflow                     -> tests/test_handoff_inline_pointers.py,
                                                    tests/test_handoff_manifest_v2.py
  * valid JSON/XML/goal syntax                   -> tests/test_handoff_manifest_contract_matrix.py
                                                    (well-formedness assertions); Section 1 (new,
                                                    light smoke check on this module's own renders)
  * stale/expired token and board-revision       -> tests/test_dd07ece0_handoff_token.py,
                                                    tests/test_handoff_board_divergence.py
  * tenant/project isolation                     -> Section 4 (new) +
                                                    tests/test_aec043cb_handoff_mode_scoping.py::
                                                    test_goal_token_wrong_project_still_detected_after_default_change
  * degraded/offline behavior                    -> Section 3 (new) +
                                                    tests/test_65c8b426_handoff_timeout_fallback.py
  * continuation lineage                         -> tests/test_7732e096_delta_session_scope.py,
                                                    tests/test_handoff_delta_durable_since_ts.py
  * workspace-context exclusion (non-full modes) -> tests/test_aec043cb_handoff_mode_scoping.py
                                                    (test_workspace_records_absent_from_*)
  * explicit omission counts/reasons             -> Section 2 (new) +
                                                    tests/test_handoff_item_selection.py
  * generate_handoff <-> load_handoff consistency -> Section 3 (new, THE dedicated
                                                    regression test the sprint item asks for)
  * selected_item_ids MCP exposure               -> Section 5 (new, schema-level proof)
"""
from __future__ import annotations

import re

import pytest

import meridian.server  # noqa: F401 — load the server before handler to avoid its import cycle
from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian import mcp_tools
from meridian.mcp import handler as mh


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


def _extract_token(text: str) -> "str | None":
    m = re.search(r"<goal_token>([^<]+)</goal_token>", text or "")
    return m.group(1).strip() if m else None


def _item_count_tag(text: str) -> "int | None":
    m = re.search(r'<executor_item_ids count="(\d+)"', text or "")
    return int(m.group(1)) if m else None


async def _gh(db, tmp_path, project_id, **kwargs):
    """Thin wrapper around the MCP dispatch used throughout this file — every
    call goes through the real hosted-transport dispatch, not the bare
    handoff.generate_handoff function, so response-field coverage (the whole
    point of this file) is exercised the same way a real client sees it."""
    kwargs.setdefault("project_id", project_id)
    return await mh._handle_task_tools(
        "generate_handoff", kwargs, db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )


async def _load(db, tmp_path, project_id):
    return await mh._handle_task_tools(
        "load_handoff", {"project_id": project_id}, db, str(tmp_path),
        tenant=None, _mcp_tenant_id=None,
    )


# ---------------------------------------------------------------------------
# Section 1 — item-count matrix + mixed pending/in_progress
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("n", [0, 1, 3, 15])
async def test_item_count_matrix_renders_correct_count(db, tmp_path, n):
    pid = await _project(db, f"matrix-item-count-{n}")
    ids = []
    for i in range(n):
        it = await db_module.add_sprint_item(db, pid, "v1", f"item {i}", force=True)
        ids.append(it["id"])

    result = await _gh(db, tmp_path, pid, mode="goal")
    assert "error" not in result
    content = result["content"]
    if n == 0:
        # No pending items: the executor_item_ids tag is either absent or
        # explicitly count="0" depending on the empty-board render path —
        # either way there must be no fabricated id.
        for iid in ids:
            assert iid not in content
    else:
        assert _item_count_tag(content) == n
        for iid in ids:
            assert iid in content


@pytest.mark.asyncio
async def test_item_count_over_budget_truncates_tool_requirements_with_marker(db, tmp_path):
    """20 items > _DEFAULT_COMPACT_CONTRACT_MAX_ITEMS (15): the FULL item list
    itself is never silently truncated (every id still appears in
    <executor_item_ids>), but the <tool_requirements> clause (bounded by
    full_contract_max_items for goal/starter renders — see
    _build_tool_requirements_clause's own docstring) caps at 15 and emits an
    explicit, machine-readable <tool_requirements_truncated total="20"
    included="15" /> marker rather than dropping the excess silently."""
    pid = await _project(db, "matrix-item-count-over-budget")
    for i in range(20):
        await db_module.add_sprint_item(
            db, pid, "v1", f"item needing pytest {i}",
            required_tool="pytest", force=True,
        )

    result = await _gh(db, tmp_path, pid, mode="goal")
    assert "error" not in result
    content = result["content"]
    assert _item_count_tag(content) == 20, (
        "the full item list must never be silently truncated"
    )
    m = re.search(r'<tool_requirements_truncated total="(\d+)" included="(\d+)"', content)
    assert m is not None, "expected an explicit tool_requirements_truncated marker"
    assert int(m.group(1)) == 20
    assert int(m.group(2)) == 15


@pytest.mark.asyncio
async def test_in_progress_item_excluded_from_pending_batch(db, tmp_path):
    pid = await _project(db, "matrix-pending-in-progress-mixed")
    pending_item = await db_module.add_sprint_item(db, pid, "v1", "still pending", force=True)
    claimed_item = await db_module.add_sprint_item(db, pid, "v1", "already claimed", force=True)
    sess = await db_module.register_session(db, pid, "claimer")
    await db_module.claim_sprint_item(db, pid, claimed_item["id"], sess["id"])

    result = await _gh(db, tmp_path, pid, mode="goal")
    content = result["content"]
    assert pending_item["id"] in content
    assert claimed_item["id"] not in content


# ---------------------------------------------------------------------------
# Section 2 — selected_scope: the new structured omission signal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_selected_scope_is_none_when_selected_item_ids_omitted(db, tmp_path):
    pid = await _project(db, "matrix-selected-scope-unused")
    await db_module.add_sprint_item(db, pid, "v1", "ordinary item", force=True)

    result = await _gh(db, tmp_path, pid, mode="goal")
    assert result["selected_scope"] is None


@pytest.mark.asyncio
async def test_selected_scope_populated_with_closure_on_clean_selection(db, tmp_path):
    pid = await _project(db, "matrix-selected-scope-clean")
    parent = await db_module.add_sprint_item(db, pid, "v1", "parent", force=True)
    child = await db_module.add_sprint_item(
        db, pid, "v1", "child", depends_on=parent["id"], force=True,
    )

    result = await _gh(db, tmp_path, pid, mode="goal", selected_item_ids=[child["id"]])
    assert "error" not in result
    scope = result["selected_scope"]
    assert scope is not None
    assert scope["selected_item_ids"] == [child["id"]]
    assert set(scope["closure_item_ids"]) == {parent["id"], child["id"]}
    assert scope["closure_hash"]
    assert scope["all_excluded"] is False
    assert scope["excluded_requested"] == []
    assert child["id"] in scope["executable_ids"]


@pytest.mark.asyncio
async def test_selected_scope_reports_partial_exclusion_reason_without_failing_closed(
    db, tmp_path
):
    """The dedicated regression for the addendum's "return explicit
    selected_scope_ids plus omitted IDs" ask: a selection mixing a claimable
    item with a manual-blocked one must succeed (not raise
    HandoffScopeNonExecutable — that only fires when EVERY requested id is
    excluded) AND must surface WHICH id was dropped and why, as a structured
    field — not just as a total-failure-only signal."""
    pid = await _project(db, "matrix-selected-scope-partial")
    ordinary = await db_module.add_sprint_item(db, pid, "v1", "ordinary claimable item", force=True)
    manual = await db_module.add_sprint_item(
        db, pid, "v1", "configure PyPI trusted publisher",
        blocker_kind="manual", force=True,
    )

    result = await _gh(
        db, tmp_path, pid, mode="goal",
        selected_item_ids=[ordinary["id"], manual["id"]],
    )
    assert "error" not in result, f"expected success, got {result.get('error')}"
    assert ordinary["id"] in result["content"]

    scope = result["selected_scope"]
    assert scope is not None
    assert scope["all_excluded"] is False
    assert scope["executable_ids"] == [ordinary["id"]]
    assert scope["excluded_requested"] == [
        {"id": manual["id"], "reason": "not_in_pending_batch"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["goal", "starter", "full", "delta"])
async def test_selected_scope_present_across_every_executable_mode(db, tmp_path, mode):
    pid = await _project(db, f"matrix-selected-scope-modes-{mode}")
    item = await db_module.add_sprint_item(db, pid, "v1", "solo item", force=True)

    result = await _gh(db, tmp_path, pid, mode=mode, selected_item_ids=[item["id"]])
    assert "error" not in result
    scope = result["selected_scope"]
    assert scope is not None
    assert scope["selected_item_ids"] == [item["id"]]
    assert scope["all_excluded"] is False


# ---------------------------------------------------------------------------
# Section 3 — retrievable_via_load_handoff + the priority generate/load
# consistency regression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [
    ("goal", True), ("full", True), ("delta", True),
    ("starter", False), ("compact", False), ("planner", False),
])
async def test_retrievable_via_load_handoff_matches_documented_persistence_contract(
    db, tmp_path, mode, expected
):
    pid = await _project(db, f"matrix-retrievable-{mode}")
    await db_module.add_sprint_item(db, pid, "v1", "an item", force=True)

    result = await _gh(db, tmp_path, pid, mode=mode)
    assert "error" not in result
    assert result["retrievable_via_load_handoff"] is expected
    # The pure function backing this field must agree exactly (no drift
    # between the transport's own field and the shared source of truth).
    assert handoff_module.handoff_mode_is_retrievable(result["mode"]) is expected


@pytest.mark.asyncio
async def test_retrievable_via_load_handoff_false_on_l0_fallback_degrade(
    db, tmp_path, monkeypatch
):
    """Mirrors test_65c8b426_handoff_timeout_fallback.py's own harness: the
    emergency L0 fallback never reaches
    _persist_handoff_history_and_pending_goal, so retrievable_via_load_handoff
    must be False even though `mode` becomes the synthetic 'l0_fallback'
    value (itself excluded from the retrievable set)."""
    import asyncio as _asyncio

    pid = await _project(db, "matrix-retrievable-l0-fallback")
    await db_module.set_goal(db, pid, "do things", sprint="v1")

    async def _mock_wait_for(coro, timeout):
        coro.close()
        raise _asyncio.TimeoutError()

    monkeypatch.setattr(_asyncio, "wait_for", _mock_wait_for)

    result = await _gh(db, tmp_path, pid)
    assert result["mode"] == "l0_fallback"
    assert result["retrievable_via_load_handoff"] is False


@pytest.mark.asyncio
async def test_generate_then_load_handoff_consistency_goal_mode(db, tmp_path):
    """THE dedicated regression for the PERSISTENCE READINESS addendum's
    "FINAL READINESS GATE" bug report: a fresh generate_handoff(mode='goal')
    must be exactly what an immediate load_handoff(project_id) returns —
    same mode, same token, byte-identical content — not an older stale
    full/delta render. Already fixed by aec043cb (goal mode wired into
    _persist_handoff_history_and_pending_goal); this test would have FAILED
    against the pre-aec043cb code path (load_handoff would have returned
    has_handoff=False / handoff=None, since 'goal' mode never persisted
    anything for load_handoff to find)."""
    pid = await _project(db, "matrix-consistency-goal")
    await db_module.add_sprint_item(db, pid, "v1", "ship it", force=True)

    generated = await _gh(db, tmp_path, pid, mode="goal")
    assert generated["mode"] == "goal"
    assert generated["retrievable_via_load_handoff"] is True
    gen_token = _extract_token(generated["content"])
    assert gen_token

    loaded = await _load(db, tmp_path, pid)
    assert loaded["is_trusted_channel"] is True
    assert loaded["handoff"] is not None
    assert loaded["handoff"]["mode"] == generated["mode"]
    assert loaded["handoff"]["content"] == generated["content"], (
        "load_handoff must return the exact body generate_handoff just "
        "rendered — a mismatch here is the exact staleness bug reported "
        "against production (11,986-char fresh goal render vs a 55,248-char "
        "stale delta returned by load_handoff)."
    )
    assert _extract_token(loaded["handoff"]["content"]) == gen_token


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["full", "delta"])
async def test_generate_then_load_handoff_consistency_full_and_delta(db, tmp_path, mode):
    pid = await _project(db, f"matrix-consistency-{mode}")
    await db_module.add_sprint_item(db, pid, "v1", "ship it", force=True)
    sess = await db_module.register_session(db, pid, "resumer")

    generated = await _gh(db, tmp_path, pid, mode=mode, session_id=sess["id"])
    assert generated["mode"] == mode

    loaded = await _load(db, tmp_path, pid)
    assert loaded["handoff"]["mode"] == mode
    assert loaded["handoff"]["content"] == generated["content"]


@pytest.mark.asyncio
async def test_starter_mode_never_clobbers_the_retrievable_channel(db, tmp_path):
    """starter/compact is explicitly NOT in the retrievable set (see
    handoff_mode_is_retrievable) — this proves that in practice: generating a
    starter-mode render AFTER a goal-mode one must leave load_handoff()
    still returning the ORIGINAL goal content, not the ephemeral starter
    preview and not an empty/broken record."""
    pid = await _project(db, "matrix-starter-does-not-clobber")
    await db_module.add_sprint_item(db, pid, "v1", "ship it", force=True)

    goal_result = await _gh(db, tmp_path, pid, mode="goal")
    assert goal_result["retrievable_via_load_handoff"] is True

    starter_result = await _gh(db, tmp_path, pid, mode="starter")
    assert starter_result["retrievable_via_load_handoff"] is False
    assert starter_result["content"] != goal_result["content"]

    loaded = await _load(db, tmp_path, pid)
    assert loaded["handoff"]["mode"] == "goal"
    assert loaded["handoff"]["content"] == goal_result["content"]


# ---------------------------------------------------------------------------
# Section 4 — tenant/project isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_never_leaks_another_projects_items_or_stored_handoff(db, tmp_path):
    pid_a = await _project(db, "matrix-isolation-a")
    pid_b = await _project(db, "matrix-isolation-b")
    item_a = await db_module.add_sprint_item(db, pid_a, "v1", "project A item", force=True)
    item_b = await db_module.add_sprint_item(db, pid_b, "v1", "project B item", force=True)

    result_a = await _gh(db, tmp_path, pid_a, mode="goal")
    assert item_a["id"] in result_a["content"]
    assert item_b["id"] not in result_a["content"]

    result_b = await _gh(db, tmp_path, pid_b, mode="goal")
    assert item_b["id"] in result_b["content"]
    assert item_a["id"] not in result_b["content"]

    loaded_a = await _load(db, tmp_path, pid_a)
    assert loaded_a["handoff"]["content"] == result_a["content"]
    assert item_b["id"] not in loaded_a["handoff"]["content"]


# ---------------------------------------------------------------------------
# Section 5 — selected_item_ids MCP schema exposure (proof, not assumption)
# ---------------------------------------------------------------------------


def test_selected_item_ids_is_exposed_on_the_public_generate_handoff_schema():
    """Schema-level proof that generate_handoff's public MCP tool definition
    accepts selected_item_ids — the addendum's original "expose the selector
    safely" ask. This was already true on dev tip (cffb9323/94f48e4d) before
    this sprint item ran; this test makes that fact durable/regression-proof
    rather than something a future refactor could silently drop."""
    entry = next(
        t for t in mcp_tools._MCP_TOOLS_LIST if t["name"] == "generate_handoff"
    )
    props = entry["inputSchema"]["properties"]
    assert "selected_item_ids" in props
    assert props["selected_item_ids"]["type"] == "array"
    # d2fc7465 — the response-side counterpart must be documented too.
    assert "selected_scope" in props["selected_item_ids"]["description"]
