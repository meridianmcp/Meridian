"""The Qt side of the owner-confirmed runner controls: the confirmation dialog, the off-thread job, the launch-time starts.

This is Qt code, so it sits in ui/. Its pure parts are in meridian_desktop/runner_controls.py and
meridian_desktop/launch_starts.py, and the contract is docs/local-trust-contract.md.

- RunnerActions holds every outside effect the controls use: the saved core setup, the RunnerControl factory, the
  confirmation dialog, and the Zotero and local-URL calls. The app builds it from the settings file. Tests pass fakes.
- ask_control builds the confirmation dialog. Its default button is Cancel, Escape is Cancel, and it names the exact
  command, the scope and the working folder. Only the confirm button returns True (rule 2).
- submit runs one call on the thread pool. Its result comes back through shared_delivery() with the owner token and
  a generation number. The job never holds a widget, so a widget that is deleted while a call runs is never touched.
- LaunchStarts runs the two launch-time starts (amendments A and B) once, after the window shows, through the same
  delivery path. It starts nothing when both settings are off.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtWidgets import QFileDialog, QLabel, QMessageBox, QPushButton, QWidget

from meridian_desktop import local_client, zotero_client
from meridian_desktop.core_locator import NOT_FOUND, LocatedCore, locate_core, remember_core
from meridian_desktop.launch_starts import TUNNEL_MODES, LaunchDeps, LaunchNote, run_launch_starts
from meridian_desktop.runner_controls import SCOPE_LOCAL, SCOPE_TUNNEL, RunnerControl, default_log_path
from meridian_desktop.settings_store import CONFIRMED_KEYS, CONTROL_FLAG_KEYS, CORE_CHECKOUT_KEY, SettingsStore
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.text import make_selectable

LAUNCH_FAILED_TEXT = "A launch-time start could not be run ({})."
LOCATE_FAILED_TEXT = "The Meridian core could not be looked for ({})."
NOT_SAVED_FOUND_TEXT = (
    "The Meridian core was found, but it could not be saved in Settings, so it is not in use. "
    "Check that the settings folder can be written."
)
CORE_FOLDER_DIALOG_TITLE = "Choose the Meridian core folder"
CHOOSE_CANCELLED_TEXT = "Cancelled. The Meridian core folder was not changed."
CHOSEN_TEXT = "Meridian core folder saved. Looking for its Python now."
CORE_NOT_SAVED_TEXT = "Not saved: the settings file could not be written."
LAUNCH_SWITCHES_OFF_TEXT = (
    "The Zotero and local server switches are now off, because the core setup changed. "
    "Turn them on again to confirm the new setup."
)
TUNNEL_ON_TEXT = "Tunnel controls turned on. Each button still asks first."
TUNNEL_NOT_SAVED_TEXT = "Not turned on: the settings file could not be written."
# The one generation a launch run uses. It runs once, so it never needs to be superseded.
_LAUNCH_GENERATION = 1


@dataclass(frozen=True)
class CoreSetup:
    """The settings the controls read: the core checkout and its Python, the local URL, and the three switches.

    The confirmed values are the exact ones the owner saw in the dialog when a launch-time switch was turned on. The
    launch-time starts use only these, never the current core setup (docs/local-trust-contract.md, amendments A and B).
    """

    python: str
    cwd: str
    local_url: str
    tunnel_control_enabled: bool
    zotero_autostart: bool
    local_server_autostart: bool
    confirmed_python: str = ""
    confirmed_cwd: str = ""
    confirmed_zotero_exe: str = ""
    # The desktop mode, and the tunnel's launch-time switch (amendment G). The tunnel start needs a TUNNEL_MODES mode.
    mode: str = "local"
    tunnel_autostart: bool = False

    @property
    def core_ready(self) -> bool:
        return bool(self.python.strip() and self.cwd.strip())

    @property
    def launch_ready(self) -> bool:
        """True when the core checkout and its Python were confirmed for a launch-time start."""
        return bool(self.confirmed_python.strip() and self.confirmed_cwd.strip())


ConfirmFn = Callable[[QWidget | None, str, str, str], bool]
# A folder chooser takes (parent, start folder) and returns the chosen folder, or "" when cancelled.
FolderChooser = Callable[[QWidget | None, str], str]
# A core search takes the settings store and returns what it found. locate_core by default.
LocateFn = Callable[[SettingsStore], LocatedCore]


def control_box(parent: QWidget | None, title: str, body: str, confirm_text: str) -> tuple[QMessageBox, QPushButton]:
    """The confirmation dialog, built but not shown. Its default and Escape button is Cancel, so Enter never confirms.

    The title and the body are plain text, so a command or path is never read as HTML.
    """
    box = QMessageBox(parent)
    box.setWindowTitle("Meridian Desktop")
    box.setIcon(QMessageBox.Icon.Question)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(title)
    box.setInformativeText(body)
    confirm = box.addButton(confirm_text, QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    for child in box.findChildren(QLabel):
        child.setTextFormat(Qt.TextFormat.PlainText)
        make_selectable(child)
    return box, confirm


def _choose_core_folder(parent: QWidget | None, start_dir: str) -> str:
    """Ask for the Meridian core folder. Return its path, or "" when the dialog is cancelled."""
    return QFileDialog.getExistingDirectory(parent, CORE_FOLDER_DIALOG_TITLE, start_dir)


def _search_core(store: SettingsStore) -> LocatedCore:
    """The real search: settings, the Documents folders and the pixi environments, with the real probe."""
    return locate_core(store)


def ask_control(parent: QWidget | None, title: str, body: str, confirm_text: str) -> bool:
    """Show the confirmation. True only when the confirm button is chosen. Cancel, Escape and closing are all no."""
    box, confirm = control_box(parent, title, body, confirm_text)
    box.exec()
    return box.clickedButton() is confirm


class RunnerActions(QObject):
    """The desktop's runner controls: the saved setup, the core search, the control factory, the dialog and the launch calls.

    Every outside effect is a constructor argument, so a test passes fakes and never starts a process, opens a socket,
    reads the real settings file or opens a dialog. The defaults are the real calls, and only the app uses them.

    ``coreChanged`` is emitted on the GUI thread when a core search starts and when it finishes, when the owner chooses
    a core folder, and when the tunnel switch is turned on. The controls connect to it, so each one reads the setup again.
    """

    coreChanged = Signal()
    # Emitted when a tunnel or local call is claimed or released, so every control for that scope reads its state again.
    scopeBusyChanged = Signal()

    def __init__(
        self,
        store: SettingsStore,
        *,
        make_control: Callable[[str, str], RunnerControl] | None = None,
        confirm: ConfirmFn | None = None,
        find_zotero: Callable[[], str | None] | None = None,
        probe_zotero: Callable[[], Any] | None = None,
        start_zotero: Callable[..., str] | None = None,
        probe_local: Callable[[str], str] | None = None,
        log_path: Path | None = None,
        locate: LocateFn | None = None,
        choose_folder: FolderChooser | None = None,
        spawn: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__()
        self._store = store
        self._log_path = log_path
        # The process runner the default controls use. None means subprocess.run. Tests pass a fake.
        self._spawn = spawn
        # Set by the window that owns these controls. See set_on_started.
        self._on_started: Callable[[int, str], None] | None = None
        self._make_control = make_control if make_control is not None else self._default_control
        self._confirm = confirm if confirm is not None else ask_control
        self._find_zotero = find_zotero if find_zotero is not None else zotero_client.find_zotero_exe
        self._probe_zotero = probe_zotero if probe_zotero is not None else zotero_client.probe_api
        self._start_zotero = start_zotero if start_zotero is not None else zotero_client.start_zotero
        self._probe_local = probe_local if probe_local is not None else local_client.probe_health
        self._locate = locate if locate is not None else _search_core
        self._choose_folder = choose_folder if choose_folder is not None else _choose_core_folder
        # The last search's result. None until a search has finished in this process.
        self.located: LocatedCore | None = None
        self._locating = False
        # Identifies this object's searches. A plain object, so a job can hold it without holding the object.
        self._locate_owner = object()
        self._locate_generation = 0
        # The owner that holds each scope's one call (the Tunnel tab's controls and the Home card's Start share it).
        self._scope_owner: dict[str, object] = {}
        shared_delivery().delivered.connect(self._on_located)

    def _log(self) -> Path:
        return self._log_path if self._log_path is not None else default_log_path()

    def set_on_started(self, callback: Callable[[int, str], None]) -> None:
        """Call ``callback(pid, core checkout)`` when a local start is confirmed with a pid. Later controls use it."""
        self._on_started = callback

    def _default_control(self, python: str, cwd: str) -> RunnerControl:
        if self._spawn is None:
            return RunnerControl(python, cwd, log_path=self._log())
        return RunnerControl(python, cwd, spawn=self._spawn, log_path=self._log())

    @property
    def locating(self) -> bool:
        """True while a core search is running."""
        return self._locating

    def scope_busy_by_other(self, scope: str, owner: object) -> bool:
        """True while a different control holds this scope's call. Only one start, stop or restart runs per scope."""
        holder = self._scope_owner.get(scope)
        return holder is not None and holder is not owner

    def begin_scope_call(self, scope: str, owner: object) -> bool:
        """Claim the scope for one owner's call. False when another owner holds it. Starts nothing."""
        if self.scope_busy_by_other(scope, owner):
            return False
        self._scope_owner[scope] = owner
        self.scopeBusyChanged.emit()
        return True

    def end_scope_call(self, scope: str, owner: object) -> None:
        """Release the scope when this owner's whole action has finished. A restart holds it through its start too."""
        if self._scope_owner.get(scope) is owner:
            del self._scope_owner[scope]
            self.scopeBusyChanged.emit()

    def core(self) -> CoreSetup:
        """The settings as they are saved now. Read on the calling thread, and cheap (one small file).

        When the last search found no usable core, the checkout and Python read as not set, so the controls stay off
        until the owner chooses one. The saved values are never read as working in that case.
        """
        settings = self._store.load().settings
        python, cwd = settings.core_python, settings.core_checkout
        if self.located is not None and not self.located.found and not self._locating:
            python, cwd = "", ""
        return CoreSetup(
            python=python,
            cwd=cwd,
            local_url=settings.local_url,
            tunnel_control_enabled=settings.tunnel_control_enabled,
            zotero_autostart=settings.zotero_autostart,
            local_server_autostart=settings.local_server_autostart,
            confirmed_python=settings.confirmed_core_python,
            confirmed_cwd=settings.confirmed_core_checkout,
            confirmed_zotero_exe=settings.confirmed_zotero_exe,
            mode=settings.mode,
            tunnel_autostart=settings.tunnel_autostart,
        )

    def control(self, python: str, cwd: str) -> Any:
        control = self._make_control(python, cwd)
        # Every control this object hands out gets the window's start hook, including one from a make_control factory.
        # A control without set_on_started cannot take the hook, so that is an error here, not a silent gap later.
        if self._on_started is not None:
            attach = getattr(control, "set_on_started", None)
            if not callable(attach):
                raise TypeError(
                    f"{type(control).__name__} has no set_on_started, so the start hook cannot be attached to it",
                )
            attach(self._on_started)
        return control

    def ask(self, parent: QWidget | None, title: str, body: str, confirm_text: str) -> bool:
        return self._confirm(parent, title, body, confirm_text)

    def set_flag(self, name: str, value: bool, *, confirmed: Mapping[str, str] | None = None) -> None:
        """Save one runner switch, and any confirmed values in the same save. Every other field is kept.

        ``confirmed`` holds the exact values the owner saw in the dialog for a switch turned on. Only the confirmed
        keys are accepted. Raises OSError or ValueError when it cannot save.
        """
        if name not in CONTROL_FLAG_KEYS:
            raise ValueError(f"unknown runner switch: {name}")
        recorded = dict(confirmed or {})
        if any(key not in CONFIRMED_KEYS for key in recorded):
            raise ValueError("unknown confirmed value")
        self._store.save(replace(self._store.load().settings, **{name: value}, **recorded))

    def find_zotero(self) -> str | None:
        return self._find_zotero()

    # ----------------------------------------------------------- finding the core

    def find_core(self) -> None:
        """Look for the Meridian core on this computer, on the thread pool. A search already running is not doubled.

        The app calls this once at start. The Settings button "Find it again" and the Choose button call it too.
        """
        if self._locating:
            return
        self._locating = True
        self._locate_generation += 1
        generation = self._locate_generation
        store, locate = self._store, self._locate
        submit(self._locate_owner, generation, lambda: locate(store))
        self.coreChanged.emit()

    def notify_core_changed(self) -> None:
        """Tell the controls that the core setup was saved, so each one reads it again."""
        self.coreChanged.emit()

    def choose_core_folder(self, parent: QWidget | None = None) -> str:
        """Ask for the Meridian core folder, save it when it is one, then look for its Python. Returns the line to show.

        The folder is saved as the owner's own choice, so the locator never replaces it. Saving a core path turns both
        launch-time switches off (docs/local-trust-contract.md, amendment C), and the returned line says so.
        """
        before = self._store.load().settings
        chosen = self._choose_folder(parent, before.core_checkout)
        if not chosen:
            return CHOOSE_CANCELLED_TEXT
        try:
            self._store.set_core_path(CORE_CHECKOUT_KEY, chosen)
        except ValueError as exc:
            return str(exc)  # plain words from settings_store, which names the refused folder and saves nothing
        except OSError:
            return CORE_NOT_SAVED_TEXT
        # A search that is still running may have started for the old setup. Its result is dropped by the new generation.
        self._locating = False
        self.find_core()
        if before.zotero_autostart or before.local_server_autostart or before.tunnel_autostart:
            return f"{CHOSEN_TEXT} {LAUNCH_SWITCHES_OFF_TEXT}"
        return CHOSEN_TEXT

    def enable_tunnel_controls(self) -> str:
        """Turn on tunnel_control_enabled, the switch the Tunnel buttons read. Returns the line to show.

        The settings file is copied aside first when it did not load cleanly, because this write replaces it.
        """
        try:
            self._store.keep_unreadable_copy()
            self.set_flag("tunnel_control_enabled", True)
        except (OSError, ValueError):
            return TUNNEL_NOT_SAVED_TEXT
        self.coreChanged.emit()
        return TUNNEL_ON_TEXT

    @Slot(object)
    def _on_located(self, item: object) -> None:
        """Runs on the GUI thread. A result from another object, or from a search that a newer one replaced, is dropped."""
        if not isinstance(item, Delivered) or item.owner is not self._locate_owner:
            return
        generation, (value, failure) = item.value
        if generation != self._locate_generation:
            return
        self._locating = False
        if failure is not None or not isinstance(value, LocatedCore):
            result = LocatedCore(NOT_FOUND, reason=LOCATE_FAILED_TEXT.format(failure or "no result"))
        else:
            result = value
            try:
                remember_core(self._store, value)
            except (OSError, ValueError):
                result = LocatedCore(NOT_FOUND, reason=NOT_SAVED_FOUND_TEXT)
        self.located = result
        self.coreChanged.emit()

    def launch_deps(self, core: CoreSetup) -> LaunchDeps:
        """The calls the launch-time starts make. They use only the confirmed values, never the current setup.

        With no confirmed Python or checkout, the local start is skipped with a plain line. With no confirmed Zotero
        program, the Zotero start is skipped the same way. Each call runs on the worker.
        """
        control = self.control(core.confirmed_python, core.confirmed_cwd)
        log = self._log()
        probe_zotero = self._probe_zotero
        start_zotero = self._start_zotero
        probe_local = self._probe_local
        local_url = core.local_url
        confirmed_exe = core.confirmed_zotero_exe if core.confirmed_zotero_exe.strip() else None
        return LaunchDeps(
            zotero_probe=probe_zotero,
            zotero_exe=lambda: confirmed_exe,
            zotero_start=lambda exe: start_zotero(exe, log_path=log),
            local_health=lambda: probe_local(local_url),
            local_status=lambda: control.status(SCOPE_LOCAL),
            local_start=lambda: control.start(SCOPE_LOCAL),
            core_ready=core.launch_ready,
            tunnel_mode=core.mode in TUNNEL_MODES,
            tunnel_controls_on=core.tunnel_control_enabled,
            tunnel_status=lambda: control.status(SCOPE_TUNNEL),
            tunnel_start=lambda: control.start(SCOPE_TUNNEL),
        )


class ActionJob(QRunnable):
    """Run ``work`` on the thread pool and deliver (value, failure_class_name).

    A raised exception is reduced to its class name, so no traceback or message reaches the UI.
    """

    def __init__(self, work: Callable[[], Any], deliver: Callable[[tuple], None]) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._work = work
        self._deliver = deliver

    def run(self) -> None:
        value: Any = None
        failure: str | None = None
        try:
            value = self._work()
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            failure = exc.__class__.__name__
        try:
            self._deliver((value, failure))
        except RuntimeError:
            pass  # the shared delivery object is gone, for example at shutdown


def submit(owner: object, generation: int, work: Callable[[], Any]) -> None:
    """Run ``work`` on the thread pool. Its result is delivered as Delivered(owner, (generation, (value, failure))).

    The deliver closure captures only the owner, the generation and the shared delivery object, never a widget.
    """
    delivery = shared_delivery()

    def deliver(payload: tuple) -> None:
        delivery.delivered.emit(Delivered(owner, (generation, payload)))

    QThreadPool.globalInstance().start(ActionJob(work, deliver))


class LaunchStarts(QObject):
    """Run the launch-time starts once, on the thread pool, and report what they did through ``report``.

    Call start() after the window shows. It does nothing when neither switch is on, and it never runs twice.
    """

    def __init__(
        self,
        actions: RunnerActions,
        *,
        report: Callable[[str], None] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._actions = actions
        self._report = report
        # Identifies this object's launch results. A plain object, so a job can hold it without holding the object.
        self._owner = object()
        self._started = False
        shared_delivery().delivered.connect(self._on_delivered)

    @Slot()
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        core = self._actions.core()
        zotero_on, local_on, tunnel_on = core.zotero_autostart, core.local_server_autostart, core.tunnel_autostart
        if not (zotero_on or local_on or tunnel_on):
            return
        deps = self._actions.launch_deps(core)
        submit(
            self._owner,
            _LAUNCH_GENERATION,
            lambda: run_launch_starts(zotero_on, local_on, deps, tunnel_on=tunnel_on),
        )

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another object is dropped here.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        _generation, (notes, failure) = item.value
        if failure is not None:
            text = LAUNCH_FAILED_TEXT.format(failure)
        else:
            text = "\n".join(note.message for note in notes if isinstance(note, LaunchNote))
        if text and self._report is not None:
            self._report(text)


__all__ = [
    "ActionJob",
    "ConfirmFn",
    "CoreSetup",
    "FolderChooser",
    "LaunchStarts",
    "LocateFn",
    "RunnerActions",
    "ask_control",
    "control_box",
    "submit",
]
