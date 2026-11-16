#!/usr/bin/env python3
"""Verify the MeridianDesktop ONEDIR bundle against packaging/lgpl_manifest.toml.

Run after packaging/build.py. Exit 0 only when every check passes.

Fail closed: a check that cannot run (pefile or PyInstaller's archive reader
missing, a manifest table missing, an unreadable or unparsable file, a missing
input such as packaging/native_runtimes.toml, any other unexpected error) prints
a FAIL line and is counted. It is never a skip, and it never escapes as a
traceback.

Start-up guard (check_import_roots): the verifier refuses to start when a module
it imports, or a distribution whose licence it reads, resolves outside the pixi
environment (its site-packages and standard library, and its DLLs folder) or
outside this file's own directory. Modules are located by where they resolve,
never by configuration or environment variables. The verifier's own top-level
imports run before the guard, so a standard-library name shadowed from outside
the environment would already have run when the guard looks at it.

PYZ contents are scanned with PyInstaller's own ZlibArchiveReader. A bundle with
no PYZ archive, or a PYZ that cannot be read, fails with "PYZ contents not
scanned".

Secret shapes (item g) are matched on the raw bytes of every file, every zip
member, and the UTF-8, UTF-16 LE and UTF-16 BE decodings of both. Every string
constant in the PYZ code objects gets the same four shape rules.

Licence gate (run_license_gate, plus the PYZ owner check in run):
  (a) every *.dist-info in the bundle, at the root and in every subdirectory, is
      read from its own METADATA. License-Expression, License and the License ::
      classifiers must agree on one licence. That licence must be permissive, or
      LGPL-3.0-only for a name in lgpl_family. GPL, AGPL, MPL, unknown and missing
      licences fail, naming the distribution. When the metadata is missing or
      ambiguous, the distribution's LICENSE or COPYING files are read, and a GPL or
      AGPL signature in them fails. Every package directory needs a licence record,
      and every [bundled] distribution needs a dist-info in the bundle. A file that
      more than one distribution's RECORD claims passes only when every claimant is in
      lgpl_family and they all carry the same election line (D1). Otherwise it fails,
      and the message names the file and the claimants.
  (a2) every PYZ module (the code PyInstaller stores in the exe's PYZ archive) is
      owned by an installed distribution, the standard library (CPython, listed in
      [pyz_owners]), or the project's own package ([pyz_owners] first_party). Each
      owner is classified by the same licence rules as a dist-info in the bundle.
      A module with no owner fails, and the message names it. The one exception is a
      PyInstaller bootstrap module (module_patterns in a [pyinstaller_*] table, D3). It passes
      only while NOTICE.md names PyInstaller and THIRD_PARTY_LICENSES.txt carries its
      GPL-2.0 text. Counsel must confirm that exception before release.
  (b) every .dll, .pyd and .exe, and every file whose first two bytes are MZ, must
      be a pinned Qt or PySide binary, a file named in packaging/native_runtimes.toml
      with the sha256 declared for its own bundle path (D2), a .pyd owned (through
      RECORD) by allowed distributions, or the application executable. Every bundled
      copy of a runtime file needs its own path key. An LGPL-3.0-only runtime is
      accepted only for files under _internal/PySide6/ or _internal/shiboken6/ (D4).
  (c) every DLL that a native binary imports, delay imports included, must be
      bundled or on the Windows OS allowlist in [system_dlls]. A Microsoft runtime name
      from [microsoft_prerequisites] is a system prerequisite: it passes only while its
      file is absent from the bundle.
  (c2) no Microsoft runtime file is in the bundle, in any folder. The names and patterns
      are in [microsoft_prerequisites]: the Visual C++ runtime, ucrtbase.dll and the
      api-ms-win- stubs. Each file found is named.
  (d) THIRD_PARTY_LICENSES.txt has a "== name version (SPDX) ==" header for every
      distribution and runtime, and at least 200 characters of licence text under
      each header. LICENSES/ holds the LGPL, GPL and MSL texts, and NOTICE.md names
      every distribution and runtime.
  (e) EULA.txt and OFFER-SOURCE.md carry no legal-review marker, in any encoding.
      Counsel removes those markers after review, and any marker blocks release.
  (f) OFFER-SOURCE.md has an https URL for PySide6 and one for Qt, and every URL in
      it has a host on [source_offer].hosts.
  (g) no secret-named file or directory (.env*, *.env, meridian.toml, *.pem, *.key,
      .netrc) and no secret-shaped text in any file. Only file names and rule ids
      are reported, never the matched text.

Configuration comes only from the manifest and from packaging/native_runtimes.toml.
Nothing here reads the environment (h).

Known gaps, not closed by this file:
  - Standard-library modules are owned by CPython, and that owner is named in
    [pyz_owners]. The check does not read the CPython licence text from the
    interpreter. The native runtimes table lists the CPython runtime separately.
  - A module planted in this file's own directory resolves inside the allowed
    roots, as the start-up guard specifies.
  - A DLL loaded at run time with LoadLibrary is invisible to a static import check.
  - Distributions that own PYZ modules are checked for licence, but they are not yet
    required in THIRD_PARTY_LICENSES.txt or NOTICE.md. That requirement follows the
    [bundled] list, and the artifacts owner decides when those distributions join it.

Licence records come from the bundle's own dist-info (licence gate) and from the
installed distributions via importlib.metadata (environment check and PYZ owners).

Hashes: the first verified build writes sha256 values for every Qt6*.dll and
every Qt plugin into the [hashes] table of the manifest. Later builds must
match those values exactly. Writing happens only when every other check passes.
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import hashlib
import importlib.metadata
import importlib.util
import json
import posixpath
import re
import sys
import sysconfig
import unicodedata
import zipfile
from collections.abc import Callable, Iterable
from email.parser import Parser
from pathlib import Path
from urllib.parse import urlsplit

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None  # load_manifest turns this into a FAIL line

REPO = Path(__file__).resolve().parents[1]
VERIFIER_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = REPO / "packaging" / "lgpl_manifest.toml"
NATIVE_RUNTIMES = REPO / "packaging" / "native_runtimes.toml"
DEFAULT_DIST = REPO / "dist" / "MeridianDesktop"
DEFAULT_REPORT = REPO / "build" / "linkage_report.json"
# Extracted PYZ entries go here: repo build/, never under dist/.
ARCHIVE_SCRATCH = REPO / "build" / "archive-scan"
SOURCE_PACKAGE = REPO / "meridian_desktop"
APP_EXE = "MeridianDesktop.exe"
INTERNAL_DIR = "_internal"
# Top-level modules the verifier imports. A loaded module with one of these names, or a
# standard-library name, must resolve inside the import roots (check_import_roots).
VERIFIER_IMPORTS = frozenset(
    {
        "argparse", "collections", "csv", "email", "hashlib", "importlib", "json", "pathlib",
        "posixpath", "re", "sys", "sysconfig", "tomli", "tomllib", "unicodedata", "urllib",
        "zipfile", "pefile", "PyInstaller",
    }
)
# Directories at the bundle root that are not package or dist-info directories.
ROOT_EXEMPT_DIRS = frozenset({"LICENSES"})

FORBIDDEN_PATH_PARTS = {".git", ".venv", "tests"}
SECRET_MARKERS = (b"sk_meridian_", b"BEARER_TOKEN=")
# Matched case-insensitively against the file bytes.
ABS_PATH_MARKERS = (b"c:\\users\\13144", b"c:/users/13144")
QT_DLL_RE = re.compile(r"^Qt6.+\.dll$", re.IGNORECASE)
LGPL_TOKEN = "LGPL-3.0-only"
HASHES_HEADER_RE = re.compile(r"^\s*\[hashes\]\s*(#.*)?$")
TABLE_HEADER_RE = re.compile(r"^\s*\[")
PYSIDE_DIRS = ("PySide6", "shiboken6")
NATIVE_SUFFIXES = (".dll", ".pyd", ".exe")
MODULE_SUFFIXES = (".py", ".pyc", ".pyd", ".so")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
HEADER_LINE_RE = re.compile(r"^== .+ ==$")
MIN_LICENCE_TEXT_CHARS = 200
# Secret shapes, matched against bytes. Only the rule id is reported.
SECRET_TEXT_RULES = (
    ("aws-access-key-id", re.compile(rb"AKIA[0-9A-Z]{16}")),
    ("meridian-token", re.compile(rb"sk_(?:live|test|meridian)_[A-Za-z0-9]{16,}")),
    ("pem-private-key", re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    (
        "credential-assignment",
        re.compile(rb"(?i)(?:api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+=]{20,}"),
    ),
)
# Byte-order marks, and the encodings text is decoded with (UTF-8, UTF-16 LE, UTF-16 BE).
BOM_ENCODINGS = ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))
TEXT_ENCODINGS = ("utf-8", "utf-16-le", "utf-16-be")
# Legal-review markers. Counsel removes them after review. Any one of them blocks release.
LEGAL_REVIEW_FILES = ("EULA.txt", "OFFER-SOURCE.md")
LEGAL_REVIEW_MARKERS = ("DRAFT - NOT FOR RELEASE", "TBD", "placeholder")
# Dash, space and invisible characters folded before a marker is matched.
DASH_CHARS = frozenset({chr(c) for c in range(0x2010, 0x2016)} | {"\u2212", "\ufe58", "\ufe63", "\uff0d"})
SPACE_CHARS = frozenset({chr(c) for c in range(0x2000, 0x200B)} | {"\u00a0", "\u1680", "\u202f", "\u205f", "\u3000"})
ZERO_WIDTH_CHARS = frozenset({"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u00ad", "\u200e", "\u200f"})
LICENCE_FILES = ("LICENSES/LGPL-3.0.txt", "LICENSES/GPL-3.0.txt", "LICENSES/MSL-1.0.txt")
LICENCE_NAME_PREFIXES = ("LICENSE", "LICENCE", "COPYING")
# Copyleft licence names, matched against raw licence metadata with no alias.
COPYLEFT_RE = re.compile(r"(?i)\bA?GPL\b|\bLGPL\b|\bMPL\b|GNU (?:AFFERO|GENERAL|LESSER)|MOZILLA PUBLIC")
# A GPL or AGPL signature in licence file text.
GPL_TEXT_RE = re.compile(r"(?i)\bA?GPL\b|GNU\s+(?:AFFERO\s+|GENERAL\s+PUBLIC\s+)")
URL_RE = re.compile(r"https?://[^\s<>\"'`)\]]+", re.IGNORECASE)


# ---------------------------------------------------------------- helpers


def _cannot_run(what: str, exc: BaseException) -> str:
    return f"{what} could not run: {exc.__class__.__name__}: {exc} (fail closed)"


def _run_check(fn: Callable, *args) -> list[str]:
    """Run one check. An unexpected error becomes a FAIL line, never a traceback."""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - this is the fail-closed boundary
        return [_cannot_run(fn.__name__, exc)]


def _run_check_pair(fn: Callable, *args) -> tuple[list[str], dict]:
    """Same as _run_check for checks that return (problems, report)."""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001
        return [_cannot_run(fn.__name__, exc)], {}


def load_manifest(path: Path) -> dict:
    if tomllib is None:
        raise ValueError("neither tomllib nor tomli is installed")
    with path.open("rb") as fh:
        return tomllib.load(fh)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rel_posix(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def normalize_dist_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _one_line(value) -> str:
    return " ".join(str(value or "").split())


def _secret_named(name: str) -> bool:
    """File and directory names that must never ship: .env, .env_temp, *.env, meridian.toml, *.pem, *.key, .netrc."""
    lower = name.lower()
    return (
        lower.startswith(".env")
        or lower.endswith(".env")
        or lower == "meridian.toml"
        or lower.endswith((".pem", ".key"))
        or lower == ".netrc"
    )


def _under_roots(path: Path, roots: Iterable[Path]) -> bool:
    return any(path.is_relative_to(root) for root in roots)


# ---------------------------------------------------------------- start-up guard


def import_roots() -> list[Path]:
    """Directories a verifier module or distribution may resolve from.

    The pixi environment's site-packages and standard library (from sysconfig), its
    DLLs folder, and this file's own directory. Nothing is read from the environment.
    """
    paths = sysconfig.get_paths()
    candidates = [
        paths["purelib"],
        paths["platlib"],
        paths["stdlib"],
        paths["platstdlib"],
        Path(sys.prefix) / "DLLs",
        VERIFIER_DIR,
    ]
    return [Path(c).resolve() for c in candidates]


def _loaded_verifier_modules() -> dict[str, list[str]]:
    """Loaded modules the verifier imports (by top-level name), with the file or folders each resolves to."""
    stdlib = sys.stdlib_module_names
    loaded: dict[str, list[str]] = {}
    for name, module in list(sys.modules.items()):
        if module is None:
            continue
        top = name.partition(".")[0]
        if top not in VERIFIER_IMPORTS and top not in stdlib:
            continue
        location = getattr(module, "__file__", None)
        if location:
            loaded[name] = [str(location)]
            continue
        folders = [str(p) for p in (getattr(module, "__path__", None) or [])]
        if folders:  # a namespace package: every folder must resolve inside the roots
            loaded[name] = folders
    return loaded


def find_import_root_problems(module_locations: dict[str, str | Iterable[str]], roots: list[Path]) -> list[str]:
    """One problem per module location that resolves outside the import roots."""
    problems: list[str] = []
    for name in sorted(module_locations):
        value = module_locations[name]
        locations = [value] if isinstance(value, str) else list(value)
        for location in locations:
            resolved = Path(location).resolve()
            if not _under_roots(resolved, roots):
                problems.append(
                    f"module {name} resolves outside the pixi environment and the verifier directory "
                    f"(fail closed): {resolved}"
                )
    return problems


def _top_level_spec_problems(roots: list[Path]) -> list[str]:
    """Locate pefile and PyInstaller without importing them, and check where they resolve."""
    problems: list[str] = []
    for name in ("pefile", "PyInstaller"):
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            continue  # not installed: the checks that need it fail closed
        if spec is None:
            continue
        locations: list[str] = []
        if spec.origin and spec.origin not in ("built-in", "frozen"):
            locations.append(spec.origin)
        locations += list(spec.submodule_search_locations or [])
        for location in locations:
            resolved = Path(location).resolve()
            if not _under_roots(resolved, roots):
                problems.append(
                    f"module {name} resolves outside the pixi environment and the verifier directory "
                    f"(fail closed): {resolved}"
                )
    return problems


def find_distribution_root_problems(dists: Iterable, roots: list[Path]) -> list[str]:
    """One problem per installed distribution whose dist-info is read from outside the import roots."""
    problems: list[str] = []
    for dist in dists:
        name = _one_line(dist.metadata.get("Name")) or "(unnamed)"
        location = Path(dist.locate_file("")).resolve()
        if not _under_roots(location, roots):
            problems.append(
                f"distribution {name} is read from outside the pixi environment and the verifier directory "
                f"(fail closed): {location}"
            )
    return problems


def check_import_roots() -> list[str]:
    """Refuse to start when a module or distribution the verifier reads resolves outside the import roots."""
    roots = import_roots()
    problems = _top_level_spec_problems(roots)
    problems += find_import_root_problems(_loaded_verifier_modules(), roots)
    problems += find_distribution_root_problems(importlib.metadata.distributions(), roots)
    return problems


# ---------------------------------------------------------------- checks


def check_forbidden_paths(root: Path) -> list[str]:
    """No .git, .venv, tests/ or secret-named file or directory (see _secret_named) in the bundle."""
    problems = []
    for path in sorted(root.rglob("*")):
        rel = rel_posix(root, path)
        hit = set(path.relative_to(root).parts) & FORBIDDEN_PATH_PARTS
        if hit:
            problems.append(f"forbidden path component {sorted(hit)}: {rel}")
        if _secret_named(path.name):
            kind = "directory" if path.is_dir() else "file"
            problems.append(f"forbidden {kind}: {rel}")
    return problems


def check_source_leak(root: Path, source_package: Path = SOURCE_PACKAGE) -> list[str]:
    """No plain .py from meridian_desktop (by path or by content) in the bundle."""
    problems = []
    source_hashes = {
        sha256_file(p): rel_posix(source_package, p)
        for p in source_package.rglob("*.py")
    }
    for path in root.rglob("*.py"):
        rel = rel_posix(root, path)
        if "meridian_desktop" in rel:
            problems.append(f"meridian_desktop python source in bundle: {rel}")
        elif sha256_file(path) in source_hashes:
            problems.append(f"copy of meridian_desktop source in bundle: {rel}")
    for path in root.rglob("meridian_desktop"):
        problems.append(f"meridian_desktop package directory in bundle: {rel_posix(root, path)}")
    return problems


def _scan_blob(label: str, data: bytes, markers, case_insensitive: bool, problems: list[str]) -> None:
    haystack = data.lower() if case_insensitive else data
    for marker in markers:
        if marker in haystack:
            problems.append(f"{label}: contains marker {marker.decode('ascii', 'replace')!r}")


def scan_bundle_bytes(root: Path, markers, case_insensitive: bool) -> list[str]:
    """Scan every file, and every member of zip archives, for byte markers.

    Compressed content (PYZ, zip members that fail to open) is reported, not
    skipped. See scan_pyz_files for PYZ contents.
    """
    problems: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = rel_posix(root, path)
        _scan_blob(rel, path.read_bytes(), markers, case_insensitive, problems)
        if path.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(path) as archive:
                    for member in archive.namelist():
                        _scan_blob(f"{rel}!{member}", archive.read(member), markers, case_insensitive, problems)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{rel}: zip archive not scanned ({exc.__class__.__name__}: {exc}) (fail closed)")
    return problems


def _text_variants(data: bytes):
    """Yield the text of `data` decoded as UTF-8, UTF-16 LE and UTF-16 BE, in that order.

    A byte-order mark, when present, selects the encoding tried first and is stripped.
    Undecodable bytes become replacement characters, so every variant always exists.
    """
    first = None
    payload = data
    for bom, encoding in BOM_ENCODINGS:
        if data.startswith(bom):
            first, payload = encoding, data[len(bom):]
            break
    order = list(TEXT_ENCODINGS)
    if first is not None:
        order.remove(first)
        order.insert(0, first)
    for encoding in order:
        yield payload.decode(encoding, errors="replace")


def _shape_candidates(data: bytes):
    yield data
    for text in _text_variants(data):
        yield text.encode("utf-8", "replace")


def _shape_rules_in(data: bytes) -> list[str]:
    """Rule ids of the secret shapes found in the raw bytes or in any text decoding of them."""
    found: list[str] = []
    for candidate in _shape_candidates(data):
        for rule, pattern in SECRET_TEXT_RULES:
            if rule not in found and pattern.search(candidate):
                found.append(rule)
        if len(found) == len(SECRET_TEXT_RULES):
            break
    return found


def _zip_member_shape_problems(path: Path, rel: str) -> list[str]:
    problems: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            for member in archive.namelist():
                for rule in _shape_rules_in(archive.read(member)):
                    problems.append(f"secret-shaped text in {rel}!{member}: rule {rule} (matched text not shown)")
    except Exception:  # noqa: BLE001 - scan_bundle_bytes reports an unreadable archive
        return problems
    return problems


def _secret_shape_problems(root: Path) -> list[str]:
    """Secret-shaped text in every file and every zip member. Reports the file and the rule, never the text."""
    problems: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = rel_posix(root, path)
        for rule in _shape_rules_in(path.read_bytes()):
            problems.append(f"secret-shaped text in {rel}: rule {rule} (matched text not shown)")
        if path.suffix.lower() == ".zip":
            problems += _zip_member_shape_problems(path, rel)
    return problems


def check_secrets(root: Path) -> list[str]:
    problems = [
        p.replace("contains marker", "secret-shaped string, contains marker")
        for p in scan_bundle_bytes(root, SECRET_MARKERS, case_insensitive=False)
    ]
    problems += _secret_shape_problems(root)
    return problems


def check_absolute_paths(root: Path) -> list[str]:
    return [
        p.replace("contains marker", "absolute developer path, contains marker")
        for p in scan_bundle_bytes(root, ABS_PATH_MARKERS, case_insensitive=True)
    ]


def check_qt_modules(root: Path, manifest: dict) -> tuple[list[str], dict]:
    """Every Qt6*.dll and every PySide6 .pyd maps to an allowed Qt module."""
    qt = manifest["qt"]
    allowed = set(qt["allowed_dlls"])
    forbidden = set(qt["forbidden_dlls"])
    allowed_plugin_dirs = set(qt["allowed_plugin_dirs"])
    allowed_plugin_files = set(qt.get("allowed_plugin_files", []))
    allowed_non_qt_pyd = set(qt.get("allowed_non_qt_pyd", []))
    problems: list[str] = []
    inventory: dict[str, list[str]] = {"qt_dlls": [], "pyside_pyd": [], "plugins": []}

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = rel_posix(root, path)
        parts = path.relative_to(root).parts
        name = path.name

        if QT_DLL_RE.match(name):
            stem = name[:-4]
            inventory["qt_dlls"].append(rel)
            if stem in forbidden:
                problems.append(f"forbidden Qt module DLL: {rel}")
            elif stem not in allowed:
                problems.append(f"Qt DLL not on allowed list (fail closed): {rel}")

        if "PySide6" in parts and path.suffix.lower() == ".pyd":
            inventory["pyside_pyd"].append(rel)
            stem = path.stem
            if stem.startswith("Qt"):
                dll = "Qt6" + stem[2:]
                if dll in forbidden:
                    problems.append(f"forbidden Qt module extension: {rel} needs {dll}")
                elif dll not in allowed:
                    problems.append(f"PySide6 extension for a Qt module not allowed: {rel} needs {dll}")
            elif stem not in allowed_non_qt_pyd:
                problems.append(f"non-Qt PySide6 extension not on allowed list: {rel}")

        if "PySide6" in parts and "plugins" in parts:
            idx = parts.index("plugins")
            if len(parts) > idx + 2:
                category = parts[idx + 1]
                inventory["plugins"].append(rel)
                if category not in allowed_plugin_dirs:
                    problems.append(f"Qt plugin category not allowed: {rel}")
                elif "/".join(parts[idx + 1 :]) not in allowed_plugin_files:
                    problems.append(f"Qt plugin file not on allowed list: {rel}")

    if not any(p.endswith("platforms/qwindows.dll") for p in inventory["plugins"]):
        problems.append("Qt platform plugin platforms/qwindows.dll missing")
    if not any(Path(p).name == "Qt6Core.dll" for p in inventory["qt_dlls"]):
        problems.append("Qt6Core.dll missing")
    return problems, inventory


# ---------------------------------------------------------------- licence records


def licence_sources(meta) -> list[tuple[str, str]]:
    """Every licence statement in a distribution's metadata, as (field, raw value) pairs.

    Reads License-Expression, License (unless UNKNOWN) and each License :: classifier.
    The sources must agree; classify_licence decides that.
    """
    sources: list[tuple[str, str]] = []
    expression = _one_line(meta.get("License-Expression"))
    if expression:
        sources.append(("License-Expression", expression))
    licence = _one_line(meta.get("License"))
    if licence and licence.upper() != "UNKNOWN":
        sources.append(("License", licence))
    for entry in meta.get_all("Classifier") or []:
        if entry.strip().startswith("License ::"):
            value = _one_line(entry.split("::")[-1])
            if value:
                sources.append(("Classifier", value))
    return sources


def _is_licence_file_name(name: str) -> bool:
    upper = name.upper()
    return upper.startswith(LICENCE_NAME_PREFIXES) and not upper.endswith((".PY", ".PYC", ".PYD", ".DLL", ".SO"))


def _licence_text_problems(label: str, texts: Iterable[tuple[str, str]]) -> list[str]:
    return [
        f"GPL or AGPL signature in licence file (GPL and AGPL are not allowed): {label} ({name})"
        for name, text in texts
        if GPL_TEXT_RE.search(text)
    ]


def classify_licence(
    label: str,
    key: str,
    sources: list[tuple[str, str]],
    licence_texts: Callable[[], list[tuple[str, str]]],
    lic: dict,
) -> tuple[str | None, list[str]]:
    """Return (spdx, problems) for one distribution. spdx is None when the licence fails.

    sources come from licence_sources. They must all resolve to one SPDX id through the
    manifest's aliases. licence_texts is called only when the metadata is missing or
    ambiguous. It returns (file name, text) pairs for the distribution's LICENSE and
    COPYING files, and a GPL or AGPL signature in any of them fails the distribution.
    """
    aliases = lic["aliases"]
    family = {normalize_dist_name(n) for n in lic["lgpl_family"]}
    allowed = set(lic["allowed_spdx"])
    if not sources:
        return None, [f"distribution has no licence (fail closed): {label}"] + _licence_text_problems(
            label, licence_texts()
        )
    resolved = {raw: aliases[raw] for _field, raw in sources if raw in aliases}
    unresolved = [(field, raw) for field, raw in sources if raw not in aliases]
    ids = set(resolved.values())
    raw_text = "; ".join(f"{field}: {raw}" for field, raw in sources)

    problems: list[str] = []
    if len(ids) > 1 or unresolved:
        problems += _licence_text_problems(label, licence_texts())
    if len(ids) > 1:
        problems.append(f"licence sources disagree (fail closed): {label} ({raw_text})")
    copyleft = [raw for _field, raw in unresolved if COPYLEFT_RE.search(raw)]
    if copyleft:
        problems.append(
            f"copyleft licence in bundle (GPL, AGPL and MPL are not allowed): {label} ({'; '.join(copyleft)})"
        )
    elif unresolved:
        problems.append(f"licence not recognised (fail closed): {label} ({raw_text})")
    if problems:
        return None, problems

    spdx = next(iter(ids))
    if key in family:
        if spdx == LGPL_TOKEN:
            return LGPL_TOKEN, []
        return None, [f"LGPL family distribution does not elect {LGPL_TOKEN}: {label} ({raw_text})"]
    if spdx == LGPL_TOKEN:
        return None, [f"{LGPL_TOKEN} is allowed only for the Qt family, not for {label}"]
    if COPYLEFT_RE.search(spdx):
        return None, [f"copyleft licence in bundle (GPL, AGPL and MPL are not allowed): {label} ({spdx})"]
    if spdx not in allowed:
        return None, [f"licence not on allowed list: {label} ({spdx})"]
    return spdx, []


def _inventory_licence(sources: list[tuple[str, str]], aliases: dict[str, str]) -> str:
    """One licence string for the environment inventory. Disagreeing sources are joined, so check_licenses fails them."""
    raws = list(dict.fromkeys(raw for _field, raw in sources))
    if not raws:
        return ""
    if len(raws) == 1:
        return raws[0]
    resolved = {aliases.get(raw) for raw in raws}
    if None not in resolved and len(resolved) == 1:
        return raws[0]
    return " ; ".join(raws)


def run_license_inventory(aliases: dict[str, str] | None = None) -> list[dict]:
    """Licence inventory of the installed distributions, standard library only.

    Each row has Name, Version and License. License comes from licence_sources. Sources
    that disagree are joined into one string that check_licenses rejects. An empty
    License means the distribution declared no licence.
    """
    aliases = aliases or {}
    rows: list[dict] = []
    for dist in importlib.metadata.distributions():
        meta = dist.metadata
        name = _one_line(meta.get("Name"))
        if not name:
            continue
        rows.append(
            {
                "Name": name,
                "Version": _one_line(meta.get("Version")),
                "License": _inventory_licence(licence_sources(meta), aliases),
            }
        )
    return rows


def _licence_problems(manifest: dict) -> list[str]:
    return check_licenses(run_license_inventory(manifest["licenses"]["aliases"]), manifest)


def check_licenses(records: list[dict], manifest: dict) -> list[str]:
    """Every bundled distribution must carry an allowed licence.

    records: inventory rows from run_license_inventory(), each with Name, Version and License.
    """
    lic = manifest["licenses"]
    bundled = {normalize_dist_name(n) for n in manifest["bundled"]["distributions"]}
    family = {normalize_dist_name(n) for n in lic["lgpl_family"]}
    allowed = set(lic["allowed_spdx"])
    aliases = lic["aliases"]
    by_name = {normalize_dist_name(r["Name"]): r for r in records}
    problems: list[str] = []

    for name in sorted(bundled):
        row = by_name.get(name)
        if row is None:
            problems.append(f"bundled distribution has no licence record (fail closed): {name}")
            continue
        raw = (row.get("License") or "").strip()
        if not raw or raw.upper() in {"UNKNOWN", "NONE"}:
            problems.append(f"bundled distribution has unknown licence: {name}")
            continue
        if name in family:
            options = [t.strip() for t in re.split(r"\s+OR\s+", raw)]
            if LGPL_TOKEN in options or aliases.get(raw) == LGPL_TOKEN:
                continue
            problems.append(f"LGPL family distribution does not elect {LGPL_TOKEN}: {name} ({raw})")
            continue
        spdx = aliases.get(raw)
        if spdx is None:
            problems.append(f"bundled distribution licence not recognised (fail closed): {name} ({raw})")
        elif spdx.upper().startswith("GPL"):
            problems.append(f"GPL-only licence in bundle: {name} ({raw})")
        elif spdx not in allowed:
            problems.append(f"licence not on allowed list: {name} ({spdx})")
    return problems


def check_notices(root: Path, manifest: dict) -> list[str]:
    qt_version = manifest["pyside6"]["qt_version"]
    problems: list[str] = []

    def need(rel: str, markers: list[str]) -> None:
        path = root / rel
        if not path.is_file():
            problems.append(f"notice file missing: {rel}")
            return
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in markers:
            if marker not in text:
                problems.append(f"{rel} lacks required marker {marker!r}")

    lgpl = root / "LICENSES" / "LGPL-3.0.txt"
    if not lgpl.is_file() or lgpl.stat().st_size == 0:
        problems.append("LICENSES/LGPL-3.0.txt missing or empty")
    else:
        if "GNU LESSER GENERAL PUBLIC LICENSE" not in lgpl.read_text(encoding="utf-8", errors="replace"):
            problems.append("LICENSES/LGPL-3.0.txt lacks the LGPL title")
    need("NOTICE.md", ["PySide6", "shiboken6", qt_version, LGPL_TOKEN])
    need("OFFER-SOURCE.md", ["PySide6", qt_version, "https://"])
    # The DRAFT marker is not required here. It must be absent at release, and the
    # legal-review check below blocks the release while it is present.
    need("EULA.txt", ["reverse engineer", "modif", LGPL_TOKEN])
    return problems


def _bundle_licence_texts(root: Path, dist_dir: Path) -> list[tuple[str, str]]:
    texts: list[tuple[str, str]] = []
    for path in sorted(dist_dir.rglob("*")):
        if path.is_file() and _is_licence_file_name(path.name):
            texts.append((rel_posix(root, path), path.read_text(encoding="utf-8", errors="replace")))
    return texts


def _installed_licence_texts(dist) -> list[tuple[str, str]]:
    texts: list[tuple[str, str]] = []
    for file in dist.files or []:
        rel = str(file).replace("\\", "/")
        if not _is_licence_file_name(rel.rsplit("/", 1)[-1]):
            continue
        location = Path(dist.locate_file(file))
        if location.is_file():
            texts.append((rel, location.read_text(encoding="utf-8", errors="replace")))
    return texts


def _ancestor_dirs(rel: str) -> set[str]:
    parts = rel.split("/")[:-1]
    return {"/".join(parts[:i]) for i in range(1, len(parts) + 1)}


def _record_paths(root: Path, dist_dir: Path) -> tuple[list[str], str]:
    """Bundle-relative paths listed in a dist-info RECORD, and a problem string if RECORD is missing."""
    record = dist_dir / "RECORD"
    if not record.is_file():
        return [], "RECORD missing, its files cannot be attributed (fail closed)"
    base = dist_dir.parent.relative_to(root).as_posix()
    rels: list[str] = []
    with record.open(encoding="utf-8", errors="replace", newline="") as fh:
        for row in csv.reader(fh):
            if not row or not row[0].strip():
                continue
            rel = posixpath.normpath(posixpath.join(base, row[0].strip()))
            if rel == "." or rel == ".." or rel.startswith("../"):
                continue  # outside the bundle (console scripts, for example)
            rels.append(rel)
    return rels, ""


def _has_python_code(path: Path) -> bool:
    return any(
        child.is_file() and (child.name == "__init__.py" or child.suffix.lower() in (".py", ".pyc", ".pyd"))
        for child in path.iterdir()
    )


def _unclaimed_directory_problems(root: Path, internal: Path, claimed_dirs: set[str]) -> list[str]:
    """Package directories, anywhere in the bundle, that no dist-info RECORD claims.

    A directory directly under the bundle root or under _internal is always checked. Any
    other directory is checked when it holds Python code.
    """
    problems: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_dir():
            continue
        if any(part.endswith(".dist-info") for part in path.relative_to(root).parts):
            continue
        rel = rel_posix(root, path)
        if rel in claimed_dirs:
            continue
        if path.parent == root and path.name in ROOT_EXEMPT_DIRS:
            continue
        if path.parent in (root, internal) or _has_python_code(path):
            problems.append(f"package directory has no licence record in the bundle (fail closed): {rel}")
    return problems


def _election_line(sources: list[tuple[str, str]]) -> str:
    """The License and License-Expression statements of one distribution, joined.

    This is the line that carries the Qt election. Classifiers are left out.
    """
    return " ; ".join(sorted({raw for field, raw in sources if field in ("License-Expression", "License")}))


def _shared_claim_problem(
    rel: str,
    claimants: list[str],
    family: set[str],
    elections: dict[str, str],
) -> str | None:
    """A file claimed by several distributions passes only for LGPL family members with one election line (D1)."""
    lines = {elections.get(key) for key in claimants}
    if all(key in family for key in claimants) and None not in lines and len(lines) == 1:
        return None
    return (
        "file claimed by more than one distribution, and the claimants are not all LGPL family members "
        f"with one election line (fail closed): {rel} ({', '.join(sorted(claimants))})"
    )


def inventory_bundle(root: Path, manifest: dict) -> tuple[list[str], dict]:
    """Read every *.dist-info in the bundle and check its licence.

    The dist-info folders are found at the bundle root and in every subdirectory.
    Returns (problems, inventory). inventory holds the distributions that resolved to an
    SPDX id, the distributions that own each RECORD path (a tuple of keys, one key unless
    the file is shared), and the distributions that passed.
    """
    internal = root / INTERNAL_DIR
    if not internal.is_dir():
        return [f"{INTERNAL_DIR} directory missing: no licence records (fail closed)"], {}
    lic = manifest["licenses"]
    family = {normalize_dist_name(n) for n in lic["lgpl_family"]}
    problems: list[str] = []
    distributions: list[dict] = []
    claims: dict[str, list[str]] = {}
    elections: dict[str, str] = {}
    allowed_keys: set[str] = set()
    present_keys: set[str] = set()
    claimed_dirs: set[str] = set()

    dist_dirs = sorted(p for p in root.rglob("*.dist-info") if p.is_dir())
    if not dist_dirs:
        problems.append("no *.dist-info directory in the bundle (fail closed)")
    for dist_dir in dist_dirs:
        dist_rel = rel_posix(root, dist_dir)
        meta_file = dist_dir / "METADATA"
        if not meta_file.is_file():
            problems.append(f"{dist_rel}: METADATA missing, licence cannot be read (fail closed)")
            continue
        msg = Parser().parsestr(meta_file.read_text(encoding="utf-8", errors="replace"), headersonly=True)
        name = _one_line(msg.get("Name"))
        version = _one_line(msg.get("Version"))
        if not name:
            problems.append(f"{dist_rel}: distribution has no Name (fail closed)")
            continue
        key = normalize_dist_name(name)
        present_keys.add(key)
        label = f"{name} {version}".strip()
        sources = licence_sources(msg)
        spdx, dist_problems = classify_licence(
            label,
            key,
            sources,
            lambda d=dist_dir: _bundle_licence_texts(root, d),
            lic,
        )
        if spdx == LGPL_TOKEN:
            elections[key] = _election_line(sources)
        rels, record_problem = _record_paths(root, dist_dir)
        if record_problem:
            dist_problems.append(f"{label}: {record_problem}")
        problems += dist_problems
        if spdx is not None:
            distributions.append({"name": name, "version": version, "spdx": spdx, "key": key})
        if not dist_problems:
            allowed_keys.add(key)
        for rel in rels:
            claimants = claims.setdefault(rel, [])
            if key not in claimants:
                claimants.append(key)
            claimed_dirs.update(_ancestor_dirs(rel))

    for bundled in manifest["bundled"]["distributions"]:
        if normalize_dist_name(bundled) not in present_keys:
            problems.append(f"bundled distribution has no dist-info in the bundle (fail closed): {bundled}")
    owners: dict[str, tuple[str, ...]] = {}
    for rel, claimants in sorted(claims.items()):
        owners[rel] = tuple(sorted(claimants))
        if len(claimants) > 1:
            shared = _shared_claim_problem(rel, claimants, family, elections)
            if shared is not None:
                problems.append(shared)
    problems += _unclaimed_directory_problems(root, internal, claimed_dirs)
    inventory = {"distributions": distributions, "owners": owners, "allowed": allowed_keys}
    return problems, inventory


# ---------------------------------------------------------------- PYZ owners


def _module_of_record_path(record_path: str) -> str | None:
    """Dotted module name of a RECORD path, or None for a file that is not a module."""
    rel = record_path.replace("\\", "/")
    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    if not rel.endswith(MODULE_SUFFIXES):
        return None
    if any(part.endswith((".dist-info", ".egg-info", ".data")) or part == "__pycache__" for part in parts[:-1]):
        return None
    leaf = parts[-1]
    if leaf in ("__init__.py", "__init__.pyc"):
        parts = parts[:-1]
    else:
        parts[-1] = leaf.split(".")[0]
    return ".".join(parts) if parts and all(parts) else None


def build_owner_index(dists: Iterable) -> dict[str, dict[str, dict]]:
    """Map each dotted module name to the installed distributions whose RECORD lists it."""
    index: dict[str, dict[str, dict]] = {}
    for dist in dists:
        meta = dist.metadata
        name = _one_line(meta.get("Name"))
        if not name:
            continue
        key = normalize_dist_name(name)
        owner = {
            "name": name,
            "version": _one_line(meta.get("Version")),
            "key": key,
            "meta": meta,
            "dist": dist,
        }
        for file in dist.files or []:
            dotted = _module_of_record_path(str(file))
            if dotted:
                index.setdefault(dotted, {})[key] = owner
    return index


def _owners_of(module: str, index: dict[str, dict[str, dict]]) -> list[dict]:
    """Owners of a PYZ module: the exact module, or for a namespace package every module beneath it."""
    if module in index:
        return list(index[module].values())
    prefix = module + "."
    found: dict[str, dict] = {}
    for dotted, owners in index.items():
        if dotted.startswith(prefix):
            found.update(owners)
    return list(found.values())


def _listed(items: list[str], limit: int = 5) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


# The only licences a PyInstaller bootstrap table may carry (lgpl_manifest.toml). Apache-2.0 is
# for the run-time hooks and fake modules; the Bootloader Exception is for the bootloader.
PYINSTALLER_ALLOWED_LICENCES = {"Apache-2.0", "GPL-2.0-or-later WITH Bootloader-exception"}


def _pyinstaller_module(module: str, cfg: dict) -> bool:
    """True when a PYZ module's top-level name matches a PyInstaller bootstrap pattern ([pyinstaller], D3)."""
    top = module.partition(".")[0]
    return any(fnmatch.fnmatchcase(top, pattern) for pattern in cfg.get("module_patterns", []))


def _pyinstaller_exception_reasons(root: Path | None, cfg: dict) -> list[str]:
    """Why the PyInstaller exception cannot be confirmed. An empty list means it can (D3).

    The exception holds only while NOTICE.md names PyInstaller and THIRD_PARTY_LICENSES.txt
    carries the GPL-2.0 text listed in copying_markers. Without the bundle root it cannot hold.
    """
    if root is None:
        return ["the bundle root is needed to read NOTICE.md and THIRD_PARTY_LICENSES.txt"]
    if not cfg.get("module_patterns") or not cfg.get("copying_markers"):
        return ["a [pyinstaller_*] table in lgpl_manifest.toml is incomplete"]
    name = str(cfg.get("notice_name", "PyInstaller"))
    reasons: list[str] = []
    notice = root / "NOTICE.md"
    if not notice.is_file() or name.lower() not in notice.read_text(encoding="utf-8", errors="replace").lower():
        reasons.append(f"NOTICE.md does not name {name}")
    third = root / "THIRD_PARTY_LICENSES.txt"
    text = third.read_text(encoding="utf-8", errors="replace") if third.is_file() else ""
    if not third.is_file() or any(marker not in text for marker in cfg["copying_markers"]):
        reasons.append(f"THIRD_PARTY_LICENSES.txt does not carry the {name} COPYING text")
    return reasons


def pyz_owner_problems(
    names: Iterable[str],
    manifest: dict,
    dists: Iterable | None = None,
    root: Path | None = None,
) -> list[str]:
    """Every PYZ module must have an owner whose licence passes (see the module docstring, item a2).

    names: the PYZ table of contents. dists: installed distributions (default: the build
    interpreter's importlib.metadata). root: the bundle root, read for the PyInstaller
    exception (D3). A module with no owner fails, and the message names it.
    """
    lic = manifest["licenses"]
    owner_cfg = manifest["pyz_owners"]
    first_party = set(owner_cfg["first_party"])
    stdlib_owner = owner_cfg["stdlib_owner"]
    stdlib_spdx = owner_cfg["stdlib_spdx"]
    pyi_cfgs = [manifest[t] for t in ("pyinstaller_runtime_hooks", "pyinstaller_bootloader") if t in manifest]
    problems: list[str] = []
    if stdlib_spdx not in set(lic["allowed_spdx"]):
        problems.append(f"{stdlib_owner} standard library licence {stdlib_spdx} is not on the allowed list (fail closed)")

    index = build_owner_index(importlib.metadata.distributions() if dists is None else dists)
    owners_by_key: dict[str, dict] = {}
    modules_by_key: dict[str, set[str]] = {}
    pyinstaller_modules: list[str] = []
    for module in sorted(set(names)):
        owners = _owners_of(module, index)
        if owners:
            for owner in owners:
                owners_by_key[owner["key"]] = owner
                modules_by_key.setdefault(owner["key"], set()).add(module)
            continue
        top = module.partition(".")[0]
        if top in first_party or top in sys.stdlib_module_names:
            continue  # first-party code, or the standard library (owned by the CPython runtime)
        matched = next((cfg for cfg in pyi_cfgs if _pyinstaller_module(module, cfg)), None)
        if matched is not None:
            pyinstaller_modules.append((module, matched))  # PyInstaller's own bootstrap code, checked below
            continue
        problems.append(f"PYZ module {module} has no owning distribution (fail closed)")

    for module, cfg in pyinstaller_modules:
        reasons = _pyinstaller_exception_reasons(root, cfg)
        if cfg.get("licence") not in PYINSTALLER_ALLOWED_LICENCES:
            reasons.append(f"the table licence {cfg.get('licence')!r} is not a PyInstaller licence this gate accepts")
        if reasons:
            problems.append(
                f"PYZ module {module} has no owning distribution, and its PyInstaller licence "
                f"({cfg.get('licence')}) is not confirmed: {'; '.join(reasons)} (fail closed)"
            )

    for key in sorted(owners_by_key):
        owner = owners_by_key[key]
        label = f"{owner['name']} {owner['version']}".strip()
        _spdx, owner_problems = classify_licence(
            label,
            key,
            licence_sources(owner["meta"]),
            lambda o=owner: _installed_licence_texts(o["dist"]),
            lic,
        )
        for problem in owner_problems:
            problems.append(f"{problem} (owns PYZ modules: {_listed(sorted(modules_by_key[key]))})")
    return problems


# ---------------------------------------------------------------- PYZ contents

# PYZ contents: every module's code object is scanned.


def _scan_code(code, label: str, problems: list[str]) -> None:
    """Scan one code object and every code object nested in it."""
    filename = code.co_filename.lower()
    for marker in ABS_PATH_MARKERS:
        if marker.decode() in filename:
            problems.append(f"{label}: code object filename holds absolute developer path")
    for const in code.co_consts:
        _scan_constant(const, label, problems)


def _scan_constant(const, label: str, problems: list[str]) -> None:
    if hasattr(const, "co_filename"):
        _scan_code(const, label, problems)
    elif isinstance(const, (tuple, frozenset)):
        for item in const:
            _scan_constant(item, label, problems)
    elif isinstance(const, (str, bytes)):
        data = const if isinstance(const, bytes) else const.encode("utf-8", "replace")
        lowered = data.lower()
        for marker in SECRET_MARKERS:
            if marker in data:
                problems.append(
                    f"{label}: secret-shaped string in code constants, contains marker {marker.decode()!r}"
                )
        for rule, pattern in SECRET_TEXT_RULES:
            if pattern.search(data):
                problems.append(
                    f"{label}: secret-shaped string in code constants, rule {rule} (matched text not shown)"
                )
        for marker in ABS_PATH_MARKERS:
            if marker in lowered:
                problems.append(
                    f"{label}: absolute developer path in code constants, contains marker {marker.decode()!r}"
                )


def scan_pyz_files(pyz_files: list[Path]) -> list[str]:
    """Scan every module of each PYZ archive with PyInstaller's ZlibArchiveReader.

    A PYZ that cannot be opened, or an entry that cannot be extracted, is reported
    as "PYZ contents not scanned". It is never a silent pass.
    """
    try:
        from PyInstaller.loader.pyimod01_archive import ZlibArchiveReader
    except Exception as exc:  # noqa: BLE001
        return [
            "PYZ contents not scanned: PyInstaller archive reader unavailable "
            f"({exc.__class__.__name__}: {exc}) (fail closed)"
        ]

    problems: list[str] = []
    for pyz in pyz_files:
        try:
            reader = ZlibArchiveReader(str(pyz))
            names = sorted(reader.toc)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{pyz.name}: PYZ contents not scanned ({exc.__class__.__name__}: {exc}) (fail closed)")
            continue
        for name in names:
            label = f"{pyz.name}:{name}"
            try:
                code = reader.extract(name)
            except (Exception, SystemExit) as exc:  # the reader exits if the file vanished
                problems.append(f"{label}: PYZ entry not scanned ({exc.__class__.__name__}: {exc}) (fail closed)")
                continue
            if code is None:
                continue  # PEP 420 namespace package: no code object
            if not hasattr(code, "co_filename"):
                problems.append(f"{label}: PYZ entry is not a code object, not scanned (fail closed)")
                continue
            module_problems: list[str] = []
            _scan_code(code, label, module_problems)
            problems.extend(dict.fromkeys(module_problems))
    return problems


def pyz_module_names(pyz_files: list[Path]) -> tuple[list[str], list[str]]:
    """The module table of contents of each PYZ archive: (problems, names)."""
    try:
        from PyInstaller.loader.pyimod01_archive import ZlibArchiveReader
    except Exception as exc:  # noqa: BLE001
        return [
            "PYZ module table not read: PyInstaller archive reader unavailable "
            f"({exc.__class__.__name__}: {exc}) (fail closed)"
        ], []
    problems: list[str] = []
    names: set[str] = set()
    for pyz in pyz_files:
        try:
            names.update(ZlibArchiveReader(str(pyz)).toc)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{pyz.name}: PYZ module table not read ({exc.__class__.__name__}: {exc}) (fail closed)")
    return problems, sorted(names)


def read_pyz_archive(exe: Path, scratch: Path = ARCHIVE_SCRATCH) -> tuple[list[str], list[Path]]:
    """Collect the bundle's PYZ archives: (problems, PYZ paths).

    PYZ files beside the exe are used in place. PYZ entries inside the exe's CArchive
    are extracted to `scratch` (build/archive-scan at the repo root, never under dist/).
    A bundle with no PYZ at all is a problem.
    """
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except Exception as exc:  # noqa: BLE001
        return [
            "PYZ contents not scanned: PyInstaller archive reader unavailable "
            f"({exc.__class__.__name__}: {exc}) (fail closed)"
        ], []
    try:
        archive = CArchiveReader(str(exe))
    except Exception as exc:  # noqa: BLE001
        return [
            f"{exe.name}: PYZ contents not scanned, exe is not a readable PyInstaller archive "
            f"({exc.__class__.__name__}: {exc}) (fail closed)"
        ], []

    problems: list[str] = []
    pyz_files = sorted(exe.parent.rglob("*.pyz"))
    for entry in sorted(archive.toc):
        if not entry.endswith(".pyz"):
            continue
        try:
            blob = archive.extract(entry)
            if isinstance(blob, tuple):
                blob = blob[1]
            scratch.mkdir(parents=True, exist_ok=True)
            target = scratch / entry
            target.write_bytes(blob)
            pyz_files.append(target)
        except Exception as exc:  # noqa: BLE001
            problems.append(
                f"{exe.name}!{entry}: PYZ contents not scanned ({exc.__class__.__name__}: {exc}) (fail closed)"
            )
    if not pyz_files:
        problems.append(f"{exe.name}: no PYZ archive found: PYZ contents not scanned (fail closed)")
    return problems, pyz_files


def check_pyinstaller_archive(exe: Path, scratch: Path = ARCHIVE_SCRATCH) -> list[str]:
    """Collect the bundle's PYZ archives and scan them. See read_pyz_archive."""
    problems, pyz_files = read_pyz_archive(exe, scratch)
    if not pyz_files:
        return problems
    return problems + scan_pyz_files(pyz_files)


def check_pyz_owners(
    exe: Path,
    manifest: dict,
    scratch: Path = ARCHIVE_SCRATCH,
    dists: Iterable | None = None,
) -> list[str]:
    """The PYZ owner section: the module table of the exe's PYZ, each module owned and licensed."""
    problems, pyz_files = read_pyz_archive(exe, scratch)
    if not pyz_files:
        return problems
    table_problems, names = pyz_module_names(pyz_files)
    return problems + table_problems + pyz_owner_problems(names, manifest, dists, root=exe.parent)


# PE linkage


def _pe_imports(pefile, path: Path) -> list[str]:
    """DLL names from the import table and the delay-load import table."""
    pe = pefile.PE(str(path), fast_load=True)
    try:
        pe.parse_data_directories(
            directories=[
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"],
            ]
        )
        entries = list(getattr(pe, "DIRECTORY_ENTRY_IMPORT", []))
        entries += list(getattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT", []))
        return [entry.dll.decode("ascii", "replace") for entry in entries]
    finally:
        pe.close()


def _pe_import_names(path: Path) -> list[str]:
    import pefile  # noqa: WPS433 - a missing pefile fails the whole check, see check_native_imports

    return _pe_imports(pefile, path)


def _file_version(pefile, path: Path) -> str:
    pe = pefile.PE(str(path), fast_load=True)
    try:
        # fast_load skips resources, so the version resource must be parsed explicitly.
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
        info = pe.VS_FIXEDFILEINFO[0]
        return ".".join(
            str(x)
            for x in (
                info.FileVersionMS >> 16,
                info.FileVersionMS & 0xFFFF,
                info.FileVersionLS >> 16,
            )
        )
    finally:
        pe.close()


def check_pe_linkage(root: Path, manifest: dict, exe: Path) -> tuple[list[str], dict]:
    """Parse PE import tables with pefile. Every Qt DLL a PySide6 extension, Qt DLL
    or plugin imports must be an allowed Qt DLL that exists as a separate file.
    """
    try:
        import pefile  # noqa: WPS433
    except ImportError:
        return ["pefile not installed: PE import tables cannot be verified (fail closed)"], {}

    problems: list[str] = []
    report: dict[str, dict] = {}
    allowed = set(manifest["qt"]["allowed_dlls"])
    present = {p.name.lower(): p for p in root.rglob("*.dll")}

    targets = [p for p in root.rglob("*") if p.is_file() and (
        p.suffix.lower() == ".pyd" and "PySide6" in p.relative_to(root).parts
        or QT_DLL_RE.match(p.name)
        or ("PySide6" in p.relative_to(root).parts and "plugins" in p.relative_to(root).parts and p.suffix.lower() == ".dll")
    )]
    for path in sorted(targets):
        rel = rel_posix(root, path)
        try:
            imports = _pe_imports(pefile, path)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{rel}: PE parse failed ({exc.__class__.__name__}: {exc}) (fail closed)")
            continue
        qt_imports = sorted({d for d in imports if QT_DLL_RE.match(d)})
        report[rel] = {"imports": sorted(set(imports)), "qt_imports": qt_imports}
        for dll in qt_imports:
            stem = dll[:-4]
            if stem not in allowed:
                problems.append(f"{rel} imports Qt DLL not on allowed list: {dll}")
            if dll.lower() not in present:
                problems.append(f"{rel} imports {dll} which is not a separate file in the bundle")

    report["_exe"] = {"path": APP_EXE}
    try:
        _pe_imports(pefile, exe)  # the exe must parse as a PE image
    except Exception as exc:  # noqa: BLE001
        problems.append(f"{APP_EXE}: PE parse failed ({exc.__class__.__name__}: {exc}) (fail closed)")

    qt_core = present.get("qt6core.dll")
    if qt_core is not None:
        try:
            version = _file_version(pefile, qt_core)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"Qt6Core.dll: version unreadable ({exc.__class__.__name__}: {exc}) (fail closed)")
        else:
            report["_qt_core_version"] = {"file_version": version}
            if not version.startswith(manifest["pyside6"]["qt_version"]):
                problems.append(
                    f"Qt6Core.dll version {version} does not match manifest qt_version "
                    f"{manifest['pyside6']['qt_version']}"
                )
    return problems, report


def check_exe_embeds_no_pe(exe: Path) -> list[str]:
    """The exe must not embed another PE image (Qt DLLs stay separate files)."""
    data = exe.read_bytes()
    problems: list[str] = []
    start = 1
    while True:
        idx = data.find(b"MZ", start)
        if idx < 0:
            break
        start = idx + 1
        if idx + 0x40 > len(data):
            continue
        e_lfanew = int.from_bytes(data[idx + 0x3C : idx + 0x40], "little")
        pe_off = idx + e_lfanew
        if 0 < e_lfanew < 0x1000 and pe_off + 4 <= len(data) and data[pe_off : pe_off + 4] == b"PE\0\0":
            problems.append(f"exe embeds a PE image at offset {idx}")
    return problems


# ---------------------------------------------------------------- native binaries


def _digest_path(key: str) -> str | None:
    """A runtime sha256 key as a bundle-relative posix path, lower-cased; None when it is not one."""
    text = key.replace("\\", "/")
    if ":" in text or text.startswith("/") or any(part in ("", ".", "..") for part in text.split("/")):
        return None
    return text.lower()


def _is_pyside_path(path: str) -> bool:
    """True when a lower-cased bundle path sits under _internal/PySide6/ or _internal/shiboken6/ (D4)."""
    return path.startswith(tuple(f"{INTERNAL_DIR.lower()}/{name.lower()}/" for name in PYSIDE_DIRS))


def load_native_runtimes(path: Path, manifest: dict) -> tuple[list[str], list[dict]]:
    """Read packaging/native_runtimes.toml. A missing or malformed file is a FAIL.

    Each [[runtime]] `files` list names files. Its `sha256` table is keyed by path relative
    to the bundle root, for example "_internal/python3.dll" (D2). Every listed name
    needs at least one path key. Every path key needs a 64-digit lower-case hex digest and
    a listed file name. A key that is not a bundle-relative path fails, and so does a
    duplicate. An LGPL-3.0-only runtime is accepted only when every path is under
    _internal/PySide6/ or _internal/shiboken6/ (D4).
    """
    if not path.is_file():
        return [f"{path.name} missing: native runtimes cannot be inventoried (fail closed)"], []
    data = load_manifest(path)
    entries = data.get("runtime")
    if not isinstance(entries, list) or not entries:
        return ["native_runtimes.toml has no [[runtime]] entries (fail closed)"], []
    allowed = set(manifest["runtimes"]["allowed_spdx"])
    problems: list[str] = []
    runtimes: list[dict] = []
    for index, entry in enumerate(entries):
        label = str(entry.get("name") or f"runtime #{index + 1}")
        missing = [key for key in ("name", "version", "spdx", "files", "notice_url", "sha256") if key not in entry]
        if missing:
            problems.append(f"runtime {label} is missing {', '.join(missing)} (fail closed)")
            continue
        files = entry["files"]
        if not isinstance(files, list) or not files or not all(isinstance(f, str) and f for f in files):
            problems.append(f"runtime {label} has a files list that is empty or not a list of names (fail closed)")
            continue
        digests = entry["sha256"]
        if not isinstance(digests, dict):
            problems.append(f"runtime {label} sha256 is not a table of bundle paths to digests (fail closed)")
            continue
        declared: dict[str, str] = {}  # lower-cased bundle path -> digest
        shown: dict[str, str] = {}  # lower-cased bundle path -> the key as written
        file_problems: list[str] = []
        for key, digest in digests.items():
            text = str(key)
            lower = _digest_path(text)
            if lower is None:
                file_problems.append(f"runtime {label} sha256 key {text} is not a bundle-relative path (fail closed)")
                continue
            if lower in declared:
                file_problems.append(f"runtime {label} has more than one sha256 for {text} (fail closed)")
                continue
            declared[lower] = str(digest).lower()
            shown[lower] = text
        listed = {f.lower() for f in files}
        for lower in sorted(declared):
            base = lower.rsplit("/", 1)[-1]
            if base not in listed:
                file_problems.append(
                    f"runtime {label} has a sha256 for {shown[lower]}, which is not a file in its files list (fail closed)"
                )
            elif not SHA256_RE.fullmatch(declared[lower]):
                file_problems.append(
                    f"runtime {label} file {shown[lower]} has a sha256 that is not 64 hex digits (fail closed)"
                )
        for file_name in files:
            if not any(lower.rsplit("/", 1)[-1] == file_name.lower() for lower in declared):
                file_problems.append(f"runtime {label} file {file_name} has no sha256 (fail closed)")
        if file_problems:
            problems += file_problems
            continue
        spdx = str(entry["spdx"])
        if spdx == LGPL_TOKEN:
            outside = [shown[lower] for lower in sorted(declared) if not _is_pyside_path(lower)]
            if outside:
                problems += [
                    f"runtime {label} is {LGPL_TOKEN}, accepted only under {INTERNAL_DIR}/PySide6/ or "
                    f"{INTERNAL_DIR}/shiboken6/, and {text} is outside them (fail closed)"
                    for text in outside
                ]
                continue
        elif spdx not in allowed:
            problems.append(f"runtime {label} licence not on allowed list: {spdx}")
            continue
        if not str(entry["notice_url"]).startswith("https://"):
            problems.append(f"runtime {label} notice_url is not https (fail closed)")
            continue
        runtimes.append(
            {
                "name": str(entry["name"]),
                "version": str(entry["version"]),
                "spdx": spdx,
                "files": list(files),
                "sha256": declared,
                "notice_url": str(entry["notice_url"]),
            }
        )
    return problems, runtimes


def _is_mz_header(path: Path) -> bool:
    """True when the file starts with the MZ header. An unreadable file counts, so it is reported."""
    try:
        with path.open("rb") as fh:
            return fh.read(2) == b"MZ"
    except OSError:
        return True


def _native_files(root: Path) -> list[Path]:
    """Every native binary: a file with a native suffix, or any file that starts with an MZ header."""
    return [
        p
        for p in sorted(root.rglob("*"))
        if p.is_file() and (p.suffix.lower() in NATIVE_SUFFIXES or _is_mz_header(p))
    ]


def check_native_binaries(root: Path, manifest: dict, inventory: dict, runtimes: list[dict]) -> list[str]:
    """Every native binary must be inventoried (see the module docstring, item b).

    A runtime file is compared with the digest for its own bundle path (D2), so two copies
    with one name in different folders are checked separately. A runtime file with no digest
    for its own path fails.
    """
    if not inventory:
        return ["distribution inventory unavailable: native binaries cannot be attributed (fail closed)"]
    pins = manifest.get("hashes", {})
    forbidden_other = {name.lower() for name in manifest["qt"]["forbidden_other_dlls"]}
    runtime_paths: dict[str, str] = {}  # lower-cased bundle path -> digest
    runtime_names: set[str] = set()
    for runtime in runtimes:
        runtime_names.update(name.lower() for name in runtime["files"])
        runtime_paths.update(runtime["sha256"])
    bundled = [p for p in root.rglob("*") if p.is_file()]
    bundled_paths = {rel_posix(root, p).lower() for p in bundled}
    bundled_names = {p.name.lower() for p in bundled}
    owners = inventory["owners"]
    allowed = inventory["allowed"]
    problems: list[str] = []
    for digest_path in sorted(runtime_paths):
        if digest_path not in bundled_paths:
            problems.append(f"runtime sha256 path is not in the bundle (fail closed): {digest_path}")
    for runtime in runtimes:
        for name in runtime["files"]:
            if name.lower() not in bundled_names:
                problems.append(f"runtime file listed in native_runtimes.toml is not in the bundle (fail closed): {name}")
    for path in _native_files(root):
        rel = rel_posix(root, path)
        parts = path.relative_to(root).parts
        suffix = path.suffix.lower()
        if suffix == ".exe" and len(parts) == 1 and path.name == APP_EXE:
            continue  # the application itself; its imports are checked by check_native_imports
        if path.stem.lower() in forbidden_other:
            problems.append(f"forbidden native binary: {rel}")
            continue
        is_qt_or_pyside = bool(QT_DLL_RE.match(path.name)) or any(p in PYSIDE_DIRS for p in parts)
        if rel in pins and is_qt_or_pyside:
            if sha256_file(path) != pins[rel]:
                problems.append(f"sha256 differs from the pinned value: {rel}")
            continue
        if rel.lower() in runtime_paths:
            if sha256_file(path) != runtime_paths[rel.lower()]:
                problems.append(f"sha256 differs from the value declared in native_runtimes.toml: {rel}")
            continue
        if path.name.lower() in runtime_names:
            problems.append(f"runtime file has no sha256 for its own path in native_runtimes.toml (fail closed): {rel}")
            continue
        if suffix == ".pyd":
            owner = owners.get(rel)
            if owner is None:
                problems.append(
                    "native module is neither listed in native_runtimes.toml nor owned by a dist-info RECORD "
                    f"(fail closed): {rel}"
                )
            elif not all(key in allowed for key in owner):
                problems.append(
                    f"native module from a distribution that is not allowed: {rel} ({', '.join(owner)})"
                )
            continue
        if is_qt_or_pyside:
            problems.append(
                f"Qt or PySide binary has no pinned sha256 and is not in native_runtimes.toml (fail closed): {rel}"
            )
        else:
            problems.append(f"native binary not inventoried (fail closed): {rel}")
    return problems


def load_microsoft_prerequisites(manifest: dict) -> tuple[set[str], list[str]]:
    """(names, patterns) from [microsoft_prerequisites], lower-cased. A missing table or key raises, and the check fails."""
    table = manifest["microsoft_prerequisites"]
    names = {str(name).lower() for name in table["names"]}
    patterns = [str(pattern).lower() for pattern in table["patterns"]]
    if not names or not patterns:
        raise ValueError("[microsoft_prerequisites] must list names and patterns")
    return names, patterns


def is_microsoft_runtime_name(name: str, names: set[str], patterns: list[str]) -> bool:
    """True when a file name is a Microsoft runtime file: an exact listed name, or a listed pattern."""
    lower = name.lower()
    return lower in names or any(fnmatch.fnmatchcase(lower, pattern) for pattern in patterns)


def check_native_imports(root: Path, manifest: dict, read_imports: Callable[[Path], list[str]] | None = None) -> list[str]:
    """Every DLL a shipped native binary imports must be bundled or on the OS allowlist.

    A Microsoft runtime name is a system prerequisite (see [microsoft_prerequisites]). It is
    accepted only while its file is absent from the bundle. A bundled copy is reported here too.
    """
    if read_imports is None:
        import pefile  # noqa: F401 - a missing pefile fails the whole check, not each file
    reader = read_imports or _pe_import_names
    system = manifest["system_dlls"]
    allowed_names = {name.lower() for name in system["names"]}
    prefixes = tuple(prefix.lower() for prefix in system["prefixes"])
    ms_names, ms_patterns = load_microsoft_prerequisites(manifest)
    bundled = {p.name.lower() for p in root.rglob("*.dll") if p.is_file()}
    problems: list[str] = []
    for path in _native_files(root):
        rel = rel_posix(root, path)
        try:
            imports = {name.lower() for name in reader(path)}
        except Exception as exc:  # noqa: BLE001 - fail closed per file
            problems.append(f"{rel}: PE import table could not be read ({exc.__class__.__name__}: {exc}) (fail closed)")
            continue
        for dll in sorted(imports):
            if is_microsoft_runtime_name(dll, ms_names, ms_patterns):
                if dll in bundled:
                    problems.append(f"{rel} imports {dll}, a Microsoft runtime file that must not be bundled")
                continue  # a system prerequisite the user installs, absent from the bundle
            if dll in bundled or dll in allowed_names or dll.startswith(prefixes):
                continue
            problems.append(f"{rel} imports {dll}, which is neither bundled nor on the Windows OS allowlist")
    return problems


def check_microsoft_runtime_absent(root: Path, manifest: dict) -> list[str]:
    """No Microsoft runtime file or folder in any folder of the bundle (item c2). Each one found is named."""
    names, patterns = load_microsoft_prerequisites(manifest)
    problems: list[str] = []
    for path in sorted(root.rglob("*")):
        if is_microsoft_runtime_name(path.name, names, patterns):
            kind = "folder" if path.is_dir() else "file"
            problems.append(
                f"Microsoft runtime {kind} must not be bundled; users install the Visual C++ redistributable "
                f"instead: {rel_posix(root, path)}"
            )
    return problems


# ---------------------------------------------------------------- artifacts


def _section_text_lengths(text: str) -> dict[str, int]:
    """Characters of text under each "== name version (SPDX) ==" header, up to the next header."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if HEADER_LINE_RE.match(stripped):
            current = stripped
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return {header: len("\n".join(body).strip()) for header, body in sections.items()}


def check_required_artifacts(root: Path, manifest: dict, inventory: dict, runtimes: list[dict]) -> list[str]:
    """THIRD_PARTY_LICENSES.txt headers and text, licence texts and NOTICE.md names (item d)."""
    problems: list[str] = []
    for rel in LICENCE_FILES:
        path = root / rel
        if not path.is_file() or path.stat().st_size == 0:
            problems.append(f"{rel} missing or empty (fail closed)")
    gpl = root / "LICENSES" / "GPL-3.0.txt"
    if gpl.is_file() and "GNU GENERAL PUBLIC LICENSE" not in gpl.read_text(encoding="utf-8", errors="replace"):
        problems.append("LICENSES/GPL-3.0.txt lacks the GPL title (fail closed)")

    distributions = inventory.get("distributions", []) if inventory else None
    if distributions is None:
        problems.append("distribution inventory unavailable: artifacts cannot be checked (fail closed)")
        distributions = []
    headers = [
        f"== {d['name']} {d['version']} ({d['spdx']}) ==" for d in distributions
    ] + [f"== {r['name']} {r['version']} ({r['spdx']}) ==" for r in runtimes]
    names = [d["name"] for d in distributions] + [r["name"] for r in runtimes]

    third = root / "THIRD_PARTY_LICENSES.txt"
    if not third.is_file() or third.stat().st_size == 0:
        problems.append("THIRD_PARTY_LICENSES.txt missing or empty (fail closed)")
    else:
        lengths = _section_text_lengths(third.read_text(encoding="utf-8", errors="replace"))
        for header in headers:
            if header not in lengths:
                problems.append(f"THIRD_PARTY_LICENSES.txt has no header {header!r}")
            elif lengths[header] < MIN_LICENCE_TEXT_CHARS:
                problems.append(
                    f"THIRD_PARTY_LICENSES.txt section {header!r} has {lengths[header]} characters of licence text; "
                    f"at least {MIN_LICENCE_TEXT_CHARS} are required (fail closed)"
                )

    notice = root / "NOTICE.md"
    if not notice.is_file():
        problems.append("NOTICE.md missing (fail closed)")
    else:
        text = notice.read_text(encoding="utf-8", errors="replace").lower()
        for name in names:
            if name.lower() not in text:
                problems.append(f"NOTICE.md does not name {name} (fail closed)")
    return problems


def _fold_for_markers(text: str) -> str:
    """Fold text for marker matching: NFKC, every dash to '-', every space-like character to a space,
    zero-width characters removed, case folded, and all whitespace removed (so spacing around a
    dash cannot hide a marker)."""
    text = unicodedata.normalize("NFKC", text)
    out: list[str] = []
    for ch in text:
        if ch in ZERO_WIDTH_CHARS:
            continue
        if ch in DASH_CHARS:
            out.append("-")
        elif ch in SPACE_CHARS:
            out.append(" ")
        else:
            out.append(ch)
    return "".join("".join(out).casefold().split())


def check_legal_markers(root: Path) -> list[str]:
    """EULA.txt and OFFER-SOURCE.md carry no legal-review marker, in UTF-8 or UTF-16 (item e)."""
    problems: list[str] = []
    keys = {marker: _fold_for_markers(marker) for marker in LEGAL_REVIEW_MARKERS}
    for name in LEGAL_REVIEW_FILES:
        path = root / name
        if not path.is_file():
            problems.append(f"{name} missing (fail closed)")
            continue
        found: set[str] = set()
        for text in _text_variants(path.read_bytes()):
            folded = _fold_for_markers(text)
            for marker, key in keys.items():
                if key in folded:
                    found.add(marker)
        for marker in LEGAL_REVIEW_MARKERS:
            if marker in found:
                problems.append(
                    f"{name} contains legal-review marker {marker!r}; counsel removes it after review, release is blocked"
                )
    return problems


def check_source_offer(root: Path, manifest: dict) -> list[str]:
    """OFFER-SOURCE.md: https URLs for PySide6 and Qt, every host allowlisted (item f)."""
    hosts = {host.lower() for host in manifest["source_offer"]["hosts"]}
    path = root / "OFFER-SOURCE.md"
    if not path.is_file():
        return ["OFFER-SOURCE.md missing (fail closed)"]
    text = path.read_text(encoding="utf-8", errors="replace")
    problems: list[str] = []
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:")
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme.lower() != "https":
            problems.append(f"OFFER-SOURCE.md has a URL that is not https: {host or '(no host)'}")
        if host not in hosts:
            problems.append(f"OFFER-SOURCE.md URL host is not on the source offer allowlist: {host or '(no host)'}")
    lines = text.splitlines()
    for component, pattern in (("PySide6", r"pyside6"), ("Qt", r"\bqt\b")):
        if not any(re.search(pattern, line, re.IGNORECASE) and "https://" in line.lower() for line in lines):
            problems.append(f"OFFER-SOURCE.md has no https URL for {component} (fail closed)")
    return problems


def run_license_gate(
    root: Path,
    manifest: dict,
    runtimes_path: Path | None = None,
    read_imports: Callable[[Path], list[str]] | None = None,
) -> list[tuple[str, list[str]]]:
    """Checks (a) to (g) of the licence gate. Returns (title, problems) for each section."""
    path = NATIVE_RUNTIMES if runtimes_path is None else runtimes_path
    sections: list[tuple[str, list[str]]] = []

    inv_problems, inventory = _run_check_pair(inventory_bundle, root, manifest)
    if not isinstance(inventory, dict):
        inventory = {}
    sections.append(("bundled distribution licences from dist-info", inv_problems))

    rt_problems, runtimes = _run_check_pair(load_native_runtimes, path, manifest)
    if not isinstance(runtimes, list):
        runtimes = []
    sections.append(("native runtimes listed", rt_problems))

    sections.append(("native binaries inventoried", _run_check(check_native_binaries, root, manifest, inventory, runtimes)))
    sections.append(
        ("native imports bundled or Windows OS DLLs", _run_check(check_native_imports, root, manifest, read_imports))
    )
    sections.append(("Microsoft runtime not bundled", _run_check(check_microsoft_runtime_absent, root, manifest)))
    sections.append(
        ("licence and notice artifacts present", _run_check(check_required_artifacts, root, manifest, inventory, runtimes))
    )
    sections.append(("legal-review markers absent", _run_check(check_legal_markers, root)))
    sections.append(("source offer complete and allowlisted", _run_check(check_source_offer, root, manifest)))
    return sections


# ---------------------------------------------------------------- hashes


def check_hashes(root: Path, manifest: dict) -> tuple[list[str], dict[str, str]]:
    """Return (problems, computed). The caller writes the table on a first pass."""
    targets = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        parts = path.relative_to(root).parts
        if QT_DLL_RE.match(path.name) or ("PySide6" in parts and "plugins" in parts):
            targets[rel_posix(root, path)] = sha256_file(path)

    expected = manifest.get("hashes", {})
    problems: list[str] = []
    if not expected:
        return ["hashes table empty: first verified build must write it"], targets
    for key in sorted(set(targets) - set(expected)):
        problems.append(f"file not in hashes table: {key}")
    for key in sorted(set(expected) - set(targets)):
        problems.append(f"hashed file missing from bundle: {key}")
    for key in sorted(set(targets) & set(expected)):
        if targets[key] != expected[key]:
            problems.append(f"sha256 mismatch: {key}")
    return problems, targets


def write_hashes(manifest_path: Path, hashes: dict[str, str]) -> None:
    """Replace the [hashes] table with `hashes`.

    [hashes] may sit on any line, including the first. A missing table is appended.
    Refuses to rewrite when another table follows [hashes], because that would
    delete it. [hashes] must therefore stay the last table in the manifest.
    """
    head: list[str] = []
    tail: list[str] = []
    found = False
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not found and HASHES_HEADER_RE.match(line):
            found = True
        elif found:
            tail.append(line)
        else:
            head.append(line)
    if any(TABLE_HEADER_RE.match(line) for line in tail):
        raise ValueError("[hashes] is followed by another table; refusing to rewrite the manifest")

    while head and not head[-1].strip():
        head.pop()
    body = head + [""] if head else []
    body += [
        "[hashes]",
        "# sha256 of every Qt6*.dll and Qt plugin, written by verify_bundle.py on the",
        "# first verified build. Later builds must match exactly.",
    ]
    body += [f'"{key}" = "{hashes[key]}"' for key in sorted(hashes)]
    manifest_path.write_text("\n".join(body) + "\n", encoding="utf-8", newline="\n")


# ---------------------------------------------------------------- driver


def run(
    dist: Path,
    manifest_path: Path,
    report_path: Path,
    write: bool,
    runtimes_path: Path | None = None,
    read_imports: Callable[[Path], list[str]] | None = None,
) -> int:
    import_problems = _run_check(check_import_roots)
    if import_problems:
        print("FAIL import roots: the verifier refuses to start")
        for problem in import_problems:
            print(f"     - {problem}")
        print(f"RESULT FAIL ({len(import_problems)} problems)")
        return 1

    try:
        manifest = load_manifest(manifest_path)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL manifest unreadable: {manifest_path} ({exc.__class__.__name__}: {exc}) (fail closed)")
        print("RESULT FAIL (1 problems)")
        return 1

    exe = dist / APP_EXE
    if not dist.is_dir() or not exe.is_file():
        print(f"FAIL bundle not found: {dist}")
        return 1

    sections: list[tuple[str, list[str]]] = [
        ("forbidden paths", _run_check(check_forbidden_paths, dist)),
        ("no meridian_desktop source", _run_check(check_source_leak, dist)),
        ("no secret-shaped strings", _run_check(check_secrets, dist)),
        ("no absolute developer paths", _run_check(check_absolute_paths, dist)),
        ("pyinstaller archive scan", _run_check(check_pyinstaller_archive, exe)),
        ("exe embeds no PE image", _run_check(check_exe_embeds_no_pe, exe)),
    ]

    report: dict = {}
    qt_problems, inventory = _run_check_pair(check_qt_modules, dist, manifest)
    report["inventory"] = inventory
    sections.append(("allowed Qt modules only", qt_problems))
    sections.append(("bundled licences allowed", _run_check(_licence_problems, manifest)))
    sections.append(("notices present", _run_check(check_notices, dist, manifest)))

    pe_problems, pe_report = _run_check_pair(check_pe_linkage, dist, manifest, exe)
    report["pe"] = pe_report
    sections.append(("PE import tables dynamic", pe_problems))

    sections.extend(run_license_gate(dist, manifest, runtimes_path, read_imports))
    sections.append(("PYZ modules owned and licensed", _run_check(check_pyz_owners, exe, manifest)))

    failures = 0
    for title, problems in sections:
        status = "PASS" if not problems else "FAIL"
        print(f"{status} {title}")
        for problem in problems:
            print(f"     - {problem}")
        failures += len(problems)

    # Hashes: write on the first verified build only, when nothing else failed.
    if failures == 0 and write and not manifest.get("hashes"):
        try:
            _ignored, computed = check_hashes(dist, {"hashes": {}})
            write_hashes(manifest_path, computed)
            print(f"WROTE {len(computed)} sha256 hashes into {manifest_path.name} (first verified build)")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL sha256 write: {_cannot_run('writing hashes', exc)}")
    else:
        hash_problems, _computed = _run_check_pair(check_hashes, dist, manifest)
        status = "PASS" if not hash_problems else "FAIL"
        print(f"{status} sha256 match manifest")
        for problem in hash_problems:
            print(f"     - {problem}")
        failures += len(hash_problems)

    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"linkage report: {report_path}")
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"FAIL linkage report not written: {_cannot_run('writing the report', exc)}")

    print("RESULT", "PASS" if failures == 0 else f"FAIL ({failures} problems)")
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the MeridianDesktop ONEDIR bundle.")
    parser.add_argument("--dist", type=Path, default=DEFAULT_DIST)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--no-write-hashes", action="store_true")
    args = parser.parse_args(argv)
    return run(args.dist, args.manifest, args.report, write=not args.no_write_hashes)


if __name__ == "__main__":
    raise SystemExit(main())
