"""Padding invariance: adding filler must change the flags, not the feature values.

Filler here means the two kinds that add words without adding information:

- a verbatim repeat of an existing sentence;
- a sentence made only of stopwords ("It is what it is.").

Feature values are type-based, so neither kind moves them. Both kinds do move the
flags, which is the point: a repeat is flagged as a duplicate, and a stopword
sentence is asked about as generic. Length counts (word_count, sentence_count) grow
with any filler, by design, and are plain counts rather than feature values.
A control test shows the features do respond to genuinely new vocabulary, so the
invariance is not just a constant.
"""

import pytest

from meridian.prose import length_counts, lexical_diversity, run_prose_check, specificity

BASE = (
    "Meridian stores 1200 session records in Postgres. "
    "The sync job runs every 15 minutes on Fly.io. "
    "Operators review the dashboard each morning. "
    "Restarting the worker clears the in-memory cache."
)
REPEATED = "The sync job runs every 15 minutes on Fly.io."
STOPWORD_FILLER = "It is what it is."
NEW_CONTENT = "Revenue grew 40% in Berlin."

FILLERS = {
    "verbatim_repeat": f"{BASE} {REPEATED}",
    "stopword_only": f"{BASE} {STOPWORD_FILLER}",
    "both": f"{BASE} {REPEATED} {STOPWORD_FILLER}",
}


def _feature_values(text: str) -> dict[str, dict[str, object]]:
    return {
        "specificity": specificity(text).as_dict(),
        "lexical_diversity": lexical_diversity(text).as_dict(),
    }


def _flag_count(text: str, kind: str) -> int:
    return sum(1 for item in run_prose_check(text)["flags"] if item["kind"] == kind)


@pytest.mark.parametrize("filler", sorted(FILLERS))
def test_feature_values_do_not_change_under_filler(filler):
    padded = FILLERS[filler]
    assert _feature_values(padded) == _feature_values(BASE)


def test_verbatim_repeat_changes_the_duplicate_and_span_flags_not_the_values():
    padded = FILLERS["verbatim_repeat"]
    assert _flag_count(BASE, "duplicate_sentence") == 0
    assert _flag_count(padded, "duplicate_sentence") == 1
    assert _flag_count(padded, "repeated_span") > _flag_count(BASE, "repeated_span")
    assert _feature_values(padded) == _feature_values(BASE)


def test_stopword_filler_changes_the_generic_questions_not_the_values():
    padded = FILLERS["stopword_only"]
    before = _flag_count(BASE, "generic_sentence")
    after = _flag_count(padded, "generic_sentence")
    assert after == before + 1
    assert _feature_values(padded) == _feature_values(BASE)


def test_both_kinds_of_filler_move_the_flags_and_still_leave_values_alone():
    padded = FILLERS["both"]
    assert _flag_count(padded, "duplicate_sentence") == 1
    assert _flag_count(padded, "generic_sentence") == _flag_count(BASE, "generic_sentence") + 1
    assert _feature_values(padded) == _feature_values(BASE)


def test_control_genuinely_new_vocabulary_does_move_the_values():
    grown = f"{BASE} {NEW_CONTENT}"
    assert specificity(grown).numerator > specificity(BASE).numerator
    assert lexical_diversity(grown).value > lexical_diversity(BASE).value


def test_length_counts_grow_with_filler_by_design():
    base_counts = length_counts(BASE)
    padded_counts = length_counts(FILLERS["both"])
    assert padded_counts["word_count"].value > base_counts["word_count"].value
    assert padded_counts["sentence_count"].value > base_counts["sentence_count"].value
