"""Tests for the owner's custom connectors (meridian_desktop/custom_connectors.py) and their saved settings. Pure Python, no Qt.

Every program lookup is a fake, so nothing on this computer is read. A URL is only parsed, never opened. The settings file is
in pytest's temp dir.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from meridian_desktop import custom_connectors as cc
from meridian_desktop.custom_connectors import ConnectorError
from meridian_desktop.local_cache import CUSTOM_PREFIX
from meridian_desktop.settings_store import Settings, SettingsStore

FOUND = {"notes-cli": "C:/tools/notes-cli.exe", "python": "C:/py/python.exe"}


def finder(found: dict[str, str]):
    """A stand-in for find_command: the mapped path, or None. It never looks at the real PATH."""

    def resolve(name, fallbacks=()):
        return found.get(name)

    return resolve


RESOLVE = finder(FOUND)
NOTHING = finder({})


def stdio(**overrides) -> dict:
    base = {"name": "notes", "kind": "copy", "transport": "stdio", "command": "notes-cli", "args": ["--serve"]}
    base.update(overrides)
    return base


def http(**overrides) -> dict:
    base = {"name": "remote", "kind": "copy", "transport": "http", "url": "https://example.com/mcp"}
    base.update(overrides)
    return base


# ------------------------------------------------------------------ names

@pytest.mark.parametrize("name", ["notes", "Notes_2", "a-b", "x" * 40])
def test_a_safe_name_is_accepted(name):
    assert cc.clean_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["", "   ", "1notes", "my notes", "notes;rm", "../x", "a/b", "x" * 41, "notes.v2", "notes$", "no\ntes", 5, None],
)
def test_an_unsafe_name_is_refused(name):
    with pytest.raises(ConnectorError):
        cc.clean_name(name)


def test_a_name_is_stripped_before_it_is_checked():
    assert cc.clean_name("  notes  ") == "notes"


# ------------------------------------------------------------------ commands and arguments

@pytest.mark.parametrize(
    "command",
    ["notes-cli;calc", "a|b", "a&b", "$HOME/x", "`x`", "a>b", "a<b", 'a"b', "a'b", "a(b)", "a{b}", "a*b", "a%PATH%",
     "a#b", "a!b", "a\nb", "a\tb"],
)
def test_a_command_with_a_shell_metacharacter_is_refused(command):
    with pytest.raises(ConnectorError, match="shell"):
        cc.connector_from_fields(stdio(command=command))


def test_a_windows_path_and_a_short_path_are_accepted():
    connector = cc.connector_from_fields(stdio(command="C:\\Tools\\notes-cli.exe", args=["--data", "C:/PROGRA~1/x"]))
    assert connector.command == "C:\\Tools\\notes-cli.exe"
    assert connector.args == ("--data", "C:/PROGRA~1/x")


def test_an_argument_with_a_shell_metacharacter_is_refused():
    with pytest.raises(ConnectorError, match="shell"):
        cc.connector_from_fields(stdio(args=["--ok", "a;b"]))


def test_arguments_must_be_a_list_and_not_too_many():
    with pytest.raises(ConnectorError, match="list"):
        cc.connector_from_fields(stdio(args="--serve"))
    with pytest.raises(ConnectorError, match="more than"):
        cc.connector_from_fields(stdio(args=["--x"] * 21))


def test_an_empty_command_is_refused_with_a_plain_message():
    with pytest.raises(ConnectorError, match="Type the command first"):
        cc.connector_from_fields(stdio(command="   "))


# ------------------------------------------------------------------ URLs

@pytest.mark.parametrize(
    "url",
    ["https://example.com/mcp", "HTTPS://Example.com/x", "http://127.0.0.1:8000/mcp", "http://localhost/mcp",
     "http://[::1]:8000/mcp"],
)
def test_an_https_url_or_a_loopback_http_url_is_accepted(url):
    assert cc.clean_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/mcp",
        "http://127.0.0.1.example.com/mcp",
        "http://localhost.evil.com/mcp",
        "ftp://example.com/mcp",
        "javascript:alert(1)",
        "https://user:pass@example.com/mcp",
        "https://user@example.com/mcp",
        "https://",
        "https://exa mple.com/mcp",
        "https://example.com:99999/mcp",
        "https://example.com/\nmcp",
        "",
        5,
    ],
)
def test_a_url_that_is_not_https_or_loopback_or_has_credentials_is_refused(url):
    with pytest.raises(ConnectorError):
        cc.clean_url(url)


def test_an_http_connector_with_a_plain_http_url_is_refused_by_the_form():
    with pytest.raises(ConnectorError, match="https://"):
        cc.connector_from_fields(http(url="http://example.com/mcp"))


# ------------------------------------------------------------------ the form fields

def test_a_saved_form_holds_only_the_fields_its_transport_uses():
    saved = cc.connector_from_fields(stdio()).to_saved()
    assert saved == {
        "name": "notes", "kind": "copy", "transport": "stdio", "enabled": True, "tunnel_exposed": False,
        "command": "notes-cli", "args": ["--serve"],
    }
    assert "url" not in saved
    assert "url" in cc.connector_from_fields(http()).to_saved()
    assert "command" not in cc.connector_from_fields(http()).to_saved()


def test_kind_transport_and_flags_are_checked():
    with pytest.raises(ConnectorError, match="copy or linked"):
        cc.connector_from_fields(stdio(kind="other"))
    with pytest.raises(ConnectorError, match="stdio"):
        cc.connector_from_fields(stdio(transport="ssh"))
    with pytest.raises(ConnectorError, match="true or false"):
        cc.connector_from_fields(stdio(enabled="yes"))
    with pytest.raises(ConnectorError, match="true or false"):
        cc.connector_from_fields(stdio(tunnel_exposed=1))


def test_tunnel_exposed_is_off_unless_the_owner_turns_it_on():
    entry = stdio()
    entry.pop("tunnel_exposed", None)
    assert cc.connector_from_fields(entry).tunnel_exposed is False


# ------------------------------------------------------------------ add, duplicates, linked commands

def test_add_appends_one_connector_and_keeps_the_others_in_order():
    raw = cc.add((), stdio(), resolve=RESOLVE)
    raw = cc.add(raw, http(name="remote"), resolve=RESOLVE)
    assert [item["name"] for item in raw] == ["notes", "remote"]


def test_add_refuses_a_duplicate_name_whatever_its_case_and_changes_nothing():
    raw = cc.add((), stdio(name="Notes"), resolve=RESOLVE)
    with pytest.raises(ConnectorError, match="already exists"):
        cc.add(raw, stdio(name="notes"), resolve=RESOLVE)
    assert len(raw) == 1


def test_a_linked_command_must_exist_on_this_computer_when_it_is_added():
    linked = stdio(kind="linked", command="notes-cli")
    assert cc.add((), linked, resolve=RESOLVE)[0]["kind"] == "linked"
    with pytest.raises(ConnectorError, match="was not found on this computer"):
        cc.add((), linked, resolve=NOTHING)


def test_a_copied_command_need_not_exist_when_it_is_added():
    assert len(cc.add((), stdio(command="not-installed-yet"), resolve=NOTHING)) == 1


def test_add_refuses_past_the_limit():
    full = tuple(stdio(name=f"n{index}") for index in range(cc.MAX_CONNECTORS))
    with pytest.raises(ConnectorError, match="limit"):
        cc.add(full, stdio(name="one-more"), resolve=RESOLVE)


# ------------------------------------------------------------------ reading saved entries

def test_reading_saved_entries_marks_each_bad_one_and_keeps_the_good_ones():
    raw = [stdio(), stdio(name="x;y"), "not an object", stdio(name="NOTES"), http()]
    rows = cc.read_rows(raw)
    assert [row.index for row in rows] == [0, 1, 2, 3, 4]
    assert rows[0].connector is not None
    assert rows[1].connector is None and "letter" in rows[1].problem
    assert rows[1].name == "x;y"
    assert rows[2].connector is None and "not a connector object" in rows[2].problem
    assert rows[3].connector is None and "already named" in rows[3].problem
    assert rows[4].connector is not None


def test_a_saved_entry_that_is_not_usable_cannot_be_switched():
    with pytest.raises(ConnectorError, match="cannot change"):
        cc.set_enabled([stdio(name="x;y")], 0, False)


def test_remove_drops_one_entry_and_refuses_a_missing_index():
    raw = cc.add(cc.add((), stdio(), resolve=RESOLVE), http(), resolve=RESOLVE)
    assert [item["name"] for item in cc.remove(raw, 0)] == ["remote"]
    with pytest.raises(ConnectorError, match="no longer"):
        cc.remove(raw, 5)


def test_remove_can_drop_an_entry_that_is_not_usable():
    assert cc.remove([stdio(name="x;y")], 0) == ()


def test_enable_and_disable_save_the_switch_and_keep_the_rest():
    raw = cc.add((), stdio(), resolve=RESOLVE)
    off = cc.set_enabled(raw, 0, False)
    assert off[0]["enabled"] is False and off[0]["command"] == "notes-cli"
    assert cc.set_enabled(off, 0, True)[0]["enabled"] is True


def test_tunnel_flag_is_stored_and_changes_nothing_else():
    raw = cc.add((), stdio(), resolve=RESOLVE)
    exposed = cc.set_tunnel_exposed(raw, 0, True)
    assert exposed[0]["tunnel_exposed"] is True
    assert exposed[0]["enabled"] is True


# ------------------------------------------------------------------ status

def _row(entry: dict, index: int = 0):
    return cc.read_rows([entry])[index]


def test_status_for_an_enabled_command_that_is_found():
    text = cc.status_text(_row(stdio()), resolve=RESOLVE, tunnel_opt_in=False)
    assert text == "On. Written to Claude Desktop and Codex at the next start."


def test_status_for_a_switched_off_connector():
    text = cc.status_text(_row(stdio(enabled=False)), resolve=RESOLVE, tunnel_opt_in=False)
    assert text.startswith("Off.")


def test_status_for_a_command_that_is_not_found():
    text = cc.status_text(_row(stdio(command="gone-cli")), resolve=NOTHING, tunnel_opt_in=False)
    assert "was not found on this computer" in text


def test_status_for_an_http_connector_says_it_is_not_written_locally():
    text = cc.status_text(_row(http()), resolve=RESOLVE, tunnel_opt_in=False)
    assert "not written to Claude Desktop or Codex" in text


def test_status_for_the_tunnel_flag_depends_on_the_owner_opt_in():
    exposed = _row(stdio(tunnel_exposed=True))
    assert "Tunnel: off, because the tunnel opt-in is off." in cc.status_text(exposed, resolve=RESOLVE, tunnel_opt_in=False)
    assert "does not serve custom connectors yet" in cc.status_text(exposed, resolve=RESOLVE, tunnel_opt_in=True)


def test_status_for_a_saved_entry_that_is_not_usable_gives_the_reason():
    text = cc.status_text(_row(stdio(name="x;y")), resolve=RESOLVE, tunnel_opt_in=False)
    assert text.startswith("Not used:")


# ------------------------------------------------------------------ what the local cache gets

def test_cache_entries_write_each_enabled_found_command_under_the_custom_prefix():
    raw = [stdio(), stdio(name="other", enabled=False)]
    cache = cc.cache_entries(raw, resolve=RESOLVE)
    assert len(cache.specs) == 1
    spec = cache.specs[0]
    assert spec.name == CUSTOM_PREFIX + "notes"
    assert spec.server == "custom:notes"
    assert spec.command == "C:/tools/notes-cli.exe"
    assert spec.args == ("--serve",)
    assert cache.notes == ()


def test_cache_entries_note_an_http_connector_and_a_missing_command():
    raw = [http(), stdio(name="gone", command="gone-cli")]
    cache = cc.cache_entries(raw, resolve=NOTHING)
    assert cache.specs == ()
    assert any("meridian-custom-remote is not written" in note and "stdio" in note for note in cache.notes)
    assert any("gone-cli was not found" in note for note in cache.notes)


def test_a_disabled_entry_is_absent_and_leaves_no_note():
    cache = cc.cache_entries([http(enabled=False), stdio(enabled=False)], resolve=RESOLVE)
    assert cache.specs == () and cache.notes == ()


def test_a_saved_entry_that_is_not_usable_is_noted_and_not_written():
    cache = cc.cache_entries([stdio(name="x;y")], resolve=RESOLVE)
    assert cache.specs == ()
    assert any(note.startswith("A custom connector is not written") for note in cache.notes)


def test_the_tunnel_flag_does_not_change_what_the_local_cache_writes():
    plain = cc.cache_entries([stdio()], resolve=RESOLVE)
    exposed = cc.cache_entries([stdio(tunnel_exposed=True)], resolve=RESOLVE)
    assert plain.specs == exposed.specs


# ------------------------------------------------------------------ the saved settings file

def test_custom_connectors_and_the_tunnel_opt_in_are_not_written_when_they_are_default(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.save(Settings())
    saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert "custom_connectors" not in saved
    assert "custom_tunnel_opt_in" not in saved


def test_connectors_and_the_tunnel_opt_in_round_trip_through_the_store(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    raw = cc.add((), stdio(), resolve=RESOLVE)
    store.save(replace(Settings(), custom_connectors=raw, custom_tunnel_opt_in=True))
    loaded = store.load()
    assert loaded.warning is None
    assert loaded.settings.custom_connectors == raw
    assert loaded.settings.custom_tunnel_opt_in is True


def test_the_tunnel_opt_in_is_off_when_the_file_has_no_key(tmp_path):
    (tmp_path / "settings.json").write_text("{}", encoding="utf-8")
    assert SettingsStore(tmp_path / "settings.json").load().settings.custom_tunnel_opt_in is False


def test_a_tunnel_opt_in_that_is_not_true_or_false_is_read_as_off_with_a_warning(tmp_path):
    (tmp_path / "settings.json").write_text('{"custom_tunnel_opt_in": "yes"}', encoding="utf-8")
    loaded = SettingsStore(tmp_path / "settings.json").load()
    assert loaded.settings.custom_tunnel_opt_in is False
    assert "custom_tunnel_opt_in must be true or false" in loaded.warning


def test_a_connector_list_that_is_not_a_list_of_objects_is_ignored_with_a_warning(tmp_path):
    (tmp_path / "settings.json").write_text('{"custom_connectors": ["notes"]}', encoding="utf-8")
    loaded = SettingsStore(tmp_path / "settings.json").load()
    assert loaded.settings.custom_connectors == ()
    assert "custom_connectors must be a list" in loaded.warning
