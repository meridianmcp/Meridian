"""50cdd9b4 — empirical verification of the file/symbol claim mechanism under
concurrent load.

These tests drive the real db-layer primitives (``claim_file`` / ``claim_symbol`` /
``get_file_claims`` / ``release_file``) with multiple simulated sessions competing for
the same file, and assert the documented guarantees actually hold:

  (a) write is exclusive  — a second session's write claim is refused while the file
      is held, and the response names the current holder.
  (b) read is shared      — many readers coexist; a writer is blocked while any reader
      holds the file, and a fresh reader is blocked while a writer holds it.
  (c) symbol resolution   — line ranges are accurate for BOTH parsers Meridian ships:
      Python ``ast`` and TypeScript tree-sitter (the two languages in this repo's own
      mixed stack — server in Python, ``dashboard.ts`` in TypeScript), and overlapping
      symbol claims are refused while a non-overlapping one succeeds.
  (d) TTL expiry          — a stale lock is auto-released on next access, via BOTH the
      explicit ``expires_at`` TTL and the stale-session-heartbeat path.

The read/write matrix and Python+JS extraction are partly covered elsewhere
(``test_cov_handler.test_read_write_claim_distinction``,
``test_core.test_extract_symbols_python_and_js``); the specifically new coverage here is
the TypeScript tree-sitter path (this repo's frontend language) and the explicit
``expires_at`` TTL sweep.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from meridian import db as db_module
from meridian.symbols import detect_language, extract_symbols


def _hours_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=n)).strftime("%Y-%m-%d %H:%M:%S")


# Real-ish TypeScript so the tree-sitter path resolves interface/class/function nodes.
_TS_SRC = (
    "interface Claim {\n"                      # 1
    "  path: string;\n"                        # 2
    "}\n"                                       # 3
    "class ClaimStore {\n"                      # 4
    "  put(c: Claim): void {}\n"               # 5
    "}\n"                                       # 6
    "function resolve(x: number): number {\n"  # 7
    "  return x + 1;\n"                         # 8
    "}\n"                                       # 9
)

_PY_SRC = (
    "class AuthRouter:\n"     # 1
    "    def login(self):\n"  # 2
    "        return 1\n"      # 3
    "\n"                      # 4
    "def helper():\n"        # 5
    "    return 3\n"         # 6
)


def _require_ts_grammar() -> None:
    """Skip when the TypeScript tree-sitter grammar isn't installed in this env."""
    if not extract_symbols("x.ts", _TS_SRC):
        pytest.skip("TypeScript tree-sitter grammar not available in this environment")


# ---------------------------------------------------------------------------
# (a) write is exclusive — write-vs-write
# ---------------------------------------------------------------------------
async def test_write_claim_is_exclusive(db):
    p = await db_module.create_project(db, "claim-conc-ww")
    s1 = await db_module.register_session(db, p["id"], "writer-1")
    s2 = await db_module.register_session(db, p["id"], "writer-2")
    path = "meridian/server.py"

    r1 = await db_module.claim_file(db, path, s1["id"], mode="write")
    assert r1["claimed"] is True
    assert r1["claim_mode"] == "write"

    # a second writer is refused; the response points at the current holder
    r2 = await db_module.claim_file(db, path, s2["id"], mode="write")
    assert r2["claimed"] is False
    assert r2["holder_session_id"] == s1["id"]

    # the holder can re-claim its own lock (idempotent refresh)
    assert (await db_module.claim_file(db, path, s1["id"], mode="write"))["claimed"] is True

    # after release, the other session can take it
    assert await db_module.release_file(db, path, s1["id"]) is True
    r2b = await db_module.claim_file(db, path, s2["id"], mode="write")
    assert r2b["claimed"] is True
    assert r2b["session_id"] == s2["id"]


# ---------------------------------------------------------------------------
# (b) read is shared; write excludes readers and vice-versa
# ---------------------------------------------------------------------------
async def test_read_shared_write_exclusive_matrix(db):
    p = await db_module.create_project(db, "claim-conc-rw")
    r_a = await db_module.register_session(db, p["id"], "reader-a")
    r_b = await db_module.register_session(db, p["id"], "reader-b")
    w = await db_module.register_session(db, p["id"], "writer")
    path = "meridian/db/__init__.py"

    # two readers coexist on the same file
    ra = await db_module.claim_file(db, path, r_a["id"], mode="read")
    assert ra["claimed"] is True and ra["claim_mode"] == "read"
    rb = await db_module.claim_file(db, path, r_b["id"], mode="read")
    assert rb["claimed"] is True
    assert set(rb["readers"]) == {r_a["id"], r_b["id"]}
    assert rb["reader_count"] == 2

    # a writer is blocked while readers hold the file
    wr = await db_module.claim_file(db, path, w["id"], mode="write")
    assert wr["claimed"] is False
    assert wr["reason"] == "read_locked"
    assert set(wr["read_claims"]) == {r_a["id"], r_b["id"]}

    # once both readers release, the writer acquires it
    await db_module.release_file(db, path, r_a["id"])
    await db_module.release_file(db, path, r_b["id"])
    assert (await db_module.claim_file(db, path, w["id"], mode="write"))["claimed"] is True

    # and now a fresh reader is blocked by the exclusive writer
    rc = await db_module.claim_file(db, path, r_a["id"], mode="read")
    assert rc["claimed"] is False
    assert rc["reason"] == "write_locked"
    assert rc["holder_session_id"] == w["id"]


# ---------------------------------------------------------------------------
# (c) symbol resolution — Python ast AND TypeScript tree-sitter
# ---------------------------------------------------------------------------
def test_symbol_resolution_python_ast():
    assert detect_language("x.py") == "python"
    by_name = {s["name"]: s for s in extract_symbols("x.py", _PY_SRC)}
    # class spans its method; the nested method name is dotted (ast path)
    assert (by_name["AuthRouter"]["line_start"], by_name["AuthRouter"]["line_end"]) == (1, 3)
    assert by_name["AuthRouter.login"]["line_start"] == 2
    assert (by_name["helper"]["line_start"], by_name["helper"]["line_end"]) == (5, 6)


def test_symbol_resolution_typescript_tree_sitter():
    _require_ts_grammar()
    # This repo's frontend is TypeScript (dashboard.ts); the TS tree-sitter path is
    # what actually guards symbol-level claims on the frontend.
    assert detect_language("x.ts") == "typescript"
    assert detect_language("x.tsx") == "tsx"
    by_name = {s["name"]: s for s in extract_symbols("x.ts", _TS_SRC)}
    # 1-based inclusive line ranges for interface / class / function
    assert (by_name["Claim"]["line_start"], by_name["Claim"]["line_end"]) == (1, 3)
    assert (by_name["ClaimStore"]["line_start"], by_name["ClaimStore"]["line_end"]) == (4, 6)
    assert (by_name["resolve"]["line_start"], by_name["resolve"]["line_end"]) == (7, 9)


async def test_symbol_claim_overlap_refused_typescript(db):
    _require_ts_grammar()
    p = await db_module.create_project(db, "claim-conc-sym")
    s1 = await db_module.register_session(db, p["id"], "sym-a")
    s2 = await db_module.register_session(db, p["id"], "sym-b")
    path = "meridian/static/dashboard.ts"

    # s1 claims the class (lines 4-6)
    r1 = await db_module.claim_symbol(db, s1["id"], path, "ClaimStore", _TS_SRC)
    assert r1["claimed"] is True
    assert (r1["line_start"], r1["line_end"]) == (4, 6)

    # s2 can safely claim a NON-overlapping symbol in the same file (lines 7-9)
    r2 = await db_module.claim_symbol(db, s2["id"], path, "resolve", _TS_SRC)
    assert r2["claimed"] is True
    assert (r2["line_start"], r2["line_end"]) == (7, 9)

    # s2 claiming the same (overlapping) symbol is refused
    r3 = await db_module.claim_symbol(db, s2["id"], path, "ClaimStore", _TS_SRC)
    assert r3["claimed"] is False
    assert r3["reason"] == "symbol_conflict"


# ---------------------------------------------------------------------------
# (d) TTL expiry — explicit expires_at AND stale-heartbeat
# ---------------------------------------------------------------------------
async def test_explicit_ttl_expiry_releases_write_lock(db):
    p = await db_module.create_project(db, "claim-conc-ttl")
    s1 = await db_module.register_session(db, p["id"], "holder")
    s2 = await db_module.register_session(db, p["id"], "next")
    path = "meridian/pointers.py"

    assert (await db_module.claim_file(db, path, s1["id"], mode="write"))["claimed"] is True
    # blocked while the lock is live
    assert (await db_module.claim_file(db, path, s2["id"], mode="write"))["claimed"] is False

    # back-date the lock past its TTL; the next access must sweep it lazily
    await db.execute(
        "UPDATE file_locks SET expires_at = ? WHERE session_id = ?",
        (_hours_ago(3), s1["id"]),
    )
    await db.commit()

    claims = await db_module.get_file_claims(db, path)
    assert claims["file_lock"] is None  # expired + reaped on read

    # the waiting session can now claim it
    assert (await db_module.claim_file(db, path, s2["id"], mode="write"))["claimed"] is True


async def test_stale_heartbeat_expiry_releases_lock(db):
    p = await db_module.create_project(db, "claim-conc-hb")
    s1 = await db_module.register_session(db, p["id"], "crashed")
    s2 = await db_module.register_session(db, p["id"], "survivor")
    path = "meridian/goal_md.py"

    assert (await db_module.claim_file(db, path, s1["id"], mode="write"))["claimed"] is True
    # simulate a crashed/orphaned session whose lock was never released
    await db.execute(
        "UPDATE sessions SET last_seen = ? WHERE id = ?",
        (_hours_ago(3), s1["id"]),
    )
    await db.commit()

    # the next claim attempt triggers expire_file_locks(), reaping the orphaned lock
    r = await db_module.claim_file(db, path, s2["id"], mode="write")
    assert r["claimed"] is True
    assert r["session_id"] == s2["id"]
