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
async def test_parallelizable_groups_running_field(db):
    """df573218 — in-flight (claimed) items surface under 'running'."""
    p = await db_module.create_project(db, "running1")
    a = await db_module.add_sprint_item(
        db, p["id"], "v1", "a", touches_resources=["file:a.py"], prospect_bypass=True
    )
    await db_module.add_sprint_item(db, p["id"], "v1", "b", touches_resources=["file:b.py"])
    await db_module.claim_sprint_item(db, p["id"], a["id"])
    res = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    running_ids = {r["id"] for r in res["running"]}
    assert a["id"] in running_ids
    # a is in flight → only b remains claimable.
    assert res["eligible_count"] == 1


def test_normalize_symbol_resource_id():
    """63b030a6 — symbol ids normalize the file-path part, keep the ::symbol scope."""
    assert db_module.normalize_resource_id(
        "symbol:meridian\\db\\__init__.py::create_project"
    ) == "symbol:meridian/db/__init__.py::create_project"
    assert db_module.normalize_resource_id(
        "symbol:./a.py::foo"
    ) == "symbol:a.py::foo"
    # symbol is a recognized type now.
    assert "symbol" in db_module.RESOURCE_TYPES


def test_two_resources_conflict_file_symbol_hierarchy():
    """63b030a6 — file ⊃ symbol conflict rules."""
    c = db_module._two_resources_conflict
    # Identical ids conflict.
    assert c("file:a.py", "file:a.py") is True
    assert c("symbol:a.py::f", "symbol:a.py::f") is True
    # Whole-file lock conflicts with any symbol on that file (either order).
    assert c("file:a.py", "symbol:a.py::f") is True
    assert c("symbol:a.py::f", "file:a.py") is True
    # Different symbols on the same file do NOT conflict.
    assert c("symbol:a.py::f", "symbol:a.py::g") is False
    # Symbols on different files don't conflict.
    assert c("symbol:a.py::f", "symbol:b.py::f") is False
    # Unrelated files don't conflict.
    assert c("file:a.py", "file:b.py") is False
    # Non-file types conflict only on exact equality.
    assert c("db:migrations", "db:migrations") is True
    assert c("db:migrations", "db:other") is False


@pytest.mark.asyncio
async def test_parallelizable_groups_symbol_level(db):
    """63b030a6 — two distinct symbols in the same file co-schedule; a whole-file
    item conflicts with a symbol item in that file."""
    p = await db_module.create_project(db, "symgroups")
    await db_module.add_sprint_item(db, p["id"], "v1", "edit f",
                                    touches_resources=["symbol:a.py::f"])
    await db_module.add_sprint_item(db, p["id"], "v1", "edit g",
                                    touches_resources=["symbol:a.py::g"])
    res = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    # Distinct symbols on the same file → one parallel group of two.
    assert res["group_count"] == 1
    assert len(res["groups"][0]) == 2


@pytest.mark.asyncio
async def test_parallelizable_groups_file_vs_symbol_conflict(db):
    p = await db_module.create_project(db, "symconf")
    await db_module.add_sprint_item(db, p["id"], "v1", "whole file",
                                    touches_resources=["file:a.py"])
    await db_module.add_sprint_item(db, p["id"], "v1", "one symbol",
                                    touches_resources=["symbol:a.py::f"])
    res = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    # file:a.py conflicts with symbol:a.py::f → must serialize into 2 groups.
    assert res["group_count"] == 2


_XLOCK_SRC = (
    "class AuthRouter:\n"
    "    def login(self):\n"
    "        return 1\n"
    "\n"
    "def helper():\n"
    "    return 3\n"
)


@pytest.mark.asyncio
async def test_claim_file_blocked_by_other_session_symbol_claim(db):
    """63b030a6 gap (1) — a whole-file claim is refused while another session
    holds a live symbol claim on that file."""
    p = await db_module.create_project(db, "xlock1")
    s1 = await db_module.register_session(db, p["id"], "sess-a")
    s2 = await db_module.register_session(db, p["id"], "sess-b")
    claim = await db_module.claim_symbol(db, s1["id"], "meridian/server.py", "AuthRouter", _XLOCK_SRC)
    assert claim["claimed"] is True
    blocked = await db_module.claim_file(db, "meridian/server.py", s2["id"])
    assert blocked["claimed"] is False
    assert blocked["reason"] == "symbol_locked"
    # The same session that holds the symbol claim is still allowed to whole-file
    # claim (no other-session symbol claims block it).
    same = await db_module.claim_file(db, "meridian/server.py", s1["id"])
    assert same["claimed"] is True


@pytest.mark.asyncio
async def test_claim_symbol_blocked_by_other_session_file_lock(db):
    """63b030a6 gap (2) — a symbol claim is refused while another session holds a
    whole-file lock on that file."""
    p = await db_module.create_project(db, "xlock2")
    s1 = await db_module.register_session(db, p["id"], "sess-a")
    s2 = await db_module.register_session(db, p["id"], "sess-b")
    fc = await db_module.claim_file(db, "meridian/server.py", s1["id"])
    assert fc["claimed"] is True
    blocked = await db_module.claim_symbol(db, s2["id"], "meridian/server.py", "AuthRouter", _XLOCK_SRC)
    assert blocked["claimed"] is False
    assert blocked["reason"] == "file_locked"


def test_normalize_resource_id_strips_inferred_marker():
    # 07bdfdbb — inferred resources canonicalize the same as explicit ones.
    assert db_module.normalize_resource_id(
        "inferred:file:meridian/server.py"
    ) == "file:meridian/server.py"
    assert db_module.normalize_resource_id(
        "file:meridian/server.py"
    ) == "file:meridian/server.py"


def test_serialize_touches_resources_preserves_inferred_marker():
    out = db_module.serialize_touches_resources(["inferred:file:a.py"])
    assert json.loads(out) == ["inferred:file:a.py"]
    # Explicit resources are stored without a marker.
    out2 = db_module.serialize_touches_resources(["file:a.py"])
    assert json.loads(out2) == ["file:a.py"]


def test_parse_touches_resources_strips_inferred_for_comparison():
    # Stored marker is dropped for the conflict-comparison id.
    assert db_module.parse_touches_resources(["inferred:file:a.py"]) == ["file:a.py"]


@pytest.mark.asyncio
async def test_inferred_resource_conflicts_with_explicit(db):
    """07bdfdbb — an inferred resource collides with an explicit one for the same
    file, so the two items are serialized into different groups."""
    p = await db_module.create_project(db, "infconf")
    await db_module.add_sprint_item(
        db, p["id"], "v1", "a", touches_resources=["file:meridian/server.py"]
    )
    await db_module.add_sprint_item(
        db, p["id"], "v1", "b", touches_resources=["inferred:file:meridian/server.py"]
    )
    res = await db_module.get_parallelizable_groups(db, p["id"], version="v1")
    assert res["undeclared_count"] == 0  # both have resources
    assert res["group_count"] == 2       # same file → can't co-schedule


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


# ---------------------------------------------------------------------------
# 18c488b6 — symbol-scoped resource-lock gate at claim_sprint_item time
#
# claim_sprint_item() previously enforced status/prospecting gates only; the
# MCP claim path's file-conflict check (_sprint_item_file_claim_conflicts)
# compared touches_files against file_locks and never even looked at
# touches_resources, so a symbol: declaration was pure metadata with no
# actual claim-time enforcement. These tests exercise the new transactional
# gate (meridian.mcp.handler._sprint_item_resource_claim_gate), wired into
# handle_claim_sprint_item, which ACQUIRES a real file/symbol lock for every
# declared resource under the caller's session_id.
# ---------------------------------------------------------------------------

_FOO_BAR_SRC = (
    "def foo():\n"
    "    return 1\n"
    "\n"
    "\n"
    "def bar():\n"
    "    return 2\n"
)


@pytest.mark.asyncio
async def test_claim_sprint_item_symbol_resource_acquires_real_claim(db):
    """18c488b6 — a symbol: resource with source content supplied gets a REAL
    AST-resolved symbol claim (not just a whole-file lock), and the response
    exposes machine-readable lock_scope metadata."""
    import meridian.server as srv

    p = await db_module.create_project(db, "18c488b6-basic")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "w1")
    sid = sess["id"]

    item = await db_module.add_sprint_item(
        db, pid, "v1", "Refactor foo",
        touches_resources=["symbol:pkg/mod.py::foo"],
        prospect_bypass=True,
    )

    result = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {
            "project_id": pid, "item_id": item["id"], "session_id": sid,
            "resource_contents": {"pkg/mod.py": _FOO_BAR_SRC},
        },
        db, "/tmp",
    )
    assert "error" not in result and not result.get("blocked")
    scope = result.get("resource_lock_scope")
    assert scope and len(scope) == 1
    entry = scope[0]
    assert entry["scope"] == "symbol"
    assert entry["file_path"] == "pkg/mod.py"
    assert entry["symbol"] == "foo"
    assert entry["acquired"] is True
    assert "fallback_reason" not in entry

    # The claim really did land in the shared file_symbol_claims table (not
    # just a claim_sprint_item-local bookkeeping structure).
    live = await db_module.get_symbol_claims(db, "pkg/mod.py")
    assert any(c["symbol_name"] == "foo" and c["session_id"] == sid for c in live)


@pytest.mark.asyncio
async def test_claim_sprint_item_same_symbol_concurrent_claims_exactly_one_winner(db):
    """18c488b6 — two DIFFERENT sprint items that both declare the SAME
    symbol: resource must have exactly one winner; the loser is hard-blocked
    (RESOURCE_LOCKED) and stays pending, not silently re-scheduled at the
    file level."""
    import meridian.server as srv

    p = await db_module.create_project(db, "18c488b6-same-symbol")
    pid = p["id"]
    s1 = await db_module.register_session(db, pid, "w1")
    s2 = await db_module.register_session(db, pid, "w2")

    item_a = await db_module.add_sprint_item(
        db, pid, "v1", "Touch foo (a)",
        touches_resources=["symbol:pkg/mod.py::foo"], prospect_bypass=True,
    )
    item_b = await db_module.add_sprint_item(
        db, pid, "v1", "Touch foo (b)",
        touches_resources=["symbol:pkg/mod.py::foo"], prospect_bypass=True,
        force=True,  # near-duplicate title vs item_a — force past the title-similarity guard
    )
    contents = {"resource_contents": {"pkg/mod.py": _FOO_BAR_SRC}}

    winner = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {"project_id": pid, "item_id": item_a["id"], "session_id": s1["id"], **contents},
        db, "/tmp",
    )
    assert "error" not in winner and not winner.get("blocked")

    loser = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {"project_id": pid, "item_id": item_b["id"], "session_id": s2["id"], **contents},
        db, "/tmp",
    )
    assert loser["error"] == "RESOURCE_LOCKED"
    assert loser["conflicts"][0]["conflict"]["reason"] == "symbol_conflict"

    reread_b = await db_module.get_sprint_item(db, item_b["id"])
    assert reread_b["status"] == "pending"
    reread_a = await db_module.get_sprint_item(db, item_a["id"])
    assert reread_a["status"] == "in_progress"


@pytest.mark.asyncio
async def test_claim_sprint_item_disjoint_symbols_same_file_both_succeed(db):
    """18c488b6 — two items declaring DIFFERENT symbols in the SAME file must
    BOTH be able to claim (the core win over file-level-only scheduling)."""
    import meridian.server as srv

    p = await db_module.create_project(db, "18c488b6-disjoint")
    pid = p["id"]
    s1 = await db_module.register_session(db, pid, "w1")
    s2 = await db_module.register_session(db, pid, "w2")

    item_foo = await db_module.add_sprint_item(
        db, pid, "v1", "Touch foo",
        touches_resources=["symbol:pkg/mod.py::foo"], prospect_bypass=True,
    )
    item_bar = await db_module.add_sprint_item(
        db, pid, "v1", "Touch bar",
        touches_resources=["symbol:pkg/mod.py::bar"], prospect_bypass=True,
        force=True,  # similar title vs item_foo — force past the title-similarity guard
    )
    contents = {"resource_contents": {"pkg/mod.py": _FOO_BAR_SRC}}

    res_foo = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {"project_id": pid, "item_id": item_foo["id"], "session_id": s1["id"], **contents},
        db, "/tmp",
    )
    res_bar = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {"project_id": pid, "item_id": item_bar["id"], "session_id": s2["id"], **contents},
        db, "/tmp",
    )
    assert "error" not in res_foo and not res_foo.get("blocked")
    assert "error" not in res_bar and not res_bar.get("blocked")
    assert (await db_module.get_sprint_item(db, item_foo["id"]))["status"] == "in_progress"
    assert (await db_module.get_sprint_item(db, item_bar["id"]))["status"] == "in_progress"


@pytest.mark.asyncio
async def test_claim_sprint_item_file_lock_blocks_later_symbol_claim(db):
    """18c488b6 — file ⊃ symbol hierarchy, direction 1: a whole-file resource
    claimed first hard-blocks a later item's symbol: claim on that file."""
    import meridian.server as srv

    p = await db_module.create_project(db, "18c488b6-file-then-symbol")
    pid = p["id"]
    s1 = await db_module.register_session(db, pid, "w1")
    s2 = await db_module.register_session(db, pid, "w2")

    item_file = await db_module.add_sprint_item(
        db, pid, "v1", "Whole file edit",
        touches_resources=["file:pkg/mod.py"], prospect_bypass=True,
    )
    item_sym = await db_module.add_sprint_item(
        db, pid, "v1", "Just foo",
        touches_resources=["symbol:pkg/mod.py::foo"], prospect_bypass=True,
        force=True,
    )

    ok = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {"project_id": pid, "item_id": item_file["id"], "session_id": s1["id"]},
        db, "/tmp",
    )
    assert "error" not in ok and not ok.get("blocked")

    blocked = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {
            "project_id": pid, "item_id": item_sym["id"], "session_id": s2["id"],
            "resource_contents": {"pkg/mod.py": _FOO_BAR_SRC},
        },
        db, "/tmp",
    )
    assert blocked["error"] == "RESOURCE_LOCKED"
    assert blocked["conflicts"][0]["conflict"]["reason"] == "file_locked"
    assert (await db_module.get_sprint_item(db, item_sym["id"]))["status"] == "pending"


@pytest.mark.asyncio
async def test_claim_sprint_item_symbol_claim_blocks_later_file_lock(db):
    """18c488b6 — file ⊃ symbol hierarchy, direction 2: a live symbol claim
    hard-blocks a later item's whole-file: claim on that same file."""
    import meridian.server as srv

    p = await db_module.create_project(db, "18c488b6-symbol-then-file")
    pid = p["id"]
    s1 = await db_module.register_session(db, pid, "w1")
    s2 = await db_module.register_session(db, pid, "w2")

    item_sym = await db_module.add_sprint_item(
        db, pid, "v1", "Just foo",
        touches_resources=["symbol:pkg/mod.py::foo"], prospect_bypass=True,
    )
    item_file = await db_module.add_sprint_item(
        db, pid, "v1", "Whole file edit",
        touches_resources=["file:pkg/mod.py"], prospect_bypass=True,
        force=True,
    )

    ok = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {
            "project_id": pid, "item_id": item_sym["id"], "session_id": s1["id"],
            "resource_contents": {"pkg/mod.py": _FOO_BAR_SRC},
        },
        db, "/tmp",
    )
    assert "error" not in ok and not ok.get("blocked")

    blocked = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {"project_id": pid, "item_id": item_file["id"], "session_id": s2["id"]},
        db, "/tmp",
    )
    assert blocked["error"] == "RESOURCE_LOCKED"
    assert blocked["conflicts"][0]["conflict"]["reason"] == "symbol_locked"
    assert (await db_module.get_sprint_item(db, item_file["id"]))["status"] == "pending"


@pytest.mark.asyncio
async def test_claim_sprint_item_symbol_fallback_no_source_supplied_is_explicit(db):
    """18c488b6 — a symbol: resource with NO resource_contents supplied falls
    back to a whole-file lock, and the fallback is EXPLICIT (a fallback_reason
    on the lock_scope entry), never a silent downgrade."""
    import meridian.server as srv

    p = await db_module.create_project(db, "18c488b6-no-source")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "w1")

    item = await db_module.add_sprint_item(
        db, pid, "v1", "Touch foo, no content",
        touches_resources=["symbol:pkg/mod.py::foo"], prospect_bypass=True,
    )
    result = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {"project_id": pid, "item_id": item["id"], "session_id": sess["id"]},
        db, "/tmp",
    )
    assert "error" not in result and not result.get("blocked")
    entry = result["resource_lock_scope"][0]
    assert entry["scope"] == "file"
    assert entry["file_path"] == "pkg/mod.py"
    assert entry["acquired"] is True
    assert entry["fallback_reason"] == "no_source_supplied"

    # It really is a whole-file lock, not a symbol-range claim.
    file_claims = await db_module.get_file_claims(db, "pkg/mod.py")
    assert file_claims["file_lock"] is not None
    assert file_claims["file_lock"]["session_id"] == sess["id"]


@pytest.mark.asyncio
async def test_claim_sprint_item_symbol_fallback_symbol_not_found_is_explicit(db):
    """18c488b6 — content IS supplied but the declared symbol name isn't in it
    (drift between the declaration and the real code) also falls back to a
    whole-file lock with an explicit, distinct fallback_reason."""
    import meridian.server as srv

    p = await db_module.create_project(db, "18c488b6-sym-not-found")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "w1")

    item = await db_module.add_sprint_item(
        db, pid, "v1", "Touch missing symbol",
        touches_resources=["symbol:pkg/mod.py::does_not_exist"], prospect_bypass=True,
    )
    result = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {
            "project_id": pid, "item_id": item["id"], "session_id": sess["id"],
            "resource_contents": {"pkg/mod.py": _FOO_BAR_SRC},
        },
        db, "/tmp",
    )
    assert "error" not in result and not result.get("blocked")
    entry = result["resource_lock_scope"][0]
    assert entry["scope"] == "file"
    assert entry["acquired"] is True
    assert entry["fallback_reason"] == "symbol_not_found"


@pytest.mark.asyncio
async def test_claim_sprint_item_resource_lock_rolls_back_on_deferred_block(db):
    """18c488b6 — when a LATER structural gate (DEFERRED here) refuses the
    claim, any resource lock the new gate already acquired must be rolled
    back, not left orphaned holding a lock for an item that was never
    actually claimed."""
    import meridian.server as srv
    from datetime import datetime, timedelta

    p = await db_module.create_project(db, "18c488b6-rollback-deferred")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "w1")

    future = (datetime.utcnow() + timedelta(hours=48)).isoformat()
    item = await db_module.add_sprint_item(
        db, pid, "v1", "Deferred symbol work",
        touches_resources=["symbol:pkg/mod.py::foo"], prospect_bypass=True,
        deferred_until=future,
    )
    result = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {
            "project_id": pid, "item_id": item["id"], "session_id": sess["id"],
            "resource_contents": {"pkg/mod.py": _FOO_BAR_SRC},
        },
        db, "/tmp",
    )
    assert result.get("blocked") is True
    assert result.get("error") == "DEFERRED"
    reread = await db_module.get_sprint_item(db, item["id"])
    assert reread["status"] == "pending"

    # The resource lock must NOT have been left behind.
    live_symbols = await db_module.get_symbol_claims(db, "pkg/mod.py")
    assert live_symbols == []
    file_claims = await db_module.get_file_claims(db, "pkg/mod.py")
    assert file_claims["file_lock"] is None


@pytest.mark.asyncio
async def test_claim_sprint_item_same_session_reclaim_is_idempotent(db):
    """18c488b6 — a session re-calling claim_sprint_item on its OWN
    already-in-progress item must not be treated as a resource conflict: the
    resource gate refreshes its own held claim harmlessly, and the existing
    already_claimed response is unchanged."""
    import meridian.server as srv

    p = await db_module.create_project(db, "18c488b6-idempotent")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "w1")

    item = await db_module.add_sprint_item(
        db, pid, "v1", "Reclaim me",
        touches_resources=["symbol:pkg/mod.py::foo"], prospect_bypass=True,
    )
    contents = {"resource_contents": {"pkg/mod.py": _FOO_BAR_SRC}}
    first = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {"project_id": pid, "item_id": item["id"], "session_id": sess["id"], **contents},
        db, "/tmp",
    )
    assert "error" not in first and not first.get("blocked")

    second = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {"project_id": pid, "item_id": item["id"], "session_id": sess["id"], **contents},
        db, "/tmp",
    )
    assert second.get("status") == "already_claimed"

    # The session's own symbol claim survived the re-claim attempt (not
    # rolled back as if it were a stranger's failed acquisition).
    live_symbols = await db_module.get_symbol_claims(db, "pkg/mod.py")
    assert any(
        c["symbol_name"] == "foo" and c["session_id"] == sess["id"] for c in live_symbols
    )


@pytest.mark.asyncio
async def test_claim_sprint_item_no_regression_touches_files_conflict_gate(db):
    """18c488b6 — no regression: the pre-existing touches_files CONFLICT gate
    (file-only, no session_id at all) still works unchanged when a caller
    never supplies session_id, which also exercises the new gate's fail-open
    'no_session_id' skip path."""
    import json
    import meridian.server as srv

    p = await db_module.create_project(db, "18c488b6-no-regression")
    await db_module.update_project_settings(db, p["id"], auto_worktrees=0)
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Edit dashboard")
    await db.execute(
        "UPDATE sprint_items SET touches_files = ? WHERE id = ?",
        (json.dumps(["meridian/static/dashboard.js"]), item["id"]),
    )
    session = await db_module.register_session(db, p["id"], "dashboard-worker")
    await db_module.claim_file(db, "meridian/static/dashboard.js", session["id"])

    result = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {"project_id": p["id"], "item_id": item["id"]},  # no session_id
        db, "/tmp",
    )
    assert result["error"] == "CONFLICT"
    assert result["conflicts"][0]["file_path"] == "meridian/static/dashboard.js"
    reread = await db_module.get_sprint_item(db, item["id"])
    assert reread["status"] == "pending"


@pytest.mark.asyncio
async def test_claim_sprint_item_no_regression_unprospected_gate_still_blocks(db):
    """18c488b6 — no regression: a resource-declaring item with no durable
    pointer evidence is still refused by the pre-existing UNPROSPECTED gate,
    even though it now ALSO carries a symbol: resource the new lock gate
    could otherwise acquire."""
    import meridian.server as srv

    p = await db_module.create_project(db, "18c488b6-unprospected")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "w1")
    item = await db_module.add_sprint_item(
        db, pid, "v1", "Not prospected",
        touches_resources=["symbol:pkg/mod.py::foo"],  # no prospect_bypass
    )
    result = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {
            "project_id": pid, "item_id": item["id"], "session_id": sess["id"],
            "resource_contents": {"pkg/mod.py": _FOO_BAR_SRC},
        },
        db, "/tmp",
    )
    assert result.get("blocked") is True
    assert result.get("error") == "UNPROSPECTED"
    reread = await db_module.get_sprint_item(db, item["id"])
    assert reread["status"] == "pending"
    # No orphaned resource lock either.
    live_symbols = await db_module.get_symbol_claims(db, "pkg/mod.py")
    assert live_symbols == []


@pytest.mark.asyncio
async def test_sprint_item_resource_claim_gate_skips_without_session_id(db):
    """18c488b6 — direct unit check: the gate itself fail-opens (ok=True,
    nothing acquired) when session_id is falsy, so every pre-18c488b6 caller
    of db_module.claim_sprint_item() that never threads a session_id through
    sees zero behavior change."""
    import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
    from meridian.mcp.handler import _sprint_item_resource_claim_gate

    p = await db_module.create_project(db, "18c488b6-gate-unit")
    pid = p["id"]
    item = await db_module.add_sprint_item(
        db, pid, "v1", "no session",
        touches_resources=["symbol:pkg/mod.py::foo"], prospect_bypass=True,
    )
    gate = await _sprint_item_resource_claim_gate(db, pid, item["id"], None)
    assert gate == {"ok": True, "lock_scope": [], "skipped_reason": "no_session_id"}


@pytest.mark.asyncio
async def test_sprint_item_resource_claim_gate_bare_symbol_no_file_scope(db):
    """18c488b6 — a bare 'symbol:<name>' resource (no '::' file scope, the
    handoff-side shorthand) has nothing locatable to lock: the gate records an
    explicit no_file_scope entry rather than crashing or silently skipping."""
    import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
    from meridian.mcp.handler import _sprint_item_resource_claim_gate

    p = await db_module.create_project(db, "18c488b6-bare-symbol")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "w1")
    item = await db_module.add_sprint_item(
        db, pid, "v1", "bare symbol",
        touches_resources=["symbol:_helper_fn"], prospect_bypass=True,
    )
    gate = await _sprint_item_resource_claim_gate(db, pid, item["id"], sess["id"])
    assert gate["ok"] is True
    assert gate["lock_scope"][0]["fallback_reason"] == "no_file_scope"
    assert gate["lock_scope"][0]["acquired"] is False


@pytest.mark.asyncio
async def test_sprint_item_resource_claim_gate_rolls_back_earlier_resource_in_same_call(db):
    """18c488b6 — one item declares TWO resources; the first is free and gets
    acquired, but the second conflicts with another live session. The whole
    gate call must be all-or-nothing: the first resource's lock (acquired
    earlier in THIS same call) must be rolled back, not left dangling."""
    import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
    from meridian.mcp.handler import _sprint_item_resource_claim_gate

    p = await db_module.create_project(db, "18c488b6-in-call-rollback")
    pid = p["id"]
    holder = await db_module.register_session(db, pid, "holder")
    claimant = await db_module.register_session(db, pid, "claimant")

    # holder pre-claims symbol foo in b.py via the plain claim_symbol primitive
    # (simulating unrelated prior work, not through claim_sprint_item at all).
    pre = await db_module.claim_symbol(db, holder["id"], "b.py", "foo", _FOO_BAR_SRC)
    assert pre["claimed"] is True

    item = await db_module.add_sprint_item(
        db, pid, "v1", "touches two resources",
        touches_resources=["file:a.py", "symbol:b.py::foo"],
        prospect_bypass=True,
    )
    gate = await _sprint_item_resource_claim_gate(
        db, pid, item["id"], claimant["id"],
        resource_contents={"b.py": _FOO_BAR_SRC},
    )
    assert gate["ok"] is False
    assert gate["error"] == "RESOURCE_LOCKED"

    # a.py must have been rolled back -- claimant does not hold it.
    file_claims = await db_module.get_file_claims(db, "a.py")
    assert file_claims["file_lock"] is None

    # holder's pre-existing symbol claim on b.py is untouched.
    live_symbols = await db_module.get_symbol_claims(db, "b.py")
    assert any(
        c["symbol_name"] == "foo" and c["session_id"] == holder["id"] for c in live_symbols
    )


# ---------------------------------------------------------------------------
# 2a176d6d — lifecycle-audit findings (1)/(3)/(4) enforced at the
# claim_sprint_item (handle_claim_sprint_item) call site, layered ON TOP of
# the low-level gate (_sprint_item_resource_claim_gate) rather than changing
# that function's own fail-open/fail-soft contract, which is pinned by its
# own direct unit tests above.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_sprint_item_missing_session_id_with_declared_resources_is_refused(db):
    """2a176d6d (finding 1) — the real MCP claim_sprint_item entry point must
    fail CLOSED when no session_id is supplied and the item declares
    touches_resources, instead of silently walking away with zero lock
    protection (the low-level gate's own fail-open behavior)."""
    import meridian.server as srv

    p = await db_module.create_project(db, "2a176d6d-no-identity")
    pid = p["id"]
    item = await db_module.add_sprint_item(
        db, pid, "v1", "no identity supplied",
        touches_resources=["symbol:pkg/mod.py::foo"], prospect_bypass=True,
    )
    result = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {"project_id": pid, "item_id": item["id"]},  # no session_id
        db, "/tmp",
    )
    assert result.get("ok") is False
    assert result["error"] == "MISSING_EXECUTION_IDENTITY"
    reread = await db_module.get_sprint_item(db, item["id"])
    assert reread["status"] == "pending"


@pytest.mark.asyncio
async def test_claim_sprint_item_missing_session_id_no_resources_is_unaffected(db):
    """2a176d6d (finding 1) — an item with NOTHING declared has nothing to
    protect, so the new identity gate must stay a no-op for it (matches the
    low-level gate's own 'genuinely nothing to enforce' carve-out)."""
    import meridian.server as srv

    p = await db_module.create_project(db, "2a176d6d-no-identity-noop")
    pid = p["id"]
    item = await db_module.add_sprint_item(db, pid, "v1", "nothing declared")
    result = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {"project_id": pid, "item_id": item["id"]},  # no session_id
        db, "/tmp",
    )
    assert result.get("error") != "MISSING_EXECUTION_IDENTITY"


@pytest.mark.asyncio
async def test_claim_sprint_item_bare_symbol_resource_is_rejected(db):
    """2a176d6d (finding 3) — a bare 'symbol:<name>' declaration (no '::'
    file scope) acquires zero lock; the real claim_sprint_item path must hard
    -block it (MALFORMED_RESOURCE) rather than silently reporting an
    'ok' claim with no actual protection."""
    import meridian.server as srv

    p = await db_module.create_project(db, "2a176d6d-bare-symbol-claim")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "w1")
    item = await db_module.add_sprint_item(
        db, pid, "v1", "bare symbol declared",
        touches_resources=["symbol:_helper_fn"], prospect_bypass=True,
    )
    result = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {"project_id": pid, "item_id": item["id"], "session_id": sess["id"]},
        db, "/tmp",
    )
    assert result.get("ok") is False
    assert result["error"] == "MALFORMED_RESOURCE"
    assert "symbol:_helper_fn" in result["malformed_resources"]
    reread = await db_module.get_sprint_item(db, item["id"])
    assert reread["status"] == "pending"


@pytest.mark.asyncio
async def test_claim_sprint_item_bare_symbol_rolls_back_earlier_resource(db):
    """2a176d6d (finding 3) — an item declaring a GOOD file: resource AND a
    malformed bare symbol: resource must roll back the good one too: the
    whole claim is refused, all-or-nothing, exactly like a real conflict."""
    import meridian.server as srv

    p = await db_module.create_project(db, "2a176d6d-bare-symbol-rollback")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "w1")
    item = await db_module.add_sprint_item(
        db, pid, "v1", "mixed good and malformed",
        touches_resources=["file:a.py", "symbol:_helper_fn"], prospect_bypass=True,
    )
    result = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {"project_id": pid, "item_id": item["id"], "session_id": sess["id"]},
        db, "/tmp",
    )
    assert result.get("ok") is False
    assert result["error"] == "MALFORMED_RESOURCE"
    # a.py must NOT have been left locked behind the refused claim.
    file_claims = await db_module.get_file_claims(db, "a.py")
    assert file_claims["file_lock"] is None
    reread = await db_module.get_sprint_item(db, item["id"])
    assert reread["status"] == "pending"


@pytest.mark.asyncio
async def test_claim_sprint_item_claim_granularity_marks_fallback_as_coarse(db):
    """2a176d6d (finding 4) — a symbol: resource that widened to a whole-file
    lock (no resource_contents supplied) must be classified 'coarse' in
    claim_granularity, never 'symbol' or bare 'file' (which would look
    indistinguishable from a genuinely-declared file: resource)."""
    import meridian.server as srv

    p = await db_module.create_project(db, "2a176d6d-granularity-coarse")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "w1")
    item = await db_module.add_sprint_item(
        db, pid, "v1", "symbol without source",
        touches_resources=["symbol:pkg/mod.py::foo"], prospect_bypass=True,
    )
    result = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {"project_id": pid, "item_id": item["id"], "session_id": sess["id"]},
        db, "/tmp",
    )
    assert "error" not in result and not result.get("blocked")
    assert result["claim_granularity"]["symbol:pkg/mod.py::foo"] == "coarse"


@pytest.mark.asyncio
async def test_claim_sprint_item_claim_granularity_marks_real_symbol_claim(db):
    """2a176d6d (finding 4) — a REAL AST-resolved symbol claim (source
    supplied) must be classified 'symbol', not 'coarse'."""
    import meridian.server as srv

    p = await db_module.create_project(db, "2a176d6d-granularity-symbol")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "w1")
    item = await db_module.add_sprint_item(
        db, pid, "v1", "symbol with source",
        touches_resources=["symbol:pkg/mod.py::foo"], prospect_bypass=True,
    )
    result = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {
            "project_id": pid, "item_id": item["id"], "session_id": sess["id"],
            "resource_contents": {"pkg/mod.py": _FOO_BAR_SRC},
        },
        db, "/tmp",
    )
    assert "error" not in result and not result.get("blocked")
    assert result["claim_granularity"]["symbol:pkg/mod.py::foo"] == "symbol"


@pytest.mark.asyncio
async def test_claim_sprint_item_claim_granularity_marks_real_file_claim(db):
    """2a176d6d (finding 4) — a genuinely-declared file: resource must be
    classified 'file', distinct from a symbol: resource that fell back to a
    file lock ('coarse')."""
    import meridian.server as srv

    p = await db_module.create_project(db, "2a176d6d-granularity-file")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "w1")
    item = await db_module.add_sprint_item(
        db, pid, "v1", "whole file declared",
        touches_resources=["file:a.py"], prospect_bypass=True,
    )
    result = await srv._dispatch_mcp_tool(
        "claim_sprint_item",
        {"project_id": pid, "item_id": item["id"], "session_id": sess["id"]},
        db, "/tmp",
    )
    assert "error" not in result and not result.get("blocked")
    assert result["claim_granularity"]["file:a.py"] == "file"


# ---------------------------------------------------------------------------
# 22cad9b8 — atomic batch claim: claim_parallel_batch reserves an ENTIRE
# parallel-safe batch (every item's status AND every declared resource)
# atomically, before workers launch. Closes the gap between
# get_parallelizable_groups computing a safe batch and each worker
# individually calling claim_sprint_item afterward.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_parallel_batch_disjoint_resources_all_succeed(db):
    """The core happy path: a batch of items with pairwise-disjoint declared
    resources all claim atomically, each item lands in_progress, and an
    immutable 'claimed' manifest is persisted."""
    p = await db_module.create_project(db, "batch-happy")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    a = await db_module.add_sprint_item(
        db, pid, "v1", "a", touches_resources=["file:a.py"], prospect_bypass=True,
    )
    b = await db_module.add_sprint_item(
        db, pid, "v1", "b", touches_resources=["file:b.py"], prospect_bypass=True,
    )
    result = await db_module.claim_parallel_batch(
        db, pid, sess["id"], [a["id"], b["id"]],
    )
    assert result["ok"] is True
    assert set(result["claimed_item_ids"]) == {a["id"], b["id"]}
    assert result["resources"] == ["file:a.py", "file:b.py"]
    assert result["manifest"]["status"] == "claimed"

    reread_a = await db_module.get_sprint_item(db, a["id"])
    reread_b = await db_module.get_sprint_item(db, b["id"])
    assert reread_a["status"] == "in_progress"
    assert reread_b["status"] == "in_progress"
    assert reread_a["actor"] == sess["id"]
    assert reread_b["actor"] == sess["id"]

    # Both file locks were really acquired under the claiming session.
    fa = await db_module.get_file_claims(db, "a.py")
    fb = await db_module.get_file_claims(db, "b.py")
    assert fa["file_lock"]["session_id"] == sess["id"]
    assert fb["file_lock"]["session_id"] == sess["id"]


@pytest.mark.asyncio
async def test_claim_parallel_batch_distinct_item_sessions(db):
    """item_sessions pre-assigns each item to the DISTINCT worker session that
    will actually execute it — resources end up held under the session doing
    the work, no hand-off needed once workers launch."""
    p = await db_module.create_project(db, "batch-distinct-sessions")
    pid = p["id"]
    orchestrator = await db_module.register_session(db, pid, "orchestrator")
    w1 = await db_module.register_session(db, pid, "worker-1")
    w2 = await db_module.register_session(db, pid, "worker-2")
    a = await db_module.add_sprint_item(
        db, pid, "v1", "a", touches_resources=["file:a.py"], prospect_bypass=True,
    )
    b = await db_module.add_sprint_item(
        db, pid, "v1", "b", touches_resources=["file:b.py"], prospect_bypass=True,
    )
    result = await db_module.claim_parallel_batch(
        db, pid, orchestrator["id"], [a["id"], b["id"]],
        item_sessions={a["id"]: w1["id"], b["id"]: w2["id"]},
    )
    assert result["ok"] is True
    reread_a = await db_module.get_sprint_item(db, a["id"])
    reread_b = await db_module.get_sprint_item(db, b["id"])
    assert reread_a["actor"] == w1["id"]
    assert reread_b["actor"] == w2["id"]
    fa = await db_module.get_file_claims(db, "a.py")
    fb = await db_module.get_file_claims(db, "b.py")
    assert fa["file_lock"]["session_id"] == w1["id"]
    assert fb["file_lock"]["session_id"] == w2["id"]
    # The manifest itself still records the orchestrating session.
    assert result["manifest"]["session_id"] == orchestrator["id"]


@pytest.mark.asyncio
async def test_claim_parallel_batch_external_conflict_rolls_back_everything(db):
    """If item B's resource is already held by ANOTHER live session, the
    WHOLE batch fails cleanly: item A's already-claimed status/resource
    (acquired earlier in this same call) must be rolled back too — no
    partial-claim state left behind."""
    p = await db_module.create_project(db, "batch-external-conflict")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    stranger = await db_module.register_session(db, pid, "stranger")
    a = await db_module.add_sprint_item(
        db, pid, "v1", "a", touches_resources=["file:a.py"], prospect_bypass=True,
    )
    b = await db_module.add_sprint_item(
        db, pid, "v1", "b", touches_resources=["file:b.py"], prospect_bypass=True,
    )
    # A stranger session already holds file:b.py before the batch is attempted.
    pre = await db_module.claim_file(db, "b.py", stranger["id"])
    assert pre["claimed"] is True

    result = await db_module.claim_parallel_batch(
        db, pid, sess["id"], [a["id"], b["id"]],
    )
    assert result["ok"] is False
    assert result["error"] == "BATCH_RESOURCE_CONFLICT"
    assert result["item_id"] == b["id"]
    assert result["holder_session_id"] == stranger["id"]

    # Item a's claim (which succeeded before b's resource conflicted) was
    # fully rolled back: status reverted to pending, no orphaned file lock.
    reread_a = await db_module.get_sprint_item(db, a["id"])
    assert reread_a["status"] == "pending"
    assert reread_a["claimed_at"] is None
    fa = await db_module.get_file_claims(db, "a.py")
    assert fa["file_lock"] is None

    # Item b was never claimed at all.
    reread_b = await db_module.get_sprint_item(db, b["id"])
    assert reread_b["status"] == "pending"

    # The stranger's pre-existing lock on b.py is untouched.
    fb = await db_module.get_file_claims(db, "b.py")
    assert fb["file_lock"]["session_id"] == stranger["id"]

    # The manifest recorded the failure — durable audit trail.
    manifest = await db_module.get_batch_claim_manifest(
        db, pid, db_module.compute_batch_key([a["id"], b["id"]]),
    )
    assert manifest["status"] == "failed"
    assert manifest["failure_detail"]["error"] == "BATCH_RESOURCE_CONFLICT"


@pytest.mark.asyncio
async def test_claim_parallel_batch_undeclared_resource_blocks_multi_item_batch(db):
    """22cad9b8/de730a25 — an item with NO declared touches_resources can
    never be PROVEN parallel-safe. Including it in a multi-item batch must be
    refused outright (never silently treated as conflict-free), even though
    its OTHER batch-mate has a perfectly valid declaration."""
    p = await db_module.create_project(db, "batch-undeclared")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    a = await db_module.add_sprint_item(
        db, pid, "v1", "a", touches_resources=["file:a.py"], prospect_bypass=True,
    )
    u = await db_module.add_sprint_item(db, pid, "v1", "undeclared")  # no resources

    result = await db_module.claim_parallel_batch(db, pid, sess["id"], [a["id"], u["id"]])
    assert result["ok"] is False
    assert result["error"] == "UNDECLARED_RESOURCE_IN_BATCH"
    assert u["id"] in result["undeclared_item_ids"]

    # Neither item was touched.
    assert (await db_module.get_sprint_item(db, a["id"]))["status"] == "pending"
    assert (await db_module.get_sprint_item(db, u["id"]))["status"] == "pending"
    assert (await db_module.get_file_claims(db, "a.py"))["file_lock"] is None


@pytest.mark.asyncio
async def test_claim_parallel_batch_undeclared_singleton_allowed(db):
    """An undeclared item is fine as a batch OF ONE — nothing else in the
    batch exists for it to conflict with, so the singleton exception applies
    (mirrors get_parallelizable_groups' own undeclared-gets-its-own-group
    behavior)."""
    p = await db_module.create_project(db, "batch-undeclared-singleton")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    u = await db_module.add_sprint_item(db, pid, "v1", "solo undeclared")
    result = await db_module.claim_parallel_batch(db, pid, sess["id"], [u["id"]])
    assert result["ok"] is True
    assert result["claimed_item_ids"] == [u["id"]]
    assert (await db_module.get_sprint_item(db, u["id"]))["status"] == "in_progress"


@pytest.mark.asyncio
async def test_claim_parallel_batch_composition_conflict_rejected(db):
    """22cad9b8 — if the requested batch is NOT actually internally
    conflict-free (a stale/hand-assembled batch), the whole request is
    refused up front with a structured error naming the conflicting pair —
    no manifest is persisted, nothing is claimed."""
    p = await db_module.create_project(db, "batch-composition-conflict")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    a = await db_module.add_sprint_item(
        db, pid, "v1", "a", touches_resources=["file:a.py"], prospect_bypass=True,
    )
    c = await db_module.add_sprint_item(
        db, pid, "v1", "c", touches_resources=["file:a.py"], prospect_bypass=True,
        force=True,  # same-file declaration as 'a' -- title would also dup-guard
    )
    result = await db_module.claim_parallel_batch(db, pid, sess["id"], [a["id"], c["id"]])
    assert result["ok"] is False
    assert result["error"] == "BATCH_COMPOSITION_CONFLICT"
    assert len(result["conflicting_pairs"]) == 1
    pair = result["conflicting_pairs"][0]
    assert {pair["item_a"], pair["item_b"]} == {a["id"], c["id"]}

    assert (await db_module.get_sprint_item(db, a["id"]))["status"] == "pending"
    assert (await db_module.get_sprint_item(db, c["id"]))["status"] == "pending"
    # No manifest was ever persisted for this doomed-from-the-start batch.
    manifest = await db_module.get_batch_claim_manifest(
        db, pid, db_module.compute_batch_key([a["id"], c["id"]]),
    )
    assert manifest is None


@pytest.mark.asyncio
async def test_claim_parallel_batch_symbol_level_disjoint_succeeds(db):
    """22cad9b8/18c488b6 — two items claiming genuinely DISJOINT symbols in
    the SAME file must both succeed atomically as part of one batch: the
    atomic-batch mechanism must NOT regress fine-grained symbol-level
    claiming into a coarser whole-file-only lock."""
    p = await db_module.create_project(db, "batch-symbol-disjoint")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    item_foo = await db_module.add_sprint_item(
        db, pid, "v1", "touch foo", touches_resources=["symbol:pkg/mod.py::foo"],
        prospect_bypass=True,
    )
    item_bar = await db_module.add_sprint_item(
        db, pid, "v1", "touch bar", touches_resources=["symbol:pkg/mod.py::bar"],
        prospect_bypass=True, force=True,
    )
    result = await db_module.claim_parallel_batch(
        db, pid, sess["id"], [item_foo["id"], item_bar["id"]],
        resource_contents={"pkg/mod.py": _FOO_BAR_SRC},
    )
    assert result["ok"] is True
    assert set(result["claimed_item_ids"]) == {item_foo["id"], item_bar["id"]}

    live_symbols = await db_module.get_symbol_claims(db, "pkg/mod.py")
    names = {s["symbol_name"] for s in live_symbols}
    assert names == {"foo", "bar"}
    # Neither claim escalated to a whole-file lock.
    assert (await db_module.get_file_claims(db, "pkg/mod.py"))["file_lock"] is None


@pytest.mark.asyncio
async def test_claim_parallel_batch_file_vs_symbol_conflict_rejected(db):
    """22cad9b8/63b030a6 — a whole-file declaration and a symbol declaration
    on that same file conflict under the file⊃symbol hierarchy, so a batch
    combining them is rejected as an internal composition conflict."""
    p = await db_module.create_project(db, "batch-file-symbol-conflict")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    whole = await db_module.add_sprint_item(
        db, pid, "v1", "whole file", touches_resources=["file:pkg/mod.py"],
        prospect_bypass=True,
    )
    one_symbol = await db_module.add_sprint_item(
        db, pid, "v1", "one symbol", touches_resources=["symbol:pkg/mod.py::foo"],
        prospect_bypass=True, force=True,
    )
    result = await db_module.claim_parallel_batch(
        db, pid, sess["id"], [whole["id"], one_symbol["id"]],
    )
    assert result["ok"] is False
    assert result["error"] == "BATCH_COMPOSITION_CONFLICT"


@pytest.mark.asyncio
async def test_claim_parallel_batch_persists_immutable_manifest(db):
    """Acceptance #1 — a successful batch claim leaves a durable, auditable
    manifest recording exactly which items and which resources were in the
    batch."""
    p = await db_module.create_project(db, "batch-manifest-audit")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    a = await db_module.add_sprint_item(
        db, pid, "v1", "a", touches_resources=["file:a.py"], prospect_bypass=True,
    )
    b = await db_module.add_sprint_item(
        db, pid, "v1", "b", touches_resources=["symbol:b.py::foo"], prospect_bypass=True,
    )
    result = await db_module.claim_parallel_batch(
        db, pid, sess["id"], [a["id"], b["id"]],
        resource_contents={"b.py": _FOO_BAR_SRC},
    )
    assert result["ok"] is True
    batch_key = db_module.compute_batch_key([a["id"], b["id"]])
    manifest = await db_module.get_batch_claim_manifest(db, pid, batch_key)
    assert manifest is not None
    assert manifest["status"] == "claimed"
    assert manifest["session_id"] == sess["id"]
    assert set(manifest["item_ids"]) == {a["id"], b["id"]}
    assert set(manifest["resources"]) == {"file:a.py", "symbol:b.py::foo"}
    assert manifest["item_resource_map"][a["id"]] == ["file:a.py"]
    assert manifest["item_resource_map"][b["id"]] == ["symbol:b.py::foo"]
    assert manifest["superseded_at"] is None


@pytest.mark.asyncio
async def test_claim_parallel_batch_manifest_immutable_without_force(db):
    """Mirrors eb2e44f8's immutability contract: re-attempting the EXACT same
    item set while an active manifest already exists is refused unless
    force_manifest=True — never a silent overwrite."""
    p = await db_module.create_project(db, "batch-manifest-immutable")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    a = await db_module.add_sprint_item(
        db, pid, "v1", "a", touches_resources=["file:a.py"], prospect_bypass=True,
    )
    first = await db_module.claim_parallel_batch(db, pid, sess["id"], [a["id"]])
    assert first["ok"] is True

    # Directly exercise the low-level immutability guard (persist_batch_claim_manifest)
    # the same way worktree_manifest.persist_worktree_manifest is tested: a second
    # persist for the SAME (project_id, batch_key) without force=True raises.
    with pytest.raises(ValueError):
        await db_module.persist_batch_claim_manifest(
            db, pid, sess["id"], [a["id"]], {a["id"]: ["file:a.py"]}, ["file:a.py"],
        )

    # force=True explicitly supersedes instead of silently overwriting.
    superseded = await db_module.persist_batch_claim_manifest(
        db, pid, sess["id"], [a["id"]], {a["id"]: ["file:a.py"]}, ["file:a.py"],
        force=True, reason="test supersede",
    )
    assert superseded["status"] == "pending"
    history = await db_module.get_batch_claim_manifest_history(
        db, pid, db_module.compute_batch_key([a["id"]]),
    )
    assert len(history) == 2
    superseded_row = next(h for h in history if h["id"] != superseded["id"])
    assert superseded_row["superseded_at"] is not None
    assert superseded_row["superseded_reason"] == "test supersede"


@pytest.mark.asyncio
async def test_claim_parallel_batch_item_not_found(db):
    p = await db_module.create_project(db, "batch-item-not-found")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    result = await db_module.claim_parallel_batch(db, pid, sess["id"], ["nonexistent-id"])
    assert result["ok"] is False
    assert result["error"] == "ITEM_NOT_FOUND"


@pytest.mark.asyncio
async def test_claim_parallel_batch_requires_session_id_and_item_ids(db):
    p = await db_module.create_project(db, "batch-bad-args")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    with pytest.raises(ValueError):
        await db_module.claim_parallel_batch(db, pid, "", ["x"])
    with pytest.raises(ValueError):
        await db_module.claim_parallel_batch(db, pid, sess["id"], [])


@pytest.mark.asyncio
async def test_claim_parallel_batch_via_mcp_handler(db):
    """The MCP tool wrapper (claim_parallel_batch) routes through
    _dispatch_mcp_tool exactly like claim_sprint_item does."""
    import meridian.server as srv

    p = await db_module.create_project(db, "batch-mcp-handler")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    a = await db_module.add_sprint_item(
        db, pid, "v1", "a", touches_resources=["file:a.py"], prospect_bypass=True,
    )
    b = await db_module.add_sprint_item(
        db, pid, "v1", "b", touches_resources=["file:b.py"], prospect_bypass=True,
    )
    result = await srv._dispatch_mcp_tool(
        "claim_parallel_batch",
        {"project_id": pid, "session_id": sess["id"], "item_ids": [a["id"], b["id"]]},
        db, "/tmp",
    )
    assert result["ok"] is True
    assert set(result["claimed_item_ids"]) == {a["id"], b["id"]}


@pytest.mark.asyncio
async def test_claim_parallel_batch_end_to_end_from_parallelizable_groups(db):
    """Integration: a batch computed by get_parallelizable_groups is fed
    straight into claim_parallel_batch and claims cleanly end-to-end."""
    p = await db_module.create_project(db, "batch-e2e-groups")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    await db_module.add_sprint_item(
        db, pid, "v1", "a", touches_resources=["file:a.py"], prospect_bypass=True,
    )
    await db_module.add_sprint_item(
        db, pid, "v1", "b", touches_resources=["file:b.py"], prospect_bypass=True,
    )
    groups = await db_module.get_parallelizable_groups(db, pid, version="v1")
    assert groups["group_count"] == 1
    batch_item_ids = [it["id"] for it in groups["groups"][0]]

    result = await db_module.claim_parallel_batch(db, pid, sess["id"], batch_item_ids)
    assert result["ok"] is True
    assert set(result["claimed_item_ids"]) == set(batch_item_ids)

    # The batch no longer shows up as eligible (both items are in_progress now).
    groups_after = await db_module.get_parallelizable_groups(db, pid, version="v1")
    assert groups_after["eligible_count"] == 0


# ---------------------------------------------------------------------------
# 2a176d6d (finding 6) — claim_file / claim_symbol / release_symbol now all
# route through the same _normalize_file_path (previously several call sites
# used a bare .strip() directly instead) so the STRIP rule can never drift
# apart across functions. _normalize_file_path deliberately stays strip-only
# (no separator canonicalization) — see its docstring: this module
# coordinates OTHER live, concurrent sessions' locks, and matching how
# claim_file already stores existing rows matters more here than collapsing
# a Windows-style path onto a forward-slash one, which would need a real data
# migration to be safe. No dedicated test for the strip-only consolidation
# itself beyond the existing claim_file/claim_symbol/release_symbol coverage
# elsewhere in this file, which already exercises it implicitly.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 2a176d6d (finding 5) — claim_symbol rejects ambiguous symbol names instead
# of silently claiming the first match's line range.
# ---------------------------------------------------------------------------

_DUP_NAME_SRC = (
    "def helper():\n"
    "    return 1\n"
    "\n"
    "\n"
    "def helper():\n"
    "    return 2\n"
)


@pytest.mark.asyncio
async def test_claim_symbol_rejects_ambiguous_duplicate_top_level_name(db):
    """2a176d6d (finding 5) — two top-level function defs that share the exact
    name 'helper' (a redefinition — syntactically legal Python; ast.parse
    still produces two distinct FunctionDef nodes) must be refused as
    ambiguous rather than silently claiming symbols[0]'s line range."""
    p = await db_module.create_project(db, "2a176d6d-ambiguous-symbol")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "sess-a")
    result = await db_module.claim_symbol(
        db, sess["id"], "dup.py", "helper", _DUP_NAME_SRC
    )
    assert result["claimed"] is False
    assert result["reason"] == "ambiguous_symbol"
    assert len(result["matches"]) == 2
    # Nothing was actually claimed for the ambiguous name.
    live = await db_module.get_symbol_claims(db, "dup.py")
    assert live == []


@pytest.mark.asyncio
async def test_claim_symbol_unambiguous_name_still_claims_normally(db):
    """2a176d6d (finding 5) — a non-duplicate name in the SAME file with
    duplicates present must still claim normally (only the ambiguous name is
    refused, not the whole file)."""
    src = _DUP_NAME_SRC + "\ndef unique():\n    return 3\n"
    p = await db_module.create_project(db, "2a176d6d-unambiguous-symbol")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "sess-a")
    result = await db_module.claim_symbol(db, sess["id"], "dup2.py", "unique", src)
    assert result["claimed"] is True


# ---------------------------------------------------------------------------
# 2a176d6d — bonus root-cause fix (audit's literal example): a
# "file:<path>:<symbol>" declaration (single extra colon, the widely-used
# "preferred form" per the SYMBOL_SCOPE_HINT helper — NOT the "::" symbol:
# convention) must still be recognised as touching the SAME real file as
# another "file:<path>:<other_symbol>" declaration for CONFLICT purposes,
# even though the stored/serialized resource string is left untouched.
# ---------------------------------------------------------------------------


def test_resource_file_of_strips_colon_symbol_suffix():
    assert db_module._resource_file_of("file:x.py:funcA") == "x.py"
    assert db_module._resource_file_of("file:x.py:funcB") == "x.py"
    # Plain file: (no embedded colon) is unaffected.
    assert db_module._resource_file_of("file:x.py") == "x.py"
    # symbol: is unaffected (already handled via "::").
    assert db_module._resource_file_of("symbol:x.py::foo") == "x.py"
    # A genuine Windows drive-letter path is exempted, not mistaken for the
    # "file:<path>:<symbol>" pattern.
    assert db_module._resource_file_of("file:C:/repo/x.py") == "C:/repo/x.py"


def test_two_resources_conflict_file_colon_symbol_suffix_same_file():
    """The audit's literal example: two DIFFERENT 'file:<path>:<symbol>'
    declarations on the SAME real file must conflict."""
    c = db_module._two_resources_conflict
    assert c("file:x.py:funcA", "file:x.py:funcB") is True
    # Different real files still don't conflict even with the suffix present.
    assert c("file:x.py:funcA", "file:y.py:funcB") is False
    # The stored string is untouched by normalize_resource_id (still the
    # "preferred form" per the SYMBOL_SCOPE_HINT helper).
    assert db_module.normalize_resource_id("file:x.py:funcA") == "file:x.py:funcA"


@pytest.mark.asyncio
async def test_parallelizable_groups_splits_colon_symbol_suffixed_same_file_items(db):
    """22cad9b8/2a176d6d — the exact 2026-08-04 V026-batch6 audit scenario:
    two items each declaring 'file:<path>:<symbol>' on the SAME real file
    must be split into separate groups, not co-scheduled as if disjoint."""
    p = await db_module.create_project(db, "2a176d6d-colon-suffix-groups")
    pid = p["id"]
    await db_module.add_sprint_item(
        db, pid, "v1", "touch funcA",
        touches_resources=["file:sprint_items.py:funcA"],
    )
    await db_module.add_sprint_item(
        db, pid, "v1", "touch funcB",
        touches_resources=["file:sprint_items.py:funcB"], force=True,
    )
    res = await db_module.get_parallelizable_groups(db, pid, version="v1")
    assert res["group_count"] == 2


# ---------------------------------------------------------------------------
# 2a176d6d (finding 3 + 4) — bare symbol:<name> rejection and claim_granularity
# classification in the ATOMIC BATCH path (claim_parallel_batch /
# _claim_batch_resource), mirroring the single-item-path coverage above.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_parallel_batch_bare_symbol_resource_rejected(db):
    """2a176d6d (finding 3) — a bare symbol:<name> resource in a batch item
    acquires no lock; the batch must be refused (MALFORMED_RESOURCE), not
    treated as claimed."""
    p = await db_module.create_project(db, "2a176d6d-batch-bare-symbol")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    item = await db_module.add_sprint_item(
        db, pid, "v1", "bare symbol solo",
        touches_resources=["symbol:_helper_fn"], prospect_bypass=True,
    )
    result = await db_module.claim_parallel_batch(db, pid, sess["id"], [item["id"]])
    assert result["ok"] is False
    assert result["error"] == "MALFORMED_RESOURCE"
    assert (await db_module.get_sprint_item(db, item["id"]))["status"] == "pending"


@pytest.mark.asyncio
async def test_claim_parallel_batch_resource_claims_report_granularity(db):
    """2a176d6d (finding 4) — a successful batch claim's resource_claims must
    correctly distinguish a real symbol claim, a coarse fallback, and a
    genuine file: declaration."""
    p = await db_module.create_project(db, "2a176d6d-batch-granularity")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    item_symbol = await db_module.add_sprint_item(
        db, pid, "v1", "real symbol claim",
        touches_resources=["symbol:pkg/mod.py::foo"], prospect_bypass=True,
    )
    item_file = await db_module.add_sprint_item(
        db, pid, "v1", "real file claim",
        touches_resources=["file:other.py"], prospect_bypass=True, force=True,
    )
    result = await db_module.claim_parallel_batch(
        db, pid, sess["id"], [item_symbol["id"], item_file["id"]],
        resource_contents={"pkg/mod.py": _FOO_BAR_SRC},
    )
    assert result["ok"] is True
    by_resource = {c["resource"]: c for c in result["resource_claims"]}
    assert by_resource["symbol:pkg/mod.py::foo"]["claim_granularity"] == "symbol"
    assert by_resource["file:other.py"]["claim_granularity"] == "file"


@pytest.mark.asyncio
async def test_claim_parallel_batch_resource_claims_coarse_fallback(db):
    """2a176d6d (finding 4) — a symbol: resource in a batch with NO
    resource_contents supplied for it must be reported claim_granularity
    'coarse', not 'symbol'."""
    p = await db_module.create_project(db, "2a176d6d-batch-granularity-coarse")
    pid = p["id"]
    sess = await db_module.register_session(db, pid, "orchestrator")
    item = await db_module.add_sprint_item(
        db, pid, "v1", "symbol no source",
        touches_resources=["symbol:pkg/mod.py::foo"], prospect_bypass=True,
    )
    result = await db_module.claim_parallel_batch(db, pid, sess["id"], [item["id"]])
    assert result["ok"] is True
    assert result["resource_claims"][0]["claim_granularity"] == "coarse"
    assert result["resource_claims"][0]["fallback_reason"] == "no_source_supplied"


# ---------------------------------------------------------------------------
# 2a176d6d — additive scheduling-granularity redesign piece: STATIC
# (planning-time) predicted_granularity on each get_parallelizable_groups
# item, purely a resource-id-shape classification. Does not change the
# coloring/grouping algorithm at all.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallelizable_groups_predicted_granularity_shapes(db):
    p = await db_module.create_project(db, "2a176d6d-predicted-granularity")
    pid = p["id"]
    await db_module.add_sprint_item(
        db, pid, "v1", "mixed resources",
        touches_resources=["file:a.py", "symbol:b.py::foo", "db:migrations"],
    )
    res = await db_module.get_parallelizable_groups(db, pid, version="v1")
    item = res["groups"][0][0]
    pg = item["predicted_granularity"]
    assert pg["file:a.py"] == "file"
    assert pg["symbol:b.py::foo"] == "symbol"
    assert pg["db:migrations"] == "other"


@pytest.mark.asyncio
async def test_parallelizable_groups_predicted_granularity_flags_malformed_symbol(db):
    """2a176d6d — a bare symbol:<name> (no '::' file scope) is flagged
    'malformed_symbol' at PLANNING time, before any worker ever attempts to
    claim it, so an orchestrator can catch it before launch."""
    p = await db_module.create_project(db, "2a176d6d-predicted-malformed")
    pid = p["id"]
    await db_module.add_sprint_item(
        db, pid, "v1", "bare symbol declared",
        touches_resources=["symbol:_helper_fn"],
    )
    res = await db_module.get_parallelizable_groups(db, pid, version="v1")
    item = res["groups"][0][0]
    assert item["predicted_granularity"]["symbol:_helper_fn"] == "malformed_symbol"
