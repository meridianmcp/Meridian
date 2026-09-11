"""Tests for W1-C: Vancouver/numbered bibliography style support.

Covers the new ``style`` parameter on ``insert_bibliography_entry`` /
``update_bibliography_entry`` (default ``"apa"``, also accepting
``"vancouver"``) and the new ``format_vancouver_reference`` formatter.

Pure Python (stdlib + pytest) -- no mcp, no network. Bibliography functions
mutate a .docx file in place, so tests write a minimal .docx to tmp_path
first, following the same pattern as test_docs_intel_new_primitives.py.
"""
from __future__ import annotations

import io
import zipfile

from meridian_docs import docs_intel

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


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


_EMPTY_DOC_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Some body text.</w:t></w:r></w:p>
  </w:body>
</w:document>
"""

_DOC_WITH_REFERENCES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Some body text.</w:t></w:r></w:p>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>References</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""


def _extract_paragraph_texts(docx_path: str) -> list[str]:
    """Return the text content of every body paragraph, in document order."""
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(docx_path) as zf:
        xml_bytes = zf.read("word/document.xml")
    root = ET.fromstring(xml_bytes)
    body = root.find(f"{{{_W}}}body")
    texts = []
    for p in body.findall(f"{{{_W}}}p"):
        text = "".join(t.text or "" for t in p.iter(f"{{{_W}}}t"))
        texts.append(text)
    return texts


# ---------------------------------------------------------------------------
# format_vancouver_reference — pure formatter tests
# ---------------------------------------------------------------------------

class TestFormatVancouverReference:
    def test_journal_article_full(self):
        item = {
            "type": "article-journal",
            "author": [
                {"family": "Smith", "given": "John A."},
                {"family": "Doe", "given": "Jane"},
            ],
            "title": "A study of things",
            "container-title": "Journal of Studies",
            "journalAbbreviation": "J Stud",
            "volume": "10",
            "issue": "2",
            "page": "100-110",
            "issued": {"date-parts": [[2023]]},
        }
        result = docs_intel.format_vancouver_reference(item)
        assert result == (
            "Smith JA, Doe J. A study of things. J Stud. 2023;10(2):100-110."
        )

    def test_journal_article_prefers_abbreviation_over_container_title(self):
        item = {
            "type": "journalArticle",
            "author": [{"family": "Lee", "given": "K"}],
            "title": "Title here",
            "container-title": "Full Journal Name",
            "journalAbbreviation": "Full J Name",
            "volume": "5",
            "issued": {"date-parts": [[2021]]},
        }
        result = docs_intel.format_vancouver_reference(item)
        assert "Full J Name" in result
        assert "Full Journal Name" not in result

    def test_journal_article_no_volume_or_issue(self):
        item = {
            "type": "article-journal",
            "author": [{"family": "Ng", "given": "P"}],
            "title": "Minimal article",
            "container-title": "Some Journal",
            "issued": {"date-parts": [[2020]]},
        }
        result = docs_intel.format_vancouver_reference(item)
        assert result == "Ng P. Minimal article. Some Journal. 2020."

    def test_author_more_than_six_uses_et_al(self):
        authors = [
            {"family": f"Author{i}", "given": "X"} for i in range(1, 9)
        ]
        item = {
            "type": "article-journal",
            "author": authors,
            "title": "Many authors",
            "container-title": "J",
            "issued": {"date-parts": [[2022]]},
        }
        result = docs_intel.format_vancouver_reference(item)
        assert result.startswith(
            "Author1 X, Author2 X, Author3 X, Author4 X, Author5 X, "
            "Author6 X, et al."
        )
        assert "Author7" not in result
        assert "Author8" not in result

    def test_author_exactly_six_lists_all_no_et_al(self):
        authors = [{"family": f"A{i}", "given": "X"} for i in range(1, 7)]
        item = {
            "type": "article-journal",
            "author": authors,
            "title": "Six authors",
            "container-title": "J",
            "issued": {"date-parts": [[2019]]},
        }
        result = docs_intel.format_vancouver_reference(item)
        assert "et al." not in result
        for i in range(1, 7):
            assert f"A{i} X" in result

    def test_no_authors_falls_back_to_unknown_author(self):
        item = {
            "type": "article-journal",
            "title": "Anonymous work",
            "container-title": "J",
            "issued": {"date-parts": [[2018]]},
        }
        result = docs_intel.format_vancouver_reference(item)
        assert result.startswith("Unknown Author.")

    def test_corporate_literal_author(self):
        item = {
            "type": "article-journal",
            "author": [{"literal": "World Health Organization"}],
            "title": "Global report",
            "container-title": "WHO Bulletin",
            "issued": {"date-parts": [[2021]]},
        }
        result = docs_intel.format_vancouver_reference(item)
        assert result.startswith("World Health Organization.")

    def test_book(self):
        item = {
            "type": "book",
            "author": [{"family": "Osler", "given": "William"},],
            "title": "The Principles and Practice of Medicine",
            "publisher": "Appleton",
            "publisher-place": "New York",
            "edition": "3rd",
            "issued": {"date-parts": [[1898]]},
        }
        result = docs_intel.format_vancouver_reference(item)
        assert result == (
            "Osler W. The Principles and Practice of Medicine. 3rd ed. "
            "New York: Appleton; 1898."
        )

    def test_book_no_edition_no_place(self):
        item = {
            "type": "book",
            "author": [{"family": "Brown", "given": "B"}],
            "title": "A Book",
            "publisher": "Pub Co",
            "issued": {"date-parts": [[2000]]},
        }
        result = docs_intel.format_vancouver_reference(item)
        assert result == "Brown B. A Book. Pub Co; 2000."

    def test_book_chapter(self):
        item = {
            "type": "chapter",
            "author": [{"family": "Chen", "given": "L"}],
            "title": "A chapter title",
            "editor": [{"family": "Editor", "given": "E"}],
            "container-title": "The Big Book",
            "publisher": "Pub Co",
            "publisher-place": "Boston",
            "page": "45-60",
            "issued": {"date-parts": [[2015]]},
        }
        result = docs_intel.format_vancouver_reference(item)
        assert result == (
            "Chen L. A chapter title. In: Editor E, editor. The Big Book. "
            "Boston: Pub Co; 2015. p. 45-60."
        )

    def test_book_chapter_multiple_editors_uses_plural(self):
        item = {
            "type": "bookSection",
            "author": [{"family": "Chen", "given": "L"}],
            "title": "Ch",
            "editor": [
                {"family": "Ed1", "given": "A"},
                {"family": "Ed2", "given": "B"},
            ],
            "container-title": "Book",
            "issued": {"date-parts": [[2010]]},
        }
        result = docs_intel.format_vancouver_reference(item)
        assert "Ed1 A, Ed2 B, editors." in result

    def test_conference_paper(self):
        item = {
            "type": "paper-conference",
            "author": [{"family": "Kim", "given": "S"}],
            "title": "Paper title",
            "container-title": "Proc. of the Big Conf",
            "publisher": "ACM",
            "page": "1-9",
            "issued": {"date-parts": [[2022]]},
        }
        result = docs_intel.format_vancouver_reference(item)
        assert result == (
            "Kim S. Paper title. In: Proc. of the Big Conf; ACM; 2022. p. 1-9."
        )

    def test_unrecognised_type_minimal_fallback(self):
        item = {
            "type": "report",
            "author": [{"family": "Doe", "given": "J"}],
            "title": "A Report",
            "issued": {"date-parts": [[2024]]},
        }
        result = docs_intel.format_vancouver_reference(item)
        assert result == "Doe J. A Report. 2024."

    def test_doi_appended(self):
        item = {
            "type": "article-journal",
            "author": [{"family": "Doe", "given": "J"}],
            "title": "T",
            "container-title": "J",
            "issued": {"date-parts": [[2020]]},
            "DOI": "10.1000/xyz",
        }
        result = docs_intel.format_vancouver_reference(item)
        assert result.endswith("https://doi.org/10.1000/xyz")

    def test_no_reference_number_prefix(self):
        """The formatter never emits a leading '1.'/'2.' number -- callers
        cannot determine citation-appearance order from a single entry."""
        item = {
            "type": "article-journal",
            "author": [{"family": "Doe", "given": "J"}],
            "title": "T",
            "container-title": "J",
            "issued": {"date-parts": [[2020]]},
        }
        result = docs_intel.format_vancouver_reference(item)
        assert not result[0].isdigit()

    def test_non_dict_input_returns_empty_string(self):
        assert docs_intel.format_vancouver_reference(None) == ""
        assert docs_intel.format_vancouver_reference("not a dict") == ""
        assert docs_intel.format_vancouver_reference([1, 2, 3]) == ""

    def test_never_raises_on_malformed_item(self):
        # author is not a list, issued is malformed, title missing.
        item = {"type": "article-journal", "author": "not a list", "issued": 12345}
        result = docs_intel.format_vancouver_reference(item)
        assert isinstance(result, str)
        assert "Unknown Author" in result
        assert "Untitled" in result


# ---------------------------------------------------------------------------
# insert_bibliography_entry — style parameter
# ---------------------------------------------------------------------------

_JOURNAL_ITEM = {
    "type": "article-journal",
    "author": [{"family": "Zed", "given": "A"}],
    "title": "Zed's paper",
    "container-title": "Z Journal",
    "volume": "1",
    "issue": "1",
    "page": "1-2",
    "issued": {"date-parts": [[2020]]},
}

_JOURNAL_ITEM_ALPHA_FIRST = {
    "type": "article-journal",
    "author": [{"family": "Abel", "given": "A"}],
    "title": "Abel's paper",
    "container-title": "A Journal",
    "issued": {"date-parts": [[2020]]},
}


class TestInsertBibliographyEntryStyle:
    def test_default_style_is_apa_and_unchanged(self, tmp_path):
        """No style arg -> APA formatting, exactly as before style existed."""
        path = _write_docx(tmp_path, _DOC_WITH_REFERENCES_XML)
        result = docs_intel.insert_bibliography_entry(
            path, "key1", _JOURNAL_ITEM
        )
        assert result["status"] == "inserted"
        expected_apa = docs_intel.format_apa_reference(_JOURNAL_ITEM)
        assert result["formatted_text"] == expected_apa

    def test_explicit_apa_style_matches_default(self, tmp_path):
        path_default = _write_docx(tmp_path, _DOC_WITH_REFERENCES_XML, "a.docx")
        path_explicit = _write_docx(tmp_path, _DOC_WITH_REFERENCES_XML, "b.docx")

        r1 = docs_intel.insert_bibliography_entry(path_default, "key1", _JOURNAL_ITEM)
        r2 = docs_intel.insert_bibliography_entry(
            path_explicit, "key1", _JOURNAL_ITEM, style="apa"
        )
        assert r1["formatted_text"] == r2["formatted_text"]
        with open(path_default, "rb") as f1, open(path_explicit, "rb") as f2:
            assert f1.read() == f2.read()

    def test_vancouver_style_uses_vancouver_formatter(self, tmp_path):
        path = _write_docx(tmp_path, _DOC_WITH_REFERENCES_XML)
        result = docs_intel.insert_bibliography_entry(
            path, "key1", _JOURNAL_ITEM, style="vancouver"
        )
        assert result["status"] == "inserted"
        expected_vancouver = docs_intel.format_vancouver_reference(_JOURNAL_ITEM)
        assert result["formatted_text"] == expected_vancouver
        # Sanity: Vancouver and APA formatting genuinely differ for this item.
        assert result["formatted_text"] != docs_intel.format_apa_reference(_JOURNAL_ITEM)

    def test_vancouver_style_appends_at_end_not_alphabetical(self, tmp_path):
        """Insert an entry that would sort FIRST alphabetically (APA order)
        after one that would sort LAST -- with style="vancouver" the second
        entry must land AFTER the first (append order), not before it."""
        path = _write_docx(tmp_path, _DOC_WITH_REFERENCES_XML)

        r1 = docs_intel.insert_bibliography_entry(
            path, "zed", _JOURNAL_ITEM, style="vancouver"
        )
        r2 = docs_intel.insert_bibliography_entry(
            path, "abel", _JOURNAL_ITEM_ALPHA_FIRST, style="vancouver"
        )
        assert r1["status"] == "inserted"
        assert r2["status"] == "inserted"

        texts = _extract_paragraph_texts(path)
        zed_text = docs_intel.format_vancouver_reference(_JOURNAL_ITEM)
        abel_text = docs_intel.format_vancouver_reference(_JOURNAL_ITEM_ALPHA_FIRST)
        zed_idx = texts.index(zed_text)
        abel_idx = texts.index(abel_text)
        # abel was inserted SECOND -> must appear AFTER zed (append, not
        # alphabetical -- APA insertion would have put abel BEFORE zed).
        assert abel_idx > zed_idx

    def test_apa_style_still_inserts_alphabetically(self, tmp_path):
        """Control case: confirms the alphabetical-insert behaviour used for
        comparison above is genuinely exercised for style="apa"."""
        path = _write_docx(tmp_path, _DOC_WITH_REFERENCES_XML)

        docs_intel.insert_bibliography_entry(path, "zed", _JOURNAL_ITEM, style="apa")
        docs_intel.insert_bibliography_entry(
            path, "abel", _JOURNAL_ITEM_ALPHA_FIRST, style="apa"
        )

        texts = _extract_paragraph_texts(path)
        zed_text = docs_intel.format_apa_reference(_JOURNAL_ITEM)
        abel_text = docs_intel.format_apa_reference(_JOURNAL_ITEM_ALPHA_FIRST)
        zed_idx = texts.index(zed_text)
        abel_idx = texts.index(abel_text)
        # abel sorts before zed alphabetically -> APA insert moves it earlier.
        assert abel_idx < zed_idx

    def test_vancouver_style_creates_heading_when_missing(self, tmp_path):
        path = _write_docx(tmp_path, _EMPTY_DOC_XML)
        result = docs_intel.insert_bibliography_entry(
            path, "key1", _JOURNAL_ITEM, style="vancouver"
        )
        assert result["status"] == "inserted"
        texts = _extract_paragraph_texts(path)
        assert "References" in texts

    def test_invalid_style_returns_error_and_does_not_mutate(self, tmp_path):
        path = _write_docx(tmp_path, _DOC_WITH_REFERENCES_XML)
        with open(path, "rb") as f:
            before = f.read()
        result = docs_intel.insert_bibliography_entry(
            path, "key1", _JOURNAL_ITEM, style="chicago"
        )
        assert "error" in result
        assert "chicago" in result["error"]
        with open(path, "rb") as f:
            after = f.read()
        assert before == after

    def test_style_is_case_insensitive(self, tmp_path):
        path = _write_docx(tmp_path, _DOC_WITH_REFERENCES_XML)
        result = docs_intel.insert_bibliography_entry(
            path, "key1", _JOURNAL_ITEM, style="VANCOUVER"
        )
        assert result["status"] == "inserted"
        assert result["formatted_text"] == docs_intel.format_vancouver_reference(
            _JOURNAL_ITEM
        )


# ---------------------------------------------------------------------------
# update_bibliography_entry — style parameter
# ---------------------------------------------------------------------------

class TestUpdateBibliographyEntryStyle:
    def test_default_style_is_apa_and_unchanged(self, tmp_path):
        path = _write_docx(tmp_path, _DOC_WITH_REFERENCES_XML)
        docs_intel.insert_bibliography_entry(path, "key1", _JOURNAL_ITEM, style="apa")

        updated_item = dict(_JOURNAL_ITEM, title="Zed's revised paper")
        result = docs_intel.update_bibliography_entry(path, "key1", updated_item)
        assert result["status"] == "updated"
        assert result["formatted_text"] == docs_intel.format_apa_reference(updated_item)

    def test_vancouver_style_reformats_with_vancouver_formatter(self, tmp_path):
        path = _write_docx(tmp_path, _DOC_WITH_REFERENCES_XML)
        docs_intel.insert_bibliography_entry(
            path, "key1", _JOURNAL_ITEM, style="vancouver"
        )

        updated_item = dict(_JOURNAL_ITEM, title="Zed's revised paper")
        result = docs_intel.update_bibliography_entry(
            path, "key1", updated_item, style="vancouver"
        )
        assert result["status"] == "updated"
        expected = docs_intel.format_vancouver_reference(updated_item)
        assert result["formatted_text"] == expected

        texts = _extract_paragraph_texts(path)
        assert expected in texts

    def test_invalid_style_returns_error_and_does_not_mutate(self, tmp_path):
        path = _write_docx(tmp_path, _DOC_WITH_REFERENCES_XML)
        docs_intel.insert_bibliography_entry(path, "key1", _JOURNAL_ITEM, style="apa")
        with open(path, "rb") as f:
            before = f.read()

        result = docs_intel.update_bibliography_entry(
            path, "key1", _JOURNAL_ITEM, style="mla"
        )
        assert "error" in result
        assert "mla" in result["error"]
        with open(path, "rb") as f:
            after = f.read()
        assert before == after

    def test_missing_entry_still_errors_before_style_check_matters(self, tmp_path):
        path = _write_docx(tmp_path, _DOC_WITH_REFERENCES_XML)
        result = docs_intel.update_bibliography_entry(
            path, "nonexistent", _JOURNAL_ITEM, style="vancouver"
        )
        assert "error" in result
        assert "nonexistent" in result["error"]
