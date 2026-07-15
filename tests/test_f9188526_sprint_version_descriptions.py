"""Tests for f9188526 — auto-generated sprint version bucket descriptions.

Covers:
  * _auto_generate_version_description: pure-Python generator (no DB)
  * get_sprint_version_description / upsert_sprint_version_description DB helpers
  * get_all_sprint_version_descriptions: multi-bucket fetch
  * add_sprint_item: seeds description on first add, refreshes on subsequent adds
  * get_sprint_progress (via MCP dispatch): includes version_descriptions when present
  * Migration idempotency: _migrate_sprint_version_descriptions is a no-op on a
    DB that already has the table.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

# Fixtures: db (aiosqlite connection) and project dict are provided by conftest.
# Use the same fixture names as the rest of the test suite.


# ---------------------------------------------------------------------------
# Pure-Python generator tests (no DB required)
# ---------------------------------------------------------------------------

def test_auto_generate_no_items():
    from meridian.db import _auto_generate_version_description
    desc = _auto_generate_version_description("v1.0", [])
    assert "v1.0" in desc
    assert "no items" in desc.lower()


def test_auto_generate_single_item():
    from meridian.db import _auto_generate_version_description
    desc = _auto_generate_version_description("v1.0", ["Add OAuth login"])
    assert "v1.0" in desc
    assert "1 item" in desc


def test_auto_generate_multiple_items_produces_theme():
    from meridian.db import _auto_generate_version_description
    titles = [
        "FEAT: add authentication middleware",
        "FIX: fix authentication token refresh",
        "FEAT: improve database migration tooling",
        "CHORE: add database connection pool",
    ]
    desc = _auto_generate_version_description("v2.0", titles)
    # Should mention the version label.
    assert "v2.0" in desc
    # Should note item count.
    assert "4 item" in desc
    # 'authentication' or 'database' should appear as top themes.
    desc_lower = desc.lower()
    assert "authentication" in desc_lower or "database" in desc_lower


def test_auto_generate_strips_prefix_tokens():
    from meridian.db import _auto_generate_version_description
    titles = [
        "feat: search indexing overhaul",
        "fix: search result ranking",
        "chore: search cache invalidation",
    ]
    desc = _auto_generate_version_description("v3.0", titles)
    # "search" should be a top theme since it is in every title after stripping prefixes.
    assert "search" in desc.lower()


def test_auto_generate_empty_version_label():
    from meridian.db import _auto_generate_version_description
    # version="" is legal (items with no version).
    desc = _auto_generate_version_description("", ["Fix the thing"])
    assert isinstance(desc, str)
    assert len(desc) > 0


def test_auto_generate_all_stopwords():
    from meridian.db import _auto_generate_version_description
    # Titles whose content reduces to stop-words only — should still return a
    # non-empty string without raising.
    titles = ["in on at to for of with by from up as is it a the and or"]
    desc = _auto_generate_version_description("v0", titles)
    assert isinstance(desc, str)
    assert "v0" in desc


# ---------------------------------------------------------------------------
# DB helper tests (require the aiosqlite fixture from conftest)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_sprint_version_description_missing(db):
    """Returns None when no description has been stored."""
    from meridian.db import get_sprint_version_description, create_project
    p = await create_project(db, "desc-missing-project")
    result = await get_sprint_version_description(db, p["id"], "v9.9")
    assert result is None


@pytest.mark.asyncio
async def test_upsert_and_get_description(db):
    """upsert stores a description; get returns it."""
    from meridian.db import (
        get_sprint_version_description,
        upsert_sprint_version_description,
        create_project,
    )
    p = await create_project(db, "desc-upsert-project")
    await upsert_sprint_version_description(db, p["id"], "v1.0", "Auth sprint")
    result = await get_sprint_version_description(db, p["id"], "v1.0")
    assert result == "Auth sprint"


@pytest.mark.asyncio
async def test_upsert_overwrites_existing_description(db):
    """A second upsert replaces the existing description."""
    from meridian.db import (
        get_sprint_version_description,
        upsert_sprint_version_description,
        create_project,
    )
    p = await create_project(db, "desc-overwrite-project")
    await upsert_sprint_version_description(db, p["id"], "v1.0", "First description")
    await upsert_sprint_version_description(db, p["id"], "v1.0", "Updated description")
    result = await get_sprint_version_description(db, p["id"], "v1.0")
    assert result == "Updated description"


@pytest.mark.asyncio
async def test_get_all_sprint_version_descriptions_empty(db):
    """Returns empty dict when no descriptions exist."""
    from meridian.db import get_all_sprint_version_descriptions, create_project
    p = await create_project(db, "all-desc-empty-project")
    result = await get_all_sprint_version_descriptions(db, p["id"])
    assert result == {}


@pytest.mark.asyncio
async def test_get_all_sprint_version_descriptions_multiple(db):
    """Returns all stored descriptions as a {version: desc} dict."""
    from meridian.db import (
        get_all_sprint_version_descriptions,
        upsert_sprint_version_description,
        create_project,
    )
    p = await create_project(db, "all-desc-multi-project")
    await upsert_sprint_version_description(db, p["id"], "v1.0", "Sprint A")
    await upsert_sprint_version_description(db, p["id"], "v2.0", "Sprint B")
    result = await get_all_sprint_version_descriptions(db, p["id"])
    assert result == {"v1.0": "Sprint A", "v2.0": "Sprint B"}


# ---------------------------------------------------------------------------
# Integration: add_sprint_item seeds descriptions automatically
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_sprint_item_seeds_version_description(db):
    """First add_sprint_item for a versioned item seeds a description."""
    from meridian.db import (
        add_sprint_item,
        get_sprint_version_description,
        create_project,
        set_goal,
    )
    p = await create_project(db, "auto-desc-seed-project")
    await set_goal(db, p["id"], "test goal")
    await add_sprint_item(db, p["id"], "v1.0", "FEAT: implement authentication")
    desc = await get_sprint_version_description(db, p["id"], "v1.0")
    assert desc is not None
    assert isinstance(desc, str)
    assert len(desc) > 0
    # The version label should appear in the description.
    assert "v1.0" in desc


@pytest.mark.asyncio
async def test_add_sprint_item_refreshes_description_on_second_add(db):
    """A second add_sprint_item to the same version refreshes the description."""
    from meridian.db import (
        add_sprint_item,
        get_sprint_version_description,
        create_project,
        set_goal,
    )
    p = await create_project(db, "auto-desc-refresh-project")
    await set_goal(db, p["id"], "test goal")
    await add_sprint_item(db, p["id"], "v1.0", "FEAT: authentication module")
    desc1 = await get_sprint_version_description(db, p["id"], "v1.0")

    await add_sprint_item(
        db, p["id"], "v1.0", "FIX: database migration for auth tables", force=True
    )
    desc2 = await get_sprint_version_description(db, p["id"], "v1.0")
    # After two items the description should reference 2 items.
    assert "2 item" in (desc2 or "")
    # Both descriptions must be non-None strings.
    assert desc1 is not None
    assert desc2 is not None


@pytest.mark.asyncio
async def test_add_sprint_item_no_description_for_empty_version(db):
    """Items with no version string do not seed a version description."""
    from meridian.db import (
        add_sprint_item,
        get_all_sprint_version_descriptions,
        create_project,
        set_goal,
    )
    p = await create_project(db, "auto-desc-empty-version-project")
    await set_goal(db, p["id"], "test goal")
    # version="" is falsy — should not write a description.
    await add_sprint_item(db, p["id"], "", "FEAT: no version item")
    descs = await get_all_sprint_version_descriptions(db, p["id"])
    # No key for the empty-string version.
    assert "" not in descs


@pytest.mark.asyncio
async def test_add_sprint_item_isolated_versions(db):
    """Adding items to different versions creates independent descriptions."""
    from meridian.db import (
        add_sprint_item,
        get_all_sprint_version_descriptions,
        create_project,
        set_goal,
    )
    p = await create_project(db, "auto-desc-isolated-project")
    await set_goal(db, p["id"], "test goal")
    await add_sprint_item(db, p["id"], "v1.0", "FEAT: auth overhaul")
    await add_sprint_item(db, p["id"], "v2.0", "FEAT: search indexing")
    descs = await get_all_sprint_version_descriptions(db, p["id"])
    assert "v1.0" in descs
    assert "v2.0" in descs
    # Each description should mention its own version label.
    assert "v1.0" in descs["v1.0"]
    assert "v2.0" in descs["v2.0"]


# ---------------------------------------------------------------------------
# MCP layer: get_sprint_progress includes version_descriptions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_sprint_progress_includes_version_descriptions(db):
    """get_sprint_progress response includes version_descriptions when items exist."""
    import meridian.server as srv
    from meridian.db import create_project, set_goal, add_sprint_item
    p = await create_project(db, "progress-desc-project")
    await set_goal(db, p["id"], "test goal")
    await add_sprint_item(db, p["id"], "v1.0", "FEAT: implement login")
    result = await srv._dispatch_mcp_tool(
        "get_sprint_progress", {"project_id": p["id"]}, db, "/tmp"
    )
    assert isinstance(result, dict)
    # version_descriptions key should be present.
    assert "version_descriptions" in result
    assert "v1.0" in result["version_descriptions"]
    assert isinstance(result["version_descriptions"]["v1.0"], str)


@pytest.mark.asyncio
async def test_get_sprint_progress_no_descriptions_key_when_empty(db):
    """get_sprint_progress omits version_descriptions when none are stored."""
    import meridian.server as srv
    from meridian.db import create_project, set_goal
    p = await create_project(db, "progress-no-desc-project")
    await set_goal(db, p["id"], "test goal")
    result = await srv._dispatch_mcp_tool(
        "get_sprint_progress", {"project_id": p["id"]}, db, "/tmp"
    )
    assert isinstance(result, dict)
    # key must be absent (not just empty) when there are no descriptions.
    assert "version_descriptions" not in result


# ---------------------------------------------------------------------------
# Migration idempotency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_migration_is_idempotent(db):
    """Running _migrate_sprint_version_descriptions twice does not error."""
    from meridian.db.migrations import _migrate_sprint_version_descriptions
    await _migrate_sprint_version_descriptions(db)
    await _migrate_sprint_version_descriptions(db)  # second call: no-op
    # If we get here without exception the idempotency guard works.
