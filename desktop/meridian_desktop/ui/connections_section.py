"""The Connections block on the Settings tab: the owner's own MCP connectors, one row each, and a form that adds one. Qt code.

A row shows the connector, whether it is written to Claude Desktop and Codex, and its tunnel flag. Its switches save at once,
as the MCP tools switches do. Remove asks first. The form adds a command or a URL, or a link to a command that already exists
on this computer. Every change goes through custom_connectors.py, so a refused entry is never saved. A saved entry that is not
usable shows why on its own row, and can be removed. A change is refused while the settings file has a problem, so nothing
is written over a file the app could not read.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from meridian_desktop import custom_connectors
from meridian_desktop.custom_connectors import (
    KIND_COPY,
    KIND_LINKED,
    KIND_TEXT,
    TRANSPORT_HTTP,
    TRANSPORT_STDIO,
    TRANSPORT_TEXT,
    ConnectorError,
    Row,
)
from meridian_desktop.local_cache import Resolve, find_command
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui.confirm import ask_yes_no
from meridian_desktop.ui.text import make_selectable

CONNECTIONS_TITLE = "Connections"
INTRO_TEXT = (
    "Your own MCP connectors. Add a command you run on this computer, or link to one that already exists. An enabled command "
    "is written to Claude Desktop and Codex the next time Meridian Desktop starts, and a switched-off or removed one is taken "
    "out of them then. An http connector is listed but is not written there."
)
TUNNEL_NOTE_TEXT = (
    "claude.ai reaches a connector only through the tunnel, while the tunnel is up, and only when the opt-in above is on and "
    "the connector is marked for the tunnel. The tunnel bridge does not serve custom connectors yet, so nothing is reachable "
    "from claude.ai until that step is built."
)
OPT_IN_TEXT = "Let claude.ai use my custom connectors through the tunnel, while the tunnel is up. Off by default."
OPT_IN_ON_TEXT = "The tunnel opt-in is on. It takes effect once the tunnel bridge serves custom connectors."
OPT_IN_OFF_TEXT = "The tunnel opt-in is off. No custom connector is reachable from claude.ai."
EMPTY_TEXT = "No connectors yet. Add one below."
ADD_HEADING_TEXT = "Add a connector"
NAME_PLACEHOLDER = "Name, for example notes"
COMMAND_PLACEHOLDER = "Command, for example C:/tools/notes.exe"
ARGS_PLACEHOLDER = "Arguments, separated by spaces (optional)"
URL_PLACEHOLDER = "URL, for example https://example.com/mcp"
KIND_FORM_TEXT = {KIND_COPY: "Copy: Meridian keeps its own definition", KIND_LINKED: "Linked: points at one that exists"}
TRANSPORT_FORM_TEXT = {TRANSPORT_STDIO: "Command (stdio)", TRANSPORT_HTTP: "URL (http)"}
NEW_TUNNEL_TEXT = "Mark for the tunnel"
ADD_TEXT = "Add connector"
ADDED_TEXT = "Added {name}. It is written to Claude Desktop and Codex at the next start, if it is on and its command is found."
ENABLED_TEXT = "On"
ROW_TUNNEL_TEXT = "Tunnel"
REMOVE_TEXT = "Remove"
REMOVE_TITLE = "Remove this connector?"
REMOVE_BODY = "{name} leaves this list. At the next start it is taken out of Claude Desktop and Codex. Nothing else changes."
REMOVED_TEXT = "Removed {name}."
CANCELLED_TEXT = "Cancelled. Nothing was removed."
UNNAMED_TEXT = "(no name)"
NOT_WRITTEN_TEXT = "Not saved: the settings file could not be written."
SETTINGS_PROBLEM_TEXT = "Not saved: the settings file has a problem ({warning}). Fix the file first, or see Diagnostics."

# (parent, title, text) -> True only when the owner clicks Yes.
Confirm = Callable[[QWidget, str, str], bool]


def _row_object(index: int, part: str = "") -> str:
    """The object name of one row, or of one control in it. Index-based, so an unusable entry has a name too."""
    return f"connections-row-{index}-{part}" if part else f"connections-row-{index}"


def _describe(connector: custom_connectors.Connector) -> str:
    kind = KIND_TEXT[connector.kind]
    if connector.transport == TRANSPORT_STDIO:
        target = " ".join([connector.command, *connector.args])
    else:
        target = connector.url
    return f"{kind}, {TRANSPORT_TEXT[connector.transport].lower()}: {target}"


def _plain_label(parent: QWidget, name: str, text: str = "") -> QLabel:
    """A label for text the owner typed: shown as plain text, so nothing in it is read as markup."""
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return make_selectable(label)


class ConnectionsSection(QWidget):
    """The Connections block. Build it once; call refresh() to show the saved list again."""

    def __init__(
        self,
        store: SettingsStore,
        parent: QWidget | None = None,
        *,
        resolve: Resolve = find_command,
        confirm: Confirm | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("connections-section")
        self._store = store
        self._resolve = resolve
        # None means the Yes/No dialog. It is looked up when Remove is pressed, so a test can swap it.
        self._confirm = confirm
        self._build()
        self.refresh()

    # ----------------------------------------------------------- layout

    def _build(self) -> None:
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        column.addWidget(_plain_label(self, "connections-intro", INTRO_TEXT))

        self._opt_in = QCheckBox(OPT_IN_TEXT, self)
        self._opt_in.setObjectName("connections-tunnel-opt-in")
        self._opt_in.toggled.connect(self._on_opt_in_toggled)
        column.addWidget(self._opt_in)
        column.addWidget(_plain_label(self, "connections-tunnel-note", TUNNEL_NOTE_TEXT))

        self._list_host = QWidget(self)
        self._list_host.setObjectName("connections-list")
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(6)
        column.addWidget(self._list_host)
        self._empty = _plain_label(self, "connections-empty", EMPTY_TEXT)
        column.addWidget(self._empty)

        self._message = _plain_label(self, "connections-message")
        self._message.setVisible(False)
        column.addWidget(self._message)

        heading = make_selectable(QLabel(ADD_HEADING_TEXT, self))
        heading.setObjectName("connections-add-heading")
        font = heading.font()
        font.setBold(True)
        heading.setFont(font)
        column.addWidget(heading)

        self._new_name = QLineEdit(self)
        self._new_name.setObjectName("connections-new-name")
        self._new_name.setPlaceholderText(NAME_PLACEHOLDER)
        self._new_kind = QComboBox(self)
        self._new_kind.setObjectName("connections-new-kind")
        for kind in (KIND_COPY, KIND_LINKED):
            self._new_kind.addItem(KIND_FORM_TEXT[kind], kind)
        self._new_transport = QComboBox(self)
        self._new_transport.setObjectName("connections-new-transport")
        for transport in (TRANSPORT_STDIO, TRANSPORT_HTTP):
            self._new_transport.addItem(TRANSPORT_FORM_TEXT[transport], transport)
        top = QHBoxLayout()
        top.addWidget(self._new_name, 1)
        top.addWidget(self._new_kind, 1)
        top.addWidget(self._new_transport, 1)
        column.addLayout(top)

        self._new_command = QLineEdit(self)
        self._new_command.setObjectName("connections-new-command")
        self._new_command.setPlaceholderText(COMMAND_PLACEHOLDER)
        self._new_args = QLineEdit(self)
        self._new_args.setObjectName("connections-new-args")
        self._new_args.setPlaceholderText(ARGS_PLACEHOLDER)
        self._stdio_row = QWidget(self)
        stdio = QHBoxLayout(self._stdio_row)
        stdio.setContentsMargins(0, 0, 0, 0)
        stdio.addWidget(self._new_command, 2)
        stdio.addWidget(self._new_args, 1)
        column.addWidget(self._stdio_row)

        self._new_url = QLineEdit(self)
        self._new_url.setObjectName("connections-new-url")
        self._new_url.setPlaceholderText(URL_PLACEHOLDER)
        self._http_row = QWidget(self)
        http = QHBoxLayout(self._http_row)
        http.setContentsMargins(0, 0, 0, 0)
        http.addWidget(self._new_url, 1)
        column.addWidget(self._http_row)
        self._http_row.setVisible(False)

        bottom = QHBoxLayout()
        self._new_tunnel = QCheckBox(NEW_TUNNEL_TEXT, self)
        self._new_tunnel.setObjectName("connections-new-tunnel")
        self._add_button = QPushButton(ADD_TEXT, self)
        self._add_button.setObjectName("connections-add")
        bottom.addWidget(self._new_tunnel)
        bottom.addStretch(1)
        bottom.addWidget(self._add_button)
        column.addLayout(bottom)

        self._new_transport.currentIndexChanged.connect(self._on_form_transport_changed)
        self._add_button.clicked.connect(self._on_add_clicked)
        self._new_command.returnPressed.connect(self._on_add_clicked)
        self._new_url.returnPressed.connect(self._on_add_clicked)

    def _make_row(self, row: Row, tunnel_opt_in: bool) -> QFrame:
        index = row.index
        connector = row.connector
        frame = QFrame(self._list_host)
        frame.setObjectName(_row_object(index))
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        column = QVBoxLayout(frame)

        name_text = row.name or UNNAMED_TEXT
        name = _plain_label(frame, _row_object(index, "name"), name_text)
        font = name.font()
        font.setBold(True)
        name.setFont(font)
        column.addWidget(name)
        if connector is not None:
            column.addWidget(_plain_label(frame, _row_object(index, "detail"), _describe(connector)))
        status_text = custom_connectors.status_text(row, resolve=self._resolve, tunnel_opt_in=tunnel_opt_in)
        column.addWidget(_plain_label(frame, _row_object(index, "status"), status_text))

        controls = QHBoxLayout()
        enabled = QCheckBox(ENABLED_TEXT, frame)
        enabled.setObjectName(_row_object(index, "enabled"))
        enabled.setChecked(bool(connector and connector.enabled))
        enabled.setEnabled(connector is not None)
        enabled.toggled.connect(lambda checked, i=index: self._on_enabled_toggled(i, checked))
        tunnel = QCheckBox(ROW_TUNNEL_TEXT, frame)
        tunnel.setObjectName(_row_object(index, "tunnel"))
        tunnel.setChecked(bool(connector and connector.tunnel_exposed))
        tunnel.setEnabled(connector is not None)
        tunnel.toggled.connect(lambda checked, i=index: self._on_tunnel_toggled(i, checked))
        remove = QPushButton(REMOVE_TEXT, frame)
        remove.setObjectName(_row_object(index, "remove"))
        remove.clicked.connect(lambda *_, i=index, n=name_text: self._on_remove_clicked(i, n))
        controls.addWidget(enabled)
        controls.addWidget(tunnel)
        controls.addStretch(1)
        controls.addWidget(remove)
        column.addLayout(controls)
        return frame

    # ----------------------------------------------------------- showing the saved list

    def refresh(self) -> None:
        """Show the saved list. Showing it never saves anything."""
        settings = self._store.load().settings
        self._opt_in.blockSignals(True)  # showing the saved value must not save it again
        self._opt_in.setChecked(settings.custom_tunnel_opt_in)
        self._opt_in.blockSignals(False)
        self._clear_rows()
        rows = custom_connectors.read_rows(settings.custom_connectors)
        for row in rows:
            self._list_layout.addWidget(self._make_row(row, settings.custom_tunnel_opt_in))
        self._empty.setVisible(not rows)

    def _clear_rows(self) -> None:
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)  # detached at once, so a lookup by name finds only the new rows
                widget.deleteLater()

    def _show(self, text: str) -> None:
        self._message.setText(text)
        self._message.setVisible(bool(text))

    # ----------------------------------------------------------- saving

    def _current_list(self) -> tuple[dict, ...]:
        return self._store.load().settings.custom_connectors

    def _save_changes(self, **changes: object) -> bool:
        """Save changed fields through the store. Every other field in the saved file is kept. Refused while the file has a
        problem, so nothing is written over a file the app could not read. Returns False, with the reason shown, when refused."""
        result = self._store.load()
        if result.warning is not None:
            self._show(SETTINGS_PROBLEM_TEXT.format(warning=result.warning))
            return False
        try:
            self._store.save(replace(result.settings, **changes))
        except OSError:
            self._show(NOT_WRITTEN_TEXT)
            return False
        return True

    def _apply(self, operation: Callable[[Sequence[dict]], tuple[dict, ...]], done: str = "") -> None:
        """Run one change to the saved list, save it, and show the saved list again. A refused change saves nothing."""
        try:
            raw = operation(self._current_list())
        except ConnectorError as exc:
            self._show(str(exc))
            self.refresh()
            return
        if not self._save_changes(custom_connectors=raw):
            self.refresh()
            return
        self.refresh()
        self._show(done)

    # ----------------------------------------------------------- actions

    def _clear_form(self) -> None:
        self._new_name.clear()
        self._new_command.clear()
        self._new_args.clear()
        self._new_url.clear()
        self._new_tunnel.setChecked(False)

    @Slot(int)
    def _on_form_transport_changed(self, _position: int) -> None:
        is_stdio = self._new_transport.currentData() == TRANSPORT_STDIO
        self._stdio_row.setVisible(is_stdio)
        self._http_row.setVisible(not is_stdio)

    @Slot()
    def _on_add_clicked(self) -> None:
        transport = self._new_transport.currentData()
        data: dict = {
            "name": self._new_name.text(),
            "kind": self._new_kind.currentData(),
            "transport": transport,
            "enabled": True,
            "tunnel_exposed": self._new_tunnel.isChecked(),
        }
        if transport == TRANSPORT_STDIO:
            data["command"] = self._new_command.text()
            data["args"] = self._new_args.text().split()
        else:
            data["url"] = self._new_url.text()
        name = self._new_name.text().strip()
        try:
            raw = custom_connectors.add(self._current_list(), data, resolve=self._resolve)
        except ConnectorError as exc:
            self._show(str(exc))  # fixed wording from custom_connectors; no value is repeated
            return
        if not self._save_changes(custom_connectors=raw):
            return
        self._clear_form()
        self.refresh()
        self._show(ADDED_TEXT.format(name=name))

    def _on_enabled_toggled(self, index: int, checked: bool) -> None:
        self._apply(lambda raw: custom_connectors.set_enabled(raw, index, checked))

    def _on_tunnel_toggled(self, index: int, checked: bool) -> None:
        self._apply(lambda raw: custom_connectors.set_tunnel_exposed(raw, index, checked))

    def _on_remove_clicked(self, index: int, name: str) -> None:
        ask = self._confirm if self._confirm is not None else ask_yes_no
        if not ask(self, REMOVE_TITLE, REMOVE_BODY.format(name=name)):
            self._show(CANCELLED_TEXT)
            return
        self._apply(lambda raw: custom_connectors.remove(raw, index), REMOVED_TEXT.format(name=name))

    @Slot(bool)
    def _on_opt_in_toggled(self, checked: bool) -> None:
        if not self._save_changes(custom_tunnel_opt_in=bool(checked)):
            self.refresh()
            return
        self.refresh()
        self._show(OPT_IN_ON_TEXT if checked else OPT_IN_OFF_TEXT)


__all__ = ["CONNECTIONS_TITLE", "ConnectionsSection"]
