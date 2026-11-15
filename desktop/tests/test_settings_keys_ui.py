"""Tests for the Keys tab and the Settings tab's groups, with pytest-qt. Offscreen Qt, from conftest.

The keys are a tab of their own (ui/keys_tab.py), so the Keys tests build that tab. The Settings tests build the Settings
tab and check that it holds no keys. The keychain is a MemoryKeyring, the RunPod client never answers, and every removal
confirmation is an injected fake, so nothing touches the real credential store, the network or a dialog. FAKE_VALUE is an
obviously fake value, so the leak checks can search for it.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QPoint, QThreadPool
from PySide6.QtWidgets import QAbstractButton, QFrame, QLabel, QLineEdit, QPushButton, QToolButton, QWidget

from _fakes import MemoryKeyring
from meridian_desktop import local_client
from meridian_desktop.api_keys import KEY_SERVICE, STATUS_NOT_SET, STATUS_SET, ApiKeys
from meridian_desktop.secret_store import RUNPOD_ACCOUNT, RUNPOD_SERVICE, KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop.ui import keys_section as keys_module
from meridian_desktop.ui.client_setup_section import ClientSetupSection
from meridian_desktop.ui.keys_section import (
    ADDED_TEXT,
    NOT_REMOVED_OS_TEXT,
    SEARCH_EMPTY_TEXT,
    STATUS_TEXT,
    KeysSection,
)
from meridian_desktop.ui.keys_tab import KeysTab
from meridian_desktop.ui.runpod_panel import (
    KEY_MISSING_TEXT,
    KEY_STORED_TEXT,
    REMOVE_KEY_CANCELLED_TEXT,
    REMOVE_KEY_TITLE,
    RunPodPanel,
)
from meridian_desktop.ui.settings_group import CollapsibleGroup
from meridian_desktop.ui.settings_tab import SettingsTab

FAKE_VALUE = "sk-fake-keys-ui-0001"
FAKE_VALUE_TWO = "sk-fake-keys-ui-0002"
# The Settings groups in order. The keys are not among them: they are a tab of their own.
GROUP_ORDER = (
    "settings-group-connection",
    "settings-group-local-server",
    "settings-group-client-setup",
    "settings-group-tools",
    "settings-group-connections",
    "settings-group-gpu-hosts",
    "settings-group-limits",
    "settings-group-diagnostics",
)
GROUP_TITLES = (
    "Connection",
    "Local server and startup",
    "Client setup",
    "MCP tools",
    "Connections",
    "GPU hosts over SSH",
    "Limits",
    "Diagnostics (logs)",
)
TAB_WIDTH = 1280
TAB_HEIGHT = 3200


class FakeClient:
    """A RunPod client that never answers. The probe reads 'not running', so no request is sent."""

    def probe_health(self, url, timeout):
        return local_client.HEALTH_NOT_RUNNING


def _unexpected_list(url, token):
    raise AssertionError("the hosted list must not be called in these tests")


def _no_dialog(parent, start_dir):
    raise AssertionError("the real file dialog must not open in tests")


def _tab(qtbot, settings_path, secrets, **kwargs):
    tab = SettingsTab(
        SettingsStore(settings_path),
        choose_database=_no_dialog,
        secrets=secrets,
        list_hosted=_unexpected_list,
        **kwargs,
    )
    qtbot.addWidget(tab)
    return tab


def _say_yes(parent, title, text):
    return True


def _keys_tab(qtbot, settings_path, secrets, confirm=None):
    """The Keys tab as the main window builds it, with the RunPod client faked so no request is sent.

    The RunPod Remove question is answered Yes unless a test passes its own, so no dialog can open. The named keys keep
    their own questions, which a test patches where it needs them.
    """
    answer = _say_yes if confirm is None else confirm
    tab = KeysTab(SettingsStore(settings_path), secrets, client=FakeClient(), confirm=answer)
    qtbot.addWidget(tab)
    return tab


def _panel(tab) -> RunPodPanel:
    """The RunPod panel that owns the card's jobs. It is hidden and has no rows of its own."""
    panel = tab.findChild(RunPodPanel, "runpod-panel")
    assert panel is not None
    return panel


def _wait_names_loaded(qtbot, section) -> None:
    """The Keys block reads the saved names on the thread pool, so wait for that read before looking at its cards."""
    qtbot.waitUntil(lambda: not section.loading, timeout=5000)


class GatedKeys:
    """Wraps the real ApiKeys. Its first entries() call reads the list, then waits on the gate, so a test can change the
    saved names while that first read is still held. It records each thread that calls it."""

    def __init__(self, inner, gate):
        self._inner = inner
        self._gate = gate
        self.threads = []
        self.first_read_done = threading.Event()

    def entries(self):
        first = not self.threads
        self.threads.append(threading.current_thread())
        result = self._inner.entries()
        if first:
            self.first_read_done.set()
            self._gate.wait(5)
        return result

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _all_text(widget) -> str:
    """Every label, button and placeholder on the widget. A leak check searches this.

    An entry box's own text is left out: the owner's typed value sits in a password box, drawn as dots, until a save clears it.
    """
    parts = [label.text() for label in widget.findChildren(QLabel)]
    parts += [button.text() for button in widget.findChildren(QPushButton)]
    parts += [line.placeholderText() for line in widget.findChildren(QLineEdit)]
    return "\n".join(parts)


def _group_names(tab) -> list[str]:
    content = tab.findChild(QWidget, "settings-content")
    layout = content.layout()
    widgets = [layout.itemAt(index).widget() for index in range(layout.count())]
    return [widget.objectName() for widget in widgets if widget is not None]


def _seed(settings_path, backend, *, names=("OpenAI", "Anthropic"), value_for=None):
    SettingsStore(settings_path).save(Settings(key_names=tuple(names)))
    if value_for is not None:
        backend.set_password(KEY_SERVICE, "key-" + value_for.lower(), FAKE_VALUE)


# ----------------------------------------------------------- Settings groups


def test_the_eight_settings_groups_are_in_the_requested_order(qtbot, settings_path):
    tab = _tab(qtbot, settings_path, KeyringSecretStore(backend=MemoryKeyring()))

    assert _group_names(tab) == list(GROUP_ORDER)
    titles = [tab.findChild(QToolButton, f"{name}-toggle").text() for name in GROUP_ORDER]
    assert tuple(titles) == GROUP_TITLES


def test_settings_holds_no_keys_group_and_no_key_card(qtbot, settings_path):
    tab = _tab(qtbot, settings_path, KeyringSecretStore(backend=MemoryKeyring()))

    assert tab.findChild(CollapsibleGroup, "settings-group-keys") is None, "the keys are a tab of their own"
    assert tab.findChild(KeysSection, "keys-section") is None
    assert tab.findChild(QWidget, "runpod-entry-card") is None


def test_the_first_two_groups_start_open_and_the_rest_start_folded(qtbot, settings_path):
    tab = _tab(qtbot, settings_path, KeyringSecretStore(backend=MemoryKeyring()))

    for name in GROUP_ORDER[:2]:
        assert tab.findChild(CollapsibleGroup, name).is_expanded() is True, name
    for name in GROUP_ORDER[2:]:
        assert tab.findChild(CollapsibleGroup, name).is_expanded() is False, name


def test_a_group_folds_and_unfolds_and_keeps_its_widgets(qtbot, settings_path):
    tab = _tab(qtbot, settings_path, KeyringSecretStore(backend=MemoryKeyring()))
    group = tab.findChild(CollapsibleGroup, "settings-group-client-setup")
    toggle = tab.findChild(QAbstractButton, "settings-group-client-setup-toggle")

    # The Client setup group starts folded, so the first press opens it.
    toggle.click()
    assert group.is_expanded() is True
    section = tab.findChild(ClientSetupSection)

    toggle.click()
    assert group.is_expanded() is False
    assert tab.findChild(ClientSetupSection) is section, "folding keeps the widgets"

    toggle.click()
    assert group.is_expanded() is True


def test_the_content_column_never_narrows_below_its_minimum_width(qtbot, settings_path):
    tab = _tab(qtbot, settings_path, KeyringSecretStore(backend=MemoryKeyring()))

    assert tab.findChild(QWidget, "settings-content").minimumWidth() >= 600


# ----------------------------------------------------------- Keys tab: RunPod card


def test_the_runpod_card_sits_on_the_keys_tab_and_its_panel_keeps_no_rows(qtbot, settings_path):
    secrets = KeyringSecretStore(backend=MemoryKeyring())
    tab = _keys_tab(qtbot, settings_path, secrets)

    card = tab.findChild(QWidget, "runpod-entry-card")
    assert card is not None
    assert tab.findChild(KeysSection, "keys-section").isAncestorOf(card), "the Keys section places the card"
    assert _panel(tab).findChild(QWidget, "runpod-entry-card") is None, "the panel gave its card away"
    assert _panel(tab).isHidden(), "the panel is never shown, its card is"


def test_the_runpod_entry_still_saves_and_removes_its_key(qtbot, settings_path):
    backend = MemoryKeyring()
    secrets = KeyringSecretStore(backend=backend)
    asked = []

    def ask(parent, title, text):
        asked.append(title)
        return True

    tab = _keys_tab(qtbot, settings_path, secrets, confirm=ask)
    panel = _panel(tab)
    key_input = tab.findChild(QLineEdit, "runpod-key-input")
    status = tab.findChild(QLabel, "runpod-key-status")
    # The panel's first keychain read holds its buttons until it answers, so wait for it before the first click.
    qtbot.waitUntil(lambda: not panel.busy, timeout=5000)

    key_input.setText(FAKE_VALUE)
    tab.findChild(QPushButton, "runpod-save-key").click()
    qtbot.waitUntil(lambda: status.text() == KEY_STORED_TEXT, timeout=5000)

    assert backend.snapshot() == {(RUNPOD_SERVICE, RUNPOD_ACCOUNT): FAKE_VALUE}
    assert key_input.text() == ""
    assert FAKE_VALUE not in _all_text(tab)

    qtbot.waitUntil(lambda: not panel.busy, timeout=5000)
    tab.findChild(QPushButton, "runpod-remove-key").click()
    qtbot.waitUntil(lambda: status.text() == KEY_MISSING_TEXT, timeout=5000)
    assert asked == [REMOVE_KEY_TITLE], "the RunPod Remove asks first"
    assert backend.snapshot() == {}


def test_declining_the_runpod_remove_keeps_the_key_in_the_keychain(qtbot, settings_path):
    backend = MemoryKeyring()
    backend.set_password(RUNPOD_SERVICE, RUNPOD_ACCOUNT, FAKE_VALUE)
    secrets = KeyringSecretStore(backend=backend)
    tab = _keys_tab(qtbot, settings_path, secrets, confirm=lambda parent, title, text: False)
    status = tab.findChild(QLabel, "runpod-key-status")
    qtbot.waitUntil(lambda: status.text() == KEY_STORED_TEXT, timeout=5000)

    tab.findChild(QPushButton, "runpod-remove-key").click()

    assert backend.snapshot() == {(RUNPOD_SERVICE, RUNPOD_ACCOUNT): FAKE_VALUE}
    assert tab.findChild(QLabel, "runpod-result").text() == REMOVE_KEY_CANCELLED_TEXT
    assert status.text() == KEY_STORED_TEXT


def test_the_keys_block_lists_runpod_once_as_a_named_card(qtbot, settings_path):
    secrets = KeyringSecretStore(backend=MemoryKeyring())
    tab = _keys_tab(qtbot, settings_path, secrets)

    heading = tab.findChild(QLabel, "keys-runpod-name")
    assert heading is not None and heading.text() == "RunPod"
    assert tab.findChild(QWidget, "keys-entry-runpod") is not None


# ----------------------------------------------------------- Keys tab: named keys


def test_a_named_key_is_added_and_shows_set_without_its_value(qtbot, settings_path):
    backend = MemoryKeyring()
    tab = _keys_tab(qtbot, settings_path, KeyringSecretStore(backend=backend))

    tab.findChild(QLineEdit, "keys-new-name").setText("OpenAI")
    tab.findChild(QLineEdit, "keys-new-value").setText(FAKE_VALUE)
    tab.findChild(QPushButton, "keys-add").click()

    assert backend.snapshot() == {(KEY_SERVICE, "key-openai"): FAKE_VALUE}
    assert tab.findChild(QLabel, "keys-entry-openai-name").text() == "OpenAI"
    assert tab.findChild(QLabel, "keys-entry-openai-status").text() == STATUS_TEXT[STATUS_SET]
    assert tab.findChild(QLineEdit, "keys-new-value").text() == ""
    assert tab.findChild(QLabel, "keys-message").text() == ADDED_TEXT.format(name="OpenAI")
    assert SettingsStore(settings_path).load().settings.key_names == ("OpenAI",)
    assert FAKE_VALUE not in _all_text(tab)


def test_a_refused_name_is_shown_inline_and_nothing_is_written(qtbot, settings_path):
    backend = MemoryKeyring()
    tab = _keys_tab(qtbot, settings_path, KeyringSecretStore(backend=backend))

    tab.findChild(QLineEdit, "keys-new-name").setText("bad/name")
    tab.findChild(QLineEdit, "keys-new-value").setText(FAKE_VALUE)
    tab.findChild(QPushButton, "keys-add").click()

    message = tab.findChild(QLabel, "keys-message")
    assert not message.isHidden(), "the refusal is shown"
    assert "letters, digits, spaces" in message.text()
    assert backend.snapshot() == {}
    assert not settings_path.exists()
    assert FAKE_VALUE not in _all_text(tab)


def test_saved_names_show_their_status_from_the_keychain(qtbot, settings_path):
    backend = MemoryKeyring()
    _seed(settings_path, backend, names=("OpenAI", "Anthropic"), value_for="OpenAI")

    tab = _keys_tab(qtbot, settings_path, KeyringSecretStore(backend=backend))
    _wait_names_loaded(qtbot, tab.findChild(KeysSection, "keys-section"))

    assert tab.findChild(QLabel, "keys-entry-openai-status").text() == STATUS_TEXT[STATUS_SET]
    assert tab.findChild(QLabel, "keys-entry-anthropic-status").text() == STATUS_TEXT[STATUS_NOT_SET]
    assert FAKE_VALUE not in _all_text(tab)


def test_replace_changes_only_the_value_and_remove_asks_first(qtbot, settings_path, monkeypatch):
    backend = MemoryKeyring()
    tab = _keys_tab(qtbot, settings_path, KeyringSecretStore(backend=backend))
    tab.findChild(QLineEdit, "keys-new-name").setText("OpenAI")
    tab.findChild(QLineEdit, "keys-new-value").setText(FAKE_VALUE)
    tab.findChild(QPushButton, "keys-add").click()

    # Replace: the box is cleared, the listing stays, and only the value changes.
    tab.findChild(QLineEdit, "keys-entry-openai-value").setText(FAKE_VALUE_TWO)
    tab.findChild(QPushButton, "keys-entry-openai-save").click()
    assert backend.snapshot() == {(KEY_SERVICE, "key-openai"): FAKE_VALUE_TWO}
    assert tab.findChild(QLineEdit, "keys-entry-openai-value").text() == ""
    assert tab.findChild(QLabel, "keys-entry-openai-status").text() == STATUS_TEXT[STATUS_SET]

    # Remove, declined: the value and the name are kept.
    asked = []
    monkeypatch.setattr(keys_module, "_ask_remove", lambda parent, title, text: asked.append(text) or False)
    tab.findChild(QPushButton, "keys-entry-openai-remove").click()
    assert len(asked) == 1 and "OpenAI" in asked[0], "the owner is asked first, and the name is in the question"
    assert backend.snapshot() == {(KEY_SERVICE, "key-openai"): FAKE_VALUE_TWO}
    assert tab.findChild(QLabel, "keys-message").text() == "Cancelled. Nothing was removed."

    # Remove, confirmed: the value goes, the name leaves the list, and its card is gone.
    monkeypatch.setattr(keys_module, "_ask_remove", lambda parent, title, text: True)
    tab.findChild(QPushButton, "keys-entry-openai-remove").click()
    assert backend.snapshot() == {}
    # The card is removed when the event loop runs its deferred delete, so wait for that.
    qtbot.waitUntil(lambda: tab.findChild(QWidget, "keys-entry-openai") is None, timeout=5000)
    assert SettingsStore(settings_path).load().settings.key_names == ()


def test_a_settings_write_that_fails_after_the_keychain_delete_shows_the_keychain_state(qtbot, settings_path, monkeypatch):
    backend = MemoryKeyring()
    tab = _keys_tab(qtbot, settings_path, KeyringSecretStore(backend=backend))
    tab.findChild(QLineEdit, "keys-new-name").setText("OpenAI")
    tab.findChild(QLineEdit, "keys-new-value").setText(FAKE_VALUE)
    tab.findChild(QPushButton, "keys-add").click()

    # The keychain value is deleted first, then the names list is written. A refused write must not leave the card
    # saying a value is set when the keychain no longer holds one.
    def refuse_save(self, settings):
        raise PermissionError("the settings file is locked")

    monkeypatch.setattr(keys_module, "_ask_remove", lambda parent, title, text: True)
    monkeypatch.setattr(SettingsStore, "save", refuse_save)
    tab.findChild(QPushButton, "keys-entry-openai-remove").click()

    assert backend.snapshot() == {}
    assert tab.findChild(QLabel, "keys-entry-openai-status").text() == STATUS_TEXT[STATUS_NOT_SET]
    assert tab.findChild(QLabel, "keys-message").text() == NOT_REMOVED_OS_TEXT
    assert FAKE_VALUE not in _all_text(tab)


def test_no_value_is_ever_shown_on_the_tab_after_a_save_or_a_refresh(qtbot, settings_path):
    backend = MemoryKeyring()
    _seed(settings_path, backend, names=("OpenAI",), value_for="OpenAI")
    tab = _keys_tab(qtbot, settings_path, KeyringSecretStore(backend=backend))

    assert FAKE_VALUE not in _all_text(tab)
    tab.findChild(KeysSection, "keys-section").refresh()
    assert FAKE_VALUE not in _all_text(tab)
    if settings_path.exists():
        assert FAKE_VALUE.encode() not in settings_path.read_bytes(), "the settings file holds names, never values"


def test_the_keys_section_on_its_own_has_no_runpod_card_and_lists_custom_names(qtbot, settings_path):
    backend = MemoryKeyring()
    _seed(settings_path, backend, names=("OpenAI",), value_for="OpenAI")
    keys = ApiKeys(SettingsStore(settings_path), KeyringSecretStore(backend=backend))
    section = KeysSection(keys)
    qtbot.addWidget(section)
    _wait_names_loaded(qtbot, section)

    assert section.findChild(QWidget, "keys-entry-runpod") is None, "no RunPod card without a panel to host"
    assert section.findChild(QLabel, "keys-entry-openai-status").text() == STATUS_TEXT[STATUS_SET]


def test_the_names_are_read_on_the_thread_pool_so_building_the_block_does_not_wait_for_the_keychain(qtbot, settings_path):
    backend = MemoryKeyring()
    _seed(settings_path, backend, names=("OpenAI",), value_for="OpenAI")
    gate = threading.Event()
    keys = GatedKeys(ApiKeys(SettingsStore(settings_path), KeyringSecretStore(backend=backend)), gate)

    section = KeysSection(keys)
    qtbot.addWidget(section)

    # The first read is held at the gate, and the block was still built: construction did not wait for it.
    assert section.loading is True
    assert section.findChild(QLabel, "keys-entry-openai-status") is None
    gate.set()
    _wait_names_loaded(qtbot, section)
    assert section.findChild(QLabel, "keys-entry-openai-status").text() == STATUS_TEXT[STATUS_SET]
    assert keys.threads and all(thread is not threading.main_thread() for thread in keys.threads)


def test_a_read_that_a_later_refresh_supersedes_cannot_bring_back_a_name_the_refresh_dropped(qtbot, settings_path):
    backend = MemoryKeyring()
    _seed(settings_path, backend, names=("OpenAI",), value_for="OpenAI")
    gate = threading.Event()
    keys = GatedKeys(ApiKeys(SettingsStore(settings_path), KeyringSecretStore(backend=backend)), gate)
    section = KeysSection(keys)
    qtbot.addWidget(section)
    assert keys.first_read_done.wait(5), "the first read has listed the old name"

    # The name leaves the list while that read is still held. The refresh shows the list as it is now.
    SettingsStore(settings_path).save(Settings(key_names=()))
    section.refresh()
    assert section.findChild(QWidget, "keys-entry-openai") is None

    # The held read now delivers the old list. Its generation is out of date, so it is dropped.
    gate.set()
    assert QThreadPool.globalInstance().waitForDone(5000)
    qtbot.wait(20)
    assert section.findChild(QWidget, "keys-entry-openai") is None
    assert section.loading is False


# ----------------------------------------------------------- search


def _search_box(tab) -> QLineEdit:
    return tab.findChild(QLineEdit, "keys-search")


def _search_empty(tab) -> QLabel:
    return tab.findChild(QLabel, "keys-search-empty")


def _frame(tab, key: str) -> QFrame:
    return tab.findChild(QFrame, f"keys-entry-{key}")


def _shown(tab, key: str) -> bool:
    return not _frame(tab, key).isHidden()


def test_the_search_box_sits_above_the_key_cards_and_says_what_it_is_for(qtbot, settings_path):
    backend = MemoryKeyring()
    _seed(settings_path, backend, names=("OpenAI",), value_for="OpenAI")
    tab = _keys_tab(qtbot, settings_path, KeyringSecretStore(backend=backend))
    _show_keys_at_1280(qtbot, tab)

    search = _search_box(tab)
    assert search.placeholderText() == "Search keys"
    assert search.text() == ""
    assert _search_empty(tab).isHidden()

    def top(widget):
        return widget.mapTo(tab, QPoint(0, 0)).y()

    assert top(search) + search.height() <= top(_frame(tab, "runpod")), "the search is above the RunPod card"
    assert top(search) + search.height() <= top(_frame(tab, "openai")), "the search is above the saved cards"


def test_typing_filters_the_names_as_the_owner_types_and_ignores_case(qtbot, settings_path):
    backend = MemoryKeyring()
    _seed(settings_path, backend, names=("OpenAI", "Anthropic", "Mistral"), value_for="OpenAI")
    tab = _keys_tab(qtbot, settings_path, KeyringSecretStore(backend=backend))
    _wait_names_loaded(qtbot, tab.findChild(KeysSection, "keys-section"))
    search = _search_box(tab)
    assert _shown(tab, "openai") and _shown(tab, "anthropic") and _shown(tab, "mistral")

    qtbot.keyClicks(search, "OPEN")
    assert _shown(tab, "openai")
    assert not _shown(tab, "anthropic") and not _shown(tab, "mistral")

    search.clear()
    qtbot.keyClicks(search, "an")
    assert _shown(tab, "anthropic")
    assert not _shown(tab, "openai") and not _shown(tab, "mistral")

    search.clear()
    assert _shown(tab, "openai") and _shown(tab, "anthropic") and _shown(tab, "mistral")
    assert _search_empty(tab).isHidden()


def test_the_search_matches_names_only_never_a_value_or_a_status(qtbot, settings_path):
    backend = MemoryKeyring()
    _seed(settings_path, backend, names=("OpenAI", "Anthropic"), value_for="OpenAI")
    tab = _keys_tab(qtbot, settings_path, KeyringSecretStore(backend=backend))
    _wait_names_loaded(qtbot, tab.findChild(KeysSection, "keys-section"))
    search = _search_box(tab)

    # Part of the saved value matches no name, so the cards stay hidden.
    qtbot.keyClicks(search, "sk-fake")
    assert not _shown(tab, "openai") and not _shown(tab, "anthropic")
    assert not _search_empty(tab).isHidden()

    # Status wording, "A value is set.", is not searched either.
    search.clear()
    qtbot.keyClicks(search, "is set")
    assert not _shown(tab, "openai") and not _shown(tab, "anthropic")
    assert FAKE_VALUE not in _all_text(tab)


def test_the_empty_state_shows_when_nothing_matches_and_goes_when_the_search_clears(qtbot, settings_path):
    backend = MemoryKeyring()
    _seed(settings_path, backend, names=("OpenAI", "Anthropic"))
    tab = _keys_tab(qtbot, settings_path, KeyringSecretStore(backend=backend))
    _wait_names_loaded(qtbot, tab.findChild(KeysSection, "keys-section"))
    search = _search_box(tab)

    qtbot.keyClicks(search, "zebra")
    empty = _search_empty(tab)
    assert not empty.isHidden()
    assert empty.text() == SEARCH_EMPTY_TEXT
    assert "zebra" not in empty.text(), "the empty state does not repeat what the owner typed"
    assert not _shown(tab, "openai") and not _shown(tab, "anthropic")

    search.clear()
    assert empty.isHidden()
    assert _shown(tab, "openai") and _shown(tab, "anthropic")


def test_the_empty_state_shows_for_an_empty_list_and_a_search_text(qtbot, settings_path):
    tab = _keys_tab(qtbot, settings_path, KeyringSecretStore(backend=MemoryKeyring()))
    _wait_names_loaded(qtbot, tab.findChild(KeysSection, "keys-section"))

    assert _search_empty(tab).isHidden(), "no search text means no empty state"
    qtbot.keyClicks(_search_box(tab), "openai")
    assert not _search_empty(tab).isHidden()


def test_the_runpod_card_is_filtered_by_its_name_like_the_others(qtbot, settings_path):
    backend = MemoryKeyring()
    secrets = KeyringSecretStore(backend=backend)
    _seed(settings_path, backend, names=("OpenAI",))
    tab = _keys_tab(qtbot, settings_path, secrets)
    _wait_names_loaded(qtbot, tab.findChild(KeysSection, "keys-section"))
    search = _search_box(tab)

    qtbot.keyClicks(search, "runpod")
    assert _shown(tab, "runpod") and not _shown(tab, "openai")

    search.clear()
    qtbot.keyClicks(search, "open")
    assert not _shown(tab, "runpod") and _shown(tab, "openai")
    assert _search_empty(tab).isHidden()


def test_the_filter_holds_across_a_refresh(qtbot, settings_path):
    backend = MemoryKeyring()
    _seed(settings_path, backend, names=("OpenAI", "Anthropic"))
    tab = _keys_tab(qtbot, settings_path, KeyringSecretStore(backend=backend))
    _wait_names_loaded(qtbot, tab.findChild(KeysSection, "keys-section"))

    qtbot.keyClicks(_search_box(tab), "open")
    tab.findChild(KeysSection, "keys-section").refresh()

    assert _shown(tab, "openai") and not _shown(tab, "anthropic")


def test_save_and_remove_still_work_under_a_search_and_no_value_shows(qtbot, settings_path, monkeypatch):
    backend = MemoryKeyring()
    _seed(settings_path, backend, names=("OpenAI", "Anthropic"), value_for="OpenAI")
    tab = _keys_tab(qtbot, settings_path, KeyringSecretStore(backend=backend))
    _wait_names_loaded(qtbot, tab.findChild(KeysSection, "keys-section"))
    qtbot.keyClicks(_search_box(tab), "open")
    assert FAKE_VALUE not in _all_text(tab)

    # Save value: the new value goes to the keychain, the box is cleared, and the card stays in the filtered list.
    tab.findChild(QLineEdit, "keys-entry-openai-value").setText(FAKE_VALUE_TWO)
    tab.findChild(QPushButton, "keys-entry-openai-save").click()
    assert backend.snapshot() == {(KEY_SERVICE, "key-openai"): FAKE_VALUE_TWO}
    assert _shown(tab, "openai") and not _shown(tab, "anthropic")
    assert FAKE_VALUE_TWO not in _all_text(tab)

    # Remove: the owner is asked first, the question names the key, and once confirmed the name leaves the list.
    asked = []
    monkeypatch.setattr(keys_module, "_ask_remove", lambda parent, title, text: asked.append(text) or True)
    tab.findChild(QPushButton, "keys-entry-openai-remove").click()
    assert len(asked) == 1 and "OpenAI" in asked[0]
    assert backend.snapshot() == {}
    assert not _search_empty(tab).isHidden(), "no name matches the search any more"
    assert _search_empty(tab).text() == SEARCH_EMPTY_TEXT
    qtbot.waitUntil(lambda: tab.findChild(QWidget, "keys-entry-openai") is None, timeout=5000)
    assert FAKE_VALUE_TWO not in _all_text(tab)


# ----------------------------------------------------------- no clipping at 1280 px


def _expand_all(tab) -> None:
    for name in GROUP_ORDER:
        tab.findChild(CollapsibleGroup, name).set_expanded(True)


def _fully_inside(widget, tab) -> bool:
    origin = widget.mapTo(tab, QPoint(0, 0))
    return origin.x() >= 0 and origin.x() + widget.width() <= tab.width()


def _show_at_1280(qtbot, page) -> None:
    """Show the page sized 1280 wide and tall enough that no section scrolls out of sight, then let the layout settle."""
    page.resize(TAB_WIDTH, TAB_HEIGHT)
    page.show()
    qtbot.waitExposed(page)
    qtbot.wait(30)


def _show_keys_at_1280(qtbot, tab) -> None:
    """Show the Keys tab at 1280 px, and wait until the names are read from the keychain."""
    _show_at_1280(qtbot, tab)
    _wait_names_loaded(qtbot, tab.findChild(KeysSection, "keys-section"))


def _show_settings_at_1280(qtbot, tab) -> None:
    """Show the Settings tab at 1280 px, and wait until its content column has its full width."""
    _show_at_1280(qtbot, tab)
    qtbot.waitUntil(lambda: tab.findChild(QWidget, "settings-content").width() >= 600, timeout=5000)


def _assert_labels_and_buttons_fully_inside(tab, *, min_labels: int, min_buttons: int) -> list[QPushButton]:
    labels = [label for label in tab.findChildren(QLabel) if label.isVisibleTo(tab) and label.text()]
    assert len(labels) >= min_labels, f"the walk reaches the real labels: found {len(labels)}"
    for label in labels:
        if label.wordWrap():
            assert label.height() >= label.heightForWidth(label.width()) - 1, label.objectName() or label.text()
        else:
            assert label.width() >= label.sizeHint().width() - 1, label.objectName() or label.text()
        assert _fully_inside(label, tab), label.objectName() or label.text()

    buttons = [button for button in tab.findChildren(QPushButton) if button.isVisibleTo(tab)]
    assert len(buttons) >= min_buttons, f"the walk reaches the real buttons: found {len(buttons)}"
    for button in buttons:
        assert button.width() >= button.minimumSizeHint().width() - 1, button.objectName()
        assert button.height() >= button.minimumSizeHint().height() - 1, button.objectName()
        assert _fully_inside(button, tab), button.objectName()
    return buttons


def test_no_label_or_button_on_the_settings_tab_is_clipped_at_1280_px(qtbot, settings_path):
    secrets = KeyringSecretStore(backend=MemoryKeyring())
    tab = _tab(qtbot, settings_path, secrets)
    _expand_all(tab)
    _show_settings_at_1280(qtbot, tab)

    _assert_labels_and_buttons_fully_inside(tab, min_labels=20, min_buttons=15)


def test_no_label_or_button_on_the_keys_tab_is_clipped_at_1280_px(qtbot, settings_path):
    backend = MemoryKeyring()
    _seed(settings_path, backend, names=("OpenAI", "Anthropic"), value_for="OpenAI")
    tab = _keys_tab(qtbot, settings_path, KeyringSecretStore(backend=backend))
    _show_keys_at_1280(qtbot, tab)
    # Each card's controls sit behind its Details button, so open the two named cards before the walk. Only visible
    # widgets are checked, and an opened card's controls are visible.
    for key in ("openai", "anthropic"):
        tab.findChild(QPushButton, f"keys-entry-{key}-details-toggle").click()
    qtbot.waitUntil(lambda: tab.findChild(QPushButton, "keys-entry-openai-save").isVisible(), timeout=5000)

    buttons = _assert_labels_and_buttons_fully_inside(tab, min_labels=8, min_buttons=10)
    # The walk reaches the RunPod card and the two named cards, not only the add form.
    for name in ("runpod-save-key", "runpod-remove-key", "keys-add", "keys-entry-openai-save", "keys-entry-anthropic-user-env"):
        assert tab.findChild(QPushButton, name) in buttons, name


def test_client_setup_apply_remove_and_the_check_result_are_fully_visible_at_1280_px(qtbot, settings_path):
    secrets = KeyringSecretStore(backend=MemoryKeyring())
    tab = _tab(qtbot, settings_path, secrets)
    _expand_all(tab)
    result = tab.findChild(QLabel, "client-setup-result")
    result.setText(
        "Written: C:\\Users\\example\\.claude\\settings.json. Backup saved at: "
        "C:\\Users\\example\\.claude\\settings.json.bak. Restart Claude Code to load the change."
    )
    result.setVisible(True)
    _show_settings_at_1280(qtbot, tab)

    actions = [
        button
        for button in tab.findChild(CollapsibleGroup, "settings-group-client-setup").findChildren(QPushButton)
        if button.objectName().endswith(("-apply", "-remove"))
    ]
    assert len(actions) >= 4, "each client has its Apply and Remove"
    for button in actions:
        assert button.height() >= button.minimumSizeHint().height() - 1, button.objectName()
        assert _fully_inside(button, tab), button.objectName()
    assert result.height() >= result.heightForWidth(result.width()) - 1, "the check result is not cut off"
    for caption in ("client-setup-checkout-label", "client-setup-python-label"):
        label = tab.findChild(QLabel, caption)
        assert label.wordWrap() is False, caption
        assert label.width() >= label.sizeHint().width() - 1, caption
