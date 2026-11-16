"""The Zotero tab's bridge install section: the install or update button, the licence notice, the confirmation dialog,
the force-close question, the bridge update check and its switch, and the result text. Offscreen Qt, no network, and no
process started.

The panel gets a fake installer (or the real installer pointed at temp folders), a fake client and a fake save bridge.
The dialogs are fakes that answer at once, so no modal dialog opens during a test.
"""

import hashlib
import json
import threading
from types import SimpleNamespace

from PySide6.QtWidgets import QCheckBox, QLabel, QMessageBox, QPushButton

from meridian_desktop import zotero_bridge, zotero_bridge_install as bi
from meridian_desktop.settings_store import SettingsStore
from meridian_desktop.ui import zotero_panel as zp
from meridian_desktop.ui.delivery import Delivered
from meridian_desktop.zotero_client import API_NOT_RUNNING, NOT_RUNNING_MESSAGE, ApiCheck, ZoteroStatus

PAYLOAD = b"signed bridge bytes for the panel test, not a real .xpi"
ZOTERO_RUNNING = "zotero.exe                     4242 Console                    1    210,000 K\n"


class FakeClient:
    """The Zotero client the panel reads. Nothing here reaches Zotero."""

    def status(self):
        return ZoteroStatus(exe=None, process_running=False, api=ApiCheck(API_NOT_RUNNING, NOT_RUNNING_MESSAGE))

    def probe_api(self):
        return ApiCheck(API_NOT_RUNNING, NOT_RUNNING_MESSAGE)

    def list_libraries(self):
        return ([], "")

    def list_collections(self, library):
        return []

    def search_items(self, query):
        return []


class FakeBridge:
    """The save bridge's status check. It answers that the bridge is off, and nothing is written."""

    def bridge_status(self):
        return zotero_bridge.BridgeStatus(zotero_bridge.BRIDGE_OFF, "The bridge is off in this test.")


class FakeInstaller:
    """An installer whose check, update check and install results are given. Each install is recorded with its arguments.

    ``install_queue`` gives the results of successive installs, in order; after it is used up, ``install_value`` is returned.
    ``settings_path``, when given, records the force-close opt-in as each install starts, so an ordering can be checked.
    """

    def __init__(self, check_value, install_value=None, *, install_queue=(), update_value=None, settings_path=None):
        self.check_value = check_value
        self.update_value = update_value
        self.install_value = install_value or bi.InstallResult(
            bi.ST_INSTALLED, "Installed the Zotero bridge 0.1.8. The bridge is turned on in Zotero."
        )
        self.install_queue = list(install_queue)
        self.installs: list[dict] = []
        self.update_calls = 0
        self.settings_path = settings_path
        self.opt_in_seen: list[bool] = []
        self._lock = threading.Lock()

    def check(self):
        return self.check_value

    def check_for_update(self):
        with self._lock:
            self.update_calls += 1
        return self.update_value if self.update_value is not None else self.check_value

    def install(self, *, allow_close=True, allow_force_close=False, skip_graceful=False):
        if self.settings_path is not None:
            self.opt_in_seen.append(SettingsStore(self.settings_path).load().settings.zotero_force_close_allowed)
        self.installs.append(
            {"allow_close": allow_close, "allow_force_close": allow_force_close, "skip_graceful": skip_graceful}
        )
        if self.install_queue:
            return self.install_queue.pop(0)
        return self.install_value


def check(state, *, bundled="0.1.8", installed=None, registration=bi.REG_ENABLED, profile_found=True):
    return bi.BridgeCheck(state, bundled, installed, registration, profile_found)


def _panel(
    qtbot,
    tmp_path,
    *,
    installer,
    with_store=True,
    licence_answer=True,
    confirm_answer=True,
    settings=None,
    force_answer=(False, False),
):
    store = settings if settings is not None else (SettingsStore(tmp_path / "settings.json") if with_store else None)
    asked = {"licence": [], "confirm": [], "force": []}

    def licence_dialog(_parent, text):
        asked["licence"].append(text)
        return licence_answer

    def confirm_dialog(_parent, text):
        asked["confirm"].append(text)
        return confirm_answer

    def force_dialog(_parent):
        asked["force"].append(True)
        return force_answer

    panel = zp.ZoteroPanel(
        client=FakeClient(),
        bridge=FakeBridge(),
        journal_path=tmp_path / "journal.json",
        scopes_path=tmp_path / "scopes.json",
        bridge_installer=installer,
        settings=store,
        licence_dialog=licence_dialog,
        confirm_install=confirm_dialog,
        confirm_force_close=force_dialog,
    )
    qtbot.addWidget(panel)
    return panel, store, asked


def _named(panel, cls, name):
    widget = panel.findChild(cls, name)
    assert widget is not None, name
    return widget


def _shown(qtbot, panel):
    panel.show()
    qtbot.waitExposed(panel)
    qtbot.waitUntil(lambda: _named(panel, QPushButton, "zotero-refresh").isEnabled())
    qtbot.waitUntil(lambda: _named(panel, QLabel, "zotero-bridge-install-state").text() != zp.INSTALL_CHECKING_TEXT)


def _state_text(panel):
    return _named(panel, QLabel, "zotero-bridge-install-state").text()


def _result_text(panel):
    return _named(panel, QLabel, "zotero-bridge-install-result").text()


def _update_line(panel):
    return _named(panel, QLabel, "zotero-bridge-update-line")


def _install_button(panel):
    return _named(panel, QPushButton, "zotero-bridge-install")


def _licence_button(panel):
    return _named(panel, QPushButton, "zotero-bridge-licence")


def _licence_note(panel):
    return _named(panel, QLabel, "zotero-bridge-licence-note")


def _accepted_store(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.set_licence_accepted(True, notice_sha256=bi.LICENCE_NOTICE_SHA256)
    return store


def _stale_store(tmp_path):
    """An acceptance that was saved for an older notice text: the flag is on, but the digest is not the current one."""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"licence_accepted": True, "licence_notice_sha256": "0" * 64}), encoding="utf-8")
    return SettingsStore(path)


class _OptInWriteFails(SettingsStore):
    """A settings store whose force-close opt-in cannot be written. Its error text is never shown in full."""

    def set_zotero_force_close_allowed(self, value):
        raise OSError(f"cannot write {self.path}")


class _UpdateSwitchWriteFails(SettingsStore):
    """A settings store whose update-check switch cannot be written. Its error text is never shown in full."""

    def set_zotero_update_checks(self, value):
        raise OSError(f"cannot write {self.path}")


class _Recorder:
    """Records every command the installer would run, and every close poll. Nothing is started."""

    def __init__(self):
        self.commands = []
        self.sleeps = 0

    def runner(self, argv):
        self.commands.append(list(argv))
        return 1

    def sleep(self, _seconds):
        self.sleeps += 1


def _real_installer(tmp_path, host, *, settings):
    """The real installer with a bundle and a process listing that says Zotero is running. Only a refusal can be expected."""
    folder = tmp_path / "bundle" / bi.BUNDLE_SUBDIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / bi.XPI_FILENAME).write_bytes(PAYLOAD)
    (folder / bi.BUNDLE_RECORD).write_text(
        json.dumps(
            {"addon_id": bi.ADDON_ID, "file": bi.XPI_FILENAME, "sha256": hashlib.sha256(PAYLOAD).hexdigest(), "version": "0.1.8"}
        ),
        encoding="utf-8",
    )
    return bi.ZoteroBridgeInstaller(
        bi.load_bundled_bridge(folder),
        data_dir=tmp_path / "no-zotero-here",
        token_path=tmp_path / "state" / "zotero_bridge" / "api_token",
        process_list=lambda: ZOTERO_RUNNING,
        runner=host.runner,
        sleep=host.sleep,
        platform="linux",
        settings=settings,
    )


CAN_FORCE = bi.InstallResult(bi.ST_CLOSE_FAILED, bi.CLOSE_FAILED_MESSAGE, can_force_close=True)
INSTALLED_NOW = bi.InstallResult(bi.ST_INSTALLED, "Installed the Zotero bridge 0.1.8. The bridge is turned on in Zotero.")


# ----------------------------------------------------------------- what the section shows


def test_no_bundle_says_so_and_offers_no_install(qtbot, tmp_path):
    panel, _store, _asked = _panel(qtbot, tmp_path, installer=FakeInstaller(check(bi.STATE_NO_BUNDLE, bundled=None)))
    _shown(qtbot, panel)
    assert _state_text(panel) == "No bundled bridge in this build."
    assert _install_button(panel).isHidden()
    assert _licence_button(panel).isHidden()


def test_not_installed_disables_install_until_the_licence_is_accepted(qtbot, tmp_path):
    panel, _store, _asked = _panel(qtbot, tmp_path, installer=FakeInstaller(check(bi.STATE_NOT_INSTALLED)))
    _shown(qtbot, panel)
    assert not _install_button(panel).isHidden()
    assert _install_button(panel).text() == zp.INSTALL_BUTTON_TEXT
    assert not _install_button(panel).isEnabled()
    assert _licence_button(panel).isEnabled()
    assert _licence_note(panel).text() == zp.LICENCE_NEEDED_TEXT


def test_an_older_bridge_offers_update_once_the_licence_is_accepted(qtbot, tmp_path):
    installer = FakeInstaller(check(bi.STATE_OLDER, installed="0.1.7"))
    panel, _store, _asked = _panel(qtbot, tmp_path, installer=installer, settings=_accepted_store(tmp_path))
    _shown(qtbot, panel)
    assert _install_button(panel).text() == zp.UPDATE_BUTTON_TEXT
    assert _install_button(panel).isEnabled()
    assert _licence_button(panel).isHidden()
    assert _state_text(panel) == "Zotero bridge: installed 0.1.7, older than the bundled 0.1.8."


def test_a_current_bridge_offers_no_install(qtbot, tmp_path):
    panel, _store, _asked = _panel(
        qtbot, tmp_path, installer=FakeInstaller(check(bi.STATE_CURRENT, installed="0.1.8")), settings=_accepted_store(tmp_path)
    )
    _shown(qtbot, panel)
    assert _install_button(panel).isHidden()
    assert _licence_button(panel).isHidden()
    assert "up to date" in _state_text(panel)


def test_the_panel_builds_with_no_installer_argument(qtbot, tmp_path):
    panel = zp.ZoteroPanel(
        client=FakeClient(),
        bridge=FakeBridge(),
        journal_path=tmp_path / "journal.json",
        scopes_path=tmp_path / "scopes.json",
    )
    qtbot.addWidget(panel)
    _shown(qtbot, panel)
    assert _state_text(panel) == "No bundled bridge in this build."


# ----------------------------------------------------------------- the licence


def test_accepting_the_licence_stores_it_and_enables_install(qtbot, tmp_path):
    panel, store, asked = _panel(qtbot, tmp_path, installer=FakeInstaller(check(bi.STATE_NOT_INSTALLED)), licence_answer=True)
    _shown(qtbot, panel)
    _licence_button(panel).click()
    qtbot.waitUntil(lambda: _install_button(panel).isEnabled())
    assert asked["licence"] == [bi.LICENCE_NOTICE_TEXT]
    assert _result_text(panel) == zp.LICENCE_ACCEPTED_TEXT
    assert SettingsStore(tmp_path / "settings.json").load().settings.licence_accepted is True
    assert _licence_button(panel).isHidden()


def test_declining_the_licence_stores_nothing_and_keeps_install_off(qtbot, tmp_path):
    panel, _store, _asked = _panel(qtbot, tmp_path, installer=FakeInstaller(check(bi.STATE_NOT_INSTALLED)), licence_answer=False)
    _shown(qtbot, panel)
    _licence_button(panel).click()
    qtbot.waitUntil(lambda: _result_text(panel) == zp.LICENCE_DECLINED_TEXT)
    assert not _install_button(panel).isEnabled()
    assert not (tmp_path / "settings.json").exists()


def test_without_a_settings_store_the_licence_cannot_be_saved_here(qtbot, tmp_path):
    panel, _store, asked = _panel(qtbot, tmp_path, installer=FakeInstaller(check(bi.STATE_NOT_INSTALLED)), with_store=False)
    _shown(qtbot, panel)
    assert _licence_note(panel).text() == zp.LICENCE_NO_STORE_TEXT
    _licence_button(panel).click()
    assert _result_text(panel) == zp.LICENCE_NO_STORE_TEXT
    assert asked["licence"] == []
    assert not _install_button(panel).isEnabled()


def test_a_licence_save_failure_names_only_the_error_class(qtbot, tmp_path):
    class FailingStore:
        def load(self):
            return SimpleNamespace(settings=SimpleNamespace(licence_accepted=False, licence_notice_sha256=""))

        def set_licence_accepted(self, value, *, notice_sha256=""):
            raise OSError(f"cannot write {tmp_path / 'settings.json'}")

    panel, _store, _asked = _panel(
        qtbot, tmp_path, installer=FakeInstaller(check(bi.STATE_NOT_INSTALLED)), settings=FailingStore(), licence_answer=True
    )
    _shown(qtbot, panel)
    _licence_button(panel).click()
    qtbot.waitUntil(lambda: _result_text(panel) == zp.LICENCE_SAVE_FAILED_TEXT.format("OSError"))
    assert str(tmp_path) not in _result_text(panel)


def test_a_licence_saved_for_an_older_notice_asks_again(qtbot, tmp_path):
    panel, _store, _asked = _panel(
        qtbot, tmp_path, installer=FakeInstaller(check(bi.STATE_NOT_INSTALLED)), settings=_stale_store(tmp_path)
    )
    _shown(qtbot, panel)
    assert not _install_button(panel).isEnabled()
    assert not _licence_button(panel).isHidden()
    assert _licence_note(panel).text() == zp.LICENCE_NEEDED_TEXT


def test_a_state_read_that_started_before_the_licence_save_cannot_undo_the_acceptance(qtbot, tmp_path):
    installer = FakeInstaller(check(bi.STATE_NOT_INSTALLED))
    panel, _store, _asked = _panel(qtbot, tmp_path, installer=installer, licence_answer=True)
    _shown(qtbot, panel)
    stale = panel._latest.get(zp.BRIDGE_INSTALL_STATE)
    assert stale is not None
    _licence_button(panel).click()
    qtbot.waitUntil(lambda: _install_button(panel).isEnabled())
    # The save restarts the state check, so the read that began before it is superseded.
    assert panel._latest.get(zp.BRIDGE_INSTALL_STATE) != stale
    # That older read reports the licence as not accepted, and it arrives after the save. It must be dropped.
    late = Delivered(panel._owner, zp._Done(zp.BRIDGE_INSTALL_STATE, stale, (installer.check(), False)))
    panel._on_delivered(late)
    assert _install_button(panel).isEnabled()
    assert _licence_button(panel).isHidden()


# ----------------------------------------------------------------- the install


def test_a_declined_install_confirmation_runs_nothing(qtbot, tmp_path):
    installer = FakeInstaller(check(bi.STATE_NOT_INSTALLED))
    panel, _store, asked = _panel(qtbot, tmp_path, installer=installer, settings=_accepted_store(tmp_path), confirm_answer=False)
    _shown(qtbot, panel)
    _install_button(panel).click()
    qtbot.waitUntil(lambda: _result_text(panel) == zp.INSTALL_CANCELLED_TEXT)
    assert installer.installs == []
    assert "Zotero must close" in asked["confirm"][0]


def test_a_confirmed_install_runs_with_no_digest_argument_and_shows_the_result(qtbot, tmp_path):
    installer = FakeInstaller(check(bi.STATE_NOT_INSTALLED))
    panel, _store, _asked = _panel(qtbot, tmp_path, installer=installer, settings=_accepted_store(tmp_path), confirm_answer=True)
    _shown(qtbot, panel)
    _install_button(panel).click()
    qtbot.waitUntil(lambda: installer.installs != [])
    qtbot.waitUntil(lambda: _result_text(panel) == installer.install_value.message)
    assert installer.installs == [{"allow_close": True, "allow_force_close": False, "skip_graceful": False}]


def test_a_close_that_failed_shows_the_installers_text_and_no_path(qtbot, tmp_path):
    installer = FakeInstaller(
        check(bi.STATE_NOT_INSTALLED),
        install_value=bi.InstallResult(bi.ST_CLOSE_FAILED, bi.CLOSE_FAILED_MESSAGE),
    )
    panel, _store, asked = _panel(qtbot, tmp_path, installer=installer, settings=_accepted_store(tmp_path), confirm_answer=True)
    _shown(qtbot, panel)
    _install_button(panel).click()
    qtbot.waitUntil(lambda: _result_text(panel) == bi.CLOSE_FAILED_MESSAGE)
    assert "api_token" not in _result_text(panel)
    assert str(tmp_path) not in _result_text(panel)
    assert asked["force"] == []  # a close that cannot be forced is not offered a force


def test_the_real_installer_behind_the_button_installs_into_a_temp_profile(qtbot, tmp_path):
    payload = PAYLOAD
    folder = tmp_path / "bundle" / bi.BUNDLE_SUBDIR
    folder.mkdir(parents=True)
    (folder / bi.XPI_FILENAME).write_bytes(payload)
    (folder / bi.BUNDLE_RECORD).write_text(
        json.dumps(
            {
                "addon_id": bi.ADDON_ID,
                "file": bi.XPI_FILENAME,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "version": "0.1.8",
            }
        ),
        encoding="utf-8",
    )
    root = tmp_path / "Zotero" / "Zotero"
    profile = root / "Profiles" / "p.default"
    profile.mkdir(parents=True)
    (profile / "prefs.js").write_text("// prefs\n", encoding="utf-8")
    (root / "profiles.ini").write_text(
        "[Profile0]\nPath=Profiles/p.default\nIsRelative=1\nDefault=1\n", encoding="utf-8"
    )
    commands = []

    def runner(argv):
        commands.append(list(argv))
        return 0

    settings = _accepted_store(tmp_path)
    installer = bi.ZoteroBridgeInstaller(
        bi.load_bundled_bridge(folder),
        data_dir=root,
        token_path=tmp_path / "state" / "zotero_bridge" / "api_token",
        process_list=lambda: "",
        runner=runner,
        sleep=lambda _s: None,
        platform="linux",
        settings=settings,
    )
    panel, _store, _asked = _panel(qtbot, tmp_path, installer=installer, settings=settings, confirm_answer=True)
    _shown(qtbot, panel)
    _install_button(panel).click()
    qtbot.waitUntil(lambda: (profile / "extensions" / bi.XPI_FILENAME).exists())
    qtbot.waitUntil(lambda: _result_text(panel).startswith("Installed the Zotero bridge 0.1.8."))
    assert (profile / "extensions" / bi.XPI_FILENAME).read_bytes() == payload
    assert commands == []  # nothing was closed, and no process was started


def test_an_install_that_raises_shows_the_install_failure_text_not_the_check_text(qtbot, tmp_path):
    class _RaisingInstaller(FakeInstaller):
        def install(self, *, allow_close=True, allow_force_close=False, skip_graceful=False):
            self.installs.append({"allow_force_close": allow_force_close})
            raise OSError("the install failed inside the installer")

    installer = _RaisingInstaller(check(bi.STATE_NOT_INSTALLED))
    panel, _store, _asked = _panel(qtbot, tmp_path, installer=installer, settings=_accepted_store(tmp_path), confirm_answer=True)
    _shown(qtbot, panel)
    _install_button(panel).click()
    qtbot.waitUntil(lambda: _result_text(panel) == zp.INSTALL_FAILED_TEXT)
    assert "check failed" not in _result_text(panel).lower()
    assert "nothing is known to have been written" in _result_text(panel)
    assert "Press Refresh" in _result_text(panel)


# ----------------------------------------------------------------- the force-close question (Z1)


def test_a_close_that_does_not_finish_asks_whether_to_force_close_and_no_writes_nothing(qtbot, tmp_path):
    installer = FakeInstaller(check(bi.STATE_NOT_INSTALLED), install_queue=[CAN_FORCE])
    panel, _store, asked = _panel(
        qtbot, tmp_path, installer=installer, settings=_accepted_store(tmp_path), confirm_answer=True, force_answer=(False, False)
    )
    _shown(qtbot, panel)
    _install_button(panel).click()
    qtbot.waitUntil(lambda: len(asked["force"]) == 1)
    qtbot.waitUntil(lambda: _result_text(panel) == bi.CLOSE_FAILED_MESSAGE)
    # The install ran once, with no forced close, and the No answer started no second install.
    assert len(installer.installs) == 1
    assert installer.installs[0]["allow_force_close"] is False
    stored = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert "zotero_force_close_allowed" not in stored  # nothing was stored


def test_yes_runs_the_install_again_with_a_forced_close_allowed_and_stores_no_opt_in_when_unticked(qtbot, tmp_path):
    path = tmp_path / "settings.json"
    installer = FakeInstaller(check(bi.STATE_NOT_INSTALLED), install_queue=[CAN_FORCE, INSTALLED_NOW], settings_path=path)
    panel, _store, asked = _panel(
        qtbot, tmp_path, installer=installer, settings=_accepted_store(tmp_path), confirm_answer=True, force_answer=(True, False)
    )
    _shown(qtbot, panel)
    _install_button(panel).click()
    qtbot.waitUntil(lambda: len(installer.installs) == 2)
    qtbot.waitUntil(lambda: _result_text(panel) == INSTALLED_NOW.message)
    assert installer.installs[1]["allow_force_close"] is True
    assert installer.installs[0]["skip_graceful"] is False  # the first run waits the normal close window
    assert installer.installs[1]["skip_graceful"] is True  # the owner said yes, so the window is not waited a second time
    assert installer.opt_in_seen == [False, False]  # Force close next time was not ticked, so nothing was stored
    assert "zotero_force_close_allowed" not in json.loads(path.read_text(encoding="utf-8"))
    assert len(asked["force"]) == 1


def test_ticking_force_close_next_time_stores_the_opt_in_before_the_forced_install_runs(qtbot, tmp_path):
    path = tmp_path / "settings.json"
    installer = FakeInstaller(check(bi.STATE_NOT_INSTALLED), install_queue=[CAN_FORCE, INSTALLED_NOW], settings_path=path)
    panel, _store, _asked = _panel(
        qtbot, tmp_path, installer=installer, settings=_accepted_store(tmp_path), confirm_answer=True, force_answer=(True, True)
    )
    _shown(qtbot, panel)
    _install_button(panel).click()
    qtbot.waitUntil(lambda: len(installer.installs) == 2)
    qtbot.waitUntil(lambda: _result_text(panel) == INSTALLED_NOW.message)
    # The opt-in was on disk when the forced install started, and it is on disk now.
    assert installer.opt_in_seen == [False, True]
    assert installer.installs[1]["allow_force_close"] is True
    assert installer.installs[1]["skip_graceful"] is True  # the forced run skips the window, and the opt-in is stored too
    assert SettingsStore(path).load().settings.zotero_force_close_allowed is True


def test_a_force_close_opt_in_that_cannot_be_saved_stops_the_forced_install(qtbot, tmp_path):
    installer = FakeInstaller(check(bi.STATE_NOT_INSTALLED), install_queue=[CAN_FORCE])
    store = _OptInWriteFails(tmp_path / "settings.json")
    store.set_licence_accepted(True, notice_sha256=bi.LICENCE_NOTICE_SHA256)
    panel, _store, _asked = _panel(
        qtbot, tmp_path, installer=installer, settings=store, confirm_answer=True, force_answer=(True, True)
    )
    _shown(qtbot, panel)
    _install_button(panel).click()
    qtbot.waitUntil(lambda: _result_text(panel) == zp.FORCE_NOT_SAVED_TEXT.format("OSError"))
    assert len(installer.installs) == 1  # the forced install never ran
    assert str(tmp_path) not in _result_text(panel)


def _answer_force_box(monkeypatch, *, answer, tick):
    """Stands in for QMessageBox.exec: ticks the checkbox when asked, then clicks the button named ``answer``. None clicks nothing,
    as closing the dialog does."""

    def fake_exec(self):
        if tick:
            self.findChild(QCheckBox).setChecked(True)
        for button in self.buttons():
            if button.text() == answer:
                button.click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)


def test_the_real_force_close_question_returns_yes_no_and_the_checkbox(qtbot, monkeypatch):
    _answer_force_box(monkeypatch, answer="Yes", tick=True)
    assert zp.ask_force_close(None) == (True, True)
    _answer_force_box(monkeypatch, answer="Yes", tick=False)
    assert zp.ask_force_close(None) == (True, False)
    _answer_force_box(monkeypatch, answer="No", tick=True)
    assert zp.ask_force_close(None) == (False, False)  # No never stores the opt-in, even with the box ticked
    _answer_force_box(monkeypatch, answer=None, tick=True)
    assert zp.ask_force_close(None) == (False, False)  # closing the dialog is no


def test_an_opt_in_already_on_passes_a_forced_close_and_does_not_ask(qtbot, tmp_path):
    store = _accepted_store(tmp_path)
    store.set_zotero_force_close_allowed(True)
    installer = FakeInstaller(check(bi.STATE_NOT_INSTALLED), install_queue=[CAN_FORCE])
    panel, _store, asked = _panel(
        qtbot, tmp_path, installer=installer, settings=store, confirm_answer=True, force_answer=(False, False)
    )
    _shown(qtbot, panel)
    _install_button(panel).click()
    qtbot.waitUntil(lambda: _result_text(panel) == bi.CLOSE_FAILED_MESSAGE)
    assert installer.installs[0]["allow_force_close"] is True  # the opt-in is passed on the first run
    assert installer.installs[0]["skip_graceful"] is False  # but the opt-in alone still waits the normal close window
    assert asked["force"] == []  # and the question is skipped


def test_no_answer_to_the_force_question_runs_no_second_install_and_stores_nothing(qtbot, tmp_path):
    path = tmp_path / "settings.json"
    installer = FakeInstaller(check(bi.STATE_NOT_INSTALLED), install_queue=[CAN_FORCE], settings_path=path)
    panel, _store, asked = _panel(
        qtbot, tmp_path, installer=installer, settings=_accepted_store(tmp_path), confirm_answer=True, force_answer=(False, True)
    )
    _shown(qtbot, panel)
    _install_button(panel).click()
    qtbot.waitUntil(lambda: _result_text(panel) == bi.CLOSE_FAILED_MESSAGE)
    assert len(asked["force"]) == 1
    assert len(installer.installs) == 1  # the No answer started no second install
    assert "zotero_force_close_allowed" not in json.loads(path.read_text(encoding="utf-8"))  # and stored no opt-in


# ----------------------------------------------------------------- the update check and its switch (Z2)


def test_update_checks_off_never_calls_the_update_check(qtbot, tmp_path):
    installer = FakeInstaller(check(bi.STATE_NOT_INSTALLED))
    panel, _store, _asked = _panel(qtbot, tmp_path, installer=installer, settings=_accepted_store(tmp_path))
    _shown(qtbot, panel)
    qtbot.wait(50)
    assert installer.update_calls == 0
    assert _update_line(panel).isHidden()


def test_update_checks_on_run_at_start_and_again_when_the_tab_first_opens(qtbot, tmp_path):
    store = _accepted_store(tmp_path)
    store.set_zotero_update_checks(True)
    installer = FakeInstaller(check(bi.STATE_CURRENT, installed="0.1.8"))
    panel, _store, _asked = _panel(qtbot, tmp_path, installer=installer, settings=store)
    qtbot.waitUntil(lambda: installer.update_calls == 1)  # at start, with the panel built and the tab not yet open
    _shown(qtbot, panel)
    qtbot.waitUntil(lambda: installer.update_calls == 2)  # when the tab is first opened
    qtbot.wait(50)
    assert installer.update_calls == 2
    assert installer.installs == []


def test_a_newer_bundled_bridge_shows_the_line_and_installs_nothing(qtbot, tmp_path):
    store = _accepted_store(tmp_path)
    store.set_zotero_update_checks(True)
    installer = FakeInstaller(check(bi.STATE_OLDER, installed="0.1.7"))
    panel, _store, _asked = _panel(qtbot, tmp_path, installer=installer, settings=store)
    _shown(qtbot, panel)
    qtbot.waitUntil(lambda: not _update_line(panel).isHidden())
    text = _update_line(panel).text()
    assert text == zp.UPDATE_AVAILABLE_TEXT.format(bundled="0.1.8", installed="0.1.7")
    assert "can be installed below" in text
    assert installer.installs == []


def test_a_bridge_that_is_up_to_date_shows_no_update_line(qtbot, tmp_path):
    store = _accepted_store(tmp_path)
    store.set_zotero_update_checks(True)
    installer = FakeInstaller(check(bi.STATE_CURRENT, installed="0.1.8"))
    panel, _store, _asked = _panel(qtbot, tmp_path, installer=installer, settings=store)
    _shown(qtbot, panel)
    qtbot.waitUntil(lambda: installer.update_calls == 2)
    qtbot.wait(50)
    assert _update_line(panel).isHidden()


def test_a_successful_install_hides_the_update_line_even_while_the_registry_still_reads_the_old_version(qtbot, tmp_path):
    """Once the bundled bridge is installed the line goes. Zotero's registry can still read the old version until Zotero starts,
    and a check that reads it must not bring the line back."""
    store = _accepted_store(tmp_path)
    store.set_zotero_update_checks(True)
    installer = FakeInstaller(check(bi.STATE_OLDER, installed="0.1.7"), install_queue=[INSTALLED_NOW])
    panel, _store, _asked = _panel(qtbot, tmp_path, installer=installer, settings=store, confirm_answer=True)
    _shown(qtbot, panel)
    qtbot.waitUntil(lambda: not _update_line(panel).isHidden())
    _install_button(panel).click()
    qtbot.waitUntil(lambda: _result_text(panel) == INSTALLED_NOW.message)
    assert _update_line(panel).isHidden()
    qtbot.wait(50)
    assert _update_line(panel).isHidden()


def test_a_failed_install_leaves_the_update_line_showing(qtbot, tmp_path):
    """Only a successful install hides the line. A close that failed installs nothing, so the update is still pending."""
    store = _accepted_store(tmp_path)
    store.set_zotero_update_checks(True)
    installer = FakeInstaller(check(bi.STATE_OLDER, installed="0.1.7"), install_queue=[CAN_FORCE])
    panel, _store, _asked = _panel(
        qtbot, tmp_path, installer=installer, settings=store, confirm_answer=True, force_answer=(False, False)
    )
    _shown(qtbot, panel)
    qtbot.waitUntil(lambda: not _update_line(panel).isHidden())
    _install_button(panel).click()
    qtbot.waitUntil(lambda: _result_text(panel) == bi.CLOSE_FAILED_MESSAGE)
    assert not _update_line(panel).isHidden()


def test_the_update_checkbox_saves_the_switch_and_unticking_it_clears_it(qtbot, tmp_path):
    path = tmp_path / "settings.json"
    panel, _store, _asked = _panel(
        qtbot, tmp_path, installer=FakeInstaller(check(bi.STATE_NOT_INSTALLED)), settings=_accepted_store(tmp_path)
    )
    box = _named(panel, QCheckBox, "zotero-update-checks")
    assert box.text() == zp.UPDATE_CHECK_TEXT
    assert box.isChecked() is False
    box.setChecked(True)
    assert SettingsStore(path).load().settings.zotero_update_checks is True
    box.setChecked(False)
    assert SettingsStore(path).load().settings.zotero_update_checks is False
    assert "zotero_update_checks" not in json.loads(path.read_text(encoding="utf-8"))


def test_a_failed_update_switch_save_puts_the_box_back_and_names_only_the_class(qtbot, tmp_path):
    store = _UpdateSwitchWriteFails(tmp_path / "settings.json")
    store.set_licence_accepted(True, notice_sha256=bi.LICENCE_NOTICE_SHA256)
    panel, _store, _asked = _panel(qtbot, tmp_path, installer=FakeInstaller(check(bi.STATE_NOT_INSTALLED)), settings=store)
    box = _named(panel, QCheckBox, "zotero-update-checks")
    box.setChecked(True)
    assert box.isChecked() is False
    assert _result_text(panel) == zp.UPDATE_CHECK_NOT_SAVED_TEXT.format("OSError")
    assert str(tmp_path) not in _result_text(panel)


def test_the_update_checkbox_is_off_and_disabled_without_a_settings_store(qtbot, tmp_path):
    panel, _store, _asked = _panel(qtbot, tmp_path, installer=FakeInstaller(check(bi.STATE_NOT_INSTALLED)), with_store=False)
    box = _named(panel, QCheckBox, "zotero-update-checks")
    assert not box.isEnabled()
    assert not box.isChecked()


# ----------------------------------------------------------------- the worker functions


def test_a_stale_licence_refuses_the_install_before_anything_closes(tmp_path):
    host = _Recorder()
    store = _stale_store(tmp_path)
    done = zp._run_install(_real_installer(tmp_path, host, settings=store), store)
    assert done.result.status == bi.ST_LICENCE_NOT_ACCEPTED
    assert host.commands == []
    assert host.sleeps == 0
    assert not (tmp_path / "state").exists()  # nothing was written


def test_the_worker_refuses_the_real_install_when_no_licence_is_saved(tmp_path):
    host = _Recorder()
    store = SettingsStore(tmp_path / "settings.json")
    done = zp._run_install(_real_installer(tmp_path, host, settings=store), store)
    assert done.result.status == bi.ST_LICENCE_NOT_ACCEPTED
    assert host.commands == []


def test_the_worker_passes_no_digest_and_no_forced_close_when_none_is_allowed(tmp_path):
    installer = FakeInstaller(check(bi.STATE_NOT_INSTALLED))
    done = zp._run_install(installer, SettingsStore(tmp_path / "settings.json"))
    assert installer.installs == [{"allow_close": True, "allow_force_close": False, "skip_graceful": False}]
    assert done.force_allowed is False


def test_only_the_owners_yes_to_force_skips_the_graceful_window(tmp_path):
    """The owner's yes (force_now) skips the normal close window. An opt-in alone still waits that window before it forces."""
    installer = FakeInstaller(check(bi.STATE_NOT_INSTALLED))
    store = SettingsStore(tmp_path / "settings.json")
    yes = zp._run_install(installer, store, force_now=True)
    store.set_zotero_force_close_allowed(True)
    opted_in = zp._run_install(installer, store)
    assert installer.installs == [
        {"allow_close": True, "allow_force_close": True, "skip_graceful": True},
        {"allow_close": True, "allow_force_close": True, "skip_graceful": False},
    ]
    assert yes.force_allowed is True
    assert opted_in.force_allowed is True


# ----------------------------------------------------------------- pure texts


def test_the_state_text_covers_each_state_and_never_shows_a_path():
    cases = {
        check(bi.STATE_NOT_INSTALLED): "not installed. The bundled version is 0.1.8.",
        check(bi.STATE_OLDER, installed="0.1.7"): "installed 0.1.7, older than the bundled 0.1.8.",
        check(bi.STATE_UNKNOWN, installed="beta"): "the installed version could not be read. The bundled version is 0.1.8.",
        check(bi.STATE_CURRENT, installed="0.1.8"): "installed 0.1.8, up to date.",
        check(bi.STATE_NEWER, installed="0.1.9"): "installed 0.1.9, newer than the bundled 0.1.8.",
        check(bi.STATE_NO_BUNDLE, bundled=None): "No bundled bridge in this build.",
        check(bi.STATE_NO_PROFILE, installed=None, profile_found=False): "Zotero's default profile was not found.",
        check(bi.STATE_UNREADABLE): "extensions.json could not be read",
        check(bi.STATE_DAMAGED, bundled=None): "damaged",
    }
    for value, expected in cases.items():
        text = zp.bridge_state_text(value)
        assert expected in text
        assert "\\" not in text and "AppData" not in text and "api_token" not in text
    assert zp.bridge_state_text(None) == zp.INSTALL_CHECKING_TEXT


def test_a_disabled_entry_adds_the_registration_note():
    text = zp.bridge_state_text(check(bi.STATE_OLDER, installed="0.1.7", registration=bi.REG_DISABLED))
    assert zp.BRIDGE_REG_DISABLED in text


def test_the_install_confirmation_says_what_is_written_and_what_happens_when_zotero_does_not_close():
    text = zp.bridge_install_confirm_text(check(bi.STATE_NOT_INSTALLED))
    assert text.startswith("Install the Zotero bridge 0.1.8 in your Zotero profile.")
    assert "Zotero must close first" in text
    assert "never force-closes" not in text  # the old sentence is gone
    assert "asks you before it forces Zotero to close" in text
    assert "Force close next time" in text
    assert "api_token" not in text and "AppData" not in text
    assert zp.bridge_install_confirm_text(check(bi.STATE_OLDER, installed="0.1.7")).startswith("Update the Zotero bridge")


def test_the_install_confirmation_names_the_three_user_js_lines_the_install_writes():
    text = zp.bridge_install_confirm_text(check(bi.STATE_NOT_INSTALLED))
    assert "three lines in the profile's user.js" in text
    assert "two lines" not in text
