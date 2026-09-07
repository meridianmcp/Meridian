"""Tests for the a2cd9f54 safe structural table edit primitives:
docs_intel.insert_column / split_cell / transpose_table.

Follows the same conventions as test_relocate_table.py / test_docs_intel_new_
primitives.py: pure Python (stdlib + pytest), no mcp, no network -- every
fixture is a small, disposable, synthetic .docx built in-memory.

Every one of these three primitives runs a real render-capability check
(_enforce_render_verification) as its last write-time gate, exactly like
insert_caption / insert_equation_local / insert_highlighted_note (see
test_docx_word_com_regression.py). Since this test machine has pywin32
importable but no real Word/LibreOffice installed, the REAL render check can
resolve to any of "rendered" / "failed" / "unavailable-with-reason"
depending on the machine -- so, like test_docx_word_com_regression.py, every
test that expects a WRITE TO SUCCEED monkeypatches
docs_intel.render_gate.check_render_capability to a deterministic stub
rather than depending on real Word/LibreOffice/COM behavior. A handful of
tests exercise the render-gate wiring itself (rendered / failed / unavailable
default-fail-closed / degraded-accept) the same way
test_docx_word_com_regression.py does for the other three writers.
"""
from __future__ import annotations

import io
import zipfile

from meridian_docs import docs_intel, server


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


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


def _rendered_ok(monkeypatch):
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "rendered", "backend": "test-stub", "detail": {}},
    )


def _load(path):
    _raw, root = docs_intel._load_docx_xml_stdlib(path)
    return root


def _table(path, table_index=0):
    root = _load(path)
    body = root.find(docs_intel._q(_W, "body"))
    return list(body)[table_index]


def _cell_texts(tbl) -> list[list[str]]:
    from meridian_docs._vendored_content_tree import _paragraph_text

    rows = []
    for tr in tbl.findall(docs_intel._q(_W, "tr")):
        row = []
        for tc in tr.findall(docs_intel._q(_W, "tc")):
            texts = [_paragraph_text(p) for p in tc.findall(docs_intel._q(_W, "p"))]
            row.append("".join(texts))
        rows.append(row)
    return rows


def _grid_col_count(tbl) -> int:
    grid = tbl.find(docs_intel._q(_W, "tblGrid"))
    if grid is None:
        return 0
    return len(grid.findall(docs_intel._q(_W, "gridCol")))


def _cell_alignments(tbl) -> list[list[str | None]]:
    """4544bbe5 -- per-cell w:jc value (None when no pPr/jc is present at all),
    one row of the returned list per w:tr, one entry per w:tc."""
    rows = []
    for tr in tbl.findall(docs_intel._q(_W, "tr")):
        row = []
        for tc in tr.findall(docs_intel._q(_W, "tc")):
            aligns = []
            for p in tc.findall(docs_intel._q(_W, "p")):
                pPr = p.find(docs_intel._q(_W, "pPr"))
                jc = pPr.find(docs_intel._q(_W, "jc")) if pPr is not None else None
                aligns.append(jc.get(docs_intel._q(_W, "val")) if jc is not None else None)
            row.append(aligns[0] if aligns else None)
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SIMPLE_2X2_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:p w14:paraId="P0000001"><w:r><w:t>Intro.</w:t></w:r></w:p>
    <w:tbl>
      <w:tblPr><w:tblStyle w:val="TableGrid"/></w:tblPr>
      <w:tblGrid>
        <w:gridCol w:w="2000"/>
        <w:gridCol w:w="3000"/>
      </w:tblGrid>
      <w:tr>
        <w:tc><w:tcPr><w:tcW w:w="2000" w:type="dxa"/></w:tcPr><w:p><w:r><w:t>A1</w:t></w:r></w:p></w:tc>
        <w:tc><w:tcPr><w:tcW w:w="3000" w:type="dxa"/></w:tcPr><w:p><w:r><w:t>B1</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:tcPr><w:tcW w:w="2000" w:type="dxa"/></w:tcPr><w:p><w:r><w:t>A2</w:t></w:r></w:p></w:tc>
        <w:tc><w:tcPr><w:tcW w:w="3000" w:type="dxa"/></w:tcPr><w:p><w:r><w:t>B2</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:p w14:paraId="P0000002"><w:r><w:t>End.</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""

# 3-column table where row 0 has ONE cell spanning all 3 columns (gridSpan=3)
# and row 1 has 3 plain, unmerged cells.
_MERGED_ROW0_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:tbl>
      <w:tblPr><w:tblStyle w:val="TableGrid"/></w:tblPr>
      <w:tblGrid>
        <w:gridCol w:w="1000"/>
        <w:gridCol w:w="1000"/>
        <w:gridCol w:w="1000"/>
      </w:tblGrid>
      <w:tr>
        <w:tc><w:tcPr><w:gridSpan w:val="3"/></w:tcPr><w:p><w:r><w:t>Header</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>A2</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>B2</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>C2</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:sectPr/>
  </w:body>
</w:document>
"""

# Malformed: tblGrid declares 3 columns but row 0 only has 2 unspanned cells.
_AMBIGUOUS_GRID_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">
  <w:body>
    <w:tbl>
      <w:tblGrid>
        <w:gridCol w:w="1000"/>
        <w:gridCol w:w="1000"/>
        <w:gridCol w:w="1000"/>
      </w:tblGrid>
      <w:tr>
        <w:tc><w:p><w:r><w:t>A1</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>B1</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:sectPr/>
  </w:body>
</w:document>
"""

_NO_TABLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>No table here.</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""

# Cell content carrying a bookmark, a numbering reference, and an image
# relationship -- used to prove transpose_table survives them verbatim
# (it reuses the SAME element objects, only repositioning them).
_RICH_CELL_TABLE_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:r="{_R_NS}" xmlns:a="{_A_NS}">
  <w:body>
    <w:tbl>
      <w:tblGrid>
        <w:gridCol w:w="1000"/>
        <w:gridCol w:w="1000"/>
        <w:gridCol w:w="1000"/>
      </w:tblGrid>
      <w:tr>
        <w:tc><w:p w14:paraId="C0000001">
          <w:bookmarkStart w:id="1" w:name="myBookmark"/>
          <w:r><w:t>bookmarked</w:t></w:r>
          <w:bookmarkEnd w:id="1"/>
        </w:p></w:tc>
        <w:tc><w:p w14:paraId="C0000002">
          <w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="7"/></w:numPr></w:pPr>
          <w:r><w:t>numbered</w:t></w:r>
        </w:p></w:tc>
        <w:tc><w:p w14:paraId="C0000003">
          <w:r><w:drawing><a:blip r:embed="rId9"/></w:drawing></w:r>
        </w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>D2</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>E2</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>F2</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:sectPr/>
  </w:body>
</w:document>
"""


# ---------------------------------------------------------------------------
# insert_column
# ---------------------------------------------------------------------------


def test_insert_column_before_adds_blank_cell_and_grows_grid(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)

    result = docs_intel.insert_column(
        path, table_index=1, col_index=0, position="before",
    )

    assert result["status"] == "inserted"
    assert result["grid_col_count"] == 3
    assert result["row_count"] == 2
    assert result["col_count"] == 3
    assert result["render_verified"] is True

    tbl = _table(path, 1)
    assert _grid_col_count(tbl) == 3
    rows = _cell_texts(tbl)
    assert rows == [["", "A1", "B1"], ["", "A2", "B2"]]

    # Surrounding content (the intro/end paragraphs) is untouched.
    root = _load(path)
    body = root.find(docs_intel._q(_W, "body"))
    body_ids = [c.get(docs_intel._q(_W14, "paraId")) for c in body]
    assert "P0000001" in body_ids and "P0000002" in body_ids


def test_insert_column_after_lands_on_the_other_side(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)

    result = docs_intel.insert_column(path, table_index=1, col_index=0, position="after")

    assert result["status"] == "inserted"
    tbl = _table(path, 1)
    rows = _cell_texts(tbl)
    assert rows == [["A1", "", "B1"], ["A2", "", "B2"]]


def test_insert_column_new_cell_has_a_fresh_unique_para_id(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)

    docs_intel.insert_column(path, table_index=1, col_index=1, position="after")

    tbl = _table(path, 1)
    new_col_paras = [
        p
        for tr in tbl.findall(docs_intel._q(_W, "tr"))
        for tc in tr.findall(docs_intel._q(_W, "tc"))[-1:]
        for p in tc.findall(docs_intel._q(_W, "p"))
    ]
    ids = [p.get(docs_intel._q(_W14, "paraId")) for p in new_col_paras]
    assert all(ids), "every brand-new cell paragraph must carry a w14:paraId"
    assert len(set(ids)) == len(ids), "new paraIds must be unique, never duplicated"


def test_insert_column_straddling_merged_cell_grows_gridspan_instead_of_new_cell(
    tmp_path, monkeypatch
):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _MERGED_ROW0_XML)

    # Insert a column between grid columns 0 and 1 -- lands strictly INSIDE
    # row 0's gridSpan=3 merged header cell.
    result = docs_intel.insert_column(path, table_index=0, col_index=0, position="after")

    assert result["status"] == "inserted"
    assert result["grid_col_count"] == 4
    tbl = _table(path, 0)
    rows = list(tbl.findall(docs_intel._q(_W, "tr")))

    # Row 0 still has exactly ONE cell (the merge absorbed the new column).
    row0_cells = rows[0].findall(docs_intel._q(_W, "tc"))
    assert len(row0_cells) == 1
    assert docs_intel._cell_grid_span(row0_cells[0]) == 4

    # Row 1 (no merge) got a genuine new blank cell at the boundary.
    row1_cells = rows[1].findall(docs_intel._q(_W, "tc"))
    assert len(row1_cells) == 4
    texts = _cell_texts(tbl)[1]
    assert texts == ["A2", "", "B2", "C2"]

    assert _grid_col_count(tbl) == 4


def test_insert_column_rejects_ambiguous_grid_and_leaves_file_untouched(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _AMBIGUOUS_GRID_XML)
    before = open(path, "rb").read()

    result = docs_intel.insert_column(path, table_index=0, col_index=0)

    assert "error" in result
    assert result["reason"] == "ambiguous_grid"
    assert open(path, "rb").read() == before


def test_insert_column_rejects_bad_table_index(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _NO_TABLE_XML)

    result = docs_intel.insert_column(path, table_index=0, col_index=0)

    assert "error" in result
    assert "not a <w:tbl>" in result["error"]


def test_insert_column_rejects_out_of_range_col_index(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)
    before = open(path, "rb").read()

    result = docs_intel.insert_column(path, table_index=1, col_index=5)

    assert "error" in result
    assert open(path, "rb").read() == before


def test_insert_column_rejects_bad_position(tmp_path):
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)

    result = docs_intel.insert_column(path, table_index=1, col_index=0, position="sideways")

    assert "error" in result


def test_insert_column_bootstraps_a_missing_tblgrid(tmp_path, monkeypatch):
    """A table with no <w:tblGrid> at all (rare -- real Word output always
    has one) still works: _ensure_tblgrid derives the column count from the
    widest row and builds a width-less grid first."""
    _rendered_ok(monkeypatch)
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>A1</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>B1</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:sectPr/>
  </w:body>
</w:document>
"""
    path = _write_docx(tmp_path, xml)

    result = docs_intel.insert_column(path, table_index=0, col_index=0, position="after")

    assert result["status"] == "inserted"
    tbl = _table(path, 0)
    assert _grid_col_count(tbl) == 3
    assert _cell_texts(tbl) == [["A1", "", "B1"]]


# --- render-gate wiring ------------------------------------------------


def test_insert_column_render_failed_restores_and_errors(tmp_path, monkeypatch):
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)
    before = open(path, "rb").read()
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "failed", "reason": "simulated render failure"},
    )

    result = docs_intel.insert_column(path, table_index=1, col_index=0)

    assert "error" in result
    assert result["render_status"] == "failed"
    assert result["file_restored"] is True
    assert open(path, "rb").read() == before


def test_insert_column_render_unavailable_fails_closed_by_default(tmp_path, monkeypatch):
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)
    before = open(path, "rb").read()
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "unavailable-with-reason", "reason": "no backend"},
    )

    result = docs_intel.insert_column(path, table_index=1, col_index=0)

    assert "error" in result
    assert result["render_status"] == "unavailable-with-reason"
    assert result["file_restored"] is True
    assert open(path, "rb").read() == before


def test_insert_column_degrades_with_audited_override(tmp_path, monkeypatch):
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "unavailable-with-reason", "reason": "no backend"},
    )

    result = docs_intel.insert_column(
        path, table_index=1, col_index=0,
        allow_degraded_render=True,
        degraded_render_reason="CI sandbox has no LibreOffice/Word installed",
    )

    assert result["status"] == "inserted"
    assert result["render_verified"] is False
    assert result["render_degraded"] is True


def test_insert_column_allow_degraded_render_requires_non_empty_reason(tmp_path):
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)
    before = open(path, "rb").read()

    result = docs_intel.insert_column(
        path, table_index=1, col_index=0, allow_degraded_render=True,
    )

    assert "error" in result
    assert "degraded_render_reason" in result["error"]
    assert open(path, "rb").read() == before


def test_insert_column_server_wrapper_delegates(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)

    result = server.insert_column(path, table_index=1, col_index=0)

    assert result["status"] == "inserted"


# ---------------------------------------------------------------------------
# split_cell
# ---------------------------------------------------------------------------


def test_split_cell_cols_only_splits_row_and_widens_other_rows(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)

    result = docs_intel.split_cell(path, table_index=1, row_index=0, col_index=0, cols=2)

    assert result["status"] == "split"
    assert result["row_count"] == 2
    assert result["col_count"] == 3

    tbl = _table(path, 1)
    assert _grid_col_count(tbl) == 3
    rows = list(tbl.findall(docs_intel._q(_W, "tr")))
    row0_cells = rows[0].findall(docs_intel._q(_W, "tc"))
    assert len(row0_cells) == 3  # A1 split into 2 + original B1
    row1_cells = rows[1].findall(docs_intel._q(_W, "tc"))
    assert len(row1_cells) == 3  # A2, blank (widened), B2

    texts = _cell_texts(tbl)
    assert texts[0] == ["", "", "B1"]
    assert texts[1] == ["A2", "", "B2"]


def test_split_cell_cols_divides_width_evenly(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)

    docs_intel.split_cell(path, table_index=1, row_index=0, col_index=0, cols=2)

    tbl = _table(path, 1)
    row0 = tbl.findall(docs_intel._q(_W, "tr"))[0]
    cells = row0.findall(docs_intel._q(_W, "tc"))
    widths = [docs_intel._tc_width(tc) for tc in cells[:2]]
    assert widths == [1000, 1000]  # original 2000 // 2


def test_split_cell_rows_only_inserts_row_and_vmerges_siblings(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)

    result = docs_intel.split_cell(path, table_index=1, row_index=0, col_index=0, rows=2)

    assert result["status"] == "split"
    assert result["row_count"] == 3
    assert result["col_count"] == 2

    tbl = _table(path, 1)
    rows = list(tbl.findall(docs_intel._q(_W, "tr")))
    assert len(rows) == 3

    # Row 0: target cell (A1) restarts nothing (it's the fresh original);
    # sibling B1 becomes vMerge="restart".
    row0_cells = rows[0].findall(docs_intel._q(_W, "tc"))
    assert docs_intel._cell_vmerge(row0_cells[1]) == "restart"

    # New row 1 (inserted): target column gets a brand-new empty cell;
    # sibling column gets a vMerge="continue" placeholder.
    row1_cells = rows[1].findall(docs_intel._q(_W, "tc"))
    assert docs_intel._cell_vmerge(row1_cells[0]) is None
    assert docs_intel._cell_vmerge(row1_cells[1]) == "continue"

    # Original row 2 (was row 1, "A2"/"B2") is completely untouched.
    row2_cells = rows[2].findall(docs_intel._q(_W, "tc"))
    texts = _cell_texts(tbl)[2]
    assert texts == ["A2", "B2"]
    assert docs_intel._cell_vmerge(row2_cells[1]) is None


def test_split_cell_rows_new_cells_have_fresh_unique_para_ids(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)

    docs_intel.split_cell(path, table_index=1, row_index=0, col_index=0, rows=3)

    tbl = _table(path, 1)
    all_ids = [
        p.get(docs_intel._q(_W14, "paraId"))
        for p in tbl.iter(docs_intel._q(_W, "p"))
        if p.get(docs_intel._q(_W14, "paraId"))
    ]
    assert len(set(all_ids)) == len(all_ids)


def test_split_cell_combined_cols_and_rows(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)

    result = docs_intel.split_cell(
        path, table_index=1, row_index=0, col_index=0, cols=2, rows=2
    )

    assert result["status"] == "split"
    assert result["row_count"] == 3
    assert result["col_count"] == 3

    tbl = _table(path, 1)
    rows = list(tbl.findall(docs_intel._q(_W, "tr")))
    assert len(rows) == 3
    for tr in rows:
        assert len(tr.findall(docs_intel._q(_W, "tc"))) == 3


def test_split_cell_rejects_target_with_existing_gridspan(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _MERGED_ROW0_XML)
    before = open(path, "rb").read()

    result = docs_intel.split_cell(path, table_index=0, row_index=0, col_index=0, cols=2)

    assert "error" in result
    assert result["reason"] == "unsupported_merge"
    assert open(path, "rb").read() == before


def test_split_cell_rejects_col_index_inside_merged_cell(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _MERGED_ROW0_XML)

    result = docs_intel.split_cell(path, table_index=0, row_index=0, col_index=1, rows=2)

    assert "error" in result
    assert result["reason"] == "unsupported_merge"
    assert "STARTING grid column" in result["error"]


def test_split_cell_rejects_when_already_vmerged(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)
    # Row-split col_index=1 (B1) -- its SIBLING, col_index=0 (A1), is the one
    # that becomes vMerge="restart" (see _split_rows: every OTHER cell in the
    # target row grows the merge). Then try to split THAT now-merged sibling.
    docs_intel.split_cell(path, table_index=1, row_index=0, col_index=1, rows=2)
    before = open(path, "rb").read()

    result = docs_intel.split_cell(path, table_index=1, row_index=0, col_index=0, cols=2)

    assert "error" in result
    assert result["reason"] == "unsupported_merge"
    assert open(path, "rb").read() == before


def test_split_cell_rejects_no_op_when_cols_and_rows_both_one(tmp_path):
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)

    result = docs_intel.split_cell(path, table_index=1, row_index=0, col_index=0)

    assert "error" in result


def test_split_cell_rejects_bad_row_index(tmp_path):
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)

    result = docs_intel.split_cell(path, table_index=1, row_index=9, col_index=0, cols=2)

    assert "error" in result


def test_split_cell_rejects_col_index_with_no_matching_cell(tmp_path):
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)

    result = docs_intel.split_cell(path, table_index=1, row_index=0, col_index=99, cols=2)

    assert "error" in result
    assert "reason" not in result or result.get("reason") != "unsupported_merge"


def test_split_cell_render_failed_restores_and_errors(tmp_path, monkeypatch):
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)
    before = open(path, "rb").read()
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "failed", "reason": "simulated render failure"},
    )

    result = docs_intel.split_cell(path, table_index=1, row_index=0, col_index=0, cols=2)

    assert "error" in result
    assert result["file_restored"] is True
    assert open(path, "rb").read() == before


def test_split_cell_server_wrapper_delegates(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)

    result = server.split_cell(path, table_index=1, row_index=0, col_index=0, cols=2)

    assert result["status"] == "split"


# ---------------------------------------------------------------------------
# transpose_table
# ---------------------------------------------------------------------------


def test_transpose_table_swaps_rows_and_columns(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)

    result = docs_intel.transpose_table(path, table_index=1)

    assert result["status"] == "transposed"
    assert result["row_count"] == 2
    assert result["col_count"] == 2
    tbl = _table(path, 1)
    rows = _cell_texts(tbl)
    # Original: [[A1, B1], [A2, B2]] -> transposed: [[A1, A2], [B1, B2]]
    assert rows == [["A1", "A2"], ["B1", "B2"]]
    assert _grid_col_count(tbl) == 2


def test_transpose_table_non_square_shape(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:tbl>
      <w:tblGrid>
        <w:gridCol w:w="1000"/><w:gridCol w:w="1000"/><w:gridCol w:w="1000"/>
      </w:tblGrid>
      <w:tr>
        <w:tc><w:p><w:r><w:t>A1</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>B1</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>C1</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>A2</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>B2</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>C2</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:sectPr/>
  </w:body>
</w:document>
"""
    path = _write_docx(tmp_path, xml)

    result = docs_intel.transpose_table(path, table_index=0)

    assert result["status"] == "transposed"
    assert result["row_count"] == 3   # was 3 columns
    assert result["col_count"] == 2   # was 2 rows
    tbl = _table(path, 0)
    rows = _cell_texts(tbl)
    assert rows == [["A1", "A2"], ["B1", "B2"], ["C1", "C2"]]
    assert _grid_col_count(tbl) == 2


def test_transpose_table_preserves_bookmarks_numbering_and_relationships(tmp_path, monkeypatch):
    """transpose_table reuses the SAME <w:tc> element objects -- everything
    inside a cell (bookmark, numbering reference, image relationship id)
    must survive verbatim, just at a new position."""
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _RICH_CELL_TABLE_XML)

    result = docs_intel.transpose_table(path, table_index=0)

    assert result["status"] == "transposed"
    tbl = _table(path, 0)
    xml_out = docs_intel.ET.tostring(tbl, encoding="unicode")
    assert 'w:name="myBookmark"' in xml_out
    assert 'w:numId w:val="7"' in xml_out
    assert 'r:embed="rId9"' in xml_out


def test_transpose_table_structural_counts_are_invariant(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)
    _raw0, root0 = docs_intel._load_docx_xml_stdlib(path)
    body0 = root0.find(docs_intel._q(_W, "body"))
    before_counts = docs_intel._structural_counts([body0])

    docs_intel.transpose_table(path, table_index=1)

    _raw1, root1 = docs_intel._load_docx_xml_stdlib(path)
    body1 = root1.find(docs_intel._q(_W, "body"))
    after_counts = docs_intel._structural_counts([body1])
    assert after_counts == before_counts


def test_transpose_table_rejects_gridspan(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _MERGED_ROW0_XML)
    before = open(path, "rb").read()

    result = docs_intel.transpose_table(path, table_index=0)

    assert "error" in result
    assert result["reason"] == "unsupported_merge"
    assert open(path, "rb").read() == before


def test_transpose_table_rejects_vmerge(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)
    docs_intel.split_cell(path, table_index=1, row_index=0, col_index=1, rows=2)
    before = open(path, "rb").read()

    result = docs_intel.transpose_table(path, table_index=1)

    assert "error" in result
    assert result["reason"] == "unsupported_merge"
    assert open(path, "rb").read() == before


def test_transpose_table_rejects_non_uniform_rows(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>A1</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>B1</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>A2</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:sectPr/>
  </w:body>
</w:document>
"""
    path = _write_docx(tmp_path, xml)

    result = docs_intel.transpose_table(path, table_index=0)

    assert "error" in result
    assert result["reason"] == "ambiguous_grid"


def test_transpose_table_rejects_bad_table_index(tmp_path):
    path = _write_docx(tmp_path, _NO_TABLE_XML)

    result = docs_intel.transpose_table(path, table_index=0)

    assert "error" in result


def test_transpose_table_render_unavailable_fails_closed_by_default(tmp_path, monkeypatch):
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)
    before = open(path, "rb").read()
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "unavailable-with-reason", "reason": "no backend"},
    )

    result = docs_intel.transpose_table(path, table_index=1)

    assert "error" in result
    assert result["file_restored"] is True
    assert open(path, "rb").read() == before


def test_transpose_table_degrades_with_audited_override(tmp_path, monkeypatch):
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "unavailable-with-reason", "reason": "no backend"},
    )

    result = docs_intel.transpose_table(
        path, table_index=1,
        allow_degraded_render=True,
        degraded_render_reason="CI sandbox has no LibreOffice/Word installed",
    )

    assert result["status"] == "transposed"
    assert result["render_verified"] is False
    assert result["render_degraded"] is True


def test_transpose_table_server_wrapper_delegates(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)

    result = server.transpose_table(path, table_index=1)

    assert result["status"] == "transposed"


# ---------------------------------------------------------------------------
# Never uses the unsafe whole-document native rewrite path -- these three
# primitives must go through the SAME disposable-copy write pipeline as
# every other writer in this module (relocate_table, insert_caption, ...).
# ---------------------------------------------------------------------------


def test_insert_column_leaves_a_bak_backup_after_a_successful_write(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)

    docs_intel.insert_column(path, table_index=1, col_index=0)

    import os
    assert os.path.exists(path + ".bak"), (
        "a successful write must go through _save_docx_xml_stdlib's disposable-"
        "copy + backup discipline, not an in-place native rewrite"
    )


def test_split_cell_and_transpose_are_zip_valid_after_write(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)

    docs_intel.split_cell(path, table_index=1, row_index=0, col_index=0, cols=2)
    with zipfile.ZipFile(path) as zf:
        assert zf.testzip() is None
        assert "word/document.xml" in zf.namelist()

    docs_intel.transpose_table(path, table_index=1)
    with zipfile.ZipFile(path) as zf:
        assert zf.testzip() is None


# ---------------------------------------------------------------------------
# insert_table / remove_table (0a1e9c22)
# ---------------------------------------------------------------------------

_NO_TABLE_WITH_HEADING_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}">
  <w:body>
    <w:p w14:paraId="H0000001"><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Section One</w:t></w:r></w:p>
    <w:p w14:paraId="P0000001"><w:r><w:t>Section one body.</w:t></w:r></w:p>
    <w:p w14:paraId="H0000002"><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>Subsection</w:t></w:r></w:p>
    <w:p w14:paraId="P0000002"><w:r><w:t>Subsection body.</w:t></w:r></w:p>
    <w:p w14:paraId="H0000003"><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Section Two</w:t></w:r></w:p>
    <w:p w14:paraId="P0000003"><w:r><w:t>Section two body.</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""

_BOOKMARK_ACROSS_TWO_PARAGRAPHS_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}">
  <w:body>
    <w:p w14:paraId="P0000001">
      <w:bookmarkStart w:id="1" w:name="spans"/>
      <w:r><w:t>First half.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="P0000002">
      <w:r><w:t>Second half.</w:t></w:r>
      <w:bookmarkEnd w:id="1"/>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""


def test_insert_table_after_anchor_lands_where_expected(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _NO_TABLE_XML)

    result = docs_intel.insert_table(path, anchor_para_id="p0", rows=2, cols=3, position="after")

    assert result["status"] == "inserted"
    assert result["row_count"] == 2
    assert result["col_count"] == 3
    assert result["render_verified"] is True
    tbl = _table(path, result["table_index"])
    assert _grid_col_count(tbl) == 3
    assert _cell_texts(tbl) == [["", "", ""], ["", "", ""]]


def test_insert_table_before_anchor_lands_before_it(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _NO_TABLE_XML)

    result = docs_intel.insert_table(path, anchor_para_id="p0", rows=1, cols=1, position="before")

    root = _load(path)
    body_list = list(root.find(docs_intel._q(_W, "body")))
    assert body_list[result["table_index"]].tag == docs_intel._q(_W, "tbl")
    assert result["table_index"] == 0


def test_insert_table_uses_table_grid_style(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _NO_TABLE_XML)

    docs_intel.insert_table(path, anchor_para_id="p0", rows=1, cols=1, position="after")

    tbl = _table(path, 1)
    style = tbl.find(docs_intel._q(_W, "tblPr")).find(docs_intel._q(_W, "tblStyle"))
    assert style.get(docs_intel._q(_W, "val")) == "TableGrid"


def test_insert_table_with_cell_texts_fills_them_in(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _NO_TABLE_XML)

    docs_intel.insert_table(
        path, anchor_para_id="p0", rows=2, cols=2,
        cell_texts=[["a", "b"], ["c", "d"]],
    )

    tbl = _table(path, 1)
    assert _cell_texts(tbl) == [["a", "b"], ["c", "d"]]


def test_insert_table_new_cells_have_fresh_unique_para_ids(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _NO_TABLE_XML)

    docs_intel.insert_table(path, anchor_para_id="p0", rows=2, cols=2)

    tbl = _table(path, 1)
    ids = [
        p.get(docs_intel._q(_W14, "paraId"))
        for tr in tbl.findall(docs_intel._q(_W, "tr"))
        for tc in tr.findall(docs_intel._q(_W, "tc"))
        for p in tc.findall(docs_intel._q(_W, "p"))
    ]
    assert all(ids)
    assert len(set(ids)) == len(ids)


def test_insert_table_no_style_policy_adds_no_jc(tmp_path, monkeypatch):
    """4544bbe5 -- default (no style_policy) reproduces pre-4544bbe5 output
    exactly: no w:jc at all on any new cell paragraph."""
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _NO_TABLE_XML)

    docs_intel.insert_table(path, anchor_para_id="p0", rows=2, cols=3)

    tbl = _table(path, 1)
    assert _cell_alignments(tbl) == [[None, None, None], [None, None, None]]


def test_insert_table_style_policy_sets_label_and_data_column_alignment(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _NO_TABLE_XML)

    docs_intel.insert_table(
        path, anchor_para_id="p0", rows=2, cols=3,
        style_policy={
            "table_label_column_alignment": "left",
            "table_data_column_alignment": "center",
        },
    )

    tbl = _table(path, 1)
    assert _cell_alignments(tbl) == [
        ["left", "center", "center"],
        ["left", "center", "center"],
    ]


def test_insert_table_style_policy_label_only_leaves_data_columns_unset(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _NO_TABLE_XML)

    docs_intel.insert_table(
        path, anchor_para_id="p0", rows=1, cols=2,
        style_policy={"table_label_column_alignment": "right"},
    )

    tbl = _table(path, 1)
    assert _cell_alignments(tbl) == [["right", None]]


def test_insert_table_rejects_bad_style_policy_without_mutating_file(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _NO_TABLE_XML)
    with open(path, "rb") as fh:
        original_bytes = fh.read()

    result = docs_intel.insert_table(
        path, anchor_para_id="p0", rows=1, cols=1,
        style_policy={"table_label_column_alignment": "diagonal"},
    )
    assert "error" in result
    with open(path, "rb") as fh:
        assert fh.read() == original_bytes


def test_insert_table_server_wrapper_passes_through_style_policy(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _NO_TABLE_XML)

    result = server.insert_table(
        path, anchor_para_id="p0", rows=1, cols=2,
        style_policy={
            "table_label_column_alignment": "left",
            "table_data_column_alignment": "center",
        },
    )
    assert result["status"] == "inserted"
    tbl = _table(path, result["table_index"])
    assert _cell_alignments(tbl) == [["left", "center"]]


def test_insert_table_after_heading_lands_after_whole_section(tmp_path, monkeypatch):
    """Mirrors relocate_table's own heading-anchor behavior: an "after"
    anchor resolving to a heading lands after that heading's ENTIRE
    section (body + subsections), not merely after the heading paragraph."""
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _NO_TABLE_WITH_HEADING_XML)

    result = docs_intel.insert_table(
        path, anchor_para_id="H0000001", rows=1, cols=1, position="after",
    )

    root = _load(path)
    body_list = list(root.find(docs_intel._q(_W, "body")))
    w14_paraId = docs_intel._q(_W14, "paraId")
    table_pos = result["table_index"]
    # Section One's subsection (H0000002 + its body) must come BEFORE the
    # table -- i.e. the table landed after ALL of Section One, not right
    # after its own heading paragraph.
    subsection_idx = next(
        i for i, el in enumerate(body_list) if el.get(w14_paraId) == "H0000002"
    )
    assert subsection_idx < table_pos
    # Section Two must come AFTER the table.
    section_two_idx = next(
        i for i, el in enumerate(body_list) if el.get(w14_paraId) == "H0000003"
    )
    assert section_two_idx > table_pos


def test_insert_table_rejects_bad_position(tmp_path):
    path = _write_docx(tmp_path, _NO_TABLE_XML)

    result = docs_intel.insert_table(path, anchor_para_id="p0", rows=1, cols=1, position="sideways")

    assert "error" in result


def test_insert_table_rejects_non_positive_rows_or_cols(tmp_path):
    path = _write_docx(tmp_path, _NO_TABLE_XML)

    assert "error" in docs_intel.insert_table(path, anchor_para_id="p0", rows=0, cols=1)
    assert "error" in docs_intel.insert_table(path, anchor_para_id="p0", rows=1, cols=-1)
    assert "error" in docs_intel.insert_table(path, anchor_para_id="p0", rows=True, cols=1)


def test_insert_table_rejects_mismatched_cell_texts_shape(tmp_path):
    path = _write_docx(tmp_path, _NO_TABLE_XML)
    before = open(path, "rb").read()

    result = docs_intel.insert_table(
        path, anchor_para_id="p0", rows=2, cols=2, cell_texts=[["only one row"]],
    )

    assert "error" in result
    assert open(path, "rb").read() == before


def test_insert_table_rejects_unknown_anchor(tmp_path):
    path = _write_docx(tmp_path, _NO_TABLE_XML)
    before = open(path, "rb").read()

    result = docs_intel.insert_table(path, anchor_para_id="bogus", rows=1, cols=1)

    assert "error" in result
    assert open(path, "rb").read() == before


def test_insert_table_between_a_bookmarks_start_and_end_is_not_a_split(tmp_path, monkeypatch):
    """Unlike remove_table/relocate_table, inserting brand-new content
    between an existing bookmarkStart and bookmarkEnd only widens the
    bookmark's span -- it can never sever it, so this must succeed with no
    bookmark-split guard at all."""
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _BOOKMARK_ACROSS_TWO_PARAGRAPHS_XML)

    result = docs_intel.insert_table(path, anchor_para_id="P0000001", rows=1, cols=1, position="after")

    assert result["status"] == "inserted"


def test_insert_table_render_failed_restores_and_errors(tmp_path, monkeypatch):
    path = _write_docx(tmp_path, _NO_TABLE_XML)
    before = open(path, "rb").read()
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability",
        lambda p, **kwargs: {"status": "failed", "backend": "test-stub", "detail": {"reason": "boom"}},
    )

    result = docs_intel.insert_table(path, anchor_para_id="p0", rows=1, cols=1)

    assert "error" in result
    assert open(path, "rb").read() == before


def test_insert_table_allow_degraded_render_requires_non_empty_reason(tmp_path):
    path = _write_docx(tmp_path, _NO_TABLE_XML)

    result = docs_intel.insert_table(
        path, anchor_para_id="p0", rows=1, cols=1, allow_degraded_render=True,
    )

    assert "error" in result


def test_insert_table_server_wrapper_delegates(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _NO_TABLE_XML)

    result = server.insert_table(path, anchor_para_id="p0", rows=1, cols=1)

    assert result["status"] == "inserted"


def test_remove_table_removes_it_and_preserves_surrounding_content(tmp_path, monkeypatch):
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)

    result = docs_intel.remove_table(path, table_index=1)

    assert result["status"] == "removed"
    assert result["row_count"] == 2
    assert result["col_count"] == 2
    root = _load(path)
    body_list = list(root.find(docs_intel._q(_W, "body")))
    assert not any(el.tag == docs_intel._q(_W, "tbl") for el in body_list)
    w14_paraId = docs_intel._q(_W14, "paraId")
    body_ids = [el.get(w14_paraId) for el in body_list]
    assert "P0000001" in body_ids and "P0000002" in body_ids


def test_remove_table_rejects_bad_table_index(tmp_path):
    path = _write_docx(tmp_path, _NO_TABLE_XML)

    result = docs_intel.remove_table(path, table_index=0)

    assert "error" in result
    assert "not a <w:tbl>" in result["error"]


def test_remove_table_rejects_out_of_range_index(tmp_path):
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)
    before = open(path, "rb").read()

    result = docs_intel.remove_table(path, table_index=99)

    assert "error" in result
    assert open(path, "rb").read() == before


def test_remove_table_does_not_invoke_the_render_gate(tmp_path, monkeypatch):
    """Deleting valid content can't manufacture malformed OOXML -- same
    reasoning relocate_table already applies to a move."""
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)

    def _boom(*_args, **_kwargs):
        raise AssertionError("remove_table must not call the render gate")

    monkeypatch.setattr(docs_intel.render_gate, "check_render_capability", _boom)

    result = docs_intel.remove_table(path, table_index=1)

    assert result["status"] == "removed"


def test_remove_table_rejects_split_bookmark_by_default(tmp_path):
    """A bookmark starting BEFORE the table and ending INSIDE one of its own
    cells would have its end carried away by the removal while its start
    stays behind -- genuinely split, must be refused."""
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}">
  <w:body>
    <w:p w14:paraId="P0000001">
      <w:bookmarkStart w:id="1" w:name="spans"/>
      <w:r><w:t>Before.</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tblGrid><w:gridCol w:w="1000"/></w:tblGrid>
      <w:tr><w:tc><w:p><w:r><w:t>Cell.</w:t></w:r></w:p><w:bookmarkEnd w:id="1"/></w:tc></w:tr>
    </w:tbl>
    <w:p w14:paraId="P0000002">
      <w:r><w:t>After.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""
    path = _write_docx(tmp_path, xml)
    before = open(path, "rb").read()

    result = docs_intel.remove_table(path, table_index=1)

    assert "error" in result
    assert result["reason"] == "split_bookmarks"
    assert open(path, "rb").read() == before


def test_remove_table_server_wrapper_delegates(tmp_path):
    path = _write_docx(tmp_path, _SIMPLE_2X2_XML)

    result = server.remove_table(path, table_index=1)

    assert result["status"] == "removed"


def test_insert_then_remove_table_round_trip_restores_original_document(tmp_path, monkeypatch):
    _rendered_ok(monkeypatch)
    path = _write_docx(tmp_path, _NO_TABLE_XML)

    def _paragraph_texts_and_tags(p):
        root = _load(p)
        body_list = list(root.find(docs_intel._q(_W, "body")))
        from meridian_docs._vendored_content_tree import _paragraph_text
        return [
            (el.tag, _paragraph_text(el) if el.tag == docs_intel._q(_W, "p") else None)
            for el in body_list
        ]

    original = _paragraph_texts_and_tags(path)

    insert_result = docs_intel.insert_table(
        path, anchor_para_id="p0", rows=2, cols=2, cell_texts=[["a", "b"], ["c", "d"]],
    )
    assert insert_result["status"] == "inserted"

    remove_result = docs_intel.remove_table(path, table_index=insert_result["table_index"])
    assert remove_result["status"] == "removed"

    final = _paragraph_texts_and_tags(path)
    assert final == original, "the round trip must restore the exact original body structure and text"
