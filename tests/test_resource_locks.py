"""Tests for 501ec93f — touches_resources field + generalized resource_locks.

Covers the typed-resource identifier parsing/normalization helpers, the
touches_resources column wiring on add/update/fan_out sprint items, and the
resource_locks claim/release/expire/conflict primitive (the generalization of
file_locks to any typed resource).
"""
from __future__ import annotations

import json

import pytest

from meridian import db as db_module


# ── identifier parsing / normalization ──────────────────────────────────────

@pytest.mark.parametrize(
    "identifier,expected",
    [
        ("file:meridian/db/__init__.py", ("file", "meridian/db/__init__.py")),
        ("db:migrations", ("db", "migrations")),
        ("mcp_tool:get_parallelizable_groups", ("mcp_tool", "get_parallelizable_groups")),
        ("route:POST:/projects", ("route", "POST:/projects")),
        ("pypi:publish", ("pypi", "publish")),
        ("github:tag", ("github", "tag")),
        ("  FILE:foo.py  ", ("file", "foo.py")),  # trimmed + lowercased type
    ],
)
def test_parse_resource_identifier_valid(identifier, expected):
    assert db_module.parse_resource_identifier(identifier) == expected


@pytest.mark.parametrize(
    "bad",
    ["", "nocolon", "bogus:thing", "file:", ":value", "   ", "db:"],
)
def test_parse_resource_identifier_invalid(bad):
    with pytest.raises(ValueError):
        db_module.parse_resource_identifier(bad)


def test_normalize_resource_id_file_path():
    # Backslashes → slashes, leading ./ stripped, type lowercased.
    assert db_module.normalize_resource_id("FILE:.\\meridian\\server.py") == "file:meridian/server.py"
    # Non-file types pass the value through unchanged (but type lowercased).
    assert db_module.normalize_resource_id("DB:migrations") == "db:migrations"
    # route keeps its embedded colon
    assert db_module.normalize_resource_id("route:GET:/x") == "route:GET:/x"


def test_parse_touches_resources_decodes_and_dedupes():
    raw = json.dumps([
        "file:./a.py", "file:a.py", "db:migrations", "bogus:x", "",
    ])
    # file:./a.py and file:a.py collapse to one; bogus:x and "" dropped.
    assert db_module.parse_touches_resources(raw) == ["file:a.py", "db:migrations"]


def test_parse_touches_resources_comma_string_and_none():
    assert db_module.parse_touches_resources(None) == []
    assert db_module.parse_touches_resources("") == []
    assert db_module.parse_touches_resources("file:a.py, db:migrations") == [
        "file:a.py", "db:migrations",
    ]


def test_serialize_touches_resources_roundtrip_and_clear():
    assert db_module.serialize_touches_resources(None) is None
    assert db_module.serialize_touches_resources([]) is None
    out = db_module.serialize_touches_resources(["file:a.py", "file:a.py"])
    assert json.loads(out) == ["file:a.py"]


def test_serialize_touches_resources_raises_on_bad_input():
    with pytest.raises(ValueError):
        db_module.serialize_touches_resources(["not-a-valid-id"])


# ── sprint-item column wiring ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_sprint_item_persists_touches_resources(db):
    p = await db_module.create_project(db, "alpha")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "feature",
        touches_resources=["file:meridian/db/__init__.py", "db:migrations"],
    )
    stored = await db_module.get_sprint_item(db, item["id"])
    assert db_module.parse_touches_resources(stored["touches_resources"]) == [
        "file:meridian/db/__init__.py", "db:migrations",
    ]


@pytest.mark.asyncio
async def test_add_sprint_item_rejects_bad_resource(db):
    p = await db_module.create_project(db, "alpha")
    with pytest.raises(ValueError):
        await db_module.add_sprint_item(
            db, p["id"], "v1", "feature", touches_resources=["garbage"],
        )


@pytest.mark.asyncio
async def test_add_sprint_item_without_resources_is_null(db):
    p = await db_module.create_project(db, "alpha")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "plain")
    stored = await db_module.get_sprint_item(db, item["id"])
    assert stored["touches_resources"] is None


@pytest.mark.asyncio
async def test_patch_sprint_item_sets_and_clears_resources(db):
    p = await db_module.create_project(db, "alpha")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "feature")
    # Set
    await db_module.patch_sprint_item(
        db, p["id"], item["id"], touches_resources=["mcp_tool:foo"],
    )
    stored = await db_module.get_sprint_item(db, item["id"])
    assert db_module.parse_touches_resources(stored["touches_resources"]) == ["mcp_tool:foo"]
    # Omitting the field leaves it untouched (sentinel)
    await db_module.patch_sprint_item(db, p["id"], item["id"], title="renamed")
    stored = await db_module.get_sprint_item(db, item["id"])
    assert stored["title"] == "renamed"
    assert db_module.parse_touches_resources(stored["touches_resources"]) == ["mcp_tool:foo"]
    # Clear with []
    await db_module.patch_sprint_item(db, p["id"], item["id"], touches_resources=[])
    stored = await db_module.get_sprint_item(db, item["id"])
    assert stored["touches_resources"] is None


@pytest.mark.asyncio
async def test_fan_out_sprint_items_accepts_resources(db):
    p = await db_module.create_project(db, "alpha")
    ids = await db_module.fan_out_sprint_items(db, p["id"], [
        {"title": "a", "touches_resources": ["file:a.py"]},
        {"title": "b", "touches_resources": ["bogus"]},  # bad → skipped, no crash
    ])
    assert len(ids) == 2
    first = await db_module.get_sprint_item(db, ids[0])
    second = await db_module.get_sprint_item(db, ids[1])
    assert db_module.parse_touches_resources(first["touches_resources"]) == ["file:a.py"]
    assert second["touches_resources"] is None


# ── resource_locks primitive ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_claim_and_release_resource(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "w1")
    res = await db_module.claim_resource(db, "db:migrations", s["id"])
    assert res["claimed"] is True
    assert res["resource_id"] == "db:migrations"
    assert res["resource_type"] == "db"
    # Re-claim by same session refreshes (idempotent)
    again = await db_module.claim_resource(db, "db:migrations", s["id"])
    assert again["claimed"] is True
    # Release
    assert await db_module.release_resource(db, "db:migrations", s["id"]) is True
    # Releasing again is a no-op False
    assert await db_module.release_resource(db, "db:migrations", s["id"]) is False


@pytest.mark.asyncio
async def test_claim_resource_conflict_between_sessions(db):
    p = await db_module.create_project(db, "alpha")
    s1 = await db_module.register_session(db, p["id"], "w1")
    s2 = await db_module.register_session(db, p["id"], "w2")
    first = await db_module.claim_resource(db, "pypi:publish", s1["id"])
    assert first["claimed"] is True
    second = await db_module.claim_resource(db, "pypi:publish", s2["id"])
    assert second["claimed"] is False
    assert second["holder_session_id"] == s1["id"]


@pytest.mark.asyncio
async def test_claim_resource_rejects_bad_identifier(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "w1")
    with pytest.raises(ValueError):
        await db_module.claim_resource(db, "not-typed", s["id"])


@pytest.mark.asyncio
async def test_get_resource_claims(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "w1")
    await db_module.claim_resource(db, "github:tag", s["id"])
    claims = await db_module.get_resource_claims(db, "github:tag")
    assert claims["resource_lock"] is not None
    assert claims["resource_lock"]["session_name"] == "w1"
    # Unheld resource → None
    empty = await db_module.get_resource_claims(db, "github:other")
    assert empty["resource_lock"] is None


@pytest.mark.asyncio
async def test_expire_resource_locks_by_ttl(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "w1")
    await db_module.claim_resource(db, "route:POST:/x", s["id"])
    # Force the lock to be expired
    await db.execute(
        "UPDATE resource_locks SET expires_at = datetime('now', '-1 hour')"
    )
    await db.commit()
    cleared = await db_module.expire_resource_locks(db)
    assert cleared == 1
    claims = await db_module.get_resource_claims(db, "route:POST:/x")
    assert claims["resource_lock"] is None


@pytest.mark.asyncio
async def test_get_resource_conflicts_overlap_and_exclude(db):
    p = await db_module.create_project(db, "alpha")
    s1 = await db_module.register_session(db, p["id"], "w1")
    s2 = await db_module.register_session(db, p["id"], "w2")
    await db_module.claim_resource(db, "file:meridian/server.py", s1["id"])
    # s2 wants two resources, one of which s1 holds
    conflicts = await db_module.get_resource_conflicts(
        db, p["id"], ["file:meridian/server.py", "db:migrations"],
        exclude_session_id=s2["id"],
    )
    assert len(conflicts) == 1
    assert conflicts[0]["resource_id"] == "file:meridian/server.py"
    assert conflicts[0]["session_id"] == s1["id"]
    # Excluding the holder yields no conflicts
    none_conf = await db_module.get_resource_conflicts(
        db, p["id"], ["file:meridian/server.py"], exclude_session_id=s1["id"],
    )
    assert none_conf == []


@pytest.mark.asyncio
async def test_close_session_releases_resource_locks(db):
    p = await db_module.create_project(db, "alpha")
    s = await db_module.register_session(db, p["id"], "w1")
    await db_module.claim_resource(db, "db:migrations", s["id"])
    await db_module.close_session(db, s["id"])
    # After close, the lock is gone (released_for_session)
    async with db.execute(
        "SELECT COUNT(*) AS c FROM resource_locks WHERE session_id = ?", (s["id"],)
    ) as cur:
        row = await cur.fetchone()
    assert (row["c"] if isinstance(row, dict) else row[0]) == 0


# ── get_parallelizable_groups (255096d9) ────────────────────────────────────

@pytest.mark.asyncio
async def test_parallelizable_groups_empty(db):
    p = await db_module.create_project(db, "alpha")
    res = await db_module.get_parallelizable_groups(db, p["id"])
    assert res["groups"] == []
    assert res["group_count"] == 0
    assert res["eligible_count"] == 0
    assert res["blocked"] == []


@pytest.mark.asyncio
async def test_parallelizable_groups_disjoint_resources_one_batch(db):
    p = await db_module.create_project(db, "alpha")
    await db_module.add_sprint_item(db, p["id"], "v1", "a", touches_resources=["file:a.py"])
    await db_module.add_sprint_item(db, p["id"], "v1", "b", touches_resources=["file:b.py"])
    res = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    assert res["group_count"] == 1
    assert res["eligible_count"] == 2
    assert len(res["groups"][0]) == 2


@pytest.mark.asyncio
async def test_parallelizable_groups_conflict_splits_batches(db):
    p = await db_module.create_project(db, "alpha")
    await db_module.add_sprint_item(db, p["id"], "v1", "a", touches_resources=["file:a.py"])
    await db_module.add_sprint_item(db, p["id"], "v1", "b", touches_resources=["file:b.py"])
    await db_module.add_sprint_item(db, p["id"], "v1", "c", touches_resources=["file:a.py"])
    res = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    # a and c both touch file:a.py → they must land in different batches.
    assert res["group_count"] == 2
    assert res["eligible_count"] == 3
    # Core invariant: within every group, no two items share a resource.
    for group in res["groups"]:
        seen: set[str] = set()
        for it in group:
            res_set = set(it["resources"])
            assert res_set.isdisjoint(seen), "conflicting items in same batch"
            seen.update(res_set)
    # a and c are never grouped together.
    for group in res["groups"]:
        titles = {it["title"] for it in group}
        assert not {"a", "c"}.issubset(titles)


@pytest.mark.asyncio
async def test_parallelizable_groups_undeclared_count(db):
    p = await db_module.create_project(db, "alpha")
    await db_module.add_sprint_item(db, p["id"], "v1", "x")  # no resources
    await db_module.add_sprint_item(db, p["id"], "v1", "y")  # no resources
    res = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    assert res["undeclared_count"] == 2
    # de730a25 — undeclared items can't be proven conflict-free, so each gets its
    # own sequential group instead of being co-scheduled in group 0.
    assert res["group_count"] == 2
    assert all(len(g) == 1 for g in res["groups"])


@pytest.mark.asyncio
async def test_parallelizable_groups_undeclared_isolated_from_declared(db):
    """de730a25 — an undeclared item never shares a group with declared items."""
    p = await db_module.create_project(db, "alpha")
    await db_module.add_sprint_item(db, p["id"], "v1", "a", touches_resources=["file:a.py"])
    await db_module.add_sprint_item(db, p["id"], "v1", "b", touches_resources=["file:b.py"])
    await db_module.add_sprint_item(db, p["id"], "v1", "u")  # undeclared
    res = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    assert res["undeclared_count"] == 1
    # Declared a + b (disjoint) share one parallel group; the undeclared item is
    # alone in its own group → 2 groups total.
    assert res["group_count"] == 2
    declared_group = next(g for g in res["groups"] if len(g) == 2)
    assert {it["title"] for it in declared_group} == {"a", "b"}
    undeclared_group = next(g for g in res["groups"] if len(g) == 1)
    assert undeclared_group[0]["title"] == "u"


@pytest.mark.asyncio
async def test_parallelizable_groups_blocked_by_unsatisfied_dependency(db):
    p = await db_module.create_project(db, "alpha")
    parent = await db_module.add_sprint_item(db, p["id"], "v1", "parent")
    child = await db_module.add_sprint_item(
        db, p["id"], "v1", "child", depends_on=parent["id"],
    )
    res = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    blocked_ids = {b["id"] for b in res["blocked"]}
    assert child["id"] in blocked_ids
    # parent is eligible (no dependency)
    eligible_ids = {it["id"] for g in res["groups"] for it in g}
    assert parent["id"] in eligible_ids
    assert child["id"] not in eligible_ids


@pytest.mark.asyncio
async def test_parallelizable_groups_dependency_satisfied_when_parent_done(db):
    p = await db_module.create_project(db, "alpha")
    parent = await db_module.add_sprint_item(db, p["id"], "v1", "parent")
    child = await db_module.add_sprint_item(
        db, p["id"], "v1", "child", depends_on=parent["id"],
    )
    await db_module.complete_sprint_item(db, p["id"], parent["id"])
    res = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    eligible_ids = {it["id"] for g in res["groups"] for it in g}
    assert child["id"] in eligible_ids
    assert res["blocked"] == []


@pytest.mark.asyncio
async def test_parallelizable_groups_failed_parent_continue_is_eligible(db):
    p = await db_module.create_project(db, "alpha")
    parent = await db_module.add_sprint_item(db, p["id"], "v1", "parent")
    child = await db_module.add_sprint_item(
        db, p["id"], "v1", "child", depends_on=parent["id"], failure_mode="continue",
    )
    await db_module.fail_sprint_item(db, p["id"], parent["id"], reason="boom")
    res = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    eligible_ids = {it["id"] for g in res["groups"] for it in g}
    assert child["id"] in eligible_ids


@pytest.mark.asyncio
async def test_parallelizable_groups_failed_parent_stop_is_blocked(db):
    p = await db_module.create_project(db, "alpha")
    parent = await db_module.add_sprint_item(db, p["id"], "v1", "parent")
    child = await db_module.add_sprint_item(
        db, p["id"], "v1", "child", depends_on=parent["id"], failure_mode="stop",
    )
    await db_module.fail_sprint_item(db, p["id"], parent["id"], reason="boom")
    res = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    blocked_ids = {b["id"] for b in res["blocked"]}
    assert child["id"] in blocked_ids


@pytest.mark.asyncio
async def test_parallelizable_groups_excludes_in_progress(db):
    p = await db_module.create_project(db, "alpha")
    a = await db_module.add_sprint_item(db, p["id"], "v1", "a")
    await db_module.add_sprint_item(db, p["id"], "v1", "b")
    await db_module.claim_sprint_item(db, p["id"], a["id"])  # now in_progress
    res = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    eligible_ids = {it["id"] for g in res["groups"] for it in g}
    assert a["id"] not in eligible_ids
    assert res["eligible_count"] == 1


@pytest.mark.asyncio
async def test_parallelizable_groups_via_mcp_handler(db):
    from meridian import server as srv
    p = await db_module.create_project(db, "alpha")
    await db_module.add_sprint_item(db, p["id"], "v1", "a", touches_resources=["file:a.py"])
    res = await srv._dispatch_mcp_tool(
        "get_parallelizable_groups", {"project_id": p["id"], "version": "v1"}, db, "/tmp",
    )
    assert res["group_count"] == 1
    assert res["eligible_count"] == 1
    # No undeclared items → no warning.
    assert "warning" not in res


@pytest.mark.asyncio
async def test_parallelizable_groups_mcp_handler_warns_on_undeclared(db):
    """de730a25 — the MCP tool surfaces a warning when items lack declarations."""
    from meridian import server as srv
    p = await db_module.create_project(db, "alpha")
    await db_module.add_sprint_item(db, p["id"], "v1", "u")  # undeclared
    res = await srv._dispatch_mcp_tool(
        "get_parallelizable_groups", {"project_id": p["id"], "version": "v1"}, db, "/tmp",
    )
    assert res["undeclared_count"] == 1
    assert "warning" in res
    assert "resource declarations" in res["warning"]


@pytest.mark.asyncio
async def test_orchestration_hint_surfaces_undeclared_warning(db, tmp_path):
    """de730a25 — start_session's orchestration hint flags undeclared items."""
    import meridian.server as srv
    p = await db_module.create_project(db, "orch-undeclared")
    pid = p["id"]
    await db_module.add_sprint_item(db, pid, "v0.1.x", "declared",
                                    touches_resources=["file:a.py"])
    await db_module.add_sprint_item(db, pid, "v0.1.x", "undeclared-one")
    payload = await srv._start_session_composite(
        db, pid, "orch-und-sess", str(tmp_path), version="v0.1.x", compact=True,
    )
    orch = payload["orchestration"]
    assert orch["undeclared_count"] == 1
    assert "warning" in orch
    assert "parallel safety" in orch["warning"]
