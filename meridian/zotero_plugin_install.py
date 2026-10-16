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
  ``extensions.json``, ``prefs.js`` or any other profile file.
  ``install_default_profile`` runs that install for the default profile named in
  ``profiles.ini``, with no close step. It never raises.
* ``close_zotero_gracefully`` asks a running Zotero to close with
  ``taskkill /IM zotero.exe`` (never ``/F`` or ``/T``) and waits for it to stop.
  The taskkill argument list has no force flag, and a close that fails leaves
  Zotero as it was.
* ``install_default_with_close`` is the tray's install. It finds the default
  profile and builds the bundle first, in the same order as the CLI, and only
  then calls ``install_with_close``. A missing profile or a bundle that will not
  build is refused before anything closes.
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
* whether Zotero loads an unsigned .xpi that is dropped into ``extensions/``
  without an ``extensions.json`` entry. This module relies on that load path
  and does not verify it.
* whether ``taskkill /IM zotero.exe`` (a close request, not a force kill) makes
  Zotero exit within the 20-second wait. A Zotero that is busy or shows a dialog
  will not close, and the install then reports ``close_failed``.

CLI: ``python -m meridian zotero install-plugin [--profile DIR] [--dry-run] [--keep-zotero-open]``.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Mapping

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
_RUNTIME_DIRS = ("content", "lib")
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
_CHECK_FAILED_MESSAGE = "Could not check whether Zotero is running. Nothing was written."
REFUSED_RUNNING_MESSAGE = "Zotero is running. Close Zotero, then run this command again. Nothing was written."
CLOSE_FAILED_MESSAGE = (
    "Zotero did not close, so nothing was installed. Close Zotero yourself, then try the install again. "
    "Nothing was written."
)
_CLOSE_POLL_SECONDS = 0.5
_CLOSE_TIMEOUT_SECONDS = 20.0
_TASKKILL_ARGV = ("taskkill", "/IM", "zotero.exe")

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

_MANUAL_STEPS = (
    "Install the plugin by hand:",
    "  1. Close Zotero.",
    "  2. In Zotero, open Tools > Add-ons, click the gear icon, and choose Install Add-on From File.",
    "  3. Select: {xpi}",
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


def install_into_profile(
    profile_dir: "Path | str | None",
    xpi_path: "Path | str",
    *,
    running: "Callable[[], bool] | None" = None,
) -> dict[str, Any]:
    """Copy the built .xpi into ``profile_dir/extensions/``.

    ``status`` is one of ``installed``, ``unchanged``, ``refused_not_profile``,
    ``refused_running``, ``check_failed``, ``no_profile`` or ``failed``. The
    profile must hold a marker file (``prefs.js`` or ``zotero.sqlite``), and the
    process check must succeed and find Zotero closed. Every refusal happens
    before any write. ``running`` returns True, False, or None for a failed
    check. It defaults to :func:`zotero_process_state`, looked up at call time.
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
        if target.is_file() and target.read_bytes() == payload:
            return _result("unchanged", target)
        _atomic_write_bytes(target, payload)
        return _result("installed", target)
    except Exception as exc:  # noqa: BLE001 -- report every failure as data, never raise
        return _result("failed", None, error=str(exc))


def _prepare_default_install() -> "tuple[Path, Path] | dict[str, Any]":
    """The default profile and the built bundle, resolved before anything is closed. Never raises.

    Returns ``(profile, xpi)`` when both are ready. Otherwise it returns the refusal to hand back
    unchanged: ``no_profile`` when there is no default profile, or ``failed`` when the bundle will
    not build. The bundle is written into Meridian's own state folder, never into Zotero.
    """
    try:
        profile = find_default_profile_dir(zotero_data_dir())
        if profile is None:
            return _result("no_profile", None)
        return profile, build_xpi(default_xpi_path())
    except Exception as exc:  # noqa: BLE001 -- report every failure as data, never raise
        return _result("failed", None, error=str(exc))


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
    profile, xpi = prepared
    return install_into_profile(profile, xpi, running=running)


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
) -> dict[str, Any]:
    """Install the plugin, closing Zotero first when the choice allows it. Never raises.

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
        return _install_outcome(install, closed=False)
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
        return _install_outcome(install, closed=False)
    result = _install_outcome(install, closed=True)
    result["restart"] = _restart_after_close(restart)
    return result


def install_default_with_close(
    settings: "Mapping[str, Any] | None",
    *,
    close_zotero: "bool | None" = None,
    running: "Callable[[], bool | None] | None" = None,
    close: "Callable[..., str] | None" = None,
    restart: "Callable[[], Any] | None" = None,
    env: "Mapping[str, str] | None" = None,
) -> dict[str, Any]:
    """The tray's install: the default profile and the bundle first, then :func:`install_with_close`. Never raises.

    The profile is found and the bundle is built before anything closes, as the CLI does it. A missing
    default profile, or a bundle that will not build, is refused and Zotero is left alone.
    """
    prepared = _prepare_default_install()
    if isinstance(prepared, dict):
        return {**prepared, "closed_zotero": False, "restart": None}
    profile, xpi = prepared
    check = running if running is not None else zotero_process_state
    return install_with_close(
        settings,
        install=lambda: install_into_profile(profile, xpi, running=check),
        close_zotero=close_zotero,
        running=check,
        close=close,
        restart=restart,
        env=env,
    )


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


def _print_install_status(result: Mapping[str, Any], xpi: Path, profile: Path) -> int:
    """Print the install's own outcome line. Returns the exit code."""
    status = result["status"]
    if status == "installed":
        print(f"Installed the Zotero bridge plugin to {result['path']}.")
        return 0
    if status == "unchanged":
        print(f"The Zotero bridge plugin is already up to date at {result['path']}.")
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
            "then run this command again. Nothing was written."
        )
        return 1
    if status == "no_profile":
        print(f"No Zotero profile folder exists at {profile}, so nothing was installed.")
        _print_manual_steps(xpi)
        return 1
    print(f"The install failed: {result.get('error', 'unknown error')}")
    return 1


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
    args = parser.parse_args(argv)
    profile = Path(args.profile) if args.profile is not None else find_default_profile_dir(zotero_data_dir())
    close_zotero = False if args.keep_zotero_open else None
    if args.dry_run:
        return _dry_run(profile, close_zotero=close_zotero)
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
    result = install_with_close(
        load_settings(settings_path()),
        close_zotero=close_zotero,
        install=lambda: install_into_profile(profile, xpi),
    )
    code = _print_install_status(result, xpi, profile)
    if result.get("closed_zotero"):
        print(_restart_sentence(result.get("restart")))
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
        _print_manual_steps(default_xpi_path())
        return 0
    if not is_zotero_profile(profile):
        print("Would refuse: that folder is not a Zotero profile (no prefs.js or zotero.sqlite).")
        return 0
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
    current = target.read_bytes() if target.is_file() else None
    if current == payload:
        print(f"Would leave the installed plugin unchanged at {target}.")
    else:
        print(f"Would install to {target}.")
    return 0
