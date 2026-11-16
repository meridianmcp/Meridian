"""Tests for packaging/make_release.py: the release zip holds the bundle, both install scripts, and no secret-named file.

A fake bundle folder stands in for dist/MeridianDesktop, so no PyInstaller run and no real dist folder is touched.
The fake secret-named files hold placeholder text only.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "packaging" / "release"


def _load():
    name = "make_release_under_test"
    spec = importlib.util.spec_from_file_location(name, REPO / "packaging" / "make_release.py")
    module = importlib.util.module_from_spec(spec)
    # Registered first: a dataclass looks its module up in sys.modules while the module runs.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mr = _load()


def _bundle(root: Path) -> Path:
    bundle = root / "MeridianDesktop"
    (bundle / "_internal" / "PySide6").mkdir(parents=True)
    (bundle / "MeridianDesktop.exe").write_bytes(b"MZ-fake")
    (bundle / "_internal" / "base.dll").write_bytes(b"dll")
    (bundle / "_internal" / "PySide6" / "QtCore.pyd").write_bytes(b"pyd")
    (bundle / "README.txt").write_text("readme", encoding="utf-8")
    # Secret-named files that must never reach the zip, at the top and nested.
    (bundle / ".env").write_text("placeholder", encoding="utf-8")
    (bundle / ".env.local").write_text("placeholder", encoding="utf-8")
    (bundle / "meridian.toml").write_text("placeholder", encoding="utf-8")
    (bundle / "_internal" / "nested").mkdir()
    (bundle / "_internal" / "nested" / ".env").write_text("placeholder", encoding="utf-8")
    return bundle


def _pixi(root: Path, version: str = "0.1.0") -> Path:
    path = root / "pixi.toml"
    path.write_text(f'[workspace]\nname = "meridian-desktop"\nversion = "{version}"\n', encoding="utf-8")
    return path


def _names(zip_path: Path) -> set[str]:
    with zipfile.ZipFile(zip_path) as archive:
        return set(archive.namelist())


def test_zip_holds_the_bundle_at_its_root_and_both_scripts(tmp_path):
    result = mr.build_release(_bundle(tmp_path), SCRIPTS, tmp_path / "release", "0.1.0")

    assert result.path == tmp_path / "release" / "MeridianDesktop-0.1.0.zip"
    assert result.path.is_file()
    names = _names(result.path)
    assert {
        "MeridianDesktop.exe",
        "_internal/base.dll",
        "_internal/PySide6/QtCore.pyd",
        "README.txt",
        "install.cmd",
        "uninstall.cmd",
    } <= names
    assert set(result.files) == names


def test_secret_named_files_are_left_out_and_listed(tmp_path):
    result = mr.build_release(_bundle(tmp_path), SCRIPTS, tmp_path / "release", "0.1.0")

    names = _names(result.path)
    for name in names:
        leaf = name.rsplit("/", 1)[-1]
        assert leaf not in {".env", ".env.local", "meridian.toml"}, name
        assert not leaf.endswith(".env"), name
    assert result.excluded == (
        ".env",
        ".env.local",
        "_internal/nested/.env",
        "meridian.toml",
    )


def test_scripts_are_crlf_and_run_the_exe_from_their_own_folder(tmp_path):
    result = mr.build_release(_bundle(tmp_path), SCRIPTS, tmp_path / "release", "0.1.0")

    with zipfile.ZipFile(result.path) as archive:
        install = archive.read("install.cmd")
        uninstall = archive.read("uninstall.cmd")

    for data in (install, uninstall):
        assert data.count(b"\r\n") > 5
        assert data.count(b"\n") == data.count(b"\r\n")  # no bare LF
    assert b'"%~dp0MeridianDesktop.exe" --install --desktop' in install
    assert b'"%~dp0MeridianDesktop.exe" --uninstall' in uninstall
    # The scripts must not carry a machine-specific path.
    for data in (install, uninstall):
        assert b"C:\\" not in data
        assert b"Users" not in data


def test_the_real_scripts_say_what_they_run():
    install = (SCRIPTS / "install.cmd").read_text(encoding="utf-8")
    uninstall = (SCRIPTS / "uninstall.cmd").read_text(encoding="utf-8")
    assert '%~dp0MeridianDesktop.exe" --install --desktop' in install
    assert "cd /d" in install
    assert '%~dp0MeridianDesktop.exe" --uninstall' in uninstall


def test_version_comes_from_the_workspace_table_of_pixi_toml(tmp_path):
    assert mr.read_version(_pixi(tmp_path, "2.4.1")) == "2.4.1"
    assert re.fullmatch(r"\d+\.\d+\.\d+.*", mr.read_version(REPO / "pixi.toml"))


@pytest.mark.parametrize("version", ["latest", "1.2.3-rc1", "1.2.3+build", "v1.2.3", "1.2.3 beta", ""])
def test_a_version_that_is_not_a_plain_number_is_refused(tmp_path, version):
    with pytest.raises(mr.ReleaseError, match="plain number"):
        mr.read_version(_pixi(tmp_path, version))


@pytest.mark.parametrize("version", ["1.2", "7", "10.20.30"])
def test_plain_dotted_integers_are_accepted_as_the_updater_accepts_them(tmp_path, version):
    """P8: the same rule as meridian_desktop/updater.py _VERSION: [0-9]+ parts joined by dots."""
    assert mr.read_version(_pixi(tmp_path, version)) == version


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_a_bundle_holding_a_link_is_refused_before_anything_is_zipped(tmp_path, kind):
    """P7: a symbolic link inside the bundle could point outside it, so the build stops before zipping."""
    bundle = _bundle(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = bundle / f"linked-{kind}"
    try:
        link.symlink_to(outside / "file.txt" if kind == "file" else outside, target_is_directory=kind == "directory")
    except (OSError, NotImplementedError):
        pytest.skip("this machine cannot create symbolic links")
    out = tmp_path / "release"
    with pytest.raises(mr.ReleaseError, match=f"holds a link, linked-{kind}") as caught:
        mr.build_release(bundle, SCRIPTS, out, "0.1.0")
    assert "Nothing was zipped" in str(caught.value)
    assert not out.exists()


def test_a_link_is_refused_even_where_this_machine_cannot_create_one(tmp_path, monkeypatch):
    """P7 without a real link: the detection hook reports one, and the build must still stop before zipping."""
    bundle = _bundle(tmp_path)
    monkeypatch.setattr(mr, "_is_link", lambda path: path.name == "MeridianDesktop.exe")
    out = tmp_path / "release"
    with pytest.raises(mr.ReleaseError, match="holds a link, MeridianDesktop.exe"):
        mr.build_release(bundle, SCRIPTS, out, "0.1.0")
    assert not out.exists()


def test_a_bundle_without_links_is_still_zipped(tmp_path):
    result = mr.build_release(_bundle(tmp_path), SCRIPTS, tmp_path / "release", "0.1.0")
    assert result.path.is_file()


def test_main_names_the_zip_after_the_version_in_pixi_toml(tmp_path, capsys):
    code = mr.main(
        [
            "--bundle",
            str(_bundle(tmp_path)),
            "--out",
            str(tmp_path / "release"),
            "--scripts",
            str(SCRIPTS),
            "--pixi",
            str(_pixi(tmp_path, "3.0.1")),
        ]
    )

    assert code == 0
    assert (tmp_path / "release" / "MeridianDesktop-3.0.1.zip").is_file()
    out = capsys.readouterr().out
    assert "left out (secret-named): meridian.toml" in out
    assert "wrote" in out


def test_a_missing_exe_is_refused_with_the_next_step(tmp_path):
    bundle = _bundle(tmp_path)
    (bundle / "MeridianDesktop.exe").unlink()
    with pytest.raises(mr.ReleaseError, match="build the bundle first"):
        mr.build_release(bundle, SCRIPTS, tmp_path / "release", "0.1.0")
    assert not (tmp_path / "release").exists() or not any((tmp_path / "release").iterdir())


def test_main_returns_1_when_the_bundle_is_missing(tmp_path, capsys):
    code = mr.main(
        [
            "--bundle",
            str(tmp_path / "nowhere"),
            "--out",
            str(tmp_path / "release"),
            "--scripts",
            str(SCRIPTS),
            "--pixi",
            str(_pixi(tmp_path)),
        ]
    )
    assert code == 1
    assert "release failed" in capsys.readouterr().err


def test_a_missing_script_is_refused(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "install.cmd").write_text("@echo off\r\n", encoding="utf-8")
    with pytest.raises(mr.ReleaseError, match="uninstall.cmd"):
        mr.build_release(_bundle(tmp_path), scripts, tmp_path / "release", "0.1.0")


def test_a_bundle_that_already_holds_a_script_name_is_refused(tmp_path):
    bundle = _bundle(tmp_path)
    (bundle / "install.cmd").write_text("old", encoding="utf-8")
    with pytest.raises(mr.ReleaseError, match="already holds install.cmd"):
        mr.build_release(bundle, SCRIPTS, tmp_path / "release", "0.1.0")


def test_rebuilding_replaces_the_old_zip_and_leaves_no_temp_file(tmp_path):
    out = tmp_path / "release"
    mr.build_release(_bundle(tmp_path), SCRIPTS, out, "0.1.0")
    (tmp_path / "MeridianDesktop" / "NEW.txt").write_text("new", encoding="utf-8")
    result = mr.build_release(tmp_path / "MeridianDesktop", SCRIPTS, out, "0.1.0")

    assert "NEW.txt" in _names(result.path)
    assert sorted(path.name for path in out.iterdir()) == ["MeridianDesktop-0.1.0.zip"]
