"""Regression tests for c027922d — touches_resources cross-item pollution
under concurrent claims.

Bug (confirmed via direct source read on dev tip 1b257dee): the amendment
side-effect in ``_amend_sprint_item_resources_for_session`` (fired from
``claim_file``/``claim_symbol`` on a successful claim) picked "the sprint
item this claim belongs to" via

    SELECT id, touches_resources, wave FROM sprint_items
    WHERE actor = ? AND status = 'in_progress'
    ORDER BY claimed_at DESC LIMIT 1

i.e. "whichever in_progress item this session touched most recently" — with
zero connection to which item the caller actually intended the claim for.
The instant a session holds 2+ concurrently in_progress sprint items, a claim
meant for the OLDER item gets silently appended to whichever item happens to
be "most recent" by claimed_at, polluting the wrong item's touches_resources.

Fix: thread an explicit, optional ``item_id`` through
``_amend_sprint_item_resources_for_session`` / ``claim_file`` /
``claim_symbol``. When given, the lookup is pinned to that exact row
(``WHERE id = ? AND actor = ? AND status = 'in_progress'``) instead of
guessing. Omitted, the original heuristic is preserved unchanged (so the
legitimate single-item mid-execution-pivot widening case from acf6f51a keeps
working exactly as before — this bug is latent, not observable, when a
session holds at most one in_progress item).

Test matrix:
  (1) THE REPRO: one session holds two concurrently in_progress sprint items
      with disjoint declared resources. A claim_file call passing the OLDER
      item's item_id must amend that item's own touches_resources — not the
      newer sibling's, even though the newer sibling would win the old
      ORDER BY claimed_at DESC heuristic.
  (2) Same shape, claim_symbol instead of claim_file.
  (3) Without item_id (the omitted/legacy-caller case), the OLD buggy
      cross-item behavior is preserved on purpose (documents exactly what the
      fallback heuristic still does — no regression for callers that don't
      pass item_id yet).
  (4) Legitimate single-item widening (acf6f51a's scenario) still works
      identically whether item_id is passed explicitly or omitted, when the
      session holds only ONE in_progress item.
  (5) item_id given but stale/wrong (no matching in_progress row under this
      session for that id) -> no amendment, no crash (fails closed rather
      than falling back to guessing a different item).
"""
from __future__ import annotations

import json

import pytest
import pytest_asyncio

from meridian import db as db_module


# ---------------------------------------------------------------------------
# Fixture: one session holding TWO concurrently in_progress sprint items with
# genuinely disjoint declared resource sets — the exact incident shape.
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db_with_two_concurrent_items():
    """In-memory DB with:
      - project P1
      - session S1 (actor='sess-s1')
      - sprint item OLD, in_progress, claimed by S1, claimed_at EARLIER,
        touches_resources = ["file:meridian/old_module.py"], wave='wave-1'
      - sprint item NEW, in_progress, claimed by S1, claimed_at LATER,
        touches_resources = ["file:meridian/new_module.py"], wave='wave-2'

    NEW has the later claimed_at, so it is the row the pre-fix
    ``ORDER BY claimed_at DESC LIMIT 1`` heuristic would (wrongly) pick for
    ANY claim by S1, regardless of which item the claim was actually for.

    Returns (db, project_id, session_id, old_item_id, new_item_id).
    """
    conn = await db_module.init_db(":memory:")
    try:
        project_id = db_module._new_id()
        await conn.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            (project_id, "test-project"),
        )

        session_id = "sess-s1"
        await conn.execute(
            "INSERT INTO sessions (id, project_id, name, status) VALUES (?, ?, ?, 'active')",
            (session_id, project_id, session_id),
        )

        old_item_id = db_module._new_id()
        await conn.execute(
            "INSERT INTO sprint_items "
            "(id, project_id, version, title, status, actor, touches_resources, "
            "wave, claimed_at) "
            "VALUES (?, ?, '', 'Older item', 'in_progress', ?, ?, 'wave-1', "
            "datetime('now', '-1 hour'))",
            (old_item_id, project_id, session_id,
             json.dumps(["file:meridian/old_module.py"])),
        )

        new_item_id = db_module._new_id()
        await conn.execute(
            "INSERT INTO sprint_items "
            "(id, project_id, version, title, status, actor, touches_resources, "
            "wave, claimed_at) "
            "VALUES (?, ?, '', 'Newer item', 'in_progress', ?, ?, 'wave-2', "
            "datetime('now'))",
            (new_item_id, project_id, session_id,
             json.dumps(["file:meridian/new_module.py"])),
        )
        await conn.commit()

        yield conn, project_id, session_id, old_item_id, new_item_id
    finally:
        await conn.close()


def _resources_of(row) -> list[str]:
    r = dict(row) if not isinstance(row, dict) else row
    return json.loads(r["touches_resources"] or "[]")


async def _fetch_item(conn, item_id):
    async with conn.execute(
        "SELECT id, touches_resources, resources_amended FROM sprint_items WHERE id = ?",
        (item_id,),
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if not isinstance(row, dict) else row


# ---------------------------------------------------------------------------
# (1) THE REPRO — claim_file with explicit item_id=OLDER item must amend
#     the OLDER item only, never the newer sibling.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claim_file_with_item_id_amends_only_the_named_item(
    db_with_two_concurrent_items,
):
    conn, project_id, session_id, old_item_id, new_item_id = db_with_two_concurrent_items

    # A claim for a NEW file, explicitly declared as belonging to the OLDER
    # item (item_id=old_item_id) — even though NEW has the later claimed_at
    # and would win the pre-fix heuristic.
    result = await db_module.claim_file(
        conn, "meridian/extra_old_dependency.py", session_id, item_id=old_item_id,
    )
    assert result.get("claimed") is True

    old_row = await _fetch_item(conn, old_item_id)
    new_row = await _fetch_item(conn, new_item_id)

    old_resources = _resources_of(old_row)
    new_resources = _resources_of(new_row)

    # The OLDER item got the amendment.
    assert "file:meridian/old_module.py" in old_resources  # original preserved
    assert "file:meridian/extra_old_dependency.py" in old_resources  # newly appended
    assert old_row["resources_amended"] == 1

    # The NEWER (sibling) item is completely untouched — this is the actual
    # regression assertion: no cross-item pollution.
    assert new_resources == ["file:meridian/new_module.py"]
    assert not new_row["resources_amended"]


@pytest.mark.asyncio
async def test_claim_file_with_item_id_amends_only_the_newer_item_when_named(
    db_with_two_concurrent_items,
):
    """Symmetric check: naming the NEWER item explicitly amends IT, not the
    older sibling — proving item_id (not claimed_at recency) drives the
    outcome in both directions."""
    conn, project_id, session_id, old_item_id, new_item_id = db_with_two_concurrent_items

    result = await db_module.claim_file(
        conn, "meridian/extra_new_dependency.py", session_id, item_id=new_item_id,
    )
    assert result.get("claimed") is True

    old_row = await _fetch_item(conn, old_item_id)
    new_row = await _fetch_item(conn, new_item_id)

    assert _resources_of(old_row) == ["file:meridian/old_module.py"]
    assert not old_row["resources_amended"]

    new_resources = _resources_of(new_row)
    assert "file:meridian/new_module.py" in new_resources
    assert "file:meridian/extra_new_dependency.py" in new_resources
    assert new_row["resources_amended"] == 1


# ---------------------------------------------------------------------------
# (3) Documents the INTENTIONALLY unchanged fallback: a caller that omits
#     item_id entirely still gets the pre-fix "most recently claimed
#     in_progress item wins" heuristic. This is not a new regression — it is
#     the documented, unchanged behavior for callers with no item context
#     (see docstrings on _amend_sprint_item_resources_for_session/claim_file).
#     Real callers that know their item_id (claim_sprint_item's resource
#     gate, the batch-claim path) now DO pass it, closing the actual bug.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_omitting_item_id_keeps_pre_fix_heuristic_unchanged(
    db_with_two_concurrent_items,
):
    conn, project_id, session_id, old_item_id, new_item_id = db_with_two_concurrent_items

    # No item_id passed — legacy call shape. The heuristic falls back to
    # "most recently claimed in_progress item", which is NEW here.
    result = await db_module.claim_file(
        conn, "meridian/ambiguous_target.py", session_id,
    )
    assert result.get("claimed") is True

    old_row = await _fetch_item(conn, old_item_id)
    new_row = await _fetch_item(conn, new_item_id)

    # Documented (unchanged) behavior: the amendment landed on the item with
    # the LATEST claimed_at (NEW), regardless of which item this claim was
    # conceptually "for" — exactly the ambiguity that motivates callers to
    # pass item_id explicitly.
    assert "file:meridian/ambiguous_target.py" in _resources_of(new_row)
    assert new_row["resources_amended"] == 1
    assert _resources_of(old_row) == ["file:meridian/old_module.py"]
    assert not old_row["resources_amended"]


# ---------------------------------------------------------------------------
# (2) Same repro shape via claim_symbol.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claim_symbol_with_item_id_amends_only_the_named_item(
    db_with_two_concurrent_items,
):
    conn, project_id, session_id, old_item_id, new_item_id = db_with_two_concurrent_items

    content = "def helper_for_old_item():\n    pass\n"
    result = await db_module.claim_symbol(
        conn, session_id, "meridian/old_helpers.py", "helper_for_old_item", content,
        item_id=old_item_id,
    )
    assert result.get("claimed") is True

    old_row = await _fetch_item(conn, old_item_id)
    new_row = await _fetch_item(conn, new_item_id)

    old_resources = _resources_of(old_row)
    assert "file:meridian/old_module.py" in old_resources
    assert any("helper_for_old_item" in r for r in old_resources)
    assert old_row["resources_amended"] == 1

    # Sibling (newer) item untouched.
    assert _resources_of(new_row) == ["file:meridian/new_module.py"]
    assert not new_row["resources_amended"]


# ---------------------------------------------------------------------------
# (3) Full end-to-end repro: complete the OLDER item while the NEWER item
#     remains legitimately in_progress, and assert the completed item's
#     touches_resources contains ONLY its own resources.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_older_item_touches_resources_excludes_sibling(
    db_with_two_concurrent_items,
):
    conn, project_id, session_id, old_item_id, new_item_id = db_with_two_concurrent_items

    # Claim additional files for each item, correctly attributed via item_id
    # (the fixed call pattern an executor / claim_sprint_item resource gate
    # now uses).
    await db_module.claim_file(
        conn, "meridian/old_extra_a.py", session_id, item_id=old_item_id,
    )
    await db_module.claim_file(
        conn, "meridian/new_extra_a.py", session_id, item_id=new_item_id,
    )
    await db_module.claim_file(
        conn, "meridian/old_extra_b.py", session_id, item_id=old_item_id,
    )

    # "Complete" the older item the way complete_sprint_item would leave it —
    # simply assert its stored touches_resources at this point, since
    # complete_sprint_item itself never touches touches_resources (per
    # c027922d findings; it only surfaces what's already persisted).
    old_row = await _fetch_item(conn, old_item_id)
    new_row = await _fetch_item(conn, new_item_id)

    old_resources = set(_resources_of(old_row))
    new_resources = set(_resources_of(new_row))

    assert old_resources == {
        "file:meridian/old_module.py",
        "file:meridian/old_extra_a.py",
        "file:meridian/old_extra_b.py",
    }
    # Disjoint — no sibling contamination in either direction.
    assert old_resources.isdisjoint(new_resources)
    assert new_resources == {
        "file:meridian/new_module.py",
        "file:meridian/new_extra_a.py",
    }


# ---------------------------------------------------------------------------
# (4) Legitimate single-item widening (acf6f51a) still works — with AND
#     without item_id, since a session with only one in_progress item has
#     only one candidate row either way.
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db_with_single_item():
    conn = await db_module.init_db(":memory:")
    try:
        project_id = db_module._new_id()
        await conn.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            (project_id, "test-project-single"),
        )
        session_id = "sess-single"
        await conn.execute(
            "INSERT INTO sessions (id, project_id, name, status) VALUES (?, ?, ?, 'active')",
            (session_id, project_id, session_id),
        )
        item_id = db_module._new_id()
        await conn.execute(
            "INSERT INTO sprint_items "
            "(id, project_id, version, title, status, actor, touches_resources, wave) "
            "VALUES (?, ?, '', 'Solo item', 'in_progress', ?, ?, 'wave-1')",
            (item_id, project_id, session_id,
             json.dumps(["file:meridian/server.py"])),
        )
        await conn.commit()
        yield conn, project_id, session_id, item_id
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_single_item_widening_still_works_with_explicit_item_id(
    db_with_single_item,
):
    conn, project_id, session_id, item_id = db_with_single_item

    result = await db_module.claim_file(
        conn, "meridian/db/locks.py", session_id, item_id=item_id,
    )
    assert result.get("claimed") is True
    assert result.get("wave_assignment_hint") is not None

    row = await _fetch_item(conn, item_id)
    resources = _resources_of(row)
    assert "file:meridian/server.py" in resources
    assert "file:meridian/db/locks.py" in resources
    assert row["resources_amended"] == 1


@pytest.mark.asyncio
async def test_single_item_widening_still_works_without_item_id(db_with_single_item):
    """acf6f51a's original scenario, unchanged: no item_id passed at all —
    the fallback heuristic still correctly widens the session's one
    in_progress item."""
    conn, project_id, session_id, item_id = db_with_single_item

    result = await db_module.claim_file(conn, "meridian/db/locks.py", session_id)
    assert result.get("claimed") is True
    assert result.get("wave_assignment_hint") is not None

    row = await _fetch_item(conn, item_id)
    resources = _resources_of(row)
    assert "file:meridian/server.py" in resources
    assert "file:meridian/db/locks.py" in resources
    assert row["resources_amended"] == 1


# ---------------------------------------------------------------------------
# (5) item_id given but does not match any in_progress row for this session
#     -> no amendment anywhere (fails closed, does not fall back to guessing).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_item_id_fails_closed_no_amendment(
    db_with_two_concurrent_items,
):
    conn, project_id, session_id, old_item_id, new_item_id = db_with_two_concurrent_items

    bogus_item_id = db_module._new_id()
    result = await db_module.claim_file(
        conn, "meridian/orphan_claim.py", session_id, item_id=bogus_item_id,
    )
    # The file claim itself still succeeds — only the amendment side-effect
    # is skipped.
    assert result.get("claimed") is True
    assert not result.get("wave_assignment_hint")

    old_row = await _fetch_item(conn, old_item_id)
    new_row = await _fetch_item(conn, new_item_id)
    # Neither sibling item was touched.
    assert _resources_of(old_row) == ["file:meridian/old_module.py"]
    assert _resources_of(new_row) == ["file:meridian/new_module.py"]
    assert not old_row["resources_amended"]
    assert not new_row["resources_amended"]


@pytest.mark.asyncio
async def test_amend_directly_with_item_id_pins_exact_row(
    db_with_two_concurrent_items,
):
    """Unit-level check directly on _amend_sprint_item_resources_for_session,
    mirroring the existing (d) test style in test_2593a5fe_resources_amended.py."""
    conn, project_id, session_id, old_item_id, new_item_id = db_with_two_concurrent_items

    result = await db_module._amend_sprint_item_resources_for_session(
        conn, session_id, "file:meridian/direct_call_target.py", item_id=old_item_id,
    )
    assert result is not None
    assert result.get("item_id") == old_item_id
    assert result.get("item_wave") == "wave-1"

    old_row = await _fetch_item(conn, old_item_id)
    new_row = await _fetch_item(conn, new_item_id)
    assert "file:meridian/direct_call_target.py" in _resources_of(old_row)
    assert _resources_of(new_row) == ["file:meridian/new_module.py"]
