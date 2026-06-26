"""Coverage tests for meridian.handoff and meridian.routes.handoff.

Exercises generate_handoff in every mode (full, delta, starter, planner),
the readiness-warning / empty-state branches, the L0 fallback, the custom
template path, the workspace block, the queued-session append, the small
pure helpers, and the HTTP endpoints (including error paths).
"""
from __future__ import annotations

import asyncio

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
        assert f'project_id: {p["id"]}' in content
        assert "start_session" in content
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
