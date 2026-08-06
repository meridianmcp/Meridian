"""Local fallback output-provenance gate.

Sprint item d3374b0e (proposal 1abedabe-2f82-40e5-a320-3b32d550cc40).

Prior state / porting note
---------------------------
This item's brief asked to "port the proven ``_codex_output_provenance_gate``
behavior" into this module. Before writing anything, the whole repository was
searched (tracked files, git history/log across ALL branches, the untracked
``.codex/`` directory and its ``sprint_batch_items.json``, every ``workspace/*``
scratch directory, and any path containing ``temp_scripts``) for
``_codex_output_provenance_gate`` / ``output_provenance_gate`` / ``provenance_gate``
in any form. Nothing matched. The sibling package-skeleton item's own notes
(4ff6ff22) clarify why: the "existing thesis temp_scripts copies" this whole
``tools/meridian_fallbacks/`` package is meant to eventually reach parity with
live in a *different*, external project ("the thesis" -- see the
``workspace/*`` docx-audit scratch directories for other artifacts from that
same external engagement), not in this repository. There is therefore no
in-repo implementation to port. Per this item's own instruction ("Keep
temp_scripts source untouched"), no attempt is made to reach into that other
project either -- this module is a fresh, from-scratch implementation of the
behavior the acceptance criteria specify, deliberately kept at PARITY (same
statuses, same on-disk ledger formats/locations, same SHA-256 algorithm) with
this repository's own already-proven local provenance system,
``extensions/meridian-outputs/meridian_outputs/provenance_status.py``
(sprint item bd5b8d79, itself extended for hash/convergence-awareness by this
same sprint item -- see that module's docstring). That IS a proven, tested,
shipped local system in this repo, and is the closest legitimate thing to
"the proven behavior" this item's brief could be pointing at.

Why this is a SEPARATE, self-contained module (not a thin wrapper)
--------------------------------------------------------------------
``tools/meridian_fallbacks`` exists (per 4ff6ff22's own title) so an executor
still has a working provenance answer when the ``meridian-outputs`` MCP
extension/tunnel is unavailable -- exactly the "fallback_chain" contract
AGENTS.md's capability-manifest section (649e095f) describes: a fallback must
not itself depend on the thing it's a fallback FOR. Concretely, that means
this module:

  - Has NO import dependency on ``meridian_outputs``, ``duckdb``, or
    ``xxhash`` (all real, heavier dependencies the extension declares in its
    own ``pyproject.toml`` / this repo's ``pixi.toml``) -- pure standard
    library only (``hashlib``, ``json``, ``os``, ``dataclasses``, ``re``).
  - Has NO relative/package import of its own siblings either -- per this
    item's explicit instruction, the sibling package-skeleton item
    (4ff6ff22) is landing ``tools/meridian_fallbacks/__init__.py`` in a
    PARALLEL worktree; this file must be importable standalone (as a bare
    module, e.g. ``sys.path.insert(...); import output_provenance_gate``)
    today, before that skeleton exists, AND continue working unchanged once
    it's cherry-picked in ahead of this commit.
  - Reads the SAME on-disk ledger files, at the SAME location, in the SAME
    JSON shape the real extension already writes --
    ``<outputs_dir>/.meridian-outputs-cache/provenance_ledger.json``
    (``meridian_outputs.annotate.record_provenance``'s ledger) and
    ``<outputs_dir>/.meridian-outputs-cache/fingerprint_ledger.json``
    (``meridian_outputs.fingerprint.tag_output``'s ledger) -- via plain
    ``json.load``, not an import. This is what makes the fallback a genuine
    fallback rather than a second, drifting source of truth: same facts,
    read a different (dependency-light) way.
  - Computes its OWN local file-existence index (bounded directory walk) and
    its OWN canonical/archival classification, both using plain SHA-256
    (``hashlib``), because the extension's own equivalents
    (``outputs_local.OutputsFtsIndex`` / ``outputs_local.classify_canonical_
    archival``) are DuckDB- and xxhash-backed and therefore unavailable in
    exactly the "hosted/extension is down" scenario this module exists for.

Explicit statuses (``provenance_type``), ranked most- to least-authoritative
------------------------------------------------------------------------------
  1. ``STALE_BY_SCRIPT`` -- an exact provenance record exists, AND the output
     was also independently fingerprint-tagged (``fingerprint.tag_output``),
     AND that tag's generating-script content hash no longer matches the
     script's CURRENT on-disk hash. A stronger, more specific signal than
     generic output-content staleness: the script that made this output has
     since changed (bug fix or otherwise), so the output may reflect
     behavior that no longer exists, even though the output FILE itself
     looks perfectly valid and its own content never changed.
  2. ``EXACT`` -- an exact provenance-ledger record exists for this path and
     is not superseded by the ``STALE_BY_SCRIPT`` check above. Comes with a
     ``staleness`` block (existence + OUTPUT content-hash check, independent
     of the script-hash check above).
  3. ``DIRECTORY_FALLBACK`` -- no exact record, but a ``MERIDIAN_NOTES.md``
     file covers this path (nearest ancestor directory under ``outputs_dir``,
     read directly -- same filename convention ``outputs_local`` uses,
     reimplemented here without importing it).
  4. ``UNREGISTERED`` -- no exact record, no directory note, but THIS call's
     own bounded filesystem scan of ``outputs_dir`` found the path (it is a
     real file under the tree, just never had provenance recorded).
  5. ``UNKNOWN`` -- the scan did not find the path. See ``inconclusive``
     below before treating this as a confident answer.

``inconclusive`` -- an unconverged index is inconclusive, never proof of
missing provenance. This module's own scan (:func:`_scan_outputs_dir`) is
bounded by ``max_scan_files`` and can also hit directory-read errors; when
either happens the scan is ``truncated``/errored and therefore NOT
``converged``. A ``provenance_type`` of ``UNKNOWN`` (or ``UNREGISTERED``, in
principle, though it never actually needs the caveat -- discovering the path
IS a positive fact regardless of whether the rest of the tree finished
scanning) reached under a non-converged scan sets ``inconclusive=True``: the
correct read is "not found by this call, walk didn't finish, don't treat
this as confirmed absence," never "confirmed absent." A generation counter is
persisted to a small disposable ledger
(``<outputs_dir>/.meridian-outputs-cache/fallback_index_ledger.json``) purely
so repeated calls have SOME notion of "which scan pass" produced an answer;
losing that file (it is disposable, matching this item's "disposable ledger
fixtures" framing) just resets the counter to 1, never breaks correctness.

NO hosted/MCP call is made anywhere in this module -- fully local, matching
every other fallback in this package and the ``meridian_outputs`` extension
it mirrors.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "EXACT",
    "DIRECTORY_FALLBACK",
    "UNREGISTERED",
    "UNKNOWN",
    "STALE_BY_SCRIPT",
    "ScanState",
    "check_output_provenance",
    "main",
]

# ---------------------------------------------------------------------------
# Explicit statuses -- kept string-identical to
# extensions/meridian-outputs/meridian_outputs/provenance_status.py's own
# constants for genuine cross-tool parity (item d3374b0e / 50f224ec).
# ---------------------------------------------------------------------------
EXACT = "exact"
DIRECTORY_FALLBACK = "directory_fallback"
UNREGISTERED = "unregistered"
UNKNOWN = "unknown"
STALE_BY_SCRIPT = "stale_by_script"

_CACHE_DIRNAME = ".meridian-outputs-cache"
_PROVENANCE_LEDGER_FILENAME = "provenance_ledger.json"
_FINGERPRINT_LEDGER_FILENAME = "fingerprint_ledger.json"
_INDEX_LEDGER_FILENAME = "fallback_index_ledger.json"
_MERIDIAN_NOTES_FILENAME = "MERIDIAN_NOTES.md"

# Same naming heuristic as outputs_local.archival_candidate/_canonical_name
# (leading underscore, or an "_old"/"_old_N" suffix) -- reimplemented here
# without importing outputs_local (see module docstring).
_ARCHIVAL_SUFFIX_RE = re.compile(r"_old(?:_\d+)?$", re.IGNORECASE)

# Generous default: a full recursive walk of outputs_dir happens on every
# call (this module is synchronous/single-shot per call, not a persistent
# resumable index like outputs_local's), so this only needs to bound
# pathological trees, not steer normal operation. Tests override it with a
# small value to deterministically force truncation.
DEFAULT_MAX_SCAN_FILES = 200_000


# ---------------------------------------------------------------------------
# Path normalization (mirrors annotate._normalize_path / outputs_local's
# equivalent exactly -- same algorithm, so hashes/keys computed here compare
# equal to the real extension's own ledger keys).
# ---------------------------------------------------------------------------

def _normalize_path(path: Any) -> str:
    if not isinstance(path, str) or not path.strip():
        return ""
    s = path.strip()
    try:
        s = os.path.abspath(s)
    except (OSError, ValueError):
        pass
    return os.path.normcase(os.path.normpath(s)).replace("\\", "/")


def _sha256_file(path: str | None) -> str | None:
    """SHA-256 of ``path``'s current on-disk bytes. Never raises -- an
    unreadable/missing/None path yields ``None``, itself a meaningful signal
    to callers (matches ``fingerprint.script_content_hash``'s contract)."""
    if not path:
        return None
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Ledger readers -- plain JSON reads of the SAME files the real
# meridian_outputs extension writes (annotate.py / fingerprint.py), read
# independently so this module has no import dependency on that package.
# ---------------------------------------------------------------------------

def _cache_dir(outputs_dir: str) -> str:
    return os.path.join(outputs_dir, _CACHE_DIRNAME)


def _read_json_object(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_provenance_record(outputs_dir: str, path: str) -> dict[str, Any] | None:
    """Exact-match lookup into ``provenance_ledger.json``, keyed the SAME
    way ``annotate._write_ledger_entry`` keys it (normalized path)."""
    key = _normalize_path(path)
    if not key:
        return None
    ledger = _read_json_object(os.path.join(_cache_dir(outputs_dir), _PROVENANCE_LEDGER_FILENAME))
    rec = ledger.get(key)
    return dict(rec) if isinstance(rec, dict) else None


def _read_fingerprint_record(outputs_dir: str, path: str) -> dict[str, Any] | None:
    """Lookup into ``fingerprint_ledger.json``.

    ``fingerprint._write_ledger_entry`` keys this ledger by the RAW
    ``output_path`` string originally passed to ``tag_output`` -- unlike
    ``annotate``'s ledger, it is NOT run through a path normalizer. Try an
    exact-string match first (the common case: same process/CWD that tagged
    also queries), then fall back to a normalized scan of every entry so a
    differently-spelled-but-equivalent path (back-slashes, case, a relative
    vs. absolute form) is still found.
    """
    ledger = _read_json_object(os.path.join(_cache_dir(outputs_dir), _FINGERPRINT_LEDGER_FILENAME))
    if path in ledger and isinstance(ledger[path], dict):
        return dict(ledger[path])
    target = _normalize_path(path)
    if not target:
        return None
    for key, entry in ledger.items():
        if isinstance(entry, dict) and _normalize_path(key) == target:
            return dict(entry)
    return None


# ---------------------------------------------------------------------------
# Output-content staleness (mirrors provenance_status._staleness exactly --
# same fields, same reasons, same SHA-256 algorithm -- just reimplemented
# without importing that module).
# ---------------------------------------------------------------------------

def _staleness(path: str, record: dict[str, Any]) -> dict[str, Any]:
    recorded_path = record.get("path") or path
    normalized = _normalize_path(recorded_path) or recorded_path
    exists = os.path.isfile(normalized)
    current_hash = _sha256_file(normalized) if exists else None
    recorded_hash = record.get("content_hash")

    stale = False
    reasons: list[str] = []
    if not exists:
        stale = True
        reasons.append(
            "recorded path no longer exists at its original location "
            "(relocated or deleted since provenance was recorded)"
        )
    elif recorded_hash is None:
        reasons.append(
            "no content hash was captured when this record was made -- "
            "staleness cannot be confirmed either way"
        )
    elif current_hash is None:
        reasons.append(
            "path exists but its current content could not be hashed -- "
            "staleness cannot be confirmed either way"
        )
    elif current_hash != recorded_hash:
        stale = True
        reasons.append(
            "current content hash differs from the hash recorded at "
            "provenance time -- the file has changed since"
        )
    else:
        reasons.append("content hash unchanged since provenance was recorded")

    return {
        "exists_on_disk": exists,
        "recorded_content_hash": recorded_hash,
        "current_content_hash": current_hash,
        "stale": stale,
        "reason": "; ".join(reasons),
    }


# ---------------------------------------------------------------------------
# Generator-script resolution + script-hash staleness (mirrors
# fingerprint._resolve_script_path / fingerprint.check_staleness's per-entry
# logic exactly, reimplemented standalone).
# ---------------------------------------------------------------------------

def _resolve_script_path(hint: str, *, search_root: str | None = None) -> str | None:
    if os.path.isabs(hint) and os.path.isfile(hint):
        return hint
    if search_root:
        candidate = os.path.join(search_root, hint)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    if os.path.isfile(hint):
        return os.path.abspath(hint)
    return None


def _generator_script_info(
    record: dict[str, Any] | None,
    fp_record: dict[str, Any] | None,
    *,
    search_root: str | None = None,
) -> dict[str, Any]:
    """Generator-script path + SHA-256, per this item's explicit acceptance
    criterion. Prefers the fingerprint ledger's already-RESOLVED
    ``script_path`` (real, confirmed-on-disk-at-tag-time path) over
    re-resolving the provenance record's bare ``generating_script`` hint."""
    hint = (record or {}).get("generating_script")
    resolved = (fp_record or {}).get("script_path")
    if not resolved and hint:
        resolved = _resolve_script_path(hint, search_root=search_root)
    return {
        "hint": hint,
        "resolved_path": resolved,
        "sha256": _sha256_file(resolved) if resolved else None,
    }


def _script_staleness(fp_record: dict[str, Any] | None) -> dict[str, Any] | None:
    """Whether the fingerprint-tagged generating script has changed since
    tagging. ``None`` means "never fingerprint-tagged" -- NOT "not stale";
    callers must check for ``None`` before reading ``is_stale``."""
    if not fp_record:
        return None
    script_path = fp_record.get("script_path")
    tagged_hash = fp_record.get("script_hash")
    if not script_path:
        return {
            "script_path": None,
            "tagged_script_hash": tagged_hash,
            "current_script_hash": None,
            "is_stale": False,
            "reason": "no generating script resolved at tag time",
        }
    current_hash = _sha256_file(script_path)
    if current_hash is None:
        return {
            "script_path": script_path,
            "tagged_script_hash": tagged_hash,
            "current_script_hash": None,
            "is_stale": True,
            "reason": "generating script no longer readable/present",
        }
    if tagged_hash is not None and current_hash != tagged_hash:
        return {
            "script_path": script_path,
            "tagged_script_hash": tagged_hash,
            "current_script_hash": current_hash,
            "is_stale": True,
            "reason": "generating script content changed since tagging",
        }
    return {
        "script_path": script_path,
        "tagged_script_hash": tagged_hash,
        "current_script_hash": current_hash,
        "is_stale": False,
        "reason": "generating script unchanged since tagging",
    }


# ---------------------------------------------------------------------------
# Canonical/archival identity (mirrors outputs_local.archival_candidate /
# _canonical_name / classify_canonical_archival's two-stage logic exactly,
# reimplemented with plain SHA-256 instead of importing outputs_local/xxhash,
# scoped to just the one queried path + its sibling directory).
# ---------------------------------------------------------------------------

def _archival_candidate(path: str) -> bool:
    base = os.path.basename(path)
    stem = os.path.splitext(base)[0]
    if base.startswith("_"):
        return True
    return bool(_ARCHIVAL_SUFFIX_RE.search(stem))


def _canonical_name(path: str) -> str:
    directory = os.path.dirname(path)
    base = os.path.basename(path)
    stem, ext = os.path.splitext(base)
    stem = _ARCHIVAL_SUFFIX_RE.sub("", stem)
    if base.startswith("_"):
        stem = stem.lstrip("_")
    return os.path.join(directory, f"{stem}{ext}") if directory else f"{stem}{ext}"


def _classify_archival(path: str) -> dict[str, Any] | None:
    """Best-effort canonical/archival classification for ``path`` alone.

    Returns ``None`` only when ``path`` doesn't exist on disk at all (there
    is nothing to classify). Otherwise always ``{"is_archival", "canonical_
    path", "reason"}`` -- ``is_archival`` is only ever ``True`` when a
    same-directory canonical twin exists AND is byte-identical (SHA-256).
    """
    if not path or not os.path.isfile(path):
        return None
    if not _archival_candidate(path):
        return {
            "is_archival": False,
            "canonical_path": None,
            "reason": "not a name-pattern candidate",
        }
    twin = _canonical_name(path)
    if os.path.normcase(os.path.abspath(twin)) == os.path.normcase(os.path.abspath(path)) or not os.path.isfile(twin):
        return {
            "is_archival": False,
            "canonical_path": twin if os.path.isfile(twin) else None,
            "reason": "archival name pattern but no canonical twin present on disk",
        }
    cand_hash = _sha256_file(path)
    twin_hash = _sha256_file(twin)
    if cand_hash is not None and cand_hash == twin_hash:
        return {
            "is_archival": True,
            "canonical_path": twin,
            "reason": "SHA-256 identical to canonical twin",
        }
    return {
        "is_archival": False,
        "canonical_path": twin,
        "reason": "archival name pattern but content differs from twin",
    }


# ---------------------------------------------------------------------------
# Directory-level MERIDIAN_NOTES.md fallback (mirrors
# outputs_local.MERIDIAN_NOTES_FILENAME / get_annotations_for_path's
# directory-note tier, reimplemented as a direct filesystem read -- no FTS
# index, no DuckDB).
# ---------------------------------------------------------------------------

def _find_directory_note(outputs_dir: str, path: str) -> dict[str, Any] | None:
    """Nearest-ancestor ``MERIDIAN_NOTES.md`` covering ``path``, walking
    upward from ``path``'s own directory up to (and including)
    ``outputs_dir``. ``None`` if none exists at any level."""
    root = os.path.normcase(os.path.abspath(outputs_dir))
    current = os.path.dirname(os.path.abspath(path))
    seen: set[str] = set()
    while True:
        norm_current = os.path.normcase(current)
        if norm_current in seen:
            break  # defensive: never loop forever on a filesystem cycle
        seen.add(norm_current)
        candidate = os.path.join(current, _MERIDIAN_NOTES_FILENAME)
        if os.path.isfile(candidate):
            try:
                with open(candidate, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                text = ""
            return {"source": _MERIDIAN_NOTES_FILENAME, "path": candidate, "note": text}
        if norm_current == root:
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


# ---------------------------------------------------------------------------
# Index generation/convergence: a bounded, per-call filesystem scan of
# outputs_dir, with a persisted (disposable) generation counter.
# ---------------------------------------------------------------------------

@dataclass
class ScanState:
    """One snapshot of how converged this call's local scan of
    ``outputs_dir`` is. See the module docstring's ``inconclusive``
    section for how this gates UNKNOWN/UNREGISTERED confidence."""

    generation: int
    converged: bool
    scanned_count: int
    truncated: bool
    errors: list[str]
    scanned_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _index_ledger_path(outputs_dir: str) -> str:
    return os.path.join(_cache_dir(outputs_dir), _INDEX_LEDGER_FILENAME)


def _load_prior_generation(outputs_dir: str) -> int:
    data = _read_json_object(_index_ledger_path(outputs_dir))
    try:
        return int(data.get("generation", 0))
    except (TypeError, ValueError):
        return 0


def _persist_scan_state(outputs_dir: str, state: ScanState) -> None:
    """Best-effort persistence of the generation counter. This ledger is
    explicitly DISPOSABLE (per this item's "disposable ledger fixtures"
    framing): a write failure here never affects the convergence answer
    already computed for THIS call, and losing the file just resets the
    generation counter back to 1 on the next call -- never a correctness
    issue, only a lost breadcrumb."""
    path = _index_ledger_path(outputs_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state.to_dict(), fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass


def _walk_paths(outputs_dir: str, *, max_files: int) -> tuple[set[str], bool, list[str]]:
    """Bounded, deterministic (sorted) walk of ``outputs_dir``.

    Skips hidden directories (``.git``, etc.) and this module's own cache
    directory. Returns ``(normalized_paths, truncated, errors)`` --
    ``truncated`` is True the moment ``max_files`` is reached (there may be
    more files that were never visited); ``errors`` collects any directory
    that could not be listed (permission denied, removed mid-walk) without
    aborting the rest of the walk.
    """
    found: set[str] = set()
    errors: list[str] = []
    truncated = False
    stack: list[str] = [outputs_dir]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            errors.append(f"{current}: {exc}")
            continue
        dirs: list[str] = []
        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if entry.name.startswith(".") or entry.name == _CACHE_DIRNAME:
                    continue
                dirs.append(entry.path)
            else:
                found.add(_normalize_path(entry.path))
                if len(found) >= max_files:
                    truncated = True
                    return found, truncated, errors
        stack.extend(reversed(dirs))
    return found, truncated, errors


def _scan_outputs_dir(
    outputs_dir: str, *, max_files: int = DEFAULT_MAX_SCAN_FILES,
) -> tuple[set[str], ScanState]:
    prior_generation = _load_prior_generation(outputs_dir)
    found, truncated, errors = _walk_paths(outputs_dir, max_files=max_files)
    state = ScanState(
        generation=prior_generation + 1,
        converged=not truncated and not errors,
        scanned_count=len(found),
        truncated=truncated,
        errors=errors,
        scanned_at=datetime.now(timezone.utc).isoformat(),
    )
    _persist_scan_state(outputs_dir, state)
    return found, state


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def check_output_provenance(
    outputs_dir: str,
    path: str,
    *,
    max_scan_files: int = DEFAULT_MAX_SCAN_FILES,
) -> dict[str, Any]:
    """The local fallback answer to "what do we know about this output's
    provenance" -- see the module docstring for the full status ranking and
    the ``inconclusive`` contract.

    Args:
      outputs_dir:      Absolute path to the outputs directory.
      path:              The output file to look up.
      max_scan_files:    Bound on this call's own filesystem scan (see
                         :func:`_scan_outputs_dir`). Lower this in tests to
                         deterministically force a non-converged scan.

    Returns:
      ``{"error": ...}`` if ``outputs_dir``/``path`` are missing, or
      ``outputs_dir`` does not exist. Otherwise always:

        ``path``, ``provenance_type`` (one of the five explicit statuses),
        ``record`` (the provenance-ledger record, or ``None``),
        ``directory_note`` (the covering ``MERIDIAN_NOTES.md``, or ``None``),
        ``staleness`` (output-content staleness, populated only for
        ``EXACT``/``STALE_BY_SCRIPT``), ``script_staleness`` (generator-
        script staleness, ``None`` if never fingerprint-tagged),
        ``output_sha256`` (exact, freshly-computed SHA-256 of ``path`` on
        disk right now -- independent of any ledger, ``None`` if the file
        doesn't currently exist), ``generator_script`` (hint/resolved_path/
        sha256), ``archival`` (canonical/archival identity, or ``None`` if
        ``path`` doesn't exist on disk), ``convergence`` (this call's
        :class:`ScanState`), and ``inconclusive`` (see module docstring).
    """
    if not outputs_dir or not str(outputs_dir).strip():
        return {"error": "outputs_dir is required"}
    if not os.path.isdir(outputs_dir):
        return {"error": f"outputs_dir does not exist: {outputs_dir}"}
    if not path or not str(path).strip():
        return {"error": "path is required"}

    output_sha256 = _sha256_file(path) if os.path.isfile(path) else None
    archival = _classify_archival(path)
    scanned_paths, scan_state = _scan_outputs_dir(outputs_dir, max_files=max_scan_files)
    discovered = _normalize_path(path) in scanned_paths
    convergence = scan_state.to_dict()

    record = _read_provenance_record(outputs_dir, path)
    if record is not None:
        fp_record = _read_fingerprint_record(outputs_dir, path)
        script_staleness = _script_staleness(fp_record)
        provenance_type = (
            STALE_BY_SCRIPT
            if script_staleness is not None and script_staleness.get("is_stale")
            else EXACT
        )
        return {
            "path": path,
            "provenance_type": provenance_type,
            "record": record,
            "directory_note": None,
            "staleness": _staleness(path, record),
            "script_staleness": script_staleness,
            "output_sha256": output_sha256,
            "generator_script": _generator_script_info(
                record, fp_record, search_root=outputs_dir,
            ),
            "archival": archival,
            "convergence": convergence,
            "inconclusive": False,
        }

    directory_note = _find_directory_note(outputs_dir, path)
    if directory_note is not None:
        return {
            "path": path,
            "provenance_type": DIRECTORY_FALLBACK,
            "record": None,
            "directory_note": directory_note,
            "staleness": None,
            "script_staleness": None,
            "output_sha256": output_sha256,
            "generator_script": _generator_script_info(None, None, search_root=outputs_dir),
            "archival": archival,
            "convergence": convergence,
            "inconclusive": False,
        }

    provenance_type = UNREGISTERED if discovered else UNKNOWN
    inconclusive = (not discovered) and (not scan_state.converged)
    return {
        "path": path,
        "provenance_type": provenance_type,
        "record": None,
        "directory_note": None,
        "staleness": None,
        "script_staleness": None,
        "output_sha256": output_sha256,
        "generator_script": _generator_script_info(None, None, search_root=outputs_dir),
        "archival": archival,
        "convergence": convergence,
        "inconclusive": inconclusive,
    }


# ---------------------------------------------------------------------------
# CLI -- genuinely runnable standalone (no package, no MCP), per this
# package's "fallback tool" purpose: an executor with no meridian-outputs
# MCP connection can still shell out to this file directly.
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="output_provenance_gate",
        description=(
            "Local fallback provenance gate: reports what is known about an "
            "output file's provenance without any meridian-outputs MCP/"
            "extension connection."
        ),
    )
    parser.add_argument("outputs_dir", help="Absolute path to the outputs directory.")
    parser.add_argument("path", help="The output file to look up.")
    parser.add_argument(
        "--max-scan-files", type=int, default=DEFAULT_MAX_SCAN_FILES,
        help="Bound on this call's own filesystem scan.",
    )
    args = parser.parse_args(argv)

    result = check_output_provenance(
        args.outputs_dir, args.path, max_scan_files=args.max_scan_files,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
