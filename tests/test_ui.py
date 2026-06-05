"""Frontend / UI tests for the Meridian dashboard.

These tests use BeautifulSoup to inspect the static HTML structure and the
raw JS/CSS source files.  The dashboard is mostly built dynamically by
``dashboard.js``, so we check the *source* rather than a live browser.

Run:
    pixi run pytest tests/test_ui.py -v

No Playwright or headless browser is required — these are plain pytest tests
that hit the FastAPI TestClient.
"""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def html(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    return r.text


@pytest.fixture()
def soup(html):
    return BeautifulSoup(html, "html.parser")


@pytest.fixture()
def js(client):
    return client.get("/static/dashboard.js").text


@pytest.fixture()
def css(client):
    return client.get("/static/dashboard.css").text


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_dashboard_loads_200(client):
    """Dashboard returns HTTP 200 with a valid HTML document (smoke test)."""
    r = client.get("/dashboard")
    assert r.status_code == 200, f"expected 200, got {r.status_code}"

    soup = BeautifulSoup(r.text, "html.parser")
    assert soup.find("html") is not None, "response is not valid HTML"
    assert soup.find("body") is not None, "HTML body element missing"
    # Static JS and CSS must be referenced
    scripts = [s.get("src", "") for s in soup.find_all("script")]
    assert any("dashboard.js" in s for s in scripts), (
        "dashboard.html must load /static/dashboard.js"
    )
    links = [l.get("href", "") for l in soup.find_all("link")]
    assert any("dashboard.css" in l for l in links), (
        "dashboard.html must load /static/dashboard.css"
    )


def test_dashboard_north_star_not_same_as_version_goal(js):
    """Goal tab shows three separate subtabs — not stacked textareas sharing content.

    Bug 1 + Bug 5: The goal panel now has a [North Star] [Version Goal] [Sprint]
    tab bar. Each tab shows one full-height textarea. No edit/preview toggle on
    goal fields (those are structured data, not markdown documents).
    """
    # Subtab buttons must exist
    assert 'data-gtab="north-star"' in js, "north-star subtab button missing"
    assert 'data-gtab="version-goal"' in js, "version-goal subtab button missing"
    assert 'data-gtab="sprint"' in js, "sprint subtab button missing"
    # Each subtab panel has a distinct textarea ID
    assert "goal-north-star-" in js, "north-star textarea ID prefix missing"
    assert 'id="goal-${' in js or "goal-${project.id}" in js or '`goal-${' in js, (
        "version-goal textarea id missing"
    )
    assert "goal-sprint-" in js, "sprint textarea ID missing"
    # JS still reads goal.north_star for north star field
    assert "goal.north_star" in js, "refreshGoal must read goal.north_star"
    assert "goal.sprint" in js, "refreshGoal must read goal.sprint"
    # Goal subtab class names in JS
    assert "goal-subtab-btn" in js, "goal subtab buttons missing from buildTabBody"
    assert "goal-subtab-panel" in js, "goal subtab panels missing from buildTabBody"


def test_dashboard_has_save_buttons(client, js):
    """All three goal fields have dedicated save buttons and adequate height.

    Also checks Bug 2: the goal-area CSS must not cap textareas at 70 px —
    that height is too small to edit multi-line goal content comfortably.
    """
    # Save button IDs for all three goal fields
    assert "save-north-star-" in js, "save-north-star button missing from JS"
    assert "save-goal-" in js, "save-goal (version goal) button missing from JS"
    assert "save-sprint-" in js, "save-sprint button missing from JS"

    # Save async functions for all three
    assert "saveNorthStar" in js
    assert "saveGoal" in js
    assert "saveSprint" in js

    # Bug 2 — textarea height must be usable for multi-line content.
    # 70 px min-height is too small; the fix is ≥ 200 px or calc().
    css_text = client.get("/static/dashboard.css").text
    assert "min-height: 70px" not in css_text, (
        "Bug 2: .goal-area min-height is only 70px — textareas are unreadably "
        "small for editing goal content.  Fix: increase to ≥ 200px or use "
        "calc(100vh - 300px)."
    )


def test_dashboard_has_edit_preview_toggles(js, css):
    """Goal textareas expose edit/preview toggle chips (Bug 4 related).

    Also verifies that ``marked.parse`` is called without the deprecated
    ``mangle`` and ``headerIds`` options that were removed in marked.js v9+.
    Passing them produces a runtime warning and can break preview rendering
    on some CDN builds.
    """
    # Preview toggle CSS classes
    assert "preview-toggle-row" in css, "preview-toggle-row CSS class missing"
    assert "preview-btn" in css, "preview-btn CSS class missing"
    assert "goal-preview" in css, "goal-preview CSS class missing"

    # Preview wiring must exist in JS (wireGoalPreviewToggle is generic helper; file editor wires preview inline)
    assert "wireGoalPreviewToggle" in js, (
        "wireGoalPreviewToggle function missing — it's the generic preview toggle helper used by files tab"
    )
    # File editor must have edit/preview mode toggle
    assert "file-mode-preview-" in js, (
        "File editor edit/preview toggle missing — preview belongs on Files tab, not Goal tab"
    )
    assert "marked.parse" in js, (
        "marked.parse call missing — edit/preview toggle won't render markdown"
    )

    # Bug 4 — marked.js v9+ removed mangle and headerIds.
    # Passing them generates a deprecation warning and may break rendering.
    assert "mangle: false" not in js, (
        "Bug 4: marked.parse uses deprecated `mangle: false` option "
        "(removed in marked.js v9+). Remove the options object from the call."
    )
    assert "headerIds: false" not in js, (
        "Bug 4: marked.parse uses deprecated `headerIds: false` option "
        "(removed in marked.js v9+). Remove the options object from the call."
    )


def test_dashboard_has_timeline_tab(soup, js):
    """The dashboard exposes a TIMELINE vtab and loads it lazily."""
    # vtab markup is generated by JS (buildTabBody)
    assert 'data-vtab="timeline"' in js, (
        "timeline vtab button missing from buildTabBody in dashboard.js"
    )
    assert "loadTimeline" in js, (
        "loadTimeline function missing — timeline data won't load on tab open"
    )
    # The HTML template must load the JS bundle
    scripts = [s.get("src", "") for s in soup.find_all("script")]
    assert any("dashboard.js" in s for s in scripts), (
        "dashboard.html must reference /static/dashboard.js"
    )


def test_dashboard_js_has_github_connect_card(js):
    """dashboard.js includes the GitHub connect card in Settings."""
    assert "github/connect" in js
    assert "Connect GitHub repo" in js


def test_dashboard_goal_tab_has_no_preview_toggle(js):
    """Goal textareas must NOT have the edit/preview chip toggle (Bug 5).

    Goal fields are structured data (north star, version goal, sprint),
    not markdown documents. The preview toggle belongs on the Files tab.
    The goal drawer must use subtab switching, not stacked sections with previews.
    """
    # Goal subtab structure must exist
    assert 'data-gtab="north-star"' in js, "goal north-star subtab missing"
    assert "goal-subtab-strip" in js, "goal subtab strip class missing from JS"
    assert "goal-subtab-btn" in js, "goal subtab button class missing from JS"


def test_dashboard_files_tab_has_preview_toggle(client, js):
    """File editor has edit/preview toggle chips (STEP 1 — preview moves to files).

    The edit/preview Marked.js toggle belongs on the Files tab, where
    files like AGENTS.md and ROADMAP.md are actual markdown documents.
    """
    # File editor mode buttons must be in JS
    assert "file-mode-preview-" in js, (
        "File editor preview mode button ID missing from dashboard.js"
    )
    assert "file-mode-edit-" in js, (
        "File editor edit mode button ID missing from dashboard.js"
    )
    # File preview div must be in JS
    assert "file-preview-" in js, (
        "File preview div ID missing from dashboard.js"
    )
    # Marked.js must still be loaded (used by file preview)
    html = client.get("/dashboard").text
    assert "marked.min.js" in html, "marked.js CDN link must remain in dashboard.html"


def test_dashboard_sidebar_has_no_translatex(client):
    """Left sidebar must be permanently visible on desktop — no translateX hiding (Bug 8).

    On desktop the sidebar grid column is always 280px. No CSS transform
    is used to hide it. A hamburger button appears ONLY on mobile (<768px).
    """
    css = client.get("/static/dashboard.css").text
    # The LEFT sidebar column in .app must be a fixed width (not 0)
    assert "280px" in css, "sidebar 280px column must exist in CSS"
    # Mobile responsive code should exist for <768px
    assert "768px" in css, "mobile breakpoint missing — sidebar must hide on mobile only"
    # The mobile sidebar must use left: -280px (not translateX)
    assert "left: -280px" in css or "left:-280px" in css, (
        "Mobile sidebar must use `left: -280px` for hiding, not translateX"
    )


def test_dashboard_live_tab_exists(client):
    """LIVE vtab (⚡) is registered and wired in dashboard.js (v1.6.x).

    Section A: active sessions (filtered to last 24h) with claimed task
    shown indented per session.  Section B: queue (pending + in_progress
    tasks) with an add-task input and per-row cancel.  Header buttons:
    [Pause] / [Run All] (stubs).  WebSocket-driven — no setInterval.
    """
    js = client.get("/static/dashboard.js").text
    css = client.get("/static/dashboard.css").text
    assert 'data-vtab="live"' in js, "LIVE vtab button missing from buildTabBody"
    assert "drawer-live-" in js, "LIVE drawer panel missing"
    assert "loadLiveTab" in js, "loadLiveTab function missing"
    assert "renderLiveSessions" in js, "renderLiveSessions function missing"
    assert "renderLiveQueue" in js, "renderLiveQueue function missing"
    assert "live-sessions-" in js, "live sessions container ID missing"
    assert "live-queue-" in js, "live queue container ID missing"
    assert "live-add-input-" in js, "add task input ID missing"
    assert "live-pause-" in js, "Pause stub button missing"
    assert "live-run-" in js, "Run All stub button missing"
    # Add task posts to /tasks with status pending
    assert "addLiveTask" in js, "addLiveTask helper missing"
    assert "'/tasks'" in js or '"/tasks"' in js, "POST /tasks not referenced"
    # Sessions older than 24h are filtered out
    assert "24 * 3600 * 1000" in js or "24*3600*1000" in js, (
        "session age > 24h filter missing from LIVE tab"
    )
    # WS handler refreshes the LIVE tab when active
    assert "refreshLiveTab" in js, "refreshLiveTab missing from WS handler"
    # CSS for the new panel
    assert ".live-body" in css, "live-body CSS class missing"
    assert ".live-task-row" in css, "live-task-row CSS class missing"


def test_dashboard_claude_tab_has_session_controls(client):
    """Claude launch panel exposes the 4 control sections (v1.5.x).

    The panel was a single "Open in Claude" CTA; the v1.5.x overhaul splits it
    into: (1) continue-session dropdown + copy-resume command, (2) start-worker
    button that shows worker_context XML, (3) handoff copy + regenerate, and
    (4) open in claude.ai as a narrow secondary action.  All wireup lives in
    dashboard.js and the markup is generated dynamically per project tab.
    """
    js = client.get("/static/dashboard.js").text
    html = client.get("/dashboard").text
    # Section 1 — continue session controls
    assert "continue-session-" in js, "continue session dropdown ID missing"
    assert "copy-resume-" in js, "copy resume command button missing"
    assert "start_session(project_id=" in js, (
        "resume command template (start_session call) must be embedded in JS"
    )
    assert 'get_context_block(project_id="' in js, (
        "resume flow should mention get_context_block for reloading context"
    )
    # Section 2 — worker session
    assert "start-worker-" in js, "start worker button missing"
    assert "copy-worker-" in js, "copy worker context button missing"
    assert "worker_context" in js, "worker_context payload key not referenced"
    assert "/start-worker-session" in js, "start-worker-session endpoint not called"
    # Section 3 — handoff
    assert "copy-handoff-" in js, "copy handoff button missing"
    assert "regen-handoff-" in js, "regenerate handoff button missing"
    assert "Regenerated" in js, "regenerated confirmation message missing"
    assert "/handoff" in js, "handoff controls should call the generate_handoff endpoint"
    # Section 4 — open in Claude (narrow secondary)
    assert "open-in-claude-" in js, "open in Claude button missing"
    assert "claude.ai" in js, "claude.ai destination missing"
    # Generic markers — the new test from the handoff
    text = html.lower() + js.lower()
    assert "resume" in text or "session" in text
    assert "worker" in text
    assert "handoff" in text
    assert "constitution-warning-" in js, "decisions tab should expose a constitution warning host"
    assert "/projects/${projectId}/settings" in js, (
        "dashboard should load persisted per-project settings"
    )


def test_dashboard_open_in_claude_not_dominant(client):
    """The 'Open in Claude' panel must not dominate the layout (Bug 6).

    Goal fields are the primary surface. The Claude handoff panel
    must be a narrow utility strip, not flex:1 competing with goal content.
    """
    css = client.get("/static/dashboard.css").text
    # Claude panel must be fixed narrow width (not flex:1)
    assert ".claude-handoff-panel" in css
    # Check it's not declared as flex:1 (which would make it half the screen)
    # Extract the rule block
    import re
    m = re.search(r'\.claude-handoff-panel\s*\{([^}]+)\}', css)
    assert m, ".claude-handoff-panel CSS rule not found"
    rule = m.group(1)
    assert "flex: 1" not in rule and "flex:1" not in rule, (
        "Bug 6: .claude-handoff-panel has flex:1 — it dominates the layout. "
        "Fix: give it a fixed narrow width (e.g. width: 200px; flex-shrink: 0)."
    )
    # vtab-drawer should now be the dominant panel (flex:1)
    m2 = re.search(r'\.vtab-drawer\s*\{([^}]+)\}', css)
    assert m2, ".vtab-drawer CSS rule not found"
    drawer_rule = m2.group(1)
    assert "flex: 1" in drawer_rule or "flex:1" in drawer_rule, (
        "Bug 6: .vtab-drawer must be flex:1 so goal panel dominates the layout."
    )


def test_dashboard_live_tab_has_progress_bar(client, js):
    """LIVE tab shows a sprint progress bar ([████░░] done/total).

    The bar is rendered by renderSprintProgress() which fetches
    /sprint-items and produces a monospace block-character bar.
    Checks JS source (bar is dynamically rendered, not in static HTML).
    """
    assert "live-sprint-progress-" in js, (
        "sprint progress bar container ID missing from LIVE tab HTML in buildTabBody"
    )
    assert "renderSprintProgress" in js, (
        "renderSprintProgress function missing from dashboard.js"
    )
    assert "sprint-items" in js, (
        "/sprint-items API call missing — progress bar won't load"
    )
    # CSS classes for the bar must exist
    css = client.get("/static/dashboard.css").text
    assert "live-sprint-bar" in css, (
        ".live-sprint-bar CSS class missing"
    )
