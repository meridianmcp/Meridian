"""Request splitter: a pasted dump becomes one item per request."""

from __future__ import annotations

import pytest

from meridian.request_splitter import split_request_text


def _titles(text):
    return [item["title"] for item in split_request_text(text)]


def test_numbered_lines_become_one_item_each_and_keep_the_marker_in_notes():
    items = split_request_text("1. Fix login\n2. Add export\n3. Write docs")
    assert [i["title"] for i in items] == ["Fix login", "Add export", "Write docs"]
    assert [i["notes"] for i in items] == ["1. Fix login", "2. Add export", "3. Write docs"]


def test_closing_parenthesis_numbering_is_a_list():
    assert _titles("1) Fix login\n2) Add export") == ["Fix login", "Add export"]


def test_bulleted_lines_with_each_marker_become_one_item_each():
    items = split_request_text("- alpha\n* beta\n+ gamma\n• delta")
    assert [i["title"] for i in items] == ["alpha", "beta", "gamma", "delta"]
    assert [i["notes"] for i in items] == ["- alpha", "* beta", "+ gamma", "• delta"]


def test_mixed_intro_numbered_bulleted_and_trailing_paragraph():
    text = "Please do these:\n1. Fix login\n- Add export\n\nAlso, rename the flag."
    items = split_request_text(text)
    assert [i["title"] for i in items] == [
        "Please do these:",
        "Fix login",
        "Add export",
        "Also, rename the flag.",
    ]
    assert items[1]["notes"] == "1. Fix login"


def test_plain_paragraph_is_one_item_with_its_original_lines_in_notes():
    text = "Make the dashboard faster.\nIt is slow on mobile."
    assert split_request_text(text) == [
        {
            "title": "Make the dashboard faster. It is slow on mobile.",
            "notes": "Make the dashboard faster.\nIt is slow on mobile.",
        }
    ]


def test_blank_line_separates_paragraphs_into_separate_items():
    assert _titles("First request.\n\nSecond request.") == ["First request.", "Second request."]


def test_wrapped_continuation_stays_with_its_list_item():
    items = split_request_text("1. Fix login\n   on Safari too\n2. Add export")
    assert [i["title"] for i in items] == ["Fix login", "Add export"]
    assert items[0]["notes"] == "1. Fix login\n   on Safari too"


def test_blank_line_ends_a_list_item_so_following_text_is_its_own_item():
    assert _titles("1. Fix login\n\nSeparate thought.") == ["Fix login", "Separate thought."]


def test_text_directly_before_a_list_is_its_own_item():
    assert _titles("Intro line\n1. Fix login") == ["Intro line", "Fix login"]


def test_nested_bullets_each_count_as_an_item():
    items = split_request_text("- parent\n  - child")
    assert [i["title"] for i in items] == ["parent", "child"]
    assert items[1]["notes"] == "- child"


def test_numbers_and_dashes_inside_prose_are_not_list_markers():
    assert _titles("2024 was a good year.") == ["2024 was a good year."]
    assert _titles("3.5 GB of RAM") == ["3.5 GB of RAM"]
    assert _titles("-5 degrees is cold") == ["-5 degrees is cold"]


def test_bare_markers_file_nothing():
    assert _titles("-\n1.\n1. Real request") == ["Real request"]


def test_horizontal_rules_file_nothing_and_end_the_item_before_them():
    assert _titles("---\n1. Real request") == ["Real request"]
    assert _titles("1. a\n---\nb") == ["a", "b"]


def test_whitespace_is_collapsed_in_titles_but_notes_keep_the_text():
    items = split_request_text("1.   Fix   login  \n")
    assert items == [{"title": "Fix login", "notes": "1.   Fix   login"}]


def test_windows_line_endings_are_handled():
    assert _titles("1. Fix login\r\n2. Add export\r\n") == ["Fix login", "Add export"]


def test_every_item_has_exactly_a_title_and_notes():
    for item in split_request_text("1. a\n\nb"):
        assert set(item) == {"title", "notes"}


@pytest.mark.parametrize("text", ["", "   ", "\n\t\n  \n", None])
def test_empty_or_whitespace_input_returns_no_items(text):
    assert split_request_text(text) == []
