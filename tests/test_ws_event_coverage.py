"""8a665a03 -- the live-view rule, enforced from source.

    Every mutation publishes exactly one event, and every list view subscribes.

The permanently-deleted Backburner row that stayed on screen until a reload broke BOTH
halves at once: the DELETE route published nothing, and the client had no branch that
would have repainted the Queue tab even if it had. Eight sibling gaps of the same shape
turned up in the audit. These tests make the rule structural so the next one cannot slip
in unnoticed:

1. Every WebSocket event type the server publishes (``_publish_project_event``,
   ``_publish_task``, ``publish_global`` anywhere under ``meridian/``) has a branch in
   ``handleWsEvent`` in ``meridian/static/dashboard.ts`` -- or is on
   ``UNSUBSCRIBED_EVENTS`` below with the reason nothing on screen shows it. And the
   reverse: a branch for an event nothing publishes is a typo or dead code.
2. Every mutating route under ``meridian/routes/`` that the dashboard calls, and that
   changes project-scoped data a WebSocket-fed view lists, publishes an event on its
   path -- or is on ``SILENT_ROUTES`` below with the reason that is acceptable.

Both are static scans (AST for the server, regex for the TypeScript), deliberately cheap
and deliberately conservative: a scan can miss a case, but adding a publish call, a
client branch or an allowlist entry is always enough to satisfy it. Allowlist entries are
checked for staleness in both directions so the lists cannot rot into noise.

Limits of scan 2 -- it is a guard, not a proof:

* only routes under ``meridian/routes/`` are scanned; the few ``@app.*`` routes defined
  directly in ``meridian/server.py`` (start-session, hitl-requests, ...) are not;
* a dashboard URL built from a variable segment (``/sprint-items/${id}/${action}``) is
  invisible to the caller detection, so skip / fail are only covered through their sibling
  routes and the publish helpers they share (``_transition_status``);
* it proves the route's path publishes *an* event, not that the right VIEW listens for it.
  That half is scan 1 here plus the per-event routing tests in
  ``meridian/static/live-refresh.test.ts``.
"""
from __future__ import annotations

import ast
import functools
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "meridian"
ROUTES = PKG / "routes"
DB = PKG / "db"
STATIC = PKG / "static"

PUBLISH_CALLS = {"_publish_project_event", "_publish_task", "publish_global"}


# ---------------------------------------------------------------------------
# 1. published event types vs handleWsEvent
# ---------------------------------------------------------------------------

# Event types the server publishes that no dashboard view renders. Each needs a reason a
# reviewer can check; "nobody got round to it" is not one -- add the client branch.
UNSUBSCRIBED_EVENTS: dict[str, str] = {
    "resource_released": (
        "published by release_symbol so a session WAITING on a symbol claim can wake up; "
        "no dashboard list shows symbol claims, so there is nothing to repaint"
    ),
    "proposal_evidence_linked": (
        "published when evidence is linked to a control-plane proposal; the Proposals "
        "panel (dashboard-settings.ts cp-panel-proposals) is a lazy, open-on-demand "
        "panel that fetches when opened, not a live list"
    ),
}


def _event_type_from_call(call: ast.Call) -> tuple[str | None, bool]:
    """(event type, is_dynamic) for one publish call; (None, False) if not a publish."""
    fn = call.func
    name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
    if name not in PUBLISH_CALLS:
        return None, False
    if name == "_publish_project_event":
        arg = call.args[1] if len(call.args) > 1 else None
    elif name == "_publish_task":
        arg = call.args[0] if call.args else None
    else:  # publish_global({"type": "...", ...})
        arg = None
        if call.args and isinstance(call.args[0], ast.Dict):
            for k, v in zip(call.args[0].keys, call.args[0].values):
                if isinstance(k, ast.Constant) and k.value == "type":
                    arg = v
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value, False
    return None, True


@functools.lru_cache(maxsize=1)
def _published_event_types() -> tuple[dict[str, list[str]], list[str]]:
    """({event type: [files publishing it]}, [dynamic publish sites])."""
    found: dict[str, list[str]] = {}
    dynamic: list[str] = []
    for path in sorted(PKG.rglob("*.py")):
        if "static" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        # Cheap pre-filter: most of the package's ~13k-line modules never publish, and
        # parsing them all dominated this test's runtime.
        if not any(name in text for name in PUBLISH_CALLS):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:  # pragma: no cover - a broken file fails its own tests
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                etype, is_dynamic = _event_type_from_call(node)
                rel = path.relative_to(REPO).as_posix()
                if is_dynamic:
                    dynamic.append(f"{rel}:{node.lineno}")
                elif etype:
                    found.setdefault(etype, []).append(rel)
    return found, dynamic


def _handle_ws_event_source() -> str:
    src = (STATIC / "dashboard.ts").read_text(encoding="utf-8").replace("\r", "")
    m = re.search(r"^function handleWsEvent\(.*?^\}", src, re.S | re.M)
    assert m, "handleWsEvent not found in meridian/static/dashboard.ts"
    return m.group(0)


def _client_handled_types() -> set[str]:
    return set(re.findall(r"event\.type === '([A-Za-z_]+)'", _handle_ws_event_source()))


def test_published_event_types_are_all_statically_known():
    """A publish call whose event type is computed at runtime cannot be verified."""
    _, dynamic = _published_event_types()
    assert dynamic == [], (
        "publish call(s) with a non-literal event type -- pass the type as a string literal "
        f"so tests/test_ws_event_coverage.py can check it has a client handler: {dynamic}"
    )


def test_every_published_event_type_has_a_client_handler():
    published, _ = _published_event_types()
    handled = _client_handled_types()
    unhandled = sorted(t for t in published if t not in handled and t not in UNSUBSCRIBED_EVENTS)
    assert not unhandled, (
        "The server publishes these WebSocket event types but handleWsEvent in "
        "meridian/static/dashboard.ts has no branch for them, so every view showing that "
        "data needs a reload to catch up: "
        + ", ".join(f"{t} (from {', '.join(sorted(set(published[t])))})" for t in unhandled)
        + ".  Add a branch that repaints the view(s) -- or, if nothing on screen shows this "
        "data, add the type to UNSUBSCRIBED_EVENTS in this file with the reason."
    )


def test_unsubscribed_allowlist_has_no_stale_entries():
    published, _ = _published_event_types()
    handled = _client_handled_types()
    for etype in UNSUBSCRIBED_EVENTS:
        assert etype in published, f"UNSUBSCRIBED_EVENTS lists {etype!r} but nothing publishes it any more -- remove it"
        assert etype not in handled, f"{etype!r} now has a client handler -- remove it from UNSUBSCRIBED_EVENTS"


def test_every_client_event_branch_matches_a_published_type():
    """A branch for an event nobody publishes is a typo (e.g. sprint_item_delete) or dead code."""
    published, _ = _published_event_types()
    orphans = sorted(_client_handled_types() - set(published))
    assert not orphans, (
        f"handleWsEvent has branches for event types no server code publishes: {orphans}"
    )


def test_client_handles_every_event_the_delete_and_edit_paths_publish():
    """The specific types this change introduced, pinned by name so a rename shows up here."""
    published, _ = _published_event_types()
    handled = _client_handled_types()
    for etype in (
        "sprint_item_deleted", "sprint_item_updated", "sprint_item_added", "sprint_items_fanned_out",
        "note_updated", "note_deleted", "decision_updated", "decision_deleted", "insight_added",
        "task_deleted", "session_updated", "session_started", "project_icon_changed",
        "project_parent_changed",
    ):
        assert etype in published, f"{etype} is no longer published"
        assert etype in handled, f"{etype} has no handleWsEvent branch"


# ---------------------------------------------------------------------------
# 2. dashboard-driven mutating routes publish
# ---------------------------------------------------------------------------

# Only these paths carry project data a WebSocket-fed list view shows (the dashboard
# subscribes to /ws/{project_id}). Everything else -- billing, tunnel plugin config, the
# workspace-level notes/decisions/sprint screens, auth, blog, github connection, MCP proxy
# endpoints -- is a per-user / per-workspace screen the acting tab repaints itself and no
# project event stream feeds, so it is out of scope for the "publishes" rule by design.
def _in_scope(path: str) -> bool:
    return (
        path.startswith("/projects/{project_id}/")
        or path == "/tasks"
        or path.startswith("/tasks/")
        or path.startswith("/sessions")
        or path.startswith("/hitl")
    )


# (METHOD, path) of in-scope routes the dashboard calls that publish nothing, with why
# that is acceptable. Reasons are specific on purpose: a route can only stay here while
# the reason is still true.
SILENT_ROUTES: dict[tuple[str, str], str] = {
    ("PATCH", "/hitl/{request_id}"): (
        "dismiss / answer a HITL request; the dashboard re-polls GET /hitl every 10 s "
        "(initHitlPanel) and the acting tab calls refreshHitl() itself, so another tab "
        "catches up within 10 s"
    ),
    ("PATCH", "/projects/{project_id}/settings"): (
        "per-project settings form (max pinned decisions, executor config); no list view "
        "renders it, the Settings tab reloads it when opened"
    ),
    ("PATCH", "/projects/{project_id}/ntfy"): (
        "notification target form on the Account card; only that form shows it and the "
        "acting tab keeps its own cache in step (_syncAccountCacheAfterSave)"
    ),
    ("POST", "/projects/{project_id}/notify/test"): "sends a test notification; changes no stored data",
    ("PATCH", "/projects/{project_id}/agent-instructions"): (
        "agent-instructions editor in Settings; single-document form, reloaded when opened"
    ),
    ("POST", "/projects/{project_id}/rewind-token"): (
        "mints/returns a share token for the Rewind tab's link; no list view shows tokens"
    ),
    ("POST", "/projects/{project_id}/codebase-map"): (
        "regenerates a derived artifact on demand and returns it to the caller; not a list view"
    ),
    ("POST", "/projects/{project_id}/decisions/consolidate"): (
        "asks an LLM to PROPOSE a consolidation and returns the preview; nothing is written "
        "until the user applies it through decisions-pinned/replace-all, which publishes"
    ),
    ("PUT", "/projects/{project_id}/files/{filename}"): (
        "Files tab editor saving one project file; the editing tab owns the buffer and "
        "repaints it, there is no WebSocket-fed file list"
    ),
    ("POST", "/projects/{project_id}/github/connect"): "GitHub connection form in Settings; reloaded via loadSettingsTab",
    ("POST", "/projects/{project_id}/github/push-mcp-template"): "one-shot repo write; changes no dashboard-listed data",
    ("DELETE", "/projects/{project_id}/github/disconnect"): "GitHub connection form in Settings; reloaded via loadSettingsTab",
    ("PATCH", "/projects/{project_id}/github/account"): "GitHub account picker in Settings; reloaded via loadSettingsTab(force)",
}


@functools.lru_cache(maxsize=1)
def _mutating_routes() -> list[tuple[str, str, str, ast.AST]]:
    """(METHOD, path, file, handler node) for every router.post/put/patch/delete route."""
    out = []
    for path in sorted(ROUTES.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if (
                    isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr in {"post", "put", "patch", "delete"}
                    and isinstance(dec.func.value, ast.Name)
                    and dec.func.value.id == "router"
                    and dec.args
                    and isinstance(dec.args[0], ast.Constant)
                ):
                    out.append((dec.func.attr.upper(), dec.args[0].value, path.name, node))
    return out


@functools.lru_cache(maxsize=1)
def _function_index() -> dict[str, list[ast.AST]]:
    """name -> function nodes, for the modules a route handler reaches its writes through."""
    index: dict[str, list[ast.AST]] = {}
    files = sorted(DB.glob("*.py")) + sorted(ROUTES.glob("*.py")) + [
        PKG / "_deps.py", PKG / "batch_ops.py", PKG / "handoff.py",
    ]
    for f in files:
        if not f.exists():
            continue
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                index.setdefault(node.name, []).append(node)
    return index


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for c in ast.walk(node):
        if isinstance(c, ast.Call):
            if isinstance(c.func, ast.Name):
                names.add(c.func.id)
            elif isinstance(c.func, ast.Attribute):
                names.add(c.func.attr)
    return names


def _publishes(node: ast.AST, index: dict[str, list[ast.AST]], depth: int = 5, seen: set[str] | None = None) -> bool:
    """True when the handler (or anything it calls, a few levels down) calls a publish helper."""
    seen = seen if seen is not None else set()
    names = _called_names(node)
    if names & PUBLISH_CALLS:
        return True
    if depth == 0:
        return False
    for name in sorted(names):
        if name in seen or name not in index:
            continue
        seen.add(name)
        if any(_publishes(fn, index, depth - 1, seen) for fn in index[name]):
            return True
    return False


@functools.lru_cache(maxsize=1)
def _dashboard_source() -> str:
    parts = []
    for p in sorted(STATIC.glob("*.ts")):
        if p.name.endswith((".test.ts", ".d.ts")) or p.name == "source-harness.ts":
            continue
        parts.append(p.read_text(encoding="utf-8"))
    return "\n".join(parts).replace("\r", "")


_SEGMENT = r"(?:\$\{[^}]*\}|[^/'\"`$?\s]+)"


def _dashboard_calls(method: str, path: str, ts: str) -> bool:
    """Does any dashboard module call ``METHOD path``? (Heuristic: URL literal + nearby method.)"""
    segs = path.strip("/").split("/")
    parts = [_SEGMENT if re.fullmatch(r"\{[^}]+\}", s) else re.escape(s) for s in segs]
    patterns = [r"['\"`]/" + "/".join(parts) + r"(?![\w\-/{.])"]
    if re.fullmatch(r"\{[^}]+\}", segs[-1]):
        # '/tasks/' + id -- the last segment concatenated instead of interpolated
        patterns.append(r"['\"`]/" + "/".join(re.escape(s) for s in segs[:-1]) + r"/['\"]\s*\+")
    for pat in patterns:
        for m in re.finditer(pat, ts):
            window = ts[m.start(): m.start() + 450]
            nxt = re.search(r"\b(?:api|projectApi|fetch)\(", window[10:])
            if nxt:
                window = window[: 10 + nxt.start()]
            mm = re.search(r"method:\s*['\"](\w+)['\"]", window)
            used = mm.group(1).upper() if mm else "GET"
            if used == method:
                return True
    return False


def test_dashboard_driven_mutating_routes_publish_an_event():
    index = _function_index()
    ts = _dashboard_source()
    offenders = []
    for method, path, fname, node in _mutating_routes():
        if not _in_scope(path):
            continue
        if not _dashboard_calls(method, path, ts):
            continue  # API-only / MCP-only / hook-only route: no dashboard view to repaint
        if _publishes(node, index):
            continue
        if (method, path) in SILENT_ROUTES:
            continue
        offenders.append(f"{method} {path}  ({fname})")
    assert not offenders, (
        "These mutating routes are called by the dashboard but publish no WebSocket event "
        "(directly or through the db function they call), so any other open dashboard, "
        "session or MCP client leaves the view stale until a reload:\n  "
        + "\n  ".join(offenders)
        + "\nPublish one with _publish_project_event(project_id, '<type>', {...}) from the db "
        "function that makes the change and add a handleWsEvent branch -- or, if no "
        "WebSocket-fed view shows this data, add the route to SILENT_ROUTES in "
        "tests/test_ws_event_coverage.py with the reason."
    )


def test_silent_routes_allowlist_has_no_stale_entries():
    index = _function_index()
    ts = _dashboard_source()
    routes = {(m, p): node for m, p, _f, node in _mutating_routes()}
    for key, reason in SILENT_ROUTES.items():
        assert reason.strip(), f"{key} has no reason"
        assert key in routes, f"SILENT_ROUTES lists {key} but no such route exists -- remove it"
        assert _in_scope(key[1]), f"{key} is out of scope for the rule anyway -- remove it"
        assert _dashboard_calls(key[0], key[1], ts), (
            f"{key} is no longer called by the dashboard -- remove it from SILENT_ROUTES"
        )
        assert not _publishes(routes[key], index), (
            f"{key} publishes an event now -- remove it from SILENT_ROUTES"
        )


def test_the_scan_sees_the_routes_this_change_fixed():
    """Guard the scan itself: if these stop being detected the test above proves nothing."""
    index = _function_index()
    ts = _dashboard_source()
    routes = {(m, p): node for m, p, _f, node in _mutating_routes()}
    for key in (
        ("DELETE", "/projects/{project_id}/sprint-items/{item_id}"),
        ("PATCH", "/projects/{project_id}/sprint-items/{item_id}"),
        ("POST", "/projects/{project_id}/sprint-items/{item_id}/complete"),
        ("POST", "/projects/{project_id}/sprint-items/{item_id}/push"),
        ("DELETE", "/tasks/{task_id}"),
        ("DELETE", "/projects/{project_id}/notes/{note_id}"),
        ("PATCH", "/projects/{project_id}/decisions-pinned/{decision_id}"),
        ("PATCH", "/sessions/{session_id}"),
    ):
        assert key in routes, f"route {key} not found"
        assert _dashboard_calls(key[0], key[1], ts), f"dashboard caller for {key} not detected"
        assert _publishes(routes[key], index), f"{key} should publish an event"


def test_the_publish_scan_is_not_vacuous():
    """A synthetic route that writes and publishes nothing must be flagged by the scan."""
    tree = ast.parse(
        "async def silent(project_id, request):\n"
        "    db = await _db(request)\n"
        "    await db.execute('DELETE FROM sprint_items WHERE id = ?', (project_id,))\n"
        "    await db.commit()\n"
    )
    assert not _publishes(tree.body[0], _function_index())
    tree2 = ast.parse(
        "async def loud(project_id, request):\n"
        "    _publish_project_event(project_id, 'x_changed', {})\n"
    )
    assert _publishes(tree2.body[0], _function_index())
