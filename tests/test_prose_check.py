"""run_prose_check: one call, one flat list of flags, no score or verdict anywhere."""

import pytest

from meridian.prose import RuleConfig, run_prose_check
from meridian.prose.check import MAX_CHARS
from meridian.prose.rules import parse_rule_config

GENERAL_PROSE = (
    "In today's fast-paced world, teams delve into robust, scalable, and seamless workflows. "
    "Revenue grew significantly. "
    "We ship weekly, and the cache clears daily."
)

FORBIDDEN_KEYS = {
    "score", "verdict", "grade", "rating", "quality", "pass", "fail", "passed", "failed",
    "ai_score", "ai_probability", "is_ai", "good", "bad",
}
FORBIDDEN_VALUES = {"good", "bad", "pass", "fail", "passed", "failed", "passing", "failing"}


def _walk(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield ("key", key)
            yield from _walk(inner)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            yield from _walk(inner)
    else:
        yield ("value", value)


def test_result_is_plain_data_with_flags_sorted_by_location():
    result = run_prose_check(GENERAL_PROSE)
    assert result["preset"] == "general"
    assert result["normalizer"] == "none"
    starts = [item["start"] for item in result["flags"]]
    assert starts == sorted(starts)
    assert result["flags"], "general prose should produce flags"


def test_every_flag_has_a_status_and_is_uncalibrated_and_locates_itself():
    for item in run_prose_check(GENERAL_PROSE)["flags"]:
        assert item["status"] in {"flag", "question"}
        assert item["validation_status"] == "uncalibrated"
        assert item["sentence"] >= 1 and item["line"] >= 1
        assert item["locations"] and item["locations"][0]["start"] == item["start"]


def test_counts_by_kind_are_plain_counts_that_match_the_flag_list():
    result = run_prose_check(GENERAL_PROSE)
    expected: dict[str, int] = {}
    for item in result["flags"]:
        expected[item["kind"]] = expected.get(item["kind"], 0) + 1
    assert result["counts_by_kind"] == expected


def test_general_prose_gets_stock_phrase_triad_and_question_flags():
    result = run_prose_check(GENERAL_PROSE)
    rules = {item["evidence"] for item in result["flags"] if item["kind"] == "rule"}
    lowered = {r.lower() for r in rules}
    assert "in today's fast-paced world" in lowered
    assert "delve" in lowered and "robust" in lowered
    assert "robust, scalable, and seamless" in rules
    kinds = {item["kind"] for item in result["flags"]}
    assert "quantity_claim_without_number" in kinds


def test_no_score_verdict_or_pass_fail_key_or_value_appears_anywhere():
    result = run_prose_check(GENERAL_PROSE)
    for kind, item in _walk(result):
        if kind == "key":
            assert item not in FORBIDDEN_KEYS, item
        else:
            assert not (isinstance(item, str) and item.lower() in FORBIDDEN_VALUES), item


def test_academic_preset_drops_dash_and_triad_flags_only():
    text = "We ship fast, reliable, and cheap — delve in."
    general = {i["evidence"] for i in run_prose_check(text, preset="general")["flags"] if i["kind"] == "rule"}
    academic = {i["evidence"] for i in run_prose_check(text, preset="academic")["flags"] if i["kind"] == "rule"}
    assert "—" in general and "fast, reliable, and cheap" in general
    assert academic == {"delve"}


def test_overleaf_preset_reads_cite_as_a_source_marker_so_no_unsourced_question():
    latex = "Sales grew 40 percent \\cite{lee2024}."
    general = run_prose_check(latex, preset="general")
    overleaf = run_prose_check(latex, preset="overleaf")
    assert overleaf["normalizer"] == "latex"
    assert any(i["kind"] == "unsourced_figure" for i in general["flags"])
    assert not any(i["kind"] == "unsourced_figure" for i in overleaf["flags"])


def test_docx_preset_matches_curly_apostrophe_phrases():
    result = run_prose_check("It’s worth noting the cache clears.", preset="docx")
    assert result["normalizer"] == "docx"
    assert any(i["kind"] == "rule" and "worth noting" in i["evidence"].lower() for i in result["flags"])


def test_custom_rule_appears_as_a_custom_source_flag_for_its_presets_only():
    config = parse_rule_config(
        {"rule": [{"id": "avoid-synergy", "kind": "phrase", "pattern": "synergy", "presets": ["general"]}]}
    )
    general = run_prose_check("A synergy here.", preset="general", rule_config=config)
    academic = run_prose_check("A synergy here.", preset="academic", rule_config=config)
    assert general["custom_rules_loaded"] == 1
    assert general["custom_rules_applied"] == ["avoid-synergy"]
    assert any(i["source"] == "custom" for i in general["flags"])
    assert academic["custom_rules_applied"] == []
    assert not any(i["source"] == "custom" for i in academic["flags"])


def test_disabled_group_in_config_is_reported_as_not_applied():
    result = run_prose_check("A — B.", rule_config=RuleConfig(groups={"dash_marks": False}))
    assert "dash_marks" not in result["rule_groups"]
    assert not any(i["group"] == "dash_marks" for i in result["flags"])


def test_measures_carry_values_status_and_no_target():
    measures = run_prose_check(GENERAL_PROSE)["measures"]
    assert set(measures) == {"word_count", "sentence_count", "specificity", "lexical_diversity"}
    assert measures["word_count"]["value"] > 0
    for item in measures.values():
        assert item["validation_status"] == "uncalibrated"
        assert item["calibrated"] is False
        assert "target" not in item


def test_notes_state_that_nothing_is_changed_and_no_verdict_is_given():
    notes = " ".join(run_prose_check(GENERAL_PROSE)["notes"])
    assert "Nothing in the text is changed" in notes
    assert "nothing here is a score or a verdict" in notes


@pytest.mark.parametrize("bad", [None, 42, ["text"]])
def test_non_string_text_is_rejected(bad):
    with pytest.raises(ValueError):
        run_prose_check(bad)


def test_text_longer_than_the_limit_is_rejected():
    with pytest.raises(ValueError, match="longer than"):
        run_prose_check("a" * (MAX_CHARS + 1))


def test_unknown_preset_is_rejected():
    with pytest.raises(ValueError):
        run_prose_check("Some text.", preset="strict")


def test_empty_text_gives_an_empty_flag_list_not_an_error():
    result = run_prose_check("")
    assert result["flags"] == []
    assert result["measures"]["word_count"]["value"] == 0
