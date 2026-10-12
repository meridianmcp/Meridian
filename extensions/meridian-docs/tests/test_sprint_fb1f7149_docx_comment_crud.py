"""Regression tests for sprint item fb1f7149, slice 1: native Word comment CRUD.

Covers list / read / update / delete over real ``word/comments.xml`` and the
``commentRangeStart`` / ``commentRangeEnd`` / ``commentReference`` markers in
``word/document.xml``, including a round trip through the existing
``insert_word_comment`` writer.
"""

from __future__ import annotations

import zipfile
import xml.etree.ElementTree as ET

import pytest

from meridian_docs import docs_intel, docx_review


@pytest.fixture(autouse=True)
def _stub_render_capability(monkeypatch):
    """Stub a successful render so these tests check structure, not the local render backend."""
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
_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_WP14 = "http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing"


_DOC_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" xmlns:wp14="http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing" mc:Ignorable="w14 wp14">
  <w:body>
    <w:p w14:paraId="1A000001">
      <w:r><w:t xml:space="preserve">Alpha </w:t></w:r>
      <w:commentRangeStart w:id="0"/>
      <w:r><w:t>beta gamma</w:t></w:r>
      <w:commentRangeEnd w:id="0"/>
      <w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:commentReference w:id="0"/></w:r>
    </w:p>
    <w:p w14:paraId="1A000002">
      <w:commentRangeStart w:id="1"/>
      <w:r><w:t>Second paragraph under review.</w:t></w:r>
      <w:commentRangeEnd w:id="1"/>
      <w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:commentReference w:id="1"/></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""

_COMMENTS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:comment w:id="0" w:author="Reviewer One" w:initials="R1" w:date="2026-10-01T09:30:00Z">
    <w:p w14:paraId="2B000001">
      <w:pPr><w:pStyle w:val="CommentText"/></w:pPr>
      <w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:annotationRef/></w:r>
      <w:r><w:t>Original first comment.</w:t></w:r>
    </w:p>
    <w:p>
      <w:r><w:t>Second paragraph of the same comment.</w:t></w:r>
    </w:p>
  </w:comment>
  <w:comment w:id="1" w:author="Reviewer Two" w:initials="R2" w:date="2026-10-02T10:00:00Z">
    <w:p>
      <w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:annotationRef/></w:r>
      <w:r><w:t>Keep this one.</w:t></w:r>
    </w:p>
  </w:comment>
</w:comments>
"""

_PLAIN_DOC_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="3C000001"><w:r><w:t>No comments here.</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""

_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>
</Relationships>
"""

_CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>
</Types>
"""


def _write_docx(tmp_path, document_xml=_DOC_XML, comments_xml=_COMMENTS_XML, name="doc.docx"):
    path = str(tmp_path / name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/_rels/document.xml.rels", _RELS_XML)
        if comments_xml is not None:
            archive.writestr("word/comments.xml", comments_xml)
    return path


def _part(path, member):
    with zipfile.ZipFile(path) as archive:
        return archive.read(member)


def _paragraph_texts(path):
    root = ET.fromstring(_part(path, "word/document.xml"))
    return [
        "".join(node.text or "" for node in paragraph.iter(f"{{{_W}}}t"))
        for paragraph in root.iter(f"{{{_W}}}p")
    ]


def _comment_element(path, comment_id):
    root = ET.fromstring(_part(path, "word/comments.xml"))
    for element in root.findall(f"{{{_W}}}comment"):
        if element.get(f"{{{_W}}}id") == comment_id:
            return element
    raise AssertionError(f"comment {comment_id} missing from comments.xml")


def _marker_ids(path):
    root = ET.fromstring(_part(path, "word/document.xml"))
    tags = {f"{{{_W}}}{name}" for name in ("commentRangeStart", "commentRangeEnd", "commentReference")}
    return {element.get(f"{{{_W}}}id") for element in root.iter() if element.tag in tags}


# ---------------------------------------------------------------------------
# list / read
# ---------------------------------------------------------------------------


def test_list_reports_authorship_text_and_anchor_paragraph(tmp_path):
    path = _write_docx(tmp_path)

    result = docx_review.list_word_comments(path)

    assert result["status"] == "ok"
    assert result["count"] == 2
    assert result["orphan_marker_ids"] == []
    first, second = result["comments"]
    assert first["comment_id"] == "0"
    assert first["author"] == "Reviewer One"
    assert first["initials"] == "R1"
    assert first["date"] == "2026-10-01T09:30:00Z"
    assert first["text"] == "Original first comment.\nSecond paragraph of the same comment."
    assert first["anchor_para_id"] == "1A000001"
    assert first["anchored"] is True
    assert second["comment_id"] == "1"
    assert second["anchor_para_id"] == "1A000002"


def test_read_returns_one_comment_and_rejects_bad_ids(tmp_path):
    path = _write_docx(tmp_path)

    found = docx_review.read_word_comment(path, 1)
    assert found["status"] == "ok"
    assert found["comment"]["comment_id"] == "1"
    assert found["comment"]["text"] == "Keep this one."

    assert docx_review.read_word_comment(path, "01")["comment"]["comment_id"] == "1"
    assert "not found" in docx_review.read_word_comment(path, 9)["error"]
    assert "non-negative integer" in docx_review.read_word_comment(path, "x")["error"]
    assert "non-negative integer" in docx_review.read_word_comment(path, True)["error"]


def test_document_without_comments_part_lists_empty(tmp_path):
    path = _write_docx(tmp_path, document_xml=_PLAIN_DOC_XML, comments_xml=None)

    result = docx_review.list_word_comments(path)

    assert result["status"] == "ok"
    assert result["count"] == 0
    assert result["comments"] == []
    assert result["comments_part"] is None
    assert result["orphan_marker_ids"] == []


def test_missing_file_is_reported_by_every_entry_point(tmp_path):
    missing = str(tmp_path / "nope.docx")

    assert "error" in docx_review.list_word_comments(missing)
    assert "error" in docx_review.read_word_comment(missing, 0)
    assert "error" in docx_review.update_word_comment(missing, 0, "text")
    assert "error" in docx_review.delete_word_comment(missing, 0)


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


def test_update_replaces_body_and_keeps_author_initials_and_date(tmp_path):
    path = _write_docx(tmp_path)

    result = docx_review.update_word_comment(path, 0, "Revised first line.\nRevised second line.")

    assert result["status"] == "updated"
    assert result["author"] == "Reviewer One"
    assert result["initials"] == "R1"
    assert result["date"] == "2026-10-01T09:30:00Z"

    listed = docx_review.list_word_comments(path)
    first = listed["comments"][0]
    assert first["text"] == "Revised first line.\nRevised second line."
    assert first["author"] == "Reviewer One"
    assert first["initials"] == "R1"
    assert first["date"] == "2026-10-01T09:30:00Z"
    assert first["anchor_para_id"] == "1A000001"
    assert first["anchored"] is True


def test_update_keeps_annotation_ref_and_first_paragraph_properties(tmp_path):
    path = _write_docx(tmp_path)

    docx_review.update_word_comment(path, 0, "Replaced.")

    comment = _comment_element(path, "0")
    head = comment.findall(f"{{{_W}}}p")[0]
    assert head.find(f"{{{_W}}}pPr/{{{_W}}}pStyle").get(f"{{{_W}}}val") == "CommentText"
    assert len(list(comment.iter(f"{{{_W}}}annotationRef"))) == 1
    assert len(comment.findall(f"{{{_W}}}p")) == 1


def test_update_leaves_other_comment_and_document_text_untouched(tmp_path):
    path = _write_docx(tmp_path)
    before_paragraphs = _paragraph_texts(path)
    before_markers = _marker_ids(path)

    docx_review.update_word_comment(path, 0, "Only comment zero changes.")

    assert _paragraph_texts(path) == before_paragraphs
    assert _marker_ids(path) == before_markers
    assert docx_review.list_word_comments(path)["comments"][1]["text"] == "Keep this one."


def test_update_refuses_unknown_and_orphan_ids_without_touching_file(tmp_path):
    path = _write_docx(tmp_path)
    before = _part(path, "word/comments.xml")

    assert "has no comment body" in docx_review.update_word_comment(path, 9, "x")["error"]
    assert _part(path, "word/comments.xml") == before


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_removes_body_and_markers_for_one_id_only(tmp_path):
    path = _write_docx(tmp_path)

    result = docx_review.delete_word_comment(path, 0)

    assert result["status"] == "deleted"
    assert result["removed_comment_body"] is True
    assert result["removed_markers"] == 3

    listed = docx_review.list_word_comments(path)
    assert [record["comment_id"] for record in listed["comments"]] == ["1"]
    assert listed["comments"][0]["anchored"] is True
    assert listed["comments"][0]["anchor_para_id"] == "1A000002"
    assert _marker_ids(path) == {"1"}


def test_delete_leaves_anchored_text_and_no_empty_run(tmp_path):
    path = _write_docx(tmp_path)

    docx_review.delete_word_comment(path, 0)

    assert _paragraph_texts(path)[0] == "Alpha beta gamma"
    root = ET.fromstring(_part(path, "word/document.xml"))
    first_paragraph = next(root.iter(f"{{{_W}}}p"))
    assert len(list(first_paragraph.iter(f"{{{_W}}}r"))) == 2


def test_delete_keeps_root_namespace_declarations_for_mc_ignorable(tmp_path):
    path = _write_docx(tmp_path)

    docx_review.delete_word_comment(path, 1)

    document_bytes = _part(path, "word/document.xml")
    assert b'xmlns:wp14="' + _WP14.encode() + b'"' in document_bytes
    root = ET.fromstring(document_bytes)
    assert root.get(f"{{{_MC}}}Ignorable") == "w14 wp14"


def test_delete_orphan_marker_without_comment_body(tmp_path):
    orphan_doc = _DOC_XML.replace(
        "</w:p>\n    <w:sectPr/>",
        '<w:commentRangeStart w:id="7"/><w:r><w:t>tail</w:t></w:r><w:commentRangeEnd w:id="7"/>'
        '<w:r><w:commentReference w:id="7"/></w:r></w:p>\n    <w:sectPr/>',
    )
    path = _write_docx(tmp_path, document_xml=orphan_doc)
    assert docx_review.list_word_comments(path)["orphan_marker_ids"] == ["7"]

    result = docx_review.delete_word_comment(path, 7)

    assert result["status"] == "deleted"
    assert result["removed_comment_body"] is False
    assert result["removed_markers"] == 3
    listed = docx_review.list_word_comments(path)
    assert listed["orphan_marker_ids"] == []
    assert listed["count"] == 2


def test_delete_refuses_unknown_id_without_touching_file(tmp_path):
    path = _write_docx(tmp_path)
    before = _part(path, "word/document.xml")

    assert "not found" in docx_review.delete_word_comment(path, 9)["error"]
    assert _part(path, "word/document.xml") == before


# ---------------------------------------------------------------------------
# input validation
# ---------------------------------------------------------------------------


def test_invalid_inputs_are_rejected_before_any_write(tmp_path):
    path = _write_docx(tmp_path)
    before_doc = _part(path, "word/document.xml")
    before_comments = _part(path, "word/comments.xml")

    assert "non-empty" in docx_review.update_word_comment(path, 0, "   ")["error"]
    assert "non-negative integer" in docx_review.update_word_comment(path, "x", "text")["error"]
    assert "non-negative integer" in docx_review.update_word_comment(path, -1, "text")["error"]
    assert "degraded_render_reason" in docx_review.update_word_comment(
        path, 0, "text", allow_degraded_render=True, degraded_render_reason="  "
    )["error"]
    assert "non-negative integer" in docx_review.delete_word_comment(path, True)["error"]
    assert "degraded_render_reason" in docx_review.delete_word_comment(
        path, 0, allow_degraded_render=True
    )["error"]

    assert _part(path, "word/document.xml") == before_doc
    assert _part(path, "word/comments.xml") == before_comments


# ---------------------------------------------------------------------------
# round trip through the existing writer
# ---------------------------------------------------------------------------


def test_insert_update_delete_round_trip_with_insert_word_comment(tmp_path):
    path = _write_docx(tmp_path)
    original_paragraphs = _paragraph_texts(path)

    inserted = docs_intel.insert_word_comment(
        path, "Added by the round trip", anchor_para_id="1A000002", author="Adam Camerer", initials="AC"
    )
    assert inserted["status"] == "inserted"
    new_id = str(inserted["comment_id"])

    added = docx_review.read_word_comment(path, new_id)["comment"]
    assert added["author"] == "Adam Camerer"
    assert added["initials"] == "AC"
    assert added["text"] == "Added by the round trip"
    assert added["anchor_para_id"] == "1A000002"

    edited = docx_review.update_word_comment(path, new_id, "Edited in the round trip")
    assert edited["status"] == "updated"
    after_edit = docx_review.read_word_comment(path, new_id)["comment"]
    assert after_edit["text"] == "Edited in the round trip"
    assert after_edit["date"] == added["date"]
    assert after_edit["author"] == "Adam Camerer"

    deleted = docx_review.delete_word_comment(path, new_id)
    assert deleted["status"] == "deleted"
    final = docx_review.list_word_comments(path)
    assert [record["comment_id"] for record in final["comments"]] == ["0", "1"]
    assert _paragraph_texts(path) == original_paragraphs
    assert _marker_ids(path) == {"0", "1"}
