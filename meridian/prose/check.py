"""One call, one list of flags: run a preset over a text.

The result is plain data. ``flags`` holds every flag and question in one list,
sorted by location. Each entry has ``status`` of ``"flag"`` or ``"question"``.
There is no score, grade, verdict or pass/fail anywhere in the result. Counts
are plain counts of what was found.
"""

from __future__ import annotations

from typing import Any

from .features import length_counts, lexical_diversity, specificity
from .flags import duplicate_sentences, restatement_candidates
from .presets import get_preset, normalize
from .questions import author_questions
from .registry import UNCALIBRATED
from .repeats import repeated_spans
from .rules import RuleConfig, rule_hits
from .segment import Location, Locator, split_sentences

MAX_CHARS = 200_000
MAX_FLAGS_PER_KIND = 500

NOTES: tuple[str, ...] = (
    "Flags and questions only. Nothing in the text is changed, and nothing here is a score or a verdict.",
    "Every rule, threshold and measure is uncalibrated (calibrated is false).",
)


def _flag(
    kind: str,
    status: str,
    message: str,
    locations: list[Location],
    *,
    rule: str | None = None,
    group: str | None = None,
    source: str = "core",
    evidence: str = "",
) -> dict[str, Any]:
    first = locations[0]
    return {
        "kind": kind,
        "status": status,
        "rule": rule,
        "group": group,
        "source": source,
        "evidence": evidence,
        "message": message,
        "start": first.start,
        "end": first.end,
        "line": first.line,
        "sentence": first.sentence,
        "locations": [loc.as_dict() for loc in locations],
        "validation_status": UNCALIBRATED,
    }


def run_prose_check(
    text: str,
    *,
    preset: str = "general",
    rule_config: RuleConfig | None = None,
) -> dict[str, Any]:
    """Run ``preset`` over ``text`` and return plain data. Raises ValueError on bad input."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    if len(text) > MAX_CHARS:
        raise ValueError(f"text is longer than {MAX_CHARS} characters")

    active = get_preset(preset)
    config = rule_config or RuleConfig.empty()
    normalized = normalize(text, active.normalizer)
    locator = Locator(normalized, split_sentences(normalized))
    groups = [group for group in active.rule_groups if config.groups.get(group, True)]

    flags: list[dict[str, Any]] = []

    for hit in rule_hits(normalized, locator, groups, config, active.name):
        flags.append(
            _flag(
                "rule",
                "flag",
                f"'{hit.matched}': {hit.label}",
                [hit.location],
                rule=hit.rule_id,
                group=hit.group,
                source=hit.source,
                evidence=hit.matched,
            )
        )

    span_report = repeated_spans(normalized)
    for span in span_report.spans:
        flags.append(
            _flag(
                "repeated_span",
                "flag",
                f"'{span.text}' occurs {len(span.occurrences)} times. Diagnostic, not a defect.",
                list(span.occurrences),
                evidence=span.text,
            )
        )

    for dup in duplicate_sentences(normalized):
        flags.append(
            _flag(
                "duplicate_sentence",
                "flag",
                f"Identical sentence at {len(dup.locations)} locations. Listed for review.",
                list(dup.locations),
                evidence=dup.text,
            )
        )

    for pair in restatement_candidates(normalized, limit=MAX_FLAGS_PER_KIND + 1):
        flags.append(
            _flag(
                "restatement_candidate",
                "flag",
                f"Sentences {pair.first.sentence} and {pair.second.sentence} share "
                f"{len(pair.shared_content_types)} content word types (overlap {pair.overlap}). "
                "Candidate for review.",
                [pair.first, pair.second],
                evidence=", ".join(pair.shared_content_types),
            )
        )

    for question in author_questions(normalized):
        flags.append(
            _flag(
                question.kind,
                "question",
                question.question,
                [question.location],
                evidence=question.evidence,
            )
        )

    flags.sort(key=lambda f: (f["start"], f["kind"], f["rule"] or ""))

    kept: list[dict[str, Any]] = []
    seen_per_kind: dict[str, int] = {}
    truncated: set[str] = set()
    for item in flags:
        kind = item["kind"]
        seen_per_kind[kind] = seen_per_kind.get(kind, 0) + 1
        if seen_per_kind[kind] > MAX_FLAGS_PER_KIND:
            truncated.add(kind)
            continue
        kept.append(item)

    counts: dict[str, int] = {}
    for item in kept:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1

    notes = list(NOTES) + [f"Locations refer to the text after the '{active.normalizer}' normaliser."]
    if truncated:
        notes.append(
            f"More than {MAX_FLAGS_PER_KIND} flags exist for: {', '.join(sorted(truncated))}. "
            "The list is capped; the counts for those kinds are the listed numbers."
        )

    counted = length_counts(normalized)
    return {
        "preset": active.name,
        "normalizer": active.normalizer,
        "rule_groups": groups,
        "custom_rules_loaded": len(config.custom_rules),
        "custom_rules_applied": [
            rule.id
            for rule in config.custom_rules
            if rule.presets is None or active.name in rule.presets
        ],
        "measures": {
            "word_count": counted["word_count"].as_dict(),
            "sentence_count": counted["sentence_count"].as_dict(),
            "specificity": specificity(normalized).as_dict(),
            "lexical_diversity": lexical_diversity(normalized).as_dict(),
        },
        "counts_by_kind": counts,
        "truncated_kinds": sorted(truncated),
        "flags": kept,
        "notes": notes,
    }
