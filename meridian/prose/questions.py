"""Questions to the author. The module returns questions and never inserts content.

Four kinds, each a heuristic that is uncalibrated:

- ``unsourced_figure``: a sentence with a number and no source marker.
- ``quantity_claim_without_number``: a magnitude or change word with no number.
- ``generic_sentence``: no digit, no mid-sentence capitalised word, and fewer
  content words than ``generic_min_content``.
- ``repeat_may_be_intended``: a repeated span whose occurrences sit in the same
  or an adjacent sentence. Asked as a question. Distant repeats are not asked
  about; they appear only as repeated-span flags.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .repeats import repeated_spans
from .segment import Location, Sentence, split_sentences

# Words that claim a size or a change. They normally carry a number. This is a
# trigger for a question, not a list of banned words.
VAGUE_QUANTITY_TERMS: frozenset[str] = frozenset(
    {
        "significantly", "substantially", "dramatically", "considerably", "majority",
        "most", "many", "several", "few", "much", "increased", "increases", "decreased",
        "decreases", "reduced", "reduces", "improved", "improves", "faster", "slower",
        "larger", "smaller",
    }
)

_SOURCE_RE = re.compile(
    r"\[\s*cite\s*\]"
    r"|\[\s*\d+(?:\s*[,–-]\s*\d+)*\s*\]"
    r"|\((?:[^()]*?,\s*)?(?:19|20)\d{2}[a-z]?\)"
    r"|https?://\S+"
    r"|\bdoi:\s*\S+"
    r"|\baccording to\b"
    r"|\bsource:",
    re.IGNORECASE,
)
_FIGURE_RE = re.compile(r"(?<![\w.])[$€£]?\d+(?:[.,]\d+)*%?")
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


@dataclass(frozen=True)
class AuthorQuestion:
    kind: str
    location: Location
    evidence: str
    question: str
    status: str = "question"

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "location": self.location.as_dict(),
            "evidence": self.evidence,
            "question": self.question,
            "status": self.status,
        }


def _figures(sentence_text: str) -> list[str]:
    out: list[str] = []
    for match in _FIGURE_RE.finditer(sentence_text):
        figure = match.group(0)
        if _YEAR_RE.fullmatch(figure):
            continue
        out.append(figure)
    return out


def has_source_marker(sentence_text: str) -> bool:
    return _SOURCE_RE.search(sentence_text) is not None


def author_questions(
    text: str,
    *,
    repeat_n: int = 4,
    repeat_min_content: int = 2,
    generic_min_content: int = 4,
) -> tuple[AuthorQuestion, ...]:
    """Questions for the author about ``text``. The text itself is never changed."""
    questions: list[AuthorQuestion] = []
    for sentence in split_sentences(text):
        questions.extend(_sentence_questions(sentence, generic_min_content))
    questions.extend(_repeat_questions(text, repeat_n, repeat_min_content))
    questions.sort(key=lambda q: (q.location.start, q.kind, q.evidence))
    return tuple(questions)


def _sentence_questions(sentence: Sentence, generic_min_content: int) -> list[AuthorQuestion]:
    out: list[AuthorQuestion] = []
    number = sentence.number
    figures = _figures(sentence.text)

    if figures and not has_source_marker(sentence.text):
        listed = ", ".join(figures)
        out.append(
            AuthorQuestion(
                "unsourced_figure",
                sentence.location,
                listed,
                f"Sentence {number} gives {listed}. Where does this figure come from?",
            )
        )

    vague = [word for word in sentence.words if word.text in VAGUE_QUANTITY_TERMS]
    if vague and not figures:
        term = vague[0].raw
        out.append(
            AuthorQuestion(
                "quantity_claim_without_number",
                sentence.location,
                term,
                f"Sentence {number} uses '{term}' without a number. What is the measured "
                "amount, and what is it compared with?",
            )
        )

    content = sum(1 for word in sentence.words if word.is_content)
    if not any(word.is_specific for word in sentence.words) and content < generic_min_content:
        out.append(
            AuthorQuestion(
                "generic_sentence",
                sentence.location,
                f"{content} content words",
                f"Sentence {number} has no number, name or specific term. What specific "
                "claim does it make?",
            )
        )
    return out


def _repeat_questions(text: str, n: int, min_content: int) -> list[AuthorQuestion]:
    report = repeated_spans(text, n=n, min_content_words=min_content)
    pairs: dict[tuple[int, int], tuple[str, Location]] = {}
    for span in report.spans:
        # Occurrences are listed in sentence order, so the inner scan can stop early.
        occurrences = span.occurrences
        for a_index, first in enumerate(occurrences):
            for b_index in range(a_index + 1, len(occurrences)):
                second = occurrences[b_index]
                if second.sentence - first.sentence > 1:
                    break
                pairs.setdefault((first.sentence, second.sentence), (span.text, first))

    out: list[AuthorQuestion] = []
    for (low, high), (span_text, location) in pairs.items():
        if low == high:
            where = f"twice in sentence {low}"
        else:
            where = f"in sentences {low} and {high}"
        out.append(
            AuthorQuestion(
                "repeat_may_be_intended",
                location,
                span_text,
                f"'{span_text}' appears {where}. Is the repetition intended?",
            )
        )
    return out
