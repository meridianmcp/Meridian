"""Tests for 36fea6ca — inline RESOLVED sprint-item pointers in the handoff.

generate_handoff improvement (2): each pending sprint item's DURABLE pointers
(the ones persisted in sprint_item_pointers via add_sprint_item_pointer) are
resolved and rendered inline in the handoff's plain-text markdown — not just
stored in the DB requiring a separate resolve_sprint_item_pointers call. Default
ON, gated by workspace_settings.handoff_inline_pointers (default True).

A ``range`` selector is used for the end-to-end cases because it is self-resolving
(the pointer IS the location) and needs no code graph / tunnel / network, so the
test is deterministic on both SQLite and Postgres.
"""

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


# ---------------------------------------------------------------------------
# workspace_settings.handoff_inline_pointers — flag end-to-end (DB layer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inline_pointers_default_true(db):
    """The flag defaults to True with no workspace_settings row written."""
    settings = await db_module.get_workspace_settings(db)
    assert settings["handoff_inline_pointers"] is True


@pytest.mark.asyncio
async def test_inline_pointers_flag_roundtrip(db):
    """handoff_inline_pointers can be toggled off and back on, persisting."""
    ws = await db_module.update_workspace_settings(db, handoff_inline_pointers=False)
    assert ws["handoff_inline_pointers"] is False
    ws2 = await db_module.get_workspace_settings(db)
    assert ws2["handoff_inline_pointers"] is False

    ws3 = await db_module.update_workspace_settings(db, handoff_inline_pointers=True)
    assert ws3["handoff_inline_pointers"] is True


@pytest.mark.asyncio
async def test_inline_pointers_flag_string_falsey(db):
    """A "0"/"false" string turns the flag off (mirrors the sibling toggles)."""
    ws = await db_module.update_workspace_settings(db, handoff_inline_pointers="0")
    assert ws["handoff_inline_pointers"] is False


# ---------------------------------------------------------------------------
# pure renderer helpers
# ---------------------------------------------------------------------------


def test_format_resolved_symbol_target():
    line = handoff_module._format_resolved_pointer_target(
        {
            "resolved": True,
            "selector_type": "symbol",
            "uri": "file:meridian/handoff.py",
            "qualified_name": "handoff.generate_handoff",
            "file": "meridian/handoff.py",
        }
    )
    assert "`handoff.generate_handoff`" in line
    assert "meridian/handoff.py" in line


def test_format_resolved_range_target():
    line = handoff_module._format_resolved_pointer_target(
        {
            "resolved": True,
            "selector_type": "range",
            "uri": "file:meridian/db/__init__.py",
            "range": {"start_line": 10, "end_line": 20},
        }
    )
    assert "meridian/db/__init__.py" in line
    assert ":10-20" in line


def test_format_unresolved_symbol_target_marks_it():
    line = handoff_module._format_resolved_pointer_target(
        {
            "resolved": False,
            "selector_type": "symbol",
            "uri": "file:x.py",
            "qualified_name": "x.missing",
            "reason": "no matching symbol in graph snapshot",
        }
    )
    assert "unresolved" in line


def test_format_resolved_pointer_flattens_targets():
    out = handoff_module._format_resolved_pointer(
        {
            "source_type": "code",
            "label": "the fix site",
            "targets": [
                {
                    "resolved": True,
                    "selector_type": "range",
                    "uri": "file:a.py",
                    "range": {"start_line": 1, "end_line": 2},
                },
            ],
        }
    )
    assert out["source_type"] == "code"
    assert out["label"] == "the fix site"
    assert out["targets"] == ["file:a.py:1-2"]


def test_format_resolved_pointer_empty_targets_returns_none():
    assert handoff_module._format_resolved_pointer({"targets": []}) is None
    assert handoff_module._format_resolved_pointer(None) is None


# ---------------------------------------------------------------------------
# _annotate_resolved_pointers — DB-backed, guarded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_annotate_resolved_pointers_attaches_range(db):
    p = await db_module.create_project(db, "inline-annotate")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Patch the parser")
    await db_module.add_sprint_item_pointer(
        db,
        p["id"],
        item["id"],
        "code",
        [
            {
                "uri": "file:meridian/parser.py",
                "selector": {"type": "range", "start_line": 42, "end_line": 58},
            }
        ],
        label="parser entry",
    )

    items = [{"id": item["id"], "title": "Patch the parser"}]
    out = await handoff_module._annotate_resolved_pointers(db, p["id"], items)
    rp = out[0]["resolved_pointers"]
    assert rp
    assert rp[0]["label"] == "parser entry"
    assert "meridian/parser.py:42-58" in rp[0]["targets"][0]


@pytest.mark.asyncio
async def test_annotate_resolved_pointers_no_pointers_left_untouched(db):
    p = await db_module.create_project(db, "inline-none")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "No pointers here")
    items = [{"id": item["id"], "title": "No pointers here"}]
    out = await handoff_module._annotate_resolved_pointers(db, p["id"], items)
    assert "resolved_pointers" not in out[0]


# ---------------------------------------------------------------------------
# end-to-end via generate_handoff + template rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_handoff_renders_resolved_pointers_inline(db, tmp_path):
    p = await db_module.create_project(db, "inline-e2e")
    await db_module.set_goal(db, p["id"], "ship inline pointers")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Fix the migration guard")
    await db_module.add_sprint_item_pointer(
        db,
        p["id"],
        item["id"],
        "code",
        [
            {
                "uri": "file:meridian/db/migrations.py",
                "selector": {"type": "range", "start_line": 100, "end_line": 120},
            }
        ],
        label="guard site",
    )

    _, content = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert "Resolved pointers:" in content
    assert "meridian/db/migrations.py:100-120" in content
    assert "guard site" in content


@pytest.mark.asyncio
async def test_generate_handoff_omits_resolved_pointers_when_flag_off(db, tmp_path):
    p = await db_module.create_project(db, "inline-e2e-off")
    await db_module.set_goal(db, p["id"], "ship inline pointers")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Fix the migration guard")
    await db_module.add_sprint_item_pointer(
        db,
        p["id"],
        item["id"],
        "code",
        [
            {
                "uri": "file:meridian/db/migrations.py",
                "selector": {"type": "range", "start_line": 100, "end_line": 120},
            }
        ],
    )
    await db_module.update_workspace_settings(db, handoff_inline_pointers=False)

    _, content = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert "Resolved pointers:" not in content
    assert "meridian/db/migrations.py:100-120" not in content


@pytest.mark.asyncio
async def test_generate_handoff_survives_pointer_resolve_blowup(db, tmp_path, monkeypatch):
    """A resolve failure degrades to no inline pointers — never breaks the handoff."""
    p = await db_module.create_project(db, "inline-blowup")
    await db_module.set_goal(db, p["id"], "resilience")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "resilient item")
    await db_module.add_sprint_item_pointer(
        db,
        p["id"],
        item["id"],
        "code",
        [
            {
                "uri": "file:x.py",
                "selector": {"type": "range", "start_line": 1, "end_line": 2},
            }
        ],
    )

    async def _boom(*_a, **_k):
        raise RuntimeError("pointer fetch exploded")

    monkeypatch.setattr(db_module, "get_sprint_item_pointers", _boom)

    # Must not raise, and must still produce a handoff.
    _, content = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert "resilient item" in content
    assert "Resolved pointers:" not in content
