"""Coverage tests for meridian.handoff and meridian.routes.handoff.

Exercises generate_handoff in every mode (full, delta, starter, planner),
the readiness-warning / empty-state branches, the L0 fallback, the custom
template path, the workspace block, the queued-session append, the small
pure helpers, and the HTTP endpoints (including error paths).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path

import pytest

from meridian import capability_contract as cc
from meridian import db as db_module
from meridian import executor_contract as ec
from meridian import handoff as handoff_module
import meridian.server  # noqa: F401 — load the server before handler to avoid its import cycle
from meridian.mcp import handler as mcp_handler


def _run(coro):
    """Run a coroutine in a fresh event loop (pytest-safe)."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_slugify_handles_unsafe_and_empty():
    assert handoff_module._slugify("My Project!! v2") == "My-Project-v2"
    assert handoff_module._slugify("***") == "project"


def test_format_content_str_and_dict():
    assert handoff_module._format_content("plain") == "plain"
    out = handoff_module._format_content({"goal": "x"})
    assert '"goal": "x"' in out


def test_resolve_handoff_mode_explicit_and_default():
    assert handoff_module.resolve_handoff_mode("planner") == "planner"
    assert handoff_module.resolve_handoff_mode("starter") == "starter"
    assert handoff_module.resolve_handoff_mode(None) == "full"
    assert handoff_module.resolve_handoff_mode("nonsense") == "full"


def test_resolve_handoff_mode_repeat_session_switches_to_delta():
    handoff_module._SESSION_HANDOFF_STATE["sess-resolve"] = "2026-01-01 00:00:00"
    try:
        assert handoff_module.resolve_handoff_mode(None, "sess-resolve") == "delta"
    finally:
        handoff_module._SESSION_HANDOFF_STATE.pop("sess-resolve", None)


def test_extract_keywords_drops_stopwords():
    kws = handoff_module._extract_keywords("Fix the broken auth redirect")
    assert "broken" in kws
    assert "auth" in kws
    assert "fix" not in kws  # stopword
    assert "the" not in kws


def test_completed_after_branches():
    # No completed_at → never after
    assert handoff_module._completed_after(None, "2026-01-01 00:00:00") is False
    # No since_ts → always after
    assert handoff_module._completed_after("2026-01-01 00:00:00", None) is True
    # After
    assert handoff_module._completed_after(
        "2026-02-01 00:00:00", "2026-01-01 00:00:00"
    ) is True
    # Before
    assert handoff_module._completed_after(
        "2025-12-01 00:00:00", "2026-01-01 00:00:00"
    ) is False
    # ISO 'T' + Z + fractional-second parsing path
    assert handoff_module._completed_after(
        "2026-02-01T00:00:00.123456Z", "2026-01-01 00:00:00"
    ) is True


def test_build_quick_start_goal_with_and_without_items():
    empty = handoff_module._build_quick_start_goal([])
    assert "Verify remaining work is complete" in empty
    full = handoff_module._build_quick_start_goal(
        [
            {"id": "abc123", "title": "FEAT: first change"},
            {"id": "def456", "title": "FEAT: second change"},
        ]
    )
    assert "abc123" in full and "def456" in full
    assert "complete_sprint_item()" in full
    # f628b880 — non-deferential executor directive leads the items /goal.
    # 5abf3e12 — the /goal is now XML-structured: /goal, then an <executor_directive>
    # tag whose body is the executor directive.
    assert full.startswith("/goal\n<executor_directive>You are an executor. Claim and execute")
    assert "<executor_directive>You are an executor. Claim and execute" in full
    # 4cfaecc2 — the items /goal instructs a live board query up front.
    # 0d5453bc — full suite runs ONCE at the end of the megasprint, not per item.
    assert 'get_sprint_items(status="pending")' in full
    # abac2298 — NO test_floor was configured/passed for this call, so the
    # completion criteria must NOT invent a numeric pass count (the exact bug
    # this item fixes: a receiving repo used to be silently told its floor
    # was Meridian's own historical "2150+", regardless of how many tests it
    # actually had). It must instead ask for an honest collect-only baseline.
    assert "pixi run test passes 2150+" not in full
    assert "pixi run test passes (no test floor is configured" in full
    assert "--collect-only -q" in full
    assert not hasattr(handoff_module, "_DEFAULT_GOAL_TEST_FLOOR")
    assert "ONCE at the very end of the entire" in full
    assert "not per item" in full


def test_build_quick_start_goal_uses_executor_directive_not_role():
    """0af1d7d6 — regression guard: the /goal must use <executor_directive> (not the
    injection-shaped <role> tag that a receiving session's prompt-injection screening
    would rightly flag). Checks both the items path and the empty-board path."""
    items_goal = handoff_module._build_quick_start_goal([{"id": "abc123"}])
    empty_goal = handoff_module._build_quick_start_goal([])
    # New tag must be present on both paths.
    assert "<executor_directive>" in items_goal
    assert "</executor_directive>" in items_goal
    assert "<executor_directive>" in empty_goal
    assert "</executor_directive>" in empty_goal
    # Old injection-shaped tag must NOT appear anywhere in either output.
    assert "<role>" not in items_goal, "<role> tag must not appear — use <executor_directive>"
    assert "</role>" not in items_goal, "</role> tag must not appear — use </executor_directive>"
    assert "<role>" not in empty_goal, "<role> tag must not appear on empty-board path"
    assert "</role>" not in empty_goal, "</role> tag must not appear on empty-board path"


def test_build_quick_start_goal_max_turns():
    """d2c47f43 — max_turns sets the 'Stop after N turns' ceiling (default 200)."""
    # Default 200 on both paths.
    assert "Stop after 200 turns" in handoff_module._build_quick_start_goal([])
    assert "Stop after 200 turns" in handoff_module._build_quick_start_goal([{"id": "x"}])
    # Override applies to both paths.
    assert "Stop after 50 turns" in handoff_module._build_quick_start_goal([], max_turns=50)
    assert "Stop after 50 turns" in handoff_module._build_quick_start_goal(
        [{"id": "x"}], max_turns=50)
    # Invalid / non-positive falls back to default.
    assert "Stop after 200 turns" in handoff_module._build_quick_start_goal([{"id": "x"}], max_turns=0)
    assert "Stop after 200 turns" in handoff_module._build_quick_start_goal([{"id": "x"}], max_turns="bad")


def test_build_quick_start_goal_excludes_manual_items():
    """3a02041a — MANUAL/human items must be kept OUT of the 'claim and execute'
    batch list (an AI executor can't do them and may fake-complete under pressure)
    and surfaced separately as the maintainer's own todo."""
    from meridian.handoff import _is_manual_sprint_item

    items = [
        {"id": "code01", "title": "FEAT: real code work"},
        {"id": "man-title", "title": "MANUAL (Adam): publish the blog post"},
        # 943afe1e — a manual signal is blocker_kind=='manual' (a real-world action
        # outside Meridian), NOT the mere presence of human_id.
        {"id": "man-blk", "title": "Configure PyPI publisher", "blocker_kind": "manual"},
        {"id": "man-mile", "title": "Install binary", "milestone_type": "human"},
    ]
    goal = handoff_module._build_quick_start_goal(items)

    # The executable item leads the claim-and-execute directive.
    assert "code01" in goal
    # 5abf3e12 — XML-structured /goal: /goal then <executor_directive>…executor directive…
    assert goal.startswith("/goal\n<executor_directive>You are an executor. Claim and execute")
    # MANUAL ids appear ONLY in the separate <exclusions> tag, never in the
    # pressure list. (5abf3e12 — the maintainer todo is now an <exclusions> tag.)
    exec_clause, _, manual_clause = goal.partition("<exclusions>")
    assert manual_clause, "expected a separate <exclusions> MANUAL todo section"
    assert "maintainer's own todo" in manual_clause
    for mid in ("man-title", "man-blk", "man-mile"):
        assert mid not in exec_clause, f"{mid} leaked into the executor list"
        assert mid in manual_clause
    assert "code01" not in manual_clause
    # The maintainer note carries no completion-pressure / anti-stop language
    # (no <not_done_until> and no legacy "is a FAILURE" phrasing).
    assert "is a FAILURE" not in manual_clause
    assert "not done while any listed item" not in manual_clause
    assert "must NOT claim, execute, or complete" in manual_clause

    # Helper classifies each GENUINE MANUAL signal, and leaves real work alone.
    assert _is_manual_sprint_item({"title": "MANUAL (Adam): x"})
    assert _is_manual_sprint_item({"title": "x", "blocker_kind": "manual"})
    assert _is_manual_sprint_item({"title": "x", "milestone_type": "human"})
    assert not _is_manual_sprint_item({"id": "y", "title": "FEAT: y"})
    # 943afe1e — human_id ALONE (who is assigned) is NOT a manual-only signal: an
    # executor-actionable item assigned to a maintainer stays claimable.
    assert not _is_manual_sprint_item(
        {"id": "y", "title": "BUG: crash on start", "human_id": "adam"}
    )


def test_human_id_bug_item_is_actionable_not_excluded():
    """943afe1e — regression: generate_handoff's <exclusions> logic wrongly caught
    recently-created/edited ``human_id='adam'`` items alongside genuine MANUAL-only
    items. An executor-actionable item (BUG/FIX/FEAT title, real touches_resources)
    assigned to a human must land in the claim-and-execute list, NOT the exclusions
    block; genuine MANUAL items must still be excluded."""
    from meridian.handoff import _is_manual_sprint_item

    items = [
        # Executor-actionable, merely assigned to a human — must be actionable.
        {
            "id": "bug-adam",
            "title": "BUG: file_read_claims TTL cleanup crashes",
            "human_id": "adam",
            "touches_resources": ["file:meridian/db/__init__.py"],
        },
        # A genuine maintainer-only todo — must stay excluded.
        {
            "id": "man-adam",
            "title": "MANUAL (Adam): configure PyPI trusted publisher",
            "human_id": "adam",
        },
    ]

    # Direct classifier: assignment is not a manual signal; a MANUAL title is.
    assert not _is_manual_sprint_item(items[0])
    assert _is_manual_sprint_item(items[1])

    goal = handoff_module._build_quick_start_goal(items)
    exec_clause, _, manual_clause = goal.partition("<exclusions>")
    assert manual_clause, "expected a separate <exclusions> MANUAL todo section"

    # The BUG item assigned to adam is in the claim-and-execute list, NOT excluded.
    assert "bug-adam" in exec_clause
    assert "bug-adam" not in manual_clause
    # The genuine MANUAL item is still excluded and never leaks into the exec list.
    assert "man-adam" in manual_clause
    assert "man-adam" not in exec_clause


def test_build_quick_start_goal_all_manual_still_surfaces_todo():
    """3a02041a — when every pending item is MANUAL the executor path has nothing
    to claim (falls back to the verify goal), but the MANUAL todo is still shown."""
    goal = handoff_module._build_quick_start_goal(
        [{"id": "m1", "title": "MANUAL (Adam): screenshots", "human_id": "adam"}]
    )
    assert "Verify remaining work is complete" in goal   # empty executable path
    # (MANUAL-tagged title is the manual signal here; human_id is incidental.)
    assert "maintainer's own todo" in goal
    # 5abf3e12 — the maintainer todo is the <exclusions> tag.
    before_note = goal.split("<exclusions>")[0]
    assert "m1" not in before_note   # never in a claim-and-execute clause
    assert "m1" in goal              # present only in the note


def test_infer_sprint_type_heuristics(monkeypatch):
    """f9fa00e4 — sprint-type inference by count / item_group / title keyword."""
    from meridian import handoff as h
    from meridian.handoff import _infer_sprint_type
    assert _infer_sprint_type([]) == "general"
    assert _infer_sprint_type(
        [{"id": str(i), "title": "feat x"} for i in range(12)]) == "megasprint"
    assert _infer_sprint_type(
        [{"id": "1", "item_group": "research"}, {"id": "2", "item_group": "paper"}]) == "research"
    assert _infer_sprint_type(
        [{"id": "1", "title": "REFACTOR: split module"},
         {"id": "2", "title": "refactor dashboard"}]) == "refactor"
    assert _infer_sprint_type(
        [{"id": "1", "item_group": "ops"}, {"id": "2", "item_group": "release"}]) == "ops"
    assert _infer_sprint_type([{"id": "1", "title": "hotfix: crash on start"}]) == "hotfix"
    assert _infer_sprint_type([{"id": "1", "title": "FEAT: add a button"}]) == "feature"
    assert _infer_sprint_type(
        [{"id": "1", "title": "add button"}, {"id": "2", "title": "add page"}]) == "general"

    general_goal = h._build_quick_start_goal(
        [{"id": "1", "title": "add button"}]
    )
    assert '<sprint_type value="general">' in general_goal
    assert "<test_gate_note>" not in general_goal
    assert "pixi run test" not in general_goal
    assert "deploy" not in general_goal

    empty_goal = h._build_quick_start_goal([])
    assert "pixi run test" not in empty_goal
    assert "deploy" not in empty_goal

    original_infer = h._infer_sprint_type

    def _unexpected_infer(_items):
        raise AssertionError("custom project criteria must skip inference")

    monkeypatch.setattr(h, "_infer_sprint_type", _unexpected_infer)
    custom_goal = h._build_quick_start_goal(
        [{"id": "1", "title": "FEAT: still skipped"}],
        completion_criteria_override="Ship when A < B & C > D.",
    )
    assert "Ship when A &lt; B &amp; C &gt; D." in custom_goal
    assert '<sprint_type value="general">' in custom_goal
    assert "<test_gate_note>" not in custom_goal
    monkeypatch.setattr(h, "_infer_sprint_type", original_infer)


def test_build_quick_start_goal_tags_sprint_type():
    """f9fa00e4 — the /goal carries an inferred sprint type + guidance, without
    disturbing the existing structure. 5abf3e12 — the type now rides a
    <sprint_type value="..."> XML tag instead of a "[sprint:TYPE]" inline tag."""
    from meridian.handoff import _build_quick_start_goal
    goal = _build_quick_start_goal([{"id": "a1", "title": "hotfix: crash on start"}])
    assert '<sprint_type value="hotfix">' in goal
    assert "HOTFIX sprint" in goal
    assert "a1" in goal
    assert 'get_sprint_items(status="pending")' in goal
    assert "Stop after" in goal


def test_build_quick_start_goal_completion_mode_and_group_style():
    """9f57374b — completion_mode='lenient' drops the anti-stop completion
    constraint; goal_group_style='waves' restores Wave headers for a dependency
    graph; the executor_config extractors default to strict/flat.

    5abf3e12 — the anti-stop framing is now the <not_done_until> XML tag (the old
    prose-threat "is a FAILURE" wording is gone) but the constraint is unchanged:
    strict mode carries it, lenient mode drops it."""
    from meridian.handoff import (
        _build_quick_start_goal,
        _completion_mode_from_settings,
        _goal_group_style_from_settings,
    )
    strict = _build_quick_start_goal([{"id": "a1"}])
    assert "<not_done_until>" in strict
    assert "not done while any listed item is still" in strict
    assert "is a FAILURE" not in strict  # threat wording removed by 5abf3e12
    lenient = _build_quick_start_goal([{"id": "a1"}], completion_mode="lenient")
    assert "<not_done_until>" not in lenient

    deps = [{"id": "a1"}, {"id": "a2", "depends_on": "a1"}]
    assert "Wave" not in _build_quick_start_goal(deps)  # default flat
    waved = _build_quick_start_goal(deps, goal_group_style="waves")
    assert "Wave 1: a1" in waved and "Wave 2: a2" in waved

    assert _completion_mode_from_settings(
        {"executor_config": {"completion_mode": "lenient"}}) == "lenient"
    assert _completion_mode_from_settings({}) == "strict"
    assert _goal_group_style_from_settings(
        {"executor_config": {"goal_group_style": "waves"}}) == "waves"
    assert _goal_group_style_from_settings(None) == "flat"


def _parse_goal_xml(goal):
    """Wrap the /goal body (everything after the '/goal' + newline prefix) in a
    root element and parse it — asserts the /goal is well-formed XML."""
    import xml.etree.ElementTree as ET
    body = goal.split("/goal", 1)[1].lstrip("\n")
    root = ET.fromstring(f"<goal_root>{body}</goal_root>")
    return {child.tag: (child.text or "") for child in root}, root


def test_build_quick_start_goal_xml_structure_preserves_constraints():
    """5abf3e12 — the /goal is XML-structured (no ALL-CAPS threat prose) yet every
    semantic constraint from the old prose form is preserved, and the XML parses.

    Constraints checked: executor role/immediacy, live-board first step, the item
    list, the completion criteria (complete_sprint_item + test floor +
    generate_handoff), the anti-stop 'not done until every item complete', the
    turn + HITL stop conditions, the sprint-type tag, and the MANUAL exclusion —
    all as clean XML tags."""
    from meridian.handoff import _build_quick_start_goal

    goal = _build_quick_start_goal(
        [
            {"id": "code1", "title": "FEAT: real work"},
            {"id": "code2", "title": "FEAT: more work"},
            {"id": "m1", "title": "MANUAL (Adam): publish the blog post", "human_id": "adam"},
        ],
        test_floor=350,
    )
    # No prose-threat / ALL-CAPS framing survives.
    assert "is a FAILURE" not in goal
    assert "FAILURE" not in goal

    tags, root = _parse_goal_xml(goal)  # asserts well-formed XML
    # Every constraint is present as a distinct XML tag.
    assert set(tags) >= {
        "executor_directive", "first_step", "sprint_items", "completion_criteria",
        "not_done_until", "stop_conditions", "sprint_type", "exclusions",
    }
    # <executor_directive> — executor, act immediately, don't ask.
    assert "You are an executor" in tags["executor_directive"]
    assert "without asking for direction" in tags["executor_directive"]
    # <first_step> — live board query before trusting the snapshot.
    assert 'get_sprint_items(status="pending")' in tags["first_step"]
    # <sprint_items> — the executable ids, MANUAL item excluded.
    assert "code1" in tags["sprint_items"] and "code2" in tags["sprint_items"]
    assert "m1" not in tags["sprint_items"]
    # <completion_criteria> — the three done-conditions, verbatim semantics.
    # 0d5453bc — wording must be explicit: full suite once at end of megasprint.
    assert "complete_sprint_item()" in tags["completion_criteria"]
    assert "pixi run test passes 350+" in tags["completion_criteria"]
    assert "ONCE at the very end of the entire" in tags["completion_criteria"]
    assert "not per item" in tags["completion_criteria"]
    assert "generate_handoff()" in tags["completion_criteria"]
    # <not_done_until> — the anti-stop constraint (re-expressed from the threat).
    assert "every listed item" in tags["not_done_until"]
    assert "not done while any listed item is still pending" in tags["not_done_until"]
    assert "do not hand off with items pending" in tags["not_done_until"]
    # <stop_conditions> — turn ceiling + HITL.
    assert "Stop after 200 turns" in tags["stop_conditions"]
    assert "or if HITL triggered." in tags["stop_conditions"]
    # <sprint_type> — inferred type on the attribute.
    assert root.find("sprint_type").attrib["value"] == "feature"
    # <exclusions> — the MANUAL carve-out, still saying the executor must NOT do them.
    assert "m1" in tags["exclusions"]
    assert "must NOT claim, execute, or complete" in tags["exclusions"]
    assert "maintainer's own todo" in tags["exclusions"]


def test_build_quick_start_goal_xml_hitl_mode1_no_hitl_rule():
    """5abf3e12 — the no-HITL rule (auto-answer mode) rides <stop_conditions>, and
    the goal stays well-formed XML."""
    from meridian.handoff import _build_quick_start_goal
    goal = _build_quick_start_goal([{"id": "a1"}], hitl_auto_answer_mode=1)
    tags, _ = _parse_goal_xml(goal)
    assert "Do NOT file HITLs" in tags["stop_conditions"]
    assert "or if HITL triggered." not in goal


def test_build_quick_start_goal_xml_escapes_untrusted_manual_title():
    """5abf3e12 — a MANUAL item title carrying raw XML metacharacters is escaped
    inside <exclusions>, so it cannot inject tags into the /goal (build_goal_xml
    escaping discipline). The /goal remains parseable."""
    from meridian.handoff import _build_quick_start_goal
    goal = _build_quick_start_goal(
        [
            {"id": "c1", "title": "FEAT real"},
            {"id": "m1", "title": "MANUAL: publish <script>alert(1)</script> & more",
             "human_id": "adam"},
        ]
    )
    # Raw injected tag must NOT appear; escaped form must.
    assert "<script>" not in goal
    assert "&lt;script&gt;" in goal
    tags, _ = _parse_goal_xml(goal)  # still well-formed
    assert "alert(1)" in tags["exclusions"]


def test_build_quick_start_goal_test_gate_note_is_xml_escaped():
    """Regression for c6c63b0/b6a3f7c1 — the <test_gate_note> block's guidance
    text contained literal `<path>::<test>` placeholder angle brackets, which a
    real XML parser reads as unclosed tags ("mismatched tag") and broke every
    consumer that parses the generated /goal as XML. The content must go through
    _xml_escape like every other dynamic block in this function."""
    from meridian.handoff import _build_quick_start_goal
    goal = _build_quick_start_goal([{"id": "c1", "title": "FEAT real"}])
    # The raw, unescaped placeholder must never appear directly.
    assert "<path>::<test>" not in goal
    # The escaped form must be present instead.
    assert "&lt;path&gt;::&lt;test&gt;" in goal
    tags, _ = _parse_goal_xml(goal)  # asserts well-formed XML end-to-end
    assert "test_gate_note" in tags
    assert "INTERNALERROR" in tags["test_gate_note"]


def test_resolve_graph_searcher_uses_registered_resolver():
    """4cfaecc2 — _resolve_graph_searcher consults the injectable resolver and is
    guarded so a resolver that raises can never break the mandatory handoff."""
    try:
        # No resolver registered -> None (historical default).
        handoff_module.set_graph_searcher_resolver(None)
        assert handoff_module._resolve_graph_searcher("proj-1") is None
        # Registered resolver's return value is passed through, keyed by project.
        def _sentinel(_q):
            return [{"file": "a.py"}]
        handoff_module.set_graph_searcher_resolver(
            lambda pid: _sentinel if pid == "proj-1" else None
        )
        assert handoff_module._resolve_graph_searcher("proj-1") is _sentinel
        assert handoff_module._resolve_graph_searcher("other") is None
        # A resolver that raises degrades to None.
        def _boom(_pid):
            raise RuntimeError("nope")
        handoff_module.set_graph_searcher_resolver(_boom)
        assert handoff_module._resolve_graph_searcher("proj-1") is None
    finally:
        handoff_module.set_graph_searcher_resolver(None)


def test_partition_into_waves_topological_layers():
    """3726cf70 — depends_on chains are layered into ordered waves; within-wave
    items are unordered; cycles/external deps collapse to wave 0."""
    items = [
        {"id": "a"},
        {"id": "b", "depends_on": "a"},
        {"id": "c", "depends_on": "b"},
        {"id": "d", "depends_on": "a"},
        {"id": "e", "depends_on": "external-not-in-set"},
    ]
    waves = handoff_module._partition_into_waves(items)
    ids = [[it["id"] for it in w] for w in waves]
    assert ids[0] == ["a", "e"]           # roots (a; e's dep is external)
    assert set(ids[1]) == {"b", "d"}      # depend on a
    assert ids[2] == ["c"]                # depends on b
    # A cycle must not hang or drop items.
    cyc = handoff_module._partition_into_waves(
        [{"id": "x", "depends_on": "y"}, {"id": "y", "depends_on": "x"}]
    )
    assert sum(len(w) for w in cyc) == 2


def test_build_quick_start_goal_flattens_deps_no_wave_headers():
    """eeee02c6 — a dependency graph flattens into one ordered id list (no 'Wave'
    headers, which invite stopping between waves); a flat list keeps the legacy
    phrasing. Both carry the anti-stop completion constraint (5abf3e12: now the
    <not_done_until> XML tag, not the old "is a FAILURE" prose)."""
    flat = handoff_module._build_quick_start_goal([{"id": "a1"}, {"id": "a2"}])
    assert "Complete sprint items: a1, a2." in flat
    assert "Wave" not in flat
    assert "<not_done_until>" in flat
    waved = handoff_module._build_quick_start_goal([
        {"id": "a1"}, {"id": "a2", "depends_on": "a1"},
    ])
    assert "dependency order" in waved
    assert "a1, a2" in waved
    assert "Wave" not in waved
    assert "<not_done_until>" in waved


def test_agent_instructions_stale_detection():
    """99e50a1d — stored copies predating the standard are flagged; the current
    default and genuinely-bespoke docs are not."""
    from meridian import agent_defaults as ad
    # Current default carries the marker -> not stale.
    assert ad.parse_standard_version(ad.DEFAULT_AGENT_INSTRUCTIONS) == \
        ad.AGENT_INSTRUCTIONS_STANDARD_VERSION
    assert ad.agent_instructions_stale(ad.DEFAULT_AGENT_INSTRUCTIONS) is False
    # None / empty -> session uses live default, never stale.
    assert ad.agent_instructions_stale(None) is False
    assert ad.agent_instructions_stale("   ") is False
    # A pre-versioning Meridian rules doc (no marker) -> stale.
    old = "# Meridian — executor rules\nCall start_session(project_id=...) first."
    assert ad.parse_standard_version(old) is None
    assert ad.agent_instructions_stale(old) is True
    # An explicit older version marker -> stale.
    assert ad.agent_instructions_stale(
        "Meridian start_session <!-- meridian-executor-standard: v1 -->"
    ) is True
    # Bespoke, non-Meridian instructions -> never nagged.
    assert ad.agent_instructions_stale("Just do whatever, no rules here.") is False


@pytest.mark.asyncio
async def test_generate_handoff_warns_on_stale_executor_rules(db, tmp_path):
    """99e50a1d — a project whose stored rules predate the standard leads its
    handoff with a sync notice; a current project does not."""
    from meridian import agent_defaults as ad
    stale = await db_module.create_project(db, "stale-proj")
    await db_module.set_agent_instructions(
        db, stale["id"],
        "# Meridian — executor rules\nCall start_session(project_id=...) first.",
    )
    _, content, _ = await handoff_module.generate_handoff(
        db, stale["id"], str(tmp_path), skip_ai_summary=True
    )
    assert "Executor rules are behind the current standard" in content

    fresh = await db_module.create_project(db, "fresh-proj")
    await db_module.set_agent_instructions(db, fresh["id"], ad.DEFAULT_AGENT_INSTRUCTIONS)
    _, content2, _ = await handoff_module.generate_handoff(
        db, fresh["id"], str(tmp_path), skip_ai_summary=True
    )
    assert "Executor rules are behind the current standard" not in content2


def test_max_turns_from_settings():
    """d2c47f43 — extract executor_config.max_turns with a 200 default."""
    f = handoff_module._max_turns_from_settings
    assert f(None) == 200
    assert f({"executor_config": {}}) == 200
    assert f({"executor_config": {"max_turns": 75}}) == 75
    assert f({"executor_config": {"max_turns": 0}}) == 200       # non-positive → default
    assert f({"executor_config": {"max_turns": "nope"}}) == 200  # bad → default
    assert f({"executor_config": "notadict"}) == 200


def test_execution_policy_from_settings():
    """75ac1c8e — resolves the canonical execution policy from proj_settings +
    the already-normalized execution_mode, honoring executor_config.max_planning_turns."""
    f = handoff_module._execution_policy_from_settings
    default = f(None, "autonomous")
    assert default["execution_mode"] == "immediate"
    assert default["required_first_action"] == "claim_sprint_item"
    assert default["max_planning_turns"] == 1
    assert default["no_confirmation"] is True
    assert default["permitted_parallel_wave"] is True
    assert default["claim_before_edit"] is True

    relaxed = f({"executor_config": {}}, "interactive")
    assert relaxed["execution_mode"] == "relaxed"
    assert relaxed["required_first_action"] == "get_sprint_items"
    assert relaxed["no_confirmation"] is False
    assert relaxed["permitted_parallel_wave"] is False

    overridden = f({"executor_config": {"max_planning_turns": 7}}, "autonomous")
    assert overridden["max_planning_turns"] == 7

    # A non-dict executor_config degrades to the mode default, never raises.
    assert f({"executor_config": "nope"}, "autonomous")["max_planning_turns"] == 1


def test_build_execution_policy_clause_renders_attributes_and_escapes():
    from meridian.handoff import _build_execution_policy_clause

    policy = {
        "execution_mode": "immediate",
        "max_planning_turns": 1,
        "required_first_action": "claim_sprint_item",
        "no_confirmation": True,
        "permitted_parallel_wave": True,
        "claim_before_edit": True,
        "genuine_blocker_escalation": 'Escalate only for "genuine" blockers.',
    }
    clause = _build_execution_policy_clause(policy)
    assert clause.startswith("\n<execution_policy ")
    assert 'execution_mode="immediate"' in clause
    assert 'max_planning_turns="1"' in clause
    assert 'required_first_action="claim_sprint_item"' in clause
    assert 'no_confirmation="true"' in clause
    assert 'permitted_parallel_wave="true"' in clause
    assert 'claim_before_edit="true"' in clause
    assert clause.endswith("</execution_policy>")
    # Falsy/invalid policy degrades to no tag.
    assert _build_execution_policy_clause(None) == ""
    assert _build_execution_policy_clause({}) == ""


# ---------------------------------------------------------------------------
# 6cfdabd7 — render the configured test command and parallelism policy in
# handoffs. set_executor_config's test_cmd used to be silently ignored by
# /goal rendering: the completion criteria and xdist test_gate_note both
# hardcoded "pixi run test -n 3" regardless of what a project actually had
# configured (or pixi.toml's own default, which had already moved to
# "-n auto"). These tests cover the new settings-reading helpers directly.
# ---------------------------------------------------------------------------


def test_test_cmd_from_settings():
    """Sibling of _max_turns_from_settings: same read pattern/fail-safe default."""
    f = handoff_module._test_cmd_from_settings
    assert f(None) == "pixi run test"
    assert f({"executor_config": {}}) == "pixi run test"
    assert f({"executor_config": {"test_cmd": "pixi run test -n auto"}}) == \
        "pixi run test -n auto"
    # Whitespace is trimmed.
    assert f({"executor_config": {"test_cmd": "  pixi run test-pg  "}}) == \
        "pixi run test-pg"
    # Blank/non-string/non-dict all degrade to the default, never raise.
    assert f({"executor_config": {"test_cmd": "   "}}) == "pixi run test"
    assert f({"executor_config": {"test_cmd": 42}}) == "pixi run test"
    assert f({"executor_config": "notadict"}) == "pixi run test"


# ---------------------------------------------------------------------------
# abac2298 — repository-aware test floor: executor_config.test_min flows
# into the completion criteria as an honest, project-scoped number instead
# of Meridian's own historical "2150+" being silently applied everywhere.
# ---------------------------------------------------------------------------


def test_test_floor_from_settings():
    """Sibling of _branch_from_settings: unset/invalid all degrade to None
    (never a fabricated fallback number) rather than raising."""
    f = handoff_module._test_floor_from_settings
    assert f(None) is None
    assert f({"executor_config": {}}) is None
    assert f({"executor_config": {"test_min": 42}}) == 42
    assert f({"executor_config": {"test_min": "88"}}) == 88
    # Non-numeric / non-positive / non-dict all degrade to None, never raise.
    assert f({"executor_config": {"test_min": "not-a-number"}}) is None
    assert f({"executor_config": {"test_min": 0}}) is None
    assert f({"executor_config": {"test_min": -5}}) is None
    assert f({"executor_config": "notadict"}) is None


def test_render_test_floor_clause_configured_vs_unknown():
    f = handoff_module._render_test_floor_clause
    # A configured, positive floor renders the literal historical "N+" claim.
    assert f("pixi run test -n auto", 88) == "pixi run test -n auto passes 88+"
    # Unset/invalid floors never invent a number -- they ask for an honest
    # collect-only baseline instead (abac2298: the exact bug this fixes).
    for bad in (None, 0, -1):
        clause = f("pixi run test", bad)
        assert clause.startswith("pixi run test passes (")
        assert "no test floor is configured" in clause
        assert "--collect-only -q" in clause
        assert "2150" not in clause


def test_is_plausible_test_cmd():
    f = handoff_module._is_plausible_test_cmd
    assert f("pixi run test -n auto") is True
    assert f("pytest tests/ -q") is True
    assert f("npx jest") is True
    # Blank, or a command mentioning no recognized test-runner token at all
    # (e.g. accidentally configured to a build/deploy command), is flagged.
    assert f("") is False
    assert f("   ") is False
    assert f("pixi run build") is False
    assert f("npm run deploy") is False


def test_branch_from_settings():
    f = handoff_module._branch_from_settings
    assert f(None) is None
    assert f({"executor_config": {}}) is None
    assert f({"executor_config": {"branch": "dev"}}) == "dev"
    assert f({"executor_config": {"branch": "  main  "}}) == "main"
    assert f({"executor_config": {"branch": "   "}}) is None
    assert f({"executor_config": "notadict"}) is None


def test_parallelism_policy_from_test_cmd():
    f = handoff_module._parallelism_policy_from_test_cmd
    assert f("pixi run test -n auto") == "-n auto"
    assert f("pixi run test -n 8") == "-n 8"
    assert f("pytest tests/ --numprocesses=4") == "-n 4"
    assert f("pytest tests/ --numprocesses 4") == "-n 4"
    # No -n/--numprocesses flag -> clearly-labeled fallback, never a guess.
    assert f("pixi run test") == "not declared in test_cmd (task's own default applies)"
    assert f("") == "not declared in test_cmd (task's own default applies)"


def test_strip_parallelism_flag():
    f = handoff_module._strip_parallelism_flag
    # A flag present -> removed cleanly.
    assert f("pixi run test -n auto") == "pixi run test"
    assert f("pixi run test -n 8 -q") == "pixi run test -q"
    # No flag present -> explicit no:xdist appended rather than a silent no-op,
    # so the rendered triage command always actually disables parallelism.
    assert f("pixi run test") == "pixi run test -p no:xdist"
    assert f("") == "pixi run test -p no:xdist"


def test_build_test_gate_config_clause_renders_effective_values():
    from meridian.handoff import _build_test_gate_config_clause

    clause = _build_test_gate_config_clause(
        test_cmd="pixi run test -n auto", branch="dev", version="v0.3.1", test_floor=88,
    )
    assert clause.startswith("\n<test_gate_config ")
    assert 'test_cmd="pixi run test -n auto"' in clause
    assert 'parallelism="-n auto"' in clause
    assert 'branch="dev"' in clause
    assert 'version="v0.3.1"' in clause
    # abac2298 — a configured test_floor is machine-readable too, not just
    # in the completion-criteria prose.
    assert 'test_floor="88"' in clause
    assert 'baseline="configured"' in clause
    assert 'test_cmd_plausible="true"' in clause
    assert clause.endswith(" />")

    # Unset branch/version render a clearly-labeled fallback, not omission.
    unset = _build_test_gate_config_clause(test_cmd="pixi run test", branch=None, version=None)
    assert 'branch="unset"' in unset
    assert 'version="unscoped"' in unset
    # abac2298 — no test_floor passed at all -> honest "unknown" baseline,
    # never a silently-invented default.
    assert 'test_floor="unknown"' in unset
    assert 'baseline="unknown_validate_via_collect_only"' in unset

    # abac2298 — a test_cmd with no recognized test-runner token is flagged
    # implausible on the SAME machine-readable tag.
    implausible = _build_test_gate_config_clause(
        test_cmd="pixi run build", branch=None, version=None,
    )
    assert 'test_cmd_plausible="false"' in implausible


def test_build_quick_start_goal_renders_effective_test_cmd_not_hardcoded():
    """The bug this item fixes: a configured test_cmd must flow into BOTH the
    completion criteria prose and the test_gate_note/test_gate_config -- not
    a hardcoded 'pixi run test -n 3'."""
    goal = handoff_module._build_quick_start_goal(
        # ``version`` (below) scopes the claimable batch to items whose own
        # ``version`` field matches -- give the item the matching value so it
        # survives that filter and the completion/test_gate_note text is
        # actually rendered (see _build_quick_start_goal's version handling).
        [{"id": "c1", "title": "FEAT: real work", "version": "v0.4.0"}],
        test_floor=100,
        test_cmd="pixi run test -n auto",
        branch="dev",
        version="v0.4.0",
    )
    assert "-n 3" not in goal
    assert "pixi run test -n auto passes 100+" in goal
    assert 'test_cmd="pixi run test -n auto"' in goal
    assert 'parallelism="-n auto"' in goal
    assert 'branch="dev"' in goal
    assert 'version="v0.4.0"' in goal


def test_build_quick_start_goal_default_test_cmd_has_no_stale_flag():
    """No executor_config configured -> the fallback is the bare project test
    task with no independently-hardcoded -n value (that hardcoded value going
    stale is the exact bug 6cfdabd7 fixes)."""
    goal = handoff_module._build_quick_start_goal(
        [{"id": "c1", "title": "FEAT: real work"}],
    )
    assert "-n 3" not in goal
    assert "pixi run test passes" in goal
    assert 'test_cmd="pixi run test"' in goal
    assert 'parallelism="not declared in test_cmd' in goal
    assert 'branch="unset"' in goal
    assert 'version="unscoped"' in goal


@pytest.mark.asyncio
async def test_handoff_modes_render_same_effective_test_cmd_parallelism_branch_version(
    db, tmp_path,
):
    """6cfdabd7 acceptance: full/delta/starter/goal all render the SAME
    effective test_cmd/parallelism/branch/version for the SAME underlying
    executor_config -- proving the four modes can never disagree, because
    they all resolve settings via the SAME _test_cmd_from_settings/
    _branch_from_settings helpers into the SAME shared _build_quick_start_goal
    call.

    abac2298 — this project's executor_config ALSO sets test_min=42; before
    this fix, _build_quick_start_goal never read test_min at all (there was
    no such wiring), so every mode silently rendered Meridian's own
    hardcoded "2150+" instead of the 42 this project actually configured.
    All four modes must now agree on "passes 42+" too."""
    p = await db_module.create_project(db, "test-cmd-parity")
    await db_module.set_executor_config(
        db, p["id"],
        {"test_cmd": "pixi run test -n auto", "branch": "dev", "test_min": 42},
    )
    s = await db_module.register_session(db, p["id"], "sess-parity")
    await db_module.add_sprint_item(db, p["id"], "v1", "FEAT: parity check", force=True)

    expected_snippets = [
        'test_cmd="pixi run test -n auto"',
        'parallelism="-n auto"',
        'branch="dev"',
        # No explicit version scope was requested for any mode below, so all
        # four must agree on the SAME unscoped fallback label too.
        'version="unscoped"',
        # abac2298 — the configured test_min=42 must flow through as the
        # REAL floor, both in prose and on the machine-readable tag.
        "pixi run test -n auto passes 42+",
        'test_floor="42"',
        'baseline="configured"',
    ]

    for mode in ("full", "delta", "starter", "goal"):
        _, content, _ = await handoff_module.generate_handoff(
            db, p["id"], str(tmp_path), skip_ai_summary=True, mode=mode,
            session_id=s["id"],
        )
        for snippet in expected_snippets:
            assert snippet in content, (
                f"mode={mode!r} missing {snippet!r} -- handoff modes disagree "
                "on the effective test_cmd/parallelism/branch/test_floor"
            )
        # No mode should ever surface the old hardcoded, now-stale values.
        assert "-n 3" not in content, f"mode={mode!r} leaked stale '-n 3' text"
        assert "2150" not in content, (
            f"mode={mode!r} leaked Meridian's own historical test count "
            "instead of this project's configured test_min=42"
        )


def test_build_quick_start_goal_execution_policy_default_immediate():
    """75ac1c8e — the default executor handoff (no execution_policy passed,
    execution_mode defaults to 'autonomous') always carries the immediate-mode
    <execution_policy> tag right after <executor_directive>, on both the
    empty-board and normal item paths."""
    from meridian.handoff import _build_quick_start_goal

    items_goal = _build_quick_start_goal([{"id": "abc123"}])
    empty_goal = _build_quick_start_goal([])
    for goal in (items_goal, empty_goal):
        assert "</executor_directive>\n<execution_policy " in goal
        assert 'execution_mode="immediate"' in goal
        assert 'required_first_action="claim_sprint_item"' in goal
        assert 'no_confirmation="true"' in goal
        assert 'claim_before_edit="true"' in goal
        tags, _ = _parse_goal_xml(goal)
        assert "execution_policy" in tags


def test_build_quick_start_goal_execution_policy_relaxed_mode_honored():
    """Explicit execution_mode='interactive' produces the relaxed policy —
    different required_first_action and no_confirmation=false — while the
    <executor_directive> body keeps its own existing deferential framing."""
    from meridian.handoff import _build_quick_start_goal

    goal = _build_quick_start_goal(
        [{"id": "abc123"}], execution_mode="interactive",
    )
    assert 'execution_mode="relaxed"' in goal
    assert 'required_first_action="get_sprint_items"' in goal
    assert 'no_confirmation="false"' in goal
    assert 'permitted_parallel_wave="false"' in goal
    # claim_before_edit is non-negotiable even in relaxed mode.
    assert 'claim_before_edit="true"' in goal
    tags, _ = _parse_goal_xml(goal)
    assert "you are assisting interactively" in tags["executor_directive"].lower()


def test_build_quick_start_goal_execution_policy_explicit_override():
    """A caller-supplied execution_policy dict (e.g. from
    _execution_policy_from_settings with a max_planning_turns override) is
    serialized verbatim rather than recomputed from execution_mode alone."""
    from meridian.handoff import _build_quick_start_goal

    custom_policy = {
        "execution_mode": "immediate",
        "max_planning_turns": 3,
        "required_first_action": "claim_sprint_item",
        "no_confirmation": True,
        "permitted_parallel_wave": True,
        "claim_before_edit": True,
        "genuine_blocker_escalation": "custom escalation text",
    }
    goal = _build_quick_start_goal(
        [{"id": "abc123"}], execution_policy=custom_policy,
    )
    assert 'max_planning_turns="3"' in goal
    assert "custom escalation text" in goal


def test_note_tags_and_select_strategic_notes():
    notes = [
        {"title": "Insight one", "body": "b", "kind": "insight"},
        {"title": "Strat", "body": "b", "tags": "strategy, foo"},
        {"title": "HighPri", "body": "b", "priority": "high"},
        {"title": "Plain", "body": "b", "tags": "technical"},
    ]
    selected = handoff_module._select_strategic_notes(notes)
    titles = [n["title"] for n in selected]
    assert "Insight one" in titles
    assert "Strat" in titles
    assert "HighPri" in titles
    assert "Plain" not in titles
    # High priority sorts first
    assert selected[0]["title"] == "HighPri"


def test_build_readiness_block_warnings():
    block = handoff_module._build_readiness_block(None, 0, 0)
    assert "No sprint name set" in block
    assert "No pending sprint items" in block
    assert "No pinned decisions" in block


def test_build_readiness_block_ok():
    block = handoff_module._build_readiness_block("week-1", 2, 1)
    assert "Sprint: week-1" in block
    assert "2 pending sprint items" in block
    assert "1 pinned decision" in block  # singular


def test_render_workspace_handoff_block_empty_and_full():
    assert handoff_module._render_workspace_handoff_block([], []) == ""
    block = handoff_module._render_workspace_handoff_block(
        [{"title": "WS dec", "body": "body", "category": "TECH"}],
        [{"title": "WS note", "body": "nbody", "tags": "x"}],
    )
    assert "Workspace (applies to all projects)" in block
    assert "WS dec" in block
    assert "WS note" in block


# ---------------------------------------------------------------------------
# 2d6d8677 — per-item body clipping so a handoff can't overflow the MCP result cap
# ---------------------------------------------------------------------------


def test_clip_body_short_body_unchanged():
    """A body at or under the limit passes through verbatim (no marker)."""
    assert handoff_module._clip_body("short body") == "short body"
    assert handoff_module._clip_body("") == ""
    assert handoff_module._clip_body(None) == ""
    exact = "x" * 300
    assert handoff_module._clip_body(exact) == exact  # ==limit, untouched


def test_clip_body_long_body_truncated_with_marker():
    """A body over the limit is cut to the limit and marked with the omitted count."""
    body = "a" * 5000
    clipped = handoff_module._clip_body(body)  # default 300
    # The rendered preview is far shorter than the original 5000 chars.
    assert len(clipped) < 400
    assert clipped.startswith("a" * 200)  # keeps the head
    assert "5000" not in clipped[:300]  # not the whole body
    assert "(+4700 more chars)" in clipped  # 5000 - 300 omitted
    assert body not in clipped  # NOT the full body


def test_clip_body_custom_limit():
    body = "b" * 1000
    clipped = handoff_module._clip_body(body, 100)
    assert "(+900 more chars)" in clipped
    assert len(clipped) < 150


def test_render_custom_handoff_empty_sources_render_none():
    out = handoff_module._render_custom_handoff(
        "T:{{recent_tasks}}|D:{{decisions}}|P:{{pending_items}}|N:{{notes}}|S:{{sprint}}",
        sprint=None,
        north_star=None,
        version_goal=None,
        recent_tasks=[],
        decisions=[],
        pending_items=[],
        notes=[],
    )
    assert out.count("(none)") == 5
    assert out.endswith("\n")


def test_render_custom_handoff_populated_blocks():
    out = handoff_module._render_custom_handoff(
        "{{recent_tasks}}\n{{decisions}}\n{{pending_items}}\n{{notes}}",
        sprint="s",
        north_star="ns",
        version_goal="vg",
        recent_tasks=[{"description": "did a thing", "status": "done"}],
        decisions=[{"title": "Dec", "body": "b", "status": "active", "category": "T"}],
        pending_items=[{"id": "i1", "title": "Pend", "status": "todo"}],
        notes=[{"title": "N", "body": "nb", "tags": "tag"}],
    )
    assert "did a thing" in out
    assert "Dec" in out
    assert "i1" in out
    assert "N" in out


def test_reconcile_sprint_items_confidence():
    pending = [
        {"id": "i1", "title": "fix authentication redirect handler"},
        {"id": "i2", "title": "a the"},  # too few keywords → skipped
    ]
    commits = [
        {"sha": "abc1234567890", "message": "authentication redirect handler reworked"},
    ]
    res = handoff_module.reconcile_sprint_items(pending, commits)
    assert len(res) == 1
    assert res[0]["item_id"] == "i1"
    assert res[0]["confidence"] == "high"  # 3+ keyword overlap


def test_annotate_possibly_done():
    pending = [{"id": "i1", "title": "rewrite postgres adapter cursor"}]
    tasks = [{"description": "rewrite postgres adapter to use cursor pooling"}]
    out = handoff_module._annotate_possibly_done(pending, tasks)
    assert out[0]["possibly_done"] is True
    assert out[0]["possibly_done_matches"]


def test_extract_keywords_extra_stop():
    # 23e79944 — extra_stop drops Meridian-domain terms on top of the base set.
    text = "session handoff notes decisions postgres"
    base = handoff_module._extract_keywords(text)
    assert {"session", "handoff", "notes", "decisions", "postgres"} <= base
    filtered = handoff_module._extract_keywords(
        text, handoff_module._MERIDIAN_STOP_WORDS
    )
    assert filtered == {"postgres"}


def test_annotate_possibly_done_ignores_meridian_stopwords():
    # 23e79944 — an item overlapping a busy task log ONLY on Meridian-domain
    # high-frequency words (session/handoff/notes/decisions/sprint) must NOT be
    # flagged possibly_done. This was the constant false-positive being fixed.
    pending = [{"id": "i1", "title": "session handoff notes decisions sprint"}]
    tasks = [{
        "description": "logged session, generated handoff, pinned decisions, "
                       "updated notes for the sprint board",
    }]
    out = handoff_module._annotate_possibly_done(pending, tasks)
    assert not out[0].get("possibly_done")


def test_annotate_possibly_done_threshold_is_three():
    # 23e79944 — exactly 2 distinctive keyword overlaps is now below threshold.
    pending = [{"id": "i1", "title": "oauth redirect bug"}]
    tasks = [{"description": "fixed the oauth redirect on login"}]  # overlap 2
    out = handoff_module._annotate_possibly_done(pending, tasks)
    assert not out[0].get("possibly_done")
    # 3 distinctive overlaps still flags.
    pending2 = [{"id": "i2", "title": "oauth redirect cookie bug"}]
    tasks2 = [{"description": "fixed the oauth redirect cookie on login"}]
    out2 = handoff_module._annotate_possibly_done(pending2, tasks2)
    assert out2[0].get("possibly_done") is True


# ---------------------------------------------------------------------------
# Sprint-item drift detection (7e212375)
# ---------------------------------------------------------------------------


def test_detect_sprint_item_drift_migration_and_commit():
    blocks = (
        ("_migrate_handoffs_table",
         frozenset({"handoffs", "table", "body", "session", "mode"})),
    )
    # 3 tokens shared with a migration block → migration match.
    m = handoff_module.detect_sprint_item_drift(
        "handoffs table body column", migration_blocks=blocks)
    assert any(
        x["kind"] == "migration" and x["ref"] == "_migrate_handoffs_table"
        for x in m)
    # 3 tokens shared with a commit message → commit match.
    m2 = handoff_module.detect_sprint_item_drift(
        "oauth token refresh endpoint",
        recent_commits=[{"sha": "abc123def4567", "message":
                         "feat: oauth token refresh endpoint added"}],
        migration_blocks=(),
    )
    assert any(x["kind"] == "commit" for x in m2)
    # Below the overlap threshold → nothing.
    assert handoff_module.detect_sprint_item_drift(
        "totally novel widget", migration_blocks=blocks, recent_commits=[]) == []
    # <3 keywords → guarded out.
    assert handoff_module.detect_sprint_item_drift(
        "ab cd", migration_blocks=blocks) == []


def test_migration_blocks_includes_known_migrations():
    names = {n for n, _ in handoff_module._migration_blocks()}
    assert "_migrate_handoffs_table" in names
    assert "_migrate_decision_assumption" in names


# ---------------------------------------------------------------------------
# Stop-hook transcript narrative (571b8b60)
# ---------------------------------------------------------------------------


def test_extract_transcript_narrative(tmp_path):
    # 571b8b60 — keep assistant text turns, drop tool_use/tool_result noise.
    f = tmp_path / "transcript.jsonl"
    lines = [
        json.dumps({"type": "assistant", "message": {"role": "assistant",
                    "content": [
                        {"type": "text", "text": "Implemented the OAuth refresh endpoint."},
                        {"type": "tool_use", "name": "Edit", "input": {"secret": "xyzsecret"}},
                    ]}}),
        json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "file-written-output"},
        ]}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant",
                    "content": "Added tests and they pass."}}),
        "not json at all",
    ]
    f.write_text("\n".join(lines), encoding="utf-8")
    nar = handoff_module.extract_transcript_narrative(str(f))
    assert "OAuth refresh endpoint" in nar
    assert "Added tests and they pass." in nar
    assert "xyzsecret" not in nar       # tool input excluded
    assert "file-written-output" not in nar  # tool result excluded


def test_extract_transcript_narrative_missing_file_returns_empty():
    assert handoff_module.extract_transcript_narrative("/no/such/path.jsonl") == ""


def test_extract_transcript_narrative_caps_length(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text(
        json.dumps({"message": {"role": "assistant", "content": "x" * 10000}}),
        encoding="utf-8",
    )
    nar = handoff_module.extract_transcript_narrative(str(f), max_chars=100)
    assert len(nar) <= 101  # 100 chars + the leading ellipsis


@pytest.mark.asyncio
async def test_generate_handoff_includes_extra_narrative(db, tmp_path):
    # 571b8b60 — extra_narrative is folded into the delta body.
    p = await db_module.create_project(db, "narr-proj")
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="delta",
        extra_narrative="Did the thing and verified it.",
    )
    assert "Session narrative (from transcript)" in content
    assert "Did the thing and verified it." in content


# ---------------------------------------------------------------------------
# Handoff history table (8819d6b1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_and_list_handoffs(db):
    # 8819d6b1 — record + list newest-first + latest + session scoping + by-id.
    p = await db_module.create_project(db, "handoff-history")
    h1 = await db_module.record_handoff(
        db, p["id"], "full", "body one", session_id="s1"
    )
    assert h1["id"] and h1["mode"] == "full" and h1["body"] == "body one"
    assert h1["project_id"] == p["id"] and h1["session_id"] == "s1"
    # Force h1 older so ordering is deterministic regardless of clock resolution.
    await db.execute(
        "UPDATE handoffs SET created_at = '2000-01-01 00:00:00' WHERE id = ?",
        (h1["id"],),
    )
    await db.commit()
    h2 = await db_module.record_handoff(
        db, p["id"], "delta", "body two", session_id="s2"
    )
    rows = await db_module.get_handoffs(db, p["id"])
    assert [r["id"] for r in rows] == [h2["id"], h1["id"]]  # newest first
    latest = await db_module.get_latest_handoff(db, p["id"])
    assert latest["id"] == h2["id"]
    # session scoping
    only_s1 = await db_module.get_handoffs(db, p["id"], session_id="s1")
    assert len(only_s1) == 1 and only_s1[0]["id"] == h1["id"]
    # fetch by id
    assert (await db_module.get_handoff(db, h1["id"]))["body"] == "body one"


@pytest.mark.asyncio
async def test_get_latest_handoff_none_when_empty(db):
    p = await db_module.create_project(db, "handoff-empty")
    assert await db_module.get_latest_handoff(db, p["id"]) is None
    assert await db_module.get_handoffs(db, p["id"]) == []


@pytest.mark.asyncio
async def test_generate_handoff_persists_history_row(db, tmp_path):
    # 8819d6b1 — generate_handoff (full) writes a handoffs row linked to session.
    p = await db_module.create_project(db, "handoff-persist")
    await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True,
        mode="full", session_id="sess-x",
    )
    rows = await db_module.get_handoffs(db, p["id"])
    assert len(rows) >= 1
    assert rows[0]["mode"] == "full"
    assert rows[0]["session_id"] == "sess-x"
    assert rows[0]["body"]


@pytest.mark.asyncio
async def test_migrate_handoffs_table_idempotent(db):
    # Re-running the migration must be a no-op (init_db already ran it).
    from meridian.db.migrations import _migrate_handoffs_table
    await _migrate_handoffs_table(db)
    await _migrate_handoffs_table(db)


# ---------------------------------------------------------------------------
# generate_handoff — modes & branches (db fixture, direct)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_handoff_project_not_found(db, tmp_path):
    with pytest.raises(ValueError):
        await handoff_module.generate_handoff(db, "nope", str(tmp_path))


@pytest.mark.asyncio
async def test_generate_handoff_bad_mode(db, tmp_path):
    p = await db_module.create_project(db, "alpha-badmode")
    with pytest.raises(ValueError):
        await handoff_module.generate_handoff(
            db, p["id"], str(tmp_path), mode="bogus"
        )


@pytest.mark.asyncio
async def test_generate_handoff_no_goal_uses_placeholder(db, tmp_path):
    """No goal set → handoff still renders with placeholder + warnings."""
    p = await db_module.create_project(db, "alpha-nogoal")
    await db_module.upsert_sprint_version_description(
        db, p["id"], "", "Project done when A < B & C > D."
    )
    path, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert "HANDOFF READINESS" in content
    assert "No sprint name set" in content
    assert "No pending sprint items" in content
    assert "No pinned decisions" in content
    assert "Project done when A &lt; B &amp; C &gt; D." in content
    assert path.endswith(f"{handoff_module.handoff_file_stem(p['id'])}_handoff.md")

    for mode in ("delta", "starter", "compact", "goal"):
        _, mode_content, _ = await handoff_module.generate_handoff(
            db,
            p["id"],
            str(tmp_path),
            skip_ai_summary=True,
            mode=mode,
        )
        assert "Project done when A &lt; B &amp; C &gt; D." in mode_content


@pytest.mark.asyncio
async def test_generate_handoff_same_name_projects_no_path_collision(db, tmp_path):
    """44fc189d regression: the ambient handoff file is keyed on project_id, not
    the display-name slug, so two DIFFERENT projects sharing the SAME name write
    to two DIFFERENT files instead of silently colliding/overwriting each other
    in the shared process-global data_dir.

    ``projects.name`` is UNIQUE *within one database*, but each real tenant
    gets its own isolated (Neon) Postgres database — the collision this item
    guards against is across TWO DIFFERENT tenant databases sharing ONE
    process-global data_dir (see ``_deps._data_dir``), so this test models
    that with two independent db connections rather than one shared db.
    """
    from pathlib import Path as _Path

    db2 = await db_module.init_db(":memory:")
    try:
        p1 = await db_module.create_project(db, "duplicate-name")
        p2 = await db_module.create_project(db2, "duplicate-name")
        assert p1["id"] != p2["id"]
        await db_module.set_goal(db, p1["id"], "project one goal")
        await db_module.set_goal(db2, p2["id"], "project two goal")

        path1, content1, _ = await handoff_module.generate_handoff(
            db, p1["id"], str(tmp_path), skip_ai_summary=True
        )
        path2, content2, _ = await handoff_module.generate_handoff(
            db2, p2["id"], str(tmp_path), skip_ai_summary=True
        )

        assert path1 != path2
        assert path1.endswith(
            f"{handoff_module.handoff_file_stem(p1['id'])}_handoff.md"
        )
        assert path2.endswith(
            f"{handoff_module.handoff_file_stem(p2['id'])}_handoff.md"
        )
        assert "project one goal" in content1
        assert "project two goal" in content2
        # Both files landed on disk independently — neither overwrote the other.
        assert _Path(path1).exists()
        assert _Path(path2).exists()
        assert _Path(path1).read_text(encoding="utf-8") == content1
        assert _Path(path2).read_text(encoding="utf-8") == content2
    finally:
        await db2.close()


@pytest.mark.asyncio
async def test_generate_handoff_full_with_decisions_and_workspace(db, tmp_path):
    """Full handoff including pinned decisions, workspace block, sprint."""
    p = await db_module.create_project(db, "alpha-rich")
    await db_module.set_goal(
        db, p["id"], "ship rich handoff", sprint="sprint-rich"
    )
    await db_module.pin_decision(
        db, p["id"], "Use psycopg3", "asyncpg DLL issues", "TECHNICAL"
    )
    await db_module.pin_workspace_decision(
        db, "Workspace rule", "applies everywhere", "STRATEGIC"
    )
    await db_module.add_workspace_note(db, "WS note title", "ws note body", "x")
    s = await db_module.register_session(db, p["id"], "sess-rich")
    await db_module.log_task(db, s["id"], p["id"], "did the rich thing", "done")
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert "Use psycopg3" in content
    assert "Workspace (applies to all projects)" in content
    assert "Workspace rule" in content
    assert "Sprint: sprint-rich" in content
    assert "1 pinned decision" in content


@pytest.mark.asyncio
async def test_generate_handoff_clips_long_bodies(db, tmp_path):
    """2d6d8677 — long decision / workspace-note / task bodies are clipped to a
    ~300-char preview with a truncation marker, NOT dumped at full length (the
    420K-blob-over-MCP-cap bug). Short bodies stay intact."""
    long_dec = "D" * 5000
    long_note = "N" * 5000
    long_task = "T" * 5000
    p = await db_module.create_project(db, "alpha-longbody")
    await db_module.set_goal(db, p["id"], "clip long bodies", sprint="s-clip")
    await db_module.pin_decision(db, p["id"], "Long decision", long_dec, "TECHNICAL")
    await db_module.pin_decision(db, p["id"], "Short decision", "brief body", "TECHNICAL")
    await db_module.add_workspace_note(db, "Long WS note", long_note, "x")
    s = await db_module.register_session(db, p["id"], "sess-clip")
    await db_module.log_task(db, s["id"], p["id"], long_task, "done")
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    # None of the full 5000-char bodies survive verbatim.
    assert long_dec not in content
    assert long_note not in content
    assert long_task not in content
    # But a bounded preview + truncation marker does.
    assert "D" * 200 in content
    assert "N" * 200 in content
    assert "(+4700 more chars)" in content  # 5000 - 300 clip
    # Titles and short bodies stay intact.
    assert "Long decision" in content
    assert "Short decision" in content
    assert "brief body" in content


@pytest.mark.asyncio
async def test_generate_handoff_total_size_bounded_with_many_long_items(db, tmp_path):
    """2d6d8677 — a realistic board of dozens of long-bodied decisions + notes
    keeps the whole rendered handoff comfortably under the MCP result cap. Without
    the per-body clip this fixture alone would be ~300K+ chars; with it, well under."""
    p = await db_module.create_project(db, "alpha-bulk")
    await db_module.set_goal(db, p["id"], "bulk clip", sprint="s-bulk")
    s = await db_module.register_session(db, p["id"], "sess-bulk")
    huge = "Z" * 8000
    for i in range(30):
        await db_module.pin_decision(db, p["id"], f"Decision {i}", huge, "TECHNICAL")
        await db_module.add_project_note(
            db, p["id"], f"Insight {i}", huge, tags="strategy", kind="insight",
            priority="high",
        )
        await db_module.log_task(db, s["id"], p["id"], huge, "done")
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    # 30 decisions + 30 insight notes + 30 tasks × 8000 chars would be >700K raw;
    # clipped, the whole handoff stays well under a conservative bound.
    assert len(content) < 60_000, f"handoff too large: {len(content)} chars"
    assert huge not in content  # no full 8000-char body leaked through


@pytest.mark.asyncio
async def test_generate_handoff_with_ai_summary_stub(db, tmp_path):
    """summarizer stub injects the ai_summary blurb (non-skip path)."""
    p = await db_module.create_project(db, "alpha-aisum")
    await db_module.set_goal(db, p["id"], "ship", sprint="s")
    s = await db_module.register_session(db, p["id"], "sess-ai")
    await db_module.log_task(db, s["id"], p["id"], "made progress", "done")

    def _summarizer(prompt):
        return "STUB SUMMARY: did work, do more."

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), summarizer=_summarizer, skip_ai_summary=False
    )
    assert "STUB SUMMARY" in content


@pytest.mark.asyncio
async def test_generate_handoff_checkpoint_true_bounds_delta_content(db, tmp_path):
    """60eed526 — checkpoint=True resolves the RETURNED content's byte
    budget to _DEFAULT_CHECKPOINT_MAX_BYTES for full/delta modes (the
    mode-aware sentinel default, alongside starter=16000/goal=12000 from
    248c0bb9), closing the confirmed ~139KB checkpoint() regression. A plain
    checkpoint=False call to the SAME mode (every pre-existing caller) is
    completely unaffected — full/delta keeps the generous, unbounded-by-
    default budget. An explicit max_content_bytes argument still always wins
    over the checkpoint-aware default, exactly as for every other mode."""
    p = await db_module.create_project(db, "ckpt-flag-test")
    await db_module.set_goal(db, p["id"], "ship", sprint="s")
    # ~64,000 chars from one field alone — comfortably over
    # _DEFAULT_CHECKPOINT_MAX_BYTES, well under the unbounded full/delta cap.
    # .strip()'d up front (not just at assertion time) because
    # tool_requirements.py's own validation strips the stored `purpose`
    # field — comparing against the un-stripped literal would spuriously
    # fail even on genuinely unbounded/untruncated content.
    huge_purpose = ("context " * 8000).strip()
    await db_module.add_sprint_item(
        db, p["id"], "v1", "solo item",
        tool_requirements=[{
            "name": "find_symbol", "server_or_namespace": "Serena",
            "required_or_preferred": "required", "purpose": huge_purpose,
        }],
    )

    _, checkpoint_content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="delta",
        session_id="sess-ckpt-flag", checkpoint=True,
    )
    assert (
        len(checkpoint_content.encode("utf-8"))
        <= handoff_module._DEFAULT_CHECKPOINT_MAX_BYTES
    )
    assert "TRUNCATED" in checkpoint_content
    assert huge_purpose not in checkpoint_content

    _, plain_delta_content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="delta",
        session_id="sess-ckpt-flag-plain", checkpoint=False,
    )
    # checkpoint=False (the default) keeps the unbounded, byte-for-byte
    # unchanged full/delta contract: the huge field survives untruncated.
    assert huge_purpose in plain_delta_content
    assert "TRUNCATED" not in plain_delta_content

    # An explicit max_content_bytes always wins over the checkpoint-aware default.
    _, explicit_content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="delta",
        session_id="sess-ckpt-flag-explicit", checkpoint=True,
        max_content_bytes=None,
    )
    assert huge_purpose in explicit_content
    assert "TRUNCATED" not in explicit_content


@pytest.mark.asyncio
async def test_generate_handoff_delta_with_in_progress(db, tmp_path):
    """Delta mode surfaces 'Currently running' for in_progress items."""
    p = await db_module.create_project(db, "alpha-delta-ip")
    await db_module.set_goal(db, p["id"], "delta work")
    # 94c26322 — prospect_bypass=True so the claim gate passes (not testing the gate here)
    running = await db_module.add_sprint_item(
        db, p["id"], "v1", "Running item", prospect_bypass=True
    )
    await db_module.add_sprint_item(db, p["id"], "v1", "Pending item")
    await db_module.claim_sprint_item(db, p["id"], running["id"])
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="delta",
        session_id="sess-delta-ip",
    )
    assert "Currently running:" in content
    assert f"- {running['id']} — Running item" in content
    assert "Pending item" in content


@pytest.mark.asyncio
async def test_generate_handoff_delta_empty(db, tmp_path):
    """Delta mode with no items shows 'none' placeholders."""
    p = await db_module.create_project(db, "alpha-delta-empty")
    await db_module.set_goal(db, p["id"], "nothing pending")
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="delta",
        session_id="sess-delta-empty",
    )
    assert "Completed since last handoff:" in content
    assert "- none" in content


# ---------------------------------------------------------------------------
# aef94e4a — sprint retrospective note
# ---------------------------------------------------------------------------


def test_build_retro_prompt_names_three_sections():
    items = [{"title": "Ship auth fix", "status": "done"},
             {"title": "Add graph snapshot", "status": "done"}]
    decisions = [{"decision": "Use psycopg3", "status": "active"}]
    prompt = handoff_module._build_retro_prompt(items, decisions, "v0.1.x")
    assert "What shipped" in prompt
    assert "Patterns revealed" in prompt
    assert "Direction confirmed" in prompt
    assert "Ship auth fix" in prompt
    assert "v0.1.x" in prompt
    assert "Use psycopg3" in prompt
    # the retro prompt is a stable discriminator vs. the ai_summary prompt
    assert "sprint retrospective" in prompt


def test_render_retro_fallback_lists_shipped_and_handles_empty():
    items = [{"title": "Ship auth fix", "status": "done"}]
    body = handoff_module._render_retro_fallback(items, "v1")
    assert "Ship auth fix" in body
    assert "1 item" in body
    assert handoff_module._render_retro_fallback([], None).startswith("No items")


def _retro_summarizer(prompt):
    # Discriminate the retro prompt from the ai_summary prompt (both share the
    # injected summarizer) so tests assert on the retrospective body only.
    if "sprint retrospective" in prompt:
        return "RETRO STUB: what shipped, patterns, direction."
    return "AI SUMMARY STUB."


@pytest.mark.asyncio
async def test_generate_handoff_writes_retrospective_note(db, tmp_path):
    p = await db_module.create_project(db, "retro-proj")
    await db_module.set_goal(db, p["id"], "ship", sprint="v0.1.x")
    it = await db_module.add_sprint_item(db, p["id"], "v0.1.x", "Ship the retro")
    await db_module.complete_sprint_item(db, p["id"], it["id"])

    await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), summarizer=_retro_summarizer,
        skip_ai_summary=False,
    )
    notes = await db_module.get_project_notes(
        db, p["id"], tag="retrospective", bodies=True
    )
    assert len(notes) == 1
    n = notes[0]
    assert n["body"] == "RETRO STUB: what shipped, patterns, direction."
    assert (n.get("note_kind") or n.get("kind")) == "insight"
    assert "retrospective" in (n.get("tags") or "")
    assert n["priority"] == "high"
    assert n["title"] == "Sprint Retrospective — v0.1.x"


@pytest.mark.asyncio
async def test_generate_handoff_retrospective_is_idempotent(db, tmp_path):
    p = await db_module.create_project(db, "retro-idem")
    await db_module.set_goal(db, p["id"], "ship", sprint="v9")
    it = await db_module.add_sprint_item(db, p["id"], "v9", "Item one")
    await db_module.complete_sprint_item(db, p["id"], it["id"])

    seq = {"n": 0}

    def _seq_summarizer(prompt):
        if "sprint retrospective" in prompt:
            seq["n"] += 1
            return f"RETRO#{seq['n']}"
        return "AI SUMMARY STUB."

    for _ in range(2):
        await handoff_module.generate_handoff(
            db, p["id"], str(tmp_path), summarizer=_seq_summarizer,
            skip_ai_summary=False,
        )
    notes = await db_module.get_project_notes(
        db, p["id"], tag="retrospective", bodies=True
    )
    assert len(notes) == 1  # updated in place, not duplicated
    assert notes[0]["body"] == "RETRO#2"


@pytest.mark.asyncio
async def test_generate_handoff_skip_ai_summary_no_retrospective(db, tmp_path):
    p = await db_module.create_project(db, "retro-skip")
    await db_module.set_goal(db, p["id"], "ship", sprint="v1")
    it = await db_module.add_sprint_item(db, p["id"], "v1", "Item")
    await db_module.complete_sprint_item(db, p["id"], it["id"])
    await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True,
    )
    notes = await db_module.get_project_notes(db, p["id"], tag="retrospective")
    assert notes == []


@pytest.mark.asyncio
async def test_generate_handoff_no_completed_items_no_retrospective(db, tmp_path):
    p = await db_module.create_project(db, "retro-none")
    await db_module.set_goal(db, p["id"], "ship", sprint="v1")
    await db_module.add_sprint_item(db, p["id"], "v1", "Still pending")
    await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), summarizer=_retro_summarizer,
        skip_ai_summary=False,
    )
    notes = await db_module.get_project_notes(db, p["id"], tag="retrospective")
    assert notes == []


# ---------------------------------------------------------------------------
# 78ebc812 — _annotate_touches_files persists to touches_resources (the column
# that actually exists), not the phantom touches_files column.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_annotate_touches_files_writes_touches_resources(db, tmp_path, monkeypatch):
    """The auto-inferred file match round-trips: _annotate_touches_files must
    persist typed touches_resources ids that get_sprint_items reads back — the
    old touches_files UPDATE targeted a non-existent column and failed silently."""
    import subprocess as _subprocess

    class _R:
        stdout = "meridian/pg_adapter.py\n"

    monkeypatch.setattr(_subprocess, "run", lambda *a, **k: _R())

    p = await db_module.create_project(db, "touches-proj")
    it = await db_module.add_sprint_item(
        db, p["id"], "v1", "rewrite pg_adapter cursor handling"
    )

    pending = [{"id": it["id"], "title": "rewrite pg_adapter cursor handling",
                "status": "pending"}]
    out = await handoff_module._annotate_touches_files(db, p["id"], pending)

    # In-memory annotation set the typed, provenance-marked resource id.
    assert out[0]["touches_resources"]
    assert "inferred:file:meridian/pg_adapter.py" in out[0]["touches_resources"]

    # And it actually persisted — get_sprint_items reads it back (previously the
    # write hit a phantom touches_files column and silently no-op'd).
    items = await db_module.get_sprint_items(db, p["id"])
    stored = next(i for i in items if i["id"] == it["id"])
    resources = db_module.parse_touches_resources(stored.get("touches_resources"))
    assert "file:meridian/pg_adapter.py" in resources


# ---------------------------------------------------------------------------
# 093d55e0 — retrospective idempotency keys on the sprint VERSION, not the
# verbose (drift-prone) sprint description embedded in the note title.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrospective_idempotent_across_sprint_text_drift(db, tmp_path):
    """Two retro persists for the SAME sprint version but DIFFERENT sprint text
    (which drifts the rendered title) must update ONE note in place, not insert a
    duplicate. Previously the title-embedded verbose description broke the match."""
    p = await db_module.create_project(db, "retro-drift")

    # First persist: verbose sprint description A.
    await handoff_module._persist_sprint_retrospective(
        db, p["id"], "v0.2.x — wire tunnel, add graph searcher, ship docs",
        "RETRO BODY A", version=7,
    )
    # Second persist: SAME version, reworded/expanded description (title drifts).
    await handoff_module._persist_sprint_retrospective(
        db, p["id"],
        "v0.2.x — wire tunnel; add graph searcher; ship docs; fix flaky test",
        "RETRO BODY B", version=7,
    )

    notes = await db_module.get_project_notes(
        db, p["id"], tag="retrospective", bodies=True
    )
    assert len(notes) == 1  # updated in place, not duplicated
    assert notes[0]["body"] == "RETRO BODY B"
    # The stable per-version tag is present so future runs match regardless of drift.
    assert "retro:v7" in (notes[0].get("tags") or "")


@pytest.mark.asyncio
async def test_retrospective_distinct_versions_are_separate_notes(db, tmp_path):
    """Different sprint versions get their own retro notes (the stable key is
    per-version, so it must not collapse distinct sprints into one)."""
    p = await db_module.create_project(db, "retro-versions")
    await handoff_module._persist_sprint_retrospective(
        db, p["id"], "v1 sprint", "BODY V1", version=1,
    )
    await handoff_module._persist_sprint_retrospective(
        db, p["id"], "v2 sprint", "BODY V2", version=2,
    )
    notes = await db_module.get_project_notes(
        db, p["id"], tag="retrospective", bodies=True
    )
    assert len(notes) == 2


@pytest.mark.asyncio
async def test_generate_handoff_planner_mode_full_content(db, tmp_path):
    """Planner mode emits a directive planning prompt: tool-order protocol,
    sprint-items-to-review (real pending item), open-HITL section (real HITL),
    strategic context, and the thinking scaffold."""
    p = await db_module.create_project(db, "alpha-planner")
    await db_module.set_goal(
        db, p["id"], "vision", north_star="Be the best", sprint="plan-sprint"
    )
    await db_module.pin_decision(
        db, p["id"], "Planner decision", "rationale here", "STRATEGIC"
    )
    await db_module.add_project_note(
        db, p["id"], "Strat note", "strategic body", "strategy"
    )
    await db_module.request_hitl(db, p["id"], "Rate limit per IP or token?")
    await db_module.add_sprint_item(db, p["id"], "v1", "Planner pending item")
    s = await db_module.register_session(db, p["id"], "sess-plan")
    await db_module.log_task(db, s["id"], p["id"], "planner task", "done")
    path, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), mode="planner"
    )
    # Framed as a planning session, not a data dump.
    assert "Meridian Planning Session — alpha-planner" in content
    # The call-these-tools-in-this-order protocol, using the real tool names.
    assert "Planning protocol — call these tools in this order" in content
    assert "get_planning_brief(" in content
    assert "get_sprint_progress(" in content
    assert "list_hitl_requests(" in content
    assert "get_pinned_decisions(" in content
    # Strategic frame carried over.
    assert "Be the best" in content
    assert "plan-sprint" in content
    assert "Planner decision" in content
    assert "Strat note" in content
    # Sprint items to review — the real pending item shows up.
    assert "## Sprint items to review" in content
    assert "Planner pending item" in content
    # Open decisions (HITL) — the real open HITL question shows up.
    assert "## Open decisions (HITL)" in content
    assert "Rate limit per IP or token?" in content
    # Recent activity + thinking scaffold sections.
    assert "planner task" in content
    assert "## Thinking scaffold" in content
    assert "### Current state" in content
    assert "### Gaps & risks" in content
    assert "### Priorities" in content
    assert "### Proposed next sprint items" in content
    assert "### Open questions" in content
    assert path.endswith(
        f"{handoff_module.handoff_file_stem(p['id'])}_planner_handoff.md"
    )


@pytest.mark.asyncio
async def test_generate_handoff_planner_mode_minimal(db, tmp_path):
    """Planner mode with no goal/items/HITLs still renders a clean prompt with
    'none' placeholders rather than crashing."""
    p = await db_module.create_project(db, "alpha-planner-min")
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), mode="planner"
    )
    assert "## North Star" in content
    assert "(not set)" in content
    # Protocol + scaffold are always present.
    assert "Planning protocol — call these tools in this order" in content
    assert "## Thinking scaffold" in content
    # Empty backlog + empty HITL queue render the "none" placeholders.
    assert "## Sprint items to review" in content
    assert "## Open decisions (HITL)" in content
    assert content.count("- none") >= 2


@pytest.mark.asyncio
async def test_generate_handoff_starter_and_compact(db, tmp_path):
    """starter and compact both route through the starter renderer."""
    p = await db_module.create_project(db, "alpha-starter-cov")
    await db_module.set_goal(db, p["id"], "starter goal")
    it1 = await db_module.add_sprint_item(db, p["id"], "v1", "Done item")
    it2 = await db_module.add_sprint_item(db, p["id"], "v1", "Open item")
    await db_module.complete_sprint_item(db, p["id"], it1["id"])
    for mode in ("starter", "compact"):
        path, content, _ = await handoff_module.generate_handoff(
            db, p["id"], str(tmp_path), mode=mode
        )
        assert f'start_session(project_name="{p["name"]}"' in content  # 11a91d31
        assert f'project_id (fallback): {p["id"]}' in content
        assert it2["id"][:8] in content
        assert "Done:" in content
        assert path.endswith(f"{handoff_module.handoff_file_stem(p['id'])}_starter.md")


@pytest.mark.asyncio
async def test_generate_handoff_starter_no_completed(db, tmp_path):
    """Starter renders 'Done: (none)' and 'Pending (none)' when empty."""
    p = await db_module.create_project(db, "alpha-starter-empty")
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), mode="starter"
    )
    assert "Done: (none)" in content
    assert "(none)" in content


@pytest.mark.asyncio
async def test_generate_handoff_appends_queued_session(db, tmp_path):
    """Queued next-session goal is appended once then cleared."""
    p = await db_module.create_project(db, "alpha-queued")
    await db_module.set_goal(db, p["id"], "queued goal")
    await db_module.set_queued_session(db, p["id"], "/goal do the queued thing")
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert "QUEUED NEXT SESSION" in content
    assert "do the queued thing" in content
    # Second call — queue cleared, no longer present.
    _, content2, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert "QUEUED NEXT SESSION" not in content2


@pytest.mark.asyncio
async def test_generate_handoff_l0_fallback(db, tmp_path):
    """_generate_handoff_l0 writes a minimal north-star + decisions file."""
    p = await db_module.create_project(db, "alpha-l0")
    await db_module.set_goal(
        db, p["id"], "l0 content", north_star="L0 star", sprint="l0-sprint"
    )
    await db_module.pin_decision(db, p["id"], "L0 dec", "L0 body", "TECHNICAL")
    path, content = await handoff_module._generate_handoff_l0(
        db, p["id"], str(tmp_path)
    )
    assert "L0 fallback" in content
    assert "L0 star" in content
    assert "l0-sprint" in content
    assert "L0 dec" in content
    assert path.endswith(f"{handoff_module.handoff_file_stem(p['id'])}_handoff.md")


@pytest.mark.asyncio
async def test_generate_ai_summary_fallback_no_key(db, tmp_path, monkeypatch):
    """No ANTHROPIC_API_KEY and no summarizer → fallback to first task desc."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    text = await handoff_module._generate_ai_summary(
        [{"description": "the first task", "status": "done"}], "sprint"
    )
    assert "the first task" in text


@pytest.mark.asyncio
async def test_generate_ai_summary_empty_tasks(db, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    text = await handoff_module._generate_ai_summary([], None)
    assert text == "No recent activity logged."


@pytest.mark.asyncio
async def test_generate_ai_summary_summarizer_dict_result(db):
    def _summarizer(prompt):
        return {"text": "dict-based summary"}

    text = await handoff_module._generate_ai_summary(
        [{"description": "t", "status": "done"}], "s", summarizer=_summarizer
    )
    assert text == "dict-based summary"


@pytest.mark.asyncio
async def test_generate_ai_summary_summarizer_raises_falls_back(db, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def _summarizer(prompt):
        raise RuntimeError("boom")

    text = await handoff_module._generate_ai_summary(
        [{"description": "fallback desc", "status": "done"}], "s",
        summarizer=_summarizer,
    )
    assert "fallback desc" in text


# ---------------------------------------------------------------------------
# routes/handoff.py — HTTP endpoints
# ---------------------------------------------------------------------------


def test_post_handoff_endpoint_full(client):
    project = client.post("/projects", json={"name": "http-full"}).json()
    client.post(f"/projects/{project['id']}/goal", json={"content": "ship http"})
    r = client.post(f"/projects/{project['id']}/handoff")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "full"
    assert "ship http" in body["content"]
    assert body["path"].endswith(
        f"{handoff_module.handoff_file_stem(project['id'])}_handoff.md"
    )


def test_post_handoff_endpoint_404(client):
    r = client.post("/projects/does-not-exist/handoff")
    assert r.status_code == 404


def test_post_handoff_endpoint_starter_mode(client):
    project = client.post("/projects", json={"name": "http-starter"}).json()
    client.post(f"/projects/{project['id']}/goal", json={"content": "g"})
    r = client.post(
        f"/projects/{project['id']}/handoff", json={"mode": "starter"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "starter"
    assert "start_session" in body["content"]


def test_post_handoff_endpoint_invalid_json_body(client):
    """Non-dict / bad JSON body is tolerated → defaults to full mode."""
    project = client.post("/projects", json={"name": "http-badjson"}).json()
    r = client.post(
        f"/projects/{project['id']}/handoff",
        content=b"not json at all",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["mode"] == "full"


def test_planner_handoff_endpoint(client):
    project = client.post("/projects", json={"name": "http-planner"}).json()
    client.post(
        f"/projects/{project['id']}/goal",
        json={"content": "vision", "north_star": "be great"},
    )
    r = client.get(f"/projects/{project['id']}/handoff/planner")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "planner"
    assert "Meridian Planning Session" in body["content"]
    assert "be great" in body["content"]


def test_planner_handoff_endpoint_404(client):
    r = client.get("/projects/missing/handoff/planner")
    assert r.status_code == 404


def test_post_handoff_endpoint_timeout_falls_back_to_l0(client, monkeypatch):
    """POST handoff: generate_handoff timeout → L0 fallback path."""
    from meridian.routes import handoff as routes_handoff

    project = client.post("/projects", json={"name": "http-timeout"}).json()
    client.post(f"/projects/{project['id']}/goal", json={"content": "tg"})

    async def _boom(*a, **k):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(
        routes_handoff.handoff_module, "generate_handoff", _boom
    )
    r = client.post(f"/projects/{project['id']}/handoff")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "full"
    assert "L0 fallback" in body["content"]


def test_planner_handoff_endpoint_timeout_returns_504(client, monkeypatch):
    """GET planner handoff: timeout → HTTP 504."""
    from meridian.routes import handoff as routes_handoff

    project = client.post("/projects", json={"name": "http-planner-timeout"}).json()

    async def _boom(*a, **k):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(
        routes_handoff.handoff_module, "generate_handoff", _boom
    )
    r = client.get(f"/projects/{project['id']}/handoff/planner")
    assert r.status_code == 504


# ---------------------------------------------------------------------------
# 45f519a0/8a883f60/eb8b6894 — POST /projects/{id}/handoff previously silently
# dropped force_include_ids/strict_evidence/strict_pointer_evidence from the
# request body even though the MCP HTTP dispatch (handler.py) already threaded
# all three. These tests prove the REST route now forwards them for real.
# ---------------------------------------------------------------------------


def test_post_handoff_endpoint_force_include_ids_threads_through(client):
    """force_include_ids in the POST body re-includes a deferred item that a
    plain call (no override) leaves hidden — proving the REST route actually
    forwards the field to generate_handoff instead of dropping it."""
    project = client.post("/projects", json={"name": "http-force-include"}).json()
    pid = project["id"]
    future = "2099-01-01T00:00:00"
    item = _run(db_module.add_sprint_item(
        client.app.state.db, pid, "v1", "HTTP deferred task", deferred_until=future,
    ))

    # Baseline: without force_include_ids the deferred item stays hidden.
    baseline = client.post(f"/projects/{pid}/handoff")
    assert baseline.status_code == 200
    assert "HTTP deferred task" not in baseline.json()["content"]

    r = client.post(
        f"/projects/{pid}/handoff",
        json={"force_include_ids": [item["id"]]},
    )
    assert r.status_code == 200
    assert "HTTP deferred task" in r.json()["content"]
    # deferred_until is NOT cleared by the one-call override.
    refetched = _run(db_module.get_sprint_item(client.app.state.db, item["id"]))
    assert refetched["deferred_until"] == future


def test_post_handoff_endpoint_strict_evidence_returns_structured_422(client, monkeypatch):
    """strict_evidence=True on a failed capability now returns a structured
    422 (HANDOFF_EVIDENCE_BLOCKED) instead of falling through to a generic
    500, mirroring the MCP HTTP dispatch's own HandoffEvidenceRequired
    refusal shape (see test_generate_handoff_strict_evidence_raises_and_writes_nothing
    above for the underlying handoff_module-level behavior this wraps)."""
    project = client.post("/projects", json={"name": "http-strict-evidence"}).json()
    pid = project["id"]
    client.post(
        f"/projects/{pid}/sprint-items", json={"version": "v1", "title": "pending item"},
    )

    async def _boom(*_a, **_k):
        raise RuntimeError("wave_gate_configs table missing")

    monkeypatch.setattr(db_module, "get_wave_gate_configs", _boom)

    r = client.post(f"/projects/{pid}/handoff", json={"strict_evidence": True})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "HANDOFF_EVIDENCE_BLOCKED"
    assert detail["project_id"] == pid
    assert any(
        e["capability"] == "wave_gate_exclusion" for e in detail["evidence_errors"]
    )


def test_post_handoff_endpoint_omitting_new_args_is_backward_compatible(client, monkeypatch):
    """Regression: a request body with none of the new keys (force_include_ids/
    strict_evidence/strict_pointer_evidence/version) behaves exactly as
    before — even the SAME failed-capability state that trips strict_evidence
    above must still degrade gracefully (200, not 422/500) when
    strict_evidence is simply absent from the body."""
    project = client.post("/projects", json={"name": "http-backward-compat"}).json()
    pid = project["id"]
    client.post(
        f"/projects/{pid}/sprint-items", json={"version": "v1", "title": "compat item"},
    )

    async def _boom(*_a, **_k):
        raise RuntimeError("wave_gate_configs table missing")

    monkeypatch.setattr(db_module, "get_wave_gate_configs", _boom)

    r = client.post(f"/projects/{pid}/handoff", json={"mode": "full"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "full"
    assert "compat item" in body["content"]


# ---------------------------------------------------------------------------
# ddd8b9bf — hitl_auto_answer_mode adapts /goal HITL clause + agent_instructions
# ---------------------------------------------------------------------------

def test_build_quick_start_goal_hitl_mode_0_says_stop():
    """Mode 0: /goal ends with 'or if HITL triggered'."""
    goal = handoff_module._build_quick_start_goal(
        [{"id": "item-1"}], hitl_auto_answer_mode=0
    )
    assert "or if HITL triggered." in goal
    assert "Do NOT file HITLs" not in goal


def test_build_quick_start_goal_hitl_mode_1_says_skip():
    """Mode 1: /goal says 'Do NOT file HITLs — auto-answer is on'."""
    goal = handoff_module._build_quick_start_goal(
        [{"id": "item-1"}], hitl_auto_answer_mode=1
    )
    assert "Do NOT file HITLs" in goal
    assert "or if HITL triggered." not in goal


def test_build_quick_start_goal_hitl_mode_2_says_skip():
    """Mode 2 (aggressive): same skip clause as mode 1."""
    goal = handoff_module._build_quick_start_goal(
        [{"id": "item-1"}], hitl_auto_answer_mode=2
    )
    assert "Do NOT file HITLs" in goal


def test_build_quick_start_goal_empty_items_hitl_mode_adapts():
    """Empty-items fallback path also adapts the HITL clause."""
    empty_mode0 = handoff_module._build_quick_start_goal([], hitl_auto_answer_mode=0)
    assert "or if HITL triggered." in empty_mode0
    empty_mode2 = handoff_module._build_quick_start_goal([], hitl_auto_answer_mode=2)
    assert "Do NOT file HITLs" in empty_mode2


def test_handoff_start_session_lines_prefer_project_name():
    """11a91d31 — the starter + delta handoff start_session lines default to
    project_name (the idiomatic interface per 8a449ec0); project_id stays only as
    a fallback comment, never as the start_session(project_id=...) call."""
    proj = {"id": "abc-123-uuid", "name": "meridian-build"}
    starter = handoff_module._render_starter_handoff(
        proj, completed_items=[], pending_items=[], quick_start_goal="/goal x",
    )
    assert 'start_session(project_name="meridian-build"' in starter
    assert 'start_session(project_id=' not in starter
    assert "abc-123-uuid" in starter  # present as the fallback reference

    delta = handoff_module._render_delta_handoff(
        proj, generated_at="2026-06-30", completed_items=[],
        in_progress_items=[], pending_sprint_items=[], quick_start_goal="/goal x",
    )
    assert 'start_session(project_name="meridian-build"' in delta
    assert 'start_session(project_id=' not in delta
    assert "project_id=abc-123-uuid" in delta  # inline fallback comment


def test_handoff_start_session_substitutes_caller_identity():
    """bdc251ec — when the authenticated caller identity is known, the starter and
    delta start_session lines pre-fill human_id="<identity>" instead of leaving a
    generic placeholder; when it's unknown, no human_id clause is emitted."""
    proj = {"id": "abc-123-uuid", "name": "meridian-build"}

    starter = handoff_module._render_starter_handoff(
        proj, completed_items=[], pending_items=[], quick_start_goal="/goal x",
        identity="alice",
    )
    assert 'human_id="alice"' in starter
    delta = handoff_module._render_delta_handoff(
        proj, generated_at="2026-06-30", completed_items=[],
        in_progress_items=[], pending_sprint_items=[], quick_start_goal="/goal x",
        identity="alice",
    )
    assert 'human_id="alice"' in delta

    # No identity → no human_id clause (backwards compatible placeholder form).
    starter_none = handoff_module._render_starter_handoff(
        proj, completed_items=[], pending_items=[], quick_start_goal="/goal x",
    )
    assert "human_id=" not in starter_none
    delta_none = handoff_module._render_delta_handoff(
        proj, generated_at="2026-06-30", completed_items=[],
        in_progress_items=[], pending_sprint_items=[], quick_start_goal="/goal x",
    )
    assert "human_id=" not in delta_none


def test_human_id_clause_helper_trims_and_guards():
    """bdc251ec — the shared clause builder only emits when there's a real handle."""
    assert handoff_module._human_id_clause("adam") == ', human_id="adam"'
    assert handoff_module._human_id_clause("  bob  ") == ', human_id="bob"'
    assert handoff_module._human_id_clause("") == ""
    assert handoff_module._human_id_clause(None) == ""


def test_start_session_agent_instructions_includes_hitl_directive(client):
    """start_session agent_instructions leads with both execution-mode and HITL directives."""
    import json as _json
    project = client.post("/projects", json={"name": "hitl-instr-proj"}).json()
    pid = project["id"]
    r = client.post("/mcp/sse", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "start_session",
                   "arguments": {"project_id": pid, "session_name": "hitl-test"}}
    })
    assert r.status_code == 200
    body = r.json()
    result_text = body.get("result", {}).get("content", [{}])[0].get("text", "{}")
    data = _json.loads(result_text)
    ai = data.get("agent_instructions", "")
    assert "EXECUTION MODE:" in ai
    assert "HITL:" in ai


def test_start_session_includes_current_timestamp(client):
    """de193a81 — the start_session response carries current_timestamp so an
    executor session spanning calendar days is anchored to the real date."""
    import json as _json
    import re
    project = client.post("/projects", json={"name": "ts-instr-proj"}).json()
    pid = project["id"]
    r = client.post("/mcp/sse", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "start_session",
                   "arguments": {"project_id": pid, "session_name": "ts-test"}}
    })
    assert r.status_code == 200
    data = _json.loads(r.json()["result"]["content"][0]["text"])
    assert "current_timestamp" in data
    assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", data["current_timestamp"])


# ---------------------------------------------------------------------------
# 5abf3e12 — per-session goal-compliance metric
# ---------------------------------------------------------------------------


async def _seed_session_with_items(db, project_name, n_listed, n_done):
    """Create a project + session, claim ``n_listed`` items for the session
    (actor = session id) and complete the first ``n_done`` of them."""
    p = await db_module.create_project(db, project_name)
    sess = await db_module.register_session(db, p["id"], "exec-sess")
    sid = sess["id"]
    items = []
    for i in range(n_listed):
        # 94c26322 — prospect_bypass=True so the claim gate passes for these
        # test-helper items which have no durable pointers (not testing the gate).
        it = await db_module.add_sprint_item(
            db, p["id"], "v1", f"item {i}", prospect_bypass=True
        )
        await db_module.claim_sprint_item(db, p["id"], it["id"], actor=sid)
        items.append(it)
    for it in items[:n_done]:
        await db_module.complete_sprint_item(db, p["id"], it["id"], actor=sid)
    return p, sid, items


@pytest.mark.asyncio
async def test_goal_compliance_full(db):
    """Full compliance: every item the session took on (N) was completed (M==N)."""
    p, sid, _ = await _seed_session_with_items(db, "gc-full", 3, 3)
    metric = await db_module.compute_session_goal_compliance(db, p["id"], sid)
    assert metric["listed"] == 3
    assert metric["completed"] == 3
    assert metric["fully_completed"] is True
    assert metric["zero_listed"] is False
    assert metric["compliance_pct"] == 100
    assert metric["session_id"] == sid


@pytest.mark.asyncio
async def test_goal_compliance_partial(db):
    """Partial compliance: M < N → not fully completed, pct reflects the ratio."""
    p, sid, _ = await _seed_session_with_items(db, "gc-partial", 4, 1)
    metric = await db_module.compute_session_goal_compliance(db, p["id"], sid)
    assert metric["listed"] == 4
    assert metric["completed"] == 1
    assert metric["fully_completed"] is False
    assert metric["zero_listed"] is False
    assert metric["compliance_pct"] == 25


@pytest.mark.asyncio
async def test_goal_compliance_zero_listed(db):
    """Zero-listed edge case: a session that claimed no items is NOT vacuously
    'fully_completed' — nothing was listed, so the flag is False and zero_listed
    is True."""
    p = await db_module.create_project(db, "gc-zero")
    sess = await db_module.register_session(db, p["id"], "idle-sess")
    metric = await db_module.compute_session_goal_compliance(db, p["id"], sess["id"])
    assert metric["listed"] == 0
    assert metric["completed"] == 0
    assert metric["fully_completed"] is False
    assert metric["zero_listed"] is True
    assert metric["compliance_pct"] == 0


@pytest.mark.asyncio
async def test_goal_compliance_scoped_to_session(db):
    """The metric only counts items attributed to THIS session (actor), not the
    project-wide board — a second session's items are excluded."""
    p = await db_module.create_project(db, "gc-scope")
    s1 = (await db_module.register_session(db, p["id"], "s1"))["id"]
    s2 = (await db_module.register_session(db, p["id"], "s2"))["id"]
    # 94c26322 — prospect_bypass=True so claim gate passes (not testing the gate here)
    a = await db_module.add_sprint_item(db, p["id"], "v1", "a", prospect_bypass=True)
    b = await db_module.add_sprint_item(db, p["id"], "v1", "b", prospect_bypass=True)
    await db_module.claim_sprint_item(db, p["id"], a["id"], actor=s1)
    await db_module.complete_sprint_item(db, p["id"], a["id"], actor=s1)
    await db_module.claim_sprint_item(db, p["id"], b["id"], actor=s2)  # other session, still pending
    m1 = await db_module.compute_session_goal_compliance(db, p["id"], s1)
    m2 = await db_module.compute_session_goal_compliance(db, p["id"], s2)
    assert m1 == {**m1, "listed": 1, "completed": 1, "fully_completed": True}
    assert m2["listed"] == 1 and m2["completed"] == 0 and m2["fully_completed"] is False


@pytest.mark.asyncio
async def test_goal_compliance_cross_session_completion_reattributes(db):
    """5abf3e12 — cross-session hand-off: session A claims an item but session B
    *completes* it (actor=B, as complete_sprint_item does). The item is
    reattributed to the completer, so credit follows B — A no longer counts it,
    B counts it as completed. Pins the documented attribution (complete overwrites
    actor, unlike claim which COALESCEs); this is correct for the common
    single-session loop and defined behaviour for parallel/coordinator patterns.

    8693b6a8 — this is the coordinator/hand-off pattern the claim-ownership
    gate's force_foreign_claim escape hatch exists for: B completing A's live,
    non-stale claim on purpose. Pass force_foreign_claim=True to acknowledge
    that explicitly, same as a real coordinator session would."""
    p = await db_module.create_project(db, "gc-xsession")
    a = (await db_module.register_session(db, p["id"], "claimer"))["id"]
    b = (await db_module.register_session(db, p["id"], "completer"))["id"]
    it = await db_module.add_sprint_item(db, p["id"], "v1", "handed-off item")
    await db_module.claim_sprint_item(db, p["id"], it["id"], actor=a)
    await db_module.complete_sprint_item(
        db, p["id"], it["id"], actor=b, force_foreign_claim=True
    )
    ma = await db_module.compute_session_goal_compliance(db, p["id"], a)
    mb = await db_module.compute_session_goal_compliance(db, p["id"], b)
    # A claimed it but B finalised it → the item is now attributed to B.
    assert ma["listed"] == 0 and ma["zero_listed"] is True
    assert mb["listed"] == 1 and mb["completed"] == 1 and mb["fully_completed"] is True


@pytest.mark.asyncio
async def test_record_session_goal_compliance_persists(db):
    """record_session_goal_compliance stores the metric on sessions.goal_compliance
    and get_session_goal_compliance reads it back verbatim."""
    p, sid, _ = await _seed_session_with_items(db, "gc-store", 2, 1)
    assert await db_module.get_session_goal_compliance(db, sid) is None  # nothing yet
    returned = await db_module.record_session_goal_compliance(db, p["id"], sid)
    stored = await db_module.get_session_goal_compliance(db, sid)
    assert stored == returned
    assert stored["listed"] == 2 and stored["completed"] == 1
    assert stored["fully_completed"] is False


@pytest.mark.asyncio
async def test_generate_handoff_stores_goal_compliance(db, tmp_path):
    """5abf3e12 — the metric is computed & persisted at the canonical session-end
    point (generate_handoff), so every completed session leaves a durable record
    of whether its /goal item list was fully done."""
    p, sid, _ = await _seed_session_with_items(db, "gc-handoff", 2, 2)
    # No metric until the session ends.
    assert await db_module.get_session_goal_compliance(db, sid) is None
    await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, session_id=sid
    )
    stored = await db_module.get_session_goal_compliance(db, sid)
    assert stored is not None
    assert stored["listed"] == 2 and stored["completed"] == 2
    assert stored["fully_completed"] is True


@pytest.mark.asyncio
async def test_generate_handoff_no_session_id_skips_compliance(db, tmp_path):
    """A session-less handoff must not attempt to store a metric (nothing to key
    it on) and must not raise."""
    p = await db_module.create_project(db, "gc-nosess")
    await db_module.add_sprint_item(db, p["id"], "v1", "an item")
    # No session_id → no compliance write, no error.
    path, _, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert path


# ---------------------------------------------------------------------------
# b7f41c73 — datetime-safe delta path (Postgres returns real datetime objects
# for completed_at/added_at/claimed_at where SQLite returns ISO strings).
# ---------------------------------------------------------------------------


def test_iso_ts_coerces_datetime_str_and_none():
    from datetime import datetime as _dt

    # datetime (Postgres) → ISO string.
    out = handoff_module._iso_ts(_dt(2026, 7, 6, 11, 58, 0))
    assert out == "2026-07-06T11:58:00"
    # str (SQLite) passes through.
    assert handoff_module._iso_ts("2026-07-06 11:58:00") == "2026-07-06 11:58:00"
    # None / empty → None.
    assert handoff_module._iso_ts(None) is None
    assert handoff_module._iso_ts("") is None


def test_completed_after_accepts_datetime_operands():
    """b7f41c73 — on Postgres both operands arrive as datetime objects; the old
    str-only .strip() path raised AttributeError. Both str and datetime must work,
    and the two shapes must compare identically."""
    from datetime import datetime as _dt

    later = _dt(2026, 2, 1, 0, 0, 0)
    earlier = _dt(2026, 1, 1, 0, 0, 0)
    # datetime vs datetime
    assert handoff_module._completed_after(later, earlier) is True
    assert handoff_module._completed_after(earlier, later) is False
    # datetime completed_at vs str since_ts (mixed, as PG delta path can hit)
    assert handoff_module._completed_after(later, "2026-01-01 00:00:00") is True
    # str completed_at vs datetime since_ts
    assert handoff_module._completed_after("2026-02-01 00:00:00", earlier) is True
    # None datetime completed_at → False (never after)
    assert handoff_module._completed_after(None, earlier) is False


def test_ts_safe_items_coerces_datetime_fields_and_json_dumps():
    """b7f41c73 — _ts_safe_items must turn datetime timestamp fields into ISO
    strings so json.dumps of the sanitized item never raises 'Object of type
    datetime is not JSON serializable'. Original dicts are left untouched."""
    from datetime import datetime as _dt

    original = {
        "id": "abc",
        "title": "item",
        "completed_at": _dt(2026, 7, 6, 11, 58, 0),
        "added_at": _dt(2026, 7, 5, 9, 0, 0),
        "claimed_at": _dt(2026, 7, 6, 10, 0, 0),
        "status": "done",
    }
    safe = handoff_module._ts_safe_items([original])
    item = safe[0]
    assert isinstance(item["completed_at"], str)
    assert isinstance(item["added_at"], str)
    assert isinstance(item["claimed_at"], str)
    # Sanitized dict is JSON-serializable without a custom default.
    json.dumps(item)
    # Original dict is not mutated (shallow copy).
    assert isinstance(original["completed_at"], _dt)
    # Non-dict entries pass through unharmed.
    assert handoff_module._ts_safe_items(["not-a-dict"]) == ["not-a-dict"]


def test_json_default_serializes_datetime():
    from datetime import datetime as _dt

    payload = {"completed_at": _dt(2026, 7, 6, 11, 58, 0)}
    # Without the default this raises; with it, we get a clean ISO string.
    dumped = json.dumps(payload, default=handoff_module._json_default)
    assert "2026-07-06T11:58:00" in dumped


@pytest.mark.asyncio
async def test_generate_handoff_delta_with_datetime_completed_at(db, tmp_path, monkeypatch):
    """b7f41c73 — regression: simulate Postgres by making get_sprint_items return
    items whose completed_at is a real datetime object, then run the delta path.
    It must not raise (the old code crashed on .strip()/json.dumps of a datetime)
    and must still surface the completed item."""
    from datetime import datetime as _dt

    p = await db_module.create_project(db, "alpha-delta-pg")
    await db_module.set_goal(db, p["id"], "delta pg work")
    done = await db_module.add_sprint_item(db, p["id"], "v1", "Shipped PG item")
    await db_module.complete_sprint_item(db, p["id"], done["id"])

    real_get = db_module.get_sprint_items

    async def _pg_get_sprint_items(conn, project_id, *args, **kwargs):
        rows = await real_get(conn, project_id, *args, **kwargs)
        # Rewrite string timestamps to datetime objects, as psycopg3 would.
        for row in rows:
            for field in ("completed_at", "added_at", "claimed_at"):
                val = row.get(field)
                if isinstance(val, str) and val:
                    try:
                        row[field] = _dt.fromisoformat(val.replace("Z", ""))
                    except ValueError:
                        row[field] = _dt(2026, 7, 6, 11, 58, 0)
            # Guarantee at least completed_at is a datetime on the done item.
            if row.get("status") == "done" and not isinstance(
                row.get("completed_at"), _dt
            ):
                row["completed_at"] = _dt(2026, 7, 6, 11, 58, 0)
        return rows

    monkeypatch.setattr(db_module, "get_sprint_items", _pg_get_sprint_items)
    monkeypatch.setattr(handoff_module.db_module, "get_sprint_items", _pg_get_sprint_items)

    # No prior handoff state → since_ts is None → the done item is "completed after".
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="delta",
        session_id="sess-delta-pg",
    )
    assert "Completed since last handoff:" in content
    assert "Shipped PG item" in content


# ---------------------------------------------------------------------------
# 302db181 — computed session span (first/last activity, distinct days, elapsed).
# ---------------------------------------------------------------------------


def test_compute_session_span_mixed_datetime_and_str():
    """302db181 — the helper must handle a mix of datetime (Postgres) and str
    (SQLite) timestamps, skip None/unparseable entries, and report the correct
    first/last, distinct calendar days, and elapsed span."""
    from datetime import datetime as _dt

    span = handoff_module.compute_session_span([
        "2026-07-05 09:00:00",              # str, day 1
        _dt(2026, 7, 6, 11, 0, 0),          # datetime, day 2
        "2026-07-06T13:30:00Z",             # ISO T+Z, day 2
        None,                               # skipped
        "not a timestamp",                  # skipped (unparseable)
    ])
    assert span["count"] == 3
    assert span["distinct_days"] == 2
    assert span["first"].startswith("2026-07-05T09:00:00")
    assert span["last"].startswith("2026-07-06T13:30:00")
    # 2026-07-05 09:00 → 2026-07-06 13:30 = 1 day 4h 30m.
    assert span["elapsed_seconds"] == (28 * 3600 + 30 * 60)
    assert span["elapsed_human"] == "1d 4h"


def test_compute_session_span_empty_is_zeroed():
    span = handoff_module.compute_session_span([])
    assert span == {
        "first": None,
        "last": None,
        "distinct_days": 0,
        "elapsed_seconds": 0,
        "elapsed_human": "0s",
        "count": 0,
    }
    # All-unparseable / all-None input is also a clean zero, never a raise.
    assert handoff_module.compute_session_span([None, "", "xyz"])["count"] == 0


def test_humanize_span_units():
    h = handoff_module._humanize_span
    assert h(0) == "0s"
    assert h(30) == "30s"
    assert h(90) == "1m 30s"
    assert h(3600) == "1h"
    assert h(3661) == "1h 1m"          # only the two most-significant units
    assert h(90000) == "1d 1h"          # 25h → 1d 1h


def test_render_session_span_block_empty_and_populated():
    assert handoff_module._render_session_span_block(
        {"count": 0, "elapsed_human": "0s"}
    ) == ""
    block = handoff_module._render_session_span_block({
        "count": 3,
        "first": "2026-07-05T09:00:00",
        "last": "2026-07-06T13:30:00",
        "distinct_days": 2,
        "elapsed_human": "1d 4h",
    })
    assert "## Session span" in block
    assert "2026-07-05 09:00:00" in block
    assert "1d 4h" in block
    assert "2 calendar days" in block


@pytest.mark.asyncio
async def test_generate_handoff_full_renders_session_span(db, tmp_path):
    """302db181 — a full handoff with logged activity carries the computed span
    block built from real task_log/session timestamps."""
    p = await db_module.create_project(db, "span-full")
    s = await db_module.register_session(db, p["id"], "span-sess")
    sid = s["id"] if isinstance(s, dict) else s
    await db_module.log_task(db, sid, p["id"], "did some work")
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, session_id=sid
    )
    assert "## Session span" in content
    assert "calendar day" in content


# ---------------------------------------------------------------------------
# 9c6cac08 (665 follow-up) — deterministic paste-ready handoff serialization
# and scope fidelity.
#
# Item 23e20656 built ONE canonical, hashable per-item executor_contract
# (meridian.executor_contract.build_executor_contract) with pure JSON/XML/
# text projections (to_json / render_xml_clause / render_text). This section
# proves — end-to-end, through generate_handoff / the MCP dispatch, not just
# the isolated module already covered by tests/test_executor_contract.py —
# that:
#   1. the canonical serialization format itself is pinned (golden payloads),
#   2. two calls against IDENTICAL DB state produce materially identical
#      output (modulo the single-use goal_token/timestamps),
#   3. the /goal text's <tool_requirements>/<sprint_item_pointers> XML
#      clauses carry the SAME canonical JSON the sibling capability_contract
#      field embeds — no independent re-derivation that could drift,
#   4. a requested scope (a sprint version) is fully accounted for — every
#      pending item is visible somewhere (claimable batch or a structured
#      exclusion note), and nothing outside the requested scope leaks in,
#   5. a non-executable state (an unavailable required tool) is visible
#      across all three projections, not silently dropped.
# ---------------------------------------------------------------------------


_GOAL_TOKEN_RE = re.compile(r"<goal_token>[^<]*</goal_token>")


def _strip_goal_token(content: str) -> str:
    """Replace the single-use provenance token (dd07ece0) with a fixed
    placeholder so two otherwise-identical /goal renders can be diffed
    byte-for-byte. The token is a fresh nonce BY DESIGN (a new one is minted
    on every call, see test_mint_handoff_token_produces_unique_tokens in
    test_dd07ece0_handoff_token.py) — the one field explicitly exempted,
    alongside timestamps/session ids, from the determinism guarantee."""
    return _GOAL_TOKEN_RE.sub("<goal_token>STRIPPED</goal_token>", content)


def _extract_xml_tag_body(content: str, tag: str) -> str:
    """Pull the body text out of the FIRST ``<tag ...>...</tag>`` occurrence
    and XML-unescape it — the tool_requirements/sprint_item_pointers clauses
    embed canonical JSON, which needs unescaping before json.loads (the
    renderer only escapes &/</>, not quotes, so this round-trips cleanly).
    Attribute-agnostic: matches both a bare ``<tag>`` and an attributed
    ``<tag count="1">`` opening (e.g. excluded_unprospected/
    excluded_wave_gate_pending)."""
    from xml.sax.saxutils import unescape as _xml_unescape

    open_start = content.index(f"<{tag}")
    open_end = content.index(">", open_start) + 1
    end_marker = f"</{tag}>"
    end = content.index(end_marker, open_end)
    return _xml_unescape(content[open_end:end])


# ---------------------------------------------------------------------------
# (1) Golden canonical payloads — pins the EXACT serialization format so any
# accidental drift (indent added, separators changed, key order changed)
# breaks immediately, not just "still looks like JSON/XML/text".
# ---------------------------------------------------------------------------


def test_golden_executor_contract_json_xml_text_simple():
    contract = {
        "schema_version": 1,
        "item_id": "golden-item",
        "version": "v1",
        "mode": "autonomous",
        "executable": True,
        "executable_reasons": [],
        "allowed_tools": [],
        "forbidden_tools": [],
        "steps": [{
            "order": 1, "kind": "finish",
            "description": "Call complete_sprint_item(item_id, project_id).",
        }],
        "gate_after": None,
        "contract_hash": "deadbeef",
    }
    assert ec.to_json(contract) == (
        '{"allowed_tools":[],"contract_hash":"deadbeef","executable":true,'
        '"executable_reasons":[],"forbidden_tools":[],"gate_after":null,'
        '"item_id":"golden-item","mode":"autonomous","schema_version":1,'
        '"steps":[{"description":"Call complete_sprint_item(item_id, project_id).",'
        '"kind":"finish","order":1}],"version":"v1"}'
    )
    assert ec.render_xml_clause(contract) == (
        '<executor_contract item_id="golden-item" mode="autonomous" '
        'executable="true" contract_hash="deadbeef">\n'
        '  <step order="1" kind="finish">Call complete_sprint_item(item_id, project_id).</step>\n'
        "</executor_contract>"
    )
    assert ec.render_text(contract) == (
        "Executor contract — item golden-item (version=v1, mode=autonomous)\n"
        "Steps:\n"
        "  1. Call complete_sprint_item(item_id, project_id)."
    )


def test_golden_executor_contract_serialize_and_hash_for_non_executable_contract():
    """A richer, non-executable contract (missing required tool, an active
    step, populated dependency/completion_checks/scope) — pins
    serialize_executor_contract's canonical JSON exactly, then proves
    contract_hash IS sha256 of that exact canonical form (computed in-test
    via hashlib, not hand-transcribed, so this also guards the hashing
    contract itself without risking a transcription typo on a 64-char hex
    string)."""
    contract = {
        "schema_version": 1,
        "item_id": "golden-item-2",
        "version": "v1",
        "scope": {
            "project_id": "proj-1", "requested_version": None, "wave": None,
            "track": None, "milestone_type": None, "priority": None,
        },
        "mode": "autonomous",
        "executable": False,
        "executable_reasons": ["missing_required_tools:Serena: find_symbol"],
        "allowed_tools": [{
            "name": "find_symbol", "server_or_namespace": "Serena",
            "required_or_preferred": "required", "purpose": "locate target",
            "call_template": None, "fallback": [], "risk_class": "read",
            "availability_status": "missing", "fallback_used": None,
        }],
        "forbidden_tools": [{
            "name": "find_symbol", "server_or_namespace": "Serena",
            "reason": "required tool unavailable; no fallback declared",
        }],
        "scheduling": {"touches_resources": []},
        "steps": [
            {
                "order": 1, "kind": "tool_call",
                "description": "Use Serena: find_symbol — locate target",
                "tool": {
                    "name": "find_symbol", "server_or_namespace": "Serena",
                    "call_template": None,
                },
            },
            {
                "order": 2, "kind": "finish",
                "description": "Call complete_sprint_item(item_id, project_id).",
            },
        ],
        "gate_after": None,
        "gate_blocking": None,
        "dependency": {
            "depends_on": None, "failure_mode": "continue",
            "blocking_item": None, "satisfied": True,
        },
        "output_requirements": {
            "artifact_kind": None, "planned_output": None, "policy": None,
            "declared": False,
        },
        "pointers": None,
        "completion_checks": {
            "required_notes": False, "required_notes_satisfied": True,
            "require_verification": False, "require_verification_satisfied": True,
            "verification_on_file": None,
            "prospecting": {
                "declares_resources": False, "has_pointer_evidence": False,
                "prospected": True, "prospect_bypass": False,
            },
        },
        "generated_at": "2026-01-01T00:00:00+00:00",
        "contract_hash": "fixedhash123",
    }
    expected_serialized = (
        '{"allowed_tools":[{"availability_status":"missing","call_template":null,'
        '"fallback":[],"fallback_used":null,"name":"find_symbol","purpose":"locate target",'
        '"required_or_preferred":"required","risk_class":"read","server_or_namespace":"Serena"}],'
        '"completion_checks":{"prospecting":{"declares_resources":false,"has_pointer_evidence":false,'
        '"prospect_bypass":false,"prospected":true},"require_verification":false,'
        '"require_verification_satisfied":true,"required_notes":false,"required_notes_satisfied":true,'
        '"verification_on_file":null},"dependency":{"blocking_item":null,"depends_on":null,'
        '"failure_mode":"continue","satisfied":true},"executable":false,'
        '"executable_reasons":["missing_required_tools:Serena: find_symbol"],'
        '"forbidden_tools":[{"name":"find_symbol","reason":"required tool unavailable; no fallback declared",'
        '"server_or_namespace":"Serena"}],"gate_after":null,"gate_blocking":null,'
        '"item_id":"golden-item-2","mode":"autonomous","output_requirements":{"artifact_kind":null,'
        '"declared":false,"planned_output":null,"policy":null},"pointers":null,'
        '"scheduling":{"touches_resources":[]},"schema_version":1,"scope":{"milestone_type":null,'
        '"priority":null,"project_id":"proj-1","requested_version":null,"track":null,"wave":null},'
        '"steps":[{"description":"Use Serena: find_symbol \\u2014 locate target","kind":"tool_call",'
        '"order":1,"tool":{"call_template":null,"name":"find_symbol","server_or_namespace":"Serena"}},'
        '{"description":"Call complete_sprint_item(item_id, project_id).","kind":"finish","order":2}],'
        '"version":"v1"}'
    )
    serialized = ec.serialize_executor_contract(contract)
    assert serialized == expected_serialized
    assert "2026-01-01T00:00:00" not in serialized  # generated_at excluded
    assert "fixedhash123" not in serialized  # contract_hash excluded
    assert ec.executor_contract_hash(contract) == hashlib.sha256(
        expected_serialized.encode("utf-8")
    ).hexdigest()

    # And the XML/text projections of the SAME contract make the
    # non-executable state visible, not just the JSON.
    xml = ec.render_xml_clause(contract)
    text = ec.render_text(contract)
    assert 'executable="false"' in xml
    assert "<forbidden_tool>Serena: find_symbol</forbidden_tool>" in xml
    assert "NOT EXECUTABLE" in text
    assert "Do NOT rely on (confirmed unavailable):" in text


# ---------------------------------------------------------------------------
# (2) Repeated-run determinism — identical DB state, two calls, diff modulo
# the goal_token / generated_at fields.
# ---------------------------------------------------------------------------


async def test_generate_handoff_goal_mode_deterministic_modulo_token(db, tmp_path):
    p = await db_module.create_project(db, "determinism-goal-text")
    await db_module.add_sprint_item(
        db, p["id"], "v1", "Refactor the parser",
        tool_requirements=[{
            "name": "find_symbol", "server_or_namespace": "Serena",
            "required_or_preferred": "required", "purpose": "locate target",
            "fallback": ["grep_search"],
        }],
    )
    _, content_a, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal"
    )
    _, content_b, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal"
    )
    # Tokens themselves must differ (fresh nonce each call)...
    assert content_a != content_b
    # ...but everything else must be byte-identical.
    assert _strip_goal_token(content_a) == _strip_goal_token(content_b)


async def test_generate_handoff_capability_contract_deterministic_across_repeated_mcp_calls(
    db, tmp_path
):
    """Same guarantee as test_capability_contract.py's
    test_contract_serialize_is_byte_stable_for_same_state, but proven through
    the REAL MCP generate_handoff dispatch (mcp/handler.py), which is what
    actually ships to a caller — not just a direct build_capability_contract
    unit call."""
    p = await db_module.create_project(db, "determinism-mcp-contract")
    await db_module.add_sprint_item(
        db, p["id"], "v1", "Refactor the auth handshake",
        tool_requirements=[{
            "name": "find_symbol", "server_or_namespace": "Serena",
            "required_or_preferred": "required", "purpose": "locate target",
        }],
    )
    result_a = await mcp_handler._handle_task_tools(
        "generate_handoff", {"project_id": p["id"], "mode": "goal"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    result_b = await mcp_handler._handle_task_tools(
        "generate_handoff", {"project_id": p["id"], "mode": "goal"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    cc_a, cc_b = result_a["capability_contract"], result_b["capability_contract"]
    assert cc_a["contract_hash"] == cc_b["contract_hash"]
    assert cc.serialize_contract(cc_a) == cc.serialize_contract(cc_b)
    assert _strip_goal_token(result_a["content"]) == _strip_goal_token(result_b["content"])


async def test_generate_handoff_surfaces_blocker_policy_for_empty_critical_item(
    db, tmp_path,
):
    """b108f2e0 — acceptance case 1, through the REAL MCP generate_handoff
    dispatch: an empty-scope CRITICAL item is quarantined (not a run stop),
    appears in the handoff's blocker_policy field as non-executable, while
    an independent well-scoped item stays eligible.
    """
    p = await db_module.create_project(db, "blocker-policy-handoff")
    empty_item = await db_module.add_sprint_item(
        db, p["id"], "v1", "CRITICAL tenant isolation breach",
        notes="", priority="urgent",
    )
    good_item = await db_module.add_sprint_item(
        db, p["id"], "v1", "unrelated well-scoped fix", notes="clear repro + fix plan",
    )
    result = await mcp_handler._handle_task_tools(
        "generate_handoff", {"project_id": p["id"], "mode": "goal"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    decision = result["blocker_policy"]
    assert decision is not None
    assert decision["policy"] == "quarantine_continue"
    assert decision["run_stop"] is False
    assert empty_item["id"] in decision["blocked_item_ids"]
    assert decision["classifications"][empty_item["id"]] == "needs_scope"
    assert good_item["id"] in decision["eligible_item_ids"]


async def test_generate_handoff_blocker_policy_run_stop_when_configured(db, tmp_path):
    """An explicit project-level run_stop policy makes even a single
    under-scoped item halt the whole run, surfaced through the same
    generate_handoff field.
    """
    p = await db_module.create_project(db, "blocker-policy-run-stop")
    await db_module.set_project_blocker_policy(db, p["id"], "run_stop")
    await db_module.add_sprint_item(
        db, p["id"], "v1", "under-scoped item", notes="", priority="urgent",
    )
    result = await mcp_handler._handle_task_tools(
        "generate_handoff", {"project_id": p["id"], "mode": "goal"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    decision = result["blocker_policy"]
    assert decision is not None
    assert decision["run_stop"] is True
    assert decision["eligible_item_ids"] == []


# ---------------------------------------------------------------------------
# (3) XML/JSON projection parity — the /goal text's typed clauses must carry
# the SAME canonical JSON as the sibling capability_contract field, for the
# SAME request (never two independent derivations that could silently
# drift).
# ---------------------------------------------------------------------------


async def test_tool_requirements_xml_clause_matches_capability_contract_json(db, tmp_path):
    p = await db_module.create_project(db, "parity-toolreq")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Needs a specific tool",
        tool_requirements=[{
            "name": "find_symbol", "server_or_namespace": "Serena",
            "required_or_preferred": "required", "purpose": "locate target",
            "fallback": ["grep_search"],
        }],
    )
    result = await mcp_handler._handle_task_tools(
        "generate_handoff", {"project_id": p["id"], "mode": "goal"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    from_xml = json.loads(_extract_xml_tag_body(result["content"], "tool_requirements"))
    assert from_xml == result["capability_contract"]["item_tool_requirements"]
    assert any(e["item_id"] == item["id"] for e in from_xml)


async def test_sprint_item_pointers_xml_clause_matches_capability_contract_json(db, tmp_path):
    p = await db_module.create_project(db, "parity-pointers")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Touches a real file",
        touches_resources=["file:meridian/parser.py"],
    )
    await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "code",
        [{"uri": "file:meridian/parser.py", "selector": {"type": "range", "start_line": 1, "end_line": 5}}],
        label="entry point",
    )
    result = await mcp_handler._handle_task_tools(
        "generate_handoff", {"project_id": p["id"], "mode": "goal"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    from_xml = json.loads(_extract_xml_tag_body(result["content"], "sprint_item_pointers"))
    assert from_xml == result["capability_contract"]["item_sprint_item_pointers"]
    assert any(e["item_id"] == item["id"] for e in from_xml)


async def test_artifact_pointer_findings_xml_clause_matches_capability_contract_json(db, tmp_path):
    """70c10ca3 (b730 follow-up) — the batch /goal's <artifact_pointer_findings>
    clause and capability_contract's item_artifact_pointer_findings section
    must carry IDENTICAL data for the SAME request, proven through the REAL
    MCP generate_handoff dispatch (mirrors the tool_requirements/
    sprint_item_pointers parity tests immediately above)."""
    p = await db_module.create_project(db, "parity-artifact-findings")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Regenerate the results table with new benchmark numbers",
        artifact_policy={"artifact_pointer_check": "strict"},
    )
    stored = await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "docs",
        [{"uri": "outputs/report.docx",
          "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    )
    result = await mcp_handler._handle_task_tools(
        "generate_handoff", {"project_id": p["id"], "mode": "goal"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    from_xml = json.loads(_extract_xml_tag_body(result["content"], "artifact_pointer_findings"))
    assert from_xml == result["capability_contract"]["item_artifact_pointer_findings"]
    assert len(from_xml) == 1
    finding = from_xml[0]
    assert finding["item_id"] == item["id"]
    assert finding["warning_code"] == "insufficient_pointer_bare_docx"
    assert finding["pointer_status"] == "weak"
    assert finding["ready"] is False
    assert finding["affected_pointer_ids"] == [str(stored["id"])]
    # The readiness verification (3196ba0e) genuinely ran: a relative
    # "outputs/report.docx" path does not exist from the test cwd.
    assert finding["target_readiness"][0]["ready"] is False
    assert finding["target_readiness"][0]["targets"][0]["status"] == "missing"


_GOLDEN_ARTIFACT_FINDING_KEYS = {
    "item_id", "classification", "policy", "warning_code",
    "required_remediation", "affected_pointer_ids", "ready",
    "pointer_status", "target_readiness",
}


async def test_golden_artifact_pointer_finding_schema_pinned(db, tmp_path):
    """f9bacd5b (b730 follow-up, final gate) — pins the EXACT key set of an
    ``item_artifact_pointer_findings`` entry, mirroring this file's own
    golden-payload discipline (see the executor_contract golden tests above)
    so an accidental field rename/removal/addition breaks immediately."""
    p = await db_module.create_project(db, "golden-artifact-finding-schema")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Regenerate the results table with new benchmark numbers",
        artifact_policy={"artifact_pointer_check": "warn"},
    )
    await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "docs",
        [{"uri": "outputs/report.docx",
          "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    )
    result = await mcp_handler._handle_task_tools(
        "generate_handoff", {"project_id": p["id"], "mode": "goal"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    findings = result["capability_contract"]["item_artifact_pointer_findings"]
    assert len(findings) == 1
    assert set(findings[0].keys()) == _GOLDEN_ARTIFACT_FINDING_KEYS


async def test_artifact_pointer_findings_absent_when_no_active_warning(db, tmp_path):
    p = await db_module.create_project(db, "parity-artifact-findings-none")
    await db_module.add_sprint_item(db, p["id"], "v1", "Renumber figure captions")
    result = await mcp_handler._handle_task_tools(
        "generate_handoff", {"project_id": p["id"], "mode": "goal"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    assert "<artifact_pointer_findings>" not in result["content"]
    assert result["capability_contract"]["item_artifact_pointer_findings"] == []


async def test_build_capability_contract_item_artifact_pointer_findings_empty_project(db):
    p = await db_module.create_project(db, "artifact-findings-empty-contract")
    contract = await cc.build_capability_contract(db, p["id"])
    assert contract["item_artifact_pointer_findings"] == []


async def test_extract_artifact_pointer_findings_self_fetch_matches_pre_annotated(db):
    """Whether the caller pre-annotates items (via _annotate_resolved_pointers)
    or passes raw items and lets extract_artifact_pointer_findings self-fetch
    + resolve + verify readiness, the two paths must produce identical typed
    output for the same underlying data."""
    p = await db_module.create_project(db, "artifact-findings-self-fetch")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Regenerate the results table with new benchmark numbers",
        artifact_policy={"artifact_pointer_check": "warn"},
    )
    await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "docs",
        [{"uri": "outputs/report.docx",
          "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    )

    raw_items = [dict(item)]
    self_fetched = await cc.extract_artifact_pointer_findings(db, p["id"], raw_items)

    annotated_items = [dict(item)]
    await handoff_module._annotate_resolved_pointers(db, p["id"], annotated_items)
    pre_annotated = await cc.extract_artifact_pointer_findings(db, p["id"], annotated_items)

    assert self_fetched == pre_annotated
    assert self_fetched[0]["item_id"] == item["id"]
    assert self_fetched[0]["warning_code"] == "insufficient_pointer_bare_docx"


async def test_build_capability_contract_accepts_explicit_items_override_for_artifact_findings(db):
    p = await db_module.create_project(db, "artifact-findings-explicit-items")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Regenerate the results table with new benchmark numbers",
        artifact_policy={"artifact_pointer_check": "warn"},
    )
    await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "docs",
        [{"uri": "outputs/report.docx",
          "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    )
    items = [dict(item)]
    contract = await cc.build_capability_contract(db, p["id"], items=items)
    expected = await cc.extract_artifact_pointer_findings(db, p["id"], items)
    assert contract["item_artifact_pointer_findings"] == expected


async def test_generate_handoff_artifact_pointer_findings_deterministic_across_repeated_mcp_calls(
    db, tmp_path
):
    """Same byte-stability guarantee the sibling capability_contract test
    above proves, specific to the new item_artifact_pointer_findings section
    and its <artifact_pointer_findings> XML twin."""
    p = await db_module.create_project(db, "determinism-artifact-findings")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Regenerate the results table with new benchmark numbers",
        artifact_policy={"artifact_pointer_check": "strict"},
    )
    await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "docs",
        [{"uri": "outputs/report.docx",
          "selector": {"type": "range", "start_line": 1, "end_line": 1}}],
    )
    result_a = await mcp_handler._handle_task_tools(
        "generate_handoff", {"project_id": p["id"], "mode": "goal"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    result_b = await mcp_handler._handle_task_tools(
        "generate_handoff", {"project_id": p["id"], "mode": "goal"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    assert (
        result_a["capability_contract"]["item_artifact_pointer_findings"]
        == result_b["capability_contract"]["item_artifact_pointer_findings"]
    )
    from_xml_a = json.loads(_extract_xml_tag_body(result_a["content"], "artifact_pointer_findings"))
    from_xml_b = json.loads(_extract_xml_tag_body(result_b["content"], "artifact_pointer_findings"))
    assert from_xml_a == from_xml_b
    assert _strip_goal_token(result_a["content"]) == _strip_goal_token(result_b["content"])


# ---------------------------------------------------------------------------
# (4) Requested-vs-emitted scope fidelity — every pending item for the
# requested version is visible SOMEWHERE (claimable batch or a structured
# exclusion note); nothing silently vanishes, nothing outside scope leaks in.
# ---------------------------------------------------------------------------


async def test_requested_scope_fully_accounted_for_no_silent_drop(db, tmp_path):
    p = await db_module.create_project(db, "scope-accounting")
    item_plain = await db_module.add_sprint_item(db, p["id"], "v1", "Plain claimable item")
    item_manual = await db_module.add_sprint_item(
        db, p["id"], "v1", "MANUAL: publish blog post"
    )
    item_backburner = await db_module.add_sprint_item(
        db, p["id"], "v1", "Backburnered work", track="backburner"
    )
    item_wave_gated = await db_module.add_sprint_item(
        db, p["id"], "v1", "Deploy notification service", wave="wave-2"
    )
    item_unprospected = await db_module.add_sprint_item(
        db, p["id"], "v1", "Touches ghost file", touches_resources=["file:ghost.py"]
    )
    item_other_version = await db_module.add_sprint_item(
        db, p["id"], "v2", "Other version item, must not leak into v1 scope"
    )
    await db_module.configure_wave_gate(
        db, p["id"], wave_end="wave-1", actions=[{"type": "wait", "seconds": 1}],
    )

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal", version="v1",
    )

    # Every requested-scope item is visible somewhere — claimable or excluded
    # with a structured, named reason. Nothing silently disappears.
    for it in (item_plain, item_manual, item_backburner, item_wave_gated, item_unprospected):
        assert it["id"] in content, f"{it['title']!r} vanished from the requested-scope handoff"

    # Only the genuinely-claimable item is in the claimable batch itself.
    start = content.rindex("<sprint_items>") + len("<sprint_items>")
    end = content.index("</sprint_items>", start)
    assert content[start:end].strip() == f"Complete sprint items: {item_plain['id']}."

    # And the two items with a UNIQUELY-tagged exclusion reason (manual/
    # backburner share a generic <exclusions> tag, checked above via plain
    # membership — wave-gate/unprospected each get their own distinct tag)
    # are excluded for the RIGHT documented reason, not just "somewhere".
    assert item_wave_gated["id"] in _extract_xml_tag_body(
        content, "excluded_wave_gate_pending"
    )
    assert item_unprospected["id"] in _extract_xml_tag_body(content, "excluded_unprospected")
    # Neither of those two structurally-excluded items is ALSO claimable.
    assert item_wave_gated["id"] not in content[start:end]
    assert item_unprospected["id"] not in content[start:end]

    # No silent broadening: an item from an entirely different version never
    # appears anywhere in this version-scoped handoff.
    assert item_other_version["id"] not in content


async def test_nonexistent_version_scope_does_not_broaden_to_whole_board(db, tmp_path):
    """Requesting a version with zero pending items (typo'd/stale reference)
    must FAIL VISIBLY — an empty, honestly-scoped /goal — never silently
    broaden to show the rest of the board."""
    p = await db_module.create_project(db, "nonexistent-version-scope")
    item_v1 = await db_module.add_sprint_item(db, p["id"], "v1", "Real v1 item")

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
        version="v999-typo-does-not-exist",
    )
    assert item_v1["id"] not in content
    assert "<executor_directive>Verify remaining work is complete.</executor_directive>" in content

    # The MCP surface's own scope metadata makes the requested (empty) scope
    # explicit rather than silently indistinguishable from "unscoped".
    result = await mcp_handler._handle_task_tools(
        "generate_handoff",
        {"project_id": p["id"], "mode": "goal", "version": "v999-typo-does-not-exist"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    assert result["scope"]["requested_version"] == "v999-typo-does-not-exist"
    assert result["scope"]["effective_version"] == "v999-typo-does-not-exist"


# ---------------------------------------------------------------------------
# (5) The embedded per-item executor_contract must never drift from a
# standalone build of the SAME live state — and an unavailable required tool
# must fail visibly across every projection, not just the JSON.
# ---------------------------------------------------------------------------


async def test_item_executor_contract_embedded_matches_standalone_build(db, tmp_path):
    p = await db_module.create_project(db, "embed-vs-standalone")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Refactor the payments module",
        tool_requirements=[{
            "name": "find_symbol", "server_or_namespace": "Serena",
            "required_or_preferred": "required", "purpose": "locate target",
        }],
    )
    result = await mcp_handler._handle_task_tools(
        "generate_handoff", {"project_id": p["id"], "mode": "goal"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    embedded = next(
        e for e in result["capability_contract"]["item_executor_contracts"]
        if e["item_id"] == item["id"]
    )
    fresh = await db_module.get_sprint_item(db, item["id"])
    standalone = await ec.build_executor_contract(db, p["id"], fresh)
    # The embedded entry's own generated_at was already stripped at build
    # time (23e20656); serialize_executor_contract strips both sides' anyway,
    # so the two must be byte-identical AND hash-identical.
    assert ec.serialize_executor_contract(embedded) == ec.serialize_executor_contract(standalone)
    assert embedded["contract_hash"] == standalone["contract_hash"]


async def test_unavailable_required_tool_fails_visibly_across_all_three_projections(db):
    """An item whose required tool is CONFIRMED unavailable (a live inventory
    shows the plugin disabled, not merely 'unknown') must be visibly
    non-executable in the JSON, the XML clause, AND the human-text
    projection — proving the executor_contract really is the single source
    of truth all three renderings agree on, not just the JSON field a caller
    might not even look at."""
    p = await db_module.create_project(db, "unavailable-tool-visibility")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Needs a dead tool",
        tool_requirements=[{
            "name": "find_symbol", "server_or_namespace": "Serena",
            "required_or_preferred": "required", "purpose": "locate target",
        }],
    )
    inventory = {
        "tunnel_reachable": True,
        "builtin_tools": set(),
        "plugins": {"Serena": {"enabled": False, "invocable": False, "tools": set()}},
        "stdio_registry": {},
    }
    contract = await ec.build_executor_contract(db, p["id"], item, tool_inventory=inventory)
    assert contract["executable"] is False

    as_json = ec.to_json(contract)
    as_xml = ec.render_xml_clause(contract)
    as_text = ec.render_text(contract)

    assert contract["forbidden_tools"], "forbidden_tools must be non-empty in the built object"
    assert json.loads(as_json)["forbidden_tools"][0]["name"] == "find_symbol"
    assert 'executable="false"' in as_xml
    assert "<forbidden_tool>Serena: find_symbol</forbidden_tool>" in as_xml
    assert "NOT EXECUTABLE" in as_text
    assert "Do NOT rely on (confirmed unavailable):" in as_text
    assert "Serena: find_symbol" in as_text


# ---------------------------------------------------------------------------
# cb00889c — bounded handoff profiles with on-demand executor contracts.
#
# Two independent pieces:
#   (a) executor_contract.render_xml_clause(contract, compact=True) — a
#       bounded per-item projection (item_id/title/scope/contract_hash/
#       pointer_ids/executable only), with the pre-existing full projection
#       still available on demand via the unchanged compact=False default.
#   (b) handoff.format_handoff_mcp_content's/generate_handoff's own
#       max_bytes/max_content_bytes budget — an integrity-first backstop that
#       never touches disk/DB persistence and never cuts through an embedded
#       <goal_token>/SECURITY banner.
# ---------------------------------------------------------------------------


def test_render_xml_clause_compact_default_unchanged():
    """The function-level default stays compact=False — byte-for-byte the
    same full projection every existing caller already depends on."""
    contract = {
        "item_id": "abc123", "mode": "autonomous", "executable": False,
        "contract_hash": "deadbeef",
        "executable_reasons": ["missing_required_tools:Serena: find_symbol"],
        "allowed_tools": [],
        "forbidden_tools": [{"name": "find_symbol", "server_or_namespace": "Serena"}],
        "steps": [],
        "gate_after": None,
    }
    full = ec.render_xml_clause(contract)
    assert "compact" not in full
    assert "<forbidden_tool>Serena: find_symbol</forbidden_tool>" in full


def test_render_xml_clause_compact_embeds_only_bounded_fields():
    contract = {
        "item_id": "item-42",
        "title": "Refactor the payments module",
        "scope": {
            "project_id": "proj-1", "requested_version": "v3", "wave": "2",
            "track": "backend", "milestone_type": "feature", "priority": "high",
        },
        "contract_hash": "cafef00d",
        "executable": True,
        "executable_reasons": [],
        "allowed_tools": [{
            "name": "find_symbol", "server_or_namespace": "Serena",
            "required_or_preferred": "required", "availability_status": "available",
        }],
        "forbidden_tools": [],
        "steps": [{"order": 1, "kind": "finish", "description": "Call complete_sprint_item."}],
        "gate_after": {"wave_end": "wave-2", "gate_passed": True},
        "pointers": {
            "item_id": "item-42",
            "pointers": [
                {"id": "ptr-1", "source_type": "code", "targets": []},
                {"id": "ptr-2", "source_type": "code", "targets": []},
                {"source_type": "code", "targets": []},  # no id — must be skipped
            ],
        },
    }
    compact = ec.render_xml_clause(contract, compact=True)
    assert compact == (
        '<executor_contract compact="true" item_id="item-42" '
        'title="Refactor the payments module" executable="true" '
        'contract_hash="cafef00d" requested_version="v3" wave="2" '
        'track="backend" milestone_type="feature" priority="high" '
        'pointer_ids="ptr-1,ptr-2" />'
    )
    # Bounded: none of the full projection's tool/step/gate detail leaks in.
    assert "find_symbol" not in compact
    assert "complete_sprint_item" not in compact
    assert "gate_after" not in compact
    # The SAME information remains available on demand via the full/JSON/text
    # projections of this identical, already-built contract — no re-fetch.
    full = ec.render_xml_clause(contract, compact=False)
    assert "find_symbol" in full
    as_json = json.loads(ec.to_json(contract))
    assert as_json["item_id"] == "item-42"


def test_render_xml_clause_compact_handles_missing_pointers_title_scope():
    """A minimal/degraded contract (no pointers, no title, no scope) still
    renders a valid, well-formed compact element — never raises."""
    contract = {"item_id": "item-99", "contract_hash": "h", "executable": False}
    compact = ec.render_xml_clause(contract, compact=True)
    assert compact.startswith('<executor_contract compact="true"')
    assert compact.endswith("/>")
    assert 'pointer_ids=""' in compact
    assert 'title=""' in compact
    assert 'executable="false"' in compact


async def test_build_executor_contract_carries_title_for_compact_projection(db):
    """build_executor_contract now carries the item's title (cheap, always
    available on ``item``) so the compact projection can name the item
    without any re-fetch — proven end-to-end against a real built contract,
    not just a static fixture."""
    p = await db_module.create_project(db, "ec-title")
    item = await db_module.add_sprint_item(db, p["id"], "v1", "Refactor the parser")
    contract = await ec.build_executor_contract(db, p["id"], item)
    assert contract["title"] == "Refactor the parser"
    compact = ec.render_xml_clause(contract, compact=True)
    assert 'title="Refactor the parser"' in compact
    assert f'item_id="{item["id"]}"' in compact


# ---------------------------------------------------------------------------
# format_handoff_mcp_content's byte budget.
# ---------------------------------------------------------------------------


def test_format_handoff_mcp_content_identity_under_budget():
    """Content at/under the default budget is returned byte-identical — zero
    functional change for the overwhelming common case (every existing
    caller's content)."""
    small = "/goal\nstart_session()\n<sprint_items>a, b, c</sprint_items>"
    assert handoff_module.format_handoff_mcp_content(small) == small


def test_format_handoff_mcp_content_disabled_via_none_or_nonpositive():
    huge = "X" * 5000
    assert handoff_module.format_handoff_mcp_content(huge, max_bytes=None) == huge
    assert handoff_module.format_handoff_mcp_content(huge, max_bytes=0) == huge
    assert handoff_module.format_handoff_mcp_content(huge, max_bytes=-1) == huge


def test_format_handoff_mcp_content_truncates_oversized_content():
    huge = "Y" * 5000
    out = handoff_module.format_handoff_mcp_content(huge, max_bytes=1000)
    assert len(out.encode("utf-8")) < 5000
    assert "TRUNCATED" in out
    assert "limit=1000 bytes" in out


async def test_format_handoff_mcp_content_never_truncates_goal_token_banner(db):
    """Even a budget far smaller than the protected region still keeps the
    <goal_token>/SECURITY banner (and everything before it) byte-for-byte —
    integrity-first over strict budget compliance."""
    p = await db_module.create_project(db, "budget-token-protect")
    body = '/goal\nstart_session(project_name="x")'
    embedded = await handoff_module._mint_and_embed_goal_token(db, p["id"], body)
    assert "<goal_token>" in embedded  # sanity: mint actually worked
    padded = embedded + "\n<sprint_items>" + ("z" * 5000) + "</sprint_items>"
    out = handoff_module.format_handoff_mcp_content(padded, max_bytes=10)
    banner_match = handoff_module._GOAL_TOKEN_BANNER_RE.search(embedded)
    assert banner_match is not None
    protected_prefix = embedded[: banner_match.end()]
    assert out.startswith(protected_prefix)
    assert "TRUNCATED" in out
    assert len(out.encode("utf-8")) < len(padded.encode("utf-8"))


def test_format_handoff_mcp_content_non_string_passthrough():
    assert handoff_module.format_handoff_mcp_content(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# generate_handoff's own max_content_bytes threading — bounds the RETURNED
# content only; disk/DB persistence always keeps the full render.
# ---------------------------------------------------------------------------


async def test_generate_handoff_default_budget_noop_for_typical_content(db, tmp_path):
    p = await db_module.create_project(db, "budget-genhandoff-default")
    await db_module.set_goal(db, p["id"], "ship", sprint="s1")
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True,
    )
    assert "TRUNCATED" not in content


async def test_generate_handoff_default_budget_truncates_pathologically_large_content(
    db, tmp_path,
):
    """A pathologically large handoff (simulated via extra_narrative, the
    fastest deterministic way to exceed the default budget without hundreds
    of DB writes) is bounded in the RETURNED content, while the on-disk file
    and the DB-persisted pending_goal both keep the complete render."""
    p = await db_module.create_project(db, "budget-genhandoff-huge")
    await db_module.set_goal(db, p["id"], "ship", sprint="s1")
    huge_narrative = "N" * 400_000
    path, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True,
        extra_narrative=huge_narrative,
    )
    assert len(content.encode("utf-8")) < 400_000
    assert "TRUNCATED" in content
    on_disk = Path(path).read_text(encoding="utf-8")
    assert huge_narrative in on_disk  # disk keeps the full, untruncated render


async def test_generate_handoff_max_content_bytes_none_disables_budget(db, tmp_path):
    p = await db_module.create_project(db, "budget-genhandoff-disable")
    await db_module.set_goal(db, p["id"], "ship", sprint="s1")
    huge_narrative = "M" * 400_000
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True,
        extra_narrative=huge_narrative, max_content_bytes=None,
    )
    assert huge_narrative in content
    assert "TRUNCATED" not in content


async def test_generate_handoff_max_content_bytes_explicit_override(db, tmp_path):
    """An explicit, smaller max_content_bytes bounds even ordinarily-small
    content, proving the parameter is genuinely threaded through (not just
    inert plumbing that happens to never trigger)."""
    p = await db_module.create_project(db, "budget-genhandoff-explicit")
    await db_module.set_goal(db, p["id"], "ship", sprint="s1")
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, max_content_bytes=50,
    )
    assert "TRUNCATED" in content
    assert len(content.encode("utf-8")) < 5000


# ---------------------------------------------------------------------------
# (6) A fresh session's /goal block alone must be sufficient to start — no
# missing context a human would need to fill in by hand.
# ---------------------------------------------------------------------------


async def test_fresh_session_goal_block_self_sufficient_no_manual_reconstruction(db, tmp_path):
    p = await db_module.create_project(db, "self-sufficient-goal")
    item = await db_module.add_sprint_item(
        db, p["id"], "v1", "Fix the migration guard",
        tool_requirements=[{
            "name": "find_symbol", "server_or_namespace": "Serena",
            "required_or_preferred": "required", "purpose": "locate target",
            "fallback": ["grep_search"],
        }],
    )
    await db_module.add_sprint_item_pointer(
        db, p["id"], item["id"], "code",
        [{"uri": "file:meridian/db/migrations.py", "selector": {"type": "range", "start_line": 100, "end_line": 120}}],
        label="guard site",
    )

    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal"
    )

    # Structurally self-contained: a fresh executor needs nothing beyond this
    # single string to know WHAT to do, WHICH tool to use, WHERE to look, and
    # WHEN to stop.
    assert content.strip().startswith("/goal") or content.strip().startswith("/loop /goal")
    assert "<executor_directive>" in content
    assert item["id"] in content
    assert "meridian/db/migrations.py:100-120" in content  # concrete pointer, inlined
    assert "guard site" in content
    assert "<goal_token>" in content  # provenance, verifiable on receipt
    assert "<stop_conditions>" in content
    assert "<completion_criteria>" in content
    assert "<execution_policy" in content  # required_first_action etc., not just prose
    # The typed tool-requirement contract is present verbatim, not just a
    # hint the executor has to remember from earlier conversation turns.
    tool_reqs = json.loads(_extract_xml_tag_body(content, "tool_requirements"))
    assert any(
        e["item_id"] == item["id"]
        and any(r.get("name") == "find_symbol" for r in e.get("requirements", []))
        for e in tool_reqs
    )


# ---------------------------------------------------------------------------
# 8a883f60 — explicit, machine-readable evidence_status for generate_handoff's
# best-effort steps (code-pointer enrichment, resolved-pointer annotation,
# freshness re-query, wave-gate exclusion, graph-search availability), plus
# the opt-in strict_evidence fail-closed gate. See meridian/handoff.py's
# _capability_outcome / HandoffEvidenceRequired / _finalize_capability_status
# for the implementation this section exercises.
# ---------------------------------------------------------------------------

_ALL_EVIDENCE_CAPS = {
    "code_pointer_enrichment", "resolved_pointer_annotation",
    "freshness_requery", "wave_gate_exclusion", "graph_search_availability",
}


@pytest.mark.parametrize("mode", ["full", "delta", "starter", "goal"])
async def test_generate_handoff_evidence_status_always_reports_all_five_capabilities(
    db, tmp_path, mode,
):
    """Acceptance point 1: every best-effort step gets an EXPLICIT outcome —
    never a silently-missing key — on every executable mode, not just goal."""
    p = await db_module.create_project(db, f"evidence-shape-{mode}")
    await db_module.add_sprint_item(db, p["id"], "v1", "Some pending item")
    evidence_status: dict = {}
    await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode=mode,
        evidence_status=evidence_status,
    )
    assert set(evidence_status) == _ALL_EVIDENCE_CAPS
    for cap_id, entry in evidence_status.items():
        assert entry["status"] in {"verified", "skipped", "failed", "degraded"}, cap_id
        # Acceptance point 2: exact reason, never blank/generic.
        assert isinstance(entry["reason"], str) and entry["reason"].strip()
        assert "fallback" in entry


async def test_generate_handoff_evidence_status_starter_marks_pointer_steps_skipped(
    db, tmp_path,
):
    """starter/compact structurally never resolves code pointers at all
    (pre-existing behavior, unchanged) — that must read as an explicit
    'skipped', not simply be absent from evidence_status."""
    p = await db_module.create_project(db, "evidence-starter-skips")
    await db_module.add_sprint_item(db, p["id"], "v1", "Some pending item")
    evidence_status: dict = {}
    await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="starter",
        evidence_status=evidence_status,
    )
    for cap_id in (
        "code_pointer_enrichment", "resolved_pointer_annotation",
        "graph_search_availability", "freshness_requery",
    ):
        assert evidence_status[cap_id]["status"] == "skipped", cap_id
    # wave-gate exclusion IS a real check in starter mode.
    assert evidence_status["wave_gate_exclusion"]["status"] == "verified"


async def test_generate_handoff_evidence_status_is_pure_addition_content_unchanged(
    db, tmp_path,
):
    """Acceptance point 5: passing evidence_status (default callers never do)
    must not change `content` at all — pure addition, byte-identical modulo
    the single-use goal_token (same 9c6cac08-style comparison the rest of
    this file already uses for mode='goal')."""
    p = await db_module.create_project(db, "evidence-pure-addition")
    await db_module.add_sprint_item(db, p["id"], "v1", "Some pending item")

    _, content_without, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
    )
    _, content_with, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="goal",
        evidence_status={},
    )
    assert _strip_goal_token(content_without) == _strip_goal_token(content_with)


@pytest.mark.parametrize("mode", ["full", "delta", "starter", "goal"])
async def test_generate_handoff_evidence_status_deterministic_repeated_calls(
    db, tmp_path, mode,
):
    """Acceptance point 4 — same 'call twice, diff' pattern 9c6cac08
    established (see test_generate_handoff_goal_mode_deterministic_modulo_token
    above), applied to evidence_status for each of the four modes. Unlike
    `content` (which carries a fresh goal_token/timestamp each call),
    evidence_status carries no wall-clock/nonce field at all, so it must be
    EXACTLY equal across two calls against identical DB state — no stripping
    needed."""
    p = await db_module.create_project(db, f"evidence-determinism-{mode}")
    await db_module.add_sprint_item(db, p["id"], "v1", "Some pending item")
    status_a: dict = {}
    status_b: dict = {}
    await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode=mode,
        evidence_status=status_a,
    )
    await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode=mode,
        evidence_status=status_b,
    )
    assert status_a == status_b


async def test_generate_handoff_evidence_status_survives_code_pointer_search_error(
    db, tmp_path,
):
    """_annotate_code_pointers NEVER raises for a per-item search failure (it
    catches it, sets prospect_status='error', and continues) — the outer
    try/except around that call therefore can't see it on its own. This
    proves generate_handoff recovers that signal instead of reporting
    'verified' just because the outer call didn't raise (see
    _code_pointer_enrichment_error_outcome)."""
    p = await db_module.create_project(db, "evidence-searcher-blowup")
    await db_module.add_sprint_item(db, p["id"], "v1", "Fix OAuth redirect bug")

    def _boom_searcher(_query):
        raise RuntimeError("search index unavailable")

    evidence_status: dict = {}
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="full",
        graph_searcher=_boom_searcher, evidence_status=evidence_status,
    )
    cpe = evidence_status["code_pointer_enrichment"]
    assert cpe["status"] in {"failed", "degraded"}
    assert "search index unavailable" in cpe["reason"]
    assert cpe["fallback"]
    # And the mandatory handoff still rendered — this is best-effort, not fatal.
    assert content


async def test_generate_handoff_evidence_status_wave_gate_fetch_failure(
    db, tmp_path, monkeypatch,
):
    """A wave-gate config fetch failure (e.g. pre-migration DB) is reported
    as an explicit failed capability with the real exception text, not
    silently folded into 'verified' just because generate_handoff itself
    kept going."""
    p = await db_module.create_project(db, "evidence-wave-gate-blowup")
    await db_module.add_sprint_item(db, p["id"], "v1", "Some pending item")

    async def _boom(*_a, **_k):
        raise RuntimeError("wave_gate_configs table missing")

    monkeypatch.setattr(db_module, "get_wave_gate_configs", _boom)

    evidence_status: dict = {}
    _, content, _ = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="full",
        evidence_status=evidence_status,
    )
    wge = evidence_status["wave_gate_exclusion"]
    assert wge["status"] == "failed"
    assert "wave_gate_configs table missing" in wge["reason"]
    assert wge["fallback"]
    assert content  # still a valid mandatory handoff — degrade, don't break


# ---------------------------------------------------------------------------
# strict_evidence — opt-in, fail-closed (acceptance point 3). Mirrors
# sprint_evidence_guard's strict_evidence/require_strict_evidence contract:
# never engages unless a caller explicitly asks, and when it does, nothing
# is rendered/persisted for that call.
# ---------------------------------------------------------------------------


async def test_generate_handoff_strict_evidence_off_by_default_same_as_before(
    db, tmp_path, monkeypatch,
):
    """The exact same broken state (wave-gate fetch raising) that trips
    strict_evidence below must NOT change behavior at all when
    strict_evidence is omitted — today's graceful-degrade default."""
    p = await db_module.create_project(db, "evidence-default-unaffected")
    await db_module.add_sprint_item(db, p["id"], "v1", "Some pending item")

    async def _boom(*_a, **_k):
        raise RuntimeError("wave_gate_configs table missing")

    monkeypatch.setattr(db_module, "get_wave_gate_configs", _boom)

    path, content, amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="full",
    )
    assert content  # rendered normally; no exception, no behavior change
    assert Path(path).exists()


async def test_generate_handoff_strict_evidence_raises_and_writes_nothing(
    db, tmp_path, monkeypatch,
):
    """strict_evidence=True on a failed capability raises
    HandoffEvidenceRequired BEFORE anything is rendered/written/persisted —
    fail CLOSED, not a plausible-looking-but-incomplete goal."""
    p = await db_module.create_project(db, "evidence-strict-blocks")
    await db_module.add_sprint_item(db, p["id"], "v1", "Some pending item")

    async def _boom(*_a, **_k):
        raise RuntimeError("wave_gate_configs table missing")

    monkeypatch.setattr(db_module, "get_wave_gate_configs", _boom)

    out_dir = tmp_path / "strict-out"
    out_dir.mkdir()
    with pytest.raises(handoff_module.HandoffEvidenceRequired) as excinfo:
        await handoff_module.generate_handoff(
            db, p["id"], str(out_dir), skip_ai_summary=True, mode="full",
            strict_evidence=True,
        )
    errors = excinfo.value.errors
    assert any(e["capability"] == "wave_gate_exclusion" for e in errors)
    assert any(e["status"] == "failed" for e in errors)
    assert excinfo.value.evidence_status["wave_gate_exclusion"]["status"] == "failed"
    # Nothing was written for this refused call.
    assert list(out_dir.iterdir()) == []
    # And the pending_goal channel (5efe254b) was never touched either — a
    # refused handoff must not leak a stale-but-plausible /goal into it.
    pending = await db_module.get_pending_goal(db, p["id"])
    assert pending is None


async def test_generate_handoff_strict_evidence_passes_when_nothing_failed(
    db, tmp_path,
):
    """strict_evidence=True must NOT block a genuinely clean run — only a
    failed/degraded capability triggers the refusal, never 'skipped' ones."""
    p = await db_module.create_project(db, "evidence-strict-clean")
    await db_module.add_sprint_item(db, p["id"], "v1", "Some pending item")

    path, content, amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True, mode="starter",
        strict_evidence=True,
    )
    assert content
    assert Path(path).exists()


async def test_generate_handoff_mcp_dispatch_returns_handoff_evidence_status(db, tmp_path):
    """The real MCP dispatch (mcp/handler.py) — what actually ships to a
    caller — surfaces handoff_evidence_status on every call, plus the
    strict_evidence flag it echoed back, as pure additions alongside the
    pre-existing capability_contract field."""
    p = await db_module.create_project(db, "evidence-mcp-dispatch")
    await db_module.add_sprint_item(db, p["id"], "v1", "Some pending item")

    result = await mcp_handler._handle_task_tools(
        "generate_handoff", {"project_id": p["id"], "mode": "goal"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    assert set(result["handoff_evidence_status"]) == _ALL_EVIDENCE_CAPS
    assert result["strict_evidence"] is False
    assert result["content"]  # unaffected — still the bare /goal block
    assert result["capability_contract"] is not None or result["capability_contract"] is None


async def test_generate_handoff_mcp_dispatch_strict_evidence_blocked_response(
    db, tmp_path, monkeypatch,
):
    """strict_evidence=true over the real MCP dispatch returns a structured
    refusal (mirrors complete_sprint_item's STRICT_EVIDENCE_BLOCKED shape)
    instead of raising an unhandled exception up through the transport."""
    p = await db_module.create_project(db, "evidence-mcp-strict-blocked")
    await db_module.add_sprint_item(db, p["id"], "v1", "Some pending item")

    async def _boom(*_a, **_k):
        raise RuntimeError("wave_gate_configs table missing")

    monkeypatch.setattr(db_module, "get_wave_gate_configs", _boom)

    result = await mcp_handler._handle_task_tools(
        "generate_handoff", {"project_id": p["id"], "mode": "full", "strict_evidence": True},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    assert result["error"] == "HANDOFF_EVIDENCE_BLOCKED"
    assert result["project_id"] == p["id"]
    assert any(
        e["capability"] == "wave_gate_exclusion" for e in result["evidence_errors"]
    )
    assert "wave_gate_exclusion" in result["evidence_status"]


# ---------------------------------------------------------------------------
# 3af86d28 — corrective handoffs: data structure + invalidate-original +
# generate-new-revision path (meridian.handoff.record_handoff_correction /
# invalidate_handoff / regenerate_handoff_correction / load_handoff_correction).
# ---------------------------------------------------------------------------


async def _seed_handoff(db, name: str, tmp_path):
    """Create a project with a goal + one FRESH generated handoff row.
    Returns (project, handoff_row)."""
    p = await db_module.create_project(db, name)
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True,
    )
    rows = await db_module.get_handoffs(db, p["id"], limit=1)
    return p, rows[0]


@pytest.mark.asyncio
async def test_record_handoff_correction_rejects_bad_blocker_classification(db, tmp_path):
    p, h = await _seed_handoff(db, "corr-bad-blocker", tmp_path)
    with pytest.raises(handoff_module.HandoffCorrectionError):
        await handoff_module.record_handoff_correction(
            db, p["id"], source_handoff_id=h["id"], blocker_classification="nonsense",
        )


@pytest.mark.asyncio
async def test_record_handoff_correction_rejects_bad_status(db, tmp_path):
    p, h = await _seed_handoff(db, "corr-bad-status", tmp_path)
    with pytest.raises(handoff_module.HandoffCorrectionError):
        await handoff_module.record_handoff_correction(
            db, p["id"], source_handoff_id=h["id"], blocker_classification="other",
            status="not-a-status",
        )


@pytest.mark.asyncio
async def test_record_handoff_correction_rejects_unknown_source(db):
    p = await db_module.create_project(db, "corr-unknown-source")
    with pytest.raises(handoff_module.HandoffCorrectionError):
        await handoff_module.record_handoff_correction(
            db, p["id"], source_handoff_id="nonexistent-id", blocker_classification="other",
        )


@pytest.mark.asyncio
async def test_record_handoff_correction_happy_path(db, tmp_path):
    p, h = await _seed_handoff(db, "corr-happy", tmp_path)
    corr = await handoff_module.record_handoff_correction(
        db, p["id"],
        source_handoff_id=h["id"],
        blocker_classification="pointer_unresolved",
        investigation_evidence={"finding": "file renamed"},
        added_pointers=[{
            "source_type": "code",
            "targets": [{"uri": "a.py", "selector": {"type": "range", "start_line": 1, "end_line": 2}}],
        }],
        changed_resources=["meridian/server.py"],
        version="v1",
    )
    assert corr["status"] == "draft"
    assert corr["source_handoff_id"] == h["id"]
    assert corr["source_body_hash"] == handoff_module._hash_goal_body(h["body"])
    assert corr["investigation_evidence"] == {"finding": "file renamed"}
    assert corr["changed_resources"] == ["meridian/server.py"]
    assert len(corr["added_pointers"]) == 1
    assert corr["removed_pointers"] == []
    assert corr["new_handoff_id"] is None
    assert corr["pointer_repair_report"] is None


@pytest.mark.asyncio
async def test_record_handoff_correction_idempotency_key_dedups(db, tmp_path):
    p, h = await _seed_handoff(db, "corr-idem", tmp_path)
    first = await handoff_module.record_handoff_correction(
        db, p["id"], source_handoff_id=h["id"], blocker_classification="other",
        idempotency_key="retry-1",
    )
    second = await handoff_module.record_handoff_correction(
        db, p["id"], source_handoff_id=h["id"], blocker_classification="scope_stale",
        idempotency_key="retry-1",
    )
    assert first["id"] == second["id"]
    assert second["blocker_classification"] == "other"  # the ORIGINAL wins, not overwritten
    listed = await handoff_module.list_handoff_corrections(db, p["id"])
    assert len(listed) == 1


@pytest.mark.asyncio
async def test_record_handoff_correction_auto_supersedes_prior_open_correction(db, tmp_path):
    p, h = await _seed_handoff(db, "corr-supersede", tmp_path)
    first = await handoff_module.record_handoff_correction(
        db, p["id"], source_handoff_id=h["id"], blocker_classification="other",
    )
    second = await handoff_module.record_handoff_correction(
        db, p["id"], source_handoff_id=h["id"], blocker_classification="scope_stale",
    )
    refreshed_first = await handoff_module.get_handoff_correction(db, first["id"])
    assert refreshed_first["status"] == "superseded"
    assert second["status"] == "draft"


@pytest.mark.asyncio
async def test_list_handoff_corrections_filters_by_source_and_status(db, tmp_path):
    p = await db_module.create_project(db, "corr-list-filters")
    await db_module.set_goal(db, p["id"], "goal", sprint="s")
    await handoff_module.generate_handoff(db, p["id"], str(tmp_path), skip_ai_summary=True)
    h1 = (await db_module.get_handoffs(db, p["id"], limit=1))[0]
    await db_module.pop_pending_goal(db, p["id"])  # simulate a start_session consuming it
    # get_handoffs orders by created_at DESC, id DESC — SQLite's created_at is
    # only second-granularity, so two rows inserted within the same second can
    # tie and the id (a random UUID) is not a chronological tiebreaker. Diff
    # the id sets before/after instead of trusting "limit=1 is the new one".
    _before_ids = {r["id"] for r in await db_module.get_handoffs(db, p["id"], limit=10)}
    await handoff_module.generate_handoff(db, p["id"], str(tmp_path), skip_ai_summary=True)
    _after_rows = await db_module.get_handoffs(db, p["id"], limit=10)
    h2 = next(r for r in _after_rows if r["id"] not in _before_ids)
    assert h2["id"] != h1["id"]

    c1 = await handoff_module.record_handoff_correction(
        db, p["id"], source_handoff_id=h1["id"], blocker_classification="other",
    )
    c2 = await handoff_module.record_handoff_correction(
        db, p["id"], source_handoff_id=h2["id"], blocker_classification="other",
        status="blocked",
    )

    only_h1 = await handoff_module.list_handoff_corrections(db, p["id"], source_handoff_id=h1["id"])
    assert [c["id"] for c in only_h1] == [c1["id"]]

    only_blocked = await handoff_module.list_handoff_corrections(db, p["id"], status="blocked")
    assert [c["id"] for c in only_blocked] == [c2["id"]]

    everything = await handoff_module.list_handoff_corrections(db, p["id"])
    assert {c["id"] for c in everything} == {c1["id"], c2["id"]}


@pytest.mark.asyncio
async def test_invalidate_handoff_marks_row_without_mutating_body(db, tmp_path):
    p, h = await _seed_handoff(db, "corr-invalidate", tmp_path)
    original_body = h["body"]
    updated = await handoff_module.invalidate_handoff(
        db, h["id"], reason="testing", correction_id="corr-x",
    )
    assert bool(updated["invalidated"]) is True
    assert updated["invalidated_reason"] == "testing"
    assert updated["superseded_by_correction_id"] == "corr-x"
    assert updated["body"] == original_body


@pytest.mark.asyncio
async def test_invalidate_handoff_unknown_id_returns_none(db):
    assert await handoff_module.invalidate_handoff(db, "nope", reason="x") is None


@pytest.mark.asyncio
async def test_update_handoff_correction_status_sets_status_and_reason(db, tmp_path):
    p, h = await _seed_handoff(db, "corr-status-update", tmp_path)
    corr = await handoff_module.record_handoff_correction(
        db, p["id"], source_handoff_id=h["id"], blocker_classification="other",
    )
    updated = await handoff_module.update_handoff_correction_status(
        db, corr["id"], "blocked", reason="waiting on upstream fix",
    )
    assert updated["status"] == "blocked"
    assert updated["status_reason"] == "waiting on upstream fix"


@pytest.mark.asyncio
async def test_update_handoff_correction_status_rejects_bad_status(db, tmp_path):
    p, h = await _seed_handoff(db, "corr-status-bad", tmp_path)
    corr = await handoff_module.record_handoff_correction(
        db, p["id"], source_handoff_id=h["id"], blocker_classification="other",
    )
    with pytest.raises(handoff_module.HandoffCorrectionError):
        await handoff_module.update_handoff_correction_status(db, corr["id"], "nope")


@pytest.mark.asyncio
async def test_update_handoff_correction_status_unknown_id_returns_none(db):
    assert await handoff_module.update_handoff_correction_status(db, "nope", "blocked") is None


@pytest.mark.asyncio
async def test_regenerate_handoff_correction_invalidates_source_and_preserves_body(db, tmp_path):
    p, h = await _seed_handoff(db, "corr-regen", tmp_path)
    original_body = h["body"]
    corr = await handoff_module.record_handoff_correction(
        db, p["id"], source_handoff_id=h["id"], blocker_classification="scope_stale",
    )
    result = await handoff_module.regenerate_handoff_correction(
        db, p["id"], corr["id"], str(tmp_path), mode="full",
    )
    assert result["regenerated"] is True
    assert result["already_regenerated"] is False
    assert result["new_handoff_id"] is not None
    assert result["new_handoff_id"] != h["id"]
    assert result["new_token"]
    assert result["new_body_hash"] == handoff_module._hash_goal_body(result["new_handoff_content"])
    assert bool(result["invalidated_source"]["invalidated"]) is True
    assert result["invalidated_source"]["body"] == original_body  # untouched
    assert result["correction"]["status"] == "verified"
    assert result["correction"]["new_handoff_id"] == result["new_handoff_id"]
    assert result["correction"]["new_token"] == result["new_token"]

    # The ORIGINAL row, re-fetched independently, is untouched and still invalidated.
    refetched_source = await db_module.get_handoff(db, h["id"])
    assert refetched_source["body"] == original_body
    assert bool(refetched_source["invalidated"]) is True
    assert refetched_source["superseded_by_correction_id"] == corr["id"]

    # Exactly two handoffs rows exist now (source + new revision) — the fix for
    # the amend-vs-fresh interaction (see regenerate_handoff_correction's own
    # comment) means the new revision landed in a FRESH row, never an amend of
    # the just-invalidated source row.
    all_rows = await db_module.get_handoffs(db, p["id"], limit=10)
    assert len(all_rows) == 2
    assert {r["id"] for r in all_rows} == {h["id"], result["new_handoff_id"]}


@pytest.mark.asyncio
async def test_regenerate_handoff_correction_repairs_pointers_and_persists_report(db, tmp_path):
    p, h = await _seed_handoff(db, "corr-regen-pointers", tmp_path)
    corr = await handoff_module.record_handoff_correction(
        db, p["id"], source_handoff_id=h["id"], blocker_classification="pointer_unresolved",
        added_pointers=[{
            "source_type": "code",
            "targets": [{"uri": "a.py", "selector": {"type": "range", "start_line": 1, "end_line": 3}}],
        }],
    )
    result = await handoff_module.regenerate_handoff_correction(
        db, p["id"], corr["id"], str(tmp_path),
    )
    report = result["pointer_repair_report"]
    assert report["repaired_count"] == 1
    assert report["unresolved_count"] == 0
    assert result["correction"]["pointer_repair_report"]["repaired_count"] == 1


@pytest.mark.asyncio
async def test_regenerate_handoff_correction_is_idempotent(db, tmp_path):
    p, h = await _seed_handoff(db, "corr-regen-idem", tmp_path)
    corr = await handoff_module.record_handoff_correction(
        db, p["id"], source_handoff_id=h["id"], blocker_classification="other",
    )
    first = await handoff_module.regenerate_handoff_correction(
        db, p["id"], corr["id"], str(tmp_path),
    )
    rows_after_first = await db_module.get_handoffs(db, p["id"], limit=10)

    second = await handoff_module.regenerate_handoff_correction(
        db, p["id"], corr["id"], str(tmp_path),
    )
    rows_after_second = await db_module.get_handoffs(db, p["id"], limit=10)

    assert second["already_regenerated"] is True
    assert second["regenerated"] is False
    assert second["new_handoff_id"] == first["new_handoff_id"]
    assert second["new_token"] == first["new_token"]
    assert len(rows_after_second) == len(rows_after_first)  # no new row on retry


@pytest.mark.asyncio
async def test_regenerate_handoff_correction_refuses_blocked_status(db, tmp_path):
    p, h = await _seed_handoff(db, "corr-regen-blocked", tmp_path)
    corr = await handoff_module.record_handoff_correction(
        db, p["id"], source_handoff_id=h["id"], blocker_classification="other",
        status="blocked",
    )
    with pytest.raises(handoff_module.HandoffCorrectionError):
        await handoff_module.regenerate_handoff_correction(db, p["id"], corr["id"], str(tmp_path))


@pytest.mark.asyncio
async def test_regenerate_handoff_correction_refuses_superseded_status(db, tmp_path):
    p, h = await _seed_handoff(db, "corr-regen-superseded", tmp_path)
    first = await handoff_module.record_handoff_correction(
        db, p["id"], source_handoff_id=h["id"], blocker_classification="other",
    )
    await handoff_module.record_handoff_correction(  # auto-supersedes `first`
        db, p["id"], source_handoff_id=h["id"], blocker_classification="scope_stale",
    )
    with pytest.raises(handoff_module.HandoffCorrectionError):
        await handoff_module.regenerate_handoff_correction(db, p["id"], first["id"], str(tmp_path))


@pytest.mark.asyncio
async def test_regenerate_handoff_correction_unknown_id_raises(db, tmp_path):
    p = await db_module.create_project(db, "corr-regen-unknown")
    with pytest.raises(handoff_module.HandoffCorrectionError):
        await handoff_module.regenerate_handoff_correction(db, p["id"], "nope", str(tmp_path))


@pytest.mark.asyncio
async def test_regenerate_handoff_correction_cross_project_raises(db, tmp_path):
    p1, h1 = await _seed_handoff(db, "corr-regen-cross-a", tmp_path)
    p2 = await db_module.create_project(db, "corr-regen-cross-b")
    corr = await handoff_module.record_handoff_correction(
        db, p1["id"], source_handoff_id=h1["id"], blocker_classification="other",
    )
    with pytest.raises(handoff_module.HandoffCorrectionError):
        await handoff_module.regenerate_handoff_correction(db, p2["id"], corr["id"], str(tmp_path))


@pytest.mark.asyncio
async def test_load_handoff_correction_by_id_source_and_latest(db, tmp_path):
    p, h = await _seed_handoff(db, "corr-load", tmp_path)
    corr = await handoff_module.record_handoff_correction(
        db, p["id"], source_handoff_id=h["id"], blocker_classification="other",
    )
    by_id = await handoff_module.load_handoff_correction(db, p["id"], correction_id=corr["id"])
    assert by_id["id"] == corr["id"]
    assert by_id["new_handoff_content"] is None  # not regenerated yet

    latest = await handoff_module.load_handoff_correction(db, p["id"])
    assert latest["id"] == corr["id"]

    by_source = await handoff_module.load_handoff_correction(
        db, p["id"], source_handoff_id=h["id"],
    )
    assert by_source["id"] == corr["id"]


@pytest.mark.asyncio
async def test_load_handoff_correction_includes_new_content_after_regenerate(db, tmp_path):
    p, h = await _seed_handoff(db, "corr-load-regen", tmp_path)
    corr = await handoff_module.record_handoff_correction(
        db, p["id"], source_handoff_id=h["id"], blocker_classification="other",
    )
    regen = await handoff_module.regenerate_handoff_correction(
        db, p["id"], corr["id"], str(tmp_path),
    )
    loaded = await handoff_module.load_handoff_correction(db, p["id"], correction_id=corr["id"])
    assert loaded["new_handoff_content"] == regen["new_handoff_content"]


@pytest.mark.asyncio
async def test_load_handoff_correction_returns_none_when_absent(db):
    p = await db_module.create_project(db, "corr-load-none")
    assert await handoff_module.load_handoff_correction(db, p["id"]) is None


@pytest.mark.asyncio
async def test_load_handoff_correction_cross_project_raises(db, tmp_path):
    p1, h1 = await _seed_handoff(db, "corr-load-cross-a", tmp_path)
    p2 = await db_module.create_project(db, "corr-load-cross-b")
    corr = await handoff_module.record_handoff_correction(
        db, p1["id"], source_handoff_id=h1["id"], blocker_classification="other",
    )
    with pytest.raises(handoff_module.HandoffCorrectionError):
        await handoff_module.load_handoff_correction(db, p2["id"], correction_id=corr["id"])


# ---------------------------------------------------------------------------
# MCP dispatch: record_handoff_correction + load_handoff's correction surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_dispatch_record_handoff_correction_record_only(db, tmp_path):
    p, h = await _seed_handoff(db, "corr-mcp-record", tmp_path)
    result = await mcp_handler._handle_task_tools(
        "record_handoff_correction",
        {"project_id": p["id"], "source_handoff_id": h["id"],
         "blocker_classification": "pointer_unresolved"},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    assert result["regenerated"] is False
    assert result["correction"]["status"] == "draft"
    assert result["correction"]["source_handoff_id"] == h["id"]


@pytest.mark.asyncio
async def test_mcp_dispatch_record_handoff_correction_with_regenerate(db, tmp_path):
    p, h = await _seed_handoff(db, "corr-mcp-regen", tmp_path)
    result = await mcp_handler._handle_task_tools(
        "record_handoff_correction",
        {"project_id": p["id"], "source_handoff_id": h["id"],
         "blocker_classification": "other", "regenerate": True},
        db, str(tmp_path), tenant=None, _mcp_tenant_id=None,
    )
    assert result["regenerated"] is True
    assert result["new_handoff_id"]
    assert result["invalidated_source"]["invalidated"] in (1, True)


@pytest.mark.asyncio
async def test_mcp_dispatch_record_handoff_correction_invalid_source_returns_error(db):
    p = await db_module.create_project(db, "corr-mcp-invalid")
    result = await mcp_handler._handle_task_tools(
        "record_handoff_correction",
        {"project_id": p["id"], "source_handoff_id": "nope", "blocker_classification": "other"},
        db, "/tmp", tenant=None, _mcp_tenant_id=None,
    )
    assert result["error"] == "HANDOFF_CORRECTION_INVALID"


@pytest.mark.asyncio
async def test_mcp_dispatch_load_handoff_surfaces_correction_and_invalidation(db, tmp_path):
    p, h = await _seed_handoff(db, "corr-mcp-load", tmp_path)
    corr = await handoff_module.record_handoff_correction(
        db, p["id"], source_handoff_id=h["id"], blocker_classification="other",
    )
    regen = await handoff_module.regenerate_handoff_correction(
        db, p["id"], corr["id"], str(tmp_path),
    )

    result = await mcp_handler._handle_task_tools(
        "load_handoff", {"project_id": p["id"]}, db, str(tmp_path),
        tenant=None, _mcp_tenant_id=None,
    )
    assert result["correction"]["id"] == corr["id"]
    # correction.new_handoff_content is fetched by EXACT new_handoff_id (see
    # load_handoff_correction), so it is always correct regardless of how
    # load_handoff's own separate "latest handoff" lookup below resolves —
    # a receiving executor reading the `correction` field never depends on
    # get_handoffs' created_at/id tiebreak ordering at all.
    assert result["correction"]["new_handoff_content"] == regen["new_handoff_content"]
    assert result["correction"]["new_handoff_id"] == regen["new_handoff_id"]
    # The pre-existing `handoff` field (load_handoff's OWN "latest row"
    # lookup, unrelated to this feature) now carries the three new
    # invalidation keys regardless of which row it resolves to. Note:
    # get_handoffs' created_at/id ordering is only second-granularity on
    # SQLite, so which of the two rows (source vs regenerated) it considers
    # "latest" when both share a wall-clock second is a separate, known,
    # pre-existing limitation — not asserted here; only the new keys' mere
    # presence is.
    assert "invalidated" in result["handoff"]
    assert "invalidated_reason" in result["handoff"]
    assert "superseded_by_correction_id" in result["handoff"]


@pytest.mark.asyncio
async def test_mcp_dispatch_load_handoff_correction_null_when_none_recorded(db, tmp_path):
    p = await db_module.create_project(db, "corr-mcp-load-none")
    result = await mcp_handler._handle_task_tools(
        "load_handoff", {"project_id": p["id"]}, db, str(tmp_path),
        tenant=None, _mcp_tenant_id=None,
    )
    assert result["correction"] is None


# ---------------------------------------------------------------------------
# f46372e8 — load_handoff now routes its stored `content` through the SAME
# format_handoff_mcp_content() helper every generate_handoff transport uses,
# so the "one canonical serializer" guarantee actually covers every
# content-returning handoff surface, not "every one except load_handoff".
# ---------------------------------------------------------------------------


async def test_load_handoff_content_routed_through_format_handoff_mcp_content(
    db, tmp_path,
):
    p = await db_module.create_project(db, "load-handoff-canonical-serializer")
    await db_module.set_goal(db, p["id"], "ship it", sprint="s1")
    await db_module.add_sprint_item(db, p["id"], "v1", "do the thing")

    _path, generated_content, _amended = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )

    result = await mcp_handler._handle_task_tools(
        "load_handoff", {"project_id": p["id"]}, db, str(tmp_path), None, None,
    )
    assert result is not mcp_handler._MISS
    assert result["handoff"]["content"] == handoff_module.format_handoff_mcp_content(
        generated_content
    )
    # Sanity: with format_handoff_mcp_content currently an identity function,
    # this also equals the raw generated content exactly.
    assert result["handoff"]["content"] == generated_content


async def test_load_handoff_returns_none_handoff_when_none_stored(db, tmp_path):
    """No prior generate_handoff call for this project — handoff is None and
    format_handoff_mcp_content is never reached (no crash on a missing row)."""
    p = await db_module.create_project(db, "load-handoff-no-prior-handoff")
    result = await mcp_handler._handle_task_tools(
        "load_handoff", {"project_id": p["id"]}, db, str(tmp_path), None, None,
    )
    assert result is not mcp_handler._MISS
    assert result["handoff"] is None
    assert result["has_handoff"] is False
