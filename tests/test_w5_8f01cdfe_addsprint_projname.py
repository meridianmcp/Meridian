"""Regression tests for item 8f01cdfe — add_sprint_item + project_name.

BUG: ``add_sprint_item`` read ``args["project_id"]`` directly (drift check,
active-session warning, and the db call), so calling it with ``project_name``
instead of ``project_id`` — or with NEITHER and no active session — raised a raw
``KeyError: 'project_id'`` that leaked to the caller as a cryptic JSON-RPC
-32603, even though the schema/description advertise ``project_name`` as an
accepted alternative.

FIX: the MCP dispatcher's project_name resolver (``_dispatch_mcp_tool``) already
turns a present, resolvable ``project_name`` into ``project_id`` before the
handler runs; the handler now additionally guards the "neither present" case and
returns a clean ``{"error": ...}`` dict instead of letting the direct
``args["project_id"]`` reads raise ``KeyError``.

These are pure unit tests: an in-memory SQLite ``db`` fixture (conftest), NO
servers/ports/network/sleeps. ``force=True`` skips the offline drift/commit
check entirely, so nothing reaches git or the network.
"""

from __future__ import annotations

import pytest

import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
from meridian import db as db_module
from meridian.mcp.handler import _dispatch_mcp_tool


async def _add(db, **args):
    """Invoke the add_sprint_item tool through the real MCP dispatch path.

    Routing through ``_dispatch_mcp_tool`` (not the handler group directly)
    exercises BOTH the dispatcher's project_name resolver and the handler's
    project_id guard — the two halves of the fix — together.
    """
    return await _dispatch_mcp_tool("add_sprint_item", dict(args), db, "/tmp", tenant=None)


@pytest.mark.asyncio
async def test_add_via_project_name_resolves_and_creates(db):
    """project_name (no project_id) must resolve → item created, no KeyError."""
    proj = await db_module.create_project(db, "proj-by-name-8f01cdfe")

    result = await _add(
        db,
        project_name="proj-by-name-8f01cdfe",
        version="v1",
        title="Item added by project_name",
        force=True,  # skip the offline drift check — no git/network
    )

    assert isinstance(result, dict)
    assert "error" not in result, result
    # The item was created against the resolved project id.
    assert result.get("project_id") == proj["id"]
    assert result.get("title") == "Item added by project_name"

    # And it is actually persisted under that project.
    items = await db_module.get_sprint_items(db, proj["id"])
    titles = [it["title"] for it in items]
    assert "Item added by project_name" in titles


@pytest.mark.asyncio
async def test_add_with_neither_returns_clean_error_not_keyerror(db):
    """Neither project_id nor project_name → clean {error}, never a raw KeyError."""
    result = await _add(
        db,
        version="v1",
        title="Orphan item",
        force=True,
    )

    assert isinstance(result, dict), result
    assert "error" in result, result
    assert "project_id" in result["error"]
    # Descriptive, not a bare KeyError repr like "'project_id'".
    assert result["error"] != "'project_id'"
    assert "project_name" in result["error"]


@pytest.mark.asyncio
async def test_add_with_empty_project_id_returns_clean_error(db):
    """An explicit empty/blank project_id is treated as absent → clean error."""
    result = await _add(
        db,
        project_id="",
        version="v1",
        title="Blank pid item",
        force=True,
    )

    assert isinstance(result, dict), result
    assert "error" in result, result
    assert result["error"] != "'project_id'"


@pytest.mark.asyncio
async def test_add_via_project_id_still_works(db):
    """Regression guard: the normal project_id path is unaffected by the fix."""
    proj = await db_module.create_project(db, "proj-by-id-8f01cdfe")

    result = await _add(
        db,
        project_id=proj["id"],
        version="v1",
        title="Item added by id",
        force=True,
    )

    assert isinstance(result, dict)
    assert "error" not in result, result
    assert result.get("project_id") == proj["id"]
    assert result.get("title") == "Item added by id"


@pytest.mark.asyncio
async def test_add_unresolvable_project_name_does_not_crash_with_keyerror(db):
    """A project_name that matches no project must not surface a raw KeyError.

    The dispatcher resolver raises a descriptive ValueError for an unresolvable
    name (surfaced by the outer dispatch layer as a clean JSON-RPC error), and in
    no case does the bare ``KeyError: 'project_id'`` escape.
    """
    with pytest.raises(ValueError) as exc:
        await _add(
            db,
            project_name="no-such-project-xyz-8f01cdfe",
            version="v1",
            title="Unresolvable name item",
            force=True,
        )
    # Descriptive message about the name — not a KeyError, not "'project_id'".
    assert "no-such-project-xyz-8f01cdfe" in str(exc.value)
    assert not isinstance(exc.value, KeyError)
