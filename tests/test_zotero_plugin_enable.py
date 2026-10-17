"""Registering and enabling the plugin without a click: the scan line in user.js, the registry change, the order of steps.

Nothing here starts Zotero, closes Zotero, or reaches Mozilla. Zotero's behaviour is simulated by the fakes below;
the real behaviour was checked by hand in throwaway profiles (see the commit that added this module).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from meridian import zotero_plugin_install as zpi
from meridian import zotero_plugin_sign as sign

PAYLOAD = b"stand-in signed bytes"
SHA = hashlib.sha256(PAYLOAD).hexdigest()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Meridian's state lives in tmp_path; Zotero is never started, closed, or asked for a file."""
    monkeypatch.setenv("MERIDIAN_LOCAL_RUNNER_STATE_DIR", str(tmp_path / "meridian-state"))
    monkeypatch.setattr(zpi, "_start_zotero_after_close", lambda: "started")
    monkeypatch.setattr(zpi, "_fetch_approved_if_missing", lambda: None)


@pytest.fixture
def profile(tmp_path) -> Path:
    folder = tmp_path / "profile"
    folder.mkdir()
    (folder / "prefs.js").write_text("// zotero profile\n", encoding="utf-8")
    return folder


@pytest.fixture
def signed(tmp_path) -> Path:
    path = tmp_path / "signed.xpi"
    path.write_bytes(PAYLOAD)
    return path


def _ours(**overrides) -> dict:
    entry = {
        "id": zpi.ADDON_ID,
        "version": "0.1.2",
        "userDisabled": True,
        "active": False,
        "appDisabled": False,
        "softDisabled": False,
        "blocklistState": 0,
        "location": "app-profile",
    }
    entry.update(overrides)
    return entry


def _other(**overrides) -> dict:
    entry = {"id": "someone-else@example.org", "userDisabled": True, "active": False, "location": "app-profile"}
    entry.update(overrides)
    return entry


def _write_registry(profile: Path, *entries: dict) -> None:
    (profile / "extensions.json").write_text(
        json.dumps({"schemaVersion": 37, "addons": list(entries)}, indent=2), encoding="utf-8",
    )


def _registry(profile: Path) -> dict:
    return json.loads((profile / "extensions.json").read_text(encoding="utf-8"))


def _entry(profile: Path, addon_id: str = zpi.ADDON_ID) -> dict:
    return next(item for item in _registry(profile)["addons"] if item["id"] == addon_id)


def _no_wait_registration(profile, *, launch, close, running=lambda: False, timeout_s=0.0):
    return zpi.register_and_enable(
        profile, launch=launch, close=close, running=running, timeout_s=timeout_s,
        sleep=lambda _s: None, clock=lambda: 0.0,
    )


# --- the scan line in user.js ---------------------------------------------------------------

def test_the_addon_id_is_the_signing_id():
    assert zpi.ADDON_ID == sign.PLUGIN_ADDON_ID


def test_the_scan_line_is_added_and_every_other_line_is_kept(profile):
    (profile / "user.js").write_text('user_pref("browser.startup.homepage", "about:blank");\n', encoding="utf-8")
    assert zpi.ensure_scan_pref(profile) == "added"
    text = (profile / "user.js").read_text(encoding="utf-8")
    assert text.startswith('user_pref("browser.startup.homepage", "about:blank");\n')
    assert text.endswith(zpi.SCAN_PREF_LINE + "\n")


def test_a_missing_user_js_is_created_with_only_the_scan_line(profile):
    assert zpi.ensure_scan_pref(profile) == "added"
    assert (profile / "user.js").read_text(encoding="utf-8") == zpi.SCAN_PREF_LINE + "\n"


def test_a_second_ensure_writes_nothing(profile):
    zpi.ensure_scan_pref(profile)
    before = (profile / "user.js").read_bytes()
    assert zpi.ensure_scan_pref(profile) == "present"
    assert (profile / "user.js").read_bytes() == before


def test_a_conflicting_scan_value_is_refused_and_left_alone(profile):
    original = 'user_pref("extensions.startupScanScopes", 0);\n'
    (profile / "user.js").write_text(original, encoding="utf-8")
    assert zpi.check_scan_pref(profile) == "conflict"
    assert zpi.ensure_scan_pref(profile) == "conflict"
    assert (profile / "user.js").read_text(encoding="utf-8") == original


def test_a_commented_out_scan_line_is_not_a_setting_so_the_active_line_is_added(profile):
    original = '// user_pref("extensions.startupScanScopes", 1);\n'
    (profile / "user.js").write_bytes(original.encode("ascii"))
    assert zpi.check_scan_pref(profile) == "added"
    assert zpi.ensure_scan_pref(profile) == "added"
    raw = (profile / "user.js").read_text(encoding="utf-8")
    assert raw.startswith(original)
    active = [line for line in raw.splitlines() if not line.lstrip().startswith("//")]
    assert active == [zpi.SCAN_PREF_LINE]
    assert zpi.check_scan_pref(profile) == "present"


def test_a_commented_out_conflicting_value_is_ignored_and_the_active_one_counts(profile):
    commented = '// user_pref("extensions.startupScanScopes", 0);\n'
    (profile / "user.js").write_bytes((commented + zpi.SCAN_PREF_LINE + "\n").encode("ascii"))
    assert zpi.check_scan_pref(profile) == "present"


def test_a_last_line_without_a_newline_is_terminated_before_the_scan_line_is_added(profile):
    (profile / "user.js").write_bytes(b'user_pref("a", 1);')
    assert zpi.ensure_scan_pref(profile) == "added"
    assert (profile / "user.js").read_bytes() == b'user_pref("a", 1);\n' + zpi.SCAN_PREF_LINE.encode("ascii") + b"\n"


def test_a_windows_1252_user_js_keeps_its_bytes_when_the_scan_line_is_added(profile):
    original = 'user_pref("browser.example", "café …");\r\n'.encode("cp1252")
    (profile / "user.js").write_bytes(original)
    assert zpi.ensure_scan_pref(profile) == "added"
    assert (profile / "user.js").read_bytes() == original + (zpi.SCAN_PREF_LINE + "\r\n").encode("ascii")


def test_an_unreadable_user_js_is_a_refusal_state_and_the_check_never_raises(profile):
    (profile / "user.js").mkdir()  # the file's name is taken by a folder: it exists, and it cannot be read as a file
    assert zpi.check_scan_pref(profile) == "unreadable"
    assert zpi.ensure_scan_pref(profile) == "unreadable"
    assert (profile / "user.js").is_dir()


# --- the registry: one entry, enabled, nothing else changed ----------------------------------

def test_enabling_changes_only_our_entry_in_extensions_json(profile):
    _write_registry(profile, _other(), _ours())
    before = _registry(profile)
    result = _no_wait_registration(
        profile, launch=lambda: pytest.fail("Zotero is already registered; it must not be launched"),
        close=lambda running=None: pytest.fail("nothing to close when nothing was launched"),
    )
    assert result["status"] == "enabled"
    after = _registry(profile)
    assert _entry(profile, "someone-else@example.org") == before["addons"][0]
    assert _entry(profile)["userDisabled"] is False
    assert _entry(profile)["active"] is True
    untouched = {key: value for key, value in _entry(profile).items() if key not in ("userDisabled", "active")}
    original = {key: value for key, value in before["addons"][1].items() if key not in ("userDisabled", "active")}
    assert untouched == original
    assert after["schemaVersion"] == before["schemaVersion"]


def test_an_app_disabled_or_blocked_entry_is_not_enabled(profile):
    _write_registry(profile, _ours(appDisabled=True))
    result = _no_wait_registration(profile, launch=lambda: None, close=lambda running=None: "closed")
    assert result["status"] == "blocked"
    assert _entry(profile)["userDisabled"] is True


def test_an_unreadable_registry_is_reported_and_left_alone(profile):
    (profile / "extensions.json").write_text("{not json", encoding="utf-8")
    result = _no_wait_registration(profile, launch=lambda: None, close=lambda running=None: "closed")
    assert result["status"] == "registry_unreadable"
    assert (profile / "extensions.json").read_text(encoding="utf-8") == "{not json"


# --- registration: launch, wait, close, then enable ---------------------------------------------

def test_an_unregistered_plugin_is_registered_by_a_launch_then_enabled(profile):
    calls: list[str] = []

    def launch():
        calls.append("launch")
        _write_registry(profile, _ours())  # what Zotero writes when it registers the new file
        return "started"

    def close(running=None):
        calls.append("close")
        return "closed"

    result = _no_wait_registration(profile, launch=launch, close=close)
    assert result["status"] == "enabled"
    assert calls == ["launch", "close"]
    assert _entry(profile)["userDisabled"] is False


def test_a_registration_that_never_appears_enables_nothing(profile):
    calls: list[str] = []
    result = _no_wait_registration(
        profile, launch=lambda: calls.append("launch"), close=lambda running=None: calls.append("close") or "closed",
    )
    assert result["status"] == "register_timeout"
    assert calls == ["launch", "close"]
    assert not (profile / "extensions.json").exists()


def test_zotero_that_does_not_close_after_registration_enables_nothing(profile):
    def launch():
        _write_registry(profile, _ours())

    result = _no_wait_registration(profile, launch=launch, close=lambda running=None: "close_failed")
    assert result["status"] == "close_failed"
    assert _entry(profile)["userDisabled"] is True


# --- respecting a user who turned the plugin off ----------------------------------------------

def test_a_plugin_the_user_disables_after_meridian_enabled_it_stays_disabled(profile):
    _write_registry(profile, _ours())
    assert _no_wait_registration(profile, launch=lambda: None, close=lambda running=None: "closed")["status"] == "enabled"
    _write_registry(profile, _ours(userDisabled=True, active=False))  # the user turns it off in Zotero
    before = (profile / "extensions.json").read_bytes()
    result = _no_wait_registration(profile, launch=lambda: None, close=lambda running=None: "closed")
    assert result["status"] == "left_disabled"
    assert (profile / "extensions.json").read_bytes() == before


def test_an_already_enabled_plugin_is_recorded_so_a_later_user_disable_is_respected(profile):
    _write_registry(profile, _ours(userDisabled=False, active=True))
    assert _no_wait_registration(profile, launch=lambda: None, close=lambda running=None: "closed")["status"] == "already_enabled"
    _write_registry(profile, _ours(userDisabled=True, active=False))
    assert _no_wait_registration(profile, launch=lambda: None, close=lambda running=None: "closed")["status"] == "left_disabled"


# --- the install's order, through the real install functions --------------------------------------

def test_the_tray_install_writes_the_file_then_registers_then_restarts(monkeypatch, profile, signed):
    calls: list[str] = []
    states = iter([True, False, False, False])  # Zotero runs at the start, and is closed after the close step

    def running():
        return next(states, False)

    def close(running=None):
        calls.append("close")
        return "closed"

    def launch():
        calls.append("launch")
        _write_registry(profile, _ours())
        return "started"

    def restart():
        calls.append("restart")
        return "started"

    monkeypatch.setattr(zpi, "_prepare_default_install", lambda: (profile, signed, SHA))
    result = zpi.install_default_with_close(
        {"close_zotero_for_install": True}, running=running, close=close, restart=restart, launch=launch,
        register_timeout_s=0.0,
    )
    assert result["status"] == "installed"
    assert result["enable"]["status"] == "enabled"
    assert result["restart"] == "started"
    assert calls == ["close", "launch", "close", "restart"]
    assert (profile / "extensions" / zpi.XPI_FILENAME).read_bytes() == PAYLOAD
    user_js = (profile / "user.js").read_text(encoding="utf-8")
    assert user_js.startswith(zpi.SCAN_PREF_LINE + "\n")
    assert user_js.count("apiTokenPath") == 1
    assert _entry(profile)["userDisabled"] is False


def test_a_conflicting_user_js_is_refused_before_anything_closes(monkeypatch, profile, signed):
    (profile / "user.js").write_text(zpi.SCAN_PREF_LINE.replace("1", "0") + "\n", encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(zpi, "_prepare_default_install", lambda: (profile, signed, SHA))
    result = zpi.install_default_with_close(
        {"close_zotero_for_install": True},
        running=lambda: True,
        close=lambda running=None: calls.append("close") or "closed",
        restart=lambda: calls.append("restart") or "started",
    )
    assert result["status"] == zpi.PREF_CONFLICT_STATUS
    assert calls == []
    assert not (profile / "extensions" / zpi.XPI_FILENAME).exists()


def test_an_unreadable_user_js_is_refused_before_anything_closes(monkeypatch, profile, signed):
    (profile / "user.js").mkdir()
    calls: list[str] = []
    monkeypatch.setattr(zpi, "_prepare_default_install", lambda: (profile, signed, SHA))
    result = zpi.install_default_with_close(
        {"close_zotero_for_install": True},
        running=lambda: True,
        close=lambda running=None: calls.append("close") or "closed",
        restart=lambda: calls.append("restart") or "started",
    )
    assert result["status"] == zpi.PREF_CONFLICT_STATUS
    assert "cannot be read" in result["message"]
    assert result["closed_zotero"] is False
    assert calls == []
    assert not (profile / "extensions" / zpi.XPI_FILENAME).exists()


def test_the_scan_line_is_written_only_after_the_file_is_in_place(profile, signed):
    result = zpi._install_files(profile, signed, lambda: False, SHA)
    assert result["status"] == "installed"
    assert result["scan_pref"] == "added"
    assert (profile / "extensions" / zpi.XPI_FILENAME).is_file()
    assert zpi.check_scan_pref(profile) == "present"
