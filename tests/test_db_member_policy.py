"""Storage layer of the configurable project-scoped member policy (lane L0).

Covers the ``member_policy`` auth-DB table and its db functions, the
role/family/subproject-map readers, the member-removal and re-invite hygiene,
the compare-and-set guards in ``create_project`` / ``set_parent_project`` /
``delete_project``, the export and tenant-record coverage of the new table, and
PERM_MANAGE_POLICY.

Every DB test uses ``anydb`` so it runs on SQLite locally and on Postgres in the
CI ``test-postgres`` job (the Postgres variant skips itself without
TEST_DATABASE_URL). Nothing here reads a row with the SQLite-only API.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid

import pytest

from meridian import db as db_module
from meridian import roles
from meridian.db import workspace as ws_module


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

async def _tenant(db, email="owner@example.com") -> str:
    return (await db_module.upsert_tenant(db, email))["id"]


async def _member(db, tid, email, role="member", project_id=None, accepted=True):
    """Invite ``email`` (one workspace_members row) and optionally accept it."""
    row = await db_module.create_workspace_invite(
        db, tid, email, role, f"tok-{uuid.uuid4().hex}", project_id=project_id,
    )
    if accepted:
        row = await db_module.accept_workspace_invite(db, row["id"])
    return row


async def _project(db, name, parent=None) -> str:
    return (await db_module.create_project(db, name, parent_project_id=parent))["id"]


async def _raw_set_parent(db, project_id, parent_id):
    """Write parent_project_id with no validation (builds states the API refuses)."""
    await db.execute(
        "UPDATE projects SET parent_project_id = ? WHERE id = ?", (parent_id, project_id)
    )
    await db.commit()


# ---------------------------------------------------------------------------
# table + default/override round trip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_default_and_override_round_trip(anydb):
    db = anydb
    tid = await _tenant(db)
    other = await _tenant(db, "other-owner@example.com")

    assert await db_module.get_member_policy_rows(db, tid, "Ann@Example.com") == {
        "default": None, "override": None,
    }

    res = await db_module.set_member_policy_default(
        db, tid, json.dumps({"delete_subproject": True}), "owner@example.com",
    )
    assert res["subject"] == "*" and res["previous"] is None
    assert json.loads(res["policy"]) == {"delete_subproject": True}

    # a dict is accepted too, and the stored text is canonical
    res = await db_module.set_member_policy_override(
        db, tid, "Ann@Example.com", {"write_project": False, "a": None}, "owner@example.com",
    )
    assert res["subject"] == "ann@example.com"
    assert res["policy"] == '{"a":null,"write_project":false}'

    rows = await db_module.get_member_policy_rows(db, tid, "ANN@example.COM")
    assert json.loads(rows["default"]) == {"delete_subproject": True}
    assert json.loads(rows["override"]) == {"a": None, "write_project": False}

    # someone else in the same workspace sees the default but not Ann's override
    bob = await db_module.get_member_policy_rows(db, tid, "bob@example.com")
    assert bob["override"] is None and bob["default"] == rows["default"]
    # and another workspace sees neither
    assert await db_module.get_member_policy_rows(db, other, "ann@example.com") == {
        "default": None, "override": None,
    }

    # replacing keeps ONE row per (tenant, subject) and reports what it replaced
    res = await db_module.set_member_policy_override(
        db, tid, "ann@example.com", {"delete_project": True}, "owner@example.com",
    )
    assert json.loads(res["previous"]) == {"a": None, "write_project": False}
    assert await db_module.list_member_policy_overrides(db, tid) == {
        "ann@example.com": '{"delete_project":true}',
    }
    async with db.execute(
        "SELECT COUNT(*) AS cnt FROM member_policy WHERE tenant_id = ?", (tid,)
    ) as cur:
        assert (await cur.fetchone())["cnt"] == 2  # default + Ann

    # an empty email asks for the default only
    only_default = await db_module.get_member_policy_rows(db, tid, "")
    assert only_default["override"] is None and only_default["default"] is not None


@pytest.mark.asyncio
async def test_clear_override(anydb):
    db = anydb
    tid = await _tenant(db)
    await db_module.set_member_policy_override(db, tid, "ann@example.com", {"x": True}, "o")
    res = await db_module.clear_member_policy_override(
        db, tid, "ANN@example.com", updated_by="owner@example.com",
    )
    assert res["removed"] is True and res["previous"] == '{"x":true}'
    assert (await db_module.get_member_policy_rows(db, tid, "ann@example.com"))["override"] is None
    # clearing again is a no-op and writes no audit row
    again = await db_module.clear_member_policy_override(db, tid, "ann@example.com")
    assert again == {"removed": False, "previous": None, "audited": False}


@pytest.mark.asyncio
async def test_setters_reject_non_objects_and_bad_subjects(anydb):
    db = anydb
    tid = await _tenant(db)
    for bad in ("not json", "[1, 2]", "null", "true", "7", b"\xff", [1], None):
        with pytest.raises(ValueError):
            await db_module.set_member_policy_default(db, tid, bad, "o")
    for email in ("", "   ", "*", None):
        with pytest.raises(ValueError):
            await db_module.set_member_policy_override(db, tid, email, {}, "o")
        with pytest.raises(ValueError):
            await db_module.clear_member_policy_override(db, tid, email)
    assert await db_module.list_member_policy_overrides(db, tid) == {}
    assert (await db_module.get_member_policy_rows(db, tid, "x@y.z"))["default"] is None


@pytest.mark.asyncio
async def test_unparseable_stored_json_is_returned_verbatim(anydb):
    """Storage does not interpret the text: the resolver decides that a corrupt
    row denies everything. A row that is not valid JSON must come back exactly."""
    db = anydb
    tid = await _tenant(db)
    for subject, text in (("*", "{broken"), ("ann@example.com", "[1,2,3]"), ("bob@example.com", "")):
        await db.execute(
            "INSERT INTO member_policy (tenant_id, subject, policy, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (tid, subject, text, "2026-01-01T00:00:00+00:00"),
        )
    await db.commit()
    rows = await db_module.get_member_policy_rows(db, tid, "ann@example.com")
    assert rows == {"default": "{broken", "override": "[1,2,3]"}
    assert await db_module.list_member_policy_overrides(db, tid) == {
        "ann@example.com": "[1,2,3]", "bob@example.com": "",
    }


@pytest.mark.asyncio
async def test_partial_policy_patches_reject_corrupt_rows(anydb):
    db = anydb
    tid = await _tenant(db)
    await db.execute(
        "INSERT INTO member_policy (tenant_id, subject, policy, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (tid, "*", "{broken", "2026-01-01T00:00:00+00:00"),
    )
    await db.execute(
        "INSERT INTO member_policy (tenant_id, subject, policy, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (tid, "ann@example.com", "[false]", "2026-01-01T00:00:00+00:00"),
    )
    await db.commit()

    with pytest.raises(ws_module.CorruptMemberPolicyRow):
        await ws_module.patch_member_policy_default(
            db, tid, {"write_project": False}, "owner@example.com",
        )
    with pytest.raises(ws_module.CorruptMemberPolicyRow):
        await ws_module.patch_member_policy_override(
            db, tid, "ann@example.com", {"write_project": False}, "owner@example.com",
        )
    rows = await db_module.get_member_policy_rows(db, tid, "ann@example.com")
    assert rows == {"default": "{broken", "override": "[false]"}


@pytest.mark.asyncio
async def test_concurrent_partial_policy_patches_keep_both_changes(anydb):
    db = anydb
    tid = await _tenant(db)
    await asyncio.gather(
        ws_module.patch_member_policy_default(
            db, tid, {"write_project": False}, "owner@example.com",
        ),
        ws_module.patch_member_policy_default(
            db, tid, {"delete_project": True}, "owner@example.com",
        ),
    )
    await asyncio.gather(
        ws_module.patch_member_policy_override(
            db, tid, "ann@example.com", {"write_project": False}, "owner@example.com",
        ),
        ws_module.patch_member_policy_override(
            db, tid, "ann@example.com", {"delete_project": True}, "owner@example.com",
        ),
    )
    rows = await db_module.get_member_policy_rows(db, tid, "ann@example.com")
    assert json.loads(rows["default"]) == {
        "delete_project": True, "write_project": False,
    }
    assert json.loads(rows["override"]) == {
        "delete_project": True, "write_project": False,
    }


class _CountingConn:
    """Forwards to the real connection and remembers every statement."""

    def __init__(self, inner):
        self._inner = inner
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append(sql)
        return self._inner.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@pytest.mark.asyncio
async def test_policy_rows_and_roles_are_one_query_each(anydb):
    db = anydb
    tid = await _tenant(db)
    await db_module.set_member_policy_default(db, tid, {"write_project": True}, "o")
    counting = _CountingConn(db)
    await db_module.get_member_policy_rows(counting, tid, "ann@example.com")
    assert len(counting.statements) == 1
    counting.statements.clear()
    await db_module.get_member_roles(counting, tid, "ann@example.com")
    assert len(counting.statements) == 1


@pytest.mark.asyncio
async def test_member_policy_summary_context_is_one_query_and_role_complete(anydb):
    db = anydb
    tid = await _tenant(db)
    p1 = await _project(db, "policy-summary-p1")
    p2 = await _project(db, "policy-summary-p2")
    ann1 = await _member(db, tid, "ann@example.com", "admin", p1)
    ann2 = await _member(db, tid, "Ann@Example.com", "viewer", p2)
    bob = await _member(db, tid, "bob@example.com", "member", p1)
    await db_module.set_member_policy_override(
        db, tid, "ann@example.com", {"write_project": True}, "owner@example.com",
    )

    counting = _CountingConn(db)
    rows = await ws_module.list_member_policy_members(counting, tid)
    assert len(counting.statements) == 1
    member_rows = [row for row in rows if row["row_kind"] == "member"]
    role_rows = [row for row in rows if row["row_kind"] == "role"]
    assert {row["member_id"] for row in member_rows} == {
        ann1["id"], ann2["id"], bob["id"],
    }
    assert {row["email"].casefold() for row in member_rows} == {
        "ann@example.com", "bob@example.com",
    }
    assert {row["policy_role"] for row in role_rows} == {"admin", "viewer", "member"}
    ann_override_rows = [row for row in member_rows if row["email"] == "ann@example.com"]
    assert all(row["override"] == '{"write_project":true}' for row in ann_override_rows)


@pytest.mark.asyncio
async def test_bytes_json_is_accepted_and_a_corrupt_previous_row_is_audited_verbatim(anydb):
    db = anydb
    tid = await _tenant(db)
    await db.execute(
        "INSERT INTO member_policy (tenant_id, subject, policy, updated_at) VALUES (?, ?, ?, ?)",
        (tid, "*", "{broken", "2026-01-01T00:00:00+00:00"),
    )
    await db.commit()
    res = await db_module.set_member_policy_default(db, tid, b'{"write_project": false}', "o")
    assert res["previous"] == "{broken" and res["policy"] == '{"write_project":false}'
    assert res["audited"] is True
    (event,) = await db_module.get_action_audit_log(
        db, tenant_id=tid, event_type="member_policy_changed",
    )
    assert json.loads(event["detail"])["before"] == "{broken"


@pytest.mark.asyncio
async def test_read_errors_propagate():
    """No silent fall-back to 'no policy': an error from the table reaches the caller."""
    class _Boom:
        def execute(self, *_a, **_k):
            raise RuntimeError("auth db unavailable")

    with pytest.raises(RuntimeError):
        await db_module.get_member_policy_rows(_Boom(), "t", "ann@example.com")
    with pytest.raises(RuntimeError):
        await db_module.get_member_roles(_Boom(), "t", "ann@example.com")
    with pytest.raises(RuntimeError):
        await db_module.list_member_policy_overrides(_Boom(), "t")


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_policy_writes_append_an_audit_event(anydb):
    db = anydb
    tid = await _tenant(db)
    r1 = await db_module.set_member_policy_default(db, tid, {"delete_subproject": True}, "owner@example.com")
    r2 = await db_module.set_member_policy_override(db, tid, "ann@example.com", {"write_project": False}, "owner@example.com")
    r3 = await db_module.clear_member_policy_override(db, tid, "ann@example.com", updated_by="owner@example.com")
    assert r1["audited"] and r2["audited"] and r3["audited"]

    events = await db_module.get_action_audit_log(
        db, tenant_id=tid, event_type="member_policy_changed",
    )
    assert [e["actor"] for e in events] == ["owner@example.com"] * 3
    seen = {
        (d["subject"], json.dumps(d["before"], sort_keys=True), json.dumps(d["after"], sort_keys=True))
        for d in (json.loads(e["detail"]) for e in events)
    }
    assert seen == {
        ("*", "null", '{"delete_subproject": true}'),
        ("ann@example.com", "null", '{"write_project": false}'),
        ("ann@example.com", '{"write_project": false}', "null"),
    }


@pytest.mark.asyncio
async def test_audit_failure_is_reported_and_never_rolls_the_edit_back(anydb, monkeypatch, caplog):
    db = anydb
    tid = await _tenant(db)

    async def _boom(*_a, **_k):
        raise RuntimeError("audit store down")

    monkeypatch.setattr(ws_module, "record_action_audit_event", _boom)
    with caplog.at_level(logging.ERROR, logger=ws_module.__name__):
        res = await db_module.set_member_policy_default(db, tid, {"write_project": False}, "o")
    assert res["audited"] is False
    assert any("member_policy_changed audit write failed" in r.getMessage() for r in caplog.records)
    # the edit itself stuck
    assert (await db_module.get_member_policy_rows(db, tid, ""))["default"] == '{"write_project":false}'


# ---------------------------------------------------------------------------
# get_member_roles
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_member_roles_returns_accepted_rows_only(anydb):
    db = anydb
    tid = await _tenant(db)
    other = await _tenant(db, "other-owner@example.com")
    p1 = await _project(db, "roles-p1")
    p2 = await _project(db, "roles-p2")
    await _member(db, tid, "Ann@Example.com", "admin", p1)
    await _member(db, tid, "ann@example.com", "viewer", p2)
    await _member(db, tid, "ann@example.com", "member", None, accepted=False)  # pending: left out
    await _member(db, other, "ann@example.com", "owner", p1)  # another workspace: left out

    got = await db_module.get_member_roles(db, tid, "ANN@example.com")
    assert sorted(got, key=lambda t: t[0]) == [("admin", p1), ("viewer", p2)]
    assert all(isinstance(t, tuple) for t in got)

    # a workspace-wide row reports project_id None
    await _member(db, tid, "wide@example.com", "member", None)
    assert await db_module.get_member_roles(db, tid, "wide@example.com") == [("member", None)]

    # unknown, empty, the owner (no workspace_members row) and a pending-only person
    assert await db_module.get_member_roles(db, tid, "nobody@example.com") == []
    assert await db_module.get_member_roles(db, tid, "") == []
    assert await db_module.get_member_roles(db, tid, "owner@example.com") == []
    await _member(db, tid, "pending@example.com", "member", p1, accepted=False)
    assert await db_module.get_member_roles(db, tid, "pending@example.com") == []


# ---------------------------------------------------------------------------
# hygiene: removing / re-inviting a member
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_workspace_member_removes_override_only_with_the_last_row(anydb):
    db = anydb
    tid = await _tenant(db)
    p1 = await _project(db, "hyg-p1")
    p2 = await _project(db, "hyg-p2")
    row1 = await _member(db, tid, "ann@example.com", "member", p1)
    row2 = await _member(db, tid, "Ann@example.com", "member", p2)
    await db_module.set_member_policy_default(db, tid, {"delete_project": True}, "o")
    await db_module.set_member_policy_override(db, tid, "ann@example.com", {"delete_project": False}, "o")
    await db_module.set_member_policy_override(db, tid, "bob@example.com", {"write_project": False}, "o")

    # one of two rows goes: Ann is still a member, her override stays
    await db_module.delete_workspace_member(db, row1["id"], tid)
    assert (await db_module.get_member_policy_rows(db, tid, "ann@example.com"))["override"] is not None

    # the last row goes: the override goes with it, nobody else's does, the default stays
    await db_module.delete_workspace_member(db, row2["id"], tid)
    rows = await db_module.get_member_policy_rows(db, tid, "ann@example.com")
    assert rows["override"] is None and rows["default"] is not None
    assert list((await db_module.list_member_policy_overrides(db, tid))) == ["bob@example.com"]


@pytest.mark.asyncio
async def test_delete_workspace_member_keeps_override_while_a_pending_row_remains(anydb):
    db = anydb
    tid = await _tenant(db)
    p1 = await _project(db, "hyg-pend-p1")
    accepted = await _member(db, tid, "ann@example.com", "member", p1)
    await _member(db, tid, "ann@example.com", "member", None, accepted=False)
    await db_module.set_member_policy_override(db, tid, "ann@example.com", {"write_project": False}, "o")
    await db_module.delete_workspace_member(db, accepted["id"], tid)
    assert (await db_module.get_member_policy_rows(db, tid, "ann@example.com"))["override"] is not None


@pytest.mark.asyncio
async def test_delete_workspace_member_for_unknown_id_is_a_noop(anydb):
    db = anydb
    tid = await _tenant(db)
    await db_module.set_member_policy_override(db, tid, "ann@example.com", {"x": True}, "o")
    assert await db_module.delete_workspace_member(db, "no-such-id", tid) is True
    assert (await db_module.get_member_policy_rows(db, tid, "ann@example.com"))["override"] is not None


@pytest.mark.asyncio
async def test_delete_workspace_member_ignores_another_tenants_row(anydb):
    db = anydb
    tid = await _tenant(db)
    other = await _tenant(db, "other-owner@example.com")
    p1 = await _project(db, "hyg-x-p1")
    row = await _member(db, other, "ann@example.com", "member", p1)
    await db_module.set_member_policy_override(db, tid, "ann@example.com", {"x": True}, "o")
    await db_module.set_member_policy_override(db, other, "ann@example.com", {"x": True}, "o")
    # wrong tenant id: nothing is deleted and neither override is touched
    await db_module.delete_workspace_member(db, row["id"], tid)
    assert (await db_module.get_member_policy_rows(db, tid, "ann@example.com"))["override"] is not None
    assert (await db_module.get_member_policy_rows(db, other, "ann@example.com"))["override"] is not None


@pytest.mark.asyncio
async def test_create_invite_clears_a_stale_override_but_not_a_live_one(anydb):
    db = anydb
    tid = await _tenant(db)
    p1 = await _project(db, "inv-p1")
    p2 = await _project(db, "inv-p2")

    # stale: an override with no workspace_members row behind it
    await db_module.set_member_policy_override(db, tid, "ann@example.com", {"delete_project": True}, "o")
    await _member(db, tid, "Ann@Example.com", "member", p1, accepted=False)
    assert (await db_module.get_member_policy_rows(db, tid, "ann@example.com"))["override"] is None

    # live: Ann has a row now, so a second invite (another project) keeps what was set
    await db_module.set_member_policy_override(db, tid, "ann@example.com", {"delete_project": True}, "o")
    await _member(db, tid, "ann@example.com", "member", p2, accepted=False)
    assert (await db_module.get_member_policy_rows(db, tid, "ann@example.com"))["override"] is not None

    # a different tenant's invite for the same email does not touch this workspace
    other = await _tenant(db, "other-owner@example.com")
    await _member(db, other, "ann@example.com", "member", p1, accepted=False)
    assert (await db_module.get_member_policy_rows(db, tid, "ann@example.com"))["override"] is not None


@pytest.mark.asyncio
async def test_remove_then_reinvite_does_not_resurrect_rights(anydb):
    db = anydb
    tid = await _tenant(db)
    p1 = await _project(db, "rein-p1")
    row = await _member(db, tid, "ann@example.com", "member", p1)
    await db_module.set_member_policy_override(
        db, tid, "ann@example.com", {"delete_project": True, "delete_subproject": True}, "o",
    )
    await db_module.delete_workspace_member(db, row["id"], tid)
    await _member(db, tid, "ann@example.com", "member", p1)
    rows = await db_module.get_member_policy_rows(db, tid, "ann@example.com")
    assert rows["override"] is None
    assert await db_module.list_member_policy_overrides(db, tid) == {}


# ---------------------------------------------------------------------------
# get_project_family
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_project_family(anydb):
    db = anydb
    top = await _project(db, "fam-top")
    kid_a = await _project(db, "fam-kid-a", parent=top)
    kid_b = await _project(db, "fam-kid-b", parent=top)
    lone = await _project(db, "fam-lone")
    gone_parent = await _project(db, "fam-gone-parent")
    orphan = await _project(db, "fam-orphan", parent=gone_parent)
    await db_module.delete_project(db, gone_parent)  # owner path: leaves the child behind

    fam = await db_module.get_project_family(db, [top, kid_a, lone, orphan, "unknown-id", "", top])
    assert set(fam) == {top, kid_a, lone, orphan}  # unknown / empty ids are absent, repeats collapse

    assert fam[top]["name"] == "fam-top"
    assert not fam[top]["parent_project_id"] and fam[top]["parent_exists"] is False
    assert sorted(fam[top]["child_ids"]) == sorted([kid_a, kid_b])
    assert fam[top]["status"] == "active"

    assert fam[kid_a]["parent_project_id"] == top and fam[kid_a]["parent_exists"] is True
    assert fam[kid_a]["child_ids"] == []

    assert fam[lone]["child_ids"] == [] and fam[lone]["parent_exists"] is False

    assert fam[orphan]["parent_project_id"] == gone_parent
    assert fam[orphan]["parent_exists"] is False  # dangling parent

    assert await db_module.get_project_family(db, []) == {}


@pytest.mark.asyncio
async def test_get_project_family_is_chunked(anydb, monkeypatch):
    db = anydb
    top = await _project(db, "famc-top")
    kids = [await _project(db, f"famc-kid-{i}", parent=top) for i in range(5)]
    solo = await _project(db, "famc-solo")

    # a request far past the real chunk size goes through without a bind-variable error
    many = [f"missing-{i}" for i in range(1500)] + [top, solo]
    fam = await db_module.get_project_family(db, many)
    assert set(fam) == {top, solo}
    assert sorted(fam[top]["child_ids"]) == sorted(kids)

    # tiny chunks: every id, and every child list, is stitched back together
    monkeypatch.setattr(db_module, "_PROJECT_ID_CHUNK", 2)
    fam = await db_module.get_project_family(db, [top, solo, *kids])
    assert set(fam) == {top, solo, *kids}
    assert sorted(fam[top]["child_ids"]) == sorted(kids)


# ---------------------------------------------------------------------------
# get_subproject_map
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_subproject_map_one_level_under_top_level_parents(anydb):
    db = anydb
    top = await _project(db, "map-top")
    kid_a = await _project(db, "map-kid-a", parent=top)
    kid_b = await _project(db, "map-kid-b", parent=top)
    other_top = await _project(db, "map-other-top")
    other_kid = await _project(db, "map-other-kid", parent=other_top)

    assert await db_module.get_subproject_map(db, [top]) == {kid_a: top, kid_b: top}
    assert await db_module.get_subproject_map(db, [top, other_top, top]) == {
        kid_a: top, kid_b: top, other_kid: other_top,
    }
    assert await db_module.get_subproject_map(db, [kid_a]) == {}  # a leaf has none
    assert await db_module.get_subproject_map(db, []) == {}
    assert await db_module.get_subproject_map(db, ["unknown-id"]) == {}


@pytest.mark.asyncio
async def test_get_subproject_map_excludes_depth_two_chains(anydb):
    db = anydb
    top = await _project(db, "d2-top")
    mid = await _project(db, "d2-mid", parent=top)
    leaf = await _project(db, "d2-leaf")
    await _raw_set_parent(db, leaf, mid)  # the two-level chain the API refuses to build

    assert await db_module.get_subproject_map(db, [top]) == {mid: top}  # leaf is not reachable via top
    assert await db_module.get_subproject_map(db, [mid]) == {}  # mid is not a top-level parent


@pytest.mark.asyncio
async def test_get_subproject_map_excludes_children_of_a_deleted_parent(anydb):
    db = anydb
    top = await _project(db, "del-top")
    kid = await _project(db, "del-kid", parent=top)
    assert await db_module.get_subproject_map(db, [top]) == {kid: top}
    await db_module.delete_project(db, top)  # owner path does not touch children
    assert await db_module.get_subproject_map(db, [top]) == {}
    assert (await db_module.get_project(db, kid))["parent_project_id"] == top  # still dangling


@pytest.mark.asyncio
async def test_get_subproject_map_follows_a_reparent(anydb):
    db = anydb
    a = await _project(db, "rp-a")
    b = await _project(db, "rp-b")
    kid = await _project(db, "rp-kid", parent=a)
    await db_module.set_parent_project(db, kid, b)
    assert await db_module.get_subproject_map(db, [a]) == {}
    assert await db_module.get_subproject_map(db, [b]) == {kid: b}
    await db_module.set_parent_project(db, kid, None)
    assert await db_module.get_subproject_map(db, [a, b]) == {}


@pytest.mark.asyncio
async def test_get_subproject_map_is_chunked(anydb, monkeypatch):
    db = anydb
    parents = [await _project(db, f"mapc-p{i}") for i in range(5)]
    kids = {await _project(db, f"mapc-k{i}", parent=p): p for i, p in enumerate(parents)}

    many = [f"missing-{i}" for i in range(1500)] + parents
    assert await db_module.get_subproject_map(db, many) == kids

    monkeypatch.setattr(db_module, "_PROJECT_ID_CHUNK", 2)
    assert await db_module.get_subproject_map(db, parents) == kids


# ---------------------------------------------------------------------------
# compare-and-set guards are covered in tests/test_project_parent.py (parent) and
# below (delete)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_project_expect_class_mismatch_refuses_before_deleting(anydb):
    db = anydb
    top = await _project(db, "exp-top")
    other = await _project(db, "exp-other")
    await db_module.add_project_note(db, top, "keep me", "body")

    # the caller classified `top` as top-level, but it was nested in the meantime
    await db_module.set_parent_project(db, top, other)
    with pytest.raises(ValueError, match="changed concurrently"):
        await db_module.delete_project(db, top, expect="top")
    assert await db_module.get_project(db, top) is not None
    async with db.execute("SELECT COUNT(*) AS cnt FROM project_notes WHERE project_id = ?", (top,)) as cur:
        assert (await cur.fetchone())["cnt"] == 1  # the project's data is untouched

    # the matching class goes through
    await db_module.delete_project(db, top, expect="subproject")
    assert await db_module.get_project(db, top) is None
    assert await db_module.get_project(db, other) is not None


@pytest.mark.asyncio
async def test_delete_project_expect_dict_is_all_or_nothing(anydb):
    db = anydb
    p = await _project(db, "expd-p")
    q = await _project(db, "expd-q")
    kid = await _project(db, "expd-kid", parent=p)

    # q is top-level, but the caller said subproject: nothing in the batch is deleted
    with pytest.raises(ValueError, match="changed concurrently"):
        await db_module.delete_project(db, [kid, q], expect={kid: "subproject", q: "subproject"})
    assert await db_module.get_project(db, kid) is not None
    assert await db_module.get_project(db, q) is not None

    await db_module.delete_project(db, [kid, q], expect={kid: "subproject", q: "top"})
    assert await db_module.get_project(db, kid) is None
    assert await db_module.get_project(db, q) is None
    assert await db_module.get_project(db, p) is not None


@pytest.mark.asyncio
async def test_delete_project_expect_orphan_class(anydb):
    db = anydb
    gone = await _project(db, "expo-gone")
    orphan = await _project(db, "expo-orphan", parent=gone)
    await db_module.delete_project(db, gone)

    with pytest.raises(ValueError, match="changed concurrently"):
        await db_module.delete_project(db, orphan, expect="subproject")
    await db_module.delete_project(db, orphan, expect="orphan")
    assert await db_module.get_project(db, orphan) is None


@pytest.mark.asyncio
async def test_delete_project_expect_refuses_to_leave_subprojects_behind(anydb):
    db = anydb
    p = await _project(db, "expl-p")
    kid = await _project(db, "expl-kid", parent=p)
    with pytest.raises(ValueError, match="subproject"):
        await db_module.delete_project(db, p, expect="top")
    assert await db_module.get_project(db, p) is not None
    assert await db_module.get_project(db, kid) is not None
    # taking the child along in the same call is fine
    await db_module.delete_project(db, [p, kid], expect={p: "top", kid: "subproject"})
    assert await db_module.get_project(db, p) is None and await db_module.get_project(db, kid) is None


@pytest.mark.asyncio
async def test_delete_project_without_expect_is_unchanged(anydb):
    """The owner path: no class check, and a parent's children are left alone."""
    db = anydb
    p = await _project(db, "expu-p")
    kid = await _project(db, "expu-kid", parent=p)
    await db_module.delete_project(db, p)
    assert await db_module.get_project(db, p) is None
    assert await db_module.get_project(db, kid) is not None


@pytest.mark.asyncio
async def test_delete_project_expect_rejects_an_unknown_class(anydb):
    db = anydb
    p = await _project(db, "expx-p")
    with pytest.raises(ValueError, match="unknown project class"):
        await db_module.delete_project(db, p, expect="grandchild")
    assert await db_module.get_project(db, p) is not None


@pytest.mark.asyncio
async def test_delete_project_expect_on_an_already_deleted_id_is_a_noop(anydb):
    db = anydb
    p = await _project(db, "expg-p")
    await db_module.delete_project(db, p)
    await db_module.delete_project(db, p, expect="top")  # nothing to classify, nothing to delete


# ---------------------------------------------------------------------------
# export / tenant records
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_export_tenant_data_includes_member_policy_unless_project_restricted(anydb):
    db = anydb
    tid = await _tenant(db)
    a = await _project(db, "exp-a")
    await db_module.set_member_policy_default(db, tid, {"write_project": True}, "owner@example.com")
    await db_module.set_member_policy_override(db, tid, "ann@example.com", {"delete_project": True}, "owner@example.com")

    data = await db_module.export_tenant_data(db, tid)
    by_subject = {r["subject"]: r for r in data["member_policy"]}
    assert set(by_subject) == {"*", "ann@example.com"}
    assert json.loads(by_subject["ann@example.com"]["policy"]) == {"delete_project": True}
    assert by_subject["ann@example.com"]["updated_by"] == "owner@example.com"

    # a project-scoped member downloading their data must not get the others' rights
    restricted = await db_module.export_tenant_data(db, tid, project_ids=[a])
    assert restricted["member_policy"] == []
    assert "ann@example.com" not in json.dumps(restricted, default=str)


@pytest.mark.asyncio
async def test_delete_tenant_records_removes_member_policy(anydb):
    db = anydb
    tid = await _tenant(db)
    other = await _tenant(db, "other-owner@example.com")
    await db_module.set_member_policy_default(db, tid, {"write_project": True}, "o")
    await db_module.set_member_policy_override(db, tid, "ann@example.com", {"x": True}, "o")
    await db_module.set_member_policy_default(db, other, {"write_project": False}, "o")

    await db_module.delete_tenant_records(db, tid)
    assert await db_module.get_member_policy_rows(db, tid, "ann@example.com") == {
        "default": None, "override": None,
    }
    assert (await db_module.get_member_policy_rows(db, other, "ann@example.com"))["default"] is not None


# ---------------------------------------------------------------------------
# schema literals + re-exports + roles (no DB)
# ---------------------------------------------------------------------------

def test_member_policy_table_is_in_both_base_literals_with_the_pk_inline():
    from meridian import pg_adapter

    for name, literal in (
        ("db/__init__ CREATE_TABLES", db_module.CREATE_TABLES),
        ("pg_adapter CREATE_TABLES_HOSTED", pg_adapter.CREATE_TABLES_HOSTED),
    ):
        m = re.search(
            r"CREATE TABLE IF NOT EXISTS member_policy \((.*?)\n\);", literal, re.DOTALL,
        )
        assert m, f"member_policy missing from {name}"
        body = m.group(1)
        assert "PRIMARY KEY (tenant_id, subject)" in body, name  # inline, not a later ALTER
        for col in ("tenant_id TEXT NOT NULL", "subject TEXT NOT NULL", "policy TEXT NOT NULL",
                    "updated_by TEXT", "updated_at TEXT NOT NULL"):
            assert col in body, f"{name}: {col}"
    # the auth-only table is not created in every customer DB
    assert "member_policy" not in pg_adapter.CREATE_TABLES_CORE
    # no ALTER on any auth table was needed
    assert "ALTER TABLE member_policy" not in pg_adapter.CREATE_TABLES_HOSTED


def test_db_package_re_exports_the_member_policy_functions():
    for name in (
        "get_member_policy_rows", "set_member_policy_default", "set_member_policy_override",
        "clear_member_policy_override", "list_member_policy_overrides", "get_member_roles",
        "MEMBER_POLICY_DEFAULT_SUBJECT", "MEMBER_POLICY_CHANGED_EVENT",
    ):
        assert getattr(db_module, name) is getattr(ws_module, name), name
    for name in ("get_project_family", "get_subproject_map"):
        assert callable(getattr(db_module, name))


def test_perm_manage_policy_is_owner_only():
    assert roles.PERM_MANAGE_POLICY == "policy"
    assert roles.has_perm("owner", roles.PERM_MANAGE_POLICY)
    for role in ("admin", "member", "viewer", "nope", None, ""):
        assert not roles.has_perm(role, roles.PERM_MANAGE_POLICY), role
    holders = {r for r, perms in roles.ROLE_PERMS.items() if roles.PERM_MANAGE_POLICY in perms}
    assert holders == {roles.ROLE_OWNER}
    # adding it did not change what the other roles can do
    assert roles.ROLE_PERMS[roles.ROLE_ADMIN] == frozenset({
        roles.PERM_READ, roles.PERM_WRITE, roles.PERM_HITL_ANSWER,
        roles.PERM_INVITE, roles.PERM_SETTINGS,
    })
    assert roles.ROLE_PERMS[roles.ROLE_MEMBER] == frozenset({roles.PERM_READ, roles.PERM_WRITE})
    assert roles.ROLE_PERMS[roles.ROLE_VIEWER] == frozenset({roles.PERM_READ})
    assert roles.ROLE_PERMS[roles.ROLE_OWNER] == frozenset({
        roles.PERM_READ, roles.PERM_WRITE, roles.PERM_HITL_ANSWER, roles.PERM_INVITE,
        roles.PERM_SETTINGS, roles.PERM_BILLING, roles.PERM_DELETE_TENANT,
        roles.PERM_MANAGE_POLICY,
    })
