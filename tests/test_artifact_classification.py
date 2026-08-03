"""Tests for sprint item 5fd9d2fd (b7308039 / 665 follow-up) — deterministic
figure/table-vs-safe-category classifier.

Covers:

1. meridian.artifact_classification.classify_artifact_work — declared
   artifact_kind takes priority (including the "explicit document_only
   override" case where title wording suggests figure work but the
   declared kind says otherwise), then the conservative title/notes/
   pointer-evidence fallback for legacy items: positive (clear figure/table
   creation, both from title wording and from a concrete pointer), negative
   (caption-only/equation-only/embedded-drawing/code-only/paragraph-only —
   none of which are artifact-sensitive), and ambiguous (indirect/weak
   figure wording, mixed pointer evidence, and "nothing recognizable at
   all").
2. Pointer-evidence guardrails — a bare .docx path, a directory-looking
   uri, and a generic scheme-prefixed resource id (mcp_tool:/db:/…) must
   NEVER count as "exact" figure/table evidence.
3. meridian.artifact_classification.summarize_artifact_classifications —
   the batch aggregate used by handoff readiness.
4. meridian.handoff — the <artifact_work_classification> clause in
   build_item_briefing, and the readiness-block artifact warning line.
"""
from __future__ import annotations

import json as _json

import pytest

from meridian import artifact_classification as ac
from meridian import handoff as handoff_module


# ---------------------------------------------------------------------------
# 1a. Declared artifact_kind wins — authoritative, no fallback heuristics run.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["figure", "table"])
def test_declared_figure_table_kind_is_artifact_sensitive(kind):
    item = {"id": "i1", "title": "Produce the ablation output", "artifact_kind": kind}
    result = ac.classify_artifact_work(item)
    assert result["classification"] == kind
    assert result["is_artifact_sensitive"] is True
    assert result["confidence"] == "high"
    assert result["ambiguous"] is False
    assert result["rule"] == "declared_artifact_kind"


def test_declared_document_only_kind_is_not_artifact_sensitive():
    item = {"id": "i2", "title": "Tidy up the intro", "artifact_kind": "document_only"}
    result = ac.classify_artifact_work(item)
    assert result["classification"] == "document_only"
    assert result["is_artifact_sensitive"] is False
    assert result["rule"] == "declared_artifact_kind"


def test_explicit_document_only_override_beats_figure_sounding_title():
    """The required 'explicit document_only override' scenario: title text
    reads like a figure-creation task, but a human explicitly declared
    artifact_kind='document_only' on the item — that declaration must win
    outright, with none of the fallback heuristics ever running."""
    item = {
        "id": "i3",
        "title": "Insert a brand-new ablation results chart into section 4",
        "notes": "Replace the placeholder figure with the real regenerated chart.",
        "artifact_kind": "document_only",
    }
    result = ac.classify_artifact_work(item)
    assert result["classification"] == "document_only"
    assert result["is_artifact_sensitive"] is False
    assert result["confidence"] == "high"
    assert result["rule"] == "declared_artifact_kind"


def test_declared_kind_case_and_whitespace_tolerant():
    item = {"id": "i4", "title": "x", "artifact_kind": "  Figure  "}
    result = ac.classify_artifact_work(item)
    assert result["classification"] == "figure"


# ---------------------------------------------------------------------------
# 1b. Fallback — POSITIVE: clear figure/table creation, no declared kind.
# ---------------------------------------------------------------------------

def test_fallback_strong_figure_creation_verb_is_sensitive():
    item = {"id": "i5", "title": "Insert a new ablation chart figure into the results section"}
    result = ac.classify_artifact_work(item)
    assert result["classification"] == "figure"
    assert result["is_artifact_sensitive"] is True
    assert result["confidence"] == "high"
    assert result["ambiguous"] is False
    assert result["rule"] == "title_notes_strong_figure"
    assert result["evidence"]


def test_fallback_strong_table_creation_verb_is_sensitive():
    item = {"id": "i6", "title": "Regenerate the results table with new benchmark numbers"}
    result = ac.classify_artifact_work(item)
    assert result["classification"] == "table"
    assert result["is_artifact_sensitive"] is True
    assert result["rule"] == "title_notes_strong_table"


def test_fallback_table_of_contents_is_not_table_evidence():
    item = {"id": "i7", "title": "Fix the table of contents pagination after the merge"}
    result = ac.classify_artifact_work(item)
    assert result["classification"] != "table"


def test_fallback_concrete_pointer_to_figure_file_is_sensitive():
    item = {
        "id": "i8",
        "title": "Update the deliverable",
        "touches_resources": _json.dumps(["file:outputs/figures/ablation.png"]),
    }
    result = ac.classify_artifact_work(item)
    assert result["classification"] == "figure"
    assert result["is_artifact_sensitive"] is True
    assert result["rule"] == "pointer_evidence_figure"
    assert "ablation.png" in result["evidence"][0]


def test_fallback_concrete_pointer_to_table_file_via_planned_output():
    item = {
        "id": "i9",
        "title": "Update the deliverable",
        "planned_output": _json.dumps({
            "source_type": "code",
            "targets": [{
                "uri": "outputs/tables/results.csv",
                "selector": {"type": "range", "start_line": 1, "end_line": 1},
                "target_kind": "planned_new",
            }],
        }),
    }
    result = ac.classify_artifact_work(item)
    assert result["classification"] == "table"
    assert result["is_artifact_sensitive"] is True
    assert result["rule"] == "pointer_evidence_table"


def test_fallback_pointer_records_figure_evidence():
    item = {
        "id": "i10",
        "title": "Update the deliverable",
        "pointer_records": [{
            "source_type": "code",
            "targets": [{"uri": "outputs/figures/roc_curve.svg"}],
        }],
    }
    result = ac.classify_artifact_work(item)
    assert result["classification"] == "figure"
    assert result["is_artifact_sensitive"] is True


# ---------------------------------------------------------------------------
# 1c. Fallback — NEGATIVE: safe categories, never artifact-sensitive.
# ---------------------------------------------------------------------------

def test_fallback_caption_only_not_sensitive():
    item = {"id": "i11", "title": "Renumber figure captions after Figure 4 was deleted"}
    result = ac.classify_artifact_work(item)
    assert result["classification"] == "caption_only"
    assert result["is_artifact_sensitive"] is False
    assert result["rule"] == "title_notes_caption_only"


def test_fallback_equation_only_not_sensitive():
    item = {"id": "i12", "title": "Fix the LaTeX in equation 7", "notes": "Sign error in the denominator."}
    result = ac.classify_artifact_work(item)
    assert result["classification"] == "equation_only"
    assert result["is_artifact_sensitive"] is False
    assert result["rule"] == "title_notes_equation_only"


def test_fallback_embedded_docx_drawing_not_sensitive():
    item = {"id": "i13", "title": "Resize the embedded DOCX drawing in the cover page"}
    result = ac.classify_artifact_work(item)
    assert result["classification"] == "embedded_docx_drawing"
    assert result["is_artifact_sensitive"] is False
    assert result["rule"] == "title_notes_embedded_docx_drawing"


def test_fallback_code_only_not_sensitive():
    item = {"id": "i14", "title": "Add unit tests to verify the docx table writer"}
    result = ac.classify_artifact_work(item)
    assert result["classification"] == "code_only"
    assert result["is_artifact_sensitive"] is False
    assert result["rule"] == "title_notes_code_only"


def test_fallback_code_only_not_confused_by_regression_test_phrase():
    item = {"id": "i15", "title": "Add a regression test for the figure caption renumbering bug"}
    result = ac.classify_artifact_work(item)
    # "regression test" + "caption" both present; code_only vocabulary and
    # caption vocabulary both fire but caption is checked first (branch 2).
    assert result["classification"] in {"caption_only", "code_only"}
    assert result["is_artifact_sensitive"] is False


def test_fallback_paragraph_only_not_sensitive():
    item = {"id": "i16", "title": "Rewrite the introduction paragraph for clarity"}
    result = ac.classify_artifact_work(item)
    assert result["classification"] == "paragraph_only"
    assert result["is_artifact_sensitive"] is False
    assert result["rule"] == "title_notes_paragraph_only"


# ---------------------------------------------------------------------------
# 1d. Fallback — AMBIGUOUS: indirect wording, mixed evidence, no evidence.
# ---------------------------------------------------------------------------

def test_fallback_indirect_figure_wording_still_sensitive_but_flagged():
    """'a figure/table item must be treated as artifact-sensitive even when
    wording is indirect' — a bare noun mention with no creation verb still
    classifies as sensitive, but at medium confidence and ambiguous=True."""
    item = {"id": "i17", "title": "Review the figure placement in section 3"}
    result = ac.classify_artifact_work(item)
    assert result["classification"] == "figure"
    assert result["is_artifact_sensitive"] is True
    assert result["confidence"] == "medium"
    assert result["ambiguous"] is True
    assert result["rule"] == "title_notes_weak_figure"


def test_fallback_indirect_table_wording_still_sensitive_but_flagged():
    item = {"id": "i18", "title": "Double-check the benchmark table before merging"}
    result = ac.classify_artifact_work(item)
    assert result["classification"] == "table"
    assert result["is_artifact_sensitive"] is True
    assert result["ambiguous"] is True


def test_fallback_mixed_pointer_evidence_is_ambiguous_but_sensitive():
    item = {
        "id": "i19",
        "title": "Update the deliverable",
        "touches_resources": _json.dumps([
            "file:outputs/figures/ablation.png",
            "file:outputs/tables/results.csv",
        ]),
    }
    result = ac.classify_artifact_work(item)
    assert result["is_artifact_sensitive"] is True
    assert result["ambiguous"] is True
    assert result["rule"] == "pointer_evidence_mixed"


def test_fallback_no_signal_at_all_is_ambiguous_and_not_sensitive():
    item = {"id": "i20", "title": "Fix the OAuth redirect bug"}
    result = ac.classify_artifact_work(item)
    assert result["classification"] == "ambiguous"
    assert result["is_artifact_sensitive"] is False
    assert result["confidence"] == "low"
    assert result["ambiguous"] is True
    assert result["rule"] == "no_signal_ambiguous"


def test_classify_artifact_work_never_raises_on_malformed_item():
    assert ac.classify_artifact_work(None)["classification"] == "ambiguous"  # type: ignore[arg-type]
    assert ac.classify_artifact_work({})["classification"] == "ambiguous"


# ---------------------------------------------------------------------------
# 2. Pointer-evidence guardrails — never trust a bare docx / directory /
#    generic scheme-prefixed resource id as exact figure/table evidence.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("uri", [
    "outputs/report.docx",
    "outputs/figures/",
    "outputs/figures",
    "mcp_tool:search_outputs",
    "db:migrations",
    "route:GET:/api/report",
])
def test_pointer_uri_never_counts_as_exact_figure_table_evidence(uri):
    assert ac._classify_uri(uri) is None


@pytest.mark.parametrize("uri,expected", [
    ("outputs/figures/ablation.png", "figure"),
    ("outputs/figures/roc.svg", "figure"),
    ("outputs/tables/results.csv", "table"),
    ("outputs/tables/results.xlsx", "table"),
])
def test_pointer_uri_with_concrete_extension_counts_as_evidence(uri, expected):
    assert ac._classify_uri(uri) == expected


def test_pointer_evidence_ignored_when_bare_docx_directory_or_tool_id():
    item = {
        "id": "i21",
        "title": "Update the deliverable",
        "touches_resources": _json.dumps([
            "file:outputs/report.docx",
            "file:outputs/figures/",
            "mcp_tool:search_outputs",
        ]),
    }
    result = ac.classify_artifact_work(item)
    assert result["classification"] == "ambiguous"
    assert result["is_artifact_sensitive"] is False


# ---------------------------------------------------------------------------
# 3. summarize_artifact_classifications — batch aggregate.
# ---------------------------------------------------------------------------

def test_summarize_counts_and_flags_sensitive_items_without_pointer_evidence():
    items = [
        {"id": "s1", "title": "Insert a new ablation chart figure"},  # sensitive, no pointer
        {"id": "s2", "title": "Renumber figure captions"},  # caption_only
        {
            "id": "s3",
            "title": "Regenerate the results table",
            "touches_resources": _json.dumps(["file:outputs/tables/results.csv"]),
        },  # sensitive, HAS pointer evidence
    ]
    summary = ac.summarize_artifact_classifications(items)
    assert summary["counts"]["figure"] == 1
    assert summary["counts"]["caption_only"] == 1
    assert summary["counts"]["table"] == 1
    assert summary["sensitive_without_pointer"] == ["s1"]


def test_summarize_respects_off_policy():
    items = [{
        "id": "s4",
        "title": "Insert a new ablation chart figure",
        "artifact_policy": _json.dumps({"artifact_pointer_check": "off"}),
    }]
    summary = ac.summarize_artifact_classifications(items)
    assert summary["sensitive_without_pointer"] == []


def test_summarize_handles_none_and_empty():
    assert ac.summarize_artifact_classifications(None) == {
        "counts": {}, "sensitive_without_pointer": [], "ambiguous_items": [],
    }
    assert ac.summarize_artifact_classifications([])["counts"] == {}


def test_summarize_tracks_ambiguous_items():
    items = [{"id": "s5", "title": "Review the figure placement"}]
    summary = ac.summarize_artifact_classifications(items)
    assert summary["ambiguous_items"] == ["s5"]


# ---------------------------------------------------------------------------
# 4a. handoff.build_item_briefing — <artifact_work_classification> clause.
# ---------------------------------------------------------------------------

def _extract_clause(briefing: str, tag: str) -> "dict | None":
    open_tag, close_tag = f"<{tag}>", f"</{tag}>"
    if open_tag not in briefing:
        return None
    start = briefing.index(open_tag) + len(open_tag)
    end = briefing.index(close_tag)
    return _json.loads(briefing[start:end])


def test_build_item_briefing_renders_classification_for_declared_kind():
    item = {"id": "item-uuid", "title": "Produce the ablation figure", "artifact_kind": "figure"}
    briefing = handoff_module.build_item_briefing(item)
    embedded = _extract_clause(briefing, "artifact_work_classification")
    assert embedded is not None
    assert embedded["classification"] == "figure"
    assert embedded["is_artifact_sensitive"] is True
    assert embedded["rule"] == "declared_artifact_kind"


def test_build_item_briefing_renders_classification_for_fallback_evidence():
    item = {"id": "item-uuid", "title": "Renumber figure captions after Figure 4 was deleted"}
    briefing = handoff_module.build_item_briefing(item)
    embedded = _extract_clause(briefing, "artifact_work_classification")
    assert embedded is not None
    assert embedded["classification"] == "caption_only"
    assert embedded["is_artifact_sensitive"] is False


def test_build_item_briefing_omits_classification_when_no_signal_at_all():
    item = {"id": "item-uuid", "title": "Fix the OAuth redirect bug"}
    briefing = handoff_module.build_item_briefing(item)
    assert "<artifact_work_classification>" not in briefing


# ---------------------------------------------------------------------------
# 4b. handoff readiness-block artifact warning.
# ---------------------------------------------------------------------------

def test_build_artifact_readiness_warnings_flags_missing_pointer_evidence():
    items = [{"id": "r1", "title": "Insert a new ablation chart figure into the results"}]
    warnings = handoff_module._build_artifact_readiness_warnings(items)
    assert len(warnings) == 1
    assert "r1" in warnings[0]
    assert "figure/table" in warnings[0]


def test_build_artifact_readiness_warnings_empty_when_nothing_missing():
    items = [{"id": "r2", "title": "Renumber figure captions"}]
    assert handoff_module._build_artifact_readiness_warnings(items) == []


def test_build_artifact_readiness_warnings_never_raises_on_bad_input():
    assert handoff_module._build_artifact_readiness_warnings(None) == []
    assert handoff_module._build_artifact_readiness_warnings("not-a-list") == []  # type: ignore[arg-type]


def test_build_readiness_block_includes_artifact_warnings():
    block = handoff_module._build_readiness_block(
        "week-1", 2, 1, artifact_warnings=["⚠ 1 pending item looks like figure/table work: r1"],
    )
    assert "⚠ 1 pending item looks like figure/table work: r1" in block
    warn_idx = block.index("⚠ 1 pending item looks like figure/table work: r1")
    close_idx = block.index("=========================")
    assert warn_idx < close_idx


def test_build_readiness_block_backward_compatible_without_artifact_warnings():
    block = handoff_module._build_readiness_block("week-1", 2, 1)
    assert "=== HANDOFF READINESS ===" in block
