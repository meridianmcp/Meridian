"""Secret detection and redaction for Meridian (14491654).

Two complementary layers share this one pattern registry:

1. Local tool-output guard (``.claude/hooks/secret_guard.sh``/``.ps1``):
   ``scan()``/``redact()``/``is_sensitive_path()`` mask or block secret-shaped
   content in local filesystem reads / process output before it reaches model
   context.
2. DB write-path scrubbing (``meridian/db/__init__.py``,
   ``meridian/db/sprint_items.py``): ``check_for_secrets()`` hard-rejects any
   text destined for ``sprint_items.notes`` / ``task_log.description`` /
   ``decisions_pinned.body`` / ``project_notes.body`` before the SQL executes.

Design: a flat list of (name, compiled_regex) pairs so callers can easily
extend the pattern set without touching any of the functions below.

Usage
-----
    from meridian.secret_redaction import scan, redact, is_sensitive_path, check_for_secrets

    matches = scan(text)                      # -> list[SecretMatch]
    safe    = redact(text)                     # -> str with matches replaced by [REDACTED:name]
    blocked = is_sensitive_path(p)              # -> bool -- file path is itself sensitive
    check_for_secrets(text, context="note")     # raises ValueError on match (fail-closed)
"""
from __future__ import annotations

import bisect
import fnmatch
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, NamedTuple


# ---------------------------------------------------------------------------
# Pattern registry
# ---------------------------------------------------------------------------

class _Pattern(NamedTuple):
    name: str
    regex: "re.Pattern[str]"


def _p(name: str, pattern: str, flags: int = 0) -> _Pattern:
    return _Pattern(name, re.compile(pattern, flags))


# Each pattern is (human-readable-name, compiled-regex).
# re.DOTALL is used where the match could span a line (e.g. PEM blocks).
# Order matters only for cosmetics in redacted output -- all patterns are
# applied independently; the first match wins per span in redact().
SECRET_PATTERNS: list[_Pattern] = [
    # PEM private key blocks (multi-line). A block whose END line is missing
    # (truncated, or cut off by a length cap) runs to end-of-text so the key
    # body is still masked: fail closed, never leak a partial key (RT-HC-003).
    _p(
        "pem-private-key",
        r"-----BEGIN (?:[A-Z ]+)?PRIVATE KEY-----[\s\S]*?(?:-----END (?:[A-Z ]+)?PRIVATE KEY-----|\Z)",
        re.DOTALL,
    ),
    # AWS access key ID
    _p("aws-access-key-id", r"AKIA[0-9A-Z]{16}"),
    # Stripe LIVE secret key (not test keys)
    _p("stripe-live-key", r"sk_live_[A-Za-z0-9]{24,}"),
    # Meridian's own bearer token (ba31dedf). Underscore-delimited, unlike the
    # hyphenated openai-anthropic-key pattern below, so it needs its own entry --
    # a bare BEARER_TOKEN JSON/TOML/env pair (quoted name and value) otherwise slips
    # past every existing pattern here (confirmed: none of stripe-live-key,
    # openai-anthropic-key, or dotenv-credential's `KEY=value`-shaped match fire on
    # it, since the value is quoted/colon-joined, not a bare `=` assignment, and the
    # prefix uses `_` not `-`). See docs/meridian-storage-and-file-inspector-contract
    # investigation notes / ba31dedf launch-matrix sprint item for the audit that
    # found this gap across three independent config-generation code paths.
    _p("meridian-token", r"sk_meridian_[A-Za-z0-9_-]{16,}"),
    # GitHub tokens: personal access, OAuth, server-to-server, refresh, user
    _p("github-token", r"gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,}"),
    # Slack tokens
    _p("slack-token", r"xox[baprs]-[A-Za-z0-9][A-Za-z0-9_-]{5,}"),
    # OpenAI / Anthropic-style keys (sk-... with at least 20 chars total after sk-).
    # Modern OpenAI keys use sk-proj-..., sk-org-..., sk-None-...; Anthropic uses
    # sk-ant-...; all have dashes in the body, so allow [A-Za-z0-9-] in the suffix.
    # Underscores also occur inside provider key bodies (RT-HC-001), so the
    # suffix class includes "_" as well.
    _p("openai-anthropic-key", r"sk-[A-Za-z0-9_-]{20,}"),
    # Generic JWT (three base64url segments). The left edge must not be a
    # base64url character: without it, a long run of "eyJ" with no dot re-scans
    # the whole run from every "eyJ" start, which is quadratic (RT-HC-004 class).
    _p(
        "jwt",
        r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
    ),
    # dotenv-style KEY=value where KEY looks credential-like.
    # Captures the whole assignment so the value is redacted. The value must
    # be at least 6 non-whitespace/quote chars to avoid false positives on
    # trivial values (e.g. "KEY=yes", "FLAG=on"). Anchored to start-of-line
    # (via MULTILINE) so we don't accidentally match comments or mid-line prose.
    # Credential-named assignment, optionally prefixed by a shell verb
    # (``export NAME=``, ``set NAME=``, ``setx NAME=``, ``$env:NAME=``).
    # The credential-word test is a lookahead and the name is then consumed
    # in ONE greedy run: the earlier form ``[A-Z0-9_]*(?:KEY|...)[A-Z0-9_]*``
    # backtracked over every keyword occurrence on a long line, which made the
    # scan quadratic on repeated keywords (RT-HC-004). Value may be quoted.
    _p(
        "dotenv-credential",
        r"^[ \t]*(?:(?:export|setx?|\$env:)[ \t]+)?"
        r"(?=[A-Z0-9_]*?(?:SECRET|KEY|TOKEN|PASSWORD|PASSWD|CREDENTIAL|AUTH))"
        r"[A-Z0-9_]+[ \t]*=[ \t]*[\"']?[^\s\"']{6,}",
        re.IGNORECASE | re.MULTILINE,
    ),
    # Credential carriers that are not a bare NAME=value line (RT-HC-002).
    # Both are linear: each starts on a fixed delimiter and the name run is
    # consumed in one greedy pass after a lookahead.
    # A JSON / quoted-dict pair whose key is credential-named. Redaction only:
    # structured payloads carry keys such as "secret_name" or "monkey", so this
    # shape is not a hard-reject gate on DB writes (see check_for_secrets).
    _p(
        "credential-json-pair",
        r"[\"'](?=[A-Z0-9_-]*?(?:SECRET|KEY|TOKEN|PASSWORD|PASSWD|CREDENTIAL|AUTH))[A-Z0-9_-]+[\"'][ \t]*:[ \t]*[\"'][^\s\"']{6,}[\"']",
        re.IGNORECASE,
    ),
    # An Authorization header carrying a scheme (Basic / Bearer / Token / Digest).
    # Enforced on DB writes as well: prose rarely contains one.
    _p(
        "credential-carrier",
        r"\bauthorization[ \t]*[:=][ \t]*(?:basic|bearer|token|digest)[ \t]+[A-Za-z0-9+/=._~-]{8,}",
        re.IGNORECASE,
    ),
]


# Patterns that only report text no specific shape already claims (see scan()).
_FALLBACK_PATTERN_NAMES: frozenset[str] = frozenset({"credential-json-pair", "credential-carrier"})

# Patterns scanned for redaction but not hard-rejected by check_for_secrets().
_ADVISORY_ON_WRITE_PATTERN_NAMES: frozenset[str] = frozenset({"credential-json-pair"})


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------

@dataclass
class SecretMatch:
    """A single detected secret in a piece of text."""
    name: str        # pattern name, e.g. "stripe-live-key"
    start: int       # byte offset in text
    end: int         # exclusive end offset
    snippet: str     # truncated preview of the matched text (never the full value)


# ---------------------------------------------------------------------------
# Matching-time normalisation (RT-HC-006)
# ---------------------------------------------------------------------------

# Format (Cf) characters that are invisible in output but break a literal
# match when interleaved into a key: zero-width space/joiner/non-joiner, bidi
# marks and overrides, word joiner, soft hyphen, BOM, interlinear marks, tag
# characters. Matching runs on text with these removed.
_FORMAT_CHARS_RE = re.compile(
    r"[\u00ad\u0600-\u0605\u061c\u06dd\u070f\u08e2\u180e\u200b-\u200f"
    r"\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb"
    r"\U000110bd\U000110cd\U0001d173-\U0001d17a\U000e0001\U000e0020-\U000e007f]"
)

_FOLD_CACHE: dict[str, str] = {}
_FOLD_CACHE_MAX = 4096


def _fold_char(ch: str) -> str:
    """NFKC form of one character; format (Cf) characters fold to nothing."""
    cached = _FOLD_CACHE.get(ch)
    if cached is None:
        if unicodedata.category(ch) == "Cf":
            cached = ""
        else:
            cached = unicodedata.normalize("NFKC", ch)
        if len(_FOLD_CACHE) < _FOLD_CACHE_MAX:
            _FOLD_CACHE[ch] = cached
    return cached


def fold_for_matching(text: str) -> str:
    """Return *text* folded for pattern matching.

    Fullwidth and other compatibility look-alikes are mapped to their plain
    forms (NFKC) and format characters are dropped, so ``ｓｋ－`` and
    ``sk\\u200b-`` match the same patterns as ``sk-``. ASCII text, and text
    with no format characters that is already NFKC-stable, is returned as-is
    (fast path). Use only for matching: :func:`scan` maps offsets back onto
    the original text, so redaction never rewrites benign characters.
    """
    if not text or text.isascii():
        return text
    if _FORMAT_CHARS_RE.search(text) is None and unicodedata.normalize("NFKC", text) == text:
        return text
    return "".join([_fold_char(ch) for ch in text])


def _fold_with_offsets(text: str) -> tuple[str, list[int], list[int]]:
    """Folded text plus, per folded char, the original (start, end) index."""
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    for i, ch in enumerate(text):
        for out in _fold_char(ch):
            chars.append(out)
            starts.append(i)
            ends.append(i + 1)
    return "".join(chars), starts, ends


# ---------------------------------------------------------------------------
# scan(): find all matches, return list of SecretMatch
# ---------------------------------------------------------------------------

def scan(text: str) -> list[SecretMatch]:
    """Scan *text* for secrets matching any registered pattern.

    Returns a list of :class:`SecretMatch`, one per non-overlapping match
    across all patterns.  Overlapping spans are resolved by taking the first
    match in document order (shortest start wins; ties go to the pattern
    appearing first in SECRET_PATTERNS).

    Matching runs on :func:`fold_for_matching` output; the returned offsets
    and snippets always refer to the original *text*.

    The returned list is sorted by ``start`` offset.
    """
    if not text:
        return []

    # Collect all raw matches across all patterns (on the folded text).
    folded = fold_for_matching(text)
    primary: list[tuple[int, int, str]] = []  # (start, end, name)
    fallback: list[tuple[int, int, str]] = []
    for pat in SECRET_PATTERNS:
        bucket = fallback if pat.name in _FALLBACK_PATTERN_NAMES else primary
        for m in pat.regex.finditer(folded):
            bucket.append((m.start(), m.end(), pat.name))

    if not primary and not fallback:
        return []

    # A fallback (credential-carrier) span survives only where no specific
    # shape claims the same text, so a provider key inside an Authorization
    # header or a JSON pair is still reported under its own pattern name.
    raw = list(primary)
    if fallback:
        claimed: list[tuple[int, int]] = []  # disjoint, sorted union of primary spans
        for start, end, _ in sorted(primary):
            if claimed and start < claimed[-1][1]:
                claimed[-1] = (claimed[-1][0], max(claimed[-1][1], end))
            else:
                claimed.append((start, end))
        claimed_starts = [s for s, _ in claimed]
        for start, end, name in fallback:
            j = bisect.bisect_left(claimed_starts, end)
            if j > 0 and claimed[j - 1][1] > start:
                continue
            raw.append((start, end, name))

    # Sort by start offset. Stable, so ties between specific matches keep
    # SECRET_PATTERNS order (a fallback span never ties with one it overlaps).
    raw.sort(key=lambda t: t[0])

    # Merge overlapping/adjacent spans (keep first match at each position).
    merged: list[tuple[int, int, str]] = []
    last_end = -1
    for start, end, name in raw:
        if start < last_end:
            # Overlaps with the previous match -- skip (first match wins).
            continue
        merged.append((start, end, name))
        last_end = end

    # Map folded offsets back onto the original text (identity when the
    # folding changed nothing).
    if folded != text:
        _, starts, ends = _fold_with_offsets(text)
    else:
        starts = ends = None

    # Build SecretMatch objects with a safe preview snippet.
    results: list[SecretMatch] = []
    for start, end, name in merged:
        if starts is not None:
            orig_start, orig_end = starts[start], ends[end - 1]
        else:
            orig_start, orig_end = start, end
        matched = text[orig_start:orig_end]
        # Truncate to 12 chars + "..." to avoid leaking the full secret in logs.
        preview = matched[:12] + ("..." if len(matched) > 12 else "")
        results.append(SecretMatch(name=name, start=orig_start, end=orig_end, snippet=preview))

    return results


# ---------------------------------------------------------------------------
# redact(): replace matches with [REDACTED:name]
# ---------------------------------------------------------------------------

def redact(text: str) -> str:
    """Return *text* with every detected secret replaced by ``[REDACTED:name]``.

    This is a masking function (not a suppression function): callers still see
    the shape and surrounding context of the output, which is more useful for
    a local dev-tool guard than outright suppression.  The secrets themselves
    are never returned to the caller.
    """
    matches = scan(text)
    if not matches:
        return text

    parts: list[str] = []
    cursor = 0
    for m in matches:
        parts.append(text[cursor:m.start])
        parts.append(f"[REDACTED:{m.name}]")
        cursor = m.end
    parts.append(text[cursor:])
    return "".join(parts)


# ---------------------------------------------------------------------------
# scan_file() / scan_paths(): non-disclosing scan over files on disk
# (ff9d2963 — SECURITY-LAUNCH acceptance: "tracked-file scan emits
# paths/categories only"). Kept separate from scan()/redact() (pure
# string -> string/list, no I/O) so those stay usable in contexts (DB
# write-path guard, request bodies) that never touch the filesystem.
# ---------------------------------------------------------------------------

def scan_file(path: str) -> list[SecretMatch]:
    """Scan one file on disk for secrets. Never raises: a missing file,
    permission error, or file that isn't valid UTF-8 (a binary asset —
    image, video, compiled artifact) returns ``[]`` rather than propagating
    an exception, so a caller can point this at a large, mixed tracked-file
    tree without special-casing non-text files itself. Decoding uses
    ``errors="ignore"`` for the same reason -- a false-positive match inside
    a mangled binary decode is unlikely (the patterns require fairly long,
    specific-alphabet runs) and is one of `scan`'s regular false-positive
    considerations, not a new risk this introduces.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return []
    return scan(text)


def scan_paths(paths: "Iterable[str]") -> list[dict]:
    """Scan multiple files; return ONLY ``{"path": ..., "category": ...}``
    rows -- never the matched text, the surrounding line, or file content.

    This is the "non-disclosing scanner" ff9d2963's acceptance criteria ask
    for: a caller (a test, a CI step, a manual audit) can safely print,
    assert on, or log this return value without risking a live secret value
    leaking into test output, CI logs, or a bug report, unlike calling
    `scan()`/`scan_file()` and printing the raw `SecretMatch` list (whose
    `.snippet` field, while truncated, is still a real fragment of the
    matched text).
    """
    rows: list[dict] = []
    for path in paths:
        for m in scan_file(path):
            rows.append({"path": path, "category": m.name})
    return rows


# ---------------------------------------------------------------------------
# is_sensitive_path(): filename-based gate (reuses outputs_local patterns)
# ---------------------------------------------------------------------------

# Filename patterns (fnmatch, case-insensitive) for files that should NEVER
# be read by an AI tool.  Applied to the basename only.
# This mirrors + extends _SECRET_BASENAME_PATTERNS from outputs_local.py; kept
# in-repo rather than imported so this module has zero runtime deps.
_SENSITIVE_BASENAME_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.env",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.crt",
    "*.cer",
    "*.der",
    "id_rsa",
    "id_rsa.*",
    "id_dsa",
    "id_dsa.*",
    "id_ecdsa",
    "id_ecdsa.*",
    "id_ed25519",
    "id_ed25519.*",
    "*secret*",
    "*secrets*",
    "*credential*",
    "*credentials*",
    "*password*",
    "*passwd*",
    "*.vault",
    "vault.yaml",
    "vault.yml",
    "*.tfvars",
    "terraform.tfstate",
    "terraform.tfstate.backup",
    ".netrc",
    "netrc",
    "*.htpasswd",
    "*apikey*",
    "*api_key*",
    "*auth_key*",
    "*access_key*",
    "*private_key*",
    "*_token",
    "*_token.*",
    "token",
    "token.*",
    "meridian.toml",  # live Meridian credentials (AGENTS.md hard rules)
)

_SENSITIVE_PATTERNS_LOWER: tuple[str, ...] = tuple(
    p.lower() for p in _SENSITIVE_BASENAME_PATTERNS
)

# 55d48d69 fix round 1: a file with one of these final extensions is source,
# script, test or notebook code -- never a credential store, whatever its name
# says (secret_redaction.py, test_secret_redaction.py, refresh_token.py,
# secret_guard.ps1). A template of a credential file (.env.example,
# secrets.env.example) carries no secret either. Mirrored by
# .claude/hooks/secret_guard.{ps1,sh}.
_SAFE_SOURCE_EXTENSIONS: frozenset[str] = frozenset({
    "py", "pyi", "pyx", "ipynb", "ps1", "psm1", "psd1", "sh", "bash", "zsh", "fish", "bat", "cmd",
    "js", "mjs", "cjs", "ts", "tsx", "jsx", "mts", "cts", "go", "rs", "java", "kt", "kts", "scala",
    "rb", "php", "cs", "fs", "c", "h", "cc", "cpp", "cxx", "hpp", "swift", "m", "lua", "r", "jl",
    "dart", "ex", "exs", "erl", "hs", "ml", "clj", "groovy", "pl", "pm", "sql", "vue", "svelte", "awk",
})
_TEMPLATE_SUFFIXES: frozenset[str] = frozenset({"example", "sample", "template", "tmpl", "dist"})


def is_sensitive_path(path: str) -> bool:
    """Return True if *path*'s basename matches any known-sensitive file pattern.

    Case-insensitive fnmatch on the basename only, so it catches files at any
    depth.  False negatives (an unusual secret file name) are safer than false
    positives (blocking a legitimate file); the patterns below are deliberately
    conservative.
    """
    if not path:
        return False
    base_lower = os.path.basename(path.replace("\\", "/")).lower()
    dot = base_lower.rfind(".")
    if dot > 0:
        ext = base_lower[dot + 1:]
        if ext in _SAFE_SOURCE_EXTENSIONS or ext in _TEMPLATE_SUFFIXES:
            return False
    for pattern in _SENSITIVE_PATTERNS_LOWER:
        if fnmatch.fnmatch(base_lower, pattern):
            return True
    return False


# ---------------------------------------------------------------------------
# check_for_secrets(): hard-rejection gate for DB write paths
# ---------------------------------------------------------------------------

def check_for_secrets(text: str | None, context: str = "text") -> None:
    """Scan *text* against SECRET_PATTERNS; raise on a match (fail-closed).

    Used by DB write paths (``sprint_items.notes``, ``task_log.description``,
    ``decisions_pinned.body``, ``project_notes.body``) where silent
    redact-and-continue would mutate what a caller thought they wrote --
    a hard rejection forces the caller to notice and fix it instead.

    ``context`` is a short label inserted into the error message so the
    caller knows which DB column / field triggered the rejection
    (e.g. "task description", "decision body").

    Raises ``ValueError`` naming the matched pattern class when a match is
    found. Returns ``None`` when the text is clean. Do not catch and swallow
    the raised error -- let it propagate so the MCP layer surfaces it to the
    agent that tried to write the secret.
    """
    if not text:
        return
    folded = fold_for_matching(text)
    for pattern in SECRET_PATTERNS:
        if pattern.name in _ADVISORY_ON_WRITE_PATTERN_NAMES:
            continue
        if pattern.regex.search(folded):
            raise ValueError(
                f"Refusing to persist {context}: text matches a {pattern.name} pattern. "
                "Remove or redact the secret before writing to the database."
            )
