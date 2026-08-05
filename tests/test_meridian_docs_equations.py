"""Tests for OMML/equation support in meridian-docs (a80af3a0).

Covers:
  - parse_docx_equations_local: standalone-paragraph and table-numbered patterns
  - latex_to_omml_local / _omml_flatten_text_local helpers
  - index_docx_equations / get_local_equations sidecar round-trip
  - insert_equation_local: before / after / append positions, OMML + LaTeX payloads
  - edit_equation_local: OMML replacement
  - remove_equation_local: display-mode (whole paragraph) and inline (oMath only)
  - Error paths: unknown doc, bad para_id, malformed OMML, file unchanged on error

All tests use synthetic .docx bytes built inline — no real files, no network.
"""
from __future__ import annotations

import io
import os
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

# Captured at import time, before the autouse fixture below ever monkeypatches
# docs_intel.render_gate.check_render_capability -- the one test in this file
# that needs the GENUINE implementation (see
# test_insert_equation_local_real_render_gate_or_structural_fallback) cannot
# recover it from docs_intel.render_gate afterwards, since monkeypatch.setattr
# mutates that shared module object's namespace directly.
_REAL_CHECK_RENDER_CAPABILITY = docs_intel.render_gate.check_render_capability


@pytest.fixture(autouse=True)
def _default_render_capability(monkeypatch):
    """016015e1/ddd79188 -- insert_equation_local, insert_caption, and
    insert_highlighted_note (mode="inline") now invoke the real
    render-capability gate (render_gate.check_render_capability) AFTER
    structural verification passes. Every test in this file exercises
    STRUCTURAL correctness and must not depend on -- or be slowed/blocked
    by -- whichever render backends (LibreOffice, Word COM) happen to be
    installed on the machine running the suite. Stub a successful
    'rendered' result by default, mirroring
    test_19be1551_insert_figure_block.py's fixture of the same name. Tests
    that specifically exercise the render gate's own contract override this
    stub explicitly (see test_docx_word_com_regression.py).
    """
    monkeypatch.setattr(
        docs_intel.render_gate,
        "check_render_capability",
        lambda docx_path, **kwargs: {
            "status": "rendered",
            "backend": "test-stub",
            "detail": {"stub": True},
        },
    )


# ---------------------------------------------------------------------------
# Shared constants and helpers
# ---------------------------------------------------------------------------

_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"


def _zip_docx(xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def _write_docx(tmp_path, xml: str, name: str = "sample.docx") -> str:
    path = tmp_path / name
    path.write_bytes(_zip_docx(xml))
    return str(path)


def _read_docx_xml(path: str) -> bytes:
    with zipfile.ZipFile(path) as zf:
        return zf.read("word/document.xml")


def _count_omath(path: str) -> int:
    import xml.etree.ElementTree as ET  # noqa: PLC0415
    root = ET.fromstring(_read_docx_xml(path))
    return sum(1 for _ in root.iter(f"{{{_M}}}oMath"))


def _count_paragraphs(path: str) -> int:
    import xml.etree.ElementTree as ET  # noqa: PLC0415
    root = ET.fromstring(_read_docx_xml(path))
    return sum(1 for _ in root.iter(f"{{{_W}}}p"))


# ---------------------------------------------------------------------------
# Synthetic .docx XML fixtures
# ---------------------------------------------------------------------------

# A document with:
#   - one heading paragraph (0000A001)
#   - a standalone inline equation in 0000A002 (E=mc^2)
#   - a standalone inline equation in 0000A003 (F=ma)
#   - a plain text paragraph 0000A004 (no equation)
_STANDALONE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"
    xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">
  <w:body>
    <w:p w14:paraId="0000A001">
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Physics</w:t></w:r>
    </w:p>
    <w:p w14:paraId="0000A002">
      <w:r><w:t>Einstein: </w:t></w:r>
      <m:oMath>
        <m:r><m:t>E</m:t></m:r>
        <m:r><m:t>=</m:t></m:r>
        <m:r><m:t>m</m:t></m:r>
        <m:sSup>
          <m:e><m:r><m:t>c</m:t></m:r></m:e>
          <m:sup><m:r><m:t>2</m:t></m:r></m:sup>
        </m:sSup>
      </m:oMath>
    </w:p>
    <w:p w14:paraId="0000A003">
      <m:oMath>
        <m:r><m:t>F</m:t></m:r>
        <m:r><m:t>=</m:t></m:r>
        <m:r><m:t>m</m:t></m:r>
        <m:r><m:t>a</m:t></m:r>
      </m:oMath>
    </w:p>
    <w:p w14:paraId="0000A004">
      <w:r><w:t>No equation here.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""

# A document with a table-numbered equation: first cell has oMath, second has "(1)".
# Also contains a non-numbered 2-column table (should be treated as standalone).
_TABLE_NUMBERED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"
    xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">
  <w:body>
    <w:p w14:paraId="0000B001">
      <w:r><w:t>Equation table:</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tr>
        <w:tc>
          <w:p w14:paraId="0000B002">
            <m:oMath>
              <m:r><m:t>E</m:t></m:r>
              <m:r><m:t>=</m:t></m:r>
              <m:r><m:t>m</m:t></m:r>
              <m:sSup>
                <m:e><m:r><m:t>c</m:t></m:r></m:e>
                <m:sup><m:r><m:t>2</m:t></m:r></m:sup>
              </m:sSup>
            </m:oMath>
          </w:p>
        </w:tc>
        <w:tc>
          <w:p><w:r><w:t>(1)</w:t></w:r></w:p>
        </w:tc>
      </w:tr>
      <w:tr>
        <w:tc>
          <w:p w14:paraId="0000B003">
            <m:oMath>
              <m:f>
                <m:num><m:r><m:t>a</m:t></m:r></m:num>
                <m:den><m:r><m:t>b</m:t></m:r></m:den>
              </m:f>
            </m:oMath>
          </w:p>
        </w:tc>
        <w:tc>
          <w:p><w:r><w:t>(2a)</w:t></w:r></w:p>
        </w:tc>
      </w:tr>
    </w:tbl>
    <w:tbl>
      <w:tr>
        <w:tc>
          <w:p><w:r><w:t>Header 1</w:t></w:r></w:p>
        </w:tc>
        <w:tc>
          <w:p><w:r><w:t>Header 2</w:t></w:r></w:p>
        </w:tc>
      </w:tr>
      <w:tr>
        <w:tc>
          <w:p>
            <m:oMath><m:r><m:t>x</m:t></m:r></m:oMath>
          </w:p>
        </w:tc>
        <w:tc>
          <w:p><w:r><w:t>not a number</w:t></w:r></w:p>
        </w:tc>
      </w:tr>
    </w:tbl>
  </w:body>
</w:document>
"""

# A minimal document with one equation paragraph for write-back tests.
_WRITE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"
    xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">
  <w:body>
    <w:p w14:paraId="0000C001">
      <w:r><w:t>Introduction.</w:t></w:r>
    </w:p>
    <w:p w14:paraId="0000C002">
      <m:oMath>
        <m:r><m:t>E</m:t></m:r>
        <m:r><m:t>=</m:t></m:r>
        <m:r><m:t>m</m:t></m:r>
        <m:sSup>
          <m:e><m:r><m:t>c</m:t></m:r></m:e>
          <m:sup><m:r><m:t>2</m:t></m:r></m:sup>
        </m:sSup>
      </m:oMath>
    </w:p>
    <w:p w14:paraId="0000C003">
      <w:r><w:t>Conclusion.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""

# A document with an inline equation appended to a text paragraph.
_INLINE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"
    xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">
  <w:body>
    <w:p w14:paraId="0000D001">
      <w:r><w:t>See formula: </w:t></w:r>
      <m:oMath><m:r><m:t>F</m:t></m:r><m:r><m:t>=</m:t></m:r><m:r><m:t>ma</m:t></m:r></m:oMath>
    </w:p>
    <w:p w14:paraId="0000D002">
      <w:r><w:t>End.</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
"""

_EMPTY_XML = """<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>No equations.</w:t></w:r></w:p>
  </w:body>
</w:document>
"""


# ---------------------------------------------------------------------------
# _omml_flatten_text_local
# ---------------------------------------------------------------------------

def test_omml_flatten_text_local_returns_concatenated_m_t_runs():
    omml = (
        f'<m:oMath xmlns:m="{_M}">'
        "<m:r><m:t>E</m:t></m:r>"
        "<m:r><m:t>=</m:t></m:r>"
        "<m:r><m:t>mc2</m:t></m:r>"
        "</m:oMath>"
    )
    assert docs_intel._omml_flatten_text_local(omml) == "E=mc2"


def test_omml_flatten_text_local_handles_empty_and_malformed():
    assert docs_intel._omml_flatten_text_local(None) == ""
    assert docs_intel._omml_flatten_text_local("") == ""
    assert docs_intel._omml_flatten_text_local("<not-xml") == ""


# ---------------------------------------------------------------------------
# latex_to_omml_local
# ---------------------------------------------------------------------------

def test_latex_to_omml_local_converts_superscript():
    result = docs_intel.latex_to_omml_local("x^2")
    assert result is not None
    assert "<m:sSup" in result


def test_latex_to_omml_local_converts_fraction():
    result = docs_intel.latex_to_omml_local(r"\frac{a}{b}")
    assert result is not None
    assert "<m:f>" in result
    assert "<m:num>" in result
    assert "<m:den>" in result


def test_latex_to_omml_local_converts_subscript():
    result = docs_intel.latex_to_omml_local("x_1")
    assert result is not None
    assert "<m:sSub" in result


def test_latex_to_omml_local_converts_sqrt():
    result = docs_intel.latex_to_omml_local(r"\sqrt{x}")
    assert result is not None
    assert "<m:rad>" in result


def test_latex_to_omml_local_returns_none_for_blank_input():
    assert docs_intel.latex_to_omml_local("") is None
    assert docs_intel.latex_to_omml_local("   ") is None
    assert docs_intel.latex_to_omml_local(None) is None


def test_latex_to_omml_local_round_trips_flat_text():
    """LaTeX "E=mc^2" should produce OMML whose flat text contains E, =, m, c, 2."""
    result = docs_intel.latex_to_omml_local("E=mc^2")
    assert result is not None
    flat = docs_intel._omml_flatten_text_local(result)
    # Flat text order may vary slightly by mathml converter, but all chars must appear.
    for ch in ("E", "=", "m", "c", "2"):
        assert ch in flat, f"expected {ch!r} in flat text {flat!r}"


def test_latex_to_omml_local_guarded_against_latex2mathml_failure(monkeypatch):
    """A latex2mathml exception must degrade to None, never raise."""
    import latex2mathml.converter as l2m  # noqa: PLC0415
    monkeypatch.setattr(l2m, "convert", lambda _: (_ for _ in ()).throw(RuntimeError("bad")))
    assert docs_intel.latex_to_omml_local("x^2") is None


# ---------------------------------------------------------------------------
# parse_docx_equations_local — standalone pattern
# ---------------------------------------------------------------------------

def test_parse_docx_equations_local_standalone_reads_omml():
    data = _zip_docx(_STANDALONE_XML)
    equations = docs_intel.parse_docx_equations_local(data)
    assert len(equations) == 2

    eq0 = equations[0]
    assert eq0["ordinal"] == 0
    assert eq0["para_id"] == "0000A002"
    assert eq0["pattern"] == "standalone"
    assert eq0["number"] is None
    assert "E" in eq0["flat_text"]
    assert "<m:oMath" in eq0["omml_raw"]

    eq1 = equations[1]
    assert eq1["ordinal"] == 1
    assert eq1["para_id"] == "0000A003"
    assert eq1["pattern"] == "standalone"
    assert "F" in eq1["flat_text"]


def test_parse_docx_equations_local_empty_document_returns_empty():
    data = _zip_docx(_EMPTY_XML)
    assert docs_intel.parse_docx_equations_local(data) == []


def test_parse_docx_equations_local_file_path(tmp_path):
    path = tmp_path / "sample.docx"
    path.write_bytes(_zip_docx(_STANDALONE_XML))
    equations = docs_intel.parse_docx_equations_local(str(path))
    assert len(equations) == 2


# ---------------------------------------------------------------------------
# parse_docx_equations_local — table-numbered pattern
# ---------------------------------------------------------------------------

def test_parse_docx_equations_local_table_numbered_detected():
    data = _zip_docx(_TABLE_NUMBERED_XML)
    equations = docs_intel.parse_docx_equations_local(data)

    # Expected: 2 table-numbered + 1 standalone (x in the non-numbered table).
    assert len(equations) == 3

    numbered = [e for e in equations if e["pattern"] == "table-numbered"]
    standalone = [e for e in equations if e["pattern"] == "standalone"]
    assert len(numbered) == 2
    assert len(standalone) == 1

    # First numbered equation.
    eq1 = numbered[0]
    assert eq1["number"] == "(1)"
    assert eq1["para_id"] == "0000B002"
    # Flat text should contain E, =, m, c, 2.
    for ch in ("E", "=", "m", "c", "2"):
        assert ch in eq1["flat_text"]

    # Second numbered equation.
    eq2 = numbered[1]
    assert eq2["number"] == "(2a)"
    assert eq2["para_id"] == "0000B003"
    assert "ab" in eq2["flat_text"]  # fraction a/b

    # Non-numbered: "x" in a regular table cell.
    assert standalone[0]["flat_text"] == "x"
    assert standalone[0]["number"] is None


def test_parse_docx_equations_local_various_number_formats():
    """Verify that different parenthesised-number formats are recognised."""
    def _make_xml(num_text: str) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"
    xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">
  <w:body>
    <w:tbl>
      <w:tr>
        <w:tc>
          <w:p><m:oMath><m:r><m:t>x</m:t></m:r></m:oMath></w:p>
        </w:tc>
        <w:tc>
          <w:p><w:r><w:t>{num_text}</w:t></w:r></w:p>
        </w:tc>
      </w:tr>
    </w:tbl>
  </w:body>
</w:document>"""

    for num in ("(1)", "(2a)", "(A.1)", "(eq3)", "( 1 )"):
        eqs = docs_intel.parse_docx_equations_local(_zip_docx(_make_xml(num)))
        assert len(eqs) == 1, f"expected 1 equation for number {num!r}, got {len(eqs)}"
        assert eqs[0]["pattern"] == "table-numbered", f"pattern wrong for {num!r}"
        assert eqs[0]["number"] == num.strip() or eqs[0]["number"] is not None

    # Non-matching second cell should NOT produce table-numbered.
    for non_num in ("not-a-number", "1", "eq1", "[1]", "{1}"):
        eqs = docs_intel.parse_docx_equations_local(_zip_docx(_make_xml(non_num)))
        assert all(e["pattern"] == "standalone" for e in eqs), \
            f"expected standalone for non-number {non_num!r}"


# ---------------------------------------------------------------------------
# index_docx_equations / get_local_equations — sidecar round-trip
# ---------------------------------------------------------------------------

def test_index_and_get_equations_round_trip(tmp_path):
    db_path = str(tmp_path / "test.db")
    result = docs_intel.index_docx_equations(_zip_docx(_STANDALONE_XML), db_path)
    assert result["equation_count"] == 2
    assert result["index_db"] == db_path

    eqs = docs_intel.get_local_equations(db_path)
    assert len(eqs) == 2
    assert eqs[0]["ordinal"] == 0
    assert eqs[0]["para_id"] == "0000A002"
    assert eqs[0]["pattern"] == "standalone"
    assert "E" in eqs[0]["flat_text"]


def test_index_equations_is_idempotent(tmp_path):
    db_path = str(tmp_path / "test.db")
    docs_intel.index_docx_equations(_zip_docx(_STANDALONE_XML), db_path)
    docs_intel.index_docx_equations(_zip_docx(_STANDALONE_XML), db_path)
    eqs = docs_intel.get_local_equations(db_path)
    assert len(eqs) == 2  # not doubled


def test_get_local_equations_empty_when_not_indexed(tmp_path):
    db_path = str(tmp_path / "empty.db")
    eqs = docs_intel.get_local_equations(db_path)
    assert eqs == []


def test_index_equations_table_numbered_stored_correctly(tmp_path):
    db_path = str(tmp_path / "tbl.db")
    docs_intel.index_docx_equations(_zip_docx(_TABLE_NUMBERED_XML), db_path)
    eqs = docs_intel.get_local_equations(db_path)
    numbered = [e for e in eqs if e["pattern"] == "table-numbered"]
    assert len(numbered) == 2
    assert numbered[0]["number"] == "(1)"
    assert numbered[1]["number"] == "(2a)"


# ---------------------------------------------------------------------------
# insert_equation_local — OMML payload
# ---------------------------------------------------------------------------

_SIMPLE_OMATH = (
    f'<m:oMath xmlns:m="{_M}">'
    "<m:r><m:t>z</m:t></m:r>"
    "</m:oMath>"
)


def test_insert_equation_local_after_appends_display_paragraph(tmp_path):
    path = _write_docx(tmp_path, _WRITE_XML)
    base_paras = _count_paragraphs(path)
    result = docs_intel.insert_equation_local(path, "0000C003", _SIMPLE_OMATH, "after")
    assert "error" not in result
    assert result["status"] == "inserted"
    assert result["position"] == "after"
    assert "<m:oMath" in result["omml"]
    assert _count_paragraphs(path) == base_paras + 1
    assert _count_omath(path) == 2  # original E=mc^2 + new z


def test_insert_equation_local_before_inserts_display_paragraph(tmp_path):
    path = _write_docx(tmp_path, _WRITE_XML)
    base_paras = _count_paragraphs(path)
    result = docs_intel.insert_equation_local(path, "0000C001", _SIMPLE_OMATH, "before")
    assert "error" not in result
    assert _count_paragraphs(path) == base_paras + 1
    assert _count_omath(path) == 2


def test_insert_equation_local_append_adds_inline_omath(tmp_path):
    path = _write_docx(tmp_path, _WRITE_XML)
    base_paras = _count_paragraphs(path)
    result = docs_intel.insert_equation_local(path, "0000C003", _SIMPLE_OMATH, "append")
    assert "error" not in result
    # Append = inline, no new paragraph.
    assert _count_paragraphs(path) == base_paras
    assert _count_omath(path) == 2


# ---------------------------------------------------------------------------
# insert_equation_local — LaTeX payload
# ---------------------------------------------------------------------------

def test_insert_equation_local_latex_converted_to_omml(tmp_path):
    path = _write_docx(tmp_path, _WRITE_XML)
    result = docs_intel.insert_equation_local(path, "0000C003", r"\frac{a}{b}", "after")
    assert "error" not in result
    assert "<m:f>" in result["omml"]
    assert _count_omath(path) == 2


def test_insert_equation_local_latex_emc2(tmp_path):
    path = _write_docx(tmp_path, _WRITE_XML)
    result = docs_intel.insert_equation_local(path, "0000C001", "E=mc^2", "before")
    assert "error" not in result
    assert "<m:oMath" in result["omml"]


def test_insert_equation_local_latex_sqrt(tmp_path):
    path = _write_docx(tmp_path, _WRITE_XML)
    result = docs_intel.insert_equation_local(path, "0000C001", r"\sqrt{x}", "after")
    assert "error" not in result
    assert "<m:rad>" in result["omml"]


# ---------------------------------------------------------------------------
# insert_equation_local — error paths
# ---------------------------------------------------------------------------

def test_insert_equation_local_bad_para_id_does_not_mutate(tmp_path):
    path = _write_docx(tmp_path, _WRITE_XML)
    before = _read_docx_xml(path)
    result = docs_intel.insert_equation_local(path, "does-not-exist", _SIMPLE_OMATH)
    assert "error" in result
    assert "does-not-exist" in result["error"]
    assert _read_docx_xml(path) == before


def test_insert_equation_local_unknown_file_returns_error():
    result = docs_intel.insert_equation_local("/no/such/file.docx", "p0", _SIMPLE_OMATH)
    assert "error" in result


def test_insert_equation_local_blank_payload_returns_error(tmp_path):
    path = _write_docx(tmp_path, _WRITE_XML)
    before = _read_docx_xml(path)
    result = docs_intel.insert_equation_local(path, "0000C001", "   ")
    assert "error" in result
    assert _read_docx_xml(path) == before


def test_insert_equation_local_malformed_omml_returns_error(tmp_path):
    path = _write_docx(tmp_path, _WRITE_XML)
    before = _read_docx_xml(path)
    result = docs_intel.insert_equation_local(path, "0000C001", "<m:oMath><unclosed")
    assert "error" in result
    assert "not valid XML" in result["error"]
    assert _read_docx_xml(path) == before


def test_insert_equation_local_bad_position_returns_error(tmp_path):
    path = _write_docx(tmp_path, _WRITE_XML)
    result = docs_intel.insert_equation_local(path, "0000C001", _SIMPLE_OMATH, "sideways")
    assert "error" in result
    assert "position" in result["error"]


def test_insert_equation_local_blank_latex_returns_error(tmp_path):
    """An unresolvable LaTeX string (blank after strip) must return an error."""
    path = _write_docx(tmp_path, _WRITE_XML)
    before = _read_docx_xml(path)
    # Empty LaTeX — latex_to_omml_local returns None — must error before write.
    result = docs_intel.insert_equation_local(path, "0000C001", "")
    assert "error" in result
    assert _read_docx_xml(path) == before


# ---------------------------------------------------------------------------
# edit_equation_local
# ---------------------------------------------------------------------------

def test_edit_equation_local_replaces_omath_with_omml(tmp_path):
    path = _write_docx(tmp_path, _WRITE_XML)
    new_omml = (
        f'<m:oMath xmlns:m="{_M}">'
        "<m:r><m:t>y</m:t></m:r>"
        "</m:oMath>"
    )
    result = docs_intel.edit_equation_local(path, "0000C002", new_omml)
    assert "error" not in result
    assert result["status"] == "edited"
    assert "y" in docs_intel._omml_flatten_text_local(result["omml"])
    # Still exactly one oMath in the file.
    assert _count_omath(path) == 1
    # Flat text of the equation is now "y".
    eqs = docs_intel.parse_docx_equations_local(path)
    assert any("y" in e["flat_text"] for e in eqs)


def test_edit_equation_local_replaces_omath_with_latex(tmp_path):
    path = _write_docx(tmp_path, _WRITE_XML)
    result = docs_intel.edit_equation_local(path, "0000C002", r"\frac{p}{q}")
    assert "error" not in result
    assert "<m:f>" in result["omml"]


def test_edit_equation_local_bad_para_id_does_not_mutate(tmp_path):
    path = _write_docx(tmp_path, _WRITE_XML)
    before = _read_docx_xml(path)
    result = docs_intel.edit_equation_local(path, "no-such-para", _SIMPLE_OMATH)
    assert "error" in result
    assert _read_docx_xml(path) == before


def test_edit_equation_local_non_equation_para_returns_error(tmp_path):
    """Editing a paragraph that contains no oMath must return an error."""
    path = _write_docx(tmp_path, _WRITE_XML)
    before = _read_docx_xml(path)
    result = docs_intel.edit_equation_local(path, "0000C001", _SIMPLE_OMATH)
    assert "error" in result
    assert _read_docx_xml(path) == before


def test_edit_equation_local_malformed_new_payload_returns_error(tmp_path):
    path = _write_docx(tmp_path, _WRITE_XML)
    before = _read_docx_xml(path)
    result = docs_intel.edit_equation_local(path, "0000C002", "<m:oMath><unclosed")
    assert "error" in result
    assert _read_docx_xml(path) == before

def test_edit_equation_local_preserves_surrounding_run_order(tmp_path):
    xml = _WRITE_XML.replace(
        "      <m:oMath>\n",
        "      <w:r><w:t>before</w:t></w:r>\n      <m:oMath>\n",
    ).replace(
        "      </m:oMath>\n    </w:p>",
        "      </m:oMath>\n      <w:r><w:t>after</w:t></w:r>\n    </w:p>",
    )
    path = _write_docx(tmp_path, xml)
    replacement = (
        f'<m:oMath xmlns:m="{_M}">'
        "<m:r><m:t>replacement</m:t></m:r>"
        "</m:oMath>"
    )

    result = docs_intel.edit_equation_local(path, "0000C002", replacement)

    assert "error" not in result
    output = _read_docx_xml(path).decode("utf-8")
    assert output.index("before") < output.index("replacement") < output.index("after")


def test_append_text_run_after_math_adds_run_in_place(tmp_path):
    path = _write_docx(tmp_path, _INLINE_XML)

    result = docs_intel.append_text_run_after_math(path, "0000D001", " after")

    assert result["status"] == "appended"
    output = _read_docx_xml(path).decode("utf-8")
    assert output.index("</m:oMath>") < output.index(" after")
    assert output.index(" after") < output.index("End.")


def test_append_text_run_after_math_rejects_ambiguous_equation(tmp_path):
    xml = _INLINE_XML.replace(
        "</m:oMath>",
        "</m:oMath><m:oMath><m:r><m:t>G</m:t></m:r></m:oMath>",
        1,
    )
    path = _write_docx(tmp_path, xml)

    result = docs_intel.append_text_run_after_math(path, "0000D001", " after")

    assert "error" in result
    assert "math_index is required" in result["error"]
    assert " after" not in _read_docx_xml(path).decode("utf-8")


def test_append_text_run_after_math_selects_by_index(tmp_path):
    xml = _INLINE_XML.replace(
        "</m:oMath>",
        "</m:oMath><m:oMath><m:r><m:t>G</m:t></m:r></m:oMath>",
        1,
    )
    path = _write_docx(tmp_path, xml)

    result = docs_intel.append_text_run_after_math(path, "0000D001", " after", math_index=1)

    assert result["status"] == "appended"
    output = _read_docx_xml(path).decode("utf-8")
    assert output.index(">G<") < output.index(" after")


# ---------------------------------------------------------------------------
# remove_equation_local — display-mode (whole paragraph)
# ---------------------------------------------------------------------------

def test_remove_equation_local_removes_display_paragraph(tmp_path):
    path = _write_docx(tmp_path, _WRITE_XML)
    base_paras = _count_paragraphs(path)
    result = docs_intel.remove_equation_local(path, "0000C002")
    assert "error" not in result
    assert result["status"] == "removed"
    assert result["removed_whole_paragraph"] is True
    assert _count_paragraphs(path) == base_paras - 1
    assert _count_omath(path) == 0


# ---------------------------------------------------------------------------
# remove_equation_local — inline equation
# ---------------------------------------------------------------------------

def test_remove_equation_local_removes_inline_omath_only(tmp_path):
    path = _write_docx(tmp_path, _INLINE_XML)
    base_paras = _count_paragraphs(path)
    result = docs_intel.remove_equation_local(path, "0000D001")
    assert "error" not in result
    assert result["status"] == "removed"
    assert result["removed_whole_paragraph"] is False
    # Paragraph count unchanged — only the oMath was removed.
    assert _count_paragraphs(path) == base_paras
    assert _count_omath(path) == 0
    # The text run "See formula: " must still be present.
    import xml.etree.ElementTree as ET  # noqa: PLC0415
    doc_xml = ET.fromstring(_read_docx_xml(path))
    texts = [t.text for t in doc_xml.iter(f"{{{_W}}}t") if t.text]
    assert any("See formula" in t for t in texts)


# ---------------------------------------------------------------------------
# remove_equation_local — error paths
# ---------------------------------------------------------------------------

def test_remove_equation_local_bad_para_id_does_not_mutate(tmp_path):
    path = _write_docx(tmp_path, _WRITE_XML)
    before = _read_docx_xml(path)
    result = docs_intel.remove_equation_local(path, "no-such-para")
    assert "error" in result
    assert _read_docx_xml(path) == before


def test_remove_equation_local_non_equation_para_returns_error(tmp_path):
    path = _write_docx(tmp_path, _WRITE_XML)
    before = _read_docx_xml(path)
    result = docs_intel.remove_equation_local(path, "0000C001")
    assert "error" in result
    assert _read_docx_xml(path) == before


def test_remove_equation_local_unknown_file_returns_error():
    result = docs_intel.remove_equation_local("/no/such/file.docx", "p0")
    assert "error" in result


# ---------------------------------------------------------------------------
# Sidecar invalidation: index_db_path triggers mtime clear after write
# ---------------------------------------------------------------------------

def test_insert_equation_local_invalidates_sidecar(tmp_path):
    """After insert_equation_local with index_db_path, sidecar mtime is cleared."""
    import sqlite3  # noqa: PLC0415
    path = _write_docx(tmp_path, _WRITE_XML)
    db_path = str(tmp_path / "sidecar.db")

    # Seed the sidecar with a fake mtime so we can detect the clear.
    docs_intel.index_docx(_zip_docx(_WRITE_XML), db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO docx_index_meta (key, value) VALUES (?, ?)",
        ("source_mtime", "999999999.0"),
    )
    conn.commit()
    conn.close()

    docs_intel.insert_equation_local(
        path, "0000C001", _SIMPLE_OMATH, "after", index_db_path=db_path
    )

    conn2 = sqlite3.connect(db_path)
    row = conn2.execute(
        "SELECT value FROM docx_index_meta WHERE key='source_mtime'"
    ).fetchone()
    conn2.close()
    assert row is None or row[0] is None, "sidecar mtime should be cleared after write"


# ---------------------------------------------------------------------------
# Regression: existing caption/citation functions unaffected
# ---------------------------------------------------------------------------

def test_existing_caption_functions_still_work_after_equation_additions(tmp_path):
    """Ensure insert_caption still works — confirm equations didn't break shared helpers."""
    path = _write_docx(tmp_path, _WRITE_XML)
    result = docs_intel.insert_caption(
        docx_path=path,
        anchor_para_id="0000C001",
        kind="Figure",
        label_text="Test figure",
        position="after",
    )
    assert "error" not in result
    assert result["status"] == "inserted"
    assert result["kind"] == "Figure"


# ---------------------------------------------------------------------------
# Configurable style policy (4efc63fd): insert_equation_local /
# insert_caption / insert_highlighted_note consult resolve_style_policy
# instead of hardcoding style choices. Deep policy-validation coverage lives
# in extensions/meridian-docs/tests/test_docx_equation_style_audit.py; this
# section covers the write-path integration on the existing equation fixtures.
# ---------------------------------------------------------------------------

def test_insert_equation_local_default_policy_centers_display_equation(tmp_path):
    """No style_policy passed -> default equation_alignment="center" is still
    applied to a newly inserted display-mode equation paragraph."""
    path = _write_docx(tmp_path, _WRITE_XML)
    result = docs_intel.insert_equation_local(path, "0000C003", _SIMPLE_OMATH, "after")
    assert "error" not in result
    xml = _read_docx_xml(path).decode("utf-8")
    assert 'w:val="center"' in xml


def test_insert_equation_local_append_mode_ignores_style_policy(tmp_path):
    """position="append" has no paragraph of its own, so no pPr/jc/ind is added."""
    path = _write_docx(tmp_path, _WRITE_XML)
    before_paras = _count_paragraphs(path)
    result = docs_intel.insert_equation_local(
        path, "0000C003", _SIMPLE_OMATH, "append",
        style_policy={"equation_alignment": "right", "body_indent_twips": 500},
    )
    assert "error" not in result
    assert _count_paragraphs(path) == before_paras


def test_insert_equation_local_custom_style_policy_alignment_and_indent(tmp_path):
    path = _write_docx(tmp_path, _WRITE_XML)
    result = docs_intel.insert_equation_local(
        path, "0000C003", _SIMPLE_OMATH, "before",
        style_policy={"equation_alignment": "right", "body_indent_twips": 240},
    )
    assert "error" not in result
    xml = _read_docx_xml(path).decode("utf-8")
    assert 'w:val="right"' in xml
    assert 'w:left="240"' in xml


def test_insert_equation_local_invalid_style_policy_errors_without_mutation(tmp_path):
    path = _write_docx(tmp_path, _WRITE_XML)
    before = _read_docx_xml(path)
    result = docs_intel.insert_equation_local(
        path, "0000C003", _SIMPLE_OMATH, "after",
        style_policy={"equation_alignment": "sideways"},
    )
    assert "error" in result
    assert _read_docx_xml(path) == before


def test_insert_caption_default_policy_not_centered(tmp_path):
    """Default caption_centered=False preserves pre-4efc63fd output exactly:
    no w:jc on the caption paragraph."""
    path = _write_docx(tmp_path, _WRITE_XML)
    result = docs_intel.insert_caption(
        docx_path=path, anchor_para_id="0000C001", kind="Figure",
        label_text="Uncentered figure", position="after",
    )
    assert "error" not in result
    xml = _read_docx_xml(path).decode("utf-8")
    assert "w:jc" not in xml  # _WRITE_XML has no pre-existing alignment either


def test_insert_caption_style_policy_centers_caption(tmp_path):
    path = _write_docx(tmp_path, _WRITE_XML)
    result = docs_intel.insert_caption(
        docx_path=path, anchor_para_id="0000C001", kind="Figure",
        label_text="Centered figure", position="after",
        style_policy={"caption_centered": True},
    )
    assert "error" not in result
    xml = _read_docx_xml(path).decode("utf-8")
    assert 'w:val="center"' in xml


def test_insert_caption_invalid_style_policy_errors_without_mutation(tmp_path):
    path = _write_docx(tmp_path, _WRITE_XML)
    before = _read_docx_xml(path)
    result = docs_intel.insert_caption(
        docx_path=path, anchor_para_id="0000C001", kind="Figure",
        label_text="Bad policy", position="after",
        style_policy={"caption_centered": "yes"},
    )
    assert "error" in result
    assert _read_docx_xml(path) == before


def test_insert_highlighted_note_default_policy_preserves_original_style(tmp_path):
    path = _write_docx(tmp_path, _WRITE_XML)
    result = docs_intel.insert_highlighted_note(
        path, "Check this later.", "0000C001",
    )
    assert "error" not in result
    assert result["style"] == "MeridianInternalNote"
    xml = _read_docx_xml(path).decode("utf-8")
    assert "MeridianInternalNote" in xml
    assert 'w:val="yellow"' in xml


def test_insert_highlighted_note_custom_style_policy(tmp_path):
    path = _write_docx(tmp_path, _WRITE_XML)
    result = docs_intel.insert_highlighted_note(
        path, "Check this later.", "0000C001",
        style_policy={"note_style": "MeridianReviewNote", "note_highlight_color": "cyan"},
    )
    assert "error" not in result
    assert result["style"] == "MeridianReviewNote"
    xml = _read_docx_xml(path).decode("utf-8")
    assert "MeridianReviewNote" in xml
    assert 'w:val="cyan"' in xml


def test_insert_highlighted_note_invalid_style_policy_errors_without_mutation(tmp_path):
    path = _write_docx(tmp_path, _WRITE_XML)
    before = _read_docx_xml(path)
    result = docs_intel.insert_highlighted_note(
        path, "Check this later.", "0000C001",
        style_policy={"note_highlight_color": "not-a-real-color"},
    )
    assert "error" in result
    assert _read_docx_xml(path) == before


# ---------------------------------------------------------------------------
# audit_equation_style smoke coverage (full matrix lives in
# extensions/meridian-docs/tests/test_docx_equation_style_audit.py)
# ---------------------------------------------------------------------------

def test_audit_equation_style_smoke_on_standalone_fixture(tmp_path):
    path = _write_docx(tmp_path, _STANDALONE_XML)
    result = docs_intel.audit_equation_style(path)
    assert "error" not in result
    assert result["equation_count"] == 2
    assert isinstance(result["findings"], list)
    assert result["finding_count"] == len(result["findings"])


def test_audit_equation_style_smoke_on_table_numbered_fixture(tmp_path):
    path = _write_docx(tmp_path, _TABLE_NUMBERED_XML)
    result = docs_intel.audit_equation_style(path)
    assert "error" not in result
    assert result["equation_count"] == 3
    # No duplicate/gap issues in this fixture: numbers are (1) and (2a).
    types = {f["type"] for f in result["findings"]}
    assert "duplicate_equation_number" not in types
    assert "equation_number_gap" not in types


def test_audit_equation_style_unknown_file_returns_error():
    result = docs_intel.audit_equation_style("/no/such/file.docx")
    assert "error" in result


# ---------------------------------------------------------------------------
# Real render-gate CI evidence for insert_equation_local (W2-D, 9a817fce).
#
# Every test above this point runs under this module's autouse
# ``_default_render_capability`` fixture, which stubs check_render_capability
# so structural correctness can be tested independently of whatever render
# backends happen to be installed. That is deliberate for THIS module (per
# its own docstring), but it also means none of it is real evidence that the
# actual _word_com_render / _soffice_render backends behave correctly --
# extensions/meridian-docs/tests/test_docx_word_com_regression.py owns that
# in depth, but that suite lived entirely outside CI until this item wired
# it into a new .github/workflows/test.yml job (extensions/meridian-docs is
# not a pixi.toml dependency, so it never ran in `pixi run test` before).
#
# This one test un-stubs the autouse fixture and drives the REAL
# render_gate.check_render_capability end to end, right inside the SAME
# `tests/` suite pixi.toml/test-core has always run -- so this file alone
# (regardless of whether the new extensions/meridian-docs CI job is ever
# skipped or misconfigured) is independent, always-on CI evidence that
# insert_equation_local's render-gate integration still works for real: it
# is not skipped when no backend is installed (this test-core runner has
# neither LibreOffice nor Word/COM by default) -- it falls back to the same
# "structural validation" the item title calls for, verified independently
# here rather than trusting docs_intel's own internal verification helpers.
# ---------------------------------------------------------------------------


def test_insert_equation_local_real_render_gate_or_structural_fallback(tmp_path, monkeypatch):
    # Undo this module's autouse stub for this one test only -- restores the
    # genuine render_gate.check_render_capability implementation captured at
    # import time (see _REAL_CHECK_RENDER_CAPABILITY above).
    monkeypatch.setattr(
        docs_intel.render_gate, "check_render_capability", _REAL_CHECK_RENDER_CAPABILITY,
    )

    path = _write_docx(tmp_path, _WRITE_XML)
    before = _read_docx_xml(path)
    status = docs_intel.render_gate.check_render_capability(path)["status"]

    if status == docs_intel.render_gate.RENDERED:
        result = docs_intel.insert_equation_local(path, "0000C003", _SIMPLE_OMATH, "append")
        assert result["status"] == "inserted"
        assert result["render_status"] == docs_intel.render_gate.RENDERED
        assert result["render_verified"] is True
    elif status == docs_intel.render_gate.UNAVAILABLE_WITH_REASON:
        result = docs_intel.insert_equation_local(
            path, "0000C003", _SIMPLE_OMATH, "append",
            allow_degraded_render=True,
            degraded_render_reason="no render backend available in this CI/dev environment",
        )
        assert result["status"] == "inserted"
        assert result["render_status"] == docs_intel.render_gate.UNAVAILABLE_WITH_REASON
        assert result["render_verified"] is False
        assert result["render_degraded"] is True
        # Structural validation fallback: an independent re-parse (not the
        # writer's own internal verification helper) confirms the equation
        # genuinely landed in a well-formed document. _WRITE_XML starts with
        # exactly one oMath (paragraph 0000C002); this appends a second into
        # 0000C003.
        assert _count_omath(path) == 2
        with open(path, "rb") as handle:
            paragraphs = docs_intel.parse_docx(handle.read())
        assert paragraphs
    else:
        assert status == docs_intel.render_gate.FAILED
        result = docs_intel.insert_equation_local(path, "0000C003", _SIMPLE_OMATH, "append")
        assert "error" in result
        assert result["render_status"] == docs_intel.render_gate.FAILED
        assert result["file_restored"] is True
        assert _read_docx_xml(path) == before
