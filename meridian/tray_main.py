"""4e4c3817 -- Windows tray/GUI installer: ``meridian-tray.exe``.

Per pinned decision bce15b67 (which reconciles and supersedes the since-
deleted decision 6358f17f's original 5-file spec): a system-tray wrapper
around the already-built-and-tested :mod:`meridian.local_runner` primitive
(item 899936dd) -- ``LocalRunner`` already solves "supervise one child
process, don't double-spawn, recover from a stale PID, bound the log/output"
in general; this module's only job is the thin tray UI on top of it, wired
to the real Meridian HTTP server. Ships UNSIGNED per decision 8460f167 (code
signing deferred until money/time allow -- Microsoft killed the instant
SmartScreen reputation win for signed binaries in March 2024, so signing is
not a launch blocker).

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
import os
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

SCOPE = "meridian-tray"
_RUN_SERVER_FLAG = "--run-server"
_HEALTH_PROBE_TIMEOUT_SECONDS = 1.5


def _default_port() -> int:
    return int(os.environ.get("MERIDIAN_PORT", "7878"))


def _dashboard_url() -> str:
    return f"http://127.0.0.1:{_default_port()}/"


def _health_probe() -> bool:
    """LocalRunner's ``health_probe`` callable: a real HTTP GET against the
    server's own ``/health`` route (not a guess, not a bare port-open check
    -- a closed port never means "ready" and an open-but-not-yet-serving
    port never falsely reports ready either). Any failure means "not ready
    yet", never an exception escaping to ``LocalRunner`` (its own
    ``_await_readiness`` already treats a raising probe as "not ready" too,
    but staying defensive here keeps this callable's own contract explicit)."""
    url = f"http://127.0.0.1:{_default_port()}/health"
    try:
        with urllib.request.urlopen(url, timeout=_HEALTH_PROBE_TIMEOUT_SECONDS) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


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


def _server_env() -> "dict[str, str]":
    """The child's environment. Must be the FULL parent environment plus our
    one addition, never a bare ``{"MERIDIAN_FROZEN_MODE": "server"}`` dict --
    ``subprocess.Popen(env=...)`` REPLACES the environment entirely rather
    than merging, and the child needs PATH/etc. to function at all."""
    env = dict(os.environ)
    env["MERIDIAN_FROZEN_MODE"] = "server"
    return env


def _icon_image_path() -> Path:
    """Resolve ``meridian-tray.ico`` both frozen (PyInstaller bundles it
    under ``sys._MEIPASS`` per the ``datas=`` entry in meridian-tray.spec)
    and from source (``meridian/static/``)."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parent / "static"
    return base / "meridian-tray.ico"


def _build_runner() -> LocalRunner:
    return LocalRunner(
        scope=SCOPE,
        command=_server_command(),
        env=_server_env(),
        health_probe=_health_probe,
    )


# ---------------------------------------------------------------------------
# Tkinter dialogs -- each opens its OWN fresh Tk root and tears it down when
# closed, rather than keeping a persistent root alongside pystray's own event
# loop. pystray invokes each menu action in its own dedicated thread, so this
# never collides with a concurrently-open dialog from a different click.
# ---------------------------------------------------------------------------


def _show_status_dialog(runner: LocalRunner) -> None:
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
    root = tk.Tk()
    root.withdraw()
    messagebox.showinfo("Meridian status", "\n".join(lines), parent=root)
    root.destroy()


def _show_logs_window(runner: LocalRunner) -> None:
    import tkinter as tk
    from tkinter import scrolledtext

    tail = runner.tail_log() or "(no log output yet)"
    root = tk.Tk()
    root.title("Meridian -- log tail")
    root.geometry("800x500")
    text = scrolledtext.ScrolledText(root, wrap="word")
    text.insert("1.0", tail)
    text.configure(state="disabled")
    text.pack(fill="both", expand=True)
    root.mainloop()


def _show_error_dialog(title: str, message: str) -> None:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(title, message, parent=root)
    root.destroy()


# ---------------------------------------------------------------------------
# Tray icon
# ---------------------------------------------------------------------------


def _run_tray() -> int:
    import pystray
    from PIL import Image

    runner = _build_runner()

    try:
        runner.start()
    except RunnerAlreadyRunningError:
        # Already running (from a prior launch, or another tray instance) --
        # attach to it rather than treating this as an error. See module
        # docstring's "known limitation" note on multi-tray-icon launches.
        pass
    except Exception as exc:  # noqa: BLE001 -- must not silently exit with no UI at all
        _show_error_dialog("Meridian failed to start", str(exc))
        return 1

    icon_path = _icon_image_path()
    try:
        image = Image.open(icon_path)
    except (FileNotFoundError, OSError):
        # A missing icon file must never prevent the tray (and the server it
        # supervises) from running -- fall back to a plain solid square
        # rather than crashing the whole app over cosmetics.
        image = Image.new("RGBA", (64, 64), (0, 102, 204, 255))

    def _open_dashboard(icon: "pystray.Icon", item: Any) -> None:
        webbrowser.open(_dashboard_url())

    def _show_status(icon: "pystray.Icon", item: Any) -> None:
        threading.Thread(target=_show_status_dialog, args=(runner,), daemon=True).start()

    def _show_logs(icon: "pystray.Icon", item: Any) -> None:
        threading.Thread(target=_show_logs_window, args=(runner,), daemon=True).start()

    def _restart(icon: "pystray.Icon", item: Any) -> None:
        def _do_restart() -> None:
            try:
                runner.restart()
            except Exception as exc:  # noqa: BLE001 -- report, don't crash the tray
                _show_error_dialog("Meridian restart failed", str(exc))

        threading.Thread(target=_do_restart, daemon=True).start()

    def _quit(icon: "pystray.Icon", item: Any) -> None:
        try:
            runner.stop()
        except Exception:  # noqa: BLE001 -- shutting down must never hang the tray
            pass
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open Dashboard", _open_dashboard, default=True),
        pystray.MenuItem("Status", _show_status),
        pystray.MenuItem("View Logs", _show_logs),
        pystray.MenuItem("Restart", _restart),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", _quit),
    )
    icon = pystray.Icon("meridian-tray", image, "Meridian", menu)
    icon.run()
    return 0


def main(argv: "list[str] | None" = None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(
        prog="meridian-tray",
        description="Windows tray icon wrapping the Meridian local HTTP server (4e4c3817).",
    )
    parser.add_argument(
        _RUN_SERVER_FLAG,
        action="store_true",
        help=argparse.SUPPRESS,  # internal self-relaunch flag -- never for a human to type
    )
    args = parser.parse_args(argv)

    if args.run_server:
        os.environ["MERIDIAN_FROZEN_MODE"] = "server"
        from . import __main__ as meridian_entry

        return meridian_entry.main([])

    return _run_tray()


if __name__ == "__main__":  # pragma: no cover -- exercised via main() in tests
    sys.exit(main())
