"""Tests for the one-time Zotero bridge plugin install (meridian/zotero_plugin_install.py).

Every root is a tmp_path or a monkeypatched value. Nothing here reads the real
Zotero profile, the real Meridian state directory, or the real process list:
subprocess.run and Popen are blocked for the whole file, and the process check
is always injected.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from meridian import zotero_plugin_install as zpi

XPI_NAME = "meridian-zotero@usemeridian.us.xpi"
ENV_AUTOINSTALL = "MERIDIAN_ZOTERO_PLUGIN_AUTOINSTALL"
ENV_DATA_DIR = "MERIDIAN_ZOTERO_DATA_DIR"
REPO_EXTENSION = Path(zpi.__file__).resolve().parent.parent / "extensions" / "zotero-bridge"
EXPECTED_ENTRIES = [
    "bootstrap.js",
    "content/preferences.xhtml",
    "lib/bibkey.js",
    "lib/pdf.js",
    "manifest.json",
    "prefs.js",
]


@pytest.fixture(autouse=True)
def _no_real_processes(monkeypatch):
    def _blocked(*args, **kwargs):
        raise RuntimeError("real process calls are blocked in tests")

    monkeypatch.setattr(zpi.subprocess, "run", _blocked)
    monkeypatch.setattr(zpi.subprocess, "Popen", _blocked)


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    return path


def _fake_source(root: Path) -> Path:
    """A miniature extensions/zotero-bridge tree, including files that must be excluded."""
    src = root / "zotero-bridge"
    _write(src / "manifest.json", '{"manifest_version": 2}')
    _write(src / "bootstrap.js", "// boot")
    _write(src / "prefs.js", 'pref("x", true);')
    _write(src / "content" / "preferences.xhtml", "<xul/>")
    _write(src / "lib" / "bibkey.js", "export {};")
    _write(src / "lib" / "pdf.js", "export {};")
    _write(src / "test" / "bibkey.test.js", "t")
    _write(src / "node_modules" / "left-pad" / "index.js", "x")
    _write(src / "package.json", "{}")
    _write(src / "package-lock.json", "{}")
    _write(src / "README.md", "# readme")
    return src


def _snapshot(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        out[rel] = "<dir>" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
    return out


@pytest.fixture
def profile(tmp_path):
    folder = tmp_path / "Zotero" / "Profiles" / "abc.default"
    folder.mkdir(parents=True)
    _write(folder / "prefs.js", 'user_pref("keep", true);\n')
    _write(folder / "extensions.json", '{"addons": []}')
    return folder


@pytest.fixture
def xpi(tmp_path):
    return zpi.build_xpi(tmp_path / "build" / XPI_NAME, source_dir=_fake_source(tmp_path / "src"))


# ---------------------------------------------------------------------------
# build_xpi
# ---------------------------------------------------------------------------


def test_build_xpi_contains_only_the_runtime_files(tmp_path):
    out = zpi.build_xpi(tmp_path / "out" / XPI_NAME, source_dir=_fake_source(tmp_path / "src"))
    assert out == tmp_path / "out" / XPI_NAME
    with zipfile.ZipFile(out) as zf:
        assert zf.namelist() == EXPECTED_ENTRIES


def test_build_xpi_excludes_tests_node_modules_and_package_metadata(tmp_path):
    out = zpi.build_xpi(tmp_path / XPI_NAME, source_dir=_fake_source(tmp_path / "src"))
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert not any(name.startswith(("test/", "node_modules/")) for name in names)
    for banned in ("package.json", "package-lock.json", "README.md"):
        assert banned not in names


def test_rebuild_is_byte_identical_even_after_source_mtimes_change(tmp_path):
    src = _fake_source(tmp_path / "src")
    first = zpi.build_xpi(tmp_path / "a.xpi", source_dir=src).read_bytes()
    later = 1_900_000_000
    for path in src.rglob("*"):
        if path.is_file():
            os.utime(path, (later, later))
    second = zpi.build_xpi(tmp_path / "b.xpi", source_dir=src).read_bytes()
    assert first == second


def test_zip_entries_use_fixed_timestamps(tmp_path):
    out = zpi.build_xpi(tmp_path / XPI_NAME, source_dir=_fake_source(tmp_path / "src"))
    with zipfile.ZipFile(out) as zf:
        assert {info.date_time for info in zf.infolist()} == {(1980, 1, 1, 0, 0, 0)}


def test_real_extension_builds_with_manifest_bootstrap_and_prefs(tmp_path):
    out = zpi.build_xpi(tmp_path / XPI_NAME, source_dir=REPO_EXTENSION)
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        manifest = json.loads(zf.read("manifest.json"))
    for required in ("manifest.json", "bootstrap.js", "prefs.js"):
        assert required in names
    assert not any(name.startswith(("test/", "node_modules/")) for name in names)
    assert manifest["applications"]["zotero"]["id"] == "meridian-zotero@usemeridian.us"


# ---------------------------------------------------------------------------
# zotero_data_dir (defaults are unverified on a real install; see module docstring)
# ---------------------------------------------------------------------------


def test_data_dir_windows_default_is_under_appdata(monkeypatch, tmp_path):
    monkeypatch.setattr(zpi, "_platform", lambda: "win32")
    env = {"APPDATA": str(tmp_path / "Roaming")}
    assert zpi.zotero_data_dir(env=env) == tmp_path / "Roaming" / "Zotero" / "Zotero"


def test_data_dir_windows_without_appdata_is_none(monkeypatch):
    monkeypatch.setattr(zpi, "_platform", lambda: "win32")
    assert zpi.zotero_data_dir(env={}) is None


def test_data_dir_macos_default(monkeypatch, tmp_path):
    monkeypatch.setattr(zpi, "_platform", lambda: "darwin")
    expected = tmp_path / "Library" / "Application Support" / "Zotero"
    assert zpi.zotero_data_dir(env={}, home=tmp_path) == expected


def test_data_dir_linux_default(monkeypatch, tmp_path):
    monkeypatch.setattr(zpi, "_platform", lambda: "linux")
    assert zpi.zotero_data_dir(env={}, home=tmp_path) == tmp_path / ".zotero" / "zotero"


def test_data_dir_env_override_wins_on_every_platform(monkeypatch, tmp_path):
    monkeypatch.setattr(zpi, "_platform", lambda: "win32")
    env = {"APPDATA": str(tmp_path / "Roaming"), ENV_DATA_DIR: str(tmp_path / "custom")}
    assert zpi.zotero_data_dir(env=env) == tmp_path / "custom"


# ---------------------------------------------------------------------------
# find_default_profile_dir (profiles.ini)
# ---------------------------------------------------------------------------


def test_install_default_section_is_used_first_and_resolved_relative(tmp_path):
    root = tmp_path / "Zotero"
    (root / "Profiles" / "abc.default").mkdir(parents=True)
    _write(
        root / "profiles.ini",
        "[Install4F96D1932A9F858E]\nDefault=Profiles/abc.default\nLocked=1\n\n"
        "[Profile0]\nName=default\nIsRelative=1\nPath=Profiles/other\nDefault=1\n",
    )
    assert zpi.find_default_profile_dir(root) == root / "Profiles" / "abc.default"


def test_profile_flagged_default_resolves_relative_when_is_relative_is_one(tmp_path):
    root = tmp_path / "Zotero"
    (root / "Profiles" / "xyz.default-release").mkdir(parents=True)
    _write(
        root / "profiles.ini",
        "[General]\nStartWithLastProfile=1\n\n"
        "[Profile0]\nName=old\nIsRelative=1\nPath=Profiles/old\n\n"
        "[Profile1]\nName=release\nIsRelative=1\nPath=Profiles/xyz.default-release\nDefault=1\n",
    )
    assert zpi.find_default_profile_dir(root) == root / "Profiles" / "xyz.default-release"


def test_absolute_profile_path_is_used_as_written_when_is_relative_is_zero(tmp_path):
    root = tmp_path / "Zotero"
    root.mkdir()
    absolute = tmp_path / "elsewhere" / "p.default"
    absolute.mkdir(parents=True)
    _write(root / "profiles.ini", f"[Profile0]\nName=default\nIsRelative=0\nPath={absolute}\nDefault=1\n")
    assert zpi.find_default_profile_dir(root) == absolute


def test_missing_profiles_ini_returns_none(tmp_path):
    root = tmp_path / "Zotero"
    root.mkdir()
    assert zpi.find_default_profile_dir(root) is None
    assert zpi.find_default_profile_dir(tmp_path / "does-not-exist") is None
    assert zpi.find_default_profile_dir(None) is None


def test_no_default_flag_returns_none_instead_of_guessing(tmp_path):
    root = tmp_path / "Zotero"
    (root / "Profiles" / "a").mkdir(parents=True)
    _write(
        root / "profiles.ini",
        "[Profile0]\nName=a\nIsRelative=1\nPath=Profiles/a\n\n"
        "[Profile1]\nName=b\nIsRelative=1\nPath=Profiles/b\nDefault=0\n",
    )
    assert zpi.find_default_profile_dir(root) is None


def test_install_default_pointing_at_a_missing_folder_falls_back_to_the_flagged_profile(tmp_path):
    root = tmp_path / "Zotero"
    (root / "Profiles" / "live.default").mkdir(parents=True)
    _write(
        root / "profiles.ini",
        "[Install4F96D1932A9F858E]\nDefault=Profiles/gone.default\n\n"
        "[Profile0]\nName=live\nIsRelative=1\nPath=Profiles/live.default\nDefault=1\n",
    )
    assert zpi.find_default_profile_dir(root) == root / "Profiles" / "live.default"


# ---------------------------------------------------------------------------
# zotero_is_running (injected runner; the real process list is never called)
# ---------------------------------------------------------------------------


def test_running_check_on_windows_parses_tasklist_output(monkeypatch):
    monkeypatch.setattr(zpi, "_platform", lambda: "win32")
    assert zpi.zotero_is_running(runner=lambda: "zotero.exe     1234 Console    1   100,000 K\n") is True
    assert zpi.zotero_is_running(runner=lambda: "INFO: No tasks are running which match the criteria.\n") is False


def test_running_check_on_posix_parses_pgrep_output(monkeypatch):
    monkeypatch.setattr(zpi, "_platform", lambda: "linux")
    assert zpi.zotero_is_running(runner=lambda: "4321\n") is True
    assert zpi.zotero_is_running(runner=lambda: "") is False


def test_running_check_fails_closed_when_the_check_errors(monkeypatch):
    def _broken():
        raise OSError("tasklist missing")

    monkeypatch.setattr(zpi, "_platform", lambda: "win32")
    assert zpi.zotero_is_running(runner=_broken) is True
    monkeypatch.setattr(zpi, "_platform", lambda: "linux")
    assert zpi.zotero_is_running(runner=_broken) is True


def test_default_windows_runner_filters_tasklist_on_the_zotero_image(monkeypatch):
    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(zpi.subprocess, "run", _fake_run)
    monkeypatch.setattr(zpi, "_platform", lambda: "win32")
    assert zpi.zotero_is_running() is False
    assert calls == [["tasklist", "/FI", "IMAGENAME eq zotero.exe", "/NH"]]


def test_default_posix_runner_asks_pgrep_for_the_exact_zotero_name(monkeypatch):
    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="77\n", stderr="")

    monkeypatch.setattr(zpi.subprocess, "run", _fake_run)
    monkeypatch.setattr(zpi, "_platform", lambda: "linux")
    assert zpi.zotero_is_running() is True
    assert calls == [["pgrep", "-x", "zotero"]]


# ---------------------------------------------------------------------------
# install_into_profile
# ---------------------------------------------------------------------------


def test_install_refuses_while_zotero_is_running_and_writes_nothing(profile, xpi):
    before = _snapshot(profile)
    result = zpi.install_into_profile(profile, xpi, running=lambda: True)
    assert result["status"] == "refused_running"
    assert _snapshot(profile) == before
    assert not (profile / "extensions").exists()


def test_install_copies_the_xpi_and_leaves_no_temp_file(profile, xpi):
    result = zpi.install_into_profile(profile, xpi, running=lambda: False)
    target = profile / "extensions" / XPI_NAME
    assert result["status"] == "installed"
    assert target.read_bytes() == xpi.read_bytes()
    assert sorted(p.name for p in (profile / "extensions").iterdir()) == [XPI_NAME]


def test_second_install_reports_unchanged_and_does_not_rewrite(monkeypatch, profile, xpi):
    assert zpi.install_into_profile(profile, xpi, running=lambda: False)["status"] == "installed"

    def _must_not_write(target, payload):
        raise AssertionError("an unchanged install must not rewrite the file")

    monkeypatch.setattr(zpi, "_atomic_write_bytes", _must_not_write)
    assert zpi.install_into_profile(profile, xpi, running=lambda: False)["status"] == "unchanged"


def test_install_never_touches_extensions_json_prefs_or_anything_else(profile, xpi):
    before = _snapshot(profile)
    zpi.install_into_profile(profile, xpi, running=lambda: False)
    after = _snapshot(profile)
    assert after["extensions.json"] == before["extensions.json"]
    assert after["prefs.js"] == before["prefs.js"]
    for rel, digest in before.items():
        assert after[rel] == digest
    assert set(after) - set(before) == {"extensions", f"extensions/{XPI_NAME}"}


def test_install_with_no_profile_reports_no_profile_and_creates_nothing(tmp_path, xpi):
    assert zpi.install_into_profile(None, xpi, running=lambda: False)["status"] == "no_profile"
    missing = tmp_path / "missing"
    assert zpi.install_into_profile(missing, xpi, running=lambda: False)["status"] == "no_profile"
    assert not missing.exists()


def test_install_reports_failed_when_the_source_is_missing_and_writes_nothing(profile, tmp_path):
    result = zpi.install_into_profile(profile, tmp_path / "nope.xpi", running=lambda: False)
    assert result["status"] == "failed"
    assert not (profile / "extensions").exists()


# ---------------------------------------------------------------------------
# settings file
# ---------------------------------------------------------------------------


def test_settings_live_in_the_meridian_per_user_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("MERIDIAN_LOCAL_RUNNER_STATE_DIR", str(tmp_path / "state"))
    assert zpi.settings_path() == tmp_path / "state" / "zotero_plugin.json"


def test_settings_defaults_when_the_file_is_missing(tmp_path):
    assert zpi.load_settings(tmp_path / "zotero_plugin.json") == {"autoinstall": True, "decided": False}


def test_settings_defaults_when_the_file_is_corrupt_or_wrong_shape(tmp_path):
    for index, text in enumerate(("{not json", "[1, 2]", '{"autoinstall": "yes", "decided": 1}')):
        path = _write(tmp_path / f"s{index}.json", text)
        assert zpi.load_settings(path) == {"autoinstall": True, "decided": False}


def test_settings_round_trip_creates_the_folder_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "state" / "zotero_plugin.json"
    zpi.save_settings(path, {"autoinstall": False, "decided": True})
    assert zpi.load_settings(path) == {"autoinstall": False, "decided": True}
    assert json.loads(path.read_text(encoding="utf-8")) == {"autoinstall": False, "decided": True}
    assert sorted(p.name for p in path.parent.iterdir()) == ["zotero_plugin.json"]


def test_first_run_install_choice_enables_autoinstall_and_marks_decided():
    original = {"autoinstall": False, "decided": False}
    updated = zpi.apply_first_run_choice(original, True)
    assert updated == {"autoinstall": True, "decided": True}
    assert original == {"autoinstall": False, "decided": False}


def test_first_run_not_now_choice_disables_autoinstall_and_marks_decided():
    updated = zpi.apply_first_run_choice({"autoinstall": True, "decided": False}, False)
    assert updated == {"autoinstall": False, "decided": True}


# ---------------------------------------------------------------------------
# effective_autoinstall (env var precedence)
# ---------------------------------------------------------------------------

_ON = {"autoinstall": True, "decided": True}
_OFF = {"autoinstall": False, "decided": True}


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "OFF", " False "])
def test_env_false_values_force_autoinstall_off(raw):
    assert zpi.effective_autoinstall(_ON, {ENV_AUTOINSTALL: raw}) is False


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "ON"])
def test_env_true_values_force_autoinstall_on(raw):
    assert zpi.effective_autoinstall(_OFF, {ENV_AUTOINSTALL: raw}) is True


def test_unset_env_uses_the_saved_setting():
    assert zpi.effective_autoinstall(_ON, {}) is True
    assert zpi.effective_autoinstall(_OFF, {}) is False


@pytest.mark.parametrize("raw", ["", "   ", "maybe"])
def test_blank_or_unknown_env_falls_back_to_the_saved_setting(raw):
    assert zpi.effective_autoinstall(_ON, {ENV_AUTOINSTALL: raw}) is True
    assert zpi.effective_autoinstall(_OFF, {ENV_AUTOINSTALL: raw}) is False


# ---------------------------------------------------------------------------
# CLI: python -m meridian zotero install-plugin [--profile DIR] [--dry-run]
# ---------------------------------------------------------------------------


def test_cli_dry_run_prints_the_plan_and_writes_nothing(monkeypatch, capsys, profile):
    monkeypatch.setattr(zpi, "zotero_is_running", lambda runner=None: False)
    before = _snapshot(profile)
    rc = zpi.cli_main(["--profile", str(profile), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry run" in out.lower()
    assert str(profile) in out
    assert XPI_NAME in out
    assert _snapshot(profile) == before
    assert not zpi.default_xpi_path().exists()


def test_cli_installs_into_the_given_profile(monkeypatch, capsys, profile):
    monkeypatch.setattr(zpi, "zotero_is_running", lambda runner=None: False)
    rc = zpi.cli_main(["--profile", str(profile)])
    assert rc == 0
    assert (profile / "extensions" / XPI_NAME).read_bytes() == zpi.build_xpi_bytes()
    assert "installed" in capsys.readouterr().out.lower()


def test_cli_refuses_with_a_clear_message_while_zotero_runs(monkeypatch, capsys, profile):
    monkeypatch.setattr(zpi, "zotero_is_running", lambda runner=None: True)
    rc = zpi.cli_main(["--profile", str(profile)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "zotero is running" in (captured.out + captured.err).lower()
    assert not (profile / "extensions").exists()


def test_cli_prints_manual_steps_when_no_profile_is_found(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(zpi, "zotero_is_running", lambda runner=None: False)
    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path / "empty-zotero-root"))
    rc = zpi.cli_main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Install Add-on From File" in out
    assert XPI_NAME in out


def test_dispatch_routes_zotero_install_plugin_to_the_installer(monkeypatch, capsys, profile):
    from meridian import __main__ as entry

    monkeypatch.setattr(zpi, "zotero_is_running", lambda runner=None: False)
    rc = entry._dispatch_subcommand(["zotero", "install-plugin", "--profile", str(profile), "--dry-run"])
    assert rc == 0
    assert "dry run" in capsys.readouterr().out.lower()
