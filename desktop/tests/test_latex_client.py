"""Tests for the LaTeX engine probe (meridian_desktop/latex_client.py) and its panel.

Most probe tests use a fake request function, so they make no request. A real loopback
http.server stub answers the connected case and the not-running case, so the loopback GET
is exercised too. Panel tests use a fake client. No test reads the engine's files, the real
settings file or the real home directory.
"""

import json
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QLabel, QPushButton, QWidget

from _fakes import MemoryKeyring, QuietServer
from meridian_desktop import latex_client
from meridian_desktop.data_source import DataSources
from meridian_desktop.latex_client import (
    ENGINE_CONNECTED,
    ENGINE_ERROR,
    ENGINE_NOT_ANSWERING,
    ENGINE_NOT_RUNNING,
    EXTENSION_VERSION_PATH,
    HEALTH_PATH,
    NOT_REPORTED,
    PROBE_TIMEOUT,
    EngineStatus,
)
from meridian_desktop.local_client import LocalClientError
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui.latex_panel import CONNECTED_TEXT, NOT_CONNECTED_TEXT, LatexPanel
from meridian_desktop.ui.main_window import LATEX_SECTION, MainWindow

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_URL = "http://127.0.0.1:8471"
HEALTH_OK = (200, b'{"ok": true}')
EXTENSION_OK = (200, b'{"hash": "0123456789abcdef0123456789abcdef01234567"}')
SHOWN_BUILD = "0123456789ab"
CONNECTED_MESSAGE = latex_client.CONNECTED_MESSAGE
NOT_RUNNING_MESSAGE = latex_client.NOT_RUNNING_MESSAGE


class _FakeGet:
    """A request function that answers from a table. An exception in the table is raised.
    A path that is not in the table answers 404. Every call is recorded."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, base_url, path, timeout):
        self.calls.append((base_url, path, timeout))
        answer = self.answers.get(path, (404, b'{"error": "not found"}'))
        if isinstance(answer, BaseException):
            raise answer
        status, body = answer
        return SimpleNamespace(status=status, body=body)

    @property
    def paths(self):
        return [path for _base, path, _timeout in self.calls]


class _Engine:
    """A loopback stub. Each path maps to (status, body). A path that is not listed answers 404."""

    def __init__(self, routes):
        self.routes = dict(routes)
        self.paths = []
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server naming
                engine.paths.append(self.path)
                status, body = engine.routes.get(self.path, (404, b'{"error": "not found"}'))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # keep test output quiet
                return

        self._server = QuietServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def make_engine():
    made = []

    def make(routes):
        engine = _Engine(routes)
        made.append(engine)
        return engine

    yield make
    for engine in made:
        engine.close()


def _closed_port_url():
    """A loopback URL on a port that nothing listens on right now."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return f"http://127.0.0.1:{port}"


# ----------------------------------------------------------- the probe


@pytest.mark.parametrize(
    "health, extension, expected_state",
    [
        pytest.param(HEALTH_OK, EXTENSION_OK, ENGINE_CONNECTED, id="both-answer"),
        pytest.param(HEALTH_OK, (404, b"{}"), ENGINE_NOT_ANSWERING, id="extension-version-missing"),
        pytest.param((404, b"{}"), EXTENSION_OK, ENGINE_NOT_ANSWERING, id="health-missing"),
        pytest.param((200, b"ok"), EXTENSION_OK, ENGINE_NOT_ANSWERING, id="health-not-json"),
        pytest.param(HEALTH_OK, (200, b"[1, 2]"), ENGINE_NOT_ANSWERING, id="extension-json-not-an-object"),
        pytest.param((302, b""), EXTENSION_OK, ENGINE_NOT_ANSWERING, id="health-redirect-not-followed"),
        pytest.param(HEALTH_OK, (500, b'{"error": "boom"}'), ENGINE_NOT_ANSWERING, id="extension-server-error"),
        pytest.param(LocalClientError("not_running", NOT_RUNNING_MESSAGE), EXTENSION_OK, ENGINE_NOT_RUNNING, id="not-running"),
        pytest.param(HEALTH_OK, LocalClientError("timeout", "slow"), ENGINE_ERROR, id="extension-version-timeout"),
    ],
)
def test_latex_client_connected_only_when_health_and_extension_version_answer(health, extension, expected_state):
    get = _FakeGet({HEALTH_PATH: health, EXTENSION_VERSION_PATH: extension})

    status = latex_client.probe_engine(LOCAL_URL, get=get)

    assert status.state == expected_state
    assert status.connected is (expected_state == ENGINE_CONNECTED)
    if expected_state == ENGINE_CONNECTED:
        assert get.paths == [HEALTH_PATH, EXTENSION_VERSION_PATH]
    else:
        # The connected state is never shown with a version that did not answer.
        assert status.engine_version is None and status.extension_version is None


def test_extension_version_is_not_asked_when_health_does_not_answer():
    get = _FakeGet({HEALTH_PATH: (404, b"{}"), EXTENSION_VERSION_PATH: EXTENSION_OK})

    latex_client.probe_engine(LOCAL_URL, get=get)

    assert get.paths == [HEALTH_PATH]


def test_probe_reads_both_versions_from_a_loopback_engine(make_engine):
    engine = make_engine({HEALTH_PATH: HEALTH_OK, EXTENSION_VERSION_PATH: EXTENSION_OK})

    status = latex_client.probe_engine(engine.url)

    assert status.connected
    assert status.message == CONNECTED_MESSAGE
    assert status.engine_version is None
    assert status.extension_version == SHOWN_BUILD
    assert engine.paths == [HEALTH_PATH, EXTENSION_VERSION_PATH]


def test_probe_not_running_when_nothing_listens():
    status = latex_client.probe_engine(_closed_port_url(), timeout=PROBE_TIMEOUT)

    assert status.state == ENGINE_NOT_RUNNING
    assert status.message == NOT_RUNNING_MESSAGE
    assert not status.connected


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:8471",  # not loopback
        "https://127.0.0.1:8471",  # not http
        "http://127.0.0.1.example.com:8471",  # a lookalike of a loopback address
        "http://someone@127.0.0.1:8471",  # a user name in the address
        "",  # empty
    ],
)
def test_probe_refuses_a_non_loopback_address_without_sending(url):
    get = _FakeGet({HEALTH_PATH: HEALTH_OK, EXTENSION_VERSION_PATH: EXTENSION_OK})

    status = latex_client.probe_engine(url, get=get)

    assert status.state == ENGINE_ERROR
    assert not status.connected
    assert get.calls == []


@pytest.mark.parametrize(
    "value, shown",
    [
        ("0123456789abcdef", "0123456789ab"),
        ("0123456789abcdef0123456789abcdef01234567", "0123456789ab"),
        ("<b>bold</b>", NOT_REPORTED),
        ("0123", NOT_REPORTED),
        ("ABCDEF0123456789", NOT_REPORTED),
        ("", NOT_REPORTED),
        (3, NOT_REPORTED),
        (None, NOT_REPORTED),
    ],
)
def test_only_a_plain_extension_hash_is_shown_shortened(value, shown):
    extension = json.dumps({"hash": value}).encode("utf-8")
    get = _FakeGet({HEALTH_PATH: (200, b'{"ok": true}'), EXTENSION_VERSION_PATH: (200, extension)})

    status = latex_client.probe_engine(LOCAL_URL, get=get)

    assert status.connected
    assert status.engine_version is None
    assert status.extension_version == shown


def test_a_version_field_is_never_read_because_the_engine_does_not_send_one():
    get = _FakeGet({HEALTH_PATH: (200, b'{"version": "9.9.9"}'), EXTENSION_VERSION_PATH: (200, b'{"version": "9.9.9"}')})

    status = latex_client.probe_engine(LOCAL_URL, get=get)

    assert status.connected
    assert status.engine_version is None
    assert status.extension_version == NOT_REPORTED


@pytest.mark.parametrize(
    "address, port",
    [
        ("http://127.0.0.1:8471", "8471"),
        ("http://localhost:9999", "9999"),
        ("http://someone@127.0.0.1:8471", "8471"),  # a user name is never part of the text
        ("http://127.0.0.1", latex_client.UNKNOWN_PORT),  # no port written in the address
        ("http://127.0.0.1:99999", latex_client.UNKNOWN_PORT),  # out of range
        ("not a url", latex_client.UNKNOWN_PORT),
        (None, latex_client.UNKNOWN_PORT),
    ],
)
def test_port_text_shows_only_the_port(address, port):
    assert latex_client.port_text(address) == port


def test_importing_the_probe_makes_no_request():
    code = (
        "import socket\n"
        "def refuse(*args, **kwargs):\n"
        "    raise SystemExit('a network call was made at import')\n"
        "socket.socket.connect = refuse\n"
        "socket.create_connection = refuse\n"
        "import meridian_desktop.latex_client\n"
        "print('import ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "import ok" in result.stdout


# ----------------------------------------------------------- the panel


class _FakeEngineClient:
    """Stands in for latex_client inside the panel. It records each call and the thread it ran on."""

    def __init__(self, status):
        self.status = status
        self.calls = []

    def probe_engine(self, base_url, timeout):
        self.calls.append((base_url, timeout, threading.current_thread() is not threading.main_thread()))
        if isinstance(self.status, BaseException):
            raise self.status
        return self.status


CONNECTED_STATUS = EngineStatus(ENGINE_CONNECTED, CONNECTED_MESSAGE, engine_version=None, extension_version="0123456789ab")
NOT_RUNNING_STATUS = EngineStatus(ENGINE_NOT_RUNNING, NOT_RUNNING_MESSAGE)


def _label(panel, name):
    return panel.findChild(QLabel, name)


def test_panel_makes_no_request_until_it_is_shown(qtbot, settings_path):
    client = _FakeEngineClient(CONNECTED_STATUS)

    panel = LatexPanel(SettingsStore(settings_path), client=client)
    qtbot.addWidget(panel)

    assert client.calls == []


def test_panel_probes_once_on_first_show_and_shows_both_versions_side_by_side(qtbot, settings_path):
    SettingsStore(settings_path).save(Settings(engine_url=LOCAL_URL))
    client = _FakeEngineClient(CONNECTED_STATUS)
    panel = LatexPanel(SettingsStore(settings_path), client=client)
    qtbot.addWidget(panel)

    panel.show()
    qtbot.waitExposed(panel)

    status = _label(panel, "latex-status")
    qtbot.waitUntil(lambda: status.text() == CONNECTED_TEXT, timeout=5000)
    assert client.calls == [(LOCAL_URL, PROBE_TIMEOUT, True)]
    engine = _label(panel, "latex-engine-version")
    extension = _label(panel, "latex-extension-version")
    assert engine.text() == "Engine version: not available"
    assert extension.text() == "Extension build: 0123456789ab"
    assert _label(panel, "latex-detail").text() == CONNECTED_MESSAGE
    # Both versions sit in one row, the engine version on the left.
    assert engine.geometry().top() == extension.geometry().top()
    assert engine.geometry().right() < extension.geometry().left()


def test_check_again_probes_once_more_and_shows_not_connected_without_versions(qtbot, settings_path):
    client = _FakeEngineClient(CONNECTED_STATUS)
    panel = LatexPanel(SettingsStore(settings_path), client=client)
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitExposed(panel)
    status = _label(panel, "latex-status")
    qtbot.waitUntil(lambda: status.text() == CONNECTED_TEXT, timeout=5000)

    client.status = NOT_RUNNING_STATUS
    panel.findChild(QPushButton, "latex-check").click()

    qtbot.waitUntil(lambda: status.text() == NOT_CONNECTED_TEXT, timeout=5000)
    assert len(client.calls) == 2
    assert _label(panel, "latex-detail").text() == NOT_RUNNING_MESSAGE
    assert _label(panel, "latex-engine-version").text() == "Engine version: not available"
    assert _label(panel, "latex-extension-version").text() == "Extension build: not available"


def test_a_failed_check_shows_the_class_name_only(qtbot, settings_path):
    client = _FakeEngineClient(ValueError("secret detail in the message"))
    panel = LatexPanel(SettingsStore(settings_path), client=client)
    qtbot.addWidget(panel)
    panel.show()
    qtbot.waitExposed(panel)

    status = _label(panel, "latex-status")
    qtbot.waitUntil(lambda: status.text() == NOT_CONNECTED_TEXT, timeout=5000)
    detail = _label(panel, "latex-detail").text()
    assert detail == "The check failed unexpectedly (ValueError)."
    assert "secret detail" not in detail


def _no_source(*args):
    raise AssertionError("no data source is read in this test")


def test_settings_diagnostics_keeps_the_latex_label_and_places_the_panel_under_it(qtbot, settings_path):
    sources = DataSources(
        probe=lambda url: "not_running",
        read_server=_no_source,
        read_file=_no_source,
        get_token=lambda: None,
        list_hosted=_no_source,
    )
    window = MainWindow(
        SettingsStore(settings_path),
        secret_store=KeyringSecretStore(backend=MemoryKeyring()),
        data_sources=sources,
    )
    qtbot.addWidget(window)
    names = [window.tabs.tabText(index) for index in range(window.tabs.count())]
    # LaTeX is a tab of its own now, so the label and its probe sit together on that tab, not under Settings.
    page = window.tabs.widget(names.index("LaTeX"))

    panel = page.findChild(LatexPanel, "latex-panel")
    labels = [label for label in page.findChildren(QLabel) if label.text() == LATEX_SECTION]
    assert panel is not None
    assert len(labels) == 1
    settings = window.tabs.widget(names.index("Settings")).findChild(QWidget, "settings-page")
    assert settings.findChild(LatexPanel) is None
    # The label and its probe sit together, one under the other, on the LaTeX tab.
    layout = panel.parentWidget().layout()
    assert layout.indexOf(panel) == layout.indexOf(labels[0]) + 1
