"""Tests for cffb9323 — explicit item-scoped executor handoffs.

The 2026-08-05 parallel follow-up exposed a concrete gap: generate_handoff
had no INCLUDE-ONLY item/group filter. force_include_ids only ever WIDENS
the pending list (re-adds specific deferred ids); there was no way to
NARROW a handoff down to just the ids a caller names, so a supposedly
isolated executor handoff for a two-item follow-up still emitted the whole
eligible version backlog — able to overlap an active wave/batch a sibling
session already owns.

Covers:
  - _resolve_selected_item_scope: validation (not_found/wrong_project/
    wrong_version/in_progress/not_pending) and dependency-closure semantics.
  - generate_handoff(selected_item_ids=...): every executable mode (full,
    delta, starter, goal) narrows its pending batch to the SAME closure and
    renders the SAME <selected_item_scope> declaration.
  - Fail-closed contract: an invalid selection raises HandoffSelectionError
    BEFORE anything is rendered/persisted/token-minted — mirrors
    HandoffStaleReferenceError's own regression coverage in
    test_ee8a6af1_handoff_stale_references.py.
  - Body/token verifiability: the selected-scope declaration lives inside
    quick_start_goal BEFORE the provenance token is minted, so it is bound
    into the same body-hash mechanism (efaa918a) as the rest of the /goal —
    verify_handoff_token still succeeds on a scoped handoff.

7a373f41 adds connector-schema parity coverage: everything above only
exercised the core generate_handoff function directly. The hosted HTTP MCP
dispatch (meridian/mcp/handler.py) and REST route (meridian/routes/handoff.py)
already threaded selected_item_ids through and handled both
HandoffSelectionError (HANDOFF_SELECTION_BLOCKED) and
HandoffScopeNonExecutable (HANDOFF_SCOPE_NON_EXECUTABLE) with structured
refusals, but had ZERO test coverage proving it; the stdio transport
(meridian/mcp/stdio_handler.py, covered separately in
tests/test_stdio_handoff_arg_parity.py) was missing the
HandoffScopeNonExecutable except clause entirely — a real parity gap, fixed
alongside this sprint item. The tests below mirror
test_ee8a6af1_handoff_stale_references.py's own
"Transport-layer structured refusals" section for the same two exception
types, through both the MCP dispatch and REST surfaces.
"""
from __future__ import annotations

import hashlib
import json
import re

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module
import meridian.server  # noqa: F401 — load the server before handler to avoid its import cycle
from meridian.mcp import handler as mcp_handler


_TOKEN_RE = re.compile(r"<goal_token>([^<]+)</goal_token>")
_SCOPE_TAG_RE = re.compile(
    r'<selected_item_scope requested="([^"]*)" closure="([^"]*)" '
    r'closure_hash="([^"]*)">'
)
_CONTINUATION_MANIFEST_RE = re.compile(
    r"<continuation_manifest>.*?</continuation_manifest>", re.DOTALL,
)


def _extract_token(text: str) -> str | None:
    m = _TOKEN_RE.search(text or "")
    return m.group(1).strip() if m else None


def _strip_continuation_manifest(text: str) -> str:
    """delta mode embeds a <continuation_manifest> JSON tag (836ca1d5) that is
    DELIBERATELY a whole-board revision/staleness signal — the SAME canonical,
    project-wide snapshot ledger start_wave_run/resume_wave share — not part
    of the executable/claimable scope this item protects. It legitimately
    carries every non-done item's id (capped) regardless of selected_item_ids
    so a resuming session can detect ANY board change, not just one inside a
    narrow scope. Strip it before asserting an unrelated item is absent from
    the rendered handoff, so this test targets the actual claimable-scope
    surface (the /goal block and its <selected_item_scope> declaration)
    rather than that unrelated, intentionally-unscoped side channel."""
    return _CONTINUATION_MANIFEST_RE.sub("", text or "")


def _extract_scope_tag(text: str):
    m = _SCOPE_TAG_RE.search(text or "")
    if m is None:
        return None
    requested, closure, closure_hash = m.groups()
    return {
        "requested": [i for i in requested.split(", ") if i],
        "closure": [i for i in closure.split(", ") if i],
        "closure_hash": closure_hash,
    }


def _expected_closure_hash(closure_ids: list[str]) -> str:
    """Independent re-derivation of _resolve_selected_item_scope's hash —
    deliberately NOT importing the private helper, so this test proves the
    documented contract (sha256 of the canonical-JSON sorted closure id
    list) rather than merely mirroring the implementation."""
    canonical = json.dumps(sorted(closure_ids), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


async def _count_handoff_tokens(db, project_id: str) -> int:
    async with db.execute(
        "SELECT COUNT(*) AS c FROM handoff_tokens WHERE project_id = ?",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return 0
    return row["c"] if isinstance(row, dict) else row[0]


# ---------------------------------------------------------------------------
# Core acceptance criterion: a two-item parallel follow-up handoff excludes
# unrelated pending items, across every executable mode.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["goal", "starter", "full", "delta"])
async def test_selected_item_ids_excludes_unrelated_pending_items(db, tmp_path, mode):
    pid = await _project(db, f"selection-excludes-unrelated-{mode}")
    a = await db_module.add_sprint_item(db, pid, "v1", "follow-up item A", force=True)
    b = await db_module.add_sprint_item(db, pid, "v1", "follow-up item B", force=True)
    # Simulate an active, unrelated "Batch 7" the parallel follow-up must
    # never see or overlap.
    batch7_1 = await db_module.add_sprint_item(db, pid, "v1", "unrelated batch7 item one", force=True)
    batch7_2 = await db_module.add_sprint_item(db, pid, "v1", "unrelated batch7 item two", force=True)
    batch7_3 = await db_module.add_sprint_item(db, pid, "v1", "unrelated batch7 item three", force=True)

    out_dir = tmp_path / mode
    out_dir.mkdir()
    _path, content, _amended = await handoff_module.generate_handoff(
        db, pid, str(out_dir), skip_ai_summary=True, mode=mode,
        selected_item_ids=[a["id"], b["id"]],
    )

    assert a["id"] in content
    assert b["id"] in content
    _claimable_scope_text = _strip_continuation_manifest(content)
    for unrelated in (batch7_1, batch7_2, batch7_3):
        assert unrelated["id"] not in _claimable_scope_text, (
            f"mode={mode}: unrelated pending item {unrelated['id']} leaked into "
            "an item-scoped handoff's claimable scope"
        )

    # Body/token verifiable: the scope declaration is bound into the SAME
    # body-hash the provenance token was minted for (efaa918a).
    token = _extract_token(content)
    assert token, f"mode={mode} must still mint a genuine provenance token"
    verify = await handoff_module.verify_handoff_token(db, token, pid)
    assert verify == {"valid": True, "reason": "ok"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["goal", "starter", "full", "delta"])
async def test_selected_item_scope_tag_present_and_hash_matches(db, tmp_path, mode):
    pid = await _project(db, f"selection-scope-tag-{mode}")
    a = await db_module.add_sprint_item(db, pid, "v1", "scoped item A", force=True)
    b = await db_module.add_sprint_item(db, pid, "v1", "scoped item B", force=True)
    await db_module.add_sprint_item(db, pid, "v1", "unrelated item", force=True)

    out_dir = tmp_path / mode
    out_dir.mkdir()
    _path, content, _amended = await handoff_module.generate_handoff(
        db, pid, str(out_dir), skip_ai_summary=True, mode=mode,
        selected_item_ids=[a["id"], b["id"]],
    )

    tag = _extract_scope_tag(content)
    assert tag is not None, f"mode={mode} must render <selected_item_scope>"
    assert sorted(tag["requested"]) == sorted([a["id"], b["id"]])
    assert sorted(tag["closure"]) == sorted([a["id"], b["id"]])
    assert tag["closure_hash"] == _expected_closure_hash([a["id"], b["id"]])


@pytest.mark.asyncio
async def test_no_selected_item_ids_renders_no_scope_tag(db, tmp_path):
    """Purely additive: a caller that never passes selected_item_ids sees no
    <selected_item_scope> tag at all — byte-for-byte the pre-cffb9323 shape."""
    pid = await _project(db, "selection-omitted-no-tag")
    await db_module.add_sprint_item(db, pid, "v1", "ordinary item", force=True)

    _path, content, _amended = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
    )
    assert "<selected_item_scope" not in content
    assert _extract_scope_tag(content) is None


# ---------------------------------------------------------------------------
# Dependency-closure semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dependency_closure_pulls_in_pending_parent(db, tmp_path):
    pid = await _project(db, "selection-closure-pending-parent")
    parent = await db_module.add_sprint_item(db, pid, "v1", "prerequisite, still pending", force=True)
    child = await db_module.add_sprint_item(
        db, pid, "v1", "follow-up item", depends_on=parent["id"], force=True,)
    unrelated = await db_module.add_sprint_item(db, pid, "v1", "unrelated pending item", force=True)

    _path, content, _amended = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
        selected_item_ids=[child["id"]],
    )

    tag = _extract_scope_tag(content)
    assert tag["requested"] == [child["id"]]
    assert sorted(tag["closure"]) == sorted([child["id"], parent["id"]])
    assert parent["id"] in content
    assert child["id"] in content
    assert unrelated["id"] not in content


@pytest.mark.asyncio
async def test_dependency_closure_skips_done_dependency(db, tmp_path):
    """A satisfied (done) dependency needs no seat in the scope — it is not
    pulled into the closure, and its absence must not break rendering."""
    pid = await _project(db, "selection-closure-done-parent")
    parent = await db_module.add_sprint_item(db, pid, "v1", "prerequisite, already done", force=True)
    await db_module.complete_sprint_item(db, pid, parent["id"])
    child = await db_module.add_sprint_item(
        db, pid, "v1", "follow-up item", depends_on=parent["id"], force=True,)

    _path, content, _amended = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
        selected_item_ids=[child["id"]],
    )

    tag = _extract_scope_tag(content)
    assert tag["closure"] == [child["id"]]
    assert child["id"] in content


@pytest.mark.asyncio
async def test_dependency_closure_transitive_chain(db, tmp_path):
    pid = await _project(db, "selection-closure-transitive")
    grandparent = await db_module.add_sprint_item(db, pid, "v1", "grandparent, pending", force=True)
    parent = await db_module.add_sprint_item(
        db, pid, "v1", "parent, pending", depends_on=grandparent["id"], force=True,)
    child = await db_module.add_sprint_item(
        db, pid, "v1", "child, pending", depends_on=parent["id"], force=True,)

    _path, content, _amended = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
        selected_item_ids=[child["id"]],
    )
    tag = _extract_scope_tag(content)
    assert sorted(tag["closure"]) == sorted(
        [child["id"], parent["id"], grandparent["id"]]
    )


# ---------------------------------------------------------------------------
# Fail-closed validation — missing/foreign/in_progress/not_pending/wrong_version
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejects_unknown_id_not_found(db, tmp_path):
    pid = await _project(db, "selection-reject-not-found")
    await db_module.add_sprint_item(db, pid, "v1", "real item", force=True)

    tokens_before = await _count_handoff_tokens(db, pid)
    with pytest.raises(handoff_module.HandoffSelectionError) as excinfo:
        await handoff_module.generate_handoff(
            db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
            selected_item_ids=["totally-made-up-item-id"],
        )
    exc = excinfo.value
    assert exc.project_id == pid
    assert exc.rejected == [{"id": "totally-made-up-item-id", "reason": "not_found"}]
    assert list(tmp_path.iterdir()) == [], "nothing must be written on refusal"
    assert await _count_handoff_tokens(db, pid) == tokens_before, (
        "no provenance token may be minted for a refused selection"
    )


@pytest.mark.asyncio
async def test_rejects_foreign_project_id(db, tmp_path):
    pid_a = await _project(db, "selection-reject-foreign-a")
    pid_b = await _project(db, "selection-reject-foreign-b")
    foreign = await db_module.add_sprint_item(db, pid_b, "v1", "lives in project B", force=True)

    with pytest.raises(handoff_module.HandoffSelectionError) as excinfo:
        await handoff_module.generate_handoff(
            db, pid_a, str(tmp_path), skip_ai_summary=True, mode="goal",
            selected_item_ids=[foreign["id"]],
        )
    assert excinfo.value.rejected == [{"id": foreign["id"], "reason": "wrong_project"}]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_rejects_in_progress_id(db, tmp_path):
    """The exact failure mode named in the sprint-item notes: pulling an
    already-claimed (in_progress) item into a supposedly isolated scope
    would defeat the entire point of this feature."""
    pid = await _project(db, "selection-reject-in-progress")
    claimed = await db_module.add_sprint_item(db, pid, "v1", "claimed by a sibling", force=True)
    await db_module.claim_sprint_item(db, pid, claimed["id"], actor="sibling-session")

    with pytest.raises(handoff_module.HandoffSelectionError) as excinfo:
        await handoff_module.generate_handoff(
            db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
            selected_item_ids=[claimed["id"]],
        )
    rejected = excinfo.value.rejected
    assert len(rejected) == 1
    assert rejected[0]["id"] == claimed["id"]
    assert rejected[0]["reason"] == "in_progress"
    assert rejected[0]["claimed_by"] == "sibling-session"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_rejects_done_id_as_not_pending(db, tmp_path):
    pid = await _project(db, "selection-reject-done")
    done_item = await db_module.add_sprint_item(db, pid, "v1", "already shipped", force=True)
    await db_module.complete_sprint_item(db, pid, done_item["id"])

    with pytest.raises(handoff_module.HandoffSelectionError) as excinfo:
        await handoff_module.generate_handoff(
            db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
            selected_item_ids=[done_item["id"]],
        )
    rejected = excinfo.value.rejected
    assert rejected == [{"id": done_item["id"], "reason": "not_pending", "status": "done"}]


@pytest.mark.asyncio
async def test_rejects_skipped_id_as_not_pending(db, tmp_path):
    pid = await _project(db, "selection-reject-skipped")
    skipped_item = await db_module.add_sprint_item(db, pid, "v1", "intentionally skipped", force=True)
    await db_module.skip_sprint_item(db, pid, skipped_item["id"], reason="no longer needed")

    with pytest.raises(handoff_module.HandoffSelectionError) as excinfo:
        await handoff_module.generate_handoff(
            db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
            selected_item_ids=[skipped_item["id"]],
        )
    assert excinfo.value.rejected[0]["reason"] == "not_pending"


@pytest.mark.asyncio
async def test_rejects_wrong_version_id_when_version_scoped(db, tmp_path):
    pid = await _project(db, "selection-reject-wrong-version")
    v2_item = await db_module.add_sprint_item(db, pid, "v2", "lives in v2", force=True)
    await db_module.add_sprint_item(db, pid, "v1", "lives in v1", force=True)

    with pytest.raises(handoff_module.HandoffSelectionError) as excinfo:
        await handoff_module.generate_handoff(
            db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
            version="v1", selected_item_ids=[v2_item["id"]],
        )
    rejected = excinfo.value.rejected
    assert rejected[0]["id"] == v2_item["id"]
    assert rejected[0]["reason"] == "wrong_version"
    assert rejected[0]["item_version"] == "v2"
    assert rejected[0]["requested_version"] == "v1"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_rejects_multiple_invalid_ids_reports_all(db, tmp_path):
    """Every invalid id in the selection is reported, not just the first."""
    pid = await _project(db, "selection-reject-multiple")
    done_item = await db_module.add_sprint_item(db, pid, "v1", "already shipped", force=True)
    await db_module.complete_sprint_item(db, pid, done_item["id"])

    with pytest.raises(handoff_module.HandoffSelectionError) as excinfo:
        await handoff_module.generate_handoff(
            db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
            selected_item_ids=["ghost-id", done_item["id"]],
        )
    reasons = {r["id"]: r["reason"] for r in excinfo.value.rejected}
    assert reasons == {"ghost-id": "not_found", done_item["id"]: "not_pending"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["goal", "starter", "full", "delta"])
async def test_invalid_selection_blocks_every_mode(db, tmp_path, mode):
    pid = await _project(db, f"selection-reject-every-mode-{mode}")
    out_dir = tmp_path / mode
    out_dir.mkdir()
    with pytest.raises(handoff_module.HandoffSelectionError):
        await handoff_module.generate_handoff(
            db, pid, str(out_dir), skip_ai_summary=True, mode=mode,
            selected_item_ids=["nonexistent-id"],
        )
    assert list(out_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# Valid ids already visible (already in the pending list) are left alone,
# and duplicate ids in the request are de-duplicated without error.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_requested_ids_deduplicated(db, tmp_path):
    pid = await _project(db, "selection-dedup")
    a = await db_module.add_sprint_item(db, pid, "v1", "item A", force=True)
    await db_module.add_sprint_item(db, pid, "v1", "unrelated item", force=True)

    _path, content, _amended = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
        selected_item_ids=[a["id"], a["id"]],
    )
    tag = _extract_scope_tag(content)
    assert tag["requested"] == [a["id"]]
    assert tag["closure"] == [a["id"]]


@pytest.mark.asyncio
async def test_empty_list_is_treated_as_no_selection(db, tmp_path):
    """An empty selected_item_ids list is falsy — same as omitting the
    parameter entirely (no scope tag, full eligible backlog visible)."""
    pid = await _project(db, "selection-empty-list")
    item = await db_module.add_sprint_item(db, pid, "v1", "ordinary item", force=True)

    _path, content, _amended = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
        selected_item_ids=[],
    )
    assert _extract_scope_tag(content) is None
    assert item["id"] in content


# ---------------------------------------------------------------------------
# fb82e51f — a selected_item_ids scope that validates cleanly in
# _resolve_selected_item_scope (genuinely todo/pending, right project/version)
# can still collapse to zero executable items once _build_quick_start_goal's
# OWN, separate exclusion filters (manual/backburner/unprospected/wave-gate)
# run. That must fail visibly (HandoffScopeNonExecutable) rather than persist
# and hand back a /goal whose declared scope has nothing claimable in it —
# the exact 2026-07-27 incident recorded in this item's notes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["goal", "starter", "full", "delta"])
async def test_scope_non_executable_when_sole_requested_item_is_manual(db, tmp_path, mode):
    """The fail-closed exception fires uniformly across every mode, but the
    reported ``reason`` legitimately differs: full/delta/goal-only fetch
    pending items with ``include_human=False`` (which ALSO excludes
    ``blocker_kind='manual'`` items — see get_sprint_items's own docstring),
    so the item is already absent by the time _build_quick_start_goal's own
    manual-item tracking runs, and the outcome falls back to
    ``not_in_pending_batch``. starter mode's fetch has no such DB-level
    filter, so its own in-function manual-item split catches it and reports
    the more specific ``manual`` reason. Both are genuinely non-executable —
    this test pins the (mode-dependent) reason so a future change to either
    fetch path is caught rather than silently drifting."""
    pid = await _project(db, f"selection-scope-non-executable-manual-{mode}")
    manual_item = await db_module.add_sprint_item(
        db, pid, "v1", "configure PyPI trusted publisher", blocker_kind="manual", force=True,
    )

    out_dir = tmp_path / mode
    out_dir.mkdir()
    tokens_before = await _count_handoff_tokens(db, pid)
    with pytest.raises(handoff_module.HandoffScopeNonExecutable) as excinfo:
        await handoff_module.generate_handoff(
            db, pid, str(out_dir), skip_ai_summary=True, mode=mode,
            selected_item_ids=[manual_item["id"]],
        )
    exc = excinfo.value
    assert exc.project_id == pid
    assert exc.requested_ids == [manual_item["id"]]
    expected_reason = "manual" if mode == "starter" else "not_in_pending_batch"
    assert exc.excluded == [{"id": manual_item["id"], "reason": expected_reason}]
    assert list(out_dir.iterdir()) == [], "nothing must be written on refusal"
    assert await _count_handoff_tokens(db, pid) == tokens_before, (
        "no provenance token may be minted for a scope that collapses to "
        "zero executable items"
    )


@pytest.mark.asyncio
async def test_scope_non_executable_when_sole_requested_item_is_backburner(db, tmp_path):
    pid = await _project(db, "selection-scope-non-executable-backburner")
    backburner_item = await db_module.add_sprint_item(
        db, pid, "v1", "backburnered follow-up", track="backburner", force=True,
    )

    with pytest.raises(handoff_module.HandoffScopeNonExecutable) as excinfo:
        await handoff_module.generate_handoff(
            db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
            selected_item_ids=[backburner_item["id"]],
        )
    exc = excinfo.value
    assert exc.requested_ids == [backburner_item["id"]]
    assert exc.excluded == [{"id": backburner_item["id"], "reason": "backburner"}]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_scope_stays_executable_when_only_some_requested_ids_excluded(db, tmp_path):
    """A selection that mixes a genuinely-claimable item with a manual one
    must NOT fail closed — HandoffScopeNonExecutable only fires when EVERY
    requested id is excluded. The claimable item still renders normally."""
    pid = await _project(db, "selection-scope-partial-exclusion")
    ordinary_item = await db_module.add_sprint_item(db, pid, "v1", "ordinary claimable item", force=True)
    manual_item = await db_module.add_sprint_item(
        db, pid, "v1", "configure PyPI trusted publisher", blocker_kind="manual", force=True,
    )

    _path, content, _amended = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
        selected_item_ids=[ordinary_item["id"], manual_item["id"]],
    )
    assert ordinary_item["id"] in content
    token = _extract_token(content)
    assert token, "a partially-executable scope must still mint a genuine token"
    verify = await handoff_module.verify_handoff_token(db, token, pid)
    assert verify == {"valid": True, "reason": "ok"}


# ---------------------------------------------------------------------------
# 7a373f41 — connector-schema parity: the hosted HTTP MCP dispatch
# (meridian/mcp/handler.py) and REST route (meridian/routes/handoff.py) must
# return the SAME structured refusal shape as the core function raises,
# mirroring test_ee8a6af1_handoff_stale_references.py's own
# "Transport-layer structured refusals" section. The stdio transport gets
# its own dedicated coverage in tests/test_stdio_handoff_arg_parity.py
# (that is where the actual missing-except-clause bug lived and was fixed).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_dispatch_selection_error_returns_structured_error(db, tmp_path):
    pid = await _project(db, "selection-mcp-dispatch-error")
    await db_module.add_sprint_item(db, pid, "v1", "real pending item", force=True)

    result = await mcp_handler._handle_task_tools(
        "generate_handoff",
        {
            "project_id": pid, "mode": "goal",
            "selected_item_ids": ["totally-made-up-item-id"],
        },
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    assert result["error"] == "HANDOFF_SELECTION_BLOCKED"
    assert result["project_id"] == pid
    assert result["selection_rejected"] == [
        {"id": "totally-made-up-item-id", "reason": "not_found"}
    ]
    assert "content" not in result
    assert "path" not in result


@pytest.mark.asyncio
async def test_mcp_dispatch_scope_non_executable_returns_structured_error(db, tmp_path):
    pid = await _project(db, "selection-mcp-dispatch-non-executable")
    manual_item = await db_module.add_sprint_item(
        db, pid, "v1", "configure PyPI trusted publisher",
        blocker_kind="manual", force=True,
    )

    result = await mcp_handler._handle_task_tools(
        "generate_handoff",
        {
            "project_id": pid, "mode": "goal",
            "selected_item_ids": [manual_item["id"]],
        },
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    assert result["error"] == "HANDOFF_SCOPE_NON_EXECUTABLE"
    assert result["project_id"] == pid
    assert result["requested_ids"] == [manual_item["id"]]
    assert result["excluded_requested"] == [
        {"id": manual_item["id"], "reason": "not_in_pending_batch"}
    ]
    assert "content" not in result
    assert "path" not in result


def test_routes_handoff_endpoint_selection_error_returns_structured_422(client):
    project = client.post(
        "/projects", json={"name": "selection-http-error"}
    ).json()
    pid = project["id"]
    client.post(
        f"/projects/{pid}/sprint-items",
        json={"version": "v1", "title": "real pending item"},
    )

    r = client.post(
        f"/projects/{pid}/handoff",
        json={"mode": "goal", "selected_item_ids": ["totally-made-up-item-id"]},
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "HANDOFF_SELECTION_BLOCKED"
    assert detail["project_id"] == pid
    assert detail["selection_rejected"] == [
        {"id": "totally-made-up-item-id", "reason": "not_found"}
    ]


# Note: there is no REST-level HandoffScopeNonExecutable test alongside the
# HandoffSelectionError one above — the public sprint-items REST creation
# endpoint (routes/sprint.py's add_sprint_item_endpoint) does not accept
# blocker_kind/track, so a "manual"/"backburner" item (the only way to
# reliably trigger HandoffScopeNonExecutable) cannot be constructed through
# REST-only calls without expanding that unrelated endpoint's schema, which
# is out of scope for this sprint item. routes/handoff.py's
# HandoffScopeNonExecutable except-block is structurally identical to (and
# directly adjacent to) the HandoffSelectionError block just exercised above,
# and the same exception is independently covered end-to-end at the core
# function (test_scope_non_executable_when_sole_requested_item_is_manual
# etc. above) and the hosted MCP dispatch
# (test_mcp_dispatch_scope_non_executable_returns_structured_error above)
# and stdio transport (tests/test_stdio_handoff_arg_parity.py) layers.
