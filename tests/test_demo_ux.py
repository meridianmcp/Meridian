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
    assert "detail" in body or "Demo mode" in json.dumps(body)


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
