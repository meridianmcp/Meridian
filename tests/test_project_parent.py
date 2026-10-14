"""Coverage for set_parent_project / rename_project (7acb8563).

Makes the projects.parent_project_id relationship editable AFTER creation
(create_project only accepted it at creation time), plus an MCP wrapper over the
existing db.rename_project. Exercises the one-level-deep invariant (3b6ff466) at
the DB layer and both tools through the real _dispatch_mcp_tool path.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from meridian import db as db_module


# ---------------------------------------------------------------------------
# DB layer — set_parent_project invariants
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_parent_project_attach_and_detach(db):
    parent = await db_module.create_project(db, "pp-parent")
    child = await db_module.create_project(db, "pp-child")
    assert child.get("parent_project_id") in (None, "")

    attached = await db_module.set_parent_project(db, child["id"], parent["id"])
    assert attached["parent_project_id"] == parent["id"]

    detached = await db_module.set_parent_project(db, child["id"], None)
    assert detached.get("parent_project_id") in (None, "")


@pytest.mark.asyncio
async def test_set_parent_project_missing_project_returns_none(db):
    assert await db_module.set_parent_project(db, "does-not-exist", None) is None


@pytest.mark.asyncio
async def test_set_parent_project_rejects_self_parent(db):
    p = await db_module.create_project(db, "pp-self")
    with pytest.raises(ValueError):
        await db_module.set_parent_project(db, p["id"], p["id"])


@pytest.mark.asyncio
async def test_set_parent_project_rejects_missing_parent(db):
    p = await db_module.create_project(db, "pp-orphan")
    with pytest.raises(ValueError):
        await db_module.set_parent_project(db, p["id"], "no-such-parent")


@pytest.mark.asyncio
async def test_set_parent_project_rejects_two_levels(db):
    """A parent that is itself a subproject can't be a parent (one level deep)."""
    top = await db_module.create_project(db, "pp-top")
    mid = await db_module.create_project(db, "pp-mid", parent_project_id=top["id"])
    leaf = await db_module.create_project(db, "pp-leaf")
    with pytest.raises(ValueError):
        await db_module.set_parent_project(db, leaf["id"], mid["id"])


@pytest.mark.asyncio
async def test_set_parent_project_rejects_reparenting_a_parent(db):
    """A project that already HAS subprojects can't become a subproject itself."""
    a = await db_module.create_project(db, "pp-a")
    _b = await db_module.create_project(db, "pp-b", parent_project_id=a["id"])
    c = await db_module.create_project(db, "pp-c")
    with pytest.raises(ValueError):
        await db_module.set_parent_project(db, a["id"], c["id"])


# ---------------------------------------------------------------------------
# MCP dispatch — set_parent_project / rename_project
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_set_parent_project_by_name(db):
    from meridian import server as srv
    parent = await db_module.create_project(db, "pp-mcp-parent")
    child = await db_module.create_project(db, "pp-mcp-child")

    result = await srv._dispatch_mcp_tool(
        "set_parent_project",
        {"project_name": "pp-mcp-child", "parent_project_name": "pp-mcp-parent"},
        db, "/tmp",
    )
    assert result["parent_project_id"] == parent["id"]

    # omitting the parent detaches (top-level again)
    detached = await srv._dispatch_mcp_tool(
        "set_parent_project", {"project_id": child["id"]}, db, "/tmp",
    )
    assert detached.get("parent_project_id") in (None, "")


@pytest.mark.asyncio
async def test_mcp_set_parent_project_invalid_returns_error(db):
    from meridian import server as srv
    p = await db_module.create_project(db, "pp-mcp-bad")
    result = await srv._dispatch_mcp_tool(
        "set_parent_project",
        {"project_id": p["id"], "parent_project_id": p["id"]},  # self-parent
        db, "/tmp",
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_mcp_rename_project(db):
    from meridian import server as srv
    p = await db_module.create_project(db, "pp-rename-old")
    result = await srv._dispatch_mcp_tool(
        "rename_project",
        {"project_id": p["id"], "new_name": "pp-rename-new"},
        db, "/tmp",
    )
    assert result["name"] == "pp-rename-new"
    assert (await db_module.get_project(db, p["id"]))["name"] == "pp-rename-new"


def test_parent_project_tools_registered():
    from meridian.mcp_tools import _MCP_TOOLS_LIST, _READ_ONLY_TOOLS
    names = {t["name"] for t in _MCP_TOOLS_LIST}
    assert {"set_parent_project", "rename_project"} <= names
    # both are writes, not read-only
    assert "set_parent_project" not in _READ_ONLY_TOOLS
    assert "rename_project" not in _READ_ONLY_TOOLS


# ---------------------------------------------------------------------------
# Compare-and-set: the write is ONE conditional UPDATE, so a project or parent
# that changed between the validation and the write is refused, not overwritten.
# ---------------------------------------------------------------------------

class _RaceConn:
    """Forwards to the real connection, but first runs ``before(real_conn)`` once,
    right before the first statement that starts with ``prefix``: the other
    request that wins the race, landing between validation and write."""

    def __init__(self, inner, prefix, before):
        self._inner = inner
        self._prefix = prefix
        self._before = before
        self.fired = False

    def execute(self, sql, params=()):
        if not self.fired and sql.lstrip().startswith(self._prefix):
            self.fired = True
            return self._run(sql, params)
        return self._inner.execute(sql, params)

    async def _run(self, sql, params):
        await self._before(self._inner)
        return await self._inner.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._inner, name)


async def _raw_insert_child(conn, name, parent_id):
    """What a concurrent create_project(parent=...) leaves behind."""
    await conn.execute(
        "INSERT INTO projects (id, name, parent_project_id) VALUES (?, ?, ?)",
        (f"raced-{name}", name, parent_id),
    )
    await conn.commit()


async def _raw_set_parent(conn, project_id, parent_id):
    await conn.execute(
        "UPDATE projects SET parent_project_id = ? WHERE id = ?", (parent_id, project_id)
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_nest_loses_the_race_against_a_child_being_created(db):
    """x is nested under p while a child is created under x: only one can win, the
    nest must not turn x into the middle of a two-level chain."""
    p = await db_module.create_project(db, "cas-nest-p")
    x = await db_module.create_project(db, "cas-nest-x")

    async def create_child_under_x(conn):
        await _raw_insert_child(conn, "cas-nest-late-child", x["id"])

    racing = _RaceConn(db, "UPDATE projects SET parent_project_id", create_child_under_x)
    with pytest.raises(ValueError, match="changed concurrently"):
        await db_module.set_parent_project(racing, x["id"], p["id"])
    assert racing.fired
    assert not (await db_module.get_project(db, x["id"])).get("parent_project_id")  # x stayed top-level
    # the child that won the race is intact
    assert (await db_module.get_project(db, "raced-cas-nest-late-child"))["parent_project_id"] == x["id"]


@pytest.mark.asyncio
async def test_child_create_loses_the_race_against_the_parent_being_nested(db):
    """The other order: the parent is nested after create_project validated it."""
    p = await db_module.create_project(db, "cas-kid-p")
    x = await db_module.create_project(db, "cas-kid-x")

    async def nest_x_under_p(conn):
        await _raw_set_parent(conn, x["id"], p["id"])

    racing = _RaceConn(db, "INSERT INTO projects", nest_x_under_p)
    with pytest.raises(ValueError, match="changed concurrently"):
        await db_module.create_project(racing, "cas-kid-late", parent_project_id=x["id"])
    assert racing.fired
    async with db.execute("SELECT 1 FROM projects WHERE name = ?", ("cas-kid-late",)) as cur:
        assert await cur.fetchone() is None  # nothing was inserted under the now-nested x


@pytest.mark.asyncio
async def test_nest_loses_the_race_against_the_new_parent_being_nested(db):
    a = await db_module.create_project(db, "cas-par-a")
    b = await db_module.create_project(db, "cas-par-b")
    x = await db_module.create_project(db, "cas-par-x")

    async def nest_b_under_a(conn):
        await _raw_set_parent(conn, b["id"], a["id"])

    racing = _RaceConn(db, "UPDATE projects SET parent_project_id", nest_b_under_a)
    with pytest.raises(ValueError, match="changed concurrently"):
        await db_module.set_parent_project(racing, x["id"], b["id"])
    assert not (await db_module.get_project(db, x["id"])).get("parent_project_id")


@pytest.mark.asyncio
async def test_set_parent_project_expect_parent_matches(db):
    a = await db_module.create_project(db, "cas-exp-a")
    b = await db_module.create_project(db, "cas-exp-b")
    x = await db_module.create_project(db, "cas-exp-x")

    # top-level expected as None or ""
    moved = await db_module.set_parent_project(db, x["id"], a["id"], expect_parent=None)
    assert moved["parent_project_id"] == a["id"]
    moved = await db_module.set_parent_project(db, x["id"], b["id"], expect_parent=a["id"])
    assert moved["parent_project_id"] == b["id"]
    detached = await db_module.set_parent_project(db, x["id"], None, expect_parent=b["id"])
    assert not detached.get("parent_project_id")
    again = await db_module.set_parent_project(db, x["id"], a["id"], expect_parent="")
    assert again["parent_project_id"] == a["id"]
    # same parent again is a no-op that still succeeds
    same = await db_module.set_parent_project(db, x["id"], a["id"], expect_parent=a["id"])
    assert same["parent_project_id"] == a["id"]


@pytest.mark.asyncio
async def test_set_parent_project_wrong_expected_parent_changes_nothing(db):
    a = await db_module.create_project(db, "cas-wrong-a")
    b = await db_module.create_project(db, "cas-wrong-b")
    x = await db_module.create_project(db, "cas-wrong-x", parent_project_id=a["id"])

    for expect in (None, "", b["id"], "no-such-project"):
        with pytest.raises(ValueError, match="changed concurrently"):
            await db_module.set_parent_project(db, x["id"], b["id"], expect_parent=expect)
        with pytest.raises(ValueError, match="changed concurrently"):
            await db_module.set_parent_project(db, x["id"], None, expect_parent=expect)
    assert (await db_module.get_project(db, x["id"]))["parent_project_id"] == a["id"]


@pytest.mark.asyncio
async def test_detach_with_expected_parent_loses_to_a_concurrent_move(db):
    """The caller saw x under a and may detach it. Someone moves it under b first:
    the detach must not go through on the stale decision."""
    a = await db_module.create_project(db, "cas-mv-a")
    b = await db_module.create_project(db, "cas-mv-b")
    x = await db_module.create_project(db, "cas-mv-x", parent_project_id=a["id"])

    async def move_x_under_b(conn):
        await _raw_set_parent(conn, x["id"], b["id"])

    racing = _RaceConn(db, "UPDATE projects SET parent_project_id", move_x_under_b)
    with pytest.raises(ValueError, match="changed concurrently"):
        await db_module.set_parent_project(racing, x["id"], None, expect_parent=a["id"])
    assert (await db_module.get_project(db, x["id"]))["parent_project_id"] == b["id"]


@pytest.mark.asyncio
async def test_set_parent_project_expect_parent_for_a_dangling_parent(db):
    gone = await db_module.create_project(db, "cas-dangle-gone")
    x = await db_module.create_project(db, "cas-dangle-x", parent_project_id=gone["id"])
    await db_module.delete_project(db, gone["id"])
    other = await db_module.create_project(db, "cas-dangle-other")
    # classified as an orphan with its dangling parent id: re-homing it is allowed
    moved = await db_module.set_parent_project(db, x["id"], other["id"], expect_parent=gone["id"])
    assert moved["parent_project_id"] == other["id"]


@pytest.mark.asyncio
async def test_set_parent_project_without_expect_parent_behaves_as_before(db):
    """Existing callers pass no expect_parent: no parent comparison, same results."""
    a = await db_module.create_project(db, "cas-old-a")
    b = await db_module.create_project(db, "cas-old-b")
    x = await db_module.create_project(db, "cas-old-x", parent_project_id=a["id"])
    assert (await db_module.set_parent_project(db, x["id"], b["id"]))["parent_project_id"] == b["id"]
    assert not (await db_module.set_parent_project(db, x["id"], None)).get("parent_project_id")
    # the validation errors keep their own messages (not the concurrency one)
    with pytest.raises(ValueError, match="own parent"):
        await db_module.set_parent_project(db, x["id"], x["id"])
    with pytest.raises(ValueError, match="does not exist"):
        await db_module.set_parent_project(db, x["id"], "no-such-parent")


@pytest.mark.asyncio
async def test_create_project_under_a_top_level_parent_still_works(db):
    p = await db_module.create_project(db, "cas-create-p")
    kid = await db_module.create_project(db, "cas-create-kid", human_id="h1", parent_project_id=p["id"])
    assert kid["parent_project_id"] == p["id"]
    assert kid["creator_human_id"] == "h1"
    with pytest.raises(ValueError, match="one level deep"):
        await db_module.create_project(db, "cas-create-grandkid", parent_project_id=kid["id"])
    with pytest.raises(ValueError, match="does not exist"):
        await db_module.create_project(db, "cas-create-nobody", parent_project_id="nope")


@pytest.mark.asyncio
async def test_postgres_hierarchy_writes_wait_for_other_connection_lock():
    """Child creation and reparenting take a cross-connection PG table lock."""
    import conftest

    pg_url = conftest._pg_test_url()
    if not pg_url:
        pytest.skip("Postgres hierarchy locking requires TEST_DATABASE_URL")
    await conftest._ensure_pg_schema_built(pg_url)

    from meridian.pg_adapter import open_pg_connection

    blocker = await open_pg_connection(pg_url)
    writer = await open_pg_connection(pg_url)
    created_ids: list[str] = []
    suffix = uuid.uuid4().hex
    async with writer.execute("SELECT 1") as cur:
        await cur.fetchone()

    class _ObservedConnection:
        def __init__(self, inner):
            self.inner = inner
            self.lock_attempted = asyncio.Event()

        def execute(self, sql, params=()):
            # Deliberately not ``async def``: callers write ``async with db.execute(...)``,
            # which needs the awaitable proxy that PostgresConnection.execute returns.
            if sql.strip().upper() == "LOCK TABLE PROJECTS IN SHARE ROW EXCLUSIVE MODE":
                self.lock_attempted.set()
            return self.inner.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    observed_writer = _ObservedConnection(writer)

    async def must_wait_for_row_exclusive(operation):
        observed_writer.lock_attempted.clear()
        task = None
        async with blocker.begin_transaction():
            try:
                await blocker.execute("LOCK TABLE projects IN ROW EXCLUSIVE MODE")
                task = asyncio.create_task(operation())
                await asyncio.wait_for(observed_writer.lock_attempted.wait(), timeout=5)
                await asyncio.sleep(0.05)
                completed_while_lock_held = task.done()
                await blocker.commit()
            except BaseException:
                await blocker.rollback()
                if task is not None and not task.done():
                    await asyncio.gather(task, return_exceptions=True)
                raise
        assert task is not None
        if completed_while_lock_held:
            await task
            pytest.fail("hierarchy mutation skipped its conflicting table lock")
        return await asyncio.wait_for(task, timeout=10)

    try:
        parent = await db_module.create_project(writer, f"cas-lock-parent-{suffix}")
        created_ids.append(parent["id"])
        target = await db_module.create_project(writer, f"cas-lock-target-{suffix}")
        created_ids.append(target["id"])
        source = await db_module.create_project(writer, f"cas-lock-source-{suffix}")
        created_ids.append(source["id"])

        child = await must_wait_for_row_exclusive(
            lambda: db_module.create_project(
                observed_writer,
                f"cas-lock-child-{suffix}",
                parent_project_id=parent["id"],
            )
        )
        created_ids.append(child["id"])
        assert child["parent_project_id"] == parent["id"]

        moved = await must_wait_for_row_exclusive(
            lambda: db_module.set_parent_project(
                observed_writer, source["id"], target["id"]
            )
        )
        assert moved["parent_project_id"] == target["id"]
    finally:
        for project_id in reversed(created_ids):
            await db_module.delete_project(writer, project_id)
        await blocker.close()
        await writer.close()
