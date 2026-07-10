"""Tests for trial-expiration reminder emails (9f7bfcca).

Scope: the *pure* decision logic (`compute_trial_reminder`) and the idempotency
plumbing that reads/writes the existing ``tenants.notification_prefs`` JSON blob,
plus a mocked end-to-end pass of ``run_trial_reminder_check``.

ANTI-STALL GUARANTEES (per the sprint item):
  * No background loop is started, no ``asyncio.sleep``, no port is bound, no
    real network call is made.
  * The email sender (Resend, called via ``httpx.AsyncClient``) is fully mocked.
  * The time source is injected (``now=``) — no wall-clock dependence.
The DB used by the orchestrator test is a fresh in-memory SQLite connection
(the shared ``db`` fixture) — local, no network.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx

from meridian import hosted


NOW = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)


def _fmt(dt: datetime) -> str:
    """The ``"%Y-%m-%d %H:%M:%S"`` form Meridian writes for expiry columns."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _trial_tenant(**over):
    """A trialing tenant dict with a 10-day-out expiry by default."""
    base = {
        "id": "t1",
        "email": "user@example.com",
        "plan": "free",
        "is_internal": 0,
        "trial_started_at": _fmt(NOW - timedelta(days=20)),
        "inactivity_expires_at": _fmt(NOW + timedelta(days=10)),
        "notification_prefs": "{}",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Pure logic: which threshold fires
# ---------------------------------------------------------------------------

def test_threshold_fires_at_14_days():
    t = _trial_tenant(inactivity_expires_at=_fmt(NOW + timedelta(days=14)))
    assert hosted.compute_trial_reminder(t, NOW) == 14


def test_threshold_fires_at_7_days():
    t = _trial_tenant(inactivity_expires_at=_fmt(NOW + timedelta(days=7)))
    assert hosted.compute_trial_reminder(t, NOW) == 7


def test_threshold_fires_at_1_day():
    # ~1.5 days remaining -> floor to 1 day remaining -> 1-day reminder.
    t = _trial_tenant(inactivity_expires_at=_fmt(NOW + timedelta(days=1, hours=12)))
    assert hosted.compute_trial_reminder(t, NOW) == 1


def test_between_thresholds_buckets_up_to_14():
    # 10 days remaining: tightest bucket that still covers it is 14, so the
    # "ends in 14 days" reminder is the one that fires (accurate round-up).
    t = _trial_tenant(inactivity_expires_at=_fmt(NOW + timedelta(days=10)))
    assert hosted.compute_trial_reminder(t, NOW) == 14


def test_3_days_out_buckets_up_to_7():
    # Between 1 and 7 -> tightest covering bucket is 7.
    t = _trial_tenant(inactivity_expires_at=_fmt(NOW + timedelta(days=3)))
    assert hosted.compute_trial_reminder(t, NOW) == 7


def test_no_reminder_when_more_than_14_days_out():
    t = _trial_tenant(inactivity_expires_at=_fmt(NOW + timedelta(days=25)))
    assert hosted.compute_trial_reminder(t, NOW) is None


def test_falls_back_to_trial_started_at_30day_window():
    # No inactivity_expires_at -> anchor on trial_started_at + 30 days.
    # Started 24 days ago -> 6 days remaining -> 7-day reminder is due.
    t = _trial_tenant(
        inactivity_expires_at=None,
        trial_started_at=_fmt(NOW - timedelta(days=24)),
    )
    assert hosted.compute_trial_reminder(t, NOW) == 7


# ---------------------------------------------------------------------------
# Idempotency: each threshold fires once
# ---------------------------------------------------------------------------

def test_already_sent_threshold_is_skipped():
    # 14-day threshold already recorded -> at 14 days out nothing new fires.
    t = _trial_tenant(
        inactivity_expires_at=_fmt(NOW + timedelta(days=14)),
        notification_prefs=json.dumps({"trial_reminders_sent": [14]}),
    )
    assert hosted.compute_trial_reminder(t, NOW) is None


def test_next_smaller_threshold_fires_after_prior_sent():
    # 14 already sent; now 7 days out -> the 7-day reminder is due.
    t = _trial_tenant(
        inactivity_expires_at=_fmt(NOW + timedelta(days=7)),
        notification_prefs=json.dumps({"trial_reminders_sent": [14]}),
    )
    assert hosted.compute_trial_reminder(t, NOW) == 7


def test_all_sent_returns_none():
    t = _trial_tenant(
        inactivity_expires_at=_fmt(NOW + timedelta(days=1)),
        notification_prefs=json.dumps({"trial_reminders_sent": [14, 7, 1]}),
    )
    assert hosted.compute_trial_reminder(t, NOW) is None


def test_record_preserves_other_notification_prefs_keys():
    t = _trial_tenant(
        notification_prefs=json.dumps({"weekly_digest": True}),
    )
    updated = json.loads(hosted._with_reminder_recorded(t, 14))
    assert updated["weekly_digest"] is True
    assert updated["trial_reminders_sent"] == [14]


def test_record_appends_without_dropping_prior_thresholds():
    t = _trial_tenant(
        notification_prefs=json.dumps({"trial_reminders_sent": [14]}),
    )
    updated = json.loads(hosted._with_reminder_recorded(t, 7))
    assert sorted(updated["trial_reminders_sent"]) == [7, 14]


def test_malformed_notification_prefs_treated_as_none_sent():
    t = _trial_tenant(
        inactivity_expires_at=_fmt(NOW + timedelta(days=14)),
        notification_prefs="not-json{",
    )
    # A corrupt blob must not crash and must be treated as "nothing sent".
    assert hosted.compute_trial_reminder(t, NOW) == 14


# ---------------------------------------------------------------------------
# Skips: non-trial / expired / internal
# ---------------------------------------------------------------------------

def test_paying_plan_skipped():
    for plan in ("standard", "pro", "admin"):
        t = _trial_tenant(plan=plan)
        assert hosted.compute_trial_reminder(t, NOW) is None, plan


def test_internal_tenant_skipped():
    t = _trial_tenant(is_internal=1)
    assert hosted.compute_trial_reminder(t, NOW) is None


def test_expired_trial_skipped():
    # Expiry already in the past -> no "ends soon" nudge.
    t = _trial_tenant(inactivity_expires_at=_fmt(NOW - timedelta(days=2)))
    assert hosted.compute_trial_reminder(t, NOW) is None


def test_expiring_this_instant_skipped():
    t = _trial_tenant(inactivity_expires_at=_fmt(NOW))
    assert hosted.compute_trial_reminder(t, NOW) is None


def test_no_expiry_and_no_anchor_returns_none():
    t = _trial_tenant(
        inactivity_expires_at=None,
        trial_started_at=None,
        created_at=None,
    )
    assert hosted.compute_trial_reminder(t, NOW) is None


# ---------------------------------------------------------------------------
# Orchestrator: mocked email sender + injected clock, real in-memory DB.
# No loop / sleep / port / network.
# ---------------------------------------------------------------------------

class _FakeResponse:
    def raise_for_status(self):  # noqa: D401 — mimic httpx.Response
        return None


class _FakeAsyncClient:
    """Drop-in for ``httpx.AsyncClient`` that records POSTs instead of sending."""

    sent: list[dict] = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, *, headers=None, json=None):  # noqa: A002
        _FakeAsyncClient.sent.append({"url": url, "json": json})
        return _FakeResponse()


async def _insert_tenant(db, tenant: dict) -> None:
    await db.execute(
        "INSERT INTO tenants (id, email, plan, notification_prefs, "
        "trial_started_at, inactivity_expires_at, is_internal) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            tenant["id"],
            tenant["email"],
            tenant["plan"],
            tenant.get("notification_prefs") or "{}",
            tenant.get("trial_started_at"),
            tenant.get("inactivity_expires_at"),
            int(tenant.get("is_internal") or 0),
        ),
    )
    await db.commit()


async def _prefs(db, tenant_id: str) -> dict:
    async with db.execute(
        "SELECT notification_prefs FROM tenants WHERE id = ?", (tenant_id,)
    ) as cur:
        row = await cur.fetchone()
    raw = row["notification_prefs"] if row is not None else "{}"
    return json.loads(raw or "{}")


async def test_run_sends_once_and_persists_idempotency(db, monkeypatch):
    _FakeAsyncClient.sent = []
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    # 7 days out -> 14-day reminder is due (still unsent).
    await _insert_tenant(
        db,
        _trial_tenant(
            id="tt1",
            inactivity_expires_at=_fmt(NOW + timedelta(days=7)),
        ),
    )

    await hosted.run_trial_reminder_check(db, now=NOW)

    # Exactly one email sent, to the trial tenant, and marker persisted.
    assert len(_FakeAsyncClient.sent) == 1
    assert _FakeAsyncClient.sent[0]["json"]["to"] == ["user@example.com"]
    assert (await _prefs(db, "tt1"))["trial_reminders_sent"] == [7]

    # Second pass on the same day sends nothing (idempotent).
    _FakeAsyncClient.sent = []
    await hosted.run_trial_reminder_check(db, now=NOW)
    assert _FakeAsyncClient.sent == []


async def test_run_skips_non_trial_and_expired_tenants(db, monkeypatch):
    _FakeAsyncClient.sent = []
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    # Paying tenant (queried out) and an expired-trial tenant (skipped by logic).
    await _insert_tenant(
        db, _trial_tenant(id="paid", plan="standard", email="paid@example.com")
    )
    await _insert_tenant(
        db,
        _trial_tenant(
            id="expired",
            email="expired@example.com",
            inactivity_expires_at=_fmt(NOW - timedelta(days=1)),
        ),
    )

    await hosted.run_trial_reminder_check(db, now=NOW)
    assert _FakeAsyncClient.sent == []


async def test_run_no_email_key_is_noop(db, monkeypatch):
    _FakeAsyncClient.sent = []
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    # Also clear the Fly alias so _cfg finds nothing.
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    await _insert_tenant(
        db,
        _trial_tenant(id="tt2", inactivity_expires_at=_fmt(NOW + timedelta(days=7))),
    )

    await hosted.run_trial_reminder_check(db, now=NOW)
    # No transport configured -> nothing attempted, nothing persisted.
    assert _FakeAsyncClient.sent == []
    assert (await _prefs(db, "tt2")) == {}
