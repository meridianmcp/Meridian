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
"""
from __future__ import annotations

import hashlib
import json
import re

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


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
