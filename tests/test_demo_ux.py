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
    js = client.get("/static/dashboard.js").text
    # Both buttons must be present in the demo-disable block (the list that
    # rewires onclick to showDemoReadonlyToast()).
    assert "setupHooksBtn," in js
    assert "copyStartChatBtn," in js


def test_hosted_connection_switch_does_not_restart_server(client):
    """Phase 3: hosted instances must never trigger a full-server restart when
    switching DB connections — that would kill the shared Fly machine."""
    js = client.get("/static/dashboard.js").text
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
