"""Regression tests for sprint item fb1f7149, slice 2: comment threads and resolution.

Covers reply_word_comment and resolve_word_comment over word/comments.xml and
word/commentsExtended.xml, the thread fields list_word_comments reports, and the
paragraph-id rule that keeps update_word_comment from breaking a thread link.
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
_W15 = "http://schemas.microsoft.com/office/word/2012/wordml"
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_EXT_REL_TYPE = "http://schemas.microsoft.com/office/2011/relationships/commentsExtended"
_EXT_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml"
)


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

# Comment 0's last paragraph has no w14:paraId, so a thread on it must mint one.
# Comment 1 is a single paragraph that already carries 2B000002.
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
    <w:p w14:paraId="2B000002">
      <w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:annotationRef/></w:r>
      <w:r><w:t>Keep this one.</w:t></w:r>
    </w:p>
  </w:comment>
</w:comments>
"""

# A Word-written commentsExtended part with extra namespaces and one resolved thread.
_EXTENDED_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w15:commentsEx xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml" xmlns:w16cid="http://schemas.microsoft.com/office/word/2016/wordml/cid" mc:Ignorable="w15 w16cid">
  <w15:commentEx w15:paraId="2B000002" w15:done="1"/>
</w15:commentsEx>
"""

_PLAIN_DOC_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="3C000001"><w:r><w:t>No comments here.</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def _rels_xml(extended: bool) -> str:
    extra = (
        f'  <Relationship Id="rId2" Type="{_EXT_REL_TYPE}" Target="commentsExtended.xml"/>\n'
        if extended
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        '  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>\n'
        f"{extra}"
        "</Relationships>\n"
    )


def _content_types_xml(extended: bool) -> str:
    extra = (
        f'  <Override PartName="/word/commentsExtended.xml" ContentType="{_EXT_CONTENT_TYPE}"/>\n'
        if extended
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        '  <Default Extension="xml" ContentType="application/xml"/>\n'
        '  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>\n'
        '  <Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>\n'
        f"{extra}"
        "</Types>\n"
    )


def _write_docx(tmp_path, *, document_xml=_DOC_XML, comments_xml=_COMMENTS_XML, extended_xml=None, name="doc.docx"):
    path = str(tmp_path / name)
    extended = extended_xml is not None
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types_xml(extended))
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/_rels/document.xml.rels", _rels_xml(extended))
        if comments_xml is not None:
            archive.writestr("word/comments.xml", comments_xml)
        if extended:
            archive.writestr("word/commentsExtended.xml", extended_xml)
    return path


def _part(path, member):
    with zipfile.ZipFile(path) as archive:
        return archive.read(member)


def _has_part(path, member):
    with zipfile.ZipFile(path) as archive:
        return member in archive.namelist()


def _paragraph_texts(path):
    root = ET.fromstring(_part(path, "word/document.xml"))
    return [
        "".join(node.text or "" for node in paragraph.iter(f"{{{_W}}}t"))
        for paragraph in root.iter(f"{{{_W}}}p")
    ]


def _body_paragraph_ids(path, comment_id):
    """w14:paraId of each paragraph of one comment body, in order (None where absent)."""
    root = ET.fromstring(_part(path, "word/comments.xml"))
    comment = next(
        element
        for element in root.findall(f"{{{_W}}}comment")
        if element.get(f"{{{_W}}}id") == comment_id
    )
    return [paragraph.get(f"{{{_W14}}}paraId") for paragraph in comment.findall(f"{{{_W}}}p")]


def _entries(path):
    """paraId -> (paraIdParent, done) for every commentEx in word/commentsExtended.xml."""
    if not _has_part(path, "word/commentsExtended.xml"):
        return {}
    root = ET.fromstring(_part(path, "word/commentsExtended.xml"))
    return {
        entry.get(f"{{{_W15}}}paraId"): (
            entry.get(f"{{{_W15}}}paraIdParent"),
            entry.get(f"{{{_W15}}}done"),
        )
        for entry in root.iter(f"{{{_W15}}}commentEx")
    }


def _all_para_ids(path):
    ids = []
    for member in ("word/document.xml", "word/comments.xml"):
        root = ET.fromstring(_part(path, member))
        ids += [element.get(f"{{{_W14}}}paraId") for element in root.iter() if element.get(f"{{{_W14}}}paraId")]
    return ids


def _marker_sequence(path, paragraph_index=0):
    """(tag, id) for every comment range and reference marker in one document paragraph."""
    root = ET.fromstring(_part(path, "word/document.xml"))
    paragraph = list(root.iter(f"{{{_W}}}p"))[paragraph_index]
    sequence = []
    for element in paragraph.iter():
        local = element.tag.split("}")[1]
        if local in {"commentRangeStart", "commentRangeEnd", "commentReference"}:
            sequence.append((local, element.get(f"{{{_W}}}id")))
    return sequence


# ---------------------------------------------------------------------------
# reply
# ---------------------------------------------------------------------------


def test_reply_threads_under_the_root_with_its_own_anchor_markers(tmp_path):
    path = _write_docx(tmp_path)
    texts_before = _paragraph_texts(path)

    result = docx_review.reply_word_comment(
        path, 0, "Agree, tighten this.\nSee section 2.", "Reviewer Three", "R3"
    )

    assert result["status"] == "replied"
    assert result["mode"] == "reply_comment"
    assert result["comment_id"] == "2"
    assert result["parent_comment_id"] == "0"
    assert result["thread_root_id"] == "0"
    record = docx_review.read_word_comment(path, "2")["comment"]
    assert record["author"] == "Reviewer Three"
    assert record["initials"] == "R3"
    assert record["date"] == result["date"]
    assert record["date"].endswith("Z")
    assert record["text"] == "Agree, tighten this.\nSee section 2."
    assert record["parent_comment_id"] == "0"
    assert record["thread_root_id"] == "0"
    assert record["anchored"] is True
    assert record["anchor_para_id"] == "1A000001"
    assert record["resolved"] is False
    # The reply's markers sit beside the root's: start after start, end after end, reference after reference.
    assert _marker_sequence(path) == [
        ("commentRangeStart", "0"),
        ("commentRangeStart", "2"),
        ("commentRangeEnd", "0"),
        ("commentRangeEnd", "2"),
        ("commentReference", "0"),
        ("commentReference", "2"),
    ]
    assert _paragraph_texts(path) == texts_before


def test_reply_links_through_comments_extended_by_paragraph_id(tmp_path):
    path = _write_docx(tmp_path)

    docx_review.reply_word_comment(path, 0, "First line.\nSecond line.", "Reviewer Three", "R3")

    root_ids = _body_paragraph_ids(path, "0")
    assert root_ids[0] == "2B000001"
    assert root_ids[1] not in (None, "2B000001")  # minted on the root's last paragraph
    reply_ids = _body_paragraph_ids(path, "2")
    assert None not in reply_ids and len(set(reply_ids)) == 2
    entries = _entries(path)
    assert entries[root_ids[1]] == (None, "0")
    assert entries[reply_ids[-1]] == (root_ids[1], "0")
    all_ids = _all_para_ids(path)
    assert len(all_ids) == len(set(all_ids))
    assert set(entries) <= set(all_ids)


def test_reply_creates_comments_extended_and_registers_it(tmp_path):
    path = _write_docx(tmp_path)
    assert not _has_part(path, "word/commentsExtended.xml")

    docx_review.reply_word_comment(path, 1, "Reply.", "Reviewer Three", "R3")

    assert _has_part(path, "word/commentsExtended.xml")
    rels = ET.fromstring(_part(path, "word/_rels/document.xml.rels"))
    relations = {(rel.get("Type"), rel.get("Target")) for rel in rels.iter(f"{{{_REL}}}Relationship")}
    assert (_EXT_REL_TYPE, "commentsExtended.xml") in relations
    types = ET.fromstring(_part(path, "[Content_Types].xml"))
    overrides = {item.get("PartName"): item.get("ContentType") for item in types.iter(f"{{{_CT}}}Override")}
    assert overrides["/word/commentsExtended.xml"] == _EXT_CONTENT_TYPE
    reply_para = _body_paragraph_ids(path, "2")[-1]
    # Comment 1 already carries 2B000002, so the reply threads onto that paragraph.
    assert _entries(path)[reply_para] == ("2B000002", "0")
    assert _entries(path)["2B000002"] == (None, "0")


def test_reply_keeps_existing_comments_extended_namespaces_and_entries(tmp_path):
    path = _write_docx(tmp_path, extended_xml=_EXTENDED_XML)

    docx_review.reply_word_comment(path, 0, "Thanks.", "Reviewer Three", "R3")

    raw = _part(path, "word/commentsExtended.xml").decode("utf-8")
    assert 'xmlns:w16cid="http://schemas.microsoft.com/office/word/2016/wordml/cid"' in raw
    assert 'mc:Ignorable="w15 w16cid"' in raw
    entries = _entries(path)
    assert entries["2B000002"] == (None, "1")  # the existing resolved thread stays resolved
    root_para = _body_paragraph_ids(path, "0")[-1]
    reply_para = _body_paragraph_ids(path, "2")[-1]
    assert entries[root_para] == (None, "0")
    assert entries[reply_para] == (root_para, "0")


def test_reply_to_a_reply_attaches_to_the_thread_root(tmp_path):
    path = _write_docx(tmp_path)
    first = docx_review.reply_word_comment(path, 0, "First reply.", "Reviewer Three", "R3")

    second = docx_review.reply_word_comment(path, first["comment_id"], "Second reply.", "Reviewer Four", "R4")

    assert second["requested_comment_id"] == first["comment_id"]
    assert second["parent_comment_id"] == "0"
    assert second["thread_root_id"] == "0"
    root_para = _body_paragraph_ids(path, "0")[-1]
    first_para = _body_paragraph_ids(path, first["comment_id"])[-1]
    second_para = _body_paragraph_ids(path, second["comment_id"])[-1]
    entries = _entries(path)
    assert entries[first_para] == (root_para, "0")
    assert entries[second_para] == (root_para, "0")
    listing = {record["comment_id"]: record for record in docx_review.list_word_comments(path)["comments"]}
    assert listing[second["comment_id"]]["parent_comment_id"] == "0"
    assert listing[second["comment_id"]]["thread_root_id"] == "0"


@pytest.mark.parametrize(
    "comment_id, text, author, kwargs, expected",
    [
        ("99", "Text.", "R3", {}, "has no comment body"),
        (True, "Text.", "R3", {}, "non-negative integer"),
        (0, "   ", "R3", {}, "text must be"),
        (0, "Text.", "  ", {}, "author must be"),
        (0, "Text.", "R3", {"allow_degraded_render": True}, "degraded_render_reason"),
    ],
)
def test_reply_refuses_bad_input_and_leaves_the_file_unchanged(
    tmp_path, comment_id, text, author, kwargs, expected
):
    path = _write_docx(tmp_path)
    before = (_part(path, "word/document.xml"), _part(path, "word/comments.xml"))

    result = docx_review.reply_word_comment(path, comment_id, text, author, "R3", **kwargs)

    assert expected in result["error"]
    assert (_part(path, "word/document.xml"), _part(path, "word/comments.xml")) == before
    assert not _has_part(path, "word/commentsExtended.xml")


def test_reply_refuses_a_comment_body_with_no_anchor(tmp_path):
    orphan = (
        '  <w:comment w:id="5" w:author="Ghost" w:initials="G" w:date="2026-10-03T08:00:00Z">'
        "<w:p><w:r><w:t>Orphan body.</w:t></w:r></w:p></w:comment>\n</w:comments>"
    )
    comments = _COMMENTS_XML.replace("</w:comments>", orphan)
    path = _write_docx(tmp_path, comments_xml=comments)
    before = _part(path, "word/comments.xml")

    result = docx_review.reply_word_comment(path, 5, "Reply.", "R3")

    assert "not anchored" in result["error"]
    assert _part(path, "word/comments.xml") == before


def test_reply_leaves_other_comments_and_document_text_untouched(tmp_path):
    path = _write_docx(tmp_path)
    texts_before = _paragraph_texts(path)
    other_before = docx_review.read_word_comment(path, "1")["comment"]

    docx_review.reply_word_comment(path, 0, "Reply.", "Reviewer Three", "R3")

    assert _paragraph_texts(path) == texts_before
    other_after = docx_review.read_word_comment(path, "1")["comment"]
    assert other_after["text"] == other_before["text"]
    assert other_after["author"] == other_before["author"]
    assert other_after["date"] == other_before["date"]
    assert other_after["anchored"] is True


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


def test_resolve_marks_the_whole_thread_from_any_member(tmp_path):
    path = _write_docx(tmp_path)
    reply = docx_review.reply_word_comment(path, 0, "Reply.", "Reviewer Three", "R3")

    resolved = docx_review.resolve_word_comment(path, reply["comment_id"])

    assert resolved["status"] == "resolved"
    assert resolved["mode"] == "resolve_comment"
    assert resolved["thread_root_id"] == "0"
    assert resolved["resolved"] is True
    root_para = _body_paragraph_ids(path, "0")[-1]
    assert _entries(path)[root_para] == (None, "1")
    listing = {record["comment_id"]: record for record in docx_review.list_word_comments(path)["comments"]}
    assert listing["0"]["resolved"] is True
    assert listing[reply["comment_id"]]["resolved"] is True
    assert listing["1"]["resolved"] is False

    reopened = docx_review.resolve_word_comment(path, "0", False)

    assert reopened["status"] == "reopened"
    assert reopened["resolved"] is False
    assert _entries(path)[root_para] == (None, "0")


def test_resolve_repeat_and_reopen_of_an_open_thread_write_nothing(tmp_path):
    path = _write_docx(tmp_path)
    comments_before = _part(path, "word/comments.xml")

    unchanged = docx_review.resolve_word_comment(path, 0, False)

    assert unchanged["status"] == "unchanged"
    assert unchanged["resolved"] is False
    assert _part(path, "word/comments.xml") == comments_before
    assert not _has_part(path, "word/commentsExtended.xml")

    first = docx_review.resolve_word_comment(path, 0, True)
    assert first["status"] == "resolved"
    after_first = _part(path, "word/commentsExtended.xml")

    again = docx_review.resolve_word_comment(path, 0, True)

    assert again["status"] == "unchanged"
    assert again["resolved"] is True
    assert _part(path, "word/commentsExtended.xml") == after_first


def test_resolve_mints_a_paragraph_id_on_a_root_that_has_none(tmp_path):
    path = _write_docx(tmp_path)
    assert _body_paragraph_ids(path, "0")[-1] is None

    docx_review.resolve_word_comment(path, 0)

    root_ids = _body_paragraph_ids(path, "0")
    assert root_ids[0] == "2B000001"
    assert root_ids[-1] not in (None, "2B000001")
    assert _entries(path)[root_ids[-1]] == (None, "1")
    assert docx_review.read_word_comment(path, "0")["comment"]["resolved"] is True


def test_resolve_rejects_bad_flags_and_unknown_ids_without_writing(tmp_path):
    path = _write_docx(tmp_path)
    comments_before = _part(path, "word/comments.xml")

    assert "must be True or False" in docx_review.resolve_word_comment(path, 0, "yes")["error"]
    assert "has no comment body" in docx_review.resolve_word_comment(path, 99)["error"]
    assert "non-negative integer" in docx_review.resolve_word_comment(path, "x")["error"]
    assert _part(path, "word/comments.xml") == comments_before
    assert not _has_part(path, "word/commentsExtended.xml")


def test_resolve_keeps_an_existing_entry_for_another_thread(tmp_path):
    path = _write_docx(tmp_path, extended_xml=_EXTENDED_XML)

    docx_review.resolve_word_comment(path, 0)

    entries = _entries(path)
    assert entries["2B000002"] == (None, "1")
    root_para = _body_paragraph_ids(path, "0")[-1]
    assert entries[root_para] == (None, "1")


# ---------------------------------------------------------------------------
# list fields and thread-preserving update
# ---------------------------------------------------------------------------


def test_list_reports_no_thread_for_plain_comments(tmp_path):
    path = _write_docx(tmp_path)

    comments = {record["comment_id"]: record for record in docx_review.list_word_comments(path)["comments"]}

    for comment_id in ("0", "1"):
        assert comments[comment_id]["parent_comment_id"] is None
        assert comments[comment_id]["thread_root_id"] == comment_id
        assert comments[comment_id]["resolved"] is False


def test_update_keeps_the_thread_link_when_the_root_body_is_rewritten(tmp_path):
    path = _write_docx(tmp_path)
    reply = docx_review.reply_word_comment(path, 0, "Reply.", "Reviewer Three", "R3")
    root_para = _body_paragraph_ids(path, "0")[-1]

    updated = docx_review.update_word_comment(path, 0, "Rewritten as one line.")

    assert updated["status"] == "updated"
    assert _body_paragraph_ids(path, "0")[-1] == root_para
    assert _entries(path)[root_para] == (None, "0")
    reply_para = _body_paragraph_ids(path, reply["comment_id"])[-1]
    assert _entries(path)[reply_para] == (root_para, "0")
    listing = {record["comment_id"]: record for record in docx_review.list_word_comments(path)["comments"]}
    assert listing[reply["comment_id"]]["parent_comment_id"] == "0"


def test_update_to_several_lines_keeps_the_thread_id_on_the_last_paragraph(tmp_path):
    path = _write_docx(tmp_path)
    docx_review.reply_word_comment(path, 0, "Reply.", "Reviewer Three", "R3")
    root_para = _body_paragraph_ids(path, "0")[-1]

    docx_review.update_word_comment(path, 0, "Line one.\nLine two.")

    ids = _body_paragraph_ids(path, "0")
    assert ids[-1] == root_para
    assert ids[0] not in (None, root_para)
    assert len(set(ids)) == len(ids)
    assert _entries(path)[root_para] == (None, "0")


def test_update_of_a_single_paragraph_root_to_two_lines_moves_the_id_to_the_last_line(tmp_path):
    path = _write_docx(tmp_path)
    docx_review.reply_word_comment(path, 1, "Reply.", "Reviewer Three", "R3")

    docx_review.update_word_comment(path, 1, "A line.\nAnother line.")

    ids = _body_paragraph_ids(path, "1")
    assert ids[-1] == "2B000002"
    assert ids[0] not in (None, "2B000002")
    reply_para = _body_paragraph_ids(path, "2")[-1]
    assert _entries(path)[reply_para] == ("2B000002", "0")


def test_insert_reply_resolve_update_round_trip(tmp_path):
    path = _write_docx(tmp_path, document_xml=_PLAIN_DOC_XML, comments_xml=None)

    inserted = docs_intel.insert_word_comment(
        path, "Initial note", anchor_para_id="3C000001", author="Adam Camerer", initials="AC"
    )
    root_id = str(inserted["comment_id"])
    reply = docx_review.reply_word_comment(path, root_id, "Reply one.", "Reviewer Three", "R3")
    assert reply["thread_root_id"] == root_id

    assert docx_review.resolve_word_comment(path, root_id)["status"] == "resolved"
    assert docx_review.update_word_comment(path, root_id, "Initial note, revised.")["status"] == "updated"

    listing = {record["comment_id"]: record for record in docx_review.list_word_comments(path)["comments"]}
    assert listing[root_id]["resolved"] is True
    assert listing[root_id]["text"] == "Initial note, revised."
    assert listing[reply["comment_id"]]["parent_comment_id"] == root_id
    assert listing[reply["comment_id"]]["resolved"] is True
    assert docx_review.list_word_comments(path)["orphan_marker_ids"] == []
