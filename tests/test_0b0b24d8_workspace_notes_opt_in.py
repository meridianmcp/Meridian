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
import pathlib
import shutil
import subprocess
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
            "window_session_id": kwargs.get("window_session_id"),
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


def _package_python_files(root: pathlib.Path = _ROOT, *, use_git: bool = True):
    """The ``meridian`` package's own source, and nothing else.

    Git-tracked files only (``git ls-files``). A whole-root walk also finds
    every executor worktree under ``.claude/worktrees`` (gitignored, one full
    copy of the repo per session): a stale copy from an older session carries
    the very calls these scans exist to reject, so it failed the suite on an
    otherwise clean checkout. Without git (an sdist, a container with no
    ``.git``) it falls back to the package directory alone, which is where
    every handoff caller lives; tests are excluded by design (they exercise
    the default on purpose)."""
    package = root / "meridian"
    names: "list[str]" = []
    if use_git and shutil.which("git"):
        try:
            listed = subprocess.run(
                ["git", "-C", str(root), "ls-files", "-z", "--", "meridian"],
                capture_output=True, check=True, timeout=60,
            ).stdout.decode("utf-8")
            names = [n for n in listed.split("\0") if n.endswith(".py")]
        except (OSError, subprocess.SubprocessError):
            names = []
    if names:
        # a tracked file deleted in the working tree is listed but unreadable
        return sorted(p for p in (root / n for n in names) if p.is_file())
    skip = {"__pycache__", "node_modules"}
    return sorted(
        p for p in package.rglob("*.py") if not skip.intersection(p.parts)
    )


def _omitting_mode_offenders(root: pathlib.Path = _ROOT, **kw) -> "dict[str, list[int]]":
    return {
        str(p.relative_to(root)): lines
        for p in _package_python_files(root, **kw)
        if (lines := _calls_omitting_mode(p))
    }


def test_no_generate_handoff_call_omits_mode():
    """A call that leaves ``mode`` off silently depends on the default, which
    is exactly how three internal callers once inherited 'full'. The function's
    own ``def`` is a FunctionDef, not a Call, so it needs no allowlist entry."""
    offenders = _omitting_mode_offenders()
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
    """``x or "full"``, ``x or y or "full"``, ``x if c else "full"`` and
    ``body.get("mode", "full")`` (also ``pop``/``setdefault``): an expression
    that turns "not specified" into 'full'. A plain ``"full"`` constant is an
    explicit request and is allowed."""
    if isinstance(expr, ast.BoolOp) and isinstance(expr.op, ast.Or):
        return any(_is_full_const(v) or _falls_back_to_full(v) for v in expr.values)
    if isinstance(expr, ast.IfExp):
        return any(
            _is_full_const(b) or _falls_back_to_full(b) for b in (expr.body, expr.orelse)
        )
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr in {"get", "pop", "setdefault"}
        and len(expr.args) >= 2
    ):
        # the second argument is the value used when the key is absent
        return _is_full_const(expr.args[1]) or _falls_back_to_full(expr.args[1])
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
        # names bound to a fall-back-to-full expression, with the assignment line:
        # ``m = body.get("mode", "full")`` followed by ``generate_handoff(mode=m)``
        fallback_names: "dict[str, int]" = {}
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and _falls_back_to_full(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        fallback_names[target.id] = node.lineno

        def _fallback_site(expr: ast.AST) -> "int | None":
            """Line to report when ``expr`` is, or is a name bound to, a
            fall-back-to-full expression."""
            if _falls_back_to_full(expr):
                return expr.lineno
            if isinstance(expr, ast.Name):
                return fallback_names.get(expr.id)
            return None

        # (2) ``generate_handoff(..., mode=<x or "full">)``, or a name holding it
        for call in calls:
            for kw in call.keywords:
                if kw.arg == "mode" and (site := _fallback_site(kw.value)) is not None:
                    hits.add(site)
            # (4) ``resolve_handoff_mode(<x or "full">, ...)``: its first
            # parameter is the requested mode, positional or by keyword
            if _call_name(call) == "resolve_handoff_mode":
                requested = [kw.value for kw in call.keywords if kw.arg == "requested_mode"]
                requested += call.args[:1]
                for expr in requested:
                    if (site := _fallback_site(expr)) is not None:
                        hits.add(site)
        # (3) ``mode = <x or "full">`` computed before the call, even when the
        # name is not (visibly) the one forwarded
        if "mode" in fallback_names:
            hits.add(fallback_names["mode"])
    return sorted(hits)


def _full_default_offenders(root: pathlib.Path = _ROOT, **kw) -> "dict[str, list[int]]":
    return {
        str(p.relative_to(root)): lines
        for p in _package_python_files(root, **kw)
        if (lines := _full_by_default_sites(p))
    }


def test_no_handoff_caller_defaults_or_falls_back_to_full():
    offenders = _full_default_offenders()
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
    pytest.param(  # the most natural spelling of the same bug
        "async def f(m, body):\n"
        "    await m.generate_handoff(db, 'p', 'd', mode=body.get('mode', 'full'))\n",
        [2], id="keyword-get-default-full",
    ),
    pytest.param(
        "async def f(m, body):\n"
        "    await m.generate_handoff(db, 'p', 'd', mode=body.pop('mode', 'full'))\n",
        [2], id="keyword-pop-default-full",
    ),
    pytest.param(  # two steps, under a name other than ``mode``
        "async def f(m, body):\n"
        "    chosen = body.get('mode', 'full')\n"
        "    await m.generate_handoff(db, 'p', 'd', mode=chosen)\n",
        [2], id="name-bound-to-get-default-full",
    ),
    pytest.param(
        "async def f(m, x):\n"
        "    chosen = 'full' if not x else x\n"
        "    await m.generate_handoff(db, 'p', 'd', mode=chosen)\n",
        [2], id="name-bound-to-ternary-full",
    ),
    pytest.param(
        "async def f(m, arguments):\n"
        "    wanted = arguments.get('mode', 'full')\n"
        "    mode = m.resolve_handoff_mode(wanted, None)\n",
        [2], id="resolve-name-bound-to-get-default-full",
    ),
    pytest.param(
        "async def f(m, body):\n"
        "    await m.generate_handoff(db, 'p', 'd', mode=body.get('mode'))\n",
        [], id="keyword-get-without-default",
    ),
    pytest.param(
        "async def f(m, body):\n"
        "    await m.generate_handoff(db, 'p', 'd', mode=body.get('mode', None))\n",
        [], id="keyword-get-default-none",
    ),
    pytest.param(
        "async def f(m, body):\n"
        "    chosen = body.get('mode', 'goal')\n"
        "    await m.generate_handoff(db, 'p', 'd', mode=chosen)\n",
        [], id="name-bound-to-get-default-goal",
    ),
    pytest.param(  # a fall-back-to-full name that never reaches a handoff call
        "async def f(m, body):\n"
        "    label = body.get('mode', 'full')\n"
        "    await m.generate_handoff(db, 'p', 'd', mode='goal')\n",
        [], id="full-fallback-name-not-forwarded",
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


# The scans read the package, not the checkout. Executor worktrees live in
# .claude/worktrees (gitignored, a full copy of the repo each), and a stale one
# carries the exact calls the scans reject.

_CLEAN_SOURCE = "async def f(db, m):\n    await m.generate_handoff(db, 'p', 'd', mode='goal')\n"
_BARE_CALL = "async def f(db, m):\n    await m.generate_handoff(db, 'p', 'd')\n"
_FULL_FALLBACK = (
    "async def f(db, m, body):\n"
    "    await m.generate_handoff(db, 'p', 'd', mode=body.get('mode') or 'full')\n"
)


def _checkout_with_a_stale_worktree(tmp_path: pathlib.Path, *, git: bool) -> pathlib.Path:
    root = tmp_path / "checkout"
    (root / "meridian").mkdir(parents=True)
    (root / "meridian" / "ok.py").write_text(_CLEAN_SOURCE)
    for old in (
        root / ".claude" / "worktrees" / "older-session" / "meridian" / "routes",
        root / "node_modules" / "somepkg",
    ):
        old.mkdir(parents=True)
        (old / "sessions.py").write_text(_BARE_CALL)
        (old / "handoff.py").write_text(_FULL_FALLBACK)
    if git:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "meridian/ok.py"], cwd=root, check=True)
    return root


@pytest.mark.parametrize("git", [
    pytest.param(
        True, id="git-tracked",
        marks=pytest.mark.skipif(shutil.which("git") is None, reason="git not installed"),
    ),
    pytest.param(False, id="package-directory-fallback"),
])
def test_source_scans_ignore_a_stale_worktree_copy(tmp_path, git):
    root = _checkout_with_a_stale_worktree(tmp_path, git=git)

    assert [p.relative_to(root).as_posix() for p in _package_python_files(
        root, use_git=git,
    )] == ["meridian/ok.py"]
    assert _omitting_mode_offenders(root, use_git=git) == {}
    assert _full_default_offenders(root, use_git=git) == {}


@pytest.mark.parametrize("git", [
    pytest.param(
        True, id="git-tracked",
        marks=pytest.mark.skipif(shutil.which("git") is None, reason="git not installed"),
    ),
    pytest.param(False, id="package-directory-fallback"),
])
def test_source_scans_still_reject_a_real_offender_in_the_package(tmp_path, git):
    """The pruning above must not blind the scans: the same two sources inside
    the package are flagged, so the clean result there is not vacuous."""
    root = _checkout_with_a_stale_worktree(tmp_path, git=git)
    (root / "meridian" / "routes").mkdir()
    (root / "meridian" / "routes" / "sessions.py").write_text(_BARE_CALL)
    (root / "meridian" / "routes" / "handoff.py").write_text(_FULL_FALLBACK)
    if git:
        subprocess.run(["git", "add", "meridian/routes"], cwd=root, check=True)

    def _posix(offenders: "dict[str, list[int]]") -> "dict[str, list[int]]":
        return {k.replace("\\", "/"): v for k, v in offenders.items()}

    assert _posix(_omitting_mode_offenders(root, use_git=git)) == {
        "meridian/routes/sessions.py": [2]
    }
    assert _posix(_full_default_offenders(root, use_git=git)) == {
        "meridian/routes/handoff.py": [2]
    }


def test_source_scans_cover_the_real_package_and_only_it():
    files = _package_python_files()
    package = _ROOT / "meridian"
    assert all(package in p.parents for p in files)
    assert {"handoff.py", "server.py", "sessions.py"} <= {p.name for p in files}
    assert not [p for p in files if ".claude" in p.parts or "tests" in p.parts]


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


def test_index_block_policy_tag_must_equal_policy_not_merely_contain_it():
    block = _deps._render_workspace_index_block([], [
        {"title": "Exactly policy", "body": "b", "tags": "ops, POLICY"},
        {"title": "Only contains it", "body": "b", "tags": "non-policy, policyish, policy-draft"},
    ])
    assert "Exactly policy" in block
    assert "Only contains it" not in block


def test_index_block_says_how_many_policy_notes_are_not_listed():
    cap = _deps._WORKSPACE_INDEX_MAX_POLICY_TITLES
    total = cap + 3
    block = _deps._render_workspace_index_block([], [
        {"title": f"Policy note {i}", "body": "b", "tags": "policy"} for i in range(total)
    ])
    assert sum(f"Policy note {i}" in block for i in range(total)) == cap
    assert "(+3 more)" in block


def test_index_block_caps_are_the_documented_ones():
    """docs/configuration.md states these numbers, and the size bound rests on
    them; a widened cap should be a deliberate edit of both, not a drive-by."""
    assert _deps._WORKSPACE_INDEX_MAX_DECISIONS == 5
    assert _deps._WORKSPACE_INDEX_MAX_POLICY_TITLES == 5
    assert _deps._WORKSPACE_INDEX_DECISION_CHARS == 160
    assert _deps._WORKSPACE_INDEX_DECISION_TITLE_CHARS == 120
    assert _deps._WORKSPACE_INDEX_CATEGORY_CHARS == 40
    assert _deps._WORKSPACE_INDEX_TITLE_CHARS == 80


@pytest.mark.asyncio
async def test_printed_policy_fetch_call_returns_every_note_the_index_lists(db):
    """The index matches policy tags case-insensitively and prints
    get_workspace_notes(tag="policy") as the way to fetch them. Postgres' LIKE
    is case-sensitive (SQLite's is not), so the filter has to fold case itself
    or a note tagged 'Policy' is listed but not returned on the hosted tier.
    case_sensitive_like makes SQLite behave like Postgres for this check."""
    await db_module.add_workspace_note(db, "Mixed case", "body", "ops, Policy")
    await db_module.add_workspace_note(db, "Lower case", "body", "policy")
    await db_module.add_workspace_note(db, "Unrelated", "body", "research")
    index = _deps._render_workspace_index_block(
        [], await db_module.get_workspace_notes(db),
    )
    assert "Mixed case" in index and "Lower case" in index
    assert 'get_workspace_notes(tag="policy")' in index

    await db.execute("PRAGMA case_sensitive_like = ON")
    try:
        for tag in ("policy", "POLICY", "Policy"):
            fetched = await db_module.get_workspace_notes(db, tag=tag)
            assert {n["title"] for n in fetched} == {"Mixed case", "Lower case"}, tag
    finally:
        await db.execute("PRAGMA case_sensitive_like = OFF")


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
    # bounded by the expired session, attributed to none
    assert [
        (c["kwargs_mode"], c["session_id"], c["window_session_id"]) for c in spy.calls
    ] == [("delta", None, sess["id"])]
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

    assert [(c["session_id"], c["window_session_id"]) for c in spy.calls] == [
        (None, fresher["id"])
    ]


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

    assert [
        (c["kwargs_mode"], c["session_id"], c["window_session_id"]) for c in spy.calls
    ] == [("delta", None, sess["id"])]
    _assert_completed_section_is_this_sessions_work(spy.calls[0]["content"], recent)
    written = (
        pathlib.Path(client.app.state.data_dir)
        / f"{handoff_module.handoff_file_stem(pid)}_handoff.md"
    ).read_text(encoding="utf-8")
    _assert_completed_section_is_this_sessions_work(written, recent)


# ---------------------------------------------------------------------------
# 7b. Bounded is not attributed
# ---------------------------------------------------------------------------
#
# f9dc387a bounded the two background writers by passing them a session_id.
# That also made the unattended write the session's "last handoff": its
# handoffs row, its in-memory anchor, its resumed-session marker (an omitted
# mode from it then resolves to delta instead of goal) and its goal-compliance
# record. A session that resumes after an idle expiry (or after a close that
# is later reopened) then got an explicit delta whose "Completed since last
# handoff" list started AT THE AUTO-SAVE and silently dropped everything
# completed before it. The writers now pass window_session_id: the bound
# without the ownership.


def _minutes_ago(db, minutes: int) -> str:
    """SQL for "N minutes ago" as a value for handoffs.created_at.

    handoffs.created_at is TIMESTAMPTZ on Postgres, while the adapter rewrites
    datetime('now', ...) to a formatted TEXT expression, which Postgres refuses to assign
    to it (DatatypeMismatch -- caught by the Postgres CI job, invisible on SQLite). The
    same ``hasattr(db, "_pool")`` switch db.record_handoff uses picks the native form."""
    if hasattr(db, "_pool"):
        return f"now() - interval '{int(minutes)} minutes'"
    return f"datetime('now', '-{int(minutes)} minutes')"


async def _complete(db, pid: str, title: str, offset: str) -> dict:
    """A done item whose completed_at is ``offset`` from now ('-120 minutes')."""
    item = await db_module.add_sprint_item(db, pid, "v1", title, force=True)
    await db.execute(
        "UPDATE sprint_items SET status = 'done', completed_at = datetime('now', ?) "
        "WHERE id = ?",
        (offset, item["id"]),
    )
    await db.commit()
    return item


def _completed_section(content: str) -> str:
    assert "Completed since last handoff:" in content
    return content.split("Completed since last handoff:", 1)[1]


async def _assert_not_attributed_to(db, pid: str, sid: str) -> None:
    """Nothing a background write did may be readable as ``sid``'s own handoff."""
    assert await db_module.get_handoffs(db, pid, limit=20, session_id=sid) == []
    assert sid not in handoff_module._SESSION_HANDOFF_STATE
    assert handoff_module.resolve_handoff_mode(None, sid) == "goal"
    assert await db_module.get_session_goal_compliance(db, sid) is None


async def _explicit_delta_for(db, tmp_path, pid: str, sid: str) -> str:
    _, content, _ = await handoff_module.generate_handoff(
        db, pid, str(tmp_path / "explicit"), skip_ai_summary=True,
        mode="delta", session_id=sid,
    )
    return content


@pytest.mark.asyncio
async def test_idle_expire_auto_save_is_bounded_but_not_attributed_to_the_expired_session(
    db, tmp_path, spy, _no_claude_md_write,
):
    """The verifier's reproduction. S started 3h ago, Alpha was completed 2h
    ago, S idles out and the loop writes its auto-save, S resumes and completes
    Bravo, S asks for a delta. Alpha and Bravo are both S's work."""
    pid = await _project(db, "0b0b24d8-idle-resume")
    sess = await db_module.register_session(db, pid, "resumable")
    sid = sess["id"]
    await db.execute(
        "UPDATE sessions SET created_at = datetime('now', '-180 minutes'), "
        "last_seen = datetime('now', '-60 minutes') WHERE id = ?",
        (sid,),
    )
    await db.commit()
    await _complete(db, pid, "Alpha before the idle expiry", "-120 minutes")

    result = await srv._expire_and_generate_handoffs(db, str(tmp_path))

    assert result["auto_handoff_generated"] is True
    auto_save = spy.calls[0]
    assert auto_save["kwargs_mode"] == "delta"
    assert auto_save["session_id"] is None
    assert auto_save["window_session_id"] == sid
    # the unattended write is still bounded to the session's window
    assert "Alpha before the idle expiry" in _completed_section(auto_save["content"])
    await _assert_not_attributed_to(db, pid, sid)

    await _complete(db, pid, "Bravo after the resume", "+1 minutes")
    section = _completed_section(await _explicit_delta_for(db, tmp_path, pid, sid))

    assert "Alpha before the idle expiry" in section
    assert "Bravo after the resume" in section


def test_session_close_auto_save_is_bounded_but_not_attributed_to_the_closed_session(
    client, spy, _no_claude_md_write, tmp_path,
):
    """Same loss through the other writer: a closed session can be reopened
    (PATCH /sessions/{id} status=active), and its next explicit delta must
    still list what it completed before the close."""
    pid = client.post("/projects", json={"name": "0b0b24d8-close-resume"}).json()["id"]
    sid = client.post(
        "/sessions/register", json={"project_id": pid, "name": "reopened"},
    ).json()["id"]
    db = client.app.state.db

    async def _seed() -> None:
        await db.execute(
            "UPDATE sessions SET created_at = datetime('now', '-180 minutes') "
            "WHERE id = ?",
            (sid,),
        )
        await db.commit()
        await _complete(db, pid, "Alpha before the close", "-120 minutes")

    asyncio.run(_seed())

    assert client.post(f"/sessions/{sid}/close").status_code == 200
    deadline = time.monotonic() + 20
    while not spy.finished.is_set() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert spy.finished.is_set(), "close_session never ran its auto-save handoff"

    assert len(spy.calls) == 1
    auto_save = spy.calls[0]
    assert auto_save["kwargs_mode"] == "delta"
    assert auto_save["session_id"] is None
    assert auto_save["window_session_id"] == sid
    assert "Alpha before the close" in _completed_section(auto_save["content"])

    async def _after_reopen() -> str:
        await _assert_not_attributed_to(db, pid, sid)
        await _complete(db, pid, "Bravo after the reopen", "+1 minutes")
        return await _explicit_delta_for(db, tmp_path, pid, sid)

    assert client.patch(f"/sessions/{sid}", json={"status": "active"}).status_code == 200
    section = _completed_section(asyncio.run(_after_reopen()))

    assert "Alpha before the close" in section
    assert "Bravo after the reopen" in section


@pytest.mark.asyncio
async def test_window_session_id_bounds_by_the_sessions_last_handoff_not_just_its_start(
    db, tmp_path,
):
    pid = await _project(db, "0b0b24d8-window-last-handoff")
    sid = (await db_module.register_session(db, pid, "windowed"))["id"]
    await db.execute(
        "UPDATE sessions SET created_at = datetime('now', '-180 minutes') WHERE id = ?",
        (sid,),
    )
    row = await db_module.record_handoff(db, pid, "delta", "an earlier handoff", sid)
    await db.execute(
        f"UPDATE handoffs SET created_at = {_minutes_ago(db, 30)} WHERE id = ?",
        (row["id"],),
    )
    await db.commit()
    await _complete(db, pid, "Done before that handoff", "-90 minutes")
    await _complete(db, pid, "Done after that handoff", "-10 minutes")

    _, content, _ = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="delta",
        window_session_id=sid,
    )

    section = _completed_section(content)
    assert "Done after that handoff" in section
    assert "Done before that handoff" not in section


@pytest.mark.asyncio
async def test_window_session_id_writes_an_unowned_row_and_session_id_still_attributes(
    db, tmp_path,
):
    pid = await _project(db, "0b0b24d8-window-vs-owner")
    sid = (await db_module.register_session(db, pid, "owner"))["id"]

    await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="delta",
        window_session_id=sid,
    )
    rows = await db_module.get_handoffs(db, pid, limit=5)
    assert [(r["mode"], r["session_id"]) for r in rows] == [("delta", None)]
    await _assert_not_attributed_to(db, pid, sid)

    # control: the same call with session_id is the session's own handoff. A
    # start_session consumes the pending goal first; otherwise the unconsumed
    # row above would be amended in place (edd9c54b) and keep its NULL owner.
    await db_module.pop_pending_goal(db, pid)
    await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="delta", session_id=sid,
    )
    assert len(await db_module.get_handoffs(db, pid, limit=5, session_id=sid)) == 1
    assert sid in handoff_module._SESSION_HANDOFF_STATE
    assert handoff_module.resolve_handoff_mode(None, sid) == "delta"


@pytest.mark.asyncio
async def test_window_session_id_scopes_the_span_footer_to_that_session(db, tmp_path):
    """Delta's span footer is per-session (7732e096); a background write that
    names a window session keeps that scope instead of reporting the whole
    project's history."""
    pid = await _project(db, "0b0b24d8-window-span")
    old = await db_module.register_session(db, pid, "old")
    new = await db_module.register_session(db, pid, "new")
    await db_module.log_task(db, old["id"], pid, "ancient work", status="done")
    await db_module.log_task(db, new["id"], pid, "recent work", status="done")
    await db.execute(
        "UPDATE sessions SET created_at = '2020-01-01 00:00:00', "
        "last_seen = '2020-01-01 00:00:00' WHERE id = ?",
        (old["id"],),
    )
    await db.execute(
        "UPDATE task_log SET created_at = '2020-01-01 00:00:00' WHERE session_id = ?",
        (old["id"],),
    )
    await db.commit()

    async def _first_activity(**kw) -> str:
        _, content, _ = await handoff_module.generate_handoff(
            db, pid, str(tmp_path), skip_ai_summary=True, mode="delta", **kw,
        )
        span = content.split("## Session span", 1)[1]
        return [ln for ln in span.splitlines() if ln.startswith("- First activity:")][0]

    assert "2020-01-01" in await _first_activity()  # control: project-wide
    assert "2020-01-01" not in await _first_activity(window_session_id=new["id"])


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


@pytest.mark.asyncio
async def test_get_context_block_tool_call_is_scoped_by_the_dispatchers_tenant(
    db, monkeypatch, tmp_path,
):
    """The same guarantee through the real dispatcher, which is what derives
    the tenant id from the authenticated caller's tenant record. A handler test
    that passes the id positionally cannot see that link break."""
    pid = await _project(db, "0b0b24d8-cb-dispatch-tenants")
    await _seed_two_tenants(db)

    for flag in (None, "1"):
        if flag:
            monkeypatch.setenv(_FLAG_ENV, flag)
        scoped = await srv._dispatch_mcp_tool(
            "get_context_block", {"project_id": pid}, db, str(tmp_path),
            tenant={"id": "tenant-a"},
        )
        _assert_only_tenant_a(scoped["text"])
        # non-vacuity: with no tenant (self-host) both tenants' rows are listed
        unscoped = await srv._dispatch_mcp_tool(
            "get_context_block", {"project_id": pid}, db, str(tmp_path),
        )
        assert _TENANT_B_POLICY_NOTE in unscoped["text"]
        assert _TENANT_B_DECISION in unscoped["text"]


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


# ---------------------------------------------------------------------------
# 10. Final pass: the retrospective, the tool/dashboard text, the unattended amend
# ---------------------------------------------------------------------------
#
# (1) Moving the two background writers from the old 'full' default to 'delta'
#     also stopped the automatic Sprint Retrospective note (aef94e4a): delta
#     disables every Haiku seam (4c7cd788), and the retrospective step sat
#     behind that switch. They now pass refresh_retrospective=True.
# (2) Tool descriptions and the dashboard hint still said workspace notes and
#     decisions are "injected at the top of every project's context block +
#     handoff", which stopped being true when bodies became opt-in.
# (3) An unattended write amended the latest handoffs row in place even when a
#     session owned it, moving that session's "last handoff" anchor.


@pytest.fixture
def _no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


async def _retro_notes(db, pid: str) -> list[dict]:
    return await db_module.get_project_notes(db, pid, tag="retrospective", bodies=True)


async def _project_with_one_done_item(db, name: str) -> str:
    pid = await _project(db, name)
    await _complete(db, pid, "Shipped the retro subject", "-10 minutes")
    return pid


@pytest.mark.asyncio
async def test_idle_expire_loop_still_refreshes_the_retrospective_without_an_api_key(
    db, tmp_path, spy, _no_claude_md_write, _no_api_key,
):
    pid = await _project_with_one_done_item(db, "0b0b24d8-retro-idle")
    await _seed_workspace(db)
    sess = await db_module.register_session(db, pid, "stale")
    await db.execute(
        "UPDATE sessions SET last_seen = datetime('now', '-60 minutes') WHERE id = ?",
        (sess["id"],),
    )
    await db.commit()
    assert await _retro_notes(db, pid) == []

    result = await srv._expire_and_generate_handoffs(db, str(tmp_path))

    assert result["auto_handoff_generated"] is True
    assert [c["kwargs_mode"] for c in spy.calls] == ["delta"]
    notes = await _retro_notes(db, pid)
    assert len(notes) == 1
    assert "Shipped the retro subject" in notes[0]["body"]
    # the workspace notes did not come back with it
    assert _leaks(_written_text(tmp_path)) == []
    assert _leaks(spy.calls[0]["content"]) == []
    assert _leaks(notes[0]["body"] + notes[0]["title"]) == []


def test_session_close_auto_save_still_refreshes_the_retrospective_without_an_api_key(
    client, spy, _no_claude_md_write, _no_api_key,
):
    pid = client.post("/projects", json={"name": "0b0b24d8-retro-close"}).json()["id"]
    sid = client.post(
        "/sessions/register", json={"project_id": pid, "name": "closer"},
    ).json()["id"]
    db = client.app.state.db

    async def _seed() -> None:
        await _complete(db, pid, "Shipped the retro subject", "-10 minutes")
        await _seed_workspace(db)

    asyncio.run(_seed())

    assert client.post(f"/sessions/{sid}/close").status_code == 200
    deadline = time.monotonic() + 20
    while not spy.finished.is_set() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert spy.finished.is_set(), "close_session never ran its auto-save handoff"

    assert [c["kwargs_mode"] for c in spy.calls] == ["delta"]
    notes = asyncio.run(_retro_notes(db, pid))
    assert len(notes) == 1
    assert "Shipped the retro subject" in notes[0]["body"]
    written = (
        pathlib.Path(client.app.state.data_dir)
        / f"{handoff_module.handoff_file_stem(pid)}_handoff.md"
    ).read_text(encoding="utf-8")
    assert _leaks(written) == []
    assert _leaks(spy.calls[0]["content"]) == []
    assert _leaks(notes[0]["body"] + notes[0]["title"]) == []


@pytest.mark.asyncio
async def test_an_explicit_delta_still_skips_the_retrospective(db, tmp_path, _no_api_key):
    """4c7cd788 stays true for everything that did not ask for the step: an
    explicit delta (and checkpoint(), which is one) is a lightweight update."""
    pid = await _project_with_one_done_item(db, "0b0b24d8-retro-explicit-delta")
    sid = (await db_module.register_session(db, pid, "explicit"))["id"]

    await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="delta", session_id=sid,
    )
    await handoff_module.generate_handoff(
        db, pid, str(tmp_path), mode="delta", window_session_id=sid,
    )

    assert await _retro_notes(db, pid) == []


@pytest.mark.asyncio
async def test_refresh_retrospective_with_skip_ai_summary_makes_no_network_call(
    db, tmp_path, monkeypatch,
):
    pid = await _project_with_one_done_item(db, "0b0b24d8-retro-no-network")
    # a key IS configured: skip_ai_summary alone must keep the step offline
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")

    async def _must_not_be_called(*_a, **_k):
        raise AssertionError("the AI retrospective seam ran despite skip_ai_summary")

    monkeypatch.setattr(
        handoff_module, "_generate_sprint_retrospective", _must_not_be_called
    )

    await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="delta",
        refresh_retrospective=True,
    )

    notes = await _retro_notes(db, pid)
    assert len(notes) == 1
    assert "Shipped the retro subject" in notes[0]["body"]


@pytest.mark.asyncio
async def test_refresh_retrospective_without_skip_uses_the_retrospective_generator(
    db, tmp_path, monkeypatch,
):
    """What the session-close auto-save does when a key is configured: the same
    generator the old 'full' default used (Haiku in production, stubbed here)."""
    pid = await _project_with_one_done_item(db, "0b0b24d8-retro-generator")
    seen: list[int] = []

    async def _stub(completed, _decisions, _sprint, summarizer=None):
        seen.append(len(completed))
        return "STUB RETROSPECTIVE BODY"

    monkeypatch.setattr(handoff_module, "_generate_sprint_retrospective", _stub)

    await handoff_module.generate_handoff(
        db, pid, str(tmp_path), mode="delta", refresh_retrospective=True,
    )

    assert seen == [1]
    notes = await _retro_notes(db, pid)
    assert [n["body"] for n in notes] == ["STUB RETROSPECTIVE BODY"]


@pytest.mark.asyncio
async def test_refresh_retrospective_never_runs_the_other_delta_seams(
    db, tmp_path, monkeypatch, _no_api_key,
):
    pid = await _project_with_one_done_item(db, "0b0b24d8-retro-only-that-step")
    ran: list[str] = []

    async def _no_summary(*_a, **_k):
        ran.append("summarize_session")
        return None

    async def _no_ai_summary(*_a, **_k):
        ran.append("ai_summary")
        return ""

    monkeypatch.setattr(db_module, "summarize_session", _no_summary)
    monkeypatch.setattr(handoff_module, "_generate_ai_summary", _no_ai_summary)
    await db_module.register_session(db, pid, "someone")

    await handoff_module.generate_handoff(
        db, pid, str(tmp_path), mode="delta", refresh_retrospective=True,
    )

    assert ran == []
    assert len(await _retro_notes(db, pid)) == 1


# -- (2) stale text ---------------------------------------------------------

_STALE_PHRASES = (
    "injected at the top of every project",
    "context block + handoff",
)
_WORKSPACE_TOOLS = ("add_workspace_note", "pin_workspace_decision")


def _workspace_tool_texts_http() -> "dict[str, str]":
    from meridian.mcp_tools import _MCP_TOOLS_LIST

    return {
        t["name"]: t["description"]
        for t in _MCP_TOOLS_LIST
        if t["name"] in _WORKSPACE_TOOLS
    }


async def _workspace_tool_texts_stdio(db, monkeypatch, tmp_path) -> "dict[str, str]":
    import mcp.types as mcp_types

    server = _stdio_server(monkeypatch, db, tmp_path)
    listed = await server.request_handlers[mcp_types.ListToolsRequest](
        mcp_types.ListToolsRequest()
    )
    return {
        t.name: t.description
        for t in listed.root.tools
        if t.name in _WORKSPACE_TOOLS
    }


def _assert_truthful_workspace_text(texts: "dict[str, str]") -> None:
    assert set(texts) == set(_WORKSPACE_TOOLS)
    for name, text in texts.items():
        low = text.lower()
        for phrase in _STALE_PHRASES:
            assert phrase not in low, (name, phrase)
        assert "index" in low, name
        assert "include_workspace_context" in text, name
        # the explicit full handoff is the one place a handoff carries them
        assert 'mode="full"' in text, name
    assert "get_workspace_notes" in texts["add_workspace_note"]
    assert "get_workspace_decisions" in texts["pin_workspace_decision"]


def test_http_workspace_tool_descriptions_are_truthful_about_inlining():
    _assert_truthful_workspace_text(_workspace_tool_texts_http())


@pytest.mark.asyncio
async def test_stdio_workspace_tool_descriptions_are_truthful_about_inlining(
    db, monkeypatch, tmp_path,
):
    _assert_truthful_workspace_text(
        await _workspace_tool_texts_stdio(db, monkeypatch, tmp_path)
    )


def test_dashboard_workspace_hint_is_truthful_about_inlining():
    src = (_ROOT / "meridian" / "static" / "dashboard-settings.ts").read_text(
        encoding="utf-8"
    )
    low = src.lower()
    for phrase in _STALE_PHRASES:
        assert phrase not in low, phrase
    assert "get_workspace_notes" in src and "get_workspace_decisions" in src


# -- (3) the unattended amend ----------------------------------------------


async def _session_with_an_unconsumed_goal_handoff(db, tmp_path, pid: str) -> str:
    """S started 5h ago and owns a goal handoff stamped 200 minutes ago whose
    pending_goal nobody has consumed (the state the amend path acts on)."""
    sid = (await db_module.register_session(db, pid, "owner-of-a-goal"))["id"]
    await handoff_module.generate_handoff(
        db, pid, str(tmp_path / "own"), skip_ai_summary=True, mode="goal",
        session_id=sid,
    )
    await db.execute(
        "UPDATE sessions SET created_at = datetime('now', '-300 minutes') WHERE id = ?",
        (sid,),
    )
    await db.execute(
        f"UPDATE handoffs SET created_at = {_minutes_ago(db, 200)} "
        "WHERE session_id = ?",
        (sid,),
    )
    await db.commit()
    assert await db_module.get_pending_goal(db, pid) is not None
    return sid


@pytest.mark.asyncio
async def test_unattended_delta_never_amends_a_row_a_session_owns(db, tmp_path):
    """The verifier's probe: S owns a goal handoff at -200m, Alpha completes at
    -100m, a background delta (window_session_id only) runs, Bravo completes, S
    asks for a delta. Alpha used to be lost: the background write amended S's
    row in place and moved its created_at, S's anchor, to the auto-save."""
    pid = await _project(db, "0b0b24d8-amend-probe")
    sid = await _session_with_an_unconsumed_goal_handoff(db, tmp_path, pid)
    own_before = await db_module.get_handoffs(db, pid, limit=5, session_id=sid)
    assert len(own_before) == 1
    await _complete(db, pid, "Alpha before the background write", "-100 minutes")

    _, auto_content, amended = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="delta",
        window_session_id=sid,
    )

    assert amended is False
    assert "Alpha before the background write" in _completed_section(auto_content)
    own_after = await db_module.get_handoffs(db, pid, limit=5, session_id=sid)
    # S's own row is untouched: same body, same anchor
    assert [(r["id"], r["body"], r["created_at"]) for r in own_after] == [
        (r["id"], r["body"], r["created_at"]) for r in own_before
    ]
    rows = await db_module.get_handoffs(db, pid, limit=5)
    assert len(rows) == 2
    assert [r["session_id"] for r in rows if r["id"] != own_before[0]["id"]] == [None]

    await _complete(db, pid, "Bravo after the background write", "+1 minutes")
    section = _completed_section(await _explicit_delta_for(db, tmp_path, pid, sid))

    assert "Alpha before the background write" in section
    assert "Bravo after the background write" in section


@pytest.mark.asyncio
async def test_idle_expire_loop_does_not_move_the_expired_sessions_anchor(
    db, tmp_path, _no_claude_md_write,
):
    """The same probe through the real writer (the idle-expire loop)."""
    pid = await _project(db, "0b0b24d8-amend-idle")
    sid = await _session_with_an_unconsumed_goal_handoff(db, tmp_path, pid)
    await db.execute(
        "UPDATE sessions SET last_seen = datetime('now', '-60 minutes') WHERE id = ?",
        (sid,),
    )
    await db.commit()
    await _complete(db, pid, "Alpha before the idle expiry", "-100 minutes")

    result = await srv._expire_and_generate_handoffs(db, str(tmp_path / "loop"))

    assert result["auto_handoff_generated"] is True
    await _complete(db, pid, "Bravo after the resume", "+1 minutes")
    section = _completed_section(await _explicit_delta_for(db, tmp_path, pid, sid))

    assert "Alpha before the idle expiry" in section
    assert "Bravo after the resume" in section


def test_session_close_auto_save_does_not_move_the_closed_sessions_anchor(
    client, _no_claude_md_write, tmp_path,
):
    """The same probe through the other writer (session close). No spy here: the
    setup itself calls generate_handoff, so the wait is on the new handoffs row."""
    pid = client.post("/projects", json={"name": "0b0b24d8-amend-close"}).json()["id"]
    db = client.app.state.db

    async def _setup() -> str:
        sid = await _session_with_an_unconsumed_goal_handoff(db, tmp_path, pid)
        await _complete(db, pid, "Alpha before the close", "-100 minutes")
        return sid

    sid = asyncio.run(_setup())

    assert client.post(f"/sessions/{sid}/close").status_code == 200
    deadline = time.monotonic() + 20
    rows: list[dict] = []
    while time.monotonic() < deadline:
        rows = asyncio.run(db_module.get_handoffs(db, pid, limit=5))
        if len(rows) >= 2:
            break
        time.sleep(0.05)
    # the auto-save wrote a SEPARATE unowned row instead of amending S's own
    assert sorted(str(r["session_id"]) for r in rows) == sorted([sid, "None"]), rows

    async def _after() -> str:
        await _complete(db, pid, "Bravo after the reopen", "+1 minutes")
        return await _explicit_delta_for(db, tmp_path, pid, sid)

    section = _completed_section(asyncio.run(_after()))

    assert "Alpha before the close" in section
    assert "Bravo after the reopen" in section


@pytest.mark.asyncio
async def test_unattended_writes_still_amend_an_unowned_row_in_place(db, tmp_path):
    """The amend itself is kept: two background writes are one row, as before."""
    pid = await _project(db, "0b0b24d8-amend-unowned")
    sid = (await db_module.register_session(db, pid, "bystander"))["id"]

    _, _, first = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="delta",
        window_session_id=sid,
    )
    _, _, second = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="delta",
        window_session_id=sid,
    )

    assert (first, second) == (False, True)
    assert len(await db_module.get_handoffs(db, pid, limit=5)) == 1


@pytest.mark.asyncio
async def test_a_session_still_amends_its_own_unconsumed_row(db, tmp_path):
    pid = await _project(db, "0b0b24d8-amend-own")
    sid = (await db_module.register_session(db, pid, "self"))["id"]

    await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="goal", session_id=sid,
    )
    _, _, amended = await handoff_module.generate_handoff(
        db, pid, str(tmp_path), skip_ai_summary=True, mode="delta", session_id=sid,
    )

    assert amended is True
    rows = await db_module.get_handoffs(db, pid, limit=5)
    assert [(r["mode"], r["session_id"]) for r in rows] == [("delta", sid)]
