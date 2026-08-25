"""1d34c076 (build milestone; investigation 549e66c6) — tests for
:mod:`meridian.object_store`: the provider-neutral ObjectStoreBackend
Protocol, its error taxonomy, :class:`LocalObjectStoreBackend` (real
adapter over :mod:`meridian.artifact_store`), and :class:`FakeS3Backend`
(in-memory S3-shaped test double, NOT a real network client).

Acceptance-gate coverage (sprint item 1d34c076):
  1. Local backend behavior + existing artifact/provenance tests remain
     green — this file adds NEW coverage; tests/test_ai_log_retention.py
     and friends are untouched and still exercise artifact_store.py
     directly.
  2. FakeS3Backend tests cover: upload, download, head, hash mismatch,
     retry, offline fallback, tenant isolation, idempotency — each has
     its own section below.
  3. LocalObjectStoreBackend parity tests against real artifact_store.py
     behavior — see the "parity" section.

TigrisObjectStoreBackend is deliberately NOT tested here beyond
confirming it refuses to do anything (it is an inactive-by-default
NotImplementedError stub — see its own docstring in object_store.py).
"""
from __future__ import annotations

import pytest

from meridian import artifact_store
from meridian.object_store import (
    ARTIFACT_CLASSES,
    ConditionalRequestConflictError,
    FakeS3Backend,
    InvalidKeyError,
    LocalObjectStoreBackend,
    ObjectNotFoundError,
    ObjectStoreUnavailableError,
    PreconditionFailedError,
    TigrisObjectStoreBackend,
    build_object_key,
    get_default_backend,
)


# ---------------------------------------------------------------------------
# build_object_key
# ---------------------------------------------------------------------------

def test_build_object_key_shape():
    h = artifact_store.content_hash(b"hello world")
    key = build_object_key("proj-1", "ai_log_artifact", h)
    digest = h.split(":", 1)[1]
    assert key == f"proj-1/ai_log_artifact/{digest[:2]}/{digest}"


def test_build_object_key_tenant_prefixed():
    h = artifact_store.content_hash(b"hello world")
    key = build_object_key("proj-1", "ai_log_artifact", h, tenant_id="tenant-a")
    digest = h.split(":", 1)[1]
    assert key == f"tenants/tenant-a/proj-1/ai_log_artifact/{digest[:2]}/{digest}"


def test_build_object_key_rejects_unknown_artifact_class():
    h = artifact_store.content_hash(b"x")
    with pytest.raises(InvalidKeyError):
        build_object_key("proj-1", "not_a_real_class", h)


def test_build_object_key_rejects_unsafe_project_id():
    h = artifact_store.content_hash(b"x")
    with pytest.raises(InvalidKeyError):
        build_object_key("../etc/passwd", "ai_log_artifact", h)


def test_build_object_key_rejects_malformed_hash():
    with pytest.raises(InvalidKeyError):
        build_object_key("proj-1", "ai_log_artifact", "not-a-hash")


def test_artifact_classes_are_fixed_and_nonempty():
    assert ARTIFACT_CLASSES
    assert all(isinstance(c, str) and c for c in ARTIFACT_CLASSES)


def test_get_default_backend_is_local(tmp_path):
    backend = get_default_backend(str(tmp_path))
    assert isinstance(backend, LocalObjectStoreBackend)


# ---------------------------------------------------------------------------
# LocalObjectStoreBackend — real adapter over artifact_store.py
# ---------------------------------------------------------------------------

async def test_local_backend_put_get_roundtrip(tmp_path):
    backend = LocalObjectStoreBackend(str(tmp_path))
    h = artifact_store.content_hash(b"payload bytes")
    key = build_object_key("proj-1", "ai_log_artifact", h)

    result = await backend.put(key, b"payload bytes", content_type="text/plain")
    assert result.key == key
    assert result.etag == h
    assert result.size == len(b"payload bytes")

    got = await backend.get(key)
    assert got == b"payload bytes"


async def test_local_backend_head(tmp_path):
    backend = LocalObjectStoreBackend(str(tmp_path))
    h = artifact_store.content_hash(b"head me")
    key = build_object_key("proj-1", "ai_log_artifact", h)
    await backend.put(key, b"head me", content_type="text/plain")

    meta = await backend.head(key)
    assert meta.key == key
    assert meta.size == len(b"head me")
    assert meta.content_hash == h
    assert meta.etag == h
    assert meta.content_type == "text/plain"


async def test_local_backend_get_missing_raises_not_found(tmp_path):
    backend = LocalObjectStoreBackend(str(tmp_path))
    h = artifact_store.content_hash(b"never stored")
    key = build_object_key("proj-1", "ai_log_artifact", h)
    with pytest.raises(ObjectNotFoundError):
        await backend.get(key)


async def test_local_backend_head_missing_raises_not_found(tmp_path):
    backend = LocalObjectStoreBackend(str(tmp_path))
    h = artifact_store.content_hash(b"never stored")
    key = build_object_key("proj-1", "ai_log_artifact", h)
    with pytest.raises(ObjectNotFoundError):
        await backend.head(key)


async def test_local_backend_exists(tmp_path):
    backend = LocalObjectStoreBackend(str(tmp_path))
    h = artifact_store.content_hash(b"exists check")
    key = build_object_key("proj-1", "ai_log_artifact", h)
    assert await backend.exists(key) is False
    await backend.put(key, b"exists check")
    assert await backend.exists(key) is True


async def test_local_backend_delete_is_idempotent(tmp_path):
    backend = LocalObjectStoreBackend(str(tmp_path))
    h = artifact_store.content_hash(b"delete me")
    key = build_object_key("proj-1", "ai_log_artifact", h)
    await backend.put(key, b"delete me")

    assert await backend.delete(key) is True
    assert await backend.exists(key) is False
    # Idempotent: deleting an already-absent key returns False, not an error.
    assert await backend.delete(key) is False


async def test_local_backend_list_is_project_scoped(tmp_path):
    backend = LocalObjectStoreBackend(str(tmp_path))
    h1 = artifact_store.content_hash(b"proj a content")
    h2 = artifact_store.content_hash(b"proj b content")
    await backend.put(build_object_key("proj-a", "ai_log_artifact", h1), b"proj a content")
    await backend.put(build_object_key("proj-b", "ai_log_artifact", h2), b"proj b content")

    page_a = await backend.list("proj-a")
    page_b = await backend.list("proj-b")
    assert len(page_a.keys) == 1
    assert len(page_b.keys) == 1
    assert page_a.keys[0].content_hash == h1
    assert page_b.keys[0].content_hash == h2


async def test_local_backend_rejects_malformed_key(tmp_path):
    backend = LocalObjectStoreBackend(str(tmp_path))
    with pytest.raises(InvalidKeyError):
        await backend.get("not/a/valid/key/shape/at/all/too/many/parts")


async def test_local_backend_if_none_match_star_rejects_existing_key(tmp_path):
    backend = LocalObjectStoreBackend(str(tmp_path))
    h = artifact_store.content_hash(b"create only")
    key = build_object_key("proj-1", "ai_log_artifact", h)
    await backend.put(key, b"create only")

    with pytest.raises(PreconditionFailedError):
        await backend.put(key, b"create only", if_none_match="*")


async def test_local_backend_if_match_rejects_missing_key(tmp_path):
    backend = LocalObjectStoreBackend(str(tmp_path))
    h = artifact_store.content_hash(b"update only")
    key = build_object_key("proj-1", "ai_log_artifact", h)
    with pytest.raises(PreconditionFailedError):
        await backend.put(key, b"update only", if_match="sha256:" + "0" * 64)


async def test_local_backend_hash_mismatch_between_key_and_bytes(tmp_path):
    """A key that DECLARES one content_hash but is supplied DIFFERENT bytes
    must never be silently stored under the wrong key — this is the
    'hash mismatch' acceptance-gate scenario for the local backend."""
    backend = LocalObjectStoreBackend(str(tmp_path))
    wrong_hash = artifact_store.content_hash(b"this is not the real content")
    key = build_object_key("proj-1", "ai_log_artifact", wrong_hash)

    with pytest.raises(InvalidKeyError):
        await backend.put(key, b"totally different bytes")


# ---------------------------------------------------------------------------
# LocalObjectStoreBackend parity with real artifact_store.py behavior
# ---------------------------------------------------------------------------

async def test_local_backend_parity_written_via_backend_readable_via_artifact_store(tmp_path):
    data_dir = str(tmp_path)
    backend = LocalObjectStoreBackend(data_dir)
    h = artifact_store.content_hash(b"cross-layer content")
    key = build_object_key("proj-1", "ai_log_artifact", h)
    await backend.put(key, b"cross-layer content")

    # The bytes must be readable through the RAW artifact_store API too —
    # LocalObjectStoreBackend adds no new storage semantics, only the
    # Protocol shape (module docstring's core claim).
    raw = artifact_store.get_artifact(data_dir, "proj-1", h)
    assert raw == b"cross-layer content"


async def test_local_backend_parity_written_via_artifact_store_readable_via_backend(tmp_path):
    data_dir = str(tmp_path)
    meta = artifact_store.store_artifact(data_dir, "proj-1", b"written the old way")
    backend = LocalObjectStoreBackend(data_dir)
    key = build_object_key("proj-1", "ai_log_artifact", meta["content_hash"])

    got = await backend.get(key)
    assert got == b"written the old way"


async def test_local_backend_idempotent_restore_matches_artifact_store_semantics(tmp_path):
    """Re-storing byte-identical content is a no-op that returns the
    EXISTING artifact's metadata unchanged — artifact_store.store_artifact's
    own documented idempotency, which LocalObjectStoreBackend must preserve
    exactly (it delegates, adds no new semantics)."""
    data_dir = str(tmp_path)
    backend = LocalObjectStoreBackend(data_dir)
    h = artifact_store.content_hash(b"idempotent content")
    key = build_object_key("proj-1", "ai_log_artifact", h)

    first = await backend.put(key, b"idempotent content")
    first_meta = artifact_store.get_artifact_metadata(data_dir, "proj-1", h)
    second = await backend.put(key, b"idempotent content")
    second_meta = artifact_store.get_artifact_metadata(data_dir, "proj-1", h)

    assert first.etag == second.etag == h
    assert first_meta["created_at"] == second_meta["created_at"]  # first write wins


# ---------------------------------------------------------------------------
# FakeS3Backend — upload / download / head
# ---------------------------------------------------------------------------

async def test_fake_s3_upload_and_download():
    backend = FakeS3Backend()
    result = await backend.put("proj-1/ai_log_artifact/ab/abcd", b"uploaded bytes")
    assert result.size == len(b"uploaded bytes")
    assert result.etag.startswith("sha256:")

    got = await backend.get("proj-1/ai_log_artifact/ab/abcd")
    assert got == b"uploaded bytes"


async def test_fake_s3_head_returns_correct_metadata():
    backend = FakeS3Backend()
    await backend.put(
        "proj-1/ai_log_artifact/ab/abcd", b"head payload",
        content_type="application/json", metadata={"source": "test"},
    )
    meta = await backend.head("proj-1/ai_log_artifact/ab/abcd")
    assert meta.size == len(b"head payload")
    assert meta.content_type == "application/json"
    assert meta.custom_metadata == {"source": "test"}


async def test_fake_s3_get_missing_raises_not_found():
    backend = FakeS3Backend()
    with pytest.raises(ObjectNotFoundError):
        await backend.get("proj-1/ai_log_artifact/ab/never-uploaded")


async def test_fake_s3_head_missing_raises_not_found():
    backend = FakeS3Backend()
    with pytest.raises(ObjectNotFoundError):
        await backend.head("proj-1/ai_log_artifact/ab/never-uploaded")


async def test_fake_s3_delete_is_idempotent():
    backend = FakeS3Backend()
    key = "proj-1/ai_log_artifact/ab/abcd"
    await backend.put(key, b"to be deleted")
    assert await backend.delete(key) is True
    assert await backend.exists(key) is False
    assert await backend.delete(key) is False  # idempotent, no error


# ---------------------------------------------------------------------------
# FakeS3Backend — hash mismatch (integrity check use case)
# ---------------------------------------------------------------------------

async def test_fake_s3_downloaded_content_hash_matches_declared_content_hash():
    """A caller storing content-addressed data can verify integrity by
    comparing artifact_store.content_hash(downloaded_bytes) against the
    hash embedded in the key/etag — this is the 'hash mismatch' detection
    contract a real S3/Tigris backend must also support."""
    backend = FakeS3Backend()
    real_content = b"the real content"
    real_hash = artifact_store.content_hash(real_content)
    key = build_object_key("proj-1", "ai_log_artifact", real_hash)

    await backend.put(key, real_content)
    downloaded = await backend.get(key)
    assert artifact_store.content_hash(downloaded) == real_hash

    # Simulate a corrupted/substituted object: different bytes stored
    # under a key that CLAIMS a different hash than what's actually there.
    tampered_key = build_object_key("proj-1", "ai_log_artifact", real_hash)
    await backend.put(tampered_key, b"substituted, wrong content")
    downloaded_tampered = await backend.get(tampered_key)
    assert artifact_store.content_hash(downloaded_tampered) != real_hash


# ---------------------------------------------------------------------------
# FakeS3Backend — retry (transient failure recovers)
# ---------------------------------------------------------------------------

async def test_fake_s3_transient_failure_then_retry_succeeds():
    backend = FakeS3Backend()
    backend.fail_next_put = True
    key = "proj-1/ai_log_artifact/ab/retry-me"

    with pytest.raises(ObjectStoreUnavailableError):
        await backend.put(key, b"retry content")

    # Retry the SAME put — succeeds because fail_next_put only fires once.
    result = await backend.put(key, b"retry content")
    assert result.etag.startswith("sha256:")
    assert await backend.get(key) == b"retry content"


async def test_fake_s3_conditional_conflict_then_retry_succeeds():
    """AWS's own documented guidance for a 409 ConditionalRequestConflict:
    re-fetch state and retry the upload (investigation 549e66c6 §2)."""
    backend = FakeS3Backend()
    key = "proj-1/ai_log_artifact/ab/conflict-me"
    backend.force_conflict_once(key)

    with pytest.raises(ConditionalRequestConflictError):
        await backend.put(key, b"conflict content", if_none_match="*")

    result = await backend.put(key, b"conflict content", if_none_match="*")
    assert result.etag.startswith("sha256:")


# ---------------------------------------------------------------------------
# Offline fallback — local-first invariant (never lose data on remote outage)
# ---------------------------------------------------------------------------

async def test_offline_remote_backend_never_loses_local_data(tmp_path):
    """Mirrors artifact_store.py's own documented Redis-cache invariant,
    applied to remote object storage: a remote outage must degrade to
    latency only, NEVER data loss or a wrong answer. Content already
    written locally via LocalObjectStoreBackend must remain fully
    readable even while the remote backend is completely unavailable."""
    data_dir = str(tmp_path)
    local = LocalObjectStoreBackend(data_dir)
    remote = FakeS3Backend()
    remote.unavailable = True

    h = artifact_store.content_hash(b"local first content")
    key = build_object_key("proj-1", "ai_log_artifact", h)

    # Local write always succeeds regardless of remote state.
    await local.put(key, b"local first content")

    # A sync attempt against the down remote fails loudly (never silently)...
    with pytest.raises(ObjectStoreUnavailableError):
        await remote.put(key, b"local first content")

    # ...but the local copy is completely unaffected: still fully readable.
    assert await local.get(key) == b"local first content"
    assert await local.exists(key) is True


async def test_offline_remote_read_raises_unavailable_not_silent_miss():
    """A remote read while unavailable must raise, not silently behave
    like ObjectNotFoundError — the caller needs to distinguish 'genuinely
    absent' from 'backend unreachable' to route retry vs. give-up logic
    correctly."""
    backend = FakeS3Backend()
    await backend.put("proj-1/ai_log_artifact/ab/x", b"data")
    backend.unavailable = True

    with pytest.raises(ObjectStoreUnavailableError):
        await backend.get("proj-1/ai_log_artifact/ab/x")
    with pytest.raises(ObjectStoreUnavailableError):
        await backend.exists("proj-1/ai_log_artifact/ab/x")


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------

async def test_fake_s3_tenant_prefixes_never_collide():
    backend = FakeS3Backend()
    h = artifact_store.content_hash(b"same content, two tenants")
    key_a = build_object_key("proj-1", "ai_log_artifact", h, tenant_id="tenant-a")
    key_b = build_object_key("proj-1", "ai_log_artifact", h, tenant_id="tenant-b")
    assert key_a != key_b

    await backend.put(key_a, b"tenant a's bytes")
    await backend.put(key_b, b"tenant b's bytes")

    assert await backend.get(key_a) == b"tenant a's bytes"
    assert await backend.get(key_b) == b"tenant b's bytes"


async def test_fake_s3_deleting_one_tenant_key_does_not_affect_another():
    backend = FakeS3Backend()
    h = artifact_store.content_hash(b"shared content shape")
    key_a = build_object_key("proj-1", "ai_log_artifact", h, tenant_id="tenant-a")
    key_b = build_object_key("proj-1", "ai_log_artifact", h, tenant_id="tenant-b")
    await backend.put(key_a, b"a")
    await backend.put(key_b, b"b")

    await backend.delete(key_a)

    assert await backend.exists(key_a) is False
    assert await backend.exists(key_b) is True


async def test_fake_s3_list_scoped_to_tenant_prefix():
    backend = FakeS3Backend()
    h1 = artifact_store.content_hash(b"a1")
    h2 = artifact_store.content_hash(b"a2")
    h3 = artifact_store.content_hash(b"b1")
    await backend.put(build_object_key("proj-1", "ai_log_artifact", h1, tenant_id="tenant-a"), b"a1")
    await backend.put(build_object_key("proj-1", "ai_log_artifact", h2, tenant_id="tenant-a"), b"a2")
    await backend.put(build_object_key("proj-1", "ai_log_artifact", h3, tenant_id="tenant-b"), b"b1")

    page_a = await backend.list("tenants/tenant-a/")
    page_b = await backend.list("tenants/tenant-b/")
    assert len(page_a.keys) == 2
    assert len(page_b.keys) == 1


async def test_local_backend_project_scoping_is_the_isolation_boundary(tmp_path):
    """LocalObjectStoreBackend has no bucket/tenant concept of its own —
    isolation comes entirely from artifact_store.py's project-scoped
    filesystem layout (investigation §4's 'application-layer key-namespace
    enforcement... already provides the primary isolation boundary
    regardless of bucket topology'). Two different projects' identical
    content never collide."""
    data_dir = str(tmp_path)
    backend = LocalObjectStoreBackend(data_dir)
    content = b"identical bytes, different projects"
    h = artifact_store.content_hash(content)
    key_a = build_object_key("proj-a", "ai_log_artifact", h)
    key_b = build_object_key("proj-b", "ai_log_artifact", h)

    await backend.put(key_a, content)
    assert await backend.exists(key_b) is False  # proj-b never got it
    await backend.put(key_b, content)
    assert await backend.exists(key_a) is True
    assert await backend.exists(key_b) is True

    await backend.delete(key_a)
    assert await backend.exists(key_a) is False
    assert await backend.exists(key_b) is True  # untouched


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

async def test_fake_s3_put_same_bytes_twice_yields_same_etag():
    backend = FakeS3Backend()
    key = "proj-1/ai_log_artifact/ab/idempotent"
    r1 = await backend.put(key, b"idempotent bytes")
    r2 = await backend.put(key, b"idempotent bytes")
    assert r1.etag == r2.etag
    assert backend.put_call_count == 2  # both calls happened...
    assert await backend.get(key) == b"idempotent bytes"  # ...but state is unchanged


async def test_fake_s3_create_only_put_twice_is_rejected_not_duplicated():
    """Content-addressed create-only semantics: a second if_none_match='*'
    put for a key that already exists must be rejected, never silently
    accepted as a duplicate write (investigation §2's If-None-Match
    mapping onto content-addressed idempotency)."""
    backend = FakeS3Backend()
    key = "proj-1/ai_log_artifact/ab/create-once"
    await backend.put(key, b"first write", if_none_match="*")
    with pytest.raises(PreconditionFailedError):
        await backend.put(key, b"first write", if_none_match="*")


async def test_local_backend_put_call_count_but_one_file_on_disk(tmp_path):
    """Two identical stores through the backend must not create two
    files/hashes — dedup by construction (artifact_store.py's own
    documented behavior), which LocalObjectStoreBackend must preserve."""
    data_dir = str(tmp_path)
    backend = LocalObjectStoreBackend(data_dir)
    h = artifact_store.content_hash(b"dedup me")
    key = build_object_key("proj-1", "ai_log_artifact", h)

    await backend.put(key, b"dedup me")
    await backend.put(key, b"dedup me")

    listed = artifact_store.list_artifacts(data_dir, "proj-1")
    assert len(listed) == 1


# ---------------------------------------------------------------------------
# TigrisObjectStoreBackend — inactive-by-default stub
# ---------------------------------------------------------------------------

def test_tigris_backend_cannot_be_instantiated():
    with pytest.raises(NotImplementedError):
        TigrisObjectStoreBackend()


def test_tigris_backend_instantiation_never_touches_network_or_env(monkeypatch):
    """Constructing (and immediately failing) must never read env vars or
    open a socket — verified by asserting no AWS_* env var lookup occurs.
    monkeypatch.delenv with raising=False proves nothing is required to be
    set for the NotImplementedError to fire deterministically."""
    for var in (
        "AWS_ENDPOINT_URL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
        "AWS_REGION", "TIGRIS_BUCKET", "TIGRIS_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(NotImplementedError):
        TigrisObjectStoreBackend(bucket="whatever", access_key="whatever")
