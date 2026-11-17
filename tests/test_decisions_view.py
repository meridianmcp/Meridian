"""list_decisions view: which HITL rows count as open decisions, and how they read."""

from __future__ import annotations

import json

from meridian.decisions_view import (
    decision_view,
    is_open_decision,
    open_decisions,
)


def _row(**overrides):
    base = {
        "id": "r1",
        "project_id": "p1",
        "kind": "question",
        "status": "pending",
        "question": "Which way?",
        "context": "ctx",
        "urgency": "high",
        "created_at": "2026-11-10 00:00:00",
        "payload": json.dumps({"options": ["A", "B"], "recommended": 0}),
    }
    base.update(overrides)
    return base


def test_options_are_numbered_from_one_and_parsed_from_json_text():
    view = decision_view(_row())
    assert view["options"] == [{"number": 1, "label": "A"}, {"number": 2, "label": "B"}]


def test_recommended_index_is_zero_based_in_the_payload_and_one_based_in_the_view():
    assert decision_view(_row())["recommended"] == 1
    rec_second = _row(payload=json.dumps({"options": ["A", "B"], "recommended": 1}))
    assert decision_view(rec_second)["recommended"] == 2


def test_recommended_option_text_is_matched_to_its_number():
    row = _row(payload=json.dumps({"options": ["Env only", "Keep chain"], "recommended": "Keep chain"}))
    assert decision_view(row)["recommended"] == 2


def test_recommended_that_matches_nothing_is_none():
    out_of_range = _row(payload=json.dumps({"options": ["A"], "recommended": 5}))
    unknown_text = _row(payload=json.dumps({"options": ["A"], "recommended": "Z"}))
    boolean = _row(payload=json.dumps({"options": ["A"], "recommended": True}))
    assert decision_view(out_of_range)["recommended"] is None
    assert decision_view(unknown_text)["recommended"] is None
    assert decision_view(boolean)["recommended"] is None


def test_blank_options_are_dropped_and_numbering_follows_what_remains():
    row = _row(payload=json.dumps({"options": ["  ", "A", "", "B"]}))
    assert [o["label"] for o in decision_view(row)["options"]] == ["A", "B"]


def test_require_human_is_read_from_the_payload_and_defaults_to_false():
    assert decision_view(_row())["require_human"] is False
    flagged = _row(payload=json.dumps({"options": ["A"], "require_human": True}))
    assert decision_view(flagged)["require_human"] is True


def test_payload_as_a_dict_is_accepted_and_bad_json_reads_as_no_options():
    dict_payload = _row(payload={"options": ["A"]})
    assert [o["label"] for o in decision_view(dict_payload)["options"]] == ["A"]
    broken = _row(payload="{not json")
    assert decision_view(broken)["options"] == []
    not_an_object = _row(payload="[1, 2]")
    assert decision_view(not_an_object)["options"] == []


def test_non_list_options_read_as_none():
    row = _row(payload=json.dumps({"options": "A,B"}))
    assert decision_view(row)["options"] == []


def test_missing_urgency_defaults_to_normal():
    assert decision_view(_row(urgency=None))["urgency"] == "normal"


def test_only_pending_questions_with_options_are_open_decisions():
    assert is_open_decision(_row())
    assert not is_open_decision(_row(kind="correction"))
    assert not is_open_decision(_row(status="answered"))
    assert not is_open_decision(_row(payload=json.dumps({"options": []})))
    assert not is_open_decision(_row(payload=None))


def test_a_row_without_a_kind_is_treated_as_a_question():
    assert is_open_decision(_row(kind=None))


def test_open_decisions_sort_by_urgency_then_oldest_first_and_count_matches():
    rows = [
        _row(id="normal-new", urgency="normal", created_at="2026-11-10 03:00:00"),
        _row(id="high", urgency="high", created_at="2026-11-10 02:00:00"),
        _row(id="blocking", urgency="blocking", created_at="2026-11-10 04:00:00"),
        _row(id="normal-old", urgency="normal", created_at="2026-11-10 01:00:00"),
        _row(id="unknown", urgency="weird", created_at="2026-11-10 00:00:00"),
        _row(id="answered", status="answered"),
        _row(id="no-options", payload=json.dumps({"options": []})),
    ]
    out = open_decisions(rows)
    assert out["count"] == 5
    assert [d["id"] for d in out["decisions"]] == [
        "blocking", "high", "normal-old", "normal-new", "unknown",
    ]


def test_open_decisions_on_no_rows_is_an_empty_list():
    assert open_decisions([]) == {"count": 0, "decisions": []}
