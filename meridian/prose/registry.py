"""Measure registry: what each prose measure is, and how far it has been validated.

Every measure ships with ``calibrated=False`` and ``validation_status="uncalibrated"``.
The calibration protocol is not built (see ``NOT_BUILT`` in ``__init__.py``), so
nothing in this package can mark a measure as calibrated.

``kind`` is one of: ``count`` (a plain number), ``value`` (a reported value with
its parts), ``flag`` (a location-bearing match shown for the author), or
``question`` (a question put to the author). None of the kinds is a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass

UNCALIBRATED = "uncalibrated"


@dataclass(frozen=True)
class Measure:
    name: str
    kind: str
    definition: str
    validation_status: str = UNCALIBRATED
    calibrated: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "definition": self.definition,
            "validation_status": self.validation_status,
            "calibrated": self.calibrated,
        }


MEASURES: tuple[Measure, ...] = (
    Measure(
        "word_count",
        "count",
        "Number of word tokens: runs of letters or digits, with apostrophes kept inside a word. "
        "Stopwords are included. No target is attached.",
    ),
    Measure(
        "sentence_count",
        "count",
        "Number of sentences that contain at least one word token. Boundary rule in segment.py. "
        "No target is attached.",
    ),
    Measure(
        "specificity",
        "value",
        "Distinct content word types that are specific (contain a digit, or are capitalised "
        "and not sentence-initial), divided by distinct content word types. Stopwords are "
        "excluded. Reported with its numerator and denominator. Not labelled good or bad.",
    ),
    Measure(
        "lexical_diversity",
        "value",
        "Number of distinct content word types. Counted by type, not by token, so repeating a "
        "sentence does not change it. A type-token ratio was not used because repetition moves "
        "it. Not labelled good or bad.",
    ),
    Measure(
        "repeated_span",
        "flag",
        "A run of n consecutive word tokens inside one sentence that occurs at two or more "
        "places. Each is listed with its locations. The count is a diagnostic, not a defect.",
    ),
    Measure(
        "duplicate_sentence",
        "flag",
        "Two or more sentences identical after case, whitespace and punctuation are removed. "
        "Listed with every location.",
    ),
    Measure(
        "restatement_candidate",
        "flag",
        "A pair of non-identical sentences whose content word types overlap by at least "
        "min_shared types and by a Jaccard ratio of at least threshold. Both thresholds are "
        "defaults, not calibrated.",
    ),
    Measure(
        "question:unsourced_figure",
        "question",
        "A sentence with a number and no source marker (bracketed citation, author-year, "
        "\\cite, URL, 'according to', 'source:'). Plain years are not treated as figures.",
    ),
    Measure(
        "question:quantity_claim_without_number",
        "question",
        "A sentence with a magnitude or change word (for example 'significantly', 'increased') "
        "and no number.",
    ),
    Measure(
        "question:generic_sentence",
        "question",
        "A sentence with no digit, no mid-sentence capitalised word, and fewer content words "
        "than generic_min_content.",
    ),
    Measure(
        "question:repeat_may_be_intended",
        "question",
        "A repeated span whose occurrences are in the same or an adjacent sentence. Asked as a "
        "question; distant repeats appear only as repeated_span flags.",
    ),
    Measure(
        "rule:stock_phrases",
        "flag",
        "A built-in phrase from the stock-phrase list, matched case-insensitively with word "
        "boundaries. A starting list for the owner to edit. Not a house-style list.",
    ),
    Measure(
        "rule:dash_marks",
        "flag",
        "Each em dash, en dash or double hyphen, listed with its location. A count of use, "
        "not a defect.",
    ),
    Measure(
        "rule:triad_lists",
        "flag",
        "A three-item list joined by 'and' or 'or' (word, word, and word). Listed as a "
        "candidate for review, not as a defect.",
    ),
    Measure(
        "rule:custom",
        "flag",
        "A match of an owner-written rule in prose_rules.toml. The rule's own label is shown.",
    ),
)

_BY_NAME: dict[str, Measure] = {}
for _measure in MEASURES:
    if _measure.name in _BY_NAME:
        raise ValueError(f"duplicate measure name: {_measure.name}")
    _BY_NAME[_measure.name] = _measure


def get_measure(name: str) -> Measure:
    """Return the registry entry for ``name``. Raises KeyError for an unknown name."""
    return _BY_NAME[name]


def measure_table() -> list[dict[str, object]]:
    """Every measure as plain dicts, in registry order."""
    return [m.as_dict() for m in MEASURES]
