"""Start, probe and autostart the Zotero desktop app for Meridian.

Meridian's Zotero integration talks to Zotero's local HTTP API on
127.0.0.1:23119. That API only answers while the Zotero desktop app is running.
This module is the launcher core behind it:

* find the Zotero executable (env override, then the standard install paths);
* report whether Zotero is running and whether its local API answers;
* start Zotero detached and wait for the API, never killing any process;
* plan, install and remove a Windows Startup entry that runs ``start`` at logon.

Nothing writes to disk or launches a process unless the caller asks for it:
the autostart writes need ``confirm=True`` and ``start`` is an explicit call.
wxPython is not used.

CLI: ``python -m meridian.zotero_launcher {status,start,autostart-plan,
autostart-install,autostart-remove}``.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# Import only the constant. zotero_client pulls in stdlib modules only.
from meridian.zotero_client import _DEFAULT_BASE_URL as _ZOTERO_LOCAL_API
# The probe's redirect rule lives with the connect flow, so the two cannot drift apart.
from meridian import zotero_connect

_EXE_ENV = "MERIDIAN_ZOTERO_EXE"

# Standard Windows install locations, checked in this order after the env override.
_PROGRAM_FILES_CANDIDATES = (
    r"C:\Program Files\Zotero\zotero.exe",
    r"C:\Program Files (x86)\Zotero\zotero.exe",
)

# Any HTTP 200 from this one-item request means the local API is up.
_PROBE_URL = f"{_ZOTERO_LOCAL_API}/users/0/items?limit=1"

def _build_probe_opener(*handlers):
    # An explicit empty ProxyHandler keeps localhost off any configured HTTP proxy. The redirect
    # handler never follows a redirect to the authorize endpoint or to an ambiguous authority.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), zotero_connect._NoAuthorizeRedirect(), *handlers,
    )


_PROBE_OPENER = _build_probe_opener()

_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

_STARTUP_FILENAME = "meridian-zotero.cmd"
_STARTUP_SUBDIR = os.path.join("Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def _is_windows():
    return sys.platform == "win32"


def find_zotero_executable(env=None):
    """Return the path to zotero.exe, or None when it is not found.

    Order: the MERIDIAN_ZOTERO_EXE override (only if it names an existing file),
    the Program Files locations, then %LOCALAPPDATA%\\Zotero\\zotero.exe. Reads
    ``os.environ`` when ``env`` is None.
    """
    source = os.environ if env is None else env
    for candidate in _candidate_exe_paths(source):
        if os.path.isfile(candidate):
            return Path(candidate)
    return None


def _candidate_exe_paths(env):
    candidates = []
    override = str(env.get(_EXE_ENV) or "").strip()
    if override:
        candidates.append(override)
    candidates.extend(_PROGRAM_FILES_CANDIDATES)
    local_app_data = str(env.get("LOCALAPPDATA") or "").strip()
    if local_app_data:
        candidates.append(os.path.join(local_app_data, "Zotero", "zotero.exe"))
    return candidates


def is_zotero_process_running(runner=None):
    """True when tasklist lists a zotero.exe process. Never raises.

    ``runner`` is a zero-argument callable returning tasklist stdout as text, so
    tests can avoid a real process listing. Off Windows this always returns False.
    """
    if not _is_windows():
        return False
    try:
        output = (runner or _run_tasklist)()
        return _tasklist_lists_zotero(str(output or ""))
    except Exception:
        return False


def _run_tasklist():
    completed = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq zotero.exe", "/NH"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        creationflags=_CREATE_NO_WINDOW,
    )
    return completed.stdout or ""


def _tasklist_lists_zotero(output):
    for line in output.splitlines():
        fields = line.split()
        if fields and fields[0].lower() == "zotero.exe":
            return True
    return False


def zotero_api_alive(timeout=2.0, opener=None):
    """True only when Zotero's local API answers HTTP 200. Never raises.

    ``opener`` is called as ``opener(url, timeout=...)`` and must return a
    response object (or raise). The default is a proxy-free urllib opener.
    """
    try:
        open_url = opener if opener is not None else _PROBE_OPENER.open
        response = open_url(_PROBE_URL, timeout=timeout)
        try:
            return _http_status(response) == 200
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    except Exception:
        return False


def _http_status(response):
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        if callable(getcode):
            status = getcode()
    return status


def start_zotero(timeout_s=60.0, poll_s=2.0, launcher=None, sleep=None, clock=None):
    """Start Zotero if needed and wait for its local API to answer.

    Returns ``{"action", "exe", "api_ready"}``. Actions:

    * ``already_running``: the API answered before any launch, so nothing started.
    * ``started``: Zotero was launched and the API came up within ``timeout_s``.
    * ``not_installed``: no executable was found, so nothing was launched.
    * ``timeout``: Zotero was launched but the API did not answer in time.

    Nothing is killed. A failed launch raises OSError to the caller.
    """
    if zotero_api_alive():
        return _start_result("already_running", find_zotero_executable(), True)
    exe = find_zotero_executable()
    if exe is None:
        return _start_result("not_installed", None, False)
    launch = launcher if launcher is not None else _launch_detached
    wait = sleep if sleep is not None else time.sleep
    now = clock if clock is not None else time.monotonic
    launch(exe)
    deadline = now() + float(timeout_s)
    while True:
        if zotero_api_alive():
            return _start_result("started", exe, True)
        if now() >= deadline:
            return _start_result("timeout", exe, False)
        wait(poll_s)


def _start_result(action, exe, api_ready):
    return {
        "action": action,
        "exe": str(exe) if exe is not None else None,
        "api_ready": bool(api_ready),
    }


def _launch_detached(exe):
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if _is_windows():
        kwargs["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(exe)], **kwargs)


def status():
    """Snapshot of install, process and API state. Never raises."""
    exe = find_zotero_executable()
    return {
        "installed": exe is not None,
        "exe": str(exe) if exe is not None else None,
        "process_running": is_zotero_process_running(),
        "api_ready": zotero_api_alive(),
    }


def _default_startup_dir():
    appdata = os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming"
    )
    return os.path.join(appdata, _STARTUP_SUBDIR)


def _startup_file(startup_dir=None):
    folder = startup_dir if startup_dir is not None else _default_startup_dir()
    return os.path.join(str(folder), _STARTUP_FILENAME)


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cmd_escape(text):
    # A literal percent sign must be doubled inside a .cmd file.
    return str(text).replace("%", "%%")


def _autostart_content(python_exe, repo_dir):
    lines = [
        "@echo off",
        "rem Starts the Zotero desktop app for Meridian at logon.",
        "rem Written by meridian.zotero_launcher. Remove with autostart-remove.",
        f'cd /d "{_cmd_escape(repo_dir)}"',
        f'start "" /min "{_cmd_escape(python_exe)}" -m meridian.zotero_launcher start',
        "",
    ]
    return "\r\n".join(lines)


def autostart_plan(startup_dir=None, python_exe=None, repo_dir=None):
    """Describe the Startup entry without writing anything.

    Returns ``{"path", "content"}``. Defaults: the per-user Startup folder, the
    running interpreter, and the repository root that contains ``meridian/``.
    """
    return {
        "path": _startup_file(startup_dir),
        "content": _autostart_content(
            python_exe or sys.executable,
            repo_dir or _repo_root(),
        ),
    }


def autostart_install(confirm, startup_dir=None, python_exe=None, repo_dir=None):
    """Write the Startup entry, but only when ``confirm`` is True.

    Returns the plan plus ``"written"``, which is False unless the file was written.
    """
    plan = autostart_plan(startup_dir, python_exe, repo_dir)
    if confirm is not True:
        return {**plan, "written": False}
    target = Path(plan["path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(plan["content"], encoding="utf-8", newline="")
    return {**plan, "written": True}


def autostart_remove(confirm, startup_dir=None):
    """Delete the Startup entry, but only when ``confirm`` is True.

    Returns ``{"path", "existed", "removed"}``.
    """
    target = Path(_startup_file(startup_dir))
    existed = target.is_file()
    if confirm is True and existed:
        target.unlink()
        return {"path": str(target), "existed": True, "removed": True}
    return {"path": str(target), "existed": existed, "removed": False}


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m meridian.zotero_launcher",
        description="Start, probe and autostart the Zotero desktop app for Meridian.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Print install, process and API state as JSON.")
    start = sub.add_parser("start", help="Start Zotero if needed and wait for its API.")
    start.add_argument("--timeout", type=float, default=60.0, help="Seconds to wait for the API.")
    sub.add_parser("autostart-plan", help="Print the Startup entry plan without writing it.")
    install = sub.add_parser("autostart-install", help="Write the Startup entry.")
    install.add_argument("--yes", action="store_true", help="Actually write the file.")
    remove = sub.add_parser("autostart-remove", help="Delete the Startup entry.")
    remove.add_argument("--yes", action="store_true", help="Actually delete the file.")
    return parser


def _print_json(payload):
    print(json.dumps(payload, indent=2))


def _run_start(timeout_s):
    try:
        result = start_zotero(timeout_s=timeout_s)
    except OSError as exc:
        _print_json({"action": "error", "exe": None, "api_ready": False, "error": str(exc)})
        return 1
    _print_json(result)
    return 0 if result["action"] in ("already_running", "started") else 1


def main(argv=None):
    """CLI entry point. Returns 0 on success, 1 when Zotero is missing or the start timed out."""
    args = _build_parser().parse_args(argv)
    if args.command == "status":
        _print_json(status())
        return 0
    if args.command == "start":
        return _run_start(args.timeout)
    if args.command == "autostart-plan":
        _print_json(autostart_plan())
        return 0
    if args.command == "autostart-install":
        _print_json(autostart_install(confirm=args.yes))
        return 0
    # Only autostart-remove is left among the required subcommands.
    _print_json(autostart_remove(confirm=args.yes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
