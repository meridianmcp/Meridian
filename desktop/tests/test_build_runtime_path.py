"""PyInstaller searches the build env's own runtime folder before any other folder on PATH.

Regression for the desk-bundle-fix run. A plain `python packaging/build.py` did not put the conda env's
Library/bin on PATH, so PyInstaller could not resolve libcrypto, libssl, libffi, expat, bzip2 or XZ, and it
took sqlite3.dll from another program's folder. Uses synthetic folders in tmp_path only.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def load_build():
    spec = importlib.util.spec_from_file_location("build_runtime_path_under_test", REPO / "packaging" / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_env_runtime_folder_comes_first_on_path(tmp_path, monkeypatch):
    build = load_build()
    prefix = tmp_path / "env"
    runtime = prefix / "Library" / "bin"
    runtime.mkdir(parents=True)
    decoy = tmp_path / "other-program"
    decoy.mkdir()
    (decoy / "sqlite3.dll").write_bytes(b"not the pinned copy")
    other = tmp_path / "more"
    monkeypatch.setenv("PATH", os.pathsep.join([str(decoy), str(other)]))

    parts = build.pyinstaller_env(prefix=prefix)["PATH"].split(os.pathsep)

    assert parts == [str(runtime), str(decoy), str(other)]


def test_path_is_unchanged_when_the_env_has_no_runtime_folder(tmp_path, monkeypatch):
    build = load_build()
    monkeypatch.setenv("PATH", str(tmp_path / "other"))

    assert build.pyinstaller_env(prefix=tmp_path / "no-such-env")["PATH"] == str(tmp_path / "other")


def test_runtime_folder_is_the_env_library_bin(tmp_path):
    build = load_build()

    assert build.runtime_bin(tmp_path) == tmp_path / "Library" / "bin"
