"""End to end: the real MainWindow, a loopback stub for the Meridian server, and an in-memory
keyring. QTest presses the Keys-tab buttons, the test waits for each worker, and the
visible results are asserted. Screenshots of the Keys tab go to DESKTOP_SCREENSHOT_DIR
(default: a pytest temp directory).

The stub answers the three local routes. Its check and pod answers depend on the keyring
entry, the way Meridian's keyring: credential reference does. Nothing leaves 127.0.0.1.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton, QTableWidget, QWidget

from _fakes import MemoryKeyring, QuietServer
from meridian_desktop.secret_store import RUNPOD_ACCOUNT, RUNPOD_SERVICE, KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui import runpod_panel as runpod_panel_module
from meridian_desktop.ui.main_window import MainWindow
from meridian_desktop.ui.runpod_panel import (
    KEY_MISSING_TEXT,
    KEY_STORED_TEXT,
    POD_COLUMNS,
    REMOVE_KEY_BODY,
    REMOVE_KEY_TITLE,
    RunPodPanel,
)
from meridian_desktop import local_client

# The conftest guard replaces the local RunPod calls for every test. This test talks to its own loopback server on purpose,
# so it takes the real functions, captured here at import, before that guard runs.
REAL_RUNPOD_CHECK = local_client.runpod_check
REAL_RUNPOD_LIST_PODS = local_client.runpod_list_pods

FAKE_KEY = "test-key-not-real"
# /projects is the Projects tab's read-only GET on the local server, made when the window first shows.
EXPECTED_PATHS = {"/health", "/projects", "/compute/runpod/check", "/compute/runpod/pods"}
NO_KEY_MESSAGE = (
    "No RunPod API key was found in the sources tried. Set RUNPOD_API_KEY, store the key "
    "in the keyring entry, or set up the runpodctl config."
)
PODS = [
    {
        "id": "pod-a1",
        "name": "trainer",
        "status": "RUNNING",
        "gpu": {"id": "NVIDIA RTX A4000", "count": 1},
        "costPerHour": 0.34,
        "uptimeSeconds": 7200,
    },
    {"id": "pod-b2", "name": "idle-box", "status": "EXITED", "gpu": None, "costPerHour": 0.0, "uptimeSeconds": None},
]


class MeridianStub:
    """Answers /health and the two RunPod routes the way the Meridian core does, for the desktop's calls."""

    def __init__(self, keyring_backend: MemoryKeyring) -> None:
        self._keys = keyring_backend
        self.requests: list[tuple[str, str, dict]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server naming
                stub.requests.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}))
                if self.path == "/health":
                    return self._json(200, {"status": "ok", "service": "meridian", "git_sha": "e2e", "version": "0.0.0"})
                if self.path == "/compute/runpod/check":
                    return self._json(200, stub._check_body())
                if self.path == "/compute/runpod/pods":
                    return self._json(200, stub._pods_body())
                return self._json(404, {"detail": "Not Found"})

            def _json(self, status, payload):
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                return

        self._server = QuietServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._stopped = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        if not self._stopped:
            self._stopped = True
            if self._thread.is_alive():
                # shutdown() waits for serve_forever to return. Bound that wait so a stuck stub
                # cannot hang the whole test run.
                closer = threading.Thread(target=self._server.shutdown, daemon=True)
                closer.start()
                closer.join(timeout=5)
            self._server.server_close()

    def _stored(self) -> str | None:
        # The same entry Meridian reads through keyring:meridian-desktop/runpod-api-key.
        return self._keys.get_password(RUNPOD_SERVICE, RUNPOD_ACCOUNT)

    def _check_body(self) -> dict:
        if self._stored():
            return {"ok": True, "credential_source": "keyring", "pod_count": 2, "billing_available": True, "error": None}
        return {
            "ok": False,
            "credential_source": None,
            "pod_count": 0,
            "billing_available": False,
            "error": {"code": "no_credential", "message": NO_KEY_MESSAGE, "source_kinds_tried": ["env", "keyring", "runpodctl"]},
        }

    def _pods_body(self) -> dict:
        if self._stored():
            return {"ok": True, "credential_source": "keyring", "pods": PODS, "pod_count": 2, "has_more_pods": False, "error": None}
        return {
            "ok": False,
            "credential_source": None,
            "pods": [],
            "pod_count": 0,
            "has_more_pods": False,
            "error": {"code": "no_credential", "message": NO_KEY_MESSAGE, "source_kinds_tried": ["env", "keyring", "runpodctl"]},
        }


def _button(page, name):
    return page.findChild(QPushButton, name)


def _press(qtbot, page, name):
    """Press a button on the Keys tab as a person does, then wait until the RunPod panel's job has finished."""
    QTest.mouseClick(_button(page, name), Qt.MouseButton.LeftButton)
    panel = page.findChild(RunPodPanel, "runpod-panel")
    qtbot.waitUntil(lambda: not panel.busy, timeout=10000)


def _wait_for_keys_card(qtbot, page):
    """Wait until the RunPod card's buttons are on screen. The card sits on the Keys tab, which is current when this runs.

    A press on a hidden button is not a press a person can make, so the test waits for the buttons before it presses one.
    """
    save = _button(page, "runpod-save-key")
    qtbot.waitUntil(lambda: save.isVisible() and save.geometry().height() > 0, timeout=5000)


def _text(page, name):
    return page.findChild(QLabel, name).text()


def _every_text(root: QWidget) -> str:
    chunks = []
    for widget in [root, *root.findChildren(QWidget)]:
        for getter in ("text", "placeholderText", "windowTitle", "toolTip"):
            method = getattr(widget, getter, None)
            if callable(method):
                try:
                    value = method()
                except TypeError:
                    continue
                if isinstance(value, str):
                    chunks.append(value)
    for table in root.findChildren(QTableWidget):
        for row in range(table.rowCount()):
            for column in range(table.columnCount()):
                item = table.item(row, column)
                if item is not None:
                    chunks.append(item.text())
    return "\n".join(chunks)


class _OwnerSaysYes:
    """Answers the Yes or No question about removing the key with Yes, the way the owner does, and keeps the question.

    The window builds its RunPod panel without a confirmation of its own, so the test swaps the dialog function the panel
    uses for this one. The question is still asked, and the test checks its title and text before the key is deleted.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, parent, title: str, text: str) -> bool:
        self.calls.append((title, text))
        return True


def _use_readable_font(qapp):
    """The offscreen platform finds no font directory here, so screenshots show boxes.

    Load one system font file, when present, for the screenshots only. Returns a function
    that restores the previous application font.
    """
    previous = QApplication.font()
    for candidate in (Path("C:/Windows/Fonts/segoeui.ttf"), Path("C:/Windows/Fonts/arial.ttf")):
        if not candidate.is_file():
            continue
        family_ids = QFontDatabase.addApplicationFont(str(candidate))
        families = QFontDatabase.applicationFontFamilies(family_ids)
        if families:
            size = previous.pointSize() if previous.pointSize() > 0 else 9
            QApplication.setFont(QFont(families[0], size))
            break
    return lambda: QApplication.setFont(previous)


def _screenshot_dir(tmp_path_factory) -> Path:
    configured = os.environ.get("DESKTOP_SCREENSHOT_DIR")
    directory = Path(configured) if configured else tmp_path_factory.mktemp("screenshots")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def test_runpod_panel_end_to_end(qtbot, tmp_path, tmp_path_factory, qapp, monkeypatch):
    backend = MemoryKeyring()
    stub = MeridianStub(backend)
    stub.start()
    shots = _screenshot_dir(tmp_path_factory)
    restore_font = _use_readable_font(qapp)
    owner = _OwnerSaysYes()
    monkeypatch.setattr(runpod_panel_module, "ask_yes_no", owner)
    monkeypatch.setattr(local_client, "runpod_check", REAL_RUNPOD_CHECK)
    monkeypatch.setattr(local_client, "runpod_list_pods", REAL_RUNPOD_LIST_PODS)
    try:
        settings_file = tmp_path / "settings" / "settings.json"
        SettingsStore(settings_file).save(Settings(local_url=stub.url))
        window = MainWindow(SettingsStore(settings_file), KeyringSecretStore(backend=backend))
        qtbot.addWidget(window)
        window.resize(960, 640)
        window.show()
        qtbot.waitExposed(window)

        names = [window.tabs.tabText(index) for index in range(window.tabs.count())]
        page = window.tabs.widget(names.index("Keys"))
        # The Keys tab holds the RunPod card. Its panel keeps the jobs and is hidden.
        panel = page.findChild(RunPodPanel, "runpod-panel")
        assert panel is not None
        window.tabs.setCurrentWidget(page)

        # Showing the tab probes the server. Wait for the probe and the start-up key read.
        qtbot.waitUntil(lambda: not panel.busy, timeout=10000)
        assert _text(page, "runpod-server-status") == f"Local Meridian server: running at {stub.url}"
        assert _text(page, "runpod-key-status") == KEY_MISSING_TEXT
        _wait_for_keys_card(qtbot, page)
        window.grab().save(str(shots / "keys-no-key.png"))

        # Save a key by typing it, the way a user does.
        field = page.findChild(QLineEdit, "runpod-key-input")
        QTest.keyClicks(field, FAKE_KEY)
        assert field.text() == FAKE_KEY
        _press(qtbot, page, "runpod-save-key")

        assert backend.get_password(RUNPOD_SERVICE, RUNPOD_ACCOUNT) == FAKE_KEY
        assert field.text() == ""
        assert _text(page, "runpod-key-status") == KEY_STORED_TEXT
        assert _text(page, "runpod-result") == "Key saved in the keychain."
        assert FAKE_KEY not in _every_text(window)
        window.grab().save(str(shots / "keys-key-saved.png"))

        # Test key: Meridian finds the keychain entry and reports the source.
        _press(qtbot, page, "runpod-test-key")
        assert _text(page, "runpod-result") == (
            "RunPod key works. Key source: keyring. Billing: available. Pods on the first page: 2."
        )

        # Refresh pods: the table shows the two pods, with the column headers in order.
        _press(qtbot, page, "runpod-refresh-pods")
        table = page.findChild(QTableWidget, "runpod-pods")
        headers = tuple(table.horizontalHeaderItem(column).text() for column in range(table.columnCount()))
        assert headers == POD_COLUMNS
        rows = [tuple(table.item(row, column).text() for column in range(table.columnCount())) for row in range(table.rowCount())]
        assert rows == [
            ("pod-a1", "trainer", "RUNNING", "NVIDIA RTX A4000 x1", "0.34", "7200"),
            ("pod-b2", "idle-box", "EXITED", "", "0.0", ""),
        ]
        assert _text(page, "runpod-result") == "Listed 2 pod(s)."
        window.grab().save(str(shots / "keys-pods-listed.png"))

        # Remove the key: the status flips, and Meridian then reports that no key is found.
        _press(qtbot, page, "runpod-remove-key")
        assert owner.calls == [(REMOVE_KEY_TITLE, REMOVE_KEY_BODY)], "the owner is asked once, before the key is deleted"
        assert backend.get_password(RUNPOD_SERVICE, RUNPOD_ACCOUNT) is None
        assert _text(page, "runpod-key-status") == KEY_MISSING_TEXT
        assert _text(page, "runpod-result") == "Key removed from the keychain."

        _press(qtbot, page, "runpod-test-key")
        assert _text(page, "runpod-result") == f"RunPod key check failed: {NO_KEY_MESSAGE}"
        _press(qtbot, page, "runpod-refresh-pods")
        assert table.rowCount() == 0
        window.grab().save(str(shots / "keys-key-removed.png"))

        # Stop the server. The panel reports that it is not running, and sends no RunPod call.
        stub.stop()
        paths_before = len(stub.requests)
        _press(qtbot, page, "runpod-test-key")
        assert _text(page, "runpod-result") == "Request not sent: the local Meridian server is not running."
        assert _text(page, "runpod-server-status") == (
            f"Local Meridian server: not running at {stub.url}. Start Meridian on this computer."
        )
        assert len(stub.requests) == paths_before
        window.grab().save(str(shots / "keys-server-stopped.png"))

        # Across every request the desktop sent: only GETs to the three routes, and no key.
        requests = stub.requests
        assert requests, "the stub received no requests"
        for method, path, headers in requests:
            assert method == "GET"
            assert path in EXPECTED_PATHS
            assert FAKE_KEY not in path
            assert all(FAKE_KEY not in value for value in headers.values())
            assert "authorization" not in headers
        assert FAKE_KEY not in _every_text(window)
    finally:
        stub.stop()
        restore_font()

    for name in ("keys-no-key.png", "keys-key-saved.png", "keys-pods-listed.png",
                 "keys-key-removed.png", "keys-server-stopped.png"):
        assert (shots / name).stat().st_size > 0, name
