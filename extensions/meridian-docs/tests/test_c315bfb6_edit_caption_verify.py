"""Regression tests for edit_caption's post-write verification (c315bfb6, W1-B item E).

Before this fix, ``edit_caption`` wrote its mutation directly via
``_save_docx_xml_stdlib`` with NO post-write verification at all -- unlike
every sibling content-mutating writer in this module (``insert_caption``,
``insert_equation_local``, etc.), which all hold ``docx_path``'s promotion
lock across a stage -> verify -> restore-on-mismatch cycle. That meant
neither a corrupted write nor a lost concurrent update was ever caught for
caption edits.

The correct fix must verify the edit via the paragraph's STABLE
body-position index captured BEFORE the edit -- not by re-resolving the
paragraph via its content-derived synth id (``_find_para_by_id`` scheme 2),
which drifts the moment the edit changes the very label text that id is
derived from. See ``docs_intel._verify_caption_edit_write``'s docstring.
"""
from __future__ import annotations

import zipfile

from meridian_docs import docs_intel
from meridian_docs._vendored_content_tree import _build_synth_id_map


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS = f'xmlns:w="{_W}"'

# Neither paragraph carries a native w14:paraId, so the Caption paragraph
# resolves via _find_para_by_id's content-derived synth-id scheme (scheme
# 2) -- exactly the realistic case for most real-world documents (Word
# does not assign w14:paraId to every paragraph).
_DOCUMENT_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document {_NS}>
  <w:body>
    <w:p>
      <w:r><w:t>Intro paragraph.</w:t></w:r>
    </w:p>
    <w:p>
      <w:pPr><w:pStyle w:val="Caption"/></w:pPr>
      <w:r><w:t xml:space="preserve">Figure </w:t></w:r>
      <w:fldSimple w:instr="SEQ Figure \\* ARABIC">
        <w:r><w:t>1</w:t></w:r>
      </w:fldSimple>
      <w:r><w:t xml:space="preserve">. Original label</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
'''


def _write_docx(tmp_path, name="doc.docx") -> str:
    path = str(tmp_path / name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", _DOCUMENT_XML)
    return path


def _read_document_xml(path: str) -> bytes:
    with zipfile.ZipFile(path) as zf:
        return zf.read("word/document.xml")


def _caption_synth_id(path: str) -> str:
    """Resolve the caption paragraph's content-derived synth id, the same
    id a real caller without a native w14:paraId would pass to
    edit_caption."""
    _raw, root = docs_intel._load_docx_xml_stdlib(path)
    body = root.find(docs_intel._q(_W, "body"))
    _b, caption_elem, _idx = docs_intel._find_para_by_id(root, "p1")
    synth_map = _build_synth_id_map(body)
    synth_id = synth_map.get(id(caption_elem))
    assert synth_id is not None
    return synth_id


def test_edit_caption_verifies_via_stable_body_position_after_content_derived_id_drifts(tmp_path):
    """The exact bug this item fixes: the caption is located by its
    PRE-edit content-derived synth id (as a real caller would), the edit
    changes the very content that id is derived from, and post-write
    verification must still succeed by re-checking the paragraph at its
    stable body position -- not by trying to re-resolve the now-stale id.
    """
    path = _write_docx(tmp_path)
    synth_id_before = _caption_synth_id(path)

    result = docs_intel.edit_caption(path, synth_id_before, "New label")

    assert result == {
        "status": "edited",
        "caption_para_id": synth_id_before,
        "new_label_text": "New label",
        "docx_path": path,
    }
    after_xml = _read_document_xml(path)
    assert b"New label" in after_xml

    # Demonstrate the actual bug this fix avoids: re-resolving by the
    # PRE-edit content-derived id against the POST-edit document fails,
    # because the edit changed the content that id was derived from.
    # (edit_caption's own verification never does this -- it uses the
    # stable body_child_index instead -- which is exactly why the edit
    # above succeeded rather than spuriously erroring.)
    _raw2, root2 = docs_intel._load_docx_xml_stdlib(path)
    assert docs_intel._find_para_by_id(root2, synth_id_before) is None


def test_edit_caption_structural_verification_failure_restores_and_errors(tmp_path):
    """Mirrors insert_caption's own
    test_insert_caption_structural_verification_failure_restores_and_errors:
    a post-write mismatch must fail closed and restore the file, never be
    reported as edited."""
    path = _write_docx(tmp_path)
    before = _read_document_xml(path)

    monkeypatch_target = docs_intel._verify_caption_edit_write
    try:
        docs_intel._verify_caption_edit_write = (
            lambda *a, **kw: {"error": "post-write verification failed: simulated mismatch"}
        )
        result = docs_intel.edit_caption(path, "p1", "New label")
    finally:
        docs_intel._verify_caption_edit_write = monkeypatch_target

    assert "error" in result
    assert result["file_restored"] is True
    assert _read_document_xml(path) == before


def test_verify_caption_edit_write_uses_body_child_index_not_re_resolution(tmp_path):
    """Direct unit test of _verify_caption_edit_write: it must accept a
    write verified purely via body_child_index, with no dependency on
    caption_para_id still resolving post-edit."""
    path = _write_docx(tmp_path)
    _raw, root = docs_intel._load_docx_xml_stdlib(path)
    _body, _caption_elem, body_child_index = docs_intel._find_para_by_id(root, "p1")

    # A caption_para_id that resolves to NOTHING post-edit (simulating the
    # exact id-drift scenario) must not matter -- verification is keyed on
    # body_child_index alone.
    error = docs_intel._verify_caption_edit_write(
        path,
        caption_para_id="sp_this_id_no_longer_resolves_to_anything",
        expected_label_text="Original label",
        body_child_index=body_child_index,
    )
    assert error is None

    # A wrong body_child_index (pointing at the intro paragraph, not the
    # caption) correctly fails verification.
    error = docs_intel._verify_caption_edit_write(
        path,
        caption_para_id="sp_this_id_no_longer_resolves_to_anything",
        expected_label_text="Original label",
        body_child_index=0,
    )
    assert error is not None
    assert "mismatch" in error["error"]
