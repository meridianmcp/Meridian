"""Stable artifact registry: source-bound, hash-verified, relocation-safe IDs.

Sprint item e1c979e3 (MDE-4 P0).

Problem
-------
Every existing resolution primitive in this package (``provenance
.resolve_figure_output``'s basename-fallback tier, ``provenance
.bind_artifact_provenance``'s per-write classification) answers "does THIS
path resolve against the outputs index right now". None of them give a
caller a STABLE, portable identifier for an artifact that survives the file
being relocated, renamed, or copied into a docs/media folder -- every lookup
starts over from a machine-local path. That means two different agents (or
the same agent across sessions) working with "the same" figure have no
shared vocabulary for it beyond "whatever path it currently happens to sit
at", and a caller wanting to say "reject this write, its artifact identity
is ambiguous" has no durable place to record that verdict.

What this module adds
----------------------
A small, durable, atomic JSON-ledger registry (same on-disk convention as
:mod:`annotate` / :mod:`fingerprint` -- ``<outputs_dir>/.meridian-outputs-
cache/artifact_registry.json``, read-modify-written via ``os.replace``, no
DB engine, no hosted call) that:

  * Mints a **relocation-safe public ``artifact_id``** deterministically from
    PORTABLE identity signals only -- content hash, generator/tool, and an
    explicit ``source_locator`` -- never from the absolute path. Re-
    registering the same logical artifact from a new location (same content,
    same generator/source) yields the exact same id.
  * Binds each artifact to source identity, content hash, generator/run,
    canonical-or-archival role, and an explicit ``lifecycle_state`` --
    :func:`register_artifact`.
  * Stores the exact on-disk path ONLY as redacted/local metadata
    (``local_paths[*].local_only_path``), clearly separated from the
    portable identity fields -- :func:`strip_local_metadata` produces a
    shareable view with that field removed.
  * Verifies content hashes on demand -- :func:`verify_artifact_hash` --
    and never silently reports "verified" when there is nothing on file to
    compare against.
  * Records explicit source-to-artifact AND artifact-to-source edges --
    :func:`bind_source_edge`, :func:`get_source_artifacts`,
    :func:`get_artifact_sources`.
  * Resolves an artifact from a path or content hash with EXPLICIT
    ``resolved`` / ``ambiguous`` / ``unresolved`` / ``orphaned`` outcomes --
    :func:`resolve_artifact` -- and never falls back to a basename/fuzzy
    guess the way ``provenance.resolve_figure_output``'s second tier does.
    Multiple genuine candidates are surfaced as ``ambiguous`` with every
    candidate id listed, never silently narrowed to "the first one".
  * Produces a migration/reconciliation report for legacy outputs already
    known to :mod:`annotate`'s provenance ledger but not yet in this
    registry -- :func:`reconcile_legacy_outputs`.
  * Fails closed on ambiguous identity: :func:`register_artifact` REFUSES
    (raises :class:`RegistryError`) to mint an id when there is nothing
    portable to anchor it to (every artifact of the same ``kind`` would
    otherwise collide onto the identical id), and refuses when an explicit
    ``expected_sha256`` contradicts what is actually on disk or already on
    file for that id.

Reuse over reinvention: content hashing reuses
:func:`fingerprint.script_content_hash` (the SAME sha256-of-bytes helper
:mod:`annotate` already uses for its own ``content_hash`` field); the
ledger read/write/atomic-replace idiom mirrors :mod:`annotate`'s
``provenance_ledger.json`` exactly; nothing in :mod:`outputs_local` is
modified.

Non-goals: this module does not replace ``provenance.resolve_figure_output``
or ``provenance.bind_artifact_provenance`` -- both keep their existing,
narrower contracts (per-write, path-first resolution against the outputs
FTS index) and are unaffected. This module is the NEW, separate identity
layer sprint item e1c979e3 asks for; wiring it as the sole resolution path
everywhere else is a follow-up, not part of this change.
"""
from __future__ import annotations

import json
import os
import platform
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import annotate, fingerprint, outputs_local

__all__ = [
    "RegistryError",
    "RESOLVED",
    "AMBIGUOUS",
    "HASH_MISMATCH",
    "UNRESOLVED",
    "ORPHANED",
    "ARTIFACT_REGISTRY_STATUSES",
    "ACTIVE",
    "QUARANTINED",
    "DEPRECATED",
    "DELETED",
    "LIFECYCLE_STATES",
    "ArtifactRecord",
    "SourceEdge",
    "compute_artifact_id",
    "register_artifact",
    "resolve_artifact",
    "verify_artifact_hash",
    "bind_source_edge",
    "get_artifact_sources",
    "get_source_artifacts",
    "set_lifecycle_state",
    "get_artifact",
    "list_artifacts",
    "strip_local_metadata",
    "reconcile_legacy_outputs",
]

_CACHE_DIRNAME = ".meridian-outputs-cache"
_REGISTRY_FILENAME = "artifact_registry.json"

# Fixed, never-changing namespace UUID -- part of this module's own stable
# contract. Changing it would silently re-mint every previously-issued
# artifact_id, so it is a constant, not configuration.
_ARTIFACT_ID_NAMESPACE = uuid.UUID("6f1d6b6a-6d3a-4b1a-9a9b-e1c979e36d57")

# In-process only, matching annotate.py's own reasoning: this is a
# lower-stakes, mostly-single-writer sidecar ledger, not the shared FTS
# index -- cross-process portalocker exclusivity is not warranted here
# either.
_write_lock = threading.Lock()

RESOLVED = "resolved"
AMBIGUOUS = "ambiguous"
HASH_MISMATCH = "hash_mismatch"
UNRESOLVED = "unresolved"
ORPHANED = "orphaned"
ARTIFACT_REGISTRY_STATUSES = (RESOLVED, AMBIGUOUS, HASH_MISMATCH, UNRESOLVED, ORPHANED)

ACTIVE = "active"
QUARANTINED = "quarantined"
DEPRECATED = "deprecated"
DELETED = "deleted"
LIFECYCLE_STATES = (ACTIVE, QUARANTINED, DEPRECATED, DELETED)


class RegistryError(ValueError):
    """Raised for fail-closed registry integrity failures: an identity that
    cannot be anchored to any portable signal, a hash that contradicts what
    is already on file, or an edge/lifecycle operation naming an artifact_id
    that was never registered. Deliberately mirrors
    ``research_evidence.EnvelopeValidationError``'s "one exception type for
    every construction-time failure" convention -- callers catch one thing.
    """


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _cache_dir(outputs_dir: str) -> str:
    cache_dir = os.path.join(outputs_dir, _CACHE_DIRNAME)
    try:
        os.makedirs(cache_dir, exist_ok=True)
        outputs_local.ensure_gitignored(cache_dir)
    except OSError:
        pass
    return cache_dir


def _registry_path(outputs_dir: str) -> str:
    return os.path.join(_cache_dir(outputs_dir), _REGISTRY_FILENAME)


def _empty_registry() -> dict[str, Any]:
    return {"artifacts": {}, "edges": []}


def _read_registry(outputs_dir: str) -> dict[str, Any]:
    path = _registry_path(outputs_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return _empty_registry()
    if not isinstance(data, dict):
        return _empty_registry()
    data.setdefault("artifacts", {})
    data.setdefault("edges", [])
    if not isinstance(data["artifacts"], dict):
        data["artifacts"] = {}
    if not isinstance(data["edges"], list):
        data["edges"] = []
    return data


def _write_registry(outputs_dir: str, registry: dict[str, Any]) -> None:
    path = _registry_path(outputs_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(registry, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)  # atomic on both POSIX and Windows


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_path_key(path: Any) -> str:
    """Case/slash-insensitive key for exact local-path membership checks.

    Deliberately does NOT resolve relative to the current working directory
    (same reasoning as ``provenance._path_key``): this key is only used for
    an EXACT membership test against previously-recorded local paths, never
    for a fuzzy/basename comparison.
    """
    if not path:
        return ""
    return os.path.normcase(str(path).strip().replace("\\", "/"))


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SourceEdge:
    """One typed edge between an ``artifact_id`` and a ``source_locator``.

    Undirected in storage (one row), but queryable from both ends --
    :func:`get_artifact_sources` (artifact -> its sources) and
    :func:`get_source_artifacts` (source -> the artifacts it produced) both
    read the same edge list, so the two directions can never drift apart.
    """

    edge_id: str
    artifact_id: str
    source_locator: str
    relation: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ArtifactRecord:
    """One durable registry entry: a relocation-safe public identity bound
    to portable provenance (content hash, generator/run, role, lifecycle)
    plus a REDACTED bucket of local, machine-specific path sightings.
    """

    artifact_id: str
    kind: str
    content_hash: str | None = None
    hash_algorithm: str = "sha256"
    generator: str | None = None
    run_id: str | None = None
    source_locator: str | None = None
    role: str | None = None
    lifecycle_state: str = ACTIVE
    created_at: str = ""
    updated_at: str = ""
    hash_verified: bool = False
    local_paths: list[dict[str, Any]] = field(default_factory=list)
    lifecycle_history: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def strip_local_metadata(record: dict[str, Any]) -> dict[str, Any]:
    """A shareable projection of a registry record with every
    machine-local absolute path removed.

    ``local_paths`` entries keep only ``basename``/``host``/``first_seen``/
    ``last_seen`` -- ``local_only_path`` (the actual absolute path) is
    dropped entirely. Every other field is portable already and passes
    through unchanged. Never mutates ``record``.
    """
    clean = dict(record)
    clean["local_paths"] = [
        {k: v for k, v in entry.items() if k != "local_only_path"}
        for entry in record.get("local_paths", [])
    ]
    return clean


# ---------------------------------------------------------------------------
# Identity minting
# ---------------------------------------------------------------------------

def compute_artifact_id(
    kind: str,
    *,
    content_hash: str | None = None,
    generator: str | None = None,
    source_locator: str | None = None,
) -> str:
    """Deterministic, relocation-safe public id for an artifact.

    Derived ONLY from portable signals (never from a filesystem path):
    ``kind`` + ``content_hash`` + ``generator`` + ``source_locator``. The
    same logical artifact re-registered from a different machine or a
    different path on the same machine yields the exact same id, as long as
    at least one of these signals is unchanged.

    Raises:
      RegistryError: none of ``content_hash``/``generator``/
        ``source_locator`` is a non-empty string. Minting an id from
        ``kind`` alone would collide every artifact of that kind onto the
        identical id -- the fail-closed identity-ambiguity case this module
        exists to prevent, so this is refused outright rather than silently
        producing a colliding id.
    """
    if not kind or not str(kind).strip():
        raise RegistryError("compute_artifact_id: kind is required")
    anchors = [
        str(content_hash).strip() if content_hash else "",
        str(generator).strip() if generator else "",
        str(source_locator).strip() if source_locator else "",
    ]
    if not any(anchors):
        raise RegistryError(
            "cannot mint a relocation-safe artifact id: none of "
            "content_hash/generator/source_locator was provided -- with no "
            "portable identity signal, every artifact of this kind would "
            "collide onto the same id (ambiguous by construction). Supply "
            "at least one, or resolve/verify by explicit local path instead "
            "of registering a new identity."
        )
    seed = "|".join(["artifact_registry_v1", str(kind).strip(), *anchors])
    return str(uuid.uuid5(_ARTIFACT_ID_NAMESPACE, seed))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_artifact(
    outputs_dir: str,
    kind: str,
    *,
    canonical_path: str | None = None,
    expected_sha256: str | None = None,
    generator: str | None = None,
    run_id: str | None = None,
    source_locator: str | None = None,
    role: str | None = None,
    lifecycle_state: str = ACTIVE,
    metadata: dict[str, Any] | None = None,
    host: str | None = None,
) -> dict[str, Any]:
    """Bind (create or update) a stable public artifact identity.

    Args:
      outputs_dir:      Absolute path to the outputs directory (the registry
                        ledger lives under its ``.meridian-outputs-cache/``,
                        same convention as every sibling module).
      kind:              Artifact kind (e.g. ``"figure"``, ``"table"``,
                        ``"equation"``, ``"output"``, ``"document"``).
                        Required, non-empty.
      canonical_path:    The artifact's current on-disk location, if known.
                        Hashed (best-effort) to derive ``content_hash``;
                        stored ONLY under the redacted ``local_paths`` bucket
                        -- never used to derive ``artifact_id`` itself.
      expected_sha256:  Optional caller-asserted hash. Checked against the
                        freshly-computed hash of ``canonical_path`` (when
                        readable) AND against any hash already on file for
                        this id (when updating an existing record) -- a
                        mismatch against EITHER refuses the write
                        (fail-closed; see Raises).
      generator:         The script/tool that produced this artifact.
                        Portable identity anchor.
      run_id:            Optional run/session identifier.
      source_locator:    Portable locator for the source this artifact came
                        from (a relative path, dataset name, DOI, sprint-item
                        id, etc.) -- deliberately caller-defined, not
                        interpreted here. Portable identity anchor.
      role:              ``"canonical"`` or ``"archival"``, if known.
      lifecycle_state:   One of :data:`LIFECYCLE_STATES` (default
                        ``"active"``).
      metadata:          Opaque caller-supplied extra fields, merged
                        (shallow) into the record's own ``metadata`` on
                        update.
      host:              Override for the local hostname recorded on the
                        redacted path-sighting entry (defaults to
                        ``platform.node()``); mainly for deterministic tests.

    Returns:
      The stored record as a dict (see :class:`ArtifactRecord`), plus
      ``created`` (``True`` on first registration, ``False`` on an update to
      an existing id).

    Raises:
      RegistryError:
        - ``outputs_dir``/``kind`` missing, or ``lifecycle_state`` not one
          of :data:`LIFECYCLE_STATES`.
        - :func:`compute_artifact_id` cannot anchor an id (see its own
          docstring).
        - ``expected_sha256`` is given and contradicts the freshly-computed
          hash of ``canonical_path`` (when readable), OR contradicts a hash
          already on file for this id from a prior registration. Either way
          the write is refused rather than silently accepted or silently
          overwriting a conflicting prior hash.
    """
    if not outputs_dir or not str(outputs_dir).strip():
        raise RegistryError("register_artifact: outputs_dir is required")
    if not kind or not str(kind).strip():
        raise RegistryError("register_artifact: kind is required")
    if lifecycle_state not in LIFECYCLE_STATES:
        raise RegistryError(
            f"register_artifact: lifecycle_state must be one of "
            f"{LIFECYCLE_STATES}, got {lifecycle_state!r}"
        )

    computed_hash: str | None = None
    if canonical_path and os.path.isfile(canonical_path):
        computed_hash = fingerprint.script_content_hash(canonical_path)

    if expected_sha256 and computed_hash and str(expected_sha256) != str(computed_hash):
        raise RegistryError(
            f"register_artifact: expected_sha256 {expected_sha256!r} does not "
            f"match the freshly-computed content hash {computed_hash!r} of "
            f"{canonical_path!r} -- refusing to register a mismatched hash"
        )

    content_hash = computed_hash or expected_sha256
    artifact_id = compute_artifact_id(
        kind, content_hash=content_hash, generator=generator,
        source_locator=source_locator,
    )

    with _write_lock:
        registry = _read_registry(outputs_dir)
        existing = registry["artifacts"].get(artifact_id)
        now = _utcnow_iso()

        if existing and existing.get("content_hash") and content_hash:
            if str(existing["content_hash"]) != str(content_hash):
                raise RegistryError(
                    f"register_artifact: artifact {artifact_id!r} already has "
                    f"content_hash {existing['content_hash']!r} on file, which "
                    f"conflicts with this registration's {content_hash!r} -- "
                    "refusing to silently overwrite a conflicting hash "
                    "(this should not happen for a correctly-anchored id; "
                    "investigate before forcing an update)"
                )

        record = dict(existing) if existing else asdict(
            ArtifactRecord(
                artifact_id=artifact_id, kind=str(kind).strip(),
                created_at=now, updated_at=now,
            )
        )
        record["artifact_id"] = artifact_id
        record["kind"] = str(kind).strip()
        record["updated_at"] = now
        record.setdefault("created_at", now)
        if content_hash:
            record["content_hash"] = content_hash
            record["hash_verified"] = bool(computed_hash)
        if generator:
            record["generator"] = generator
        if run_id:
            record["run_id"] = run_id
        if source_locator:
            record["source_locator"] = source_locator
        if role:
            record["role"] = role
        record["lifecycle_state"] = lifecycle_state
        if metadata:
            merged = dict(record.get("metadata") or {})
            merged.update(metadata)
            record["metadata"] = merged
        record.setdefault("local_paths", [])
        record.setdefault("lifecycle_history", [])

        if canonical_path:
            key = _normalize_path_key(canonical_path)
            sighting_host = host or platform.node() or "unknown-host"
            found = False
            for entry in record["local_paths"]:
                if _normalize_path_key(entry.get("local_only_path")) == key:
                    entry["last_seen"] = now
                    entry["host"] = sighting_host
                    found = True
                    break
            if not found:
                record["local_paths"].append({
                    "local_only_path": canonical_path,
                    "basename": os.path.basename(str(canonical_path).replace("\\", "/")),
                    "host": sighting_host,
                    "first_seen": now,
                    "last_seen": now,
                })

        registry["artifacts"][artifact_id] = record
        _write_registry(outputs_dir, registry)

    return {**record, "created": existing is None}


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def get_artifact(outputs_dir: str, artifact_id: str) -> dict[str, Any] | None:
    """Direct, exact lookup by public id. ``None`` if never registered."""
    if not artifact_id:
        return None
    registry = _read_registry(outputs_dir)
    rec = registry["artifacts"].get(artifact_id)
    return dict(rec) if rec is not None else None


def list_artifacts(
    outputs_dir: str,
    *,
    kind: str | None = None,
    lifecycle_state: str | None = None,
) -> list[dict[str, Any]]:
    """All registered artifacts, optionally filtered, sorted by artifact_id
    for deterministic output."""
    registry = _read_registry(outputs_dir)
    rows = list(registry["artifacts"].values())
    if kind is not None:
        rows = [r for r in rows if r.get("kind") == kind]
    if lifecycle_state is not None:
        rows = [r for r in rows if r.get("lifecycle_state") == lifecycle_state]
    return sorted((dict(r) for r in rows), key=lambda r: r["artifact_id"])


def resolve_artifact(
    outputs_dir: str,
    *,
    artifact_id: str | None = None,
    canonical_path: str | None = None,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Resolve an artifact by public id or by local path, with an EXPLICIT
    outcome -- never a silent basename/fuzzy guess.

    Exactly one of ``artifact_id`` or ``canonical_path`` should normally be
    given. Precedence when both are given: ``artifact_id`` (direct lookup),
    with ``canonical_path``/``expected_sha256`` then used only to verify the
    resolved record, never to override which record is returned.

    Returns:
      ``{"status": ..., "artifact_id": <str|None>, "record": <dict|None>,
      "evidence": <str|None>, "candidates": <list[str]>, "reason": <str>}``
      where ``status`` is one of:

        - ``"resolved"``       -- exactly one confident match. ``record`` is
          populated. ``evidence`` is ``"artifact_id"`` (direct lookup),
          ``"content_hash"`` (matched by content hash -- the strongest
          path-based evidence), or ``"local_path_exact"`` (matched by an
          exact, previously-recorded local path sighting -- weaker than a
          hash match, never a basename/fuzzy match).
        - ``"hash_mismatch"``  -- the record was found (by id or by path/hash
          lookup) but ``expected_sha256`` contradicts its recorded
          ``content_hash``.
        - ``"ambiguous"``      -- MORE THAN ONE distinct artifact_id matches
          the given evidence (e.g. two different registered artifacts happen
          to share the same content_hash, or the same local path was
          sighted under two different ids). ``candidates`` lists every
          matching artifact_id; ``record`` is ``None`` -- this function
          never silently narrows an ambiguous match down to "the first one".
        - ``"orphaned"``       -- an explicit ``artifact_id`` was given and
          nothing in the registry matches it at all.
        - ``"unresolved"``     -- neither ``artifact_id`` nor
          ``canonical_path`` was given, OR a ``canonical_path`` was given
          but nothing in the registry (by hash or by exact local-path
          sighting) references it.

      Never raises: an unreadable/missing ``canonical_path`` just means no
      ``content_hash`` evidence is available, falling through to the
      local-path-sighting tier.
    """
    if artifact_id:
        record = get_artifact(outputs_dir, artifact_id)
        if record is None:
            return {
                "status": ORPHANED, "artifact_id": artifact_id, "record": None,
                "evidence": None, "candidates": [],
                "reason": f"no registered artifact with id {artifact_id!r}",
            }
        if expected_sha256 and record.get("content_hash") and \
                str(expected_sha256) != str(record["content_hash"]):
            return {
                "status": HASH_MISMATCH, "artifact_id": artifact_id, "record": record,
                "evidence": "artifact_id", "candidates": [artifact_id],
                "reason": (
                    f"expected_sha256 {expected_sha256!r} does not match "
                    f"registered content_hash {record['content_hash']!r} for "
                    f"{artifact_id!r}"
                ),
            }
        return {
            "status": RESOLVED, "artifact_id": artifact_id, "record": record,
            "evidence": "artifact_id", "candidates": [artifact_id], "reason": None,
        }

    if not canonical_path or not str(canonical_path).strip():
        return {
            "status": UNRESOLVED, "artifact_id": None, "record": None,
            "evidence": None, "candidates": [],
            "reason": "neither artifact_id nor canonical_path was given",
        }

    registry = _read_registry(outputs_dir)
    rows: list[dict[str, Any]] = list(registry["artifacts"].values())

    computed_hash: str | None = None
    if os.path.isfile(canonical_path):
        computed_hash = fingerprint.script_content_hash(canonical_path)
    target_hash = computed_hash or expected_sha256

    if target_hash:
        hash_matches = [r for r in rows if r.get("content_hash") == target_hash]
        if len(hash_matches) == 1:
            record = hash_matches[0]
            if expected_sha256 and str(expected_sha256) != str(record.get("content_hash")):
                return {
                    "status": HASH_MISMATCH, "artifact_id": record["artifact_id"],
                    "record": record, "evidence": "content_hash",
                    "candidates": [record["artifact_id"]],
                    "reason": "expected_sha256 does not match the matched record",
                }
            return {
                "status": RESOLVED, "artifact_id": record["artifact_id"],
                "record": record, "evidence": "content_hash",
                "candidates": [record["artifact_id"]], "reason": None,
            }
        if len(hash_matches) > 1:
            ids = sorted(r["artifact_id"] for r in hash_matches)
            return {
                "status": AMBIGUOUS, "artifact_id": None, "record": None,
                "evidence": "content_hash", "candidates": ids,
                "reason": (
                    f"{len(ids)} distinct registered artifacts share content "
                    f"hash {target_hash!r} -- cannot resolve to a single "
                    "identity without more evidence"
                ),
            }

    key = _normalize_path_key(canonical_path)
    path_matches = [
        r for r in rows
        if any(_normalize_path_key(e.get("local_only_path")) == key for e in r.get("local_paths", []))
    ]
    if len(path_matches) == 1:
        record = path_matches[0]
        if expected_sha256 and record.get("content_hash") and \
                str(expected_sha256) != str(record["content_hash"]):
            return {
                "status": HASH_MISMATCH, "artifact_id": record["artifact_id"],
                "record": record, "evidence": "local_path_exact",
                "candidates": [record["artifact_id"]],
                "reason": "expected_sha256 does not match the matched record",
            }
        return {
            "status": RESOLVED, "artifact_id": record["artifact_id"],
            "record": record, "evidence": "local_path_exact",
            "candidates": [record["artifact_id"]], "reason": None,
        }
    if len(path_matches) > 1:
        ids = sorted(r["artifact_id"] for r in path_matches)
        return {
            "status": AMBIGUOUS, "artifact_id": None, "record": None,
            "evidence": "local_path_exact", "candidates": ids,
            "reason": (
                f"{len(ids)} distinct registered artifacts were previously "
                f"sighted at the exact same local path -- cannot resolve to "
                "a single identity without more evidence"
            ),
        }

    return {
        "status": UNRESOLVED, "artifact_id": None, "record": None,
        "evidence": None, "candidates": [],
        "reason": (
            f"no registered artifact matches {canonical_path!r} by content "
            "hash or by exact prior local-path sighting -- never guessed by "
            "basename"
        ),
    }


def verify_artifact_hash(
    outputs_dir: str, artifact_id: str, path: str | None = None,
) -> dict[str, Any]:
    """Recompute an artifact's content hash from disk and compare it to what
    is on file, on demand.

    Args:
      outputs_dir:  Absolute path to the outputs directory.
      artifact_id:  The registered artifact to verify.
      path:         Explicit path to hash. Defaults to the most-recently-seen
                    entry in the record's own ``local_paths`` (last_seen,
                    latest first) when omitted.

    Returns:
      ``{"artifact_id", "verified": bool, "current_hash", "registered_hash",
      "path", "reason"}``. ``verified`` is ``True`` only when both hashes are
      present and equal -- a missing registered hash or a missing/unreadable
      path never gets silently reported as "verified".
    """
    record = get_artifact(outputs_dir, artifact_id)
    if record is None:
        return {
            "artifact_id": artifact_id, "verified": False, "current_hash": None,
            "registered_hash": None, "path": path,
            "reason": f"no registered artifact with id {artifact_id!r}",
        }

    if not path:
        sightings = sorted(
            record.get("local_paths", []),
            key=lambda e: e.get("last_seen") or "", reverse=True,
        )
        path = sightings[0]["local_only_path"] if sightings else None

    registered_hash = record.get("content_hash")
    if not path:
        return {
            "artifact_id": artifact_id, "verified": False, "current_hash": None,
            "registered_hash": registered_hash, "path": None,
            "reason": "no on-disk path available to verify against (none "
                      "given, and no local_paths sighting on record)",
        }

    current_hash = fingerprint.script_content_hash(path)
    if current_hash is None:
        return {
            "artifact_id": artifact_id, "verified": False, "current_hash": None,
            "registered_hash": registered_hash, "path": path,
            "reason": f"{path!r} could not be read to compute its hash",
        }
    if not registered_hash:
        return {
            "artifact_id": artifact_id, "verified": False, "current_hash": current_hash,
            "registered_hash": None, "path": path,
            "reason": "this artifact has no hash on file to verify against",
        }

    verified = str(current_hash) == str(registered_hash)
    return {
        "artifact_id": artifact_id, "verified": verified, "current_hash": current_hash,
        "registered_hash": registered_hash, "path": path,
        "reason": None if verified else "current on-disk content hash does not match the registered hash",
    }


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def set_lifecycle_state(
    outputs_dir: str, artifact_id: str, lifecycle_state: str, *, reason: str | None = None,
) -> dict[str, Any]:
    """Explicit lifecycle-state transition for a registered artifact.

    Raises:
      RegistryError: ``lifecycle_state`` is not one of :data:`LIFECYCLE_STATES`,
        or ``artifact_id`` was never registered.
    """
    if lifecycle_state not in LIFECYCLE_STATES:
        raise RegistryError(
            f"set_lifecycle_state: lifecycle_state must be one of "
            f"{LIFECYCLE_STATES}, got {lifecycle_state!r}"
        )
    with _write_lock:
        registry = _read_registry(outputs_dir)
        record = registry["artifacts"].get(artifact_id)
        if record is None:
            raise RegistryError(
                f"set_lifecycle_state: no registered artifact with id {artifact_id!r}"
            )
        now = _utcnow_iso()
        previous = record.get("lifecycle_state")
        record["lifecycle_state"] = lifecycle_state
        record["updated_at"] = now
        record.setdefault("lifecycle_history", []).append({
            "from": previous, "to": lifecycle_state, "at": now, "reason": reason,
        })
        registry["artifacts"][artifact_id] = record
        _write_registry(outputs_dir, registry)
    return dict(record)


# ---------------------------------------------------------------------------
# Source <-> artifact edges
# ---------------------------------------------------------------------------

def bind_source_edge(
    outputs_dir: str,
    artifact_id: str,
    source_locator: str,
    *,
    relation: str = "produced_by",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record a typed edge between a registered artifact and a source
    locator. Idempotent: calling again with the same
    ``(artifact_id, source_locator, relation)`` updates the existing edge's
    ``metadata`` rather than creating a duplicate.

    Raises:
      RegistryError: ``artifact_id`` was never registered, or
        ``source_locator``/``relation`` is empty.
    """
    if not source_locator or not str(source_locator).strip():
        raise RegistryError("bind_source_edge: source_locator is required")
    if not relation or not str(relation).strip():
        raise RegistryError("bind_source_edge: relation is required")

    with _write_lock:
        registry = _read_registry(outputs_dir)
        if artifact_id not in registry["artifacts"]:
            raise RegistryError(
                f"bind_source_edge: cannot bind an edge to unregistered "
                f"artifact {artifact_id!r} -- register it first"
            )
        now = _utcnow_iso()
        for edge in registry["edges"]:
            if (edge.get("artifact_id") == artifact_id
                    and edge.get("source_locator") == source_locator
                    and edge.get("relation") == relation):
                if metadata:
                    merged = dict(edge.get("metadata") or {})
                    merged.update(metadata)
                    edge["metadata"] = merged
                _write_registry(outputs_dir, registry)
                return dict(edge)

        edge = SourceEdge(
            edge_id=str(uuid.uuid4()),
            artifact_id=artifact_id,
            source_locator=source_locator,
            relation=relation,
            created_at=now,
            metadata=dict(metadata or {}),
        ).to_dict()
        registry["edges"].append(edge)
        _write_registry(outputs_dir, registry)
    return edge


def get_artifact_sources(outputs_dir: str, artifact_id: str) -> list[dict[str, Any]]:
    """Artifact -> its sources. Sorted by ``source_locator`` for determinism."""
    registry = _read_registry(outputs_dir)
    rows = [e for e in registry["edges"] if e.get("artifact_id") == artifact_id]
    return sorted((dict(e) for e in rows), key=lambda e: (e["source_locator"], e["relation"]))


def get_source_artifacts(outputs_dir: str, source_locator: str) -> list[dict[str, Any]]:
    """Source -> the artifacts it produced. Sorted by ``artifact_id`` for
    determinism."""
    registry = _read_registry(outputs_dir)
    rows = [e for e in registry["edges"] if e.get("source_locator") == source_locator]
    return sorted((dict(e) for e in rows), key=lambda e: (e["artifact_id"], e["relation"]))


# ---------------------------------------------------------------------------
# Migration / reconciliation for legacy outputs
# ---------------------------------------------------------------------------

def _legacy_entries_from_provenance_ledger(outputs_dir: str) -> list[dict[str, Any]]:
    """Default source of "legacy" entries: everything already known to
    :mod:`annotate`'s provenance ledger (``record_provenance``), which very
    commonly predates this registry existing at all -- exactly the "legacy
    outputs" this reconciliation exists to migrate."""
    entries: list[dict[str, Any]] = []
    for rec in annotate.list_provenance(outputs_dir):
        entries.append({
            "kind": "output",
            "canonical_path": rec.get("path"),
            "expected_sha256": rec.get("content_hash"),
            "generator": rec.get("generating_script"),
            "source_locator": rec.get("sprint_item_id") or rec.get("decision_id"),
        })
    return entries


def reconcile_legacy_outputs(
    outputs_dir: str,
    legacy_entries: list[dict[str, Any]] | None = None,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Migration/reconciliation report: register (or preview registering)
    every legacy entry that isn't already a resolved artifact.

    Args:
      outputs_dir:      Absolute path to the outputs directory.
      legacy_entries:   Explicit list of ``{"kind", "canonical_path",
                        "expected_sha256", "generator", "source_locator",
                        "role"}`` dicts to reconcile. When omitted, defaults
                        to every path already known to
                        :func:`annotate.list_provenance` -- the common "this
                        predates the registry" case.
      dry_run:          When ``True`` (default), nothing is written: entries
                        that would be newly registered are reported under
                        ``would_register`` with their WOULD-BE artifact_id,
                        computed the same way :func:`register_artifact` would
                        without touching the ledger. When ``False``, actually
                        calls :func:`register_artifact` for each such entry
                        and reports the result under ``registered``.

    Returns:
      ``{"outputs_dir", "dry_run", "scanned", "already_registered": [ids],
      "registered"/"would_register": [ids], "ambiguous": [entries],
      "errors": [{"entry", "reason"}], "skipped_unanchored": [entries]}``.
      An entry that :func:`resolve_artifact` finds AMBIGUOUS is reported
      under ``ambiguous`` and is never registered (dry_run or not) -- fixing
      an ambiguous legacy identity is a human/caller decision, not something
      this reconciliation silently resolves by picking one. An entry with no
      portable identity signal at all (see
      :func:`compute_artifact_id`) is reported under
      ``skipped_unanchored``, never registered, and never raises the
      reconciliation itself.
    """
    if legacy_entries is None:
        legacy_entries = _legacy_entries_from_provenance_ledger(outputs_dir)

    already_registered: list[str] = []
    registered_or_would: list[str] = []
    ambiguous: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    skipped_unanchored: list[dict[str, Any]] = []

    for entry in legacy_entries:
        canonical_path = entry.get("canonical_path")
        kind = entry.get("kind") or "output"
        expected_sha256 = entry.get("expected_sha256")
        generator = entry.get("generator")
        source_locator = entry.get("source_locator")

        existing = resolve_artifact(
            outputs_dir, canonical_path=canonical_path, expected_sha256=expected_sha256,
        )
        if existing["status"] == RESOLVED:
            already_registered.append(existing["artifact_id"])
            continue
        if existing["status"] == AMBIGUOUS:
            ambiguous.append({"entry": entry, "candidates": existing["candidates"]})
            continue

        content_hash = expected_sha256
        if canonical_path and os.path.isfile(canonical_path):
            content_hash = fingerprint.script_content_hash(canonical_path) or expected_sha256
        try:
            would_be_id = compute_artifact_id(
                kind, content_hash=content_hash, generator=generator,
                source_locator=source_locator,
            )
        except RegistryError:
            skipped_unanchored.append(entry)
            continue

        if dry_run:
            registered_or_would.append(would_be_id)
            continue

        try:
            result = register_artifact(
                outputs_dir, kind, canonical_path=canonical_path,
                expected_sha256=expected_sha256, generator=generator,
                source_locator=source_locator, role=entry.get("role"),
            )
            registered_or_would.append(result["artifact_id"])
        except RegistryError as exc:
            errors.append({"entry": entry, "reason": str(exc)})

    result_key = "would_register" if dry_run else "registered"
    return {
        "outputs_dir": outputs_dir,
        "dry_run": dry_run,
        "scanned": len(legacy_entries),
        "already_registered": sorted(set(already_registered)),
        result_key: sorted(set(registered_or_would)),
        "ambiguous": ambiguous,
        "errors": errors,
        "skipped_unanchored": skipped_unanchored,
    }
