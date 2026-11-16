"""The restart banner's buttons: Restart app appears for a code change and calls back; the local server's controls appear
for a server change; Dismiss still works. No process is started and no file is read: the watch is a fake.
"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QPushButton, QWidget

from meridian_desktop.restart_check import Restart
from meridian_desktop.ui.restart_banner import DISMISS_TEXT, RESTART_APP_TEXT, RestartBanner


class _FakeWatch:
    def __init__(self, restart):
        self.restart = restart

    def check(self):
        return self.restart


class _BusyControls(QWidget):
    """Stands in for ScopeControls: the busy state is set by the test, and busyChanged is emitted as the real one does."""

    busyChanged = Signal(bool)

    def __init__(self):
        super().__init__()
        self.own = False
        self.other = False

    def in_flight(self):
        return self.own

    def scope_busy(self):
        return self.own or self.other

    def set_busy(self, own, other=False):
        self.own, self.other = own, other
        self.busyChanged.emit(self.scope_busy())


def _button(banner, name):
    return banner.findChild(QPushButton, name)


def test_a_code_change_shows_restart_app_and_clicking_it_calls_back(qtbot):
    calls = []
    banner = RestartBanner(_FakeWatch(Restart(code=("core.py",))), restart_app=lambda: calls.append("restart"))
    qtbot.addWidget(banner)
    banner.check_now()
    button = _button(banner, "restart-banner-app")
    assert button.text() == RESTART_APP_TEXT
    assert not button.isHidden()
    button.click()
    assert calls == ["restart"]


def test_without_a_restart_callback_there_is_no_restart_app_button(qtbot):
    banner = RestartBanner(_FakeWatch(Restart(code=("core.py",))))
    qtbot.addWidget(banner)
    banner.check_now()
    assert _button(banner, "restart-banner-app").isHidden()


def test_a_server_change_shows_the_server_controls_and_not_restart_app(qtbot):
    server_controls = QWidget()
    server_controls.setObjectName("fake-server-controls")
    banner = RestartBanner(
        _FakeWatch(Restart(server=True)),
        restart_app=lambda: None,
        server_controls=server_controls,
    )
    qtbot.addWidget(banner)
    banner.check_now()
    assert not server_controls.isHidden()
    assert _button(banner, "restart-banner-app").isHidden()


def test_a_code_only_change_hides_the_server_controls(qtbot):
    server_controls = QWidget()
    banner = RestartBanner(
        _FakeWatch(Restart(code=("core.py",))),
        restart_app=lambda: None,
        server_controls=server_controls,
    )
    qtbot.addWidget(banner)
    banner.check_now()
    assert server_controls.isHidden()
    assert not _button(banner, "restart-banner-app").isHidden()


def test_dismiss_still_hides_the_banner_with_the_new_buttons_present(qtbot):
    banner = RestartBanner(_FakeWatch(Restart(code=("core.py",))), restart_app=lambda: None)
    qtbot.addWidget(banner)
    banner.check_now()
    dismiss = _button(banner, "restart-banner-dismiss")
    assert dismiss.text() == DISMISS_TEXT
    dismiss.click()
    assert banner.isHidden()


def test_a_check_mid_action_keeps_the_banner_and_the_server_controls_shown(qtbot):
    # The server record clears when the server stops, so a check during the strip's own Restart reports no change.
    watch = _FakeWatch(Restart(server=True))
    controls = _BusyControls()
    banner = RestartBanner(watch, restart_app=lambda: None, server_controls=controls)
    qtbot.addWidget(banner)
    banner.check_now()
    assert not banner.isHidden()
    controls.set_busy(True)
    watch.restart = Restart()
    banner.check_now()
    assert not banner.isHidden()
    assert not controls.isHidden()
    # The action has ended, so the next check may hide the strip.
    controls.set_busy(False)
    banner.check_now()
    assert banner.isHidden()


def test_restart_app_is_off_while_the_local_scope_is_busy(qtbot):
    controls = _BusyControls()
    banner = RestartBanner(
        _FakeWatch(Restart(code=("core.py",), server=True)), restart_app=lambda: None, server_controls=controls
    )
    qtbot.addWidget(banner)
    banner.check_now()
    button = _button(banner, "restart-banner-app")
    assert button.isEnabled()
    controls.set_busy(True)
    assert not button.isEnabled()
    controls.set_busy(False)
    assert button.isEnabled()
    # Another control (Settings) holding the scope turns it off too.
    controls.set_busy(False, other=True)
    assert not button.isEnabled()
