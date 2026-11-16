"""Tests for the known key services: the registry, the one-GET check, and the service cards on the Keys block.

No test reaches the network. Every check's transport is a fake that records the request and answers with a status or
raises. The keychain is a MemoryKeyring, so nothing touches the real credential store. FAKE_KEY is an obviously fake
value, so the leak checks can search every output string for it.
"""

from __future__ import annotations

import dataclasses
import io
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QComboBox, QLabel, QLineEdit, QPushButton, QWidget

from _fakes import FailingKeyring, MemoryKeyring
from meridian_desktop import key_services
from meridian_desktop.api_keys import KEY_SERVICE, ApiKeys, account_for
from meridian_desktop.key_services import (
    CHECK_TIMEOUT,
    NO_CHECK_TEXT,
    NO_KEY_TEXT,
    REJECTED_TEXT,
    SERVICES,
    UNREACHABLE_TEXT,
    WORKS_TEXT,
    KeyService,
    check_saved_key,
    service_for,
)
from meridian_desktop.secret_store import KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui import keys_section as keys_module
from meridian_desktop.ui.keys_section import (
    CHECK_TEXT,
    CHECKING_TEXT,
    CREATE_TEXT,
    OTHER_CHOICE,
    SERVICE_CHOICES,
    KeysSection,
)

FAKE_KEY = "sk-fake-key-services-0001"
OPENAI = service_for("OpenAI")
ANTHROPIC = service_for("Anthropic")
GITHUB = service_for("GitHub")
# name, provider host, path, header that carries the key, the text before the key in that header
PROVIDERS = [
    ("OpenAI", "api.openai.com", "/v1/models", "Authorization", "Bearer "),
    ("Anthropic", "api.anthropic.com", "/v1/models", "x-api-key", ""),
    ("GitHub", "api.github.com", "/user", "Authorization", "Bearer "),
]


class FakeTransport:
    """Records each request and its timeout. It answers with a status, or raises the error it was given."""

    def __init__(self, status: int = 200, error: BaseException | None = None) -> None:
        self.status = status
        self.error = error
        self.calls: list[tuple[urllib.request.Request, float]] = []

    def __call__(self, request: urllib.request.Request, timeout: float) -> int:
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.status


class GatedTransport(FakeTransport):
    """A fake that holds each call until the gate is set, so a test can look at the section while a check runs."""

    def __init__(self, gate: threading.Event, status: int = 200) -> None:
        super().__init__(status)
        self.gate = gate

    def __call__(self, request, timeout):
        result = super().__call__(request, timeout)
        self.gate.wait(5)
        return result


def _header(request: urllib.request.Request, name: str) -> str | None:
    """A request header by name. urllib keeps header names capitalized in its own way, so the match ignores case."""
    headers = {key.lower(): value for key, value in request.header_items()}
    return headers.get(name.lower())


def _keychain(*saved: str) -> tuple[KeyringSecretStore, MemoryKeyring]:
    """A keychain with FAKE_KEY saved under each given name. Returns the store and the backend."""
    backend = MemoryKeyring()
    for name in saved:
        backend.set_password(KEY_SERVICE, account_for(name), FAKE_KEY)
    return KeyringSecretStore(backend=backend), backend


def _with_check(service: KeyService, **changes) -> KeyService:
    return dataclasses.replace(service, check=dataclasses.replace(service.check, **changes))


# ----------------------------------------------------------- registry


def test_the_registry_holds_openai_anthropic_and_github_in_that_order():
    assert [service.name for service in SERVICES] == ["OpenAI", "Anthropic", "GitHub"]


def test_each_service_has_a_one_line_description_and_a_plain_text_create_page():
    for service in SERVICES:
        assert service.description and "\n" not in service.description, service.name
        assert service.create_page and "://" not in service.create_page, "the page is text, never a link fetched"


@pytest.mark.parametrize(("name", "host", "path", "header", "prefix"), PROVIDERS)
def test_each_check_points_at_its_provider_host_over_https_with_the_right_header(name, host, path, header, prefix):
    check = service_for(name).check

    parts = urllib.parse.urlsplit(check.url)
    assert parts.scheme == "https"
    assert parts.hostname == host
    assert parts.path == path
    assert (check.key_header, check.key_prefix) == (header, prefix)


def test_anthropic_also_sends_the_api_version_header():
    assert dict(ANTHROPIC.check.extra_headers) == {"anthropic-version": "2023-06-01"}


def test_service_for_matches_by_slug_and_leaves_other_names_alone():
    assert service_for("openai") is OPENAI
    assert service_for("OpenAI") is OPENAI
    assert service_for("GitHub") is GITHUB
    assert service_for("Mistral") is None
    assert service_for("RunPod") is None, "RunPod keeps its own panel, so it is not a registry service"


def test_the_form_offers_the_registry_services_and_other():
    assert SERVICE_CHOICES == ("OpenAI", "Anthropic", "GitHub", OTHER_CHOICE)


# ----------------------------------------------------------- the request


@pytest.mark.parametrize(("name", "host", "path", "header", "prefix"), PROVIDERS)
def test_a_check_sends_one_get_to_the_provider_host_with_the_key_in_its_header(name, host, path, header, prefix):
    secrets, _backend = _keychain(name)
    transport = FakeTransport(200)

    assert check_saved_key(service_for(name), name, secrets, transport=transport) == WORKS_TEXT

    assert len(transport.calls) == 1
    request, timeout = transport.calls[0]
    assert request.get_method() == "GET"
    assert urllib.parse.urlsplit(request.full_url).hostname == host, "the key goes to the provider host only"
    assert urllib.parse.urlsplit(request.full_url).scheme == "https"
    assert _header(request, header) == prefix + FAKE_KEY
    assert timeout == CHECK_TIMEOUT == 10.0


def test_the_anthropic_check_carries_the_version_header_next_to_the_key():
    secrets, _backend = _keychain("Anthropic")
    transport = FakeTransport(200)

    check_saved_key(ANTHROPIC, "Anthropic", secrets, transport=transport)

    request, _timeout = transport.calls[0]
    assert _header(request, "anthropic-version") == "2023-06-01"
    assert _header(request, "Authorization") is None, "Anthropic uses its own key header, not a bearer token"


# ----------------------------------------------------------- results


@pytest.mark.parametrize("status", [200, 204])
def test_a_2xx_reply_reports_works(status):
    secrets, _backend = _keychain("OpenAI")

    assert check_saved_key(OPENAI, "OpenAI", secrets, transport=FakeTransport(status)) == WORKS_TEXT


@pytest.mark.parametrize("status", [401, 403])
def test_a_401_or_403_reply_reports_rejected(status):
    secrets, _backend = _keychain("OpenAI")

    result = check_saved_key(OPENAI, "OpenAI", secrets, transport=FakeTransport(status))

    assert result == REJECTED_TEXT.format(service="OpenAI") == "Rejected by OpenAI. Check the key."


@pytest.mark.parametrize("status", [302, 404, 429, 500])
def test_any_other_status_reports_could_not_reach(status):
    secrets, _backend = _keychain("OpenAI")

    result = check_saved_key(OPENAI, "OpenAI", secrets, transport=FakeTransport(status))

    assert result == UNREACHABLE_TEXT.format(service="OpenAI") == "Could not reach OpenAI."


def test_a_timeout_reports_could_not_reach():
    secrets, _backend = _keychain("OpenAI")
    transport = FakeTransport(error=TimeoutError("timed out"))

    assert check_saved_key(OPENAI, "OpenAI", secrets, transport=transport) == "Could not reach OpenAI."


@pytest.mark.parametrize(
    "error",
    [urllib.error.URLError("connection refused"), ConnectionResetError("reset"), ValueError("bad response")],
)
def test_a_failure_to_connect_reports_could_not_reach(error):
    secrets, _backend = _keychain("GitHub")

    assert check_saved_key(GITHUB, "GitHub", secrets, transport=FakeTransport(error=error)) == "Could not reach GitHub."


def test_no_saved_key_reports_no_key_and_sends_nothing():
    secrets, _backend = _keychain()
    transport = FakeTransport(200)

    assert check_saved_key(OPENAI, "OpenAI", secrets, transport=transport) == NO_KEY_TEXT.format(name="OpenAI")
    assert NO_KEY_TEXT.format(name="OpenAI") == "No key is saved for OpenAI."
    assert transport.calls == []


def test_a_blank_saved_value_reports_no_key_and_sends_nothing():
    backend = MemoryKeyring()
    backend.set_password(KEY_SERVICE, account_for("OpenAI"), "   ")
    transport = FakeTransport(200)

    result = check_saved_key(OPENAI, "OpenAI", KeyringSecretStore(backend=backend), transport=transport)

    assert result == NO_KEY_TEXT.format(name="OpenAI")
    assert transport.calls == []


def test_a_keychain_that_cannot_be_read_reports_no_key_and_sends_nothing():
    secrets = KeyringSecretStore(backend=FailingKeyring(FAKE_KEY))
    transport = FakeTransport(200)

    result = check_saved_key(OPENAI, "OpenAI", secrets, transport=transport)

    assert result == NO_KEY_TEXT.format(name="OpenAI")
    assert transport.calls == []


def test_a_service_with_no_check_reports_no_check_and_reads_nothing():
    no_check = dataclasses.replace(OPENAI, check=None)
    secrets = KeyringSecretStore(backend=FailingKeyring(FAKE_KEY))  # a read would fail, so a read would show as no key
    transport = FakeTransport(200)

    assert check_saved_key(no_check, "OpenAI", secrets, transport=transport) == NO_CHECK_TEXT == (
        "No check yet for this service."
    )
    assert transport.calls == []


def test_a_check_is_refused_when_its_url_is_not_https_and_nothing_is_sent():
    secrets, _backend = _keychain("OpenAI")
    transport = FakeTransport(200)

    for url in ("http://api.openai.com/v1/models", "https:///v1/models"):
        service = _with_check(OPENAI, url=url)
        assert check_saved_key(service, "OpenAI", secrets, transport=transport) == UNREACHABLE_TEXT.format(
            service="OpenAI"
        )
    assert transport.calls == [], "the key never goes over plain http, or to a URL with no host"


def test_a_saved_value_that_cannot_go_in_a_header_is_refused_without_a_request():
    for bad in ("sk fake with a space", "sk-fäke"):
        backend = MemoryKeyring()
        backend.set_password(KEY_SERVICE, account_for("OpenAI"), bad)
        transport = FakeTransport(200)

        result = check_saved_key(OPENAI, "OpenAI", KeyringSecretStore(backend=backend), transport=transport)

        assert result == REJECTED_TEXT.format(service="OpenAI")
        assert transport.calls == []


def test_the_default_transport_sends_the_get_without_following_redirects_and_returns_the_status(monkeypatch):
    opener = _FakeOpener(200)
    monkeypatch.setattr(key_services.urllib.request, "build_opener", lambda *handlers: opener.with_handlers(handlers))
    secrets, _backend = _keychain("OpenAI")

    assert check_saved_key(OPENAI, "OpenAI", secrets) == WORKS_TEXT

    request, timeout = opener.requests[0]
    assert urllib.parse.urlsplit(request.full_url).hostname == "api.openai.com"
    assert timeout == CHECK_TIMEOUT
    assert any(isinstance(handler, key_services._NoRedirects) for handler in opener.handlers)


@pytest.mark.parametrize("outcome", [401, 403])
def test_the_default_transport_reads_a_refused_status_from_the_error(monkeypatch, outcome):
    error = urllib.error.HTTPError("https://api.openai.com/v1/models", outcome, "refused", {}, io.BytesIO(b""))
    monkeypatch.setattr(key_services.urllib.request, "build_opener", lambda *handlers: _FakeOpener(error))
    secrets, _backend = _keychain("OpenAI")

    assert check_saved_key(OPENAI, "OpenAI", secrets) == REJECTED_TEXT.format(service="OpenAI")


def test_the_default_transport_reports_a_timeout_as_could_not_reach(monkeypatch):
    timed_out = urllib.error.URLError(TimeoutError("timed out"))
    monkeypatch.setattr(key_services.urllib.request, "build_opener", lambda *handlers: _FakeOpener(timed_out))
    secrets, _backend = _keychain("OpenAI")

    assert check_saved_key(OPENAI, "OpenAI", secrets) == "Could not reach OpenAI."


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeOpener:
    """Stands in for urllib's opener. It answers with a status or raises the error it was given, and keeps requests."""

    def __init__(self, outcome, handlers=()) -> None:
        self.outcome = outcome
        self.handlers = tuple(handlers)
        self.requests: list[tuple[urllib.request.Request, float]] = []

    def with_handlers(self, handlers):
        self.handlers = tuple(handlers)
        return self

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return _FakeResponse(self.outcome)


# ----------------------------------------------------------- no key in any output


def _outputs(secrets, transport) -> list[str]:
    """Every string a check hands back, for each kind of outcome. The key must be in none of them."""
    texts = []
    for name in ("OpenAI", "Anthropic", "GitHub"):
        service = service_for(name)
        for make in (
            lambda: FakeTransport(200),
            lambda: FakeTransport(401),
            lambda: FakeTransport(error=TimeoutError("timed out")),
            lambda: FakeTransport(error=ValueError(f"Invalid header value {FAKE_KEY}")),
            lambda: FakeTransport(error=OSError(f"send failed for {FAKE_KEY}")),
        ):
            texts.append(check_saved_key(service, name, secrets, transport=make()))
    unreadable = KeyringSecretStore(backend=FailingKeyring(FAKE_KEY))
    texts.append(check_saved_key(OPENAI, "OpenAI", unreadable, transport=transport))
    texts.append(check_saved_key(OPENAI, "OpenAI", _keychain()[0], transport=transport))
    return texts


def test_no_check_output_holds_the_key_text_on_any_path():
    secrets, _backend = _keychain("OpenAI", "Anthropic", "GitHub")

    texts = _outputs(secrets, FakeTransport(200))

    assert len(texts) == 17
    assert all(FAKE_KEY not in text for text in texts)
    assert all(text.endswith(".") for text in texts), "every output is one of the fixed messages"


# ----------------------------------------------------------- the Keys block: service cards and Check


def _all_text(widget) -> str:
    """Every label, button and placeholder on the widget. The leak checks search this."""
    parts = [label.text() for label in widget.findChildren(QLabel)]
    parts += [button.text() for button in widget.findChildren(QPushButton)]
    parts += [line.placeholderText() for line in widget.findChildren(QLineEdit)]
    return "\n".join(parts)


def _section(qtbot, settings_path, backend, transport=None, names=()) -> KeysSection:
    """A Keys block over a MemoryKeyring, with the given names and transport. Its first read is awaited."""
    if names:
        SettingsStore(settings_path).save(Settings(key_names=tuple(names)))
    keys = ApiKeys(SettingsStore(settings_path), KeyringSecretStore(backend=backend))
    section = KeysSection(keys, transport=transport)
    qtbot.addWidget(section)
    qtbot.waitUntil(lambda: not section.loading, timeout=5000)
    return section


def _saved_openai(qtbot, settings_path, transport):
    """A Keys block that lists OpenAI with FAKE_KEY saved. Returns the backend and the block."""
    backend = MemoryKeyring()
    backend.set_password(KEY_SERVICE, account_for("OpenAI"), FAKE_KEY)
    return backend, _section(qtbot, settings_path, backend, transport=transport, names=("OpenAI",))


def _pick(combo, text: str) -> None:
    """The owner picks ``text`` in the list. activated is what a real pick emits."""
    index = combo.findText(text)
    combo.setCurrentIndex(index)
    combo.activated.emit(index)


def _press_check(section: KeysSection) -> None:
    section.findChild(QPushButton, "keys-entry-openai-check").click()


def test_the_form_lists_the_services_with_other_chosen_by_default(qtbot, settings_path):
    section = _section(qtbot, settings_path, MemoryKeyring())

    combo = section.findChild(QComboBox, "keys-new-service")
    assert [combo.itemText(index) for index in range(combo.count())] == list(SERVICE_CHOICES)
    assert combo.currentText() == OTHER_CHOICE


def test_picking_a_service_fills_the_name_and_other_keeps_the_name_as_typed(qtbot, settings_path):
    section = _section(qtbot, settings_path, MemoryKeyring())
    combo = section.findChild(QComboBox, "keys-new-service")
    name = section.findChild(QLineEdit, "keys-new-name")

    _pick(combo, "GitHub")
    assert name.text() == "GitHub"

    name.setText("Work token")
    _pick(combo, OTHER_CHOICE)
    assert name.text() == "Work token", "Other leaves the free name alone"


def test_an_openai_key_added_from_the_form_shows_what_it_is_for_and_a_check(qtbot, settings_path):
    backend = MemoryKeyring()
    section = _section(qtbot, settings_path, backend)
    _pick(section.findChild(QComboBox, "keys-new-service"), "OpenAI")
    section.findChild(QLineEdit, "keys-new-value").setText(FAKE_KEY)
    section.findChild(QPushButton, "keys-add").click()

    assert section.findChild(QLabel, "keys-entry-openai-description").text() == OPENAI.description
    assert section.findChild(QLabel, "keys-entry-openai-create").text() == CREATE_TEXT.format(page=OPENAI.create_page)
    assert section.findChild(QPushButton, "keys-entry-openai-check").text() == CHECK_TEXT == "Check"
    assert section.findChild(QComboBox, "keys-new-service").currentText() == OTHER_CHOICE, "the form resets its service"
    assert FAKE_KEY not in _all_text(section)


def test_a_saved_name_that_is_no_service_keeps_the_plain_row(qtbot, settings_path):
    section = _section(qtbot, settings_path, MemoryKeyring(), names=("Mistral",))

    assert section.findChild(QLabel, "keys-entry-mistral-status") is not None
    assert section.findChild(QLabel, "keys-entry-mistral-description") is None
    assert section.findChild(QPushButton, "keys-entry-mistral-check") is None


def test_check_reports_works_with_one_get_to_openai_and_never_shows_the_key(qtbot, settings_path):
    transport = FakeTransport(200)
    _backend, section = _saved_openai(qtbot, settings_path, transport)
    button = section.findChild(QPushButton, "keys-entry-openai-check")
    result = section.findChild(QLabel, "keys-entry-openai-check-result")

    button.click()
    qtbot.waitUntil(lambda: result.text() == WORKS_TEXT, timeout=5000)

    assert len(transport.calls) == 1
    request, timeout = transport.calls[0]
    assert urllib.parse.urlsplit(request.full_url).hostname == "api.openai.com"
    assert _header(request, "Authorization") == "Bearer " + FAKE_KEY
    assert timeout == 10.0
    assert button.isEnabled()
    assert FAKE_KEY not in _all_text(section)


@pytest.mark.parametrize("status", [401, 403])
def test_check_reports_a_refused_key_as_rejected(qtbot, settings_path, status):
    _backend, section = _saved_openai(qtbot, settings_path, FakeTransport(status))
    result = section.findChild(QLabel, "keys-entry-openai-check-result")

    _press_check(section)
    qtbot.waitUntil(lambda: result.text() == REJECTED_TEXT.format(service="OpenAI"), timeout=5000)

    assert result.text() == "Rejected by OpenAI. Check the key."


def test_check_reports_a_timeout_as_could_not_reach(qtbot, settings_path):
    _backend, section = _saved_openai(qtbot, settings_path, FakeTransport(error=TimeoutError("timed out")))
    result = section.findChild(QLabel, "keys-entry-openai-check-result")

    _press_check(section)
    qtbot.waitUntil(lambda: result.text() == "Could not reach OpenAI.", timeout=5000)


def test_a_check_in_progress_disables_its_button_and_a_second_press_sends_nothing(qtbot, settings_path):
    gate = threading.Event()
    transport = GatedTransport(gate, 200)
    _backend, section = _saved_openai(qtbot, settings_path, transport)
    button = section.findChild(QPushButton, "keys-entry-openai-check")
    result = section.findChild(QLabel, "keys-entry-openai-check-result")
    try:
        _press_check(section)
        qtbot.waitUntil(lambda: len(transport.calls) == 1, timeout=5000)

        assert not button.isEnabled()
        assert result.text() == CHECKING_TEXT.format(service="OpenAI")
        _press_check(section)  # the button is off, so the press does nothing
        button.setEnabled(True)  # even if it were on, a check already running for the card is not started again
        _press_check(section)
        assert len(transport.calls) == 1
    finally:
        gate.set()
    qtbot.waitUntil(lambda: result.text() == WORKS_TEXT, timeout=5000)
    assert button.isEnabled()
    assert len(transport.calls) == 1
    assert QThreadPool.globalInstance().waitForDone(5000)


def test_a_saved_key_removed_outside_the_block_reports_no_key_and_sends_nothing(qtbot, settings_path):
    transport = FakeTransport(200)
    backend, section = _saved_openai(qtbot, settings_path, transport)
    backend.delete_password(KEY_SERVICE, account_for("OpenAI"))
    result = section.findChild(QLabel, "keys-entry-openai-check-result")

    _press_check(section)
    qtbot.waitUntil(lambda: result.text() == NO_KEY_TEXT.format(name="OpenAI"), timeout=5000)

    assert result.text() == "No key is saved for OpenAI."
    assert transport.calls == []


def test_an_error_that_names_the_key_is_never_shown_on_the_card(qtbot, settings_path):
    transport = FakeTransport(error=ValueError(f"Invalid header value {FAKE_KEY}"))
    _backend, section = _saved_openai(qtbot, settings_path, transport)
    result = section.findChild(QLabel, "keys-entry-openai-check-result")

    _press_check(section)
    qtbot.waitUntil(lambda: result.text() == "Could not reach OpenAI.", timeout=5000)

    assert FAKE_KEY not in result.text()
    assert FAKE_KEY not in _all_text(section)


def test_a_service_with_no_check_shows_no_check_text_and_no_button(qtbot, settings_path, monkeypatch):
    no_check = (dataclasses.replace(OPENAI, check=None), ANTHROPIC, GITHUB)
    monkeypatch.setattr(key_services, "SERVICES", no_check)
    _backend, section = _saved_openai(qtbot, settings_path, FakeTransport(200))

    assert section.findChild(QLabel, "keys-entry-openai-check-result").text() == NO_CHECK_TEXT
    assert section.findChild(QPushButton, "keys-entry-openai-check") is None
    assert section.findChild(QLabel, "keys-entry-openai-description").text() == OPENAI.description


def test_a_result_for_a_card_removed_while_its_check_runs_is_dropped(qtbot, settings_path):
    gate = threading.Event()
    transport = GatedTransport(gate, 200)
    _backend, section = _saved_openai(qtbot, settings_path, transport)
    try:
        _press_check(section)
        qtbot.waitUntil(lambda: len(transport.calls) == 1, timeout=5000)
        SettingsStore(settings_path).save(Settings(key_names=()))
        section.refresh()
        # The card is removed when the event loop runs its deferred delete, so wait for that.
        qtbot.waitUntil(lambda: section.findChild(QWidget, "keys-entry-openai") is None, timeout=5000)
    finally:
        gate.set()
    assert QThreadPool.globalInstance().waitForDone(5000)
    qtbot.wait(20)

    assert section.findChild(QWidget, "keys-entry-openai") is None, "the late result does not bring the card back"
    assert section.findChild(QLabel, "keys-entry-openai-check-result") is None


def test_a_check_whose_worker_raises_shows_could_not_reach_and_names_no_value(qtbot, settings_path, monkeypatch):
    def broken_check(service, name, store, *, transport=None):
        raise RuntimeError(f"the worker failed while holding {FAKE_KEY}")

    monkeypatch.setattr(keys_module, "check_saved_key", broken_check)
    _backend, section = _saved_openai(qtbot, settings_path, FakeTransport(200))
    button = section.findChild(QPushButton, "keys-entry-openai-check")
    result = section.findChild(QLabel, "keys-entry-openai-check-result")

    _press_check(section)
    qtbot.waitUntil(lambda: result.text() == "Could not reach OpenAI.", timeout=5000)

    assert button.isEnabled()
    assert FAKE_KEY not in _all_text(section)


def test_saving_a_new_value_drops_an_old_check_result_and_enables_the_check(qtbot, settings_path):
    _backend, section = _saved_openai(qtbot, settings_path, FakeTransport(200))
    button = section.findChild(QPushButton, "keys-entry-openai-check")
    result = section.findChild(QLabel, "keys-entry-openai-check-result")
    _press_check(section)
    qtbot.waitUntil(lambda: result.text() == WORKS_TEXT, timeout=5000)

    section.findChild(QLineEdit, "keys-entry-openai-value").setText(FAKE_KEY + "-two")
    section.findChild(QPushButton, "keys-entry-openai-save").click()

    assert result.text() == ""
    assert button.isEnabled()
