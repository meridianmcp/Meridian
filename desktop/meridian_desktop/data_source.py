"""Choose the data source for the Projects view. Pure Python: no Qt import.

``resolve`` reads the mode from Settings and returns one source, never two.

Local mode:
- the local Meridian server, when its probe state is running: source "server".
- otherwise the database file, when db_path names an existing file: source "file".
- otherwise state "no_local_data", with a message and no rows.

Hosted mode:
- no stored token: state "not_signed_in". Nothing is probed, read or fetched.
- a stored token: the rows from the hosted client, source "hosted".

The tunnel belongs to hosted mode. Hosted tools reach this computer through the tunnel, so the
projects come from the same hosted client with the same token. The tunnel's state is shown on the
top bar, not checked here, and a stopped tunnel does not change what is read.

This module makes no network call and reads no file itself. Each outside effect is an
injected callable in DataSources. The app passes the real functions (local_client.probe_health,
local_client.list_projects, projects_reader.list_projects, token_store.get_token and hosted_client.list_projects),
and tests pass fakes. The token goes only to the hosted client. It never appears in a
Resolution, a message or an error.

A failed read gives state "read_failed", with the chosen source and no rows. A HostedError
(from hosted_client) gives its own fixed text, which holds no token, body or path. Any other
exception is named by its class only, never a path, a token or a response body.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from meridian_desktop import local_client
from meridian_desktop.hosted_client import HostedError
from meridian_desktop.settings_store import VALID_MODES, Settings

# The modes whose projects come from the hosted client.
HOSTED_PATH_MODES = ("hosted",)

SOURCE_SERVER = "server"
SOURCE_FILE = "file"
SOURCE_HOSTED = "hosted"

STATE_OK = "ok"
STATE_NO_LOCAL_DATA = "no_local_data"
STATE_NOT_SIGNED_IN = "not_signed_in"
STATE_READ_FAILED = "read_failed"

NO_LOCAL_DATA_MESSAGE = (
    "No local Meridian data is available. The local server is not running, and no database "
    "file was found. Start Meridian on this computer, or choose a database file in Settings."
)
NOT_SIGNED_IN_MESSAGE = (
    "No hosted token is saved, so no hosted projects are loaded. "
    "Sign in with your browser in Settings."
)
_READ_FAILED_TEXT = {
    SOURCE_SERVER: "The local Meridian server could not be read",
    SOURCE_FILE: "The database file could not be read",
    SOURCE_HOSTED: "Hosted Meridian projects could not be loaded",
}

Row = dict[str, Any]
# Each callable does its own I/O. The resolver only decides which one is called.
ProbeFn = Callable[[str], str]  # local URL -> probe state (local_client.probe_health)
ReadServerFn = Callable[[str], list[Row]]  # local URL -> projects from the running server
ReadFileFn = Callable[[Path], list[Row]]  # database path -> projects (projects_reader.list_projects)
GetTokenFn = Callable[[], str | None]  # -> the stored hosted token, or None (token_store.get_token)
ListHostedFn = Callable[[str, str], list[Row]]  # (hosted URL, token) -> projects from the hosted client


@dataclass(frozen=True)
class DataSources:
    """The callables the resolver may call. Each is injected, so nothing here is wired to a host or a file."""

    probe: ProbeFn
    read_server: ReadServerFn
    read_file: ReadFileFn
    get_token: GetTokenFn
    list_hosted: ListHostedFn


@dataclass(frozen=True)
class Resolution:
    """The result for one mode. ``source`` is None when no source was used."""

    state: str
    source: str | None
    rows: list[Row] = field(default_factory=list)
    message: str = ""


def _is_file(text: str) -> bool:
    """True when ``text`` names an existing regular file. An unusable path reads as not a file."""
    try:
        return Path(text).is_file()
    except (OSError, ValueError):
        return False


def _read_failed_message(source: str, exc: BaseException) -> str:
    if isinstance(exc, HostedError):
        # The hosted client's messages are fixed text: no token, no response body, no raw exception.
        return f"{_READ_FAILED_TEXT[source]}. {exc}"
    return f"{_READ_FAILED_TEXT[source]} ({exc.__class__.__name__})."


def _read(source: str, load: Callable[[], list[Row]]) -> Resolution:
    """Run one injected read. Any failure becomes state read_failed, reported by class name only."""
    try:
        rows = list(load())
    except Exception as exc:  # noqa: BLE001 - a failed read is reported by class name only
        return Resolution(STATE_READ_FAILED, source, message=_read_failed_message(source, exc))
    return Resolution(STATE_OK, source, rows=rows)


def _resolve_local(settings: Settings, sources: DataSources) -> Resolution:
    if sources.probe(settings.local_url) == local_client.HEALTH_RUNNING:
        # A running server wins. The file is not read, so the result holds one source only.
        return _read(SOURCE_SERVER, lambda: sources.read_server(settings.local_url))
    db_path = settings.db_path
    if db_path is not None and _is_file(db_path):
        path = Path(db_path)
        return _read(SOURCE_FILE, lambda: sources.read_file(path))
    return Resolution(STATE_NO_LOCAL_DATA, None, message=NO_LOCAL_DATA_MESSAGE)


def _resolve_hosted(settings: Settings, sources: DataSources) -> Resolution:
    token = sources.get_token()
    if not isinstance(token, str) or not token.strip():
        # No token: return now. The probe, the file and the hosted client are never called.
        return Resolution(STATE_NOT_SIGNED_IN, None, message=NOT_SIGNED_IN_MESSAGE)
    return _read(SOURCE_HOSTED, lambda: sources.list_hosted(settings.hosted_url, token))


def resolve(settings: Settings, sources: DataSources) -> Resolution:
    """Return the one source the mode uses, with its rows, or the state that says why there is none.

    "hosted" reads through the hosted path. Raise ValueError when the mode is not one of VALID_MODES.
    """
    if settings.mode == "local":
        return _resolve_local(settings, sources)
    if settings.mode in HOSTED_PATH_MODES:
        return _resolve_hosted(settings, sources)
    raise ValueError(f"mode must be one of {VALID_MODES}, got {settings.mode!r}")


__all__ = [
    "NOT_SIGNED_IN_MESSAGE",
    "NO_LOCAL_DATA_MESSAGE",
    "SOURCE_FILE",
    "SOURCE_HOSTED",
    "SOURCE_SERVER",
    "STATE_NOT_SIGNED_IN",
    "STATE_NO_LOCAL_DATA",
    "STATE_OK",
    "STATE_READ_FAILED",
    "DataSources",
    "Resolution",
    "resolve",
]
