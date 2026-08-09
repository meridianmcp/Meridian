"""c0168425 — IMPLEMENT R2-C: ship local-first AI-log artifact lifecycle with
retention, redaction, hashing, purge, and export.

Implementation follow-up to the Round 1 design item ea972129 (local-first
storage, retention, redaction, artifact persistence — tests/test_ai_log_retention.py
covers that already-shipped surface, including db.ai_log.export_events, and
is NOT duplicated here). This file covers the two genuinely NEW capabilities
this item adds on top of that design:

  1.  meridian.artifact_store.export_artifacts — receipted, project-scoped
      bulk export of stored artifacts (content + metadata, base64-encoded).
  2.  The MCP-facing surface wired into meridian.mcp.handler._handle_task_tools
      (meridian/mcp_tools.py's schema entries): export_ai_log,
      export_ai_log_artifacts, purge_ai_log — the "ship" half of this item,
      making the already-tested internals in meridian.db.ai_log /
      meridian.artifact_store actually reachable over MCP for the first
      time.

It deliberately does NOT cover capture-boundary enumeration or
search/indexing (sibling items' job), and does not re-test
store_artifact/get_artifact/delete_artifact/list_artifacts/
purge_artifacts_before/purge_events_before's own internals — those already
have full coverage in tests/test_ai_log_retention.py.
"""
from __future__ import annotations

import base64

import pytest

import meridian.server  # noqa: F401 — load the server before handler to avoid its import cycle
from meridian import artifact_store
from meridian import db as db_module
from meridian.mcp import handler as mcp_handler


async def _project(db, name: str) -> str:
    proj = await db_module.create_project(db, name)
    return proj["id"]


# ---------------------------------------------------------------------------
# 1. meridian.artifact_store.export_artifacts
# ---------------------------------------------------------------------------

def test_export_artifacts_returns_receipted_bundle_with_content(tmp_path):
    data_dir = str(tmp_path)
    meta = artifact_store.store_artifact(data_dir, "proj-1", b"raw tool output")

    bundle = artifact_store.export_artifacts(data_dir, "proj-1")

    assert bundle["project_id"] == "proj-1"
    assert bundle["artifact_count"] == 1
    assert bundle["total_size"] == len(b"raw tool output")
    assert bundle["export_hash"].startswith("sha256:")
    entry = bundle["artifacts"][0]
    assert entry["content_hash"] == meta["content_hash"]
    assert base64.b64decode(entry["content_base64"]) == b"raw tool output"


def test_export_artifacts_empty_project_returns_empty_bundle(tmp_path):
    bundle = artifact_store.export_artifacts(str(tmp_path), "never-stored-anything")
    assert bundle["artifact_count"] == 0
    assert bundle["total_size"] == 0
    assert bundle["artifacts"] == []
    assert bundle["export_hash"].startswith("sha256:")


def test_export_artifacts_is_project_scoped(tmp_path):
    data_dir = str(tmp_path)
    artifact_store.store_artifact(data_dir, "proj-a", b"a's content")
    artifact_store.store_artifact(data_dir, "proj-b", b"b's content one")
    artifact_store.store_artifact(data_dir, "proj-b", b"b's content two")

    bundle_a = artifact_store.export_artifacts(data_dir, "proj-a")
    bundle_b = artifact_store.export_artifacts(data_dir, "proj-b")

    assert bundle_a["artifact_count"] == 1
    assert bundle_b["artifact_count"] == 2


def test_export_artifacts_explicit_subset(tmp_path):
    data_dir = str(tmp_path)
    keep = artifact_store.store_artifact(data_dir, "proj-1", b"keep me")
    artifact_store.store_artifact(data_dir, "proj-1", b"skip me")

    bundle = artifact_store.export_artifacts(
        data_dir, "proj-1", content_hashes=[keep["content_hash"]],
    )

    assert bundle["artifact_count"] == 1
    assert bundle["artifacts"][0]["content_hash"] == keep["content_hash"]


def test_export_artifacts_explicit_subset_missing_hash_raises(tmp_path):
    data_dir = str(tmp_path)
    missing_hash = artifact_store.content_hash(b"never stored")
    with pytest.raises(artifact_store.ArtifactStoreError):
        artifact_store.export_artifacts(data_dir, "proj-1", content_hashes=[missing_hash])


def test_export_artifacts_hash_is_deterministic_for_same_content(tmp_path):
    data_dir = str(tmp_path)
    artifact_store.store_artifact(data_dir, "proj-1", b"stable content")

    first = artifact_store.export_artifacts(data_dir, "proj-1")
    second = artifact_store.export_artifacts(data_dir, "proj-1")

    assert first["export_hash"] == second["export_hash"]


def test_export_artifacts_hash_excludes_content_base64_payload(tmp_path):
    data_dir = str(tmp_path)
    artifact_store.store_artifact(data_dir, "proj-1", b"same bookkeeping, different hash test")
    bundle = artifact_store.export_artifacts(data_dir, "proj-1")
    # Recomputing the hash after stripping content_base64 (what export_hash
    # actually covers, per the function's own docstring) must match exactly.
    import hashlib
    import json
    hashable = [
        {k: v for k, v in a.items() if k != "content_base64"}
        for a in bundle["artifacts"]
    ]
    canonical = json.dumps(hashable, sort_keys=True, separators=(",", ":"), default=str)
    expected = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert bundle["export_hash"] == expected


def test_export_artifacts_includes_redacted_content(tmp_path):
    data_dir = str(tmp_path)
    secret_text = "config: AWS_KEY=AKIAABCDEFGHIJKLMNOP done"
    artifact_store.store_artifact(data_dir, "proj-1", secret_text.encode("utf-8"))

    bundle = artifact_store.export_artifacts(data_dir, "proj-1")

    assert bundle["artifacts"][0]["redacted"] is True
    decoded = base64.b64decode(bundle["artifacts"][0]["content_base64"])
    assert b"AKIAABCDEFGHIJKLMNOP" not in decoded


def test_export_artifacts_rejects_unsafe_project_id(tmp_path):
    with pytest.raises(artifact_store.ArtifactStoreError):
        artifact_store.export_artifacts(str(tmp_path), "../escape")


# ---------------------------------------------------------------------------
# 2. MCP dispatch — export_ai_log (events)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_export_ai_log_dispatches_to_export_events(db, tmp_path):
    pid = await _project(db, "mcp-export-ai-log")
    created = await db_module.append_event(db, pid, "session.started", "session")

    result = await mcp_handler._handle_task_tools(
        "export_ai_log", {"project_id": pid}, db, str(tmp_path),
        tenant=None, _mcp_tenant_id=None,
    )

    assert result["project_id"] == pid
    assert result["event_count"] == 1
    assert result["events"][0]["id"] == created["id"]
    assert result["export_hash"].startswith("sha256:")


@pytest.mark.asyncio
async def test_mcp_export_ai_log_applies_filters(db, tmp_path):
    pid = await _project(db, "mcp-export-ai-log-filter")
    await db_module.append_event(db, pid, "tool.invoked", "tool")
    match = await db_module.append_event(db, pid, "llm.response", "model")

    result = await mcp_handler._handle_task_tools(
        "export_ai_log", {"project_id": pid, "event_type": "llm.response"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )

    assert result["event_count"] == 1
    assert result["events"][0]["id"] == match["id"]


# ---------------------------------------------------------------------------
# 3. MCP dispatch — export_ai_log_artifacts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_export_ai_log_artifacts_dispatches_to_export_artifacts(db, tmp_path):
    pid = await _project(db, "mcp-export-artifacts")
    meta = artifact_store.store_artifact(str(tmp_path), pid, b"exported via mcp")

    result = await mcp_handler._handle_task_tools(
        "export_ai_log_artifacts", {"project_id": pid}, db, str(tmp_path),
        tenant=None, _mcp_tenant_id=None,
    )

    assert result["project_id"] == pid
    assert result["artifact_count"] == 1
    assert result["artifacts"][0]["content_hash"] == meta["content_hash"]


@pytest.mark.asyncio
async def test_mcp_export_ai_log_artifacts_explicit_subset(db, tmp_path):
    pid = await _project(db, "mcp-export-artifacts-subset")
    keep = artifact_store.store_artifact(str(tmp_path), pid, b"keep")
    artifact_store.store_artifact(str(tmp_path), pid, b"skip")

    result = await mcp_handler._handle_task_tools(
        "export_ai_log_artifacts",
        {"project_id": pid, "content_hashes": [keep["content_hash"]]},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )

    assert result["artifact_count"] == 1
    assert result["artifacts"][0]["content_hash"] == keep["content_hash"]


@pytest.mark.asyncio
async def test_mcp_export_ai_log_artifacts_invalid_hash_returns_structured_error(db, tmp_path):
    pid = await _project(db, "mcp-export-artifacts-bad-hash")

    result = await mcp_handler._handle_task_tools(
        "export_ai_log_artifacts",
        {"project_id": pid, "content_hashes": ["sha256:" + "0" * 64]},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )

    assert result["error"] == "ARTIFACT_EXPORT_INVALID"


@pytest.mark.asyncio
async def test_mcp_export_ai_log_artifacts_rejects_non_list_content_hashes(db, tmp_path):
    pid = await _project(db, "mcp-export-artifacts-bad-type")
    with pytest.raises(ValueError):
        await mcp_handler._handle_task_tools(
            "export_ai_log_artifacts",
            {"project_id": pid, "content_hashes": "not-a-list"},
            db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
        )


# ---------------------------------------------------------------------------
# 4. MCP dispatch — purge_ai_log (combined events + artifacts sweep)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_purge_ai_log_deletes_old_events_and_artifacts(db, tmp_path):
    pid = await _project(db, "mcp-purge-combined")
    data_dir = str(tmp_path)

    old_event = await db_module.append_event(db, pid, "session.started", "session")
    await db.execute(
        "UPDATE ai_log_events SET recorded_at = '2020-01-01 00:00:00' WHERE id = ?",
        (old_event["id"],),
    )
    await db.commit()
    new_event = await db_module.append_event(db, pid, "session.started", "session")

    old_artifact = artifact_store.store_artifact(data_dir, pid, b"old artifact")
    old_meta_path = artifact_store._meta_path(
        data_dir, pid, old_artifact["content_hash"].split(":", 1)[1],
    )
    import json as _json
    backdated = dict(old_artifact, created_at="2020-01-01T00:00:00.000Z")
    artifact_store._atomic_write_bytes(
        old_meta_path, _json.dumps(backdated, sort_keys=True).encode("utf-8"),
    )
    new_artifact = artifact_store.store_artifact(data_dir, pid, b"new artifact")

    result = await mcp_handler._handle_task_tools(
        "purge_ai_log", {"project_id": pid, "cutoff": "2025-01-01T00:00:00Z"},
        db, data_dir, tenant=None, _mcp_tenant_id=None,
    )

    assert result["project_id"] == pid
    assert result["events_deleted"] == 1
    assert result["artifacts_deleted"] == 1
    assert result["purged_at"]

    assert await db_module.get_event(db, old_event["id"]) is None
    assert await db_module.get_event(db, new_event["id"]) is not None
    assert artifact_store.get_artifact(data_dir, pid, old_artifact["content_hash"]) is None
    assert artifact_store.get_artifact(data_dir, pid, new_artifact["content_hash"]) == b"new artifact"


@pytest.mark.asyncio
async def test_mcp_purge_ai_log_requires_cutoff(db, tmp_path):
    pid = await _project(db, "mcp-purge-no-cutoff")
    with pytest.raises(ValueError):
        await mcp_handler._handle_task_tools(
            "purge_ai_log", {"project_id": pid}, db, str(tmp_path),
            tenant=None, _mcp_tenant_id=None,
        )


@pytest.mark.asyncio
async def test_mcp_purge_ai_log_rejects_malformed_cutoff(db, tmp_path):
    pid = await _project(db, "mcp-purge-bad-cutoff")
    with pytest.raises(ValueError):
        await mcp_handler._handle_task_tools(
            "purge_ai_log", {"project_id": pid, "cutoff": "not-a-date"},
            db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
        )


@pytest.mark.asyncio
async def test_mcp_purge_ai_log_is_project_scoped(db, tmp_path):
    pid_a = await _project(db, "mcp-purge-scope-a")
    pid_b = await _project(db, "mcp-purge-scope-b")
    data_dir = str(tmp_path)
    ev_a = await db_module.append_event(db, pid_a, "session.started", "session")
    ev_b = await db_module.append_event(db, pid_b, "session.started", "session")
    for ev in (ev_a, ev_b):
        await db.execute(
            "UPDATE ai_log_events SET recorded_at = '2020-01-01 00:00:00' WHERE id = ?",
            (ev["id"],),
        )
    await db.commit()

    result = await mcp_handler._handle_task_tools(
        "purge_ai_log", {"project_id": pid_a, "cutoff": "2025-01-01T00:00:00Z"},
        db, data_dir, tenant=None, _mcp_tenant_id=None,
    )

    assert result["events_deleted"] == 1
    assert await db_module.get_event(db, ev_a["id"]) is None
    # pid_b's equally-old row must survive — purge never crosses projects.
    assert await db_module.get_event(db, ev_b["id"]) is not None
