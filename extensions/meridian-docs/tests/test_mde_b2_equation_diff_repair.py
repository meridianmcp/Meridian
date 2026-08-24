"""Tests for compare_equation_structures and repair_equation_batch
(e4265dd1, MDE-B2 P0) -- reference-aware equation diff and draft-only
staged repair.

Covers:
  - compare_equation_structures: equivalent (incl. XML prefix/whitespace-
    only differences), structural_change (subscript/fraction/limit/
    operator), missing_in_candidate/added_in_candidate, number-based
    matching, anchor-based matching, positional fallback matching.
  - repair_equation_batch: every operation class (remove_duplicate_plaintext,
    split_merged_omml, restore_missing_omml, renumber_equation,
    manual_review_required); all-or-nothing batch rejection; the canonical
    source staying byte-identical after every repair; a deterministic patch
    manifest (reusing tools.meridian_fallbacks.patch_manifest.PatchManifest);
    staleness (expected_structure_hash / expected_source_fingerprint);
    same-anchor-twice-in-one-batch conflict; the soft-dependency failure
    path when PatchManifest is unavailable.

All tests use synthetic .docx bytes/files built inline -- no real files, no
network, no dependency on any dissertation or manuscript.
"""
from __future__ import annotations

import io
import os
import sys
import zipfile

import pytest

_EXT_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _EXT_PATH not in sys.path:
    sys.path.insert(0, _EXT_PATH)

from meridian_docs import docs_intel  # noqa: E402

_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"

_NS_HEADER = f'xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:m="{_M}"'


def _zip_docx(xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def _write_docx(tmp_path, xml: str, name: str = "sample.docx") -> str:
    path = tmp_path / name
    path.write_bytes(_zip_docx(xml))
    return str(path)


def _doc(body_xml: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document {_NS_HEADER}>
  <w:body>
{body_xml}
  </w:body>
</w:document>"""


def _omath(inner: str) -> str:
    return f"<m:oMath>{inner}</m:oMath>"


def _run(*texts: str) -> str:
    return "".join(f"<m:r><m:t>{t}</m:t></m:r>" for t in texts)


def _numbered_row(para_id: str, omath_xml: str, number_text: str) -> str:
    return f'''    <w:tr>
      <w:tc><w:p w14:paraId="{para_id}">{omath_xml}</w:p></w:tc>
      <w:tc><w:p><w:r><w:t>{number_text}</w:t></w:r></w:p></w:tc>
    </w:tr>'''


_SUBSCRIPT_X1 = (
    '<m:sSub><m:e><m:r><m:t>x</m:t></m:r></m:e>'
    '<m:sub><m:r><m:t>1</m:t></m:r></m:sub></m:sSub>'
)


# ---------------------------------------------------------------------------
# compare_equation_structures
# ---------------------------------------------------------------------------

def test_error_propagates_from_either_side():
    good = _zip_docx(_doc(f'<w:p w14:paraId="BBB00001">{_omath(_run("x"))}</w:p>'))
    result = docs_intel.compare_equation_structures(b"not a zip", good)
    assert "error" in result
    assert result["error"].startswith("reference_source:")

    result2 = docs_intel.compare_equation_structures(good, b"not a zip")
    assert "error" in result2
    assert result2["error"].startswith("candidate_source:")


def test_identical_equation_is_equivalent():
    doc = _zip_docx(_doc(f'<w:p w14:paraId="BBB00002">{_omath(_run("x", "=", "y"))}</w:p>'))
    result = docs_intel.compare_equation_structures(doc, doc)
    assert result["mismatch_count"] == 0
    assert result["comparisons"][0]["classification"] == "equivalent"
    assert result["comparisons"][0]["match_basis"] == "anchor"


def test_prefix_and_whitespace_only_difference_is_equivalent():
    reference = _zip_docx(_doc(
        f'<w:p w14:paraId="BBB00003"><m:oMath xmlns:m="{_M}"><m:r><m:t>x</m:t></m:r></m:oMath></w:p>'
    ))
    candidate = _zip_docx(_doc(
        f'<w:p w14:paraId="BBB00003"><ns9:oMath xmlns:ns9="{_M}">'
        "<ns9:r><ns9:t>x</ns9:t></ns9:r></ns9:oMath></w:p>"
    ))
    result = docs_intel.compare_equation_structures(reference, candidate)
    assert result["comparisons"][0]["classification"] == "equivalent"
    assert result["comparisons"][0]["token_diff"]["equal"] is True


def test_subscript_added_is_a_structural_change():
    reference = _zip_docx(_doc(f'<w:p w14:paraId="BBB00004">{_omath(_run("x"))}</w:p>'))
    candidate = _zip_docx(_doc(f'<w:p w14:paraId="BBB00004">{_omath(_SUBSCRIPT_X1)}</w:p>'))
    result = docs_intel.compare_equation_structures(reference, candidate)
    comparison = result["comparisons"][0]
    assert comparison["classification"] == "structural_change"
    assert "subscript" in comparison["token_diff"]["changed_categories"]


def test_fraction_changed_is_categorized():
    reference = _zip_docx(_doc(
        f'<w:p w14:paraId="BBB00005">{_omath(_run("2"))}</w:p>'
    ))
    fraction = (
        '<m:f><m:num><m:r><m:t>1</m:t></m:r></m:num>'
        '<m:den><m:r><m:t>2</m:t></m:r></m:den></m:f>'
    )
    candidate = _zip_docx(_doc(f'<w:p w14:paraId="BBB00005">{_omath(fraction)}</w:p>'))
    result = docs_intel.compare_equation_structures(reference, candidate)
    categories = result["comparisons"][0]["token_diff"]["changed_categories"]
    assert "fraction" in categories


def test_missing_in_candidate():
    reference = _zip_docx(_doc(f'<w:p w14:paraId="BBB00006">{_omath(_run("x"))}</w:p>'))
    candidate = _zip_docx(_doc('<w:p w14:paraId="BBB00006"><w:r><w:t>no equation now</w:t></w:r></w:p>'))
    result = docs_intel.compare_equation_structures(reference, candidate)
    assert result["comparisons"][0]["classification"] == "missing_in_candidate"
    assert result["comparisons"][0]["candidate_anchor"] is None


def test_added_in_candidate():
    reference = _zip_docx(_doc('<w:p w14:paraId="BBB00007"><w:r><w:t>no equation</w:t></w:r></w:p>'))
    candidate = _zip_docx(_doc(f'<w:p w14:paraId="BBB00007">{_omath(_run("x"))}</w:p>'))
    result = docs_intel.compare_equation_structures(reference, candidate)
    assert result["comparisons"][0]["classification"] == "added_in_candidate"
    assert result["comparisons"][0]["reference_anchor"] is None


def test_matching_by_explicit_number_across_changed_anchors():
    reference = _zip_docx(_doc(
        "<w:tbl>\n" + _numbered_row("BBB00008", _omath(_run("a")), "(1)") + "\n</w:tbl>"
    ))
    candidate = _zip_docx(_doc(
        "<w:tbl>\n" + _numbered_row("DIFFERENT_ID", _omath(_run("a")), "(1)") + "\n</w:tbl>"
    ))
    result = docs_intel.compare_equation_structures(reference, candidate)
    comparison = result["comparisons"][0]
    assert comparison["match_basis"] == "number"
    assert comparison["classification"] == "equivalent"


def test_positional_fallback_for_unnumbered_equation_with_changed_anchor():
    reference = _zip_docx(_doc(f'<w:p w14:paraId="BBB00009">{_omath(_run("x"))}</w:p>'))
    candidate = _zip_docx(_doc(f'<w:p w14:paraId="CHANGED_ID">{_omath(_run("x"))}</w:p>'))
    result = docs_intel.compare_equation_structures(reference, candidate)
    comparison = result["comparisons"][0]
    assert comparison["match_basis"] == "position"
    assert comparison["classification"] == "equivalent"


def test_result_includes_underlying_audit_findings():
    reference = _zip_docx(_doc(f'<w:p w14:paraId="BBB00010">{_omath(_run("x"))}</w:p>'))
    candidate = _zip_docx(_doc(
        '<w:p w14:paraId="BBB00010"><w:r><w:t>F=ma</w:t></w:r>'
        f'{_omath(_run("F", "=", "ma"))}</w:p>'
    ))
    result = docs_intel.compare_equation_structures(reference, candidate)
    assert any(f["type"] == "plaintext_math_duplicate" for f in result["candidate_findings"])


# ---------------------------------------------------------------------------
# repair_equation_batch -- shared setup helpers
# ---------------------------------------------------------------------------

def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def test_operations_must_be_a_non_empty_list(tmp_path):
    path = _write_docx(tmp_path, _doc(f'<w:p w14:paraId="CCC00001">{_omath(_run("x"))}</w:p>'))
    result = docs_intel.repair_equation_batch(path, [], str(tmp_path / "draft.docx"))
    assert result["applied"] is False
    assert "error" in result


def test_draft_output_path_must_differ_from_document_path(tmp_path):
    path = _write_docx(tmp_path, _doc(f'<w:p w14:paraId="CCC00002">{_omath(_run("x"))}</w:p>'))
    result = docs_intel.repair_equation_batch(
        path, [{"op_class": "manual_review_required", "anchor": "CCC00002"}], path,
    )
    assert result["applied"] is False
    assert "error" in result


def test_soft_dependency_failure_when_patch_manifest_unavailable(tmp_path, monkeypatch):
    path = _write_docx(tmp_path, _doc(f'<w:p w14:paraId="CCC00003">{_omath(_run("x"))}</w:p>'))
    monkeypatch.setattr(docs_intel, "_EquationPatchManifest", None)
    result = docs_intel.repair_equation_batch(
        path, [{"op_class": "manual_review_required", "anchor": "CCC00003"}],
        str(tmp_path / "draft.docx"),
    )
    assert result["applied"] is False
    assert "not importable" in result["error"]
    assert not os.path.exists(tmp_path / "draft.docx")


# ---------------------------------------------------------------------------
# remove_duplicate_plaintext
# ---------------------------------------------------------------------------

def test_remove_duplicate_plaintext_leaves_canonical_untouched_and_fixes_draft(tmp_path):
    xml = _doc(
        '<w:p w14:paraId="CCC00004"><w:r><w:t>F=ma</w:t></w:r>'
        f'{_omath(_run("F", "=", "ma"))}</w:p>'
    )
    path = _write_docx(tmp_path, xml)
    before = _read(path)
    draft = str(tmp_path / "draft.docx")

    result = docs_intel.repair_equation_batch(
        path, [{"op_class": "remove_duplicate_plaintext", "anchor": "CCC00004"}], draft,
    )
    assert result["applied"] is True
    assert _read(path) == before  # canonical byte-identical

    draft_audit = docs_intel.audit_equation_integrity(draft)
    assert draft_audit["findings"] == []
    assert draft_audit["equation_count"] == 1


def test_remove_duplicate_plaintext_conflict_when_no_duplicate_present(tmp_path):
    path = _write_docx(tmp_path, _doc(f'<w:p w14:paraId="CCC00005">{_omath(_run("x"))}</w:p>'))
    draft = str(tmp_path / "draft.docx")
    result = docs_intel.repair_equation_batch(
        path, [{"op_class": "remove_duplicate_plaintext", "anchor": "CCC00005"}], draft,
    )
    assert result["applied"] is False
    assert result["reason"] == "batch_has_conflicts"
    assert result["conflicts"][0]["status"] == "conflict"
    assert not os.path.exists(draft)


# ---------------------------------------------------------------------------
# split_merged_omml
# ---------------------------------------------------------------------------

def test_split_merged_omml_produces_two_clean_equations(tmp_path):
    inner = _run("a", "=", "b") + '<m:r><m:t xml:space="preserve"> </m:t></m:r>' + _run("c", "=", "d")
    xml = _doc(f'<w:p w14:paraId="CCC00006">{_omath(inner)}</w:p>')
    path = _write_docx(tmp_path, xml)
    before = _read(path)
    draft = str(tmp_path / "draft.docx")

    result = docs_intel.repair_equation_batch(
        path, [{"op_class": "split_merged_omml", "anchor": "CCC00006"}], draft,
    )
    assert result["applied"] is True
    assert result["applied_operations"][0]["split_into"] == 2
    assert _read(path) == before

    draft_audit = docs_intel.audit_equation_integrity(draft)
    assert draft_audit["equation_count"] == 2
    assert draft_audit["findings"] == []
    flat_texts = sorted(r["flat_text"] for r in draft_audit["records"])
    assert flat_texts == ["a=b", "c=d"]


def test_split_merged_omml_conflict_when_not_actually_merged(tmp_path):
    path = _write_docx(tmp_path, _doc(f'<w:p w14:paraId="CCC00007">{_omath(_run("x"))}</w:p>'))
    draft = str(tmp_path / "draft.docx")
    result = docs_intel.repair_equation_batch(
        path, [{"op_class": "split_merged_omml", "anchor": "CCC00007"}], draft,
    )
    assert result["applied"] is False
    assert result["conflicts"][0]["status"] == "conflict"


# ---------------------------------------------------------------------------
# restore_missing_omml
# ---------------------------------------------------------------------------

def test_restore_missing_omml_from_latex(tmp_path):
    xml = _doc('<w:p w14:paraId="CCC00008"><w:r><w:t>F = ma</w:t></w:r></w:p>')
    path = _write_docx(tmp_path, xml)
    before = _read(path)
    draft = str(tmp_path / "draft.docx")

    result = docs_intel.repair_equation_batch(
        path,
        [{"op_class": "restore_missing_omml", "anchor": "CCC00008", "latex": "F=ma"}],
        draft,
    )
    assert result["applied"] is True
    assert _read(path) == before

    draft_audit = docs_intel.audit_equation_integrity(draft)
    assert draft_audit["equation_count"] == 1
    assert draft_audit["findings"] == []


def test_restore_missing_omml_requires_a_payload(tmp_path):
    path = _write_docx(tmp_path, _doc('<w:p w14:paraId="CCC00009"><w:r><w:t>F = ma</w:t></w:r></w:p>'))
    draft = str(tmp_path / "draft.docx")
    result = docs_intel.repair_equation_batch(
        path, [{"op_class": "restore_missing_omml", "anchor": "CCC00009"}], draft,
    )
    assert result["applied"] is False
    assert "requires 'omml'" in result["conflicts"][0]["reason"]


def test_restore_missing_omml_rejects_invalid_omml(tmp_path):
    path = _write_docx(tmp_path, _doc('<w:p w14:paraId="CCC00010"><w:r><w:t>F = ma</w:t></w:r></w:p>'))
    draft = str(tmp_path / "draft.docx")
    result = docs_intel.repair_equation_batch(
        path,
        [{"op_class": "restore_missing_omml", "anchor": "CCC00010", "omml": "<not-omml/>"}],
        draft,
    )
    assert result["applied"] is False


# ---------------------------------------------------------------------------
# renumber_equation
# ---------------------------------------------------------------------------

def test_renumber_equation(tmp_path):
    xml = _doc(
        "<w:tbl>\n" + _numbered_row("CCC00011", _omath(_run("a")), "(5)") + "\n</w:tbl>"
    )
    path = _write_docx(tmp_path, xml)
    before = _read(path)
    draft = str(tmp_path / "draft.docx")

    result = docs_intel.repair_equation_batch(
        path,
        [{"op_class": "renumber_equation", "anchor": "CCC00011", "new_number": "(1)"}],
        draft,
    )
    assert result["applied"] is True
    assert _read(path) == before

    draft_audit = docs_intel.audit_equation_integrity(draft)
    assert draft_audit["records"][0]["number"] == "(1)"


def test_renumber_equation_requires_table_numbered_pattern(tmp_path):
    path = _write_docx(tmp_path, _doc(f'<w:p w14:paraId="CCC00012">{_omath(_run("x"))}</w:p>'))
    draft = str(tmp_path / "draft.docx")
    result = docs_intel.repair_equation_batch(
        path,
        [{"op_class": "renumber_equation", "anchor": "CCC00012", "new_number": "(1)"}],
        draft,
    )
    assert result["applied"] is False
    assert "not a table-numbered equation" in result["conflicts"][0]["reason"]


# ---------------------------------------------------------------------------
# manual_review_required -- deliberate no-op, still travels in the manifest.
# ---------------------------------------------------------------------------

def test_manual_review_required_is_a_no_op_that_still_produces_a_draft(tmp_path):
    path = _write_docx(tmp_path, _doc(f'<w:p w14:paraId="CCC00013">{_omath(_run("x"))}</w:p>'))
    before = _read(path)
    draft = str(tmp_path / "draft.docx")
    result = docs_intel.repair_equation_batch(
        path,
        [{"op_class": "manual_review_required", "anchor": "CCC00013", "note": "ambiguous, ask a human"}],
        draft,
    )
    assert result["applied"] is True
    assert result["applied_operations"][0]["mutated"] is False
    assert _read(path) == before
    # Nothing was mutated -- the draft's equation is logically unchanged
    # from the canonical (byte-identity isn't guaranteed across a
    # save/reserialize round trip even with zero edits, so compare
    # structurally instead).
    comparison = docs_intel.compare_equation_structures(path, draft)
    assert comparison["mismatch_count"] == 0


def test_manual_review_required_does_not_need_an_anchor(tmp_path):
    path = _write_docx(tmp_path, _doc(f'<w:p w14:paraId="CCC00014">{_omath(_run("x"))}</w:p>'))
    draft = str(tmp_path / "draft.docx")
    result = docs_intel.repair_equation_batch(
        path, [{"op_class": "manual_review_required", "note": "no specific anchor"}], draft,
    )
    assert result["applied"] is True


# ---------------------------------------------------------------------------
# All-or-nothing batch semantics.
# ---------------------------------------------------------------------------

def test_batch_with_one_bad_operation_rejects_the_whole_batch(tmp_path):
    xml = _doc(
        '<w:p w14:paraId="CCC00015"><w:r><w:t>F=ma</w:t></w:r>'
        f'{_omath(_run("F", "=", "ma"))}</w:p>'
        f'<w:p w14:paraId="CCC00016">{_omath(_run("x"))}</w:p>'
    )
    path = _write_docx(tmp_path, xml)
    before = _read(path)
    draft = str(tmp_path / "draft.docx")

    result = docs_intel.repair_equation_batch(
        path,
        [
            {"op_class": "remove_duplicate_plaintext", "anchor": "CCC00015"},  # valid
            {"op_class": "remove_duplicate_plaintext", "anchor": "CCC00016"},  # no duplicate -> conflict
        ],
        draft,
    )
    assert result["applied"] is False
    assert not os.path.exists(draft)
    assert _read(path) == before
    assert len(result["conflicts"]) == 1


def test_same_anchor_targeted_twice_in_one_batch_is_a_conflict(tmp_path):
    # manual_review_required deliberately skips anchor validation entirely
    # (see test_manual_review_required_does_not_need_an_anchor) -- exercise
    # the collision guard with an op_class that actually resolves the
    # anchor against the live audit.
    xml = _doc(
        "<w:tbl>\n" + _numbered_row("CCC00017", _omath(_run("a")), "(5)") + "\n</w:tbl>"
    )
    path = _write_docx(tmp_path, xml)
    draft = str(tmp_path / "draft.docx")
    result = docs_intel.repair_equation_batch(
        path,
        [
            {"op_class": "renumber_equation", "anchor": "CCC00017", "new_number": "(1)"},
            {"op_class": "renumber_equation", "anchor": "CCC00017", "new_number": "(2)"},
        ],
        draft,
    )
    assert result["applied"] is False
    assert any("also targeted by operation" in (c["reason"] or "") for c in result["conflicts"])


def test_unknown_anchor_is_not_found(tmp_path):
    path = _write_docx(tmp_path, _doc(f'<w:p w14:paraId="CCC00018">{_omath(_run("x"))}</w:p>'))
    draft = str(tmp_path / "draft.docx")
    result = docs_intel.repair_equation_batch(
        path,
        [{"op_class": "renumber_equation", "anchor": "NO_SUCH_ANCHOR", "new_number": "(1)"}],
        draft,
    )
    assert result["applied"] is False
    assert result["conflicts"][0]["status"] == "not_found"


def test_unknown_op_class_is_invalid(tmp_path):
    path = _write_docx(tmp_path, _doc(f'<w:p w14:paraId="CCC00019">{_omath(_run("x"))}</w:p>'))
    draft = str(tmp_path / "draft.docx")
    result = docs_intel.repair_equation_batch(
        path, [{"op_class": "delete_everything", "anchor": "CCC00019"}], draft,
    )
    assert result["applied"] is False
    assert result["conflicts"][0]["status"] == "invalid"


# ---------------------------------------------------------------------------
# Staleness preconditions.
# ---------------------------------------------------------------------------

def test_expected_source_fingerprint_mismatch_is_rejected(tmp_path):
    path = _write_docx(tmp_path, _doc(f'<w:p w14:paraId="CCC00020">{_omath(_run("x"))}</w:p>'))
    draft = str(tmp_path / "draft.docx")
    result = docs_intel.repair_equation_batch(
        path,
        [{"op_class": "manual_review_required", "anchor": "CCC00020"}],
        draft,
        expected_source_fingerprint="0" * 64,
    )
    assert result["applied"] is False
    assert result["reason"] == "document_changed_since_planning"
    assert not os.path.exists(draft)


def test_expected_structure_hash_mismatch_is_stale(tmp_path):
    path = _write_docx(
        tmp_path,
        _doc('<w:p w14:paraId="CCC00021"><w:r><w:t>F=ma</w:t></w:r>' + _omath(_run("F", "=", "ma")) + "</w:p>"),
    )
    draft = str(tmp_path / "draft.docx")
    result = docs_intel.repair_equation_batch(
        path,
        [{
            "op_class": "remove_duplicate_plaintext",
            "anchor": "CCC00021",
            "expected_structure_hash": "0" * 64,
        }],
        draft,
    )
    assert result["applied"] is False
    assert result["conflicts"][0]["status"] == "stale"


# ---------------------------------------------------------------------------
# The patch manifest itself: reused PatchManifest primitive, deterministic
# content hash.
# ---------------------------------------------------------------------------

def test_patch_manifest_is_a_real_reused_patchmanifest(tmp_path):
    path = _write_docx(tmp_path, _doc(f'<w:p w14:paraId="CCC00022">{_omath(_run("x"))}</w:p>'))
    draft = str(tmp_path / "draft.docx")
    result = docs_intel.repair_equation_batch(
        path, [{"op_class": "manual_review_required", "anchor": "CCC00022"}], draft,
    )
    manifest = result["patch_manifest"]
    assert manifest["schema_version"] == 1
    assert manifest["status"] == "applied"
    assert manifest["operations"][0]["kind"] == "custom"
    assert manifest["operations"][0]["metadata"]["repair_class"] == "manual_review_required"
    assert manifest["base_sha256"] is not None


def test_manifest_content_hash_is_deterministic_across_calls(tmp_path):
    xml = _doc(f'<w:p w14:paraId="CCC00023">{_omath(_run("x"))}</w:p>')
    path = _write_docx(tmp_path, xml)
    ops = [{"op_class": "manual_review_required", "anchor": "CCC00023"}]

    result1 = docs_intel.repair_equation_batch(path, ops, str(tmp_path / "draft1.docx"))
    result2 = docs_intel.repair_equation_batch(path, ops, str(tmp_path / "draft2.docx"))

    assert result1["manifest_content_hash"] == result2["manifest_content_hash"]
    # But the real manifests are genuinely distinct records (fresh id/timestamp).
    assert result1["patch_manifest"]["manifest_id"] != result2["patch_manifest"]["manifest_id"]


def test_manifest_content_hash_changes_when_operations_differ(tmp_path):
    xml = _doc(f'<w:p w14:paraId="CCC00024">{_omath(_run("x"))}</w:p>')
    path = _write_docx(tmp_path, xml)

    result1 = docs_intel.repair_equation_batch(
        path, [{"op_class": "manual_review_required", "anchor": "CCC00024"}], str(tmp_path / "draft1.docx"),
    )
    # A rejected batch still gets a manifest with the conflict recorded --
    # exercise that path too for a batch whose operation list is genuinely
    # different (a different op_class) and fails validation.
    reject = docs_intel.repair_equation_batch(
        path, [{"op_class": "renumber_equation", "anchor": "CCC00024", "new_number": "(1)"}],
        str(tmp_path / "draft3.docx"),
    )
    assert reject["applied"] is False
    assert reject["patch_manifest"]["status"] == "aborted"
    assert result1["manifest_content_hash"] != reject["manifest_content_hash"]
