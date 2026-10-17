"""Tests for the one-time Zotero bridge plugin install (meridian/zotero_plugin_install.py).

Every root is a tmp_path or a monkeypatched value. Nothing here reads the real
Zotero profile, the real Meridian state directory, or the real process list:
subprocess.run and Popen are blocked for the whole file, and the process check
is always injected.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from meridian import zotero_bridge_api as bridge
from meridian import zotero_plugin_install as zpi

XPI_NAME = "meridian-zotero@usemeridian.us.xpi"
ENV_AUTOINSTALL = "MERIDIAN_ZOTERO_PLUGIN_AUTOINSTALL"
ENV_DATA_DIR = "MERIDIAN_ZOTERO_DATA_DIR"
REPO_ROOT = Path(zpi.__file__).resolve().parent.parent
REPO_EXTENSION = REPO_ROOT / "extensions" / "zotero-bridge"
SPEC_PATH = REPO_ROOT / "meridian-tray.spec"
EXPECTED_ENTRIES = [
    "api/listener.js",
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
    _write(src / "manifest.json", '{"manifest_version": 2, "version": "0.1.0"}')
    _write(src / "bootstrap.js", "// boot")
    _write(src / "prefs.js", 'pref("x", true);')
    _write(src / "api" / "listener.js", "// listener")
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


@pytest.fixture(autouse=True)
def _isolated_meridian_state(monkeypatch, tmp_path):
    """Every default Meridian path (the bundle, the signed artifacts, the settings) lives in tmp_path."""
    monkeypatch.setenv("MERIDIAN_LOCAL_RUNNER_STATE_DIR", str(tmp_path / "meridian-state"))


@pytest.fixture(autouse=True)
def _no_fetch_no_registration(monkeypatch):
    """No test asks Mozilla for a file, or runs the registration step, which starts and closes Zotero.

    The registration step is covered by test_zotero_plugin_enable.py. These tests check the install around it, so the
    step is stubbed to a neutral result here. The restart tests keep the real launcher start path.
    """
    monkeypatch.setattr(zpi, "_fetch_approved_if_missing", lambda: None)
    monkeypatch.setattr(zpi, "_register_default", lambda *args, **kwargs: {"status": "not_exercised", "message": ""})


def _seed_signed(state_dir: Path, source: Path, payload: bytes) -> Path:
    """Store ``payload`` as the signed artifact for ``source``, through the real record path. No Mozilla involved."""
    from meridian import zotero_plugin_sign as sign

    state_dir.mkdir(parents=True, exist_ok=True)
    produced = state_dir / "stand-in-produced.xpi"
    produced.write_bytes(payload)
    try:
        return sign.record_signed_artifact(state_dir, produced, source_dir=source, clock=lambda: 0.0)
    finally:
        produced.unlink()


def _forget_signed(state_dir: Path) -> None:
    """Remove the signed record and the stored signed file, so the bundle looks never signed.

    A stored file with no record is a used version, so both must go for the bundle to read as unsigned.
    """
    (state_dir / "signed.json").unlink(missing_ok=True)
    shutil.rmtree(state_dir / "signed", ignore_errors=True)


def _sha_of(path: Path) -> str:
    """The sha256 of a file's bytes: the hash an install is given for it."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def profile(tmp_path):
    folder = tmp_path / "Zotero" / "Profiles" / "abc.default"
    folder.mkdir(parents=True)
    _write(folder / "prefs.js", 'user_pref("keep", true);\n')
    _write(folder / "extensions.json", '{"addons": []}')
    # The repo bundle counts as signed (a stand-in with the bundle's own bytes), so CLI installs can run.
    _seed_signed(zpi.signing_state_dir(), zpi._bundled_source_dir(), zpi.build_xpi_bytes())
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


def _with_crlf(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))


def test_crlf_and_lf_checkouts_pack_identical_bytes(tmp_path):
    lf = _fake_source(tmp_path / "lf")
    crlf = _fake_source(tmp_path / "crlf")
    for root in (lf, crlf):
        _write(root / "manifest.json", '{\n  "manifest_version": 2\n}\n')
        _write(root / "lib" / "pdf.js", "export {};\n// two lines\n")
    _with_crlf(crlf)
    # The CRLF copy really differs on disk, so the equality below is not vacuous.
    assert (crlf / "manifest.json").read_bytes() != (lf / "manifest.json").read_bytes()
    lf_bytes = zpi.build_xpi(tmp_path / "lf.xpi", source_dir=lf).read_bytes()
    crlf_bytes = zpi.build_xpi(tmp_path / "crlf.xpi", source_dir=crlf).read_bytes()
    assert lf_bytes == crlf_bytes
    with zipfile.ZipFile(io.BytesIO(crlf_bytes)) as zf:
        assert zf.read("manifest.json") == b'{\n  "manifest_version": 2\n}\n'
        assert zf.read("lib/pdf.js") == b"export {};\n// two lines\n"


def test_binary_entries_are_packed_untouched_even_when_they_contain_crlf(tmp_path):
    src = _fake_source(tmp_path / "src")
    blob = bytes([0x89, 0x50, 0x00, 0x0D, 0x0A, 0x1A])  # NUL and CRLF: binary, never rewritten
    _write(src / "content" / "icon.png", blob)
    out = zpi.build_xpi(tmp_path / "out.xpi", source_dir=src)
    with zipfile.ZipFile(out) as zf:
        assert zf.read("content/icon.png") == blob


# ---------------------------------------------------------------------------
# Frozen desktop build: the bridge source is resolved from sys._MEIPASS
# ---------------------------------------------------------------------------


def test_source_resolves_to_the_repo_copy_when_not_frozen(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert zpi._bundled_source_dir() == zpi._SOURCE_DIR


def test_frozen_build_packs_the_bridge_from_the_meipass_bundle(monkeypatch, tmp_path):
    bundle = tmp_path / "meipass"
    _fake_source(bundle / "extensions")  # meipass/extensions/zotero-bridge, where the spec puts it
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    assert zpi._bundled_source_dir() == bundle / "extensions" / "zotero-bridge"
    assert zipfile.ZipFile(io.BytesIO(zpi.build_xpi_bytes())).namelist() == EXPECTED_ENTRIES


class _SpecAnalysis:
    """Records the datas/hiddenimports the spec hands to PyInstaller's Analysis()."""

    def __init__(self, scripts, **kwargs):
        self.scripts = scripts
        self.datas = kwargs.get("datas", [])
        self.hiddenimports = kwargs.get("hiddenimports", [])
        self.pure = []
        self.zipped_data = []
        self.binaries = []
        self.zipfiles = []


def _run_tray_spec_analysis(monkeypatch):
    """Execute meridian-tray.spec with fake PyInstaller globals, from the repo root."""
    monkeypatch.chdir(REPO_ROOT)  # the spec's source paths are relative, as PyInstaller runs it
    monkeypatch.setattr(sys, "platform", "win32")
    namespace = {
        "__name__": "__pyinstaller_spec__",
        "__file__": str(SPEC_PATH),
        "Analysis": _SpecAnalysis,
        "PYZ": lambda *args, **kwargs: None,
        "EXE": lambda *args, **kwargs: None,
        "COLLECT": lambda *args, **kwargs: None,
        "BUNDLE": lambda *args, **kwargs: None,
    }
    source = SPEC_PATH.read_text(encoding="utf-8")
    exec(compile(source, str(SPEC_PATH), "exec"), namespace)  # noqa: S102
    return namespace["a"]


def test_tray_spec_bundles_the_bridge_source_without_test_or_node_modules(monkeypatch):
    analysis = _run_tray_spec_analysis(monkeypatch)
    bundled = {}
    for src, folder in analysis.datas:
        archive = f"{folder.replace(os.sep, '/')}/{Path(src).name}"
        if archive.startswith("extensions/zotero-bridge/"):
            bundled[archive] = Path(src)
    for required in (
        "extensions/zotero-bridge/manifest.json",
        "extensions/zotero-bridge/bootstrap.js",
        "extensions/zotero-bridge/prefs.js",
        "extensions/zotero-bridge/lib/bibkey.js",
        "extensions/zotero-bridge/content/preferences.xhtml",
    ):
        assert required in bundled, required
        assert bundled[required].is_file()
    assert not any("/test/" in name or "/node_modules/" in name for name in bundled)


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


def _completed(argv, returncode, stdout=""):
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")


def test_tasklist_nonzero_exit_fails_closed_even_with_empty_output(monkeypatch):
    monkeypatch.setattr(zpi, "_platform", lambda: "win32")
    monkeypatch.setattr(
        zpi.subprocess, "run", lambda argv, **kw: _completed(argv, 1, stdout=""),
    )
    assert zpi.zotero_is_running() is True


def test_tasklist_zero_exit_with_empty_output_means_not_running(monkeypatch):
    monkeypatch.setattr(zpi, "_platform", lambda: "win32")
    monkeypatch.setattr(
        zpi.subprocess, "run", lambda argv, **kw: _completed(argv, 0, stdout=""),
    )
    assert zpi.zotero_is_running() is False


def test_pgrep_other_nonzero_exit_fails_closed_even_with_empty_output(monkeypatch):
    monkeypatch.setattr(zpi, "_platform", lambda: "linux")
    monkeypatch.setattr(
        zpi.subprocess, "run", lambda argv, **kw: _completed(argv, 2, stdout=""),
    )
    assert zpi.zotero_is_running() is True


def test_pgrep_exit_one_is_its_no_match_answer_so_not_running(monkeypatch):
    # pgrep exits 1 when no process matches. That is a clean "not running",
    # not a probe error, so treating it as running would refuse every install
    # while Zotero is closed.
    monkeypatch.setattr(zpi, "_platform", lambda: "linux")
    monkeypatch.setattr(
        zpi.subprocess, "run", lambda argv, **kw: _completed(argv, 1, stdout=""),
    )
    assert zpi.zotero_is_running() is False


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_probe_exception_from_subprocess_fails_closed(monkeypatch, platform):
    def _raise(argv, **kwargs):
        raise OSError("process listing unavailable")

    monkeypatch.setattr(zpi, "_platform", lambda: platform)
    monkeypatch.setattr(zpi.subprocess, "run", _raise)
    assert zpi.zotero_is_running() is True


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
# zotero_process_state: True, False, or None when the check itself failed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_process_state_is_unknown_when_the_check_fails(monkeypatch, platform):
    def _broken():
        raise OSError("process listing unavailable")

    monkeypatch.setattr(zpi, "_platform", lambda: platform)
    assert zpi.zotero_process_state(runner=_broken) is None


def test_process_state_is_true_or_false_for_a_successful_listing(monkeypatch):
    monkeypatch.setattr(zpi, "_platform", lambda: "win32")
    monkeypatch.setattr(
        zpi.subprocess, "run", lambda argv, **kw: _completed(argv, 0, stdout="zotero.exe 1 Console 1 K\n"),
    )
    assert zpi.zotero_process_state() is True
    monkeypatch.setattr(zpi.subprocess, "run", lambda argv, **kw: _completed(argv, 0, stdout=""))
    assert zpi.zotero_process_state() is False


def test_tasklist_nonzero_exit_is_unknown_not_closed(monkeypatch):
    monkeypatch.setattr(zpi, "_platform", lambda: "win32")
    monkeypatch.setattr(zpi.subprocess, "run", lambda argv, **kw: _completed(argv, 1, stdout=""))
    assert zpi.zotero_process_state() is None
    assert zpi.zotero_is_running() is True


# ---------------------------------------------------------------------------
# install_into_profile
# ---------------------------------------------------------------------------


def test_install_refuses_while_zotero_is_running_and_writes_nothing(profile, xpi):
    before = _snapshot(profile)
    result = zpi.install_into_profile(profile, xpi, running=lambda: True, expected_sha256=_sha_of(xpi))
    assert result["status"] == "refused_running"
    assert _snapshot(profile) == before
    assert not (profile / "extensions").exists()


def test_install_reports_check_failed_and_writes_nothing_when_the_process_check_fails(monkeypatch, profile, xpi):
    monkeypatch.setattr(zpi, "_platform", lambda: "win32")
    monkeypatch.setattr(zpi.subprocess, "run", lambda argv, **kw: _completed(argv, 1, stdout=""))
    before = _snapshot(profile)

    result = zpi.install_into_profile(profile, xpi, expected_sha256=_sha_of(xpi))

    assert result["status"] == "check_failed"
    assert "could not check whether zotero is running" in result["error"].lower()
    assert _snapshot(profile) == before
    assert not (profile / "extensions").exists()


def test_install_default_profile_reports_check_failed_not_refused_running(monkeypatch, default_profile_env):
    monkeypatch.setattr(zpi, "_platform", lambda: "win32")
    monkeypatch.setattr(zpi.subprocess, "run", lambda argv, **kw: _completed(argv, 1, stdout=""))
    before = _snapshot(default_profile_env)

    result = zpi.install_default_profile()

    assert result["status"] == "check_failed"
    assert _snapshot(default_profile_env) == before


def test_install_copies_the_xpi_and_leaves_no_temp_file(profile, xpi):
    result = zpi.install_into_profile(profile, xpi, running=lambda: False, expected_sha256=_sha_of(xpi))
    target = profile / "extensions" / XPI_NAME
    assert result["status"] == "installed"
    assert target.read_bytes() == xpi.read_bytes()
    assert sorted(p.name for p in (profile / "extensions").iterdir()) == [XPI_NAME]


def test_second_install_reports_unchanged_and_does_not_rewrite(monkeypatch, profile, xpi):
    assert zpi.install_into_profile(profile, xpi, running=lambda: False, expected_sha256=_sha_of(xpi))["status"] == "installed"

    def _must_not_write(target, payload):
        raise AssertionError("an unchanged install must not rewrite the file")

    monkeypatch.setattr(zpi, "_atomic_write_bytes", _must_not_write)
    assert zpi.install_into_profile(profile, xpi, running=lambda: False, expected_sha256=_sha_of(xpi))["status"] == "unchanged"


def test_install_never_touches_extensions_json_prefs_or_anything_else(profile, xpi):
    before = _snapshot(profile)
    zpi.install_into_profile(profile, xpi, running=lambda: False, expected_sha256=_sha_of(xpi))
    after = _snapshot(profile)
    assert after["extensions.json"] == before["extensions.json"]
    assert after["prefs.js"] == before["prefs.js"]
    for rel, digest in before.items():
        assert after[rel] == digest
    assert set(after) - set(before) == {"extensions", f"extensions/{XPI_NAME}"}


def test_install_with_no_profile_reports_no_profile_and_creates_nothing(tmp_path, xpi):
    assert zpi.install_into_profile(None, xpi, running=lambda: False, expected_sha256=_sha_of(xpi))["status"] == "no_profile"
    missing = tmp_path / "missing"
    assert zpi.install_into_profile(missing, xpi, running=lambda: False, expected_sha256=_sha_of(xpi))["status"] == "no_profile"
    assert not missing.exists()


def test_install_reports_failed_when_the_source_is_missing_and_writes_nothing(profile, tmp_path):
    result = zpi.install_into_profile(profile, tmp_path / "nope.xpi", running=lambda: False, expected_sha256="0" * 64)
    assert result["status"] == "failed"
    assert not (profile / "extensions").exists()


def test_install_refuses_a_folder_without_a_profile_marker_and_writes_nothing(tmp_path, xpi):
    bare = tmp_path / "not-a-profile"
    bare.mkdir()
    _write(bare / "notes.txt", "hello")
    before = _snapshot(bare)
    result = zpi.install_into_profile(bare, xpi, running=lambda: False, expected_sha256=_sha_of(xpi))
    assert result["status"] == "refused_not_profile"
    assert "prefs.js" in result["error"] and "zotero.sqlite" in result["error"]
    assert _snapshot(bare) == before
    assert not (bare / "extensions").exists()


@pytest.mark.parametrize("marker", ["prefs.js", "zotero.sqlite"])
def test_install_accepts_a_folder_holding_either_profile_marker(tmp_path, xpi, marker):
    folder = tmp_path / "profile"
    folder.mkdir()
    _write(folder / marker, "marker")
    assert zpi.install_into_profile(folder, xpi, running=lambda: False, expected_sha256=_sha_of(xpi))["status"] == "installed"
    assert (folder / "extensions" / XPI_NAME).is_file()


def test_install_with_no_hash_to_check_refuses_and_writes_nothing(profile, xpi):
    """None is never a pass: with nothing to compare the bytes against, the copy is refused."""
    before = _snapshot(profile)
    result = zpi.install_into_profile(profile, xpi, running=lambda: False, expected_sha256=None)
    assert result["status"] == "integrity_failed"
    assert _snapshot(profile) == before


# ---------------------------------------------------------------------------
# settings file
# ---------------------------------------------------------------------------


def test_settings_live_in_the_meridian_per_user_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("MERIDIAN_LOCAL_RUNNER_STATE_DIR", str(tmp_path / "state"))
    assert zpi.settings_path() == tmp_path / "state" / "zotero_plugin.json"


def test_settings_defaults_when_the_file_is_missing(tmp_path):
    assert zpi.load_settings(tmp_path / "zotero_plugin.json") == {
        "autoinstall": False, "decided": False, "close_zotero_for_install": True,
    }


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[1, 2]",
        '"just a string"',
        '{"autoinstall": "yes", "decided": 1}',
        '{"autoinstall": true}',
        '{"decided": true}',
        b"\xff\xfe\x00 not utf-8",
    ],
)
def test_unreadable_or_wrong_shape_settings_are_undecided_with_autoinstall_off(tmp_path, text):
    path = _write(tmp_path / "zotero_plugin.json", text)
    settings = zpi.load_settings(path)
    assert settings == {"autoinstall": False, "decided": False, "close_zotero_for_install": True}
    assert zpi.effective_autoinstall(settings, {}) is False
    assert zpi.effective_autoinstall(settings, {ENV_AUTOINSTALL: "1"}) is False


def test_corrupt_file_never_turns_a_saved_opt_out_back_on(tmp_path):
    path = _write(tmp_path / "zotero_plugin.json", '{"autoinstall": false, "decided": tru')
    assert zpi.load_settings(path)["autoinstall"] is False


def test_save_refuses_settings_that_are_not_two_booleans(tmp_path):
    path = tmp_path / "zotero_plugin.json"
    with pytest.raises(ValueError):
        zpi.save_settings(path, {"autoinstall": "yes", "decided": True})
    assert not path.exists()


def test_settings_round_trip_creates_the_folder_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "state" / "zotero_plugin.json"
    zpi.save_settings(path, {"autoinstall": False, "decided": True})
    assert zpi.load_settings(path) == {"autoinstall": False, "decided": True, "close_zotero_for_install": True}
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "autoinstall": False, "decided": True, "close_zotero_for_install": True,
    }
    assert sorted(p.name for p in path.parent.iterdir()) == ["zotero_plugin.json"]


def test_first_run_install_choice_enables_autoinstall_and_marks_decided():
    original = {"autoinstall": False, "decided": False}
    updated = zpi.apply_first_run_choice(original, True)
    assert updated == {"autoinstall": True, "decided": True, "close_zotero_for_install": True}
    assert original == {"autoinstall": False, "decided": False}


def test_first_run_not_now_choice_disables_autoinstall_and_marks_decided():
    updated = zpi.apply_first_run_choice({"autoinstall": True, "decided": False}, False)
    assert updated == {"autoinstall": False, "decided": True, "close_zotero_for_install": True}


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


def test_undecided_settings_never_install_even_when_the_env_var_says_on():
    undecided = {"autoinstall": True, "decided": False}
    assert zpi.effective_autoinstall(undecided, {ENV_AUTOINSTALL: "1"}) is False
    assert zpi.effective_autoinstall(undecided, {}) is False


def test_env_override_is_reported_only_for_recognised_values():
    assert zpi.autoinstall_env_override({ENV_AUTOINSTALL: "off"}) is False
    assert zpi.autoinstall_env_override({ENV_AUTOINSTALL: "YES"}) is True
    assert zpi.autoinstall_env_override({ENV_AUTOINSTALL: "maybe"}) is None
    assert zpi.autoinstall_env_override({}) is None


@pytest.mark.parametrize(
    ("raw", "forced_off"),
    [
        ("0", True), (" FALSE ", True), ("No", True), ("off", True),
        ("1", False), ("true", False), ("", False), ("maybe", False),
    ],
)
def test_automatic_installs_forced_off_is_true_only_for_the_off_values(raw, forced_off):
    assert zpi.automatic_installs_forced_off({ENV_AUTOINSTALL: raw}) is forced_off


def test_docs_list_every_env_value_the_code_accepts_and_say_what_it_scopes():
    # The guide is read from the repo checkout. Nothing here touches Zotero or the user's profile.
    docs = (REPO_ROOT / "docs" / "zotero-plugin-install.md").read_text(encoding="utf-8")
    paragraphs = [paragraph for paragraph in docs.split("\n\n") if "These values force" in paragraph]
    assert len(paragraphs) == 1, "the guide needs exactly one paragraph that lists the env values"
    text = paragraphs[0]
    off = re.search(r"force automatic installs \*\*off\*\*: ([^.]*)\.", text)
    on = re.search(r"force them \*\*on\*\*, but only after you have answered the prompt: ([^.]*)\.", text)
    assert off and on, "the off and on value lists are not in the expected sentences"
    assert set(re.findall(r"`([^`]+)`", off.group(1))) == zpi._FALSE_VALUES
    assert set(re.findall(r"`([^`]+)`", on.group(1))) == zpi._TRUE_VALUES
    assert ENV_AUTOINSTALL in text
    assert "trimmed of spaces" in text
    assert "without regard to case" in text
    assert "Any other value, or an empty one, is ignored" in text
    assert "Install Zotero plugin now" in text


# ---------------------------------------------------------------------------
# CLI: python -m meridian zotero install-plugin [--profile DIR] [--dry-run]
# ---------------------------------------------------------------------------


def test_cli_dry_run_prints_the_plan_and_writes_nothing(monkeypatch, capsys, profile):
    monkeypatch.setattr(zpi, "zotero_process_state", lambda runner=None: False)
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
    monkeypatch.setattr(zpi, "zotero_process_state", lambda runner=None: False)
    rc = zpi.cli_main(["--profile", str(profile)])
    assert rc == 0
    assert (profile / "extensions" / XPI_NAME).read_bytes() == zpi.build_xpi_bytes()
    assert "installed" in capsys.readouterr().out.lower()


def test_cli_refuses_a_folder_that_is_not_a_zotero_profile(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(zpi, "zotero_process_state", lambda runner=None: False)
    bare = tmp_path / "bare"
    bare.mkdir()
    rc = zpi.cli_main(["--profile", str(bare)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "not a zotero profile" in (captured.out + captured.err).lower()
    assert not (bare / "extensions").exists()


def test_cli_refuses_with_a_clear_message_while_zotero_runs(monkeypatch, capsys, profile):
    monkeypatch.setattr(zpi, "zotero_process_state", lambda runner=None: True)
    rc = zpi.cli_main(["--profile", str(profile), "--keep-zotero-open"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "zotero is running" in (captured.out + captured.err).lower()
    assert not (profile / "extensions").exists()


def test_cli_prints_manual_steps_when_no_profile_is_found(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(zpi, "zotero_process_state", lambda runner=None: False)
    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path / "empty-zotero-root"))
    rc = zpi.cli_main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Tools > Plugins" in out
    assert "Install Plugin From File" in out
    assert "Add-on" not in out
    assert XPI_NAME in out


def test_cli_says_the_check_failed_and_never_that_zotero_is_running(monkeypatch, capsys, profile):
    monkeypatch.setattr(zpi, "zotero_process_state", lambda runner=None: None)
    rc = zpi.cli_main(["--profile", str(profile)])
    captured = capsys.readouterr()
    text = (captured.out + captured.err).lower()
    assert rc == 1
    assert "could not check whether zotero is running" in text
    assert "nothing was written to the zotero profile." in text
    assert "zotero is running. close" not in text
    assert not (profile / "extensions").exists()


def test_cli_dry_run_says_the_check_failed_and_writes_nothing(monkeypatch, capsys, profile):
    monkeypatch.setattr(zpi, "zotero_process_state", lambda runner=None: None)
    before = _snapshot(profile)
    rc = zpi.cli_main(["--profile", str(profile), "--dry-run"])
    out = capsys.readouterr().out.lower()
    assert rc == 0
    assert "could not check whether zotero is running" in out
    assert "would refuse: zotero is running" not in out
    assert _snapshot(profile) == before


def test_cli_installs_even_when_the_env_var_turns_automatic_installs_off(monkeypatch, capsys, profile):
    # The explicit command is a request from the user, so MERIDIAN_ZOTERO_PLUGIN_AUTOINSTALL=0 does not stop it.
    monkeypatch.setenv(ENV_AUTOINSTALL, "0")
    monkeypatch.setattr(zpi, "zotero_process_state", lambda runner=None: False)
    rc = zpi.cli_main(["--profile", str(profile)])
    assert rc == 0
    assert (profile / "extensions" / XPI_NAME).is_file()


def test_dispatch_routes_zotero_install_plugin_to_the_installer(monkeypatch, capsys, profile):
    from meridian import __main__ as entry

    monkeypatch.setattr(zpi, "zotero_process_state", lambda runner=None: False)
    rc = entry._dispatch_subcommand(["zotero", "install-plugin", "--profile", str(profile), "--dry-run"])
    assert rc == 0
    assert "dry run" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# install_into_profile never raises: a PermissionError while probing maps to failed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("probe", ["is_dir", "is_zotero_profile"])
def test_permission_error_while_probing_the_profile_maps_to_failed(monkeypatch, profile, xpi, probe):
    if probe == "is_dir":
        real_is_dir = zpi.Path.is_dir

        def _denied_is_dir(self, *args, **kwargs):
            if self == profile:
                raise PermissionError("access denied")
            return real_is_dir(self, *args, **kwargs)

        monkeypatch.setattr(zpi.Path, "is_dir", _denied_is_dir)
    else:

        def _denied_probe(folder):
            raise PermissionError("access denied")

        monkeypatch.setattr(zpi, "is_zotero_profile", _denied_probe)

    result = zpi.install_into_profile(profile, xpi, running=lambda: False, expected_sha256=_sha_of(xpi))

    assert result["status"] == "failed"
    assert "access denied" in result["error"]
    assert not (profile / "extensions").exists()


# ---------------------------------------------------------------------------
# tasklist decoding: undecodable bytes must not read as "running" or hide zotero.exe
# ---------------------------------------------------------------------------

# 0x81, 0x8D, 0x8F, 0x90 and 0x9D have no cp1252 meaning, so a strict decode fails on them.
_CP1252_UNDECODABLE = b"\x81\x8d\x8f\x90\x9d"


def _text_mode_run(raw):
    """A fake subprocess.run that decodes the captured bytes the way text mode does (cp1252 here)."""
    seen = {}

    def _fake_run(argv, **kwargs):
        seen.update(kwargs)
        stdout = raw.decode("cp1252", errors=kwargs.get("errors") or "strict")
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    return _fake_run, seen


def test_tasklist_undecodable_bytes_read_as_not_running_when_zotero_is_closed(monkeypatch):
    fake_run, seen = _text_mode_run(b"Setup" + _CP1252_UNDECODABLE + b".exe  2468 Console\r\n")
    monkeypatch.setattr(zpi, "_platform", lambda: "win32")
    monkeypatch.setattr(zpi.subprocess, "run", fake_run)

    assert zpi.zotero_is_running() is False
    assert seen["errors"] == "replace"


def test_tasklist_undecodable_bytes_still_find_a_running_zotero(monkeypatch):
    raw = b"Setup" + _CP1252_UNDECODABLE + b".exe 2468 Console\r\nzotero.exe  1234 Console  1  100,000 K\r\n"
    fake_run, _ = _text_mode_run(raw)
    monkeypatch.setattr(zpi, "_platform", lambda: "win32")
    monkeypatch.setattr(zpi.subprocess, "run", fake_run)

    assert zpi.zotero_is_running() is True


def test_undecodable_byte_glued_to_zotero_exe_cannot_fake_a_match(monkeypatch):
    # The replacement character is not ASCII, so a garbled name never equals zotero.exe.
    fake_run, _ = _text_mode_run(b"zotero.exe\x81  77 Console\r\n")
    monkeypatch.setattr(zpi, "_platform", lambda: "win32")
    monkeypatch.setattr(zpi.subprocess, "run", fake_run)

    assert zpi.zotero_is_running() is False


# ---------------------------------------------------------------------------
# install_default_profile: the packaged app's one-call install. It never raises.
# ---------------------------------------------------------------------------


@pytest.fixture
def default_profile_env(monkeypatch, tmp_path):
    """A Zotero data root whose profiles.ini names a default profile, with every write redirected."""
    data = tmp_path / "ZoteroData"
    profile = data / "Profiles" / "abc.default"
    _write(profile / "prefs.js", 'user_pref("keep", true);\n')
    _write(
        data / "profiles.ini",
        "[Profile0]\nName=default\nIsRelative=1\nPath=Profiles/abc.default\nDefault=1\n",
    )
    source = _fake_source(tmp_path / "src")
    monkeypatch.setattr(zpi, "zotero_data_dir", lambda: data)
    monkeypatch.setattr(zpi, "_bundled_source_dir", lambda: source)
    monkeypatch.setattr(zpi, "default_xpi_path", lambda: tmp_path / "state" / XPI_NAME)
    _seed_signed(tmp_path / "state", source, zpi.build_xpi_bytes(source))
    return profile


def test_install_default_profile_installs_into_zoteros_default_profile(default_profile_env):
    result = zpi.install_default_profile(running=lambda: False)

    assert result["status"] == "installed"
    assert (default_profile_env / "extensions" / XPI_NAME).is_file()


def test_install_default_profile_reports_no_profile_and_writes_nothing_without_a_default(monkeypatch, tmp_path):
    empty = tmp_path / "EmptyZoteroData"

    def _must_not_build():
        raise AssertionError("no plugin bundle should be built when there is no profile")

    monkeypatch.setattr(zpi, "zotero_data_dir", lambda: empty)
    monkeypatch.setattr(zpi, "default_xpi_path", _must_not_build)

    result = zpi.install_default_profile(running=lambda: False)

    assert result["status"] == "no_profile"
    assert not empty.exists()


def test_install_default_profile_refuses_while_zotero_runs_and_writes_nothing(default_profile_env):
    before = _snapshot(default_profile_env)

    result = zpi.install_default_profile(running=lambda: True)

    assert result["status"] == "refused_running"
    assert _snapshot(default_profile_env) == before


def test_install_default_profile_turns_a_build_failure_into_failed(monkeypatch, default_profile_env):
    def _disk_full(out_path, source_dir=None):
        raise OSError("disk full")

    monkeypatch.setattr(zpi, "build_xpi", _disk_full)

    result = zpi.install_default_profile(running=lambda: False)

    assert result["status"] == "failed"
    assert "disk full" in result["error"]
    assert not (default_profile_env / "extensions").exists()


def test_install_default_profile_turns_a_profile_lookup_failure_into_failed(monkeypatch):
    def _denied():
        raise PermissionError("access denied")

    monkeypatch.setattr(zpi, "zotero_data_dir", _denied)

    result = zpi.install_default_profile(running=lambda: False)

    assert result["status"] == "failed"
    assert "access denied" in result["error"]


# ---------------------------------------------------------------------------
# Closing Zotero for the install: close_zotero_gracefully and install_with_close
# ---------------------------------------------------------------------------

CLOSE_ENV = "MERIDIAN_ZOTERO_CLOSE_ON_INSTALL"
TASKKILL_ARGV = ["taskkill", "/IM", "zotero.exe"]
_CLOSE_ON = {"autoinstall": False, "decided": True, "close_zotero_for_install": True}
_CLOSE_OFF = {"autoinstall": False, "decided": True, "close_zotero_for_install": False}


class _FakeSleep:
    """Records every wait and never blocks."""

    def __init__(self):
        self.waits = []

    def __call__(self, seconds):
        self.waits.append(seconds)


def _states(*values):
    """A process check that answers each value in turn, then keeps answering the last one."""
    queue = list(values)

    def _check():
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]

    return _check


def _runner_recording(calls):
    def _run(argv):
        calls.append(list(argv))
        return None

    return _run


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(zpi, "_platform", lambda: "win32")


# close_zotero_gracefully -------------------------------------------------------


def test_close_argv_is_exactly_taskkill_by_image_name_with_no_force_flag(windows):
    calls = []
    outcome = zpi.close_zotero_gracefully(
        runner=_runner_recording(calls), timeout_s=1.0, sleep=_FakeSleep(), running=_states(True, False),
    )
    assert outcome == "closed"
    assert calls == [TASKKILL_ARGV]
    assert all(part.upper() not in {"/F", "/T"} for part in calls[0])
    assert not any("kill" in part.lower() for part in calls[0][1:])


def test_default_runner_hands_subprocess_the_same_argv_and_no_shell(monkeypatch, windows):
    seen = []

    def _fake_run(argv, **kwargs):
        seen.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(zpi.subprocess, "run", _fake_run)
    outcome = zpi.close_zotero_gracefully(timeout_s=1.0, sleep=_FakeSleep(), running=_states(True, False))
    assert outcome == "closed"
    assert [argv for argv, _ in seen] == [TASKKILL_ARGV]
    assert not seen[0][1].get("shell", False)


def test_close_reports_closed_once_zotero_stops(windows):
    calls = []
    outcome = zpi.close_zotero_gracefully(
        runner=_runner_recording(calls), timeout_s=20.0, sleep=_FakeSleep(), running=_states(True, True, False),
    )
    assert outcome == "closed"
    assert calls == [TASKKILL_ARGV]


def test_close_polls_every_half_second_up_to_the_timeout(windows):
    clock = _FakeSleep()
    outcome = zpi.close_zotero_gracefully(
        runner=_runner_recording([]), timeout_s=20.0, sleep=clock, running=_states(True, True, False),
    )
    assert outcome == "closed"
    assert clock.waits == [0.5, 0.5]


def test_close_reports_not_running_and_runs_nothing_when_zotero_was_already_closed(windows):
    calls, clock = [], _FakeSleep()
    outcome = zpi.close_zotero_gracefully(runner=_runner_recording(calls), sleep=clock, running=_states(False))
    assert outcome == "not_running"
    assert calls == []
    assert clock.waits == []


def test_close_reports_close_failed_when_zotero_still_runs_at_the_timeout(windows):
    clock = _FakeSleep()
    outcome = zpi.close_zotero_gracefully(
        runner=_runner_recording([]), timeout_s=2.0, sleep=clock, running=_states(True),
    )
    assert outcome == "close_failed"
    assert clock.waits == [0.5, 0.5, 0.5, 0.5]


def test_close_fails_closed_when_the_check_fails_before_taskkill_runs(windows):
    calls = []
    outcome = zpi.close_zotero_gracefully(
        runner=_runner_recording(calls), sleep=_FakeSleep(), running=lambda: None,
    )
    assert outcome == "close_failed"
    assert calls == []


def test_close_fails_closed_when_the_check_fails_while_waiting(windows):
    outcome = zpi.close_zotero_gracefully(
        runner=_runner_recording([]), timeout_s=20.0, sleep=_FakeSleep(), running=_states(True, None),
    )
    assert outcome == "close_failed"


def test_close_treats_a_raising_check_as_a_failed_check(windows):
    def _broken():
        raise RuntimeError("tasklist exploded")

    outcome = zpi.close_zotero_gracefully(runner=_runner_recording([]), sleep=_FakeSleep(), running=_broken)
    assert outcome == "close_failed"


def test_a_taskkill_that_raises_never_escapes_and_the_poll_decides(windows):
    def _boom(argv):
        raise OSError("taskkill is missing")

    outcome = zpi.close_zotero_gracefully(runner=_boom, timeout_s=1.0, sleep=_FakeSleep(), running=_states(True))
    assert outcome == "close_failed"


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_close_is_unsupported_off_windows_and_runs_nothing(monkeypatch, platform):
    monkeypatch.setattr(zpi, "_platform", lambda: platform)
    calls = []
    outcome = zpi.close_zotero_gracefully(runner=_runner_recording(calls), sleep=_FakeSleep(), running=_states(True))
    assert outcome == "unsupported"
    assert calls == []


def test_close_uses_the_process_state_check_when_none_is_injected(monkeypatch, windows):
    monkeypatch.setattr(zpi, "zotero_process_state", lambda runner=None: False)
    assert zpi.close_zotero_gracefully(runner=_runner_recording([]), sleep=_FakeSleep()) == "not_running"


# install_with_close: the orchestration -----------------------------------------


def _steps(events, *, install_status="installed", close_outcome="closed", restart_action="started"):
    """Fake close, install and restart callables that record their order in ``events``."""

    def _close(running=None):
        events.append("close")
        return close_outcome

    def _install():
        events.append("install")
        return {"status": install_status, "path": "P", "error": None}

    def _restart():
        events.append("restart")
        return restart_action

    return _close, _install, _restart


def test_closes_installs_then_restarts_when_meridian_closed_zotero():
    events = []
    close, install, restart = _steps(events)
    result = zpi.install_with_close(
        _CLOSE_ON, close_zotero=True, running=_states(True), close=close, install=install, restart=restart,
    )
    assert events == ["close", "install", "restart"]
    assert result["status"] == "installed"
    assert result["closed_zotero"] is True
    assert result["restart"] == "started"


def test_never_restarts_zotero_that_was_not_running_at_the_start():
    events = []
    close, install, restart = _steps(events)
    result = zpi.install_with_close(
        _CLOSE_ON, close_zotero=True, running=_states(False), close=close, install=install, restart=restart,
    )
    assert events == ["install"]
    assert result["status"] == "installed"
    assert result["closed_zotero"] is False
    assert result["restart"] is None


def test_a_close_that_finds_zotero_already_gone_installs_without_restarting():
    events = []
    close, install, restart = _steps(events, close_outcome="not_running")
    result = zpi.install_with_close(
        _CLOSE_ON, close_zotero=True, running=_states(True), close=close, install=install, restart=restart,
    )
    assert events == ["close", "install"]
    assert result["closed_zotero"] is False
    assert result["restart"] is None


def test_close_zotero_false_refuses_while_running_and_never_calls_close():
    events = []
    close, install, restart = _steps(events)
    result = zpi.install_with_close(
        _CLOSE_OFF, close_zotero=False, running=_states(True), close=close, install=install, restart=restart,
    )
    assert events == []
    assert result["status"] == "refused_running"
    assert result["closed_zotero"] is False
    assert result["restart"] is None
    assert "Zotero is running" in result["message"]


def test_close_zotero_none_follows_the_saved_setting():
    events = []
    close, install, restart = _steps(events)
    result = zpi.install_with_close(
        _CLOSE_OFF, close_zotero=None, running=_states(True), close=close, install=install, restart=restart,
    )
    assert events == []
    assert result["status"] == "refused_running"


def test_the_close_env_on_value_never_beats_a_saved_off():
    events = []
    close, install, restart = _steps(events)
    result = zpi.install_with_close(
        _CLOSE_OFF, close_zotero=None, env={CLOSE_ENV: "1"},
        running=_states(True), close=close, install=install, restart=restart,
    )
    assert events == []
    assert result["status"] == "refused_running"
    assert result["closed_zotero"] is False


def test_the_close_env_off_value_turns_closing_off_even_when_the_saved_setting_is_on():
    events = []
    close, install, restart = _steps(events)
    result = zpi.install_with_close(
        _CLOSE_ON, close_zotero=None, env={CLOSE_ENV: "off"},
        running=_states(True), close=close, install=install, restart=restart,
    )
    assert events == []
    assert result["status"] == "refused_running"


def test_an_explicit_close_zotero_argument_beats_the_close_env_override():
    events = []
    close, install, restart = _steps(events)
    result = zpi.install_with_close(
        _CLOSE_ON, close_zotero=False, env={CLOSE_ENV: "1"},
        running=_states(True), close=close, install=install, restart=restart,
    )
    assert events == []
    assert result["status"] == "refused_running"


def test_a_failed_process_check_refuses_without_closing_or_calling_the_install():
    def _install():
        pytest.fail("the install must not run when the process check fails")

    def _close(running=None):
        pytest.fail("Zotero must not be closed when the process check fails")

    result = zpi.install_with_close(
        _CLOSE_ON, close_zotero=True, running=lambda: None, close=_close, install=_install,
        restart=lambda: "started",
    )
    assert result["status"] == "check_failed"
    assert result["closed_zotero"] is False
    assert result["restart"] is None
    assert result["message"] == (
        "Could not check whether Zotero is running. Nothing was written to the Zotero profile."
    )


# install_default_with_close: the tray's install resolves the profile and builds the bundle before any close ---


def test_the_default_install_with_close_closes_then_installs_then_restarts(monkeypatch, default_profile_env):
    from meridian import zotero_launcher

    events = []
    real_build, real_install = zpi.build_xpi, zpi.install_into_profile

    def _build(out_path, source_dir=None):
        events.append("build")
        return real_build(out_path, source_dir=source_dir)

    def _install(profile_dir, xpi_path, **kwargs):
        events.append("install")
        return real_install(profile_dir, xpi_path, **kwargs)

    def _restart(**kwargs):
        events.append("restart")
        return {"action": "started", "exe": "zotero.exe", "api_ready": True}

    monkeypatch.setattr(zpi, "build_xpi", _build)
    monkeypatch.setattr(zpi, "install_into_profile", _install)
    monkeypatch.setattr(zotero_launcher, "start_zotero", _restart)
    result = zpi.install_default_with_close(
        _CLOSE_ON, close_zotero=True, running=_states(True, False),
        close=lambda running=None: events.append("close") or "closed",
    )
    assert events == ["build", "close", "install", "restart"]
    assert result["status"] == "installed"
    assert result["closed_zotero"] is True
    assert result["restart"] == "started"
    assert (default_profile_env / "extensions" / XPI_NAME).is_file()


def test_a_failed_process_check_on_the_default_install_writes_nothing_and_never_closes(default_profile_env):
    before = _snapshot(default_profile_env)
    result = zpi.install_default_with_close(
        _CLOSE_ON, close_zotero=True, running=lambda: None,
        close=lambda running=None: pytest.fail("Zotero must not be closed when the process check fails"),
    )
    assert result["status"] == "check_failed"
    assert result["closed_zotero"] is False
    assert _snapshot(default_profile_env) == before


def test_a_failed_process_check_never_reaches_the_profile_writer(monkeypatch, default_profile_env):
    def _must_not_write(*args, **kwargs):
        pytest.fail("the profile must not be written when the process check fails")

    monkeypatch.setattr(zpi, "install_into_profile", _must_not_write)
    result = zpi.install_default_with_close(
        _CLOSE_ON, close_zotero=True, running=lambda: None,
        close=lambda running=None: pytest.fail("Zotero must not be closed when the process check fails"),
    )
    assert result["status"] == "check_failed"


def test_no_default_profile_refuses_before_any_build_or_close(monkeypatch, tmp_path):
    empty = tmp_path / "EmptyZoteroData"
    monkeypatch.setattr(zpi, "zotero_data_dir", lambda: empty)
    monkeypatch.setattr(zpi, "build_xpi", lambda *a, **k: pytest.fail("no bundle is built without a profile"))
    result = zpi.install_default_with_close(
        _CLOSE_ON, close_zotero=True, running=lambda: True,
        close=lambda running=None: pytest.fail("Zotero must not be closed without a profile"),
    )
    assert result["status"] == "no_profile"
    assert result["closed_zotero"] is False
    assert result["restart"] is None
    assert not empty.exists()


def test_a_bundle_that_will_not_build_refuses_before_any_close(monkeypatch, default_profile_env):
    def _disk_full(out_path, source_dir=None):
        raise OSError("disk full")

    monkeypatch.setattr(zpi, "build_xpi", _disk_full)
    result = zpi.install_default_with_close(
        _CLOSE_ON, close_zotero=True, running=lambda: True,
        close=lambda running=None: pytest.fail("Zotero must not be closed when the bundle will not build"),
    )
    assert result["status"] == "failed"
    assert "disk full" in result["error"]
    assert result["closed_zotero"] is False
    assert not (default_profile_env / "extensions").exists()


def test_the_default_install_refuses_a_folder_without_a_marker_before_any_build_or_close(monkeypatch, tmp_path):
    data = tmp_path / "ZoteroData"
    bare = data / "Profiles" / "bare.default"
    bare.mkdir(parents=True)
    _write(data / "profiles.ini", "[Profile0]\nName=default\nIsRelative=1\nPath=Profiles/bare.default\nDefault=1\n")
    built = tmp_path / "state" / XPI_NAME
    monkeypatch.setattr(zpi, "zotero_data_dir", lambda: data)
    monkeypatch.setattr(zpi, "_bundled_source_dir", lambda: _fake_source(tmp_path / "src"))
    monkeypatch.setattr(zpi, "default_xpi_path", lambda: built)
    result = zpi.install_default_with_close(
        _CLOSE_ON, close_zotero=True, running=lambda: True,
        close=lambda running=None: pytest.fail("Zotero must not be closed for a folder that is not a profile"),
        restart=lambda: pytest.fail("nothing is restarted when Meridian closed nothing"),
    )
    assert result["status"] == "refused_not_profile"
    assert result["closed_zotero"] is False
    assert result["restart"] is None
    assert "neither prefs.js nor zotero.sqlite" in result["error"]
    assert _snapshot(bare) == {}
    assert not built.exists()


@pytest.mark.parametrize("zotero_state", [True, False, None])
def test_an_identical_installed_plugin_is_unchanged_with_no_close_and_no_restart(default_profile_env, zotero_state):
    # A complete install first, so the profile holds every repaired line. A rerun on it then writes nothing at all.
    assert zpi.install_default_with_close(_CLOSE_ON, close_zotero=True, running=lambda: False)["status"] == "installed"
    before = _snapshot(default_profile_env)
    result = zpi.install_default_with_close(
        _CLOSE_ON, close_zotero=True, running=lambda: zotero_state,
        close=lambda running=None: pytest.fail("Zotero must not be closed when the plugin is already current"),
        restart=lambda: pytest.fail("nothing is restarted when Meridian closed nothing"),
    )
    assert result["status"] == "unchanged"
    assert result["closed_zotero"] is False
    assert result["restart"] is None
    assert result["repaired"] == []
    assert _snapshot(default_profile_env) == before


def _without_lines(path: Path, marker: str) -> None:
    """Drop every line that contains ``marker`` from a text file, keeping the rest."""
    kept = [line for line in path.read_text(encoding="utf-8").splitlines(keepends=True) if marker not in line]
    path.write_text("".join(kept), encoding="utf-8")


def test_an_already_current_plugin_gets_its_removed_api_port_line_back_on_a_rerun(monkeypatch, default_profile_env):
    assert zpi.install_default_with_close(_CLOSE_ON, close_zotero=True, running=lambda: False)["status"] == "installed"
    user_js = default_profile_env / "user.js"
    _without_lines(user_js, "apiPort")
    assert "apiPort" not in user_js.read_text(encoding="utf-8")
    installed = default_profile_env / "extensions" / XPI_NAME
    xpi_before = installed.read_bytes()
    monkeypatch.setattr(zpi, "install_into_profile", lambda *a, **k: pytest.fail("the .xpi must not be rewritten"))

    result = zpi.install_default_with_close(
        _CLOSE_ON, close_zotero=True, running=lambda: False,
        close=lambda running=None: pytest.fail("an already-current plugin does not close Zotero"),
    )

    assert result["status"] == "unchanged"
    assert result["api_prefs"] == bridge.PREFS_UPDATED
    assert result["api_token"] == bridge.TOKEN_KEPT
    assert result["repaired"] == ["api_prefs"]
    assert user_js.read_text(encoding="utf-8").count("apiPort") == 1
    assert installed.read_bytes() == xpi_before


def test_an_already_current_plugin_gets_a_removed_scan_line_and_token_back_on_a_rerun(default_profile_env):
    assert zpi.install_default_with_close(_CLOSE_ON, close_zotero=True, running=lambda: False)["status"] == "installed"
    user_js = default_profile_env / "user.js"
    _without_lines(user_js, "startupScanScopes")
    bridge.token_file_path().unlink()

    result = zpi.install_default_with_close(_CLOSE_ON, close_zotero=True, running=lambda: False)

    assert result["status"] == "unchanged"
    assert result["scan_pref"] == zpi.SCAN_ADDED
    assert result["api_token"] == bridge.TOKEN_CREATED
    assert result["repaired"] == ["scan_pref", "api_token"]
    assert zpi.check_scan_pref(default_profile_env) == zpi.SCAN_PRESENT
    assert bridge.token_is_usable(bridge.token_file_path())


def test_cli_an_already_current_plugin_repairs_a_removed_api_port_line_and_says_so(monkeypatch, capsys, profile):
    monkeypatch.setattr(zpi, "zotero_process_state", lambda runner=None: False)
    assert zpi.cli_main(["--profile", str(profile)]) == 0
    capsys.readouterr()
    user_js = profile / "user.js"
    _without_lines(user_js, "apiPort")
    monkeypatch.setattr(zpi, "install_into_profile", lambda *a, **k: pytest.fail("the .xpi must not be rewritten"))

    rc = zpi.cli_main(["--profile", str(profile)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "already up to date" in out
    assert "Repaired in this profile: the local write API settings in user.js." in out
    assert user_js.read_text(encoding="utf-8").count("apiPort") == 1


def test_an_identical_installed_plugin_is_unchanged_even_when_closing_is_off(default_profile_env):
    assert zpi.install_default_profile(running=lambda: False)["status"] == "installed"
    result = zpi.install_default_with_close(
        _CLOSE_OFF, close_zotero=False, running=lambda: True,
        close=lambda running=None: pytest.fail("Zotero must not be closed when the plugin is already current"),
    )
    assert result["status"] == "unchanged"
    assert result["closed_zotero"] is False


def test_a_different_installed_plugin_still_closes_zotero_for_the_install(default_profile_env):
    assert zpi.install_default_profile(running=lambda: False)["status"] == "installed"
    installed = default_profile_env / "extensions" / XPI_NAME
    installed.write_bytes(b"an older plugin build")
    events = []
    result = zpi.install_default_with_close(
        _CLOSE_ON, close_zotero=True, running=_states(True, False),
        close=lambda running=None: events.append("close") or "closed",
        restart=lambda: "started",
    )
    assert events == ["close"]
    assert result["status"] == "installed"
    assert result["closed_zotero"] is True
    assert installed.read_bytes() == zpi.build_xpi_bytes()


@pytest.mark.parametrize("restart", ["started", "already_running"])
@pytest.mark.parametrize(
    "process_states",
    [(True, None), (True, True)],
    ids=["check_failed_after_close", "refused_running_after_close"],
)
def test_cli_closed_but_unconfirmed_says_meridian_closed_zotero_and_never_that_zotero_is_running(
    monkeypatch, capsys, profile, process_states, restart,
):
    from meridian import zotero_launcher

    monkeypatch.setattr(zpi, "zotero_process_state", _states(*process_states))
    monkeypatch.setattr(zpi, "close_zotero_gracefully", lambda **kwargs: "closed")
    monkeypatch.setattr(
        zotero_launcher, "start_zotero", lambda **kwargs: {"action": restart, "exe": "Z", "api_ready": True},
    )
    rc = zpi.cli_main(["--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 1
    assert out == (
        "Meridian closed Zotero and restarted it, but could not confirm that Zotero is closed, so nothing was "
        "installed. Close Zotero, then run this command again.\n"
    )
    assert not (profile / "extensions").exists()


@pytest.mark.parametrize("process_states", [(True, None), (True, True)], ids=["check_failed", "refused_running"])
def test_cli_closed_but_unconfirmed_without_a_restart_says_to_make_sure_zotero_is_closed(
    monkeypatch, capsys, profile, process_states,
):
    from meridian import zotero_launcher

    monkeypatch.setattr(zpi, "zotero_process_state", _states(*process_states))
    monkeypatch.setattr(zpi, "close_zotero_gracefully", lambda **kwargs: "closed")

    def _no_start(**kwargs):
        raise OSError("no exe")

    monkeypatch.setattr(zotero_launcher, "start_zotero", _no_start)
    rc = zpi.cli_main(["--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 1
    assert out == (
        "Meridian closed Zotero, but could not restart it, and could not confirm that Zotero is closed, so nothing "
        "was installed. Make sure Zotero is closed, then run this command again.\n"
    )
    assert not (profile / "extensions").exists()


def test_cli_an_identical_plugin_is_unchanged_and_never_closes_a_running_zotero(monkeypatch, capsys, profile):
    monkeypatch.setattr(zpi, "zotero_process_state", lambda runner=None: False)
    assert zpi.cli_main(["--profile", str(profile)]) == 0
    capsys.readouterr()
    monkeypatch.setattr(zpi, "zotero_process_state", lambda runner=None: True)
    monkeypatch.setattr(zpi, "close_zotero_gracefully", lambda **kwargs: pytest.fail("the close must not run"))
    rc = zpi.cli_main(["--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "already up to date" in out
    assert "Meridian closed" not in out


def test_close_failed_never_installs_never_restarts_and_says_to_close_zotero_and_try_again():
    events = []
    close, install, restart = _steps(events, close_outcome="close_failed")
    result = zpi.install_with_close(
        _CLOSE_ON, close_zotero=True, running=_states(True), close=close, install=install, restart=restart,
    )
    assert events == ["close"]
    assert result["status"] == "close_failed"
    assert result["closed_zotero"] is False
    assert result["restart"] is None
    assert result["message"] == (
        "Zotero did not close, so nothing was installed. Close Zotero yourself, then try the install again. "
        "Nothing was written to the Zotero profile."
    )
    assert result["message"] == zpi.CLOSE_FAILED_MESSAGE
    assert not hasattr(zpi, "_CLI_CLOSE_FAILED_MESSAGE")


def test_a_close_failure_leaves_the_profile_byte_identical(profile, xpi):
    before = _snapshot(profile)
    result = zpi.install_with_close(
        _CLOSE_ON,
        close_zotero=True,
        running=_states(True),
        close=lambda running=None: "close_failed",
        install=lambda: zpi.install_into_profile(profile, xpi, running=lambda: False, expected_sha256=_sha_of(xpi)),
        restart=lambda: "started",
    )
    assert result["status"] == "close_failed"
    assert _snapshot(profile) == before
    assert not (profile / "extensions").exists()


def test_a_closed_zotero_is_restarted_even_when_the_install_then_refuses():
    events = []
    close, _, restart = _steps(events)

    def _install():
        events.append("install")
        return {"status": "refused_running", "path": None}

    result = zpi.install_with_close(
        _CLOSE_ON, close_zotero=True, running=_states(True), close=close, install=_install, restart=restart,
    )
    assert events == ["close", "install", "restart"]
    assert result["status"] == "refused_running"
    assert result["closed_zotero"] is True
    assert result["restart"] == "started"


def test_an_install_that_raises_becomes_failed_and_zotero_is_still_restarted():
    events = []
    close, _, restart = _steps(events)

    def _install():
        events.append("install")
        raise RuntimeError("disk on fire")

    result = zpi.install_with_close(
        _CLOSE_ON, close_zotero=True, running=_states(True), close=close, install=_install, restart=restart,
    )
    assert result["status"] == "failed"
    assert "disk on fire" in result["error"]
    assert events == ["close", "install", "restart"]


@pytest.mark.parametrize("error", [RuntimeError("boom"), OSError("launch failed")])
def test_a_restart_that_raises_never_escapes_and_the_install_result_stands(error):
    events = []
    close, install, _ = _steps(events)

    def _restart():
        raise error

    result = zpi.install_with_close(
        _CLOSE_ON, close_zotero=True, running=_states(True), close=close, install=install, restart=_restart,
    )
    assert result["status"] == "installed"
    assert result["closed_zotero"] is True
    assert result["restart"] == "error"


def test_the_default_restart_uses_the_launcher_start_path_without_reinstalling(monkeypatch):
    from meridian import zotero_launcher

    seen = {}

    def _fake_start(**kwargs):
        seen.update(kwargs)
        return {"action": "started", "exe": "zotero.exe", "api_ready": True}

    def _no_install(*args, **kwargs):
        raise AssertionError("the restart must not install again")

    monkeypatch.setattr(zotero_launcher, "start_zotero", _fake_start)
    monkeypatch.setattr(zpi, "install_into_profile", _no_install)
    result = zpi.install_with_close(
        _CLOSE_ON, close_zotero=True, running=_states(True),
        close=lambda running=None: "closed", install=lambda: {"status": "installed", "path": "P"},
    )
    assert result["restart"] == "started"
    assert callable(seen["before_launch"])
    seen["before_launch"]()  # the restart's own pre-launch hook does nothing


def test_the_default_restart_reports_an_os_error_from_the_launcher_as_error(monkeypatch):
    from meridian import zotero_launcher

    def _fail(**kwargs):
        raise OSError("no exe")

    monkeypatch.setattr(zotero_launcher, "start_zotero", _fail)
    result = zpi.install_with_close(
        _CLOSE_ON, close_zotero=True, running=_states(True),
        close=lambda running=None: "closed", install=lambda: {"status": "installed", "path": "P"},
    )
    assert result["status"] == "installed"
    assert result["restart"] == "error"


# settings: the close_zotero_for_install key -------------------------------------


def test_close_setting_defaults_to_true_and_a_legacy_file_keeps_its_decision(tmp_path):
    path = _write(tmp_path / "zotero_plugin.json", '{"autoinstall": true, "decided": true}')
    assert zpi.load_settings(path) == {"autoinstall": True, "decided": True, "close_zotero_for_install": True}


def test_close_setting_round_trips_as_false(tmp_path):
    path = tmp_path / "zotero_plugin.json"
    zpi.save_settings(path, {"autoinstall": True, "decided": True, "close_zotero_for_install": False})
    assert zpi.load_settings(path) == {"autoinstall": True, "decided": True, "close_zotero_for_install": False}
    assert json.loads(path.read_text(encoding="utf-8"))["close_zotero_for_install"] is False


def test_a_non_boolean_close_setting_makes_the_whole_file_wrong_shape(tmp_path):
    path = _write(
        tmp_path / "zotero_plugin.json",
        '{"autoinstall": true, "decided": true, "close_zotero_for_install": "no"}',
    )
    assert zpi.load_settings(path) == {"autoinstall": False, "decided": False, "close_zotero_for_install": True}


def test_save_refuses_a_non_boolean_close_setting(tmp_path):
    path = tmp_path / "zotero_plugin.json"
    with pytest.raises(ValueError):
        zpi.save_settings(path, {"autoinstall": False, "decided": True, "close_zotero_for_install": "no"})
    assert not path.exists()


def test_first_run_choice_keeps_the_saved_close_setting():
    original = {"autoinstall": False, "decided": False, "close_zotero_for_install": False}
    assert zpi.apply_first_run_choice(original, True) == {
        "autoinstall": True, "decided": True, "close_zotero_for_install": False,
    }


@pytest.mark.parametrize(
    ("env", "saved", "expected"),
    [
        ({CLOSE_ENV: "0"}, True, False),
        ({CLOSE_ENV: "off"}, True, False),
        ({CLOSE_ENV: "on"}, True, True),
        ({CLOSE_ENV: "on"}, False, False),
        ({CLOSE_ENV: "1"}, False, False),
        ({CLOSE_ENV: "yes"}, False, False),
        ({CLOSE_ENV: "maybe"}, False, False),
        ({CLOSE_ENV: "maybe"}, True, True),
        ({}, True, True),
        ({}, False, False),
        ({CLOSE_ENV: ""}, False, False),
    ],
)
def test_the_close_env_can_turn_closing_off_but_never_on(env, saved, expected):
    settings = {"autoinstall": False, "decided": True, "close_zotero_for_install": saved}
    assert zpi.effective_close_zotero(settings, env) is expected


@pytest.mark.parametrize(
    ("raw", "disables"),
    [("0", True), ("off", True), ("NO", True), ("1", False), ("on", False), ("maybe", False), ("", False)],
)
def test_only_an_off_value_of_the_close_env_disables_closing(raw, disables):
    assert zpi.close_zotero_env_disables({CLOSE_ENV: raw}) is disables


def test_close_env_override_is_reported_only_for_recognised_values():
    assert zpi.close_zotero_env_override({CLOSE_ENV: "off"}) is False
    assert zpi.close_zotero_env_override({CLOSE_ENV: "YES"}) is True
    assert zpi.close_zotero_env_override({CLOSE_ENV: "maybe"}) is None
    assert zpi.close_zotero_env_override({}) is None


# CLI: --keep-zotero-open and the saved setting ----------------------------------


def test_cli_keep_zotero_open_refuses_and_never_closes(monkeypatch, capsys, profile):
    monkeypatch.setattr(zpi, "zotero_process_state", lambda runner=None: True)
    monkeypatch.setattr(zpi, "close_zotero_gracefully", lambda **kwargs: pytest.fail("the close must not run"))
    rc = zpi.cli_main(["--profile", str(profile), "--keep-zotero-open"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "zotero is running" in (captured.out + captured.err).lower()
    assert not (profile / "extensions").exists()


def test_cli_closes_installs_and_restarts_by_default(monkeypatch, capsys, profile):
    from meridian import zotero_launcher

    monkeypatch.setattr(zpi, "zotero_process_state", _states(True, False))
    closed = []

    def _close(**kwargs):
        closed.append(kwargs)
        return "closed"

    monkeypatch.setattr(zpi, "close_zotero_gracefully", _close)
    monkeypatch.setattr(
        zotero_launcher, "start_zotero", lambda **kwargs: {"action": "started", "exe": "Z", "api_ready": True},
    )
    rc = zpi.cli_main(["--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 0
    assert len(closed) == 1
    assert (profile / "extensions" / XPI_NAME).read_bytes() == zpi.build_xpi_bytes()
    assert "restarted" in out.lower()


def test_cli_follows_a_saved_close_setting_of_false(monkeypatch, capsys, profile):
    zpi.save_settings(zpi.settings_path(), {"autoinstall": False, "decided": True, "close_zotero_for_install": False})
    monkeypatch.setattr(zpi, "zotero_process_state", lambda runner=None: True)
    monkeypatch.setattr(zpi, "close_zotero_gracefully", lambda **kwargs: pytest.fail("the close must not run"))
    rc = zpi.cli_main(["--profile", str(profile)])
    assert rc == 1
    assert "zotero is running" in capsys.readouterr().out.lower()


def test_cli_reports_close_failed_with_the_shared_text_and_writes_nothing(monkeypatch, capsys, profile):
    monkeypatch.setattr(zpi, "zotero_process_state", lambda runner=None: True)
    monkeypatch.setattr(zpi, "close_zotero_gracefully", lambda **kwargs: "close_failed")
    before = _snapshot(profile)
    rc = zpi.cli_main(["--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 1
    assert out == zpi.CLOSE_FAILED_MESSAGE + "\n"
    assert _snapshot(profile) == before


@pytest.mark.parametrize(
    ("restart", "expected"),
    [
        ("started", "Meridian closed Zotero and restarted it."),
        ("already_running", "Meridian closed Zotero and restarted it."),
        ("error", "Meridian closed Zotero, but could not restart it. Start Zotero yourself."),
        ("timeout", "Meridian closed Zotero, but could not restart it. Start Zotero yourself."),
        (None, "Meridian closed Zotero, but could not restart it. Start Zotero yourself."),
    ],
)
def test_the_cli_restart_sentence_says_closed_and_restarted_or_that_the_restart_failed(restart, expected):
    assert zpi._restart_sentence(restart) == expected


def test_cli_a_failed_install_after_closing_says_zotero_was_restarted_and_never_that_it_installed(
    monkeypatch, capsys, profile,
):
    from meridian import zotero_launcher

    monkeypatch.setattr(zpi, "zotero_process_state", _states(True, False))
    monkeypatch.setattr(zpi, "close_zotero_gracefully", lambda **kwargs: "closed")
    monkeypatch.setattr(
        zotero_launcher, "start_zotero", lambda **kwargs: {"action": "started", "exe": "Z", "api_ready": True},
    )
    monkeypatch.setattr(
        zpi, "install_into_profile",
        lambda profile_dir, xpi_path, **kwargs: {"status": "failed", "path": None, "error": "disk full"},
    )
    rc = zpi.cli_main(["--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "The install failed: disk full" in out
    assert "Meridian closed Zotero and restarted it." in out
    assert "Installed the Zotero bridge plugin" not in out


def test_cli_says_when_it_closed_zotero_but_could_not_restart_it(monkeypatch, capsys, profile):
    from meridian import zotero_launcher

    monkeypatch.setattr(zpi, "zotero_process_state", _states(True, False))
    monkeypatch.setattr(zpi, "close_zotero_gracefully", lambda **kwargs: "closed")

    def _no_start(**kwargs):
        raise OSError("no exe")

    monkeypatch.setattr(zotero_launcher, "start_zotero", _no_start)
    rc = zpi.cli_main(["--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Installed the Zotero bridge plugin" in out
    assert "Meridian closed Zotero, but could not restart it. Start Zotero yourself." in out


# docs ---------------------------------------------------------------------------


def test_docs_say_closing_is_part_of_the_install_and_never_forced():
    docs = (REPO_ROOT / "docs" / "zotero-plugin-install.md").read_text(encoding="utf-8")
    assert "close_zotero_for_install" in docs
    assert "Close Zotero to install the plugin" in docs
    assert "--keep-zotero-open" in docs
    assert "never force-closes Zotero" in docs
    assert "Windows" in docs
    assert "the taskkill argument list has no force flag" in docs
    assert "it never uses a kill command" not in docs
    assert "can still time out" in docs
    assert "can only turn closing off" in docs
    assert "`1`, `true`, `yes` and `on` turn it on" not in docs
    assert "update_url" in docs


def test_the_bridge_readme_says_the_install_command_closes_zotero_and_can_time_out():
    readme = (REPO_EXTENSION / "README.md").read_text(encoding="utf-8")
    assert "--keep-zotero-open" in readme
    assert "MERIDIAN_ZOTERO_CLOSE_ON_INSTALL" in readme
    assert "can still time out" in readme


_ALREADY_CURRENT_SENTENCE = "An install that finds the plugin already current does not close Zotero."


def test_docs_say_an_install_that_finds_the_plugin_already_current_does_not_close_zotero():
    docs = (REPO_ROOT / "docs" / "zotero-plugin-install.md").read_text(encoding="utf-8")
    assert _ALREADY_CURRENT_SENTENCE in docs


def test_the_bridge_readme_says_an_install_that_finds_the_plugin_already_current_does_not_close_zotero():
    readme = (REPO_EXTENSION / "README.md").read_text(encoding="utf-8")
    assert _ALREADY_CURRENT_SENTENCE in readme


# ---------------------------------------------------------------------------
# The signing gate: an unsigned plugin version is never installed (see test_zotero_plugin_sign.py)
# ---------------------------------------------------------------------------


def test_install_default_profile_reports_needs_signature_when_nothing_is_stored(default_profile_env):
    _forget_signed(zpi.signing_state_dir())
    before = _snapshot(default_profile_env)
    result = zpi.install_default_profile(running=lambda: False)
    assert result["status"] == "needs_signature"
    assert result["unsigned_path"] == str(zpi.default_xpi_path())
    assert "This plugin version is not signed yet." in result["message"]
    assert _snapshot(default_profile_env) == before


def test_default_install_with_close_refuses_an_unsigned_plugin_before_closing(default_profile_env):
    _forget_signed(zpi.signing_state_dir())
    before = _snapshot(default_profile_env)
    result = zpi.install_default_with_close(
        _CLOSE_ON, close_zotero=True, running=lambda: True,
        close=lambda running=None: pytest.fail("Zotero must not be closed for an unsigned plugin"),
        restart=lambda: pytest.fail("nothing is restarted when Meridian closed nothing"),
    )
    assert result["status"] == "needs_signature"
    assert result["closed_zotero"] is False
    assert result["restart"] is None
    assert _snapshot(default_profile_env) == before


def test_cli_install_without_a_signature_closes_nothing_and_points_at_sign_plugin(monkeypatch, capsys, profile):
    _forget_signed(zpi.signing_state_dir())
    monkeypatch.setattr(zpi, "close_zotero_gracefully", lambda **kwargs: pytest.fail("no close without a signature"))
    before = _snapshot(profile)
    rc = zpi.cli_main(["--profile", str(profile)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "python -m meridian zotero sign-plugin" in out
    assert "Tools > Plugins" in out
    assert "Install Plugin From File" in out
    assert "Meridian closed" not in out
    assert _snapshot(profile) == before
    assert not (profile / "extensions").exists()


def test_cli_dry_run_says_the_unsigned_file_would_be_written_and_writes_nothing(capsys, profile):
    _forget_signed(zpi.signing_state_dir())
    before = _snapshot(profile)
    rc = zpi.cli_main(["--profile", str(profile), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "not signed yet" in out
    assert "would be written" in out
    assert str(zpi.default_xpi_path()) in out
    assert "shown here" not in out
    assert "Select:" not in out
    assert "Would install" not in out
    assert not zpi.default_xpi_path().exists()
    assert _snapshot(profile) == before
    assert not (profile / "extensions").exists()


def test_cli_dry_run_refuses_a_version_already_submitted_without_a_signed_file(capsys, profile):
    from meridian import zotero_plugin_sign as sign

    bundle = zpi._bundled_source_dir()
    state = zpi.signing_state_dir()
    _forget_signed(state)
    sign.record_submission(state, sign.bundle_version(bundle), sign.source_fingerprint(bundle), clock=lambda: 0.0)
    before = _snapshot(profile)
    rc = zpi.cli_main(["--profile", str(profile), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Would refuse." in out
    assert "Bump the version in manifest.json to sign a new build." in out
    assert "not signed yet" not in out
    assert _snapshot(profile) == before


def test_cli_installs_the_signed_bytes_and_not_the_unsigned_build(monkeypatch, capsys, profile):
    signed_payload = b"PK-stand-in-for-mozilla-signed-bytes"
    _seed_signed(zpi.signing_state_dir(), zpi._bundled_source_dir(), signed_payload)
    monkeypatch.setattr(zpi, "zotero_process_state", lambda runner=None: False)
    assert zpi.cli_main(["--profile", str(profile)]) == 0
    assert (profile / "extensions" / XPI_NAME).read_bytes() == signed_payload
    assert signed_payload != zpi.build_xpi_bytes()
