"""Contract checks for the shared drafting text model (sprint item 07c85328).

Keeps three things in agreement: the mapping table in
docs/drafting/text-model.md, the kind enum in the golden fixture, and the
golden cases themselves. Every golden block must validate against the
documented field contract, and the golden set must cover docx, latex and gdocs.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = ROOT / "docs" / "drafting" / "text-model.md"
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "drafting" / "text_model_contract.json"

TABLE_START = "<!-- kind-table:start -->"
TABLE_END = "<!-- kind-table:end -->"

# (source, kind) pairs the item acceptance requires the doc to map.
REQUIRED_MAPPING = {
    ("docx", "heading"),
    ("docx", "paragraph"),
    ("latex", "heading"),
    ("latex", "figure"),
    ("latex", "table"),
    ("latex", "equation"),
    ("latex", "citation"),
    ("gdocs", "heading"),
    ("gdocs", "paragraph"),
    ("gdocs", "table"),
    ("gdocs", "table_of_contents"),
    ("gdocs", "section_break"),
    ("gdocs", "unsupported"),
}

SKIP_KINDS = {"table_of_contents", "section_break", "unsupported"}
BLOCK_FIELDS = {"id", "kind", "text", "runs", "section_type", "skip", "source", "order"}
RUN_FIELDS = {"text", "bold", "italic"}


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _doc_text() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def _doc_table_rows() -> list[tuple[str, str, str]]:
    text = _doc_text()
    assert TABLE_START in text and TABLE_END in text, "kind-table markers missing from doc"
    block = text.split(TABLE_START, 1)[1].split(TABLE_END, 1)[0]
    rows: list[tuple[str, str, str]] = []
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells[0] == "Kind" or set(cells[0]) <= {"-", ":"}:
            continue
        assert len(cells) == 3, f"mapping row must have kind, source and construct: {line}"
        rows.append((cells[0], cells[1], cells[2]))
    return rows


def _doc_kinds() -> set[str]:
    return {kind for kind, _source, _construct in _doc_table_rows()}


CASES = _load_fixture()["cases"]


def test_doc_table_kinds_match_json_enum():
    enum = _load_fixture()["kinds"]
    assert len(enum) == len(set(enum)), "duplicate kind in the JSON enum"
    doc_kinds = _doc_kinds()
    missing_from_enum = doc_kinds - set(enum)
    missing_from_doc = set(enum) - doc_kinds
    assert not missing_from_enum, f"documented kinds missing from JSON enum: {sorted(missing_from_enum)}"
    assert not missing_from_doc, f"JSON enum kinds not documented: {sorted(missing_from_doc)}"


def test_doc_source_mapping_matches_acceptance():
    pairs = [(source, kind) for kind, source, _construct in _doc_table_rows()]
    assert len(pairs) == len(set(pairs)), "duplicate (source, kind) row in the mapping table"
    assert set(pairs) == REQUIRED_MAPPING


def test_golden_block_kinds_are_in_enum():
    fixture = _load_fixture()
    enum = set(fixture["kinds"])
    used = {block["kind"] for case in fixture["cases"] for block in case["expected_blocks"]}
    undefined = used - enum
    assert not undefined, f"golden cases use kinds the enum does not define: {sorted(undefined)}"
    undocumented = used - _doc_kinds()
    assert not undocumented, f"golden cases use kinds the doc does not document: {sorted(undocumented)}"


def test_golden_cases_cover_each_source():
    fixture = _load_fixture()
    assert len(fixture["cases"]) >= 3
    case_sources = {case["source"] for case in fixture["cases"]}
    assert case_sources == set(fixture["sources"]) == {"docx", "latex", "gdocs"}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_golden_case_blocks_validate(case):
    fixture = _load_fixture()
    enum = set(fixture["kinds"])
    section_types = set(fixture["section_types"])
    sources = set(fixture["sources"])
    blocks = case["expected_blocks"]
    assert case["source"] in sources
    assert blocks, "golden case must list at least one block"

    ids = [block["id"] for block in blocks]
    assert len(ids) == len(set(ids)), "block ids must be unique within a case"

    for index, block in enumerate(blocks):
        where = f"{case['name']}[{index}]"
        assert set(block) == BLOCK_FIELDS, f"{where}: fields must be exactly {sorted(BLOCK_FIELDS)}"
        assert block["kind"] in enum, f"{where}: kind {block['kind']!r} not in enum"
        assert block["section_type"] in section_types, f"{where}: bad section_type"
        assert block["source"] == case["source"], f"{where}: source must match the case source"
        assert isinstance(block["id"], str) and block["id"].startswith(f"{case['source']}:")
        assert len(block["id"]) > len(case["source"]) + 1, f"{where}: id has no opaque part"
        assert isinstance(block["order"], int) and not isinstance(block["order"], bool)
        assert block["order"] == index, f"{where}: order must be contiguous from 0"
        assert isinstance(block["skip"], bool), f"{where}: skip must be boolean"
        assert block["skip"] == (block["kind"] in SKIP_KINDS), f"{where}: skip does not match kind"

        text = block["text"]
        assert isinstance(text, str), f"{where}: text must be a string"
        assert unicodedata.normalize("NFC", text) == text, f"{where}: text must be NFC"
        assert text == " ".join(text.split()), f"{where}: text must be whitespace-normalized and trimmed"

        if block["skip"]:
            assert text == "", f"{where}: skip block must have empty text"
            assert block["runs"] == [], f"{where}: skip block must have no runs"
            continue

        runs = block["runs"]
        assert isinstance(runs, list) and runs, f"{where}: non-skip block needs at least one run"
        for run in runs:
            assert set(run) == RUN_FIELDS, f"{where}: run fields must be exactly {sorted(RUN_FIELDS)}"
            assert isinstance(run["text"], str)
            assert isinstance(run["bold"], bool) and isinstance(run["italic"], bool)
        assert "".join(run["text"] for run in runs) == text, f"{where}: runs must concatenate to text"
        for left, right in zip(runs, runs[1:]):
            same_format = left["bold"] == right["bold"] and left["italic"] == right["italic"]
            assert not same_format, f"{where}: adjacent runs with equal formatting must be merged"


def test_doc_documents_every_block_field():
    text = _doc_text()
    for field in sorted(BLOCK_FIELDS):
        assert f"`{field}`" in text, f"block field {field!r} is not documented"


def test_doc_states_cross_format_identity_rule():
    text = _doc_text()
    assert "Cross-format identity is not id equality" in text
    assert "matches blocks by normalized text and position" in text
