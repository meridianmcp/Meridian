"""Repeated word spans: runs of n word tokens that occur at two or more places.

Spans are read inside one sentence only, so a span never crosses a sentence
boundary. Overlapping occurrences are all listed ("a a a a" with n=2 has three).
The count is a diagnostic. It is not a defect and nothing is changed.
"""

from __future__ import annotations

from dataclasses import dataclass

from .segment import Location, split_sentences

DIAGNOSTIC_NOTE = "The count is a diagnostic, not a defect. Nothing in the text is changed."


@dataclass(frozen=True)
class RepeatedSpan:
    words: tuple[str, ...]
    occurrences: tuple[Location, ...]

    @property
    def text(self) -> str:
        return " ".join(self.words)

    def as_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "occurrences": [loc.as_dict() for loc in self.occurrences],
        }


@dataclass(frozen=True)
class RepeatedSpanReport:
    n: int
    min_content_words: int
    spans: tuple[RepeatedSpan, ...]
    status: str = "diagnostic"
    note: str = DIAGNOSTIC_NOTE

    @property
    def count(self) -> int:
        """Number of distinct repeated spans. A diagnostic, not a defect."""
        return len(self.spans)

    def as_dict(self) -> dict[str, object]:
        return {
            "n": self.n,
            "min_content_words": self.min_content_words,
            "count": self.count,
            "status": self.status,
            "note": self.note,
            "spans": [span.as_dict() for span in self.spans],
        }


def repeated_spans(text: str, n: int = 4, min_content_words: int = 1) -> RepeatedSpanReport:
    """List every word span of length ``n`` that occurs at least twice.

    ``min_content_words`` drops windows with fewer content words. The default of 1
    drops windows made only of stopwords ("it is what it"). Raises ValueError for
    ``n < 2`` or a negative ``min_content_words``.
    """
    if n < 2:
        raise ValueError("n must be at least 2")
    if min_content_words < 0:
        raise ValueError("min_content_words must not be negative")

    found: dict[tuple[str, ...], list[Location]] = {}
    for sentence in split_sentences(text):
        words = sentence.words
        for i in range(len(words) - n + 1):
            window = words[i : i + n]
            if sum(1 for word in window if word.is_content) < min_content_words:
                continue
            key = tuple(word.text for word in window)
            found.setdefault(key, []).append(
                Location(window[0].start, window[-1].end, window[0].line, sentence.number)
            )

    spans = [
        RepeatedSpan(key, tuple(locations))
        for key, locations in found.items()
        if len(locations) > 1
    ]
    spans.sort(key=lambda span: (span.occurrences[0].start, span.words))
    return RepeatedSpanReport(n=n, min_content_words=min_content_words, spans=tuple(spans))
