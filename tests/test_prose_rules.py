"""Built-in rule groups, and the owner's prose_rules.toml: loading, validation and matching."""

import pytest

from meridian.prose import RULE_FILE_NAME, RuleConfig, RuleFileError, load_rule_config, run_prose_check
from meridian.prose.rules import STOCK_PHRASES, parse_rule_config


def _rules(text: str, config: RuleConfig | None = None, preset: str = "general") -> list[dict]:
    result = run_prose_check(text, preset=preset, rule_config=config)
    return [item for item in result["flags"] if item["kind"] == "rule"]


def _matched(text: str, **kwargs) -> list[str]:
    return [item["evidence"] for item in _rules(text, **kwargs)]


def test_stock_phrases_match_whole_words_case_insensitively():
    assert _matched("Delve into the detail.") == ["Delve"]
    assert "delve" not in " ".join(_matched("The delivery arrived."))
    assert _matched("It’s worth noting that the cache clears.") == ["It’s worth noting"]
    assert _matched("A true game changer for teams.") == ["game changer"]


def test_every_stock_phrase_is_lower_case_and_unique():
    assert len(set(STOCK_PHRASES)) == len(STOCK_PHRASES)
    assert all(phrase == phrase.lower() for phrase in STOCK_PHRASES)


def test_dash_marks_list_each_em_en_and_double_hyphen():
    matches = _matched("A — B -- C – D.")
    assert matches == ["—", "--", "–"]


def test_three_item_list_is_flagged_as_a_candidate():
    assert _matched("We ship fast, reliable, and cheap.") == ["fast, reliable, and cheap"]
    assert _matched("Pick red, blue or green.") == ["red, blue or green"]


def test_disable_phrases_removes_one_built_in_phrase():
    config = RuleConfig(disabled_phrases=frozenset({"robust"}))
    assert _matched("A robust design and a pivotal decision.", config=config) == ["pivotal"]


def test_switching_a_group_off_removes_its_flags():
    config = RuleConfig(groups={"dash_marks": False})
    assert "—" not in _matched("A — B.", config=config)


def test_academic_preset_does_not_flag_dashes_or_triads():
    text = "Fast, reliable, and cheap — delve in."
    general = _matched(text, preset="general")
    academic = _matched(text, preset="academic")
    assert "—" in general and "Fast, reliable, and cheap" in general
    assert academic == ["delve"]


def test_custom_phrase_rule_is_applied_with_its_label_and_source():
    config = parse_rule_config(
        {"rule": [{"id": "avoid-synergy", "kind": "phrase", "pattern": "synergy", "label": "Team wording"}]}
    )
    (hit,) = [item for item in _rules("Some synergy here.", config=config) if item["source"] == "custom"]
    assert hit["rule"] == "avoid-synergy"
    assert hit["message"].endswith("Team wording")
    assert hit["group"] == "custom"


def test_custom_rule_limited_to_named_presets():
    config = parse_rule_config(
        {"rule": [{"id": "only-academic", "kind": "phrase", "pattern": "synergy", "presets": ["academic"]}]}
    )
    assert "synergy" not in _matched("Some synergy here.", config=config, preset="general")
    assert "synergy" in _matched("Some synergy here.", config=config, preset="academic")


def test_custom_regex_rule_matches_and_ignore_case_can_be_turned_off():
    loose = parse_rule_config({"rule": [{"id": "pct", "kind": "regex", "pattern": r"\d+ ?%"}]})
    assert _matched("Grew 40 %.", config=loose) == ["40 %"]
    strict = parse_rule_config(
        {"rule": [{"id": "shout", "kind": "regex", "pattern": "NOTE", "ignore_case": False}]}
    )
    assert _matched("note here", config=strict) == []
    assert _matched("NOTE here", config=strict) == ["NOTE"]


def test_missing_rule_file_means_no_owner_rules(tmp_path):
    config = load_rule_config(tmp_path / RULE_FILE_NAME)
    assert config.custom_rules == () and config.groups == {}


def test_rule_file_round_trips_groups_disabled_phrases_and_rules(tmp_path):
    path = tmp_path / RULE_FILE_NAME
    path.write_text(
        'disable_phrases = ["robust"]\n'
        "[groups]\ntriad_lists = false\n"
        "[[rule]]\n"
        'id = "avoid-synergy"\nkind = "phrase"\npattern = "synergy"\n'
        'label = "Team wording"\npresets = ["general"]\n',
        encoding="utf-8",
    )
    config = load_rule_config(path)
    assert config.disabled_phrases == frozenset({"robust"})
    assert config.groups == {"triad_lists": False}
    assert [rule.id for rule in config.custom_rules] == ["avoid-synergy"]
    assert config.custom_rules[0].presets == ("general",)


@pytest.mark.parametrize(
    "body, fragment",
    [
        ("[[rule]]\nid = 'a'\nkind = 'phrase'\n", "pattern"),
        ("bogus_key = 1\n", "unknown top-level"),
        ("[groups]\nnope = true\n", "unknown group"),
        ("[groups]\ntriad_lists = 'yes'\n", "true or false"),
        ("disable_phrases = 'robust'\n", "list of strings"),
        ("[[rule]]\nid = 'Bad Id'\nkind = 'phrase'\npattern = 'x'\n", "id of lower-case"),
        ("[[rule]]\nid = 'a'\nkind = 'glob'\npattern = 'x'\n", "kind must be"),
        ("[[rule]]\nid = 'a'\nkind = 'phrase'\npattern = ''\n", "non-empty pattern"),
        ("[[rule]]\nid = 'a'\nkind = 'phrase'\npattern = 'x'\nweight = 2\n", "unknown key"),
        ("[[rule]]\nid = 'a'\nkind = 'phrase'\npattern = 'x'\npresets = ['nope']\n", "unknown preset"),
        ("[[rule]]\nid = 'a'\nkind = 'phrase'\npattern = 'x'\npresets = []\n", "must not be empty"),
        ("[[rule]]\nid = 'a'\nkind = 'regex'\npattern = '('\n", "does not compile"),
        ("[[rule]]\nid = 'a'\nkind = 'phrase'\npattern = 'x'\nignore_case = 'no'\n", "true or false"),
        ("[[rule]]\nid = 'a'\nkind = 'phrase'\npattern = 'x'\n[[rule]]\nid = 'a'\nkind = 'phrase'\npattern = 'y'\n", "duplicate rule id"),
        ("rule = 'not a list'\n", "array of tables"),
        ("[[rule]]\nid = 'a'\nkind = 'phrase'\npattern = '" + "x" * 301 + "'\n", "longer than"),
        ("[[rule]]\nid = 'a'\nkind = 'phrase'\npattern = 'x'\nlabel = '" + "y" * 201 + "'\n", "label must"),
    ],
)
def test_bad_rule_file_is_rejected_with_a_message_naming_the_problem(tmp_path, body, fragment):
    path = tmp_path / RULE_FILE_NAME
    path.write_text(body, encoding="utf-8")
    with pytest.raises(RuleFileError, match=fragment):
        load_rule_config(path)


def test_invalid_toml_is_reported_as_a_rule_file_error(tmp_path):
    path = tmp_path / RULE_FILE_NAME
    path.write_text("this is = = not toml", encoding="utf-8")
    with pytest.raises(RuleFileError, match="not valid TOML"):
        load_rule_config(path)
