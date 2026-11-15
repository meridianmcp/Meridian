#!/usr/bin/env python3
"""Build the MeridianDesktop ONEDIR bundle with PyInstaller, then add its licences.

ONEDIR (not onefile) keeps Qt and PySide6 as separate files so the LGPL
libraries can be replaced by the user. Qt modules this app does not use are
excluded so they are not bundled.

Steps, in order. A failed check stops the build:
1. Pin check: every licence text the build reads or ships must match its
   sha256 in packaging/licenses/SHA256SUMS, and each text must be the text it
   claims to be (LGPL, GPL, MSL). The Microsoft runtime patterns in this file must
   equal [microsoft_prerequisites] in packaging/lgpl_manifest.toml.
2. PyInstaller runs with --copy-metadata for each bundled distribution, so each
   dist-info, with its licence files, is in _internal. setuptools is excluded:
   nothing the app or its direct dependencies imports it, and its vendored
   packages carry no licence files in the bundle. The conda env's Library/bin is first on
   its PATH (pyinstaller_env), so the DLLs it bundles are the copies native_runtimes.toml pins.
3. Forbidden Qt files are pruned (packaging/lgpl_manifest.toml). Microsoft runtime
   files (the Visual C++ runtime, ucrtbase.dll and the api-ms-win- stubs) are removed
   from every folder of the bundle, and each removal is logged. The build fails if one
   cannot be removed. packaging/README.bundle.txt is then written to the bundle root as
   README.txt, which tells the user to install the Visual C++ redistributable first.
4. NOTICE.md, OFFER-SOURCE.md, EULA.txt and LICENSES/ are copied to the bundle root.
5. THIRD_PARTY_LICENSES.txt is written at the bundle root. It has one section per
   bundled distribution (from its dist-info), one per native runtime (from
   packaging/native_runtimes.toml) and one for PyInstaller (its bootloader and
   boot module under GPL-2.0-or-later WITH Bootloader-exception, and its run-time hooks
   under Apache-2.0). Every section starts with the line "== name version (SPDX) ==".
   The build fails, naming the item, when a licence file or a version is missing,
   when a licence is GPL-only or AGPL, when a native runtime DLL in the bundle is
   not listed in native_runtimes.toml, when a bundled copy of a listed file has no
   sha256 for its path there, or when NOTICE.md does not name PyInstaller and that
   licence. A bundled copy whose bytes differ from the sha256 declared for its
   path is printed as RELEASE BLOCKED. It fails the build with --release.

Release gate: the build succeeds while EULA.txt or OFFER-SOURCE.md carries the
legal-review marker "DRAFT - NOT FOR RELEASE", and it prints a warning. With
--release the build fails instead, and it also fails while either file contains
TBD text. Counsel removes the marker after review.

Next step: python packaging/verify_bundle.py
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[1]
ENTRY = REPO / "meridian_desktop" / "__main__.py"
APP_NAME = "MeridianDesktop"
DIST_DIR = REPO / "dist"
BUILD_DIR = REPO / "build"
BUNDLE_DIR = DIST_DIR / APP_NAME
INTERNAL_DIR = BUNDLE_DIR / "_internal"  # PyInstaller 6 keeps dist-infos and data here
# The exe's embedded icon, which the Start Menu and desktop shortcuts show. Made by make_icon.ps1.
ICON = REPO / "packaging" / "assets" / "meridian-desktop.ico"
MANIFEST = REPO / "packaging" / "lgpl_manifest.toml"
RUNTIMES = REPO / "packaging" / "native_runtimes.toml"
SHA256SUMS = REPO / "packaging" / "licenses" / "SHA256SUMS"
NATIVE_TEXTS = REPO / "packaging" / "licenses" / "native"
THIRD_PARTY = BUNDLE_DIR / "THIRD_PARTY_LICENSES.txt"

RELEASE_MARKER = "DRAFT - NOT FOR RELEASE"
LGPL_ELECTED = "LGPL-3.0-only"
# PyInstaller's own code is two licences (its COPYING.txt, and the SPDX headers in its source).
# The bootloader and the boot module (pyiboot*) are under the Bootloader Exception. The run-time
# hooks and fake modules (_pyi_rth_*, pyi_rth_*, pyi_splash*) are Apache-2.0. Each section may
# appear in THIRD_PARTY_LICENSES.txt only while NOTICE.md names PyInstaller and its licence.
PYINSTALLER_BOOTLOADER_LICENCE = "GPL-2.0-or-later WITH Bootloader-exception"
PYINSTALLER_HOOKS_LICENCE = "Apache-2.0"
APACHE2_TEXT = REPO / "packaging" / "licenses" / "Apache-2.0.txt"

# Qt modules the skeleton does not use. Excluding them keeps their DLLs and
# plugins out of the bundle. verify_bundle.py fails the build if any of them
# appear anyway.
UNUSED_PYSIDE_MODULES = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DAnimation",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQml",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtSql",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtLocation",
    "PySide6.QtSerialPort",
    "PySide6.QtSensors",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtStateMachine",
    "PySide6.QtNetworkAuth",
    "PySide6.QtHttpServer",
    "PySide6.QtGraphs",
    "PySide6.QtTest",
    "PySide6.QtXml",
]

# Distributions whose code the bundle contains, besides the manifest's list. Each
# gets --copy-metadata, so its dist-info (and licence files) lands in _internal.
# Names are distribution names. keyring itself is in the manifest's list. keyring's
# jaraco and more-itertools dependencies are imported by keyring, and keyring's
# Windows backend imports pywin32-ctypes. certifi supplies the CA bundle that the
# hosted client uses for the default host (hosted_client._tls_context_for).
EXTRA_METADATA = [
    "jaraco.classes",
    "jaraco.functools",
    "jaraco.context",
    "more-itertools",
    "pywin32-ctypes",
    "certifi",
]

# Not imported by the app or by its direct dependencies (checked against PySide6,
# shiboken6, keyring, jaraco and more-itertools). Without this, PyInstaller's
# distutils hook bundles setuptools and its vendored packages, which have no
# licence files in the bundle.
EXCLUDED_MODULES = ["setuptools", "pkg_resources", "_distutils_hack"]

# SPDX identifiers for distributions whose METADATA carries no SPDX expression.
# jaraco.classes gives only the classifier "License :: OSI Approved :: MIT License";
# its shipped LICENSE file is the MIT text.
SPDX_OVERRIDES = {"jaraco-classes": "MIT"}

# Native DLL names. A DLL in the bundle that matches a pattern must be listed in
# native_runtimes.toml, or the build fails. The Microsoft runtime files match some of
# these patterns, but they are removed before this check runs (see below).
NATIVE_DLL_PATTERNS = [
    r"python3\d*\.dll",
    r"libcrypto-\d-x64\.dll",
    r"libssl-\d-x64\.dll",
    r"ffi-\d+\.dll",
    r"libexpat\.dll",
    r"libbz2\.dll",
    r"liblzma\.dll",
    r"zlib\d*\.dll",
    r"sqlite3\.dll",
    r"msvcp140\S*\.dll",
    r"vcruntime140\S*\.dll",
    r"ucrtbase\.dll",
    r"(?:api-ms|ext-ms)-win-\S+\.dll",
]

# Microsoft runtime files are NOT shipped. Users install Microsoft's own Visual C++ 2015-2022
# Redistributable (x64), from MICROSOFT_VC_REDIST_URL. The Universal CRT comes with Windows 10
# and later. These patterns are matched, case-insensitively, against every file name in the
# bundle, in every folder. verify_bundle.py reads the same patterns from [microsoft_prerequisites]
# in packaging/lgpl_manifest.toml, and check_microsoft_manifest() fails the build when they differ.
MICROSOFT_RUNTIME_PATTERNS = ("MSVCP140*.dll", "VCRUNTIME140*.dll", "ucrtbase.dll", "api-ms-win-*.dll")
MICROSOFT_VC_REDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
BUNDLE_README_SOURCE = REPO / "packaging" / "README.bundle.txt"
BUNDLE_README = BUNDLE_DIR / "README.txt"

LICENCE_FILE_NAME = re.compile(r"(LICEN[CS]E|COPYING|NOTICE)", re.IGNORECASE)

# Each licence text must contain these markers. They confirm the text is the one
# its file name claims, not some other file.
LICENCE_MARKERS = {
    "LICENSES/LGPL-3.0.txt": ("GNU LESSER GENERAL PUBLIC LICENSE", "Version 3, 29 June 2007"),
    "LICENSES/GPL-3.0.txt": ("GNU GENERAL PUBLIC LICENSE", "Version 3, 29 June 2007"),
    "LICENSES/MSL-1.0.txt": ("Meridian Source License 1.0 (MSL-1.0)", "must retain this license text"),
}

NOTICE_FILES = ["NOTICE.md", "OFFER-SOURCE.md", "EULA.txt"]
NOTICE_DIRS = ["LICENSES"]
REQUIRED_LICENCES = ["LICENSES/LGPL-3.0.txt", "LICENSES/GPL-3.0.txt", "LICENSES/MSL-1.0.txt"]

PREAMBLE = [
    "Third-party licence texts for MeridianDesktop. Written by packaging/build.py; do not edit by hand.",
    "Each section starts with a header line of the form '== name version (SPDX) =='.",
    "PySide6, PySide6_Essentials, PySide6_Addons and shiboken6 are used under LGPL-3.0-only, the licence elected for Qt.",
    "The LGPL-3.0 text is in LICENSES/LGPL-3.0.txt and the source offer is in OFFER-SOURCE.md. NOTICE.md lists everything.",
]

PRUNE_REPORT = BUILD_DIR / "pruned.txt"


def pyinstaller_command(metadata: list[str]) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--icon",
        str(ICON),
        "--name",
        APP_NAME,
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(BUILD_DIR / "pyinstaller"),
        "--specpath",
        str(BUILD_DIR),
        "--paths",
        str(REPO),
    ]
    for module in UNUSED_PYSIDE_MODULES:
        cmd += ["--exclude-module", module]
    for module in EXCLUDED_MODULES:
        cmd += ["--exclude-module", module]
    for dist in metadata:
        cmd += ["--copy-metadata", dist]
    cmd.append(str(ENTRY))
    return cmd


def runtime_bin(prefix: str | Path | None = None) -> Path:
    """The folder where the conda env keeps its native DLLs: OpenSSL, libffi, expat, bzip2, XZ, zlib and SQLite."""
    return Path(prefix or sys.base_prefix) / "Library" / "bin"


def pyinstaller_env(prefix: str | Path | None = None) -> dict[str, str]:
    """The environment for PyInstaller, with the env's runtime folder first on PATH.

    On Windows, PyInstaller finds the DLLs that a .pyd or .dll needs by searching PATH. `pixi run`
    puts Library/bin on PATH, but a plain `python packaging/build.py` does not. Then the OpenSSL,
    libffi, expat, bzip2 and XZ DLLs were not found, and sqlite3.dll was taken from another program's
    folder (C:\\Program Files\\Amazon\\AWSCLIV2, file version 3.37.2.0, not the pinned 3.54.0).
    Putting the env's folder first makes PyInstaller bundle the copies that native_runtimes.toml pins.
    """
    env = dict(os.environ)
    folder = runtime_bin(prefix)
    if folder.is_dir():
        rest = [part for part in env.get("PATH", "").split(os.pathsep) if part]
        env["PATH"] = os.pathsep.join([str(folder), *rest])
    return env


def prune_forbidden(bundle_root: Path, qt: dict) -> list[str]:
    """Delete bundle files the manifest forbids, and plugin files not on the allowlist.

    PyInstaller collects Qt plugins and their DLL dependencies through hooks, so a
    clean build still contains forbidden files (verified on 2026-10-14: 53 problems
    before pruning). This removes them after collection. verify_bundle then checks
    what is left, so pruning cannot hide a violation. Every key is required: a
    missing key raises KeyError and the build fails.
    """
    forbidden = set(qt["forbidden_dlls"])
    forbidden_other = set(qt["forbidden_other_dlls"])
    allowed = set(qt["allowed_dlls"])
    allowed_plugin_files = set(qt["allowed_plugin_files"])
    removed: list[str] = []
    for path in sorted(bundle_root.rglob("*")):
        if not path.is_file():
            continue
        parts = path.relative_to(bundle_root).parts
        suffix = path.suffix.lower()
        stem = path.stem
        drop = False
        if suffix == ".dll" and (stem in forbidden or stem in forbidden_other):
            drop = True
        elif suffix == ".pyd" and "PySide6" in parts and stem.startswith("Qt"):
            dll = "Qt6" + stem[2:]
            drop = dll not in allowed
        elif "PySide6" in parts and "plugins" in parts:
            idx = parts.index("plugins")
            if len(parts) > idx + 2 and "/".join(parts[idx + 1 :]) not in allowed_plugin_files:
                drop = True
        if drop:
            path.unlink()
            removed.append(path.relative_to(bundle_root).as_posix())
    return removed


def check_microsoft_manifest(manifest: dict) -> None:
    """The removal patterns are the manifest's [microsoft_prerequisites] patterns, and the link is the same (fail closed)."""
    table = manifest.get("microsoft_prerequisites")
    if not isinstance(table, dict) or "patterns" not in table or "redistributable_url" not in table:
        raise SystemExit(f"{MANIFEST.name} has no [microsoft_prerequisites] patterns or redistributable_url (fail closed)")
    listed = sorted(str(pattern).lower() for pattern in table["patterns"])
    if listed != sorted(pattern.lower() for pattern in MICROSOFT_RUNTIME_PATTERNS):
        raise SystemExit(
            f"[microsoft_prerequisites] patterns in {MANIFEST.name} differ from MICROSOFT_RUNTIME_PATTERNS in "
            "build.py. Make the two lists the same (fail closed)"
        )
    if table["redistributable_url"] != MICROSOFT_VC_REDIST_URL:
        raise SystemExit(
            f"[microsoft_prerequisites] redistributable_url in {MANIFEST.name} differs from MICROSOFT_VC_REDIST_URL "
            "in build.py (fail closed)"
        )


def check_bundle_readme_source() -> None:
    """packaging/README.bundle.txt exists and gives the Visual C++ redistributable link (fail closed)."""
    if not BUNDLE_README_SOURCE.is_file():
        raise SystemExit(f"missing bundle README: {BUNDLE_README_SOURCE} (fail closed)")
    if MICROSOFT_VC_REDIST_URL not in BUNDLE_README_SOURCE.read_text(encoding="utf-8"):
        raise SystemExit(f"{BUNDLE_README_SOURCE.name} does not give {MICROSOFT_VC_REDIST_URL} (fail closed)")


def microsoft_runtime_files(bundle_root: Path) -> list[Path]:
    """Every file in the bundle, in any folder, whose name matches a Microsoft runtime pattern."""
    patterns = [pattern.lower() for pattern in MICROSOFT_RUNTIME_PATTERNS]
    return [
        path
        for path in sorted(bundle_root.rglob("*"))
        if path.is_file() and any(fnmatch.fnmatchcase(path.name.lower(), pattern) for pattern in patterns)
    ]


def remove_microsoft_runtime(bundle_root: Path) -> list[str]:
    """Delete every Microsoft runtime file from the bundle, log each removal, and return the removed paths.

    A failed removal stops the build. A second scan afterwards must find nothing, or the build stops too.
    """
    removed: list[str] = []
    for path in microsoft_runtime_files(bundle_root):
        rel = path.relative_to(bundle_root).as_posix()
        try:
            path.unlink()
        except OSError as exc:
            raise SystemExit(f"could not remove Microsoft runtime file {rel}: {exc} (fail closed)") from exc
        print(f"removed Microsoft runtime file: {rel}")
        removed.append(rel)
    left = [path.relative_to(bundle_root).as_posix() for path in microsoft_runtime_files(bundle_root)]
    if left:
        raise SystemExit(f"Microsoft runtime files are still in the bundle: {', '.join(left)} (fail closed)")
    return removed


def write_bundle_readme() -> None:
    """Copy packaging/README.bundle.txt to the bundle root as README.txt."""
    check_bundle_readme_source()
    shutil.copyfile(BUNDLE_README_SOURCE, BUNDLE_README)
    if not BUNDLE_README.is_file():
        raise SystemExit(f"bundle README was not written: {BUNDLE_README} (fail closed)")


# ---------------------------------------------------------------- licence pins


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_pins() -> dict[str, str]:
    """Parse SHA256SUMS: 'sha256  path' lines, with paths relative to the repo root."""
    pins: dict[str, str] = {}
    for number, line in enumerate(SHA256SUMS.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-f]{64})  (\S+)", line)
        if match is None:
            raise SystemExit(f"{SHA256SUMS.name} line {number} is not 'sha256  path' (fail closed)")
        digest, rel = match.groups()
        pins[rel] = digest
    return pins


def check_licence_pins() -> None:
    """Every licence text is pinned, and every pinned text still has its pinned hash."""
    pins = read_pins()
    for rel, digest in sorted(pins.items()):
        path = REPO / rel
        if not path.is_file():
            raise SystemExit(f"pinned licence file is missing: {rel} (fail closed)")
        if sha256_file(path) != digest:
            raise SystemExit(
                f"licence file changed since it was pinned: {rel}. Review it, then update "
                f"{SHA256SUMS.name} (fail closed)"
            )
    shipped = list((REPO / "LICENSES").glob("*")) + list(NATIVE_TEXTS.glob("*.txt"))
    for path in sorted(shipped):
        rel = path.relative_to(REPO).as_posix()
        if rel not in pins:
            raise SystemExit(f"licence file is not pinned in SHA256SUMS: {rel} (fail closed)")


def check_licence_texts() -> None:
    """Each licence text is the text its name claims, and the MSL copy equals LICENSE."""
    for rel, markers in LICENCE_MARKERS.items():
        # Line wrapping in the licence must not split a marker, so compare flattened text.
        text = " ".join((REPO / rel).read_text(encoding="utf-8").split())
        for marker in markers:
            if marker not in text:
                raise SystemExit(f"{rel} does not contain {marker!r}, so it is not the expected licence (fail closed)")
    if (REPO / "LICENSE").read_bytes() != (REPO / "LICENSES" / "MSL-1.0.txt").read_bytes():
        raise SystemExit("LICENSE differs from LICENSES/MSL-1.0.txt. Review LICENSE, then copy it again (fail closed)")


def release_problems() -> list[str]:
    """Reasons the bundle must not be released yet. An empty list means none."""
    problems: list[str] = []
    for name in ("EULA.txt", "OFFER-SOURCE.md"):
        text = (REPO / name).read_text(encoding="utf-8")
        if RELEASE_MARKER in text:
            problems.append(f"{name} carries the legal-review marker {RELEASE_MARKER!r}")
        if re.search(r"\bTBD\b", text):
            problems.append(f"{name} contains TBD text")
    return problems


# ---------------------------------------------------------------- runtimes


def is_relative_path(key: str) -> bool:
    """True for a forward-slash path relative to its root, with no '..' part and no drive letter."""
    if not key or "\\" in key or re.match(r"[A-Za-z]:", key):
        return False
    path = PurePosixPath(key)
    return not path.is_absolute() and ".." not in path.parts and str(path) == key


def load_runtimes() -> list[dict]:
    """Read packaging/native_runtimes.toml. Each sha256 key is a bundle-relative path (D2)."""
    data = tomllib.loads(RUNTIMES.read_text(encoding="utf-8"))
    runtimes = data.get("runtime", [])
    if not runtimes:
        raise SystemExit(f"{RUNTIMES.name} lists no [[runtime]] entries (fail closed)")
    pinned = read_pins()
    for entry in runtimes:
        name = entry.get("name", "<unnamed>")
        for key in ("name", "version", "spdx", "files", "notice_url"):
            if key not in entry:
                raise SystemExit(f"runtime {name} has no {key!r} in {RUNTIMES.name} (fail closed)")
        for key in ("version", "spdx"):
            if str(entry[key]).strip().lower() == "unknown":
                raise SystemExit(
                    f"runtime {name} has {key} 'unknown'. Read it from the pixi conda-meta before building (fail closed)"
                )
        if not isinstance(entry["files"], list) or not entry["files"]:
            raise SystemExit(f"runtime {name} lists no files in {RUNTIMES.name} (fail closed)")
        if not all(isinstance(f, str) and f for f in entry["files"]):
            raise SystemExit(f"runtime {name} has a file name that is not a non-empty string (fail closed)")
        listed = {f.lower() for f in entry["files"]}
        if len(listed) != len(entry["files"]):
            raise SystemExit(f"runtime {name} lists a file name twice in {RUNTIMES.name} (fail closed)")
        pins = entry.get("sha256")
        if not isinstance(pins, dict) or not pins:
            raise SystemExit(f"runtime {name} has no sha256 table in {RUNTIMES.name} (fail closed)")
        keys = [str(key) for key in pins]
        if len({key.lower() for key in keys}) != len(keys):
            raise SystemExit(f"runtime {name} has a sha256 path twice in {RUNTIMES.name}, ignoring case (fail closed)")
        for path_key, digest in pins.items():
            if not is_relative_path(str(path_key)):
                raise SystemExit(
                    f"runtime {name} has a sha256 key {path_key!r} that is not a path relative to the bundle root (fail closed)"
                )
            if PurePosixPath(str(path_key)).name.lower() not in listed:
                raise SystemExit(
                    f"runtime {name} has a sha256 for {path_key}, whose file name is not in its files list (fail closed)"
                )
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise SystemExit(
                    f"runtime {name} has a sha256 for {path_key} that is not 64 lower-case hex digits (fail closed)"
                )
        covered = {PurePosixPath(key).name.lower() for key in keys}
        missing = sorted(listed - covered)
        if missing:
            raise SystemExit(f"runtime {name} has no sha256 for {', '.join(missing)} in {RUNTIMES.name} (fail closed)")
        if "licence_file" in entry:
            licence = str(entry["licence_file"])
            if not is_relative_path(licence) or licence not in pinned:
                raise SystemExit(f"runtime {name} licence_file {licence} is not pinned in {SHA256SUMS.name} (fail closed)")
            if not (REPO / licence).is_file():
                raise SystemExit(f"runtime {name} licence_file {licence} is missing (fail closed)")
    return runtimes


def bundle_index() -> dict[str, list[Path]]:
    """Map each lower-cased file name in the bundle to its paths."""
    index: dict[str, list[Path]] = {}
    for path in BUNDLE_DIR.rglob("*"):
        if path.is_file():
            index.setdefault(path.name.lower(), []).append(path)
    return index


def check_runtime_files(runtimes: list[dict], index: dict[str, list[Path]]) -> None:
    """Each digest path is a file in the bundle, and each bundled copy of a listed file has its own path digest.

    Each native DLL in the bundle must be listed. A bundled runtime file with no path key fails.
    """
    listed: set[str] = set()
    keyed: dict[str, set[str]] = {}  # lower-case file name -> lower-case bundle paths with a digest
    for entry in runtimes:
        for path_key in entry["sha256"]:
            if not (BUNDLE_DIR / path_key).is_file():
                raise SystemExit(f"runtime {entry['name']} lists {path_key}, which is not in the bundle (fail closed)")
            keyed.setdefault(PurePosixPath(path_key).name.lower(), set()).add(path_key.lower())
        listed.update(name.lower() for name in entry["files"])
    for lower_name, paths in sorted(index.items()):
        if lower_name not in listed:
            continue
        for path in sorted(paths):
            rel = path.relative_to(BUNDLE_DIR).as_posix()
            if rel.lower() not in keyed.get(lower_name, set()):
                raise SystemExit(
                    f"runtime file in the bundle has no sha256 for its path in {RUNTIMES.name}: {rel} (fail closed)"
                )
    pattern = re.compile("|".join(f"(?:{p})" for p in NATIVE_DLL_PATTERNS), re.IGNORECASE)
    for lower_name, paths in sorted(index.items()):
        if pattern.fullmatch(lower_name) and lower_name not in listed:
            rel = paths[0].relative_to(BUNDLE_DIR).as_posix()
            raise SystemExit(f"native runtime DLL in the bundle is not listed in {RUNTIMES.name}: {rel} (fail closed)")


def runtime_copy_problems(runtimes: list[dict]) -> list[str]:
    """Bundled copies whose sha256 differs from the digest declared for their path.

    Each bundled copy has its own declared digest, keyed by its path relative to the bundle
    root. A copy with other bytes is a release problem, so the caller prints it as RELEASE
    BLOCKED. verify_bundle.py reports the same copies.
    """
    problems: list[str] = []
    for entry in runtimes:
        for path_key, want in entry["sha256"].items():
            got = sha256_file(BUNDLE_DIR / path_key)
            if got != want:
                problems.append(
                    f"runtime {entry['name']}: {path_key} has sha256 {got}, but {RUNTIMES.name} declares "
                    f"{want} for that path"
                )
    return sorted(problems)


def runtime_section(entry: dict) -> str:
    name, version, spdx = entry["name"], entry["version"], entry["spdx"]
    if "licence_file" in entry:
        text_path = REPO / entry["licence_file"]  # a pinned repo licence text, checked by load_runtimes
    else:
        text_path = NATIVE_TEXTS / f"{slug(name)}.txt"
    if not text_path.is_file():
        rel = text_path.relative_to(REPO).as_posix()
        raise SystemExit(f"no licence text for runtime {name}: expected {rel} (fail closed)")
    lines = [
        f"== {name} {version} ({spdx}) ==",
        f"Files listed in packaging/{RUNTIMES.name}: {len(entry['files'])} names, "
        f"{len(entry['sha256'])} bundled copies, each with a sha256 there",
        f"Upstream licence page: {entry['notice_url']}",
        "",
        f"--- licence text: {text_path.relative_to(REPO).as_posix()} ---",
        read_text(text_path).rstrip(),
    ]
    return "\n".join(lines) + "\n"


def bootloader_section() -> str:
    """PyInstaller's bootloader and boot module, under PYINSTALLER_BOOTLOADER_LICENCE.

    The section carries PyInstaller's COPYING.txt in full. The build fails when that text has no
    Bootloader Exception section, because the licence depends on it.
    """
    dist = importlib.metadata.distribution("pyinstaller")
    licence = dist.read_text("licenses/COPYING.txt")
    if not licence:
        raise SystemExit("PyInstaller's licenses/COPYING.txt is not in the build environment (fail closed)")
    if "Bootloader Exception" not in licence:
        raise SystemExit("PyInstaller's licenses/COPYING.txt has no Bootloader Exception section (fail closed)")
    lines = [
        f"== PyInstaller {dist.version} ({PYINSTALLER_BOOTLOADER_LICENCE}) ==",
        "PyInstaller builds MeridianDesktop.exe. Its bootloader is compiled into the exe, and its boot module",
        "(pyiboot01_bootstrap) is embedded in the exe. The licence named in the header covers the bootloader and",
        "the boot module, and its Bootloader Exception is in the text below. The text is PyInstaller's COPYING.txt.",
        "",
        "--- licence file: PyInstaller licenses/COPYING.txt ---",
        licence.rstrip(),
    ]
    return "\n".join(lines) + "\n"


def hooks_section() -> str:
    """PyInstaller's run-time hooks and fake modules, under Apache-2.0 (COPYING.txt, Run-time Hooks).

    They are embedded in the exe (_pyi_rth_*, pyi_rth_*, pyi_splash*). Apache-2.0 section 4 needs a
    copy of the licence with the redistribution, so the full text is included.
    """
    dist = importlib.metadata.distribution("pyinstaller")
    text = APACHE2_TEXT.read_text(encoding="utf-8") if APACHE2_TEXT.is_file() else ""
    if "Apache License" not in text or "Version 2.0, January 2004" not in text:
        raise SystemExit("packaging/licenses/Apache-2.0.txt is missing or is not the Apache-2.0 text (fail closed)")
    lines = [
        f"== PyInstaller {dist.version} ({PYINSTALLER_HOOKS_LICENCE}) ==",
        "PyInstaller's run-time hooks and fake modules are embedded in MeridianDesktop.exe (for example",
        "_pyi_rth_utils and pyi_rth_* modules). They are under the Apache License 2.0 text below.",
        "",
        "--- licence file: packaging/licenses/Apache-2.0.txt ---",
        text.rstrip(),
    ]
    return "\n".join(lines) + "\n"


def check_pyinstaller_notice() -> None:
    """The PyInstaller section (D3) may be written only while NOTICE.md names PyInstaller and its licence."""
    text = " ".join((REPO / "NOTICE.md").read_text(encoding="utf-8").split())
    for marker in ("PyInstaller", PYINSTALLER_BOOTLOADER_LICENCE, PYINSTALLER_HOOKS_LICENCE):
        if marker not in text:
            raise SystemExit(f"NOTICE.md does not name {marker!r}, which the PyInstaller section needs (fail closed)")


# ---------------------------------------------------------------- distributions


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def is_lgpl_family(key: str) -> bool:
    """PySide6 and shiboken6 distributions carry the Qt licence election."""
    return key.startswith("pyside6") or key == "shiboken6"


def metadata_of(dist_info: Path):
    meta = importlib.metadata.PathDistribution(dist_info).metadata
    for key in ("Name", "Version"):
        if not meta.get(key):
            raise SystemExit(f"{dist_info.name} has no {key} in its METADATA (fail closed)")
    return meta


def find_dist_infos(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.dist-info") if p.is_dir())


def check_metadata_present(dist_infos: list[Path], required: list[str]) -> None:
    present = {normalize(metadata_of(p)["Name"]) for p in dist_infos}
    missing = [name for name in required if normalize(name) not in present]
    if missing:
        raise SystemExit(
            "no dist-info in _internal for: " + ", ".join(missing) + ". Add them to --copy-metadata (fail closed)"
        )


def licence_problem(spdx: str) -> str | None:
    """Why a licence outside the Qt family is refused, or None when it is allowed."""
    if not spdx or spdx.upper() in {"UNKNOWN", "NONE"}:
        return "no licence declared"
    if "AGPL" in spdx.upper():
        return f"AGPL licence not allowed in the bundle ({spdx})"
    if "LGPL" in spdx.upper():
        return f"LGPL licence outside the elected Qt family needs owner review ({spdx})"
    options = [part.strip() for part in re.split(r"\s+OR\s+", spdx)]
    if all("GPL" in option.upper() for option in options):
        return f"GPL-only licence not allowed in the bundle ({spdx})"
    return None


def licence_files(dist_info: Path, meta) -> list[Path]:
    found = [p for p in sorted(dist_info.rglob("*")) if p.is_file() and LICENCE_FILE_NAME.match(p.name)]
    for ref in meta.get_all("License-File") or []:
        for candidate in (dist_info / "licenses" / ref, dist_info / ref):
            if candidate.is_file():
                if candidate not in found:
                    found.append(candidate)
                break
    return found


def distribution_section(dist_info: Path) -> str:
    meta = metadata_of(dist_info)
    name = meta["Name"].strip()
    version = meta["Version"].strip()
    key = normalize(name)
    raw = " ".join((meta.get("License-Expression") or meta.get("License") or "").split())
    notes: list[str] = []
    if is_lgpl_family(key):
        if LGPL_ELECTED not in re.split(r"\s+OR\s+", raw):
            raise SystemExit(
                f"{name} {version}: the Qt family must offer {LGPL_ELECTED}, but License is {raw!r} (fail closed)"
            )
        spdx = LGPL_ELECTED
        notes = [
            f"Licence elected by this project: {LGPL_ELECTED}. Upstream License field: {raw}.",
            "Licence text: LICENSES/LGPL-3.0.txt. Source offer: OFFER-SOURCE.md.",
            "The licence file listed below is the one the distribution declares. It is not the elected licence.",
        ]
    else:
        spdx = SPDX_OVERRIDES.get(key, raw)
        problem = licence_problem(spdx)
        if problem:
            raise SystemExit(f"{name} {version}: {problem} (fail closed)")
    files = licence_files(dist_info, meta)
    if not files:
        raise SystemExit(f"{name} {version} has no licence file in {dist_info.name} (fail closed)")
    lines = [f"== {name} {version} ({spdx}) =="]
    lines += notes
    for path in files:
        rel = path.relative_to(BUNDLE_DIR).as_posix()
        lines += ["", f"--- licence file: {rel} ---", read_text(path).rstrip()]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- steps


def copy_notices() -> None:
    for name in NOTICE_FILES:
        src = REPO / name
        if not src.is_file():
            raise SystemExit(f"missing notice file: {src}")
        shutil.copy2(src, BUNDLE_DIR / name)
    for name in NOTICE_DIRS:
        src = REPO / name
        if not src.is_dir():
            raise SystemExit(f"missing notice directory: {src}")
        dst = BUNDLE_DIR / name
        if dst.exists():
            shutil.rmtree(dst)  # a rebuild must not keep licence files that were removed
        shutil.copytree(src, dst)
    for rel in REQUIRED_LICENCES:
        if not (BUNDLE_DIR / rel).is_file():
            raise SystemExit(f"bundle is missing {rel} (fail closed)")


def metadata_distributions(manifest: dict) -> list[str]:
    """Distributions to copy metadata for: the manifest's list, then the extras, without repeats."""
    return list(dict.fromkeys(list(manifest["bundled"]["distributions"]) + EXTRA_METADATA))


def write_third_party(manifest: dict, runtimes: list[dict]) -> list[str]:
    """Write THIRD_PARTY_LICENSES.txt. Returns the RELEASE BLOCKED copy problems, if any."""
    if not INTERNAL_DIR.is_dir():
        raise SystemExit(f"bundle has no _internal folder: {INTERNAL_DIR} (fail closed)")
    dist_infos = find_dist_infos(INTERNAL_DIR)
    check_metadata_present(dist_infos, metadata_distributions(manifest))
    index = bundle_index()
    check_runtime_files(runtimes, index)

    ordered = sorted(dist_infos, key=lambda p: (normalize(metadata_of(p)["Name"]), metadata_of(p)["Version"]))
    sections = [distribution_section(p) for p in ordered]
    sections += [runtime_section(e) for e in sorted(runtimes, key=lambda e: e["name"].lower())]
    sections.append(bootloader_section())
    sections.append(hooks_section())
    text = "\n".join(PREAMBLE) + "\n\n" + "\n".join(sections)
    THIRD_PARTY.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {THIRD_PARTY.name}: {len(sections)} sections")
    return runtime_copy_problems(runtimes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the MeridianDesktop ONEDIR bundle.")
    parser.add_argument(
        "--release",
        action="store_true",
        help="fail while EULA.txt or OFFER-SOURCE.md carries the legal-review marker or TBD text",
    )
    args = parser.parse_args(argv)
    if not ENTRY.is_file():
        raise SystemExit(f"entry point not found: {ENTRY}")
    if not ICON.is_file():
        raise SystemExit(f"icon not found: {ICON} (run packaging/make_icon.ps1)")
    check_licence_pins()
    check_licence_texts()
    check_pyinstaller_notice()
    manifest = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
    check_microsoft_manifest(manifest)
    check_bundle_readme_source()
    runtimes = load_runtimes()
    problems = release_problems()
    for problem in problems:
        print(f"RELEASE BLOCKED: {problem}")
    if problems and args.release:
        raise SystemExit("release blocked: remove the legal-review marker and TBD text, then build again")

    cmd = pyinstaller_command(metadata_distributions(manifest))
    print("running:", " ".join(cmd))
    env = pyinstaller_env()
    print("PyInstaller PATH starts with:", env.get("PATH", "").split(os.pathsep)[0])
    subprocess.run(cmd, cwd=REPO, check=True, env=env)
    if not (BUNDLE_DIR / f"{APP_NAME}.exe").is_file():
        raise SystemExit("PyInstaller did not produce the executable")
    removed = prune_forbidden(BUNDLE_DIR, manifest["qt"])
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    PRUNE_REPORT.write_text("\n".join(removed) + "\n", encoding="utf-8")
    print(f"pruned {len(removed)} forbidden or unlisted files (list: {PRUNE_REPORT})")
    removed_microsoft = remove_microsoft_runtime(BUNDLE_DIR)
    print(f"removed {len(removed_microsoft)} Microsoft runtime files: the bundle does not ship them")
    write_bundle_readme()
    copy_notices()
    copy_problems = write_third_party(manifest, runtimes)
    for problem in copy_problems:
        print(f"RELEASE BLOCKED: {problem}")
    if copy_problems and args.release:
        raise SystemExit("release blocked: a bundled runtime copy does not match native_runtimes.toml (fail closed)")
    print(f"bundle ready: {BUNDLE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
