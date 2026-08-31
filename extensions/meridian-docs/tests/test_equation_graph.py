"""Regression tests for the read-only equation dependency graph."""
from __future__ import annotations

import io
import zipfile

from meridian_docs import server
from meridian_docs.equation_graph import build_equation_graph


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"


def _docx(*body_children: str) -> bytes:
    document = (
        f'<w:document xmlns:w="{W}" xmlns:w14="{W14}" xmlns:m="{M}">'
        f"<w:body>{''.join(body_children)}<w:sectPr /></w:body></w:document>"
    ).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def _math(text: str, *, display: bool = False) -> str:
    raw = f'<m:oMath><m:r><m:t>{text}</m:t></m:r></m:oMath>'
    return f"<m:oMathPara>{raw}</m:oMathPara>" if display else raw


def _paragraph(text: str = "", math: str = "", *, para_id: str = "") -> str:
    identifier = f' w14:paraId="{para_id}"' if para_id else ""
    run = f"<w:r><w:t>{text}</w:t></w:r>" if text else ""
    return f"<w:p{identifier}>{run}{math}</w:p>"


def _numbered_table(number: str, text: str, *, table_id: str = "") -> str:
    return (
        f'<w:tbl><w:tr><w:tc><w:p w14:paraId="{table_id or "TC1"}">'
        f"{_math(text)}</w:p></w:tc>"
        f'<w:tc><w:p><w:r><w:t>({number})</w:t></w:r></w:p></w:tc>'
        "</w:tr></w:tbl>"
    )


def test_graph_lists_display_inline_and_table_embedded_equations(tmp_path):
    path = tmp_path / "equations.docx"
    heading = (
        '<w:p w14:paraId="H1"><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        '<w:r><w:t>Methods</w:t></w:r></w:p>'
    )
    path.write_bytes(
        _docx(
            heading,
            _paragraph("where R is the radius.", _math("R", display=True), para_id="P1"),
            _paragraph("Inline x appears here: ", _math("x"), para_id="P2"),
            _numbered_table("2", "x", table_id="P3"),
            _paragraph("See Equation (2).", para_id="P4"),
        )
    )

    result = build_equation_graph(
        str(path),
        {"symbols": [{"symbol": "R", "role": "radius"}, {"symbol": "x"}]},
    )

    assert "error" not in result
    assert result["equation_count"] == 3
    assert result["placements"]["line_separated"]
    assert result["placements"]["inline"]
    assert result["placements"]["table_numbered"]
    display_equation = next(
        equation for equation in result["equations"] if equation["placement"] == "line_separated"
    )
    assert display_equation["section_path"] == ["Methods"]
    assert display_equation["section_path_ids"] == ["H1"]
    assert any(node["kind"] == "symbol" and node["symbol"] == "R" for node in result["nodes"])
    assert any(edge["type"] == "defines_candidate" for edge in result["edges"])
    assert any(edge["type"] == "references" and edge["reference"] == "2" for edge in result["edges"])


def test_graph_reports_duplicate_numbers_and_is_byte_stable(tmp_path):
    path = tmp_path / "duplicate.docx"
    path.write_bytes(_docx(_numbered_table("1", "a"), _numbered_table("1", "b")))

    first = build_equation_graph(str(path))
    second = build_equation_graph(str(path))

    assert first["graph_sha256"] == second["graph_sha256"]
    duplicate = [finding for finding in first["conflicts"] if finding["type"] == "duplicate_equation_number"]
    assert duplicate and duplicate[0]["number"] == "1"


def test_graph_reports_numbering_gaps_as_advisory_observations(tmp_path):
    path = tmp_path / "gap.docx"
    path.write_bytes(_docx(_numbered_table("1", "a"), _numbered_table("3", "b")))

    result = build_equation_graph(str(path))

    assert result["numbering"]["visible_numbers"] == ["1", "3"]
    gap = [item for item in result["observations"] if item["type"] == "equation_number_gap"]
    assert gap and gap[0]["missing_numbers"] == [2]
    assert result["conflict_count"] == 0


def test_graph_marks_ambiguous_reference_and_dag_state(tmp_path):
    path = tmp_path / "refs.docx"
    path.write_bytes(
        _docx(
            _paragraph("Equation (9) is discussed.", _math("Equation 9", display=True)),
            _paragraph("See Equation (99)."),
        )
    )

    result = build_equation_graph(str(path))

    assert result["dag"]["acyclic"] is True
    assert any(finding["type"] == "unresolved_equation_reference" for finding in result["conflicts"])


def test_graph_validates_explicit_equation_dependency_order(tmp_path):
    path = tmp_path / "dependency.docx"
    path.write_bytes(
        _docx(
            _numbered_table("1", "a"),
            (
                '<w:tbl><w:tr><w:tc><w:p>'
                '<w:r><w:t>See Equation (1)</w:t></w:r>'
                f"{_math('b')}"
                "</w:p></w:tc><w:tc><w:p><w:r><w:t>(2)</w:t></w:r></w:p></w:tc>"
                "</w:tr></w:tbl>"
            ),
        )
    )

    result = build_equation_graph(str(path))

    assert result["dag"]["acyclic"] is True
    assert result["dag"]["edge_count"] == 1
    dependency = [edge for edge in result["edges"] if edge["type"] == "depends_on"]
    assert len(dependency) == 1
    assert result["dag"]["ordered_equations"] == [
        dependency[0]["target"],
        dependency[0]["source"],
    ]


def test_graph_includes_word_fields_and_bookmark_reference_signals(tmp_path):
    path = tmp_path / "word-fields.docx"
    path.write_bytes(
        _docx(
            (
                '<w:p w14:paraId="CAP">'
                '<w:bookmarkStart w:id="7" w:name="_RefEquation1"/>'
                '<w:r><w:t>Equation </w:t></w:r>'
                '<w:fldSimple w:instr=" SEQ Equation \\* ARABIC ">' 
                '<w:r><w:t>1</w:t></w:r></w:fldSimple>'
                '<w:bookmarkEnd w:id="7"/>'
                f"{_math('a=b', display=True)}"
                "</w:p>"
            ),
            (
                '<w:p w14:paraId="REF">'
                '<w:r><w:t>See </w:t></w:r>'
                '<w:fldSimple w:instr=" REF _RefEquation1 \\h">'
                '<w:r><w:t>Equation 1</w:t></w:r></w:fldSimple>'
                "</w:p>"
            ),
        )
    )

    result = build_equation_graph(str(path))

    assert "error" not in result
    fields = [node for node in result["nodes"] if node["kind"] == "word_field"]
    bookmarks = [node for node in result["nodes"] if node["kind"] == "bookmark"]
    assert [(field["field_type"], field.get("sequence_identifier"), field.get("bookmark_name")) for field in fields] == [
        ("SEQ", "Equation", None),
        ("REF", None, "_RefEquation1"),
    ]
    assert len(bookmarks) == 1
    assert any(
        edge["type"] == "refers_to_bookmark"
        and edge["source"] in {field["id"] for field in fields if field["field_type"] == "REF"}
        and edge["target"] == bookmarks[0]["id"]
        for edge in result["edges"]
    )
    assert result["reference_extraction"]["record_count"] == 3
    assert result["reference_extraction"]["records"]


def test_mcp_graph_tool_is_exposed_and_read_only(tmp_path):
    path = tmp_path / "one.docx"
    path.write_bytes(_docx(_paragraph(math=_math("z", display=True))))

    result = server.build_equation_graph(str(path))

    assert result["equation_count"] == 1
    assert result["graph_sha256"]
    assert path.read_bytes() == path.read_bytes()
