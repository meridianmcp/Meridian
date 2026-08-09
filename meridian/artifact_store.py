"""ea972129 (Round 1 proposal e143949d) — local-first, content-addressed
artifact persistence for the ai_log observability layer.

SCOPE
-----
A sibling to :mod:`meridian.ai_log` / :mod:`meridian.db.ai_log`'s
ExecutionEvent contract and storage scaffold (9e83be4a). Those modules
record small, structured EVENTS; this module records the (potentially
large) OPAQUE BLOBS an event's payload sometimes needs to point at instead
of embedding inline — a full tool-call result, an LLM request/response
body, a captured file diff. An event references an artifact by content
hash (e.g. ``payload = {"artifact_ref": "sha256:...", "artifact_size": N}``)
rather than the blob being inlined into ``ai_log_events.payload`` — this
keeps that table's rows small and bounded regardless of how large a
captured artifact is.

Deliberately OUT OF SCOPE for this item (mirrors 9e83be4a's own scope
discipline):
  * No capture/ingestion wiring. Nothing in the running server calls
    :func:`store_artifact` today; deciding WHAT gets captured as an
    artifact (vs. inlined, vs. never captured at all) is sibling item
    4d113dcb's job (capture-boundary mapping,
    tests/test_ai_log_capture_boundaries.py).
  * No Redis (or any other) read-acceleration layer — see the "REDIS READ
    ACCELERATION" note below for the design contract a future item must
    honor if it adds one.

c0168425 (implementation follow-up to this design item) — EXPORT
--------------------------------------------------------------------
:func:`export_artifacts` is this module's one addition beyond the design
above: a read-only, receipted, project-scoped bulk export (content +
metadata, base64-encoded for JSON-safety) — for a local-first backup or a
retention sweep's pre-purge archive. It never deletes anything
(:func:`purge_artifacts_before` remains the only deletion path) and adds no
new dependency (still zero-DB, filesystem-only). The MCP-facing surface
(``export_ai_log_artifacts`` / ``purge_ai_log``) is wired in
``meridian/mcp/handler.py``'s ``_handle_task_tools`` — see that function for
how this module's export is combined with ``db.ai_log``'s own event export
at the integration layer.

DESIGN DECISIONS
-----------------
* Local-first, content-addressed: every artifact is stored on the local
  filesystem under ``<data_dir>/ai_log_artifacts/<project_id>/<hash[:2]>/
  <hash>.bin``, keyed by ``sha256`` of the (post-redaction) content.
  Identical content — even referenced by many different events, or
  re-stored many times — is written to disk exactly once (dedup by
  construction, not a separate GC pass). ``data_dir`` is the same
  server-wide local data directory every other local artifact in this repo
  already uses (``MERIDIAN_DATA_DIR`` / ``server.DEFAULT_DATA_DIR`` — e.g.
  handoff markdown files already live under it), passed explicitly rather
  than read from a global, exactly like ``data_dir: str`` is threaded
  through ``server.py``'s own handoff/session functions.
* Metadata sidecar, not a DB table: each artifact's small bookkeeping
  record (``size``, ``content_type``, ``created_at``, ``redacted``) lives
  in a ``<hash>.meta.json`` file next to the content, not a new SQL table
  — this module has ZERO database dependency (no migration needed on
  either backend), staying true to "local-first": the filesystem alone is
  authoritative for both the bytes and their bookkeeping.
* Redaction on write: text-decodable content is scanned with
  :mod:`meridian.secret_redaction` (the same registry
  ``db.ai_log.append_event`` now gates on) BEFORE it is hashed and
  written, so the hash always matches what is actually stored —
  ``get_artifact(store_artifact(x)["content_hash"], ...)`` returns the
  exact bytes written, never the pre-redaction original. Binary content
  (fails UTF-8 decode) is stored as-is; scanning arbitrary binary for
  ASCII secret patterns is unreliable and out of scope here — a
  capture-boundary decision (sibling item 4d113dcb) about what may reach
  :func:`store_artifact` as binary in the first place is the real control
  for that case.
* Retention: :func:`purge_artifacts_before` — a project-scoped,
  cutoff-based bulk delete — is the ONLY deletion path, mirroring
  ``db.ai_log.purge_events_before``'s append-only-content /
  explicit-bulk-purge split. A stored artifact's bytes are never modified
  in place; deleting one always removes the whole file (+ its sidecar),
  never edits it.
* Crash-safe writes: every write (content AND sidecar) goes through a
  temp-file-then-``os.replace`` sequence (mirrors
  ``meridian.process_registry``'s persistence discipline) so a crash
  mid-write can never leave a partially-written artifact that a concurrent
  reader could observe. Content is always written BEFORE its sidecar, so
  "the sidecar exists" is a reliable proxy for "the content exists" when
  checking for a dedup hit — a crash between the two just means the next
  :func:`store_artifact` call for the same content re-writes both (safe,
  idempotent, self-healing). The reverse (a crash mid-:func:`delete_artifact`
  leaving an orphaned content file with no sidecar) is a deliberately
  accepted, harmless edge case — see that function's docstring.
* Path-safety: ``project_id`` and any caller-supplied ``content_hash`` are
  validated to contain only the characters this module itself ever
  generates (see :func:`_safe_component`) before being used to build a
  filesystem path — defense against path traversal from a malformed or
  adversarial id, even though every id in this codebase is normally
  server-generated.

REDIS READ ACCELERATION (design note, not implemented here)
-------------------------------------------------------------
Per Round 1 proposal e143949d, a future item MAY put a Redis cache in
front of :func:`get_artifact`, keyed by the same ``content_hash``, purely
as a read accelerator for hot artifacts. The invariant that makes this
safe: Redis must NEVER become the write path or the only copy of an
artifact. Every write still lands here (local disk) first and
synchronously; a Redis miss — cold cache, eviction, or Redis being down
entirely — always falls back to a local read that returns byte-identical
content, never a degraded or partial result. Losing Redis must be purely a
latency regression, never a durability or correctness one. This module
imports no Redis client and has no Redis dependency; it stays that way
until a sibling item explicitly opts in, and even then this module (not
Redis, not Langfuse) remains the system of record.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from meridian.secret_redaction import redact

#: Directory name under a project's data_dir where artifacts are stored.
_ARTIFACT_SUBDIR = "ai_log_artifacts"

#: project_id components must match this before being used to build a
#: filesystem path — defense against path traversal. Every project_id this
#: codebase actually generates is a UUID4 (hex digits + hyphens), which is
#: a strict subset of this.
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")

#: A well-formed content_hash as produced by content_hash() below.
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ArtifactStoreError(ValueError):
    """Raised for an invalid argument to this module's functions — a
    malformed project_id/content_hash, non-bytes content, path traversal
    attempt, or similar."""


def _safe_component(value: str, *, label: str) -> str:
    if not value or not isinstance(value, str) or not _SAFE_COMPONENT_RE.match(value):
        raise ArtifactStoreError(
            f"{label} {value!r} must be a non-empty string of letters, digits, "
            "'_' or '-' only"
        )
    return value


def _utc_now_iso() -> str:
    """Millisecond-precision UTC ISO-8601 with a literal 'Z' suffix — same
    shape as meridian.ai_log._utc_now_iso, so ``created_at`` values here and
    an event's ``occurred_at`` are lexicographically comparable."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def content_hash(data: bytes) -> str:
    """``sha256:<hex>`` over *data*, exactly as it will be stored."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _digest_of(content_hash_value: str) -> str:
    if not isinstance(content_hash_value, str) or not _HASH_RE.match(content_hash_value):
        raise ArtifactStoreError(
            f"content_hash {content_hash_value!r} must look like "
            "'sha256:<64 lowercase hex chars>'"
        )
    return content_hash_value.split(":", 1)[1]


def _project_dir(data_dir: str, project_id: str) -> Path:
    _safe_component(project_id, label="project_id")
    return Path(data_dir) / _ARTIFACT_SUBDIR / project_id


def _content_path(data_dir: str, project_id: str, digest: str) -> Path:
    return _project_dir(data_dir, project_id) / digest[:2] / f"{digest}.bin"


def _meta_path(data_dir: str, project_id: str, digest: str) -> Path:
    return _project_dir(data_dir, project_id) / digest[:2] / f"{digest}.meta.json"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write *data* to *path* via temp-file-then-``os.replace`` so a crash
    mid-write never leaves a partially-written file observable to a
    concurrent reader (mirrors ``meridian.process_registry``'s persistence
    helper)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}_", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
        except OSError:
            pass


def _read_meta(meta_path: Path) -> "dict[str, Any] | None":
    try:
        raw = meta_path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _redact_if_text(content: bytes) -> "tuple[bytes, bool]":
    """Best-effort redaction: text-decodable content is scanned + redacted
    via :func:`meridian.secret_redaction.redact`; anything that doesn't
    decode as UTF-8 is returned unchanged. Returns
    ``(bytes_to_store, was_redacted)``."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content, False
    redacted_text = redact(text)
    if redacted_text == text:
        return content, False
    return redacted_text.encode("utf-8"), True


def store_artifact(
    data_dir: str,
    project_id: str,
    content: bytes,
    *,
    content_type: "str | None" = None,
) -> dict[str, Any]:
    """Durably store *content* for *project_id*, content-addressed by the
    sha256 of its (post-redaction) bytes. Idempotent: re-storing
    byte-identical content is a no-op that returns the EXISTING artifact's
    metadata unchanged (first write wins for ``content_type``/
    ``created_at``) — mirrors ``db.ai_log.append_event``'s
    idempotency_key dedup discipline, except keyed on content itself
    rather than a caller-supplied key.

    Returns a metadata dict: ``content_hash``, ``project_id``, ``size``,
    ``content_type``, ``created_at``, ``redacted``.

    Raises :class:`ArtifactStoreError` if *content* is not bytes or
    *project_id* is not a safe path component (see module docstring).
    """
    if not isinstance(content, (bytes, bytearray)):
        raise ArtifactStoreError("content must be bytes")
    _safe_component(project_id, label="project_id")

    stored_bytes, was_redacted = _redact_if_text(bytes(content))
    digest_hash = content_hash(stored_bytes)
    digest = digest_hash.split(":", 1)[1]

    meta_path = _meta_path(data_dir, project_id, digest)
    existing = _read_meta(meta_path)
    if existing is not None:
        return existing

    content_path = _content_path(data_dir, project_id, digest)
    _atomic_write_bytes(content_path, stored_bytes)

    meta: dict[str, Any] = {
        "content_hash": digest_hash,
        "project_id": project_id,
        "size": len(stored_bytes),
        "content_type": content_type,
        "created_at": _utc_now_iso(),
        "redacted": was_redacted,
    }
    _atomic_write_bytes(meta_path, json.dumps(meta, sort_keys=True).encode("utf-8"))
    return meta


def get_artifact(
    data_dir: str, project_id: str, content_hash_value: str,
) -> "bytes | None":
    """Return the stored bytes for *content_hash_value*, or ``None`` if no
    such artifact exists for *project_id*."""
    digest = _digest_of(content_hash_value)
    path = _content_path(data_dir, project_id, digest)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def get_artifact_metadata(
    data_dir: str, project_id: str, content_hash_value: str,
) -> "dict[str, Any] | None":
    """Return the sidecar metadata for *content_hash_value*, or ``None`` if
    absent."""
    digest = _digest_of(content_hash_value)
    return _read_meta(_meta_path(data_dir, project_id, digest))


def delete_artifact(data_dir: str, project_id: str, content_hash_value: str) -> bool:
    """Delete one artifact's content + sidecar. Idempotent: returns
    ``False`` (no error) if it was already absent.

    Removes the sidecar BEFORE the content file — the reverse order of
    :func:`store_artifact` — so a crash mid-delete can only ever leave an
    orphaned content file with no sidecar (invisible to
    :func:`list_artifacts` / :func:`purge_artifacts_before`, and harmless
    disk usage — never a stale sidecar pointing at bytes that are already
    gone, which would be the more dangerous inconsistency for a reader).
    """
    digest = _digest_of(content_hash_value)
    content_path = _content_path(data_dir, project_id, digest)
    meta_path = _meta_path(data_dir, project_id, digest)
    existed = content_path.exists() or meta_path.exists()
    for p in (meta_path, content_path):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    return existed


def list_artifacts(data_dir: str, project_id: str) -> list[dict[str, Any]]:
    """List every artifact's metadata for *project_id* (unspecified order).
    Returns an empty list if the project has never stored an artifact."""
    base = _project_dir(data_dir, project_id)
    if not base.exists():
        return []
    out: list[dict[str, Any]] = []
    for meta_file in base.glob("*/*.meta.json"):
        meta = _read_meta(meta_file)
        if meta is not None:
            out.append(meta)
    return out


def purge_artifacts_before(data_dir: str, project_id: str, cutoff_iso: str) -> int:
    """Delete every artifact for *project_id* whose ``created_at`` is
    strictly before *cutoff_iso* (an ISO-8601 UTC string, lexicographically
    comparable — see :func:`_utc_now_iso`). Returns the number of artifacts
    deleted.

    The ONLY bulk deletion path this module exposes — mirrors
    ``db.ai_log.purge_events_before``'s project-scoped, cutoff-based
    retention discipline. Always project-scoped (never a cross-project
    sweep in one call).
    """
    if not cutoff_iso:
        raise ArtifactStoreError("cutoff_iso is required")
    deleted = 0
    for meta in list_artifacts(data_dir, project_id):
        created_at = meta.get("created_at") or ""
        if created_at < cutoff_iso:
            if delete_artifact(data_dir, project_id, meta["content_hash"]):
                deleted += 1
    return deleted


def export_artifacts(
    data_dir: str,
    project_id: str,
    *,
    content_hashes: "list[str] | None" = None,
) -> dict[str, Any]:
    """Project-scoped, receipted bulk export of stored artifacts — content
    AND metadata, content base64-encoded for JSON-safety — for a
    local-first backup or a retention sweep's pre-purge archive. Read-only:
    never deletes anything (:func:`purge_artifacts_before` remains the only
    deletion path).

    *content_hashes*: an explicit subset of ``sha256:...`` values to
    export. Omit (``None``) to export EVERY artifact currently stored for
    *project_id* — mirrors :func:`list_artifacts`/
    :func:`purge_artifacts_before`'s "no filter = whole project" convention.
    Raises :class:`ArtifactStoreError` if an explicitly requested hash has
    no stored artifact for this project — an explicit request is
    all-or-nothing, never a silent partial result (a caller who didn't ask
    for "everything currently stored" gets an error, not a shorter list, if
    something it named is missing).

    Returns a receipted bundle: ``{project_id, exported_at, artifact_count,
    total_size, artifacts, export_hash}``. Each entry in ``artifacts`` is
    the stored sidecar metadata plus ``content_base64``. ``export_hash`` is
    a ``sha256:<hex>`` over the canonical (sorted-key) JSON of the
    artifacts list with EACH entry's ``content_base64`` excluded —
    deliberately: hashing the bookkeeping fields alone keeps ``export_hash``
    cheap to recompute/verify without re-encoding every blob, and each
    artifact's own ``content_hash`` is already itself a cryptographic
    commitment to its bytes, so nothing is lost by excluding the base64
    payload from the outer hash.
    """
    _safe_component(project_id, label="project_id")
    if content_hashes is not None:
        selected_metas: list[dict[str, Any]] = []
        for content_hash_value in content_hashes:
            meta = get_artifact_metadata(data_dir, project_id, content_hash_value)
            if meta is None:
                raise ArtifactStoreError(
                    f"content_hash {content_hash_value!r} has no stored artifact "
                    f"for project_id {project_id!r}"
                )
            selected_metas.append(meta)
    else:
        selected_metas = list_artifacts(data_dir, project_id)

    exported: list[dict[str, Any]] = []
    total_size = 0
    for meta in selected_metas:
        content = get_artifact(data_dir, project_id, meta["content_hash"])
        if content is None:
            # Sidecar existed (found via list_artifacts/get_artifact_metadata
            # above) but the content file is gone. store_artifact always
            # writes content BEFORE its sidecar (see module docstring), so
            # this specific ordering never happens from a normal write; it
            # can only mean a concurrent delete_artifact raced this export
            # between the metadata read and the content read. Skip this one
            # artifact rather than failing the whole export over a benign
            # concurrent-delete race.
            continue
        exported.append({
            **meta,
            "content_base64": base64.b64encode(content).decode("ascii"),
        })
        total_size += meta.get("size") or 0

    hashable = [
        {k: v for k, v in artifact.items() if k != "content_base64"}
        for artifact in exported
    ]
    canonical = json.dumps(hashable, sort_keys=True, separators=(",", ":"), default=str)
    export_hash = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    return {
        "project_id": project_id,
        "exported_at": _utc_now_iso(),
        "artifact_count": len(exported),
        "total_size": total_size,
        "artifacts": exported,
        "export_hash": export_hash,
    }
