"""TEST/HARDEN (f9bacd5b, b730 follow-up) — final verification gate for the
"artifact-integrity-b7308039" item_group.

This is a regression-proof, END-TO-END hardening pass over primitives that
already shipped across several merged items in this chain:

* 5fd9d2fd — ``meridian.artifact_classification.classify_artifact_work``, the
  deterministic figure/table-vs-safe-category classifier.
* 3196ba0e — ``meridian.pointers.verify_target_readiness`` /
  ``verify_pointer_readiness``, the fail-closed, completion-time readiness
  check (existing-on-disk vs. planned_new-created-and-provenance-registered).
* 88f82c15 — ``meridian.pointers.evaluate_artifact_pointer_policy``, the
  warn/strict/off policy evaluator over the classifier's verdict, and
  ``artifact_classification``'s insufficiency classifiers (bare docx /
  directory / generic reference / unsupported type / missing entirely).
* 70c10ca3 — ``meridian.pointers.build_artifact_pointer_finding`` /
  ``assemble_artifact_pointer_findings_from_annotated_items``, combining the
  policy verdict with readiness verification into ONE canonical finding, and
  its XML (``handoff._build_artifact_pointer_findings_clause``) / JSON
  (``capability_contract.extract_artifact_pointer_findings``) twins.

Each primitive above already has thorough dedicated unit coverage
(``tests/test_artifact_classification.py``, ``tests/test_pointers.py``) and
existing handoff-level parity/determinism/scope tests
(``tests/test_682005f4_goal_only_handoff.py``, ``tests/test_handoff_inline_pointers.py``,
``tests/test_cov_handoff.py``). This file does not re-litigate those atomic
unit cases; instead it proves the WIRING end-to-end (through the real DB +
``generate_handoff``/``capability_contract`` pipeline) and closes two gaps
that were not covered anywhere else at the time this item was written:

* XML well-formedness of the ``<artifact_pointer_findings>`` /
  ``<sprint_item_pointers>`` clauses when a pointer's own uri/label text
  contains XML metacharacters (``&``, ``<``, ``>``, quotes) — see
  ``test_e2e_special_characters_in_pointer_uri_keep_clauses_well_formed_xml``.
* Requested-vs-emitted SCOPE fidelity specific to artifact-pointer findings
  (a version-scoped handoff must not leak a finding from another version,
  and must not silently drop a finding that belongs to the requested
  version) — see
  ``test_e2e_version_scoped_handoff_neither_leaks_nor_drops_artifact_pointer_findings``.

Fixtures use only temporary paths (pytest's ``tmp_path``) and injected fake
async resolvers/getters — never a live tunnel, never a machine-local
absolute path baked into persisted DB state (the manifest provenance rule in
AGENTS.md; ``tmp_path`` here is an ephemeral value passed directly into a
pure function call for one test's lifetime, never written into a project's
sprint_items/manifest columns).
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from xml.sax.saxutils import unescape as _xml_unescape

import pytest

from meridian import artifact_classification as ac
from meridian import artifact_declaration as ad
from meridian import capability_contract as cc
from meridian import db as db_module
from meridian import handoff as handoff_module
from meridian import pointers as pointers_module
import meridian.server  # noqa: F401 — load the server before handler to avoid its import cycle
from meridian.mcp import handler as mcp_handler


# ---------------------------------------------------------------------------
# small local helpers
# ---------------------------------------------------------------------------


def _clause(content: str, tag: str) -> str:
    """Return the standalone ``<tag>...</tag>`` substring for a plain
    (attribute-free) tag — e.g. ``<artifact_pointer_findings>``/
    ``<sprint_item_pointers>``, which are always rendered bare (see
    ``handoff._build_artifact_pointer_findings_clause`` /
    ``_build_pointer_records_clause``)."""
    start = content.index(f"<{tag}>")
    end = content.index(f"</{tag}>") + len(f"</{tag}>")
    return content[start:end]


def _clause_json(content: str, tag: str):
    """Extract + XML-unescape + JSON-decode one clause's body."""
    clause = _clause(content, tag)
    inner = clause[len(f"<{tag}>"):-len(f"</{tag}>")]
    return json.loads(_xml_unescape(inner))


_TABLE_TITLE = "Regenerate the results table with new benchmark numbers"
_FIGURE_TITLE = "Insert a new ablation chart figure into the results section"


# ===========================================================================
# 1. Declarations — artifact_kind / planned_output / artifact_policy persist
#    through the real DB and are read back exactly via the effective_* access
#    path, not just at the pure-validation level.
# ===========================================================================


@pytest.mark.asyncio
async def test_declared_artifact_kind_round_trips_and_overrides_title_wording(db):
    """A human-declared artifact_kind wins over figure-sounding title text,
    proven through a real DB round trip (create -> fetch -> classify), not
    just a hand-built dict."""
    p = await db_module.create_project(db, "decl-kind-e2e")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", _FIGURE_TITLE, artifact_kind="document_only",
    )
    fresh = await db_module.get_sprint_item(db, item["id"])
    assert ad.effective_artifact_kind(fresh) == "document_only"
    result = ac.classify_artifact_work(fresh)
    assert result["classification"] == "document_only"
    assert result["is_artifact_sensitive"] is False
    assert result["rule"] == "declared_artifact_kind"


@pytest.mark.asyncio
async def test_declared_planned_output_and_policy_round_trip_through_db(db):
    p = await db_module.create_project(db, "decl-planned-output-e2e")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Produce the ablation figure",
        artifact_kind="figure",
        planned_output={
            "source_type": "docs",
            "targets": [{
                "uri": "outputs/figures/ablation.png",
                "selector": {"type": "range", "start_line": 1, "end_line": 1},
                "target_kind": "planned_new",
            }],
            "provenance_required": True,
        },
        artifact_policy={
            "artifact_pointer_check": "strict",
            "require_exact_figure_output_pointer": True,
        },
    )
    fresh = await db_module.get_sprint_item(db, item["id"])
    assert ad.effective_artifact_kind(fresh) == "figure"

    planned = ad.effective_planned_output(fresh)
    assert planned["targets"][0]["uri"] == "outputs/figures/ablation.png"
    assert planned["targets"][0]["target_kind"] == "planned_new"
    assert planned["provenance_required"] is True

    policy = ad.effective_artifact_policy(fresh)
    assert policy["artifact_pointer_check"] == "strict"
    assert policy["require_exact_figure_output_pointer"] is True
    assert policy["require_exact_table_output_pointer"] is False  # untouched default


@pytest.mark.asyncio
async def test_undeclared_item_reads_as_unknown_never_guessed(db):
    p = await db_module.create_project(db, "decl-absent-e2e")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Ordinary item")
    fresh = await db_module.get_sprint_item(db, item["id"])
    assert ad.effective_artifact_kind(fresh) is None
    assert ad.effective_planned_output(fresh) is None
    assert ad.effective_artifact_policy(fresh) == ad.default_artifact_policy()


# ===========================================================================
# 2. Detector exceptions — a genuinely document_only/caption_only/
#    equation_only/embedded_docx_drawing/code_only (fallback-classified, no
#    declared kind) item must NEVER trigger a weak-pointer warning, even with
#    a bare/insufficient pointer attached.
# ===========================================================================


@pytest.mark.parametrize(
    "title,expected_classification",
    [
        ("Renumber figure captions after Figure 4 was deleted", "caption_only"),
        ("Fix the LaTeX in equation 7", "equation_only"),
        ("Resize the embedded DOCX drawing in the cover page", "embedded_docx_drawing"),
        ("Add unit tests to verify the docx table writer", "code_only"),
        ("Rewrite the introduction paragraph for clarity", "paragraph_only"),
        ("This item is document-only, no new artifacts", "document_only"),
    ],
)
def test_non_sensitive_fallback_classification_never_warns_with_bare_pointer(
    title, expected_classification
):
    item = {
        "id": "item-safe",
        "title": title,
        "pointer_records": [{"id": "ptr-safe", "targets": [{"uri": "outputs/report.docx"}]}],
    }
    classification = ac.classify_artifact_work(item)
    assert classification["classification"] == expected_classification
    assert classification["is_artifact_sensitive"] is False

    policy_result = pointers_module.evaluate_artifact_pointer_policy(item)
    assert policy_result["warning_code"] is None
    assert policy_result["ready"] is True


# ===========================================================================
# 3. Weak-pointer warnings — bare DOCX / directory / generic-reference /
#    unsupported-type pointer on a figure/table-sensitive item, each naming
#    the SPECIFIC insufficiency code (never a generic "no pointer" catch-all).
# ===========================================================================


@pytest.mark.parametrize(
    "uri,expected_code",
    [
        ("outputs/report.docx", ac.INSUFFICIENT_BARE_DOCX),
        ("outputs/figures/", ac.INSUFFICIENT_DIRECTORY),
        ("mcp_tool:search_outputs", ac.INSUFFICIENT_GENERIC_REFERENCE),
        ("outputs/figures/notes.txt", ac.INSUFFICIENT_UNSUPPORTED_TYPE),
    ],
)
def test_weak_pointer_warning_names_specific_insufficiency_code(uri, expected_code):
    item = {
        "id": "item-weak",
        "title": _FIGURE_TITLE,
        "pointer_records": [{"id": "ptr-weak", "targets": [{"uri": uri}]}],
    }
    result = pointers_module.evaluate_artifact_pointer_policy(item)
    assert result["warning_code"] == expected_code
    assert result["affected_pointer_ids"] == ["ptr-weak"]
    assert result["required_remediation"]
    assert result["ready"] is True  # default policy is "warn" — surfaced, never blocking


def test_missing_pointer_entirely_uses_missing_pointer_code():
    item = {"id": "item-missing", "title": _FIGURE_TITLE}
    result = pointers_module.evaluate_artifact_pointer_policy(item)
    assert result["warning_code"] == ac.INSUFFICIENT_MISSING_POINTER
    assert result["affected_pointer_ids"] == []


# ===========================================================================
# 4. Strict readiness blocking — strict policy + an active insufficiency
#    finding -> ready=False; warn keeps the SAME finding non-blocking; off
#    suppresses the warning entirely while preserving the raw classification.
# ===========================================================================


def test_strict_policy_with_insufficient_pointer_is_not_ready():
    item = {
        "id": "item-strict",
        "title": _TABLE_TITLE,
        "artifact_policy": {"artifact_pointer_check": "strict"},
        "pointer_records": [{"id": "ptr-strict", "targets": [{"uri": "outputs/report.docx"}]}],
    }
    result = pointers_module.evaluate_artifact_pointer_policy(item)
    assert result["warning_code"] == ac.INSUFFICIENT_BARE_DOCX
    assert result["ready"] is False


def test_warn_policy_with_same_finding_stays_ready():
    item = {
        "id": "item-warn",
        "title": _TABLE_TITLE,
        "artifact_policy": {"artifact_pointer_check": "warn"},
        "pointer_records": [{"id": "ptr-warn", "targets": [{"uri": "outputs/report.docx"}]}],
    }
    result = pointers_module.evaluate_artifact_pointer_policy(item)
    assert result["warning_code"] == ac.INSUFFICIENT_BARE_DOCX
    assert result["ready"] is True


def test_off_policy_suppresses_warning_but_preserves_classification_and_policy():
    item = {
        "id": "item-off",
        "title": _TABLE_TITLE,
        "artifact_policy": {"artifact_pointer_check": "off"},
        "pointer_records": [{"id": "ptr-off", "targets": [{"uri": "outputs/report.docx"}]}],
    }
    result = pointers_module.evaluate_artifact_pointer_policy(item)
    assert result["warning_code"] is None
    assert result["ready"] is True
    assert result["classification"]["is_artifact_sensitive"] is True
    assert result["policy"]["artifact_pointer_check"] == "off"


# ===========================================================================
# 5. Planned-output existence/provenance — planned_new must clear BOTH bars:
#    the file genuinely exists AND a provenance record is on file. Missing
#    either fails closed with an explicit, distinct status.
# ===========================================================================


@pytest.mark.asyncio
async def test_planned_new_not_created_yet_fails_closed(tmp_path):
    target = {"uri": str(tmp_path / "outputs" / "fig.png"), "target_kind": "planned_new"}
    result = await pointers_module.verify_target_readiness(target)
    assert result["ready"] is False
    assert result["status"] == "not_created"


@pytest.mark.asyncio
async def test_planned_new_created_but_no_provenance_getter_wired_fails_closed(tmp_path):
    p = tmp_path / "fig.png"
    p.write_bytes(b"fake png bytes")
    target = {"uri": str(p), "target_kind": "planned_new"}
    result = await pointers_module.verify_target_readiness(target)  # provenance_getter=None
    assert result["ready"] is False
    assert result["status"] == "provenance_unavailable"


@pytest.mark.asyncio
async def test_planned_new_created_but_provenance_record_missing_fails_closed(tmp_path):
    p = tmp_path / "fig.png"
    p.write_bytes(b"fake png bytes")
    target = {"uri": str(p), "target_kind": "planned_new"}

    async def _no_record(_outputs_dir, _uri):
        return None

    result = await pointers_module.verify_target_readiness(
        target, provenance_getter=_no_record,
    )
    assert result["ready"] is False
    assert result["status"] == "provenance_missing"


@pytest.mark.asyncio
async def test_planned_new_provenance_getter_raising_fails_closed_not_silently_ready(tmp_path):
    p = tmp_path / "fig.png"
    p.write_bytes(b"fake png bytes")
    target = {"uri": str(p), "target_kind": "planned_new"}

    async def _boom(_outputs_dir, _uri):
        raise RuntimeError("provenance ledger unreachable")

    result = await pointers_module.verify_target_readiness(target, provenance_getter=_boom)
    assert result["ready"] is False
    assert result["status"] == "provenance_check_failed"


@pytest.mark.asyncio
async def test_planned_new_created_and_provenance_on_file_is_ready(tmp_path):
    p = tmp_path / "fig.png"
    p.write_bytes(b"fake png bytes")
    target = {"uri": str(p), "target_kind": "planned_new"}

    async def _fake_provenance(_outputs_dir, _uri):
        return {"script": "make_fig.py", "content_hash": "abc123"}

    result = await pointers_module.verify_target_readiness(
        target, provenance_getter=_fake_provenance,
    )
    assert result["ready"] is True
    assert result["status"] == "ready"
    assert result["provenance"]["script"] == "make_fig.py"


# ===========================================================================
# 6. Canonical/archival distinction + tunnel/tool-unavailable degradation —
#    an "existing" target's hard gate is on-disk presence; meridian-outputs
#    enrichment beyond that is recorded evidence, never a second gate, and an
#    unreachable/raising resolver degrades EXPLICITLY, never silently to
#    "canonical"/"ready".
# ===========================================================================


@pytest.mark.asyncio
async def test_existing_target_canonical_vs_archival_distinction(tmp_path):
    p = tmp_path / "fig.png"
    p.write_bytes(b"x")
    target = {"uri": str(p), "target_kind": "existing"}

    async def _canonical_resolver(_outputs_dir, _uri):
        return {"is_archival": False, "match_type": "exact"}

    async def _archival_resolver(_outputs_dir, _uri):
        return {"is_archival": True, "match_type": "exact"}

    canonical = await pointers_module.verify_target_readiness(
        target, figure_resolver=_canonical_resolver,
    )
    archival = await pointers_module.verify_target_readiness(
        target, figure_resolver=_archival_resolver,
    )
    assert canonical["status"] == "canonical" and canonical["ready"] is True
    # Archival is recorded evidence, NOT a second gate — still ready.
    assert archival["status"] == "archival" and archival["ready"] is True


@pytest.mark.asyncio
async def test_existing_target_no_figure_resolver_is_explicit_unresolved_not_silently_canonical(
    tmp_path,
):
    p = tmp_path / "fig.png"
    p.write_bytes(b"x")
    target = {"uri": str(p), "target_kind": "existing"}
    result = await pointers_module.verify_target_readiness(target)  # figure_resolver=None
    assert result["ready"] is True  # disk presence alone is the hard gate
    assert result["status"] == "unresolved"  # but never mislabeled "canonical"


@pytest.mark.asyncio
async def test_existing_target_figure_resolver_raising_degrades_explicitly(tmp_path):
    p = tmp_path / "fig.png"
    p.write_bytes(b"x")
    target = {"uri": str(p), "target_kind": "existing"}

    async def _boom(_outputs_dir, _uri):
        raise RuntimeError("meridian-outputs tunnel down")

    result = await pointers_module.verify_target_readiness(target, figure_resolver=_boom)
    assert result["ready"] is True
    assert result["status"] == "degraded"  # never silently "canonical"


@pytest.mark.asyncio
async def test_planned_new_provenance_getter_none_is_explicit_not_ready_never_faked(tmp_path):
    """Belt-and-suspenders companion to section 5's dedicated test: an
    unavailable provenance checker for a genuinely-created planned_new file
    must degrade to an explicit, named unavailable status — never silently
    ready just because the file itself exists."""
    p = tmp_path / "table.csv"
    p.write_bytes(b"a,b,c\n")
    target = {"uri": str(p), "target_kind": "planned_new"}
    result = await pointers_module.verify_target_readiness(target)
    assert result["ready"] is False
    assert result["status"] == "provenance_unavailable"


# ===========================================================================
# 7. End-to-end: a planned_new pointer flowing through the REAL
#    _annotate_resolved_pointers/generate_handoff wiring (not just the
#    isolated pointers.py unit call) must show the fail-closed readiness
#    verdict explicitly — this is the actual production wiring the sprint
#    item's spec calls "tunnel/tool-unavailable degradation" for.
# ===========================================================================


@pytest.mark.asyncio
async def test_e2e_planned_new_weak_pointer_shows_not_created_through_real_pipeline(db):
    p = await db_module.create_project(db, "e2e-planned-new-weak")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", _TABLE_TITLE,
        artifact_policy={"artifact_pointer_check": "warn"},
    )
    stored = await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "docs",
        [{
            "uri": "outputs/report.docx",
            "selector": {"type": "range", "start_line": 1, "end_line": 1},
            "target_kind": "planned_new",
        }],
    )
    items = [{"id": item["id"], "title": item["title"], "artifact_policy": item["artifact_policy"]}]
    out = await handoff_module._annotate_resolved_pointers(db, p["id"], items)
    finding = out[0]["artifact_pointer_finding"]
    assert finding["warning_code"] == "insufficient_pointer_bare_docx"
    readiness = finding["target_readiness"][0]
    assert readiness["pointer_id"] == str(stored["id"])
    assert readiness["ready"] is False
    target_verdict = readiness["targets"][0]
    assert target_verdict["target_kind"] == "planned_new"
    # A relative "outputs/report.docx" genuinely does not exist from the test
    # cwd — the fail-closed check ran for real, not a stub.
    assert target_verdict["status"] == "not_created"


# ===========================================================================
# 8. XML well-formedness — the <artifact_pointer_findings> AND
#    <sprint_item_pointers> clauses must parse as valid, standalone XML even
#    when a pointer's own uri/label carries raw XML metacharacters
#    (&, <, >, a literal quote). This was NOT covered anywhere else: every
#    existing XML-escaping test exercised item titles/notes, never a
#    pointer's own uri/label text specifically.
# ===========================================================================


@pytest.mark.asyncio
async def test_e2e_special_characters_in_pointer_uri_keep_clauses_well_formed_xml(db, tmp_path):
    p = await db_module.create_project(db, "e2e-xml-special-chars")
    item = await db_module.add_sprint_item(db, p["id"], "v1", _TABLE_TITLE)
    tricky_uri = 'outputs/tables/results & "final" <v2>.docx'
    stored = await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "docs",
        [{"uri": tricky_uri, "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
        label="report <draft> & final",
    )

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
    )

    # The raw metacharacters must never appear unescaped inside the clause.
    findings_clause = _clause(content, "artifact_pointer_findings")
    pointers_clause = _clause(content, "sprint_item_pointers")
    assert "<v2>.docx" not in findings_clause
    assert "<v2>.docx" not in pointers_clause
    assert "<draft>" not in pointers_clause

    # Each clause parses standalone as well-formed XML — this is the actual
    # regression check (ET.fromstring raises ParseError on malformed input).
    findings_root = ET.fromstring(findings_clause)
    pointers_root = ET.fromstring(pointers_clause)
    assert findings_root.tag == "artifact_pointer_findings"
    assert pointers_root.tag == "sprint_item_pointers"

    # And the original text survives the escape/unescape round trip exactly.
    findings = _clause_json(content, "artifact_pointer_findings")
    assert findings[0]["item_id"] == item["id"]
    assert findings[0]["warning_code"] == "insufficient_pointer_bare_docx"

    pointer_entries = _clause_json(content, "sprint_item_pointers")
    entry = next(e for e in pointer_entries if e["item_id"] == item["id"])
    record = entry["pointers"][0]
    assert record["targets"][0]["uri"] == tricky_uri
    assert record["label"] == "report <draft> & final"
    assert record["id"] == str(stored["id"])


# ===========================================================================
# 9. Machine-readable schema — the finding's shape is pinned (a stable,
#    known key set) and byte-identical across repeated builds of the SAME
#    underlying state.
# ===========================================================================


_EXPECTED_FINDING_KEYS = {
    "item_id", "classification", "policy", "warning_code",
    "required_remediation", "affected_pointer_ids", "ready",
    "pointer_status", "target_readiness",
}


@pytest.mark.asyncio
async def test_artifact_pointer_finding_schema_pinned_and_stable_across_calls(db):
    p = await db_module.create_project(db, "schema-pin")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", _TABLE_TITLE,
        artifact_policy={"artifact_pointer_check": "warn"},
    )
    await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "docs",
        [{"uri": "outputs/report.docx", "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    )
    contract_a = await cc.build_capability_contract(db, p["id"])
    contract_b = await cc.build_capability_contract(db, p["id"])
    findings_a = contract_a["item_artifact_pointer_findings"]
    findings_b = contract_b["item_artifact_pointer_findings"]

    assert len(findings_a) == 1
    assert set(findings_a[0].keys()) == _EXPECTED_FINDING_KEYS
    # Stable shape AND stable values across two independent builds of the
    # SAME underlying DB state.
    assert findings_a == findings_b


@pytest.mark.asyncio
async def test_artifact_pointer_finding_schema_stable_pre_annotated_vs_self_fetch(db):
    """The same schema/values whether the caller pre-annotated the items
    (handoff._annotate_resolved_pointers) or lets extract_artifact_pointer_findings
    self-fetch — the two code paths inside capability_contract.py must never
    silently diverge in shape."""
    p = await db_module.create_project(db, "schema-pin-two-paths")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", _TABLE_TITLE,
        artifact_policy={"artifact_pointer_check": "warn"},
    )
    await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "docs",
        [{"uri": "outputs/report.docx", "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    )

    raw_items = [dict(item)]
    self_fetched = await cc.extract_artifact_pointer_findings(db, p["id"], raw_items)

    annotated_items = [dict(item)]
    await handoff_module._annotate_resolved_pointers(db, p["id"], annotated_items)
    pre_annotated = await cc.extract_artifact_pointer_findings(db, p["id"], annotated_items)

    assert self_fetched == pre_annotated
    assert set(self_fetched[0].keys()) == _EXPECTED_FINDING_KEYS


# ===========================================================================
# 10. Deterministic ordering — item id then pointer id, and byte-identical
#     across repeated calls (not merely "sorted", but sorted against an
#     independently-known-correct order, so a regression that drops the sort
#     can't accidentally pass by coincidence of insertion order).
# ===========================================================================


@pytest.mark.asyncio
async def test_e2e_deterministic_ordering_item_id_then_pointer_id(db):
    p = await db_module.create_project(db, "e2e-ordering")
    # Inserted in an order that does not already match sorted-by-id, so the
    # assertions below are a real proof the code sorts rather than merely
    # preserving insertion order.
    item_b = await db_module.add_sprint_item(db, p["id"], "v1", _FIGURE_TITLE)
    item_a = await db_module.add_sprint_item(db, p["id"], "v1", _TABLE_TITLE)

    # item_a carries TWO insufficient pointers failing for the SAME reason
    # (bare docx), so both land in the SAME finding's target_readiness list —
    # proving pointer-level sort, not just item-level sort.
    ptr_1 = await db_module.add_sprint_item_pointer(
        db, p["id"], item_a["id"], "docs",
        [{"uri": "outputs/report_a.docx", "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    )
    ptr_2 = await db_module.add_sprint_item_pointer(
        db, p["id"], item_a["id"], "docs",
        [{"uri": "outputs/report_b.docx", "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    )
    await db_module.add_sprint_item_pointer(
        db, p["id"], item_b["id"], "docs",
        [{"uri": "mcp_tool:search_outputs", "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    )

    contract_a = await cc.build_capability_contract(db, p["id"])
    contract_b = await cc.build_capability_contract(db, p["id"])
    findings_a = contract_a["item_artifact_pointer_findings"]
    findings_b = contract_b["item_artifact_pointer_findings"]

    assert findings_a == findings_b  # byte-identical across repeated builds

    item_ids = [f["item_id"] for f in findings_a]
    assert item_ids == sorted([item_a["id"], item_b["id"]])

    entry_a = next(f for f in findings_a if f["item_id"] == item_a["id"])
    pointer_ids = [t["pointer_id"] for t in entry_a["target_readiness"]]
    assert pointer_ids == sorted([str(ptr_1["id"]), str(ptr_2["id"])])


# ===========================================================================
# 11. Requested-vs-emitted scope — a version-scoped handoff must not leak a
#     finding from a DIFFERENT version, and must not silently drop the
#     finding that genuinely belongs to the requested version. Proven in
#     BOTH directions (request v1, then request v2) through the real MCP
#     generate_handoff dispatch so both the XML clause and the
#     capability_contract JSON are checked for the SAME request.
# ===========================================================================


@pytest.mark.asyncio
async def test_e2e_version_scoped_handoff_neither_leaks_nor_drops_artifact_pointer_findings(
    db, tmp_path
):
    p = await db_module.create_project(db, "e2e-version-scope-findings")
    item_v1 = await db_module.add_sprint_item(db, p["id"], "v1", _TABLE_TITLE)
    await db_module.add_sprint_item_pointer(
        db, p["id"], item_v1["id"], "docs",
        [{"uri": "outputs/report.docx", "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    )
    item_v2 = await db_module.add_sprint_item(db, p["id"], "v2", _FIGURE_TITLE)
    await db_module.add_sprint_item_pointer(
        db, p["id"], item_v2["id"], "docs",
        [{"uri": "outputs/figures/", "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    )

    result_v1 = await mcp_handler._handle_task_tools(
        "generate_handoff", {"project_id": p["id"], "mode": "goal", "version": "v1"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    ids_v1 = {
        f["item_id"] for f in result_v1["capability_contract"]["item_artifact_pointer_findings"]
    }
    assert item_v1["id"] in ids_v1  # requested-version finding present — not dropped
    assert item_v2["id"] not in ids_v1  # other-version finding absent — not leaked
    xml_ids_v1 = {f["item_id"] for f in _clause_json(result_v1["content"], "artifact_pointer_findings")}
    assert xml_ids_v1 == ids_v1  # XML clause and JSON contract agree for this request

    result_v2 = await mcp_handler._handle_task_tools(
        "generate_handoff", {"project_id": p["id"], "mode": "goal", "version": "v2"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    ids_v2 = {
        f["item_id"] for f in result_v2["capability_contract"]["item_artifact_pointer_findings"]
    }
    assert item_v2["id"] in ids_v2  # requested-version finding present — not dropped
    assert item_v1["id"] not in ids_v2  # other-version finding absent — not leaked
    xml_ids_v2 = {f["item_id"] for f in _clause_json(result_v2["content"], "artifact_pointer_findings")}
    assert xml_ids_v2 == ids_v2
