"""Tests for the licence gate in packaging/verify_bundle.py (checks a to g, and the review findings).

Each test builds a small synthetic bundle in tmp_path. The PE import reader is
injected, so no real binaries are needed. A fully valid bundle must pass. Each
failure test changes one thing and asserts that the message names that item.

Secret-shaped test values are assembled at run time, so this file holds no
literal secret shape.
"""

import hashlib
import importlib.util
import re
import shutil
import sys
import tomllib
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("verify_bundle", REPO / "packaging" / "verify_bundle.py")
vb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vb)

MANIFEST = REPO / "packaging" / "lgpl_manifest.toml"
PYSIDE_LICENCE = "License: LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only\n"
STUB = b"stub"
STUB_SHA = hashlib.sha256(STUB).hexdigest()
# Assembled from pieces, so the file holds no literal secret shape.
SECRET_AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
SECRET_CREDENTIAL = "api" + "_key=\"" + "Z9" * 12 + "\""
GPL_TEXT = "GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n"
MIT_TEXT = "MIT License\n\nPermission is hereby granted, free of charge, to any person obtaining a copy.\n"
BODY = ("Licence text for the component named above, reproduced from its upstream source. " * 4).strip()
assert len(BODY) >= vb.MIN_LICENCE_TEXT_CHARS

RUNTIMES_TEMPLATE = """\
[[runtime]]
name = "CPython"
version = "3.12.0"
spdx = "PSF-2.0"
files = ["python3.dll", "_socket.pyd"]
sha256 = { "_internal/python3.dll" = "@STUB@", "_internal/_socket.pyd" = "@STUB@" }
notice_url = "https://docs.python.org/3/license.html"

[[runtime]]
name = "OpenSSL"
version = "3.0.0"
spdx = "Apache-2.0"
files = ["libcrypto-3-x64.dll"]
sha256 = { "_internal/libcrypto-3-x64.dll" = "@STUB@" }
notice_url = "https://www.openssl.org/source/license.html"
"""
RUNTIMES_TOML = RUNTIMES_TEMPLATE.replace("@STUB@", STUB_SHA)

HEADERS = [
    "== PySide6 6.12.0 (LGPL-3.0-only) ==",
    "== PySide6_Essentials 6.12.0 (LGPL-3.0-only) ==",
    "== PySide6_Addons 6.12.0 (LGPL-3.0-only) ==",
    "== shiboken6 6.12.0 (LGPL-3.0-only) ==",
    "== keyring 25.7.0 (MIT) ==",
    "== CPython 3.12.0 (PSF-2.0) ==",
    "== OpenSSL 3.0.0 (Apache-2.0) ==",
]

NOTICE = (
    "# Third-party notices\n\n"
    "PySide6, PySide6_Essentials, PySide6_Addons and shiboken6 6.12.0, Qt 6.12.0, LGPL-3.0-only.\n"
    "keyring (MIT). CPython (PSF-2.0). OpenSSL (Apache-2.0).\n"
)

EULA = (
    "MERIDIAN DESKTOP END USER LICENSE AGREEMENT\n\n"
    "You may reverse engineer the LGPL components where LGPL-3.0-only allows it,\n"
    "and you may modify them.\n"
)

OFFER = (
    "# Written offer for source code\n\n"
    "PySide6 6.12.0 and shiboken6 6.12.0 source: https://code.qt.io/cgit/pyside/pyside-setup.git\n"
    "Qt 6.12.0 libraries source: https://download.qt.io/official_releases/qt/6.12/6.12.0/single/\n"
)

VALID_IMPORTS = {
    "MeridianDesktop.exe": ["KERNEL32.dll", "user32.dll", "COMCTL32.dll"],
    "python3.dll": ["KERNEL32.dll", "api-ms-win-core-synch-l1-1-0.dll"],
    "_socket.pyd": ["ws2_32.dll", "KERNEL32.dll"],
    "libcrypto-3-x64.dll": ["advapi32.dll", "crypt32.dll"],
    "QtCore.pyd": ["python3.dll", "KERNEL32.dll"],
}


# ---------------------------------------------------------------- fixtures and helpers


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


class FakeDist:
    """An installed distribution as importlib.metadata shows it: metadata, RECORD files, locate_file."""

    def __init__(self, name, version, fields=None, files=(), classifiers=(), root=None):
        values = {"Name": name, "Version": version}
        values.update(fields or {})
        self.metadata = _FakeMetadata(values, classifiers)
        self.files = list(files)
        self._root = root or Path(".")

    def locate_file(self, path):
        return self._root / str(path)


def add_dist_info(site: Path, name: str, version: str, licence_lines: str = "", record=()) -> Path:
    """Write <name>-<version>.dist-info with METADATA and RECORD under `site`."""
    dist = site / f"{name}-{version}.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n{licence_lines}",
        encoding="utf-8",
    )
    rows = list(record) + [f"{dist.name}/METADATA", f"{dist.name}/RECORD"]
    (dist / "RECORD").write_text("".join(f"{row},,\n" for row in rows), encoding="utf-8")
    return dist


def third_party_text(bodies: dict[str, str] | None = None) -> str:
    """THIRD_PARTY_LICENSES.txt: every header, each followed by its body (BODY unless overridden)."""
    bodies = bodies or {}
    return "".join(f"{header}\n{bodies.get(header, BODY)}\n\n" for header in HEADERS)


def build_valid_bundle(tmp_path: Path) -> tuple[Path, Path]:
    """A bundle that passes every licence gate check. Returns (bundle root, runtimes toml)."""
    root = tmp_path / "bundle"
    internal = root / "_internal"
    (internal / "PySide6").mkdir(parents=True)
    (root / "LICENSES").mkdir()
    (root / "MeridianDesktop.exe").write_bytes(b"MZ application stub")
    for name in ("python3.dll", "_socket.pyd", "libcrypto-3-x64.dll"):
        (internal / name).write_bytes(STUB)
    (internal / "PySide6" / "QtCore.pyd").write_bytes(STUB)

    add_dist_info(internal, "PySide6", "6.12.0", PYSIDE_LICENCE, ["PySide6/QtCore.pyd"])
    add_dist_info(internal, "PySide6_Essentials", "6.12.0", PYSIDE_LICENCE)
    add_dist_info(internal, "PySide6_Addons", "6.12.0", PYSIDE_LICENCE)
    add_dist_info(internal, "shiboken6", "6.12.0", PYSIDE_LICENCE)
    add_dist_info(internal, "keyring", "25.7.0", "License-Expression: MIT\n", ["keyring/__init__.py"])

    (root / "EULA.txt").write_text(EULA, encoding="utf-8")
    (root / "NOTICE.md").write_text(NOTICE, encoding="utf-8")
    (root / "OFFER-SOURCE.md").write_text(OFFER, encoding="utf-8")
    (root / "THIRD_PARTY_LICENSES.txt").write_text(third_party_text(), encoding="utf-8")
    (root / "LICENSES" / "LGPL-3.0.txt").write_text("GNU LESSER GENERAL PUBLIC LICENSE\nVersion 3\n", encoding="utf-8")
    (root / "LICENSES" / "GPL-3.0.txt").write_text("GNU GENERAL PUBLIC LICENSE\nVersion 3\n", encoding="utf-8")
    (root / "LICENSES" / "MSL-1.0.txt").write_text("Microsoft licence text, redistributed unchanged.\n", encoding="utf-8")

    runtimes = tmp_path / "native_runtimes.toml"
    runtimes.write_text(RUNTIMES_TOML, encoding="utf-8")
    return root, runtimes


def _reader(imports_by_name: dict):
    def read(path: Path) -> list[str]:
        return list(imports_by_name.get(path.name, []))

    return read


def run_gate(root: Path, runtimes: Path, imports: dict | None = None) -> list[tuple[str, list[str]]]:
    manifest = vb.load_manifest(MANIFEST)
    return vb.run_license_gate(root, manifest, runtimes_path=runtimes, read_imports=_reader(imports or VALID_IMPORTS))


def problems_of(sections: list[tuple[str, list[str]]]) -> list[str]:
    return [problem for _title, problems in sections for problem in problems]


def section_problems(sections: list[tuple[str, list[str]]], title: str) -> list[str]:
    return [p for t, problems in sections if t == title for p in problems]


def _write_pyz(tmp_path: Path, source: str, name: str = "leaky") -> Path:
    """Write a PYZ with PyInstaller's own ZlibArchiveWriter, holding one module."""
    from PyInstaller.archive.writers import ZlibArchiveWriter

    src = tmp_path / f"{name}.py"
    src.write_text(source, encoding="utf-8")
    code = compile(source, str(src), "exec")
    pyz = tmp_path / "PYZ-00.pyz"
    ZlibArchiveWriter(str(pyz), [(name, str(src), "PYMODULE")], {name: code})
    return pyz


# ---------------------------------------------------------------- a fully valid bundle passes


def test_valid_bundle_passes_every_gate_check(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    sections = run_gate(root, runtimes)
    assert [(title, problems) for title, problems in sections if problems] == []
    manifest = vb.load_manifest(MANIFEST)
    assert vb.check_forbidden_paths(root) == []
    assert vb.check_secrets(root) == []
    assert vb.check_notices(root, manifest) == []


# ---------------------------------------------------------------- (a) distribution licences


@pytest.mark.parametrize(
    ("name", "licence_lines"),
    [
        ("gplpkg", "License-Expression: GPL-3.0-only\n"),
        ("agplpkg", "License: AGPL-3.0-only\n"),
        ("agplclassed", "Classifier: License :: OSI Approved :: GNU Affero General Public License v3\n"),
        ("mplpkg", "License: MPL-2.0\n"),
    ],
)
def test_copyleft_distribution_fails_and_is_named(tmp_path, name, licence_lines):
    root, runtimes = build_valid_bundle(tmp_path)
    add_dist_info(root / "_internal", name, "1.0", licence_lines)
    problems = problems_of(run_gate(root, runtimes))
    assert any(name in p and "copyleft" in p for p in problems), problems


def test_unknown_licence_fails_and_is_named(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    add_dist_info(root / "_internal", "mysterypkg", "2.0", "License: Acme Proprietary Terms\n")
    problems = problems_of(run_gate(root, runtimes))
    assert any("mysterypkg" in p and "not recognised" in p for p in problems), problems


def test_missing_licence_fails_and_is_named(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    add_dist_info(root / "_internal", "nolicpkg", "1.0")
    problems = problems_of(run_gate(root, runtimes))
    assert any("nolicpkg" in p and "no licence" in p for p in problems), problems


def test_lgpl_outside_the_qt_family_fails_and_is_named(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    add_dist_info(root / "_internal", "sneakylgpl", "1.0", "License: LGPL-3.0-only\n")
    problems = problems_of(run_gate(root, runtimes))
    assert any("sneakylgpl" in p and "allowed only for the Qt family" in p for p in problems), problems


def test_permissive_extra_distribution_is_allowed(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    add_dist_info(root / "_internal", "goodpkg", "1.0", "License: Apache-2.0\n")
    problems = problems_of(run_gate(root, runtimes))
    licence_problems = [p for p in problems if "goodpkg" in p and "licence" in p.lower()]
    assert licence_problems == [], problems
    # An allowed distribution still needs its THIRD_PARTY header and its NOTICE entry.
    assert any("goodpkg" in p and "header" in p for p in problems), problems
    assert any("NOTICE.md does not name goodpkg" in p for p in problems), problems


def test_dist_info_without_record_fails_and_is_named(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    (root / "_internal" / "keyring-25.7.0.dist-info" / "RECORD").unlink()
    problems = problems_of(run_gate(root, runtimes))
    assert any("keyring" in p and "RECORD missing" in p for p in problems), problems


def test_bundled_distribution_without_dist_info_fails_and_is_named(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    shutil.rmtree(root / "_internal" / "shiboken6-6.12.0.dist-info")
    problems = problems_of(run_gate(root, runtimes))
    assert any("bundled distribution has no dist-info" in p and "shiboken6" in p for p in problems), problems


def test_package_directory_without_licence_record_fails_and_is_named(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    setuptools_dir = root / "_internal" / "setuptools"
    setuptools_dir.mkdir()
    (setuptools_dir / "__init__.py").write_text("", encoding="utf-8")
    problems = problems_of(run_gate(root, runtimes))
    assert any("_internal/setuptools" in p and "no licence record" in p for p in problems), problems


# -- finding 5: licence taken from every declared source, and from the LICENSE text when unclear


def test_disagreeing_licence_fields_fail_and_are_named(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    add_dist_info(root / "_internal", "splitpkg", "1.0", "License-Expression: MIT\nLicense: Apache-2.0\n")
    problems = problems_of(run_gate(root, runtimes))
    assert any("splitpkg" in p and "disagree" in p for p in problems), problems


def test_copyleft_classifier_beside_permissive_licence_fails(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    add_dist_info(
        root / "_internal",
        "classpkg",
        "1.0",
        "License: MIT\nClassifier: License :: OSI Approved :: GNU General Public License v3 (GPLv3)\n",
    )
    problems = problems_of(run_gate(root, runtimes))
    assert any("classpkg" in p and "copyleft" in p for p in problems), problems


def test_agreeing_licence_fields_pass(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    add_dist_info(
        root / "_internal",
        "agreepkg",
        "1.0",
        "License-Expression: MIT\nLicense: MIT\nClassifier: License :: OSI Approved :: MIT License\n",
    )
    problems = problems_of(run_gate(root, runtimes))
    assert not any("agreepkg" in p and "licence" in p.lower() for p in problems), problems


def test_ambiguous_metadata_reads_licence_file_and_fails_on_gpl(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    dist = add_dist_info(root / "_internal", "ambigpkg", "1.0", "License-Expression: MIT\nLicense: BSD-3-Clause\n")
    (dist / "LICENSE").write_text(GPL_TEXT, encoding="utf-8")
    problems = problems_of(run_gate(root, runtimes))
    assert any("ambigpkg" in p and "disagree" in p for p in problems), problems
    assert any("GPL or AGPL signature" in p and "ambigpkg" in p and "LICENSE" in p for p in problems), problems


def test_missing_metadata_reads_licence_file_and_fails_on_gpl(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    dist = add_dist_info(root / "_internal", "bareGPLpkg", "1.0")
    (dist / "COPYING").write_text(GPL_TEXT, encoding="utf-8")
    problems = problems_of(run_gate(root, runtimes))
    assert any("bareGPLpkg" in p and "no licence" in p for p in problems), problems
    assert any("GPL or AGPL signature" in p and "bareGPLpkg" in p and "COPYING" in p for p in problems), problems


def test_clear_metadata_does_not_read_licence_file(tmp_path):
    # Unambiguous metadata decides. A GPL mention in the LICENSE file is not read.
    root, runtimes = build_valid_bundle(tmp_path)
    dist = add_dist_info(root / "_internal", "cleanpkg", "1.0", "License-Expression: MIT\n")
    (dist / "LICENSE").write_text(MIT_TEXT + GPL_TEXT, encoding="utf-8")
    problems = problems_of(run_gate(root, runtimes))
    assert not any("cleanpkg" in p and "GPL" in p for p in problems), problems


# -- finding 6: dist-info discovery and orphan packages cover the whole bundle, not only _internal


def test_gpl_dist_info_at_bundle_root_fails_and_is_named(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    add_dist_info(root, "gplroot", "1.0", "License-Expression: GPL-3.0-only\n")
    problems = problems_of(run_gate(root, runtimes))
    assert any("gplroot" in p and "copyleft" in p for p in problems), problems


def test_gpl_dist_info_in_a_subdirectory_fails_and_is_named(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    add_dist_info(root / "tools", "gpltool", "1.0", "License: AGPL-3.0-only\n")
    problems = problems_of(run_gate(root, runtimes))
    assert any("gpltool" in p and "copyleft" in p for p in problems), problems


def test_package_directory_at_bundle_root_fails_and_is_named(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    (root / "evilpkg").mkdir()
    (root / "evilpkg" / "__init__.py").write_text("", encoding="utf-8")
    problems = problems_of(run_gate(root, runtimes))
    assert any("evilpkg" in p and "no licence record" in p for p in problems), problems


def test_package_directory_in_a_nested_folder_fails_and_is_named(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    nested = root / "_internal" / "PySide6" / "extras" / "hidden"
    nested.mkdir(parents=True)
    (nested / "__init__.py").write_text("", encoding="utf-8")
    problems = problems_of(run_gate(root, runtimes))
    assert any("PySide6/extras/hidden" in p and "no licence record" in p for p in problems), problems


# ---------------------------------------------------------------- (b) native binaries


def test_unlisted_native_dll_fails_and_is_named(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    (root / "_internal" / "evil.dll").write_bytes(STUB)
    problems = problems_of(run_gate(root, runtimes))
    assert any("evil.dll" in p and "not inventoried" in p for p in problems), problems


def test_forbidden_other_dll_fails_and_is_named(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    (root / "_internal" / "PySide6" / "avcodec-63.dll").write_bytes(STUB)
    problems = problems_of(run_gate(root, runtimes))
    assert any("avcodec-63.dll" in p and "forbidden native binary" in p for p in problems), problems


def test_pyd_without_licence_record_fails_and_is_named(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    shutil.rmtree(root / "_internal" / "PySide6-6.12.0.dist-info")
    problems = problems_of(run_gate(root, runtimes))
    assert any("QtCore.pyd" in p and "neither listed" in p for p in problems), problems


# -- finding 7: native binaries are found by the MZ header, not only by suffix


def test_mz_file_with_unlisted_suffix_fails_and_is_named(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    (root / "_internal" / "payload.bin").write_bytes(b"MZ" + b"\x00" * 64)
    problems = problems_of(run_gate(root, runtimes))
    assert any("payload.bin" in p and "not inventoried" in p for p in problems), problems


def test_mz_file_in_a_text_suffix_is_still_inventoried(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    (root / "_internal" / "helper.txt").write_bytes(b"MZ" + b"\x00" * 64)
    problems = problems_of(run_gate(root, runtimes))
    assert any("helper.txt" in p and "not inventoried" in p for p in problems), problems


# ---------------------------------------------------------------- (c) imports


def test_unbundled_import_fails_and_is_named(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    imports = dict(VALID_IMPORTS)
    imports["libcrypto-3-x64.dll"] = ["advapi32.dll", "evilthing.dll"]
    problems = problems_of(run_gate(root, runtimes, imports))
    assert any("libcrypto-3-x64.dll" in p and "evilthing.dll" in p for p in problems), problems


def test_unreadable_import_table_fails_closed(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    manifest = vb.load_manifest(MANIFEST)

    def unreadable(path):
        raise ValueError("not a PE file")

    problems = vb.check_native_imports(root, manifest, unreadable)
    assert problems and all("(fail closed)" in p for p in problems)


# -- finding 8: every runtime file needs a sha256, and the bundled file must match it


def test_runtime_file_without_sha256_fails_and_is_named(tmp_path):
    root, _runtimes = build_valid_bundle(tmp_path)
    bad = tmp_path / "no_hash.toml"
    bad.write_text(
        RUNTIMES_TOML.replace('sha256 = { "_internal/libcrypto-3-x64.dll" = "' + STUB_SHA + '" }', "sha256 = {}"),
        encoding="utf-8",
    )
    sections = run_gate(root, bad)
    problems = section_problems(sections, "native runtimes listed")
    assert any("libcrypto-3-x64.dll" in p and "has no sha256" in p for p in problems), problems


def test_runtime_without_sha256_table_fails_and_is_named(tmp_path):
    root, _runtimes = build_valid_bundle(tmp_path)
    bad = tmp_path / "no_table.toml"
    text = "\n".join(line for line in RUNTIMES_TOML.splitlines() if not line.startswith('sha256 = { "_internal/libcrypto'))
    bad.write_text(text, encoding="utf-8")
    sections = run_gate(root, bad)
    problems = section_problems(sections, "native runtimes listed")
    assert any("OpenSSL" in p and "sha256" in p for p in problems), problems


def test_malformed_runtime_digest_fails_and_is_named(tmp_path):
    root, _runtimes = build_valid_bundle(tmp_path)
    bad = tmp_path / "short_hash.toml"
    bad.write_text(RUNTIMES_TOML.replace(STUB_SHA, "abc123"), encoding="utf-8")
    sections = run_gate(root, bad)
    problems = section_problems(sections, "native runtimes listed")
    assert any("not 64 hex digits" in p and "python3.dll" in p for p in problems), problems


def test_bundled_runtime_file_with_wrong_content_fails_and_is_named(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    (root / "_internal" / "python3.dll").write_bytes(b"a different file with the same name")
    problems = problems_of(run_gate(root, runtimes))
    assert any("sha256 differs" in p and "python3.dll" in p for p in problems), problems


def test_runtime_file_missing_from_bundle_fails_and_is_named(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    (root / "_internal" / "libcrypto-3-x64.dll").unlink()
    problems = problems_of(run_gate(root, runtimes))
    assert any("libcrypto-3-x64.dll" in p and "not in the bundle" in p for p in problems), problems


def test_microsoft_spdx_ids_are_allowed_for_runtimes_only():
    manifest = vb.load_manifest(MANIFEST)
    assert "LicenseRef-Microsoft-VC-Redist" in manifest["runtimes"]["allowed_spdx"]
    assert "LicenseRef-Microsoft-Windows-SDK" in manifest["runtimes"]["allowed_spdx"]
    assert "LicenseRef-Microsoft-VC-Redist" not in manifest["licenses"]["allowed_spdx"]
    assert "LicenseRef-Microsoft-Windows-SDK" not in manifest["licenses"]["allowed_spdx"]


def test_unlisted_runtime_licence_is_rejected(tmp_path):
    root, _runtimes = build_valid_bundle(tmp_path)
    bad = tmp_path / "unknown_licence.toml"
    bad.write_text(RUNTIMES_TOML.replace('spdx = "Apache-2.0"', 'spdx = "LicenseRef-Unlisted"'), encoding="utf-8")
    sections = run_gate(root, bad)
    problems = section_problems(sections, "native runtimes listed")
    assert any("OpenSSL" in p and "LicenseRef-Unlisted" in p for p in problems), problems


# ---------------------------------------------------------------- (d) artifacts


def test_missing_third_party_file_fails_and_is_named(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    (root / "THIRD_PARTY_LICENSES.txt").unlink()
    problems = problems_of(run_gate(root, runtimes))
    assert any("THIRD_PARTY_LICENSES.txt missing" in p for p in problems), problems


def test_missing_distribution_header_fails_and_is_named(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    text = (root / "THIRD_PARTY_LICENSES.txt").read_text(encoding="utf-8")
    (root / "THIRD_PARTY_LICENSES.txt").write_text(text.replace("== keyring 25.7.0 (MIT) ==\n", ""), encoding="utf-8")
    problems = problems_of(run_gate(root, runtimes))
    assert any("== keyring 25.7.0 (MIT) ==" in p for p in problems), problems


def test_missing_runtime_header_fails_and_is_named(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    text = (root / "THIRD_PARTY_LICENSES.txt").read_text(encoding="utf-8")
    (root / "THIRD_PARTY_LICENSES.txt").write_text(
        text.replace("== OpenSSL 3.0.0 (Apache-2.0) ==\n", ""), encoding="utf-8"
    )
    problems = problems_of(run_gate(root, runtimes))
    assert any("== OpenSSL 3.0.0 (Apache-2.0) ==" in p for p in problems), problems


# -- finding 9: every header is followed by at least 200 characters of licence text


def test_header_with_no_licence_text_fails_and_names_the_entry(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    (root / "THIRD_PARTY_LICENSES.txt").write_text(
        third_party_text({"== keyring 25.7.0 (MIT) ==": ""}), encoding="utf-8"
    )
    problems = problems_of(run_gate(root, runtimes))
    assert any("keyring 25.7.0 (MIT)" in p and "0 characters" in p for p in problems), problems


def test_header_with_199_characters_fails_and_names_the_entry(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    short = "a" * 199
    (root / "THIRD_PARTY_LICENSES.txt").write_text(
        third_party_text({"== OpenSSL 3.0.0 (Apache-2.0) ==": short}), encoding="utf-8"
    )
    problems = problems_of(run_gate(root, runtimes))
    assert any("OpenSSL 3.0.0 (Apache-2.0)" in p and "199 characters" in p for p in problems), problems


def test_header_with_200_characters_passes(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    exact = "a" * 200
    (root / "THIRD_PARTY_LICENSES.txt").write_text(
        third_party_text({"== OpenSSL 3.0.0 (Apache-2.0) ==": exact}), encoding="utf-8"
    )
    problems = problems_of(run_gate(root, runtimes))
    assert not any("OpenSSL" in p and "characters" in p for p in problems), problems


def test_text_under_another_header_does_not_count(tmp_path):
    # The body of the header above is the only text that counts, not the text of the next section.
    root, runtimes = build_valid_bundle(tmp_path)
    text = third_party_text({"== keyring 25.7.0 (MIT) ==": ""})
    (root / "THIRD_PARTY_LICENSES.txt").write_text(text, encoding="utf-8")
    manifest = vb.load_manifest(MANIFEST)
    inventory = {
        "distributions": [{"name": "keyring", "version": "25.7.0", "spdx": "MIT"}],
    }
    problems = vb.check_required_artifacts(root, manifest, inventory, [])
    assert any("keyring 25.7.0 (MIT)" in p and "0 characters" in p for p in problems), problems


def test_missing_licence_text_fails_and_is_named(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    (root / "LICENSES" / "GPL-3.0.txt").unlink()
    problems = problems_of(run_gate(root, runtimes))
    assert any("LICENSES/GPL-3.0.txt" in p and "missing or empty" in p for p in problems), problems


def test_notice_that_omits_a_runtime_fails_and_is_named(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    (root / "NOTICE.md").write_text(NOTICE.replace("OpenSSL", "Other"), encoding="utf-8")
    problems = problems_of(run_gate(root, runtimes))
    assert any("NOTICE.md does not name OpenSSL" in p for p in problems), problems


def test_missing_runtimes_file_fails_closed(tmp_path):
    root, _runtimes = build_valid_bundle(tmp_path)
    sections = run_gate(root, tmp_path / "absent_native_runtimes.toml")
    problems = section_problems(sections, "native runtimes listed")
    assert any("native_runtimes.toml missing" in p and "(fail closed)" in p for p in problems), problems


def test_runtime_with_copyleft_licence_is_rejected(tmp_path):
    root, _runtimes = build_valid_bundle(tmp_path)
    bad = tmp_path / "bad_runtimes.toml"
    bad.write_text(RUNTIMES_TOML.replace('spdx = "PSF-2.0"', 'spdx = "GPL-2.0-only"'), encoding="utf-8")
    sections = run_gate(root, bad)
    problems = section_problems(sections, "native runtimes listed")
    assert any("CPython" in p and "GPL-2.0-only" in p for p in problems), problems


# ---------------------------------------------------------------- (e) legal-review markers


def test_draft_marker_in_eula_blocks_release_and_is_named(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    (root / "EULA.txt").write_text("DRAFT - NOT FOR RELEASE\n" + EULA, encoding="utf-8")
    problems = problems_of(run_gate(root, runtimes))
    hits = [p for p in problems if "EULA.txt" in p and "DRAFT - NOT FOR RELEASE" in p]
    assert hits, problems
    assert "counsel removes it after review" in hits[0]


def test_tbd_and_placeholder_markers_in_offer_block_release(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    (root / "OFFER-SOURCE.md").write_text(
        OFFER + "Where to get the source: https://TBD-owner-source-host\nThis is a Placeholder term.\n",
        encoding="utf-8",
    )
    problems = problems_of(run_gate(root, runtimes))
    assert any("OFFER-SOURCE.md" in p and "'TBD'" in p for p in problems), problems
    assert any("OFFER-SOURCE.md" in p and "'placeholder'" in p for p in problems), problems


# -- finding 3: markers pass through dash, space and encoding variants


_MARKER_VARIANTS = [
    ("en dash", "DRAFT – NOT FOR RELEASE", "DRAFT - NOT FOR RELEASE"),
    ("em dash without spaces", "DRAFT—NOT FOR RELEASE", "DRAFT - NOT FOR RELEASE"),
    ("minus sign", "DRAFT − NOT FOR RELEASE", "DRAFT - NOT FOR RELEASE"),
    ("fullwidth hyphen", "DRAFT － NOT FOR RELEASE", "DRAFT - NOT FOR RELEASE"),
    ("no-break space", "DRAFT - NOT FOR RELEASE", "DRAFT - NOT FOR RELEASE"),
    ("lower case", "draft - not for release", "DRAFT - NOT FOR RELEASE"),
    ("zero-width inside TBD", "T​B⁠D", "TBD"),
    ("zero-width inside placeholder", "Place﻿holder", "placeholder"),
    ("narrow no-break space", "tbd item", "TBD"),
]


@pytest.mark.parametrize(("label", "text", "marker"), _MARKER_VARIANTS, ids=[v[0] for v in _MARKER_VARIANTS])
def test_marker_variant_in_utf8_blocks_release(tmp_path, label, text, marker):
    root, _runtimes = build_valid_bundle(tmp_path)
    (root / "EULA.txt").write_text(EULA + text + "\n", encoding="utf-8")
    problems = vb.check_legal_markers(root)
    assert any("EULA.txt" in p and repr(marker) in p for p in problems), problems


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
def test_marker_in_utf16_blocks_release(tmp_path, encoding):
    root, _runtimes = build_valid_bundle(tmp_path)
    payload = "DRAFT – NOT FOR RELEASE\n".encode(encoding)
    (root / "EULA.txt").write_bytes(b"\xff\xfe" + payload if encoding == "utf-16-le" else b"\xfe\xff" + payload)
    problems = vb.check_legal_markers(root)
    assert any("EULA.txt" in p and "'DRAFT - NOT FOR RELEASE'" in p for p in problems), problems


def test_marker_in_utf16_without_bom_blocks_release(tmp_path):
    root, _runtimes = build_valid_bundle(tmp_path)
    (root / "OFFER-SOURCE.md").write_bytes(OFFER.encode("utf-16-le") + "placeholder".encode("utf-16-le"))
    problems = vb.check_legal_markers(root)
    assert any("OFFER-SOURCE.md" in p and "'placeholder'" in p for p in problems), problems


def test_ordinary_draft_wording_does_not_trip_the_marker_check(tmp_path):
    root, _runtimes = build_valid_bundle(tmp_path)
    (root / "EULA.txt").write_text(EULA + "Draft notes are kept elsewhere. The drafting team met today.\n", encoding="utf-8")
    assert [p for p in vb.check_legal_markers(root) if "EULA.txt" in p] == []


# ---------------------------------------------------------------- (f) source offer


def test_off_allowlist_source_host_fails_and_is_named(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    (root / "OFFER-SOURCE.md").write_text(OFFER + "Mirror: https://evil.example.com/src\n", encoding="utf-8")
    problems = problems_of(run_gate(root, runtimes))
    assert any("evil.example.com" in p and "allowlist" in p for p in problems), problems


def test_source_offer_without_a_pyside_url_fails(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    (root / "OFFER-SOURCE.md").write_text(
        "PySide6 source is available on request.\n"
        "Qt 6.12.0 source: https://download.qt.io/official_releases/qt/\n",
        encoding="utf-8",
    )
    problems = problems_of(run_gate(root, runtimes))
    assert any("no https URL for PySide6" in p for p in problems), problems


# ---------------------------------------------------------------- (g) secrets


@pytest.mark.parametrize(
    "name",
    [".env", ".env_temp", ".env.local", "prod.env", "meridian.toml", "server.pem", "signing.key", ".netrc"],
)
def test_secret_named_file_fails_and_is_named(tmp_path, name):
    root, _runtimes = build_valid_bundle(tmp_path)
    (root / "_internal" / name).write_text("stub", encoding="utf-8")
    problems = vb.check_forbidden_paths(root)
    assert any(f"forbidden file: _internal/{name}" in p for p in problems), problems


# -- finding 10: secret-named directories are flagged too


@pytest.mark.parametrize("name", [".env_temp", "prod.env", "signing.key", ".netrc"])
def test_secret_named_directory_fails_and_is_named(tmp_path, name):
    root, _runtimes = build_valid_bundle(tmp_path)
    (root / "_internal" / name).mkdir()
    problems = vb.check_forbidden_paths(root)
    assert any(f"forbidden directory: _internal/{name}" in p for p in problems), problems


@pytest.mark.parametrize(
    ("name", "rule", "secret"),
    [
        ("api" + "_key = '" + "Z9" * 12 + "'", "credential-assignment", "Z9" * 12),
        ("AKIA" + "ABCDEFGHIJKLMNOP", "aws-access-key-id", "AKIA" + "ABCDEFGHIJKLMNOP"),
        ("-----BEGIN " + "RSA PRIVATE KEY-----", "pem-private-key", "BEGIN RSA PRIVATE KEY"),
    ],
)
def test_secret_shaped_text_fails_and_never_shows_the_match(tmp_path, name, rule, secret):
    root, _runtimes = build_valid_bundle(tmp_path)
    line = name
    (root / "_internal" / "notes.txt").write_text(line + "\n", encoding="utf-8")
    problems = vb.check_secrets(root)
    hits = [p for p in problems if "notes.txt" in p and rule in p]
    assert hits, problems
    assert all(secret not in p for p in problems), problems


# -- finding 1: every file is scanned, in UTF-16 too, and the message names only file and rule


@pytest.mark.parametrize(
    ("name", "payload", "rule"),
    [
        ("notes.txt", SECRET_AWS.encode("utf-16-le"), "aws-access-key-id"),
        ("blob.bin", b"\xfe\xff" + SECRET_AWS.encode("utf-16-be"), "aws-access-key-id"),
        ("payload.bin", SECRET_AWS.encode("ascii"), "aws-access-key-id"),
        ("setup.dat", ("﻿" + SECRET_CREDENTIAL).encode("utf-16-le"), "credential-assignment"),
    ],
)
def test_secret_shape_is_found_in_any_file_and_any_encoding(tmp_path, name, payload, rule):
    root, _runtimes = build_valid_bundle(tmp_path)
    (root / "_internal" / name).write_bytes(payload)
    problems = vb.check_secrets(root)
    assert any(f"secret-shaped text in _internal/{name}: rule {rule}" in p for p in problems), problems
    assert all(SECRET_AWS not in p and SECRET_CREDENTIAL not in p for p in problems), problems


def test_secret_shape_inside_a_zip_member_is_found(tmp_path):
    import zipfile

    root, _runtimes = build_valid_bundle(tmp_path)
    with zipfile.ZipFile(root / "_internal" / "bundle.zip", "w") as archive:
        archive.writestr("inner.txt", SECRET_AWS.encode("utf-16-le"))
    problems = vb.check_secrets(root)
    assert any("bundle.zip!inner.txt" in p and "aws-access-key-id" in p for p in problems), problems


# -- finding 2: every string constant in the PYZ gets the four shape rules


def test_pyz_constant_with_aws_key_shape_is_found(tmp_path):
    pyz = _write_pyz(tmp_path, "KEY = '" + SECRET_AWS + "'\n")
    problems = vb.scan_pyz_files([pyz])
    assert any("PYZ-00.pyz:leaky" in p and "rule aws-access-key-id" in p for p in problems), problems
    assert all(SECRET_AWS not in p for p in problems), problems


def test_pyz_constant_with_credential_assignment_shape_is_found(tmp_path):
    source = "CONF = " + repr(SECRET_CREDENTIAL) + "\n"
    pyz = _write_pyz(tmp_path, source)
    problems = vb.scan_pyz_files([pyz])
    assert any("PYZ-00.pyz:leaky" in p and "rule credential-assignment" in p for p in problems), problems


def test_pyz_clean_constants_still_pass(tmp_path):
    pyz = _write_pyz(tmp_path, "GREETING = 'hello'\nLIMIT = 42\n")
    assert vb.scan_pyz_files([pyz]) == []


# ---------------------------------------------------------------- finding 4: every PYZ module has a licensed owner


def test_unowned_pyz_module_fails_and_is_named():
    manifest = vb.load_manifest(MANIFEST)
    problems = vb.pyz_owner_problems(["mysterymodule"], manifest, dists=[])
    assert any("PYZ module mysterymodule has no owning distribution" in p for p in problems), problems


def test_first_party_and_standard_library_modules_have_an_owner():
    manifest = vb.load_manifest(MANIFEST)
    problems = vb.pyz_owner_problems(["meridian_desktop.ui.app", "json.decoder", "argparse"], manifest, dists=[])
    assert problems == [], problems


def test_owned_module_from_a_copyleft_distribution_fails_and_names_the_module(tmp_path):
    manifest = vb.load_manifest(MANIFEST)
    dist = FakeDist(
        "gplpkg",
        "1.0",
        {"License-Expression": "GPL-3.0-only"},
        files=["gplmod/__init__.py", "gplmod/core.py"],
        root=tmp_path,
    )
    problems = vb.pyz_owner_problems(["gplmod", "gplmod.core"], manifest, dists=[dist])
    assert any("gplpkg" in p and "copyleft" in p and "gplmod" in p for p in problems), problems


def test_owned_module_from_a_permissive_distribution_passes(tmp_path):
    manifest = vb.load_manifest(MANIFEST)
    dist = FakeDist("goodpkg", "1.0", {"License": "MIT"}, files=["goodmod.py"], root=tmp_path)
    assert vb.pyz_owner_problems(["goodmod"], manifest, dists=[dist]) == []


def test_owner_with_a_gpl_licence_file_fails(tmp_path):
    manifest = vb.load_manifest(MANIFEST)
    (tmp_path / "gpllicensed-1.0.dist-info").mkdir()
    (tmp_path / "gpllicensed-1.0.dist-info" / "LICENSE").write_text(GPL_TEXT, encoding="utf-8")
    dist = FakeDist(
        "gpllicensed",
        "1.0",
        files=["gpllicensed.py", "gpllicensed-1.0.dist-info/LICENSE"],
        root=tmp_path,
    )
    problems = vb.pyz_owner_problems(["gpllicensed"], manifest, dists=[dist])
    assert any("GPL or AGPL signature" in p and "gpllicensed" in p for p in problems), problems


def test_namespace_package_is_owned_by_the_distributions_beneath_it(tmp_path):
    manifest = vb.load_manifest(MANIFEST)
    dist = FakeDist("nsdist", "1.0", {"License": "MIT"}, files=["ns/part/mod.py"], root=tmp_path)
    assert vb.pyz_owner_problems(["ns"], manifest, dists=[dist]) == []


@pytest.mark.skipif(sys.platform != "win32", reason="expects the Windows keyring backend and the Windows bundle layout")
def test_real_environment_owns_every_pyz_module_it_ships():
    manifest = vb.load_manifest(MANIFEST)
    names = [
        "keyring",
        "keyring.backends.Windows",
        "jaraco.classes",
        "more_itertools",
        "win32ctypes",
        "typing_extensions",
        "PySide6",
        "PySide6.support",
        "shiboken6",
        "json",
        "meridian_desktop.ui.app",
    ]
    assert vb.pyz_owner_problems(names, manifest) == []


def test_pyinstaller_runtime_module_has_no_owner_and_is_named():
    # _pyi_rth_utils is PyInstaller's runtime. No installed distribution lists it, so it
    # fails until counsel decides how the PyInstaller exception covers it.
    manifest = vb.load_manifest(MANIFEST)
    problems = vb.pyz_owner_problems(["_pyi_rth_utils"], manifest)
    assert any("PYZ module _pyi_rth_utils has no owning distribution" in p for p in problems), problems


def test_pyz_owner_list_is_derived_from_record_files(tmp_path):
    dist = FakeDist("sample", "1.0", files=["sample/__init__.py", "sample/data.txt", "../outside.py"], root=tmp_path)
    index = vb.build_owner_index([dist])
    assert set(index) == {"sample"}


# ---------------------------------------------------------------- finding 11: the verifier refuses to start outside its roots


def test_module_resolving_outside_the_import_roots_is_refused(tmp_path):
    roots = [tmp_path / "pixi_env" / "Lib" / "site-packages"]
    outside = tmp_path / "elsewhere" / "argparse.py"
    problems = vb.find_import_root_problems({"argparse": str(outside)}, roots)
    assert any("module argparse resolves outside" in p for p in problems), problems


def test_module_inside_the_verifier_directory_is_allowed():
    roots = vb.import_roots()
    inside = vb.VERIFIER_DIR / "pefile.py"
    assert vb.find_import_root_problems({"pefile": str(inside)}, roots) == []


def test_namespace_package_folder_outside_the_roots_is_refused(tmp_path):
    roots = [tmp_path / "env"]
    problems = vb.find_import_root_problems({"nsfolder": [str(tmp_path / "evil")]}, roots)
    assert any("module nsfolder resolves outside" in p for p in problems), problems


def test_distribution_read_from_outside_the_roots_is_refused(tmp_path):
    roots = [tmp_path / "env"]
    dist = FakeDist("planted", "1.0", root=tmp_path / "PYTHONPATH_like")
    problems = vb.find_distribution_root_problems([dist], roots)
    assert any("distribution planted is read from outside" in p for p in problems), problems


def test_real_environment_passes_the_import_root_guard():
    assert vb.check_import_roots() == []


def test_loaded_stdlib_module_from_outside_is_refused(monkeypatch, tmp_path):
    planted = types.ModuleType("argparse")
    planted.__file__ = str(tmp_path / "planted" / "argparse.py")
    monkeypatch.setitem(sys.modules, "argparse", planted)
    problems = vb.check_import_roots()
    assert any("module argparse resolves outside" in p for p in problems), problems


def test_run_refuses_to_start_and_runs_no_check(tmp_path, monkeypatch, capsys):
    root, runtimes = build_valid_bundle(tmp_path)
    monkeypatch.setattr(vb, "check_import_roots", lambda: ["module json resolves outside the roots (fail closed): x"])
    code = vb.run(root, MANIFEST, tmp_path / "report.json", write=False, runtimes_path=runtimes)
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL import roots" in out
    assert "RESULT FAIL" in out
    assert "PASS" not in out
    assert "forbidden paths" not in out
    assert not (tmp_path / "report.json").exists()


def test_verifier_reads_no_environment_variables():
    source = (REPO / "packaging" / "verify_bundle.py").read_text(encoding="utf-8")
    assert "os.environ" not in source
    assert "getenv" not in source
    assert not re.search(r"^import os\b", source, re.MULTILINE)


def test_manifest_hashes_table_stays_last():
    headers = [
        line.strip()
        for line in MANIFEST.read_text(encoding="utf-8").splitlines()
        if re.match(r"^\s*\[", line)
    ]
    assert headers[-1] == "[hashes]", headers


def test_manifest_licence_and_source_policy():
    manifest = vb.load_manifest(MANIFEST)
    allowed = set(manifest["licenses"]["allowed_spdx"])
    assert {"MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "PSF-2.0", "ISC", "0BSD", "Zlib", "Unlicense"} <= allowed
    assert not any(spdx.upper().startswith(("GPL", "AGPL", "MPL")) for spdx in allowed)
    assert "keyring" in manifest["bundled"]["distributions"]
    assert {"download.qt.io", "code.qt.io"} <= set(manifest["source_offer"]["hosts"])
    assert {"icu.dll", "icuin.dll", "icuuc.dll"} <= set(manifest["system_dlls"]["names"])


def test_manifest_has_no_stale_wave_seven_claims():
    text = MANIFEST.read_text(encoding="utf-8").lower()
    assert "wave 7" not in text
    assert "unverified" not in text
    assert "expect verify_bundle to fail" not in text


# ---------------------------------------------------------------- D1: a shared file needs one licence line


def _claim(dist_dir: Path, rel_under_internal: str) -> None:
    """Add one path to a dist-info RECORD, as the installer writes it."""
    record = dist_dir / "RECORD"
    record.write_text(record.read_text(encoding="utf-8") + f"{rel_under_internal},,\n", encoding="utf-8")


def test_file_shared_by_two_non_family_distributions_fails_and_names_claimants(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    add_dist_info(root / "_internal", "alphapkg", "1.0", "License: MIT\n", ["shared/common.py"])
    add_dist_info(root / "_internal", "betapkg", "1.0", "License: MIT\n", ["shared/common.py"])
    problems = problems_of(run_gate(root, runtimes))
    hits = [p for p in problems if "shared/common.py" in p and "more than one distribution" in p]
    assert hits, problems
    assert "alphapkg" in hits[0] and "betapkg" in hits[0], hits


def test_file_shared_only_by_lgpl_family_with_one_election_line_passes(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    internal = root / "_internal"
    (internal / "PySide6" / "shared.pyi").write_text("", encoding="utf-8")
    _claim(internal / "PySide6-6.12.0.dist-info", "PySide6/shared.pyi")
    _claim(internal / "PySide6_Essentials-6.12.0.dist-info", "PySide6/shared.pyi")
    problems = problems_of(run_gate(root, runtimes))
    assert not any("shared.pyi" in p for p in problems), problems


def test_file_shared_by_family_members_with_different_election_lines_fails(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    internal = root / "_internal"
    (internal / "PySide6" / "shared.pyi").write_text("", encoding="utf-8")
    addons = internal / "PySide6_Addons-6.12.0.dist-info"
    (addons / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: PySide6_Addons\nVersion: 6.12.0\nLicense: LGPL-3.0-only\n",
        encoding="utf-8",
    )
    _claim(internal / "PySide6-6.12.0.dist-info", "PySide6/shared.pyi")
    _claim(addons, "PySide6/shared.pyi")
    problems = problems_of(run_gate(root, runtimes))
    hits = [p for p in problems if "shared.pyi" in p and "more than one distribution" in p]
    assert hits, problems
    assert "pyside6-addons" in hits[0] and "pyside6," in hits[0], hits


def test_file_shared_by_family_and_a_permissive_distribution_fails_and_names_both(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    internal = root / "_internal"
    (internal / "PySide6" / "shared.pyi").write_text("", encoding="utf-8")
    _claim(internal / "PySide6-6.12.0.dist-info", "PySide6/shared.pyi")
    _claim(internal / "keyring-25.7.0.dist-info", "PySide6/shared.pyi")
    problems = problems_of(run_gate(root, runtimes))
    hits = [p for p in problems if "shared.pyi" in p and "more than one distribution" in p]
    assert hits, problems
    assert "keyring" in hits[0] and "pyside6" in hits[0], hits


# ---------------------------------------------------------------- D2: digests are keyed by bundle path


def _openssl_runtimes(tmp_path: Path, digests: dict[str, str]) -> Path:
    """native_runtimes.toml with the OpenSSL runtime's sha256 table replaced by `digests` (path -> digest).

    OpenSSL stands in for a runtime that ships in more than one folder. The Visual C++ runtime, which
    used to, is no longer bundled.
    """
    table = ", ".join(f'"{path}" = "{digest}"' for path, digest in digests.items())
    text = RUNTIMES_TOML.replace(
        'sha256 = { "_internal/libcrypto-3-x64.dll" = "' + STUB_SHA + '" }',
        f"sha256 = {{ {table} }}",
    )
    runtimes = tmp_path / "openssl_runtimes.toml"
    runtimes.write_text(text, encoding="utf-8")
    return runtimes


def test_two_files_with_one_name_in_different_folders_are_verified_separately(tmp_path):
    root, _runtimes = build_valid_bundle(tmp_path)
    other = b"a second copy of the OpenSSL runtime, with other bytes"
    (root / "_internal" / "PySide6" / "libcrypto-3-x64.dll").write_bytes(other)
    runtimes = _openssl_runtimes(
        tmp_path,
        {
            "_internal/libcrypto-3-x64.dll": STUB_SHA,
            "_internal/PySide6/libcrypto-3-x64.dll": hashlib.sha256(other).hexdigest(),
        },
    )
    problems = problems_of(run_gate(root, runtimes))
    assert not any("libcrypto-3-x64.dll" in p for p in problems), problems


def test_copy_in_one_folder_is_not_checked_against_the_digest_of_another(tmp_path):
    root, _runtimes = build_valid_bundle(tmp_path)
    (root / "_internal" / "PySide6" / "libcrypto-3-x64.dll").write_bytes(b"a second copy with other bytes")
    runtimes = _openssl_runtimes(
        tmp_path,
        {"_internal/libcrypto-3-x64.dll": STUB_SHA, "_internal/PySide6/libcrypto-3-x64.dll": STUB_SHA},
    )
    problems = problems_of(run_gate(root, runtimes))
    assert any("sha256 differs" in p and "_internal/PySide6/libcrypto-3-x64.dll" in p for p in problems), problems


def test_bundled_runtime_copy_without_a_path_key_fails_and_is_named(tmp_path):
    root, _runtimes = build_valid_bundle(tmp_path)
    (root / "_internal" / "PySide6" / "libcrypto-3-x64.dll").write_bytes(STUB)
    runtimes = _openssl_runtimes(tmp_path, {"_internal/libcrypto-3-x64.dll": STUB_SHA})
    problems = problems_of(run_gate(root, runtimes))
    assert any(
        "has no sha256 for its own path" in p and "_internal/PySide6/libcrypto-3-x64.dll" in p for p in problems
    ), problems


def test_name_only_digest_key_does_not_verify_a_bundled_copy(tmp_path):
    root, _runtimes = build_valid_bundle(tmp_path)
    runtimes = _openssl_runtimes(tmp_path, {"libcrypto-3-x64.dll": STUB_SHA})
    problems = problems_of(run_gate(root, runtimes))
    assert any(
        "has no sha256 for its own path" in p and "_internal/libcrypto-3-x64.dll" in p for p in problems
    ), problems


# ---------------------------------------------------------------- D3: PyInstaller's bootstrap modules

GPL2_COPYING = (
    "                    GNU GENERAL PUBLIC LICENSE\n"
    "                       Version 2, June 1991\n"
    "Bootloader Exception\n"
)
APACHE2_COPYING = (
    "                                 Apache License\n"
    "                           Version 2.0, January 2004\n"
)


def _pyinstaller_bundle(tmp_path: Path) -> Path:
    """A valid bundle whose NOTICE.md names PyInstaller and whose THIRD_PARTY_LICENSES.txt carries both
    PyInstaller licence texts: the Bootloader Exception (GPL-2) and the Apache-2.0 text of the run-time hooks."""
    root, _runtimes = build_valid_bundle(tmp_path)
    (root / "NOTICE.md").write_text(
        NOTICE + "PyInstaller 6.0.0 (GPL-2.0-or-later WITH Bootloader-exception; Apache-2.0): bootloader and run-time hooks.\n",
        encoding="utf-8",
    )
    bootloader = "== PyInstaller 6.0.0 (GPL-2.0-or-later WITH Bootloader-exception) ==\n" + GPL2_COPYING + BODY
    hooks = "== PyInstaller 6.0.0 (Apache-2.0) ==\n" + APACHE2_COPYING + BODY
    (root / "THIRD_PARTY_LICENSES.txt").write_text(third_party_text() + bootloader + hooks + "\n", encoding="utf-8")
    return root


def test_pyinstaller_runtime_hooks_and_bootloader_pass_under_their_own_licences(tmp_path):
    root = _pyinstaller_bundle(tmp_path)
    manifest = vb.load_manifest(MANIFEST)
    names = ["_pyi_rth_utils", "_pyi_rth_utils.qt", "pyiboot01_bootstrap"]
    assert vb.pyz_owner_problems(names, manifest, dists=[], root=root) == []


def test_pyinstaller_bootstrap_module_fails_when_notice_does_not_name_pyinstaller(tmp_path):
    root = _pyinstaller_bundle(tmp_path)
    (root / "NOTICE.md").write_text(NOTICE, encoding="utf-8")
    manifest = vb.load_manifest(MANIFEST)
    problems = vb.pyz_owner_problems(["_pyi_rth_utils"], manifest, dists=[], root=root)
    assert any(
        "PYZ module _pyi_rth_utils has no owning distribution" in p and "NOTICE.md does not name PyInstaller" in p
        for p in problems
    ), problems


def test_pyinstaller_bootstrap_module_fails_without_the_copying_text(tmp_path):
    root = _pyinstaller_bundle(tmp_path)
    (root / "THIRD_PARTY_LICENSES.txt").write_text(third_party_text(), encoding="utf-8")
    manifest = vb.load_manifest(MANIFEST)
    problems = vb.pyz_owner_problems(["_pyi_rth_utils"], manifest, dists=[], root=root)
    assert any("_pyi_rth_utils" in p and "COPYING text" in p for p in problems), problems


def test_gpl2_module_that_is_not_pyinstaller_fails_and_is_named(tmp_path):
    root = _pyinstaller_bundle(tmp_path)
    manifest = vb.load_manifest(MANIFEST)
    dist = FakeDist("gpltwo", "1.0", {"License-Expression": "GPL-2.0-only"}, files=["gpltwomod.py"], root=tmp_path)
    problems = vb.pyz_owner_problems(["gpltwomod"], manifest, dists=[dist], root=root)
    assert any("gpltwo" in p and "copyleft" in p and "gpltwomod" in p for p in problems), problems


def test_pyinstaller_exception_licence_does_not_cover_other_modules(tmp_path):
    root = _pyinstaller_bundle(tmp_path)
    manifest = vb.load_manifest(MANIFEST)
    dist = FakeDist(
        "pyiexc",
        "1.0",
        {"License-Expression": "GPL-2.0-or-later WITH Bootloader-exception"},
        files=["pyiexcmod.py"],
        root=tmp_path,
    )
    problems = vb.pyz_owner_problems(["pyiexcmod"], manifest, dists=[dist], root=root)
    assert any("pyiexc" in p and "copyleft" in p and "pyiexcmod" in p for p in problems), problems


# ---------------------------------------------------------------- D4: the LGPL Qt extension binaries


def _qt_extension_runtimes(tmp_path: Path, path: str, file_name: str, digest: str) -> Path:
    """native_runtimes.toml plus one LGPL-3.0-only runtime that lists `file_name` at bundle path `path`."""
    entry = (
        "\n[[runtime]]\n"
        'name = "Qt for Python extension binaries"\n'
        'version = "6.12.0"\n'
        'spdx = "LGPL-3.0-only"\n'
        f'files = ["{file_name}"]\n'
        'notice_url = "https://code.qt.io/cgit/pyside/pyside-setup.git"\n\n'
        "[runtime.sha256]\n"
        f'"{path}" = "{digest}"\n'
    )
    runtimes = tmp_path / "qt_runtimes.toml"
    runtimes.write_text(RUNTIMES_TOML + entry, encoding="utf-8")
    return runtimes


@pytest.mark.parametrize(
    ("folder", "file_name", "dist"),
    [("PySide6", "pyside6.abi3.dll", "PySide6"), ("shiboken6", "shiboken6.abi3.dll", "shiboken6")],
)
def test_lgpl_runtime_under_a_pyside_folder_passes(tmp_path, folder, file_name, dist):
    root, _runtimes = build_valid_bundle(tmp_path)
    data = b"Qt extension binary stub"
    rel = f"_internal/{folder}/{file_name}"
    (root / "_internal" / folder).mkdir(exist_ok=True)
    (root / rel).write_bytes(data)
    _claim(root / "_internal" / f"{dist}-6.12.0.dist-info", f"{folder}/{file_name}")
    runtimes = _qt_extension_runtimes(tmp_path, rel, file_name, hashlib.sha256(data).hexdigest())
    sections = run_gate(root, runtimes)
    assert section_problems(sections, "native runtimes listed") == [], problems_of(sections)
    assert not any(file_name in p for p in problems_of(sections)), problems_of(sections)


def test_lgpl_runtime_outside_pyside_folders_fails_and_is_named(tmp_path):
    root, _runtimes = build_valid_bundle(tmp_path)
    data = b"Qt extension binary stub"
    (root / "_internal" / "pyside6.abi3.dll").write_bytes(data)
    runtimes = _qt_extension_runtimes(
        tmp_path, "_internal/pyside6.abi3.dll", "pyside6.abi3.dll", hashlib.sha256(data).hexdigest()
    )
    sections = run_gate(root, runtimes)
    listed = section_problems(sections, "native runtimes listed")
    assert any("_internal/pyside6.abi3.dll" in p and "outside them" in p for p in listed), listed
    # The rejected runtime is not inventoried, so its file is also reported as not inventoried.
    native = section_problems(sections, "native binaries inventoried")
    assert any("pyside6.abi3.dll" in p and "not inventoried" in p for p in native), native


# ---------------------------------------------------------------- Microsoft runtime files are not bundled

MS_SECTION = "Microsoft runtime not bundled"
IMPORT_SECTION = "native imports bundled or Windows OS DLLs"
MS_RUNTIME_NAMES = {
    "MSVCP140.dll", "MSVCP140_1.dll", "MSVCP140_2.dll", "VCRUNTIME140.dll", "VCRUNTIME140_1.dll", "ucrtbase.dll",
}
MS_RUNTIME_PATTERNS = {"MSVCP140*.dll", "VCRUNTIME140*.dll", "ucrtbase.dll", "api-ms-win-*.dll"}
VC_REDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"


def _load_build():
    """packaging/build.py as a module. Importing it has no side effects, and main() is not run."""
    spec = importlib.util.spec_from_file_location("build_for_license_gate", REPO / "packaging" / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "rel",
    [
        "MSVCP140.dll",
        "_internal/MSVCP140.dll",
        "_internal/PySide6/MSVCP140_1.dll",
        "_internal/PySide6/MSVCP140_2.dll",
        "_internal/PySide6/VCRUNTIME140.dll",
        "_internal/shiboken6/VCRUNTIME140_1.dll",
        "_internal/ucrtbase.dll",
        "_internal/api-ms-win-core-synch-l1-1-0.dll",
        "_internal/PySide6/plugins/styles/api-ms-win-crt-time-l1-1-0.dll",
    ],
)
def test_microsoft_runtime_file_in_the_bundle_fails_and_is_named(tmp_path, rel):
    root, runtimes = build_valid_bundle(tmp_path)
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(STUB)
    problems = section_problems(run_gate(root, runtimes), MS_SECTION)
    assert any(rel in p and "must not be bundled" in p for p in problems), problems


def test_microsoft_runtime_file_name_is_matched_whatever_its_case(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    (root / "_internal" / "msvcp140_1.DLL").write_bytes(STUB)
    problems = section_problems(run_gate(root, runtimes), MS_SECTION)
    assert any("msvcp140_1.DLL" in p for p in problems), problems


def test_valid_bundle_has_the_microsoft_check_and_it_passes(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    sections = run_gate(root, runtimes)
    assert MS_SECTION in [title for title, _problems in sections]
    assert section_problems(sections, MS_SECTION) == []


def test_microsoft_check_fails_closed_without_its_manifest_table(tmp_path):
    root, _runtimes = build_valid_bundle(tmp_path)
    problems = vb._run_check(vb.check_microsoft_runtime_absent, root, {})
    assert problems and all("(fail closed)" in p for p in problems), problems


def test_microsoft_runtime_import_is_accepted_while_its_file_is_absent(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    imports = dict(VALID_IMPORTS)
    imports["QtCore.pyd"] = [
        "python3.dll", "KERNEL32.dll", "VCRUNTIME140.dll", "MSVCP140_1.dll", "ucrtbase.dll",
        "api-ms-win-crt-runtime-l1-1-0.dll",
    ]
    sections = run_gate(root, runtimes, imports)
    assert section_problems(sections, IMPORT_SECTION) == [], problems_of(sections)


def test_microsoft_runtime_import_is_refused_when_its_file_is_bundled(tmp_path):
    root, runtimes = build_valid_bundle(tmp_path)
    (root / "_internal" / "VCRUNTIME140.dll").write_bytes(STUB)
    imports = dict(VALID_IMPORTS)
    imports["QtCore.pyd"] = ["python3.dll", "VCRUNTIME140.dll"]
    sections = run_gate(root, runtimes, imports)
    problems = section_problems(sections, IMPORT_SECTION)
    assert any("QtCore.pyd" in p and "vcruntime140.dll" in p.lower() and "must not be bundled" in p for p in problems), problems


def test_manifest_lists_the_microsoft_prerequisites_and_no_longer_lists_ucrt_as_a_system_dll():
    manifest = vb.load_manifest(MANIFEST)
    table = manifest["microsoft_prerequisites"]
    assert set(table["names"]) == MS_RUNTIME_NAMES
    assert set(table["patterns"]) == MS_RUNTIME_PATTERNS
    assert table["redistributable_url"] == VC_REDIST_URL
    assert "ucrtbase.dll" not in manifest["system_dlls"]["names"]
    assert manifest["system_dlls"]["prefixes"] == []


def test_build_removal_patterns_are_the_manifest_patterns():
    build = _load_build()
    manifest = vb.load_manifest(MANIFEST)
    assert sorted(build.MICROSOFT_RUNTIME_PATTERNS) == sorted(manifest["microsoft_prerequisites"]["patterns"])
    build.check_microsoft_manifest(manifest)


def test_build_refuses_to_run_when_the_patterns_drift():
    build = _load_build()
    drifted = {"microsoft_prerequisites": {"patterns": ["MSVCP140*.dll"], "redistributable_url": VC_REDIST_URL}}
    with pytest.raises(SystemExit, match="differ from MICROSOFT_RUNTIME_PATTERNS"):
        build.check_microsoft_manifest(drifted)


def test_build_refuses_to_run_when_the_redistributable_link_differs():
    build = _load_build()
    manifest = vb.load_manifest(MANIFEST)
    changed = {
        "microsoft_prerequisites": dict(manifest["microsoft_prerequisites"], redistributable_url="https://example.com/x.exe")
    }
    with pytest.raises(SystemExit, match="redistributable_url"):
        build.check_microsoft_manifest(changed)


def test_build_removes_microsoft_runtime_files_in_every_folder_and_logs_each(tmp_path, capsys):
    build = _load_build()
    bundle = tmp_path / "MeridianDesktop"
    doomed = [
        "_internal/MSVCP140.dll",
        "_internal/MSVCP140_1.dll",
        "_internal/VCRUNTIME140.dll",
        "_internal/VCRUNTIME140_1.dll",
        "_internal/PySide6/MSVCP140_1.dll",
        "_internal/PySide6/MSVCP140_2.dll",
        "_internal/PySide6/VCRUNTIME140.dll",
        "_internal/shiboken6/MSVCP140.dll",
        "_internal/shiboken6/VCRUNTIME140_1.dll",
        "_internal/ucrtbase.dll",
        "_internal/api-ms-win-core-synch-l1-1-0.dll",
        "_internal/PySide6/plugins/styles/api-ms-win-crt-time-l1-1-0.dll",
    ]
    kept = [
        "MeridianDesktop.exe",
        "_internal/python312.dll",
        "_internal/PySide6/Qt6Core.dll",
        "_internal/PySide6/plugins/platforms/qwindows.dll",
    ]
    for rel in doomed + kept:
        target = bundle / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(STUB)
    removed = build.remove_microsoft_runtime(bundle)
    assert sorted(removed) == sorted(doomed)
    for rel in doomed:
        assert not (bundle / rel).exists(), rel
    for rel in kept:
        assert (bundle / rel).is_file(), rel
    out = capsys.readouterr().out
    for rel in doomed:
        assert f"removed Microsoft runtime file: {rel}" in out, rel


def test_failed_removal_stops_the_build(tmp_path, monkeypatch):
    build = _load_build()
    bundle = tmp_path / "MeridianDesktop"
    (bundle / "_internal").mkdir(parents=True)
    (bundle / "_internal" / "MSVCP140.dll").write_bytes(STUB)

    def refuse(self, *args, **kwargs):
        raise PermissionError("held open by another process")

    monkeypatch.setattr(build.Path, "unlink", refuse)
    with pytest.raises(SystemExit, match="could not remove Microsoft runtime file _internal/MSVCP140.dll"):
        build.remove_microsoft_runtime(bundle)


def test_bundle_readme_names_the_redistributable_link_and_the_ucrt(tmp_path, monkeypatch):
    build = _load_build()
    text = (REPO / "packaging" / "README.bundle.txt").read_text(encoding="utf-8")
    flat = " ".join(text.split())
    assert "Microsoft Visual C++ Redistributable (x64) 2015-2022" in flat
    assert "NOT included in this folder" in flat
    assert VC_REDIST_URL in flat
    assert "Universal CRT" in flat and "Windows 10 and later" in flat
    build.check_bundle_readme_source()
    target = tmp_path / "README.txt"
    monkeypatch.setattr(build, "BUNDLE_README", target)
    build.write_bundle_readme()
    assert target.read_text(encoding="utf-8") == text


def test_native_runtimes_list_no_microsoft_file():
    manifest = vb.load_manifest(MANIFEST)
    names, patterns = vb.load_microsoft_prerequisites(manifest)
    data = tomllib.loads((REPO / "packaging" / "native_runtimes.toml").read_text(encoding="utf-8"))
    for entry in data["runtime"]:
        assert "microsoft" not in entry["name"].lower(), entry["name"]
        for file_name in entry["files"]:
            assert not vb.is_microsoft_runtime_name(file_name, names, patterns), file_name


def test_eula_has_a_requirements_section_before_network_use():
    text = (REPO / "EULA.txt").read_text(encoding="utf-8")
    assert text.index("7. Requirements") < text.index("8. Network use") < text.index("9. Termination")
    section = text[text.index("7. Requirements"):text.index("8. Network use")]
    flat = " ".join(section.split())
    assert "Microsoft Visual C++ 2015-2022 Redistributable (x64)" in flat
    assert VC_REDIST_URL in section
    assert "Microsoft's own licence governs that download" in flat


def test_notice_has_prerequisites_and_no_microsoft_runtime_rows():
    text = (REPO / "NOTICE.md").read_text(encoding="utf-8")
    flat = " ".join(text.split())
    assert "## Prerequisites" in text
    assert VC_REDIST_URL in flat
    assert "Universal CRT" in flat
    assert "| Microsoft VC++ runtime |" not in text
    assert "| Microsoft UCRT |" not in text
    assert "LicenseRef-Microsoft" not in text
