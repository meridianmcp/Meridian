"""Tests for the browser sign-in (meridian_desktop/hosted_session.py): the device flow, GET /me, refresh and the session record.

Every request goes through a scripted fake transport, so nothing touches the network. The keychain is an in-memory
backend, and the clock and the sleep are fakes, so the poll loop runs in no real time. Sign-in text and tokens are
checked for leaks: an error message must never carry a token or a device code.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest

from _fakes import MemoryKeyring, ScriptedTransport
from meridian_desktop import hosted_session as hs
from meridian_desktop.hosted_client import BadBaseUrl, HostedError, HostedTimeout, Reply
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.token_store import HOSTED_TOKEN_ACCOUNT, TOKEN_SERVICE, get_token, set_token

BASE = "https://hosted.test"
DEVICE_CODE = "device-code-fake-session-0001"
USER_CODE = "WDJB-MJHT"
VERIFY = f"{BASE}/activate?code={USER_CODE}"
ACCESS = "sk_meridian_fake-session-access-0001"
REFRESH = "rt_meridian_fake-session-refresh-0001"
ROTATED = "rt_meridian_fake-session-refresh-0002"
NEW_ACCESS = "sk_meridian_fake-session-access-0002"
EMAIL = "owner@example.test"
NOW = 1_800_000_000.0


def _json(status: int, data: dict) -> Reply:
    return Reply(status, json.dumps(data).encode("utf-8"))


def _device_reply(**overrides) -> Reply:
    data = {
        "device_code": DEVICE_CODE,
        "user_code": USER_CODE,
        "verification_uri": f"{BASE}/activate",
        "verification_uri_complete": VERIFY,
        "expires_in": 300,
        "interval": 5,
    }
    data.update(overrides)
    return _json(200, data)


class Clock:
    def __init__(self, start: float = NOW) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def secrets():
    return KeyringSecretStore(backend=MemoryKeyring())


# ----------------------------------------------------------- the device flow


def test_start_reads_the_codes_and_keeps_the_device_code_out_of_repr():
    script = ScriptedTransport({"/oauth/device": [_device_reply()]})

    auth = hs.start_device_sign_in(BASE, script)

    assert auth.user_code == USER_CODE
    assert auth.device_code == DEVICE_CODE
    assert auth.verification_uri == VERIFY
    assert auth.expires_in == 300
    assert auth.interval == 5
    assert DEVICE_CODE not in repr(auth)
    assert script.requests[0]["method"] == "POST"
    assert script.paths() == ["/oauth/device"]


def test_start_refuses_a_verification_page_on_another_host_so_no_browser_opens():
    script = ScriptedTransport({"/oauth/device": [_device_reply(verification_uri_complete="https://evil.test/activate?code=x")]})

    with pytest.raises(hs.SignInError, match="another host"):
        hs.start_device_sign_in(BASE, script)


def test_start_refuses_a_reply_without_the_codes_and_names_only_the_status():
    script = ScriptedTransport({"/oauth/device": [Reply(500, b'{"device_code": "' + DEVICE_CODE.encode() + b'"}')]})

    with pytest.raises(hs.SignInError) as caught:
        hs.start_device_sign_in(BASE, script)

    assert str(caught.value) == "Hosted Meridian did not start the sign-in (HTTP 500)."
    assert DEVICE_CODE not in str(caught.value)


def test_start_uses_the_documented_expiry_and_interval_when_the_reply_leaves_them_out():
    reply = _json(200, {"device_code": DEVICE_CODE, "user_code": USER_CODE, "verification_uri_complete": VERIFY})

    auth = hs.start_device_sign_in(BASE, ScriptedTransport({"/oauth/device": [reply]}))

    assert (auth.expires_in, auth.interval) == (300, 5)


def test_start_refuses_a_plain_http_hosted_url_before_any_request():
    script = ScriptedTransport({})

    with pytest.raises(BadBaseUrl):
        hs.start_device_sign_in("http://example.test", script)
    assert script.requests == []


def test_poll_waits_the_interval_and_backs_off_by_five_on_slow_down():
    script = ScriptedTransport(
        {
            "/oauth/token": [
                _json(200, {"error": "authorization_pending"}),
                _json(200, {"error": "slow_down"}),
                _json(200, {"error": "authorization_pending"}),
                _json(200, {"access_token": ACCESS, "token_type": "bearer", "expires_in": 7776000}),
            ]
        }
    )
    auth = hs.DeviceAuth(device_code=DEVICE_CODE, user_code=USER_CODE, verification_uri=VERIFY, expires_in=300, interval=5)
    clock = Clock()

    grant = hs.poll_for_token(BASE, auth, script, sleep=clock.sleep, clock=clock)

    assert grant.access_token == ACCESS
    assert grant.expires_in == 7776000
    assert grant.refresh_token is None, "the device grant issues no refresh token"
    assert clock.sleeps == [5, 10, 10]
    body = script.requests[0]["body"].decode("ascii")
    assert urllib.parse.parse_qs(body) == {
        "grant_type": ["urn:ietf:params:oauth:grant-type:device_code"],
        "device_code": [DEVICE_CODE],
    }


def test_poll_ends_with_a_plain_error_for_an_unknown_reply_and_never_repeats_the_body():
    script = ScriptedTransport({"/oauth/token": [Reply(500, f"leak {DEVICE_CODE}".encode())]})
    auth = hs.DeviceAuth(device_code=DEVICE_CODE, user_code=USER_CODE, verification_uri=VERIFY, expires_in=300, interval=5)
    clock = Clock()

    with pytest.raises(hs.SignInError) as caught:
        hs.poll_for_token(BASE, auth, script, sleep=clock.sleep, clock=clock)

    assert str(caught.value) == "Hosted Meridian did not complete the sign-in (HTTP 500)."
    assert DEVICE_CODE not in str(caught.value)


def test_a_denied_code_raises_denied_and_an_expired_code_raises_expired():
    auth = hs.DeviceAuth(device_code=DEVICE_CODE, user_code=USER_CODE, verification_uri=VERIFY, expires_in=300, interval=5)
    clock = Clock()

    denied = ScriptedTransport({"/oauth/token": [_json(400, {"error": "access_denied"})]})
    with pytest.raises(hs.SignInDenied):
        hs.poll_for_token(BASE, auth, denied, sleep=clock.sleep, clock=clock)

    expired = ScriptedTransport({"/oauth/token": [_json(400, {"error": "expired_token"})]})
    with pytest.raises(hs.SignInExpired):
        hs.poll_for_token(BASE, auth, expired, sleep=clock.sleep, clock=clock)


def test_a_grant_without_expires_in_uses_the_server_ninety_day_lifetime():
    script = ScriptedTransport({"/oauth/token": [_json(200, {"access_token": ACCESS})]})
    auth = hs.DeviceAuth(device_code=DEVICE_CODE, user_code=USER_CODE, verification_uri=VERIFY, expires_in=300, interval=5)
    clock = Clock()

    grant = hs.poll_for_token(BASE, auth, script, sleep=clock.sleep, clock=clock)

    assert grant.expires_in == 86400 * 90


def test_a_token_grant_repr_never_shows_the_token():
    grant = hs.TokenGrant(access_token=ACCESS, expires_in=60, refresh_token=REFRESH)

    assert ACCESS not in repr(grant)
    assert REFRESH not in repr(grant)


# ----------------------------------------------------------- GET /me


def test_check_account_sends_the_bearer_token_and_returns_the_email():
    script = ScriptedTransport({"/me": [_json(200, {"email": EMAIL, "plan": "standard"})]})

    check = hs.check_account(BASE, ACCESS, script)

    assert check == hs.AccountCheck(hs.ACCOUNT_OK, EMAIL)
    assert script.requests[0]["method"] == "GET"
    assert script.requests[0]["headers"]["Authorization"] == f"Bearer {ACCESS}"


@pytest.mark.parametrize("status", [401, 403])
def test_check_account_treats_401_and_403_as_a_rejected_token(status):
    script = ScriptedTransport({"/me": [_json(status, {"detail": "invalid API token"})]})

    assert hs.check_account(BASE, ACCESS, script).state == hs.ACCOUNT_REJECTED


def test_check_account_treats_a_server_error_and_a_timeout_as_unreachable_not_rejected():
    server_error = ScriptedTransport({"/me": [Reply(503, b"")]})
    assert hs.check_account(BASE, ACCESS, server_error).state == hs.ACCOUNT_UNREACHABLE

    timeout = ScriptedTransport({"/me": [TimeoutError("slow")]})
    assert hs.check_account(BASE, ACCESS, timeout).state == hs.ACCOUNT_UNREACHABLE


def test_check_account_with_no_usable_token_is_rejected_and_sends_nothing():
    script = ScriptedTransport({})

    assert hs.check_account(BASE, "", script).state == hs.ACCOUNT_REJECTED
    assert hs.check_account(BASE, "has a space", script).state == hs.ACCOUNT_REJECTED
    assert script.requests == []


# ----------------------------------------------------------- refresh


def test_refresh_rotates_the_refresh_token_and_keeps_the_account(secrets):
    script = ScriptedTransport({"/oauth/token": [_json(200, {"access_token": NEW_ACCESS, "expires_in": 7776000, "refresh_token": ROTATED})]})
    previous = hs.Session(account=EMAIL, issued_at=NOW - 100, expires_at=NOW + 10, refreshed_at=None)

    grant = hs.refresh_grant(BASE, REFRESH, script)
    session = hs.save_refreshed(secrets, grant, previous, now=NOW)

    body = urllib.parse.parse_qs(script.requests[0]["body"].decode("ascii"))
    assert body == {"grant_type": ["refresh_token"], "refresh_token": [REFRESH]}
    assert session.account == EMAIL
    assert session.refreshed_at == NOW
    assert session.expires_at == NOW + 7776000
    saved = hs.load_saved(secrets)
    assert saved.access_token == NEW_ACCESS
    assert saved.refresh_token == ROTATED, "the server rotates the refresh token, so the new one replaces the old"


@pytest.mark.parametrize("status", [400, 401, 403])
def test_a_refused_refresh_raises_refresh_failed(status):
    script = ScriptedTransport({"/oauth/token": [_json(status, {"error": "invalid_grant"})]})

    with pytest.raises(hs.RefreshFailed):
        hs.refresh_grant(BASE, REFRESH, script)


def test_a_refresh_that_cannot_reach_the_server_raises_a_hosted_error_not_refresh_failed():
    script = ScriptedTransport({"/oauth/token": [OSError("dropped")]})

    with pytest.raises(HostedError) as caught:
        hs.refresh_grant(BASE, REFRESH, script)

    assert not isinstance(caught.value, hs.RefreshFailed)


# ----------------------------------------------------------- the whole sign-in, and the saved session


def test_sign_in_saves_the_token_the_account_and_the_expiry(secrets):
    script = ScriptedTransport(
        {
            "/oauth/device": [_device_reply()],
            "/oauth/token": [_json(200, {"access_token": ACCESS, "token_type": "bearer", "expires_in": 7776000})],
            "/me": [_json(200, {"email": EMAIL})],
        }
    )
    announced: list[hs.DeviceAuth] = []
    clock = Clock()

    session = hs.sign_in(BASE, secrets=secrets, transport=script, announce=announced.append, sleep=clock.sleep, clock=clock)

    assert [auth.user_code for auth in announced] == [USER_CODE]
    assert session.account == EMAIL
    assert session.expires_at - session.issued_at == 7776000
    saved = hs.load_saved(secrets)
    assert saved.access_token == ACCESS
    assert saved.refresh_token is None
    assert saved.session == session
    assert script.paths() == ["/oauth/device", "/oauth/token", "/me"]


def test_sign_in_does_not_save_a_token_the_server_rejects(secrets):
    script = ScriptedTransport(
        {
            "/oauth/device": [_device_reply()],
            "/oauth/token": [_json(200, {"access_token": ACCESS, "expires_in": 7776000})],
            "/me": [_json(401, {"detail": "invalid API token"})],
        }
    )
    clock = Clock()

    with pytest.raises(hs.SignInError, match="rejected"):
        hs.sign_in(BASE, secrets=secrets, transport=script, announce=lambda auth: None, sleep=clock.sleep, clock=clock)

    assert get_token(store=secrets) is None
    assert hs.load_saved(secrets) is None


def test_a_saved_session_with_a_broken_record_still_reads_the_token(secrets):
    set_token(ACCESS, store=secrets)
    set_token("not json at all", hs.SESSION_ACCOUNT, store=secrets)

    saved = hs.load_saved(secrets)

    assert saved.access_token == ACCESS
    assert saved.session == hs.Session()


def test_load_saved_is_none_without_a_token_even_if_a_record_is_left(secrets):
    set_token("{}", hs.SESSION_ACCOUNT, store=secrets)

    assert hs.load_saved(secrets) is None


def test_sign_out_removes_every_saved_part_and_reports_what_was_removed(secrets):
    set_token(ACCESS, store=secrets)
    set_token(REFRESH, hs.REFRESH_ACCOUNT, store=secrets)
    set_token('{"account":"x"}', hs.SESSION_ACCOUNT, store=secrets)

    assert hs.sign_out(secrets) is True
    assert get_token(store=secrets) is None
    assert get_token(hs.REFRESH_ACCOUNT, store=secrets) is None
    assert get_token(hs.SESSION_ACCOUNT, store=secrets) is None
    assert hs.sign_out(secrets) is False


def test_the_saved_session_record_holds_no_token_text(secrets):
    script = ScriptedTransport(
        {
            "/oauth/device": [_device_reply()],
            "/oauth/token": [_json(200, {"access_token": ACCESS, "expires_in": 7776000})],
            "/me": [_json(200, {"email": EMAIL})],
        }
    )
    clock = Clock()

    hs.sign_in(BASE, secrets=secrets, transport=script, announce=lambda auth: None, sleep=clock.sleep, clock=clock)

    record = get_token(hs.SESSION_ACCOUNT, store=secrets)
    assert ACCESS not in record and DEVICE_CODE not in record
    assert json.loads(record)["account"] == EMAIL
    assert get_token(HOSTED_TOKEN_ACCOUNT, store=secrets) == ACCESS


def test_the_keychain_entry_names_match_the_token_store_service():
    assert TOKEN_SERVICE == "meridian-desktop"
    assert hs.SESSION_ACCOUNT == "hosted-session"
    assert hs.REFRESH_ACCOUNT == "hosted-refresh-token"


def test_a_transport_failure_during_the_poll_is_a_hosted_error_with_no_token():
    script = ScriptedTransport({"/oauth/token": [HostedTimeout()]})
    auth = hs.DeviceAuth(device_code=DEVICE_CODE, user_code=USER_CODE, verification_uri=VERIFY, expires_in=300, interval=5)
    clock = Clock()

    with pytest.raises(HostedError) as caught:
        hs.poll_for_token(BASE, auth, script, sleep=clock.sleep, clock=clock)

    assert DEVICE_CODE not in str(caught.value)
