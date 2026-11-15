"""Install the Meridian Zotero bridge from the signed .xpi that Meridian Desktop carries.

Standard library, the desktop's pure settings module (for the licence record), and no import from the Meridian core
package. The core's installer (meridian/zotero_plugin_install.py and meridian/zotero_bridge_api.py) is the model for the
profile lookup, the user.js lines, the token file and the refusals. This module differs in four ways, on purpose:

* Zotero is first asked to close by the normal close request, ``taskkill /IM zotero.exe`` with no ``/F`` and no ``/T``.
  It is polled for up to 60 seconds. A Zotero that still runs then is left alone and nothing is written, unless the
  caller passes ``allow_force_close=True``. That runs one forced close, ``taskkill /F /IM zotero.exe`` with no ``/T``,
  polled for up to 10 seconds. The Zotero tab asks the owner before a forced close, unless the owner has opted in. When
  the owner has just said yes to that question, the caller also passes ``skip_graceful=True``, and the install goes
  straight to the forced close, with no second 60-second window.
* Meridian never starts Zotero. It reads extensions.json and reports the bridge's enabled state, and it never
  changes that state.
* The bundled file is the one the owner signed. It is copied only after its sha256 matches this build's record,
  and the copy is checked again before and after it is put in place.
* No message names a path. The token file's path and contents are never shown.

Every process listing, close request and icacls call goes through an injectable callable, and every path can be
injected too, so tests write only under temp folders and never start a process.
"""

from __future__ import annotations

import configparser
import getpass
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meridian_desktop.settings_store import licence_accepted_for

ADDON_ID = "meridian-zotero@usemeridian.us"
XPI_FILENAME = "meridian-zotero@usemeridian.us.xpi"
BUNDLE_SUBDIR = "zotero_bridge"
BUNDLE_RECORD = "bridge.json"
PROFILES_INI = "profiles.ini"
EXTENSIONS_JSON = "extensions.json"
USER_JS = "user.js"
PROFILE_MARKERS = ("prefs.js", "zotero.sqlite")

API_PORT = 23125
TOKEN_PREFIX = "sk_meridian_"
TOKEN_SUBDIR = "zotero_bridge"
TOKEN_FILENAME = "api_token"
STATE_DIR_ENV_VAR = "MERIDIAN_LOCAL_RUNNER_STATE_DIR"
PREF_PREFIX = "extensions.zotero.meridianZoteroBridge."
PORT_PREF = PREF_PREFIX + "apiPort"
TOKEN_PATH_PREF = PREF_PREFIX + "apiTokenPath"
SCAN_PREF = "extensions.startupScanScopes"
SCAN_PREF_VALUE = 1
SCAN_PREF_LINE = f'user_pref("{SCAN_PREF}", {SCAN_PREF_VALUE});'

# The close request. There is no /F (force) and no /T (tree): Zotero is asked to close, as its window's close button is.
TASKKILL_ARGV = ("taskkill", "/IM", "zotero.exe")
CLOSE_TIMEOUT_SECONDS = 60.0
CLOSE_POLL_SECONDS = 0.5
# The forced close. It runs only when the caller allows it, after the close request has not worked. /F ends the process at
# once, so unsaved Zotero work can be lost. There is no /T, so only zotero.exe itself is ended, not its child processes.
FORCE_TASKKILL_ARGV = ("taskkill", "/F", "/IM", "zotero.exe")
FORCE_CLOSE_TIMEOUT_SECONDS = 10.0
_CREATE_NO_WINDOW = 0x08000000

# The licence notice the person reads and accepts, copied word for word from meridian/zotero_plugin_install.py
# (LICENCE_NOTICE_TEXT). Do not reword it here. The text describes the core install, which also registers and enables
# the plugin; this build does not do that step (see the module docstring and the panel's result text).
LICENCE_NOTICE_TEXT = (
    "The Meridian Zotero bridge is released under the Meridian Source License 1.0 (MSL-1.0), the LICENSE file at the root of "
    "the Meridian repository. In plain terms: you may run it, and copy and change it for your own use, including inside your "
    "organization. You may not sublicense it, and you may not use it for a competing hosted coordination service during the "
    "protection period. Each version converts to MIT five years after its first public release. It is provided without warranty.\n\n"
    "It runs inside Zotero and listens only on this computer (127.0.0.1).\n\n"
    "Installing it does more than copy the plugin into your Zotero profile. It also sets the Zotero preference "
    "extensions.startupScanScopes to 1 in user.js; adds two settings there that point the plugin at Meridian's local "
    "port, 127.0.0.1:23125; writes a token file that only your Windows user account can read; and registers the "
    "plugin as enabled in Zotero's extensions.json. Removing the plugin does not undo these changes.\n\n"
    "Source: the bridge's own repository, meridian-zotero-bridge, which will be linked here once published. "
    "This is a summary, not legal advice; the LICENSE file at the root of the Meridian repository is the authority."
)
# The SHA-256 of LICENCE_NOTICE_TEXT as UTF-8. An acceptance is stored with this digest, and it counts only while it matches.
LICENCE_NOTICE_SHA256 = hashlib.sha256(LICENCE_NOTICE_TEXT.encode("utf-8")).hexdigest()

# Statuses an install or a check can return.
ST_INSTALLED = "installed"
ST_CURRENT = "current"
ST_LICENCE_NOT_ACCEPTED = "licence_not_accepted"
ST_NO_BUNDLE = "no_bundle"
ST_BUNDLE_DAMAGED = "bundle_damaged"
ST_BUNDLE_MISSING = "bundle_missing"
ST_INTEGRITY_FAILED = "integrity_failed"
ST_NO_PROFILE = "no_profile"
ST_NOT_PROFILE = "refused_not_profile"
ST_REGISTRY_UNREADABLE = "registry_unreadable"
ST_NEWER_INSTALLED = "newer_installed"
ST_PREF_CONFLICT = "pref_conflict"
ST_PREF_UNREADABLE = "pref_unreadable"
ST_CHECK_FAILED = "check_failed"
ST_REFUSED_RUNNING = "refused_running"
ST_CLOSE_FAILED = "close_failed"
ST_FAILED = "failed"

# Install states a check reports.
STATE_NO_BUNDLE = "no_bundle"
STATE_DAMAGED = "bundle_damaged"
STATE_NO_PROFILE = "no_profile"
STATE_UNREADABLE = "unreadable"
STATE_NOT_INSTALLED = "not_installed"
STATE_OLDER = "older"
STATE_CURRENT = "current"
STATE_NEWER = "newer"
STATE_UNKNOWN = "unknown"

# Registration states, read from the bridge's entry in extensions.json.
REG_NOT_LISTED = "not_listed"
REG_ENABLED = "enabled"
REG_DISABLED = "disabled"
REG_BLOCKED = "blocked"

# What the profile's user.js says about the scan setting.
SCAN_ADDED = "added"
SCAN_PRESENT = "present"
SCAN_CONFLICT = "conflict"
# What the API prefs write did.
PREFS_ADDED = "added"
PREFS_UPDATED = "updated"
PREFS_PRESENT = "present"
# What the token file did.
TOKEN_CREATED = "created"
TOKEN_KEPT = "kept"

LICENCE_NOT_ACCEPTED_MESSAGE = (
    "The Meridian Zotero bridge is released under the Meridian Source License 1.0 (MSL-1.0). Nothing was installed: read its "
    "licence notice on the Zotero tab of Meridian Desktop and accept it there first. Nothing was written."
)
NO_BUNDLE_MESSAGE = "No bundled bridge in this build. Nothing was written."
BUNDLE_DAMAGED_MESSAGE = (
    "The bridge record in this build is damaged, so nothing was installed. Rebuild Meridian Desktop with the signed "
    "bridge file. Nothing was written."
)
BUNDLE_MISSING_MESSAGE = "The bridge file in this build is missing, so nothing was installed. Nothing was written."
INTEGRITY_FAILED_MESSAGE = (
    "The bridge file failed its sha256 check against this build's record, so nothing was installed. Nothing was written."
)
NO_PROFILE_MESSAGE = "Zotero's default profile was not found. Open Zotero once, then try again. Nothing was written."
NOT_PROFILE_MESSAGE = (
    "Zotero's default profile has neither prefs.js nor zotero.sqlite, so nothing was installed. Nothing was written."
)
REGISTRY_UNREADABLE_MESSAGE = (
    "Zotero's extensions.json could not be read, so Meridian cannot tell which bridge version is installed. "
    "Nothing was written."
)
NEWER_INSTALLED_MESSAGE = (
    "Zotero has a newer bridge ({installed}) than this build carries ({bundled}), so nothing was changed. "
    "Nothing was written."
)
PREF_CONFLICT_MESSAGE = (
    "The profile's user.js sets extensions.startupScanScopes to something other than 1, so Meridian will not change it. "
    "Remove that line, then try again. Nothing was written."
)
PREF_UNREADABLE_MESSAGE = (
    "The profile's user.js exists but cannot be read, so Meridian will not change it. Nothing was written."
)
CHECK_FAILED_MESSAGE = "Could not check whether Zotero is running. Make sure Zotero is closed, then try again. Nothing was written."
REFUSED_RUNNING_MESSAGE = "Zotero is running. Close Zotero, then try again. Nothing was written."
CLOSE_FAILED_MESSAGE = (
    "Meridian asked Zotero to close, and it did not close within 60 seconds. Nothing was installed and nothing was "
    "written. Close Zotero yourself, then try again."
)
FORCE_FAILED_MESSAGE = (
    "Meridian asked Zotero to close, then forced it to close, and Zotero is still running. Nothing was installed and "
    "nothing was written. Close Zotero yourself, then try again."
)
CURRENT_MESSAGE = "The Zotero bridge {version} is already installed and up to date. Nothing was written."
INSTALLED_MESSAGE = "Installed the Zotero bridge {version}."
UPDATED_MESSAGE = "Updated the Zotero bridge from {installed} to {version}."
FAILED_MESSAGE = "The install stopped with an error ({name}). Some files may have been written. Run the install again."
RESTART_MESSAGE = "Meridian closed Zotero for this install. Start Zotero again to load the bridge."

REG_NOT_LISTED_MESSAGE = (
    "Zotero does not list the bridge yet. Start Zotero once, then turn the bridge on in Tools > Plugins. This build "
    "does not start Zotero or change the bridge's enabled state."
)
REG_ENABLED_MESSAGE = "The bridge is turned on in Zotero."
REG_DISABLED_MESSAGE = "The bridge is turned off in Zotero. This build does not change that: turn it on in Tools > Plugins."
REG_BLOCKED_MESSAGE = "Zotero blocks the bridge, so it cannot run. This build does not change that."
REG_UNREADABLE_MESSAGE = "Zotero's extensions.json could not be read after the install, so the bridge's state is not known."

_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_-]{32,}$")
_TOKEN_TRIM = " \t\r\n\f\v﻿"
# ASCII flag: user.js is read as latin-1, so a non-ASCII byte must never count as whitespace here.
# The name may be quoted with single or double quotes, and any spacing is allowed. Group 1 is the quote, group 2 the value.
_SCAN_PREF_RE = re.compile(r"""user_pref\(\s*(["'])extensions\.startupScanScopes\1\s*,\s*([^)]*?)\s*\)\s*;?""", re.ASCII)
# Every mention of the scan pref name in active text. A mention that _SCAN_PREF_RE does not parse counts as a conflict.
_SCAN_NAME_RE = re.compile(r"extensions\.startupScanScopes", re.ASCII)
_MANAGED_LINE = re.compile(
    r"""^\s*user_pref\(\s*(["'])(?:""" + re.escape(PORT_PREF) + "|" + re.escape(TOKEN_PATH_PREF) + r""")\1\s*,.*\)\s*;?\s*$""",
    re.ASCII,
)
_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class BundleError(Exception):
    """The bundle record is present but cannot be used. The message is the owner's text."""


@dataclass(frozen=True)
class BundledBridge:
    """The signed bridge file this build carries, as its record describes it."""

    xpi: Path
    version: str
    sha256: str


@dataclass(frozen=True)
class RegistryRead:
    """extensions.json as read: ``missing`` (no file), ``unreadable``, or ``ok`` with this add-on's entry (None if absent)."""

    status: str
    entry: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class BridgeCheck:
    """What is installed, read without writing anything."""

    state: str
    bundled_version: str | None
    installed_version: str | None
    registration: str
    profile_found: bool


@dataclass(frozen=True)
class InstallResult:
    """The outcome of one install. ``message`` is plain text for the person and names no path.

    ``can_force_close`` is True only for a close_failed result that a forced close could still resolve: the normal close
    was tried, a forced close was not, and nothing was written. The Zotero tab then asks the owner.
    """

    status: str
    message: str
    closed_zotero: bool = False
    can_force_close: bool = False


# ---------------------------------------------------------------------------
# The bundle this build carries
# ---------------------------------------------------------------------------


def bundled_bridge_folder(*, frozen: bool | None = None, executable: str | None = None) -> Path | None:
    """The folder next to MeridianDesktop.exe that holds the bridge, or None when this is not a frozen build."""
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if not is_frozen:
        return None
    exe = sys.executable if executable is None else executable
    return Path(exe).resolve().parent / BUNDLE_SUBDIR


def load_bundled_bridge(folder: Path | None) -> BundledBridge | None:
    """The bundled bridge, or None when this build carries none. Raises BundleError when a record is there but unusable."""
    if folder is None:
        return None
    record = Path(folder) / BUNDLE_RECORD
    if not record.is_file():
        return None
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError(BUNDLE_DAMAGED_MESSAGE) from exc
    if not isinstance(data, dict):
        raise BundleError(BUNDLE_DAMAGED_MESSAGE)
    version = data.get("version")
    digest = data.get("sha256")
    if (
        data.get("addon_id") != ADDON_ID
        or data.get("file") != XPI_FILENAME
        or not isinstance(version, str)
        or _VERSION.match(version) is None
        or not isinstance(digest, str)
        or _SHA256.match(digest) is None
    ):
        raise BundleError(BUNDLE_DAMAGED_MESSAGE)
    return BundledBridge(xpi=Path(folder) / XPI_FILENAME, version=version, sha256=digest)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


def version_tuple(text: object) -> tuple[int, ...] | None:
    """Plain dotted integers as a tuple, or None for anything else."""
    if not isinstance(text, str) or _VERSION.match(text) is None:
        return None
    return tuple(int(part) for part in text.split("."))


def compare_versions(installed: object, bundled: str) -> int | None:
    """-1, 0 or 1 for installed against bundled (missing parts count as zero), or None when either is not plain numbers."""
    left = version_tuple(installed)
    right = version_tuple(bundled)
    if left is None or right is None:
        return None
    width = max(len(left), len(right))
    a = left + (0,) * (width - len(left))
    b = right + (0,) * (width - len(right))
    return (a > b) - (a < b)


# ---------------------------------------------------------------------------
# Zotero's profile
# ---------------------------------------------------------------------------


def zotero_data_dir(env: Mapping[str, str] | None = None) -> Path | None:
    """Zotero's data root, the folder that holds profiles.ini: %APPDATA%\\Zotero\\Zotero. None when APPDATA is unset."""
    source = os.environ if env is None else env
    appdata = str(source.get("APPDATA") or "").strip()
    if not appdata:
        return None
    return Path(appdata) / "Zotero" / "Zotero"


def _resolve_ini_path(root: Path, value: str, *, relative: bool) -> Path | None:
    text = (value or "").strip()
    if not text:
        return None
    candidate = Path(text)
    if candidate.is_absolute():
        return candidate
    if relative:
        return root / candidate
    return None  # IsRelative=0 promises an absolute path; do not guess one


def _existing_dir(path: Path | None) -> Path | None:
    return path if path is not None and path.is_dir() else None


def find_default_profile(data_dir: Path | None) -> Path | None:
    """The default profile folder named by profiles.ini, or None rather than a guess.

    The ``[InstallXXXX] Default=`` key is tried first, relative to the data root unless absolute. Then the first
    ``[Profile*]`` section with ``Default=1``, honouring ``IsRelative``. A candidate counts only when its folder exists.
    The same rules as the core's find_default_profile_dir.
    """
    if data_dir is None:
        return None
    root = Path(data_dir)
    try:
        text = (root / PROFILES_INI).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    parser = configparser.RawConfigParser(strict=False)
    try:
        parser.read_string(text)
    except configparser.Error:
        return None
    sections = parser.sections()
    for section in sections:
        if section.startswith("Install") and parser.has_option(section, "Default"):
            candidate = _existing_dir(_resolve_ini_path(root, parser.get(section, "Default"), relative=True))
            if candidate is not None:
                return candidate
    for section in sections:
        if not section.startswith("Profile"):
            continue
        if parser.get(section, "Default", fallback="").strip() != "1":
            continue
        relative = parser.get(section, "IsRelative", fallback="1").strip() != "0"
        candidate = _existing_dir(_resolve_ini_path(root, parser.get(section, "Path", fallback=""), relative=relative))
        if candidate is not None:
            return candidate
    return None


def is_profile(profile: Path) -> bool:
    """True when the folder holds one of Zotero's profile marker files."""
    return any((profile / name).is_file() for name in PROFILE_MARKERS)


def read_registry(profile: Path) -> RegistryRead:
    """extensions.json, read only. Its entry for this add-on is returned as it is, and never written here."""
    path = profile / EXTENSIONS_JSON
    if not path.is_file():
        return RegistryRead("missing")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return RegistryRead("unreadable")
    addons = data.get("addons") if isinstance(data, dict) else None
    if not isinstance(addons, list):
        return RegistryRead("unreadable")
    entry = next((item for item in addons if isinstance(item, dict) and item.get("id") == ADDON_ID), None)
    return RegistryRead("ok", entry)


def registration_of(entry: Mapping[str, Any] | None) -> str:
    """Whether Zotero lists the bridge, and if so whether it is on, off, or blocked."""
    if entry is None:
        return REG_NOT_LISTED
    if entry.get("appDisabled"):
        return REG_BLOCKED
    if entry.get("userDisabled"):
        return REG_DISABLED
    return REG_ENABLED


def _state_for(reg: RegistryRead, bundled_version: str) -> tuple[str, str | None]:
    """(install state, installed version) for one registry read against the bundled version. Pure."""
    if reg.entry is None:
        return STATE_NOT_INSTALLED, None
    raw = reg.entry.get("version")
    installed = raw if isinstance(raw, str) else None
    order = compare_versions(installed, bundled_version)
    if order is None:
        return STATE_UNKNOWN, installed
    if order < 0:
        return STATE_OLDER, installed
    if order == 0:
        return STATE_CURRENT, installed
    return STATE_NEWER, installed


def zotero_process_is_running(process_list: Callable[[], str]) -> bool | None:
    """True when the process listing names zotero.exe, False when it does not, None when the listing failed. Never raises."""
    try:
        text = str(process_list() or "")
    except Exception:  # noqa: BLE001 -- a failed listing is an unknown state, never "not running"
        return None
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0].casefold() == "zotero.exe":
            return True
    return False


def tasklist_text() -> str:
    """The default process listing (tasklist). Raises when tasklist fails, which the caller reads as unknown."""
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    completed = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq zotero.exe", "/NH"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=10,
        check=False,
        **kwargs,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"tasklist exited with code {completed.returncode}")
    return completed.stdout or ""


def run_command(argv: list[str]) -> int:
    """The default runner: runs one command and returns its exit code. Only the close request and icacls use it."""
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30,
        check=False,
        **kwargs,
    )
    return completed.returncode


# ---------------------------------------------------------------------------
# user.js: the scan line and the two API prefs, the same lines the core writes
# ---------------------------------------------------------------------------


def read_user_js(profile: Path) -> str:
    """user.js as latin-1 text, one character per byte. Empty when there is no user.js. Raises OSError when it cannot be read."""
    path = profile / USER_JS
    if not path.exists():
        return ""
    return path.read_bytes().decode("latin-1")


def user_js_lines(text: str) -> tuple[list[str], str]:
    """The lines of user.js without their LF, and the file's line ending. Splits on LF only, as the core does."""
    newline = "\r\n" if "\r\n" in text else "\n"
    if text and not text.endswith("\n"):
        text += newline
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines, newline


def _active_text(text: str) -> str:
    """The text without its comment lines. A line whose first non-space characters are ``//`` is not a setting."""
    return "\n".join(line for line in text.split("\n") if not line.lstrip(" \t").startswith("//"))


def scan_state(text: str) -> str:
    """``added`` (no active scan line), ``present`` (the one Meridian needs), or ``conflict``. Pure.

    An active line that names the scan pref is a conflict when its value is not 1, whatever the quotes or spacing. A
    mention of the name that is not in a form this module parses is also a conflict, so the install refuses rather than
    guess what Zotero will read.
    """
    active = _active_text(text)
    values = [match.group(2).strip() for match in _SCAN_PREF_RE.finditer(active)]
    if len(_SCAN_NAME_RE.findall(active)) != len(values):
        return SCAN_CONFLICT
    if any(value != str(SCAN_PREF_VALUE) for value in values):
        return SCAN_CONFLICT
    return SCAN_PRESENT if values else SCAN_ADDED


def api_pref_lines(token_path: Path, port: int = API_PORT) -> list[str]:
    """The two managed user_pref lines, written exactly as the core's api_pref_lines writes them."""
    return [
        f'user_pref("{PORT_PREF}", {int(port)});',
        f'user_pref("{TOKEN_PATH_PREF}", {json.dumps(str(token_path))});',
    ]


# ---------------------------------------------------------------------------
# Writes: every write goes through a temp file in the same folder, then os.replace
# ---------------------------------------------------------------------------


def _atomic_write(
    target: Path,
    payload: bytes,
    expected_sha256: str | None = None,
    prepare: Callable[[Path], None] | None = None,
) -> None:
    """Write ``payload`` to ``target``. When ``expected_sha256`` is given, the temp file must hash to it before the replace.

    ``prepare`` runs on the empty temp file before any byte of ``payload`` is written, so a token can be restricted to
    its owner before it ever holds content. Whatever fails (including a KeyboardInterrupt or a timed-out command) removes
    the temp file, and ``target`` is replaced only when every step has succeeded.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    try:
        os.close(fd)
        if prepare is not None:
            prepare(Path(temporary))
        with open(temporary, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if expected_sha256 is not None and sha256_file(Path(temporary)) != expected_sha256:
            raise OSError("the copy does not match its sha256")
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _write_xpi(target: Path, payload: bytes, expected_sha256: str) -> str:
    """Put the verified bytes in place. Returns ``unchanged`` or ``installed``. The copy is checked before and after."""
    if target.is_file() and sha256_file(target) == expected_sha256:
        return "unchanged"
    # Before: the temp copy must hash to the expected value. The replace is atomic.
    _atomic_write(target, payload, expected_sha256=expected_sha256)
    # After: the file in place must still hash to the expected value. If not, nothing bad is left behind.
    if sha256_file(target) != expected_sha256:
        target.unlink(missing_ok=True)
        raise OSError("the installed copy does not match its sha256")
    return "installed"


def ensure_scan_pref(profile: Path) -> str:
    """Add the scan line to user.js when no active one exists, keeping every other byte. Returns the state found before."""
    text = read_user_js(profile)
    state = scan_state(text)
    if state == SCAN_ADDED:
        lines, newline = user_js_lines(text)
        body = "".join(line + "\n" for line in lines) + SCAN_PREF_LINE + newline
        _atomic_write(profile / USER_JS, body.encode("latin-1"))
    return state


def ensure_api_prefs(profile: Path, token_path: Path, port: int = API_PORT) -> str:
    """Set the two API prefs in user.js and leave every other line's bytes as they are. Returns added, updated or present."""
    lines, newline = user_js_lines(read_user_js(profile))
    desired = api_pref_lines(token_path, port)
    existing = [line.strip() for line in lines if _MANAGED_LINE.match(line)]
    if sorted(existing) == sorted(desired):
        return PREFS_PRESENT
    kept = [line for line in lines if not _MANAGED_LINE.match(line)]
    body = "".join(line + "\n" for line in kept) + "".join(line + newline for line in desired)
    _atomic_write(profile / USER_JS, body.encode("latin-1"))
    return PREFS_UPDATED if existing else PREFS_ADDED


def token_file_path(env: Mapping[str, str] | None = None) -> Path:
    """The bridge's token file: zotero_bridge/api_token under Meridian's local-runner state folder.

    The same location as meridian_desktop.zotero_bridge.token_file_path, which a test checks.
    """
    source = os.environ if env is None else env
    override = str(source.get(STATE_DIR_ENV_VAR) or "").strip()
    base = Path(override) if override else Path.home() / ".meridian" / "local_runner"
    return base / TOKEN_SUBDIR / TOKEN_FILENAME


def token_is_usable(path: Path) -> bool:
    """True when the token file exists and holds a usable token. Reads only. Never raises. The value is never returned."""
    try:
        return path.is_file() and _TOKEN_SHAPE.match(path.read_text(encoding="utf-8-sig").strip(_TOKEN_TRIM)) is not None
    except (OSError, ValueError):
        return False


def owner_principal(env: Mapping[str, str] | None = None) -> str:
    """The account the token is granted to: ``DOMAIN\\user`` from USERDOMAIN and USERNAME when both are set.

    The bare user name is used only when those variables are not both set (the user name from USERNAME, or from
    getpass when USERNAME is unset). UNVERIFIED on Microsoft and domain accounts: this form has not been checked against
    a real Microsoft or domain account in this build. Only the argv is tested, with a fake environment.
    """
    source = os.environ if env is None else env
    user = str(source.get("USERNAME") or "").strip()
    domain = str(source.get("USERDOMAIN") or "").strip()
    if user and domain:
        return f"{domain}\\{user}"
    return user or getpass.getuser()


def restrict_to_owner(
    target: Path,
    *,
    runner: Callable[[list[str]], int],
    platform: str,
    env: Mapping[str, str] | None = None,
) -> None:
    """On Windows, leave the file readable by its owner only, with inheritance removed. Raises OSError on failure.

    Any exception from the runner, a timed-out icacls included, is reported as OSError, so every failure to restrict
    reaches the caller the same way.
    """
    if platform != "win32":
        return
    try:
        code = runner(["icacls", str(target), "/inheritance:r", "/grant:r", f"{owner_principal(env)}:F"])
    except Exception as exc:  # noqa: BLE001 -- every failure to restrict is one refusal; the caller removes the token
        raise OSError("the token file could not be restricted to its owner") from exc
    if code != 0:
        raise OSError("the token file could not be restricted to its owner")


def ensure_token(
    path: Path,
    *,
    runner: Callable[[list[str]], int],
    platform: str,
    env: Mapping[str, str] | None = None,
) -> str:
    """Create the token file unless it already holds a usable token, which is kept. Returns created or kept.

    A new token is restricted to its owner before its first byte is written, so it is never visible with the inherited
    ACL. If the restriction fails, the temp file is removed and no token file is left at ``path``. The error is raised.
    """
    if token_is_usable(path):
        restrict_to_owner(path, runner=runner, platform=platform, env=env)
        return TOKEN_KEPT
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    _atomic_write(
        path,
        (token + "\n").encode("ascii"),
        prepare=lambda temporary: restrict_to_owner(temporary, runner=runner, platform=platform, env=env),
    )
    return TOKEN_CREATED


# ---------------------------------------------------------------------------
# The installer
# ---------------------------------------------------------------------------


def _result(status: str, message: str, closed: bool = False, can_force_close: bool = False) -> InstallResult:
    return InstallResult(status=status, message=message, closed_zotero=closed, can_force_close=can_force_close)


def _install_is_complete(profile: Path, bundle: BundledBridge, token: Path) -> bool:
    """True when the bundled file is in extensions/ with the bundle's sha256, user.js holds the scan line and both API
    prefs exactly as they are written, and the token file holds a usable token. A missing or empty token is not complete,
    so the write steps run and make one. Reads only: it writes nothing, closes nothing and never returns a token value.
    """
    if not token_is_usable(token):
        return False
    xpi = profile / "extensions" / XPI_FILENAME
    try:
        if not xpi.is_file() or sha256_file(xpi) != bundle.sha256:
            return False
        user_js = read_user_js(profile)
    except OSError:
        return False
    if scan_state(user_js) != SCAN_PRESENT:
        return False
    lines, _newline = user_js_lines(user_js)
    managed = sorted(line.strip() for line in lines if _MANAGED_LINE.match(line))
    return managed == sorted(api_pref_lines(token))


def _registration_message(profile: Path) -> str:
    """What Zotero says about the bridge now, read only. It names no path."""
    reg = read_registry(profile)
    if reg.status == "unreadable":
        return REG_UNREADABLE_MESSAGE
    kind = registration_of(reg.entry if reg.status == "ok" else None)
    return {
        REG_NOT_LISTED: REG_NOT_LISTED_MESSAGE,
        REG_ENABLED: REG_ENABLED_MESSAGE,
        REG_DISABLED: REG_DISABLED_MESSAGE,
        REG_BLOCKED: REG_BLOCKED_MESSAGE,
    }[kind]


class ZoteroBridgeInstaller:
    """Checks and installs the bundled bridge. Every outside effect is injectable: the bundle, the profile root, the token
    path, the process listing, the command runner, the sleep between close polls, the platform, and the settings store that
    records the licence acceptance.

    The default constructor takes an already-loaded bundle. ``for_this_build`` loads the one this build carries.
    """

    def __init__(
        self,
        bundle: BundledBridge | None = None,
        *,
        bundle_problem: str | None = None,
        data_dir: Path | None = None,
        env: Mapping[str, str] | None = None,
        token_path: Path | None = None,
        process_list: Callable[[], str] | None = None,
        runner: Callable[[list[str]], int] | None = None,
        sleep: Callable[[float], Any] = time.sleep,
        platform: str | None = None,
        close_timeout_s: float = CLOSE_TIMEOUT_SECONDS,
        settings: Any = None,
    ) -> None:
        self._bundle = bundle
        self._bundle_problem = bundle_problem
        self._data_dir = data_dir
        self._env = env
        self._token_path = token_path
        self._process_list = process_list if process_list is not None else tasklist_text
        self._runner = runner if runner is not None else run_command
        self._sleep = sleep
        self._platform = sys.platform if platform is None else platform
        self._close_timeout_s = close_timeout_s
        # The settings store (a SettingsStore, or anything with load()). None means no acceptance can be read, so no install runs.
        self._settings = settings

    @classmethod
    def for_this_build(cls, **deps: Any) -> ZoteroBridgeInstaller:
        """An installer for the bridge this build carries. A damaged record is kept as a problem, never raised."""
        try:
            bundle = load_bundled_bridge(bundled_bridge_folder())
            problem = None
        except BundleError as exc:
            bundle, problem = None, str(exc)
        return cls(bundle, bundle_problem=problem, **deps)

    @property
    def bundle(self) -> BundledBridge | None:
        return self._bundle

    def _zotero_root(self) -> Path | None:
        return self._data_dir if self._data_dir is not None else zotero_data_dir(self._env)

    def _token(self) -> Path:
        return self._token_path if self._token_path is not None else token_file_path(self._env)

    def check(self) -> BridgeCheck:
        """What is installed now. Reads only: no write, no close, no process start."""
        if self._bundle_problem is not None:
            return BridgeCheck(STATE_DAMAGED, None, None, REG_NOT_LISTED, False)
        bundle = self._bundle
        if bundle is None:
            return BridgeCheck(STATE_NO_BUNDLE, None, None, REG_NOT_LISTED, False)
        profile = find_default_profile(self._zotero_root())
        if profile is None or not is_profile(profile):
            return BridgeCheck(STATE_NO_PROFILE, bundle.version, None, REG_NOT_LISTED, False)
        reg = read_registry(profile)
        if reg.status == "unreadable":
            return BridgeCheck(STATE_UNREADABLE, bundle.version, None, REG_NOT_LISTED, True)
        state, installed = _state_for(reg, bundle.version)
        return BridgeCheck(state, bundle.version, installed, registration_of(reg.entry), True)

    def check_for_update(self) -> BridgeCheck:
        """The read-only update check: the installed bridge against the bundled one. Reads only. It installs nothing and makes
        no network call. The Zotero tab runs it at start and when the tab first opens, and only when the owner has turned
        the check on.
        """
        return self.check()

    def install(
        self,
        *,
        allow_close: bool = True,
        allow_force_close: bool = False,
        skip_graceful: bool = False,
    ) -> InstallResult:
        """Install the bundled bridge. Never raises. Every refusal happens before anything is closed or written.

        The licence gate is here, and a caller cannot change it. The install refuses unless the settings store records an
        acceptance of the notice whose SHA-256 is LICENCE_NOTICE_SHA256, the digest of this module's own notice text. There
        is no digest argument. ``allow_close=False`` refuses a running Zotero instead of asking it to close.
        ``allow_force_close=True`` lets the install force Zotero closed once the normal close window has passed.
        ``skip_graceful=True``, with ``allow_force_close=True``, goes straight to the forced close and skips the normal close
        window. Only the owner's yes to the force-close question sets it. Without ``allow_force_close`` it has no effect.
        """
        try:
            return self._install(
                allow_close=allow_close,
                allow_force_close=allow_force_close,
                skip_graceful=skip_graceful,
            )
        except Exception as exc:  # noqa: BLE001 -- every failure is reported as data, never raised or shown with a path
            return _result(ST_FAILED, FAILED_MESSAGE.format(name=type(exc).__name__))

    def _licence_recorded(self) -> bool:
        """True only when the settings store holds an acceptance of this module's notice. Reads only. Never raises."""
        if self._settings is None:
            return False
        try:
            return licence_accepted_for(self._settings.load().settings, LICENCE_NOTICE_SHA256)
        except Exception:  # noqa: BLE001 -- an unreadable settings file means the licence is not accepted
            return False

    def _install(self, *, allow_close: bool, allow_force_close: bool, skip_graceful: bool) -> InstallResult:
        if not self._licence_recorded():
            return _result(ST_LICENCE_NOT_ACCEPTED, LICENCE_NOT_ACCEPTED_MESSAGE)
        if self._bundle_problem is not None:
            return _result(ST_BUNDLE_DAMAGED, BUNDLE_DAMAGED_MESSAGE)
        bundle = self._bundle
        if bundle is None:
            return _result(ST_NO_BUNDLE, NO_BUNDLE_MESSAGE)
        try:
            payload = bundle.xpi.read_bytes()
        except OSError:
            return _result(ST_BUNDLE_MISSING, BUNDLE_MISSING_MESSAGE)
        if sha256_bytes(payload) != bundle.sha256:
            return _result(ST_INTEGRITY_FAILED, INTEGRITY_FAILED_MESSAGE)

        profile = find_default_profile(self._zotero_root())
        if profile is None:
            return _result(ST_NO_PROFILE, NO_PROFILE_MESSAGE)
        if not is_profile(profile):
            return _result(ST_NOT_PROFILE, NOT_PROFILE_MESSAGE)

        reg = read_registry(profile)
        if reg.status == "unreadable":
            return _result(ST_REGISTRY_UNREADABLE, REGISTRY_UNREADABLE_MESSAGE)
        state, installed = _state_for(reg, bundle.version)
        token = self._token()
        if state == STATE_CURRENT:
            if _install_is_complete(profile, bundle, token):
                return _result(ST_CURRENT, CURRENT_MESSAGE.format(version=bundle.version) + " " + _registration_message(profile))
            # The same version is recorded, but the file or the prefs are missing or changed. Repair them below, as an install.
            installed = None
        if state == STATE_NEWER:
            message = NEWER_INSTALLED_MESSAGE.format(installed=installed, bundled=bundle.version)
            return _result(ST_NEWER_INSTALLED, message)

        # Everything below is checked before Zotero is closed. Only the scan line and the prefs are kept
        # in user.js, so a user.js we cannot read or that sets the scan pref to another value stops here.
        try:
            user_js = read_user_js(profile)
        except OSError:
            return _result(ST_PREF_UNREADABLE, PREF_UNREADABLE_MESSAGE)
        if scan_state(user_js) == SCAN_CONFLICT:
            return _result(ST_PREF_CONFLICT, PREF_CONFLICT_MESSAGE)

        running = zotero_process_is_running(self._process_list)
        if running is None:
            return _result(ST_CHECK_FAILED, CHECK_FAILED_MESSAGE)
        closed_by_us = False
        if running:
            if not allow_close:
                return _result(ST_REFUSED_RUNNING, REFUSED_RUNNING_MESSAGE)
            outcome = self._close_zotero(allow_force_close=allow_force_close, skip_graceful=skip_graceful)
            if outcome == "check_failed":
                return _result(ST_CHECK_FAILED, CHECK_FAILED_MESSAGE)
            if outcome == "close_failed":
                # A forced close was tried when it was allowed, so the owner is asked only when it was not.
                if allow_force_close:
                    return _result(ST_CLOSE_FAILED, FORCE_FAILED_MESSAGE)
                return _result(ST_CLOSE_FAILED, CLOSE_FAILED_MESSAGE, can_force_close=True)
            # Only a close this install made counts as ours. "not_running" means Zotero exited on its own between the two
            # listings, so Meridian did not close it and does not ask the owner to start it again.
            closed_by_us = outcome == "closed"

        # The writes. Each one follows the one before it. A failure part way leaves what was written, and the message says so.
        # Once Meridian has closed Zotero, a failure must also say that Zotero has to be started again.
        try:
            _write_xpi(profile / "extensions" / XPI_FILENAME, payload, bundle.sha256)
            ensure_scan_pref(profile)
            ensure_token(token, runner=self._runner, platform=self._platform, env=self._env)
            ensure_api_prefs(profile, token)
        except Exception as exc:  # noqa: BLE001 -- reported as data; the restart text is added when Zotero was closed
            if not closed_by_us:
                raise
            return _result(ST_FAILED, FAILED_MESSAGE.format(name=type(exc).__name__) + " " + RESTART_MESSAGE, closed=True)

        if installed is None:
            headline = INSTALLED_MESSAGE.format(version=bundle.version)
        else:
            headline = UPDATED_MESSAGE.format(installed=installed, version=bundle.version)
        parts = [headline, _registration_message(profile)]
        if closed_by_us:
            parts.append(RESTART_MESSAGE)
        return _result(ST_INSTALLED, " ".join(parts), closed=closed_by_us)

    def _close_zotero(self, *, allow_force_close: bool = False, skip_graceful: bool = False) -> str:
        """Ask Zotero to close, then poll for the graceful window. A forced close runs only when allowed, after that window.

        Returns closed, not_running, close_failed or check_failed. Nothing is written here. Without ``allow_force_close``
        Zotero is never force-closed, and a Zotero that still runs is close_failed. With both ``allow_force_close`` and
        ``skip_graceful``, the owner has already said yes to the forced close, so the normal close request and its window are
        skipped, and the forced close runs at once.
        """
        start = zotero_process_is_running(self._process_list)
        if start is None:
            return "check_failed"
        if start is False:
            return "not_running"
        if not (allow_force_close and skip_graceful):
            self._send(TASKKILL_ARGV)
            graceful = self._poll_closed(self._close_timeout_s)
            if graceful == "closed":
                return "closed"
            if graceful == "check_failed":
                return "check_failed"
            if not allow_force_close:
                return "close_failed"
        self._send(FORCE_TASKKILL_ARGV)
        forced = self._poll_closed(FORCE_CLOSE_TIMEOUT_SECONDS)
        if forced == "closed":
            return "closed"
        if forced == "check_failed":
            return "check_failed"
        return "close_failed"

    def _send(self, argv: tuple[str, ...]) -> None:
        """Send one close request. A request that cannot be started is not an error here: the poll decides whether Zotero
        closed."""
        try:
            self._runner(list(argv))
        except Exception:  # noqa: BLE001 -- the poll decides whether Zotero closed
            pass

    def _poll_closed(self, timeout_s: float) -> str:
        """Poll the process list every CLOSE_POLL_SECONDS for up to ``timeout_s``. Returns closed, open or check_failed."""
        for _ in range(int(round(timeout_s / CLOSE_POLL_SECONDS))):
            self._sleep(CLOSE_POLL_SECONDS)
            state = zotero_process_is_running(self._process_list)
            if state is False:
                return "closed"
            if state is None:
                return "check_failed"
        return "open"


__all__ = [
    "ADDON_ID",
    "BridgeCheck",
    "BundleError",
    "BundledBridge",
    "InstallResult",
    "LICENCE_NOTICE_TEXT",
    "LICENCE_NOT_ACCEPTED_MESSAGE",
    "STATE_CURRENT",
    "STATE_DAMAGED",
    "STATE_NEWER",
    "STATE_NOT_INSTALLED",
    "STATE_NO_BUNDLE",
    "STATE_NO_PROFILE",
    "STATE_OLDER",
    "STATE_UNKNOWN",
    "STATE_UNREADABLE",
    "TASKKILL_ARGV",
    "XPI_FILENAME",
    "FORCE_TASKKILL_ARGV",
    "LICENCE_NOTICE_SHA256",
    "ZoteroBridgeInstaller",
    "bundled_bridge_folder",
    "compare_versions",
    "find_default_profile",
    "load_bundled_bridge",
    "zotero_data_dir",
]
