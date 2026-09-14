"""37dd1004 (W1-N) — thin Tigris/S3 object-storage adapter with graceful
degradation, plus the oversized-payload "spill" path used by
:mod:`meridian.db.experiments`.

SCOPE
-----
This module does NOT reimplement object storage. It is a thin caller-facing
wrapper around two things that already exist:

* :class:`meridian.object_store.TigrisObjectStoreBackend` — the
  inactive-by-default ``NotImplementedError`` stub (see its own docstring).
  This module is the ONLY place besides ``object_store.py`` itself and its
  test suite that imports it — every attempt to construct or use it is
  wrapped so a caller of THIS module can never observe the
  ``NotImplementedError`` directly. That is the whole point: per decision
  bad077b9 ("Keep Tigris/S3 inactive until adapter and live contract gate
  pass"), Tigris stays genuinely inactive in this checkout today, but a
  caller of :func:`spill_oversized_payload` never has to know that — it
  just gets a durable, retrievable spill record either way.
* :mod:`meridian.artifact_store` — the existing local, content-addressed
  filesystem store (also what ``object_store.LocalObjectStoreBackend``
  wraps). This is the local fallback backend, and — because Tigris remains
  inactive by design — the ONLY backend actually reachable in this
  checkout today.

GRACEFUL DEGRADATION (the item's own explicit requirement)
--------------------------------------------------------------------------
:func:`spill_oversized_payload` and :func:`fetch_spilled_payload` NEVER
raise for a storage-backend problem — an unconfigured/not-yet-implemented
Tigris backend, a network failure, or (in the local fallback) a disk
error all degrade to a returned status dict the caller inspects
(``{"spilled": False, "error": ...}``) rather than an exception propagating
up through :mod:`meridian.db.experiments`. The only things that raise here
are genuine programmer errors (a non-bytes payload) or a real security
rejection (:func:`meridian.secret_redaction.check_for_secrets` — deferred
to callers that already do their own secret-shaped-content rejection, e.g.
``meridian.db.experiments.complete_experiment_run``, which calls it BEFORE
handing bytes to this module; see that module for why the hard-reject
posture is deliberately preserved rather than silently downgraded to this
module's own soft, best-effort redaction path via ``artifact_store``).

ACTIVATION IS AUTOMATIC, NOT SOMETHING THIS ITEM PERFORMS
--------------------------------------------------------------------------
:func:`_try_construct_tigris_backend` is the ONLY place this module ever
imports/instantiates ``TigrisObjectStoreBackend``. A future item that
actually implements that class (per the activation checklist in
``docs/object-storage-backend.md``) makes ``spill_oversized_payload`` start
genuinely writing to Tigris the moment ``TIGRIS_ENABLED`` is set — with ZERO
changes needed here or in any caller. This mirrors
``object_store.get_default_backend``'s own stated design goal ("this
function exists so future opt-in wiring has one obvious place to change,
without touching every call site").
"""
from __future__ import annotations

import logging
import os
from typing import Any

from meridian import artifact_store

logger = logging.getLogger("meridian.tigris_adapter")

_TRUTHY = {"1", "true", "yes", "on"}


def is_tigris_enabled() -> bool:
    """True only when ``TIGRIS_ENABLED`` is explicitly set to a truthy
    value. Reading this flag NEVER reads any credential env var
    (``AWS_ACCESS_KEY_ID`` etc.) — matches
    ``object_store.TigrisObjectStoreBackend``'s own documented discipline
    that even config-reading is left to a real future implementation."""
    return os.environ.get("TIGRIS_ENABLED", "").strip().lower() in _TRUTHY


def _try_construct_tigris_backend() -> "Any | None":
    """Attempt to construct the Tigris backend. Returns ``None`` on ANY
    failure — import error, ``NotImplementedError`` (today's permanent
    state per decision bad077b9), or anything else — never raises. Skips
    the attempt entirely (returns ``None`` immediately) when
    :func:`is_tigris_enabled` is false, so a self-hosted install with no
    Tigris intent never even imports ``object_store`` for this purpose."""
    if not is_tigris_enabled():
        return None
    try:
        from meridian.object_store import TigrisObjectStoreBackend  # noqa: PLC0415

        return TigrisObjectStoreBackend()
    except Exception:  # noqa: BLE001 — graceful degradation, never crash the caller
        logger.info(
            "tigris_adapter: TIGRIS_ENABLED is set but the backend is not yet "
            "reachable (expected while TigrisObjectStoreBackend remains an "
            "inactive-by-default stub) -- degrading to local storage",
        )
        return None


def _tigris_key(project_id: str, digest_hex: str) -> str:
    """Namespaced key shape for the Tigris path only — mirrors
    ``object_store.build_object_key``'s ``{project_id}/{class}/{hh}/{digest}``
    SHAPE for operator-readability, without importing that function (and
    its ``ARTIFACT_CLASSES`` allow-list, which this module has no reason to
    extend just for its own private key format)."""
    return f"{project_id}/experiment_receipts/{digest_hex[:2]}/{digest_hex}"


async def spill_oversized_payload(
    data_dir: str,
    project_id: str,
    payload: bytes,
    *,
    content_type: "str | None" = None,
) -> "dict[str, Any]":
    """Durably store *payload* out-of-line and return a small pointer
    record describing where it landed — never the payload itself embedded
    back in the record, so a caller can safely persist the returned dict
    inline in place of the oversized original.

    Returns, on success::

        {"spilled": True, "backend": "tigris" | "local", "key": "...",
         "content_hash": "sha256:...", "size": N,
         "content_type": "...", "project_id": "..."}

    ``key`` is present only for the ``"tigris"`` backend (the local backend
    is addressed purely by ``content_hash``, matching
    ``artifact_store.py``'s own content-addressed design).

    On total failure (bad input, or every backend attempted raising),
    returns ``{"spilled": False, "error": "..."}`` — NEVER raises. Tries
    Tigris first (only reachable once :func:`is_tigris_enabled` is true AND
    the backend actually constructs — today, per decision bad077b9, it
    never does), then unconditionally falls back to the same local,
    content-addressed :mod:`meridian.artifact_store` storage
    ``LocalObjectStoreBackend`` already wraps, so a spill is durable and
    retrievable even with zero external dependencies configured.
    """
    if not isinstance(payload, (bytes, bytearray)):
        return {"spilled": False, "error": "payload must be bytes"}
    payload_bytes = bytes(payload)

    backend = _try_construct_tigris_backend()
    if backend is not None:
        digest_hash = artifact_store.content_hash(payload_bytes)
        digest_hex = digest_hash.split(":", 1)[1]
        key = _tigris_key(project_id, digest_hex)
        try:
            result = await backend.put(key, payload_bytes, content_type=content_type)
        except Exception:  # noqa: BLE001 — graceful degradation, fall through to local
            logger.warning(
                "tigris_adapter: Tigris put failed, falling back to local storage",
                exc_info=True,
            )
        else:
            return {
                "spilled": True,
                "backend": "tigris",
                "key": result.key,
                "content_hash": digest_hash,
                "size": result.size,
                "content_type": content_type,
                "project_id": project_id,
            }

    # Local fallback — also today's ONLY reachable path, by design (Tigris
    # remains inactive; see decision bad077b9 / object_store.py).
    try:
        meta = artifact_store.store_artifact(
            data_dir, project_id, payload_bytes, content_type=content_type,
        )
    except Exception as exc:  # noqa: BLE001 — graceful degradation, never crash the caller
        logger.warning("tigris_adapter: local spill fallback failed", exc_info=True)
        return {"spilled": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "spilled": True,
        "backend": "local",
        "content_hash": meta["content_hash"],
        "size": meta["size"],
        "content_type": meta.get("content_type"),
        "project_id": project_id,
    }


async def fetch_spilled_payload(
    data_dir: str, spill_record: "dict[str, Any]",
) -> "bytes | None":
    """Retrieve bytes previously stored via :func:`spill_oversized_payload`.

    Returns ``None`` — never raises — when the record is malformed, names
    an unrecognized backend, the content is missing, or (for a
    ``"tigris"`` record) Tigris is no longer reachable. A ``"tigris"``-
    backed spill can only ever be recovered while Tigris itself is
    reachable — there is no local copy to fall back to, since the whole
    point of spilling there is to keep the bytes OUT of local storage.
    """
    if not isinstance(spill_record, dict) or not spill_record.get("spilled"):
        return None
    backend_name = spill_record.get("backend")
    project_id = spill_record.get("project_id")
    content_hash_value = spill_record.get("content_hash")
    if not project_id or not content_hash_value:
        return None

    if backend_name == "tigris":
        backend = _try_construct_tigris_backend()
        if backend is None:
            return None
        key = spill_record.get("key")
        if not key:
            return None
        try:
            return await backend.get(key)
        except Exception:  # noqa: BLE001 — graceful degradation
            logger.warning("tigris_adapter: Tigris get failed", exc_info=True)
            return None

    if backend_name == "local":
        try:
            return artifact_store.get_artifact(data_dir, project_id, content_hash_value)
        except Exception:  # noqa: BLE001 — graceful degradation
            logger.warning("tigris_adapter: local fetch failed", exc_info=True)
            return None

    return None
