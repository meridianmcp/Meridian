"""Browser sign-in for the hosted Meridian account, and the saved session it leaves behind. Pure Python: no Qt import.

The owner signs in through the server's device flow (RFC 8628), not by pasting a token. Every request goes through
an injectable transport, so tests pass a fake and make no network call. The routes and reply shapes below are the ones
in the server code (meridian/routes/oauth.py and meridian/server.py):

- POST {base}/oauth/device, with no body, starts the flow. The reply holds device_code, user_code, verification_uri,
  verification_uri_complete, expires_in (300 seconds) and interval (5 seconds).
- POST {base}/oauth/token, form-encoded, with grant_type urn:ietf:params:oauth:grant-type:device_code and the
  device_code, is polled every interval seconds. HTTP 200 with error authorization_pending means keep polling, and
  slow_down means poll less often. HTTP 400 with expired_token or access_denied ends the sign-in. HTTP 200 with
  access_token is success. The reply carries expires_in (7776000 seconds, 90 days) and no refresh_token.
- GET {base}/me, with Authorization: Bearer, names the account by its email. 401 or 403 means the token was rejected.
- POST {base}/oauth/token with grant_type refresh_token rotates the refresh token. The device grant issues none, so a
  refresh runs only when a refresh token was saved some other way.

Saved state lives in the OS keychain, through token_store, and never in the settings file or a log:
- the access token, under the hosted token account that the rest of the desktop reads;
- the refresh token, under REFRESH_ACCOUNT, when one was issued, replaced on every refresh;
- a session record (account, issued_at, expires_at, refreshed_at) as JSON, under SESSION_ACCOUNT.

The device code, the user code and the tokens are never logged and never put in an error message. Error messages
are fixed wording, so a failure cannot carry a value.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from meridian_desktop import hosted_client, token_store
from meridian_desktop.hosted_client import HOSTED_TIMEOUT, USER_AGENT, HostedError, Reply, Transport
from meridian_desktop.secret_store import SecretStore

DEVICE_PATH = "/oauth/device"
TOKEN_PATH = "/oauth/token"
ME_PATH = "/me"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
REFRESH_GRANT = "refresh_token"
FORM_TYPE = "application/x-www-form-urlencoded"
# The values the server sends. They are used only when a reply leaves one out.
DEFAULT_DEVICE_EXPIRES_IN = 300
DEFAULT_POLL_INTERVAL = 5
SLOW_DOWN_STEP = 5
DEFAULT_TOKEN_LIFETIME = 86400 * 90

REFRESH_ACCOUNT = "hosted-refresh-token"
SESSION_ACCOUNT = "hosted-session"

ACCOUNT_OK = "ok"
ACCOUNT_REJECTED = "rejected"
ACCOUNT_UNREACHABLE = "unreachable"


class SignInError(Exception):
    """A sign-in did not finish. The message is fixed wording in plain words."""


class SignInDenied(SignInError):
    """The owner refused the sign-in in the browser."""


class SignInExpired(SignInError):
    """The sign-in code ran out before it was approved."""


class RefreshFailed(Exception):
    """The server did not renew the session. The message is fixed wording."""


def live_transport(request: urllib.request.Request, timeout: float) -> Reply:
    """The real transport, through hosted_client. The UI passes this by name, so a test can replace it at one place."""
    return hosted_client._urllib_transport(request, timeout)  # noqa: SLF001 - the one shared hosted transport


@dataclass(frozen=True)
class DeviceAuth:
    """What the server gave at the start. device_code is the poll secret: it is never shown or logged."""

    device_code: str = field(repr=False)
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class TokenGrant:
    """A token the server issued. The token values are left out of repr."""

    access_token: str = field(repr=False)
    expires_in: int
    refresh_token: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class AccountCheck:
    """The answer from GET /me: ACCOUNT_OK with the email, ACCOUNT_REJECTED, or ACCOUNT_UNREACHABLE."""

    state: str
    email: str = ""


@dataclass(frozen=True)
class Session:
    """The non-secret part of the saved session. Times are epoch seconds, or None when no record was saved."""

    account: str = ""
    issued_at: float | None = None
    expires_at: float | None = None
    refreshed_at: float | None = None


@dataclass(frozen=True)
class SavedSession:
    """The keychain state read at once. The tokens are left out of repr."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    session: Session


def _call(transport: Transport | None, method: str, url: str, *, headers: dict[str, str], body: bytes | None = None) -> Reply:
    """One request through the transport. Failures come back as HostedError, with no value in the text."""
    request = urllib.request.Request(url, data=body, method=method, headers={"User-Agent": USER_AGENT, **headers})
    send = transport if transport is not None else live_transport
    return hosted_client._send(send, request, HOSTED_TIMEOUT)  # noqa: SLF001 - maps transport errors to HostedError


def _parse_object(raw: bytes | str | None) -> dict[str, Any]:
    """A JSON object from a reply body or a stored record. Anything else reads as an empty object."""
    if raw is None:
        return {}
    try:
        value = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def _text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    return value.strip() if isinstance(value, str) else ""


def _seconds(data: dict[str, Any], key: str, default: int) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return default
    return int(value)


def _number(data: dict[str, Any], key: str) -> float | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _same_origin(base: str, url: str) -> bool:
    """True when url has the same scheme and host as base. Only then is the browser opened to it."""
    mine = urllib.parse.urlsplit(base)
    theirs = urllib.parse.urlsplit(url)
    return bool(theirs.netloc) and (mine.scheme, mine.netloc) == (theirs.scheme, theirs.netloc)


def start_device_sign_in(base_url: str, transport: Transport | None) -> DeviceAuth:
    """POST /oauth/device. Raises SignInError when the reply has no codes, or when the page is on another host."""
    base = hosted_client.validate_base_url(base_url)
    reply = _call(transport, "POST", base + DEVICE_PATH, headers={"Accept": "application/json"}, body=b"")
    data = _parse_object(reply.body) if reply.status == 200 else {}
    device_code = _text(data, "device_code")
    user_code = _text(data, "user_code")
    uri = _text(data, "verification_uri_complete") or _text(data, "verification_uri")
    if not (device_code and user_code and uri):
        raise SignInError(f"Hosted Meridian did not start the sign-in (HTTP {reply.status}).")
    if not _same_origin(base, uri):
        raise SignInError("Hosted Meridian gave a sign-in page on another host, so no browser was opened.")
    return DeviceAuth(
        device_code=device_code,
        user_code=user_code,
        verification_uri=uri,
        expires_in=_seconds(data, "expires_in", DEFAULT_DEVICE_EXPIRES_IN),
        interval=_seconds(data, "interval", DEFAULT_POLL_INTERVAL),
    )


def _grant(data: dict[str, Any]) -> TokenGrant:
    return TokenGrant(
        access_token=_text(data, "access_token"),
        expires_in=_seconds(data, "expires_in", DEFAULT_TOKEN_LIFETIME),
        refresh_token=_text(data, "refresh_token") or None,
    )


def poll_for_token(
    base_url: str,
    auth: DeviceAuth,
    transport: Transport | None,
    *,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> TokenGrant:
    """Poll POST /oauth/token until the owner approves, the code is denied or expires, or the deadline passes.

    The first poll is sent at once. Each later poll waits the interval, and slow_down adds SLOW_DOWN_STEP to it.
    ``sleep`` and ``clock`` are injected, so a test runs the whole loop with no wait.
    """
    base = hosted_client.validate_base_url(base_url)
    deadline = clock() + auth.expires_in
    interval = auth.interval
    body = urllib.parse.urlencode({"grant_type": DEVICE_GRANT, "device_code": auth.device_code}).encode("ascii")
    while True:
        if clock() >= deadline:
            raise SignInExpired("The sign-in code ran out before it was approved. Sign in again.")
        reply = _call(
            transport,
            "POST",
            base + TOKEN_PATH,
            headers={"Content-Type": FORM_TYPE, "Accept": "application/json"},
            body=body,
        )
        data = _parse_object(reply.body)
        if reply.status == 200 and _text(data, "access_token"):
            return _grant(data)
        error = _text(data, "error")
        if error == "authorization_pending":
            pass
        elif error == "slow_down":
            interval += SLOW_DOWN_STEP
        elif error == "access_denied":
            raise SignInDenied("The sign-in was refused in the browser. Nothing was saved.")
        elif error == "expired_token":
            raise SignInExpired("The sign-in code ran out before it was approved. Sign in again.")
        else:
            raise SignInError(f"Hosted Meridian did not complete the sign-in (HTTP {reply.status}).")
        sleep(interval)


def check_account(base_url: str, token: str, transport: Transport | None) -> AccountCheck:
    """GET /me with the bearer token. 401 or 403 is a rejected token. Any other failure is unreachable."""
    try:
        bearer = hosted_client.usable_token(token)
        base = hosted_client.validate_base_url(base_url)
        reply = _call(
            transport,
            "GET",
            base + ME_PATH,
            headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json"},
        )
    except hosted_client.BadBaseUrl:
        raise
    except (hosted_client.BadToken, hosted_client.NotSignedIn):
        return AccountCheck(ACCOUNT_REJECTED)
    except HostedError:
        return AccountCheck(ACCOUNT_UNREACHABLE)
    if reply.status in (401, 403):
        return AccountCheck(ACCOUNT_REJECTED)
    if reply.status != 200:
        return AccountCheck(ACCOUNT_UNREACHABLE)
    return AccountCheck(ACCOUNT_OK, _text(_parse_object(reply.body), "email"))


def refresh_grant(base_url: str, refresh_token: str, transport: Transport | None) -> TokenGrant:
    """POST /oauth/token with grant_type refresh_token. A refused or failed renewal raises RefreshFailed.

    A transport failure raises HostedError instead, so the caller can tell a refused renewal from a dropped connection.
    """
    base = hosted_client.validate_base_url(base_url)
    body = urllib.parse.urlencode({"grant_type": REFRESH_GRANT, "refresh_token": refresh_token}).encode("ascii")
    reply = _call(
        transport,
        "POST",
        base + TOKEN_PATH,
        headers={"Content-Type": FORM_TYPE, "Accept": "application/json"},
        body=body,
    )
    data = _parse_object(reply.body)
    if reply.status == 200 and _text(data, "access_token"):
        return _grant(data)
    if reply.status in (400, 401, 403):
        raise RefreshFailed("Hosted Meridian did not accept the saved sign-in. Sign in again.")
    raise RefreshFailed(f"Hosted Meridian did not renew the sign-in (HTTP {reply.status}).")


def _write(secrets: SecretStore, grant: TokenGrant, session: Session) -> None:
    token_store.set_token(grant.access_token, store=secrets)
    if grant.refresh_token:
        token_store.set_token(grant.refresh_token, REFRESH_ACCOUNT, store=secrets)
    else:
        token_store.delete_token(REFRESH_ACCOUNT, store=secrets)
    record = {
        "account": session.account,
        "issued_at": session.issued_at,
        "expires_at": session.expires_at,
        "refreshed_at": session.refreshed_at,
    }
    token_store.set_token(json.dumps(record, separators=(",", ":")), SESSION_ACCOUNT, store=secrets)


def save_session(secrets: SecretStore, grant: TokenGrant, *, account: str, now: float) -> Session:
    """Store a new sign-in: the access token, any refresh token, and the session record. Raises TokenStoreError."""
    session = Session(account=account, issued_at=now, expires_at=now + grant.expires_in, refreshed_at=None)
    _write(secrets, grant, session)
    return session


def save_refreshed(secrets: SecretStore, grant: TokenGrant, previous: Session, *, now: float) -> Session:
    """Store a renewed sign-in in place of the old one. The account stays; the expiry starts again."""
    session = Session(account=previous.account, issued_at=now, expires_at=now + grant.expires_in, refreshed_at=now)
    _write(secrets, grant, session)
    return session


def load_saved(secrets: SecretStore) -> SavedSession | None:
    """What is saved now, or None when no access token is stored. A keychain read that fails reads as nothing saved."""
    access = token_store.get_token(store=secrets)
    if not access:
        return None
    refresh = token_store.get_token(REFRESH_ACCOUNT, store=secrets) or None
    record = _parse_object(token_store.get_token(SESSION_ACCOUNT, store=secrets))
    session = Session(
        account=_text(record, "account"),
        issued_at=_number(record, "issued_at"),
        expires_at=_number(record, "expires_at"),
        refreshed_at=_number(record, "refreshed_at"),
    )
    return SavedSession(access_token=access, refresh_token=refresh, session=session)


def sign_out(secrets: SecretStore) -> bool:
    """Remove the access token, the refresh token and the session record. True when a token was removed.

    Raises TokenStoreError when the keychain fails, so the caller never shows a sign-out that did not happen.
    """
    removed = token_store.delete_token(store=secrets)
    token_store.delete_token(REFRESH_ACCOUNT, store=secrets)
    token_store.delete_token(SESSION_ACCOUNT, store=secrets)
    return removed


def sign_in(
    base_url: str,
    *,
    secrets: SecretStore,
    transport: Transport | None,
    announce: Callable[[DeviceAuth], None],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> Session:
    """The whole browser sign-in: start, announce the code (the caller opens the browser), poll, identify, save.

    ``announce`` runs once with the device auth, before polling. A token the server rejects on /me is not saved.
    An account check that cannot reach the server still saves the token, with no account name yet.
    """
    auth = start_device_sign_in(base_url, transport)
    announce(auth)
    grant = poll_for_token(base_url, auth, transport, sleep=sleep, clock=clock)
    check = check_account(base_url, grant.access_token, transport)
    if check.state == ACCOUNT_REJECTED:
        raise SignInError("Hosted Meridian rejected the new sign-in. Nothing was saved. Sign in again.")
    return save_session(secrets, grant, account=check.email, now=clock())


__all__ = [
    "ACCOUNT_OK",
    "ACCOUNT_REJECTED",
    "ACCOUNT_UNREACHABLE",
    "REFRESH_ACCOUNT",
    "SESSION_ACCOUNT",
    "AccountCheck",
    "DeviceAuth",
    "RefreshFailed",
    "SavedSession",
    "Session",
    "SignInDenied",
    "SignInError",
    "SignInExpired",
    "TokenGrant",
    "check_account",
    "live_transport",
    "load_saved",
    "poll_for_token",
    "refresh_grant",
    "save_refreshed",
    "save_session",
    "sign_in",
    "sign_out",
    "start_device_sign_in",
]
