"""meridian.temp_artifacts -- local-first temporary script/artifact registry.

Sprint item c4c74141 (source proposal a08defbc-481f-4354-915d-462a254e5e75).

Problem
-------
Impromptu one-off scripts ("just run this quick fix") accumulate no durable
record of what they were, what they read/wrote, or whether they're still
trustworthy -- there is no local, restart-safe place to answer "is the
script that produced this still the one on disk, unchanged?" without
re-deriving it by hand each time.

Prior state (confirmed by reading the existing components before writing a
line of this module, per this item's own acceptance criteria):

  * ``extensions/meridian-outputs/meridian_outputs/fingerprint.py`` already
    solves an adjacent but narrower problem: ``script_content_hash`` (a
    plain SHA-256 hex digest, fail-soft to ``None`` on an unreadable file)
    and ``tag_output``/``check_staleness``/``find_stale_by_script`` track,
    for an OUTPUT file, which generating-script content hash produced it,
    and whether that script has since changed. Ledger:
    ``<outputs_dir>/.meridian-outputs-cache/fingerprint_ledger.json``,
    read-modify-write with a plain ``threading.Lock`` and an atomic
    ``tmp + os.replace`` write.
  * ``extensions/meridian-outputs/meridian_outputs/annotate.py`` records
    lightweight per-OUTPUT reproducibility metadata (generating script,
    params, sprint/decision links, a best-effort content hash of the
    OUTPUT itself) in a sibling ledger (``provenance_ledger.json``, same
    cache dir, same atomic-write idiom, a ``threading.RLock`` per its own
    fa600e42 re-entrancy fix).
  * ``extensions/meridian-outputs/meridian_outputs/provenance_status.py``
    composes the two ledgers above (plus ``outputs_local``'s FTS index)
    into one ranked per-file answer.
  * ``meridian/db/worktree_manifest.py`` is the reusable IMMUTABLE-manifest
    template this item's own notes point at: ``persist_worktree_manifest``
    refuses to silently overwrite an active row (raises unless
    ``force=True``, which explicitly marks the prior row
    ``superseded_at``/``superseded_reason`` rather than deleting it) --
    the "never silently overwrite/discard, mark superseded instead of
    deleting" discipline this module's own lifecycle states borrow.
  * ``meridian/local_resilience.py`` already established BOTH conventions
    this item's target contract calls for that neither ``fingerprint.py``
    nor ``annotate.py`` needed: ``is_onedrive_path``/
    ``assert_disk_only_prestage_path`` (OneDrive must never receive a
    temp/draft artifact -- reused directly, not reimplemented, for this
    module's default storage root), and the documented core/extension
    import boundary ("meridian-outputs and meridian-docs are standalone,
    separately-installable packages ... this module therefore never
    imports either extension; anything reused across that boundary is a
    duck-typed, documented STRING CONVENTION ... never an import").
  * ``meridian/capability_manifest.py`` already defines and enforces the
    "no machine-local absolute path in SHARED state" rule
    (``_ABSOLUTE_PATH_RE`` / ``_check_no_secrets_or_local_paths``) for
    capability manifests. Reused here (by reference to the exact same
    regex, not a re-derived pattern) for this module's OWN
    local-path-vs-shared-pointer boundary.

What this module is, and deliberately is NOT
----------------------------------------------
This is a NEW, general-purpose registry for TEMPORARY SCRIPTS AND THEIR
RUN ARTIFACTS (not just an output file) -- broader in shape than either
existing ledger (adds ``command``, declared ``inputs`` with per-input
fingerprints, ``owner``, ``environment``/tool versions, an explicit
``lifecycle`` state machine, and a ``replaced_by``/``expiry_condition``
pair) and, per this item's own explicit instruction, it does NOT duplicate
either existing ledger's job:

  * It does not re-implement output-staleness tracking -- that remains
    ``fingerprint.py``'s job. :func:`cross_reference_outputs_fingerprint`
    is the (optional, soft-import, never-required) COMPOSITION point that
    proves the two systems agree rather than silently disagreeing: it
    hashes nothing new, it reads ``fingerprint.py``'s own
    ``check_staleness`` ledger rows (when that package happens to be
    installed) and compares its OWN recorded ``script_sha256`` against
    theirs for the same script path.
  * It does not re-implement per-output reproducibility notes -- that
    remains ``annotate.py``'s job.
  * It is not a DB-backed table (unlike ``worktree_manifest.py``'s
    ``wave_base_manifests``) -- per this item's explicit "local-first,
    zero hosted-service dependency" requirement, it is a plain local JSON
    file, matching ``fingerprint.py``/``annotate.py``'s own ledger shape
    (a single JSON object, atomic ``tmp + os.replace`` write) rather than
    a SQL table.
  * meridian core does NOT import the ``meridian_outputs`` extension
    package at module scope anywhere in this file -- see
    ``local_resilience.py``'s own module docstring for why (it is a
    separately-installable package with its own ``pyproject.toml``; core
    must keep working with it absent). ``script_content_hash`` below is
    therefore a deliberate, small re-implementation of
    ``fingerprint.script_content_hash`` (identical algorithm: a plain
    SHA-256 hex digest of the file's raw bytes, ``None`` on any
    ``OSError``) rather than an import, so hashes from the two systems
    remain byte-for-byte comparable without a hard dependency.

Local-vs-shared path boundary
------------------------------
A registry entry's own ``script_path``/``inputs[*].path``/``outputs`` MAY
be machine-local absolute paths -- the manifest itself is genuinely local,
gitignored, per-machine state (see ``default_registry_root``), exactly
like ``local_resilience``'s own temp-run manifests. The boundary this
module enforces is at the point something crosses OUT of this local
manifest INTO Meridian-SHARED state (a note body, a sprint-item pointer, a
handoff): :func:`to_shared_safe_pointer` converts a path to either a
project-relative URI (when it can be made relative to a given
``project_root``) or an explicitly REDACTED reference plus a hash (never
the raw absolute path); :func:`assert_shared_safe` is the fail-closed
guard a caller can run immediately before a ``add_note``/
``generate_handoff``-style call. Both reuse
``capability_manifest._ABSOLUTE_PATH_RE`` (via :func:`is_local_absolute_path`)
so "what counts as a disallowed local path" never drifts between the two
call sites.

Default storage root
----------------------
:func:`default_registry_root` prefers a project-local, git-ignored
``.meridian-temp-artifacts/`` directory (the same
"``.meridian-outputs-cache``-style sidecar, created + gitignored on first
use" convention ``fingerprint.py``/``annotate.py`` already use for their
own ledgers) -- UNLESS that resolves under a OneDrive-synced location
(checked via ``local_resilience.assert_disk_only_prestage_path``, the
SAME detector already established and tested elsewhere in this codebase,
never reimplemented here), in which case it falls back to a
machine-local-only directory under ``tempfile.gettempdir()`` and reports
the fallback EXPLICITLY (``used_fallback``/``reason``) -- never a silent
swap. The root is always returned to the caller, never silently assumed;
every public function in this module also accepts an explicit
``registry_root`` override.

Never executes or deletes a script
-------------------------------------
Nothing in this module ever calls ``subprocess``/``os.remove``/``os.unlink``
on a registered script. :func:`check_artifact`/:func:`check_all` are
read-only diagnostics (best-effort SHA-256 re-read of the script's current
bytes for comparison, and -- only when ``touch=True``, the default --
recording a check timestamp on the registry entry itself); lifecycle
changes (:func:`supersede_artifact`/:func:`retire_artifact`) only ever
flip a ``lifecycle`` string field and bump a version counter, mirroring
``worktree_manifest.py``'s own "mark superseded, never delete" discipline.

NO hosted call is made anywhere in this module -- fully local, matching
the "no hosted call" contract ``fingerprint.py``/``annotate.py`` both
declare for themselves.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from . import capability_manifest as _capability_manifest
from . import local_resilience

__all__ = [
    "TempArtifactError",
    "CorruptManifestError",
    "ACTIVE",
    "SUPERSEDED",
    "RETIRED",
    "LIFECYCLE_STATES",
    "STATUS_PRESENT_CURRENT",
    "STATUS_MISSING",
    "STATUS_CHANGED",
    "STATUS_SUPERSEDED",
    "STATUS_RETIRED",
    "STATUS_UNREADABLE",
    "STATUS_NEVER_VERIFIED",
    "CHECK_STATUSES",
    "TempArtifactEntry",
    "script_content_hash",
    "default_registry_root",
    "is_local_absolute_path",
    "to_shared_safe_pointer",
    "assert_shared_safe",
    "inspect_registry",
    "quarantine_corrupt_registry",
    "scan_for_incomplete_writes",
    "register_artifact",
    "get_artifact",
    "list_artifacts",
    "supersede_artifact",
    "retire_artifact",
    "check_artifact",
    "check_all",
    "cross_reference_outputs_fingerprint",
]

# ---------------------------------------------------------------------------
# Lifecycle + check-status vocabularies.
# ---------------------------------------------------------------------------

ACTIVE = "active"
SUPERSEDED = "superseded"
RETIRED = "retired"
LIFECYCLE_STATES = (ACTIVE, SUPERSEDED, RETIRED)

STATUS_PRESENT_CURRENT = "present_current"
STATUS_MISSING = "missing"
STATUS_CHANGED = "changed"
STATUS_SUPERSEDED = "superseded"
STATUS_RETIRED = "retired"
STATUS_UNREADABLE = "unreadable"
STATUS_NEVER_VERIFIED = "never_verified"
CHECK_STATUSES = (
    STATUS_PRESENT_CURRENT, STATUS_MISSING, STATUS_CHANGED, STATUS_SUPERSEDED,
    STATUS_RETIRED, STATUS_UNREADABLE, STATUS_NEVER_VERIFIED,
)


class TempArtifactError(ValueError):
    """Raised for invalid input or a not-found lookup in this module.
    Mirrors the one-exception-type-per-module convention already used by
    ``local_resilience.LocalResilienceError``/
    ``research_evidence.EnvelopeValidationError``."""


class CorruptManifestError(TempArtifactError):
    """Raised when the on-disk registry file exists but fails validation.

    The corrupt file is NEVER touched by the code path that raises this --
    it is left exactly as found on disk for recovery/inspection. Call
    :func:`quarantine_corrupt_registry` to move it aside (atomic rename,
    never delete) before writing a fresh registry to the same root.
    """


# ---------------------------------------------------------------------------
# Registry entry shape.
# ---------------------------------------------------------------------------

@dataclass
class TempArtifactEntry:
    """One temporary script/artifact's full local record."""

    artifact_id: str
    name: str
    script_path: str
    script_sha256: str | None
    command: str | None
    inputs: list[dict[str, Any]]
    outputs: list[str]
    owner: str | None
    environment: dict[str, Any]
    lifecycle: str
    replaced_by: str | None
    expiry_condition: str | None
    created_at: str
    created_at_epoch: float
    last_checked_at: str | None = None
    last_checked_at_epoch: float | None = None
    version: int = 1
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Hashing -- same algorithm/shape as fingerprint.script_content_hash,
# reimplemented (not imported) per this module's documented core/extension
# boundary -- see module docstring.
# ---------------------------------------------------------------------------

def script_content_hash(path: str) -> str | None:
    """SHA-256 hex digest of ``path``'s current on-disk bytes.

    Never raises: an unreadable/missing/None path yields ``None`` -- the
    exact fail-soft contract
    ``meridian_outputs.fingerprint.script_content_hash`` documents for
    itself, reproduced here byte-for-byte (same algorithm, same encoding)
    so a hash computed by either system is directly comparable to the
    other's -- see :func:`cross_reference_outputs_fingerprint`.
    """
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Default storage root -- project-local, gitignored, OUTSIDE OneDrive.
# ---------------------------------------------------------------------------

_DEFAULT_DIRNAME = ".meridian-temp-artifacts"
_REGISTRY_FILENAME = "temp_artifact_registry.json"
SCHEMA_VERSION = 1


def _ensure_gitignored(directory: str) -> None:
    """Best-effort: append a gitignore entry for ``directory``'s basename
    to the ``.gitignore`` in its PARENT directory only (never walk further
    up -- mirrors ``outputs_local.ensure_gitignored``'s own documented
    reasoning: walking up into an unrelated ancestor's ``.gitignore`` is
    wrong when this directory doesn't live directly under the repo root).
    Swallows all errors -- gitignore hygiene must never block a registry
    write.
    """
    try:
        directory = os.path.abspath(directory)
        parent = os.path.dirname(directory)
        basename = os.path.basename(directory)
        entry = f"/{basename}/"
        gi_path = os.path.join(parent, ".gitignore")
        if os.path.isfile(gi_path):
            with open(gi_path, "r", encoding="utf-8") as fh:
                content = fh.read()
            existing = {ln.strip() for ln in content.splitlines()}
            if entry in existing or f"{basename}/" in existing or basename in existing:
                return
            with open(gi_path, "a", encoding="utf-8") as fh:
                if content and not content.endswith("\n"):
                    fh.write("\n")
                fh.write(entry + "\n")
        else:
            with open(gi_path, "w", encoding="utf-8") as fh:
                fh.write(entry + "\n")
    except OSError:
        pass


def default_registry_root(project_root: str | None = None) -> dict[str, Any]:
    """Compute (and create) this registry's default local storage root.

    Args:
      project_root:  Directory the registry is "for" (typically a repo
                     root). Defaults to ``os.getcwd()``.

    Returns:
      ``{"root": path, "used_fallback": bool, "reason": str | None,
      "project_root": str, "gitignored": bool | None}``. Always surfaces
      the resolved path explicitly -- never a silent, undiscoverable
      default (this item's own contract).

    Prefers ``<project_root>/.meridian-temp-artifacts`` (created +
    gitignored on first use, the same sidecar convention ``fingerprint.py``
    /``annotate.py`` use for their own cache dirs). If THAT path resolves
    under a OneDrive-synced location -- per
    ``local_resilience.assert_disk_only_prestage_path`` (the existing,
    already-tested OneDrive detector this codebase established; never
    reimplemented here) -- falls back to a machine-local-only directory
    under ``tempfile.gettempdir()``, namespaced by a hash of
    ``project_root`` so multiple projects never collide, and reports the
    fallback EXPLICITLY via ``used_fallback``/``reason`` rather than
    silently swapping locations. Never raises: a directory-creation
    failure is reported via ``gitignored=None`` rather than propagating.
    """
    root = os.path.abspath(project_root or os.getcwd())
    candidate = os.path.join(root, _DEFAULT_DIRNAME)
    prestage = local_resilience.assert_disk_only_prestage_path(candidate)
    if prestage["allowed"]:
        gitignored: bool | None
        try:
            os.makedirs(candidate, exist_ok=True)
            _ensure_gitignored(candidate)
            gitignored = True
        except OSError:
            gitignored = None
        return {
            "root": candidate, "used_fallback": False, "reason": None,
            "project_root": root, "gitignored": gitignored,
        }

    key = hashlib.sha256(root.encode("utf-8")).hexdigest()[:16]
    fallback = os.path.join(tempfile.gettempdir(), "meridian-temp-artifacts", key)
    try:
        os.makedirs(fallback, exist_ok=True)
    except OSError:
        pass
    return {
        "root": fallback, "used_fallback": True, "reason": prestage["reason"],
        "project_root": root, "gitignored": None,
    }


# ---------------------------------------------------------------------------
# Local-path vs. shared-safe-pointer boundary -- reuses
# capability_manifest's own absolute-path detector, never re-derived.
# ---------------------------------------------------------------------------

def is_local_absolute_path(value: str) -> bool:
    """``True`` iff ``value`` looks like a machine-local absolute path.

    Reuses ``capability_manifest._ABSOLUTE_PATH_RE`` verbatim (the exact
    pattern that module already enforces for shared capability-manifest
    state: Windows drive letters, UNC paths, POSIX ``/home``/``/Users``/
    ``/root``/``/etc``/``/var``) so "what counts as a disallowed local
    path" never drifts between the two call sites.
    """
    if not value:
        return False
    return bool(_capability_manifest._ABSOLUTE_PATH_RE.search(str(value)))


def to_shared_safe_pointer(
    path: str, *, project_root: str | None = None, content_hash: str | None = None,
) -> dict[str, Any]:
    """Convert ``path`` into a form safe to place in Meridian-SHARED state
    (a note body, a sprint-item pointer, a handoff) -- NEVER the raw
    machine-local absolute path.

    Args:
      path:          The (possibly machine-local) path to convert.
      project_root:  If given and ``path`` resolves under it, the pointer
                     is a portable, forward-slashed, PROJECT-RELATIVE URI.
      content_hash:  Optional hash (e.g. this entry's ``script_sha256``)
                     appended to a redacted pointer so identity can still
                     be confirmed without leaking the raw path.

    Returns:
      ``{"pointer": str, "portable": bool, "reason": str | None}``.
      ``portable=True`` for a project-relative or already-relative path;
      ``portable=False`` for a redacted ``<redacted-local-path:basename>``
      form (optionally with a ``#sha256:<hash>`` suffix), used only when
      ``path`` is absolute/local and cannot be made relative to
      ``project_root``. Never raises.
    """
    raw = str(path)
    if project_root:
        try:
            rel = os.path.relpath(os.path.abspath(raw), os.path.abspath(project_root))
        except ValueError:
            rel = None  # Windows: different drive -- relpath cannot express it
        if rel is not None and rel != os.pardir and not rel.startswith(os.pardir + os.sep):
            return {"pointer": rel.replace(os.sep, "/"), "portable": True, "reason": None}

    if is_local_absolute_path(raw) or os.path.isabs(raw):
        pointer = f"<redacted-local-path:{os.path.basename(raw)}>"
        if content_hash:
            pointer += f"#sha256:{content_hash}"
        return {
            "pointer": pointer, "portable": False,
            "reason": (
                "path is machine-local/absolute and could not be made "
                "relative to project_root -- redacted for shared state"
            ),
        }
    return {"pointer": raw.replace(os.sep, "/"), "portable": True, "reason": None}


def assert_shared_safe(value: str) -> None:
    """Fail-closed guard: raise :class:`TempArtifactError` if ``value``
    (text bound for Meridian-SHARED state) contains a machine-local
    absolute path. Call this immediately before handing a pointer derived
    from this registry to something like ``add_note``/``generate_handoff``
    -- local MANIFEST fields on :class:`TempArtifactEntry` itself are
    exempt by design (see module docstring); this guard is only for the
    boundary crossing INTO shared state.
    """
    if is_local_absolute_path(value):
        raise TempArtifactError(
            f"refusing to place a machine-local absolute path into shared "
            f"Meridian state: {value!r} -- convert it with "
            "to_shared_safe_pointer() first"
        )


# ---------------------------------------------------------------------------
# Registry file I/O -- atomic write, corrupt-manifest detection+preservation.
# ---------------------------------------------------------------------------

# RLock (not Lock): check_artifact's touch=True path calls back into
# _read_registry/_write_registry from within a call already holding this
# lock in check_all's per-item loop is avoided by design (each check_artifact
# call acquires/releases independently), but mirrors annotate.py's own
# fa600e42 re-entrancy fix defensively -- a plain Lock re-acquired by the
# same thread deadlocks; RLock does not.
_write_lock = threading.RLock()


def _registry_path(registry_root: str) -> str:
    return os.path.join(registry_root, _REGISTRY_FILENAME)


def inspect_registry(registry_root: str) -> dict[str, Any]:
    """Read-only status of the registry file at ``registry_root`` --
    NEVER raises, and never mutates anything. Call this (or just call any
    other function and catch :class:`CorruptManifestError`) before
    deciding whether recovery is needed.

    Returns ``{"path", "exists", "corrupt", "reason", "entry_count"}``.
    """
    path = _registry_path(registry_root)
    if not os.path.isfile(path):
        return {"path": path, "exists": False, "corrupt": False, "reason": None, "entry_count": 0}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        return {"path": path, "exists": True, "corrupt": True, "reason": f"unreadable: {exc}", "entry_count": 0}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return {"path": path, "exists": True, "corrupt": True, "reason": f"invalid JSON: {exc}", "entry_count": 0}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return {
            "path": path, "exists": True, "corrupt": True,
            "reason": "missing or invalid top-level 'entries' object",
            "entry_count": 0,
        }
    return {"path": path, "exists": True, "corrupt": False, "reason": None, "entry_count": len(data["entries"])}


def _read_registry(registry_root: str) -> dict[str, Any]:
    """Return a valid registry dict (an empty in-memory shell if none
    exists yet on disk).

    Raises:
      CorruptManifestError: the on-disk file exists but fails validation.
      The bad file is left completely untouched on disk -- this function
      never overwrites or deletes it. Call
      :func:`quarantine_corrupt_registry` to recover.
    """
    status = inspect_registry(registry_root)
    if not status["exists"]:
        return {"schema_version": SCHEMA_VERSION, "registry_version": 0, "entries": {}}
    if status["corrupt"]:
        raise CorruptManifestError(
            f"temp artifact registry at {status['path']!r} is corrupt "
            f"({status['reason']}) -- left untouched on disk, NOT "
            "overwritten. Call quarantine_corrupt_registry() to move it "
            "aside before a fresh registry can be written here."
        )
    with open(status["path"], "r", encoding="utf-8") as fh:
        data = json.load(fh)
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("registry_version", 0)
    data.setdefault("entries", {})
    return data


def _write_registry(registry_root: str, data: dict[str, Any]) -> None:
    os.makedirs(registry_root, exist_ok=True)
    path = _registry_path(registry_root)
    data["registry_version"] = int(data.get("registry_version", 0)) + 1
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)  # atomic on both POSIX and Windows -- same idiom
    # fingerprint.py/annotate.py's own ledger writes already use.


def quarantine_corrupt_registry(registry_root: str) -> dict[str, Any]:
    """Move an existing CORRUPT registry file aside via an atomic rename
    (never a delete) so it remains available for manual recovery/
    inspection, clearing the way for a fresh registry to be written at
    ``registry_root``.

    Raises:
      TempArtifactError: no registry file exists, or the existing file is
      NOT actually corrupt (this is a recovery action, not a reset
      button -- refusing to quarantine a valid, in-use registry).
    """
    status = inspect_registry(registry_root)
    if not status["exists"]:
        raise TempArtifactError(f"no registry file at {status['path']!r} to quarantine")
    if not status["corrupt"]:
        raise TempArtifactError(
            f"registry at {status['path']!r} is not corrupt -- refusing to "
            "quarantine a valid, in-use registry"
        )
    path = status["path"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    quarantined = f"{path}.corrupt-{stamp}.bak"
    with _write_lock:
        os.replace(path, quarantined)  # atomic rename, never a delete
    return {"original_path": path, "quarantined_path": quarantined, "reason": status["reason"]}


def scan_for_incomplete_writes(registry_root: str) -> dict[str, Any]:
    """Read-only: reports whether a leftover ``<registry>.tmp`` sibling
    exists -- the fossil of a write interrupted (process killed) between
    opening the tmp file and the final atomic ``os.replace`` rename.

    Never deletes it (left for manual/forensic inspection). The COMMITTED
    registry file itself is never at risk regardless: ``os.replace`` is
    atomic, so a reader only ever observes either the fully-old or
    fully-new committed file, never a torn write -- this function reports
    an informational fossil, not a corruption.
    """
    tmp_path = _registry_path(registry_root) + ".tmp"
    exists = os.path.isfile(tmp_path)
    return {
        "tmp_path": tmp_path,
        "incomplete_write_detected": exists,
        "note": (
            "a prior write did not complete its final atomic rename -- "
            "the committed registry file is unaffected; this stray file "
            "is left for manual inspection, never auto-deleted"
        ) if exists else None,
    }


# ---------------------------------------------------------------------------
# Registration + lifecycle.
# ---------------------------------------------------------------------------

def _normalize_inputs(inputs: "list[Any] | None") -> list[dict[str, Any]]:
    if not inputs:
        return []
    normalized: list[dict[str, Any]] = []
    for item in inputs:
        if isinstance(item, dict):
            path = item.get("path")
            fingerprint = item.get("fingerprint")
        else:
            path, fingerprint = item, None
        if not path:
            continue
        if fingerprint is None:
            fingerprint = script_content_hash(path)
        normalized.append({"path": path, "fingerprint": fingerprint})
    return normalized


def register_artifact(
    registry_root: str,
    *,
    name: str,
    script_path: str,
    command: str | None = None,
    inputs: "list[Any] | None" = None,
    outputs: "list[str] | None" = None,
    owner: str | None = None,
    environment: "dict[str, Any] | None" = None,
    expiry_condition: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Register ONE new temporary script/artifact entry.

    Never executes ``script_path`` -- only opens it (if present) to
    compute a best-effort SHA-256 via :func:`script_content_hash`, the
    same read-only, fail-soft contract ``fingerprint.py`` documents for
    itself.

    Args:
      registry_root:     Local storage root (see
                          :func:`default_registry_root`).
      name:                Human label for this artifact.
      script_path:         Path to the script (local-only or already-
                          portable, caller's choice -- see module
                          docstring's local-vs-shared boundary).
      command:             Exact invoked command line, if known.
      inputs:              Declared inputs: a list of paths, or of
                          ``{"path", "fingerprint"}`` dicts. A missing
                          ``fingerprint`` is computed best-effort via
                          :func:`script_content_hash`.
      outputs:             Output pointers (paths), verbatim.
      owner:               Free-text owner/author.
      environment:         Opaque environment/tool-version info; this
                          function fills in ``python_version``/
                          ``platform`` if the caller omits them.
      expiry_condition:    Free-text description of when this entry
                          should be considered expired/replaced.
      notes:               Optional free-text note.

    Returns:
      The new entry as a dict (``lifecycle="active"``, ``version=1``).

    Raises:
      TempArtifactError: ``name``/``script_path`` missing.
      CorruptManifestError: the on-disk registry is corrupt (see
      :func:`quarantine_corrupt_registry`).
    """
    if not name or not str(name).strip():
        raise TempArtifactError("register_artifact: name is required")
    if not script_path or not str(script_path).strip():
        raise TempArtifactError("register_artifact: script_path is required")

    env = dict(environment or {})
    env.setdefault("python_version", sys.version.split()[0])
    env.setdefault("platform", platform.platform())

    now = time.time()
    entry = TempArtifactEntry(
        artifact_id=str(uuid.uuid4()),
        name=str(name).strip(),
        script_path=script_path,
        script_sha256=script_content_hash(script_path),
        command=command,
        inputs=_normalize_inputs(inputs),
        outputs=list(outputs or []),
        owner=owner,
        environment=env,
        lifecycle=ACTIVE,
        replaced_by=None,
        expiry_condition=expiry_condition,
        created_at=datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        created_at_epoch=now,
        notes=notes,
    )
    with _write_lock:
        data = _read_registry(registry_root)
        data["entries"][entry.artifact_id] = entry.to_dict()
        _write_registry(registry_root, data)
    return entry.to_dict()


def get_artifact(registry_root: str, artifact_id: str) -> "dict[str, Any] | None":
    """The stored entry dict for ``artifact_id``, or ``None``."""
    data = _read_registry(registry_root)
    entry = data["entries"].get(artifact_id)
    return dict(entry) if entry is not None else None


def list_artifacts(registry_root: str, *, lifecycle: str | None = None) -> list[dict[str, Any]]:
    """All entries, newest-created first, optionally filtered by
    ``lifecycle``. ``[]`` if nothing is registered yet."""
    data = _read_registry(registry_root)
    rows = list(data["entries"].values())
    if lifecycle is not None:
        rows = [r for r in rows if r.get("lifecycle") == lifecycle]
    return sorted(rows, key=lambda r: r.get("created_at_epoch") or 0.0, reverse=True)


def _transition(
    registry_root: str, artifact_id: str, *, new_state: str,
    replaced_by: "str | None" = None, reason: "str | None" = None,
) -> dict[str, Any]:
    if new_state not in LIFECYCLE_STATES:
        raise TempArtifactError(f"invalid lifecycle state: {new_state!r}")
    with _write_lock:
        data = _read_registry(registry_root)
        entry = data["entries"].get(artifact_id)
        if entry is None:
            raise TempArtifactError(f"no temp artifact with id {artifact_id!r} in {registry_root!r}")
        entry = dict(entry)
        entry["lifecycle"] = new_state
        if replaced_by is not None:
            entry["replaced_by"] = replaced_by
        if reason:
            prior_notes = entry.get("notes")
            entry["notes"] = (f"{prior_notes} | " if prior_notes else "") + f"[{new_state}] {reason}"
        entry["version"] = int(entry.get("version", 1)) + 1
        data["entries"][artifact_id] = entry
        _write_registry(registry_root, data)
    return entry


def supersede_artifact(
    registry_root: str, artifact_id: str, *,
    replaced_by: "str | None" = None, reason: "str | None" = None,
) -> dict[str, Any]:
    """Mark an entry SUPERSEDED (never deletes it) -- mirrors
    ``worktree_manifest.persist_worktree_manifest``'s own "explicit,
    audited replacement, prior row marked superseded" discipline.
    ``replaced_by`` may name the new entry's ``artifact_id``.
    """
    return _transition(registry_root, artifact_id, new_state=SUPERSEDED, replaced_by=replaced_by, reason=reason)


def retire_artifact(registry_root: str, artifact_id: str, *, reason: "str | None" = None) -> dict[str, Any]:
    """Mark an entry RETIRED (never deletes it)."""
    return _transition(registry_root, artifact_id, new_state=RETIRED, reason=reason)


# ---------------------------------------------------------------------------
# Read-only checker -- NEVER deletes or executes the registered script.
# ---------------------------------------------------------------------------

def check_artifact(registry_root: str, artifact_id: str, *, touch: bool = True) -> dict[str, Any]:
    """Read-only diagnostic for ONE entry. Never deletes or executes
    ``script_path`` -- only re-reads its current bytes (if present) to
    recompute a hash for comparison.

    Precedence: an entry's ``lifecycle`` (superseded/retired) is checked
    FIRST and wins outright, regardless of whether the script still
    happens to exist unchanged on disk -- lifecycle is an explicit
    human/executor decision, more authoritative than a raw file
    comparison. For an ``active`` entry: missing (file gone) >
    unreadable (exists, can't be read/hashed) > never_verified (no hash
    was ever recorded to compare against) > changed (hash differs) >
    present_current.

    Args:
      touch:  When ``True`` (default), records this check's timestamp on
              the entry (``last_checked_at``). Pass ``False`` for a
              purely read-only probe that must not mutate the registry.

    Returns:
      ``{"artifact_id", "name", "status", "detail", "lifecycle",
      "script_path", "recorded_script_sha256", "current_script_sha256",
      "checked_at"}`` where ``status`` is one of :data:`CHECK_STATUSES`.

    Raises:
      TempArtifactError: ``artifact_id`` is unknown.
      CorruptManifestError: the on-disk registry is corrupt.
    """
    data = _read_registry(registry_root)
    entry = data["entries"].get(artifact_id)
    if entry is None:
        raise TempArtifactError(f"no temp artifact with id {artifact_id!r} in {registry_root!r}")

    lifecycle = entry.get("lifecycle")
    script_path = entry.get("script_path")
    recorded_hash = entry.get("script_sha256")
    current_hash: "str | None" = None

    if lifecycle == SUPERSEDED:
        status = STATUS_SUPERSEDED
        detail = f"superseded (replaced_by={entry.get('replaced_by')!r})"
    elif lifecycle == RETIRED:
        status = STATUS_RETIRED
        detail = "retired"
    elif not script_path or not os.path.isfile(script_path):
        status = STATUS_MISSING
        detail = "script_path no longer exists on disk"
    else:
        current_hash = script_content_hash(script_path)
        if current_hash is None:
            status = STATUS_UNREADABLE
            detail = "script exists but could not be read/hashed"
        elif recorded_hash is None:
            status = STATUS_NEVER_VERIFIED
            detail = "no script hash was recorded at registration time to compare against"
        elif current_hash != recorded_hash:
            status = STATUS_CHANGED
            detail = "current script content hash differs from the hash recorded at registration"
        else:
            status = STATUS_PRESENT_CURRENT
            detail = "script content unchanged since registration"

    checked_at = datetime.now(timezone.utc).isoformat()
    result = {
        "artifact_id": artifact_id, "name": entry.get("name"), "status": status,
        "detail": detail, "lifecycle": lifecycle, "script_path": script_path,
        "recorded_script_sha256": recorded_hash, "current_script_sha256": current_hash,
        "checked_at": checked_at,
    }

    if touch:
        with _write_lock:
            fresh = _read_registry(registry_root)
            live = fresh["entries"].get(artifact_id)
            if live is not None:
                live = dict(live)
                live["last_checked_at"] = checked_at
                live["last_checked_at_epoch"] = time.time()
                fresh["entries"][artifact_id] = live
                _write_registry(registry_root, fresh)
    return result


def check_all(registry_root: str, *, touch: bool = True) -> list[dict[str, Any]]:
    """:func:`check_artifact` for every registered entry, ordered by
    ``artifact_id`` for determinism. ``[]`` if nothing is registered."""
    data = _read_registry(registry_root)
    return [check_artifact(registry_root, aid, touch=touch) for aid in sorted(data["entries"])]


# ---------------------------------------------------------------------------
# Optional, soft-import composition with the meridian-outputs extension's
# OWN fingerprint ledger -- proves the two systems agree, never a hard
# dependency (see module docstring's core/extension boundary section).
# ---------------------------------------------------------------------------

def cross_reference_outputs_fingerprint(
    registry_root: str, artifact_id: str, outputs_dir: str,
) -> dict[str, Any]:
    """Best-effort cross-check against
    ``extensions/meridian-outputs/meridian_outputs/fingerprint.py``'s OWN
    independent script-tagging ledger (sprint item 7518bfcd) for the same
    script path, when that package happens to be installed in the current
    environment.

    This never duplicates fingerprint.py's storage -- it only reads its
    ``check_staleness`` results and compares THIS registry's recorded
    ``script_sha256`` against fingerprint.py's own ``tagged_script_hash``
    for a ledger row pointing at the same ``script_path``, proving the two
    systems compute byte-identical SHA-256 hashes for the same input and
    can be correlated rather than silently disagreeing or duplicating each
    other's bookkeeping.

    Args:
      registry_root:  This module's registry root.
      artifact_id:    The entry to cross-check.
      outputs_dir:    The ``outputs_dir`` fingerprint.py's ledger lives
                      under (``<outputs_dir>/.meridian-outputs-cache/``).

    Returns:
      ``{"available": bool, "reason": str | None, "agrees": bool | None,
      "temp_artifacts_script_sha256": str | None,
      "fingerprint_ledger_script_hash": str | None}``.

      ``available=False`` (the ``meridian_outputs`` package is not
      installed in this environment, or this script was never
      fingerprint-tagged under ``outputs_dir``) is a normal, EXPECTED
      outcome -- never an error. This function never raises for either
      reason; ``agrees`` is only meaningful when ``available=True`` AND a
      matching ledger row was found.

    Raises:
      TempArtifactError: ``artifact_id`` is unknown in THIS registry.
    """
    entry = get_artifact(registry_root, artifact_id)
    if entry is None:
        raise TempArtifactError(f"no temp artifact with id {artifact_id!r} in {registry_root!r}")
    ours = entry.get("script_sha256")

    try:
        from meridian_outputs import fingerprint as _outputs_fingerprint  # type: ignore
    except ImportError:
        return {
            "available": False,
            "reason": "meridian_outputs (extensions/meridian-outputs) is not installed in this environment",
            "agrees": None, "temp_artifacts_script_sha256": ours,
            "fingerprint_ledger_script_hash": None,
        }

    script_path = entry.get("script_path")
    script_abspath = os.path.abspath(script_path) if script_path else None
    for staleness in _outputs_fingerprint.check_staleness(outputs_dir):
        if staleness.script_path and script_abspath and os.path.abspath(staleness.script_path) == script_abspath:
            ledger_hash = staleness.tagged_script_hash
            return {
                "available": True, "reason": None,
                "agrees": bool(ledger_hash is not None and ours is not None and ledger_hash == ours),
                "temp_artifacts_script_sha256": ours,
                "fingerprint_ledger_script_hash": ledger_hash,
            }

    return {
        "available": True,
        "reason": f"{script_path!r} was never fingerprint-tagged under {outputs_dir!r}",
        "agrees": None, "temp_artifacts_script_sha256": ours,
        "fingerprint_ledger_script_hash": None,
    }


# ---------------------------------------------------------------------------
# Small CLI -- usable for impromptu fixes right now, zero hosted dependency.
# ---------------------------------------------------------------------------

def _resolve_root(explicit_root: "str | None") -> str:
    if explicit_root:
        return explicit_root
    info = default_registry_root()
    print(f"# using default registry root: {info['root']}", file=sys.stderr)
    if info["used_fallback"]:
        print(f"# NOTE: fell back to a machine-local temp dir -- {info['reason']}", file=sys.stderr)
    return info["root"]


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m meridian.temp_artifacts",
        description="Local-first temporary script/artifact registry (read-only checks; never executes or deletes a script).",
    )
    parser.add_argument("--root", default=None, help="Registry storage root (default: computed via default_registry_root()).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_register = sub.add_parser("register", help="Register a new temporary script/artifact.")
    p_register.add_argument("--name", required=True)
    p_register.add_argument("--script", required=True, dest="script_path")
    p_register.add_argument("--command", default=None)
    p_register.add_argument("--owner", default=None)
    p_register.add_argument("--expiry", dest="expiry_condition", default=None)
    p_register.add_argument("--input", dest="inputs", action="append", default=[])
    p_register.add_argument("--output", dest="outputs", action="append", default=[])
    p_register.add_argument("--notes", default=None)

    p_check = sub.add_parser("check", help="Check one artifact (or all, if none given). Read-only.")
    p_check.add_argument("artifact_id", nargs="?", default=None)

    p_list = sub.add_parser("list", help="List registered artifacts.")
    p_list.add_argument("--lifecycle", choices=LIFECYCLE_STATES, default=None)

    p_supersede = sub.add_parser("supersede", help="Mark an artifact superseded (never deletes it).")
    p_supersede.add_argument("artifact_id")
    p_supersede.add_argument("--replaced-by", dest="replaced_by", default=None)
    p_supersede.add_argument("--reason", default=None)

    p_retire = sub.add_parser("retire", help="Mark an artifact retired (never deletes it).")
    p_retire.add_argument("artifact_id")
    p_retire.add_argument("--reason", default=None)

    sub.add_parser("inspect", help="Report whether the registry file is present/corrupt. Read-only.")

    return parser


def _cli(argv: "list[str] | None" = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    root = _resolve_root(args.root)

    if args.cmd == "register":
        result: Any = register_artifact(
            root, name=args.name, script_path=args.script_path, command=args.command,
            inputs=list(args.inputs), outputs=list(args.outputs), owner=args.owner,
            expiry_condition=args.expiry_condition, notes=args.notes,
        )
    elif args.cmd == "check":
        result = check_artifact(root, args.artifact_id) if args.artifact_id else check_all(root)
    elif args.cmd == "list":
        result = list_artifacts(root, lifecycle=args.lifecycle)
    elif args.cmd == "supersede":
        result = supersede_artifact(root, args.artifact_id, replaced_by=args.replaced_by, reason=args.reason)
    elif args.cmd == "retire":
        result = retire_artifact(root, args.artifact_id, reason=args.reason)
    elif args.cmd == "inspect":
        result = inspect_registry(root)
    else:  # pragma: no cover -- argparse's required=True already prevents this
        parser.error(f"unknown command {args.cmd!r}")
        return 2

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover -- exercised via _cli() in tests
    raise SystemExit(_cli())
