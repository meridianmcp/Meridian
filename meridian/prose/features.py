"""Feature values with no verdict: specificity, lexical diversity and plain counts.

Each value comes with its numerator and denominator where it is a ratio, and with
the validation status from the registry. None is labelled good or bad.

Both value features are defined over distinct word types, not tokens, so the
padding rule holds: repeating an existing sentence, or adding a sentence made only
of stopwords, leaves them unchanged. Those edits change the flags, not the values.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .registry import get_measure
from .segment import split_sentences


@dataclass(frozen=True)
class FeatureValue:
    name: str
    value: float | int | None
    numerator: int | None
    denominator: int | None
    unit: str
    validation_status: str
    calibrated: bool
    definition: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _value(
    name: str,
    value: float | int | None,
    unit: str,
    numerator: int | None = None,
    denominator: int | None = None,
) -> FeatureValue:
    measure = get_measure(name)
    return FeatureValue(
        name=name,
        value=value,
        numerator=numerator,
        denominator=denominator,
        unit=unit,
        validation_status=measure.validation_status,
        calibrated=measure.calibrated,
        definition=measure.definition,
    )


def specificity(text: str) -> FeatureValue:
    """Share of distinct content word types that are specific. None if no content words."""
    specific_by_type: dict[str, bool] = {}
    for sentence in split_sentences(text):
        for word in sentence.words:
            if word.is_content:
                specific_by_type[word.text] = specific_by_type.get(word.text, False) or word.is_specific
    denominator = len(specific_by_type)
    numerator = sum(1 for is_specific in specific_by_type.values() if is_specific)
    value = round(numerator / denominator, 4) if denominator else None
    return _value("specificity", value, "ratio", numerator, denominator)


def lexical_diversity(text: str) -> FeatureValue:
    """Number of distinct content word types."""
    types = {
        word.text
        for sentence in split_sentences(text)
        for word in sentence.words
        if word.is_content
    }
    return _value("lexical_diversity", len(types), "distinct content word types")


def length_counts(text: str) -> dict[str, FeatureValue]:
    """Plain counts of words and sentences. No target and no score."""
    sentences = split_sentences(text)
    word_total = sum(len(sentence.words) for sentence in sentences)
    return {
        "word_count": _value("word_count", word_total, "words"),
        "sentence_count": _value("sentence_count", len(sentences), "sentences"),
    }
