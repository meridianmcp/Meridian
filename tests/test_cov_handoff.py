"""Coverage tests for meridian.handoff and meridian.routes.handoff.

Exercises generate_handoff in every mode (full, delta, starter, planner),
the readiness-warning / empty-state branches, the L0 fallback, the custom
template path, the workspace block, the queued-session append, the small
pure helpers, and the HTTP endpoints (including error paths).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from meridian import db as db_module
from meridian import handoff as handoff_module


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
    # 4cfaecc2 — the items /goal instructs a live board query up front, and the
    # test floor tracks the real suite size (524 -> 2150).
    # 0d5453bc — full suite runs ONCE at the end of the megasprint, not per item.
    assert 'get_sprint_items(status="pending")' in full
    assert "pixi run test passes 2150+" in full
    assert "ONCE at the very end of the entire" in full
    assert "not per item" in full
    assert handoff_module._DEFAULT_GOAL_TEST_FLOOR == 2150


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
