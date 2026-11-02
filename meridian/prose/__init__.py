"""Prose checks: flags, questions and feature values for a piece of writing.

Pure Python. Standard library only. No network calls and no new dependencies.
Everything returned is a flag, a question, a plain count or a reported value
with its validation status. Nothing is a score, a grade or a verdict on a
document, and nothing here changes the text it checks.

Modules:

- ``segment``: sentences, words and locations (the conventions are in its docstring).
- ``registry``: each measure's name, definition, validation status and calibrated flag.
- ``repeats``: repeated word spans of length n, each listed with its locations.
- ``flags``: duplicate sentences and restatement candidates.
- ``questions``: questions to the author. Never inserts content.
- ``features``: specificity and lexical diversity as values, plus word and sentence counts.
- ``rules``: built-in rule groups and the owner's editable ``prose_rules.toml``.
- ``presets``: the presets (general, academic, overleaf, docx) and their normalisers.
- ``check``: ``run_prose_check``, which runs one preset and returns one flat list of flags.

NOT_BUILT records what this package deliberately leaves out, so the omission is
visible in code rather than only in a plan.
"""

from __future__ import annotations

from .check import run_prose_check
from .features import FeatureValue, length_counts, lexical_diversity, specificity
from .flags import (
    DuplicateSentenceFlag,
    RestatementFlag,
    duplicate_sentences,
    restatement_candidates,
)
from .presets import PRESET_NAMES, get_preset, normalize
from .questions import AuthorQuestion, author_questions
from .registry import MEASURES, Measure, get_measure, measure_table
from .repeats import RepeatedSpan, RepeatedSpanReport, repeated_spans
from .rules import RULE_FILE_NAME, RuleConfig, RuleFileError, load_rule_config
from .segment import Location, Locator, Sentence, Word, split_sentences

NOT_BUILT: tuple[str, ...] = (
    "Calibration protocol (item 9): not built. It waits for the owner's calibration data and "
    "decisions. Until it exists every measure keeps calibrated=False, and nothing here can set it.",
    "House-style stock-phrase list (item 4 as first scoped): not built. It waits for the owner's "
    "decisions. The stock-phrase group in rules.py is a separate anti-AI starting list, added on "
    "the owner's 2026-11-02 scope addition, and it is not a house-style list.",
)

__all__ = [
    "AuthorQuestion",
    "DuplicateSentenceFlag",
    "FeatureValue",
    "Location",
    "Locator",
    "MEASURES",
    "Measure",
    "NOT_BUILT",
    "PRESET_NAMES",
    "RULE_FILE_NAME",
    "RepeatedSpan",
    "RepeatedSpanReport",
    "RestatementFlag",
    "RuleConfig",
    "RuleFileError",
    "Sentence",
    "Word",
    "author_questions",
    "duplicate_sentences",
    "get_measure",
    "get_preset",
    "length_counts",
    "lexical_diversity",
    "load_rule_config",
    "measure_table",
    "normalize",
    "repeated_spans",
    "restatement_candidates",
    "run_prose_check",
    "specificity",
    "split_sentences",
]
