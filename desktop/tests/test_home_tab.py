"""The Home tab: the checks run on their own, the cards show what they found, and the actions ask first.

Offscreen Qt, pytest-qt, fakes only. No test opens a socket, starts a process or reads the real settings or keychain.
The LaTeX and slot-port probes are stand-ins from conftest.py.
"""

import threading
from datetime import datetime

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFrame, QLabel, QPushButton

from meridian_desktop import local_client, updater
from meridian_desktop.runner_client import ERR_NOT_AVAILABLE, NOT_AVAILABLE_MESSAGE, RunnerResult
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.tools_status import YES, ToolRow, ToolsStatus
from meridian_desktop.ui import home_tab
from meridian_desktop.ui.home_tab import CHECK_INTERVAL_MS, HomeDeps, HomeTab
from meridian_desktop.ui.runner_actions import RunnerActions
from meridian_desktop.ui.text import SELECTABLE_TEXT
from meridian_desktop.ui.update_notice import UpdateNoticeBar
from meridian_desktop.zotero_client import API_NOT_RUNNING, ApiCheck, ZoteroStatus


class _FrozenClock:
    """Stands in for the time module inside home_tab only. Every reading is far in the future, so a card is stale."""

    @staticmethod
    def monotonic():
        return 10**9


class FakeZotero:
    """Stands in for the Zotero client. Counts the status reads and the launches."""

    def __init__(self, exe=None, running=False):
        self.exe = exe
        self.running = running
        self.status_calls = 0
        self.started = []

    def status(self):
        self.status_calls += 1
        return ZoteroStatus(
            exe=self.exe,
            process_running=self.running,
            api=ApiCheck(API_NOT_RUNNING, "Zotero is not answering on its local API."),
        )

    def probe_api(self):
        return ApiCheck(API_NOT_RUNNING, "Zotero is not answering on its local API.")

    def start_zotero(self, exe):
        self.started.append(exe)
        return "Started Zotero."

    def search_items(self, query):
        return []


def _tunnel_not_available(scope):
    return RunnerResult(verb="status", ok=False, error_code=ERR_NOT_AVAILABLE, error_message=NOT_AVAILABLE_MESSAGE)


def _tools_ready():
    rows = (ToolRow("This computer", "Local Meridian server", YES, YES, "The local Meridian server answers."),)
    return ToolsStatus(rows=rows, message="Checked. No tool is started or stopped.", checked_at=datetime(2026, 10, 23, 14, 5))


def _deps(tmp_path, *, probe=None, tunnel=None, actions=None, zotero=None, tools=None, updates=None, confirm=None):
    store = SettingsStore(tmp_path / "settings.json")
    if actions is None:
        actions = RunnerActions(
            store,
            make_control=lambda python, cwd: None,
            confirm=confirm or (lambda parent, title, body, confirm_text: False),
            find_zotero=lambda: None,
            probe_zotero=lambda: ApiCheck(API_NOT_RUNNING, "not running"),
            start_zotero=lambda exe, log_path=None: "started",
            probe_local=lambda url: local_client.HEALTH_NOT_RUNNING,
        )
    return HomeDeps(
        store=store,
        probe=probe or (lambda url: local_client.HEALTH_NOT_RUNNING),
        tunnel=tunnel or _tunnel_not_available,
        actions=actions,
        zotero=zotero if zotero is not None else FakeZotero(),
        tools=tools or _tools_ready,
        updates=updates,
        current_version="0.2.0",
    )


def _shown(qtbot, deps):
    tab = HomeTab(deps)
    qtbot.addWidget(tab)
    tab.show()
    qtbot.waitExposed(tab)
    return tab


def _card(tab, key):
    card = tab.findChild(QFrame, f"home-card-{key}")
    assert card is not None, key
    return card


def _headline(tab, key):
    return _card(tab, key).findChild(QLabel, f"home-{key}-headline").text()


def _wait_settled(qtbot, tab, key):
    """Wait until a card has left its checking state."""
    qtbot.waitUntil(lambda: _headline(tab, key) not in ("Checking...", ""), timeout=5000)


# ----------------------------------------------------------------- boot

def test_every_card_checks_once_when_the_window_first_shows_with_no_click(qtbot, tmp_path):
    calls = {"probe": [], "tools": [], "tunnel": [], "zotero": FakeZotero()}

    def probe(url):
        calls["probe"].append(url)
        return local_client.HEALTH_NOT_RUNNING

    def tools():
        calls["tools"].append(1)
        return _tools_ready()

    def tunnel(scope):
        calls["tunnel"].append(scope)
        return _tunnel_not_available(scope)

    tab = _shown(qtbot, _deps(tmp_path, probe=probe, tools=tools, tunnel=tunnel, zotero=calls["zotero"]))
    for key in ("local", "tunnel", "services", "zotero", "tools"):
        _wait_settled(qtbot, tab, key)
    assert calls["probe"] == ["http://127.0.0.1:7878"]
    assert calls["tools"] == [1]
    assert calls["tunnel"] == ["tunnel"]
    assert calls["zotero"].status_calls == 1
    assert _headline(tab, "local") == "Not running"
    assert _headline(tab, "tools") == "All set up"
    # Nothing in this boot is a failure: the services card reads its stand-in probes and says so.
    assert _headline(tab, "services") == "LaTeX engine not running"
    assert _headline(tab, "tunnel") == "Not checked"
    assert _headline(tab, "zotero") == "Not installed"
    for key in ("local", "tunnel", "services", "zotero", "tools"):
        assert _headline(tab, key) != "Could not check", key


@pytest.mark.parametrize("mode", ["local", "hosted"])
def test_the_local_tunnel_and_services_cards_check_at_boot_in_every_saved_mode(qtbot, tmp_path, mode):
    """Home's boot checks do not depend on the saved mode. Only the one-line tunnel status in the window's mode bar does."""
    SettingsStore(tmp_path / "settings.json").save(Settings(mode=mode))
    calls = {"probe": [], "tunnel": []}

    def probe(url):
        calls["probe"].append(url)
        return local_client.HEALTH_NOT_RUNNING

    def tunnel(scope):
        calls["tunnel"].append(scope)
        return _tunnel_not_available(scope)

    tab = _shown(qtbot, _deps(tmp_path, probe=probe, tunnel=tunnel))
    for key in ("local", "tunnel", "services"):
        _wait_settled(qtbot, tab, key)
    assert calls["probe"] == ["http://127.0.0.1:7878"]
    assert calls["tunnel"] == ["tunnel"]


def test_nothing_is_checked_until_the_tab_is_shown(qtbot, tmp_path):
    zotero = FakeZotero()
    tab = HomeTab(_deps(tmp_path, zotero=zotero))
    qtbot.addWidget(tab)
    qtbot.wait(50)
    assert zotero.status_calls == 0


def test_the_card_shows_checking_until_its_read_finishes(qtbot, tmp_path):
    gate = threading.Event()

    def slow_tools():
        gate.wait(5)
        return _tools_ready()

    tab = _shown(qtbot, _deps(tmp_path, tools=slow_tools))
    assert _headline(tab, "tools") == "Checking..."
    gate.set()
    _wait_settled(qtbot, tab, "tools")
    assert _headline(tab, "tools") == "All set up"


def test_a_check_that_raises_shows_only_its_class_name(qtbot, tmp_path):
    def probe(url):
        raise RuntimeError("secret detail that must not show")

    tab = _shown(qtbot, _deps(tmp_path, probe=probe))
    _wait_settled(qtbot, tab, "local")
    card = _card(tab, "local")
    assert _headline(tab, "local") == "Could not check"
    assert "(RuntimeError)" in card.findChild(QLabel, "home-local-detail").text()
    assert "secret detail" not in card.findChild(QLabel, "home-local-detail").text()


# ----------------------------------------------------------------- periodic checks

def test_the_five_minute_timer_runs_only_while_home_is_showing(qtbot, tmp_path):
    tab = _shown(qtbot, _deps(tmp_path))
    timers = [timer for timer in tab.findChildren(QTimer) if timer.interval() == CHECK_INTERVAL_MS]
    assert CHECK_INTERVAL_MS == 5 * 60 * 1000
    assert len(timers) == 1
    assert timers[0].isActive()
    tab.hide()
    assert not timers[0].isActive()


def test_check_now_runs_every_card_again(qtbot, tmp_path):
    zotero = FakeZotero()
    tab = _shown(qtbot, _deps(tmp_path, zotero=zotero))
    _wait_settled(qtbot, tab, "zotero")
    assert zotero.status_calls == 1
    button = tab.findChild(QPushButton, "home-check-now")
    assert button is not None
    button.click()
    qtbot.waitUntil(lambda: zotero.status_calls == 2, timeout=5000)


def test_showing_home_again_reruns_the_checks_once_they_are_stale(qtbot, tmp_path, monkeypatch):
    zotero = FakeZotero()
    tab = _shown(qtbot, _deps(tmp_path, zotero=zotero))
    _wait_settled(qtbot, tab, "zotero")
    tab.hide()
    # Pretend the last run was more than five minutes ago, so showing again checks at once.
    monkeypatch.setattr(home_tab, "time", _FrozenClock)
    tab.show()
    qtbot.waitUntil(lambda: zotero.status_calls == 2, timeout=5000)


# ----------------------------------------------------------------- stall and stale answers

def test_a_check_that_never_finishes_stops_blocking_its_card_and_a_new_check_runs(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(home_tab, "STALL_SECONDS", 0.05)
    release = threading.Event()
    calls = []

    def tools():
        calls.append(1)
        if len(calls) == 1:
            release.wait(10)  # the first read hangs
            return ToolsStatus(rows=(), message="stale answer")
        return _tools_ready()

    tab = _shown(qtbot, _deps(tmp_path, tools=tools))
    qtbot.waitUntil(lambda: _headline(tab, "tools") == "Still checking", timeout=5000)
    assert "more than 0.05 seconds" in _card(tab, "tools").findChild(QLabel, "home-tools-detail").text()

    tab.check_all()
    _wait_settled(qtbot, tab, "tools")
    assert _headline(tab, "tools") == "All set up"

    release.set()
    qtbot.wait(100)
    # The hung read answers late, but a newer check replaced it, so its answer is dropped.
    assert _headline(tab, "tools") == "All set up"


def test_a_late_answer_still_shows_when_no_newer_check_replaced_it(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(home_tab, "STALL_SECONDS", 0.05)
    release = threading.Event()

    def tools():
        release.wait(10)
        return _tools_ready()

    tab = _shown(qtbot, _deps(tmp_path, tools=tools))
    qtbot.waitUntil(lambda: _headline(tab, "tools") == "Still checking", timeout=5000)
    release.set()
    _wait_settled_to(qtbot, tab, "tools", "All set up")


def _wait_settled_to(qtbot, tab, key, text):
    qtbot.waitUntil(lambda: _headline(tab, key) == text, timeout=5000)


# ----------------------------------------------------------------- tunnel

def _core_settings(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings(core_python=str(tmp_path / "python.exe"), core_checkout=str(tmp_path / "core")))
    return store


def test_the_tunnel_card_offers_start_and_opens_the_tunnel_tab_but_holds_no_stop_or_restart(qtbot, tmp_path):
    tab = _shown(qtbot, _deps(tmp_path))
    _wait_settled(qtbot, tab, "tunnel")
    for name in ("tunnel-start", "tunnel-stop", "tunnel-restart", "home-tunnel-stop", "home-tunnel-restart"):
        assert tab.findChild(QPushButton, name) is None, name
    # No core is set in this test, so the card's Start is off, and the gate says why.
    start = tab.findChild(QPushButton, "home-tunnel-start")
    assert start is not None
    assert start.text() == "Start tunnel"
    assert start.isEnabled() is False
    settings_button = tab.findChild(QPushButton, "home-tunnel-settings")
    assert settings_button is not None
    assert settings_button.isVisible()
    assert settings_button.text() == "Open the Tunnel panel"


def test_the_open_tunnel_panel_button_stays_with_the_core_set(qtbot, tmp_path):
    store = _core_settings(tmp_path)
    actions = RunnerActions(store, make_control=lambda python, cwd: None, confirm=lambda *args: False)
    tab = _shown(qtbot, _deps(tmp_path, actions=actions))
    _wait_settled(qtbot, tab, "tunnel")
    assert tab.findChild(QPushButton, "home-tunnel-settings").isVisible()


def test_a_core_found_after_the_boot_checks_is_read_again_by_the_tunnel_and_tools_cards(qtbot, tmp_path):
    """The launch-time core search can finish after the boot checks. The Tunnel and Tools cards then read the core again,
    so they stop saying it is not set."""
    store = SettingsStore(tmp_path / "settings.json")
    found = {"core": False}

    def tunnel(scope):
        if not found["core"]:
            return _tunnel_not_available(scope)
        return RunnerResult(verb="status", ok=True, payload={"scope": "tunnel", "child": {"state": "not_started"}}, exit_code=0)

    def tools():
        if not found["core"]:
            not_set = "The Meridian core checkout is not set. Choose it in Settings."
            return ToolsStatus(rows=(), message=not_set, checked_at=datetime(2026, 10, 23, 14, 5))
        return _tools_ready()

    actions = RunnerActions(store, make_control=lambda python, cwd: None, confirm=lambda *args: False)
    tab = _shown(qtbot, _deps(tmp_path, actions=actions, tunnel=tunnel, tools=tools))
    _wait_settled(qtbot, tab, "tunnel")
    qtbot.waitUntil(lambda: _headline(tab, "tools") == "Not checked yet", timeout=5000)
    assert _headline(tab, "tunnel") == "Not checked"
    assert tab.findChild(QPushButton, "home-tunnel-settings").isVisible()

    # The search finds the core, saves it, and says it has finished.
    store.save(Settings(core_python=str(tmp_path / "python.exe"), core_checkout=str(tmp_path / "core")))
    found["core"] = True
    actions.coreChanged.emit()

    qtbot.waitUntil(lambda: _headline(tab, "tunnel") == "Not running", timeout=5000)
    qtbot.waitUntil(lambda: _headline(tab, "tools") == "All set up", timeout=5000)
    assert tab.findChild(QPushButton, "home-tunnel-settings").isVisible()


def test_a_search_still_running_does_not_re_read_the_cards_yet(qtbot, tmp_path):
    """A coreChanged that comes from the start of a search is skipped, so the cards are not read while the core is unknown."""
    store = SettingsStore(tmp_path / "settings.json")
    reads = []

    def tunnel(scope):
        reads.append(scope)
        return _tunnel_not_available(scope)

    actions = RunnerActions(store, make_control=lambda python, cwd: None, confirm=lambda *args: False)
    tab = _shown(qtbot, _deps(tmp_path, actions=actions, tunnel=tunnel))
    _wait_settled(qtbot, tab, "tunnel")
    before = len(reads)
    actions._locating = True  # a search is running: the start of one emits coreChanged too
    actions.coreChanged.emit()
    qtbot.wait(50)
    assert len(reads) == before


def test_the_tunnel_card_start_asks_first_and_runs_nothing_when_the_owner_says_no(qtbot, tmp_path):
    store = _core_settings(tmp_path)
    asked = []

    def confirm(parent, title, body, confirm_text):
        asked.append(confirm_text)
        return False

    actions = RunnerActions(store, make_control=lambda python, cwd: None, confirm=confirm)
    tab = _shown(qtbot, _deps(tmp_path, actions=actions))
    _wait_settled(qtbot, tab, "tunnel")
    start = tab.findChild(QPushButton, "home-tunnel-start")
    assert start.isEnabled() is True
    start.click()
    assert asked == ["Start"]
    assert tab.findChild(QLabel, "home-tunnel-control-result").text() == "Start cancelled. Nothing was started."


def test_a_tunnel_call_on_another_control_disables_the_home_start_until_it_ends(qtbot, tmp_path):
    store = _core_settings(tmp_path)
    actions = RunnerActions(store, make_control=lambda python, cwd: None, confirm=lambda *args: False)
    tab = _shown(qtbot, _deps(tmp_path, actions=actions))
    _wait_settled(qtbot, tab, "tunnel")
    start = tab.findChild(QPushButton, "home-tunnel-start")
    assert start.isEnabled() is True
    # Another owner holds the tunnel's one call, as the Tunnel tab's Stop or Restart does while it runs.
    other = object()
    assert actions.begin_scope_call("tunnel", other) is True
    assert start.isEnabled() is False
    actions.end_scope_call("tunnel", other)
    assert start.isEnabled() is True


def test_open_tunnel_panel_asks_the_window_to_switch_to_the_tunnel_tab(qtbot, tmp_path):
    tab = _shown(qtbot, _deps(tmp_path))
    _wait_settled(qtbot, tab, "tunnel")
    with qtbot.waitSignal(tab.tunnelRequested, timeout=2000):
        tab.findChild(QPushButton, "home-tunnel-settings").click()


# ----------------------------------------------------------------- zotero

def test_zotero_card_shows_running_and_offers_no_launch_when_it_runs(qtbot, tmp_path):
    tab = _shown(qtbot, _deps(tmp_path, zotero=FakeZotero(exe="C:/Zotero/zotero.exe", running=True)))
    _wait_settled(qtbot, tab, "zotero")
    assert _headline(tab, "zotero") == "Running"
    assert not _card(tab, "zotero").findChild(QPushButton, "home-zotero-action").isVisible()


def test_launch_zotero_asks_first_and_a_no_starts_nothing(qtbot, tmp_path, monkeypatch):
    zotero = FakeZotero(exe="C:/Zotero/zotero.exe", running=False)
    asked = []

    def ask(parent, command, folder):
        asked.append((command, folder))
        return False

    monkeypatch.setattr(home_tab, "ask_start", ask)
    tab = _shown(qtbot, _deps(tmp_path, zotero=zotero))
    _wait_settled(qtbot, tab, "zotero")
    button = _card(tab, "zotero").findChild(QPushButton, "home-zotero-action")
    assert button.isVisible()
    assert button.text() == "Launch Zotero"
    button.click()
    assert len(asked) == 1
    assert zotero.started == []
    assert "Launch cancelled" in _card(tab, "zotero").findChild(QLabel, "home-zotero-note").text()


def test_launch_zotero_starts_it_only_after_a_yes_and_then_checks_again(qtbot, tmp_path, monkeypatch):
    zotero = FakeZotero(exe="C:/Zotero/zotero.exe", running=False)
    monkeypatch.setattr(home_tab, "ask_start", lambda parent, command, folder: True)
    tab = _shown(qtbot, _deps(tmp_path, zotero=zotero))
    _wait_settled(qtbot, tab, "zotero")
    _card(tab, "zotero").findChild(QPushButton, "home-zotero-action").click()
    qtbot.waitUntil(lambda: zotero.started == ["C:/Zotero/zotero.exe"], timeout=5000)
    qtbot.waitUntil(lambda: zotero.status_calls >= 2, timeout=5000)


# ----------------------------------------------------------------- updates

def test_updates_card_shows_the_one_startup_check_and_its_release_button(qtbot, tmp_path, monkeypatch):
    def fake_check(current, transport, root, **kwargs):
        return updater.UpdateResult("update_staged", version="9.9.9")

    monkeypatch.setattr(updater, "running_frozen", lambda: True)
    monkeypatch.setattr(updater, "check_and_stage", fake_check)
    bar = UpdateNoticeBar("0.2.0", transport=object(), install_root=tmp_path, enabled=lambda: True)
    qtbot.addWidget(bar)
    bar.start()
    tab = _shown(qtbot, _deps(tmp_path, updates=bar))
    qtbot.waitUntil(lambda: _headline(tab, "updates") == "Update ready: v9.9.9", timeout=5000)
    assert _card(tab, "updates").findChild(QPushButton, "home-updates-action").text() == "Open release page"


def test_a_source_run_says_updates_are_not_applied_and_makes_no_request(qtbot, tmp_path, monkeypatch):
    """A run from source cannot take an update. The Updates card gets the refused reason at once, instead of staying at
    "Not checked yet", and no request or check is made."""
    calls = []

    def transport(request, timeout):
        calls.append(request)
        raise AssertionError("a source run must not make a request")

    def must_not_stage(*args, **kwargs):
        raise AssertionError("a source run must not stage an update")

    monkeypatch.setattr(updater, "running_frozen", lambda: False)
    monkeypatch.setattr(updater, "check_and_stage", must_not_stage)
    bar = UpdateNoticeBar("0.2.0", transport=transport, install_root=tmp_path, enabled=lambda: True)
    qtbot.addWidget(bar)
    bar.start()
    tab = _shown(qtbot, _deps(tmp_path, updates=bar))
    detail = _card(tab, "updates").findChild(QLabel, "home-updates-detail")
    qtbot.waitUntil(lambda: detail.text() == updater.DEV_RUN_REASON, timeout=5000)
    assert _headline(tab, "updates") == "Could not check"
    assert calls == []


def test_updates_card_says_checks_are_off_when_check_updates_is_off(qtbot, tmp_path):
    bar = UpdateNoticeBar("0.2.0", transport=object(), enabled=lambda: False)
    qtbot.addWidget(bar)
    bar.start()
    tab = _shown(qtbot, _deps(tmp_path, updates=bar))
    assert _headline(tab, "updates") == "Version 0.2.0"
    assert "Update checks are off" in _card(tab, "updates").findChild(QLabel, "home-updates-detail").text()


# ----------------------------------------------------------------- tools rows

def test_tools_rows_each_show_one_action_and_check_again_runs_the_read(qtbot, tmp_path):
    reads = []

    def tools():
        reads.append(1)
        rows = (
            ToolRow("Codex", "Meridian entries", "no", "unchecked", "Not in Codex's settings yet."),
            ToolRow("This computer", "Local Meridian server", YES, YES, "answers"),
        )
        return ToolsStatus(rows=rows, message="Checked.", checked_at=datetime(2026, 10, 23, 14, 5))

    tab = _shown(qtbot, _deps(tmp_path, tools=tools))
    qtbot.waitUntil(lambda: _headline(tab, "tools") == "1 need attention", timeout=5000)
    card = _card(tab, "tools")
    first = card.findChild(QPushButton, "home-tools-row-0-action")
    second = card.findChild(QPushButton, "home-tools-row-1-action")
    assert first.text() == "Set up in Settings"
    assert second.text() == "Check again"
    second.click()
    qtbot.waitUntil(lambda: len(reads) == 2, timeout=5000)


# ----------------------------------------------------------------- shape

def test_every_home_label_is_selectable_and_nothing_says_not_built_yet(qtbot, tmp_path):
    tab = _shown(qtbot, _deps(tmp_path))
    for key in ("local", "tunnel", "services", "zotero", "tools", "updates"):
        _wait_settled(qtbot, tab, key)
    labels = [label for label in tab.findChildren(QLabel) if label.text()]
    assert len(labels) >= 12
    for label in labels:
        assert label.textInteractionFlags() == SELECTABLE_TEXT, label.objectName()
        assert "Not built yet" not in label.text()
        assert "Check services" not in label.text()


def test_home_has_six_cards_in_the_expected_order(qtbot, tmp_path):
    tab = _shown(qtbot, _deps(tmp_path))
    titles = []
    for key in ("local", "tunnel", "services", "zotero", "tools", "updates"):
        titles.append(_card(tab, key).findChild(QLabel, f"home-{key}-title").text())
    assert titles == [
        "Local Meridian server",
        "Tunnel",
        "Services",
        "Zotero",
        "AI clients and tools",
        "Updates",
    ]
