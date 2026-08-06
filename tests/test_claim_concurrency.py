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

import asyncio
import os
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


# ---------------------------------------------------------------------------
# 56e9b3c7 — autonomous stale-claim reconciliation: claim_sprint_item's
# inline auto-reset, and reconcile_stale_claims' bulk project/version-scoped
# sweep (dry-run, bounded batch, two-project isolation, race safety,
# recovery). Every scenario below is driven ONLY against the disposable
# in-memory `db` test fixture — synthetic projects/items/sessions created
# inline, never any real Meridian project — and no test ever passes
# dry_run=False against anything but that throwaway fixture.
# ---------------------------------------------------------------------------


async def test_claim_sprint_item_autonomously_reconciles_a_proven_stale_claim(db):
    """The core behavioral fix: a NEW claim attempt against a genuinely
    abandoned claim (explicitly closed owning session) must succeed
    immediately — no ValueError, no manual force-reclaim step — because
    claim_sprint_item itself now reconciles it inline before raising."""
    p = await db_module.create_project(db, "reconcile-autonomous")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "abandoned work")
    dead_owner = await db_module.register_session(db, p["id"], "dead-owner")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=dead_owner["id"])
    await db.execute("UPDATE sessions SET status = 'closed' WHERE id = ?", (dead_owner["id"],))
    await db.commit()

    new_owner = await db_module.register_session(db, p["id"], "rescuer")
    reclaimed = await db_module.claim_sprint_item(db, p["id"], item["id"], actor=new_owner["id"])
    assert reclaimed["status"] == "in_progress"
    assert reclaimed["actor"] == new_owner["id"]
    assert reclaimed["stall_count"] == 1

    # Audited: an action_audit_log row records the reconciliation.
    audit_rows = await db_module.get_action_audit_log(
        db, project_id=p["id"], event_type=db_module.RECONCILE_STALE_CLAIM_AUDIT_EVENT,
    )
    assert len(audit_rows) == 1
    assert audit_rows[0]["actor"] == new_owner["id"]


async def test_claim_sprint_item_never_auto_reconciles_an_active_claim(db):
    """Regression safety: a claim under a session with a live heartbeat must
    keep raising ValueError exactly as before — autonomous reconciliation
    must never silently steal a genuinely active claim."""
    p = await db_module.create_project(db, "reconcile-no-touch-active")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "still working")
    owner = await db_module.register_session(db, p["id"], "live-owner")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])

    with pytest.raises(ValueError, match="in_progress"):
        await db_module.claim_sprint_item(db, p["id"], item["id"], actor="someone-else")

    unchanged = await db_module.get_sprint_item(db, item["id"])
    assert unchanged["status"] == "in_progress"
    assert unchanged["actor"] == owner["id"]


async def test_reconcile_stale_claims_dry_run_reports_without_mutating(db):
    p = await db_module.create_project(db, "reconcile-dry-run")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "dry run me")
    owner = await db_module.register_session(db, p["id"], "dry-run-owner")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])
    await db.execute("UPDATE sessions SET status = 'closed' WHERE id = ?", (owner["id"],))
    await db.commit()

    report = await db_module.reconcile_stale_claims(db, p["id"], dry_run=True)
    assert report["dry_run"] is True
    assert len(report["stale"]) == 1
    assert report["stale"][0]["item_id"] == item["id"]
    assert report["reset"] == []  # nothing written

    # Item is untouched — still in_progress under the original (dead) owner.
    unchanged = await db_module.get_sprint_item(db, item["id"])
    assert unchanged["status"] == "in_progress"
    assert unchanged["actor"] == owner["id"]


async def test_reconcile_stale_claims_live_run_resets_and_releases_locks(db):
    p = await db_module.create_project(db, "reconcile-live-run")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "reset me", touches_resources=["file:reconcile_me.py"],
        prospect_bypass=True,
    )
    owner = await db_module.register_session(db, p["id"], "live-run-owner")
    # db.claim_sprint_item itself doesn't acquire resource locks (that's the
    # MCP handler layer's job, via _sprint_item_resource_claim_gate) — claim
    # the declared file lock directly under the same owner session, exactly
    # as an executor's real claim_sprint_item MCP call would end up doing.
    assert (await db_module.claim_file(db, "reconcile_me.py", owner["id"]))["claimed"] is True
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])
    assert (await db_module.get_file_claims(db, "reconcile_me.py"))["file_lock"] is not None
    await db.execute("UPDATE sessions SET status = 'closed' WHERE id = ?", (owner["id"],))
    await db.commit()

    report = await db_module.reconcile_stale_claims(db, p["id"], dry_run=False, actor="sweeper")
    assert len(report["reset"]) == 1
    assert report["reset"][0]["item_id"] == item["id"]
    assert "file:reconcile_me.py" in report["reset"][0]["released_resources"]

    reset_item = await db_module.get_sprint_item(db, item["id"])
    assert reset_item["status"] == "pending"
    assert reset_item["claimed_at"] is None
    # The lock the abandoned claim held is released — a new session can claim it.
    assert (await db_module.get_file_claims(db, "reconcile_me.py"))["file_lock"] is None


async def test_reconcile_stale_claims_two_project_isolation(db):
    """A sweep scoped to project A must never classify or touch project B's
    stale claims, even though both look identical (closed owning session)."""
    pa = await db_module.create_project(db, "reconcile-isolation-a")
    pb = await db_module.create_project(db, "reconcile-isolation-b")
    item_a = await db_module.add_sprint_item(db, pa["id"], "v1", "a's item")
    item_b = await db_module.add_sprint_item(db, pb["id"], "v1", "b's item")
    owner_a = await db_module.register_session(db, pa["id"], "owner-a")
    owner_b = await db_module.register_session(db, pb["id"], "owner-b")
    await db_module.claim_sprint_item(db, pa["id"], item_a["id"], actor=owner_a["id"])
    await db_module.claim_sprint_item(db, pb["id"], item_b["id"], actor=owner_b["id"])
    await db.execute(
        "UPDATE sessions SET status = 'closed' WHERE id IN (?, ?)",
        (owner_a["id"], owner_b["id"]),
    )
    await db.commit()

    report = await db_module.reconcile_stale_claims(db, pa["id"], dry_run=False)
    assert [r["item_id"] for r in report["reset"]] == [item_a["id"]]

    # b's item was never scanned, let alone reset.
    b_untouched = await db_module.get_sprint_item(db, item_b["id"])
    assert b_untouched["status"] == "in_progress"
    assert b_untouched["actor"] == owner_b["id"]


async def test_reconcile_stale_claims_bounded_batch_truncates(db):
    p = await db_module.create_project(db, "reconcile-batch")
    items = []
    for i in range(5):
        # force=True — near-duplicate titles ("stale item 0".."stale item 4")
        # would otherwise trip add_sprint_item's own duplicate-title guard
        # (b0d42ef6) against the earlier open items in this same loop.
        it = await db_module.add_sprint_item(db, p["id"], "v1", f"stale item {i}", force=True)
        owner = await db_module.register_session(db, p["id"], f"owner-{i}")
        await db_module.claim_sprint_item(db, p["id"], it["id"], actor=owner["id"])
        await db.execute("UPDATE sessions SET status = 'closed' WHERE id = ?", (owner["id"],))
        items.append(it)
    await db.commit()

    report = await db_module.reconcile_stale_claims(db, p["id"], dry_run=True, max_batch=2)
    assert report["max_batch"] == 2
    assert report["candidates_total"] == 5
    assert report["scanned"] == 2
    assert report["truncated"] is True
    assert len(report["stale"]) == 2


async def test_reconcile_stale_claims_version_scope(db):
    p = await db_module.create_project(db, "reconcile-version-scope")
    v1_item = await db_module.add_sprint_item(db, p["id"], "v1", "v1 stale")
    v2_item = await db_module.add_sprint_item(db, p["id"], "v2", "v2 stale")
    owner1 = await db_module.register_session(db, p["id"], "v1-owner")
    owner2 = await db_module.register_session(db, p["id"], "v2-owner")
    await db_module.claim_sprint_item(db, p["id"], v1_item["id"], actor=owner1["id"])
    await db_module.claim_sprint_item(db, p["id"], v2_item["id"], actor=owner2["id"])
    await db.execute(
        "UPDATE sessions SET status = 'closed' WHERE id IN (?, ?)",
        (owner1["id"], owner2["id"]),
    )
    await db.commit()

    report = await db_module.reconcile_stale_claims(db, p["id"], version="v1", dry_run=True)
    assert [v["item_id"] for v in report["stale"]] == [v1_item["id"]]


async def test_reconcile_stale_claims_never_touches_active_or_ambiguous(db):
    p = await db_module.create_project(db, "reconcile-active-ambiguous")
    active_item = await db_module.add_sprint_item(db, p["id"], "v1", "active")
    ambiguous_item = await db_module.add_sprint_item(db, p["id"], "v1", "ambiguous")
    live_owner = await db_module.register_session(db, p["id"], "still-alive")
    await db_module.claim_sprint_item(db, p["id"], active_item["id"], actor=live_owner["id"])
    # No actor at all -> ambiguous, never touchable.
    await db_module.claim_sprint_item(db, p["id"], ambiguous_item["id"])

    report = await db_module.reconcile_stale_claims(db, p["id"], dry_run=False)
    assert report["stale"] == []
    assert report["reset"] == []
    assert {v["item_id"] for v in report["active"]} == {active_item["id"]}
    assert {v["item_id"] for v in report["ambiguous"]} == {ambiguous_item["id"]}

    still_active = await db_module.get_sprint_item(db, active_item["id"])
    still_ambiguous = await db_module.get_sprint_item(db, ambiguous_item["id"])
    assert still_active["status"] == "in_progress"
    assert still_ambiguous["status"] == "in_progress"


async def test_reconcile_recovery_item_is_claimable_and_completable_after_reset(db):
    """End-to-end recovery: a proven-stale claim, once reset, flows all the
    way through a fresh claim + completion — nothing about the item is left
    in a broken state by reconciliation."""
    p = await db_module.create_project(db, "reconcile-recovery")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "recover me")
    dead_owner = await db_module.register_session(db, p["id"], "recovery-dead-owner")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=dead_owner["id"])
    await db.execute("UPDATE sessions SET status = 'closed' WHERE id = ?", (dead_owner["id"],))
    await db.commit()

    await db_module.reconcile_stale_claims(db, p["id"], dry_run=False)
    recovered = await db_module.get_sprint_item(db, item["id"])
    assert recovered["status"] == "pending"

    rescuer = await db_module.register_session(db, p["id"], "recovery-rescuer")
    claimed = await db_module.claim_sprint_item(db, p["id"], item["id"], actor=rescuer["id"])
    assert claimed["status"] == "in_progress"
    done = await db_module.complete_sprint_item(db, p["id"], item["id"], actor=rescuer["id"])
    assert done["status"] == "done"


async def test_reconcile_stale_claims_concurrent_sweeps_reset_exactly_once(db):
    """Race safety: N concurrent reconcile_stale_claims sweeps against the
    SAME stale item must reset it exactly once — the atomic from_statuses
    guard in _transition_status (via _reset_stale_claim) must reject every
    loser as a clean no-op, never double-release locks or double-audit."""
    p = await db_module.create_project(db, "reconcile-race-sweep")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "raced reset")
    owner = await db_module.register_session(db, p["id"], "raced-owner")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])
    await db.execute("UPDATE sessions SET status = 'closed' WHERE id = ?", (owner["id"],))
    await db.commit()

    results = await asyncio.gather(*[
        db_module.reconcile_stale_claims(db, p["id"], dry_run=False) for _ in range(5)
    ])
    total_reset = sum(len(r["reset"]) for r in results)
    assert total_reset == 1, f"expected exactly one winning reset, got {total_reset}"

    audit_rows = await db_module.get_action_audit_log(
        db, project_id=p["id"], event_type=db_module.RECONCILE_STALE_CLAIM_AUDIT_EVENT,
    )
    assert len(audit_rows) == 1

    final = await db_module.get_sprint_item(db, item["id"])
    assert final["status"] == "pending"


async def test_claim_sprint_item_concurrent_race_against_stale_claim_exactly_one_winner(db):
    """Real end-to-end race: N sessions concurrently attempt to claim the
    SAME abandoned item via claim_sprint_item (the autonomous, inline
    reconciliation path). Exactly one must win a real in_progress claim; the
    rest must lose cleanly (ValueError), never silently succeed twice."""
    p = await db_module.create_project(db, "reconcile-race-claim")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "raced reclaim")
    dead_owner = await db_module.register_session(db, p["id"], "raced-dead-owner")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=dead_owner["id"])
    await db.execute("UPDATE sessions SET status = 'closed' WHERE id = ?", (dead_owner["id"],))
    await db.commit()

    rescuers = [
        await db_module.register_session(db, p["id"], f"rescuer-{i}") for i in range(5)
    ]

    async def _attempt(rescuer_id):
        try:
            return await db_module.claim_sprint_item(db, p["id"], item["id"], actor=rescuer_id)
        except ValueError:
            return None

    results = await asyncio.gather(*[_attempt(r["id"]) for r in rescuers])
    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"expected exactly 1 winner, got {len(winners)}: {winners}"

    final = await db_module.get_sprint_item(db, item["id"])
    assert final["status"] == "in_progress"
    assert final["actor"] == winners[0]["actor"]


# ---------------------------------------------------------------------------
# 56e9b3c7 — worktree-activity signal, including the self-hosted-only
# owner-PID liveness check (mirrors worktree_cleanup's own liveness gate).
# A live, registered worktree is a VETO against auto-reset even when the
# claiming session's heartbeat has gone fully cold and no task_log evidence
# exists — this is the "preserve legitimate long-running work" guarantee for
# a quiet executor that never calls log_task.
# ---------------------------------------------------------------------------


async def test_classify_dead_owner_pid_counts_as_worktree_corroborator(db, tmp_path):
    """A worktree registered for this claim but whose recorded owner PID is
    confirmed dead (self-hosted only, via repo_root) counts the SAME as a
    removed worktree — a stale-leaning corroborating signal, not a veto."""
    p = await db_module.create_project(db, "reconcile-worktree-dead-pid")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "dead pid worktree")
    owner = await db_module.register_session(db, p["id"], "dead-pid-owner")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])
    await db.execute(
        "UPDATE sessions SET last_seen = ? WHERE id = ?", (_hours_ago(5), owner["id"]),
    )
    # An almost-certainly-nonexistent PID.
    await db_module.register_worktree(
        db, owner["id"], p["id"], "worktree/dead", str(tmp_path), item_id=item["id"], pid=2**30 - 1,
    )
    await db.commit()

    fresh_item = await db_module.get_sprint_item(db, item["id"])
    verdict = await db_module.classify_stale_claim(db, fresh_item, repo_root=tmp_path)
    assert verdict["signals"]["worktree_live"] is False
    assert verdict["classification"] == "stale"


async def test_classify_live_owner_pid_vetoes_auto_reset(db, tmp_path):
    """A worktree whose recorded owner PID IS confirmed alive (this test
    process's own PID) vetoes auto-reset entirely — even with a cold
    heartbeat and zero task_log evidence, the claim stays 'ambiguous', never
    'stale'. This is the concrete 'preserve legitimate long-running work'
    guarantee for a quiet executor."""
    p = await db_module.create_project(db, "reconcile-worktree-live-pid")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "live pid worktree")
    owner = await db_module.register_session(db, p["id"], "live-pid-owner")
    await db_module.claim_sprint_item(db, p["id"], item["id"], actor=owner["id"])
    await db.execute(
        "UPDATE sessions SET last_seen = ? WHERE id = ?", (_hours_ago(5), owner["id"]),
    )
    await db_module.register_worktree(
        db, owner["id"], p["id"], "worktree/live", str(tmp_path), item_id=item["id"], pid=os.getpid(),
    )
    await db.commit()

    fresh_item = await db_module.get_sprint_item(db, item["id"])
    verdict = await db_module.classify_stale_claim(db, fresh_item, repo_root=tmp_path)
    assert verdict["signals"]["worktree_live"] is True
    assert verdict["classification"] == "ambiguous"
