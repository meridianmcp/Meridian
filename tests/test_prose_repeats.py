"""Repeated word spans: every span of length n listed with its locations, a diagnostic count."""

import pytest

from meridian.prose import repeated_spans


def test_repeated_span_lists_each_location_and_counts_distinct_spans():
    text = "The sync job runs nightly. Operators read logs. The sync job runs nightly again."
    report = repeated_spans(text, n=4)
    assert report.count == 2
    assert sorted(span.text for span in report.spans) == [
        "sync job runs nightly",
        "the sync job runs",
    ]
    for span in report.spans:
        assert [loc.sentence for loc in span.occurrences] == [1, 3]


def test_each_occurrence_offset_slices_back_to_the_span_text():
    text = "The sync job runs nightly. Operators read logs. The sync job runs nightly again."
    report = repeated_spans(text, n=4)
    span = next(s for s in report.spans if s.text == "the sync job runs")
    for loc in span.occurrences:
        assert text[loc.start : loc.end] == "The sync job runs"


def test_count_is_labelled_as_a_diagnostic_not_a_defect():
    report = repeated_spans("Data data data data.", n=2)
    assert report.status == "diagnostic"
    assert "diagnostic" in report.note and "not a defect" in report.note
    assert report.as_dict()["status"] == "diagnostic"


def test_overlapping_occurrences_are_all_listed():
    report = repeated_spans("Data data data data.", n=2)
    assert report.count == 1
    assert len(report.spans[0].occurrences) == 3


def test_text_with_no_repeats_has_count_zero():
    report = repeated_spans("Each sentence says something different here.", n=4)
    assert report.count == 0 and report.spans == ()


def test_stopword_only_windows_are_excluded_by_default():
    text = "It is what it is. It is what it is."
    assert repeated_spans(text, n=4).count == 0
    assert repeated_spans(text, n=4, min_content_words=0).count > 0


def test_spans_never_cross_a_sentence_boundary():
    # Read as one flat run, "fish blue" would occur twice: once across the boundary
    # after "Red fish." and once inside "Fish blue sky." Read inside sentences, it occurs once.
    text = "Red fish. Blue fish. Fish blue sky."
    assert repeated_spans(text, n=2).count == 0


@pytest.mark.parametrize("n", [0, 1])
def test_n_below_two_is_rejected(n):
    with pytest.raises(ValueError):
        repeated_spans("Some words here.", n=n)


def test_negative_min_content_words_is_rejected():
    with pytest.raises(ValueError):
        repeated_spans("Some words here.", n=2, min_content_words=-1)
