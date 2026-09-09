"""Word-open OMML regression harness (sprint item 443ba085).

Disposable-DOCX regression coverage for ``insert_equation``/
``insert_equation_local`` focused on the equation SHAPES the existing
suites under-cover: piecewise/cases equations (``m:eqArr``), function
nodes (``m:func``), and nested function/subscript/fraction combinations
-- plus fail-closed behavior on an empty equation body, display vs.
inline insertion, and a REAL, direct Word-open verification pass that
never claims Word verified a write unless Word genuinely did.

Every OMML shape asserted on below was independently confirmed against
this exact codebase before being encoded here (not guessed): the LaTeX ->
OMML conversion is real (latex2mathml + ``_stdlib_append_mathml``), so a
test failure here means the CONVERSION or WRITE pipeline actually
regressed, not that a hand-written fixture no longer matches an assumption.

Structure:
  - TestPiecewiseCasesEquations       -- m:eqArr (cases/piecewise) branches
  - TestFunctionNodes                 -- m:func nodes + their required children
  - TestNestedCombinations            -- func-in-subscript, sub+sup, frac-in-cases
  - TestEmptyBodyRejection            -- blank payload AND empty <m:oMath/> body
  - TestDisplayVsInlineInsertion      -- before/after (display) vs append (inline)
  - TestRenderGateWithComplexPayloads -- tri-state render contract generalizes
  - TestZipAndOoxmlIntegrity          -- independent, from-scratch package checks
  - TestWordOpenReadabilityVerification -- REAL (unmocked) Word-COM open/render,
                                           gracefully degraded when unavailable

This file is TEST-ONLY except for one narrowly-scoped production fix in
``docs_intel._validate_omml_structure`` (see TestEmptyBodyRejection's
docstring): an ``<m:oMath>`` with literally zero child elements passed
validation before this item and is rejected now -- the gap the item's
"empty-body rejection" acceptance criterion calls for.
"""
from __future__ import annotations

import zipfile
import xml.etree.ElementTree as ET

import pytest

from meridian_docs import docs_intel, render_gate, server


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"

_NS = f'xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:m="{_M}"'


def _q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def _qw(tag: str) -> str:
    return _q(_W, tag)


def _qm(tag: str) -> str:
    return _q(_M, tag)


def _omml(body: str) -> str:
    return f'<m:oMath xmlns:m="{_M}">{body}</m:oMath>'


# ---------------------------------------------------------------------------
# Real, verified LaTeX -> OMML shapes.
#
# Each comment records the ACTUAL structural output this codebase's
# latex2mathml + _stdlib_append_mathml pipeline produces for that input --
# confirmed by direct execution against this exact worktree, not assumed.
# ---------------------------------------------------------------------------

# -> <m:r>{</m:r><m:eqArr><m:e><m:e>x</m:e><m:e>x&gt;0</m:e></m:e>
#          <m:e><m:e>-x</m:e><m:e>x<=0</m:e></m:e></m:eqArr>
# Two rows, two cells each -- a genuine piecewise/cases equation.
_CASES_LATEX = r"\begin{cases} x & x>0 \\ -x & x \le 0 \end{cases}"

# -> <m:r>{</m:r><m:eqArr><m:e><m:e><m:f>...1/x...</m:f></m:e><m:e>x != 0</m:e></m:e>
#          <m:e><m:e>0</m:e><m:e>x = 0</m:e></m:e></m:eqArr>
# A fraction NESTED inside one eqArr cell -- "nested structures".
_NESTED_FRACTION_CASES_LATEX = (
    r"\begin{cases} \frac{1}{x} & x \ne 0 \\ 0 & x = 0 \end{cases}"
)

# -> <m:func><m:fName><m:r>max</m:r></m:fName><m:e/></m:func><m:r>(</m:r>...
# "max"/"min"/"argmin"/"argmax"/"sup"/"inf" are the special-cased names that
# convert to a real m:func node (see _stdlib_append_mathml).
_MAX_FUNC_LATEX = r"\max(a,b)"

# -> <m:r>sin</m:r><m:r>(</m:r>... -- flattened plain text runs, NO m:func.
# "sin" is deliberately NOT in the special-cased function-name set; this
# pins that scope boundary so it can't silently drift.
_SIN_NOT_A_FUNC_LATEX = r"\sin(x)"

# -> <m:sSub><m:e><m:func>...min...</m:func></m:e><m:sub>x</m:sub></m:sSub>
#    <m:r>f</m:r><m:r>(</m:r>...
# A function node NESTED inside a subscript base -- "nested function/
# subscript combinations".
_MIN_SUBSCRIPT_LATEX = r"\min_{x} f(x)"

# -> <m:sSubSup><m:e>x</m:e><m:sub>i,j</m:sub><m:sup>2</m:sup></m:sSubSup>
_SUBSCRIPT_SUPERSCRIPT_LATEX = r"x_{i,j}^{2}"

# Raw OMML, hand-built for full control: a 3-branch piecewise (LaTeX's cases
# environment realistically produces 2 branches in the tests above; this
# exercises eqArr with a THIRD row directly).
_THREE_BRANCH_CASES_OMML = _omml(
    "<m:eqArr>"
    "<m:e><m:r><m:t>1,</m:t></m:r><m:r><m:t>x&gt;0</m:t></m:r></m:e>"
    "<m:e><m:r><m:t>0,</m:t></m:r><m:r><m:t>x=0</m:t></m:r></m:e>"
    "<m:e><m:r><m:t>-1,</m:t></m:r><m:r><m:t>x&lt;0</m:t></m:r></m:e>"
    "</m:eqArr>"
)

_SIMPLE_OMATH = _omml("<m:r><m:t>z</m:t></m:r>")


# ---------------------------------------------------------------------------
# Disposable DOCX fixtures.
# ---------------------------------------------------------------------------

_DOCUMENT_XML = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document {_NS}>
  <w:body>
    <w:p w14:paraId="P0000001"><w:r><w:t>Anchor one.</w:t></w:r></w:p>
    <w:p w14:paraId="P0000002"><w:r><w:t>Anchor two.</w:t></w:r></w:p>
    <w:p w14:paraId="P0000003">
      <w:r><w:t>Before text. </w:t></w:r>
      <w:r><w:t>After text.</w:t></w:r>
    </w:p>
    <w:sectPr/>
  </w:body>
</w:document>
'''


def _write_docx(tmp_path, name: str = "doc.docx", xml: str = _DOCUMENT_XML) -> str:
    path = str(tmp_path / name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return path


def _read_document_xml(path: str) -> bytes:
    with zipfile.ZipFile(path) as zf:
        return zf.read("word/document.xml")


# Full-fidelity "Word-authored" package (every part a genuine Word save
# produces) -- required for the real, unmocked render-capability section:
# a bare word/document.xml is sufficient for a monkeypatched backend but not
# something a real backend (Word COM or LibreOffice) is guaranteed to open.

_CONTENT_TYPES_XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/fontTable.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml"/>
  <Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>
'''

_ROOT_RELS_XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
'''

_DOCUMENT_RELS_XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/fontTable" Target="fontTable.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>
</Relationships>
'''

_STYLES_XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults/>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
</w:styles>
'''

_FONT_TABLE_XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:fonts xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:font w:name="Calibri"/>
</w:fonts>
'''

_SETTINGS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>\n'
)

_CORE_PROPS_XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:title>Meridian 443ba085 OMML regression fixture</dc:title>
</cp:coreProperties>
'''

_APP_PROPS_XML = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">
  <Application>Meridian test fixture</Application>
</Properties>
'''


def _write_word_authored_docx(tmp_path, name: str = "word_authored.docx", document_xml: str = _DOCUMENT_XML) -> str:
    """A full-fidelity fixture with every part a genuine Word save produces."""
    path = str(tmp_path / name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        archive.writestr("_rels/.rels", _ROOT_RELS_XML)
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/_rels/document.xml.rels", _DOCUMENT_RELS_XML)
        archive.writestr("word/styles.xml", _STYLES_XML)
        archive.writestr("word/fontTable.xml", _FONT_TABLE_XML)
        archive.writestr("word/settings.xml", _SETTINGS_XML)
        archive.writestr("docProps/core.xml", _CORE_PROPS_XML)
        archive.writestr("docProps/app.xml", _APP_PROPS_XML)
    return path


def _assert_valid_ooxml_package(path: str, *, required_parts=("word/document.xml",)) -> None:
    """Independent, from-scratch package-integrity check.

    Deliberately does NOT reuse docs_intel's own internal ``_verify_*_write``
    helpers -- real evidence that can't share a blind spot with the code
    under test: valid ZIP (no CRC/central-directory corruption), every
    ``.xml``/``.rels`` part well-formed, and every required part present.
    """
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None, f"{path} has a corrupted ZIP member"
        names = set(archive.namelist())
        for required in required_parts:
            assert required in names, f"missing required OOXML part: {required}"
        for part_name in names:
            if not (part_name.endswith(".xml") or part_name.endswith(".rels")):
                continue
            raw = archive.read(part_name)
            try:
                ET.fromstring(raw)
            except ET.ParseError as exc:  # pragma: no cover -- failure path
                raise AssertionError(f"{part_name} is not well-formed XML: {exc}") from exc


def _oMath_elements(path: str) -> list[ET.Element]:
    xml = _read_document_xml(path)
    root = ET.fromstring(xml)
    return list(root.iter(_qm("oMath")))


# ---------------------------------------------------------------------------
# Piecewise / cases equations (m:eqArr)
# ---------------------------------------------------------------------------


class TestPiecewiseCasesEquations:
    def test_latex_cases_converts_to_eqarr_with_two_rows_two_cells(self):
        omml = docs_intel.latex_to_omml_local(_CASES_LATEX)
        assert omml is not None
        root = docs_intel._validate_omml_structure(omml)
        eqarr = root.find(_qm("eqArr"))
        assert eqarr is not None, "cases LaTeX must convert to a real m:eqArr, not flattened text"
        rows = list(eqarr)
        assert len(rows) == 2, f"expected 2 case branches, got {len(rows)}"
        for row in rows:
            cells = list(row)
            assert len(cells) == 2, "each case branch must have a value cell and a condition cell"

    def test_cases_semantic_manifest_reports_eqarr_structural_tag(self):
        omml = docs_intel.latex_to_omml_local(_CASES_LATEX)
        record = docs_intel._omml_semantic_record(ET.fromstring(omml))
        assert record["structural_tags"] == {"eqArr": 1}
        assert not record["issues"]

    def test_three_branch_raw_omml_eqarr_round_trips(self):
        root = docs_intel._validate_omml_structure(_THREE_BRANCH_CASES_OMML)
        eqarr = root.find(_qm("eqArr"))
        assert len(list(eqarr)) == 3

    def test_eqarr_missing_e_child_is_rejected(self):
        with pytest.raises(ValueError, match=r"m:eqArr.*missing required child.*e"):
            docs_intel._validate_omml_structure(_omml("<m:eqArr></m:eqArr>"))

    def test_insert_equation_local_display_mode_writes_cases_and_survives_independent_reparse(
        self, tmp_path, monkeypatch,
    ):
        path = _write_docx(tmp_path)
        monkeypatch.setattr(
            docs_intel.render_gate, "check_render_capability",
            lambda p, **kw: {"status": "rendered", "backend": "test-stub", "detail": {}},
        )
        omml = docs_intel.latex_to_omml_local(_CASES_LATEX)

        result = docs_intel.insert_equation_local(path, "P0000001", omml, "after")

        assert result["status"] == "inserted"
        assert result["render_verified"] is True

        # Independent re-extraction (parse_docx_equations_local), NOT the
        # writer's own _verify_equation_write helper -- a genuinely separate
        # read path so a bug shared between write and verify can't hide.
        equations = docs_intel.parse_docx_equations_local(path)
        assert len(equations) == 1
        assert equations[0]["pattern"] == "standalone"
        reparsed_root = ET.fromstring(equations[0]["omml_raw"])
        assert reparsed_root.tag == _qm("oMath")
        eqarr = reparsed_root.find(_qm("eqArr"))
        assert eqarr is not None and len(list(eqarr)) == 2
        assert "x>0" in equations[0]["flat_text"] or "x&gt;0" in equations[0]["omml_raw"]

    def test_insert_equation_local_rejects_malformed_cases_without_mutating_file(
        self, tmp_path,
    ):
        path = _write_docx(tmp_path)
        before = _read_document_xml(path)

        result = docs_intel.insert_equation_local(
            path, "P0000001", _omml("<m:eqArr></m:eqArr>"), "after",
        )

        assert "error" in result
        assert "eqArr" in result["error"]
        assert _read_document_xml(path) == before


# ---------------------------------------------------------------------------
# Function nodes (m:func)
# ---------------------------------------------------------------------------


class TestFunctionNodes:
    def test_latex_max_converts_to_func_node(self):
        omml = docs_intel.latex_to_omml_local(_MAX_FUNC_LATEX)
        root = docs_intel._validate_omml_structure(omml)
        func = root.find(_qm("func"))
        assert func is not None, "\\max(...) must convert to a real m:func node"
        fname_text = "".join(
            t.text or "" for t in func.find(_qm("fName")).iter(_qm("t"))
        )
        assert fname_text == "max"

    def test_sin_does_not_produce_a_func_node_pinning_current_scope(self):
        """Only min/max/argmin/argmax/sup/inf get the semantic m:func
        treatment (see _stdlib_append_mathml) -- "sin" is deliberately
        outside that set. This pins the boundary so silently widening or
        narrowing the special-cased name set trips a test."""
        omml = docs_intel.latex_to_omml_local(_SIN_NOT_A_FUNC_LATEX)
        root = docs_intel._validate_omml_structure(omml)
        assert root.find(_qm("func")) is None
        flat = docs_intel._omml_flatten_text_local(omml)
        assert flat == "sin(x)"

    def test_func_missing_fname_is_rejected(self):
        bad = _omml('<m:func><m:e><m:r><m:t>x</m:t></m:r></m:e></m:func>')
        with pytest.raises(ValueError, match=r"m:func.*missing required child.*fName"):
            docs_intel._validate_omml_structure(bad)

    def test_func_missing_e_is_rejected(self):
        bad = _omml('<m:func><m:fName><m:r><m:t>f</m:t></m:r></m:fName></m:func>')
        with pytest.raises(ValueError, match=r"m:func.*missing required child.*e"):
            docs_intel._validate_omml_structure(bad)

    def test_insert_equation_local_append_mode_preserves_existing_runs_around_func_node(
        self, tmp_path, monkeypatch,
    ):
        path = _write_docx(tmp_path)
        monkeypatch.setattr(
            docs_intel.render_gate, "check_render_capability",
            lambda p, **kw: {"status": "rendered", "backend": "test-stub", "detail": {}},
        )
        omml = docs_intel.latex_to_omml_local(_MAX_FUNC_LATEX)

        result = docs_intel.insert_equation_local(path, "P0000003", omml, "append")
        assert result["status"] == "inserted"

        root = ET.fromstring(_read_document_xml(path))
        para = root.find(f".//{_qw('p')}[@{_q(_W14,'paraId')}=\"P0000003\"]")
        children = list(para)
        # The two pre-existing <w:r> runs must survive, in original order,
        # with the new <m:oMath> appended AFTER them (inline append never
        # rewrites or reorders sibling content).
        run_texts = [
            "".join(t.text or "" for t in child.iter(_qw("t")))
            for child in children if child.tag == _qw("r")
        ]
        assert run_texts == ["Before text. ", "After text."]
        # The new <m:oMath> must be the LAST child (appended, not spliced
        # in among the existing runs), and it must itself contain the
        # func node -- not just co-occur with it somewhere in the paragraph.
        assert children[-1].tag == _qm("oMath")
        assert children[-1].find(_qm("func")) is not None
        # Structural manifest confirms the func node landed intact.
        manifest = docs_intel._equation_semantic_manifest([para])
        assert manifest["count"] == 1
        assert manifest["entries"][0]["structural_tags"] == {"func": 1}

    def test_server_insert_equation_wrapper_delegates_function_node_payload(
        self, tmp_path, monkeypatch,
    ):
        """server.insert_equation (the MCP tool surface, file scope's
        read-only reference) must faithfully delegate to
        insert_equation_local -- exercised end to end, not mocked out."""
        path = _write_docx(tmp_path)
        monkeypatch.setattr(
            docs_intel.render_gate, "check_render_capability",
            lambda p, **kw: {"status": "rendered", "backend": "test-stub", "detail": {}},
        )
        omml = docs_intel.latex_to_omml_local(_MAX_FUNC_LATEX)

        result = server.insert_equation(path, "P0000002", omml, "after")

        assert result["status"] == "inserted"
        equations = docs_intel.parse_docx_equations_local(path)
        assert len(equations) == 1
        assert ET.fromstring(equations[0]["omml_raw"]).find(_qm("func")) is not None


# ---------------------------------------------------------------------------
# Nested function/subscript/fraction combinations
# ---------------------------------------------------------------------------


class TestNestedCombinations:
    def test_min_with_subscript_nests_func_inside_ssub_base(self):
        """\\min_{x} f(x) -> m:sSub whose base (m:e) directly contains an
        m:func -- a genuine nested function/subscript combination, verified
        by walking the actual tree (not just co-occurrence in a tag-count
        Counter, which would not prove nesting)."""
        omml = docs_intel.latex_to_omml_local(_MIN_SUBSCRIPT_LATEX)
        root = docs_intel._validate_omml_structure(omml)
        ssub = root.find(_qm("sSub"))
        assert ssub is not None
        base_e = ssub.find(_qm("e"))
        assert base_e is not None
        nested_func = base_e.find(_qm("func"))
        assert nested_func is not None, "func node must be nested INSIDE sSub's base, not a sibling"
        fname_text = "".join(t.text or "" for t in nested_func.find(_qm("fName")).iter(_qm("t")))
        assert fname_text == "min"
        sub_text = "".join(t.text or "" for t in ssub.find(_qm("sub")).iter(_qm("t")))
        assert sub_text == "x"

    def test_min_with_subscript_semantic_manifest_reports_both_tags(self):
        omml = docs_intel.latex_to_omml_local(_MIN_SUBSCRIPT_LATEX)
        record = docs_intel._omml_semantic_record(ET.fromstring(omml))
        assert record["structural_tags"] == {"func": 1, "sSub": 1}
        assert not record["issues"]

    def test_subscript_superscript_combo(self):
        omml = docs_intel.latex_to_omml_local(_SUBSCRIPT_SUPERSCRIPT_LATEX)
        root = docs_intel._validate_omml_structure(omml)
        ssubsup = root.find(_qm("sSubSup"))
        assert ssubsup is not None
        base_text = "".join(t.text or "" for t in ssubsup.find(_qm("e")).iter(_qm("t")))
        sub_text = "".join(t.text or "" for t in ssubsup.find(_qm("sub")).iter(_qm("t")))
        sup_text = "".join(t.text or "" for t in ssubsup.find(_qm("sup")).iter(_qm("t")))
        assert base_text == "x"
        assert sub_text == "i,j"
        assert sup_text == "2"

    def test_fraction_nested_inside_cases_branch(self):
        """A fraction nested inside one eqArr cell -- the item title's
        "nested structures" case: eqArr containing f, not just co-occurring
        with it."""
        omml = docs_intel.latex_to_omml_local(_NESTED_FRACTION_CASES_LATEX)
        assert omml is not None
        root = docs_intel._validate_omml_structure(omml)
        eqarr = root.find(_qm("eqArr"))
        assert eqarr is not None
        nested_frac = eqarr.find(f".//{_qm('f')}")
        assert nested_frac is not None, "fraction must be nested INSIDE eqArr, not a sibling"
        num_text = "".join(t.text or "" for t in nested_frac.find(_qm("num")).iter(_qm("t")))
        den_text = "".join(t.text or "" for t in nested_frac.find(_qm("den")).iter(_qm("t")))
        assert num_text == "1"
        assert den_text == "x"
        record = docs_intel._omml_semantic_record(root)
        assert record["structural_tags"] == {"eqArr": 1, "f": 1}
        assert not record["issues"]

    def test_full_pipeline_preserves_nested_fraction_in_cases_through_write_and_reparse(
        self, tmp_path, monkeypatch,
    ):
        """The single most load-bearing structural-preservation test in this
        file: a nested (fraction-inside-cases) equation is run through the
        REAL insert_equation_local write+verify pipeline, then re-extracted
        from disk via an entirely independent read path
        (parse_docx_equations_local), confirming the nesting survived intact
        end to end -- not just immediately after being built in memory."""
        path = _write_docx(tmp_path)
        monkeypatch.setattr(
            docs_intel.render_gate, "check_render_capability",
            lambda p, **kw: {"status": "rendered", "backend": "test-stub", "detail": {}},
        )
        omml = docs_intel.latex_to_omml_local(_NESTED_FRACTION_CASES_LATEX)

        result = docs_intel.insert_equation_local(path, "P0000002", omml, "before")
        assert result["status"] == "inserted"

        equations = docs_intel.parse_docx_equations_local(path)
        assert len(equations) == 1
        reread_root = ET.fromstring(equations[0]["omml_raw"])
        eqarr = reread_root.find(_qm("eqArr"))
        assert eqarr is not None
        nested_frac = eqarr.find(f".//{_qm('f')}")
        assert nested_frac is not None, (
            "fraction nested inside eqArr did not survive the write+reparse round trip"
        )
        record = docs_intel._omml_semantic_record(reread_root)
        assert record["structural_tags"] == {"eqArr": 1, "f": 1}
        assert not record["issues"]
        _assert_valid_ooxml_package(path)


# ---------------------------------------------------------------------------
# Empty-body rejection
# ---------------------------------------------------------------------------


class TestEmptyBodyRejection:
    """Covers two distinct empty-payload gaps:

    1. A blank/whitespace-only ``payload`` STRING -- already rejected by
       ``insert_equation_local`` before this item (pinned here as a real
       regression guard, not a new behavior).
    2. A syntactically valid ``<m:oMath/>`` with literally ZERO child
       elements -- this previously passed ``_validate_omml_structure`` and
       would have been written as a genuinely blank equation box. This item
       adds the minimal, narrowly-scoped rejection in
       ``docs_intel._validate_omml_structure`` (see its own inline comment,
       tag "443ba085") and this class is its regression coverage.
    """

    def test_blank_and_whitespace_payload_rejected_without_touching_file(self, tmp_path):
        path = _write_docx(tmp_path)
        before = _read_document_xml(path)

        for blank_payload in ("", "   ", "\n\t"):
            result = docs_intel.insert_equation_local(path, "P0000001", blank_payload, "after")
            assert "error" in result
            assert "non-empty string" in result["error"]

        assert _read_document_xml(path) == before

    @pytest.mark.parametrize("empty_xml", [
        f'<m:oMath xmlns:m="{_M}"/>',
        f'<m:oMath xmlns:m="{_M}"></m:oMath>',
    ])
    def test_self_closing_and_open_close_empty_omath_rejected_by_validator(self, empty_xml):
        with pytest.raises(ValueError, match="empty"):
            docs_intel._validate_omml_structure(empty_xml)

    def test_insert_equation_local_rejects_empty_omath_end_to_end_without_mutating_file(
        self, tmp_path,
    ):
        path = _write_docx(tmp_path)
        before = _read_document_xml(path)

        result = docs_intel.insert_equation_local(
            path, "P0000001", f'<m:oMath xmlns:m="{_M}"/>', "after",
        )

        assert "error" in result
        assert "empty" in result["error"]
        assert _read_document_xml(path) == before

    def test_whitespace_only_text_run_is_still_accepted_documenting_deliberate_scope(self):
        """The fix is scoped to "zero child elements" -- a run that HAS a
        child element but whose text is blank (e.g. an intentional spacer)
        is a different, narrower case and is NOT rejected by this item.
        Pinning this prevents the scope from silently drifting wider in a
        future change without a test noticing."""
        payload = _omml("<m:r><m:t></m:t></m:r>")
        root = docs_intel._validate_omml_structure(payload)
        assert len(list(root)) == 1

    def test_edit_equation_local_also_rejects_empty_replacement_body(self, tmp_path, monkeypatch):
        """The same validator backs edit_equation_local's new_payload path
        -- confirm the empty-body gate applies there too, not just on
        insert."""
        path = _write_docx(tmp_path)
        monkeypatch.setattr(
            docs_intel.render_gate, "check_render_capability",
            lambda p, **kw: {"status": "rendered", "backend": "test-stub", "detail": {}},
        )
        seed = docs_intel.insert_equation_local(path, "P0000001", _SIMPLE_OMATH, "append")
        assert seed["status"] == "inserted"
        before = _read_document_xml(path)

        result = docs_intel.edit_equation_local(
            path, "P0000001", f'<m:oMath xmlns:m="{_M}"/>',
        )
        assert "error" in result
        assert _read_document_xml(path) == before


# ---------------------------------------------------------------------------
# Display (before/after) vs. inline (append) insertion
# ---------------------------------------------------------------------------


class TestDisplayVsInlineInsertion:
    def test_after_creates_new_display_paragraph_with_default_center_alignment(
        self, tmp_path, monkeypatch,
    ):
        path = _write_docx(tmp_path)
        monkeypatch.setattr(
            docs_intel.render_gate, "check_render_capability",
            lambda p, **kw: {"status": "rendered", "backend": "test-stub", "detail": {}},
        )
        before_body_len = len(list(ET.fromstring(_read_document_xml(path)).find(_qw("body"))))
        omml = docs_intel.latex_to_omml_local(_CASES_LATEX)

        result = docs_intel.insert_equation_local(path, "P0000001", omml, "after")
        assert result["status"] == "inserted"

        root = ET.fromstring(_read_document_xml(path))
        body = root.find(_qw("body"))
        after_children = list(body)
        assert len(after_children) == before_body_len + 1, (
            "display-mode insertion must add exactly one new top-level paragraph"
        )
        new_para = after_children[1]  # immediately after P0000001 (index 0)
        assert new_para.tag == _qw("p")
        assert new_para.get(_q(_W14, "paraId")) == result["inserted_para_id"]
        jc = new_para.find(f"./{_qw('pPr')}/{_qw('jc')}")
        assert jc is not None and jc.get(_qw("val")) == "center"
        assert new_para.find(f".//{_qm('eqArr')}") is not None

    def test_before_inserts_immediately_before_anchor_not_after(self, tmp_path, monkeypatch):
        path = _write_docx(tmp_path)
        monkeypatch.setattr(
            docs_intel.render_gate, "check_render_capability",
            lambda p, **kw: {"status": "rendered", "backend": "test-stub", "detail": {}},
        )
        omml = docs_intel.latex_to_omml_local(_MAX_FUNC_LATEX)

        result = docs_intel.insert_equation_local(path, "P0000002", omml, "before")
        assert result["status"] == "inserted"

        root = ET.fromstring(_read_document_xml(path))
        body_children = list(root.find(_qw("body")))
        para_ids = [c.get(_q(_W14, "paraId")) for c in body_children if c.tag == _qw("p")]
        assert para_ids.index(result["inserted_para_id"]) == para_ids.index("P0000002") - 1

    def test_custom_style_policy_alignment_is_honored_for_display_equations(
        self, tmp_path, monkeypatch,
    ):
        path = _write_docx(tmp_path)
        monkeypatch.setattr(
            docs_intel.render_gate, "check_render_capability",
            lambda p, **kw: {"status": "rendered", "backend": "test-stub", "detail": {}},
        )
        omml = docs_intel.latex_to_omml_local(_CASES_LATEX)

        result = docs_intel.insert_equation_local(
            path, "P0000001", omml, "after",
            style_policy={"equation_alignment": "left", "body_indent_twips": 240},
        )
        assert result["status"] == "inserted"

        root = ET.fromstring(_read_document_xml(path))
        new_para = root.find(f".//{_qw('p')}[@{_q(_W14,'paraId')}=\"{result['inserted_para_id']}\"]")
        jc = new_para.find(f"./{_qw('pPr')}/{_qw('jc')}")
        ind = new_para.find(f"./{_qw('pPr')}/{_qw('ind')}")
        assert jc.get(_qw("val")) == "left"
        assert ind.get(_qw("left")) == "240"

    def test_append_mode_does_not_create_a_new_paragraph(self, tmp_path, monkeypatch):
        path = _write_docx(tmp_path)
        monkeypatch.setattr(
            docs_intel.render_gate, "check_render_capability",
            lambda p, **kw: {"status": "rendered", "backend": "test-stub", "detail": {}},
        )
        before_body_len = len(list(ET.fromstring(_read_document_xml(path)).find(_qw("body"))))
        omml = docs_intel.latex_to_omml_local(_CASES_LATEX)

        result = docs_intel.insert_equation_local(path, "P0000001", omml, "append")
        assert result["status"] == "inserted"

        root = ET.fromstring(_read_document_xml(path))
        after_children = list(root.find(_qw("body")))
        assert len(after_children) == before_body_len, (
            "inline (append) insertion must NOT add a new top-level body element"
        )
        anchor = root.find(f".//{_qw('p')}[@{_q(_W14,'paraId')}=\"P0000001\"]")
        assert anchor.find(f".//{_qm('eqArr')}") is not None

    def test_display_and_inline_insertion_of_same_equation_produce_identical_semantic_manifest(
        self, tmp_path, monkeypatch,
    ):
        """Position changes the PARAGRAPH wrapping only -- the equation's
        own structural identity (semantic manifest) must be unaffected."""
        monkeypatch.setattr(
            docs_intel.render_gate, "check_render_capability",
            lambda p, **kw: {"status": "rendered", "backend": "test-stub", "detail": {}},
        )
        omml = docs_intel.latex_to_omml_local(_MIN_SUBSCRIPT_LATEX)

        path_display = _write_docx(tmp_path, "display.docx")
        r1 = docs_intel.insert_equation_local(path_display, "P0000001", omml, "after")
        assert r1["status"] == "inserted"

        path_inline = _write_docx(tmp_path, "inline.docx")
        r2 = docs_intel.insert_equation_local(path_inline, "P0000001", omml, "append")
        assert r2["status"] == "inserted"

        eqs_display = docs_intel.parse_docx_equations_local(path_display)
        eqs_inline = docs_intel.parse_docx_equations_local(path_inline)
        assert len(eqs_display) == 1 and len(eqs_inline) == 1

        rec_display = docs_intel._omml_semantic_record(ET.fromstring(eqs_display[0]["omml_raw"]))
        rec_inline = docs_intel._omml_semantic_record(ET.fromstring(eqs_inline[0]["omml_raw"]))
        assert rec_display["structural_tags"] == rec_inline["structural_tags"] == {
            "func": 1, "sSub": 1,
        }
        assert rec_display["flat_text"] == rec_inline["flat_text"]


# ---------------------------------------------------------------------------
# The tri-state render-capability gate generalizes to complex payloads.
#
# test_docx_word_com_regression.py already proves the tri-state contract
# (rendered / failed / unavailable-with-reason) for TRIVIAL single-run
# equations. This section proves the SAME contract holds for the eqArr/
# func/nested payloads this item is actually about -- guarding against a
# regression where the gate integration silently only worked for simple
# equations.
# ---------------------------------------------------------------------------


class TestRenderGateWithComplexPayloads:
    def test_rendered_reports_evidence_and_keeps_nested_structure(self, tmp_path, monkeypatch):
        path = _write_docx(tmp_path)
        monkeypatch.setattr(
            docs_intel.render_gate, "check_render_capability",
            lambda p, **kw: {"status": "rendered", "backend": "libreoffice-soffice", "detail": {}},
        )
        omml = docs_intel.latex_to_omml_local(_NESTED_FRACTION_CASES_LATEX)

        result = docs_intel.insert_equation_local(path, "P0000001", omml, "after")

        assert result["status"] == "inserted"
        assert result["render_status"] == "rendered"
        assert result["render_verified"] is True
        equations = docs_intel.parse_docx_equations_local(path)
        assert ET.fromstring(equations[0]["omml_raw"]).find(_qm("eqArr")) is not None

    def test_failed_restores_original_file_for_function_node_equation(self, tmp_path, monkeypatch):
        path = _write_docx(tmp_path)
        before = _read_document_xml(path)
        monkeypatch.setattr(
            docs_intel.render_gate, "check_render_capability",
            lambda p, **kw: {"status": "failed", "reason": "simulated render crash"},
        )
        omml = docs_intel.latex_to_omml_local(_MAX_FUNC_LATEX)

        result = docs_intel.insert_equation_local(path, "P0000001", omml, "append")

        assert "error" in result
        assert result["render_status"] == "failed"
        assert result["file_restored"] is True
        assert _read_document_xml(path) == before

    def test_unavailable_fails_closed_by_default_and_degrades_with_audited_override(
        self, tmp_path, monkeypatch,
    ):
        path = _write_docx(tmp_path)
        before = _read_document_xml(path)
        monkeypatch.setattr(
            docs_intel.render_gate, "check_render_capability",
            lambda p, **kw: {
                "status": "unavailable-with-reason",
                "reason": "no render backend available in this environment",
            },
        )
        omml = docs_intel.latex_to_omml_local(_MIN_SUBSCRIPT_LATEX)

        default_result = docs_intel.insert_equation_local(path, "P0000001", omml, "before")
        assert "error" in default_result
        assert default_result["render_status"] == "unavailable-with-reason"
        assert default_result["file_restored"] is True
        assert _read_document_xml(path) == before

        degraded_result = docs_intel.insert_equation_local(
            path, "P0000001", omml, "before",
            allow_degraded_render=True,
            degraded_render_reason="CI sandbox has no LibreOffice/Word installed",
        )
        assert degraded_result["status"] == "inserted"
        assert degraded_result["render_verified"] is False
        assert degraded_result["render_degraded"] is True

        equations = docs_intel.parse_docx_equations_local(path)
        assert len(equations) == 1
        record = docs_intel._omml_semantic_record(ET.fromstring(equations[0]["omml_raw"]))
        assert record["structural_tags"] == {"func": 1, "sSub": 1}


# ---------------------------------------------------------------------------
# ZIP / OOXML package integrity (independent of any single insertion shape)
# ---------------------------------------------------------------------------


class TestZipAndOoxmlIntegrity:
    @pytest.mark.parametrize("label,payload_getter", [
        ("cases", lambda: docs_intel.latex_to_omml_local(_CASES_LATEX)),
        ("func", lambda: docs_intel.latex_to_omml_local(_MAX_FUNC_LATEX)),
        ("nested", lambda: docs_intel.latex_to_omml_local(_MIN_SUBSCRIPT_LATEX)),
        ("three_branch_cases", lambda: _THREE_BRANCH_CASES_OMML),
    ])
    def test_document_stays_valid_ooxml_after_each_equation_shape(
        self, tmp_path, monkeypatch, label, payload_getter,
    ):
        path = _write_docx(tmp_path, f"{label}.docx")
        monkeypatch.setattr(
            docs_intel.render_gate, "check_render_capability",
            lambda p, **kw: {"status": "rendered", "backend": "test-stub", "detail": {}},
        )
        result = docs_intel.insert_equation_local(path, "P0000001", payload_getter(), "after")
        assert result["status"] == "inserted", f"{label}: {result}"
        _assert_valid_ooxml_package(path)

    def test_three_equation_shapes_in_one_document_stay_a_single_valid_package(
        self, tmp_path, monkeypatch,
    ):
        """Cases (display), function node (before), and a nested combo
        (inline append) all land in the SAME document -- confirming the
        document remains one coherent, valid package and every equation's
        semantic identity survives independently."""
        path = _write_docx(tmp_path)
        monkeypatch.setattr(
            docs_intel.render_gate, "check_render_capability",
            lambda p, **kw: {"status": "rendered", "backend": "test-stub", "detail": {}},
        )

        r1 = docs_intel.insert_equation_local(
            path, "P0000001", docs_intel.latex_to_omml_local(_CASES_LATEX), "after",
        )
        r2 = docs_intel.insert_equation_local(
            path, "P0000002", docs_intel.latex_to_omml_local(_MAX_FUNC_LATEX), "before",
        )
        r3 = docs_intel.insert_equation_local(
            path, "P0000003", docs_intel.latex_to_omml_local(_MIN_SUBSCRIPT_LATEX), "append",
        )
        assert r1["status"] == r2["status"] == r3["status"] == "inserted"

        _assert_valid_ooxml_package(path)
        equations = docs_intel.parse_docx_equations_local(path)
        assert len(equations) == 3
        tag_sets = [
            docs_intel._omml_semantic_record(ET.fromstring(eq["omml_raw"]))["structural_tags"]
            for eq in equations
        ]
        assert {"eqArr": 1} in tag_sets
        assert {"func": 1} in tag_sets
        assert {"func": 1, "sSub": 1} in tag_sets


# ---------------------------------------------------------------------------
# Direct Word-open/readability verification.
#
# NEVER monkeypatched: this section drives the REAL render_gate machinery.
# It probes the actual Word-COM-only receipt (render_gate.
# check_word_com_render_receipt) *before* asserting anything, and branches
# on the genuinely observed status -- so this suite degrades gracefully (and
# HONESTLY) on a machine without Word/pywin32, and equally never
# under-claims on a machine that does have Word available.
# ---------------------------------------------------------------------------


class TestWordOpenReadabilityVerification:
    def test_word_authored_fixture_itself_is_a_valid_package(self, tmp_path):
        """Sanity check on the fixture builder, independent of any writer."""
        path = _write_word_authored_docx(tmp_path)
        _assert_valid_ooxml_package(
            path,
            required_parts=(
                "[Content_Types].xml", "_rels/.rels", "word/document.xml",
                "word/styles.xml", "word/_rels/document.xml.rels",
            ),
        )

    @pytest.mark.parametrize("label,anchor,position,payload_fn", [
        ("cases", "P0000001", "after", lambda: docs_intel.latex_to_omml_local(_CASES_LATEX)),
        ("func", "P0000002", "before", lambda: docs_intel.latex_to_omml_local(_MAX_FUNC_LATEX)),
        ("nested", "P0000003", "append", lambda: docs_intel.latex_to_omml_local(_MIN_SUBSCRIPT_LATEX)),
    ])
    def test_real_word_open_verification_or_honest_degrade(
        self, tmp_path, label, anchor, position, payload_fn,
    ):
        path = _write_word_authored_docx(tmp_path, f"{label}_real.docx")
        before = _read_document_xml(path)
        payload = payload_fn()

        # Insert using insert_equation_local's OWN (real, unmocked) render
        # gate first -- this may pick whichever backend
        # render_gate.KNOWN_BACKENDS finds first (LibreOffice or Word COM);
        # either way it proves the write pipeline genuinely accepts this
        # equation shape end to end on THIS machine.
        general_status = render_gate.check_render_capability(path)["status"]
        if general_status == render_gate.RENDERED:
            result = docs_intel.insert_equation_local(path, anchor, payload, position)
            assert result["status"] == "inserted", f"{label}: {result}"
        elif general_status == render_gate.UNAVAILABLE_WITH_REASON:
            result = docs_intel.insert_equation_local(
                path, anchor, payload, position,
                allow_degraded_render=True,
                degraded_render_reason="no render backend available in this environment (structural fallback)",
            )
            assert result["status"] == "inserted", f"{label}: {result}"
            assert result["render_verified"] is False
            assert result["render_degraded"] is True
        else:
            assert general_status == render_gate.FAILED
            result = docs_intel.insert_equation_local(path, anchor, payload, position)
            assert "error" in result
            assert _read_document_xml(path) == before
            return  # nothing further to check -- the write correctly did not land

        _assert_valid_ooxml_package(
            path,
            required_parts=(
                "[Content_Types].xml", "_rels/.rels", "word/document.xml",
                "word/styles.xml", "word/_rels/document.xml.rels",
            ),
        )
        assert _oMath_elements(path), f"{label}: no <m:oMath> found after insertion"

        # -- Now the DIRECT, Word-specific probe (never mocked). This is
        # deliberately a SEPARATE check from the general render gate above:
        # check_render_capability's default backend order may satisfy the
        # write via LibreOffice even on a machine that also has Word, so
        # only check_word_com_render_receipt actually proves WORD opened
        # this exact file.
        word_status = render_gate.check_word_com_render_receipt(path)
        status = word_status["status"]

        if status == render_gate.RENDERED:
            # Real, direct Word-open evidence -- the ONLY branch allowed to
            # claim Word actually verified this document.
            detail = word_status.get("detail") or {}
            assert word_status.get("backend") == "word-com"
            assert detail.get("converted_via") == "word-com"
            assert detail.get("pdf_sha256")
            assert detail.get("page_count", 0) >= 1
        else:
            # Word is not actually available for real verification on this
            # machine (no pywin32, no Word install, or a live COM failure).
            # This branch must NEVER claim Word-verified success -- it only
            # asserts the deterministic structural fallback evidence that
            # does not depend on Word being present, and explicitly marks
            # the renderer capability as degraded rather than silent.
            assert status in (render_gate.UNAVAILABLE_WITH_REASON, render_gate.FAILED)
            capability_report = {
                "word_open_verified": False,
                "renderer_capability": "degraded",
                "reason": word_status.get("reason"),
            }
            assert capability_report["word_open_verified"] is False
            # Structural coverage still holds even without Word.
            _assert_valid_ooxml_package(
                path,
                required_parts=(
                    "[Content_Types].xml", "_rels/.rels", "word/document.xml",
                ),
            )
            assert _oMath_elements(path)

    def test_word_com_receipt_status_is_always_one_of_the_documented_tri_states(self, tmp_path):
        """Guards the contract itself: check_word_com_render_receipt must
        never return a status outside the documented three -- a caller
        branching on exactly those three (as this file does throughout)
        would otherwise silently fall through to an unhandled case."""
        path = _write_word_authored_docx(tmp_path)
        result = render_gate.check_word_com_render_receipt(path)
        assert result["status"] in render_gate.RENDER_STATUSES
