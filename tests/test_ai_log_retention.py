"""ea972129 (Round 1 proposal e143949d) — ROUND1-AI-LOG local-first storage,
retention, redaction, and artifact persistence.

SCOPE: this file tests the additions sibling item ea972129 layers on top of
9e83be4a's ExecutionEvent contract/storage scaffold (tests/test_ai_log_contract.py
covers that scaffold itself and is NOT duplicated here):

  1.  db.ai_log.purge_events_before — cutoff-based bulk retention delete,
      project isolation, required-argument validation.
  2.  db.ai_log.append_event's new redact-on-write gate — a secret-shaped
      payload is hard-rejected and never reaches storage.
  3.  db.ai_log.AiLogStore — the project-scoped facade (append/get/list/
      purge_older_than) delegates to the exact same free functions.
  4.  meridian.artifact_store — local-first, content-addressed artifact
      persistence: hashing determinism, store/get/delete/list round-trip,
      dedup-by-content, redaction-on-write for text content, binary content
      stored unchanged, cutoff-based retention purge, and path-safety
      validation (malformed project_id / content_hash).

It deliberately does NOT cover capture-boundary enumeration, export format,
or search/indexing — those are sibling items 4d113dcb / 14009d86's job.
"""
from __future__ import annotations

import json

import pytest

from meridian import artifact_store
from meridian import db as db_module


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


# ---------------------------------------------------------------------------
# 1. purge_events_before
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_purge_events_before_deletes_only_older_rows(db):
    pid = await _project(db, "ai-log-retention-cutoff")
    old = await db_module.append_event(db, pid, "session.started", "session")
    # Force an old recorded_at directly (recorded_at is DB-assigned, not
    # settable via append_event's public signature — this is the only way
    # to simulate "aged past the cutoff" without sleeping in a test).
    await db.execute(
        "UPDATE ai_log_events SET recorded_at = '2020-01-01 00:00:00' WHERE id = ?",
        (old["id"],),
    )
    await db.commit()
    new = await db_module.append_event(db, pid, "session.started", "session")

    deleted = await db_module.purge_events_before(db, pid, "2025-01-01 00:00:00")

    assert deleted == 1
    assert await db_module.get_event(db, old["id"]) is None
    survivor = await db_module.get_event(db, new["id"])
    assert survivor is not None


@pytest.mark.asyncio
async def test_purge_events_before_is_project_scoped(db):
    pid_a = await _project(db, "ai-log-retention-a")
    pid_b = await _project(db, "ai-log-retention-b")
    ev_a = await db_module.append_event(db, pid_a, "session.started", "session")
    ev_b = await db_module.append_event(db, pid_b, "session.started", "session")
    for ev in (ev_a, ev_b):
        await db.execute(
            "UPDATE ai_log_events SET recorded_at = '2020-01-01 00:00:00' WHERE id = ?",
            (ev["id"],),
        )
    await db.commit()

    deleted = await db_module.purge_events_before(db, pid_a, "2025-01-01 00:00:00")

    assert deleted == 1
    assert await db_module.get_event(db, ev_a["id"]) is None
    # pid_b's equally-old row must survive — purge never crosses projects.
    assert await db_module.get_event(db, ev_b["id"]) is not None


@pytest.mark.asyncio
async def test_purge_events_before_returns_zero_when_nothing_matches(db):
    pid = await _project(db, "ai-log-retention-noop")
    await db_module.append_event(db, pid, "session.started", "session")
    deleted = await db_module.purge_events_before(db, pid, "2000-01-01 00:00:00")
    assert deleted == 0


@pytest.mark.asyncio
async def test_purge_events_before_requires_project_id(db):
    with pytest.raises(ValueError):
        await db_module.purge_events_before(db, "", "2025-01-01 00:00:00")


@pytest.mark.asyncio
async def test_purge_events_before_requires_cutoff(db):
    pid = await _project(db, "ai-log-retention-badcutoff")
    with pytest.raises(ValueError):
        await db_module.purge_events_before(db, pid, "")


# ---------------------------------------------------------------------------
# 2. append_event redaction gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_append_event_rejects_secret_shaped_payload(db):
    pid = await _project(db, "ai-log-redact-reject")
    with pytest.raises(ValueError):
        await db_module.append_event(
            db, pid, "tool.invoked", "tool",
            payload={"env": "AWS_KEY=AKIAABCDEFGHIJKLMNOP"},
        )
    async with db.execute(
        "SELECT COUNT(*) AS n FROM ai_log_events WHERE project_id = ?", (pid,),
    ) as cur:
        row = await cur.fetchone()
    assert row["n"] == 0  # rejected event never reached storage


@pytest.mark.asyncio
async def test_append_event_allows_clean_payload(db):
    pid = await _project(db, "ai-log-redact-clean")
    created = await db_module.append_event(
        db, pid, "tool.invoked", "tool",
        payload={"symbol": "ExecutionEvent", "args": {"x": 1}},
    )
    assert created["payload"] == {"symbol": "ExecutionEvent", "args": {"x": 1}}


@pytest.mark.asyncio
async def test_append_event_rejects_secret_in_nested_payload(db):
    pid = await _project(db, "ai-log-redact-nested")
    with pytest.raises(ValueError):
        await db_module.append_event(
            db, pid, "llm.response", "model",
            payload={"choices": [{"text": "here is a key sk-ant-" + "a" * 40}]},
        )


# ---------------------------------------------------------------------------
# 3. AiLogStore facade
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ai_log_store_append_get_list_round_trip(db):
    pid = await _project(db, "ai-log-store-facade")
    store = db_module.AiLogStore(db, pid)

    created = await store.append("session.started", "session", source="mcp")
    assert created["project_id"] == pid

    fetched = await store.get(created["id"])
    assert fetched is not None
    assert fetched["id"] == created["id"]

    listed = await store.list()
    assert len(listed) == 1
    assert listed[0]["id"] == created["id"]


@pytest.mark.asyncio
async def test_ai_log_store_purge_older_than_delegates_correctly(db):
    pid = await _project(db, "ai-log-store-purge")
    store = db_module.AiLogStore(db, pid)
    old = await store.append("session.started", "session")
    await db.execute(
        "UPDATE ai_log_events SET recorded_at = '2020-01-01 00:00:00' WHERE id = ?",
        (old["id"],),
    )
    await db.commit()

    deleted = await store.purge_older_than("2025-01-01 00:00:00")

    assert deleted == 1
    assert await store.get(old["id"]) is None


def test_ai_log_store_requires_project_id():
    with pytest.raises(ValueError):
        db_module.AiLogStore(object(), "")


@pytest.mark.asyncio
async def test_ai_log_store_is_isolated_per_project(db):
    pid_a = await _project(db, "ai-log-store-iso-a")
    pid_b = await _project(db, "ai-log-store-iso-b")
    store_a = db_module.AiLogStore(db, pid_a)
    store_b = db_module.AiLogStore(db, pid_b)
    await store_a.append("session.started", "session")
    await store_a.append("tool.invoked", "tool")
    await store_b.append("session.started", "session")

    assert len(await store_a.list()) == 2
    assert len(await store_b.list()) == 1


# ---------------------------------------------------------------------------
# 4. meridian.artifact_store
# ---------------------------------------------------------------------------

def test_content_hash_is_deterministic_sha256():
    h1 = artifact_store.content_hash(b"hello world")
    h2 = artifact_store.content_hash(b"hello world")
    assert h1 == h2
    assert h1.startswith("sha256:")
    assert len(h1) == len("sha256:") + 64


def test_content_hash_differs_for_different_content():
    assert artifact_store.content_hash(b"a") != artifact_store.content_hash(b"b")


def test_store_and_get_artifact_round_trip(tmp_path):
    data_dir = str(tmp_path)
    meta = artifact_store.store_artifact(data_dir, "proj-1", b"raw tool output")

    assert meta["project_id"] == "proj-1"
    assert meta["size"] == len(b"raw tool output")
    assert meta["redacted"] is False
    assert meta["content_hash"].startswith("sha256:")

    fetched = artifact_store.get_artifact(data_dir, "proj-1", meta["content_hash"])
    assert fetched == b"raw tool output"

    fetched_meta = artifact_store.get_artifact_metadata(
        data_dir, "proj-1", meta["content_hash"]
    )
    assert fetched_meta == meta


def test_get_artifact_missing_returns_none(tmp_path):
    data_dir = str(tmp_path)
    missing_hash = artifact_store.content_hash(b"never stored")
    assert artifact_store.get_artifact(data_dir, "proj-1", missing_hash) is None
    assert artifact_store.get_artifact_metadata(data_dir, "proj-1", missing_hash) is None


def test_store_artifact_dedups_identical_content(tmp_path):
    data_dir = str(tmp_path)
    first = artifact_store.store_artifact(
        data_dir, "proj-1", b"same bytes", content_type="text/plain",
    )
    second = artifact_store.store_artifact(
        data_dir, "proj-1", b"same bytes", content_type="application/json",
    )
    # First write wins -- content_type from the second call is ignored.
    assert second == first
    assert second["content_type"] == "text/plain"


def test_store_artifact_isolates_by_project(tmp_path):
    data_dir = str(tmp_path)
    meta = artifact_store.store_artifact(data_dir, "proj-a", b"shared content")
    # Same bytes, different project: proj-b has never stored anything.
    assert artifact_store.get_artifact(data_dir, "proj-b", meta["content_hash"]) is None
    assert artifact_store.get_artifact(data_dir, "proj-a", meta["content_hash"]) == b"shared content"


def test_store_artifact_redacts_text_secrets(tmp_path):
    data_dir = str(tmp_path)
    secret_text = "config: AWS_KEY=AKIAABCDEFGHIJKLMNOP done"
    meta = artifact_store.store_artifact(
        data_dir, "proj-1", secret_text.encode("utf-8"), content_type="text/plain",
    )
    assert meta["redacted"] is True
    stored = artifact_store.get_artifact(data_dir, "proj-1", meta["content_hash"])
    assert b"AKIAABCDEFGHIJKLMNOP" not in stored
    assert b"[REDACTED:" in stored


def test_store_artifact_leaves_binary_content_unchanged(tmp_path):
    data_dir = str(tmp_path)
    binary_blob = bytes(range(256))  # not valid UTF-8
    meta = artifact_store.store_artifact(data_dir, "proj-1", binary_blob)
    assert meta["redacted"] is False
    assert artifact_store.get_artifact(data_dir, "proj-1", meta["content_hash"]) == binary_blob


def test_store_artifact_rejects_non_bytes_content(tmp_path):
    with pytest.raises(artifact_store.ArtifactStoreError):
        artifact_store.store_artifact(str(tmp_path), "proj-1", "not bytes")


def test_delete_artifact_is_idempotent(tmp_path):
    data_dir = str(tmp_path)
    meta = artifact_store.store_artifact(data_dir, "proj-1", b"to be deleted")

    assert artifact_store.delete_artifact(data_dir, "proj-1", meta["content_hash"]) is True
    assert artifact_store.get_artifact(data_dir, "proj-1", meta["content_hash"]) is None
    # Second delete: already absent, no error, returns False.
    assert artifact_store.delete_artifact(data_dir, "proj-1", meta["content_hash"]) is False


def test_list_artifacts_empty_for_unknown_project(tmp_path):
    assert artifact_store.list_artifacts(str(tmp_path), "never-stored-anything") == []


def test_list_artifacts_returns_every_stored_artifact(tmp_path):
    data_dir = str(tmp_path)
    m1 = artifact_store.store_artifact(data_dir, "proj-1", b"artifact one")
    m2 = artifact_store.store_artifact(data_dir, "proj-1", b"artifact two")
    listed = artifact_store.list_artifacts(data_dir, "proj-1")
    hashes = {m["content_hash"] for m in listed}
    assert hashes == {m1["content_hash"], m2["content_hash"]}


def test_purge_artifacts_before_deletes_only_older_ones(tmp_path):
    data_dir = str(tmp_path)
    old = artifact_store.store_artifact(data_dir, "proj-1", b"old artifact")
    new = artifact_store.store_artifact(data_dir, "proj-1", b"new artifact")
    # Backdate the "old" artifact's sidecar directly (created_at is
    # assigned internally, same pattern as backdating recorded_at above).
    meta_path = artifact_store._meta_path(
        data_dir, "proj-1", old["content_hash"].split(":", 1)[1],
    )
    backdated = dict(old, created_at="2020-01-01T00:00:00.000Z")
    artifact_store._atomic_write_bytes(
        meta_path, json.dumps(backdated, sort_keys=True).encode("utf-8"),
    )

    deleted = artifact_store.purge_artifacts_before(
        data_dir, "proj-1", "2025-01-01T00:00:00.000Z",
    )

    assert deleted == 1
    assert artifact_store.get_artifact(data_dir, "proj-1", old["content_hash"]) is None
    assert artifact_store.get_artifact(data_dir, "proj-1", new["content_hash"]) == b"new artifact"


def test_purge_artifacts_before_requires_cutoff(tmp_path):
    with pytest.raises(artifact_store.ArtifactStoreError):
        artifact_store.purge_artifacts_before(str(tmp_path), "proj-1", "")


@pytest.mark.parametrize("bad_project_id", ["", "../escape", "a/b", "a\\b", None])
def test_store_artifact_rejects_unsafe_project_id(tmp_path, bad_project_id):
    with pytest.raises(artifact_store.ArtifactStoreError):
        artifact_store.store_artifact(str(tmp_path), bad_project_id, b"x")


@pytest.mark.parametrize(
    "bad_hash", ["not-a-hash", "sha256:tooshort", "md5:" + "a" * 32, "", None],
)
def test_get_artifact_rejects_malformed_hash(tmp_path, bad_hash):
    with pytest.raises(artifact_store.ArtifactStoreError):
        artifact_store.get_artifact(str(tmp_path), "proj-1", bad_hash)
