"""Local mode sends no request to the hosted service (meridian_desktop/connection_status.py). Pure Python, scripted transport.

A saved sign-in token is read from the keychain only. The fake transport records every request, and the test checks that
none was made. The keychain is in memory and the clock is fixed.
"""

from __future__ import annotations

from datetime import datetime, timezone

from _fakes import MemoryKeyring, ScriptedTransport
from meridian_desktop import connection_status as cs
from meridian_desktop import hosted_session as hs
from meridian_desktop import local_client
from meridian_desktop.secret_store import KeyringSecretStore

HOSTED = "https://usemeridian.us"
LOCAL = "http://127.0.0.1:7878"
EMAIL = "owner@example.test"
ACCESS = "sk_meridian_fake-local-mode-access-0001"
NOW = datetime(2027, 1, 1, 12, tzinfo=timezone.utc).timestamp()
FAR = datetime(2027, 3, 2, 12, tzinfo=timezone.utc).timestamp()


def _probe_not_running(url: str) -> str:
    return local_client.HEALTH_NOT_RUNNING


def _secrets() -> KeyringSecretStore:
    return KeyringSecretStore(backend=MemoryKeyring())


def _save_session(secrets: KeyringSecretStore) -> None:
    grant = hs.TokenGrant(access_token=ACCESS, expires_in=int(FAR - NOW), refresh_token=None)
    hs.save_session(secrets, grant, account=EMAIL, now=NOW)


def _read(secrets: KeyringSecretStore, transport: ScriptedTransport, *, hosted_checks: bool) -> cs.ConnectionSnapshot:
    return cs.read_connection(
        hosted_url=HOSTED,
        local_url=LOCAL,
        secrets=secrets,
        transport=transport,
        probe=_probe_not_running,
        clock=lambda: NOW,
        hosted_checks=hosted_checks,
    )


def test_local_mode_sends_no_request_to_the_hosted_service_even_with_a_saved_token():
    secrets = _secrets()
    _save_session(secrets)
    transport = ScriptedTransport({})

    snapshot = _read(secrets, transport, hosted_checks=False)

    assert transport.requests == [], "no request may reach the hosted service in Local mode"
    assert snapshot.signed_in is True, "the saved token is still reported as saved, without a network check"


def test_local_mode_with_no_saved_token_is_not_signed_in_and_sends_nothing():
    transport = ScriptedTransport({})

    snapshot = _read(_secrets(), transport, hosted_checks=False)

    assert transport.requests == []
    assert snapshot.signed_in is False


LOCAL_MODE_LINE = "Local mode: hosted sign-in is off. Switch to Hosted mode to sign in."


def test_local_mode_with_a_saved_token_says_hosted_sign_in_is_off_and_offers_no_sign_in():
    secrets = _secrets()
    _save_session(secrets)

    snapshot = _read(secrets, ScriptedTransport({}), hosted_checks=False)
    view = cs.connection_view(snapshot, hosted_url=HOSTED)

    assert snapshot.hosted_checked is False, "Local mode records that the hosted service was not checked"
    assert view.headline == LOCAL_MODE_LINE
    assert view.sign_in_offered is False, "Local mode offers no sign-in button, even with a token saved"
    assert view.state == cs.STATE_NOT_CONNECTED
    assert view.hosted == "Not connected: Hosted (usemeridian.us)."
    assert "sign in again" not in view.headline.lower()


def test_local_mode_with_no_saved_token_says_hosted_sign_in_is_off_and_offers_no_sign_in():
    transport = ScriptedTransport({})

    snapshot = _read(_secrets(), transport, hosted_checks=False)
    view = cs.connection_view(snapshot, hosted_url=HOSTED)

    assert transport.requests == []
    assert view.headline == LOCAL_MODE_LINE
    assert view.sign_in_offered is False


def test_hosted_mode_with_an_expired_session_still_offers_sign_in_so_only_local_mode_changed():
    secrets = _secrets()
    # Issued two hours before NOW with a one-hour life, so it expired an hour ago. No request is needed to know that.
    hs.save_session(secrets, hs.TokenGrant(access_token=ACCESS, expires_in=3600), account=EMAIL, now=NOW - 7200)

    snapshot = _read(secrets, ScriptedTransport({}), hosted_checks=True)
    view = cs.connection_view(snapshot, hosted_url=HOSTED)

    assert snapshot.hosted_checked is True
    assert view.headline == "Not connected: sign in again."
    assert view.sign_in_offered is True
