"""925909aa — magic-link anti-spam hardening.

Covers the disposable-email blocklist, the persistent per-IP signup_attempts
log, the guarded Cloudflare Turnstile verifier, and the end-to-end rejection of
a disposable address through POST /auth/magic.
"""
from __future__ import annotations

import asyncio

from meridian import db as db_module
from meridian import hosted
from meridian.email_blocklist import (
    email_domain,
    is_disposable_domain,
    is_disposable_email,
)


# ---------------------------------------------------------------------------
# Disposable-domain blocklist (pure)
# ---------------------------------------------------------------------------

def test_blocklist_flags_known_disposable():
    assert is_disposable_email("foo@mailinator.com")
    assert is_disposable_email("x@guerrillamail.com")
    assert is_disposable_email("Y@YOPMAIL.COM")            # case-insensitive
    assert is_disposable_email("z@inbox.mailinator.com")   # subdomain match


def test_blocklist_allows_normal_and_malformed():
    assert not is_disposable_email("adam@gmail.com")
    assert not is_disposable_email("dev@usemeridian.us")
    assert not is_disposable_email("notanemail")
    assert not is_disposable_email("")
    assert not is_disposable_email(None)  # type: ignore[arg-type]


def test_email_domain_and_domain_helpers():
    assert email_domain(" A@B.Com ") == "b.com"
    assert email_domain("weird@@x.com") == "x.com"
    assert email_domain("nope") == ""
    assert is_disposable_domain("mailinator.com")
    assert not is_disposable_domain("")


# ---------------------------------------------------------------------------
# signup_attempts persistence
# ---------------------------------------------------------------------------

def test_signup_attempts_record_and_count():
    async def _run():
        db = await db_module.init_db(":memory:")
        await db_module.record_signup_attempt(db, "iphash1", "emh1")
        await db_module.record_signup_attempt(db, "iphash1", "emh2")
        await db_module.record_signup_attempt(db, "iphash2", "emh3")
        return (
            await db_module.count_recent_signup_attempts(db, "iphash1", "1970-01-01 00:00:00"),
            await db_module.count_recent_signup_attempts(db, "iphash2", "1970-01-01 00:00:00"),
            await db_module.count_recent_signup_attempts(db, "iphash1", "2999-01-01 00:00:00"),
            await db_module.count_recent_signup_attempts(db, "nosuchip", "1970-01-01 00:00:00"),
        )

    all_ip1, ip2, future, missing = asyncio.run(_run())
    assert all_ip1 == 2
    assert ip2 == 1
    assert future == 0   # window excludes everything before 2999
    assert missing == 0


# ---------------------------------------------------------------------------
# Cloudflare Turnstile verifier (guarded)
# ---------------------------------------------------------------------------

def test_verify_turnstile_skips_without_key(monkeypatch):
    monkeypatch.setattr(hosted, "_cfg", lambda k, d="": "" if k == "TURNSTILE_SECRET_KEY" else d)
    assert asyncio.run(hosted._verify_turnstile("anything")) is True
    assert asyncio.run(hosted._verify_turnstile(None)) is True


def test_verify_turnstile_requires_token_when_key_set(monkeypatch):
    monkeypatch.setattr(hosted, "_cfg", lambda k, d="": "sk_secret" if k == "TURNSTILE_SECRET_KEY" else d)
    assert asyncio.run(hosted._verify_turnstile("")) is False
    assert asyncio.run(hosted._verify_turnstile(None)) is False


# ---------------------------------------------------------------------------
# End-to-end: disposable address rejected at POST /auth/magic (no token issued)
# ---------------------------------------------------------------------------

def test_disposable_email_rejected_no_dev_link(client):
    resp = client.post("/auth/magic", json={"email": "spam@mailinator.com"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"           # generic response — no enumeration
    assert not body.get("dev_link")         # no token created → no link surfaced


# ---------------------------------------------------------------------------
# 88affef6 — unique, timestamped magic-link subject (deliverability)
# ---------------------------------------------------------------------------

def test_magic_email_subject_is_unique_timestamped():
    s = hosted._magic_email_subject()
    assert s.startswith("Sign in to Meridian (")   # brand text preserved as prefix
    assert s.endswith(")")
    assert "UTC" in s                              # carries a UTC timestamp
