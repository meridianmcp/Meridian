"""Tests for the meridian-docs side of the artifact-provenance fail-closed
gate (sprint item 6d02f343 -- bind figure/table/equation artifacts to
per-file provenance and fail closed on mismatched writes).

``docs_intel.py`` never imports ``meridian_outputs`` itself (a separate,
optionally-installed extension) -- a caller computes the provenance-binding
verdict (e.g. via ``meridian_outputs.provenance.bind_artifact_provenance``)
and hands the resulting plain dict in as the new ``artifact_provenance``
kwarg. These tests exercise the consuming side of that contract:

  - ``_check_artifact_provenance_binding`` -- the pure gate.
  - ``_verify_docx_write`` -- the new kwarg is checked ONLY after every
    existing structural/hash check already passed.
  - ``relocate_figure`` -- an artifact_provenance failure fails the move
    closed (same restore-if-safe handling as any other post-write
    verification failure); a clean verdict is unaffected.
  - ``insert_caption`` -- an artifact_provenance failure fails the caption
    insert closed AFTER structural + render verification both already
    passed; a clean verdict is unaffected.
  - Backward compatibility: omitting ``artifact_provenance`` (every call
    site that predates this item) is unaffected.
"""
from __future__ import annotations

import io
import zipfile

import pytest

from meridian_docs import docs_intel


@pytest.fixture(autouse=True)
def _default_render_capability(monkeypatch):
    """insert_caption invokes the real render-capability gate AFTER
    structural verification passes. Stub a successful 'rendered' result so
    these tests exercise the artifact-provenance gate specifically, not
    whichever render backends happen to be installed on the test machine
    (same fixture pattern as test_docx_bookmark_identity.py)."""
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
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"


_SIMPLE_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}">
  <w:body>
    <w:p w14:paraId="P0000001">
      <w:r><w:t>First body paragraph.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000002">
      <w:r><w:t>Second body paragraph.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""

_FIGURE_BLOCK_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:r="{_R}" xmlns:a="{_A}">
  <w:body>
    <w:p w14:paraId="P0000001"><w:r><w:t>Intro</w:t></w:r></w:p>
    <w:p w14:paraId="IMG000001"><w:r><w:drawing><a:blip r:embed="rId7"/></w:drawing></w:r></w:p>
    <w:p w14:paraId="CAP000001"><w:fldSimple w:instr=" SEQ Figure \\* ARABIC "><w:r><w:t>1</w:t></w:r></w:fldSimple></w:p>
    <w:p w14:paraId="P0000002"><w:r><w:t>Destination</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>'''


def _write_docx(tmp_path, xml: str, name: str = "doc.docx") -> str:
    path = str(tmp_path / name)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
        if "media/image1.png" in xml or "rId7" in xml:
            archive.writestr("word/media/image1.png", b"image")
    with open(path, "wb") as fh:
        fh.write(buf.getvalue())
    return path


_ALL_CLEAR = {
    "bindings": [{"artifact_id": "fig-1", "kind": "figure", "status": "resolved"}],
    "counts": {"resolved": 1, "orphaned": 0, "hash_mismatch": 0, "unresolved": 0},
    "all_clear": True,
}

_ORPHANED = {
    "bindings": [
        {
            "artifact_id": "fig-1", "kind": "figure", "status": "orphaned",
            "reason": "canonical_path is not resolvable",
        },
    ],
    "counts": {"resolved": 0, "orphaned": 1, "hash_mismatch": 0, "unresolved": 0},
    "all_clear": False,
}


# ---------------------------------------------------------------------------
# _check_artifact_provenance_binding -- the pure gate
# ---------------------------------------------------------------------------

class TestCheckArtifactProvenanceBinding:
    def test_none_is_a_no_op(self) -> None:
        assert docs_intel._check_artifact_provenance_binding(None) is None

    def test_all_clear_verdict_is_a_no_op(self) -> None:
        assert docs_intel._check_artifact_provenance_binding(_ALL_CLEAR) is None

    def test_not_all_clear_verdict_returns_error_dict(self) -> None:
        error = docs_intel._check_artifact_provenance_binding(_ORPHANED)
        assert error is not None
        assert "error" in error
        assert len(error["artifact_provenance_mismatches"]) == 1
        assert error["artifact_provenance_mismatches"][0]["status"] == "orphaned"

    def test_malformed_verdict_fails_closed(self) -> None:
        error = docs_intel._check_artifact_provenance_binding({"bindings": []})
        assert error is not None
        assert "error" in error

    def test_non_dict_verdict_fails_closed(self) -> None:
        error = docs_intel._check_artifact_provenance_binding("not-a-dict")
        assert error is not None
        assert "error" in error


# ---------------------------------------------------------------------------
# _verify_docx_write -- checked only after structural verification passes
# ---------------------------------------------------------------------------

class TestVerifyDocxWriteArtifactProvenance:
    def _expected_counts(self, path: str) -> dict[str, int]:
        raw, root = docs_intel._load_docx_xml_stdlib(path)
        body = root.find(docs_intel._q(_W, "body"))
        counts = docs_intel._structural_counts([body])
        counts["image_count"] = docs_intel._docx_media_count(raw)
        return counts

    def test_structural_pass_then_all_clear_returns_none(self, tmp_path) -> None:
        path = _write_docx(tmp_path, _SIMPLE_XML)
        result = docs_intel._verify_docx_write(
            path,
            expected_counts=self._expected_counts(path),
            artifact_provenance=_ALL_CLEAR,
        )
        assert result is None

    def test_structural_pass_then_not_all_clear_returns_error(self, tmp_path) -> None:
        path = _write_docx(tmp_path, _SIMPLE_XML)
        result = docs_intel._verify_docx_write(
            path,
            expected_counts=self._expected_counts(path),
            artifact_provenance=_ORPHANED,
        )
        assert result is not None
        assert "error" in result
        assert result["artifact_provenance_mismatches"][0]["status"] == "orphaned"

    def test_structural_failure_reported_even_with_all_clear_provenance(
        self, tmp_path,
    ) -> None:
        """A structural mismatch must still win -- artifact_provenance is
        never consulted (let alone able to paper over) a structural
        verification failure."""
        path = _write_docx(tmp_path, _SIMPLE_XML)
        bad_counts = dict(self._expected_counts(path))
        bad_counts["image_count"] = bad_counts.get("image_count", 0) + 1
        result = docs_intel._verify_docx_write(
            path, expected_counts=bad_counts, artifact_provenance=_ALL_CLEAR,
        )
        assert result is not None
        assert "count_mismatches" in result
        assert result["count_mismatches"]

    def test_omitted_artifact_provenance_is_unaffected(self, tmp_path) -> None:
        path = _write_docx(tmp_path, _SIMPLE_XML)
        result = docs_intel._verify_docx_write(
            path, expected_counts=self._expected_counts(path),
        )
        assert result is None


# ---------------------------------------------------------------------------
# relocate_figure -- integration
# ---------------------------------------------------------------------------

class TestRelocateFigureArtifactProvenance:
    def test_not_all_clear_fails_the_move_closed(self, tmp_path) -> None:
        path = _write_docx(tmp_path, _FIGURE_BLOCK_XML, name="figure.docx")
        before = open(path, "rb").read()

        result = docs_intel.relocate_figure(
            path, 1, "P0000002", artifact_provenance=_ORPHANED,
        )
        assert "error" in result
        assert result.get("artifact_provenance_mismatches")
        # restore-if-safe: no concurrent writer exists in this test, so the
        # move must be safely rolled back -- the file is back to its
        # pre-write state.
        assert open(path, "rb").read() == before

    def test_all_clear_move_succeeds(self, tmp_path) -> None:
        path = _write_docx(tmp_path, _FIGURE_BLOCK_XML, name="figure.docx")

        result = docs_intel.relocate_figure(
            path, 1, "P0000002", artifact_provenance=_ALL_CLEAR,
        )
        assert result["status"] == "moved"

    def test_omitted_artifact_provenance_is_unaffected(self, tmp_path) -> None:
        path = _write_docx(tmp_path, _FIGURE_BLOCK_XML, name="figure.docx")
        result = docs_intel.relocate_figure(path, 1, "P0000002")
        assert result["status"] == "moved"


# ---------------------------------------------------------------------------
# insert_caption -- integration
# ---------------------------------------------------------------------------

class TestInsertCaptionArtifactProvenance:
    def test_not_all_clear_fails_the_insert_closed(self, tmp_path) -> None:
        path = _write_docx(tmp_path, _SIMPLE_XML)
        before = open(path, "rb").read()

        result = docs_intel.insert_caption(
            path, "P0000001", "Figure", "A figure.",
            artifact_provenance=_ORPHANED,
        )
        assert "error" in result
        assert result.get("artifact_provenance_mismatches")
        assert open(path, "rb").read() == before

    def test_all_clear_insert_succeeds(self, tmp_path) -> None:
        path = _write_docx(tmp_path, _SIMPLE_XML)

        result = docs_intel.insert_caption(
            path, "P0000001", "Figure", "A figure.",
            artifact_provenance=_ALL_CLEAR,
        )
        assert result["status"] == "inserted"

    def test_omitted_artifact_provenance_is_unaffected(self, tmp_path) -> None:
        path = _write_docx(tmp_path, _SIMPLE_XML)
        result = docs_intel.insert_caption(path, "P0000001", "Figure", "A figure.")
        assert result["status"] == "inserted"
