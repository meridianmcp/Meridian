"""User-initiated Zotero connect flow for Meridian.

Meridian writes to Zotero's local API on 127.0.0.1:23119. Zotero must approve
that access, and it does so in its own dialog. This module owns that approval:

* :func:`connect` is the only function that POSTs to ``/local/authorize``. A user
  starts it on purpose, with ``python -m meridian.zotero_connect connect`` or the
  ``POST /zotero/connect`` route. Nothing else calls it: not a background job, a
  filer run, a startup hook or a timer. It makes one request per call and keeps
  at least :data:`CONNECT_MIN_INTERVAL_S` seconds between calls, across threads
  and processes.
* :func:`write_with_stored_approval` is the only sender of the stored approval.
  It never calls :func:`connect`, so it never opens Zotero's dialog. A 401 deletes
  the stored key. A 403 is probed with one approval-free GET first: the key is
  deleted only when that GET succeeds, because a 403 can also mean the local API
  is switched off, and then the key is kept.
* :func:`status` is read-only: one GET, never a POST.
* :func:`disconnect` deletes the stored key. Only Zotero can revoke the approval
  itself, so the result says where to clear it.

The approval key lives in the OS keyring, under the same service name that
:mod:`meridian.tunnel_config` uses for the Zotero API key, with its own account
name. The key is sent only in the ``Authorization`` header, and only to loopback
hosts. Every request that carries it refuses a redirect off loopback, checked
on every hop. It never appears in a result, a log line or an error string.

Standard library only. The keyring is loaded lazily through ``tunnel_config``.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from meridian import zotero_client
from meridian.tunnel_config import (
    _SERVICE,
    ZoteroCredentialStoreUnavailable,
    _native_keyring,
)

_LOG = logging.getLogger(__name__)

APP_NAME = "Meridian"
CONNECT_MIN_INTERVAL_S = 60.0

# The one line a user sees when a live filer run has no stored approval.
CONNECT_FIRST_LINE = (
    "Connect Meridian to Zotero first: run 'python -m meridian.zotero_connect connect', "
    "approve it in Zotero, then run this again. Nothing was filed."
)

_APPROVAL_ACCOUNT = "zotero-local-approval"
_AUTHORIZE_PATH = "/local/authorize"
_PROBE_PATH = "/users/0/items?limit=1"
_SERVER_ID_HEADER = "Zotero-Server-ID"
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_STATE_FILENAME = "zotero-connect.json"
_PROBE_TIMEOUT_S = 2.0
_WRITE_TIMEOUT_S = 10.0
# The authorize request is held open while the user answers Zotero's dialog.
_CONNECT_TIMEOUT_S = 120.0

_REDIRECT_HOSTS = frozenset({"127.0.0.1", "localhost"})


class _RedirectRefused(Exception):
    """A reply tried to redirect a request off this machine. The request stops there, with the approval unsent."""


class _LoopbackRedirectGuard(urllib.request.HTTPRedirectHandler):
    """Refuses any redirect whose target is not 127.0.0.1 or localhost.

    urllib calls :meth:`redirect_request` once for every redirect hop, so every hop is
    checked, not only the first. A refused hop raises before urllib builds the next
    request, so no ``Authorization`` header is ever sent to the target.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        host = (urllib.parse.urlsplit(newurl).hostname or "").lower()
        if host not in _REDIRECT_HOSTS:
            try:
                fp.close()
            except Exception:  # noqa: BLE001 — closing the refused reply is best effort
                pass
            raise _RedirectRefused("Zotero tried to redirect the request off this machine; it was stopped.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_TRANSPORT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _LoopbackRedirectGuard())
_PROBE_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _LoopbackRedirectGuard())

# Serializes the cooldown check and record inside this process. The file lock below covers other processes.
_COOLDOWN_LOCK = threading.Lock()
_LOCK_TIMEOUT_S = 5.0

# The last connect() attempt in this process. The state file covers other processes.
_IN_PROCESS_LAST_ATTEMPT: float | None = None

_REVOKE_TEXT = (
    "Meridian no longer keeps its Zotero approval. Only Zotero can revoke the approval itself: "
    "open Zotero's Advanced settings and clear the write authorizations for Meridian."
)

_MESSAGES = {
    "connected": "Meridian is connected to Zotero.",
    "denied": "Zotero did not approve Meridian. Nothing was stored.",
    "local_api_disabled": "Zotero's local API is turned off. Turn it on in Zotero's Advanced settings, then try again.",
    "rate_limited": "Zotero is limiting requests right now. Try again later.",
    "timeout": "Zotero did not answer in time. Nothing was stored.",
    "unreachable": "Zotero is not reachable on this machine. Start Zotero, then try again.",
    "store_unavailable": (
        "Zotero approved Meridian, but no supported OS credential store is available, "
        "so nothing was stored."
    ),
    "error": "Zotero gave an unexpected answer, or the request was refused. Nothing was stored.",
    "needs_connection": "Meridian is not connected to Zotero. Connect Meridian to Zotero first.",
}

_STATUS_MESSAGES = {
    "connected": "Meridian is connected to Zotero.",
    "not_connected": "Zotero answers, but Meridian is not connected. Run the connect step.",
    "unreachable": "Zotero's local API did not answer on this machine. Start Zotero, then try again.",
    "local_api_disabled": "Zotero's local API is turned off. Turn it on in Zotero's Advanced settings.",
}


def _result(status: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "message": _MESSAGES[status], **extra}


class KeyringApprovalStore:
    """The approval key in the native OS keyring, under Meridian's service name."""

    def get(self) -> str | None:
        try:
            keyring = _native_keyring()
        except ZoteroCredentialStoreUnavailable:
            return None
        try:
            value = keyring.get_password(_SERVICE, _APPROVAL_ACCOUNT)
        except Exception:  # noqa: BLE001 — a broken vault reads as "not connected"
            _LOG.warning("Could not read the Zotero approval from the OS credential store.")
            return None
        return value.strip() if isinstance(value, str) and value.strip() else None

    def set(self, key: str) -> None:
        keyring = _native_keyring()  # raises ZoteroCredentialStoreUnavailable
        try:
            keyring.set_password(_SERVICE, _APPROVAL_ACCOUNT, key)
        except Exception:  # noqa: BLE001 — surface a storage failure without the cause text
            raise ZoteroCredentialStoreUnavailable(
                "Could not store the Zotero approval in the native OS credential store."
            ) from None

    def delete(self) -> bool:
        try:
            keyring = _native_keyring()
        except ZoteroCredentialStoreUnavailable:
            return False
        try:
            if keyring.get_password(_SERVICE, _APPROVAL_ACCOUNT) is None:
                return False
            keyring.delete_password(_SERVICE, _APPROVAL_ACCOUNT)
        except Exception:  # noqa: BLE001
            raise ZoteroCredentialStoreUnavailable(
                "Could not remove the Zotero approval from the native OS credential store."
            ) from None
        return True


def has_stored_approval(store: Any = None) -> bool:
    """True when an approval key is stored. The value is read only to test presence and is never returned or logged."""
    approval_store = store if store is not None else KeyringApprovalStore()
    return approval_store.get() is not None


def _require_loopback(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme in ("http", "https") and (parts.hostname or "").lower() in _LOCAL_HOSTS


def _api_base(base_url: str | None) -> str:
    return zotero_client._base_url(base_url)


def _default_transport(timeout: float) -> Callable[[str, str, bytes | None, dict[str, str]], tuple[int, str]]:
    """One real HTTP request to Zotero. HTTP error statuses are returned, not raised; transport errors propagate."""

    def send(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, str]:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with _TRANSPORT_OPENER.open(request, timeout=timeout) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    return send


def transport_failure(exc: BaseException) -> str:
    """Name a transport failure. The exception text is dropped, so it can never carry a key into a result."""
    if isinstance(exc, TimeoutError) or (
        isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError)
    ):
        return "timeout"
    if isinstance(exc, OSError):
        return "unreachable"
    return "error"


# ---------------------------------------------------------------------------
# Rate limit: one connect() per CONNECT_MIN_INTERVAL_S, across calls and processes
# ---------------------------------------------------------------------------

def _state_path() -> Path:
    return Path.home() / ".meridian" / _STATE_FILENAME


def _read_last_attempt(path: Path) -> float | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    value = data.get("last_connect_at") if isinstance(data, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _write_last_attempt(path: Path, at: float) -> None:
    """Record the attempt time atomically. Best effort: the in-process record still applies if this fails."""
    tmp_name: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".zotero-connect-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"last_connect_at": at}, handle)
        os.replace(tmp_name, path)
        tmp_name = None
    except OSError:
        _LOG.debug("could not record the Zotero connect attempt time", exc_info=True)
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _cooldown_left(now: float, path: Path) -> float:
    """Seconds until connect() may run again, or 0.0 when it may run now. Future-dated records are ignored."""
    recorded = [t for t in (_IN_PROCESS_LAST_ATTEMPT, _read_last_attempt(path)) if t is not None and t <= now]
    if not recorded:
        return 0.0
    return max(0.0, min(CONNECT_MIN_INTERVAL_S, max(recorded) + CONNECT_MIN_INTERVAL_S - now))


def _lock_path(record: Path) -> Path:
    """The sidecar lock file for the record. The record is never locked itself: it is replaced with
    os.replace, which Windows refuses while another handle has it open."""
    return record.with_name(record.name + ".lock")


def _lock_handle(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_handle(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _file_lock(path: Path, timeout: float) -> Iterator[bool]:
    """Hold a cross-process lock on ``path``, creating the file if needed. Yields True once held.

    Yields False when the lock is still busy after ``timeout`` seconds. Raises OSError when the
    lock file cannot be opened at all.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    acquired = False
    try:
        if os.name == "nt":
            # msvcrt locks a byte range, so the file must have a byte for the lock to cover.
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
        started = time.monotonic()
        while True:
            try:
                handle.seek(0)
                _lock_handle(handle)
                acquired = True
                break
            except OSError:
                if time.monotonic() - started >= timeout:
                    break
                time.sleep(0.05)
        yield acquired
    finally:
        if acquired:
            try:
                handle.seek(0)
                _unlock_handle(handle)
            except OSError:
                pass  # closing the handle releases the lock regardless
        handle.close()


def _claim_attempt(now: float, path: Path) -> float:
    """Check the cooldown and, when it is clear, record this attempt, as one atomic step.

    Returns 0.0 when the attempt is recorded and may send, or the seconds left to wait. One
    threading lock and one file lock cover the read, the check and the write, so two concurrent
    calls cannot both pass. When the lock file cannot be opened at all, the in-process record
    still applies, which is the same best-effort rule the state file follows.
    """
    global _IN_PROCESS_LAST_ATTEMPT
    with _COOLDOWN_LOCK:
        with ExitStack() as stack:
            try:
                held = stack.enter_context(_file_lock(_lock_path(path), _LOCK_TIMEOUT_S))
            except OSError:
                held = None
            if held is False:
                # Another process holds the check right now. Wait a full interval instead of racing it.
                return CONNECT_MIN_INTERVAL_S
            wait = _cooldown_left(now, path)
            if wait > 0:
                return wait
            _IN_PROCESS_LAST_ATTEMPT = now
            _write_last_attempt(path, now)
            return 0.0


# ---------------------------------------------------------------------------
# connect(): the only POST to the authorize endpoint
# ---------------------------------------------------------------------------

def _approved_key(text: str) -> str | None:
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    key = data.get("key")
    return key.strip() if isinstance(key, str) and key.strip() else None


def _authorize_result(code: int, text: str, store: Any) -> dict[str, Any]:
    if code == 429:
        return _result("rate_limited")
    if code == 403 and "not enabled" in (text or "").lower():
        return _result("local_api_disabled")
    if code in (401, 403):
        return _result("denied")
    if 200 <= code < 300:
        key = _approved_key(text or "")
        if key is None:
            return _result("error")
        approval_store = store if store is not None else KeyringApprovalStore()
        try:
            approval_store.set(key)
        except ZoteroCredentialStoreUnavailable:
            return _result("store_unavailable")
        return _result("connected")
    return _result("error")


def connect(
    *,
    base_url: str | None = None,
    transport: Callable[[str, str, bytes | None, dict[str, str]], tuple[int, str]] | None = None,
    store: Any = None,
    clock: Callable[[], float] | None = None,
    state_path: Path | None = None,
) -> dict[str, Any]:
    """Ask Zotero to approve Meridian. One POST to ``/local/authorize`` per call.

    Zotero shows its own dialog, so this call can wait for the user. A key that
    Zotero returns is stored, and nothing else is kept. Every other outcome stores
    nothing. A call inside :data:`CONNECT_MIN_INTERVAL_S` of the previous one returns
    ``cooldown`` with ``retry_after_s`` and sends nothing. Every call that does send
    counts, including one that times out.
    """
    url = _api_base(base_url) + _AUTHORIZE_PATH
    if not _require_loopback(url):
        return _result("error")
    now = (clock or time.time)()
    path = state_path if state_path is not None else _state_path()
    wait = _claim_attempt(now, path)
    if wait > 0:
        retry_after = math.ceil(wait)
        return {
            "status": "cooldown",
            "retry_after_s": retry_after,
            "message": f"Wait {retry_after} seconds before asking Zotero again.",
        }
    send = transport if transport is not None else _default_transport(_CONNECT_TIMEOUT_S)
    body = json.dumps({"appName": APP_NAME}).encode("utf-8")
    headers = {"Content-Type": "application/json", "Zotero-Allowed-Request": "1"}
    try:
        code, text = send("POST", url, body, headers)
    except Exception as exc:  # noqa: BLE001 — classified; the exception text is dropped
        return _result(transport_failure(exc))
    return _authorize_result(code, text, store)


# ---------------------------------------------------------------------------
# status(): read-only
# ---------------------------------------------------------------------------

def _http_status(response: Any) -> int | None:
    status_code = getattr(response, "status", None)
    if status_code is None:
        getcode = getattr(response, "getcode", None)
        status_code = getcode() if callable(getcode) else None
    return status_code


def _probe(open_url: Callable[..., Any], url: str) -> str:
    """One full GET. Returns ``reachable``, ``local_api_disabled`` or ``unreachable``."""
    request = urllib.request.Request(url, method="GET")
    try:
        response = open_url(request, timeout=_PROBE_TIMEOUT_S)
    except urllib.error.HTTPError as exc:
        return "local_api_disabled" if exc.code == 403 else "unreachable"
    except Exception:  # noqa: BLE001 — any failure to reach the API reads as unreachable
        return "unreachable"
    try:
        code = _http_status(response)
        headers = getattr(response, "headers", None)
        server_id = headers.get(_SERVER_ID_HEADER) if headers is not None else None
        response.read()
    except Exception:  # noqa: BLE001
        return "unreachable"
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    if code == 403:
        return "local_api_disabled"
    return "reachable" if code == 200 and server_id else "unreachable"


def status(*, base_url: str | None = None, opener: Any = None, store: Any = None) -> dict[str, Any]:
    """Read-only: one GET to the local API, then whether an approval is stored. Never sends a POST."""
    url = _api_base(base_url) + _PROBE_PATH
    if not _require_loopback(url):
        state = "unreachable"
    else:
        open_url = opener if opener is not None else _PROBE_OPENER.open
        state = _probe(open_url, url)
        if state == "reachable":
            state = "connected" if has_stored_approval(store) else "not_connected"
    return {"status": state, "message": _STATUS_MESSAGES[state]}


# ---------------------------------------------------------------------------
# write_with_stored_approval(): stored key only, never connect()
# ---------------------------------------------------------------------------

def _forget_approval(approval_store: Any) -> None:
    try:
        approval_store.delete()
    except ZoteroCredentialStoreUnavailable:
        _LOG.warning("Zotero rejected the stored approval, but it could not be removed from the OS credential store.")


def _probe_local_api(send: Callable[[str, str, bytes | None, dict[str, str]], tuple[int, str]]) -> str:
    """One GET to the local API with no approval. Returns ``reachable``, ``local_api_disabled``, or a failure status."""
    try:
        code, text = send("GET", _api_base(None) + _PROBE_PATH, None, {"Zotero-Allowed-Request": "1"})
    except Exception as exc:  # noqa: BLE001 — classified; the exception text is dropped
        return transport_failure(exc)
    if 200 <= code < 300:
        return "reachable"
    if code == 403 or "not enabled" in (text or "").lower():
        return "local_api_disabled"
    return "error"


def write_with_stored_approval(
    request: Mapping[str, Any],
    *,
    transport: Callable[[str, str, bytes | None, dict[str, str]], tuple[int, str]] | None = None,
    store: Any = None,
) -> dict[str, Any]:
    """Send one write with the stored approval. It never calls :func:`connect`, so it never opens Zotero's dialog.

    ``request`` takes ``method``, ``url`` and an optional JSON-able ``payload``.
    With no stored approval nothing is sent and the result is ``needs_connection``.
    A 401 means Zotero rejected the key, so the stored key is deleted and the result is
    ``needs_connection``. A 403 also means the local API may be switched off, so one
    approval-free GET probes it first. The key is deleted only when that GET succeeds.
    When the GET reports the local API off or cannot reach it, the result is
    ``local_api_disabled`` (or the failure status) and the key is kept. Any other failure
    also keeps the stored key.
    """
    approval_store = store if store is not None else KeyringApprovalStore()
    key = approval_store.get()
    if key is None:
        return _result("needs_connection")
    url = str(request.get("url") or "")
    if not _require_loopback(url):
        return _result("error")
    method = str(request.get("method") or "POST").upper()
    payload = request.get("payload")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Zotero-Allowed-Request": "1", "Authorization": f"Bearer {key}"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    send = transport if transport is not None else _default_transport(_WRITE_TIMEOUT_S)
    try:
        code, _text = send(method, url, body, headers)
    except Exception as exc:  # noqa: BLE001 — classified; the exception text is dropped
        return _result(transport_failure(exc))
    if code == 401:
        _forget_approval(approval_store)
        return _result("needs_connection")
    if code == 403:
        probe = _probe_local_api(send)
        if probe == "reachable":
            _forget_approval(approval_store)
            return _result("needs_connection")
        if probe == "local_api_disabled":
            return _result("local_api_disabled", http_status=403)
        return _result(probe, http_status=403)
    if 200 <= code < 300:
        return {"status": "written", "http_status": code}
    return _result("error", http_status=code)


# ---------------------------------------------------------------------------
# disconnect()
# ---------------------------------------------------------------------------

def disconnect(*, store: Any = None) -> dict[str, Any]:
    """Delete the stored approval. Zotero keeps its own record until the user clears it in Zotero's Advanced settings."""
    approval_store = store if store is not None else KeyringApprovalStore()
    try:
        removed = bool(approval_store.delete())
    except ZoteroCredentialStoreUnavailable:
        return {"status": "store_unavailable", "removed": False, "message": _REVOKE_TEXT}
    return {"status": "disconnected", "removed": removed, "message": _REVOKE_TEXT}


# ---------------------------------------------------------------------------
# CLI: the user runs these by hand
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """Print one JSON result. ``connect`` exits 0 only when connected; the others exit 0."""
    parser = argparse.ArgumentParser(
        prog="python -m meridian.zotero_connect",
        description="Connect Meridian to the Zotero desktop app.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Report whether Zotero answers and whether Meridian is connected (read-only).")
    sub.add_parser("connect", help="Ask Zotero to approve Meridian. Zotero shows a dialog; approve it there.")
    sub.add_parser("disconnect", help="Forget Meridian's stored approval. Clear it in Zotero's Advanced settings too.")
    args = parser.parse_args(argv)
    if args.command == "status":
        result = status()
    elif args.command == "connect":
        result = connect()
    else:
        result = disconnect()
    print(json.dumps(result, indent=2))
    if args.command == "connect":
        return 0 if result["status"] == "connected" else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
