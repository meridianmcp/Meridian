"""Read-only venue-aware figure/table spacing audit tests."""
from __future__ import annotations

import io
import zipfile

from meridian_docs import docs_intel


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _write_docx(
    tmp_path,
    body: str,
    name: str = "spacing.docx",
    *,
    styles_xml: str | None = None,
) -> str:
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{_W}" xmlns:w14="{_W14}" xmlns:a="{_A}" xmlns:r="{_R}">
  <w:body>{body}<w:sectPr/></w:body>
</w:document>'''
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
        if styles_xml is not None:
            archive.writestr("word/styles.xml", styles_xml)
    path = tmp_path / name
    path.write_bytes(buf.getvalue())
    return str(path)


def _p(text: str = "", pid: str | None = None, *, spacing: str = "", extra: str = "") -> str:
    pid_attr = f' w14:paraId="{pid}"' if pid else ""
    ppr = f"<w:pPr>{spacing}</w:pPr>" if spacing else ""
    run = f"<w:r><w:t>{text}</w:t></w:r>" if text else ""
    return f"<w:p{pid_attr}>{ppr}{run}{extra}</w:p>"


def _blank(line: int = 240, pid: str | None = None) -> str:
    return _p(pid=pid, spacing=f'<w:spacing w:line="{line}" w:lineRule="auto"/>')


def _caption(kind: str, pid: str, text: str = "Caption") -> str:
    return (
        f'<w:p w14:paraId="{pid}">'
        f'<w:fldSimple w:instr=" SEQ {kind} \\* ARABIC ">'
        '<w:r><w:t>1</w:t></w:r></w:fldSimple>'
        f'<w:r><w:t xml:space="preserve">. {text}</w:t></w:r></w:p>'
    )


def _figure(pid: str = "IMG000001") -> str:
    return (
        f'<w:p w14:paraId="{pid}"><w:r><w:drawing>'
        '<a:blip r:embed="rId1"/>'
        '</w:drawing></w:r></w:p>'
    )


def _table() -> str:
    return '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>data</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'


def test_mst_profile_accepts_double_plus_single_above_and_three_single_below(tmp_path):
    body = (
        _p("before", "P0000001")
        + _blank(480)
        + _blank(240)
        + _figure()
        + _caption("Figure", "CAP000001")
        + _blank(240)
        + _blank(240)
        + _blank(240)
        + _p("after", "P0000002")
    )
    path = _write_docx(tmp_path, body)

    result = docs_intel.audit_figure_table_spacing(
        path, {"figure_table_spacing_profile": "mst_thesis"}
    )

    assert result["status"] == "ok"
    assert result["object_count"] == 1
    assert result["paired_display_count"] == 1
    assert result["findings"] == []


def test_mst_profile_flags_small_gap_but_keeps_it_as_warning(tmp_path):
    body = _p("before", "P0000001") + _figure() + _caption("Figure", "CAP000001") + _p("after", "P0000002")
    path = _write_docx(tmp_path, body)

    result = docs_intel.audit_figure_table_spacing(
        path, {"figure_table_spacing_profile": "mst_thesis"}
    )

    findings = [f for f in result["findings"] if f["type"] == "figure_table_spacing_too_small"]
    assert len(findings) == 2
    assert {f["side"] for f in findings} == {"before", "after"}
    assert {f["severity"] for f in findings} == {"warning"}


def test_mst_profile_flags_gap_above_preferred_range(tmp_path):
    body = (
        _p("before", "P0000001") + _blank(240) * 5 + _figure()
        + _caption("Figure", "CAP000001") + _p("after", "P0000002")
    )
    path = _write_docx(tmp_path, body)

    result = docs_intel.audit_figure_table_spacing(
        path, {"figure_table_spacing_profile": "mst_thesis"}
    )

    above = [f for f in result["findings"] if f["type"] == "figure_table_spacing_above_preferred"]
    assert len(above) == 1
    assert above[0]["side"] == "before"
    assert above[0]["actual_single_line_equivalents"] == 5.0


def test_table_caption_above_table_is_measured_on_both_sides(tmp_path):
    body = (
        _p("before", "P0000001") + _blank(240) * 3
        + _caption("Table", "CAP000001") + _table()
        + _blank(240) * 3 + _p("after", "P0000002")
    )
    path = _write_docx(tmp_path, body)

    result = docs_intel.audit_figure_table_spacing(
        path, {"figure_table_spacing_profile": "mst_thesis"}
    )

    assert result["object_count"] == 1
    assert result["paired_display_count"] == 1
    assert result["findings"] == []


def test_asce_profile_checks_explicit_figure_caption_line_spacing(tmp_path):
    single_caption = (
        '<w:p w14:paraId="CAP000001"><w:pPr>'
        '<w:spacing w:line="240" w:lineRule="auto"/>'
        '</w:pPr><w:fldSimple w:instr=" SEQ Figure \\* ARABIC ">'
        '<w:r><w:t>1</w:t></w:r></w:fldSimple></w:p>'
    )
    body = _figure() + single_caption
    path = _write_docx(tmp_path, body)

    result = docs_intel.audit_figure_table_spacing(
        path, {"figure_table_spacing_profile": "asce_manuscript"}
    )

    assert result["findings_by_type"] == {"caption_line_spacing_mismatch": 1}
    assert result["findings"][0]["expected_line_spacing"] == "double"


def test_spacing_uses_docdefaults_for_blank_paragraphs(tmp_path):
    styles_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="{_W}">
  <w:docDefaults><w:pPrDefault><w:pPr>
    <w:spacing w:line="480" w:lineRule="auto"/>
  </w:pPr></w:pPrDefault></w:docDefaults>
</w:styles>'''
    # The blank paragraphs have no direct spacing.  Their effective line
    # height is therefore double-spaced through docDefaults, not single.
    body = (
        _p("before", "P0000001") + _p() + _figure()
        + _caption("Figure", "CAP000001") + _p() + _p("after", "P0000002")
    )
    path = _write_docx(tmp_path, body, styles_xml=styles_xml)

    result = docs_intel.audit_figure_table_spacing(
        path, {"figure_table_spacing_profile": "mst_thesis"}
    )

    before = next(f for f in result["findings"] if f["side"] == "before")
    after = next(f for f in result["findings"] if f["side"] == "after")
    assert before["actual_single_line_equivalents"] == 2.0
    assert after["actual_single_line_equivalents"] == 2.0


def test_spacing_uses_builtin_normal_style_when_paragraph_has_no_pstyle(tmp_path):
    styles_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="{_W}">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
    <w:pPr><w:spacing w:line="480" w:lineRule="auto"/></w:pPr>
  </w:style>
</w:styles>'''
    body = (
        _p("before", "P0000001") + _p() + _figure()
        + _caption("Figure", "CAP000001") + _p() + _p("after", "P0000002")
    )
    path = _write_docx(tmp_path, body, styles_xml=styles_xml)

    result = docs_intel.audit_figure_table_spacing(
        path, {"figure_table_spacing_profile": "mst_thesis"}
    )

    before = next(f for f in result["findings"] if f["side"] == "before")
    after = next(f for f in result["findings"] if f["side"] == "after")
    assert before["actual_single_line_equivalents"] == 2.0
    assert after["actual_single_line_equivalents"] == 2.0


def test_asce_profile_accepts_caption_spacing_in_docdefaults(tmp_path):
    styles_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="{_W}">
  <w:docDefaults><w:pPrDefault><w:pPr>
    <w:spacing w:line="480" w:lineRule="auto"/>
  </w:pPr></w:pPrDefault></w:docDefaults>
</w:styles>'''
    path = _write_docx(
        tmp_path,
        _figure() + _caption("Figure", "CAP000001"),
        styles_xml=styles_xml,
    )

    result = docs_intel.audit_figure_table_spacing(
        path, {"figure_table_spacing_profile": "asce_manuscript"}
    )

    assert result["findings_by_type"] == {}


def test_jcshm_profile_does_not_invent_whitespace_requirement(tmp_path):
    body = _p("before", "P0000001") + _figure() + _caption("Figure", "CAP000001") + _p("after", "P0000002")
    path = _write_docx(tmp_path, body)

    result = docs_intel.audit_figure_table_spacing(
        path, {"figure_table_spacing_profile": "jcshm_springer"}
    )

    assert result["findings"] == []
    assert result["profile_definition"]["enforcement"] == "structural_only"
    assert result["measurement"]["page_geometry_checked"] is False


def test_none_profile_is_safe_default_and_does_not_scan_spacing(tmp_path):
    path = _write_docx(tmp_path, _p("before", "P0000001") + _figure())

    result = docs_intel.audit_figure_table_spacing(path)

    assert result["status"] == "ok"
    assert result["enabled"] is False
    assert result["findings"] == []


def test_unknown_spacing_profile_is_rejected_before_reading_the_docx(tmp_path):
    path = _write_docx(tmp_path, _p("text", "P0000001"))

    result = docs_intel.audit_figure_table_spacing(
        path, {"figure_table_spacing_profile": "not_a_real_venue"}
    )

    assert "error" in result
    assert "figure_table_spacing_profile" in result["error"]


def test_custom_spacing_profile_is_supported_without_new_detector_code(tmp_path):
    body = _p("before", "P0000001") + _blank(240) + _figure() + _caption("Figure", "CAP000001") + _blank(240) + _p("after", "P0000002")
    path = _write_docx(tmp_path, body)

    result = docs_intel.audit_figure_table_spacing(
        path,
        {
            "figure_table_spacing_profile": {
                "name": "example_journal",
                "description": "One blank line on either side.",
                "enforcement": "minimum",
                "min_gap_single_lines": 1,
                "gap_sides": ["before", "after"],
                "source_urls": ["https://example.org/author-guide"],
            }
        },
    )

    assert result["status"] == "ok"
    assert result["profile"] == "example_journal"
    assert result["findings"] == []


def test_document_review_surfaces_spacing_findings_with_caption_locator(tmp_path):
    body = _p("before", "P0000001") + _figure() + _caption("Figure", "CAP000001") + _p("after", "P0000002")
    path = _write_docx(tmp_path, body)

    result = docs_intel.build_document_review(
        path,
        style_policy={
            "figure_table_spacing_profile": "mst_thesis",
            "equation_punctuation_required": False,
        },
    )

    spacing_findings = [
        finding for finding in result["findings"]
        if finding["type"] == "figure_table_spacing_too_small"
    ]
    assert len(spacing_findings) == 2
    assert all(finding["category"] == "caption" for finding in spacing_findings)
    assert all(
        finding["locator"]["target_para_id"] == "CAP000001"
        for finding in spacing_findings
    )
    assert result["figure_table_spacing_audit"]["profile"] == "mst_thesis"


def test_page_break_side_is_not_falsely_classified_as_too_much_or_too_little(tmp_path):
    body = (
        _p("before", "P0000001")
        + _p(extra='<w:r><w:br w:type="page"/></w:r>')
        + _figure()
        + _caption("Figure", "CAP000001")
        + _p("after", "P0000002")
    )
    path = _write_docx(tmp_path, body)

    result = docs_intel.audit_figure_table_spacing(
        path, {"figure_table_spacing_profile": "mst_thesis"}
    )

    assert not any(
        f["type"] == "figure_table_spacing_too_small" and f["side"] == "before"
        for f in result["findings"]
    )


def test_preflight_includes_active_figure_table_audit(tmp_path):
    body = _p("before", "P0000001") + _figure() + _caption("Figure", "CAP000001") + _p("after", "P0000002")
    path = _write_docx(tmp_path, body)

    result = docs_intel.preflight_document(
        path, {"figure_table_spacing_profile": "mst_thesis", "equation_punctuation_required": False}
    )

    assert result["status"] == "needs_review"
    assert result["ready_for_render"] is False
    assert result["figure_table_audit"]["finding_count"] == 2
