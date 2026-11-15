"""The restart banner's buttons: Restart app appears for a code change and calls back; the local server's controls appear
for a server change; Dismiss still works. No process is started and no file is read: the watch is a fake.
"""

from PySide6.QtWidgets import QPushButton, QWidget

from meridian_desktop.restart_check import Restart
from meridian_desktop.ui.restart_banner import DISMISS_TEXT, RESTART_APP_TEXT, RestartBanner


class _FakeWatch:
    def __init__(self, restart):
        self.restart = restart

    def check(self):
        return self.restart


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
