"""Regression test for 71db285b.

BUG: document_outline (via parse_docx) and move_section/copy_section/
_locate_section_bounds disagreed on paragraph identity for any docx without
real w14:paraId. parse_docx used a naive f"p{index}" position counter, while
move_section/copy_section/_locate_section_bounds resolve ids against
_vendored_content_tree._build_synth_id_map's content-hash "sp<hash>" scheme
(heading breadcrumb + normalized paragraph text + occurrence counter). A
caller who discovered a heading's id via document_outline (the natural,
documented way to discover ids) and then passed that id straight into
move_section/copy_section would get a "not found" style failure on any real
document that lacks a native w14:paraId on every paragraph -- which is most
real Word documents, since Word does not assign w14:paraId to every
paragraph.

FIX: parse_docx now calls _build_synth_id_map(body) once up front and uses
the same three-tier id resolution (native w14:paraId -> synth_map -> f"p{N}")
that _paragraph_node / _find_para_by_id / _locate_section_bounds already use.

These tests build a document with NO native w14:paraId anywhere, discover a
heading's id via document_outline (the code path that was broken), and then
feed that exact id into move_section / copy_section -- proving they now
resolve identically instead of only working after a totally different id
was manually looked up via index_docx_structure.
"""
from __future__ import annotations

import io
import zipfile

import pytest

from meridian_docs import docs_intel, server
from meridian_docs._vendored_content_tree import _build_synth_id_map


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


# No paragraph anywhere carries a native w14:paraId -- the exact scenario the
# bug report calls out ("any docx without real w14:paraId").
_NO_NATIVE_IDS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>Intro body paragraph.</w:t></w:r></w:p>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Setup</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>Setup body paragraph.</w:t></w:r></w:p>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Conclusion</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>Conclusion body paragraph.</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def test_parse_docx_ids_match_synth_id_map_directly():
    """parse_docx's para_id must equal what _build_synth_id_map assigns to the
    same live paragraph element -- not a bare positional f"p{N}"."""
    docx_bytes = _make_docx_bytes(_NO_NATIVE_IDS_XML)
    root = docs_intel.ET.fromstring(
        zipfile.ZipFile(io.BytesIO(docx_bytes)).read("word/document.xml")
    )
    body = root.find(docs_intel._q(_W, "body"))
    synth_map = _build_synth_id_map(body)
    expected_ids = [synth_map[id(p)] for p in body.findall(docs_intel._q(_W, "p"))]
    assert all(eid.startswith("sp") for eid in expected_ids)

    paras = docs_intel.parse_docx(docx_bytes)
    actual_ids = [p["para_id"] for p in paras]
    assert actual_ids == expected_ids
    # Confirm this is really exercising the fix, not accidentally passing:
    # the old buggy behavior would have produced positional ids instead.
    assert actual_ids != [f"p{i}" for i in range(len(paras))]


def test_document_outline_ids_are_synth_ids_not_positional():
    docx_bytes = _make_docx_bytes(_NO_NATIVE_IDS_XML)
    outline = docs_intel.document_outline(docx_bytes)
    heading_ids = [h["para_id"] for h in outline["headings"]]
    assert len(heading_ids) == 3
    assert all(hid.startswith("sp") for hid in heading_ids)


def test_move_section_accepts_document_outline_discovered_id(tmp_path):
    """The end-to-end regression: discover an id the way a real caller would
    (document_outline), then feed it straight into move_section. Before the
    fix, this id would not resolve against _locate_section_bounds's
    synth-id-based scan and the move would either fail outright or (worse)
    silently target the wrong paragraph."""
    path = _write_docx(tmp_path, _NO_NATIVE_IDS_XML, name="no_native_ids.docx")

    outline_before = docs_intel.document_outline(path)
    by_text = {h["text"]: h["para_id"] for h in outline_before["headings"]}
    setup_id = by_text["Setup"]
    conclusion_id = by_text["Conclusion"]
    assert setup_id.startswith("sp")
    assert conclusion_id.startswith("sp")

    result = docs_intel.move_section(path, setup_id, conclusion_id, destination_position="before")
    assert result["status"] == "moved"
    assert result["moved_block_count"] == 2  # Setup heading + its body paragraph

    outline_after = docs_intel.document_outline(path)
    order = [h["text"] for h in outline_after["headings"]]
    assert order == ["Introduction", "Setup", "Conclusion"]


def test_copy_section_accepts_document_outline_discovered_id(tmp_path):
    path = _write_docx(tmp_path, _NO_NATIVE_IDS_XML, name="no_native_ids_copy.docx")

    outline_before = docs_intel.document_outline(path)
    by_text = {h["text"]: h["para_id"] for h in outline_before["headings"]}
    intro_id = by_text["Introduction"]
    conclusion_id = by_text["Conclusion"]

    result = docs_intel.copy_section(path, intro_id, conclusion_id, destination_position="after")
    assert result["status"] == "copied"

    outline_after = docs_intel.document_outline(path)
    texts = [h["text"] for h in outline_after["headings"]]
    assert texts.count("Introduction") == 2
    assert texts == ["Introduction", "Setup", "Conclusion", "Introduction"]


# ---------------------------------------------------------------------------
# 1dff1300 -- cursor-based pagination for document_outline. Grouped in this
# file since pagination must preserve the SAME identity guarantees
# (para_id/index) the fix above established -- a paginated page's headings
# must be byte-for-byte identical (same ids, same order) to the
# corresponding slice of an unpaginated call.
# ---------------------------------------------------------------------------


def _three_heading_docx(tmp_path) -> str:
    return _write_docx(tmp_path, _NO_NATIVE_IDS_XML, name="three_headings.docx")


def test_document_outline_default_call_is_backward_compatible(tmp_path):
    path = _three_heading_docx(tmp_path)

    outline = docs_intel.document_outline(path)

    assert set(outline.keys()) == {
        "paragraph_count", "heading_count", "headings", "section_regions",
        "document_fingerprint",
    }
    assert outline["heading_count"] == 3
    assert len(outline["headings"]) == 3
    assert "cursor" not in outline
    assert isinstance(outline["document_fingerprint"], str) and outline["document_fingerprint"]
    # Each heading also now carries its own paragraph index (additive field).
    for h in outline["headings"]:
        assert isinstance(h["index"], int)


def test_document_outline_pagination_first_and_second_page_reconstruct_full_set(tmp_path):
    path = _three_heading_docx(tmp_path)
    full = docs_intel.document_outline(path)

    page1 = docs_intel.document_outline(path, page_size=2)
    assert page1["total"] == 3
    assert page1["has_more"] is True
    assert page1["cursor"] is not None
    assert [h["text"] for h in page1["headings"]] == ["Introduction", "Setup"]

    page2 = docs_intel.document_outline(path, cursor=page1["cursor"])
    assert page2["has_more"] is False
    assert page2["cursor"] is None
    assert [h["text"] for h in page2["headings"]] == ["Conclusion"]

    reconstructed = page1["headings"] + page2["headings"]
    assert reconstructed == full["headings"]


def test_document_outline_paginated_heading_ids_match_unpaginated_ids(tmp_path):
    """The core identity guarantee this file exists for, applied across a
    pagination boundary: a heading's id must be IDENTICAL whether fetched
    via a full call or via a paginated page."""
    path = _three_heading_docx(tmp_path)
    full = docs_intel.document_outline(path)
    full_by_text = {h["text"]: h["para_id"] for h in full["headings"]}

    page1 = docs_intel.document_outline(path, page_size=1)
    page2 = docs_intel.document_outline(path, cursor=page1["cursor"])
    page3 = docs_intel.document_outline(path, cursor=page2["cursor"])

    for page in (page1, page2, page3):
        for h in page["headings"]:
            assert h["para_id"] == full_by_text[h["text"]]
            assert h["para_id"].startswith("sp")


def test_document_outline_page_size_one_visits_every_heading_exactly_once(tmp_path):
    path = _three_heading_docx(tmp_path)
    seen: list[str] = []
    cursor = None
    page_size = 1
    for _ in range(10):  # bounded loop -- must terminate well before this
        page = docs_intel.document_outline(
            path, page_size=page_size if cursor is None else None, cursor=cursor
        )
        assert "error" not in page
        seen.extend(h["text"] for h in page["headings"])
        if not page["has_more"]:
            break
        cursor = page["cursor"]
    else:
        raise AssertionError("pagination did not terminate within 10 pages")
    assert seen == ["Introduction", "Setup", "Conclusion"]


def test_document_outline_rejects_invalid_page_size(tmp_path):
    path = _three_heading_docx(tmp_path)

    result = docs_intel.document_outline(path, page_size=0)

    assert "error" in result
    assert result["reason"] == "invalid_page_size"


def test_document_outline_rejects_malformed_cursor(tmp_path):
    path = _three_heading_docx(tmp_path)

    result = docs_intel.document_outline(path, cursor="not-a-real-cursor")

    assert "error" in result
    assert result["reason"] == "invalid_cursor"


def _raw_cursor(payload: dict) -> str:
    """Build a cursor token bypassing _encode_page_cursor's own validation
    -- used to exercise _decode_page_cursor's individual structural checks
    directly (missing/wrong-typed fields), not just "not base64 at all"."""
    raw = docs_intel.json.dumps(payload).encode("utf-8")
    return docs_intel.base64.urlsafe_b64encode(raw).decode("ascii")


def test_document_outline_rejects_cursor_missing_fingerprint(tmp_path):
    path = _three_heading_docx(tmp_path)
    cursor = _raw_cursor({"v": 1, "kind": "outline", "off": 0, "ps": 2, "sa": None})

    result = docs_intel.document_outline(path, cursor=cursor)

    assert "error" in result
    assert result["reason"] == "invalid_cursor"


def test_document_outline_rejects_cursor_with_negative_offset(tmp_path):
    path = _three_heading_docx(tmp_path)
    cursor = _raw_cursor(
        {"v": 1, "kind": "outline", "fp": "deadbeef", "off": -5, "ps": 2, "sa": None}
    )

    result = docs_intel.document_outline(path, cursor=cursor)

    assert "error" in result
    assert result["reason"] == "invalid_cursor"


def test_document_outline_rejects_cursor_with_non_positive_page_size(tmp_path):
    path = _three_heading_docx(tmp_path)
    cursor = _raw_cursor(
        {"v": 1, "kind": "outline", "fp": "deadbeef", "off": 0, "ps": 0, "sa": None}
    )

    result = docs_intel.document_outline(path, cursor=cursor)

    assert "error" in result
    assert result["reason"] == "invalid_cursor"


def test_document_outline_rejects_cursor_with_non_string_section_anchor(tmp_path):
    path = _three_heading_docx(tmp_path)
    cursor = _raw_cursor(
        {"v": 1, "kind": "outline", "fp": "deadbeef", "off": 0, "ps": 2, "sa": 123}
    )

    result = docs_intel.document_outline(path, cursor=cursor)

    assert "error" in result
    assert result["reason"] == "invalid_cursor"


def test_document_outline_rejects_cursor_wrong_version(tmp_path):
    path = _three_heading_docx(tmp_path)
    cursor = _raw_cursor(
        {"v": 99, "kind": "outline", "fp": "deadbeef", "off": 0, "ps": 2, "sa": None}
    )

    result = docs_intel.document_outline(path, cursor=cursor)

    assert "error" in result
    assert result["reason"] == "invalid_cursor"


def test_document_outline_explicit_invalid_page_size_override_with_valid_cursor(tmp_path):
    """page_size can be passed ALONGSIDE a cursor to override the cursor's
    own embedded page_size -- an invalid override must still be rejected,
    not silently fall back to the cursor's original page_size."""
    path = _three_heading_docx(tmp_path)
    page1 = docs_intel.document_outline(path, page_size=1)
    assert page1["has_more"] is True

    result = docs_intel.document_outline(path, cursor=page1["cursor"], page_size=0)

    assert "error" in result
    assert result["reason"] == "invalid_page_size"


def test_document_outline_rejects_stale_cursor_after_document_changes(tmp_path):
    path = _three_heading_docx(tmp_path)
    page1 = docs_intel.document_outline(path, page_size=2)
    assert page1["has_more"] is True

    # The document changes on disk between page requests (e.g. a concurrent
    # editor session saved a real edit).
    docs_intel.move_section(path, *[
        h["para_id"] for h in docs_intel.document_outline(path)["headings"][:2]
    ], destination_position="before")

    result = docs_intel.document_outline(path, cursor=page1["cursor"])

    assert "error" in result
    assert result["reason"] == "stale_cursor"


def test_document_outline_cursor_offset_beyond_current_total_is_stale(tmp_path):
    path = _three_heading_docx(tmp_path)
    full = docs_intel.document_outline(path, page_size=100)
    assert full["has_more"] is False

    # Manually mint a cursor pointing past the end for THIS document's real
    # fingerprint -- simulates a cursor issued against a longer prior
    # revision of the same content-identity window.
    forged_cursor = docs_intel._encode_page_cursor(
        kind="outline", fingerprint=full["document_fingerprint"],
        offset=999, page_size=10, section_anchor=None,
    )

    result = docs_intel.document_outline(path, cursor=forged_cursor)

    assert "error" in result
    assert result["reason"] == "stale_cursor"


def test_document_outline_snapshot_cursor_rejected_by_outline(tmp_path):
    """A cursor minted by read_document_snapshot must never be accepted by
    document_outline -- each pagination cursor is bound to its own kind."""
    path = _three_heading_docx(tmp_path)
    snapshot_page = docs_intel.read_document_snapshot(path, page_size=1)

    result = docs_intel.document_outline(path, cursor=snapshot_page["cursor"])

    assert "error" in result
    assert result["reason"] == "invalid_cursor"


def test_document_outline_section_anchor_scopes_to_subsection(tmp_path):
    path = _three_heading_docx(tmp_path)
    by_text = {h["text"]: h["para_id"] for h in docs_intel.document_outline(path)["headings"]}

    result = docs_intel.document_outline(path, section_anchor=by_text["Setup"])

    assert "error" not in result
    assert [h["text"] for h in result["headings"]] == ["Setup"]


def test_document_outline_section_anchor_not_found_returns_clear_error(tmp_path):
    path = _three_heading_docx(tmp_path)

    result = docs_intel.document_outline(path, section_anchor="Nonexistent Heading")

    assert "error" in result
    assert result["reason"] == "section_not_found"


def test_document_outline_server_wrapper_supports_pagination(tmp_path):
    path = _three_heading_docx(tmp_path)

    page1 = server.document_outline(path, page_size=2)

    assert page1["has_more"] is True
    assert len(page1["headings"]) == 2


# ---------------------------------------------------------------------------
# e21b2ca7 -- durable document-bound synth-id addressing, end-to-end through
# this package's real MCP-facing surface (document_outline / _find_para_by_id),
# not just the unit-level _build_synth_id_map covered directly in
# tests/test_docs_intel.py against the docparse package's own copy.
# ---------------------------------------------------------------------------


def test_document_outline_ids_stable_across_ancestor_heading_retitle(tmp_path):
    """A body paragraph's id discovered via document_outline must be the SAME
    before and after an ANCESTOR heading (not the paragraph itself) is
    retitled -- the exact "ancestry" mutability e21b2ca7 fixes."""
    _ns = (
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'
    )

    def _xml(heading_text: str) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document {_ns}>
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>{heading_text}</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>Body paragraph under the heading.</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""

    path_before = _write_docx(tmp_path, _xml("Setup"), name="before_retitle.docx")
    path_after = _write_docx(tmp_path, _xml("Setup (renamed)"), name="after_retitle.docx")

    before_paras = docs_intel.parse_docx(path_before)
    after_paras = docs_intel.parse_docx(path_after)

    before_body = next(
        p for p in before_paras if p["text"] == "Body paragraph under the heading."
    )
    after_body = next(
        p for p in after_paras if p["text"] == "Body paragraph under the heading."
    )

    assert before_body["para_id"].startswith("sp")
    assert before_body["para_id"] == after_body["para_id"], (
        "retitling the enclosing heading must not change the body "
        f"paragraph's id: {before_body['para_id']!r} -> {after_body['para_id']!r}"
    )


def test_find_para_by_id_raises_on_duplicate_native_paraid():
    """e21b2ca7: _find_para_by_id must fail closed (raise, not silently
    return the first match in document order) when the queried native
    w14:paraId is assigned to more than one paragraph -- a Word-invalid but
    real-world-possible (hand-edited or third-party-tool-generated) document
    state that would otherwise risk silently mutating the wrong paragraph."""
    _ns = (
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'
    )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document {_ns}>
  <w:body>
    <w:p w14:paraId="6BDC5378"><w:r><w:t>First copy.</w:t></w:r></w:p>
    <w:p w14:paraId="6BDC5378"><w:r><w:t>Second copy.</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
    docx_bytes = _make_docx_bytes(xml)
    root = docs_intel.ET.fromstring(
        zipfile.ZipFile(io.BytesIO(docx_bytes)).read("word/document.xml")
    )

    with pytest.raises(docs_intel.AmbiguousParagraphIdError):
        docs_intel._find_para_by_id(root, "6BDC5378")


def test_find_para_by_id_still_resolves_unique_native_paraid():
    """Sanity check alongside the ambiguity test above: a native paraId that
    is NOT duplicated still resolves normally (the new check must not
    false-positive on ordinary, valid documents)."""
    _ns = (
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'
    )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document {_ns}>
  <w:body>
    <w:p w14:paraId="11111111"><w:r><w:t>Only copy.</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
    docx_bytes = _make_docx_bytes(xml)
    root = docs_intel.ET.fromstring(
        zipfile.ZipFile(io.BytesIO(docx_bytes)).read("word/document.xml")
    )

    located = docs_intel._find_para_by_id(root, "11111111")

    assert located is not None
    _body, para, _idx = located
    assert docs_intel._q(_W, "p") == para.tag
