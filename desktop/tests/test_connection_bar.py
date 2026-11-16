"""Tests for the connection bar (meridian_desktop/ui/connection_bar.py), with pytest-qt.

The bar's check runs on the thread pool, so each test waits for the bar to show its text. The keychain, the hosted
transport, the local probe and the clock are fakes, so nothing touches the network, the real keychain or a real server.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from PySide6.QtWidgets import QLabel, QPushButton

from _fakes import MemoryKeyring, ScriptedTransport
from meridian_desktop import hosted_session as hs
from meridian_desktop import local_client
from meridian_desktop.data_source import DataSources
from meridian_desktop.hosted_client import Reply
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui.connection_bar import ConnectionBar
from meridian_desktop.token_store import set_token
from meridian_desktop.ui.main_window import MainWindow

HOSTED = "https://usemeridian.us"
EMAIL = "owner@example.test"
ACCESS = "sk_meridian_fake-bar-access-0001"
NEW_ACCESS = "sk_meridian_fake-bar-access-0002"
NOW = datetime(2027, 1, 1, 12, tzinfo=timezone.utc).timestamp()
FAR = datetime(2027, 3, 2, 12, tzinfo=timezone.utc).timestamp()
SOON = NOW + 10 * 86400
NOT_CONNECTED = "Not connected: sign in again."
CHECKING = "Checking the connection..."


def _json(status: int, data: dict) -> Reply:
    return Reply(status, json.dumps(data).encode("utf-8"))


def _me_ok() -> Reply:
    return _json(200, {"email": EMAIL})


def _not_running(url: str) -> str:
    return local_client.HEALTH_NOT_RUNNING


def _running(url: str) -> str:
    return local_client.HEALTH_RUNNING


def _no_read(*args, **kwargs):
    raise AssertionError("the main window reads nothing in these tests")


def _save_session(secrets, *, expires_at: float = FAR) -> None:
    grant = hs.TokenGrant(access_token=ACCESS, expires_in=int(expires_at - NOW))
    hs.save_session(secrets, grant, account=EMAIL, now=NOW)


def _secrets():
    return KeyringSecretStore(backend=MemoryKeyring())


def _bar(qtbot, settings_path, secrets, transport, *, probe=_not_running, clock=None, mode="hosted") -> ConnectionBar:
    # Hosted mode by default: the bar's checks reach the hosted service. The Local mode tests pass mode="local".
    SettingsStore(settings_path).save(Settings(hosted_url=HOSTED, mode=mode))
    bar = ConnectionBar(
        settings_store=SettingsStore(settings_path),
        secrets=secrets,
        probe=probe,
        transport=transport,
        clock=clock if clock is not None else (lambda: NOW),
    )
    qtbot.addWidget(bar)
    return bar


def _text(bar, name: str) -> str:
    return bar.findChild(QLabel, name).text()


def test_the_bar_shows_the_account_with_no_sign_in_prompt_when_the_expiry_is_not_recorded(qtbot, settings_path):
    secrets = _secrets()
    set_token(ACCESS, store=secrets)
    bar = _bar(qtbot, settings_path, secrets, ScriptedTransport({"/me": [_me_ok()]}))

    bar.start()

    qtbot.waitUntil(lambda: _text(bar, "connection-bar-headline") == f"Signed in: {EMAIL}.", timeout=5000)
    assert bar.findChild(QPushButton, "connection-bar-sign-in").isHidden()


def test_the_sign_in_button_stays_hidden_while_the_session_has_a_recorded_expiry_far_away(qtbot, settings_path):
    secrets = _secrets()
    _save_session(secrets)
    bar = _bar(qtbot, settings_path, secrets, ScriptedTransport({"/me": [_me_ok()]}))

    bar.start()

    qtbot.waitUntil(lambda: bar.property("state") == "connected", timeout=5000)
    assert bar.findChild(QPushButton, "connection-bar-sign-in").isHidden()


def test_before_the_first_check_the_bar_says_it_is_checking(qtbot, settings_path):
    bar = _bar(qtbot, settings_path, _secrets(), ScriptedTransport({}))

    assert _text(bar, "connection-bar-headline") == CHECKING
    assert bar.property("state") == "checking"


def test_with_no_token_the_bar_shows_no_sign_in_prompt_and_sends_nothing_to_the_hosted_host(qtbot, settings_path):
    transport = ScriptedTransport({})
    bar = _bar(qtbot, settings_path, _secrets(), transport)

    bar.start()

    qtbot.waitUntil(lambda: bar.property("state") == "not_connected", timeout=5000)
    assert bar.findChild(QLabel, "connection-bar-headline").isHidden()
    assert bar.findChild(QPushButton, "connection-bar-sign-in").isHidden()
    assert _text(bar, "connection-bar-hosted") == "Not connected: Hosted (usemeridian.us)."
    assert _text(bar, "connection-bar-local") == "Local: none."
    assert transport.requests == []


def test_a_connected_session_shows_the_account_the_date_the_hosted_line_and_the_local_server(qtbot, settings_path):
    secrets = _secrets()
    _save_session(secrets)
    transport = ScriptedTransport({"/me": [_me_ok()]})
    bar = _bar(qtbot, settings_path, secrets, transport, probe=_running)

    bar.start()

    expected = f"Signed in: {EMAIL}. Signed in until 2027-03-02."
    qtbot.waitUntil(lambda: _text(bar, "connection-bar-headline") == expected, timeout=5000)
    assert _text(bar, "connection-bar-hosted") == "Connected: Hosted (usemeridian.us)."
    assert _text(bar, "connection-bar-local") == "Local: Meridian server at 127.0.0.1:7878 (live)."
    assert bar.property("state") == "connected"


def test_under_fourteen_days_the_bar_is_in_the_warning_state(qtbot, settings_path):
    secrets = _secrets()
    _save_session(secrets, expires_at=SOON)
    bar = _bar(qtbot, settings_path, secrets, ScriptedTransport({"/me": [_me_ok()]}))

    bar.start()

    qtbot.waitUntil(lambda: bar.property("state") == "warning", timeout=5000)
    assert "Under 14 days left: sign in again before then." in _text(bar, "connection-bar-headline")


def test_a_rejected_token_shows_not_connected_and_asks_for_a_new_sign_in(qtbot, settings_path):
    secrets = _secrets()
    _save_session(secrets)
    bar = _bar(qtbot, settings_path, secrets, ScriptedTransport({"/me": [_json(401, {"detail": "invalid API token"})]}))

    bar.start()

    qtbot.waitUntil(lambda: _text(bar, "connection-bar-headline") == NOT_CONNECTED, timeout=5000)
    assert _text(bar, "connection-bar-hosted") == "Not connected: Hosted (usemeridian.us)."
    assert bar.property("state") == "not_connected"


def test_an_unreachable_hosted_server_shows_reconnecting_and_keeps_the_account(qtbot, settings_path):
    secrets = _secrets()
    _save_session(secrets)
    bar = _bar(qtbot, settings_path, secrets, ScriptedTransport({"/me": [Reply(503, b"")]}))

    bar.start()

    qtbot.waitUntil(lambda: bar.property("state") == "reconnecting", timeout=5000)
    assert _text(bar, "connection-bar-headline") == f"Signed in: {EMAIL}. Signed in until 2027-03-02."
    assert _text(bar, "connection-bar-hosted") == "Reconnecting: Hosted (usemeridian.us)."


def test_a_check_that_raises_is_shown_as_reconnecting_with_only_the_class_name(qtbot, settings_path):
    def broken_clock() -> float:
        raise RuntimeError("clock broke with a secret-looking detail")

    bar = _bar(qtbot, settings_path, _secrets(), ScriptedTransport({}), clock=broken_clock)

    bar.start()

    qtbot.waitUntil(lambda: bar.property("state") == "reconnecting", timeout=5000)
    headline = _text(bar, "connection-bar-headline")
    assert headline == "Connection check failed (RuntimeError). It is tried again in five minutes."
    assert "secret-looking" not in headline


def test_signing_in_and_checking_again_updates_the_bar(qtbot, settings_path):
    secrets = _secrets()
    transport = ScriptedTransport({"/me": [_me_ok()]})
    bar = _bar(qtbot, settings_path, secrets, transport)
    bar.start()
    qtbot.waitUntil(lambda: bar.property("state") == "not_connected", timeout=5000)

    _save_session(secrets)
    bar.refresh()

    qtbot.waitUntil(lambda: _text(bar, "connection-bar-headline").startswith("Signed in: "), timeout=5000)
    assert bar.property("state") == "connected"


def test_the_main_window_puts_the_connection_bar_first_and_checks_again_after_a_settings_sign_in(qtbot, settings_path):
    secrets = _secrets()
    SettingsStore(settings_path).save(Settings(hosted_url=HOSTED, mode="hosted"))
    transport = ScriptedTransport({"/me": [_me_ok()]})
    sources = DataSources(
        probe=_not_running,
        read_server=_no_read,
        read_file=_no_read,
        get_token=lambda: None,
        list_hosted=_no_read,
    )
    window = MainWindow(
        SettingsStore(settings_path),
        secret_store=secrets,
        data_sources=sources,
        connection_transport=transport,
    )
    qtbot.addWidget(window)
    bar = window.findChild(ConnectionBar)
    first = window.centralWidget().layout().itemAt(0).widget()

    assert first is bar
    assert bar.objectName() == "connection-bar"
    qtbot.waitUntil(lambda: bar.property("state") == "not_connected", timeout=5000)

    _save_session(secrets)
    window._settings_tab.signedInChanged.emit()

    qtbot.waitUntil(lambda: _text(bar, "connection-bar-headline").startswith("Signed in: "), timeout=5000)


# ----------------------------------------------------------- Local mode

LOCAL_MODE_LINE = "Local mode: hosted sign-in is off. Switch to Hosted mode to sign in."


def test_local_mode_says_hosted_sign_in_is_off_and_offers_no_sign_in_control_even_with_a_saved_token(qtbot, settings_path):
    secrets = _secrets()
    _save_session(secrets)
    transport = ScriptedTransport({"/me": [_me_ok()]})
    bar = _bar(qtbot, settings_path, secrets, transport, mode="local")

    bar.start()

    qtbot.waitUntil(lambda: _text(bar, "connection-bar-headline") == LOCAL_MODE_LINE, timeout=5000)
    assert bar.property("state") == "not_connected"
    assert _text(bar, "connection-bar-hosted") == "Not connected: Hosted (usemeridian.us)."
    assert bar.findChild(QPushButton, "connection-bar-sign-in").isHidden()
    assert all(button.isHidden() for button in bar.findChildren(QPushButton)), "no sign-in control is offered in Local mode"


def test_local_mode_with_no_token_shows_the_local_line_offers_no_sign_in_and_sends_nothing(qtbot, settings_path):
    transport = ScriptedTransport({})
    bar = _bar(qtbot, settings_path, _secrets(), transport, mode="local")

    bar.start()

    qtbot.waitUntil(lambda: _text(bar, "connection-bar-headline") == LOCAL_MODE_LINE, timeout=5000)
    assert all(button.isHidden() for button in bar.findChildren(QPushButton))
    assert transport.requests == []


def test_local_mode_the_timer_tick_and_check_send_no_hosted_request_but_still_probe_the_local_server(qtbot, settings_path):
    secrets = _secrets()
    _save_session(secrets)
    transport = ScriptedTransport({"/me": [_me_ok()]})
    probes: list[str] = []

    def probe(url: str) -> str:
        probes.append(url)
        return local_client.HEALTH_RUNNING

    bar = _bar(qtbot, settings_path, secrets, transport, probe=probe, mode="local")
    bar.start()
    qtbot.waitUntil(lambda: _text(bar, "connection-bar-headline") == LOCAL_MODE_LINE, timeout=5000)
    assert len(probes) == 1  # the first check probed the local server once

    bar._timer.timeout.emit()  # noqa: SLF001 - the five-minute tick, through the timer's own connection
    qtbot.waitUntil(lambda: len(probes) == 2, timeout=5000)
    bar.refresh()  # Check: the slot the main window calls after a sign-in or sign-out
    qtbot.waitUntil(lambda: len(probes) == 3, timeout=5000)

    # Each check probed the local server, so the three checks did run, and none of them reached the hosted host.
    assert transport.requests == [], "the timer and Check sent nothing to the hosted host in Local mode"
    assert _text(bar, "connection-bar-local") == "Local: Meridian server at 127.0.0.1:7878 (live)."


def test_hosted_mode_the_same_timer_tick_and_check_do_reach_the_hosted_host(qtbot, settings_path):
    """The control for the Local mode test: the same tick and Check, in Hosted mode, do call the hosted host."""
    secrets = _secrets()
    _save_session(secrets)
    transport = ScriptedTransport({"/me": [_me_ok()]})
    bar = _bar(qtbot, settings_path, secrets, transport, probe=_running, mode="hosted")
    bar.start()
    qtbot.waitUntil(lambda: bar.property("state") == "connected", timeout=5000)
    assert len(transport.requests) == 1

    bar._timer.timeout.emit()  # noqa: SLF001 - the five-minute tick, through the timer's own connection
    qtbot.waitUntil(lambda: len(transport.requests) == 2, timeout=5000)
    bar.refresh()
    qtbot.waitUntil(lambda: len(transport.requests) == 3, timeout=5000)
    assert transport.paths() == ["/me", "/me", "/me"]
