"""Save reading-list items into the owner's Zotero library through the Meridian Zotero bridge. Stdlib only, no Qt.

The bridge is the Meridian plugin that the owner installs in Zotero. It listens on 127.0.0.1:23125 and answers only
a request that carries its bearer token. A write also needs a grant key. When Zotero's open local writes setting is
on, a grant is answered at once with no prompt. This module never waits for a prompt and never clicks anything. If a
grant is not granted at once, it cancels the grant and reports not_open_for_writes.

- token_file_path and read_token: the token file under Meridian's local-runner state folder. A missing or empty file
  means the bridge is off, and no request is sent.
- bridge_status: GET /v1/status, which only reads.
- save_items: one grant for the batch, one POST /v1/items per item, then the grant ends. It always returns a result. The
  result lists the titles that landed, the items not saved, and any item whose write got no clear answer. It reports
  each item's start and outcome through a progress hook, so the caller can record them. It never trashes, restores or
  deletes anything.
- parse_lines: reads the "Title | URL" lines the panel shows.

Every request goes through an injectable transport: transport(method, path, headers, body, timeout) -> Reply. A
transport raises ZoteroError when a request fails. Messages are fixed text. A response body, the bearer token and a
grant key never reach a message.
"""

from __future__ import annotations

import datetime as dt
import http.client
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from meridian_desktop import __version__
from meridian_desktop.zotero_client import Reply, ZoteroError, _NoRedirects, _read_bounded

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 23125
BRIDGE_BASE_URL = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}"
STATUS_PATH = "/v1/status"
GRANTS_PATH = "/v1/grants"
ITEMS_PATH = "/v1/items"
GRANT_HEADER = "Meridian-Write-Grant"
APP_NAME = "Meridian desktop"
# The same User-Agent the desktop sends to the local server. It is kept here, so the bridge does not import that client.
USER_AGENT = f"MeridianDesktop/{__version__} (local client)"
ITEM_TYPE = "webpage"
TOKEN_SUBDIR = "zotero_bridge"
TOKEN_FILENAME = "api_token"
STATE_DIR_ENV_VAR = "MERIDIAN_LOCAL_RUNNER_STATE_DIR"
MAX_ITEMS = 50
MAX_TITLE_CHARS = 500
MAX_URL_CHARS = 2000
TIMEOUT = 10.0
# Filing into a collection (collection_keys on POST /v1/items) first shipped in bridge plugin 0.1.4 (commit 0172bb7f).
# An older bridge would ignore the field and file the item elsewhere, so the save is refused before any grant opens.
PLACEMENT_MIN_PLUGIN_VERSION = (0, 1, 4)
COLLECTION_KEY = re.compile(r"^[A-Z0-9]{8}$")
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?$")
PLACEMENT_OLD_TEXT = (
    "Nothing was saved. The Meridian Zotero bridge is older than 0.1.4, so it cannot file items into the project's "
    "collection. Update the bridge plugin, then try again."
)
PLACEMENT_UNKNOWN_TEXT = (
    "Nothing was saved. The bridge did not say which version it is, so it cannot be checked for filing into the "
    "project's collection."
)
COLLECTION_KEY_TEXT = (
    "Nothing was saved. The project's Zotero collection key is not valid. Choose the collection again in the Zotero tab."
)
# A grant is stopped this long before its expiry, so a write cannot land on the edge of it.
GRANT_MARGIN = dt.timedelta(seconds=30)
_SAFE_CODE = re.compile(r"^[a-z][a-z_]{0,39}$")
_BOM = "﻿"

# Result states.
SAVED = "saved"
BRIDGE_OFF = "bridge_off"
NOT_OPEN_FOR_WRITES = "not_open_for_writes"
GRANT_EXPIRED = "grant_expired"
UNKNOWN = "unknown"
ERROR = "error"

# Status states, as bridge_status reports them. ERROR is shared with the result states.
ANSWERING = "answering"
UNAUTHORIZED = "unauthorized"

# Outcomes of one item, as the progress hook is told them. They match the journal's outcomes.
OUTCOME_LANDED = "landed"
OUTCOME_UNKNOWN = "unknown"
OUTCOME_REFUSED = "refused"

PROMPT_OPEN_WARNING = "A prompt may be open in Zotero. Press Deny there."
PROMPT_STILL_OPEN_WARNING = "A prompt may still be open in Zotero. Press Deny there."

NOTHING_TO_SAVE = "Type at least one line as Title | URL. Nothing was saved."
TOO_MANY_TEXT = f"Save at most {MAX_ITEMS} items at a time. Nothing was saved."
LINE_TEXT = "Line {} needs a title and a URL, separated by |. Nothing was saved."
LINE_PROBLEM_TEXT = "Line {}: {}. Nothing was saved."
ITEM_PROBLEM_TEXT = "Item {}: {}. Nothing was saved and no request was sent."
EMPTY_TITLE_TEXT = "the title is empty"
CONTROL_TITLE_TEXT = "the title has a line break or a control character"
LONG_TITLE_TEXT = f"the title is longer than {MAX_TITLE_CHARS} characters"
LONG_URL_TEXT = f"the URL is longer than {MAX_URL_CHARS} characters"
BAD_URL_TEXT = "the URL must start with http:// or https:// and name a host, with no spaces"
NO_TOKEN_TEXT = (
    "The Meridian Zotero bridge is off: its token file is missing or empty. "
    "Nothing was saved and no request was sent."
)
NOT_ANSWERING_TEXT = "Nothing answered on 127.0.0.1:23125 within the connect window."
UNREACHABLE_TEXT = "The Meridian Zotero bridge could not be reached on 127.0.0.1:23125."
BAD_ADDRESS_TEXT = "The Meridian Zotero bridge address must be http on 127.0.0.1. Nothing was sent."
TIMEOUT_TEXT = (
    "The Meridian Zotero bridge did not answer in time. If it was answering a grant request, a prompt may be open "
    "in Zotero. Press Deny there."
)
PLAIN_TIMEOUT_TEXT = "The Meridian Zotero bridge did not answer in time."
TOKEN_REFUSED_TEXT = "The Meridian Zotero bridge refused its token. Check the token file, then try again."
BAD_RESPONSE_TEXT = "The Meridian Zotero bridge sent a response the desktop could not read."
SAVED_TEXT = "Saved {} item(s) to Zotero through the Meridian bridge."
NOT_OPEN_REFUSED_TEXT = "Nothing was saved. Zotero refused the request ({}), so nothing was opened or cancelled."
NOT_OPEN_UNNAMED_TEXT = (
    "Nothing was saved. Zotero's reply did not name a grant ({}), so a prompt may be open in Zotero. Press Deny there."
)
GRANT_BUSY_TEXT = "Nothing was saved. Another request's prompt is waiting in Zotero. Look there, answer it, then try again."
NOT_OPEN_TEXT = "Nothing was saved. Zotero did not grant a write at once: {}. The request was cancelled."
NOT_OPEN_PROMPT_TEXT = (
    "Nothing was saved. Zotero did not grant a write at once: {}. The request was cancelled. " + PROMPT_STILL_OPEN_WARNING
)
NOT_OPEN_CANCEL_FAILED_TEXT = (
    "Nothing was saved. Zotero did not grant a write at once: {}. The request could not be cancelled. "
    + PROMPT_STILL_OPEN_WARNING
)
NOT_OPEN_CANCEL_FAILED_NO_PROMPT_TEXT = (
    "Nothing was saved. Zotero did not grant a write at once: {}. The request could not be cancelled, and no prompt "
    "is waiting."
)
KEY_MAY_STAY_LIVE_TEXT = (
    "The write key could not be ended, so it may stay live until Zotero restarts or for up to eight hours."
)
LANDED_TEXT = "Landed in Zotero: {}."
NOTHING_LANDED_TEXT = "Nothing landed in Zotero."
NOTHING_CONFIRMED_TEXT = "Nothing is confirmed to have landed."
NOT_SAVED_TEXT = "Not saved: {}."
NOT_SENT_TEXT = "Not sent: {}."
NO_ITEMS_AFTER_TEXT = "There were no items after it."
CHECK_BEFORE_SAVE_TEXT = "Check Zotero before you save again."
GRANT_EXPIRED_TEXT = "The write grant ended after {} of {} item(s). {} {} {}"
STOPPED_TEXT = "Saved {} of {} item(s). Stopped at item {}: {}. {} {} {}"
UNKNOWN_TEXT = (
    "Saved {} of {} item(s). Item {} may or may not have been saved. Check Zotero for it before you retry. {} {}"
)
UNEXPECTED_TEXT = (
    "Saved {} of {} item(s). The save stopped unexpectedly, so item {} may or may not have been saved. "
    "Check Zotero for it before you retry. {} {}"
)
UNEXPECTED_DONE_TEXT = (
    "Saved {} of {} item(s). The save stopped unexpectedly after the last write. Check Zotero before you save again."
)
PENDING_REASON = "Zotero is asking a person to answer a prompt"
DENIED_REASON = "a person refused it"
EXPIRED_REASON = "the grant had already expired"
REVOKED_REASON = "the grant was revoked"
UNKNOWN_REASON = "Zotero did not grant it"
ITEM_REFUSED_TEXT = "Zotero refused the item ({})"
ITEM_HTTP_TEXT = "Zotero answered HTTP {}"
ITEM_BRIDGE_STOPPED = "the bridge stopped answering"
ITEM_TOKEN_REFUSED = "the bridge refused its token"
ITEM_LOST_REASON = "the reply was lost"
ITEM_UNCONFIRMED = "the bridge's reply did not confirm the item"
ITEM_JOURNAL_FAILED = "the save journal could not be written, so the item was not sent"
ITEM_GRANT_ENDED = "the write grant is no longer live"
ANSWERING_STATUS = "Bridge: answering on 127.0.0.1:23125."
# The bridge-off and could-not-be-checked lines say what to do, and name the bridge, never the Meridian server.
CHECK_BRIDGE_TEXT = "Check that Zotero is open with the Meridian Zotero bridge installed, then press Refresh."
NO_TOKEN_STATUS = (
    "Bridge: off. Its token file is missing or empty, so nothing is sent to 127.0.0.1:23125. " + CHECK_BRIDGE_TEXT
)
NOT_ANSWERING_STATUS = (
    "Bridge: off. Nothing answered on 127.0.0.1:23125 within the connect window. " + CHECK_BRIDGE_TEXT
)
UNAUTHORIZED_STATUS = "Bridge: running, but it refused the token."
# The {} is one reason from STATUS_REASONS, or an HTTP status.
STATUS_ERROR_STATUS = "Bridge: could not be checked on 127.0.0.1:23125. {}. " + CHECK_BRIDGE_TEXT
STATUS_REASONS = {
    "timeout": "The bridge did not answer in time",
    "unreachable": "The bridge could not be reached",
}
STATUS_REASON_DEFAULT = "The bridge gave no usable answer"

# transport(method, path, headers, body, timeout) -> Reply. It raises ZoteroError for a failed request.
Transport = Callable[[str, str, Mapping[str, str], bytes | None, float], Reply]


class SaveProgress(Protocol):
    """Told as each item's write starts, and as its outcome is known.

    ``started`` may raise: the item is then not sent. ``finished`` may raise: an item that landed is then reported as
    unknown, so nothing is retried without a check.
    """

    def started(self, index: int) -> None: ...

    def finished(self, index: int, outcome: str) -> None: ...


class _NoProgress:
    def started(self, index: int) -> None:
        return None

    def finished(self, index: int, outcome: str) -> None:
        return None


@dataclass(frozen=True)
class ReadingItem:
    """One item to save. The title and the URL are plain text, already stripped by the reader."""

    title: str
    url: str


@dataclass(frozen=True)
class SaveResult:
    """What a save did.

    ``saved`` counts the items Zotero created before the batch stopped, and ``landed`` holds their titles in order.
    An item whose write got no clear answer is not counted as saved or as not saved. It is reported as unknown.
    """

    state: str
    saved: int
    message: str
    landed: tuple[str, ...] = ()


@dataclass(frozen=True)
class BridgeStatus:
    """Whether the bridge answers and accepts the token. ``message`` is fixed text for a person to read."""

    state: str
    message: str


class BridgeInputError(Exception):
    """The lines a person typed cannot be saved. ``message`` says which line and why, in fixed text."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class _Refusal:
    """One item the bridge did not take: the result state it implies, and a short reason."""

    state: str
    reason: str


class _NotOpen(Exception):
    """A grant was not granted at once. ``reason`` is a short phrase for the message.

    ``refused``: Zotero refused the grant request, so no grant exists. ``unnamed``: the reply carried no grant, but it
    was not a refusal, so a prompt may be open. ``prompt_possible``: a prompt may still be on screen. ``cancel_failed``:
    the grant existed and the request to cancel it failed.
    """

    def __init__(
        self,
        reason: str,
        *,
        refused: bool = False,
        unnamed: bool = False,
        prompt_possible: bool = False,
        cancel_failed: bool = False,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.refused = refused
        self.unnamed = unnamed
        self.prompt_possible = prompt_possible
        self.cancel_failed = cancel_failed


@dataclass
class _Phase:
    """How far the batch got with the grant. It decides which warnings a message may carry."""

    requested: bool = False
    grant_id: str | None = None
    grant_ended: bool | None = None


def token_file_path(env: Mapping[str, str] | None = None) -> Path:
    """The token file: zotero_bridge/api_token under Meridian's local-runner state folder, where the plugin keeps it.

    The override folder named by MERIDIAN_LOCAL_RUNNER_STATE_DIR is used as it is. Otherwise the folder is
    ~/.meridian/local_runner.
    """
    env = os.environ if env is None else env
    override = env.get(STATE_DIR_ENV_VAR, "").strip()
    base = Path(override) if override else Path.home() / ".meridian" / "local_runner"
    return base / TOKEN_SUBDIR / TOKEN_FILENAME


def read_token(path: Path | None = None) -> str | None:
    """The bridge token, or None when the file is missing, unreadable or holds nothing usable. Never raises.

    The plugin tolerates whitespace and a byte-order mark at either end of the file, so this does too.
    """
    target = token_file_path() if path is None else path
    try:
        raw = target.read_bytes()
    except OSError:
        return None
    token = raw.decode("utf-8", errors="replace").strip().lstrip(_BOM).strip()
    if not token or not token.isascii() or any(ch.isspace() for ch in token):
        return None
    return token


def _transport_error(reason: object) -> ZoteroError:
    """Map the reason urllib gives for a request that failed before a reply.

    urllib wraps a failure in URLError only while it connects or sends the request. A refused connection, or a timeout
    inside URLError, therefore means nothing answered within the connect window. The request is small, so a timeout while
    sending it is not expected here. A reply that never comes is not wrapped: its timeout is raised as a bare TimeoutError
    and handled as a timeout by the caller.
    """
    if isinstance(reason, (ConnectionRefusedError, TimeoutError)):
        return ZoteroError("not_running", NOT_ANSWERING_TEXT)
    return ZoteroError("unreachable", UNREACHABLE_TEXT)


def _read_error_body(exc: urllib.error.HTTPError) -> bytes:
    """The body of an error reply. The status is what callers need, so a body that cannot be read is dropped, not raised."""
    try:
        return _read_bounded(exc)
    except (ZoteroError, OSError, http.client.HTTPException, ValueError):
        return b""


def _assert_bridge_address(base_url: str) -> None:
    """Raise ZoteroError("not_loopback") unless ``base_url`` is http on 127.0.0.1, with no user name, query or fragment.

    The bridge has one loopback address, so the check lives here rather than in the Meridian server client.
    """
    try:
        parts = urllib.parse.urlsplit(base_url)
        port = parts.port
    except ValueError:
        raise ZoteroError("not_loopback", BAD_ADDRESS_TEXT) from None
    if (
        parts.scheme != "http"
        or parts.hostname != BRIDGE_HOST
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ZoteroError("not_loopback", BAD_ADDRESS_TEXT)


def bridge_transport(base_url: str = BRIDGE_BASE_URL) -> Transport:
    """A loopback transport for one base URL. http_request, the default, is this for the bridge on 127.0.0.1:23125.

    A non-2xx status comes back as a Reply. A redirect is not followed. No Origin header is sent, so a browser page
    cannot use this path. Each request goes through an empty proxy handler, so loopback traffic never leaves the machine.
    A base URL that is not http on 127.0.0.1 is refused before any request is made.
    """

    def transport(method: str, path: str, headers: Mapping[str, str], body: bytes | None, timeout: float) -> Reply:
        _assert_bridge_address(base_url)
        request = urllib.request.Request(
            base_url + path,
            data=body,
            method=method,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json", **headers},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects())
        try:
            with opener.open(request, timeout=timeout) as response:
                return Reply(response.status, _read_bounded(response))
        except urllib.error.HTTPError as exc:
            try:
                return Reply(exc.code, _read_error_body(exc))
            finally:
                exc.close()
        except urllib.error.URLError as exc:
            raise _transport_error(exc.reason) from None
        except TimeoutError:
            raise ZoteroError("timeout", TIMEOUT_TEXT) from None
        except (OSError, http.client.HTTPException, ValueError):
            raise ZoteroError("unreachable", UNREACHABLE_TEXT) from None

    return transport


http_request: Transport = bridge_transport()


def _send(
    fetch: Transport,
    method: str,
    path: str,
    token: str,
    *,
    grant_key: str | None = None,
    body: Mapping[str, object] | None = None,
) -> Reply:
    """One request with the bearer token, and the grant key when a write needs one. A 401 raises for every caller."""
    headers = {"Authorization": f"Bearer {token}"}
    if grant_key is not None:
        headers[GRANT_HEADER] = grant_key
    payload = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(body).encode("utf-8")
    reply = fetch(method, path, headers, payload, TIMEOUT)
    if reply.status == 401:
        raise ZoteroError("unauthorized", TOKEN_REFUSED_TEXT)
    return reply


def _grant_request(
    fetch: Transport,
    method: str,
    path: str,
    token: str,
    *,
    body: Mapping[str, object] | None = None,
    prompt_warning: bool,
) -> Reply:
    """A request in the grant phase. ``prompt_warning`` is set only for the grant request itself, which may open a prompt.

    A refused connection and a refused token pass through unchanged, because nothing was opened. A timeout's text already
    warns about a prompt. Any other transport failure of the grant request gets the prompt warning added. Requests after a
    granted reply say nothing about a prompt, because none is open.
    """
    try:
        return _send(fetch, method, path, token, body=body)
    except ZoteroError as exc:
        if exc.code in ("not_running", "unauthorized"):
            raise
        if exc.code == "timeout":
            raise ZoteroError("timeout", TIMEOUT_TEXT if prompt_warning else PLAIN_TIMEOUT_TEXT) from None
        if not prompt_warning:
            raise
        raise ZoteroError(exc.code, f"{exc.message} {PROMPT_OPEN_WARNING}") from None


def _json_object(body: bytes) -> dict:
    """The JSON object in a body, or an empty dict. Never raises, and the text is never shown."""
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def _error_code(reply: Reply) -> str:
    """The bridge's short error code, when it sent a safe one. Otherwise "unknown". Its text is never used."""
    error = _json_object(reply.body).get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and _SAFE_CODE.match(code) else "unknown"


def _parse_time(value: object) -> dt.datetime | None:
    """An ISO 8601 time from the bridge, as an aware datetime. None when it is missing or cannot be read."""
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.timezone.utc)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def check_item(title: str, url: str) -> str | None:
    """The first problem with one item, as a short phrase, or None when it is fine. Makes no request."""
    stripped = title.strip()
    if not stripped:
        return EMPTY_TITLE_TEXT
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in stripped):
        return CONTROL_TITLE_TEXT
    if len(stripped) > MAX_TITLE_CHARS:
        return LONG_TITLE_TEXT
    if len(url) > MAX_URL_CHARS:
        return LONG_URL_TEXT
    if not url or any(ch.isspace() for ch in url):
        return BAD_URL_TEXT
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return BAD_URL_TEXT
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return BAD_URL_TEXT
    return None


def parse_lines(text: str) -> list[ReadingItem]:
    """Read the lines a person typed, each "Title | URL". Blank lines are skipped. The URL is the text after the last |.

    Raises BridgeInputError, with the line number, before anything is sent. The items come back in line order, one per
    non-blank line.
    """
    items: list[ReadingItem] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        title, separator, url = line.rpartition("|")
        if not separator:
            raise BridgeInputError(LINE_TEXT.format(number))
        title, url = title.strip(), url.strip()
        problem = check_item(title, url)
        if problem is not None:
            raise BridgeInputError(LINE_PROBLEM_TEXT.format(number, problem))
        items.append(ReadingItem(title, url))
    if not items:
        raise BridgeInputError(NOTHING_TO_SAVE)
    if len(items) > MAX_ITEMS:
        raise BridgeInputError(TOO_MANY_TEXT)
    return items


def bridge_status(*, transport: Transport | None = None, token_path: Path | None = None) -> BridgeStatus:
    """Whether the bridge answers and accepts the token. Reads only, and never raises."""
    token = read_token(token_path)
    if token is None:
        return BridgeStatus(BRIDGE_OFF, NO_TOKEN_STATUS)
    fetch = http_request if transport is None else transport
    try:
        reply = _send(fetch, "GET", STATUS_PATH, token)
    except ZoteroError as exc:
        if exc.code == "unauthorized":
            return BridgeStatus(UNAUTHORIZED, UNAUTHORIZED_STATUS)
        if exc.code == "not_running":
            return BridgeStatus(BRIDGE_OFF, NOT_ANSWERING_STATUS)
        return BridgeStatus(ERROR, STATUS_ERROR_STATUS.format(STATUS_REASONS.get(exc.code, STATUS_REASON_DEFAULT)))
    if reply.status == 200:
        return BridgeStatus(ANSWERING, ANSWERING_STATUS)
    return BridgeStatus(ERROR, STATUS_ERROR_STATUS.format(f"HTTP {reply.status}"))


def _batch_problem(items: Sequence[ReadingItem]) -> str | None:
    """The first problem with the batch, or None. Checked before any request is sent."""
    if not items:
        return NOTHING_TO_SAVE
    if len(items) > MAX_ITEMS:
        return TOO_MANY_TEXT
    for number, item in enumerate(items, start=1):
        problem = check_item(item.title, item.url)
        if problem is not None:
            return ITEM_PROBLEM_TEXT.format(number, problem)
    return None


def _not_granted(status: object, *, after_granted: bool) -> _NotOpen:
    """The exception for a grant that is not granted.

    A prompt can be on screen only when the grant request itself answered pending, or when the grant is still unknown
    before the bridge had said it was granted.
    """
    if status == "denied":
        return _NotOpen(DENIED_REASON)
    if status == "expired":
        return _NotOpen(EXPIRED_REASON)
    if status == "revoked":
        return _NotOpen(REVOKED_REASON)
    if status == "pending":
        return _NotOpen(PENDING_REASON, prompt_possible=True)
    return _NotOpen(UNKNOWN_REASON, prompt_possible=not after_granted)


def _open_grant(fetch: Transport, token: str, phase: _Phase) -> tuple[str, str, dt.datetime | None]:
    """Ask for a write grant and read its key. Raises _NotOpen when it is not granted at once.

    Any grant that exists and is not usable is cancelled before the error leaves this function, and ``phase`` records
    whether the cancel worked. A grant with no expires_at is still usable: the expiry is then unknown, and the bridge's own
    refusal (grant_required) stops the batch if the grant has lapsed.
    """
    phase.requested = True
    reply = _grant_request(fetch, "POST", GRANTS_PATH, token, body={"app_name": APP_NAME}, prompt_warning=True)
    body = _json_object(reply.body)
    grant_id = body.get("grant_id")
    if not isinstance(grant_id, str) or not grant_id:
        if reply.status < 400 or reply.status >= 500:
            raise _NotOpen(f"HTTP {reply.status}", unnamed=True)
        raise _NotOpen(_error_code(reply), refused=True)
    phase.grant_id = grant_id
    try:
        if reply.status not in (200, 202):
            raise _NotOpen(f"HTTP {reply.status}", prompt_possible=True)
        if body.get("status") != "granted":
            raise _not_granted(body.get("status"), after_granted=False)
        poll = _grant_request(fetch, "GET", f"{GRANTS_PATH}/{grant_id}", token, prompt_warning=False)
        grant = _json_object(poll.body)
        if poll.status != 200:
            raise ZoteroError("bad_response", BAD_RESPONSE_TEXT)
        if grant.get("status") != "granted":
            raise _not_granted(grant.get("status"), after_granted=True)
        write_key = grant.get("write_key")
        if not isinstance(write_key, str) or not write_key:
            raise ZoteroError("bad_response", BAD_RESPONSE_TEXT)
        expires_at = _parse_time(grant.get("expires_at"))
    except _NotOpen as exc:
        phase.grant_ended = _end_grant(fetch, token, grant_id)
        if not phase.grant_ended:
            raise _NotOpen(exc.reason, prompt_possible=exc.prompt_possible, cancel_failed=True) from None
        raise
    except BaseException:
        phase.grant_ended = _end_grant(fetch, token, grant_id)
        raise
    return grant_id, write_key, expires_at


def _item_body(item: ReadingItem, collection_key: str | None = None) -> dict[str, object]:
    """Title and URL go inside "fields". A top-level title is refused by the bridge with 422 invalid_fields.

    With a collection key the bridge files the item into that collection as it creates it.
    """
    body: dict[str, object] = {"item_type": ITEM_TYPE, "fields": {"title": item.title.strip(), "url": item.url.strip()}}
    if collection_key is not None:
        body["collection_keys"] = [collection_key]
    return body


def _parse_version(value: object) -> tuple[tuple[int, int, int], bool] | None:
    """A plugin version such as 0.1.4 or 0.1.5-beta: its numbers, and whether it is a pre-release. None if it is neither.

    The text is never shown.
    """
    if not isinstance(value, str):
        return None
    match = _VERSION.fullmatch(value)
    if match is None:
        return None
    numbers = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return numbers, match.group(4) is not None


def _can_file_into_collections(version: tuple[tuple[int, int, int], bool]) -> bool:
    """True from 0.1.4 on. A pre-release of 0.1.4 sorts before 0.1.4, as it does in semantic versioning."""
    numbers, prerelease = version
    if numbers == PLACEMENT_MIN_PLUGIN_VERSION:
        return not prerelease
    return numbers > PLACEMENT_MIN_PLUGIN_VERSION


def _placement_problem(fetch: Transport, token: str) -> SaveResult | None:
    """None when the bridge can file an item into a collection. Reads only: no grant is asked for and no item is written.

    A status read that fails is reported as an unscoped save reports the same failure, so the message names the real
    problem. A reply that is not 200, or that names no usable version, is unknown and is refused.
    """
    try:
        reply = _send(fetch, "GET", STATUS_PATH, token)
    except ZoteroError as exc:
        if exc.code == "timeout":
            # A status read never opens a grant, so the grant prompt warning in the grant timeout text does not apply.
            return SaveResult(ERROR, 0, PLAIN_TIMEOUT_TEXT)
        return SaveResult(BRIDGE_OFF if exc.code == "not_running" else ERROR, 0, exc.message)
    if reply.status != 200:
        return SaveResult(ERROR, 0, PLACEMENT_UNKNOWN_TEXT)
    version = _parse_version(_json_object(reply.body).get("plugin_version"))
    if version is None:
        return SaveResult(ERROR, 0, PLACEMENT_UNKNOWN_TEXT)
    if not _can_file_into_collections(version):
        return SaveResult(ERROR, 0, PLACEMENT_OLD_TEXT)
    return None


def _create_item(
    fetch: Transport,
    token: str,
    write_key: str | None,
    item: ReadingItem,
    collection_key: str | None = None,
) -> _Refusal | None:
    """Write one item. None when Zotero created it. Otherwise the refusal that stops the batch.

    Only a 201 confirms the item. A 4xx is a refusal, so nothing was written. Anything else, including a 5xx, a 2xx other
    than 201, a 3xx, a timeout and a lost reply, may have created the item, so it is unknown and is not retried.
    """
    try:
        reply = _send(fetch, "POST", ITEMS_PATH, token, grant_key=write_key, body=_item_body(item, collection_key))
    except ZoteroError as exc:
        if exc.code == "not_running":
            return _Refusal(BRIDGE_OFF, ITEM_BRIDGE_STOPPED)
        if exc.code == "unauthorized":
            return _Refusal(ERROR, ITEM_TOKEN_REFUSED)
        return _Refusal(UNKNOWN, ITEM_LOST_REASON)
    if reply.status == 201:
        return None
    if reply.status == 403 and _error_code(reply) == "grant_required":
        return _Refusal(GRANT_EXPIRED, ITEM_GRANT_ENDED)
    if reply.status == 422:
        return _Refusal(ERROR, ITEM_REFUSED_TEXT.format(_error_code(reply)))
    if reply.status < 400 or reply.status >= 500:
        return _Refusal(UNKNOWN, ITEM_UNCONFIRMED)
    return _Refusal(ERROR, ITEM_HTTP_TEXT.format(reply.status))


def _end_grant(fetch: Transport, token: str, grant_id: str) -> bool:
    """End a grant at once. True when the bridge confirmed it ended, or it was already gone.

    This runs in cleanup, so nothing may escape from it. Any failure is reported as False, and the caller then warns that
    the key may stay live.
    """
    try:
        reply = _send(fetch, "DELETE", f"{GRANTS_PATH}/{grant_id}", token)
    except Exception:  # noqa: BLE001 - cleanup must never raise
        return False
    return 200 <= reply.status < 300 or reply.status == 404


def _lapsed(clock: Callable[[], dt.datetime], expires_at: dt.datetime | None) -> bool:
    """True when the grant is at or near its expiry. An unknown expiry never lapses here."""
    return expires_at is not None and clock() + GRANT_MARGIN >= expires_at


def _landed(items: Sequence[ReadingItem], saved: int) -> tuple[str, ...]:
    return tuple(item.title for item in items[:saved])


def _landed_text(landed: tuple[str, ...], *, empty: str = NOTHING_LANDED_TEXT) -> str:
    return empty if not landed else LANDED_TEXT.format("; ".join(landed))


def _span(first: int, last: int) -> str:
    return f"item {first}" if first == last else f"items {first} to {last}"


def _not_open_message(exc: _NotOpen) -> str:
    """The message for a grant that was not granted at once. The prompt warning appears only when a prompt may be open."""
    if exc.unnamed:
        return NOT_OPEN_UNNAMED_TEXT.format(exc.reason)
    if exc.refused:
        if exc.reason == "grant_busy":
            return GRANT_BUSY_TEXT
        return NOT_OPEN_REFUSED_TEXT.format(exc.reason)
    if exc.cancel_failed:
        template = NOT_OPEN_CANCEL_FAILED_TEXT if exc.prompt_possible else NOT_OPEN_CANCEL_FAILED_NO_PROMPT_TEXT
        return template.format(exc.reason)
    template = NOT_OPEN_PROMPT_TEXT if exc.prompt_possible else NOT_OPEN_TEXT
    return template.format(exc.reason)


def _grant_ended(items: Sequence[ReadingItem], saved: int) -> SaveResult:
    total = len(items)
    landed = _landed(items, saved)
    message = GRANT_EXPIRED_TEXT.format(
        saved,
        total,
        _landed_text(landed),
        NOT_SAVED_TEXT.format(_span(saved + 1, total)),
        CHECK_BEFORE_SAVE_TEXT,
    )
    return SaveResult(GRANT_EXPIRED, saved, message, landed)


def _stopped(items: Sequence[ReadingItem], saved: int, number: int, refusal: _Refusal) -> SaveResult:
    total = len(items)
    landed = _landed(items, saved)
    message = STOPPED_TEXT.format(
        saved,
        total,
        number,
        refusal.reason,
        _landed_text(landed),
        NOT_SAVED_TEXT.format(_span(number, total)),
        CHECK_BEFORE_SAVE_TEXT,
    )
    return SaveResult(refusal.state, saved, message, landed)


def _unknown(items: Sequence[ReadingItem], saved: int, number: int) -> SaveResult:
    """Item ``number`` got no clear answer, so it is reported as unknown. It is not counted as saved or as not saved."""
    total = len(items)
    landed = _landed(items, saved)
    rest = NOT_SENT_TEXT.format(_span(number + 1, total)) if number < total else NO_ITEMS_AFTER_TEXT
    message = UNKNOWN_TEXT.format(saved, total, number, _landed_text(landed, empty=NOTHING_CONFIRMED_TEXT), rest)
    return SaveResult(UNKNOWN, saved, message, landed)


def _unexpected_stop(items: Sequence[ReadingItem], landed: list[str], phase: _Phase) -> SaveResult:
    """A save that stopped on an error no branch expected. It still lists what landed, and treats the next item as unknown.

    The prompt warning appears only when a grant request may have been sent and no grant is known. The key warning
    appears only when a grant exists and its cancel did not confirm.
    """
    total = len(items)
    saved = len(landed)
    confirmed = tuple(landed)
    if saved < total:
        number = saved + 1
        rest = NOT_SENT_TEXT.format(_span(number + 1, total)) if number < total else NO_ITEMS_AFTER_TEXT
        message = UNEXPECTED_TEXT.format(saved, total, number, _landed_text(confirmed, empty=NOTHING_CONFIRMED_TEXT), rest)
    else:
        message = UNEXPECTED_DONE_TEXT.format(saved, total)
    if phase.requested and phase.grant_id is None:
        message = f"{message} {PROMPT_OPEN_WARNING}"
    if phase.grant_id is not None and phase.grant_ended is not True:
        message = f"{message} {KEY_MAY_STAY_LIVE_TEXT}"
    return SaveResult(UNKNOWN, saved, message, confirmed)


def _outcome(refusal: _Refusal | None) -> str:
    if refusal is None:
        return OUTCOME_LANDED
    if refusal.state == UNKNOWN:
        return OUTCOME_UNKNOWN
    return OUTCOME_REFUSED


def _write_batch(
    fetch: Transport,
    token: str,
    write_key: str | None,
    items: Sequence[ReadingItem],
    clock: Callable[[], dt.datetime],
    expires_at: dt.datetime | None,
    landed: list[str],
    progress: SaveProgress,
    collection_key: str | None = None,
) -> SaveResult:
    """Write the items in order. Each title Zotero confirms is appended to ``landed`` as soon as it is confirmed."""
    for number, item in enumerate(items, start=1):
        index = number - 1
        if _lapsed(clock, expires_at):
            return _grant_ended(items, len(landed))
        try:
            progress.started(index)
        except Exception:  # noqa: BLE001 - if the journal cannot record the write, the item is not sent
            return _stopped(items, len(landed), number, _Refusal(ERROR, ITEM_JOURNAL_FAILED))
        refusal = _create_item(fetch, token, write_key, item, collection_key)
        try:
            progress.finished(index, _outcome(refusal))
        except Exception:  # noqa: BLE001 - a write that landed but was not recorded is reported as unknown
            if refusal is None:
                return _unknown(items, len(landed), number)
        if refusal is None:
            landed.append(item.title)
            continue
        saved = len(landed)
        if refusal.state == GRANT_EXPIRED:
            return _grant_ended(items, saved)
        if refusal.state == UNKNOWN:
            return _unknown(items, saved, number)
        return _stopped(items, saved, number, refusal)
    return SaveResult(SAVED, len(landed), SAVED_TEXT.format(len(landed)), tuple(landed))


def _save_granted(
    items: Sequence[ReadingItem],
    fetch: Transport,
    token: str,
    clock: Callable[[], dt.datetime],
    landed: list[str],
    phase: _Phase,
    progress: SaveProgress,
    collection_key: str | None = None,
) -> SaveResult:
    """The writes, with the bridge's token only. The bridge asks for no grant, so no grant is opened or ended here."""
    try:
        return _write_batch(fetch, token, None, items, clock, None, landed, progress, collection_key)
    except Exception:  # noqa: BLE001 - the batch must still report what landed, whatever failed
        return _unexpected_stop(items, landed, phase)


def save_items(
    items: Sequence[ReadingItem],
    *,
    transport: Transport | None = None,
    token_path: Path | None = None,
    now: Callable[[], dt.datetime] | None = None,
    progress: SaveProgress | None = None,
    collection_key: str | None = None,
) -> SaveResult:
    """Save the items as one batch: one grant, one write per item, then the grant ends. Always returns a result.

    With ``collection_key`` every item is filed into that collection. A bridge older than 0.1.4, or one that reports no
    version, is refused before any grant opens, so nothing lands outside the collection.

    The batch is checked before any request. A grant that is not granted at once writes nothing. A refusal stops the
    batch. The result names the titles that landed, the items not saved, and an item whose write got no clear answer,
    which is reported as unknown and never retried. ``progress`` is told when each item's write starts and what its
    outcome is. If the grant cannot be ended, the result says the key may stay live. The batch never trashes or deletes.
    """
    problem = _batch_problem(items)
    if problem is not None:
        return SaveResult(ERROR, 0, problem)
    if collection_key is not None and COLLECTION_KEY.fullmatch(collection_key) is None:
        return SaveResult(ERROR, 0, COLLECTION_KEY_TEXT)
    token = read_token(token_path)
    if token is None:
        return SaveResult(BRIDGE_OFF, 0, NO_TOKEN_TEXT)
    fetch = http_request if transport is None else transport
    clock = _utc_now if now is None else now
    recorder: SaveProgress = _NoProgress() if progress is None else progress
    landed: list[str] = []
    phase = _Phase()
    try:
        if collection_key is not None:
            refused = _placement_problem(fetch, token)
            if refused is not None:
                return refused
        return _save_granted(items, fetch, token, clock, landed, phase, recorder, collection_key)
    except Exception:  # noqa: BLE001 - last line of defence: a save never raises into the panel
        return _unexpected_stop(items, landed, phase)


__all__ = [
    "ANSWERING",
    "BRIDGE_OFF",
    "ERROR",
    "GRANT_EXPIRED",
    "NOT_OPEN_FOR_WRITES",
    "OUTCOME_LANDED",
    "OUTCOME_REFUSED",
    "OUTCOME_UNKNOWN",
    "PROMPT_OPEN_WARNING",
    "SAVED",
    "UNAUTHORIZED",
    "UNKNOWN",
    "BridgeInputError",
    "BridgeStatus",
    "ReadingItem",
    "SaveProgress",
    "SaveResult",
    "bridge_status",
    "bridge_transport",
    "check_item",
    "http_request",
    "parse_lines",
    "read_token",
    "save_items",
    "token_file_path",
]
