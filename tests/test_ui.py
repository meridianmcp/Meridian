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
    """The north-star textarea reads ``goal.north_star`` — not ``goal.content``.

    Bug 1 screenshot: both north-star and version-goal showed identical text
    because ``refreshGoal`` was setting both textareas from ``goal.content``.
    This test verifies the JS reads the correct field for each textarea.
    """
    # north_star field must be populated from goal.north_star, not goal.content
    assert "nsTA.value = goal.north_star" in js, (
        "Bug 1: refreshGoal must set north-star textarea from goal.north_star, "
        "not goal.content."
    )
    # The north-star element must have its own distinct ID prefix
    assert "goal-north-star-" in js, (
        "north-star textarea must have a distinct id prefix 'goal-north-star-'"
    )
    # Version goal uses goal.content (the API field name) — correct behaviour
    # Confirm version-goal uses a DIFFERENT id prefix to north-star
    assert '`goal-${' in js or "goal-${project.id}" in js, (
        "version-goal textarea must use id prefix 'goal-' (without 'north-star')"
    )
    # Sprint must also have its own id
    assert "goal-sprint-" in js, (
        "sprint textarea must have a distinct id prefix 'goal-sprint-'"
    )
    assert "spTA.value = goal.sprint" in js, (
        "refreshGoal must set sprint textarea from goal.sprint"
    )


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

    # Preview wiring must exist in JS
    assert "wireGoalPreviewToggle" in js, (
        "wireGoalPreviewToggle function missing from dashboard.js"
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
