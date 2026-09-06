"""Tests for docs_intel's typed prose-edit packet contract (4c992e91,
BE4ED581-W1: "define reviewable prose-edit packets with anchor
re-resolution and drift rejection").

Covers:
  - build_prose_edit_packet: read-only, never mutates document_path;
    returns the documented field set on success; refuses (no packet built)
    on a malformed anchor_query/replacement_text, an anchor that does not
    resolve, and -- the single most important case -- an EQUATION anchor
    (never falls back to plain text for math).
  - apply_prose_edit_packets: re-resolves every packet fresh against the
    CURRENT document, applies to an isolated draft, all-or-nothing;
    rejects with two DISTINGUISHABLE drift reasons (context_hash_mismatch
    vs. base_docx_hash_mismatch), rejects a vanished/ambiguous anchor, and
    a mixed-packet regression asserting a prose packet can never touch
    <m:oMath> content.
"""
from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest

from meridian_docs import docs_intel


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"

_SIMPLE_OMATH = (
    f'<m:oMath xmlns:m="{_M}">'
    "<m:r><m:t>E</m:t></m:r><m:r><m:t>=</m:t></m:r><m:r><m:t>mc2</m:t></m:r>"
    "</m:oMath>"
)

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
    <w:p w14:paraId="EQ0000001">
      {_SIMPLE_OMATH}
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
    <w:p w14:paraId="P0000002">
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
# build_prose_edit_packet
# ---------------------------------------------------------------------------

class TestBuildProseEditPacket:
    def test_build_succeeds_for_plain_paragraph(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        before = _read_bytes(doc)
        packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "P0000001"}, "Replacement text.",
        )
        assert packet["packet_kind"] == "prose_edit"
        assert packet["status"] == "built"
        assert packet["target_para_id"] == "P0000001"
        assert packet["element_type"] == "paragraph"
        assert packet["replacement_text"] == "Replacement text."
        assert packet["anchor_query"] == {"para_id": "P0000001"}
        assert isinstance(packet["expected_context_hash"], str) and packet["expected_context_hash"]
        assert isinstance(packet["base_docx_hash"], str) and packet["base_docx_hash"]
        assert packet["base_docx_hash"] == packet["built_at_source_fingerprint"]
        assert packet["base_docx_hash"] == docs_intel._source_fingerprint(before)
        # Read-only: document_path must be byte-for-byte unchanged.
        assert _read_bytes(doc) == before

    def test_build_succeeds_for_heading(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "H0000001"}, "New Introduction Title",
        )
        assert packet["status"] == "built"
        assert packet["element_type"] == "heading"

    def test_expected_context_hash_matches_live_text(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "P0000001"}, "New text.",
        )
        live = docs_intel.locate_anchor(doc, {"para_id": "P0000001"})
        assert packet["expected_context_hash"] == docs_intel._prose_context_hash(
            live["quoted_text"]
        )
        # The hash must not simply be the raw text -- it should be a sha256
        # hex digest (64 lowercase hex chars), not the paragraph's own text.
        assert packet["expected_context_hash"] != live["quoted_text"]
        assert len(packet["expected_context_hash"]) == 64

    def test_section_role_and_provenance_are_carried_and_hashed(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "P0000001"}, "New text.",
            section_role="main",
            source_provenance={"origin": "human_edit", "author": "a.camerer"},
        )
        assert packet["section_role"] == "main"
        assert isinstance(packet["source_provenance_hash"], str)
        assert len(packet["source_provenance_hash"]) == 64
        # Deterministic: rebuilding with the same provenance dict (even with
        # different key order) reproduces the identical hash.
        packet2 = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "P0000001"}, "New text.",
            source_provenance={"author": "a.camerer", "origin": "human_edit"},
        )
        assert packet2["source_provenance_hash"] == packet["source_provenance_hash"]

    def test_no_provenance_is_none(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "P0000001"}, "New text.",
        )
        assert packet["source_provenance_hash"] is None

    def test_build_refuses_replacement_text_not_a_string(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "P0000001"}, 12345,  # type: ignore[arg-type]
        )
        assert packet["status"] == "refused"
        assert "replacement_text" in packet["reason"]

    def test_build_refuses_empty_anchor_query(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        packet = docs_intel.build_prose_edit_packet(doc, {}, "New text.")
        assert packet["status"] == "refused"
        assert "anchor_query" in packet["reason"]

    def test_build_refuses_unresolvable_anchor(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "NOPE"}, "New text.",
        )
        assert packet["status"] == "refused"
        assert packet["anchor"]["status"] == "not_found"

    def test_build_refuses_equation_anchor(self, tmp_path) -> None:
        """The single most important new test per this item's own
        discovery notes: a prose-edit packet must NEVER be built against
        an equation -- this is what stops _set_paragraph_text from ever
        being reachable against <m:oMath> content.

        Note: _resolve_anchor_query's own raw classification reports this
        anchor's element_type as "paragraph" (empty text), not "equation"
        -- see _prose_effective_element_type's docstring for why. The
        refusal must therefore be keyed on the reported
        effective_element_type ("equation"), not the anchor's raw
        element_type field.
        """
        doc = _write_docx(tmp_path)
        packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "EQ0000001"}, "some replacement prose",
        )
        assert packet["status"] == "refused"
        assert "unsupported_element_type" in packet["reason"]
        assert packet["effective_element_type"] == "equation"

    def test_build_refuses_table_caption_anchor(self, tmp_path) -> None:
        """Deliberately narrower than the generic batch layer: a caption is
        not "prose" in this item's sense, even though plan_batch_transform
        would happily mutate it."""
        doc = _write_docx(tmp_path)
        packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "F0000001"}, "New caption text.",
        )
        assert packet["status"] == "refused"
        assert "unsupported_element_type" in packet["reason"]
        assert packet["anchor"]["element_type"] == "figure_caption"

    def test_build_refuses_table_anchor(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        # tbl4: the <w:tbl> is the 5th body child (index 4, 0-based) --
        # H0000001(0), P0000001(1), EQ0000001(2), F0000001(3), <w:tbl>(4).
        packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "tbl4"}, "New table text.",
        )
        assert packet["status"] == "refused"
        assert "unsupported_element_type" in packet["reason"]
        assert packet["effective_element_type"] == "table"

    def test_expected_source_fingerprint_mismatch_refuses(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "P0000001"}, "New text.",
            expected_source_fingerprint="deadbeef" * 8,
        )
        assert packet["status"] == "refused"
        assert packet["anchor"]["status"] == "stale"


# ---------------------------------------------------------------------------
# apply_prose_edit_packets
# ---------------------------------------------------------------------------

class TestApplyProseEditPackets:
    def test_apply_succeeds_when_nothing_has_drifted(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        before = _read_bytes(doc)
        packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "P0000001"}, "Replaced text.",
        )
        draft = str(tmp_path / "draft.docx")
        result = docs_intel.apply_prose_edit_packets(doc, [packet], draft)
        assert result["applied"] is True
        assert result["draft_output_path"] == draft
        assert result["applied_packets"] == [
            {"index": 0, "target_para_id": "P0000001", "section_role": None},
        ]
        # Source document is untouched, byte-for-byte.
        assert _read_bytes(doc) == before
        # The draft actually carries the new text.
        assert "Replaced text." in _document_xml_text(draft)
        assert "Plain paragraph one." not in _document_xml_text(draft)

    def test_apply_carries_section_role_through(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "P0000001"}, "Replaced text.", section_role="main",
        )
        draft = str(tmp_path / "draft.docx")
        result = docs_intel.apply_prose_edit_packets(doc, [packet], draft)
        assert result["applied"] is True
        assert result["applied_packets"][0]["section_role"] == "main"

    def test_apply_rejects_on_anchor_content_drift(self, tmp_path) -> None:
        """The anchor's OWN text changed since the packet was built ->
        context_hash_mismatch, distinguishable from a whole-document-only
        change."""
        doc = _write_docx(tmp_path)
        packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "P0000001"}, "Replaced text.",
        )
        # Mutate the live document's P0000001 text out from under the packet.
        mutated_xml = _DOC_XML.replace(
            "Plain paragraph one.", "Plain paragraph one -- EDITED.",
        )
        with open(doc, "wb") as fh:
            fh.write(_make_docx_bytes(mutated_xml))

        draft = str(tmp_path / "draft.docx")
        result = docs_intel.apply_prose_edit_packets(doc, [packet], draft)
        assert result["applied"] is False
        assert result["reason"] == "batch_has_conflicts"
        assert result["conflicts"][0]["reason"] == "context_hash_mismatch"
        # No draft written at all.
        import os
        assert not os.path.exists(draft)

    def test_apply_rejects_on_whole_document_drift_elsewhere(self, tmp_path) -> None:
        """A DIFFERENT anchor changed; this packet's own anchor text is
        untouched -> base_docx_hash_mismatch, distinct from
        context_hash_mismatch."""
        doc = _write_docx(tmp_path)
        packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "P0000001"}, "Replaced text.",
        )
        # Mutate a DIFFERENT paragraph -- P0000001's own text is unchanged.
        mutated_xml = _DOC_XML.replace(
            "Closing paragraph.", "Closing paragraph -- EDITED.",
        )
        with open(doc, "wb") as fh:
            fh.write(_make_docx_bytes(mutated_xml))

        draft = str(tmp_path / "draft.docx")
        result = docs_intel.apply_prose_edit_packets(doc, [packet], draft)
        assert result["applied"] is False
        assert result["conflicts"][0]["reason"] == "base_docx_hash_mismatch"

    def test_apply_rejects_when_anchor_deleted(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "P0000001"}, "Replaced text.",
        )
        mutated_xml = _DOC_XML.replace(
            '<w:p w14:paraId="P0000001">\n      <w:r><w:t>Plain paragraph one.</w:t></w:r>\n    </w:p>',
            "",
        )
        assert mutated_xml != _DOC_XML  # sanity: the replace actually matched
        with open(doc, "wb") as fh:
            fh.write(_make_docx_bytes(mutated_xml))

        draft = str(tmp_path / "draft.docx")
        result = docs_intel.apply_prose_edit_packets(doc, [packet], draft)
        assert result["applied"] is False
        assert result["conflicts"][0]["reason"] == "anchor_unresolved"

    def test_apply_rejects_ambiguous_anchor(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        packet = docs_intel.build_prose_edit_packet(
            doc, {"text": "paragraph"}, "Replaced text.",
        )
        # {"text": "paragraph"} matches BOTH "Plain paragraph one." and
        # "Closing paragraph." at build time -> refused as ambiguous, so
        # there is no built packet to apply in the first place.
        assert packet["status"] == "refused"
        assert packet["anchor"]["status"] == "ambiguous"

    def test_multi_packet_batch_one_stale_packet_blocks_the_whole_apply(
        self, tmp_path
    ) -> None:
        """One packet's anchor (P0000001) is edited directly -- its own
        context_hash mismatches. Because base_docx_hash is a whole-FILE
        hash, this SAME edit also changes the file bytes the OTHER
        (otherwise-untouched) packet was built against, so it fails its
        own base_docx_hash check too -- correctly distinguishable from the
        first packet's more specific context_hash_mismatch. All-or-nothing:
        neither packet's change is written."""
        doc = _write_docx(tmp_path)
        untouched_packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "H0000001"}, "New Introduction",
        )
        stale_packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "P0000001"}, "Replaced text.",
        )
        # Drift only the second anchor's own text.
        mutated_xml = _DOC_XML.replace(
            "Plain paragraph one.", "Plain paragraph one -- EDITED.",
        )
        with open(doc, "wb") as fh:
            fh.write(_make_docx_bytes(mutated_xml))

        draft = str(tmp_path / "draft.docx")
        result = docs_intel.apply_prose_edit_packets(
            doc, [untouched_packet, stale_packet], draft,
        )
        assert result["applied"] is False
        assert result["ready_count"] == 0
        by_index = {c["index"]: c["reason"] for c in result["conflicts"]}
        assert by_index[0] == "base_docx_hash_mismatch"
        assert by_index[1] == "context_hash_mismatch"
        # All-or-nothing: neither packet's change is written.
        import os
        assert not os.path.exists(draft)

    def test_one_genuinely_ready_packet_still_blocked_by_a_sibling_failure(
        self, tmp_path
    ) -> None:
        """Document is untouched (no drift at all) -- one packet is a
        completely valid, ready-to-apply prose edit; the other is
        structurally invalid. All-or-nothing still blocks the valid one."""
        doc = _write_docx(tmp_path)
        good_packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "H0000001"}, "New Introduction",
        )
        assert good_packet["status"] == "built"
        draft = str(tmp_path / "draft.docx")
        result = docs_intel.apply_prose_edit_packets(
            doc, [good_packet, {"packet_kind": "not_prose_edit"}], draft,
        )
        assert result["applied"] is False
        assert result["ready_count"] == 1
        assert result["conflicts"][0]["index"] == 1
        assert result["conflicts"][0]["reason"] == "invalid_packet"
        import os
        assert not os.path.exists(draft)

    def test_duplicate_target_in_one_batch_is_a_conflict(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        p1 = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "P0000001"}, "First replacement.",
        )
        p2 = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "P0000001"}, "Second replacement.",
        )
        draft = str(tmp_path / "draft.docx")
        result = docs_intel.apply_prose_edit_packets(doc, [p1, p2], draft)
        assert result["applied"] is False
        assert result["conflicts"][0]["reason"] == "duplicate_target_in_batch"

    def test_apply_rejects_invalid_packet_shape(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        draft = str(tmp_path / "draft.docx")
        result = docs_intel.apply_prose_edit_packets(
            doc, [{"packet_kind": "prose_edit", "status": "built"}], draft,
        )
        assert result["applied"] is False
        assert result["conflicts"][0]["reason"] == "invalid_packet"

    def test_apply_rejects_refused_packet(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        refused = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "EQ0000001"}, "some text",
        )
        assert refused["status"] == "refused"
        assert refused["effective_element_type"] == "equation"
        draft = str(tmp_path / "draft.docx")
        result = docs_intel.apply_prose_edit_packets(doc, [refused], draft)
        assert result["applied"] is False
        assert result["conflicts"][0]["reason"] == "invalid_packet"

    def test_draft_output_path_must_differ_from_document_path(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "P0000001"}, "Replaced text.",
        )
        result = docs_intel.apply_prose_edit_packets(doc, [packet], doc)
        assert result["applied"] is False
        assert "error" in result

    def test_empty_packets_list_is_an_error(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        result = docs_intel.apply_prose_edit_packets(
            doc, [], str(tmp_path / "draft.docx"),
        )
        assert result["applied"] is False
        assert "error" in result

    def test_expected_source_fingerprint_mismatch_short_circuits(self, tmp_path) -> None:
        doc = _write_docx(tmp_path)
        packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "P0000001"}, "Replaced text.",
        )
        draft = str(tmp_path / "draft.docx")
        result = docs_intel.apply_prose_edit_packets(
            doc, [packet], draft, expected_source_fingerprint="deadbeef" * 8,
        )
        assert result["applied"] is False
        assert result["reason"] == "document_changed_before_apply"
        import os
        assert not os.path.exists(draft)


# ---------------------------------------------------------------------------
# Regression: a prose packet must never touch OMML, complementing (not
# duplicating) test_omml_contract_semantics.py.
# ---------------------------------------------------------------------------

class TestProsePacketNeverTouchesOmml:
    def test_mixed_batch_equation_packet_cannot_be_built_at_all(self, tmp_path) -> None:
        """Even inside a batch that also contains valid prose packets, the
        equation anchor is refused at BUILD time -- there is never a path
        from build_prose_edit_packet to an applied equation mutation."""
        doc = _write_docx(tmp_path)
        prose_packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "P0000001"}, "New prose.",
        )
        equation_packet = docs_intel.build_prose_edit_packet(
            doc, {"para_id": "EQ0000001"}, "E = mc^2 as plain text",
        )
        assert prose_packet["status"] == "built"
        assert equation_packet["status"] == "refused"
        assert equation_packet["effective_element_type"] == "equation"

    def test_hand_assembled_equation_packet_is_refused_at_apply_time_too(
        self, tmp_path
    ) -> None:
        """Defense in depth: even if a caller hand-assembles a packet dict
        that CLAIMS to target the equation paragraph (bypassing
        build_prose_edit_packet's own gate), apply_prose_edit_packets
        re-resolves the anchor fresh and refuses it again before ever
        calling the plain-text writer -- <m:oMath> content must survive
        untouched."""
        doc = _write_docx(tmp_path)
        before_xml = _document_xml_text(doc)
        forged_packet = {
            "packet_kind": "prose_edit",
            "status": "built",
            "document_path": doc,
            "target_para_id": "EQ0000001",
            "anchor_query": {"para_id": "EQ0000001"},
            "element_type": "paragraph",  # forged -- does not match reality
            "section_path": None,
            "section_role": None,
            "expected_context_hash": docs_intel._prose_context_hash(""),
            "replacement_text": "E = mc^2 flattened to plain text",
            "source_provenance_hash": None,
            "base_docx_hash": docs_intel._source_fingerprint(_read_bytes(doc)),
            "built_at_source_fingerprint": docs_intel._source_fingerprint(_read_bytes(doc)),
        }
        draft = str(tmp_path / "draft.docx")
        result = docs_intel.apply_prose_edit_packets(doc, [forged_packet], draft)
        assert result["applied"] is False
        assert result["conflicts"][0]["reason"] == "unsupported_element_type"
        assert result["conflicts"][0]["effective_element_type"] == "equation"
        # No draft was ever written, and the source document's OMML is
        # completely untouched.
        assert _document_xml_text(doc) == before_xml
        import os
        assert not os.path.exists(draft)
