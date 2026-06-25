"""Tests for 0bf67524 — workspace-default settings cascade.

Covers the new workspace_settings columns (execution_mode_default,
code_intel_enabled_default), their get/update round-trip, and the
creation-time cascade that seeds NEW projects from those defaults while
leaving explicit values and existing projects untouched.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module


# ---------------------------------------------------------------------------
# workspace_settings new columns — round-trip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_workspace_settings_defaults_roundtrip(db):
    # Fresh DB → new default fields are None (no workspace default set).
    ws = await db_module.get_workspace_settings(db)
    assert ws["execution_mode_default"] is None
    assert ws["code_intel_enabled_default"] is None

    out = await db_module.update_workspace_settings(
        db, execution_mode_default="interactive", code_intel_enabled_default=True,
    )
    assert out["execution_mode_default"] == "interactive"
    assert out["code_intel_enabled_default"] is True

    # Persisted on a fresh read.
    ws2 = await db_module.get_workspace_settings(db)
    assert ws2["execution_mode_default"] == "interactive"
    assert ws2["code_intel_enabled_default"] is True


@pytest.mark.asyncio
async def test_workspace_settings_execution_mode_default_normalizes_and_clears(db):
    # Garbage normalizes to autonomous.
    out = await db_module.update_workspace_settings(db, execution_mode_default="bogus")
    assert out["execution_mode_default"] == "autonomous"
    # Empty string clears it back to None.
    out2 = await db_module.update_workspace_settings(db, execution_mode_default="")
    assert out2["execution_mode_default"] is None


@pytest.mark.asyncio
async def test_workspace_settings_code_intel_default_clear(db):
    await db_module.update_workspace_settings(db, code_intel_enabled_default=1)
    assert (await db_module.get_workspace_settings(db))["code_intel_enabled_default"] is True
    # "" clears.
    await db_module.update_workspace_settings(db, code_intel_enabled_default="")
    assert (await db_module.get_workspace_settings(db))["code_intel_enabled_default"] is None
    # 0 sets it to False (explicit off), not cleared.
    await db_module.update_workspace_settings(db, code_intel_enabled_default=0)
    assert (await db_module.get_workspace_settings(db))["code_intel_enabled_default"] is False


# ---------------------------------------------------------------------------
# create_project cascade-at-creation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_project_seeds_execution_mode_from_workspace(db):
    await db_module.update_workspace_settings(
        db, execution_mode_default="interactive", tenant_id="t1",
    )
    proj = await db_module.create_project(db, "seeded", tenant_id="t1")
    settings = await db_module.get_project_settings(db, proj["id"])
    assert settings["execution_mode"] == "interactive"


@pytest.mark.asyncio
async def test_create_project_seeds_hitl_and_code_intel(db):
    await db_module.update_workspace_settings(
        db, hitl_auto_answer_default=True, code_intel_enabled_default=True, tenant_id="t1",
    )
    proj = await db_module.create_project(db, "seeded2", tenant_id="t1")
    settings = await db_module.get_project_settings(db, proj["id"])
    assert settings["hitl_auto_answer"] == 1          # workspace ON → mode 1 (safe)
    assert settings["code_intel_enabled"] == 1


@pytest.mark.asyncio
async def test_create_project_explicit_execution_mode_not_overridden(db):
    await db_module.update_workspace_settings(
        db, execution_mode_default="interactive", tenant_id="t1",
    )
    # Caller passes an explicit mode → wins over the workspace default.
    proj = await db_module.create_project(
        db, "explicit", execution_mode="autonomous", tenant_id="t1",
    )
    settings = await db_module.get_project_settings(db, proj["id"])
    assert settings["execution_mode"] == "autonomous"


@pytest.mark.asyncio
async def test_create_project_no_tenant_no_seeding(db):
    # A workspace default exists for t1, but creating without a tenant_id must
    # NOT seed (self-host / unauthenticated path).
    await db_module.update_workspace_settings(
        db, execution_mode_default="interactive", code_intel_enabled_default=True,
        tenant_id="t1",
    )
    proj = await db_module.create_project(db, "no-tenant")
    settings = await db_module.get_project_settings(db, proj["id"])
    assert settings["execution_mode"] == "autonomous"   # built-in default
    assert settings["code_intel_enabled"] == 0


@pytest.mark.asyncio
async def test_create_project_no_workspace_default_uses_builtin(db):
    # tenant_id given but no workspace defaults set → built-in defaults.
    proj = await db_module.create_project(db, "plain", tenant_id="t1")
    settings = await db_module.get_project_settings(db, proj["id"])
    assert settings["execution_mode"] == "autonomous"
    assert settings["code_intel_enabled"] == 0
    assert settings["hitl_auto_answer"] == 0
