"""Opt-in run list for docparse ``parse_docx`` (sprint item draft-docparse-run-model).

Pins the default ``parse_docx`` output to a stored golden, and checks the
``include_runs=True`` records: run texts concatenate to the paragraph text,
``bold``/``italic`` follow ``w:b``/``w:i``, field runs keep their displayed
result text, and a tab or break inside a run follows the paragraph rule.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from docparse import docs_intel

_DOC_HEAD = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"><w:body>'
)
_DOC_TAIL = "</w:body></w:document>"

_FIXTURE_PARAGRAPHS = (
    '<w:p w14:paraId="1A000001"><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
    "<w:r><w:t>Chapter One</w:t></w:r></w:p>"
    '<w:p w14:paraId="1A000002"><w:r><w:t xml:space="preserve">Alpha </w:t></w:r>'
    "<w:r><w:rPr><w:b/></w:rPr><w:t>Bold</w:t></w:r>"
    "<w:r><w:rPr><w:i/></w:rPr><w:t xml:space=\"preserve\"> Ital</w:t></w:r>"
    '<w:r><w:rPr><w:b w:val="0"/><w:i w:val="false"/></w:rPr>'
    '<w:t xml:space="preserve"> off</w:t></w:r></w:p>'
    '<w:p w14:paraId="1A000003"><w:r><w:t>(a)</w:t><w:tab/><w:t>some</w:t>'
    "<w:br/><w:t>text</w:t></w:r></w:p>"
    '<w:p w14:paraId="1A000004"><w:r><w:t xml:space="preserve">See </w:t></w:r>'
    '<w:fldSimple w:instr=" REF _Ref1 \\h "><w:r><w:t>Figure 2</w:t></w:r></w:fldSimple>'
    "<w:r><w:t>.</w:t></w:r></w:p>"
    '<w:p w14:paraId="1A000005"><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
    '<w:r><w:instrText xml:space="preserve"> SEQ Figure \\* ARABIC </w:instrText></w:r>'
    '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
    "<w:r><w:rPr><w:noProof/></w:rPr><w:t>3</w:t></w:r>"
    '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
    '<w:r><w:t xml:space="preserve">: Results</w:t></w:r></w:p>'
    '<w:p w14:paraId="1A000006"><w:hyperlink w:anchor="_Toc1">'
    "<w:r><w:rPr><w:b/></w:rPr><w:t>Linked</w:t></w:r></w:hyperlink></w:p>"
    '<w:p w14:paraId="1A000007"><w:r><w:t>Kept</w:t></w:r>'
    '<w:del w:id="1" w:author="Tester" w:date="2026-01-01T00:00:00Z">'
    "<w:r><w:delText>gone</w:delText></w:r></w:del></w:p>"
    '<w:p w14:paraId="1A000008"/>'
)
_FIXTURE_XML = _DOC_HEAD + _FIXTURE_PARAGRAPHS + _DOC_TAIL


def _docx(xml: str) -> bytes:
    """Wrap raw document XML in a .docx ZIP (word/document.xml only)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


def _as_tuples(runs: list[dict]) -> list[tuple]:
    return [(r["text"], r["bold"], r["italic"], r["field"]) for r in runs]


# Stored golden for the default output on _FIXTURE_XML. Captured from the
# pre-change parse_docx on origin/dev, so it proves the default is unchanged.
_GOLDEN_DEFAULT = [
    {
        "index": 0,
        "para_id": "1A000001",
        "style": "Heading1",
        "text": "Chapter One",
        "kind": "heading",
        "fields": [],
    },
    {
        "index": 1,
        "para_id": "1A000002",
        "style": None,
        "text": "Alpha Bold Ital off",
        "kind": "paragraph",
        "fields": [],
    },
    {
        "index": 2,
        "para_id": "1A000003",
        "style": None,
        "text": "(a)\tsome\ntext",
        "kind": "paragraph",
        "fields": [],
    },
    {
        "index": 3,
        "para_id": "1A000004",
        "style": None,
        "text": "See Figure 2.",
        "kind": "paragraph",
        "fields": [
            {
                "kind": "field",
                "field_type": "REF",
                "instruction": "REF _Ref1 \\h",
                "needs_refresh": True,
                "cached_result": "Figure 2",
            }
        ],
    },
    {
        "index": 4,
        "para_id": "1A000005",
        "style": None,
        "text": "3: Results",
        "kind": "paragraph",
        "fields": [
            {
                "kind": "field",
                "field_type": "SEQ",
                "instruction": "SEQ Figure \\* ARABIC",
                "needs_refresh": True,
                "cached_result": "3",
            }
        ],
    },
    {
        "index": 5,
        "para_id": "1A000006",
        "style": None,
        "text": "Linked",
        "kind": "paragraph",
        "fields": [],
    },
    {
        "index": 6,
        "para_id": "1A000007",
        "style": None,
        "text": "Kept",
        "kind": "paragraph",
        "fields": [],
    },
    {
        "index": 7,
        "para_id": "1A000008",
        "style": None,
        "text": "",
        "kind": "paragraph",
        "fields": [],
    },
]

# (text, bold, italic, field) per paragraph index, for _FIXTURE_XML.
_EXPECTED_RUNS = {
    0: [("Chapter One", False, False, False)],
    1: [
        ("Alpha ", False, False, False),
        ("Bold", True, False, False),
        (" Ital", False, True, False),
        (" off", False, False, False),
    ],
    2: [("(a)\tsome\ntext", False, False, False)],
    3: [
        ("See ", False, False, False),
        ("Figure 2", False, False, True),
        (".", False, False, False),
    ],
    4: [("3", False, False, True), (": Results", False, False, False)],
    5: [("Linked", True, False, False)],
    6: [("Kept", False, False, False)],
    7: [],
}


def test_default_output_matches_golden():
    assert docs_intel.parse_docx(_docx(_FIXTURE_XML)) == _GOLDEN_DEFAULT


def test_default_records_carry_no_runs_key():
    for record in docs_intel.parse_docx(_docx(_FIXTURE_XML)):
        assert "runs" not in record


def test_include_runs_false_equals_default():
    data = _docx(_FIXTURE_XML)
    assert docs_intel.parse_docx(data, include_runs=False) == docs_intel.parse_docx(data)


def test_include_runs_adds_runs_and_leaves_other_keys_unchanged():
    records = docs_intel.parse_docx(_docx(_FIXTURE_XML), include_runs=True)
    assert [{k: v for k, v in r.items() if k != "runs"} for r in records] == _GOLDEN_DEFAULT
    for record in records:
        assert isinstance(record["runs"], list)
        for run in record["runs"]:
            assert set(run) == {"text", "bold", "italic", "field"}


def test_include_runs_is_keyword_only():
    with pytest.raises(TypeError):
        docs_intel.parse_docx(_docx(_FIXTURE_XML), True)


def test_run_texts_concatenate_to_paragraph_text():
    for record in docs_intel.parse_docx(_docx(_FIXTURE_XML), include_runs=True):
        assert "".join(run["text"] for run in record["runs"]) == record["text"]


def test_runs_match_expected_per_paragraph():
    records = docs_intel.parse_docx(_docx(_FIXTURE_XML), include_runs=True)
    assert {r["index"]: _as_tuples(r["runs"]) for r in records} == _EXPECTED_RUNS


def test_bold_and_italic_follow_w_b_and_w_i_values():
    xml = _DOC_HEAD + (
        '<w:p w14:paraId="2B000001">'
        '<w:r><w:rPr><w:b w:val="1"/></w:rPr><w:t>a</w:t></w:r>'
        '<w:r><w:rPr><w:b/><w:i w:val="0"/></w:rPr><w:t>b</w:t></w:r>'
        '<w:r><w:rPr><w:b w:val="none"/><w:i w:val="off"/></w:rPr><w:t>c</w:t></w:r>'
        "<w:r><w:rPr><w:i/></w:rPr><w:t>d</w:t></w:r>"
        "<w:r><w:t>e</w:t></w:r>"
        "</w:p>"
    ) + _DOC_TAIL
    (record,) = docs_intel.parse_docx(_docx(xml), include_runs=True)
    assert _as_tuples(record["runs"]) == [
        ("a", True, False, False),
        ("b", True, False, False),
        ("c", False, False, False),
        ("d", False, True, False),
        ("e", False, False, False),
    ]


def test_field_runs_are_marked_and_keep_displayed_result_text():
    records = docs_intel.parse_docx(_docx(_FIXTURE_XML), include_runs=True)
    default = docs_intel.parse_docx(_docx(_FIXTURE_XML))
    for idx in (3, 4):
        field_runs = [run for run in records[idx]["runs"] if run["field"]]
        cached = [f["cached_result"] for f in default[idx]["fields"]]
        assert [run["text"] for run in field_runs] == cached
    # The instruction text never reaches a run.
    all_run_text = " ".join(run["text"] for r in records for run in r["runs"])
    assert "SEQ" not in all_run_text
    assert "REF" not in all_run_text
    assert "ARABIC" not in all_run_text


def test_tab_and_break_inside_run_follow_paragraph_rule():
    records = docs_intel.parse_docx(_docx(_FIXTURE_XML), include_runs=True)
    tab_break = records[2]
    assert tab_break["text"] == "(a)\tsome\ntext"
    assert [run["text"] for run in tab_break["runs"]] == ["(a)\tsome\ntext"]


def test_hyperlink_runs_included_and_deleted_runs_excluded():
    records = docs_intel.parse_docx(_docx(_FIXTURE_XML), include_runs=True)
    assert _as_tuples(records[5]["runs"]) == [("Linked", True, False, False)]
    assert _as_tuples(records[6]["runs"]) == [("Kept", False, False, False)]


def test_empty_paragraph_has_empty_run_list():
    records = docs_intel.parse_docx(_docx(_FIXTURE_XML), include_runs=True)
    assert records[7]["runs"] == []


def test_text_box_text_is_counted_once_in_its_run():
    xml = _DOC_HEAD + (
        '<w:p w14:paraId="3C000001"><w:r><w:drawing><w:txbxContent>'
        "<w:p><w:r><w:t>Box</w:t></w:r></w:p>"
        "</w:txbxContent></w:drawing><w:t>Out</w:t></w:r></w:p>"
    ) + _DOC_TAIL
    (record,) = docs_intel.parse_docx(_docx(xml), include_runs=True)
    assert record["text"] == "BoxOut"
    assert _as_tuples(record["runs"]) == [("BoxOut", False, False, False)]


def test_stray_field_end_marker_does_not_mark_later_runs_as_field():
    xml = _DOC_HEAD + (
        '<w:p w14:paraId="4D000001"><w:r><w:fldChar w:fldCharType="end"/></w:r>'
        "<w:r><w:t>plain</w:t></w:r></w:p>"
    ) + _DOC_TAIL
    (record,) = docs_intel.parse_docx(_docx(xml), include_runs=True)
    assert _as_tuples(record["runs"]) == [("plain", False, False, False)]
