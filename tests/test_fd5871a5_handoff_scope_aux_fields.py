"""Tests for sprint item fd5871a5 — the genuine remaining gap identified by
this item's own discovery brief: ``generate_handoff``'s ``selected_item_ids``
scoping (cffb9323, fb82e51f, d2fc7465 et al — already fully shipped and
covered by ``tests/test_handoff_item_selection.py``'s 21 tests) correctly
narrows the rendered ``content`` /goal text, but the two STRUCTURED auxiliary
fields emitted alongside it on every ``generate_handoff`` call —
``capability_contract`` and ``proposal_evidence`` — were built with NO scope
awareness at all. A handoff correctly scoped to one item's dependency
closure could still embed ``item_tool_requirements`` /
``item_sprint_item_pointers`` / ``item_artifact_pointer_findings`` /
``item_executor_contracts`` / ``item_routing_summary`` entries for OTHER,
unrelated pending items on the board, and ``proposal_evidence`` for
proposals that touch none of the requested items — reproducing, via a
different field, the exact "unscoped size/content leak" class of bug this
whole item exists to close. This was explicitly flagged as a deliberate,
separate follow-up in commit 3509ae94 / ``tests/test_537a7cef_capability_
contract_bloat.py``'s own "Root cause C ... deliberately NOT fixed here"
note.

Covers the fix:
  * ``meridian.handoff.resolve_closure_items_for_scope`` — new helper that
    turns a ``selected_scope_outcome`` dict's ``closure_item_ids`` into the
    full sprint_item dicts a caller can pass as ``build_effective_
    capability_contract``'s ``items=``, WITHOUT mutating/bloating the public
    ``selected_scope`` response field itself.
  * ``meridian.db.proposal_links.get_proposal_ids_for_items`` — new scoped
    sibling of ``get_proposal_ids_for_project``, filtering to proposals with
    at least one ``sprint_item`` evidence link into a given id set.
  * ``meridian.handoff.build_proposal_evidence_for_handoff``'s new optional
    ``item_ids`` parameter, threaded through to the function above.
  * The two real emit sites now wired to both of the above:
    ``meridian/mcp/handler.py``'s ``generate_handoff`` MCP dispatch, and
    ``meridian/routes/handoff.py``'s general (non-planner) REST endpoint.

Every test asserts the SAME "zero behavior change when selected_item_ids is
never passed" contract this item's discovery brief demanded be preserved.
"""
from __future__ import annotations

import json

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module
import meridian.server  # noqa: F401 — load the server before handler to avoid its import cycle
from meridian.mcp import handler as mcp_handler


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


def _routing_title(n: "int | str") -> str:
    """Same 'Investigate ...' phrasing test_537a7cef's own ``_make_board``
    uses — guarantees ``executor_contract.build_routing_hint`` infers a
    routing hint from the title alone, so ``item_routing_summary`` carries
    one entry per item with no extra per-item setup needed."""
    return f"Investigate large-board regression candidate number {n}"


async def _make_unrelated_board(db, project_id: str, count: int) -> list[str]:
    ids = []
    for i in range(count):
        it = await db_module.add_sprint_item(
            db, project_id, "v1", _routing_title(i), force=True,
        )
        ids.append(it["id"])
    return ids


# ---------------------------------------------------------------------------
# resolve_closure_items_for_scope — pure(ish) helper, DB only for the actual
# per-id sprint_item fetch.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_closure_items_for_scope_none_when_outcome_falsy(db):
    assert await handoff_module.resolve_closure_items_for_scope(db, None) is None
    assert await handoff_module.resolve_closure_items_for_scope(db, {}) is None


@pytest.mark.asyncio
async def test_resolve_closure_items_for_scope_none_when_no_closure_ids(db):
    """A caller that passed selected_item_ids=None to generate_handoff still
    gets a (present but empty-of-closure) selected_scope_outcome dict in some
    code paths -- must resolve to None (unscoped), not an empty list, so a
    downstream `items=None` self-fetches the normal way."""
    outcome = {"selected_item_ids": None, "closure_item_ids": None, "closure_hash": None}
    assert await handoff_module.resolve_closure_items_for_scope(db, outcome) is None


@pytest.mark.asyncio
async def test_resolve_closure_items_for_scope_returns_full_item_dicts(db):
    pid = await _project(db, "resolve-closure-items-basic")
    a = await db_module.add_sprint_item(db, pid, "v1", "closure item A", force=True)
    b = await db_module.add_sprint_item(db, pid, "v1", "closure item B", force=True)
    await db_module.add_sprint_item(db, pid, "v1", "unrelated item", force=True)

    outcome = {"closure_item_ids": sorted([a["id"], b["id"]])}
    items = await handoff_module.resolve_closure_items_for_scope(db, outcome)
    assert items is not None
    assert {it["id"] for it in items} == {a["id"], b["id"]}
    # Full dicts, not bare ids -- a real sprint_item row shape.
    assert all(isinstance(it, dict) and "title" in it for it in items)


@pytest.mark.asyncio
async def test_resolve_closure_items_for_scope_none_when_every_id_unresolvable(db):
    """A transient DB hiccup / already-purged id must degrade to None
    (unscoped fallback), never a misleadingly-empty scoped contract."""
    outcome = {"closure_item_ids": ["nonexistent-id-1", "nonexistent-id-2"]}
    assert await handoff_module.resolve_closure_items_for_scope(db, outcome) is None


@pytest.mark.asyncio
async def test_resolve_closure_items_for_scope_skips_partial_failures(db):
    pid = await _project(db, "resolve-closure-items-partial")
    a = await db_module.add_sprint_item(db, pid, "v1", "resolvable item", force=True)
    outcome = {"closure_item_ids": sorted([a["id"], "ghost-id-does-not-exist"])}
    items = await handoff_module.resolve_closure_items_for_scope(db, outcome)
    assert items is not None
    assert {it["id"] for it in items} == {a["id"]}


@pytest.mark.asyncio
async def test_resolve_closure_items_for_scope_never_raises_on_lookup_failure(db, monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated get_sprint_item failure")

    monkeypatch.setattr(db_module, "get_sprint_item", _boom)
    outcome = {"closure_item_ids": ["whatever"]}
    assert await handoff_module.resolve_closure_items_for_scope(db, outcome) is None


# ---------------------------------------------------------------------------
# db.proposal_links.get_proposal_ids_for_items — scoped sibling of
# get_proposal_ids_for_project.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_proposal_ids_for_items_scoped_to_given_ids(db):
    project = await db_module.create_project(db, "proposal-ids-for-items-scoped")
    item_a = await db_module.add_sprint_item(db, project["id"], "v1", "item A", force=True)
    item_b = await db_module.add_sprint_item(db, project["id"], "v1", "item B", force=True)
    await db_module.link_proposal_evidence(
        db, project["id"], "prop-a", "sprint_item", item_a["id"],
    )
    await db_module.link_proposal_evidence(
        db, project["id"], "prop-b", "sprint_item", item_b["id"],
    )

    ids = await db_module.get_proposal_ids_for_items(db, project["id"], [item_a["id"]])
    assert ids == ["prop-a"]


@pytest.mark.asyncio
async def test_get_proposal_ids_for_items_empty_for_empty_item_ids(db):
    project = await db_module.create_project(db, "proposal-ids-for-items-empty")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "item", force=True)
    await db_module.link_proposal_evidence(
        db, project["id"], "prop-x", "sprint_item", item["id"],
    )
    assert await db_module.get_proposal_ids_for_items(db, project["id"], []) == []
    assert await db_module.get_proposal_ids_for_items(db, project["id"], None) == []


@pytest.mark.asyncio
async def test_get_proposal_ids_for_items_ignores_non_sprint_item_links(db):
    """A proposal linked only via a note/artifact (not a sprint_item) must
    never surface from this ITEM-scoped lookup even if its entity_id string
    happens to collide with a real sprint_item id — the entity_type='sprint_item'
    filter is load-bearing, not just an id match."""
    project = await db_module.create_project(db, "proposal-ids-for-items-type-filter")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "item", force=True)
    note = await db_module.add_project_note(db, project["id"], "N", "body")
    await db_module.link_proposal_evidence(
        db, project["id"], "prop-note-only", "note", note["id"],
    )
    ids = await db_module.get_proposal_ids_for_items(db, project["id"], [item["id"]])
    assert ids == []


# ---------------------------------------------------------------------------
# handoff.build_proposal_evidence_for_handoff(item_ids=...)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_proposal_evidence_for_handoff_scoped_excludes_unrelated_proposal(db):
    project = await db_module.create_project(db, "build-proposal-evidence-scoped")
    item_a = await db_module.add_sprint_item(db, project["id"], "v1", "item A", force=True)
    item_b = await db_module.add_sprint_item(db, project["id"], "v1", "item B", force=True)
    await db_module.link_proposal_evidence(
        db, project["id"], "prop-a", "sprint_item", item_a["id"],
    )
    await db_module.link_proposal_evidence(
        db, project["id"], "prop-b", "sprint_item", item_b["id"],
    )

    scoped = await handoff_module.build_proposal_evidence_for_handoff(
        db, project["id"], item_ids=[item_a["id"]],
    )
    assert scoped is not None
    assert [b["proposal_id"] for b in scoped] == ["prop-a"]

    # Zero-behavior-change contract: omitting item_ids still returns both.
    unscoped = await handoff_module.build_proposal_evidence_for_handoff(db, project["id"])
    assert {b["proposal_id"] for b in unscoped} == {"prop-a", "prop-b"}


@pytest.mark.asyncio
async def test_build_proposal_evidence_for_handoff_empty_item_ids_falls_back_to_unscoped(db):
    """An empty list (falsy) must behave exactly like item_ids=None — the
    project-wide view — not "scoped to nothing"."""
    project = await db_module.create_project(db, "build-proposal-evidence-empty-list")
    item = await db_module.add_sprint_item(db, project["id"], "v1", "item", force=True)
    await db_module.link_proposal_evidence(
        db, project["id"], "prop-only", "sprint_item", item["id"],
    )
    result = await handoff_module.build_proposal_evidence_for_handoff(
        db, project["id"], item_ids=[],
    )
    assert [b["proposal_id"] for b in result] == ["prop-only"]


@pytest.mark.asyncio
async def test_build_proposal_evidence_for_handoff_scoped_never_raises(db, monkeypatch):
    project = await db_module.create_project(db, "build-proposal-evidence-scoped-guarded")

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated get_proposal_ids_for_items failure")

    monkeypatch.setattr(db_module, "get_proposal_ids_for_items", _boom)
    result = await handoff_module.build_proposal_evidence_for_handoff(
        db, project["id"], item_ids=["some-id"],
    )
    assert result is None


# ---------------------------------------------------------------------------
# End-to-end via the REAL mcp/handler.py generate_handoff dispatch — the
# code path this item's own discovery brief flagged as the missing wiring.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["goal", "full"])
async def test_generate_handoff_capability_contract_scoped_to_selected_item_ids(db, tmp_path, mode):
    pid = await _project(db, f"mcp-capability-contract-scoped-{mode}")
    unrelated_ids = await _make_unrelated_board(db, pid, 20)
    selected = await db_module.add_sprint_item(
        db, pid, "v1", _routing_title("selected"), force=True,
    )

    result = await mcp_handler._handle_task_tools(
        "generate_handoff",
        {"project_id": pid, "mode": mode, "selected_item_ids": [selected["id"]]},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    assert "error" not in result, result
    contract = result["capability_contract"]
    assert contract is not None

    for section in ("item_executor_contracts", "item_routing_summary"):
        got_ids = {e["item_id"] for e in contract[section]}
        assert got_ids == {selected["id"]}, (
            f"mode={mode} section={section}: expected only the selected item, "
            f"got {got_ids} (leaked unrelated ids: {got_ids & set(unrelated_ids)})"
        )
        trunc_key = f"{section}_truncated"
        assert contract[trunc_key]["total_candidates"] == 1, (
            f"mode={mode} {trunc_key}: total_candidates must reflect the "
            "1-item CLOSURE, not the 21-item board"
        )
        assert contract[trunc_key]["truncated"] is False


@pytest.mark.asyncio
async def test_generate_handoff_capability_contract_unscoped_call_unaffected(db, tmp_path):
    """Zero behavior change for every pre-existing caller that never passes
    selected_item_ids: item_routing_summary still sees the whole board."""
    pid = await _project(db, "mcp-capability-contract-unscoped")
    ids = await _make_unrelated_board(db, pid, 3)

    result = await mcp_handler._handle_task_tools(
        "generate_handoff", {"project_id": pid, "mode": "goal"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    contract = result["capability_contract"]
    got_ids = {e["item_id"] for e in contract["item_routing_summary"]}
    assert got_ids == set(ids)


@pytest.mark.asyncio
async def test_generate_handoff_proposal_evidence_scoped_to_selected_item_ids(db, tmp_path):
    pid = await _project(db, "mcp-proposal-evidence-scoped")
    item_a = await db_module.add_sprint_item(db, pid, "v1", "item A", force=True)
    item_b = await db_module.add_sprint_item(db, pid, "v1", "item B", force=True)
    await db_module.link_proposal_evidence(db, pid, "prop-a", "sprint_item", item_a["id"])
    await db_module.link_proposal_evidence(db, pid, "prop-b", "sprint_item", item_b["id"])

    result = await mcp_handler._handle_task_tools(
        "generate_handoff",
        {"project_id": pid, "mode": "goal", "selected_item_ids": [item_a["id"]]},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    assert "error" not in result, result
    bundles = result["proposal_evidence"]
    assert [b["proposal_id"] for b in bundles] == ["prop-a"], (
        f"expected only prop-a (linked to the selected item), got {bundles!r}"
    )


@pytest.mark.asyncio
async def test_generate_handoff_unscoped_proposal_evidence_still_returns_all(db, tmp_path):
    pid = await _project(db, "mcp-proposal-evidence-unscoped")
    item_a = await db_module.add_sprint_item(db, pid, "v1", "item A", force=True)
    item_b = await db_module.add_sprint_item(db, pid, "v1", "item B", force=True)
    await db_module.link_proposal_evidence(db, pid, "prop-a", "sprint_item", item_a["id"])
    await db_module.link_proposal_evidence(db, pid, "prop-b", "sprint_item", item_b["id"])

    result = await mcp_handler._handle_task_tools(
        "generate_handoff", {"project_id": pid, "mode": "goal"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    bundles = {b["proposal_id"] for b in result["proposal_evidence"]}
    assert bundles == {"prop-a", "prop-b"}


@pytest.mark.asyncio
async def test_generate_handoff_scoped_aux_fields_include_dependency_closure_ancestor(db, tmp_path):
    """The scoping fix must use the FULL resolved dependency closure (see
    _resolve_selected_item_scope), not just the literally-requested id: an
    ancestor pulled in because it's a still-pending depends_on prerequisite
    must appear in capability_contract's per-item sections AND have its own
    linked proposal surfaced in proposal_evidence — proving both aux fields
    read the SAME closure `content` itself was already scoped to."""
    pid = await _project(db, "mcp-scoped-aux-fields-closure")
    ancestor = await db_module.add_sprint_item(
        db, pid, "v1", _routing_title("ancestor"), force=True,
    )
    child = await db_module.add_sprint_item(
        db, pid, "v1", _routing_title("child"), depends_on=ancestor["id"], force=True,
    )
    unrelated = await db_module.add_sprint_item(
        db, pid, "v1", _routing_title("unrelated"), force=True,
    )
    await db_module.link_proposal_evidence(db, pid, "prop-ancestor", "sprint_item", ancestor["id"])
    await db_module.link_proposal_evidence(db, pid, "prop-unrelated", "sprint_item", unrelated["id"])

    result = await mcp_handler._handle_task_tools(
        "generate_handoff",
        {"project_id": pid, "mode": "goal", "selected_item_ids": [child["id"]]},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    assert "error" not in result, result
    contract = result["capability_contract"]
    routing_ids = {e["item_id"] for e in contract["item_routing_summary"]}
    assert routing_ids == {ancestor["id"], child["id"]}, (
        "capability_contract must reflect the full dependency closure "
        f"(ancestor+child), not just the literal request; got {routing_ids}"
    )
    proposal_ids = {b["proposal_id"] for b in result["proposal_evidence"]}
    assert proposal_ids == {"prop-ancestor"}, (
        "proposal_evidence must surface the ancestor's linked proposal (it "
        "is part of the closure) but never the unrelated item's proposal; "
        f"got {proposal_ids}"
    )


@pytest.mark.asyncio
async def test_generate_handoff_scoped_capability_contract_size_not_proportional_to_board(db, tmp_path):
    """Regression guard mirroring the item's own live repro: a handoff scoped
    to ONE item stays small regardless of how many OTHER unrelated pending
    items exist on the board — capability_contract size must track the
    selected closure, not the board."""
    pid = await _project(db, "mcp-scoped-size-not-proportional")
    await _make_unrelated_board(db, pid, 60)
    selected = await db_module.add_sprint_item(
        db, pid, "v1", _routing_title("selected"), force=True,
    )

    result = await mcp_handler._handle_task_tools(
        "generate_handoff",
        {"project_id": pid, "mode": "goal", "selected_item_ids": [selected["id"]]},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    contract = result["capability_contract"]
    size = len(json.dumps(contract))
    # A single thin synthetic item's contract is a few hundred bytes; this is
    # a generous regression ceiling, not a tight bound — the point is that it
    # does NOT scale with the 60-item unrelated board.
    assert size < 5_000, f"capability_contract for a 1-item scope is {size} bytes"
    for section in ("item_executor_contracts", "item_routing_summary"):
        assert len(contract[section]) == 1, section


# ---------------------------------------------------------------------------
# REST parity — meridian/routes/handoff.py's general (non-planner) endpoint.
# ---------------------------------------------------------------------------


def test_rest_handoff_endpoint_capability_contract_scoped_to_selected_item_ids(client):
    pid = client.post(
        "/projects", json={"name": "rest-capability-contract-scoped"}
    ).json()["id"]
    unrelated_ids = []
    for i in range(5):
        r = client.post(
            f"/projects/{pid}/sprint-items",
            # force=True: these titles are near-duplicates of each other by
            # design (same "Investigate ... number N" template that guarantees
            # a routing hint — see _routing_title's docstring), which the
            # duplicate-title guard would otherwise reject as an 86%+ word
            # match against the previous item.
            json={"version": "v1", "title": _routing_title(i), "force": True},
        )
        unrelated_ids.append(r.json()["id"])
    selected = client.post(
        f"/projects/{pid}/sprint-items",
        json={"version": "v1", "title": _routing_title("selected"), "force": True},
    ).json()

    r = client.post(
        f"/projects/{pid}/handoff",
        json={"mode": "goal", "selected_item_ids": [selected["id"]]},
    )
    assert r.status_code == 200, r.text
    contract = r.json()["capability_contract"]
    got_ids = {e["item_id"] for e in contract["item_routing_summary"]}
    assert got_ids == {selected["id"]}, (
        f"REST endpoint leaked unrelated ids: {got_ids & set(unrelated_ids)}"
    )


def test_rest_handoff_endpoint_unscoped_call_unaffected(client):
    """Zero behavior change for the REST endpoint's existing unscoped callers."""
    pid = client.post(
        "/projects", json={"name": "rest-capability-contract-unscoped"}
    ).json()["id"]
    ids = []
    for i in range(3):
        r = client.post(
            f"/projects/{pid}/sprint-items",
            json={"version": "v1", "title": _routing_title(i), "force": True},
        )
        ids.append(r.json()["id"])

    r = client.post(f"/projects/{pid}/handoff", json={"mode": "goal"})
    assert r.status_code == 200, r.text
    contract = r.json()["capability_contract"]
    got_ids = {e["item_id"] for e in contract["item_routing_summary"]}
    assert got_ids == set(ids)


# ---------------------------------------------------------------------------
# Live-status requery: _resolve_selected_item_scope fetches fresh status per
# id on EVERY call, never a cached/stale snapshot — the discovery brief noted
# this was not clearly exercised by name in the existing 21-test suite.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_selected_scope_reflects_item_claimed_between_two_calls(db, tmp_path):
    pid = await _project(db, "selected-scope-live-requery")
    item = await db_module.add_sprint_item(db, pid, "v1", "requeried item", force=True)

    # First call: item is genuinely pending -- succeeds normally.
    _path, content, _amended = await handoff_module.generate_handoff(
        db, pid, str(tmp_path / "first"), skip_ai_summary=True, mode="goal",
        selected_item_ids=[item["id"]],
    )
    assert item["id"] in content

    # A sibling session claims the SAME item between the two calls.
    await db_module.claim_sprint_item(db, pid, item["id"], actor="sibling-session")

    # Second call with the IDENTICAL selected_item_ids must now see the
    # FRESH in_progress status, not a stale/cached "pending" snapshot from
    # the first call above.
    with pytest.raises(handoff_module.HandoffSelectionError) as excinfo:
        await handoff_module.generate_handoff(
            db, pid, str(tmp_path / "second"), skip_ai_summary=True, mode="goal",
            selected_item_ids=[item["id"]],
        )
    assert excinfo.value.rejected == [
        {"id": item["id"], "reason": "in_progress", "claimed_by": "sibling-session"}
    ]
