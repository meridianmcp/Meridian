"""The Settings tab: seven collapsible groups in one scrolling column, in this order.

1. Connection: the mode (shown, set by the top-bar selector), the hosted URL, the hosted sign-in (Sign in with browser
   and Sign out), the Test connection and Try again buttons, the local URL and the database file.
2. Local server and startup: the runner block (ui/runner_section.py), its state and Start, Stop and Restart, the two
   launch-time switches, and the automatic update checkbox. Each control asks the owner first, except the update checkbox.
3. Client setup: the Meridian core checkout and its Python, then one row per client (ui/client_setup_section.py).
4. MCP tools: one folding group per tool group, with a switch for each tool and for each group (ui/tools_section.py).
   Every tool is on by default. The choice is saved in the settings file and applied at the next start.
5. Connections: the owner's own MCP connectors, each with an on switch, a tunnel switch and Remove, and a form that adds one
   (ui/connections_section.py). Written to Claude Desktop and Codex at the next start. Nothing reaches claude.ai from here.
6. GPU hosts over SSH: the SSH host names the GPUs tab reads. Only names are saved.
7. Limits: one slider for each owner limit, with its saved value beside it (ui/limits_section.py). Each value is kept in
   the settings file and clamped to its range there. Enforcement happens on the server and is not active yet.
   It starts folded.
8. Diagnostics (logs): the settings file path, any note from loading it, and the Logs view (ui/logs_page.py).
   It starts folded. The Logs view reads on its first show, and its Refresh reads again.

The keys are not here. The Keys tab (ui/keys_tab.py) is a tab of its own: the RunPod entry card and one card per named key.

The hosted URL is checked with hosted_client.validate_base_url before anything is written. Sign in with browser runs the
server's device flow (hosted_session.sign_in) on a worker thread: it shows the sign-in code, opens the browser, and saves
the token to the keychain through token_store when the owner approves. No token is typed or pasted. The tab shows only
whether a token is set. Sign out removes the token and the saved session. Test connection makes one GET /projects with
the saved URL and token, on a worker thread, and shows the result in plain words. Try again runs the same test, from
the button beside it.

The local URL is checked with local_client.assert_loopback before anything is written. A value that is not an http
loopback URL is refused inline, and the settings file is not touched. The database file is picked with a file dialog, and
only its path is saved.

The mode is shown, not edited. The top-bar selector is the one control that changes it, and the main window calls
refresh() after each save, so both always show the same saved mode.

"""

from __future__ import annotations

import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QObject, QRunnable, QSize, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import hosted_client, hosted_session, projects_backup, token_store
from meridian_desktop.hosted_client import BadBaseUrl, HostedError
from meridian_desktop.local_client import LocalClientError, assert_loopback
from meridian_desktop.secret_store import KeyringSecretStore, SecretStore
from meridian_desktop.settings_store import DEFAULT_HOSTED_URL, DEFAULT_LOCAL_URL, SettingsStore, SettingsUnreadable
from meridian_desktop.ssh_config import read_host_names as read_ssh_config_names
from meridian_desktop.token_store import TokenStoreError
from meridian_desktop.tools_status import ToolsStatus
from meridian_desktop.ui.client_setup_section import ClientSetupSection
from meridian_desktop.ui.delivery import Delivered, Delivery, shared_delivery
from meridian_desktop.ui.limits_section import LIMITS_TITLE, LimitsSection
from meridian_desktop.ui.logs_page import LogsPage
from meridian_desktop.ui.runner_actions import RunnerActions
from meridian_desktop.ui.runner_section import RunnerSection
from meridian_desktop.ui.connections_section import CONNECTIONS_TITLE, ConnectionsSection
from meridian_desktop.ui.settings_group import CollapsibleGroup
from meridian_desktop.ui.tools_panel import ToolsPanel
from meridian_desktop.ui.tools_section import TOOLS_TITLE, ToolsSection
from meridian_desktop.ui.text import make_selectable

# Mode names in display order: (settings value, label). The top-bar selector uses this list too. The tunnel belongs to
# Hosted, so it has no entry of its own.
MODE_ITEMS = (("hosted", "Hosted"), ("local", "Local"))
MODE_LABELS = dict(MODE_ITEMS)

# The width the Settings column never shrinks below, so a label wraps instead of being cut off.
CONTENT_MIN_WIDTH = 640
CONNECTION_TITLE = "Connection"
LOCAL_SERVER_TITLE = "Local server and startup"
CLIENT_SETUP_TITLE = "Client setup"
DIAGNOSTICS_TITLE = "Diagnostics (logs)"
# The one place for diagnostics. The LaTeX section label is the one main_window shows and tests look for.
LATEX_SECTION = "LaTeX (local engine on 127.0.0.1:8471)"
TOOLS_SECTION = "Tools (full table)"
DIAGNOSTICS_INTRO = (
    "Read-only views for finding a fault: the file path, the full tools table, the LaTeX engine probe, and the desktop, "
    "control, tunnel and runner logs. Nothing here starts a service."
)
SETTINGS_FILE_TEXT = "Settings file: {path}"
LOAD_WARNING_TEXT = "Note from the settings file: {warning}"

DB_DIALOG_TITLE = "Choose the Meridian database file"
DB_FILE_FILTER = "SQLite database (*.db *.sqlite *.sqlite3);;All files (*)"
NOT_CHOSEN_TEXT = "No database file chosen yet."
LOCAL_URL_SAVED_TEXT = "Local URL saved."
DB_SAVED_TEXT = "Database file saved."
NOT_WRITTEN_TEXT = "Not saved: the settings file could not be written"
HOSTED_URL_SAVED_TEXT = "Hosted URL saved."
GPU_HOSTS_TITLE = "GPU hosts over SSH"
GPU_HOSTS_INTRO = (
    "SSH host names from your ssh config, separated by commas, for example gpu-box, lab-a100. The GPUs tab reads each "
    "one in batch mode: no password prompt, and an unknown host key is refused. The ssh config is read only when you press "
    "Add names from my ssh config. That adds the Host names it finds to this box, and nothing is saved until you press Save."
)
GPU_HOSTS_SAVED_TEXT = "Saved. The GPUs tab reads these hosts on its next refresh."
GPU_HOSTS_CLEARED_TEXT = "Saved. No SSH hosts are read by the GPUs tab."
ADD_SSH_BUTTON_TEXT = "Add names from my ssh config"
SSH_NONE_FOUND_TEXT = "No host names were found in your ssh config file, so nothing was added."
SSH_ADDED_TEXT = "Added {count} name(s) from your ssh config: {names}. Press Save SSH hosts to keep them."
TOKEN_SET_TEXT = "Hosted token: set"
TOKEN_NOT_SET_TEXT = "Hosted token: not set"
SIGN_IN_STARTING_TEXT = "Starting the sign-in with Hosted Meridian..."
SIGN_IN_OPENED_TEXT = "Approve the sign-in in the browser that opened. It shows the code {code}. Waiting up to five minutes."
SIGN_IN_NO_BROWSER_TEXT = "The browser did not open. Go to {uri} and check that it shows the code {code}."
SIGN_IN_DONE_TEXT = "Signed in as {account}. The token is in the keychain."
SIGN_IN_DONE_NO_NAME_TEXT = "Signed in. The account name shows once Hosted Meridian answers."
SIGN_IN_FAILED_TEXT = "Not signed in. {reason}"
SIGN_IN_UNEXPECTED_TEXT = "Sign-in failed ({name}). Nothing was saved."
TOKEN_REMOVED_TEXT = "Signed out. The hosted token was removed from the keychain."
TOKEN_NOTHING_REMOVED_TEXT = "No hosted token was saved, so there was nothing to sign out of."
TEST_RUNNING_TEXT = "Testing the saved hosted URL and token..."
TEST_CONNECTED_TEXT = "Connected to {url}. Hosted Meridian returned {count} project(s)."
TEST_UNEXPECTED_TEXT = "Connection test failed ({name})."

# A file chooser takes (parent, start directory) and returns the chosen path, or "" when cancelled.
FileChooser = Callable[[QWidget, str], str]
# The hosted list call: (hosted URL, token or None) -> the project rows. hosted_client.list_projects by default.
ListHostedFn = Callable[[str, str | None], list[Any]]


def mode_text(mode: str) -> str:
    """The saved mode as this tab shows it."""
    return f"Mode: {MODE_LABELS.get(mode, mode)} (set by the Mode selector above)"


def describe_test_failure(exc: BaseException) -> str:
    """Plain words for a failed connection test.

    A HostedError carries fixed text, with no token, body or raw exception. Any other error is
    named by its class only, so its text (which may hold a value) is never shown.
    """
    if isinstance(exc, HostedError):
        return str(exc)
    return TEST_UNEXPECTED_TEXT.format(name=exc.__class__.__name__)


def _choose_database_file(parent: QWidget, start_dir: str) -> str:
    """Ask for an existing file. Return its path, or "" when the dialog is cancelled."""
    path, _selected_filter = QFileDialog.getOpenFileName(parent, DB_DIALOG_TITLE, start_dir, DB_FILE_FILTER)
    return path


class _FullHeightScroll(QScrollArea):
    """A scroll area that is as tall as its content, so the page around it does the scrolling.

    QScrollArea's own size hint is capped at 24 lines of text. Inside the Settings page that cap made this column
    336 px tall, so the Client setup result line and the Diagnostics group were cut off behind a second, small scrollbar.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Never taller than the content, so the space left over in the page goes to the RunPod card, not to this box.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

    def setWidget(self, widget: QWidget) -> None:  # noqa: N802 - Qt naming
        super().setWidget(widget)
        widget.installEventFilter(self)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        widget = self.widget()
        if widget is None:
            return super().sizeHint()
        hint = widget.sizeHint()
        # Wrapped labels are taller at the width the page gives them, so the height is read for that width.
        width = self.viewport().width() or hint.width()
        wrapped = widget.heightForWidth(width) if widget.hasHeightForWidth() else -1
        frame = 2 * self.frameWidth()
        return QSize(hint.width() + frame, max(hint.height(), wrapped) + frame)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        # The parent layout caches the hint, so a new width or new content must tell it to ask again.
        self.updateGeometry()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt naming
        if watched is self.widget() and event.type() in (QEvent.Type.LayoutRequest, QEvent.Type.Resize):
            self.updateGeometry()
        return super().eventFilter(watched, event)


def _label(parent: QWidget, name: str, text: str = "") -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


def _caption(parent: QWidget, text: str) -> QLabel:
    """A short caption at the start of a row. It never wraps, so the column is as wide as the caption."""
    label = QLabel(text, parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(False)
    return make_selectable(label)


def _set_message(label: QLabel, text: str) -> None:
    """Show a message line, or hide the line when the text is empty."""
    label.setText(text)
    label.setVisible(bool(text))


@dataclass(frozen=True)
class _TestDone:
    """One finished connection test, as its job hands it back: its generation and the plain-words text to show."""

    generation: int
    text: str


class _ConnectionCheck:
    """The hosted connection test, run on the worker thread through the settings store, the keychain and the list call.

    It holds no widget. A test still running when the tab is deleted therefore never keeps the tab alive, and never
    touches it. The tab holds one of these and hands its ``run`` method to the thread pool.
    """

    def __init__(self, settings_store: SettingsStore, secrets: SecretStore, list_hosted: ListHostedFn) -> None:
        self._store = settings_store
        self._secrets = secrets
        self._list_hosted = list_hosted

    def run(self) -> str:
        """Runs on a worker. One GET /projects with the saved URL and token. Returns the text to show."""
        url = self._store.load().settings.hosted_url
        token = token_store.get_token(store=self._secrets)
        try:
            rows = self._list_hosted(url, token)
        except Exception as exc:  # noqa: BLE001 - shown as plain words, never as the raw exception
            return describe_test_failure(exc)
        return TEST_CONNECTED_TEXT.format(url=url, count=len(rows))


class _ConnectionTestJob(QRunnable):
    """Run one connection test on the thread pool and deliver a _TestDone to the shared delivery object.

    The job holds only the work, the owner token, the generation and the shared delivery object. It never holds the tab.
    """

    def __init__(self, generation: int, work: Callable[[], str], owner: object, delivery: Delivery) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._generation = generation
        self._work = work
        self._owner = owner
        self._delivery = delivery

    def run(self) -> None:
        try:
            text = self._work()
        except Exception as exc:  # noqa: BLE001 - a worker must never raise into the pool
            text = describe_test_failure(exc)
        self._delivery.delivered.emit(Delivered(self._owner, _TestDone(self._generation, text)))


@dataclass(frozen=True)
class _SignInEvent:
    """One step of a browser sign-in, from its job. done False carries the code to show; done True the final outcome."""

    generation: int
    done: bool
    ok: bool
    text: str


def _try_open(open_browser: Callable[[str], bool], uri: str) -> bool:
    """Open the browser at uri. False when it did not open, or the opener raised."""
    try:
        return bool(open_browser(uri))
    except Exception:  # noqa: BLE001 - a browser that will not open is reported, never raised
        return False


class _SignInJob(QRunnable):
    """Run the browser sign-in on the thread pool. Each step goes to the shared delivery object as a _SignInEvent.

    The job holds the hosted URL, the secret store, the transport, the opener, the timing functions, the owner token,
    the generation and the shared delivery object. It never holds the tab. The events carry the user code and fixed
    wording only, never the device code or a token.
    """

    def __init__(
        self,
        generation: int,
        base_url: str,
        secrets: SecretStore,
        transport: hosted_client.Transport | None,
        open_browser: Callable[[str], bool],
        sleep: Callable[[float], None],
        clock: Callable[[], float],
        owner: object,
        delivery: Delivery,
    ) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._generation = generation
        self._base_url = base_url
        self._secrets = secrets
        self._transport = transport
        self._open_browser = open_browser
        self._sleep = sleep
        self._clock = clock
        self._owner = owner
        self._delivery = delivery

    def _emit(self, event: _SignInEvent) -> None:
        try:
            self._delivery.delivered.emit(Delivered(self._owner, event))
        except RuntimeError:
            pass  # the shared delivery object is gone, for example at shutdown

    def run(self) -> None:
        def announce(auth: hosted_session.DeviceAuth) -> None:
            self._emit(_SignInEvent(self._generation, False, True, SIGN_IN_OPENED_TEXT.format(code=auth.user_code)))
            if not _try_open(self._open_browser, auth.verification_uri):
                text = SIGN_IN_NO_BROWSER_TEXT.format(uri=auth.verification_uri, code=auth.user_code)
                self._emit(_SignInEvent(self._generation, False, True, text))

        try:
            session = hosted_session.sign_in(
                self._base_url,
                secrets=self._secrets,
                transport=self._transport,
                announce=announce,
                sleep=self._sleep,
                clock=self._clock,
            )
        except hosted_session.SignInError as exc:
            outcome = _SignInEvent(self._generation, True, False, SIGN_IN_FAILED_TEXT.format(reason=str(exc)))
        except HostedError as exc:
            outcome = _SignInEvent(self._generation, True, False, SIGN_IN_FAILED_TEXT.format(reason=str(exc)))
        except TokenStoreError as exc:
            # TokenStoreError text names the step and the error class only, never the token.
            outcome = _SignInEvent(self._generation, True, False, f"Not saved. {exc}")
        except Exception as exc:  # noqa: BLE001 - a worker never raises into the pool; only the class name is shown
            outcome = _SignInEvent(self._generation, True, False, SIGN_IN_UNEXPECTED_TEXT.format(name=exc.__class__.__name__))
        else:
            text = SIGN_IN_DONE_TEXT.format(account=session.account) if session.account else SIGN_IN_DONE_NO_NAME_TEXT
            outcome = _SignInEvent(self._generation, True, True, text)
        self._emit(outcome)


class SettingsTab(QWidget):
    """The Settings groups: connection, local server and startup, client setup, MCP tools, connections, GPU hosts and diagnostics.

    The keys are a tab of their own (ui/keys_tab.py), so this tab holds none of them.
    """

    # Emitted on the GUI thread after a sign-in is saved or a sign-out removes the token. The main window's top bar
    # listens, so it checks again at once.
    signedInChanged = Signal()

    def __init__(
        self,
        settings_store: SettingsStore,
        parent: QWidget | None = None,
        choose_database: FileChooser = _choose_database_file,
        secrets: SecretStore | None = None,
        list_hosted: ListHostedFn | None = None,
        actions: RunnerActions | None = None,
        tools_status: Callable[[], ToolsStatus] | None = None,
        sign_in_transport: hosted_client.Transport | None = None,
        open_browser: Callable[[str], bool] = webbrowser.open,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("settings-tab")
        self._store = settings_store
        self._choose_database = choose_database
        self._secrets = secrets if secrets is not None else KeyringSecretStore()
        self._list_hosted = list_hosted if list_hosted is not None else hosted_client.list_projects
        # The sign-in's transport, opener and timing. Tests pass fakes. The app passes none, so the live transport is used.
        self._sign_in_transport = sign_in_transport
        self._open_browser = open_browser
        self._sign_in_sleep = sleep
        self._sign_in_clock = clock
        self._sign_in_generation = 0
        self._actions = actions if actions is not None else RunnerActions(settings_store)
        # The one tools read the full tools table shows. None leaves the table out (a tab built without the window).
        self._tools_status = tools_status
        self._check = _ConnectionCheck(settings_store, self._secrets, self._list_hosted)
        # Identifies this tab's connection tests. A plain object, so a job can hold it without holding the tab.
        self._owner = object()
        self._generation = 0
        shared_delivery().delivered.connect(self._on_delivered)
        self._build()
        self.refresh()

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = _FullHeightScroll(self)
        scroll.setObjectName("settings-scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)

        content = QWidget(scroll)
        content.setObjectName("settings-content")
        content.setMinimumWidth(CONTENT_MIN_WIDTH)
        column = QVBoxLayout(content)
        column.setContentsMargins(4, 4, 12, 4)
        column.setSpacing(6)
        scroll.setWidget(content)

        column.addWidget(self._build_connection(content))
        column.addWidget(self._build_local_server(content))
        column.addWidget(self._build_client_setup(content))
        column.addWidget(self._build_tools(content))
        column.addWidget(self._build_connections(content))
        column.addWidget(self._build_gpu_hosts(content))
        column.addWidget(self._build_limits(content))
        column.addWidget(self._build_diagnostics(content))
        column.addStretch(1)

        self._wire()

    def _build_connection(self, parent: QWidget) -> CollapsibleGroup:
        group = CollapsibleGroup("connection", CONNECTION_TITLE, parent)
        body = group.body()
        settings = self._store.load().settings

        self._mode = _label(body, "settings-mode")
        group.add_widget(self._mode)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)

        # Hosted URL: the URL, then its message.
        grid.addWidget(_caption(body, "Hosted URL:"), 0, 0)
        self._hosted_url = QLineEdit(settings.hosted_url, body)
        self._hosted_url.setObjectName("settings-hosted-url")
        self._hosted_url.setPlaceholderText(DEFAULT_HOSTED_URL)
        grid.addWidget(self._hosted_url, 0, 1)
        self._save_hosted_url = QPushButton("Save hosted URL", body)
        self._save_hosted_url.setObjectName("settings-save-hosted-url")
        grid.addWidget(self._save_hosted_url, 0, 2)
        self._hosted_url_message = _label(body, "settings-hosted-url-message")
        self._hosted_url_message.setVisible(False)
        grid.addWidget(self._hosted_url_message, 1, 0, 1, 3)

        # Hosted sign-in: the status line, then Sign in with browser and Sign out. The code to approve and the result
        # are shown under them. The token value is never shown.
        self._token_status = _label(body, "settings-token-status")
        grid.addWidget(self._token_status, 2, 0, 1, 4)
        grid.addWidget(_caption(body, "Account:"), 3, 0)
        self._sign_in = QPushButton("Sign in with browser", body)
        self._sign_in.setObjectName("settings-sign-in")
        grid.addWidget(self._sign_in, 3, 1)
        self._remove_token = QPushButton("Sign out", body)
        self._remove_token.setObjectName("settings-remove-token")
        grid.addWidget(self._remove_token, 3, 2)
        self._sign_in_code = _label(body, "settings-sign-in-code")
        self._sign_in_code.setVisible(False)
        self._token_message = _label(body, "settings-token-message")
        self._token_message.setVisible(False)
        sign_in_lines = QVBoxLayout()
        sign_in_lines.addWidget(self._sign_in_code)
        sign_in_lines.addWidget(self._token_message)
        grid.addLayout(sign_in_lines, 4, 0, 1, 4)

        # Test connection and Try again, side by side, then their result. Try again runs the same test.
        self._test_button = QPushButton("Test connection", body)
        self._test_button.setObjectName("settings-test-connection")
        self._try_again_button = QPushButton("Try again", body)
        self._try_again_button.setObjectName("settings-try-again")
        test_row = QHBoxLayout()
        test_row.setContentsMargins(0, 0, 0, 0)
        test_row.setSpacing(8)
        test_row.addWidget(self._test_button, 0, Qt.AlignmentFlag.AlignLeft)
        test_row.addWidget(self._try_again_button, 0, Qt.AlignmentFlag.AlignLeft)
        test_row.addStretch(1)
        grid.addLayout(test_row, 5, 0, 1, 2)
        self._test_result = _label(body, "settings-test-result")
        self._test_result.setVisible(False)
        grid.addWidget(self._test_result, 6, 0, 1, 4)

        # Local URL.
        grid.addWidget(_caption(body, "Local URL:"), 7, 0)
        self._local_url = QLineEdit(settings.local_url, body)
        self._local_url.setObjectName("settings-local-url")
        self._local_url.setPlaceholderText(DEFAULT_LOCAL_URL)
        grid.addWidget(self._local_url, 7, 1)
        self._save_url = QPushButton("Save local URL", body)
        self._save_url.setObjectName("settings-save-local-url")
        grid.addWidget(self._save_url, 7, 2)
        self._url_message = _label(body, "settings-local-url-message")
        self._url_message.setVisible(False)
        grid.addWidget(self._url_message, 8, 0, 1, 4)

        # Database file.
        grid.addWidget(_caption(body, "Database file:"), 9, 0)
        self._db_path = _label(body, "settings-db-path")
        grid.addWidget(self._db_path, 9, 1)
        self._choose_db = QPushButton("Choose database file...", body)
        self._choose_db.setObjectName("settings-choose-db")
        grid.addWidget(self._choose_db, 9, 2)
        self._db_message = _label(body, "settings-db-message")
        self._db_message.setVisible(False)
        grid.addWidget(self._db_message, 10, 0, 1, 4)

        # Project backups: the folder the Projects tab writes its hosted-project backups to. Read-only.
        grid.addWidget(_caption(body, "Project backups:"), 11, 0)
        destination = projects_backup.destination_text(projects_backup.backup_dir())
        self._backup_destination = _label(body, "settings-backup-destination", destination)
        grid.addWidget(self._backup_destination, 11, 1, 1, 2)

        group.add_layout(grid)
        return group

    def _build_local_server(self, parent: QWidget) -> CollapsibleGroup:
        group = CollapsibleGroup("local-server", LOCAL_SERVER_TITLE, parent)
        # The runner block. Its own heading repeats the group title, so the heading is hidden here.
        self._runner = RunnerSection(self._actions, group.body())
        heading = self._runner.findChild(QLabel, "runner-heading")
        if heading is not None:
            heading.setVisible(False)
        group.add_widget(self._runner)
        # Automatic updates: one checkbox. Off stops the checks at launch and every six hours after.
        self._auto_update = QCheckBox("Check for and install Meridian Desktop updates automatically", group.body())
        self._auto_update.setObjectName("auto-update-box")
        self._auto_update.setChecked(self._store.load().settings.auto_update)
        self._auto_update.toggled.connect(self._on_auto_update_toggled)
        group.add_widget(self._auto_update)
        return group

    def _build_client_setup(self, parent: QWidget) -> CollapsibleGroup:
        group = CollapsibleGroup("client-setup", CLIENT_SETUP_TITLE, parent, expanded=False)
        self._client_setup_group = group
        # The client setup block has its own heading, which repeats the group title, so the heading is hidden here.
        self._client_setup = ClientSetupSection(self._store, group.body(), actions=self._actions)
        heading = self._client_setup.findChild(QLabel, "client-setup-heading")
        if heading is not None:
            heading.setVisible(False)
        group.add_widget(self._client_setup)
        return group

    def _build_tools(self, parent: QWidget) -> CollapsibleGroup:
        group = CollapsibleGroup("tools", TOOLS_TITLE, parent, expanded=False)
        # One switch per tool, and one per group. The block reads the saved choice on refresh and saves each change.
        self._tools = ToolsSection(self._store, group.body())
        group.add_widget(self._tools)
        return group

    def _build_connections(self, parent: QWidget) -> CollapsibleGroup:
        group = CollapsibleGroup("connections", CONNECTIONS_TITLE, parent, expanded=False)
        # The owner's own MCP connectors. The block reads the saved list on refresh and saves each change.
        self._connections = ConnectionsSection(self._store, group.body())
        group.add_widget(self._connections)
        return group

    def _build_gpu_hosts(self, parent: QWidget) -> CollapsibleGroup:
        """The SSH host names the GPUs tab reads. Only names are saved. The tab reads them once per refresh."""
        group = CollapsibleGroup("gpu-hosts", GPU_HOSTS_TITLE, parent, expanded=False)
        body = group.body()
        group.add_widget(_label(body, "settings-gpu-hosts-intro", GPU_HOSTS_INTRO))
        row = QHBoxLayout()
        self._gpu_hosts = QLineEdit(", ".join(self._store.load().settings.gpu_hosts), body)
        self._gpu_hosts.setObjectName("settings-gpu-hosts")
        self._gpu_hosts.setPlaceholderText("SSH host names, for example gpu-box, lab-a100")
        self._gpu_hosts.setAccessibleName("SSH host names for the GPUs tab")
        row.addWidget(self._gpu_hosts, 1)
        self._add_ssh_names = QPushButton(ADD_SSH_BUTTON_TEXT, body)
        self._add_ssh_names.setObjectName("settings-add-ssh-names")
        self._add_ssh_names.clicked.connect(self._on_add_ssh_names_clicked)
        row.addWidget(self._add_ssh_names)
        self._save_gpu_hosts = QPushButton("Save SSH hosts", body)
        self._save_gpu_hosts.setObjectName("settings-save-gpu-hosts")
        self._save_gpu_hosts.clicked.connect(self._on_save_gpu_hosts_clicked)
        row.addWidget(self._save_gpu_hosts)
        group.add_layout(row)
        self._gpu_hosts_message = _label(body, "settings-gpu-hosts-message")
        self._gpu_hosts_message.setVisible(False)
        group.add_widget(self._gpu_hosts_message)
        return group

    def _build_limits(self, parent: QWidget) -> CollapsibleGroup:
        """The owner limits: one slider each, saved through the store. Enforcement is not active yet."""
        group = CollapsibleGroup("limits", LIMITS_TITLE, parent, expanded=False)
        # The block reads the saved values on refresh and saves each slider the owner moves.
        self._limits = LimitsSection(self._store, group.body())
        group.add_widget(self._limits)
        return group

    def _build_diagnostics(self, parent: QWidget) -> CollapsibleGroup:
        group = CollapsibleGroup("diagnostics", DIAGNOSTICS_TITLE, parent, expanded=False)
        group.add_widget(_label(group.body(), "diagnostics-intro", DIAGNOSTICS_INTRO))
        self._settings_file = _label(group.body(), "settings-file-path")
        group.add_widget(self._settings_file)
        self._load_warning = _label(group.body(), "settings-load-warning")
        self._load_warning.setVisible(False)
        group.add_widget(self._load_warning)
        # The full tools table reads when it is first shown and when its Refresh is pressed.
        if self._tools_status is not None:
            group.add_widget(make_selectable(QLabel(TOOLS_SECTION, group.body())))
            group.add_widget(ToolsPanel(self._tools_status, parent=group.body()))
        # The LaTeX probe is a tab of its own (LaTeX), so Diagnostics holds the logs only.
        # The Logs view reads nothing until its own Refresh is pressed, so folding this group costs nothing.
        self._logs = LogsPage(parent=group.body())
        group.add_widget(self._logs)
        return group

    def _wire(self) -> None:
        self._save_hosted_url.clicked.connect(self._on_save_hosted_url_clicked)
        self._hosted_url.returnPressed.connect(self._on_save_hosted_url_clicked)
        self._sign_in.clicked.connect(self._on_sign_in_clicked)
        self._remove_token.clicked.connect(self._on_remove_token_clicked)
        self._test_button.clicked.connect(self._on_test_clicked)
        self._try_again_button.clicked.connect(self._on_test_clicked)
        self._save_url.clicked.connect(self._on_save_url_clicked)
        self._local_url.returnPressed.connect(self._on_save_url_clicked)
        self._choose_db.clicked.connect(self._on_choose_db_clicked)
        # A saved core path turns the launch switches off, so the runner block on this tab shows them as saved now.
        self._client_setup.coreChanged.connect(self._runner.refresh_flags)
        self._actions.coreChanged.connect(self._on_core_changed)

    def refresh(self) -> None:
        """Show the saved mode, the database file and whether a hosted token is set.

        The main window calls this after a mode change, so the top bar and this tab always show the same saved mode.
        """
        result = self._store.load()
        settings = result.settings
        self._mode.setText(mode_text(settings.mode))
        self._auto_update.blockSignals(True)  # showing the saved value must not save it again
        self._auto_update.setChecked(settings.auto_update)
        self._auto_update.blockSignals(False)
        self._db_path.setText(settings.db_path or NOT_CHOSEN_TEXT)
        self._refresh_token_status()
        self._runner.refresh_flags()
        self._tools.refresh()
        self._connections.refresh()
        self._limits.refresh()
        self._settings_file.setText(SETTINGS_FILE_TEXT.format(path=self._store.path))
        _set_message(self._load_warning, LOAD_WARNING_TEXT.format(warning=result.warning) if result.warning else "")

    def _refresh_token_status(self) -> None:
        """Show only whether a token is saved. The value is read here and never shown.

        A keychain read that fails reads as not set, the same as the resolver reads it.
        """
        stored = bool(token_store.get_token(store=self._secrets))
        self._token_status.setText(TOKEN_SET_TEXT if stored else TOKEN_NOT_SET_TEXT)

    # ----------------------------------------------------------- saving

    @Slot()
    def _on_core_changed(self) -> None:
        """Open Client setup when the Meridian core is not usable, so its Choose and Find buttons are in view.

        Client setup stays folded when the core is set and found. It only opens, never folds itself.
        """
        if not self._actions.core().core_ready:
            self._client_setup_group.set_expanded(True)

    @Slot(bool)
    def _on_auto_update_toggled(self, checked: bool) -> None:
        self._update(auto_update=bool(checked))

    def _update(self, **changes: object) -> None:
        """Save changed fields through the store. Every other field in the saved file is kept."""
        self._store.save(replace(self._store.load().settings, **changes))

    @Slot()
    def _on_save_hosted_url_clicked(self) -> None:
        try:
            url = hosted_client.validate_base_url(self._hosted_url.text())
        except BadBaseUrl as exc:
            # The text is fixed wording from hosted_client. The value typed is not repeated.
            _set_message(self._hosted_url_message, f"Not saved. {exc}")
            return
        try:
            self._update(hosted_url=url)
        except (OSError, ValueError):
            _set_message(self._hosted_url_message, f"{NOT_WRITTEN_TEXT}.")
            return
        self._hosted_url.setText(url)
        _set_message(self._hosted_url_message, HOSTED_URL_SAVED_TEXT)

    def start_sign_in(self) -> None:
        """Start the browser sign-in, the same as its Sign in with browser button. The top bar's button calls this."""
        if self._sign_in.isEnabled():
            self._on_sign_in_clicked()

    @Slot()
    def _on_sign_in_clicked(self) -> None:
        """Start the browser sign-in on the pool. The button stays off until the sign-in finishes."""
        try:
            base = hosted_client.validate_base_url(self._store.load().settings.hosted_url)
        except BadBaseUrl as exc:
            _set_message(self._token_message, f"Not signed in. {exc}")
            return
        self._sign_in.setEnabled(False)
        self._sign_in_generation += 1
        self._sign_in_code.setVisible(False)
        _set_message(self._token_message, SIGN_IN_STARTING_TEXT)
        # The job holds the URL, the secret store, the owner token and the shared delivery object, never the tab.
        job = _SignInJob(
            self._sign_in_generation,
            base,
            self._secrets,
            self._sign_in_transport,
            self._open_browser,
            self._sign_in_sleep,
            self._sign_in_clock,
            self._owner,
            shared_delivery(),
        )
        QThreadPool.globalInstance().start(job)

    @Slot()
    def _on_remove_token_clicked(self) -> None:
        """Sign out: remove the token, any refresh token and the saved session from the keychain."""
        try:
            removed = hosted_session.sign_out(self._secrets)
        except TokenStoreError as exc:
            _set_message(self._token_message, f"Not removed. {exc}")
            return
        self._refresh_token_status()
        _set_message(self._token_message, TOKEN_REMOVED_TEXT if removed else TOKEN_NOTHING_REMOVED_TEXT)
        self.signedInChanged.emit()

    def _on_sign_in_event(self, event: _SignInEvent) -> None:
        """Runs on the GUI thread. An event from an earlier sign-in is dropped; the code line shows until the outcome."""
        if event.generation != self._sign_in_generation:
            return
        if not event.done:
            self._sign_in_code.setText(event.text)
            self._sign_in_code.setVisible(True)
            return
        self._sign_in_code.setVisible(False)
        self._sign_in.setEnabled(True)
        self._refresh_token_status()
        _set_message(self._token_message, event.text)
        if event.ok:
            self.signedInChanged.emit()

    # ----------------------------------------------------------- connection test

    @Slot()
    def _on_test_clicked(self) -> None:
        self._test_button.setEnabled(False)
        self._try_again_button.setEnabled(False)
        _set_message(self._test_result, TEST_RUNNING_TEXT)
        self._generation += 1
        generation = self._generation
        # The job holds the check, the owner token, the generation and the shared delivery object. It never holds the tab.
        QThreadPool.globalInstance().start(_ConnectionTestJob(generation, self._check.run, self._owner, shared_delivery()))

    @Slot(object)
    def _on_delivered(self, item: object) -> None:
        # Runs on the GUI thread. A result for another tab is dropped here, and so is one from an earlier test.
        if not isinstance(item, Delivered) or item.owner is not self._owner:
            return
        done = item.value
        if isinstance(done, _SignInEvent):
            self._on_sign_in_event(done)
            return
        if not isinstance(done, _TestDone) or done.generation != self._generation:
            return
        self._test_button.setEnabled(True)
        self._try_again_button.setEnabled(True)
        _set_message(self._test_result, done.text)

    # ----------------------------------------------------------- local settings

    @Slot()
    def _on_save_url_clicked(self) -> None:
        url = self._local_url.text().strip()
        try:
            assert_loopback(url)
        except LocalClientError as exc:
            # The text is fixed wording from local_client. The value typed is not repeated.
            _set_message(self._url_message, f"Not saved. {exc}")
            return
        try:
            self._update(local_url=url)
        except (OSError, ValueError):
            _set_message(self._url_message, f"{NOT_WRITTEN_TEXT}.")
            return
        _set_message(self._url_message, LOCAL_URL_SAVED_TEXT)

    @Slot()
    def _on_choose_db_clicked(self) -> None:
        current = self._store.load().settings.db_path
        start_dir = str(Path(current).parent) if current else ""
        chosen = self._choose_database(self, start_dir)
        if not chosen:
            return  # the dialog was cancelled, so nothing changes
        try:
            self._update(db_path=chosen)
        except (OSError, ValueError):
            _set_message(self._db_message, f"{NOT_WRITTEN_TEXT}.")
            return
        self.refresh()  # show the path back from the saved file
        _set_message(self._db_message, DB_SAVED_TEXT)

    @Slot()
    def _on_add_ssh_names_clicked(self) -> None:
        """Add the Host names from the owner's ssh config to the box. Nothing is saved here: Save SSH hosts does that."""
        found = read_ssh_config_names()
        if not found:
            _set_message(self._gpu_hosts_message, SSH_NONE_FOUND_TEXT)
            return
        typed = [name.strip() for name in self._gpu_hosts.text().split(",") if name.strip()]
        self._gpu_hosts.setText(", ".join(dict.fromkeys([*typed, *found])))
        _set_message(self._gpu_hosts_message, SSH_ADDED_TEXT.format(count=len(found), names=", ".join(found)))

    @Slot()
    def _on_save_gpu_hosts_clicked(self) -> None:
        try:
            saved = self._store.set_gpu_hosts(self._gpu_hosts.text())
        except SettingsUnreadable as exc:
            # Nothing was saved. The text is the owner's message: a copy of the unreadable file is kept, never rebuilt.
            _set_message(self._gpu_hosts_message, str(exc))
            return
        except ValueError as exc:
            # The text is fixed wording from parse_host_list. A refused name is shown only as far as it was typed.
            _set_message(self._gpu_hosts_message, f"Not saved. {exc}")
            return
        except OSError:
            _set_message(self._gpu_hosts_message, f"{NOT_WRITTEN_TEXT}.")
            return
        self._gpu_hosts.setText(", ".join(saved.gpu_hosts))
        _set_message(self._gpu_hosts_message, GPU_HOSTS_SAVED_TEXT if saved.gpu_hosts else GPU_HOSTS_CLEARED_TEXT)


__all__ = [
    "DB_FILE_FILTER",
    "DB_SAVED_TEXT",
    "HOSTED_URL_SAVED_TEXT",
    "LOCAL_URL_SAVED_TEXT",
    "MODE_ITEMS",
    "NOT_CHOSEN_TEXT",
    "NOT_WRITTEN_TEXT",
    "TEST_CONNECTED_TEXT",
    "TEST_UNEXPECTED_TEXT",
    "TOKEN_NOT_SET_TEXT",
    "TOKEN_SET_TEXT",
    "SettingsTab",
    "describe_test_failure",
    "mode_text",
]
