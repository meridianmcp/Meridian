"""Duplicate-sentence and restatement flags. Flags only; nothing is changed."""

import pytest

from meridian.prose import duplicate_sentences, restatement_candidates

RESTATED = "The cache is cleared on every worker restart."
RESTATES = "Every worker restart clears the cache."
UNRELATED = "Operators review the dashboard each morning."


def test_duplicates_ignore_case_spacing_and_punctuation_and_list_every_location():
    text = "We ship weekly. we   ship weekly!\n\nUnrelated line here."
    flags = duplicate_sentences(text)
    assert len(flags) == 1
    assert [loc.sentence for loc in flags[0].locations] == [1, 2]
    assert flags[0].status == "flag"
    assert flags[0].validation_status == "uncalibrated"


def test_each_duplicate_location_points_at_its_own_copy():
    text = "Cache clears daily. Other note. Cache clears daily."
    (flag,) = duplicate_sentences(text)
    for loc in flag.locations:
        assert text[loc.start : loc.end] == "Cache clears daily."


def test_text_without_duplicates_has_no_duplicate_flags():
    assert duplicate_sentences("One thing here. Another thing there.") == ()


def test_restatement_pair_is_flagged_with_shared_types_and_overlap():
    text = f"{RESTATED} {RESTATES} {UNRELATED}"
    (flag,) = restatement_candidates(text)
    assert (flag.first.sentence, flag.second.sentence) == (1, 2)
    assert flag.shared_content_types == ("cache", "every", "restart", "worker")
    assert flag.overlap == pytest.approx(0.667)
    assert flag.threshold == 0.6 and flag.min_shared == 3
    assert flag.status == "flag"


def test_identical_sentences_are_duplicates_not_restatements():
    assert restatement_candidates("Cache clears daily. Cache clears daily.") == ()


def test_raising_threshold_or_min_shared_removes_the_candidate():
    text = f"{RESTATED} {RESTATES}"
    assert restatement_candidates(text, threshold=0.9) == ()
    assert restatement_candidates(text, min_shared=5) == ()


@pytest.mark.parametrize("threshold", [0.0, 1.5])
def test_invalid_threshold_is_rejected(threshold):
    with pytest.raises(ValueError):
        restatement_candidates("A b c d.", threshold=threshold)


def test_min_shared_below_one_is_rejected():
    with pytest.raises(ValueError):
        restatement_candidates("A b c d.", min_shared=0)


def test_flag_dataclasses_hold_locations_and_no_replacement_text():
    (dup,) = duplicate_sentences("Ship it. Ship it.")
    assert set(dup.as_dict()) == {"text", "locations", "status", "validation_status"}
    assert all({"start", "end", "line", "sentence"} <= set(loc) for loc in dup.as_dict()["locations"])
