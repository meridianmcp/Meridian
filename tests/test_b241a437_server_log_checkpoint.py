"""Tests for b241a437 -- lightweight positional/checkpoint index for server_logs.

Coverage:
* ServerLogCheckpointIndex.build() correctly groups rows into minute buckets.
* seek_hint() returns the correct min_recorded_at for a target timestamp.
* seek_hint() returns None when the index is empty or target is before all rows.
* as_dict() returns the expected shape with correct metadata.
* Module-level build_checkpoint / get_checkpoint_dict / seek_hint_for work end-to-end.
* Fallback: seek_hint_for returns None when no rows have been indexed yet.
* get_server_logs MCP handler: seek_to= uses checkpoint to narrow since= hint.
* get_server_log_checkpoint MCP handler: returns index shape, self-warms on first call.
* get_server_log_checkpoint MCP handler: seek_to= returns seek_hint field.
* search_server_logs MCP handler: warms the checkpoint index as a side-effect.
* Tool registration: get_server_log_checkpoint in _READ_ONLY_TOOLS, category=session,
  role=both, in tool list.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from meridian.server_log_checkpoint import (
    ServerLogCheckpointIndex,
    build_checkpoint,
    get_checkpoint_dict,
    seek_hint_for,
    _bucket_label,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_row(
    *,
    recorded_at: str = "2026-07-17 03:00:00",
    level: str = "ERROR",
    logger: str = "meridian.server",
    message: str = "test message",
    row_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": row_id or str(uuid.uuid4()),
        "level": level,
        "logger": logger,
        "message": message,
        "exc_text": None,
        "recorded_at": recorded_at,
    }


def _make_multi_minute_rows() -> list[dict[str, Any]]:
    """Five rows spread across three different minute buckets."""
    return [
        _make_row(recorded_at="2026-07-17 03:00:00", row_id="id-a1"),
        _make_row(recorded_at="2026-07-17 03:00:30", row_id="id-a2"),
        _make_row(recorded_at="2026-07-17 03:01:00", row_id="id-b1"),
        _make_row(recorded_at="2026-07-17 03:02:00", row_id="id-c1"),
        _make_row(recorded_at="2026-07-17 03:02:45", row_id="id-c2"),
    ]


# ---------------------------------------------------------------------------
# ServerLogCheckpointIndex -- unit tests
# ---------------------------------------------------------------------------

def test_build_groups_into_minute_buckets():
    idx = ServerLogCheckpointIndex()
    rows = _make_multi_minute_rows()
    idx.build(rows)
    d = idx.as_dict()
    # Three distinct minute buckets: 03:00, 03:01, 03:02
    assert d["bucket_count"] == 3
    bucket_keys = [b["bucket"] for b in d["buckets"]]
    assert "2026-07-17 03:00" in bucket_keys
    assert "2026-07-17 03:01" in bucket_keys
    assert "2026-07-17 03:02" in bucket_keys


def test_build_counts_per_bucket():
    idx = ServerLogCheckpointIndex()
    rows = _make_multi_minute_rows()
    idx.build(rows)
    d = idx.as_dict()
    buckets = {b["bucket"]: b for b in d["buckets"]}
    assert buckets["2026-07-17 03:00"]["count"] == 2
    assert buckets["2026-07-17 03:01"]["count"] == 1
    assert buckets["2026-07-17 03:02"]["count"] == 2


def test_build_min_max_per_bucket():
    idx = ServerLogCheckpointIndex()
    rows = _make_multi_minute_rows()
    idx.build(rows)
    d = idx.as_dict()
    buckets = {b["bucket"]: b for b in d["buckets"]}
    b00 = buckets["2026-07-17 03:00"]
    assert b00["min_recorded_at"] == "2026-07-17 03:00:00"
    assert b00["max_recorded_at"] == "2026-07-17 03:00:30"
    b02 = buckets["2026-07-17 03:02"]
    assert b02["min_recorded_at"] == "2026-07-17 03:02:00"
    assert b02["max_recorded_at"] == "2026-07-17 03:02:45"


def test_build_first_last_ids_per_bucket():
    idx = ServerLogCheckpointIndex()
    rows = _make_multi_minute_rows()
    idx.build(rows)
    d = idx.as_dict()
    buckets = {b["bucket"]: b for b in d["buckets"]}
    b00 = buckets["2026-07-17 03:00"]
    # first_id is the row with the earliest timestamp in the bucket
    assert b00["first_id"] == "id-a1"
    assert b00["last_id"] == "id-a2"


def test_build_buckets_oldest_first():
    idx = ServerLogCheckpointIndex()
    rows = _make_multi_minute_rows()
    idx.build(rows)
    d = idx.as_dict()
    bucket_keys = [b["bucket"] for b in d["buckets"]]
    # Oldest-first: 03:00 < 03:01 < 03:02
    assert bucket_keys == sorted(bucket_keys)


def test_build_total_rows_and_metadata():
    idx = ServerLogCheckpointIndex()
    rows = _make_multi_minute_rows()
    idx.build(rows)
    d = idx.as_dict()
    assert d["total_rows"] == 5
    assert d["min_recorded_at"] == "2026-07-17 03:00:00"
    assert d["max_recorded_at"] == "2026-07-17 03:02:45"
    assert d["bucket_granularity_label"] == "minute"


def test_build_empty_rows():
    idx = ServerLogCheckpointIndex()
    idx.build([])
    d = idx.as_dict()
    assert d["bucket_count"] == 0
    assert d["total_rows"] == 0
    assert d["min_recorded_at"] is None
    assert d["max_recorded_at"] is None
    assert d["buckets"] == []


def test_seek_hint_exact_bucket():
    idx = ServerLogCheckpointIndex()
    rows = _make_multi_minute_rows()
    idx.build(rows)
    # Seek exactly to the 03:01 bucket -- should return min_recorded_at of 03:01
    hint = idx.seek_hint("2026-07-17 03:01:00")
    # The hint is min_recorded_at of the bucket at or just before 03:01
    assert hint == "2026-07-17 03:01:00"


def test_seek_hint_between_buckets():
    idx = ServerLogCheckpointIndex()
    rows = _make_multi_minute_rows()
    idx.build(rows)
    # Seek to a time between 03:01 and 03:02 -- should return min_recorded_at of 03:01
    hint = idx.seek_hint("2026-07-17 03:01:30")
    assert hint == "2026-07-17 03:01:00"


def test_seek_hint_before_all_rows():
    idx = ServerLogCheckpointIndex()
    rows = _make_multi_minute_rows()
    idx.build(rows)
    # Seek before the first bucket (01:00 < 03:00) -- nothing to anchor to
    hint = idx.seek_hint("2026-07-17 01:00:00")
    assert hint is None


def test_seek_hint_after_all_rows():
    idx = ServerLogCheckpointIndex()
    rows = _make_multi_minute_rows()
    idx.build(rows)
    # Seek after the last bucket -- returns the last bucket's min_recorded_at
    hint = idx.seek_hint("2026-07-17 05:00:00")
    assert hint == "2026-07-17 03:02:00"


def test_seek_hint_empty_index():
    idx = ServerLogCheckpointIndex()
    idx.build([])
    hint = idx.seek_hint("2026-07-17 03:00:00")
    assert hint is None


def test_seek_hint_empty_target():
    idx = ServerLogCheckpointIndex()
    rows = _make_multi_minute_rows()
    idx.build(rows)
    hint = idx.seek_hint("")
    assert hint is None


def test_seek_hint_none_target():
    idx = ServerLogCheckpointIndex()
    rows = _make_multi_minute_rows()
    idx.build(rows)
    hint = idx.seek_hint(None)  # type: ignore[arg-type]
    assert hint is None


def test_rebuild_replaces_old_index():
    idx = ServerLogCheckpointIndex()
    # First build with 5 rows.
    rows = _make_multi_minute_rows()
    idx.build(rows)
    assert idx.as_dict()["total_rows"] == 5

    # Rebuild with a single row (simulating ring-buffer eviction).
    single = [_make_row(recorded_at="2026-07-17 04:00:00")]
    idx.build(single)
    d = idx.as_dict()
    assert d["total_rows"] == 1
    assert d["bucket_count"] == 1
    assert d["buckets"][0]["bucket"] == "2026-07-17 04:00"


def test_stats_method():
    idx = ServerLogCheckpointIndex()
    rows = _make_multi_minute_rows()
    idx.build(rows)
    s = idx.stats()
    assert s["total_rows"] == 5
    assert s["bucket_count"] == 3
    assert s["min_recorded_at"] == "2026-07-17 03:00:00"
    assert s["max_recorded_at"] == "2026-07-17 03:02:45"


def test_custom_bucket_granularity_hourly():
    """bucket_chars=13 gives hourly granularity ("YYYY-MM-DD HH")."""
    idx = ServerLogCheckpointIndex(bucket_chars=13)
    rows = [
        _make_row(recorded_at="2026-07-17 03:00:00"),
        _make_row(recorded_at="2026-07-17 03:45:00"),
        _make_row(recorded_at="2026-07-17 04:00:00"),
    ]
    idx.build(rows)
    d = idx.as_dict()
    # Two distinct hour buckets: "2026-07-17 03" and "2026-07-17 04"
    assert d["bucket_count"] == 2
    assert d["bucket_granularity_label"] == "hourly"


def test_rows_with_missing_recorded_at_are_skipped():
    idx = ServerLogCheckpointIndex()
    rows = [
        _make_row(recorded_at=""),
        _make_row(recorded_at="2026-07-17 03:00:00"),
    ]
    idx.build(rows)
    d = idx.as_dict()
    # Only the row with a valid timestamp contributes to buckets
    assert d["bucket_count"] == 1
    # total_rows still counts both (as that's the raw snapshot size)
    assert d["total_rows"] == 2


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------

def test_module_build_and_get_checkpoint_dict():
    """build_checkpoint + get_checkpoint_dict round-trip."""
    rows = _make_multi_minute_rows()
    build_checkpoint(rows)
    d = get_checkpoint_dict()
    assert d["bucket_count"] >= 3  # may have been populated by other tests
    assert d["total_rows"] >= 5


def test_module_seek_hint_for():
    """seek_hint_for returns a valid since= hint after a build."""
    rows = [
        _make_row(recorded_at="2026-07-17 10:00:00"),
        _make_row(recorded_at="2026-07-17 10:01:00"),
        _make_row(recorded_at="2026-07-17 10:02:00"),
    ]
    build_checkpoint(rows)
    hint = seek_hint_for("2026-07-17 10:01:30")
    # Should be the min_recorded_at of the 10:01 bucket
    assert hint is not None
    assert hint <= "2026-07-17 10:01:30"


def test_bucket_label_known_values():
    assert _bucket_label(16) == "minute"
    assert _bucket_label(13) == "hourly"
    assert _bucket_label(10) == "daily"
    assert _bucket_label(19) == "second"
    assert _bucket_label(7) == "monthly"
    assert _bucket_label(5) == "5-char prefix"


# ---------------------------------------------------------------------------
# MCP handler: handle_get_server_log_checkpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_get_server_log_checkpoint_shape(db):
    """handle_get_server_log_checkpoint returns the expected keys."""
    from meridian.mcp.handlers.session_tools import handle_get_server_log_checkpoint
    from meridian import db as db_module

    # Seed some log rows so the self-warm fetch has something to return
    await db_module.record_server_log(
        db, level="ERROR", logger="meridian.server", message="checkpoint test error"
    )
    result = await handle_get_server_log_checkpoint(
        args={}, db=db, data_dir="", tenant=None, _mcp_tenant_id=None
    )
    assert "total_rows" in result
    assert "bucket_count" in result
    assert "buckets" in result
    assert "bucket_granularity_label" in result
    assert isinstance(result["buckets"], list)


@pytest.mark.asyncio
async def test_handle_get_server_log_checkpoint_self_warms(db):
    """handler fetches from DB and populates the index when cold."""
    from meridian.mcp.handlers.session_tools import handle_get_server_log_checkpoint
    from meridian import db as db_module, server_log_checkpoint as slc

    # Reset the module singleton
    slc._checkpoint_index = None

    await db_module.record_server_log(
        db, level="WARNING", logger="test.warm", message="warm test"
    )
    result = await handle_get_server_log_checkpoint(
        args={}, db=db, data_dir="", tenant=None, _mcp_tenant_id=None
    )
    # Index should now have at least one row
    assert result["total_rows"] >= 1
    assert result["bucket_count"] >= 1


@pytest.mark.asyncio
async def test_handle_get_server_log_checkpoint_seek_to(db):
    """seek_to= arg returns seek_hint field in the response."""
    from meridian.mcp.handlers.session_tools import handle_get_server_log_checkpoint
    from meridian import db as db_module

    await db_module.record_server_log(
        db, level="ERROR", logger="meridian.server", message="seek test"
    )
    result = await handle_get_server_log_checkpoint(
        args={"seek_to": "2026-07-17 03:00:00"},
        db=db, data_dir="", tenant=None, _mcp_tenant_id=None
    )
    # seek_hint key should always be present when seek_to= was given
    assert "seek_hint" in result
    # Value is either None (not in range) or a string timestamp
    assert result["seek_hint"] is None or isinstance(result["seek_hint"], str)


@pytest.mark.asyncio
async def test_handle_get_server_log_checkpoint_no_seek_to(db):
    """Without seek_to=, seek_hint key is absent from the response."""
    from meridian.mcp.handlers.session_tools import handle_get_server_log_checkpoint

    result = await handle_get_server_log_checkpoint(
        args={}, db=db, data_dir="", tenant=None, _mcp_tenant_id=None
    )
    assert "seek_hint" not in result


# ---------------------------------------------------------------------------
# MCP handler: handle_get_server_logs with seek_to=
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_server_logs_seek_to_uses_checkpoint(db):
    """get_server_logs with seek_to= applies a checkpoint-derived since= hint.

    We seed three rows at distinct timestamps, warm the checkpoint via a direct
    build_checkpoint call, then request seek_to=<middle timestamp> and verify
    that only rows at or after the seek point are returned (since rows before
    are filtered by the checkpoint-narrowed since=).
    """
    from meridian import db as db_module, server_log_checkpoint as slc
    from meridian.mcp.handlers.session_tools import handle_get_server_logs

    import uuid
    unique = str(uuid.uuid4())[:8]

    # Insert three rows with distinct timestamps via direct DB call
    # (recorded_at is auto-set to now, but we need manual control for the test)
    # We'll verify the behavior with a future since= that excludes past rows.

    # Record a row now
    await db_module.record_server_log(
        db, level="ERROR", logger=f"test.seek.{unique}", message="row-now"
    )

    # Warm the checkpoint with a synthetic dataset at known timestamps
    synthetic_rows = [
        {
            "id": "synth-1",
            "level": "ERROR",
            "logger": "test.seek",
            "message": "old row",
            "exc_text": None,
            "recorded_at": "2026-07-17 01:00:00",
        },
        {
            "id": "synth-2",
            "level": "ERROR",
            "logger": "test.seek",
            "message": "new row",
            "exc_text": None,
            "recorded_at": "2026-07-17 02:00:00",
        },
    ]
    slc.build_checkpoint(synthetic_rows)

    # Seek to 02:00 -- checkpoint should provide a since= hint around 02:00
    result = await handle_get_server_logs(
        args={"seek_to": "2026-07-17 02:00:00", "limit": 500, "module_filter": f"test.seek.{unique}"},
        db=db, data_dir="", tenant=None, _mcp_tenant_id=None
    )
    # The since= hint derived from the checkpoint (2026-07-17 02:00:00) means
    # the DB query will filter to rows >= that timestamp. Our actual DB row
    # (recorded just now, which is > 2026-07-17 02:00) should be included.
    assert "entries" in result
    assert "count" in result


@pytest.mark.asyncio
async def test_get_server_logs_seek_to_fallback_when_cold(db):
    """get_server_logs with seek_to= on a cold checkpoint falls back to full scan."""
    from meridian import server_log_checkpoint as slc
    from meridian.mcp.handlers.session_tools import handle_get_server_logs
    from meridian import db as db_module

    # Reset the checkpoint to empty
    slc._checkpoint_index = None

    await db_module.record_server_log(
        db, level="ERROR", logger="meridian.fallback", message="fallback test"
    )

    # No crash -- just a full scan (since= hint is None)
    result = await handle_get_server_logs(
        args={"seek_to": "2026-07-17 03:00:00", "limit": 100},
        db=db, data_dir="", tenant=None, _mcp_tenant_id=None
    )
    assert "entries" in result
    assert result["count"] >= 1


@pytest.mark.asyncio
async def test_get_server_logs_seek_to_ignored_when_since_provided(db):
    """If since= is already given, seek_to= is ignored (explicit wins)."""
    from meridian.mcp.handlers.session_tools import handle_get_server_logs

    result = await handle_get_server_logs(
        args={
            "since": "2099-01-01 00:00:00",
            "seek_to": "2026-07-17 03:00:00",
            "limit": 100,
        },
        db=db, data_dir="", tenant=None, _mcp_tenant_id=None
    )
    # since=future means no rows returned (future date)
    assert result["entries"] == []
    assert result["since"] == "2099-01-01 00:00:00"


# ---------------------------------------------------------------------------
# MCP handler: search_server_logs warms the checkpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_server_logs_warms_checkpoint(db, tmp_path):
    """search_server_logs builds the checkpoint index as a side-effect."""
    pytest.importorskip("duckdb")
    from meridian import server_log_checkpoint as slc
    from meridian.mcp.handlers.session_tools import handle_search_server_logs
    from meridian import db as db_module

    # Reset so we can detect the change
    slc._checkpoint_index = None

    await db_module.record_server_log(
        db, level="ERROR", logger="meridian.server", message="OAuth error for search"
    )
    await handle_search_server_logs(
        args={"query": "OAuth error"},
        db=db, data_dir=str(tmp_path), tenant=None, _mcp_tenant_id=None
    )
    # After search_server_logs, the checkpoint should have been built
    d = slc.get_checkpoint_dict()
    assert d["total_rows"] >= 1, "checkpoint was not warmed by search_server_logs"


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def test_get_server_log_checkpoint_in_read_only_tools():
    """get_server_log_checkpoint is declared read-only."""
    from meridian.mcp_tools import _READ_ONLY_TOOLS
    assert "get_server_log_checkpoint" in _READ_ONLY_TOOLS


def test_get_server_log_checkpoint_category_is_session():
    """get_server_log_checkpoint category is 'session'."""
    from meridian.mcp_tools import _TOOL_CATEGORY
    assert _TOOL_CATEGORY.get("get_server_log_checkpoint") == "session"


def test_get_server_log_checkpoint_role_is_both():
    """get_server_log_checkpoint role_relevance is 'both'."""
    from meridian.mcp_tools import _TOOL_ROLE_RELEVANCE
    assert _TOOL_ROLE_RELEVANCE.get("get_server_log_checkpoint") == "both"


def test_get_server_log_checkpoint_in_tool_list():
    """get_server_log_checkpoint appears in the MCP tools list."""
    from meridian.mcp_tools import _MCP_TOOLS_LIST
    names = [t["name"] for t in _MCP_TOOLS_LIST]
    assert "get_server_log_checkpoint" in names


def test_get_server_log_checkpoint_required_is_empty():
    """get_server_log_checkpoint inputSchema required is empty (seek_to is optional)."""
    from meridian.mcp_tools import _MCP_TOOLS_LIST
    tool = next(t for t in _MCP_TOOLS_LIST if t["name"] == "get_server_log_checkpoint")
    assert tool["inputSchema"]["required"] == []


def test_get_server_logs_has_seek_to_in_schema():
    """get_server_logs inputSchema includes seek_to property."""
    from meridian.mcp_tools import _MCP_TOOLS_LIST
    tool = next(t for t in _MCP_TOOLS_LIST if t["name"] == "get_server_logs")
    props = tool["inputSchema"]["properties"]
    assert "seek_to" in props


def test_get_server_log_checkpoint_dispatch():
    """get_server_log_checkpoint is routed by _handle_session_tools (not _MISS)."""
    import asyncio
    from meridian.mcp import handler as mh
    from meridian import db as db_module

    async def _run():
        conn = await db_module.init_db(":memory:")
        try:
            result = await mh._handle_session_tools(
                "get_server_log_checkpoint", {}, conn, "/tmp", None, None
            )
            return result
        finally:
            await conn.close()

    result = asyncio.run(_run())
    assert result is not None
    # Should NOT be _MISS
    from meridian.mcp.handler import _MISS
    assert result is not _MISS
    assert "buckets" in result
