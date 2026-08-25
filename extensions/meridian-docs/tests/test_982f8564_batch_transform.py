"""Tests for docs_intel's declarative batch transforms (982f8564, MDE-8 P1).

Covers:
  - plan_batch_transform: pure/read-only, reproducible manifest_hash,
    stale-anchor rejection (whole-document fingerprint AND per-anchor
    expected_quoted_text), in-batch conflict detection (duplicate target,
    compound-partner overlap), unsupported element types, deterministic
    application order.
  - apply_batch_transform: all-or-nothing against an isolated draft (source
    document is NEVER mutated, byte-for-byte, whether the batch succeeds or
    is rejected), owned compound objects (table+caption, figure+caption)
    auto-included on delete unless keep_caption=True, TOCTOU re-check
    between plan and apply.
  - apply_and_merge_batch_transform: end-to-end promotion via the existing,
    unmodified merge_draft_into_canonical, with a batch_receipt carrying
    package/provenance/render evidence.
"""
from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest

from meridian_docs import docs_intel


@pytest.fixture(autouse=True)
def _default_render_capability(monkeypatch):
    """Same stub convention as test_fe989980_merge_draft.py's own fixture:
    apply_and_merge_batch_transform's promotion step (merge_draft_into_
    canonical) invokes the REAL render-capability gate after structural
    verification -- these tests exercise BATCH-TRANSFORM correctness
    (planning/conflicts/compound objects/isolation), not render-backend
    behavior, and must not depend on -- or be slowed/blocked or made
    flaky by -- whichever render backends (LibreOffice, Word COM) happen
    to be installed on the machine running the suite. A hand-crafted
    minimal .docx (document.xml only, no [Content_Types].xml/_rels) is
    also not something a REAL Word/LibreOffice install can open, which
    would otherwise fail these tests for a reason unrelated to what they
    exist to check.
    """
    monkeypatch.setattr(
        docs_intel.render_gate,
        "check_render_capability",
        lambda docx_path, **kwargs: {
            "status": "rendered",
            "backend": "test-stub",
            "detail": {"stub": True},
        },
    )


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"

_DOC_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="{_W}"
    xmlns:w14="{_W14}">
  <w:body>
    <w:p w14:paraId="H0000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000001">
      <w:r><w:t>Plain paragraph one.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000002">
      <w:r><w:t>Figure host paragraph.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="F0000001">
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:r><w:t xml:space="preserve">Figure </w:t></w:r>
      <w:fldSimple w:instr="SEQ Figure \\* ARABIC"><w:r><w:t>1</w:t></w:r></w:fldSimple>
      <w:r><w:t xml:space="preserve">. Overview diagram.</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Metric</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:p w14:paraId="T0000001">
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:r><w:t xml:space="preserve">Table </w:t></w:r>
      <w:fldSimple w:instr="SEQ Table \\* ARABIC"><w:r><w:t>1</w:t></w:r></w:fldSimple>
      <w:r><w:t xml:space="preserve">. Threshold data.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000003">
      <w:r><w:t>Closing paragraph.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def _make_docx_bytes(xml: str = _DOC_XML) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def _write_docx(tmp_path, xml: str = _DOC_XML, name: str = "doc.docx") -> str:
    path = str(tmp_path / name)
    with open(path, "wb") as fh:
        fh.write(_make_docx_bytes(xml))
    return path


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _document_xml_text(docx_path: str) -> str:
    with zipfile.ZipFile(docx_path) as zf:
        return zf.read("word/document.xml").decode("utf-8")


# ---------------------------------------------------------------------------
# plan_batch_transform
# ---------------------------------------------------------------------------

class TestPlanBatchTransform:
    def test_empty_operations_is_an_error(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        result = docs_intel.plan_batch_transform(doc, [])
        assert "error" in result

    def test_missing_document_is_an_error(self, tmp_path) -> None:
        result = docs_intel.plan_batch_transform(
            str(tmp_path / "nope.docx"),
            [{"op": "delete_anchor", "anchor": {"para_id": "P0000001"}}],
        )
        assert "error" in result

    def test_simple_delete_plans_ready(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        plan = docs_intel.plan_batch_transform(
            doc, [{"op_id": "op1", "op": "delete_anchor", "anchor": {"para_id": "P0000001"}}],
        )
        assert plan["ready"] is True
        assert plan["ready_count"] == 1
        assert plan["conflict_count"] == 0
        assert plan["application_order"][0]["target_para_id"] == "P0000001"
        assert plan["application_order"][0]["compound_partner_para_id"] is None

    def test_unresolvable_anchor_is_a_conflict(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        plan = docs_intel.plan_batch_transform(
            doc, [{"op": "delete_anchor", "anchor": {"para_id": "NOPE"}}],
        )
        assert plan["ready"] is False
        assert plan["conflict_count"] == 1
        assert plan["conflicts"][0]["status"] == "not_found"

    def test_table_delete_auto_includes_caption_partner(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        plan = docs_intel.plan_batch_transform(
            doc, [{"op": "delete_anchor", "anchor": {"para_id": "tbl4"}}],
        )
        assert plan["ready"] is True
        entry = plan["application_order"][0]
        assert entry["element_type"] == "table"
        assert entry["compound_partner_para_id"] == "T0000001"

    def test_figure_delete_auto_includes_caption_partner(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        plan = docs_intel.plan_batch_transform(
            doc, [{"op": "delete_anchor", "anchor": {"para_id": "P0000002"}}],
        )
        assert plan["ready"] is True
        entry = plan["application_order"][0]
        assert entry["compound_partner_para_id"] == "F0000001"

    def test_keep_caption_true_skips_auto_partner(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        plan = docs_intel.plan_batch_transform(
            doc, [{"op": "delete_anchor", "anchor": {"para_id": "tbl4"}, "keep_caption": True}],
        )
        entry = plan["application_order"][0]
        assert entry["compound_partner_para_id"] is None

    def test_expected_quoted_text_mismatch_is_stale(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        plan = docs_intel.plan_batch_transform(
            doc,
            [{
                "op": "delete_anchor", "anchor": {"para_id": "P0000001"},
                "expected_quoted_text": "This is not the real text",
            }],
        )
        assert plan["ready"] is False
        assert plan["conflicts"][0]["status"] == "stale"

    def test_expected_quoted_text_match_is_ready(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        plan = docs_intel.plan_batch_transform(
            doc,
            [{
                "op": "delete_anchor", "anchor": {"para_id": "P0000001"},
                "expected_quoted_text": "Plain paragraph one.",
            }],
        )
        assert plan["ready"] is True

    def test_expected_source_fingerprint_mismatch_stales_every_op(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        plan = docs_intel.plan_batch_transform(
            doc,
            [
                {"op": "delete_anchor", "anchor": {"para_id": "P0000001"}},
                {"op": "delete_anchor", "anchor": {"para_id": "P0000003"}},
            ],
            expected_source_fingerprint="not-the-real-fingerprint",
        )
        assert plan["ready"] is False
        assert plan["conflict_count"] == 2
        assert all(c["status"] == "stale" for c in plan["conflicts"])

    def test_expected_source_fingerprint_match_proceeds_normally(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        first = docs_intel.plan_batch_transform(
            doc, [{"op": "delete_anchor", "anchor": {"para_id": "P0000001"}}],
        )
        second = docs_intel.plan_batch_transform(
            doc, [{"op": "delete_anchor", "anchor": {"para_id": "P0000001"}}],
            expected_source_fingerprint=first["source_fingerprint"],
        )
        assert second["ready"] is True

    def test_duplicate_target_in_same_batch_is_a_conflict(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        plan = docs_intel.plan_batch_transform(
            doc,
            [
                {"op_id": "a", "op": "delete_anchor", "anchor": {"para_id": "P0000001"}},
                {"op_id": "b", "op": "set_text", "anchor": {"para_id": "P0000001"}, "new_text": "x"},
            ],
        )
        assert plan["ready"] is False
        statuses = [c["status"] for c in plan["conflicts"]]
        assert "conflict" in statuses

    def test_compound_partner_overlap_is_a_conflict(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        # op "a" explicitly targets the table's caption; op "b" deletes the
        # table itself, which would auto-include that SAME caption.
        plan = docs_intel.plan_batch_transform(
            doc,
            [
                {"op_id": "a", "op": "set_text", "anchor": {"para_id": "T0000001"}, "new_text": "x"},
                {"op_id": "b", "op": "delete_anchor", "anchor": {"para_id": "tbl4"}},
            ],
        )
        assert plan["ready"] is False

    def test_unsupported_element_type_is_a_conflict(self, tmp_path) -> None:
        # table_cell is resolvable via locate_anchor's own machinery (a
        # direct para_id match against the "tbl<N>:r<row>:c<col>" synthetic
        # id) but is deliberately NOT in this batch layer's mutable set --
        # confirms unsupported element types are refused, not guessed at.
        doc = _write_docx(tmp_path)
        plan = docs_intel.plan_batch_transform(
            doc, [{"op": "delete_anchor", "anchor": {"para_id": "tbl4:r0:c0"}}],
        )
        assert plan["ready"] is False
        assert "unsupported_element_type" in plan["conflicts"][0]["reason"]

    def test_invalid_op_type_is_invalid_status(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        plan = docs_intel.plan_batch_transform(
            doc, [{"op": "delete_everything", "anchor": {"para_id": "P0000001"}}],
        )
        assert plan["ready"] is False
        assert plan["conflicts"][0]["status"] == "invalid"

    def test_missing_anchor_is_invalid(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        plan = docs_intel.plan_batch_transform(doc, [{"op": "delete_anchor"}])
        assert plan["conflicts"][0]["status"] == "invalid"

    def test_reproducible_manifest_hash(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        ops = [
            {"op_id": "z", "op": "delete_anchor", "anchor": {"para_id": "P0000003"}},
            {"op_id": "a", "op": "set_text", "anchor": {"para_id": "P0000001"}, "new_text": "hi"},
        ]
        plan1 = docs_intel.plan_batch_transform(doc, ops)
        plan2 = docs_intel.plan_batch_transform(doc, ops)
        assert plan1["manifest_hash"] == plan2["manifest_hash"]
        assert plan1["manifest_hash"]

    def test_different_batches_hash_differently(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        plan1 = docs_intel.plan_batch_transform(
            doc, [{"op": "delete_anchor", "anchor": {"para_id": "P0000001"}}],
        )
        plan2 = docs_intel.plan_batch_transform(
            doc, [{"op": "delete_anchor", "anchor": {"para_id": "P0000003"}}],
        )
        assert plan1["manifest_hash"] != plan2["manifest_hash"]

    def test_deterministic_application_order_by_document_position(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        # Declared out of document order -- application_order must still
        # come back in document position order (P0000001 before P0000003).
        plan = docs_intel.plan_batch_transform(
            doc,
            [
                {"op_id": "later", "op": "delete_anchor", "anchor": {"para_id": "P0000003"}},
                {"op_id": "earlier", "op": "delete_anchor", "anchor": {"para_id": "P0000001"}},
            ],
        )
        assert plan["ready"] is True
        ordered_ids = [e["op_id"] for e in plan["application_order"]]
        assert ordered_ids == ["earlier", "later"]

    def test_never_mutates_the_source_document(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        before = _read_bytes(doc)
        docs_intel.plan_batch_transform(
            doc, [{"op": "delete_anchor", "anchor": {"para_id": "P0000001"}}],
        )
        after = _read_bytes(doc)
        assert before == after


# ---------------------------------------------------------------------------
# apply_batch_transform
# ---------------------------------------------------------------------------

class TestApplyBatchTransform:
    def test_draft_path_same_as_source_is_rejected(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        result = docs_intel.apply_batch_transform(
            doc, [{"op": "delete_anchor", "anchor": {"para_id": "P0000001"}}], doc,
        )
        assert result["applied"] is False
        assert "error" in result

    def test_conflicted_batch_never_writes_a_draft(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        draft = str(tmp_path / "draft.docx")
        result = docs_intel.apply_batch_transform(
            doc, [{"op": "delete_anchor", "anchor": {"para_id": "NOPE"}}], draft,
        )
        assert result["applied"] is False
        assert result["reason"] == "batch_has_conflicts"
        assert result["conflicts"]
        assert not docs_intel.os.path.exists(draft)

    def test_source_document_never_mutated_on_success(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        draft = str(tmp_path / "draft.docx")
        before = _read_bytes(doc)
        result = docs_intel.apply_batch_transform(
            doc, [{"op": "delete_anchor", "anchor": {"para_id": "P0000001"}}], draft,
        )
        assert result["applied"] is True
        assert _read_bytes(doc) == before

    def test_delete_anchor_removes_paragraph_in_draft_only(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        draft = str(tmp_path / "draft.docx")
        docs_intel.apply_batch_transform(
            doc, [{"op": "delete_anchor", "anchor": {"para_id": "P0000001"}}], draft,
        )
        draft_xml = _document_xml_text(draft)
        assert "P0000001" not in draft_xml
        source_xml = _document_xml_text(doc)
        assert "P0000001" in source_xml

    def test_table_delete_removes_table_and_caption_together(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        draft = str(tmp_path / "draft.docx")
        result = docs_intel.apply_batch_transform(
            doc, [{"op": "delete_anchor", "anchor": {"para_id": "tbl4"}}], draft,
        )
        assert result["applied"] is True
        applied_types = {a["op_type"] for a in result["applied_operations"]}
        assert "delete_compound_partner" in applied_types
        draft_xml = _document_xml_text(draft)
        assert "<w:tbl>" not in draft_xml
        assert "T0000001" not in draft_xml  # caption gone too

    def test_table_delete_keep_caption_leaves_caption(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        draft = str(tmp_path / "draft.docx")
        docs_intel.apply_batch_transform(
            doc, [{"op": "delete_anchor", "anchor": {"para_id": "tbl4"}, "keep_caption": True}], draft,
        )
        draft_xml = _document_xml_text(draft)
        assert "<w:tbl>" not in draft_xml
        assert "T0000001" in draft_xml  # caption preserved

    def test_figure_delete_removes_figure_and_caption(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        draft = str(tmp_path / "draft.docx")
        docs_intel.apply_batch_transform(
            doc, [{"op": "delete_anchor", "anchor": {"para_id": "P0000002"}}], draft,
        )
        draft_xml = _document_xml_text(draft)
        assert "P0000002" not in draft_xml
        assert "F0000001" not in draft_xml

    def test_set_text_replaces_paragraph_content(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        draft = str(tmp_path / "draft.docx")
        result = docs_intel.apply_batch_transform(
            doc,
            [{"op": "set_text", "anchor": {"para_id": "P0000001"}, "new_text": "Replaced text."}],
            draft,
        )
        assert result["applied"] is True
        draft_xml = _document_xml_text(draft)
        assert "Replaced text." in draft_xml
        assert "Plain paragraph one." not in draft_xml

    def test_set_text_missing_new_text_fails(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        draft = str(tmp_path / "draft.docx")
        result = docs_intel.apply_batch_transform(
            doc, [{"op": "set_text", "anchor": {"para_id": "P0000001"}}], draft,
        )
        assert result["applied"] is False

    def test_set_text_on_table_is_rejected(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        draft = str(tmp_path / "draft.docx")
        result = docs_intel.apply_batch_transform(
            doc, [{"op": "set_text", "anchor": {"para_id": "tbl4"}, "new_text": "x"}], draft,
        )
        assert result["applied"] is False

    def test_write_transaction_present_on_success(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        draft = str(tmp_path / "draft.docx")
        result = docs_intel.apply_batch_transform(
            doc, [{"op": "delete_anchor", "anchor": {"para_id": "P0000001"}}], draft,
        )
        assert result["write_transaction"]
        assert "promoted_sha256" in result["write_transaction"]
        assert result["manifest"]["manifest_hash"]

    def test_document_changed_between_plan_and_apply_is_refused(self, tmp_path, monkeypatch) -> None:
        doc = _write_docx(tmp_path)
        draft = str(tmp_path / "draft.docx")

        real_fingerprint = docs_intel._source_fingerprint
        calls: list[int] = []

        def _flaky_fingerprint(source):
            calls.append(1)
            # First call is plan_batch_transform's own read; second is
            # apply_batch_transform's independent re-read immediately
            # before mutating -- simulate a concurrent external write
            # landing in between by returning a DIFFERENT fingerprint the
            # second time, regardless of actual content.
            if len(calls) >= 2:
                return "tampered-fingerprint-simulating-a-concurrent-write"
            return real_fingerprint(source)

        monkeypatch.setattr(docs_intel, "_source_fingerprint", _flaky_fingerprint)
        result = docs_intel.apply_batch_transform(
            doc, [{"op": "delete_anchor", "anchor": {"para_id": "P0000001"}}], draft,
        )
        assert result["applied"] is False
        assert result["reason"] == "document_changed_during_apply"
        assert not docs_intel.os.path.exists(draft)

    def test_element_vanished_during_apply_is_refused(self, tmp_path, monkeypatch) -> None:
        doc = _write_docx(tmp_path)
        draft = str(tmp_path / "draft.docx")
        # Force the plan to believe an element exists at a para_id that the
        # live tree lookup will not find, by monkeypatching the resolver's
        # target after planning succeeds but before the tree walk.
        original_plan = docs_intel.plan_batch_transform

        def _fake_plan(*args, **kwargs):
            result = original_plan(*args, **kwargs)
            if result.get("ready"):
                for entry in result["application_order"]:
                    entry["target_para_id"] = "GHOST_ID_NOT_IN_TREE"
            return result

        monkeypatch.setattr(docs_intel, "plan_batch_transform", _fake_plan)
        result = docs_intel.apply_batch_transform(
            doc, [{"op": "delete_anchor", "anchor": {"para_id": "P0000001"}}], draft,
        )
        assert result["applied"] is False
        assert result["reason"] == "element_vanished_during_apply"
        assert not docs_intel.os.path.exists(draft)


# ---------------------------------------------------------------------------
# apply_and_merge_batch_transform
# ---------------------------------------------------------------------------

class TestApplyAndMergeBatchTransform:
    def test_conflicted_batch_never_touches_canonical(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        draft = str(tmp_path / "draft.docx")
        before = _read_bytes(doc)
        result = docs_intel.apply_and_merge_batch_transform(
            doc, [{"op": "delete_anchor", "anchor": {"para_id": "NOPE"}}], draft,
        )
        assert result.get("applied") is False or result.get("merged") is False
        assert _read_bytes(doc) == before

    def test_end_to_end_promotion_carries_batch_receipt(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        draft = str(tmp_path / "draft.docx")

        result = docs_intel.apply_and_merge_batch_transform(
            doc, [{"op": "delete_anchor", "anchor": {"para_id": "P0000001"}}], draft,
        )

        assert result["merged"] is True
        assert "batch_receipt" in result
        receipt = result["batch_receipt"]
        assert receipt["manifest_hash"]
        assert receipt["operations_applied"]
        assert receipt["render_status"] == "rendered"
        assert receipt["render_verified"] is True
        assert receipt["draft_write_manifest_hash"]
        assert receipt["draft_promoted_sha256"]

        # Canonical file itself was actually updated by the promotion.
        canonical_xml = _document_xml_text(doc)
        assert "P0000001" not in canonical_xml

    def test_promotion_without_degraded_render_fails_closed_when_unavailable(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            docs_intel.render_gate, "check_render_capability",
            lambda docx_path, **kwargs: {
                "status": "unavailable-with-reason",
                "reason": "no render backend available in this environment",
            },
        )
        doc = _write_docx(tmp_path)
        draft = str(tmp_path / "draft.docx")
        before = _read_bytes(doc)

        result = docs_intel.apply_and_merge_batch_transform(
            doc, [{"op": "delete_anchor", "anchor": {"para_id": "P0000001"}}], draft,
        )

        assert result.get("merged") is not True
        assert _read_bytes(doc) == before

    def test_degraded_override_promotes_with_evidence_stamped(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            docs_intel.render_gate, "check_render_capability",
            lambda docx_path, **kwargs: {
                "status": "unavailable-with-reason",
                "reason": "no render backend available in this environment",
            },
        )
        doc = _write_docx(tmp_path)
        draft = str(tmp_path / "draft.docx")

        result = docs_intel.apply_and_merge_batch_transform(
            doc, [{"op": "delete_anchor", "anchor": {"para_id": "P0000001"}}], draft,
            allow_degraded_render=True,
            degraded_render_reason="no render backend in this CI sandbox, human confirmed manually",
        )

        assert result["merged"] is True
        assert result["batch_receipt"]["render_status"] == "unavailable-with-reason"
        assert result["batch_receipt"]["render_verified"] is False
