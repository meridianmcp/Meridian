"""Tests for sprint item 537a7cef — generate_handoff's ``capability_contract``
field returning ~440KB instead of the documented compact summary object,
breaking full/delta/goal modes alike.

Confirmed root causes this file guards against (see the discovery brief and
the fix's own inline comments for the full trace):

* Root cause A — every ``generate_handoff`` call site (MCP tool dispatch in
  ``mcp/handler.py``, and both REST endpoints in ``routes/handoff.py``)
  omitted ``max_executor_contracts``/``max_contract_list_items``, so they got
  ``capability_contract.build_capability_contract``'s own generous 25/200
  defaults instead of a small, handoff-appropriate bound — for EVERY mode
  (full/delta/goal/starter alike), not just starter/goal's already-bounded
  /goal text.
* Root cause B — ``requested.capabilities``/``effective.capabilities``
  embedded the FULL manifest/resolved-profile list verbatim with NO cap of
  any kind, not even under start_session's own 0/0 compact caps.
* Root cause E (new, found live during this fix's own verification) —
  ``item_routing_summary`` was never wired to ``max_contract_list_items`` at
  all: on a real project's compact ``start_session`` response it alone
  accounted for ~25KB of a ~30KB ``capability_contract`` (83%) at 96 pending
  items — the single largest observed contributor, and (by extension) the
  dominant driver on the much larger board the item's own repro describes.
* Root cause D — ``db.proposal_links.get_proposal_evidence`` hydrated every
  linked note/finding/sprint_item/decision/artifact for a proposal with no
  per-bucket bound at all.

Root cause C (``selected_item_ids`` scoping not reaching the contract build)
is a real, separately-confirmed scope-correctness gap but is deliberately
NOT fixed here — see the executor's own final report for why it was
deferred rather than folded into this size-focused pass.
"""
from __future__ import annotations

import json

from meridian import capability_contract as cc
from meridian import db as db_module
from meridian import executor_contract as ec
import meridian.server  # noqa: F401 — load the server before handler to avoid its import cycle
from meridian.mcp import handler as mcp_handler


def _valid_capability(**overrides):
    base = {
        "id": "code-search",
        "purpose": "find symbols/functions/classes",
        "required_tools": ["Serena: find_symbol"],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Root cause E — item_routing_summary was never capped at all.
# ---------------------------------------------------------------------------


async def test_item_routing_summary_capped_deterministically(db):
    p = await db_module.create_project(db, "cc-routing-summary-cap")
    ids = []
    titles = [
        "Investigate the parser regression",
        "Explore the codebase for dead code paths",
        "Claim sprint items for the current wave",
        "Trace the root cause of the intermittent crash",
        "Audit the migration guard for correctness",
    ]
    for t in titles:
        it = await db_module.add_sprint_item(db, p["id"], "v1", t, force=True)
        ids.append(it["id"])
    ids.sort()

    contract = await cc.build_capability_contract(db, p["id"], max_contract_list_items=2)
    assert len(contract["item_routing_summary"]) == 2
    assert contract["item_routing_summary_truncated"] == {
        "truncated": True, "total_candidates": 5, "included": 2,
    }
    # Deterministic subset: the two lowest sprint-item ids, every time.
    assert [h["item_id"] for h in contract["item_routing_summary"]] == ids[:2]

    # The hash is computed over the FULL (pre-cap) summary, preserving its
    # documented parity semantics with an independent build_routing_summary
    # call over the same live items — only the embedded LIST is truncated,
    # never the hash's input.
    live_items = await db_module.get_sprint_items(db, p["id"], version="v1")
    full_summary = ec.build_routing_summary(live_items)
    assert len(full_summary) == 5
    assert contract["item_routing_summary_hash"] == ec.routing_summary_hash(full_summary)

    # Deterministic across repeated builds.
    contract_b = await cc.build_capability_contract(db, p["id"], max_contract_list_items=2)
    assert (
        [h["item_id"] for h in contract_b["item_routing_summary"]]
        == [h["item_id"] for h in contract["item_routing_summary"]]
    )


async def test_item_routing_summary_not_truncated_under_default_cap(db):
    p = await db_module.create_project(db, "cc-routing-summary-under-cap")
    await db_module.add_sprint_item(db, p["id"], "v1", "Investigate the single pending item")
    contract = await cc.build_capability_contract(db, p["id"])
    assert contract["item_routing_summary_truncated"] == {
        "truncated": False, "total_candidates": 1, "included": 1,
    }
    assert len(contract["item_routing_summary"]) == 1


# ---------------------------------------------------------------------------
# Root cause B — requested/effective capability lists were never capped.
# ---------------------------------------------------------------------------


async def test_requested_and_effective_capabilities_capped_deterministically(db):
    project = await db_module.create_project(db, "cc-capability-list-cap")
    caps = [_valid_capability(id=f"cap-{i:02d}") for i in range(5)]
    saved = await db_module.set_project_capability_manifest(db, project["id"], caps)
    assert [c["id"] for c in saved["capabilities"]] == sorted(c["id"] for c in caps)

    contract = await cc.build_capability_contract(
        db, project["id"], max_capability_list_items=2,
    )
    assert len(contract["requested"]["capabilities"]) == 2
    assert contract["requested"]["capabilities_truncated"] == {
        "truncated": True, "total_candidates": 5, "included": 2,
    }
    assert len(contract["effective"]["capabilities"]) == 2
    assert contract["effective"]["capabilities_truncated"] == {
        "truncated": True, "total_candidates": 5, "included": 2,
    }
    # Truncation preserves the manifest's own deterministic (id-sorted)
    # order — a plain prefix take, never a re-sort or random subset.
    assert [c["id"] for c in contract["requested"]["capabilities"]] == [
        "cap-00", "cap-01",
    ]


async def test_capability_list_cap_never_affects_executability_or_hash(db):
    """The cap only bounds what gets EMBEDDED for display — executable/
    missing_required/manifest_hash must still see the FULL capability list,
    even when a required-but-unavailable capability sorts past the cap."""
    project = await db_module.create_project(db, "cc-capability-cap-executability")
    caps = [_valid_capability(id=f"cap-{i:02d}") for i in range(4)]
    caps.append(
        _valid_capability(id="zzz-required-missing", availability_policy="required")
    )
    saved = await db_module.set_project_capability_manifest(db, project["id"], caps)

    def _fake_checker(capabilities):
        return {
            "available": [c["id"] for c in capabilities if c["id"] != "zzz-required-missing"],
            "missing": ["zzz-required-missing"],
            "degraded": [],
        }

    contract = await cc.build_capability_contract(
        db, project["id"], max_capability_list_items=2, availability_checker=_fake_checker,
    )
    # The embedded list is capped to 2 (so "zzz-required-missing" — sorted
    # last — is NOT present in the displayed list)...
    assert len(contract["requested"]["capabilities"]) == 2
    assert "zzz-required-missing" not in [
        c["id"] for c in contract["requested"]["capabilities"]
    ]
    # ...but the executability verdict still reflects the FULL 5-capability
    # set: the missing required capability is still correctly flagged.
    assert contract["executable"] is False
    assert "missing_required_capabilities:zzz-required-missing" in contract["executable_reasons"]
    assert contract["manifest_hash"] == db_module._capability_manifest.manifest_hash(
        saved["capabilities"]
    )


async def test_small_manifest_capabilities_not_truncated_under_default_cap(db):
    project = await db_module.create_project(db, "cc-capability-small-manifest")
    caps = [_valid_capability(id="alpha"), _valid_capability(id="beta")]
    saved = await db_module.set_project_capability_manifest(db, project["id"], caps)

    contract = await cc.build_capability_contract(db, project["id"])
    # Byte-identical to the pre-cap behavior for any manifest at or under
    # the default threshold — the existing exact-equality contract tests
    # (test_capability_contract.py) depend on this.
    assert contract["requested"]["capabilities"] == saved["capabilities"]
    assert contract["effective"]["capabilities"] == saved["capabilities"]
    assert contract["requested"]["capabilities_truncated"] == {
        "truncated": False, "total_candidates": 2, "included": 2,
    }
    assert contract["effective"]["capabilities_truncated"] == {
        "truncated": False, "total_candidates": 2, "included": 2,
    }


# ---------------------------------------------------------------------------
# Root cause A — generate_handoff's own call sites now pass a bound, for
# EVERY mode (full/delta/goal/starter alike) — the item's own repro.
# ---------------------------------------------------------------------------


async def _make_board(db, project_id, count):
    ids = []
    for i in range(count):
        it = await db_module.add_sprint_item(
            db, project_id, "v1",
            f"Investigate large-board regression candidate number {i}",
            force=True,
        )
        ids.append(it["id"])
    return ids


async def test_generate_handoff_capability_contract_bounded_on_large_board_full_delta_goal(
    db, tmp_path,
):
    """The item's own repro: a large board must not blow up
    capability_contract for full/delta/goal modes alike."""
    project = await db_module.create_project(db, "537a7cef-large-board")
    ids = await _make_board(db, project["id"], 40)

    for mode in ("full", "delta", "goal"):
        result = await mcp_handler._handle_task_tools(
            "generate_handoff", {"project_id": project["id"], "mode": mode},
            db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
        )
        contract = result.get("capability_contract")
        assert contract is not None, f"mode={mode} lost capability_contract entirely"

        # Bounded to generate_handoff's own small cap (15), not
        # build_capability_contract's generous raw default (200) or the
        # unbounded pre-fix behavior (40).
        assert len(contract["item_routing_summary"]) <= 15
        assert contract["item_routing_summary_truncated"]["truncated"] is True
        assert contract["item_routing_summary_truncated"]["total_candidates"] == len(ids)
        assert contract["item_routing_summary_truncated"]["included"] <= 15

        for section in (
            "item_tool_requirements", "item_sprint_item_pointers",
            "item_artifact_pointer_findings", "item_executor_contracts",
        ):
            assert len(contract[section]) <= 15, f"mode={mode} section={section}"

        # The overall contract stays genuinely compact — nowhere near the
        # ~440KB the item reports for an unbounded large board. Generous
        # headroom (a real per-item executor_contract can be a few hundred
        # bytes to low KB); this is a regression guard, not a tight bound.
        size = len(json.dumps(contract))
        assert size < 60_000, f"mode={mode} capability_contract is {size} bytes"


async def test_generate_handoff_capability_contract_small_board_unaffected(db, tmp_path):
    """A board at or under the cap must render identically to the pre-fix
    behavior — no spurious truncation on an ordinary small project."""
    project = await db_module.create_project(db, "537a7cef-small-board")
    await db_module.add_sprint_item(db, project["id"], "v1", "Investigate the parser regression")
    await db_module.add_sprint_item(db, project["id"], "v1", "Trace a payments regression")

    for mode in ("full", "delta", "goal", "starter"):
        result = await mcp_handler._handle_task_tools(
            "generate_handoff", {"project_id": project["id"], "mode": mode},
            db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
        )
        contract = result["capability_contract"]
        assert contract["item_routing_summary_truncated"] == {
            "truncated": False, "total_candidates": 2, "included": 2,
        }
        assert len(contract["item_routing_summary"]) == 2


async def test_generate_handoff_capability_contract_deterministic_on_large_board(db, tmp_path):
    """contract_hash/serialize_contract determinism must survive the new
    caps: two generate_handoff calls against the SAME unmutated board
    produce byte-identical capability_contract content."""
    project = await db_module.create_project(db, "537a7cef-determinism")
    await _make_board(db, project["id"], 20)

    result_a = await mcp_handler._handle_task_tools(
        "generate_handoff", {"project_id": project["id"], "mode": "goal"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    result_b = await mcp_handler._handle_task_tools(
        "generate_handoff", {"project_id": project["id"], "mode": "goal"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    cc_a = dict(result_a["capability_contract"])
    cc_b = dict(result_b["capability_contract"])
    for d in (cc_a, cc_b):
        d.pop("generated_at", None)
        d.pop("contract_hash", None)
    assert cc_a == cc_b


# ---------------------------------------------------------------------------
# Root cause D — get_proposal_evidence hydrated every linked entity with no
# per-bucket bound.
# ---------------------------------------------------------------------------


async def test_proposal_evidence_bucket_capped_with_truncation_marker(db):
    project = await db_module.create_project(db, "537a7cef-proposal-bucket-cap")
    for i in range(30):
        await db_module.link_proposal_evidence(
            db, project["id"], "prop-large", "artifact", f"artifact-{i:03d}",
            label=f"generated output {i}",
        )

    evidence = await db_module.get_proposal_evidence(
        db, project["id"], "prop-large", max_bucket_items=5,
    )
    assert evidence["link_count"] == 30  # the TRUE total, never capped
    assert len(evidence["artifacts"]) == 5
    assert evidence["bucket_truncated"]["artifacts"] == {
        "truncated": True, "total_candidates": 30, "included": 5,
    }
    # Untouched buckets report a clean, non-truncated marker too.
    for name in ("notes", "findings", "sprint_items", "decisions"):
        assert evidence["bucket_truncated"][name] == {
            "truncated": False, "total_candidates": 0, "included": 0,
        }


async def test_proposal_evidence_small_case_unaffected_by_default_cap(db):
    project = await db_module.create_project(db, "537a7cef-proposal-bucket-small")
    note = await db_module.add_project_note(
        db, project["id"], "a proposal-linked note", "note body",
    )
    await db_module.link_proposal_evidence(
        db, project["id"], "prop-small", "note", note["id"],
    )
    evidence = await db_module.get_proposal_evidence(db, project["id"], "prop-small")
    assert evidence["link_count"] == 1
    assert len(evidence["notes"]) == 1
    assert evidence["bucket_truncated"]["notes"] == {
        "truncated": False, "total_candidates": 1, "included": 1,
    }
