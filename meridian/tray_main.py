"""4e4c3817 -- Windows tray/GUI installer: ``meridian-tray.exe``.

Per pinned decision bce15b67 (which reconciles and supersedes the since-
deleted decision 6358f17f's original 5-file spec): a system-tray wrapper
around the already-built-and-tested :mod:`meridian.local_runner` primitive
(item 899936dd) -- ``LocalRunner`` already solves "supervise one child
process, don't double-spawn, recover from a stale PID, bound the log/output"
in general; this module's only job is the thin tray UI on top of it, wired
to the real Meridian HTTP server. Ships UNSIGNED per decision 8460f167 --
full id 8460f167-55fe-4130-ae10-5b8416781f71, "Code signing: skip-or-cheap-
DIY for launch, defer subscription/EV until revenue" -- re-verified live via
get_pinned_decisions 2026-09-28/29 (status=active; genuinely exists in
Meridian's decision store even though no DECISIONS.md file exists in this
checkout to grep against -- decisions here live in that store, not a
committed markdown file). Signing is deferred until money/time allow --
Microsoft killed the instant SmartScreen reputation win for signed binaries
in March 2024, so this is not a launch blocker.

Two-mode single binary, no separate "full server" exe needed
--------------------------------------------------------------
``meridian.spec`` (the existing downloadable ``meridian.exe``) is
deliberately the SLIM tunnel-only client -- it excludes tkinter, PIL, and the
entire FastAPI/uvicorn/psycopg server stack (see that spec's own docstring).
A tray app needs the opposite: it IS the GUI, and it needs to supervise the
REAL HTTP server as a child process. Rather than building and shipping a
second, separate "full server" binary, this module supports two run modes
from the ONE ``meridian-tray.exe``:

* No flag (normal double-click / Start Menu launch): show the tray icon and
  use ``LocalRunner`` to spawn the Meridian HTTP server as a CHILD process.
* ``--run-server`` (internal only -- never for a human to type): skip the
  tray UI entirely and just dispatch straight into
  ``meridian.__main__.main()`` with ``MERIDIAN_FROZEN_MODE=server`` set, so
  a frozen build (which would otherwise default to the tunnel client per
  ``meridian.__main__._frozen_default_to_tunnel``) runs the actual dashboard
  HTTP server instead. See :func:`_server_command` for exactly how/when
  each path is used -- running from source (``python -m meridian.tray_main``)
  never needs this flag at all; it re-invokes the existing, already-correct
  ``python -m meridian`` entry point directly.

Known, documented limitation (not hidden): this module does not enforce
single-instance-of-the-tray-icon-itself (only ``LocalRunner`` prevents a
SECOND SERVER child from spawning -- a caught, handled
``RunnerAlreadyRunningError`` on startup means "attach to the one already
running", not a crash). Launching ``meridian-tray.exe`` twice shows two tray
icons pointed at the same underlying server rather than refusing the second
launch outright. A real OS-level single-instance mutex is a reasonable
follow-up, not required for a working v1.
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

# This module is also PyInstaller's Analysis entry-point script
# (meridian-tray.spec: Analysis(['meridian/tray_main.py'], ...)), which runs
# it as __main__ with __package__ unset -- the exact "attempted relative
# import with no known parent package" failure meridian/__main__.py's own
# top-of-file comment already documents and fixes for the identical reason
# (confirmed live: the frozen meridian-tray.exe crashed on this before this
# fix was added). Mirrors that fix verbatim rather than inventing a second
# one.
if __package__ is None or __package__ == "":
    _pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _pkg_dir not in sys.path:
        sys.path.insert(0, _pkg_dir)
    __package__ = "meridian"

from .local_runner import (
    ChildState,
    LocalMcpState,
    LocalRunner,
    RunnerAlreadyRunningError,
)
from .zotero_setup import ZoteroSetupError, run_zotero_setup_dialog

_logger = logging.getLogger(__name__)

SCOPE = "meridian-tray"
_RUN_SERVER_FLAG = "--run-server"
_CONFIGURE_ZOTERO_FLAG = "--configure-zotero"
_HEALTH_PROBE_TIMEOUT_SECONDS = 1.5


def _default_port() -> int:
    return int(os.environ.get("MERIDIAN_PORT", "7878"))


def _dashboard_url() -> str:
    return f"http://127.0.0.1:{_default_port()}/"


def _pid_owns_listening_port(pid: int, port: int) -> bool:
    """True iff *pid* is CONFIRMED to itself hold a LISTENING socket on
    *port* right now (2026-09-28 review finding #13). A bare "HTTP 200 on
    127.0.0.1:port/health" has no identity binding at all on its own -- a
    completely unrelated local process that happens to win the race to bind
    that port first is indistinguishable from the real server without this
    check. Degrades to True ("can't verify, don't block on it") when psutil
    is unavailable or *pid* has already exited by the time this runs -- this
    only ever NARROWS an already-successful HTTP response, never invents a
    failure the response itself didn't report."""
    try:
        import psutil  # type: ignore

        proc = psutil.Process(pid)
        # net_connections() is the modern (psutil>=6.0) name; connections()
        # is the same call under its older, now-deprecated name -- this
        # repo's own pin (psutil>=5.9) spans both, so try the modern one
        # first and fall back rather than assuming either is present.
        if hasattr(proc, "net_connections"):
            conns = proc.net_connections(kind="inet")
        else:
            conns = proc.connections(kind="inet")
    except Exception:  # noqa: BLE001
        return True
    return any(
        getattr(c, "status", None) == psutil.CONN_LISTEN
        and c.laddr and getattr(c.laddr, "port", None) == port
        for c in conns
    )


def _expected_pid(runner: LocalRunner) -> "int | None":
    """The PID :func:`_health_probe` should cross-check the ``/health``
    responder against -- the CURRENTLY relevant child for *runner*'s scope.
    Prefers the live in-process handle (set the instant ``_spawn()``
    returns, well before the health-probe polling loop ever starts -- see
    ``LocalRunner._spawn_and_record``) since it is always freshest; falls
    back to the persisted record's pid for a read-only, status()-triggered
    re-probe (see ``LocalRunner._build_local_mcp_status``'s stale-state
    self-heal, 2026-09-28 review item #1c) where there may be no live
    in-process handle at all. Returns ``None`` (probe degrades to the
    HTTP-only check) when neither is available."""
    live = runner._live_handle
    if live is not None:
        return live.pid
    record = runner._load_record()
    return record.pid if record is not None else None


def _health_probe(expected_pid: "int | None" = None) -> bool:
    """LocalRunner's ``health_probe`` callable: a real HTTP GET against the
    server's own ``/health`` route (not a guess, not a bare port-open check
    -- a closed port never means "ready" and an open-but-not-yet-serving
    port never falsely reports ready either). Any failure means "not ready
    yet", never an exception escaping to ``LocalRunner`` (its own
    ``_await_readiness`` already treats a raising probe as "not ready" too,
    but staying defensive here keeps this callable's own contract explicit).

    *expected_pid*, when supplied, additionally cross-checks (via
    :func:`_pid_owns_listening_port`) that the process actually LISTENING on
    the port is the one LocalRunner spawned -- see that function's own
    docstring for the finding this closes. Optional and defaults to
    ``None`` (the pre-existing HTTP-only behavior) so this stays callable
    standalone exactly as before; :func:`_build_runner` is what wires the
    real cross-check in via a closure over the live runner."""
    url = f"http://127.0.0.1:{_default_port()}/health"
    try:
        with urllib.request.urlopen(url, timeout=_HEALTH_PROBE_TIMEOUT_SECONDS) as resp:
            if not (200 <= resp.status < 300):
                return False
    except (urllib.error.URLError, OSError, ValueError):
        return False
    if expected_pid is None:
        return True
    return _pid_owns_listening_port(expected_pid, _default_port())


def _server_command() -> "list[str]":
    """Command :class:`LocalRunner` spawns as the actual server child.

    Frozen (``meridian-tray.exe``): self-relaunch this SAME exe with the
    internal ``--run-server`` flag -- ``sys.executable`` is the frozen exe's
    own path in that case (PyInstaller convention), so this never depends on
    a separate binary or a Python install being present on the machine.
    Unfrozen (source/dev): the existing, already-correct
    ``python -m meridian`` entry point -- no flag needed, no self-relaunch
    trick required at all, since ``_frozen_default_to_tunnel`` is already a
    no-op when not frozen.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, _RUN_SERVER_FLAG]
    return [sys.executable, "-m", "meridian"]


def _local_cli_command(*args: str) -> list[str]:
    """Build a command for an existing local-only Meridian maintenance tool.

    Reuse the same executable in packaged mode and the current Python
    environment in source mode, so the tray never depends on a second
    Meridian installation or sends local paths to the hosted dashboard.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-m", "meridian", *args]


def _launch_local_cli(*args: str, cwd: str | None = None) -> None:
    """Open one local maintenance command in a visible console when possible."""
    command = _local_cli_command(*args)
    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if sys.platform == "win32" else 0
    try:
        subprocess.Popen(command, cwd=cwd, creationflags=creationflags)
    except OSError as exc:
        _show_error_dialog("Meridian local tool failed to open", str(exc))


def _choose_project_root(title: str, parent: Any | None = None) -> str | None:
    """Ask which local repository a setup or health command should inspect."""
    import tkinter as tk
    from tkinter import filedialog

    owns_root = parent is None
    root = parent if parent is not None else tk.Tk()
    if owns_root:
        root.withdraw()
    try:
        selected = filedialog.askdirectory(parent=root, title=title, mustexist=True)
        return str(selected) if selected else None
    finally:
        if owns_root:
            root.destroy()


def _server_env() -> "dict[str, str]":
    """The child's environment. Must be the FULL parent environment plus our
    additions, never a bare ``{"MERIDIAN_FROZEN_MODE": "server"}`` dict --
    ``subprocess.Popen(env=...)`` REPLACES the environment entirely rather
    than merging, and the child needs PATH/etc. to function at all.

    2026-09-28 review finding #17: when frozen, the ``--run-server`` child
    self-relaunches the SAME onefile exe, which would otherwise independently
    re-extract itself (a second, redundant PyInstaller bootloader
    extraction, fully counted inside ``cold_start_timeout``'s window) even
    though the TRAY process just did the exact same extraction moments ago.
    ``_MEIPASS2`` is PyInstaller's own documented mechanism for exactly this
    self-relaunch case (see PyInstaller's "Sometimes a frozen app needs to
    restart itself" advanced-topics note): a child launched with
    ``_MEIPASS2`` set to an already-extracted onefile directory reuses it
    directly instead of extracting a fresh one. Only set when frozen and a
    real ``sys._MEIPASS`` exists -- a no-op (key simply absent) otherwise."""
    env = dict(os.environ)
    env["MERIDIAN_FROZEN_MODE"] = "server"
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            env["_MEIPASS2"] = str(meipass)
    return env


_DEFAULT_COLD_START_TIMEOUT_SECONDS = 20.0  # matches local_runner's own default explicitly, for clarity here
_FROZEN_COLD_START_TIMEOUT_SECONDS = 30.0  # extra headroom for the frozen path's own import/startup cost


def _cold_start_timeout() -> float:
    """2026-09-28 review finding #17: the frozen ``--run-server`` child's
    own Python import/startup cost (fastapi/uvicorn/psycopg, etc.) is real
    even with ``_MEIPASS2`` reuse eliminating the DOUBLE extraction above --
    give the frozen path a larger default window, and let
    ``MERIDIAN_TRAY_COLD_START_TIMEOUT`` override either path for field
    debugging without a code change. Falls back to the default on a
    missing/invalid override rather than raising."""
    override = os.environ.get("MERIDIAN_TRAY_COLD_START_TIMEOUT", "").strip()
    if override:
        try:
            return float(override)
        except ValueError:
            _logger.warning(
                "tray_main: ignoring invalid MERIDIAN_TRAY_COLD_START_TIMEOUT=%r", override,
            )
    if getattr(sys, "frozen", False):
        return _FROZEN_COLD_START_TIMEOUT_SECONDS
    return _DEFAULT_COLD_START_TIMEOUT_SECONDS


def _icon_image_path() -> Path:
    """Resolve ``meridian-tray.ico`` both frozen (PyInstaller bundles it
    under ``sys._MEIPASS`` per the ``datas=`` entry in meridian-tray.spec)
    and from source (``meridian/static/``).

    Frozen resolution deliberately matches the SAME ``meridian/static/``
    sub-path the unfrozen branch already uses (2026-09-28 review finding
    #23), rather than the bundle root -- meridian-tray.spec's ``datas=``
    only ever copies the WHOLE ``meridian/static`` directory once (needed
    for ``server.py``'s StaticFiles mount, which the icon file rides along
    with for free); there is no second, separate root-level copy of the
    icon to resolve against any more."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "meridian" / "static"
    else:
        base = Path(__file__).resolve().parent / "static"
    return base / "meridian-tray.ico"


def _build_runner() -> LocalRunner:
    runner = LocalRunner(
        scope=SCOPE,
        command=_server_command(),
        env=_server_env(),
        health_probe=None,  # bound to `runner` itself right below
        cold_start_timeout=_cold_start_timeout(),
    )
    # A closure over `runner` (not a bare module-level callable) is what
    # lets _health_probe cross-check the /health responder's identity
    # (2026-09-28 review finding #13) -- LocalRunner's health_probe contract
    # is a plain zero-arg Callable[[], bool], so the expected-pid lookup has
    # to happen HERE, at call time, rather than being passed in once.
    runner.health_probe = lambda: _health_probe(_expected_pid(runner))
    return runner


def _sweep_stale_runtime_extractions() -> None:
    """2026-09-28 review finding #5/#22 (secondary part): best-effort sweep
    of orphaned PyInstaller onefile extraction dirs left behind by a
    forcibly-killed --run-server child (whenever the graceful-CTRL_BREAK
    path in process_lifecycle.py still had to fall back to
    TerminateJobObject). ``meridian-tray.spec`` now pins ``runtime_tmpdir``
    to a FIXED, Meridian-owned directory (instead of the OS-wide default
    temp root) specifically so this sweep can safely delete stale
    ``_MEI*`` siblings without ever touching an unrelated app's temp files.

    A no-op when not frozen (running from source has no ``sys._MEIPASS`` /
    onefile extraction concept at all) and never raises -- a sweep failure
    (e.g. a sibling still locked by a concurrently-running second tray
    instance -- see this module's own "known limitation" docstring note)
    must never prevent the tray itself from starting.
    """
    if not getattr(sys, "frozen", False):
        return
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return
    try:
        current = Path(meipass).resolve()
        runtime_tmpdir = current.parent
        for sibling in runtime_tmpdir.iterdir():
            if sibling == current or not sibling.is_dir():
                continue
            if not sibling.name.startswith("_MEI"):
                continue  # never touch anything this sweep didn't itself create
            try:
                shutil.rmtree(sibling, ignore_errors=True)
            except Exception:  # noqa: BLE001 -- best-effort, one bad sibling must not stop the sweep
                pass
    except Exception:  # noqa: BLE001 -- must never prevent the tray from starting
        _logger.warning("tray_main: stale runtime-extraction sweep failed", exc_info=True)


# ---------------------------------------------------------------------------
# Tkinter dialogs -- each opens its OWN fresh Tk root and tears it down when
# closed, rather than keeping a persistent root alongside pystray's own event
# loop. pystray invokes each menu action in its own dedicated thread, so this
# never collides with a concurrently-open dialog from a different click.
# ---------------------------------------------------------------------------


def _show_status_dialog(runner: LocalRunner, parent: Any | None = None) -> None:
    import tkinter as tk
    from tkinter import messagebox

    status = runner.status()
    lines = [
        f"Child process: {status.child.state.value}",
        f"PID: {status.child.pid or '-'}",
        f"Uptime: {status.child.uptime_seconds:.0f}s" if status.child.uptime_seconds else "Uptime: -",
        f"Dashboard: {status.local_mcp.state.value} -- {status.local_mcp.detail or '(no detail)'}",
    ]
    if status.warnings:
        lines.append("")
        lines.extend(f"Warning: {w}" for w in status.warnings)
    owns_root = parent is None
    root = parent if parent is not None else tk.Tk()
    if owns_root:
        root.withdraw()
    try:
        messagebox.showinfo("Meridian status", "\n".join(lines), parent=root)
    finally:
        if owns_root:
            root.destroy()


def _show_logs_window(runner: LocalRunner, parent: Any | None = None) -> None:
    import tkinter as tk
    from tkinter import scrolledtext

    tail = runner.tail_log() or "(no log output yet)"
    root = tk.Toplevel(parent) if parent is not None else tk.Tk()
    root.title("Meridian -- log tail")
    root.geometry("800x500")
    text = scrolledtext.ScrolledText(root, wrap="word")
    text.insert("1.0", tail)
    text.configure(state="disabled")
    text.pack(fill="both", expand=True)
    if parent is None:
        root.mainloop()


def _show_error_dialog(title: str, message: str, parent: Any | None = None) -> None:
    import tkinter as tk
    from tkinter import messagebox

    owns_root = parent is None
    root = parent if parent is not None else tk.Tk()
    if owns_root:
        root.withdraw()
    try:
        messagebox.showerror(title, message, parent=root)
    finally:
        if owns_root:
            root.destroy()


class _TkUiDispatcher:
    """Run tray-triggered Tk actions from the thread that owns the Tk root."""

    def __init__(self, root: Any) -> None:
        import queue

        self._queue_module = queue
        self._pending = queue.SimpleQueue()
        self._root = root
        root.after(0, self._drain)

    def submit(self, callback: Any) -> None:
        self._pending.put(callback)

    def _drain(self) -> None:
        try:
            while True:
                try:
                    callback = self._pending.get_nowait()
                except self._queue_module.Empty:
                    break
                callback()
        finally:
            # Keep the queue alive after a failed menu action. Tk will still
            # report the callback exception, while later actions remain usable.
            self._root.after(25, self._drain)


# ---------------------------------------------------------------------------
# Tray icon
# ---------------------------------------------------------------------------


def _run_tray() -> int:
    import pystray
    from PIL import Image

    ui_root = None
    ui_dispatcher = None
    darwin_nsapplication = None
    if sys.platform == "darwin":
        import tkinter as tk
        from AppKit import NSApplication

        # Tk and the Darwin status item both need the process main thread. Let
        # Tk own that thread and attach pystray to Tk's shared Cocoa run loop.
        ui_root = tk.Tk()
        ui_root.withdraw()
        ui_dispatcher = _TkUiDispatcher(ui_root)
        darwin_nsapplication = NSApplication.sharedApplication()

    _sweep_stale_runtime_extractions()
    runner = _build_runner()

    # 4e4c3817 follow-up (owner feedback 2026-09-27): a bare tray icon gives
    # zero visible feedback on launch -- a human who just double-clicked this
    # (or hit it via Start Menu/startup) sees literally nothing happen, since
    # a new tray icon is often auto-hidden into Windows' overflow chevron.
    # Open the dashboard immediately so launching it always shows something,
    # instead of requiring the tray icon to be found and clicked first.
    #
    # 2026-09-28 review fix: this must NOT open unconditionally. start()'s
    # RunnerStatus (and, on the attach-to-existing path, a fresh status()
    # call) is checked first -- neither a COLD_START_TIMEOUT/FAILED fresh
    # start nor an "already running" record that turns out to be unhealthy
    # should silently open a browser tab to a dead URL with zero feedback.
    should_open_dashboard = False
    try:
        status = runner.start()
        should_open_dashboard = status.local_mcp.state in (
            LocalMcpState.READY, LocalMcpState.NOT_CONFIGURED,
        )
        if not should_open_dashboard:
            _show_error_dialog(
                "Meridian did not become ready",
                status.local_mcp.detail or f"local MCP state: {status.local_mcp.state.value}",
                parent=ui_root,
            )
    except RunnerAlreadyRunningError:
        # Already running (from a prior launch, or another tray instance) --
        # attach to it rather than treating this as an error. See module
        # docstring's "known limitation" note on multi-tray-icon launches.
        # "Already running" only means the prior record's pid is alive, NOT
        # that the server is actually healthy -- check before opening.
        try:
            should_open_dashboard = runner.status().local_mcp.state in (
                LocalMcpState.READY, LocalMcpState.NOT_CONFIGURED,
            )
        except Exception:  # noqa: BLE001 -- a broken status check must never crash the tray
            should_open_dashboard = False
    except Exception as exc:  # noqa: BLE001 -- must not silently exit with no UI at all
        _show_error_dialog("Meridian failed to start", str(exc), parent=ui_root)
        if ui_root is not None:
            ui_root.destroy()
        return 1

    if not should_open_dashboard and ui_root is not None:
        ui_root.destroy()
        return 1

    if should_open_dashboard:
        try:
            webbrowser.open(_dashboard_url())
        except Exception:  # noqa: BLE001 -- a browser-open failure must never stop the tray/server
            pass

    icon_path = _icon_image_path()
    try:
        image = Image.open(icon_path)
    except (FileNotFoundError, OSError):
        # A missing icon file must never prevent the tray (and the server it
        # supervises) from running -- fall back to a plain solid square
        # rather than crashing the whole app over cosmetics.
        image = Image.new("RGBA", (64, 64), (0, 102, 204, 255))

    def _dispatch_ui(callback: Any) -> None:
        if ui_dispatcher is not None:
            ui_dispatcher.submit(callback)
        else:
            threading.Thread(target=callback, daemon=True).start()

    def _open_dashboard(icon: "pystray.Icon", item: Any) -> None:
        webbrowser.open(_dashboard_url())

    def _show_status(icon: "pystray.Icon", item: Any) -> None:
        _dispatch_ui(lambda: _show_status_dialog(runner, parent=ui_root))

    def _show_logs(icon: "pystray.Icon", item: Any) -> None:
        _dispatch_ui(lambda: _show_logs_window(runner, parent=ui_root))

    def _configure_zotero(icon: "pystray.Icon", item: Any) -> None:
        def _open_setup() -> None:
            try:
                run_zotero_setup_dialog(parent=ui_root)
            except ZoteroSetupError as exc:
                _show_error_dialog("Zotero setup unavailable", str(exc), parent=ui_root)
            except Exception as exc:  # noqa: BLE001 — tray stays alive if the dialog fails
                _show_error_dialog("Zotero setup failed", str(exc), parent=ui_root)

        _dispatch_ui(_open_setup)

    def _configure_local_project(icon: "pystray.Icon", item: Any) -> None:
        def _choose_and_configure() -> None:
            try:
                project_root = _choose_project_root(
                    "Choose a local project to set up", parent=ui_root,
                )
                if project_root:
                    _launch_local_cli("setup", "--repo", project_root, cwd=project_root)
            except Exception as exc:  # noqa: BLE001 -- keep the tray available if the picker fails
                _show_error_dialog("Meridian project setup failed", str(exc), parent=ui_root)

        _dispatch_ui(_choose_and_configure)

    def _check_local_project(icon: "pystray.Icon", item: Any) -> None:
        def _choose_and_check() -> None:
            try:
                project_root = _choose_project_root(
                    "Choose a local project to check", parent=ui_root,
                )
                if project_root:
                    _launch_local_cli("doctor", "--repo", project_root, cwd=project_root)
            except Exception as exc:  # noqa: BLE001 -- keep the tray available if the picker fails
                _show_error_dialog("Meridian project check failed", str(exc), parent=ui_root)

        _dispatch_ui(_choose_and_check)

    def _catalog_local_sessions(icon: "pystray.Icon", item: Any) -> None:
        _launch_local_cli("recovery", "catalog")

    def _show_artifact_commands(icon: "pystray.Icon", item: Any) -> None:
        _launch_local_cli("artifacts", "--help")

    def _restart(icon: "pystray.Icon", item: Any) -> None:
        def _do_restart() -> None:
            try:
                runner.restart()
            except Exception as exc:  # noqa: BLE001 -- report, don't crash the tray
                _dispatch_ui(
                    lambda error=exc: _show_error_dialog(
                        "Meridian restart failed", str(error), parent=ui_root,
                    )
                )

        threading.Thread(target=_do_restart, daemon=True).start()

    def _quit(icon: "pystray.Icon", item: Any) -> None:
        try:
            runner.stop()
        except Exception:  # noqa: BLE001 -- shutting down must never hang the tray
            pass
        icon.stop()
        if ui_dispatcher is not None and ui_root is not None:
            ui_dispatcher.submit(ui_root.quit)

    local_tools = pystray.Menu(
        pystray.MenuItem("Set up a local project…", _configure_local_project),
        pystray.MenuItem("Check a local project…", _check_local_project),
        pystray.MenuItem("Catalog local sessions", _catalog_local_sessions),
        pystray.MenuItem("Artifact capture commands", _show_artifact_commands),
    )
    menu = pystray.Menu(
        pystray.MenuItem("Open Dashboard", _open_dashboard, default=True),
        pystray.MenuItem("Status", _show_status),
        pystray.MenuItem("Local workstation tools", local_tools),
        pystray.MenuItem("Zotero connection…", _configure_zotero),
        pystray.MenuItem("View Logs", _show_logs),
        pystray.MenuItem("Restart", _restart),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", _quit),
    )
    icon_options = {}
    if darwin_nsapplication is not None:
        icon_options["darwin_nsapplication"] = darwin_nsapplication
    icon = pystray.Icon("meridian-tray", image, "Meridian", menu, **icon_options)
    if ui_root is None:
        icon.run()
    else:
        try:
            icon.run_detached()
            ui_root.mainloop()
        finally:
            ui_root.destroy()
    return 0


def _acquire_windows_tray_lock() -> tuple[str, Any | None]:
    """Acquire a per-user byte-range lock so Windows launches share one tray."""
    if sys.platform != "win32":
        return "unsupported", None

    import errno
    import msvcrt

    local_app_data = os.environ.get("LOCALAPPDATA")
    base_dir = (
        Path(local_app_data)
        if local_app_data
        else Path.home() / "AppData" / "Local"
    )
    lock_path = base_dir / "Meridian" / "tray-instance.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            handle.close()
            if (
                exc.errno in {errno.EACCES, errno.EDEADLK}
                or getattr(exc, "winerror", None) in {32, 33}
            ):
                return "already_running", None
            raise
        return "acquired", handle
    except BaseException:
        if not handle.closed:
            handle.close()
        raise


def _release_windows_tray_lock(handle: Any | None) -> None:
    if handle is None:
        return
    try:
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        handle.close()


def main(argv: "list[str] | None" = None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]
    # The packaged tray executable also exposes offline Meridian maintenance
    # commands such as ``setup``. Route those through the shared CLI dispatcher
    # instead of treating them as tray-only arguments.
    if argv and argv[0] in {"artifacts", "doctor", "hooks", "memory", "recovery", "setup"}:
        from .__main__ import main as meridian_main

        return meridian_main(argv)
    parser = argparse.ArgumentParser(
        prog="meridian-tray",
        description="Windows tray icon wrapping the Meridian local HTTP server (4e4c3817).",
    )
    parser.add_argument(
        _RUN_SERVER_FLAG,
        action="store_true",
        help=argparse.SUPPRESS,  # internal self-relaunch flag -- never for a human to type
    )
    parser.add_argument(
        _CONFIGURE_ZOTERO_FLAG,
        action="store_true",
        help="Open the optional, local-only Zotero connection setup.",
    )
    args = parser.parse_args(argv)

    if args.configure_zotero:
        try:
            run_zotero_setup_dialog()
            return 0
        except ZoteroSetupError as exc:
            _show_error_dialog("Zotero setup unavailable", str(exc))
            return 1

    if args.run_server:
        os.environ["MERIDIAN_FROZEN_MODE"] = "server"
        from . import __main__ as meridian_entry

        return meridian_entry.main([])

    try:
        lock_state, lock_handle = _acquire_windows_tray_lock()
    except OSError as exc:
        _show_error_dialog(
            "Meridian tray could not start",
            f"Could not establish the single-instance lock: {exc}",
        )
        return 1
    if lock_state == "already_running":
        # A second click should bring the user back to Meridian without adding
        # another icon to the tray overflow area.
        try:
            webbrowser.open(_dashboard_url())
        except Exception:  # noqa: BLE001 -- the existing tray remains usable
            pass
        return 0
    try:
        return _run_tray()
    finally:
        _release_windows_tray_lock(lock_handle)


if __name__ == "__main__":  # pragma: no cover -- exercised via main() in tests
    sys.exit(main())
