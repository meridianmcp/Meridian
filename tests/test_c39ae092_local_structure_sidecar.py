"""c39ae092 — local-only structural doc-store fallback for ingest_local_document_structure.

Tests that headings/figures/tables can be stored in and retrieved from the local
sidecar SQLite index (docs_intel.index_docx_structure / get_local_structure_elements),
without any hosted POST. Also tests the updated ingest_local_document_structure
which prefers the local path when index_db_path is supplied.
"""
from __future__ import annotations

import hashlib
import io
import os
import sys
import zipfile

import pytest

# ---------------------------------------------------------------------------
# Fixture: ensure meridian_docs is importable from the extensions directory
# ---------------------------------------------------------------------------

_EXT_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "extensions", "meridian-docs")
)
if _EXT_PATH not in sys.path:
    sys.path.insert(0, _EXT_PATH)


# ---------------------------------------------------------------------------
# Synthetic .docx fixtures
# ---------------------------------------------------------------------------

_STRUCTURAL_DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="AA000001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Introduction</w:t></w:r>
    </w:p>
    <w:p w14:paraId="AA000002">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>Background</w:t></w:r>
    </w:p>
    <w:p w14:paraId="AA000003">
      <w:r><w:t>Some body text here.</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Parameter</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Temperature</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>25 C</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:p w14:paraId="AA000004">
      <w:fldSimple w:instr="SEQ Table \\* ARABIC">
        <w:r><w:t>1</w:t></w:r>
      </w:fldSimple>
      <w:r><w:t>: Experimental conditions summary</w:t></w:r>
    </w:p>
    <w:p w14:paraId="AA000005">
      <w:r><w:t>Figure </w:t></w:r>
      <w:r><w:fldChar w:fldCharType="begin"/></w:r>
      <w:r><w:instrText> SEQ Figure \\* ARABIC </w:instrText></w:r>
      <w:r><w:fldChar w:fldCharType="separate"/></w:r>
      <w:r><w:t>1</w:t></w:r>
      <w:r><w:fldChar w:fldCharType="end"/></w:r>
      <w:r><w:t>: Photograph of the apparatus.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="AA000006">
      <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
      <w:r><w:t>Results</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""


def _make_docx(xml: str = _STRUCTURAL_DOCUMENT_XML) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# docs_intel.index_docx_structure — unit tests (stdlib-only, no hosted call)
# ---------------------------------------------------------------------------

def test_index_docx_structure_returns_counts(tmp_path):
    """index_docx_structure returns correct heading/figure/table counts."""
    try:
        from meridian_docs import docs_intel
    except ImportError:
        pytest.skip("meridian_docs not importable")

    docx_bytes = _make_docx()
    db = str(tmp_path / "struct.db")
    result = docs_intel.index_docx_structure(docx_bytes, db)

    assert result["heading_count"] == 3, f"expected 3 headings, got {result}"
    assert result["figure_count"] == 1, f"expected 1 figure, got {result}"
    assert result["table_count"] == 1, f"expected 1 table, got {result}"
    assert result["index_db"] == db


def test_index_docx_structure_from_file_path(tmp_path):
    """index_docx_structure accepts a file path as well as raw bytes."""
    try:
        from meridian_docs import docs_intel
    except ImportError:
        pytest.skip("meridian_docs not importable")

    docx_path = tmp_path / "test.docx"
    docx_path.write_bytes(_make_docx())
    db = str(tmp_path / "struct.db")
    result = docs_intel.index_docx_structure(str(docx_path), db)
    assert result["heading_count"] == 3


def test_get_local_structure_elements_returns_all_kinds(tmp_path):
    """get_local_structure_elements returns headings, figures, and tables."""
    try:
        from meridian_docs import docs_intel
    except ImportError:
        pytest.skip("meridian_docs not importable")

    docx_bytes = _make_docx()
    db = str(tmp_path / "struct.db")
    docs_intel.index_docx_structure(docx_bytes, db)

    elements = docs_intel.get_local_structure_elements(db)

    assert "headings" in elements
    assert "figures" in elements
    assert "tables" in elements
    assert elements["heading_count"] == 3
    assert elements["figure_count"] == 1
    assert elements["table_count"] == 1


def test_get_local_structure_elements_heading_detail(tmp_path):
    """Headings have correct level and text."""
    try:
        from meridian_docs import docs_intel
    except ImportError:
        pytest.skip("meridian_docs not importable")

    db = str(tmp_path / "struct.db")
    docs_intel.index_docx_structure(_make_docx(), db)
    elements = docs_intel.get_local_structure_elements(db)

    headings = elements["headings"]
    texts = [h["text"] for h in headings]
    assert "Introduction" in texts
    assert "Background" in texts
    assert "Results" in texts

    h1 = next(h for h in headings if h["text"] == "Introduction")
    assert h1["level"] == 1, f"expected level 1, got {h1['level']}"

    h2 = next(h for h in headings if h["text"] == "Background")
    assert h2["level"] == 2, f"expected level 2, got {h2['level']}"


def test_get_local_structure_elements_figure_caption(tmp_path):
    """Figures have non-empty captions."""
    try:
        from meridian_docs import docs_intel
    except ImportError:
        pytest.skip("meridian_docs not importable")

    db = str(tmp_path / "struct.db")
    docs_intel.index_docx_structure(_make_docx(), db)
    elements = docs_intel.get_local_structure_elements(db)

    figures = elements["figures"]
    assert len(figures) == 1
    fig = figures[0]
    assert "apparatus" in fig["caption"].lower() or "photograph" in fig["caption"].lower()


def test_get_local_structure_elements_table_has_rows(tmp_path):
    """Tables have row/col counts and serialised row data."""
    try:
        from meridian_docs import docs_intel
    except ImportError:
        pytest.skip("meridian_docs not importable")

    db = str(tmp_path / "struct.db")
    docs_intel.index_docx_structure(_make_docx(), db)
    elements = docs_intel.get_local_structure_elements(db)

    tables = elements["tables"]
    assert len(tables) == 1
    tbl = tables[0]
    assert tbl["row_count"] == 2
    assert tbl["col_count"] == 2
    rows = tbl["rows"]
    assert isinstance(rows, list)
    assert rows[0] == ["Parameter", "Value"]
    assert rows[1] == ["Temperature", "25 C"]


def test_get_local_structure_elements_empty_on_fresh_db(tmp_path):
    """get_local_structure_elements returns empty lists on a DB with no structure indexed."""
    try:
        from meridian_docs import docs_intel
    except ImportError:
        pytest.skip("meridian_docs not importable")

    # Only run index_docx (no index_docx_structure), so structural tables are empty.
    db = str(tmp_path / "fresh.db")
    docs_intel.index_docx(_make_docx(), db)
    elements = docs_intel.get_local_structure_elements(db)
    assert elements["heading_count"] == 0
    assert elements["figure_count"] == 0
    assert elements["table_count"] == 0


def test_index_docx_structure_idempotent(tmp_path):
    """Re-indexing the same doc replaces rows; counts stay consistent."""
    try:
        from meridian_docs import docs_intel
    except ImportError:
        pytest.skip("meridian_docs not importable")

    db = str(tmp_path / "struct.db")
    docx_bytes = _make_docx()
    docs_intel.index_docx_structure(docx_bytes, db)
    result = docs_intel.index_docx_structure(docx_bytes, db)

    elements = docs_intel.get_local_structure_elements(db)
    assert elements["heading_count"] == result["heading_count"]
    assert elements["figure_count"] == result["figure_count"]
    assert elements["table_count"] == result["table_count"]


def test_index_docx_structure_empty_document(tmp_path):
    """An empty .docx body produces zero counts without raising."""
    try:
        from meridian_docs import docs_intel
    except ImportError:
        pytest.skip("meridian_docs not importable")

    empty_xml = """<?xml version="1.0"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
</w:document>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", empty_xml)

    db = str(tmp_path / "empty.db")
    result = docs_intel.index_docx_structure(buf.getvalue(), db)
    assert result["heading_count"] == 0
    assert result["figure_count"] == 0
    assert result["table_count"] == 0


# ---------------------------------------------------------------------------
# index_docx_structure co-exists with paragraph index in the same DB
# ---------------------------------------------------------------------------

def test_structure_and_paragraph_index_coexist_in_same_sidecar(tmp_path):
    """index_docx (paragraph index) and index_docx_structure (structural index)
    can share the same SQLite sidecar without conflicts."""
    try:
        from meridian_docs import docs_intel
    except ImportError:
        pytest.skip("meridian_docs not importable")

    docx_bytes = _make_docx()
    db = str(tmp_path / "shared.db")

    # Build both indexes against the same DB.
    para_summary = docs_intel.index_docx(docx_bytes, db)
    struct_summary = docs_intel.index_docx_structure(docx_bytes, db)

    # Paragraph index still works.
    paragraphs = docs_intel.find_paragraphs(db, "Some body text")
    assert len(paragraphs) == 1

    # Structural elements also accessible.
    elements = docs_intel.get_local_structure_elements(db)
    assert elements["heading_count"] == 3

    # Neither index corrupted the other.
    assert para_summary["paragraph_count"] > 0
    assert struct_summary["heading_count"] == 3


# ---------------------------------------------------------------------------
# e9b2cd2b — SHA-256 freshness fingerprint + completeness boundary marker
#
# index_docx_structure now stamps docx_index_meta with a source SHA-256
# fingerprint and an explicit "structure_complete" boundary marker, and
# get_local_structure_elements fails closed (raises) when the sidecar is
# stale or the result of an interrupted indexing run, rather than silently
# handing back partial/outdated counts as if they were authoritative.
# ---------------------------------------------------------------------------

def test_index_docx_structure_fresh_and_complete(tmp_path):
    """A normal, uninterrupted run stamps complete=True and the correct
    SHA-256 fingerprint of the exact bytes indexed, and reads it back as
    trustworthy."""
    try:
        from meridian_docs import docs_intel
    except ImportError:
        pytest.skip("meridian_docs not importable")

    docx_bytes = _make_docx()
    db = str(tmp_path / "fresh.db")

    result = docs_intel.index_docx_structure(docx_bytes, db)
    assert result["complete"] is True
    assert result["source_sha256"] == hashlib.sha256(docx_bytes).hexdigest()

    freshness = docs_intel.get_structure_freshness(db)
    assert freshness["indexed"] is True
    assert freshness["complete"] is True
    assert freshness["stale"] is False
    assert freshness["trustworthy"] is True
    assert freshness["source_sha256"] == result["source_sha256"]

    # A trustworthy read never raises and never carries a "freshness" key
    # (that key is only attached when allow_stale=True was needed).
    elements = docs_intel.get_local_structure_elements(db)
    assert elements["heading_count"] == 3
    assert "freshness" not in elements


def test_get_structure_freshness_never_indexed_is_trustworthy(tmp_path):
    """A sidecar that has never had index_docx_structure run on it is not
    stale or incomplete -- there's nothing to distrust yet -- matching the
    pre-existing get_local_structure_elements empty-list behavior."""
    try:
        from meridian_docs import docs_intel
    except ImportError:
        pytest.skip("meridian_docs not importable")

    db = str(tmp_path / "never_structured.db")
    docs_intel.index_docx(_make_docx(), db)  # paragraph index only

    freshness = docs_intel.get_structure_freshness(db)
    assert freshness["indexed"] is False
    assert freshness["trustworthy"] is True
    assert freshness["reason"] == "never-indexed"

    # Must not raise.
    elements = docs_intel.get_local_structure_elements(db)
    assert elements["heading_count"] == 0


def test_index_docx_structure_stale_after_source_file_changes(tmp_path):
    """When index_docx_structure is built from a file path and the file's
    content changes afterward, the SHA-256 fingerprint no longer matches:
    get_structure_freshness reports stale, and get_local_structure_elements
    fails closed instead of serving the old (now-stale) counts."""
    try:
        from meridian_docs import docs_intel
    except ImportError:
        pytest.skip("meridian_docs not importable")

    docx_path = tmp_path / "doc.docx"
    docx_path.write_bytes(_make_docx())
    db = str(tmp_path / "stale.db")

    result = docs_intel.index_docx_structure(str(docx_path), db)
    assert result["heading_count"] == 3

    # A file path with unchanged content stays fresh.
    freshness = docs_intel.get_structure_freshness(db)
    assert freshness["stale"] is False
    assert freshness["trustworthy"] is True

    # Now change the source content on disk (fewer headings) without
    # re-indexing -- simulates an edit landing after the index was built.
    changed_xml = _STRUCTURAL_DOCUMENT_XML.replace(
        '<w:pStyle w:val="Heading2"/></w:pPr>\n      <w:r><w:t>Results</w:t>',
        '<w:pStyle w:val="Heading2"/></w:pPr>\n      <w:r><w:t>Discussion</w:t>',
    )
    docx_path.write_bytes(_make_docx(changed_xml))

    staleness = docs_intel.check_structure_staleness(db)
    assert staleness["stale"] is True
    assert staleness["reason"] == "sha256-mismatch"

    freshness = docs_intel.get_structure_freshness(db)
    assert freshness["stale"] is True
    assert freshness["trustworthy"] is False

    with pytest.raises(docs_intel.StructureIndexNotTrustworthyError):
        docs_intel.get_local_structure_elements(db)

    # allow_stale=True still returns the (now-stale) old data, but flags it.
    elements = docs_intel.get_local_structure_elements(db, allow_stale=True)
    assert elements["heading_count"] == 3  # old data, not re-scanned
    assert elements["freshness"]["stale"] is True


def test_index_docx_structure_incomplete_run_fails_closed(tmp_path, monkeypatch):
    """A run that is interrupted partway through the walk (simulated here by
    forcing an exception after the document has been parsed but before the
    structural tables are written) leaves the completeness boundary marker
    at 'incomplete'. A subsequent read must fail closed rather than trust
    whatever rows are still sitting in the sidecar from the prior good run."""
    try:
        from meridian_docs import docs_intel
    except ImportError:
        pytest.skip("meridian_docs not importable")

    docx_bytes = _make_docx()
    db = str(tmp_path / "incomplete.db")

    # First, a normal successful run so there IS prior data sitting in the
    # sidecar that a naive reader might otherwise be tempted to trust.
    first = docs_intel.index_docx_structure(docx_bytes, db)
    assert first["complete"] is True

    # Now force the walk to blow up partway through the NEXT run, after the
    # completeness boundary has already been opened (marked incomplete) but
    # before the structural tables are rewritten/committed.
    def _boom(*args, **kwargs):
        raise RuntimeError("simulated interruption mid-walk")

    monkeypatch.setattr(docs_intel, "_assign_section_types", _boom)

    with pytest.raises(RuntimeError, match="simulated interruption"):
        docs_intel.index_docx_structure(docx_bytes, db)

    freshness = docs_intel.get_structure_freshness(db)
    assert freshness["complete"] is False
    assert freshness["trustworthy"] is False
    assert freshness["reason"] == "incomplete-run"

    # The old rows are still physically present (DELETE never ran)...
    with pytest.raises(docs_intel.StructureIndexNotTrustworthyError):
        docs_intel.get_local_structure_elements(db)

    # ...and are only visible via the explicit escape hatch, clearly flagged.
    elements = docs_intel.get_local_structure_elements(db, allow_stale=True)
    assert elements["heading_count"] == 3
    assert elements["freshness"]["complete"] is False


def test_get_structure_elements_mcp_tool_fails_closed_on_incomplete(tmp_path, monkeypatch):
    """The get_structure_elements MCP tool (server.py) surfaces the same
    fail-closed behavior as the underlying docs_intel function -- a consumer
    that only goes through the MCP layer still can't accidentally trust an
    incomplete index."""
    try:
        from meridian_docs import docs_intel
        from meridian_docs import server as mcp_server
    except ImportError:
        pytest.skip("meridian_docs not importable")

    docx_bytes = _make_docx()
    db = str(tmp_path / "mcp_incomplete.db")
    docs_intel.index_docx_structure(docx_bytes, db)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated interruption mid-walk")

    monkeypatch.setattr(docs_intel, "_assign_section_types", _boom)
    with pytest.raises(RuntimeError):
        docs_intel.index_docx_structure(docx_bytes, db)

    with pytest.raises(docs_intel.StructureIndexNotTrustworthyError):
        mcp_server.get_structure_elements(db)

    # Explicit opt-in still works through the MCP layer too.
    result = mcp_server.get_structure_elements(db, allow_stale=True)
    assert result["freshness"]["trustworthy"] is False


# ---------------------------------------------------------------------------
# local_ingest.ingest_local_document_structure_sidecar — unit tests
# ---------------------------------------------------------------------------

def test_ingest_local_document_structure_sidecar_basic(tmp_path):
    """ingest_local_document_structure_sidecar stores elements locally without
    any network call."""
    try:
        from meridian_docs import local_ingest
    except ImportError:
        pytest.skip("meridian_docs not importable")

    docx_path = tmp_path / "doc.docx"
    docx_path.write_bytes(_make_docx())
    db = str(tmp_path / "sidecar.db")

    result = local_ingest.ingest_local_document_structure_sidecar(
        path=str(docx_path),
        index_db_path=db,
        title="Test Doc",
    )

    assert result["heading_count"] == 3
    assert result["figure_count"] == 1
    assert result["table_count"] == 1
    assert result["local_path"] == str(docx_path)
    assert result["source"] == str(docx_path)  # defaults to path
    assert result["index_db"] == db


def test_ingest_local_document_structure_sidecar_custom_source(tmp_path):
    """Custom source is stored in the sidecar meta and returned."""
    try:
        from meridian_docs import local_ingest
    except ImportError:
        pytest.skip("meridian_docs not importable")

    docx_path = tmp_path / "doc.docx"
    docx_path.write_bytes(_make_docx())
    db = str(tmp_path / "sidecar.db")
    custom_source = "my-custom-source-key"

    result = local_ingest.ingest_local_document_structure_sidecar(
        path=str(docx_path),
        index_db_path=db,
        source=custom_source,
    )
    assert result["source"] == custom_source


def test_ingest_local_document_structure_sidecar_rejects_non_docx(tmp_path):
    """Only .docx files are accepted."""
    try:
        from meridian_docs import local_ingest
    except ImportError:
        pytest.skip("meridian_docs not importable")

    txt = tmp_path / "doc.txt"
    txt.write_text("hello")
    db = str(tmp_path / "sidecar.db")

    with pytest.raises(local_ingest.UnsupportedDocumentError):
        local_ingest.ingest_local_document_structure_sidecar(str(txt), db)


def test_ingest_local_document_structure_sidecar_raises_on_missing_file(tmp_path):
    """FileNotFoundError when path does not exist."""
    try:
        from meridian_docs import local_ingest
    except ImportError:
        pytest.skip("meridian_docs not importable")

    db = str(tmp_path / "sidecar.db")
    with pytest.raises(FileNotFoundError):
        local_ingest.ingest_local_document_structure_sidecar(
            str(tmp_path / "nonexistent.docx"), db
        )


# ---------------------------------------------------------------------------
# local_ingest.ingest_local_document_structure with index_db_path
# ---------------------------------------------------------------------------

def test_ingest_local_document_structure_uses_sidecar_when_index_db_path_given(tmp_path):
    """When index_db_path is supplied, the local sidecar path is taken and
    call_hosted_ingest_structure is NOT called."""
    try:
        from meridian_docs import local_ingest
    except ImportError:
        pytest.skip("meridian_docs not importable")

    docx_path = tmp_path / "doc.docx"
    docx_path.write_bytes(_make_docx())
    db = str(tmp_path / "sidecar.db")

    # Patch the hosted call to verify it is NOT invoked.
    hosted_calls: list[dict] = []

    def _mock_hosted(*args, **kwargs):
        hosted_calls.append({"args": args, "kwargs": kwargs})
        return {"document_id": "should-not-be-called", "source": "x"}

    import unittest.mock as mock
    with mock.patch.object(local_ingest, "call_hosted_ingest_structure", _mock_hosted):
        result = local_ingest.ingest_local_document_structure(
            path=str(docx_path),
            project_id="proj-123",
            index_db_path=db,
        )

    assert len(hosted_calls) == 0, "hosted POST must not be called when index_db_path is set"
    assert result["heading_count"] == 3
    assert result["local_path"] == str(docx_path)


def test_ingest_local_document_structure_raises_when_no_index_db_and_no_force_hosted(tmp_path):
    """f8c7ffdc — when index_db_path is None and force_hosted is False (default),
    a DocExtractionError is raised rather than silently routing to hosted.
    Hosted routing must be explicitly opted in via force_hosted=True."""
    try:
        from meridian_docs import local_ingest
    except ImportError:
        pytest.skip("meridian_docs not importable")

    docx_path = tmp_path / "doc.docx"
    docx_path.write_bytes(_make_docx())

    with pytest.raises(local_ingest.DocExtractionError, match="hosted routing is opt-in only"):
        local_ingest.ingest_local_document_structure(
            path=str(docx_path),
            project_id="proj-456",
            index_db_path=None,
            # force_hosted defaults to False — must raise
        )


def test_ingest_local_document_structure_uses_hosted_when_force_hosted_true(tmp_path, monkeypatch):
    """f8c7ffdc — when force_hosted=True is explicitly set and index_db_path is None,
    the hosted POST path IS used (explicit opt-in)."""
    try:
        from meridian_docs import local_ingest
    except ImportError:
        pytest.skip("meridian_docs not importable")

    docx_path = tmp_path / "doc.docx"
    docx_path.write_bytes(_make_docx())

    calls: list[dict] = []

    def _mock_hosted(project_id, source, blocks, title=None, doc_type="docx",
                     base_url=None, token=None):
        calls.append({"project_id": project_id, "source": source, "blocks": blocks})
        return {"document_id": "mock-doc-id", "source": source, "element_count": len(blocks)}

    monkeypatch.setattr(local_ingest, "call_hosted_ingest_structure", _mock_hosted)

    result = local_ingest.ingest_local_document_structure(
        path=str(docx_path),
        project_id="proj-456",
        index_db_path=None,
        force_hosted=True,  # explicit opt-in to hosted path
    )

    assert len(calls) == 1, "hosted call must be made when force_hosted=True"
    assert result["document_id"] == "mock-doc-id"
    assert result["blocks_forwarded"] > 0


def test_call_mcp_tool_headers_include_accept_language(monkeypatch):
    """40d93549 — _call_mcp_tool must include Accept-Language in its header set
    as defensive WAF hardening (some WAFs trigger on missing standard headers
    beyond just User-Agent)."""
    try:
        from meridian_docs import local_ingest
    except ImportError:
        pytest.skip("meridian_docs not importable")

    captured_headers: list[dict] = []

    def _mock_urlopen(req, timeout=None):
        captured_headers.append(dict(req.headers))
        raise local_ingest.DocExtractionError("mock stop")  # prevent actual network call

    import unittest.mock as mock

    with mock.patch("urllib.request.urlopen", side_effect=_mock_urlopen):
        try:
            local_ingest._call_mcp_tool("test_tool", {}, base_url="https://example.com", token="tok")
        except local_ingest.DocExtractionError:
            pass  # expected

    assert captured_headers, "urlopen must have been called"
    headers = captured_headers[0]
    # Header keys are capitalised by urllib.request.Request
    header_keys_lower = {k.lower() for k in headers}
    assert "accept-language" in header_keys_lower, (
        f"Accept-Language header missing from _call_mcp_tool request; got: {list(headers.keys())}"
    )
