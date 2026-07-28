"""efaa918a — regression: generate_handoff(mode='starter') ignored sprint-version scope.

Prior-session finding (recorded on sprint item efaa918a-3a9b-4e87-ba81-c82fa96aa049):
generate_handoff(mode='starter') was observed emitting the old default
~44-item cross-version backlog and excluding v0.2.5-scoped items as
unprospected, even though start_session(version='v0.2.5') correctly scopes.

Root cause: ``_generate_starter_handoff`` called
``get_sprint_items(db, project_id)`` with NO version filter, and
``_build_quick_start_goal``'s own ``version`` kwarg (designed for exactly this)
was never invoked with a real value from ANY call site in handoff.py. A
version-scoped session therefore always saw the ENTIRE cross-version backlog:
older, already-prospected items from other versions dominated the executable
/goal list while fresh, not-yet-prospected same-version items got filtered out
by the (correctly functioning) unprospected gate — looking like the newer
items were the problem when the real bug was the missing scope.

Fix: generate_handoff gained an optional ``version`` kwarg; when omitted and
``session_id`` is given, the starter path resolves that session's own
``sprint_version`` (the same value ``start_session(version=...)`` stores) and
threads it through to ``_generate_starter_handoff`` -> ``get_sprint_items``.

NOT fixed (documented, out of scope for this item): the full/delta and
goal-only handoff modes have the identical underlying gap. See
generate_handoff's own docstring in meridian/handoff.py.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


@pytest.mark.asyncio
async def test_starter_mode_scopes_to_session_sprint_version(db, tmp_path):
    """The exact regression: a v0.2.5-scoped session's starter /goal must show
    ONLY v0.2.5 items, not the whole cross-version backlog."""
    p = await db_module.create_project(db, "starter-version-scope")
    await db_module.set_goal(db, p["id"], "ship version scoping")

    # A big pile of OLDER, unrelated backlog across other versions.
    for i in range(5):
        await db_module.add_sprint_item(
            db, p["id"], "v0.1.x", f"old backlog item {i}",
        )
    # The CURRENT version's own items.
    v25_done = await db_module.add_sprint_item(db, p["id"], "v0.2.5", "v0.2.5 done item")
    await db_module.complete_sprint_item(db, p["id"], v25_done["id"])
    v25_pending = await db_module.add_sprint_item(
        db, p["id"], "v0.2.5", "v0.2.5 pending item",
    )

    session = await db_module.register_session(
        db, p["id"], "executor-v025", sprint_version="v0.2.5",
    )

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path),
        skip_ai_summary=True, mode="starter", session_id=session["id"],
    )

    assert v25_pending["id"][:8] in content
    for i in range(5):
        assert f"old backlog item {i}" not in content
    # Sanity: without the fix this would have been "6 of 6" or worse.
    assert "old backlog" not in content


@pytest.mark.asyncio
async def test_starter_mode_explicit_version_overrides_session_scope(db, tmp_path):
    p = await db_module.create_project(db, "starter-explicit-version")
    await db_module.add_sprint_item(db, p["id"], "v1", "v1 item")
    v2_item = await db_module.add_sprint_item(db, p["id"], "v2", "v2 item")

    session = await db_module.register_session(
        db, p["id"], "executor-v1", sprint_version="v1",
    )

    # Explicit version= wins over the session's own stored scope.
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path),
        skip_ai_summary=True, mode="starter", session_id=session["id"],
        version="v2",
    )
    assert v2_item["id"][:8] in content
    assert "v1 item" not in content


@pytest.mark.asyncio
async def test_starter_mode_unscoped_session_sees_every_version(db, tmp_path):
    """Backward compatibility: no session_id / no version= -> every version,
    exactly the pre-existing behavior for a caller that never scoped."""
    p = await db_module.create_project(db, "starter-unscoped")
    it1 = await db_module.add_sprint_item(db, p["id"], "v1", "v1 item")
    it2 = await db_module.add_sprint_item(db, p["id"], "v2", "v2 item")

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="starter",
    )
    assert it1["id"][:8] in content
    assert it2["id"][:8] in content


@pytest.mark.asyncio
async def test_starter_mode_session_with_no_stored_scope_is_unscoped(db, tmp_path):
    """A session_id that resolves to a session with sprint_version=NULL (never
    version-scoped) must behave exactly like no session_id at all."""
    p = await db_module.create_project(db, "starter-null-scope")
    it1 = await db_module.add_sprint_item(db, p["id"], "v1", "v1 item")
    it2 = await db_module.add_sprint_item(db, p["id"], "v2", "v2 item")
    session = await db_module.register_session(db, p["id"], "executor-unscoped")

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="starter",
        session_id=session["id"],
    )
    assert it1["id"][:8] in content
    assert it2["id"][:8] in content


@pytest.mark.asyncio
async def test_resolve_session_sprint_version_helper(db):
    p = await db_module.create_project(db, "resolve-helper")
    scoped = await db_module.register_session(
        db, p["id"], "scoped-sess", sprint_version="v9.9",
    )
    unscoped = await db_module.register_session(db, p["id"], "unscoped-sess")

    assert await handoff_module._resolve_session_sprint_version(
        db, scoped["id"],
    ) == "v9.9"
    assert await handoff_module._resolve_session_sprint_version(
        db, unscoped["id"],
    ) is None
    assert await handoff_module._resolve_session_sprint_version(
        db, "no-such-session",
    ) is None
    assert await handoff_module._resolve_session_sprint_version(db, None) is None


@pytest.mark.asyncio
async def test_compact_mode_alias_also_scopes(db, tmp_path):
    """mode='compact' is the same code path as 'starter' — must scope too."""
    p = await db_module.create_project(db, "compact-version-scope")
    await db_module.add_sprint_item(db, p["id"], "v1", "v1 item")
    v2_item = await db_module.add_sprint_item(db, p["id"], "v2", "v2 item")
    session = await db_module.register_session(
        db, p["id"], "executor-compact-v2", sprint_version="v2",
    )

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="compact",
        session_id=session["id"],
    )
    assert v2_item["id"][:8] in content
    assert "v1 item" not in content
