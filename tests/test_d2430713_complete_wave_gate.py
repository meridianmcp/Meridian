"""d2430713 — complete_wave_gate MCP tool tests.

Coverage:
  1. Gate rejected without any verification_payload (missing evidence).
  2. Gate rejected with fabricated/self-report payload (status!='ok').
  3. Gate rejected with non-zero exit_code (tests failed).
  4. Gate rejected with status='not_configured'.
  5. Gate rejected with status='not_connected'.
  6. Gate accepted with a genuine passing run_verification result.
  7. After gate completes, next-wave items are reported as unblocked.
  8. Duplicate gate completion is rejected.
  9. MCP tool schema is registered correctly.
 10. MCP tool dispatches via _dispatch_mcp_tool.
 11. project_name resolves to project_id correctly.
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import server as srv
from meridian.mcp_tools import (
    _MCP_TOOLS_LIST,
    _READ_ONLY_TOOLS,
    _TITLE_OVERRIDES,
    _TOOL_EXAMPLES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _project(db, name: str = "wave-gate-test"):
    proj = await srv._dispatch_mcp_tool(
        "create_project", {"name": name}, db, "/tmp"
    )
    return proj["id"]


_GOOD_PAYLOAD = {
    "status": "ok",
    "exit_code": 0,
    "passed": 42,
    "failed": 0,
    "stdout_tail": "42 passed in 5.3s",
    "stderr_tail": "",
}


# ---------------------------------------------------------------------------
# 1. Missing verification_payload
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_rejected_no_payload(db):
    pid = await _project(db, "gate-no-payload")
    result = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1"},
        db, "/tmp",
    )
    assert "error" in result
    assert "verification_payload" in result["error"].lower()


# ---------------------------------------------------------------------------
# 2. Self-report / bad status rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_rejected_bad_status(db):
    pid = await _project(db, "gate-bad-status")
    # An executor self-reporting success without running anything.
    bad_payload = {"status": "ok", "exit_code": 0, "passed": None, "failed": None}
    # Even with exit_code=0, passed=None is technically allowed — what matters
    # is a non-ok status is rejected.
    bad_payload2 = {"status": "self_reported", "exit_code": 0, "passed": 0, "failed": 0}
    result = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1",
         "verification_payload": bad_payload2},
        db, "/tmp",
    )
    assert "error" in result
    assert "self_reported" in result["error"] or "status" in result["error"]


# ---------------------------------------------------------------------------
# 3. Non-zero exit_code rejected (tests failed)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_rejected_nonzero_exit(db):
    pid = await _project(db, "gate-failed-tests")
    failing_payload = {
        "status": "ok",
        "exit_code": 1,
        "passed": 10,
        "failed": 3,
        "stdout_tail": "10 passed, 3 failed in 2s",
        "stderr_tail": "FAILED test_foo.py",
    }
    result = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1",
         "verification_payload": failing_payload},
        db, "/tmp",
    )
    assert "error" in result
    assert "exit_code" in result["error"]


# ---------------------------------------------------------------------------
# 4. status='not_configured' rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_rejected_not_configured(db):
    pid = await _project(db, "gate-not-configured")
    not_configured = {
        "status": "not_configured",
        "exit_code": None,
        "passed": None,
        "failed": None,
        "stdout_tail": "",
        "stderr_tail": "",
    }
    result = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1",
         "verification_payload": not_configured},
        db, "/tmp",
    )
    assert "error" in result
    assert "not_configured" in result["error"].lower()


# ---------------------------------------------------------------------------
# 5. status='not_connected' rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_rejected_not_connected(db):
    pid = await _project(db, "gate-not-connected")
    not_connected = {
        "status": "not_connected",
        "exit_code": None,
        "passed": None,
        "failed": None,
        "stdout_tail": "",
        "stderr_tail": "",
    }
    result = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1",
         "verification_payload": not_connected},
        db, "/tmp",
    )
    assert "error" in result
    assert "not_connected" in result["error"].lower()


# ---------------------------------------------------------------------------
# 6. Gate accepted with genuine passing payload
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_accepted_with_passing_payload(db):
    pid = await _project(db, "gate-accepted")
    result = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1",
         "verification_payload": _GOOD_PAYLOAD},
        db, "/tmp",
    )
    assert result.get("gate_completed") is True
    assert result.get("wave_label") == "wave-1"
    assert result.get("next_wave_label") == "wave-2"
    assert "gate_id" in result
    assert result["gate_id"]  # non-empty UUID


# ---------------------------------------------------------------------------
# 7. Next-wave items are reported after gate completes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_reports_next_wave_items(db):
    pid = await _project(db, "gate-next-wave-items")
    # Add two items in wave-2 (the next wave after wave-1).
    item_a = await db_module.add_sprint_item(
        db, pid, "v1", "next-wave item A", touches_resources=["file:foo.py"]
    )
    item_b = await db_module.add_sprint_item(
        db, pid, "v1", "next-wave item B", touches_resources=["file:bar.py"],
        force=True,
    )
    # Manually assign them to wave-2 (normally assign_sprint_waves does this).
    await db_module.patch_sprint_item(db, pid, item_a["id"], wave="wave-2")
    await db_module.patch_sprint_item(db, pid, item_b["id"], wave="wave-2")

    result = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1",
         "verification_payload": _GOOD_PAYLOAD},
        db, "/tmp",
    )
    assert result.get("gate_completed") is True
    assert result.get("next_wave_label") == "wave-2"
    assert result.get("next_wave_item_count") == 2
    assert set(result.get("next_wave_item_ids", [])) == {item_a["id"], item_b["id"]}


# ---------------------------------------------------------------------------
# 8. Duplicate gate completion rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_duplicate_rejected(db):
    pid = await _project(db, "gate-duplicate")
    # First call should succeed.
    first = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1",
         "verification_payload": _GOOD_PAYLOAD},
        db, "/tmp",
    )
    assert first.get("gate_completed") is True

    # Second call with same wave_label on same project should fail.
    second = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1",
         "verification_payload": _GOOD_PAYLOAD},
        db, "/tmp",
    )
    assert "error" in second
    assert "already been completed" in second["error"] or "already" in second["error"]


# ---------------------------------------------------------------------------
# 9. MCP tool schema is registered correctly
# ---------------------------------------------------------------------------

def test_complete_wave_gate_registered_in_tools_list():
    by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    assert "complete_wave_gate" in by_name, "complete_wave_gate not in _MCP_TOOLS_LIST"
    tool = by_name["complete_wave_gate"]
    schema = tool["inputSchema"]
    props = schema["properties"]
    assert "project_id" in props
    assert "wave_label" in props
    assert "verification_payload" in props
    assert "actor" in props
    # wave_label and verification_payload are required
    assert "wave_label" in schema.get("required", [])
    assert "verification_payload" in schema.get("required", [])
    # Not read-only (it writes a gate result).
    assert "complete_wave_gate" not in _READ_ONLY_TOOLS
    # Has a title override.
    assert _TITLE_OVERRIDES.get("complete_wave_gate") == "Complete Wave Gate"
    assert tool.get("title") == "Complete Wave Gate"
    # Has an example.
    assert "complete_wave_gate" in _TOOL_EXAMPLES


# ---------------------------------------------------------------------------
# 10. Dispatches through _dispatch_mcp_tool without error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_wave_gate_dispatch_works(db):
    """Verifies the MCP dispatch table includes complete_wave_gate."""
    pid = await _project(db, "gate-dispatch")
    # A call with missing project_id should return an error dict, not raise.
    result = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"wave_label": "wave-1", "verification_payload": _GOOD_PAYLOAD},
        db, "/tmp",
    )
    # Should return an error about missing project_id, not _MISS sentinel.
    assert isinstance(result, dict)
    assert "error" in result

    # ed8e4524 — an explicit version arg dispatches through and is echoed
    # back on the result, confirming the MCP layer threads it to the DB layer.
    versioned = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_id": pid, "wave_label": "wave-1",
         "verification_payload": _GOOD_PAYLOAD, "version": "v-dispatch"},
        db, "/tmp",
    )
    assert versioned.get("gate_completed") is True
    assert versioned.get("version") == "v-dispatch"


# ---------------------------------------------------------------------------
# 11. project_name resolves to project_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_wave_gate_project_name_resolution(db):
    pid = await _project(db, "gate-by-name")
    result = await srv._dispatch_mcp_tool(
        "complete_wave_gate",
        {"project_name": "gate-by-name", "wave_label": "wave-1",
         "verification_payload": _GOOD_PAYLOAD},
        db, "/tmp",
    )
    assert result.get("gate_completed") is True
    assert result.get("wave_label") == "wave-1"
    # ed8e4524 — no version passed -> stays unscoped (legacy behavior).
    assert result.get("version") is None


# ---------------------------------------------------------------------------
# 12. Direct DB function test: complete_wave_gate validates payload
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_db_complete_wave_gate_rejects_non_dict(db):
    pid = await db_module.create_project(db, name="gate-db-direct")
    pid = pid["id"]
    with pytest.raises(ValueError, match="verification_payload dict"):
        await db_module.complete_wave_gate(db, pid, "wave-1", True)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_db_complete_wave_gate_rejects_error_status(db):
    pid = (await db_module.create_project(db, name="gate-db-error-status"))["id"]
    with pytest.raises(ValueError, match="status='error'"):
        await db_module.complete_wave_gate(
            db, pid, "wave-1",
            {"status": "error", "exit_code": None, "passed": None, "failed": None},
        )


@pytest.mark.asyncio
async def test_db_complete_wave_gate_succeeds(db):
    pid = (await db_module.create_project(db, name="gate-db-success"))["id"]
    result = await db_module.complete_wave_gate(db, pid, "wave-1", _GOOD_PAYLOAD)
    assert result["gate_completed"] is True
    assert result["wave_label"] == "wave-1"
    assert result["next_wave_label"] == "wave-2"
    assert result["gate_id"]
    # ed8e4524 — an unscoped call (no version passed) stays unscoped: the
    # legacy/project-wide behavior for a project with one sprint version.
    assert result["version"] is None


# ---------------------------------------------------------------------------
# 13. ed8e4524 — sprint-version scoping regression tests.
#
# Confirmed defect: complete_wave_gate's next-wave query (and
# configure_wave_gate's immutability/upsert checks, and claim_sprint_item's
# structural wave-gate block via _get_blocking_wave_gate) were scoped by
# project_id + wave label ONLY. Two different sprint versions that happen to
# reuse the same wave label (e.g. both have a 'wave-2') could leak or
# satisfy each other's gate. These tests prove: (a) two REAL, DIFFERENT
# versions never cross-contaminate gate configuration, completion, next-wave
# readiness, or claim-unblocking; (b) a project that never explicitly
# version-scopes its wave-gate calls (the common/legacy case — including one
# whose items still carry an ordinary version string like 'v1') keeps
# behaving exactly as it did before this fix.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_two_versions_same_wave_label_do_not_cross_contaminate(db):
    pid = (await db_module.create_project(db, name="two-version-wave-gate"))["id"]

    # Configure an INDEPENDENT 'wave-1' gate boundary for each version.
    cfg_a = await db_module.configure_wave_gate(
        db, pid, "wave-1", [{"type": "run_verification"}], version="vA",
    )
    cfg_b = await db_module.configure_wave_gate(
        db, pid, "wave-1", [{"type": "run_verification"}], version="vB",
    )
    assert cfg_a["gate_config_id"] != cfg_b["gate_config_id"]
    assert cfg_a["version"] == "vA"
    assert cfg_b["version"] == "vB"

    # Each version gets its own wave-2 item.
    item_a = await db_module.add_sprint_item(db, pid, "vA", "vA wave-2 item")
    item_b = await db_module.add_sprint_item(
        db, pid, "vB", "vB wave-2 item", force=True,
    )
    await db_module.patch_sprint_item(db, pid, item_a["id"], wave="wave-2")
    await db_module.patch_sprint_item(db, pid, item_b["id"], wave="wave-2")

    # CLAIM-UNBLOCKING: both start out blocked by their OWN unpassed gate.
    blocked_a = await db_module.claim_sprint_item(db, pid, item_a["id"])
    assert blocked_a.get("blocked") is True
    assert blocked_a.get("error") == "WAVE_GATE_PENDING"
    blocked_b = await db_module.claim_sprint_item(db, pid, item_b["id"])
    assert blocked_b.get("blocked") is True
    assert blocked_b.get("error") == "WAVE_GATE_PENDING"

    # GATE COMPLETION + next-wave READINESS: complete version A's gate only.
    result_a = await db_module.complete_wave_gate(
        db, pid, "wave-1", _GOOD_PAYLOAD, version="vA",
    )
    assert result_a["gate_completed"] is True
    assert result_a["version"] == "vA"
    # Only vA's item is reported ready — vB's item must NOT leak in.
    assert result_a["next_wave_item_ids"] == [item_a["id"]]
    assert item_b["id"] not in result_a["next_wave_item_ids"]

    # CLAIM-UNBLOCKING: vA's item is now claimable...
    claimed_a = await db_module.claim_sprint_item(db, pid, item_a["id"])
    assert claimed_a.get("status") == "in_progress"
    # ...but vB's item is STILL blocked — vA's completion must not leak
    # across versions and wrongly unblock it.
    still_blocked_b = await db_module.claim_sprint_item(db, pid, item_b["id"])
    assert still_blocked_b.get("blocked") is True
    assert still_blocked_b.get("error") == "WAVE_GATE_PENDING"

    # GATE COMPLETION: version B can still independently complete ITS OWN
    # wave-1 gate — it must not have been silently "used up" by version A's
    # completion of the same wave_label (the exact bug: a shared
    # UNIQUE(project_id, wave_label) row would have rejected this as a dup).
    result_b = await db_module.complete_wave_gate(
        db, pid, "wave-1", _GOOD_PAYLOAD, version="vB",
    )
    assert result_b["gate_completed"] is True
    assert result_b["version"] == "vB"
    assert result_b["next_wave_item_ids"] == [item_b["id"]]

    claimed_b = await db_module.claim_sprint_item(db, pid, item_b["id"])
    assert claimed_b.get("status") == "in_progress"


@pytest.mark.asyncio
async def test_unversioned_legacy_wave_gate_still_project_wide(db):
    """A project that never explicitly version-scopes its wave-gate calls —
    still the common/legacy case per AGENTS.md — must behave EXACTLY as
    before ed8e4524: one project-wide gate per wave label, blocking/
    unblocking every item in that wave regardless of the item's own version
    field (items commonly carry an ordinary version string like 'v1' even
    when the project isn't using multi-version scoping)."""
    pid = (await db_module.create_project(db, name="legacy-unversioned-wave-gate"))["id"]
    await db_module.configure_wave_gate(
        db, pid, "wave-1", [{"type": "run_verification"}],
    )
    item = await db_module.add_sprint_item(db, pid, "v1", "legacy wave-2 item")
    await db_module.patch_sprint_item(db, pid, item["id"], wave="wave-2")

    blocked = await db_module.claim_sprint_item(db, pid, item["id"])
    assert blocked.get("blocked") is True
    assert blocked.get("error") == "WAVE_GATE_PENDING"

    result = await db_module.complete_wave_gate(db, pid, "wave-1", _GOOD_PAYLOAD)
    assert result["gate_completed"] is True
    assert result["version"] is None
    assert result["next_wave_item_ids"] == [item["id"]]

    claimed = await db_module.claim_sprint_item(db, pid, item["id"])
    assert claimed.get("status") == "in_progress"


# ---------------------------------------------------------------------------
# 12. Residual-constraint gap on a table that predates ed8e4524 (live bug)
# ---------------------------------------------------------------------------
#
# Confirmed live on a hosted project: a version-scoped complete_wave_gate()
# call for a wave_label that already has an UNSCOPED (version IS NULL)
# result row fails with a raw
#   duplicate key value violates unique constraint
#   "wave_gate_results_project_id_wave_label_key"
# The test fixture's `db` always gets a FRESH wave_gate_results table (the
# CREATE TABLE branch already has the correct 3-column constraint), so the
# tests above never exercise the actual bug: a table that existed BEFORE
# ed8e4524 shipped, where only the additive `ADD COLUMN version` half of
# that migration ran and the OLD 2-column UNIQUE(project_id, wave_label)
# stuck around. This rebuilds the fixture's table down to that pre-fix
# shape, then proves _migrate_wave_gate_results_version_unique /
# _migrate_wave_gate_configs_version_unique repair it in place.

async def _downgrade_wave_gate_tables_to_pre_ed8e4524(db):
    """Rebuild wave_gate_results/wave_gate_configs with the OLD 2-column
    UNIQUE constraint, simulating a table that predates ed8e4524's version
    column (that migration only ever ADD COLUMN'd version onto tables in
    this shape — it never widened the constraint).

    Backend-portable: ``datetime('now')`` is SQLite-only (invalid syntax on
    Postgres — no such function), which is one of the two things that broke
    this helper under ``TEST_DATABASE_URL``. Mirrors the
    ``"now()" if hasattr(db, "_pool") else "datetime('now')"``
    backend-discrimination convention already used throughout
    meridian/db/__init__.py and meridian/handoff.py."""
    now_expr = "now()" if hasattr(db, "_pool") else "datetime('now')"
    await db.executescript(
        f"""
        DROP TABLE IF EXISTS wave_gate_results;
        CREATE TABLE wave_gate_results (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            wave_label TEXT NOT NULL,
            version TEXT,
            gate_passed INTEGER NOT NULL DEFAULT 1,
            exit_code INTEGER,
            passed_count INTEGER,
            failed_count INTEGER,
            verification_status TEXT,
            evidence_snapshot TEXT,
            actor TEXT,
            completed_at TEXT NOT NULL DEFAULT ({now_expr}),
            UNIQUE(project_id, wave_label)
        );
        DROP TABLE IF EXISTS wave_gate_configs;
        CREATE TABLE wave_gate_configs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            wave_start TEXT NOT NULL,
            wave_end TEXT NOT NULL,
            version TEXT,
            actions TEXT NOT NULL,
            actor TEXT,
            created_at TEXT NOT NULL DEFAULT ({now_expr}),
            updated_at TEXT NOT NULL DEFAULT ({now_expr}),
            UNIQUE(project_id, wave_end)
        );
        """
    )


def _integrity_error_types() -> tuple[type[BaseException], ...]:
    """Backend-portable exception types for a UNIQUE/duplicate-key violation.

    ``sqlite3.IntegrityError`` on the SQLite backend; psycopg3's
    ``IntegrityError`` (the base class of ``psycopg.errors.UniqueViolation``,
    confirmed via ``psycopg.errors.UniqueViolation.__mro__``) on Postgres.
    Mirrors the same ``hasattr(db, "_pool")`` backend-discrimination
    convention used throughout meridian/db/__init__.py — asserting against
    ``sqlite3.IntegrityError`` alone (the original code) only ever passes on
    SQLite; the real bug this test reproduces was confirmed live on hosted
    Postgres in the first place (see the module-level comment above), so the
    Postgres exception type has to be recognized too."""
    import sqlite3

    types: list[type[BaseException]] = [sqlite3.IntegrityError]
    try:
        import psycopg
    except ImportError:  # pragma: no cover - psycopg is a hard dependency
        pass
    else:
        types.append(psycopg.IntegrityError)
    return tuple(types)


@pytest.mark.asyncio
async def test_pre_existing_table_blocks_multi_version_gate_before_migration(db):
    """Reproduces the live bug directly: on the OLD 2-column-constraint
    schema, a second version's legitimate complete_wave_gate() for the
    SAME wave_label raises — proving the failure mode is real, not just
    theoretical, before asserting the repair migration fixes it below."""
    pid = (await db_module.create_project(db, name="pre-ed8e4524-wave-gate"))["id"]
    await _downgrade_wave_gate_tables_to_pre_ed8e4524(db)

    result_a = await db_module.complete_wave_gate(
        db, pid, "wave-1", _GOOD_PAYLOAD, version="vA",
    )
    assert result_a["gate_completed"] is True

    with pytest.raises(_integrity_error_types()):
        await db_module.complete_wave_gate(
            db, pid, "wave-1", _GOOD_PAYLOAD, version="vB",
        )


@pytest.mark.asyncio
async def test_version_unique_migration_repairs_pre_existing_table(db):
    """The actual fix: _migrate_wave_gate_results_version_unique /
    _migrate_wave_gate_configs_version_unique (SQLite) or
    _migrate_pg_wave_gate_version_unique_constraints (Postgres) rebuild a
    pre-ed8e4524 table in place, preserving existing rows, so a second
    version's gate for the same wave_label can complete afterward without
    error.

    Backend-dispatched: SQLite and Postgres ship SEPARATE implementations
    (meridian.db.migrations vs. meridian.pg_adapter), invoked by init_db /
    init_pg_db respectively — meridian.db.migrations._migrate_wave_gate_
    *_version_unique are never called against a real Postgres connection in
    production (confirmed: init_db, the ONLY caller, builds an
    aiosqlite.Connection). Calling the SQLite-only functions directly
    against this test's `db` fixture when it's actually a PostgresConnection
    (TEST_DATABASE_URL set) was the root cause of the original CI failure:
    their executescript() body contains a literal ``BEGIN;`` / ``COMMIT;``
    SQLite transaction-script pair AND ``datetime('now')`` (invalid Postgres
    syntax). PostgresConnection.executescript() already opens its own
    ``conn.transaction()`` (a SAVEPOINT, since the test fixture's connection
    is already inside an outer per-test transaction) around every script it
    runs — executing a literal ``COMMIT`` statement inside that block closes
    the real underlying transaction out from under psycopg3's own
    transaction-tracking, so when that ``conn.transaction()`` context
    manager then tries to exit (releasing what it believes is still an open
    SAVEPOINT), Postgres raises exactly the "savepoint release outside a
    transaction" class of error seen in CI. Dispatching to the real
    Postgres-side migration (already wired into init_pg_db's own migration
    list) avoids all of that and exercises the ACTUAL code path a live
    Postgres project runs."""
    pid = (await db_module.create_project(db, name="repaired-wave-gate"))["id"]
    await _downgrade_wave_gate_tables_to_pre_ed8e4524(db)

    # Existing (pre-migration) data that must survive the rebuild.
    result_a = await db_module.complete_wave_gate(
        db, pid, "wave-1", _GOOD_PAYLOAD, version="vA",
    )
    assert result_a["gate_completed"] is True
    cfg = await db_module.configure_wave_gate(
        db, pid, "wave-2", [{"type": "run_verification"}], version="vA",
    )
    assert cfg["configured"] is True

    # Run the backend-appropriate repair migration(s) (idempotent — safe to
    # call on any shape, including twice in a row on an already-correct one).
    if hasattr(db, "_pool"):
        from meridian.pg_adapter import (
            _migrate_pg_wave_gate_version_unique_constraints,
        )

        await _migrate_pg_wave_gate_version_unique_constraints(db)
        # Calling twice must be a true no-op (already-correct schema).
        await _migrate_pg_wave_gate_version_unique_constraints(db)
    else:
        from meridian.db.migrations import (
            _migrate_wave_gate_configs_version_unique,
            _migrate_wave_gate_results_version_unique,
        )

        await _migrate_wave_gate_results_version_unique(db)
        await _migrate_wave_gate_configs_version_unique(db)
        # Calling twice must be a true no-op (already-correct schema).
        await _migrate_wave_gate_results_version_unique(db)
        await _migrate_wave_gate_configs_version_unique(db)

    # Pre-existing row survived the rebuild.
    async with db.execute(
        "SELECT version FROM wave_gate_results WHERE project_id = ? AND wave_label = ?",
        (pid, "wave-1"),
    ) as cur:
        row = await cur.fetchone()
    assert (row["version"] if isinstance(row, dict) else row[0]) == "vA"

    # The actual repro: version B can now complete its OWN gate for the
    # SAME wave_label — no more duplicate-key error.
    result_b = await db_module.complete_wave_gate(
        db, pid, "wave-1", _GOOD_PAYLOAD, version="vB",
    )
    assert result_b["gate_completed"] is True
    assert result_b["version"] == "vB"

    # Config side too: a second version's config for the same wave_end.
    cfg_b = await db_module.configure_wave_gate(
        db, pid, "wave-2", [{"type": "run_verification"}], version="vB",
    )
    assert cfg_b["configured"] is True
    assert cfg_b["gate_config_id"] != cfg["gate_config_id"]
