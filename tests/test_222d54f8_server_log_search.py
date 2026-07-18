"""Tests for 222d54f8 — BM25 FTS index over server_logs ring-buffer.

Coverage:
* Indexing and BM25 search returns real, correctly-ranked hits for realistic
  log content.
* Incremental indexing does not redo unchanged work (verified via sync call
  count and _last_sync_count state).
* since/level filters work correctly (post-BM25).
* Empty/no-match query returns a clean empty result, not an error.
* Ring-buffer eviction consistency: rows pruned from server_logs are removed
  from the FTS index on the next sync call.
* search_server_logs() module-level function works end-to-end.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from meridian.server_log_index import ServerLogFtsIndex, search_server_logs


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_row(
    *,
    level: str = "ERROR",
    logger: str = "meridian.server",
    message: str = "test message",
    exc_text: str | None = None,
    recorded_at: str = "2026-07-17 00:00:00",
    row_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": row_id or str(uuid.uuid4()),
        "level": level,
        "logger": logger,
        "message": message,
        "exc_text": exc_text,
        "recorded_at": recorded_at,
    }


def _make_realistic_rows() -> list[dict[str, Any]]:
    """A small set of realistic server_log rows covering multiple scenarios."""
    return [
        _make_row(
            level="ERROR",
            logger="meridian.hosted",
            message="OAuth token refresh failed: invalid_grant",
            exc_text="Traceback (most recent call last):\n  ...\nValueError: invalid_grant",
            recorded_at="2026-07-17 01:00:00",
        ),
        _make_row(
            level="WARNING",
            logger="meridian.db",
            message="psycopg connection pool exhausted, waiting for slot",
            recorded_at="2026-07-17 01:01:00",
        ),
        _make_row(
            level="EXCEPTION",
            logger="meridian.server",
            message="Unhandled exception in tools/list endpoint",
            exc_text="TimeoutError: tools/list fetch timed out after 5s",
            recorded_at="2026-07-17 01:02:00",
        ),
        _make_row(
            level="ERROR",
            logger="meridian.pg_adapter",
            message="Postgres migration failed: column already exists",
            recorded_at="2026-07-17 01:03:00",
        ),
        _make_row(
            level="WARNING",
            logger="meridian.tunnel",
            message="Tunnel slot heartbeat missed, marking stale",
            recorded_at="2026-07-17 00:30:00",
        ),
    ]


# ---------------------------------------------------------------------------
# ServerLogFtsIndex — unit tests
# ---------------------------------------------------------------------------

@pytest.fixture
def idx():
    """A fresh in-memory ServerLogFtsIndex for each test."""
    pytest.importorskip("duckdb")
    return ServerLogFtsIndex(db_path=":memory:")


def test_sync_indexes_rows(idx):
    rows = _make_realistic_rows()
    total = idx.sync(rows)
    assert total == len(rows)
    assert idx._last_sync_count == len(rows)


def test_search_returns_bm25_hits(idx):
    rows = _make_realistic_rows()
    idx.sync(rows)
    hits = idx.search("OAuth token refresh")
    assert len(hits) >= 1
    # The OAuth row should be the top hit.
    assert "OAuth" in hits[0]["message"] or "oauth" in hits[0]["message"].lower()
    # Every hit has a positive BM25 score.
    for h in hits:
        assert h["bm25"] > 0
        assert h["score"] == h["bm25"]


def test_search_ranks_most_relevant_first(idx):
    rows = _make_realistic_rows()
    idx.sync(rows)
    hits = idx.search("psycopg connection pool")
    assert len(hits) >= 1
    assert "psycopg" in hits[0]["message"].lower() or "pool" in hits[0]["message"].lower()


def test_search_matches_logger_field(idx):
    rows = _make_realistic_rows()
    idx.sync(rows)
    # "pg_adapter" only appears in the logger field, not the message.
    hits = idx.search("pg_adapter")
    assert len(hits) >= 1
    assert hits[0]["logger"] == "meridian.pg_adapter"


def test_search_matches_exc_text(idx):
    rows = _make_realistic_rows()
    idx.sync(rows)
    hits = idx.search("TimeoutError")
    assert len(hits) >= 1
    # The EXCEPTION row has TimeoutError in exc_text.
    matched_ids = {h["id"] for h in hits}
    timeout_row = next(r for r in rows if r.get("exc_text") and "TimeoutError" in r["exc_text"])
    assert timeout_row["id"] in matched_ids


def test_search_empty_query_returns_empty(idx):
    rows = _make_realistic_rows()
    idx.sync(rows)
    hits = idx.search("")
    assert hits == []


def test_search_no_match_returns_empty(idx):
    rows = _make_realistic_rows()
    idx.sync(rows)
    # Use simple short tokens that the Porter stemmer doesn't confuse with any
    # term in the test corpus (DuckDB FTS can spuriously match very long compound
    # tokens due to stemming edge cases, so we use clearly absent short words).
    hits = idx.search("banana kumquat papaya")
    assert hits == []


def test_search_level_filter(idx):
    rows = _make_realistic_rows()
    idx.sync(rows)
    # Search for a broad term, then filter to WARNING only.
    hits = idx.search("meridian", level="WARNING")
    assert len(hits) >= 1
    for h in hits:
        assert h["level"] == "WARNING"


def test_search_since_filter(idx):
    rows = _make_realistic_rows()
    idx.sync(rows)
    # Only rows at 01:00:00 or later.
    hits = idx.search("meridian", since="2026-07-17 01:00:00")
    assert len(hits) >= 1
    for h in hits:
        assert h["recorded_at"] >= "2026-07-17 01:00:00"
    # The tunnel row at 00:30:00 should be excluded.
    all_loggers = {h["logger"] for h in hits}
    assert "meridian.tunnel" not in all_loggers


def test_search_result_shape(idx):
    rows = _make_realistic_rows()
    idx.sync(rows)
    hits = idx.search("OAuth")
    assert len(hits) >= 1
    h = hits[0]
    assert "id" in h
    assert "level" in h
    assert "logger" in h
    assert "message" in h
    assert "exc_text" in h
    assert "recorded_at" in h
    assert "score" in h
    assert "bm25" in h


def test_incremental_sync_no_reindex_when_unchanged(idx):
    """Second sync with identical rows does not write any new rows."""
    rows = _make_realistic_rows()
    idx.sync(rows)
    count_before = idx._last_sync_count

    # Sync the same rows again — should be a no-op at the DB level.
    total2 = idx.sync(rows)
    assert total2 == len(rows)
    assert idx._last_sync_count == count_before

    # FTS is still built; search still works.
    hits = idx.search("OAuth")
    assert len(hits) >= 1


def test_incremental_sync_detects_new_rows(idx):
    """Adding a new row triggers a re-sync and FTS rebuild."""
    rows = _make_realistic_rows()
    idx.sync(rows)

    new_row = _make_row(
        level="ERROR",
        logger="meridian.billing",
        message="Stripe webhook signature verification failed",
        recorded_at="2026-07-17 02:00:00",
    )
    extended = rows + [new_row]
    total2 = idx.sync(extended)
    assert total2 == len(extended)

    # The new row should now be searchable.
    hits = idx.search("Stripe webhook")
    assert len(hits) >= 1
    assert "Stripe" in hits[0]["message"]


def test_ring_buffer_eviction_removes_stale_rows(idx):
    """When server_logs prunes old rows, the FTS index reflects that."""
    rows = _make_realistic_rows()
    idx.sync(rows)

    # Simulate ring-buffer eviction: keep only the 3 most recent rows.
    recent_rows = sorted(rows, key=lambda r: r["recorded_at"], reverse=True)[:3]
    total2 = idx.sync(recent_rows)
    assert total2 == 3

    # The evicted rows should no longer be searchable.
    # "Tunnel slot heartbeat missed" was the oldest (00:30:00) -- evicted.
    hits = idx.search("heartbeat tunnel")
    tunnel_row = next(r for r in rows if r["logger"] == "meridian.tunnel")
    evicted_ids = {tunnel_row["id"]}
    found_ids = {h["id"] for h in hits}
    assert found_ids.isdisjoint(evicted_ids), (
        f"Evicted row(s) still appear in search: {found_ids & evicted_ids}"
    )


def test_ring_buffer_full_eviction_then_new_rows(idx):
    """After a full eviction (empty snapshot), new rows are indexed fresh."""
    rows = _make_realistic_rows()
    idx.sync(rows)

    # Simulate a full eviction.
    idx.sync([])
    hits_after_eviction = idx.search("OAuth")
    assert hits_after_eviction == []

    # Now new rows arrive.
    new_rows = [
        _make_row(message="New OAuth error after eviction", level="ERROR",
                  recorded_at="2026-07-17 03:00:00"),
    ]
    idx.sync(new_rows)
    hits = idx.search("OAuth eviction")
    # The old OAuth row is gone; the new one is findable.
    assert any("eviction" in h["message"].lower() or "oauth" in h["message"].lower()
               for h in hits)


def test_fts_pending_lazy_build_on_first_search(idx):
    """b1789c0d parity: if rows are synced but FTS was pending, search builds it."""
    rows = _make_realistic_rows()
    # Manually simulate _fts_pending by syncing rows then clearing _fts_built.
    idx.sync(rows)
    idx._fts_built = False
    idx._fts_pending = True

    # search() should trigger a lazy FTS build and return real hits.
    hits = idx.search("OAuth")
    assert len(hits) >= 1
    assert idx._fts_built is True
    assert idx._fts_pending is False


# ---------------------------------------------------------------------------
# search_server_logs() module-level function
# ---------------------------------------------------------------------------

def test_module_fn_empty_query(tmp_path):
    pytest.importorskip("duckdb")
    result = search_server_logs(
        [], "", db_path=":memory:",
    )
    assert "error" in result
    assert result["hits"] == []


def test_module_fn_bm25_search(tmp_path):
    pytest.importorskip("duckdb")
    rows = _make_realistic_rows()
    result = search_server_logs(
        rows,
        "OAuth token refresh",
        db_path=":memory:",
    )
    assert result["query"] == "OAuth token refresh"
    assert result["total_in_index"] == len(rows)
    assert result["count"] >= 1
    assert len(result["hits"]) >= 1
    assert "score" in result["hits"][0]


def test_module_fn_level_filter(tmp_path):
    pytest.importorskip("duckdb")
    rows = _make_realistic_rows()
    result = search_server_logs(rows, "meridian", level="ERROR", db_path=":memory:")
    for h in result["hits"]:
        assert h["level"] == "ERROR"


def test_module_fn_since_filter(tmp_path):
    pytest.importorskip("duckdb")
    rows = _make_realistic_rows()
    result = search_server_logs(
        rows, "meridian", since="2026-07-17 01:00:00", db_path=":memory:",
    )
    for h in result["hits"]:
        assert h["recorded_at"] >= "2026-07-17 01:00:00"


def test_module_fn_no_match(tmp_path):
    pytest.importorskip("duckdb")
    rows = _make_realistic_rows()
    result = search_server_logs(rows, "xyzzy_completely_absent", db_path=":memory:")
    assert result["hits"] == []
    assert result["count"] == 0
    # Should not raise; total_in_index still reflects the full sync.
    assert result["total_in_index"] == len(rows)


def test_module_fn_empty_rows_no_error(tmp_path):
    pytest.importorskip("duckdb")
    result = search_server_logs([], "OAuth", db_path=":memory:")
    assert result["hits"] == []
    assert result["total_in_index"] == 0
    assert "error" not in result


def test_module_fn_result_shape(tmp_path):
    pytest.importorskip("duckdb")
    rows = _make_realistic_rows()
    result = search_server_logs(rows, "OAuth", db_path=":memory:")
    assert "query" in result
    assert "total_in_index" in result
    assert "count" in result
    assert "hits" in result


def test_module_fn_incremental_via_persistent_db(tmp_path):
    """Two calls to search_server_logs on the same db_path share the index."""
    pytest.importorskip("duckdb")
    db_path = str(tmp_path / "test_index.duckdb")
    rows = _make_realistic_rows()

    r1 = search_server_logs(rows, "OAuth", db_path=db_path)
    assert r1["total_in_index"] == len(rows)

    # Second call with the same rows — should NOT re-insert any rows.
    # We verify by checking that total_in_index is still len(rows).
    r2 = search_server_logs(rows, "psycopg connection", db_path=db_path)
    assert r2["total_in_index"] == len(rows)
    assert r2["count"] >= 1


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------

def test_search_server_logs_in_read_only_tools():
    """search_server_logs is registered as read-only."""
    from meridian.mcp_tools import _READ_ONLY_TOOLS
    assert "search_server_logs" in _READ_ONLY_TOOLS


def test_search_server_logs_category_is_session():
    """search_server_logs category is 'session'."""
    from meridian.mcp_tools import _TOOL_CATEGORY
    assert _TOOL_CATEGORY.get("search_server_logs") == "session"


def test_search_server_logs_role_is_both():
    """search_server_logs role_relevance is 'both'."""
    from meridian.mcp_tools import _TOOL_ROLE_RELEVANCE
    assert _TOOL_ROLE_RELEVANCE.get("search_server_logs") == "both"


def test_search_server_logs_in_tool_list():
    """search_server_logs appears in the MCP tools list."""
    from meridian.mcp_tools import _MCP_TOOLS_LIST
    names = [t["name"] for t in _MCP_TOOLS_LIST]
    assert "search_server_logs" in names


def test_search_server_logs_required_field_is_query():
    """search_server_logs inputSchema requires only 'query'."""
    from meridian.mcp_tools import _MCP_TOOLS_LIST
    tool = next(t for t in _MCP_TOOLS_LIST if t["name"] == "search_server_logs")
    assert tool["inputSchema"]["required"] == ["query"]


# ---------------------------------------------------------------------------
# MCP handler (integration) — uses the shared `db` fixture from conftest
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_search_server_logs_basic(db, tmp_path):
    """handle_search_server_logs returns correctly shaped results."""
    pytest.importorskip("duckdb")
    from meridian.mcp.handlers.session_tools import handle_search_server_logs
    from meridian import db as db_module

    await db_module.record_server_log(
        db, level="ERROR", logger="meridian.server",
        message="OAuth token refresh failed",
    )
    result = await handle_search_server_logs(
        args={"query": "OAuth token"},
        db=db,
        data_dir=str(tmp_path),
        tenant=None,
        _mcp_tenant_id=None,
    )
    assert "query" in result
    assert "total_in_index" in result
    assert "hits" in result
    assert result["count"] >= 1
    assert any("OAuth" in h["message"] for h in result["hits"])


@pytest.mark.asyncio
async def test_handle_search_server_logs_empty_query(db, tmp_path):
    """handle_search_server_logs with empty query returns error, no exception."""
    from meridian.mcp.handlers.session_tools import handle_search_server_logs

    result = await handle_search_server_logs(
        args={"query": ""},
        db=db,
        data_dir=str(tmp_path),
        tenant=None,
        _mcp_tenant_id=None,
    )
    assert "error" in result
    assert result["hits"] == []


# ---------------------------------------------------------------------------
# 36a401fa — deadline-skip / lazy-pending escape hatch for sync()'s
# _rebuild_fts() call (verifying whether the b1789c0d cold-index failure
# on OutputsFtsIndex was inherited here).
#
# Before this fix, ServerLogFtsIndex.sync() called self._rebuild_fts(con)
# unconditionally whenever rows changed, with NO deadline check at all --
# the same "monolithic full-rebuild, no budget" shape that caused
# b1789c0d/de33589b once OutputsFtsIndex's data source outgrew what a
# synchronous rebuild could do inside the external MCP client timeout.
# server_logs is capped small today (2000-row ring buffer), so the failure
# was not YET reachable in practice, but nothing enforced that invariant.
# ---------------------------------------------------------------------------

class TestSyncDeadlineSkip:
    def test_sync_skips_fts_rebuild_when_deadline_already_passed(self):
        """An already-expired max_seconds must skip _rebuild_fts() entirely,
        writing rows but deferring the FTS build via _fts_pending."""
        pytest.importorskip("duckdb")
        idx = ServerLogFtsIndex(db_path=":memory:")
        fts_call_count = [0]
        real_rebuild_fts = idx._rebuild_fts

        def counting_rebuild_fts(con: Any) -> None:
            fts_call_count[0] += 1
            real_rebuild_fts(con)

        idx._rebuild_fts = counting_rebuild_fts
        try:
            rows = _make_realistic_rows()
            # Deeply-negative budget: deadline is already in the past.
            total = idx.sync(rows, max_seconds=-1.0)
            assert total == len(rows)
            assert fts_call_count[0] == 0, (
                "36a401fa: _rebuild_fts() must be skipped when the sync() "
                "deadline has already passed"
            )
            assert idx._fts_pending is True
            assert idx.last_sync_partial is True
            # Rows were still written even though FTS was deferred.
            assert idx._last_sync_count == len(rows)
        finally:
            idx.close()

    def test_search_lazily_builds_fts_after_deadline_skip(self):
        """After a deadline-skipped sync(), the next search() call performs
        the deferred FTS build with a fresh budget and returns real hits."""
        pytest.importorskip("duckdb")
        idx = ServerLogFtsIndex(db_path=":memory:")
        try:
            rows = _make_realistic_rows()
            idx.sync(rows, max_seconds=-1.0)
            assert idx._fts_built is False
            assert idx._fts_pending is True

            hits = idx.search("OAuth token refresh")
            assert len(hits) >= 1
            assert idx._fts_built is True
            assert idx._fts_pending is False
        finally:
            idx.close()

    def test_sync_default_budget_completes_normally(self):
        """The default (generous) budget must behave exactly as before: FTS
        builds inline within sync(), no partial/pending state left behind."""
        pytest.importorskip("duckdb")
        idx = ServerLogFtsIndex(db_path=":memory:")
        try:
            rows = _make_realistic_rows()
            idx.sync(rows)
            assert idx._fts_built is True
            assert idx._fts_pending is False
            assert idx.last_sync_partial is False
            hits = idx.search("OAuth")
            assert len(hits) >= 1
        finally:
            idx.close()

    def test_module_fn_search_server_logs_surfaces_partial_on_deadline_skip(
        self, tmp_path,
    ):
        """search_server_logs() must surface partial=True when the sync()
        deadline is hit, mirroring search_outputs()'s b1789c0d contract, and
        must NOT return a silent, unexplained empty/ambiguous result.

        Uses a unique tmp_path db_path (not ":memory:") for isolation --
        ":memory:" is a shared cache key (see _index_cache), so reusing it
        across tests would alias onto whatever index another test already
        built for that key.

        Note: with an already-expired deadline (max_seconds=-1.0), sync()
        defers _rebuild_fts() (verified directly against ServerLogFtsIndex in
        test_sync_skips_fts_rebuild_when_deadline_already_passed above), but
        the module function's own immediate follow-up call to index.search()
        has no deadline of its own, so for a small row count it recovers
        inline within the SAME call -- real hits ARE returned here.  The
        durable signal a caller can rely on is `partial=True` (sourced from
        last_sync_partial, which is not reset by search()), confirming the
        FTS rebuild itself was deferred by sync() rather than paid for
        unconditionally, exactly the invariant b1789c0d/de33589b required.
        """
        pytest.importorskip("duckdb")
        db_path = str(tmp_path / "test_partial_signal.duckdb")
        rows = _make_realistic_rows()
        result = search_server_logs(
            rows, "OAuth token refresh", db_path=db_path, max_seconds=-1.0,
        )
        assert result.get("partial") is True
        assert result["total_in_index"] == len(rows)
        # search()'s inline lazy build recovers immediately for this tiny
        # row count, so real hits are still present (not silently dropped).
        assert result["count"] >= 1
        assert "OAuth" in result["hits"][0]["message"]

    def test_module_fn_persistent_db_recovers_after_deadline_skip(self, tmp_path):
        """Using a persistent db_path (so the index is reused across calls,
        as the real MCP handler does), a deadline-skipped first call is
        followed by a normal-budget second call that still returns real hits,
        and the partial signal clears once a sync() completes within budget."""
        pytest.importorskip("duckdb")
        db_path = str(tmp_path / "test_deadline.duckdb")
        rows = _make_realistic_rows()

        result1 = search_server_logs(
            rows, "OAuth token refresh", db_path=db_path, max_seconds=-1.0,
        )
        assert result1.get("partial") is True

        # Second call, same persistent index, default (generous) budget --
        # sync() completes within budget this time, so partial clears.
        result2 = search_server_logs(rows, "OAuth token refresh", db_path=db_path)
        assert result2["count"] >= 1
        assert result2.get("partial") is not True
        assert result2.get("fts_pending") is not True
        assert "OAuth" in result2["hits"][0]["message"]
