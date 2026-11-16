"""Every DLL in the installed PySide6 folder is classified, and the allowed set is closed.

Runs against the PySide6 the desktop environment imports, so no build is needed.
Missing PySide6 or an unreadable PE file fails the test (fail closed).
"""

from __future__ import annotations

import sys
import tomllib
from importlib import util
from pathlib import Path

import pefile
import pytest

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "packaging" / "lgpl_manifest.toml"

# Every test here reads Windows PE binaries (.dll and .pyd). Off Windows they have nothing to read.
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="checks Windows PE binaries; runs on Windows only")


def pyside_dir() -> Path:
    spec = util.find_spec("PySide6")
    assert spec is not None and spec.submodule_search_locations, "PySide6 is not installed (fail closed)"
    return Path(next(iter(spec.submodule_search_locations)))


def load_qt() -> dict:
    return tomllib.loads(MANIFEST.read_text(encoding="utf-8"))["qt"]


def imports_of(path: Path) -> list[str]:
    pe = pefile.PE(str(path), fast_load=True)
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
    names = [entry.dll.decode("ascii", "replace") for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])]
    pe.close()
    return names


def test_every_qt6_dll_is_classified():
    qt = load_qt()
    allowed, forbidden = set(qt["allowed_dlls"]), set(qt["forbidden_dlls"])
    stems = {p.stem for p in pyside_dir().glob("*.dll") if p.stem.startswith("Qt6")}
    assert not (stems - allowed - forbidden), f"unclassified Qt6 DLLs: {sorted(stems - allowed - forbidden)}"
    assert not (allowed & forbidden), f"listed as both allowed and forbidden: {sorted(allowed & forbidden)}"
    assert not ((allowed | forbidden) - stems), f"manifest names not in folder: {sorted((allowed | forbidden) - stems)}"


def test_every_non_qt_dll_is_classified():
    qt = load_qt()
    allowed, forbidden = set(qt["allowed_other_dlls"]), set(qt["forbidden_other_dlls"])
    stems = {p.stem for p in pyside_dir().glob("*.dll") if not p.stem.startswith("Qt6")}
    assert not (stems - allowed - forbidden), f"unclassified non-Qt DLLs: {sorted(stems - allowed - forbidden)}"
    assert not (allowed & forbidden), f"listed as both allowed and forbidden: {sorted(allowed & forbidden)}"
    assert not ((allowed | forbidden) - stems), f"manifest names not in folder: {sorted((allowed | forbidden) - stems)}"


def test_allowed_set_is_closed_under_imports():
    root = pyside_dir()
    qt = load_qt()
    by_name = {p.name.lower(): p for p in root.glob("*.dll")}
    seeds = [root / "plugins" / "platforms" / "qwindows.dll", root / "plugins" / "styles" / "qmodernwindowsstyle.dll"]
    seeds += [by_name[f"{name.lower()}.dll"] for name in qt["allowed_dlls"]]
    seen: set[str] = set()
    queue = list(seeds)
    while queue:
        path = queue.pop()
        if path.name.lower() in seen:
            continue
        seen.add(path.name.lower())
        for dep in imports_of(path):
            if dep.lower() in by_name:
                queue.append(by_name[dep.lower()])
    allowed_lower = {name.lower() for name in qt["allowed_dlls"]}
    qt_reached = {Path(name).stem for name in seen if name.startswith("qt6")}
    assert qt_reached <= allowed_lower, f"allowed set pulls in outside modules: {sorted(qt_reached - allowed_lower)}"
    assert allowed_lower <= qt_reached, f"an allowed DLL is not reached from the app's imports: {sorted(allowed_lower - qt_reached)}"


def test_allowed_extensions_are_exactly_the_three_app_modules():
    qt = load_qt()
    allowed = set(qt["allowed_dlls"])
    mapped = set()
    for pyd in pyside_dir().glob("*.pyd"):
        if pyd.stem.startswith("Qt"):
            dll = "Qt6" + pyd.stem[2:]
            if dll in allowed:
                mapped.add(pyd.name.lower())
    assert mapped == {"qtcore.pyd", "qtgui.pyd", "qtwidgets.pyd"}, f"allowed extensions: {sorted(mapped)}"
