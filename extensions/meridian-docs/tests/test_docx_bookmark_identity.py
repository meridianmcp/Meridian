"""Tests for OOXML bookmark identity generation (5b2ce3fb).

Covers the four symbols repaired by this sprint item:
  - meridian_docs.docs_intel._build_caption_paragraph
  - meridian_docs.docs_intel._build_internal_note_paragraph
  - meridian_docs.docs_intel._next_ref_bookmark_name
  - meridian_docs.docs_intel._next_note_bookmark_name

The motivating bug: both paragraph builders hardcoded ``w:id="0"`` on every
``<w:bookmarkStart>``/``<w:bookmarkEnd>`` pair they emitted. Two captions (or
a caption and an internal note, or two internal notes) inserted into the
same document therefore produced MULTIPLE bookmarks all sharing the numeric
id ``"0"`` -- Word-invalid duplicate ``w:id`` markers that make
bookmarkStart/bookmarkEnd pairing ambiguous, since OOXML requires ``w:id``
to be unique per document (the bookmark *name* is a separate, independently
unique identifier and was never the problem).

All tests are pure Python (stdlib + pytest) -- no mcp, no network. Follows
the same conventions as test_docs_intel_new_primitives.py: tests that mutate
a .docx write a minimal one to tmp_path first via zipfile.
"""
from __future__ import annotations

import io
import zipfile

from meridian_docs import docs_intel


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"


def _make_docx_bytes(xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def _write_docx(tmp_path, xml: str, name: str = "doc.docx") -> str:
    path = str(tmp_path / name)
    with open(path, "wb") as fh:
        fh.write(_make_docx_bytes(xml))
    return path


_TWO_ANCHOR_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="H0000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Results</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000001">
      <w:r><w:t>First body paragraph.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000002">
      <w:r><w:t>Second body paragraph.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000003">
      <w:r><w:t>Third body paragraph.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""

# A variant that already carries a genuine, human/Word-authored bookmark
# using the small sequential w:id Word itself assigns (0, 1, 2, ...) --
# exactly the range the OLD hardcoded "0" collided with, and the range the
# previous _next_note_bookmark_name seed of 0 would have walked straight
# back into.
_XML_WITH_EXISTING_SMALL_ID_BOOKMARK = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="P0000001">
      <w:bookmarkStart w:id="1" w:name="_CustomAnchor"/>
      <w:r><w:t>Body paragraph with a real, human-authored bookmark.</w:t></w:r>
      <w:bookmarkEnd w:id="1"/>
    </w:p>
    <w:p w14:paraId="P0000002">
      <w:r><w:t>Second body paragraph.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""


def _all_bookmark_pairs(root) -> list[tuple[str, str]]:
    """Return every (w:id, w:name) pair found on <w:bookmarkStart> elements,
    plus a parallel check that every bookmarkStart id has a matching
    bookmarkEnd id somewhere in the document (pairing is intact)."""
    starts = [
        (bm.get(docs_intel._q(_W, "id")), bm.get(docs_intel._q(_W, "name")))
        for bm in root.iter(docs_intel._q(_W, "bookmarkStart"))
    ]
    end_ids = [
        bm.get(docs_intel._q(_W, "id")) for bm in root.iter(docs_intel._q(_W, "bookmarkEnd"))
    ]
    for start_id, _name in starts:
        assert start_id in end_ids, (
            f"bookmarkStart w:id={start_id!r} has no matching bookmarkEnd"
        )
    return starts


# ---------------------------------------------------------------------------
# Integration: insert_caption -- multiple insertions must not collide.
# ---------------------------------------------------------------------------


def test_two_caption_insertions_get_unique_bookmark_ids(tmp_path):
    path = _write_docx(tmp_path, _TWO_ANCHOR_XML)

    r1 = docs_intel.insert_caption(path, "P0000001", "Figure", "First figure.")
    r2 = docs_intel.insert_caption(path, "P0000002", "Figure", "Second figure.")
    assert r1["status"] == "inserted"
    assert r2["status"] == "inserted"
    assert r1["ref_bookmark"] != r2["ref_bookmark"]

    _raw, root = docs_intel._load_docx_xml_stdlib(path)
    pairs = _all_bookmark_pairs(root)
    assert len(pairs) == 2

    ids = [pid for pid, _name in pairs]
    # The old bug: both bookmarks hardcoded w:id="0" -- a real duplicate.
    assert ids.count("0") == 0
    assert len(ids) == len(set(ids)), f"duplicate bookmark w:id values: {ids}"

    # Each bookmark's numeric w:id must equal its own name's digit suffix.
    for bm_id, name in pairs:
        m = docs_intel._REF_BOOKMARK_RE.match(name)
        assert m is not None, f"unexpected bookmark name shape: {name!r}"
        assert bm_id == m.group(1)


def test_three_internal_note_insertions_get_unique_bookmark_ids(tmp_path):
    path = _write_docx(tmp_path, _TWO_ANCHOR_XML)

    note_ids = []
    for anchor in ("P0000001", "P0000002", "P0000003"):
        result = docs_intel.insert_highlighted_note(path, "Check this.", anchor)
        assert result["status"] == "inserted"
        note_ids.append(result["note_id"])

    assert len(note_ids) == len(set(note_ids)) == 3

    _raw, root = docs_intel._load_docx_xml_stdlib(path)
    pairs = _all_bookmark_pairs(root)
    assert len(pairs) == 3

    ids = [pid for pid, _name in pairs]
    assert ids.count("0") == 0
    assert len(ids) == len(set(ids)), f"duplicate bookmark w:id values: {ids}"

    for bm_id, name in pairs:
        m = docs_intel._INTERNAL_NOTE_BOOKMARK_RE.match(name)
        assert m is not None, f"unexpected bookmark name shape: {name!r}"
        assert bm_id == m.group(1)


def test_caption_and_internal_note_bookmark_ids_never_collide(tmp_path):
    """The exact cross-type collision the shared hardcoded w:id="0" caused:
    a caption AND a note in the same document used to mint identical
    bookmark ids even though their NAMES already differed."""
    path = _write_docx(tmp_path, _TWO_ANCHOR_XML)

    cap = docs_intel.insert_caption(path, "P0000001", "Figure", "The only figure.")
    note = docs_intel.insert_highlighted_note(path, "Double-check this.", "P0000002")
    assert cap["status"] == "inserted"
    assert note["status"] == "inserted"

    _raw, root = docs_intel._load_docx_xml_stdlib(path)
    pairs = _all_bookmark_pairs(root)
    assert len(pairs) == 2
    ids = [pid for pid, _name in pairs]
    assert len(ids) == len(set(ids)), f"caption and note bookmark ids collided: {ids}"


def test_note_inserted_alongside_existing_small_id_bookmark_does_not_collide(tmp_path):
    """Reproduces the real-world failure mode: a document already carries a
    genuine, human-authored bookmark using a small sequential w:id (the kind
    Word itself hands out). The old _next_note_bookmark_name seed of 0 meant
    the FIRST note minted "_MNote1", and the old hardcoded builder id of "0"
    was even worse -- either way it could land squarely on ids already used
    by real content in the file."""
    path = _write_docx(tmp_path, _XML_WITH_EXISTING_SMALL_ID_BOOKMARK)

    result = docs_intel.insert_highlighted_note(path, "New note.", "P0000002")
    assert result["status"] == "inserted"

    _raw, root = docs_intel._load_docx_xml_stdlib(path)
    pairs = _all_bookmark_pairs(root)
    ids = [pid for pid, _name in pairs]
    assert len(ids) == len(set(ids)), f"duplicate bookmark w:id values: {ids}"
    # The pre-existing bookmark's id must survive untouched.
    assert "1" in ids


# ---------------------------------------------------------------------------
# Unit: _build_caption_paragraph / _build_internal_note_paragraph
# ---------------------------------------------------------------------------


def test_build_caption_paragraph_id_derived_from_name_digits():
    p = docs_intel._build_caption_paragraph(
        kind="Figure", label_text="A figure.", seq_cached="3", ref_bookmark="_Ref100000007"
    )
    starts = list(p.iter(docs_intel._q(_W, "bookmarkStart")))
    ends = list(p.iter(docs_intel._q(_W, "bookmarkEnd")))
    assert len(starts) == 1 and len(ends) == 1
    assert starts[0].get(docs_intel._q(_W, "id")) == "100000007"
    assert starts[0].get(docs_intel._q(_W, "name")) == "_Ref100000007"
    # bookmarkStart/bookmarkEnd must share the SAME id -- that is what makes
    # them a valid, unambiguous pair.
    assert ends[0].get(docs_intel._q(_W, "id")) == "100000007"


def test_build_caption_paragraph_no_ref_bookmark_emits_no_bookmark():
    p = docs_intel._build_caption_paragraph(
        kind="Table", label_text="A table.", seq_cached="1", ref_bookmark=None
    )
    assert list(p.iter(docs_intel._q(_W, "bookmarkStart"))) == []
    assert list(p.iter(docs_intel._q(_W, "bookmarkEnd"))) == []


def test_build_caption_paragraph_two_calls_get_different_ids():
    """Direct unit-level regression guard for the original bug: two builder
    calls with two different ref_bookmark names must not both fall back to
    the same hardcoded id."""
    p1 = docs_intel._build_caption_paragraph(
        kind="Figure", label_text="One.", seq_cached="1", ref_bookmark="_Ref100000001"
    )
    p2 = docs_intel._build_caption_paragraph(
        kind="Figure", label_text="Two.", seq_cached="2", ref_bookmark="_Ref100000002"
    )
    id1 = next(iter(p1.iter(docs_intel._q(_W, "bookmarkStart")))).get(docs_intel._q(_W, "id"))
    id2 = next(iter(p2.iter(docs_intel._q(_W, "bookmarkStart")))).get(docs_intel._q(_W, "id"))
    assert id1 != id2
    assert "0" not in (id1, id2)


def test_build_internal_note_paragraph_id_derived_from_name_digits():
    p = docs_intel._build_internal_note_paragraph(
        "Check this stat.", "_MNote200000003", "MeridianInternalNote"
    )
    starts = list(p.iter(docs_intel._q(_W, "bookmarkStart")))
    ends = list(p.iter(docs_intel._q(_W, "bookmarkEnd")))
    assert len(starts) == 1 and len(ends) == 1
    assert starts[0].get(docs_intel._q(_W, "id")) == "200000003"
    assert starts[0].get(docs_intel._q(_W, "name")) == "_MNote200000003"
    assert ends[0].get(docs_intel._q(_W, "id")) == "200000003"


def test_build_caption_paragraph_nonconforming_name_falls_back_safely():
    """Defensive path: every real caller sources ref_bookmark from
    _next_ref_bookmark_name (or a local seed reserved from it), so this
    should never fire in practice -- but a non-conforming name must not
    crash the builder, and the start/end pair must still share one id."""
    p = docs_intel._build_caption_paragraph(
        kind="Figure", label_text="X.", seq_cached="1", ref_bookmark="_CustomBookmark"
    )
    starts = list(p.iter(docs_intel._q(_W, "bookmarkStart")))
    ends = list(p.iter(docs_intel._q(_W, "bookmarkEnd")))
    assert starts[0].get(docs_intel._q(_W, "id")) == ends[0].get(docs_intel._q(_W, "id"))


def test_build_internal_note_paragraph_nonconforming_name_falls_back_safely():
    p = docs_intel._build_internal_note_paragraph(
        "Text.", "_CustomNote", "MeridianInternalNote"
    )
    starts = list(p.iter(docs_intel._q(_W, "bookmarkStart")))
    ends = list(p.iter(docs_intel._q(_W, "bookmarkEnd")))
    assert starts[0].get(docs_intel._q(_W, "id")) == ends[0].get(docs_intel._q(_W, "id"))


# ---------------------------------------------------------------------------
# Unit: _next_ref_bookmark_name / _next_note_bookmark_name
# ---------------------------------------------------------------------------


def test_next_ref_bookmark_name_fresh_doc_seeded_at_100000000(tmp_path):
    path = _write_docx(tmp_path, _TWO_ANCHOR_XML)
    _raw, root = docs_intel._load_docx_xml_stdlib(path)
    name = docs_intel._next_ref_bookmark_name(root)
    assert name == "_Ref100000000"


def test_next_note_bookmark_name_fresh_doc_seeded_at_200000000(tmp_path):
    """5b2ce3fb -- the seed was raised from 0 to 200000000 specifically so the
    digit suffix (reused verbatim as the bookmark's numeric w:id by
    _build_internal_note_paragraph) stays clear of the small sequential ids
    real Word bookmarks use."""
    path = _write_docx(tmp_path, _TWO_ANCHOR_XML)
    _raw, root = docs_intel._load_docx_xml_stdlib(path)
    name = docs_intel._next_note_bookmark_name(root)
    assert name == "_MNote200000000"


def test_next_note_bookmark_name_unaffected_by_unrelated_small_id_bookmark(tmp_path):
    """A pre-existing bookmark with a small w:id but a NAME that doesn't
    match the _MNote<digits> pattern must not perturb the note counter --
    only same-scheme names feed max_seen."""
    path = _write_docx(tmp_path, _XML_WITH_EXISTING_SMALL_ID_BOOKMARK)
    _raw, root = docs_intel._load_docx_xml_stdlib(path)
    name = docs_intel._next_note_bookmark_name(root)
    assert name == "_MNote200000000"


def test_next_note_bookmark_name_increments_past_existing_note(tmp_path):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="P0000001">
      <w:bookmarkStart w:id="200000005" w:name="_MNote200000005"/>
      <w:r><w:t>Existing note.</w:t></w:r>
      <w:bookmarkEnd w:id="200000005"/>
    </w:p>
  </w:body>
</w:document>
"""
    path = _write_docx(tmp_path, xml)
    _raw, root = docs_intel._load_docx_xml_stdlib(path)
    name = docs_intel._next_note_bookmark_name(root)
    assert name == "_MNote200000006"
