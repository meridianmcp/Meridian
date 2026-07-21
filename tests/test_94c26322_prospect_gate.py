"""94c26322 — tests for the prospecting safety gate.

Three surfaces are tested:
1. _build_quick_start_goal excludes unprospected items (no evidence, no bypass)
   and emits a visible <excluded_unprospected> tag in the /goal output.
2. _build_quick_start_goal includes unprospected items when prospect_bypass=True.
3. claim_sprint_item returns a structured UNPROSPECTED blocked dict for
   unprospected items, mirroring the DEFERRED gate (dec69708).
4. claim_sprint_item allows the claim when prospect_bypass=True.
5. patch_sprint_item wires through the prospect_bypass field.
6. Migration: sprint_items gains a prospect_bypass column defaulting to 0.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from typing import Any

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import meridian.db as db_module
from meridian.handoff import _build_quick_start_goal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_item(
    iid: str,
    title: str,
    *,
    prospect_status: str | None = None,
    code_pointers: list | None = None,
    pointers: list | None = None,
    prospect_bypass: bool = False,
    touches_resources: list | None = None,
    version: str = "v1",
    status: str = "pending",
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": iid,
        "version": version,
        "status": status,
        "title": title,
        "prospect_bypass": 1 if prospect_bypass else 0,
    }
    if prospect_status is not None:
        d["prospect_status"] = prospect_status
    if code_pointers is not None:
        d["code_pointers"] = code_pointers
    if pointers is not None:
        d["pointers"] = pointers
    if touches_resources is not None:
        d["touches_resources"] = touches_resources
    return d


def _run(coro: Any) -> Any:
    # asyncio.run() (not get_event_loop().run_until_complete()) — the latter can
    # return a stale/closed loop when this file shares an xdist worker process
    # with other pytest-asyncio-managed test files, leaving dangling
    # "coroutine was never awaited" warnings to surface in unrelated tests.
    return asyncio.run(coro)


async def _make_db() -> Any:
    tmp = tempfile.mktemp(suffix=".db")
    return await db_module.init_db(tmp)


# ---------------------------------------------------------------------------
# 1. Goal gate: unprospected items excluded + visible tag
#
# d5849a67 — REWRITTEN. The exclusion decision now mirrors claim_sprint_item's
# actual gate exactly (is_item_claim_prospected): only items that DECLARED
# touches_resources and lack a DURABLE row in sprint_item_pointers are
# excluded. The old version of this section exercised the pre-fix behaviour
# (excluding/including based on transient prospect_status/code_pointers/
# pointers fields, ignoring touches_resources entirely), which is exactly
# what caused generate_handoff's excluded_unprospected list to disagree with
# claim_sprint_item's real enforcement -- see the "5. End-to-end consistency"
# section below for tests that exercise both checks against the same item.
#
# pointer_evidence_ids is passed explicitly in every test below to stand in
# for the batch-resolved durable-pointer signal (db.get_pointer_evidence_item_ids)
# that generate_handoff supplies in production.
# ---------------------------------------------------------------------------

def test_goal_gate_excludes_unprospected_item():
    """An item that declared touches_resources but has no durable pointer
    evidence (absent from pointer_evidence_ids) and no bypass must be
    excluded from the /goal's claimable ids but appear in the excluded tag.
    """
    items = [
        _make_item("id-no-evidence", "Fix login",
                   touches_resources=["file:meridian/x.py:sym"]),
    ]
    goal = _build_quick_start_goal(items, pointer_evidence_ids=set())
    # Must appear in the exclusion tag (so it's visible to a human reviewing the /goal)
    assert '<excluded_unprospected' in goal
    assert 'count="1"' in goal
    assert "id-no-evidence" in goal  # appears in the exclusion tag
    # Must NOT appear in the claimable sprint_items section
    items_clause = goal.split('<sprint_items>')[1].split('</sprint_items>')[0] if '<sprint_items>' in goal else ""
    assert "id-no-evidence" not in items_clause


def test_goal_gate_includes_item_without_declared_resources():
    """An item with NO declared touches_resources was never a real prospecting
    candidate (SCOPE GUARD) -- it is included regardless of prospect_status or
    pointer_evidence_ids, mirroring claim_sprint_item's own scope guard.
    """
    items = [
        _make_item("id-no-resources", "Write docs", prospect_status="no_match"),
    ]
    goal = _build_quick_start_goal(items, pointer_evidence_ids=set())
    assert "id-no-resources" in goal
    assert '<excluded_unprospected' not in goal


def test_goal_gate_includes_item_with_durable_pointer_evidence():
    """An item with touches_resources declared AND present in
    pointer_evidence_ids (a durable sprint_item_pointers row) IS included."""
    items = [
        _make_item("id-good", "Fix auth", touches_resources=["file:meridian/auth.py:login"]),
    ]
    goal = _build_quick_start_goal(items, pointer_evidence_ids={"id-good"})
    assert "id-good" in goal
    assert '<excluded_unprospected' not in goal


def test_goal_gate_transient_enrichment_annotation_is_not_sufficient():
    """d5849a67 regression: an item carrying a transient, in-memory-only
    code_pointers / prospect_status='prospected' annotation (as attached by
    handoff-time enrichment, _annotate_code_pointers) but with NO durable
    sprint_item_pointers row must still be EXCLUDED. Before this fix, those
    transient fields alone were enough to pass the gate here even though
    claim_sprint_item only ever checks the durable table -- exactly the drift
    the executor hit (items absent from <excluded_unprospected> yet refused
    at claim time).
    """
    items = [
        _make_item(
            "id-transient-only", "Looks prospected but isn't durable",
            touches_resources=["file:meridian/x.py:sym"],
            prospect_status="prospected", code_pointers=[{"file": "x.py"}],
        ),
    ]
    goal = _build_quick_start_goal(items, pointer_evidence_ids=set())
    assert '<excluded_unprospected' in goal
    items_clause = goal.split('<sprint_items>')[1].split('</sprint_items>')[0] if '<sprint_items>' in goal else ""
    assert "id-transient-only" not in items_clause


def test_goal_gate_bypassed_with_prospect_bypass():
    """An item with touches_resources declared, no durable evidence, BUT
    prospect_bypass=True IS included (bypass wins)."""
    items = [
        _make_item("id-bypass", "Unusual task",
                   touches_resources=["file:meridian/x.py:sym"], prospect_bypass=True),
    ]
    goal = _build_quick_start_goal(items, pointer_evidence_ids=set())
    assert "id-bypass" in goal
    assert '<excluded_unprospected' not in goal


def test_goal_gate_mixed_items_selective_exclusion():
    """Mixed list: only resource-declaring, non-bypassed items lacking durable
    evidence are excluded."""
    items = [
        _make_item("id-excl", "No evidence", touches_resources=["file:meridian/a.py:a"]),
        _make_item("id-incl", "Has durable evidence", touches_resources=["file:meridian/b.py:b"]),
        _make_item("id-bypass", "Bypassed",
                   touches_resources=["file:meridian/c.py:c"], prospect_bypass=True),
        _make_item("id-no-res", "No resources declared"),
    ]
    goal = _build_quick_start_goal(items, pointer_evidence_ids={"id-incl"})
    # Claimable items section must include id-incl, id-bypass, id-no-res
    items_clause = goal.split('<sprint_items>')[1].split('</sprint_items>')[0] if '<sprint_items>' in goal else ""
    assert "id-excl" not in items_clause, "id-excl must be excluded from claimable list"
    assert "id-incl" in items_clause, "id-incl (durable evidence) must be included"
    assert "id-bypass" in items_clause, "id-bypass (bypass set) must be included"
    assert "id-no-res" in items_clause, "id-no-res (no declared resources) must be included"
    # Only id-excl was excluded
    assert 'count="1"' in goal
    exc_section = goal.split('<excluded_unprospected')[1].split('</excluded_unprospected>')[0] if '<excluded_unprospected' in goal else ""
    assert "id-excl" in exc_section


def test_goal_gate_excluded_tag_contains_item_ids():
    """The <excluded_unprospected> tag must list the excluded item ids."""
    items = [
        _make_item("aaa-111", "Item A", touches_resources=["file:meridian/a.py:a"]),
        _make_item("bbb-222", "Item B", touches_resources=["file:meridian/b.py:b"]),
    ]
    goal = _build_quick_start_goal(items, pointer_evidence_ids=set())
    assert "aaa-111" in goal
    assert "bbb-222" in goal
    exc_section = goal.split('<excluded_unprospected')[1].split('</excluded_unprospected>')[0]
    assert "aaa-111" in exc_section
    assert "bbb-222" in exc_section


def test_goal_gate_no_excluded_tag_when_none_excluded():
    """No <excluded_unprospected> tag is emitted when all items have durable evidence."""
    items = [
        _make_item("id-ok", "Prospected", touches_resources=["file:meridian/server.py:run"]),
    ]
    goal = _build_quick_start_goal(items, pointer_evidence_ids={"id-ok"})
    assert '<excluded_unprospected' not in goal


def test_goal_gate_empty_board_after_exclusion():
    """When all items are excluded, the empty-board branch is returned WITH the tag."""
    items = [
        _make_item("id-x1", "Item X1", touches_resources=["file:meridian/a.py:a"]),
        _make_item("id-x2", "Item X2", touches_resources=["file:meridian/b.py:b"]),
    ]
    goal = _build_quick_start_goal(items, pointer_evidence_ids=set())
    # The exclusion tag must be present with both ids
    assert '<excluded_unprospected count="2">' in goal
    # Item ids must appear ONLY in the exclusion region (not in any claimable list)
    assert '<sprint_items>' not in goal, "No claimable items section when all are excluded"
    exc_section = goal.split('<excluded_unprospected')[1].split('</excluded_unprospected>')[0]
    assert "id-x1" in exc_section
    assert "id-x2" in exc_section


def test_goal_gate_unknown_evidence_fails_open():
    """d5849a67 — when pointer_evidence_ids is None (the batch DB query
    failed), the gate fails OPEN: nothing is excluded on that basis, even for
    resource-declaring items, so a transient DB hiccup never mass-excludes
    the claimable batch (mirrors claim_sprint_item's own fail-open try/except)."""
    items = [
        _make_item("id-unknown", "Item", touches_resources=["file:meridian/a.py:a"]),
    ]
    goal = _build_quick_start_goal(items, pointer_evidence_ids=None)
    assert '<excluded_unprospected' not in goal
    assert "id-unknown" in goal


# ---------------------------------------------------------------------------
# 2. claim_sprint_item gate: unprospected => blocked dict
# ---------------------------------------------------------------------------

def test_claim_blocks_unprospected_item():
    """claim_sprint_item returns a blocked dict for an item that DECLARED
    real code-touching resources but has no durable pointer evidence.

    An item with NO declared touches_resources was never a prospecting
    candidate in the first place (nothing for add_sprint_item's inline
    prospecting to attempt) and is intentionally NOT gated — see
    test_claim_allows_item_without_declared_resources below.
    """
    async def run():
        db = await _make_db()
        proj = await db_module.create_project(db, "Test Project")
        pid = proj["id"]
        item = await db_module.add_sprint_item(
            db, pid, "v1", "Unprospected task",
            touches_resources=["file:meridian/nonexistent_module_xyz.py:no_such_symbol"],
        )
        iid = item["id"]
        # Item declared a resource but has no code_pointers / pointers -> unprospected
        result = await db_module.claim_sprint_item(db, pid, iid)
        assert isinstance(result, dict)
        assert result.get("blocked") is True
        assert result.get("error") == "UNPROSPECTED"
        assert "prospect_bypass" in result.get("reason", "")
        # Item must still be pending (not claimed)
        refetched = await db_module.get_sprint_item(db, iid)
        assert refetched["status"] == "pending"
        await db.close()

    _run(run())


@pytest.mark.asyncio
async def test_claim_allows_item_with_real_durable_pointer(anydb):
    """Cross-backend regression: a resource-declaring item WITH a real
    add_sprint_item_pointer() row must be claimable on BOTH SQLite and
    Postgres.

    Original bug: the claim-time evidence check ran
    ``SELECT COUNT(*) FROM sprint_item_pointers ...`` with no column alias,
    then read the dict-mode result via ``row.get("COUNT(*)")``. Postgres's
    dict_row cursor names an unaliased COUNT(*) column ``count`` (lowercase,
    no parens) -- never the literal string "COUNT(*)" -- so that lookup
    always returned None -> 0 on Postgres, permanently blocking claim on
    every resource-declaring item regardless of real pointer evidence. Only
    caught live against the hosted Postgres backend; SQLite's tuple-mode
    fetch never exercised the dict-key branch at all. Fixed by aliasing the
    column (``AS cnt``) and reading that alias in both branches.
    """
    p = await db_module.create_project(anydb, "pointer-claim-regression")
    pid = p["id"]
    item = await db_module.add_sprint_item(
        anydb, pid, "v1", "Has a real pointer",
        touches_resources=["file:meridian/outputs_indexer.py:annotate_outputs"],
    )
    iid = item["id"]
    await db_module.add_sprint_item_pointer(
        anydb, pid, iid, "code",
        [{
            "uri": "file:meridian/outputs_indexer.py",
            "selector": {"type": "symbol", "qualified_name": "meridian.outputs_indexer.annotate_outputs"},
        }],
    )
    result = await db_module.claim_sprint_item(anydb, pid, iid)
    assert isinstance(result, dict)
    assert not result.get("blocked"), f"expected claim to succeed, got: {result}"
    assert result.get("status") == "in_progress"


def test_claim_allows_item_without_declared_resources():
    """claim_sprint_item does NOT gate an item that declared no touches_resources.

    Such an item was never a prospecting candidate in the first place (manual
    tasks, proposals, docs-only work, or anything filed without explicit
    file/route/tool targets) -- gating it would block the overwhelming
    majority of ordinary items, not just genuinely-risky unprospected ones.
    """
    async def run():
        db = await _make_db()
        proj = await db_module.create_project(db, "Test Project")
        pid = proj["id"]
        item = await db_module.add_sprint_item(db, pid, "v1", "No resources declared")
        iid = item["id"]
        result = await db_module.claim_sprint_item(db, pid, iid)
        assert isinstance(result, dict)
        assert not result.get("blocked")
        assert result.get("status") == "in_progress"
        await db.close()

    _run(run())


def test_claim_allows_bypassed_unprospected_item():
    """claim_sprint_item succeeds when prospect_bypass=True even without evidence."""
    async def run():
        db = await _make_db()
        proj = await db_module.create_project(db, "Test Project")
        pid = proj["id"]
        item = await db_module.add_sprint_item(db, pid, "v1", "Bypassed task")
        iid = item["id"]
        # Set the bypass flag (human override)
        await db_module.patch_sprint_item(db, pid, iid, prospect_bypass=True)
        result = await db_module.claim_sprint_item(db, pid, iid)
        assert isinstance(result, dict)
        # Must NOT be a blocked dict
        assert not result.get("blocked")
        assert result.get("status") == "in_progress"
        await db.close()

    _run(run())


def test_claim_allows_item_with_durable_pointer():
    """claim_sprint_item succeeds for an item with a durable sprint_item_pointer."""
    async def run():
        db = await _make_db()
        proj = await db_module.create_project(db, "Test Project")
        pid = proj["id"]
        item = await db_module.add_sprint_item(db, pid, "v1", "Durable pointer task",
                                               touches_resources=["file:meridian/server.py"])
        iid = item["id"]
        # Add a durable pointer (this is the durable evidence the claim gate checks).
        await db_module.add_sprint_item_pointer(
            db, pid, iid, "code",
            [{"uri": "file:meridian/server.py", "selector": {"type": "range",
              "start_line": 1, "end_line": 10}}],
        )
        result = await db_module.claim_sprint_item(db, pid, iid)
        assert isinstance(result, dict)
        assert not result.get("blocked")
        assert result.get("status") == "in_progress"
        await db.close()

    _run(run())


# ---------------------------------------------------------------------------
# 5. d5849a67 — get_pointer_evidence_item_ids: the batch DB helper generate_handoff
# uses to resolve the same durable-evidence signal claim_sprint_item checks.
# ---------------------------------------------------------------------------

def test_pointer_evidence_batch_returns_ids_with_durable_pointer():
    """Only ids with >=1 sprint_item_pointers row are returned."""
    async def run():
        db = await _make_db()
        proj = await db_module.create_project(db, "Test Project")
        pid = proj["id"]
        item_with = await db_module.add_sprint_item(
            db, pid, "v1", "Has pointer", touches_resources=["file:meridian/server.py"])
        item_without = await db_module.add_sprint_item(
            db, pid, "v1", "No pointer", touches_resources=["file:meridian/other.py"])
        await db_module.add_sprint_item_pointer(
            db, pid, item_with["id"], "code",
            [{"uri": "file:meridian/server.py",
              "selector": {"type": "range", "start_line": 1, "end_line": 5}}],
        )
        ids = await db_module.get_pointer_evidence_item_ids(
            db, [item_with["id"], item_without["id"]]
        )
        assert ids == {item_with["id"]}
        await db.close()

    _run(run())


def test_pointer_evidence_batch_empty_input_returns_empty_set():
    async def run():
        db = await _make_db()
        result = await db_module.get_pointer_evidence_item_ids(db, [])
        assert result == set()
        result_none = await db_module.get_pointer_evidence_item_ids(db, None)
        assert result_none == set()
        await db.close()

    _run(run())


# ---------------------------------------------------------------------------
# 6. d5849a67 — End-to-end consistency: excluded_unprospected agrees with
# claim_sprint_item. This is the exact disagreement the executor hit: 6 of 8
# nominal "batch 1" items were blocked as UNPROSPECTED at claim time despite
# NOT appearing in generate_handoff's own <excluded_unprospected> list. Each
# test below drives BOTH checks against the SAME live-DB item and asserts
# they agree.
# ---------------------------------------------------------------------------

def test_e2e_excluded_item_actually_fails_claim():
    """An item _build_quick_start_goal excludes (using the real DB-backed
    get_pointer_evidence_item_ids helper) must ALSO be refused by
    claim_sprint_item -- the two checks must never disagree.
    """
    async def run():
        db = await _make_db()
        proj = await db_module.create_project(db, "Test Project")
        pid = proj["id"]
        item = await db_module.add_sprint_item(
            db, pid, "v1", "Declared but unprospected",
            touches_resources=["file:meridian/nonexistent_module_xyz.py:no_such_symbol"],
        )
        iid = item["id"]
        pending = [await db_module.get_sprint_item(db, iid)]
        pointer_evidence_ids = await db_module.get_pointer_evidence_item_ids(
            db, [it["id"] for it in pending]
        )
        goal = _build_quick_start_goal(pending, pointer_evidence_ids=pointer_evidence_ids)
        assert '<excluded_unprospected' in goal
        exc_section = goal.split('<excluded_unprospected')[1].split('</excluded_unprospected>')[0]
        assert iid in exc_section

        result = await db_module.claim_sprint_item(db, pid, iid)
        assert isinstance(result, dict)
        assert result.get("blocked") is True
        assert result.get("error") == "UNPROSPECTED"
        await db.close()

    _run(run())


def test_e2e_included_item_actually_succeeds_claim():
    """An item _build_quick_start_goal does NOT exclude (a real durable
    pointer is present) must ALSO succeed at claim_sprint_item.
    """
    async def run():
        db = await _make_db()
        proj = await db_module.create_project(db, "Test Project")
        pid = proj["id"]
        item = await db_module.add_sprint_item(
            db, pid, "v1", "Declared and prospected",
            touches_resources=["file:meridian/server.py:run"],
        )
        iid = item["id"]
        await db_module.add_sprint_item_pointer(
            db, pid, iid, "code",
            [{"uri": "file:meridian/server.py",
              "selector": {"type": "range", "start_line": 1, "end_line": 10}}],
        )
        pending = [await db_module.get_sprint_item(db, iid)]
        pointer_evidence_ids = await db_module.get_pointer_evidence_item_ids(
            db, [it["id"] for it in pending]
        )
        goal = _build_quick_start_goal(pending, pointer_evidence_ids=pointer_evidence_ids)
        assert '<excluded_unprospected' not in goal
        assert iid in goal

        result = await db_module.claim_sprint_item(db, pid, iid)
        assert isinstance(result, dict)
        assert not result.get("blocked")
        assert result.get("status") == "in_progress"
        await db.close()

    _run(run())


def test_e2e_transient_enrichment_pointer_without_durable_row_agrees_with_claim():
    """Reproduces the exact reported drift: an item enriched at handoff time
    with a transient code_pointers/prospect_status='prospected' annotation
    (never persisted to sprint_item_pointers) must be excluded from the goal
    AND refused by claim. Before d5849a67, generate_handoff's exclusion check
    trusted those transient fields and would have silently included this item
    in the claimable batch, while claim_sprint_item (which only ever checks
    the durable table) refused it anyway.
    """
    async def run():
        db = await _make_db()
        proj = await db_module.create_project(db, "Test Project")
        pid = proj["id"]
        item = await db_module.add_sprint_item(
            db, pid, "v1", "Enriched but not durable",
            touches_resources=["file:meridian/x.py:sym"],
        )
        iid = item["id"]
        # Simulate handoff-time enrichment (_annotate_code_pointers): transient
        # fields set on the fetched dict, WITHOUT persisting a
        # sprint_item_pointers row (that function never does).
        fetched = await db_module.get_sprint_item(db, iid)
        fetched["prospect_status"] = "prospected"
        fetched["code_pointers"] = [{"file": "x.py"}]

        pointer_evidence_ids = await db_module.get_pointer_evidence_item_ids(db, [iid])
        goal = _build_quick_start_goal([fetched], pointer_evidence_ids=pointer_evidence_ids)
        assert '<excluded_unprospected' in goal
        exc_section = goal.split('<excluded_unprospected')[1].split('</excluded_unprospected>')[0]
        assert iid in exc_section

        result = await db_module.claim_sprint_item(db, pid, iid)
        assert isinstance(result, dict)
        assert result.get("blocked") is True
        assert result.get("error") == "UNPROSPECTED"
        await db.close()

    _run(run())


# ---------------------------------------------------------------------------
# 3. patch_sprint_item wires prospect_bypass through
# ---------------------------------------------------------------------------

def test_patch_sprint_item_sets_prospect_bypass():
    """patch_sprint_item correctly writes prospect_bypass to the DB."""
    async def run():
        db = await _make_db()
        proj = await db_module.create_project(db, "Test Project")
        pid = proj["id"]
        item = await db_module.add_sprint_item(db, pid, "v1", "Test item")
        iid = item["id"]
        # Default: bypass is 0
        refetched = await db_module.get_sprint_item(db, iid)
        assert int(refetched.get("prospect_bypass") or 0) == 0
        # Set to True
        updated = await db_module.patch_sprint_item(db, pid, iid, prospect_bypass=True)
        assert int(updated.get("prospect_bypass") or 0) == 1
        # Clear back to False
        cleared = await db_module.patch_sprint_item(db, pid, iid, prospect_bypass=False)
        assert int(cleared.get("prospect_bypass") or 0) == 0
        await db.close()

    _run(run())


def test_patch_sprint_item_unset_leaves_prospect_bypass_unchanged():
    """Omitting prospect_bypass from patch_sprint_item leaves the stored value untouched."""
    async def run():
        db = await _make_db()
        proj = await db_module.create_project(db, "Test Project")
        pid = proj["id"]
        item = await db_module.add_sprint_item(db, pid, "v1", "Test item")
        iid = item["id"]
        # Set to True
        await db_module.patch_sprint_item(db, pid, iid, prospect_bypass=True)
        # Update something else (title) — prospect_bypass must not be touched
        updated = await db_module.patch_sprint_item(db, pid, iid, title="New title")
        assert int(updated.get("prospect_bypass") or 0) == 1
        await db.close()

    _run(run())


# ---------------------------------------------------------------------------
# 4. Migration: prospect_bypass column exists with correct default
# ---------------------------------------------------------------------------

def test_migration_prospect_bypass_column_exists():
    """After init_db, sprint_items has a prospect_bypass column defaulting to 0."""
    async def run():
        db = await _make_db()
        proj = await db_module.create_project(db, "Test Project")
        pid = proj["id"]
        item = await db_module.add_sprint_item(db, pid, "v1", "Check column")
        iid = item["id"]
        fetched = await db_module.get_sprint_item(db, iid)
        # Column must exist (not raise KeyError, not be absent)
        assert "prospect_bypass" in fetched
        assert int(fetched["prospect_bypass"] or 0) == 0
        await db.close()

    _run(run())
