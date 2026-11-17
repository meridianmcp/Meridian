"""Segmentation for the prose checks: sentences, words and locations.

Standard library only. No network access. Every module in ``meridian.prose``
uses these conventions:

- sentence numbers and line numbers are 1-based (sentence 0 means "before the
  first sentence", used only for matches in text with no words);
- character offsets are 0-based with an exclusive end, as in Python slicing;
- a sentence ends at ``.``, ``!`` or ``?`` (plus any closing quote or bracket)
  when that is followed by whitespace or the end of the text, and at every
  blank line. A period with no space after it does not end one, so ``3.5``
  stays whole. A known limit: an abbreviation followed by a space ("e.g. the")
  does end a sentence;
- only sentences that contain at least one word token are returned.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

# Function words. A word in this set is not "content": it does not count
# towards specificity, lexical diversity, or the generic-sentence check.
# Quantity words such as "few", "more" and "most" are deliberately absent,
# so they count as content.
STOPWORDS: frozenset[str] = frozenset(
    """
    a about above after again against all also am an and any are as at be because
    been before being below between both but by can could did do does doing down
    during each for from further had has have having he her here hers him his how
    i if in into is it its itself just me may might my no nor not now of off on
    once only or other our ours out over own same she should so some such than
    that the their theirs them then there these they this those through to too
    under until up us very was we were what when where which while who whom why
    will with would you your yours
    """.split()
)

_WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")
_BLANK_LINE_RE = re.compile(r"\n[ \t]*\n")
_TERMINATORS = ".!?"
_CLOSERS = "\"')]”’"


@dataclass(frozen=True)
class Location:
    """Where a match sits: 0-based char offsets, 1-based line and sentence."""

    start: int
    end: int
    line: int
    sentence: int

    def as_dict(self) -> dict[str, int]:
        return {"start": self.start, "end": self.end, "line": self.line, "sentence": self.sentence}


@dataclass(frozen=True)
class Word:
    """One word token. ``text`` is lower-cased with curly apostrophes made straight."""

    text: str
    raw: str
    start: int
    end: int
    line: int
    sentence: int
    initial: bool

    @property
    def is_content(self) -> bool:
        return self.text not in STOPWORDS

    @property
    def has_digit(self) -> bool:
        return any(ch.isdigit() for ch in self.raw)

    @property
    def is_specific(self) -> bool:
        """A content word with a digit, or a capitalised word that is not sentence-initial."""
        if not self.is_content:
            return False
        return self.has_digit or (not self.initial and self.raw[:1].isupper())


@dataclass(frozen=True)
class Sentence:
    number: int
    start: int
    end: int
    line: int
    text: str
    words: tuple[Word, ...]

    @property
    def location(self) -> Location:
        return Location(self.start, self.end, self.line, self.number)


def _split_block(text: str, lo: int, hi: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = lo
    i = lo
    while i < hi:
        if text[i] in _TERMINATORS:
            j = i
            while j < hi and text[j] in _TERMINATORS:
                j += 1
            k = j
            while k < hi and text[k] in _CLOSERS:
                k += 1
            if k >= hi or text[k].isspace():
                spans.append((start, k))
                start = k
            i = k
        else:
            i += 1
    if start < hi:
        spans.append((start, hi))
    return spans


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    block_start = 0
    for blank in _BLANK_LINE_RE.finditer(text):
        spans.extend(_split_block(text, block_start, blank.start()))
        block_start = blank.end()
    spans.extend(_split_block(text, block_start, len(text)))
    return spans


def _line_of(newlines: list[int], pos: int) -> int:
    return bisect_left(newlines, pos) + 1


def split_sentences(text: str) -> tuple[Sentence, ...]:
    """Split text into sentences with their word tokens. Pure and deterministic."""
    newlines = [i for i, ch in enumerate(text) if ch == "\n"]
    sentences: list[Sentence] = []
    for raw_start, raw_end in _sentence_spans(text):
        start, end = raw_start, raw_end
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start >= end:
            continue
        number = len(sentences) + 1
        words = tuple(
            Word(
                text=m.group(0).lower().replace("’", "'"),
                raw=m.group(0),
                start=m.start(),
                end=m.end(),
                line=_line_of(newlines, m.start()),
                sentence=number,
                initial=index == 0,
            )
            for index, m in enumerate(_WORD_RE.finditer(text, start, end))
        )
        if not words:
            continue
        sentences.append(
            Sentence(number, start, end, _line_of(newlines, start), text[start:end], words)
        )
    return tuple(sentences)


class Locator:
    """Maps a match in ``text`` to a :class:`Location` using precomputed positions."""

    def __init__(self, text: str, sentences: tuple[Sentence, ...]) -> None:
        self._newlines = [i for i, ch in enumerate(text) if ch == "\n"]
        self._sentences = sentences
        self._starts = [s.start for s in sentences]

    def at(self, start: int, end: int) -> Location:
        line = _line_of(self._newlines, start)
        index = bisect_right(self._starts, start) - 1
        number = self._sentences[index].number if index >= 0 else 0
        return Location(start, end, line, number)
