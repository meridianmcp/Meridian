"""Questions to the author: returned as questions, never as inserted content."""

import dataclasses

import pytest

from meridian.prose import author_questions
from meridian.prose.questions import AuthorQuestion


def _kinds(text: str, **kwargs) -> list[tuple[str, int]]:
    return [(q.kind, q.location.sentence) for q in author_questions(text, **kwargs)]


@pytest.mark.parametrize(
    "sourced",
    [
        "Revenue grew 40% [3].",
        "Revenue grew 40% (Lee, 2023).",
        "Revenue grew 40% according to the annual report.",
        "Revenue grew 40% [cite].",
        "Source: annual report, revenue grew 40%.",
        "Revenue grew 40% per https://example.com/report.",
    ],
)
def test_figure_with_a_source_marker_is_not_asked_about(sourced):
    assert not any(kind == "unsourced_figure" for kind, _ in _kinds(sourced))


def test_unsourced_figure_is_asked_about_and_plain_years_are_not_figures():
    assert ("unsourced_figure", 1) in _kinds("Revenue grew 40% in 2023.")
    assert not any(kind == "unsourced_figure" for kind, _ in _kinds("Founded in 2019."))


def test_quantity_claim_without_a_number_is_asked_about_but_not_with_one():
    assert ("quantity_claim_without_number", 1) in _kinds("Revenue grew significantly.")
    with_number = _kinds("Revenue grew significantly by 40%.")
    assert not any(kind == "quantity_claim_without_number" for kind, _ in with_number)


def test_generic_sentence_has_no_number_name_and_few_content_words():
    assert ("generic_sentence", 1) in _kinds("Results are shown below.")
    assert ("generic_sentence", 1) in _kinds("It is what it is.")
    assert not any(kind == "generic_sentence" for kind, _ in _kinds("Meridian stores session records in Postgres."))


def test_generic_threshold_is_a_parameter():
    text = "The cache clears on restart."
    assert ("generic_sentence", 1) in _kinds(text, generic_min_content=4)
    assert not any(kind == "generic_sentence" for kind, _ in _kinds(text, generic_min_content=2))


def test_repeat_in_adjacent_sentences_is_asked_about_and_distant_repeat_is_not():
    adjacent = "The sync job runs nightly. The sync job runs nightly again."
    asked = [q for q in author_questions(adjacent) if q.kind == "repeat_may_be_intended"]
    assert len(asked) == 1
    assert asked[0].question.endswith("Is the repetition intended?")

    distant = "The sync job runs nightly. Operators read logs. The sync job runs nightly."
    assert not any(q.kind == "repeat_may_be_intended" for q in author_questions(distant))


def test_every_question_is_phrased_as_a_question():
    text = (
        "Revenue grew 40% in 2023. Revenue grew significantly. It is what it is. "
        "The sync job runs nightly. The sync job runs nightly again."
    )
    questions = author_questions(text)
    assert questions
    assert all(q.question.endswith("?") for q in questions)


def test_questions_carry_a_location_and_no_replacement_content():
    fields = {f.name for f in dataclasses.fields(AuthorQuestion)}
    assert fields == {"kind", "location", "evidence", "question", "status"}
    (question,) = [q for q in author_questions("Revenue grew 40% in 2023.") if q.kind == "unsourced_figure"]
    assert question.location.sentence == 1
    assert question.evidence == "40%"
    assert question.status == "question"
