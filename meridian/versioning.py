"""Version-label rules for moving sprint items between versions.

Sprint-item versions are free-text labels (``v1.9``, ``v2.0``, ``0.2``,
``v0.2.x``, even ``current sprint v0.2``); nothing in the schema constrains
them. The dashboard's "move to the next version" action still needs a
deterministic answer to "what comes after ``v2.2``?", so this module owns the
one rule for it and the one rule for what a label may look like.

The rule is deliberately narrow: increment the LAST numeric component and keep
everything else exactly as written. Anything this module cannot parse
unambiguously returns ``None`` from :func:`next_version` -- callers then ask the
human for a version instead of guessing one.

``meridian/static/dashboard-versions.ts`` implements the identical rule for the
browser (it previews the number on the button before the server is asked).
Both implementations are exercised against ``tests/fixtures/next_version_cases.json``
so they cannot drift: change a case there and both test suites must agree.
"""
from __future__ import annotations

import re
import unicodedata

#: Longest label accepted for a version. Real labels are a handful of
#: characters; the cap only exists so a pasted blob cannot land in the board.
MAX_VERSION_LENGTH = 64

#: A component with more digits than this is refused by :func:`next_version`.
#: JavaScript numbers lose integer precision above 2**53, so letting Python
#: increment a 30-digit component would make the two implementations disagree.
_MAX_COMPONENT_DIGITS = 9

# Only ASCII digits (``re.ASCII`` plus explicit classes, because ``\d`` would
# accept Arabic-Indic and full-width digits in Python but not in JavaScript).
# Shape: optional ``v``/``V`` prefix, dot-separated numbers, optional trailing
# ``.x`` wildcard (``v0.2.x`` means "the 0.2 line").
_VERSION_RE = re.compile(
    r"(?P<prefix>[vV]?)(?P<nums>[0-9]+(?:\.[0-9]+)*)(?P<wild>\.[xX])?",
    re.ASCII,
)

# Only these characters are trimmed from the ends before parsing, so Python's
# str.strip() (which also strips \x1c-\x1f, \x85, ...) and JavaScript's
# String.prototype.trim() (which also strips U+FEFF) cannot disagree.
_ASCII_WS = " \t\r\n"

# Unicode categories that must never appear in a label: control characters
# (Cc), invisible format characters such as zero-width joiners and bidi
# overrides (Cf), and line/paragraph separators (Zl, Zp). Any space separator
# (Zs) other than a plain ASCII space is refused too, so a pasted non-breaking
# space cannot hide at the end of a label.
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def next_version(version: str | None) -> str | None:
    """Return the version that follows ``version``, or ``None`` if unsure.

    The last numeric component is incremented; prefix, separators, the other
    components, a trailing ``.x`` wildcard and zero-padding width are kept::

        v2.1   -> v2.2        v2.9     -> v2.10      2.1   -> 2.2
        v2     -> v3          v0.2.x   -> v0.3.x     1.0.0 -> 1.0.1
        2026.09 -> 2026.10

    Returns ``None`` for anything else (``current sprint v0.2``, ``v2.1-beta``,
    ``v2.x.1``, an empty value, a component longer than nine digits, ...): the
    caller must ask for an explicit version rather than have one invented.
    """
    if not isinstance(version, str):
        return None
    text = version.strip(_ASCII_WS)
    if not text or len(text) > MAX_VERSION_LENGTH:
        return None
    match = _VERSION_RE.fullmatch(text)
    if match is None:
        return None
    parts = match.group("nums").split(".")
    if any(len(p) > _MAX_COMPONENT_DIGITS for p in parts):
        return None
    last = parts[-1]
    bumped = str(int(last) + 1)
    if len(last) > 1 and last[0] == "0":
        # Keep calendar-style padding: 2026.09 -> 2026.10, 1.008 -> 1.009.
        bumped = bumped.zfill(len(last))
    parts[-1] = bumped
    result = match.group("prefix") + ".".join(parts) + (match.group("wild") or "")
    return result if len(result) <= MAX_VERSION_LENGTH else None


def validate_version_label(value: object) -> str:
    """Return ``value`` trimmed, or raise ``ValueError`` if it is not a usable label.

    Used for a version a human typed (the "specific version" option): it must
    be a non-empty string of at most :data:`MAX_VERSION_LENGTH` characters with
    no control, invisible-format or line-separator characters. Interior ASCII
    spaces are allowed because existing boards already carry labels such as
    ``current sprint v0.2``.
    """
    if not isinstance(value, str):
        raise ValueError("version must be a string")
    label = value.strip(_ASCII_WS)
    if not label:
        raise ValueError("version must not be empty")
    if len(label) > MAX_VERSION_LENGTH:
        raise ValueError(f"version must be at most {MAX_VERSION_LENGTH} characters")
    for ch in label:
        category = unicodedata.category(ch)
        if category in _FORBIDDEN_CATEGORIES or (category == "Zs" and ch != " "):
            raise ValueError("version must not contain control or invisible characters")
    return label
