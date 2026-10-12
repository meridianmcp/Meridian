"""Regression tests for sprint item 29fe401f (OMML builder: Word-valid cases/functions).

The LaTeX -> OMML converter used to emit two shapes Word does not accept:

* ``m:eqArr`` rows nested each cell as an ``m:e`` inside the row ``m:e``.
  ``m:e`` is an argument container, not a math element, so an ``m:e`` that is
  a direct child of another ``m:e`` is schema-invalid.  A row is now one
  ``m:e`` whose cells are separated by an ``&`` alignment point.
* ``m:func`` and ``m:eqArr`` carried no control properties.  Word writes
  ``m:funcPr`` / ``m:eqArrPr`` holding an ``m:ctrlPr`` (Cambria Math run
  formatting), and writes function names upright (``m:sty m:val="p"``).
"""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
import zipfile

import pytest

# Make meridian_docs importable from the local extensions directory.
_EXT_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "extensions", "meridian-docs")
)
if _EXT_PATH not in sys.path:
    sys.path.insert(0, _EXT_PATH)

from meridian_docs import docs_intel  # noqa: E402

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"


def _qm(tag: str) -> str:
    return f"{{{_M}}}{tag}"


def _qw(tag: str) -> str:
    return f"{{{_W}}}{tag}"


_CASES_LATEX = r"\begin{cases} x & x>0 \\ -x & x \le 0 \end{cases}"
_NESTED_FRACTION_CASES_LATEX = (
    r"\begin{cases} \frac{1}{x} & x \ne 0 \\ 0 & x = 0 \end{cases}"
)
_MAX_FUNC_LATEX = r"\max(a,b)"
_MIN_SUBSCRIPT_LATEX = r"\min_{x} f(x)"
_CONVERTED_LATEX = [
    _CASES_LATEX,
    _NESTED_FRACTION_CASES_LATEX,
    _MAX_FUNC_LATEX,
    _MIN_SUBSCRIPT_LATEX,
]

_DOCUMENT_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:m="{_M}">
  <w:body>
    <w:p w14:paraId="P0000001"><w:r><w:t>Anchor one.</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>
'''


def _convert(latex: str) -> ET.Element:
    omml = docs_intel.latex_to_omml_local(latex)
    assert omml is not None, f"conversion failed for {latex!r}"
    return docs_intel._validate_omml_structure(omml)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class TestEqArrRowsAreSchemaValid:
    def test_cases_rows_are_single_m_e_with_one_alignment_point(self):
        root = _convert(_CASES_LATEX)
        eqarr = root.find(_qm("eqArr"))
        assert eqarr is not None
        rows = eqarr.findall(_qm("e"))
        assert len(rows) == 2, f"expected 2 case branches, got {len(rows)}"
        for row in rows:
            assert all(child.tag != _qm("e") for child in row), (
                "a cell must not be nested as an m:e inside the row m:e"
            )
            alignment = [
                child for child in row
                if child.tag == _qm("r")
                and "".join(t.text or "" for t in child.iter(_qm("t"))) == "&"
            ]
            assert len(alignment) == 1, "cells must be separated by one & alignment point"

    @pytest.mark.parametrize("latex", _CONVERTED_LATEX)
    def test_no_m_e_is_ever_a_direct_child_of_m_e(self, latex):
        root = _convert(latex)
        for element in root.iter(_qm("e")):
            nested = [child for child in element if child.tag == _qm("e")]
            assert not nested, f"m:e nested directly inside m:e for {latex!r}"

    def test_eqarr_children_follow_schema_order(self):
        root = _convert(_CASES_LATEX)
        eqarr = root.find(_qm("eqArr"))
        assert [_local(child.tag) for child in eqarr] == ["eqArrPr", "e", "e"]


class TestControlProperties:
    def test_func_node_leads_with_funcpr_carrying_ctrlpr(self):
        root = _convert(_MAX_FUNC_LATEX)
        func = root.find(_qm("func"))
        assert func is not None
        assert [_local(child.tag) for child in func][:3] == ["funcPr", "fName", "e"]
        rfonts = func.find(
            f"{_qm('funcPr')}/{_qm('ctrlPr')}/{_qw('rPr')}/{_qw('rFonts')}"
        )
        assert rfonts is not None, "funcPr must carry a ctrlPr with w:rPr run formatting"
        assert rfonts.get(_qw("ascii")) == "Cambria Math"
        assert rfonts.get(_qw("hAnsi")) == "Cambria Math"

    def test_func_name_run_is_upright(self):
        root = _convert(_MAX_FUNC_LATEX)
        sty = root.find(f"{_qm('func')}/{_qm('fName')}/{_qm('r')}/{_qm('rPr')}/{_qm('sty')}")
        assert sty is not None
        assert sty.get(_qm("val")) == "p"

    def test_eqarr_leads_with_eqarrpr_carrying_ctrlpr(self):
        root = _convert(_CASES_LATEX)
        ctrl = root.find(f"{_qm('eqArr')}/{_qm('eqArrPr')}/{_qm('ctrlPr')}")
        assert ctrl is not None, "eqArr must carry an eqArrPr/ctrlPr"
        assert ctrl.find(_qw("rPr")) is not None

    def test_serialised_control_properties_use_word_prefixes(self):
        omml = docs_intel.latex_to_omml_local(_CASES_LATEX)
        assert "ns0:" not in omml
        assert "<w:rPr" in omml


class TestWordValidCasesWriteRoundTrip:
    def test_inserted_cases_reparse_with_eqarrpr_and_flat_rows(self, tmp_path, monkeypatch):
        path = str(tmp_path / "cases.docx")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", _DOCUMENT_XML)
        monkeypatch.setattr(
            docs_intel.render_gate, "check_render_capability",
            lambda p, **kw: {"status": "rendered", "backend": "test-stub", "detail": {}},
        )
        omml = docs_intel.latex_to_omml_local(_CASES_LATEX)

        result = docs_intel.insert_equation_local(path, "P0000001", omml, "after")
        assert result["status"] == "inserted"

        equations = docs_intel.parse_docx_equations_local(path)
        assert len(equations) == 1
        reparsed = ET.fromstring(equations[0]["omml_raw"])
        eqarr = reparsed.find(_qm("eqArr"))
        assert eqarr is not None
        assert eqarr.find(_qm("eqArrPr")) is not None
        rows = eqarr.findall(_qm("e"))
        assert len(rows) == 2
        assert all(child.tag != _qm("e") for row in rows for child in row)
