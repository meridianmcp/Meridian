"""The local write API's token file and the profile prefs that point the plugin at it.

The token value is a credential. These tests check its shape and that it is kept, but they never print it, and
they never compare it to a literal. Every path is in a temporary folder.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from meridian import zotero_bridge_api as bridge
from meridian import zotero_plugin_install as zpi


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("MERIDIAN_LOCAL_RUNNER_STATE_DIR", str(tmp_path / "state"))


def test_the_token_file_lives_under_the_local_runner_state_folder(tmp_path):
    assert bridge.token_file_path() == tmp_path / "state" / "zotero_bridge" / "api_token"


def test_a_new_token_has_the_meridian_prefix_and_passes_the_plugins_shape_check(tmp_path):
    path = tmp_path / "bridge" / "api_token"
    assert bridge.ensure_api_token(path) == bridge.TOKEN_CREATED
    text = path.read_text(encoding="utf-8").strip()
    assert text.startswith(bridge.TOKEN_PREFIX)
    assert len(text) >= 40
    assert bridge._usable(text)


def test_an_existing_usable_token_is_kept_and_never_rotated(tmp_path):
    path = tmp_path / "api_token"
    bridge.ensure_api_token(path)
    before = path.read_bytes()
    assert bridge.ensure_api_token(path) == bridge.TOKEN_KEPT
    assert path.read_bytes() == before


@pytest.mark.parametrize("content", ["", "short\n", "has spaces in it " * 3, "path/like/value/" + "a" * 40])
def test_an_empty_or_malformed_token_file_is_replaced_with_a_usable_one(tmp_path, content):
    path = tmp_path / "api_token"
    path.write_text(content, encoding="utf-8")
    assert bridge.ensure_api_token(path) == bridge.TOKEN_CREATED
    assert bridge._usable(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "payload",
    [
        ("sk_meridian_" + "a" * 40 + "\n").encode("utf-16"),  # UTF-16 with a byte-order mark
        ("sk_meridian_" + "a" * 40 + "\n").encode("utf-16-le"),  # UTF-16 without one: NUL bytes are valid UTF-8
        b"\x81\x8d\x8f\x90\x9d",  # bytes that are not UTF-8 at all
    ],
    ids=["utf16_with_bom", "utf16_without_bom", "not_utf8"],
)
def test_an_undecodable_token_file_is_replaced_and_its_error_never_escapes(tmp_path, payload):
    path = tmp_path / "api_token"
    path.write_bytes(payload)
    assert bridge.ensure_api_token(path) == bridge.TOKEN_CREATED
    assert path.read_bytes() != payload
    assert bridge._usable(path.read_text(encoding="utf-8"))


def test_a_token_file_with_a_byte_order_mark_is_kept_and_never_rotated(tmp_path):
    path = tmp_path / "api_token"
    bridge.ensure_api_token(path)
    original = path.read_bytes()
    path.write_bytes(b"\xef\xbb\xbf" + original)
    assert bridge.ensure_api_token(path) == bridge.TOKEN_KEPT
    assert path.read_bytes() == b"\xef\xbb\xbf" + original


def test_usable_ignores_surrounding_whitespace_and_byte_order_marks_but_nothing_else():
    token = bridge.TOKEN_PREFIX + "x" * 40
    assert bridge._usable("﻿ \n" + token + "\r\n")
    assert not bridge._usable(token + " tail")


def test_the_token_file_is_written_through_a_temporary_file_that_is_then_removed(tmp_path):
    path = tmp_path / "api_token"
    bridge.ensure_api_token(path)
    assert not (tmp_path / "api_token.tmp").exists()


def test_the_pref_lines_escape_a_windows_path_so_the_profile_reads_it_back_exactly():
    token_path = r"D:\state\zotero_bridge\api_token"
    port_line, path_line = bridge.api_pref_lines(token_path, 23125)
    assert port_line == 'user_pref("extensions.zotero.meridianZoteroBridge.apiPort", 23125);'
    literal = re.search(r'"extensions\.zotero\.meridianZoteroBridge\.apiTokenPath", (".*")\);$', path_line).group(1)
    assert json.loads(literal) == token_path


def test_the_prefs_are_added_once_and_every_other_user_js_line_is_left_alone(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    original = 'user_pref("browser.example", true);\nuser_pref("extensions.startupScanScopes", 1);\n'
    (profile / "user.js").write_text(original, encoding="utf-8")
    token_path = tmp_path / "api_token"

    assert bridge.ensure_api_prefs(profile, token_path) == bridge.PREFS_ADDED
    text = (profile / "user.js").read_text(encoding="utf-8")
    assert text.startswith(original)
    assert text.count("apiPort") == 1
    assert text.count("apiTokenPath") == 1

    assert bridge.ensure_api_prefs(profile, token_path) == bridge.PREFS_PRESENT
    assert (profile / "user.js").read_text(encoding="utf-8") == text


def test_a_changed_token_path_updates_its_line_instead_of_adding_a_second(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    bridge.ensure_api_prefs(profile, tmp_path / "first" / "api_token")
    assert bridge.ensure_api_prefs(profile, tmp_path / "second" / "api_token") == bridge.PREFS_UPDATED
    text = (profile / "user.js").read_text(encoding="utf-8")
    assert text.count("apiTokenPath") == 1
    assert "second" in text and "first" not in text


def test_a_profile_without_a_user_js_gets_one(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    assert bridge.ensure_api_prefs(profile, tmp_path / "api_token") == bridge.PREFS_ADDED
    assert (profile / "user.js").is_file()


def test_a_crlf_user_js_keeps_its_line_endings(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "user.js").write_bytes(b'user_pref("browser.example", true);\r\n')
    bridge.ensure_api_prefs(profile, tmp_path / "api_token")
    raw = (profile / "user.js").read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n") == 3


def test_an_lf_user_js_gets_lf_lines_and_no_carriage_return(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "user.js").write_bytes(b'user_pref("browser.example", true);\n')
    bridge.ensure_api_prefs(profile, tmp_path / "api_token")
    assert b"\r" not in (profile / "user.js").read_bytes()


def test_a_windows_1252_user_js_keeps_its_non_ascii_bytes_after_the_prefs_are_written(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    # Windows-1252 bytes, including 0x85 (an ellipsis): a text-mode or str.splitlines() split would break on it.
    original = 'user_pref("browser.example", "café — “quoted” …");\r\n'.encode("cp1252")
    (profile / "user.js").write_bytes(original)

    assert bridge.ensure_api_prefs(profile, tmp_path / "api_token") == bridge.PREFS_ADDED

    raw = (profile / "user.js").read_bytes()
    assert raw.startswith(original)
    assert raw.count(b"\x85") == 1
    assert b"caf\xe9" in raw
    added = raw[len(original):]
    assert added.count(b"\r\n") == 2 and added.count(b"\n") == 2


def test_the_install_writes_the_token_then_the_prefs_into_the_isolated_state(tmp_path):
    profile = tmp_path / "profile"
    (profile / "extensions").mkdir(parents=True)
    (profile / "prefs.js").write_text("", encoding="utf-8")
    payload = zpi.build_xpi_bytes()
    signed = tmp_path / "signed.xpi"
    signed.write_bytes(payload)

    result = zpi._install_files(profile, signed, lambda: False, zpi._sha256(payload))

    assert result["status"] == "installed"
    assert result["api_prefs"] == bridge.PREFS_ADDED
    token_file = bridge.token_file_path()
    assert token_file.is_file()
    assert bridge._usable(token_file.read_text(encoding="utf-8"))
    assert "apiTokenPath" in (profile / "user.js").read_text(encoding="utf-8")
    assert bridge.TOKEN_PREFIX not in json.dumps(result), "the install result never carries the token"


def test_a_token_file_that_cannot_be_written_fails_the_install_and_says_why(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    (profile / "extensions").mkdir(parents=True)
    (profile / "prefs.js").write_text("", encoding="utf-8")
    payload = zpi.build_xpi_bytes()
    signed = tmp_path / "signed.xpi"
    signed.write_bytes(payload)

    def _denied(path):
        raise PermissionError("denied by the test")

    monkeypatch.setattr(bridge, "ensure_api_token", _denied)
    result = zpi._install_files(profile, signed, lambda: False, zpi._sha256(payload))
    assert result["status"] == "failed"
    assert "local write API" in result["error"]
    assert "denied by the test" in result["error"]
