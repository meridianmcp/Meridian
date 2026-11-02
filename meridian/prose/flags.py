"""Duplicate-sentence and restatement flags. Flags only: the text is never changed.

- A duplicate group is two or more sentences identical after lower-casing and
  removal of whitespace and punctuation. One flag lists every location.
- A restatement candidate is a pair of distinct sentences whose distinct
  content word types overlap enough (Jaccard ratio at or above ``threshold``,
  and at least ``min_shared`` shared types). Each distinct sentence is compared at
  its first occurrence; repeats of it are duplicates, not restatement candidates.
  Both thresholds are defaults, not calibrated.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

from .registry import UNCALIBRATED
from .segment import Location, Sentence, split_sentences


@dataclass(frozen=True)
class DuplicateSentenceFlag:
    text: str
    locations: tuple[Location, ...]
    status: str = "flag"
    validation_status: str = UNCALIBRATED

    def as_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "locations": [loc.as_dict() for loc in self.locations],
            "status": self.status,
            "validation_status": self.validation_status,
        }


@dataclass(frozen=True)
class RestatementFlag:
    first: Location
    second: Location
    shared_content_types: tuple[str, ...]
    overlap: float
    threshold: float
    min_shared: int
    status: str = "flag"
    validation_status: str = UNCALIBRATED

    def as_dict(self) -> dict[str, object]:
        return {
            "first": self.first.as_dict(),
            "second": self.second.as_dict(),
            "shared_content_types": list(self.shared_content_types),
            "overlap": self.overlap,
            "threshold": self.threshold,
            "min_shared": self.min_shared,
            "status": self.status,
            "validation_status": self.validation_status,
        }


def duplicate_sentences(text: str) -> tuple[DuplicateSentenceFlag, ...]:
    """One flag per group of identical sentences, listing every location."""
    groups: dict[tuple[str, ...], list] = {}
    for sentence in split_sentences(text):
        key = tuple(word.text for word in sentence.words)
        groups.setdefault(key, []).append(sentence)

    flags = [
        DuplicateSentenceFlag(
            text=group[0].text,
            locations=tuple(sentence.location for sentence in group),
        )
        for group in groups.values()
        if len(group) > 1
    ]
    flags.sort(key=lambda flag: flag.locations[0].start)
    return tuple(flags)


def restatement_candidates(
    text: str,
    *,
    threshold: float = 0.6,
    min_shared: int = 3,
    limit: int | None = None,
) -> tuple[RestatementFlag, ...]:
    """Pairs of non-identical sentences that share most of their content vocabulary.

    ``limit`` stops the scan once that many flags are found, so a long, repetitive
    text cannot make the check quadratic in output. When the limit is reached the
    result is a prefix of the full list, in sentence order, and may be incomplete.
    """
    if not 0 < threshold <= 1:
        raise ValueError("threshold must be in (0, 1]")
    if min_shared < 1:
        raise ValueError("min_shared must be at least 1")
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1")

    # Distinct sentences only, each at its first occurrence. A repeated sentence is a
    # duplicate, already flagged with all its locations; comparing every copy with every
    # other copy would add no information and would be quadratic in repeated text.
    first_by_key: dict[tuple[str, ...], Sentence] = {}
    for sentence in split_sentences(text):
        first_by_key.setdefault(tuple(word.text for word in sentence.words), sentence)
    sentences = list(first_by_key.values())
    contents = [frozenset(word.text for word in s.words if word.is_content) for s in sentences]

    # Inverted index: content type -> ascending sentence indices. Only pairs that share
    # at least one type are ever compared, which keeps long texts far from O(n^2).
    postings: dict[str, list[int]] = {}
    for index, types in enumerate(contents):
        for word_type in types:
            postings.setdefault(word_type, []).append(index)

    flags: list[RestatementFlag] = []
    for i in range(len(sentences)):
        shared_with: dict[int, int] = {}
        for word_type in contents[i]:
            plist = postings[word_type]
            for k in range(bisect_right(plist, i), len(plist)):
                j = plist[k]
                shared_with[j] = shared_with.get(j, 0) + 1
        for j in sorted(shared_with):
            if shared_with[j] < min_shared:
                continue
            shared = contents[i] & contents[j]
            union = contents[i] | contents[j]
            if not union:
                continue
            overlap = len(shared) / len(union)
            if overlap >= threshold:
                flags.append(
                    RestatementFlag(
                        first=sentences[i].location,
                        second=sentences[j].location,
                        shared_content_types=tuple(sorted(shared)),
                        overlap=round(overlap, 3),
                        threshold=threshold,
                        min_shared=min_shared,
                    )
                )
                if limit is not None and len(flags) >= limit:
                    return tuple(flags)
    return tuple(flags)
