"""Tests for demo UX — onboarding overlay, 403 handling, nav links, quickstart.

HTTP tests use the ``client`` or ``demo_client`` fixture.
``demo_client`` explicitly enables demo DB seeding (overrides MERIDIAN_SKIP_DEMO).
Playwright tests require ``pixi run install-browsers`` and are skipped
when the playwright package or chromium is absent.
"""

from __future__ import annotations

import json
import pytest


@pytest.fixture
def demo_client(tmp_path, monkeypatch):
    """TestClient with demo DB enabled and seeded (overrides MERIDIAN_SKIP_DEMO)."""
    monkeypatch.setenv("MERIDIAN_DB", ":memory:")
    monkeypatch.setenv("MERIDIAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_DEMO_DB_URL", "")
    monkeypatch.setenv("MERIDIAN_STANDARD_KEY", "")
    monkeypatch.setenv("MERIDIAN_SKIP_DEMO", "0")
    monkeypatch.setenv("MERIDIAN_GOAL_MD", str(tmp_path / "GOAL.md"))

    from fastapi.testclient import TestClient
    import importlib
    import meridian.server as server_module
    server_module = importlib.reload(server_module)
    with TestClient(server_module.app) as c:
        yield c


# ---------------------------------------------------------------------------
# HTTP tests — no browser required
# ---------------------------------------------------------------------------


def test_demo_loads_200(client):
    """GET /demo returns 200 without any auth cookie."""
    r = client.get("/demo", follow_redirects=False)
    assert r.status_code == 200, r.text


def test_demo_no_auth_redirect(client):
    """/demo must not redirect unauthenticated users — it's always public."""
    r = client.get("/demo", follow_redirects=False)
    assert r.status_code not in (301, 302, 307, 308), "Got unexpected redirect"


def test_demo_projects_seeded(demo_client):
    """After /demo sets the cookie, /projects returns the seeded demo data."""
    r = demo_client.get("/demo")
    assert r.status_code == 200
    # cookie is set by demo route; TestClient automatically carries it
    projects_r = demo_client.get("/projects")
    assert projects_r.status_code == 200, projects_r.text
    names = [p["name"] for p in projects_r.json()]
    assert "backend-api-v2" in names, f"backend-api-v2 not found in: {names}"


def test_demo_write_blocked_403(demo_client):
    """Writes with the demo cookie must return 403, not a raw error."""
    demo_client.get("/demo")  # sets demo cookie
    # POST to any write endpoint — middleware blocks before route logic runs
    r = demo_client.post(
        "/projects/00000000-0000-0000-0000-000000000000/start-session",
        json={"session_name": "test", "human_id": "test"},
    )
    assert r.status_code == 403
    body = r.json()
    assert "error" in body or "demo" in json.dumps(body).lower()


# ---------------------------------------------------------------------------
# Phase 2 — demo isolation: fail-closed _db() + /config demo gating
# ---------------------------------------------------------------------------


def test_demo_db_fails_closed_on_remote_backend(client):
    """A demo cookie on a remote backend with no demo_db must 503, never leak.

    Regression: /demo on a hosted/Postgres self-host previously fell through to
    app.state.db (real tenant data). _db() now fails closed unless the backend
    is a pure-local SQLite self-host.
    """
    app = client.app
    prev_remote = getattr(app.state, "db_is_remote", None)
    prev_demo = getattr(app.state, "demo_db", None)
    app.state.db_is_remote = True
    app.state.demo_db = None
    try:
        client.cookies.set("meridian_demo", "1")
        r = client.get("/projects")
        assert r.status_code == 503, r.text
        assert "demo" in r.text.lower()
    finally:
        client.cookies.delete("meridian_demo")
        app.state.db_is_remote = prev_remote
        app.state.demo_db = prev_demo


def test_config_demo_mode_reflects_cookie(client):
    """/config must reflect the demo cookie, not just the env flag.

    A /demo visitor on a self-host must never see the host's real connection
    switcher or toml path (split-brain leak).
    """
    client.cookies.set("meridian_demo", "1")
    try:
        r = client.get("/config")
        assert r.status_code == 200, r.text
        cfg = r.json()
        assert cfg["demo_mode"] is True
        assert cfg["db"] == "demo"
        assert cfg["connections"] == []
        assert cfg["toml_path"] == ""
        assert cfg["toml_exists"] is False
    finally:
        client.cookies.delete("meridian_demo")


def test_config_no_cookie_is_not_demo(client):
    """Without the demo cookie, /config reports demo_mode False (no false-positive)."""
    r = client.get("/config")
    assert r.status_code == 200, r.text
    assert r.json()["demo_mode"] is False


def test_demo_gates_setup_hooks_and_planning_chat(client):
    """Phase 3: in demo mode the Setup Hooks and Planning Chat buttons are
    disabled with a sign-in toast, alongside the other write actions."""
    js = client.get("/static/dashboard.ts").text
    # Both buttons must be present in the demo-disable block (the list that
    # rewires onclick to showDemoReadonlyToast()).
    assert "setupHooksBtn," in js
    assert "copyStartChatBtn," in js


def test_hosted_connection_switch_does_not_restart_server(client):
    """Phase 3: hosted instances must never trigger a full-server restart when
    switching DB connections — that would kill the shared Fly machine."""
    js = client.get("/static/dashboard.ts").text
    assert "applies on next server restart" in js
    # The hosted branch must guard the _doRestart() call in the switcher.
    assert "if (isHostedMode()) {" in js


def test_waitlist_pending_200(client):
    """GET /waitlist-pending loads without error."""
    r = client.get("/waitlist-pending")
    assert r.status_code == 200


def test_waitlist_pending_back_to_home(client):
    """/waitlist-pending contains a 'Back to home' navigation link."""
    r = client.get("/waitlist-pending")
    assert r.status_code == 200
    assert "Back to home" in r.text or 'href="/"' in r.text


def test_waitlist_pending_try_demo_link(client):
    """/waitlist-pending contains a link to /demo."""
    r = client.get("/waitlist-pending")
    assert r.status_code == 200
    assert "/demo" in r.text


def test_mcp_quickstart_200(client):
    """GET /mcp/quickstart returns 200 plain text."""
    r = client.get("/mcp/quickstart")
    assert r.status_code == 200
    ct = r.headers.get("content-type", "")
    assert "text/plain" in ct, f"Expected text/plain, got: {ct}"


def test_mcp_quickstart_contains_start_session(client):
    """/mcp/quickstart must mention start_session prominently."""
    r = client.get("/mcp/quickstart")
    assert r.status_code == 200
    assert "start_session" in r.text


def test_mcp_quickstart_tool_names(client):
    """/mcp/quickstart must list the five core tools."""
    r = client.get("/mcp/quickstart")
    assert r.status_code == 200
    for tool in ("start_session", "log_task", "checkpoint", "pin_decision", "request_hitl"):
        assert tool in r.text, f"Tool '{tool}' not found in /mcp/quickstart"


# ---------------------------------------------------------------------------
# Playwright tests — skipped when playwright/chromium unavailable
# ---------------------------------------------------------------------------

try:
    from playwright.sync_api import sync_playwright  # noqa: F401
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

pytestmark_playwright = pytest.mark.skipif(
    not _PLAYWRIGHT_AVAILABLE, reason="playwright not installed — run pixi run install-browsers"
)


@pytestmark_playwright
def test_demo_overlay_renders(client):
    """Playwright: onboarding overlay appears on /demo load."""
    import threading
    import uvicorn
    from meridian import server as server_module

    with sync_playwright() as p:
        # Start a real server on a free port for Playwright
        config = uvicorn.Config(server_module.app, host="127.0.0.1", port=17878, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        # Give it a moment to start
        import time; time.sleep(1.5)

        try:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto("http://127.0.0.1:17878/demo", wait_until="domcontentloaded")
            page.wait_for_timeout(2000)  # allow JS to run

            overlay = page.query_selector("#demo-onboarding-overlay")
            assert overlay is not None, "Demo onboarding overlay not found"
            assert overlay.is_visible(), "Demo onboarding overlay not visible"

            browser.close()
        finally:
            server.should_exit = True


@pytestmark_playwright
def test_demo_overlay_dismisses(client):
    """Playwright: X button dismisses overlay; no localStorage key set."""
    import threading
    import uvicorn
    from meridian import server as server_module

    with sync_playwright() as p:
        config = uvicorn.Config(server_module.app, host="127.0.0.1", port=17879, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        import time; time.sleep(1.5)

        try:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto("http://127.0.0.1:17879/demo", wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

            # Click the X dismiss button
            page.click("#demo-onboarding-overlay button[title='Dismiss']")
            page.wait_for_timeout(500)

            overlay = page.query_selector("#demo-onboarding-overlay")
            assert overlay is None or not overlay.is_visible(), "Overlay still visible after dismiss"

            # Verify no localStorage key was set (old behavior was storing 'meridian_demo_hint')
            hint_value = page.evaluate("() => localStorage.getItem('meridian_demo_hint')")
            assert hint_value is None, f"Unexpected localStorage key set: meridian_demo_hint={hint_value!r}"

            browser.close()
        finally:
            server.should_exit = True


@pytestmark_playwright
def test_demo_write_button_shows_friendly_toast(client):
    """Playwright: clicking a write button in demo shows 'Read-only demo' toast, not raw 403."""
    import threading
    import uvicorn
    from meridian import server as server_module

    with sync_playwright() as p:
        config = uvicorn.Config(server_module.app, host="127.0.0.1", port=17880, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        import time; time.sleep(1.5)

        try:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto("http://127.0.0.1:17880/demo", wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

            # Dismiss connection-setup modal if present (appears on CI where no
            # meridian.toml exists — the modal intercepts all pointer events).
            conn_modal = page.query_selector("#conn-setup-modal")
            if conn_modal and conn_modal.is_visible():
                page.keyboard.press("Escape")
                page.wait_for_timeout(150)
                if conn_modal.is_visible():
                    close_btn = page.query_selector("#conn-setup-modal button[title='Close (Esc)']")
                    if close_btn:
                        close_btn.click()
                page.wait_for_timeout(300)

            # Dismiss demo onboarding overlay if present
            overlay_btn = page.query_selector("#demo-onboarding-overlay button")
            if overlay_btn:
                overlay_btn.click()
                page.wait_for_timeout(300)

            # Try to find and click the "Claim & start worker" button
            worker_btn = page.query_selector("[id^='start-worker-']")
            if worker_btn:
                worker_btn.click()
                page.wait_for_timeout(800)
                toast = page.query_selector("#toast.show")
                if toast:
                    toast_text = toast.inner_text()
                    assert "403" not in toast_text, f"Raw 403 shown in toast: {toast_text!r}"
                    assert "Read-only" in toast_text or "sign in" in toast_text.lower(), \
                        f"Expected friendly message, got: {toast_text!r}"

            browser.close()
        finally:
            server.should_exit = True


@pytestmark_playwright
def test_codebase_force_graph_builder(client):
    """65742e42 — _buildCodebaseForceGraph + _normalizeGraphEdges produce a valid
    ECharts force-graph option from architecture packages + edges."""
    import threading
    import uvicorn
    from meridian import server as server_module

    with sync_playwright() as p:
        config = uvicorn.Config(server_module.app, host="127.0.0.1", port=17887, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        import time; time.sleep(1.5)
        try:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto("http://127.0.0.1:17887/demo", wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            res = page.evaluate(
                """() => {
                    const bg = window._buildCodebaseForceGraph;
                    const ne = window._normalizeGraphEdges;
                    if (typeof bg !== 'function' || typeof ne !== 'function') return {error: 'missing'};
                    const pkgs = [
                        {name: 'meridian.db', node_count: 100, layer: 0},
                        {name: 'meridian.routes', node_count: 40, layer: 1},
                        {name: 'tests', node_count: 10, layer: 2},
                    ];
                    // Edge normalization from a row-array result envelope.
                    const edges = ne({content: [{text: JSON.stringify([
                        ['meridian.routes', 'meridian.db', 12],
                        ['tests', 'meridian.db', 3],
                    ])}]});
                    const opt = bg(pkgs, edges, 'packages');
                    const optHot = bg(pkgs, edges, 'hotspots');
                    return {
                        edges,
                        type: opt.series[0].type,
                        layout: opt.series[0].layout,
                        nNodes: opt.series[0].data.length,
                        nLinks: opt.series[0].links.length,
                        nCats: opt.series[0].categories.length,
                        empty: bg([], edges, 'packages'),
                        hotHasData: optHot.series[0].data.length === 3,
                    };
                }"""
            )
            assert res.get("error") is None, res
            assert res["edges"] == [
                {"source": "meridian.routes", "target": "meridian.db", "value": 12},
                {"source": "tests", "target": "meridian.db", "value": 3},
            ]
            assert res["type"] == "graph"
            assert res["layout"] == "force"
            assert res["nNodes"] == 3
            assert res["nLinks"] == 2
            assert res["nCats"] == 3          # 3 distinct layers
            assert res["empty"] is None       # no packages → null
            assert res["hotHasData"] is True
            browser.close()
        finally:
            server.should_exit = True


@pytestmark_playwright
def test_sprint_history_badges_renderer(client):
    """c2fe20c3 — stall counter, retried tag, and live pulse dot render per item."""
    import threading
    import uvicorn
    from meridian import server as server_module

    with sync_playwright() as p:
        config = uvicorn.Config(server_module.app, host="127.0.0.1", port=17886, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        import time; time.sleep(1.5)
        try:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto("http://127.0.0.1:17886/demo", wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            res = page.evaluate(
                """() => {
                    const f = window._sprintHistoryBadges;
                    if (typeof f !== 'function') return {error: 'missing'};
                    return {
                        stalled: f({status: 'pending', stall_count: 3}),
                        retried: f({status: 'pending', claimed_at: '2026-01-01 00:00:00'}),
                        live: f({status: 'in_progress', claimed_at: '2026-01-01 00:00:00'}),
                        fresh: f({status: 'pending'}),
                    };
                }"""
            )
            assert res.get("error") is None, res
            assert "↻3" in res["stalled"]
            assert "sprint-retried-badge" in res["retried"]
            assert "sprint-live-dot" in res["live"]
            assert res["fresh"] == ""  # fresh pending → no badges
            # The pulse keyframes style was injected.
            has_style = page.evaluate("() => !!document.getElementById('sprint-pulse-style')")
            assert has_style
            browser.close()
        finally:
            server.should_exit = True


@pytestmark_playwright
def test_auto_answered_hitl_renderer(client):
    """0b9b12c8 — auto-answered HITLs render greyed; non-auto / empty render ''."""
    import threading
    import uvicorn
    from meridian import server as server_module

    with sync_playwright() as p:
        config = uvicorn.Config(server_module.app, host="127.0.0.1", port=17885, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        import time; time.sleep(1.5)
        try:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto("http://127.0.0.1:17885/demo", wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            res = page.evaluate(
                """() => {
                    const f = window._renderAutoAnsweredHitls;
                    if (typeof f !== 'function') return {error: 'missing'};
                    return {
                        withAuto: f([
                            {id: 'a', answered_by: 'auto', question: 'deploy?', answer: 'yes'},
                            {id: 'b', answered_by: 'human', question: 'x', answer: 'no'},
                        ]),
                        none: f([{id: 'b', answered_by: 'human', question: 'x'}]),
                        empty: f([]),
                    };
                }"""
            )
            assert res.get("error") is None, res
            assert "AUTO-ANSWERED" in res["withAuto"]
            assert "deploy?" in res["withAuto"]
            assert "data-hitl-auto-id" in res["withAuto"]
            # The human-answered one is NOT included.
            assert res["withAuto"].count("data-hitl-auto-id") == 1
            assert res["none"] == ""
            assert res["empty"] == ""
            browser.close()
        finally:
            server.should_exit = True


@pytestmark_playwright
def test_slot_health_warning_renderer(client):
    """9a8645c1 — _renderSlotHealthWarning shows an actionable badge for an
    unhealthy slot and nothing for a healthy one. Runs the real bundle."""
    import threading
    import uvicorn
    from meridian import server as server_module

    with sync_playwright() as p:
        config = uvicorn.Config(server_module.app, host="127.0.0.1", port=17884, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        import time; time.sleep(1.5)
        try:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto("http://127.0.0.1:17884/demo", wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            res = page.evaluate(
                """() => {
                    const f = window._renderSlotHealthWarning;
                    if (typeof f !== 'function') return {error: 'missing'};
                    const ss = {extract: {reason: 'access_denied', detail: 'use a specific repo path'}};
                    return {
                        unhealthy: f('extract', ss),
                        healthy: f('fs', ss),       // no entry → ''
                        empty: f('extract', {}),
                    };
                }"""
            )
            assert res.get("error") is None, res
            assert "access denied" in res["unhealthy"]
            assert "specific repo path" in res["unhealthy"]
            assert "data-slot-warning" in res["unhealthy"]
            assert res["healthy"] == ""
            assert res["empty"] == ""
            browser.close()
        finally:
            server.should_exit = True


@pytestmark_playwright
def test_settings_tab_classifier(client):
    """0bf67524 — the settings-tab classifier maps section ids to the right tab.

    Runs the real bundled `window._classifySettingsSection` in-browser so the
    shipped logic (not a re-implementation) is what's asserted.
    """
    import threading
    import uvicorn
    from meridian import server as server_module

    with sync_playwright() as p:
        config = uvicorn.Config(server_module.app, host="127.0.0.1", port=17882, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        import time; time.sleep(1.5)

        try:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto("http://127.0.0.1:17882/demo", wait_until="domcontentloaded")
            page.wait_for_timeout(2000)  # allow the bundle to load + run

            result = page.evaluate(
                """() => {
                    const fn = window._classifySettingsSection;
                    if (typeof fn !== 'function') return {error: 'classifier missing'};
                    const mk = (id) => { const d = document.createElement('div'); d.id = id; return d; };
                    return {
                        account_card: fn(mk('settings-account-card-P'), 'P'),
                        danger: fn(mk('settings-account-danger-P'), 'P'),
                        members: fn(mk('members-section-P'), 'P'),
                        github: fn(mk('github-card-P'), 'P'),
                        workspace: fn(mk('workspace-section-P'), 'P'),
                        project_grp: fn(mk('settings-grp-ps-P'), 'P'),
                        notifications: fn(mk('settings-notifications-card-P'), 'P'),
                        unknown: fn(mk('something-random-P'), 'P'),
                    };
                }"""
            )
            assert result.get("error") is None, result
            assert result["account_card"] == "account"
            assert result["danger"] == "account"
            assert result["members"] == "account"
            assert result["github"] == "account"
            assert result["workspace"] == "workspace"
            assert result["project_grp"] == "project"
            assert result["notifications"] == "project"
            assert result["unknown"] == "project"  # safe default

            browser.close()
        finally:
            server.should_exit = True


@pytestmark_playwright
def test_settings_disconnects_observer_before_innerhtml_wipe():
    """Regression guard (4bde9437 settings black-screen). The blank-on-second-open
    bug was a leaked MutationObserver reparenting freshly-rendered children into
    orphaned panes. The fix requires loadSettingsTab to disconnect
    body._settingsObs AND clear dataset.tabbed BEFORE it wipes innerHTML — this
    test pins that ordering so a future refactor can't silently reintroduce the
    black screen.
    """
    from pathlib import Path
    src = (
        Path(__file__).resolve().parent.parent
        / "meridian" / "static" / "dashboard-settings.ts"
    )
    text = src.read_text(encoding="utf-8")
    disconnect_idx = text.find("_settingsObs.disconnect()")
    # First innerHTML assignment in loadSettingsTab is the 'loading…' wipe.
    wipe_idx = text.find("body.innerHTML =")
    assert disconnect_idx != -1, "observer disconnect removed — black-screen guard gone"
    assert wipe_idx != -1, "settings innerHTML wipe not found"
    assert disconnect_idx < wipe_idx, (
        "MutationObserver must be disconnected BEFORE the innerHTML wipe or the "
        "settings panel blanks on the second open (black-screen regression)"
    )
    assert "delete body.dataset.tabbed" in text, (
        "dataset.tabbed must be cleared before the wipe so the 30s TTL early-return "
        "cannot serve a blanked panel as fresh"
    )


def test_settings_tabs_organize_and_switch(client):
    """0bf67524 — _organizeSettingsIntoTabs builds the Project/Workspace/Account
    tab bar, distributes sections, extracts the nested workspace-section, routes
    async-appended sections, and switches panes on click. Runs the real bundle."""
    import threading
    import uvicorn
    from meridian import server as server_module

    with sync_playwright() as p:
        config = uvicorn.Config(server_module.app, host="127.0.0.1", port=17883, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        import time; time.sleep(1.5)

        try:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto("http://127.0.0.1:17883/demo", wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

            built = page.evaluate(
                """() => {
                    const PID = 'TESTPID';
                    // Build a synthetic flat settings body mirroring the template:
                    // an account card, the PROJECT SETTINGS group, and the account
                    // group with the workspace-section nested inside it.
                    const body = document.createElement('div');
                    body.id = 'settings-body-' + PID;
                    body.innerHTML =
                        '<div id="settings-account-card-' + PID + '">acct</div>' +
                        '<details id="settings-grp-ps-' + PID + '">proj</details>' +
                        '<details id="settings-grp-aw-' + PID + '">acctgrp' +
                          '<div id="workspace-section-' + PID + '">ws</div>' +
                        '</details>' +
                        '<div id="settings-notifications-card-' + PID + '">notif</div>';
                    document.body.appendChild(body);
                    window._organizeSettingsIntoTabs(PID);

                    const paneOf = (sel) => {
                        const el = document.querySelector(sel);
                        const pane = el && el.closest('.settings-tabpane');
                        return pane ? pane.dataset.stabPane : null;
                    };
                    const tabBtns = body.querySelectorAll('.settings-tabbar .settings-tab-btn');

                    // Simulate an async-appended Tunnel Plugins section → account.
                    const tp = document.createElement('div');
                    tp.id = 'tunnel-plugins-' + PID;
                    tp.textContent = 'tunnel';
                    body.appendChild(tp);
                    return {
                        nButtons: tabBtns.length,
                        labels: Array.from(tabBtns).map(b => b.textContent),
                        accountCardPane: paneOf('#settings-account-card-' + PID),
                        projGrpPane: paneOf('#settings-grp-ps-' + PID),
                        workspacePane: paneOf('#workspace-section-' + PID),
                        notifPane: paneOf('#settings-notifications-card-' + PID),
                    };
                }"""
            )
            assert built["nButtons"] == 3, built
            assert built["labels"] == ["Project", "Workspace", "Account"], built
            assert built["accountCardPane"] == "account", built
            assert built["projGrpPane"] == "project", built
            # The nested workspace-section is extracted into the Workspace pane.
            assert built["workspacePane"] == "workspace", built
            assert built["notifPane"] == "project", built

            # Let the MutationObserver route the async-appended tunnel section.
            page.wait_for_timeout(200)
            tunnel_pane = page.evaluate(
                """() => {
                    const el = document.getElementById('tunnel-plugins-TESTPID');
                    const pane = el && el.closest('.settings-tabpane');
                    return pane ? pane.dataset.stabPane : null;
                }"""
            )
            assert tunnel_pane == "account", f"tunnel section routed to {tunnel_pane!r}"

            # Switching tabs: activate Workspace → only the workspace pane visible.
            vis = page.evaluate(
                """() => {
                    window._activateSettingsTab('TESTPID', 'workspace');
                    const body = document.getElementById('settings-body-TESTPID');
                    const paneVis = {};
                    body.querySelectorAll(':scope > .settings-tabpane').forEach(pn => {
                        paneVis[pn.dataset.stabPane] = pn.style.display !== 'none';
                    });
                    return paneVis;
                }"""
            )
            assert vis["workspace"] is True, vis
            assert vis["project"] is False, vis
            assert vis["account"] is False, vis

            browser.close()
        finally:
            server.should_exit = True


@pytestmark_playwright
def test_panels_render_without_pageerror(client):
    """Playwright: /dashboard and /demo render a non-empty main panel, throw
    zero uncaught JS errors, and never 5xx on the panel's data requests.

    Guards the blank-panel regression. The real defect was not a JS throw but a
    server-side 500 on /projects/{id}/sessions ('cached plan must not change
    result type' after an ALTER TABLE migration), which left the panel blank
    because its sessions/status data never loaded. This asserts both the panel
    container is populated AND that its backing requests succeed.
    """
    import threading
    import time
    import urllib.request
    import uvicorn
    from meridian import server as server_module

    with sync_playwright() as p:
        config = uvicorn.Config(server_module.app, host="127.0.0.1", port=17881, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        time.sleep(1.5)

        # Seed a project against the *live* server so /dashboard renders an
        # active project panel rather than the first-run wizard. The uvicorn
        # server runs its own lifespan and owns a separate in-memory DB from
        # the TestClient, so the row must be created over HTTP, not via client.
        urllib.request.urlopen(
            urllib.request.Request(
                "http://127.0.0.1:17881/projects",
                data=b'{"name": "panel-render"}',
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            timeout=8,
        )

        try:
            browser = p.chromium.launch()
            for path in ("/dashboard", "/demo"):
                errors: list[str] = []
                console_errors: list[str] = []
                request_failed: list[str] = []
                server_errors: list[str] = []
                page = browser.new_page()
                page.on("pageerror", lambda e, _errs=errors: _errs.append(str(e)))
                page.on(
                    "console",
                    lambda msg, _errs=console_errors: _errs.append(msg.text)
                    if msg.type == "error"
                    and "ERR_INSUFFICIENT_RESOURCES" not in msg.text
                    else None,
                )
                page.on(
                    "requestfailed",
                    lambda req, _errs=request_failed: _errs.append(
                        f"{req.method} {req.url} -> {req.failure}"
                    ),
                )
                page.on(
                    "response",
                    lambda r, _errs=server_errors: _errs.append(f"{r.status} {r.url}")
                    if r.status >= 500
                    else None,
                )
                page.goto(f"http://127.0.0.1:17881{path}", wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                # Dismiss overlays/modals so they don't mask the panel.
                for sel in ("#demo-onboarding-overlay", "#conn-setup-modal"):
                    page.evaluate(
                        f"() => {{ const e = document.querySelector('{sel}'); if (e) e.remove(); }}"
                    )
                page.wait_for_timeout(200)

                assert not errors, f"{path} threw uncaught JS errors: {errors}"
                assert not console_errors, f"{path} logged console errors: {console_errors}"
                assert not request_failed, f"{path} had failed requests: {request_failed}"
                assert not server_errors, \
                    f"{path} backing requests 5xx'd (would blank the panel): {server_errors}"

                body_len = page.evaluate("() => document.body.innerHTML.length")
                assert body_len > 5000, f"{path} body suspiciously small: {body_len}"

                # When a project panel is present (always on /dashboard, where we
                # seeded a project) its default 'status' drawer must render content.
                # /demo has no seeded project under MERIDIAN_SKIP_DEMO, so only
                # enforce the drawer check when a panel actually rendered.
                pid = page.evaluate(
                    "() => { const el = document.querySelector('[id^=\"vtab-strip-\"]');"
                    " return el ? el.id.replace('vtab-strip-','') : null; }"
                )
                if path == "/dashboard":
                    assert pid, f"{path}: no active project panel found"
                if pid:
                    body_inner = page.evaluate(
                        f"() => {{ const b = document.querySelector('#tab-body-{pid}');"
                        f" return b ? b.innerHTML.trim().length : -1; }}"
                    )
                    assert body_inner > 100, \
                        f"{path}: main panel container blank (len={body_inner})"
                    drawer_len = page.evaluate(
                        f"() => {{ const d = document.querySelector('#drawer-status-{pid}');"
                        f" return d ? d.innerHTML.trim().length : -1; }}"
                    )
                    assert drawer_len > 2, f"{path}: status drawer blank (len={drawer_len})"
                page.close()
            browser.close()
        finally:
            server.should_exit = True


@pytestmark_playwright
def test_dashboard_shows_visible_error_when_sessions_request_fails(client):
    """Playwright: a failed sessions fetch must surface a visible retryable error."""
    import json as _json
    import threading
    import time
    import urllib.request
    import uvicorn
    from meridian import server as server_module

    with sync_playwright() as p:
        config = uvicorn.Config(server_module.app, host="127.0.0.1", port=17883, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        time.sleep(1.5)

        req = urllib.request.Request(
            "http://127.0.0.1:17883/projects",
            data=b'{"name": "panel-error"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            project = _json.loads(resp.read().decode("utf-8"))
        pid = project["id"]

        try:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.route(
                "**/projects/*/sessions*",
                lambda route: route.fulfill(
                    status=500,
                    content_type="application/json",
                    body=_json.dumps({"detail": "cached plan must not change result type"}),
                ),
            )
            page.goto(
                f"http://127.0.0.1:17883/dashboard?project_id={pid}",
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(2500)

            alert = page.locator(f"#project-fetch-alert-{pid}")
            assert alert.is_visible(), "project fetch alert should be visible on sessions failure"
            alert_text = alert.inner_text()
            assert "project data failed to load" in alert_text.lower()
            assert "/sessions" in alert_text
            assert "HTTP 500" in alert_text
            assert "cached plan must not change result type" in alert_text

            sessions_panel = page.locator(f"#sessions-{pid}")
            sessions_text = sessions_panel.inner_text()
            assert "sessions unavailable" in sessions_text.lower()
            assert "retry failed loads" in sessions_text.lower()

            body_inner = page.evaluate(
                f"() => {{ const b = document.querySelector('#tab-body-{pid}');"
                f" return b ? b.innerHTML.trim().length : -1; }}"
            )
            assert body_inner > 100, "tab body should stay rendered even when sessions fails"
            browser.close()
        finally:
            server.should_exit = True


@pytestmark_playwright
def test_demo_never_opens_connection_setup_modal(client):
    """G3.13 — /demo runs against the seeded demo DB and must never trigger
    the local-server connection-setup wizard. _showConnSetupIfNeeded bails
    out early in demo mode."""
    import threading
    import time
    import uvicorn
    from meridian import server as server_module

    with sync_playwright() as p:
        config = uvicorn.Config(server_module.app, host="127.0.0.1", port=17889, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        time.sleep(1.5)
        try:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto("http://127.0.0.1:17889/demo", wait_until="domcontentloaded")
            page.wait_for_timeout(2200)
            modal = page.locator("#conn-setup-modal")
            assert modal.count() == 1, "conn-setup-modal element must exist in markup"
            # Modal might be in the DOM but must NOT be displayed.
            displayed = page.evaluate(
                "() => { const m = document.getElementById('conn-setup-modal');"
                " return m ? window.getComputedStyle(m).display : 'missing'; }"
            )
            assert displayed in ("none", ""), \
                f"conn-setup-modal should stay hidden on /demo, got display={displayed!r}"
            browser.close()
        finally:
            server.should_exit = True


@pytestmark_playwright
def test_free_tier_signout_visible_on_hosted_dashboard(client, monkeypatch):
    """Item 42 — free-tier /dashboard must show the sign-out control.

    Regression. Previously the sign-out link was created only inside
    _renderPlanBadge(me), which runs only when /me returns a plan. Anywhere
    /me returned {} or errored, the link never appeared — the bug surfaced on
    free tier. Fix: ensureSignOutLink() is called from hideHostedAdminControls,
    which runs unconditionally for any hosted user at init.

    This test boots the server with MERIDIAN_HOSTED=true and a session cookie
    so /me returns {}. The sign-out link must still render and be visible.
    """
    import threading
    import time
    import uvicorn
    from meridian import server as server_module
    from meridian import hosted as hosted_module
    from playwright.sync_api import sync_playwright

    monkeypatch.setenv("MERIDIAN_HOSTED", "true")

    # Mock get_current_tenant to accept the request and return a free-tier tenant
    # so /me returns {} (no plan).
    async def mock_get_current_tenant(request):
        return {"email": "test@example.com", "id": "test-tenant-id"}

    monkeypatch.setattr(hosted_module, "get_current_tenant", mock_get_current_tenant)

    with sync_playwright() as p:
        config = uvicorn.Config(server_module.app, host="127.0.0.1", port=17884, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        time.sleep(1.5)

        try:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto("http://127.0.0.1:17884/dashboard", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)  # let loadServerConfig + /me + init finish

            # Remove modals/wizards that can mask the sidebar footer.
            for sel in ("#conn-setup-modal", "#demo-onboarding-overlay", "#ez-wizard"):
                page.evaluate(
                    f"() => {{ const e = document.querySelector('{sel}'); if (e) e.remove(); }}"
                )
            page.wait_for_timeout(150)

            # The dashboard must be running in hosted mode for this test to be
            # meaningful — otherwise hideHostedAdminControls never fires.
            hosted_flag = page.evaluate("() => !!window.MERIDIAN_HOSTED")
            assert hosted_flag, (
                "test setup error: window.MERIDIAN_HOSTED is false — "
                "MERIDIAN_HOSTED env var didn't reach the running server"
            )

            signout = page.query_selector("#signout-link")
            assert signout is not None, (
                "Item 42 regression: #signout-link missing from /dashboard for a "
                "hosted user without an auth cookie (the free-tier path). "
                "ensureSignOutLink() must be called from hideHostedAdminControls "
                "so the link appears even when /me returns {}."
            )
            assert signout.is_visible(), (
                "Item 42 regression: #signout-link is in the DOM but not visible"
            )
            href = signout.get_attribute("href")
            assert href == "/auth/logout", (
                f"Item 42: signout-link href must be /auth/logout, got {href!r}"
            )

            browser.close()
        finally:
            server.should_exit = True


@pytestmark_playwright
def test_demo_tour_persists_and_finishes(client):
    """Phase 4: the rebuilt demo tour persists progress across reloads and,
    once 'Finish tutorial' is clicked, is never auto-shown again."""
    import threading
    import time
    import uvicorn
    from meridian import server as server_module

    with sync_playwright() as p:
        config = uvicorn.Config(server_module.app, host="127.0.0.1", port=17882, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        time.sleep(1.5)

        try:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto("http://127.0.0.1:17882/demo", wait_until="domcontentloaded")
            page.wait_for_timeout(1500)

            # Start the tour from step 0 (dismiss the onboarding overlay first).
            page.evaluate(
                "() => { document.getElementById('demo-onboarding-overlay')?.remove(); startDemoTour(0); }"
            )
            page.wait_for_timeout(300)
            assert page.query_selector("#demo-tour-tooltip"), "tour tooltip did not render"

            # Advancing persists progress to localStorage.
            page.click("#demo-tour-next")
            page.wait_for_timeout(200)
            step = page.evaluate("() => localStorage.getItem('meridian_demo_tour.step')")
            assert step == "1", f"expected saved step 1, got {step!r}"

            # Reload + resume picks up at the saved step (label '2 / N').
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(1200)
            page.evaluate(
                "() => { document.getElementById('demo-onboarding-overlay')?.remove(); resumeDemoTour(); }"
            )
            page.wait_for_timeout(300)
            label = page.evaluate(
                "() => document.querySelector('#demo-tour-tooltip div')?.textContent || ''"
            )
            assert label.strip().startswith("2 /"), f"tour did not resume at step 2: {label!r}"

            # Finish marks the tour done; resuming must not reopen it.
            page.click("#demo-tour-finish")
            page.wait_for_timeout(200)
            done = page.evaluate("() => localStorage.getItem('meridian_demo_tour.done')")
            assert done == "1", f"finish did not set done flag: {done!r}"
            page.evaluate("() => resumeDemoTour()")
            page.wait_for_timeout(200)
            assert page.query_selector("#demo-tour-tooltip") is None, \
                "tour re-opened after Finish was clicked"

            browser.close()
        finally:
            server.should_exit = True
