"""prune_forbidden removes exactly the forbidden files and unlisted plugins, and fails closed.

Uses a fake bundle tree in tmp_path. The real build is checked by verify_bundle, not here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def load_build():
    spec = importlib.util.spec_from_file_location("build_under_test", REPO / "packaging" / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


QT = {
    "allowed_dlls": ["Qt6Core", "Qt6Gui", "Qt6Widgets"],
    "forbidden_dlls": ["Qt6Network", "Qt6Svg"],
    "forbidden_other_dlls": ["opengl32sw"],
    "allowed_plugin_files": ["platforms/qwindows.dll", "styles/qmodernwindowsstyle.dll"],
}

TREE = [
    "PySide6/Qt6Core.dll",
    "PySide6/Qt6Gui.dll",
    "PySide6/Qt6Widgets.dll",
    "PySide6/Qt6Network.dll",
    "PySide6/Qt6Svg.dll",
    "PySide6/QtCore.pyd",
    "PySide6/QtNetwork.pyd",
    "PySide6/QtSvg.pyd",
    "PySide6/opengl32sw.dll",
    "msvcp140.dll",
    "PySide6/plugins/platforms/qwindows.dll",
    "PySide6/plugins/platforms/qminimal.dll",
    "PySide6/plugins/imageformats/qjpeg.dll",
    "PySide6/plugins/styles/qmodernwindowsstyle.dll",
]

REMOVED = {
    "PySide6/Qt6Network.dll",
    "PySide6/Qt6Svg.dll",
    "PySide6/QtNetwork.pyd",
    "PySide6/QtSvg.pyd",
    "PySide6/opengl32sw.dll",
    "PySide6/plugins/platforms/qminimal.dll",
    "PySide6/plugins/imageformats/qjpeg.dll",
}

KEPT = set(TREE) - REMOVED


def make_tree(root: Path) -> None:
    for rel in TREE:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")


def test_prune_removes_exactly_the_forbidden_files(tmp_path):
    build = load_build()
    make_tree(tmp_path)
    removed = set(build.prune_forbidden(tmp_path, QT))
    assert removed == REMOVED
    for rel in REMOVED:
        assert not (tmp_path / rel).exists(), rel
    for rel in KEPT:
        assert (tmp_path / rel).is_file(), rel


def test_prune_fails_closed_on_a_missing_manifest_key(tmp_path):
    build = load_build()
    make_tree(tmp_path)
    incomplete = {key: value for key, value in QT.items() if key != "allowed_plugin_files"}
    with pytest.raises(KeyError):
        build.prune_forbidden(tmp_path, incomplete)
    assert (tmp_path / "PySide6/Qt6Network.dll").is_file(), "nothing may be deleted before the manifest is read"
