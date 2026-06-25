"""Tests for 91ac0199 — generate_handoff code-pointer enrichment.

When the codebase is indexed and the per-project setting
``enrich_handoffs_with_code_pointers`` (default True) is on, generate_handoff
runs a code-graph search per pending sprint item and injects exact
{file, function, qualified_name} pointers into each item's handoff block.

The code-graph lives in an out-of-process MCP, so enrichment is driven through
an injectable ``graph_searcher`` callable. These tests inject a stub.
"""

from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


# ---------------------------------------------------------------------------
# setting helpers — default True, togglable via executor_config JSON
# ---------------------------------------------------------------------------


def test_setting_defaults_true_when_unset():
    assert handoff_module._code_pointers_enabled(None) is True
    assert handoff_module._code_pointers_enabled({}) is True
    assert handoff_module._code_pointers_enabled({"executor_config": {}}) is True
    assert (
        handoff_module._code_pointers_enabled({"executor_config": None}) is True
    )


def test_setting_respects_explicit_false():
    settings = {"executor_config": {"enrich_handoffs_with_code_pointers": False}}
    assert handoff_module._code_pointers_enabled(settings) is False


def test_setting_respects_explicit_true():
    settings = {"executor_config": {"enrich_handoffs_with_code_pointers": True}}
    assert handoff_module._code_pointers_enabled(settings) is True


def test_setting_stored_in_executor_config_no_migration():
    """The flag rides in the existing executor_config JSON — non-dict configs
    degrade to the default rather than raising."""
    assert handoff_module._code_pointers_enabled(
        {"executor_config": "not-a-dict"}
    ) is True


# ---------------------------------------------------------------------------
# match coercion / pointer normalization
# ---------------------------------------------------------------------------


def test_coerce_match_list_variants():
    assert handoff_module._coerce_match_list(None) == []
    assert handoff_module._coerce_match_list([1, 2]) == [1, 2]
    assert handoff_module._coerce_match_list({"results": [{"a": 1}]}) == [{"a": 1}]
    assert handoff_module._coerce_match_list({"matches": [{"b": 2}]}) == [{"b": 2}]
    # bare match dict becomes a one-element list
    assert handoff_module._coerce_match_list({"file": "x.py"}) == [{"file": "x.py"}]
    assert handoff_module._coerce_match_list("garbage") == []


def test_normalize_code_pointer_field_aliases():
    ptr = handoff_module._normalize_code_pointer(
        {"file_path": "meridian/db/__init__.py", "name": "get_goal"}
    )
    assert ptr == {
        "file": "meridian/db/__init__.py",
        "function": "get_goal",
        "qualified_name": "get_goal",
    }


def test_normalize_code_pointer_prefers_qualified_name():
    ptr = handoff_module._normalize_code_pointer(
        {
            "file": "a.py",
            "function": "foo",
            "qualified_name": "mod.Cls.foo",
        }
    )
    assert ptr["qualified_name"] == "mod.Cls.foo"


def test_normalize_code_pointer_returns_none_when_empty():
    assert handoff_module._normalize_code_pointer({}) is None
    assert handoff_module._normalize_code_pointer("nope") is None


# ---------------------------------------------------------------------------
# enrichment annotation (unit, no DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_annotate_code_pointers_injects_top_match():
    items = [{"id": "i1", "title": "Fix OAuth redirect bug", "status": "pending"}]

    def searcher(query):
        return [
            {
                "file": "meridian/hosted.py",
                "function": "oauth_redirect",
                "qualified_name": "hosted.oauth_redirect",
            },
            {"file": "other.py", "function": "ignored"},
        ]

    out = await handoff_module._annotate_code_pointers(items, searcher)
    assert out[0]["code_pointers"] == [
        {
            "file": "meridian/hosted.py",
            "function": "oauth_redirect",
            "qualified_name": "hosted.oauth_redirect",
        }
    ]


@pytest.mark.asyncio
async def test_annotate_code_pointers_awaits_coroutine_searcher():
    items = [{"id": "i1", "title": "wire pg adapter cursor", "status": "pending"}]

    async def searcher(query):
        return {"results": [{"file": "meridian/pg_adapter.py", "name": "cursor"}]}

    out = await handoff_module._annotate_code_pointers(items, searcher)
    assert out[0]["code_pointers"][0]["file"] == "meridian/pg_adapter.py"


@pytest.mark.asyncio
async def test_annotate_code_pointers_none_searcher_is_noop():
    items = [{"id": "i1", "title": "anything here", "status": "pending"}]
    out = await handoff_module._annotate_code_pointers(items, None)
    assert "code_pointers" not in out[0]


@pytest.mark.asyncio
async def test_annotate_code_pointers_never_raises_on_searcher_error():
    items = [{"id": "i1", "title": "explode please now", "status": "pending"}]

    def searcher(query):
        raise RuntimeError("graph backend down")

    out = await handoff_module._annotate_code_pointers(items, searcher)
    # Degrades to no pointer — must not propagate.
    assert "code_pointers" not in out[0]


@pytest.mark.asyncio
async def test_annotate_code_pointers_no_match_leaves_item_untouched():
    items = [{"id": "i1", "title": "some pending item", "status": "pending"}]
    out = await handoff_module._annotate_code_pointers(items, lambda q: [])
    assert "code_pointers" not in out[0]


# ---------------------------------------------------------------------------
# end-to-end via generate_handoff + template rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_handoff_injects_pointers_when_indexed(db, tmp_path):
    p = await db_module.create_project(db, "enrich-on")
    await db_module.set_goal(db, p["id"], "ship enrichment")
    await db_module.add_sprint_item(db, p["id"], "v1", "Fix OAuth redirect bug")

    captured: list[str] = []

    def searcher(query):
        captured.append(query)
        return [
            {
                "file": "meridian/hosted.py",
                "function": "oauth_redirect",
                "qualified_name": "hosted.oauth_redirect",
            }
        ]

    _, content = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, graph_searcher=searcher
    )
    # The graph was actually queried with keywords from the item title.
    assert captured and "oauth" in captured[0]
    # The template renders a Code pointers section with the qualified name + file.
    assert "Code pointers:" in content
    assert "hosted.oauth_redirect" in content
    assert "meridian/hosted.py" in content


@pytest.mark.asyncio
async def test_generate_handoff_no_pointers_when_setting_off(db, tmp_path):
    p = await db_module.create_project(db, "enrich-off")
    await db_module.set_goal(db, p["id"], "ship enrichment")
    await db_module.add_sprint_item(db, p["id"], "v1", "Fix OAuth redirect bug")
    # Turn the flag off via executor_config (no migration / new column).
    await db_module.update_project_settings(
        db,
        p["id"],
        executor_config={"enrich_handoffs_with_code_pointers": False},
    )

    called = {"n": 0}

    def searcher(query):
        called["n"] += 1
        return [{"file": "x.py", "function": "y", "qualified_name": "y"}]

    _, content = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, graph_searcher=searcher
    )
    # Searcher must not be consulted at all when the setting is off.
    assert called["n"] == 0
    assert "Code pointers:" not in content


@pytest.mark.asyncio
async def test_generate_handoff_no_pointers_when_not_indexed(db, tmp_path):
    """No searcher (unindexed / code intel off) ⇒ graceful, no pointers, no
    crash. This is the default behaviour with no graph_searcher injected."""
    p = await db_module.create_project(db, "enrich-unindexed")
    await db_module.set_goal(db, p["id"], "ship enrichment")
    await db_module.add_sprint_item(db, p["id"], "v1", "Fix OAuth redirect bug")

    _, content = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert "Code pointers:" not in content
    # Item itself still renders normally.
    assert "Fix OAuth redirect bug" in content


@pytest.mark.asyncio
async def test_generate_handoff_survives_searcher_blowup(db, tmp_path):
    """A searcher that raises must never break the mandatory handoff."""
    p = await db_module.create_project(db, "enrich-boom")
    await db_module.set_goal(db, p["id"], "ship enrichment")
    await db_module.add_sprint_item(db, p["id"], "v1", "Fix OAuth redirect bug")

    def searcher(query):
        raise RuntimeError("graph down")

    _, content = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, graph_searcher=searcher
    )
    # Handoff still produced; just no pointers.
    assert "MERIDIAN_CONTEXT" in content
    assert "Code pointers:" not in content
