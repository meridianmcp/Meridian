"""Tests for 2593a5fe — touches_resources amendment on mid-execution pivot.

When claim_file or claim_symbol is called with a resource NOT already in the
active sprint item's touches_resources declaration, the new resource should be
appended and resources_amended=1 set. A wave_assignment_hint should surface in
the claim response when the item already had a wave label.

Test matrix:
  (a) claim_file on a session with an in_progress item that declares the file
      -> no amendment needed, resources_amended stays 0.
  (b) claim_file on a session with an in_progress item that does NOT declare the
      file -> resource appended, resources_amended=1, wave_assignment_hint present
      when the item has a wave.
  (c) claim_file with NO active in_progress item for the session -> no amendment,
      no wave_assignment_hint, claim succeeds normally.
  (d) _amend_sprint_item_resources_for_session directly: already-declared
      resource is a no-op; undeclared resource appends + sets resources_amended.
  (e) Migration smoke test: resources_amended column exists on sprint_items after
      init_db.
  (f) claim_symbol on a session with an in_progress item that does NOT declare the
      symbol -> resource appended, resources_amended=1.
"""
from __future__ import annotations

import json

import pytest
import pytest_asyncio

from meridian import db as db_module


# ---------------------------------------------------------------------------
# Shared fixture: an in-memory DB with two sessions and a sprint item.
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db_with_item():
    """In-memory DB with:
      - project P1
      - session S1 (in_progress, actor='sess-s1')
      - sprint item I1 in_progress claimed by actor='sess-s1'
        with touches_resources = ["file:meridian/server.py"]
        and wave = 'wave-1'
      - session S2 (no sprint item)
    Returns (db, project_id, session_id, item_id, session2_id).
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

        session2_id = "sess-s2"
        await conn.execute(
            "INSERT INTO sessions (id, project_id, name, status) VALUES (?, ?, ?, 'active')",
            (session2_id, project_id, session2_id),
        )

        item_id = db_module._new_id()
        resources_json = json.dumps(["file:meridian/server.py"])
        await conn.execute(
            "INSERT INTO sprint_items "
            "(id, project_id, version, title, status, actor, touches_resources, wave) "
            "VALUES (?, ?, '', 'Test item', 'in_progress', ?, ?, 'wave-1')",
            (item_id, project_id, session_id, resources_json),
        )
        await conn.commit()

        yield conn, project_id, session_id, item_id, session2_id
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# (e) Migration smoke: resources_amended column exists after init_db
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resources_amended_column_exists_after_init_db():
    """The resources_amended column is present on sprint_items after init_db.
    This exercises _migrate_sprint_item_resources_amended on a fresh DB."""
    conn = await db_module.init_db(":memory:")
    try:
        project_id = db_module._new_id()
        await conn.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            (project_id, "col-test"),
        )
        item_id = db_module._new_id()
        # Explicitly write resources_amended=0 — would fail if column missing
        await conn.execute(
            "INSERT INTO sprint_items (id, project_id, version, title, resources_amended) "
            "VALUES (?, ?, '', 'col test', 0)",
            (item_id, project_id),
        )
        await conn.commit()

        async with conn.execute(
            "SELECT resources_amended FROM sprint_items WHERE id = ?", (item_id,)
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        amended = row["resources_amended"] if isinstance(row, dict) else row[0]
        assert amended == 0 or amended is None  # 0 or NULL, either fine
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# (d) _amend_sprint_item_resources_for_session -- unit tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_amend_no_op_when_resource_already_declared(db_with_item):
    """Already-declared resource -> no amendment (returns None, resources_amended stays 0)."""
    conn, project_id, session_id, item_id, _ = db_with_item

    result = await db_module._amend_sprint_item_resources_for_session(
        conn, session_id, "file:meridian/server.py"
    )
    # No amendment needed -> returns None
    assert result is None

    # Verify resources_amended is still 0 (not set)
    async with conn.execute(
        "SELECT resources_amended, touches_resources FROM sprint_items WHERE id = ?",
        (item_id,),
    ) as cur:
        row = await cur.fetchone()
    r = dict(row) if not isinstance(row, dict) else row
    assert not r["resources_amended"]  # 0 or NULL


@pytest.mark.asyncio
async def test_amend_appends_new_resource_and_sets_flag(db_with_item):
    """Undeclared resource -> appended to touches_resources, resources_amended=1."""
    conn, project_id, session_id, item_id, _ = db_with_item

    result = await db_module._amend_sprint_item_resources_for_session(
        conn, session_id, "file:meridian/db/__init__.py"
    )
    # Amendment happened -> result is a dict
    assert result is not None
    assert result.get("amended_resource") == "file:meridian/db/__init__.py"
    assert result.get("item_id") == item_id
    assert result.get("item_wave") == "wave-1"
    # wave_assignment_hint is present because item has wave label
    assert result.get("wave_assignment_hint") is not None
    assert "WAVE_STALE" in result["wave_assignment_hint"]

    # Verify DB state: resources_amended=1, new resource in touches_resources
    async with conn.execute(
        "SELECT resources_amended, touches_resources FROM sprint_items WHERE id = ?",
        (item_id,),
    ) as cur:
        row = await cur.fetchone()
    r = dict(row) if not isinstance(row, dict) else row
    assert r["resources_amended"] == 1
    resources = json.loads(r["touches_resources"])
    assert "file:meridian/server.py" in resources  # original preserved
    assert "file:meridian/db/__init__.py" in resources  # new resource added


@pytest.mark.asyncio
async def test_amend_no_sprint_item_for_session(db_with_item):
    """Session with no in_progress item -> no amendment, returns None."""
    conn, project_id, session_id, item_id, session2_id = db_with_item

    result = await db_module._amend_sprint_item_resources_for_session(
        conn, session2_id, "file:meridian/db/__init__.py"
    )
    assert result is None

    # Original item is unchanged
    async with conn.execute(
        "SELECT resources_amended FROM sprint_items WHERE id = ?", (item_id,)
    ) as cur:
        row = await cur.fetchone()
    r = dict(row) if not isinstance(row, dict) else row
    assert not r["resources_amended"]


@pytest.mark.asyncio
async def test_amend_no_wave_hint_when_no_wave_label(db_with_item):
    """Item without a wave label still gets resources_amended=1 but wave_assignment_hint
    in the result dict is None (item has no wave label to go stale)."""
    conn, project_id, session_id, item_id, _ = db_with_item

    # Clear the wave label
    await conn.execute(
        "UPDATE sprint_items SET wave = NULL WHERE id = ?", (item_id,)
    )
    await conn.commit()

    result = await db_module._amend_sprint_item_resources_for_session(
        conn, session_id, "file:meridian/mcp/handler.py"
    )
    assert result is not None
    # wave_assignment_hint should be None (item had no wave), not a string
    assert result.get("wave_assignment_hint") is None
    assert result.get("amended_resource") == "file:meridian/mcp/handler.py"
    assert result.get("item_wave") is None

    # resources_amended IS still set to 1
    async with conn.execute(
        "SELECT resources_amended FROM sprint_items WHERE id = ?", (item_id,)
    ) as cur:
        row = await cur.fetchone()
    r = dict(row) if not isinstance(row, dict) else row
    assert r["resources_amended"] == 1


# ---------------------------------------------------------------------------
# (a) claim_file: resource already declared -> no amendment
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claim_file_no_amendment_when_resource_declared(db_with_item):
    """claim_file for a file already in touches_resources: no amendment, no hint."""
    conn, project_id, session_id, item_id, _ = db_with_item

    result = await db_module.claim_file(
        conn, "meridian/server.py", session_id
    )
    assert result.get("claimed") is True
    # No wave_assignment_hint (or it is None) -- resource was already declared
    assert not result.get("wave_assignment_hint")

    async with conn.execute(
        "SELECT resources_amended FROM sprint_items WHERE id = ?", (item_id,)
    ) as cur:
        row = await cur.fetchone()
    r = dict(row) if not isinstance(row, dict) else row
    assert not r["resources_amended"]


# ---------------------------------------------------------------------------
# (b) claim_file: new resource -> amendment + wave_assignment_hint in response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claim_file_amendment_when_resource_not_declared(db_with_item):
    """claim_file for a file NOT in touches_resources: resource appended, hint in response."""
    conn, project_id, session_id, item_id, _ = db_with_item

    result = await db_module.claim_file(
        conn, "meridian/db/locks.py", session_id
    )
    assert result.get("claimed") is True
    # wave_assignment_hint present (item has wave='wave-1')
    assert result.get("wave_assignment_hint") is not None
    assert "WAVE_STALE" in result["wave_assignment_hint"]

    # Verify DB: resources_amended=1, new file in list
    async with conn.execute(
        "SELECT resources_amended, touches_resources FROM sprint_items WHERE id = ?",
        (item_id,),
    ) as cur:
        row = await cur.fetchone()
    r = dict(row) if not isinstance(row, dict) else row
    assert r["resources_amended"] == 1
    resources = json.loads(r["touches_resources"])
    assert "file:meridian/server.py" in resources
    assert "file:meridian/db/locks.py" in resources


# ---------------------------------------------------------------------------
# (c) claim_file with no active sprint item -> claim succeeds, no amendment
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claim_file_no_sprint_item_for_session(db_with_item):
    """claim_file for a session with no in_progress sprint item -> claim ok, no amendment."""
    conn, project_id, session_id, item_id, session2_id = db_with_item

    result = await db_module.claim_file(
        conn, "meridian/db/locks.py", session2_id
    )
    assert result.get("claimed") is True
    assert not result.get("wave_assignment_hint")

    # Item owned by session_id is unchanged
    async with conn.execute(
        "SELECT resources_amended FROM sprint_items WHERE id = ?", (item_id,)
    ) as cur:
        row = await cur.fetchone()
    r = dict(row) if not isinstance(row, dict) else row
    assert not r["resources_amended"]


# ---------------------------------------------------------------------------
# (f) claim_symbol: new symbol resource -> amendment
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claim_symbol_amendment_when_symbol_not_declared(db_with_item):
    """claim_symbol for a symbol NOT in touches_resources: symbol resource appended.

    Uses a minimal file content with a parseable Python function so claim_symbol
    can extract the symbol. The symbol resource id is 'symbol:<path>::<name>'.
    """
    conn, project_id, session_id, item_id, _ = db_with_item

    # Minimal parseable Python content
    content = "def my_function():\n    pass\n"
    result = await db_module.claim_symbol(
        conn, session_id, "meridian/test_temp.py", "my_function", content
    )
    assert result.get("claimed") is True

    # The symbol resource should be appended
    async with conn.execute(
        "SELECT resources_amended, touches_resources FROM sprint_items WHERE id = ?",
        (item_id,),
    ) as cur:
        row = await cur.fetchone()
    r = dict(row) if not isinstance(row, dict) else row
    assert r["resources_amended"] == 1
    resources = json.loads(r["touches_resources"])
    assert "file:meridian/server.py" in resources  # original preserved
    assert any("my_function" in res for res in resources)  # symbol appended


# ---------------------------------------------------------------------------
# (d2) Idempotency: amending the same resource twice does not duplicate it
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_amend_idempotent_no_duplicate(db_with_item):
    """Amending the same resource twice does not duplicate it in touches_resources."""
    conn, project_id, session_id, item_id, _ = db_with_item

    await db_module._amend_sprint_item_resources_for_session(
        conn, session_id, "file:meridian/pg_adapter.py"
    )
    await db_module._amend_sprint_item_resources_for_session(
        conn, session_id, "file:meridian/pg_adapter.py"
    )

    async with conn.execute(
        "SELECT touches_resources FROM sprint_items WHERE id = ?", (item_id,)
    ) as cur:
        row = await cur.fetchone()
    r = dict(row) if not isinstance(row, dict) else row
    resources = json.loads(r["touches_resources"])
    # No duplicates
    assert resources.count("file:meridian/pg_adapter.py") == 1
