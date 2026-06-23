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

from dashboard_src import dashboard_source


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
    # dashboard.js was split into dashboard-*.js modules (v1.1 extraction).
    # Use the raw concatenated source (not the esbuild bundle, which renames
    # vars / escapes unicode) so string + structure assertions stay accurate.
    return dashboard_source()


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
    assert any("dashboard.bundle.js" in s for s in scripts), (
        "dashboard.html must load /static/dashboard.bundle.js"
    )
    links = [l.get("href", "") for l in soup.find_all("link")]
    assert any("dashboard.css" in l for l in links), (
        "dashboard.html must load /static/dashboard.css"
    )


def test_backburner_section_has_grouping_search_and_archive(js):
    """e62ce019 — the backburner section groups by item_group, has a filter box,
    and a per-item permanent-delete (archive) button wired to the DELETE path."""
    # Search box wired to the client-side filter.
    assert "backburner-search-" in js, "backburner search input missing"
    assert "filterBackburner(" in js, "backburner filter wiring missing"
    # Grouping by item_group.
    assert "bb-group" in js, "backburner item_group grouping missing"
    # Per-item archive/delete button (write control → must be demo-hidden).
    assert "sprintArchive(" in js, "backburner archive button missing"
    assert "async function sprintArchive" in js, "sprintArchive impl missing"


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
    assert any("dashboard.bundle.js" in s for s in scripts), (
        "dashboard.html must reference /static/dashboard.bundle.js"
    )


def test_dashboard_js_has_github_connect_card(js):
    """dashboard.js includes the GitHub connect card in Settings."""
    assert "github/connect" in js
    assert "Connect GitHub repo" in js


def test_signout_link_created_unconditionally_for_hosted_users(js, client):
    """Item 42 — Free-tier sign-out regression.

    The sign-out link in .sidebar-footer must be added by hideHostedAdminControls()
    so it appears for ALL hosted users (free + admin), not gated behind /me
    returning a plan. Previously the link creation lived only inside
    _renderPlanBadge(me); if /me errored or returned {} for any reason, the
    link never appeared — the bug the user reported on free tier.
    """
    assert "function ensureSignOutLink" in js, (
        "ensureSignOutLink() helper missing — sign-out link creation must be "
        "factored out of _renderPlanBadge so hideHostedAdminControls can call it."
    )
    # hideHostedAdminControls runs unconditionally for hosted users at init,
    # so calling ensureSignOutLink from there guarantees free-tier coverage.
    hosted_fn_start = js.index("function hideHostedAdminControls")
    hosted_fn_end = js.index("\nfunction ", hosted_fn_start + 1)
    hosted_fn_body = js[hosted_fn_start:hosted_fn_end]
    assert "ensureSignOutLink()" in hosted_fn_body, (
        "hideHostedAdminControls() must call ensureSignOutLink() so the link "
        "appears for free-tier users without waiting on /me."
    )
    # _renderPlanBadge was extracted to dashboard-sprint.js — check it there.
    js_sprint = client.get("/static/dashboard-sprint.js").text
    plan_badge_start = js_sprint.index("function _renderPlanBadge")
    import re as _re
    _next = _re.search(r"\n(?:export )?function ", js_sprint[plan_badge_start + 1:])
    plan_badge_end = (plan_badge_start + 1 + _next.start()) if _next else len(js_sprint)
    plan_badge_body = js_sprint[plan_badge_start:plan_badge_end]
    assert "ensureSignOutLink(me.email)" in plan_badge_body, (
        "_renderPlanBadge must still call ensureSignOutLink(me.email) to update "
        "the tooltip with the signed-in email when /me succeeds."
    )


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


def test_files_vtab_gated_for_hosted_without_repo(client):
    """1332fe4d — the Files vtab is hidden for hosted users with no GitHub repo
    connected (an empty Files tab is dead weight on the hosted dashboard)."""
    js = client.get("/static/dashboard.js").text
    assert 'data-vtab="files"' in js, "Files vtab button missing from buildTabBody"
    # Button must be wrapped in a hosted + github-repo guard.
    assert "MERIDIAN_HOSTED && !(project.github_repo" in js, (
        "Files vtab is not gated behind hosted + github_repo"
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
    assert "sequential-mode-" in js, "sequential mode toggle missing"
    assert "touches-files-warning-" in js, "touches_files warning host missing"
    assert "findTouchesFilesConflicts" in js, "handoff should check touches_files overlap"
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


def test_dashboard_settings_has_os_detection_banner(client):
    js = client.get("/static/dashboard.js").text
    # OS hint lives inside the Meridian Connect tab only (osExecutorHintBanner),
    # not duplicated at the top of settings. The old standalone
    # detectHookInstallOS helper + settings-top banner were removed; the Connect
    # tab banner does its own OS detection.
    assert "osExecutorHintBanner" in js
    assert "settings-os-detection-banner-" not in js


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


def test_dashboard_constants_in_utils(client):
    """DEFAULT_CONTEXT_THRESHOLD and DEFAULT_MAX_PINNED_DECISIONS must be
    defined in dashboard-utils.js so the settings tab can access them as
    globals before dashboard.js finishes initialising (16389f76).
    """
    from pathlib import Path
    utils_src = (
        Path(__file__).parent.parent / "meridian" / "static" / "dashboard-utils.js"
    ).read_text(encoding="utf-8")
    dashboard_src = (
        Path(__file__).parent.parent / "meridian" / "static" / "dashboard.js"
    ).read_text(encoding="utf-8")

    assert "DEFAULT_CONTEXT_THRESHOLD" in utils_src, (
        "DEFAULT_CONTEXT_THRESHOLD must be defined in dashboard-utils.js"
    )
    assert "DEFAULT_MAX_PINNED_DECISIONS" in utils_src, (
        "DEFAULT_MAX_PINNED_DECISIONS must be defined in dashboard-utils.js"
    )
    # Neither constant should be defined (as a const) in dashboard.js anymore
    import re
    assert not re.search(r"^const DEFAULT_CONTEXT_THRESHOLD\s*=", dashboard_src, re.MULTILINE), (
        "DEFAULT_CONTEXT_THRESHOLD must not be re-defined in dashboard.js"
    )
    assert not re.search(r"^const DEFAULT_MAX_PINNED_DECISIONS\s*=", dashboard_src, re.MULTILINE), (
        "DEFAULT_MAX_PINNED_DECISIONS must not be re-defined in dashboard.js"
    )


def test_settings_tab_renderer_is_not_duplicated():
    """Only the settings module may define loadSettingsTab.

    Regression for fix-settings-tab: dashboard.js once defined its own
    loadSettingsTab (Executor Rules only). In the esbuild IIFE bundle it shadowed
    the full-settings module's loadSettingsTab, so opening Settings rendered ONLY
    Executor Rules — every other section vanished. The Executor Rules render now
    lives under the distinct name loadExecutorRulesSection, which the settings
    module appends. Guard against the duplicate name coming back.
    """
    import re
    from pathlib import Path

    static = Path(__file__).parent.parent / "meridian" / "static"
    dashboard_src = (static / "dashboard.js").read_text(encoding="utf-8")
    settings_src = (static / "dashboard-settings.js").read_text(encoding="utf-8")

    # The full settings renderer is defined once, in the module.
    assert re.search(r"function loadSettingsTab\s*\(", settings_src), (
        "dashboard-settings.js must define loadSettingsTab (the full settings renderer)"
    )
    # dashboard.js must NOT define loadSettingsTab (that collision is the bug).
    assert not re.search(r"function loadSettingsTab\s*\(", dashboard_src), (
        "dashboard.js must not define loadSettingsTab — it collides with the "
        "settings module in the bundle and shadows the full settings render"
    )
    # The Executor Rules section lives under its own name and is appended by the module.
    assert re.search(r"function loadExecutorRulesSection\s*\(", dashboard_src), (
        "dashboard.js must define loadExecutorRulesSection (the Executor Rules section)"
    )
    assert "loadExecutorRulesSection" in settings_src, (
        "dashboard-settings.js must append the Executor Rules section via "
        "window.loadExecutorRulesSection so the full settings tab includes it"
    )


def test_known_locations_has_manual_path_entry(js):
    """Item a89bb60d — Known Locations card has a manual path-entry form (cwd +
    hostname inputs + Add button) that merges into executor_config.repo_paths and
    persists via GET-merge-PATCH."""
    # Inputs + Add button present in the rendered card.
    assert "exec-ez-add-cwd-" in js, "manual cwd input missing"
    assert "exec-ez-add-host-" in js, "manual hostname input missing"
    assert "exec-ez-add-btn-" in js, "Add button missing"
    # Hostname input pre-fills from registered machines (datalist of known hosts).
    assert "exec-ez-host-options-" in js, "hostname datalist (registered machines) missing"
    # Add handler GETs settings, merges into repo_paths, and PATCHes back.
    assert "_doAddPath" in js, "Add handler missing"
    assert "cfg.repo_paths = paths" in js, "Add handler must merge into repo_paths"
    assert "saveProjectSettings(projectId, { executor_config: cfg })" in js, (
        "Add handler must persist via saveProjectSettings (PATCH settings)"
    )


def test_codeintel_tab_sends_project_slug_not_repo_path(js):
    """HOTFIX 9d11c952 — the Code Intel tab must call index_status / get_architecture
    with the `project` slug the code-intel graph keys on (root_path with :/\\ collapsed
    to dashes, e.g. C-Users-13144-Documents-Meridian-repository), NOT a raw `repo_path`.
    The backend tools require `project`; sending `repo_path` made the lookup a no-op."""
    import re
    # The slug helper exists and collapses drive-colon + path separators to dashes.
    assert "_repoPathToProject" in js, "repo-path -> project slug helper missing"
    assert re.search(r"_repoPathToProject\s*\([^)]*\)\s*\{[^}]*\[\\\\/:\]", js), (
        "_repoPathToProject must collapse [\\\\/:] runs to dashes"
    )
    # Both tool calls send the derived `project`, and neither still sends `repo_path`.
    assert "name: 'index_status', arguments: {project: _repoPathToProject(" in js, (
        "index_status must be called with project slug, not repo_path"
    )
    assert "name: 'get_architecture', arguments: archArgs" in js
    assert "{project: _repoPathToProject(archPath)}" in js, (
        "get_architecture must be called with project slug, not repo_path"
    )
    assert "index_status', arguments: {repo_path:" not in js, (
        "stale repo_path arg still sent to index_status"
    )


def test_tunnel_plugins_section_exposed_on_window(js):
    """Regression (9d03d7cc): loadTunnelPluginsSection must be on window so the
    settings module's window.loadTunnelPluginsSection?.() call actually renders it."""
    import re
    # It's invoked via window.* from the settings module …
    assert "window.loadTunnelPluginsSection" in js
    # … so it MUST be in the Object.assign(window, {...}) export, else the call no-ops.
    assert re.search(r"Object\.assign\(window,\s*\{[^}]*\bloadTunnelPluginsSection\b", js), (
        "loadTunnelPluginsSection missing from the window export — settings call would no-op"
    )


def test_tunnel_plugins_section_plan_gated_and_collapsible(js):
    """9f40cb60 enhance: the card is Pro/admin-gated and rendered as a collapsible."""
    # Plan gate: bail out for non-pro/admin.
    assert "plan === 'pro' || plan === 'admin'" in js
    # Collapsible <details> card.
    assert "<details class=\"meridian-disclosure\" open" in js or \
           "<details class='meridian-disclosure' open" in js


def test_tunnel_plugins_section_has_ux_enhancements(js):
    """Sprint bca73c3f — the Tunnel Plugins card gains four UX sub-features:
    (1) explicit reset-confirm dialog, (2) per-plugin live tools dropdown,
    (3) OS-detected dependency-install cards, (4) a curated installable list."""
    import re

    # (4) Curated installable plugin list — a module-level constant with real,
    # well-known MCP servers (name + command + description + docs).
    assert "_CURATED_TUNNEL_PLUGINS" in js, "curated plugin constant missing"
    assert "uvx mcp-server-fetch" in js, "curated 'Fetch' command missing"
    assert "@modelcontextprotocol/server-sequential-thinking" in js, (
        "curated 'Sequential Thinking' command missing"
    )
    # Rendered with a copy-to-clipboard action.
    assert "navigator.clipboard" in js, "clipboard copy not wired"

    # (2) Per-plugin live tools dropdown — JSON-RPC tools/list against the slot's
    # MCP proxy at /<slot>/mcp/<tenantId>/mcp, parsed JSON-or-SSE like the code tab.
    assert "method: 'tools/list'" in js, "tools/list JSON-RPC call missing"
    assert re.search(r"`/\$\{slot\}/mcp/\$\{tenantId\}/mcp`", js), (
        "per-slot MCP proxy URL (/<slot>/mcp/<tenantId>/mcp) not constructed"
    )
    # Tenant id sourced from /me (reused across slots), and the live tool names
    # rendered from result.tools.
    assert "api('/me')" in js, "tenant id must come from /me"
    assert "result.tools" in js or "result && parsed.result.tools" in js or \
           "parsed.result && parsed.result.tools" in js, "tool list not read from result.tools"
    # Graceful 'not connected' state when the slot isn't live.
    assert "not connected — start the tunnel" in js, "missing inactive-slot message"

    # (3) OS-detected install command cards — navigator-based detection + the
    # winget / brew install one-liners for uv and Node.js.
    assert "_detectTunnelOs" in js, "OS detection helper missing"
    assert "navigator.userAgent" in js and "navigator.platform" in js, (
        "OS detection must read navigator.userAgent / navigator.platform"
    )
    assert "winget install --id=astral-sh.uv -e" in js, "Windows uv install cmd missing"
    assert "winget install OpenJS.NodeJS -e" in js, "Windows Node install cmd missing"
    assert "brew install uv" in js and "brew install node" in js, "macOS install cmds missing"
    assert "https://astral.sh/uv/install.sh" in js, "Linux uv install cmd missing"

    # (1) Reset still guards with a confirm() dialog (not regressed).
    assert "confirm(" in js, "reset confirmation dialog regressed"
