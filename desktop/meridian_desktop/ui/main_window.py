"""Main window: a connection bar, a mode selector, then eight tabs: Home, Projects, Sessions, GPUs, Search, Tunnel, Keys, Settings.

The connection bar (ui/connection_bar.py) is the strip at the very top. It states who is signed in and until when, what
the desktop is connected to (the hosted URL, with connected, reconnecting or not connected), and the local server when
one is live. It checks when the window opens, every five minutes, and after a sign-in or sign-out. With no token saved
it makes no hosted call. With one saved, it calls GET /me on the hosted URL, and it renews the token only when a refresh
token was saved and under 14 days remain. Sign in with browser in Settings calls the hosted device flow only when the
owner presses it.

Home (ui/home_tab.py) has one card for each part of Meridian: the local Meridian server, the tunnel, the services
(the LaTeX engine and the slot ports), Zotero, the AI clients and their tools, and the updates. Each card checks its
part on its own when the window first shows, again every five minutes while Home is showing, and when Check now is
pressed. The tunnel card's Start is on Home, Stop and Restart are on the Tunnel tab, and each asks first. The Zotero launch
asks first too. Search (ui/search_tab.py) lists the local server's code, outputs and research, grouped by source. It has no
search box. Code and outputs have no list route yet, so they send nothing, and research runs are read from the Local URL.
The Tunnel tab (ui/tunnel_panel.py) is the one place for the tunnel: its status, Start, Stop and
Restart, the lanes, the tool groups and the custom connectors. The Keys tab (ui/keys_tab.py) holds the RunPod card and
one card per named key. Settings then holds the connection settings, the Zotero library panel, the Writing area (DOCX
and Slides, each Not connected), and
Diagnostics: the full tools table, the LaTeX engine probe, and the desktop, tunnel and runner logs. The Projects and
Sessions tabs are read-only.

Qt code lives in meridian_desktop/ui/ (and the listed exception tray.py). Settings go through settings_store and the
RunPod key goes through secret_store, and neither imports Qt. The only network calls, apart from the update check
below and the hosted calls of the connection bar and Sign in with browser (described at the top), are loopback calls: to the local Meridian server at the local_url setting, to the local LaTeX engine at the
engine_url setting, to the local Zotero API on 127.0.0.1:23119, and to 127.0.0.1 slot ports for a connect-only check.
A check makes its call when the window first shows, on the Home tab's five-minute timer while it is showing, or when
its button is pressed. The Search tab reads the research runs when it is first shown and when Refresh is pressed. Hosted mode calls
only the hosted URL the owner sets: on the Projects tab, on the Sessions tab when it is first shown or its Refresh is
pressed (for the project list, and for the sessions when no local server answers and a hosted token is saved), and
on the Test connection and Try again buttons in Settings.

The Sessions tab (read-only) lists the sessions of one project, picked from the project list. The project list
and the sessions are read when the tab is first shown and when its Refresh button is pressed, through the mode
chosen in the mode selector. While the tab stays visible, a 5-second timer reads only the local server, and only
in local mode, for the picked project. The timer never reads the project list and never calls the hosted host,
and when the local server does not answer it changes nothing on screen. Changing the picked project reads the
local server the same way, in local mode only; in any other mode it clears the table and says to press Refresh.

At a real start, one update check runs on the thread pool, and again every six hours while the app runs. It asks GitHub
for the latest release and its signed checksum list, downloads a newer release only after the signature verifies, and
stages it for the next restart. The status bar shows the download and the staged release, and the Updates card on Home
reads that same one result without asking GitHub again. It is off when the auto_update or check_updates setting is
false, and the app passes no transport under --smoke, so a smoke run makes no request.

Hosted mode also shows the tunnel's status. Its top bar shows one line with the tunnel's state
(from one read-only status call, made when Hosted is chosen and at start in Hosted mode), the tools
that need the tunnel, and how to start it by hand. The top bar has no control that starts, stops or
restarts anything.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThreadPool, Qt, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import local_client, token_store, updater, zotero_client
from meridian_desktop.data_source import STATE_OK, DataSources, resolve
from meridian_desktop.hosted_client import Transport
from meridian_desktop.restart_check import RestartWatch
from meridian_desktop.runner_client import RunnerClient, RunnerResult
from meridian_desktop.secret_store import KeyringSecretStore, SecretStore
from meridian_desktop.settings_store import HOSTED_MODE, SettingsStore, SettingsUnreadable
from meridian_desktop.tools_status import ToolsStatus, read_tools_status
from meridian_desktop.tunnel_status import BAR_CHECKING_TEXT, DEPENDENCY_TEXT, bar_texts, read_tunnel_status
from meridian_desktop.ui.connection_bar import ConnectionBar
from meridian_desktop.ui.delivery import Delivered, shared_delivery
from meridian_desktop.ui.gpu_jobs_panel import GpuJobsPanel
from meridian_desktop.ui.home_tab import HomeDeps, HomeTab
from meridian_desktop.ui.keys_tab import KeysTab
from meridian_desktop.ui.projects_tab import ProjectsTab, default_sources
from meridian_desktop.ui.restart_banner import RestartBanner
from meridian_desktop.ui.runner_actions import RunnerActions
from meridian_desktop.ui.search_tab import SearchTab
from meridian_desktop.ui.sessions_panel import SessionsPanel
from meridian_desktop.ui.settings_tab import LATEX_SECTION, MODE_ITEMS, SettingsTab
from meridian_desktop.ui.tab_wheel import TabBarWheelBlocker
from meridian_desktop.ui.text import make_selectable
from meridian_desktop.ui.tunnel_check import TunnelCheckJob
from meridian_desktop.ui.tunnel_panel import TunnelPanel
from meridian_desktop.ui.update_notice import UpdateNoticeBar
from meridian_desktop.ui.writing_area import WRITING_SECTION, WritingArea
from meridian_desktop.ui.latex_panel import LatexPanel
from meridian_desktop.ui.zotero_panel import ZoteroPanel
from meridian_desktop.version import APP_VERSION

# (tab name, wave that built it). Display order is the tuple order. Each tab is a top-level area: the rest of the
# areas are cards on Home, the Search tab's kinds, or sections of Settings.
TAB_SPECS = (
    ("Home", 7),
    ("Projects", 2),
    ("Sessions", 3),
    ("GPUs", 3),
    ("Search", 6),
    ("Tunnel", 8),
    ("Zotero", 8),
    ("LaTeX", 8),
    ("Keys", 8),
    ("Settings", 2),
)
TAB_NAMES = tuple(name for name, _wave in TAB_SPECS)

ZOTERO_SECTION = (
    "Zotero (the Zotero app on this computer: searches read 127.0.0.1:23119, saves go through the bridge on 127.0.0.1:23125)"
)

STATUS_MESSAGE = "Home checks each part of Meridian when this window opens, then every five minutes while Home is showing."

# In Hosted mode the top bar shows the tunnel's status, because hosted tools reach this computer through the tunnel.
# One plain sentence per mode. The one for the chosen mode shows under the selector.
MODE_DESCRIPTIONS = {
    "hosted": (
        "Hosted: projects load from your hosted Meridian account, with the hosted token saved in Settings. "
        "The top bar shows the tunnel's status, and hosted tools reach this computer through the tunnel."
    ),
    "local": "Local: projects load from the Meridian server or database file on this computer.",
}

# The window opens at this size (width, height). The tab labels fit at 1280 px, and Home scrolls its cards
# below that height, so no card is cut off on a short window.
DEFAULT_WINDOW_SIZE = (1280, 780)


def _fit_and_center(window: QWidget, size: tuple[int, int]) -> None:
    """Open the window no larger than the usable screen area, and centred on it. Without this the window
    keeps the requested size at the system default position, so on a small or scaled screen its bottom
    and right edges fall off the screen."""
    screen = window.screen() or QGuiApplication.primaryScreen()
    if screen is None:
        window.resize(*size)
        return
    area = screen.availableGeometry()
    width = min(size[0], int(area.width() * 0.95))
    height = min(size[1], int(area.height() * 0.9))
    window.resize(width, height)
    window.move(area.x() + (area.width() - width) // 2, area.y() + (area.height() - height) // 2)


def _updates_allowed_reader(store: SettingsStore) -> Callable[[], bool]:
    """A reader that says whether automatic updates and the update check are both on. Read at each check, so a change
    in Settings applies at once. It holds the store only, never the window, so the window can still be freed."""

    def allowed() -> bool:
        settings = store.load().settings
        return settings.auto_update and settings.check_updates

    return allowed


class ProjectsUnavailable(Exception):
    """The Projects resolver found no source to read. The Sessions tab then says the projects could not be read,
    in plain words that name no class and no path."""


def _saved_local_url(store: SettingsStore) -> Callable[[], str]:
    """The local URL as saved now, read by the Search tab when a list is read.

    It holds the store, not the window. A bound method of the window kept the window in a reference
    cycle, so a closed window was freed only by the cyclic collector. The collector runs on whichever
    thread allocates, and the panels call this provider on the pool, so a window could be freed on a
    pool worker and its widgets deleted off the GUI thread.
    """

    def read() -> str:
        return store.load().settings.local_url

    return read


def _saved_gpu_hosts(store: SettingsStore) -> Callable[[], tuple[str, ...]]:
    """The SSH host names saved for the GPUs tab, read on the worker thread at each read. Holds the store, not the window."""

    def read() -> tuple[str, ...]:
        return store.load().settings.gpu_hosts

    return read


def _saved_runpod_pods(store: SettingsStore) -> Callable[[], dict[str, Any]]:
    """The RunPod pods the local Meridian server lists, read on the worker thread. Raises when that server is not
    answering, and the GPUs tab then says RunPod is not connected. Holds the store, not the window."""

    def read() -> dict[str, Any]:
        return local_client.runpod_list_pods(store.load().settings.local_url)

    return read


def _saved_mode(store: SettingsStore) -> Callable[[], str]:
    """The mode saved in Settings, read by the Sessions tab's timer. Runs on the worker thread, holds no window."""

    def read() -> str:
        return store.load().settings.mode

    return read


def _saved_only_active(store: SettingsStore) -> bool:
    """The Sessions tab's "Only projects with active sessions" switch as saved. Read once when the tab is built,
    from the local settings file. No session or server is read."""
    return store.load().settings.sessions_only_active_projects


def _save_only_active(store: SettingsStore) -> Callable[[bool], bool]:
    """The save callback for the Sessions tab's switch. It returns False when the file could not be written, so the tab
    says the choice holds for this session only. It holds no window."""

    def write(value: bool) -> bool:
        try:
            store.set_sessions_only_active(value)
        except (OSError, SettingsUnreadable):
            return False
        return True

    return write


def _saved_hosted(store: SettingsStore, secrets: SecretStore) -> Callable[[], tuple[str, str | None]]:
    """The hosted URL in Settings and the saved hosted token, for the Sessions tab. Runs on the worker thread.

    The token goes to the Sessions client for one request. The tab never shows it. Like _saved_local_url, this
    holds the store and the secret store, never the window, so no reference cycle keeps a closed window alive.
    """

    def read() -> tuple[str, str | None]:
        return store.load().settings.hosted_url, token_store.get_token(store=secrets)

    return read


def _page_with_label(label: str, widget: QWidget, parent: QWidget) -> QScrollArea:
    """A tab page: a section label over one widget, in a scroll area so the lower part is never hidden."""
    container = QWidget(parent)
    column = QVBoxLayout(container)
    column.addWidget(make_selectable(QLabel(label, container)))
    column.addWidget(widget)
    column.addStretch(1)
    scroll = QScrollArea(parent)
    scroll.setWidgetResizable(True)
    scroll.setWidget(container)
    return scroll


def _scroll_page(widget: QWidget, parent: QWidget) -> QScrollArea:
    """A tab page that is one widget with its own heading, in a scroll area so the lower part is never hidden."""
    scroll = QScrollArea(parent)
    scroll.setWidgetResizable(True)
    scroll.setWidget(widget)
    return scroll


def _zotero_projects(store: SettingsStore, sources: DataSources) -> Callable[[], list[tuple[str, str, str]]]:
    """The project list the Zotero tab's scope reads. When no list can be read, the reason reaches the tab as text."""
    read = _saved_projects(store, sources)

    def scoped() -> list[tuple[str, str, str]]:
        try:
            return read()
        except ProjectsUnavailable as exc:
            raise zotero_client.ZoteroError("projects_unavailable", str(exc)) from None

    return scoped


def _saved_projects(store: SettingsStore, sources: DataSources) -> Callable[[], list[tuple[str, str, str]]]:
    """The projects the Projects tab resolves, as (id, name, parent id) triples, through the same resolver and Settings.

    The parent id is "" for a top-level project. The Sessions tab uses it to group a project's subprojects under it.
    When the resolver found no source to read, the provider raises ProjectsUnavailable. The Sessions tab then
    says the projects could not be read, rather than that no projects exist.
    """

    def read() -> list[tuple[str, str, str]]:
        outcome = resolve(store.load().settings, sources)
        if outcome.state != STATE_OK:
            raise ProjectsUnavailable(outcome.message)
        triples: list[tuple[str, str, str]] = []
        for row in outcome.rows:
            pid = row.get("id")
            if isinstance(pid, str) and pid.strip():
                name = row.get("name")
                parent = row.get("parent_project_id")
                triples.append(
                    (
                        pid,
                        name if isinstance(name, str) else "",
                        parent if isinstance(parent, str) else "",
                    )
                )
        return triples

    return read


def _saved_tools_status(store: SettingsStore, probe: Callable[[str], str]) -> Callable[[], ToolsStatus]:
    """One tools read, through the core Python and checkout saved in Settings. Runs on the worker thread.

    The local server row asks ``probe``, the window's own probe, the same one Home's local card and the connection bar
    use. A window built with fake sources therefore never opens a socket from this read.
    The tunnel row uses only the status call of a RunnerClient built from those two settings. With either one
    unset, that call is None and the tunnel row says Not checked. No tool is started or stopped.
    """

    def read() -> ToolsStatus:
        settings = store.load().settings
        tunnel_call = None
        if settings.core_python.strip() and settings.core_checkout.strip():
            client = RunnerClient(python=settings.core_python, cwd=settings.core_checkout)
            tunnel_call = client.status
        return read_tools_status(store, probe=probe, tunnel_status_call=tunnel_call)

    return read


class _ConfiguredRunner:
    """The runner's read-only status call, through the core Python and folder saved in Settings.

    The client is built on each call, so a change in Settings is used at once. With no core Python
    saved, the client reports not available and starts nothing. This class has no start, stop or
    restart method, and the client never passes one of those verbs.
    """

    def __init__(self, settings_store: SettingsStore) -> None:
        self._store = settings_store

    def status(self, scope: str) -> RunnerResult:
        settings = self._store.load().settings
        client = RunnerClient(python=settings.core_python or None, cwd=settings.core_checkout or None)
        return client.status(scope)


class MainWindow(QMainWindow):
    """Mode selector above the tabs: Home, Projects, Sessions, GPUs, Search, Tunnel, Keys and Settings."""

    def __init__(
        self,
        settings_store: SettingsStore | None = None,
        secret_store: SecretStore | None = None,
        data_sources: DataSources | None = None,
        zotero: Any | None = None,
        tunnel_runner: Any | None = None,
        tools_status: Callable[[], ToolsStatus] | None = None,
        update_transport: updater.Transport | None = None,
        runner_actions: RunnerActions | None = None,
        update_root: Path | None = None,
        record_apps_entry: updater.AppsEntryWriter | None = None,
        connection_transport: Transport | None = None,
        restart_watch: RestartWatch | None = None,
    ) -> None:
        super().__init__()
        self._store = settings_store if settings_store is not None else SettingsStore()
        self._secrets = secret_store if secret_store is not None else KeyringSecretStore()
        # The Projects tab reads through these. Tests pass fakes. The app uses the real sources.
        self._sources = data_sources if data_sources is not None else default_sources(self._secrets)
        # The connection bar's hosted check (GET /me) and its local probe. Tests pass a fake transport. None is the live one.
        self._connection_transport = connection_transport
        # The Zotero cards and panel call this object. Tests pass a fake. The app uses the zotero_client module.
        self._zotero = zotero if zotero is not None else zotero_client
        # The tunnel status call (a status(scope) method). Tests pass a fake. The app builds the client
        # from the core Python and folder in Settings. The status call never starts or stops anything. The
        # Start, Stop and Restart controls use runner_actions instead, and they stay off until the owner turns them on.
        self._tunnel_runner = tunnel_runner if tunnel_runner is not None else _ConfiguredRunner(self._store)
        # What this window runs. The app takes this baseline before the first-run dialog, so it matches what was loaded at
        # launch. Tests pass a watch with fake readers.
        self._restart_watch = restart_watch if restart_watch is not None else RestartWatch()
        # The runner controls (the Tunnel panel and the Settings runner block). Tests pass fakes. The app reads Settings.
        self._runner_actions = runner_actions if runner_actions is not None else RunnerActions(self._store)
        # A local start that the owner confirms through these controls hands its pid to the window's watch. Any real
        # RunnerActions gets this, built here or handed in. A fake without the method is left alone.
        if isinstance(self._runner_actions, RunnerActions):
            self._runner_actions.set_on_started(self._restart_watch.note_server_started)
        # The tools read (a no-argument callable that returns ToolsStatus). Tests pass a fake. The app reads
        # the core Python and folder in Settings. It starts and stops nothing.
        self._tools_status_read = (
            tools_status if tools_status is not None else _saved_tools_status(self._store, self._sources.probe)
        )
        # Each tunnel check takes the next number. A result with an older number is dropped.
        self._tunnel_generation = 0
        # A plain token that identifies this window's checks. A result carries it, so the window keeps only its own.
        self._owner = object()
        shared_delivery().delivered.connect(self._on_delivered)
        self.setWindowTitle("Meridian")

        central = QWidget(self)
        layout = QVBoxLayout(central)
        # The connection bar is the first strip in the window, above the mode selector. It is built before the tabs,
        # because Settings connects its sign-in signal to it.
        self._connection_bar = ConnectionBar(
            settings_store=self._store,
            secrets=self._secrets,
            probe=self._sources.probe,
            transport=self._connection_transport,
            parent=central,
        )
        layout.addWidget(self._connection_bar)
        layout.addLayout(self._build_mode_bar(central))
        # Hidden until the code or the local server changes on disk. Dismiss hides it, and it returns only for a new change.
        self._restart_banner = RestartBanner(self._restart_watch, parent=central)
        layout.addWidget(self._restart_banner)

        # The status-bar notice holds the one update check of this window, and Home's Updates card reads its result, so
        # it is built before the tabs and added to the status bar below. The bar is hidden unless a release is downloading
        # or staged. A check runs at start and every six hours after, only while auto_update and check_updates are on. No
        # transport means no request is made, and no update_root means nothing is staged or restarted.
        self._update_notice = UpdateNoticeBar(
            APP_VERSION,
            update_transport,
            install_root=update_root,
            enabled=_updates_allowed_reader(self._store),
            report=self.statusBar().showMessage,
            parent=self,
            record_apps_entry=record_apps_entry,
        )

        self.tabs = QTabWidget(central)
        for name, _wave in TAB_SPECS:
            if name == "Home":
                home = HomeTab(self._home_deps(), parent=self.tabs)
                home.settingsRequested.connect(self._show_settings)
                home.tunnelRequested.connect(self._show_tunnel)
                home.tunnelChanged.connect(self._on_tunnel_changed)
                self._home_page = home
                page: QWidget = home
            elif name == "Projects":
                self._projects = ProjectsTab(self._store, self._sources, parent=self.tabs)
                page = self._projects
            elif name == "Sessions":
                # Nothing is read on build. The first show and the Refresh button read the project list and the
                # sessions. While the tab stays visible, a 5-second timer reads only the local server, and only in
                # local mode. The providers run on the worker thread, never on the UI thread, and hold no window.
                page = SessionsPanel(
                    _saved_local_url(self._store),
                    _saved_hosted(self._store, self._secrets),
                    _saved_projects(self._store, self._sources),
                    _saved_mode(self._store),
                    parent=self.tabs,
                    only_active=_saved_only_active(self._store),
                    on_only_active_changed=_save_only_active(self._store),
                )
            elif name == "GPUs":
                # Nothing is read on build. The first show and the Refresh button read the GPUs. While the tab stays
                # visible, a 5-second timer reads them again. The saved SSH hosts and the RunPod list are read on the worker thread.
                page = GpuJobsPanel(
                    _saved_gpu_hosts(self._store),
                    _saved_runpod_pods(self._store),
                    parent=self.tabs,
                )
            elif name == "Tunnel":
                # The tunnel's status, Start, Stop and Restart (the Home card has its own Start), the lanes, the tool groups and the custom
                # connectors. Its status is read when the tab is first shown. Settings no longer holds a copy.
                self._tunnel_panel = TunnelPanel(self._store, self._runner_actions, self._tunnel_runner.status, parent=self.tabs)
                # The panel has its own heading. The scroll area keeps its lower part reachable, as Zotero and LaTeX are.
                page = _scroll_page(self._tunnel_panel, self.tabs)
                self._tunnel_page = page
            elif name == "Search":
                # Nothing is searched on build or show. Search or Enter reads the Local URL at that moment.
                page = SearchTab(_saved_local_url(self._store), parent=self.tabs)
            elif name == "Zotero":
                # Zotero is a tab of its own: the library search, the save bridge, and the project folder.
                page = _page_with_label(
                    ZOTERO_SECTION,
                    ZoteroPanel(client=self._zotero, projects=_zotero_projects(self._store, self._sources), parent=self.tabs),
                    self.tabs,
                )
            elif name == "LaTeX":
                # The LaTeX engine probe is a tab of its own. It makes no request until the tab is first shown.
                page = _page_with_label(LATEX_SECTION, LatexPanel(self._store, parent=self.tabs), self.tabs)
            elif name == "Keys":
                # The RunPod card and one card per named key. Its RunPod probe runs when the tab is first shown.
                page = KeysTab(self._store, self._secrets, parent=self.tabs)
            else:
                page = self._build_settings_page()
                self._settings_page = page
            self.tabs.addTab(page, name)
        layout.addWidget(self.tabs)
        self.setCentralWidget(central)
        # Qt hides tabs behind scroll arrows when the bar is narrower than its labels. The window
        # may not shrink below the full bar, so every tab label stays visible.
        self.tabs.setMinimumWidth(self.tabs.tabBar().sizeHint().width())
        # The tab bar takes no wheel turns, so the wheel scrolls the page and never moves between tabs.
        self._tab_wheel = TabBarWheelBlocker(self)
        self.tabs.tabBar().installEventFilter(self._tab_wheel)
        _fit_and_center(self, DEFAULT_WINDOW_SIZE)

        self.statusBar().showMessage(STATUS_MESSAGE)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.statusBar().addPermanentWidget(self._update_notice)
        self._update_notice.start()
        self._connection_bar.start()
        # A saved Hosted mode starts its tunnel status check now. Local mode only shows its description.
        self._apply_mode_view(self._saved_mode)
        # The core search at start, and any later search or chosen core folder, changes what the tunnel check can reach.
        # A Hosted mode window checks again once a search has finished.
        self._runner_actions.coreChanged.connect(self._on_core_changed)

    @Slot()
    def _on_core_changed(self) -> None:
        """Re-check the tunnel in Hosted mode once the core search has finished. Local mode does nothing."""
        if self._runner_actions.locating or self._saved_mode != HOSTED_MODE:
            return
        self._start_tunnel_check()

    # ----------------------------------------------------------- building

    def _home_deps(self) -> HomeDeps:
        """What the Home cards read and do. Each one is the window's own call, so tests and the app use the same wiring."""
        return HomeDeps(
            store=self._store,
            probe=self._sources.probe,
            tunnel=self._tunnel_runner.status,
            actions=self._runner_actions,
            zotero=self._zotero,
            tools=self._tools_status_read,
            updates=self._update_notice,
            current_version=APP_VERSION,
        )

    def _build_settings_page(self) -> QWidget:
        """Settings: the connection settings, the Writing area, then Diagnostics. Zotero and LaTeX have their own tabs.

        The page scrolls, so the longest section never hides the others. The keys are a tab of their own (Keys).
        """
        container = QWidget(self.tabs)
        container.setObjectName("settings-page")
        column = QVBoxLayout(container)
        self._settings_tab = SettingsTab(
            self._store,
            secrets=self._secrets,
            parent=container,
            actions=self._runner_actions,
            tools_status=self._tools_status_read,
        )
        # A sign-in or sign-out in Settings checks the top bar again at once.
        self._settings_tab.signedInChanged.connect(self._connection_bar.refresh)
        self._connection_bar.signInRequested.connect(self._sign_in_from_bar)
        column.addWidget(self._settings_tab)
        # The Writing area sits in Settings, not as a tab: DOCX and Slides, each Not connected with its reason.
        column.addWidget(make_selectable(QLabel(WRITING_SECTION, container)))
        column.addWidget(WritingArea(container))
        column.addStretch(1)

        scroll = QScrollArea(self.tabs)
        scroll.setObjectName("settings-scroll")
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        return scroll

    def _build_mode_bar(self, parent: QWidget) -> QVBoxLayout:
        result = self._store.load()
        # The mode the selector last saved, or loaded at start. A failed save reverts to it.
        self._saved_mode = result.settings.mode
        bar = QHBoxLayout()
        bar.addWidget(make_selectable(QLabel("Mode:", parent)))

        self.mode_selector = QComboBox(parent)
        self.mode_selector.setObjectName("mode-selector")
        for value, label in MODE_ITEMS:
            self.mode_selector.addItem(label, value)
        self.mode_selector.setCurrentIndex(self.mode_selector.findData(result.settings.mode))
        self.mode_selector.currentIndexChanged.connect(self._on_mode_changed)
        bar.addWidget(self.mode_selector)

        # The one-line tunnel status. It is shown only in Hosted mode (see _apply_mode_view).
        self.tunnel_status_label = make_selectable(QLabel(parent))
        self.tunnel_status_label.setObjectName("tunnel-mode-status")
        self.tunnel_status_label.setTextFormat(Qt.TextFormat.PlainText)
        bar.addWidget(self.tunnel_status_label)

        self.settings_warning = make_selectable(QLabel(parent))
        self.settings_warning.setObjectName("settings-warning")
        if result.warning:
            self.settings_warning.setText(f"Settings: {result.warning}")
        else:
            self.settings_warning.hide()
        bar.addWidget(self.settings_warning, 1)

        # The chosen mode's description. In Hosted mode, the detail line names the tools that need the
        # tunnel and, when the tunnel is not running, how to start it by hand.
        self.mode_description = make_selectable(QLabel(parent))
        self.mode_description.setObjectName("mode-description")
        self.mode_description.setTextFormat(Qt.TextFormat.PlainText)
        self.mode_description.setWordWrap(True)
        self.tunnel_detail = make_selectable(QLabel(parent))
        self.tunnel_detail.setObjectName("tunnel-mode-detail")
        self.tunnel_detail.setTextFormat(Qt.TextFormat.PlainText)
        self.tunnel_detail.setWordWrap(True)

        column = QVBoxLayout()
        column.addLayout(bar)
        column.addWidget(self.mode_description)
        column.addWidget(self.tunnel_detail)
        return column

    # ----------------------------------------------------------- tabs

    def _show_settings(self) -> None:
        """Switch to the Settings tab. Home's Settings buttons use this."""
        self.tabs.setCurrentWidget(self._settings_page)

    @Slot()
    def _sign_in_from_bar(self) -> None:
        """The top bar's sign-in button: show Settings and start the same browser sign-in as its own button."""
        self._show_settings()
        self._settings_tab.start_sign_in()

    def _show_tunnel(self) -> None:
        """Switch to the Tunnel tab. Home's Open the Tunnel panel button uses this."""
        self.tabs.setCurrentWidget(self._tunnel_page)

    @Slot()
    def _on_tunnel_changed(self) -> None:
        """A Start from the Home tunnel card has finished. Read the Tunnel tab and the top-bar line again, so all three agree."""
        panel = getattr(self, "_tunnel_panel", None)
        if panel is not None:
            panel.check()
        if self._saved_mode == HOSTED_MODE:
            self._start_tunnel_check()

    @Slot(int)
    def _on_tab_changed(self, index: int) -> None:
        """Home's checks line is Home's own. It shows while Home is the tab, and any other tab clears it, so it never sticks.

        A line another part of the window set, such as a launch note or an update notice, is left alone when Home is chosen.
        """
        status = self.statusBar()
        if self.tabs.widget(index) is self._home_page:
            if not status.currentMessage():
                status.showMessage(STATUS_MESSAGE)
        elif status.currentMessage() == STATUS_MESSAGE:
            status.clearMessage()

    # ----------------------------------------------------------- mode

    def _on_mode_changed(self, index: int) -> None:
        mode = self.mode_selector.itemData(index)
        try:
            self._store.set_mode(mode)
        except (OSError, ValueError) as exc:
            self._revert_mode_selector()
            self.settings_warning.setText(f"Settings: could not save mode ({exc.__class__.__name__})")
            self.settings_warning.show()
            return
        self._saved_mode = mode
        self.settings_warning.hide()
        # The Settings tab shows the saved mode, so it reads the new one too.
        self._settings_tab.refresh()
        # The Projects tab reads from the source the new mode names.
        self._projects.refresh()
        self._apply_mode_view(mode)

    def _apply_mode_view(self, mode: str) -> None:
        """Show the description of ``mode``, and the tunnel lines only in Hosted mode.

        Switching to Hosted mode starts one read-only status check. Nothing here starts, stops or
        restarts the tunnel.
        """
        self.mode_description.setText(MODE_DESCRIPTIONS.get(mode, ""))
        shows_tunnel = mode == HOSTED_MODE
        self.tunnel_status_label.setVisible(shows_tunnel)
        self.tunnel_detail.setVisible(shows_tunnel)
        if shows_tunnel:
            self._start_tunnel_check()
        else:
            # A check still running once the mode has left Hosted is dropped when it finishes.
            self._tunnel_generation += 1

    def _start_tunnel_check(self) -> None:
        """One read-only tunnel status call on the thread pool. The UI thread never waits on it.

        The job holds the runner, this window's owner token and the shared delivery object, never the window. Its
        result comes back to _on_delivered on the GUI thread. A window that is closed while the check runs is never
        touched by the worker.
        """
        self._tunnel_generation += 1
        generation = self._tunnel_generation
        runner = self._tunnel_runner
        owner = self._owner
        delivery = shared_delivery()
        self.tunnel_status_label.setText(BAR_CHECKING_TEXT)
        self.tunnel_detail.setText(DEPENDENCY_TEXT)
        QThreadPool.globalInstance().start(
            TunnelCheckJob(
                lambda: read_tunnel_status(runner.status),
                lambda payload: delivery.delivered.emit(Delivered(owner, (generation, payload))),
            )
        )

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        """Runs on the GUI thread. A result for another window, an older check, or one from before a mode change is dropped."""
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        generation, (status, failure) = item.value
        if generation != self._tunnel_generation or self._saved_mode != HOSTED_MODE:
            return  # an older check, or the mode changed after this check started
        line, detail = bar_texts(status, failure)
        self.tunnel_status_label.setText(line)
        self.tunnel_detail.setText(detail)

    def show_launch_notes(self, text: str) -> None:
        """Show what the launch-time starts did, on the status bar. Called on the GUI thread by ui/runner_actions.LaunchStarts."""
        self.statusBar().showMessage(text)

    def _revert_mode_selector(self) -> None:
        """Show the last saved mode again. Signals stay blocked, so the change does not save a second time."""
        self.mode_selector.blockSignals(True)
        try:
            self.mode_selector.setCurrentIndex(self.mode_selector.findData(self._saved_mode))
        finally:
            self.mode_selector.blockSignals(False)
