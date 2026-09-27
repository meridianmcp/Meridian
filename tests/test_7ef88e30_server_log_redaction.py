"""7ef88e30 — server_logs must never serve bearer tokens or client IPs.

The 1b4dc353 ``[mcp_auth] unrecognised token`` warning used to log
``Authorization[:60]`` (effectively the whole ``Bearer sk_meridian_...``
token), the client IP and a header dump carrying x-forwarded-for /
cf-connecting-ip / signature headers.  Those rows land in the process-global
``server_logs`` ring-buffer that ``get_server_logs`` / ``search_server_logs``
serve to ANY authenticated tenant.

Covers:
- write time: the /mcp auth-failure warning carries a non-reversible token
  fingerprint, /24-anonymized networks, header NAMES + a value allowlist;
- read time: legacy rows already stored (ring-buffer + DuckDB sidecar) come
  back redacted from both MCP handlers, without mutating stored data;
- unit edge cases for fingerprinting, IP anonymization and redact_log_text.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time

import pytest

from meridian import db as db_module
from meridian.log_redaction import (
    anonymize_ip,
    fingerprint_authorization,
    fingerprint_token,
    first_forwarded_client_ip,
    is_sensitive_header,
    redact_log_row,
    redact_log_text,
    summarize_headers_for_log,
)

_PREFIX = "sk_meridian_"


def _new_token() -> tuple[str, str]:
    """(full token, secret part) — same shape as db.create_api_token()."""
    secret = secrets.token_urlsafe(32)
    assert len(secret) == 43
    return f"{_PREFIX}{secret}", secret


def _assert_no_fragment(text: str, secret: str, n: int = 12) -> None:
    """No n-char window of *secret* may appear anywhere in *text*."""
    for i in range(len(secret) - n + 1):
        frag = secret[i:i + n]
        assert frag not in text, f"secret fragment #{i} leaked into log text"


def _sha10(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:10]


# ---------------------------------------------------------------------------
# Write time — the real /mcp auth-failure path
# ---------------------------------------------------------------------------


def _mcp_auth_messages(caplog) -> list[str]:
    return [
        rec.getMessage() for rec in caplog.records
        if rec.name == "meridian.mcp_auth" and "unrecognised token" in rec.getMessage()
    ]


def test_unauth_mcp_warning_fingerprints_token_and_anonymizes_ips(client, caplog):
    tok, secret = _new_token()
    with caplog.at_level(logging.WARNING, logger="meridian.mcp_auth"):
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={
                "Authorization": f"Bearer {tok}",
                "User-Agent": "probe-agent/7.1",
                "X-Diagnostic-Marker": "marker-value-7ef88e30",
                "X-Forwarded-For": "198.51.100.23, 10.9.8.7",
                "CF-Connecting-IP": "203.0.113.77",
                "Fly-Client-IP": "192.0.2.44",
                "CF-IPCountry": "NL",
                "Signature": "sig1=:c2lnbmF0dXJlLXZhbHVlLWxlYWs=:",
                "Signature-Input": 'sig1=("@authority");keyid="leaky-key-id"',
                "Signature-Agent": '"https://bot.example"',
                "X-Api-Key": "apikey-value-should-not-appear",
                "Cookie": "session=cookie-value-should-not-appear",
            },
        )
    assert r.status_code == 401
    msgs = _mcp_auth_messages(caplog)
    assert len(msgs) == 1, msgs
    msg = msgs[0]

    # Fingerprint present: scheme, public prefix, length, sha256[:10].
    assert f"auth={fingerprint_authorization('Bearer ' + tok)}" in msg
    assert f"Bearer[{_PREFIX}... len={len(tok)} sha256={_sha10(tok)}]" in msg
    # ...and nothing of the secret part.
    _assert_no_fragment(msg, secret)

    # No full IP from any source; the real client (cf-connecting-ip wins
    # over fly-client-ip / x-forwarded-for) survives only as its /24.
    for full_ip in ("198.51.100.23", "10.9.8.7", "203.0.113.77", "192.0.2.44"):
        assert full_ip not in msg
    assert "fwd=203.0.113.0/24" in msg

    # UA + allowlisted values kept; other headers by NAME only.
    assert "probe-agent/7.1" in msg
    assert "'cf-ipcountry': 'NL'" in msg
    assert "x-diagnostic-marker" in msg
    assert "marker-value-7ef88e30" not in msg
    assert "signature-agent" in msg
    assert "bot.example" not in msg

    # Signature / credential / IP-carrier headers dropped entirely.
    lowered = msg.lower()
    for dropped in ("cookie", "x-forwarded-for", "cf-connecting-ip", "fly-client-ip",
                    "x-api-key", "signature-input", "'signature'"):
        assert dropped not in lowered
    for leaked_value in ("c2lnbmF0dXJl", "leaky-key-id", "apikey-value", "cookie-value"):
        assert leaked_value not in msg


def test_unauth_mcp_warning_short_and_non_bearer_credentials(client, caplog):
    """A short token is never echoed back; a non-Bearer scheme keeps only
    its normalized scheme name + fingerprint."""
    with caplog.at_level(logging.WARNING, logger="meridian.mcp_auth"):
        client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                    headers={"Authorization": "Bearer Zq9xW"})
        client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
                    headers={"Authorization": "Basic dXNlcjpodW50ZXIyLXBhc3N3b3Jk"})
    msgs = _mcp_auth_messages(caplog)
    assert len(msgs) == 2, msgs
    assert f"auth=Bearer[len=5 sha256={_sha10('Zq9xW')}]" in msgs[0]
    assert "Zq9xW" not in msgs[0]
    assert "auth=Basic[dXN... len=28 " in msgs[1]
    _assert_no_fragment(msgs[1], "dXNlcjpodW50ZXIyLXBhc3N3b3Jk", n=6)


# ---------------------------------------------------------------------------
# Read time — legacy rows already in the ring-buffer / DuckDB sidecar
# ---------------------------------------------------------------------------


def _legacy_row_message(tok: str) -> str:
    """Exactly what the pre-7ef88e30 1b4dc353 warning wrote."""
    raw = f"Bearer {tok}"[:60]
    headers = {
        "host": "usemeridian.us",
        "x-forwarded-for": "198.51.100.23, 10.9.8.7",
        "cf-connecting-ip": "2001:db8:abcd:12::77",
        "fly-client-ip": "203.0.113.77",
        "signature": "sig1=:c2lnbmF0dXJlLXZhbHVlLWxlYWs=:",
        "signature-input": 'sig1=("@authority");keyid="leaky-key-id"',
        "user-agent": "python-httpx/0.28.1",
    }
    return (
        f"[mcp_auth] unrecognised token raw={raw!r} ua='python-httpx/0.28.1' "
        f"ip=203.0.113.77 headers={headers!r}"
    )


def _assert_row_redacted(entry: dict, secret: str, exc_secret: str) -> None:
    blob = f"{entry.get('message')}\n{entry.get('exc_text')}"
    _assert_no_fragment(blob, secret)
    _assert_no_fragment(blob, exc_secret)
    for full_ip in ("198.51.100.23", "10.9.8.7", "203.0.113.77", "2001:db8:abcd:12::77"):
        assert full_ip not in blob
    assert "203.0.113.0/24" in blob
    assert "c2lnbmF0dXJl" not in blob
    assert "leaky-key-id" not in blob
    # Diagnostics survive.
    assert "[mcp_auth] unrecognised token" in blob
    assert "python-httpx/0.28.1" in blob


async def _seed_leaky_rows(db) -> tuple[str, str]:
    tok, secret = _new_token()
    exc_tok, exc_secret = _new_token()
    await db_module.record_server_log(
        db, level="WARNING", logger="meridian.mcp_auth", message=_legacy_row_message(tok),
    )
    await db_module.record_server_log(
        db,
        level="EXCEPTION",
        logger="meridian.server",
        message="upstream call failed for 198.51.100.23",
        exc_text=(
            "Traceback (most recent call last):\n"
            f"httpx.HTTPStatusError: 401 for Authorization: Bearer {exc_tok} "
            "from 203.0.113.77"
        ),
    )
    return secret, exc_secret


@pytest.mark.asyncio
async def test_get_server_logs_redacts_seeded_legacy_rows(db):
    from meridian.mcp.handlers.session_tools import handle_get_server_logs

    secret, exc_secret = await _seed_leaky_rows(db)
    result = await handle_get_server_logs(
        args={"limit": 500}, db=db, data_dir="", tenant=None, _mcp_tenant_id=None,
    )
    entries = result["entries"]
    assert result["count"] == len(entries) >= 2
    auth_rows = [e for e in entries if e["logger"] == "meridian.mcp_auth"]
    exc_rows = [e for e in entries if e["level"] == "EXCEPTION"]
    assert auth_rows and exc_rows
    for e in entries:
        _assert_no_fragment(json.dumps(e), secret)
        _assert_no_fragment(json.dumps(e), exc_secret)
    auth = auth_rows[0]
    assert "raw=Bearer[sk_meridian_... len=53 sha256=" in auth["message"]
    assert "'x-forwarded-for': '[REDACTED]'" in auth["message"]
    assert "'signature': '[REDACTED]'" in auth["message"]
    merged = {"message": auth["message"] + "\n" + exc_rows[0]["message"],
              "exc_text": exc_rows[0]["exc_text"]}
    _assert_row_redacted(merged, secret, exc_secret)
    assert "Bearer[sk_meridian_... len=55 sha256=" in exc_rows[0]["exc_text"]

    # Read-time only: the stored rows are NOT mutated.
    stored = await db_module.get_server_logs(db, limit=500)
    assert any(secret[:20] in (r["message"] or "") for r in stored)


@pytest.mark.asyncio
async def test_get_server_logs_filters_still_redact(db):
    from meridian.mcp.handlers.session_tools import handle_get_server_logs

    secret, exc_secret = await _seed_leaky_rows(db)
    for args in ({"level_filter": "WARNING"}, {"module_filter": "mcp_auth"},
                 {"level_filter": "EXCEPTION"}, {"since": "2000-01-01 00:00:00"}):
        result = await handle_get_server_logs(
            args=args, db=db, data_dir="", tenant=None, _mcp_tenant_id=None,
        )
        assert result["entries"], args
        blob = json.dumps(result)
        _assert_no_fragment(blob, secret)
        _assert_no_fragment(blob, exc_secret)


@pytest.mark.asyncio
async def test_search_server_logs_redacts_hits(db, tmp_path):
    pytest.importorskip("duckdb")
    from meridian.mcp.handlers.session_tools import handle_search_server_logs

    secret, exc_secret = await _seed_leaky_rows(db)
    for query in ("unrecognised token", "meridian", "HTTPStatusError", "upstream"):
        result = await handle_search_server_logs(
            args={"query": query, "limit": 50},
            db=db, data_dir=str(tmp_path), tenant=None, _mcp_tenant_id=None,
        )
        assert result["count"] >= 1, query
        blob = json.dumps(result)
        _assert_no_fragment(blob, secret)
        _assert_no_fragment(blob, exc_secret)
        for full_ip in ("198.51.100.23", "203.0.113.77", "2001:db8:abcd:12::77"):
            assert full_ip not in blob
    # Diagnostics survive redaction of the served hit.
    result = await handle_search_server_logs(
        args={"query": "unrecognised token"},
        db=db, data_dir=str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    hit = next(h for h in result["hits"] if h["logger"] == "meridian.mcp_auth")
    _assert_row_redacted({"message": hit["message"], "exc_text": None}, secret, exc_secret)
    assert "raw=Bearer[sk_meridian_... len=53 sha256=" in hit["message"]
    # Read-time only: the stored rows are NOT mutated.
    stored = await db_module.get_server_logs(db, limit=500)
    assert any(secret[:20] in (r["message"] or "") for r in stored)


@pytest.mark.asyncio
async def test_server_log_checkpoint_exposes_no_row_text(db):
    """The checkpoint index is ids/timestamps only — nothing to redact."""
    from meridian.mcp.handlers.session_tools import (
        handle_get_server_log_checkpoint,
        handle_get_server_logs,
    )

    secret, exc_secret = await _seed_leaky_rows(db)
    await handle_get_server_logs(
        args={"limit": 500}, db=db, data_dir="", tenant=None, _mcp_tenant_id=None,
    )
    result = await handle_get_server_log_checkpoint(
        args={}, db=db, data_dir="", tenant=None, _mcp_tenant_id=None,
    )
    blob = json.dumps(result)
    _assert_no_fragment(blob, secret)
    _assert_no_fragment(blob, exc_secret)
    assert "203.0.113" not in blob


# ---------------------------------------------------------------------------
# Unit: fingerprinting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_fingerprint_authorization_absent(raw):
    assert fingerprint_authorization(raw) == "(none)"


def test_fingerprint_meridian_token_matches_db_hash_prefix():
    from meridian.routes.oauth import _oauth_token_hash

    for _ in range(20):
        tok, secret = _new_token()
        fp = fingerprint_authorization(f"Bearer {tok}")
        assert fp == f"Bearer[sk_meridian_... len=55 sha256={_oauth_token_hash(tok)[:10]}]"
        _assert_no_fragment(fp, secret, n=4)


def test_fingerprint_bearer_case_and_whitespace():
    tok, secret = _new_token()
    assert fingerprint_authorization(f"bearer   {tok}  ") == fingerprint_authorization(f"Bearer {tok}")
    assert fingerprint_authorization("Bearer") == "Bearer[empty]"


def test_fingerprint_non_bearer_scheme_and_unknown_scheme():
    basic = fingerprint_authorization("Basic dXNlcjpodW50ZXIyLXBhc3N3b3Jk")
    assert basic.startswith("Basic[dXN... len=28 sha256=")
    # Unknown scheme words are caller-controlled text: never echoed.
    other = fingerprint_authorization("Sk_live_SchemeWordSecret abcdefghijklmnopqrstuvwxyz")
    assert other.startswith("other[abc... len=26 sha256=")
    assert "SchemeWordSecret" not in other
    assert "defghijklmnop" not in other


def test_fingerprint_short_and_schemeless_tokens():
    # Shorter than the 16-char public-prefix threshold: no prefix at all.
    assert fingerprint_token("abc12345") == f"len=8 sha256={_sha10('abc12345')}"
    assert fingerprint_authorization("Bearer abc") == f"Bearer[len=3 sha256={_sha10('abc')}]"
    # Meridian prefix is public even on a truncated/short token.
    short = fingerprint_authorization("Bearer sk_meridian_Xy9")
    assert short == f"Bearer[sk_meridian_... len=15 sha256={_sha10('sk_meridian_Xy9')}]"
    # A bare token with no scheme.
    tok, secret = _new_token()
    bare = fingerprint_authorization(tok)
    assert bare.startswith("no-scheme[sk_meridian_... len=55 ")
    _assert_no_fragment(bare, secret, n=4)
    assert fingerprint_token("") == "empty"


# ---------------------------------------------------------------------------
# Unit: IP anonymization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    ("203.0.113.77", "203.0.113.0/24"),
    (" 203.0.113.77 ", "203.0.113.0/24"),
    ("203.0.113.77:4431", "203.0.113.0/24"),
    ("2001:db8:abcd:12::77", "2001:db8:abcd::/48"),
    ("[2001:db8:abcd:12::77]:443", "2001:db8:abcd::/48"),
    ("fe80::1%eth0", "fe80::/48"),
    ("::ffff:198.51.100.23", "198.51.100.0/24"),
    ("not-an-ip", "(invalid)"),
    ("testclient", "(invalid)"),
    ("999.1.1.1", "(invalid)"),
    ("1.2.3", "(invalid)"),
    ("<script>alert(1)</script>", "(invalid)"),
    ("x" * 5000, "(invalid)"),
    ("", "unknown"),
    (None, "unknown"),
    ("unknown", "unknown"),
])
def test_anonymize_ip(value, expected):
    assert anonymize_ip(value) == expected


def test_first_forwarded_client_ip_precedence():
    assert first_forwarded_client_ip({}) is None
    assert first_forwarded_client_ip({"x-forwarded-for": " 198.51.100.23 , 10.0.0.1"}) == "198.51.100.23"
    assert first_forwarded_client_ip({
        "x-forwarded-for": "198.51.100.23", "fly-client-ip": "192.0.2.44",
    }) == "192.0.2.44"
    assert first_forwarded_client_ip({
        "x-forwarded-for": "198.51.100.23", "fly-client-ip": "192.0.2.44",
        "cf-connecting-ip": "203.0.113.77",
    }) == "203.0.113.77"
    assert first_forwarded_client_ip(object()) is None  # never raises


# ---------------------------------------------------------------------------
# Unit: header dump
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "authorization", "Proxy-Authorization", "cookie", "x-forwarded-for",
    "cf-connecting-ip", "fly-client-ip", "true-client-ip", "x-real-ip", "forwarded",
    "signature", "signature-input", "x-api-key", "x-auth-token", "x-client-secret",
    "x-meridian-token",
])
def test_is_sensitive_header(name):
    assert is_sensitive_header(name)


@pytest.mark.parametrize("name", [
    "user-agent", "accept", "host", "signature-agent", "mcp-protocol-version", "", "x-request-id",
])
def test_is_not_sensitive_header(name):
    assert not is_sensitive_header(name)


def test_summarize_headers_for_log_contract():
    tok, secret = _new_token()
    dump = summarize_headers_for_log([
        ("user-agent", f"evil-client {tok}"),
        ("accept", "application/json"),
        ("content-type", "application/json"),
        ("mcp-protocol-version", "2025-06-18"),
        ("cf-ipcountry", "DE"),
        ("host", "usemeridian.us"),
        ("x-diagnostic-marker", "marker-value"),
        ("x-diagnostic-marker", "second-value"),
        ("x-forwarded-for", "198.51.100.23"),
        ("x-auth-token", "tokval"),
        ("cookie", "c=1"),
    ])
    assert "'accept': 'application/json'" in dump
    assert "'content-type': 'application/json'" in dump
    assert "'mcp-protocol-version': '2025-06-18'" in dump
    assert "'cf-ipcountry': 'DE'" in dump
    assert "names=[host, x-diagnostic-marker]" in dump
    assert "marker-value" not in dump and "second-value" not in dump
    assert "x-forwarded-for" not in dump and "198.51.100" not in dump
    assert "x-auth-token" not in dump and "tokval" not in dump
    assert "cookie" not in dump
    # A token smuggled into an allowlisted value is fingerprinted.
    assert "evil-client sk_meridian_..." in dump
    _assert_no_fragment(dump, secret)


def test_summarize_headers_for_log_caps_size_and_accepts_mapping():
    many = [(f"x-custom-header-{i:04d}", "v") for i in range(500)]
    dump = summarize_headers_for_log(many)
    assert dump.endswith("...(truncated)")
    assert len(dump) == 1000 + len("...(truncated)")
    assert summarize_headers_for_log({"User-Agent": "ua/1"}) == "values={'user-agent': 'ua/1'} names=[]"


# ---------------------------------------------------------------------------
# Unit: redact_log_text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", 42])
def test_redact_log_text_passthrough(value):
    assert redact_log_text(value) == value


def test_redact_legacy_row_is_complete_and_idempotent():
    tok, secret = _new_token()
    out = redact_log_text(_legacy_row_message(tok))
    _assert_no_fragment(out, secret)
    assert "raw=Bearer[sk_meridian_... len=53 sha256=" in out
    assert "ip=203.0.113.0/24" in out
    assert "'x-forwarded-for': '[REDACTED]'" in out
    assert "'cf-connecting-ip': '[REDACTED]'" in out
    assert "'fly-client-ip': '[REDACTED]'" in out
    assert "'signature': '[REDACTED]'" in out
    assert "'signature-input': '[REDACTED]'" in out
    assert "'host': 'usemeridian.us'" in out
    assert redact_log_text(out) == out


def test_redact_legacy_raw_none_and_non_bearer():
    assert redact_log_text("[mcp_auth] unrecognised token raw='(none)' ua=''") == (
        "[mcp_auth] unrecognised token raw='(none)' ua=''"
    )
    out = redact_log_text('[mcp_auth] unrecognised token raw="Basic dXNlcjpodW50ZXIyLXBhc3N3b3Jk" ua=""')
    assert "raw=Basic[dXN... len=28 sha256=" in out
    assert "cjpodW50ZXIy" not in out
    # raw= outside an [mcp_auth] line is not an auth field — left alone.
    assert redact_log_text("parser: raw='{\"a\": 1}'") == "parser: raw='{\"a\": 1}'"


def test_redact_truncated_legacy_header_dump():
    """The old dump was cut at 1000 chars, leaving an unterminated value."""
    text = "headers={'host': 'h', 'signature': 'sig1=:QUJDREVGR0hJSktM...(truncated)"
    out = redact_log_text(text)
    assert "QUJDREVGR0hJSktM" not in out
    assert "'signature': '[REDACTED]'" in out


def test_redact_bare_and_bearer_tokens():
    tok, secret = _new_token()
    assert redact_log_text(f"token={tok} end") == f"token=sk_meridian_...{_sha10(tok)} end"
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    out = redact_log_text(f"sent Authorization: Bearer {jwt}")
    assert out == f"sent Authorization: Bearer[eyJ... len={len(jwt)} sha256={_sha10(jwt)}]"
    opaque = "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MA"
    assert "Zm9vYmFy" not in redact_log_text(f"BEARER {opaque}")


@pytest.mark.parametrize("prose", [
    "Missing Bearer token",
    "Bearer authentication failed",
    'WWW-Authenticate: Bearer realm="MCP", error="invalid_token"',
])
def test_redact_leaves_bearer_prose_alone(prose):
    assert redact_log_text(prose) == prose


@pytest.mark.parametrize("text,expected", [
    ("peer 203.0.113.77 refused", "peer 203.0.113.0/24 refused"),
    ("peer 203.0.113.77:5432 refused", "peer 203.0.113.0/24:5432 refused"),
    ("net 203.0.113.77/16", "net 203.0.0.0/16"),
    ("net 203.0.113.0/24", "net 203.0.113.0/24"),
    ("peer 2001:db8:abcd:12::77 refused", "peer 2001:db8:abcd::/48 refused"),
    ("peer [2001:db8:abcd:12::77]:443", "peer [2001:db8:abcd::/48]:443"),
    ("mapped ::ffff:198.51.100.23", "mapped ::ffff:198.51.100.0/24"),
    ("xff=198.51.100.23, 10.9.8.7", "xff=198.51.100.0/24, 10.9.8.0/24"),
    # Left alone: loopback/unspecified, versions, timestamps, symbols, ids, MACs.
    ("listening on 0.0.0.0:8000 and 127.0.0.1 and ::1", "listening on 0.0.0.0:8000 and 127.0.0.1 and ::1"),
    ("lib v1.2.3.4 and 1.2.3.4.5", "lib v1.2.3.4 and 1.2.3.4.5"),
    ("at 2026-09-27 02:24:08.123", "at 2026-09-27 02:24:08.123"),
    ("tests/test_x.py::TestA::test_b", "tests/test_x.py::TestA::test_b"),
    ("worktree task-7ef88e30-08a1-4a7c-86bb-14abef537624", "worktree task-7ef88e30-08a1-4a7c-86bb-14abef537624"),
    ("mac 00:1a:2b:3c:4d:5e", "mac 00:1a:2b:3c:4d:5e"),
    ("bad 999.1.1.1", "bad 999.1.1.1"),
])
def test_redact_ip_addresses(text, expected):
    assert redact_log_text(text) == expected
    assert redact_log_text(expected) == expected


def test_redact_other_secret_shapes_reuse_secret_redaction_patterns():
    samples = {
        "github-token": "ghp_" + "a1B2" * 9,
        "slack-token": "xoxb-123456789012-abcdefABCDEF",
        "stripe-live-key": "sk_live_" + "Q" * 24,
        "stripe-key": "sk_test_" + "Z" * 24,
        "aws-access-key-id": "AKIA" + "ABCDEFGHIJKLMNOP",
        "openai-anthropic-key": "sk-ant-api03-" + "x" * 30,
    }
    for name, secret in samples.items():
        out = redact_log_text(f"config value {secret} loaded")
        assert out == f"config value [REDACTED:{name}] loaded", name
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"
    assert redact_log_text(f"key:\n{pem}\n") == "key:\n[REDACTED:pem-private-key]\n"


def test_secret_pattern_reuse_and_guard_table_stay_in_sync():
    """Reused patterns come from secret_redaction.SECRET_PATTERNS; every
    literal fast-path guard must name a pattern that actually runs."""
    from meridian import log_redaction
    from meridian.secret_redaction import SECRET_PATTERNS

    running = {name for name, _ in log_redaction._OTHER_SECRET_PATTERNS}
    reused = {p.name for p in SECRET_PATTERNS} - {"meridian-token", "dotenv-credential"}
    assert reused <= running
    assert set(log_redaction._LITERAL_GUARDS) <= running


def test_redact_url_credentials_and_query_secrets():
    out = redact_log_text(
        "connect postgresql://neon_user:s3cretPassw0rd@ep-x.neon.tech/db?sslmode=require"
        " then GET https://api.example/cb?code=abc123XYZ&state=ok&access_token=tok999"
    )
    assert "s3cretPassw0rd" not in out and "abc123XYZ" not in out and "tok999" not in out
    assert "postgresql://neon_user:[REDACTED]@ep-x.neon.tech/db?sslmode=require" in out
    assert "?code=[REDACTED]&state=ok&access_token=[REDACTED]" in out
    assert redact_log_text(out) == out


def test_redact_is_linear_on_pathological_input():
    """Unterminated quoted values full of backslashes must not backtrack."""
    nasty = "[mcp_auth] raw='" + "\\" * 20001 + "\n" + "'signature': '" + "\\a" * 20000
    t0 = time.perf_counter()
    redact_log_text(nasty)
    assert time.perf_counter() - t0 < 2.0


def test_redact_log_row_copies_and_redacts_only_text_fields():
    tok, secret = _new_token()
    row = {"id": "r1", "level": "WARNING", "logger": "x", "message": f"Bearer {tok}",
           "exc_text": None, "recorded_at": "2026-09-27 00:00:00"}
    out = redact_log_row(row)
    assert out is not row
    assert row["message"] == f"Bearer {tok}"  # input untouched
    assert out["message"].startswith("Bearer[sk_meridian_... len=55")
    assert out["exc_text"] is None
    assert {k: v for k, v in out.items() if k != "message"} == {
        k: v for k, v in row.items() if k != "message"
    }
    # Rows without the text fields pass through.
    assert redact_log_row({"id": "x"}) == {"id": "x"}
