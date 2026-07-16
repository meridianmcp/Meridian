"""Tests for 81396666 — model-tier hinting on sprint items.

A lightweight heuristic that suggests "haiku"/"sonnet"/"opus" per sprint item
based on priority + inferred sprint type. Surfaced in generate_handoff and /goal
output. Toggleable per project via executor_config.model_tier_hints_enabled
(default False — opt-in).
"""
from __future__ import annotations

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


# ---------------------------------------------------------------------------
# Unit tests: suggest_model_tier heuristic
# ---------------------------------------------------------------------------


def test_suggest_model_tier_priority_high_default_sprint_type():
    """High priority with no sprint type -> opus (feature table high row)."""
    item = {"priority": "high"}
    result = handoff_module.suggest_model_tier(item)
    assert result == "opus"


def test_suggest_model_tier_priority_normal_default():
    """Normal priority with no sprint type -> sonnet."""
    item = {"priority": "normal"}
    assert handoff_module.suggest_model_tier(item) == "sonnet"


def test_suggest_model_tier_priority_low_default():
    """Low priority with no sprint type -> haiku (lightweight mechanical work)."""
    item = {"priority": "low"}
    assert handoff_module.suggest_model_tier(item) == "haiku"


def test_suggest_model_tier_missing_priority_defaults_to_normal():
    """Missing priority field is treated as normal."""
    item = {}
    assert handoff_module.suggest_model_tier(item) == "sonnet"


def test_suggest_model_tier_none_priority_defaults_to_normal():
    """None priority is treated as normal."""
    item = {"priority": None}
    assert handoff_module.suggest_model_tier(item) == "sonnet"


def test_suggest_model_tier_hotfix_high_opus():
    """High-priority hotfix -> opus (high-stakes fix, needs best reasoning)."""
    item = {"priority": "high"}
    assert handoff_module.suggest_model_tier(item, sprint_type="hotfix") == "opus"


def test_suggest_model_tier_hotfix_normal_sonnet():
    """Normal hotfix -> sonnet (routine bug fix)."""
    item = {"priority": "normal"}
    assert handoff_module.suggest_model_tier(item, sprint_type="hotfix") == "sonnet"


def test_suggest_model_tier_hotfix_low_haiku():
    """Low-priority hotfix -> haiku (trivial patch)."""
    item = {"priority": "low"}
    assert handoff_module.suggest_model_tier(item, sprint_type="hotfix") == "haiku"


def test_suggest_model_tier_research_high_opus():
    """Research item high priority -> opus (deep analysis)."""
    item = {"priority": "high"}
    assert handoff_module.suggest_model_tier(item, sprint_type="research") == "opus"


def test_suggest_model_tier_research_normal_opus():
    """Research item normal priority -> opus (still requires deep reasoning)."""
    item = {"priority": "normal"}
    assert handoff_module.suggest_model_tier(item, sprint_type="research") == "opus"


def test_suggest_model_tier_research_low_sonnet():
    """Research item low priority -> sonnet (not haiku for research work)."""
    item = {"priority": "low"}
    assert handoff_module.suggest_model_tier(item, sprint_type="research") == "sonnet"


def test_suggest_model_tier_refactor_high_sonnet():
    """High-priority refactor -> sonnet (careful structural change, not opus-level)."""
    item = {"priority": "high"}
    assert handoff_module.suggest_model_tier(item, sprint_type="refactor") == "sonnet"


def test_suggest_model_tier_refactor_low_haiku():
    """Low-priority refactor -> haiku (trivial rename/whitespace)."""
    item = {"priority": "low"}
    assert handoff_module.suggest_model_tier(item, sprint_type="refactor") == "haiku"


def test_suggest_model_tier_ops_any_sonnet():
    """Ops items -> sonnet regardless of priority (deploy/release needs care)."""
    for prio in ("high", "normal"):
        item = {"priority": prio}
        assert handoff_module.suggest_model_tier(item, sprint_type="ops") == "sonnet"


def test_suggest_model_tier_feature_high_opus():
    """High-priority feature -> opus (complex new feature)."""
    item = {"priority": "high"}
    assert handoff_module.suggest_model_tier(item, sprint_type="feature") == "opus"


def test_suggest_model_tier_feature_normal_sonnet():
    """Normal feature -> sonnet."""
    item = {"priority": "normal"}
    assert handoff_module.suggest_model_tier(item, sprint_type="feature") == "sonnet"


def test_suggest_model_tier_feature_low_haiku():
    """Low-priority feature -> haiku (mechanical boilerplate)."""
    item = {"priority": "low"}
    assert handoff_module.suggest_model_tier(item, sprint_type="feature") == "haiku"


def test_suggest_model_tier_megasprint_normal_sonnet():
    """Megasprint normal -> sonnet (long run; haiku lacks context depth)."""
    item = {"priority": "normal"}
    assert handoff_module.suggest_model_tier(item, sprint_type="megasprint") == "sonnet"


def test_suggest_model_tier_unknown_sprint_type_falls_back_to_priority_only():
    """Unknown sprint type falls through to the priority-only fallback."""
    item = {"priority": "high"}
    result = handoff_module.suggest_model_tier(item, sprint_type="something_new")
    assert result == "opus"


def test_suggest_model_tier_output_is_always_valid_tier():
    """Every priority/type combo returns one of haiku/sonnet/opus."""
    valid = {"haiku", "sonnet", "opus"}
    for stype in (None, "hotfix", "research", "refactor", "ops", "feature",
                  "megasprint", "orchestrator", "unknown"):
        for prio in ("high", "normal", "low", None, ""):
            item = {"priority": prio}
            result = handoff_module.suggest_model_tier(item, sprint_type=stype)
            assert result in valid, f"Unexpected tier {result!r} for stype={stype}, priority={prio}"


# ---------------------------------------------------------------------------
# Unit tests: _model_tier_hints_enabled toggle
# ---------------------------------------------------------------------------


def test_toggle_defaults_false_when_unset():
    """Default is OFF — no schema migration, no surprise behaviour for existing projects."""
    assert handoff_module._model_tier_hints_enabled(None) is False
    assert handoff_module._model_tier_hints_enabled({}) is False
    assert handoff_module._model_tier_hints_enabled({"executor_config": {}}) is False
    assert handoff_module._model_tier_hints_enabled({"executor_config": None}) is False


def test_toggle_respects_explicit_true():
    settings = {"executor_config": {"model_tier_hints_enabled": True}}
    assert handoff_module._model_tier_hints_enabled(settings) is True


def test_toggle_respects_explicit_false():
    settings = {"executor_config": {"model_tier_hints_enabled": False}}
    assert handoff_module._model_tier_hints_enabled(settings) is False


def test_toggle_non_dict_executor_config_degrades_to_default():
    """Non-dict executor_config degrades gracefully (same as absent)."""
    assert handoff_module._model_tier_hints_enabled(
        {"executor_config": "not-a-dict"}
    ) is False


# ---------------------------------------------------------------------------
# Unit tests: _build_model_hints_clause (pure function)
# ---------------------------------------------------------------------------


def test_build_model_hints_clause_disabled_returns_empty():
    items = [{"id": "abc", "suggested_model": "opus"}]
    assert handoff_module._build_model_hints_clause(items, enabled=False) == ""


def test_build_model_hints_clause_enabled_no_annotations_returns_empty():
    items = [{"id": "abc"}]  # no suggested_model
    assert handoff_module._build_model_hints_clause(items, enabled=True) == ""


def test_build_model_hints_clause_enabled_with_annotations():
    items = [
        {"id": "item1", "suggested_model": "opus"},
        {"id": "item2", "suggested_model": "haiku"},
    ]
    out = handoff_module._build_model_hints_clause(items, enabled=True)
    assert "<model_hints>" in out
    assert "item1: opus" in out
    assert "item2: haiku" in out
    assert "hint only" in out


def test_build_model_hints_clause_skips_items_without_id_or_model():
    items = [
        {"id": "item1", "suggested_model": "sonnet"},
        {"id": "item2"},  # no suggested_model
        {"suggested_model": "haiku"},  # no id
    ]
    out = handoff_module._build_model_hints_clause(items, enabled=True)
    assert "item1: sonnet" in out
    assert "item2" not in out


# ---------------------------------------------------------------------------
# _build_quick_start_goal — model_tier_hints parameter
# ---------------------------------------------------------------------------


def test_goal_includes_model_hints_when_enabled():
    items = [
        {"id": "aaa111", "suggested_model": "opus", "priority": "high"},
        {"id": "bbb222", "suggested_model": "haiku", "priority": "low"},
    ]
    goal = handoff_module._build_quick_start_goal(items, model_tier_hints=True)
    assert "<model_hints>" in goal
    assert "aaa111: opus" in goal
    assert "bbb222: haiku" in goal


def test_goal_omits_model_hints_when_disabled():
    items = [
        {"id": "aaa111", "suggested_model": "opus", "priority": "high"},
    ]
    goal = handoff_module._build_quick_start_goal(items, model_tier_hints=False)
    assert "<model_hints>" not in goal
    assert "suggested_model" not in goal


def test_goal_omits_model_hints_by_default():
    """Default (model_tier_hints not passed) -> no model hints in goal."""
    items = [{"id": "aaa111", "suggested_model": "opus"}]
    goal = handoff_module._build_quick_start_goal(items)
    assert "<model_hints>" not in goal


# ---------------------------------------------------------------------------
# End-to-end via generate_handoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_handoff_includes_hints_when_toggle_on(db, tmp_path):
    """When executor_config.model_tier_hints_enabled=True, generate_handoff
    annotates items and includes <model_hints> in the /goal and 'Suggested model'
    in the handoff markdown."""
    p = await db_module.create_project(db, "hint-e2e-on")
    await db_module.set_goal(db, p["id"], "ship model-tier hints")
    await db_module.add_sprint_item(db, p["id"], "v1", "High-priority research task",
                                    priority="high", group="research")

    # Enable the toggle via update_project_settings (executor_config JSON blob).
    await db_module.update_project_settings(
        db, p["id"],
        executor_config={"model_tier_hints_enabled": True},
    )

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    # /goal block should have <model_hints>
    assert "<model_hints>" in content
    # The handoff markdown body should have the per-item suggestion
    assert "Suggested model:" in content
    # Hint should name one of the valid tiers
    assert any(tier in content for tier in ("haiku", "sonnet", "opus"))


@pytest.mark.asyncio
async def test_generate_handoff_omits_hints_when_toggle_off(db, tmp_path):
    """When executor_config.model_tier_hints_enabled is absent/False (default),
    no model hints appear in the handoff or /goal output."""
    p = await db_module.create_project(db, "hint-e2e-off")
    await db_module.set_goal(db, p["id"], "no hints by default")
    await db_module.add_sprint_item(db, p["id"], "v1", "Normal item", priority="high")

    # No executor_config update — toggle stays at default (False).
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert "<model_hints>" not in content
    assert "Suggested model:" not in content


@pytest.mark.asyncio
async def test_generate_handoff_toggle_off_explicitly(db, tmp_path):
    """Explicitly setting model_tier_hints_enabled=False suppresses hints."""
    p = await db_module.create_project(db, "hint-e2e-explicit-off")
    await db_module.set_goal(db, p["id"], "explicit off")
    await db_module.add_sprint_item(db, p["id"], "v1", "Item", priority="high")

    await db_module.update_project_settings(
        db, p["id"],
        executor_config={"model_tier_hints_enabled": False},
    )
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert "<model_hints>" not in content
    assert "Suggested model:" not in content


@pytest.mark.asyncio
async def test_generate_handoff_hints_correct_tiers_for_priority(db, tmp_path):
    """The annotated tiers in the handoff match the heuristic's expected output."""
    p = await db_module.create_project(db, "hint-correct-tiers")
    await db_module.set_goal(db, p["id"], "tier accuracy")
    # low-priority item: expect haiku
    await db_module.add_sprint_item(db, p["id"], "v1", "Low prio task", priority="low")

    await db_module.update_project_settings(
        db, p["id"],
        executor_config={"model_tier_hints_enabled": True},
    )
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    # Low-priority, feature-type board => haiku expected
    assert "haiku" in content


@pytest.mark.asyncio
async def test_generate_handoff_hints_survive_empty_board(db, tmp_path):
    """No pending items -> no hints block (not an error)."""
    p = await db_module.create_project(db, "hint-empty")
    await db_module.set_goal(db, p["id"], "empty board")
    await db_module.update_project_settings(
        db, p["id"],
        executor_config={"model_tier_hints_enabled": True},
    )
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    # No model_hints tag on an empty board (nothing to annotate).
    assert "<model_hints>" not in content
