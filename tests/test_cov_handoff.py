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
    full = handoff_module._build_quick_start_goal([{"id": "abc123"}, {"id": "def456"}])
    assert "abc123" in full and "def456" in full
    assert "complete_sprint_item()" in full
    # f628b880 — non-deferential executor directive leads the items /goal.
    assert full.startswith("/goal You are an executor. Claim and execute")
    # 4cfaecc2 — the items /goal instructs a live board query up front, and the
    # test floor tracks the real suite size (524 -> 2150).
    assert 'get_sprint_items(status="pending")' in full
    assert "pixi run test passes 2150+" in full
    assert handoff_module._DEFAULT_GOAL_TEST_FLOOR == 2150


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


def test_infer_sprint_type_heuristics():
    """f9fa00e4 — sprint-type inference by count / item_group / title keyword."""
    from meridian.handoff import _infer_sprint_type
    assert _infer_sprint_type([]) == "feature"
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
    assert _infer_sprint_type(
        [{"id": "1", "title": "add button"}, {"id": "2", "title": "add page"}]) == "feature"


def test_build_quick_start_goal_tags_sprint_type():
    """f9fa00e4 — the /goal carries an inferred [sprint:TYPE] tag + guidance,
    without disturbing the existing structure."""
    from meridian.handoff import _build_quick_start_goal
    goal = _build_quick_start_goal([{"id": "a1", "title": "hotfix: crash on start"}])
    assert "[sprint:hotfix]" in goal
    assert "HOTFIX sprint" in goal
    assert "a1" in goal
    assert 'get_sprint_items(status="pending")' in goal
    assert "Stop after" in goal


def test_build_quick_start_goal_completion_mode_and_group_style():
    """9f57374b — completion_mode='lenient' drops the anti-stop failure framing;
    goal_group_style='waves' restores Wave headers for a dependency graph; the
    executor_config extractors default to strict/flat."""
    from meridian.handoff import (
        _build_quick_start_goal,
        _completion_mode_from_settings,
        _goal_group_style_from_settings,
    )
    assert "is a FAILURE" in _build_quick_start_goal([{"id": "a1"}])
    assert "is a FAILURE" not in _build_quick_start_goal(
        [{"id": "a1"}], completion_mode="lenient")

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
    phrasing. Both carry the anti-stop failure framing."""
    flat = handoff_module._build_quick_start_goal([{"id": "a1"}, {"id": "a2"}])
    assert "Complete sprint items: a1, a2." in flat
    assert "Wave" not in flat
    assert "is a FAILURE" in flat
    waved = handoff_module._build_quick_start_goal([
        {"id": "a1"}, {"id": "a2", "depends_on": "a1"},
    ])
    assert "dependency order" in waved
    assert "a1, a2" in waved
    assert "Wave" not in waved
    assert "is a FAILURE" in waved


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
    _, content = await handoff_module.generate_handoff(
        db, stale["id"], str(tmp_path), skip_ai_summary=True
    )
    assert "Executor rules are behind the current standard" in content

    fresh = await db_module.create_project(db, "fresh-proj")
    await db_module.set_agent_instructions(db, fresh["id"], ad.DEFAULT_AGENT_INSTRUCTIONS)
    _, content2 = await handoff_module.generate_handoff(
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
    _, content = await handoff_module.generate_handoff(
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
    path, content = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert "HANDOFF READINESS" in content
    assert "No sprint name set" in content
    assert "No pending sprint items" in content
    assert "No pinned decisions" in content
    assert path.endswith("alpha-nogoal_handoff.md")


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
    _, content = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert "Use psycopg3" in content
    assert "Workspace (applies to all projects)" in content
    assert "Workspace rule" in content
    assert "Sprint: sprint-rich" in content
    assert "1 pinned decision" in content


@pytest.mark.asyncio
async def test_generate_handoff_with_ai_summary_stub(db, tmp_path):
    """summarizer stub injects the ai_summary blurb (non-skip path)."""
    p = await db_module.create_project(db, "alpha-aisum")
    await db_module.set_goal(db, p["id"], "ship", sprint="s")
    s = await db_module.register_session(db, p["id"], "sess-ai")
    await db_module.log_task(db, s["id"], p["id"], "made progress", "done")

    def _summarizer(prompt):
        return "STUB SUMMARY: did work, do more."

    _, content = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), summarizer=_summarizer, skip_ai_summary=False
    )
    assert "STUB SUMMARY" in content


@pytest.mark.asyncio
async def test_generate_handoff_delta_with_in_progress(db, tmp_path):
    """Delta mode surfaces 'Currently running' for in_progress items."""
    p = await db_module.create_project(db, "alpha-delta-ip")
    await db_module.set_goal(db, p["id"], "delta work")
    running = await db_module.add_sprint_item(db, p["id"], "v1", "Running item")
    await db_module.add_sprint_item(db, p["id"], "v1", "Pending item")
    await db_module.patch_sprint_item(
        db, p["id"], running["id"], status="in_progress"
    )
    _, content = await handoff_module.generate_handoff(
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
    _, content = await handoff_module.generate_handoff(
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
    await db_module.patch_sprint_item(db, p["id"], it["id"], status="done")

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
    await db_module.patch_sprint_item(db, p["id"], it["id"], status="done")

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
    await db_module.patch_sprint_item(db, p["id"], it["id"], status="done")
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
    path, content = await handoff_module.generate_handoff(
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
    assert path.endswith("alpha-planner_planner_handoff.md")


@pytest.mark.asyncio
async def test_generate_handoff_planner_mode_minimal(db, tmp_path):
    """Planner mode with no goal/items/HITLs still renders a clean prompt with
    'none' placeholders rather than crashing."""
    p = await db_module.create_project(db, "alpha-planner-min")
    _, content = await handoff_module.generate_handoff(
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
        path, content = await handoff_module.generate_handoff(
            db, p["id"], str(tmp_path), mode=mode
        )
        assert f'start_session(project_name="{p["name"]}"' in content  # 11a91d31
        assert f'project_id (fallback): {p["id"]}' in content
        assert it2["id"][:8] in content
        assert "Done:" in content
        assert path.endswith("alpha-starter-cov_starter.md")


@pytest.mark.asyncio
async def test_generate_handoff_starter_no_completed(db, tmp_path):
    """Starter renders 'Done: (none)' and 'Pending (none)' when empty."""
    p = await db_module.create_project(db, "alpha-starter-empty")
    _, content = await handoff_module.generate_handoff(
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
    _, content = await handoff_module.generate_handoff(
        db, p["id"], str(tmp_path), skip_ai_summary=True
    )
    assert "QUEUED NEXT SESSION" in content
    assert "do the queued thing" in content
    # Second call — queue cleared, no longer present.
    _, content2 = await handoff_module.generate_handoff(
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
    assert path.endswith("alpha-l0_handoff.md")


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
    assert body["path"].endswith("http-full_handoff.md")


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
