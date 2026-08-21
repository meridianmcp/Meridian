"""End-to-end integration gate for the v0.2.7-research-os megasprint
(sprint item bbe3fb3d).

A single, realistic research-proposal scenario that exercises the SIX
sprint items this megasprint shipped, together, against the real production
modules (no mocking of the modules under test themselves -- the only
monkeypatch used is the established os.walk-failure-injection idiom already
used by test_bm25_fallback.py to deterministically simulate a directory-walk
error):

  * ``meridian.proposal_promotion`` (proposal-to-handoff: preview/commit).
  * ``meridian.db.sprint_items.add_sprint_item``'s overlap/duplicate guard
    and ``depends_on`` parent/child DAG wiring.
  * ``meridian.pointers`` / ``add_sprint_item_pointer``'s ``existing`` vs
    ``planned_new`` target_kind contract.
  * ``meridian_codeindex.bm25_index`` -- the hardened local BM25 secondary
    search path, including its explicit degraded/inconclusive vocabulary.
  * ``meridian.outputs_indexer.search_outputs`` -- a partial (not-yet-
    converged) outputs index reported honestly.
  * ``meridian.research_graph`` / ``meridian.db.research_graph`` -- typed
    nodes/edges, an unresolved citation edge, and append-only idempotency.
  * ``meridian.proposal_gates`` -- a typed, lane-blocking HITL gate: raised,
    retried (rejected without reopening -- a real decision receipt),
    reopened, and finally resolved to ``allowed``.
  * ``meridian.handoff.generate_handoff`` -- the unified ``proposal_scope``
    out-param (missing graph/Serena reported as a documented DEGRADED
    capability that stays executable; a genuinely FAILED capability
    correctly flips ``executable`` to False -- never falsely executable
    while something is broken) and the ``research_evidence_envelope``
    bridge to ``extensions/meridian-outputs/meridian_outputs/research_evidence``.
  * ``meridian.docx_integrity_gate`` -- confirmed to run over ZERO real DOCX
    artifacts for this scenario (nothing here ever creates, opens, or edits
    a canonical thesis DOCX -- the explicit acceptance-criteria constraint).

Uses the standard ``db`` fixture (in-memory SQLite via conftest's schema
template) exactly like its sibling integration suites
(tests/test_proposal_handoff_contract.py, tests/test_proposal_hitl_gates.py,
tests/test_research_artifact_graph.py, tests/test_bm25_fallback.py).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# extensions/meridian-outputs is not a pypi-dependency of the main pixi env
# (see reference_testing_detached_extension_packages) -- sys.path insert is
# the SAME convention tests/test_research_evidence_envelope.py already uses
# to import it directly.
sys.path.insert(0, str(Path(__file__).parent.parent / "extensions" / "meridian-outputs"))

from meridian import db as db_module
from meridian import docx_integrity_gate as docx_gate_module
from meridian import handoff as handoff_module
from meridian import outputs_indexer as oi
from meridian import proposal_gates as gates_module
from meridian import proposal_promotion as promo_module
from meridian import research_graph as rg
from meridian_codeindex import bm25_index as bi
from meridian_outputs import research_evidence as RE

pytestmark = pytest.mark.asyncio


def _boom_searcher(_query: str):
    """A graph/code-intel searcher that is AVAILABLE (callable) but ERRORS
    when actually invoked -- the real production distinction between
    ``graph_search_availability`` (was a searcher provided at all) and
    ``code_pointer_enrichment`` (did using it actually work). Used to prove
    ``build_proposal_run_scope`` genuinely flips ``executable`` to False on
    a real FAILED capability, never just on an ordinary DEGRADED one."""
    raise RuntimeError("graph search backend unreachable (simulated)")


async def test_research_proposal_end_to_end_gate(db, tmp_path, monkeypatch):
    # =======================================================================
    # SECTION 0 -- setup
    # =======================================================================
    project = await db_module.create_project(db, "research-os-e2e")
    pid = project["id"]
    session = await db_module.register_session(db, pid, "research-os-e2e-session")
    sid = session["id"]

    # =======================================================================
    # SECTION 1 -- proposal intake, WITH a retry/idempotency check
    # (add_workspace_proposal's idempotency_key contract).
    # =======================================================================
    # Deliberately avoids the citation/docs/experiment title-keyword lists
    # _infer_pointer_source_type checks (meridian/handoff.py) so these items
    # classify as plain 'code' pointer candidates -- Section 11 below needs
    # at least one 'code'-classified item to route through graph_searcher.
    proposal_title = "Add BM25-backed secondary lookup pipeline for research proposals"
    proposal_body = (
        "Investigate and wire a BM25-backed secondary search path for citation "
        "resolution when the primary graph/Serena code-intel path is "
        "unavailable, with typed research-evidence provenance recorded per "
        "claim in the research artifact graph."
    )
    proposal = await db_module.add_workspace_proposal(
        db, proposal_title, proposal_body,
        actor="researcher", session_id=sid,
        idempotency_key="research-os-e2e-proposal-v1",
    )
    proposal_retry = await db_module.add_workspace_proposal(
        db, "DIFFERENT TITLE -- must be ignored on a retry",
        "different body -- must be ignored on a retry",
        actor="researcher", session_id=sid,
        idempotency_key="research-os-e2e-proposal-v1",
    )
    assert proposal_retry["id"] == proposal["id"], (
        "add_workspace_proposal retry with the same idempotency_key must "
        "return the SAME row, not create a second proposal"
    )
    assert proposal_retry["title"] == proposal_title

    # =======================================================================
    # SECTION 2 -- proposal-to-handoff: promote to a real sprint item via the
    # preview/commit contract in meridian.proposal_promotion, WITH a
    # retry/idempotency check (a second commit against an already-promoted
    # proposal must be a safe no-op, never a duplicate sprint item).
    # =======================================================================
    depth = "sprint_items"
    preview = await promo_module.preview_proposal_promotion(
        db, proposal["id"], pid, depth,
        sprint_item_version="v1",
        touches_resources=["meridian/research_graph.py"],
        infer_touches_resources=False,
    )
    assert preview["already_satisfied"] is False
    commit = await promo_module.commit_proposal_promotion(
        db, proposal["id"], pid, depth, preview["preview_hash"],
        actor="researcher", session_id=sid,
        sprint_item_version="v1",
        touches_resources=["meridian/research_graph.py"],
        infer_touches_resources=False,
    )
    assert commit["already_satisfied"] is False
    assert commit["deviation"] is None
    assert commit["hitl_pending"] is False
    parent_id = commit["committed"]["sprint_item"]["id"]
    assert parent_id

    items_after_first_commit = await db_module.get_sprint_items(db, pid)
    count_after_first_commit = len(items_after_first_commit)

    # Retry: commit AGAIN with the SAME preview_hash. commit_proposal_promotion
    # re-previews internally FIRST; since the proposal is now status=promoted,
    # that fresh preview reports already_satisfied=True and the function
    # short-circuits to a no-op BEFORE ever comparing preview_hash values.
    retry_commit = await promo_module.commit_proposal_promotion(
        db, proposal["id"], pid, depth, preview["preview_hash"],
        actor="researcher", session_id=sid,
        sprint_item_version="v1",
        touches_resources=["meridian/research_graph.py"],
        infer_touches_resources=False,
    )
    assert retry_commit["already_satisfied"] is True
    assert retry_commit["committed"] == {}
    items_after_retry = await db_module.get_sprint_items(db, pid)
    assert len(items_after_retry) == count_after_first_commit, (
        "retrying commit_proposal_promotion against an already-promoted "
        "proposal must never create a second sprint item"
    )

    # =======================================================================
    # SECTION 3 -- overlap/duplicate detection when decomposing the proposal
    # into further sprint items (meridian.db.sprint_items.add_sprint_item's
    # b0d42ef6 word-overlap guard), plus the real child-item DAG.
    # =======================================================================
    overlapping_title = "BM25 secondary lookup pipeline hardening"  # >=60% word overlap w/ parent_title
    duplicate_attempt = await db_module.add_sprint_item(
        db, pid, "v1", overlapping_title, prospect_bypass=True,
    )
    assert duplicate_attempt.get("error") == "duplicate", (
        f"expected the overlap guard to reject {overlapping_title!r} as a "
        f"near-duplicate of the parent item's title; got {duplicate_attempt!r}"
    )
    assert duplicate_attempt["existing"]["id"] == parent_id
    assert duplicate_attempt["existing"]["overlap_pct"] >= 60

    # The SAME overlapping title, force=True: a legitimate child task that
    # happens to share most of its words with its parent.
    child_a = await db_module.add_sprint_item(
        db, pid, "v1", overlapping_title, depends_on=parent_id,
        force=True, prospect_bypass=True,
    )
    assert "error" not in child_a
    assert child_a["depends_on"] == parent_id

    # A second, non-overlapping child -- carries the pointers/citation/gate
    # below.
    child_b = await db_module.add_sprint_item(
        db, pid, "v1",
        "Wire typed research-evidence provenance into the lookup handoff",
        depends_on=parent_id, prospect_bypass=True,
    )
    assert "error" not in child_b
    assert child_b["depends_on"] == parent_id

    # A genuinely deferred, backburner-tracked item -- must be honestly
    # reported as OMITTED by generate_handoff's proposal_scope later, never
    # silently dropped or silently included.
    child_c = await db_module.add_sprint_item(
        db, pid, "v1", "Publish lookup-pipeline retrospective write-up",
        track="backburner", prospect_bypass=True,
    )
    assert "error" not in child_c

    # =======================================================================
    # SECTION 4 -- existing vs. planned_new pointers on child_b.
    # =======================================================================
    existing_file = tmp_path / "citation_pipeline.py"
    existing_file.write_text(
        "def resolve_zotero_key_e2e_marker():\n    return 'resolved'\n",
        encoding="utf-8",
    )
    planned_file = tmp_path / "citation_pipeline_v2.py"  # deliberately NOT created

    existing_pointer = await db_module.add_sprint_item_pointer(
        db, pid, child_b["id"], "code",
        [{
            "uri": str(existing_file),
            "selector": {"type": "range", "start_line": 1, "end_line": 2},
            "target_kind": "existing",
        }],
        label="existing citation-pipeline module",
    )
    assert existing_pointer["targets"][0]["target_kind"] == "existing"

    planned_pointer = await db_module.add_sprint_item_pointer(
        db, pid, child_b["id"], "code",
        [{
            "uri": str(planned_file),
            "selector": {"type": "range", "start_line": 1, "end_line": 1},
            "target_kind": "planned_new",
        }],
        label="planned v2 module (does not exist yet)",
    )
    assert planned_pointer["targets"][0]["target_kind"] == "planned_new"
    assert not planned_file.exists()  # confirms this really was never created

    recorded_pointers = await db_module.get_sprint_item_pointers(db, child_b["id"])
    kinds_recorded = {p["targets"][0]["target_kind"] for p in recorded_pointers}
    assert kinds_recorded == {"existing", "planned_new"}

    # =======================================================================
    # SECTION 5 -- BM25 fallback path: a clean baseline, then a genuinely
    # DEGRADED/INCONCLUSIVE result from a real directory-walk failure -- an
    # empty/short hits list must never be silently read as "no match".
    # =======================================================================
    baseline = bi.bm25_fallback_search(str(tmp_path), "resolve_zotero_key_e2e_marker")
    assert not baseline.get("error")
    assert baseline["inconclusive"] is False
    assert baseline["degraded"] is False
    assert baseline["total_indexed"] >= 1
    assert any(
        "resolve_zotero_key_e2e_marker" in (h.get("snippet") or h.get("content") or "")
        or "citation_pipeline" in (h.get("path") or "")
        for h in baseline["hits"]
    )

    def _fake_walk_with_permission_error(root, onerror=None, **kwargs):
        if onerror is not None:
            onerror(OSError(13, "Permission denied", str(tmp_path / "locked-subdir")))
        return
        yield  # pragma: no cover -- makes this a generator function, never reached

    monkeypatch.setattr(bi.os, "walk", _fake_walk_with_permission_error)
    degraded_bm25 = bi.bm25_fallback_search(str(tmp_path), "resolve_zotero_key_e2e_marker")
    monkeypatch.undo()
    assert degraded_bm25["inconclusive"] is True
    assert degraded_bm25["degraded"] is True
    assert degraded_bm25["walk_errors"], "the injected permission error must be captured, not swallowed"
    assert "could not fully walk" in (degraded_bm25.get("error") or "")

    # =======================================================================
    # SECTION 6 -- missing graph/Serena code-intel: a documented DEGRADED
    # capability, never a crash, and NOT enough on its own to flip
    # executable to False. Exercised through a real generate_handoff call
    # (mode='delta') with no graph_searcher supplied -- the natural,
    # no-tunnel-registered default for a fresh project.
    # =======================================================================
    scope_mid: dict = {}
    _mid_path, _mid_content, _mid_amended = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="delta",
        session_id=sid, proposal_scope=scope_mid,
    )
    mid_items_by_id = {i["id"]: i for i in scope_mid["items"]}
    assert parent_id in mid_items_by_id
    assert child_a["id"] in mid_items_by_id
    assert child_b["id"] in mid_items_by_id
    omitted_by_id = {o["id"]: o["reason"] for o in scope_mid["omitted_items"]}
    assert omitted_by_id.get(child_c["id"]) == "backburner", (
        "the deferred/backburner item must be HONESTLY reported as omitted "
        "(with a reason), never silently dropped or silently claimable"
    )
    graph_cap = scope_mid["required_capabilities"].get("graph_search_availability")
    assert graph_cap is not None
    assert graph_cap["status"] == "degraded", (
        "no graph_searcher/tunnel registered -> documented degraded "
        "capability, not a crash and not silently 'verified'"
    )
    assert scope_mid["executable"] is True, (
        "a DEGRADED (not FAILED) capability must not, by itself, make the "
        "scope non-executable"
    )
    assert scope_mid["degraded"] is False
    assert scope_mid["executable_reasons"] == []

    # =======================================================================
    # SECTION 7 -- partial (not-yet-converged) outputs index reported
    # honestly, never silently treated as an authoritative empty result.
    # =======================================================================
    outputs_dir = tmp_path / "outputs"
    outputs_dir.mkdir()
    (outputs_dir / "recall_metrics.csv").write_text(
        "query,recall\nzotero_key_lookup,0.83\n", encoding="utf-8",
    )
    (outputs_dir / "recall_metrics_v2.csv").write_text(
        "query,recall\nzotero_key_lookup,0.91\n", encoding="utf-8",
    )
    partial_result = oi.search_outputs(str(outputs_dir), "recall", max_seconds=-1.0)
    assert partial_result["partial"] is True, (
        "an expired rebuild budget must be reported as partial=True, never "
        "silently presented as a complete pass"
    )
    assert partial_result["degraded"] is True
    assert partial_result["convergence"]["partial_index"] is True
    assert partial_result["convergence"]["pending_count"] >= 1
    assert partial_result.get("zero_hits_warning"), (
        "a zero-hit result while the index is still incomplete must carry an "
        "explicit warning, not be silently indistinguishable from a confirmed miss"
    )

    # A generous budget on the SAME (now cached) index converges cleanly --
    # confirms 'partial' really tracked a real not-yet-converged state, not a
    # permanently broken index.
    converged_result = oi.search_outputs(str(outputs_dir), "recall", max_seconds=30.0)
    assert converged_result["convergence"]["partial_index"] is False
    assert converged_result["convergence"]["inconclusive"] is False
    assert converged_result["degraded"] is False
    assert not converged_result.get("partial")

    # =======================================================================
    # SECTION 8 -- research artifact graph: claim/citation/output nodes, an
    # UNRESOLVED citation edge (surfaced honestly, not dropped), append-only
    # idempotency on retry, and auto-resolution once the citation is ingested.
    # =======================================================================
    claim_key = rg.claim_identity_key("claim-thesis-1")
    citation_key = rg.citation_identity_key(doi="10.9999/unresolved-citation")
    output_key = rg.output_identity_key(path=str(outputs_dir / "recall_metrics_v2.csv"))

    claim_node = await db_module.create_node(
        db, pid, "claim", claim_key,
        title="BM25 fallback improves citation recall under degraded graph search",
        created_by="researcher",
    )
    assert claim_node["status"] == "active"

    cite_edge_1 = await db_module.create_edge(
        db, pid, "cites",
        {"node_type": "claim", "identity_key": claim_key},
        {"node_type": "citation", "identity_key": citation_key},
        created_by="researcher",
    )
    assert cite_edge_1["to_node_id"] is None  # unresolved: citation not ingested yet
    assert cite_edge_1["resolved_at"] is None

    unresolved = await db_module.get_unresolved_edges(db, pid)
    assert any(e["id"] == cite_edge_1["id"] for e in unresolved), (
        "an edge naming a not-yet-ingested citation must surface via "
        "get_unresolved_edges, never be silently dropped"
    )

    # Retry/idempotency: creating the SAME edge again must not double-create.
    cite_edge_1_retry = await db_module.create_edge(
        db, pid, "cites",
        {"node_type": "claim", "identity_key": claim_key},
        {"node_type": "citation", "identity_key": citation_key},
        created_by="researcher",
    )
    assert cite_edge_1_retry["id"] == cite_edge_1["id"]
    edges_for_claim = await db_module.get_edges_for_identity(db, pid, "claim", claim_key)
    assert len(edges_for_claim) == 1, "create_edge retry must not corrupt state / double-create"

    # Ingesting the citation auto-resolves the previously-unresolved edge.
    await db_module.create_node(
        db, pid, "citation", citation_key,
        title="A paper about BM25 fallback recall", created_by="researcher",
    )
    unresolved_after = await db_module.get_unresolved_edges(db, pid)
    assert not any(e["id"] == cite_edge_1["id"] for e in unresolved_after), (
        "ingesting the target identity must auto-resolve the pending edge"
    )

    # A fully-resolved evidence edge: the recall-metrics output SUPPORTS the claim.
    output_node = await db_module.create_node(
        db, pid, "output", output_key, title="recall_metrics_v2.csv", created_by="researcher",
    )
    await db_module.create_edge(
        db, pid, "supports",
        {"node_type": "output", "identity_key": output_key},
        {"node_type": "claim", "identity_key": claim_key},
        created_by="researcher",
    )
    claim_evidence = await db_module.get_claim_evidence(db, pid, claim_key)
    supports_edges = [e for e in claim_evidence if e["edge_kind"] == "supports"]
    assert len(supports_edges) == 1
    assert supports_edges[0]["resolved"] is True
    assert supports_edges[0]["evidence_node"]["id"] == output_node["id"]

    # =======================================================================
    # SECTION 9 -- typed proposal HITL gate (meridian.proposal_gates):
    # raised (fail-safe 'blocked'), a real decision receipt (retry rejected
    # without reopening), reopened, and finally resolved to 'allowed'.
    # =======================================================================
    gate = await db_module.create_proposal_gate(
        db, pid, "contradiction_acceptance",
        "Two evidence sources disagree on whether the BM25 fallback actually "
        "improved recall for this query set -- accept the contradiction or "
        "reject one source before child_b proceeds?",
        [{"sprint_item_id": child_b["id"]}],
        "recall_metrics.csv reports 0.83 vs recall_metrics_v2.csv reports "
        "0.91 for the identical query on the SAME index -- two runs, "
        "irreconcilable without a human call.",
        created_by="researcher",
    )
    assert gate["state"] == "blocked"  # fail-safe default
    assert gate["decided_at"] is None

    pending_items_now = await db_module.get_sprint_items(db, pid, status="pending")
    readiness_before = await handoff_module.build_proposal_gate_readiness_for_handoff(
        db, pid, pending_items=pending_items_now,
    )
    assert readiness_before is not None
    assert readiness_before["open_gate_count"] == 1
    assert gate["id"] in readiness_before["blocking_pending_item_gate_ids"]

    blocking = await db_module.blocking_gates_for_sprint_item(db, pid, child_b["id"])
    assert any(g["id"] == gate["id"] for g in blocking)

    # A human quarantines the lane (limited scope).
    resolved_gate = await db_module.resolve_proposal_gate(
        db, pid, gate["id"], "quarantined",
        "Restrict to the read-only recall metric only until the dataset "
        "versions reconcile.",
        "human-reviewer",
    )
    assert resolved_gate["state"] == "quarantined"
    assert resolved_gate["decided_at"] is not None

    # Retry/idempotency: resolving an ALREADY-decided, unexpired gate again
    # (without reopening first) must be REJECTED -- a real receipt, not
    # silently overwritable.
    with pytest.raises(gates_module.ProposalGateError):
        await db_module.resolve_proposal_gate(
            db, pid, gate["id"], "allowed", "silently overwriting, should fail", "someone-else",
        )
    still_quarantined = await db_module.get_proposal_gate(db, gate["id"], project_id=pid)
    assert still_quarantined["state"] == "quarantined"
    assert still_quarantined["actor"] == "human-reviewer", (
        "the rejected retry must not have corrupted the real decision"
    )

    # Reopen, then genuinely allow -- unblocking child_b.
    await db_module.reopen_proposal_gate(
        db, pid, gate["id"], "human-reviewer", "dataset versions reconciled",
    )
    fully_resolved_gate = await db_module.resolve_proposal_gate(
        db, pid, gate["id"], "allowed",
        "Dataset versions reconciled; both runs now agree at 0.91 recall.",
        "human-reviewer",
    )
    assert fully_resolved_gate["state"] == "allowed"
    assert gates_module.effective_state(fully_resolved_gate) == "allowed"

    blocking_after = await db_module.blocking_gates_for_sprint_item(db, pid, child_b["id"])
    assert not any(g["id"] == gate["id"] for g in blocking_after)
    readiness_after = await handoff_module.build_proposal_gate_readiness_for_handoff(
        db, pid, pending_items=pending_items_now,
    )
    assert readiness_after["open_gate_count"] == 0

    # =======================================================================
    # SECTION 10 -- docx integrity gate: confirm it runs over ZERO real DOCX
    # artifacts for this scenario. Nothing in this test ever creates, opens,
    # or edits a canonical thesis DOCX -- the explicit constraint.
    # =======================================================================
    docx_gate_result = await docx_gate_module.build_docx_integrity_gate(db, pid)
    assert docx_gate_result["candidate_count"] == 0
    assert docx_gate_result["checked_artifacts"] == []
    assert docx_gate_result["executable"] is True
    assert docx_gate_result["executable_reasons"] == []
    for root, _dirs, files in os.walk(str(tmp_path)):
        for fn in files:
            assert not fn.lower().endswith(".docx"), (
                f"a .docx file was created during this test: {os.path.join(root, fn)}"
            )

    # =======================================================================
    # SECTION 11 -- the FINAL generate_handoff call: a caller-supplied typed
    # research-evidence provenance envelope, a genuinely FAILED (not merely
    # degraded) required capability, and a proposal_scope that must
    # correctly report executable=False -- never falsely executable while a
    # capability is broken.
    # =======================================================================
    claim_record = RE.EvidenceRecord(
        identity=RE.EvidenceIdentity(
            id="claim-thesis-1", kind=RE.EvidenceKind.CLAIM,
            locator="claim:claim-thesis-1",
            label="BM25 fallback improves citation recall under degraded graph search",
        ),
        timestamps=RE.EvidenceTimestamps(
            observed_at="2026-08-21T00:00:00+00:00",
            updated_at="2026-08-21T00:05:00+00:00",
        ),
        resolver=RE.ResolverState(
            status=RE.ResolverStatus.AMBIGUOUS, confidence=0.4,
            reason="two evidence sources disagreed before the gate was resolved",
        ),
        partial=True,
        partial_reason="citation DOI resolution only completed after this envelope was built",
    )
    citation_record = RE.EvidenceRecord(
        identity=RE.EvidenceIdentity(
            id="citation-unresolved-1", kind=RE.EvidenceKind.CITATION,
            locator="doi:10.9999/unresolved-citation",
        ),
        timestamps=RE.EvidenceTimestamps(
            observed_at="2026-08-21T00:00:00+00:00",
            updated_at="2026-08-21T00:00:00+00:00",
        ),
        resolver=RE.ResolverState(
            status=RE.ResolverStatus.VERIFIED, confidence=0.9,
            reason="ingested into the research artifact graph in Section 8",
        ),
    )
    cite_link = RE.EvidenceLink(
        id="link-1", relation="cites",
        source_id="claim-thesis-1", target_id="citation-unresolved-1",
        resolver=RE.ResolverState(status=RE.ResolverStatus.VERIFIED, confidence=0.9),
    )
    envelope = RE.build_envelope(
        records=[claim_record, citation_record], links=[cite_link],
        envelope_id="envelope-research-os-e2e",
        generated_at="2026-08-21T00:10:00+00:00",
        partial=True, partial_reason="broader provenance capture still in progress",
    )

    scope_final: dict = {}
    _final_path, final_content, _final_amended = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="full",
        session_id=sid, proposal_scope=scope_final,
        research_evidence_envelope=envelope,
        graph_searcher=_boom_searcher,
    )

    # The provenance envelope was genuinely rendered into the handoff body
    # (a real ProvenanceEnvelope instance delegates to its own to_markdown(),
    # which opens with "# Provenance Envelope `<id>`" -- see
    # _render_research_evidence_block's duck-typed to_markdown() branch).
    assert "Provenance Envelope" in final_content
    assert "envelope-research-os-e2e" in final_content
    assert "AMBIGUOUS" in final_content
    assert "PARTIAL" in final_content

    # A real, broken required capability -- never silently swallowed as a
    # bare 'this outer call did not raise'.
    cp_cap = scope_final["required_capabilities"].get("code_pointer_enrichment")
    assert cp_cap is not None
    assert cp_cap["status"] == "failed"
    assert "graph search backend unreachable" in cp_cap["reason"]

    assert scope_final["executable"] is False, (
        "a genuinely FAILED required capability must flip executable to "
        "False -- never falsely executable while code-pointer enrichment "
        "is broken"
    )
    assert scope_final["degraded"] is True
    assert any(
        r.startswith("required_capability_failed:") for r in scope_final["executable_reasons"]
    )

    # The scope still agrees with the real board state established over the
    # whole scenario: parent + both real children in scope, the backburner
    # item honestly omitted.
    final_items_by_id = {i["id"]: i for i in scope_final["items"]}
    assert parent_id in final_items_by_id
    assert child_a["id"] in final_items_by_id
    assert child_b["id"] in final_items_by_id
    final_omitted_by_id = {o["id"]: o["reason"] for o in scope_final["omitted_items"]}
    assert final_omitted_by_id.get(child_c["id"]) == "backburner"
    assert scope_final["proposal_id"]
    assert scope_final["content_hash"]

    # Final sanity: still no .docx artifact anywhere, even after the fully
    # rendered handoff (this is the whole point of the acceptance criteria's
    # explicit "must not edit a canonical thesis DOCX" constraint).
    for root, _dirs, files in os.walk(str(tmp_path)):
        for fn in files:
            assert not fn.lower().endswith(".docx")
