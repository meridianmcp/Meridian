"""Feature values with no verdict: specificity, lexical diversity and plain counts."""

from meridian.prose import length_counts, lexical_diversity, specificity


def test_specificity_reports_numerator_denominator_and_ratio():
    value = specificity("Meridian uses Postgres and Redis.")
    # Content types: meridian (initial, not specific), uses (no), postgres (yes), redis (yes).
    assert (value.numerator, value.denominator) == (2, 4)
    assert value.value == 0.5
    assert value.unit == "ratio"


def test_specificity_is_none_when_there_are_no_content_words():
    value = specificity("It is what it is.")
    assert value.value is None
    assert (value.numerator, value.denominator) == (0, 0)


def test_lexical_diversity_counts_distinct_content_types_not_tokens():
    assert lexical_diversity("Cache clears daily.").value == 3
    assert lexical_diversity("The cache clears. The cache clears.").value == 2


def test_feature_values_are_uncalibrated_and_carry_their_definition():
    for value in (specificity("Meridian uses Postgres."), lexical_diversity("Cache clears.")):
        data = value.as_dict()
        assert data["validation_status"] == "uncalibrated"
        assert data["calibrated"] is False
        assert data["definition"]


def test_feature_values_carry_no_score_grade_or_verdict_field():
    forbidden = {"score", "verdict", "grade", "rating", "quality", "good", "bad", "pass", "fail"}
    for value in (specificity("Meridian uses Postgres."), lexical_diversity("Cache clears.")):
        assert not forbidden & set(value.as_dict())


def test_length_counts_are_plain_counts_of_words_and_sentences():
    counts = length_counts("One two. Three four five.")
    assert counts["word_count"].value == 5
    assert counts["sentence_count"].value == 2
    assert counts["word_count"].unit == "words"


def test_length_counts_of_empty_text_are_zero():
    counts = length_counts("")
    assert counts["word_count"].value == 0 and counts["sentence_count"].value == 0
