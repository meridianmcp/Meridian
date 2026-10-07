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
import asyncio
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
    """A developer's real env AND meridian.toml must not flip these tests.

    The toml is the documented self-host way to turn the flag on, so an owner
    who does exactly that in the repo-root meridian.toml (which load_toml finds
    through the cwd) would otherwise see every default-is-off test here fail.
    Only the one key is stripped: every other table the app reads at startup
    passes through untouched, and a test that wants a toml value patches
    load_toml itself, which wins over this wrapper.
    """
    monkeypatch.delenv(_FLAG_ENV, raising=False)
    real_load_toml = toml_config.load_toml

    def _load_toml_without_the_flag():
        data = real_load_toml()
        table = data.get("meridian") if isinstance(data, dict) else None
        if isinstance(table, dict) and "include_workspace_context" in table:
            data = {
                **data,
                "meridian": {
                    k: v for k, v in table.items() if k != "include_workspace_context"
                },
            }
        return data

    monkeypatch.setattr(toml_config, "load_toml", _load_toml_without_the_flag)


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
            "session_id": kwargs.get("session_id"),
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


def _rest_regenerate_correction(client, monkeypatch, extra_body: dict) -> dict:
    """Seed workspace records and a goal handoff over HTTP, then POST
    /projects/{id}/handoff/corrections with regenerate=true. Returns the JSON."""
    # No Haiku call from a developer's shell: the summary step keys off this.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _seed_workspace_http(client)
    pid = client.post("/projects", json={"name": "0b0b24d8-rest-correction"}).json()["id"]
    assert client.post(
        f"/projects/{pid}/sprint-items", json={"version": "v1", "title": "Ship it"},
    ).status_code == 201
    assert client.post(f"/projects/{pid}/handoff", json={"mode": "goal"}).status_code == 200

    # There is no REST listing of handoff rows; read the id from the app's db
    # the same way other client-based tests reach into it.
    async def _source_id() -> str:
        rows = await db_module.get_handoffs(client.app.state.db, pid, limit=1)
        return rows[0]["id"]

    resp = client.post(
        f"/projects/{pid}/handoff/corrections",
        json={
            "source_handoff_id": asyncio.run(_source_id()),
            "blocker_classification": "scope_stale",
            "regenerate": True,
            **extra_body,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_rest_correction_regeneration_without_mode_never_writes_workspace_text(
    client, spy, monkeypatch, _no_claude_md_write,
):
    """POST /projects/{id}/handoff/corrections is the REST mirror of the MCP
    record_handoff_correction tool. Its ``mode=body.get("mode") or None`` once
    read ``or "full"``, which silently turned an omitted mode into the archival
    dump. Nothing else covered that line."""
    result = _rest_regenerate_correction(client, monkeypatch, {})

    assert result["regenerated"] is True
    # spy.calls[0] is the goal handoff seeded above; the regeneration is last.
    regen = spy.calls[-1]
    assert len(spy.calls) == 2
    # behaviour first, so a regression names the leaked text...
    assert _leaks(regen["content"]) == []
    assert _leaks(result["new_handoff_content"]) == []
    assert _leaks(_written_text(pathlib.Path(client.app.state.data_dir))) == []
    # ...then the shape of the call: nothing was forced, the default resolved it
    assert regen["effective_mode"] != "full", regen
    assert regen["kwargs_mode"] is None and regen["mode_passed"] is True

    async def _rows():
        return await db_module.get_handoffs(client.app.state.db, result["correction"]["project_id"], limit=20)

    rows = asyncio.run(_rows())
    assert rows and all(r["mode"] != "full" for r in rows), [r["mode"] for r in rows]
    assert all(_leaks(r["body"]) == [] for r in rows)


def test_rest_correction_regeneration_with_explicit_full_still_includes_everything(
    client, spy, monkeypatch, _no_claude_md_write,
):
    """The archival dump stays reachable on purpose, and this proves the
    absence assertion above is not vacuous over the same HTTP path."""
    result = _rest_regenerate_correction(client, monkeypatch, {"mode": "full"})

    assert result["regenerated"] is True
    assert spy.calls[-1]["kwargs_mode"] == "full"
    assert sorted(_leaks(result["new_handoff_content"])) == sorted(_SENTINELS)


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


# The scan above only proves ``mode=`` is PRESENT. ``mode=body.get("mode") or
# "full"`` satisfies it while turning an omitted mode back into the archival
# dump, which is exactly what the REST correction endpoint once did. This scan
# checks the VALUE: a function that forwards ``mode`` to the handoff family must
# not default it to, or fall back to, the literal "full".
#
# ``resolve_handoff_mode`` is in the family because every transport (REST, HTTP
# MCP, stdio MCP) feeds the caller's mode through it: ``resolve_handoff_mode(
# arguments.get("mode") or "full", ...)`` makes an omitted mode an explicit
# 'full' request, which the function then honours. That form has no ``mode=``
# keyword and no ``mode = ...`` assignment, so it needs its own check below.
_HANDOFF_FAMILY = frozenset(
    {
        "generate_handoff", "regenerate_handoff_correction", "amend_handoff",
        "resolve_handoff_mode",
    }
)


def _call_name(node: ast.Call) -> "str | None":
    fn = node.func
    return fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)


def _is_full_const(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == "full"


def _falls_back_to_full(expr: ast.AST) -> bool:
    """``x or "full"``, ``x or y or "full"`` and ``x if c else "full"``: an
    expression that turns "not specified" into 'full'. A plain ``"full"``
    constant is an explicit request and is allowed."""
    if isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.Or):
        return any(_is_full_const(v) or _falls_back_to_full(v) for v in expr.values)
    if isinstance(expr, ast.IfExp):
        return any(
            _is_full_const(b) or _falls_back_to_full(b) for b in (expr.body, expr.orelse)
        )
    return False


def _full_by_default_sites(path: pathlib.Path) -> list[int]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    hits: set[int] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call) and _call_name(n) in _HANDOFF_FAMILY
        ]
        if not calls:
            continue
        # (1) ``def f(..., mode="full")`` on a function that forwards mode
        positional = fn.args.posonlyargs + fn.args.args
        padding = [None] * (len(positional) - len(fn.args.defaults))
        for arg, default in [
            *zip(positional, padding + list(fn.args.defaults)),
            *zip(fn.args.kwonlyargs, fn.args.kw_defaults),
        ]:
            if arg.arg == "mode" and default is not None and _is_full_const(default):
                hits.add(fn.lineno)
        # (2) ``generate_handoff(..., mode=<x or "full">)``
        for call in calls:
            for kw in call.keywords:
                if kw.arg == "mode" and _falls_back_to_full(kw.value):
                    hits.add(kw.value.lineno)
            # (4) ``resolve_handoff_mode(<x or "full">, ...)``: its first
            # parameter is the requested mode, positional or by keyword
            if _call_name(call) == "resolve_handoff_mode":
                requested = [kw.value for kw in call.keywords if kw.arg == "requested_mode"]
                requested += call.args[:1]
                for expr in requested:
                    if _falls_back_to_full(expr):
                        hits.add(expr.lineno)
        # (3) ``mode = <x or "full">`` computed before the call
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "mode" for t in node.targets)
                and _falls_back_to_full(node.value)
            ):
                hits.add(node.lineno)
    return sorted(hits)


def test_no_handoff_caller_defaults_or_falls_back_to_full():
    offenders = {
        str(p.relative_to(_ROOT)): lines
        for p in _non_test_python_files()
        if (lines := _full_by_default_sites(p))
    }
    assert offenders == {}, (
        "a function forwarding mode to generate_handoff / "
        "regenerate_handoff_correction / amend_handoff defaults it to, or falls "
        f"back to, 'full' (use None so the intent-based default applies): {offenders}"
    )


@pytest.mark.parametrize("source,expected", [
    pytest.param(  # the REST correction endpoint's historical bug
        "async def f(m, body):\n"
        "    await m.regenerate_handoff_correction(db, 'p', 'c', 'd', mode=body.get('mode') or 'full')\n",
        [2], id="keyword-or-full",
    ),
    pytest.param(
        "async def f(m, body):\n"
        "    await m.generate_handoff(db, 'p', 'd', mode=body.get('m') or body.get('n') or 'full')\n",
        [2], id="keyword-chained-or-full",
    ),
    pytest.param(
        "async def f(m, x):\n"
        "    await m.generate_handoff(db, 'p', 'd', mode=x if x else 'full')\n",
        [2], id="keyword-ternary-full",
    ),
    pytest.param(
        "async def f(m, body):\n"
        "    mode = body.get('mode') or 'full'\n"
        "    await m.amend_handoff(db, 'p', 's', 'd', mode=mode)\n",
        [2], id="assignment-or-full",
    ),
    pytest.param(
        "async def f(m, mode='full'):\n"
        "    await m.generate_handoff(db, 'p', 'd', mode=mode)\n",
        [1], id="parameter-default-full",
    ),
    pytest.param(
        "async def f(m, *, mode: str = 'full'):\n"
        "    await m.generate_handoff(db, 'p', 'd', mode=mode)\n",
        [1], id="kwonly-parameter-default-full",
    ),
    pytest.param(  # the stdio transport's form: no mode= keyword at all
        "async def f(m, arguments):\n"
        "    mode = m.resolve_handoff_mode(\n"
        "        arguments.get('mode') or 'full',\n"
        "        None,\n"
        "    )\n",
        [3], id="resolve-positional-or-full",
    ),
    pytest.param(
        "async def f(m, arguments):\n"
        "    mode = m.resolve_handoff_mode(requested_mode=arguments.get('mode') or 'full')\n",
        [2], id="resolve-keyword-or-full",
    ),
    pytest.param(
        "async def f(m, x):\n"
        "    mode = m.resolve_handoff_mode(x if x else 'full', None)\n",
        [2], id="resolve-ternary-full",
    ),
    pytest.param(
        "async def f(m, arguments):\n"
        "    mode = m.resolve_handoff_mode(arguments.get('mode'), None, session_role='executor')\n",
        [], id="resolve-forwarding-the-raw-mode",
    ),
    pytest.param(
        "async def f(m):\n"
        "    mode = m.resolve_handoff_mode('full', None)\n",
        [], id="resolve-explicit-full-constant",
    ),
    # allowed: forwarding None, an explicit constant request, or a "full"
    # label that never feeds a handoff call
    pytest.param(
        "async def f(m, body):\n"
        "    await m.regenerate_handoff_correction(db, 'p', 'c', 'd', mode=body.get('mode') or None)\n",
        [], id="keyword-or-none",
    ),
    pytest.param(
        "async def f(m):\n"
        "    await m.generate_handoff(db, 'p', 'd', mode='full')\n",
        [], id="explicit-full-constant",
    ),
    pytest.param(
        "async def f(m):\n"
        "    mode = 'full'\n"
        "    return mode\n",
        [], id="full-label-without-handoff-call",
    ),
    pytest.param(
        "def g(mode='full'):\n"
        "    return mode\n",
        [], id="default-full-without-handoff-call",
    ),
])
def test_full_by_default_scan_detects_each_form(tmp_path, source, expected):
    target = tmp_path / "case.py"
    target.write_text(source)
    assert _full_by_default_sites(target) == expected


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


def test_a_real_toml_file_with_the_flag_on_cannot_flip_these_tests(tmp_path, monkeypatch):
    """Pins the _flag_off fixture: load_toml finds meridian.toml through the cwd,
    so an owner who turns the flag on there must still see this file's
    default-is-off tests pass."""
    import tomllib

    toml_file = tmp_path / "meridian.toml"
    toml_file.write_text("[meridian]\ninclude_workspace_context = true\n", encoding="utf-8")
    assert tomllib.loads(toml_file.read_text(encoding="utf-8"))["meridian"][
        "include_workspace_context"
    ] is True
    monkeypatch.chdir(tmp_path)
    assert toml_config.get_include_workspace_context() is False


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


def _decision_line_limit() -> int:
    """Longest a rendered DECISION line may be, derived from the clip limits
    (each clipped field gains at most the one-character ellipsis) plus the
    fixed punctuation around them."""
    return (
        len("  - DECISION [] : ") + 3
        + _deps._WORKSPACE_INDEX_CATEGORY_CHARS
        + _deps._WORKSPACE_INDEX_DECISION_TITLE_CHARS
        + _deps._WORKSPACE_INDEX_DECISION_CHARS
    )


def test_index_block_clips_oversized_decision_title_and_category():
    """REST caps neither field and MCP caps only the title, so the index must
    clip both: they are free text that a record can make arbitrarily long."""
    decision = {"title": "T" * 90_000, "category": "C" * 200_000, "body": "short"}
    block = _deps._render_workspace_index_block([decision], [])
    assert len(block) < 1000
    assert "T" * (_deps._WORKSPACE_INDEX_DECISION_TITLE_CHARS + 1) not in block
    assert "C" * (_deps._WORKSPACE_INDEX_CATEGORY_CHARS + 1) not in block
    # still recognisably that decision, not dropped
    assert "T" * 100 in block and "C" * 30 in block
    # a clipped title/category means the line is not the whole record, so the
    # caller is told where the rest is even though the body itself was short
    assert "get_workspace_decisions()" in block


def test_index_block_flattens_newlines_in_decision_title_and_category():
    block = _deps._render_workspace_index_block(
        [{"title": "line one\nline two", "category": "A\nB", "body": "b"}], [],
    )
    decision_lines = [ln for ln in block.splitlines() if "DECISION" in ln]
    assert decision_lines == ["  - DECISION [A B] line one line two: b"]


def test_index_block_is_bounded_when_every_free_text_field_is_oversized():
    decisions = [
        {
            "title": "T" * 90_000, "category": "C" * 200_000, "body": "d" * 90_000,
        }
        for _ in range(300)
    ]
    notes = [
        {"title": "N" * 90_000, "body": "n" * 90_000, "tags": "policy"}
        for _ in range(300)
    ]
    block = _deps._render_workspace_index_block(decisions, notes)
    assert len(block) < 3000
    shown = [ln for ln in block.splitlines() if "DECISION" in ln and "more" not in ln]
    assert len(shown) == _deps._WORKSPACE_INDEX_MAX_DECISIONS
    assert all(len(ln) <= _decision_line_limit() for ln in shown), [len(ln) for ln in shown]


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


def test_start_session_workspace_context_is_bounded_with_oversized_decision_titles(
    client, monkeypatch,
):
    """Verifier repro: POST /workspace/decisions has no title cap (only the body
    guard), and five 90k-char titles used to make workspace_context 455k chars."""
    for i in range(5):
        r = client.post("/workspace/decisions", json={
            "title": f"{i}" + "T" * 90_000, "body": "short", "category": "STRATEGIC",
        })
        assert r.status_code == 201
    ctx = _start_session_workspace_context(client, "0b0b24d8-ss-long-titles")
    assert 0 < len(ctx) < 3000, len(ctx)
    assert "T" * (_deps._WORKSPACE_INDEX_DECISION_TITLE_CHARS + 1) not in ctx
    assert "get_workspace_decisions()" in ctx
    # not vacuous: the oversized titles really are stored, and the opt-in flag
    # (the documented way to ask for everything) still hands them back whole
    monkeypatch.setenv(_FLAG_ENV, "1")
    full = _start_session_workspace_context(client, "0b0b24d8-ss-long-titles-on")
    assert len(full) > 90_000


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
async def test_get_context_block_is_bounded_with_an_oversized_decision_category(db):
    """Verifier repro: MCP pin_workspace_decision caps the title (500) but not
    the category, and one 200k-char category made the block 200k chars."""
    pid = await _project(db, "0b0b24d8-cb-long-category")
    pinned = await srv._dispatch_mcp_tool(
        "pin_workspace_decision",
        {"title": "Standing policy", "body": "short", "category": "C" * 200_000},
        db, "/tmp",
    )
    assert len(pinned["category"]) == 200_000
    text = await _context_block_text(db, pid)
    assert len(text) < 20_000, len(text)
    assert "C" * (_deps._WORKSPACE_INDEX_CATEGORY_CHARS + 1) not in text
    assert "Standing policy" in text


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


# ---------------------------------------------------------------------------
# 7. The two background delta writers bound "Completed since last handoff"
# ---------------------------------------------------------------------------
#
# Moving session-close auto-save and the idle-expire loop from the implicit
# 'full' to an explicit 'delta' is only half a fix: delta takes the lower bound
# of its "Completed since last handoff" list from the session. Called with no
# session_id the list has no bound, so a project with a long history got its
# OLDEST 20 completed items under that label (+N more), in the very file these
# two paths exist to keep fresh.

_ANCIENT_STAMP = "2020-01-01 00:00:00"
_ANCIENT_COUNT = 25
_RECENT_COUNT = 3


async def _seed_completed_history(db, pid: str) -> list[str]:
    """Completed items from years ago plus a few completed just now.

    Returns the titles of the recent ones. ``force=True`` skips the duplicate
    title guard, since these titles deliberately resemble each other."""
    for i in range(_ANCIENT_COUNT):
        item = await db_module.add_sprint_item(
            db, pid, "v1", f"Ancient chore {i:03d}", force=True,
        )
        await db.execute(
            "UPDATE sprint_items SET status = 'done', completed_at = ? WHERE id = ?",
            (_ANCIENT_STAMP, item["id"]),
        )
    recent = []
    for i in range(_RECENT_COUNT):
        title = f"Fresh deliverable {i}"
        item = await db_module.add_sprint_item(db, pid, "v1", title, force=True)
        await db.execute(
            "UPDATE sprint_items SET status = 'done', completed_at = datetime('now') "
            "WHERE id = ?",
            (item["id"],),
        )
        recent.append(title)
    await db.commit()
    return recent


def _assert_completed_section_is_this_sessions_work(text: str, recent: list[str]) -> None:
    assert "Completed since last handoff:" in text
    for title in recent:
        assert title in text, title
    assert "Ancient chore" not in text
    assert "more completed" not in text


@pytest.mark.asyncio
async def test_idle_expire_loop_scopes_delta_to_the_expired_session(
    db, tmp_path, spy, _no_claude_md_write,
):
    pid = await _project(db, "0b0b24d8-idle-bounded")
    sess = await db_module.register_session(db, pid, "stale")
    recent = await _seed_completed_history(db, pid)
    await db.execute(
        "UPDATE sessions SET last_seen = datetime('now', '-60 minutes') WHERE id = ?",
        (sess["id"],),
    )
    await db.commit()

    result = await srv._expire_and_generate_handoffs(db, str(tmp_path))

    assert result["auto_handoff_generated"] is True
    assert [(c["kwargs_mode"], c["session_id"]) for c in spy.calls] == [("delta", sess["id"])]
    _assert_completed_section_is_this_sessions_work(spy.calls[0]["content"], recent)
    written = (tmp_path / f"{handoff_module.handoff_file_stem(pid)}_handoff.md").read_text(
        encoding="utf-8"
    )
    _assert_completed_section_is_this_sessions_work(written, recent)


@pytest.mark.asyncio
async def test_idle_expire_loop_writes_one_handoff_per_project_for_the_freshest_session(
    db, tmp_path, spy, _no_claude_md_write,
):
    pid = await _project(db, "0b0b24d8-idle-two-sessions")
    older = await db_module.register_session(db, pid, "older")
    fresher = await db_module.register_session(db, pid, "fresher")
    for sid, age in ((older["id"], 120), (fresher["id"], 45)):
        await db.execute(
            "UPDATE sessions SET last_seen = datetime('now', ? || ' minutes') WHERE id = ?",
            (f"-{age}", sid),
        )
    await db.commit()

    await srv._expire_and_generate_handoffs(db, str(tmp_path))

    assert [c["session_id"] for c in spy.calls] == [fresher["id"]]


@pytest.mark.asyncio
async def test_expire_idle_sessions_reports_which_sessions_expired_per_project(db):
    p1 = await db_module.create_project(db, "0b0b24d8-expire-a")
    p2 = await db_module.create_project(db, "0b0b24d8-expire-b")
    a_old = await db_module.register_session(db, p1["id"], "a-old")
    a_new = await db_module.register_session(db, p1["id"], "a-new")
    b_only = await db_module.register_session(db, p2["id"], "b-only")
    fresh = await db_module.register_session(db, p2["id"], "still-alive")
    for sid, age in ((a_old["id"], 200), (a_new["id"], 50), (b_only["id"], 90)):
        await db.execute(
            "UPDATE sessions SET last_seen = datetime('now', ? || ' minutes') WHERE id = ?",
            (f"-{age}", sid),
        )
    await db.commit()

    result = await db_module.expire_idle_sessions(db, max_age_minutes=30)

    assert result["count"] == 3
    assert sorted(result["project_ids"]) == sorted([p1["id"], p2["id"]])
    assert result["session_ids_by_project"] == {
        p1["id"]: [a_new["id"], a_old["id"]],   # most recently seen first
        p2["id"]: [b_only["id"]],               # the live session is not listed
    }
    assert fresh["id"] not in {
        s for ids in result["session_ids_by_project"].values() for s in ids
    }


def test_session_close_auto_save_scopes_delta_to_the_closed_session(
    client, spy, _no_claude_md_write,
):
    project = client.post("/projects", json={"name": "0b0b24d8-close-bounded"}).json()
    pid = project["id"]
    sess = client.post(
        "/sessions/register", json={"project_id": pid, "name": "s1"},
    ).json()

    async def _seed() -> list[str]:
        return await _seed_completed_history(client.app.state.db, pid)

    recent = asyncio.run(_seed())

    assert client.post(f"/sessions/{sess['id']}/close").status_code == 200
    deadline = time.monotonic() + 20
    while not spy.finished.is_set() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert spy.finished.is_set(), "close_session never ran its auto-save handoff"

    assert [(c["kwargs_mode"], c["session_id"]) for c in spy.calls] == [("delta", sess["id"])]
    _assert_completed_section_is_this_sessions_work(spy.calls[0]["content"], recent)
    written = (
        pathlib.Path(client.app.state.data_dir)
        / f"{handoff_module.handoff_file_stem(pid)}_handoff.md"
    ).read_text(encoding="utf-8")
    _assert_completed_section_is_this_sessions_work(written, recent)


# ---------------------------------------------------------------------------
# 8. Tenant scoping of the workspace index
# ---------------------------------------------------------------------------
#
# The index names the number of notes and the titles of policy-tagged ones, so
# an unscoped fetch would show tenant A a count and titles that belong to
# tenant B. Single-tenant tests cannot see that: both tenants' rows have to
# exist in the same database.

_TENANT_A_NOTE = "TENANT-A-NOTE"
_TENANT_B_POLICY_NOTE = "TENANT-B-SECRET-POLICY"
_TENANT_B_DECISION = "TENANT-B-SECRET-DECISION"


async def _seed_two_tenants(db) -> None:
    await db_module.add_workspace_note(
        db, _TENANT_A_NOTE, "a-body", "policy", tenant_id="tenant-a",
    )
    await db_module.add_workspace_note(
        db, _TENANT_B_POLICY_NOTE, "b-body", "policy", tenant_id="tenant-b",
    )
    await db_module.add_workspace_note(
        db, "TENANT-B-PLAIN-NOTE", "b-body-2", "research", tenant_id="tenant-b",
    )
    await db_module.pin_workspace_decision(
        db, "TENANT-A-DECISION", "a-decision", "STRATEGIC", tenant_id="tenant-a",
    )
    await db_module.pin_workspace_decision(
        db, _TENANT_B_DECISION, "b-decision", "STRATEGIC", tenant_id="tenant-b",
    )


def _assert_only_tenant_a(text: str) -> None:
    assert _TENANT_A_NOTE in text and "TENANT-A-DECISION" in text
    assert "TENANT-B" not in text
    assert "b-body" not in text and "b-decision" not in text


@pytest.mark.asyncio
async def test_workspace_context_block_is_scoped_to_the_callers_tenant(db, monkeypatch):
    await _seed_two_tenants(db)

    index = await _deps._build_workspace_context_block(db, tenant_id="tenant-a")
    _assert_only_tenant_a(index)
    # B's two notes must not inflate the count either: A owns exactly one
    assert "1 workspace note(s)" in index

    monkeypatch.setenv(_FLAG_ENV, "1")
    _assert_only_tenant_a(await _deps._build_workspace_context_block(db, tenant_id="tenant-a"))


@pytest.mark.asyncio
async def test_workspace_context_block_still_shows_pre_isolation_rows(db):
    """A row with no tenant is only ever present on a dedicated per-tenant
    database (see _ws_tenant_clause), so every tenant keeps seeing it."""
    await db_module.add_workspace_note(db, "LEGACY-NOTE", "legacy", "policy")
    await _seed_two_tenants(db)

    index = await _deps._build_workspace_context_block(db, tenant_id="tenant-a")

    assert "2 workspace note(s)" in index and "LEGACY-NOTE" in index
    assert "TENANT-B" not in index


@pytest.mark.asyncio
async def test_get_context_block_is_scoped_to_the_callers_tenant(db, monkeypatch):
    """handle_get_context_block gets the tenant from the transport and must hand
    it to the workspace helper: dropping it there would leak B's index into A's
    block even though the helper itself filters correctly."""
    from meridian.mcp.handlers import session_tools

    pid = await _project(db, "0b0b24d8-cb-tenants")
    await _seed_two_tenants(db)

    for flag in (None, "1"):
        if flag:
            monkeypatch.setenv(_FLAG_ENV, flag)
        result = await session_tools.handle_get_context_block(
            {"project_id": pid}, db, "/tmp", None, "tenant-a",
        )
        _assert_only_tenant_a(result["text"])


# ---------------------------------------------------------------------------
# 9. The stdio transport (python -m meridian --mcp, the documented self-host
#    connection) resolves an omitted mode through the same intent logic
# ---------------------------------------------------------------------------


def _stdio_server(monkeypatch, db, tmp_path):
    """The stdio MCP server with its lazy DB pinned to ``db`` and its handoff
    files written under ``tmp_path`` (never the developer's own data dir)."""
    async def _return_db(*_a, **_k):
        return db

    monkeypatch.setattr(db_module, "init_db", _return_db)
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.delenv("MERIDIAN_DB_URL", raising=False)
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    server, _run_stdio = srv.build_mcp_server()
    return server


async def _stdio_generate_handoff(server, arguments: dict) -> dict:
    import json

    import mcp.types as mcp_types

    called = await server.request_handlers[mcp_types.CallToolRequest](
        mcp_types.CallToolRequest(
            params=mcp_types.CallToolRequestParams(
                name="generate_handoff", arguments=arguments,
            )
        )
    )
    return json.loads(called.root.content[0].text)


@pytest.mark.asyncio
async def test_stdio_generate_handoff_omitted_mode_is_bounded_never_full(
    db, monkeypatch, tmp_path,
):
    pid = await _project(db, "0b0b24d8-stdio-omitted")
    await _seed_workspace(db)
    server = _stdio_server(monkeypatch, db, tmp_path)

    result = await _stdio_generate_handoff(server, {"project_id": pid})

    assert _leaks(result["content"]) == []
    assert result["mode"] == "goal"
    assert (await db_module.get_handoffs(db, pid, limit=1))[0]["mode"] == "goal"
    assert _leaks(_written_text(tmp_path)) == []
    for row in await db_module.get_handoffs(db, pid, limit=20):
        assert _leaks(row["body"]) == [], row["mode"]


@pytest.mark.asyncio
async def test_stdio_generate_handoff_omitted_mode_for_a_resumed_session_is_delta(
    db, monkeypatch, tmp_path,
):
    pid = await _project(db, "0b0b24d8-stdio-resumed")
    await _seed_workspace(db)
    sess = await db_module.register_session(db, pid, "stdio-resumed")
    server = _stdio_server(monkeypatch, db, tmp_path)

    first = await _stdio_generate_handoff(
        server, {"project_id": pid, "session_id": sess["id"]},
    )
    second = await _stdio_generate_handoff(
        server, {"project_id": pid, "session_id": sess["id"]},
    )

    assert (first["mode"], second["mode"]) == ("goal", "delta")
    assert _leaks(first["content"]) == [] and _leaks(second["content"]) == []
    assert _leaks(_written_text(tmp_path)) == []


@pytest.mark.asyncio
async def test_stdio_generate_handoff_explicit_full_still_includes_everything(
    db, monkeypatch, tmp_path,
):
    """Proves the absence assertions above are not vacuous over this transport."""
    pid = await _project(db, "0b0b24d8-stdio-full")
    await _seed_workspace(db)
    server = _stdio_server(monkeypatch, db, tmp_path)

    result = await _stdio_generate_handoff(server, {"project_id": pid, "mode": "full"})

    assert result["mode"] == "full"
    assert sorted(_leaks(result["content"])) == sorted(_SENTINELS)
