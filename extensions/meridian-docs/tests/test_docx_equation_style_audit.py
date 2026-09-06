"""Tests for the configurable document style policy and audit_equation_style
(4efc63fd).

Covers:
  - resolve_style_policy: defaults, override merging, and validation errors
    for every recognised key.
  - audit_equation_style: misaligned_equation, missing/incorrect trailing
    punctuation, and duplicate/gap equation numbering findings, each in
    isolation, plus the intentional exclusions (inline equations, ambiguous
    multi-equation paragraphs, table-numbered equations for alignment).
  - Read-only invariant: audit_equation_style never mutates the .docx.
  - Error paths: unknown file, invalid style_policy.
  - Integration: insert_equation_local's default policy output satisfies
    audit_equation_style's default expectations (write + audit consistency).

All tests use synthetic .docx bytes built inline -- no real files, no network.
"""
from __future__ import annotations

import io
import os
import sys
import zipfile

import pytest

# Make meridian_docs importable from the local extensions directory (mirrors
# tests/test_meridian_docs_equations.py's convention for root-level tests;
# harmless no-op when meridian_docs is already installed/importable).
_EXT_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _EXT_PATH not in sys.path:
    sys.path.insert(0, _EXT_PATH)

from meridian_docs import docs_intel, server  # noqa: E402


@pytest.fixture(autouse=True)
def _default_render_capability(monkeypatch):
    """016015e1/ddd79188 -- insert_equation_local now invokes the real
    render-capability gate (render_gate.check_render_capability) AFTER
    structural verification passes. Tests in this file exercise STRUCTURAL
    correctness and must not depend on -- or be slowed/blocked by --
    whichever render backends (LibreOffice, Word COM) happen to be
    installed on the machine running the suite. Stub a successful
    'rendered' result by default, mirroring
    test_19be1551_insert_figure_block.py's fixture of the same name.
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


_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"

_NS_HEADER = (
    f'xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:m="{_M}"'
)


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


_SIMPLE_OMATH = (
    f'<m:oMath xmlns:m="{_M}">'
    "<m:r><m:t>E</m:t></m:r><m:r><m:t>=</m:t></m:r><m:r><m:t>m</m:t></m:r>"
    "<m:sSup><m:e><m:r><m:t>c</m:t></m:r></m:e>"
    "<m:sup><m:r><m:t>2</m:t></m:r></m:sup></m:sSup>"
    "</m:oMath>"
)


# ---------------------------------------------------------------------------
# Synthetic document fixtures
# ---------------------------------------------------------------------------

def _doc(body_xml: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document {_NS_HEADER}>
  <w:body>
{body_xml}
  </w:body>
</w:document>"""


_EMPTY_DOC = _doc('    <w:p><w:r><w:t>No equations here.</w:t></w:r></w:p>')

# Display equation, no pPr at all -> alignment defaults to "left", and no
# trailing text -> both misaligned_equation and missing_trailing_punctuation.
_DISPLAY_UNSTYLED_DOC = _doc(f'''    <w:p w14:paraId="0000E001">
      {_SIMPLE_OMATH}
    </w:p>''')

# Display equation, explicitly centered, no trailing text -> only
# missing_trailing_punctuation.
_DISPLAY_CENTERED_NO_PUNCT_DOC = _doc(f'''    <w:p w14:paraId="0000F001">
      <w:pPr><w:jc w:val="center"/></w:pPr>
      {_SIMPLE_OMATH}
    </w:p>''')

# Display equation, centered, trailing "." -> clean (no findings).
_DISPLAY_CLEAN_DOC = _doc(f'''    <w:p w14:paraId="0000G001">
      <w:pPr><w:jc w:val="center"/></w:pPr>
      {_SIMPLE_OMATH}
      <w:r><w:t>.</w:t></w:r>
    </w:p>''')

# Display equation, centered, trailing text ending in a letter -> incorrect
# trailing punctuation.
_DISPLAY_BAD_PUNCT_DOC = _doc(f'''    <w:p w14:paraId="0000H001">
      <w:pPr><w:jc w:val="center"/></w:pPr>
      {_SIMPLE_OMATH}
      <w:r><w:t xml:space="preserve"> where c is the speed of light</w:t></w:r>
    </w:p>''')

# Inline equation mixed with prose -> excluded from both checks even though
# the trailing text doesn't end in accepted punctuation and there is no jc.
_INLINE_MIXED_DOC = _doc(f'''    <w:p w14:paraId="0000I001">
      <w:r><w:t xml:space="preserve">Einstein: </w:t></w:r>
      {_SIMPLE_OMATH}
      <w:r><w:t xml:space="preserve"> was a physicist</w:t></w:r>
    </w:p>''')

# Two oMath as the ONLY content of one paragraph -> ambiguous, both skipped.
_AMBIGUOUS_MULTI_EQ_DOC = _doc(f'''    <w:p w14:paraId="0000J001">
      {_SIMPLE_OMATH}
      <m:oMath><m:r><m:t>F</m:t></m:r><m:r><m:t>=</m:t></m:r><m:r><m:t>ma</m:t></m:r></m:oMath>
    </w:p>''')


def _numbered_row(para_id: str, number_text: str) -> str:
    return f'''    <w:tr>
      <w:tc><w:p w14:paraId="{para_id}">{_SIMPLE_OMATH}</w:p></w:tc>
      <w:tc><w:p><w:r><w:t>{number_text}</w:t></w:r></w:p></w:tc>
    </w:tr>'''


_DUPLICATE_NUMBERS_DOC = _doc(
    "    <w:tbl>\n"
    + _numbered_row("EQD001", "(1)") + "\n"
    + _numbered_row("EQD002", "(1)") + "\n"
    "    </w:tbl>"
)

_GAP_NUMBERS_DOC = _doc(
    "    <w:tbl>\n"
    + _numbered_row("EQG001", "(1)") + "\n"
    + _numbered_row("EQG002", "(3)") + "\n"
    "    </w:tbl>"
)

_ALPHA_SUFFIX_DOC = _doc(
    "    <w:tbl>\n"
    + _numbered_row("EQA001", "(1)") + "\n"
    + _numbered_row("EQA002", "(2)") + "\n"
    + _numbered_row("EQA003", "(2a)") + "\n"
    "    </w:tbl>"
)

_NONNUMERIC_DUP_DOC = _doc(
    "    <w:tbl>\n"
    + _numbered_row("EQN001", "(A.1)") + "\n"
    + _numbered_row("EQN002", "(A.1)") + "\n"
    "    </w:tbl>"
)


# ---------------------------------------------------------------------------
# resolve_style_policy
# ---------------------------------------------------------------------------

def test_resolve_style_policy_defaults():
    policy = docs_intel.resolve_style_policy()
    assert policy == {
        "caption_centered": False,
        "body_indent_twips": 0,
        "equation_alignment": "center",
        "equation_punctuation_required": True,
        "equation_punctuation_chars": ".,;:",
        "note_style": "MeridianInternalNote",
        "note_highlight_color": "yellow",
        "heading_terminal_punctuation": None,
        "table_label_column_alignment": None,
        "table_data_column_alignment": None,
    }


def test_resolve_style_policy_none_overrides_returns_defaults():
    assert docs_intel.resolve_style_policy(None) == docs_intel.resolve_style_policy()
    assert docs_intel.resolve_style_policy({}) == docs_intel.resolve_style_policy()


def test_resolve_style_policy_merges_partial_overrides():
    policy = docs_intel.resolve_style_policy({"caption_centered": True, "body_indent_twips": 360})
    assert policy["caption_centered"] is True
    assert policy["body_indent_twips"] == 360
    # Untouched keys keep their defaults.
    assert policy["equation_alignment"] == "center"
    assert policy["note_style"] == "MeridianInternalNote"


def test_resolve_style_policy_rejects_unknown_key():
    with pytest.raises(ValueError, match="unknown style policy key"):
        docs_intel.resolve_style_policy({"not_a_real_key": True})


@pytest.mark.parametrize("bad_value", [1, "yes", None, [], {}])
def test_resolve_style_policy_rejects_non_bool_caption_centered(bad_value):
    with pytest.raises(ValueError, match="caption_centered"):
        docs_intel.resolve_style_policy({"caption_centered": bad_value})


@pytest.mark.parametrize("bad_value", [-1, -100, "360", 1.5, True])
def test_resolve_style_policy_rejects_bad_body_indent_twips(bad_value):
    with pytest.raises(ValueError, match="body_indent_twips"):
        docs_intel.resolve_style_policy({"body_indent_twips": bad_value})


def test_resolve_style_policy_accepts_zero_body_indent_twips():
    assert docs_intel.resolve_style_policy({"body_indent_twips": 0})["body_indent_twips"] == 0


@pytest.mark.parametrize("valid_alignment", ["left", "center", "right", "both"])
def test_resolve_style_policy_accepts_all_valid_equation_alignments(valid_alignment):
    policy = docs_intel.resolve_style_policy({"equation_alignment": valid_alignment})
    assert policy["equation_alignment"] == valid_alignment


def test_resolve_style_policy_rejects_bad_equation_alignment():
    with pytest.raises(ValueError, match="equation_alignment"):
        docs_intel.resolve_style_policy({"equation_alignment": "diagonal"})


def test_resolve_style_policy_rejects_non_bool_punctuation_required():
    with pytest.raises(ValueError, match="equation_punctuation_required"):
        docs_intel.resolve_style_policy({"equation_punctuation_required": "yes"})


def test_resolve_style_policy_rejects_empty_punctuation_chars():
    with pytest.raises(ValueError, match="equation_punctuation_chars"):
        docs_intel.resolve_style_policy({"equation_punctuation_chars": ""})


def test_resolve_style_policy_rejects_empty_note_style():
    with pytest.raises(ValueError, match="note_style"):
        docs_intel.resolve_style_policy({"note_style": "   "})


def test_resolve_style_policy_rejects_bad_note_highlight_color():
    with pytest.raises(ValueError, match="note_highlight_color"):
        docs_intel.resolve_style_policy({"note_highlight_color": "chartreuse"})


def test_resolve_style_policy_accepts_valid_note_highlight_color():
    policy = docs_intel.resolve_style_policy({"note_highlight_color": "cyan"})
    assert policy["note_highlight_color"] == "cyan"


# ---------------------------------------------------------------------------
# resolve_style_policy -- 4544bbe5 document-profile keys (heading terminal
# punctuation, table column alignment)
# ---------------------------------------------------------------------------

def test_resolve_style_policy_heading_terminal_punctuation_defaults_none():
    assert docs_intel.resolve_style_policy()["heading_terminal_punctuation"] is None


def test_resolve_style_policy_accepts_empty_heading_terminal_punctuation():
    policy = docs_intel.resolve_style_policy({"heading_terminal_punctuation": ""})
    assert policy["heading_terminal_punctuation"] == ""


def test_resolve_style_policy_accepts_string_heading_terminal_punctuation():
    policy = docs_intel.resolve_style_policy({"heading_terminal_punctuation": ":"})
    assert policy["heading_terminal_punctuation"] == ":"


@pytest.mark.parametrize("bad_value", [1, True, [], {}])
def test_resolve_style_policy_rejects_non_string_heading_terminal_punctuation(bad_value):
    with pytest.raises(ValueError, match="heading_terminal_punctuation"):
        docs_intel.resolve_style_policy({"heading_terminal_punctuation": bad_value})


@pytest.mark.parametrize(
    "key", ["table_label_column_alignment", "table_data_column_alignment"]
)
def test_resolve_style_policy_table_column_alignment_defaults_none(key):
    assert docs_intel.resolve_style_policy()[key] is None


@pytest.mark.parametrize(
    "key", ["table_label_column_alignment", "table_data_column_alignment"]
)
@pytest.mark.parametrize("valid_alignment", ["left", "center", "right", "both"])
def test_resolve_style_policy_accepts_valid_table_column_alignments(key, valid_alignment):
    policy = docs_intel.resolve_style_policy({key: valid_alignment})
    assert policy[key] == valid_alignment


@pytest.mark.parametrize(
    "key", ["table_label_column_alignment", "table_data_column_alignment"]
)
def test_resolve_style_policy_rejects_bad_table_column_alignment(key):
    with pytest.raises(ValueError, match=key):
        docs_intel.resolve_style_policy({key: "diagonal"})


# ---------------------------------------------------------------------------
# get_journal_style_preset -- 4544bbe5 publishing-convention shorthand
# ---------------------------------------------------------------------------

def test_get_journal_style_preset_default_matches_resolve_style_policy_defaults():
    assert docs_intel.get_journal_style_preset("default") == docs_intel.resolve_style_policy()


def test_get_journal_style_preset_jcshm_is_fully_resolved():
    preset = docs_intel.get_journal_style_preset("jcshm")
    # Every resolve_style_policy key is present (fully resolved, not raw overrides).
    assert set(preset) == set(docs_intel.resolve_style_policy())
    assert preset["caption_centered"] is True
    assert preset["heading_terminal_punctuation"] == ""
    assert preset["table_label_column_alignment"] == "left"
    assert preset["table_data_column_alignment"] == "center"


def test_get_journal_style_preset_jcshm_round_trips_through_resolve_style_policy():
    preset = docs_intel.get_journal_style_preset("jcshm")
    # A fully-resolved policy must be idempotent under re-resolution.
    assert docs_intel.resolve_style_policy(preset) == preset


def test_get_journal_style_preset_rejects_unknown_name():
    with pytest.raises(ValueError, match="unknown journal style preset"):
        docs_intel.get_journal_style_preset("not-a-real-journal")


# ---------------------------------------------------------------------------
# get_journal_style_preset -- MCP tool boundary (server.py wrapper)
# ---------------------------------------------------------------------------

def test_server_get_journal_style_preset_delegates_to_docs_intel():
    assert server.get_journal_style_preset("jcshm") == docs_intel.get_journal_style_preset("jcshm")


def test_server_get_journal_style_preset_unknown_name_returns_error_dict():
    """The MCP boundary never raises -- an unknown preset name comes back as
    a structured {"error": ...} dict, same convention as every other tool."""
    result = server.get_journal_style_preset("not-a-real-journal")
    assert "error" in result
    assert "not-a-real-journal" in result["error"]


def test_server_get_journal_style_preset_result_usable_as_style_policy(tmp_path):
    """The preset returned at the MCP boundary round-trips straight into
    another tool's style_policy= parameter with no further transformation."""
    preset = server.get_journal_style_preset("jcshm")
    assert docs_intel.resolve_style_policy(preset) == preset


# ---------------------------------------------------------------------------
# audit_equation_style -- basic shape / no equations
# ---------------------------------------------------------------------------

def test_audit_equation_style_empty_document_has_no_findings(tmp_path):
    path = _write_docx(tmp_path, _EMPTY_DOC)
    result = docs_intel.audit_equation_style(path)
    assert "error" not in result
    assert result["equation_count"] == 0
    assert result["findings"] == []
    assert result["finding_count"] == 0
    assert result["findings_by_type"] == {}
    assert result["policy"] == docs_intel.resolve_style_policy()


# ---------------------------------------------------------------------------
# audit_equation_style -- alignment
# ---------------------------------------------------------------------------

def test_audit_flags_misaligned_and_missing_punctuation_for_unstyled_equation(tmp_path):
    path = _write_docx(tmp_path, _DISPLAY_UNSTYLED_DOC)
    result = docs_intel.audit_equation_style(path)
    assert result["equation_count"] == 1
    assert result["findings_by_type"] == {
        "misaligned_equation": 1,
        "missing_trailing_punctuation": 1,
    }
    misaligned = next(f for f in result["findings"] if f["type"] == "misaligned_equation")
    assert misaligned["para_id"] == "0000E001"
    assert misaligned["expected_alignment"] == "center"
    assert misaligned["actual_alignment"] == "left"


def test_audit_no_misalignment_finding_when_centered_matches_default_policy(tmp_path):
    path = _write_docx(tmp_path, _DISPLAY_CENTERED_NO_PUNCT_DOC)
    result = docs_intel.audit_equation_style(path)
    types = {f["type"] for f in result["findings"]}
    assert "misaligned_equation" not in types


def test_audit_alignment_policy_left_matches_unset_jc(tmp_path):
    path = _write_docx(tmp_path, _DISPLAY_UNSTYLED_DOC)
    result = docs_intel.audit_equation_style(path, style_policy={"equation_alignment": "left"})
    types = {f["type"] for f in result["findings"]}
    assert "misaligned_equation" not in types


def test_audit_inline_equation_excluded_from_alignment_and_punctuation(tmp_path):
    path = _write_docx(tmp_path, _INLINE_MIXED_DOC)
    result = docs_intel.audit_equation_style(path)
    assert result["equation_count"] == 1
    assert result["findings"] == []


def test_audit_ambiguous_multi_equation_paragraph_is_skipped(tmp_path):
    path = _write_docx(tmp_path, _AMBIGUOUS_MULTI_EQ_DOC)
    result = docs_intel.audit_equation_style(path)
    assert result["equation_count"] == 2
    assert result["findings"] == []


# ---------------------------------------------------------------------------
# audit_equation_style -- trailing punctuation
# ---------------------------------------------------------------------------

def test_audit_missing_trailing_punctuation(tmp_path):
    path = _write_docx(tmp_path, _DISPLAY_CENTERED_NO_PUNCT_DOC)
    result = docs_intel.audit_equation_style(path)
    assert result["findings_by_type"] == {"missing_trailing_punctuation": 1}
    finding = result["findings"][0]
    assert finding["para_id"] == "0000F001"
    assert finding["expected_punctuation_chars"] == ".,;:"


def test_audit_clean_equation_has_no_findings(tmp_path):
    path = _write_docx(tmp_path, _DISPLAY_CLEAN_DOC)
    result = docs_intel.audit_equation_style(path)
    assert result["findings"] == []
    assert result["finding_count"] == 0


def test_audit_incorrect_trailing_punctuation(tmp_path):
    path = _write_docx(tmp_path, _DISPLAY_BAD_PUNCT_DOC)
    result = docs_intel.audit_equation_style(path)
    assert result["findings_by_type"] == {"incorrect_trailing_punctuation": 1}
    finding = result["findings"][0]
    assert finding["para_id"] == "0000H001"
    assert finding["actual_char"] == "t"
    assert "light" in finding["actual_trailing_text"]


def test_audit_punctuation_not_required_suppresses_punctuation_findings(tmp_path):
    path = _write_docx(tmp_path, _DISPLAY_CENTERED_NO_PUNCT_DOC)
    result = docs_intel.audit_equation_style(
        path, style_policy={"equation_punctuation_required": False}
    )
    assert result["findings"] == []


def test_audit_custom_punctuation_chars(tmp_path):
    # "!" is not in the default accepted set, so make a document ending in "!"
    # and confirm it flags under the default policy but not when "!" is added.
    doc = _doc(f'''    <w:p w14:paraId="0000K001">
      <w:pPr><w:jc w:val="center"/></w:pPr>
      {_SIMPLE_OMATH}
      <w:r><w:t>!</w:t></w:r>
    </w:p>''')
    path = _write_docx(tmp_path, doc)

    default_result = docs_intel.audit_equation_style(path)
    assert default_result["findings_by_type"] == {"incorrect_trailing_punctuation": 1}

    custom_result = docs_intel.audit_equation_style(
        path, style_policy={"equation_punctuation_chars": ".,;:!"}
    )
    assert custom_result["findings"] == []


# ---------------------------------------------------------------------------
# audit_equation_style -- numbering: duplicates and gaps
# ---------------------------------------------------------------------------

def test_audit_duplicate_equation_numbers(tmp_path):
    path = _write_docx(tmp_path, _DUPLICATE_NUMBERS_DOC)
    result = docs_intel.audit_equation_style(path)
    assert result["findings_by_type"] == {"duplicate_equation_number": 1}
    finding = result["findings"][0]
    assert finding["number"] == "(1)"
    assert sorted(finding["para_ids"]) == ["EQD001", "EQD002"]
    assert len(finding["ordinals"]) == 2


def test_audit_equation_number_gap(tmp_path):
    path = _write_docx(tmp_path, _GAP_NUMBERS_DOC)
    result = docs_intel.audit_equation_style(path)
    assert result["findings_by_type"] == {"equation_number_gap": 1}
    finding = result["findings"][0]
    assert finding["missing_number"] == 2


def test_audit_alphabetic_suffix_does_not_create_false_gap_or_duplicate(tmp_path):
    path = _write_docx(tmp_path, _ALPHA_SUFFIX_DOC)
    result = docs_intel.audit_equation_style(path)
    assert result["findings"] == []


def test_audit_nonnumeric_labels_still_detect_duplicates_but_skip_gaps(tmp_path):
    path = _write_docx(tmp_path, _NONNUMERIC_DUP_DOC)
    result = docs_intel.audit_equation_style(path)
    assert result["findings_by_type"] == {"duplicate_equation_number": 1}
    finding = result["findings"][0]
    assert finding["number"] == "(A.1)"


def test_audit_table_numbered_equations_excluded_from_alignment_check(tmp_path):
    """Table-numbered equations never produce misaligned_equation findings --
    their 2-column layout has no single well-defined expected alignment."""
    path = _write_docx(tmp_path, _GAP_NUMBERS_DOC)
    result = docs_intel.audit_equation_style(path)
    types = {f["type"] for f in result["findings"]}
    assert "misaligned_equation" not in types
    assert "missing_trailing_punctuation" not in types


# ---------------------------------------------------------------------------
# audit_equation_style -- read-only invariant
# ---------------------------------------------------------------------------

def test_audit_equation_style_never_mutates_the_file(tmp_path):
    path = _write_docx(tmp_path, _DISPLAY_UNSTYLED_DOC)
    before = _read_docx_xml(path)
    docs_intel.audit_equation_style(path)
    docs_intel.audit_equation_style(path, style_policy={"equation_alignment": "left"})
    assert _read_docx_xml(path) == before


# ---------------------------------------------------------------------------
# audit_equation_style -- error paths
# ---------------------------------------------------------------------------

def test_audit_equation_style_unknown_file_returns_error():
    result = docs_intel.audit_equation_style("/no/such/file.docx")
    assert "error" in result


def test_audit_equation_style_invalid_style_policy_key_returns_error(tmp_path):
    path = _write_docx(tmp_path, _EMPTY_DOC)
    result = docs_intel.audit_equation_style(path, style_policy={"bogus": True})
    assert "error" in result
    assert "unknown style policy key" in result["error"]


def test_audit_equation_style_invalid_style_policy_value_returns_error(tmp_path):
    path = _write_docx(tmp_path, _EMPTY_DOC)
    result = docs_intel.audit_equation_style(
        path, style_policy={"equation_alignment": "diagonal"}
    )
    assert "error" in result


def test_audit_equation_style_malformed_docx_returns_error(tmp_path):
    path = tmp_path / "not-a-docx.docx"
    path.write_bytes(b"not a zip file at all")
    result = docs_intel.audit_equation_style(str(path))
    assert "error" in result


# ---------------------------------------------------------------------------
# Integration: write path (insert_equation_local) satisfies audit defaults
# ---------------------------------------------------------------------------

_MINIMAL_WRITE_DOC = _doc('''    <w:p w14:paraId="0000W001">
      <w:r><w:t>Introduction.</w:t></w:r>
    </w:p>''')


def test_insert_equation_local_default_policy_satisfies_audit_alignment(tmp_path):
    """insert_equation_local's default style_policy (equation_alignment=
    "center") produces a paragraph that audit_equation_style's default
    policy considers correctly aligned -- write and audit share one policy."""
    path = _write_docx(tmp_path, _MINIMAL_WRITE_DOC)
    insert_result = docs_intel.insert_equation_local(
        path, "0000W001", _SIMPLE_OMATH, "after"
    )
    assert "error" not in insert_result

    audit_result = docs_intel.audit_equation_style(path)
    types = {f["type"] for f in audit_result["findings"]}
    assert "misaligned_equation" not in types
    # No trailing punctuation was appended, so that finding is still expected.
    assert "missing_trailing_punctuation" in types


def test_insert_then_append_punctuation_yields_a_fully_clean_audit(tmp_path):
    path = _write_docx(tmp_path, _MINIMAL_WRITE_DOC)
    insert_result = docs_intel.insert_equation_local(
        path, "0000W001", _SIMPLE_OMATH, "after"
    )
    assert "error" not in insert_result

    # The inserted equation is now the only equation in the doc -- find its
    # para_id via parse_docx_equations_local rather than assuming a fixed id.
    equations = docs_intel.parse_docx_equations_local(path)
    assert len(equations) == 1
    eq_para_id = equations[0]["para_id"]

    append_result = docs_intel.append_text_run_after_math(path, eq_para_id, ".")
    assert "error" not in append_result

    audit_result = docs_intel.audit_equation_style(path)
    assert audit_result["findings"] == []


def test_insert_equation_local_custom_alignment_and_indent(tmp_path):
    path = _write_docx(tmp_path, _MINIMAL_WRITE_DOC)
    result = docs_intel.insert_equation_local(
        path, "0000W001", _SIMPLE_OMATH, "after",
        style_policy={"equation_alignment": "left", "body_indent_twips": 720},
    )
    assert "error" not in result

    xml = _read_docx_xml(path).decode("utf-8")
    assert 'w:val="left"' in xml
    assert 'w:left="720"' in xml

    # Auditing with the SAME custom policy shows no misalignment.
    audit_result = docs_intel.audit_equation_style(
        path, style_policy={"equation_alignment": "left"}
    )
    types = {f["type"] for f in audit_result["findings"]}
    assert "misaligned_equation" not in types


def test_insert_equation_local_invalid_style_policy_does_not_mutate(tmp_path):
    path = _write_docx(tmp_path, _MINIMAL_WRITE_DOC)
    before = _read_docx_xml(path)
    result = docs_intel.insert_equation_local(
        path, "0000W001", _SIMPLE_OMATH, "after",
        style_policy={"equation_alignment": "diagonal"},
    )
    assert "error" in result
    assert _read_docx_xml(path) == before
