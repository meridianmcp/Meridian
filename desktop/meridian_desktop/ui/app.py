"""Application runner: QApplication, main window and tray.

``--smoke`` builds the window and tray, checks that the window is visible,
prints "smoke ok" and returns 0 without starting the event loop.

On a real start with no settings file yet, the first-run dialog runs before the window.
``--smoke`` never shows it, and ``first_run=False`` turns it off for callers that must not see it.
A real start checks for a newer release at launch and every six hours after, unless auto_update or check_updates is off.
``--smoke`` does not check. A start with ``--after-update <old>`` (the staged version, started by Restart to update)
finishes the update, and shows one line that says it was updated from <old>.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from meridian_desktop import custom_connectors, install, local_cache, token_store, updater
from meridian_desktop.desktop_log import start_desktop_log
from meridian_desktop.restart_check import RestartWatch
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.tray import MeridianTray
from meridian_desktop.ui.first_run import needs_first_run, run_first_run
from meridian_desktop.ui.main_window import MainWindow
from meridian_desktop.ui.runner_actions import LaunchStarts, RunnerActions
from meridian_desktop.version import APP_VERSION

SMOKE_FLAG = "--smoke"
LOCAL_CACHE_LOGGER = "meridian_desktop.local_cache"


def _sync_local_cache(store: SettingsStore) -> None:
    """At a real start, write the local servers into Claude Desktop and Codex, skipping the tools the owner switched off.

    A settings file with a problem is not used. Its tool choice may be unreadable, so every tool would look on and the
    existing entries would be rewritten. Nothing here stops the app: a failure is logged and the app starts anyway.
    """
    log = logging.getLogger(LOCAL_CACHE_LOGGER)
    try:
        result = store.load()
        if result.warning is not None:
            log.warning("local cache not updated: the settings file has a problem (%s)", result.warning)
            return
        settings = result.settings
        custom = custom_connectors.cache_entries(settings.custom_connectors, resolve=local_cache.find_command)
        for line in local_cache.sync_local_cache(
            disabled=settings.disabled_tools, core_checkout=settings.core_checkout, custom=custom
        ):
            log.info("%s", line)
    except Exception as exc:  # the app must start even when the cache cannot be written
        log.warning("local cache not updated (%s)", exc.__class__.__name__)


def _after_update_version(args: list[str]) -> str | None:
    """The old version named after --after-update, or None. A flag with no plain version after it is ignored."""
    if updater.AFTER_UPDATE_FLAG not in args:
        return None
    index = args.index(updater.AFTER_UPDATE_FLAG)
    if index + 1 >= len(args):
        return None
    candidate = args[index + 1]
    try:
        updater.parse_version(candidate)
    except updater.Refused:
        return None
    return candidate


def _finish_staged_update() -> None:
    """At a real start of the installed bundle, finish a staged update, or point the shortcuts at this version.

    Nothing here stops the app from starting. A failure leaves any staged update in place for the next start.
    """
    if not updater.running_frozen():
        return
    try:
        env = os.environ
        install_root = install.install_dir(env)
        updater.finish_update_at_start(
            install_root,
            Path(sys.executable).resolve(),
            APP_VERSION,
            install.update_shortcut_writers(env),
            record_apps_entry=install.apps_entry_recorder(env),
        )
    except Exception:  # noqa: BLE001 - a failed finish must never stop the app from starting
        return


def _refuse_request(request: object, timeout: float) -> object:
    """The transport a --smoke run gives the connection bar. It sends nothing; the check reads it as unreachable."""
    raise OSError("a --smoke run makes no hosted request")


def main(argv: list[str] | None = None, *, settings_path: Path | None = None, first_run: bool = True) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    smoke = SMOKE_FLAG in args
    after_update = _after_update_version(args)
    # The baseline for the restart notice, taken before the first-run dialog, so it matches the code loaded at launch.
    restart_watch = RestartWatch()

    # The desktop's own log starts before anything else is built. --smoke writes no log file.
    if not smoke:
        start_desktop_log()
        _finish_staged_update()

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Meridian")
    # The tray keeps the process alive when the window is closed.
    app.setQuitOnLastWindowClosed(False)

    store = SettingsStore(settings_path)
    # The mode this run starts in. A mode the file names wins. With none, hosted when a hosted token is saved, and
    # local otherwise. Only whether a token exists is checked here. The token is never read into the window.
    store.resolve_launch_mode(lambda: token_store.has_token(store=KeyringSecretStore()))
    if first_run and needs_first_run(store, smoke=smoke):
        outcome = run_first_run(store)
        if outcome.notice:
            QMessageBox.information(None, "Meridian Desktop", outcome.notice)
    # The local cache is written at a real start only, never at --smoke, so a smoke run touches no client config.
    if not smoke:
        _sync_local_cache(store)

    # The update check runs at a real start and every six hours after, through the urllib transport. A --smoke run
    # passes no transport and no install folder, so it makes no request and stages nothing. The window still honours
    # the check_updates setting.
    # One set of runner controls is shared by every tab that starts or stops a runner, so one core search serves them all.
    actions = RunnerActions(store)
    window = MainWindow(
        store,
        update_transport=None if smoke else updater.urllib_transport,
        update_root=None if smoke else install.install_dir(os.environ),
        record_apps_entry=None if smoke else install.apps_entry_recorder(os.environ),
        runner_actions=actions,
        # A --smoke run makes no hosted request, so the connection bar's check is refused as an unreachable host would be.
        connection_transport=_refuse_request if smoke else None,
        restart_watch=restart_watch,
    )
    tray = MeridianTray(window, app)
    window.show()
    tray.show()

    if smoke:
        if not window.isVisible():
            print("meridian_desktop --smoke: window not visible", file=sys.stderr)
            return 1
        print("smoke ok")
        return 0

    if after_update is not None:
        # The one line after a restart into a staged update. It is shown for this start only.
        window.show_launch_notes(f"Updated from {after_update} to {APP_VERSION}.")

    # The core search runs once per launch, on the thread pool, after the window shows. It finds the Meridian checkout
    # and its Python, so the Tunnel and local-server controls are on without the owner typing either path. A smoke run
    # never searches. The result reaches the controls through RunnerActions.coreChanged.
    QTimer.singleShot(0, actions.find_core)

    # The launch-time starts (amendments A and B of docs/local-trust-contract.md) run once, after the window shows.
    # Each runs only when its own switch is on, and never in a smoke run, which returned above. The starter is held
    # by this frame, so it lives as long as the event loop runs.
    launch = LaunchStarts(actions, report=window.show_launch_notes)
    QTimer.singleShot(0, launch.start)
    return int(app.exec())
