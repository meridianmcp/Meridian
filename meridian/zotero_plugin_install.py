"""One-time install of the Meridian Zotero bridge plugin into Zotero's profile.

* ``build_xpi`` packs ``extensions/zotero-bridge`` into a deterministic .xpi:
  sorted entries, fixed zip timestamps, fixed file mode, and text files with LF
  line endings, so rebuilds are byte-identical on every host, CRLF checkout or
  LF. In a frozen (PyInstaller) build the source is read from ``sys._MEIPASS``.
* ``install_into_profile`` copies that .xpi into the profile's ``extensions/``
  folder. It refuses a folder that is not a Zotero profile (no ``prefs.js`` or
  ``zotero.sqlite``), refuses while Zotero is running, and reports
  ``check_failed`` when the process check itself fails. It writes nothing to the
  profile in any of these cases. It skips the write when the bytes already match. It never touches
  ``prefs.js`` or any other profile file. The profile repairs below are the other writers: they add
  the scan line and the API token and prefs. They run after the .xpi, and also when the .xpi is
  already current, so a removed line comes back. They never rewrite the .xpi. ``user.js`` is read as
  bytes, and Zotero reads it only at its start, so the repairs need no close. The registration step
  is the only writer of ``extensions.json``, and it runs with Zotero closed.
  ``install_default_profile`` runs that install for the default profile named in
  ``profiles.ini``, with no close step. It never raises.
* ``signed_install_state`` says what is stored for this bundle's version and source: the Mozilla-signed .xpi
  that ``zotero_plugin_sign`` stored (``signed``), nothing (``missing``, which returns ``needs_signature``), a
  version already submitted to Mozilla with no verified signed file (``used``, which returns ``version_used``),
  a stored file whose bytes no longer match its record (``tampered``, which returns ``integrity_failed``), or a
  record file that cannot be read (``unreadable``, which returns ``record_unreadable``). Every refusal happens
  before anything closes. The copy reads the signed file once, checks its sha256 against the record, and writes
  those same bytes. Only that signed file is ever copied. Installs never sign.
* ``close_zotero_gracefully`` asks a running Zotero to close with
  ``taskkill /IM zotero.exe`` (never ``/F`` or ``/T``) and waits for it to stop.
  The taskkill argument list has no force flag, and a close that fails leaves
  Zotero as it was.
* ``install_default_with_close`` is the tray's install. It finds the default
  profile, checks that it holds a profile marker, and builds the bundle first.
  Only then does it compare the bundle with the installed plugin, and call
  ``install_with_close``. A missing profile, a folder that is not a profile, or a
  bundle that will not build is refused before anything closes. A plugin that is
  already identical is ``unchanged``, and Zotero is not closed, even while it runs. Its profile
  repairs still run, so a scan line, token or API pref that was removed is written back.
* ``install_with_close`` is the install the tray and the CLI use. It closes
  Zotero first when the saved ``close_zotero_for_install`` choice allows it.
  ``MERIDIAN_ZOTERO_CLOSE_ON_INSTALL`` can only turn that off, never on. It then
  installs and restarts Zotero, best effort, only when Meridian closed it. If the
  process check fails, or Zotero does not close, nothing is installed.
* The settings file ``zotero_plugin.json`` (in the per-user Meridian state dir,
  next to the tray's other settings) holds the user's choice. Auto-install is
  opt-in: it is off until the first-run prompt records an answer. An unreadable
  or wrong-shape file reads as undecided with auto-install off. A file written
  before the close choice existed keeps its decisions and closes Zotero by default.

UNVERIFIED on a real machine:

* the default Zotero data-root paths in ``zotero_data_dir`` (Windows
  ``%APPDATA%\\Zotero\\Zotero``, macOS ``~/Library/Application Support/Zotero``,
  Linux ``~/.zotero/zotero``). ``MERIDIAN_ZOTERO_DATA_DIR`` overrides them.
* Zotero 10.0.6 was tested in throwaway profiles only, not on the user's profile: a new file in
  ``extensions/`` is registered only when ``extensions.startupScanScopes`` lets Zotero scan the folder, and
  a newly registered sideload comes up disabled. The registration step below relies on both facts.
* whether ``taskkill /IM zotero.exe`` (a close request, not a force kill) makes
  Zotero exit within the 20-second wait. A Zotero that is busy or shows a dialog
  will not close, and the install then reports ``close_failed``.

CLI: ``python -m meridian zotero install-plugin [--profile DIR] [--dry-run] [--keep-zotero-open]
[--local-writes on|off]``.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Mapping

from meridian import zotero_local_writes

XPI_FILENAME = "meridian-zotero@usemeridian.us.xpi"
SETTINGS_FILENAME = "zotero_plugin.json"
ZOTERO_DATA_DIR_ENV = "MERIDIAN_ZOTERO_DATA_DIR"
AUTOINSTALL_ENV = "MERIDIAN_ZOTERO_PLUGIN_AUTOINSTALL"
CLOSE_ENV = "MERIDIAN_ZOTERO_CLOSE_ON_INSTALL"
CLOSE_SETTING_KEY = "close_zotero_for_install"

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})

_SOURCE_DIR = Path(__file__).resolve().parent.parent / "extensions" / "zotero-bridge"
_RUNTIME_FILES = ("manifest.json", "bootstrap.js", "prefs.js")
_RUNTIME_DIRS = ("api", "content", "lib")
_EXCLUDED_PARTS = frozenset({"test", "node_modules", "__pycache__"})
_EXCLUDED_NAMES = frozenset({"package.json", "package-lock.json", "README.md"})
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_ZIP_FILE_MODE = 0o644
_ZIP_CREATE_SYSTEM_UNIX = 3

_DEFAULT_SETTINGS = {"autoinstall": False, "decided": False, CLOSE_SETTING_KEY: True}
_DEFAULT_STATE_SUBDIR = "zotero_plugin"
PROFILE_MARKERS = ("prefs.js", "zotero.sqlite")
_NOT_PROFILE_MESSAGE = (
    "{profile} is not a Zotero profile: it has neither prefs.js nor zotero.sqlite. Nothing was written."
)
_CHECK_FAILED_MESSAGE = "Could not check whether Zotero is running. Nothing was written to the Zotero profile."
REFUSED_RUNNING_MESSAGE = "Zotero is running. Close Zotero, then run this command again. Nothing was written."
CLOSE_FAILED_MESSAGE = (
    "Zotero did not close, so nothing was installed. Close Zotero yourself, then try the install again. "
    "Nothing was written to the Zotero profile."
)
# An install that closed Zotero but then could not confirm it was closed (the process check failed,
# or Zotero still ran when the install looked). Meridian did close it, so the wording must say so.
UNCONFIRMED_CLOSED_STATUSES = frozenset({"check_failed", "refused_running"})
_CLOSE_POLL_SECONDS = 0.5
# Zotero can take longer than 20 seconds to shut down (a real install was refused at 20s with nothing written),
# so the install's close waits up to a minute.
_CLOSE_TIMEOUT_SECONDS = 60.0
REGISTER_TIMEOUT_SECONDS = 120.0
REGISTER_POLL_SECONDS = 1.0
# Zotero shuts down slowly after its first start, so the registration's close waits longer than the install's close.
REGISTER_CLOSE_TIMEOUT_SECONDS = 60.0
_TASKKILL_ARGV = ("taskkill", "/IM", "zotero.exe")

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

_MANUAL_STEPS = (
    "Install the plugin by hand:",
    "  1. Close Zotero.",
    "  2. In Zotero, open Tools > Plugins, click the gear icon, and choose Install Plugin From File.",
    "  3. Select: {xpi}",
)
INTEGRITY_STATUS = "integrity_failed"
INTEGRITY_FAILED_MESSAGE = (
    "The stored signed file failed its integrity check. Bump the version in manifest.json to sign a new build."
)
VERSION_USED_STATUS = "version_used"
VERSION_USED_MESSAGE = (
    "This plugin version was already submitted to Mozilla, and no verified signed file is stored for it. "
    "Bump the version in manifest.json to sign a new build. Nothing was written."
)
RECORD_UNREADABLE_STATUS = "record_unreadable"
# The same sentence sign-plugin shows, so both commands give one instruction.
RECORD_UNREADABLE_MESSAGE = (
    "The signing record is unreadable, so nothing will be submitted. Check "
    "%USERPROFILE%\\.meridian\\local_runner\\zotero_plugin\\signed.json (or zotero_plugin\\signed.json in the folder "
    "named by MERIDIAN_LOCAL_RUNNER_STATE_DIR, when that is set), then try again."
)


def _platform() -> str:
    return sys.platform


# ---------------------------------------------------------------------------
# Building the .xpi
# ---------------------------------------------------------------------------


def _bundled_source_dir() -> Path:
    """The bridge source folder: inside the PyInstaller bundle when frozen, else the repo copy.

    meridian-tray.spec ships ``extensions/zotero-bridge`` under ``sys._MEIPASS``
    (the same base the tray uses for its icon), without ``test/`` or
    ``node_modules/``.
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "extensions" / "zotero-bridge"
    return _SOURCE_DIR


def _resolve_source(source_dir: "Path | str | None") -> Path:
    return Path(source_dir) if source_dir is not None else _bundled_source_dir()


def _runtime_files(source: Path) -> list[tuple[str, Path]]:
    """(archive name, source path) for every runtime file, sorted by archive name."""
    found: dict[str, Path] = {}
    for name in _RUNTIME_FILES:
        path = source / name
        if not path.is_file():
            raise FileNotFoundError(f"zotero bridge source is missing {name}: {path}")
        found[name] = path
    for folder in _RUNTIME_DIRS:
        base = source / folder
        if not base.is_dir():
            raise FileNotFoundError(f"zotero bridge source is missing {folder}/: {base}")
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            if any(part in _EXCLUDED_PARTS for part in relative.parts) or path.name in _EXCLUDED_NAMES:
                continue
            found[relative.as_posix()] = path
    return sorted(found.items())


def _packed_bytes(path: Path) -> bytes:
    """The bytes written into the .xpi for one file.

    Text files get LF line endings, so a CRLF checkout (Windows autocrlf) and an
    LF checkout pack identical bytes. Binary files (anything with a NUL byte, or
    anything that is not valid UTF-8) are packed exactly as they are.
    """
    data = path.read_bytes()
    if b"\x00" in data:
        return data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    return text.replace("\r\n", "\n").encode("utf-8")


def build_xpi_bytes(source_dir: "Path | str | None" = None) -> bytes:
    """The deterministic .xpi bytes for the bridge source, built in memory."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, path in _runtime_files(_resolve_source(source_dir)):
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = _ZIP_CREATE_SYSTEM_UNIX
            info.external_attr = _ZIP_FILE_MODE << 16
            archive.writestr(info, _packed_bytes(path), compresslevel=9)
    return buffer.getvalue()


def build_xpi(out_path: "Path | str", source_dir: "Path | str | None" = None) -> Path:
    """Write the deterministic .xpi to ``out_path`` (atomically) and return it."""
    target = Path(out_path)
    _atomic_write_bytes(target, build_xpi_bytes(source_dir))
    return target


def default_xpi_path() -> Path:
    """Where the built .xpi is kept in Meridian's per-user state dir, not in Zotero."""
    from meridian.local_runner import default_state_dir

    return default_state_dir() / _DEFAULT_STATE_SUBDIR / XPI_FILENAME


# ---------------------------------------------------------------------------
# Locating Zotero's profile
# ---------------------------------------------------------------------------


def zotero_data_dir(env: "Mapping[str, str] | None" = None, home: "Path | str | None" = None) -> "Path | None":
    """Zotero's data root (the folder that holds profiles.ini), or None when unknown.

    Defaults are UNVERIFIED on a real install; see the module docstring.
    ``MERIDIAN_ZOTERO_DATA_DIR`` overrides them on any platform.
    """
    source = os.environ if env is None else env
    override = str(source.get(ZOTERO_DATA_DIR_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    platform = _platform()
    if platform == "win32":
        appdata = str(source.get("APPDATA") or "").strip()
        if not appdata:
            return None
        return Path(appdata) / "Zotero" / "Zotero"
    base = Path(home) if home is not None else Path.home()
    if platform == "darwin":
        return base / "Library" / "Application Support" / "Zotero"
    return base / ".zotero" / "zotero"


def _resolve_ini_path(root: Path, value: str, *, relative: bool) -> "Path | None":
    text = (value or "").strip()
    if not text:
        return None
    candidate = Path(text)
    if candidate.is_absolute():
        return candidate
    if relative:
        return root / candidate
    return None  # IsRelative=0 promises an absolute path; do not guess one


def _existing_dir(path: "Path | None") -> "Path | None":
    return path if path is not None and path.is_dir() else None


def find_default_profile_dir(data_dir: "Path | str | None") -> "Path | None":
    """The default profile folder named by ``profiles.ini``, or None rather than a guess.

    The ``[InstallXXXX] Default=`` key is tried first (relative to the data
    root unless absolute). Then the first ``[Profile*]`` section with
    ``Default=1``, honouring ``IsRelative``. A candidate counts only when that
    folder exists.
    """
    if data_dir is None:
        return None
    root = Path(data_dir)
    try:
        text = (root / "profiles.ini").read_text(encoding="utf-8", errors="replace")
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
            candidate = _existing_dir(
                _resolve_ini_path(root, parser.get(section, "Default"), relative=True)
            )
            if candidate is not None:
                return candidate
    for section in sections:
        if not section.startswith("Profile"):
            continue
        if parser.get(section, "Default", fallback="").strip() != "1":
            continue
        relative = parser.get(section, "IsRelative", fallback="1").strip() != "0"
        candidate = _existing_dir(
            _resolve_ini_path(root, parser.get(section, "Path", fallback=""), relative=relative)
        )
        if candidate is not None:
            return candidate
    return None


# ---------------------------------------------------------------------------
# Is Zotero running? (fails closed: an unknown answer means "running")
# ---------------------------------------------------------------------------


def _run_tasklist() -> str:
    kwargs: dict[str, Any] = {}
    if _platform() == "win32":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    completed = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq zotero.exe", "/NH"],
        capture_output=True,
        text=True,
        errors="replace",  # a byte that is not valid in the console code page must not raise
        timeout=10,
        check=False,
        **kwargs,
    )
    if completed.returncode != 0:
        # A failed listing says nothing about Zotero, so it must not read as "not running".
        raise RuntimeError(f"tasklist exited with code {completed.returncode}")
    return completed.stdout or ""


def _run_pgrep() -> str:
    completed = subprocess.run(
        ["pgrep", "-x", "zotero"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode == 1:
        # pgrep's own answer for "no process matched": a clean "not running".
        return ""
    if completed.returncode != 0:
        raise RuntimeError(f"pgrep exited with code {completed.returncode}")
    return completed.stdout or ""


def _tasklist_lists_zotero(output: str) -> bool:
    for line in output.splitlines():
        fields = line.split()
        if fields and fields[0].casefold() == "zotero.exe":
            return True
    return False


def zotero_process_state(runner: "Callable[[], str] | None" = None) -> "bool | None":
    """True when Zotero runs, False when it does not, None when the check itself failed. Never raises.

    ``runner`` returns the raw listing text (tasklist on Windows, pgrep
    elsewhere) so tests never call the real process list. The default runners
    raise on an error exit (tasklist: any non-zero code; pgrep: any code other
    than its no-match exit 1), and that becomes None. Empty output from a
    successful listing means False.
    """
    try:
        if _platform() == "win32":
            return _tasklist_lists_zotero(str((runner or _run_tasklist)() or ""))
        output = str((runner or _run_pgrep)() or "")
        return any(line.strip().isdigit() for line in output.splitlines())
    except Exception:
        return None


def zotero_is_running(runner: "Callable[[], str] | None" = None) -> bool:
    """True unless the process check positively reports that Zotero is closed. Never raises.

    An unknown state (the check failed) counts as running, so an install is never
    attempted on it. Callers that must tell "running" from "unknown" use
    :func:`zotero_process_state`.
    """
    return zotero_process_state(runner) is not False


def _safe_state(check: "Callable[[], bool | None]") -> "bool | None":
    """The check's answer as True, False or None (None when it fails or raises). Never raises."""
    try:
        answer = check()
    except Exception:  # noqa: BLE001 -- a failing check is an unknown state, never a crash
        return None
    return None if answer is None else bool(answer)


# ---------------------------------------------------------------------------
# Closing Zotero (a graceful close request only, never a force kill)
# ---------------------------------------------------------------------------


def _run_taskkill(argv: "list[str]") -> None:
    """Run the close request. Its exit code is not the outcome: the poll in the caller decides."""
    kwargs: dict[str, Any] = {}
    if _platform() == "win32":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    subprocess.run(
        argv,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=10,
        check=False,
        **kwargs,
    )


def close_zotero_gracefully(
    *,
    runner: "Callable[[list[str]], Any] | None" = None,
    timeout_s: float = _CLOSE_TIMEOUT_SECONDS,
    sleep: "Callable[[float], Any]" = time.sleep,
    running: "Callable[[], bool | None] | None" = None,
) -> str:
    """Ask Zotero to close and wait for it. Never raises, never kills.

    The only process command is ``taskkill /IM zotero.exe``: no ``/F`` (force) and no
    ``/T`` (tree). It sends Zotero the same close request as its window's close button.
    The process check runs every half second until ``timeout_s``.

    Returns ``"unsupported"`` off Windows (nothing runs), ``"not_running"`` when
    Zotero was not running at the start (nothing runs), ``"closed"`` when it
    stopped, and ``"close_failed"`` when it still runs at the timeout or the check
    fails. A failed check counts as a failure, never as "closed" (fail closed).
    """
    if _platform() != "win32":
        return "unsupported"
    check = running if running is not None else zotero_process_state
    start = _safe_state(check)
    if start is False:
        return "not_running"
    if start is None:
        return "close_failed"
    send = runner if runner is not None else _run_taskkill
    try:
        send(list(_TASKKILL_ARGV))
    except Exception:  # noqa: BLE001 -- the poll below decides whether Zotero closed
        pass
    for _ in range(int(round(timeout_s / _CLOSE_POLL_SECONDS))):
        sleep(_CLOSE_POLL_SECONDS)
        state = _safe_state(check)
        if state is False:
            return "closed"
        if state is None:
            return "close_failed"
    return "close_failed"


# ---------------------------------------------------------------------------
# Installing into the profile
# ---------------------------------------------------------------------------


def _atomic_write_bytes(target: "Path | str", payload: bytes) -> None:
    """Write through a temp file in the same folder, then atomically replace."""
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _result(status: str, target: "Path | None", error: "str | None" = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": status, "path": None if target is None else str(target)}
    if error:
        payload["error"] = error
    return payload


def is_zotero_profile(profile_dir: "Path | str") -> bool:
    """True when the folder holds one of Zotero's profile marker files."""
    folder = Path(profile_dir)
    return any((folder / name).is_file() for name in PROFILE_MARKERS)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def install_into_profile(
    profile_dir: "Path | str | None",
    xpi_path: "Path | str",
    *,
    running: "Callable[[], bool] | None" = None,
    expected_sha256: str,
) -> dict[str, Any]:
    """Copy the .xpi into ``profile_dir/extensions/``.

    ``status`` is one of ``installed``, ``unchanged``, ``refused_not_profile``,
    ``refused_running``, ``check_failed``, ``no_profile``, ``integrity_failed`` or ``failed``.
    The profile must hold a marker file (``prefs.js`` or ``zotero.sqlite``), and the
    process check must succeed and find Zotero closed. Every refusal happens
    before any write. ``running`` returns True, False, or None for a failed
    check. It defaults to :func:`zotero_process_state`, looked up at call time.

    ``expected_sha256`` is required and keyword-only, with no default, so no caller can skip the check. The file is
    read once. Its bytes must hash to ``expected_sha256``, or the result is ``integrity_failed`` and nothing is
    written. The bytes checked are the bytes written.
    """
    try:
        if profile_dir is None or not Path(profile_dir).is_dir():
            return _result("no_profile", None)
        if not is_zotero_profile(profile_dir):
            return _result(
                "refused_not_profile", None, error=_NOT_PROFILE_MESSAGE.format(profile=profile_dir),
            )
        check = running if running is not None else zotero_process_state
        state = check()
        if state is None:
            return _result("check_failed", None, error=_CHECK_FAILED_MESSAGE)
        if state:
            return _result("refused_running", None)
        target = Path(profile_dir) / "extensions" / XPI_FILENAME
        payload = Path(xpi_path).read_bytes()
        if _sha256(payload) != expected_sha256:
            return _integrity_refusal()
        if target.is_file() and target.read_bytes() == payload:
            return _result("unchanged", target)
        _atomic_write_bytes(target, payload)
        return _result("installed", target)
    except Exception as exc:  # noqa: BLE001 -- report every failure as data, never raise
        return _result("failed", None, error=str(exc))


def _already_current(profile: "Path | str", expected_sha256: str) -> "dict[str, Any] | None":
    """The ``unchanged`` result when the installed plugin is the verified signed file, else None.

    It reads only the installed copy, never the signed file, so it can run before any close and whatever Zotero's
    state is. Never raises: a read that fails means "not known to be current", and the install path then decides.
    """
    target = Path(profile) / "extensions" / XPI_FILENAME
    try:
        if target.is_file() and _sha256(target.read_bytes()) == expected_sha256:
            return _result("unchanged", target)
    except Exception:  # noqa: BLE001 -- an unreadable file is not known to be current
        return None
    return None


def _needs_signature_result(unsigned: "Path | str") -> dict[str, Any]:
    """The refusal for an unsigned bundle. Nothing is closed or written to Zotero; the unsigned path is for the manual route."""
    return {
        "status": NEEDS_SIGNATURE_STATUS,
        "path": None,
        "message": needs_signature_message(unsigned),
        "unsigned_path": str(unsigned),
    }


def _integrity_refusal() -> dict[str, Any]:
    """The refusal for a stored signed file whose bytes no longer match its record. Nothing is closed or written."""
    return {
        "status": INTEGRITY_STATUS,
        "path": None,
        "error": INTEGRITY_FAILED_MESSAGE,
        "message": INTEGRITY_FAILED_MESSAGE,
    }


def _refusal(status: str, message: str) -> dict[str, Any]:
    """A refusal for a stored state that must not be installed. Nothing is closed or written."""
    return {"status": status, "path": None, "error": message, "message": message}


def _blocked_refusal(stored: Any) -> "dict[str, Any] | None":
    """The refusal for a stored state that must never be installed.

    None for ``signed`` and ``missing``. Never raises.
    """
    from meridian import zotero_plugin_sign as sign  # imported here: that module imports this one

    if stored.tampered:
        return _integrity_refusal()
    if stored.status == sign.USED_STATUS:
        return _refusal(VERSION_USED_STATUS, VERSION_USED_MESSAGE)
    if stored.status == sign.UNREADABLE_STATUS:
        return _refusal(RECORD_UNREADABLE_STATUS, RECORD_UNREADABLE_MESSAGE)
    return None


def _signed_refusal(stored: Any, unsigned: "Path | str") -> "dict[str, Any] | None":
    """The refusal for a stored state that is not signed, or None when the stored file may be installed."""
    if stored.signed:
        return None
    blocked = _blocked_refusal(stored)
    return blocked if blocked is not None else _needs_signature_result(unsigned)


def _prepare_default_install() -> "tuple[Path, Path, str] | dict[str, Any]":
    """The default profile, the verified signed file and its hash, resolved before anything is closed. Never raises.

    Returns ``(profile, signed_xpi, sha256)`` when everything is ready. Otherwise it returns the refusal to hand back
    unchanged: ``no_profile`` when there is no default profile, ``refused_not_profile`` when the profile folder holds
    no marker file (the same check the CLI makes), ``needs_signature`` when no signature is stored for this bundle,
    ``version_used`` when the version was submitted with no verified signed file, ``record_unreadable`` when the
    signing record cannot be read, ``integrity_failed`` when the stored file no longer matches its record, or
    ``failed`` when the bundle will not build. The unsigned bundle is written into Meridian's own state folder,
    never into Zotero.
    """
    try:
        profile = find_default_profile_dir(zotero_data_dir())
        if profile is None:
            return _result("no_profile", None)
        if not is_zotero_profile(profile):
            return _result("refused_not_profile", None, error=_NOT_PROFILE_MESSAGE.format(profile=profile))
        unsigned = build_xpi(default_xpi_path())
        stored = _stored_with_fetch()
        refusal = _signed_refusal(stored, unsigned)
        if refusal is not None:
            return refusal
        return profile, stored.path, stored.sha256
    except Exception as exc:  # noqa: BLE001 -- report every failure as data, never raise
        return _result("failed", None, error=str(exc))


NEEDS_SIGNATURE_STATUS = "needs_signature"
# The CLI keeps the signing command. The tray names the source checkout instead.
_NEEDS_SIGNATURE_TEMPLATE = (
    "This plugin version is not signed yet. Run: python -m meridian zotero sign-plugin (it needs your Mozilla "
    "API key and secret in environment variables). Until then, you can install the unsigned file yourself: "
    "Tools > Plugins, click the gear icon, choose Install Plugin From File, then select the file shown here: {path}."
)
_TRAY_NEEDS_SIGNATURE_TEMPLATE = (
    "This plugin version is not signed yet. Sign the plugin from a source checkout of Meridian, then install again "
    "here. Until then, you can install the unsigned file yourself: Tools > Plugins, click the gear icon, choose "
    "Install Plugin From File, then select the file shown here: {path}."
)


def signing_state_dir() -> Path:
    """Where the signed artifacts and their record live: the same per-user folder as the unsigned bundle."""
    return default_xpi_path().parent


def needs_signature_message(unsigned_path: "Path | str") -> str:
    """The CLI text for an unsigned bundle: the signing command, and the manual route to the unsigned file."""
    return _NEEDS_SIGNATURE_TEMPLATE.format(path=unsigned_path)


def tray_needs_signature_message(unsigned_path: "Path | str") -> str:
    """The tray text for an unsigned bundle: the source checkout, and the manual route to the unsigned file."""
    return _TRAY_NEEDS_SIGNATURE_TEMPLATE.format(path=unsigned_path)


def signed_install_state(source_dir: "Path | str | None" = None) -> Any:
    """What is stored for the bundle's version and source, as a ``StoredSigned``. Reads only. Never raises.

    An unreadable bundle reads as ``missing``, so the install asks for a signature rather than guessing.
    """
    from meridian import zotero_plugin_sign as sign  # imported here: that module imports this one

    try:
        source = _resolve_source(source_dir)
        return sign.stored_signed_artifact(
            signing_state_dir(), sign.bundle_version(source), source_sha256=sign.source_fingerprint(source),
        )
    except Exception:  # noqa: BLE001 -- an unreadable bundle or record is "not signed", never a crash
        return sign.StoredSigned(status=sign.MISSING_STATUS)


def install_default_profile(*, running: "Callable[[], bool] | None" = None) -> dict[str, Any]:
    """Install the bundled plugin into Zotero's default profile, with no close step. Never raises.

    The profile is the one ``profiles.ini`` marks as default, under
    ``zotero_data_dir()``. The Zotero profile is left alone when there is no
    default profile, while Zotero runs, or when the process check fails. The
    bundled .xpi is rebuilt into Meridian's own state folder first, which is not
    part of Zotero. A failure anywhere else, including the bundle not building,
    is reported as ``failed``. The tray closes a running Zotero through
    :func:`install_default_with_close` instead.
    """
    prepared = _prepare_default_install()
    if isinstance(prepared, dict):
        return prepared
    profile, signed, sha256 = prepared
    return install_into_profile(profile, signed, running=running, expected_sha256=sha256)


# ---------------------------------------------------------------------------
# Installing with Zotero closed first (the tray and the CLI use this)
# ---------------------------------------------------------------------------


def _nothing_before_launch() -> None:
    """The restart's pre-launch hook. The plugin was just installed, so there is nothing to place."""
    return None


def _start_zotero_after_close() -> str:
    """Restart Zotero through the launcher's start path. Returns the launcher's action."""
    from meridian import zotero_launcher  # imported here: zotero_launcher imports this module

    result = zotero_launcher.start_zotero(before_launch=_nothing_before_launch)
    return str(result.get("action") or "error")


def _restart_after_close(restart: "Callable[[], Any] | None") -> str:
    """Best-effort restart of a Zotero that Meridian closed. Never raises; returns an action or ``error``."""
    try:
        action = (restart if restart is not None else _start_zotero_after_close)()
    except Exception:  # noqa: BLE001 -- a failed restart must never fail the install
        return "error"
    return str(action or "error")


def _install_outcome(install: "Callable[[], Mapping[str, Any]]", *, closed: bool) -> dict[str, Any]:
    """Run one install and tag it with whether Meridian closed Zotero. Never raises."""
    try:
        outcome = dict(install())
    except Exception as exc:  # noqa: BLE001 -- report every failure as data, never raise
        outcome = {"status": "failed", "path": None, "error": str(exc) or "unknown error"}
    outcome["closed_zotero"] = closed
    outcome.setdefault("restart", None)
    return outcome


def _refusal_result(status: str, message: str) -> dict[str, Any]:
    """A result for an install that was not attempted: nothing was written and nothing restarts."""
    return {"status": status, "path": None, "message": message, "closed_zotero": False, "restart": None}


def install_with_close(
    settings: "Mapping[str, Any] | None",
    *,
    install: "Callable[[], Mapping[str, Any]]",
    close_zotero: "bool | None" = None,
    running: "Callable[[], bool | None] | None" = None,
    close: "Callable[..., str] | None" = None,
    restart: "Callable[[], Any] | None" = None,
    env: "Mapping[str, str] | None" = None,
    after_install: "Callable[[], Mapping[str, Any]] | None" = None,
) -> dict[str, Any]:
    """Install the plugin, closing Zotero first when the choice allows it. Never raises.

    ``after_install`` runs after a successful install, while Zotero is closed, and its result is kept under ``enable``.
    It is the registration step, which starts and closes Zotero itself, so it runs before any restart.

    ``install`` writes the plugin and is supplied by the caller, who resolves the profile and the
    bundle before this runs. So no profile lookup or build happens after a close.

    ``close_zotero=None`` follows the saved choice, and ``MERIDIAN_ZOTERO_CLOSE_ON_INSTALL`` can
    turn that off. An explicit bool wins over both: the CLI's ``--keep-zotero-open`` passes False.

    * The process check fails: ``check_failed``. Nothing is closed, and ``install`` is not called.
    * Zotero not running: install as usual. Nothing is closed or started.
    * Zotero running and closing off, or closing unsupported: ``refused_running``.
      Nothing is closed.
    * Zotero running and closing on: :func:`close_zotero_gracefully` runs.
      ``close_failed`` writes nothing and restarts nothing. ``closed`` installs,
      then restarts Zotero, best effort, because Meridian closed it.

    The result always carries ``status``, ``path``, ``closed_zotero`` (True only
    when Meridian closed Zotero in this run) and ``restart`` (the launcher's action,
    ``error``, or None when no restart was attempted). ``message`` is set for
    ``check_failed``, ``refused_running`` and ``close_failed``.
    """
    want_close = close_zotero if close_zotero is not None else effective_close_zotero(settings, env)
    check = running if running is not None else zotero_process_state
    close_step = close if close is not None else close_zotero_gracefully
    start = _safe_state(check)
    if start is None:
        # The state of Zotero is unknown. Refuse here, so that neither the close nor the install runs.
        return _refusal_result("check_failed", _CHECK_FAILED_MESSAGE)
    if start is False:
        return _after_install(_install_outcome(install, closed=False), after_install)
    if not want_close:
        return _refusal_result("refused_running", REFUSED_RUNNING_MESSAGE)
    try:
        outcome = close_step(running=check)
    except Exception:  # noqa: BLE001 -- a close that raises is a close that failed
        outcome = "close_failed"
    if outcome == "close_failed":
        return _refusal_result("close_failed", CLOSE_FAILED_MESSAGE)
    if outcome == "unsupported":
        return _refusal_result("refused_running", REFUSED_RUNNING_MESSAGE)
    if outcome != "closed":
        # "not_running": Zotero stopped between the two checks. Meridian closed nothing, so nothing restarts.
        return _after_install(_install_outcome(install, closed=False), after_install)
    result = _after_install(_install_outcome(install, closed=True), after_install)
    result["restart"] = _restart_after_close(restart)
    return result


def _after_install(result: dict[str, Any], after_install: "Callable[[], Mapping[str, Any]] | None") -> dict[str, Any]:
    """Run the registration step after a successful install, and keep its result under ``enable``. Never raises."""
    if after_install is None or result.get("status") not in ("installed", "unchanged"):
        return result
    try:
        result["enable"] = dict(after_install())
    except Exception as exc:  # noqa: BLE001 -- a failed registration is reported, never raised
        result["enable"] = {"status": "failed", "message": str(exc)}
    return result


def install_default_with_close(
    settings: "Mapping[str, Any] | None",
    *,
    close_zotero: "bool | None" = None,
    running: "Callable[[], bool | None] | None" = None,
    close: "Callable[..., str] | None" = None,
    restart: "Callable[[], Any] | None" = None,
    env: "Mapping[str, str] | None" = None,
    launch: "Callable[[], Any] | None" = None,
    register_timeout_s: "float | None" = None,
) -> dict[str, Any]:
    """The tray's install: the default profile and the bundle first, then :func:`install_with_close`. Never raises.

    After the file is in place, the registration step enables the plugin (see ``register_and_enable``). ``launch``
    starts Zotero for that step and defaults to the launcher. A ``user.js`` that sets the scan pref to anything
    else is refused before anything closes.

    The profile is found, checked for its marker, and the bundle is built before anything closes, as the
    CLI does it. A missing default profile, a folder that is not a profile, or a bundle that will not build
    is refused and Zotero is left alone. A plugin already identical to the bundle is ``unchanged`` with no
    close and no restart, whether or not Zotero runs. Its profile repairs still run, without rewriting the .xpi.
    """
    prepared = _prepare_default_install()
    if isinstance(prepared, dict):
        return {**prepared, "closed_zotero": False, "restart": None}
    profile, signed, sha256 = prepared
    check = running if running is not None else zotero_process_state
    scan = check_scan_pref(profile)
    if scan in (SCAN_CONFLICT, SCAN_UNREADABLE):
        return _refusal_result(PREF_CONFLICT_STATUS, _pref_refusal_message(scan))
    current = _already_current(profile, sha256)
    if current is not None:
        # The file is already current, so nothing is closed. The plugin is enabled only while Zotero is not running.
        return _unchanged_outcome(profile, current, check, close, launch, register_timeout_s)
    return install_with_close(
        settings,
        install=lambda: _install_files(profile, signed, check, sha256),
        close_zotero=close_zotero,
        running=check,
        close=close,
        restart=restart,
        env=env,
        after_install=lambda: _register_default(profile, check, close, launch, register_timeout_s),
    )


# ---------------------------------------------------------------------------
# Registering and enabling the plugin, with no click
# ---------------------------------------------------------------------------
#
# Zotero scans the profile's extensions/ folder only when extensions.startupScanScopes allows it (its default is
# 0), so a file dropped in is ignored by an ordinary start. A newly registered sideload comes up disabled, because
# extensions.autoDisableScopes is 15. This section makes both facts work for this one add-on:
#
# * user.js gets one line that sets the scan scope. Every other line in user.js is kept.
# * Zotero registers the file on a start. Meridian then enables only this add-on's entry in extensions.json,
#   with Zotero closed. Other entries, and the rest of the profile, are left as they are.
# * Meridian records the profiles it enabled the plugin in. A plugin the user later disables stays disabled.

ADDON_ID = "meridian-zotero@usemeridian.us"  # equal to zotero_plugin_sign.PLUGIN_ADDON_ID (a test checks it)
SCAN_PREF = "extensions.startupScanScopes"
SCAN_PREF_VALUE = 1
SCAN_PREF_LINE = f'user_pref("{SCAN_PREF}", {SCAN_PREF_VALUE});'
# ASCII flag: user.js is read as latin-1, so a non-ASCII byte must never count as whitespace here.
_SCAN_PREF_RE = re.compile(r'user_pref\(\s*"extensions\.startupScanScopes"\s*,\s*([^)]*?)\s*\)\s*;?', re.ASCII)
# What check_scan_pref and ensure_scan_pref report about user.js.
SCAN_ADDED = "added"
SCAN_PRESENT = "present"
SCAN_CONFLICT = "conflict"
SCAN_UNREADABLE = "unreadable"
EXTENSIONS_JSON = "extensions.json"
ENABLED_RECORD = "zotero_plugin_enabled.json"
PREF_CONFLICT_STATUS = "pref_conflict"
PREF_CONFLICT_MESSAGE = (
    f"This Zotero profile's user.js sets {SCAN_PREF} to something other than {SCAN_PREF_VALUE}, so Meridian will "
    "not change it. Remove that line, then run this command again. Nothing was written."
)
PREF_UNREADABLE_MESSAGE = (
    "This Zotero profile's user.js exists but cannot be read, so Meridian will not change it. Check that the file "
    "can be read, then run this command again. Nothing was written."
)
# A repair that fails after the .xpi is in place: nothing is refused, so the text says only what is missing.
_REPAIR_SCAN_FAILED_MESSAGE = (
    f"user.js could not be updated: it cannot be read, or {SCAN_PREF} is set to something other than "
    f"{SCAN_PREF_VALUE}. Fix that, then run this command again."
)
_REGISTER_MESSAGES = {
    "registry_unreadable": "Zotero's extensions.json could not be read, so the plugin was not enabled. Nothing was changed.",
    "close_failed": "Zotero did not close after it registered the plugin, so nothing was enabled. Close Zotero, then run this command again.",
    "register_timeout": "Zotero did not register the plugin within two minutes, so it is not enabled yet. Start Zotero once, then run this command again.",
    "left_disabled": "The plugin is disabled in Zotero. Meridian enabled it earlier, so it was left disabled. Enable it in Tools > Plugins to turn it back on.",
    "blocked": "Zotero blocks this plugin, so it was not enabled.",
    "not_registered": "Zotero did not list the plugin after it started, so it is not enabled yet. Start Zotero once, then run this command again.",
    "failed": "The plugin could not be enabled.",
}
_ENABLE_OK_MESSAGES = {
    "enabled": "The plugin is enabled in Zotero.",
    "already_enabled": "The plugin is enabled in Zotero.",
}
_SKIPPED_RUNNING_MESSAGE = (
    "Zotero is running, so the plugin's enable state was not changed. Close Zotero, then run this command again."
)


def _active_text(text: str) -> str:
    """The text without its comment lines. A line whose first non-space characters are ``//`` is a comment, not a setting."""
    return "\n".join(line for line in text.split("\n") if not line.lstrip(" \t").startswith("//"))


def _scan_state(text: str) -> str:
    """The scan state of user.js text: ``added``, ``present`` or ``conflict``. Pure."""
    values = [match.group(1).strip() for match in _SCAN_PREF_RE.finditer(_active_text(text))]
    if any(value != str(SCAN_PREF_VALUE) for value in values):
        return SCAN_CONFLICT
    return SCAN_PRESENT if values else SCAN_ADDED


def _pref_refusal_message(state: str) -> str:
    """The refusal text for a user.js that sets the scan pref to another value, or that cannot be read. Pure."""
    return PREF_UNREADABLE_MESSAGE if state == SCAN_UNREADABLE else PREF_CONFLICT_MESSAGE


def check_scan_pref(profile: "Path | str") -> str:
    """The scan state of user.js: ``added`` (no active scan line), ``present`` (the one Meridian needs), ``conflict``
    (any other active value), or ``unreadable`` (user.js exists but cannot be read). Reads only. Never raises.

    A commented-out line such as ``// user_pref(...)`` is not a setting and does not count.
    """
    from meridian import zotero_bridge_api as bridge  # imported here: the bridge module imports the local-runner module

    try:
        text = bridge.read_user_js(profile)
    except Exception:  # noqa: BLE001 -- an unreadable user.js is a refusal state, never a crash
        return SCAN_UNREADABLE
    return _scan_state(text)


def ensure_scan_pref(profile: "Path | str") -> str:
    """Add the scan line to user.js when no active one exists, keeping every other byte. Returns what was found.

    Writes only for ``added``. A failed write raises OSError; an unreadable user.js is reported as ``unreadable``.
    """
    from meridian import zotero_bridge_api as bridge  # imported here: see check_scan_pref

    try:
        text = bridge.read_user_js(profile)
    except Exception:  # noqa: BLE001 -- see check_scan_pref
        return SCAN_UNREADABLE
    state = _scan_state(text)
    if state == SCAN_ADDED:
        lines, newline = bridge.user_js_lines(text)
        bridge.write_user_js(profile, "".join(line + "\n" for line in lines) + SCAN_PREF_LINE + newline)
    return state


def _read_registry(profile: "Path | str") -> "tuple[str, dict[str, Any] | None, dict[str, Any] | None]":
    """``(status, data, entry)``: ``missing``, ``unreadable``, or ``ok`` with this add-on's entry (None when not registered). Reads only."""
    path = Path(profile) / EXTENSIONS_JSON
    if not path.is_file():
        return "missing", None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unreadable", None, None
    addons = data.get("addons") if isinstance(data, dict) else None
    if not isinstance(addons, list):
        return "unreadable", None, None
    entry = next((item for item in addons if isinstance(item, dict) and item.get("id") == ADDON_ID), None)
    return "ok", data, entry


def _enable_entry(profile: "Path | str") -> dict[str, Any]:
    """Clear ``userDisabled`` on this add-on's entry and nothing else. Zotero must be closed. Never raises."""
    status, data, entry = _read_registry(profile)
    if status != "ok" or entry is None:
        return _register_result("not_registered")
    if entry.get("appDisabled") or (entry.get("blocklistState") or 0) != 0:
        return _register_result("blocked")
    entry["userDisabled"] = False
    entry["active"] = not entry.get("softDisabled", False)
    _atomic_write_bytes(Path(profile) / EXTENSIONS_JSON, (json.dumps(data, indent=2) + "\n").encode("utf-8"))
    return _register_result("enabled")


def _record_path() -> Path:
    return signing_state_dir() / ENABLED_RECORD


def _profile_key(profile: "Path | str") -> str:
    return os.path.normcase(str(Path(profile).resolve()))


def _recorded_profiles() -> list[str]:
    try:
        payload = json.loads(_record_path().read_text(encoding="utf-8"))
        keys = payload.get("profiles", []) if isinstance(payload, dict) else []
        return [key for key in keys if isinstance(key, str)]
    except (OSError, ValueError):
        return []


def _enabled_by_meridian(profile: "Path | str") -> bool:
    return _profile_key(profile) in _recorded_profiles()


def _remember_enabled(profile: "Path | str") -> None:
    keys = _recorded_profiles()
    key = _profile_key(profile)
    if key not in keys:
        keys.append(key)
    _atomic_write_bytes(_record_path(), (json.dumps({"profiles": keys}, indent=2) + "\n").encode("utf-8"))


def _register_result(status: str) -> dict[str, Any]:
    message = _REGISTER_MESSAGES.get(status) or _ENABLE_OK_MESSAGES.get(status, "")
    return {"status": status, "message": message}


def _wait_for_registration(
    profile: "Path | str", *, timeout_s: float, sleep: "Callable[[float], Any]", clock: "Callable[[], float]",
) -> bool:
    """True once extensions.json lists this add-on, False when ``timeout_s`` passes first."""
    deadline = clock() + timeout_s
    while True:
        status, _data, entry = _read_registry(profile)
        if status == "ok" and entry is not None:
            return True
        if clock() >= deadline:
            return False
        sleep(REGISTER_POLL_SECONDS)


def register_and_enable(
    profile: "Path | str",
    *,
    launch: "Callable[[], Any]",
    close: "Callable[..., str]",
    running: "Callable[[], bool | None]",
    timeout_s: "float | None" = None,
    sleep: "Callable[[float], Any]" = time.sleep,
    clock: "Callable[[], float]" = time.monotonic,
) -> dict[str, Any]:
    """Get Zotero to register the plugin, then enable it. Zotero must be closed when this runs. Never raises.

    * Already enabled: ``already_enabled``. Nothing is changed except Meridian's record.
    * Not registered yet: ``launch`` starts Zotero, Meridian waits up to ``timeout_s`` for the entry, and
      ``close`` closes Zotero again. No entry means ``register_timeout``. Zotero that does not close means
      ``close_failed``. Nothing is enabled in either case.
    * Disabled, and Meridian enabled it before: ``left_disabled``. The user turned it off on purpose.
    * Otherwise the entry is enabled: ``enabled``.
    """
    try:
        status, _data, entry = _read_registry(profile)
        if status == "unreadable":
            return _register_result("registry_unreadable")
        if entry is None:
            launch()
            wait_s = REGISTER_TIMEOUT_SECONDS if timeout_s is None else timeout_s
            registered = _wait_for_registration(profile, timeout_s=wait_s, sleep=sleep, clock=clock)
            closed = close(running=running)
            if closed not in ("closed", "not_running"):
                return _register_result("close_failed")
            if not registered:
                return _register_result("register_timeout")
            status, _data, entry = _read_registry(profile)
            if status != "ok" or entry is None:
                return _register_result("not_registered")
        if not entry.get("userDisabled", False):
            _remember_enabled(profile)
            return _register_result("already_enabled")
        if _enabled_by_meridian(profile):
            return _register_result("left_disabled")
        result = _enable_entry(profile)
        if result["status"] == "enabled":
            _remember_enabled(profile)
        return result
    except Exception:  # noqa: BLE001 -- every failure is data for the caller, never a crash
        return _register_result("failed")


def _ensure_api(profile: "Path | str") -> str:
    """The local write API's token file, then the two profile prefs that point the plugin at it. Returns the prefs state.

    The token value is never returned or printed. Its state is read first, by :func:`_api_token_state`.
    """
    from meridian import zotero_bridge_api as bridge  # imported here: the bridge module imports the local-runner module

    path = bridge.token_file_path()
    bridge.ensure_api_token(path)
    return bridge.ensure_api_prefs(profile, path, bridge.API_PORT)


def _api_token_state() -> str:
    """``kept`` when the token file already holds a usable token, else ``created``: what :func:`_ensure_api` is about to do.

    Reads only, and never raises. The token value is never returned.
    """
    from meridian import zotero_bridge_api as bridge  # imported here: see _ensure_api

    try:
        return bridge.TOKEN_KEPT if bridge.token_is_usable(bridge.token_file_path()) else bridge.TOKEN_CREATED
    except Exception:  # noqa: BLE001 -- the state is only reported; _ensure_api decides what is written
        return bridge.TOKEN_CREATED


def _repaired_keys(scan: str, token: str, prefs: str) -> list[str]:
    """The repairs one run actually made: ``scan_pref``, ``api_token`` and ``api_prefs``. Pure."""
    from meridian import zotero_bridge_api as bridge  # imported here: see _ensure_api

    keys: list[str] = []
    if scan == SCAN_ADDED:
        keys.append("scan_pref")
    if token == bridge.TOKEN_CREATED:
        keys.append("api_token")
    if prefs != bridge.PREFS_PRESENT:
        keys.append("api_prefs")
    return keys


def _api_listening() -> bool:
    """Whether something accepts a connection on the local write API's loopback port. Reads only; never raises.

    The API starts when Zotero starts, from the profile's prefs, so a repair made while Zotero runs does not
    bring it up. The result tells the caller, so the install can say that Zotero needs a restart.
    """
    import socket

    from meridian import zotero_bridge_api as bridge  # imported here: see _ensure_api

    try:
        with socket.create_connection(("127.0.0.1", bridge.API_PORT), timeout=1.0):
            return True
    except OSError:
        return False


def _repair_profile(profile: "Path | str", result: Mapping[str, Any]) -> dict[str, Any]:
    """The profile repairs that follow the .xpi: the scan line, then the API token and prefs. Never raises.

    Every step is idempotent, so the same call on a complete profile writes nothing. The result gains the states
    ``scan_pref``, ``api_token`` and ``api_prefs``, and ``repaired``, the repairs this run made. The .xpi is never
    touched. A failed step makes the result ``failed``, and the steps after it do not run.
    """
    try:
        scan = ensure_scan_pref(profile)
    except Exception as exc:  # noqa: BLE001 -- a failed user.js write is reported, the file is already in place
        return {**result, "status": "failed", "error": f"user.js could not be updated: {exc}"}
    if scan in (SCAN_CONFLICT, SCAN_UNREADABLE):
        return {**result, "status": "failed", "error": _REPAIR_SCAN_FAILED_MESSAGE}
    token = _api_token_state()
    try:
        prefs = _ensure_api(profile)
    except Exception as exc:  # noqa: BLE001 -- the API stays off until its prefs are written; the result says so
        return {**result, "status": "failed", "error": f"The local write API could not be set up: {exc}"}
    return {
        **result,
        "scan_pref": scan,
        "api_token": token,
        "api_prefs": prefs,
        "repaired": _repaired_keys(scan, token, prefs),
        "api_listening": _api_listening(),
    }


def _install_files(
    profile: "Path | str", signed: "Path | str", running: "Callable[[], bool | None] | None", sha256: str,
) -> dict[str, Any]:
    """The install's writes: the signed file, then the profile repairs. Each write follows the file. Never raises."""
    result = install_into_profile(profile, signed, running=running, expected_sha256=sha256)
    if result.get("status") in ("installed", "unchanged"):
        return _repair_profile(profile, result)
    return result


def _fetch_approved_if_missing() -> None:
    """Ask Mozilla for the approved file when none is stored for this bundle. Reads only: nothing is submitted. Never raises."""
    from meridian import zotero_plugin_sign as sign  # imported here: that module imports this one

    try:
        sign.sign_plugin(fetch=True, state_dir=signing_state_dir())
    except Exception:  # noqa: BLE001 -- a failed fetch leaves the install to report that nothing is stored
        return


def _stored_with_fetch() -> Any:
    """The stored signed state, asking Mozilla for the approved file first when none is stored. Never raises."""
    stored = signed_install_state()
    if not stored.signed:
        _fetch_approved_if_missing()
        stored = signed_install_state()
    return stored


def _close_after_registration(running: "Callable[[], bool | None] | None" = None) -> str:
    """Close the Zotero the registration step started, with the longer registration wait. Never raises."""
    return close_zotero_gracefully(running=running, timeout_s=REGISTER_CLOSE_TIMEOUT_SECONDS)


def _register_default(
    profile: "Path | str",
    running: "Callable[[], bool | None] | None",
    close: "Callable[..., str] | None",
    launch: "Callable[[], Any] | None",
    timeout_s: "float | None",
) -> dict[str, Any]:
    """The registration step for the default profile, with Zotero closed. Never raises.

    The default launch is the launcher's start, which returns once Zotero's local API answers. A close sent before
    that can be ignored while Zotero is still starting, so the default close runs only after the start.
    """
    check = running if running is not None else zotero_process_state
    return register_and_enable(
        profile,
        launch=launch if launch is not None else _start_zotero_after_close,
        close=close if close is not None else _close_after_registration,
        running=check,
        timeout_s=timeout_s,
    )


def _unchanged_outcome(
    profile: "Path | str",
    current: Mapping[str, Any],
    running: "Callable[[], bool | None] | None",
    close: "Callable[..., str] | None",
    launch: "Callable[[], Any] | None",
    timeout_s: "float | None",
) -> dict[str, Any]:
    """The outcome when the installed file is already the signed one: the profile repairs run, then the enable step.

    The .xpi is not rewritten and nothing is closed. The enable step runs only while Zotero is closed. Never raises.
    """
    repaired = _repair_profile(profile, current)
    result = {**repaired, "closed_zotero": False, "restart": None}
    if repaired.get("status") != "unchanged":
        return result
    check = running if running is not None else zotero_process_state
    if _safe_state(check) is False:
        result["enable"] = _register_default(profile, check, close, launch, timeout_s)
        return result
    _status, _data, entry = _read_registry(profile)
    if entry is not None and not entry.get("userDisabled", False):
        result["enable"] = _register_result("already_enabled")
    else:
        result["enable"] = {"status": "skipped_zotero_running", "message": _SKIPPED_RUNNING_MESSAGE}
    return result


# ---------------------------------------------------------------------------
# Settings and the first-run choice
# ---------------------------------------------------------------------------


def settings_path() -> Path:
    """``zotero_plugin.json`` in the per-user Meridian state dir."""
    from meridian.local_runner import default_state_dir

    return default_state_dir() / SETTINGS_FILENAME


def _valid_settings(payload: Any) -> "dict[str, Any] | None":
    """The settings as real booleans, or None for any other shape.

    ``autoinstall`` and ``decided`` are required, so a partial file counts as wrong.
    ``close_zotero_for_install`` is optional: a file written before it existed reads as
    True, which keeps every saved decision. A present value that is not a boolean makes
    the whole file wrong.
    """
    if not isinstance(payload, dict):
        return None
    autoinstall = payload.get("autoinstall")
    decided = payload.get("decided")
    close = payload.get(CLOSE_SETTING_KEY, True)
    if not isinstance(autoinstall, bool) or not isinstance(decided, bool) or not isinstance(close, bool):
        return None
    return {"autoinstall": autoinstall, "decided": decided, CLOSE_SETTING_KEY: close}


def load_settings(path: "Path | str") -> dict[str, Any]:
    """The saved settings. A missing, unreadable or wrong-shape file reads as undecided, auto-install off.

    Nothing is installed from such a file, and the first-run prompt asks again.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(_DEFAULT_SETTINGS)
    return _valid_settings(payload) or dict(_DEFAULT_SETTINGS)


def save_settings(path: "Path | str", settings: Mapping[str, Any]) -> None:
    """Atomically persist the settings keys. Anything but booleans is refused, not written."""
    valid = _valid_settings(settings)
    if valid is None:
        raise ValueError(
            "zotero plugin settings need boolean 'autoinstall', 'decided' and 'close_zotero_for_install' values"
        )
    text = json.dumps(valid, sort_keys=True) + "\n"
    _atomic_write_bytes(Path(path), text.encode("utf-8"))


def apply_first_run_choice(settings: Mapping[str, Any], install: bool) -> dict[str, Any]:
    """A new settings dict: the answer is recorded and ``decided`` becomes true. The close choice is kept."""
    return {
        "autoinstall": bool(install),
        "decided": True,
        CLOSE_SETTING_KEY: saved_close_zotero(settings),
    }


def saved_close_zotero(settings: "Mapping[str, Any] | None") -> bool:
    """The saved ``close_zotero_for_install`` choice. True when absent or when the settings are unreadable."""
    current = _valid_settings(settings) or dict(_DEFAULT_SETTINGS)
    return bool(current[CLOSE_SETTING_KEY])


def autoinstall_supported() -> bool:
    """Auto-install and its first-run prompt are Windows-only in this build."""
    return _platform() == "win32"


def _recognised_env_bool(name: str, env: "Mapping[str, str] | None") -> "bool | None":
    source = os.environ if env is None else env
    raw = str(source.get(name) or "").strip().casefold()
    if raw in _FALSE_VALUES:
        return False
    if raw in _TRUE_VALUES:
        return True
    return None


def autoinstall_env_override(env: "Mapping[str, str] | None" = None) -> "bool | None":
    """True or False when AUTOINSTALL_ENV holds a recognised value; None when it is unset or unknown."""
    return _recognised_env_bool(AUTOINSTALL_ENV, env)


def close_zotero_env_override(env: "Mapping[str, str] | None" = None) -> "bool | None":
    """True or False when CLOSE_ENV holds a recognised value; None when it is unset or unknown."""
    return _recognised_env_bool(CLOSE_ENV, env)


def automatic_installs_forced_off(env: "Mapping[str, str] | None" = None) -> bool:
    """True when AUTOINSTALL_ENV holds a recognised off value (0, false, no, off).

    Only automatic installs are blocked by it: the launch hook and the first-run
    Install answer. The explicit tray item and the CLI still run.
    """
    return autoinstall_env_override(env) is False


def effective_autoinstall(settings: "Mapping[str, Any] | None", env: "Mapping[str, str] | None" = None) -> bool:
    """Whether auto-install may run. It needs a recorded first-run decision.

    A recognised AUTOINSTALL_ENV value overrides the saved autoinstall choice in
    both directions. An undecided install never runs, even when the env var says on.
    """
    current = _valid_settings(settings) or dict(_DEFAULT_SETTINGS)
    if not current["decided"]:
        return False
    override = autoinstall_env_override(env)
    return current["autoinstall"] if override is None else override


def close_zotero_env_disables(env: "Mapping[str, str] | None" = None) -> bool:
    """True when CLOSE_ENV holds a recognised off value. That is the only thing CLOSE_ENV can do."""
    return close_zotero_env_override(env) is False


def effective_close_zotero(settings: "Mapping[str, Any] | None", env: "Mapping[str, str] | None" = None) -> bool:
    """Whether an install may close a running Zotero: the saved choice, which CLOSE_ENV can only turn off.

    A recognised on value (1, true, yes, on) changes nothing, because closing never turns on against a saved off.
    """
    if close_zotero_env_disables(env):
        return False
    return saved_close_zotero(settings)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_manual_steps(xpi: Path) -> None:
    for line in _MANUAL_STEPS:
        print(line.format(xpi=xpi))


def _restart_sentence(restart: Any) -> str:
    """What happened to the Zotero that Meridian closed for this run. Printed after every outcome that closed it."""
    if restart in ("started", "already_running"):
        return "Meridian closed Zotero and restarted it."
    return "Meridian closed Zotero, but could not restart it. Start Zotero yourself."


def _closed_unconfirmed_cli_text(restart: Any) -> str:
    """What the CLI says when Meridian closed Zotero but could not confirm it was closed. Names the restart. Pure."""
    if restart in ("started", "already_running"):
        return (
            "Meridian closed Zotero and restarted it, but could not confirm that Zotero is closed, so nothing was "
            "installed. Close Zotero, then run this command again."
        )
    return (
        "Meridian closed Zotero, but could not restart it, and could not confirm that Zotero is closed, so nothing "
        "was installed. Make sure Zotero is closed, then run this command again."
    )


_REPAIR_LABELS = {
    "scan_pref": "the scan line in user.js",
    "api_token": "the local write API token",
    "api_prefs": "the local write API settings in user.js",
}


def _print_repairs(result: Mapping[str, Any]) -> None:
    """Name the profile repairs this run made, if it made any. Printed after an unchanged plugin."""
    labels = [_REPAIR_LABELS[key] for key in result.get("repaired") or [] if key in _REPAIR_LABELS]
    if labels:
        print("Repaired in this profile: " + ", ".join(labels) + ".")


def _print_api_state(result: Mapping[str, Any]) -> None:
    """Say when the local write API is not listening, so the user knows Zotero must start (or restart) to use it."""
    if result.get("api_listening") is False:
        print(
            "The local write API (127.0.0.1:23125) is not running yet. It starts when Zotero starts, so if Zotero "
            "is already open, restart it."
        )


def _print_install_status(result: Mapping[str, Any], xpi: Path, profile: Path) -> int:
    """Print the install's own outcome line. Returns the exit code."""
    status = result["status"]
    if status == "installed":
        print(f"Installed the Zotero bridge plugin to {result['path']}.")
        _print_api_state(result)
        return 0
    if status == "unchanged":
        print(f"The Zotero bridge plugin is already up to date at {result['path']}.")
        _print_repairs(result)
        _print_api_state(result)
        return 0
    if status == "refused_not_profile":
        print(result.get("error", _NOT_PROFILE_MESSAGE.format(profile=profile)))
        return 1
    if status == "refused_running":
        print(result.get("message", REFUSED_RUNNING_MESSAGE))
        return 1
    if status == "close_failed":
        print(CLOSE_FAILED_MESSAGE)
        return 1
    if status == "check_failed":
        print(
            "Could not check whether Zotero is running. Make sure Zotero is closed, "
            "then run this command again. Nothing was written to the Zotero profile."
        )
        return 1
    if status == NEEDS_SIGNATURE_STATUS:
        print(result.get("message", needs_signature_message(xpi)))
        return 1
    if status == INTEGRITY_STATUS:
        print(result.get("message", INTEGRITY_FAILED_MESSAGE))
        return 1
    if status == VERSION_USED_STATUS:
        print(result.get("message", VERSION_USED_MESSAGE))
        return 1
    if status == RECORD_UNREADABLE_STATUS:
        print(result.get("message", RECORD_UNREADABLE_MESSAGE))
        return 1
    if status == "no_profile":
        print(f"No Zotero profile folder exists at {profile}, so nothing was installed.")
        _print_manual_steps(xpi)
        return 1
    print(f"The install failed: {result.get('error', 'unknown error')}")
    return 1


def _local_writes_after_install(profile: "Path | str", enabled: bool) -> dict[str, Any]:
    """Set the bridge's open local writes pref in the profile's user.js for an install that asked for it. Never raises.

    The result is ``changed`` or ``unchanged`` with the state before, or ``failed`` with the error.
    """
    try:
        change = zotero_local_writes.set_local_writes(profile, enabled)
    except (OSError, ValueError) as exc:
        return {"status": "failed", "error": str(exc)}
    return {"status": "changed" if change["changed"] else "unchanged", "before": change["state"]}


def _with_local_writes(
    install: "Callable[[], Mapping[str, Any]]", profile: "Path | str", enabled: "bool | None",
) -> "Callable[[], Mapping[str, Any]]":
    """The install step, then the local-writes pref when the install succeeded and the caller asked for it.

    The pref is written while Zotero is closed, before any Zotero start in this run, so that start reads it. With
    ``enabled=None`` the install step is returned as it was, and nothing changes.
    """
    if enabled is None:
        return install

    def _install_then_local_writes() -> dict[str, Any]:
        result = dict(install())
        if result.get("status") in ("installed", "unchanged"):
            result["local_writes"] = _local_writes_after_install(profile, enabled)
        return result

    return _install_then_local_writes


def _print_local_writes(result: Mapping[str, Any], enabled: "bool | None", code: int) -> int:
    """The local-writes line for an install run that asked for it. Returns ``code``, or 1 when it was not written."""
    if enabled is None:
        return code
    local = result.get("local_writes")
    if not isinstance(local, Mapping):
        print("Local writes were not changed, because the plugin was not installed.")
        return code
    if local.get("status") == "failed":
        print(f"Local writes were not set: {local.get('error') or 'unknown error'}")
        return 1
    word = "on" if enabled else "off"
    if local.get("status") == "changed":
        before = zotero_local_writes.state_text(str(local.get("before")))
        print(
            f"Local writes are {word} in user.js. Before, the setting was {before}. "
            "Zotero reads this setting when it starts."
        )
    else:
        print(f"Local writes are already {word} in user.js. Nothing was changed.")
    return code


def _local_writes_plan_text(profile: "Path | None", enabled: bool) -> str:
    """The dry run's line for the local-writes setting. It says what a real run would do and writes nothing."""
    word = "on" if enabled else "off"
    if profile is None:
        return f"Local writes: no profile was found, so {word} would not be set."
    try:
        plan = zotero_local_writes.plan_local_writes(profile, enabled)
    except (OSError, ValueError) as exc:
        return f"Local writes: would not be set. {exc}"
    if plan["changed"]:
        before = zotero_local_writes.state_text(str(plan["state"]))
        return (
            f"Local writes: would set {word} in user.js after a successful install. "
            f"The setting is {before} now."
        )
    return f"Local writes: already {word} in user.js, so nothing would change."


def cli_main(argv: "list[str] | None" = None) -> int:
    """``python -m meridian zotero install-plugin``. Returns 0 on success, 1 otherwise.

    The explicit command installs regardless of the saved auto-install setting. It
    closes a running Zotero first when the saved close choice says so; ``--keep-zotero-open``
    turns that off for this run.
    """
    parser = argparse.ArgumentParser(
        prog="python -m meridian zotero install-plugin",
        description=(
            "Install the Meridian Zotero bridge plugin into Zotero's profile. A running Zotero is "
            "closed first unless the saved setting or --keep-zotero-open says otherwise."
        ),
    )
    parser.add_argument("--profile", type=Path, default=None, help="Zotero profile folder (default: the profile Zotero marks as default).")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan and write nothing.")
    parser.add_argument(
        "--keep-zotero-open",
        action="store_true",
        help="Do not close Zotero for this install, even when the saved setting says to. Nothing is written while Zotero runs.",
    )
    parser.add_argument(
        "--local-writes",
        choices=("on", "off"),
        default=None,
        help="After a successful install, set the Zotero bridge's open local writes setting in the profile's user.js "
        "to on or off. Without this flag the setting is left alone.",
    )
    args = parser.parse_args(argv)
    profile = Path(args.profile) if args.profile is not None else find_default_profile_dir(zotero_data_dir())
    close_zotero = False if args.keep_zotero_open else None
    local_enabled = None if args.local_writes is None else args.local_writes == "on"
    if args.dry_run:
        code = _dry_run(profile, close_zotero=close_zotero)
        if local_enabled is not None:
            print(_local_writes_plan_text(profile, local_enabled))
        return code
    try:
        xpi = build_xpi(default_xpi_path())
    except OSError as exc:
        print(f"The plugin bundle could not be built: {exc}")
        return 1
    if profile is None:
        print("No Zotero profile was found, so nothing was installed.")
        _print_manual_steps(xpi)
        return 1
    # Check the explicit profile before anything closes, so Zotero is never closed for a refused install.
    if not profile.is_dir():
        print(f"No Zotero profile folder exists at {profile}, so nothing was installed.")
        _print_manual_steps(xpi)
        return 1
    if not is_zotero_profile(profile):
        print(_NOT_PROFILE_MESSAGE.format(profile=profile))
        return 1
    # Meridian can only register and enable the plugin in Zotero's default profile, because it starts Zotero itself.
    is_default = _is_default_profile(profile)
    # An unsigned or tampered plugin is refused before anything closes: only the verified signed file is copied.
    stored = _stored_with_fetch()
    refusal = _signed_refusal(stored, xpi)
    if refusal is not None:
        return _print_install_status(refusal, xpi, profile)
    scan = check_scan_pref(profile)
    if scan in (SCAN_CONFLICT, SCAN_UNREADABLE):
        print(_pref_refusal_message(scan))
        return 1
    # A plugin that is already identical needs no close, whether or not Zotero runs. Its profile repairs still run.
    current = _already_current(profile, stored.sha256)
    if current is not None:
        # The local-writes setting is written before the repairs and the enable step, so any Zotero start here reads it.
        local = _local_writes_after_install(profile, local_enabled) if local_enabled is not None else None
        if is_default:
            outcome = _unchanged_outcome(profile, current, None, None, None, None)
        else:
            outcome = _repair_profile(profile, current)
            if outcome.get("status") == "unchanged":
                outcome = _not_default(outcome)
        if local is not None:
            outcome = {**outcome, "local_writes": local}
        code = _print_install_status_and_enable(outcome, xpi, profile)
        return _print_local_writes(outcome, local_enabled, code)
    result = install_with_close(
        load_settings(settings_path()),
        close_zotero=close_zotero,
        install=_with_local_writes(
            lambda: _install_files(profile, stored.path, None, stored.sha256), profile, local_enabled,
        ),
        after_install=(lambda: _register_default(profile, None, None, None, None)) if is_default else None,
    )
    if not is_default and result.get("status") in ("installed", "unchanged"):
        result = _not_default(result)
    if result.get("closed_zotero") and result.get("status") in UNCONFIRMED_CLOSED_STATUSES:
        # Meridian closed Zotero, so the text must not tell the user to close it as if it had not.
        print(_closed_unconfirmed_cli_text(result.get("restart")))
        return _print_local_writes(result, local_enabled, 1)
    code = _print_install_status_and_enable(result, xpi, profile)
    code = _print_local_writes(result, local_enabled, code)
    if result.get("closed_zotero"):
        print(_restart_sentence(result.get("restart")))
    return code


NOT_DEFAULT_PROFILE_MESSAGE = (
    "The plugin file and its scan setting are in this profile. Meridian enables the plugin only in Zotero's default "
    "profile, so start Zotero with this profile once, then enable the plugin in Tools > Plugins."
)


def _is_default_profile(profile: "Path | None") -> bool:
    """True when ``profile`` is the profile Zotero marks as default. Never raises."""
    try:
        default = find_default_profile_dir(zotero_data_dir())
        return profile is not None and default is not None and _profile_key(profile) == _profile_key(default)
    except Exception:  # noqa: BLE001 -- an unknown default is not the default
        return False


def _not_default(result: Mapping[str, Any]) -> dict[str, Any]:
    """The result for a profile that is not the default. Nothing is enabled and nothing is written here. Never raises."""
    return {**result, "enable": {"status": "not_default_profile", "message": NOT_DEFAULT_PROFILE_MESSAGE}}


def _print_install_status_and_enable(result: Mapping[str, Any], xpi: Path, profile: Path) -> int:
    """The install's status line, then the enable line when there is one. Returns the exit code."""
    code = _print_install_status(result, xpi, profile)
    enable = result.get("enable")
    if isinstance(enable, Mapping):
        message = _ENABLE_OK_MESSAGES.get(str(enable.get("status"))) or enable.get("message")
        if message:
            print(message)
        if str(enable.get("status")) in ("register_timeout", "close_failed", "not_registered", "registry_unreadable", "failed", "blocked"):
            code = 1
    if result.get("scan_pref_error"):
        print(f"The scan setting could not be written to user.js: {result['scan_pref_error']}")
        code = 1
    return code


def _dry_run(profile: "Path | None", *, close_zotero: "bool | None" = None) -> int:
    print("Dry run: nothing will be written.")
    print(f"Zotero profile: {profile if profile is not None else 'not found'}")
    try:
        payload = build_xpi_bytes()
    except OSError as exc:
        print(f"Plugin bundle: cannot be built ({exc})")
        return 0
    print(
        f"Plugin bundle: {XPI_FILENAME}, {len(payload)} bytes, "
        f"sha256 {hashlib.sha256(payload).hexdigest()}"
    )
    if profile is None:
        print("No Zotero profile was found, so a real install would stop here. Nothing is written by a dry run.")
        return 0
    if not is_zotero_profile(profile):
        print("Would refuse: that folder is not a Zotero profile (no prefs.js or zotero.sqlite).")
        return 0
    stored = signed_install_state()
    blocked = _blocked_refusal(stored)
    if blocked is not None:
        print(f"Would refuse. {blocked['message']}")
        return 0
    if not stored.signed:
        # The unsigned file is only described here: a dry run writes nothing, so it does not exist yet.
        print(
            "This plugin version is not signed yet, so a real install would refuse it. The unsigned file "
            f"would be written to {default_xpi_path()} first. A dry run writes nothing."
        )
        return 0
    print(f"Signed artifact: {stored.path}")
    state = zotero_process_state()
    if state is None:
        print("Would refuse: could not check whether Zotero is running. Make sure Zotero is closed first.")
        return 0
    if state:
        may_close = close_zotero if close_zotero is not None else effective_close_zotero(load_settings(settings_path()))
        if may_close:
            print("Would close Zotero, install the plugin, and restart Zotero if it closed it.")
        else:
            print("Would refuse: Zotero is running. Close Zotero first.")
        return 0
    target = profile / "extensions" / XPI_FILENAME
    if _already_current(profile, stored.sha256) is not None:
        print(
            f"Would leave the installed plugin unchanged at {target}, and repair any missing scan line or API "
            "settings in user.js."
        )
    else:
        print(f"Would install to {target}.")
    return 0
