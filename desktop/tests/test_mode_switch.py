"""Tests for the Local and Hosted mode switch.

Covered: the two modes in settings, the old saved files (an older "tunnel" reads as Hosted), Hosted
reads through the hosted path, the one-line tunnel status on the top bar in Hosted mode, and the rule
that switching to Hosted mode starts nothing. Every runner, data source and keychain here is a fake, and the settings file is in pytest's
temp directory. Process launches are patched to record and refuse, so a test that starts one fails.
Nothing reads the real %LOCALAPPDATA%, the keychain or the network.
"""

import gc
import json
import re
import subprocess
import threading
import weakref
from datetime import datetime

import pytest
from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import QAbstractButton, QFrame, QLabel, QPushButton

from _fakes import MemoryKeyring
from _shared_delivery import emit, recording_deliveries
from meridian_desktop import local_client, settings_store, token_store
from meridian_desktop.data_source import (
    NOT_SIGNED_IN_MESSAGE,
    STATE_NOT_SIGNED_IN,
    STATE_OK,
    STATE_READ_FAILED,
    SOURCE_HOSTED,
    DataSources,
    resolve,
)
from meridian_desktop.runner_client import ERR_NOT_AVAILABLE, NOT_AVAILABLE_MESSAGE, RunnerResult
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import DEFAULT_MODE, VALID_MODES, Settings, SettingsStore
from meridian_desktop.tunnel_status import (
    BAR_CHECKING_TEXT,
    CHECK_HINT,
    START_HINT,
    DEPENDENCY_TEXT,
    NOT_RUNNING,
    RUNNING,
    UNKNOWN,
    TunnelStatus,
    bar_texts,
)
from meridian_desktop.ui import main_window as main_window_module
from meridian_desktop.ui.main_window import MODE_DESCRIPTIONS, MainWindow
from meridian_desktop.ui.settings_tab import MODE_ITEMS
from meridian_desktop.ui.text import SELECTABLE_TEXT

FAKE_TOKEN = "sk_meridian_fake-tunnel-mode"
TUNNEL_ROWS = [{"id": "p-tunnel", "name": "Reached through the tunnel"}]
CHECKED = datetime(2026, 10, 21, 10, 15, 0)
CHECKED_AT_PATTERN = re.compile(r"Checked at \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.")
START_WORDS = re.compile(r"\b(start|stop|restart)\b", re.IGNORECASE)
# A runner control is named <scope>-<verb>, for the scope it drives and the verb it runs. The runner check finds them by
# object name, never by the words on the button.
RUNNER_CONTROL_NAME = re.compile(r"^(home-local|home-tunnel|local|tunnel|zotero)-(start|stop|restart)$")
# A button whose text says stop but which runs no process: Stop using this file, on the Keys block, only stops listing a
# keys file's names. It is named by object name, so the text sweep still catches every other start-like button.
NOT_A_RUNNER_CONTROL = frozenset({"keys-file-stop"})


# ------------------------------------------------------------------ fakes


def _status_reply(child_state):
    """A runner status reply for the tunnel scope, as the core would give it."""
    payload = {"scope": "tunnel", "generated_at": 1_800_000_000.0, "child": {"state": child_state}}
    return RunnerResult(verb="status", ok=True, payload=payload, exit_code=0)


def _unavailable_reply():
    return RunnerResult(verb="status", ok=False, error_code=ERR_NOT_AVAILABLE, error_message=NOT_AVAILABLE_MESSAGE)


class RecordingRunner:
    """A fake runner. It records each call as (verb, scope). It has only a status method, so it cannot
    start, stop or restart anything. ``raises`` makes the call raise. ``gate``, when set, holds the call
    until the test sets it, so a check can be seen while it runs."""

    def __init__(self, reply=None, *, raises=None, gate=None):
        self.reply = reply
        self.raises = raises
        self.gate = gate
        self._lock = threading.Lock()
        self._calls = []

    def status(self, scope):
        with self._lock:
            self._calls.append(("status", scope))
        if self.gate is not None:
            self.gate.wait(5)
        if self.raises is not None:
            raise self.raises
        return self.reply

    def snapshot(self):
        with self._lock:
            return list(self._calls)


def _no_read(*args):
    raise AssertionError("no data source is read in this test")


def _window_sources(token=None):
    """Data sources for a window. The probe answers from memory. Nothing here touches the network or disk."""
    return DataSources(
        probe=lambda url: local_client.HEALTH_NOT_RUNNING,
        read_server=_no_read,
        read_file=_no_read,
        get_token=lambda: token,
        list_hosted=lambda url, tok: [],
    )


def _resolve_sources(token, calls):
    """Sources for resolve(). Each call is recorded, so a test can see exactly which one ran."""

    def probe(url):
        calls.append(("probe", url))
        return local_client.HEALTH_RUNNING

    def read_server(url):
        calls.append(("read_server", url))
        return []

    def read_file(path):
        calls.append(("read_file", path))
        return []

    def get_token():
        calls.append(("get_token",))
        return token

    def list_hosted(url, tok):
        calls.append(("list_hosted", url, tok))
        return list(TUNNEL_ROWS)

    return DataSources(
        probe=probe,
        read_server=read_server,
        read_file=read_file,
        get_token=get_token,
        list_hosted=list_hosted,
    )


def _window(qtbot, settings_path, *, mode=None, runner=None, token=None):
    if mode is not None:
        SettingsStore(settings_path).set_mode(mode)
    window = MainWindow(
        SettingsStore(settings_path),
        secret_store=KeyringSecretStore(backend=MemoryKeyring()),
        data_sources=_window_sources(token),
        tunnel_runner=runner,
    )
    qtbot.addWidget(window)
    return window


def _line(window):
    return window.tunnel_status_label.text()


def _detail(window):
    return window.tunnel_detail.text()


def _all_label_text(window):
    return "\n".join(label.text() for label in window.findChildren(QLabel))


def _user_buttons(widget):
    """The buttons a user can press under ``widget``. Qt's own internal corner button is left out."""
    return [b for b in widget.findChildren(QAbstractButton) if not b.objectName().startswith("qt_")]


def _tab_index(window, name):
    return [window.tabs.tabText(i) for i in range(window.tabs.count())].index(name)


@pytest.fixture
def no_process(monkeypatch):
    """Record every attempt to start a process, and refuse it. Returns the list of attempts."""
    launched = []

    def refuse(*args, **kwargs):
        launched.append(args)
        raise AssertionError("no process may be started by the mode switch")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    return launched


# ------------------------------------------------------------------ the two modes in settings


def test_the_modes_are_local_and_hosted_with_local_as_the_default(tmp_path):
    assert VALID_MODES == ("local", "hosted")
    assert DEFAULT_MODE == "local"
    assert SettingsStore(tmp_path / "absent" / "settings.json").load().settings.mode == "local"


@pytest.mark.parametrize("mode", ["local", "hosted"])
def test_each_mode_round_trips_through_settings(settings_path, mode):
    SettingsStore(settings_path).set_mode(mode)
    result = SettingsStore(settings_path).load()
    assert result.settings.mode == mode
    assert result.warning is None


def test_a_saved_tunnel_mode_from_an_older_version_reads_and_saves_as_hosted(settings_path):
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"mode": "tunnel", "local_url": "http://127.0.0.1:9999"}), encoding="utf-8")
    result = SettingsStore(settings_path).load()
    assert result.settings.mode == "hosted"
    assert result.settings.local_url == "http://127.0.0.1:9999"
    assert result.warning is None

    SettingsStore(settings_path).save(result.settings)
    assert json.loads(settings_path.read_text(encoding="utf-8"))["mode"] == "hosted"


def test_set_mode_tunnel_chooses_hosted(settings_path):
    updated = SettingsStore(settings_path).set_mode("tunnel")
    assert updated.mode == "hosted"
    assert SettingsStore(settings_path).load().settings.mode == "hosted"


@pytest.mark.parametrize("mode", ["local", "hosted"])
def test_a_saved_local_or_hosted_file_reads_and_saves_as_before(settings_path, mode):
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps({"mode": mode, "local_url": "http://127.0.0.1:7878", "hosted_url": "https://usemeridian.us"}),
        encoding="utf-8",
    )
    result = SettingsStore(settings_path).load()
    assert result.settings.mode == mode
    assert result.warning is None

    # Saving an old-mode file writes the same keys it always has. Nothing about tunnel is added.
    SettingsStore(settings_path).save(result.settings)
    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    assert set(saved) == {"mode", "hosted_url", "local_url", "engine_url", "db_path"}


def test_an_unknown_saved_mode_still_falls_back_to_local_with_a_warning(settings_path):
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"mode": "cloud"}), encoding="utf-8")
    result = SettingsStore(settings_path).load()
    assert result.settings.mode == "local"
    assert "mode must be one of" in result.warning


@pytest.mark.parametrize("bad", ["Tunnel", "tunnel ", "TUNNEL", "cloud", ""])
def test_set_mode_still_refuses_anything_but_the_three_names(settings_path, bad):
    with pytest.raises(ValueError):
        SettingsStore(settings_path).set_mode(bad)


# ------------------------------------------------------------------ data: hosted reads through the hosted client


def test_hosted_reads_through_the_hosted_client_with_the_saved_token():
    calls = []
    result = resolve(Settings(mode="hosted"), _resolve_sources(FAKE_TOKEN, calls))
    assert result.state == STATE_OK
    assert result.source == SOURCE_HOSTED
    assert result.rows == TUNNEL_ROWS
    # Only the token and the hosted client are used. The probe and the database file are not read.
    assert calls == [("get_token",), ("list_hosted", Settings().hosted_url, FAKE_TOKEN)]


def test_hosted_without_a_token_says_so_plainly_and_reads_nothing_else():
    calls = []
    result = resolve(Settings(mode="hosted"), _resolve_sources(None, calls))
    assert result.state == STATE_NOT_SIGNED_IN
    assert result.message == NOT_SIGNED_IN_MESSAGE
    assert calls == [("get_token",)]


def test_a_failed_hosted_read_names_only_the_error_class():
    def broken(url, token):
        raise RuntimeError("leaked-detail-xyz")

    sources = DataSources(
        probe=lambda url: local_client.HEALTH_NOT_RUNNING,
        read_server=_no_read,
        read_file=_no_read,
        get_token=lambda: FAKE_TOKEN,
        list_hosted=broken,
    )
    result = resolve(Settings(mode="hosted"), sources)
    assert result.state == STATE_READ_FAILED
    assert "RuntimeError" in result.message
    assert "leaked-detail-xyz" not in result.message
    assert FAKE_TOKEN not in result.message


def test_resolve_still_refuses_a_mode_it_does_not_know():
    with pytest.raises(ValueError):
        resolve(Settings(mode="cloud"), _resolve_sources(FAKE_TOKEN, []))


# ------------------------------------------------------------------ the top-bar text, as plain functions


def _status(state):
    return TunnelStatus(state=state, reason="The reason in one sentence.", checked_at=CHECKED)


def test_the_top_bar_line_names_the_state_and_the_time_checked():
    assert bar_texts(_status(RUNNING))[0] == "Tunnel running. Checked at 2026-10-21 10:15:00."
    assert bar_texts(_status(NOT_RUNNING))[0] == "Tunnel not running. Checked at 2026-10-21 10:15:00."
    assert bar_texts(_status(UNKNOWN))[0] == "Tunnel status unknown. Checked at 2026-10-21 10:15:00."


@pytest.mark.parametrize("state", [RUNNING, NOT_RUNNING, UNKNOWN])
def test_the_top_bar_detail_names_the_tools_that_need_the_tunnel(state):
    detail = bar_texts(_status(state))[1]
    assert DEPENDENCY_TEXT in detail
    for tool in ("codebase-memory", "Serena", "filesystem"):
        assert tool in detail


def test_a_tunnel_that_is_not_running_says_how_to_start_it_and_that_the_desktop_does_not():
    detail = bar_texts(_status(NOT_RUNNING))[1]
    assert START_HINT in detail
    assert "Tunnel panel" in detail
    assert "pixi run" not in detail  # no hand-typed terminal command
    # A running tunnel gets no start instructions at all.
    assert START_HINT not in bar_texts(_status(RUNNING))[1]


def test_an_unknown_tunnel_offers_the_check_command_and_not_the_start_command():
    detail = bar_texts(_status(UNKNOWN))[1]
    assert CHECK_HINT in detail
    assert "pixi run" not in detail
    assert START_HINT not in detail


def test_a_failed_check_detail_holds_no_text_from_the_error():
    line, detail = bar_texts(None, "RuntimeError")
    assert "Tunnel running" not in line + detail
    assert "Tunnel not running" not in line + detail


# ------------------------------------------------------------------ the window: the selector and the status line


def test_the_selector_offers_hosted_and_local_only(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    combo = window.mode_selector
    assert [combo.itemText(i) for i in range(combo.count())] == ["Hosted", "Local"]
    assert dict(MODE_ITEMS).keys() == set(VALID_MODES)


def test_every_mode_has_one_plain_description_and_the_selector_shows_it(qtbot, settings_path):
    assert set(MODE_DESCRIPTIONS) == set(VALID_MODES)
    window = _window(qtbot, settings_path, runner=RecordingRunner(reply=_status_reply("running")))
    combo = window.mode_selector
    for mode in VALID_MODES:
        combo.setCurrentIndex(combo.findData(mode))
        assert window.mode_description.text() == MODE_DESCRIPTIONS[mode]
    assert "not built yet" not in _all_label_text(window).lower()


@pytest.mark.parametrize(
    "reply, headline, detail_part",
    [
        pytest.param(_status_reply("running"), "Tunnel running", "The runner reports the tunnel process as running.", id="running"),
        pytest.param(_status_reply("not_started"), "Tunnel not running", START_HINT, id="not-running"),
        pytest.param(_unavailable_reply(), "Tunnel status unknown", CHECK_HINT, id="unknown"),
    ],
)
def test_hosted_mode_shows_each_status_text_with_the_time_checked(qtbot, settings_path, reply, headline, detail_part):
    runner = RecordingRunner(reply=reply)
    window = _window(qtbot, settings_path, mode="hosted", runner=runner)
    qtbot.waitUntil(lambda: _line(window) != BAR_CHECKING_TEXT, timeout=5000)

    assert _line(window).startswith(headline + ". ")
    assert CHECKED_AT_PATTERN.search(_line(window))
    assert detail_part in _detail(window)
    assert DEPENDENCY_TEXT in _detail(window)
    # One read-only status call for the tunnel scope, and nothing else.
    assert runner.snapshot() == [("status", "tunnel")]


def test_a_raised_check_in_hosted_mode_says_unknown_and_never_running(qtbot, settings_path):
    runner = RecordingRunner(raises=RuntimeError("secret-detail-abc"))
    window = _window(qtbot, settings_path, mode="hosted", runner=runner)
    qtbot.waitUntil(lambda: _line(window) != BAR_CHECKING_TEXT, timeout=5000)

    # The status call raised, and the check still gives an answer with its time: unknown, never running.
    assert _line(window).startswith("Tunnel status unknown. Checked at ")
    assert CHECKED_AT_PATTERN.search(_line(window))
    assert "RuntimeError" in _detail(window)
    assert "secret-detail-abc" not in _detail(window)
    assert "Tunnel running" not in _line(window)


def test_a_check_whose_job_fails_says_no_time_was_recorded():
    line, detail = bar_texts(None, "RuntimeError")
    assert line == "Tunnel status unknown. The check did not finish, so no time was recorded."
    assert "RuntimeError" in detail


def test_the_tunnel_line_is_hidden_and_no_check_runs_in_local_mode(qtbot, settings_path):
    runner = RecordingRunner(reply=_status_reply("running"))
    window = _window(qtbot, settings_path, mode="local", runner=runner)
    assert window.tunnel_status_label.isHidden()
    assert window.tunnel_detail.isHidden()
    assert runner.snapshot() == []


def test_switching_modes_saves_and_updates_the_tunnel_line(qtbot, settings_path):
    runner = RecordingRunner(reply=_status_reply("not_started"))
    window = _window(qtbot, settings_path, runner=runner)
    combo = window.mode_selector
    assert window.tunnel_status_label.isHidden()

    combo.setCurrentIndex(combo.findData("hosted"))
    assert SettingsStore(settings_path).load().settings.mode == "hosted"
    qtbot.waitUntil(lambda: _line(window).startswith("Tunnel not running. "), timeout=5000)
    assert not window.tunnel_status_label.isHidden()
    assert runner.snapshot() == [("status", "tunnel")]

    # Switching away hides the line and makes no check.
    combo.setCurrentIndex(combo.findData("local"))
    assert SettingsStore(settings_path).load().settings.mode == "local"
    assert window.tunnel_status_label.isHidden()
    assert window.tunnel_detail.isHidden()
    assert runner.snapshot() == [("status", "tunnel")]


def test_switching_to_hosted_when_the_tunnel_is_down_starts_nothing(qtbot, settings_path, no_process):
    runner = RecordingRunner(reply=_status_reply("not_started"))
    window = _window(qtbot, settings_path, runner=runner)
    combo = window.mode_selector

    combo.setCurrentIndex(combo.findData("hosted"))
    qtbot.waitUntil(lambda: _line(window).startswith("Tunnel not running. "), timeout=5000)

    assert no_process == []
    # The only call is the read-only status check for the tunnel scope. No start, stop or restart verb is passed.
    assert runner.snapshot() == [("status", "tunnel")]
    assert SettingsStore(settings_path).load().settings.mode == "hosted"


def test_with_no_core_python_saved_the_tunnel_check_is_unknown_and_starts_nothing(qtbot, settings_path, no_process):
    window = _window(qtbot, settings_path)  # the app's own runner, with nothing saved in Settings
    combo = window.mode_selector

    combo.setCurrentIndex(combo.findData("hosted"))
    qtbot.waitUntil(lambda: _line(window).startswith("Tunnel status unknown. "), timeout=5000)

    assert no_process == []
    assert NOT_AVAILABLE_MESSAGE in _detail(window)


def test_the_app_runner_uses_only_the_saved_core_folder_and_asks_only_for_status(
    qtbot, settings_path, monkeypatch, tmp_path
):
    seen = {"calls": []}

    class FakeClient:
        def __init__(self, *, python=None, cwd=None, **_):
            seen["python"] = python
            seen["cwd"] = cwd

        def status(self, scope):
            seen["calls"].append(("status", scope))
            return _status_reply("running")

    monkeypatch.setattr(main_window_module, "RunnerClient", FakeClient)
    python = tmp_path / "pixi" / "python.exe"
    core = tmp_path / "meridian-core"
    SettingsStore(settings_path).save(Settings(mode="hosted", core_checkout=str(core), core_python=str(python)))

    window = _window(qtbot, settings_path)
    qtbot.waitUntil(lambda: _line(window).startswith("Tunnel running. "), timeout=5000)

    assert seen["python"] == str(python)
    assert seen["cwd"] == str(core)
    assert seen["calls"] == [("status", "tunnel")]


def test_a_check_that_finishes_after_the_mode_changed_is_dropped(qtbot, settings_path):
    gate = threading.Event()
    runner = RecordingRunner(reply=_status_reply("running"), gate=gate)
    window = _window(qtbot, settings_path, runner=runner)
    combo = window.mode_selector

    combo.setCurrentIndex(combo.findData("hosted"))
    qtbot.waitUntil(lambda: len(runner.snapshot()) == 1, timeout=5000)
    combo.setCurrentIndex(combo.findData("local"))
    gate.set()
    QThreadPool.globalInstance().waitForDone(5000)
    qtbot.wait(50)

    # The check finished, but the mode is no longer Hosted, so its answer is not shown.
    assert window.tunnel_status_label.isHidden()
    assert _line(window) == BAR_CHECKING_TEXT


def _tunnel_answers(emitted):
    """The tunnel answers among what reached the shared object: each value is (generation, (status, failure)).

    Other panels, such as the RunPod page, also emit to the shared object while a window is built, so a test
    picks out only the tunnel answers.
    """
    return [
        item
        for item in emitted
        if isinstance(item.value, tuple)
        and isinstance(item.value[1], tuple)
        and item.value[1] and isinstance(item.value[1][0], TunnelStatus)
    ]


def test_a_tunnel_answer_from_another_window_or_from_a_superseded_check_is_dropped(qtbot, settings_path):
    """Another window's tunnel answer, and this window's answer from a superseded check, show nothing.

    Two windows in Hosted mode each run one check, at generation 1. The other window's answer reaches this window at
    that same generation, so this window drops it by owner alone. This window then checks again after a mode change, and
    its first answer is replayed: it carries this window's own owner with a superseded generation, so it is dropped by
    generation alone. The line of the second check is what the user sees throughout.
    """
    own_runner = RecordingRunner(reply=_status_reply("running"))
    other_runner = RecordingRunner(reply=_status_reply("not_started"))
    with recording_deliveries() as emitted:
        window = _window(qtbot, settings_path, mode="hosted", runner=own_runner)
        qtbot.waitUntil(lambda: _line(window).startswith("Tunnel running. "), timeout=5000)
        own_first = _tunnel_answers(emitted)[0]
        shown_first = (_line(window), _detail(window))
        other = _window(qtbot, settings_path, runner=other_runner)  # the saved mode is Hosted, so it checks at once
        qtbot.waitUntil(lambda: _line(other).startswith("Tunnel not running. "), timeout=5000)
        assert len(_tunnel_answers(emitted)) == 2
        qtbot.wait(20)  # the other window's answer has reached every window that listens on the shared object
        assert (_line(window), _detail(window)) == shown_first
        own_runner.reply = _status_reply("not_started")
        combo = window.mode_selector
        combo.setCurrentIndex(combo.findData("local"))
        combo.setCurrentIndex(combo.findData("hosted"))
        qtbot.waitUntil(lambda: _line(window).startswith("Tunnel not running. "), timeout=5000)
        shown_second = (_line(window), _detail(window))
        assert shown_second != shown_first
        emit(own_first)  # this window's answer from the first check, which the later checks superseded
        qtbot.wait(20)
    assert (_line(window), _detail(window)) == shown_second


def test_a_tunnel_check_still_running_when_its_window_closes_does_not_keep_the_window(qtbot, settings_path):
    """A closed window is freed on the GUI thread even while its tunnel check is still running.

    The check job used to hold the window through its deliver callback. When the pool ended the job on a worker,
    the worker dropped the last reference, and the window's widgets were deleted off the GUI thread. The job now
    holds only the runner, an owner token and the shared delivery object. The collector is off, so only reference
    counting can free the window here.
    """
    gc.disable()
    gate = threading.Event()
    try:
        runner = RecordingRunner(reply=_status_reply("running"), gate=gate)
        window = _window(qtbot, settings_path, mode="hosted", runner=runner)
        # The check has started and is held at the gate, so it is still running when the window closes.
        qtbot.waitUntil(lambda: runner.snapshot() == [("status", "tunnel")], timeout=5000)
        ref = weakref.ref(window)
        window.close()
        del window
        assert ref() is None
        gate.set()
        assert QThreadPool.globalInstance().waitForDone(5000)
        qtbot.wait(50)  # the late result reaches the event loop, and nothing raises
    finally:
        gate.set()
        gc.enable()


def test_a_failed_save_on_the_switch_to_hosted_runs_no_check(qtbot, settings_path, monkeypatch):
    runner = RecordingRunner(reply=_status_reply("running"))
    window = _window(qtbot, settings_path, runner=runner)
    combo = window.mode_selector

    def failing_replace(src, dst):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(settings_store.os, "replace", failing_replace)
    combo.setCurrentIndex(combo.findData("hosted"))

    assert combo.currentData() == "local"
    assert window.tunnel_status_label.isHidden()
    assert runner.snapshot() == []


# ------------------------------------------------------------------ no start control, and plain labels


def test_tunnel_controls_sit_on_the_tunnel_tab_and_home_offers_only_start_all_disabled_by_default(qtbot, settings_path):
    runner = RecordingRunner(reply=_status_reply("not_started"))
    window = _window(qtbot, settings_path, mode="hosted", runner=runner)
    qtbot.waitUntil(lambda: _line(window).startswith("Tunnel not running. "), timeout=5000)

    buttons = _user_buttons(window)
    assert buttons  # the window has buttons, so the checks below are not empty
    # The two launch-time switches are check boxes, not push buttons, so they are left out of the start-like list.
    push_buttons = [b for b in buttons if isinstance(b, QPushButton)]
    # The update bar's Restart button restarts the app into a staged update. It is not a runner control, so it is left out.
    push_buttons = [b for b in push_buttons if b.objectName() != "update-notice-restart"]
    # Only the owner-confirmed controls are runner controls: the Zotero start, the local scope's three buttons in
    # Settings, and the tunnel scope's three buttons in the Tunnel panel in Settings (docs/local-trust-contract.md).
    # They are found by object name, never by the words on the button.
    runner_controls = sorted(b.objectName() for b in push_buttons if RUNNER_CONTROL_NAME.match(b.objectName()))
    assert runner_controls == [
        "home-tunnel-start",
        "local-restart", "local-start", "local-stop",
        "tunnel-restart", "tunnel-start", "tunnel-stop",
        "zotero-start",
    ]
    # The text sweep still reads every button, so a start-like control of any other name is caught. Only the named
    # non-runner button is left out of it.
    start_by_text = sorted(b.objectName() for b in push_buttons if START_WORDS.search(b.text()))
    assert [name for name in start_by_text if name not in NOT_A_RUNNER_CONTROL] == runner_controls
    # The tunnel controls are off until the core is found. The one fix button that turns them on is not a control, so it is
    # left out of the check that the three controls are off.
    tunnel_buttons = [b for b in buttons if b.objectName().startswith("tunnel-") and b.objectName() != "tunnel-fix"]
    assert tunnel_buttons
    assert all(not b.isEnabled() for b in tunnel_buttons)
    # The Home card's Start is the same control, so it is off with the same gate.
    home_start = window.findChild(QPushButton, "home-tunnel-start")
    assert home_start is not None and home_start.isEnabled() is False

    # The tunnel controls are on the Tunnel tab. Home's tunnel card holds only its Start and the button that opens that tab
    # (and the fix button while the gate is off). The top bar holds no button. No other tab or card holds a tunnel control.
    from meridian_desktop.ui.tunnel_panel import TunnelPanel

    tunnel_panel = window.findChild(TunnelPanel, "tunnel-panel")
    assert tunnel_panel is not None
    assert all(b in _user_buttons(tunnel_panel) for b in tunnel_buttons)
    tunnel_card = window.findChild(QFrame, "home-card-tunnel")
    assert tunnel_card is not None
    card_names = {b.objectName() for b in _user_buttons(tunnel_card)}
    # home-tunnel-action is the card's generic action button. tunnel_view sets no action, so it stays hidden.
    assert card_names <= {"home-tunnel-start", "home-tunnel-fix", "home-tunnel-settings", "home-tunnel-action"}, card_names
    assert window.findChild(QPushButton, "home-tunnel-action").isHidden()
    assert not any(b.objectName().startswith("tunnel-") for b in _user_buttons(tunnel_card))
    for name in ("Projects", "Sessions", "Search"):
        other = window.tabs.widget(_tab_index(window, name))
        assert not any(b.objectName().startswith("tunnel-") for b in _user_buttons(other)), name
    for card in ("local", "services", "zotero", "tools", "updates"):
        other_card = window.findChild(QFrame, f"home-card-{card}")
        assert other_card is not None, card
        assert not any(b.objectName().startswith("tunnel-") for b in _user_buttons(other_card)), card
    for widget in (window.mode_selector, window.tunnel_status_label, window.tunnel_detail, window.mode_description):
        assert _user_buttons(widget) == []


def test_home_open_tunnel_panel_button_switches_to_the_tunnel_tab(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    window.findChild(QPushButton, "home-tunnel-settings").click()
    assert window.tabs.tabText(window.tabs.currentIndex()) == "Tunnel"


def test_the_tunnel_text_never_says_not_built_yet(qtbot, settings_path):
    runner = RecordingRunner(reply=_unavailable_reply())
    window = _window(qtbot, settings_path, mode="hosted", runner=runner)
    qtbot.waitUntil(lambda: _line(window).startswith("Tunnel status unknown. "), timeout=5000)
    text = (_line(window) + "\n" + _detail(window) + "\n" + window.mode_description.text()).lower()
    assert "not built yet" not in text
    assert "not built yet" not in _all_label_text(window).lower()


def test_the_new_labels_are_plain_selectable_text(qtbot, settings_path):
    window = _window(qtbot, settings_path)
    for label in (window.mode_description, window.tunnel_status_label, window.tunnel_detail):
        assert label.textInteractionFlags() == SELECTABLE_TEXT
        assert label.textFormat() == Qt.TextFormat.PlainText


# ----------------------------------------------------------- the launch mode in the mode bar (owner decision, 2026-10-23)

LAUNCH_TOKEN = "sk_meridian_FAKE-mode-bar-0009"


def _launch_window(qtbot, settings_path, *, with_token):
    """A window started the way app.main starts it: the launch mode is resolved first, from a keychain with one token or none."""
    secrets = KeyringSecretStore(backend=MemoryKeyring())
    if with_token:
        token_store.set_token(LAUNCH_TOKEN, store=secrets)
    store = SettingsStore(settings_path)
    store.resolve_launch_mode(lambda: token_store.has_token(store=secrets))
    window = MainWindow(
        store,
        secret_store=secrets,
        data_sources=_window_sources(None),
        tunnel_runner=None,
    )
    qtbot.addWidget(window)
    return window


def test_a_file_with_no_mode_and_a_saved_token_starts_and_shows_hosted_in_the_mode_bar(qtbot, settings_path):
    _write_text(settings_path, json.dumps({"hosted_url": "https://usemeridian.us"}))

    window = _launch_window(qtbot, settings_path, with_token=True)

    assert window.mode_selector.currentData() == "hosted"
    assert window.mode_description.text() == MODE_DESCRIPTIONS["hosted"]


def test_a_file_with_no_mode_and_no_saved_token_starts_and_shows_local(qtbot, settings_path):
    _write_text(settings_path, json.dumps({"hosted_url": "https://usemeridian.us"}))

    window = _launch_window(qtbot, settings_path, with_token=False)

    assert window.mode_selector.currentData() == "local"
    assert window.mode_description.text() == MODE_DESCRIPTIONS["local"]


def test_a_missing_file_with_a_saved_token_starts_and_shows_hosted(qtbot, settings_path):
    window = _launch_window(qtbot, settings_path, with_token=True)

    assert window.mode_selector.currentData() == "hosted"


@pytest.mark.parametrize(
    "saved, shown",
    [("local", "local"), ("hosted", "hosted"), ("tunnel", "hosted")],
    ids=["local", "hosted", "older-tunnel-reads-as-hosted"],
)
def test_an_explicit_mode_in_the_file_is_shown_even_with_a_saved_token(qtbot, settings_path, saved, shown):
    _write_text(settings_path, json.dumps({"mode": saved}))

    window = _launch_window(qtbot, settings_path, with_token=True)

    assert window.mode_selector.currentData() == shown


def test_an_older_tunnel_file_shows_hosted_even_with_no_saved_token(qtbot, settings_path):
    # Without the alias this file would be ignored, and with no token the launch mode would be Local.
    _write_text(settings_path, json.dumps({"mode": "tunnel"}))

    window = _launch_window(qtbot, settings_path, with_token=False)

    assert window.mode_selector.currentData() == "hosted"


def _write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
