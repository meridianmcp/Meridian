"""One-time install of the Meridian Zotero bridge plugin into Zotero's profile.

* ``build_xpi`` packs ``extensions/zotero-bridge`` into a deterministic .xpi:
  sorted entries, fixed zip timestamps, fixed file mode, so rebuilds are
  byte-identical on every host.
* ``install_into_profile`` copies that .xpi into the profile's ``extensions/``
  folder. It refuses while Zotero is running and writes nothing then. It skips
  the write when the bytes already match. It never touches ``extensions.json``,
  ``prefs.js`` or any other profile file.
* The settings file ``zotero_plugin.json`` (in the per-user Meridian state dir,
  next to the tray's other settings) holds the user's choice. Auto-install is
  opt-out: it defaults on, and the first-run prompt records the answer.

UNVERIFIED on a real machine:

* the default Zotero data-root paths in ``zotero_data_dir`` (Windows
  ``%APPDATA%\\Zotero\\Zotero``, macOS ``~/Library/Application Support/Zotero``,
  Linux ``~/.zotero/zotero``). ``MERIDIAN_ZOTERO_DATA_DIR`` overrides them.
* whether Zotero loads an unsigned .xpi that is dropped into ``extensions/``
  without an ``extensions.json`` entry. This module relies on that load path
  and does not verify it.

CLI: ``python -m meridian zotero install-plugin [--profile DIR] [--dry-run]``.
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
import zipfile
from pathlib import Path
from typing import Any, Callable, Mapping

XPI_FILENAME = "meridian-zotero@usemeridian.us.xpi"
SETTINGS_FILENAME = "zotero_plugin.json"
ZOTERO_DATA_DIR_ENV = "MERIDIAN_ZOTERO_DATA_DIR"
AUTOINSTALL_ENV = "MERIDIAN_ZOTERO_PLUGIN_AUTOINSTALL"

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

_DEFAULT_SETTINGS = {"autoinstall": True, "decided": False}
_DEFAULT_STATE_SUBDIR = "zotero_plugin"

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


def _resolve_source(source_dir: "Path | str | None") -> Path:
    return Path(source_dir) if source_dir is not None else _SOURCE_DIR


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


def build_xpi_bytes(source_dir: "Path | str | None" = None) -> bytes:
    """The deterministic .xpi bytes for the bridge source, built in memory."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, path in _runtime_files(_resolve_source(source_dir)):
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = _ZIP_CREATE_SYSTEM_UNIX
            info.external_attr = _ZIP_FILE_MODE << 16
            archive.writestr(info, path.read_bytes(), compresslevel=9)
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
        timeout=10,
        check=False,
        **kwargs,
    )
    return completed.stdout or ""


def _run_pgrep() -> str:
    completed = subprocess.run(
        ["pgrep", "-x", "zotero"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return completed.stdout or ""


def _tasklist_lists_zotero(output: str) -> bool:
    for line in output.splitlines():
        fields = line.split()
        if fields and fields[0].casefold() == "zotero.exe":
            return True
    return False


def zotero_is_running(runner: "Callable[[], str] | None" = None) -> bool:
    """True when a Zotero process is running. Never raises.

    ``runner`` returns the raw listing text (tasklist on Windows, pgrep
    elsewhere) so tests never call the real process list. Any error counts as
    running, so an install is never attempted on an unknown state.
    """
    try:
        if _platform() == "win32":
            return _tasklist_lists_zotero(str((runner or _run_tasklist)() or ""))
        output = str((runner or _run_pgrep)() or "")
        return any(line.strip().isdigit() for line in output.splitlines())
    except Exception:
        return True


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


def install_into_profile(
    profile_dir: "Path | str | None",
    xpi_path: "Path | str",
    *,
    running: "Callable[[], bool] | None" = None,
) -> dict[str, Any]:
    """Copy the built .xpi into ``profile_dir/extensions/``.

    ``status`` is one of ``installed``, ``unchanged``, ``refused_running``,
    ``no_profile`` or ``failed``. Refusing while Zotero runs happens before any
    write. ``running`` defaults to :func:`zotero_is_running`, looked up at call
    time.
    """
    if profile_dir is None or not Path(profile_dir).is_dir():
        return _result("no_profile", None)
    try:
        check = running if running is not None else zotero_is_running
        if check():
            return _result("refused_running", None)
        target = Path(profile_dir) / "extensions" / XPI_FILENAME
        payload = Path(xpi_path).read_bytes()
        if target.is_file() and target.read_bytes() == payload:
            return _result("unchanged", target)
        _atomic_write_bytes(target, payload)
    except Exception as exc:  # noqa: BLE001 -- report every failure as data, never raise
        return _result("failed", None, error=str(exc))
    return _result("installed", target)


# ---------------------------------------------------------------------------
# Settings and the first-run choice
# ---------------------------------------------------------------------------


def settings_path() -> Path:
    """``zotero_plugin.json`` in the per-user Meridian state dir."""
    from meridian.local_runner import default_state_dir

    return default_state_dir() / SETTINGS_FILENAME


def _normalize(payload: Mapping[str, Any]) -> dict[str, bool]:
    autoinstall = payload.get("autoinstall")
    decided = payload.get("decided")
    return {
        "autoinstall": autoinstall if isinstance(autoinstall, bool) else _DEFAULT_SETTINGS["autoinstall"],
        "decided": decided if isinstance(decided, bool) else _DEFAULT_SETTINGS["decided"],
    }


def load_settings(path: "Path | str") -> dict[str, bool]:
    """The saved settings, or the defaults when the file is missing or unreadable."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(_DEFAULT_SETTINGS)
    if not isinstance(payload, dict):
        return dict(_DEFAULT_SETTINGS)
    return _normalize(payload)


def save_settings(path: "Path | str", settings: Mapping[str, Any]) -> None:
    """Atomically persist the two settings keys."""
    text = json.dumps(_normalize(settings), sort_keys=True) + "\n"
    _atomic_write_bytes(Path(path), text.encode("utf-8"))


def apply_first_run_choice(settings: Mapping[str, Any], install: bool) -> dict[str, bool]:
    """A new settings dict: the answer is recorded and ``decided`` becomes true."""
    return {"autoinstall": bool(install), "decided": True}


def effective_autoinstall(settings: "Mapping[str, Any] | None", env: "Mapping[str, str] | None" = None) -> bool:
    """Whether auto-install runs. The env var wins when it holds a recognised value."""
    source = os.environ if env is None else env
    raw = str(source.get(AUTOINSTALL_ENV) or "").strip().casefold()
    if raw in _FALSE_VALUES:
        return False
    if raw in _TRUE_VALUES:
        return True
    return _normalize(settings or {})["autoinstall"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_manual_steps(xpi: Path) -> None:
    for line in _MANUAL_STEPS:
        print(line.format(xpi=xpi))


def cli_main(argv: "list[str] | None" = None) -> int:
    """``python -m meridian zotero install-plugin``. Returns 0 on success, 1 otherwise.

    The explicit command installs regardless of the saved auto-install setting.
    """
    parser = argparse.ArgumentParser(
        prog="python -m meridian zotero install-plugin",
        description="Install the Meridian Zotero bridge plugin into Zotero's profile (Zotero must be closed).",
    )
    parser.add_argument("--profile", type=Path, default=None, help="Zotero profile folder (default: the profile Zotero marks as default).")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan and write nothing.")
    args = parser.parse_args(argv)
    profile = Path(args.profile) if args.profile is not None else find_default_profile_dir(zotero_data_dir())
    if args.dry_run:
        return _dry_run(profile)
    try:
        xpi = build_xpi(default_xpi_path())
    except OSError as exc:
        print(f"The plugin bundle could not be built: {exc}")
        return 1
    if profile is None:
        print("No Zotero profile was found, so nothing was installed.")
        _print_manual_steps(xpi)
        return 1
    result = install_into_profile(profile, xpi)
    status = result["status"]
    if status == "installed":
        print(f"Installed the Zotero bridge plugin to {result['path']}.")
        return 0
    if status == "unchanged":
        print(f"The Zotero bridge plugin is already up to date at {result['path']}.")
        return 0
    if status == "refused_running":
        print("Zotero is running. Close Zotero, then run this command again. Nothing was written.")
        return 1
    if status == "no_profile":
        print(f"No Zotero profile folder exists at {profile}, so nothing was installed.")
        _print_manual_steps(xpi)
        return 1
    print(f"The install failed: {result.get('error', 'unknown error')}")
    return 1


def _dry_run(profile: "Path | None") -> int:
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
    if zotero_is_running():
        print("Would refuse: Zotero is running. Close Zotero first.")
        return 0
    target = profile / "extensions" / XPI_FILENAME
    current = target.read_bytes() if target.is_file() else None
    if current == payload:
        print(f"Would leave the installed plugin unchanged at {target}.")
    else:
        print(f"Would install to {target}.")
    return 0
