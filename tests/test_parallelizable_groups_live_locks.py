"""Tests for get_parallelizable_groups' live-resource-lock awareness (b4102313).

Confirmed live bug: get_parallelizable_groups colored its conflict graph
purely from OTHER eligible items' declared touches_resources — it never
checked whether a resource was already held by a live, in_progress item's
REAL lock (file_locks / file_symbol_claims). An item that would deterministically
fail claim_sprint_item's own resource-lock gate a moment later was still
advertised inside `groups` / counted in `eligible_count`; the cross-check
against real locks (_live_resource_holder) only populated a `resource_blocked`
diagnostic that nothing actually excluded work with.

These tests assert the fix: a live-locked candidate is excluded from
`groups`/`declared`/`eligible_count` (not merely flagged), while disjoint
symbols with no live lock still co-batch, and staleness handling (TTL expiry,
dead-session heartbeat) remains correct in both directions.
"""
import pytest

from meridian import db as db_module
from meridian import server as srv

_FOO_BAR_SRC = (
    "def foo():\n"
    "    return 1\n"
    "\n"
    "\n"
    "def bar():\n"
    "    return 2\n"
)


async def _project(db, name="live-locks"):
    proj = await srv._dispatch_mcp_tool("create_project", {"name": name}, db, "/tmp")
    return proj["id"]


def _group_ids(result):
    return {it["id"] for grp in result["groups"] for it in grp}


@pytest.mark.asyncio
async def test_whole_file_lock_excludes_declared_item_from_groups(db):
    """The exact reported shape: an item declaring file:X is excluded from
    groups (not just flagged) while another live session holds X."""
    pid = await _project(db)
    holder = await db_module.register_session(db, pid, "holder")
    item = await db_module.add_sprint_item(
        db, pid, "v1", "touches a locked file",
        touches_resources=["file:contended.py"],
    )
    claimed = await db_module.claim_file(db, "contended.py", holder["id"])
    assert claimed["claimed"] is True

    result = await db_module.get_parallelizable_groups(db, pid, version="v1")
    assert item["id"] not in _group_ids(result)
    assert item["id"] not in {b["id"] for b in result["blocked"]}
    assert result["resource_blocked_count"] == 1
    rb = result["resource_blocked"][0]
    assert rb["id"] == item["id"]
    assert rb["resource"] == "file:contended.py"
    assert rb["holder_session_id"] == holder["id"]
    assert rb["claim_granularity"] == "file"
    # eligible_count / group membership must both reflect the exclusion, not
    # just the resource_blocked diagnostic (the actual bug).
    assert item["id"] not in {
        it["id"] for grp in result["groups"] for it in grp
    }


@pytest.mark.asyncio
async def test_whole_file_lock_excludes_symbol_declared_item_too(db):
    """Reproduces the exact live incident: an item declaring a SYMBOL
    resource on a file that another session holds a WHOLE-FILE lock on
    (not a symbol claim) must still be excluded — file ⊃ symbol."""
    pid = await _project(db)
    holder = await db_module.register_session(db, pid, "whole-file-holder")
    item = await db_module.add_sprint_item(
        db, pid, "v1", "declares only a symbol on a file-locked path",
        touches_resources=["symbol:sprint_items.py::complete_sprint_item"],
    )
    claimed = await db_module.claim_file(db, "sprint_items.py", holder["id"])
    assert claimed["claimed"] is True

    result = await db_module.get_parallelizable_groups(db, pid, version="v1")
    assert item["id"] not in _group_ids(result)
    assert result["resource_blocked_count"] == 1
    assert result["resource_blocked"][0]["claim_granularity"] == "file"


@pytest.mark.asyncio
async def test_disjoint_symbols_no_live_lock_still_cobatch(db):
    """Acceptance (2): two items declaring different symbols in the same
    file, with NO live lock on that file at all, remain co-batchable."""
    pid = await _project(db)
    a = await db_module.add_sprint_item(
        db, pid, "v1", "touches symbol a",
        touches_resources=["symbol:shared.py::foo"],
    )
    b = await db_module.add_sprint_item(
        db, pid, "v1", "touches symbol b",
        touches_resources=["symbol:shared.py::bar"],
        force=True,  # near-duplicate title vs "a" above; intentionally distinct item
    )

    result = await db_module.get_parallelizable_groups(db, pid, version="v1")
    assert result["resource_blocked"] == []
    ids = _group_ids(result)
    assert a["id"] in ids and b["id"] in ids
    # Same group == proven co-batchable by the conflict-graph coloring.
    same_group = any(
        {a["id"], b["id"]} <= {it["id"] for it in grp} for grp in result["groups"]
    )
    assert same_group


@pytest.mark.asyncio
async def test_live_symbol_claim_blocks_only_matching_symbol(db):
    """A real, live SYMBOL claim (not a whole-file lock) blocks an item
    declaring that exact symbol, but leaves a disjoint symbol in the same
    file schedulable — the fix must not over-block."""
    pid = await _project(db)
    holder = await db_module.register_session(db, pid, "symbol-holder")
    claimed = await db_module.claim_symbol(
        db, holder["id"], "shared2.py", "foo", _FOO_BAR_SRC
    )
    assert claimed["claimed"] is True

    blocked_item = await db_module.add_sprint_item(
        db, pid, "v1", "wants the claimed symbol",
        touches_resources=["symbol:shared2.py::foo"],
    )
    free_item = await db_module.add_sprint_item(
        db, pid, "v1", "wants the other symbol",
        touches_resources=["symbol:shared2.py::bar"],
        force=True,  # near-duplicate title vs blocked_item above; intentional
    )

    result = await db_module.get_parallelizable_groups(db, pid, version="v1")
    ids = _group_ids(result)
    assert blocked_item["id"] not in ids
    assert free_item["id"] in ids
    assert result["resource_blocked_count"] == 1
    rb = result["resource_blocked"][0]
    assert rb["id"] == blocked_item["id"]
    assert rb["claim_granularity"] == "symbol"
    assert rb["holder_session_id"] == holder["id"]


@pytest.mark.asyncio
async def test_stale_dead_session_symbol_claim_does_not_block(db):
    """Acceptance (3): a symbol claim left behind by a session that is no
    longer live (heartbeat older than the shared TTL cutoff) must NOT block
    planning forever. Regression test for _live_resource_holder's symbol
    branch, which previously used get_symbol_claims (no liveness filter at
    all) instead of the heartbeat-aware _live_symbol_claims_for_file."""
    pid = await _project(db)
    dead = await db_module.register_session(db, pid, "crashed-session")
    claimed = await db_module.claim_symbol(
        db, dead["id"], "shared3.py", "foo", _FOO_BAR_SRC
    )
    assert claimed["claimed"] is True

    # Simulate a crashed session: heartbeat far older than the TTL cutoff
    # (mirrors _live_symbol_claims_for_file's own _CLAIM_LIVE_HOURS=2 cutoff).
    # Format must match _cutoff_dt's "%Y-%m-%d %H:%M:%S" (space-separated, no
    # microseconds) — datetime.isoformat()'s "T"-separated, microsecond-precision
    # string sorts incorrectly against it in a plain SQL string comparison.
    await db.execute(
        "UPDATE sessions SET last_seen = datetime('now', '-5 hours') WHERE id = ?",
        (dead["id"],),
    )
    await db.commit()

    item = await db_module.add_sprint_item(
        db, pid, "v1", "wants a symbol claimed by a dead session",
        touches_resources=["symbol:shared3.py::foo"],
    )

    result = await db_module.get_parallelizable_groups(db, pid, version="v1")
    assert result["resource_blocked"] == []
    assert item["id"] in _group_ids(result)


@pytest.mark.asyncio
async def test_eligible_count_and_group_count_exclude_resource_blocked(db):
    """Acceptance (4): group_count/eligible_count exclude items that will
    deterministically fail claim, not just resource_blocked_count."""
    pid = await _project(db)
    holder = await db_module.register_session(db, pid, "holder")
    blocked_item = await db_module.add_sprint_item(
        db, pid, "v1", "locked", touches_resources=["file:locked.py"],
    )
    free_item = await db_module.add_sprint_item(
        db, pid, "v1", "free", touches_resources=["file:free.py"],
    )
    await db_module.claim_file(db, "locked.py", holder["id"])

    result = await db_module.get_parallelizable_groups(db, pid, version="v1")
    assert result["eligible_count"] == 1
    assert result["resource_blocked_count"] == 1
    all_group_items = [it["id"] for grp in result["groups"] for it in grp]
    assert all_group_items == [free_item["id"]]
    assert blocked_item["id"] not in all_group_items


@pytest.mark.asyncio
async def test_resource_blocked_item_rejoins_groups_after_lock_expires(db):
    """Extends the existing lease-expiry contract test: once the blocking
    lock's TTL has genuinely lapsed, the item is not just missing from
    resource_blocked — it is back inside groups, immediately claimable."""
    pid = await _project(db)
    holder = await db_module.register_session(db, pid, "holder")
    item = await db_module.add_sprint_item(
        db, pid, "v1", "touches a file", touches_resources=["file:leased2.py"],
    )
    pre = await db_module.claim_file(db, "leased2.py", holder["id"])
    assert pre["claimed"] is True

    mid = await db_module.get_parallelizable_groups(db, pid, version="v1")
    assert item["id"] not in _group_ids(mid)

    await db.execute(
        "UPDATE file_locks SET expires_at = datetime('now', '-1 hour') "
        "WHERE file_path = 'leased2.py'"
    )
    await db.commit()

    after = await db_module.get_parallelizable_groups(db, pid, version="v1")
    assert item["id"] in _group_ids(after)
    assert after["resource_blocked"] == []
    assert mid["plan_generation"] != after["plan_generation"]
