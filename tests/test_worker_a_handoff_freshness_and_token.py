"""Regression tests for four related generate_handoff/pending_goal bugs
(WORKER A batch, meridian/handoff.py, sequential same-file items):

  4611b9a2 — generate_handoff(mode="starter") never embedded a <goal_token>
             or SECURITY banner (only the full/delta path did). Fixed by
             extracting the mint+embed logic into the shared
             _mint_and_embed_goal_token helper and calling it from both
             generate_handoff (full/delta) and _generate_starter_handoff.

  dd19b6a4 — generate_handoff's pending_sprint_items/in_progress_items were
             snapshotted once near the top of the function and never
             cross-checked against a live re-query before being baked into
             quick_start_goal, even though several slow steps (session-summary
             fan-out, code-pointer enrichment) run in between and can let
             another session claim an item in the interim. Fixed by
             re-querying sprint items + sessions immediately before
             finalizing the pending list.

  6f0746bb — the <goal_token> embedded in quick_start_goal is minted with
             only _HANDOFF_TOKEN_TTL_SECONDS (300s) of validity, but that
             same quick_start_goal string is persisted verbatim as
             projects.pending_goal, which can sit unconsumed for up to
             PENDING_GOAL_STALE_HOURS (24h) before start_session ever pops
             and delivers it. With the old 1-hour cleanup grace window, the
             token's DB row was almost always physically swept long before
             a start_session up to a day later tried to verify it, so a
             genuine (merely old) token reported "not_found" instead of the
             honest "expired". Fixed by sizing
             _HANDOFF_TOKEN_CLEANUP_GRACE_SECONDS to at least
             db_module.PENDING_GOAL_STALE_HOURS.

  0a65f5cc — generate_handoff/_build_quick_start_goal included track=='backburner'
             and deferred_until items in the claimable batch instead of excluding
             them the way blocker_kind=='manual' items already are. Fixed by
             _is_backburner_sprint_item (mirrors _is_manual_sprint_item) plus a
             force_included_ids exemption so force_include_ids (45f519a0 Part 2)
             still works for a deliberately-restored deferred item.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


def _extract_token_from_goal(goal: str) -> str | None:
    m = re.search(r"<goal_token>([^<]+)</goal_token>", goal)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# 4611b9a2 — starter/compact mode must embed a goal_token + SECURITY banner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_starter_mode_embeds_goal_token_and_security_banner(db, tmp_path):
    """4611b9a2: mode="starter" must carry the same structural <goal_token> +
    SECURITY banner protection as the full/delta path, not just prose."""
    p = await db_module.create_project(db, "4611b9a2-starter-token")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    await db_module.add_sprint_item(db, p["id"], "v1", "do the starter thing")

    _path, content, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), mode="starter", skip_ai_summary=True
    )

    assert "<goal_token>" in content, (
        "starter mode must embed a <goal_token> tag (4611b9a2)"
    )
    token = _extract_token_from_goal(content)
    assert token, "starter <goal_token> must carry a non-empty value"
    assert "SECURITY" in content, "starter mode must carry the SECURITY banner"
    assert "verify_handoff_token" in content, (
        "starter mode's banner must instruct calling verify_handoff_token"
    )

    # The minted token must actually be valid (real DB row, not a fake string).
    result = await handoff_module.verify_handoff_token(db, token, p["id"])
    assert result["valid"] is True, f"starter token must verify: {result}"
    assert result["reason"] == "ok"


@pytest.mark.asyncio
async def test_compact_mode_embeds_goal_token_too(db, tmp_path):
    """4611b9a2: mode="compact" routes through the same starter renderer and
    must get the same fix (compact and starter share _generate_starter_handoff)."""
    p = await db_module.create_project(db, "4611b9a2-compact-token")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")

    _path, content, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), mode="compact", skip_ai_summary=True
    )

    assert "<goal_token>" in content
    assert "SECURITY" in content


# ---------------------------------------------------------------------------
# dd19b6a4 — pending list must be cross-checked against a fresh re-query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_list_excludes_item_claimed_mid_generation(db, tmp_path, monkeypatch):
    """dd19b6a4: if another session claims the ONLY pending item after the
    top-of-function snapshot but before quick_start_goal is finalized,
    generate_handoff must not still offer that item as claimable — the
    re-query right before finalizing the list must catch it."""
    p = await db_module.create_project(db, "dd19b6a4-race")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "race condition item")

    orig_get_sprint_items = db_module.get_sprint_items
    match_kwargs = {"include_human": False, "include_deferred": False}
    call_count = {"n": 0}

    async def _side_effect(*args, **kwargs):
        if kwargs == match_kwargs:
            call_count["n"] += 1
            if call_count["n"] == 2:
                # Simulate a second live session claiming the item in the
                # window between generate_handoff's initial snapshot and its
                # pre-finalization cross-check re-query.
                await db_module.claim_sprint_item(
                    db, p["id"], item["id"], actor="other-live-session"
                )
        return await orig_get_sprint_items(*args, **kwargs)

    # Patch the same module object handoff.py's `db_module` alias points at
    # (`from . import db as db_module` binds the module, not a copy).
    monkeypatch.setattr(db_module, "get_sprint_items", _side_effect)

    _path, content, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )

    assert call_count["n"] >= 2, (
        "test setup assumption broken: expected at least 2 matching "
        "get_sprint_items(include_human=False, include_deferred=False) calls "
        "(top snapshot + dd19b6a4 cross-check), got "
        f"{call_count['n']}"
    )
    # The item is now in_progress (claimed by "another session"), so the
    # pending list must be empty and quick_start_goal must fall into the
    # empty-board branch instead of naming the now-claimed item.
    assert item["id"] not in content.split("<goal_token>")[-1], (
        "an item claimed mid-generation must not be offered in quick_start_goal"
    )
    assert "Verify remaining work is complete." in content, (
        "with the only pending item claimed mid-generation, quick_start_goal "
        "must fall back to the empty-board branch"
    )

    # Confirm the item really is in_progress in the DB (sanity on the setup).
    fresh = await db_module.get_sprint_item(db, item["id"])
    assert fresh["status"] == "in_progress"


# ---------------------------------------------------------------------------
# 6f0746bb — token cleanup grace window must be >= PENDING_GOAL_STALE_HOURS
# ---------------------------------------------------------------------------


def test_cleanup_grace_window_covers_pending_goal_staleness_horizon():
    """6f0746bb: _HANDOFF_TOKEN_CLEANUP_GRACE_SECONDS must be at least as long
    as db_module.PENDING_GOAL_STALE_HOURS (in seconds) so a genuine token
    embedded in a pending_goal that sits unconsumed right up to the staleness
    horizon still reports 'expired' (truthful) rather than 'not_found'
    (indistinguishable from fabricated) once it is finally verified."""
    assert (
        handoff_module._HANDOFF_TOKEN_CLEANUP_GRACE_SECONDS
        >= db_module.PENDING_GOAL_STALE_HOURS * 3600
    ), (
        "cleanup grace window must cover the full pending_goal staleness "
        "horizon, not just the old fixed 1-hour window"
    )


@pytest.mark.asyncio
async def test_token_at_pending_goal_stale_horizon_reports_expired_not_not_found(anydb):
    """6f0746bb integration: a token whose age matches the exact
    PENDING_GOAL_STALE_HOURS horizon (the oldest a pending_goal is allowed to
    be before start_session flags it stale) must still report "expired", not
    "not_found", when finally verified — reproducing the real delivery path
    end-to-end (mint -> embed in pending_goal -> sit for ~24h -> pop/verify).
    """
    import secrets as _secrets

    token = _secrets.token_hex(8)
    # One minute short of the full staleness horizon -- the realistic "just
    # about to be flagged stale but still delivered" delivery point.
    age = timedelta(hours=db_module.PENDING_GOAL_STALE_HOURS) - timedelta(minutes=1)
    past_str = (datetime.now(timezone.utc) - age).strftime("%Y-%m-%d %H:%M:%S.%f")
    await anydb.execute(
        "INSERT INTO handoff_tokens (token, project_id, expires_at, consumed) "
        "VALUES (?, ?, ?, 0)",
        (token, "pid-pending-goal-horizon", past_str),
    )
    await anydb.commit()

    # An unrelated concurrent mint elsewhere runs the opportunistic cleanup,
    # exactly as could happen system-wide while this token sits inside a
    # long-unconsumed pending_goal.
    await handoff_module.mint_handoff_token(anydb, "pid-unrelated-concurrent-mint-3")

    result = await handoff_module.verify_handoff_token(
        anydb, token, "pid-pending-goal-horizon"
    )
    assert result["valid"] is False
    assert result["reason"] == "expired", (
        f"a token still within the pending_goal staleness horizon must report "
        f"'expired', not '{result['reason']}'"
    )


# ---------------------------------------------------------------------------
# 0a65f5cc — track=='backburner'/deferred_until items must be excluded from
# the claimable batch, the same way blocker_kind=='manual' items already are
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backburner_track_item_excluded_from_claimable_batch(db, tmp_path):
    """0a65f5cc: an item with track=='backburner' must not be named in
    quick_start_goal, and must instead surface in a non-silent exclusion note
    (same treatment as a blocker_kind=='manual' item)."""
    p = await db_module.create_project(db, "0a65f5cc-backburner")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    claimable = await db_module.add_sprint_item(
        db, p["id"], "v1", "claimable item", prospect_bypass=True
    )
    backburnered = await db_module.add_sprint_item(
        db, p["id"], "v1", "backburnered item",
        track="backburner", prospect_bypass=True,
    )

    _path, content, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )

    goal_section = content.split("<goal_token>")[-1]
    assert claimable["id"] in goal_section, (
        "an ordinary claimable item must still be named in quick_start_goal"
    )
    assert backburnered["id"] not in goal_section.split("<exclusions>")[0], (
        "a track=='backburner' item must not be offered as claimable"
    )
    assert backburnered["id"] in content, (
        "a backburnered item must still be surfaced (non-silent), just not "
        "as claimable"
    )


@pytest.mark.asyncio
async def test_deferred_until_item_excluded_from_starter_mode_batch(db, tmp_path):
    """0a65f5cc: _generate_starter_handoff fetches sprint items with NO
    include_deferred flag at all, so a future-deferred item would otherwise
    leak into the starter /goal's claimable batch. The _build_quick_start_goal
    -level check must catch it regardless of which caller fetched the items."""
    p = await db_module.create_project(db, "0a65f5cc-starter-deferred")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    deferred = await db_module.add_sprint_item(
        db, p["id"], "v1", "deferred item",
        deferred_until=future, prospect_bypass=True,
    )

    _path, content, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), mode="starter", skip_ai_summary=True
    )

    assert deferred["id"] not in content.split("<goal_token>")[-1].split(
        "<exclusions>"
    )[0], "a future-deferred item must not be claimable in starter mode either"


@pytest.mark.asyncio
async def test_force_include_ids_still_overrides_deferred_exclusion(db, tmp_path):
    """0a65f5cc must not regress 45f519a0 Part 2: force_include_ids is a
    deliberate override that re-adds a SPECIFIC deferred item into scope for
    one handoff call. The new backburner/deferred filter must exempt exactly
    those ids rather than immediately stripping them back out."""
    p = await db_module.create_project(db, "0a65f5cc-force-include")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    deferred = await db_module.add_sprint_item(
        db, p["id"], "v1", "force-included deferred item",
        deferred_until=future, prospect_bypass=True,
    )

    _path, content, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path),
        skip_ai_summary=True, force_include_ids=[deferred["id"]],
    )

    assert deferred["id"] in content.split("<goal_token>")[-1].split(
        "<exclusions>"
    )[0], (
        "force_include_ids must still restore a deferred item to the "
        "claimable batch despite the new backburner/deferred exclusion"
    )
