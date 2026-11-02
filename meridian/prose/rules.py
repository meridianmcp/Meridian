"""Rule groups for the anti-AI checks, and the owner's editable rule file.

Built-in groups (each is a visible list in this file, and each match is a flag):

- ``stock_phrases``: phrases from ``STOCK_PHRASES``. A starting list. It is not a
  house-style list. The owner can switch the group off, or remove single phrases
  with ``disable_phrases``.
- ``dash_marks``: em dashes, en dashes and double hyphens, listed as a count of use.
- ``triad_lists``: three-item lists joined by "and" or "or", listed as candidates.

Owner rules live in a plain TOML file, ``prose_rules.toml``, in the server's data
directory. The file is read on every check, so an edit takes effect at once.
Example::

    disable_phrases = ["robust"]

    [groups]
    triad_lists = false

    [[rule]]
    id = "avoid-synergy"
    kind = "phrase"                # "phrase" (case-insensitive, whole words) or "regex"
    pattern = "synergy"
    label = "Team wording: avoid 'synergy'"
    presets = ["general", "academic"]   # optional; omit to apply to every preset

    [[rule]]
    id = "bare-percent"
    kind = "regex"
    pattern = "\\\\d+ ?%"          # Python regex; ignore_case defaults to true
    label = "Percent figure"

Unknown keys are rejected, so a typo is reported rather than silently ignored.
A rule flags a match. Nothing is ever replaced.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .segment import Location, Locator

GROUP_NAMES: tuple[str, ...] = ("stock_phrases", "dash_marks", "triad_lists")
RULE_FILE_NAME = "prose_rules.toml"
MAX_PATTERN_LENGTH = 300
MAX_LABEL_LENGTH = 200
RULE_KINDS: tuple[str, ...] = ("phrase", "regex")

_RULE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_TOP_KEYS = frozenset({"groups", "disable_phrases", "rule"})
_RULE_KEYS = frozenset({"id", "kind", "pattern", "label", "presets", "ignore_case"})

GROUP_LABELS: dict[str, str] = {
    "stock_phrases": "On the built-in stock-phrase list. Flagged for the author's review.",
    "dash_marks": "Dash mark. Listed for the author's review; not a defect.",
    "triad_lists": "Three-item list. Listed as a candidate for review.",
}

# A starting list of stock phrases that often show up in generic or machine-drafted
# prose. It is a flag list for review, not a verdict. Edit it with disable_phrases
# or custom rules in prose_rules.toml.
STOCK_PHRASES: tuple[str, ...] = (
    "delve", "delves", "delving", "delved",
    "tapestry",
    "unlock the potential", "unlock the power of",
    "in today's fast-paced world",
    "it is worth noting", "it's worth noting",
    "in conclusion",
    "navigate the landscape", "navigating the landscape",
    "game-changer", "game changer", "game-changing",
    "embark on a journey",
    "a testament to",
    "at its core",
    "in the realm of",
    "ever-evolving",
    "seamlessly",
    "pivotal",
    "leverage", "leverages", "leveraging",
    "robust",
    "showcase", "showcases", "showcasing",
    "harness the power",
    "furthermore", "moreover",
)

_DASH_RE = re.compile(r"—|–|-{2,}")
_TRIAD_RE = re.compile(r"\b\w+, \w+,? (?:and|or) \w+\b", re.IGNORECASE)


class RuleFileError(ValueError):
    """The rule file is unreadable, not valid TOML, or breaks the rule format."""


@dataclass(frozen=True)
class CustomRule:
    id: str
    kind: str
    pattern: str
    label: str
    presets: tuple[str, ...] | None
    regex: re.Pattern[str] = field(compare=False, repr=False)


@dataclass(frozen=True)
class RuleConfig:
    groups: dict[str, bool] = field(default_factory=dict)
    disabled_phrases: frozenset[str] = frozenset()
    custom_rules: tuple[CustomRule, ...] = ()

    @classmethod
    def empty(cls) -> RuleConfig:
        return cls()


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    group: str
    source: str
    label: str
    matched: str
    location: Location

    def as_dict(self) -> dict[str, object]:
        return {
            "rule": self.rule_id,
            "group": self.group,
            "source": self.source,
            "label": self.label,
            "matched": self.matched,
            "location": self.location.as_dict(),
        }


def _phrase_regex(phrase: str) -> re.Pattern[str]:
    body = re.escape(phrase.lower()).replace("'", "['’]").replace(r"\ ", r"\s+")
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


_STOCK_REGEXES: dict[str, re.Pattern[str]] = {p: _phrase_regex(p) for p in STOCK_PHRASES}
_GROUP_REGEXES: dict[str, re.Pattern[str]] = {
    "dash_marks": _DASH_RE,
    "triad_lists": _TRIAD_RE,
}


def _strings(value: object, where: str, source: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuleFileError(f"{source}: {where} must be a list of strings")
    return value


def _check_rule(raw: object, index: int, source: str, seen: set[str], preset_names: tuple[str, ...]) -> CustomRule:
    where = f"[[rule]] #{index}"
    if not isinstance(raw, dict):
        raise RuleFileError(f"{source}: {where} must be a table")
    unknown = set(raw) - _RULE_KEYS
    if unknown:
        raise RuleFileError(f"{source}: {where} has unknown key(s): {', '.join(sorted(unknown))}")

    rule_id = raw.get("id")
    if not isinstance(rule_id, str) or not _RULE_ID_RE.fullmatch(rule_id):
        raise RuleFileError(
            f"{source}: {where} needs an id of lower-case letters, digits, '-' or '_' (max 64)"
        )
    if rule_id in seen:
        raise RuleFileError(f"{source}: duplicate rule id '{rule_id}'")
    seen.add(rule_id)

    kind = raw.get("kind")
    if kind not in RULE_KINDS:
        raise RuleFileError(f"{source}: rule '{rule_id}' kind must be one of: {', '.join(RULE_KINDS)}")

    pattern = raw.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        raise RuleFileError(f"{source}: rule '{rule_id}' needs a non-empty pattern")
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise RuleFileError(f"{source}: rule '{rule_id}' pattern is longer than {MAX_PATTERN_LENGTH} characters")

    label = raw.get("label", rule_id)
    if not isinstance(label, str) or len(label) > MAX_LABEL_LENGTH:
        raise RuleFileError(f"{source}: rule '{rule_id}' label must be a string of at most {MAX_LABEL_LENGTH} characters")

    presets: tuple[str, ...] | None = None
    if "presets" in raw:
        names = _strings(raw["presets"], f"rule '{rule_id}' presets", source)
        if not names:
            raise RuleFileError(f"{source}: rule '{rule_id}' presets must not be empty")
        bad = [name for name in names if name not in preset_names]
        if bad:
            raise RuleFileError(f"{source}: rule '{rule_id}' names unknown preset(s): {', '.join(bad)}")
        presets = tuple(names)

    ignore_case = raw.get("ignore_case", True)
    if not isinstance(ignore_case, bool):
        raise RuleFileError(f"{source}: rule '{rule_id}' ignore_case must be true or false")

    if kind == "phrase":
        regex = _phrase_regex(pattern.strip())
    else:
        try:
            regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            raise RuleFileError(f"{source}: rule '{rule_id}' regex does not compile: {exc}") from exc

    return CustomRule(rule_id, kind, pattern.strip(), label, presets, regex)


def parse_rule_config(data: dict[str, object], source: str = RULE_FILE_NAME) -> RuleConfig:
    """Validate parsed TOML data and return a :class:`RuleConfig`. Raises RuleFileError."""
    from .presets import PRESET_NAMES  # local import: presets imports this module

    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise RuleFileError(f"{source}: unknown top-level key(s): {', '.join(sorted(unknown))}")

    groups_raw = data.get("groups", {})
    if not isinstance(groups_raw, dict):
        raise RuleFileError(f"{source}: [groups] must be a table")
    groups: dict[str, bool] = {}
    for name, value in groups_raw.items():
        if name not in GROUP_NAMES:
            raise RuleFileError(f"{source}: unknown group '{name}'. Groups: {', '.join(GROUP_NAMES)}")
        if not isinstance(value, bool):
            raise RuleFileError(f"{source}: group '{name}' must be true or false")
        groups[name] = value

    disabled = _strings(data.get("disable_phrases", []), "disable_phrases", source)
    disabled_phrases = frozenset(phrase.strip().lower() for phrase in disabled)

    raw_rules = data.get("rule", [])
    if not isinstance(raw_rules, list):
        raise RuleFileError(f"{source}: [[rule]] must be an array of tables")
    seen: set[str] = set()
    rules = tuple(
        _check_rule(raw, index, source, seen, PRESET_NAMES)
        for index, raw in enumerate(raw_rules, start=1)
    )
    return RuleConfig(groups=groups, disabled_phrases=disabled_phrases, custom_rules=rules)


def load_rule_config(path: Path) -> RuleConfig:
    """Read ``path``. A missing file means no owner rules. Raises RuleFileError if broken."""
    if not path.exists():
        return RuleConfig.empty()
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise RuleFileError(f"{path.name} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise RuleFileError(f"cannot read {path.name}: {exc}") from exc
    return parse_rule_config(data, source=path.name)


def rule_hits(
    text: str,
    locator: Locator,
    groups: Iterable[str],
    config: RuleConfig,
    preset_name: str,
) -> list[RuleHit]:
    """Every built-in and owner-rule match in ``text``. The text is never changed."""
    hits: list[RuleHit] = []
    for group in groups:
        if group == "stock_phrases":
            for phrase, regex in _STOCK_REGEXES.items():
                if phrase in config.disabled_phrases:
                    continue
                hits.extend(_hits_from(regex, text, locator, "stock_phrases", "builtin", GROUP_LABELS[group]))
        elif group in _GROUP_REGEXES:
            hits.extend(_hits_from(_GROUP_REGEXES[group], text, locator, group, "builtin", GROUP_LABELS[group]))

    for rule in config.custom_rules:
        if rule.presets is not None and preset_name not in rule.presets:
            continue
        hits.extend(_hits_from(rule.regex, text, locator, rule.id, "custom", rule.label, group="custom"))

    hits.sort(key=lambda hit: (hit.location.start, hit.rule_id, hit.matched))
    return hits


def _hits_from(
    regex: re.Pattern[str],
    text: str,
    locator: Locator,
    rule_id: str,
    source: str,
    label: str,
    group: str | None = None,
) -> list[RuleHit]:
    out: list[RuleHit] = []
    for match in regex.finditer(text):
        if match.start() == match.end():
            continue
        out.append(
            RuleHit(
                rule_id=rule_id,
                group=group or rule_id,
                source=source,
                label=label,
                matched=match.group(0),
                location=locator.at(match.start(), match.end()),
            )
        )
    return out
