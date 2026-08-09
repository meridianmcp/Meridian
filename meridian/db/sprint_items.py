"""Sprint-item persistence functions — extracted from meridian/db/__init__.py.

This module contains all functions whose primary subject is the sprint_items table:
add/claim/complete/fail/push/skip/patch/split/merge/pointers/waves and related helpers.

Imported back into meridian.db via ``from .sprint_items import *`` so all existing
call sites using ``db_module.function_name()`` continue to work unchanged.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timezone  # 0d0cada7 — lease-local scheduler diagnostics
from typing import Any
from xml.sax.saxutils import escape as _xml_escape  # fdaa5b55/cd038235 — same
# escaping helper/discipline as 5abf3e12 (meridian/handoff.py's
# _build_quick_start_goal), reused here for GitHub-bound comment bodies.

import aiosqlite

# Shared helpers from the parent db package — available at import time
# because sprint_items.py is imported at the bottom of db/__init__.py,
# after all these names are defined.
from meridian.db import (  # noqa: PLC0415
    _new_id,
    _row_to_dict,
    _publish_project_event,
    _UNSET,
    serialize_touches_resources,
    parse_touches_resources,
    _resource_sets_conflict,
    _resource_file_of,
    get_executor_config,
    get_project,
    set_executor_config,
)
from .. import tool_requirements as _tool_requirements  # 76dde31f (665 follow-up)
from .. import artifact_declaration as _artifact_declaration  # 2f9cb288 (665 follow-up)
from .. import executor_config as _executor_config  # 99c0c1be — parallelism diagnostics
from .. import continuation_gate as _continuation_gate  # ecc8b280
from .. import blocker_policy as _blocker_policy  # b108f2e0 (typed blocker triage)
from .. import dependency_graph as _dependency_graph  # 05553946 (cycle detection)


# ---------------------------------------------------------------------------
# SECTION 1: Lines 2782-2965 — stall helpers and stalled-item functions
# ---------------------------------------------------------------------------

# bc9259b8 — worker stall auto-retry budget. A sprint item left in_progress by a
# closing/stale worker is re-queued to pending while its stall_count is within
# this budget; once it would exceed the budget it is marked failed silently (no
# HITL, no human ping) so the orchestrator just moves on.
_MAX_SPRINT_STALL_RETRIES = 2

# 890046a2 — time-based stall detection threshold for analyze_sprint.
#
# The persisted stall_count is only incremented when a session is explicitly
# archived/closed with items still claimed.  Items abandoned by a chat window
# that was simply closed — the common real-world case — never get their
# stall_count bumped, regardless of how many days pass.
#
# This constant guards a second, time-based detection path: any item still
# in_progress (or claimed) after this many hours is surfaced as a stall even
# when stall_count == 0.
#
# The threshold is intentionally LONGER than the 2-hour auto-release window
# used by release_stale_task_claims / _FILE_LOCK_TTL_HOURS.  Those mechanisms
# are defensive (prevent orphaned locks); this one surfaces to a human planner
# who should see truly long-running items, not routine in-flight work.  A
# sprint item sitting in_progress for 4+ hours without progress almost
# certainly represents an abandoned session, not an active worker.
_SPRINT_STALL_FLAG_HOURS = 4


async def _session_stall_summary(
    db: aiosqlite.Connection, session_id: str, *, limit: int = 5
) -> str:
    """Build a compact 'last session log' string for a stalled worker session.

    Joins the session's most recent task_log descriptions so the failure note on
    a permanently-stalled item captures what the worker was doing. Best-effort:
    returns '(no session log)' when the session logged nothing.
    """
    async with db.execute(
        "SELECT description FROM task_log WHERE session_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (session_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    descs = [str((_row_to_dict(r) or {}).get("description") or "").strip() for r in rows]
    descs = [d for d in descs if d]
    if not descs:
        return "(no session log)"
    return " | ".join(descs)


async def _stalled_item_ids_for_session(
    db: aiosqlite.Connection, session_id: str
) -> list[str]:
    """Return distinct sprint-item ids this session was working on.

    A worker links to an item via a registered worktree (active_worktrees.item_id)
    or via task_log rows tagged with sprint_item_id. The union covers both the
    worktree-isolated and single-tree worker styles.
    """
    ids: list[str] = []
    seen: set[str] = set()
    async with db.execute(
        "SELECT item_id FROM active_worktrees "
        "WHERE session_id = ? AND item_id IS NOT NULL AND removed_at IS NULL",
        (session_id,),
    ) as cur:
        for r in await cur.fetchall():
            iid = (_row_to_dict(r) or {}).get("item_id")
            if iid and iid not in seen:
                seen.add(iid)
                ids.append(iid)
    async with db.execute(
        "SELECT DISTINCT sprint_item_id FROM task_log "
        "WHERE session_id = ? AND sprint_item_id IS NOT NULL",
        (session_id,),
    ) as cur:
        for r in await cur.fetchall():
            iid = (_row_to_dict(r) or {}).get("sprint_item_id")
            if iid and iid not in seen:
                seen.add(iid)
                ids.append(iid)
    return ids


async def requeue_or_fail_stalled_item(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    *,
    session_id: str | None = None,
) -> dict[str, Any] | None:
    """bc9259b8 — handle one stalled sprint item: re-queue, or fail after the budget.

    Increments ``stall_count``. While the new count is within
    :data:`_MAX_SPRINT_STALL_RETRIES`, the item is re-queued to ``pending``
    (claim cleared) so another worker can pick it up. Once the new count exceeds
    the budget the item is marked ``failed`` with the stalling session's last log
    appended to its notes — silently, with no HITL. No-op (returns None) when the
    item is missing, in another project, or not currently ``in_progress``.
    """
    item = await get_sprint_item(db, item_id)
    if item is None or item.get("project_id") != project_id:
        return None
    if (item.get("status") or "pending") != "in_progress":
        return None  # completed/failed/already re-queued — not a stall
    new_count = int(item.get("stall_count") or 0) + 1
    if new_count > _MAX_SPRINT_STALL_RETRIES:
        last_log = (
            await _session_stall_summary(db, session_id) if session_id else "(unknown session)"
        )
        reason = (
            f"Auto-failed after {new_count - 1} stall retr"
            f"{'y' if new_count - 1 == 1 else 'ies'} "
            f"(worker closed without completing). Last session log: {last_log}"
        )
        # fa3e3331 / ARCH 1B — route through _transition_status so the atomic
        # from-state guard ("from_statuses=['in_progress']"), cache bust, and
        # live event are handled by the shared chokepoint. Returns None (no-op)
        # if the item is no longer in_progress — a concurrent completion beat us.
        # claimed_at is cleared via a separate stall_count UPDATE below when we
        # confirm the transition succeeded; stall_count is a stall-specific field
        # that _transition_status does not model, so we write it afterwards.
        _failed_result = await _transition_status(
            db, project_id, item_id, "failed",
            from_statuses=["in_progress"],
            notes=reason,
        )
        if _failed_result is None:
            return None
        # Stamp the new stall_count and clear claimed_at now that we own the row.
        await db.execute(
            "UPDATE sprint_items SET stall_count = ?, claimed_at = NULL "
            "WHERE id = ? AND project_id = ?",
            (new_count, item_id, project_id),
        )
        await db.commit()
        updated = await get_sprint_item(db, item_id)
        return {"action": "failed", "item": updated, "stall_count": new_count}
    # Re-queue path: returns None if a concurrent caller already changed status.
    _requeued_result = await _transition_status(
        db, project_id, item_id, "pending",
        from_statuses=["in_progress"],
    )
    if _requeued_result is None:
        return None
    # Stamp the new stall_count and clear claimed_at/completed_at.
    await db.execute(
        "UPDATE sprint_items SET stall_count = ?, claimed_at = NULL, completed_at = NULL "
        "WHERE id = ? AND project_id = ?",
        (new_count, item_id, project_id),
    )
    await db.commit()
    updated = await get_sprint_item(db, item_id)
    return {"action": "requeued", "item": updated, "stall_count": new_count}


async def handle_session_stall(
    db: aiosqlite.Connection, session_id: str
) -> dict[str, Any]:
    """bc9259b8 — re-queue or fail any sprint items a closing worker left in_progress.

    Finds every sprint item this session was working on (worktree or task link)
    that is still ``in_progress`` and routes it through
    :func:`requeue_or_fail_stalled_item`. Returns ``{"requeued": [ids], "failed":
    [ids]}``. Safe no-op when the session completed its work (items already done).
    """
    async with db.execute(
        "SELECT project_id FROM sessions WHERE id = ?", (session_id,)
    ) as cur:
        srow = await cur.fetchone()
    sess = _row_to_dict(srow)
    requeued: list[str] = []
    failed: list[str] = []
    if not sess or not sess.get("project_id"):
        return {"requeued": requeued, "failed": failed}
    project_id = sess["project_id"]
    for item_id in await _stalled_item_ids_for_session(db, session_id):
        result = await requeue_or_fail_stalled_item(
            db, project_id, item_id, session_id=session_id
        )
        if result is None:
            continue
        if result["action"] == "failed":
            failed.append(item_id)
        else:
            requeued.append(item_id)
    return {"requeued": requeued, "failed": failed}


# ---------------------------------------------------------------------------
# SECTION 2: Lines 4008-4038 — sprint-item task helpers
# ---------------------------------------------------------------------------

async def get_open_task_for_sprint_item(
    db: aiosqlite.Connection, sprint_item_id: str
) -> dict[str, Any] | None:
    """Return the current pending/in-progress task row for a sprint item."""
    async with db.execute(
        "SELECT * FROM task_log WHERE sprint_item_id = ? "
        "AND status IN ('pending', 'in_progress') "
        "ORDER BY CASE WHEN status = 'in_progress' THEN 0 ELSE 1 END, "
        "created_at DESC, id DESC LIMIT 1",
        (sprint_item_id,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def get_blocking_dependency_for_sprint_item(
    db: aiosqlite.Connection, sprint_item_id: str
) -> dict[str, Any] | None:
    """Return the unmet parent sprint item that blocks a claim, if any."""
    item = await get_sprint_item(db, sprint_item_id)
    if item is None:
        return None
    parent_id = item.get("depends_on")
    if not parent_id:
        return None
    parent = await get_sprint_item(db, parent_id)
    if parent is None:
        return {"id": parent_id, "title": "(missing sprint item)", "status": "missing"}
    if parent.get("status") != "done":
        return parent
    return None


# ---------------------------------------------------------------------------
# SECTION 3: Lines 4231-6072 — main sprint-item block
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Sprint items (v1.1) — machine-trackable checklist alongside the
# free-text sprint field. Fixes the "sprint drift" problem where items
# get written and silently forgotten across sessions.
# ---------------------------------------------------------------------------


_VALID_SPRINT_STATUSES = {
    "pending", "todo", "in_progress", "provisional_complete",
    "done", "failed", "skipped", "pushed", "indeterminate",
}

# Non-terminal statuses that keep a parent item "active" and never stamp
# completed_at. provisional_complete sits between in_progress and done:
# the executor has finished the work but it is not yet verified/deployed, so
# it must NOT roll a parent up to done or count toward percent_complete.
_ACTIVE_SPRINT_STATUSES = {
    "pending", "in_progress", "todo", "indeterminate", "provisional_complete",
}

# a2a027cf — bounded budget (seconds) for complete_sprint_item's purely
# ADVISORY post-commit work (parent rollup, mixed-ownership task-chain
# advance, continuation-state gather). The authoritative status write
# (sprint_items.status -> 'done') has already committed by the time any of
# this runs; none of it may hold the HTTP/MCP response hostage past a
# client's own request timeout (repeated live reports: clients timing out
# around 60s while the write had actually already landed). Generous
# relative to the real work (a handful of indexed lookups) but far under
# typical client timeouts, so it only ever engages under genuine pathology
# (e.g. a huge sprint board) rather than everyday completion calls.
_ADVISORY_PHASE_TIMEOUT_S = 5.0

# Statuses that make an existing item a *blocking* duplicate when a new item
# with a near-identical title is added. Only open/active work counts: a title
# that overlaps a finished item (done / skipped / failed / pushed) is allowed
# through, since re-doing finished work is legitimate. ``todo`` is the DB
# default for freshly-added items and is pending-equivalent here.
_DUP_BLOCKING_SPRINT_STATUSES = {"pending", "todo", "in_progress"}

# e08fee30 — app-layer priority enum for sprint items. Higher-priority PENDING
# items are surfaced (claimed / grouped) first. The DB column has no CHECK (so the
# ADD COLUMN migration stays a plain alter); add_sprint_item / patch_sprint_item
# enforce membership and raise ValueError on a bad value, like milestone_type.
_VALID_SPRINT_PRIORITIES = ("urgent", "high", "normal", "low")
# Rank used to order urgent-first (lower rank sorts earlier). Rendered into a
# portable CASE expression so both SQLite and Postgres order identically without a
# separate lookup table; an unknown/NULL priority falls back to 'normal's rank.
_SPRINT_PRIORITY_RANK = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
_SPRINT_PRIORITY_DEFAULT_RANK = _SPRINT_PRIORITY_RANK["normal"]

# 2282a636 — blocker_kind values. NULL = ordinary item; 'manual' = blocked on a
# real-world action OUTSIDE Meridian (publish something, obtain an API key, talk
# to an advisor). DISTINCT from milestone_type='human' (WHO executes) — a
# manual-blocker is excluded from executor "just claim the next pending" scoping.
# 'manual' is a SOFT gate: it only affects listing/wave-assignment surfaces
# (get_sprint_items, dashboard wave grouping) — claim_sprint_item itself still
# allows a direct claim by item_id, since a human may legitimately hand an
# executor that exact id once the real-world blocker has been cleared.
#
# f89d440f — 'superseded' = the item's whole premise has been replaced by other
# work (e.g. a workspace proposal) and it must NOT be re-executed even by a
# direct claim_sprint_item(item_id=...) call. Before this, "do not claim" only
# existed as prose in the notes field, which claim_sprint_item never reads —
# so an item correctly declined in one session (e.g. c2021725) became claimable
# again in the next, since nothing but human judgment stopped it. 'superseded'
# is therefore a HARD gate, enforced inside claim_sprint_item itself (see the
# blocked-dict check below), unlike 'manual's listing-only exclusion.
#
# cc3864bd — 'systemic_invalidated_run' = the item belongs to a wave run whose
# FOUNDATIONAL HYPOTHESIS was systemically invalidated (see
# meridian.db.wave_runs.abort_wave_run_systemic /
# block_sprint_items_for_systemic_invalidation) — a stronger, deterministic-
# evidence-gated cousin of 'superseded'. Also a HARD gate, same enforcement
# point, for the same reason: a stale goal block or prior session memory can
# still hand an executor this item_id directly.
_VALID_SPRINT_BLOCKER_KINDS = ("manual", "superseded", "systemic_invalidated_run")

# 7c82f7c8 — github_channel values, mirroring the fdaa5b55 auto-filed-issue
# labeling scheme (channel:nightly / channel:stable GitHub labels). NULL =
# no channel classification recorded on this item. 'nightly' / 'stable' track
# which release channel a linked, auto-filed GitHub issue was reported against
# (set from the issue template the reporter picked — see
# .github/ISSUE_TEMPLATE/ — at completion-time issue creation).
# 'graduated' is the third state Adam asked for: a bug that STARTED as
# nightly-only noise but has since been CONFIRMED reproducing on stable too —
# the signal it needs a real fix before general release, not just
# expected-nightly churn. The nightly deploy channel itself (cd9c2bf7) is not
# yet live; this column is the tracking mechanism so it is ready once it is.
_VALID_SPRINT_GITHUB_CHANNELS = ("nightly", "stable", "graduated")


def _sprint_priority_order_sql(column: str = "priority") -> str:
    """Return a portable ``CASE`` expression ranking ``column`` urgent-first.

    Renders the app-layer priority enum into an integer rank inside SQL so
    ``ORDER BY`` can put higher-priority items first on both SQLite and Postgres
    (neither has a native enum ordering here). NULL / unknown values fall back to
    'normal's rank, so legacy rows sort as normal rather than first-or-last.
    """
    whens = " ".join(
        f"WHEN '{p}' THEN {r}" for p, r in _SPRINT_PRIORITY_RANK.items()
    )
    return f"CASE {column} {whens} ELSE {_SPRINT_PRIORITY_DEFAULT_RANK} END"

# b0d42ef6 — fuzzy-duplicate threshold for add_sprint_item. Two titles are
# treated as duplicates when their word-set overlap is >= 60%.
_SPRINT_DUP_OVERLAP_THRESHOLD = 0.60


def _title_word_set(title: str) -> set[str]:
    """Tokenise a sprint-item title into a lowercased word set.

    Splits on any run of non-alphanumeric characters and lowercases, so
    "Add OAuth login!" and "add  oauth   LOGIN" both yield {add, oauth, login}.
    Punctuation and surrounding whitespace are discarded.
    """
    return {w for w in re.split(r"[^0-9a-z]+", title.lower()) if w}


def _title_word_overlap(a: set[str], b: set[str]) -> float:
    """Word-set overlap of two pre-tokenised titles, in ``[0.0, 1.0]``.

    Defined as ``|a ∩ b| / |smaller set|`` (the overlap coefficient). Dividing
    by the smaller of the two word sets makes the metric symmetric and means a
    short title that is fully contained in a longer one scores 1.0 — so
    "Add OAuth" vs "Add OAuth login and refresh-token rotation" is flagged as a
    duplicate even though the longer title has many extra words. Returns 0.0 if
    either set is empty.
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))

# ---------------------------------------------------------------------------
# get_sprint_progress cache — one get_sprint_items DB query serves all
# parallel sessions polling between tasks. Keyed by project_id; busted on any
# sprint-item mutation so progress counts never read stale after a write.
#
# ONLY get_sprint_progress reads through this cache (via get_sprint_items_cached,
# meridian/mcp/handlers/sprint_tools.py). The get_sprint_items MCP tool and
# _board_change_for_session both call the uncached get_sprint_items() directly —
# a live SQL query on every call — and are NEVER subject to the staleness
# described below. Do not route either of them through this cache without
# re-reading a1d75ff3's investigation notes.
#
# a1d75ff3 (2026-07-19) — cross-instance staleness on multi-instance Fly. This
# dict is per-process: _invalidate_sprint_items_cache only pops the entry in
# THIS instance's memory. Postgres (the single shared source of truth, and
# already autocommit — no held-transaction/snapshot staleness) sees every
# instance's write immediately, but a sibling instance's already-cached entry
# is untouched by another instance's write and keeps serving the pre-write
# snapshot until ITS OWN local TTL naturally elapses. That elapsing is real
# and does happen — time.monotonic() correctly resets on every cache miss, so
# no single instance can serve an entry older than _SPRINT_ITEMS_CACHE_TTL —
# but "self-heals within the TTL" is still a genuine bug: any read landing on
# a stale sibling during that window sees wrong data. True cross-instance
# invalidation (pub/sub) would need new infra (Redis / Postgres LISTEN-NOTIFY;
# the existing _publish_project_event fan-out is itself per-process, WS-only —
# it does not reach sibling machines) — out of scope for the value delivered
# here. Given that, the fix is: (1) keep write-time local invalidation, since
# it is correct and gives the writing instance's own next read true
# instant-freshness at zero cost — removing it would only make the common
# "complete an item, then check progress" pattern worse without narrowing the
# cross-instance gap at all, which is inherent to any pure-TTL cache without
# cross-instance signalling; (2) shrink the TTL from 10s to 2s so the bound
# any OTHER instance can be behind is small enough to be a non-issue in
# practice, while still meaningfully absorbing tight polling loops.
# ---------------------------------------------------------------------------
_SPRINT_ITEMS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_SPRINT_ITEMS_CACHE_TTL = 2.0  # seconds — see a1d75ff3 note above for why 2s, not 10s


def _invalidate_sprint_items_cache(project_id: str) -> None:
    """Drop the cached sprint-item list for a project after a mutation.

    a1d75ff3 — this only clears THIS process's own in-memory entry. On a
    multi-instance deployment it does NOT reach sibling instances' caches;
    see the module-level comment above _SPRINT_ITEMS_CACHE for the full
    cross-instance staleness analysis and why that is an accepted, bounded
    (TTL-sized) tradeoff rather than a fixed gap.
    """
    _SPRINT_ITEMS_CACHE.pop(project_id, None)


async def get_sprint_items_cached(
    db: aiosqlite.Connection, project_id: str
) -> list[dict[str, Any]]:
    """Return get_sprint_items(project_id), cached for _SPRINT_ITEMS_CACHE_TTL.

    Parallel executors polling get_sprint_progress between tasks share one DB
    query within the TTL window. Any add/update mutation calls
    _invalidate_sprint_items_cache so counts are never stale after a write ON
    THE SAME PROCESS. On a multi-instance deployment a sibling process's cache
    entry is NOT invalidated by another instance's write — see the a1d75ff3
    note above _SPRINT_ITEMS_CACHE for why that residual, TTL-bounded gap is
    the accepted tradeoff. Callers that need read-your-own-writes-anywhere
    guarantees (not just same-process) must call get_sprint_items() directly.

    f291bb24 — do NOT call this from complete_sprint_item's own continuation-
    state gather. This project's cache entry was just invalidated by THIS
    SAME completion's status write moments earlier (_transition_status calls
    _invalidate_sprint_items_cache before this would run), so that caller is
    a guaranteed miss every time — see get_sprint_items_continuation_scoped
    below, which is what complete_sprint_item actually uses instead.
    """
    now = time.monotonic()
    hit = _SPRINT_ITEMS_CACHE.get(project_id)
    if hit is not None and (now - hit[0]) < _SPRINT_ITEMS_CACHE_TTL:
        return hit[1]
    items = await get_sprint_items(db, project_id)
    _SPRINT_ITEMS_CACHE[project_id] = (now, items)
    return items


async def get_sprint_items_continuation_scoped(
    db: aiosqlite.Connection, project_id: str, version: str | None,
) -> list[dict[str, Any]]:
    """f291bb24 — minimal-column, version-scoped item list for
    continuation_gate.compute_continuation_state, which reads only
    ``id``/``status``/``blocker_kind`` per item.

    Replaces the previous get_sprint_items_cached(project_id) call inside
    complete_sprint_item's _gather_continuation_inputs, which (a) was a
    guaranteed cache miss for the completing call itself — this item's own
    status write invalidates the project's cache entry moments earlier — and
    (b) even on a hit would have returned every column (including the wide
    notes/tool_requirements/touches_resources TEXT columns) for every item in
    the project, then discarded everything outside one version bucket in
    Python. On a large board (thousands of items, multi-KB JSON blobs per
    row) that full-board fetch dominated the completion call's latency. This
    pushes both the version filter and the column narrowing into SQL.
    """
    clauses = ["project_id = ?"]
    params: list = [project_id]
    if version is not None:
        clauses.append("version = ?")
        params.append(version)
    query = f"SELECT id, status, blocker_kind FROM sprint_items WHERE {' AND '.join(clauses)}"
    async with db.execute(query, tuple(params)) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]  # type: ignore[misc]


async def count_new_sprint_items_since(
    db: aiosqlite.Connection, project_id: str, since: str,
) -> tuple[int, int]:
    """f291bb24 — ``(new_count, urgent_count)`` of items added after ``since``.

    Replaces _board_change_for_session's previous pattern (used by
    complete_sprint_item, claim_sprint_item, get_sprint_progress) of fetching
    EVERY item in the project via the uncached get_sprint_items(project_id) —
    every column, every row — then filtering/counting in Python. This is a
    single aggregate query scoped to the project (using the project_id
    prefix of idx_sprint_items_project) that returns just the two counts the
    caller actually needs, with no wide TEXT columns and no per-item Python
    iteration.
    """
    query = (
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN priority = 'urgent' THEN 1 ELSE 0 END) AS urgent "
        "FROM sprint_items WHERE project_id = ? AND added_at > ?"
    )
    async with db.execute(query, (project_id, since)) as cur:
        row = await cur.fetchone()
    if row is None:
        return 0, 0
    _row = _row_to_dict(row) or {}
    return int(_row.get("total") or 0), int(_row.get("urgent") or 0)


async def has_active_sprint_items(
    db: aiosqlite.Connection, project_id: str, statuses: "set[str] | tuple[str, ...]",
) -> bool:
    """f291bb24 — indexed existence check: does this project have any item
    whose status is in ``statuses``?

    Replaces an unfiltered, untimed ``SELECT * FROM sprint_items WHERE
    project_id=?`` that complete_sprint_item's MCP handler used to run on
    every single completion (unlike every other advisory query in that
    codepath, it had no asyncio.wait_for budget at all) purely to decide
    whether to fire a "sprint done" notification. Uses
    idx_sprint_items_project(project_id, status), ``LIMIT 1``, no wide TEXT
    columns — cost independent of board size.
    """
    _statuses = tuple(statuses)
    if not _statuses:
        return False
    placeholders = ", ".join("?" for _ in _statuses)
    query = (
        f"SELECT 1 FROM sprint_items WHERE project_id = ? "
        f"AND status IN ({placeholders}) LIMIT 1"
    )
    async with db.execute(query, (project_id, *_statuses)) as cur:
        row = await cur.fetchone()
    return row is not None


def _sprint_item_slug_base(text: str) -> str:
    """b944c905 — kebab-case a title into a short human-readable id base.

    d2e4f557 — applies stopword filtering (reusing _NICKNAME_STOPWORDS) before
    taking the word budget, so boilerplate title prefixes like "BUG (confirmed
    live, Adam explicit):" are stripped and the slug captures actual substance.
    Word budget is top-8 post-filter (vs. nickname's top-2) to give slugs enough
    context to be distinguishable at a glance.

    Edge-case: if the title has fewer than 3 non-stopword words after filtering,
    falls back to the unfiltered word list so the slug is never empty or
    degenerate (a title that is entirely boilerplate still produces something
    identifiable, just not a great slug — the collision-suffix mechanism handles
    the rest).
    """
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    # Apply stopword filter + length gate (same rules as nickname, len > 2).
    filtered = [w for w in words if w not in _NICKNAME_STOPWORDS and len(w) > 2]
    # Use filtered words when at least one exists; fall back to raw words only
    # when the title is entirely boilerplate/stopwords so we never produce an
    # empty slug (a title of all stopwords still produces an identifiable slug
    # from its raw words, and the collision-suffix mechanism handles the rest).
    chosen = filtered if filtered else words
    return "-".join(chosen[:8])[:60].strip("-") or "item"


async def _unique_sprint_slug(
    db: aiosqlite.Connection,
    project_id: str,
    base: str,
    exclude_id: str | None = None,
) -> str:
    """b944c905 — ``base``, or base-2/base-3/… if the slug is taken in this
    project (mirrors _unique_note_slug; slugs are unique per project)."""
    slug = base
    n = 1
    while True:
        async with db.execute(
            "SELECT id FROM sprint_items WHERE project_id = ? AND slug = ?",
            (project_id, slug),
        ) as cur:
            row = await cur.fetchone()
        existing = _row_to_dict(row)
        if existing is None or existing.get("id") == exclude_id:
            return slug
        n += 1
        slug = f"{base}-{n}"


# b6b0cee6 — short, memorable sprint-item nicknames. Word lists reuse the
# adjective+noun idiom already proven for session naming (2bce89ed), widened so a
# title-less fallback stays unique-ish before the per-project collision suffix.
_NICKNAME_ADJ = (
    "brisk", "calm", "clever", "bold", "quiet", "swift", "warm", "keen", "bright",
    "steady", "nimble", "lucid", "amber", "cobalt", "coral", "dusky", "fabled",
    "gilded", "hardy", "ivory", "jade", "lush", "mellow", "noble",
)
_NICKNAME_NOUN = (
    "otter", "harbor", "cedar", "falcon", "meadow", "ember", "delta", "willow",
    "quartz", "sparrow", "atlas", "cove", "beacon", "cypress", "drift", "fjord",
    "grove", "heron", "isle", "kite", "lagoon", "moss", "nimbus", "onyx",
)
# Title words too generic to make a distinctive slug or nickname (mostly
# sprint-item prefixes, connectives, and project-convention boilerplate words).
# Dropped before picking keeper words for both slug generation and nickname
# generation. Extended (d2e4f557) with project-observed high-frequency
# non-distinctive words: "adam", "explicit", "tonight", "real", "given" appear
# in a large fraction of titles via the house convention "FEAT (Adam explicit)"
# and add zero substance to a slug or nickname.
_NICKNAME_STOPWORDS = frozenset({
    "feat", "bug", "fix", "rule", "the", "and", "for", "add", "adds", "added",
    "new", "serious", "confirmed", "live", "severe", "paper", "blog", "post",
    "manual", "correction", "with", "via", "not", "its", "this", "that", "into",
    "from", "onto", "task", "chore", "docs", "doc", "test", "refactor",
    # d2e4f557 — project-convention boilerplate words (high-frequency, zero substance)
    "adam", "explicit", "tonight", "real", "given",
})


def _sprint_item_nickname_base(title: str, iid: str) -> str:
    """b6b0cee6 — a short (1-2 word) memorable nickname base for a sprint item.

    Prefers the first 1-2 distinctive title words (skipping generic prefixes /
    connectives); when the title has none usable, falls back to a deterministic
    adjective+noun derived from the item id (so it is stable, not random).
    """
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    picks = [w for w in words if w not in _NICKNAME_STOPWORDS and len(w) > 2]
    if picks:
        return "-".join(picks[:2])[:32].strip("-") or "item"
    h = sum(ord(c) for c in (iid or "x"))
    return (
        f"{_NICKNAME_ADJ[h % len(_NICKNAME_ADJ)]}-"
        f"{_NICKNAME_NOUN[(h // len(_NICKNAME_ADJ)) % len(_NICKNAME_NOUN)]}"
    )


async def _unique_sprint_nickname(
    db: aiosqlite.Connection,
    project_id: str,
    base: str,
    exclude_id: str | None = None,
) -> str:
    """b6b0cee6 — ``base``, or base-2/base-3/… if the nickname is taken in this
    project (mirrors _unique_sprint_slug; nicknames are unique per project)."""
    nickname = base
    n = 1
    while True:
        async with db.execute(
            "SELECT id FROM sprint_items WHERE project_id = ? AND nickname = ?",
            (project_id, nickname),
        ) as cur:
            row = await cur.fetchone()
        existing = _row_to_dict(row)
        if existing is None or existing.get("id") == exclude_id:
            return nickname
        n += 1
        nickname = f"{base}-{n}"


# ---------------------------------------------------------------------------
# f9188526 — Sprint version bucket descriptions.
#
# Each (project_id, version) pair carries a concise auto-generated summary of
# what that sprint bucket is about as a whole.  The description is seeded on
# the first add_sprint_item call for a version and refreshed on every
# subsequent add so it reflects the full item set, not just the first one.
# A human can overwrite the description; the next add_sprint_item will
# regenerate it unless the item set is unchanged.
# ---------------------------------------------------------------------------


def _auto_generate_version_description(version: str, titles: list[str]) -> str:
    """f9188526 — synthesise a concise bucket description from item titles.

    Produces a one-or-two-sentence plain-English summary of what a sprint
    version bucket is about, derived purely from the item titles already in
    that bucket.  Intentionally lightweight (no LLM call): the description is
    generated synchronously at DB write time with a simple keyword-frequency
    heuristic so it is always available and never blocks.

    Strategy:
    1. Strip boilerplate prefix words (FEAT/FIX/BUG/CHORE/…) from each title.
    2. Build a word-frequency map over the cleaned titles, weighting longer
       words higher (shorter words tend to be connectives / noise).
    3. Pick the top-3 theme words and form a sentence such as:
       "v1.2 focuses on <word1>, <word2>, and <word3>."
    4. Append a count sentence: "Contains N item(s) covering …".

    When the bucket has zero or one title, falls back to a simpler template
    that avoids repeating the single title verbatim.
    """
    # Boilerplate prefixes that appear in item titles but do not describe
    # the bucket's theme: strip them (case-insensitively) from the front.
    _STRIP_PREFIXES = (
        "feat", "fix", "bug", "chore", "refactor", "test", "docs", "style",
        "perf", "security", "harden", "revert", "wip",
    )
    _STOP_WORDS = frozenset({
        "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "up", "as", "is", "it", "its", "this",
        "that", "into", "via", "not", "no", "all", "any", "be", "was", "are",
        "has", "have", "had", "do", "did", "will", "would", "could", "should",
        "add", "adds", "added", "new", "when", "if", "so", "then", "else",
        "use", "uses", "used", "using", "make", "makes", "made", "set", "get",
        "let", "now", "also", "more", "each", "every", "both", "can", "may",
        "must", "very", "just", "only", "even", "well", "back", "out", "over",
        "after", "before", "between", "through", "across", "during", "about",
    })

    n = len(titles)
    if n == 0:
        label = version or "this sprint"
        return f"Sprint bucket '{label}' has no items yet."

    # Clean titles: strip leading prefix tokens.
    cleaned: list[str] = []
    for t in titles:
        words = re.split(r"[\s:_\-]+", t.strip())
        # Drop leading prefix words (case-insensitive match).
        while words and words[0].lower().rstrip("!?.,") in _STRIP_PREFIXES:
            words = words[1:]
        cleaned.append(" ".join(words))

    if n == 1:
        label = version or "this sprint"
        return (
            f"Sprint bucket '{label}' contains 1 item: {cleaned[0][:120]}."
        )

    # Word frequency over all cleaned titles, skipping stop words and very
    # short tokens.  Weight each occurrence by sqrt(word_length) so domain
    # terms ("authentication", "migration") outrank short connectives.
    import math as _math  # noqa: PLC0415 — light stdlib, lazy to keep top-level clean
    freq: dict[str, float] = {}
    for t in cleaned:
        words = re.findall(r"[a-zA-Z][a-z]{2,}", t)  # 3+ char, starts uppercase/lower
        seen_in_title: set[str] = set()
        for w in words:
            lw = w.lower()
            if lw in _STOP_WORDS:
                continue
            # De-duplicate within one title so a title that repeats a word
            # doesn't inflate the global count unfairly.
            if lw in seen_in_title:
                continue
            seen_in_title.add(lw)
            freq[lw] = freq.get(lw, 0.0) + _math.sqrt(max(len(lw), 1))

    # Pick up to 3 top-frequency words to form the theme description.
    top = sorted(freq, key=lambda w: (-freq[w], w))[:3]

    label = version or "this sprint"
    if not top:
        # Fallback: no meaningful words found (all stop-words / very short).
        return (
            f"Sprint bucket '{label}' contains {n} item(s)."
        )

    if len(top) == 1:
        theme = top[0]
    elif len(top) == 2:
        theme = f"{top[0]} and {top[1]}"
    else:
        theme = f"{top[0]}, {top[1]}, and {top[2]}"

    return (
        f"Sprint bucket '{label}' focuses on {theme}. "
        f"Contains {n} item(s)."
    )


async def get_sprint_version_description(
    db: aiosqlite.Connection,
    project_id: str,
    version: str,
) -> str | None:
    """f9188526 — fetch the stored description for a sprint version bucket.

    Returns ``None`` when no description has been stored yet.
    """
    async with db.execute(
        "SELECT description FROM sprint_version_descriptions "
        "WHERE project_id = ? AND version = ?",
        (project_id, version),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return (row["description"] if isinstance(row, dict) else row[0]) or None


async def upsert_sprint_version_description(
    db: aiosqlite.Connection,
    project_id: str,
    version: str,
    description: str,
) -> None:
    """f9188526 — create or replace the description for a sprint version bucket.

    Uses INSERT OR REPLACE (SQLite) so the call is always idempotent.
    ``updated_at`` is refreshed on every upsert.
    """
    iid = _new_id()
    await db.execute(
        "INSERT INTO sprint_version_descriptions "
        "(id, project_id, version, description, updated_at) "
        "VALUES (?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(project_id, version) DO UPDATE SET "
        "description = excluded.description, updated_at = datetime('now')",
        (iid, project_id, version, description),
    )
    await db.commit()


async def get_all_sprint_version_descriptions(
    db: aiosqlite.Connection,
    project_id: str,
) -> dict[str, str]:
    """f9188526 — fetch all stored version descriptions for a project.

    Returns a ``{version: description}`` mapping (empty dict when none exist).
    Used by get_sprint_progress to include version context in the progress
    response without a per-version round-trip.
    """
    async with db.execute(
        "SELECT version, description FROM sprint_version_descriptions "
        "WHERE project_id = ?",
        (project_id,),
    ) as cur:
        rows = await cur.fetchall()
    result: dict[str, str] = {}
    for row in rows:
        if isinstance(row, dict):
            v, d = row.get("version"), row.get("description")
        else:
            v, d = row[0], row[1]
        if v and d:
            result[str(v)] = str(d)
    return result


async def add_sprint_item(
    db: aiosqlite.Connection,
    project_id: str,
    version: str,
    title: str,
    group: str | None = None,
    human_id: str | None = None,
    depends_on: str | None = None,
    failure_mode: str | None = None,
    milestone_type: str = "task",
    touches_resources: Any = None,
    force: bool = False,
    slug: str | None = None,
    deferred_until: str | None = None,
    track: str | None = None,
    priority: str | None = None,
    blocker_kind: str | None = None,
    wave: str | None = None,
    sprint_name: str | None = None,
    prospect_bypass: bool = False,
    required_tool: str | None = None,
    tool_requirements: Any = None,
    artifact_kind: str | None = None,
    planned_output: Any = None,
    artifact_policy: Any = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Append a new ``todo`` sprint item to a project's checklist.

    ``group`` (stored as ``item_group``) lets items be organised under
    named objectives so the dashboard sprint board can render them in
    logical clusters. ``human_id`` attributes the item to a person.
    ``depends_on`` is the id of a parent sprint item that must be done
    before this item is surfaced as claimable. ``failure_mode`` controls
    what happens when the parent has failed: 'continue' (default) allows
    this item to proceed; 'stop' blocks it.
    ``milestone_type`` is 'task' (default) or 'milestone' — milestones
    render as vertical timeline markers in the sprint swimlane.
    ``deferred_until`` (dec69708) is an ISO timestamp; while it is in the
    future ``claim_sprint_item`` REFUSES the item (enforced deferral, not a
    text-only note). ``track`` buckets the item into a named lane (e.g.
    'paper') so a whole track can be skipped.
    ``priority`` (e08fee30) is one of {urgent, high, normal, low} (default
    'normal'); higher-priority pending items are surfaced/claimed/grouped first.
    ``blocker_kind`` (2282a636) is None (ordinary) or 'manual' (blocked on a
    real-world action outside Meridian) — a manual-blocker is surfaced distinctly
    and excluded from executor scoping, like milestone_type='human'.
    ``sprint_name`` (3d6bd938) is a nullable human-readable label for the sprint
    bucket (e.g. 'docs-cloudflare'), kept separate from ``version`` which should
    stay a structural/semver-like identifier. NULL means no separate name.
    ``required_tool`` (4d1fb28f) is a nullable free-form pin naming the specific
    MCP tool/plugin the executor MUST use for this item (e.g. 'Serena:
    replace_symbol_body'). NULL means ordinary executor discretion. When set,
    it is rendered as a hard directive (not a hint) in the /goal block built by
    ``handoff._build_quick_start_goal`` / ``build_item_briefing``.
    ``tool_requirements`` (76dde31f, 665 follow-up) is the TYPED successor to
    ``required_tool``: a list of normalized entries (see
    ``meridian.tool_requirements.normalize_tool_requirement`` for the schema —
    name, server_or_namespace, required_or_preferred, purpose, call_template,
    fallback, availability_check, verification). Distinct from
    ``touches_resources`` (parallel-conflict scheduling metadata) and from
    ``required_tool`` (a single free-form string) — once set, this structured
    field is the CANONICAL source for what build_item_briefing / the batch
    /goal's ``<tool_requirements>`` clause / the machine-readable capability
    contract render; ``required_tool`` keeps working unchanged and is used as
    a read-time compatibility fallback only when this field is empty (see
    ``tool_requirements.effective_tool_requirements``). Raises ``ValueError``
    (via ``tool_requirements.ToolRequirementError``) on malformed input —
    unknown fields, missing required fields, secret-shaped values, or
    machine-local absolute paths.
    ``artifact_kind`` / ``planned_output`` / ``artifact_policy`` (2f9cb288,
    665 follow-up) are the normalized, persisted artifact declaration
    contract — see ``meridian.artifact_declaration`` for the full schema.
    ``artifact_kind`` is a plain enum (``document_only``/``figure``/``table``),
    NULL meaning "unknown" (never guessed). ``planned_output`` is a typed
    pointer (validated via ``meridian.pointers.validate_pointer`` — NOT a
    free-form path), carrying ``source_type``, ``targets``, ``label``, and
    ``provenance_required``. ``artifact_policy`` is the artifact-pointer-check
    policy (``artifact_pointer_check`` off/warn/strict plus guard flags); an
    absent policy reads back as the project default (warn) via
    ``artifact_declaration.effective_artifact_policy``, never a hard block.
    All three raise ``ValueError`` (via ``artifact_declaration.ArtifactDeclarationError``)
    on malformed input — unknown fields, bad enum values, or a secret-shaped /
    machine-local-absolute-path value in ``planned_output`` (same screen
    ``tool_requirements`` reuses).
    ``notes`` is optional free-form context stored on the item at creation time.

    Duplicate guard (b0d42ef6): unless ``force`` is True, the new ``title``
    is compared (word-set overlap, see ``_title_word_overlap``) against every
    open item in the project (status pending / todo / in_progress). If any
    existing item meets the >= 60% overlap threshold the item is **not**
    inserted and a structured error dict is returned instead::

        {"error": "duplicate", "message": ..., "existing": {id, title,
         status, overlap_pct}}

    The caller can pass ``force=True`` to override the guard and insert
    anyway. Finished items (done / skipped / failed / pushed) never block,
    so legitimately re-doing past work is unaffected.
    """
    if notes is not None:
        from meridian.secret_redaction import check_for_secrets
        check_for_secrets(notes, context="sprint item notes")
    if failure_mode not in (None, "continue", "stop"):
        raise ValueError("failure_mode must be 'continue' or 'stop'")
    if milestone_type not in ("task", "milestone", "human"):
        raise ValueError("milestone_type must be 'task', 'milestone', or 'human'")
    # e08fee30 — validate the priority enum (default 'normal'), mirroring how
    # milestone_type raises on a bad value.
    if priority is None:
        priority = "normal"
    if priority not in _VALID_SPRINT_PRIORITIES:
        raise ValueError(
            f"priority must be one of {_VALID_SPRINT_PRIORITIES}, got {priority!r}"
        )
    # 2282a636 — validate blocker_kind. None = ordinary; see
    # _VALID_SPRINT_BLOCKER_KINDS for the defined values ('manual', 'superseded').
    if blocker_kind is not None and blocker_kind not in _VALID_SPRINT_BLOCKER_KINDS:
        raise ValueError(
            f"blocker_kind must be None or one of {_VALID_SPRINT_BLOCKER_KINDS}, "
            f"got {blocker_kind!r}"
        )
    # b0d42ef6 — block near-duplicate titles against open items unless forced.
    if not force:
        _new_words = _title_word_set(title)
        if _new_words:
            for _ex in await get_sprint_items(db, project_id):
                if _ex.get("status") not in _DUP_BLOCKING_SPRINT_STATUSES:
                    continue
                _overlap = _title_word_overlap(_new_words, _title_word_set(_ex.get("title", "")))
                if _overlap >= _SPRINT_DUP_OVERLAP_THRESHOLD:
                    _pct = round(_overlap * 100)
                    return {
                        "error": "duplicate",
                        "message": (
                            f"Sprint item not created: title is {_pct}% a word-match "
                            f"for existing {_ex['status']} item '{_ex.get('title', '')[:120]}' "
                            f"({_ex['id'][:8]}). Pass force=true to add it anyway, or update "
                            f"the existing item instead."
                        ),
                        "existing": {
                            "id": _ex["id"],
                            "title": _ex.get("title", ""),
                            "status": _ex["status"],
                            "overlap_pct": _pct,
                        },
                    }
    # 501ec93f — normalize + validate typed resource identifiers (raises on bad input).
    resources_json = serialize_touches_resources(touches_resources)
    # 76dde31f (665 follow-up) — normalize + validate the typed tool_requirements
    # contract (raises ToolRequirementError, a ValueError subclass, on bad input —
    # same fail-fast discipline as the touches_resources line above).
    tool_requirements_json = _tool_requirements.serialize_tool_requirements(tool_requirements)
    # 2f9cb288 (665 follow-up) — normalize + validate the typed artifact
    # declaration contract (raises ArtifactDeclarationError, a ValueError
    # subclass, on bad input — same fail-fast discipline as above).
    artifact_kind_value = (
        _artifact_declaration.normalize_artifact_kind(artifact_kind)
        if artifact_kind is not None else None
    )
    planned_output_json = _artifact_declaration.serialize_planned_output(planned_output)
    artifact_policy_json = _artifact_declaration.serialize_artifact_policy(artifact_policy)
    iid = _new_id()
    # b944c905 — auto-populate a human-readable slug from the title (or a
    # caller-supplied one), deduped per project.
    _item_slug = await _unique_sprint_slug(
        db, project_id, _sprint_item_slug_base(slug or title)
    )
    # b6b0cee6 — a short memorable nickname (1-2 words), deduped per project.
    _item_nickname = await _unique_sprint_nickname(
        db, project_id, _sprint_item_nickname_base(title, iid)
    )
    await db.execute(
        "INSERT INTO sprint_items "
        "(id, project_id, version, title, notes, item_group, human_id, depends_on, "
        "failure_mode, milestone_type, touches_resources, slug, nickname, "
        "deferred_until, track, priority, blocker_kind, wave, sprint_name, "
        "prospect_bypass, required_tool, tool_requirements, "
        "artifact_kind, planned_output, artifact_policy) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (iid, project_id, version, title, notes, group, human_id,
         depends_on, failure_mode or "continue", milestone_type, resources_json,
         _item_slug, _item_nickname, deferred_until or None, track or None,
         priority, blocker_kind or None, wave or None, sprint_name or None,
         1 if prospect_bypass else 0, required_tool or None, tool_requirements_json,
         artifact_kind_value, planned_output_json, artifact_policy_json),
    )
    await db.commit()
    item = await get_sprint_item(db, iid)
    assert item is not None
    _invalidate_sprint_items_cache(project_id)
    # ITEM 6 — live push so dashboards refresh the sprint board without polling.
    _publish_project_event(project_id, "sprint_item_added", {"item_id": iid})
    # f9188526 — auto-generate (or refresh) the version bucket description.
    # Runs after the item is committed so the full new item set is visible.
    # Guarded: a description failure NEVER blocks or changes the returned item.
    if version:
        try:
            _all_for_version = await get_sprint_items(db, project_id, version=version)
            _ver_titles = [
                it.get("title", "") for it in _all_for_version if it.get("title")
            ]
            _new_desc = _auto_generate_version_description(version, _ver_titles)
            await upsert_sprint_version_description(db, project_id, version, _new_desc)
        except Exception:  # noqa: BLE001 — description generation must never block
            pass
    return item


def _fan_out_spec_to_batch_entry(spec: Any, index: int) -> dict[str, Any]:
    """Map one ``fan_out_sprint_items`` item-spec onto a
    ``batch_management.execute_batch`` ``sprint_item`` create-entry (468ab67d).

    Only translates fan_out's own historical field synonyms (``sprint`` ->
    ``version``, ``item_group`` -> ``group``, ``description`` -> ``notes`` —
    the exact same mapping the legacy insert loop below already performs);
    everything else (``touches_resources``, ``force``, ``correlation_key``)
    passes through by name unchanged, since it already matches
    :func:`add_sprint_item`'s own kwarg names / ``execute_batch``'s own
    entry shape. A non-dict ``spec`` is passed through as-is so it hits the
    engine's own "entry must be an object" validation error (a real,
    reportable per-entry outcome) instead of crashing this mapper with an
    ``AttributeError`` the way the legacy loop would.
    """
    if not isinstance(spec, dict):
        return spec
    entry: dict[str, Any] = {"action": "create", "title": spec.get("title")}
    version = spec.get("version") or spec.get("sprint")
    if version:
        entry["version"] = version
    group = spec.get("group") or spec.get("item_group")
    if group:
        entry["group"] = group
    description = spec.get("description")
    if description:
        entry["notes"] = description
    if spec.get("touches_resources") is not None:
        entry["touches_resources"] = spec.get("touches_resources")
    if spec.get("force"):
        entry["force"] = True
    ck = spec.get("correlation_key")
    if isinstance(ck, str) and ck.strip():
        entry["correlation_key"] = ck
    return entry


async def fan_out_sprint_items(
    db: aiosqlite.Connection,
    project_id: str,
    items: list[dict[str, Any]],
    *,
    strict: bool = False,
    mode: str = "all_or_nothing",
    idempotency_key: str | None = None,
    tenant_id: str | None = None,
    actor: str | None = None,
) -> list[str] | dict[str, Any]:
    """Bulk-insert sprint items for an orchestrator decomposing a goal.

    ``items`` is a list of dicts, each with at minimum ``title`` (required)
    and optionally ``description``, ``group``, and ``version``.  Missing
    ``version`` defaults to the empty string (same as the common add_sprint_item
    convention).

    Legacy contract (``strict=False``, the default — UNCHANGED, 468ab67d):
    the duplicate guard is **not** applied — the orchestrator is assumed to
    have already deduped. Returns the bare ``list[str]`` of new item IDs in
    insertion order, exactly as before. Every existing caller that never
    passes ``strict=`` sees zero behavior change — this is the
    compatibility-preserving half of 468ab67d's contract.

    Strict, opt-in contract (``strict=True``, 468ab67d): reroutes through
    :func:`meridian.db.batch_management.execute_batch`'s ``sprint_item``
    create path (the SAME :func:`add_sprint_item`-backed engine
    ``execute_batch``/``add_sprint_item`` already use — no second
    duplicate/idempotency heuristic implemented here) instead of the raw
    insert loop below, which gives a caller who explicitly opts in:

    * the 60%-word-overlap duplicate guard (per-item ``force=True`` still
      overrides it, same as :func:`add_sprint_item`);
    * ``idempotency_key`` replay — a retried call with the same
      ``(project_id, "sprint_item", idempotency_key)`` returns the FIRST
      call's stored result verbatim instead of re-inserting;
    * ``mode="all_or_nothing"`` (default) or ``"best_effort"`` batch
      semantics, with compensating rollback on an ``all_or_nothing`` failure;
    * a per-entry outcome (``ok``/``error``/``rolled_back``/``not_attempted``,
      with ``error_code``/``error_message``/``retryable``) referencing the
      created item's own id, instead of a bare id list that gives no
      visibility into what happened to each entry;
    * stable per-entry ``correlation_key`` echoing (an item spec's own
      ``correlation_key``, when supplied) plus the always-present
      deterministic ``index``.

    Returns :class:`meridian.db.batch_management.BatchResult`'s
    ``to_dict()`` when ``strict=True`` — a DIFFERENT shape from the legacy
    ``list[str]`` return, by design: this is a new, explicitly-opted-into
    contract, not a silent change to the existing one. Raises
    :class:`meridian.db.batch_management.BatchEngineError` for a call-level
    contract violation (e.g. every entry in ``items`` failed to map to a
    dict with a title), matching ``execute_batch``'s own raise contract.

    See ``batch_management``'s module docstring ("Compatibility: why
    fan_out_sprint_items / add_sprint_item_pointer are NOT rerouted through
    this engine [by default]") for the full history of why the DEFAULT stays
    the legacy, unguarded loop.
    """
    if strict:
        from meridian.db import batch_management  # noqa: PLC0415 — lazy: db/__init__.py never imports batch_management itself, and this avoids any import-order dependence on where in db/__init__.py's load sequence sprint_items.py is reached.

        entries = [
            _fan_out_spec_to_batch_entry(spec, i) for i, spec in enumerate(items)
        ]
        result = await batch_management.execute_batch(
            db, project_id=project_id, entry_kind="sprint_item", entries=entries,
            mode=mode, idempotency_key=idempotency_key, tenant_id=tenant_id,
            actor=actor,
        )
        return result.to_dict()

    ids: list[str] = []
    for spec in items:
        title = (spec.get("title") or "").strip()
        if not title:
            continue
        version = (spec.get("version") or spec.get("sprint") or "").strip()
        group = spec.get("group") or spec.get("item_group") or None
        description = spec.get("description") or None
        try:
            resources_json = serialize_touches_resources(spec.get("touches_resources"))
        except ValueError:
            resources_json = None  # best-effort in bulk insert — skip bad values
        iid = _new_id()
        # ae87699d — generate slug + nickname on every creation path, not just
        # add_sprint_item. fan_out_sprint_items was leaving both null.
        _item_slug = await _unique_sprint_slug(
            db, project_id, _sprint_item_slug_base(title)
        )
        _item_nickname = await _unique_sprint_nickname(
            db, project_id, _sprint_item_nickname_base(title, iid)
        )
        await db.execute(
            "INSERT INTO sprint_items "
            "(id, project_id, version, title, item_group, notes, touches_resources, "
            "slug, nickname) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (iid, project_id, version, title, group, description, resources_json,
             _item_slug, _item_nickname),
        )
        ids.append(iid)
    if ids:
        await db.commit()
        _invalidate_sprint_items_cache(project_id)
        _publish_project_event(project_id, "sprint_items_fanned_out", {"item_ids": ids})
    return ids


async def get_sprint_item(
    db: aiosqlite.Connection, item_id: str
) -> dict[str, Any] | None:
    """Fetch one sprint item by id."""
    async with db.execute(
        "SELECT * FROM sprint_items WHERE id = ?", (item_id,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row)


async def _transition_status(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    to_status: str,
    from_statuses: list[str] | None = None,
    task_id: str | None = None,
    notes: str | None = None,
    pushed_to: str | None = None,
    actor: str | None = None,
    claimed_at_now: bool = False,
) -> dict[str, Any] | None:
    """Atomic chokepoint for ALL sprint-item status transitions.

    Every public status-changing function (claim/complete/fail/push/skip/patch/
    provisional_complete/start) routes through here so the atomic UPDATE, cache
    bust, and live-event publish are never duplicated.

    ``from_statuses`` — when given, appends ``AND status IN (<from_statuses>)``
    to the WHERE clause so the write is conditional on the item still being in
    an expected state (TOCTOU guard, closes fa3e3331). If the guard fails
    (rowcount == 0 and the item exists) this function returns **None** — it does
    NOT raise :class:`SprintItemStatusRace`. Each calling function is responsible
    for raising the appropriate exception (SprintItemStatusRace, ValueError, etc.)
    when it receives None, matching fa3e3331's intent that races are no-ops at the
    chokepoint level while callers preserve their own error contracts.

    Passing an empty list for ``from_statuses`` is a caller bug (would render
    ``AND status IN ()`` — invalid SQL) and raises ValueError.

    Terminal statuses (done / skipped / failed / pushed) stamp ``completed_at``;
    non-terminal statuses clear it. ``task_id``, ``notes``, ``pushed_to``,
    ``actor`` are optional extra fields. ``claimed_at_now`` sets
    ``claimed_at = datetime('now')`` (used by claim_sprint_item).

    Side effects on success: cache invalidation via
    :func:`_invalidate_sprint_items_cache` and live event via
    :func:`_publish_project_event`. Both are shared here so no caller can forget
    them — the two bugs in 6a17e735 and fa3e3331 that each found a separate
    bypass are closed by this consolidation.
    """
    if to_status not in _VALID_SPRINT_STATUSES:
        raise ValueError(f"invalid sprint-item status: {to_status!r}")
    if from_statuses is not None and not from_statuses:
        # An empty list would render "AND status IN ()" — invalid SQL — and
        # semantically could never match anything, so it's always a caller bug
        # rather than a legitimate "never match" request.
        raise ValueError("from_statuses must be non-empty or None")
    fields = ["status = ?"]
    values: list[Any] = [to_status]
    if to_status in {"done", "skipped", "failed", "pushed"}:
        fields.append("completed_at = datetime('now')")
    else:
        fields.append("completed_at = NULL")
    if to_status == "done":
        fields.append("claimed_at = COALESCE(claimed_at, datetime('now'))")
    if claimed_at_now:
        fields.append("claimed_at = datetime('now')")
    if task_id is not None:
        fields.append("task_id = ?")
        values.append(task_id)
    if notes is not None:
        from meridian.secret_redaction import check_for_secrets
        check_for_secrets(notes, context="sprint item notes")
        fields.append("notes = ?")
        values.append(notes)
    if pushed_to is not None:
        fields.append("pushed_to = ?")
        values.append(pushed_to)
    if actor is not None:
        fields.append("actor = ?")
        values.append(actor)
    values.append(item_id)
    values.append(project_id)
    where = "WHERE id = ? AND project_id = ?"
    if from_statuses is not None:
        ordered_from = sorted(from_statuses)
        where += f" AND status IN ({', '.join('?' for _ in ordered_from)})"
        values.extend(ordered_from)
    cursor = await db.execute(
        f"UPDATE sprint_items SET {', '.join(fields)} {where}",
        values,
    )
    await db.commit()
    if cursor.rowcount == 0:
        return None
    _invalidate_sprint_items_cache(project_id)
    result = await get_sprint_item(db, item_id)
    # Broadcast to dashboard WebSocket subscribers so the sprint board refreshes live.
    _publish_project_event(project_id, "sprint_item_updated", {"item_id": item_id, "status": to_status})
    return result


async def _update_sprint_item_status(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    status: str,
    task_id: str | None = None,
    notes: str | None = None,
    pushed_to: str | None = None,
    actor: str | None = None,
    expected_statuses: set[str] | None = None,
) -> dict[str, Any] | None:
    """Backward-compatible shim — delegates to :func:`_transition_status`.

    fa3e3331 — when ``expected_statuses`` is given and the item exists but its
    status is not in the expected set (race-lost), this shim re-raises
    :class:`SprintItemStatusRace` so every EXISTING caller keeps its error
    contract unchanged. New callers should prefer :func:`_transition_status`
    directly and handle the None return themselves.
    """
    from_statuses_list = sorted(expected_statuses) if expected_statuses is not None else None
    if expected_statuses is not None and not expected_statuses:
        raise ValueError("expected_statuses must be non-empty or None")
    result = await _transition_status(
        db, project_id, item_id, status,
        from_statuses=from_statuses_list,
        task_id=task_id, notes=notes, pushed_to=pushed_to, actor=actor,
    )
    if result is None and expected_statuses is not None:
        _raced = await get_sprint_item(db, item_id)
        if _raced is not None and _raced.get("project_id") == project_id:
            raise SprintItemStatusRace(item_id, _raced.get("status"), expected_statuses)
    return result


async def _maybe_rollup_parent(db: aiosqlite.Connection, project_id: str, item_id: str) -> None:
    """After a child status change, roll up sibling statuses to parent if applicable."""
    item = await get_sprint_item(db, item_id)
    if item is None:
        return
    parent_id = item.get("parent_id")
    if not parent_id:
        return
    async with db.execute(
        "SELECT status FROM sprint_items WHERE parent_id = ? AND project_id = ?",
        (parent_id, project_id),
    ) as cur:
        rows = await cur.fetchall()
    statuses = [
        (r["status"] if isinstance(r, dict) else r[0]) or "pending" for r in rows
    ]
    if not statuses:
        return
    has_active = any(s in _ACTIVE_SPRINT_STATUSES for s in statuses)
    if has_active:
        return
    has_failed = any(s == "failed" for s in statuses)
    all_terminal_ok = all(s in {"done", "skipped"} for s in statuses)
    if all_terminal_ok:
        await _update_sprint_item_status(db, project_id, parent_id, "done")
    elif has_failed:
        await _update_sprint_item_status(db, project_id, parent_id, "indeterminate")


class SprintItemEvidenceRequired(ValueError):
    """Raised when complete_sprint_item is blocked by the required_notes gate
    (5823db0b) — the item is flagged required_notes but has no evidence."""


class SprintItemVerificationRequired(ValueError):
    """Raised when complete_sprint_item is blocked by the require_verification
    gate (e2e1b682) — the item needs an independent, fresh-session PASS on
    file (filed by a session distinct from the one completing it) before the
    completion is allowed to stick."""


class SprintItemClaimMismatch(ValueError):
    """Raised when complete_sprint_item is blocked by the claim-ownership gate
    (8693b6a8) — the completing actor differs from the item's claim owner and
    the claim is neither stale nor force-acknowledged. See complete_sprint_item's
    docstring for the staleness/force escape hatch."""


# 8693b6a8 — claim-ownership staleness threshold for complete_sprint_item.
#
# Mirrors the pre-existing STALE_CLAIM concept used by claim_sprint_item's own
# caller (meridian/mcp/handlers/sprint_tools.py, 10c0f6a0: "claimed > 2h ago
# with no recent activity"), which itself mirrors db/locks.py's
# _CLAIM_LIVE_HOURS (== _FILE_LOCK_TTL_HOURS == 2) heartbeat-expiry threshold
# for file/symbol claims. Reusing the SAME 2h number here (rather than the
# longer, planner-facing _SPRINT_STALL_FLAG_HOURS above) keeps "is this claim
# stale" answering identically everywhere a caller might ask it — at claim
# time, at completion time, or via the file-lock heartbeat sweep.
_CLAIM_OWNERSHIP_STALE_HOURS = 2


# ---------------------------------------------------------------------------
# e2e1b682 — independent fresh-session verifier gate for sprint completion.
#
# Closes the "hallucinated-compliance completion" gap: without this, nothing
# stopped the SAME session that did (or merely claims to have done) the work
# from also being the one that reports it done — required_notes only checks
# that *some* evidence text exists, not that anyone independent looked at it.
#
# sprint_items.require_verification (opt-in INTEGER 0/1, mirrors
# prospect_bypass's structural-gate shape) marks an item as needing an
# INDEPENDENT PASS before complete_sprint_item is allowed to stick.
#
# Trust boundary (same shape as verify_handoff_token, see AGENTS.md 2ee0000c):
# this layer structurally proves WHO filed the verdict and THAT it differs
# from the completing actor — it does NOT (and cannot, from the DB layer
# alone) prove HOW the verdict was derived. The "fresh, no-memory subsession
# with read-only tools" contract is a process-level convention the launcher
# of the verifier subsession must honour; what IS enforced here, structurally:
#   1. A PASS verdict must exist on file (sprint_item_verifications).
#   2. Its verifier_session_id must differ from the actor completing the item
#      (independence — the same session cannot mark its own homework).
#   3. A FAIL verdict (or no verdict at all) refuses completion outright.
# ---------------------------------------------------------------------------

_SPRINT_ITEM_VERIFICATIONS_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS sprint_item_verifications ("
    "    id TEXT PRIMARY KEY,"
    "    project_id TEXT NOT NULL,"
    "    sprint_item_id TEXT NOT NULL,"
    "    verdict TEXT NOT NULL,"              # 'pass' | 'fail'
    "    verifier_session_id TEXT NOT NULL,"
    "    notes TEXT,"
    "    seq INTEGER NOT NULL DEFAULT 0,"     # per-item insertion order (see below)
    "    created_at TEXT NOT NULL DEFAULT (datetime('now'))"
    ")"
)

_VALID_VERIFICATION_VERDICTS = {"pass", "fail"}


async def _ensure_sprint_item_verifications_table(db: aiosqlite.Connection) -> None:
    """Idempotently create sprint_item_verifications (tolerates concurrent init;
    mirrors _ensure_wave_gate_results_table)."""
    await db.execute(_SPRINT_ITEM_VERIFICATIONS_TABLE_DDL)


async def record_sprint_item_verification(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    verifier_session_id: str,
    verdict: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """Persist one independent fresh-session PASS/FAIL verdict for a sprint item.

    ``verifier_session_id`` identifies the session that performed the
    independent check. Whether it is genuinely a *different* session from the
    one that implemented / is completing the item is checked at the
    ``complete_sprint_item`` gate, not here — this function only records the
    verdict. Returns the stored row (dict).

    ``seq`` — NOT wall-clock ``created_at`` — is what determines "latest":
    computed here as ``1 + MAX(existing seq for this item)``. Neither
    backend's clock is trustworthy enough for strict per-item ordering — the
    random-UUID ``id`` doesn't correlate with insertion order at all, SQLite's
    ``datetime('now')`` is only second-granular, and even Python's own
    ``datetime.now()`` can tie between two awaited calls on coarser system
    timers (observed on Windows). A FAIL followed by a same-tick re-check PASS
    (the core "fix and re-verify" workflow this table exists for) must never
    be ambiguous, so ordering is driven by an explicit, clock-independent
    counter instead. Not fully race-proof under truly concurrent writers on
    the SAME item across processes (same caveat as this server's other
    single-process-assuming counters, e.g. routes/sprint.py's stop-override
    budget) — acceptable here since a rare mis-ordered tie only affects which
    of two verdicts filed in the same instant is treated as "latest", never
    data loss or a false PASS.
    """
    if not (verifier_session_id or "").strip():
        raise ValueError(
            "record_sprint_item_verification requires a non-empty verifier_session_id"
        )
    verdict_norm = (verdict or "").strip().lower()
    if verdict_norm not in _VALID_VERIFICATION_VERDICTS:
        raise ValueError(
            f"verdict must be one of {sorted(_VALID_VERIFICATION_VERDICTS)}, got {verdict!r}"
        )
    await _ensure_sprint_item_verifications_table(db)
    vid = _new_id()
    async with db.execute(
        "SELECT COALESCE(MAX(seq), 0) AS m FROM sprint_item_verifications "
        "WHERE project_id = ? AND sprint_item_id = ?",
        (project_id, item_id),
    ) as cur:
        _seq_row = await cur.fetchone()
    next_seq = int((_seq_row["m"] if isinstance(_seq_row, dict) else _seq_row[0]) or 0) + 1
    await db.execute(
        "INSERT INTO sprint_item_verifications "
        "(id, project_id, sprint_item_id, verdict, verifier_session_id, notes, seq) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (vid, project_id, item_id, verdict_norm, verifier_session_id, notes, next_seq),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM sprint_item_verifications WHERE id = ?", (vid,)
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) or {}


async def get_latest_sprint_item_verification(
    db: aiosqlite.Connection, project_id: str, item_id: str
) -> dict[str, Any] | None:
    """Return the most recently filed verification row for a sprint item, or
    None if it has never been independently verified.

    Ordered by ``seq DESC`` (see :func:`record_sprint_item_verification` for
    why wall-clock ``created_at`` isn't a reliable enough ordering key on its
    own); ``created_at DESC, id DESC`` are kept only as defensive tiebreakers
    for pre-existing rows with ``seq = 0`` (the column's default, e.g. rows
    written before this counter existed).
    """
    await _ensure_sprint_item_verifications_table(db)
    async with db.execute(
        "SELECT * FROM sprint_item_verifications "
        "WHERE project_id = ? AND sprint_item_id = ? "
        "ORDER BY seq DESC, created_at DESC, id DESC LIMIT 1",
        (project_id, item_id),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) if row is not None else None


async def count_sprint_items_awaiting_verification(
    db: aiosqlite.Connection, project_id: str
) -> int:
    """e2e1b682 — count of in_progress, require_verification items that do NOT
    yet have an independent on-file PASS (no verdict at all, a FAIL, or only a
    same-session self-report). Purely informational: backs the Stop-hook
    guard's advisory ``verification_pending_count`` (routes/sprint.py's
    ``/sprint/pending_count`` endpoint) so a human watching the guard's output
    sees that a fresh check is still owed even though this never blocks a
    session from stopping (only complete_sprint_item's structural gate blocks
    the completion itself).
    """
    async with db.execute(
        "SELECT id, actor FROM sprint_items "
        "WHERE project_id = ? AND status = 'in_progress' AND require_verification = 1",
        (project_id,),
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        return 0
    n = 0
    for r in rows:
        row = _row_to_dict(r) or {}
        item_id = row.get("id")
        actor = row.get("actor")
        verification = await get_latest_sprint_item_verification(db, project_id, item_id)
        if (
            verification is None
            or verification.get("verdict") != "pass"
            or (actor and verification.get("verifier_session_id") == actor)
        ):
            n += 1
    return n


class SprintItemStatusRace(ValueError):
    """Raised by _update_sprint_item_status (fa3e3331) when an ``expected_statuses``
    guard rejects a transition — the item exists but is no longer in an expected
    from-state, because a concurrent transition committed first. Distinguishes
    this from ``cursor.rowcount == 0`` meaning "item not found", which a bare
    ``None`` return could not — a caller could not previously tell "doesn't
    exist" from "lost a race" and could not react correctly to either."""

    def __init__(self, item_id: str, current_status: str | None, expected_statuses: set[str]):
        self.item_id = item_id
        self.current_status = current_status
        self.expected_statuses = expected_statuses
        super().__init__(
            f"sprint item {item_id} is no longer in an expected state for this "
            f"transition (current status: {current_status!r}, expected one of "
            f"{sorted(expected_statuses)}) — another caller already changed it. "
            "Re-fetch the item before retrying."
        )


# 6a17e735 — the ONLY statuses patch_sprint_item may set directly. These are
# plain administrative resets with no attached business logic: no evidence
# gate, no completed_at semantics beyond "clear it", no parent rollup, no
# task-chain advancement. Every OTHER status has a dedicated function
# (complete_sprint_item -> done, skip_sprint_item -> skipped, fail_sprint_item
# -> failed, provisional_complete_sprint_item -> provisional_complete,
# claim_sprint_item -> in_progress) that enforces real guards
# _update_sprint_item_status's raw UPDATE does not replicate on its own —
# required_notes evidence, completed_at stamping, claimed_at backfill, parent
# rollup, task-chain advancement, cache invalidation, and the live dashboard
# event. Letting patch_sprint_item set an arbitrary status silently bypassed
# ALL of that: a required_notes item could be marked "done" with zero
# evidence, a parent's rollup could desync, and the sprint-items cache could
# go stale. Confirmed as a genuine backdoor, not a theoretical one — see
# meridian/routes/sprint.py's PATCH endpoint, which had already independently
# self-restricted to this exact non-terminal subset for its own safety;
# patch_sprint_item now enforces that restriction itself so every OTHER
# caller (including any future one) gets the same protection for free instead
# of having to remember to add it.
_PATCH_SPRINT_ITEM_ALLOWED_STATUSES = {"pending", "todo", "indeterminate"}


def _check_evidence_quality(evidence_text: str) -> str | None:
    """fd2800ae — lightweight heuristic for over-fit evidence in required_notes completions.

    Returns a warning string when the submitted evidence text looks suspiciously
    narrow — a signal that the "fix" may be over-fitted to a single test case
    rather than a genuine structural change. Returns None when the evidence looks
    plausibly substantive.

    This check is intentionally CONSERVATIVE. False positives here would block
    legitimate autonomous workflows, so the thresholds are kept high and the
    patterns are restricted to clear structural red-flags:

    1. **Too short**: evidence shorter than 30 characters (after stripping) cannot
       meaningfully describe what changed or how it was verified. A bare "done" or
       "test passed" tells the reader nothing structural.

    2. **Single-test-only pattern**: evidence that only mentions a single test
       function name (``test_*``) with no accompanying mention of a file path, a
       function/method name outside the test itself, or a module name — e.g.
       "test_foo_bar passes" — is a strong signal that the fixer ran exactly one
       test and stopped, which is the canonical over-fit symptom.

    3. **Explicit single-test pass claim with no broader context**: phrases like
       "1 test passed", "one test passed", or "the test passed" with no mention
       of a file, module, or changed function. A real fix should cite the suite
       run count or name the thing that was changed.

    This function is NOT a NLP classifier and deliberately avoids any ML or fuzzy
    scoring. The three rules above are each independently verifiable by reading
    this function. Easy to disable one rule if it proves too noisy.

    Called only when ``required_notes`` is set; the heuristic is silent (returns
    None) for non-gated completions where evidence was not required at all.
    """
    text = (evidence_text or "").strip()
    if not text:
        # No evidence at all — the hard gate above already refused; this branch
        # is unreachable in normal flow but is safe to return None for anyway.
        return None

    # Rule 1 — too short to describe a mechanism.
    _MIN_EVIDENCE_LENGTH = 30
    if len(text) < _MIN_EVIDENCE_LENGTH:
        return (
            f"Evidence text is very short ({len(text)} chars). "
            "A genuine fix should describe what was changed and how it was verified, "
            "not just confirm a test ran. Consider adding the file/function changed "
            "and a brief description of the fix mechanism."
        )

    text_lower = text.lower()

    # Rule 2 — single test function name with no structural context.
    # Pattern: one or more "test_<name>" references but no mention of a file path
    # (foo.py / foo/bar.py) or a non-test function/method name (word followed by
    # an open paren, or "def <name>"). We check for exactly one test_ mention and
    # no other structural keywords.
    _test_refs = re.findall(r"\btest_[a-z0-9_]+", text_lower)
    _has_file_ref = bool(re.search(r"\b\w+\.py\b", text_lower))
    _has_func_ref = bool(re.search(r"\bdef\s+\w+|\b\w+\(", text_lower))
    _has_module_ref = bool(re.search(r"\bmeridian\b|\bdb\b|\bserver\b|\bhandler\b", text_lower))
    if (
        len(_test_refs) == 1
        and not _has_file_ref
        and not _has_func_ref
        and not _has_module_ref
    ):
        return (
            f"Evidence only references a single test function ({_test_refs[0]}) "
            "with no mention of the file, module, or function that was actually changed. "
            "A structural fix should describe WHAT was changed (file/function), not only "
            "WHICH test now passes — a hardcoded return value can satisfy a single test "
            "without fixing the underlying problem."
        )

    # Rule 3 — explicit single-test-pass claim with no broader context.
    # Matches "1 test passed", "one test passed", "the test passed",
    # "a test passed", "test passed" (bare), all case-insensitively.
    _single_pass_pattern = re.compile(
        r"\b(1|one|the|a)\s+test\s+pass(ed|es)|\btest\s+pass(ed|es)\b",
        re.IGNORECASE,
    )
    if _single_pass_pattern.search(text) and not _has_file_ref and not _has_module_ref:
        return (
            "Evidence claims a single test passed with no mention of what changed "
            "structurally (file, module, or function). Completing an item by making "
            "exactly one test pass is a known over-fit risk — a real fix should be "
            "verifiable across the broader suite."
        )

    return None


async def _check_stored_evidence(
    db: aiosqlite.Connection,
    item: dict[str, Any],
    task_id: str | None,
    notes: str | None,
) -> str | None:
    """1ec33edf (refile of abb7c388 — the original shipped only on a worktree
    branch that never merged into dev, so this was reconfirmed unfixed and
    redone here) — mechanical check that STORED evidence actually exists at
    completion time.

    Distinct from :func:`_check_evidence_quality`, which analyses the *text
    quality* of evidence notes for over-fit patterns without touching disk or
    the DB. This function checks whether the physical evidence referenced by
    the item is real and verifiable:

    1. **touches_resources file/symbol entries** — when the item declares
       ``file:`` or ``symbol:`` resources (the canonical "what I touched"
       record), check that at least one of those files exists on the
       filesystem. If every declared file is absent, the evidence is likely
       fabricated, stale, or was produced in a different worktree.

    2. **task_id existence** — when a ``task_id`` is linked (either as
       argument or already stored on the item), verify the ``task_log`` row
       actually exists in the DB. A task_id that resolves to nothing is
       hollow evidence.

    3. **File paths mentioned in notes** — when the combined notes text
       explicitly mentions plausible ``.py`` paths, check at least one exists
       on the filesystem. A notes field claiming "fixed auth.py" where
       ``auth.py`` cannot be found is a thin-evidence signal. Only runs when
       there is no touches_resources declaration, to avoid double-warning on
       the same completion call.

    Design invariants (mirrors :func:`_check_evidence_quality`):
    - **ADVISORY ONLY** — returns a warning string but never raises; the
      calling :func:`complete_sprint_item` surfaces it as
      ``stored_evidence_warning`` in the returned dict without blocking the
      completion.
    - **FAIL-OPEN** — any filesystem or DB error is swallowed; the function
      returns ``None`` (no warning) rather than wedging the board.
    - **Conservative** — only warns when there is a clear, high-confidence
      signal that claimed evidence does not exist, not merely when evidence
      is absent (many legitimate items don't declare touches_resources).
    - Runs for ALL completions, not only ``required_notes`` ones — a declared
      touches_resources or linked task_id that turns out not to exist is a
      thin-evidence signal regardless of whether the item was gated.
    """
    import os  # noqa: PLC0415 — lazy: only used in this function

    try:
        # ------------------------------------------------------------------
        # Check 1: touches_resources file/symbol entries exist on disk.
        # Only warn when the item DOES declare file/symbol resources AND
        # none of them can be found — the absence of a declaration is fine
        # (many items don't declare touches_resources).
        # ------------------------------------------------------------------
        resources_raw = item.get("touches_resources")
        if resources_raw:
            resources = parse_touches_resources(resources_raw)
            # Collect file paths declared via file: or symbol: resource ids.
            declared_paths: list[str] = []
            for rid in resources:
                rid_lower = rid.lower()
                if rid_lower.startswith("inferred:"):
                    rid = rid[len("inferred:"):]
                if rid.startswith("file:"):
                    path = rid[len("file:"):]
                    declared_paths.append(path)
                elif rid.startswith("symbol:"):
                    # symbol:<path>::<symbol> — extract the path part.
                    path = rid[len("symbol:"):].partition("::")[0]
                    declared_paths.append(path)
            if declared_paths:
                any_exists = any(os.path.exists(p) for p in declared_paths)
                if not any_exists:
                    absent = declared_paths[:3]  # cap the list for readability
                    more = f" (and {len(declared_paths) - 3} more)" if len(declared_paths) > 3 else ""
                    return (
                        f"Stored evidence check: {len(declared_paths)} file(s) declared in "
                        f"touches_resources cannot be found on disk: "
                        f"{', '.join(absent)}{more}. "
                        "Either the files were not actually modified, the paths are wrong, "
                        "or the work was done in a different worktree. "
                        "Verify the correct files were changed before treating this as complete."
                    )

        # ------------------------------------------------------------------
        # Check 2: task_id resolves to an existing task_log row.
        # Only warn when a task_id IS provided; absence is fine.
        # ------------------------------------------------------------------
        effective_task_id = task_id or item.get("task_id")
        if effective_task_id:
            async with db.execute(
                "SELECT id FROM task_log WHERE id = ?", (effective_task_id,)
            ) as cur:
                task_row = await cur.fetchone()
            if task_row is None:
                return (
                    f"Stored evidence check: task_id {effective_task_id!r} is linked as "
                    "evidence but no matching task_log row was found. "
                    "The task may have been deleted or the id may be incorrect. "
                    "Pass notes=... describing what shipped, or link a valid task_id."
                )

        # ------------------------------------------------------------------
        # Check 3: file paths explicitly mentioned in notes exist on disk.
        # Only active when there are no touches_resources declarations
        # (Check 1 already covered the declared-path case), to avoid
        # double-warning on the same completion call.
        # Only warn when ALL mentioned paths are absent (fail-open: a mix of
        # present and absent paths is not suspicious — paths change over time).
        # ------------------------------------------------------------------
        if not resources_raw:
            combined_notes = " ".join(filter(None, [
                (notes or "").strip(),
                (item.get("notes") or "").strip(),
            ]))
            if combined_notes:
                mentioned_paths = re.findall(
                    r"[\w/\\.-]+\.py\b", combined_notes
                )
                # Only count paths that look structural (contain a slash, or
                # start with a known top-level dir) — excludes bare "a.py".
                plausible_paths = [
                    p for p in mentioned_paths
                    if len(p) > 5 and ("/" in p or p.startswith("tests/") or p.startswith("meridian/"))
                ]
                if plausible_paths:
                    any_exists = any(os.path.exists(p) for p in plausible_paths)
                    if not any_exists:
                        absent = plausible_paths[:3]
                        more = f" (and {len(plausible_paths) - 3} more)" if len(plausible_paths) > 3 else ""
                        return (
                            f"Stored evidence check: notes mention {len(plausible_paths)} file path(s) "
                            f"that cannot be found on disk: {', '.join(absent)}{more}. "
                            "If work was done in a different worktree, the paths may be valid "
                            "there but are unverifiable here. Consider adding the commit SHA or "
                            "test run output as evidence."
                        )

    except Exception:  # noqa: BLE001 — stored-evidence check must never block completion
        return None

    return None


_VALID_GITHUB_ISSUE_SOURCES = {"meridian_auto", "manual"}


async def link_sprint_item_github_issue(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    issue_number: int,
    issue_url: str | None,
    source: str,
) -> dict[str, Any] | None:
    """fdaa5b55 / eda40627 — the ONE write path for a sprint item's linked
    GitHub issue + its trust classification.

    ``source`` MUST be ``'meridian_auto'`` (Meridian itself filed the issue —
    only ever passed by server.py's ``_on_hitl_answered`` 'proposal_github_issue'
    branch, right after a real ``create_issue`` GitHub API call succeeds) or
    ``'manual'`` (a human filed it, or a session linked one on the human's own
    initiative). Any other value raises ``ValueError`` — there is deliberately
    no way to write anything else, and NOTHING in this codebase ever derives
    ``source`` from an issue's title/body/labels/custom fields; it is always
    an explicit, deterministic argument from a known call site.

    Returns the updated item row, or ``None`` if ``item_id`` doesn't exist
    under ``project_id`` (mirrors the other status-transition helpers' no-op-
    on-miss behaviour — never raises for a stale/foreign id).
    """
    if source not in _VALID_GITHUB_ISSUE_SOURCES:
        raise ValueError(
            f"github_issue_source must be one of {sorted(_VALID_GITHUB_ISSUE_SOURCES)}; got {source!r}"
        )
    cursor = await db.execute(
        "UPDATE sprint_items SET github_issue_number = ?, github_issue_url = ?, "
        "github_issue_source = ? WHERE id = ? AND project_id = ?",
        (issue_number, issue_url, source, item_id, project_id),
    )
    await db.commit()
    if cursor.rowcount == 0:
        return None
    _invalidate_sprint_items_cache(project_id)
    return await get_sprint_item(db, item_id)


def build_github_completion_comment(
    item: dict[str, Any],
    notes: str | None = None,
    task_id: str | None = None,
    *,
    proposed: bool = False,
) -> str:
    """fdaa5b55 / cd038235 — build the GitHub issue completion comment body.

    SECURITY: every fragment interpolated here comes ONLY from Meridian's own
    DB-stored notes/evidence (the sprint item's ``title``/``notes`` fields and
    the caller-supplied ``notes``/``task_id``) — this function never reads a
    GitHub issue's own body or comments as input (that would let anyone with
    issue-open access on a PUBLIC repo inject text back into the very comment
    Meridian posts). Each fragment is still run through the SAME
    ``xml.sax.saxutils.escape`` helper 5abf3e12 established for /goal
    generation (meridian/handoff.py's ``_xml_escape``) before being
    interpolated — defense in depth, in case a notes field was ever itself
    populated from less-trusted upstream content. Escaping happens here, at
    the point this text is emitted toward GitHub, not just at DB-write time.

    ``proposed=True`` renders the "manual issue — proposing closure, needs
    human review" framing instead of the "auto-closed" framing; the caller
    decides which based on the item's ``github_issue_source`` DB column
    (never anything read back from GitHub — see 8c170bcc).
    """
    title = _xml_escape((item.get("title") or "(untitled sprint item)").strip())
    combined = " ".join(filter(None, [
        (notes or "").strip(),
        (item.get("notes") or "").strip(),
    ])).strip()
    evidence = _xml_escape(combined) if combined else "(no additional notes recorded)"
    lines = [
        f"Sprint item **{title}** was marked complete in Meridian.",
        "",
        "Evidence / notes:",
        evidence,
    ]
    _linked_task = task_id or item.get("task_id")
    if _linked_task:
        lines.append("")
        lines.append(f"Linked task: `{_xml_escape(str(_linked_task))}`")
    lines.append("")
    if proposed:
        lines.append(
            "_This issue was not created by Meridian's automated flow, so it "
            "was **not** auto-closed. Proposing closure — please review and "
            "close manually if this looks right._"
        )
    else:
        lines.append("_Closing automatically — this issue was created by Meridian._")
    return "\n".join(lines)


async def complete_sprint_item(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    task_id: str | None = None,
    notes: str | None = None,
    actor: str | None = None,
    verifier_session_id: str | None = None,
    verification_verdict: str | None = None,
    verification_notes: str | None = None,
    force_foreign_claim: bool = False,
    correlation_id: str | None = None,
    exit_code: int | None = None,
) -> dict[str, Any] | None:
    """Mark a sprint item ``done`` and optionally link the task that shipped it.

    8693b6a8 — claim-ownership verification: previously ANY caller could
    complete ANY ``in_progress`` item regardless of who held its claim (the
    ``actor`` stamped on it by ``claim_sprint_item``) — a structural gap, not
    a deliberate design. This adds the ownership check, but preserves the
    established, legitimate pattern of a live session closing out items left
    ``in_progress`` under a different, dead/abandoned session:

    * If the item has no recorded claim ``actor``, or the completing
      ``actor`` matches it exactly, completion proceeds as before — no change.
    * If the completing ``actor`` differs from a *recorded* claim owner, the
      claim must be verifiably stale before completion is allowed: either
      ``claimed_at`` is older than :data:`_CLAIM_OWNERSHIP_STALE_HOURS` (the
      same 2h threshold ``claim_sprint_item``'s own STALE_CLAIM detection
      uses — see 10c0f6a0 / db/locks.py's ``_CLAIM_LIVE_HOURS``), or the
      claiming session is itself dead (not found, or ``status`` is
      ``closed``/``archived``, or its heartbeat ``last_seen`` has gone cold
      past the same threshold).
    * If neither is true, the caller may still complete by passing
      ``force_foreign_claim=True`` — an explicit acknowledgement that this is
      someone else's live, non-stale claim (mirrors the existing
      ``override_ci`` escape-hatch shape used elsewhere in this module).
      Otherwise :class:`SprintItemClaimMismatch` is raised.
    * A completing call that supplies no ``actor`` at all cannot be checked
      against anything and is left alone (fail-open), matching every other
      structural gate in this module (deferred/superseded/unprospected all
      fail open on missing/unparseable data) and preserving callers that have
      never plumbed ``actor`` through (tests, the legacy stdio MCP path).

    4f02340e — when the completed item is part of a mixed-ownership subtask
    chain, advance the chain: an AI→human transition auto-files a HITL handoff,
    a human→AI transition un-blocks the next AI subtask (see
    :func:`_advance_task_chain`).

    5823db0b — quality gate: when the item is flagged ``required_notes``, refuse
    to complete unless evidence exists — an existing ``notes`` value, a linked
    ``task_id``, or a ``notes`` argument on this call (which is persisted).
    ``actor`` records which executor completed the item.

    fd2800ae — evidence quality heuristic: when ``required_notes`` is set and
    evidence passes the existence gate, run :func:`_check_evidence_quality` over
    the combined evidence text. If the heuristic fires, the returned item dict
    gains an ``evidence_quality_warning`` key with an explanation. This is
    ADVISORY ONLY — it never blocks completion and never raises. The heuristic
    is conservative (high thresholds, three simple structural rules) to avoid
    false positives on legitimate autonomous workflows.

    e2e1b682 — independent fresh-session verifier gate: when the item is
    flagged ``require_verification``, completion is refused (raises
    :class:`SprintItemVerificationRequired`) unless an on-file
    ``sprint_item_verifications`` row has ``verdict == "pass"`` AND its
    ``verifier_session_id`` differs from ``actor`` (the session completing the
    item) — a same-session self-report does not satisfy the gate. Pass
    ``verifier_session_id`` + ``verification_verdict`` (``"pass"``/``"fail"``,
    optionally ``verification_notes``) to file a fresh verdict and have it
    checked in this same call; omit them to check whatever verdict is already
    on file. This is a STRUCTURAL gate — it never silently downgrades to an
    advisory warning the way the evidence-quality heuristic does.

    1ec33edf (refile of abb7c388) — stored evidence verification: after the
    existence + quality checks above, run :func:`_check_stored_evidence` over
    the item's touches_resources, task_id, and notes. This is a MECHANICAL
    check that the physical evidence declared by the item actually exists
    (files on disk, task_log rows in DB) — a real evidence check, not just
    the required_notes text-presence gate. Runs for every completion, not
    only required_notes ones. If it fires, the returned item dict gains a
    ``stored_evidence_warning`` key. Also ADVISORY ONLY — never blocks
    completion; fail-open on any error.

    fdaa5b55 — the returned row carries whatever ``github_issue_number`` /
    ``github_issue_url`` / ``github_issue_source`` this item already had (a
    plain ``SELECT *`` — nothing else is touched or looked up here). This
    function does NOT itself call GitHub: closing/commenting requires a
    tenant's GitHub PAT, which this DB-only layer never has access to. The
    MCP handler layer (meridian/mcp/handlers/sprint_tools.py,
    ``handle_complete_sprint_item``) reads the returned
    ``github_issue_number``/``github_issue_source`` and — via
    ``meridian.mcp.handler._close_or_propose_github_issue`` — either auto-
    closes (``github_issue_source == 'meridian_auto'``) or posts a proposed-
    closure comment + files a non-blocking HITL (anything else). That
    downstream step only ever touches the ONE issue linked to THIS item
    (8fc92474) and classifies purely from the DB column above, never from
    issue title/body/labels/custom fields (eda40627/8c170bcc).

    a2a027cf — timeout-safe / observable / idempotent completion. Repeated
    live reports: an MCP/HTTP client times out around 60s waiting on this
    call even though the server-side write had already landed; a defensive
    retry then either re-observed "done" (confusing) or hit a bare
    SprintItemStatusRace (actively misleading — nothing raced, the ORIGINAL
    call's own write simply finished after the client stopped waiting for
    it). This adds:

    * ``correlation_id`` — caller-supplied (thread this down from an MCP/
      HTTP request id when you have one) or freshly minted here when
      omitted. Always present on the returned dict as ``correlation_id`` so
      a client that times out before seeing the response can still
      correlate a server-side log line with the retry it's about to make.
    * ``phase_timings_ms`` — wall-clock duration of each internal phase
      (``lookup``, ``ownership_check``, ``verification_check``,
      ``evidence_check``, ``stored_evidence_check``, ``status_transition``,
      ``post_commit_advisory``), rounded to milliseconds. Purely
      observational — never affects control flow. Lets a slow phase be
      identified from the response itself instead of guessed from a raw
      client-side timeout.
    * ``completion_outcome`` — ``"committed"`` when THIS call performed the
      active->done transition, or ``"already_committed"`` when the item was
      ALREADY ``done`` on entry (an idempotent no-op — see below). Absent
      when the call raises.
    * Idempotent retry: if the item is ALREADY ``done`` when this function
      is entered, every gate below (ownership / verification / evidence)
      and every side effect (rollup, task-chain advance, GitHub-issue
      close, notifications) is SKIPPED — the current row is returned
      immediately with ``completion_outcome="already_committed"``. A
      completion call whose target state is already reached is a no-op
      success, not grounds to re-run gates whose only purpose was deciding
      whether the active->done transition may proceed, or to re-fire
      side effects that already fired once (duplicate HITL filings,
      duplicate GitHub comments, etc. — "a timeout must never cause
      duplicate completion, duplicate side effects, or misleading
      failure"). A concurrent race against a DIFFERENT terminal status
      (e.g. someone skipped/failed it first) is UNCHANGED: that still
      raises :class:`SprintItemStatusRace` exactly as before, because that
      IS a genuine conflicting outcome, not a replay of this same
      completion.
    * Bounded advisory work: the two purely-derived-state post-commit steps
      (:func:`_maybe_rollup_parent`, :func:`_advance_task_chain`) run under
      a single bounded ``asyncio.wait_for`` budget
      (:data:`_ADVISORY_PHASE_TIMEOUT_S`) so a slow rollup/chain-advance can
      never hold the ALREADY-committed response hostage. On timeout the
      commit itself is untouched (it already landed before this phase
      starts) — the response carries ``advisory_work_deferred: true``
      instead of hanging. The continuation-state gather (also advisory) is
      bounded the same way.

    7d71d6bc — ``exit_code`` (optional): when this item is a live child of
    an ACTIVE (non-terminal) wave run, a genuine, freshly-committed
    completion (``completion_outcome == "committed"``, never the idempotent
    ``"already_committed"`` replay) records the wave-run child's terminal
    outcome (``status="succeeded"``) onto its ``wave_run_children`` row via
    :func:`meridian.db.wave_runs.record_wave_run_child_outcome` —
    ``exit_code`` is threaded straight through so the REAL subprocess exit
    code of whatever ran this item's work is preserved, not just a
    pass/fail boolean. Best-effort and fail-open, same as every other
    wave-run bookkeeping hook in this module: never lets wave-run
    bookkeeping block or fail a completion that has already committed. A
    project that never calls ``start_wave_run`` sees zero behavior change.
    """
    _t_start = time.monotonic()
    _phase_ms: dict[str, float] = {}
    _last_t = _t_start

    def _mark_phase(name: str) -> None:
        nonlocal _last_t
        _now = time.monotonic()
        _phase_ms[name] = round((_now - _last_t) * 1000, 3)
        _last_t = _now

    _correlation_id = correlation_id or _new_id()

    item = await get_sprint_item(db, item_id)
    _mark_phase("lookup")

    if (
        item is not None
        and item.get("project_id") == project_id
        and item.get("status") == "done"
    ):
        # a2a027cf — idempotent retry / already-committed short-circuit. See
        # the docstring above: no gates, no side effects, just the current
        # row plus the observability fields. This is a no-op success.
        result = dict(item)
        result["completion_outcome"] = "already_committed"
        result["correlation_id"] = _correlation_id
        result["phase_timings_ms"] = dict(_phase_ms)
        return result

    _evidence_quality_warning: str | None = None
    _stored_evidence_warning: str | None = None
    if item is not None and item.get("project_id") == project_id:
        # 8693b6a8 — claim-ownership gate. See the docstring above for the
        # full contract; short version: only block when we can actually
        # compare two non-empty identities and they disagree, and even then
        # only when the disagreement isn't explained by staleness/force.
        _claim_owner = (item.get("actor") or "").strip()
        _completing_actor = (actor or "").strip()
        if _claim_owner and _completing_actor and _claim_owner != _completing_actor:
            _claim_is_stale = False
            _claimed_at_dt = _parse_deferral_ts(item.get("claimed_at"))
            if _claimed_at_dt is not None:
                from datetime import datetime as _dt_cls  # noqa: PLC0415
                _age_hours = (
                    _dt_cls.utcnow() - _claimed_at_dt
                ).total_seconds() / 3600
                if _age_hours > _CLAIM_OWNERSHIP_STALE_HOURS:
                    _claim_is_stale = True
            if not _claim_is_stale:
                # Second staleness path: the claiming session itself is dead
                # even though claimed_at hasn't crossed the time threshold —
                # not found at all, explicitly closed/archived, or its own
                # heartbeat (last_seen) has gone cold. Mirrors db/locks.py's
                # heartbeat-expiry check for file/symbol claims. Best-effort:
                # an actor string that isn't a known session id (e.g. a human
                # name) yields no row here and is treated as "can't tell", not
                # as proof of death.
                try:
                    async with db.execute(
                        "SELECT status, last_seen FROM sessions WHERE id = ?",
                        (_claim_owner,),
                    ) as _owner_cur:
                        _owner_row = await _owner_cur.fetchone()
                    _owner_sess = _row_to_dict(_owner_row)
                except Exception:  # noqa: BLE001 — never wedge completion on a DB hiccup
                    _owner_sess = None
                if _owner_sess is not None:
                    if (_owner_sess.get("status") or "") in ("closed", "archived"):
                        _claim_is_stale = True
                    else:
                        _owner_last_seen = _parse_deferral_ts(_owner_sess.get("last_seen"))
                        if _owner_last_seen is not None:
                            from datetime import datetime as _dt_cls  # noqa: PLC0415
                            _seen_age_hours = (
                                _dt_cls.utcnow() - _owner_last_seen
                            ).total_seconds() / 3600
                            if _seen_age_hours > _CLAIM_OWNERSHIP_STALE_HOURS:
                                _claim_is_stale = True
            if not _claim_is_stale and not force_foreign_claim:
                raise SprintItemClaimMismatch(
                    f"item {item_id} is claimed by actor {_claim_owner!r}, not "
                    f"{_completing_actor!r} — refusing to complete a live claim "
                    "held by a different session. If the claiming session is "
                    "dead/abandoned but the claim hasn't crossed the "
                    f"{_CLAIM_OWNERSHIP_STALE_HOURS}h staleness threshold yet, "
                    "pass force_foreign_claim=true to acknowledge and complete "
                    "anyway."
                )
        _mark_phase("ownership_check")
        if item.get("require_verification"):
            if verifier_session_id and verification_verdict:
                await record_sprint_item_verification(
                    db, project_id, item_id, verifier_session_id,
                    verification_verdict, notes=verification_notes,
                )
            verification = await get_latest_sprint_item_verification(db, project_id, item_id)
            _completing_actor = (actor or "").strip()
            if verification is None:
                raise SprintItemVerificationRequired(
                    f"item {item_id} requires an independent fresh-session "
                    "verification before it can be completed (require_verification "
                    "is set) — no verification is on file yet. Spin up a fresh, "
                    "no-memory subsession with read-only tools to independently "
                    "inspect the change, then retry complete_sprint_item passing "
                    "verifier_session_id=<that session's own id> and "
                    "verification_verdict='pass' (or 'fail')."
                )
            if verification.get("verdict") != "pass":
                _vnote = verification.get("notes")
                raise SprintItemVerificationRequired(
                    f"item {item_id}'s latest fresh verification is FAIL "
                    f"(filed by session {verification.get('verifier_session_id')!r}"
                    f"{': ' + _vnote if _vnote else ''}). Address the issue the "
                    "verifier found and obtain a fresh independent PASS before "
                    "completing."
                )
            if not _completing_actor:
                raise SprintItemVerificationRequired(
                    f"item {item_id} requires an independent fresh-session "
                    "verification, but complete_sprint_item was not given an "
                    "actor= identity — cannot confirm the on-file PASS was filed "
                    "by a session other than the one completing it. Pass actor=."
                )
            if verification.get("verifier_session_id") == _completing_actor:
                raise SprintItemVerificationRequired(
                    f"item {item_id}'s only PASS verification was filed by the "
                    f"same session ({_completing_actor!r}) that is completing it "
                    "— that is not independent. A fresh, separate subsession "
                    "(different session_id, no memory of the implementation) "
                    "must file the PASS verdict."
                )
        _mark_phase("verification_check")
        if item.get("required_notes"):
            has_evidence = bool(
                (notes or "").strip()
                or task_id
                or (item.get("notes") or "").strip()
                or (item.get("task_id"))
            )
            if not has_evidence:
                raise SprintItemEvidenceRequired(
                    f"item {item_id} requires evidence before completion — pass "
                    "notes=... (what shipped / how it was verified) or link a "
                    "task_id. This item was flagged required_notes."
                )
            # fd2800ae — evidence exists; now check its quality.  Combine the
            # caller-supplied notes with any notes already stored on the item so
            # the heuristic sees the full evidence picture.  A linked task_id is
            # treated as substantive evidence (the task log is the mechanism
            # description) and skips the text heuristic entirely.
            if not task_id and not item.get("task_id"):
                _combined_evidence = " ".join(filter(None, [
                    (notes or "").strip(),
                    (item.get("notes") or "").strip(),
                ]))
                try:
                    _evidence_quality_warning = _check_evidence_quality(_combined_evidence)
                except Exception:  # noqa: BLE001 — heuristic must never block completion
                    _evidence_quality_warning = None
        # 1ec33edf — mechanical stored-evidence check: runs for ALL completions
        # (not just required_notes), because a declared touches_resources or
        # linked task_id that does not actually exist is a thin-evidence signal
        # regardless of whether the item was gated.
        try:
            _stored_evidence_warning = await _check_stored_evidence(
                db, item, task_id, notes
            )
        except Exception:  # noqa: BLE001 — never block completion
            _stored_evidence_warning = None
    _mark_phase("evidence_check")
    _completion_outcome: str | None = None
    try:
        result = await _update_sprint_item_status(
            db, project_id, item_id, "done", task_id=task_id, notes=notes, actor=actor,
            expected_statuses=_ACTIVE_SPRINT_STATUSES,
        )
    finally:
        _mark_phase("status_transition")
    if result is not None:
        # a2a027cf — the core status write above has ALREADY committed (see
        # _transition_status: it calls db.commit() itself, synchronously,
        # before returning). Everything from here down is advisory
        # derived-state maintenance, not part of "did the item complete" —
        # so it runs under a bounded budget and can never turn an
        # already-successful commit into a hung or misleading response.
        _completion_outcome = "committed"
        # 7d71d6bc — RESCUE-R2: best-effort wave-run child terminal-outcome
        # bookkeeping, INCLUDING the real subprocess exit code (see the
        # docstring's exit_code paragraph). Only on a genuine fresh commit —
        # never on the idempotent already_committed replay above, matching
        # this function's own "no duplicate side effects on retry"
        # discipline. Lazy import + fully swallowed: must never turn an
        # already-successful completion into a failure.
        try:
            from meridian.db import wave_runs as _wave_runs_module  # noqa: PLC0415
            _wr_child = await _wave_runs_module.find_active_wave_run_child_for_item(
                db, project_id, item_id,
            )
            if _wr_child is not None:
                await _wave_runs_module.record_wave_run_child_outcome(
                    db, _wr_child["wave_run_id"], item_id,
                    status="succeeded", exit_code=exit_code,
                    actor=actor, agent_id=actor,
                )
        except Exception:  # noqa: BLE001 — wave-run bookkeeping must never wedge completion
            pass
        _advisory_deferred = False
        try:
            await asyncio.wait_for(
                _run_post_commit_side_effects(db, project_id, item_id),
                timeout=_ADVISORY_PHASE_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            _advisory_deferred = True
        except Exception:  # noqa: BLE001 — advisory only, never block completion
            pass
        _mark_phase("post_commit_advisory")
        if _evidence_quality_warning or _stored_evidence_warning:
            result = dict(result)
            if _evidence_quality_warning:
                result["evidence_quality_warning"] = _evidence_quality_warning
            if _stored_evidence_warning:
                result["stored_evidence_warning"] = _stored_evidence_warning
        # ecc8b280 — machine-readable continuation_required/terminal_ready
        # state, scoped to this item's own version bucket, so a caller that
        # only calls complete_sprint_item (never get_sprint_progress) still
        # gets a structured signal about whether autonomous work remains
        # instead of having to infer it from prose. Advisory only — never
        # blocks completion, fails open on any error (INCLUDING a timeout),
        # same shape as the two warning fields above.
        try:
            _cg_version = result.get("version")
            _cg_sibling_items, _cg_project = await asyncio.wait_for(
                _gather_continuation_inputs(db, project_id, version=_cg_version),
                timeout=_ADVISORY_PHASE_TIMEOUT_S,
            )
            result = dict(result)
            result["continuation"] = _continuation_gate.compute_continuation_state(
                _cg_sibling_items,
                execution_mode=(_cg_project or {}).get("execution_mode"),
            )
        except asyncio.TimeoutError:
            _advisory_deferred = True
        except Exception:  # noqa: BLE001 — advisory only, never block completion
            pass
        _mark_phase("continuation_state")
        result = dict(result)
        if _advisory_deferred:
            result["advisory_work_deferred"] = True
            # 394bcbdf — resource-aware diagnostic: best-effort self-sample
            # of THIS server process's own memory/CPU footprint so a
            # deferred advisory phase can be explained ("the process itself
            # is over its configured budget right now, that's why the
            # rollup/chain-advance step didn't finish in time") instead of
            # leaving the caller to guess whether a retry is likely to help.
            # Never raises and never adds meaningful latency (sampling is
            # itself best-effort and near-instant); only present when
            # advisory work was actually deferred, keeping the common-case
            # payload unchanged.
            try:
                from .. import process_budget as _process_budget_mod  # noqa: PLC0415
                _budget_report = _process_budget_mod.sample_server_process()
                result["resource_diagnostics"] = {
                    "action": _budget_report.action,
                    "reason": _budget_report.reason,
                    "retry_after_seconds": _process_budget_mod.retry_after_seconds_for_report(
                        _budget_report
                    ),
                }
            except Exception:  # noqa: BLE001 — diagnostics are best-effort only
                pass
        result["completion_outcome"] = _completion_outcome
        result["correlation_id"] = _correlation_id
        result["phase_timings_ms"] = dict(_phase_ms)
    return result


async def _run_post_commit_side_effects(
    db: aiosqlite.Connection, project_id: str, item_id: str,
) -> None:
    """a2a027cf — the two purely-advisory post-commit maintenance steps
    (parent rollup + mixed-ownership task-chain advance) that
    :func:`complete_sprint_item` used to run unbounded. Split out so both can
    be awaited under a single ``asyncio.wait_for`` budget without duplicating
    call sites. Neither step is part of the authoritative status write — the
    caller's own ``_update_sprint_item_status`` call has already committed
    ``status='done'`` before this function is ever invoked."""
    await _maybe_rollup_parent(db, project_id, item_id)
    await _advance_task_chain(db, project_id, item_id)


async def _gather_continuation_inputs(
    db: aiosqlite.Connection, project_id: str, version: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """a2a027cf — the two async fetches continuation-state computation needs,
    split out so they can be awaited under a single bounded
    ``asyncio.wait_for`` call in :func:`complete_sprint_item`.

    f291bb24 — ``version`` (the just-completed item's own version bucket) is
    now pushed into the sibling-item query itself via
    get_sprint_items_continuation_scoped, instead of fetching every item in
    the project (through a cache that's a guaranteed miss for this caller)
    and filtering to one version bucket in Python afterward.
    """
    _sibling_items = await get_sprint_items_continuation_scoped(db, project_id, version)
    _project = await get_project(db, project_id)
    return _sibling_items, _project


async def provisional_complete_sprint_item(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    task_id: str | None = None,
) -> dict[str, Any] | None:
    """Mark a sprint item ``provisional_complete`` — work finished but not yet
    verified/deployed.

    A non-terminal state between in_progress and done: it does not stamp
    ``completed_at``, does not count toward percent_complete, and keeps any
    parent item active (no roll-up). The executor flips it to ``done`` via
    complete_sprint_item once the change is verified/shipped.
    """
    return await _update_sprint_item_status(
        db, project_id, item_id, "provisional_complete", task_id=task_id,
        expected_statuses=_ACTIVE_SPRINT_STATUSES,
    )


async def skip_sprint_item(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    reason: str | None = None,
) -> dict[str, Any] | None:
    """Mark a sprint item ``skipped`` (intentionally not shipped)."""
    result = await _update_sprint_item_status(
        db, project_id, item_id, "skipped", notes=reason,
        expected_statuses=_ACTIVE_SPRINT_STATUSES,
    )
    if result is not None:
        await _maybe_rollup_parent(db, project_id, item_id)
    return result


async def start_sprint_item(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
) -> dict[str, Any] | None:
    """Flip a sprint item from ``pending``/``todo`` to ``in_progress``."""
    return await _update_sprint_item_status(
        db, project_id, item_id, "in_progress",
        expected_statuses={"pending", "todo"},
    )


def _parse_deferral_ts(value: Any) -> "datetime | None":  # noqa: F821
    """Parse a ``deferred_until`` value into a naive-UTC ``datetime``.

    dec69708 — accepts a ``datetime`` (returned as naive UTC) OR a string in
    either the DB's space-separated ``YYYY-MM-DD HH:MM:SS`` form or ISO-8601
    (``T`` separator, optional trailing ``Z`` / offset / fractional seconds).
    Returns ``None`` when the value is empty or unparseable, so a malformed
    stored deferral never hard-blocks a claim (fail-open on garbage).
    """
    from datetime import datetime, timezone

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if not s:
            return None
        s = s.replace("Z", "+00:00")
        dt = None
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(s, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            return None
    # Normalise to naive UTC so comparison against a naive utcnow() is sound.
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _is_deferred(item: dict[str, Any]) -> bool:
    """45f519a0 — return True when a sprint item's ``deferred_until`` is in the future.

    Uses the same ``_parse_deferral_ts`` parser that ``claim_sprint_item``
    uses, so the semantics are identical: fail-open on garbage (unparseable
    values are treated as not deferred), and timezone-aware values are
    normalised to naive UTC before comparison.

    Callers that want to exclude backburnered items from a list should filter
    with ``[it for it in items if not _is_deferred(it)]``.
    """
    from datetime import datetime as _dt_cls

    raw = item.get("deferred_until")
    if not raw:
        return False
    dt = _parse_deferral_ts(raw)
    if dt is None:
        return False
    return dt > _dt_cls.utcnow()


async def claim_sprint_item(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    actor: str | None = None,
) -> dict[str, Any] | None:
    """Claim a sprint item: set status='in_progress' and claimed_at=now().

    Rejects (raises ValueError) if already in_progress, done, failed, or skipped.
    Returns None if the item doesn't exist. ``actor`` (5823db0b) records which
    executor claimed the item.

    dec69708 — ENFORCED deferral: if the item's ``deferred_until`` is in the
    future, the claim is REFUSED and a structured blocked dict is returned
    (``{"blocked": True, "reason": ..., "deferred_until": ...}``) rather than
    flipping the item to in_progress. This turns a "we decided to defer this"
    intent into a real, structural guard nothing can bypass by simply claiming
    the item anyway.

    94c26322 — PROSPECTING GATE: if the item DECLARES real code-touching
    resources (``touches_resources`` is non-empty) but has no durable
    evidence (``sprint_item_pointers`` is empty) AND no human-set
    ``prospect_bypass``, the claim is REFUSED with a structured blocked dict
    (``{"blocked": True, "error": "UNPROSPECTED", ...}``). This mirrors the
    goal-generation gate's "only block confirmed enrichment failures, not
    never-attempted items" semantics: an item with NO declared
    touches_resources was never a prospecting candidate in the first place
    (nothing for add_sprint_item's inline prospecting to attempt), so it is
    NOT gated here — mirroring items without a claimable-batch concern at all
    (manual tasks, proposals, docs-only work). The ONLY bypass for a
    resource-declaring item is a human explicitly setting
    ``prospect_bypass=True`` via update_sprint_item.
    Fail-open: any DB error lets the claim proceed so a structural defect
    never permanently wedges the board.

    f89d440f — SUPERSEDED GATE: if ``blocker_kind == 'superseded'``, the claim
    is REFUSED with a structured blocked dict (``{"blocked": True, "error":
    "SUPERSEDED", ...}``), same as the DEFERRED gate below. Clear
    ``blocker_kind`` via ``update_sprint_item`` to make the item claimable
    again once a human has resolved whatever superseded it. Unlike the
    'manual' blocker_kind (a soft, listing-only exclusion — see
    ``_VALID_SPRINT_BLOCKER_KINDS``), this is a hard gate on the claim itself,
    because a superseded item's id can still reach an executor directly (a
    stale goal block, prior session memory) even when it never appears in a
    fresh listing.

    4d1fb28f — ``required_tool`` (when set via ``update_sprint_item`` /
    ``add_sprint_item``) is NOT a claim-time gate — it's advisory guidance,
    not an enforced block, because Meridian cannot verify which tool an
    executor actually invokes. It flows through on the returned dict (this
    function's result is a full ``get_sprint_item`` row) so a caller that
    claims directly (bypassing /goal's rendered ``<required_tool>``
    directive) still sees the pin and can honour it.

    56e9b3c7 — AUTONOMOUS STALE-CLAIM RECONCILIATION: when the item is
    already ``in_progress``, this function no longer just raises. It first
    classifies the existing claim via :func:`classify_stale_claim` — a
    multi-signal check (claiming session's heartbeat/closed-state, worktree
    activity, task-log evidence since ``claimed_at`` — NOT ``claimed_at`` age
    alone) — and, ONLY when the verdict is ``"stale"``, atomically releases
    the abandoned claim's item/resource locks, resets it to ``pending``, and
    writes an audit record (:func:`_reset_stale_claim`) before retrying the
    claim fresh. An ``"active"`` or ``"ambiguous"`` verdict changes nothing —
    the ``ValueError`` below still fires exactly as before. See
    :func:`reconcile_stale_claims` for the project/version-scoped bulk sweep
    (dry-run + bounded-batch) built on the same classifier.
    """
    item = await get_sprint_item(db, item_id)
    if item is None:
        return None
    if item.get("project_id") != project_id:
        return None
    # dec69708 — refuse a future-deferred item. Compare a parsed deferred_until
    # (datetime OR str) against now(); fail-open if unparseable so a garbage
    # value never wedges the board.
    _deferred_raw = item.get("deferred_until")
    if _deferred_raw:
        from datetime import datetime as _dt_cls
        _deferred_dt = _parse_deferral_ts(_deferred_raw)
        if _deferred_dt is not None and _deferred_dt > _dt_cls.utcnow():
            return {
                "blocked": True,
                "error": "DEFERRED",
                "reason": (
                    f"Sprint item is deferred until {_deferred_raw} — it cannot be "
                    "claimed before then. Clear deferred_until via update_sprint_item "
                    "to make it claimable now."
                ),
                "deferred_until": _deferred_raw,
                "track": item.get("track"),
                "item_id": item_id,
            }
    # f89d440f — refuse a superseded item. Hard gate (unlike 'manual', which is
    # only a listing-level exclusion) because a stale goal block or prior
    # session memory can hand an executor this item_id directly, bypassing any
    # listing filter entirely — exactly the recurrence this gate closes.
    if (item.get("blocker_kind") or "").strip() == "superseded":
        _notes = (item.get("notes") or "").strip()
        return {
            "blocked": True,
            "error": "SUPERSEDED",
            "reason": (
                "Sprint item is marked blocker_kind='superseded' — its premise "
                "has been replaced by other work and it must not be executed "
                "as-is. See the item's notes for what superseded it. A human "
                "must clear blocker_kind via update_sprint_item to make it "
                "claimable again."
                + (f" Notes: {_notes}" if _notes else "")
            ),
            "item_id": item_id,
        }
    # cc3864bd — refuse an item blocked by a wave run's systemic invalidation.
    # Hard gate, same enforcement point as 'superseded' above (a stale goal
    # block or prior session memory can hand an executor this item_id
    # directly, bypassing any listing filter) — see
    # meridian.db.wave_runs.abort_wave_run_systemic /
    # block_sprint_items_for_systemic_invalidation for how this gets set.
    if (item.get("blocker_kind") or "").strip() == "systemic_invalidated_run":
        _notes = (item.get("notes") or "").strip()
        return {
            "blocked": True,
            "error": "SYSTEMIC_INVALIDATED_RUN",
            "reason": (
                "Sprint item is marked blocker_kind='systemic_invalidated_run' "
                "— the wave run it belonged to was aborted because its "
                "foundational hypothesis was systemically invalidated (see "
                "the item's notes, and the wave run's executor_reports entry, "
                "for the evidence). It must not be executed until a planner "
                "reviews the evidence and clears blocker_kind via "
                "update_sprint_item on a corrected board revision."
                + (f" Notes: {_notes}" if _notes else "")
            ),
            "item_id": item_id,
        }
    # 74a8f420 — WAVE GATE STRUCTURAL ENFORCEMENT: an item whose wave sits
    # beyond a configured-but-not-yet-passed wave gate boundary cannot be
    # claimed. This is what turns wave gates (deterministic action pipelines —
    # push_dev/push_main/deploy/wait/run_verification — attached to a wave or
    # wave-range via configure_wave_gate) from advisory /goal prose into an
    # actual claim-time block, the same class of fix as the DEFERRED/
    # SUPERSEDED/UNPROSPECTED gates above. Fail-open: any DB error (e.g. the
    # wave_gate_configs table not yet migrated) lets the claim proceed so a
    # structural defect never permanently wedges the board.
    try:
        # ed8e4524 — pass the item's own sprint-version bucket so a
        # version-scoped gate config only blocks/unblocks items in that SAME
        # version; an unscoped (project-wide) config still applies to every
        # item regardless of its version, exactly as before this fix.
        _blocking_gate = await _get_blocking_wave_gate(
            db, project_id, item.get("wave"), version=item.get("version")
        )
    except Exception:  # noqa: BLE001 — gate must never wedge the board
        _blocking_gate = None
    if _blocking_gate is not None:
        _gate_end = _blocking_gate.get("wave_end")
        return {
            "blocked": True,
            "error": "WAVE_GATE_PENDING",
            "reason": (
                f"Sprint item is in wave {item.get('wave')!r}, which sits beyond "
                f"wave {_gate_end!r}'s configured gate pipeline "
                f"({_blocking_gate.get('actions')!r}). That gate has not completed "
                "yet — run its action pipeline (push_dev/push_main/deploy/wait/"
                "run_verification as configured) then call complete_wave_gate("
                f"project_id=..., wave_label={_gate_end!r}, verification_payload="
                "<real run_verification result>) before this item can be claimed."
            ),
            "wave": item.get("wave"),
            "gate_wave_start": _blocking_gate.get("wave_start"),
            "gate_wave_end": _gate_end,
            "gate_actions": _blocking_gate.get("actions"),
            "item_id": item_id,
        }
    # 94c26322/d5849a67 — refuse an unprospected item at claim time unless a
    # human explicitly set prospect_bypass. At claim time, enrichment-time
    # fields (code_pointers / prospect_status) are NOT on the DB row, so we
    # check the durable sprint_item_pointers table instead (persistently
    # pinned pointers). An item is considered evidenced if it has >= 1 row in
    # sprint_item_pointers.
    #
    # d5849a67 — the final pass/fail decision below runs through
    # is_item_claim_prospected(), the SAME shared helper generate_handoff's
    # excluded_unprospected list uses (with a batch-resolved evidence signal —
    # see get_pointer_evidence_item_ids). This is what makes the two checks
    # agree: previously handoff's exclusion never looked at touches_resources
    # or the durable pointers table at all, so an item could sit outside
    # <excluded_unprospected> yet still be refused here.
    #
    # SCOPE GUARD (is_item_claim_prospected / _item_declares_resources): only
    # gated when the item actually declared touches_resources (i.e. was a real
    # prospecting candidate at add-time). An item with no declared resources
    # was never attempted — nothing for _persist_prospected_pointer to
    # prospect — and gating it here would block the overwhelming majority of
    # ordinary items (manual tasks, proposals, anything filed without explicit
    # file/route/tool targets), not just genuinely-risky unprospected ones.
    #
    # Fail-open: any DB error leaves has_pointer_evidence=True so the claim
    # proceeds — a structural defect never permanently wedges the board.
    _has_pointer_evidence = True
    if _item_declares_resources(item) and not bool(item.get("prospect_bypass")):
        try:
            async with db.execute(
                "SELECT COUNT(*) AS cnt FROM sprint_item_pointers WHERE sprint_item_id = ?",
                (item_id,),
            ) as _ptr_cur:
                _ptr_row = await _ptr_cur.fetchone()
            _ptr_count = (
                (_ptr_row[0] if not isinstance(_ptr_row, dict) else _ptr_row.get("cnt"))
                if _ptr_row else 0
            ) or 0
            _has_pointer_evidence = bool(_ptr_count)
        except Exception:  # noqa: BLE001 — gate must never wedge the board
            _has_pointer_evidence = True
    if not is_item_claim_prospected(item, has_pointer_evidence=_has_pointer_evidence):
        return {
            "blocked": True,
            "error": "UNPROSPECTED",
            "reason": (
                "Sprint item has no durable pointers (sprint_item_pointers is "
                "empty). It cannot be claimed without explicit human approval. "
                "A human/planning session must either add a pointer via "
                "add_sprint_item_pointer() or call "
                "update_sprint_item(item_id=..., prospect_bypass=true) to "
                "explicitly allow this item. Executors must NOT set "
                "prospect_bypass themselves."
            ),
            "item_id": item_id,
        }
    blocked = {"in_progress", "done", "failed", "skipped"}
    if (item.get("status") or "pending") in blocked:
        # 56e9b3c7 — AUTONOMOUS STALE-CLAIM RECONCILIATION: previously this
        # branch just raised ValueError for an in_progress item, and the ONLY
        # feedback a caller got was the MCP handler's own reactive, age-only
        # (claimed_at > 2h) STALE_CLAIM report AFTER the raise — nothing ever
        # actually reconciled the claim; the response merely suggested
        # update_sprint_item(status='pending', force=true), a schema that
        # doesn't exist. Before giving up, run the full multi-signal
        # classification (heartbeat/session-liveness, worktree activity,
        # task-log evidence, explicit close — NOT claimed_at age alone) via
        # classify_stale_claim(). Only a "stale" verdict is auto-reconciled;
        # "active" and "ambiguous" verdicts fall through to the unchanged
        # raise below — a claim is NEVER reset on a hunch or on age alone.
        if (item.get("status") or "") == "in_progress":
            try:
                _reconcile_verdict = await classify_stale_claim(db, item)
            except Exception:  # noqa: BLE001 — classification must never wedge a claim attempt
                _reconcile_verdict = {"classification": "ambiguous"}
            if _reconcile_verdict.get("classification") == "stale":
                _reconciled = await _reset_stale_claim(
                    db, project_id, item_id, _reconcile_verdict, actor=actor,
                )
                if _reconciled is not None:
                    # The stale claim was atomically released back to pending
                    # (locks released, stall_count bumped, audit record
                    # written by _reset_stale_claim) — re-fetch and fall
                    # through to the normal claim path below as if this
                    # caller had found the item pending in the first place.
                    item = await get_sprint_item(db, item_id)
        if (item.get("status") or "pending") in blocked:
            raise ValueError(
                f"cannot claim item with status '{item.get('status')}'"
            )
    # fa3e3331 — the pre-check above (read, then this UPDATE) is a classic
    # TOCTOU race: two concurrent claims can both pass the pre-check before
    # either commits. Routing through _transition_status with from_statuses
    # set to the claimable statuses makes the actual write atomic — only one
    # concurrent caller's UPDATE can match — so at most one claim ever
    # succeeds even when both callers observed a claimable status.
    # actor is handled via COALESCE so an existing actor is never cleared.
    # claimed_at_now=True sets claimed_at = datetime('now').
    # Note: actor COALESCE logic: we pass actor through the normal actor= field
    # (sets actor = ?); the old raw UPDATE used COALESCE(?, actor) to preserve
    # an existing actor. _transition_status sets actor unconditionally when
    # provided. For claim, we only pass actor when it is not None, matching
    # the COALESCE(actor_arg, existing_actor) semantics — if actor is None,
    # _transition_status leaves the actor column untouched (no "actor = ?" field
    # is added), so any existing actor value is preserved naturally.
    claimable = {"pending", "todo", "indeterminate", "provisional_complete"}
    result = await _transition_status(
        db, project_id, item_id, "in_progress",
        from_statuses=sorted(claimable),
        actor=actor,
        claimed_at_now=True,
    )
    if result is None:
        # The pre-check passed but a concurrent transition committed first
        # between that read and this UPDATE (or the item vanished/moved project).
        # Re-fetch and raise the SAME ValueError shape the pre-check above uses,
        # so this race-lost case is indistinguishable to callers (including the
        # MCP handler's existing except-ValueError handling) from a claim that
        # was rejected up front — no separate handling needed for the race case.
        _raced = await get_sprint_item(db, item_id)
        if _raced is None or _raced.get("project_id") != project_id:
            return None
        raise ValueError(
            f"cannot claim item with status '{_raced.get('status')}'"
        )
    # 7d71d6bc — RESCUE-R2: best-effort wave-run child-lease bookkeeping.
    # If this item is a live child of an ACTIVE (non-terminal) wave run,
    # stamp the claim-before-work timestamp + agent identity onto its
    # wave_run_children row (claim_wave_run_child itself decides
    # first_claim/reclaim/retry — see meridian.db.wave_runs). Lazy import to
    # avoid any module-load-order coupling between sprint_items.py and
    # wave_runs.py (both are imported at the bottom of db/__init__.py).
    # Never lets wave-run bookkeeping block or fail a claim that has ALREADY
    # committed above — a ForeignWaveRunChildLeaseError from a still-live
    # sibling agent, a missing wave_run_children table on an old mid-
    # migration DB, etc. are all swallowed here. A project that never calls
    # start_wave_run sees zero behavior change:
    # find_active_wave_run_child_for_item returns None immediately.
    if actor:
        try:
            from meridian.db import wave_runs as _wave_runs_module  # noqa: PLC0415
            _wr_child = await _wave_runs_module.find_active_wave_run_child_for_item(
                db, project_id, item_id,
            )
            if _wr_child is not None:
                await _wave_runs_module.claim_wave_run_child(
                    db, _wr_child["wave_run_id"], item_id, agent_id=actor, actor=actor,
                )
        except Exception:  # noqa: BLE001 — wave-run bookkeeping must never wedge a claim
            pass
    return result


# ---------------------------------------------------------------------------
# 56e9b3c7 — autonomous stale-claim reconciliation.
#
# THE DEFECT: claim_sprint_item only ever REPORTED a stale claim reactively —
# after a second claim attempt raised ValueError, the MCP handler
# (meridian/mcp/handlers/sprint_tools.py, 10c0f6a0) checked claimed_at age
# alone (>2h) and, if stale, told the caller to self-service via
# ``update_sprint_item(status='pending', force=true)`` — a recovery contract
# the public update tool schema never actually exposed. There was no
# autonomous sweep, no session-heartbeat/liveness check, no worktree/process
# check, no evidence check, and no audit trail of who reset what or why.
#
# THE FIX: two entry points sharing ONE classifier (:func:`classify_stale_claim`)
# and ONE atomic reset (:func:`_reset_stale_claim`):
#   1. claim_sprint_item itself (see above) — autonomously reconciles the ONE
#      item a caller is actively trying to claim, inline, before raising.
#   2. :func:`reconcile_stale_claims` — a project/version-scoped BULK sweep
#      with dry-run and bounded-batch modes, for a scheduler path or an
#      explicit human/planner-triggered audit across an entire board.
#
# CLASSIFICATION IS DELIBERATELY CONSERVATIVE. Per the acceptance criteria:
# "never reset an active or ambiguous claim" and "require multiple signals
# ... when evidence is ambiguous". Concretely:
#   * A claiming session with a LIVE heartbeat (found, not closed/archived,
#     last_seen within the staleness window) is ALWAYS "active", no matter
#     how old claimed_at is — a 70-hour-old claim under a session that is
#     still heartbeating is genuine long-running work, not abandonment.
#   * No actor recorded at all -> "ambiguous" (nothing to verify liveness
#     against — resetting blind is the one thing this module must never do).
#   * The claiming session explicitly closed/archived -> "stale" outright,
#     unconditionally — this mirrors the ALREADY-SHIPPED, accepted precedent
#     in complete_sprint_item's own claim-ownership gate (8693b6a8): an
#     explicit close is a strong, non-time-based signal, not a guess.
#   * Anything short of that (session simply not found — e.g. a human-named
#     actor string, per the SAME "can't tell != proof of death" precedent
#     8693b6a8 already established — or a session whose heartbeat has merely
#     gone cold without an explicit close) requires claimed_at age STALE
#     *plus* at least one corroborating signal (no live worktree activity, no
#     task-log evidence since the claim, or — for the fully-unrecognised-actor
#     case — two independent corroborators) before landing on "stale". A
#     single signal alone (age, or a cold-but-not-explicitly-closed session)
#     is "ambiguous", never "stale" — the multi-signal requirement the
#     acceptance criteria calls for.
#   * When ``repo_root`` is supplied (self-hosted only — mirrors
#     sprint_evidence_guard's own self-hosted-only gate), a claim that already
#     has real, fresh, matching completion evidence on file
#     (:func:`meridian.sprint_evidence_guard.verify_strict_completion_evidence`
#     returns ``ok=True``) is downgraded from "stale" to "ambiguous" — this
#     looks like completed work that was never marked done, not an abandoned
#     claim, and resetting it to pending would silently discard real work.
# ---------------------------------------------------------------------------

# Reuse the SAME 2h number claim_sprint_item's reactive report and
# complete_sprint_item's claim-ownership gate already use, so "is this claim
# stale" answers identically everywhere in the codebase that asks.
_RECONCILE_STALE_HOURS = _CLAIM_OWNERSHIP_STALE_HOURS

# Bulk-sweep batch bounds (reconcile_stale_claims). Bounded so a huge board
# can never turn one sweep call into an unbounded scan/lock storm.
_RECONCILE_DEFAULT_BATCH = 25
_RECONCILE_MAX_BATCH = 200

# event_type recorded in action_audit_log for every autonomous/bulk reset —
# same append-only audit table sprint_evidence_guard's override path uses.
RECONCILE_STALE_CLAIM_AUDIT_EVENT = "sprint_item_stale_claim_reconciled"

# Classification verdicts returned by classify_stale_claim().
RECONCILE_ACTIVE = "active"
RECONCILE_STALE = "stale"
RECONCILE_AMBIGUOUS = "ambiguous"
RECONCILE_NOT_APPLICABLE = "not_applicable"


async def _claim_session_liveness(
    db: aiosqlite.Connection, actor: str
) -> dict[str, Any]:
    """Resolve a claim's ``actor`` against the sessions table.

    Mirrors (and is intentionally consistent with) complete_sprint_item's
    own claim-ownership staleness check (8693b6a8): a session row that
    doesn't exist at all is "can't tell", NOT proof of death — many actor
    strings are human names or foreign identifiers with no session row.
    """
    async with db.execute(
        "SELECT status, last_seen FROM sessions WHERE id = ?", (actor,)
    ) as cur:
        row = await cur.fetchone()
    sess = _row_to_dict(row)
    if sess is None:
        return {
            "found": False, "status": None, "last_seen": None,
            "closed_or_archived": False, "heartbeat_cold": None,
            "verified_alive": False,
        }
    status = sess.get("status") or ""
    closed_or_archived = status in ("closed", "archived")
    last_seen_dt = _parse_deferral_ts(sess.get("last_seen"))
    heartbeat_cold: bool | None = None
    if last_seen_dt is not None:
        from datetime import datetime as _dt_cls  # noqa: PLC0415
        age_h = (_dt_cls.utcnow() - last_seen_dt).total_seconds() / 3600
        heartbeat_cold = age_h > _RECONCILE_STALE_HOURS
    verified_alive = (not closed_or_archived) and (heartbeat_cold is False)
    return {
        "found": True, "status": status, "last_seen": sess.get("last_seen"),
        "closed_or_archived": closed_or_archived,
        "heartbeat_cold": heartbeat_cold,
        "verified_alive": verified_alive,
    }


async def _claim_worktree_activity(
    db: aiosqlite.Connection,
    actor: str,
    item_id: str,
    *,
    repo_root: "Any | None" = None,
) -> bool | None:
    """Worktree-activity signal for one (actor, item) claim.

    Returns ``True`` when a live, registered worktree for this exact
    (session, item) pair exists — real evidence of ongoing work. Returns
    ``False`` when a worktree WAS registered for this pair but is no longer
    live (removed, or — self-hosted only, when ``repo_root`` is supplied —
    its recorded owner ``pid`` is confirmed dead via the same liveness check
    ``worktree_cleanup`` itself uses before a real disk removal). Returns
    ``None`` (unknown, votes neither way) when no worktree was ever
    registered for this pair at all — many legitimate executors work in a
    single tree and never call register_worktree.
    """
    async with db.execute(
        "SELECT * FROM active_worktrees WHERE session_id = ? AND item_id = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (actor, item_id),
    ) as cur:
        row = await cur.fetchone()
    wt = _row_to_dict(row)
    if wt is None:
        return None
    if wt.get("removed_at"):
        return False
    if repo_root is not None and wt.get("pid") is not None:
        try:
            from ..worktree_cleanup import _pid_is_alive  # noqa: PLC0415
            if not _pid_is_alive(int(wt["pid"])):
                return False
        except Exception:  # noqa: BLE001 — an unverifiable pid is not proof of death
            pass
    return True


async def _claim_recent_task_evidence(
    db: aiosqlite.Connection,
    actor: str | None,
    item_id: str,
    claimed_at_dt: "Any | None",
) -> bool | None:
    """True when a task_log row for this actor/item was logged at/since the claim.

    Returns ``None`` (unknown) when ``claimed_at_dt`` couldn't be parsed —
    there is nothing to compare "since" against. Uses ``>=`` rather than a
    strict ``>``: both timestamps are second-granularity TEXT columns, so a
    task logged in the SAME second as the claim (a common real sequence —
    claim, then immediately log progress) must still count as evidence, not
    be excluded by a same-second tie.
    """
    if claimed_at_dt is None:
        return None
    claimed_at_str = claimed_at_dt.strftime("%Y-%m-%d %H:%M:%S")
    params: list[Any] = [item_id, claimed_at_str]
    actor_clause = ""
    if actor:
        actor_clause = "OR session_id = ? "
        params.insert(1, actor)
    async with db.execute(
        "SELECT COUNT(*) AS cnt FROM task_log "
        f"WHERE (sprint_item_id = ? {actor_clause}) AND created_at >= ?",
        tuple(params),
    ) as cur:
        row = await cur.fetchone()
    cnt = (row["cnt"] if isinstance(row, dict) else row[0]) if row else 0
    return bool(cnt)


async def classify_stale_claim(
    db: aiosqlite.Connection,
    item: dict[str, Any],
    *,
    repo_root: "Any | None" = None,
    now: "Any | None" = None,
) -> dict[str, Any]:
    """Classify ONE sprint item's current claim as active/stale/ambiguous.

    See the module-level "56e9b3c7" comment block above claim_sprint_item's
    reconciliation hook for the full decision-tree rationale. Never raises
    for a normal classification outcome — an internal error degrades a
    specific signal to "unknown" rather than aborting, matching this
    module's established fail-open-on-uncertainty convention (never fail
    open toward RESETTING a claim, only toward classifying it "ambiguous").

    Returns ``{"item_id", "classification", "reasons": [str, ...],
    "signals": {...}}``. ``classification`` is one of "active", "stale",
    "ambiguous", or "not_applicable" (item isn't in_progress at all).
    """
    from datetime import datetime as _dt_cls  # noqa: PLC0415

    item_id = item.get("id")
    reasons: list[str] = []
    if (item.get("status") or "") != "in_progress":
        return {
            "item_id": item_id, "classification": RECONCILE_NOT_APPLICABLE,
            "reasons": ["item is not in_progress"], "signals": {},
        }

    now_dt = now or _dt_cls.utcnow()
    claimed_at_dt = _parse_deferral_ts(item.get("claimed_at"))
    age_hours: float | None = None
    age_stale = False
    if claimed_at_dt is not None:
        age_hours = (now_dt - claimed_at_dt).total_seconds() / 3600
        age_stale = age_hours > _RECONCILE_STALE_HOURS

    actor = (item.get("actor") or "").strip()
    signals: dict[str, Any] = {
        "actor": actor or None,
        "claimed_at": item.get("claimed_at"),
        "age_hours": round(age_hours, 2) if age_hours is not None else None,
        "age_stale": age_stale,
    }

    if not actor:
        signals.update({"session_found": None, "worktree_live": None, "recent_evidence": None})
        return {
            "item_id": item_id, "classification": RECONCILE_AMBIGUOUS,
            "reasons": ["no actor recorded on the claim — cannot verify session "
                        "liveness, so the claim is never auto-reset blind"],
            "signals": signals,
        }

    session = await _claim_session_liveness(db, actor)
    signals.update({
        "session_found": session["found"],
        "session_status": session["status"],
        "session_last_seen": session["last_seen"],
        "session_explicitly_closed": session["closed_or_archived"],
        "session_heartbeat_cold": session["heartbeat_cold"],
        "session_verified_alive": session["verified_alive"],
    })

    if session["verified_alive"]:
        reasons.append(
            f"claiming session {actor!r} has a live heartbeat "
            f"(status={session['status']!r}, last_seen={session['last_seen']!r})"
        )
        return {
            "item_id": item_id, "classification": RECONCILE_ACTIVE,
            "reasons": reasons, "signals": signals,
        }

    if session["closed_or_archived"]:
        reasons.append(
            f"claiming session {actor!r} is explicitly {session['status']!r} "
            "— mirrors complete_sprint_item's own claim-ownership precedent "
            "(8693b6a8) that an explicit close is unconditional proof of death"
        )
        return {
            "item_id": item_id, "classification": RECONCILE_STALE,
            "reasons": reasons, "signals": signals,
        }

    worktree_live = await _claim_worktree_activity(db, actor, item_id, repo_root=repo_root)
    recent_evidence = await _claim_recent_task_evidence(db, actor, item_id, claimed_at_dt)
    signals["worktree_live"] = worktree_live
    signals["recent_evidence"] = recent_evidence

    corroborators = 0
    if age_stale:
        corroborators += 1
        reasons.append(f"claimed_at is {signals['age_hours']}h old (> {_RECONCILE_STALE_HOURS}h)")
    if worktree_live is False:
        corroborators += 1
        reasons.append("no live registered worktree for this claim (removed or owning process dead)")
    if recent_evidence is False:
        corroborators += 1
        reasons.append("no task_log activity for this item/session since it was claimed")

    session_heartbeat_dead = bool(session["found"] and session["heartbeat_cold"])
    session_unknown = not session["found"]
    # A LIVE, registered worktree is a veto against auto-reset, not merely
    # "not a corroborator" — a quiet executor that never calls log_task but
    # still has an active worktree registered (optionally pid-confirmed
    # alive) is exactly the "legitimate long-running work" the acceptance
    # criteria says must be preserved, even with a cold heartbeat and zero
    # task_log rows. A live worktree can only ever push toward "ambiguous"
    # (never "active" outright — that still requires a verified heartbeat).
    worktree_veto = worktree_live is True

    classification = RECONCILE_AMBIGUOUS
    if session_heartbeat_dead and corroborators >= 1 and not worktree_veto:
        reasons.insert(0, f"claiming session {actor!r}'s heartbeat has gone cold")
        classification = RECONCILE_STALE
    elif session_unknown and age_stale and corroborators >= 2 and not worktree_veto:
        reasons.insert(0, f"no session row found for actor {actor!r} (unverifiable identity)")
        classification = RECONCILE_STALE
    elif corroborators == 0 and not worktree_veto and session["heartbeat_cold"] is not True:
        # Nothing suspicious at all — fresh-ish claim, no negative signals.
        classification = RECONCILE_ACTIVE
        reasons = ["no staleness signals fired"]
    elif worktree_veto and classification != RECONCILE_STALE:
        reasons.append("a live registered worktree vetoes auto-reset — needs human review")

    if classification == RECONCILE_STALE and repo_root is not None:
        try:
            from meridian.sprint_evidence_guard import verify_strict_completion_evidence  # noqa: PLC0415
            _evidence = await verify_strict_completion_evidence(
                db, repo_root, item.get("project_id"), item_id, item,
            )
            signals["strict_evidence_ok"] = bool(_evidence.get("ok"))
        except Exception:  # noqa: BLE001 — evidence check is advisory to classification
            signals["strict_evidence_ok"] = None
        if signals.get("strict_evidence_ok"):
            classification = RECONCILE_AMBIGUOUS
            reasons.append(
                "verify_strict_completion_evidence found real, fresh completion "
                "evidence on file — this looks like finished work that was never "
                "marked done, not an abandoned claim; resetting to pending would "
                "risk silently discarding it. A human should review and likely "
                "call complete_sprint_item instead."
            )

    return {"item_id": item_id, "classification": classification, "reasons": reasons, "signals": signals}


async def _reset_stale_claim(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    verdict: dict[str, Any],
    *,
    actor: str | None = None,
) -> dict[str, Any] | None:
    """Atomically reset ONE proven-stale claim: release its locks, return the
    item to pending, and write an audit record. NEVER call this directly on
    a verdict that isn't ``"stale"`` — this function does not itself re-check
    the classification, only the live status (TOCTOU guard).

    Returns ``None`` (silent no-op) if the item raced away from
    ``in_progress`` between classification and this write (a concurrent
    legitimate completion/claim/reconciliation beat this one to it) — never
    clobbers a concurrent winner, matching every other transition in this
    module's race-safety contract.
    """
    item = await get_sprint_item(db, item_id)
    if item is None or item.get("project_id") != project_id:
        return None
    prior_actor = item.get("actor")
    prior_claimed_at = item.get("claimed_at")

    # TOCTOU-safe: only transitions FROM in_progress. A race-lost attempt is
    # a silent no-op (mirrors requeue_or_fail_stalled_item / _transition_status).
    transitioned = await _transition_status(
        db, project_id, item_id, "pending",
        from_statuses=["in_progress"],
    )
    if transitioned is None:
        return None

    new_stall_count = int(item.get("stall_count") or 0) + 1
    await db.execute(
        "UPDATE sprint_items SET claimed_at = NULL, stall_count = ? "
        "WHERE id = ? AND project_id = ?",
        (new_stall_count, item_id, project_id),
    )
    await db.commit()

    # Release item/resource locks the abandoned claim held. Best-effort:
    # release_file/release_resource/release_symbol live in db/locks.py, which
    # is imported back onto the meridian.db package AFTER sprint_items.py —
    # lazy import here (called well after full package init) avoids the
    # circular-import ordering issue, same pattern _check_wrong_worktree in
    # sprint_evidence_guard.py already uses for a cross-submodule call.
    released: list[str] = []
    if prior_actor:
        try:
            from meridian.db import release_file, release_resource, release_symbol  # noqa: PLC0415
            for rid in parse_touches_resources(item.get("touches_resources")):
                body = rid[len("inferred:"):] if rid.lower().startswith("inferred:") else rid
                try:
                    if body.startswith("file:"):
                        path = body[len("file:"):]
                        if await release_file(db, path, prior_actor):
                            released.append(rid)
                    elif body.startswith("symbol:"):
                        path, _, sym = body[len("symbol:"):].partition("::")
                        if sym and await release_symbol(db, prior_actor, path, sym):
                            released.append(rid)
                        elif not sym and await release_file(db, path, prior_actor):
                            released.append(rid)
                    else:
                        if await release_resource(db, body, prior_actor):
                            released.append(rid)
                except Exception:  # noqa: BLE001 — one bad resource id must not block the rest
                    continue
        except Exception:  # noqa: BLE001 — lock release is best-effort, never blocks the reset
            pass

    from meridian.db import record_action_audit_event  # noqa: PLC0415
    detail = json.dumps({
        "item_id": item_id,
        "prior_actor": prior_actor,
        "prior_claimed_at": prior_claimed_at,
        "released_resources": released,
        "classification": verdict.get("classification"),
        "reasons": verdict.get("reasons"),
        "signals": verdict.get("signals"),
    })
    try:
        await record_action_audit_event(
            db, RECONCILE_STALE_CLAIM_AUDIT_EVENT,
            project_id=project_id, actor=actor, detail=detail,
        )
    except Exception:  # noqa: BLE001 — an audit-log hiccup must not undo an already-committed reset
        pass

    updated = await get_sprint_item(db, item_id)
    return {
        "item_id": item_id,
        "prior_actor": prior_actor,
        "prior_claimed_at": prior_claimed_at,
        "released_resources": released,
        "stall_count": new_stall_count,
        "reasons": verdict.get("reasons"),
        "item": updated,
    }


async def reconcile_stale_claims(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    version: str | None = None,
    item_ids: list[str] | None = None,
    dry_run: bool = True,
    max_batch: int = _RECONCILE_DEFAULT_BATCH,
    actor: str | None = None,
    repo_root: "Any | None" = None,
) -> dict[str, Any]:
    """56e9b3c7 — project/version-scoped, auditable stale-claim reconciliation
    sweep. The bulk counterpart to claim_sprint_item's inline autonomous
    reconciliation — for a scheduler path, or an explicit human/planner-
    triggered audit across a whole board.

    Scans ``in_progress`` items in ``project_id`` (optionally narrowed to one
    ``version`` and/or an explicit ``item_ids`` allow-list — never cross-project:
    every candidate query is hard-scoped to ``project_id``), classifies each
    via :func:`classify_stale_claim`, and — ONLY when ``dry_run=False`` — resets
    every "stale" verdict via :func:`_reset_stale_claim`. "active" and
    "ambiguous" verdicts are NEVER touched, dry-run or not.

    ``dry_run=True`` (the default) performs the full scan/classification and
    reports exactly what WOULD happen without writing anything — safe to run
    against any project, including live production boards, at any time.

    ``max_batch`` bounds how many in_progress candidates are classified (and,
    if not dry-run, potentially reset) in a single call — capped at
    :data:`_RECONCILE_MAX_BATCH` regardless of what's requested, so one call
    can never turn into an unbounded scan/lock-release storm on a huge board.
    ``truncated=True`` on the result means more candidates exist than were
    scanned this call — page through with subsequent calls.

    Returns ``{"project_id", "version", "dry_run", "max_batch",
    "candidates_total", "scanned", "truncated", "active": [...],
    "stale": [...], "ambiguous": [...], "reset": [...], "errors": [...]}``.
    Each of active/stale/ambiguous holds classify_stale_claim's verdict dicts;
    "reset" holds _reset_stale_claim's result dicts (only populated when
    dry_run=False); "errors" holds ``{"item_id", "error"}`` for any single
    candidate whose classification or reset blew up — one bad item never
    aborts the whole sweep.
    """
    if max_batch <= 0:
        raise ValueError("max_batch must be positive")
    max_batch = min(int(max_batch), _RECONCILE_MAX_BATCH)

    where = ["project_id = ?", "status = 'in_progress'"]
    params: list[Any] = [project_id]
    if version:
        where.append("version = ?")
        params.append(version)
    if item_ids:
        placeholders = ", ".join("?" for _ in item_ids)
        where.append(f"id IN ({placeholders})")
        params.extend(item_ids)
    query = (
        f"SELECT * FROM sprint_items WHERE {' AND '.join(where)} "
        "ORDER BY claimed_at ASC"
    )
    async with db.execute(query, tuple(params)) as cur:
        rows = await cur.fetchall()
    candidates = [r for r in (_row_to_dict(row) for row in rows) if r]

    active: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    reset: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    scanned = 0
    for cand in candidates:
        if scanned >= max_batch:
            break
        scanned += 1
        try:
            verdict = await classify_stale_claim(db, cand, repo_root=repo_root)
        except Exception as exc:  # noqa: BLE001 — one bad item must never wedge the sweep
            errors.append({"item_id": cand.get("id"), "error": str(exc)})
            continue
        cls = verdict.get("classification")
        if cls == RECONCILE_ACTIVE:
            active.append(verdict)
        elif cls in (RECONCILE_AMBIGUOUS, RECONCILE_NOT_APPLICABLE):
            ambiguous.append(verdict)
        else:  # RECONCILE_STALE
            stale.append(verdict)
            if not dry_run:
                try:
                    reset_result = await _reset_stale_claim(
                        db, project_id, cand["id"], verdict, actor=actor,
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append({"item_id": cand.get("id"), "error": str(exc)})
                    continue
                if reset_result is not None:
                    reset.append(reset_result)

    return {
        "project_id": project_id,
        "version": version,
        "dry_run": dry_run,
        "max_batch": max_batch,
        "candidates_total": len(candidates),
        "scanned": scanned,
        "truncated": len(candidates) > scanned,
        "active": active,
        "stale": stale,
        "ambiguous": ambiguous,
        "reset": reset,
        "errors": errors,
    }


async def fail_sprint_item(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    reason: str | None = None,
) -> dict[str, Any] | None:
    """Mark a sprint item ``failed``. ``reason`` stored in ``notes``."""
    result = await _update_sprint_item_status(
        db, project_id, item_id, "failed", notes=reason,
        expected_statuses=_ACTIVE_SPRINT_STATUSES,
    )
    if result is not None:
        await _maybe_rollup_parent(db, project_id, item_id)
    return result


async def push_sprint_item(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    to_version: str,
) -> dict[str, Any] | None:
    """Mark a sprint item ``pushed`` — deferred to a future version.

    ``to_version`` is stored in ``pushed_to`` so the board can show
    where the item was moved and the next sprint can pick it up.
    """
    if not to_version:
        raise ValueError("to_version is required for push_sprint_item")
    return await _update_sprint_item_status(
        db, project_id, item_id, "pushed", pushed_to=to_version,
        expected_statuses=_ACTIVE_SPRINT_STATUSES,
    )


async def patch_sprint_item(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    title: str | None = None,
    version: str | None = None,
    status: str | None = None,
    feedback_thumb: int | None = None,
    feedback_note: str | None = None,
    notes: str | None = None,
    human_id: str | None = None,
    item_group: str | None = None,
    touches_resources: Any = _UNSET,
    required_notes: bool | int | None = None,
    deferred_until: Any = _UNSET,
    track: Any = _UNSET,
    priority: str | None = None,
    blocker_kind: Any = _UNSET,
    wave: Any = _UNSET,
    sprint_name: Any = _UNSET,
    prospect_bypass: Any = _UNSET,
    depends_on: Any = _UNSET,
    require_verification: Any = _UNSET,
    require_strict_evidence: Any = _UNSET,
    required_tool: Any = _UNSET,
    tool_requirements: Any = _UNSET,
    artifact_kind: Any = _UNSET,
    planned_output: Any = _UNSET,
    artifact_policy: Any = _UNSET,
    github_channel: Any = _UNSET,
) -> dict[str, Any] | None:
    """Update editable fields of a sprint item.

    Editable: title, version, status, feedback, notes, human_id (assignee),
    item_group, touches_resources, required_notes, deferred_until, track,
    priority, blocker_kind, sprint_name, prospect_bypass, depends_on,
    require_verification, required_tool. Only fields passed as non-None are
    changed;
    omitted fields are left untouched. To clear human_id or item_group, pass an
    empty string. ``touches_resources`` (501ec93f) uses the ``_UNSET`` sentinel
    so it can be omitted entirely; pass ``None`` or ``[]`` to clear it, or a list
    / JSON string / comma-separated string of typed ids to set it.
    ``depends_on`` (56f607ec) uses the ``_UNSET`` sentinel: omit to leave
    unchanged, pass an empty string / ``None`` to CLEAR it (item becomes
    independently claimable again), or another sprint item's id to set/fix
    dependency ordering retroactively — previously ``depends_on`` could only
    be set at creation time via ``add_sprint_item``, with no way to fix
    ordering on an already-filed item. Raises ``meridian.dependency_graph.
    DependencyCycleError`` (a ``ValueError`` subclass, reason='cycle') if the
    edit would create a dependency cycle — this covers both a plain
    self-dependency (``depends_on == item_id``, a cycle of length one) and a
    longer cycle introduced by retroactively rewiring an existing chain (e.g.
    A depends_on B, then patching B to depend_on A); the exception carries
    the full ``cycle_path`` for diagnostics (05553946). Missing / cross-
    project / merged-away dependency targets are deliberately NOT rejected
    here — that stale-reference check is intentionally deferred to handoff-
    render time (see ``meridian.db.board_snapshot.find_stale_reference_ids``,
    ee8a6af1), so a dependency on a not-yet-created or not-yet-synced item id
    keeps working exactly as before.
    ``deferred_until`` / ``track`` (dec69708) also use the ``_UNSET`` sentinel:
    omit to leave unchanged, pass an empty string / ``None`` to CLEAR the
    deferral (making the item immediately claimable again), or an ISO timestamp
    / track name to set it.
    ``priority`` (e08fee30) is left unchanged when ``None``; pass one of
    {urgent, high, normal, low} to set it (a bad value raises ValueError, like
    milestone_type). ``blocker_kind`` (2282a636) uses the ``_UNSET`` sentinel:
    omit to leave unchanged, pass an empty string / ``None`` to CLEAR it (ordinary
    item), or 'manual' to mark it blocked on a real-world action outside Meridian.
    ``sprint_name`` (3d6bd938) uses the ``_UNSET`` sentinel: omit to leave
    unchanged, pass an empty string / ``None`` to CLEAR it, or a non-empty
    string to set a human-readable label (distinct from the structural ``version``).
    ``prospect_bypass`` (94c26322) uses the ``_UNSET`` sentinel: omit to leave
    unchanged, pass ``True`` / ``1`` to SET the bypass (allowing an unprospected
    item through the goal-generation and claim gates), or ``False`` / ``0`` to
    CLEAR it (re-enabling the structural gate). Settable by planning/human sessions
    only — executor sessions should not set this field.
    ``require_verification`` (e2e1b682) uses the ``_UNSET`` sentinel: omit to
    leave unchanged, pass ``True`` / ``1`` to SET the independent
    fresh-session verifier gate (completion via ``complete_sprint_item``
    then requires an on-file PASS filed by a session distinct from the one
    completing it), or ``False`` / ``0`` to CLEAR it (ordinary completion,
    evidence gate only).
    ``require_strict_evidence`` (5fe3502e) uses the ``_UNSET`` sentinel: omit
    to leave unchanged, pass ``True`` / ``1`` to SET the opt-in fail-closed
    evidence gate (``complete_sprint_item`` calls via
    ``meridian.mcp.handlers.sprint_tools.handle_complete_sprint_item`` then
    refuse completion — ``STRICT_EVIDENCE_BLOCKED`` — unless declared evidence
    is present, resolves to something real, isn't stale, matches the
    completing session's own worktree, and no file was edited without a
    claim; see ``meridian.sprint_evidence_guard``), or ``False`` / ``0`` to
    CLEAR it (ordinary advisory-only evidence checks). This is the persistent,
    per-item counterpart to passing ``strict_evidence=true`` on a single
    ``complete_sprint_item`` call — either is sufficient to engage the gate.
    ``required_tool`` (4d1fb28f) uses the ``_UNSET`` sentinel: omit to leave
    unchanged, pass an empty string / ``None`` to CLEAR the pin (ordinary
    executor discretion), or a free-form tool/plugin name (e.g. 'Serena:
    replace_symbol_body') to SET it — rendered as a hard directive in the
    /goal block, not left to executor habit.
    ``tool_requirements`` (76dde31f, 665 follow-up) uses the ``_UNSET``
    sentinel: omit to leave unchanged, pass ``None`` / ``[]`` to CLEAR the
    structured contract (falls back to ``required_tool`` if still set — see
    ``tool_requirements.effective_tool_requirements``), or a list of typed
    entries (schema: name, server_or_namespace, required_or_preferred,
    purpose, call_template, fallback, availability_check, verification) to
    SET/REPLACE it wholesale. Raises ``ValueError`` (via
    ``tool_requirements.ToolRequirementError``) on malformed input — unknown
    fields, missing required fields, secret-shaped values, or machine-local
    absolute paths.
    ``artifact_kind`` / ``planned_output`` / ``artifact_policy`` (2f9cb288,
    665 follow-up) each independently use the ``_UNSET`` sentinel: omit to
    leave unchanged, pass ``None`` (or ``""`` for ``artifact_kind``) to CLEAR
    it, or a valid value to SET/REPLACE it wholesale — see
    ``meridian.artifact_declaration`` for the full schema. ``artifact_kind``
    is one of ``document_only``/``figure``/``table``. ``planned_output`` is a
    typed pointer object (``source_type``, ``targets``, ``label?``,
    ``provenance_required?``), validated via
    ``meridian.pointers.validate_pointer``. ``artifact_policy`` is
    ``{artifact_pointer_check?, require_exact_figure_output_pointer?,
    require_exact_table_output_pointer?, allow_document_only_override?}``.
    Raises ``ValueError`` (via
    ``artifact_declaration.ArtifactDeclarationError``) on malformed input.
    ``github_channel`` (7c82f7c8) uses the ``_UNSET`` sentinel: omit to leave
    unchanged, pass an empty string / ``None`` to CLEAR it, or one of
    {nightly, stable, graduated} to set it. Mirrors the channel:nightly /
    channel:stable GitHub labels applied via issue-template choice
    (.github/ISSUE_TEMPLATE/); 'graduated' marks a bug that started as
    nightly-only noise but is now confirmed reproducing on stable — the
    signal it needs a real fix before general release. A bad value raises
    ValueError, like blocker_kind.
    """
    # 6a17e735 / ARCH 1B — separate the status change (routed through
    # _transition_status for guaranteed cache bust + live event) from the
    # non-status field updates (a plain UPDATE is fine there). Build two
    # independent lists: ns_fields/ns_values for non-status fields, and
    # capture status_value for the _transition_status call below.
    status_value: str | None = None
    if status is not None:
        if status not in _VALID_SPRINT_STATUSES:
            raise ValueError(f"invalid sprint-item status: {status!r}")
        # 6a17e735 — patch_sprint_item is a generic field-editor, not a
        # state-transition function. Only administrative resets are allowed
        # here; every terminal/business-logic status has a dedicated function
        # that enforces its own guards (evidence gate, rollup, task-chain,
        # claimed_at, cache invalidation, live dashboard event) and MUST be
        # used instead — silently allowing them here bypassed all of that.
        if status not in _PATCH_SPRINT_ITEM_ALLOWED_STATUSES:
            _dedicated = {
                "done": "complete_sprint_item",
                "skipped": "skip_sprint_item",
                "failed": "fail_sprint_item",
                "pushed": "push_sprint_item (or the version-push flow)",
                "in_progress": "claim_sprint_item",
                "provisional_complete": "provisional_complete_sprint_item",
            }.get(status, "a dedicated state-transition function")
            raise ValueError(
                f"patch_sprint_item cannot set status={status!r} — this is a "
                f"guarded transition that must go through {_dedicated}, which "
                "enforces evidence/rollup/task-chain rules patch_sprint_item "
                f"does not. Allowed here: {sorted(_PATCH_SPRINT_ITEM_ALLOWED_STATUSES)}."
            )
        status_value = status

    # Build non-status field lists (no status/completed_at entries here).
    ns_fields: list[str] = []
    ns_values: list[Any] = []
    if title is not None:
        ns_fields.append("title = ?")
        ns_values.append(title)
    if version is not None:
        ns_fields.append("version = ?")
        ns_values.append(version)
    if feedback_thumb is not None:
        ns_fields.append("feedback_thumb = ?")
        ns_values.append(int(feedback_thumb))
    if feedback_note is not None:
        ns_fields.append("feedback_note = ?")
        ns_values.append(feedback_note)
    if notes is not None:
        from meridian.secret_redaction import check_for_secrets
        check_for_secrets(notes, context="sprint item notes")
        ns_fields.append("notes = ?")
        ns_values.append(notes)
    if human_id is not None:
        ns_fields.append("human_id = ?")
        ns_values.append(human_id or None)
    if item_group is not None:
        ns_fields.append("item_group = ?")
        ns_values.append(item_group or None)
    if touches_resources is not _UNSET:
        ns_fields.append("touches_resources = ?")
        ns_values.append(serialize_touches_resources(touches_resources))
    if required_notes is not None:
        ns_fields.append("required_notes = ?")
        ns_values.append(1 if required_notes else 0)
    if deferred_until is not _UNSET:
        # Empty string / None CLEARS the deferral (item becomes claimable).
        ns_fields.append("deferred_until = ?")
        ns_values.append(deferred_until or None)
    if track is not _UNSET:
        ns_fields.append("track = ?")
        ns_values.append(track or None)
    if priority is not None:
        # e08fee30 — validate the enum; raise on a bad value like milestone_type.
        if priority not in _VALID_SPRINT_PRIORITIES:
            raise ValueError(
                f"priority must be one of {_VALID_SPRINT_PRIORITIES}, got {priority!r}"
            )
        ns_fields.append("priority = ?")
        ns_values.append(priority)
    if blocker_kind is not _UNSET:
        # 2282a636 — empty string / None CLEARS it (ordinary item); otherwise
        # validate the enum (only 'manual' is defined).
        _bk = blocker_kind or None
        if _bk is not None and _bk not in _VALID_SPRINT_BLOCKER_KINDS:
            raise ValueError(
                f"blocker_kind must be None or one of {_VALID_SPRINT_BLOCKER_KINDS}, "
                f"got {blocker_kind!r}"
            )
        ns_fields.append("blocker_kind = ?")
        ns_values.append(_bk)
    if wave is not _UNSET:
        # 58a45b92 — empty string / None CLEARS the wave (unassigned); any other
        # value sets the stored wave label. No enum: labels are free-form (e.g.
        # 'wave-1'), auto-filled by assign_sprint_waves or hand-set here.
        ns_fields.append("wave = ?")
        ns_values.append(wave or None)
    if sprint_name is not _UNSET:
        # 3d6bd938 — empty string / None CLEARS the sprint name; any other value
        # sets a human-readable label for the bucket (distinct from version).
        ns_fields.append("sprint_name = ?")
        ns_values.append(sprint_name or None)
    if prospect_bypass is not _UNSET:
        # 94c26322 — True/1 SETS the bypass (human override: allow unprospected
        # item through the goal-generation and claim gates); False/0/None CLEARS
        # it (re-enable the structural gate). Stored as INTEGER 0/1.
        ns_fields.append("prospect_bypass = ?")
        ns_values.append(1 if prospect_bypass else 0)
    if depends_on is not _UNSET:
        # 56f607ec — empty string / None CLEARS the dependency (item becomes
        # independently claimable); otherwise set it to another item's id.
        # Previously depends_on could only be fixed at creation time — this
        # closes the gap that forced ordering into prose notes instead.
        _dep = depends_on or None
        if _dep is not None:
            # 05553946 — reject a self-dependency OR any longer cycle the edit
            # would close. A self-dependency is just a cycle of length one, so
            # this single cycle-detection call replaces the previous ad hoc
            # ``_dep == item_id`` check with one consistent, fully-diagnosed
            # code path (both raise DependencyCycleError, a ValueError
            # subclass, with the full cycle_path attached).
            _all_project_items = await get_sprint_items(db, project_id)
            _cycle = _dependency_graph.find_dependency_cycle(
                _all_project_items, proposed_edge=(item_id, _dep)
            )
            if _cycle:
                raise _dependency_graph.DependencyCycleError(_cycle)
        ns_fields.append("depends_on = ?")
        ns_values.append(_dep)
    if require_verification is not _UNSET:
        # e2e1b682 — True/1 SETS the independent fresh-session verifier gate
        # (complete_sprint_item then requires an on-file PASS filed by a
        # session distinct from the one completing it); False/0/None CLEARS it
        # (ordinary completion, evidence gate only). Stored as INTEGER 0/1.
        ns_fields.append("require_verification = ?")
        ns_values.append(1 if require_verification else 0)
    if require_strict_evidence is not _UNSET:
        # 5fe3502e — True/1 SETS the opt-in fail-closed evidence gate
        # (complete_sprint_item's handler-level strict verification refuses
        # completion on missing/invalid/stale/wrong-worktree evidence or
        # unclaimed edits unless explicitly, auditedly overridden);
        # False/0/None CLEARS it (ordinary advisory-only evidence checks).
        # Stored as INTEGER 0/1, same shape as require_verification.
        ns_fields.append("require_strict_evidence = ?")
        ns_values.append(1 if require_strict_evidence else 0)
    if required_tool is not _UNSET:
        # 4d1fb28f — empty string / None CLEARS the pin (ordinary executor
        # discretion); any other value sets the free-form required-tool name.
        # No enum: tool/plugin names are arbitrary strings ('Serena:
        # replace_symbol_body', a named tunnel plugin, 'meridian__patch_file').
        ns_fields.append("required_tool = ?")
        ns_values.append(required_tool or None)
    if tool_requirements is not _UNSET:
        # 76dde31f (665 follow-up) — None/[] CLEARS the structured contract
        # (falls back to required_tool if still set); any other value is
        # validated/normalized and REPLACES the stored list wholesale. Raises
        # ToolRequirementError (a ValueError) on malformed input.
        ns_fields.append("tool_requirements = ?")
        ns_values.append(_tool_requirements.serialize_tool_requirements(tool_requirements))
    if artifact_kind is not _UNSET:
        # 2f9cb288 (665 follow-up) — empty string / None CLEARS it (unknown);
        # otherwise validate the enum (document_only/figure/table), like
        # blocker_kind/github_channel. Raises ArtifactDeclarationError (a
        # ValueError) on an unlisted value.
        ns_fields.append("artifact_kind = ?")
        ns_values.append(
            _artifact_declaration.normalize_artifact_kind(artifact_kind)
            if artifact_kind else None
        )
    if planned_output is not _UNSET:
        # 2f9cb288 (665 follow-up) — None CLEARS the declared planned output;
        # any other value is validated (a typed pointer, via
        # meridian.pointers.validate_pointer — NOT a free-form path) and
        # REPLACES the stored value wholesale. Raises ArtifactDeclarationError
        # (a ValueError) on malformed input.
        ns_fields.append("planned_output = ?")
        ns_values.append(_artifact_declaration.serialize_planned_output(planned_output))
    if artifact_policy is not _UNSET:
        # 2f9cb288 (665 follow-up) — None CLEARS the per-item policy override
        # (reads back as the project default warn policy — see
        # artifact_declaration.effective_artifact_policy); any other value is
        # validated/normalized and REPLACES the stored policy wholesale.
        # Raises ArtifactDeclarationError (a ValueError) on malformed input.
        ns_fields.append("artifact_policy = ?")
        ns_values.append(_artifact_declaration.serialize_artifact_policy(artifact_policy))
    if github_channel is not _UNSET:
        # 7c82f7c8 — empty string / None CLEARS it; otherwise validate the
        # enum (nightly / stable / graduated), like blocker_kind.
        _gc = github_channel or None
        if _gc is not None and _gc not in _VALID_SPRINT_GITHUB_CHANNELS:
            raise ValueError(
                f"github_channel must be None or one of "
                f"{_VALID_SPRINT_GITHUB_CHANNELS}, got {github_channel!r}"
            )
        ns_fields.append("github_channel = ?")
        ns_values.append(_gc)

    if not ns_fields and status_value is None:
        return await get_sprint_item(db, item_id)

    result = None
    if ns_fields:
        # Phase 1: write non-status fields. No race guard needed here — these
        # are plain metadata updates that are always safe to apply.
        cursor = await db.execute(
            f"UPDATE sprint_items SET {', '.join(ns_fields)} "
            "WHERE id = ? AND project_id = ?",
            ns_values + [item_id, project_id],
        )
        await db.commit()
        if cursor.rowcount == 0:
            return None
        result = await get_sprint_item(db, item_id)

    if status_value is not None:
        # Phase 2: route the status write through _transition_status so cache
        # invalidation and live dashboard event are guaranteed (6a17e735).
        # No from_statuses guard: patch_sprint_item is an administrative reset
        # and succeeds from any current status.
        result = await _transition_status(
            db, project_id, item_id, status_value,
            from_statuses=None,
        )
        if result is None:
            # Item vanished between the non-status write and this call — very
            # unlikely but handle it consistently.
            return None

    if result is None:
        result = await get_sprint_item(db, item_id)
    return result


async def add_subtask(
    db: aiosqlite.Connection,
    project_id: str,
    parent_id: str,
    title: str,
    owner: str | None = None,
    prospect_bypass: bool = False,
) -> dict[str, Any]:
    """Create a child sprint item under parent_id.

    Inherits version from parent. Rejects if parent doesn't exist or is
    done/failed/skipped.

    4f02340e — mixed-ownership task chains. ``owner`` is 'human', 'ai', or None
    (unassigned). When owner-tagged subtasks are added in sequence they form a
    *chain*: each new owned subtask ``depends_on`` the previously added owned
    sibling, so only the head of the chain is claimable and ownership alternates
    as the chain advances (see :func:`_advance_task_chain`). When an AI subtask
    completes and the next link is human-owned, a HITL handoff is auto-filed;
    when a human completes theirs, the next AI subtask un-blocks (becomes
    claimable). The parent stays in_progress until every subtask is terminal
    (existing :func:`_maybe_rollup_parent` behavior — unchanged).

    Unowned subtasks (owner=None) keep the legacy behavior: no chaining,
    independently claimable.
    """
    if owner not in (None, "human", "ai"):
        raise ValueError("owner must be 'human', 'ai', or None")
    parent = await get_sprint_item(db, parent_id)
    if parent is None or parent.get("project_id") != project_id:
        raise ValueError(f"parent sprint item not found: {parent_id}")
    blocked = {"done", "failed", "skipped"}
    if (parent.get("status") or "pending") in blocked:
        raise ValueError(
            f"cannot add subtask to parent with status '{parent.get('status')}'"
        )
    # Chain owned subtasks: a new owned subtask depends on the current tail of
    # the chain — the owned sibling that no other owned sibling depends on yet.
    # This is insertion-order-independent (added_at has only second resolution,
    # so it can't be used to break ties deterministically) and portable across
    # SQLite/Postgres. Unowned subtasks never chain.
    depends_on: str | None = None
    if owner is not None:
        async with db.execute(
            "SELECT id FROM sprint_items "
            "WHERE parent_id = ? AND project_id = ? AND owner IS NOT NULL "
            "AND id NOT IN ("
            "  SELECT depends_on FROM sprint_items "
            "  WHERE parent_id = ? AND project_id = ? AND depends_on IS NOT NULL"
            ")",
            (parent_id, project_id, parent_id, project_id),
        ) as cur:
            tails = await cur.fetchall()
        # In a well-formed chain there is exactly one tail. If somehow more than
        # one (e.g. an unchained owned item existed), prefer the one matching no
        # dependents — take the first deterministically by id.
        tail_ids = sorted(
            (r["id"] if isinstance(r, dict) else r[0]) for r in tails
        )
        if tail_ids:
            depends_on = tail_ids[-1]
    iid = _new_id()
    await db.execute(
        "INSERT INTO sprint_items "
        "(id, project_id, version, title, parent_id, milestone_type, owner, depends_on, "
        "prospect_bypass) "
        "VALUES (?, ?, ?, ?, ?, 'task', ?, ?, ?)",
        (iid, project_id, parent.get("version", ""), title, parent_id, owner, depends_on,
         1 if prospect_bypass else 0),
    )
    await db.commit()
    item = await get_sprint_item(db, iid)
    assert item is not None
    _invalidate_sprint_items_cache(project_id)
    return item


async def _advance_task_chain(
    db: aiosqlite.Connection,
    project_id: str,
    completed_item_id: str,
) -> dict[str, Any] | None:
    """4f02340e — advance a mixed-ownership subtask chain after a completion.

    Called when a subtask is marked ``done``. Finds the next link in the chain
    (the owned sibling whose ``depends_on`` is the just-completed item) and:

      - next link is **human**-owned  → auto-file a HITL handoff (kind
        ``'handoff'``, assigned to the human) so a person is pulled in. The next
        item un-blocks (its depends_on is now done) and shows as claimable in
        the human's queue.
      - next link is **ai**-owned     → no HITL; the item simply un-blocks and
        becomes claimable by an AI session (existing depends_on machinery).

    Returns the filed HITL request dict when a handoff was created, else None.
    Idempotent-ish: a handoff is only filed when the just-completed item is
    itself owned (so it is part of a chain) and a next owned link exists.
    """
    completed = await get_sprint_item(db, completed_item_id)
    if completed is None or completed.get("project_id") != project_id:
        return None
    if not completed.get("owner"):
        return None  # not part of an owned chain
    # The next link: an owned sibling that depends on the completed item.
    async with db.execute(
        "SELECT * FROM sprint_items "
        "WHERE project_id = ? AND depends_on = ? AND owner IS NOT NULL "
        "ORDER BY added_at ASC, id ASC LIMIT 1",
        (project_id, completed_item_id),
    ) as cur:
        row = await cur.fetchone()
    nxt = _row_to_dict(row) if row is not None else None
    if not nxt:
        return None
    if (nxt.get("status") or "pending") in {"done", "failed", "skipped"}:
        return None
    if nxt.get("owner") != "human":
        # AI link — nothing to file; depends_on now satisfied → claimable.
        _publish_project_event(
            project_id, "sprint_item_updated",
            {"item_id": nxt["id"], "chain": "ai_claimable"},
        )
        return None
    # Human link — pull a person in via a HITL handoff.
    title = nxt.get("title", "")
    question = (
        f"Task chain handoff: your turn on subtask '{title}'. "
        f"The preceding AI subtask ('{completed.get('title', '')}') is complete."
    )
    context = (
        f"Mixed-ownership task chain (parent {completed.get('parent_id') or '?'}). "
        f"Next subtask {nxt['id']} is assigned to a human. Mark it done "
        f"(complete_sprint_item) to release the following AI subtask."
    )
    # Shared helper imported from parent module at call time to avoid circular
    # import: sprint_items.py is loaded by db/__init__.py, which also contains
    # request_hitl (defined after the `from .sprint_items import *` line).
    from meridian.db import request_hitl  # noqa: PLC0415 — lazy import avoids circular dep
    hitl = await request_hitl(
        db, project_id, question,
        context=context,
        kind="handoff",
        assigned_to=nxt.get("human_id") or "human",
    )
    _publish_project_event(
        project_id, "sprint_item_updated",
        {"item_id": nxt["id"], "chain": "human_handoff", "hitl_id": hitl.get("id")},
    )
    return hitl


async def split_sprint_item(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    titles: list[str],
) -> list[dict[str, Any]]:
    """Split a sprint item into N new items at the same level.

    Closes the original (status=skipped). New items inherit parent_id and
    version from the original, with split_from=item_id.
    """
    original = await get_sprint_item(db, item_id)
    if original is None or original.get("project_id") != project_id:
        raise ValueError(f"sprint item not found: {item_id}")
    allowed = {"pending", "in_progress"}
    if (original.get("status") or "pending") not in allowed:
        raise ValueError(
            f"can only split pending or in_progress items, got '{original.get('status')}'"
        )
    if not titles:
        raise ValueError("titles must not be empty")
    # Close the original. fa3e3331 — atomic from-state guard: the pre-check
    # above is a read-then-write race like every other transition; a
    # concurrent status change between that read and this call must not
    # silently close an item mid-transition. Re-raise as the same ValueError
    # shape the pre-check above already uses, for caller consistency.
    try:
        await _update_sprint_item_status(
            db, project_id, item_id, "skipped", expected_statuses=allowed,
        )
    except SprintItemStatusRace as exc:
        raise ValueError(
            f"can only split pending or in_progress items, got '{exc.current_status}'"
        ) from exc
    # Create new items
    new_items = []
    for t in titles:
        nid = _new_id()
        # ae87699d — generate slug + nickname on every creation path, not just
        # add_sprint_item. split_sprint_item was leaving both null.
        _item_slug = await _unique_sprint_slug(
            db, project_id, _sprint_item_slug_base(t)
        )
        _item_nickname = await _unique_sprint_nickname(
            db, project_id, _sprint_item_nickname_base(t, nid)
        )
        await db.execute(
            "INSERT INTO sprint_items "
            "(id, project_id, version, title, parent_id, split_from, milestone_type, "
            "slug, nickname) "
            "VALUES (?, ?, ?, ?, ?, ?, 'task', ?, ?)",
            (nid, project_id, original.get("version", ""), t,
             original.get("parent_id"), item_id, _item_slug, _item_nickname),
        )
        await db.commit()
        new_item = await get_sprint_item(db, nid)
        if new_item:
            new_items.append(new_item)
    return new_items


async def merge_sprint_items(
    db: aiosqlite.Connection,
    project_id: str,
    item_ids: list[str],
    new_title: str,
) -> dict[str, Any]:
    """Merge N sprint items into one survivor.

    Closes all sources (status=skipped, merged_into=survivor_id).
    Creates survivor with merged_from=JSON(item_ids), version from first source.
    All sources must be pending or in_progress.
    """
    if not item_ids:
        raise ValueError("item_ids must not be empty")
    sources = []
    allowed = {"pending", "in_progress"}
    for iid in item_ids:
        item = await get_sprint_item(db, iid)
        if item is None or item.get("project_id") != project_id:
            raise ValueError(f"sprint item not found: {iid}")
        if (item.get("status") or "pending") not in allowed:
            raise ValueError(
                f"cannot merge item '{iid}' with status '{item.get('status')}'"
            )
        sources.append(item)
    # Create the survivor first
    survivor_id = _new_id()
    version = sources[0].get("version", "")
    merged_from_json = json.dumps(item_ids)
    # ae87699d — generate slug + nickname on every creation path, not just
    # add_sprint_item. merge_sprint_items was leaving both null on the survivor.
    _survivor_slug = await _unique_sprint_slug(
        db, project_id, _sprint_item_slug_base(new_title)
    )
    _survivor_nickname = await _unique_sprint_nickname(
        db, project_id, _sprint_item_nickname_base(new_title, survivor_id)
    )
    await db.execute(
        "INSERT INTO sprint_items "
        "(id, project_id, version, title, merged_from, milestone_type, slug, nickname) "
        "VALUES (?, ?, ?, ?, ?, 'task', ?, ?)",
        (survivor_id, project_id, version, new_title, merged_from_json,
         _survivor_slug, _survivor_nickname),
    )
    # Close all sources. fa3e3331 — atomic from-state guard: the pre-check
    # loop above is a read-then-write race like every other transition. A
    # source item that a concurrent caller completed/failed/skipped between
    # that pre-check and this UPDATE must not be silently overwritten to
    # 'skipped' — roll back the whole merge (including the survivor insert
    # above) rather than leave a half-merged state, since a merge is only
    # meaningful as an all-or-nothing operation.
    #
    # For SQLite (aiosqlite, autocommit=False): db.rollback() undoes everything
    # atomically.  For Postgres (PostgresConnection, autocommit=True): rollback()
    # is a no-op, so we apply compensating actions first — undo the source
    # closures already committed and delete the survivor — then call rollback()
    # (which is harmless on Postgres) for the SQLite path.
    closed_so_far: list[tuple[str, str]] = []  # (item_id, original_status) pairs
    for src, iid in zip(sources, item_ids):
        cursor = await db.execute(
            "UPDATE sprint_items SET status = 'skipped', completed_at = datetime('now'), "
            "merged_into = ? WHERE id = ? AND project_id = ? "
            "AND status IN ('pending', 'in_progress')",
            (survivor_id, iid, project_id),
        )
        if cursor.rowcount == 0:
            _raced = await get_sprint_item(db, iid)
            # Compensating actions for Postgres (autocommit=True): undo the
            # source closures that already committed before this race was found,
            # and delete the survivor that was inserted above.
            if hasattr(db, "_pool"):
                # Postgres path: each prior execute() already auto-committed.
                for prev_iid, orig_status in closed_so_far:
                    await db.execute(
                        "UPDATE sprint_items SET status = ?, merged_into = NULL, "
                        "completed_at = NULL WHERE id = ? AND project_id = ?",
                        (orig_status, prev_iid, project_id),
                    )
                await db.execute(
                    "DELETE FROM sprint_items WHERE id = ? AND project_id = ?",
                    (survivor_id, project_id),
                )
            # SQLite path: rollback() undoes everything atomically.
            await db.rollback()
            raise ValueError(
                f"cannot merge item '{iid}': status changed to "
                f"'{(_raced or {}).get('status')}' before the merge could complete"
            )
        closed_so_far.append((iid, src.get("status") or "pending"))
    await db.commit()
    _publish_project_event(project_id, "sprint_item_updated", {"merged_into": survivor_id})
    survivor = await get_sprint_item(db, survivor_id)
    assert survivor is not None
    return survivor


async def get_sprint_items_page(
    db: aiosqlite.Connection,
    project_id: str,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Return one SQL LIMIT/OFFSET page of sprint items plus the total count.

    True server-side pagination for large completed lists (hundreds of rows) so
    the dashboard's Completed tab doesn't fetch everything at once. Mirrors
    get_sprint_items ordering. Does not do dependency (show_blocked) filtering —
    it's for flat status-filtered lists like status='done'.
    """
    where = "project_id = ?"
    params_list: list = [project_id]
    if status is not None:
        if status not in _VALID_SPRINT_STATUSES:
            raise ValueError(
                f"invalid sprint-item status filter: {status!r}. "
                f"Valid: {sorted(_VALID_SPRINT_STATUSES)}"
            )
        where += " AND status = ?"
        params_list.append(status)
    async with db.execute(
        f"SELECT COUNT(*) AS c FROM sprint_items WHERE {where}", tuple(params_list)
    ) as cur:
        crow = await cur.fetchone()
    total = int(crow["c"] if isinstance(crow, dict) else crow[0]) if crow else 0
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    async with db.execute(
        f"SELECT * FROM sprint_items WHERE {where} "
        "ORDER BY added_at ASC, rowid ASC LIMIT ? OFFSET ?",
        (*params_list, limit, offset),
    ) as cur:
        rows = await cur.fetchall()
    items = [_row_to_dict(r) for r in rows]  # type: ignore[misc]
    return items, total


async def infer_active_sprint_version(
    db: aiosqlite.Connection, project_id: str
) -> str | None:
    """a76cb7c0 — infer the sprint-version bucket with the most pending items.

    Counts pending (status ``pending``/``todo``) sprint items per non-empty
    ``version`` and returns the bucket with the most. Human-assigned items
    (milestone_type='human') are excluded — executor scoping should track the
    automatable backlog, not a person's task list. Ties break on the bucket whose
    earliest pending item was added first (the older sprint), so scoping is
    stable across calls rather than flapping between equally-sized buckets.

    Returns ``None`` when there are no pending items (or none carry a version),
    so a session over an empty/version-less board is left unscoped (no filter)
    and behaves exactly as before.
    """
    counts: dict[str, int] = {}
    first_seen: dict[str, str] = {}
    for it in await get_sprint_items(db, project_id, include_human=False):
        if it.get("status") not in ("pending", "todo"):
            continue
        version = it.get("version")
        if not version:
            continue
        counts[version] = counts.get(version, 0) + 1
        added = str(it.get("added_at") or "")
        # Items arrive oldest-first, so the first add_at we see per bucket is
        # its earliest pending item — record it once for stable tie-breaking.
        first_seen.setdefault(version, added)
    if not counts:
        return None
    # Most pending wins; ties go to the bucket whose earliest pending item is
    # oldest (smallest added_at) for deterministic, non-flapping scoping.
    return max(
        counts,
        key=lambda v: (counts[v], _NEG_TS(first_seen.get(v, ""))),
    )


def _NEG_TS(ts: str) -> tuple[int, str]:
    """Sort key making an EARLIER timestamp rank HIGHER in a max() tie-break.

    Empty timestamps sort last (rank lowest). Returns a tuple whose natural
    ordering is the reverse of the string order, so ``max(...)`` prefers the
    oldest item without needing a separate min pass.
    """
    if not ts:
        return (0, "")
    # 1 outranks 0 (non-empty beats empty); the inverted string makes an
    # earlier ts compare greater than a later one under default tuple ordering.
    inverted = "".join(chr(255 - min(ord(c), 255)) for c in ts)
    return (1, inverted)


async def count_pending_sprint_items(
    db: aiosqlite.Connection, project_id: str
) -> int:
    """c0d2356d — count of not-yet-done sprint items (status pending/todo) for a
    project. Backs the Stop-hook sprint guard's /sprint/pending_count endpoint."""
    async with db.execute(
        "SELECT COUNT(*) AS c FROM sprint_items "
        "WHERE project_id = ? AND status IN ('pending', 'todo')",
        (project_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return 0
    return int(row["c"] if isinstance(row, dict) else row[0])


# 43539c70 - keywords that mark a sprint item as legitimately calling for
# test/coverage work, so the test-tamper guard exempts test-file edits made under
# it. Matched case-insensitively against the item's title + notes.
_TEST_COVERAGE_KEYWORDS = (
    "test",
    "tests",
    "testing",
    "coverage",
    "regression",
    "unit test",
    "add a test",
    "write a test",
)


def _text_calls_for_test_coverage(text: str | None) -> bool:
    """True if free-text explicitly calls for test/coverage work.

    Backs the test-tamper guard's exemption: legitimate feature work that a sprint
    item asks to cover with tests should NOT be flagged as test tampering. Matched
    on whole-word ``test``/``tests``/``testing``/``coverage``/``regression`` (plus
    a couple of phrases) so an incidental substring like ``latest`` or
    ``contested`` does not trip it.
    """
    if not text:
        return False
    import re as _re  # noqa: PLC0415

    lowered = text.lower()
    for kw in _TEST_COVERAGE_KEYWORDS:
        if " " in kw:
            if kw in lowered:
                return True
        elif _re.search(rf"\b{_re.escape(kw)}\b", lowered):
            return True
    return False


async def sprint_test_coverage_expected(
    db: aiosqlite.Connection, project_id: str
) -> bool:
    """43539c70 - True if an in-progress sprint item's own text calls for
    test/coverage work.

    Powers the PostToolUse test-tamper guard's exemption endpoint. If any item the
    project currently has ``in_progress`` mentions tests/coverage in its title or
    notes, editing a test file under it is legitimate feature work, not tampering,
    so the guard stays silent. Only ``in_progress`` items are considered (the item
    the executor is actively working); done/pending items do not exempt anything.
    """
    async with db.execute(
        "SELECT title, notes FROM sprint_items "
        "WHERE project_id = ? AND status = 'in_progress'",
        (project_id,),
    ) as cur:
        rows = await cur.fetchall()
    for row in rows or []:
        if isinstance(row, dict):
            title, notes = row.get("title"), row.get("notes")
        else:
            title, notes = row[0], row[1]
        if _text_calls_for_test_coverage(title) or _text_calls_for_test_coverage(notes):
            return True
    return False


async def get_sprint_items(
    db: aiosqlite.Connection,
    project_id: str,
    status: str | None = None,
    show_blocked: bool = True,
    include_human: bool = True,
    version: str | None = None,
    include_manual_blocker: bool | None = None,
    include_deferred: bool = True,
) -> list[dict[str, Any]]:
    """List sprint items for a project, highest-priority first then oldest.

    ``status`` filter is optional. ``None`` returns everything so the
    dashboard can render the full timeline.

    ``show_blocked=False`` hides items whose ``depends_on`` parent is not
    yet in a terminal state (done/skipped/failed/pushed), or whose parent
    has failed while the item has ``failure_mode='stop'``.

    ``include_human=False`` excludes items with milestone_type='human'
    (used for executor sessions that should not see human-assigned tasks).

    ``include_manual_blocker`` (2282a636) controls whether items with
    ``blocker_kind='manual'`` (blocked on a real-world action OUTSIDE Meridian —
    distinct from milestone_type='human', which is about WHO executes) are
    returned. ``None`` (default) FOLLOWS ``include_human``: an executor-scoped
    call (``include_human=False``) also hides manual-blocker items so an executor
    never treats a real-world blocker as a claimable "just claim it" pending; a
    full-board call (``include_human=True``) still surfaces them (the dashboard
    renders them distinctly). Pass an explicit ``True``/``False`` to override.

    ``version`` (a76cb7c0) filters to a single sprint-version bucket. ``None``
    returns every version. Used by version-scoped sessions so an executor sees
    only the items in its bucket.

    ``include_deferred`` (45f519a0) controls whether items with a future
    ``deferred_until`` timestamp are returned. Default ``True`` keeps the
    existing behaviour (dashboard full-board view always shows all items).
    Pass ``False`` for executor-scoped calls (generate_handoff pending-items
    list, claim-time checks) so a deferred item is genuinely invisible to an
    executor rather than merely gated at claim time.

    Ordering (e08fee30): items are returned highest-priority first
    (urgent > high > normal > low), then oldest-first within a priority, so an
    executor reading the pending bucket claims higher-priority work first. The
    priority rank is a portable CASE expression (identical on SQLite/Postgres).
    """
    # 2282a636 — resolve the manual-blocker visibility: None follows include_human.
    if include_manual_blocker is None:
        include_manual_blocker = include_human
    clauses = ["project_id = ?"]
    params_list: list = [project_id]
    if status is not None:
        if status not in _VALID_SPRINT_STATUSES:
            raise ValueError(
                f"invalid sprint-item status filter: {status!r}. "
                f"Valid: {sorted(_VALID_SPRINT_STATUSES)}"
            )
        clauses.append("status = ?")
        params_list.append(status)
    if version is not None:
        clauses.append("version = ?")
        params_list.append(version)
    if not include_human:
        clauses.append("(milestone_type IS NULL OR milestone_type != 'human')")
    if not include_manual_blocker:
        clauses.append("(blocker_kind IS NULL OR blocker_kind != 'manual')")
    query = (
        f"SELECT * FROM sprint_items WHERE {' AND '.join(clauses)} "
        f"ORDER BY {_sprint_priority_order_sql()} ASC, added_at ASC, rowid ASC"
    )
    params: tuple = tuple(params_list)
    async with db.execute(query, params) as cur:
        rows = await cur.fetchall()
    items = [_row_to_dict(r) for r in rows]  # type: ignore[misc]
    # 45f519a0 — apply post-query deferred filter. deferred_until is not a SQL
    # expression (it compares a stored timestamp against "now") so we filter in
    # Python, the same as claim_sprint_item's existing deferral gate. Fail-open:
    # unparseable values pass through (treated as not deferred).
    if not include_deferred:
        items = [it for it in items if not _is_deferred(it)]
    if show_blocked:
        return items
    # Build status lookup for dependency filtering
    _terminal = {"done", "skipped", "failed", "pushed"}
    by_id = {it["id"]: it for it in items}
    # Fetch any parents not in this result set (e.g. filtered by status)
    all_statuses: dict[str, str] = {it["id"]: it["status"] for it in items}
    missing_parents = {
        it["depends_on"] for it in items
        if it.get("depends_on") and it["depends_on"] not in all_statuses
    }
    for parent_id in missing_parents:
        parent = await get_sprint_item(db, parent_id)
        if parent:
            all_statuses[parent["id"]] = parent["status"]
    result = []
    for it in items:
        pid = it.get("depends_on")
        if not pid:
            result.append(it)
            continue
        parent_status = all_statuses.get(pid, "")
        if parent_status not in _terminal:
            continue  # blocked: parent not finished
        if parent_status == "failed" and it.get("failure_mode") == "stop":
            continue  # chain stopped
        result.append(it)
    return result


def _item_is_unprospected(it: dict[str, Any], *, enrichment_failure_only: bool = False) -> bool:
    """fba94f1a — return True when a sprint item has no prospecting evidence.

    An item is considered prospected when ANY of the following hold:
    - ``prospect_status`` is 'prospected' or 'cached' (set by
      ``_annotate_code_pointers`` in handoff.py when a real match was found
      or a prior pointer was reused).
    - ``code_pointers`` is non-empty (code-graph match attached by the handoff
      enrichment path or a prior ``claim_sprint_item`` prospecting step).
    - ``pointers`` is non-empty (generic pointer attached for non-code source
      types such as docs/citations).

    Items that were intentionally skipped (``skipped_manual`` — human/MANUAL
    items, ``skipped_cap`` — beyond the enrichment cap, ``no_backend`` — no
    searcher wired for this source type) are NOT flagged unprospected: the
    skip was deliberate and the flag would be misleading.

    By default (``enrichment_failure_only=False``), items with NO
    ``prospect_status`` at all (plain DB rows, never run through enrichment) ARE
    flagged — this is the informational mode used by ``build_sprint_items_xml``
    to surface items that lack any code grounding.

    94c26322 — When ``enrichment_failure_only=True``, only items that went
    through enrichment and received an explicit FAILURE status (``no_match``,
    ``error``, ``no_query``) AND have no pointer evidence are flagged. This
    is the mode used by the /goal generation safety gate: items that simply
    haven't been enriched yet are NOT flagged (they are in a "not yet tried"
    state rather than a confirmed-failure state). This prevents the gate from
    blocking plain test items or items added before enrichment runs.
    """
    ps = it.get("prospect_status") or ""
    # Intentional skips are not flagged (either mode).
    if ps in ("skipped_manual", "skipped_cap", "no_backend"):
        return False
    # Confirmed prospected or reused cached pointer (either mode).
    if ps in ("prospected", "cached"):
        return False
    # Non-empty code or generic pointer means real evidence exists (either mode).
    if it.get("code_pointers") or it.get("pointers"):
        return False
    # In enrichment_failure_only mode: never-enriched items (no status) are NOT flagged.
    if enrichment_failure_only and not ps:
        return False
    # Default (broad) mode: everything with no evidence is flagged.
    # enrichment_failure_only mode: ps is a failure status (no_match, error, no_query).
    return True


def _item_declares_resources(item: dict[str, Any]) -> bool:
    """d5849a67 — shared SCOPE GUARD: True iff ``item`` declared ``touches_resources``,
    i.e. it was a real prospecting candidate in the first place (something for
    ``add_sprint_item``'s inline prospecting, or a human, to have pointed at).

    Extracted so both ``claim_sprint_item`` and ``generate_handoff``'s
    ``excluded_unprospected`` list apply the IDENTICAL scope test. Before this fix,
    handoff's exclusion computation ignored ``touches_resources`` entirely, so it
    could exclude items ``claim_sprint_item`` would never gate at all (no declared
    resources) while failing to exclude items ``claim_sprint_item`` WOULD gate
    (declared resources, no durable pointer) — see ``is_item_claim_prospected``.
    """
    raw = item.get("touches_resources")
    return bool(raw) and raw not in ("[]", "null")


def is_item_claim_prospected(
    item: dict[str, Any],
    *,
    has_pointer_evidence: bool,
    strict: bool = False,
    target_resolved: "bool | None" = None,
) -> bool:
    """d5849a67 — SINGLE SOURCE OF TRUTH for "would ``claim_sprint_item``'s
    UNPROSPECTED gate let this item through?"

    Both ``claim_sprint_item`` (resolving ``has_pointer_evidence`` via a live
    single-item ``sprint_item_pointers`` count query) and ``generate_handoff``'s
    ``excluded_unprospected`` list (resolving it via a batch query across all
    pending items — see ``get_pointer_evidence_item_ids``) call this SAME
    function to make the final claimable/excluded decision. That closes the
    drift bug (d5849a67) where an item could sit outside the /goal's
    ``<excluded_unprospected>`` tag yet still have ``claim_sprint_item`` refuse
    it as UNPROSPECTED: the two call sites previously used different signals
    (transient enrichment-time ``code_pointers``/``pointers``/``prospect_status``
    fields in handoff vs. the durable ``sprint_item_pointers`` table in claim).

    Returns True (claimable / not excluded) when ANY of:
    - ``prospect_bypass`` is explicitly set on the item (human override).
    - the item declared NO ``touches_resources`` — it was never a real
      prospecting candidate (see ``_item_declares_resources``).
    - ``has_pointer_evidence`` is True — the item has >=1 durable row in
      ``sprint_item_pointers``, the ONLY evidence ``claim_sprint_item`` actually
      checks. This is deliberately NOT the same as the transient, in-memory-only
      ``code_pointers``/``pointers``/``prospect_status`` fields that
      ``_annotate_code_pointers`` (handoff.py) attaches during handoff
      generation — those are best-effort title-match guesses that are never
      persisted to ``sprint_item_pointers``, so an item showing
      ``prospect_status='prospected'`` can still have zero durable rows and
      would still be refused by ``claim_sprint_item``.

    ``strict`` / ``target_resolved`` (eb8b6894) — OPT-IN, OFF by default
    (mirrors ``generate_handoff``'s own ``strict_evidence``/8a883f60 opt-in
    shape). ``has_pointer_evidence`` alone answers "does a row exist" —
    PRESENCE, not RESOLUTION (see ``get_pointer_evidence_item_ids``'s own
    docstring: it is presence-only BY DESIGN). When a caller has ALSO
    resolved the item's pointers (e.g. ``handoff._annotate_resolved_pointers``
    via ``pointers.aggregate_pointer_evidence``) and passes both
    ``strict=True`` and the resulting ``target_resolved`` bool, a pointer
    that is structurally present but explicitly did NOT resolve
    (``target_resolved is False``) now FAILS this gate too — "a row exists"
    can no longer, by itself, satisfy a strict caller. ``target_resolved is
    None`` (the caller didn't compute a resolution-aware signal — every
    existing call site, and every call that leaves ``strict`` at its
    default) is treated exactly like ``strict=False``: nothing tightens,
    zero behaviour change from before these kwargs existed. A caller that
    never passes ``strict``/``target_resolved`` — i.e. every pre-existing
    call site — sees byte-for-byte identical results.
    """
    if bool(item.get("prospect_bypass")):
        return True
    if not _item_declares_resources(item):
        return True
    if not bool(has_pointer_evidence):
        return False
    if strict and target_resolved is False:
        return False
    return True


async def get_pointer_evidence_item_ids(
    db: aiosqlite.Connection, item_ids: "list[str] | set[str] | None"
) -> "set[str] | None":
    """d5849a67 — batch-resolve which of ``item_ids`` have >=1 durable row in
    ``sprint_item_pointers``, so ``generate_handoff`` can call
    ``is_item_claim_prospected`` with the SAME evidence signal
    ``claim_sprint_item`` checks per-item at claim time.

    Returns ``None`` (NOT an empty set) on any DB error — a query failure must
    never be mistaken for "confirmed no evidence" and mass-exclude every
    pending item from the /goal. Callers should treat ``None`` as "unknown,
    fail open" (``is_item_claim_prospected`` is called with
    ``has_pointer_evidence=True`` in that case), mirroring
    ``claim_sprint_item``'s own try/except fail-open behaviour.

    eb8b6894 — this function is, and stays, PRESENCE-ONLY BY DESIGN: a plain
    SQL existence check (a durable row is in ``sprint_item_pointers``), no
    :func:`meridian.pointers.resolve_pointer` call, no live-graph/tunnel
    reach — that would require awaiting an async resolve per item from a
    low-level DB helper with no tenant/symbol-resolver context available
    here. It is intentionally NOT extended to answer "did the target
    actually resolve" — that is a SEPARATE, RESOLUTION-aware signal, computed
    one layer up where the resolve machinery already runs:
    ``handoff._annotate_resolved_pointers`` resolves every stored pointer via
    :func:`meridian.pointers.resolve_pointer` and rolls the per-pointer
    result up via :func:`meridian.pointers.aggregate_pointer_evidence` into
    each item's ``pointer_resolution_status["target_resolved"]`` — the
    companion check a caller passes to ``is_item_claim_prospected(strict=True,
    target_resolved=...)`` to close exactly the presence-vs-resolution gap
    this function's own docstring calls out. This remains the ONE
    presence-only source; it is deliberately paired with, not silently
    conflated with, that resolution-aware companion.
    """
    ids = [i for i in (item_ids or []) if i]
    if not ids:
        return set()
    try:
        placeholders = ", ".join("?" for _ in ids)
        async with db.execute(
            "SELECT DISTINCT sprint_item_id FROM sprint_item_pointers "
            f"WHERE sprint_item_id IN ({placeholders})",
            tuple(ids),
        ) as cur:
            rows = await cur.fetchall()
        return {
            _sid for r in rows if r
            for _sid in [(_row_to_dict(r) or {}).get("sprint_item_id")]
            if _sid
        }
    except Exception:  # noqa: BLE001 — batch fetch is best-effort, never fatal
        return None


def build_sprint_items_xml(items: list[dict[str, Any]]) -> str:
    """Serialise sprint items as a ``<sprint_items>`` XML block.

    Since v1.9x items are optionally grouped by ``item_group``. When a
    group name is set, items are wrapped in ``<group name="...">`` tags
    so cold sessions can parse the board structure. Items without a
    group are emitted at the top level (ungrouped) before any groups.

    Mirrors the get_goal XML envelope (v0.6.1) so cold sessions render
    the checklist alongside the goal text in a single prompt.

    fba94f1a — items with no prospecting evidence (no code_pointers,
    no pointers, no confirmed prospect_status) gain an
    ``unprospected="true"`` attribute so an executor reading the /goal
    can see explicitly which items lack real code grounding, rather than
    silently treating a guess the same as a confirmed finding.
    """
    from xml.sax.saxutils import escape, quoteattr
    from collections import OrderedDict

    # Preserve insertion order: ungrouped first, then named groups in
    # first-seen order.
    groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for it in items:
        g = it.get("item_group") or ""
        if g not in groups:
            groups[g] = []
        groups[g].append(it)

    out = ['<sprint_items cache="false">']
    for group_name, group_items in groups.items():
        if group_name:
            out.append(f'  <group name={quoteattr(group_name)}>')
        for it in group_items:
            ver = quoteattr(it.get("version") or "")
            status = quoteattr(it.get("status") or "todo")
            iid = quoteattr(it.get("id") or "")
            title = escape(it.get("title") or "")
            pushed_to = it.get("pushed_to")
            attrs = f"id={iid} version={ver} status={status}"
            if pushed_to:
                attrs += f" pushed_to={quoteattr(str(pushed_to))}"
            # fba94f1a — emit unprospected="true" for items with no code grounding
            # so an executor can distinguish confirmed findings from pure guesswork.
            if _item_is_unprospected(it):
                attrs += ' unprospected="true"'
            indent = "    " if group_name else "  "
            out.append(f"{indent}<item {attrs}>{title}</item>")
        if group_name:
            out.append("  </group>")
    out.append("</sprint_items>")
    return "\n".join(out)


def collapse_sprint_item_clusters(
    items: list[dict[str, Any]],
    expand: bool = False,
) -> list[dict[str, Any]]:
    """9d8e858c — collapse item_group/parent_id clusters into summary rows.

    Shared by get_sprint_items, get_planning_brief, and search_all so a
    caller browsing the board sees one line per logical cluster of work
    instead of every fanned-out subtask/grouped item individually. Reuses
    the existing ``parent_id`` (set by :func:`add_subtask`/:func:`split_sprint_item`)
    and ``item_group`` (set by :func:`add_sprint_item`'s ``group`` arg) columns —
    no new schema.

    ``expand=True`` returns ``items`` unchanged (today's full-detail shape) —
    this is the backward-compatible default callers relied on before this
    item, so any pre-existing caller that explicitly wants the raw list can
    still get it.

    ``expand=False`` (the new default) groups items by ``parent_id`` if set,
    else by ``item_group`` if set. Items with neither field, and clusters
    that only contain a single item, pass through unchanged. Clusters with
    2+ items collapse into ONE summary row:
    ``{"collapsed": True, "cluster_kind": "parent_id"|"item_group",
    "item_group_or_parent": <the shared id/name>, "count": N, "done": X,
    "description": "<first item's title, truncated>", "ids": [...]}``
    where ``X`` is how many of the N items have ``status == "done"``.

    Relative ordering is preserved: a standalone item or the first-seen
    cluster keeps its original position among the returned rows.
    """
    if expand:
        return items

    from collections import OrderedDict  # noqa: PLC0415

    def _cluster_key(it: dict[str, Any]) -> tuple[str, Any] | None:
        pid = it.get("parent_id")
        if pid:
            return ("parent_id", pid)
        grp = it.get("item_group")
        if grp:
            return ("item_group", grp)
        return None

    # OrderedDict keyed by the cluster key (or a unique per-item sentinel for
    # standalone items) so first-seen order is preserved across the mix of
    # collapsed summaries and pass-through items.
    buckets: OrderedDict[Any, list[dict[str, Any]]] = OrderedDict()
    for idx, it in enumerate(items):
        key = _cluster_key(it)
        if key is None:
            key = ("__standalone__", idx)
        buckets.setdefault(key, []).append(it)

    result: list[dict[str, Any]] = []
    for key, group_items in buckets.items():
        if key[0] == "__standalone__" or len(group_items) < 2:
            result.extend(group_items)
            continue
        cluster_kind, cluster_id = key
        done_count = sum(1 for it in group_items if it.get("status") == "done")
        description = (group_items[0].get("title") or "").strip()
        if len(description) > 80:
            description = description[:77] + "..."
        result.append({
            "collapsed": True,
            "cluster_kind": cluster_kind,
            "item_group_or_parent": cluster_id,
            "count": len(group_items),
            "done": done_count,
            "description": description,
            "ids": [it.get("id") for it in group_items],
        })
    return result


# ---------------------------------------------------------------------------
# SECTION 4: Lines 7070-7151 — sprint item pointers
# ---------------------------------------------------------------------------

async def add_sprint_item_pointer(
    db: aiosqlite.Connection,
    project_id: str,
    sprint_item_id: str,
    source_type: str,
    targets: list[dict[str, Any]],
    label: str | None = None,
) -> dict[str, Any]:
    """2976e168 — persist a GENERIC POINTER on a sprint item; return the stored row.

    Validates the
    ``{source_type, targets:[{uri, selector, subSelector?, target_kind?}], label?}``
    shape via :mod:`meridian.pointers` (raising ``ValueError`` on a malformed
    pointer BEFORE any write), serializes ``targets`` to the JSON column, and
    inserts one ``sprint_item_pointers`` row. ``targets`` is an ARRAY (native
    multi-file); the composite shape is stored as JSON, NOT per-domain columns.
    The returned dict is the deserialized pointer (targets back as a list).

    ``target_kind`` (300a063d) — per-target ``"existing"`` (default) |
    ``"planned_new"``. When a caller EXPLICITLY marks a target
    ``target_kind: "existing"`` on a local-path-looking uri, validation
    verifies the path is actually present on disk and raises ``ValueError`` if
    not — closing the gap where a new-file item could point at a nonexistent
    path and be indistinguishable from real, verified prospecting.
    ``planned_new`` explicitly opts out of that check. Omitting ``target_kind``
    (the pre-300a063d shape) is unaffected: it normalizes to ``"existing"`` in
    the stored row but is never filesystem-checked, so no pointer written
    before this field existed is retroactively invalidated.

    psycopg3: ``?`` placeholders are converted to ``%s`` by the adapter; the
    shared connection is autocommit on Postgres, and ``commit()`` is a real
    flush on aiosqlite.

    86e4ae44 — this function is left untouched deliberately: it is already a
    clean validate-then-insert single-entry primitive, so
    :mod:`meridian.db.batch_management`'s shared batch engine calls it
    directly, as-is, as its ``sprint_item_pointer`` entry kind's atomic
    mutation step (with a compensating ``delete_sprint_item_pointer`` call
    for rollback on an ``all_or_nothing`` batch failure). The "routing" the
    86e4ae44 acceptance criteria ask for happens with the engine consuming
    this function, not the other way around — no wrapper needed here.
    """
    from ..pointers import (  # noqa: PLC0415 — avoid an import cycle at module load
        validate_pointer,
        serialize_targets,
        row_to_pointer,
    )

    normalized = validate_pointer(
        {"source_type": source_type, "targets": targets, "label": label}
    )
    pid = _new_id()
    targets_json = serialize_targets(normalized["targets"])
    await db.execute(
        "INSERT INTO sprint_item_pointers "
        "(id, project_id, sprint_item_id, source_type, targets, label) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            pid, project_id, sprint_item_id,
            normalized["source_type"], targets_json, label,
        ),
    )
    await db.commit()
    async with db.execute(
        "SELECT * FROM sprint_item_pointers WHERE id = ?", (pid,)
    ) as cur:
        row = await cur.fetchone()
    return row_to_pointer(_row_to_dict(row) or {})


async def get_sprint_item_pointers(
    db: aiosqlite.Connection, sprint_item_id: str
) -> list[dict[str, Any]]:
    """2976e168 — return all pointers on a sprint item, ordered by id ASC.

    Each row's JSON ``targets`` column is deserialized back into a list, so the
    caller gets the full pointer shape plus its id / source_type / label /
    created_at.

    Ordering note: we sort by ``id`` only (not ``created_at``) so that the
    result order is byte-stable on *both* SQLite (second-granularity
    ``datetime('now')``, where two inserts within one second produce identical
    timestamps) and Postgres (microsecond ``clock_timestamp()``, where
    ``created_at`` is never tied).  Using only ``id ASC`` gives a single
    deterministic sort key across both dialects; tests can assert
    ``ids == sorted(ids)`` unconditionally.
    """
    from ..pointers import row_to_pointer  # noqa: PLC0415
    async with db.execute(
        "SELECT * FROM sprint_item_pointers WHERE sprint_item_id = ? "
        "ORDER BY id ASC",
        (sprint_item_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [row_to_pointer(_row_to_dict(r) or {}) for r in rows if r is not None]


async def delete_sprint_item_pointer(
    db: aiosqlite.Connection, pointer_id: str
) -> bool:
    """2976e168 — delete one pointer by id. Return True if a row was removed."""
    async with db.execute(
        "SELECT 1 FROM sprint_item_pointers WHERE id = ?", (pointer_id,)
    ) as cur:
        existed = await cur.fetchone() is not None
    await db.execute(
        "DELETE FROM sprint_item_pointers WHERE id = ?", (pointer_id,)
    )
    await db.commit()
    return existed


# ---------------------------------------------------------------------------
# SECTION 5: Lines 8251-8270 — resource-to-sprint-item lookup
# ---------------------------------------------------------------------------

async def get_sprint_items_for_resource(
    db: aiosqlite.Connection, project_id: str, resource_id: str
) -> list[dict[str, Any]]:
    """Return sprint items whose touches_resources includes resource_id.

    f5f2a89d — reverse lookup used by the dashboard chip popover.
    Candidates are pre-filtered with LIKE, then confirmed with parse_touches_resources
    so inferred: markers and case don't produce false positives.
    """
    pattern = f"%{resource_id}%"
    async with db.execute(
        "SELECT * FROM sprint_items WHERE project_id = ? AND touches_resources LIKE ?",
        (project_id, pattern),
    ) as cur:
        rows = await cur.fetchall()
    items = [_row_to_dict(r) for r in (rows or []) if r]
    return [
        it for it in items
        if resource_id in parse_touches_resources(it.get("touches_resources"))
    ]


# ---------------------------------------------------------------------------
# SECTION 6: Lines 8478-8807 — parallelism and analysis
# ---------------------------------------------------------------------------

def _is_manual_sprint_item(item: dict[str, Any]) -> bool:
    """5a85a78f — True for items only a human can carry out; mirrors the EXACT
    semantics of ``handoff._is_manual_sprint_item`` (943afe1e).

    These items must be excluded from executor-facing eligible/parallel lists
    (``get_parallelizable_groups``, ``analyze_sprint``) — an AI cannot do them as
    intended and may fake-complete them under completion pressure.

    Three signals (any one is sufficient):
    * ``blocker_kind == 'manual'`` — blocked on a real-world action outside Meridian,
    * ``milestone_type == 'human'`` — execution is intentionally human-only,
    * a ``MANUAL``-tagged title (case-insensitive leading ``MANUAL``).

    943afe1e — does NOT key on ``human_id`` alone. ``human_id`` records *who* an
    item is assigned to, not *whether* it requires human execution. A BUG:/FIX:/FEAT:
    item assigned to a maintainer via ``human_id`` is still executor-claimable.

    NOTE: This helper MUST be kept in sync with ``meridian.handoff._is_manual_sprint_item``
    (db/__init__.py is imported BY handoff.py, so we cannot import handoff here
    without a circular import — hence the deliberate duplication).
    """
    if not isinstance(item, dict):
        return False
    if item.get("blocker_kind") == "manual" or item.get("milestone_type") == "human":
        return True
    return (item.get("title") or "").lstrip().upper().startswith("MANUAL")


# ---------------------------------------------------------------------------
# dcfbe55c — macro-wave projection: presentation/orchestration layer ONLY.
#
# get_parallelizable_groups' greedy coloring can legitimately produce many
# conflict-free groups (8-10+ on a busy sprint) — every one of them IS
# genuinely safe to fan out, but surfacing that many "batch N" waves in a
# human/executor-facing /goal is confusing to read and easy to lose track of
# mid-run. pack_groups_into_macro_waves does NOT change what is safe: it
# never merges two groups' items into one flat parallel set (that would be an
# unsafe claim-safety waiver), and claim_sprint_item's real resource-lock
# enforcement is completely independent of this projection and remains the
# actual safety mechanism regardless of how the /goal chooses to display
# things. It only decides how many DISPLAY buckets ("macro waves") the
# already-proven-safe, already-ordered groups are chunked into, preserving
# each original group as an ordered sub-batch within its macro wave.
# ---------------------------------------------------------------------------

MACRO_WAVE_COUNT_DEFAULT = 3
MACRO_WAVE_COUNT_MIN = 1
MACRO_WAVE_COUNT_MAX = 3


def _clamp_macro_wave_count(requested: Any) -> int:
    """Coerce a requested macro-wave cap into the supported [1, 3] range.

    Never raises: missing/non-numeric/out-of-range values fall back to the
    default (3) or clamp to the nearest bound, matching every other
    normalize_*/``_xxx_from_settings`` helper's fail-safe convention
    elsewhere in this codebase (e.g. ``executor_config.py``).
    """
    try:
        n = int(requested) if requested is not None else MACRO_WAVE_COUNT_DEFAULT
    except (TypeError, ValueError):
        return MACRO_WAVE_COUNT_DEFAULT
    return max(MACRO_WAVE_COUNT_MIN, min(MACRO_WAVE_COUNT_MAX, n))


def pack_groups_into_macro_waves(
    groups: list[list[dict[str, Any]]],
    requested_macro_wave_count: Any = MACRO_WAVE_COUNT_DEFAULT,
) -> list[dict[str, Any]]:
    """dcfbe55c — pack conflict-free ``groups`` into at most N macro-waves.

    ``groups`` is exactly get_parallelizable_groups' own ``"groups"`` list —
    each element already proven internally conflict-free by the greedy
    coloring above. This function never inspects or recomputes resource
    conflicts; it purely chunks the list for display.

    Algorithm: CONTIGUOUS chunking (not round-robin), so the incoming group
    order — highest-priority-first, see get_parallelizable_groups' e08fee30
    note — is preserved both within a macro wave and across macro waves. A
    round-robin scheme would scramble that ordering and could put a
    high-priority group behind a low-priority one in the rendered sequence.

    When ``len(groups) <= N`` (the common case — most boards don't have more
    conflict-free groups than the cap), every group already gets its own
    macro wave: a no-op projection, nothing to compress, and the resulting
    list is the same length/order as ``groups`` itself.

    Returns a list of ``{"batches": [group, ...], "batch_count": int,
    "item_count": int}`` dicts, one per non-empty macro wave, at most
    ``requested_macro_wave_count`` (clamped to [1, 3]) entries long. Each
    ``batches`` entry IS one of the original ``groups`` elements (same list
    object, not a copy) so a caller can cross-reference by identity/content
    without re-deriving anything.
    """
    if not groups:
        return []
    n = _clamp_macro_wave_count(requested_macro_wave_count)
    if len(groups) <= n:
        return [
            {"batches": [g], "batch_count": 1, "item_count": len(g)}
            for g in groups
        ]
    macro_waves: list[dict[str, Any]] = []
    base, extra = divmod(len(groups), n)
    idx = 0
    for wi in range(n):
        size = base + (1 if wi < extra else 0)
        if size <= 0:
            continue
        chunk = groups[idx: idx + size]
        idx += size
        macro_waves.append({
            "batches": chunk,
            "batch_count": len(chunk),
            "item_count": sum(len(b) for b in chunk),
        })
    return macro_waves


def _is_legacy_file_symbol_shorthand(resource: str) -> bool:
    """6b3b2c0e — True when a ``file:`` resource id uses the legacy
    single-colon ``file:<path>:<symbol>`` shorthand (2a176d6d's accepted
    "preferred form" per the SYMBOL_SCOPE_HINT hint in meridian.mcp.handler)
    rather than a plain ``file:<path>``.

    Delegates entirely to :func:`_resource_file_of` — the ONE place that
    already knows how to tell the two apart (including the Windows
    drive-letter exemption, ``file:C:/repo/x.py``) for scheduler conflict
    comparison (63b030a6/2a176d6d) — so this classification can never drift
    from the real-file resolution the scheduler already relies on. False for
    a non-``file:`` resource.
    """
    if not resource.startswith("file:"):
        return False
    return _resource_file_of(resource) != resource[len("file:"):]


def _predict_resource_granularity(resource: str) -> str:
    """2a176d6d — STATIC, planning-time classification of one normalized
    ``touches_resources`` entry, based purely on its string shape (never an
    actual claim attempt — ``get_parallelizable_groups`` runs before any
    worker launches, so no ``resource_contents``/file existence is known
    yet). Distinct from :func:`_claim_batch_resource`'s ``claim_granularity``,
    which classifies the ACTUAL outcome of a real claim attempt at batch-
    claim time; this is a cheaper, earlier prediction so an orchestrator can
    see a malformed declaration before it ever tries to launch a worker.

    Returns one of:
      * ``"file"``   — a well-formed ``file:<path>`` resource.
      * ``"file_legacy_symbol_suffix"`` (6b3b2c0e) — the single-colon
        ``file:<path>:<symbol>`` shorthand (see
        :func:`_is_legacy_file_symbol_shorthand`). Still resolves, for
        LOCKING purposes, to the whole real file ``<path>`` — this
        classification exists purely so a caller can SEE that a resource
        used the coarse legacy shape instead of the canonical
        ``symbol:<path>::<name>`` double-colon form, rather than that fact
        being invisible until something goes wrong at claim time.
      * ``"symbol"`` — a well-formed ``symbol:<path>::<name>`` resource.
      * ``"malformed_symbol"`` — a bare ``symbol:<name>`` with no ``::`` file
        scope (finding 3's zero-lock case) — will acquire NO lock at claim
        time no matter what, so it can never be symbol-safe.
      * ``"other"``  — any other typed resource (``db:``, ``route:``, ...).
    """
    if resource.startswith("file:"):
        return "file_legacy_symbol_suffix" if _is_legacy_file_symbol_shorthand(resource) else "file"
    if resource.startswith("symbol:"):
        value = resource[len("symbol:"):]
        _path, sep, _sym = value.partition("::")
        return "symbol" if (sep and _sym and _path) else "malformed_symbol"
    return "other"


# ---------------------------------------------------------------------------
# 0d0cada7 — lease-local scheduler diagnostics.
#
# get_parallelizable_groups already recomputes ``groups``/``blocked``/``running``
# fresh from the live board on EVERY call (nothing about it is a persisted,
# staleness-prone wave plan) — that part of the lease-local contract already
# held before this item. What was missing: (1) a deterministic digest a caller
# can compare across two calls to detect "the board moved under me" (used by
# claim_parallel_batch's new ``plan_generation`` staleness check below), and
# (2) visibility into WHY an otherwise-eligible item can't actually be claimed
# right now — a declared resource may be genuinely held by another live
# session even though nothing in get_parallelizable_groups' own conflict-graph
# coloring says so (that coloring only proves the RETURNED batch is internally
# disjoint; it never cross-checks against locks already held by unrelated
# in-flight work). Surfacing that here is what lets an executor poll with
# bounded backoff and emit a structured blocker instead of escalating to a
# native clarification (see request_hitl's new ``blocker_context`` and
# meridian/handoff.py's ``_build_scheduler_lease_clause``).
# ---------------------------------------------------------------------------


def _compute_plan_generation(entries: list[tuple[str, ...]]) -> str:
    """Deterministic digest over a board-state snapshot for staleness detection.

    ``entries`` is any list of plain-string tuples describing the relevant
    slice of board state (item id, status, claimed_at, resource set, ...).
    Sorted before hashing so caller-side ordering never perturbs the digest —
    two calls that observe the identical state always produce the identical
    digest, and any real change (a claim, a completion, a new item) changes
    it. Truncated sha256 hex: cheap to compare/log, stable across process
    restarts (no random salt, no wall-clock component).
    """
    normalized = sorted(entries)
    blob = "\n".join("|".join(t) for t in normalized)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _seconds_until(
    expiry: Any, *, default: int = 60, minimum: int = 15, maximum: int = 300
) -> int:
    """Best-effort bounded-backoff hint (seconds) from an ``expires_at`` value
    of unknown shape (TEXT on SQLite, TIMESTAMPTZ on Postgres — locks.py's own
    cross-adapter notes apply here too). Never raises: an unparsable/missing
    value falls back to ``default``. Always clamped to ``[minimum, maximum]``
    so a caller never gets an unbounded, zero, or negative retry hint — this
    is what keeps a poll loop a BOUNDED backoff rather than a busy spin or an
    indefinite wait.
    """
    if expiry is None:
        return default
    try:
        if isinstance(expiry, str):
            dt = datetime.strptime(expiry[:19], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        elif isinstance(expiry, datetime):
            dt = expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)
        else:
            return default
        remaining = int((dt - datetime.now(timezone.utc)).total_seconds())
        return max(minimum, min(maximum, remaining)) if remaining > 0 else minimum
    except (ValueError, TypeError):
        return default


async def _live_resource_holder(
    db: aiosqlite.Connection, resource: str
) -> dict[str, Any] | None:
    """Read-only: who (if anyone) currently holds ``resource`` right now.

    Cross-checks a declared ``touches_resources`` entry against the REAL lock
    tables (file_locks / file_symbol_claims / resource_locks), independent of
    get_parallelizable_groups' own conflict-graph coloring (which only proves
    the items IT returns together are pairwise disjoint from each other — it
    has no visibility into locks held by work outside that batch, e.g. an
    already in_progress item). Returns ``None`` when the resource is free, or
    ``{"holder_session_id", "lease_expiry", "claim_granularity"}`` when held.
    Mirrors the file⊃symbol hierarchy claim_symbol/claim_file already enforce:
    a whole-file lock blocks every symbol in that file too.

    6b3b2c0e — the ``file:`` branch resolves through :func:`_resource_file_of`,
    the SAME canonical real-file identity the scheduler's conflict coloring
    (63b030a6/2a176d6d) already uses, instead of the raw
    ``resource[len("file:"):]`` suffix. Without this, a legacy single-colon
    ``file:<path>:<symbol>`` declaration (2a176d6d's accepted "preferred
    form") checked liveness against a fabricated, per-declaration-unique key
    ("<path>:<symbol>") that no other claim ever writes to — so this function
    silently reported the resource as FREE even when the real file <path> was
    genuinely held by another live session (the confirmed 6b3b2c0e planning
    gap: scheduler prediction and claim-time enforcement disagreed on what
    the resource actually was).
    """
    from meridian.db import get_file_claims, get_symbol_claims, get_resource_claims  # noqa: PLC0415

    if resource.startswith("file:"):
        file_path = _resource_file_of(resource) or resource[len("file:"):]
        claims = await get_file_claims(db, file_path)
        lock = claims.get("file_lock")
        if lock and lock.get("session_id"):
            return {
                "holder_session_id": lock.get("session_id"),
                "lease_expiry": lock.get("expires_at"),
                "claim_granularity": "file",
            }
        return None

    if resource.startswith("symbol:"):
        value = resource[len("symbol:"):]
        file_path, sep, symbol_name = value.partition("::")
        if not sep or not symbol_name or not file_path:
            return None  # malformed — nothing resolvable to check
        claims = await get_file_claims(db, file_path)
        lock = claims.get("file_lock")
        if lock and lock.get("session_id"):
            # file ⊃ symbol: a whole-file lock blocks every symbol in it too.
            return {
                "holder_session_id": lock.get("session_id"),
                "lease_expiry": lock.get("expires_at"),
                "claim_granularity": "file",
            }
        for c in await get_symbol_claims(db, file_path):
            if c.get("symbol_name") == symbol_name and c.get("session_id"):
                return {
                    "holder_session_id": c.get("session_id"),
                    # file_symbol_claims carries no TTL column (heartbeat-bound
                    # only — see locks.py's _CLAIM_LIVE_HOURS) so there is no
                    # real expires_at to surface; explicit None rather than a
                    # fabricated timestamp.
                    "lease_expiry": None,
                    "claim_granularity": "symbol",
                }
        return None

    claims = await get_resource_claims(db, resource)
    lock = claims.get("resource_lock")
    if lock and lock.get("session_id"):
        return {
            "holder_session_id": lock.get("session_id"),
            "lease_expiry": lock.get("expires_at"),
            "claim_granularity": "n/a",
        }
    return None


async def _plan_generation_entries(
    db: aiosqlite.Connection,
    items: list[tuple[str, str, str, list[str]]],
    *,
    holder_cache: dict[str, "dict[str, Any] | None"] | None = None,
) -> list[tuple[str, ...]]:
    """Build the ``(item_id, status, claimed_at, holder_tagged_resources)``
    tuples :func:`_compute_plan_generation` hashes for BOTH
    get_parallelizable_groups (the digest a caller reads) and
    claim_parallel_batch (the digest it independently recomputes to check
    staleness) — factored out so the two can never drift into incompatible
    formats.

    Deliberately cross-checks each declared resource's REAL live holder (not
    just the item's own ``status``/``claimed_at``/``resources`` columns) so
    the digest changes when a totally different in_progress item's claim
    changes the picture too — a bare item-row digest would miss exactly the
    2026-08-05 incident shape: item A's own row never changes while an
    unrelated item B quietly holds a resource A also declares.

    ``items`` is ``[(item_id, status, claimed_at, resources), ...]``.
    ``holder_cache`` lets a caller that already looked up some resources
    (e.g. get_parallelizable_groups' own resource_blocked pass) reuse those
    lookups instead of re-querying; new lookups this call makes are written
    back into the SAME dict when one is supplied, so a caller can pass an
    empty dict in and inspect it afterward too.
    """
    cache = holder_cache if holder_cache is not None else {}
    entries: list[tuple[str, ...]] = []
    for iid, status, claimed_at, resources in items:
        tags: list[str] = []
        for res in resources:
            if res not in cache:
                cache[res] = await _live_resource_holder(db, res)
            holder = cache[res]
            tags.append(f"{res}={(holder or {}).get('holder_session_id') or ''}")
        entries.append((iid, status, claimed_at, ",".join(sorted(tags))))
    return entries


async def get_parallelizable_groups(
    db: aiosqlite.Connection,
    project_id: str,
    version: str | None = None,
    *,
    configured_target: int | None = None,
    host_limit: int | None = None,
    requested_parallelism: int | None = None,
    requested_macro_wave_count: Any = MACRO_WAVE_COUNT_DEFAULT,
) -> dict[str, Any]:
    """255096d9 — cluster pending sprint items that are safe to run in parallel.

    Algorithm:
      1. Take pending/todo items (optionally filtered to ``version``) whose
         ``depends_on`` is satisfied (no parent, or parent is done — or parent
         failed with failure_mode='continue'). Items still waiting on a parent
         are returned separately under ``blocked``.
      2. Build a conflict graph: two eligible items conflict when their
         ``touches_resources`` sets intersect (see 501ec93f). An item with no
         declared resources conflicts with nothing (empty ∩ anything = ∅).
      3. Greedy first-fit coloring partitions the items into groups such that no
         two items *within a group* share a resource — so every group is a batch
         the orchestrator can fan out simultaneously, and successive groups run
         in sequence.

    Returns ``{"version", "groups": [[item, ...], ...], "group_count",
    "eligible_count", "blocked": [...], "undeclared_count", "requested_parallelism",
    "effective_parallelism", "host_limit", "configured_target",
    "resource_safe_capacity", "limiting_reason", "macro_waves",
    "macro_wave_count", "requested_macro_wave_count"}``. ``groups`` items
    are full sprint-item dicts with a derived ``resources`` list attached.

    2282a636 — items with ``blocker_kind='manual'`` (blocked on a real-world
    action outside Meridian) are excluded here: they are not executor-claimable,
    so they never join a parallel batch.
    5a85a78f — items matching :func:`_is_manual_sprint_item` (blocker_kind='manual',
    milestone_type='human', or MANUAL-tagged title) are also excluded: milestone_type
    was previously passed through because ``get_sprint_items`` only gates on
    ``include_manual_blocker``, not on ``milestone_type='human'`` or title prefix.
    e08fee30 — within the safe-parallel ordering, higher-priority eligible items
    are placed first so urgent work colors into the earliest groups.

    99c0c1be — the return dict also carries deterministic PARALLELISM
    diagnostics computed by :func:`meridian.executor_config.resolve_parallelism`
    against the first (largest, resource-conflict-free) group:
    ``requested_parallelism`` (defaults to the first group's size unless the
    ``requested_parallelism`` kwarg is passed), ``configured_target`` (defaults
    to this project's persisted ``executor_config.parallelism_target`` unless
    the ``configured_target`` kwarg overrides it — so callers such as
    ``handoff.py`` that invoke this function unmodified automatically pick up
    a project's configured target), ``host_limit`` (``None`` unless the
    caller explicitly passes one — an unknown host limit is NEVER invented
    here, so wave planning never serializes disjoint, resource-safe work just
    because an unrelated vendor UI cap is unreported), ``resource_safe_capacity``
    (the first group's size), ``effective_parallelism`` and ``limiting_reason``.
    This is pure diagnostics: it does not change which items land in which
    ``groups`` — the coloring algorithm below is unchanged.

    ``requested_macro_wave_count`` (dcfbe55c, default 3, clamped to [1, 3])
    controls ``macro_waves``: a deterministic, PRESENTATION-ONLY packing of
    ``groups`` into at most that many display waves via
    :func:`pack_groups_into_macro_waves` — see that function's docstring for
    the full contract. It is NOT a claim-safety waiver; ``groups`` (the real
    conflict-free partition) is returned unchanged regardless of this cap,
    and claim_sprint_item's resource-lock enforcement never consults
    ``macro_waves`` at all.
    """
    # include_manual_blocker=False: a manual-blocker item is not claimable work,
    # so it must not be offered as a parallelizable batch member.
    items = await get_sprint_items(db, project_id, include_manual_blocker=False)
    # 5a85a78f — also filter out milestone_type='human' and MANUAL-titled items;
    # get_sprint_items only gates on blocker_kind, not the other two manual signals.
    items = [it for it in items if not _is_manual_sprint_item(it)]
    if version is not None:
        items = [it for it in items if it.get("version") == version]
    claimable_statuses = {"pending", "todo"}
    eligible: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    # df573218 — surface currently-claimed work so an orchestrator sees the live
    # parallelism state (and knows an item it planned was grabbed by another).
    running: list[dict[str, Any]] = []
    for it in items:
        if it.get("status") == "in_progress" or (
            (it.get("status") or "pending") in claimable_statuses and it.get("claimed_at")
        ):
            running.append({
                "id": it["id"],
                "title": it.get("title", ""),
                "status": it.get("status"),
                "claimed_at": it.get("claimed_at"),
            })
        if (it.get("status") or "pending") not in claimable_statuses:
            continue
        if it.get("claimed_at"):
            continue  # already in flight
        parent_block = await get_blocking_dependency_for_sprint_item(db, it["id"])
        if parent_block is not None:
            # Parent failed + this item's failure_mode='continue' → still runnable.
            if (
                parent_block.get("status") == "failed"
                and (it.get("failure_mode") or "continue") == "continue"
            ):
                pass
            else:
                blocked.append({
                    "id": it["id"],
                    "title": it.get("title", ""),
                    "depends_on": it.get("depends_on"),
                    "blocked_by_status": parent_block.get("status"),
                })
                continue
        _res = parse_touches_resources(it.get("touches_resources"))
        enriched = {
            **it,
            "resources": _res,
            # 2a176d6d — additive: static per-resource granularity prediction
            # (see _predict_resource_granularity). Does not affect coloring —
            # the conflict graph below still colors purely on `resources`.
            "predicted_granularity": {r: _predict_resource_granularity(r) for r in _res},
        }
        eligible.append(enriched)
    # Stable order: highest-priority first (e08fee30), then oldest, then id, so
    # coloring is deterministic AND urgent work colors into the earliest groups.
    eligible.sort(
        key=lambda it: (
            _SPRINT_PRIORITY_RANK.get(
                it.get("priority") or "normal", _SPRINT_PRIORITY_DEFAULT_RANK
            ),
            str(it.get("added_at") or ""),
            it["id"],
        )
    )
    # de730a25 — separate declared from undeclared items. An item with no
    # touches_resources is disjoint with everything, so the old single-pass
    # coloring dropped it into group 0 next to declared items and they fanned
    # out together — unsafe, because an undeclared item may genuinely conflict
    # with anything. Now: color-graph only the DECLARED items into safe parallel
    # groups, then give each UNDECLARED item its own singleton group so they run
    # sequentially (parallel safety can't be proven for them).
    declared = [it for it in eligible if it["resources"]]
    undeclared_items = [it for it in eligible if not it["resources"]]
    undeclared = len(undeclared_items)
    # Greedy first-fit graph coloring on the declared items' conflict graph.
    groups: list[list[dict[str, Any]]] = []
    group_resource_sets: list[set[str]] = []
    for it in declared:
        res = set(it["resources"])
        placed = False
        for gi, used in enumerate(group_resource_sets):
            # 63b030a6 — cross-type aware: file:X conflicts with symbol:X::*, but
            # symbol:X::a and symbol:X::b can co-schedule. (plain isdisjoint missed this)
            if not _resource_sets_conflict(res, used):
                groups[gi].append(it)
                used.update(res)
                placed = True
                break
        if not placed:
            groups.append([it])
            group_resource_sets.append(set(res))
    # Each undeclared item is its own sequential group (never co-scheduled).
    for it in undeclared_items:
        groups.append([it])

    # 99c0c1be — deterministic parallelism diagnostics for the first (largest,
    # resource-conflict-free) group. configured_target defaults to this
    # project's persisted executor_config.parallelism_target (fetched here) so
    # callers that invoke this function unmodified — e.g. handoff.py — pick up
    # a project's configured target automatically. host_limit has NO such
    # default: an unknown host limit must never be invented (see
    # executor_config.resolve_parallelism), so it stays None unless the
    # caller explicitly knows and passes one.
    if configured_target is None:
        try:
            _exec_cfg = await get_executor_config(db, project_id)
            configured_target = (_exec_cfg or {}).get("parallelism_target")
        except Exception:  # noqa: BLE001 — diagnostics must never break grouping
            configured_target = None
    _first_group_size = len(groups[0]) if groups else 0
    _requested = (
        requested_parallelism if requested_parallelism is not None else _first_group_size
    )
    _parallelism = _executor_config.resolve_parallelism(
        _requested,
        configured_target=configured_target,
        host_limit=host_limit,
        resource_safe_capacity=_first_group_size,
    )
    # dcfbe55c — presentation-only macro-wave projection; see the module-level
    # note above pack_groups_into_macro_waves. Does not affect "groups" above,
    # which remains the authoritative conflict-free partition.
    _macro_wave_cap = _clamp_macro_wave_count(requested_macro_wave_count)
    macro_waves = pack_groups_into_macro_waves(groups, _macro_wave_cap)

    # 0d0cada7 — cross-check every eligible item's declared resources against
    # REAL live locks, not just this call's own conflict-graph coloring (see
    # _live_resource_holder's docstring: the coloring only proves the batch
    # THIS call returns is internally disjoint — it has no visibility into a
    # lock already held by work outside that batch, e.g. an in_progress item
    # from an earlier wave). This is what lets a caller tell "genuinely
    # nothing to do yet" apart from "safe on paper, but a live session holds
    # the resource right now" — the latter is exactly the case where an
    # executor should poll with bounded backoff instead of escalating.
    # Shared cache: at most one live-lock lookup per DISTINCT resource
    # declared across the whole eligible set, reused below by BOTH the
    # resource_blocked diagnostic (which short-circuits at the first
    # blocking resource per item, for a readable one-line-per-item summary)
    # and the plan_generation digest (which needs EVERY resource's holder,
    # not just the first, so the digest can't miss a change to a
    # non-first resource).
    _holder_cache: dict[str, "dict[str, Any] | None"] = {}
    resource_blocked: list[dict[str, Any]] = []
    for it in eligible:
        for res in it["resources"]:
            if res not in _holder_cache:
                _holder_cache[res] = await _live_resource_holder(db, res)
            holder = _holder_cache[res]
            if holder is None:
                continue
            resource_blocked.append({
                "id": it["id"],
                "title": it.get("title", ""),
                "resource": res,
                "wait_reason": "resource_locked",
                "holder_session_id": holder.get("holder_session_id"),
                "lease_expiry": holder.get("lease_expiry"),
                "claim_granularity": holder.get("claim_granularity"),
                "retry_after": _seconds_until(holder.get("lease_expiry")),
            })
            break  # one blocking resource is enough to explain the wait

    # Deterministic digest of the state THIS call actually observed — lets a
    # caller (claim_parallel_batch's plan_generation staleness check below, or
    # an executor deciding whether to recompute) detect "the board moved since
    # I last looked" without re-diffing the whole payload by hand. Folds in
    # each resource's live HOLDER (via the same cache above), not just the
    # item's own status/claimed_at/resources columns — see
    # _plan_generation_entries' docstring for why that matters.
    _gen_entries = await _plan_generation_entries(
        db,
        [
            (it["id"], it.get("status") or "pending", str(it.get("claimed_at") or ""),
             it.get("resources") or [])
            for it in eligible
        ],
        holder_cache=_holder_cache,
    ) + [
        (r["id"], str(r.get("status") or ""), str(r.get("claimed_at") or ""), "")
        for r in running
    ]
    plan_generation = _compute_plan_generation(_gen_entries)
    # Index-aligned with "groups" — the digest of exactly one group's items,
    # in the same tuple shape claim_parallel_batch's own plan_generation
    # check recomputes, so a caller can pass group_generations[i] straight
    # through as claim_parallel_batch(..., plan_generation=...) for groups[i].
    group_generations = [
        _compute_plan_generation(await _plan_generation_entries(
            db,
            [
                (it["id"], it.get("status") or "pending", str(it.get("claimed_at") or ""),
                 it.get("resources") or [])
                for it in group
            ],
            holder_cache=_holder_cache,
        ))
        for group in groups
    ]

    return {
        "version": version,
        "groups": groups,
        "group_generations": group_generations,
        "group_count": len(groups),
        "eligible_count": len(eligible),
        "undeclared_count": undeclared,
        "blocked": blocked,
        "running": running,  # df573218 — items currently in flight
        "requested_parallelism": _parallelism["requested_parallelism"],
        "effective_parallelism": _parallelism["effective_parallelism"],
        "host_limit": _parallelism["host_limit"],
        "configured_target": _parallelism["configured_target"],
        "resource_safe_capacity": _parallelism["resource_safe_capacity"],
        "limiting_reason": _parallelism["limiting_reason"],
        "macro_waves": macro_waves,
        "macro_wave_count": len(macro_waves),
        "requested_macro_wave_count": _macro_wave_cap,
        # 0d0cada7 — lease-local scheduler diagnostics (additive; existing
        # keys/values above are all byte-for-byte unchanged).
        "resource_blocked": resource_blocked,
        "resource_blocked_count": len({b["id"] for b in resource_blocked}),
        "plan_generation": plan_generation,
        "recomputed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# 22cad9b8 — atomic batch claim: reserve an ENTIRE parallel-safe batch (every
# item's status AND every declared resource) before workers launch.
#
# get_parallelizable_groups (above) can prove a batch of items is safe to run
# in parallel — their declared touches_resources are pairwise disjoint — but
# it only COMPUTES that fact; nothing then atomically RESERVES it. Between
# "compute the safe batch" and "each worker calls claim_sprint_item for its
# item," another session can sneak in and claim one of those same resources,
# or the batch composition can go stale (an item's declared resources drift,
# or a sibling planner recolors the board). claim_parallel_batch closes that
# gap: it is the single atomic operation that turns "this batch was proven
# safe a moment ago" into "this batch is NOW reserved, or nothing changed."
#
# Design, mirroring eb2e44f8's immutable-manifest + this repo's existing
# transactional-gate conventions (18c488b6's _sprint_item_resource_claim_gate
# in meridian/mcp/handler.py, which this reuses the same acquire/rollback
# shape from, kept self-contained here rather than imported across the
# db→mcp layer boundary):
#
#   1. Validate every item exists, belongs to the project, and (for a
#      multi-item batch) has NON-EMPTY declared touches_resources — an item
#      with nothing declared can never be PROVEN parallel-safe, so it is
#      refused as part of a multi-item batch rather than silently treated as
#      conflict-free (empty ∩ anything == ∅ would otherwise let it slip
#      through). A batch of exactly one such item is fine: nothing else in
#      the batch exists for it to conflict with.
#   2. Validate the requested batch is INTERNALLY conflict-free (no two
#      items in it share/overlap a resource, using the same file⊃symbol-
#      aware _resource_sets_conflict get_parallelizable_groups' coloring
#      uses) — catches a stale or hand-assembled batch.
#   3. Persist an immutable batch-claim manifest (db.batch_claim) BEFORE
#      attempting any lock — a durable, auditable record of what was
#      DECIDED, independent of whether the attempt below succeeds.
#   4. Attempt, in order, for every item in the batch: claim the item's
#      status (claim_sprint_item — the SAME gates/atomicity a normal solo
#      claim gets: deferred/superseded/wave-gate/unprospected/race-lost all
#      apply unchanged), then claim every one of its declared resources
#      (file:/symbol: via claim_file/claim_symbol — preserving AST-resolved
#      symbol-level concurrency, never downgraded to a coarser whole-file
#      lock; any other typed resource via the generic claim_resource
#      primitive). The FIRST failure anywhere — item-claim conflict/gate, or
#      resource conflict — rolls back EVERYTHING this call acquired so far
#      (resources released, item statuses reverted to what they were before
#      this call touched them) and marks the manifest 'failed' with a
#      structured detail identifying exactly what conflicted. No partial
#      state is ever left behind.
#   5. On full success the manifest is marked 'claimed' and every item comes
#      back in_progress with its resources held.
#
# item_sessions lets a caller pre-assign a DISTINCT claiming session per item
# (the normal real-parallelism shape: each worker gets its own session_id
# before it launches, matching how 18c488b6's own tests exercise two
# different sessions claiming two disjoint symbols in the same file) —
# resources end up held under the SAME session that will actually do the
# work, so nothing needs to be "handed off" after workers start. Any item_id
# not present in item_sessions falls back to the top-level session_id (the
# simple single-claimant case).
# ---------------------------------------------------------------------------


async def _claim_batch_resource(
    db: aiosqlite.Connection,
    resource: str,
    session_id: str,
    resource_contents: dict[str, Any] | None,
) -> dict[str, Any]:
    """Acquire ONE declared resource for the atomic batch gate.

    Self-contained mirror of meridian.mcp.handler._sprint_item_resource_claim
    _gate's per-resource acquisition logic (file:/symbol: via claim_file/
    claim_symbol with AST-aware symbol disjointness and a whole-file
    fallback when no source content is available; anything else via the
    generic typed-resource lock claim_resource) — reimplemented here rather
    than imported, since meridian.db must not depend on meridian.mcp.

    Returns ``{"acquired": True, "newly_acquired": bool, "scope": "file"|
    "symbol"|"generic"|"none", "resource": resource, "claim_granularity":
    "file"|"symbol"|"coarse"|"unresolved"|"n/a", ...}`` on success, or
    ``{"acquired": False, "scope": ..., "resource": resource,
    "holder_session_id": ..., "reason": ..., "claim_granularity": ...}`` on
    conflict/rejection.

    2a176d6d (findings 3 + 4) — ``claim_granularity`` classifies what was
    ACTUALLY acquired, independent of the coarser ``scope`` field, so a
    caller never has to infer real precision from ``fallback_reason``
    presence/absence:
      * ``"file"``  — a genuinely-declared ``file:`` resource (real
        whole-file intent, not a fallback).
      * ``"symbol"`` — a real AST/tree-sitter-resolved symbol-range claim.
      * ``"coarse"`` — a ``symbol:`` resource that widened to a whole-file
        lock (no source supplied, unparseable, symbol not found, or
        ambiguous). The lock IS real/safe, but it must never be reported or
        relied upon as symbol-safe — that is exactly the granularity
        mismatch the 2026-08-04 V026-batch6 audit flagged.
      * ``"unresolved"`` — nothing was locked at all (a malformed/bare
        ``symbol:<name>`` with no resolvable file scope). Previously this
        silently returned ``acquired: True`` (finding 3) even though zero
        lock was taken; it is now a hard ``acquired: False`` failure so
        ``claim_parallel_batch`` rolls back and refuses the batch instead of
        calling a zero-lock resource "claimed".
      * ``"n/a"`` — a non-code typed resource (``db:``, ``route:``, ...)
        where the file/symbol distinction does not apply.

    6b3b2c0e — the ``file:`` branch below resolves the ACTUAL lock key
    through :func:`_resource_file_of`, the same canonical real-file identity
    the scheduler's conflict coloring already uses, instead of the raw
    ``resource[len("file:"):]`` suffix. Before this fix, a legacy
    single-colon ``file:<path>:<symbol>`` declaration (2a176d6d's accepted
    "preferred form") was claimed under a fabricated, per-declaration-unique
    key ("<path>:<symbol>") instead of the real file "<path>" — so two items
    declaring ``file:x.py:funcA`` and ``file:x.py:funcB`` could BOTH acquire
    a "lock" concurrently even though get_parallelizable_groups already
    proves (and always has) that they must be treated as the SAME real file
    and serialized. The outcome dict's ``resolved_from_legacy_shorthand``
    flag makes that resolution auditable for a caller/test.
    """
    from meridian.db import (  # noqa: PLC0415
        claim_file, claim_symbol, claim_resource,
        get_file_claims, get_symbol_claims, get_resource_claims,
    )

    if resource.startswith("file:"):
        file_path = _resource_file_of(resource) or resource[len("file:"):]
        legacy_shorthand = file_path != resource[len("file:"):]
        pre = await get_file_claims(db, file_path)
        pre_held = bool((pre.get("file_lock") or {}).get("session_id") == session_id)
        result = await claim_file(db, file_path, session_id, mode="write")
        if result.get("claimed"):
            outcome = {
                "acquired": True, "scope": "file", "resource": resource,
                "file_path": file_path, "newly_acquired": not pre_held,
                "claim_granularity": "file",
            }
            if legacy_shorthand:
                outcome["resolved_from_legacy_shorthand"] = True
            return outcome
        outcome = {
            "acquired": False, "scope": "file", "resource": resource,
            "file_path": file_path,
            "holder_session_id": result.get("holder_session_id"),
            "reason": result.get("reason") or "locked",
            "claim_granularity": "file",
        }
        if legacy_shorthand:
            outcome["resolved_from_legacy_shorthand"] = True
        return outcome

    if resource.startswith("symbol:"):
        value = resource[len("symbol:"):]
        file_path, sep, symbol_name = value.partition("::")
        if not sep or not symbol_name or not file_path:
            # 2a176d6d (finding 3) — a bare symbol id with no resolvable file
            # scope acquires NO lock at all. Previously this returned
            # acquired=True/scope='none' as a benign no-op, which let a
            # multi-item batch believe this resource was "claimed" when
            # nothing was ever locked for it. Reject it outright so
            # claim_parallel_batch's existing acquired-check rolls the whole
            # batch back (BATCH_RESOURCE_CONFLICT) instead of silently
            # treating a zero-lock resource as safe.
            return {
                "acquired": False, "scope": "none", "resource": resource,
                "newly_acquired": False, "reason": "no_file_scope",
                "claim_granularity": "unresolved",
            }

        content = _batch_resource_content_lookup(resource_contents, file_path)
        fallback_reason: str | None = None
        if content:
            symbol_claims = await get_symbol_claims(db, file_path)
            pre_held = any(
                c.get("symbol_name") == symbol_name and c.get("session_id") == session_id
                for c in symbol_claims
            )
            symbol_result = await claim_symbol(db, session_id, file_path, symbol_name, content)
            if symbol_result.get("claimed"):
                return {
                    "acquired": True, "scope": "symbol", "resource": resource,
                    "file_path": file_path, "symbol": symbol_name,
                    "newly_acquired": not pre_held,
                    "claim_granularity": "symbol",
                }
            if symbol_result.get("reason") in ("symbol_conflict", "file_locked"):
                holder = symbol_result.get("holder_session_id")
                if not holder:
                    conf = symbol_result.get("conflicts") or [{}]
                    holder = conf[0].get("holder_session_id")
                return {
                    "acquired": False, "scope": "symbol", "resource": resource,
                    "file_path": file_path, "symbol": symbol_name,
                    "holder_session_id": holder,
                    "reason": symbol_result.get("reason"),
                    "claim_granularity": "symbol",
                }
            # unparseable / symbol_not_found / ambiguous_symbol — explicit
            # fallback, recorded below and classified "coarse".
            fallback_reason = symbol_result.get("reason") or "unparseable"
        else:
            fallback_reason = "no_source_supplied"

        file_claims = await get_file_claims(db, file_path)
        pre_held_file = bool(
            (file_claims.get("file_lock") or {}).get("session_id") == session_id
        )
        file_result = await claim_file(db, file_path, session_id, mode="write")
        if file_result.get("claimed"):
            return {
                "acquired": True, "scope": "file", "resource": resource,
                "file_path": file_path, "symbol": symbol_name,
                "newly_acquired": not pre_held_file,
                "fallback_reason": fallback_reason,
                "claim_granularity": "coarse",
            }
        return {
            "acquired": False, "scope": "file", "resource": resource,
            "file_path": file_path, "symbol": symbol_name,
            "holder_session_id": file_result.get("holder_session_id"),
            "reason": file_result.get("reason") or "locked",
            "fallback_reason": fallback_reason,
            "claim_granularity": "coarse",
        }

    # Other typed resources (db:, route:, mcp_tool:, pypi:, github:, note:,
    # decision:) — the generic typed-resource lock primitive.
    claims = await get_resource_claims(db, resource)
    pre_held = bool((claims.get("resource_lock") or {}).get("session_id") == session_id)
    result = await claim_resource(db, resource, session_id)
    if result.get("claimed"):
        return {
            "acquired": True, "scope": "generic", "resource": resource,
            "newly_acquired": not pre_held,
            "claim_granularity": "n/a",
        }
    return {
        "acquired": False, "scope": "generic", "resource": resource,
        "holder_session_id": result.get("holder_session_id"),
        "reason": "locked",
        "claim_granularity": "n/a",
    }


def _batch_resource_content_lookup(
    resource_contents: dict[str, Any] | None, file_path: str
) -> str | None:
    """Best-effort lookup of caller-supplied source for ``file_path``,
    tolerant of backslash/leading-``./`` path-separator variance. Mirrors
    meridian.mcp.handler._resource_content_lookup."""
    if not resource_contents or not isinstance(resource_contents, dict):
        return None
    if file_path in resource_contents:
        val = resource_contents[file_path]
        return val if isinstance(val, str) and val else None
    normalized = file_path.strip().replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    for key, val in resource_contents.items():
        k = str(key or "").strip().replace("\\", "/")
        if k.startswith("./"):
            k = k[2:]
        if k == normalized and isinstance(val, str) and val:
            return val
    return None


async def _release_batch_resource(
    db: aiosqlite.Connection, entry: dict[str, Any], session_id: str
) -> None:
    """Release exactly one entry _claim_batch_resource newly acquired. Never
    raises — best-effort cleanup during a batch rollback."""
    from meridian.db import release_file, release_symbol, release_resource  # noqa: PLC0415
    scope = entry.get("scope")
    try:
        if scope == "file":
            await release_file(db, entry["file_path"], session_id)
        elif scope == "symbol":
            await release_symbol(db, session_id, entry["file_path"], entry["symbol"])
        elif scope == "generic":
            await release_resource(db, entry["resource"], session_id)
        # scope == "none": nothing was ever acquired for this entry.
    except Exception:  # noqa: BLE001 — best-effort cleanup, never raise from here
        pass


async def _revert_batch_item_claim(
    db: aiosqlite.Connection,
    project_id: str,
    item_id: str,
    original_status: str,
    original_actor: str | None,
) -> None:
    """Undo a claim_sprint_item this SAME batch call just made, used when a
    LATER item/resource in the batch fails and the whole attempt must roll
    back to a no-partial-claim state.

    Reverts status back to ``original_status`` (whatever it was immediately
    before this call claimed it) via _transition_status's atomic from-state
    guard, then explicitly restores claimed_at/actor (mirroring
    requeue_or_fail_stalled_item's re-queue bookkeeping — _transition_status
    itself only clears claimed_at when told to set it "now", never to NULL).
    """
    reverted = await _transition_status(
        db, project_id, item_id, original_status,
        from_statuses=["in_progress"],
    )
    if reverted is None:
        # Another session raced in and moved the item on from in_progress
        # before this rollback landed — nothing safe to revert; leave it as
        # is rather than force a status it may no longer legitimately hold.
        return
    await db.execute(
        "UPDATE sprint_items SET claimed_at = NULL, actor = ? "
        "WHERE id = ? AND project_id = ?",
        (original_actor, item_id, project_id),
    )
    await db.commit()
    _invalidate_sprint_items_cache(project_id)


# ---------------------------------------------------------------------------
# 704edefe — reservation / integration-queue manifest fields.
#
# claim_parallel_batch already persists an immutable "what batch was
# decided" manifest (batch_claim.py, 22cad9b8) and already rejects a
# duplicate reservation (BATCH_MANIFEST_EXISTS, unless force_manifest=True)
# and a stale one (STALE_PLAN_GENERATION, via plan_generation vs. the live
# board digest) — those two "reject duplicate/stale reservations" behaviors
# are pre-existing and are NOT touched here. What was missing from the
# manifest itself: which symbols/files each resource actually resolved to
# and at what granularity, the dependency edges this batch was validated
# against, each item's own declared expected output, a derived verifier
# class, and a dependency-respecting integration order — the fields this
# item's notes ask the manifest to record. All four helpers below read ONLY
# already-existing sprint-item fields/resource-parsing behavior (depends_on,
# artifact_kind/planned_output/artifact_policy, require_verification/
# require_strict_evidence, and the existing symbol:<path>::<name> /
# file:<path> resource shapes _claim_batch_resource already parses) — no
# new sprint_items schema, and no dependency on c2d41e96's in-flight
# canonical symbol-resource parsing work, which touches a DIFFERENT
# function (get_parallelizable_groups) in this same file. Because every one
# of these fields is recomputed from the LIVE board on every
# claim_parallel_batch call (nothing here is cached from a previous call),
# a board revision between two calls is automatically reflected — this is
# the "recompute on board revision changes" property for these fields,
# parallel to (but independent of) plan_generation's explicit staleness
# check for status/resource state.
# ---------------------------------------------------------------------------


def _classify_verifier_class(item: dict[str, Any]) -> str:
    """704edefe — deterministic verifier-class classification for the
    reservation manifest, derived purely from an item's own already-existing
    verification-related fields (no new sprint_items schema). Escalating
    strictness, most demanding first:

      * ``"strict_evidence"``       — ``require_strict_evidence`` is set
        (the override_strict_evidence gate; see SprintItemEvidenceRequired).
      * ``"verification_required"`` — ``require_verification`` is set (the
        run_verification gate; see SprintItemVerificationRequired).
      * ``"artifact_check"``        — neither flag is set, but the item
        declares an ``artifact_kind`` (figure/table/document_only), so its
        completion is still subject to an artifact-pointer check
        (``artifact_policy``) even though it isn't gated by either flag
        above.
      * ``"standard"``              — none of the above; ordinary
        completion, no special verification contract.
    """
    if item.get("require_strict_evidence"):
        return "strict_evidence"
    if item.get("require_verification"):
        return "verification_required"
    if item.get("artifact_kind"):
        return "artifact_check"
    return "standard"


def _expected_output_of(item: dict[str, Any]) -> dict[str, Any]:
    """704edefe — an item's own declared "what this produces" trio, exactly
    as recorded by 2f9cb288's artifact-declaration fields. A read-only
    snapshot for the reservation manifest; never mutates the item.

    Reads through ``artifact_declaration``'s ``effective_*`` accessors
    (the SAME canonical decode path every other caller in this codebase
    uses — see ``meridian.artifact_declaration``'s own module docstring),
    not the raw ``item.get(...)`` fields directly: ``planned_output`` and
    ``artifact_policy`` are stored as serialized JSON TEXT columns
    (``_artifact_declaration.serialize_planned_output`` /
    ``serialize_artifact_policy`` at write time), so reading the raw field
    off a fetched row returns a JSON string, not a dict, unless decoded via
    these accessors first. ``artifact_policy`` uses
    ``effective_artifact_policy`` specifically (not a bare parse) so an item
    that declares no policy at all still reports the real project-default
    policy it will actually be checked against, matching what
    ``effective_artifact_policy`` documents ("absent is unknown, never
    strict and never off" refers to the STORED value; the EFFECTIVE one
    always resolves to a concrete policy)."""
    return {
        "artifact_kind": _artifact_declaration.effective_artifact_kind(item),
        "planned_output": _artifact_declaration.effective_planned_output(item),
        "artifact_policy": _artifact_declaration.effective_artifact_policy(item),
    }


async def _dependency_frontier_snapshot(
    db: aiosqlite.Connection, items_by_id: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """704edefe — durable snapshot of each batch item's ``depends_on`` edge
    and whether that dependency was satisfied AT RESERVATION TIME, for the
    integration-queue manifest's audit trail.

    claim_sprint_item's own dependency gate already refuses to claim an item
    whose depends_on parent isn't done, so by the time claim_parallel_batch
    reaches this point every item in the batch necessarily has a satisfied
    (or absent) dependency — this function does not itself enforce
    anything; it records the fact for later audit/integration-order use.
    One extra lookup per DISTINCT out-of-batch dependency id (an in-batch
    dependency is resolved from ``items_by_id`` with no extra query;
    repeated out-of-batch ids are cached so a shared parent is only fetched
    once).
    """
    frontier: dict[str, dict[str, Any]] = {}
    dep_cache: dict[str, "dict[str, Any] | None"] = {}
    for iid, item in items_by_id.items():
        dep_id = item.get("depends_on")
        if not dep_id:
            frontier[iid] = {"depends_on": None, "dependency_satisfied": True}
            continue
        dep_item = items_by_id.get(dep_id)
        if dep_item is None:
            if dep_id not in dep_cache:
                dep_cache[dep_id] = await get_sprint_item(db, dep_id)
            dep_item = dep_cache[dep_id]
        frontier[iid] = {
            "depends_on": dep_id,
            "dependency_satisfied": bool(dep_item) and dep_item.get("status") == "done",
            "dependency_in_batch": dep_id in items_by_id,
        }
    return frontier


def _compute_integration_order(
    ordered_ids: list[str], items_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    """704edefe — dependency-respecting integration order for this batch's
    "integration queue": an item whose ``depends_on`` parent is ALSO in this
    batch must integrate strictly after that parent. Items with no in-batch
    dependency keep their original ``ordered_ids`` relative order (stable).

    Only IN-BATCH dependency edges affect ordering — an out-of-batch
    dependency is, by the depends_on claim gate's own contract, already
    'done' before this item could ever be claimed (see
    _dependency_frontier_snapshot), so it imposes no additional integration
    sequencing within this batch.

    Cycle-safe: a depends_on cycle is already rejected at write time
    (update_sprint_item's cycle guard), but if one somehow reached this far
    the loop below still terminates — any item that can never become
    "ready" is appended, unordered, at the end rather than hanging.
    """
    dep_of = {iid: items_by_id[iid].get("depends_on") for iid in ordered_ids}
    result: list[str] = []
    placed: set[str] = set()
    remaining = list(ordered_ids)
    while remaining:
        progressed = False
        next_remaining: list[str] = []
        for iid in remaining:
            dep = dep_of[iid]
            if dep is None or dep not in items_by_id or dep in placed:
                result.append(iid)
                placed.add(iid)
                progressed = True
            else:
                next_remaining.append(iid)
        remaining = next_remaining
        if not progressed:
            result.extend(remaining)  # cycle guard — never hang
            break
    return result


async def claim_parallel_batch(
    db: aiosqlite.Connection,
    project_id: str,
    session_id: str,
    item_ids: list[str],
    *,
    item_sessions: dict[str, str] | None = None,
    resource_contents: dict[str, Any] | None = None,
    force_manifest: bool = False,
    manifest_reason: str | None = None,
    plan_generation: str | None = None,
) -> dict[str, Any]:
    """22cad9b8 — atomically claim a whole parallel-safe batch of sprint items.

    ``session_id`` is the requesting/orchestrating session, recorded on the
    durable batch manifest and used as the default claiming identity for any
    item not overridden in ``item_sessions``. ``item_sessions`` optionally
    maps ``{item_id: claiming_session_id}`` so a caller can pre-assign each
    item to the DISTINCT worker session that will actually execute it —
    resources then end up held under the session that does the work, so
    nothing needs handing off once workers launch. This is the lease-local
    path the scheduler contract (0d0cada7) expects for real parallel
    fan-out: prefer it over reusing one ``session_id`` for every item in a
    multi-item batch (see ``lease_local_warning`` on the success result).

    ``plan_generation`` (0d0cada7, optional) — when supplied, must match the
    digest a caller previously computed for exactly this item set (see
    :func:`get_parallelizable_groups`'s ``group_generations``, index-aligned
    with its ``groups``). If the live board has moved since that digest was
    taken (any of these items' status/claimed_at/resources changed), the
    call is rejected with ``STALE_PLAN_GENERATION`` before anything is
    persisted or claimed — a stale wave plan is refreshed, never treated as
    still valid. Omitted (``None``, the default) skips the check entirely —
    every existing caller is unaffected.

    Returns ``{"ok": True, "manifest_id", "batch_key", "claimed_item_ids",
    "items", "resources", "manifest", "plan_generation",
    "lease_local_warning", "integration_order"}`` on success, or
    ``{"ok": False, "error": <code>, "message": ...}`` (plus error-specific
    fields) on any rejection — never raises for an expected
    validation/conflict outcome, only for a genuine caller bug (empty
    item_ids / missing session_id). See the module-level comment above for
    the full step-by-step contract; error codes are: ITEM_NOT_FOUND,
    STALE_PLAN_GENERATION, UNDECLARED_RESOURCE_IN_BATCH,
    BATCH_COMPOSITION_CONFLICT, BATCH_MANIFEST_EXISTS, ITEM_CLAIM_CONFLICT,
    <claim_sprint_item's own blocked "error" values e.g. DEFERRED/SUPERSEDED/
    WAVE_GATE_PENDING/UNPROSPECTED>, BATCH_RESOURCE_CONFLICT.

    704edefe — the persisted ``manifest`` is now a genuine reservation +
    integration-queue record, not just "which items/resources were
    decided": it also carries ``resolved_symbols`` (per-resource
    file/symbol/granularity — a static prediction at persist time,
    overwritten with the actual claim outcome once the attempt resolves),
    ``dependency_frontier`` (each item's depends_on edge and whether it was
    satisfied at reservation time), ``expected_outputs`` (each item's own
    declared artifact_kind/planned_output/artifact_policy), and
    ``verifier_class`` (a derived strict_evidence/verification_required/
    artifact_check/standard classification per item). ``integration_order``
    — the dependency-respecting sequence this batch's items should be
    integrated/merged in — is both on the manifest and promoted to the
    top-level success result for convenience. All five are computed fresh
    from the live board on every call (never cached), so a board revision
    between calls is always reflected; see the module comment above this
    function for the full rationale. The pre-existing "reject duplicate/
    stale reservations" behaviors (BATCH_MANIFEST_EXISTS,
    STALE_PLAN_GENERATION) are unchanged by this.
    """
    if not session_id:
        raise ValueError("session_id is required to claim a batch")
    seen_ids: set[str] = set()
    ordered_ids: list[str] = []
    for iid in item_ids or []:
        if iid and iid not in seen_ids:
            seen_ids.add(iid)
            ordered_ids.append(iid)
    if not ordered_ids:
        raise ValueError("item_ids must be a non-empty list")

    from .batch_claim import (  # noqa: PLC0415
        persist_batch_claim_manifest, mark_batch_claim_outcome, compute_batch_key,
    )

    # ── 1. Load + validate every item exists and belongs to this project ───
    items_by_id: dict[str, dict[str, Any]] = {}
    for iid in ordered_ids:
        item = await get_sprint_item(db, iid)
        if item is None or item.get("project_id") != project_id:
            return {
                "ok": False,
                "error": "ITEM_NOT_FOUND",
                "message": f"sprint item {iid!r} not found in project {project_id!r}",
                "item_id": iid,
            }
        items_by_id[iid] = item

    item_resources: dict[str, list[str]] = {
        iid: parse_touches_resources(items_by_id[iid].get("touches_resources"))
        for iid in ordered_ids
    }

    # ── Plan-generation staleness guard (0d0cada7) — fail BEFORE persisting a
    # manifest or claiming anything so a stale plan never leaves partial state.
    # Uses the exact same _plan_generation_entries shape/order as
    # get_parallelizable_groups' per-group digest — including the live
    # resource-HOLDER cross-check, not just each item's own status/
    # claimed_at/resources columns — so a caller's previously-fetched
    # ``group_generations`` entry compares equal when (and only when)
    # nothing about these specific items OR the resources they declare has
    # changed. A digest that only watched the items' own rows would miss the
    # 2026-08-05 incident shape exactly: an item's row never changes while a
    # totally unrelated in_progress item quietly holds its declared resource.
    # ──
    _holder_cache: dict[str, "dict[str, Any] | None"] = {}
    _current_generation = _compute_plan_generation(await _plan_generation_entries(
        db,
        [
            (
                iid, items_by_id[iid].get("status") or "pending",
                str(items_by_id[iid].get("claimed_at") or ""),
                item_resources[iid],
            )
            for iid in ordered_ids
        ],
        holder_cache=_holder_cache,
    ))
    if plan_generation is not None and plan_generation != _current_generation:
        return {
            "ok": False,
            "error": "STALE_PLAN_GENERATION",
            "message": (
                "this batch's plan_generation no longer matches the live board "
                "(an item's status, claim, or declared resources changed since "
                "the digest was computed) — recompute via "
                "get_parallelizable_groups and retry with the fresh generation "
                "instead of treating this plan as still valid."
            ),
            "expected_plan_generation": plan_generation,
            "current_plan_generation": _current_generation,
        }

    # ── Undeclared-resource guard: never silently treat "nothing declared"
    # as "safe to parallelize" (mirrors get_parallelizable_groups' de730a25
    # invariant). A batch of exactly one item is exempt — nothing else in a
    # singleton batch exists for it to conflict with. ──
    if len(ordered_ids) > 1:
        undeclared = [iid for iid in ordered_ids if not item_resources[iid]]
        if undeclared:
            return {
                "ok": False,
                "error": "UNDECLARED_RESOURCE_IN_BATCH",
                "message": (
                    "item(s) "
                    f"{', '.join(i[:8] for i in undeclared)} declare no "
                    "touches_resources, so parallel safety can't be proven for "
                    "them. Claim them individually (a batch of one) instead of "
                    "including them in a multi-item atomic batch."
                ),
                "undeclared_item_ids": undeclared,
            }

    # ── 2. Internal composition check: the requested batch must itself be
    # conflict-free, using the same file⊃symbol-aware comparison
    # get_parallelizable_groups' coloring uses. Catches a stale or
    # hand-assembled batch that was never actually disjoint. ──
    composition_conflicts: list[dict[str, Any]] = []
    for i, a_id in enumerate(ordered_ids):
        a_res = set(item_resources[a_id])
        for b_id in ordered_ids[i + 1:]:
            b_res = set(item_resources[b_id])
            if _resource_sets_conflict(a_res, b_res):
                composition_conflicts.append({
                    "item_a": a_id, "item_b": b_id,
                    "resources_a": sorted(a_res), "resources_b": sorted(b_res),
                })
    if composition_conflicts:
        return {
            "ok": False,
            "error": "BATCH_COMPOSITION_CONFLICT",
            "message": (
                "requested batch is not internally conflict-free — two or more "
                "items in it declare overlapping resources. Recompute the batch "
                "via get_parallelizable_groups and retry."
            ),
            "conflicting_pairs": composition_conflicts,
        }

    resources_union = sorted({r for lst in item_resources.values() for r in lst})

    # ── 2b. 704edefe — compute the reservation/integration-queue fields
    # BEFORE persisting, from the live board (items_by_id was just loaded
    # above), so the durable manifest below records them from the start. ──
    _predicted_resolved_symbols = [
        {"resource": r, "predicted_granularity": _predict_resource_granularity(r)}
        for r in resources_union
    ]
    _dependency_frontier = await _dependency_frontier_snapshot(db, items_by_id)
    _expected_outputs = {iid: _expected_output_of(items_by_id[iid]) for iid in ordered_ids}
    _verifier_class = {iid: _classify_verifier_class(items_by_id[iid]) for iid in ordered_ids}
    _integration_order = _compute_integration_order(ordered_ids, items_by_id)

    # ── 3. Persist the immutable manifest BEFORE attempting any lock — a
    # durable audit record of what was decided, independent of whether the
    # attempt below actually succeeds. ──
    try:
        manifest = await persist_batch_claim_manifest(
            db, project_id, session_id, ordered_ids, item_resources, resources_union,
            force=force_manifest, reason=manifest_reason,
            resolved_symbols=_predicted_resolved_symbols,
            dependency_frontier=_dependency_frontier,
            expected_outputs=_expected_outputs,
            verifier_class=_verifier_class,
            integration_order=_integration_order,
        )
    except ValueError as exc:
        return {
            "ok": False,
            "error": "BATCH_MANIFEST_EXISTS",
            "message": str(exc),
            "batch_key": compute_batch_key(ordered_ids),
        }

    # ── 4. Attempt to atomically claim every item's status AND every
    # declared resource. All-or-nothing across the whole batch. ──
    claimed_items: list[tuple[str, str, str | None]] = []  # (item_id, orig_status, orig_actor)
    acquired_resources: list[dict[str, Any]] = []
    # 2a176d6d (finding 4) — per-resource claim_granularity record for every
    # resource actually attempted in this batch (additive; does not affect
    # the "resources" union field below).
    resource_claims: list[dict[str, Any]] = []

    async def _rollback_and_fail(error_code: str, message: str, **extra: Any) -> dict[str, Any]:
        for entry in reversed(acquired_resources):
            await _release_batch_resource(db, entry, entry["_session_id"])
        for iid, orig_status, orig_actor in reversed(claimed_items):
            await _revert_batch_item_claim(db, project_id, iid, orig_status, orig_actor)
        detail = {"error": error_code, "message": message, **extra}
        # 704edefe — record however far the resource resolution actually got
        # before the failure (empty list if the failure happened before the
        # resource loop ever ran, e.g. an ITEM_CLAIM_CONFLICT on the first
        # item) rather than leaving the pre-attempt prediction unrefined.
        await mark_batch_claim_outcome(
            db, manifest["id"], "failed", failure_detail=detail,
            resolved_symbols=resource_claims,
        )
        return {"ok": False, "manifest_id": manifest["id"], **detail}

    for iid in ordered_ids:
        item = items_by_id[iid]
        orig_status = item.get("status") or "pending"
        orig_actor = item.get("actor")
        claim_session = (item_sessions or {}).get(iid, session_id)
        try:
            claim_result = await claim_sprint_item(db, project_id, iid, actor=claim_session)
        except ValueError as exc:
            return await _rollback_and_fail(
                "ITEM_CLAIM_CONFLICT",
                f"could not claim sprint item {iid!r}: {exc}",
                item_id=iid,
            )
        if claim_result is None:
            return await _rollback_and_fail(
                "ITEM_CLAIM_CONFLICT",
                f"sprint item {iid!r} vanished during the batch claim attempt",
                item_id=iid,
            )
        if isinstance(claim_result, dict) and claim_result.get("blocked"):
            return await _rollback_and_fail(
                claim_result.get("error") or "ITEM_CLAIM_BLOCKED",
                claim_result.get("reason") or f"sprint item {iid!r} could not be claimed",
                item_id=iid,
            )
        claimed_items.append((iid, orig_status, orig_actor))

        for resource in item_resources[iid]:
            outcome = await _claim_batch_resource(db, resource, claim_session, resource_contents)
            if not outcome.get("acquired"):
                # 2a176d6d (finding 3) — a resource with claim_granularity
                # "unresolved" (bare symbol:<name>, no '::' file scope) was
                # never held by anyone; "locked by another live session" would
                # be a misleading message for it. Give it its own error code
                # so a caller can tell "malformed declaration" apart from a
                # real cross-session conflict.
                if outcome.get("claim_granularity") == "unresolved":
                    return await _rollback_and_fail(
                        "MALFORMED_RESOURCE",
                        f"resource {resource!r} (item {iid!r}) has no resolvable "
                        "file scope (bare 'symbol:<name>' with no "
                        "'<path>::<symbol>' form), so no lock could be acquired "
                        "for it. Fix the touches_resources declaration to "
                        "'symbol:<path>::<symbol>'.",
                        item_id=iid, resource=resource,
                    )
                # 0d0cada7 — enrich the conflict with the same wait_reason/
                # lease_expiry/retry_after/claim_granularity shape
                # get_parallelizable_groups' resource_blocked entries use, so
                # a caller sees ONE consistent scheduler-diagnostics contract
                # regardless of which code path surfaced the contention.
                # Best-effort: a fresh lookup race (holder released between
                # the failed acquire above and this read) degrades to the
                # outcome's own fields rather than raising.
                try:
                    _holder = await _live_resource_holder(db, resource)
                except Exception:  # noqa: BLE001 — diagnostics must never mask the real conflict
                    _holder = None
                return await _rollback_and_fail(
                    "BATCH_RESOURCE_CONFLICT",
                    f"resource {resource!r} (item {iid!r}) is locked by another "
                    f"live session ({outcome.get('holder_session_id')}).",
                    item_id=iid, resource=resource,
                    holder_session_id=(_holder or {}).get("holder_session_id")
                    or outcome.get("holder_session_id"),
                    wait_reason="resource_locked",
                    lease_expiry=(_holder or {}).get("lease_expiry"),
                    claim_granularity=(_holder or {}).get("claim_granularity")
                    or outcome.get("claim_granularity"),
                    retry_after=_seconds_until((_holder or {}).get("lease_expiry")),
                    plan_generation=_current_generation,
                )
            # 2a176d6d (finding 4) — record what granularity was ACTUALLY
            # acquired for every resource in the batch (not just newly-
            # acquired ones), so the launcher/orchestrator can see which
            # resources landed a real symbol-range claim vs. a coarse
            # whole-file fallback, rather than inferring it from
            # fallback_reason presence/absence downstream.
            _resource_claim_record = {
                "item_id": iid,
                "resource": resource,
                "scope": outcome.get("scope"),
                "claim_granularity": outcome.get("claim_granularity"),
                "fallback_reason": outcome.get("fallback_reason"),
            }
            # 6b3b2c0e — surface the legacy single-colon file:<path>:<symbol>
            # classification on the PUBLIC batch result too (not just the
            # internal _claim_batch_resource outcome), so a caller can audit
            # which resources in a successful batch resolved through the
            # legacy shorthand rather than a canonical declaration.
            if outcome.get("resolved_from_legacy_shorthand"):
                _resource_claim_record["resolved_from_legacy_shorthand"] = True
            resource_claims.append(_resource_claim_record)
            if outcome.get("newly_acquired"):
                outcome["_item_id"] = iid
                outcome["_session_id"] = claim_session
                acquired_resources.append(outcome)

    # 704edefe — overwrite the pre-attempt PREDICTED resolved_symbols with
    # the ACTUAL per-resource outcome the loop above just built.
    final_manifest = await mark_batch_claim_outcome(
        db, manifest["id"], "claimed", resolved_symbols=resource_claims,
    )
    result_items = [await get_sprint_item(db, iid) for iid in ordered_ids]

    # 0d0cada7 — lease-local nudge: a multi-item batch where the SAME session
    # ends up as the claiming identity for more than one item is exactly the
    # pattern behind the live incident this item fixes (one session "planning
    # its backlog" by holding several items' claims while only genuinely
    # executing one at a time, starving every other live session). This never
    # blocks the call — ``item_sessions`` assigning each item to its own
    # DISTINCT worker session is the documented correct usage and is left
    # completely alone — it only surfaces the risk so a caller (or the
    # executor reading this response) can self-correct.
    _session_to_items: dict[str, list[str]] = {}
    for iid in ordered_ids:
        _claim_session = (item_sessions or {}).get(iid, session_id)
        _session_to_items.setdefault(_claim_session, []).append(iid)
    lease_local_warning = [
        {"session_id": sid, "item_ids": iids}
        for sid, iids in _session_to_items.items()
        if len(iids) > 1
    ] if len(ordered_ids) > 1 else []

    return {
        "ok": True,
        "manifest_id": manifest["id"],
        "batch_key": manifest["batch_key"],
        "claimed_item_ids": ordered_ids,
        "items": result_items,
        "resources": resources_union,
        # 2a176d6d (finding 4) — additive: per-resource claim_granularity so
        # a caller can see which resources landed a real symbol-range claim
        # vs. a coarse whole-file fallback, without changing "resources"
        # (still the plain sorted-string union other callers already parse).
        "resource_claims": resource_claims,
        "manifest": final_manifest,
        # 0d0cada7 — lease-local scheduler diagnostics (additive).
        "plan_generation": _current_generation,
        "lease_local_warning": lease_local_warning,
        # 704edefe — the integration-queue's dependency-respecting merge
        # order, promoted to the top level for convenience (also on
        # final_manifest["integration_order"]).
        "integration_order": _integration_order,
    }


def _topo_depth_map(items: list[dict[str, Any]]) -> dict[str, int]:
    """Compute topological depth for each item in ``items`` by dependency.

    Returns a ``{item_id: depth}`` mapping where depth 0 means "no in-set
    dependency" and depth N means "depends on something at depth N-1". Mirrors
    the logic in ``meridian.handoff._partition_into_waves`` but returns a plain
    dict rather than a grouped list so callers can layer additional coloring on
    top. Cycles are broken by treating the back-edge target as depth 0.

    NOTE: This helper is intentionally self-contained in sprint_items.py because
    meridian.handoff imports meridian.db, making a reverse import a circular
    dependency. Keep in sync with handoff._partition_into_waves (3726cf70).
    """
    by_id = {it["id"]: it for it in items if it.get("id")}
    wave_of: dict[str, int] = {}

    def _depth(iid: str, seen: set[str]) -> int:
        if iid in wave_of:
            return wave_of[iid]
        if iid in seen:  # dependency cycle — treat as a root
            return 0
        seen.add(iid)
        it = by_id.get(iid)
        dep = it.get("depends_on") if it else None
        depth = (_depth(dep, seen) + 1) if (dep and dep in by_id) else 0
        wave_of[iid] = depth
        return depth

    for iid in by_id:
        _depth(iid, set())
    return wave_of


async def assign_sprint_waves(
    db: aiosqlite.Connection,
    project_id: str,
    version: str | None = None,
) -> dict[str, Any]:
    """58a45b92 / 90955d26 — persist a full topological + conflict-free wave plan.

    Previous behaviour (:func:`get_parallelizable_groups`) did a single flat pass:
    only dependency-satisfied items were labelled; ``depends_on``-blocked items
    were dropped into ``blocked`` and left with ``wave=NULL``.  This meant a
    planner could never see the projected execution order for items that sit behind
    an unfinished parent — they simply had no wave.

    New behaviour: project ALL pending/todo non-manual items forward through the
    dependency graph using a two-pass algorithm:

    Pass 1 — topological depth (``_topo_depth_map``):
        Items are grouped into layers 0, 1, 2, … by how many hops separate them
        from a root (no in-set dependency).  Layer 0 runs first; layer 1 can start
        only after layer 0 completes; and so on.  This is the same algorithm used
        by ``_partition_into_waves`` in ``handoff.py`` for /goal rendering.

    Pass 2 — resource-conflict coloring within each layer:
        Within a topological layer, items that share a ``touches_resources`` value
        cannot co-schedule.  Greedy first-fit coloring (the same algorithm as
        ``get_parallelizable_groups``) splits each layer into one or more sub-waves.

    The two passes are combined: all sub-waves from layer 0 are numbered first
    (wave-1, wave-2, …), then layer 1's sub-waves continue the numbering, and so
    on.  The result is a monotonically numbered, globally consistent wave plan
    where an item's wave label unambiguously encodes both its dependency position
    AND its resource-conflict position.

    Blocked (dependency-pending) items now receive a future-wave label rather than
    ``NULL``, so ``get_sprint_items`` can surface the full execution plan even
    before the earlier layers have completed.

    Only pending/todo, non-manual-blocker items are labelled — done/failed/skipped/
    in_progress items are left untouched.  Idempotent: re-running recomputes from
    the live board and rewrites the labels.

    f78d7644 — urgent carve-out: ``priority='urgent'`` items whose dependency (if
    any) is already DONE are pulled out of the normal topological/priority
    layering *before* pass 1 and labelled into a dedicated ``wave-urgent``
    lane (``wave-urgent-2``, ``wave-urgent-3``, ... if urgent items themselves
    have resource conflicts and need sub-splitting). ``wave-urgent`` is
    orthogonal to the sequential ``wave-N`` numbering: it is meant to be read
    by an orchestrator as "runnable immediately, in parallel with whatever
    wave-N is already in flight" rather than "wait your turn in the queue" —
    e.g. a live-testing break mid-megasprint that needs a fix right now, not
    queued behind normal-priority waves. A single-executor session (no
    parallel fan-out) instead yields its current item at the next natural
    checkpoint and claims the wave-urgent item next — see
    ``_board_change_for_session`` in meridian/mcp/handler.py, which flags
    newly-added urgent items in its board-change message. Carving urgent
    items out first also means their presence never perturbs the wave-N
    numbering assigned to normal/lower-priority items (no shifted wave
    counters, no urgent item silently occupying a normal-item's slot).
    An urgent item still behind an unmet dependency does not qualify for
    the carve-out (priority never skips real dependency order) and instead
    flows through the normal layering below, where the existing
    highest-priority-first sort still gives it an edge within its layer.

    Returns ``{version, wave_count, assigned, waves: {'wave-1': [ids...], ...},
    blocked_count, undeclared_count, urgent_wave_count, urgent_assigned,
    cycles, graph_digest}``.
    ``blocked_count`` now counts items whose dependency is not yet DONE (they
    are still projected into a future wave, not truly dropped). ``waves``
    includes both the ``wave-N`` sequential labels and any ``wave-urgent*``
    labels — the two families are merged in the returned mapping but remain
    distinguishable by their key prefix.

    ``cycles`` (05553946) — every distinct ``depends_on`` cycle found among
    the eligible item set (see ``meridian.dependency_graph.
    find_all_dependency_cycles``), each a full closed path
    (``["a", "b", "a"]``). ``_topo_depth_map`` below silently treats a
    revisited id as a root (depth 0) purely so wave labelling always
    terminates — it never raises and never drops the involved items from the
    plan — so a cyclic item still gets SOME wave label, but ``cycles`` is
    the explicit, machine-readable signal that the label is a best-effort
    fallback rather than a real topological position; a planner should treat
    a non-empty ``cycles`` list as needing a ``depends_on`` fix (via
    ``patch_sprint_item``, which fails closed on new cycles) before trusting
    the plan. ``graph_digest`` (see ``meridian.dependency_graph.
    compute_dependency_graph_digest``) is a deterministic digest of the same
    eligible item set's ``(id, depends_on)`` edges, for cheap "did the
    dependency graph change since I last called this" comparisons.
    """
    # ── Collect all eligible pending/todo non-manual items ─────────────────────
    items = await get_sprint_items(db, project_id, include_manual_blocker=False)
    items = [it for it in items if not _is_manual_sprint_item(it)]
    if version is not None:
        items = [it for it in items if it.get("version") == version]
    # 05553946 — explicit, full-path cycle diagnostics over the same eligible
    # set _topo_depth_map is about to walk; see the docstring above for why
    # this is informational (wave assignment itself stays lenient/terminating).
    cycles = _dependency_graph.find_all_dependency_cycles(items)
    graph_digest = _dependency_graph.compute_dependency_graph_digest(items)
    claimable_statuses = {"pending", "todo"}
    # Only label pending/todo items that are not already in-flight or done.
    # In-progress items are mid-execution and must not be relabelled mid-run.
    # 5a67c8e0 — also exclude deferred items: a future ``deferred_until`` leaves
    # status='pending' untouched (see claim_sprint_item / _is_deferred), so without
    # this check a backburnered item would still get labelled into a real wave.
    candidates = [
        it for it in items
        if (it.get("status") or "pending") in claimable_statuses
        and not _is_deferred(it)
    ]

    # ── Urgent carve-out (f78d7644) ─────────────────────────────────────────────
    # Ready urgent items (dependency satisfied, or no dependency) never enter the
    # normal topological layering — they get their own immediate wave-urgent* lane
    # so they don't wait behind (or get renumbered by) normal/lower-priority waves.
    by_id_all = {it["id"]: it for it in items}

    def _dep_is_done(it: dict[str, Any]) -> bool:
        dep = it.get("depends_on")
        if not dep:
            return True
        parent = by_id_all.get(dep)
        return bool(parent) and parent.get("status") == "done"

    urgent_ready = [
        it for it in candidates
        if (it.get("priority") or "normal") == "urgent" and _dep_is_done(it)
    ]
    urgent_ready_ids = {it["id"] for it in urgent_ready}
    candidates = [it for it in candidates if it["id"] not in urgent_ready_ids]

    urgent_waves: dict[str, list[str]] = {}
    urgent_assigned = 0
    urgent_undeclared_count = 0
    if urgent_ready:
        # Oldest-first among urgent items themselves (priority is already uniform).
        urgent_ready.sort(key=lambda it: (str(it.get("added_at") or ""), it["id"]))
        urgent_enriched = [
            {**it, "resources": parse_touches_resources(it.get("touches_resources"))}
            for it in urgent_ready
        ]
        urgent_declared = [it for it in urgent_enriched if it["resources"]]
        urgent_undeclared = [it for it in urgent_enriched if not it["resources"]]
        urgent_undeclared_count = len(urgent_undeclared)
        urgent_sub_groups: list[list[dict[str, Any]]] = []
        urgent_sub_resource_sets: list[set[str]] = []
        for it in urgent_declared:
            res = set(it["resources"])
            placed = False
            for gi, used in enumerate(urgent_sub_resource_sets):
                if not _resource_sets_conflict(res, used):
                    urgent_sub_groups[gi].append(it)
                    used.update(res)
                    placed = True
                    break
            if not placed:
                urgent_sub_groups.append([it])
                urgent_sub_resource_sets.append(set(res))
        # Each undeclared urgent item is its own sequential sub-wave — parallel
        # safety can't be proven for it, same rule as the normal-lane coloring.
        for it in urgent_undeclared:
            urgent_sub_groups.append([it])
        for idx, sub_group in enumerate(urgent_sub_groups):
            label = "wave-urgent" if idx == 0 else f"wave-urgent-{idx + 1}"
            ids: list[str] = []
            for it in sub_group:
                await patch_sprint_item(db, project_id, it["id"], wave=label)
                ids.append(it["id"])
                urgent_assigned += 1
            if ids:
                urgent_waves[label] = ids

    # ── Pass 1: topological depth (on the remaining, non-urgent candidates) ─────
    depth_map = _topo_depth_map(candidates)
    if not depth_map:
        return {
            "version": version,
            "wave_count": len(urgent_waves),
            "assigned": urgent_assigned,
            "waves": dict(urgent_waves),
            "blocked_count": 0,
            "undeclared_count": urgent_undeclared_count,
            "urgent_wave_count": len(urgent_waves),
            "urgent_assigned": urgent_assigned,
            "cycles": cycles,
            "graph_digest": graph_digest,
        }

    max_topo = max(depth_map.values())
    # Group candidates by their topological depth preserving input order (which
    # inherits the DB insertion order — stable for idempotency).
    topo_layers: list[list[dict[str, Any]]] = [[] for _ in range(max_topo + 1)]
    for it in candidates:
        iid = it.get("id")
        if iid in depth_map:
            topo_layers[depth_map[iid]].append(it)

    # ── Pass 2: resource-conflict coloring within each topological layer ─────────
    # Sort each layer highest-priority-first (e08fee30), then insertion order.
    def _priority_key(it: dict[str, Any]) -> tuple[int, str, str]:
        return (
            _SPRINT_PRIORITY_RANK.get(
                it.get("priority") or "normal", _SPRINT_PRIORITY_DEFAULT_RANK
            ),
            str(it.get("added_at") or ""),
            it.get("id") or "",
        )

    wave_counter = 0
    waves: dict[str, list[str]] = {}
    assigned = 0
    undeclared_count = 0

    for layer in topo_layers:
        if not layer:
            continue
        layer.sort(key=_priority_key)
        # Attach parsed resources for conflict detection.
        enriched = [
            {**it, "resources": parse_touches_resources(it.get("touches_resources"))}
            for it in layer
        ]
        declared = [it for it in enriched if it["resources"]]
        undeclared_items = [it for it in enriched if not it["resources"]]
        undeclared_count += len(undeclared_items)

        # Greedy first-fit coloring on declared items (same as get_parallelizable_groups).
        sub_groups: list[list[dict[str, Any]]] = []
        sub_resource_sets: list[set[str]] = []
        for it in declared:
            res = set(it["resources"])
            placed = False
            for gi, used in enumerate(sub_resource_sets):
                if not _resource_sets_conflict(res, used):
                    sub_groups[gi].append(it)
                    used.update(res)
                    placed = True
                    break
            if not placed:
                sub_groups.append([it])
                sub_resource_sets.append(set(res))
        # Each undeclared item is its own sequential sub-wave (never co-scheduled).
        for it in undeclared_items:
            sub_groups.append([it])

        # Assign wave labels continuing from the previous topological layer's count.
        for sub_group in sub_groups:
            wave_counter += 1
            label = f"wave-{wave_counter}"
            ids: list[str] = []
            for it in sub_group:
                await patch_sprint_item(db, project_id, it["id"], wave=label)
                ids.append(it["id"])
                assigned += 1
            if ids:
                waves[label] = ids

    # Count items whose depends_on is not yet satisfied (informational only — they
    # now receive a projected future-wave label rather than being dropped).
    by_id_status = {it["id"]: it.get("status") for it in items}
    blocked_count = 0
    for it in candidates:
        dep = it.get("depends_on")
        if dep and by_id_status.get(dep) not in (None, "done"):
            blocked_count += 1

    # f78d7644 — merge the wave-urgent* lane into the returned mapping; it stays
    # distinguishable from the wave-N sequential labels by its key prefix.
    all_waves = {**urgent_waves, **waves}
    return {
        "version": version,
        "wave_count": len(all_waves),
        "assigned": assigned + urgent_assigned,
        "waves": all_waves,
        "blocked_count": blocked_count,
        "undeclared_count": undeclared_count + urgent_undeclared_count,
        "urgent_wave_count": len(urgent_waves),
        "urgent_assigned": urgent_assigned,
        "cycles": cycles,
        "graph_digest": graph_digest,
    }


async def analyze_sprint(
    db: aiosqlite.Connection,
    project_id: str,
    version: str | None = None,
) -> dict[str, Any]:
    """e77f09d1 — synthesize a structured planning brief for the current sprint.

    One call combines what a planner otherwise assembles from four:
      * parallelizability — conflict-free batches from
        :func:`get_parallelizable_groups` (group_count, max fan-out, blocked).
      * dependency chains — ``depends_on`` walked to the root for each open item.
      * resource conflicts — open items whose ``touches_resources`` intersect
        (why they can't co-schedule).
      * stalls — open items with a non-zero ``stall_count``.

    Returns a single dict with a human ``summary`` line and a
    ``recommended_strategy`` ('parallel' when any group holds >1 item).
    """
    groups_info = await get_parallelizable_groups(db, project_id, version=version)
    items = await get_sprint_items(db, project_id)
    if version is not None:
        items = [it for it in items if it.get("version") == version]
    _open = {"pending", "todo", "in_progress"}
    open_items = [it for it in items if (it.get("status") or "pending") in _open]
    by_id = {it["id"]: it for it in items}

    # Dependency chains: walk depends_on to the root for each open dependent item.
    def _chain_for(item: dict[str, Any]) -> list[dict[str, Any]]:
        chain: list[dict[str, Any]] = []
        seen: set[str] = set()
        cur: dict[str, Any] | None = item
        while cur is not None and cur["id"] not in seen:
            seen.add(cur["id"])
            chain.append({
                "id": cur["id"],
                "title": cur.get("title", ""),
                "status": cur.get("status"),
            })
            dep = cur.get("depends_on")
            cur = by_id.get(dep) if dep else None
        chain.reverse()
        return chain

    chains = [
        _chain_for(it) for it in open_items if it.get("depends_on")
    ]
    chains = [c for c in chains if len(c) > 1]
    longest_chain = max((len(c) for c in chains), default=1)

    # Resource/file conflicts among open items (shared touches_resources).
    res_map: dict[str, list[str]] = {}
    for it in open_items:
        for res in parse_touches_resources(it.get("touches_resources")):
            res_map.setdefault(res, []).append(it["id"])
    conflicts = [
        {"resource": res, "item_ids": ids}
        for res, ids in sorted(res_map.items()) if len(ids) > 1
    ]

    # 890046a2 — two-path stall detection:
    #   "counter" — stall_count > 0 (incremented on explicit session close)
    #   "time"    — claimed_at older than _SPRINT_STALL_FLAG_HOURS, even when
    #               stall_count == 0 (catches abandoned sessions never cleanly
    #               closed, which is the common real-world failure mode)
    #   "both"    — both conditions are true simultaneously
    from datetime import datetime, timezone, timedelta
    _stall_cutoff = datetime.now(timezone.utc) - timedelta(hours=_SPRINT_STALL_FLAG_HOURS)

    def _is_time_stalled(item: dict[str, Any]) -> bool:
        raw = item.get("claimed_at")
        if not raw:
            return False
        try:
            # SQLite stores as "YYYY-MM-DD HH:MM:SS" (no TZ); treat as UTC.
            ca = datetime.fromisoformat(str(raw).replace(" ", "T"))
            if ca.tzinfo is None:
                ca = ca.replace(tzinfo=timezone.utc)
            return ca < _stall_cutoff
        except (ValueError, TypeError):
            return False

    stalls: list[dict[str, Any]] = []
    for it in open_items:
        sc = (it.get("stall_count") or 0) > 0
        st = _is_time_stalled(it)
        if not sc and not st:
            continue
        reason = "both" if (sc and st) else ("counter" if sc else "time")
        stalls.append({
            "id": it["id"],
            "title": it.get("title", ""),
            "stall_count": it.get("stall_count") or 0,
            "claimed_at": it.get("claimed_at"),
            "reason": reason,
        })

    groups = groups_info.get("groups", [])
    max_group = max((len(g) for g in groups), default=0)
    strategy = "parallel" if max_group > 1 else "sequential"
    summary = (
        f"{groups_info.get('eligible_count', 0)} eligible item(s) in "
        f"{groups_info.get('group_count', 0)} group(s) (max {max_group} parallel); "
        f"longest dependency chain {longest_chain}; {len(conflicts)} resource "
        f"conflict(s); {len(stalls)} stalled; "
        f"{len(groups_info.get('blocked', []))} blocked."
    )
    return {
        "version": version,
        "summary": summary,
        "recommended_strategy": strategy,
        "parallelism": {
            "group_count": groups_info.get("group_count", 0),
            "eligible_count": groups_info.get("eligible_count", 0),
            "max_parallel": max_group,
            "undeclared_count": groups_info.get("undeclared_count", 0),
            "groups": [
                [{"id": it["id"], "title": it.get("title", "")} for it in g]
                for g in groups
            ],
        },
        "dependency_chains": chains,
        "longest_chain": longest_chain,
        "file_conflicts": conflicts,
        "stalls": stalls,
        "blocked": groups_info.get("blocked", []),
        "running": groups_info.get("running", []),
    }


# ---------------------------------------------------------------------------
# d2430713 — complete_wave_gate: executor calls this AFTER running the gate's
# action list (push / deploy / wait / run_verification) to signal that a wave's
# gate has passed and unlock the next wave's items.  The only accepted evidence
# is the REAL structured run_verification result payload — a plain self-reported
# "I think it passed" boolean is explicitly rejected.
#
# Evidence contract (caller must supply AT LEAST ONE of):
#   verification_payload — the full dict returned by run_verification:
#       {status: "ok", exit_code: 0, passed: N, failed: 0, ...}
#   Both status=="ok" AND exit_code==0 must hold.  Any other value (failed run,
#   non-zero exit, error status, not_configured, not_connected) is rejected.
#
# On success this function:
#   1. Writes a row into wave_gate_results with the evidence snapshot.
#   2. Returns how many items in wave (wave_label + 1) are now unblocked
#      (i.e. pending/todo items in the NEXT wave that exist in the project).
#
# "Unblocking" in the current model is purely informational: there is no
# blocker_kind='wave_gate' mechanism yet — claim_sprint_item never checked wave.
# The gate result itself is the artefact: the next wave's executor reads
# wave_gate_results to confirm the prior wave's gate passed before claiming
# next-wave items, and a future add to claim_sprint_item can query this table.
# ---------------------------------------------------------------------------

# ed8e4524 — `version` (nullable) scopes a gate result to ONE sprint-version
# bucket; NULL is the legacy/project-wide bucket (unchanged pre-fix meaning).
# The UNIQUE constraint includes version so two DIFFERENT versions can each
# complete their OWN gate for a wave_label they happen to share. This DDL is
# the fallback safety net for a table created before the formal migration
# runs (see meridian.db.migrations._migrate_wave_gate_results /
# pg_adapter._migrate_pg_wave_gate_results, which are what actually create
# this table at init_db time and are kept in sync with this text) — it is a
# no-op on an already-existing table either way.
_WAVE_GATE_RESULTS_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS wave_gate_results ("
    "    id TEXT PRIMARY KEY,"
    "    project_id TEXT NOT NULL,"
    "    wave_label TEXT NOT NULL,"         # e.g. 'wave-1'
    "    version TEXT,"                     # NULL = unscoped/legacy bucket
    "    gate_passed INTEGER NOT NULL DEFAULT 1,"  # always 1 (rejected gates never write)
    "    exit_code INTEGER,"
    "    passed_count INTEGER,"
    "    failed_count INTEGER,"
    "    verification_status TEXT,"
    "    evidence_snapshot TEXT,"           # JSON of the full payload
    "    actor TEXT,"
    "    completed_at TEXT NOT NULL DEFAULT (datetime('now')),"
    "    UNIQUE(project_id, wave_label, version)"  # one gate result per project+wave+version
    ")"
)


async def _ensure_wave_gate_results_table(db: aiosqlite.Connection) -> None:
    """Idempotently create wave_gate_results (called inline, tolerates concurrent init)."""
    await db.execute(_WAVE_GATE_RESULTS_TABLE_DDL)


async def complete_wave_gate(
    db: aiosqlite.Connection,
    project_id: str,
    wave_label: str,
    verification_payload: dict[str, Any],
    actor: str | None = None,
    version: str | None = None,
) -> dict[str, Any]:
    """d2430713 — record a verified wave gate completion and report next-wave readiness.

    The caller MUST supply the full structured result dict from run_verification
    as ``verification_payload``.  The dict is validated server-side:
      * ``status`` must be ``"ok"`` (not "error", "not_configured", "not_connected").
      * ``exit_code`` must be exactly ``0`` (integer).  Non-zero means tests failed.

    Any other value raises ValueError with a clear diagnostic.  This means an
    executor cannot satisfy the gate by passing a fabricated or self-reported payload;
    only the genuine output of run_verification — which runs the REAL test suite on
    the caller's machine — is accepted.

    ``version`` (ed8e4524) scopes this gate completion to ONE sprint-version
    bucket, closing the cross-version leak where two different sprint versions
    that happen to reuse the SAME ``wave_label`` (e.g. both have a 'wave-2')
    could satisfy or unblock each other's gate. ``None`` (default) is the
    LEGACY/unscoped behavior — exactly the pre-fix, project-wide check —
    so a project with only one sprint version in play is unaffected. When
    given, the duplicate-gate check AND the next-wave-items query are both
    restricted to items/results stamped with this SAME version (mirroring
    ``handoff._resolve_session_sprint_version`` / 660314c1's checkpoint fix,
    which resolves an analogous scope for a session's pending items).

    On success a row is written to ``wave_gate_results`` and the function returns::

        {
            "gate_completed": True,
            "wave_label": "wave-1",
            "version": "v0.2.6",              # the resolved scope, or None
            "next_wave_label": "wave-2",      # None if no next wave exists
            "next_wave_item_count": <int>,    # how many pending/todo items in next wave
            "next_wave_item_ids": [...],
            "gate_id": "<uuid>",
        }

    Raises ValueError on evidence failure (bad payload) or if the gate for this
    wave (and version, when given) has already been completed.
    """
    # ed8e4524 — normalize "" / whitespace-only to None, same convention as
    # start_wave_run's version handling, so a legacy sprint_items.version of ""
    # (the NOT-NULL column's empty-bucket default) lines up with an unscoped
    # (NULL) wave_gate_results/configs row instead of silently mismatching it.
    version = (version or "").strip() or None

    # ── 1. Validate evidence ────────────────────────────────────────────────────
    if not isinstance(verification_payload, dict):
        raise ValueError(
            "complete_wave_gate requires a verification_payload dict (the full result "
            "from run_verification). Pass the dict directly — do not pass a boolean "
            "or a self-report."
        )

    v_status = verification_payload.get("status")
    v_exit = verification_payload.get("exit_code")
    v_passed = verification_payload.get("passed")
    v_failed = verification_payload.get("failed")

    # Reject non-ok statuses up front with a clear diagnostic so the caller
    # knows exactly what went wrong.
    if v_status == "not_configured":
        raise ValueError(
            "Wave gate rejected: run_verification returned status='not_configured' — "
            "no test_cmd is set for this project. Configure executor_config.test_cmd "
            "via set_executor_config, then actually run run_verification and pass its "
            "result here."
        )
    if v_status == "not_connected":
        raise ValueError(
            "Wave gate rejected: run_verification returned status='not_connected' — "
            "the tunnel is not active. Start meridian --tunnel locally, run "
            "run_verification so it executes the REAL test suite, and pass its result."
        )
    if v_status == "error":
        raise ValueError(
            f"Wave gate rejected: run_verification returned status='error' — "
            f"the test runner itself crashed or was not found. Fix the command, "
            f"re-run run_verification, and pass its result. "
            f"Payload: {verification_payload!r}"
        )
    if v_status != "ok":
        raise ValueError(
            f"Wave gate rejected: verification_payload.status must be 'ok' but got "
            f"{v_status!r}. Only a genuinely successful run_verification result "
            f"(status='ok', exit_code=0) satisfies the gate."
        )
    if v_exit != 0:
        raise ValueError(
            f"Wave gate rejected: verification_payload.exit_code must be 0 but got "
            f"{v_exit!r} (failed={v_failed!r}). Fix the failures, re-run "
            f"run_verification, and pass the result when all tests pass."
        )

    # ── 2. Check for duplicate gate completion ────────────────────────────────────
    await _ensure_wave_gate_results_table(db)
    # ed8e4524 — scope the duplicate check to `version` when given (a DIFFERENT
    # version's completed row for the same wave_label must NOT be reported as
    # "already completed" here — that was the exact cross-version block bug).
    # version=None keeps the original unscoped match (any row for this
    # project+wave_label, regardless of stored version, counts as a dup).
    _dup_clauses = ["project_id = ?", "wave_label = ?"]
    _dup_params: list[Any] = [project_id, wave_label]
    if version is not None:
        _dup_clauses.append("version = ?")
        _dup_params.append(version)
    async with db.execute(
        f"SELECT id FROM wave_gate_results WHERE {' AND '.join(_dup_clauses)}",
        _dup_params,
    ) as _dup_cur:
        _dup_row = await _dup_cur.fetchone()
    if _dup_row is not None:
        existing_id = _dup_row[0] if not isinstance(_dup_row, dict) else _dup_row["id"]
        _version_note = f" (version {version!r})" if version else ""
        raise ValueError(
            f"Wave gate for {wave_label!r} on project {project_id!r}{_version_note} "
            f"has already been completed (gate_id={existing_id!r}). Each wave gate "
            f"may only be completed once."
        )

    # ── 3. Determine the next wave label ─────────────────────────────────────────
    # wave_label is expected to be 'wave-N'; next wave is 'wave-(N+1)'.
    next_wave_label: str | None = None
    _parts = wave_label.rsplit("-", 1)
    if len(_parts) == 2 and _parts[1].isdigit():
        next_wave_label = f"{_parts[0]}-{int(_parts[1]) + 1}"

    # ── 4. Find next-wave items (informational) ───────────────────────────────────
    # ed8e4524 — THE confirmed defect: this query used to filter only on
    # project_id + wave, so two sprint versions sharing the same wave label
    # (e.g. both have a 'wave-2') would leak each other's items into
    # next_wave_item_ids. version=None preserves the exact prior unscoped
    # query (matches sprint_items.get_sprint_items's own `if version is not
    # None` convention for "no filter means every version").
    next_wave_item_ids: list[str] = []
    if next_wave_label is not None:
        _nw_clauses = ["project_id = ?", "wave = ?", "status IN ('pending', 'todo')"]
        _nw_params: list[Any] = [project_id, next_wave_label]
        if version is not None:
            _nw_clauses.append("version = ?")
            _nw_params.append(version)
        async with db.execute(
            f"SELECT id FROM sprint_items WHERE {' AND '.join(_nw_clauses)} "
            f"ORDER BY added_at",
            _nw_params,
        ) as _nw_cur:
            _nw_rows = await _nw_cur.fetchall()
        next_wave_item_ids = [
            (r["id"] if isinstance(r, dict) else r[0]) for r in _nw_rows
        ]

    # ── 5. Write gate result ──────────────────────────────────────────────────────
    gate_id = _new_id()
    evidence_snapshot = json.dumps(verification_payload)
    await db.execute(
        "INSERT INTO wave_gate_results "
        "(id, project_id, wave_label, version, gate_passed, exit_code, passed_count, "
        " failed_count, verification_status, evidence_snapshot, actor) "
        "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
        (
            gate_id,
            project_id,
            wave_label,
            version,
            v_exit,
            v_passed,
            v_failed if v_failed is not None else 0,
            v_status,
            evidence_snapshot,
            actor,
        ),
    )
    await db.commit()

    return {
        "gate_completed": True,
        "wave_label": wave_label,
        "version": version,
        "next_wave_label": next_wave_label,
        "next_wave_item_count": len(next_wave_item_ids),
        "next_wave_item_ids": next_wave_item_ids,
        "gate_id": gate_id,
    }


# ---------------------------------------------------------------------------
# 74a8f420 — configure_wave_gate: deterministic, on-the-fly-configurable
# action pipelines attached to a wave or wave-range, ENFORCED STRUCTURALLY by
# claim_sprint_item (see the WAVE_GATE_PENDING check below) rather than being
# advisory /goal prose. complete_wave_gate (d2430713, above) already recorded
# *evidence* that a gate passed; this section adds the missing piece it
# explicitly called out: a real config table so claim_sprint_item can look up
# "is there a configured-but-unpassed gate between me and this item's wave"
# instead of trusting the executor to have read the /goal text.
#
# A gate config is keyed by its ``wave_end`` — the boundary wave after which
# the pipeline must run. ``wave_start`` documents the (possibly multi-wave)
# range the gate covers (e.g. wave_start='wave-1', wave_end='wave-3' — one
# gate checkpoint after waves 1-3, not one gate per wave). Only ``wave_end``
# is used for enforcement: any item whose numeric wave sorts strictly after
# wave_end, on the same "prefix-N" label family (e.g. 'wave-4' vs
# 'wave-3'), is blocked at claim time until a matching wave_gate_results row
# exists (written by complete_wave_gate once the pipeline's real
# run_verification evidence is supplied).
# ---------------------------------------------------------------------------

# The only action types a gate pipeline may declare. push_dev/push_main/deploy
# are executed by the executor via the trigger_workflow MCP tool; run_verification
# maps 1:1 onto the run_verification MCP tool (whose real output is what
# complete_wave_gate requires as evidence); wait is a plain pause step.
_VALID_WAVE_GATE_ACTIONS = frozenset({
    "push_dev", "push_main", "deploy", "wait", "run_verification",
})

# ed8e4524 — `version` (nullable) scopes a gate config to ONE sprint-version
# bucket, same convention as wave_gate_results.version above. This DDL is the
# fallback safety net (see the note above _WAVE_GATE_RESULTS_TABLE_DDL — kept
# in sync with meridian.db.migrations._migrate_wave_gate_configs /
# pg_adapter._migrate_pg_wave_gate_configs, the actual creation path).
_WAVE_GATE_CONFIGS_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS wave_gate_configs ("
    "    id TEXT PRIMARY KEY,"
    "    project_id TEXT NOT NULL,"
    "    wave_start TEXT NOT NULL,"     # first wave covered by this gate (documentation)
    "    wave_end TEXT NOT NULL,"       # boundary wave — enforcement key
    "    version TEXT,"                 # NULL = unscoped/legacy bucket
    "    actions TEXT NOT NULL,"        # JSON array of {"type": ..., ...params}
    "    actor TEXT,"
    "    created_at TEXT NOT NULL DEFAULT (datetime('now')),"
    "    updated_at TEXT NOT NULL DEFAULT (datetime('now')),"
    "    UNIQUE(project_id, wave_end, version)"  # one pipeline per boundary wave+version
    ")"
)


async def _ensure_wave_gate_configs_table(db: aiosqlite.Connection) -> None:
    """Idempotently create wave_gate_configs (called inline, tolerates concurrent init)."""
    await db.execute(_WAVE_GATE_CONFIGS_TABLE_DDL)


def _split_wave_label(wave_label: str | None) -> tuple[str | None, int | None]:
    """Parse a 'prefix-N' wave label (e.g. 'wave-3') into (prefix, N).

    Returns (None, None) for anything that doesn't match — callers treat that
    as "not comparable" and fail open (no gate enforcement possible without a
    parseable ordering), mirroring complete_wave_gate's own next-wave-label
    parsing.
    """
    if not wave_label:
        return (None, None)
    _parts = str(wave_label).rsplit("-", 1)
    if len(_parts) == 2 and _parts[1].isdigit():
        return (_parts[0], int(_parts[1]))
    return (None, None)


async def configure_wave_gate(
    db: aiosqlite.Connection,
    project_id: str,
    wave_end: str,
    actions: list[dict[str, Any]],
    wave_start: str | None = None,
    actor: str | None = None,
    version: str | None = None,
) -> dict[str, Any]:
    """74a8f420 — configure (or on-the-fly reconfigure) a wave gate's action pipeline.

    ``wave_end`` (e.g. 'wave-3') is the boundary: claim_sprint_item structurally
    refuses to claim any item whose wave sorts strictly after wave_end until a
    matching ``wave_gate_results`` row exists (written by complete_wave_gate).
    ``wave_start`` (defaults to wave_end) documents the range covered — e.g.
    wave_start='wave-1', wave_end='wave-3' is one deploy checkpoint after three
    waves' worth of items, not three separate gates.

    ``actions`` is the deterministic pipeline: an ordered, non-empty list of
    dicts, each with a ``type`` in push_dev | push_main | deploy | wait |
    run_verification (extra keys — e.g. {"type": "wait", "seconds": 30} — are
    preserved verbatim for the executor to read).

    ``version`` (ed8e4524) scopes this gate CONFIG to ONE sprint-version
    bucket — the same class of fix as ``complete_wave_gate``'s ``version``
    param (see its docstring). ``None`` (default) is the legacy/unscoped
    behavior: matches ANY existing config/result row for this ``wave_end``,
    exactly the pre-fix project-wide semantics. When given, both the
    immutability check (has this version's gate already passed?) and the
    upsert lookup (does this version already have a config for this
    wave_end?) are restricted to that SAME version, so version B can
    configure and later complete its own ``wave_end`` boundary independently
    of version A reusing the same label.

    Re-configuring an already-configured (but not yet passed) ``wave_end`` is
    an upsert — this is the "on-the-fly-configurable" half of the spec: a
    planner can revise the pipeline for a wave boundary right up until an
    executor actually completes it. Once wave_gate_results has a matching row
    for wave_end (and version, when given) the config is immutable (raises
    ValueError) — rewriting a passed gate's pipeline after the fact would
    silently invalidate evidence that claim_sprint_item already relied on to
    unblock items.
    """
    # ed8e4524 — same "" -> None normalization as complete_wave_gate; see that
    # function's docstring for why.
    version = (version or "").strip() or None
    wave_end = str(wave_end or "").strip()
    if not wave_end:
        raise ValueError("configure_wave_gate requires a non-empty wave_end")
    wave_start = str(wave_start).strip() if wave_start else wave_end
    if not isinstance(actions, list) or not actions:
        raise ValueError(
            "configure_wave_gate requires a non-empty actions list — the "
            "deterministic pipeline (push_dev/push_main/deploy/wait/"
            "run_verification) that must run before the next wave unlocks."
        )
    _normalized: list[dict[str, Any]] = []
    for _i, _action in enumerate(actions):
        if not isinstance(_action, dict) or not _action.get("type"):
            raise ValueError(
                f"configure_wave_gate: actions[{_i}] must be a dict with a "
                f"'type' key, got {_action!r}"
            )
        _atype = str(_action["type"]).strip().lower()
        if _atype not in _VALID_WAVE_GATE_ACTIONS:
            raise ValueError(
                f"configure_wave_gate: actions[{_i}].type={_atype!r} is not "
                f"one of the supported actions: {sorted(_VALID_WAVE_GATE_ACTIONS)}"
            )
        _normalized.append({**_action, "type": _atype})

    await _ensure_wave_gate_configs_table(db)
    await _ensure_wave_gate_results_table(db)

    # A passed gate's config is immutable — see docstring. Scoped to `version`
    # when given (ed8e4524): version=None matches ANY existing result row for
    # this wave_end (unscoped, exactly the pre-fix behavior); an explicit
    # version only matches a result row completed under that SAME version.
    _passed_clauses = ["project_id = ?", "wave_label = ?"]
    _passed_params: list[Any] = [project_id, wave_end]
    if version is not None:
        _passed_clauses.append("version = ?")
        _passed_params.append(version)
    async with db.execute(
        f"SELECT id FROM wave_gate_results WHERE {' AND '.join(_passed_clauses)}",
        _passed_params,
    ) as _res_cur:
        _already_passed = await _res_cur.fetchone()
    if _already_passed is not None:
        _version_note = f" (version {version!r})" if version else ""
        raise ValueError(
            f"Wave gate for {wave_end!r} on project {project_id!r}{_version_note} "
            "has already completed — its pipeline is immutable. Configure a NEW "
            "wave_end boundary instead of reconfiguring a passed gate."
        )

    _actions_json = json.dumps(_normalized)
    # ed8e4524 — same version scoping for the upsert lookup: a config row
    # belonging to a DIFFERENT version (or the unscoped legacy bucket) must
    # never be silently overwritten by this call.
    _cfg_clauses = ["project_id = ?", "wave_end = ?"]
    _cfg_params: list[Any] = [project_id, wave_end]
    if version is not None:
        _cfg_clauses.append("version = ?")
        _cfg_params.append(version)
    async with db.execute(
        f"SELECT id FROM wave_gate_configs WHERE {' AND '.join(_cfg_clauses)}",
        _cfg_params,
    ) as _cfg_cur:
        _existing = await _cfg_cur.fetchone()
    if _existing is not None:
        _config_id = _existing["id"] if isinstance(_existing, dict) else _existing[0]
        await db.execute(
            "UPDATE wave_gate_configs SET wave_start = ?, actions = ?, actor = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (wave_start, _actions_json, actor, _config_id),
        )
    else:
        _config_id = _new_id()
        await db.execute(
            "INSERT INTO wave_gate_configs "
            "(id, project_id, wave_start, wave_end, version, actions, actor) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_config_id, project_id, wave_start, wave_end, version, _actions_json, actor),
        )
    await db.commit()

    return {
        "configured": True,
        "gate_config_id": _config_id,
        "project_id": project_id,
        "wave_start": wave_start,
        "wave_end": wave_end,
        "version": version,
        "actions": _normalized,
    }


async def get_wave_gate_configs(
    db: aiosqlite.Connection, project_id: str, version: str | None = None,
) -> list[dict[str, Any]]:
    """Read-only: list every configured wave gate for a project (oldest first),
    each annotated with ``gate_passed`` (whether wave_gate_results already has
    a matching row) so callers don't need a second query to know what's still
    pending.

    ``version`` (ed8e4524) optionally restricts the listing to gates
    EXPLICITLY configured under that exact sprint-version bucket. ``None``
    (default) returns every configured gate regardless of its stored version
    — the unchanged, original behavior every existing caller
    (capability_contract.build_capability_contract, executor_contract,
    handoff._build_quick_start_goal and friends) relies on, since none of
    them pass this parameter.
    """
    await _ensure_wave_gate_configs_table(db)
    await _ensure_wave_gate_results_table(db)
    version = (version or "").strip() or None
    _clauses = ["project_id = ?"]
    _params: list[Any] = [project_id]
    if version is not None:
        _clauses.append("version = ?")
        _params.append(version)
    async with db.execute(
        f"SELECT * FROM wave_gate_configs WHERE {' AND '.join(_clauses)} "
        f"ORDER BY created_at",
        _params,
    ) as _cur:
        _rows = await _cur.fetchall()
    out: list[dict[str, Any]] = []
    for _row in _rows:
        _cfg = _row_to_dict(_row) or {}
        try:
            _cfg["actions"] = json.loads(_cfg.get("actions") or "[]")
        except (TypeError, ValueError):
            _cfg["actions"] = []
        # ed8e4524 — the passed-check is scoped to THIS row's own stored
        # version (not the listing filter above): an unscoped (NULL) config
        # matches ANY results row for the wave_label (project-wide, exactly
        # the pre-fix behavior); a version-scoped config only matches a
        # results row completed under that SAME version.
        _row_version = _cfg.get("version")
        _res_clauses = ["project_id = ?", "wave_label = ?"]
        _res_params: list[Any] = [project_id, _cfg.get("wave_end")]
        if _row_version is not None:
            _res_clauses.append("version = ?")
            _res_params.append(_row_version)
        async with db.execute(
            f"SELECT id FROM wave_gate_results WHERE {' AND '.join(_res_clauses)}",
            _res_params,
        ) as _res_cur:
            _cfg["gate_passed"] = (await _res_cur.fetchone()) is not None
        out.append(_cfg)
    return out


async def _get_blocking_wave_gate(
    db: aiosqlite.Connection, project_id: str, item_wave: str | None,
    version: str | None = None,
) -> dict[str, Any] | None:
    """Return the lowest-boundary configured-but-unpassed wave gate that
    structurally blocks claiming an item in ``item_wave``, or None if nothing
    blocks it (no wave on the item, no configs, an unparseable wave label,
    every configured boundary at-or-below this wave has already passed, or
    every boundary-scoped config belongs to a DIFFERENT sprint version).

    This is the function claim_sprint_item calls to turn wave gates from
    advisory /goal prose into a real, structural claim-time block.

    ``version`` (ed8e4524, pass the CLAIMED ITEM's own ``item.get("version")``)
    is the item's sprint-version bucket. A gate config with an EXPLICIT
    stored version only applies to — and can only be satisfied by evidence
    from — items/completions in that SAME version, closing the cross-version
    leak where completing version A's 'wave-1' gate could unblock version B's
    'wave-2' items just because they share the label. A config with NO stored
    version (the default when configure_wave_gate/complete_wave_gate are
    called without ``version`` — still the common, single-sprint-version
    case) remains PROJECT-WIDE and applies unconditionally regardless of the
    item's own version, exactly like before this fix — this is what keeps a
    project that never explicitly version-scopes its wave-gate calls
    (including one whose items still carry an ordinary version string like
    'v1') working unchanged.
    """
    _item_prefix, _item_num = _split_wave_label(item_wave)
    if _item_num is None:
        return None
    await _ensure_wave_gate_configs_table(db)
    await _ensure_wave_gate_results_table(db)
    version = (version or "").strip() or None
    async with db.execute(
        "SELECT * FROM wave_gate_configs WHERE project_id = ?",
        (project_id,),
    ) as _cur:
        _configs = await _cur.fetchall()
    _blocking: dict[str, Any] | None = None
    _blocking_num: int | None = None
    for _row in _configs:
        _cfg = _row_to_dict(_row) or {}
        _cfg_version = _cfg.get("version")
        if _cfg_version is not None and _cfg_version != version:
            continue  # a version-scoped config that isn't THIS item's version
        _cfg_prefix, _cfg_num = _split_wave_label(_cfg.get("wave_end"))
        if _cfg_num is None or _cfg_prefix != _item_prefix or _cfg_num >= _item_num:
            continue  # not a boundary strictly before this item's wave
        _res_clauses = ["project_id = ?", "wave_label = ?"]
        _res_params: list[Any] = [project_id, _cfg.get("wave_end")]
        if _cfg_version is not None:
            _res_clauses.append("version = ?")
            _res_params.append(_cfg_version)
        async with db.execute(
            f"SELECT id FROM wave_gate_results WHERE {' AND '.join(_res_clauses)}",
            _res_params,
        ) as _res_cur:
            _passed = await _res_cur.fetchone()
        if _passed is not None:
            continue  # this boundary's gate already passed (for this version)
        if _blocking_num is None or _cfg_num < _blocking_num:
            try:
                _cfg["actions"] = json.loads(_cfg.get("actions") or "[]")
            except (TypeError, ValueError):
                _cfg["actions"] = []
            _blocking = _cfg
            _blocking_num = _cfg_num
    return _blocking


# ---------------------------------------------------------------------------
# b108f2e0 — typed blocker triage: project-configurable executor_blocker_policy
# persistence + DB-backed evaluation, layered on the pure meridian.blocker_policy
# module. Stored inside the EXISTING executor_config JSON blob (no new table /
# migration needed — mirrors how test_cmd and other free-form executor knobs
# already persist) under the "blocker_policy" key, shaped as either a plain
# string (project-wide default) or {"default": str, "by_version": {v: str}}
# once a caller sets a version-scoped override. Auditable via the EXISTING
# append-only action_audit_log table (record_action_audit_event) — no new
# audit storage either.
# ---------------------------------------------------------------------------

_BLOCKER_POLICY_CONFIG_KEY = "blocker_policy"


def _resolve_stored_blocker_policy(raw: Any, *, version: "str | None") -> tuple[Any, str]:
    """Resolve the raw ``executor_config["blocker_policy"]`` value for one
    ``version`` bucket. Returns ``(value, source)`` where ``source`` is
    ``"version"`` (an explicit per-version override matched), ``"default"``
    (the project-wide default applied), or ``"unset"`` (nothing stored at
    all — caller falls back to ``blocker_policy.DEFAULT_POLICY``).
    """
    if isinstance(raw, dict):
        by_version = raw.get("by_version")
        if version and isinstance(by_version, dict) and version in by_version:
            return by_version[version], "version"
        if "default" in raw:
            return raw.get("default"), "default"
        return None, "unset"
    if isinstance(raw, str):
        return raw, "default"
    return None, "unset"


async def get_project_blocker_policy(
    db: aiosqlite.Connection, project_id: str, *, version: "str | None" = None
) -> dict[str, Any]:
    """Return the effective ``executor_blocker_policy`` for a project (b108f2e0).

    A project that never configured one gets :data:`blocker_policy.DEFAULT_POLICY`
    back, never an error — mirrors ``get_project_capability_manifest``'s "no
    manifest -> empty profile" contract. A corrupted/invalid stored value
    (should not happen — ``set_project_blocker_policy`` validates at write
    time — but defends against a hand-edited DB row) degrades to the safe
    default rather than raising, since this is a READ path other mandatory
    calls (board snapshots, handoffs) depend on.
    """
    cfg = await get_executor_config(db, project_id)
    raw = cfg.get(_BLOCKER_POLICY_CONFIG_KEY)
    value, source = _resolve_stored_blocker_policy(raw, version=version)
    try:
        normalized = _blocker_policy.normalize_policy(value)
    except _blocker_policy.BlockerPolicyError:
        normalized = _blocker_policy.DEFAULT_POLICY
        source = "unset"
    if value is None:
        source = "unset"
    return {
        "project_id": project_id,
        "version": version,
        "policy": normalized,
        "source": source,
    }


async def set_project_blocker_policy(
    db: aiosqlite.Connection,
    project_id: str,
    policy: str,
    *,
    version: "str | None" = None,
    actor: "str | None" = None,
) -> dict[str, Any]:
    """Validate, persist, and audit-log a project's ``executor_blocker_policy``
    (b108f2e0). Raises ``blocker_policy.BlockerPolicyError`` for an invalid
    policy value, ``ValueError`` if the project does not exist (surfaced by
    the underlying ``set_executor_config`` -> ``update_project_settings``
    call).

    ``version`` (optional) scopes the override to one sprint-version bucket,
    leaving the project-wide default (and any OTHER version's override)
    untouched — the executor_config blob is READ, merged, and WRITTEN BACK
    whole (``set_executor_config`` replaces the entire blob, so a naive write
    here would silently drop unrelated keys like ``test_cmd``).
    """
    normalized = _blocker_policy.normalize_policy(policy)
    cfg = await get_executor_config(db, project_id)
    raw = cfg.get(_BLOCKER_POLICY_CONFIG_KEY)
    stored: dict[str, Any] = dict(raw) if isinstance(raw, dict) else (
        {"default": raw} if isinstance(raw, str) else {}
    )
    if version:
        by_version = dict(stored.get("by_version") or {})
        by_version[version] = normalized
        stored["by_version"] = by_version
    else:
        stored["default"] = normalized
    new_cfg = dict(cfg)
    new_cfg[_BLOCKER_POLICY_CONFIG_KEY] = stored
    await set_executor_config(db, project_id, new_cfg)
    try:
        # Lazy import: record_action_audit_event lives in .workspace, which
        # itself imports names FROM this module (_sprint_item_slug_base /
        # _sprint_item_nickname_base) — a top-level import here would be
        # circular at package-init time. Safe at call time, long after both
        # modules have finished loading (mirrors capability_contract.py's own
        # lazy `from . import executor_contract` pattern).
        from . import workspace as _workspace_module  # noqa: PLC0415

        await _workspace_module.record_action_audit_event(
            db,
            "blocker_policy_set",
            project_id=project_id,
            actor=actor,
            detail=json.dumps({"policy": normalized, "version": version}),
        )
    except Exception:  # noqa: BLE001 — a dropped audit entry must not break the write
        pass
    return await get_project_blocker_policy(db, project_id, version=version)


async def evaluate_board_blockers(
    db: aiosqlite.Connection,
    project_id: str,
    *,
    version: "str | None" = None,
    items: "list[dict[str, Any]] | None" = None,
    signals: "dict[str, dict[str, Any]] | None" = None,
) -> dict[str, Any]:
    """DB-backed whole-run blocker decision for a project (b108f2e0):
    fetches the live (non-done) board, the project's configured
    ``executor_blocker_policy``, and combines them via the pure
    ``blocker_policy.classify_and_evaluate`` combinator.

    ``items`` lets a caller that already fetched the SAME live-item list
    (e.g. ``build_board_snapshot``'s own ``raw_items``, or a handoff's
    pending-item fetch) pass it straight through instead of this function
    re-querying — the strongest identical-data guarantee, mirroring
    ``capability_contract.build_capability_contract``'s own ``items`` kwarg.
    When omitted, self-fetches every non-``done`` item for ``project_id``
    (optionally scoped to ``version``), matching
    ``board_snapshot.build_board_snapshot``'s own "non-done is deliberately
    literal" filter so the two agree on what's "live" for the same board.

    ``signals`` is passed straight through to
    ``blocker_policy.classify_and_evaluate`` — per-item classification
    overrides (dependency/tool/security/etc. signals) a caller has already
    verified. Never raises: a malformed stored policy degrades to the safe
    default (see :func:`get_project_blocker_policy`); this function itself
    has no additional failure mode beyond the DB fetch, which callers should
    still guard per this codebase's "best-effort enrichment" convention.
    """
    live_items = items
    if live_items is None:
        raw_items = await get_sprint_items(db, project_id, version=version)
        live_items = [it for it in raw_items if (it.get("status") or "") != "done"]
    policy_row = await get_project_blocker_policy(db, project_id, version=version)
    decision = _blocker_policy.classify_and_evaluate(
        live_items, signals=signals, policy=policy_row["policy"]
    )
    decision["policy_source"] = policy_row.get("source")
    return decision


async def block_sprint_items_for_systemic_invalidation(
    db: aiosqlite.Connection,
    project_id: str,
    item_ids: "list[str]",
    *,
    wave_run_id: str,
    reason_code: str,
    basis: str,
    actor: "str | None" = None,
) -> dict[str, Any]:
    """cc3864bd — mark every LIVE item in ``item_ids`` blocked by a wave
    run's systemic invalidation, via the existing ``blocker_kind`` hard-gate
    mechanism (:data:`_VALID_SPRINT_BLOCKER_KINDS`, same claim-time
    enforcement point as ``'superseded'`` in :func:`claim_sprint_item`) —
    never a new status value, so no existing status-dependent invariant
    elsewhere in this module changes shape. Called from
    :func:`meridian.db.wave_runs.abort_wave_run_systemic`; kept here (not
    there) because it is fundamentally a sprint-item write, matching this
    module's existing division of labor with ``wave_runs.py``.

    Preserves independent completed evidence: an item already
    ``status in {'done', 'skipped'}`` is NEVER touched — reported separately
    under ``preserved_item_ids``. The entire point of quarantining rather
    than reverting a systemically-invalidated run is that work already
    verified independently STAYS verified.

    Project isolation: an id that does not resolve to a live item inside
    ``project_id`` is silently skipped (reported under
    ``skipped_other_project_or_missing_ids``) rather than raising. A wave
    run's own ``item_ids``/children are already project-scoped by
    construction, but a caller-supplied ``evidence.affected_item_ids`` is
    not trusted input — this is what stops one project's systemic
    invalidation from ever reaching into another project's board.

    Idempotent: an item already ``blocker_kind == 'systemic_invalidated_run'``
    is left completely untouched (no duplicate write, no duplicate cache
    invalidation) but still counted in ``blocked_item_ids`` — calling this
    twice with the same inputs is a no-op the second time, matching
    :func:`meridian.db.wave_runs.abort_wave_run_systemic`'s own idempotent
    contract.

    Returns ``{blocked_item_ids, preserved_item_ids,
    skipped_other_project_or_missing_ids}`` (each sorted).
    """
    blocked: list[str] = []
    preserved: list[str] = []
    skipped: list[str] = []
    marker = (
        f"[blocker_kind=systemic_invalidated_run wave_run={wave_run_id} "
        f"reason_code={reason_code}] {basis}"
    )
    for item_id in item_ids:
        item = await get_sprint_item(db, item_id)
        if item is None or item.get("project_id") != project_id:
            skipped.append(item_id)
            continue
        if (item.get("status") or "") in ("done", "skipped"):
            preserved.append(item_id)
            continue
        if (item.get("blocker_kind") or "").strip() == "systemic_invalidated_run":
            blocked.append(item_id)
            continue
        existing_notes = (item.get("notes") or "").strip()
        new_notes = f"{marker}\n\n{existing_notes}" if existing_notes else marker
        await patch_sprint_item(
            db, project_id, item_id,
            blocker_kind="systemic_invalidated_run",
            notes=new_notes,
        )
        blocked.append(item_id)
    return {
        "blocked_item_ids": sorted(blocked),
        "preserved_item_ids": sorted(preserved),
        "skipped_other_project_or_missing_ids": sorted(skipped),
    }


# ---------------------------------------------------------------------------
# 0d95003f — explicit, audited cross-project sprint-item reclassification +
# a read-only dependency-mismatch audit scanner.
#
# The item's full ask spans sessions, tasks, notes, proposals, proposal
# evidence, pointers, handoff bodies/pending goals, generated files, Redis
# keys, and index shards. This section covers SPRINT ITEMS specifically —
# the record type most directly tied to executor handoffs and completion
# ("quarantine ambiguous or foreign records so they cannot enter an executor
# handoff or be marked complete"). The other record types are explicit,
# documented follow-up, not attempted here.
#
# Mirrors two already-proven patterns in this codebase rather than inventing
# new ones: move_workspace_note_to_project's verify-then-write shape (this
# module's sibling in workspace.py) for the move itself, and
# set_project_blocker_policy's lazy-imported record_action_audit_event call
# (same file, above) for the audit trail.
# ---------------------------------------------------------------------------

CROSS_PROJECT_MOVE_EVENT_TYPE = "sprint_item_cross_project_move"


async def move_sprint_item_to_project(
    db: aiosqlite.Connection,
    item_id: str,
    source_project_id: str,
    destination_project_id: str,
    *,
    actor: "str | None",
    reason: "str | None",
) -> dict[str, Any]:
    """Explicit, audited, idempotent reclassification of ONE sprint item from
    ``source_project_id`` to ``destination_project_id`` (0d95003f).

    Never infers a destination from title/path — both project ids are
    caller-supplied and both are verified against real rows before anything
    is written. Returns a structured result, never raises for an expected
    condition::

        {"moved": bool, "item": dict | None, "error": str | None}

    Failure modes (all ``moved=False``, ``item=None``):

    * empty ``reason``/``actor`` — mirrors every other audited-override
      pattern in this codebase (code_intel_receipt, tool_discovery): an
      unattributed, unexplained move is refused.
    * ``"item not found"`` — no such sprint item.
    * item's actual ``project_id`` does not match the supplied
      ``source_project_id`` — refusing here (rather than moving anyway)
      prevents a stale-caller-state race from silently reassigning the
      wrong item.
    * ``"destination project not found"`` — never creates a destination.

    Idempotent: if the item is ALREADY on ``destination_project_id``, returns
    ``{"moved": False, "item": <the item, unchanged>, "error": None}`` — a
    repeat call is a safe no-op, not an error.

    Audit: on an actual move, records a ``sprint_item_cross_project_move``
    action_audit_log event with item_id/source/destination/actor/reason —
    best-effort (a dropped audit row must never undo an otherwise-successful,
    already-committed move; mirrors set_project_blocker_policy's identical
    guard above).

    Scope: moves the sprint_items row itself only. A ``depends_on`` pointing
    at an item that stays behind in the source project becomes a genuine
    cross-project dependency — surfaced by
    :func:`find_cross_project_dependency_mismatches`, not silently repaired
    here (auto-repairing a dependency graph risks reordering work the human
    never asked to reorder; that is deliberately a human/audited decision,
    not an automatic side effect of a move).
    """
    _reason = (reason or "").strip()
    if not _reason:
        return {"moved": False, "item": None, "error": "reason is required and must be non-empty."}
    _actor = (actor or "").strip()
    if not _actor:
        return {"moved": False, "item": None, "error": "actor is required and must be non-empty."}

    item = await get_sprint_item(db, item_id)
    if item is None:
        return {"moved": False, "item": None, "error": "item not found"}
    if item.get("project_id") != source_project_id:
        return {
            "moved": False, "item": None,
            "error": (
                f"item's actual project_id ({item.get('project_id')!r}) does not match "
                f"the supplied source_project_id ({source_project_id!r}) — refusing to "
                "move based on stale/incorrect caller state."
            ),
        }
    if item.get("project_id") == destination_project_id:
        return {"moved": False, "item": item, "error": None}
    if await get_project(db, destination_project_id) is None:
        return {"moved": False, "item": None, "error": "destination project not found"}

    await db.execute(
        "UPDATE sprint_items SET project_id = ? WHERE id = ?",
        (destination_project_id, item_id),
    )
    await db.commit()
    moved_item = await get_sprint_item(db, item_id)

    try:
        # Lazy import: same circularity reason as set_project_blocker_policy's
        # identical pattern above (workspace.py imports names FROM this module).
        from . import workspace as _workspace_module  # noqa: PLC0415

        await _workspace_module.record_action_audit_event(
            db, CROSS_PROJECT_MOVE_EVENT_TYPE,
            project_id=destination_project_id,
            actor=_actor,
            detail=json.dumps({
                "item_id": item_id,
                "source_project_id": source_project_id,
                "destination_project_id": destination_project_id,
                "reason": _reason,
            }),
        )
    except Exception:  # noqa: BLE001 — a dropped audit entry must never undo the move
        pass

    return {"moved": True, "item": moved_item, "error": None}


async def find_cross_project_dependency_mismatches(
    db: aiosqlite.Connection, project_id: str,
) -> list[dict[str, Any]]:
    """Read-only, non-destructive audit (0d95003f): sprint items in
    ``project_id`` whose ``depends_on`` points at an item belonging to a
    DIFFERENT project. Never mutates anything.

    A cross-project dependency is a genuine structural mismatch: the
    dependent item's own wave/claim ordering assumes the depended-on item's
    lifecycle is visible in the SAME project's board, which it is not once
    the two diverge. Returns one entry per mismatch::

        {"item_id", "item_project_id", "depends_on_id", "depends_on_project_id"}

    An empty list means no mismatches found — not "not checked"; the scan
    always covers every item currently in ``project_id`` with a non-null
    ``depends_on``.
    """
    mismatches: list[dict[str, Any]] = []
    async with db.execute(
        "SELECT id, depends_on FROM sprint_items WHERE project_id = ? AND depends_on IS NOT NULL",
        (project_id,),
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        row_d = _row_to_dict(row)
        dep_id = row_d.get("depends_on")
        if not dep_id:
            continue
        dep_item = await get_sprint_item(db, dep_id)
        if dep_item is None:
            continue  # dangling depends_on is a different, pre-existing concern
        dep_project_id = dep_item.get("project_id")
        if dep_project_id != project_id:
            mismatches.append({
                "item_id": row_d.get("id"),
                "item_project_id": project_id,
                "depends_on_id": dep_id,
                "depends_on_project_id": dep_project_id,
            })
    return mismatches


async def audit_and_quarantine_sprint_item_dependency_mismatches(
    db: aiosqlite.Connection, project_id: str, *, actor: str,
) -> dict[str, Any]:
    """Run find_cross_project_dependency_mismatches and flag each mismatch
    found via the generic cross-project quarantine mechanism in
    meridian/db/workspace.py (0d95003f).

    The ONLY mutation this performs is an audited quarantine event per
    mismatch — it never moves, deletes, or otherwise repairs anything
    (repairing the dependency graph is deliberately left a human/audited
    decision, same reasoning find_cross_project_dependency_mismatches'
    docstring already gives for not auto-repairing). Safe to call
    repeatedly: quarantine_cross_project_record is idempotent, so re-running
    this against an unchanged board produces the same open entries rather
    than duplicating them.

    Returns::

        {"mismatches": [...], "quarantined": [...]}

    ``mismatches`` is find_cross_project_dependency_mismatches' full,
    unfiltered result for this run. ``quarantined`` holds only the entries
    NEWLY flagged this call (an already-open entry from a prior run is
    still present in ``mismatches`` but omitted from ``quarantined``, since
    nothing changed for it).
    """
    from . import workspace as _workspace_module  # noqa: PLC0415

    mismatches = await find_cross_project_dependency_mismatches(db, project_id)
    quarantined: list[dict[str, Any]] = []
    for mismatch in mismatches:
        result = await _workspace_module.quarantine_cross_project_record(
            db,
            "sprint_item",
            mismatch["item_id"],
            mismatch["item_project_id"],
            reason=(
                f"depends_on {mismatch['depends_on_id']} which belongs to "
                f"project {mismatch['depends_on_project_id']}, not this "
                f"item's own project {mismatch['item_project_id']}."
            ),
            actor=actor,
            suspected_project_id=mismatch["depends_on_project_id"],
        )
        if result.get("quarantined"):
            quarantined.append(result["entry"])
    return {"mismatches": mismatches, "quarantined": quarantined}
