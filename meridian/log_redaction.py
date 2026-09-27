"""Credential and client-IP redaction for server-log text (7ef88e30).

Why this exists
---------------
``server_logs`` (the WARNING/ERROR ring-buffer written by
``server._MeridianDBLogHandler``) is process-global: ``get_server_logs`` and
``search_server_logs`` serve it to ANY authenticated caller, including every
hosted tenant.  The 1b4dc353 ``[mcp_auth] unrecognised token`` warning used to
log ``Authorization[:60]`` -- which, for ``"Bearer "`` plus a 55-char
``sk_meridian_`` token, is effectively the whole token -- plus the client IP
and a header dump carrying ``x-forwarded-for`` / ``cf-connecting-ip`` /
``fly-client-ip`` / ``signature*`` values.  So one tenant could read other
callers' near-complete bearer tokens and IP addresses.

Two layers, one module:

1. **Write time** -- :func:`fingerprint_authorization`, :func:`anonymize_ip`,
   :func:`first_forwarded_client_ip` and :func:`summarize_headers_for_log`
   build the diagnostic fields of the ``[mcp_auth]`` warning so it can still
   fingerprint a recurring unauthenticated caller (1b4dc353's goal) without
   carrying any secret material or a full IP address.
2. **Read time (defense in depth)** -- :func:`redact_log_text` /
   :func:`redact_log_row` mask credentials and IPs in rows *already* persisted
   (the prod ring-buffer and the DuckDB FTS sidecar) at the moment they are
   served, without mutating stored data.

Secret shapes other than Meridian's own token reuse
:data:`meridian.secret_redaction.SECRET_PATTERNS` (the same registry the DB
write-path guard and local tool-output hook use) rather than duplicating
those regexes here.

Everything in this module is pure (no I/O) and never raises on odd input --
it runs on the auth-failure path and on every log read.
"""
from __future__ import annotations

import hashlib
import ipaddress
import re
from typing import Any, Iterable, Mapping

from .secret_redaction import SECRET_PATTERNS

__all__ = [
    "MERIDIAN_TOKEN_PREFIX",
    "anonymize_ip",
    "first_forwarded_client_ip",
    "fingerprint_authorization",
    "fingerprint_token",
    "is_sensitive_header",
    "redact_log_row",
    "redact_log_text",
    "summarize_headers_for_log",
]

MERIDIAN_TOKEN_PREFIX = "sk_meridian_"

#: Hex chars of sha256(token) shown in a fingerprint.  40 bits: plenty to tell
#: recurring callers apart, useless for recovering a 256-bit random token.  It
#: is also a prefix of the DB's own ``_oauth_token_hash`` (plain sha256 hex),
#: so an operator can correlate a fingerprint with a stored/revoked token.
_FINGERPRINT_HEX_CHARS = 10

#: A non-Meridian token shows its first 3 chars only when it is at least this
#: long -- a short value (``"Bearer abc"``) must not be echoed back in full.
_MIN_LEN_FOR_PUBLIC_PREFIX = 16
_GENERIC_PREFIX_CHARS = 3

_KNOWN_AUTH_SCHEMES: dict[str, str] = {
    "bearer": "Bearer",
    "basic": "Basic",
    "token": "Token",
    "digest": "Digest",
    "dpop": "DPoP",
    "negotiate": "Negotiate",
    "apikey": "ApiKey",
}

#: ASCII (not U+2026) so the auth-failure log line never depends on the
#: console/stream encoding. Still never re-matched by the token regex.
_ELLIPSIS = "..."

_IPV4_ANON_PREFIX = 24
_IPV6_ANON_PREFIX = 48

# ---------------------------------------------------------------------------
# Header policy (write-time dump + read-time dict-repr masking)
# ---------------------------------------------------------------------------

#: Headers whose VALUES are logged: non-identifying and useful for telling
#: clients apart.
_HEADER_VALUE_ALLOWLIST: tuple[str, ...] = (
    "user-agent",
    "cf-ipcountry",
    "mcp-protocol-version",
    "accept",
    "content-type",
)

#: Headers dropped entirely (neither name nor value is logged): credentials,
#: client-IP carriers, and HTTP message-signature material.
_SENSITIVE_HEADER_NAMES: frozenset[str] = frozenset({
    "authorization",
    "proxy-authorization",
    "cookie",
    "x-forwarded-for",
    "cf-connecting-ip",
    "fly-client-ip",
    "true-client-ip",
    "x-real-ip",
    "forwarded",
    "signature",
    "signature-input",
})
_SENSITIVE_HEADER_SUBSTRINGS: tuple[str, ...] = ("token", "secret", "key", "auth")

#: Client-IP headers consulted (in order) for the anonymized ``fwd=`` field.
#: cf-connecting-ip first: when Cloudflare fronts Fly, fly-client-ip is a
#: Cloudflare edge address and only cf-connecting-ip is the real client.
_FORWARDED_IP_HEADERS: tuple[str, ...] = (
    "cf-connecting-ip",
    "true-client-ip",
    "fly-client-ip",
    "x-real-ip",
    "x-forwarded-for",
)

_HEADER_DUMP_CAP = 1000
_HEADER_VALUE_CAP = 200
_HEADER_NAME_CAP = 64


def is_sensitive_header(name: str) -> bool:
    """True if a header (or dict key) named *name* must never be logged."""
    n = (name or "").strip().lower()
    if not n:
        return False
    if n in _SENSITIVE_HEADER_NAMES:
        return True
    return any(s in n for s in _SENSITIVE_HEADER_SUBSTRINGS)


# ---------------------------------------------------------------------------
# Token fingerprinting
# ---------------------------------------------------------------------------

def _sha_prefix(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:_FINGERPRINT_HEX_CHARS]


def fingerprint_token(token: str | None) -> str:
    """Non-reversible description of a credential value.

    ``sk_meridian_... len=55 sha256=0123456789`` for a Meridian token; for any
    other value the first 3 chars are shown only when the value is at least
    16 chars long.  No part of a token after its public prefix is ever
    included.  ``"empty"`` for an empty value.
    """
    tok = token or ""
    if not tok:
        return "empty"
    if tok.startswith(MERIDIAN_TOKEN_PREFIX):
        prefix = MERIDIAN_TOKEN_PREFIX
    elif len(tok) >= _MIN_LEN_FOR_PUBLIC_PREFIX:
        prefix = tok[:_GENERIC_PREFIX_CHARS]
    else:
        prefix = ""
    parts = [f"{prefix}{_ELLIPSIS}"] if prefix else []
    parts.append(f"len={len(tok)}")
    parts.append(f"sha256={_sha_prefix(tok)}")
    return " ".join(parts)


def fingerprint_authorization(raw: str | None) -> str:
    """Fingerprint an ``Authorization`` header value for logging.

    Returns ``"(none)"`` when the header is absent/blank, otherwise
    ``"<Scheme>[<fingerprint_token(credential)>]"`` -- e.g.
    ``Bearer[sk_meridian_... len=55 sha256=0123456789]``.  Unknown schemes are
    reported as ``other`` (the scheme word is caller-controlled text and could
    itself be a pasted secret); a value with no scheme is ``no-scheme``.

    The format deliberately has no whitespace after the scheme, so
    :func:`redact_log_text`'s ``Bearer <value>`` rule never re-matches it.
    """
    value = (raw or "").strip()
    if not value:
        return "(none)"
    parts = value.split(None, 1)
    first = parts[0]
    if len(parts) == 2:
        scheme = _KNOWN_AUTH_SCHEMES.get(first.lower(), "other")
        credential = parts[1].strip()
    elif first.lower() in _KNOWN_AUTH_SCHEMES:
        scheme = _KNOWN_AUTH_SCHEMES[first.lower()]
        credential = ""
    else:
        scheme = "no-scheme"
        credential = first
    return f"{scheme}[{fingerprint_token(credential)}]"


# ---------------------------------------------------------------------------
# IP anonymization
# ---------------------------------------------------------------------------

def _anonymized_network(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    prefix: int | None = None,
) -> str:
    # int(ip) drops any IPv6 scope id ("%eth0"), which is caller-controlled text.
    if ip.version == 4:
        bits = _IPV4_ANON_PREFIX if prefix is None else min(prefix, _IPV4_ANON_PREFIX)
        return str(ipaddress.IPv4Network((int(ip), bits), strict=False))
    bits = _IPV6_ANON_PREFIX if prefix is None else min(prefix, _IPV6_ANON_PREFIX)
    return str(ipaddress.IPv6Network((int(ip), bits), strict=False))


def anonymize_ip(value: Any) -> str:
    """Reduce an IP to its /24 (IPv4) or /48 (IPv6) network.

    Accepts ``"1.2.3.4"``, ``"1.2.3.4:5678"``, ``"[2001:db8::1]:443"``,
    ``"fe80::1%eth0"`` and IPv4-mapped IPv6 (reported as IPv4).  Returns
    ``"unknown"`` for a missing value and ``"(invalid)"`` for anything that
    does not parse as an IP (a hostname, garbage) -- never the input itself.
    """
    if value is None:
        return "unknown"
    s = str(value).strip()[:100]
    if not s or s.lower() == "unknown":
        return "unknown"
    candidate = s
    if candidate.startswith("["):
        end = candidate.find("]")
        if end > 0:
            candidate = candidate[1:end]
    elif candidate.count(":") == 1 and "." in candidate:
        candidate = candidate.split(":", 1)[0]
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        return "(invalid)"
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return _anonymized_network(ip)


def _header_get(headers: Any, name: str) -> str | None:
    try:
        return headers.get(name)
    except Exception:  # noqa: BLE001 — diagnostics must never raise
        return None


def first_forwarded_client_ip(headers: Any) -> str | None:
    """Raw client IP from the first present proxy header (see
    :data:`_FORWARDED_IP_HEADERS`), or None.  For ``x-forwarded-for`` the
    left-most (original client) entry is used.  Callers must pass the result
    through :func:`anonymize_ip` before logging it."""
    for name in _FORWARDED_IP_HEADERS:
        v = _header_get(headers, name)
        if v and v.strip():
            return v.split(",", 1)[0].strip()
    return None


# ---------------------------------------------------------------------------
# Header dump
# ---------------------------------------------------------------------------

def summarize_headers_for_log(headers: Iterable[tuple[str, str]] | Mapping[str, str]) -> str:
    """Build the non-sensitive header dump for the ``[mcp_auth]`` warning.

    ``values={...}`` carries only the allowlisted headers
    (:data:`_HEADER_VALUE_ALLOWLIST`), each value itself passed through
    :func:`redact_log_text`; ``names=[...]`` lists every other header present
    by NAME only; headers matching :func:`is_sensitive_header` are omitted
    entirely.  Capped at 1000 chars against a hostile oversized request.
    """
    items: Iterable[tuple[str, str]]
    if isinstance(headers, Mapping):
        items = headers.items()
    else:
        items = headers
    values: dict[str, str] = {}
    names: list[str] = []
    seen: set[str] = set()
    try:
        for raw_name, raw_value in items:
            name = str(raw_name).strip().lower()[:_HEADER_NAME_CAP]
            if not name or is_sensitive_header(name):
                continue
            if name in _HEADER_VALUE_ALLOWLIST:
                if name not in values:
                    values[name] = redact_log_text(str(raw_value)[:_HEADER_VALUE_CAP])
            elif name not in seen:
                seen.add(name)
                names.append(name)
    except Exception:  # noqa: BLE001 — diagnostics must never raise
        pass
    dump = redact_log_text(f"values={values!r} names=[{', '.join(names)}]")
    if len(dump) > _HEADER_DUMP_CAP:
        dump = dump[:_HEADER_DUMP_CAP] + "...(truncated)"
    return dump


# ---------------------------------------------------------------------------
# Read-time redaction of arbitrary log text
# ---------------------------------------------------------------------------

# Quoted-string bodies. A backslash is ONLY consumed via the `\\.` branch, so
# the alternation is unambiguous and an unterminated string cannot trigger
# exponential backtracking.
_SQ_BODY = r"(?:\\.|[^'\\\n])*"
_DQ_BODY = r'(?:\\.|[^"\\\n])*'

# The legacy (pre-7ef88e30) 1b4dc353 field: ``[mcp_auth] ... raw='<Authorization[:60]>'``.
# Any scheme (Basic, raw token, ...) -- scoped to [mcp_auth] lines so other
# loggers' ``raw=`` fields are left alone.
_LEGACY_RAW_AUTH_RE = re.compile(
    r"(?P<head>\[mcp_auth\][^\n]*?\braw=)"
    rf"(?:'(?P<v1>{_SQ_BODY})'|\"(?P<v2>{_DQ_BODY})\")"
)
# RFC 6750 b64token after "Bearer ".
_BEARER_RE = re.compile(r"\b(?P<scheme>[Bb]earer|BEARER)(?P<sp>[ \t]+)(?P<v>[A-Za-z0-9\-._~+/]+=*)")
_MERIDIAN_TOKEN_RE = re.compile(re.escape(MERIDIAN_TOKEN_PREFIX) + r"[A-Za-z0-9_-]+")
# A quoted key/value pair as it appears in a repr()'d/JSON dict (the legacy
# header dump). The value may be cut off by the old 1000-char truncation, so
# end-of-text also closes it.
_QUOTED_KV_RE = re.compile(
    r"(?P<kq>['\"])(?P<k>[A-Za-z0-9_.-]{1,100})(?P=kq)(?P<sep>\s*:\s*)"
    rf"(?P<val>'{_SQ_BODY}(?:'|$)|\"{_DQ_BODY}(?:\"|$))"
)
_URL_USERINFO_RE = re.compile(
    r"\b(?P<head>[A-Za-z][A-Za-z0-9+.-]*://[^\s:/?#@'\"]+):(?P<pw>[^\s@/'\"]+)@"
)
_QUERY_SECRET_RE = re.compile(
    r"(?P<head>[?&;](?:access_token|refresh_token|id_token|token|api_key|apikey|key|"
    r"secret|client_secret|password|passwd|code|sig|signature)=)(?P<v>[^&\s'\"#]+)",
    re.IGNORECASE,
)

# Reused from secret_redaction.SECRET_PATTERNS, minus:
#  - meridian-token: handled above with a fingerprint instead of a blank mask;
#  - dotenv-credential: line-anchored `...TOKEN = value` also hits traceback
#    source lines (`    token = request.headers.get(...)`) in exc_text,
#    destroying diagnostics without protecting any secret.
_EXCLUDED_REUSED_PATTERNS = frozenset({"meridian-token", "dotenv-credential"})
_EXTRA_SECRET_PATTERNS: tuple[tuple[str, str, int], ...] = (
    # stripe-live-key covers sk_live_ only; add test + restricted keys.
    ("stripe-key", r"(?:sk_test|rk_live|rk_test)_[A-Za-z0-9]{16,}", 0),
    ("stripe-webhook-secret", r"whsec_[A-Za-z0-9+/=]{16,}", 0),
)


def _left_bounded(pattern: str, flags: int) -> "re.Pattern[str]":
    # SECRET_PATTERNS are unanchored; in log text "task-<uuid>" would match
    # the sk-... pattern mid-word. Require a non-alphanumeric left edge.
    return re.compile(rf"(?<![A-Za-z0-9])(?:{pattern})", flags)


_OTHER_SECRET_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = tuple(
    (p.name, _left_bounded(p.regex.pattern, p.regex.flags))
    for p in SECRET_PATTERNS
    if p.name not in _EXCLUDED_REUSED_PATTERNS
) + tuple((name, _left_bounded(pat, flags)) for name, pat, flags in _EXTRA_SECRET_PATTERNS)

#: Literal substrings at least one of which every match of the named pattern
#: must contain -- lets the common (clean) row skip that regex pass entirely.
#: A pattern with no entry here (e.g. one added to SECRET_PATTERNS later) is
#: always run, so a missing guard costs speed, never coverage.
_LITERAL_GUARDS: dict[str, tuple[str, ...]] = {
    "pem-private-key": ("-----BEGIN",),
    "aws-access-key-id": ("AKIA",),
    "stripe-live-key": ("sk_live_",),
    "github-token": ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"),
    "slack-token": ("xox",),
    "openai-anthropic-key": ("sk-",),
    "jwt": ("eyJ",),
    "stripe-key": ("sk_test_", "rk_live_", "rk_test_"),
    "stripe-webhook-secret": ("whsec_",),
}

# IPv4 with optional /prefix; not part of a longer dotted run (4-part version
# strings) and not glued to a preceding word char ("v1.2.3.4").
_IPV4_RE = re.compile(
    r"(?<![\w.])(?P<ip>(?:\d{1,3}\.){3}\d{1,3})(?:/(?P<plen>\d{1,2}))?(?!\.?\d)"
)
# IPv6 candidate (>= 2 colons), validated with ipaddress before replacing.
# Not preceded/followed by word chars, ':' or '.', so "file.py::symbol",
# timestamps and the "::ffff:" head of an IPv4-mapped address are skipped.
_IPV6_RE = re.compile(
    r"(?<![\w:.])(?P<ip>(?=[0-9A-Fa-f:]*:[0-9A-Fa-f:]*:)[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7})"
    r"(?:/(?P<plen>\d{1,3}))?(?![\w:.])"
)



def _looks_like_token(value: str) -> bool:
    """Heuristic so prose ("Bearer token", "Bearer authentication") is left
    alone while real credentials (mixed case, digits, punctuation) are not."""
    if len(value) >= 20:
        return True
    if len(value) < 8:
        return False
    if any(c.isdigit() or c in "-._~+/=" for c in value):
        return True
    return value[1:] != value[1:].lower()


def _legacy_raw_repl(m: "re.Match[str]") -> str:
    v = m.group("v1") if m.group("v1") is not None else m.group("v2")
    if v == "(none)":
        return m.group(0)
    return f"{m.group('head')}{fingerprint_authorization(v)}"


def _bearer_repl(m: "re.Match[str]") -> str:
    v = m.group("v")
    if not _looks_like_token(v):
        return m.group(0)
    return f"Bearer[{fingerprint_token(v)}]"


def _meridian_token_repl(m: "re.Match[str]") -> str:
    return f"{MERIDIAN_TOKEN_PREFIX}{_ELLIPSIS}{_sha_prefix(m.group(0))}"


def _quoted_kv_repl(m: "re.Match[str]") -> str:
    if not is_sensitive_header(m.group("k")):
        return m.group(0)
    vq = m.group("val")[0]
    return f"{m.group('kq')}{m.group('k')}{m.group('kq')}{m.group('sep')}{vq}[REDACTED]{vq}"


def _ip_repl(m: "re.Match[str]") -> str:
    try:
        ip = ipaddress.ip_address(m.group("ip"))
    except ValueError:
        return m.group(0)
    if ip.is_loopback or ip.is_unspecified:
        return m.group(0)
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    plen = m.group("plen")
    prefix = int(plen) if plen is not None else None
    return _anonymized_network(ip, prefix)


def _redact_other_secrets(text: str) -> str:
    for name, regex in _OTHER_SECRET_PATTERNS:
        guards = _LITERAL_GUARDS.get(name)
        if guards is not None and not any(g in text for g in guards):
            continue
        text = regex.sub(f"[REDACTED:{name}]", text)
    return text


def redact_log_text(text: Any) -> Any:
    """Mask credentials and client IPs in a log message / traceback.

    - legacy ``[mcp_auth] ... raw='...'`` values -> :func:`fingerprint_authorization`;
    - ``Bearer <token>`` -> ``Bearer[<fingerprint>]``;
    - ``sk_meridian_<secret>`` -> ``sk_meridian_...<sha256[:10]>``;
    - values of sensitive keys in dict reprs (``'x-forwarded-for': ...``,
      ``'signature': ...``) -> ``'[REDACTED]'``;
    - URL userinfo passwords and secret-named query parameters;
    - other known secret shapes (Stripe, GitHub, Slack, OpenAI/Anthropic,
      AWS, JWT, PEM keys) -> ``[REDACTED:<kind>]``;
    - IPv4 -> ``a.b.c.0/24``, IPv6 -> ``/48`` (loopback/unspecified kept).

    Idempotent.  Non-``str`` input (``None``) is returned unchanged.
    """
    if not isinstance(text, str) or not text:
        return text
    out = _LEGACY_RAW_AUTH_RE.sub(_legacy_raw_repl, text)
    out = _BEARER_RE.sub(_bearer_repl, out)
    out = _MERIDIAN_TOKEN_RE.sub(_meridian_token_repl, out)
    out = _QUOTED_KV_RE.sub(_quoted_kv_repl, out)
    out = _URL_USERINFO_RE.sub(r"\g<head>:[REDACTED]@", out)
    out = _QUERY_SECRET_RE.sub(r"\g<head>[REDACTED]", out)
    out = _redact_other_secrets(out)
    out = _IPV6_RE.sub(_ip_repl, out)
    out = _IPV4_RE.sub(_ip_repl, out)
    return out


_REDACTED_ROW_FIELDS: tuple[str, ...] = ("message", "exc_text")


def redact_log_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Copy of a ``server_logs`` row / search hit with ``message`` and
    ``exc_text`` passed through :func:`redact_log_text`.  The input row is
    not mutated."""
    out = dict(row)
    for field in _REDACTED_ROW_FIELDS:
        if field in out:
            out[field] = redact_log_text(out[field])
    return out
