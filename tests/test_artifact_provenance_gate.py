"""Coverage for the artifact-provenance fail-closed gate (sprint item
6d02f343 -- bind figure/table/equation artifacts to per-file provenance and
fail closed on mismatched replacements).

``meridian.doc_store`` never imports ``meridian_outputs`` itself (a
separate, optionally-installed extension) -- callers compute the
provenance-binding verdict themselves (e.g. via
``meridian_outputs.provenance.bind_artifact_provenance``) and hand the
resulting plain dict in as ``artifact_provenance``. These tests exercise the
consuming side of that contract:

* ``_check_artifact_provenance`` -- the pure gate: None is a no-op, a
  malformed verdict dict fails closed, an all_clear verdict is a no-op, a
  not-all_clear verdict raises with the rejected bindings attached.
* ``_write_docx_transaction`` -- an artifact_provenance failure rejects the
  transaction BEFORE promotion (destination stays byte-for-byte untouched,
  no ``.bak`` written, no leaked staged temp file), exactly like an existing
  structural-count mismatch; a clean verdict promotes normally.
* ``_save_docx_xml`` -- the same gate reachable through the real public
  entrypoint every docx-mutating tool in this codebase routes through.
* Backward compatibility -- omitting ``artifact_provenance`` (every call
  site that predates this item) is unaffected.
"""
from __future__ import annotations

import io
import os
import zipfile

import pytest
from lxml import etree as LET

from meridian import doc_store


_DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="AAAA0001">
      <w:r><w:t>Original text.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""

_CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""

_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""


def _write_docx(path: str) -> str:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _RELS_XML)
        zf.writestr("word/document.xml", _DOCUMENT_XML)
    return path


def _read_document_xml(path: str) -> bytes:
    with zipfile.ZipFile(path) as zf:
        return zf.read(doc_store._DOCX_DOCUMENT_PART)


def _build_transaction_payload(docx_path: str, new_text: str):
    raw, root = doc_store._load_docx_xml(docx_path)
    # Locate the single paragraph's text run and edit it directly via lxml,
    # independent of any higher-level paragraph-editing helper.
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    t_el = root.find(".//w:t", ns)
    t_el.text = new_text
    new_document = LET.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in src.infolist():
                data = src.read(info.filename)
                if info.filename == doc_store._DOCX_DOCUMENT_PART:
                    data = new_document
                dst.writestr(info, data)
    return raw, out.getvalue(), {doc_store._DOCX_DOCUMENT_PART: new_document}


_ALL_CLEAR = {
    "bindings": [{"artifact_id": "fig-1", "kind": "figure", "status": "resolved"}],
    "counts": {"resolved": 1, "orphaned": 0, "hash_mismatch": 0, "unresolved": 0},
    "all_clear": True,
}

_ORPHANED = {
    "bindings": [
        {"artifact_id": "fig-1", "kind": "figure", "status": "resolved"},
        {
            "artifact_id": "fig-2", "kind": "figure", "status": "orphaned",
            "reason": "canonical_path is not resolvable",
        },
    ],
    "counts": {"resolved": 1, "orphaned": 1, "hash_mismatch": 0, "unresolved": 0},
    "all_clear": False,
}


# ---------------------------------------------------------------------------
# _check_artifact_provenance -- the pure gate
# ---------------------------------------------------------------------------

class TestCheckArtifactProvenance:
    def test_none_is_a_no_op(self) -> None:
        doc_store._check_artifact_provenance(None)  # must not raise

    def test_all_clear_verdict_is_a_no_op(self) -> None:
        doc_store._check_artifact_provenance(_ALL_CLEAR)  # must not raise

    def test_not_all_clear_verdict_raises(self) -> None:
        with pytest.raises(doc_store.DocxWriteVerificationError) as excinfo:
            doc_store._check_artifact_provenance(_ORPHANED)
        rejected = excinfo.value.manifest["rejected_bindings"]
        assert len(rejected) == 1
        assert rejected[0]["artifact_id"] == "fig-2"
        assert rejected[0]["status"] == "orphaned"

    def test_malformed_verdict_fails_closed(self) -> None:
        with pytest.raises(doc_store.DocxWriteVerificationError):
            doc_store._check_artifact_provenance({"bindings": []})  # no all_clear key

    def test_non_dict_verdict_fails_closed(self) -> None:
        with pytest.raises(doc_store.DocxWriteVerificationError):
            doc_store._check_artifact_provenance("not-a-dict")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _write_docx_transaction -- artifact_provenance gates promotion
# ---------------------------------------------------------------------------

class TestWriteDocxTransactionArtifactProvenanceGate:
    def test_not_all_clear_rejects_before_promotion(self, tmp_path) -> None:
        docx = _write_docx(str(tmp_path / "d.docx"))
        original_bytes = open(docx, "rb").read()
        raw, payload, changed_parts = _build_transaction_payload(docx, "Edited.")
        pre_manifest = doc_store._docx_structural_manifest(raw)

        with pytest.raises(doc_store.DocxWriteVerificationError) as excinfo:
            doc_store._write_docx_transaction(
                payload, docx,
                pre_manifest=pre_manifest,
                protected_keys=("media_count", "style_count", "relationship_count"),
                changed_parts=changed_parts,
                artifact_provenance=_ORPHANED,
            )
        assert "artifact_provenance" in excinfo.value.manifest

        # dest is byte-for-byte untouched -- the structural check passed
        # (the payload is well-formed) but promotion never happened because
        # the provenance gate rejected it first.
        assert open(docx, "rb").read() == original_bytes
        assert not os.path.exists(docx + ".bak")
        leftovers = [n for n in os.listdir(tmp_path) if ".meridian-docx-stage-" in n]
        assert leftovers == []

    def test_all_clear_promotes_normally(self, tmp_path) -> None:
        docx = _write_docx(str(tmp_path / "d.docx"))
        raw, payload, changed_parts = _build_transaction_payload(docx, "Edited cleanly.")
        pre_manifest = doc_store._docx_structural_manifest(raw)

        txn = doc_store._write_docx_transaction(
            payload, docx,
            pre_manifest=pre_manifest,
            protected_keys=("media_count", "style_count", "relationship_count"),
            changed_parts=changed_parts,
            artifact_provenance=_ALL_CLEAR,
        )
        assert txn["pre_counts"] == pre_manifest
        assert b"Edited cleanly." in _read_document_xml(docx)

    def test_omitted_artifact_provenance_is_unaffected(self, tmp_path) -> None:
        """Every call site that predates this item never passes
        artifact_provenance at all -- confirms zero behavior change."""
        docx = _write_docx(str(tmp_path / "d.docx"))
        raw, payload, changed_parts = _build_transaction_payload(docx, "No gate at all.")
        pre_manifest = doc_store._docx_structural_manifest(raw)

        txn = doc_store._write_docx_transaction(
            payload, docx,
            pre_manifest=pre_manifest,
            protected_keys=("media_count", "style_count", "relationship_count"),
            changed_parts=changed_parts,
        )
        assert b"No gate at all." in _read_document_xml(docx)
        assert txn["pre_counts"] == pre_manifest


# ---------------------------------------------------------------------------
# _save_docx_xml -- the real public entrypoint
# ---------------------------------------------------------------------------

class TestSaveDocxXmlArtifactProvenanceGate:
    def test_not_all_clear_rejects_before_promotion(self, tmp_path) -> None:
        docx = _write_docx(str(tmp_path / "d.docx"))
        original_bytes = open(docx, "rb").read()
        raw, root = doc_store._load_docx_xml(docx)
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        root.find(".//w:t", ns).text = "Should never land."

        with pytest.raises(doc_store.DocxWriteVerificationError):
            doc_store._save_docx_xml(
                raw, root, docx, artifact_provenance=_ORPHANED,
            )
        assert open(docx, "rb").read() == original_bytes

    def test_all_clear_promotes_normally(self, tmp_path) -> None:
        docx = _write_docx(str(tmp_path / "d.docx"))
        raw, root = doc_store._load_docx_xml(docx)
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        root.find(".//w:t", ns).text = "Should land."

        doc_store._save_docx_xml(raw, root, docx, artifact_provenance=_ALL_CLEAR)
        assert b"Should land." in _read_document_xml(docx)
