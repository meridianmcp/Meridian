"""Tests for packaging/verify_bundle.py.

Covers the stdlib licence inventory, PYZ scanning with PyInstaller's own reader,
the archive scratch location, hashes writing, and fail-closed behaviour (FAIL
lines, never tracebacks). Bundles here are small fakes built in tmp_path.
"""

import importlib.metadata
import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("verify_bundle", REPO / "packaging" / "verify_bundle.py")
vb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vb)

MANIFEST = REPO / "packaging" / "lgpl_manifest.toml"
LGPL = "LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only"
# Assembled from pieces: the test file must not hold the marker as one string.
SECRET_SOURCE = "TOKEN = 'sk_meri' 'dian_FAKE_FOR_TEST'\n"
CLEAN_SOURCE = "GREETING = 'hello'\n"


# ---------------------------------------------------------------- fakes


class _FakeMetadata:
    def __init__(self, values, classifiers=()):
        self._values = dict(values)
        self._classifiers = list(classifiers)

    def get(self, key, default=None):
        return self._values.get(key, default)

    def get_all(self, key, default=None):
        if key == "Classifier" and self._classifiers:
            return list(self._classifiers)
        return default


def _fake_dist(name, version="1.0", fields=None, classifiers=()):
    values = {"Name": name, "Version": version}
    values.update(fields or {})
    return types.SimpleNamespace(metadata=_FakeMetadata(values, classifiers))


def _patch_distributions(monkeypatch, dists):
    monkeypatch.setattr(importlib.metadata, "distributions", lambda: list(dists))


def _write_pyz(tmp_path: Path, source: str, name: str = "leaky") -> Path:
    """Write a PYZ with PyInstaller's own ZlibArchiveWriter, holding one module."""
    from PyInstaller.archive.writers import ZlibArchiveWriter

    src = tmp_path / f"{name}.py"
    src.write_text(source, encoding="utf-8")
    code = compile(source, str(src), "exec")
    pyz = tmp_path / "PYZ-00.pyz"
    ZlibArchiveWriter(str(pyz), [(name, str(src), "PYMODULE")], {name: code})
    return pyz


def _broken_bundle(tmp_path: Path) -> Path:
    """A dist folder whose exe and DLLs are not PE files."""
    dist = tmp_path / "dist" / "MeridianDesktop"
    dist.mkdir(parents=True)
    (dist / "MeridianDesktop.exe").write_bytes(b"not a PE file " * 10)
    return dist


# ---------------------------------------------------------------- licence inventory (stdlib)


def test_inventory_uses_license_field(monkeypatch):
    _patch_distributions(monkeypatch, [_fake_dist("PySide6", "6.12.0", {"License": LGPL})])
    assert vb.run_license_inventory() == [{"Name": "PySide6", "Version": "6.12.0", "License": LGPL}]


def test_inventory_falls_back_to_license_expression(monkeypatch):
    _patch_distributions(monkeypatch, [_fake_dist("exprpkg", "1.0", {"License-Expression": "MIT"})])
    assert vb.run_license_inventory() == [{"Name": "exprpkg", "Version": "1.0", "License": "MIT"}]


def test_inventory_falls_back_to_license_classifiers(monkeypatch):
    _patch_distributions(
        monkeypatch,
        [
            _fake_dist(
                "classpkg",
                "2.0",
                classifiers=["Programming Language :: Python :: 3", "License :: OSI Approved :: MIT License"],
            )
        ],
    )
    assert vb.run_license_inventory() == [{"Name": "classpkg", "Version": "2.0", "License": "MIT License"}]


def test_inventory_with_no_licence_fails_closed(monkeypatch):
    _patch_distributions(monkeypatch, [_fake_dist("PySide6", "6.12.0")])
    rows = vb.run_license_inventory()
    assert rows == [{"Name": "PySide6", "Version": "6.12.0", "License": ""}]
    manifest = vb.load_manifest(MANIFEST)
    problems = vb.check_licenses(rows, manifest)
    assert any("unknown licence" in p and "pyside6" in p for p in problems)


def test_real_environment_inventory_gives_the_family_licence():
    manifest = vb.load_manifest(MANIFEST)
    rows = vb.run_license_inventory()
    pyside = [r for r in rows if vb.normalize_dist_name(r["Name"]) == "pyside6"]
    assert pyside and pyside[0]["License"] == LGPL
    assert vb.check_licenses(rows, manifest) == []


def test_no_pip_licenses_reference_remains():
    files = [
        REPO / "packaging" / "verify_bundle.py",
        REPO / "packaging" / "lgpl_manifest.toml",
        REPO / "packaging" / "build.py",
        REPO / "NOTICE.md",
        REPO / "pyproject.toml",
        REPO / "README.md",
    ]
    for path in files:
        text = path.read_text(encoding="utf-8").lower()
        for needle in ("pip-licenses", "piplicenses", "pip_licenses"):
            assert needle not in text, f"{path.name} still names {needle}"


# ---------------------------------------------------------------- archive scratch (a)


def test_archive_scratch_is_repo_build_not_dist():
    assert vb.ARCHIVE_SCRATCH == REPO / "build" / "archive-scan"
    assert not vb.ARCHIVE_SCRATCH.is_relative_to(REPO / "dist")


def test_broken_bundle_creates_no_scratch_under_dist(tmp_path, capsys):
    dist = _broken_bundle(tmp_path)
    vb.run(dist, MANIFEST, tmp_path / "report.json", write=False)
    assert not (tmp_path / "dist" / "build").exists()


# ---------------------------------------------------------------- PYZ contents (b)


def test_pyz_scan_finds_secret_inside_compressed_code(tmp_path):
    pyz = _write_pyz(tmp_path, SECRET_SOURCE)
    problems = vb.scan_pyz_files([pyz])
    assert any("secret-shaped" in p and "PYZ-00.pyz:leaky" in p for p in problems), problems


def test_raw_byte_scan_misses_the_same_secret(tmp_path):
    # Why the PYZ scan exists: the marker is only in the compressed module bytes.
    _write_pyz(tmp_path, SECRET_SOURCE)
    (tmp_path / "leaky.py").unlink()
    assert vb.scan_bundle_bytes(tmp_path, vb.SECRET_MARKERS, case_insensitive=False) == []


def test_clean_pyz_scans_clean(tmp_path):
    pyz = _write_pyz(tmp_path, CLEAN_SOURCE)
    assert vb.scan_pyz_files([pyz]) == []


def test_unreadable_pyz_is_reported_not_skipped(tmp_path):
    bad = tmp_path / "PYZ-00.pyz"
    bad.write_bytes(b"not a PYZ archive" * 4)
    problems = vb.scan_pyz_files([bad])
    assert problems and "PYZ contents not scanned" in problems[0]


def test_exe_without_pyz_fails_closed(tmp_path):
    dist = _broken_bundle(tmp_path)
    problems = vb.check_pyinstaller_archive(dist / "MeridianDesktop.exe", scratch=tmp_path / "scratch")
    assert len(problems) == 1
    assert "PYZ contents not scanned" in problems[0]
    assert not (tmp_path / "scratch").exists()


# ---------------------------------------------------------------- hashes writing (c)


def test_write_hashes_when_hashes_is_first_line(tmp_path):
    manifest = tmp_path / "manifest.toml"
    manifest.write_text("[hashes]\n", encoding="utf-8")
    vb.write_hashes(manifest, {"platforms/qwindows.dll": "ab" * 32})
    assert vb.load_manifest(manifest)["hashes"] == {"platforms/qwindows.dll": "ab" * 32}


def test_write_hashes_keeps_tables_before_hashes(tmp_path):
    manifest = tmp_path / "manifest.toml"
    manifest.write_text('[qt]\nallowed_dlls = ["Qt6Core"]\n\n[hashes]\n', encoding="utf-8")
    vb.write_hashes(manifest, {"Qt6Core.dll": "cd" * 32})
    data = vb.load_manifest(manifest)
    assert data["qt"]["allowed_dlls"] == ["Qt6Core"]
    assert data["hashes"] == {"Qt6Core.dll": "cd" * 32}


def test_write_hashes_appends_a_missing_table(tmp_path):
    manifest = tmp_path / "manifest.toml"
    manifest.write_text('[qt]\nallowed_dlls = ["Qt6Core"]\n', encoding="utf-8")
    vb.write_hashes(manifest, {"Qt6Core.dll": "ef" * 32})
    assert vb.load_manifest(manifest)["hashes"] == {"Qt6Core.dll": "ef" * 32}


def test_write_hashes_refuses_to_drop_a_following_table(tmp_path):
    manifest = tmp_path / "manifest.toml"
    original = "[hashes]\n[other]\nkey = 1\n"
    manifest.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError):
        vb.write_hashes(manifest, {"Qt6Core.dll": "00" * 32})
    assert manifest.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------- fail closed, no tracebacks (d)


def test_run_on_broken_bundle_prints_fail_lines_not_a_traceback(tmp_path, capsys):
    dist = _broken_bundle(tmp_path)
    code = vb.run(dist, MANIFEST, tmp_path / "report.json", write=False)
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL" in out
    assert "RESULT FAIL" in out
    assert "Traceback" not in out


def test_unparsable_manifest_prints_fail_line(tmp_path, capsys):
    manifest = tmp_path / "bad.toml"
    manifest.write_text("[qt\nallowed = ", encoding="utf-8")
    code = vb.run(tmp_path / "dist" / "MeridianDesktop", manifest, tmp_path / "r.json", write=False)
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL manifest unreadable" in out
    assert "Traceback" not in out


def test_missing_manifest_prints_fail_line(tmp_path, capsys):
    code = vb.run(tmp_path / "dist" / "MeridianDesktop", tmp_path / "missing.toml", tmp_path / "r.json", write=False)
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL manifest unreadable" in out
    assert "Traceback" not in out


def test_pe_parse_errors_are_reported_not_raised(tmp_path):
    dist = tmp_path / "bundle"
    (dist / "PySide6").mkdir(parents=True)
    (dist / "PySide6" / "QtCore.pyd").write_bytes(b"junk" * 40)
    exe = dist / "MeridianDesktop.exe"
    exe.write_bytes(b"junk" * 40)
    manifest = vb.load_manifest(MANIFEST)
    problems, _report = vb.check_pe_linkage(dist, manifest, exe)
    assert any("PE parse failed" in p for p in problems)


def test_zip_member_errors_are_reported_not_raised(tmp_path):
    (tmp_path / "base_library.zip").write_bytes(b"not a zip file")
    problems = vb.scan_bundle_bytes(tmp_path, vb.SECRET_MARKERS, case_insensitive=False)
    assert any("zip archive not scanned" in p for p in problems)


@pytest.mark.skipif(sys.platform != "win32", reason="reads Qt6Core.dll from the Windows PySide6 wheel")
def test_real_qt_core_version_is_read_from_resources():
    import importlib.util
    from pathlib import Path

    import pefile

    spec = importlib.util.find_spec("PySide6")
    assert spec is not None and spec.submodule_search_locations, "PySide6 is not installed (fail closed)"
    dll = Path(next(iter(spec.submodule_search_locations))) / "Qt6Core.dll"
    assert dll.is_file(), f"Qt6Core.dll missing: {dll} (fail closed)"

    assert vb._file_version(pefile, dll).startswith("6.12")


# ---------------------------------------------------------------- source leak scan (planted copies)

# A real module from the package. Its bytes are planted into a dist tree under other names.
SOURCE_FILE = REPO / "meridian_desktop" / "settings_store.py"


def _plant_byte_copy(dist: Path) -> str:
    target = dist / "_internal" / "vendor" / "util.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(SOURCE_FILE.read_bytes())
    return "copy of meridian_desktop source in bundle: _internal/vendor/util.py"


def _plant_py_path_name(dist: Path) -> str:
    target = dist / "lib" / "meridian_desktop_shim.py"
    target.parent.mkdir(parents=True)
    target.write_text(CLEAN_SOURCE, encoding="utf-8")
    return "meridian_desktop python source in bundle: lib/meridian_desktop_shim.py"


def _plant_directory_name(dist: Path) -> str:
    (dist / "meridian_desktop").mkdir(parents=True)
    return "meridian_desktop package directory in bundle: meridian_desktop"


@pytest.mark.parametrize(
    "plant",
    [
        pytest.param(_plant_byte_copy, id="byte-identical-copy-under-neutral-name"),
        pytest.param(_plant_py_path_name, id="py-path-containing-meridian_desktop"),
        pytest.param(_plant_directory_name, id="directory-named-meridian_desktop"),
    ],
)
def test_planted_source_copy_fails_leak_scan(tmp_path, plant):
    dist = tmp_path / "dist" / "MeridianDesktop"
    dist.mkdir(parents=True)
    expected = plant(dist)
    problems = vb.check_source_leak(dist)
    assert problems == [expected]


def test_clean_bundle_passes_leak_scan(tmp_path):
    dist = tmp_path / "dist" / "MeridianDesktop"
    (dist / "app").mkdir(parents=True)
    (dist / "app" / "main.py").write_text(CLEAN_SOURCE, encoding="utf-8")
    assert vb.check_source_leak(dist) == []
