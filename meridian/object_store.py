"""1d34c076 (build milestone; investigation 549e66c6) — backend-agnostic
object-storage adapter for large, already-content-addressed artifacts.

SCOPE
-----
This module defines the provider-neutral :class:`ObjectStoreBackend`
Protocol and its error taxonomy, plus TWO concrete backends that ship
today:

* :class:`LocalObjectStoreBackend` — a thin adapter over the EXISTING
  :mod:`meridian.artifact_store` functions. Zero new dependency, zero
  credentials, and IS today's behavior (local filesystem,
  project-scoped, content-addressed) under the new interface. This is
  the DEFAULT and AUTHORITATIVE backend — every artifact still lands on
  local disk first, synchronously, regardless of whether a remote
  backend is ever configured.
* :class:`FakeS3Backend` — an in-memory, S3-shaped test double. NOT a
  real network client (no sockets, no dependency). Exists purely so the
  full upload/download/head/hash-mismatch/retry/offline-fallback/
  tenant-isolation/idempotency contract can be exercised in CI without
  ever touching a real bucket.

:class:`TigrisObjectStoreBackend` is an intentionally NOT-implemented
stub (see its own docstring) — Tigris/S3 is inactive by default and
stays that way until a human completes the activation checklist in
``docs/object-storage-backend.md``.

WHAT THIS MODULE DOES NOT DO
-----------------------------
* It does not change what gets captured as an artifact, does not wire
  any capture/ingestion path, and does not alter default routing —
  :mod:`meridian.artifact_store` remains the only thing any existing
  caller touches unless a caller explicitly opts into
  :class:`LocalObjectStoreBackend` or (once implemented) a remote
  backend.
* It does not add ``aioboto3``/``boto3``/any S3 client dependency to
  ``pyproject.toml``/``pixi.toml``. That requires an explicit
  supply-chain review this sprint item does not authorize (see
  investigation 549e66c6 §5 and the activation checklist in
  ``docs/object-storage-backend.md``).
* It does not create a real bucket, touch ``.env``/``meridian.toml``, or
  read/write live credentials. Real-provider env-var NAMES are documented
  (not read) in ``docs/object-storage-backend.md``.

KEY NAMESPACE
-------------
Mirrors ``artifact_store.py``'s existing ``_project_dir`` layout
(investigation §4): ``{project_id}/{artifact_class}/{hash[:2]}/{sha256_hex}``,
optionally prefixed with ``tenants/{tenant_id}/`` for hosted multi-tenant
callers. ``artifact_class`` is always a fixed, code-controlled string —
see :data:`ARTIFACT_CLASSES` — never caller-supplied free text, so a key
can never be built from untrusted input. This reuses
``artifact_store._safe_component``'s exact discipline via
:func:`_safe_component` below.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from meridian import artifact_store

#: project_id / tenant_id / artifact_class components must match this
#: before being used to build a key or path — defense against path
#: traversal / injection from a malformed or adversarial id. Mirrors
#: artifact_store._SAFE_COMPONENT_RE exactly.
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")

#: A well-formed content_hash as produced by artifact_store.content_hash().
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Fixed, code-controlled artifact classes. A caller selects one of these
#: by name; nothing ever builds a class name from free-form caller input.
#: See investigation 549e66c6 §4 "Key namespace".
ARTIFACT_CLASSES: frozenset[str] = frozenset({
    "ai_log_artifact",
    "export_bundle",
    "docx_media",
    "evidence_bundle",
    "provenance_envelope",
    "research_report",
})


# ---------------------------------------------------------------------------
# Error taxonomy — investigation 549e66c6 §7 (S3/Tigris status mapping)
# ---------------------------------------------------------------------------

class ObjectStoreError(Exception):
    """Base class for every object-store backend error."""


class ObjectNotFoundError(ObjectStoreError):
    """Key does not exist (mirrors S3 404 ``NoSuchKey``)."""


class InvalidKeyError(ObjectStoreError):
    """Bad path/namespace component — mirrors
    ``artifact_store.ArtifactStoreError``. Raised locally, before any
    network call."""


class PreconditionFailedError(ObjectStoreError):
    """``If-Match``/``If-None-Match`` violated (S3 412). NOT retryable
    as-is — the caller must re-fetch state before retrying."""


class ConditionalRequestConflictError(ObjectStoreError):
    """Concurrent write race during upload (S3 409
    ``ConditionalRequestConflict``). Retryable per AWS's own documented
    guidance (investigation §2)."""


class ObjectStoreAuthError(ObjectStoreError):
    """401/403 — a configuration problem, not transient. Maps to sync
    state ``unavailable``."""


class ObjectStoreUnavailableError(ObjectStoreError):
    """Network/DNS/timeout/5xx — transient. Maps to sync state
    ``sync_failed`` (or ``unavailable`` if persistent)."""


class QuotaExceededError(ObjectStoreError):
    """Storage quota/limit exceeded."""


# ---------------------------------------------------------------------------
# Shared value types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ObjectMetadata:
    key: str
    size: int
    etag: str
    content_type: "str | None"
    content_hash: "str | None"  # sha256:... — set when the key is content-addressed
    created_at: str
    last_modified: str
    custom_metadata: "dict[str, str]" = field(default_factory=dict)


@dataclass(frozen=True)
class PutResult:
    key: str
    etag: str
    size: int
    version_id: "str | None" = None


@dataclass(frozen=True)
class ListPage:
    keys: "list[ObjectMetadata]"
    next_cursor: "str | None" = None


def _safe_component(value: str, *, label: str) -> str:
    if not value or not isinstance(value, str) or not _SAFE_COMPONENT_RE.match(value):
        raise InvalidKeyError(
            f"{label} {value!r} must be a non-empty string of letters, digits, "
            "'_' or '-' only"
        )
    return value


def _safe_artifact_class(artifact_class: str) -> str:
    if artifact_class not in ARTIFACT_CLASSES:
        raise InvalidKeyError(
            f"artifact_class {artifact_class!r} is not a recognized class. "
            f"Valid: {sorted(ARTIFACT_CLASSES)}"
        )
    return artifact_class


def _digest_of(content_hash_value: str) -> str:
    if not isinstance(content_hash_value, str) or not _HASH_RE.match(content_hash_value):
        raise InvalidKeyError(
            f"content_hash {content_hash_value!r} must look like "
            "'sha256:<64 lowercase hex chars>'"
        )
    return content_hash_value.split(":", 1)[1]


def build_object_key(
    project_id: str,
    artifact_class: str,
    content_hash_value: str,
    *,
    tenant_id: "str | None" = None,
) -> str:
    """Construct a namespaced object key.

    ``{project_id}/{artifact_class}/{hash[:2]}/{sha256_hex}`` — or, for a
    hosted multi-tenant caller, ``tenants/{tenant_id}/`` prefixed onto
    that (investigation 549e66c6 §4). Every component is validated before
    being used to build the key: this is the ONLY function in this module
    that builds a key from parts, and it never accepts a raw/pre-built
    key from an external caller — the same cross-tenant read protection
    ``artifact_store.get_artifact`` already relies on today.
    """
    _safe_component(project_id, label="project_id")
    _safe_artifact_class(artifact_class)
    digest = _digest_of(content_hash_value)
    suffix = f"{project_id}/{artifact_class}/{digest[:2]}/{digest}"
    if tenant_id is not None:
        _safe_component(tenant_id, label="tenant_id")
        return f"tenants/{tenant_id}/{suffix}"
    return suffix


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class ObjectStoreBackend(Protocol):
    """Minimal surface every backend (local passthrough, fake, Tigris/S3)
    must satisfy. Deliberately small — put/get/head/delete/list plus
    exists — mirrors ``artifact_store.py``'s function shape but as a
    Protocol so callers are backend-agnostic and unit-testable against a
    fake."""

    async def put(
        self, key: str, data: bytes, *,
        content_type: "str | None" = None,
        metadata: "dict[str, str] | None" = None,
        if_none_match: "str | None" = None,  # "*" => create-only
        if_match: "str | None" = None,       # existing ETag => update-only
    ) -> PutResult: ...

    async def get(self, key: str) -> bytes: ...            # raises ObjectNotFoundError
    async def head(self, key: str) -> ObjectMetadata: ...  # raises ObjectNotFoundError
    async def delete(self, key: str) -> bool: ...           # idempotent; True iff it existed
    async def exists(self, key: str) -> bool: ...
    async def list(
        self, prefix: str, *, limit: int = 1000, cursor: "str | None" = None,
    ) -> ListPage: ...


# ---------------------------------------------------------------------------
# LocalObjectStoreBackend — real adapter over artifact_store.py
# ---------------------------------------------------------------------------

class LocalObjectStoreBackend:
    """Adapter over the EXISTING ``meridian/artifact_store.py`` functions
    — ships with zero new dependency, zero credentials, and IS today's
    behavior under the new interface. Every method delegates directly;
    this class adds no new semantics, only the Protocol shape.

    Keys passed to this backend are the full namespaced key from
    :func:`build_object_key` (``{project_id}/{artifact_class}/{hash[:2]}/
    {digest}``, optionally ``tenants/{tenant_id}/`` prefixed) — the
    ``project_id`` and content hash are parsed back out of the key so the
    underlying ``artifact_store`` calls stay project-scoped exactly like
    every other caller of that module.
    """

    def __init__(self, data_dir: str) -> None:
        self._data_dir = data_dir

    @staticmethod
    def _parse_key(key: str) -> "tuple[str, str]":
        """Return ``(project_id, content_hash)`` parsed out of a key built
        by :func:`build_object_key`. Raises :class:`InvalidKeyError` for
        anything that doesn't match the expected shape."""
        parts = key.split("/")
        if parts and parts[0] == "tenants":
            # tenants/{tenant_id}/{project_id}/{artifact_class}/{hh}/{digest}
            if len(parts) != 6:
                raise InvalidKeyError(f"malformed tenant-prefixed key {key!r}")
            _tenant_id, project_id, artifact_class, _hh, digest = parts[1:]
        else:
            # {project_id}/{artifact_class}/{hh}/{digest}
            if len(parts) != 4:
                raise InvalidKeyError(f"malformed key {key!r}")
            project_id, artifact_class, _hh, digest = parts
        _safe_component(project_id, label="project_id")
        _safe_artifact_class(artifact_class)
        content_hash_value = f"sha256:{digest}"
        _digest_of(content_hash_value)  # validates digest shape
        return project_id, content_hash_value

    async def put(
        self, key: str, data: bytes, *,
        content_type: "str | None" = None,
        metadata: "dict[str, str] | None" = None,
        if_none_match: "str | None" = None,
        if_match: "str | None" = None,
    ) -> PutResult:
        project_id, content_hash_value = self._parse_key(key)
        existed = artifact_store.get_artifact_metadata(
            self._data_dir, project_id, content_hash_value,
        ) is not None
        if if_none_match == "*" and existed:
            raise PreconditionFailedError(
                f"key {key!r} already exists (if_none_match='*' violated)"
            )
        if if_match is not None and not existed:
            raise PreconditionFailedError(
                f"key {key!r} does not exist (if_match={if_match!r} violated)"
            )
        meta = artifact_store.store_artifact(
            self._data_dir, project_id, data, content_type=content_type,
        )
        actual_hash = meta["content_hash"]
        if actual_hash != content_hash_value:
            # The caller's key claimed a hash that doesn't match the bytes
            # actually supplied — never silently store under the wrong key.
            raise InvalidKeyError(
                f"key {key!r} declares content_hash {content_hash_value!r} "
                f"but the supplied bytes hash to {actual_hash!r}"
            )
        return PutResult(key=key, etag=actual_hash, size=meta["size"])

    async def get(self, key: str) -> bytes:
        project_id, content_hash_value = self._parse_key(key)
        data = artifact_store.get_artifact(self._data_dir, project_id, content_hash_value)
        if data is None:
            raise ObjectNotFoundError(f"key {key!r} not found")
        return data

    async def head(self, key: str) -> ObjectMetadata:
        project_id, content_hash_value = self._parse_key(key)
        meta = artifact_store.get_artifact_metadata(
            self._data_dir, project_id, content_hash_value,
        )
        if meta is None:
            raise ObjectNotFoundError(f"key {key!r} not found")
        return ObjectMetadata(
            key=key,
            size=meta["size"],
            etag=meta["content_hash"],
            content_type=meta.get("content_type"),
            content_hash=meta["content_hash"],
            created_at=meta["created_at"],
            last_modified=meta["created_at"],
            custom_metadata={},
        )

    async def delete(self, key: str) -> bool:
        project_id, content_hash_value = self._parse_key(key)
        return artifact_store.delete_artifact(self._data_dir, project_id, content_hash_value)

    async def exists(self, key: str) -> bool:
        project_id, content_hash_value = self._parse_key(key)
        return artifact_store.get_artifact_metadata(
            self._data_dir, project_id, content_hash_value,
        ) is not None

    async def list(
        self, prefix: str, *, limit: int = 1000, cursor: "str | None" = None,
    ) -> ListPage:
        # artifact_store.list_artifacts is project-scoped; `prefix` here is
        # expected to be (at minimum) a project_id, optionally
        # tenant-prefixed, matching build_object_key's shape.
        parts = prefix.split("/")
        if parts and parts[0] == "tenants" and len(parts) >= 2:
            project_id = parts[2] if len(parts) > 2 else None
        else:
            project_id = parts[0] if parts and parts[0] else None
        if not project_id:
            raise InvalidKeyError(f"prefix {prefix!r} must include a project_id")
        _safe_component(project_id, label="project_id")
        metas = artifact_store.list_artifacts(self._data_dir, project_id)
        keys = [
            ObjectMetadata(
                # artifact_class isn't recoverable from artifact_store's flat
                # listing (it doesn't record one), so list() surfaces the
                # project-scoped content key rather than a full
                # build_object_key() — callers that need the exact key for
                # get/head/delete should track it themselves at put() time.
                key=f"{project_id}/{m['content_hash']}",
                size=m["size"],
                etag=m["content_hash"],
                content_type=m.get("content_type"),
                content_hash=m["content_hash"],
                created_at=m["created_at"],
                last_modified=m["created_at"],
                custom_metadata={},
            )
            for m in metas
        ]
        keys.sort(key=lambda om: om.key)
        # cursor is a simple offset-into-sorted-list token for this backend.
        start = int(cursor) if cursor else 0
        page = keys[start:start + limit]
        next_cursor = str(start + limit) if start + limit < len(keys) else None
        return ListPage(keys=page, next_cursor=next_cursor)


# ---------------------------------------------------------------------------
# FakeS3Backend — in-memory test double, NOT a real network client
# ---------------------------------------------------------------------------

class FakeS3Backend:
    """In-memory, S3-shaped test double. NOT a real network client — no
    sockets, no dependency, exists purely to exercise the
    :class:`ObjectStoreBackend` contract in tests.

    Deliberately reproduces the S3/Tigris semantics this module's error
    taxonomy targets (investigation 549e66c6 §2, §7):

    * ``If-None-Match: "*"`` on an existing key -> :class:`PreconditionFailedError`.
    * ``If-Match: <etag>`` mismatch (or object missing) -> :class:`PreconditionFailedError`.
    * :attr:`force_conflict_once` — the NEXT ``put`` for the given key
      raises :class:`ConditionalRequestConflictError` once (simulates a
      concurrent-write race), then succeeds normally.
    * :attr:`unavailable` — every call raises
      :class:`ObjectStoreUnavailableError` (simulates network/endpoint
      down) until cleared. Used to exercise offline-fallback behavior.
    * :attr:`fail_next_put` — the NEXT ``put`` raises
      :class:`ObjectStoreUnavailableError` once, then succeeds (simulates
      a transient failure a retry should recover from).

    Storage is a plain dict keyed by the FULL key string (including any
    ``tenants/{tenant_id}/`` prefix) — this is what gives the fake its
    tenant-isolation behavior for free: two different tenant prefixes
    never collide, exactly as two different S3 key prefixes wouldn't.
    """

    def __init__(self) -> None:
        self._objects: "dict[str, dict[str, Any]]" = {}
        self.unavailable = False
        self.fail_next_put = False
        self._conflict_once_keys: "set[str]" = set()
        self.put_call_count = 0

    def force_conflict_once(self, key: str) -> None:
        """Arrange for the NEXT ``put(key, ...)`` call to raise
        :class:`ConditionalRequestConflictError` exactly once."""
        self._conflict_once_keys.add(key)

    def _check_unavailable(self) -> None:
        if self.unavailable:
            raise ObjectStoreUnavailableError("FakeS3Backend is set unavailable")

    async def put(
        self, key: str, data: bytes, *,
        content_type: "str | None" = None,
        metadata: "dict[str, str] | None" = None,
        if_none_match: "str | None" = None,
        if_match: "str | None" = None,
    ) -> PutResult:
        self._check_unavailable()
        self.put_call_count += 1
        if key in self._conflict_once_keys:
            self._conflict_once_keys.discard(key)
            raise ConditionalRequestConflictError(
                f"simulated concurrent write race on {key!r}"
            )
        if self.fail_next_put:
            self.fail_next_put = False
            raise ObjectStoreUnavailableError("simulated transient failure")

        existing = self._objects.get(key)
        if if_none_match == "*" and existing is not None:
            raise PreconditionFailedError(
                f"key {key!r} already exists (if_none_match='*' violated)"
            )
        if if_match is not None:
            if existing is None or existing["etag"] != if_match:
                raise PreconditionFailedError(
                    f"if_match={if_match!r} did not match current state of {key!r}"
                )

        etag = "sha256:" + hashlib.sha256(data).hexdigest()
        now = time.time()
        created_at = existing["created_at"] if existing is not None else now
        self._objects[key] = {
            "data": bytes(data),
            "etag": etag,
            "content_type": content_type,
            "metadata": dict(metadata or {}),
            "created_at": created_at,
            "last_modified": now,
        }
        return PutResult(key=key, etag=etag, size=len(data))

    async def get(self, key: str) -> bytes:
        self._check_unavailable()
        obj = self._objects.get(key)
        if obj is None:
            raise ObjectNotFoundError(f"key {key!r} not found")
        return obj["data"]

    async def head(self, key: str) -> ObjectMetadata:
        self._check_unavailable()
        obj = self._objects.get(key)
        if obj is None:
            raise ObjectNotFoundError(f"key {key!r} not found")
        return ObjectMetadata(
            key=key,
            size=len(obj["data"]),
            etag=obj["etag"],
            content_type=obj["content_type"],
            content_hash=obj["etag"] if obj["etag"].startswith("sha256:") else None,
            created_at=_iso(obj["created_at"]),
            last_modified=_iso(obj["last_modified"]),
            custom_metadata=dict(obj["metadata"]),
        )

    async def delete(self, key: str) -> bool:
        self._check_unavailable()
        return self._objects.pop(key, None) is not None

    async def exists(self, key: str) -> bool:
        self._check_unavailable()
        return key in self._objects

    async def list(
        self, prefix: str, *, limit: int = 1000, cursor: "str | None" = None,
    ) -> ListPage:
        self._check_unavailable()
        matching = sorted(k for k in self._objects if k.startswith(prefix))
        start = int(cursor) if cursor else 0
        page_keys = matching[start:start + limit]
        metas = [
            ObjectMetadata(
                key=k,
                size=len(self._objects[k]["data"]),
                etag=self._objects[k]["etag"],
                content_type=self._objects[k]["content_type"],
                content_hash=(
                    self._objects[k]["etag"]
                    if self._objects[k]["etag"].startswith("sha256:") else None
                ),
                created_at=_iso(self._objects[k]["created_at"]),
                last_modified=_iso(self._objects[k]["last_modified"]),
                custom_metadata=dict(self._objects[k]["metadata"]),
            )
            for k in page_keys
        ]
        next_cursor = str(start + limit) if start + limit < len(matching) else None
        return ListPage(keys=metas, next_cursor=next_cursor)


def _iso(epoch_seconds: float) -> str:
    from datetime import datetime, timezone
    return (
        datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# TigrisObjectStoreBackend — NOT implemented; inactive-by-default stub
# ---------------------------------------------------------------------------

class TigrisObjectStoreBackend:
    """NOT implemented. This is an explicit, obvious placeholder — every
    method raises :class:`NotImplementedError` immediately, before any
    network call, so a self-hosted install without the (not-yet-added)
    S3 client dependency never silently half-works.

    Per investigation 549e66c6 §5/§9, activating this backend for real
    requires, IN ORDER, and NONE of which this sprint item performs:

    1. A human supply-chain review + approval of an async S3-compatible
       client (recommended candidate: ``aioboto3``) before it is added to
       ``pyproject.toml``/``pixi.toml``.
    2. A real implementation behind this same class, translating S3
       ``ClientError`` status codes into this module's error taxonomy
       (see module docstring / investigation §7).
    3. A human provisioning a real Tigris bucket + access key OUTSIDE
       this repo/session, with credentials stored only in ``.env`` /
       ``meridian.toml`` (never committed) or the hosted secret store.
    4. A live smoke test against that bucket confirming conditional-write
       (412/409) and presigned-URL behavior BEFORE relying on them
       (investigation §2 flagged these as an unconfirmed doc-site gap for
       Tigris specifically).
    5. Explicit `set_capability_manifest` wiring with
       ``availability_policy="optional"`` — never ``"required"`` until
       production-proven.

    See ``docs/object-storage-backend.md`` for the full, exact activation
    gate. Follow-on human item 7197907f (already filed) owns steps 3-4;
    this sprint item deliberately stops here.

    Configuration is read ONLY from opt-in environment variables
    (documented, never touched by this module): ``TIGRIS_ENABLED``,
    ``AWS_ENDPOINT_URL``, ``AWS_ACCESS_KEY_ID``, ``AWS_SECRET_ACCESS_KEY``,
    ``AWS_REGION``, ``TIGRIS_BUCKET``. This class does not read them
    either — even config-reading is left to the future real
    implementation, so importing/instantiating this class can never have
    a side effect that touches the environment or the network.
    """

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError(
            "TigrisObjectStoreBackend is an inactive-by-default placeholder. "
            "Real Tigris/S3 wiring is intentionally NOT implemented — see "
            "docs/object-storage-backend.md for the exact activation gate "
            "(supply-chain review, bucket provisioning, and a live smoke "
            "test are all required, in order, before this class may store "
            "a single byte)."
        )

    async def put(self, *_args: Any, **_kwargs: Any) -> PutResult:  # pragma: no cover
        raise NotImplementedError("TigrisObjectStoreBackend.put is not implemented")

    async def get(self, *_args: Any, **_kwargs: Any) -> bytes:  # pragma: no cover
        raise NotImplementedError("TigrisObjectStoreBackend.get is not implemented")

    async def head(self, *_args: Any, **_kwargs: Any) -> ObjectMetadata:  # pragma: no cover
        raise NotImplementedError("TigrisObjectStoreBackend.head is not implemented")

    async def delete(self, *_args: Any, **_kwargs: Any) -> bool:  # pragma: no cover
        raise NotImplementedError("TigrisObjectStoreBackend.delete is not implemented")

    async def exists(self, *_args: Any, **_kwargs: Any) -> bool:  # pragma: no cover
        raise NotImplementedError("TigrisObjectStoreBackend.exists is not implemented")

    async def list(self, *_args: Any, **_kwargs: Any) -> ListPage:  # pragma: no cover
        raise NotImplementedError("TigrisObjectStoreBackend.list is not implemented")


def get_default_backend(data_dir: str) -> ObjectStoreBackend:
    """Return the backend a caller should use TODAY: always
    :class:`LocalObjectStoreBackend`, unconditionally. There is no
    environment-variable gate that switches this to a remote backend —
    Tigris/S3 stays inactive until a caller EXPLICITLY constructs
    :class:`TigrisObjectStoreBackend` itself (which currently means
    explicitly opting into a ``NotImplementedError``). This function
    exists so future opt-in wiring has one obvious place to change,
    without touching every call site."""
    return LocalObjectStoreBackend(data_dir)
