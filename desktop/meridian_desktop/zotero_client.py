"""Client for Zotero on this computer. Pure Python: stdlib only, no Qt import.

Zotero is a separate desktop app that the owner installs and runs. This module checks it and
runs one action, starting it. The Zotero tab starts it only after the owner confirms.

- find_zotero_exe: looks for zotero.exe in the usual install folders. It reads folders only, never the registry.
- is_zotero_running: reads the process list (tasklist on Windows). It starts nothing.
- probe_api: asks the local API on 127.0.0.1:23119 for one item. A 200 means on. A 403 means Zotero
  has the local API turned off, which only the owner can change in Zotero's settings. No connection
  means Zotero is not running.
- search_items: quick search of the local library. It returns the title, authors, year and item key.
- start_zotero: starts zotero.exe with no arguments. Each attempt is appended to a launch log.

Every request is a plain GET to the loopback address through an injectable transport. The default
transport refuses redirects, uses no proxy and caps each body at MAX_RESPONSE_BYTES. Errors carry a
short code and fixed text, never response body text. status() never raises.
"""

from __future__ import annotations

import csv
import datetime as dt
import http.client
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meridian_desktop import local_client
from meridian_desktop.local_client import LocalClientError

ZOTERO_HOST = "127.0.0.1"
ZOTERO_PORT = 23119
ZOTERO_BASE_URL = f"http://{ZOTERO_HOST}:{ZOTERO_PORT}"
ITEMS_PATH = "/api/users/0/items"
API_VERSION_HEADER = "Zotero-API-Version"
API_VERSION = "3"
EXE_NAME = "zotero.exe"
PROBE_TIMEOUT = 2.0
SEARCH_TIMEOUT = 10.0
PROCESS_TIMEOUT = 5.0
SEARCH_LIMIT = 25
MAX_QUERY_CHARS = 200
MAX_FIELD_CHARS = 300
MAX_RESPONSE_BYTES = local_client.MAX_RESPONSE_BYTES
LAUNCH_LOG_NAME = "zotero-launch.log"
_READ_CHUNK = 64 * 1024
_YEAR = re.compile(r"(?<!\d)(1[0-9]{3}|20[0-9]{2}|21[0-9]{2})(?!\d)")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

# API states, as probe_api reports them.
API_ON = "on"
API_DISABLED = "disabled"
API_NOT_RUNNING = "not_running"
API_ERROR = "error"

ON_MESSAGE = "Zotero's local API answered."
DISABLED_MESSAGE = (
    "Zotero's local API is turned off. In Zotero, open Settings, then Advanced, and tick "
    "\"Allow other applications on this computer to communicate with Zotero\". "
    "The desktop cannot change that setting for you."
)
NOT_RUNNING_MESSAGE = (
    "Nothing is answering on 127.0.0.1:23119, so Zotero is not running. "
    "Use Start Zotero on this tab, or open Zotero yourself."
)
TIMEOUT_MESSAGE = "Zotero did not answer on 127.0.0.1:23119 in time."
UNREACHABLE_MESSAGE = "Zotero could not be reached on 127.0.0.1:23119."
TOO_LARGE_MESSAGE = f"Zotero sent more than {MAX_RESPONSE_BYTES} bytes, which the desktop does not read."
BAD_RESPONSE_MESSAGE = "Zotero sent a response the desktop could not read."
NOT_ZOTERO_MESSAGE = "Something on 127.0.0.1:23119 answered, but not like Zotero's local API."
UNCHECKED_MESSAGE = "The Zotero local API could not be checked."
BLANK_QUERY_MESSAGE = "Type a search term first. Nothing was sent to Zotero."
START_FAILED_MESSAGE = "Zotero could not be started ({})."


def _status_message(status: int) -> str:
    return f"Zotero answered with HTTP {status}, which the desktop does not expect."


class ZoteroError(Exception):
    """A Zotero request or action failed. ``code`` is a short slug. ``message`` is fixed text."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes


# transport(base_url, path, headers, timeout) -> Reply. It raises ZoteroError for a failed request.
Transport = Callable[[str, str, Mapping[str, str], float], Reply]


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Return no follow-up request, so urllib raises HTTPError for a 3xx reply instead of following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _read_bounded(response: Any) -> bytes:
    """Read at most MAX_RESPONSE_BYTES. A larger body is refused before it is parsed."""
    declared = response.headers.get("Content-Length") if response.headers is not None else None
    if declared is not None and declared.strip().isdigit() and int(declared) > MAX_RESPONSE_BYTES:
        raise ZoteroError("too_large", TOO_LARGE_MESSAGE)
    body = bytearray()
    while True:
        chunk = response.read1(min(_READ_CHUNK, MAX_RESPONSE_BYTES + 1 - len(body)))
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ZoteroError("too_large", TOO_LARGE_MESSAGE)


def _transport_error(reason: object) -> ZoteroError:
    """A refused connection, or one that never opens, means no server is listening on the port."""
    if isinstance(reason, (ConnectionRefusedError, TimeoutError)):
        return ZoteroError("not_running", NOT_RUNNING_MESSAGE)
    return ZoteroError("unreachable", UNREACHABLE_MESSAGE)


def http_get(base_url: str, path: str, headers: Mapping[str, str], timeout: float) -> Reply:
    """One loopback GET. This is the default transport. A non-2xx status comes back as a Reply."""
    try:
        local_client.assert_loopback(base_url)
    except LocalClientError as exc:
        raise ZoteroError(exc.code, str(exc)) from None
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        method="GET",
        headers={"User-Agent": local_client.USER_AGENT, "Accept": "application/json", **headers},
    )
    # An empty ProxyHandler keeps loopback traffic off any system proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects())
    try:
        with opener.open(request, timeout=timeout) as response:
            return Reply(response.status, _read_bounded(response))
    except urllib.error.HTTPError as exc:
        try:
            return Reply(exc.code, _read_bounded(exc))
        finally:
            exc.close()
    except urllib.error.URLError as exc:
        raise _transport_error(exc.reason) from None
    except TimeoutError:
        raise ZoteroError("timeout", TIMEOUT_MESSAGE) from None
    except (OSError, http.client.HTTPException, ValueError):
        raise ZoteroError("unreachable", UNREACHABLE_MESSAGE) from None


def _get(path: str, *, transport: Transport | None, timeout: float) -> Reply:
    fetch = http_get if transport is None else transport
    return fetch(ZOTERO_BASE_URL, path, {API_VERSION_HEADER: API_VERSION}, timeout)


def _json_list(body: bytes) -> list[Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ZoteroError("bad_response", BAD_RESPONSE_MESSAGE) from None
    if not isinstance(value, list):
        raise ZoteroError("bad_response", BAD_RESPONSE_MESSAGE)
    return value


# ----------------------------------------------------------------- local API


@dataclass(frozen=True)
class ApiCheck:
    """The local API state and the fixed text that says what it means."""

    state: str
    message: str


def probe_api(*, transport: Transport | None = None, timeout: float = PROBE_TIMEOUT) -> ApiCheck:
    """Ask the local API for one item. Never raises."""
    try:
        reply = _get(f"{ITEMS_PATH}?limit=1&format=json", transport=transport, timeout=timeout)
    except ZoteroError as exc:
        if exc.code == "not_running":
            return ApiCheck(API_NOT_RUNNING, NOT_RUNNING_MESSAGE)
        return ApiCheck(API_ERROR, exc.message)
    except Exception:  # noqa: BLE001 - a probe must never raise into the UI
        return ApiCheck(API_ERROR, UNCHECKED_MESSAGE)
    if reply.status == 403:
        return ApiCheck(API_DISABLED, DISABLED_MESSAGE)
    if reply.status != 200:
        return ApiCheck(API_ERROR, _status_message(reply.status))
    try:
        _json_list(reply.body)
    except ZoteroError:
        return ApiCheck(API_ERROR, NOT_ZOTERO_MESSAGE)
    return ApiCheck(API_ON, ON_MESSAGE)


def _field(value: Any) -> str:
    """A text field, with whitespace collapsed and a length cap. Anything that is not text is empty."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text if len(text) <= MAX_FIELD_CHARS else text[: MAX_FIELD_CHARS - 3] + "..."


def _authors(creators: Any) -> str:
    if not isinstance(creators, list):
        return ""
    names: list[str] = []
    for creator in creators:
        if not isinstance(creator, dict):
            continue
        last = _field(creator.get("lastName"))
        first = _field(creator.get("firstName"))
        single = _field(creator.get("name"))
        if last and first:
            names.append(f"{last}, {first}")
        elif last or single:
            names.append(last or single)
        elif first:
            names.append(first)
    return _field("; ".join(names))


def _year(date: str) -> str:
    match = _YEAR.search(date)
    return match.group(1) if match else ""


@dataclass(frozen=True)
class ZoteroItem:
    """One search result. ``year`` is empty when the item has no year."""

    key: str
    title: str
    authors: str
    year: str
    item_type: str


def item_from_entry(entry: Mapping[str, Any]) -> ZoteroItem:
    """Read one item. The local API nests the fields under "data". A flat entry is read as well."""
    data = entry.get("data")
    fields: Mapping[str, Any] = data if isinstance(data, dict) else entry
    return ZoteroItem(
        key=_field(entry.get("key")) or _field(fields.get("key")),
        title=_field(fields.get("title")) or "(no title)",
        authors=_authors(fields.get("creators")),
        year=_year(_field(fields.get("date"))),
        item_type=_field(fields.get("itemType")),
    )


def _items_from_reply(reply: Reply) -> list[ZoteroItem]:
    if reply.status == 403:
        raise ZoteroError("api_disabled", DISABLED_MESSAGE)
    if reply.status != 200:
        raise ZoteroError(f"http_{reply.status}", _status_message(reply.status))
    return [item_from_entry(entry) for entry in _json_list(reply.body) if isinstance(entry, dict)]


def search_items(
    query: str,
    *,
    transport: Transport | None = None,
    timeout: float = SEARCH_TIMEOUT,
) -> list[ZoteroItem]:
    """Quick search of the local library. A blank query raises before any request is sent."""
    term = query.strip()[:MAX_QUERY_CHARS]
    if not term:
        raise ZoteroError("blank_query", BLANK_QUERY_MESSAGE)
    path = f"{ITEMS_PATH}?q={urllib.parse.quote(term, safe='')}&format=json&limit={SEARCH_LIMIT}"
    return _items_from_reply(_get(path, transport=transport, timeout=timeout))


# ------------------------------------------------------ libraries and collections
#
# Read-only, like everything above. The local API serves the user library at /api/users/0 and each group at
# /api/groups/<id>. Reading one collection's top-level items by path is documented. Listing the groups is less well
# documented, so when that listing fails the group libraries are left out and the reason is said, not hidden.

USER_LIBRARY_ID = "0"
USER_LIBRARY_NAME = "My Library"
USER_PREFIX = "/api/users/0"
LIBRARY_USER = "user"
LIBRARY_GROUP = "group"
GROUPS_PATH = f"{USER_PREFIX}/groups?format=json"
COLLECTION_PAGE = 100
COLLECTION_PAGE_LIMIT = 50  # at most 5,000 collections in one library; more is refused, not cut short
GROUPS_UNAVAILABLE_NOTE = "Zotero did not list its group libraries, so only My Library is offered."
TOO_MANY_COLLECTIONS_MESSAGE = (
    f"This library has more than {COLLECTION_PAGE * COLLECTION_PAGE_LIMIT} collections, which the desktop does not read."
)
BAD_SCOPE_MESSAGE = "That library or collection is not a valid Zotero id. Nothing was sent to Zotero."
_COLLECTION_KEY = re.compile(r"[A-Z0-9]{8}")
_LIBRARY_ID = re.compile(r"[0-9]{1,12}")


@dataclass(frozen=True)
class ZoteroLibrary:
    """A library Zotero can serve: the user library (id "0") or a group (its numeric id)."""

    library_type: str
    library_id: str
    name: str


@dataclass(frozen=True)
class ZoteroCollection:
    """One collection. ``parent_key`` is empty for a top-level collection."""

    key: str
    name: str
    parent_key: str


def library_prefix(library: ZoteroLibrary) -> str:
    """The API prefix for a library. Any other id is refused before a request is built."""
    if library.library_type == LIBRARY_USER and library.library_id == USER_LIBRARY_ID:
        return USER_PREFIX
    if library.library_type == LIBRARY_GROUP and _LIBRARY_ID.fullmatch(library.library_id or ""):
        return f"/api/groups/{library.library_id}"
    raise ZoteroError("bad_scope", BAD_SCOPE_MESSAGE)


def _checked_key(key: str) -> str:
    if not _COLLECTION_KEY.fullmatch(key or ""):
        raise ZoteroError("bad_scope", BAD_SCOPE_MESSAGE)
    return key


def list_libraries(
    *,
    transport: Transport | None = None,
    timeout: float = SEARCH_TIMEOUT,
) -> tuple[list[ZoteroLibrary], str]:
    """My Library, then each group Zotero lists, by name, as (libraries, note).

    An unreachable local API raises, because then nothing can be read. A failed groups listing keeps My Library and
    returns the reason in the note.
    """
    libraries = [ZoteroLibrary(LIBRARY_USER, USER_LIBRARY_ID, USER_LIBRARY_NAME)]
    reply = _get(GROUPS_PATH, transport=transport, timeout=timeout)
    if reply.status == 403:
        raise ZoteroError("api_disabled", DISABLED_MESSAGE)
    if reply.status != 200:
        return libraries, GROUPS_UNAVAILABLE_NOTE
    try:
        entries = _json_list(reply.body)
    except ZoteroError:
        return libraries, GROUPS_UNAVAILABLE_NOTE
    groups: list[ZoteroLibrary] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw_id = entry.get("id")
        group_id = str(raw_id) if isinstance(raw_id, (int, str)) and not isinstance(raw_id, bool) else ""
        if not _LIBRARY_ID.fullmatch(group_id):
            continue
        data = entry.get("data")
        name = _field(data.get("name") if isinstance(data, dict) else entry.get("name"))
        groups.append(ZoteroLibrary(LIBRARY_GROUP, group_id, name or f"Group {group_id}"))
    groups.sort(key=lambda group: group.name.lower())
    return libraries + groups, ""


def _collection_from(entry: Mapping[str, Any]) -> ZoteroCollection | None:
    data = entry.get("data")
    fields: Mapping[str, Any] = data if isinstance(data, dict) else entry
    key = _field(entry.get("key")) or _field(fields.get("key"))
    if not _COLLECTION_KEY.fullmatch(key):
        return None
    parent = fields.get("parentCollection")
    parent_key = parent if isinstance(parent, str) and _COLLECTION_KEY.fullmatch(parent) else ""
    return ZoteroCollection(key=key, name=_field(fields.get("name")) or "(unnamed collection)", parent_key=parent_key)


def list_collections(
    library: ZoteroLibrary,
    *,
    transport: Transport | None = None,
    timeout: float = SEARCH_TIMEOUT,
) -> list[ZoteroCollection]:
    """Every collection in one library, flat, each with its parent key. A library too big to read in full is refused."""
    prefix = library_prefix(library)
    found: list[ZoteroCollection] = []
    start = 0
    for _page in range(COLLECTION_PAGE_LIMIT):
        path = f"{prefix}/collections?format=json&limit={COLLECTION_PAGE}&start={start}"
        reply = _get(path, transport=transport, timeout=timeout)
        if reply.status == 403:
            raise ZoteroError("api_disabled", DISABLED_MESSAGE)
        if reply.status != 200:
            raise ZoteroError(f"http_{reply.status}", _status_message(reply.status))
        page = [entry for entry in _json_list(reply.body) if isinstance(entry, dict)]
        found.extend(collection for collection in (_collection_from(entry) for entry in page) if collection)
        if len(page) < COLLECTION_PAGE:
            return sorted(found, key=lambda collection: collection.name.lower())
        start += COLLECTION_PAGE
    raise ZoteroError("too_many", TOO_MANY_COLLECTIONS_MESSAGE)


def collection_rows(collections: Sequence[ZoteroCollection]) -> list[tuple[ZoteroCollection, int]]:
    """The collections in tree order, each with its depth. One whose parent is not in the list counts as top level."""
    known = {collection.key for collection in collections}
    children: dict[str, list[ZoteroCollection]] = {}
    roots: list[ZoteroCollection] = []
    for collection in collections:
        if collection.parent_key and collection.parent_key in known:
            children.setdefault(collection.parent_key, []).append(collection)
        else:
            roots.append(collection)
    rows: list[tuple[ZoteroCollection, int]] = []
    placed: set[str] = set()

    def place(node: ZoteroCollection, depth: int) -> None:
        if node.key in placed:
            return
        placed.add(node.key)
        rows.append((node, depth))
        for child in sorted(children.get(node.key, []), key=lambda item: item.name.lower()):
            place(child, depth + 1)

    for root in sorted(roots, key=lambda item: item.name.lower()):
        place(root, 0)
    for leftover in sorted(collections, key=lambda item: item.name.lower()):
        place(leftover, 0)  # only a parent cycle reaches here; each one is still listed once
    return rows


def search_collection_items(
    query: str,
    library: ZoteroLibrary,
    collection_key: str,
    *,
    transport: Transport | None = None,
    timeout: float = SEARCH_TIMEOUT,
) -> list[ZoteroItem]:
    """Quick search inside one collection, top-level items only, so child notes and attachments are not listed."""
    term = query.strip()[:MAX_QUERY_CHARS]
    if not term:
        raise ZoteroError("blank_query", BLANK_QUERY_MESSAGE)
    prefix = library_prefix(library)
    key = _checked_key(collection_key)
    path = (
        f"{prefix}/collections/{key}/items/top?q={urllib.parse.quote(term, safe='')}"
        f"&format=json&limit={SEARCH_LIMIT}"
    )
    return _items_from_reply(_get(path, transport=transport, timeout=timeout))


# ---------------------------------------------------------- install and process


def candidate_exes(env: Mapping[str, str] | None = None) -> list[str]:
    """The usual install paths for zotero.exe, in the order they are checked."""
    env = os.environ if env is None else env
    program_dirs = [env.get("ProgramFiles"), env.get("ProgramW6432"), env.get("ProgramFiles(x86)")]
    if not any(program_dirs):
        program_dirs = [r"C:\Program Files", r"C:\Program Files (x86)"]
    candidates = [os.path.join(folder, "Zotero", EXE_NAME) for folder in program_dirs if folder]
    local = env.get("LOCALAPPDATA")
    if local:
        candidates.append(os.path.join(local, "Programs", "Zotero", EXE_NAME))
    unique: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        key = os.path.normcase(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def find_zotero_exe(
    env: Mapping[str, str] | None = None,
    is_file: Callable[[str], bool] = os.path.isfile,
) -> str | None:
    """The first usual path that holds zotero.exe, or None. Reads folders only."""
    for candidate in candidate_exes(env):
        if is_file(candidate):
            return candidate
    return None


def _tasklist_output() -> str:
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    tasklist = os.path.join(system_root, "System32", "tasklist.exe")
    result = subprocess.run(
        [tasklist, "/FI", f"IMAGENAME eq {EXE_NAME}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=PROCESS_TIMEOUT,
        check=False,
        stdin=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )
    if result.returncode != 0:
        raise OSError("tasklist did not finish")
    return result.stdout


def is_zotero_running(process_list: Callable[[], str] | None = None) -> bool | None:
    """True when a zotero.exe process is listed, False when none is, None when it cannot be read.

    ``process_list`` returns tasklist CSV text. Tests pass a fake. Off Windows the default is None.
    """
    if process_list is None:
        if sys.platform != "win32":
            return None
        process_list = _tasklist_output
    try:
        output = process_list()
        rows = csv.reader(io.StringIO(output))
        return any(bool(row) and row[0].strip().lower() == EXE_NAME for row in rows)
    except Exception:  # noqa: BLE001 - an unreadable process list is "unknown", not an error
        return None


def command_line(exe: str) -> str:
    """The command the desktop runs for ``exe``, as it would be typed. No arguments are passed."""
    return subprocess.list2cmdline([exe])


def default_log_path(env: Mapping[str, str] | None = None) -> Path:
    """The launch log, beside the settings file: %LOCALAPPDATA%\\meridian-desktop, or ~/.meridian-desktop."""
    env = os.environ if env is None else env
    local = env.get("LOCALAPPDATA")
    base = Path(local) / "meridian-desktop" if local else Path.home() / ".meridian-desktop"
    return base / LAUNCH_LOG_NAME


def _append_log(path: Path, line: str) -> None:
    """Append one line. A log that cannot be written never stops or fails the attempt."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        return


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def start_zotero(
    exe: str,
    *,
    launcher: Callable[..., Any] | None = None,
    log_path: Path | None = None,
    now: Callable[[], dt.datetime] | None = None,
) -> str:
    """Start zotero.exe once, with no arguments, and log the attempt. Returns the result line.

    Call this only after the owner has said yes. Raises ZoteroError when the process cannot be
    started. The attempt is logged either way.
    """
    launch = subprocess.Popen if launcher is None else launcher
    path = default_log_path() if log_path is None else log_path
    stamp = (now or _utc_now)().isoformat(timespec="seconds")
    command = command_line(exe)
    try:
        launch(
            [exe],
            cwd=str(Path(exe).parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=_DETACHED if sys.platform == "win32" else 0,
        )
    except Exception as exc:  # noqa: BLE001 - any start failure is reported by class name only
        _append_log(path, f"{stamp} start attempt: {command} -> failed ({exc.__class__.__name__})")
        raise ZoteroError("start_failed", START_FAILED_MESSAGE.format(exc.__class__.__name__)) from None
    _append_log(path, f"{stamp} start attempt: {command} -> launched")
    return f"Zotero was started with: {command}"


# ----------------------------------------------------------------- status


@dataclass(frozen=True)
class ZoteroStatus:
    """What is known about Zotero. ``exe`` is None when it is not installed in a usual folder.
    ``process_running`` is None when the process list cannot be read."""

    exe: str | None
    process_running: bool | None
    api: ApiCheck

    @property
    def installed(self) -> bool:
        return self.exe is not None


def status(
    *,
    env: Mapping[str, str] | None = None,
    process_list: Callable[[], str] | None = None,
    transport: Transport | None = None,
) -> ZoteroStatus:
    """Installed, process running and local API state. Never raises. Starts nothing and writes nothing."""
    try:
        exe = find_zotero_exe(env)
    except Exception:  # noqa: BLE001
        exe = None
    running = is_zotero_running(process_list)
    api = probe_api(transport=transport)
    return ZoteroStatus(exe=exe, process_running=running, api=api)


__all__ = [
    "API_DISABLED",
    "API_ERROR",
    "API_NOT_RUNNING",
    "API_ON",
    "ITEMS_PATH",
    "ZOTERO_BASE_URL",
    "ApiCheck",
    "Reply",
    "ZoteroError",
    "ZoteroItem",
    "ZoteroStatus",
    "candidate_exes",
    "command_line",
    "default_log_path",
    "find_zotero_exe",
    "http_get",
    "is_zotero_running",
    "item_from_entry",
    "probe_api",
    "search_items",
    "start_zotero",
    "status",
]
