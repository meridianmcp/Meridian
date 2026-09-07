#!/usr/bin/env python
"""Regenerate ``tools/meridian_fallbacks/capability_manifest.json``'s
per-module ``sha256``/``byte_size`` fields from the real tracked ``.py``
files on disk (sprint item 8c047a44, "DOCS-R2-E").

WHY THIS EXISTS: ``tools/meridian_fallbacks/tests/test_capability_parity.py
::TestManifestModuleByteParity::
test_every_manifest_module_hash_and_size_match_real_file`` already CATCHES
drift between this manifest and the real module bytes -- but nothing in the
repo ever PRODUCED those ``sha256``/``byte_size`` fields in the first place.
The manifest's own trailing ``"notes"`` field says outright: "Regenerate the
sha256/byte_size fields ... by hand ... after any edit ... so this manifest
never drifts" -- a hand process is exactly how the drift this item's
discovery phase found (``figure_slot_manifest.py`` edited in commit
``55cf5006`` without a manifest refresh: recorded ``sha256=fe69ff5a...``
``size=18516`` vs actual ``sha256=7ad25f22...`` ``size=19281``) happened, and
how it will happen again without a generator.

Usage::

    # Report drift only; never writes. Exit 0 if converged, 1 if drifted,
    # 2 on a structural error (a manifest module names a file that no
    # longer exists).
    pixi run python -m tools.meridian_fallbacks.generate_capability_manifest --check

    # Recompute sha256/byte_size for every drifted module and rewrite them
    # in place, SURGICALLY (regex-targeted per module, keyed on that
    # module's own "file" value) so every other byte of this hand-formatted
    # JSON file -- key order, single-line vs multi-line arrays, comments-
    # adjacent prose -- is left completely untouched. Exit 0 on success
    # (even when nothing needed to change), 2 on a structural error.
    pixi run python -m tools.meridian_fallbacks.generate_capability_manifest --write

Exactly one of ``--check`` / ``--write`` is required.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

_PKG_DIR = Path(__file__).resolve().parent
_MANIFEST_PATH = _PKG_DIR / "capability_manifest.json"


def _sha256_and_size(path: Path) -> "tuple[str, int]":
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def compute_module_drift(
    manifest: "dict[str, Any]", pkg_dir: Path = _PKG_DIR,
) -> "list[dict[str, Any]]":
    """Return one entry per module whose recorded ``sha256``/``byte_size``
    disagrees with the real file on disk right now::

        {"module", "file", "recorded_sha256", "recorded_byte_size",
         "actual_sha256", "actual_byte_size"}

    An empty list means fully converged (this is exactly what
    ``test_every_manifest_module_hash_and_size_match_real_file`` already
    asserts, ported here as a reusable function rather than an inline test
    body). Raises :class:`FileNotFoundError` when a manifest module names a
    file that does not exist on disk -- a manifest referencing a
    deleted/renamed module is a structural defect this generator refuses to
    paper over, not silently-skippable drift.
    """
    drift: "list[dict[str, Any]]" = []
    for mod_name, entry in manifest.get("modules", {}).items():
        path = pkg_dir / entry["file"]
        if not path.is_file():
            raise FileNotFoundError(
                f"manifest module {mod_name!r} names a file that does not "
                f"exist: {path}"
            )
        actual_sha256, actual_size = _sha256_and_size(path)
        if actual_sha256 != entry.get("sha256") or actual_size != entry.get("byte_size"):
            drift.append({
                "module": mod_name,
                "file": entry["file"],
                "recorded_sha256": entry.get("sha256"),
                "recorded_byte_size": entry.get("byte_size"),
                "actual_sha256": actual_sha256,
                "actual_byte_size": actual_size,
            })
    return drift


def _surgical_field_pattern(file_name: str) -> "re.Pattern[str]":
    """Match ``"file": "<file_name>", ... "sha256": "<64 hex>", ...
    "byte_size": <digits>`` exactly as this manifest lays each module entry
    out today (``file`` immediately followed by ``sha256`` then
    ``byte_size``, one per line) -- captures everything before the sha256
    value and everything between it and the byte_size value so a
    replacement can substitute ONLY the two value spans, byte-for-byte
    identical otherwise (whitespace, key order, trailing commas, and every
    surrounding field untouched).
    """
    return re.compile(
        r'("file":\s*"' + re.escape(file_name) + r'"\s*,\s*\n\s*"sha256":\s*")'
        r'[0-9a-f]{64}'
        r'("\s*,\s*\n\s*"byte_size":\s*)'
        r'\d+',
    )


def regenerate_manifest_text(
    manifest_text: str, manifest: "dict[str, Any]", pkg_dir: Path = _PKG_DIR,
) -> "tuple[str, list[dict[str, Any]]]":
    """Surgically rewrite *manifest_text* (the manifest file's raw source)
    so every drifted module's ``sha256``/``byte_size`` matches the real file
    on disk, changing nothing else -- no reformatting, no key reordering,
    no touching of modules that are already converged.

    Returns ``(new_text, drift)`` where *drift* is the same shape
    :func:`compute_module_drift` returns (the modules that were actually
    changed). *manifest_text* is returned unchanged (byte-for-byte) when
    *drift* is empty. Raises :class:`FileNotFoundError` under the same
    condition as :func:`compute_module_drift`.
    """
    drift = compute_module_drift(manifest, pkg_dir=pkg_dir)
    text = manifest_text
    for d in drift:
        pattern = _surgical_field_pattern(d["file"])
        new_text, count = pattern.subn(
            rf'\g<1>{d["actual_sha256"]}\g<2>{d["actual_byte_size"]}', text,
        )
        if count != 1:
            raise ValueError(
                f"expected exactly one file/sha256/byte_size block for "
                f"module {d['module']!r} (file={d['file']!r}) in the "
                f"manifest text, found {count} -- refusing to guess which "
                "one to rewrite. The manifest's layout may have changed; "
                "update _surgical_field_pattern to match."
            )
        text = new_text
    return text, drift


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--check", action="store_true",
        help="report drift and exit 1 if any is found; never writes",
    )
    group.add_argument(
        "--write", action="store_true",
        help="surgically recompute and rewrite drifted sha256/byte_size fields in place",
    )
    parser.add_argument(
        "--manifest-path", default=str(_MANIFEST_PATH),
        help="path to capability_manifest.json (default: the tracked package's own copy)",
    )
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest_path)
    pkg_dir = manifest_path.parent
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)

    if args.check:
        try:
            drift = compute_module_drift(manifest, pkg_dir=pkg_dir)
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        if not drift:
            print(f"OK: {manifest_path} matches every tracked module's real bytes.")
            return 0
        print(f"DRIFT DETECTED in {manifest_path}:", file=sys.stderr)
        for d in drift:
            print(
                f"  {d['module']}: recorded sha256={d['recorded_sha256']} "
                f"size={d['recorded_byte_size']} but actual "
                f"sha256={d['actual_sha256']} size={d['actual_byte_size']}",
                file=sys.stderr,
            )
        return 1

    # --write
    try:
        new_text, drift = regenerate_manifest_text(manifest_text, manifest, pkg_dir=pkg_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not drift:
        print(f"OK: {manifest_path} already matches every tracked module's real bytes; nothing to write.")
        return 0
    manifest_path.write_text(new_text, encoding="utf-8")
    print(f"Wrote {manifest_path} (regenerated sha256/byte_size for {len(drift)} module(s):")
    for d in drift:
        print(f"  {d['module']}: {d['file']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
