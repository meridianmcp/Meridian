"""Regression tests for 0b0b24d8 -- workspace notes are opt-in, 'full' is never
a default.

Owner complaint: "I don't like 'full' by default; it includes workspace notes".
aec043cb already made an OMITTED ``mode`` on the MCP/HTTP generate_handoff tool
intent-based. Three internal Python callers still inherited the function's own
``mode="full"`` default (session-close auto-save, the idle-expire loop,
proposal promotion), and a cold session start / ``get_context_block`` prepended
every workspace decision and note unconditionally. This file pins:

  1. ``generate_handoff``'s own default can never mean 'full', and EVERY call
     site (parametrized below) keeps workspace decisions/notes out of what it
     writes, unless 'full' is requested explicitly.
  2. A source scan fails any ``generate_handoff(`` call that omits ``mode=``.
  3. Session start and ``get_context_block`` carry a bounded workspace INDEX by
     default (no note bodies) and the old full text only when
     ``include_workspace_context`` is on.
"""
from __future__ import annotations

import ast
import inspect
import os
import pathlib
import threading
import time

import pytest

import meridian.db as db_module
import meridian.server as srv
from meridian import _deps
from meridian import handoff as handoff_module
from meridian import proposal_promotion
from meridian import toml_config

_ROOT = pathlib.Path(__file__).resolve().parent.parent

_FLAG_ENV = "MERIDIAN_INCLUDE_WORKSPACE_CONTEXT"

NOTE_TITLE = "ZZNOTE-title-0b0b24d8"
NOTE_BODY = "ZZNOTE-body-0b0b24d8 unrelated interview feedback"
DECISION_TITLE = "ZZDEC-title-0b0b24d8"
DECISION_BODY = "ZZDEC-body-0b0b24d8 standing policy sentence"
_SENTINELS = (NOTE_TITLE, NOTE_BODY, DECISION_TITLE, DECISION_BODY)


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    """A developer's real env must not flip these tests."""
    monkeypatch.delenv(_FLAG_ENV, raising=False)


@pytest.fixture
def _no_claude_md_write(monkeypatch):
    """_expire_and_generate_handoffs / close_session regenerate the repo's
    CLAUDE.md as a side effect; keep the checkout clean."""
    async def _noop(*_a, **_k):
        return None
    monkeypatch.setattr(srv, "_regenerate_claude_md", _noop)


async def _seed_workspace(db) -> None:
    await db_module.pin_workspace_decision(db, DECISION_TITLE, DECISION_BODY, "STRATEGIC")
    await db_module.add_workspace_note(db, NOTE_TITLE, NOTE_BODY, "research")


async def _project(db, name: str) -> str:
    p = await db_module.create_project(db, name)
    await db_module.add_sprint_item(db, p["id"], "v1", "Ship it")
    return p["id"]


def _leaks(text: str) -> list[str]:
    return [s for s in _SENTINELS if s in text]


def _written_text(directory: pathlib.Path) -> str:
    return "\n".join(
        f.read_text(encoding="utf-8", errors="replace")
        for f in directory.rglob("*") if f.is_file()
    )


class _Spy:
    """Wraps the real generate_handoff, recording how each call was made and
    what it returned. Installed on the module attribute, so it also sees the
    in-module call inside regenerate_handoff_correction."""

    def __init__(self, real):
        self._real = real
        self.calls: list[dict] = []
        self.finished = threading.Event()

    async def __call__(self, *args, **kwargs):
        record = {
            "kwargs_mode": kwargs.get("mode"),
            "mode_passed": "mode" in kwargs,
            # Resolved BEFORE the call: the call itself marks the session as
            # having produced a handoff, which changes the answer.
            "effective_mode": handoff_module.resolve_handoff_mode(
                kwargs.get("mode"), kwargs.get("session_id")
            ),
            "content": "",
        }
        self.calls.append(record)
        try:
            result = await self._real(*args, **kwargs)
        finally:
            self.finished.set()
        record["content"] = result[1]
        return result


@pytest.fixture
def spy(monkeypatch):
    s = _Spy(handoff_module.generate_handoff)
    monkeypatch.setattr(handoff_module, "generate_handoff", s)
    return s


# ---------------------------------------------------------------------------
# 1. The Python default itself
# ---------------------------------------------------------------------------


def test_generate_handoff_mode_default_is_none_never_full():
    for fn in (
        handoff_module.generate_handoff,
        handoff_module.regenerate_handoff_correction,
        handoff_module.amend_handoff,
    ):
        default = inspect.signature(fn).parameters["mode"].default
        assert default is None, (fn.__name__, default)


@pytest.mark.asyncio
async def test_omitted_mode_resolves_to_bounded_goal_and_persists_as_goal(db, tmp_path):
    pid = await _project(db, "0b0b24d8-omitted")
    await _seed_workspace(db)
    _, content, _ = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True,
    )
    assert _leaks(content) == []
    assert (await db_module.get_handoffs(db, pid, limit=1))[0]["mode"] == "goal"


@pytest.mark.asyncio
async def test_omitted_mode_for_a_resumed_session_resolves_to_delta(db, tmp_path):
    pid = await _project(db, "0b0b24d8-resumed")
    await _seed_workspace(db)
    sess = await db_module.register_session(db, pid, "resumed")
    await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, session_id=sess["id"],
    )
    assert (await db_module.get_handoffs(db, pid, limit=1))[0]["mode"] == "goal"
    # The unconsumed first handoff is amended in place (edd9c54b), now as delta.
    await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, session_id=sess["id"],
    )
    rows = await db_module.get_handoffs(db, pid, limit=10)
    assert [r["mode"] for r in rows] == ["delta"]
    assert _leaks(rows[0]["body"]) == []


@pytest.mark.asyncio
async def test_unrecognized_mode_string_still_raises(db, tmp_path):
    """Only None means 'not specified'; a typo must not degrade quietly."""
    pid = await _project(db, "0b0b24d8-typo")
    with pytest.raises(ValueError, match="mode must be"):
        await handoff_module.generate_handoff(
            db, pid, str(tmp_path), skip_ai_summary=True, mode="fulll",
        )


@pytest.mark.asyncio
async def test_explicit_full_still_includes_workspace_decisions_and_notes(db, tmp_path):
    """The archival dump survives as an explicit request -- and proves the
    absence assertions in this file are not vacuous."""
    pid = await _project(db, "0b0b24d8-full-explicit")
    await _seed_workspace(db)
    _, content, _ = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="full",
    )
    assert sorted(_leaks(content)) == sorted(_SENTINELS)


# ---------------------------------------------------------------------------
# 2. Every generate_handoff call site: no workspace notes unless mode='full'
# ---------------------------------------------------------------------------


async def _drv_direct_omitted(db, tmp_path, pid):
    await handoff_module.generate_handoff(db, pid, str(tmp_path), skip_ai_summary=True)


async def _drv_resumed_session_omitted(db, tmp_path, pid):
    sess = await db_module.register_session(db, pid, "resumed")
    for _ in range(2):
        await handoff_module.generate_handoff(
            db, pid, str(tmp_path), skip_ai_summary=True, session_id=sess["id"],
        )


async def _drv_idle_expire_loop(db, tmp_path, pid):
    s = await db_module.register_session(db, pid, "stale")
    await db.execute(
        "UPDATE sessions SET last_seen = datetime('now', '-60 minutes') WHERE id = ?",
        (s["id"],),
    )
    await db.commit()
    result = await srv._expire_and_generate_handoffs(db, str(tmp_path))
    assert result["auto_handoff_generated"] is True


async def _drv_proposal_promotion(db, tmp_path, pid):
    proposal = await db_module.add_workspace_proposal(db, "Idea", "body")
    preview = await proposal_promotion.preview_proposal_promotion(
        db, proposal["id"], pid, "executable_handoff", infer_touches_resources=False,
    )
    result = await proposal_promotion.commit_proposal_promotion(
        db, proposal["id"], pid, "executable_handoff", preview["preview_hash"],
        infer_touches_resources=False, data_dir=str(tmp_path),
    )
    assert result["committed"]["handoff"]["path"]


async def _seed_source_handoff(db, tmp_path, pid):
    await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="goal",
    )
    return (await db_module.get_handoffs(db, pid, limit=1))[0]


async def _drv_correction_regeneration(db, tmp_path, pid):
    h = await _seed_source_handoff(db, tmp_path, pid)
    corr = await handoff_module.record_handoff_correction(
        db, pid, source_handoff_id=h["id"], blocker_classification="scope_stale",
    )
    result = await handoff_module.regenerate_handoff_correction(
        db, pid, corr["id"], str(tmp_path),
    )
    assert result["regenerated"] is True


async def _drv_mcp_correction_regeneration(db, tmp_path, pid):
    h = await _seed_source_handoff(db, tmp_path, pid)
    result = await srv._dispatch_mcp_tool(
        "record_handoff_correction",
        {
            "project_id": pid, "source_handoff_id": h["id"],
            "blocker_classification": "scope_stale", "regenerate": True,
        },
        db, str(tmp_path),
    )
    assert result["regenerated"] is True


async def _drv_mcp_generate_handoff(db, tmp_path, pid):
    await srv._dispatch_mcp_tool("generate_handoff", {"project_id": pid}, db, str(tmp_path))


# (driver, mode the call site must pass EXPLICITLY, or None when the call
# deliberately forwards an omitted mode and relies on the default)
_CALL_SITES = [
    pytest.param(_drv_direct_omitted, None, id="direct-omitted-mode"),
    pytest.param(_drv_resumed_session_omitted, None, id="resumed-session-omitted-mode"),
    pytest.param(_drv_idle_expire_loop, "delta", id="server-idle-expire-loop"),
    pytest.param(_drv_proposal_promotion, "goal", id="proposal-promotion-executable-handoff"),
    pytest.param(_drv_correction_regeneration, None, id="regenerate-handoff-correction"),
    pytest.param(_drv_mcp_correction_regeneration, None, id="mcp-record-handoff-correction"),
    pytest.param(_drv_mcp_generate_handoff, None, id="mcp-generate-handoff"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("driver,explicit_mode", _CALL_SITES)
async def test_call_site_never_writes_workspace_notes_unless_full_is_explicit(
    db, tmp_path, spy, _no_claude_md_write, driver, explicit_mode,
):
    pid = await _project(db, "0b0b24d8-call-site")
    await _seed_workspace(db)

    await driver(db, tmp_path, pid)

    assert spy.calls, "the call site never reached generate_handoff"
    for call in spy.calls:
        assert call["effective_mode"] != "full", call
        assert _leaks(call["content"]) == [], call["effective_mode"]
    assert _leaks(_written_text(tmp_path)) == []
    for row in await db_module.get_handoffs(db, pid, limit=20):
        assert _leaks(row["body"]) == [], row["mode"]
    if explicit_mode is not None:
        assert any(
            c["mode_passed"] and c["kwargs_mode"] == explicit_mode for c in spy.calls
        ), (explicit_mode, spy.calls)


def test_session_close_auto_save_passes_explicit_delta_and_writes_no_workspace_notes(
    client, spy, _no_claude_md_write,
):
    """routes/sessions.py close_session: the fire-and-forget auto-save."""
    client.post("/workspace/decisions", json={
        "title": DECISION_TITLE, "body": DECISION_BODY, "category": "STRATEGIC",
    })
    client.post("/workspace/notes", json={
        "title": NOTE_TITLE, "body": NOTE_BODY, "tags": "research",
    })
    project = client.post("/projects", json={"name": "0b0b24d8-close"}).json()
    sess = client.post(
        "/sessions/register", json={"project_id": project["id"], "name": "s1"},
    ).json()

    assert client.post(f"/sessions/{sess['id']}/close").status_code == 200

    # The auto-save is a background task: wait for the spy to see it finish.
    deadline = time.monotonic() + 20
    while not spy.finished.is_set() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert spy.finished.is_set(), "close_session never ran its auto-save handoff"

    assert [c["kwargs_mode"] for c in spy.calls] == ["delta"]
    assert spy.calls[0]["mode_passed"] is True
    assert _leaks(spy.calls[0]["content"]) == []
    assert _leaks(_written_text(pathlib.Path(client.app.state.data_dir))) == []


# ---------------------------------------------------------------------------
# 3. Source scan: no generate_handoff( call may omit mode=
# ---------------------------------------------------------------------------


def _calls_omitting_mode(path: pathlib.Path) -> list[int]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name == "generate_handoff" and "mode" not in {k.arg for k in node.keywords}:
            hits.append(node.lineno)
    return hits


def _non_test_python_files():
    # os.walk with pruning: rglob would still descend into node_modules/.pixi.
    skip = {"node_modules", ".git", "tests", "__pycache__", ".pixi", ".venv", "venv"}
    for dirpath, dirnames, filenames in os.walk(_ROOT):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames:
            if name.endswith(".py"):
                yield pathlib.Path(dirpath) / name


def test_no_generate_handoff_call_omits_mode():
    """A call that leaves ``mode`` off silently depends on the default, which
    is exactly how three internal callers once inherited 'full'. The function's
    own ``def`` is a FunctionDef, not a Call, so it needs no allowlist entry;
    tests are excluded by design (they exercise the default on purpose)."""
    offenders = {
        str(p.relative_to(_ROOT)): lines
        for p in _non_test_python_files()
        if (lines := _calls_omitting_mode(p))
    }
    assert offenders == {}, (
        "generate_handoff( calls without mode= (pass an explicit bounded mode, "
        f"or mode=None when deliberately forwarding an omitted one): {offenders}"
    )


def test_source_scan_detects_an_omitting_call(tmp_path):
    """The scanner is not vacuous: it flags a bare call and passes mode=."""
    bad = tmp_path / "bad.py"
    bad.write_text("async def f(db, m):\n    await m.generate_handoff(db, 'p', 'd')\n")
    ok = tmp_path / "ok.py"
    ok.write_text("async def f(db, m):\n    await m.generate_handoff(db, 'p', 'd', mode='goal')\n")
    assert _calls_omitting_mode(bad) == [2]
    assert _calls_omitting_mode(ok) == []


# ---------------------------------------------------------------------------
# 4. include_workspace_context: sources and precedence
# ---------------------------------------------------------------------------


def test_include_workspace_context_defaults_off(monkeypatch):
    monkeypatch.setattr(toml_config, "load_toml", lambda: None)
    assert toml_config.get_include_workspace_context() is False


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("on", True), ("yes", True),
    ("0", False), ("false", False), ("off", False),
])
def test_include_workspace_context_env_values(monkeypatch, raw, expected):
    monkeypatch.setattr(toml_config, "load_toml", lambda: None)
    monkeypatch.setenv(_FLAG_ENV, raw)
    assert toml_config.get_include_workspace_context() is expected


def test_include_workspace_context_toml_and_env_precedence(monkeypatch):
    monkeypatch.setattr(
        toml_config, "load_toml", lambda: {"meridian": {"include_workspace_context": True}},
    )
    assert toml_config.get_include_workspace_context() is True
    # env wins over toml, in both directions
    monkeypatch.setenv(_FLAG_ENV, "0")
    assert toml_config.get_include_workspace_context() is False
    # a blank env value is "not set", so toml applies
    monkeypatch.setenv(_FLAG_ENV, "  ")
    assert toml_config.get_include_workspace_context() is True


# ---------------------------------------------------------------------------
# 5. The bounded index renderer
# ---------------------------------------------------------------------------


def test_index_block_is_empty_when_there_is_nothing():
    assert _deps._render_workspace_index_block([], []) == ""


def test_index_block_withholds_note_bodies_but_lists_policy_titles_and_fetch_calls():
    notes = [
        {"title": "Release process", "body": "NOTEBODY-1", "tags": "Policy, ops"},
        {"title": "Style guide", "body": "NOTEBODY-2", "tags": "workspace-policy"},
        {"title": "Interview feedback", "body": "NOTEBODY-3", "tags": "research"},
        {"title": "No tags", "body": "NOTEBODY-4", "tags": None},
    ]
    block = _deps._render_workspace_index_block([], notes)
    assert block.startswith("WORKSPACE (applies to all projects)")
    assert "4 workspace note(s)" in block
    assert "Release process" in block and "Style guide" in block
    assert "Interview feedback" not in block and "No tags" not in block
    assert "NOTEBODY" not in block
    assert 'get_workspace_notes(tag="policy")' in block
    assert "get_workspace_notes()" in block


def test_index_block_without_policy_notes_names_only_the_plain_fetch():
    block = _deps._render_workspace_index_block(
        [], [{"title": "Plain", "body": "b", "tags": "research"}],
    )
    assert "1 workspace note(s)" in block
    assert "policy-tagged" not in block
    assert 'tag="policy"' not in block
    assert "get_workspace_notes()" in block


def test_index_block_caps_decisions_and_clips_bodies():
    decisions = [
        {"title": f"D{i}", "category": "TECHNICAL", "body": "x" * 500}
        for i in range(12)
    ]
    block = _deps._render_workspace_index_block(decisions, [])
    shown = [ln for ln in block.splitlines() if "DECISION" in ln]
    assert len(shown) == _deps._WORKSPACE_INDEX_MAX_DECISIONS
    assert "[TECHNICAL] D0" in block and "D4" in block and "D5" not in block
    assert "(+7 more decision(s))" in block
    assert "x" * 300 not in block
    assert "get_workspace_decisions()" in block


def test_index_block_keeps_a_short_decision_whole_and_skips_the_fetch_call():
    block = _deps._render_workspace_index_block(
        [{"title": "Monorepo", "category": "ARCHITECTURAL", "body": "one repo"}], [],
    )
    assert "DECISION [ARCHITECTURAL] Monorepo: one repo" in block
    assert "get_workspace_decisions()" not in block


def test_index_block_size_is_bounded_regardless_of_record_count():
    decisions = [{"title": f"D{i}", "body": "d" * 5000} for i in range(300)]
    notes = [
        {"title": f"N{i}", "body": "n" * 5000, "tags": "policy"} for i in range(300)
    ]
    block = _deps._render_workspace_index_block(decisions, notes)
    assert len(block) < 2500
    assert block.count("; ") <= _deps._WORKSPACE_INDEX_MAX_POLICY_TITLES  # titles capped


# ---------------------------------------------------------------------------
# 6. Session start and get_context_block: index by default, full when on
# ---------------------------------------------------------------------------


def _start_session_workspace_context(client, name: str) -> str:
    project = client.post("/projects", json={"name": name}).json()
    r = client.post(
        f"/projects/{project['id']}/start-session", json={"session_name": "alpha"},
    )
    assert r.status_code == 200
    return r.json()["workspace_context"]


def _seed_workspace_http(client) -> None:
    client.post("/workspace/decisions", json={
        "title": DECISION_TITLE, "body": DECISION_BODY, "category": "STRATEGIC",
    })
    client.post("/workspace/notes", json={
        "title": NOTE_TITLE, "body": NOTE_BODY, "tags": "research",
    })
    client.post("/workspace/notes", json={
        "title": "ZZPOLICY-title", "body": "ZZPOLICY-body", "tags": "policy",
    })


def test_start_session_omits_workspace_note_bodies_by_default(client):
    _seed_workspace_http(client)
    ctx = _start_session_workspace_context(client, "0b0b24d8-ss-default")
    assert NOTE_BODY not in ctx
    assert "ZZPOLICY-body" not in ctx
    assert NOTE_TITLE not in ctx            # only policy-tagged titles are listed
    assert "ZZPOLICY-title" in ctx
    assert "2 workspace note(s)" in ctx
    assert 'get_workspace_notes(tag="policy")' in ctx
    # decisions are standing policy: kept, as a capped one-line summary
    assert DECISION_TITLE in ctx and DECISION_BODY in ctx


def test_start_session_includes_workspace_note_bodies_when_setting_is_on(client, monkeypatch):
    _seed_workspace_http(client)
    monkeypatch.setenv(_FLAG_ENV, "1")
    ctx = _start_session_workspace_context(client, "0b0b24d8-ss-on")
    assert NOTE_BODY in ctx and "ZZPOLICY-body" in ctx and NOTE_TITLE in ctx
    assert DECISION_BODY in ctx
    assert "index only" not in ctx


def test_start_session_workspace_context_stays_empty_when_nothing_exists(client):
    assert _start_session_workspace_context(client, "0b0b24d8-ss-empty") == ""


async def _context_block_text(db, pid) -> str:
    result = await srv._dispatch_mcp_tool("get_context_block", {"project_id": pid}, db, "/tmp")
    return result["text"]


@pytest.mark.asyncio
async def test_get_context_block_omits_workspace_note_bodies_by_default(db):
    pid = await _project(db, "0b0b24d8-cb-default")
    await _seed_workspace(db)
    await db_module.add_workspace_note(db, "ZZPOLICY-title", "ZZPOLICY-body", "policy")
    text = await _context_block_text(db, pid)
    assert NOTE_BODY not in text and "ZZPOLICY-body" not in text
    assert NOTE_TITLE not in text
    assert "ZZPOLICY-title" in text
    assert "2 workspace note(s)" in text
    assert DECISION_BODY in text
    # the workspace index leads the block, as the full block always did
    assert text.index("WORKSPACE (applies to all projects)") < text.index("PROJECT:")
    assert text.startswith("<meridian_context")


@pytest.mark.asyncio
async def test_get_context_block_includes_workspace_note_bodies_when_setting_is_on(
    db, monkeypatch,
):
    pid = await _project(db, "0b0b24d8-cb-on")
    await _seed_workspace(db)
    monkeypatch.setenv(_FLAG_ENV, "true")
    text = await _context_block_text(db, pid)
    assert NOTE_BODY in text and NOTE_TITLE in text and DECISION_BODY in text
    assert "index only" not in text


@pytest.mark.asyncio
async def test_workspace_context_helper_matches_each_renderer(db, monkeypatch):
    await _seed_workspace(db)
    decisions = await db_module.get_workspace_decisions(db)
    notes = await db_module.get_workspace_notes(db)
    assert await _deps._build_workspace_context_block(db) == (
        _deps._render_workspace_index_block(decisions, notes)
    )
    monkeypatch.setenv(_FLAG_ENV, "1")
    assert await _deps._build_workspace_context_block(db) == (
        _deps._render_workspace_block(decisions, notes)
    )
