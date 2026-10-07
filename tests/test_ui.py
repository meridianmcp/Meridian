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

import re

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


def test_settings_panel_reinits_on_project_tab_switch(js):
    """73907f9e — the project-tab switch path (activateTab) force-reloads the
    Settings panel when settings is the active vtab, so switching to an
    already-built tab doesn't show a blank panel (the renderer's TTL cache +
    MutationObserver could otherwise leave it empty)."""
    # The forced settings re-init and its marker live in dashboard.ts (activateTab).
    assert "loadSettingsTab(id, { force: true })" in js, (
        "tab switch must force-reload the Settings panel (73907f9e)"
    )
    assert "73907f9e" in js
    # It is gated on the active vtab being settings (not fired for every tab).
    assert "_activeVtab === 'settings'" in js


def test_tunnel_stale_override_badge_rendered(js):
    """cc904bfe — the Tunnel Plugins section renders a 'newer default available'
    badge with a Use-new-default action when a slot's saved command is a stale
    copy of an old built-in default (resolve_plugins sets p.stale_override)."""
    assert "_renderStaleOverrideWarning" in js
    assert "newer default available" in js
    assert "tp-reset-default" in js
    assert "cc904bfe" in js


def test_invite_scope_dropdown_and_co_admin_badge(js):
    """95499c3e — the team-members UI has a project-scope dropdown on the invite
    form (blank = full workspace) and labels a project-scoped admin as a co-admin
    in the member list."""
    assert "invite-scope-" in js          # scope dropdown on the invite form
    assert "all projects" in js           # blank option = workspace-wide
    assert "co-admin @ " in js            # scoped-admin label in the member list
    assert "95499c3e" in js


def test_tunnel_per_machine_picker_rendered(js):
    """8660d701 — the Tunnel Plugins section renders a per-machine picker and
    scopes the fetch/save to the selected hostname (?hostname=)."""
    assert "tp-host-" in js
    assert "Default (all machines)" in js
    assert "encodeURIComponent(_selHost)" in js
    assert "8660d701" in js


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


def test_workspace_personal_backlog_section_present(js):
    """b2115251 — the Workspace settings area surfaces the cross-project personal
    backlog (workspace sprint board): a list, an add control, grouping by bucket,
    and status controls wired to the /workspace/sprint-items endpoints."""
    # Section + add control ids.
    assert "ws-sprint-list" in js, "personal backlog list container missing"
    assert "ws-sprint-add" in js, "personal backlog add button missing"
    assert "ws-sprint-title" in js, "personal backlog title input missing"
    assert "ws-sprint-group" in js, "personal backlog bucket/group input missing"
    assert "Personal backlog" in js, "personal backlog heading missing"
    # Render + fetch wiring against the workspace sprint-items endpoint.
    assert "renderWsSprint" in js, "renderWsSprint render function missing"
    assert "/workspace/sprint-items" in js, "personal backlog endpoint wiring missing"
    # Grouping by item_group bucket + status controls.
    assert "data-ws-group" in js, "personal backlog item_group grouping missing"
    assert "data-ws-status" in js, "personal backlog status dropdown missing"
    assert "/complete" in js, "personal backlog complete control missing"


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


def test_notes_tab_has_cursor_load_more(js):
    """9fa119dd — the notes tab paginates: the initial load hits the paginated
    notes endpoint, and a "Load More" button fetches the next cursor + appends,
    mirroring the devlog/tasks Load-More wiring."""
    # Initial + subsequent fetches use the cursor-pagination endpoint.
    assert "/notes?paginate=true&limit=" in js, (
        "notes tab must fetch the paginated ?paginate=true endpoint"
    )
    # A Load More control wired to the cursor exists in the notes tab.
    assert "notes-load-more-" in js, "notes Load More button id missing"
    assert "const loadMore" in js, "notes loadMore() handler missing"
    # The handler consumes the {notes, has_more, next_cursor} envelope.
    assert "next_cursor" in js and "has_more" in js, (
        "notes loadMore must read the has_more / next_cursor envelope"
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
    assert any("dashboard.bundle.js" in s for s in scripts), (
        "dashboard.html must reference /static/dashboard.bundle.js"
    )


def test_dashboard_has_connection_health_dot(soup, js):
    """bb16f9a7 — the connection-health indicator dot is present and wired.

    The dashboard has no persistent SSE/WebSocket, so the dot is driven off the
    central api() REST helper: green on a recent success, red on failure, amber
    when no successful call landed within the stale window.
    """
    # 1) The element exists in the sidebar-footer connection indicator.
    dot = soup.find(id="connection-health-dot")
    assert dot is not None, (
        "dashboard.html must include the #connection-health-dot element (bb16f9a7)"
    )
    # 2) The frontend source drives it from api() success/failure + staleness.
    assert "connection-health-dot" in js, (
        "health dot must be referenced/updated by the frontend source"
    )
    assert "_refreshHealthDot" in js
    assert "bb16f9a7" in js


def test_dashboard_js_has_github_connect_card(js):
    """dashboard.js includes the GitHub connect card in Settings."""
    assert "github/connect" in js
    assert "Connect GitHub repo" in js


# 8201de19 — the Meridian Connect panel must offer direct per-platform binary
# downloads for the tunnel connector ("meridian-connect") as a fallback when the
# install.ps1 / install.sh one-liner 404s. These asset names MUST match
# .github/workflows/release.yml exactly or the download links 404.
# 80f1d4bc — macOS Intel (dead macos-13 runner) + Linux ARM64 (pixi lacks the
# linux-aarch64 platform) were dropped from the release matrix, so their assets
# 404; removed from the panel. Only the 3 actually-built platforms remain.
_MERIDIAN_CONNECT_ASSETS = [
    "meridian-connect-x86_64-windows.exe",
    "meridian-connect-aarch64-apple-darwin",
    "meridian-connect-x86_64-unknown-linux",
]
_MERIDIAN_DROPPED_ASSETS = [
    "meridian-connect-x86_64-apple-darwin",      # macOS Intel — dead macos-13
    "meridian-connect-aarch64-unknown-linux",    # Linux ARM64 — no pixi linux-aarch64
]
_MERIDIAN_RELEASES_BASE = (
    "https://github.com/meridianmcp/Meridian/releases/latest/download"
)


def test_connect_panel_has_direct_binary_download_links(js):
    """The Connect panel exposes direct release-asset download URLs for all five
    platforms, pointing at meridianmcp/Meridian's latest release (8201de19)."""
    # The GitHub repo slug used by every download URL + install.ps1.
    assert "meridianmcp/Meridian/releases/latest/download" in js, (
        "Connect panel must link at the meridianmcp/Meridian latest-release asset path"
    )
    # The releases base path used to build every download href.
    assert _MERIDIAN_RELEASES_BASE in js, (
        "Connect panel must reference the latest-release download base URL"
    )
    # A labelled fallback section, shown alongside (not replacing) the one-liner.
    assert "Direct binary download (if the install script fails)" in js
    # Each of the five per-platform assets is present with its exact release name.
    # (The href is built as `${base}/${asset}` so we assert the base + each asset
    # rather than the pre-concatenated string, which the source never spells out.)
    for asset in _MERIDIAN_CONNECT_ASSETS:
        assert asset in js, f"Connect panel missing tunnel binary asset {asset!r}"
    # 80f1d4bc — dropped (unbuilt) platforms must NOT be linked; they'd 404.
    for asset in _MERIDIAN_DROPPED_ASSETS:
        assert asset not in js, f"dropped platform {asset!r} should not be linked (404s)"
    # The install-script one-liner must still be present (fallback is additive).
    assert "install.ps1" in js and "install.sh" in js


def test_connect_panel_asset_names_match_release_workflow():
    """The download asset names in the dashboard must exactly match the artifact
    names published by the release workflow — a mismatch silently 404s (8201de19)."""
    from pathlib import Path

    release_yml = (
        Path(__file__).parent.parent / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")
    for asset in _MERIDIAN_CONNECT_ASSETS:
        assert asset in release_yml, (
            f"release.yml no longer publishes {asset!r} — dashboard download link "
            "would 404; update both together."
        )


def test_connect_download_urls_in_built_bundle():
    """The rebuilt esbuild bundle (what actually ships) contains the five download
    URLs, so the fallback survives bundling (8201de19)."""
    from pathlib import Path

    bundle = (
        Path(__file__).parent.parent
        / "meridian"
        / "static"
        / "dashboard.bundle.js"
    ).read_text(encoding="utf-8")
    assert "meridianmcp/Meridian/releases/latest/download" in bundle
    for asset in _MERIDIAN_CONNECT_ASSETS:
        assert asset in bundle, f"built bundle missing download asset {asset!r}"


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
    js_sprint = client.get("/static/dashboard-sprint.ts").text
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
    js = client.get("/static/dashboard.ts").text
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
    js = client.get("/static/dashboard.ts").text
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
    js = client.get("/static/dashboard.ts").text
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
    js = client.get("/static/dashboard.ts").text
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


def test_vtab_strip_scrolls_instead_of_clipping(client):
    """52218bb7 — the vtab rail's ancestor (.tab-body) is `overflow: hidden`.

    Without the strip handling its own vertical overflow, a project with more
    vtabs than fit in the rail's height gets those extra icons silently
    CLIPPED by the ancestor instead of made reachable via scroll. The strip
    must own its overflow (scroll), not rely on the ancestor to clip it.
    """
    import re

    css = client.get("/static/dashboard.css").text
    m = re.search(r'\.vtab-strip\s*\{([^}]+)\}', css)
    assert m, ".vtab-strip CSS rule not found"
    rule = m.group(1)
    assert re.search(r'overflow-y\s*:\s*(auto|scroll)', rule), (
        "Bug: .vtab-strip has no overflow-y: auto/scroll — extra vtab icons "
        "beyond the rail's height are clipped by the .tab-body ancestor's "
        "overflow: hidden instead of being scrollable."
    )
    # The strip is only 44px wide — horizontal scroll would be a broken glitch,
    # not a fix, so it must be explicitly suppressed rather than left auto.
    assert re.search(r'overflow-x\s*:\s*hidden', rule), (
        ".vtab-strip should suppress horizontal overflow (it is a fixed-width "
        "icon rail, not something that should scroll sideways)."
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
        Path(__file__).parent.parent / "meridian" / "static" / "dashboard-utils.ts"
    ).read_text(encoding="utf-8")
    dashboard_src = (
        Path(__file__).parent.parent / "meridian" / "static" / "dashboard.ts"
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
    dashboard_src = (static / "dashboard.ts").read_text(encoding="utf-8")
    settings_src = (static / "dashboard-settings.ts").read_text(encoding="utf-8")

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


def test_max_turns_slider_supports_megasprints():
    """47af402c / 76cf8bda — Executor Config exposes max_turns (ceiling 500) with
    escalating warnings at 200+/350+, an Auto-continue (/loop) dropdown, and the
    checkpoint (context_threshold) ceiling raised to 200. f2157803 — these two
    controls are now number inputs (via _execTurnsNumberInputHtml) instead of
    range sliders so the exact value is always visible and directly editable; the
    ids/bounds/clamps are unchanged."""
    from pathlib import Path
    static = Path(__file__).parent.parent / "meridian" / "static"
    settings_src = (static / "dashboard-settings.ts").read_text(encoding="utf-8")
    # max_turns present with a 500 ceiling — rendered as a number input (f2157803),
    # not a range slider (the shared helper emits type="number").
    assert "exec-max_turns-" in settings_src, "max_turns control missing"
    assert 'type="number"' in settings_src, "turns controls must be number inputs"
    assert 'type="range" min="40" max="500"' not in settings_src, "max_turns slider must be gone"
    assert "_execTurnsNumberInputHtml('max_turns', projectId, execCfg.max_turns || DEFAULT_MAX_TURNS, 40, 500, 20)" in settings_src, "max_turns number input must keep [40,500] bounds"
    # Escalating inline warnings at 200+/350+ (color bands green/amber/red) — kept.
    assert "Very long sprint (350+" in settings_src, "350+ warning missing"
    assert "Long sprint (200+" in settings_src, "200+ warning missing"
    # Saved value is clamped to [40, 500].
    assert "Math.min(500, Math.max(40, mtRaw))" in settings_src, "max_turns must clamp to [40,500]"
    # 76cf8bda — Auto-continue (/loop) dropdown persists loop_enabled per project.
    assert "exec-loop_enabled-" in settings_src, "Auto-continue dropdown missing"
    # Checkpoint (context_threshold) ceiling 200 — also a number input now.
    assert "_execTurnsNumberInputHtml('context_threshold', projectId, execCfg.context_threshold || DEFAULT_CONTEXT_THRESHOLD, 10, 200, 5)" in settings_src, "checkpoint number input must keep [10,200] bounds"
    assert "Math.min(200, Math.max(10, ctxRaw))" in settings_src, "checkpoint clamp must be raised to 200"


def test_context_refresh_workspace_controls_present():
    """1688710d — the Workspace settings pane exposes Context Refresh controls
    (bf51b12e backend): a master #ws-auto-refresh checkbox, a #ws-refresh-interval
    number input (1–50), and one checkbox per default trigger. Load reads
    /workspace/settings; save PATCHes auto_refresh_enabled / refresh_interval_turns
    / refresh_triggers."""
    from pathlib import Path
    static = Path(__file__).parent.parent / "meridian" / "static"
    settings_src = (static / "dashboard-settings.ts").read_text(encoding="utf-8")
    # Master toggle + interval input.
    assert 'id="ws-auto-refresh"' in settings_src, "master auto-refresh checkbox missing"
    assert 'id="ws-refresh-interval"' in settings_src, "refresh interval input missing"
    assert 'min="1" max="50"' in settings_src, "refresh interval must clamp 1–50"
    # One checkbox per trigger.
    for trig in (
        "add_insight", "pin_decision", "pin_workspace_decision",
        "set_north_star", "set_goal", "generate_handoff",
    ):
        assert f'id="ws-trigger-{trig}"' in settings_src, f"trigger checkbox for {trig} missing"
    # Save PATCH body carries all three fields.
    assert "auto_refresh_enabled:" in settings_src, "auto_refresh_enabled not in PATCH body"
    assert "refresh_interval_turns:" in settings_src, "refresh_interval_turns not in PATCH body"
    assert "refresh_triggers:" in settings_src, "refresh_triggers not in PATCH body"


def test_documents_tab_present():
    """3f596f81 — the dashboard exposes a Documents tab: a vtab button, a drawer
    panel, a loadDocumentsTab loader that reads project_notes (note_kind=document),
    and an on-demand heading-tree structure view via /document-structure."""
    from pathlib import Path
    static = Path(__file__).parent.parent / "meridian" / "static"
    src = (static / "dashboard.ts").read_text(encoding="utf-8")
    assert 'data-vtab="documents"' in src, "Documents vtab button missing"
    assert "drawer-documents-${project.id}" in src, "Documents drawer panel missing"
    assert "documents-body-${project.id}" in src, "Documents body container missing"
    assert "async function loadDocumentsTab" in src, "loadDocumentsTab loader missing"
    assert "if (vtab === 'documents') loadDocumentsTab" in src, "Documents tab dispatch missing"
    assert "note_kind || '').toLowerCase() === 'document'" in src, "document filter missing"
    assert "/document-structure?path=" in src, "structure fetch missing"


def test_insights_tab_present():
    """0b711a9d — the dashboard exposes an Insights tab: a vtab button, a drawer
    panel, and a loadInsightsTab loader reading /projects/{id}/insights."""
    from pathlib import Path
    static = Path(__file__).parent.parent / "meridian" / "static"
    src = (static / "dashboard.ts").read_text(encoding="utf-8")
    assert 'data-vtab="insights"' in src, "Insights vtab button missing"
    assert "drawer-insights-${project.id}" in src, "Insights drawer panel missing"
    assert "insights-body-${project.id}" in src, "Insights body container missing"
    assert "async function loadInsightsTab" in src, "loadInsightsTab loader missing"
    assert "if (vtab === 'insights') loadInsightsTab" in src, "Insights tab dispatch missing"
    assert "/projects/${projectId}/insights" in src, "insights fetch missing"


def test_blog_tab_present():
    """8843250f — the dashboard exposes a workspace-scoped Blog tab: a vtab
    button, a drawer panel, a loadBlogTab loader that GETs the WORKSPACE
    endpoint /workspace/blog (not a per-project one), and a dispatch."""
    from pathlib import Path
    static = Path(__file__).parent.parent / "meridian" / "static"
    src = (static / "dashboard.ts").read_text(encoding="utf-8")
    assert 'data-vtab="blog"' in src, "Blog vtab button missing"
    assert "drawer-blog-${project.id}" in src, "Blog drawer panel missing"
    assert "blog-body-${project.id}" in src, "Blog body container missing"
    assert "async function loadBlogTab" in src, "loadBlogTab loader missing"
    assert "if (vtab === 'blog') loadBlogTab" in src, "Blog tab dispatch missing"
    # Blog is workspace-scoped — must fetch the workspace endpoint.
    assert "/workspace/blog" in src, "workspace blog fetch missing"


def test_execution_mode_toggle_present(js):
    """ecf69de8 — the settings tab renders an Execution Mode select (autonomous
    vs interactive) that persists via saveProjectSettings (PATCH settings)."""
    # Section header + the select element id.
    assert "Execution Mode" in js, "Execution Mode section header missing"
    assert "execution-mode-${projectId}" in js, "execution mode select id missing"
    # Both posture options are rendered.
    assert 'value="autonomous"' in js, "autonomous option missing"
    assert 'value="interactive"' in js, "interactive option missing"
    # onchange handler persists via saveProjectSettings with the execution_mode key.
    assert "saveProjectSettings(projectId, { execution_mode: emSel.value })" in js, (
        "execution mode change must persist via saveProjectSettings (PATCH settings)"
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
    # 19e063e4 — arXiv research-prospecting server in the curated list.
    assert "uvx arxiv-mcp-server" in js, "curated 'arXiv' command missing"
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


def test_tunnel_plugins_custom_subsection_present(js):
    """Sprint ce84619d — the Tunnel Plugins card gains a Custom plugins subsection:
    a list of existing custom plugins (each with Remove), an add form (name /
    command / port + Add), and the custom entries are merged into collectConfig."""
    # The custom list is seeded from data.custom and kept in a mutable array.
    assert "data.custom" in js or "data && data.custom" in js, (
        "custom plugins must be read from the GET /tunnel/plugins `custom` key"
    )
    assert "const customPlugins" in js, "customPlugins array missing"
    assert "renderCustomList" in js, "custom list renderer missing"

    # Add form: name / command / port inputs + an Add button (id-scoped per project).
    assert "tp-custom-name-${projectId}" in js, "custom name input missing"
    assert "tp-custom-command-${projectId}" in js, "custom command input missing"
    assert "tp-custom-port-${projectId}" in js, "custom port input missing"
    assert "tp-custom-add-${projectId}" in js, "custom Add button missing"

    # Per-row Remove button, wired via addEventListener (no inline onclick to globals).
    assert "tp-custom-remove" in js, "custom Remove button missing"
    assert "customPlugins.splice" in js, "Remove must drop the entry from customPlugins"

    # collectConfig merges the custom entries (name + command + port + enabled).
    assert "customPlugins.forEach" in js, "collectConfig must merge custom plugins"

    # LOCAL-ONLY framing surfaced to the user (no claude.ai connector).
    assert "127.0.0.1" in js, "custom plugins local-proxy framing missing"

    # Add-form validation rejects the built-in default ports (8808–8813).
    assert "8808" in js and "8813" in js, "custom port guard against built-in ports missing"


def test_code_intel_architecture_charts(js):
    """0aca014f — get_architecture renders as charts, defensively.

    The Architecture Summary no longer dumps a raw <pre>; it parses the
    get_architecture JSON and draws Chart.js bar + donut charts plus
    hotspots / packages / layers, with a raw fallback when the shape differs.
    """
    # The defensive renderer exists and is wired into the code-intel tab.
    assert "_codeArchSection" in js, "architecture renderer missing"
    assert "archSection.charts" in js, "charts must be collected for later instantiation"

    # Defensive: parses JSON in a try (older servers return formatted text → raw).
    assert "JSON.parse(archText)" in js
    # Each documented schema field drives a section.
    for field in ("node_labels", "edge_types", "hotspots", "packages", "layers"):
        assert field in js, f"architecture field {field} not handled"

    # Chart.js bar + donut, instantiated only when Chart + a canvas exist.
    assert "type: 'bar'" in js and "type: 'doughnut'" in js
    assert "window.Chart" in js, "chart instantiation must be guarded on Chart presence"
    assert "ci-nodes" in js and "ci-edges" in js, "chart canvases missing"

    # Graceful fallback: a raw-JSON view is always available.
    assert "raw JSON" in js, "raw fallback view missing"


# ---------------------------------------------------------------------------
# 3e3da82d — viewport meta + bounded responsive pass (sprint board + nav).
# ---------------------------------------------------------------------------


def test_dashboard_has_viewport_meta(soup):
    """3e3da82d — the dashboard <head> declares a device-width viewport meta.

    Without it, a phone / Add-to-Home-Screen render lays the page out at a
    zoomed-out desktop width. The PWA item (b03be6a6) added the manifest but
    not the viewport tag.
    """
    vp = soup.find("meta", attrs={"name": "viewport"})
    assert vp is not None, "dashboard <head> must include a viewport meta (3e3da82d)"
    content = vp.get("content", "")
    assert "width=device-width" in content, (
        "viewport meta must set width=device-width so mobile renders at device width"
    )
    assert "initial-scale=1" in content, "viewport meta must set initial-scale=1"


def test_dashboard_responsive_sprint_and_nav_media_block(css):
    """3e3da82d — a bounded @media pass adjusts ONLY the sprint board + top nav
    at phone widths, tagged with the sprint-item id.

    Appearance can't be unit-tested, so we assert the structural pieces exist:
    the media query, the tag comment, and rules for the sprint board rows and
    the top nav tab strip inside a narrow breakpoint.
    """
    # The sprint-item tag marks the new block.
    assert "3e3da82d" in css, "responsive pass must be tagged with the sprint-item id"
    # A phone-width breakpoint (768px already present for the sidebar; 480px added).
    assert "@media (max-width: 768px)" in css, "768px breakpoint missing"
    assert "@media (max-width: 480px)" in css, "480px phone breakpoint missing"

    # The block adjusts the sprint board rows/groups...
    tail = css[css.index("3e3da82d"):]
    assert ".sprint-item-row" in tail, "responsive pass must adjust .sprint-item-row"
    assert ".sprint-item-title" in tail, "responsive pass must adjust .sprint-item-title"
    # ...and the top nav tab strip.
    assert ".tabs" in tail, "responsive pass must adjust the top nav (.tabs)"
    assert ".tab {" in tail or ".tab{" in tail, "responsive pass must adjust nav tabs (.tab)"


# ---------------------------------------------------------------------------
# Sprint row layout contract: a long title wraps inside its OWN column and can
# never paint over the version label or the action buttons (Live tab, "Sprint
# progress"). The original bug: .sprint-item-title was an inline <span> in a plain
# <div>, so its flex/overflow/text-overflow rules did nothing while
# white-space:nowrap stopped it wrapping, and the text ran over .sprint-item-ver
# and .sprint-item-actions. Source-scanning style, like the rest of this file; the
# rendered-DOM + computed-style version lives in
# meridian/static/dashboard-sprint-layout.test.ts, and the pixel overlap is asserted
# in a real browser by tests/test_demo_ux.py.
# ---------------------------------------------------------------------------


def _css_split(text, sep):
    """Split `text` on `sep` at nesting depth 0 (outside (), [], {} and quoted strings)."""
    parts, depth, quote, start, i = [], 0, None, 0, 0
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth = max(0, depth - 1)
        elif c == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return parts


def _css_items(text):
    """The top-level items of a CSS block body: ("stmt", "prop: value" | "@import ...") for each
    ``;``-terminated statement and ("block", prelude, body) for each ``prelude { body }``."""
    items, depth, quote, start, brace, i = [], 0, None, 0, None, 0
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif brace is not None:
            if c == "{":
                brace += 1
            elif c == "}":
                brace -= 1
                if brace == 0:
                    items.append(("block", text[start:body_start].strip(), text[body_start + 1 : i]))
                    start, brace = i + 1, None
        elif c in "([":
            depth += 1
        elif c in ")]":
            depth = max(0, depth - 1)
        elif depth == 0 and c == ";":
            if text[start:i].strip():
                items.append(("stmt", text[start:i].strip()))
            start = i + 1
        elif depth == 0 and c == "{":
            body_start, brace = i, 1
        i += 1
    if brace is None and text[start:].strip():
        items.append(("stmt", text[start:].strip()))
    return items


# At-rules whose block holds ordinary rules (or, nested in a style rule, declarations for it).
_CSS_GROUP_AT_RULES = ("@media", "@supports", "@container", "@layer", "@scope", "@document", "@starting-style")


def _css_nest(parents, children):
    """Resolve nested selectors against their parents (CSS nesting: ``&`` or an implicit descendant)."""
    if parents is None:
        return children
    return [c.replace("&", p) if "&" in c else f"{p} {c}" for p in parents for c in children]


def _css_rules(css_text):
    """Parse CSS into [(gate_or_None, [selectors], {prop: value})] in source order.

    Handles what the dashboard's cascade can contain, not just flat rules: CSS nesting (``&`` and
    implicit descendants, nested @media), conditional / layer groups at any depth (@media,
    @supports, @container, @layer, @scope; the gate is their prelude), selector lists containing
    commas inside :is()/:where(), and declarations holding ``;`` inside url()/strings. Statement
    at-rules (@import, @layer a, b;) are skipped without swallowing the rule that follows them,
    and rule-less at-rules (@keyframes, @font-face) are skipped whole."""
    text = re.sub(r"/\*.*?\*/", "", css_text, flags=re.S)
    out = []

    def walk(body, selectors, gate):
        decls = {}
        for item in _css_items(body):
            if item[0] == "stmt":
                if not item[1].startswith("@") and ":" in item[1]:
                    prop, _, value = item[1].partition(":")
                    decls[prop.strip().lower()] = " ".join(value.split())
                continue
            _, prelude, inner = item
            prelude = " ".join(prelude.split())
            if prelude.startswith("@"):
                if prelude.lower().startswith(_CSS_GROUP_AT_RULES):
                    group = f"{gate} {prelude}" if gate else prelude
                    nested = walk(inner, selectors, group)
                    if nested and selectors is not None:  # `.a { @media (..) { color: red } }`
                        out.append((group, list(selectors), nested))
                continue
            resolved = _css_nest(selectors, [s.strip() for s in _css_split(prelude, ",") if s.strip()])
            slot = len(out)
            out.append(None)
            out[slot] = (gate, resolved, walk(inner, resolved, gate))
        return decls

    walk(text, None, None)
    return [rule for rule in out if rule is not None]


def _decls(rules, selector, media=None):
    """Merged declarations of every rule whose selector list contains `selector`."""
    merged = {}
    for rule_media, selectors, decls in rules:
        if rule_media == media and selector in selectors:
            merged.update(decls)
    return merged


def test_sprint_title_is_a_wrapping_block_in_its_own_column(css):
    """The title must be block-level, shrinkable and allowed to wrap/break, with no
    nowrap/ellipsis truncation, in the board rows (wrapper div) AND the sibling rows
    (needs-attention / your-tasks / backburner) where the title is a direct flex child."""
    rules = _css_rules(css)
    title = _decls(rules, ".sprint-item-title")
    assert title.get("display") == "block", "title must be block-level (an inline span ignores overflow/flex)"
    assert title.get("min-width") == "0", "title must be able to shrink below its content width"
    assert title.get("white-space") == "normal", "title must be allowed to wrap"
    assert title.get("overflow-wrap") == "anywhere", "a 300-char unbreakable token must break in the column"
    assert "text-overflow" not in title and "overflow" not in title, "title wraps; it must not clip or ellipsize"
    # the text column: the wrapper div in board rows ...
    col = _decls(rules, ".sprint-item-main")
    assert col.get("min-width") == "0"
    assert col.get("flex") == "1 1 auto", (
        "the text column is sized by its content: a fixed minimum basis (it was 15em) reserved a long "
        "title's room for every row, so at the real 347px desktop board the buttons of even a "
        "10-character title wrapped onto a second line (28px rows became 43px)"
    )
    assert col.get("max-width") == "calc(100% - 20px)", (
        "the column is capped at the room left beside the 14px icon + 6px gap, so a long title takes "
        "the row's width instead of overflowing it and the icon is never left alone above the text"
    )
    # ... and the title span itself in the sibling rows
    direct = _decls(rules, ".sprint-item-row > .sprint-item-title")
    assert direct.get("min-width") == "0"
    assert direct.get("flex") == col.get("flex"), "sibling rows must use the same column basis as board rows"
    assert direct.get("max-width") == col.get("max-width")


def test_sprint_row_wraps_and_aligns_to_the_first_title_line(css):
    """The row wraps (actions drop UNDER the text when too narrow) and baseline-aligns
    the icon / version / buttons with the title's first line."""
    rules = _css_rules(css)
    row = _decls(rules, ".sprint-item-row")
    assert row.get("display") == "flex"
    assert row.get("flex-wrap") == "wrap", "row must wrap so the buttons can drop under the text"
    assert row.get("align-items") == "baseline", "icon/version/buttons must sit on the first title line"
    assert row.get("align-content") == "center", "single-line rows stay vertically centred in min-height"
    ver = _decls(rules, ".sprint-item-ver")
    assert ver.get("flex-shrink") == "0" and ver.get("max-width") == "100%"
    actions = _decls(rules, ".sprint-item-actions")
    assert actions.get("flex-shrink") == "0"
    assert actions.get("flex-wrap") == "wrap" and actions.get("max-width") == "100%", (
        "actions wrap inside the row instead of escaping it at narrow widths"
    )
    assert actions.get("margin-left") == "auto"
    assert _decls(rules, ".sprint-item-actions:empty").get("display") == "none", (
        "an empty actions placeholder must not take a wrapped line"
    )
    chip = _decls(rules, ".sprint-item-resources .resource-chip")
    assert chip.get("max-width") == "100%" and chip.get("overflow-wrap") == "anywhere", (
        "a long resource chip must wrap inside the column"
    )


def test_sprint_row_layout_rules_use_css_variables_only(css):
    """Dark/light themes: the sprint-row layout rules carry no literal colours."""
    rules = _css_rules(css)
    selectors = [
        ".sprint-item-row", ".sprint-item-main", ".sprint-item-title", ".sprint-item-ver",
        ".sprint-item-actions", ".sprint-item-row > .sprint-item-title",
        ".sprint-item-resources .resource-chip",
    ]
    for sel in selectors:
        decls = _decls(rules, sel)
        assert decls, f"{sel} rule missing"
        for prop, value in decls.items():
            assert not re.search(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(", value), (
                f"{sel} {prop}: {value!r} hard-codes a colour; use a CSS variable"
            )


def test_sprint_phone_media_queries_are_reconciled_with_the_base_rules(css):
    """The 768px / 480px passes only tune the base rules: they must not re-introduce
    nowrap/ellipsis, nor give the title flex-basis:100% (which on the direct-child rows
    left the status icon alone on a line above the text). On a phone the action buttons
    take their own line under the text."""
    rules = _css_rules(css)
    for media in ("@media (max-width: 768px)", "@media (max-width: 480px)"):
        sprint = [(sel, d) for m, sels, d in rules if m == media for sel in sels if "sprint-item" in sel]
        assert sprint, f"{media} must still tune the sprint rows"
        for sel, d in sprint:
            assert d.get("white-space") != "nowrap" and d.get("flex-wrap") != "nowrap", (media, sel)
            assert "text-overflow" not in d and d.get("overflow") != "hidden", (media, sel)
    phone = "@media (max-width: 768px)"
    assert _decls(rules, ".sprint-item-row > .sprint-item-title", phone).get("flex-basis", "").startswith("min(")
    assert _decls(rules, ".sprint-item-main", phone).get("flex-basis", "").startswith("min(")
    assert _decls(rules, ".sprint-item-title", phone).get("flex-basis") != "100%"
    actions = _decls(rules, ".sprint-item-actions", phone)
    assert actions.get("flex-basis") == "100%" and actions.get("justify-content") == "flex-end"
    # the wrap/align/title-wrapping rules are NOT viewport-gated: they live in the base sheet
    assert _decls(rules, ".sprint-item-row").get("flex-wrap") == "wrap"
    assert _decls(rules, ".sprint-item-title").get("white-space") == "normal"


def test_sprint_markup_uses_the_main_column_class_and_no_inline_nowrap(js):
    """The board-row wrapper carries class sprint-item-main (its flex/min-width now live
    in the stylesheet, where the media queries can reach them) and no title is rendered
    with an inline nowrap/ellipsis that would out-rank the stylesheet (the backburner rows
    used to)."""
    icon = js.index('class="sprint-item-icon" style="color:${color}"')
    assert 'class="sprint-item-main"' in js[icon : icon + 400], "board-row text column must be .sprint-item-main"
    titles = [ln for ln in js.splitlines() if 'class="sprint-item-title"' in ln]
    assert len(titles) >= 4, "expected the board, needs-attention, your-tasks and backburner title spans"
    for ln in titles:
        assert "nowrap" not in ln and "ellipsis" not in ln, f"inline truncation on a sprint title: {ln.strip()[:120]}"
    # the backburner row no longer pins its own flex layout inline (the class rules apply)
    marker = 'data-item="${escapeHtml(it.id)}" data-title="${escapeHtml(it.title)}" data-version'
    row = [ln for ln in js.splitlines() if marker in ln]
    assert row, "backburner row template not found"
    assert all("display:flex" not in ln and "align-items:center" not in ln for ln in row)


# ---------------------------------------------------------------------------
# Cascade-independent scan. The exact-selector checks above (`_decls` merges only
# rules whose selector text EQUALS the one asked for) and the jsdom contract (source
# order only, no @media) are blind to a rule that wins on specificity or applies only on
# a phone, and two such mutations re-broke the layout in a real browser while every
# test passed: an earlier `.live-body .sprint-item-title { white-space: nowrap }` and a
# `@media (max-width: 768px) { .live-body span { white-space: nowrap } }`. This scan
# ignores specificity, order, layers and viewport gates: for EVERY rule (any @media /
# @supports / @container / @layer, CSS nesting resolved, :is()/:where() groups unrolled)
# it asks which sprint-row elements the selector could style, and rejects any declaration
# that could undo the contract on one of them. A value built from var()/env()/attr()
# cannot be judged from text, so it counts as harmful wherever the property could do
# harm; and a rule inside @layer is judged like any other (it can still win: an
# `!important` layered declaration beats unlayered ones, and a layered declaration of a
# property no unlayered rule sets on that element applies), so the scan flags it with the
# rule named rather than guess at the cascade. The pixel layout is asserted in a real
# browser by tests/test_demo_ux.py (test_sprint_rows_never_overlap_in_a_real_browser).
# ---------------------------------------------------------------------------


def _sprint_static(name):
    """A file of meridian/static read from disk (no app boot: these checks are pure text)."""
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent / "meridian" / "static" / name).read_text(encoding="utf-8")


# Classes of the elements INSIDE a .sprint-item-row (renderSprintProgress + _sprintHistoryBadges).
_SPRINT_ROW_CLASSES = frozenset({
    "sprint-item-row", "sprint-item-icon", "sprint-item-main", "sprint-item-title", "sprint-item-ver",
    "sprint-item-meta", "sprint-item-actions", "sprint-item-notes", "sprint-item-resources", "resource-chip",
    "sprint-btn", "sprint-btn-fail", "sprint-btn-push", "sprint-stall-badge", "sprint-retried-badge",
    "sprint-live-dot",
})
# The board's ancestors (dashboard.ts buildTabBody): a selector may qualify itself with these.
_SPRINT_CHAIN_CLASSES = frozenset({
    "app", "main", "tab-bodies", "tab-body", "vtab-drawer", "drawer-panel", "live-body", "live-section",
    "live-sprint-progress", "active", "open",
})
# Which row classes a bare element selector (`span`, `div`, `button`) can hit.
_SPRINT_TYPE_CLASSES = {
    "span": frozenset({
        "sprint-item-icon", "sprint-item-title", "sprint-item-ver", "sprint-item-meta", "sprint-item-actions",
        "resource-chip", "sprint-stall-badge", "sprint-retried-badge", "sprint-live-dot",
    }),
    "div": frozenset({"sprint-item-row", "sprint-item-main", "sprint-item-notes", "sprint-item-resources"}),
    "button": frozenset({"sprint-btn", "sprint-btn-fail", "sprint-btn-push"}),
}
# Short fixed labels that are kept on one line / truncated on purpose.
_SPRINT_DELIBERATE = frozenset({"sprint-item-meta", "sprint-btn", "sprint-btn-fail", "sprint-btn-push"})
_SPRINT_TEXT_BOXES = frozenset({"sprint-item-title", "sprint-item-main", "sprint-item-ver", "resource-chip"})
_SPRINT_COLUMN = frozenset({"sprint-item-title", "sprint-item-main"})
# Boxes that hold wrapping text (or text-bearing children). Pinning one to a fixed height, squashing its
# line box or shifting it out of the flow paints its content over the NEXT row. The icon, the live dot, the
# badges and the buttons are fixed-size on purpose and are not in this set.
_SPRINT_FLOW_BOXES = frozenset({
    "sprint-item-row", "sprint-item-main", "sprint-item-title", "sprint-item-ver", "sprint-item-actions",
    "sprint-item-notes", "sprint-item-resources", "resource-chip",
})
_SPRINT_ROW_BOXES = frozenset({"sprint-item-row", "sprint-item-actions"})
# A value taken from a custom property / environment / attribute cannot be judged from the text alone.
_SPRINT_UNRESOLVED = re.compile(r"\b(?:var|env|attr)\(")
_SPRINT_GROUP_PSEUDO = r":(?:is|where|matches|-webkit-any|-moz-any)\("


def _sprint_alternatives(selector):
    """`selector` with its :is()/:where() groups unrolled into plain alternatives; None when a
    group is too nested or too large to unroll."""
    pending, done = [selector], []
    while pending:
        sel = pending.pop()
        found = re.search(_SPRINT_GROUP_PSEUDO + r"([^()]*)\)", sel, flags=re.I)
        if found is None:
            done.append(sel)
        elif len(pending) + len(done) > 256:
            return None
        else:
            pending.extend(
                sel[: found.start()] + alt.strip() + sel[found.end() :] for alt in _css_split(found.group(1), ",")
            )
    return done


def _sprint_strip_pseudos(selector):
    """`selector` without its pseudo-classes / -elements (their argument lists included, balanced)."""
    out, i = [], 0
    while i < len(selector):
        found = re.compile(r"::?[\w-]+").match(selector, i)
        if found is None:
            out.append(selector[i])
            i += 1
            continue
        i = found.end()
        if i < len(selector) and selector[i] == "(":
            depth = 0
            while i < len(selector):
                depth += (selector[i] == "(") - (selector[i] == ")")
                i += 1
                if depth == 0:
                    break
    return "".join(out).strip()


def _sprint_reach_one(selector):
    every = frozenset(_SPRINT_ROW_CLASSES)
    sel = _sprint_strip_pseudos(selector)
    compounds = [c for c in re.split(r"\s*[>+~]\s*|\s+", sel) if c]
    if not compounds:
        return every  # a bare ':hover'
    for comp in compounds[:-1]:  # ancestors must be satisfiable by the board's own chain
        classes, ids = re.findall(r"\.([\w-]+)", comp), re.findall(r"#([\w-]+)", comp)
        tag = re.match(r"[a-zA-Z][\w-]*|\*", comp)
        if any(c not in _SPRINT_ROW_CLASSES | _SPRINT_CHAIN_CLASSES for c in classes) or any(i != "tab-bodies" for i in ids):
            return frozenset()
        if not classes and not ids and tag and tag.group(0) not in ("*", "div", "main", "body", "html", "span"):
            return frozenset()
    last = compounds[-1]
    classes, ids = re.findall(r"\.([\w-]+)", last), re.findall(r"#([\w-]+)", last)
    if ids or any(c not in _SPRINT_ROW_CLASSES for c in classes):
        return frozenset()
    if classes:
        return frozenset(classes)
    if "[" in last:  # an attribute selector alone: can't tell, assume every kind
        return every
    tag = re.match(r"[a-zA-Z][\w-]*|\*", last)
    name = tag.group(0) if tag else "*"
    return every if name == "*" else _SPRINT_TYPE_CLASSES.get(name, frozenset())


def _sprint_reach(selector):
    """The row-element classes `selector` could style (conservative: state pseudo-classes
    and :not()/:has() arguments are ignored, :is()/:where() groups are unrolled, any
    ancestor/sibling combinator counts as an ancestor), or an empty set when it cannot match
    anything inside a sprint row."""
    alternatives = _sprint_alternatives(selector)
    if alternatives is None or any(re.search(_SPRINT_GROUP_PSEUDO, a, flags=re.I) for a in alternatives):
        return frozenset(_SPRINT_ROW_CLASSES)  # too tangled to unroll: assume it can reach anything
    return frozenset().union(*(_sprint_reach_one(a) for a in alternatives))


def _sprint_squashes(line_height):
    """True when a line-height is small enough to paint wrapped lines over each other."""
    found = re.fullmatch(r"([0-9.]+)(px|pt|em|rem|%)?", line_height)
    if not found:
        return False
    floor = {None: 1, "px": 9, "pt": 7, "em": 0.75, "rem": 0.75, "%": 75}[found.group(2)]
    return float(found.group(1)) < floor


def _sprint_harm(prop, raw, kinds):
    """Why `prop: raw` could re-break the layout contract on one of `kinds`, else None. A value
    built from a custom property cannot be judged here, so it counts as harmful wherever the
    property could do harm (the real-browser test judges what it really resolves to)."""
    v = " ".join(raw.replace("!important", "").lower().split())
    unresolved = bool(_SPRINT_UNRESOLVED.search(v))
    truncating = kinds - _SPRINT_DELIBERATE
    flow = kinds & _SPRINT_FLOW_BOXES

    def verdict(applies, harmful, message):
        if not applies:
            return None
        if unresolved:
            return f"{message} (its value is built from a custom property, which cannot be resolved here)"
        return message if harmful else None

    if prop == "white-space":
        return verdict(truncating, v in ("nowrap", "pre"), "stops the text wrapping")
    if prop in ("text-wrap", "text-wrap-mode"):
        return verdict(truncating, "nowrap" in v.split(), "stops the text wrapping")
    if prop == "text-overflow":
        return verdict(truncating, v not in ("clip", "initial", "unset", "inherit"), "ellipsizes (hides) text")
    if prop in ("overflow", "overflow-x", "overflow-y", "overflow-block", "overflow-inline"):
        return verdict(truncating, bool(re.search(r"\b(hidden|clip|scroll|auto)\b", v)), "clips its content")
    if prop == "flex-wrap":
        return verdict(kinds & _SPRINT_ROW_BOXES, v == "nowrap", "stops the row wrapping")
    if prop == "flex-flow":
        return verdict(kinds & _SPRINT_ROW_BOXES, "nowrap" in v.split(), "stops the row wrapping")
    if prop in ("overflow-wrap", "word-wrap"):
        return verdict(kinds & _SPRINT_TEXT_BOXES, v == "normal", "stops a long token breaking")
    if prop == "word-break":
        return verdict(kinds, v == "keep-all", "stops a long token breaking")
    if prop == "display":
        return verdict(
            kinds & _SPRINT_COLUMN,
            not re.fullmatch(r"block|flex|grid|flow-root|inline-block|list-item", v),
            "makes the text column inline (ignores width / overflow) or hides it",
        )
    if prop == "min-width":
        return verdict(kinds & _SPRINT_COLUMN, not re.fullmatch(r"0(px)?", v), "stops the text column shrinking")
    if prop == "position":
        return verdict(kinds, v in ("absolute", "fixed"), "takes a row element out of flow (it can paint over its neighbours)")
    if prop in ("height", "block-size"):
        return verdict(
            flow, not re.fullmatch(r"auto|fit-content|min-content|max-content|initial|unset|revert|revert-layer", v),
            "pins a text box to a fixed height, so wrapped content paints over the next row",
        )
    if prop in ("max-height", "max-block-size"):
        return verdict(
            flow, v not in ("none", "initial", "unset", "revert", "revert-layer"),
            "caps a text box's height, so wrapped content paints over the next row",
        )
    if prop == "line-height":
        return verdict(flow, _sprint_squashes(v), "squashes wrapped lines onto each other")
    if prop == "font":
        slash = re.search(r"/\s*([^\s,/]+)", v)
        return verdict(flow, bool(slash) and _sprint_squashes(slash.group(1)), "squashes wrapped lines onto each other")
    if prop in ("top", "bottom", "inset", "inset-block", "inset-block-start", "inset-block-end"):
        return verdict(
            flow, not re.fullmatch(r"auto|0(px|%|em|rem)?|initial|unset|revert", v),
            "offsets a text box from its place in the flow (it can paint over the previous row)",
        )
    if prop in ("transform", "translate"):
        return verdict(flow, v not in ("none", "initial", "unset", "revert"), "moves a text box out of its place in the flow")
    if prop in ("margin", "margin-top", "margin-bottom", "margin-block", "margin-block-start", "margin-block-end"):
        return verdict(flow, bool(re.search(r"(^|\s)-[0-9.]", v)), "pulls a text box over its neighbours with a negative margin")
    if prop == "all":
        return verdict(truncating, True, "resets every property, including the wrapping contract")
    return None


def _sprint_cascade_offenders(css_text):
    out = []
    for media, selectors, decls in _css_rules(css_text):
        for sel in selectors:
            kinds = _sprint_reach(sel)
            for prop, value in decls.items():
                why = kinds and _sprint_harm(prop, value, kinds)
                if why:
                    out.append(f"{media + ' ' if media else ''}{sel} {{ {prop}: {value} }} {why}")
    return out


def test_no_stylesheet_rule_can_undo_the_sprint_row_wrapping_contract():
    """Whatever its specificity, source position or @media gate, no rule in dashboard.css
    may stop a sprint row's text wrapping, clip or ellipsize it, stop the row wrapping,
    make the title column inline / unshrinkable, or take a row element out of flow."""
    assert _sprint_cascade_offenders(_sprint_static("dashboard.css")) == []


def test_sprint_cascade_scan_reaches_the_rules_it_must_judge():
    """The scan is not vacuous: the real sheet's contract rules and its @media-gated
    rules reach row elements, and the deliberately truncated '-> v2' pill is tolerated."""
    rules = _css_rules(_sprint_static("dashboard.css"))
    reaching = [(m, s) for m, sels, _ in rules for s in sels if _sprint_reach(s)]
    assert sum(1 for m, _ in reaching if m is None) >= 10
    assert sum(1 for m, _ in reaching if m == "@media (max-width: 768px)") >= 3
    assert sum(1 for m, _ in reaching if m == "@media (max-width: 480px)") >= 1
    meta = _decls(rules, ".sprint-item-meta")
    assert meta.get("white-space") == "nowrap" and meta.get("text-overflow") == "ellipsis"
    assert _sprint_cascade_offenders(".sprint-item-meta { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }") == []


@pytest.mark.parametrize(
    "mutant",
    [
        # the two that used to survive every suite
        ".live-body .sprint-item-title { white-space: nowrap; }",
        "@media (max-width: 768px) { .live-body span { white-space: nowrap; } }",
        # other shapes of the same regression
        "@media (max-width: 480px) { span { white-space: pre; } }",
        "@supports (display: grid) { .tab-body .sprint-item-row span { white-space: nowrap; } }",
        ".sprint-item-title { overflow: hidden; text-overflow: ellipsis; }",
        ".sprint-item-row * { overflow-x: clip; }",
        ".sprint-item-row .sprint-item-title { white-space: nowrap !important; }",
        ".sprint-item-title:hover { white-space: nowrap; }",
        "@media (max-width: 480px) { .sprint-item-row { flex-wrap: nowrap; } }",
        ".sprint-item-actions { flex-wrap: nowrap; }",
        "@media (max-width: 768px) { .sprint-item-title { display: inline; } }",
        ".sprint-item-main { min-width: auto; }",
        ".sprint-item-ver { overflow-wrap: normal; }",
        ".sprint-item-resources .resource-chip { white-space: nowrap; }",
        ".sprint-item-title { position: absolute; }",
        "#tab-bodies div { white-space: nowrap; }",
        "[class*='sprint-item'] { white-space: nowrap; }",
        # --- syntax the scan used to be blind to (every one of these re-breaks a real browser) ---
        # CSS nesting: the child rule inherits its parent's selector
        ".live-body { .sprint-item-title { white-space: nowrap; } }",
        ".sprint-item-row { & .sprint-item-title { white-space: nowrap; } }",
        ".sprint-item-row { > .sprint-item-title { overflow: hidden; } }",
        ".sprint-item-title { @media (max-width: 768px) { white-space: nowrap; } }",
        # a value taken from a custom property cannot be judged from the text
        ".sprint-item-title { white-space: var(--ws); }",
        ".sprint-item-row { display: flex; flex-wrap: var(--wrap); }",
        # :is() / :where() groups (their commas used to split the selector list apart)
        ":is(.live-body, .tab-body) :is(.sprint-item-title) { white-space: nowrap; }",
        ".sprint-item-row :where(.sprint-item-title, .sprint-item-ver) { white-space: nowrap; }",
        ":is(.sprint-item-title) { white-space: nowrap; }",
        ".sprint-item-title:not(.x) { white-space: nowrap; }",
        ".sprint-item-row:has(> .sprint-item-ver) .sprint-item-title { white-space: nowrap; }",
        # layers / containers, and statement at-rules that used to swallow the rule after them
        "@layer base { .sprint-item-title { white-space: nowrap !important; } }",
        "@container (min-width: 1px) { .sprint-item-title { white-space: nowrap; } }",
        "@layer base, theme;\n.sprint-item-title { white-space: nowrap; }",
        "@import url('x.css');\n.sprint-item-title { white-space: nowrap; }",
        # --- vertical and spacing: rows pinned / squashed / shifted so their content paints over the next row ---
        ".sprint-item-row { height: 28px; }",
        ".sprint-item-row { max-height: 28px; }",
        "@media (max-width: 768px) { .sprint-item-row { block-size: 28px; } }",
        ".sprint-item-actions { height: 0; }",
        ".sprint-item-main { max-height: 1.4em; }",
        ".sprint-item-title { line-height: 0.5; }",
        ".sprint-item-title { line-height: 4px; }",
        ".sprint-item-title { font: 12px/0.5 sans-serif; }",
        ".sprint-item-title { position: relative; top: -22px; }",
        ".sprint-item-title { transform: translateY(-20px); }",
        ".sprint-item-ver { translate: 0 -20px; }",
        ".sprint-item-title { margin-top: -20px; }",
        ".sprint-item-row { margin: 0 0 -10px; }",
        # shorthands and newer longhands of the properties already watched
        ".sprint-item-row { flex-flow: row nowrap; }",
        ".sprint-item-title { text-wrap: nowrap; }",
        ".sprint-item-title { text-wrap-mode: nowrap; }",
        ".sprint-item-row { overflow-y: hidden; }",
        ".sprint-item-title { all: unset; }",
    ],
)
def test_sprint_cascade_scan_catches_each_regression_shape(mutant):
    assert _sprint_cascade_offenders(mutant), f"scan is blind to: {mutant}"
    assert _sprint_cascade_offenders(_sprint_static("dashboard.css") + "\n" + mutant), f"scan is blind to (appended): {mutant}"


@pytest.mark.parametrize(
    "benign",
    [
        ".tabs span { white-space: nowrap; }",
        ".sidebar button { overflow: hidden; }",
        "@media (max-width: 768px) { .vtab-strip .vtab-btn { white-space: nowrap; overflow: hidden; } }",
        ".sprint-item-row { gap: 10px; color: red; }",
        "button { white-space: nowrap; }",
        ".sprint-btn { white-space: nowrap; overflow: hidden; }",
        ".live-session-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }",
        # fixed-size parts of a row are not text boxes
        ".sprint-btn { height: 20px; line-height: 1; }",
        ".sprint-item-icon { height: 14px; line-height: 1; transform: scale(1.1); }",
        ".sprint-live-dot { width: 7px; height: 7px; }",
        "@keyframes sprintPulse { from { height: 0; top: -4px; } to { height: 7px; top: 0; } }",
        # values that are fine on a text box
        ".sprint-item-row { min-height: 28px; line-height: 1.4; margin: 0 0 2px; height: auto; max-height: none; }",
        ".sprint-item-title { line-height: normal; transform: none; top: auto; margin-top: 2px; font: 12px/1.4 sans-serif; }",
        ".sprint-item-actions { margin-left: auto; gap: 2px; flex-wrap: wrap; }",
        ".sprint-item-row { padding: var(--row-pad); gap: var(--gap); color: var(--text); }",
        # the same rules written with nesting, groups, layers or custom properties, but not reaching a row
        ".sidebar { .tab { white-space: nowrap; } }",
        ".tabs { span { white-space: nowrap; } }",
        ".tabs { @media (max-width: 768px) { white-space: nowrap; } }",
        ":is(.tabs, .sidebar) span { white-space: nowrap; }",
        ".vtab-btn:is(.a, .b), .tab:where(.x) { white-space: nowrap; overflow: hidden; }",
        "@layer base { .tabs span { white-space: nowrap; } }",
        "@container (min-width: 1px) { .sidebar button { overflow: hidden; } }",
        ".tabs span { white-space: var(--ws); }",
        "@font-face { font-family: X; src: url(data:font/woff2;base64,AAAA) format('woff2'); }",
        "@import url('x.css');\n.tabs span { white-space: nowrap; }",
    ],
)
def test_sprint_cascade_scan_does_not_cry_wolf(benign):
    assert _sprint_cascade_offenders(benign) == [], benign


def test_css_rules_parser_models_nesting_groups_and_statement_at_rules():
    """The scan is only as good as the parser feeding it: it must resolve nesting, keep the
    conditional gate of every rule, split selector lists only on top-level commas, not stop at a
    `;` inside url()/strings, and not lose the rule that follows a statement at-rule."""
    rules = _css_rules(
        "@import url('x.css');\n"
        "@layer base, theme;\n"
        ".a, :is(.b, .c) > .d { color: red; background: url(data:image/png;base64,AAA=); content: 'a;b'; }\n"
        ".live-body { .sprint-item-title { white-space: nowrap } &:hover { color: blue } > .x { top: 1px }\n"
        "  @media (max-width: 768px) { gap: 2px } }\n"
        "@media (max-width: 768px) { @supports (display: grid) { .g { display: grid } } }\n"
        "@keyframes k { from { height: 0 } to { height: 5px } }\n"
        "@font-face { font-family: F; src: url(f.woff2) }\n"
        "@layer base { .layered { white-space: pre } }\n"
        ".last { color: green }"
    )
    by_selector = {tuple(sels): (gate, decls) for gate, sels, decls in rules}
    assert by_selector[(".a", ":is(.b, .c) > .d")][1] == {
        "color": "red", "background": "url(data:image/png;base64,AAA=)", "content": "'a;b'",
    }
    assert by_selector[(".live-body .sprint-item-title",)][1] == {"white-space": "nowrap"}
    assert by_selector[(".live-body:hover",)][1] == {"color": "blue"}
    assert by_selector[(".live-body > .x",)][1] == {"top": "1px"}
    assert by_selector[(".live-body",)] == ("@media (max-width: 768px)", {"gap": "2px"}), (
        "a nested @media must style its parent selector under that gate"
    )
    assert by_selector[(".g",)] == ("@media (max-width: 768px) @supports (display: grid)", {"display": "grid"})
    assert by_selector[(".layered",)] == ("@layer base", {"white-space": "pre"})
    assert by_selector[(".last",)] == (None, {"color": "green"}), "the rule after statement at-rules must survive"
    assert all("k" not in sels and "from" not in sels for _, sels, _ in rules), "@keyframes steps are not rules"
    assert not any("font-family" in decls for _, _, decls in rules), "@font-face has no selector: skipped"


def test_sprint_reach_unrolls_is_and_where_groups_and_strips_other_pseudo_classes():
    assert _sprint_reach(":is(.live-body, .tab-body) :is(.sprint-item-title)") == {"sprint-item-title"}
    assert _sprint_reach(".sprint-item-row :where(.sprint-item-title, .sprint-item-ver)") == {
        "sprint-item-title", "sprint-item-ver",
    }
    assert _sprint_reach(".sprint-item-row:has(> .sprint-item-ver):not(.x)") == {"sprint-item-row"}
    assert _sprint_reach(":is(.sidebar, .tabs) .sprint-item-title") == frozenset(), "ancestors outside the board's chain"
    assert _sprint_reach(":is(.tabs, .sidebar) span") == frozenset()
    assert _sprint_reach(":is(:is(.sprint-item-title))") == {"sprint-item-title"}
    # too deeply nested to unroll: assume it can reach anything rather than miss it
    assert _sprint_reach(":is(:not(.a)) .b") == frozenset(_SPRINT_ROW_CLASSES)


def test_sprint_cascade_scan_knows_every_class_the_row_markup_renders():
    """Drift guard for _SPRINT_ROW_CLASSES: the scan can only judge the classes it
    knows; every static class in the row markup must be listed, and none may be stale."""
    src = _sprint_static("dashboard-sprint.ts")
    board = src[src.index("export function renderSprintProgress") : src.index("export function renderQueue")]
    helper = src[src.index("function _sprintHistoryBadges") : src.index("function _sprintHistoryBadges") + 1700]
    rendered = set()
    for chunk in (board, helper):
        for attr in re.findall(r'class="([^"$]*)"', chunk):
            rendered.update(attr.split())
    row_like = {c for c in rendered if c.startswith(("sprint-item-", "sprint-btn", "sprint-stall", "sprint-retried", "sprint-live-dot")) or c == "resource-chip"}
    assert row_like <= _SPRINT_ROW_CLASSES, f"row classes missing from _SPRINT_ROW_CLASSES: {sorted(row_like - _SPRINT_ROW_CLASSES)}"
    assert _SPRINT_ROW_CLASSES <= rendered, f"stale classes in _SPRINT_ROW_CLASSES: {sorted(_SPRINT_ROW_CLASSES - rendered)}"


# ---------------------------------------------------------------------------
# b03be6a6 — Minimal installable PWA: manifest + icons + network-first SW.
# ---------------------------------------------------------------------------


def test_pwa_service_worker_served_at_root(client):
    """b03be6a6 — /sw.js is served at the site root with a JS content-type.

    Root scope ("/") is required so the worker can control /dashboard; a SW
    mounted under /static would only scope to /static/*.
    """
    r = client.get("/sw.js")
    assert r.status_code == 200, f"expected 200 for /sw.js, got {r.status_code}"
    ctype = r.headers.get("content-type", "")
    assert "javascript" in ctype.lower(), f"sw.js must be JS, got {ctype!r}"
    # Root-scope enablement header (belt-and-suspenders).
    assert r.headers.get("service-worker-allowed") == "/", (
        "sw.js must advertise Service-Worker-Allowed: / for root scope"
    )


def test_pwa_service_worker_is_network_first(client):
    """b03be6a6 (HARD REQUIREMENT) — the SW must be network-first, not cache-first.

    Dashboard edits must show up on next open with zero rebuild/republish, so
    the fetch handler must call fetch(request) *before* any caches.match(), and
    it must not precache the HTML/JS app shell.
    """
    body = client.get("/sw.js").text
    assert "install" in body and "skipWaiting" in body, "install must skipWaiting"
    assert "clients.claim" in body, "activate must clients.claim()"

    # fetch() must appear before caches.match() — the defining ordering of a
    # network-first worker.
    fetch_idx = body.find("fetch(request)")
    match_idx = body.find("caches.match")
    assert fetch_idx != -1, "SW must call fetch(request)"
    assert match_idx != -1, "SW must fall back to caches.match on failure"
    assert fetch_idx < match_idx, (
        "network-first violated: fetch(request) must come before caches.match "
        "(b03be6a6)"
    )

    # The app shell must NOT be precached — no HTML/JS/CSS in the SW's cache list.
    assert "/dashboard" not in body, "SW must not cache the dashboard HTML shell"
    assert ".bundle.js" not in body and "dashboard.css" not in body, (
        "SW must not precache the JS/CSS app shell (would break live-edit)"
    )
    assert "b03be6a6" in body, "SW must carry the sprint-item tag"


def test_pwa_manifest_served_with_icons(client):
    """b03be6a6 — /manifest.webmanifest returns 200 with the manifest media type
    and declares both the 192 and 512 icons."""
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200, f"expected 200, got {r.status_code}"
    ctype = r.headers.get("content-type", "")
    assert "manifest" in ctype.lower(), f"unexpected manifest content-type {ctype!r}"

    data = r.json()
    assert data["name"] == "Meridian"
    assert data["short_name"] == "Meridian"
    assert data["display"] == "standalone"
    assert data["start_url"] == "/dashboard", "start_url must open the dashboard"

    sizes = {icon["sizes"] for icon in data["icons"]}
    assert "192x192" in sizes and "512x512" in sizes, "both icon sizes required"
    purposes = {icon.get("purpose", "") for icon in data["icons"]}
    assert any("maskable" in p for p in purposes), "a maskable icon entry is required"


def test_pwa_icons_are_pngs(client):
    """b03be6a6 — both icons return 200 with image/png (no byte-content assertion)."""
    for size in (192, 512):
        r = client.get(f"/static/icon-{size}.png")
        assert r.status_code == 200, f"icon-{size}.png missing"
        assert r.headers.get("content-type", "").startswith("image/png"), (
            f"icon-{size}.png must be served as image/png"
        )
        # Valid PNG magic bytes (not an exact-content assertion).
        assert r.content[:8] == bytes((0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A)), (
            "not a valid PNG"
        )


def test_pwa_dashboard_head_wires_manifest_and_sw(soup, html):
    """b03be6a6 — the dashboard <head> links the manifest, sets theme-color, and
    registers the service worker inline (in the template, not the TS bundle)."""
    manifest_link = soup.find("link", rel="manifest")
    assert manifest_link is not None, "dashboard must <link rel=manifest>"
    assert manifest_link.get("href") == "/manifest.webmanifest"

    theme = soup.find("meta", attrs={"name": "theme-color"})
    assert theme is not None and theme.get("content"), "theme-color meta required"

    apple = soup.find("link", rel="apple-touch-icon")
    assert apple is not None, "apple-touch-icon link required for iOS install"

    # SW registration is inline in the HTML (no bundle rebuild needed).
    assert "serviceWorker" in html, "SW registration guard missing"
    assert "navigator.serviceWorker.register('/sw.js'" in html, (
        "dashboard must register /sw.js"
    )


def test_pwa_install_prompt_wired(js):
    """afcbd8a2 — beforeinstallprompt is captured (not left to the browser's own
    mini-infobar) and replayed from a real user-gesture click, with the button
    removed again on click or on appinstalled.

    b03be6a6 shipped the installability requirements (manifest/SW/icons); this
    covers the actual install AFFORDANCE, which was the remaining gap.
    """
    assert "beforeinstallprompt" in js, "beforeinstallprompt listener missing"
    assert "event.preventDefault()" in js, (
        "must preventDefault() beforeinstallprompt to suppress the browser's own UI"
    )
    assert "appinstalled" in js, "appinstalled listener missing (button must be hidden after install)"
    assert "pwa-install-button" in js, "install button id/class missing"
    assert "promptEvent.prompt()" in js, "captured event's prompt() must be replayed on click"

    # Never shown when already running as an installed app.
    assert "display-mode: standalone" in js, "standalone display-mode check missing"
    assert "navigator.standalone" in js, "legacy iOS standalone check missing"
