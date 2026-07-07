"""Tests for the MECHANICAL model-efficiency classifier (sprint item 0fba4cb6).

The tool ``analyze_model_efficiency`` is a PURE, zero-token, deterministic
classifier: given a task / sprint-item descriptor it suggests the cheapest model
tier likely sufficient, from signals already available (title keywords, file
count, touched-resource shape/count, explicit size). It mirrors how the ultracode
orchestration script spends zero model tokens on routing — NO model call, NO DB,
NO network.

These tests are unit-level with NO servers/ports/network/sleeps. They drive the
pure classifier directly and the dispatcher via ``asyncio.run`` (the same pattern
as tests/test_cov_handler.py, called with ``db=None`` because the tool is pure).
"""
from __future__ import annotations

import asyncio

import meridian.server  # noqa: F401 — import first to avoid handler/server import cycle
from meridian.mcp import handler as mh
from meridian.mcp_tools import _MCP_TOOLS_LIST, _READ_ONLY_TOOLS


def _run(coro):
    return asyncio.run(coro)


def _dispatch(descriptor: dict):
    return _run(mh._dispatch_mcp_tool("analyze_model_efficiency", descriptor, None, "/tmp"))


# ---------------------------------------------------------------------------
# The two headline cases from the sprint item
# ---------------------------------------------------------------------------

def test_fix_typo_in_readme_maps_to_cheap_tier():
    """A trivial 'fix typo in README' single-file change -> cheap tier (haiku)."""
    out = mh._classify_task_tier({
        "title": "Fix typo in README",
        "file_count": 1,
        "size": "xs",
    })
    assert out["tier"] == "haiku", out
    assert out["mode"] == "mechanical"
    # The cheap keyword + narrow file count + tiny size all fired.
    fired = {s["signal"] for s in out["signals"]}
    assert "keyword:typo" in fired
    assert "file_count" in fired
    assert "size" in fired
    assert out["score"] <= -2
    # Every signal carries an auditable weight and detail string.
    for s in out["signals"]:
        assert isinstance(s["weight"], int)
        assert s["detail"]


def test_refactor_auth_across_12_files_plus_migration_maps_to_expensive_tier():
    """'Refactor auth across 12 files + migration' -> expensive tier (opus)."""
    out = mh._classify_task_tier({
        "title": "Refactor auth across 12 files + migration",
        "file_count": 12,
        "touches_resources": ["auth_db", "sessions_table"],
        "size": "xl",
    })
    assert out["tier"] == "opus", out
    assert out["mode"] == "mechanical"
    fired = {s["signal"] for s in out["signals"]}
    # refactor + auth + migration keywords all fire.
    assert "keyword:refactor" in fired
    assert "keyword:auth" in fired
    assert "keyword:migration" in fired
    assert "file_count" in fired
    assert "touches_resources" in fired
    assert out["score"] >= 5


# ---------------------------------------------------------------------------
# Determinism + purity (zero-token: no DB, no network — safe with db=None)
# ---------------------------------------------------------------------------

def test_classifier_is_deterministic():
    descriptor = {
        "title": "Refactor the auth module",
        "file_count": 6,
        "touches_resources": ["db"],
        "size": "l",
    }
    first = mh._classify_task_tier(descriptor)
    for _ in range(5):
        assert mh._classify_task_tier(descriptor) == first


def test_dispatch_routes_to_pure_classifier_without_db():
    """The MCP tool dispatches to the pure classifier with db=None (no DB touch)."""
    out = _dispatch({"title": "Fix typo", "file_count": 1, "size": "xs"})
    assert out["tier"] == "haiku"
    assert out["mode"] == "mechanical"

    out2 = _dispatch({
        "title": "Migrate schema and refactor auth",
        "file_count": 15,
        "touches_resources": ["a", "b", "c"],
        "size": "xl",
    })
    assert out2["tier"] == "opus"


# ---------------------------------------------------------------------------
# Middle / neutral / robustness
# ---------------------------------------------------------------------------

def test_moderate_task_maps_to_middle_tier():
    """A moderate multi-file change with no cheap/expensive keywords -> sonnet."""
    out = mh._classify_task_tier({
        "title": "Add a new field to the settings panel",
        "file_count": 3,
        "size": "m",
    })
    assert out["tier"] == "sonnet", out


def test_empty_descriptor_defaults_to_sonnet_with_no_signals():
    out = mh._classify_task_tier({})
    assert out["tier"] == "sonnet"
    assert out["signals"] == []
    assert out["score"] == 0
    assert "No strong signals" in out["rationale"]


def test_files_list_used_when_file_count_absent():
    out = mh._classify_task_tier({
        "title": "wire up frontend",
        "files": ["a.ts", "b.ts", "c.ts", "d.ts", "e.ts", "f.ts", "g.ts", "h.ts", "i.ts"],
    })
    fired = {s["signal"] for s in out["signals"]}
    assert "file_count" in fired
    # 9 files -> broad cross-cutting file signal pushes toward opus.
    assert out["tier"] == "opus", out


def test_touches_resources_accepts_integer_count():
    out = mh._classify_task_tier({"title": "small change", "touches_resources": 0})
    fired = {s["signal"]: s for s in out["signals"]}
    assert "touches_resources" in fired
    assert fired["touches_resources"]["weight"] < 0  # 0 resources = cheap-leaning


def test_bool_file_count_is_not_treated_as_int():
    """A bool must not slip through the int check (True is an int subclass)."""
    out = mh._classify_task_tier({"title": "noop", "file_count": True})
    fired = {s["signal"] for s in out["signals"]}
    assert "file_count" not in fired


def test_non_dict_descriptor_does_not_raise():
    for bad in (None, "hello", 42, ["a"]):
        out = mh._classify_task_tier(bad)  # type: ignore[arg-type]
        assert out["tier"] == "sonnet"
        assert out["signals"] == []


def test_size_is_case_insensitive():
    lower = mh._classify_task_tier({"title": "x", "size": "xl"})
    upper = mh._classify_task_tier({"title": "x", "size": "XL"})
    assert lower["score"] == upper["score"]


def test_rationale_reports_tier_and_score():
    out = mh._classify_task_tier({"title": "Fix typo", "size": "xs"})
    assert out["tier"] in out["rationale"]
    # Score is rendered with an explicit sign, e.g. "(score -4)".
    assert f"score {out['score']:+d}" in out["rationale"]


# ---------------------------------------------------------------------------
# Schema / discoverability
# ---------------------------------------------------------------------------

def test_tool_registered_in_schema_and_read_only():
    by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    assert "analyze_model_efficiency" in by_name
    tool = by_name["analyze_model_efficiency"]
    schema = tool["inputSchema"]
    props = schema["properties"]
    for key in ("title", "description", "file_count", "files", "touches_resources", "size"):
        assert key in props, key
    # Pure computation, no side effects -> advertised read-only.
    assert "analyze_model_efficiency" in _READ_ONLY_TOOLS
    assert tool.get("annotations", {}).get("readOnlyHint") is True
    # Nothing required — callable with a partial descriptor.
    assert schema.get("required", []) == []


def test_tool_description_notes_zero_token_and_semantic_followup():
    by_name = {t["name"]: t for t in _MCP_TOOLS_LIST}
    desc = by_name["analyze_model_efficiency"]["description"].lower()
    assert "zero-token" in desc or "zero token" in desc
    # The LLM-backed 'semantic' mode is explicitly deferred as a follow-up.
    assert "semantic" in desc
    assert "follow-up" in desc or "out of scope" in desc
