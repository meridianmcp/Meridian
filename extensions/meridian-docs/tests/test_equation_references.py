"""Focused tests for raw document.xml equation-reference extraction."""
from __future__ import annotations

import json

from meridian_docs.equation_references import extract_equation_references


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"


def _document(*body_children: str) -> bytes:
    return (
        f'<w:document xmlns:w="{W}" xmlns:w14="{W14}" xmlns:m="{M}">'
        f"<w:body>{''.join(body_children)}<w:sectPr/></w:body></w:document>"
    ).encode("utf-8")


def _paragraph(*children: str, para_id: str = "P1") -> str:
    return f'<w:p w14:paraId="{para_id}">{''.join(children)}</w:p>'


def _text(value: str) -> str:
    return f"<w:r><w:t>{value}</w:t></w:r>"


def test_extracts_simple_and_split_complex_ref_seq_fields_and_bookmarks():
    xml = _document(
        _paragraph(
            '<w:bookmarkStart w:id="7" w:name="_Ref42"/>',
            _text("Equation "),
            '<w:fldSimple w:instr=" SEQ Equation \\* ARABIC "><w:r><w:t>3</w:t></w:r></w:fldSimple>',
            '<w:bookmarkEnd w:id="7"/>',
            para_id="CAP1",
        ),
        _paragraph(
            _text("See "),
            '<w:r><w:fldChar w:fldCharType="begin"/></w:r>',
            '<w:r><w:instrText xml:space="preserve"> REF _Ref</w:instrText></w:r>',
            '<w:r><w:instrText xml:space="preserve">42 \\h</w:instrText></w:r>',
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>',
            _text("Equation 3"),
            '<w:r><w:fldChar w:fldCharType="end"/></w:r>',
            _text(" for details."),
            para_id="REF1",
        ),
    )

    result = extract_equation_references(xml)

    assert "error" not in result
    assert result["mutation"] is False
    fields = [record for record in result["records"] if record["kind"] == "field"]
    assert [(field["field_type"], field["instruction"]) for field in fields] == [
        ("SEQ", r"SEQ Equation \* ARABIC"),
        ("REF", r"REF _Ref42 \h"),
    ]
    assert fields[0]["sequence_identifier"] == "Equation"
    assert fields[0]["visible_text"] == "3"
    assert fields[1]["bookmark_name"] == "_Ref42"
    assert fields[1]["visible_text"] == "Equation 3"
    assert all(field["confidence"] == "high" for field in fields)
    assert all(field["source"] == "ooxml_field_instruction" for field in fields)

    bookmarks = [
        record for record in result["records"] if record["kind"] == "bookmark"
    ]
    assert [(bookmark["bookmark_name"], bookmark["bookmark_id"]) for bookmark in bookmarks] == [
        ("_Ref42", "7")
    ]


def test_extracts_only_conservative_visible_equation_text():
    xml = _document(
        _paragraph(
            _text("See Equation (1), Eq. 2, and Equations numbers [A3]."),
            para_id="PROSE1",
        ),
        _paragraph(
            '<m:oMath><m:r><m:t>Equation (99)</m:t></m:r></m:oMath>',
            para_id="MATH1",
        ),
        _paragraph(
            '<w:fldSimple w:instr=" TOC \\o &quot;1-3&quot;">'
            '<w:r><w:t>Equation (88)</w:t></w:r></w:fldSimple>',
            para_id="TOC1",
        ),
    )

    result = extract_equation_references(xml)

    visible = [record for record in result["records"] if record["kind"] == "visible_text"]
    assert [record["matched_text"] for record in visible] == [
        "Equation (1)",
        "Eq. 2",
        "Equations numbers [A3]",
    ]
    assert [record["reference_number"] for record in visible] == ["1", "2", "A3"]
    assert all(record["confidence"] == "lexical" for record in visible)
    assert all(record["source"] == "visible_word_text" for record in visible)
    # OMML m:t is not ordinary visible Word w:t text, and unsupported fields
    # are masked rather than guessed as prose references.
    assert all(record["paragraph_id"] == "PROSE1" for record in visible)


def test_result_is_deterministic_json_safe_and_does_not_mutate_bytearray():
    original = bytearray(
        _document(_paragraph(_text("Equation (4)"), para_id="P4"))
    )

    first = extract_equation_references(original)
    second = extract_equation_references(bytes(original))

    assert original == bytearray(_document(_paragraph(_text("Equation (4)"), para_id="P4")))
    assert first == second
    assert json.dumps(first, sort_keys=True, ensure_ascii=False)
    assert [record["record_index"] for record in first["records"]] == [0]


def test_malformed_or_wrong_input_returns_json_safe_error_without_reading_a_path():
    malformed = extract_equation_references(b"<w:document>")
    assert malformed["records"] == []
    assert malformed["error_type"] == "xml_invalid"
    assert malformed["mutation"] is False

    wrong_root = extract_equation_references("<root />")
    assert wrong_root["error_type"] == "xml_invalid"

    wrong_type = extract_equation_references(42)  # type: ignore[arg-type]
    assert wrong_type["error_type"] == "input_invalid"
    assert wrong_type["records"] == []
