"""Sprint-item persistence functions — extracted from meridian/db/__init__.py.

This module contains all functions whose primary subject is the sprint_items table:
add/claim/complete/fail/push/skip/patch/split/merge/pointers/waves and related helpers.

Imported back into meridian.db via ``from .sprint_items import *`` so all existing
call sites using ``db_module.function_name()`` continue to work unchanged.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

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
)


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
_VALID_SPRINT_BLOCKER_KINDS = ("manual", "superseded")


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
# get_sprint_progress 10s cache — one get_sprint_items DB query serves all
# parallel sessions polling between tasks. Keyed by project_id; busted on any
# sprint-item mutation so progress counts never read stale after a write.
# ---------------------------------------------------------------------------
_SPRINT_ITEMS_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_SPRINT_ITEMS_CACHE_TTL = 10.0  # seconds


def _invalidate_sprint_items_cache(project_id: str) -> None:
    """Drop the cached sprint-item list for a project after a mutation."""
    _SPRINT_ITEMS_CACHE.pop(project_id, None)


async def get_sprint_items_cached(
    db: aiosqlite.Connection, project_id: str
) -> list[dict[str, Any]]:
    """Return get_sprint_items(project_id), cached for _SPRINT_ITEMS_CACHE_TTL.

    Parallel executors polling get_sprint_progress between tasks share one DB
    query within the TTL window. Any add/update mutation calls
    _invalidate_sprint_items_cache so counts are never stale after a write.
    """
    now = time.monotonic()
    hit = _SPRINT_ITEMS_CACHE.get(project_id)
    if hit is not None and (now - hit[0]) < _SPRINT_ITEMS_CACHE_TTL:
        return hit[1]
    items = await get_sprint_items(db, project_id)
    _SPRINT_ITEMS_CACHE[project_id] = (now, items)
    return items


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
        "(id, project_id, version, title, item_group, human_id, depends_on, "
        "failure_mode, milestone_type, touches_resources, slug, nickname, "
        "deferred_until, track, priority, blocker_kind, wave, sprint_name, "
        "prospect_bypass) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (iid, project_id, version, title, group, human_id,
         depends_on, failure_mode or "continue", milestone_type, resources_json,
         _item_slug, _item_nickname, deferred_until or None, track or None,
         priority, blocker_kind or None, wave or None, sprint_name or None,
         1 if prospect_bypass else 0),
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


async def fan_out_sprint_items(
    db: aiosqlite.Connection,
    project_id: str,
    items: list[dict[str, Any]],
) -> list[str]:
    """Bulk-insert sprint items for an orchestrator decomposing a goal.

    ``items`` is a list of dicts, each with at minimum ``title`` (required)
    and optionally ``description``, ``group``, and ``version``.  Missing
    ``version`` defaults to the empty string (same as the common add_sprint_item
    convention).  Unlike add_sprint_item the duplicate guard is **not** applied
    here — the orchestrator is assumed to have already deduped.

    Returns the list of new item IDs in insertion order.
    """
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
) -> dict[str, Any] | None:
    """Mark a sprint item ``done`` and optionally link the task that shipped it.

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
    """
    item = await get_sprint_item(db, item_id)
    _evidence_quality_warning: str | None = None
    if item is not None and item.get("project_id") == project_id:
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
    result = await _update_sprint_item_status(
        db, project_id, item_id, "done", task_id=task_id, notes=notes, actor=actor,
        expected_statuses=_ACTIVE_SPRINT_STATUSES,
    )
    if result is not None:
        await _maybe_rollup_parent(db, project_id, item_id)
        await _advance_task_chain(db, project_id, item_id)
        if _evidence_quality_warning:
            result = dict(result)
            result["evidence_quality_warning"] = _evidence_quality_warning
    return result


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
        _blocking_gate = await _get_blocking_wave_gate(db, project_id, item.get("wave"))
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
    # 94c26322 — refuse an unprospected item at claim time unless a human
    # explicitly set prospect_bypass. At claim time, enrichment-time fields
    # (code_pointers / prospect_status) are NOT on the DB row, so we check the
    # durable sprint_item_pointers table instead (persistently pinned pointers).
    # An item is considered evidenced if it has >= 1 row in sprint_item_pointers.
    # Mirrors the goal-generation gate so claim cannot silently circumvent /goal
    # exclusions. Fail-open: any DB error lets the claim proceed so a structural
    # defect never permanently wedges the board.
    #
    # SCOPE GUARD: only applies when the item actually declared touches_resources
    # (i.e. was a real prospecting candidate at add-time). An item with no
    # declared resources was never attempted — nothing for _persist_prospected_pointer
    # to prospect — and gating it here would block the overwhelming majority of
    # ordinary items (manual tasks, proposals, anything filed without explicit
    # file/route/tool targets), not just genuinely-risky unprospected ones.
    _touches_raw = item.get("touches_resources")
    _has_declared_resources = bool(_touches_raw) and _touches_raw not in ("[]", "null")
    if _has_declared_resources and not bool(item.get("prospect_bypass")):
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
            if not _ptr_count:
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
        except Exception:  # noqa: BLE001 — gate must never wedge the board
            pass
    blocked = {"in_progress", "done", "failed", "skipped"}
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
    return result


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
) -> dict[str, Any] | None:
    """Update editable fields of a sprint item.

    Editable: title, version, status, feedback, notes, human_id (assignee),
    item_group, touches_resources, required_notes, deferred_until, track,
    priority, blocker_kind, sprint_name, prospect_bypass, depends_on,
    require_verification. Only fields passed as non-None are changed;
    omitted fields are left untouched. To clear human_id or item_group, pass an
    empty string. ``touches_resources`` (501ec93f) uses the ``_UNSET`` sentinel
    so it can be omitted entirely; pass ``None`` or ``[]`` to clear it, or a list
    / JSON string / comma-separated string of typed ids to set it.
    ``depends_on`` (56f607ec) uses the ``_UNSET`` sentinel: omit to leave
    unchanged, pass an empty string / ``None`` to CLEAR it (item becomes
    independently claimable again), or another sprint item's id to set/fix
    dependency ordering retroactively — previously ``depends_on`` could only
    be set at creation time via ``add_sprint_item``, with no way to fix
    ordering on an already-filed item. Raises ``ValueError`` if the id equals
    ``item_id`` itself (a self-dependency would deadlock the item).
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
        if _dep is not None and _dep == item_id:
            raise ValueError("depends_on cannot be the item's own id (self-dependency)")
        ns_fields.append("depends_on = ?")
        ns_values.append(_dep)
    if require_verification is not _UNSET:
        # e2e1b682 — True/1 SETS the independent fresh-session verifier gate
        # (complete_sprint_item then requires an on-file PASS filed by a
        # session distinct from the one completing it); False/0/None CLEARS it
        # (ordinary completion, evidence gate only). Stored as INTEGER 0/1.
        ns_fields.append("require_verification = ?")
        ns_values.append(1 if require_verification else 0)

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

    Validates the ``{source_type, targets:[{uri, selector, subSelector?}], label?}``
    shape via :mod:`meridian.pointers` (raising ``ValueError`` on a malformed
    pointer BEFORE any write), serializes ``targets`` to the JSON column, and
    inserts one ``sprint_item_pointers`` row. ``targets`` is an ARRAY (native
    multi-file); the composite shape is stored as JSON, NOT per-domain columns.
    The returned dict is the deserialized pointer (targets back as a list).

    psycopg3: ``?`` placeholders are converted to ``%s`` by the adapter; the
    shared connection is autocommit on Postgres, and ``commit()`` is a real
    flush on aiosqlite.
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


async def get_parallelizable_groups(
    db: aiosqlite.Connection,
    project_id: str,
    version: str | None = None,
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
    "eligible_count", "blocked": [...], "undeclared_count"}``. ``groups`` items
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
        enriched = {**it, "resources": parse_touches_resources(it.get("touches_resources"))}
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
    return {
        "version": version,
        "groups": groups,
        "group_count": len(groups),
        "eligible_count": len(eligible),
        "undeclared_count": undeclared,
        "blocked": blocked,
        "running": running,  # df573218 — items currently in flight
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

    Returns ``{version, wave_count, assigned, waves: {'wave-1': [ids...], ...},
    blocked_count, undeclared_count}``.  ``blocked_count`` now counts items whose
    dependency is not yet DONE (they are still projected into a future wave, not
    truly dropped).
    """
    # ── Collect all eligible pending/todo non-manual items ─────────────────────
    items = await get_sprint_items(db, project_id, include_manual_blocker=False)
    items = [it for it in items if not _is_manual_sprint_item(it)]
    if version is not None:
        items = [it for it in items if it.get("version") == version]
    claimable_statuses = {"pending", "todo"}
    # Only label pending/todo items that are not already in-flight or done.
    # In-progress items are mid-execution and must not be relabelled mid-run.
    candidates = [
        it for it in items
        if (it.get("status") or "pending") in claimable_statuses
    ]

    # ── Pass 1: topological depth ───────────────────────────────────────────────
    depth_map = _topo_depth_map(candidates)
    if not depth_map:
        return {
            "version": version,
            "wave_count": 0,
            "assigned": 0,
            "waves": {},
            "blocked_count": 0,
            "undeclared_count": 0,
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

    return {
        "version": version,
        "wave_count": len(waves),
        "assigned": assigned,
        "waves": waves,
        "blocked_count": blocked_count,
        "undeclared_count": undeclared_count,
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

_WAVE_GATE_RESULTS_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS wave_gate_results ("
    "    id TEXT PRIMARY KEY,"
    "    project_id TEXT NOT NULL,"
    "    wave_label TEXT NOT NULL,"         # e.g. 'wave-1'
    "    gate_passed INTEGER NOT NULL DEFAULT 1,"  # always 1 (rejected gates never write)
    "    exit_code INTEGER,"
    "    passed_count INTEGER,"
    "    failed_count INTEGER,"
    "    verification_status TEXT,"
    "    evidence_snapshot TEXT,"           # JSON of the full payload
    "    actor TEXT,"
    "    completed_at TEXT NOT NULL DEFAULT (datetime('now')),"
    "    UNIQUE(project_id, wave_label)"    # one gate result per project+wave
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

    On success a row is written to ``wave_gate_results`` and the function returns::

        {
            "gate_completed": True,
            "wave_label": "wave-1",
            "next_wave_label": "wave-2",      # None if no next wave exists
            "next_wave_item_count": <int>,    # how many pending/todo items in next wave
            "next_wave_item_ids": [...],
            "gate_id": "<uuid>",
        }

    Raises ValueError on evidence failure (bad payload) or if the gate for this
    wave has already been completed.
    """
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
    async with db.execute(
        "SELECT id FROM wave_gate_results WHERE project_id = ? AND wave_label = ?",
        (project_id, wave_label),
    ) as _dup_cur:
        _dup_row = await _dup_cur.fetchone()
    if _dup_row is not None:
        existing_id = _dup_row[0] if not isinstance(_dup_row, dict) else _dup_row["id"]
        raise ValueError(
            f"Wave gate for {wave_label!r} on project {project_id!r} has already been "
            f"completed (gate_id={existing_id!r}). Each wave gate may only be completed "
            f"once."
        )

    # ── 3. Determine the next wave label ─────────────────────────────────────────
    # wave_label is expected to be 'wave-N'; next wave is 'wave-(N+1)'.
    next_wave_label: str | None = None
    _parts = wave_label.rsplit("-", 1)
    if len(_parts) == 2 and _parts[1].isdigit():
        next_wave_label = f"{_parts[0]}-{int(_parts[1]) + 1}"

    # ── 4. Find next-wave items (informational) ───────────────────────────────────
    next_wave_item_ids: list[str] = []
    if next_wave_label is not None:
        async with db.execute(
            "SELECT id FROM sprint_items WHERE project_id = ? AND wave = ? "
            "AND status IN ('pending', 'todo') ORDER BY added_at",
            (project_id, next_wave_label),
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
        "(id, project_id, wave_label, gate_passed, exit_code, passed_count, "
        " failed_count, verification_status, evidence_snapshot, actor) "
        "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
        (
            gate_id,
            project_id,
            wave_label,
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

_WAVE_GATE_CONFIGS_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS wave_gate_configs ("
    "    id TEXT PRIMARY KEY,"
    "    project_id TEXT NOT NULL,"
    "    wave_start TEXT NOT NULL,"     # first wave covered by this gate (documentation)
    "    wave_end TEXT NOT NULL,"       # boundary wave — enforcement key
    "    actions TEXT NOT NULL,"        # JSON array of {"type": ..., ...params}
    "    actor TEXT,"
    "    created_at TEXT NOT NULL DEFAULT (datetime('now')),"
    "    updated_at TEXT NOT NULL DEFAULT (datetime('now')),"
    "    UNIQUE(project_id, wave_end)"  # one pipeline per boundary wave
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

    Re-configuring an already-configured (but not yet passed) ``wave_end`` is
    an upsert — this is the "on-the-fly-configurable" half of the spec: a
    planner can revise the pipeline for a wave boundary right up until an
    executor actually completes it. Once wave_gate_results has a row for
    wave_end the config is immutable (raises ValueError) — rewriting a passed
    gate's pipeline after the fact would silently invalidate evidence that
    claim_sprint_item already relied on to unblock items.
    """
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

    # A passed gate's config is immutable — see docstring.
    async with db.execute(
        "SELECT id FROM wave_gate_results WHERE project_id = ? AND wave_label = ?",
        (project_id, wave_end),
    ) as _res_cur:
        _already_passed = await _res_cur.fetchone()
    if _already_passed is not None:
        raise ValueError(
            f"Wave gate for {wave_end!r} on project {project_id!r} has already "
            "completed — its pipeline is immutable. Configure a NEW wave_end "
            "boundary instead of reconfiguring a passed gate."
        )

    _actions_json = json.dumps(_normalized)
    async with db.execute(
        "SELECT id FROM wave_gate_configs WHERE project_id = ? AND wave_end = ?",
        (project_id, wave_end),
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
            "(id, project_id, wave_start, wave_end, actions, actor) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_config_id, project_id, wave_start, wave_end, _actions_json, actor),
        )
    await db.commit()

    return {
        "configured": True,
        "gate_config_id": _config_id,
        "project_id": project_id,
        "wave_start": wave_start,
        "wave_end": wave_end,
        "actions": _normalized,
    }


async def get_wave_gate_configs(
    db: aiosqlite.Connection, project_id: str,
) -> list[dict[str, Any]]:
    """Read-only: list every configured wave gate for a project (oldest first),
    each annotated with ``gate_passed`` (whether wave_gate_results already has
    a matching row) so callers don't need a second query to know what's still
    pending."""
    await _ensure_wave_gate_configs_table(db)
    await _ensure_wave_gate_results_table(db)
    async with db.execute(
        "SELECT * FROM wave_gate_configs WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ) as _cur:
        _rows = await _cur.fetchall()
    out: list[dict[str, Any]] = []
    for _row in _rows:
        _cfg = _row_to_dict(_row) or {}
        try:
            _cfg["actions"] = json.loads(_cfg.get("actions") or "[]")
        except (TypeError, ValueError):
            _cfg["actions"] = []
        async with db.execute(
            "SELECT id FROM wave_gate_results WHERE project_id = ? AND wave_label = ?",
            (project_id, _cfg.get("wave_end")),
        ) as _res_cur:
            _cfg["gate_passed"] = (await _res_cur.fetchone()) is not None
        out.append(_cfg)
    return out


async def _get_blocking_wave_gate(
    db: aiosqlite.Connection, project_id: str, item_wave: str | None,
) -> dict[str, Any] | None:
    """Return the lowest-boundary configured-but-unpassed wave gate that
    structurally blocks claiming an item in ``item_wave``, or None if nothing
    blocks it (no wave on the item, no configs, an unparseable wave label, or
    every configured boundary at-or-below this wave has already passed).

    This is the function claim_sprint_item calls to turn wave gates from
    advisory /goal prose into a real, structural claim-time block.
    """
    _item_prefix, _item_num = _split_wave_label(item_wave)
    if _item_num is None:
        return None
    await _ensure_wave_gate_configs_table(db)
    await _ensure_wave_gate_results_table(db)
    async with db.execute(
        "SELECT * FROM wave_gate_configs WHERE project_id = ?",
        (project_id,),
    ) as _cur:
        _configs = await _cur.fetchall()
    _blocking: dict[str, Any] | None = None
    _blocking_num: int | None = None
    for _row in _configs:
        _cfg = _row_to_dict(_row) or {}
        _cfg_prefix, _cfg_num = _split_wave_label(_cfg.get("wave_end"))
        if _cfg_num is None or _cfg_prefix != _item_prefix or _cfg_num >= _item_num:
            continue  # not a boundary strictly before this item's wave
        async with db.execute(
            "SELECT id FROM wave_gate_results WHERE project_id = ? AND wave_label = ?",
            (project_id, _cfg.get("wave_end")),
        ) as _res_cur:
            _passed = await _res_cur.fetchone()
        if _passed is not None:
            continue  # this boundary's gate already passed
        if _blocking_num is None or _cfg_num < _blocking_num:
            try:
                _cfg["actions"] = json.loads(_cfg.get("actions") or "[]")
            except (TypeError, ValueError):
                _cfg["actions"] = []
            _blocking = _cfg
            _blocking_num = _cfg_num
    return _blocking
