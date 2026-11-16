"""Tests for the connection bar's text and check (meridian_desktop/connection_status.py). Pure Python, no Qt.

Each state's three lines are checked by exact text. The check runs against a scripted fake transport and a fake local
probe, so nothing touches the network or a real server. The keychain is an in-memory backend, and the clock is fixed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from _fakes import MemoryKeyring, ScriptedTransport
from meridian_desktop import connection_status as cs
from meridian_desktop import hosted_session as hs
from meridian_desktop import local_client
from meridian_desktop.hosted_client import Reply
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.token_store import set_token

HOSTED = "https://usemeridian.us"
LOCAL = "http://127.0.0.1:7878"
EMAIL = "owner@example.test"
ACCESS = "sk_meridian_fake-status-access-0001"
NEW_ACCESS = "sk_meridian_fake-status-access-0002"
REFRESH = "rt_meridian_fake-status-refresh-0001"
ROTATED = "rt_meridian_fake-status-refresh-0002"
NOW = datetime(2027, 1, 1, 12, tzinfo=timezone.utc).timestamp()
FAR = datetime(2027, 3, 2, 12, tzinfo=timezone.utc).timestamp()  # 60 days after NOW, shown as 2027-03-02
SOON = NOW + 10 * 86400  # 10 days after NOW, shown as 2027-01-11
HOSTED_CONNECTED = "Connected: Hosted (usemeridian.us)."
HOSTED_RECONNECTING = "Reconnecting: Hosted (usemeridian.us)."
HOSTED_NOT_CONNECTED = "Not connected: Hosted (usemeridian.us)."


def _json(status: int, data: dict) -> Reply:
    return Reply(status, json.dumps(data).encode("utf-8"))


def _me_ok() -> Reply:
    return _json(200, {"email": EMAIL, "plan": "standard"})


def _probe_running(url: str) -> str:
    return local_client.HEALTH_RUNNING


def _probe_not_running(url: str) -> str:
    return local_client.HEALTH_NOT_RUNNING


@pytest.fixture
def secrets():
    return KeyringSecretStore(backend=MemoryKeyring())


def _save(secrets, *, refresh=None, expires_at=FAR) -> None:
    """Save a session the way the sign-in does, expiring at ``expires_at`` (epoch seconds)."""
    grant = hs.TokenGrant(access_token=ACCESS, expires_in=int(expires_at - NOW), refresh_token=refresh)
    hs.save_session(secrets, grant, account=EMAIL, now=NOW)


def _check(secrets, transport, probe=_probe_not_running):
    snapshot = cs.read_connection(
        hosted_url=HOSTED,
        local_url=LOCAL,
        secrets=secrets,
        transport=transport,
        probe=probe,
        clock=lambda: NOW,
    )
    return snapshot, cs.connection_view(snapshot, hosted_url=HOSTED)


def test_no_token_says_not_signed_in_and_makes_no_hosted_request(secrets):
    transport = ScriptedTransport({})

    snapshot, view = _check(secrets, transport)

    assert view.state == cs.STATE_NOT_CONNECTED
    assert view.headline == "", "no sign-in prompt on the bar when nothing is saved; the hosted line says not connected"
    assert view.sign_in_offered is False
    assert view.hosted == HOSTED_NOT_CONNECTED
    assert view.local == "Local: none."
    assert transport.requests == [], "with no token saved, nothing is sent to the hosted host"


def test_a_connected_session_shows_the_account_the_date_and_the_hosted_and_local_lines(secrets):
    _save(secrets, expires_at=FAR)
    transport = ScriptedTransport({"/me": [_me_ok()]})

    snapshot = cs.read_connection(
        hosted_url=HOSTED,
        local_url=LOCAL,
        secrets=secrets,
        transport=transport,
        probe=_probe_running,
        clock=lambda: NOW,
    )
    view = cs.connection_view(snapshot, hosted_url=HOSTED)

    assert view.state == cs.STATE_CONNECTED
    assert view.headline == f"Signed in: {EMAIL}. Signed in until 2027-03-02."
    assert view.hosted == HOSTED_CONNECTED
    assert view.local == "Local: Meridian server at 127.0.0.1:7878 (live)."
    assert transport.requests[0]["headers"]["Authorization"] == f"Bearer {ACCESS}"


def test_fewer_than_fourteen_days_left_is_the_warning_state(secrets):
    _save(secrets, expires_at=SOON)
    transport = ScriptedTransport({"/me": [_me_ok()]})

    _snapshot, view = _check(secrets, transport)

    assert view.state == cs.STATE_WARNING
    assert view.headline == (
        f"Signed in: {EMAIL}. Signed in until 2027-01-11. Under 14 days left: sign in again before then."
    )
    assert view.hosted == HOSTED_CONNECTED


def test_a_rejected_token_is_not_connected_and_says_to_sign_in_again(secrets):
    _save(secrets)
    transport = ScriptedTransport({"/me": [_json(401, {"detail": "invalid API token"})]})

    _snapshot, view = _check(secrets, transport)

    assert view.state == cs.STATE_NOT_CONNECTED
    assert view.headline == "Not connected: sign in again."
    assert view.hosted == HOSTED_NOT_CONNECTED


def test_an_unreachable_hosted_server_is_reconnecting_and_keeps_the_account(secrets):
    _save(secrets)
    transport = ScriptedTransport({"/me": [Reply(503, b"")]})

    _snapshot, view = _check(secrets, transport)

    assert view.state == cs.STATE_RECONNECTING
    assert view.headline == f"Signed in: {EMAIL}. Signed in until 2027-03-02."
    assert view.hosted == HOSTED_RECONNECTING


def test_an_expired_session_is_not_connected_and_makes_no_request(secrets):
    _save(secrets, expires_at=NOW - 1)
    transport = ScriptedTransport({})

    _snapshot, view = _check(secrets, transport)

    assert view.state == cs.STATE_NOT_CONNECTED
    assert view.headline == "Not connected: sign in again."
    assert transport.requests == []


def test_a_session_with_no_recorded_expiry_is_connected_with_no_prompt(secrets):
    # A token saved before the browser sign-in has no session record, so its expiry is not known.
    set_token(ACCESS, store=secrets)
    transport = ScriptedTransport({"/me": [_me_ok()]})

    _snapshot, view = _check(secrets, transport)

    assert view.state == cs.STATE_CONNECTED
    assert view.headline == f"Signed in: {EMAIL}.", "no expiry prompt: the bar shows the account only"
    assert view.sign_in_offered is False


def test_a_refused_renewal_under_fourteen_days_is_not_connected_and_skips_the_account_check(secrets):
    _save(secrets, refresh=REFRESH, expires_at=SOON)
    transport = ScriptedTransport({"/oauth/token": [_json(400, {"error": "invalid_grant"})]})

    _snapshot, view = _check(secrets, transport)

    assert view.state == cs.STATE_NOT_CONNECTED
    assert view.headline == "Not connected: sign in again."
    assert transport.paths() == ["/oauth/token"], "no /me call once the renewal is refused"


def test_a_renewal_under_fourteen_days_saves_the_new_token_then_checks_it(secrets):
    _save(secrets, refresh=REFRESH, expires_at=SOON)
    transport = ScriptedTransport(
        {
            "/oauth/token": [_json(200, {"access_token": NEW_ACCESS, "expires_in": 7776000, "refresh_token": ROTATED})],
            "/me": [_me_ok()],
        }
    )

    _snapshot, view = _check(secrets, transport)

    assert view.state == cs.STATE_CONNECTED, "the renewed session has 90 days, so the warning clears"
    assert view.headline == f"Signed in: {EMAIL}. Signed in until 2027-04-01."
    assert transport.requests[-1]["headers"]["Authorization"] == f"Bearer {NEW_ACCESS}"
    saved = hs.load_saved(secrets)
    assert saved.access_token == NEW_ACCESS
    assert saved.refresh_token == ROTATED


def test_a_renewal_that_cannot_reach_the_server_is_reconnecting(secrets):
    _save(secrets, refresh=REFRESH, expires_at=SOON)
    transport = ScriptedTransport({"/oauth/token": [OSError("dropped")]})

    _snapshot, view = _check(secrets, transport)

    assert view.state == cs.STATE_RECONNECTING
    assert view.hosted == HOSTED_RECONNECTING


def test_a_probe_that_raises_reads_as_no_local_server(secrets):
    transport = ScriptedTransport({})

    def raising(url: str) -> str:
        raise OSError("probe broke")

    _snapshot, view = _check(secrets, transport, probe=raising)

    assert view.local == "Local: none."


def test_the_checking_and_failed_views_say_what_they_are_doing():
    checking = cs.checking_view(hosted_url=HOSTED)
    failed = cs.failed_view(hosted_url=HOSTED, failure="ValueError")

    assert checking.state == cs.STATE_CHECKING
    assert checking.headline == "Checking the connection..."
    assert checking.hosted == "Checking: Hosted (usemeridian.us)."
    assert failed.state == cs.STATE_RECONNECTING
    assert failed.headline == "Connection check failed (ValueError). It is tried again in five minutes."
    assert failed.hosted == HOSTED_RECONNECTING
    assert failed.local == "Local: not checked."


def test_the_hosted_name_comes_from_the_saved_url_not_a_fixed_word():
    view = cs.connection_view(
        cs.ConnectionSnapshot(hosted=cs.HostedStatus(cs.STATE_NOT_CONNECTED), signed_in=False, local_netloc=None, checked_at=NOW),
        hosted_url="https://hosted.example.test:8443",
    )

    assert view.hosted == "Not connected: Hosted (hosted.example.test:8443)."


def test_the_sign_in_button_is_offered_only_when_a_sign_in_would_change_something(secrets):
    # Not signed in: not offered, no prompt. Expiry not recorded: not offered. Far from expiry: not offered.
    # Under fourteen days: offered.
    _snapshot, not_signed_in = _check(secrets, ScriptedTransport({}))
    assert not_signed_in.state == cs.STATE_NOT_CONNECTED and not_signed_in.sign_in_offered is False

    _save(secrets)
    _snapshot, far = _check(secrets, ScriptedTransport({"/me": [_me_ok()]}))
    assert far.state == cs.STATE_CONNECTED and far.sign_in_offered is False

    _save(secrets, expires_at=SOON)
    _snapshot, warning = _check(secrets, ScriptedTransport({"/me": [_me_ok()]}))
    assert warning.state == cs.STATE_WARNING and warning.sign_in_offered is True


def test_the_snapshot_carries_no_token_text(secrets):
    _save(secrets)
    transport = ScriptedTransport({"/me": [_me_ok()]})

    snapshot, view = _check(secrets, transport)

    text = " ".join([view.headline, view.hosted, view.local, repr(snapshot)])
    assert ACCESS not in text
    assert REFRESH not in text
