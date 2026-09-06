"""Renderer-independent semantic OMML contract tests for proposal e1d0552e.

Also covers (e03b41ef, BE4ED581-W2): classify_edit_packet's fail-closed
boundary between prose-edit packets and OMML-touching operations -- see
``TestClassifyEditPacketBoundary`` below.
"""
from __future__ import annotations

import io
import zipfile
import xml.etree.ElementTree as ET

import pytest

from meridian_docs import docs_intel


_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"


def _q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def _omml(body: str) -> str:
    return f'<m:oMath xmlns:m="{_M}">{body}</m:oMath>'


def test_validator_requires_omath_root_and_rejects_omathpara():
    with pytest.raises(ValueError, match="m:oMath root"):
        docs_intel._validate_omml_structure(
            f'<m:oMathPara xmlns:m="{_M}"><m:oMath /></m:oMathPara>'
        )
    with pytest.raises(ValueError, match="m:oMath root"):
        docs_intel._validate_omml_structure(f'<m:r xmlns:m="{_M}" />')


def test_validator_rejects_malformed_fraction_and_flattened_fallback():
    with pytest.raises(ValueError, match="m:num"):
        docs_intel._validate_omml_structure(
            _omml('<m:f><m:num /><m:den><m:e /></m:den></m:f>')
        )
    with pytest.raises(ValueError, match="flattened fallback"):
        docs_intel._validate_omml_structure(
            _omml('<m:r><m:t>fraction a over b</m:t></m:r>')
        )


def test_validator_accepts_structural_fraction_subscript_and_array():
    raw = _omml(
        "<m:f><m:num><m:e><m:r><m:t>a</m:t></m:r></m:e></m:num>"
        "<m:den><m:e><m:r><m:t>b</m:t></m:r></m:e></m:den></m:f>"
        "<m:sSub><m:e><m:r><m:t>x</m:t></m:r></m:e>"
        "<m:sub><m:r><m:t>i</m:t></m:r></m:sub></m:sSub>"
        "<m:eqArr><m:e><m:r><m:t>case</m:t></m:r></m:e></m:eqArr>"
    )
    root = docs_intel._validate_omml_structure(raw)
    assert root.tag == _q(_M, "oMath")


def test_mathml_fraction_and_table_conversion_preserve_semantic_wrappers():
    math = ET.fromstring(
        '<math xmlns="http://www.w3.org/1998/Math/MathML">'
        '<mfrac><mi>a</mi><mi>b</mi></mfrac>'
        '<mtable><mtr><mtd><mi>x</mi></mtd></mtr></mtable>'
        "</math>"
    )
    root = ET.Element(_q(_M, "oMath"))
    docs_intel._stdlib_append_mathml(math, root)
    raw = ET.tostring(root, encoding="unicode")
    docs_intel._validate_omml_structure(raw)
    assert root.find(f".//{_q(_M, 'f')}/{_q(_M, 'num')}/{_q(_M, 'e')}") is not None
    assert root.find(_q(_M, "eqArr")) is not None


def test_display_builder_assigns_fresh_identity_and_style():
    first = docs_intel._build_omath_paragraph(_omml('<m:r><m:t>x</m:t></m:r>'), alignment="center")
    second = docs_intel._build_omath_paragraph(_omml('<m:r><m:t>y</m:t></m:r>'), alignment="center")
    para_attr = _q(_W14, "paraId")
    text_attr = _q(_W14, "textId")
    assert first.get(para_attr) and second.get(para_attr) != first.get(para_attr)
    assert first.get(text_attr) and second.get(text_attr) != first.get(text_attr)
    assert first.find(f"./{_q(_W, 'pPr')}/{_q(_W, 'jc')}").get(_q(_W, "val")) == "center"


# ---------------------------------------------------------------------------
# e03b41ef (BE4ED581-W2): classify_edit_packet's fail-closed boundary
# between prose-edit packets (4c992e91) and OMML-touching operations.
# ---------------------------------------------------------------------------

_CLASSIFIER_DOC_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="{_W}"
    xmlns:w14="{_W14}">
  <w:body>
    <w:p w14:paraId="P0000001">
      <w:r><w:t>Plain paragraph one.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="EQ0000001">
      {_omml('<m:r><m:t>E</m:t></m:r><m:r><m:t>=</m:t></m:r><m:r><m:t>mc2</m:t></m:r>')}
    </w:p>
    <w:p w14:paraId="EQDUP0001">
      <w:r><w:t>E=mc2</w:t></w:r>
      {_omml('<m:r><m:t>E</m:t></m:r><m:r><m:t>=</m:t></m:r><m:r><m:t>mc2</m:t></m:r>')}
    </w:p>
    <w:p w14:paraId="EQCLEAN0001">
      <w:pPr><w:jc w:val="center"/></w:pPr>
      {_omml('<m:r><m:t>a</m:t></m:r><m:r><m:t>+</m:t></m:r><m:r><m:t>b</m:t></m:r>')}
      <w:r><w:t>.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def _make_classifier_docx_bytes(xml: str = _CLASSIFIER_DOC_XML) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def _write_classifier_docx(tmp_path, xml: str = _CLASSIFIER_DOC_XML, name: str = "classifier.docx") -> str:
    path = str(tmp_path / name)
    with open(path, "wb") as fh:
        fh.write(_make_classifier_docx_bytes(xml))
    return path


_VALID_OMML_PAYLOAD = _omml('<m:r><m:t>a</m:t></m:r><m:r><m:t>+</m:t></m:r><m:r><m:t>b</m:t></m:r>')
_MALFORMED_OMML_PAYLOAD = _omml('<m:f><m:num /><m:den><m:e /></m:den></m:f>')


class TestClassifyEditPacketBoundary:
    """e03b41ef (BE4ED581-W2): classify_edit_packet composes the EXISTING
    prose gate (_prose_packet_structural_error) and the EXISTING OMML
    hardening primitives (_validate_omml_structure, audit_equation_integrity,
    audit_equation_style) into one fail-closed classification -- never a
    reimplementation of any of them, and never a guess on an ambiguous or
    mixed-signal packet."""

    # -- non-dict / ambiguous input -------------------------------------

    def test_non_dict_packet_is_rejected(self):
        result = docs_intel.classify_edit_packet("not a dict")  # type: ignore[arg-type]
        assert result["boundary"] == docs_intel.CLASSIFIER_BOUNDARY_REJECTED
        assert result["reason"] == docs_intel.CLASSIFIER_REASON_NOT_A_DICT

    def test_ambiguous_packet_with_no_recognized_kind_is_rejected(self):
        result = docs_intel.classify_edit_packet({"some": "unrelated shape"})
        assert result["boundary"] == docs_intel.CLASSIFIER_BOUNDARY_REJECTED
        assert result["reason"] == docs_intel.CLASSIFIER_REASON_AMBIGUOUS_PACKET_KIND

    # -- prose side: delegates to the EXISTING gate ----------------------

    def test_built_prose_packet_classifies_as_prose(self, tmp_path):
        doc = _write_classifier_docx(tmp_path)
        packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "P0000001"}, "New prose text.",
        )
        assert packet["status"] == "built"
        result = docs_intel.classify_edit_packet(packet)
        assert result == {"boundary": docs_intel.CLASSIFIER_BOUNDARY_PROSE, "reason": None}

    def test_prose_packet_refused_at_build_classifies_as_rejected(self, tmp_path):
        """A prose packet build_prose_edit_packet itself refused (here, an
        equation-anchor refusal -- see TestProsePacketNeverTouchesOmml in
        test_4c992e91_prose_edit_packet.py for the build/apply-time version
        of this same invariant) must classify as rejected -- it must NEVER
        be silently reclassified onto the OMML side just because its
        underlying anchor happens to be math."""
        doc = _write_classifier_docx(tmp_path)
        packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "EQ0000001"}, "some replacement prose",
        )
        assert packet["status"] == "refused"
        assert packet["effective_element_type"] == "equation"
        result = docs_intel.classify_edit_packet(packet)
        assert result["boundary"] == docs_intel.CLASSIFIER_BOUNDARY_REJECTED
        assert result["reason"] == docs_intel.CLASSIFIER_REASON_PROSE_INVALID

    def test_prose_packet_carrying_omml_payload_is_mixed_signal_rejected(self, tmp_path):
        doc = _write_classifier_docx(tmp_path)
        packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "P0000001"}, "New prose text.",
        )
        assert packet["status"] == "built"
        packet["omml_payload"] = _VALID_OMML_PAYLOAD
        result = docs_intel.classify_edit_packet(packet)
        assert result["boundary"] == docs_intel.CLASSIFIER_BOUNDARY_REJECTED
        assert result["reason"] == docs_intel.CLASSIFIER_REASON_MIXED_SIGNALS

    # -- OMML side: validates via the EXISTING hardening primitives ------

    def test_valid_omml_payload_with_no_document_classifies_as_omml(self):
        result = docs_intel.classify_edit_packet({"omml_payload": _VALID_OMML_PAYLOAD})
        assert result == {"boundary": docs_intel.CLASSIFIER_BOUNDARY_OMML, "reason": None}

    def test_raw_omml_alias_field_is_also_recognized(self):
        result = docs_intel.classify_edit_packet({"raw_omml": _VALID_OMML_PAYLOAD})
        assert result["boundary"] == docs_intel.CLASSIFIER_BOUNDARY_OMML

    def test_missing_omml_payload_value_is_rejected(self):
        result = docs_intel.classify_edit_packet({"omml_payload": ""})
        assert result["boundary"] == docs_intel.CLASSIFIER_BOUNDARY_REJECTED
        assert result["reason"] == docs_intel.CLASSIFIER_REASON_OMML_PAYLOAD_MISSING

    def test_non_string_omml_payload_value_is_rejected(self):
        result = docs_intel.classify_edit_packet({"omml_payload": 12345})
        assert result["boundary"] == docs_intel.CLASSIFIER_BOUNDARY_REJECTED
        assert result["reason"] == docs_intel.CLASSIFIER_REASON_OMML_PAYLOAD_MISSING

    def test_structurally_malformed_omml_payload_is_rejected(self):
        """Reuses the SAME malformed-fraction shape
        test_validator_rejects_malformed_fraction_and_flattened_fallback
        exercises directly against _validate_omml_structure -- the
        classifier must not reimplement or relax that validator's own
        judgment, only compose it."""
        result = docs_intel.classify_edit_packet({"omml_payload": _MALFORMED_OMML_PAYLOAD})
        assert result["boundary"] == docs_intel.CLASSIFIER_BOUNDARY_REJECTED
        assert result["reason"] == docs_intel.CLASSIFIER_REASON_OMML_STRUCTURE_INVALID
        assert "m:num" in result["detail"]

    def test_omml_targeting_anchor_with_existing_integrity_finding_is_rejected(self, tmp_path):
        """EQDUP0001 already carries a plaintext_math_duplicate finding
        (its own <w:t> text duplicates its <m:oMath> flattened text) -- a
        new OMML operation aimed at that same anchor must fail closed
        rather than silently proceed against a known-suspect anchor."""
        doc = _write_classifier_docx(tmp_path)
        integrity = docs_intel.audit_equation_integrity(doc)
        assert integrity["finding_count"] >= 1
        assert any(f["anchor"] == "EQDUP0001" for f in integrity["findings"])

        packet = {"omml_payload": _VALID_OMML_PAYLOAD, "target_para_id": "EQDUP0001"}
        result = docs_intel.classify_edit_packet(packet, document_path=doc)
        assert result["boundary"] == docs_intel.CLASSIFIER_BOUNDARY_REJECTED
        assert result["reason"] == docs_intel.CLASSIFIER_REASON_OMML_TARGET_HAS_INTEGRITY_FINDING
        assert result["findings"]

    def test_omml_targeting_clean_anchor_via_anchor_query_classifies_as_omml(self, tmp_path):
        """A genuinely clean anchor (no integrity findings, no style
        findings -- EQCLEAN0001 is centered with compliant trailing
        punctuation) is accepted even when the target is named via
        anchor_query, the same vocabulary prose packets already use -- no
        new anchor field is invented for the OMML side."""
        doc = _write_classifier_docx(tmp_path)
        packet = {
            "omml_payload": _VALID_OMML_PAYLOAD,
            "anchor_query": {"para_id": "EQCLEAN0001"},
        }
        result = docs_intel.classify_edit_packet(packet, document_path=doc)
        assert result["boundary"] == docs_intel.CLASSIFIER_BOUNDARY_OMML
        assert result["style_findings"] == []

    def test_omml_style_findings_are_informational_not_blocking(self, tmp_path):
        """A pre-existing style finding (missing trailing punctuation) on
        the target anchor is surfaced but never causes a reject -- fixing
        that exact finding may be the whole point of the caller's edit."""
        doc = _write_classifier_docx(tmp_path)
        style_audit = docs_intel.audit_equation_style(doc)
        assert any(
            f["para_id"] == "EQ0000001" and f["type"] == "missing_trailing_punctuation"
            for f in style_audit["findings"]
        )
        packet = {"omml_payload": _VALID_OMML_PAYLOAD, "target_para_id": "EQ0000001"}
        result = docs_intel.classify_edit_packet(packet, document_path=doc)
        assert result["boundary"] == docs_intel.CLASSIFIER_BOUNDARY_OMML
        assert any(f["type"] == "missing_trailing_punctuation" for f in result["style_findings"])

    # -- round 2 (e03b41ef verifier fix): audit-error fail-closed --------

    def test_omml_target_with_bad_document_path_is_rejected_not_accepted(self):
        """Verifier repro (round 2, e03b41ef): a nonexistent document_path
        makes audit_equation_integrity itself return {"error": ...} --
        that must reject with a dedicated "audit unavailable" reason, NOT
        silently fall through to a clean "omml" acceptance. Before the
        fix this returned {"boundary": "omml", "reason": None,
        "style_findings": []}, which is exactly the "editing known-corrupt
        (or entirely unverifiable) state blind" outcome this classifier is
        documented to refuse."""
        bad_path = "Z:/this/path/does/not/exist/nowhere.docx"
        packet = {"omml_payload": _VALID_OMML_PAYLOAD, "target_para_id": "EQDUP0001"}

        integrity = docs_intel.audit_equation_integrity(bad_path)
        assert "error" in integrity, "fixture invariant: bad path must make the underlying audit itself fail"

        result = docs_intel.classify_edit_packet(packet, document_path=bad_path)
        assert result["boundary"] == docs_intel.CLASSIFIER_BOUNDARY_REJECTED
        assert result["reason"] == docs_intel.CLASSIFIER_REASON_OMML_INTEGRITY_AUDIT_UNAVAILABLE
        assert result["reason"] != docs_intel.CLASSIFIER_REASON_OMML_TARGET_HAS_INTEGRITY_FINDING
        assert "detail" in result and bad_path in result["detail"]
        assert result["audit_error"] == integrity["error"]
        # No "omml" acceptance leaks through -- style_findings must never be
        # attached to a rejected result.
        assert "style_findings" not in result

    def test_omml_target_with_bad_document_path_via_anchor_query_is_also_rejected(self):
        """Same fail-closed behavior when the target anchor is named via
        anchor_query (the prose-shared vocabulary) instead of
        target_para_id directly -- the audit-unavailable check must not be
        specific to one anchor-naming path."""
        bad_path = "Z:/another/missing/document.docx"
        packet = {
            "omml_payload": _VALID_OMML_PAYLOAD,
            "anchor_query": {"para_id": "EQCLEAN0001"},
        }
        result = docs_intel.classify_edit_packet(packet, document_path=bad_path)
        assert result["boundary"] == docs_intel.CLASSIFIER_BOUNDARY_REJECTED
        assert result["reason"] == docs_intel.CLASSIFIER_REASON_OMML_INTEGRITY_AUDIT_UNAVAILABLE

    def test_omml_style_audit_error_also_rejects_after_integrity_passes(self, tmp_path, monkeypatch):
        """The integrity audit can succeed while the STYLE audit itself
        errors (e.g. a transient failure specific to that pass) -- that
        must also reject via the same audit-unavailable reason rather than
        falling through to acceptance once the first audit clears."""
        doc = _write_classifier_docx(tmp_path)
        integrity = docs_intel.audit_equation_integrity(doc)
        assert integrity["finding_count"] >= 1
        # EQCLEAN0001 carries no integrity finding -- confirm the real
        # audit_equation_integrity run for this doc/anchor pair would clear,
        # so the rejection below is attributable to the (monkeypatched)
        # style-audit failure alone, not a real integrity finding.
        assert not any(
            f.get("anchor") == "EQCLEAN0001" or "EQCLEAN0001" in (f.get("anchors") or [])
            for f in integrity["findings"]
        )

        monkeypatch.setattr(
            docs_intel, "audit_equation_style", lambda _path: {"error": "simulated style-audit failure"},
        )
        packet = {"omml_payload": _VALID_OMML_PAYLOAD, "target_para_id": "EQCLEAN0001"}
        result = docs_intel.classify_edit_packet(packet, document_path=doc)
        assert result["boundary"] == docs_intel.CLASSIFIER_BOUNDARY_REJECTED
        assert result["reason"] == docs_intel.CLASSIFIER_REASON_OMML_INTEGRITY_AUDIT_UNAVAILABLE
        assert result["audit_error"] == "simulated style-audit failure"

    def test_batch_audit_unavailable_does_not_mask_or_get_masked_by_other_results(self, tmp_path):
        """Fail-closed audit-unavailable rejection composes correctly inside
        classify_edit_packet_batch too -- one packet's audit failure must
        report independently, same invariant as the existing mixed-batch
        tests below for the other rejection reasons."""
        doc = _write_classifier_docx(tmp_path)
        clean_omml_packet = {
            "omml_payload": _VALID_OMML_PAYLOAD,
            "target_para_id": "EQCLEAN0001",
        }
        bad_path_packet = {
            "omml_payload": _VALID_OMML_PAYLOAD,
            "target_para_id": "EQCLEAN0001",
        }

        # classify_edit_packet_batch takes one document_path for the whole
        # call, so both otherwise-identical packets are classified against
        # the SAME bad path here -- confirming the audit-unavailable
        # rejection applies independently and consistently per packet, with
        # neither one slipping through to a clean "omml" acceptance.
        results = docs_intel.classify_edit_packet_batch(
            [clean_omml_packet, bad_path_packet], document_path="Z:/nope/nope.docx",
        )
        assert len(results) == 2
        for entry in results:
            assert entry["boundary"] == docs_intel.CLASSIFIER_BOUNDARY_REJECTED
            assert entry["reason"] == docs_intel.CLASSIFIER_REASON_OMML_INTEGRITY_AUDIT_UNAVAILABLE

    # -- fail-closed mixed batch ------------------------------------------

    def test_batch_never_lets_prose_success_mask_omml_rejection(self, tmp_path):
        doc = _write_classifier_docx(tmp_path)
        prose_packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "P0000001"}, "New prose text.",
        )
        assert prose_packet["status"] == "built"
        omml_packet = {"omml_payload": _VALID_OMML_PAYLOAD, "target_para_id": "EQDUP0001"}

        results = docs_intel.classify_edit_packet_batch(
            [prose_packet, omml_packet], document_path=doc,
        )
        assert len(results) == 2
        assert results[0]["index"] == 0
        assert results[0]["boundary"] == docs_intel.CLASSIFIER_BOUNDARY_PROSE
        assert results[1]["index"] == 1
        assert results[1]["boundary"] == docs_intel.CLASSIFIER_BOUNDARY_REJECTED
        assert results[1]["reason"] == docs_intel.CLASSIFIER_REASON_OMML_TARGET_HAS_INTEGRITY_FINDING

    def test_batch_mixed_signal_packet_is_rejected_independently(self, tmp_path):
        doc = _write_classifier_docx(tmp_path)
        prose_packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "P0000001"}, "New prose text.",
        )
        assert prose_packet["status"] == "built"
        mixed_packet = dict(prose_packet)
        mixed_packet["omml_payload"] = _VALID_OMML_PAYLOAD

        results = docs_intel.classify_edit_packet_batch([prose_packet, mixed_packet])
        assert results[0]["boundary"] == docs_intel.CLASSIFIER_BOUNDARY_PROSE
        assert results[1]["boundary"] == docs_intel.CLASSIFIER_BOUNDARY_REJECTED
        assert results[1]["reason"] == docs_intel.CLASSIFIER_REASON_MIXED_SIGNALS
