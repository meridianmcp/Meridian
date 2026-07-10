"""Item 22c274bd — workspace-scope leak heuristic.

Workspace notes/decisions are meant to be tenant-global (cross-project), but in
practice they accumulate PROJECT-specific content: a thesis post-mortem, one
project's CI patterns, a personal absolute filesystem path, a commit sha. The
``add_workspace_note`` / ``pin_workspace_decision`` handlers now attach a *soft*
``scope_warning`` when the content looks project-specific — the write always
proceeds, the caller is merely nudged.

These are unit-level tests: the pure heuristic (:func:`_workspace_scope_warning`)
in isolation, plus an end-to-end pass through the real dispatcher on an in-memory
SQLite DB to prove the warning surfaces on the tool result without blocking the
write.
"""

from __future__ import annotations

import pytest

import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
from meridian.mcp.handler import _workspace_scope_warning


# ---------------------------------------------------------------------------
# Pure heuristic — project-specific content trips the warning
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "title, body",
    [
        # Absolute filesystem paths (personal / machine-local).
        ("CI note", "Build output lands in /home/adam/build/artifacts.log"),
        ("path", r"Config lives at C:\Users\13144\Documents\Meridian\meridian.toml"),
        ("posix", "See /Users/adam/repo/src/main.py for the entrypoint."),
        ("tmp", "Scratch data written to /tmp/meridian-cache/run.json"),
        ("home-rel", "Drop the token in ~/.config/meridian/creds"),
        # Commit shas.
        ("regression", "Introduced by commit 3f9a1c2 — reverted next day."),
        ("long sha", "Fixed in a1b2c3d4e5f60718293a4b5c6d7e8f9012345678."),
        # Project-anaphora phrasing.
        ("retro", "The thesis post-mortem: chapter 3 slipped by two weeks."),
        ("scope", "This project uses a bespoke migration runner."),
        ("repo", "Our project's CI runs pytest under a 60s timeout."),
        ("codebase", "This codebase pins psycopg3, never asyncpg."),
    ],
)
def test_project_specific_content_warns(title, body):
    warning = _workspace_scope_warning(title, body)
    assert warning is not None
    assert "project-specific" in warning.lower()
    # It's a soft nudge, not a block — must say the write still happened.
    assert "saved anyway" in warning.lower()


def test_warning_names_the_detected_signal():
    w = _workspace_scope_warning("x", "/home/adam/notes.md and commit 9f8e7d6")
    assert w is not None
    assert "absolute filesystem path" in w
    assert "commit sha" in w


# ---------------------------------------------------------------------------
# Pure heuristic — genuinely tenant-global content stays clean
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "title, body",
    [
        ("Convention", "Prefer psycopg3 over asyncpg across all Meridian services."),
        ("Style", "Use %s placeholders in SQL; the adapter converts ? automatically."),
        ("Team norm", "Every feature ships with at least one test."),
        ("Release", "Tag releases as vX.Y.Z; CI publishes to PyPI and npm."),
        ("Ports", "Local dev serves the dashboard on 8080 by default."),
        # Pure-word hex ('deface', 'facade') and pure-digit runs (a year, a port)
        # must NOT be misread as a commit sha.
        ("year", "The 2026 roadmap emphasizes cross-project memory."),
        ("word-hex", "Never deface the audit log; append only."),
        ("digits", "Retry with exponential backoff up to 30000 ms."),
        ("empty-ish", "   "),
    ],
)
def test_global_content_no_warning(title, body):
    assert _workspace_scope_warning(title, body) is None


def test_none_inputs_no_warning():
    assert _workspace_scope_warning(None, None) is None
    assert _workspace_scope_warning("", "") is None


# ---------------------------------------------------------------------------
# End-to-end through the real dispatcher — warning surfaces, write still lands
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_workspace_note_project_specific_warns_but_writes(db):
    from meridian import server as srv
    from meridian import db as db_module

    result = await srv._dispatch_mcp_tool(
        "add_workspace_note",
        {
            "title": "thesis retro",
            "body": "The thesis post-mortem lives at /home/adam/thesis/notes.md",
        },
        db,
        "/tmp",
    )
    # Soft nudge present.
    assert "scope_warning" in result
    assert "project-specific" in result["scope_warning"].lower()
    # ...and the note was actually persisted (write not blocked).
    assert result.get("id")
    notes = await db_module.get_workspace_notes(db)
    assert any(n["id"] == result["id"] for n in notes)


@pytest.mark.asyncio
async def test_add_workspace_note_global_has_no_warning(db):
    from meridian import server as srv

    result = await srv._dispatch_mcp_tool(
        "add_workspace_note",
        {"title": "SQL convention", "body": "Use %s placeholders, never asyncpg."},
        db,
        "/tmp",
    )
    assert "scope_warning" not in result
    assert result.get("id")


@pytest.mark.asyncio
async def test_pin_workspace_decision_project_specific_warns_but_writes(db):
    from meridian import server as srv
    from meridian import db as db_module

    result = await srv._dispatch_mcp_tool(
        "pin_workspace_decision",
        {
            "title": "CI timeout",
            "body": "This project's CI pins pytest to a 60s timeout (commit 3f9a1c2).",
            "category": "TECHNICAL",
        },
        db,
        "/tmp",
    )
    assert "scope_warning" in result
    assert "saved anyway" in result["scope_warning"].lower()
    assert result.get("id")
    decisions = await db_module.get_workspace_decisions(db)
    assert any(d["id"] == result["id"] for d in decisions)


@pytest.mark.asyncio
async def test_pin_workspace_decision_global_has_no_warning(db):
    from meridian import server as srv

    result = await srv._dispatch_mcp_tool(
        "pin_workspace_decision",
        {
            "title": "Adapter choice",
            "body": "Standardize on psycopg3 for all Postgres access, workspace-wide.",
            "category": "TECHNICAL",
        },
        db,
        "/tmp",
    )
    assert "scope_warning" not in result
    assert result.get("id")
