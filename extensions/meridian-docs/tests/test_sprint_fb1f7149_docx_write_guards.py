"""Regression tests for sprint item fb1f7149: lost update and ignored render failure.

The native-comment writers (update, delete, reply, resolve) build their new
package from bytes read before ``_docx_promotion_lock`` is taken. These tests pin
two guards on that path:

- An overlapping write is refused with a retryable ``conflict`` and never reverts
  the other writer's parts.
- A render that fails or is unavailable restores the file and refuses the write.
"""

from __future__ import annotations

import zipfile

import pytest

from meridian_docs import docs_intel, docx_review


@pytest.fixture(autouse=True)
def _stub_render_capability(monkeypatch):
    """Stub a successful render so these tests check write guards, not the local render backend."""
    monkeypatch.setattr(
        docs_intel.render_gate,
        "check_render_capability",
        lambda docx_path, **kwargs: {
            "status": "rendered",
            "backend": "test-stub",
            "detail": {"stub": True},
        },
    )


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

# Comment 0's last paragraph has no w14:paraId. Comment 1 carries 2B000002.
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

# Comment 1 is already resolved. Comment 0 has no thread entry yet.
_EXTENDED_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w15:commentsEx xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml" xmlns:w16cid="http://schemas.microsoft.com/office/word/2016/wordml/cid" mc:Ignorable="w15 w16cid">
  <w15:commentEx w15:paraId="2B000002" w15:done="1"/>
</w15:commentsEx>
"""

_ORIGINAL_ZERO = "Original first comment.\nSecond paragraph of the same comment."
_SUCCESS_STATUSES = {"updated", "deleted", "replied", "resolved", "reopened"}


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


def _write_docx(tmp_path, *, extended_xml=None, name="doc.docx"):
    path = str(tmp_path / name)
    extended = extended_xml is not None
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types_xml(extended))
        archive.writestr("word/document.xml", _DOC_XML)
        archive.writestr("word/_rels/document.xml.rels", _rels_xml(extended))
        archive.writestr("word/comments.xml", _COMMENTS_XML)
        if extended:
            archive.writestr("word/commentsExtended.xml", extended_xml)
    return path


def _bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def _text(path, comment_id):
    return docx_review.read_word_comment(path, comment_id)["comment"]["text"]


def _resolved(path, comment_id):
    return docx_review.read_word_comment(path, comment_id)["comment"]["resolved"]


def _interleave_once(monkeypatch, concurrent):
    """Run ``concurrent`` after the next writer has read the package and built its
    parts, but before that writer takes the promotion lock. Fires only once, so
    the concurrent writer's own promotion passes straight through.
    """
    real_commit = docx_review._commit_package_write
    fired = []

    def interleaved(*args, **kwargs):
        if not fired:
            fired.append(True)
            concurrent()
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(docx_review, "_commit_package_write", interleaved)


# ---------------------------------------------------------------------------
# Overlapping writes: the second writer must not revert the first.
# ---------------------------------------------------------------------------


def test_overlapping_update_is_refused_and_keeps_the_other_writers_edit(tmp_path, monkeypatch):
    path = _write_docx(tmp_path)
    concurrent_results = []
    _interleave_once(
        monkeypatch,
        lambda: concurrent_results.append(docx_review.update_word_comment(path, 1, "Edited by writer B.")),
    )

    result = docx_review.update_word_comment(path, 0, "Edited by writer A.")

    assert concurrent_results[0]["status"] == "updated"
    assert _text(path, 1) == "Edited by writer B."
    assert result.get("error")
    assert result.get("status") == "conflict"
    assert result.get("retryable") is True
    assert result.get("status") not in _SUCCESS_STATUSES
    assert _text(path, 0) == _ORIGINAL_ZERO

    retried = docx_review.update_word_comment(path, 0, "Edited by writer A.")
    assert retried["status"] == "updated"
    assert _text(path, 0) == "Edited by writer A."
    assert _text(path, 1) == "Edited by writer B."


def test_overlapping_reply_is_refused_and_keeps_a_concurrent_resolve(tmp_path, monkeypatch):
    path = _write_docx(tmp_path, extended_xml=_EXTENDED_XML)
    concurrent_results = []
    _interleave_once(
        monkeypatch,
        lambda: concurrent_results.append(docx_review.resolve_word_comment(path, 0, True)),
    )

    result = docx_review.reply_word_comment(path, 0, "A reply.", "Reviewer A", "RA")

    assert concurrent_results[0]["status"] == "resolved"
    assert _resolved(path, 0) is True
    assert result.get("error")
    assert result.get("status") == "conflict"
    assert result.get("retryable") is True
    assert result.get("status") not in _SUCCESS_STATUSES
    assert docx_review.list_word_comments(path)["count"] == 2

    retried = docx_review.reply_word_comment(path, 0, "A reply.", "Reviewer A", "RA")
    assert retried["status"] == "replied"
    assert docx_review.list_word_comments(path)["count"] == 3
    assert _resolved(path, 0) is True


# ---------------------------------------------------------------------------
# Render failure: the file is restored and the write is refused, never reported.
# ---------------------------------------------------------------------------

_WRITERS = {
    "update": lambda path: docx_review.update_word_comment(path, 0, "Render-gated edit."),
    "delete": lambda path: docx_review.delete_word_comment(path, 1),
    "reply": lambda path: docx_review.reply_word_comment(path, 0, "Render-gated reply.", "Reviewer A", "RA"),
    "resolve": lambda path: docx_review.resolve_word_comment(path, 0, True),
}


@pytest.mark.parametrize(
    "render_status",
    [docs_intel.render_gate.FAILED, docs_intel.render_gate.UNAVAILABLE_WITH_REASON],
)
@pytest.mark.parametrize("writer", sorted(_WRITERS))
def test_failed_render_restores_the_file_and_refuses(tmp_path, monkeypatch, writer, render_status):
    path = _write_docx(tmp_path, extended_xml=_EXTENDED_XML)
    before = _bytes(path)
    monkeypatch.setattr(
        docs_intel.render_gate,
        "check_render_capability",
        lambda docx_path, **kwargs: {"status": render_status, "reason": "render backend failed"},
    )

    result = _WRITERS[writer](path)

    assert result.get("render_status") == render_status
    assert result.get("error")
    assert result.get("file_restored") is True
    assert result.get("status") not in _SUCCESS_STATUSES
    assert _bytes(path) == before
