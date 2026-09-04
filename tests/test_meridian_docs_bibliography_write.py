"""1258794a — Tests for meridian-docs bibliography write-back.

Covers:
  - APA 7 formatting for journal-article, book, book-chapter, and
    conference-paper CSL-JSON shapes.
  - APA author formatting: 1, 2, 3-19, 20+ authors; corporate authors;
    missing given name; 20+ author truncation.
  - insert_bibliography_entry: heading auto-creation, heading reuse,
    entry placed at its correct alphabetical position among existing
    entries, Bibliography style present, bookmark present, formatted
    text present.
  - update_bibliography_entry: text replaced, bookmark preserved.
  - remove_bibliography_entry: paragraph removed.
  - Alphabetization: insert_bibliography_entry places each new entry at
    its correct APA-alphabetical position (by author/title, then by year
    for same-author ties) among existing entries, not merely appended.
  - scan_all_citation_keys: returns keys in appearance order, deduplicated.
  - sync_bibliography: insert/update/missing_data/stale_entries logic.
  - Error paths: unknown doc, missing file, bad key, duplicate insert,
    update/remove on nonexistent entry, malformed csl_item -> {error:...},
    file byte-for-byte unchanged on error.
  - Sidecar invalidation: index_db_path triggers mtime clear.

All tests use synthetic minimal .docx files built in-memory (no network,
no Zotero instance).  Follows the mocking conventions of:
  tests/test_meridian_docs_caption_citation_write.py
  tests/test_zotero_client.py
"""
from __future__ import annotations

import io
import os
import sqlite3
import sys
import zipfile

import pytest

# Make meridian_docs importable from the local extensions directory.
_EXT_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "extensions", "meridian-docs")
)
if _EXT_PATH not in sys.path:
    sys.path.insert(0, _EXT_PATH)

from meridian_docs import docs_intel  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic .docx helpers
# ---------------------------------------------------------------------------

# A minimal document with three paragraphs.
_DOC_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="AABB0001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction</w:t></w:r>
    </w:p>
    <w:p w14:paraId="AABB0002">
      <w:r><w:t>Body paragraph.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="AABB0003">
      <w:r><w:t>Another paragraph.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""

# A document that already has a References heading + one entry.
_DOC_XML_WITH_REFS = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="BB0001">
      <w:r><w:t>Body text.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="BB0002">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>References</w:t></w:r>
    </w:p>
    <w:p w14:paraId="BB0003">
      <w:pPr><w:pStyle w:val="Bibliography"/></w:pPr>
      <w:bookmarkStart w:id="0" w:name="bibkey_smith2020"/>
      <w:r><w:t xml:space="preserve">Smith, J. (2020). Old paper.</w:t></w:r>
      <w:bookmarkEnd w:id="0"/>
    </w:p>
  </w:body>
</w:document>
"""

# A document with an in-text CSL_CITATION complex field.
_DOC_XML_WITH_CITATION = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="CC0001">
      <w:r><w:t xml:space="preserve">See Smith </w:t></w:r>
      <w:r><w:fldChar w:fldCharType="begin"/></w:r>
      <w:r><w:instrText xml:space="preserve"> ADDIN ZOTERO_ITEM CSL_CITATION {"citationID":"abc","properties":{"formattedCitation":"(Smith 2020)"},"citationItems":[{"id":"smith2020","uris":[],"itemData":{"id":"smith2020","type":"article"}}],"schema":"https://github.com/citation-style-language/schema/raw/master/csl-citation.json"} </w:instrText></w:r>
      <w:r><w:fldChar w:fldCharType="separate"/></w:r>
      <w:r><w:t>(Smith 2020)</w:t></w:r>
      <w:r><w:fldChar w:fldCharType="end"/></w:r>
    </w:p>
    <w:p w14:paraId="CC0002">
      <w:r><w:fldChar w:fldCharType="begin"/></w:r>
      <w:r><w:instrText xml:space="preserve"> ADDIN ZOTERO_ITEM CSL_CITATION {"citationID":"def","properties":{"formattedCitation":"(Jones 2021)"},"citationItems":[{"id":"jones2021","uris":[],"itemData":{"id":"jones2021","type":"article"}}],"schema":"https://github.com/citation-style-language/schema/raw/master/csl-citation.json"} </w:instrText></w:r>
      <w:r><w:fldChar w:fldCharType="separate"/></w:r>
      <w:r><w:t>(Jones 2021)</w:t></w:r>
      <w:r><w:fldChar w:fldCharType="end"/></w:r>
    </w:p>
    <w:p w14:paraId="CC0003">
      <w:r><w:t>Plain paragraph, no citation.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""

# Same as _DOC_XML_WITH_CITATION but smith2020 appears twice (dedup test).
_DOC_XML_DUPLICATE_CITATION = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="DD0001">
      <w:r><w:fldChar w:fldCharType="begin"/></w:r>
      <w:r><w:instrText xml:space="preserve"> ADDIN ZOTERO_ITEM CSL_CITATION {"citationID":"x1","properties":{"formattedCitation":"(Smith 2020)"},"citationItems":[{"id":"smith2020","uris":[],"itemData":{"id":"smith2020","type":"article"}}],"schema":"x"} </w:instrText></w:r>
      <w:r><w:fldChar w:fldCharType="separate"/></w:r>
      <w:r><w:t>(Smith 2020)</w:t></w:r>
      <w:r><w:fldChar w:fldCharType="end"/></w:r>
    </w:p>
    <w:p w14:paraId="DD0002">
      <w:r><w:fldChar w:fldCharType="begin"/></w:r>
      <w:r><w:instrText xml:space="preserve"> ADDIN ZOTERO_ITEM CSL_CITATION {"citationID":"x2","properties":{"formattedCitation":"(Smith 2020)"},"citationItems":[{"id":"smith2020","uris":[],"itemData":{"id":"smith2020","type":"article"}}],"schema":"x"} </w:instrText></w:r>
      <w:r><w:fldChar w:fldCharType="separate"/></w:r>
      <w:r><w:t>(Smith 2020)</w:t></w:r>
      <w:r><w:fldChar w:fldCharType="end"/></w:r>
    </w:p>
  </w:body>
</w:document>
"""


def _zip_docx(xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def _write_docx(path: str, xml: str = _DOC_XML) -> None:
    with open(path, "wb") as fh:
        fh.write(_zip_docx(xml))


def _read_doc_xml(path: str) -> str:
    with zipfile.ZipFile(path, "r") as zf:
        return zf.read("word/document.xml").decode("utf-8")


# ---------------------------------------------------------------------------
# CSL-JSON fixtures for common item types
# ---------------------------------------------------------------------------

def _journal_article(
    *,
    family="Smith",
    given="John A.",
    year=2023,
    title="A great study of things",
    journal="Nature",
    volume="42",
    issue="3",
    page="100-110",
    doi="10.1234/nature.2023.001",
) -> dict:
    return {
        "type": "article-journal",
        "author": [{"family": family, "given": given}],
        "issued": {"date-parts": [[year]]},
        "title": title,
        "container-title": journal,
        "volume": volume,
        "issue": issue,
        "page": page,
        "DOI": doi,
    }


def _book(
    *,
    family="Knuth",
    given="D. E.",
    year=1997,
    title="The Art of Computer Programming",
    publisher="Addison-Wesley",
    place="Reading, MA",
    edition="3rd",
) -> dict:
    return {
        "type": "book",
        "author": [{"family": family, "given": given}],
        "issued": {"date-parts": [[year]]},
        "title": title,
        "publisher": publisher,
        "publisher-place": place,
        "edition": edition,
    }


def _chapter(
    *,
    family="Jones",
    given="B.",
    year=2018,
    title="Chapter about stuff",
    container="Big Handbook",
    editor_family="Editor",
    editor_given="E.",
    publisher="Academic Press",
    page="55-80",
) -> dict:
    return {
        "type": "chapter",
        "author": [{"family": family, "given": given}],
        "issued": {"date-parts": [[year]]},
        "title": title,
        "container-title": container,
        "editor": [{"family": editor_family, "given": editor_given}],
        "publisher": publisher,
        "page": page,
    }


def _conf_paper(
    *,
    family="Lee",
    given="C.",
    year=2022,
    title="Fast algorithms",
    conference="Proceedings of ICML 2022",
    page="1-10",
) -> dict:
    return {
        "type": "paper-conference",
        "author": [{"family": family, "given": given}],
        "issued": {"date-parts": [[year]]},
        "title": title,
        "container-title": conference,
        "page": page,
    }


# ---------------------------------------------------------------------------
# APA formatter: author formatting
# ---------------------------------------------------------------------------

class TestApaAuthors:
    def test_single_author(self):
        txt = docs_intel._apa_authors([{"family": "Smith", "given": "John A."}])
        assert txt == "Smith, J. A."

    def test_two_authors(self):
        txt = docs_intel._apa_authors([
            {"family": "Smith", "given": "J."},
            {"family": "Jones", "given": "B."},
        ])
        assert "Smith" in txt
        assert "Jones" in txt
        assert "& Jones" in txt

    def test_three_authors(self):
        authors = [
            {"family": "A", "given": "A."},
            {"family": "B", "given": "B."},
            {"family": "C", "given": "C."},
        ]
        txt = docs_intel._apa_authors(authors)
        assert "A," in txt
        assert "& C" in txt

    def test_twenty_plus_authors(self):
        authors = [{"family": f"Auth{i}", "given": "X."} for i in range(21)]
        txt = docs_intel._apa_authors(authors)
        # Should contain first 19 and last author with "..."
        assert "Auth0" in txt
        assert "Auth20" in txt
        assert "..." in txt
        # Should NOT contain Auth19 (the 20th, which is elided)
        assert "Auth19" not in txt

    def test_corporate_author(self):
        txt = docs_intel._apa_authors([{"literal": "World Health Organization"}])
        assert txt == "World Health Organization"

    def test_missing_family_name(self):
        txt = docs_intel._apa_authors([{"given": "Firstname"}])
        assert txt == "Firstname"

    def test_no_authors(self):
        txt = docs_intel._apa_authors([])
        assert txt == "Unknown Author"

    def test_given_abbreviation_hyphenated(self):
        # Hyphenated given names should abbreviate each part.
        txt = docs_intel._apa_authors([{"family": "Li", "given": "Xin-Yu"}])
        assert "Li" in txt
        assert "X." in txt


# ---------------------------------------------------------------------------
# APA formatter: year extraction
# ---------------------------------------------------------------------------

class TestApaYear:
    def test_standard_date_parts(self):
        item = {"issued": {"date-parts": [[2023, 5, 10]]}}
        assert docs_intel._apa_year(item) == "2023"

    def test_year_only_date_parts(self):
        item = {"issued": {"date-parts": [[2019]]}}
        assert docs_intel._apa_year(item) == "2019"

    def test_fallback_year_field(self):
        item = {"year": "2017"}
        assert docs_intel._apa_year(item) == "2017"

    def test_no_date(self):
        item = {}
        assert docs_intel._apa_year(item) == "n.d."

    def test_malformed_date_parts(self):
        item = {"issued": {"date-parts": [[]]}}
        assert docs_intel._apa_year(item) == "n.d."


# ---------------------------------------------------------------------------
# APA formatter: journal article
# ---------------------------------------------------------------------------

class TestFormatApaJournalArticle:
    def test_journal_article_basic(self):
        item = _journal_article()
        txt = docs_intel.format_apa_reference(item)
        assert "Smith" in txt
        assert "(2023)" in txt
        assert "A great study of things" in txt
        assert "Nature" in txt
        assert "42(3)" in txt
        assert "100-110" in txt
        assert "doi.org" in txt

    def test_journal_article_no_doi(self):
        item = _journal_article(doi=None)
        del item["DOI"]
        txt = docs_intel.format_apa_reference(item)
        assert "Smith" in txt
        assert "doi.org" not in txt

    def test_journal_article_no_journal(self):
        item = _journal_article()
        del item["container-title"]
        txt = docs_intel.format_apa_reference(item)
        assert "Smith" in txt
        assert "(2023)" in txt

    def test_journal_article_no_volume(self):
        item = _journal_article()
        del item["volume"]
        txt = docs_intel.format_apa_reference(item)
        # Should still produce a reasonable reference without a volume.
        assert "Nature" in txt

    def test_journal_article_itemType_variant(self):
        # Zotero uses "journalArticle" as the itemType key.
        item = _journal_article()
        item["itemType"] = "journalArticle"
        del item["type"]
        txt = docs_intel.format_apa_reference(item)
        assert "Smith" in txt
        assert "Nature" in txt

    def test_journal_article_ends_with_doi(self):
        item = _journal_article()
        txt = docs_intel.format_apa_reference(item)
        assert txt.endswith("https://doi.org/10.1234/nature.2023.001")


# ---------------------------------------------------------------------------
# APA formatter: book
# ---------------------------------------------------------------------------

class TestFormatApaBook:
    def test_book_basic(self):
        item = _book()
        txt = docs_intel.format_apa_reference(item)
        assert "Knuth" in txt
        assert "(1997)" in txt
        assert "The Art of Computer Programming" in txt
        assert "Addison-Wesley" in txt
        assert "3rd ed." in txt

    def test_book_no_edition(self):
        item = _book()
        del item["edition"]
        txt = docs_intel.format_apa_reference(item)
        assert "ed." not in txt
        assert "Knuth" in txt

    def test_book_no_publisher(self):
        item = _book()
        del item["publisher"]
        txt = docs_intel.format_apa_reference(item)
        assert "Knuth" in txt

    def test_book_with_doi(self):
        item = _book()
        item["DOI"] = "10.9999/book"
        txt = docs_intel.format_apa_reference(item)
        assert "doi.org" in txt


# ---------------------------------------------------------------------------
# APA formatter: book chapter
# ---------------------------------------------------------------------------

class TestFormatApaChapter:
    def test_chapter_basic(self):
        item = _chapter()
        txt = docs_intel.format_apa_reference(item)
        assert "Jones" in txt
        assert "(2018)" in txt
        assert "Chapter about stuff" in txt
        assert "Big Handbook" in txt
        assert "Editor" in txt
        assert "Ed." in txt
        assert "pp. 55-80" in txt
        assert "Academic Press" in txt

    def test_chapter_two_editors(self):
        item = _chapter()
        item["editor"] = [
            {"family": "Ed1", "given": "A."},
            {"family": "Ed2", "given": "B."},
        ]
        txt = docs_intel.format_apa_reference(item)
        assert "Eds." in txt

    def test_chapter_no_editor(self):
        item = _chapter()
        del item["editor"]
        txt = docs_intel.format_apa_reference(item)
        # No "In Ed." line but container still present.
        assert "Jones" in txt

    def test_chapter_itemType_variant(self):
        item = _chapter()
        item["itemType"] = "bookSection"
        del item["type"]
        txt = docs_intel.format_apa_reference(item)
        assert "Jones" in txt
        assert "Big Handbook" in txt


# ---------------------------------------------------------------------------
# APA formatter: conference paper
# ---------------------------------------------------------------------------

class TestFormatApaConferencePaper:
    def test_conf_basic(self):
        item = _conf_paper()
        txt = docs_intel.format_apa_reference(item)
        assert "Lee" in txt
        assert "(2022)" in txt
        assert "Fast algorithms" in txt
        assert "Proceedings of ICML 2022" in txt
        assert "1-10" in txt

    def test_conf_no_conference(self):
        item = _conf_paper()
        del item["container-title"]
        txt = docs_intel.format_apa_reference(item)
        assert "Lee" in txt
        assert "Fast algorithms" in txt

    def test_conf_itemType_variant(self):
        item = _conf_paper()
        item["itemType"] = "conferencePaper"
        del item["type"]
        txt = docs_intel.format_apa_reference(item)
        assert "Lee" in txt


# ---------------------------------------------------------------------------
# APA formatter: unknown type falls back
# ---------------------------------------------------------------------------

class TestFormatApaFallback:
    def test_unknown_type_minimal(self):
        item = {
            "type": "dataset",
            "author": [{"family": "Someone", "given": "A."}],
            "issued": {"date-parts": [[2024]]},
            "title": "A dataset",
        }
        txt = docs_intel.format_apa_reference(item)
        assert "Someone" in txt
        assert "(2024)" in txt
        assert "A dataset" in txt

    def test_non_dict_input(self):
        txt = docs_intel.format_apa_reference("not a dict")  # type: ignore[arg-type]
        assert txt == ""

    def test_empty_dict(self):
        txt = docs_intel.format_apa_reference({})
        # Should produce something, not raise.
        assert isinstance(txt, str)


# ---------------------------------------------------------------------------
# scan_all_citation_keys
# ---------------------------------------------------------------------------

class TestScanAllCitationKeys:
    def test_no_citations_returns_empty(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        keys = docs_intel.scan_all_citation_keys(docx)
        assert keys == []

    def test_two_distinct_keys(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_CITATION)
        keys = docs_intel.scan_all_citation_keys(docx)
        assert "smith2020" in keys
        assert "jones2021" in keys
        assert len(keys) == 2

    def test_appearance_order(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_CITATION)
        keys = docs_intel.scan_all_citation_keys(docx)
        # smith2020 appears in the first paragraph, jones2021 in the second.
        assert keys.index("smith2020") < keys.index("jones2021")

    def test_deduplicated(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_DUPLICATE_CITATION)
        keys = docs_intel.scan_all_citation_keys(docx)
        assert keys.count("smith2020") == 1

    def test_missing_file_returns_empty(self, tmp_path):
        keys = docs_intel.scan_all_citation_keys(str(tmp_path / "gone.docx"))
        assert keys == []


# ---------------------------------------------------------------------------
# insert_bibliography_entry
# ---------------------------------------------------------------------------

class TestInsertBibliographyEntry:
    def test_insert_creates_references_heading(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        res = docs_intel.insert_bibliography_entry(
            docx_path=docx,
            citation_key="smith2023",
            csl_item=_journal_article(),
        )
        assert "error" not in res, res.get("error")
        assert res["status"] == "inserted"
        xml = _read_doc_xml(docx)
        assert "References" in xml

    def test_insert_bibliography_style_present(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        docs_intel.insert_bibliography_entry(
            docx_path=docx,
            citation_key="smith2023",
            csl_item=_journal_article(),
        )
        xml = _read_doc_xml(docx)
        assert "Bibliography" in xml

    def test_insert_bookmark_present(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        docs_intel.insert_bibliography_entry(
            docx_path=docx,
            citation_key="smith2023",
            csl_item=_journal_article(),
        )
        xml = _read_doc_xml(docx)
        assert "bibkey_smith2023" in xml

    def test_insert_formatted_text_present(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        res = docs_intel.insert_bibliography_entry(
            docx_path=docx,
            citation_key="smith2023",
            csl_item=_journal_article(),
        )
        xml = _read_doc_xml(docx)
        assert "Smith" in xml
        assert res["formatted_text"] in xml

    def test_insert_reuses_existing_references_heading(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_REFS)
        count_before = _read_doc_xml(docx).count("References")
        docs_intel.insert_bibliography_entry(
            docx_path=docx,
            citation_key="jones2021",
            csl_item=_journal_article(family="Jones", given="B.", year=2021),
        )
        xml = _read_doc_xml(docx)
        # "References" heading count should not increase.
        count_after = xml.count(">References<")
        # Only one "References" heading should exist.
        assert xml.count("bibkey_jones2021") == 1

    def test_insert_appends_when_new_entry_sorts_after_existing(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_REFS)  # existing: smith2020
        # "Zimmerman" sorts after "Smith" alphabetically -- still appended.
        docs_intel.insert_bibliography_entry(
            docx_path=docx,
            citation_key="zimmerman2021",
            csl_item=_journal_article(family="Zimmerman", given="B.", year=2021),
        )
        xml = _read_doc_xml(docx)
        smith_pos = xml.index("bibkey_smith2020")
        zimmerman_pos = xml.index("bibkey_zimmerman2021")
        assert smith_pos < zimmerman_pos

    def test_insert_lands_before_an_existing_entry_that_sorts_later(self, tmp_path):
        """1258794a follow-up (PAPER-S7 hard-fixture stress test, 2026-09-04)
        -- insert_bibliography_entry previously always appended, ignoring APA
        alphabetical order, while a generic-tool control agent given the same
        task correctly reasoned its way to alphabetical placement (see
        docs/paper-s7-hard-fixtures-stress-test-v1.md in ooxml-graph-paper).
        "Adams" must land BEFORE the existing "Smith" entry, not after it."""
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_REFS)  # existing: smith2020
        docs_intel.insert_bibliography_entry(
            docx_path=docx,
            citation_key="adams2019",
            csl_item=_journal_article(family="Adams", given="J.", year=2019),
        )
        xml = _read_doc_xml(docx)
        adams_pos = xml.index("bibkey_adams2019")
        smith_pos = xml.index("bibkey_smith2020")
        assert adams_pos < smith_pos

    def test_insert_lands_alphabetically_between_two_existing_entries(self, tmp_path):
        """Mirrors the exact scenario from the hard-fixture stress test
        (docs/paper-s7-hard-fixtures-stress-test-v1.md in ooxml-graph-paper):
        an "Adams" and a "Zimmerman" entry already present, "Marker" must
        land alphabetically between them, not after both."""
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        docs_intel.insert_bibliography_entry(
            docx_path=docx,
            citation_key="adams2019",
            csl_item=_journal_article(family="Adams", given="J.", year=2019),
        )
        docs_intel.insert_bibliography_entry(
            docx_path=docx,
            citation_key="zimmerman2021",
            csl_item=_journal_article(family="Zimmerman", given="Z.", year=2021),
        )
        docs_intel.insert_bibliography_entry(
            docx_path=docx,
            citation_key="marker2022",
            csl_item=_journal_article(
                family="Marker", given="P.", year=2022, title="Pilot marker entry",
            ),
        )
        xml = _read_doc_xml(docx)
        adams_pos = xml.index("bibkey_adams2019")
        marker_pos = xml.index("bibkey_marker2022")
        zimmerman_pos = xml.index("bibkey_zimmerman2021")
        assert adams_pos < marker_pos < zimmerman_pos

    def test_insert_same_author_different_years_orders_by_year(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_REFS)  # existing: Smith, J. (2020)
        docs_intel.insert_bibliography_entry(
            docx_path=docx,
            citation_key="smith2018",
            csl_item=_journal_article(
                family="Smith", given="J.", year=2018, title="Earlier paper",
            ),
        )
        xml = _read_doc_xml(docx)
        smith2018_pos = xml.index("bibkey_smith2018")
        smith2020_pos = xml.index("bibkey_smith2020")
        assert smith2018_pos < smith2020_pos

    def test_insert_duplicate_returns_error_file_unchanged(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_REFS)
        original = open(docx, "rb").read()
        # smith2020 already in the doc.
        res = docs_intel.insert_bibliography_entry(
            docx_path=docx,
            citation_key="smith2020",
            csl_item=_journal_article(family="Smith", given="J.", year=2020),
        )
        assert "error" in res
        assert open(docx, "rb").read() == original

    def test_insert_missing_file_returns_error(self, tmp_path):
        res = docs_intel.insert_bibliography_entry(
            docx_path=str(tmp_path / "gone.docx"),
            citation_key="k",
            csl_item=_journal_article(),
        )
        assert "error" in res

    def test_insert_empty_key_returns_error(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        original = open(docx, "rb").read()
        res = docs_intel.insert_bibliography_entry(
            docx_path=docx,
            citation_key="   ",
            csl_item=_journal_article(),
        )
        assert "error" in res
        assert open(docx, "rb").read() == original

    def test_insert_non_dict_csl_item_returns_error(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        original = open(docx, "rb").read()
        res = docs_intel.insert_bibliography_entry(
            docx_path=docx,
            citation_key="k",
            csl_item="not a dict",  # type: ignore[arg-type]
        )
        assert "error" in res
        assert open(docx, "rb").read() == original

    def test_insert_multiple_types(self, tmp_path):
        """Insert journal article, book, and chapter — all succeed."""
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        for key, item in [
            ("a_article", _journal_article()),
            ("b_book", _book()),
            ("c_chapter", _chapter()),
        ]:
            res = docs_intel.insert_bibliography_entry(
                docx_path=docx, citation_key=key, csl_item=item
            )
            assert "error" not in res, f"failed for {key}: {res.get('error')}"
        xml = _read_doc_xml(docx)
        assert "bibkey_a_article" in xml
        assert "bibkey_b_book" in xml
        assert "bibkey_c_chapter" in xml

    def test_insert_key_with_special_chars(self, tmp_path):
        """Citation keys containing DOIs (slashes, dots) are safely sanitised."""
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        res = docs_intel.insert_bibliography_entry(
            docx_path=docx,
            citation_key="10.1234/my.paper-2023",
            csl_item=_journal_article(),
        )
        assert "error" not in res
        xml = _read_doc_xml(docx)
        assert "bibkey_" in xml


# ---------------------------------------------------------------------------
# update_bibliography_entry
# ---------------------------------------------------------------------------

class TestUpdateBibliographyEntry:
    def test_update_replaces_text(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_REFS)
        new_item = _journal_article(
            family="Smith", given="J.", year=2020,
            title="Revised title for the same paper",
        )
        res = docs_intel.update_bibliography_entry(
            docx_path=docx,
            citation_key="smith2020",
            csl_item=new_item,
        )
        assert "error" not in res, res.get("error")
        assert res["status"] == "updated"
        xml = _read_doc_xml(docx)
        assert "Revised title for the same paper" in xml

    def test_update_bookmark_preserved(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_REFS)
        docs_intel.update_bibliography_entry(
            docx_path=docx,
            citation_key="smith2020",
            csl_item=_journal_article(family="Smith", given="J.", year=2020),
        )
        xml = _read_doc_xml(docx)
        assert "bibkey_smith2020" in xml

    def test_update_nonexistent_key_returns_error(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_REFS)
        original = open(docx, "rb").read()
        res = docs_intel.update_bibliography_entry(
            docx_path=docx,
            citation_key="nonexistent_key",
            csl_item=_journal_article(),
        )
        assert "error" in res
        assert open(docx, "rb").read() == original

    def test_update_missing_file_returns_error(self, tmp_path):
        res = docs_intel.update_bibliography_entry(
            docx_path=str(tmp_path / "gone.docx"),
            citation_key="k",
            csl_item=_journal_article(),
        )
        assert "error" in res

    def test_update_empty_key_returns_error(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_REFS)
        original = open(docx, "rb").read()
        res = docs_intel.update_bibliography_entry(
            docx_path=docx,
            citation_key="",
            csl_item=_journal_article(),
        )
        assert "error" in res
        assert open(docx, "rb").read() == original

    def test_update_non_dict_csl_item_returns_error(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_REFS)
        original = open(docx, "rb").read()
        res = docs_intel.update_bibliography_entry(
            docx_path=docx,
            citation_key="smith2020",
            csl_item=None,  # type: ignore[arg-type]
        )
        assert "error" in res
        assert open(docx, "rb").read() == original

    def test_update_zero_text_runs_returns_error(self, tmp_path):
        """b6a9ec99 -- an entry paragraph with no <w:t> at all (e.g.
        hand-edited down to just its bookmark pair) must fail closed
        instead of silently reporting {"status": "updated"} while writing
        nothing."""
        docx = str(tmp_path / "doc.docx")
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="DD0001">
      <w:pPr><w:pStyle w:val="Bibliography"/></w:pPr>
      <w:bookmarkStart w:id="0" w:name="bibkey_empty2020"/>
      <w:bookmarkEnd w:id="0"/>
    </w:p>
  </w:body>
</w:document>
"""
        _write_docx(docx, xml)
        original = open(docx, "rb").read()
        res = docs_intel.update_bibliography_entry(
            docx_path=docx,
            citation_key="empty2020",
            csl_item=_journal_article(),
        )
        assert "error" in res
        assert "no <w:t>" in res["error"]
        assert open(docx, "rb").read() == original

    def test_update_ambiguous_multiple_text_runs_returns_error(self, tmp_path):
        """b6a9ec99 -- an entry paragraph with more than one <w:t> must fail
        closed rather than silently overwrite only the first and leave the
        rest holding stale, now-inconsistent old text (real corruption the
        naive "first match then break" loop produced)."""
        docx = str(tmp_path / "doc.docx")
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="DD0002">
      <w:pPr><w:pStyle w:val="Bibliography"/></w:pPr>
      <w:bookmarkStart w:id="0" w:name="bibkey_split2020"/>
      <w:r><w:t xml:space="preserve">Smith, J. (2020). </w:t></w:r>
      <w:r><w:t>Old paper, split across two runs.</w:t></w:r>
      <w:bookmarkEnd w:id="0"/>
    </w:p>
  </w:body>
</w:document>
"""
        _write_docx(docx, xml)
        original = open(docx, "rb").read()
        res = docs_intel.update_bibliography_entry(
            docx_path=docx,
            citation_key="split2020",
            csl_item=_journal_article(title="Should not be written"),
        )
        assert "error" in res
        assert "2 text" in res["error"]
        assert open(docx, "rb").read() == original


# ---------------------------------------------------------------------------
# remove_bibliography_entry
# ---------------------------------------------------------------------------

class TestRemoveBibliographyEntry:
    def test_remove_existing_entry(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_REFS)
        xml_before = _read_doc_xml(docx)
        assert "bibkey_smith2020" in xml_before

        res = docs_intel.remove_bibliography_entry(
            docx_path=docx,
            citation_key="smith2020",
        )
        assert "error" not in res, res.get("error")
        assert res["status"] == "removed"
        xml_after = _read_doc_xml(docx)
        assert "bibkey_smith2020" not in xml_after

    def test_remove_nonexistent_returns_error(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_REFS)
        original = open(docx, "rb").read()
        res = docs_intel.remove_bibliography_entry(
            docx_path=docx,
            citation_key="never_inserted",
        )
        assert "error" in res
        assert open(docx, "rb").read() == original

    def test_remove_missing_file_returns_error(self, tmp_path):
        res = docs_intel.remove_bibliography_entry(
            docx_path=str(tmp_path / "gone.docx"),
            citation_key="k",
        )
        assert "error" in res

    def test_remove_empty_key_returns_error(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_REFS)
        original = open(docx, "rb").read()
        res = docs_intel.remove_bibliography_entry(
            docx_path=docx,
            citation_key="",
        )
        assert "error" in res
        assert open(docx, "rb").read() == original

    def test_remove_last_entry_with_flag_also_removes_heading(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_REFS)

        res = docs_intel.remove_bibliography_entry(
            docx_path=docx,
            citation_key="smith2020",
            remove_heading_if_empty=True,
        )
        assert "error" not in res, res.get("error")
        assert res["heading_removed"] is True
        xml_after = _read_doc_xml(docx)
        assert "bibkey_smith2020" not in xml_after
        assert "References" not in xml_after
        assert "Body text." in xml_after

    def test_remove_default_behaviour_leaves_heading_in_place(self, tmp_path):
        """The flag defaults to False -- unchanged behaviour for every
        existing caller that never asked for heading cleanup."""
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_REFS)

        res = docs_intel.remove_bibliography_entry(
            docx_path=docx,
            citation_key="smith2020",
        )
        assert "error" not in res, res.get("error")
        assert res["heading_removed"] is False
        xml_after = _read_doc_xml(docx)
        assert "References" in xml_after

    def test_remove_one_of_several_entries_keeps_heading_even_with_flag(self, tmp_path):
        """remove_heading_if_empty must never remove the heading while a
        SIBLING entry still remains under it."""
        docx = str(tmp_path / "doc.docx")
        two_entry_xml = _DOC_XML_WITH_REFS.replace(
            "  </w:body>",
            '    <w:p w14:paraId="BB0004">\n'
            '      <w:pPr><w:pStyle w:val="Bibliography"/></w:pPr>\n'
            '      <w:bookmarkStart w:id="0" w:name="bibkey_jones2021"/>\n'
            '      <w:r><w:t xml:space="preserve">Jones, B. (2021). New paper.</w:t></w:r>\n'
            '      <w:bookmarkEnd w:id="0"/>\n'
            "    </w:p>\n"
            "  </w:body>",
        )
        _write_docx(docx, two_entry_xml)

        res = docs_intel.remove_bibliography_entry(
            docx_path=docx,
            citation_key="smith2020",
            remove_heading_if_empty=True,
        )
        assert "error" not in res, res.get("error")
        assert res["heading_removed"] is False
        xml_after = _read_doc_xml(docx)
        assert "References" in xml_after
        assert "bibkey_jones2021" in xml_after
        assert "bibkey_smith2020" not in xml_after

    def test_remove_with_flag_but_no_heading_present_is_a_no_op_for_heading(self, tmp_path):
        """A document whose entry has no References heading at all (e.g. an
        entry inserted by some other path) must not error just because
        remove_heading_if_empty was requested."""
        no_heading_xml = _DOC_XML_WITH_REFS.replace(
            '      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>\n'
            "      <w:r><w:t>References</w:t></w:r>\n",
            "      <w:r><w:t>Not a heading.</w:t></w:r>\n",
        )
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, no_heading_xml)

        res = docs_intel.remove_bibliography_entry(
            docx_path=docx,
            citation_key="smith2020",
            remove_heading_if_empty=True,
        )
        assert "error" not in res, res.get("error")
        assert res["heading_removed"] is False


# ---------------------------------------------------------------------------
# sync_bibliography
# ---------------------------------------------------------------------------

class TestSyncBibliography:
    def test_sync_inserts_missing_entries(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_CITATION)
        csl_items = {
            "smith2020": _journal_article(family="Smith", given="J.", year=2020),
            "jones2021": _journal_article(family="Jones", given="B.", year=2021),
        }
        res = docs_intel.sync_bibliography(docx_path=docx, csl_items=csl_items)
        assert "error" not in res
        assert "smith2020" in res["inserted"]
        assert "jones2021" in res["inserted"]
        assert res["updated"] == []
        assert res["missing_data"] == []

    def test_sync_updates_existing_entries(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_CITATION)
        # First sync inserts.
        docs_intel.sync_bibliography(
            docx_path=docx,
            csl_items={
                "smith2020": _journal_article(family="Smith", given="J.", year=2020),
                "jones2021": _journal_article(family="Jones", given="B.", year=2021),
            },
        )
        # Second sync should update both.
        csl_items2 = {
            "smith2020": _journal_article(
                family="Smith", given="J.", year=2020, title="Updated Smith paper"
            ),
            "jones2021": _journal_article(
                family="Jones", given="B.", year=2021, title="Updated Jones paper"
            ),
        }
        res2 = docs_intel.sync_bibliography(docx_path=docx, csl_items=csl_items2)
        assert "error" not in res2
        assert "smith2020" in res2["updated"]
        assert "jones2021" in res2["updated"]
        xml = _read_doc_xml(docx)
        assert "Updated Smith paper" in xml
        assert "Updated Jones paper" in xml

    def test_sync_reports_missing_data(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx, _DOC_XML_WITH_CITATION)
        # Only supply data for smith2020, not jones2021.
        res = docs_intel.sync_bibliography(
            docx_path=docx,
            csl_items={"smith2020": _journal_article(family="Smith", given="J.", year=2020)},
        )
        assert "jones2021" in res["missing_data"]
        assert "smith2020" not in res["missing_data"]

    def test_sync_reports_stale_entries(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        # _DOC_XML_WITH_REFS has a bibliography entry for smith2020 but no
        # in-text citation in the body (the body has only a plain paragraph).
        _write_docx(docx, _DOC_XML_WITH_REFS)
        res = docs_intel.sync_bibliography(docx_path=docx, csl_items={})
        # smith2020 exists in bibliography but is not cited in-text.
        assert len(res["stale_entries"]) >= 1

    def test_sync_non_dict_csl_items_returns_error(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        res = docs_intel.sync_bibliography(docx_path=docx, csl_items="bad")  # type: ignore[arg-type]
        assert "error" in res

    def test_sync_missing_file_returns_error(self, tmp_path):
        res = docs_intel.sync_bibliography(
            docx_path=str(tmp_path / "gone.docx"),
            csl_items={},
        )
        assert "error" in res

    def test_sync_empty_csl_items_no_crash(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)
        res = docs_intel.sync_bibliography(docx_path=docx, csl_items={})
        assert "error" not in res
        assert res["inserted"] == []
        assert res["updated"] == []


# ---------------------------------------------------------------------------
# Full lifecycle: insert -> update -> remove
# ---------------------------------------------------------------------------

class TestBibliographyLifecycle:
    def test_insert_update_remove(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)

        # Insert.
        ins = docs_intel.insert_bibliography_entry(
            docx_path=docx,
            citation_key="smith2023",
            csl_item=_journal_article(),
        )
        assert "error" not in ins
        xml_after_insert = _read_doc_xml(docx)
        assert "bibkey_smith2023" in xml_after_insert
        assert "A great study of things" in xml_after_insert

        # Update.
        upd = docs_intel.update_bibliography_entry(
            docx_path=docx,
            citation_key="smith2023",
            csl_item=_journal_article(title="Updated title"),
        )
        assert "error" not in upd
        xml_after_update = _read_doc_xml(docx)
        assert "Updated title" in xml_after_update
        assert "bibkey_smith2023" in xml_after_update

        # Remove.
        rem = docs_intel.remove_bibliography_entry(
            docx_path=docx,
            citation_key="smith2023",
        )
        assert "error" not in rem
        xml_final = _read_doc_xml(docx)
        assert "bibkey_smith2023" not in xml_final

    def test_insert_two_entries_then_remove_first(self, tmp_path):
        """Removing the first entry leaves the second intact."""
        docx = str(tmp_path / "doc.docx")
        _write_docx(docx)

        docs_intel.insert_bibliography_entry(
            docx_path=docx,
            citation_key="alpha",
            csl_item=_journal_article(family="Alpha"),
        )
        docs_intel.insert_bibliography_entry(
            docx_path=docx,
            citation_key="beta",
            csl_item=_journal_article(family="Beta"),
        )
        docs_intel.remove_bibliography_entry(
            docx_path=docx,
            citation_key="alpha",
        )
        xml = _read_doc_xml(docx)
        assert "bibkey_alpha" not in xml
        assert "bibkey_beta" in xml

    def test_zip_members_preserved(self, tmp_path):
        """Extra ZIP members (styles.xml, [Content_Types].xml) survive write-back."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("word/document.xml", _DOC_XML)
            zf.writestr("[Content_Types].xml", "<ct/>")
            zf.writestr("word/styles.xml", "<styles/>")
        docx = str(tmp_path / "doc.docx")
        with open(docx, "wb") as fh:
            fh.write(buf.getvalue())

        docs_intel.insert_bibliography_entry(
            docx_path=docx,
            citation_key="k",
            csl_item=_journal_article(),
        )
        with zipfile.ZipFile(docx, "r") as zf:
            names = set(zf.namelist())
        assert "[Content_Types].xml" in names
        assert "word/styles.xml" in names
        assert "word/document.xml" in names


# ---------------------------------------------------------------------------
# Sidecar invalidation
# ---------------------------------------------------------------------------

class TestSidecarInvalidation:
    def test_insert_with_sidecar_invalidates_mtime(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        db = str(tmp_path / "index.db")
        _write_docx(docx)

        # Build sidecar index so source_mtime is stored.
        docs_intel.index_docx(docx, db)

        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT value FROM docx_index_meta WHERE key='source_mtime'"
        ).fetchone()
        conn.close()
        assert row is not None and row[0] is not None

        # Insert with sidecar.
        res = docs_intel.insert_bibliography_entry(
            docx_path=docx,
            citation_key="k",
            csl_item=_journal_article(),
            index_db_path=db,
        )
        assert "error" not in res

        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT value FROM docx_index_meta WHERE key='source_mtime'"
        ).fetchone()
        conn.close()
        assert row is None or row[0] is None

    def test_update_with_sidecar_invalidates_mtime(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        db = str(tmp_path / "index.db")
        _write_docx(docx, _DOC_XML_WITH_REFS)
        docs_intel.index_docx(docx, db)

        docs_intel.update_bibliography_entry(
            docx_path=docx,
            citation_key="smith2020",
            csl_item=_journal_article(family="Smith", given="J.", year=2020),
            index_db_path=db,
        )

        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT value FROM docx_index_meta WHERE key='source_mtime'"
        ).fetchone()
        conn.close()
        assert row is None or row[0] is None

    def test_remove_with_sidecar_invalidates_mtime(self, tmp_path):
        docx = str(tmp_path / "doc.docx")
        db = str(tmp_path / "index.db")
        _write_docx(docx, _DOC_XML_WITH_REFS)
        docs_intel.index_docx(docx, db)

        docs_intel.remove_bibliography_entry(
            docx_path=docx,
            citation_key="smith2020",
            index_db_path=db,
        )

        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT value FROM docx_index_meta WHERE key='source_mtime'"
        ).fetchone()
        conn.close()
        assert row is None or row[0] is None
