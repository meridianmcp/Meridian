"""Tests for the LaTeX engine panel's wording (meridian_desktop/ui/latex_panel.py), with pytest-qt.

Panel tests use a fake client, or the real probe with a fake opener (local_client._opener), so no
socket opens and no request leaves this process. The settings file is a temp file. Offscreen Qt,
from conftest.
"""

import gc
import threading
import urllib.error
import weakref

import pytest
import shiboken6
from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import QLabel, QPushButton, QWidget

from meridian_desktop import latex_client, local_client
from meridian_desktop.latex_client import (
    ENGINE_CONNECTED,
    ENGINE_ERROR,
    ENGINE_NOT_ANSWERING,
    ENGINE_NOT_RUNNING,
    EngineStatus,
)
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui.latex_panel import (
    CHECKING_TEXT,
    CONNECTED_TEXT,
    NOT_CONNECTED_TEXT,
    UNCHECKED_START,
    UNCHECKED_TEXT,
    LatexPanel,
    engine_start_text,
)

from _shared_delivery import emit, recording_deliveries, visible_state

LOCAL_URL = "http://127.0.0.1:8471"
CONNECTED_STATUS = EngineStatus(
    ENGINE_CONNECTED, latex_client.CONNECTED_MESSAGE, engine_version=None, extension_version="0123456789ab"
)
NOT_RUNNING_STATUS = EngineStatus(ENGINE_NOT_RUNNING, latex_client.NOT_RUNNING_MESSAGE)
NOT_ANSWERING_STATUS = EngineStatus(ENGINE_NOT_ANSWERING, latex_client.NOT_ANSWERING_MESSAGE)


class _FakeEngineClient:
    """Stands in for latex_client inside the panel. It records each call and returns a status or raises."""

    def __init__(self, status):
        self.status = status
        self.calls = []

    def probe_engine(self, base_url, timeout):
        self.calls.append((base_url, timeout))
        if isinstance(self.status, BaseException):
            raise self.status
        return self.status


class _RefusingOpener:
    """Stands in for the loopback opener. Every request fails the way a closed loopback port does."""

    def __init__(self):
        self.urls = []

    def open(self, request, timeout=None):
        self.urls.append(request.full_url)
        raise urllib.error.URLError(ConnectionRefusedError(111, "connection refused"))


def _label(panel, name):
    return panel.findChild(QLabel, name)


def _settled(panel):
    return panel.findChild(QPushButton, "latex-check").isEnabled() and _label(panel, "latex-status").text() != CHECKING_TEXT


def _show_and_wait(qtbot, panel):
    panel.show()
    qtbot.waitExposed(panel)
    qtbot.waitUntil(lambda: _settled(panel), timeout=5000)
    return panel


def _panel(qtbot, settings_path, client):
    panel = LatexPanel(SettingsStore(settings_path), client=client)
    qtbot.addWidget(panel)
    return panel


def _all_label_text(panel):
    return "\n".join(label.text() for label in panel.findChildren(QLabel))


# ----------------------------------------------------------- the sentence on how to start, without Qt


@pytest.mark.parametrize(
    "status, port, expected",
    [
        (CONNECTED_STATUS, "8471", "it is already running, so there is nothing to start."),
        (NOT_RUNNING_STATUS, "8471",
         "start the LaTeX engine you installed on this computer so it listens on port 8471, then press Check again."),
        (NOT_ANSWERING_STATUS, "8471",
         "stop the other program on port 8471, then start the LaTeX engine you installed and press Check again."),
        (EngineStatus(ENGINE_ERROR, latex_client.NOT_LOOPBACK_MESSAGE), "",
         "change the engine address in the desktop settings to one on this computer, "
         "such as http://127.0.0.1:8471, then press Check again."),
        (EngineStatus(ENGINE_ERROR, latex_client.TIMEOUT_MESSAGE), "8471",
         "check that the LaTeX engine you installed is running on this computer, then press Check again."),
        (None, "",
         "start the LaTeX engine you installed on this computer, then press Check again."),
    ],
)
def test_engine_start_text_for_each_state(status, port, expected):
    assert engine_start_text(status, port) == expected


def test_unchecked_panel_already_says_how_to_start():
    assert UNCHECKED_START.startswith("start the LaTeX engine you installed on this computer")
    assert UNCHECKED_TEXT == "LaTeX engine: not checked yet"


# ----------------------------------------------------------- the panel, shown


def test_running_engine_shows_running_with_both_versions(qtbot, settings_path):
    panel = _panel(qtbot, settings_path, _FakeEngineClient(CONNECTED_STATUS))

    _show_and_wait(qtbot, panel)

    assert _label(panel, "latex-status").text() == CONNECTED_TEXT == "LaTeX engine: running."
    assert _label(panel, "latex-engine-version").text() == "Engine version: not available"
    assert _label(panel, "latex-extension-version").text() == "Extension build: 0123456789ab"
    assert _label(panel, "latex-start").text() == "How to start: it is already running, so there is nothing to start."


def test_engine_not_listening_shows_not_running_from_a_refused_socket(qtbot, settings_path, monkeypatch):
    """The real probe with a refused connection. The panel says not running and names the port."""
    opener = _RefusingOpener()
    monkeypatch.setattr(local_client, "_opener", lambda: opener)
    SettingsStore(settings_path).save(Settings(engine_url=LOCAL_URL))
    panel = _show_and_wait(qtbot, _panel(qtbot, settings_path, latex_client))

    assert opener.urls == [f"{LOCAL_URL}/health"]
    assert _label(panel, "latex-status").text() == NOT_CONNECTED_TEXT == "LaTeX engine: not running."
    assert _label(panel, "latex-detail").text() == latex_client.NOT_RUNNING_MESSAGE
    assert _label(panel, "latex-engine-version").text() == "Engine version: not available"
    assert _label(panel, "latex-extension-version").text() == "Extension build: not available"
    assert _label(panel, "latex-start").text() == (
        "How to start: start the LaTeX engine you installed on this computer so it listens on port 8471, "
        "then press Check again."
    )


def test_the_start_sentence_names_the_port_from_the_configured_address(qtbot, settings_path):
    SettingsStore(settings_path).save(Settings(engine_url="http://127.0.0.1:9999"))
    client = _FakeEngineClient(NOT_RUNNING_STATUS)
    panel = _show_and_wait(qtbot, _panel(qtbot, settings_path, client))

    assert client.calls == [("http://127.0.0.1:9999", latex_client.PROBE_TIMEOUT)]
    assert "listens on port 9999" in _label(panel, "latex-start").text()


def test_another_program_on_the_port_reads_not_running_and_says_to_stop_it(qtbot, settings_path):
    panel = _show_and_wait(qtbot, _panel(qtbot, settings_path, _FakeEngineClient(NOT_ANSWERING_STATUS)))

    assert _label(panel, "latex-status").text() == "LaTeX engine: not running."
    assert _label(panel, "latex-detail").text() == latex_client.NOT_ANSWERING_MESSAGE
    assert _label(panel, "latex-start").text() == (
        "How to start: stop the other program on port 8471, then start the LaTeX engine you installed "
        "and press Check again."
    )


def test_an_address_with_a_user_name_is_refused_and_the_user_name_is_not_shown(qtbot, settings_path, monkeypatch):
    """A URL with a user name is refused by the probe, so no request is sent and the name is not shown."""
    SettingsStore(settings_path).save(Settings(engine_url="http://someone@127.0.0.1:8471"))
    opener = _RefusingOpener()
    monkeypatch.setattr(local_client, "_opener", lambda: opener)

    panel = _show_and_wait(qtbot, _panel(qtbot, settings_path, latex_client))

    assert opener.urls == []
    assert _label(panel, "latex-status").text() == "LaTeX engine: not running."
    assert _label(panel, "latex-detail").text() == latex_client.NOT_LOOPBACK_MESSAGE
    assert "someone" not in _all_label_text(panel)
    assert "change the engine address" in _label(panel, "latex-start").text()


@pytest.mark.parametrize(
    "status, detail",
    [
        (EngineStatus(ENGINE_ERROR, latex_client.TIMEOUT_MESSAGE), latex_client.TIMEOUT_MESSAGE),
        (EngineStatus(ENGINE_ERROR, latex_client.UNREACHABLE_MESSAGE), latex_client.UNREACHABLE_MESSAGE),
    ],
)
def test_an_engine_that_could_not_be_checked_says_so_and_how_to_check_it(qtbot, settings_path, status, detail):
    panel = _show_and_wait(qtbot, _panel(qtbot, settings_path, _FakeEngineClient(status)))

    assert _label(panel, "latex-status").text() == "LaTeX engine: not running."
    assert _label(panel, "latex-detail").text() == detail
    assert _label(panel, "latex-start").text() == (
        "How to start: check that the LaTeX engine you installed is running on this computer, "
        "then press Check again."
    )


def test_a_failed_check_names_the_error_only_and_still_says_how_to_start(qtbot, settings_path):
    secret = "SECRET-DETAIL-IN-THE-MESSAGE"
    panel = _show_and_wait(qtbot, _panel(qtbot, settings_path, _FakeEngineClient(ValueError(secret))))

    assert _label(panel, "latex-detail").text() == "The check failed unexpectedly (ValueError)."
    assert secret not in _all_label_text(panel)
    assert _label(panel, "latex-start").text() == (
        "How to start: start the LaTeX engine you installed on this computer, then press Check again."
    )


def test_check_again_moves_from_running_to_not_running_and_updates_the_sentence(qtbot, settings_path):
    client = _FakeEngineClient(CONNECTED_STATUS)
    panel = _show_and_wait(qtbot, _panel(qtbot, settings_path, client))

    client.status = NOT_RUNNING_STATUS
    panel.findChild(QPushButton, "latex-check").click()
    qtbot.waitUntil(lambda: _settled(panel), timeout=5000)

    assert _label(panel, "latex-status").text() == "LaTeX engine: not running."
    assert "listens on port 8471" in _label(panel, "latex-start").text()
    assert len(client.calls) == 2


def test_panel_labels_are_plain_text_and_selectable(qtbot, settings_path):
    client = _FakeEngineClient(EngineStatus(ENGINE_CONNECTED, "<b>ok</b>", engine_version="<i>1</i>", extension_version="2"))
    panel = _show_and_wait(qtbot, _panel(qtbot, settings_path, client))

    assert _label(panel, "latex-engine-version").text() == "Engine version: <i>1</i>"
    for label in panel.findChildren(QLabel):
        assert label.textFormat() == Qt.TextFormat.PlainText, label.objectName()
        assert label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse, label.objectName()


def test_panel_never_shows_not_built_yet(qtbot, settings_path):
    panel = _show_and_wait(qtbot, _panel(qtbot, settings_path, _FakeEngineClient(NOT_RUNNING_STATUS)))

    assert "Not built yet" not in _all_label_text(panel)


def test_a_probe_still_running_when_its_panel_is_deleted_does_not_keep_the_panel(qtbot, settings_path, monkeypatch):
    """A panel deleted while its probe still runs is freed on the GUI thread, and no widget of it is touched.

    The job used to hold the panel: its probe was a bound method, and the result went to the panel's own signal. When
    the pool ended the probe on a worker, the worker dropped the last reference, and the panel's widgets were deleted
    off the GUI thread. The job now holds the settings store, the client and the shared delivery object. The collector
    is off, so only reference counting can free the panel here.
    """
    entered = threading.Event()
    gate = threading.Event()
    touched = []
    # The probe handler is the update step. The test only records whether it ran, and never reads the panel.
    monkeypatch.setattr(LatexPanel, "_on_probe_finished", lambda self, payload: touched.append(payload))

    class HeldAtTheGate(_FakeEngineClient):
        def probe_engine(self, base_url, timeout):
            entered.set()
            gate.wait(5)
            return super().probe_engine(base_url, timeout)

    gc.disable()
    try:
        container = QWidget()
        panel = LatexPanel(SettingsStore(settings_path), client=HeldAtTheGate(CONNECTED_STATUS), parent=container)
        panel.findChild(QPushButton, "latex-check").click()
        assert entered.wait(5)  # the probe has started and is held at the gate
        ref = weakref.ref(panel)
        shiboken6.delete(container)  # the panel's widgets are deleted on the GUI thread while the probe still runs
        del panel
        assert ref() is None
        gate.set()
        assert QThreadPool.globalInstance().waitForDone(5000)
        qtbot.wait(50)  # the late result reaches the event loop, and nothing raises
        assert touched == []  # the deleted panel's probe handler never runs, so its widgets are never touched
    finally:
        gate.set()
        gc.enable()


def test_panel_makes_no_probe_until_it_is_shown(qtbot, settings_path):
    client = _FakeEngineClient(CONNECTED_STATUS)
    panel = _panel(qtbot, settings_path, client)

    assert client.calls == []
    assert _label(panel, "latex-start").text() == f"How to start: {UNCHECKED_START}"


# ----------------------------------------------------------- a result for another owner, or a superseded probe


def test_a_result_from_another_owner_or_from_a_superseded_probe_is_dropped(qtbot, settings_path):
    """A sibling panel's answer, and an answer from a probe that this panel's newer probe superseded, show nothing.

    Two panels each probe their own engine. The panel's first probe emits its Delivered. The sibling's first probe then
    emits one at the same generation, and it reaches the panel too, so the panel drops it by owner alone. The panel probes
    again, and its first Delivered is replayed: it carries the panel's own owner with a superseded generation, so it is
    dropped by generation alone. The panel's second result is what the user sees throughout, and it is the control that
    the panel's own current Delivered is accepted.
    """
    client = _FakeEngineClient(CONNECTED_STATUS)
    panel = _panel(qtbot, settings_path, client)
    sibling = _panel(qtbot, settings_path, _FakeEngineClient(NOT_RUNNING_STATUS))
    check = panel.findChild(QPushButton, "latex-check")
    sibling_check = sibling.findChild(QPushButton, "latex-check")
    with recording_deliveries() as emitted:
        check.click()
        qtbot.waitUntil(lambda: _settled(panel), timeout=5000)
        assert len(emitted) == 1
        own_first = emitted[0]
        shown_first = visible_state(panel)
        sibling_check.click()
        qtbot.waitUntil(lambda: _settled(sibling), timeout=5000)
        assert len(emitted) == 2
        assert visible_state(panel) == shown_first
        client.status = NOT_RUNNING_STATUS
        check.click()
        qtbot.waitUntil(lambda: _settled(panel), timeout=5000)
        shown_second = visible_state(panel)
        assert shown_second != shown_first
        emit(own_first)  # this panel's answer from the first probe, which the second probe superseded
        qtbot.wait(20)
    assert visible_state(panel) == shown_second
