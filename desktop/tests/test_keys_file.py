"""Tests for the keys file: a .json, .env or secrets file of key names and values that the owner chooses, and the Keys block listing it.

The file belongs to the owner, so the app never writes it. Every test that could touch it checks its bytes and its
modification time before and after. FAKE_VALUE is an obviously fake key, so the leak checks can search every output for
it. The keychain is a MemoryKeyring, every file dialog is an injected fake, and no test opens a socket, the real
credential store or a real dialog.
"""

from __future__ import annotations

import dataclasses
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton, QWidget

from _fakes import MemoryKeyring
from meridian_desktop.api_keys import KEY_SERVICE, STATUS_SET, ApiKeys, account_for
from meridian_desktop.key_services import (
    FILE_UNREADABLE_TEXT,
    NO_CHECK_TEXT,
    NOT_IN_FILE_TEXT,
    REJECTED_TEXT,
    WORKS_TEXT,
    check_saved_key,
    service_for,
)
from meridian_desktop.keys_file import (
    ENTRY_SKIPPED_TEXT,
    MAX_FILE_BYTES,
    NOT_SUPPORTED_TEXT,
    NOT_TEXT_TEXT,
    TOO_LARGE_TEXT,
    FileNames,
    KeysFileError,
    is_classified,
    read_names,
    read_value,
    skipped_summary,
    split_by_classification,
)
from meridian_desktop.secret_store import MAX_SECRET_LENGTH, KeyringSecretStore
from meridian_desktop.settings_store import Settings, SettingsStore
from meridian_desktop import api_keys, key_services
from meridian_desktop.ui import keys_section as keys_module
from meridian_desktop.ui.keys_section import (
    FILE_LISTED_MANY_TEXT,
    FILE_PATH_TEXT,
    FILE_STOPPED_TEXT,
    FROM_FILE_NOTE,
    NO_FILE_TEXT,
    REMOVED_FROM_FILE_TEXT,
    STATUS_TEXT,
    KeysSection,
)

FAKE_VALUE = "sk-fake-keys-file-0001"
FAKE_VALUE_TWO = "sk-fake-keys-file-0002"
# 2001-09-09. An old modification time, so a later write to the file would show as a new one.
PAST_SECONDS = 1_000_000_000
OPENAI = service_for("OpenAI")


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


def _header(request: urllib.request.Request, name: str) -> str | None:
    """A request header by name, ignoring case, since urllib capitalizes names its own way."""
    headers = {key.lower(): value for key, value in request.header_items()}
    return headers.get(name.lower())


def _all_text(widget: QWidget) -> str:
    """Every label, button and placeholder on the widget. The leak checks search this."""
    parts = [label.text() for label in widget.findChildren(QLabel)]
    parts += [button.text() for button in widget.findChildren(QPushButton)]
    parts += [line.placeholderText() for line in widget.findChildren(QLineEdit)]
    return "\n".join(parts)


def _write(tmp_path: Path, content) -> str:
    """Write the keys file under tmp_path and return its path. A dict is written as JSON; text is written as it is."""
    folder = tmp_path / "keys"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "my-keys.json"
    text = content if isinstance(content, str) else json.dumps(content)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _pin(path: str) -> None:
    """Give the file an old modification time, so a write to it cannot go unseen."""
    os.utime(path, (PAST_SECONDS, PAST_SECONDS))


def _state(path: str) -> tuple[bytes, int]:
    """The bytes and the modification time of a file. A write changes at least one of them."""
    return Path(path).read_bytes(), Path(path).stat().st_mtime_ns


def _empty_keychain() -> KeyringSecretStore:
    return KeyringSecretStore(backend=MemoryKeyring())


def _section(qtbot, settings_path: Path, backend, *, transport=None, choose=None) -> KeysSection:
    """A Keys block over the given keychain. ``choose`` is the file dialog fake. Its first read is awaited."""
    keys = ApiKeys(SettingsStore(settings_path), KeyringSecretStore(backend=backend))
    section = KeysSection(keys, transport=transport, choose_file=choose)
    qtbot.addWidget(section)
    qtbot.waitUntil(lambda: not section.loading, timeout=5000)
    return section


def _chooser(path: str):
    """A file dialog fake: the owner picks ``path``. It is called with the parent, as the real dialog is."""
    return lambda parent: path


def _choose(section: KeysSection) -> None:
    section.findChild(QPushButton, "keys-file-choose").click()


# ----------------------------------------------------------- the file as a listing


def test_a_valid_file_lists_its_names_in_file_order_and_no_value(tmp_path):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE, "Anthropic": FAKE_VALUE_TWO})

    listing = read_names(path)

    assert listing == FileNames(names=("OpenAI", "Anthropic"), problem=None)
    assert FAKE_VALUE not in repr(listing) and FAKE_VALUE_TWO not in repr(listing)


def test_an_empty_object_lists_no_names_and_is_not_a_refusal(tmp_path):
    assert read_names(_write(tmp_path, {})) == FileNames(names=(), problem=None)


@pytest.mark.parametrize("content", ['["OpenAI"]', '"sk-fake"', "", "{not json"])
def test_a_file_that_is_not_a_json_object_is_refused_with_a_count_and_no_value(tmp_path, content):
    listing = read_names(_write(tmp_path, content))

    assert listing.names == ()
    assert listing.problem is not None and "0 entries were read" in listing.problem
    assert "sk-fake" not in listing.problem


def _write_named(tmp_path: Path, name: str, content) -> str:
    """Write a keys file with this exact name under tmp_path and return its path. Text is written as UTF-8; bytes as they are."""
    folder = tmp_path / "keys"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return str(path)


@pytest.mark.parametrize("name", [".env", ".env.local", "production.env", "keys.secrets", "keys.secret", "secrets.local", "secrets"])
def test_a_dotenv_or_secrets_name_is_read_as_name_equals_value_lines(tmp_path, name):
    path = _write_named(tmp_path, name, f"OpenAI={FAKE_VALUE}\nAnthropic={FAKE_VALUE_TWO}\n")

    listing = read_names(path)

    assert listing == FileNames(names=("OpenAI", "Anthropic"), problem=None)
    assert FAKE_VALUE not in repr(listing) and FAKE_VALUE_TWO not in repr(listing)
    assert read_value(path, "Anthropic") == FAKE_VALUE_TWO


def test_dotenv_export_quotes_comments_and_equals_signs_are_read(tmp_path):
    path = _write_named(
        tmp_path,
        ".env",
        "# keys for the desktop\n"
        "\n"
        f'export OpenAI="{FAKE_VALUE}"\n'
        f"Anthropic='{FAKE_VALUE_TWO}'   # the second one\n"
        "GitHub=sk-with=equals-inside\n",
    )

    assert read_names(path) == FileNames(names=("OpenAI", "Anthropic", "GitHub"), problem=None)
    assert read_value(path, "OpenAI") == FAKE_VALUE
    assert read_value(path, "Anthropic") == FAKE_VALUE_TWO
    assert read_value(path, "GitHub") == "sk-with=equals-inside"


@pytest.mark.parametrize("name", ["keys.txt", "keys.toml", "keys.yaml", "keys", "keys.env.bak"])
def test_a_file_of_another_kind_is_refused_by_its_name_before_it_is_read(tmp_path, name):
    path = _write_named(tmp_path, name, f"OpenAI={FAKE_VALUE}\n")

    listing = read_names(path)

    assert listing.names == ()
    assert listing.problem == NOT_SUPPORTED_TEXT
    assert FAKE_VALUE not in listing.problem


def test_a_dotenv_line_that_is_not_name_equals_value_is_refused_by_line_number_and_no_value(tmp_path):
    path = _write_named(tmp_path, ".env", f"OpenAI={FAKE_VALUE}\nthis line has no equals sign {FAKE_VALUE_TWO}\nAnthropic=\n")

    listing = read_names(path)

    assert listing.names == ("OpenAI",)
    assert listing.problem is None
    note = skipped_summary(listing)
    assert "Skipped 2 of 3 entries" in note
    assert "line 2: line is not NAME=value" in note
    assert "line 3: value is empty" in note
    for hidden in (FAKE_VALUE, FAKE_VALUE_TWO, "OpenAI", "Anthropic", "this line"):
        assert hidden not in note


def test_dotenv_quote_problems_are_refused_by_line_number_and_no_value(tmp_path):
    path = _write_named(tmp_path, ".env", f'OpenAI="{FAKE_VALUE}\nAnthropic=\'{FAKE_VALUE_TWO}\' extra\n')

    listing = read_names(path)

    assert "line 1: value has no closing quote" in listing.problem
    assert "line 2: value has text after its closing quote" in listing.problem
    assert FAKE_VALUE not in listing.problem and FAKE_VALUE_TWO not in listing.problem


def test_a_repeated_name_in_a_dotenv_file_names_the_line_it_repeats(tmp_path):
    path = _write_named(tmp_path, ".env", f"OpenAI={FAKE_VALUE}\nopenai={FAKE_VALUE_TWO}\n")

    listing = read_names(path)

    assert listing.names == ("OpenAI",)
    assert "line 2: name repeats the one on line 1" in skipped_summary(listing)
    assert FAKE_VALUE not in skipped_summary(listing) and FAKE_VALUE_TWO not in skipped_summary(listing)


def test_web_ext_names_are_read_as_listed_names_and_values(tmp_path):
    path = _write_named(tmp_path, ".env", f"WEB_EXT_API_KEY={FAKE_VALUE}\nWEB_EXT_API_SECRET={FAKE_VALUE_TWO}\n")

    assert read_names(path).names == ("WEB_EXT_API_KEY", "WEB_EXT_API_SECRET")
    assert read_value(path, "WEB_EXT_API_SECRET") == FAKE_VALUE_TWO


def test_a_tab_after_export_is_read_as_export_and_a_hash_right_after_equals_is_part_of_the_value(tmp_path):
    path = _write_named(tmp_path, ".env", f"export\tOpenAI={FAKE_VALUE}\nAnthropic=#{FAKE_VALUE_TWO}\n")

    assert read_names(path) == FileNames(names=("OpenAI", "Anthropic"), problem=None)
    assert read_value(path, "Anthropic") == "#" + FAKE_VALUE_TWO


def test_a_hash_after_whitespace_starts_a_comment_so_the_value_is_empty_and_refused(tmp_path):
    path = _write_named(tmp_path, ".env", "GitHub=\t# only a comment\n")

    assert "line 1: value is empty" in read_names(path).problem


def test_a_form_feed_inside_a_value_does_not_shift_the_line_numbers(tmp_path):
    path = _write_named(tmp_path, ".env", f"OpenAI={FAKE_VALUE}\x0cX\nthis has no equals sign\nAnthropic={FAKE_VALUE_TWO}\n")

    listing = read_names(path)

    assert listing.names == ("OpenAI", "Anthropic")
    note = skipped_summary(listing)
    assert "Skipped 1 of 3 entries" in note
    assert "line 2: line is not NAME=value" in note


def test_a_dotenv_file_with_five_bad_lines_among_fifty_seven_loads_the_other_fifty_two(tmp_path):
    good = [f"KEY{number:02d}=value-{number:02d}" for number in range(1, 53)]
    bad = ["  -----BEGIN PRIVATE KEY-----", "MIIEvQIBADANBgkq", "continuation text", "+/abc==", "-----END PRIVATE KEY-----"]
    lines = good + bad  # 52 usable lines, then 5 bad ones
    path = _write_named(tmp_path, ".env", "\n".join(lines) + "\n")

    listing = read_names(path)

    assert listing.problem is None, "the file loads: a bad line is skipped, not the whole file"
    assert len(listing.names) == 52 and listing.total == 57
    assert len(listing.refused) == 5
    note = skipped_summary(listing)
    assert "Skipped 5 of 57 entries" in note
    assert "line 53: line is not NAME=value" in note
    assert "value-01" not in note, "no value shows in the skip note"


def test_a_dotenv_file_over_the_size_limit_is_refused_before_it_is_read(tmp_path):
    path = _write_named(tmp_path, ".env", "OpenAI=" + "x" * MAX_FILE_BYTES)

    assert read_names(path).problem == TOO_LARGE_TEXT


def test_a_dotenv_file_that_is_not_utf8_is_refused_with_no_value(tmp_path):
    path = _write_named(tmp_path, ".env", b"OpenAI=\xff\xfe")

    listing = read_names(path)

    assert listing.problem == NOT_TEXT_TEXT


def test_entries_that_are_not_usable_are_skipped_and_the_rest_still_load(tmp_path):
    path = _write(
        tmp_path,
        {"OpenAI": FAKE_VALUE, "Anthropic": 42, "GitHub": [FAKE_VALUE_TWO], "Bad/Name": FAKE_VALUE_TWO, "Blank": "   "},
    )

    listing = read_names(path)

    assert listing.names == ("OpenAI",)
    assert listing.problem is None
    assert len(listing.refused) == 4 and listing.total == 5
    note = skipped_summary(listing)
    assert "Skipped 4 of 5 entries" in note
    for hidden in (FAKE_VALUE, FAKE_VALUE_TWO, "Anthropic", "GitHub", "Bad/Name", "Blank"):
        assert hidden not in note, "a skipped entry names no entry and shows no value"


def test_a_name_repeated_by_case_or_naming_runpod_is_skipped_and_the_first_loads(tmp_path):
    text = '{"OpenAI": "%s", "openai": "%s", "RunPod": "%s"}' % (FAKE_VALUE, FAKE_VALUE_TWO, FAKE_VALUE)

    listing = read_names(_write(tmp_path, text))

    assert listing.names == ("OpenAI",)
    assert listing.problem is None
    assert "Skipped 2 of 3 entries" in skipped_summary(listing)


def test_a_skipped_runpod_entry_says_it_has_its_own_entry_and_shows_no_value(tmp_path):
    listing = read_names(_write(tmp_path, {"RunPod": FAKE_VALUE, "OpenAI": FAKE_VALUE_TWO}))

    assert listing.names == ("OpenAI",)
    note = skipped_summary(listing)
    assert "Skipped 1 of 2 entries" in note
    assert "which has its own entry" in note, "the note says RunPod keeps its own entry"
    assert FAKE_VALUE not in note and FAKE_VALUE_TWO not in note


def test_a_value_longer_than_the_limit_is_skipped_and_when_it_is_the_only_entry_nothing_loads(tmp_path):
    listing = read_names(_write(tmp_path, {"OpenAI": "x" * (MAX_SECRET_LENGTH + 1)}))

    assert listing.names == ()
    assert "All 1 entries were skipped" in listing.problem
    assert "value is longer than" in listing.problem


def test_a_file_larger_than_the_limit_is_refused_before_it_is_read(tmp_path):
    path = tmp_path / "big.json"
    path.write_text('{"OpenAI": "' + "a" * MAX_FILE_BYTES + '"}', encoding="utf-8")

    listing = read_names(str(path))

    assert listing.names == ()
    assert "larger than" in listing.problem


def test_a_missing_file_says_so_plainly(tmp_path):
    listing = read_names(str(tmp_path / "gone.json"))

    assert listing.names == ()
    assert "is missing" in listing.problem


def test_a_folder_in_place_of_the_file_is_refused_as_unreadable(tmp_path):
    # A folder with a .json name gets past the name check, so the read itself is what refuses it.
    folder = tmp_path / "as-a-folder.json"
    folder.mkdir()
    listing = read_names(str(folder))

    assert listing.names == ()
    assert "could not be read" in listing.problem


# ----------------------------------------------------------- the file as a source of one value


def test_a_check_reads_one_value_from_the_file_and_nothing_else(tmp_path):
    path = _write(tmp_path, {"OpenAI": f"  {FAKE_VALUE}  ", "Anthropic": FAKE_VALUE_TWO})

    assert read_value(path, "OpenAI") == FAKE_VALUE, "the value comes back stripped of its spaces"
    assert read_value(path, "anthropic") == FAKE_VALUE_TWO, "a name matches by its slug"
    assert read_value(path, "GitHub") is None, "a name the file does not hold has no value"


def test_a_check_refuses_a_name_whose_entry_was_skipped_and_still_reads_the_rest(tmp_path):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE, "Anthropic": 7})

    assert read_value(path, "OpenAI") == FAKE_VALUE, "a usable entry still loads"
    with pytest.raises(KeysFileError) as caught:
        read_value(path, "Anthropic")

    assert caught.value.entry_skipped is True
    assert str(caught.value) == ENTRY_SKIPPED_TEXT
    assert FAKE_VALUE not in str(caught.value)


def test_listing_and_checking_never_write_the_file(tmp_path):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE})
    _pin(path)
    before = _state(path)

    read_names(path)
    read_value(path, "OpenAI")
    read_names(str(tmp_path / "gone.json"))

    assert _state(path) == before


# ----------------------------------------------------------- the settings key


def test_the_keys_file_path_is_saved_as_a_path_and_only_when_it_is_set(settings_path, tmp_path):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE})
    store = SettingsStore(settings_path)

    store.save(Settings(keys_file_path=path))
    text = settings_path.read_text(encoding="utf-8")
    assert json.loads(text)["keys_file_path"] == path
    assert FAKE_VALUE not in text, "the value never reaches the settings file"

    store.save(Settings())
    assert "keys_file_path" not in json.loads(settings_path.read_text(encoding="utf-8"))
    assert store.load().settings.keys_file_path == ""


def test_a_keys_file_path_that_is_not_text_is_refused_with_a_warning(settings_path):
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"keys_file_path": ["x"]}), encoding="utf-8")

    loaded = SettingsStore(settings_path).load()

    assert loaded.settings.keys_file_path == ""
    assert loaded.warning is not None and "keys_file_path" in loaded.warning


# ----------------------------------------------------------- the Check reads from the file


def test_a_check_reads_its_value_from_the_file_and_not_from_the_keychain(tmp_path):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE})
    backend = MemoryKeyring()
    backend.set_password(KEY_SERVICE, account_for("OpenAI"), FAKE_VALUE_TWO)
    transport = FakeTransport(200)

    result = check_saved_key(OPENAI, "OpenAI", KeyringSecretStore(backend=backend), transport=transport, keys_file=path)

    assert result == WORKS_TEXT
    request, timeout = transport.calls[0]
    assert urllib.parse.urlsplit(request.full_url).hostname == "api.openai.com"
    assert _header(request, "Authorization") == "Bearer " + FAKE_VALUE
    assert timeout == 10.0


def test_a_check_for_a_name_the_file_does_not_hold_says_so_and_sends_nothing(tmp_path):
    path = _write(tmp_path, {"Anthropic": FAKE_VALUE})
    backend = MemoryKeyring()
    backend.set_password(KEY_SERVICE, account_for("OpenAI"), FAKE_VALUE_TWO)
    transport = FakeTransport(200)

    result = check_saved_key(OPENAI, "OpenAI", KeyringSecretStore(backend=backend), transport=transport, keys_file=path)

    assert result == NOT_IN_FILE_TEXT.format(name="OpenAI") == "OpenAI is not in the keys file."
    assert transport.calls == [], "the keychain value is not used for a file check"


def test_a_check_with_a_missing_or_refused_file_says_so_and_sends_nothing(tmp_path):
    missing = str(tmp_path / "gone.json")
    whole_file_refused = _write(tmp_path, '"sk-fake"')  # a JSON string is not an object, so the whole file is refused
    transport = FakeTransport(200)

    for path in (missing, whole_file_refused):
        result = check_saved_key(OPENAI, "OpenAI", _empty_keychain(), transport=transport, keys_file=path)
        assert result == FILE_UNREADABLE_TEXT == "The keys file could not be read. Choose the file again."

    assert transport.calls == []


def test_a_check_of_a_skipped_entry_says_it_was_skipped_and_sends_nothing(tmp_path):
    skipped = _write(tmp_path, {"OpenAI": 7})  # the file loads, but this entry is skipped
    transport = FakeTransport(200)

    result = check_saved_key(OPENAI, "OpenAI", _empty_keychain(), transport=transport, keys_file=skipped)

    assert result == ENTRY_SKIPPED_TEXT
    assert transport.calls == []


def test_a_service_with_no_check_reads_neither_the_file_nor_the_keychain(tmp_path):
    refused = _write(tmp_path, {"OpenAI": 7})
    no_check = dataclasses.replace(OPENAI, check=None)
    transport = FakeTransport(200)

    result = check_saved_key(no_check, "OpenAI", _empty_keychain(), transport=transport, keys_file=refused)

    assert result == NO_CHECK_TEXT
    assert transport.calls == []


# ----------------------------------------------------------- the Keys block


def test_choosing_a_file_lists_its_names_as_from_file_and_copies_nothing_to_the_keychain(
    qtbot, settings_path, tmp_path
):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE, "Anthropic": FAKE_VALUE_TWO})
    backend = MemoryKeyring()
    section = _section(qtbot, settings_path, backend, choose=_chooser(path))

    _choose(section)

    assert section.findChild(QLabel, "keys-entry-openai-status").text() == FROM_FILE_NOTE
    assert section.findChild(QLabel, "keys-entry-anthropic-status").text() == FROM_FILE_NOTE
    assert section.findChild(QLineEdit, "keys-entry-openai-value") is None, "a file name has no value box"
    assert section.findChild(QLabel, "keys-file-status").text() == FILE_LISTED_MANY_TEXT.format(count=2)
    assert section.findChild(QLabel, "keys-file-path").text() == FILE_PATH_TEXT.format(name="my-keys.json")
    saved = SettingsStore(settings_path).load().settings
    assert saved.keys_file_path == path
    assert saved.key_names == (), "a file name is not saved as a keychain name"
    assert backend.snapshot() == {}, "nothing is copied to the keychain"
    assert FAKE_VALUE not in _all_text(section)
    assert FAKE_VALUE not in settings_path.read_text(encoding="utf-8")


def test_the_choose_dialog_opens_on_the_home_folder_and_a_cancel_changes_nothing(qtbot, settings_path, monkeypatch):
    asked: dict[str, object] = {}

    class FakeDialog:
        @staticmethod
        def getOpenFileName(parent, title, start, names):
            asked.update(start=start, names=names)
            return "", ""

    monkeypatch.setattr(keys_module, "QFileDialog", FakeDialog)
    section = _section(qtbot, settings_path, MemoryKeyring())

    _choose(section)

    assert asked["start"] == str(Path.home())
    assert "*.json" in asked["names"]
    assert SettingsStore(settings_path).load().settings.keys_file_path == ""
    assert section.findChild(QLabel, "keys-file-path").text() == NO_FILE_TEXT


def test_the_choose_dialog_offers_the_keys_file_kinds_the_app_reads(qtbot, settings_path, monkeypatch):
    asked: dict[str, object] = {}

    class FakeDialog:
        @staticmethod
        def getOpenFileName(parent, title, start, names):
            asked.update(names=names)
            return "", ""

    monkeypatch.setattr(keys_module, "QFileDialog", FakeDialog)
    section = _section(qtbot, settings_path, MemoryKeyring())

    _choose(section)

    assert asked["names"] == keys_module.FILE_CHOOSER_FILTER
    for pattern in ("*.json", "*.env", ".env.*", "*.secrets", "secrets.*"):
        assert pattern in asked["names"], pattern
    assert "All files" not in asked["names"], "a name the app does not read is refused by name, so it is not offered"


def test_a_file_with_a_skipped_entry_is_chosen_and_the_skip_is_shown_without_a_name_or_value(qtbot, settings_path, tmp_path):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE, "Anthropic": 9})
    section = _section(qtbot, settings_path, MemoryKeyring(), choose=_chooser(path))

    _choose(section)

    status = section.findChild(QLabel, "keys-file-status").text()
    assert "Skipped 1 of 2 entries" in status
    for hidden in (FAKE_VALUE, "Anthropic"):
        assert hidden not in status
    assert SettingsStore(settings_path).load().settings.keys_file_path == path, "the file with its usable entry is chosen"
    assert section.findChild(QWidget, "keys-entry-openai") is not None, "the usable entry is listed"


def test_a_check_on_a_from_file_name_reads_the_value_from_the_file(qtbot, settings_path, tmp_path):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE})
    transport = FakeTransport(200)
    section = _section(qtbot, settings_path, MemoryKeyring(), transport=transport, choose=_chooser(path))
    _choose(section)
    result = section.findChild(QLabel, "keys-entry-openai-check-result")

    section.findChild(QPushButton, "keys-entry-openai-check").click()
    qtbot.waitUntil(lambda: result.text() == WORKS_TEXT, timeout=5000)

    assert _header(transport.calls[0][0], "Authorization") == "Bearer " + FAKE_VALUE
    assert FAKE_VALUE not in _all_text(section)


def test_a_check_on_a_from_file_name_that_the_file_later_drops_says_so(qtbot, settings_path, tmp_path):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE})
    transport = FakeTransport(200)
    section = _section(qtbot, settings_path, MemoryKeyring(), transport=transport, choose=_chooser(path))
    _choose(section)
    _write(tmp_path, {"Anthropic": FAKE_VALUE_TWO})
    section.findChild(QPushButton, "keys-entry-openai-check").click()
    result = section.findChild(QLabel, "keys-entry-openai-check-result")

    qtbot.waitUntil(lambda: result.text() == NOT_IN_FILE_TEXT.format(name="OpenAI"), timeout=5000)

    assert transport.calls == []


def test_remove_on_a_from_file_name_hides_it_for_this_session_and_leaves_the_file_unchanged(
    qtbot, settings_path, tmp_path
):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE, "Anthropic": FAKE_VALUE_TWO})
    _pin(path)
    before = _state(path)
    section = _section(qtbot, settings_path, MemoryKeyring(), choose=_chooser(path))
    _choose(section)

    section.findChild(QPushButton, "keys-entry-openai-remove").click()
    qtbot.waitUntil(lambda: section.findChild(QWidget, "keys-entry-openai") is None, timeout=5000)

    message = section.findChild(QLabel, "keys-message").text()
    assert message == REMOVED_FROM_FILE_TEXT.format(name="OpenAI")
    assert "unchanged" in message
    assert section.findChild(QWidget, "keys-entry-anthropic") is not None
    assert _state(path) == before, "the file is not written"
    assert SettingsStore(settings_path).load().settings.keys_file_path == path, "the file stays chosen"

    section.refresh()
    assert section.findChild(QWidget, "keys-entry-openai") is None, "a refresh does not bring the removed name back"


def test_a_missing_file_says_so_plainly_on_the_block(qtbot, settings_path, tmp_path):
    gone = str(tmp_path / "gone.json")
    SettingsStore(settings_path).save(Settings(keys_file_path=gone))

    section = _section(qtbot, settings_path, MemoryKeyring())

    assert "is missing" in section.findChild(QLabel, "keys-file-status").text()
    assert section.findChild(QLabel, "keys-file-path").text() == FILE_PATH_TEXT.format(name="gone.json")
    assert section.findChild(QPushButton, "keys-file-stop").isEnabled(), "a missing file can still be stopped"
    assert section.findChild(QWidget, "keys-entry-openai") is None


def test_stop_using_the_file_clears_the_path_and_its_names_and_leaves_the_file(qtbot, settings_path, tmp_path):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE})
    _pin(path)
    before = _state(path)
    section = _section(qtbot, settings_path, MemoryKeyring(), choose=_chooser(path))
    assert not section.findChild(QPushButton, "keys-file-stop").isEnabled(), "there is nothing to stop yet"
    _choose(section)

    section.findChild(QPushButton, "keys-file-stop").click()
    qtbot.waitUntil(lambda: section.findChild(QWidget, "keys-entry-openai") is None, timeout=5000)

    assert SettingsStore(settings_path).load().settings.keys_file_path == ""
    assert section.findChild(QLabel, "keys-file-path").text() == NO_FILE_TEXT
    assert section.findChild(QLabel, "keys-message").text() == FILE_STOPPED_TEXT
    assert _state(path) == before


def test_a_name_in_both_the_keychain_and_the_file_shows_once_as_the_keychain_name(qtbot, settings_path, tmp_path):
    path = _write(tmp_path, {"openai": FAKE_VALUE_TWO})
    backend = MemoryKeyring()
    backend.set_password(KEY_SERVICE, account_for("OpenAI"), FAKE_VALUE)
    SettingsStore(settings_path).save(Settings(key_names=("OpenAI",), keys_file_path=path))

    section = _section(qtbot, settings_path, backend)

    assert section.findChild(QLineEdit, "keys-entry-openai-value") is not None, "the keychain card is the one shown"
    assert section.findChild(QLabel, "keys-entry-openai-status").text() == STATUS_TEXT[STATUS_SET]
    assert FAKE_VALUE not in _all_text(section)
    assert FAKE_VALUE_TWO not in _all_text(section)


def test_a_file_that_goes_bad_after_it_was_chosen_lists_nothing_and_says_why(qtbot, settings_path, tmp_path):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE})
    section = _section(qtbot, settings_path, MemoryKeyring(), choose=_chooser(path))
    _choose(section)
    assert section.findChild(QWidget, "keys-entry-openai") is not None

    _write(tmp_path, '"now a string, not an object"')  # the whole file is refused now
    section.refresh()
    qtbot.waitUntil(lambda: section.findChild(QWidget, "keys-entry-openai") is None, timeout=5000)

    status = section.findChild(QLabel, "keys-file-status").text()
    assert "must hold one JSON object" in status
    assert FAKE_VALUE not in status


def test_no_value_reaches_the_keychain_the_settings_file_or_any_text_on_the_block(qtbot, settings_path, tmp_path):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE})
    backend = MemoryKeyring()
    transport = FakeTransport(401)
    section = _section(qtbot, settings_path, backend, transport=transport, choose=_chooser(path))
    _choose(section)
    result = section.findChild(QLabel, "keys-entry-openai-check-result")

    section.findChild(QPushButton, "keys-entry-openai-check").click()
    qtbot.waitUntil(lambda: result.text() == REJECTED_TEXT.format(service="OpenAI"), timeout=5000)

    request, _timeout = transport.calls[0]
    assert urllib.parse.urlsplit(request.full_url).hostname == "api.openai.com", "the value goes to the provider only"
    assert _header(request, "Authorization") == "Bearer " + FAKE_VALUE
    assert backend.snapshot() == {}
    assert FAKE_VALUE not in settings_path.read_text(encoding="utf-8")
    assert FAKE_VALUE not in _all_text(section)


def test_the_file_is_never_written_by_choosing_checking_removing_or_stopping(qtbot, settings_path, tmp_path):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE, "Anthropic": FAKE_VALUE_TWO})
    _pin(path)
    before = _state(path)
    section = _section(qtbot, settings_path, MemoryKeyring(), transport=FakeTransport(200), choose=_chooser(path))
    _choose(section)
    result = section.findChild(QLabel, "keys-entry-openai-check-result")
    section.findChild(QPushButton, "keys-entry-openai-check").click()
    qtbot.waitUntil(lambda: result.text() == WORKS_TEXT, timeout=5000)

    section.findChild(QPushButton, "keys-entry-openai-remove").click()
    qtbot.waitUntil(lambda: section.findChild(QWidget, "keys-entry-openai") is None, timeout=5000)
    section.findChild(QPushButton, "keys-file-stop").click()
    qtbot.waitUntil(lambda: section.findChild(QWidget, "keys-entry-anthropic") is None, timeout=5000)

    assert _state(path) == before


# ----------------------------------------------------------- the classified and unclassified groups


def _layout_order(section: KeysSection) -> list[str]:
    """The object names of the widgets in the Keys list, in the order they are laid out. Hidden ones are included."""
    layout = section._entry_layout  # noqa: SLF001 - the order of the list is what the groups are checked by
    widgets = (layout.itemAt(index).widget() for index in range(layout.count()))
    return [widget.objectName() for widget in widgets if widget is not None]


def test_a_name_is_classified_when_it_is_a_known_service_or_a_built_in_name(monkeypatch):
    # Changed from the old rule, which classified only the built-in RunPod name. Each known service in key_services.SERVICES
    # (OpenAI, Anthropic, GitHub) is classified now too, as is any built-in name. Every other name stays unclassified.
    assert is_classified("RunPod") is True
    assert is_classified("OpenAI") is True
    assert is_classified("Anthropic") is True
    assert is_classified("GitHub") is True
    assert is_classified("Acme") is False
    assert split_by_classification(["Acme", "OpenAI", "Zeta"]) == (["OpenAI"], ["Acme", "Zeta"])
    monkeypatch.setattr(api_keys, "BUILT_IN_NAMES", ("RunPod", "Acme"))
    assert is_classified("acme") is True, "the match ignores case, as a name's slug does"
    assert is_classified("Zeta") is False
    assert split_by_classification(["Zeta", "Acme", "GitHub"]) == (["Acme", "GitHub"], ["Zeta"])
    monkeypatch.setattr(key_services, "SERVICES", (key_services.KeyService("Zeta", "A test service.", "a test page"),))
    assert is_classified("zeta") is True, "a name the known services list gains is classified at once"


def test_no_file_name_is_a_known_service_so_the_block_shows_only_the_unclassified_group(qtbot, settings_path, tmp_path):
    path = _write(tmp_path, {"Acme": FAKE_VALUE, "Zeta": FAKE_VALUE_TWO})
    section = _section(qtbot, settings_path, MemoryKeyring(), choose=_chooser(path))

    _choose(section)

    assert section.findChild(QLabel, "keys-file-classified-heading").isHidden()
    assert not section.findChild(QLabel, "keys-file-unclassified-heading").isHidden()
    order = _layout_order(section)
    assert order.index("keys-file-unclassified-heading") < order.index("keys-entry-acme") < order.index("keys-entry-zeta")


def test_the_block_lists_the_classified_group_first_under_its_heading(qtbot, settings_path, tmp_path, monkeypatch):
    # The block asks the registry through these two names, so the test puts one file name in the registry.
    monkeypatch.setattr(keys_module, "is_classified", lambda name: name.casefold() == "openai")
    monkeypatch.setattr(
        keys_module,
        "split_by_classification",
        lambda names: (
            [name for name in names if name.casefold() == "openai"],
            [name for name in names if name.casefold() != "openai"],
        ),
    )
    path = _write(tmp_path, {"Anthropic": FAKE_VALUE, "OpenAI": FAKE_VALUE_TWO, "GitHub": FAKE_VALUE})
    section = _section(qtbot, settings_path, MemoryKeyring(), choose=_chooser(path))

    _choose(section)

    order = _layout_order(section)
    classified = order.index("keys-file-classified-heading")
    unclassified = order.index("keys-file-unclassified-heading")
    assert classified < order.index("keys-entry-openai") < unclassified
    assert unclassified < order.index("keys-entry-anthropic") < order.index("keys-entry-github")
    assert not section.findChild(QLabel, "keys-file-classified-heading").isHidden()
    assert not section.findChild(QLabel, "keys-file-unclassified-heading").isHidden()
    assert FAKE_VALUE not in _all_text(section)
    assert FAKE_VALUE_TWO not in _all_text(section)


def test_a_group_heading_hides_when_the_search_hides_its_group(qtbot, settings_path, tmp_path, monkeypatch):
    monkeypatch.setattr(keys_module, "is_classified", lambda name: name.casefold() == "openai")
    monkeypatch.setattr(
        keys_module,
        "split_by_classification",
        lambda names: (
            [name for name in names if name.casefold() == "openai"],
            [name for name in names if name.casefold() != "openai"],
        ),
    )
    path = _write(tmp_path, {"Anthropic": FAKE_VALUE, "OpenAI": FAKE_VALUE_TWO})
    section = _section(qtbot, settings_path, MemoryKeyring(), choose=_chooser(path))
    _choose(section)

    section.findChild(QLineEdit, "keys-search").setText("openai")

    assert not section.findChild(QLabel, "keys-file-classified-heading").isHidden()
    assert section.findChild(QLabel, "keys-file-unclassified-heading").isHidden()


def test_an_imported_name_is_set_critical_or_non_critical_automatically_and_its_badge_matches(
    qtbot, settings_path, tmp_path
):
    path = _write(tmp_path, {"OpenAI": FAKE_VALUE, "Acme": FAKE_VALUE_TWO})
    section = _section(qtbot, settings_path, MemoryKeyring(), choose=_chooser(path))

    _choose(section)

    assert not section.findChild(QLabel, "keys-entry-openai-badge").isHidden(), "a known service is critical"
    assert section.findChild(QLabel, "keys-entry-acme-badge").isHidden(), "any other name is non-critical"
    assert section.findChild(QLabel, "keys-file-classified-heading").text().startswith("Critical:")
    assert section.findChild(QLabel, "keys-file-unclassified-heading").text().startswith("Non-critical:")
    assert FAKE_VALUE not in _all_text(section)
    assert FAKE_VALUE_TWO not in _all_text(section)
