"""Regression tests for item dbc5cc37 — add_sprint_item(project_name=...).

CONCRETE PART (verified here): calling ``add_sprint_item`` with a
``project_name`` (and NO ``project_id``) must NOT raise ``KeyError('project_id')``.
The MCP dispatcher (``_dispatch_mcp_tool`` in ``meridian/mcp/handler.py``)
resolves ``project_name`` -> ``project_id`` centrally, and the ``add_sprint_item``
handler guards the "neither present" case, returning a clean ``{"error": ...}``
dict rather than letting a direct ``args["project_id"]`` read raise a bare
``KeyError`` that leaks to the caller as a cryptic JSON-RPC -32603.

This locks in the CURRENT (already-fixed) behavior for item dbc5cc37. See also
``test_w5_8f01cdfe_addsprint_projname.py`` (the sibling item that originally
introduced the fix). These are pure unit tests over an in-memory SQLite ``db``
fixture (conftest): NO servers/ports/network/sleeps. ``force=True`` skips the
offline drift/commit check so nothing reaches git or the network.

FOLLOW-UP (NOT implemented here, by design): making the handoff behavior a
workspace-level hard-enforced default is a separate design change and is
intentionally out of scope for this regression item.
"""

from __future__ import annotations

import pytest

import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
from meridian import db as db_module
from meridian.mcp.handler import _dispatch_mcp_tool


async def _dispatch_add(db, **args):
    """Invoke add_sprint_item through the real MCP dispatch path.

    Routing through ``_dispatch_mcp_tool`` (not the handler group directly)
    exercises BOTH the dispatcher's project_name -> project_id resolver and the
    handler's project_id guard together — the two halves that make the KeyError
    impossible.
    """
    return await _dispatch_mcp_tool("add_sprint_item", dict(args), db, "/tmp", tenant=None)


@pytest.mark.asyncio
async def test_add_sprint_item_by_project_name_no_keyerror(db):
    """project_name against an existing project -> item created, no KeyError."""
    proj = await db_module.create_project(db, "proj-dbc5cc37")

    result = await _dispatch_add(
        db,
        project_name="proj-dbc5cc37",
        version="v1",
        title="Item created via project_name",
        force=True,  # skip the offline drift check — no git/network
    )

    assert isinstance(result, dict), result
    assert "error" not in result, result
    # Resolved to the real project id and persisted.
    assert result.get("project_id") == proj["id"]
    assert result.get("title") == "Item created via project_name"

    items = await db_module.get_sprint_items(db, proj["id"])
    assert "Item created via project_name" in [it["title"] for it in items]


@pytest.mark.asyncio
async def test_add_sprint_item_neither_id_nor_name_clean_error(db):
    """Neither project_id nor project_name -> clean {error}, never raw KeyError."""
    result = await _dispatch_add(
        db,
        version="v1",
        title="Orphan item dbc5cc37",
        force=True,
    )

    assert isinstance(result, dict), result
    assert "error" in result, result
    # Descriptive error, not a bare KeyError repr like "'project_id'".
    assert result["error"] != "'project_id'"
    assert "project_id" in result["error"]
    assert "project_name" in result["error"]


@pytest.mark.asyncio
async def test_add_sprint_item_direct_keyerror_would_have_occurred(db):
    """Guard: the dispatch path must not propagate KeyError for the name case.

    A stricter statement of the fix: even if resolution/guarding logic regresses,
    a raw KeyError('project_id') must never escape this call.
    """
    try:
        result = await _dispatch_add(
            db,
            project_name="proj-dbc5cc37-guard",
            version="v1",
            title="name-only guard item",
            force=True,
        )
    except KeyError as exc:  # pragma: no cover - regression tripwire
        pytest.fail(f"add_sprint_item leaked a raw KeyError: {exc!r}")
    except ValueError:
        # Unresolvable name surfaces as a descriptive ValueError — acceptable and
        # explicitly NOT a KeyError. (Project was not created in this test.)
        return

    # If the project happened to resolve, it must be a clean dict either way.
    assert isinstance(result, dict), result
