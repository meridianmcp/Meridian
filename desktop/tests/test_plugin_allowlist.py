"""verify_bundle rejects a plugin file that sits in an allowed folder but is not on the file allowlist."""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def load_verifier():
    spec = importlib.util.spec_from_file_location("verify_under_test", REPO / "packaging" / "verify_bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MANIFEST = {
    "qt": {
        "allowed_dlls": ["Qt6Core", "Qt6Gui", "Qt6Widgets"],
        "forbidden_dlls": [],
        "allowed_plugin_dirs": ["platforms", "styles"],
        "allowed_plugin_files": ["platforms/qwindows.dll", "styles/qmodernwindowsstyle.dll"],
        "allowed_non_qt_pyd": [],
    }
}


def make_tree(root: Path, files: list[str]) -> None:
    for rel in files:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")


def test_unlisted_file_in_allowed_folder_is_a_problem(tmp_path):
    verify = load_verifier()
    make_tree(tmp_path, [
        "PySide6/Qt6Core.dll",
        "PySide6/plugins/platforms/qwindows.dll",
        "PySide6/plugins/platforms/qminimal.dll",
    ])
    problems, _ = verify.check_qt_modules(tmp_path, MANIFEST)
    assert any("qminimal.dll" in p and "not on allowed list" in p for p in problems), problems
    assert not any("qwindows.dll" in p for p in problems), problems


def test_missing_file_allowlist_fails_closed(tmp_path):
    verify = load_verifier()
    manifest = {"qt": {k: v for k, v in MANIFEST["qt"].items() if k != "allowed_plugin_files"}}
    make_tree(tmp_path, [
        "PySide6/Qt6Core.dll",
        "PySide6/plugins/platforms/qwindows.dll",
    ])
    problems, _ = verify.check_qt_modules(tmp_path, manifest)
    assert any("qwindows.dll" in p and "not on allowed list" in p for p in problems), problems
