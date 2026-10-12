"""Symbol-lock hardening: fallback markers, dead-holder expiry, start-session
warnings, and transactional overlap checks.

Covers four small fixes to the symbol-level locking path:

1. A whole-file fallback from ``claim_file`` (symbol given, but unparseable or
   without source) is marked with ``claim_granularity="file"`` and a
   ``fallback_reason``. A real symbol-range claim is ``claim_granularity="symbol"``.
2. ``get_symbol_claims`` / ``get_file_claims`` hide claims whose holder is not
   live under the same rule the conflict check uses, and the claim path
   soft-releases dead holders' rows before the conflict check.
3. ``get_file_conflict_warnings`` includes live symbol and read claims and uses
   the 2-hour enforcement window instead of 10 minutes.
4. The overlap check and the insert in ``claim_symbol`` run in one transaction,
   so concurrent overlapping claims cannot both succeed.
"""
import asyncio
import shutil

import pytest

import meridian.db as db_module
import meridian.db.locks as locks

_SRC = (
    "class AuthRouter:\n"
    "    def login(self):\n"
    "        return 1\n"
    "\n"
    "class MCPHandler:\n"
    "    def handle(self):\n"
    "        return 2\n"
)
_PATH = "pkg/mod.py"


async def _project_and_sessions(db, name, n=2):
    p = await db_module.create_project(db, name)
    sessions = [
        await db_module.register_session(db, p["id"], f"{name}-s{i}")
        for i in range(n)
    ]
    return p, sessions


async def _set_session(db, session_id, **fields):
    for col, val in fields.items():
        await db.execute(f"UPDATE sessions SET {col} = ? WHERE id = ?", (val, session_id))
    await db.commit()


# ---------------------------------------------------------------------------
# Fix 1 — whole-file fallbacks are marked
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claim_file_unparseable_fallback_is_marked(db):
    import meridian.server as srv

    _, (s1, _s2) = await _project_and_sessions(db, "fb-unparseable")
    res = await srv._dispatch_mcp_tool(
        "claim_file",
        {"session_id": s1["id"], "file_path": "README.txt",
         "symbol": "Foo", "content": "not code"},
        db, "/tmp",
    )
    assert res["claimed"] is True
    assert res["claim_granularity"] == "file"
    assert res["fallback_reason"] == "unparseable"


@pytest.mark.asyncio
async def test_claim_file_symbol_without_source_is_marked(db):
    import meridian.server as srv

    _, (s1, _s2) = await _project_and_sessions(db, "fb-nosource")
    res = await srv._dispatch_mcp_tool(
        "claim_file",
        {"session_id": s1["id"], "file_path": _PATH, "symbol": "AuthRouter"},
        db, "/tmp",
    )
    assert res["claimed"] is True
    assert res["claim_granularity"] == "file"
    assert res["fallback_reason"] == "no_source_supplied"


@pytest.mark.asyncio
async def test_claim_file_whole_file_has_no_fallback_reason(db):
    import meridian.server as srv

    _, (s1, _s2) = await _project_and_sessions(db, "fb-plain")
    res = await srv._dispatch_mcp_tool(
        "claim_file",
        {"session_id": s1["id"], "file_path": _PATH},
        db, "/tmp",
    )
    assert res["claimed"] is True
    assert res["claim_granularity"] == "file"
    assert res.get("fallback_reason") is None


@pytest.mark.asyncio
async def test_claim_file_parseable_symbol_is_symbol_granularity(db):
    import meridian.server as srv

    _, (s1, _s2) = await _project_and_sessions(db, "fb-symbol")
    res = await srv._dispatch_mcp_tool(
        "claim_file",
        {"session_id": s1["id"], "file_path": _PATH,
         "symbol": "AuthRouter", "content": _SRC},
        db, "/tmp",
    )
    assert res["claimed"] is True
    assert res["claim_granularity"] == "symbol"
    assert res.get("fallback_reason") is None


# ---------------------------------------------------------------------------
# Fix 2 — dead holders are hidden and soft-released
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_idle_holder_symbol_claim_is_not_listed(db):
    _, (s1, _s2) = await _project_and_sessions(db, "dead-idle")
    claimed = await db_module.claim_symbol(db, s1["id"], _PATH, "AuthRouter", _SRC)
    assert claimed["claimed"] is True

    await _set_session(db, s1["id"], status="idle")

    assert await db_module.get_symbol_claims(db, _PATH) == []
    assert (await db_module.get_file_claims(db, _PATH))["symbol_claims"] == []


@pytest.mark.asyncio
async def test_stale_heartbeat_symbol_claim_is_not_listed(db):
    _, (s1, _s2) = await _project_and_sessions(db, "dead-stale")
    await db_module.claim_symbol(db, s1["id"], _PATH, "AuthRouter", _SRC)

    await _set_session(db, s1["id"], last_seen="2020-01-01 00:00:00")

    assert await db_module.get_symbol_claims(db, _PATH) == []


@pytest.mark.asyncio
async def test_new_claim_succeeds_after_holder_dies_and_row_is_soft_released(db):
    _, (s1, s2) = await _project_and_sessions(db, "dead-reclaim")
    await db_module.claim_symbol(db, s1["id"], _PATH, "AuthRouter", _SRC)
    await _set_session(db, s1["id"], status="idle")

    fresh = await db_module.claim_symbol(db, s2["id"], _PATH, "AuthRouter", _SRC)
    assert fresh["claimed"] is True

    cur = await db.execute(
        "SELECT released_at FROM file_symbol_claims WHERE session_id = ? AND file_path = ?",
        (s1["id"], _PATH),
    )
    rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] is not None  # soft-released, history kept


# ---------------------------------------------------------------------------
# Fix 3 — start-session warnings cover symbol/read claims, 2-hour window
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_conflict_warnings_include_live_symbol_claim(db):
    p, (s1, s2) = await _project_and_sessions(db, "warn-symbol")
    await db_module.claim_symbol(db, s1["id"], _PATH, "AuthRouter", _SRC)

    warnings = await db_module.get_file_conflict_warnings(db, p["id"], s2["id"])
    assert any(_PATH in w and "AuthRouter" in w for w in warnings), warnings


@pytest.mark.asyncio
async def test_conflict_warnings_drop_symbol_claim_outside_window(db):
    p, (s1, s2) = await _project_and_sessions(db, "warn-window")
    await db_module.claim_symbol(db, s1["id"], _PATH, "AuthRouter", _SRC)
    await _set_session(db, s1["id"], last_seen="2020-01-01 00:00:00")

    warnings = await db_module.get_file_conflict_warnings(db, p["id"], s2["id"])
    assert not any("AuthRouter" in w for w in warnings), warnings


@pytest.mark.asyncio
async def test_conflict_warnings_include_live_read_claim(db):
    p, (s1, s2) = await _project_and_sessions(db, "warn-read")
    await locks.claim_file(db, _PATH, s1["id"], mode="read")

    warnings = await db_module.get_file_conflict_warnings(db, p["id"], s2["id"])
    assert any(_PATH in w and "read" in w.lower() for w in warnings), warnings

    # An expired read claim is not live and must not be reported.
    await db.execute("UPDATE file_read_claims SET expires_at = datetime('now', '-1 hours')")
    await db.commit()
    warnings = await db_module.get_file_conflict_warnings(db, p["id"], s2["id"])
    assert not any(_PATH in w and "read" in w.lower() for w in warnings), warnings


@pytest.mark.asyncio
async def test_conflict_warnings_file_lock_uses_two_hour_window(db):
    p, (s1, s2) = await _project_and_sessions(db, "warn-filelock")
    await locks.claim_file(db, _PATH, s1["id"])
    # 30 minutes idle is inside the 2-hour enforcement window, so it still warns.
    await db.execute("UPDATE sessions SET last_seen = datetime('now', '-30 minutes') WHERE id = ?",
                     (s1["id"],))
    await db.commit()

    warnings = await db_module.get_file_conflict_warnings(db, p["id"], s2["id"])
    assert any(_PATH in w for w in warnings), warnings


# ---------------------------------------------------------------------------
# Fix 4 — overlap check and insert share one transaction
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_overlap_check_runs_inside_a_transaction(db, monkeypatch):
    _, (s1, s2) = await _project_and_sessions(db, "tx-check")
    await db_module.claim_symbol(db, s1["id"], _PATH, "AuthRouter", _SRC)

    seen: list[bool] = []
    real_check = locks._live_symbol_claims_for_file

    async def spy(conn, file_path, exclude_session_id=None):
        seen.append(bool(conn.in_transaction))
        return await real_check(conn, file_path, exclude_session_id)

    monkeypatch.setattr(locks, "_live_symbol_claims_for_file", spy)
    blocked = await db_module.claim_symbol(db, s2["id"], _PATH, "AuthRouter", _SRC)

    assert blocked["claimed"] is False
    assert blocked["reason"] == "symbol_conflict"
    assert seen and all(seen), "overlap check must run inside the claim transaction"


@pytest.mark.asyncio
async def test_second_overlapping_claim_is_refused_in_sequence(db):
    _, (s1, s2) = await _project_and_sessions(db, "tx-seq")
    first = await db_module.claim_symbol(db, s1["id"], _PATH, "AuthRouter", _SRC)
    second = await db_module.claim_symbol(db, s2["id"], _PATH, "AuthRouter", _SRC)

    assert first["claimed"] is True
    assert second["claimed"] is False
    assert second["reason"] == "symbol_conflict"
    live = await db_module.get_symbol_claims(db, _PATH)
    assert [c["session_id"] for c in live] == [s1["id"]]


@pytest.mark.asyncio
async def test_concurrent_overlapping_claims_one_wins_on_one_connection(db):
    _, (s1, s2) = await _project_and_sessions(db, "tx-gather")
    results = await asyncio.gather(
        db_module.claim_symbol(db, s1["id"], _PATH, "AuthRouter", _SRC),
        db_module.claim_symbol(db, s2["id"], _PATH, "AuthRouter", _SRC),
    )
    assert sorted(r["claimed"] for r in results) == [False, True]


@pytest.mark.asyncio
async def test_overlapping_claim_from_second_connection_cannot_slip_between_check_and_insert(
    request, tmp_path, monkeypatch
):
    """Deterministic interleaving on a shared SQLite file.

    Connection A runs its overlap check (which finds nothing) and then pauses.
    While A is paused, connection B issues an overlapping claim. Without one
    transaction covering check and insert, B also finds nothing, inserts, and
    then A inserts too: both succeed. With the check and insert in one write
    transaction, B waits for A's commit, re-checks, and is refused.
    """
    import aiosqlite

    template = request.getfixturevalue("_sqlite_schema_template")
    db_path = tmp_path / "shared-locks.db"
    shutil.copy(template, db_path)

    conn_a = await aiosqlite.connect(str(db_path))
    conn_b = await aiosqlite.connect(str(db_path))
    try:
        for conn in (conn_a, conn_b):
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA busy_timeout = 5000")
        _, (s1, s2) = await _project_and_sessions(conn_a, "tx-two-conn")

        real_check = locks._live_symbol_claims_for_file
        b_task: list[asyncio.Future] = []

        async def pausing_check(conn, file_path, exclude_session_id=None):
            rows = await real_check(conn, file_path, exclude_session_id)
            if conn is conn_a and not b_task:
                b_task.append(asyncio.ensure_future(
                    db_module.claim_symbol(conn_b, s2["id"], _PATH, "AuthRouter", _SRC)
                ))
                await asyncio.sleep(0.3)  # let B run to completion if it can
            return rows

        monkeypatch.setattr(locks, "_live_symbol_claims_for_file", pausing_check)

        first = await db_module.claim_symbol(conn_a, s1["id"], _PATH, "AuthRouter", _SRC)
        second = await b_task[0]
        assert sorted([first["claimed"], second["claimed"]]) == [False, True]
    finally:
        await conn_a.close()
        await conn_b.close()
