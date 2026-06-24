// --- ITEM 4 esbuild: pull module scripts into the bundle graph ---
import "./dashboard-utils.js";
import "./dashboard-demo.js";
import "./dashboard-timeline.js";
import "./dashboard-mcp.js";
import "./dashboard-sprint.js";
import "./dashboard-settings.js";

import "./dashboard-notes.js";
import "./dashboard-files.js";
import "./dashboard-rewind.js";
﻿const TABS_KEY = 'meridian.openTabs';

const ACTIVE_PROJECT_KEY = 'meridian.activeProject';

// const _PLAN_LABELS -- moved to dashboard-utils.js

const state = {

  projects: [],

  tabs: [], // [{id, project}]

  activeTab: null,

  panels: {}, // tabId -> { ws, taskCache, sessionName, goalRaw, goalIsJson }

  apiKeyConfigured: false,

  // v0.6.5 — server runtime config fetched from /config on startup.

  serverConfig: { server_url: '', host: '', port: 0, version: '' },

  // workspace switcher — tenant_id of the currently active workspace (null = own)

  activeWorkspaceTenantId: null,

};

// Expose state on window so esbuild IIFE modules can access it via window.state
window.state = state;



// function isDemoMode -- moved to dashboard-demo.js / dashboard-timeline.js



// function isHostedMode -- moved to dashboard-demo.js / dashboard-timeline.js



// function isHostedAdmin -- moved to dashboard-demo.js / dashboard-timeline.js



async function hideHostedAdminControls() {

  // Hide ALL server-management controls — Restart/Stop kill the shared Fly machine

  // and must never be reachable from usemeridian.us.

  const toHide = [

    '#restart-server-btn', '#stop-server-btn', '#banner-restart-btn',

    '#git-check-btn', '#update-banner',   // check-updates and update banner

  ];

  toHide.forEach(sel => {

    document.querySelectorAll(sel).forEach(el => { el.style.display = 'none'; });

  });



  // The server-controls row (restart/stop/check-updates) is entirely empty on

  // hosted — every child is hidden above. Collapse the row itself so it doesn't

  // leave a gap that pushes the account block to the bottom of the footer.

  const ctrlRow = document.getElementById('server-controls-row');

  if (ctrlRow) ctrlRow.style.display = 'none';



  const connInd = document.getElementById('connection-indicator');

  if (!isHostedAdmin()) {

    // Hide connection switcher / profile selector for normal hosted users —

    // switching connections would break tenant isolation.

    if (connInd) connInd.style.display = 'none';

    document.querySelectorAll('.conn-popup').forEach(p => p.remove());

  } else {

    document.querySelectorAll('.hosted-label').forEach(el => el.remove());

  }



  // Replace connection area with a static "usemeridian.us" label for non-admins.

  const footer = document.querySelector('.sidebar-footer');

  if (!isHostedAdmin() && footer && !footer.querySelector('.hosted-label')) {

    const lbl = document.createElement('div');

    lbl.className = 'hosted-label';

    lbl.style.cssText = 'font-size:10px;color:var(--accent-green);font-family:\'IBM Plex Mono\',monospace;padding:4px 6px;border:1px solid var(--accent-green)44;border-radius:3px;opacity:0.8;letter-spacing:.03em';

    lbl.textContent = '🧭 usemeridian.us';

    footer.prepend(lbl);

  }



  // Sign-out link in sidebar-footer for all hosted users (free tier + admin).

  // Lives here (not in _renderPlanBadge) so it appears even when /me fails or

  // returns {} — the free-tier signout-missing bug fix.

  ensureSignOutLink();



  // Plan badge + signed-in-as email — fetch /me and render badge here so it

  // shows up even if loadServerConfig's /me call was swallowed by a catch.

  try {

    const me = await api('/me');

    if (me && me.plan) {

      _renderPlanBadge(me);

      ensureSignOutLink(me.email);

    }

  } catch (e) { /* not hosted / not logged in */ }



  // Rename "advanced setup ↗" → "Close" in first-run wizard (no local config on hosted)

  const advLink = document.getElementById('ez-advanced-link');

  if (advLink) advLink.textContent = 'Close';

}



function ensureSignOutLink(emailHint) {

  const footer = document.querySelector('.sidebar-footer');

  if (!footer) return;

  // Visible "signed in as {email}" line — rendered once /me resolves the email.

  // Lets users confirm which account they're on (and spot a stale session before

  // it 404s their way through another account's workspace).

  if (emailHint) {

    let who = document.getElementById('signed-in-as');

    if (!who) {

      who = document.createElement('div');

      who.id = 'signed-in-as';

      who.style = 'margin-top:8px;font-size:10px;color:var(--muted);font-family:var(--font-mono);text-align:center;opacity:0.75;word-break:break-all;line-height:1.3';

      const existingLink = document.getElementById('signout-link');

      if (existingLink) footer.insertBefore(who, existingLink);

      else footer.appendChild(who);

    }

    who.textContent = `signed in as ${emailHint}`;

    who.title = emailHint;

  }

  if (document.getElementById('signout-link')) {

    if (emailHint) document.getElementById('signout-link').title = `Signed in as ${emailHint}`;

    return;

  }

  const link = document.createElement('a');

  link.id = 'signout-link';

  link.href = '/auth/logout';

  link.textContent = 'Sign out';

  link.title = emailHint ? `Signed in as ${emailHint}` : 'Sign out';

  // Visible button-style affordance — the faint text version was easy to miss

  // on free-tier accounts.

  link.style = 'display:block;margin-top:8px;padding:6px 10px;font-size:11px;color:var(--text);font-family:var(--font-mono);text-align:center;text-decoration:none;background:var(--surface-1);border:1px solid var(--border);border-radius:5px;opacity:1';

  link.onmouseenter = () => { link.style.borderColor = 'var(--accent)'; link.style.color = 'var(--accent)'; };

  link.onmouseleave = () => { link.style.borderColor = 'var(--border)'; link.style.color = 'var(--text)'; };

  footer.appendChild(link);

}



// Workspace switcher: shown in sidebar-footer when the user belongs to more

// than one workspace (their own + accepted invites).

async function ensureWorkspaceSwitcher() {

  const footer = document.querySelector('.sidebar-footer');

  if (!footer || document.getElementById('workspace-switcher')) return;

  let workspaces;

  try { workspaces = await fetch('/me/workspaces').then(r => r.ok ? r.json() : null); }

  catch (_) { return; }

  if (!workspaces || workspaces.length < 2) return;



  const wrap = document.createElement('div');

  wrap.id = 'workspace-switcher';

  wrap.style.cssText = 'margin-top:8px';



  const label = document.createElement('div');

  label.style.cssText = 'font-size:9px;color:var(--muted);font-family:var(--font-mono);text-transform:uppercase;letter-spacing:.04em;margin-bottom:3px;opacity:.7';

  label.textContent = 'workspace';



  const sel = document.createElement('select');

  sel.style.cssText = 'width:100%;font-size:11px;font-family:var(--font-mono);background:var(--surface-1);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:4px 6px;cursor:pointer;outline:none';

  workspaces.forEach(ws => {

    const opt = document.createElement('option');

    opt.value = ws.tenant_id;

    opt.textContent = ws.is_own ? 'My workspace' : ws.owner_email;

    if (!state.activeWorkspaceTenantId && ws.is_own) opt.selected = true;

    if (state.activeWorkspaceTenantId === ws.tenant_id) opt.selected = true;

    sel.appendChild(opt);

  });



  sel.onchange = async () => {

    const chosen = sel.value;

    const own = workspaces.find(w => w.is_own);

    state.activeWorkspaceTenantId = (own && chosen === own.tenant_id) ? null : chosen;

    // Close all open tabs — they belong to the old workspace.

    [...state.tabs].forEach(t => { try { closeTab(t.id); } catch (_) {} });

    await loadProjects();

    // Show which workspace is active.

    const active = workspaces.find(w => w.tenant_id === chosen);

    sel.title = active ? (active.is_own ? 'My workspace' : `${active.owner_email} (${active.role})`) : '';

    _renderWorkspaceContextBadge(wrap, workspaces);

    _refreshGuestMode();

  };



  // "Connect your DB" link below the switcher

  const connectLink = document.createElement('a');

  connectLink.id = 'connect-db-link';

  connectLink.href = '#';

  connectLink.textContent = '⊕ Connect your DB';

  connectLink.style.cssText = 'display:block;margin-top:4px;font-size:9px;font-family:var(--font-mono);color:var(--muted);text-decoration:none;opacity:.7;letter-spacing:.03em';

  connectLink.onmouseenter = () => { connectLink.style.opacity = '1'; connectLink.style.color = 'var(--accent)'; };

  connectLink.onmouseleave = () => { connectLink.style.opacity = '.7'; connectLink.style.color = 'var(--muted)'; };

  connectLink.onclick = e => { e.preventDefault(); showConnectDbModal(); };



  wrap.appendChild(label);

  wrap.appendChild(sel);

  _renderWorkspaceContextBadge(wrap, workspaces);

  wrap.appendChild(connectLink);

  const existingLabel = footer.querySelector('.hosted-label');

  if (existingLabel) footer.insertBefore(wrap, existingLabel);

  else footer.prepend(wrap);

}


// 9b8261e4 — the caller's role in the currently ACTIVE workspace. The own
// workspace (or self-hosted) is always 'owner'; an invited workspace returns
// the membership role (admin|member|viewer). Used to hide owner-only settings
// UI for guests. Fails open to 'owner' (server-side enforcement is the gate).
async function getActiveWorkspaceRole() {
  if (!isHostedMode() || !state.activeWorkspaceTenantId) return 'owner';
  try {
    const wss = await fetch('/me/workspaces').then(r => r.ok ? r.json() : null);
    const ws = (wss || []).find(w => w.tenant_id === state.activeWorkspaceTenantId);
    return (ws && ws.role) || 'owner';
  } catch (_) { return 'owner'; }
}


// fcb02a6d — plan/role badge in the sidebar. On your own workspace it shows your
// plan (Free/Trial/Standard/Pro); on a workspace you were invited to it shows an
// "invite · {role}" badge. Re-rendered on every workspace switch.
function _renderWorkspaceContextBadge(wrap, workspaces) {
  if (!wrap) return;
  let badge = wrap.querySelector('.ws-context-badge');
  if (!badge) {
    badge = document.createElement('div');
    badge.className = 'ws-context-badge';
    badge.style.cssText = 'display:inline-block;margin-top:6px;padding:2px 8px;border-radius:10px;font-size:9px;font-weight:700;letter-spacing:.05em;font-family:var(--font-mono);text-transform:uppercase';
    wrap.appendChild(badge);
  }
  const active = (workspaces || []).find(w =>
    state.activeWorkspaceTenantId ? w.tenant_id === state.activeWorkspaceTenantId : w.is_own);
  const colors = { free: '#3b82f6', trial: '#059669', standard: '#3b82f6', pro: '#7c3aed', admin: '#9ca3af', invite: '#f59e0b' };
  let label, color;
  if (active && !active.is_own) {
    label = `invite · ${active.role || 'member'}`;
    color = colors.invite;
  } else {
    const plan = window.state.tenantPlan || 'free';
    label = (window._PLAN_LABELS && window._PLAN_LABELS[plan]) || plan;
    color = colors[plan] || '#9ca3af';
  }
  badge.textContent = label;
  badge.style.background = color + '22';
  badge.style.color = color;
  badge.style.border = '1px solid ' + color + '44';
}


// 2271635e — client-side search/filter for tab lists. Matches a row's
// data-search attribute (preferred) or its textContent; hides non-matches.
function _filterTabRows(query, container, rowSelector) {
  if (!container) return;
  const q = (query || '').trim().toLowerCase();
  container.querySelectorAll(rowSelector).forEach(row => {
    const hay = (row.dataset.search || row.textContent || '').toLowerCase();
    row.style.display = (!q || hay.includes(q)) ? '' : 'none';
  });
}

function _wireTabSearch(inputId, containerId, rowSelector) {
  const input = document.getElementById(inputId);
  const container = document.getElementById(containerId);
  if (!input || !container) return;
  if (!input.dataset.searchWired) {
    input.dataset.searchWired = '1';
    input.addEventListener('input', () => _filterTabRows(input.value, container, rowSelector));
  }
  _filterTabRows(input.value, container, rowSelector);  // re-apply after a re-render
}


// c545f727 — toggle a body class so destructive controls marked .guest-hidden
// are hidden for invited viewers/members in the active workspace. Owner/admin
// keep them. Server-side enforcement (393eed0a) is the real gate; this just
// declutters the UI so guests don't click buttons that 403.
async function _refreshGuestMode() {
  let guest = false;
  try {
    const r = await getActiveWorkspaceRole();
    guest = (r === 'viewer' || r === 'member');
  } catch (_) {}
  document.body.classList.toggle('meridian-guest', guest);
}



function showConnectDbModal() {

  if (document.getElementById('connect-db-modal')) return;

  const overlay = document.createElement('div');

  overlay.id = 'connect-db-modal';

  overlay.style.cssText = 'position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center';

  const box = document.createElement('div');

  box.style.cssText = 'background:var(--surface-0);border:1px solid var(--border);border-radius:8px;padding:24px 28px;width:460px;max-width:94vw;display:flex;flex-direction:column;gap:12px';

  box.innerHTML = `

    <div style="font-weight:700;font-size:14px">Connect your Meridian DB</div>

    <div style="font-size:12px;color:var(--muted)">Enter a PostgreSQL connection string to use your own Neon (or any Postgres) project as your workspace DB.</div>

    <input id="connect-db-url" type="password" placeholder="postgresql://user:pass@host/db?sslmode=require"

      style="font-family:var(--font-mono);font-size:11px;padding:7px 10px;border:1px solid var(--border);border-radius:5px;background:var(--surface-1);color:var(--text);width:100%;box-sizing:border-box">

    <div id="connect-db-status" style="font-size:11px;min-height:16px;color:var(--muted)"></div>

    <div style="display:flex;gap:8px;justify-content:flex-end">

      <button id="connect-db-cancel" class="secondary" style="font-size:12px">Cancel</button>

      <button id="connect-db-save" style="font-size:12px">Connect</button>

    </div>`;

  overlay.appendChild(box);

  document.body.appendChild(overlay);

  const urlInput = box.querySelector('#connect-db-url');

  const statusEl = box.querySelector('#connect-db-status');

  box.querySelector('#connect-db-cancel').onclick = () => overlay.remove();

  overlay.onclick = e => { if (e.target === overlay) overlay.remove(); };

  box.querySelector('#connect-db-save').onclick = async () => {

    const url = urlInput.value.trim();

    if (!url) { statusEl.textContent = 'Enter a connection string.'; statusEl.style.color = 'var(--danger,#dc2626)'; return; }

    statusEl.textContent = 'Connecting…'; statusEl.style.color = 'var(--muted)';

    try {

      await api('/workspace/connect-db', { method: 'POST', body: JSON.stringify({ url }) });

      statusEl.textContent = 'Connected! Reloading…'; statusEl.style.color = '#059669';

      setTimeout(() => { overlay.remove(); loadProjects(); }, 800);

    } catch (e) {

      statusEl.textContent = e.message || 'Connection failed — check the URL and credentials.';

      statusEl.style.color = 'var(--danger,#dc2626)';

    }

  };

  urlInput.focus();

}



// A persistent "Take the tour" affordance in the sidebar footer so users —

// including paid users on their own dashboard — can (re)play the guided

// walkthrough anytime, not just on first demo visit.

// function ensureTourButton -- moved to dashboard-demo.js / dashboard-timeline.js



// Small, unobtrusive "Send feedback" affordance in the sidebar footer. Tagged

// data-demo-hide so the demo's hideDemoAdminControls() sweep removes it (it

// POSTs to a write endpoint). Opens a lightweight modal — bug/feature/other.

// function ensureFeedbackButton -- moved to dashboard-demo.js / dashboard-timeline.js



// function showFeedbackModal -- moved to dashboard-demo.js / dashboard-timeline.js



function showLocalServerControls() {

  // Server-management buttons are display:none by default in the template so they

  // never flash on hosted/demo loads. Reveal them only once we've confirmed this

  // is a local, non-demo self-hosted instance.

  if (isHostedMode() || isDemoMode()) return;

  ['#git-check-btn', '#restart-server-btn', '#stop-server-btn'].forEach(sel => {

    const el = document.querySelector(sel);

    if (el) el.style.display = '';

  });

}



const STORAGE_KEY = (k) => (isDemoMode() ? 'meridian_demo_' : 'meridian_') + k.replace(/^meridian[._]/, '');

// const QUEUE_DONE_PAGE_SIZE, SESSION_LIVE_WINDOW_MS, DEFAULT_MAX_PINNED_DECISIONS, DEFAULT_CONTEXT_THRESHOLD -- moved to dashboard-utils.js

const NORTH_STAR_MIN_HEIGHT_PX = 180;

const GITHUB_OCTICON_PATH = 'M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z';



// function getPanelState -- moved to dashboard-utils.js



function _summarizeApiErrorText(raw) {

  if (raw === undefined || raw === null) return 'Request failed before data could load.';

  let summary = raw;

  if (typeof raw === 'string') {

    try {

      const parsed = JSON.parse(raw);

      if (parsed && typeof parsed === 'object') {

        summary = parsed.detail || parsed.error || parsed.message || raw;

      }

    } catch (_) {

      summary = raw;

    }

  }

  return String(summary)

    .replace(/<[^>]+>/g, ' ')

    .replace(/\s+/g, ' ')

    .trim()

    .slice(0, 240) || 'Request failed before data could load.';

}



function _projectLoadErrorInfo(path, error) {

  const status = Number.isFinite(Number(error?.status))

    ? Number(error.status)

    : (String(error?.message || '').match(/^(\d{3})\s*:/) ? parseInt(String(error.message).match(/^(\d{3})\s*:/)[1], 10) : null);

  const rawText = error?.responseText || error?.message || String(error || 'Request failed');

  return {

    endpoint: path,

    status,

    summary: _summarizeApiErrorText(rawText),

    at: Date.now(),

  };

}



function wireProjectLoadRetry(container, projectId) {

  container?.querySelectorAll('[data-project-retry]').forEach((btn) => {

    btn.onclick = () => retryProjectSurface(projectId);

  });

}



function renderProjectLoadError(projectId, title, path, error) {

  const info = _projectLoadErrorInfo(path, error);

  const statusLabel = info.status ? `HTTP ${info.status}` : 'Request failed';

  return `

    <div class="project-load-error">

      <div class="project-load-error__title">${escapeHtml(title)}</div>

      <div class="project-load-error__meta">${escapeHtml(statusLabel)} · <code>${escapeHtml(info.endpoint)}</code></div>

      <div class="project-load-error__body">${escapeHtml(info.summary)}</div>

      <div class="project-load-error__actions">

        <button class="secondary" data-project-retry="1" style="padding:4px 10px;font-size:10px">Retry failed loads</button>

      </div>

    </div>

  `;

}



function recordProjectLoadError(projectId, path, error) {

  const panel = getPanelState(projectId);

  panel.loadErrors = panel.loadErrors || {};

  const info = _projectLoadErrorInfo(path, error);

  panel.loadErrors[path] = info;

  renderProjectLoadAlert(projectId);

  return info;

}



function clearProjectLoadError(projectId, path) {

  const panel = getPanelState(projectId);

  if (!panel.loadErrors || !panel.loadErrors[path]) return;

  delete panel.loadErrors[path];

  renderProjectLoadAlert(projectId);

}



function renderProjectLoadAlert(projectId) {

  const host = document.getElementById(`project-fetch-alert-${projectId}`);

  if (!host) return;

  const panel = getPanelState(projectId);

  const errors = Object.values(panel.loadErrors || {}).sort((a, b) => b.at - a.at);

  if (!errors.length) {

    host.style.display = 'none';

    host.innerHTML = '';

    return;

  }

  const visible = errors.slice(0, 3);

  const statusText = visible.length === 1

    ? 'A backing request failed, so part of this panel may be incomplete.'

    : 'Multiple backing requests failed, so part of this panel may be incomplete.';

  const moreText = errors.length > visible.length

    ? `<div class="project-fetch-alert__meta">+${errors.length - visible.length} more failing request${errors.length - visible.length === 1 ? '' : 's'} hidden.</div>`

    : '';

  host.style.display = 'block';

  host.innerHTML = `

    <div class="project-fetch-alert__title">Project data failed to load</div>

    <div class="project-fetch-alert__summary">${escapeHtml(statusText)}</div>

    <div class="project-fetch-alert__list">

      ${visible.map((info) => {

        const statusLabel = info.status ? `HTTP ${info.status}` : 'Request failed';

        return `

          <div class="project-fetch-alert__item">

            <div class="project-fetch-alert__endpoint"><code>${escapeHtml(info.endpoint)}</code></div>

            <div class="project-fetch-alert__meta">${escapeHtml(statusLabel)} · ${escapeHtml(info.summary)}</div>

          </div>

        `;

      }).join('')}

    </div>

    ${moreText}

    <div class="project-fetch-alert__actions">

      <button class="secondary" id="project-fetch-retry-${projectId}" style="padding:4px 10px;font-size:10px">Retry failed loads</button>

    </div>

  `;

  const retryBtn = document.getElementById(`project-fetch-retry-${projectId}`);

  if (retryBtn) retryBtn.onclick = () => retryProjectSurface(projectId);

}



async function retryProjectSurface(projectId) {

  const panel = getPanelState(projectId);

  await Promise.allSettled([

    refreshGoal(projectId),

    refreshSessions(projectId),

    refreshTasks(projectId),

  ]);

  const activeVtab = panel.activeVtab || 'status';

  if (activeVtab === 'live') await refreshLiveTab(projectId);

  if (activeVtab === 'files') await loadFilesTab(projectId);

  if (activeVtab === 'timeline') await loadTimeline(projectId);

  if (activeVtab === 'rewind') await loadRewindTab(projectId, panel.rewindDays || 7);

  if (activeVtab === 'queue') {

    await loadQueue(projectId);

    await updateLiveFeed(projectId);

    await loadRecentRuns(projectId);

  }

  if (activeVtab === 'team') await loadTeamTab(projectId);

  if (activeVtab === 'notes') await loadNotesTab(projectId);

  if (activeVtab === 'hitl') await loadHitlTab(projectId);

  if (activeVtab === 'docs') await loadDocsTab(projectId);

  if (activeVtab === 'settings') await loadSettingsTab(projectId);

  if (activeVtab === 'codeintel') await loadCodeIntelTab(projectId);

}



function syncSidebarActiveProject() {

  document.querySelectorAll('.project-item').forEach(item => {

    item.classList.toggle('active', item.dataset.projectId === state.activeTab);

  });

}



function autosizeGoalField(el, minPx = NORTH_STAR_MIN_HEIGHT_PX) {

  if (!el) return;

  // Use 'auto' not '0px' — avoids the collapse flash before recalculating scrollHeight (97bfb153)

  el.style.height = 'auto';

  el.style.height = `${Math.max(el.scrollHeight, minPx)}px`;

}



function githubIconSvg(size = 12, color = 'currentColor') {

  return `<svg width="${size}" height="${size}" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true" focusable="false" style="color:${color};flex-shrink:0"><path d="${GITHUB_OCTICON_PATH}"></path></svg>`;

}



function getConstitutionLimit(projectId) {

  const panel = getPanelState(projectId);

  const parsed = parseInt(String(panel._projectSettings?.max_pinned_decisions || ''), 10);

  return Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_MAX_PINNED_DECISIONS;

}



async function loadProjectSettings(projectId, opts={}) {

  const panel = getPanelState(projectId);

  if (!opts.force && panel._projectSettings) return panel._projectSettings;

  if (!opts.force && panel._projectSettingsPromise) return panel._projectSettingsPromise;

  panel._projectSettingsPromise = projectApi(projectId, `/projects/${projectId}/settings`)

    .then((settings) => {

      panel._projectSettings = settings || { project_id: projectId, max_pinned_decisions: DEFAULT_MAX_PINNED_DECISIONS };

      return panel._projectSettings;

    })

    .finally(() => {

      panel._projectSettingsPromise = null;

    });

  return panel._projectSettingsPromise;

}



async function saveProjectSettings(projectId, patch) {

  const panel = getPanelState(projectId);

  const settings = await api(`/projects/${projectId}/settings`, {

    method: 'PATCH',

    body: JSON.stringify(patch || {}),

  });

  panel._projectSettings = settings || { project_id: projectId, max_pinned_decisions: DEFAULT_MAX_PINNED_DECISIONS };

  return panel._projectSettings;

}



// Executor Rules + Code Intelligence section. Appended to the settings body by
// the full-settings module (dashboard-settings.js loadSettingsTab) so both render
// together. These two used to collide on the name `loadSettingsTab`, which made
// this section shadow the full settings tab — only this rendered. (fix-settings-tab)
async function loadExecutorRulesSection(projectId) {

  const host = document.getElementById(`settings-body-${projectId}`);

  if (!host) return;

  // Render into a dedicated section appended to the settings body (do NOT replace
  // the body — the module already filled it). Drop any prior copy so re-renders
  // don't duplicate the section.
  const _existing = document.getElementById(`executor-rules-section-${projectId}`);

  if (_existing) _existing.remove();

  const section = document.createElement('div');

  section.id = `executor-rules-section-${projectId}`;

  host.appendChild(section);

  try {

    const [data, defaultData, settingsData] = await Promise.all([

      projectApi(projectId, `/projects/${projectId}/agent-instructions`),

      projectApi(projectId, `/projects/${projectId}/agent-instructions/default`),

      projectApi(projectId, `/projects/${projectId}/settings`),

    ]);

    const current = data.agent_instructions || '';

    const defaultText = defaultData.default_agent_instructions || '';

    const codeIntelEnabled = settingsData ? !!settingsData.code_intel_enabled : false;

    section.innerHTML = `

      <div style="margin-bottom:12px">

        <div style="font-size:11px;font-weight:700;letter-spacing:.5px;color:var(--accent);text-transform:uppercase;margin-bottom:4px">Executor Rules</div>

        <div style="font-size:10px;color:var(--muted);margin-bottom:8px;line-height:1.5">
          These rules are injected into every <code>start_session</code> response so AI coding
          sessions pick them up automatically — no per-repo file required.
        </div>

        <textarea id="agent-instructions-${projectId}"
          rows="24"
          style="width:100%;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:8px;resize:vertical;outline:none;line-height:1.5"
          placeholder="Enter executor rules…"
        >${escapeHtml(current)}</textarea>

        <div style="display:flex;justify-content:space-between;align-items:center;margin-top:6px">

          <span id="agent-instructions-chars-${projectId}" style="font-size:10px;color:var(--muted)">${current.length} chars</span>

          <span style="display:flex;gap:6px">

            <button class="secondary admin-only" id="agent-instructions-reset-${projectId}"
              style="padding:2px 10px;font-size:10px"
              title="Restore Meridian default rules">Reset to defaults</button>

            <button class="primary admin-only" id="agent-instructions-save-${projectId}"
              style="padding:2px 10px;font-size:10px">Save</button>

          </span>

        </div>

      </div>

    `;

    const ta = document.getElementById(`agent-instructions-${projectId}`);

    const charEl = document.getElementById(`agent-instructions-chars-${projectId}`);

    ta.addEventListener('input', () => {

      charEl.textContent = `${ta.value.length} chars`;

    });

    document.getElementById(`agent-instructions-save-${projectId}`).onclick = async () => {

      try {

        await api(`/projects/${projectId}/agent-instructions`, {

          method: 'PATCH',

          body: JSON.stringify({ agent_instructions: ta.value }),

        });

        toast('Executor rules saved');

      } catch (e) { toast('Save failed: ' + e.message, true); }

    };

    document.getElementById(`agent-instructions-reset-${projectId}`).onclick = async () => {

      if (!confirm('Reset to Meridian default executor rules? Your custom rules will be replaced.')) return;

      try {

        const r = await api(`/projects/${projectId}/agent-instructions`, {

          method: 'PATCH',

          body: JSON.stringify({ agent_instructions: null }),

        });

        ta.value = r.agent_instructions || defaultText;

        charEl.textContent = `${ta.value.length} chars`;

        toast('Reset to defaults');

      } catch (e) { toast('Reset failed: ' + e.message, true); }

    };

    // ---- Code Intelligence toggle -------------------------------------------

    const ciBlock = document.createElement('div');

    ciBlock.style.cssText = 'margin-top:18px;padding-top:14px;border-top:1px solid var(--border)';

    ciBlock.innerHTML = `

      <div style="font-size:11px;font-weight:700;letter-spacing:.5px;color:var(--accent);text-transform:uppercase;margin-bottom:4px">Code Intelligence</div>

      <div style="font-size:10px;color:var(--muted);margin-bottom:8px;line-height:1.5">

        Enable to connect <strong>codebase-memory-mcp</strong> — structural graph queries instead of raw file reads.
        120× fewer tokens, sub-ms lookups. <a href="https://github.com/DeusData/codebase-memory-mcp" target="_blank" style="color:var(--accent)">GitHub ↗</a>
      </div>

      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:11px;color:var(--text)">

        <input type="checkbox" id="code-intel-toggle-${projectId}" ${codeIntelEnabled ? 'checked' : ''}
          style="width:14px;height:14px;accent-color:var(--accent);cursor:pointer">

        Enable Code Intelligence for this project

      </label>

      <div id="code-intel-info-${projectId}" style="margin-top:10px;display:${codeIntelEnabled ? 'block' : 'none'}">

        <div style="font-size:10px;color:var(--muted);margin-bottom:4px">Install (once, on the machine running the tunnel):</div>

        <code style="display:block;font-size:10px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:6px 8px;color:var(--text);word-break:break-all">curl -fsSL https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/main/install.sh | bash</code>

        <div style="font-size:10px;color:var(--muted);margin-top:8px;margin-bottom:4px">Add to claude.ai after starting <code>meridian --tunnel</code>:</div>

        <code id="code-intel-url-${projectId}" style="display:block;font-size:10px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:6px 8px;color:var(--text);word-break:break-all">https://usemeridian.us/code/mcp/{your-tenant-id}/mcp</code>

      </div>

    `;

    section.appendChild(ciBlock);

    const ciToggle = document.getElementById(`code-intel-toggle-${projectId}`);

    const ciInfo = document.getElementById(`code-intel-info-${projectId}`);

    ciToggle.onchange = async () => {

      const enabled = ciToggle.checked ? 1 : 0;

      ciInfo.style.display = enabled ? 'block' : 'none';

      try {

        await api(`/projects/${projectId}/settings`, {

          method: 'PATCH',

          body: JSON.stringify({ code_intel_enabled: enabled }),

        });

        toast(enabled ? 'Code Intelligence enabled' : 'Code Intelligence disabled');

      } catch (e) { toast('Save failed: ' + e.message, true); }

    };

  } catch (e) { section.innerHTML = `<div class="empty" style="color:var(--error)">Failed to load executor rules: ${escapeHtml(e.message)}</div>`; }

}


// Tunnel Plugins — per-account (tenant) config for what `meridian --tunnel`
// spawns behind each of the three transport slots (filesystem / code-intel /
// code-extractor). Swapping a slot's command (e.g. code-intel → codegraph) or
// disabling a slot is a pure config change here — no redeploy. Rendered under
// Settings, below Executor Rules. The config is account-scoped, so projectId is
// only used to locate the settings DOM host.
const _TUNNEL_DEFAULT_PORTS = { fs: 8808, code: 8809, extract: 8810, ppt: 8811, word: 8812, dc: 8813 };

// Opt-in slots (Office + Desktop Commander) ship disabled and their launcher is
// not bundled, so a fresh account shows them "not connected" with no obvious next
// step. These per-slot hints give a clear path to fix: the exact launcher command
// + what it needs. Rendered under the slot row only while it isn't connected.
const _OPTIN_SLOT_HINTS = {
  word: { pkg: 'uvx word-mcp-live', note: 'Live Word editing with tracked changes — needs uv (uvx).' },
  ppt: { pkg: 'uvx powerpoint-mcp', note: 'PowerPoint authoring — needs uv (uvx).' },
  dc: { pkg: 'npx -y @wonderwhy-er/desktop-commander@latest', note: 'Desktop Commander, local only — needs Node (npx).' },
};

// Curated, well-known MCP servers a user can drop into a tunnel slot's command.
// Copy-to-clipboard for now (one-click custom-slot install lands in a later
// sprint). Commands use the canonical `uvx` / `npx -y` launchers the tunnel
// already knows how to spawn.
const _CURATED_TUNNEL_PLUGINS = [
  { name: 'Sequential Thinking', command: 'npx -y @modelcontextprotocol/server-sequential-thinking', description: 'Structured step-by-step reasoning', docs: 'https://github.com/modelcontextprotocol/servers' },
  { name: 'Fetch', command: 'uvx mcp-server-fetch', description: 'Fetch & convert web pages to markdown', docs: 'https://github.com/modelcontextprotocol/servers' },
  { name: 'Git', command: 'uvx mcp-server-git', description: 'Read/search/manipulate Git repos', docs: 'https://github.com/modelcontextprotocol/servers' },
  { name: 'Time', command: 'uvx mcp-server-time', description: 'Time & timezone conversion', docs: 'https://github.com/modelcontextprotocol/servers' },
  { name: 'Memory', command: 'npx -y @modelcontextprotocol/server-memory', description: 'Knowledge-graph persistent memory', docs: 'https://github.com/modelcontextprotocol/servers' },
];

// Detect the viewer's OS so we can surface the right dependency-install commands
// for the two launchers tunnel plugins use (uv → uvx, Node → npx). Returns one
// of 'windows' | 'macos' | 'linux' (best-effort; defaults to 'linux').
function _detectTunnelOs() {
  const ua = (navigator.userAgent || '') + ' ' + (navigator.platform || '');
  if (/win/i.test(ua)) return 'windows';
  if (/mac|darwin|iphone|ipad/i.test(ua)) return 'macos';
  return 'linux';
}

// uv (powers `uvx`) + Node.js (powers `npx`) install one-liners per OS.
const _TUNNEL_INSTALL_CMDS = {
  windows: {
    label: 'Windows',
    uv: 'winget install --id=astral-sh.uv -e',
    node: 'winget install OpenJS.NodeJS -e',
  },
  macos: {
    label: 'macOS',
    uv: 'brew install uv',
    node: 'brew install node',
  },
  linux: {
    label: 'Linux',
    uv: 'curl -LsSf https://astral.sh/uv/install.sh | sh',
    node: 'sudo apt-get install -y nodejs npm',
  },
};

// Copy text to the clipboard with a graceful fallback for non-secure contexts
// or browsers without the async Clipboard API.
async function _tunnelCopyToClipboard(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_) { /* fall through to legacy path */ }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch (_) {
    return false;
  }
}

async function loadTunnelPluginsSection(projectId) {
  const host = document.getElementById(`settings-body-${projectId}`);
  if (!host) return;
  const existing = document.getElementById(`tunnel-plugins-section-${projectId}`);
  if (existing) existing.remove();
  const section = document.createElement('div');
  section.id = `tunnel-plugins-section-${projectId}`;
  section.style.cssText = 'margin-top:18px;padding-top:14px;border-top:1px solid var(--border)';
  host.appendChild(section);

  try {
    const data = await api('/tunnel/plugins');
    // The tunnel is a Pro/admin feature — only show this card to those plans.
    const plan = (data && data.plan) || 'free';
    if (!(plan === 'pro' || plan === 'admin' || (data && data.is_admin))) {
      section.remove();
      return;
    }
    const plugins = (data && data.plugins) || [];
    const active = (data && data.active) || {};
    // User-defined (non-built-in) plugins — LOCAL-ONLY: they ride a local
    // mcp-proxy port + the local .mcp.json, never the claude.ai connector.
    // Kept in a mutable array so Add/Remove re-render without a round-trip;
    // collectConfig() merges them into the PUT body alongside the slot overrides.
    const customPlugins = ((data && data.custom) || []).map((c) => ({
      name: String(c.name || ''),
      command: Array.isArray(c.command) ? c.command.join(' ') : String(c.command || ''),
      port: c.port,
      enabled: c.enabled !== false,
    }));

    const renderRow = (p) => {
      const cmd = Array.isArray(p.command) ? p.command.join(' ') : '';
      const dot = active[p.slot] ? 'var(--success, #3fb950)' : 'var(--muted)';
      const dotTitle = active[p.slot] ? 'connected' : 'not connected';
      // Opt-in slots: when not connected, surface the launcher + how to enable it.
      const hint = _OPTIN_SLOT_HINTS[p.slot];
      const hintHtml = (hint && !active[p.slot]) ? `
          <div style="margin-top:6px;font-size:9px;color:var(--muted);line-height:1.6">
            Enable the toggle, then restart <code style="font-family:var(--font-mono)">meridian --tunnel</code> to launch
            <code style="font-family:var(--font-mono)">${escapeHtml(hint.pkg)}</code>.<br>${escapeHtml(hint.note)}
          </div>` : '';
      // Core tools are always-on: show a locked "core" badge instead of an enable
      // toggle. Plugins keep the checkbox. collectConfig keys off .tp-command (on
      // every row), so a core slot's command/port override still saves. (b2a60de7)
      const toggle = p.core
        ? `<span title="core tool — always on" style="font-size:8px;font-weight:700;letter-spacing:.3px;color:var(--muted);border:1px solid var(--border);border-radius:3px;padding:1px 5px;text-transform:uppercase">core</span>`
        : `<input type="checkbox" class="tp-enabled" data-name="${escapeHtml(p.name)}" ${p.enabled ? 'checked' : ''}
                style="width:14px;height:14px;accent-color:var(--accent);cursor:pointer">`;
      return `
        <div style="border:1px solid var(--border);border-radius:4px;padding:8px;margin-bottom:8px">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
            <label style="display:flex;align-items:center;gap:8px;cursor:${p.core ? 'default' : 'pointer'};font-size:11px;color:var(--text);font-weight:600">
              ${toggle}
              ${escapeHtml(p.name)}
              <span style="font-size:9px;color:var(--muted);font-weight:400">/${escapeHtml(p.slot)}</span>
            </label>
            <span title="${dotTitle}" style="display:inline-flex;align-items:center;gap:4px;font-size:9px;color:var(--muted)">
              <span style="width:8px;height:8px;border-radius:50%;background:${dot}"></span>${dotTitle}
            </span>
          </div>
          <div style="display:flex;gap:8px;align-items:center">
            <input type="text" class="tp-command" data-name="${escapeHtml(p.name)}" value="${escapeHtml(cmd)}"
              placeholder="default (${escapeHtml(p.description || 'built-in command')})"
              style="flex:1;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:5px 7px;outline:none">
            <input type="number" class="tp-port" data-name="${escapeHtml(p.name)}" data-slot="${escapeHtml(p.slot)}" value="${p.port}"
              title="local proxy port"
              style="width:74px;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:5px 7px;outline:none">
          </div>
          ${hintHtml}
          <details class="tp-tools" data-slot="${escapeHtml(p.slot)}" data-loaded="0" style="margin-top:6px">
            <summary style="cursor:pointer;list-style:none;font-size:10px;color:var(--accent);user-select:none">&#9656; tools</summary>
            <div class="tp-tools-body" style="margin-top:5px;font-size:10px;color:var(--muted);font-family:var(--font-mono)">&hellip;</div>
          </details>
        </div>`;
    };
    // Split the slots into always-on Core Tools and opt-in Plugins. (b2a60de7)
    const coreRows = plugins.filter((p) => p.core).map(renderRow).join('');
    const pluginRows = plugins.filter((p) => !p.core).map(renderRow).join('');
    const _sectionLabel = (text, note) =>
      `<div style="font-size:9px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin:2px 0 6px">${text} <span style="font-weight:400;text-transform:none">${note}</span></div>`;
    const rows = `
      ${coreRows ? _sectionLabel('Core Tools', '— always on') + coreRows : ''}
      ${_sectionLabel('Plugins', '— opt-in, toggle to enable')}
      ${pluginRows || '<div style="color:var(--muted);font-size:10px">No plugins.</div>'}`;

    const detectedOs = _detectTunnelOs();
    const installCard = (label, cmds, prominent) => `
      <div style="border:1px solid var(--border);border-radius:4px;padding:8px;margin-bottom:6px;background:var(--surface-1)${prominent ? '' : ';opacity:.85'}">
        <div style="font-size:10px;color:var(--text);font-weight:600;margin-bottom:6px">${escapeHtml(label)}${prominent ? ' <span style="color:var(--muted);font-weight:400">(detected)</span>' : ''}</div>
        ${[['uv', 'powers uvx plugins', cmds.uv], ['Node.js', 'powers npx plugins', cmds.node]].map(([dep, note, command]) => `
          <div style="margin-bottom:6px">
            <div style="font-size:9px;color:var(--muted);margin-bottom:3px">${escapeHtml(dep)} <span style="opacity:.8">— ${escapeHtml(note)}</span></div>
            <div style="display:flex;gap:6px;align-items:center">
              <code style="flex:1;box-sizing:border-box;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 7px;overflow-x:auto;white-space:nowrap">${escapeHtml(command)}</code>
              <button class="secondary tp-copy" data-copy="${escapeHtml(command)}" style="padding:2px 8px;font-size:10px;flex-shrink:0">Copy</button>
            </div>
          </div>`).join('')}
      </div>`;

    const otherOsCards = Object.keys(_TUNNEL_INSTALL_CMDS)
      .filter((k) => k !== detectedOs)
      .map((k) => installCard(_TUNNEL_INSTALL_CMDS[k].label, _TUNNEL_INSTALL_CMDS[k], false))
      .join('');

    const installSection = `
      <details style="margin-top:8px;border:1px solid var(--border);border-radius:4px;background:var(--surface-2);padding:0">
        <summary style="cursor:pointer;list-style:none;padding:6px 8px;font-size:10px;font-weight:600;color:var(--accent)">&#9656; Install dependencies</summary>
        <div style="padding:0 8px 8px">
          <div style="font-size:10px;color:var(--muted);margin-bottom:8px;line-height:1.5">
            Tunnel plugins launch via <code>uvx</code> (uv) and <code>npx</code> (Node.js). Install whichever a plugin's command needs.
          </div>
          ${installCard(_TUNNEL_INSTALL_CMDS[detectedOs].label, _TUNNEL_INSTALL_CMDS[detectedOs], true)}
          <details style="margin-top:2px">
            <summary style="cursor:pointer;list-style:none;font-size:10px;color:var(--muted)">&#9656; other platforms</summary>
            <div style="margin-top:6px">${otherOsCards}</div>
          </details>
        </div>
      </details>`;

    const curatedRows = _CURATED_TUNNEL_PLUGINS.map((c) => `
      <div style="border:1px solid var(--border);border-radius:4px;padding:8px;margin-bottom:6px;background:var(--surface-1)">
        <div style="display:flex;align-items:baseline;gap:6px;flex-wrap:wrap;margin-bottom:4px">
          <span style="font-size:11px;color:var(--text);font-weight:600">${escapeHtml(c.name)}</span>
          <a href="${escapeHtml(c.docs)}" target="_blank" rel="noopener" style="font-size:9px;color:var(--accent);text-decoration:none">docs &#8599;</a>
        </div>
        <div style="font-size:10px;color:var(--muted);margin-bottom:5px">${escapeHtml(c.description)}</div>
        <div style="display:flex;gap:6px;align-items:center">
          <code style="flex:1;box-sizing:border-box;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:4px 7px;overflow-x:auto;white-space:nowrap">${escapeHtml(c.command)}</code>
          <button class="secondary tp-copy" data-copy="${escapeHtml(c.command)}" style="padding:2px 8px;font-size:10px;flex-shrink:0">Copy command</button>
        </div>
      </div>`).join('');

    const browseSection = `
      <details style="margin-top:8px;border:1px solid var(--border);border-radius:4px;background:var(--surface-2);padding:0">
        <summary style="cursor:pointer;list-style:none;padding:6px 8px;font-size:10px;font-weight:600;color:var(--accent)">&#9656; Browse plugins</summary>
        <div style="padding:0 8px 8px">
          <div style="font-size:10px;color:var(--muted);margin-bottom:8px;line-height:1.5">
            Well-known MCP servers. Copy a command and paste it into a slot above to swap that slot's launcher.
          </div>
          ${curatedRows}
        </div>
      </details>`;

    // Custom plugins subsection. Renders the user-defined plugins list (each with
    // a Remove button) + an add form (name / command / port). LOCAL-ONLY: each
    // runs as a local mcp-proxy and is written into the local .mcp.json — they
    // never appear in the claude.ai connector. The list lives in `customPlugins`
    // and is re-rendered into #tp-custom-list-${projectId} on every Add/Remove.
    const renderCustomList = () => {
      const listEl = document.getElementById(`tp-custom-list-${projectId}`);
      if (!listEl) return;
      if (!customPlugins.length) {
        listEl.innerHTML = '<div style="color:var(--muted);font-size:10px">No custom plugins yet.</div>';
        return;
      }
      listEl.innerHTML = customPlugins.map((c, i) => `
        <div style="border:1px solid var(--border);border-radius:4px;padding:8px;margin-bottom:6px;display:flex;gap:8px;align-items:center">
          <div style="flex:1;min-width:0">
            <div style="font-size:11px;color:var(--text);font-weight:600">${escapeHtml(c.name)}
              <span style="font-size:9px;color:var(--muted);font-weight:400">:${escapeHtml(String(c.port))}</span></div>
            <div style="font-size:10px;color:var(--muted);font-family:var(--font-mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(c.command)}</div>
          </div>
          <button class="secondary tp-custom-remove" data-idx="${i}" style="padding:2px 8px;font-size:10px;flex-shrink:0">Remove</button>
        </div>`).join('');
      listEl.querySelectorAll('.tp-custom-remove').forEach((btn) => {
        btn.addEventListener('click', () => {
          const idx = parseInt(btn.dataset.idx, 10);
          if (Number.isInteger(idx)) { customPlugins.splice(idx, 1); renderCustomList(); }
        });
      });
    };

    const customSection = `
      <details style="margin-top:8px;border:1px solid var(--border);border-radius:4px;background:var(--surface-2);padding:0">
        <summary style="cursor:pointer;list-style:none;padding:6px 8px;font-size:10px;font-weight:600;color:var(--accent)">&#9656; Custom plugins</summary>
        <div style="padding:0 8px 8px">
          <div style="font-size:10px;color:var(--muted);margin-bottom:8px;line-height:1.5">
            Add your own MCP server. Runs locally as <code>http://127.0.0.1:&lt;port&gt;</code> and is written
            into this machine's <code>.mcp.json</code> for a co-located Cursor / Claude Code session.
            Local-only — it does not appear in the claude.ai connector. Use a port outside 8808–8813.
          </div>
          <div id="tp-custom-list-${projectId}" style="margin-bottom:8px"></div>
          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
            <input type="text" id="tp-custom-name-${projectId}" placeholder="name (e.g. fetch)"
              style="width:120px;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:5px 7px;outline:none">
            <input type="text" id="tp-custom-command-${projectId}" placeholder="command (e.g. uvx mcp-server-fetch)"
              style="flex:1;min-width:160px;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:5px 7px;outline:none">
            <input type="number" id="tp-custom-port-${projectId}" placeholder="port"
              style="width:74px;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:5px 7px;outline:none">
            <button class="secondary admin-only" id="tp-custom-add-${projectId}" style="padding:2px 10px;font-size:10px;flex-shrink:0">Add</button>
          </div>
        </div>
      </details>`;

    section.innerHTML = `
      <details class="meridian-disclosure" open style="border:1px solid var(--border);border-radius:6px;background:var(--surface-2);padding:0">
      <summary style="cursor:pointer;list-style:none;padding:8px 10px;font-size:11px;font-weight:700;letter-spacing:.5px;color:var(--accent);text-transform:uppercase">Tunnel Plugins</summary>
      <div style="padding:0 10px 10px">
      <div style="font-size:10px;color:var(--muted);margin-bottom:8px;line-height:1.5">
        What <code>meridian --tunnel</code> spawns behind each transport slot. Leave a command
        blank for the built-in default, or set one to swap it (e.g. <code>code-intel</code> →
        <code>codegraph</code>). Changes apply the next time the tunnel restarts.
      </div>
      ${rows || '<div style="color:var(--muted);font-size:10px">No plugins.</div>'}
      <div style="display:flex;justify-content:flex-end;gap:6px;margin-top:6px">
        <button class="secondary admin-only" id="tp-reset-${projectId}" style="padding:2px 10px;font-size:10px" title="Clear all overrides (back to built-in defaults)">Reset to defaults</button>
        <button class="primary admin-only" id="tp-save-${projectId}" style="padding:2px 10px;font-size:10px">Save</button>
      </div>
      <div id="tp-status-${projectId}" style="font-size:10px;color:var(--muted);margin-top:4px;text-align:right"></div>
      ${customSection}
      ${installSection}
      ${browseSection}
      </div>
      </details>`;

    const statusEl = document.getElementById(`tp-status-${projectId}`);
    const setStatus = (m) => { if (statusEl) { statusEl.textContent = m; if (m) setTimeout(() => { if (statusEl.textContent === m) statusEl.textContent = ''; }, 2500); } };

    const collectConfig = () => {
      const cfg = [];
      // Iterate .tp-command (present on every built-in row) rather than .tp-enabled
      // (core rows have no checkbox) so a core slot's command/port override still
      // persists. A core row with no override + no enabled toggle is skipped.
      section.querySelectorAll('.tp-command').forEach((cmdEl) => {
        const name = cmdEl.dataset.name;
        const portEl = section.querySelector(`.tp-port[data-name="${CSS.escape(name)}"]`);
        const enEl = section.querySelector(`.tp-enabled[data-name="${CSS.escape(name)}"]`);
        const entry = { name };
        if (enEl) entry.enabled = enEl.checked;  // plugins only; core stays default-on
        const cmdVal = (cmdEl.value || '').trim();
        if (cmdVal) entry.command = cmdVal;
        const portVal = parseInt(portEl && portEl.value, 10);
        const slot = portEl && portEl.dataset.slot;
        if (Number.isInteger(portVal) && portVal !== _TUNNEL_DEFAULT_PORTS[slot]) entry.port = portVal;
        // Skip empty core rows (name only — no override, no toggle).
        if (entry.command !== undefined || entry.port !== undefined || entry.enabled !== undefined) {
          cfg.push(entry);
        }
      });
      // Merge in the user-defined custom plugins (name + command + port + enabled).
      // The server keeps non-built-in names, so these round-trip back as data.custom.
      customPlugins.forEach((c) => {
        const name = (c.name || '').trim();
        const command = (c.command || '').trim();
        const port = parseInt(c.port, 10);
        if (!name || !command || !Number.isInteger(port)) return;
        cfg.push({ name, command, port, enabled: c.enabled !== false });
      });
      return cfg;
    };

    document.getElementById(`tp-save-${projectId}`).onclick = async () => {
      try {
        await api('/tunnel/plugins', { method: 'PUT', body: JSON.stringify({ config: collectConfig() }) });
        toast('Tunnel plugins saved');
        setStatus('Saved — restart the tunnel to apply.');
      } catch (e) { toast('Save failed: ' + e.message, true); }
    };

    document.getElementById(`tp-reset-${projectId}`).onclick = async () => {
      if (!confirm('Reset tunnel plugins?\n\nThis clears ALL command and port overrides for every slot and returns them to Meridian\'s built-in defaults. This cannot be undone.')) return;
      try {
        await api('/tunnel/plugins', { method: 'PUT', body: JSON.stringify({ config: [] }) });
        toast('Reset to defaults');
        loadTunnelPluginsSection(projectId);
      } catch (e) { toast('Reset failed: ' + e.message, true); }
    };

    // Custom plugins: initial render + Add-form wiring.
    renderCustomList();
    const _addCustom = () => {
      const nameEl = document.getElementById(`tp-custom-name-${projectId}`);
      const cmdEl = document.getElementById(`tp-custom-command-${projectId}`);
      const portEl = document.getElementById(`tp-custom-port-${projectId}`);
      const name = (nameEl && nameEl.value || '').trim();
      const command = (cmdEl && cmdEl.value || '').trim();
      const port = parseInt(portEl && portEl.value, 10);
      if (!name || !command || !Number.isInteger(port)) {
        toast('Custom plugin needs a name, command, and port', true);
        return;
      }
      if (port < 1024 || port > 65535 || [8808, 8809, 8810, 8811, 8812, 8813].includes(port)) {
        toast('Pick a port in 1024–65535 and outside 8808–8813', true);
        return;
      }
      if (customPlugins.some((c) => c.name === name)) {
        toast(`A custom plugin named "${name}" already exists`, true);
        return;
      }
      customPlugins.push({ name, command, port, enabled: true });
      if (nameEl) nameEl.value = '';
      if (cmdEl) cmdEl.value = '';
      if (portEl) portEl.value = '';
      renderCustomList();
    };
    const _addBtn = document.getElementById(`tp-custom-add-${projectId}`);
    if (_addBtn) _addBtn.addEventListener('click', _addCustom);

    // Copy buttons (install commands + curated plugin commands).
    section.querySelectorAll('.tp-copy').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const ok = await _tunnelCopyToClipboard(btn.dataset.copy || '');
        if (ok) {
          const prev = btn.textContent;
          btn.textContent = 'Copied';
          setTimeout(() => { btn.textContent = prev; }, 1200);
        } else {
          toast('Copy failed — select and copy manually', true);
        }
      });
    });

    // Per-plugin live tools dropdown. Lazy-load the slot's tool manifest the
    // first time its <details> is expanded; reuse one /me lookup across slots.
    let _tenantIdPromise = null;
    const _getTenantId = () => {
      if (!_tenantIdPromise) {
        _tenantIdPromise = api('/me').then((m) => (m && m.tenant_id) || null).catch(() => null);
      }
      return _tenantIdPromise;
    };

    section.querySelectorAll('.tp-tools').forEach((det) => {
      det.addEventListener('toggle', async () => {
        if (!det.open || det.dataset.loaded === '1') return;
        det.dataset.loaded = '1';
        const slot = det.dataset.slot;
        const bodyEl = det.querySelector('.tp-tools-body');
        if (!bodyEl) return;
        // Not active? Don't even bother hitting the proxy.
        if (!active[slot]) {
          bodyEl.innerHTML = '<span style="color:var(--muted)">not connected — start the tunnel</span>';
          det.dataset.loaded = '0';
          return;
        }
        bodyEl.textContent = 'loading…';
        try {
          const tenantId = await _getTenantId();
          if (!tenantId) throw new Error('no tenant');
          const r = await fetch(`/${slot}/mcp/${tenantId}/mcp`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream' },
            body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/list', params: {} }),
          });
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          const text = await r.text();
          let parsed = null;
          if (text.trim().startsWith('{')) {
            parsed = JSON.parse(text);
          } else {
            for (const line of text.split('\n')) {
              if (line.startsWith('data:')) {
                try { parsed = JSON.parse(line.slice(5).trim()); } catch (_) {}
              }
            }
          }
          if (!parsed) throw new Error('empty response');
          if (parsed.error) throw new Error(parsed.error.message || String(parsed.error));
          const tools = (parsed.result && parsed.result.tools) || [];
          if (!tools.length) {
            bodyEl.innerHTML = '<span style="color:var(--muted)">no tools reported</span>';
            return;
          }
          bodyEl.innerHTML = `<div style="color:var(--muted);margin-bottom:3px">${tools.length} tool${tools.length !== 1 ? 's' : ''}</div>` +
            tools.map((t) => `<div style="color:var(--text)">${escapeHtml(t && t.name || String(t))}</div>`).join('');
        } catch (e) {
          bodyEl.innerHTML = `<span style="color:var(--muted)">not connected — start the tunnel</span>`;
          det.dataset.loaded = '0';
        }
      });
    });
  } catch (e) {
    section.innerHTML = `<div class="empty" style="color:var(--error)">Failed to load tunnel plugins: ${escapeHtml(e.message)}</div>`;
  }
}



// function hideDemoAdminControls -- moved to dashboard-demo.js / dashboard-timeline.js



// function toast -- moved to dashboard-utils.js



// function showDemoReadonlyToast -- moved to dashboard-demo.js / dashboard-timeline.js



// function showDemoOnboardingOverlay -- moved to dashboard-demo.js / dashboard-timeline.js



// ---------------------------------------------------------------------------

// Demo guided tour — step-by-step tooltip walkthrough

// ---------------------------------------------------------------------------

// Data-driven tab/subtab tree. Each step optionally opens a main vtab (and a

// goal subtab) on the active project so the tip lands on the surface it

// describes. position is relative to the highlighted element.

const _DEMO_TOUR_STEPS = [

  {

    vtab: null,

    target: () => document.querySelector('.session-list, .sidebar-sessions, [data-tour="sessions"], .sidebar'),

    title: 'AI coding sessions',

    body: 'Each row is one Claude Code run. Multiple sessions work in parallel on the same project — no collisions.',

    position: 'right',

  },

  {

    vtab: 'status',

    title: 'Status & sessions',

    body: 'The default panel: live session status and the task log. Every meaningful action a session takes shows up here in real time.',

    position: 'bottom',

  },

  {

    vtab: 'live',

    title: 'Live view',

    body: 'A right-now feed of what every active session is doing this second — tool calls, file claims, and progress as they happen.',

    position: 'bottom',

  },

  {

    vtab: 'goal',

    gtab: 'north-star',

    title: 'Shared goal state',

    body: 'The north star and version goal every session reads on startup — so parallel runs stay aligned on one plan.',

    position: 'bottom',

  },

  {

    vtab: 'goal',

    gtab: 'decisions',

    title: 'Pinned decisions',

    body: 'An append-only constitution of architectural calls. New sessions inherit them automatically instead of relitigating settled choices.',

    position: 'bottom',

  },

  {

    vtab: 'queue',

    title: 'Work queue',

    body: 'Pending tasks claimed atomically — parallel sessions grab work without stepping on each other.',

    position: 'bottom',

  },

  {

    vtab: 'timeline',

    title: 'Activity timeline',

    body: 'Every session laid out over time — when each ran, what changed, and how long each task took.',

    position: 'bottom',

  },

  {

    vtab: 'files',

    title: 'Files',

    body: 'File claims and previews. Sessions lock the files they are editing so two runs never clobber the same file.',

    position: 'bottom',

  },

  {

    vtab: 'hitl',

    title: 'Human-in-the-loop',

    body: 'When a session needs a human decision it parks the question here and waits — you answer, it resumes. No silent guessing.',

    position: 'bottom',

  },

  {

    vtab: 'notes',

    title: 'Project notes',

    body: 'A shared per-project wiki every session can read and append to — context that outlives any single run.',

    position: 'bottom',

  },

  {

    vtab: 'settings',

    title: 'Settings',

    body: 'Notifications, hooks, and integrations. In your own project this is where you wire Meridian into your AI tools.',

    position: 'bottom',

  },

  {

    vtab: null,

    target: () => null,  // centered finish step

    title: 'You\'re all set',

    body: 'Explore any project or session. When you\'re ready to coordinate your own AI sessions — sign in and create a project.',

    position: 'center',

  },

];



// --- Tour persistence (per-demo localStorage; never auto-shown once finished) -

function _demoTourDone() {

  try { return localStorage.getItem(STORAGE_KEY('tour.done')) === '1'; } catch(e) { return false; }

}

function _demoTourSavedStep() {

  try { return parseInt(localStorage.getItem(STORAGE_KEY('tour.step')) || '0', 10) || 0; } catch(e) { return 0; }

}

function _demoTourSaveStep(step) {

  try { localStorage.setItem(STORAGE_KEY('tour.step'), String(step)); } catch(e) {}

}

function _demoTourMarkDone() {

  try {

    localStorage.setItem(STORAGE_KEY('tour.done'), '1');

    localStorage.removeItem(STORAGE_KEY('tour.step'));

  } catch(e) {}

}

function _demoTourClose() {

  document.getElementById('demo-tour-tooltip')?.remove();

  document.getElementById('demo-tour-highlight')?.remove();

}



// Open a main vtab (and optional goal subtab) on the active project so the

// step's tip lands on the right surface. No-op if the panel isn't mounted yet.

function _tourActivateVtab(vtab, gtab) {

  const pid = state.activeTab;

  if (!pid || !vtab) return;

  const btn = document.querySelector(`#vtab-strip-${pid} .vtab-btn[data-vtab="${vtab}"]`);

  if (btn) btn.click();

  if (gtab) {

    const gbtn = document.querySelector(`#drawer-goal-${pid} .goal-subtab-btn[data-gtab="${gtab}"]`);

    if (gbtn) gbtn.click();

  }

}



function startDemoTour(step) {

  _demoTourClose();



  if (step < 0) step = 0;

  if (step >= _DEMO_TOUR_STEPS.length) { _demoTourMarkDone(); return; }

  // Persist progress so closing the tooltip / reopening the demo resumes here.

  _demoTourSaveStep(step);

  const s = _DEMO_TOUR_STEPS[step];



  // Surface the tab/subtab this step describes, then let it render before

  // measuring the highlight target.

  try { _tourActivateVtab(s.vtab, s.gtab); } catch(e) {}



  const isLast = step === _DEMO_TOUR_STEPS.length - 1;

  let targetEl = null;

  if (s.target) {

    targetEl = s.target();

  } else if (s.vtab) {

    const pid = state.activeTab;

    targetEl = pid ? document.querySelector(`#vtab-strip-${pid} .vtab-btn[data-vtab="${s.vtab}"]`) : null;

  }



  // Highlight ring around target element

  if (targetEl) {

    const rect = targetEl.getBoundingClientRect();

    const ring = document.createElement('div');

    ring.id = 'demo-tour-highlight';

    ring.style.cssText = `position:fixed;z-index:29998;pointer-events:none;

      top:${rect.top - 4}px;left:${rect.left - 4}px;

      width:${rect.width + 8}px;height:${rect.height + 8}px;

      border:2px solid #7c3aed;border-radius:8px;

      box-shadow:0 0 0 4000px rgba(0,0,0,0.45);`;

    document.body.appendChild(ring);

  }



  // Tooltip card

  const tip = document.createElement('div');

  tip.id = 'demo-tour-tooltip';

  const stepLabel = `${step + 1} / ${_DEMO_TOUR_STEPS.length}`;

  tip.innerHTML = `

    <div style="font-size:.82rem;color:#6c8fff;font-weight:600;margin-bottom:8px;letter-spacing:.3px">${stepLabel}</div>

    <div style="font-size:1.12rem;font-weight:700;color:#e8eaf0;margin-bottom:10px">${s.title}</div>

    <div style="font-size:.98rem;color:#c4c6d4;line-height:1.65;margin-bottom:18px">${s.body}</div>

    <div style="display:flex;gap:8px;align-items:center">

      <button id="demo-tour-finish" style="background:none;border:none;color:#6b7280;cursor:pointer;font-size:.86rem;padding:4px 6px;font-family:inherit;text-decoration:underline">Finish tutorial</button>

      <div style="flex:1"></div>

      ${step > 0 ? '<button id="demo-tour-back" style="background:none;border:1px solid #3a3d48;border-radius:6px;color:#c4c6d4;cursor:pointer;font-size:.9rem;padding:7px 13px;font-family:inherit">← Back</button>' : ''}

      <button id="demo-tour-next" style="background:#7c3aed;border:none;border-radius:6px;color:#fff;cursor:pointer;font-size:.92rem;padding:7px 18px;font-family:inherit">

        ${isLast ? 'Done' : 'Next →'}

      </button>

    </div>`;

  tip.style.cssText = `position:fixed;z-index:30000;background:#1e2029;border:1px solid #7c3aed88;

    border-radius:10px;padding:18px 22px;width:330px;max-width:calc(100vw - 24px);

    box-shadow:0 8px 32px rgba(0,0,0,0.6);font-family:inherit;`;



  // Position tooltip relative to target or center

  if (!targetEl || s.position === 'center') {

    tip.style.top = '50%';

    tip.style.left = '50%';

    tip.style.transform = 'translate(-50%, -50%)';

  } else {

    const rect = targetEl.getBoundingClientRect();

    const PAD = 12;

    if (s.position === 'right') {

      tip.style.top = `${Math.min(rect.top, window.innerHeight - 200)}px`;

      tip.style.left = `${rect.right + PAD}px`;

    } else {  // bottom

      tip.style.top = `${Math.min(rect.bottom + PAD, window.innerHeight - 200)}px`;

      tip.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - 342))}px`;

    }

  }



  document.body.appendChild(tip);

  // Next on the last step marks the tour done (never auto-shown again).

  document.getElementById('demo-tour-next').onclick = () => {

    if (isLast) { _demoTourClose(); _demoTourMarkDone(); }

    else startDemoTour(step + 1);

  };

  const backBtn = document.getElementById('demo-tour-back');

  if (backBtn) backBtn.onclick = () => startDemoTour(step - 1);

  // Explicit Finish: mark done so the tour never auto-shows again.

  document.getElementById('demo-tour-finish').onclick = () => {

    _demoTourClose();

    _demoTourMarkDone();

  };

}



// Resume the tour at the saved step (or step 0). Does nothing if finished.

function resumeDemoTour() {

  if (_demoTourDone()) return;

  startDemoTour(_demoTourSavedStep());

}



async function api(path, opts={}) {

  const headers = {'Content-Type': 'application/json'};

  if (state.activeWorkspaceTenantId) {

    headers['X-Workspace-Tenant-Id'] = state.activeWorkspaceTenantId;

  }

  const r = await fetch(path, { headers, ...opts });

  if (!r.ok) {

    if (r.status === 403 && isDemoMode()) {

      showDemoReadonlyToast();

      throw new Error('demo_readonly');

    }

    const text = await r.text();

    const err = new Error(`${r.status}: ${text}`);

    err.status = r.status;

    err.endpoint = path;

    err.responseText = text;

    throw err;

  }

  return r.status === 204 ? null : r.json();

}



const _staleProjectsHandled = new Set();



async function projectApi(projectId, path, opts={}) {

  try {

    const data = await api(path, opts);

    clearProjectLoadError(projectId, path);

    return data;

  } catch (e) {

    // Self-heal stale tabs: a "project not found" 404 for a project that isn't

    // in the current account's project list means the signed-in account changed

    // (often in another tab). Close the orphaned tab and prompt a refresh once,

    // instead of spamming every panel with 404s until the user reloads.

    if (e && e.status === 404 && /project not found/i.test(e.responseText || '')

        && !(state.projects || []).some(p => p.id === projectId)

        && !_staleProjectsHandled.has(projectId)) {

      _staleProjectsHandled.add(projectId);

      try { closeTab(projectId); } catch (_) {}

      try { _checkAccountSwitch(); } catch (_) {}

      throw e;

    }

    recordProjectLoadError(projectId, path, e);

    throw e;

  }

}



async function loadServerConfig() {

  // v0.6.5 — pull /config so the dashboard can show the version and

  // (in hosted deployments) target a non-localhost server_url.

  try {

    const cfg = await api('/config');

    state.serverConfig = cfg || state.serverConfig;

    const verEl = document.getElementById('server-version');

    if (verEl && cfg?.version) verEl.textContent = `v${cfg.version}`;

    // v2.0-fixes — demo mode banner

    if (cfg?.demo_mode && !document.getElementById('demo-mode-banner')) {

      const b = document.createElement('div');

      b.id = 'demo-mode-banner';

      b.style = 'position:fixed;top:0;left:0;right:0;z-index:9999;background:#7c3aed;color:#fff;text-align:center;padding:4px 12px;font-size:11px;font-family:inherit;letter-spacing:0.02em';

      b.innerHTML = 'Preview mode — read only · <a href="/auth/login" style="color:#fff;text-decoration:underline;font-weight:600">Sign in →</a>';

      document.body.prepend(b);

      document.body.style.paddingTop = ((parseInt(document.body.style.paddingTop || '0', 10)) + 22) + 'px';

      // Demo onboarding overlay — self-guards once the tour is finished

      // (localStorage), and its CTA resumes the tour at the saved step.

      // Auto-start the demo tour instead of showing overlay

      if (!_demoTourDone()) {

        resumeDemoTour();

      }

    }

    // Task 16 — hide destructive admin controls in demo mode

    if (cfg?.demo_mode) hideDemoAdminControls();

    // v1.9.x — update connection indicator

    _updateConnectionIndicator(cfg);

  } catch (e) { /* offline / older server — ignore */ }

  // Show demo overlay whenever on /demo path (regardless of MERIDIAN_DEMO env var)

  if (window.location.pathname.startsWith('/demo')) {

    // Clear stale project IDs from prior logins so /demo never 404s on a stale project.

    try { localStorage.removeItem(STORAGE_KEY(TABS_KEY)); } catch(e) {}

    try { localStorage.removeItem(STORAGE_KEY(ACTIVE_PROJECT_KEY)); } catch(e) {}

    hideDemoAdminControls();

    showDemoOnboardingOverlay();

  }

  // v2.9 — plan badge + expiry banner for hosted users

  try {

    const me = await api('/me');

    if (me && me.plan) {

      state.tenantPlan = me.plan;

      state.tenantEmail = me.email || '';

      state.tenantHasStripe = !!me.has_stripe_customer;

      state.tenantIsInternal = !!me.is_internal;

      state.tenantDaysRemaining = me.days_remaining;

      state.tenantExpired = !!me.expired;

      state.tenantExpiresAt = me.inactivity_expires_at || null;

      _renderPlanBadge(me);

      updateGitHubConnectionIndicator(me);

      updateTunnelConnectionIndicator(me);

      _armAccountSwitchWatch(me.email || '');

    }

  } catch (e) { /* not hosted or not logged in */ }

}



// function _renderPlanBadge -- moved to dashboard-sprint.js



// Detect when the active session belongs to a different account than the one

// this page loaded as (e.g. the user signed into another account in a second

// tab). Re-auth replaces the session cookie underneath the loaded page, so its

// in-flight API calls would start 404ing against the wrong workspace. Rather

// than let that happen silently, watch /me and prompt a refresh.

function _armAccountSwitchWatch(loadedEmail) {

  if (!isHostedMode()) return;

  if (state._acctWatchArmed) return;

  state._acctWatchArmed = true;

  state.loadedAccountEmail = loadedEmail || '';

  // Re-check when the tab regains focus (the common case: switch account in

  // another tab, then come back) and on a slow interval as a backstop.

  document.addEventListener('visibilitychange', () => {

    if (document.visibilityState === 'visible') {

      _checkAccountSwitch();

      // Refresh active project data when tab regains focus so planning-chat

      // changes (new decisions, notes, goal updates) appear without manual refresh

      _refreshOnFocus();

    }

  });

  // Poll for new projects + active project data every 8 seconds.

  // Catches changes from planning-chat MCP calls even when dashboard is always visible.

  // ITEM 6 — 10s project-list poll removed. WS push events (note_added,
  // decision_pinned, sprint_item_added, goal_updated, hitl_filed) now drive live
  // section refreshes, and _refreshOnFocus catches new projects on tab focus.

  setInterval(_checkAccountSwitch, 60000);

}



async function _refreshOnFocus() {

  // Called when dashboard tab regains focus — refreshes data changed externally

  // (e.g. planning chat adding decisions/notes/goal via MCP)

  try { await loadProjects(); } catch(_) {}

  const pid = state.activeTab;

  if (!pid) return;

  try {

    await Promise.allSettled([

      refreshGoal(pid),

      loadPinnedDecisions(pid),

      loadNotesTab(pid),

      refreshProjectCountBadges(pid),

      refreshHitl(pid),

      loadSprintBoard(pid),

    ]);

  } catch(_) {}

}

async function _checkAccountSwitch() {

  if (document.getElementById('account-switch-banner')) return; // already shown

  let me;

  try {

    me = await api('/me');

  } catch (_) {

    return; // network blip or logged out entirely — don't false-alarm

  }

  const now = (me && me.email) || '';

  const base = state.loadedAccountEmail || '';

  if (now && base && now !== base) _showAccountSwitchBanner(now);

}



function _showAccountSwitchBanner(newEmail) {

  if (document.getElementById('account-switch-banner')) return;

  const b = document.createElement('div');

  b.id = 'account-switch-banner';

  b.style = 'position:fixed;top:0;left:0;right:0;z-index:10000;background:#b45309;color:#fff;text-align:center;padding:6px 12px;font-size:12px;font-family:inherit;letter-spacing:0.02em;display:flex;align-items:center;justify-content:center;gap:12px';

  b.innerHTML =

    `<span>You're now signed in as <strong>${escapeHtml(newEmail)}</strong> in another tab. ` +

    `Refresh to load this account.</span>` +

    `<button id="account-switch-refresh" style="background:#fff;color:#b45309;font-weight:700;border:none;text-decoration:none;padding:2px 12px;border-radius:4px;white-space:nowrap;cursor:pointer">Refresh</button>`;

  document.body.prepend(b);

  document.body.style.paddingTop = ((parseInt(document.body.style.paddingTop || '0', 10)) + 30) + 'px';

  const btn = document.getElementById('account-switch-refresh');

  if (btn) btn.onclick = () => location.reload();

}



// v1.9.x — show active DB connection in sidebar footer

function updateGitHubConnectionIndicator(source) {

  const badge = document.getElementById('connection-github');

  if (!badge || !source) return;

  const connected = !!(source.github_connected ?? source.connected);

  const repo = source.github_repo || source.repo || '';

  const branch = source.github_branch || source.branch || 'main';

  badge.style.display = connected ? 'inline-flex' : 'none';

  badge.title = connected

    ? (repo ? `GitHub repo connected: ${repo} (${branch})` : 'GitHub repo connected')

    : 'GitHub repo not connected';

}



// b43b0c6a — Pro tunnel status dot in sidebar footer
function updateTunnelConnectionIndicator(me) {

  const wrap = document.getElementById('connection-tunnel');

  if (!wrap || !me) return;

  const isPro = me.plan === 'pro' || me.plan === 'admin' || me.is_internal;

  if (!isPro) { wrap.style.display = 'none'; return; }

  const active = !!me.tunnel_active;

  const dot = document.getElementById('connection-tunnel-dot');

  wrap.style.display = 'inline-flex';

  wrap.title = active ? 'Pro tunnel connected' : 'Pro tunnel disconnected — run `meridian --tunnel`';

  if (dot) dot.style.background = active ? '#22c55e' : '#ef4444';

  wrap.style.borderColor = active ? '#22c55e55' : 'var(--border)';

  wrap.style.color = active ? '#22c55e' : 'var(--muted)';

}



function _updateConnectionIndicator(cfg) {

  if (!cfg) return;

  // Hosted non-admin users stay on the managed DB. Hosted admins keep the

  // full selector so they can inspect auth/demo/local connections.

  if (isHostedMode() && !isHostedAdmin()) return;

  const wrap = document.getElementById('connection-indicator');

  const label = document.getElementById('connection-label');

  const dot = document.getElementById('connection-dot');

  const switcher = document.getElementById('connection-switcher');

  if (!wrap || !label) return;

  wrap.style.display = 'inline-flex';

  // Demo mode: show simplified read-only badge, no switcher

  if (cfg.demo_mode) {

    label.textContent = 'demo (' + (cfg.demo_db || 'sqlite') + ')';

    dot.style.background = 'var(--accent-green)';

    wrap.style.cursor = 'default';

    wrap.title = 'Demo environment — read only';

    wrap.onclick = null;

    return;

  }

  const name = cfg.connection_name || (cfg.db === 'postgres' ? 'postgres' : 'local');

  const dbType = cfg.db || 'sqlite';

  // 1f92d344: show hostname for env postgres connections

  let connLabelText = name + ' (' + dbType + ')';

  if (cfg.db_host) connLabelText += ': ' + cfg.db_host;

  label.textContent = connLabelText;

  dot.style.background = dbType === 'postgres' ? 'var(--accent)' : 'var(--accent-green)';

  // Make indicator clickable to show connection popup

  const conns = cfg.connections || [];

  if (conns.length > 0 && wrap) {

    wrap.style.cursor = 'pointer';

    wrap.title = 'Click to switch connection';

    wrap.onclick = (e) => {

      e.stopPropagation();

      document.querySelectorAll('.conn-popup').forEach(p => p.remove());

      const popup = document.createElement('div');

      popup.className = 'conn-popup';

      popup.style.cssText = 'position:fixed;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;z-index:1001;min-width:180px;box-shadow:0 4px 12px rgba(0,0,0,0.4);font-size:11px;font-family:var(--font-mono);padding:6px 0';

      const rect = wrap.getBoundingClientRect();

      popup.style.bottom = (window.innerHeight - rect.top + 4) + 'px';

      popup.style.left = rect.left + 'px';

      // Header

      const hdr = document.createElement('div');

      hdr.style.cssText = 'padding:4px 12px;color:var(--muted);font-size:10px;border-bottom:1px solid var(--border);margin-bottom:4px';

      hdr.textContent = 'Select connection';

      popup.appendChild(hdr);

      // Connection options.

      // Use cfg.connection_name as truth for which connection is active

      // (env var overrides can make the toml active flag stale).

      const hosted = isHostedMode();

      // On hosted (admin or not), only show postgres connections — local/sqlite

      // connections are useless on Fly (ephemeral FS, no toml on the machine).

      // Self-hosted (non-hosted) admins see everything including local.

      const adminFull = !hosted || isHostedAdmin();

      const activeName = cfg.connection_name || (cfg.db === 'postgres' ? 'env (postgres)' : 'local');

      let displayConns = (conns || []).map(c => ({...c, active: c.name === activeName}));

      if (hosted) {

        // Both normal hosted users and hosted admins only see postgres connections.

        // Local/sqlite options would break tenant isolation or are simply not

        // meaningful on a hosted server.

        displayConns = displayConns.filter(c => (c.type || 'sqlite') === 'postgres');

      }

      if (!displayConns.find(c => c.active)) {

        displayConns.unshift({name: activeName, type: cfg.db, active: true});

      }

      displayConns.forEach(c => {

        const item = document.createElement('div');

        item.style.cssText = `padding:6px 12px;cursor:pointer;display:flex;align-items:center;gap:8px;justify-content:space-between;${c.active ? 'color:var(--accent)' : 'color:var(--text)'}`;

        const left = document.createElement('div');

        left.style.cssText = 'display:flex;align-items:center;gap:8px;flex:1;min-width:0';

        const dot2 = document.createElement('span');

        dot2.style.cssText = `display:inline-block;width:6px;height:6px;border-radius:50%;flex-shrink:0;background:${c.active ? 'var(--accent)' : 'var(--muted)'}`;

        left.appendChild(dot2);

        // Task 11: show truncated hostname for postgres connections

        let connLabel = c.name + ' (' + (c.type || 'sqlite') + ')';

        if (c.url_masked) {

          try {

            const hostMatch = c.url_masked.match(/@([^/:?]+)/);

            if (hostMatch) {

              const host = hostMatch[1];

              connLabel += ' — ' + (host.length > 22 ? host.slice(0, 20) + '…' : host);

            }

          } catch(_) {}

        }

        left.appendChild(document.createTextNode(connLabel));

        // Task 11: warning badge for non-prod connection names

        const _nonprod = /\b(dev|test|staging|sandbox)\b/i;

        if (_nonprod.test(c.name)) {

          const badge = document.createElement('span');

          badge.textContent = '⚠';

          badge.title = 'Non-production connection';

          badge.style.cssText = 'color:var(--accent-yellow,#f5a623);font-size:11px;flex-shrink:0;margin-left:2px';

          left.appendChild(badge);

        }

        item.appendChild(left);

        // Delete button for any named non-local connection. Normal hosted users

        // can't delete (removing the managed postgres would orphan every tenant);

        // admins manage connections fully.

        if (c.name && c.name !== 'local' && adminFull) {

          const del = document.createElement('button');

          del.textContent = '×';

          del.title = c.active ? 'Remove connection (will switch to local)' : 'Remove connection';

          del.style.cssText = 'background:none;border:none;color:var(--muted);cursor:pointer;font-size:14px;padding:0 2px;line-height:1;flex-shrink:0';

          del.onmouseenter = () => del.style.color = 'var(--status-failed)';

          del.onmouseleave = () => del.style.color = 'var(--muted)';

          del.onclick = async (e) => {

            e.stopPropagation();

            const msg = c.active

              ? 'Remove active connection "' + c.name + '"? This will switch to local (SQLite). Requires restart.'

              : 'Remove connection "' + c.name + '"?';

            if (!confirm(msg)) return;

            try {

              await api('/config/connections/' + encodeURIComponent(c.name), { method: 'DELETE' });

              popup.remove();

              await loadServerConfig();

            } catch(ex) { toast('Remove failed: ' + ex.message, true); }

          };

          item.appendChild(del);

        }

        item.onmouseenter = () => { if (!c.active) left.style.color = 'var(--accent)'; item.style.background = 'var(--surface-3)'; };

        item.onmouseleave = () => { left.style.color = ''; item.style.background = ''; };

        item.onclick = async (e) => {

          if (e.target.tagName === 'BUTTON') return; // don't activate on delete click

          popup.remove();

          if (c.active) return; // already active — nothing to switch (avoids 404 on the synthetic "env" connection)

          try {

            await api('/config/connections', { method: 'POST', body: JSON.stringify({ name: c.name, activate: true }) });

            await loadServerConfig();

            // Show restart banner — DB connection change needs restart

            // On hosted the restart button is hidden (a restart kills the

            // shared Fly machine), so don't advertise "restart to apply" \u2014

            // just confirm the change was saved for the next deploy/restart.

            if (isHostedMode()) {

              toast('Connection saved as "' + c.name + '" \u2014 applies on next server restart');

            } else {

              const banner = document.getElementById('update-banner');

              const bannerSpan = banner?.querySelector('span');

              if (banner) {

                banner.style.display = 'block';

                if (bannerSpan) bannerSpan.textContent = '\u26A0\uFE0F Connection changed to ' + c.name + ' \u2014 restart to apply';

              }

            }

            // Update indicator immediately

            const dot = document.getElementById('connection-dot');

            const label = document.getElementById('connection-label');

            if (dot) dot.style.background = 'var(--accent)';

            if (label) label.textContent = c.name + ' (' + (c.type || 'sqlite') + ')';

          } catch(e) { console.error('Switch failed:', e); toast('Switch failed: ' + e.message, true); }

        };

        popup.appendChild(item);

      });

      // "+ Add connection..." and the toml path are local-config affordances —

      // hidden for normal hosted users, shown to admins managing the server.

      if (adminFull) {

        const addItem = document.createElement('div');

        addItem.style.cssText = 'padding:6px 12px;cursor:pointer;color:var(--muted);border-top:1px solid var(--border);margin-top:4px';

        addItem.textContent = '+ Add connection...';

        addItem.onmouseenter = () => addItem.style.color = 'var(--text)';

        addItem.onmouseleave = () => addItem.style.color = 'var(--muted)';

        addItem.onclick = () => { popup.remove(); document.getElementById('conn-setup-modal').style.display = 'flex'; };

        popup.appendChild(addItem);

      }

      // Config file path at bottom — only for local self-host (path is meaningless on hosted)

      if (cfg.toml_path && adminFull && !hosted) {

        const pathRow = document.createElement('div');

        pathRow.style.cssText = 'padding:4px 12px 6px;color:var(--muted);font-size:9px;border-top:1px solid var(--border);margin-top:2px;word-break:break-all';

        pathRow.textContent = '📄 ' + cfg.toml_path;

        popup.appendChild(pathRow);

      }

      document.body.appendChild(popup);

      setTimeout(() => document.addEventListener('click', () => popup.remove(), { once: true }), 0);

    };

  }

  if (conns.length > 1 && switcher) {

    switcher.style.display = 'none'; // replaced by popup

    switcher.innerHTML = conns.map(c =>

      `<option value="${c.name}" ${c.active ? 'selected' : ''}>${c.name}</option>`

    ).join('');

    switcher.onchange = async () => {

      try {

        const sel = switcher.value;

        const conn = (cfg.connections || []).find(c => c.name === sel) || {};

        await api('/config/connections', {

          method: 'POST',

          body: JSON.stringify({ name: sel, type: conn.type || 'sqlite', activate: true }),

        });

        if (isHostedMode()) {

          // Never restart the shared machine from a hosted instance — the

          // connection is saved and applies on the next server restart/deploy.

          toast('Connection saved as "' + sel + '" — applies on next server restart');

        } else if (conn.type === 'postgres') {

          toast('Switching to ' + sel + ' — restarting…');

          await _doRestart();

        } else {

          toast('Switched to ' + sel + ' — restart to apply');

        }

      } catch(e) { toast('Switch failed: ' + e.message, true); }

    };

  }

}



// v1.9.x — POST /admin/restart then poll /health until up, then reload.

async function checkGitStatus() {

  if (isDemoMode()) {

    hideDemoAdminControls();

    return;

  }

  if (isHostedMode()) return;

  const btn = document.getElementById('git-check-btn');

  if (btn) { btn.textContent = 'checking…'; btn.style.color = 'var(--muted)'; }

  try {

    const s = await api('/admin/git-status');

    if (!s.ok) throw new Error(s.error || 'git check failed');

    if (s.behind > 0) {

      const banner = document.getElementById('update-banner');

      const span = banner?.querySelector('span');

      if (banner) {

        banner.style.display = 'block';

        if (span) span.textContent = `\u26A0\uFE0F ${s.behind} commit${s.behind>1?'s':''} behind origin/${s.branch} (${s.local_hash} \u2260 ${s.remote_hash}) \u2014 git pull recommended`;

      }

      if (btn) { btn.textContent = `\u2193 ${s.behind} behind`; btn.style.color = 'var(--status-failed)'; }

    } else {

      if (btn) { btn.textContent = '\u2713 up to date'; btn.style.color = 'var(--status-done)'; }

      setTimeout(() => { if (btn) { btn.textContent = 'check updates'; btn.style.color = 'var(--muted)'; } }, 3000);

    }

  } catch(e) {

    if (btn) { btn.textContent = 'check updates'; btn.style.color = 'var(--muted)'; }

  }

}



async function _doRestart(confirmFirst = true) {

  if (confirmFirst &&

      !confirm('This will restart the server and disconnect all active sessions on this machine. Are you sure?')) {

    return;

  }

  // confirm:true tells the server we acknowledge the all-sessions disconnect;

  // without it /admin/restart returns a requires_confirm warning instead.

  try {

    await fetch('/admin/restart', {

      method: 'POST',

      headers: { 'Content-Type': 'application/json' },

      body: JSON.stringify({ confirm: true }),

    });

  } catch(_) { /* expected */ }

  // Replace any existing restart button text

  document.querySelectorAll('#restart-server-btn, #banner-restart-btn').forEach(b => {

    b.textContent = 'Restarting…'; b.disabled = true;

  });

  const started = Date.now();

  while (Date.now() - started < 30000) {

    await new Promise(r => setTimeout(r, 2000));

    try {

      const r = await fetch('/health');

      if (r.ok) { window.location.href = window.location.pathname + '?_cb=' + Date.now(); return; }

    } catch(_) { /* server still down — keep polling */ }

  }

  // Timed out

  document.querySelectorAll('#restart-server-btn, #banner-restart-btn').forEach(b => {

    b.textContent = 'Restart timed out'; b.disabled = false;

  });

  toast('Server did not come back within 30s — start manually', true);

}



async function loadConfig() {

  try {

    const cfg = await api('/config/api-key');

    state.apiKeyConfigured = !!cfg.configured;

    const hintEl = document.getElementById('mcp-hint');

    if (hintEl) hintEl.style.display = cfg.configured ? 'none' : 'block';

    const methodEl = document.getElementById('auth-method');

    if (cfg.method === 'oauth') {

      methodEl.textContent = 'Auth: Claude Max OAuth';

      methodEl.style.display = 'block';

    } else if (cfg.method === 'api_key') {

      methodEl.textContent = 'Auth: API key';

      methodEl.style.display = 'block';

    } else {

      methodEl.style.display = 'none';

    }

  } catch (e) { /* ignore */ }

}



async function loadProjects() {

  const list = document.getElementById('project-list');

  try {

    state.projects = await api('/projects');

  } catch (e) {

    state.projects = [];

    if (list) {

      list.innerHTML = `<div class="empty" style="color:var(--status-failed);padding:6px 4px">projects failed: ${escapeHtml(e.message)}</div>`;

    }

    return;

  }

  list.innerHTML = '';

  state.projects.forEach(p => {

    const div = document.createElement('div');

    div.className = 'project-item' + (state.activeTab === p.id ? ' active' : '');

    div.dataset.projectId = p.id;

    div.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:4px;';

    const nameSpan = document.createElement('span');

    nameSpan.style.cssText = 'flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';

    nameSpan.textContent = p.name;

    // id shown in kebab menu instead

    const menuBtn = document.createElement('button');

    menuBtn.textContent = '⋯';

    menuBtn.title = 'Project actions';

    menuBtn.style.cssText = 'background:none;border:none;color:var(--muted);cursor:pointer;padding:0 4px;font-size:14px;line-height:1;flex-shrink:0';

    menuBtn.onmouseenter = () => menuBtn.style.color = 'var(--text)';

    menuBtn.onmouseleave = () => menuBtn.style.color = 'var(--muted)';

    menuBtn.onclick = (e) => {

      e.stopPropagation();

      // Find or create a fake tab object for _openTabMenu

      let t = state.tabs.find(tab => tab.id === p.id);

      if (!t) { openTab(p); t = state.tabs.find(tab => tab.id === p.id); }

      if (t) _openTabMenu(t, menuBtn);

    };

    div.appendChild(nameSpan);

    div.appendChild(menuBtn);

    div.onclick = (e) => { if (e.target !== menuBtn) openTab(p); };

    list.appendChild(div);

  });

  // v1.9.x — hide the legacy dropdown; keep element for switcher.value compat.

  const switcher = document.getElementById('project-switcher');

  if (switcher) {

    switcher.style.display = 'none';

    const previous = switcher.value;

    switcher.innerHTML = '';

    state.projects.forEach(p => {

      const opt = document.createElement('option');

      opt.value = p.id; opt.textContent = p.name;

      switcher.appendChild(opt);

    });

    if (previous && state.projects.some(p => p.id === previous)) switcher.value = previous;

  }

  syncSidebarActiveProject();

}



// function escapeHtml -- moved to dashboard-utils.js



// v0.5.1 — sessions render with relative timestamps so the user can

// see at a glance which workers are actually alive. SQLite stores

// timestamps in UTC without timezone markers; we treat them as UTC.

// function formatRelativeTime, sessionAgeMs, isLiveSession -- moved to dashboard-utils.js



function openTab(project) {

  const existing = state.tabs.find(t => t.id === project.id);

  if (existing) { activateTab(project.id); return; }

  state.tabs.push({ id: project.id, project });

  saveTabs();

  renderTabs();

  buildTabBody(project);

  activateTab(project.id);

  // G1.2 — defer badge population until after the dashboard's initial

  // fetch wave settles. Without the delay, the extra parallel /notes and

  // /decisions-pinned fetches push refreshGoal's expected 404 outside

  // browsers' HTTP/1.1 6-connection window, surfacing it as a console

  // error during the panel-render test's 2.5s wait. 100ms is well below

  // human-perceptible latency.

  setTimeout(() => refreshProjectCountBadges(project.id), 100);

}



function closeTab(id) {

  state.tabs = state.tabs.filter(t => t.id !== id);

  const panel = state.panels[id];

  if (panel) {

    try { panel.ws && panel.ws.close(); } catch(e){}

    delete state.panels[id];

  }

  document.getElementById(`tab-body-${id}`)?.remove();

  saveTabs();

  renderTabs();

  if (state.activeTab === id) {

    const next = state.tabs[state.tabs.length - 1];

    state.activeTab = next ? next.id : null;

    if (next) activateTab(next.id);

    else document.getElementById('tab-bodies').innerHTML = '<div class="empty">no project open — pick one on the left</div>';

  }

  syncSidebarActiveProject();

}



function saveTabs() {

  try {

    localStorage.setItem(STORAGE_KEY(TABS_KEY), JSON.stringify(state.tabs.map(t => t.id)));

  } catch(e) {}

}



const TAB_OVERFLOW_THRESHOLD = 10;



function renderTabs() {

  const bar = document.getElementById('tabs');

  bar.innerHTML = '';



  // v1.9.x — overflow: show first (N-1) tabs + ">>" button if 10+

  const overflow = state.tabs.length >= TAB_OVERFLOW_THRESHOLD;

  const visible = overflow ? state.tabs.slice(0, TAB_OVERFLOW_THRESHOLD - 1) : state.tabs;

  const hidden  = overflow ? state.tabs.slice(TAB_OVERFLOW_THRESHOLD - 1) : [];



  visible.forEach(t => bar.appendChild(_makeTabEl(t)));



  if (overflow) {

    const more = document.createElement('div');

    more.className = 'tab tab-overflow';

    more.textContent = `>> ${hidden.length} more`;

    more.title = hidden.map(t => t.project.name).join(', ');

    // Click opens a small dropdown of hidden tabs.

    more.onclick = (e) => {

      e.stopPropagation();

      let menu = document.getElementById('tab-overflow-menu');

      if (menu) { menu.remove(); return; }

      menu = document.createElement('div');

      menu.id = 'tab-overflow-menu';

      menu.style.cssText = 'position:fixed;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;z-index:1000;min-width:160px;box-shadow:0 4px 12px rgba(0,0,0,0.4)';

      const rect = more.getBoundingClientRect();

      menu.style.top = (rect.bottom + 4) + 'px';

      menu.style.left = rect.left + 'px';

      hidden.forEach(t => {

        const item = document.createElement('div');

        item.style.cssText = 'padding:8px 12px;cursor:pointer;font-size:11px;font-family:var(--font-mono)';

        item.textContent = t.project.name;

        item.onmouseenter = () => item.style.background = 'var(--surface-1)';

        item.onmouseleave = () => item.style.background = '';

        item.onclick = () => { menu.remove(); activateTab(t.id); };

        menu.appendChild(item);

      });

      document.body.appendChild(menu);

      const close = () => { menu.remove(); document.removeEventListener('click', close); };

      setTimeout(() => document.addEventListener('click', close), 0);

    };

    bar.appendChild(more);

  }

}



function _makeTabEl(t) {

  const div = document.createElement('div');

  div.className = 'tab' + (state.activeTab === t.id ? ' active' : '');

  div.dataset.tabId = t.id;

  div.onclick = () => activateTab(t.id);



  // G4.17 — icon (single emoji) renders before the name.

  if (t.project.icon) {

    const iconSpan = document.createElement('span');

    iconSpan.textContent = t.project.icon;

    iconSpan.style.cssText = 'margin-right:5px;font-size:1.05em';

    div.appendChild(iconSpan);

  }



  const nameSpan = document.createElement('span');

  nameSpan.textContent = t.project.name;

  div.appendChild(nameSpan);



  // v1.9.x — kebab ⋯ menu

  const kebab = document.createElement('button');

  kebab.className = 'tab-kebab';

  kebab.textContent = '⋯';

  kebab.title = 'Project actions';

  kebab.onclick = (e) => { e.stopPropagation(); _openTabMenu(t, kebab); };

  div.appendChild(kebab);



  const close = document.createElement('button');

  close.className = 'close';

  close.textContent = '×';

  close.onclick = (e) => { e.stopPropagation(); closeTab(t.id); };

  div.appendChild(close);



  return div;

}



function _openTabMenu(t, anchor) {

  // Close any existing menu.

  document.querySelectorAll('.tab-context-menu').forEach(m => m.remove());



  const menu = document.createElement('div');

  menu.className = 'tab-context-menu';

  menu.style.cssText = 'position:fixed;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;z-index:1001;min-width:150px;box-shadow:0 4px 12px rgba(0,0,0,0.4);font-size:11px;font-family:var(--font-mono)';



  function menuItem(label, fn) {

    const item = document.createElement('div');

    item.style.cssText = 'padding:8px 12px;cursor:pointer';

    item.textContent = label;

    item.onmouseenter = () => { item.style.background = 'var(--surface-3)'; item.style.color = 'var(--accent)'; };

    item.onmouseleave = () => { item.style.background = ''; item.style.color = ''; };

    item.onclick = () => { menu.remove(); fn(); };

    menu.appendChild(item);

  }



  // UUID header

  const uuidDiv = document.createElement('div');

  uuidDiv.style.cssText = 'padding:6px 12px;color:var(--muted);font-size:10px;border-bottom:1px solid var(--border);user-select:all;cursor:text';

  uuidDiv.textContent = t.id;

  menu.appendChild(uuidDiv);

  menuItem('\u270f Rename', () => _renameProject(t));

  menuItem('\ud83c\udfa8 Change icon\u2026', () => _setProjectIcon(t));

  menuItem('\u2b07 Download DB', () => window.open('/admin/snapshot', '_blank'));

  menuItem('🗑 Delete project…', () => _deleteProject(t));



  const rect = anchor.getBoundingClientRect();

  menu.style.top = (rect.bottom + 4) + 'px';

  menu.style.left = rect.left + 'px';

  document.body.appendChild(menu);

  const dismiss = () => { menu.remove(); document.removeEventListener('click', dismiss); };

  setTimeout(() => document.addEventListener('click', dismiss), 0);

}



async function _setProjectIcon(t) {

  /** G4.17 — quick prompt-based emoji picker. Paste one emoji or leave

   * blank to clear. Image uploads are intentionally NOT supported here —

   * see the v1.0.1 backlog note for "project image upload". */

  const current = t.project.icon || '';

  const next = window.prompt(

    `Paste a single emoji to use as the project icon (or leave blank to clear).\n\nCurrent: ${current || '(none)'}`,

    current,

  );

  if (next === null) return;  // cancelled

  const icon = next.trim() ? next.trim().slice(0, 8) : null;

  try {

    const updated = await api(`/projects/${t.id}/icon`, {

      method: 'PATCH', body: JSON.stringify({ icon }),

    });

    t.project = { ...t.project, icon: updated.icon || null };

    // Update in-place: tabs (header + sidebar), project switcher.

    const proj = state.projects.find(p => p.id === t.id);

    if (proj) proj.icon = updated.icon || null;

    renderTabs();

    toast(icon ? `Icon set to ${icon}` : 'Icon cleared');

  } catch (e) {

    toast('Update failed: ' + e.message, true);

  }

}



async function _renameProject(t) {

  const newName = window.prompt(`Rename "${t.project.name}" to:`, t.project.name);

  if (!newName || newName.trim() === t.project.name) return;

  try {

    const updated = await api(`/projects/${t.id}/rename`, {

      method: 'POST', body: JSON.stringify({ name: newName.trim() }),

    });

    t.project = { ...t.project, name: updated.name };

    // Update body header if open.

    const hdr = document.querySelector(`#drawer-status-${t.id} .drawer-header span:first-child`);

    if (hdr) hdr.textContent = 'STATUS · ' + updated.name;

    renderTabs();

    toast('Renamed to ' + updated.name);

  } catch(e) { toast('Rename failed: ' + e.message, true); }

}



async function _deleteProject(t) {
  await new Promise((resolve) => {
    if (document.getElementById('delete-project-modal')) return resolve();
    const overlay = document.createElement('div');
    overlay.id = 'delete-project-modal';
    overlay.style.cssText = 'position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center';
    const box = document.createElement('div');
    box.style.cssText = 'background:var(--surface-0);border:1px solid var(--border);border-radius:8px;padding:24px 28px;width:420px;max-width:94vw;display:flex;flex-direction:column;gap:14px';
    const name = escapeHtml(t.project.name);
    box.innerHTML = `<div style="font-weight:700;font-size:14px;color:var(--danger,#dc2626)">Delete "${name}"?</div><div style="font-size:12px;color:var(--muted)">Permanently deletes all sessions, tasks, decisions, and goal history. <strong>Cannot be undone.</strong></div><div style="display:flex;gap:8px;justify-content:flex-end"><button id="del-proj-cancel" class="secondary" style="font-size:12px">Cancel</button><button id="del-proj-confirm" style="font-size:12px;background:var(--danger,#dc2626);color:#fff;border:none;border-radius:5px;padding:6px 16px;cursor:pointer">Delete project</button></div>`;
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    box.querySelector('#del-proj-cancel').onclick = () => { overlay.remove(); resolve(); };
    overlay.onclick = e => { if (e.target === overlay) { overlay.remove(); resolve(); } };
    box.querySelector('#del-proj-confirm').onclick = async () => {
      overlay.remove();
      try {
        await api(`/projects/${t.id}`, { method: 'DELETE' });
        closeTab(t.id);
        state.projects = state.projects.filter(p => p.id !== t.id);
        await loadProjects();
        toast('Project deleted');
      } catch(e) {
        if (e.status === 404) {
          closeTab(t.id); state.projects = state.projects.filter(p => p.id !== t.id);
          await loadProjects(); toast('Project removed');
        } else {
          toast(e.message.includes('409') ? 'Cannot delete — active tasks in progress.' : 'Delete failed: ' + e.message, true);
        }
      }
      resolve();
    };
  });
}



function activateTab(id) {

  state.activeTab = id;

  renderTabs();

  syncSidebarActiveProject();

  document.querySelectorAll('.tab-body').forEach(el => el.classList.remove('active'));

  const body = document.getElementById(`tab-body-${id}`);

  if (body) body.classList.add('active');

  // clear empty placeholder

  const empty = document.querySelector('.tab-bodies > .empty');

  if (empty) empty.remove();

  // Persist active project so a refresh reopens to the same tab.

  try { localStorage.setItem(STORAGE_KEY(ACTIVE_PROJECT_KEY), id); } catch(e) {}

  // Keep the sidebar dropdown in sync with whichever tab the user is on.

  const switcher = document.getElementById('project-switcher');

  if (switcher) switcher.value = id;

}



function buildTabBody(project) {

  const root = document.getElementById('tab-bodies');

  const empty = root.querySelector(':scope > .empty');

  if (empty) empty.remove();



  const body = document.createElement('div');

  body.className = 'tab-body';

  body.id = `tab-body-${project.id}`;

  body.innerHTML = `

    <div class="vtab-strip" id="vtab-strip-${project.id}">

      <button class="vtab-btn active" data-vtab="status" title="Status &amp; Sessions" aria-label="Status and sessions">📊</button>

      <button class="vtab-btn" data-vtab="live" title="Live — right-now view">⚡</button>

      <button class="vtab-btn" data-vtab="goal" title="Goal State">🎯</button>

      ${(window.MERIDIAN_HOSTED && !(project.github_repo || project.repo)) ? '' : '<button class="vtab-btn" data-vtab="files" title="Files">📁</button>'}

      <button class="vtab-btn" data-vtab="devlog" title="Dev Log">📓</button>

      <button class="vtab-btn" data-vtab="timeline" title="Activity Timeline">📅</button>

      <button class="vtab-btn" data-vtab="rewind" title="Rewind — Last X days">↻</button>

      <button class="vtab-btn" data-vtab="queue" title="Work Queue">👷</button>

      <button class="vtab-btn" data-vtab="team" title="Team — per-human activity">👥</button>

      <button class="vtab-btn" data-vtab="notes" title="Notes — per-project wiki" style="position:relative">📝<span class="notes-vtab-badge vtab-count-badge muted" data-pid="${project.id}" style="display:none;position:absolute;top:2px;right:2px;background:var(--surface-3,#2a2f3a);color:var(--muted);font-size:8px;font-weight:700;padding:0 3px;border-radius:6px;line-height:14px;pointer-events:none">0</span></button>

      <button class="vtab-btn" data-vtab="hitl" title="HITL — Human-in-the-Loop queue" style="position:relative">❓<span class="hitl-vtab-badge vtab-count-badge" data-pid="${project.id}" style="display:none;position:absolute;top:2px;right:2px;background:#f87171;color:#fff;font-size:8px;font-weight:700;padding:0 3px;border-radius:6px;line-height:14px;pointer-events:none">0</span></button>

      <button class="vtab-btn" data-vtab="docs" title="MCP Tool Reference">📖</button>

      <button class="vtab-btn" data-vtab="settings" title="Notification Settings">⚙</button>

      <button class="vtab-btn" data-vtab="codeintel" title="Code Intel — codebase index &amp; architecture" id="vtab-codeintel-${project.id}" style="display:none">🔍</button>

    </div>

    <div class="vtab-drawer open" id="drawer-${project.id}">

      <div class="project-fetch-alert" id="project-fetch-alert-${project.id}"></div>

      <div class="drawer-panel active" id="drawer-status-${project.id}">

        <div class="drawer-header">

          <span>STATUS · ${escapeHtml(project.name)}</span>

          <span class="ws-dot" id="ws-${project.id}"></span>

        </div>

        <div style="flex:1;overflow-y:auto">

          <div class="section">

            <h3>Active Sessions</h3>

            <div class="sessions-list" id="sessions-${project.id}"></div>

          </div>

          <div class="hitl-banner" id="hitl-banner-${project.id}" style="display:none">HITL queue</div>

          <div id="hitl-queue-${project.id}"></div>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-live-${project.id}">

        <div class="drawer-header" style="justify-content:space-between">

          <span>LIVE · ${escapeHtml(project.name)}</span>

          <span style="display:flex;gap:6px;align-items:center">

            <button class="secondary" id="live-auto-btn-${project.id}" title="Toggle auto-refresh" style="padding:2px 8px;font-size:10px">↻ Auto</button>

            <button class="secondary" id="live-pause-${project.id}" title="Pause queue (UI stub)" style="padding:2px 8px;font-size:10px">Pause</button>

            <button class="secondary" id="live-run-${project.id}" title="Run all pending (UI stub)" style="padding:2px 8px;font-size:10px">Run All</button>

          </span>

        </div>

        <div class="live-body" id="live-body-${project.id}">

          <div class="live-section">

            <div class="live-section-label">Sprint progress</div>

            <div id="live-sprint-progress-${project.id}" class="live-sprint-progress"></div>

          </div>

          <hr class="live-divider">

          <div class="live-section">

            <div class="live-section-label">Active sessions</div>

            <div class="live-sessions" id="live-sessions-${project.id}">

              <div class="live-empty">No active sessions.</div>

            </div>

          </div>

          <hr class="live-divider">

          <div class="live-section" id="sprint-notes-section-${project.id}" style="display:none">

            <div class="live-section-label">Sprint notes (session scratch pad)</div>

            <div id="sprint-notes-${project.id}" style="font-size:11px"></div>

          </div>

          <hr class="live-divider" id="sprint-notes-divider-${project.id}" style="display:none">

          <div class="live-section">

            <div class="live-section-label" style="display:flex;justify-content:space-between;align-items:center">

              <span>Queue</span>

              <button class="secondary" id="new-sprint-btn-${project.id}" style="padding:1px 8px;font-size:9px" title="Start a new sprint">+ New Sprint</button>

            </div>

            <div class="live-queue" id="live-queue-${project.id}">

              <div class="live-empty">Queue is empty. Add a task above.</div>

            </div>

            <div class="live-add-row">

              <input type="text" class="live-add-input" id="live-add-input-${project.id}" placeholder="+ Add task… (Enter to submit)">

            </div>

          </div>

          <hr class="live-divider">

          <div class="live-section">

            <div class="live-section-label" style="display:flex;justify-content:space-between;align-items:center">

              <span>Add to run</span>

              <button class="secondary" id="add-to-run-toggle-${project.id}" style="padding:1px 8px;font-size:9px">+ Expand</button>

            </div>

            <div id="add-to-run-area-${project.id}" style="display:none;margin-top:6px">

              <textarea id="add-to-run-text-${project.id}" rows="3" placeholder="Describe what to add to the active session's goal…"

                style="width:100%;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:11px;font-family:var(--font-mono);padding:5px 8px;resize:vertical;outline:none"></textarea>

              <div style="display:flex;justify-content:flex-end;gap:6px;margin-top:4px">

                <button class="secondary" id="add-to-run-cancel-${project.id}" style="padding:2px 8px;font-size:10px">Cancel</button>

                <button class="primary" id="add-to-run-submit-${project.id}" data-project="${escapeHtml(project.id)}" style="padding:2px 10px;font-size:10px">→ Send to run</button>

              </div>

            </div>

          </div>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-goal-${project.id}">

        <div class="drawer-header" style="justify-content:space-between">

          <span style="display:flex;flex-direction:column;gap:1px">

            <span>GOAL · ${escapeHtml(project.name)}</span>

            <span style="font-size:9px;letter-spacing:0;text-transform:none;font-weight:400;opacity:0.7">Share your project context with AI sessions — north star, sprint, version goal</span>

          </span>

          <span style="display:flex;gap:6px;align-items:center">

            <span class="goal-version" id="goal-version-${project.id}"></span>

          </span>

        </div>

        <div class="goal-subtab-strip">

          <button class="goal-subtab-btn active" data-gtab="north-star" title="Permanent product vision. Rarely changes — set once, then keep stable.">🔭 North Star</button>

          <button class="goal-subtab-btn" data-gtab="version-goal" title="Current milestone — what ships this cycle (v1.2, v2.0, etc).">🎯 Version Goal</button>

          <button class="goal-subtab-btn" data-gtab="sprint" title="What this session is focused on right now — updated multiple times per day. Not a multi-week scrum sprint.">⚡ Session Focus</button>

          <button class="goal-subtab-btn" data-gtab="decisions" title="Pinned constitution + append-only decisions log.">📋 Decisions <span class="decisions-gtab-badge vtab-count-badge muted" data-pid="${project.id}" style="display:none;background:var(--surface-3,#2a2f3a);color:var(--muted);font-size:9px;font-weight:700;padding:0 5px;border-radius:8px;line-height:14px;margin-left:4px;vertical-align:1px">0</span></button>

        </div>

        <div class="goal-subtab-body">

          <div class="goal-subtab-panel active" id="gtab-north-star-${project.id}">

            <div style="color:var(--muted);font-size:10px;margin-bottom:6px">Permanent vision. Set once, change rarely or never.</div>

            <textarea class="goal-area goal-full mono" id="goal-north-star-${project.id}" placeholder="(north star not set — set once, rarely change)" style="overflow-y:hidden;min-height:0"></textarea>

            <div class="goal-actions">

              <button class="primary" id="save-north-star-${project.id}">save north star</button>

              <span class="goal-ts" id="goal-ns-ts-${project.id}"></span>

              <span id="goal-ns-lock-${project.id}" style="opacity:0.5;font-size:11px"></span>

            </div>

          </div>

          <div class="goal-subtab-panel" id="gtab-version-goal-${project.id}">

            <div style="color:var(--muted);font-size:10px;margin-bottom:6px">Current milestone — what ships this cycle (v1.2, v2.0, etc).</div>

            <div id="goal-title-${project.id}" style="font-family:var(--font-mono);font-size:11px;font-weight:600;color:var(--accent);padding:5px 8px;background:var(--surface-2);border:1px solid var(--border);border-radius:4px 4px 0 0;border-bottom:none;user-select:none;flex-shrink:0;white-space:pre-wrap;overflow:visible" title="Version title (read-only)"></div>

            <div id="goal-shipped-${project.id}" style="display:none;font-family:var(--font-mono);font-size:10px;color:var(--muted);padding:6px 8px;background:var(--surface-2);border:1px solid var(--border);border-top:none;border-bottom:none;white-space:pre-wrap;user-select:none;flex-shrink:0" title="SHIPPED section (read-only — updated by Claude Code)"></div>

            <textarea class="goal-area goal-full mono" id="goal-${project.id}" placeholder="CURRENT FOCUS:" style="border-radius:0 0 4px 4px;font-size:13px"></textarea>

            <div class="goal-actions" style="flex-shrink:0">

              <button class="primary" id="save-goal-${project.id}">save version goal</button>

              <span class="goal-version" id="goal-state-${project.id}"></span>

              <span class="goal-ts" id="goal-vg-ts-${project.id}"></span>

            </div>

            <div id="goal-autoblocks-wrapper-${project.id}" style="display:none;flex-shrink:0">

              <button onclick="(function(b,c){var open=c.style.display!=='none';c.style.display=open?'none':'block';b.textContent=open?'📋 Session Log ▶':'📋 Session Log ▼';})(this,document.getElementById('goal-autoblocks-${project.id}'))" style="background:none;border:none;color:var(--muted);font-size:10px;font-weight:600;cursor:pointer;padding:2px 0;font-family:var(--font-mono);margin-top:6px">📋 Session Log ▶</button>

              <div id="goal-autoblocks-${project.id}" style="display:none;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:8px;font-family:var(--font-mono);font-size:13px;color:var(--text);white-space:pre-wrap;word-break:break-word;margin-top:4px"></div>

            </div>

          </div>

          <div class="goal-subtab-panel" id="gtab-sprint-${project.id}">

            <div style="color:var(--muted);font-size:10px;margin-bottom:4px">What this session is doing right now. Updated frequently — not a multi-week scrum sprint.</div>

            <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px;align-items:center">

              <select id="goal-sprint-select-${project.id}" style="flex:1;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:6px 8px;color:var(--muted);font-family:var(--font-mono);font-size:11px;outline:none"><option value="" disabled selected>loading sessions…</option></select>

              <input type="text" id="goal-sprint-${project.id}" placeholder="v1.0.x — description" style="flex:1;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:6px 8px;color:var(--muted);font-family:var(--font-mono);font-size:11px;outline:none;display:none">

              <button class="secondary" id="save-sprint-${project.id}" style="white-space:nowrap">save</button>

              <span class="goal-ts" id="goal-sp-ts-${project.id}" style="font-size:10px;color:var(--muted)"></span>

            </div>

            <div id="sprint-board-goal-${project.id}"></div>

          </div>

          <div class="goal-subtab-panel" id="gtab-decisions-${project.id}">

            <div style="margin-bottom:14px">

              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">

                <div style="color:var(--accent);font-weight:600;font-size:12px">📌 Pinned (Constitution)</div>

                <div style="display:flex;gap:6px">

                  <button class="secondary" id="consolidate-decisions-${project.id}" style="padding:3px 10px;font-size:10px" title="Use AI to deduplicate and merge decisions">✨ Consolidate</button>

                  <button class="secondary" id="add-pinned-decision-${project.id}" style="padding:3px 10px;font-size:10px">+ Pin</button>

                </div>

              </div>

              <div id="add-decision-form-${project.id}" style="display:none;margin-bottom:10px;padding:10px;background:var(--surface-1);border:1px solid var(--border);border-radius:6px">
                <div style="display:grid;gap:6px">
                  <input id="dec-form-title-${project.id}" type="text" placeholder="Title" style="background:var(--surface-2);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:11px;font-family:var(--font-mono);padding:4px 8px;width:100%;box-sizing:border-box">
                  <textarea id="dec-form-body-${project.id}" rows="2" placeholder="Body" style="background:var(--surface-2);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:11px;font-family:var(--font-mono);padding:4px 8px;width:100%;box-sizing:border-box;resize:vertical"></textarea>
                  <select id="dec-form-cat-${project.id}" style="background:var(--surface-2);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:11px;font-family:var(--font-mono);padding:4px 8px">
                    <option value="TECHNICAL">TECHNICAL</option>
                    <option value="ARCHITECTURAL">ARCHITECTURAL</option>
                    <option value="PRODUCT">PRODUCT</option>
                    <option value="TACTICAL">TACTICAL</option>
                    <option value="STRATEGIC">STRATEGIC</option>
                    <option value="COMPETITIVE">COMPETITIVE</option>
                    <option value="BUSINESS">BUSINESS</option>
                  </select>
                  <div style="display:flex;gap:6px;align-items:center">
                    <button id="dec-form-add-${project.id}" class="primary" style="font-size:10px;padding:3px 10px">Add</button>
                    <button id="dec-form-cancel-${project.id}" class="secondary" style="font-size:10px;padding:3px 10px">Cancel</button>
                    <span id="dec-form-status-${project.id}" style="font-size:10px;color:var(--muted)"></span>
                  </div>
                </div>
              </div>

              <div style="color:var(--muted);font-size:10px;margin-bottom:8px">Editable current truth. Use <code>pin_decision</code> MCP tool or <code>update_decision</code> with new_title+new_body to supersede.</div>

              <div id="constitution-warning-${project.id}" style="margin-bottom:8px"></div>

              <div id="pinned-decisions-${project.id}" style="font-family:var(--font-mono);font-size:12px"></div>

            </div>

            <details open style="margin-top:14px">

              <summary style="cursor:pointer;color:var(--accent);font-weight:600;font-size:12px;padding:4px 0">📋 History (Append-only log)</summary>

              <div style="color:var(--muted);font-size:10px;margin:8px 0">Append-only via <code>set_decision</code>. Captures every micro-decision; the constitution above is the live truth.</div>

              <div id="decisions-table-${project.id}" style="font-family:var(--font-mono);font-size:12px"></div>

            </details>

          </div>

        </div>

        <div style="flex-shrink:0;padding:8px 14px;border-top:1px solid var(--border)">

          <a class="secondary" style="display:inline-block;padding:5px 12px;border:1px solid var(--border);border-radius:4px;color:var(--muted);font-size:10px;text-decoration:none;font-family:'IBM Plex Mono',monospace;cursor:pointer" href="/projects/${project.id}/export/pdf" download>⬇ Export IP Record (PDF)</a>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-files-${project.id}">

        <div class="drawer-header">FILES · ${escapeHtml(project.name)}</div>

        <div id="files-browse-${project.id}" style="flex:1;overflow-y:auto">

          <div class="file-list" id="files-list-${project.id}"></div>

        </div>

        <div id="file-editor-wrap-${project.id}" style="display:none;flex:1;flex-direction:column;overflow:hidden">

          <div class="drawer-header" style="flex-shrink:0">

            <button class="secondary" id="file-back-${project.id}" style="padding:2px 8px;font-size:10px">← back</button>

            <span id="file-name-${project.id}" style="flex:1;color:var(--accent);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span>

            <button class="primary" id="file-save-${project.id}" style="padding:2px 8px;font-size:10px">save</button>

          </div>

          <div class="preview-toggle-row" style="padding:6px 14px 4px;flex-shrink:0" id="file-toggle-row-${project.id}">

            <button class="preview-btn active" data-fmode="edit" id="file-mode-edit-${project.id}">edit</button>

            <button class="preview-btn" data-fmode="preview" id="file-mode-preview-${project.id}">preview</button>

          </div>

          <textarea id="file-content-${project.id}" style="flex:1;background:var(--surface-2);border:none;border-top:1px solid var(--border);color:var(--text);padding:10px 14px;font-family:'IBM Plex Mono',monospace;font-size:12px;resize:none;outline:none;overflow-y:auto"></textarea>

          <div id="file-preview-${project.id}" class="goal-preview" style="display:none;flex:1;margin:10px 14px;"></div>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-devlog-${project.id}">

        <div class="drawer-header">DEV LOG · ${escapeHtml(project.name)}</div>

        <div style="padding:8px 10px;border-bottom:1px solid var(--border);background:var(--surface-2)">
          <textarea id="devlog-append-text-${project.id}" rows="2" placeholder="Add a note to DEVLOG.md…" style="width:100%;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:11px;font-family:var(--font-mono);padding:5px 8px;resize:vertical;box-sizing:border-box;outline:none"></textarea>
          <div style="display:flex;gap:6px;align-items:center;margin-top:4px">
            <button id="devlog-append-btn-${project.id}" class="primary" style="font-size:10px;padding:3px 10px">Append</button>
            <span id="devlog-append-status-${project.id}" style="font-size:10px;color:var(--muted)"></span>
          </div>
        </div>

        <div style="padding:6px 10px;border-bottom:1px solid var(--border)">
          <input type="text" id="devlog-search-${project.id}" placeholder="Search dev log (description, session)…" style="width:100%;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:11px;font-family:var(--font-mono);padding:4px 8px;outline:none">
        </div>

        <div class="scroll-area"><div class="task-list" id="tasks-${project.id}"></div></div>

      </div>

      <div class="drawer-panel" id="drawer-timeline-${project.id}">

        <div class="drawer-header" style="justify-content:space-between">

          <span>TIMELINE · ${escapeHtml(project.name)}</span>

          <span style="display:flex;gap:6px;align-items:center">

            <button class="secondary" id="timeline-axis-${project.id}" title="Toggle relative/absolute time" style="padding:2px 8px;font-size:10px">relative</button>

            <button class="secondary" id="timeline-refresh-${project.id}" title="Refresh" style="padding:2px 8px;font-size:10px">refresh</button>

          </span>

        </div>

        <div style="padding:4px 14px 4px;font-size:10px;color:var(--muted);border-bottom:1px solid var(--border);flex-shrink:0;display:flex;align-items:center;gap:10px;flex-wrap:wrap">

          <span>Session activity across time — each row is one AI session, each bar is one task.</span>

          <span style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">

            <span><span style="color:#34d399">■</span> done</span>

            <span><span style="color:#f87171">■</span> pending/failed</span>

            <span><span style="color:#6c8fff">■</span> sprint</span>

            <span><span style="color:#fbbf24">■</span> north star</span>

            <span><span style="color:#a78bfa">■</span> goal</span>

          </span>

        </div>

        <div style="padding:5px 14px;display:flex;gap:6px;align-items:center;flex-wrap:wrap;border-bottom:1px solid var(--border);flex-shrink:0">

          <label style="font-size:10px;color:var(--muted)">From</label>

          <input type="date" id="timeline-from-${project.id}" style="font-size:10px;background:var(--surface-2);color:var(--text);border:1px solid var(--border);border-radius:3px;padding:1px 4px;outline:none">

          <label style="font-size:10px;color:var(--muted)">To</label>

          <input type="date" id="timeline-to-${project.id}" style="font-size:10px;background:var(--surface-2);color:var(--text);border:1px solid var(--border);border-radius:3px;padding:1px 4px;outline:none">

          <button class="secondary" id="timeline-r7d-${project.id}" style="padding:1px 7px;font-size:10px">7d</button>

          <button class="secondary" id="timeline-r30d-${project.id}" style="padding:1px 7px;font-size:10px">30d</button>

          <button class="secondary" id="timeline-rall-${project.id}" style="padding:1px 7px;font-size:10px">All</button>

          <span id="timeline-range-err-${project.id}" style="color:#f87171;font-size:10px;display:none"></span>

        </div>

        <div class="timeline-wrap" id="timeline-wrap-${project.id}" style="flex:1;overflow:auto;padding:14px"></div>

      </div>

      <div class="drawer-panel" id="drawer-rewind-${project.id}">

        <div class="drawer-header" style="justify-content:space-between">

          <span>REWIND · ${escapeHtml(project.name)}</span>

          <span style="display:flex;gap:6px;align-items:center">

            <button class="secondary rewind-day-btn" data-days="7" data-pid="${project.id}" style="padding:2px 8px;font-size:10px">7d</button>

            <button class="secondary rewind-day-btn" data-days="14" data-pid="${project.id}" style="padding:2px 8px;font-size:10px">14d</button>

            <button class="secondary rewind-day-btn" data-days="30" data-pid="${project.id}" style="padding:2px 8px;font-size:10px">30d</button>

            <button class="secondary rewind-day-btn" data-days="90" data-pid="${project.id}" style="padding:2px 8px;font-size:10px">90d</button>

            <button class="secondary rewind-day-btn" data-days="365" data-pid="${project.id}" style="padding:2px 8px;font-size:10px">1y</button>

            <button class="secondary rewind-day-btn" data-days="3650" data-pid="${project.id}" style="padding:2px 8px;font-size:10px">All</button>

          </span>

        </div>

        <div style="flex-shrink:0;padding:6px 14px;border-bottom:1px solid var(--border)">

          <input type="text" id="rewind-search-${project.id}" placeholder="Search tasks, notes, decisions…"

            style="width:100%;padding:5px 10px;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;font-family:var(--font-mono);font-size:11px;color:var(--text);outline:none;box-sizing:border-box">

        </div>

        <div class="rewind-wrap" id="rewind-wrap-${project.id}" style="flex:1;overflow:auto;padding:14px;font-family:'IBM Plex Mono',monospace;font-size:11px">

          <div class="empty" style="color:var(--muted)">pick a window above</div>

        </div>

        <div style="flex-shrink:0;padding:8px 14px;border-top:1px solid var(--border);display:flex;gap:8px">

          <button class="secondary" id="rewind-share-${project.id}" style="padding:4px 10px;font-size:10px">Copy shareable link</button>

          <a class="secondary" href="/projects/${project.id}/export/pdf" download

             style="padding:4px 10px;font-size:10px;border:1px solid var(--border);border-radius:4px;color:var(--muted);text-decoration:none;font-family:'IBM Plex Mono',monospace">Export as PDF</a>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-queue-${project.id}">

        <div class="drawer-header" style="justify-content:space-between">

          <span style="display:flex;flex-direction:column;gap:1px">

            <span>QUEUE · ${escapeHtml(project.name)}</span>

            <span style="font-size:9px;letter-spacing:0;text-transform:none;font-weight:400;opacity:0.7">Work items — claimed atomically so parallel sessions never collide</span>

          </span>

          <span style="display:flex;gap:6px;align-items:center">
            <button class="secondary" id="queue-reconcile-${project.id}" style="padding:2px 8px;font-size:10px" title="Check if any pending items may already be done based on recent commits">reconcile</button>
            <button class="secondary" id="queue-refresh-${project.id}" style="padding:2px 8px;font-size:10px">refresh</button>
          </span>

        </div>

        <div id="live-session-${project.id}" style="display:none;flex-shrink:0;border-bottom:1px solid var(--border);background:var(--surface-2);padding:8px 14px 10px"></div>

        <div id="reconcile-results-${project.id}" style="display:none;flex-shrink:0;border-bottom:1px solid var(--border);background:var(--surface-2);padding:8px 14px 10px;font-family:var(--font-mono);font-size:11px"></div>

        <div style="padding:8px 14px 0;flex-shrink:0">

          <input type="text" id="task-search-${project.id}" placeholder="Search tasks…"

            style="width:100%;padding:5px 10px;background:var(--surface-2);border:1px solid var(--border);

            color:var(--text);border-radius:4px;font-family:var(--font-mono);font-size:11px;outline:none">

        </div>

        <div class="queue-scroll" style="flex:1;min-height:0;overflow-y:auto" id="queue-scroll-${project.id}">

          <div id="queue-body-${project.id}">

          <div class="empty" style="color:var(--muted)">select queue to load</div>

        </div>

        <div id="recent-sessions-${project.id}" style="display:none;border-top:1px solid var(--border);background:var(--surface-2);padding:8px 14px 8px"></div>

        <div id="recent-runs-${project.id}" style="border-top:1px solid var(--border);background:var(--surface-2)">

          <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 14px;cursor:pointer"

               id="recent-runs-toggle-${project.id}">

            <span style="font-family:var(--font-mono);font-size:10px;color:var(--muted);letter-spacing:0.05em">RECENT RUNS</span>

            <span id="recent-runs-chevron-${project.id}" style="font-size:10px;color:var(--muted)">▲</span>

          </div>

          <div id="recent-runs-body-${project.id}" style="padding:0 14px 8px;font-family:var(--font-mono);font-size:11px">

            <div style="color:var(--muted)">loading…</div>

          </div>

        </div>

      </div>

      </div>

      <div class="drawer-panel" id="drawer-notes-${project.id}">

        <div class="drawer-header" style="justify-content:space-between">

          <span style="display:flex;flex-direction:column;gap:1px">

            <span>NOTES · ${escapeHtml(project.name)}</span>

            <span style="font-size:9px;letter-spacing:0;text-transform:none;font-weight:400;opacity:0.7">Persistent notes readable by your team and AI sessions</span>

          </span>

          <span style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;justify-content:flex-end">

            <input type="text" id="notes-search-${project.id}" placeholder="search notes…" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:2px 6px;width:120px">

            <select id="notes-kindsel-${project.id}" title="Filter by kind" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:2px 6px"><option value="">all kinds</option><option value="wiki">wiki</option><option value="insight">insight</option><option value="reference">reference</option></select>

            <select id="notes-tagsel-${project.id}" title="Filter by tag" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:2px 6px;max-width:130px"><option value="">all tags</option></select>

            <label title="Show auto-captured session summaries (checkpoint notes)" style="display:flex;align-items:center;gap:3px;font-size:9px;color:var(--muted);cursor:pointer;user-select:none"><input type="checkbox" id="notes-show-auto-${project.id}" style="margin:0;cursor:pointer">summaries</label>

          </span>

        </div>

        <div style="flex:1;overflow-y:auto;overflow-x:hidden;word-break:break-word;padding:14px;font-family:'IBM Plex Mono',monospace;font-size:12px" id="notes-body-${project.id}">

          <div class="empty" style="color:var(--muted)">loading notes…</div>

        </div>

        <div style="flex-shrink:0;padding:10px 14px;border-top:1px solid var(--border);background:var(--surface-2)">

          <div style="display:flex;gap:6px;margin-bottom:6px">

            <input type="text" id="notes-add-title-${project.id}" placeholder="Title" style="flex:1;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:11px;font-family:var(--font-mono);padding:5px 8px;outline:none">

            <input type="text" id="notes-add-tags-${project.id}" placeholder="tags (comma-sep)" style="width:140px;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:11px;font-family:var(--font-mono);padding:5px 8px;outline:none">

            <select id="notes-add-kind-${project.id}" title="Note kind" style="width:100px;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:11px;font-family:var(--font-mono);padding:5px 8px;outline:none"><option value="wiki">wiki</option><option value="insight">insight</option><option value="reference">reference</option></select>

          </div>

          <textarea id="notes-add-body-${project.id}" placeholder="Body (markdown ok)" rows="3" style="width:100%;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:11px;font-family:var(--font-mono);padding:6px 8px;outline:none;resize:vertical"></textarea>

          <div style="display:flex;justify-content:flex-end;margin-top:6px">

            <button class="primary" id="notes-add-btn-${project.id}" style="padding:4px 12px;font-size:11px">+ Add note</button>

          </div>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-hitl-${project.id}">

        <div class="drawer-header" style="justify-content:space-between">

          <span style="display:flex;flex-direction:column;gap:1px">

            <span>YOUR TURN · ${escapeHtml(project.name)}</span>

            <span style="font-size:9px;letter-spacing:0;text-transform:none;font-weight:400;opacity:0.7">Blocking questions from your AI agents that need a human decision</span>

          </span>

          <div style="display:flex;gap:6px;align-items:center">

            <input type="text" id="hitl-search-${project.id}" placeholder="search…" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:2px 6px;outline:none;width:110px">

            <select id="hitl-status-filter-${project.id}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:2px 6px">

              <option value="pending">pending</option>

              <option value="all">all</option>

              <option value="answered">answered</option>

              <option value="dismissed">dismissed</option>

            </select>

            <button class="secondary" id="hitl-refresh-${project.id}" style="padding:2px 8px;font-size:10px">refresh</button>

          </div>

        </div>

        <div style="flex:1;overflow-y:auto;padding:14px;font-family:'IBM Plex Mono',monospace;font-size:12px" id="hitl-body-${project.id}">

          <div class="empty" style="color:var(--muted)">loading HITL queue…</div>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-docs-${project.id}">

        <div class="drawer-header">

          <span>MCP TOOL REFERENCE</span>

        </div>

        <div style="flex:1;overflow-y:auto;padding:14px;font-family:'IBM Plex Mono',monospace;font-size:11px" id="docs-body-${project.id}">

          <div class="empty" style="color:var(--muted)">loading tools…</div>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-settings-${project.id}">

        <div class="drawer-header">

          <span>SETTINGS · ${escapeHtml(project.name)}</span>

        </div>

        <div style="flex:1;overflow-y:auto;padding:14px" id="settings-body-${project.id}">

          <div class="empty" style="color:var(--muted)">loading…</div>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-team-${project.id}">

        <div class="drawer-header" style="justify-content:space-between">

          <span style="display:flex;flex-direction:column;gap:1px">

            <span>TEAM · ${escapeHtml(project.name)}</span>

            <span style="font-size:9px;letter-spacing:0;text-transform:none;font-weight:400;opacity:0.7">Manage project members and access</span>

          </span>

          <span style="display:flex;gap:6px;align-items:center">

            <input type="text" id="team-search-${project.id}" placeholder="search…" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:2px 6px;outline:none;width:100px">

            <select id="team-days-${project.id}" style="background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:10px;font-family:var(--font-mono);padding:2px 4px">

              <option value="1">last 1d</option>

              <option value="7">last 7d</option>

              <option value="14" selected>last 14d</option>

              <option value="30">last 30d</option>

            </select>

            <button class="secondary" id="team-refresh-${project.id}" style="padding:2px 8px;font-size:10px">refresh</button>

          </span>

        </div>

        <div style="flex:1;overflow-y:auto;padding:14px;font-family:'IBM Plex Mono',monospace;font-size:11px" id="team-body-${project.id}">

          <div class="empty" style="color:var(--muted)">loading team summary…</div>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-codeintel-${project.id}">

        <div class="drawer-header">

          <span>CODE INTEL · ${escapeHtml(project.name)}</span>

        </div>

        <div style="flex:1;overflow-y:auto;padding:14px" id="codeintel-body-${project.id}">

          <div class="empty" style="color:var(--muted)">loading…</div>

        </div>

      </div>

    </div>

    <section class="claude-handoff-panel">

      <div class="panel-header">

        <span>CLAUDE</span>

        <span class="server-version-pill" id="server-version"></span>

      </div>

      <div class="claude-launch-body">

        <div class="claude-section" data-section="start">

          <div class="claude-section-label">Start a new session</div>

          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">

            <button class="primary claude-section-btn" id="copy-start-code-${project.id}" title="Copies start_session() command for Claude Code">Claude Code ⬡</button>

            <button class="secondary claude-section-btn" id="copy-start-chat-${project.id}" title="Copies context for Claude or Codex">Open in Claude / Codex</button>

            <button class="secondary claude-section-btn" id="btn-setup-hooks-${project.id}" title="Auto-wire SessionStart + Stop hooks for your AI tools" style="font-size:10px">⚡ Setup Hooks</button>

          </div>

          <p class="claude-hint">Claude Code: pastes <code>start_session()</code> command. Open in Claude / Codex: pastes handoff context. Hooks: opens setup instructions.</p>

        </div>

        <hr class="claude-divider">

        <div class="claude-section" data-section="continue">

          <div class="claude-section-label">Resume Claude Code session (<code>start_session</code> + <code>get_context_block</code>)</div>

          <select class="claude-session-select" id="continue-session-${project.id}">

            <option value="">(no sessions yet)</option>

          </select>

          <button class="primary claude-section-btn" id="copy-resume-${project.id}" title="MCP flow: start_session() + get_context_block()">Copy resume MCP commands</button>

          <p class="claude-hint">Uses <code>start_session()</code> to reopen the session and <code>get_context_block()</code> to reload the working context.</p>

        </div>

        <hr class="claude-divider">

        <div class="claude-section" data-section="worker">

          <div class="claude-section-label">Start Claude Code worker (<code>start_worker_session</code> + claim)</div>

          <button class="primary claude-section-btn" id="start-worker-${project.id}" title="MCP: start_worker_session() claims the next task and returns worker context">Claim &amp; start worker</button>

          <div class="claude-worker-result" id="worker-result-${project.id}" style="display:none">

            <pre class="claude-worker-xml" id="worker-xml-${project.id}"></pre>

            <button class="secondary claude-section-btn" id="copy-worker-${project.id}" title="Copy the worker_context returned by start_worker_session()">Copy worker context</button>

            <p class="claude-hint">Uses <code>start_worker_session()</code> to claim the next task and produce a worker-ready context block.</p>

          </div>

          <div class="claude-worker-empty" id="worker-empty-${project.id}" style="display:none">

            <p class="claude-hint">No pending tasks — add one to the queue first.</p>

          </div>

        </div>

        <hr class="claude-divider">

        <div class="claude-section" data-section="handoff">

          <div class="claude-section-label">Claude Code handoff (<code>generate_handoff</code>)</div>

          <label style="display:flex;align-items:center;gap:6px;font-size:10px;color:var(--text);font-family:var(--font-mono);cursor:pointer">
            <input type="checkbox" id="sequential-mode-${project.id}" style="cursor:pointer">
            <span>Sequential mode</span>
          </label>
          <p class="claude-hint" id="touches-files-warning-${project.id}" style="display:none;color:#f59e0b">⚠ touches_files overlap detected in active sprint items. Coordinate or serialize before handing this off.</p>

          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">

            <button class="primary claude-section-btn" id="copy-handoff-${project.id}" title="Fetch generate_handoff() output and copy raw plain text">Copy handoff (plain text)</button>

            <button class="secondary claude-section-btn" id="regen-handoff-${project.id}" title="Regenerate the on-disk handoff markdown via generate_handoff()">Regenerate</button>

            <span class="claude-handoff-ts" id="handoff-ts-${project.id}" style="font-size:10px;color:var(--muted)"></span>

          </div>

          <div id="handoff-raw-${project.id}" style="display:none;margin-top:8px">

            <textarea id="handoff-raw-text-${project.id}" readonly style="width:100%;height:220px;font-family:var(--font-mono);font-size:10px;background:#0d1117;color:#e6edf3;border:1px solid var(--border);padding:8px;border-radius:4px;resize:vertical;outline:none"></textarea>

            <div style="display:flex;gap:6px;margin-top:4px;align-items:center">

              <button class="secondary" id="handoff-copy-text-${project.id}" style="font-size:10px;padding:3px 10px">Copy text</button>

              <button class="secondary" id="handoff-close-raw-${project.id}" style="font-size:10px;padding:3px 10px">Close</button>

            </div>

          </div>

          <p class="claude-hint">Fetches raw plain-text handoff for a fresh Claude Code session. Select all or use Copy text.</p>

        </div>

        <hr class="claude-divider">

        <div class="claude-section claude-section-narrow" data-section="open">

          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">

            <a class="claude-cta-secondary-btn" id="open-in-claude-${project.id}"

               href="https://claude.ai/new" target="_blank" rel="noopener"

               title="Open in Claude">New Chat →</a>

            <button class="secondary claude-section-btn" id="copy-context-${project.id}" style="font-size:11px">Copy chat context</button>

          </div>

          <p class="claude-hint">Open a new Claude.ai chat, paste the context to get up to speed</p>

        </div>

      </div>

      </div>

    </section>

  `;

  root.appendChild(body);



  // Per-tab state. activeVtab tracks which drawer panel is open.

  state.panels[project.id] = {

    ws: null, taskCache: [], goalRaw: null, goalIsJson: false,

    activeVtab: 'status',

    loadErrors: {},

  };



  // Vtab drawer toggle — same tab again collapses; different tab switches.

  const vtabStrip = document.getElementById(`vtab-strip-${project.id}`);

  const drawer = document.getElementById(`drawer-${project.id}`);

  if (vtabStrip && drawer) {

    vtabStrip.querySelectorAll('.vtab-btn').forEach(btn => {

      btn.onclick = () => {

        const vtab = btn.dataset.vtab;

        const p = state.panels[project.id];

        // v1.4.0: drawer is always visible — just switch active panel.

        vtabStrip.querySelectorAll('.vtab-btn').forEach(b => {

          b.classList.toggle('active', b.dataset.vtab === vtab);

        });

        drawer.querySelectorAll('.drawer-panel').forEach(dp => {

          dp.classList.toggle('active', dp.id === `drawer-${vtab}-${project.id}`);

        });

        p.activeVtab = vtab;

        try { localStorage.setItem('meridian_last_tab_' + project.id, vtab); } catch(_) {}

        if (vtab === 'files') loadFilesTab(project.id);

        if (vtab === 'devlog') refreshTasks(project.id);

        if (vtab === 'timeline') loadTimeline(project.id);

        if (vtab === 'rewind') initRewindTab(project.id);

        if (vtab === 'queue') {

          loadQueue(project.id);

          updateLiveFeed(project.id);

          loadRecentRuns(project.id);

          // 5s live-feed poll while queue tab is visible — cleared on tab switch

          clearInterval(p._liveFeedInterval);

          p._liveFeedInterval = setInterval(() => {

            if (p.activeVtab === 'queue') updateLiveFeed(project.id);

            else clearInterval(p._liveFeedInterval);

          }, 5000);

        } else {

          clearInterval(p._liveFeedInterval);

        }

        if (vtab === 'live') loadLiveTab(project.id);

        if (vtab === 'team') loadTeamTab(project.id);

        if (vtab === 'notes') loadNotesTab(project.id);

        if (vtab === 'hitl') loadHitlTab(project.id);

        if (vtab === 'docs') loadDocsTab(project.id);

        if (vtab === 'settings') loadSettingsTab(project.id);

        if (vtab === 'codeintel') loadCodeIntelTab(project.id);

      };

    });

    // Restore last active vtab from localStorage
    try {
      const saved = localStorage.getItem('meridian_last_tab_' + project.id);
      if (saved) {
        const savedBtn = vtabStrip.querySelector('.vtab-btn[data-vtab="' + saved + '"]');
        if (savedBtn) savedBtn.click();
      }
    } catch(_) {}

    // Show Code Intel tab only when tunnel:code is active
    _initCodeIntelTabVisibility(project.id);

  }



  // Goal subtab switching (North Star / Version Goal / Sprint / Decisions)

  const goalDrawer = document.getElementById(`drawer-goal-${project.id}`);

  if (goalDrawer) {

    goalDrawer.querySelectorAll('.goal-subtab-btn').forEach(btn => {

      btn.onclick = () => {

        goalDrawer.querySelectorAll('.goal-subtab-btn').forEach(b => b.classList.toggle('active', b === btn));

        const gtab = btn.dataset.gtab;

        goalDrawer.querySelectorAll('.goal-subtab-panel').forEach(p => {

          p.classList.toggle('active', p.id === `gtab-${gtab}-${project.id}`);

        });

        // v2.4 — lazy-load pinned decisions when the subtab opens, so first

        // paint isn't blocked on the extra fetch.

        if (gtab === 'decisions') loadPinnedDecisions(project.id);

      };

    });

  }



  // v2.4 — wire the [+ Pin] and [Consolidate] buttons on the Decisions subtab.

  const addPinBtn = document.getElementById(`add-pinned-decision-${project.id}`);
  const decForm = document.getElementById(`add-decision-form-${project.id}`);
  const decFormTitle = document.getElementById(`dec-form-title-${project.id}`);
  const decFormBody = document.getElementById(`dec-form-body-${project.id}`);
  const decFormCat = document.getElementById(`dec-form-cat-${project.id}`);
  const decFormAdd = document.getElementById(`dec-form-add-${project.id}`);
  const decFormCancel = document.getElementById(`dec-form-cancel-${project.id}`);
  const decFormStatus = document.getElementById(`dec-form-status-${project.id}`);

  if (addPinBtn && decForm) {
    addPinBtn.onclick = () => {
      const visible = decForm.style.display !== 'none';
      decForm.style.display = visible ? 'none' : 'block';
      if (!visible && decFormTitle) decFormTitle.focus();
    };
  }
  if (decFormCancel) decFormCancel.onclick = () => { decForm.style.display = 'none'; };
  if (decFormAdd) {
    const doAddDecision = async () => {
      const title = (decFormTitle?.value || '').trim();
      const body = (decFormBody?.value || '').trim();
      const category = (decFormCat?.value || 'TECHNICAL');
      if (!title || !body) { if (decFormStatus) decFormStatus.textContent = 'Title and body required.'; return; }
      if (title.length > 500) { if (decFormStatus) decFormStatus.textContent = 'Title too long (500 char limit).'; if (decFormTitle) decFormTitle.style.borderColor = 'var(--red, #f87171)'; return; }
      if (body.length > 100000) { if (decFormStatus) decFormStatus.textContent = 'Body too long (100,000 char limit).'; if (decFormBody) decFormBody.style.borderColor = 'var(--red, #f87171)'; return; }
      decFormAdd.disabled = true;
      if (decFormStatus) decFormStatus.textContent = '';
      try {
        await api(`/projects/${project.id}/decisions-pinned`, { method: 'POST', body: JSON.stringify({ title, body, category }) });
        if (decFormTitle) decFormTitle.value = '';
        if (decFormBody) decFormBody.value = '';
        decForm.style.display = 'none';
        toast('decision pinned');
        loadPinnedDecisions(project.id);
      } catch(e) {
        if (decFormStatus) decFormStatus.textContent = `Error: ${escapeHtml(String(e))}`;
      } finally {
        decFormAdd.disabled = false;
      }
    };
    decFormAdd.onclick = doAddDecision;
    if (decFormTitle) decFormTitle.oninput = () => {
      const over = decFormTitle.value.length > 500;
      decFormTitle.style.borderColor = over ? 'var(--red, #f87171)' : '';
      if (decFormStatus) decFormStatus.textContent = over ? `Title: ${decFormTitle.value.length}/500` : '';
    };
    if (decFormBody) decFormBody.oninput = () => {
      const len = decFormBody.value.length, limit = 100000;
      const over = len > limit, near = len > limit * 0.9;
      decFormBody.style.borderColor = over ? 'var(--red, #f87171)' : near ? 'var(--warning, #fb923c)' : '';
      if (decFormStatus) decFormStatus.textContent = (over || near) ? `Body: ${len.toLocaleString()}/${limit.toLocaleString()}` : '';
    };
    [decFormTitle, decFormBody].forEach(el => {
      if (el) el.addEventListener('keydown', (e) => { if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') doAddDecision(); });
    });
  }

  const consolidateBtn = document.getElementById(`consolidate-decisions-${project.id}`);

  if (consolidateBtn) consolidateBtn.onclick = () => consolidateDecisions(project.id);



  const saveGoalBtn = document.getElementById(`save-goal-${project.id}`);
  if (saveGoalBtn) saveGoalBtn.onclick = () => saveGoal(project.id);

  const saveNorthStarBtn = document.getElementById(`save-north-star-${project.id}`);
  if (saveNorthStarBtn) saveNorthStarBtn.onclick = () => saveNorthStar(project.id);

  const saveSprintBtn = document.getElementById(`save-sprint-${project.id}`);
  if (saveSprintBtn) saveSprintBtn.onclick = () => saveSprint(project.id);



  // Sprint tab board — compact one-liner showing current sprint progress only.

  // Full sprint board lives in the LIVE tab. Goal tab just shows the header.

  async function loadSprintBoard() {

    const sprintItemsPath = `/projects/${project.id}/sprint-items`;

    try {

      const items = await projectApi(project.id, sprintItemsPath);

      const board = document.getElementById(`sprint-board-goal-${project.id}`);

      if (!board) return;

      if (!items || !items.length) {

        board.innerHTML = '<div style="color:var(--muted);font-size:10px;padding:4px 0">(no sprint items — use LIVE tab to add)</div>';

        return;

      }

      // Show counts for ALL active items across all versions

      const activeStatuses = new Set(['pending', 'todo', 'in_progress']);

      const activeVersions = new Set(items.filter(it => activeStatuses.has(it.status)).map(it => it.version));

      const scopeItems = items.filter(it =>

        activeStatuses.has(it.status) || (it.version && activeVersions.has(it.version))

      );

      const doneCount = scopeItems.filter(i => i.status === 'done' || i.status === 'skipped').length;

      const activeCount = scopeItems.filter(i => activeStatuses.has(i.status)).length;

      const total = scopeItems.length;

      const pct = total > 0 ? Math.round((doneCount / total) * 100) : 0;

      const pctColor = doneCount === 0 ? 'var(--muted)' : doneCount === total ? 'var(--accent-green)' : '#fbbf24';

      const pendingItems = scopeItems.filter(i => activeStatuses.has(i.status));
      const statusColors = { pending: 'var(--muted)', todo: 'var(--muted)', in_progress: '#fbbf24' };
      const itemsHtml = pendingItems.slice(0, 10).map(it => {
        const color = statusColors[it.status] || 'var(--muted)';
        const badge = it.status === 'in_progress' ? '⚡' : '·';
        return `<div style="display:flex;align-items:center;gap:5px;padding:2px 0;border-top:1px solid var(--border)20">` +
          `<span style="color:${color};font-size:9px;flex-shrink:0">${badge}</span>` +
          `<span style="font-size:10px;color:var(--text);font-family:var(--font-mono);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(it.title)}">${escapeHtml(it.title)}</span>` +
          (it.version ? `<span style="font-size:9px;color:var(--muted);flex-shrink:0">${escapeHtml(it.version)}</span>` : '') +
          `</div>`;
      }).join('');

      board.innerHTML = `<div style="font-size:10px;color:var(--muted);padding:3px 0;display:flex;align-items:center;gap:8px;margin-bottom:${pendingItems.length ? '4px' : '0'}">

        <span style="font-weight:600;color:var(--accent)">all active</span>

        <span style="color:${pctColor}">${doneCount}/${total} done (${pct}%)</span>

        ${activeCount > 0 ? `<span style="color:var(--accent)">${activeCount} pending</span>` : '<span style="color:var(--accent-green)">✓ complete</span>'}

        <span style="opacity:0.5">· See LIVE tab for full board</span>

      </div>${itemsHtml}`;

    } catch(e) {

      const board = document.getElementById(`sprint-board-goal-${project.id}`);

      if (!board) return;

      board.innerHTML = renderProjectLoadError(project.id, 'Sprint board unavailable', sprintItemsPath, e);

      wireProjectLoadRetry(board, project.id);

    }

  }

  _sprintBoardReloaders[project.id] = loadSprintBoard;

  loadSprintBoard();



  // Wire session-focus select+input combo (slight delay lets refreshGoal set inp.value first)

  setTimeout(async () => {

    const sel = document.getElementById(`goal-sprint-select-${project.id}`);

    const inp = document.getElementById(`goal-sprint-${project.id}`);

    if (!sel || !inp) return;

    try {

      const sessions = await projectApi(project.id, `/projects/${project.id}/sessions`);

      const active = (sessions || []).filter(s => s.status !== 'closed' && s.status !== 'archived');

      const opts = active.map(s => `<option value="${escapeHtml(s.name)}">${escapeHtml(s.name)}</option>`).join('');

      sel.innerHTML = opts + '<option value="__custom__">Custom…</option>';

      _sprintSelectSyncers[project.id] = function(val) {

        if (!sel) return;

        const match = Array.from(sel.options).find(o => o.value === val && o.value !== '__custom__');

        if (match) { sel.value = val; inp.value = val; inp.style.display = 'none'; }

        else if (val) { sel.value = '__custom__'; inp.style.display = 'block'; inp.value = val; }

        else {
          if (sel.options.length) sel.selectedIndex = 0;
          inp.value = sel.value && sel.value !== '__custom__' ? sel.value : '';
          inp.style.display = 'none';
        }

      };

      if (inp.value) _sprintSelectSyncers[project.id](inp.value);

    } catch (_) {

      sel.innerHTML = '<option value="__custom__">Custom…</option>';

      inp.style.display = 'block';

    }

    sel.onchange = () => {

      if (sel.value === '__custom__') { inp.style.display = 'block'; inp.focus(); }

      else { inp.style.display = 'none'; inp.value = sel.value; }

    };

  }, 200);



  const sprintAddBtn = document.getElementById(`sprint-add-btn-${project.id}`);

  const sprintAddInput = document.getElementById(`sprint-add-input-${project.id}`);

  if (sprintAddBtn && sprintAddInput) {

    const doAdd = async () => {

      const title = sprintAddInput.value.trim();

      if (!title) return;

      try {

        await api(`/projects/${project.id}/sprint-items`, { method: 'POST', body: JSON.stringify({ title, version: state.panels[project.id]?.sprint || 'current' }) });

        sprintAddInput.value = '';

        loadSprintBoard();

      } catch(e) { console.error('Add sprint item failed:', e); }

    };

    sprintAddBtn.onclick = doAdd;

    sprintAddInput.onkeydown = (e) => { if (e.key === 'Enter') doAdd(); };

  }



  // v1.6.x — goal-mode toggle removed. Auto mode (v0.4.2) appended ambient

  // task summaries to goal content; that role is now covered by the timeline

  // + session summaries + task log. Manual is the only mode worth exposing.



  // Dev log append button
  {
    const appendBtn = document.getElementById(`devlog-append-btn-${project.id}`);
    const appendText = document.getElementById(`devlog-append-text-${project.id}`);
    const appendStatus = document.getElementById(`devlog-append-status-${project.id}`);
    if (appendBtn && appendText) {
      appendBtn.onclick = async () => {
        const text = appendText.value.trim();
        if (!text) return;
        appendBtn.disabled = true;
        if (appendStatus) appendStatus.textContent = '';
        try {
          await api(`/projects/${project.id}/devlog`, { method: 'POST', body: JSON.stringify({ text }) });
          appendText.value = '';
          toast('Appended to DEVLOG.md');
        } catch(e) {
          if (appendStatus) appendStatus.textContent = `Error: ${escapeHtml(String(e))}`;
        } finally {
          appendBtn.disabled = false;
        }
      };
      appendText.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') appendBtn.click();
      });
    }
  }

  // v1.5.x — Claude launch control panel (4 sections).

  wireClaudeLaunchPanel(project.id);

  document.getElementById(`goal-${project.id}`).addEventListener('blur', () => saveGoal(project.id));

  document.getElementById(`goal-north-star-${project.id}`).addEventListener('blur', () => saveNorthStar(project.id));

  document.getElementById(`goal-sprint-${project.id}`).addEventListener('blur', () => saveSprint(project.id));

  // v0.6.4 — dirty state: highlight textarea border when unsaved changes exist

  document.getElementById(`goal-${project.id}`).addEventListener('input', function() {

    const p = state.panels[project.id];

    this.classList.toggle('dirty', this.value !== (p._lastSaved || ''));

  });

  document.getElementById(`goal-north-star-${project.id}`).addEventListener('input', function() {

    const p = state.panels[project.id];

    this.classList.toggle('dirty', this.value !== (p._serverNorthStar || ''));

    autosizeGoalField(this);

  });

  document.getElementById(`goal-sprint-${project.id}`).addEventListener('input', function() {

    const p = state.panels[project.id];

    this.classList.toggle('dirty', this.value !== (p._serverSprint || ''));

  });

  autosizeGoalField(document.getElementById(`goal-north-star-${project.id}`));



  // Files tab: back button returns to browse view; save button persists edits.

  const fileBackBtn = document.getElementById(`file-back-${project.id}`);

  const fileSaveBtn = document.getElementById(`file-save-${project.id}`);

  if (fileBackBtn) fileBackBtn.onclick = () => {

    const browse = document.getElementById(`files-browse-${project.id}`);

    const editorWrap = document.getElementById(`file-editor-wrap-${project.id}`);

    if (browse) browse.style.display = '';

    if (editorWrap) editorWrap.style.display = 'none';

  };

  if (fileSaveBtn) fileSaveBtn.onclick = () => saveFile(project.id);



  refreshTab(project.id);



  connectWs(project.id);

}



// v1.6.x — LIVE tab. Right-now view of active sessions + work queue.

// Section A: active sessions (filtered to last 24h) with claimed task

// per session, freshness dot, and human_id badge.

// Section B: pending + in_progress tasks, [+ Add task] input, cancel

// per row. WS-driven + optional 30s auto-refresh (v1.7.0).



const LIVE_REFRESH_MS = 30000;

const LIVE_THROTTLE_MS = 10000;

const liveRefreshState = {}; // keyed by projectId



function scheduleLiveRefresh(projectId) {

  const s = liveRefreshState[projectId] || (liveRefreshState[projectId] = {});

  clearTimeout(s.timer);

  if (!s.enabled) return;

  const sinceLastMs = Date.now() - (s.lastRefresh || 0);

  const wait = Math.max(LIVE_THROTTLE_MS, LIVE_REFRESH_MS - sinceLastMs);

  s.timer = setTimeout(async () => {

    s.lastRefresh = Date.now();

    const panel = state.panels[projectId];

    if (panel && panel.activeVtab === 'live') {

      await refreshLiveTab(projectId);

    }

    scheduleLiveRefresh(projectId);

  }, wait);

}



function initLiveAutoRefresh(projectId) {

  const s = liveRefreshState[projectId] || (liveRefreshState[projectId] = {});

  const stored = localStorage.getItem(STORAGE_KEY('meridian.liveAutoRefresh'));

  s.enabled = stored === null ? true : stored === 'true';

  const btn = document.getElementById(`live-auto-btn-${projectId}`);

  if (btn) {

    btn.textContent = s.enabled ? '↻ Auto' : '↻ Off';

    btn.style.opacity = s.enabled ? '1' : '0.4';

    btn.onclick = () => {

      s.enabled = !s.enabled;

      localStorage.setItem(STORAGE_KEY('meridian.liveAutoRefresh'), String(s.enabled));

      btn.textContent = s.enabled ? '↻ Auto' : '↻ Off';

      btn.style.opacity = s.enabled ? '1' : '0.4';

      if (s.enabled) scheduleLiveRefresh(projectId);

      else clearTimeout(s.timer);

    };

  }

  if (s.enabled) scheduleLiveRefresh(projectId);

}



async function loadLiveTab(projectId) {

  const panel = state.panels[projectId];

  if (!panel) return;

  panel.liveWired = panel.liveWired || false;

  // Wire the [Pause]/[Run All] header stubs once.

  if (!panel.liveWired) {

    const pause = document.getElementById(`live-pause-${projectId}`);

    const runAll = document.getElementById(`live-run-${projectId}`);

    if (pause) pause.onclick = () => toast('Pause is a stub — coming soon');

    if (runAll) runAll.onclick = () => toast('Run All is a stub — coming soon');

    const input = document.getElementById(`live-add-input-${projectId}`);

    if (input) input.addEventListener('keydown', (ev) => {

      if (ev.key !== 'Enter') return;

      ev.preventDefault();

      const text = (input.value || '').trim();

      if (!text) return;

      addLiveTask(projectId, text).then((ok) => {

        if (ok) input.value = '';

      });

    });



    // Wire "Add to run →" toggle

    const addToggle = document.getElementById(`add-to-run-toggle-${projectId}`);

    const addArea = document.getElementById(`add-to-run-area-${projectId}`);

    const addCancel = document.getElementById(`add-to-run-cancel-${projectId}`);

    const addSubmit = document.getElementById(`add-to-run-submit-${projectId}`);

    const addText = document.getElementById(`add-to-run-text-${projectId}`);

    if (addToggle && addArea) {

      addToggle.onclick = () => {

        const open = addArea.style.display !== 'none';

        addArea.style.display = open ? 'none' : 'block';

        addToggle.textContent = open ? '+ Expand' : '− Collapse';

        if (!open && addText) addText.focus();

      };

    }

    if (addCancel && addArea) {

      addCancel.onclick = () => {

        addArea.style.display = 'none';

        addToggle.textContent = '+ Expand';

        if (addText) addText.value = '';

      };

    }

    if (addSubmit) {

      addSubmit.onclick = async () => {

        const text = (addText && addText.value || '').trim();

        if (!text) { toast('Enter text first', true); return; }

        const pid = addSubmit.dataset.project;

        // Find the first active session to attach the HITL request to

        try {

          const sessions = await api(`/projects/${pid}/sessions?active_only=true`);

          const activeSid = sessions && sessions[0] && sessions[0].id;

          await api(`/projects/${pid}/hitl-requests`, {

            method: 'POST',

            body: JSON.stringify({

              question: `Add to current goal: ${text}`,

              urgency: 'high',

              session_id: activeSid || undefined,

            }),

          });

          toast('Sent to active session HITL queue');

          if (addText) addText.value = '';

          addArea.style.display = 'none';

          addToggle.textContent = '+ Expand';

        } catch (e) { toast('Failed: ' + e.message, true); }

      };

    }

    // Wire "New Sprint" button → modal → POST /projects/{id}/goal/sprint

    const newSprintBtn = document.getElementById(`new-sprint-btn-${projectId}`);

    if (newSprintBtn) {

      newSprintBtn.onclick = () => {

        const overlay = document.createElement('div');

        overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.72);z-index:9999;display:flex;align-items:center;justify-content:center';

        overlay.innerHTML = `

          <div style="background:var(--surface-1);border:1px solid var(--border);border-radius:8px;padding:22px 24px;min-width:320px;max-width:460px;width:90%;box-shadow:0 8px 32px #0008">

            <div style="font-size:13px;font-weight:600;color:var(--text);margin-bottom:12px">New Sprint</div>

            <input id="_new-sprint-input" type="text" placeholder="e.g. v1.1 — auth + billing" autofocus

              style="width:100%;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:12px;font-family:var(--font-mono);padding:6px 10px;outline:none;box-sizing:border-box">

            <div id="_new-sprint-err" style="color:#f87171;font-size:10px;margin-top:4px;display:none"></div>

            <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:14px">

              <button class="secondary" id="_new-sprint-cancel" style="padding:4px 12px;font-size:11px">Cancel</button>

              <button class="primary" id="_new-sprint-submit" style="padding:4px 12px;font-size:11px">Set Sprint</button>

            </div>

          </div>`;

        document.body.appendChild(overlay);

        const inp = overlay.querySelector('#_new-sprint-input');

        const errEl = overlay.querySelector('#_new-sprint-err');

        const close = () => overlay.remove();

        overlay.querySelector('#_new-sprint-cancel').onclick = close;

        overlay.onclick = (e) => { if (e.target === overlay) close(); };

        const submit = async () => {

          const name = (inp.value || '').trim();

          if (!name) { errEl.textContent = 'Sprint name is required'; errEl.style.display = ''; return; }

          try {

            overlay.querySelector('#_new-sprint-submit').disabled = true;

            await api(`/projects/${projectId}/goal/sprint`, { method: 'POST', body: JSON.stringify({ sprint: name }) });

            toast(`Sprint set: ${name}`);

            close();

          } catch (e) { errEl.textContent = e.message || 'Failed'; errEl.style.display = ''; overlay.querySelector('#_new-sprint-submit').disabled = false; }

        };

        overlay.querySelector('#_new-sprint-submit').onclick = submit;

        inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); if (e.key === 'Escape') close(); });

        setTimeout(() => inp.focus(), 50);

      };

    }



    panel.liveWired = true;

  }

  await refreshLiveTab(projectId);

  initLiveAutoRefresh(projectId);

}



async function refreshLiveTab(projectId) {

  /** Fetch fresh sessions + tasks + sprint items + worktrees and repaint all Live sections. */

  try {

  const sessionsPath = `/projects/${projectId}/sessions?active_only=false`;

    const tasksPath = `/projects/${projectId}/tasks?limit=200`;

    const sprintItemsPath = `/projects/${projectId}/sprint-items`;

    const worktreesPath = `/projects/${projectId}/worktrees`;

    const [sessionsResult, tasksResult, sprintItemsResult, worktreesResult] = await Promise.allSettled([

      projectApi(projectId, sessionsPath),

      projectApi(projectId, tasksPath),

      projectApi(projectId, sprintItemsPath),

      projectApi(projectId, worktreesPath),

    ]);



    if (sprintItemsResult.status === 'fulfilled') {

      renderSprintProgress(projectId, sprintItemsResult.value || []);

    } else {

      const sprintRoot = document.getElementById(`live-sprint-progress-${projectId}`);

      if (sprintRoot) {

        sprintRoot.innerHTML = renderProjectLoadError(projectId, 'Sprint progress unavailable', sprintItemsPath, sprintItemsResult.reason);

        wireProjectLoadRetry(sprintRoot, projectId);

      }

    }



    if (sessionsResult.status === 'fulfilled' && tasksResult.status === 'fulfilled') {

      const worktrees = worktreesResult.status === 'fulfilled' ? (worktreesResult.value || []) : [];

      const sessions = sessionsResult.value || [];

      renderLiveSessions(projectId, sessions, tasksResult.value || [], worktrees);

      cacheMostRecentSession(projectId, sessions);

      // Sprint notes: load for the most recently active session.

      const activeSession = sessions.find(s => s.status === 'active') || sessions[0];

      if (activeSession && activeSession.id) {

        loadSprintNotesPanel(projectId, activeSession.id).catch(() => {});

      }

    } else {

      const sessionsRoot = document.getElementById(`live-sessions-${projectId}`);

      if (sessionsRoot) {

        const liveError = sessionsResult.status === 'rejected' ? sessionsResult.reason : tasksResult.reason;

        const livePath = sessionsResult.status === 'rejected' ? sessionsPath : tasksPath;

        sessionsRoot.innerHTML = renderProjectLoadError(projectId, 'Live sessions unavailable', livePath, liveError);

        wireProjectLoadRetry(sessionsRoot, projectId);

      }

    }



    if (tasksResult.status === 'fulfilled') {

      renderLiveQueue(projectId, tasksResult.value || []);

    } else {

      const queueRoot = document.getElementById(`live-queue-${projectId}`);

      if (queueRoot) {

        queueRoot.innerHTML = renderProjectLoadError(projectId, 'Live queue unavailable', tasksPath, tasksResult.reason);

        wireProjectLoadRetry(queueRoot, projectId);

      }

    }

  } catch(e) { /* ignore — WS will retry on next event */ }

}



// function renderSprintProgress -- moved to dashboard-sprint.js



function wireSprintAddEnter(projectId, root) {

  /** Allow Enter in the sprint-add input to submit. */

  const inp = root.querySelector(`#sprint-add-input-${projectId}`);

  if (inp) inp.onkeydown = e => { if (e.key === 'Enter') addSprintItemFromInput(projectId); };

}



async function sprintAction(projectId, itemId, action) {

  /** POST to one of the sprint-item action endpoints. */

  try {

    await api(`/projects/${projectId}/sprint-items/${itemId}/${action}`,

      { method: 'POST', body: JSON.stringify({}) });

    toast(`Sprint item ${action}d`);

    await refreshLiveTab(projectId);

  } catch(e) { toast(`Failed: ${e.message}`, true); }

}


async function sprintArchive(projectId, itemId) {

  /** e62ce019 — permanently delete a backburner item decided against. */

  if (!confirm('Permanently delete this backburner item? This cannot be undone.')) return;

  try {

    const r = await fetch(`/projects/${projectId}/sprint-items/${itemId}`, { method: 'DELETE' });

    if (!r.ok && r.status !== 204) throw new Error(`${r.status}`);

    toast('Backburner item deleted');

    await refreshLiveTab(projectId);

  } catch(e) { toast(`Delete failed: ${e.message}`, true); }

}


function filterBackburner(projectId, value) {

  /** e62ce019 — client-side filter of the backburner section by title/group. */

  const q = (value || '').trim().toLowerCase();

  const sec = document.querySelector('.queue-section[data-section="backburner"]');

  if (!sec) return;

  sec.querySelectorAll('.queue-item').forEach(el => {

    const hit = !q || (el.dataset.bbTitle || '').includes(q) || (el.dataset.bbGroup || '').includes(q);

    el.style.display = hit ? '' : 'none';

  });

  sec.querySelectorAll('.bb-group').forEach(g => {

    const anyVisible = Array.from(g.querySelectorAll('.queue-item')).some(el => el.style.display !== 'none');

    g.style.display = anyVisible ? '' : 'none';

  });

}



async function sprintPushPrompt(projectId, itemId) {

  /** Prompt for a target version then push the item. */

  const toVersion = window.prompt('Push to version (e.g. v2.0):');

  if (!toVersion) return;

  try {

    await api(`/projects/${projectId}/sprint-items/${itemId}/push`,

      { method: 'POST', body: JSON.stringify({ to_version: toVersion }) });

    toast('Sprint item pushed to ' + toVersion);

    await refreshLiveTab(projectId);

  } catch(e) { toast(`Push failed: ${e.message}`, true); }

}



async function sprintFeedback(projectId, itemId, thumb, currentThumb, event) {

  event && event.stopPropagation();

  const newThumb = currentThumb === thumb ? null : thumb;

  try {

    await api(`/projects/${projectId}/sprint-items/${itemId}`,

      { method: 'PATCH', body: JSON.stringify({ feedback_thumb: newThumb }) });

    await refreshLiveTab(projectId);

  } catch(e) { toast('Feedback failed: ' + e.message, true); }

}



async function sprintFeedbackNote(projectId, itemId, note) {

  if (!note || !note.trim()) return;

  try {

    await api(`/projects/${projectId}/sprint-items/${itemId}`,

      { method: 'PATCH', body: JSON.stringify({ feedback_note: note.trim() }) });

    await refreshLiveTab(projectId);

  } catch(e) { toast('Note save failed: ' + e.message, true); }

}



async function sprintItemEdit(projectId, itemId) {

  /** Inline-edit title and version of a sprint item. */

  const row = document.querySelector(`.sprint-item-row[data-item="${CSS.escape(itemId)}"]`);

  if (!row) return;

  const curTitle = row.dataset.title || '';

  const curVersion = row.dataset.version || '';

  const titleSpan = row.querySelector('.sprint-item-title');

  const verSpan = row.querySelector('.sprint-item-ver');

  if (!titleSpan || !verSpan) return;

  // Already editing?

  if (row.querySelector('.sprint-edit-input')) return;



  const titleInput = document.createElement('input');

  titleInput.className = 'sprint-edit-input';

  titleInput.value = curTitle;

  titleInput.style.cssText = 'flex:1;min-width:60px;background:var(--surface-1);border:1px solid var(--accent);border-radius:3px;padding:1px 4px;color:var(--text);font-size:12px;font-family:var(--font-mono)';



  const verInput = document.createElement('input');

  verInput.className = 'sprint-edit-input';

  verInput.value = curVersion;

  verInput.style.cssText = 'width:60px;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;padding:1px 4px;color:var(--muted);font-size:10px;font-family:var(--font-mono)';



  titleSpan.replaceWith(titleInput);

  verSpan.replaceWith(verInput);

  titleInput.focus();

  titleInput.select();



  const save = async () => {

    const newTitle = titleInput.value.trim();

    const newVersion = verInput.value.trim();

    if (!newTitle) { cancel(); return; }

    try {

      await api(`/projects/${projectId}/sprint-items/${itemId}`, {

        method: 'PATCH',

        body: JSON.stringify({ title: newTitle, version: newVersion || undefined }),

      });

      await refreshLiveTab(projectId);

    } catch(e) { toast(`Save failed: ${e.message}`, true); cancel(); }

  };

  const cancel = () => {

    titleInput.replaceWith(titleSpan);

    verInput.replaceWith(verSpan);

  };

  titleInput.onkeydown = verInput.onkeydown = e => {

    if (e.key === 'Enter') { e.preventDefault(); save(); }

    if (e.key === 'Escape') cancel();

  };

  titleInput.onblur = verInput.onblur = () => {

    // Small delay so blur from one field to the other doesn't trigger save.

    setTimeout(() => {

      if (!row.contains(document.activeElement)) save();

    }, 150);

  };

}



async function sprintItemNotesEdit(projectId, itemId) {

  /** Inline-edit the notes field of a sprint item. */

  const row = document.querySelector(`.sprint-item-row[data-item="${CSS.escape(itemId)}"]`);

  if (!row) return;

  const curNotes = row.dataset.notes || '';

  // Already editing?

  if (row.querySelector('.sprint-notes-textarea')) return;

  const existingNotesEl = row.querySelector('.sprint-item-notes');

  const textarea = document.createElement('textarea');

  textarea.className = 'sprint-notes-textarea';

  textarea.value = curNotes;

  textarea.placeholder = 'Add context, links, or notes…';

  textarea.style.cssText = 'width:100%;min-height:56px;background:var(--surface-1);border:1px solid var(--accent);border-radius:3px;padding:3px 5px;color:var(--text);font-size:10px;font-family:var(--font-mono);line-height:1.4;resize:vertical;box-sizing:border-box;margin-top:3px';

  if (existingNotesEl) {

    existingNotesEl.replaceWith(textarea);

  } else {

    const titleSpan = row.querySelector('.sprint-item-title');

    if (titleSpan) titleSpan.parentNode.insertBefore(textarea, titleSpan.nextSibling);

    else row.appendChild(textarea);

  }

  textarea.focus();

  const save = async () => {

    const newNotes = textarea.value.trim() || null;

    try {

      await api(`/projects/${projectId}/sprint-items/${itemId}`, {

        method: 'PATCH',

        body: JSON.stringify({ notes: newNotes }),

      });

      row.dataset.notes = newNotes || '';

      await refreshLiveTab(projectId);

    } catch(e) { toast(`Save failed: ${e.message}`, true); cancel(); }

  };

  const cancel = () => {

    if (existingNotesEl) textarea.replaceWith(existingNotesEl);

    else textarea.remove();

  };

  textarea.onkeydown = e => {

    if (e.key === 'Escape') { e.preventDefault(); cancel(); }

    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); save(); }

  };

  textarea.onblur = () => setTimeout(() => {

    if (!row.contains(document.activeElement)) save();

  }, 150);

}



async function loadSprintNotesPanel(projectId, sessionId) {

  /** Load and render ephemeral session scratch-pad notes in the sprint panel. */

  const section = document.getElementById(`sprint-notes-section-${projectId}`);

  const divider = document.getElementById(`sprint-notes-divider-${projectId}`);

  const container = document.getElementById(`sprint-notes-${projectId}`);

  if (!section || !container) return;

  try {

    const notes = await projectApi(projectId, `/sessions/${sessionId}/notes`);

    if (!notes || !notes.length) {

      section.style.display = 'none';

      if (divider) divider.style.display = 'none';

      return;

    }

    section.style.display = '';

    if (divider) divider.style.display = '';

    container.innerHTML = notes.map(n => `

      <div style="background:var(--surface-2);border:1px solid var(--border);border-left:3px solid var(--accent-green,#22c55e);border-radius:0 4px 4px 0;padding:6px 8px;margin-bottom:6px">

        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">

          <span style="color:var(--accent-green,#22c55e);font-weight:600;font-size:11px">${escapeHtml(n.title || '')}</span>

          <span style="color:var(--muted);font-size:9px">${escapeHtml((n.created_at || '').slice(0, 16).replace('T', ' '))}</span>

        </div>

        <div style="color:var(--text);font-size:10px;line-height:1.5;white-space:pre-wrap;word-break:break-word">${typeof marked !== 'undefined' ? marked.parse(n.body || '') : escapeHtml(n.body || '')}</div>

      </div>

    `).join('');

  } catch (_) {

    section.style.display = 'none';

    if (divider) divider.style.display = 'none';

  }

}



async function addSprintItemFromInput(projectId) {

  /** Parse "version:title" or fall back to current sprint version. */

  const inp = document.getElementById(`sprint-add-input-${projectId}`);

  if (!inp) return;

  const val = inp.value.trim();

  if (!val) return;

  let version, title;

  const colonIdx = val.indexOf(':');

  if (colonIdx > 0) {

    version = val.slice(0, colonIdx).trim();

    title   = val.slice(colonIdx + 1).trim();

  } else {

    // Fall back to the sprint header text as a rough version token.

    const panel = state.panels[projectId];

    const sprint = (panel && panel._serverSprint) || '';

    const m = sprint.match(/v[\w.+-]+/i);

    version = m ? m[0] : 'current';

    title = val;

  }

  if (!title) { toast('Title required', true); return; }

  if (title.length > 500) { toast('Title too long (500 char limit)', true); inp.style.borderColor = 'var(--red, #f87171)'; return; }

  try {

    await api(`/projects/${projectId}/sprint-items`, {

      method: 'POST',

      body: JSON.stringify({ version, title }),

    });

    inp.value = '';

    inp.style.borderColor = '';

    toast('Sprint item added');

    await refreshLiveTab(projectId);

  } catch(e) { toast('Add failed: ' + e.message, true); }

}



function cacheMostRecentSession(projectId, sessions) {

  /** Pick the most recent active session id for "add task" attribution. */

  const panel = state.panels[projectId];

  if (!panel) return;

  const sorted = sessions.slice().sort((a, b) =>

    (b.last_seen || '').localeCompare(a.last_seen || '')

  );

  const top = sorted.find(s => isLiveSession(s)) || sorted.find(s => s.status !== 'closed') || sorted[0];

  if (top) panel.liveLastSessionId = top.id;

}



function renderLiveSessions(projectId, sessions, tasks, worktrees) {

  const root = document.getElementById(`live-sessions-${projectId}`);

  if (!root) return;

  // Build a map of session_id → active worktree branches
  const worktreeMap = new Map();
  (worktrees || []).forEach(wt => {
    if (!worktreeMap.has(wt.session_id)) worktreeMap.set(wt.session_id, []);
    worktreeMap.get(wt.session_id).push(wt.branch);
  });

  const claimMap = new Map();
  const taskMap = new Map();

  tasks.forEach(t => {

    if (t.claimed_by && (t.status === 'pending' || t.status === 'in_progress')) {

      claimMap.set(t.claimed_by, t);

    }

    const sid = t.session_id || t.claimed_by;
    if (sid) {
      if (!taskMap.has(sid)) taskMap.set(sid, []);
      taskMap.get(sid).push(t);
    }

  });
  taskMap.forEach(rows => rows.sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || ''))));

  const rows = sessions

    .map(s => {

      const ageMs = sessionAgeMs(s);

      return { s, ageMs };

    })

    .filter(({ ageMs }) => ageMs > 0 && ageMs <= 24 * 3600 * 1000)

    .sort((a, b) => a.ageMs - b.ageMs);

  // dc234d4e — hide the Active sessions panel entirely when nothing is live in
  // the last 10 min, instead of showing an empty "No active sessions" block
  // (clutter on camera). It reappears on the next refresh once a session is live.
  const LIVE_PRESENCE_MS = 10 * 60 * 1000;
  const liveSection = root.closest('.live-section');
  const sectionDivider = liveSection ? liveSection.nextElementSibling : null;
  const anyLivePresence = rows.some(({ ageMs }) => ageMs <= LIVE_PRESENCE_MS);
  if (!anyLivePresence) {
    if (liveSection) liveSection.style.display = 'none';
    if (sectionDivider && sectionDivider.classList && sectionDivider.classList.contains('live-divider')) sectionDivider.style.display = 'none';
    root.innerHTML = '';
    return;
  }
  if (liveSection) liveSection.style.display = '';
  if (sectionDivider && sectionDivider.classList && sectionDivider.classList.contains('live-divider')) sectionDivider.style.display = '';

  if (!rows.length) {

    root.innerHTML = '<div class="live-empty">No active sessions.</div>';

    return;

  }

  root.innerHTML = rows.map(({ s, ageMs }) => {

    const mins = ageMs / 60000;

    const live = isLiveSession(s, ageMs);
    const dot = live ? (mins < 5 ? '🟢' : '🟡') : '⚫';
    const displayStatus = live ? 'live' : (s.status === 'closed' || s.status === 'archived' ? s.status : 'idle');

    const label = s.human_id ? `${s.human_id}/${s.name}` : s.name;

    const claimed = claimMap.get(s.id);

    const claimedRow = claimed

      ? `<div class="live-session-task">↳ ${escapeHtml((claimed.description || '').slice(0, 140))}</div>`

      : '';
    const sessionTasks = taskMap.get(s.id) || [];
    const taskRows = sessionTasks.slice(0, 3).map(t =>
      `<div class="live-session-task">↳ ${escapeHtml((t.description || '').slice(0, 140))}</div>`
    ).join('') || claimedRow;
    const taskLink = sessionTasks.length > 0
      ? `<button class="link-button live-session-task-link" data-session-id="${escapeHtml(s.id)}" style="margin-left:18px;margin-top:3px;font-size:10px;color:var(--accent);background:none;border:none;padding:0;cursor:pointer">View all ${sessionTasks.length} tasks →</button>`
      : '';

    const summary = s.session_summary;

    const summaryRow = (summary && summary.summary && (s.status === 'closed' || s.status === 'archived'))

      ? `<div class="live-session-outcome" style="font-size:10px;color:var(--muted);margin-top:3px;padding-left:18px">`

        + `✓ ${escapeHtml((summary.summary || '').slice(0, 160))}`

        + (summary.tasks_completed != null ? ` · ${summary.tasks_completed} tasks` : '')

        + `</div>`

      : '';

    // v2.4 — framework badge (claude_code / cursor / windsurf / langgraph

    // / autogen / openviking / custom). Only render when non-default so

    // the bulk of Claude-driven sessions stay visually quiet.

    const fw = s.agent_framework || 'claude_code';

    const fwBadge = (fw && fw !== 'claude_code')

      ? `<span class="framework-badge" title="framework: ${escapeHtml(fw)}" style="display:inline-block;background:var(--surface-2);color:var(--accent);font-size:9px;font-weight:600;padding:1px 5px;border-radius:3px;margin-left:4px">${escapeHtml(fw)}</span>`

      : '';

    const sessionWorktrees = worktreeMap.get(s.id) || [];

    const worktreeBadges = sessionWorktrees.map(branch =>
      `<span class="worktree-badge" title="active worktree: ${escapeHtml(branch)}" style="display:inline-block;background:var(--surface-2);color:#a78bfa;font-size:9px;font-weight:600;padding:1px 5px;border-radius:3px;margin-left:4px">⎇ ${escapeHtml(branch.replace('worktree/', ''))}</span>`
    ).join('');

    const endBtn = live
      ? `<button class="secondary live-session-end" data-session-id="${escapeHtml(s.id)}" style="padding:1px 6px;font-size:9px;margin-left:6px" title="Mark this session idle">End session</button>`
      : '';

    return `<div class="live-session-row" data-session-status="${escapeHtml(displayStatus)}">

      <div class="live-session-head">

        <span class="live-dot">${dot}</span>

        <span class="live-session-name">${escapeHtml(label)}</span>${fwBadge}${worktreeBadges}

        <span class="live-session-status" style="font-size:9px;color:var(--muted);text-transform:uppercase">${escapeHtml(displayStatus)}</span>

        <span class="live-session-age">${escapeHtml(formatRelativeTime(s.last_seen))}</span>

        ${endBtn}

      </div>

      ${taskRows}${taskLink}${summaryRow}

    </div>`;

  }).join('');

  root.querySelectorAll('.live-session-end').forEach(btn => {
    btn.onclick = () => endLiveSession(projectId, btn.dataset.sessionId);
  });
  root.querySelectorAll('.live-session-task-link').forEach(btn => {
    btn.onclick = () => openTimelineForSession(projectId, btn.dataset.sessionId);
  });

}

async function endLiveSession(projectId, sessionId) {
  if (!sessionId) return;
  try {
    await api(`/sessions/${sessionId}`, {
      method: 'PATCH',
      body: JSON.stringify({ status: 'idle' }),
    });
    toast('Session marked idle');
    await refreshLiveTab(projectId);
  } catch(e) {
    toast(`End session failed: ${e.message}`, true);
  }
}

function openTimelineForSession(projectId, sessionId) {
  const panel = getPanelState(projectId);
  panel.timelineSessionFilter = sessionId || null;
  try { localStorage.setItem('meridian_tl_view_' + projectId, 'tasks'); } catch(_) {}
  const btn = document.querySelector(`#vtab-strip-${projectId} .vtab-btn[data-vtab="timeline"]`);
  if (btn) btn.click();
  else loadTimeline(projectId);
}



function renderLiveQueue(projectId, tasks) {

  const root = document.getElementById(`live-queue-${projectId}`);

  if (!root) return;

  const live = tasks.filter(t => t.status === 'pending' || t.status === 'in_progress');

  if (!live.length) {

    root.innerHTML = '<div class="live-empty">Queue is empty. Add a task above.</div>';

    return;

  }

  live.sort((a, b) => {

    if (a.status !== b.status) return a.status === 'in_progress' ? -1 : 1;

    return (b.created_at || '').localeCompare(a.created_at || '');

  });

  root.innerHTML = live.map(t => {

    const dot = t.status === 'in_progress' ? '🔵' : '📋';

    const claimLabel = t.claimed_by_human_id || t.claimed_by_session_name || t.claimed_by || '';

    const claimed = t.claimed_by

      ? `<span class="live-task-claim">claimed by: ${escapeHtml(claimLabel.slice(0, 24))}</span>`

      : '';

    const ts = formatRelativeTime(t.created_at);

    const eid = `live-expand-${projectId}-${t.id.slice(0, 8)}`;

    const expandMeta = [

      t.session_name ? `session: ${t.session_name}` : '',

      t.claimed_by   ? `claimed_by: ${t.claimed_by_human_id || t.claimed_by_session_name || t.claimed_by}` : '',

      t.created_at   ? `created: ${t.created_at}` : '',

      t.claimed_at   ? `claimed: ${t.claimed_at}` : '',

    ].filter(Boolean).join(' · ');

    return `<div class="live-task-row" data-task="${escapeHtml(t.id)}">

      <span class="live-dot">${dot}</span>

      <div class="live-task-body" style="cursor:pointer" onclick="toggleExpand('${eid}')">

        <div class="live-task-desc">${escapeHtml((t.description || '').slice(0, 200))}</div>

        <div class="live-task-meta">${escapeHtml(ts)} ${claimed} <span class="expand-arrow" style="font-size:9px;color:var(--muted)">▶</span></div>

        <div id="${eid}" style="display:none;margin-top:4px;font-size:10px;color:var(--muted);white-space:pre-wrap;word-break:break-word">${escapeHtml(t.description || '')}${expandMeta ? '\n' + escapeHtml(expandMeta) : ''}</div>

      </div>

      <button class="live-task-cancel" data-cancel="${escapeHtml(t.id)}" title="Mark done / cancel">×</button>

    </div>`;

  }).join('');

  root.querySelectorAll('button[data-cancel]').forEach(btn => {

    btn.onclick = () => cancelLiveTask(projectId, btn.dataset.cancel);

  });

}



async function addLiveTask(projectId, description) {

  /** POST /tasks with the most recent active session as attribution. */

  const panel = state.panels[projectId];

  const sessionId = panel && panel.liveLastSessionId;

  if (!sessionId) {

    // Try to discover one synchronously.

    try {

      const sessions = await api(`/projects/${projectId}/sessions`);

      cacheMostRecentSession(projectId, sessions || []);

    } catch(e) {}

  }

  const sid = panel && panel.liveLastSessionId;

  if (!sid) {

    toast('No active session to attribute the task to', true);

    return false;

  }

  try {

    await api('/tasks', {

      method: 'POST',

      body: JSON.stringify({

        session_id: sid, project_id: projectId,

        description, status: 'pending',

      }),

    });

    toast('task queued');

    await refreshLiveTab(projectId);

    return true;

  } catch(e) {

    toast('add task failed: ' + e.message, true);

    return false;

  }

}



async function cancelLiveTask(projectId, taskId) {

  /** PATCH /tasks/{id} → status=done. WebSocket broadcast triggers refresh. */

  try {

    await api(`/tasks/${taskId}`, {

      method: 'PATCH',

      body: JSON.stringify({ status: 'done' }),

    });

    toast('task closed');

    await refreshLiveTab(projectId);

  } catch(e) {

    toast('cancel failed: ' + e.message, true);

  }

}



// v1.5.x — Claude launch control panel. Wires the 4 sections:

function showCopyPreview(title, content) {

  const overlay = document.createElement('div');

  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.72);z-index:9998;display:flex;align-items:center;justify-content:center';

  overlay.innerHTML = `<div style="background:var(--surface-1);border:1px solid var(--border);border-radius:6px;padding:20px;width:620px;max-width:92vw;max-height:80vh;display:flex;flex-direction:column;gap:12px;box-shadow:0 8px 32px #0008">

    <div style="font-family:var(--font-mono);font-size:13px;font-weight:600;color:var(--accent)">${escapeHtml(title)}</div>

    <textarea style="width:100%;height:300px;font-family:var(--font-mono);font-size:11px;background:#0d1117;color:#e6edf3;border:1px solid var(--border);padding:10px;border-radius:4px;resize:vertical;outline:none">${escapeHtml(content)}</textarea>

    <div style="display:flex;gap:8px;justify-content:flex-end">

      <button class="secondary" style="font-size:11px;padding:5px 14px">Cancel</button>

      <button class="primary" style="font-size:11px;padding:5px 14px">Copy &amp; Close</button>

    </div>

  </div>`;

  document.body.appendChild(overlay);

  const [cancelBtn, copyBtn] = overlay.querySelectorAll('button');

  const ta = overlay.querySelector('textarea');

  const close = () => overlay.remove();

  cancelBtn.onclick = close;

  overlay.addEventListener('click', e => { if (e.target === overlay) close(); });

  copyBtn.onclick = async () => {

    try {

      await navigator.clipboard.writeText(ta.value);

    } catch(_) {

      ta.select();

      document.execCommand('copy');

    }

    copyBtn.textContent = 'Copied!';

    setTimeout(close, 700);

  };

}



// (1) continue session dropdown + copy resume command

// (2) start worker → show XML → copy

// (3) handoff copy + regenerate

// (4) open in claude.ai

function wireClaudeLaunchPanel(projectId) {

  const PROJECT_QUOTE = projectId.replace(/"/g, '\\"');
  const sequentialKey = `meridian.sequentialMode.${projectId}`;

  function normalizeTouchesFile(path) {
    return String(path || '').trim().replace(/\\/g, '/').replace(/^\.\//, '');
  }

  function parseTouchesFiles(raw) {
    if (!raw) return [];
    if (Array.isArray(raw)) return raw.map(normalizeTouchesFile).filter(Boolean);
    const text = String(raw).trim();
    if (!text) return [];
    try {
      const parsed = JSON.parse(text);
      if (Array.isArray(parsed)) return parsed.map(normalizeTouchesFile).filter(Boolean);
      return [normalizeTouchesFile(parsed)].filter(Boolean);
    } catch(e) {
      return text.split(',').map(normalizeTouchesFile).filter(Boolean);
    }
  }

  function findTouchesFilesConflicts(items) {
    const active = (items || []).filter(it => ['pending', 'todo', 'in_progress'].includes(it.status || 'pending'));
    const byFile = new Map();
    active.forEach((item) => {
      parseTouchesFiles(item.touches_files).forEach((file) => {
        const key = file.toLowerCase();
        const list = byFile.get(key) || [];
        list.push({ file, item });
        byFile.set(key, list);
      });
    });
    return Array.from(byFile.values())
      .filter(list => list.length > 1 && list.some(entry => entry.item.status === 'in_progress'))
      .flat();
  }

  function applySequentialMode(text) {
    const toggle = document.getElementById(`sequential-mode-${projectId}`);
    if (!toggle || !toggle.checked || !text) return text;
    return `${text}\n\nSEQUENTIAL MODE:\n- Work one sprint item at a time.\n- Call claim_file(session_id, path) before editing shared files.\n- Stop and coordinate if start_session returns file_warnings or claim_sprint_item returns CONFLICT.`;
  }

  async function warnBeforeHandoffCopy() {
    try {
      const items = await projectApi(projectId, `/projects/${projectId}/sprint-items`);
      const conflicts = findTouchesFilesConflicts(items || []);
      const warnEl = document.getElementById(`touches-files-warning-${projectId}`);
      if (warnEl) warnEl.style.display = conflicts.length ? '' : 'none';
      if (!conflicts.length) return true;
      const files = Array.from(new Set(conflicts.map(c => c.file))).join(', ');
      return confirm(`touches_files conflict warning:\n\n${files}\n\nContinue copying the handoff?`);
    } catch(e) {
      return true;
    }
  }

  const sequentialToggle = document.getElementById(`sequential-mode-${projectId}`);
  if (sequentialToggle) {
    try { sequentialToggle.checked = localStorage.getItem(sequentialKey) === '1'; } catch(e) {}
    sequentialToggle.onchange = () => {
      try { localStorage.setItem(sequentialKey, sequentialToggle.checked ? '1' : '0'); } catch(e) {}
    };
  }



  // Section 0 — "Start a Session" copy buttons + Auto-setup hooks

  const copyStartCodeBtn = document.getElementById(`copy-start-code-${projectId}`);

  if (copyStartCodeBtn) copyStartCodeBtn.onclick = () => {

    const cmd = `start_session(project_id="${PROJECT_QUOTE}", session_name="describe-what-youre-doing", human_id="adam")`;

    showCopyPreview('Start Claude Code Session', cmd);

  };



  const copyStartChatBtn = document.getElementById(`copy-start-chat-${projectId}`);

  if (copyStartChatBtn) copyStartChatBtn.onclick = async () => {

    const orig = copyStartChatBtn.textContent;

    copyStartChatBtn.disabled = true;

    copyStartChatBtn.textContent = 'Loading…';

    try {
      if (!(await warnBeforeHandoffCopy())) return;

      const r = await fetch(`/projects/${projectId}/handoff`, { method: 'POST' });

      if (!r.ok) throw new Error(`${r.status}`);

      const payload = await r.json();

      const text = applySequentialMode(payload.content || '');

      showCopyPreview('Claude / Codex Handoff', text);

    } catch(e) { toast('handoff failed: ' + e.message, true); }

    finally { copyStartChatBtn.disabled = false; copyStartChatBtn.textContent = orig; }

  };



  const setupHooksBtn = document.getElementById(`btn-setup-hooks-${projectId}`);

  if (setupHooksBtn) setupHooksBtn.onclick = () => {

    const baseUrl = window.location.origin;

    const instructions = `Auto-setup Meridian hooks for your AI tools:\n\n` +

      `macOS / Linux / WSL:\n  curl -fsSL ${baseUrl}/install.sh | sh\n\n` +

      `Windows PowerShell:\n  irm ${baseUrl}/install.ps1 | iex\n\n` +

      `These scripts detect Claude Code and Codex, then wire SessionStart + Stop\n` +

      `hooks pointing to ${baseUrl}/hooks/ with your project_id.\n\n` +

      `Project ID: ${projectId}`;

    showCopyPreview('⚡ Setup Hooks', instructions);

  };



  // Demo mode: disable write-action buttons with tooltip, show toast on click

  if (isDemoMode()) {

    [

      copyStartChatBtn,

      setupHooksBtn,

      document.getElementById(`copy-resume-${projectId}`),

      document.getElementById(`start-worker-${projectId}`),

      document.getElementById(`copy-handoff-${projectId}`),

      document.getElementById(`regen-handoff-${projectId}`),

    ].forEach(btn => {

      if (!btn) return;

      btn.title = 'Sign in to use';

      btn.style.opacity = '0.45';

      btn.style.cursor = 'not-allowed';

      btn.onclick = (e) => { e.preventDefault(); showDemoReadonlyToast(); };

    });

    return;

  }



  // Section 1 — Copy resume command

  const copyResumeBtn = document.getElementById(`copy-resume-${projectId}`);

  if (copyResumeBtn) copyResumeBtn.onclick = async () => {

    const sel = document.getElementById(`continue-session-${projectId}`);

    const sessionName = sel && sel.value ? sel.value : '';

    if (!sessionName) { toast('pick a session first', true); return; }

    const cmd = `start_session(project_id="${PROJECT_QUOTE}", session_name="${sessionName.replace(/"/g, '\\"')}", human_id="adam")\nget_context_block(project_id="${PROJECT_QUOTE}", mode="full")`;

    showCopyPreview('Resume MCP Flow', cmd);

  };



  // Section 2 — Start Worker

  const startWorkerBtn = document.getElementById(`start-worker-${projectId}`);

  if (startWorkerBtn) startWorkerBtn.onclick = async () => {

    if (isDemoMode()) { showDemoReadonlyToast(); return; }

    const resultEl = document.getElementById(`worker-result-${projectId}`);

    const emptyEl = document.getElementById(`worker-empty-${projectId}`);

    const xmlEl = document.getElementById(`worker-xml-${projectId}`);

    if (resultEl) resultEl.style.display = 'none';

    if (emptyEl) emptyEl.style.display = 'none';

    try {

      const r = await fetch(`/projects/${projectId}/start-worker-session`, {

        method: 'POST',

        headers: { 'Content-Type': 'application/json' },

        body: JSON.stringify({}),

      });

      if (r.status === 404) {

        if (emptyEl) emptyEl.style.display = '';

        return;

      }

      if (!r.ok) throw new Error(`${r.status}`);

      const body = await r.json();

      const xml = body.worker_context || '';

      if (xmlEl) xmlEl.textContent = xml;

      if (resultEl) resultEl.style.display = '';

      toast('worker session ready');

    } catch(e) { toast('start worker failed: ' + e.message, true); }

  };

  const copyWorkerBtn = document.getElementById(`copy-worker-${projectId}`);

  if (copyWorkerBtn) copyWorkerBtn.onclick = async () => {

    const xmlEl = document.getElementById(`worker-xml-${projectId}`);

    const text = xmlEl ? xmlEl.textContent : '';

    if (!text) { toast('nothing to copy', true); return; }

    try {

      await navigator.clipboard.writeText(text);

      toast('worker context copied');

    } catch(e) { toast('copy failed: ' + e.message, true); }

  };



  // Section 3 — Handoff copy + regenerate

  const copyHandoffBtn = document.getElementById(`copy-handoff-${projectId}`);

  if (copyHandoffBtn) copyHandoffBtn.onclick = async () => {

    const orig = copyHandoffBtn.textContent;

    copyHandoffBtn.disabled = true;

    copyHandoffBtn.textContent = 'Loading…';

    try {
      if (!(await warnBeforeHandoffCopy())) return;

      const r = await fetch(`/projects/${projectId}/handoff`, { method: 'POST' });

      if (!r.ok) throw new Error(`${r.status}`);

      const payload = await r.json();

      const text = applySequentialMode(payload.content || '');

      if (text) {

        const rawContainer = document.getElementById(`handoff-raw-${projectId}`);

        const rawTextEl = document.getElementById(`handoff-raw-text-${projectId}`);

        if (rawContainer && rawTextEl) {

          rawTextEl.value = text;

          rawContainer.style.display = '';

          rawTextEl.focus();

          rawTextEl.select();

        }

        try { await navigator.clipboard.writeText(text); toast('handoff copied to clipboard'); }

        catch(_) { toast('text shown below — select all and copy'); }

        stampHandoffTs(projectId, new Date());

      }

    } catch(e) { toast('handoff failed: ' + e.message, true); }

    finally { copyHandoffBtn.disabled = false; copyHandoffBtn.textContent = orig; }

  };

  const handoffCopyTextBtn = document.getElementById(`handoff-copy-text-${projectId}`);

  if (handoffCopyTextBtn) handoffCopyTextBtn.onclick = async () => {

    const rawTextEl = document.getElementById(`handoff-raw-text-${projectId}`);

    if (!rawTextEl) return;

    try { await navigator.clipboard.writeText(rawTextEl.value); toast('copied'); }

    catch(_) { rawTextEl.select(); document.execCommand('copy'); }

  };

  const handoffCloseBtn = document.getElementById(`handoff-close-raw-${projectId}`);

  if (handoffCloseBtn) handoffCloseBtn.onclick = () => {

    const rawContainer = document.getElementById(`handoff-raw-${projectId}`);

    if (rawContainer) rawContainer.style.display = 'none';

  };

  const copyContextBtn = document.getElementById(`copy-context-${projectId}`);

  if (copyContextBtn) copyContextBtn.onclick = async () => {

    const orig = copyContextBtn.textContent;

    copyContextBtn.disabled = true;

    copyContextBtn.textContent = 'Loading…';

    try {

      const r = await fetch(`/projects/${projectId}/context-block?mode=chat`);

      if (!r.ok) throw new Error(`${r.status}`);

      const text = await r.text();

      showCopyPreview('Chat Context — paste into claude.ai', text);

    } catch(e) { toast('copy context failed: ' + e.message, true); }

    finally { copyContextBtn.disabled = false; copyContextBtn.textContent = orig; }

  };



  const regenBtn = document.getElementById(`regen-handoff-${projectId}`);

  if (regenBtn) regenBtn.onclick = async () => {

    const tsEl = document.getElementById(`handoff-ts-${projectId}`);

    const orig = regenBtn.textContent;

    regenBtn.disabled = true;

    regenBtn.textContent = 'Regenerating…';

    try {

      const r = await fetch(`/projects/${projectId}/handoff`, { method: 'POST' });

      if (!r.ok) throw new Error(`${r.status}`);

      await r.json();

      stampHandoffTs(projectId, new Date());

      if (tsEl) {

        const prev = tsEl.textContent;

        tsEl.textContent = 'Regenerated ✓';

        setTimeout(() => stampHandoffTs(projectId, new Date()), 2000);

      }

      toast('handoff regenerated');

    } catch(e) { toast('regenerate failed: ' + e.message, true); }

    finally {

      regenBtn.disabled = false;

      regenBtn.textContent = orig;

    }

  };

}



function stampHandoffTs(projectId, when) {

  const tsEl = document.getElementById(`handoff-ts-${projectId}`);

  if (!tsEl) return;

  const iso = when.toISOString().replace('T', ' ').slice(0, 19);

  tsEl.textContent = 'Last generated: ' + formatRelativeTime(iso);

}



function populateSessionDropdown(projectId, sessions) {

  /** v1.5.x — fill the "Continue session" dropdown with the last 5 sessions

   * (newest first by last_seen). Each option label: "{name} — {age} ago". */

  const sel = document.getElementById(`continue-session-${projectId}`);

  if (!sel) return;

  const sorted = (sessions || []).slice().sort((a, b) =>

    (b.last_seen || '').localeCompare(a.last_seen || '')

  ).slice(0, 5);

  if (!sorted.length) {

    sel.innerHTML = '<option value="">(no sessions yet)</option>';

    return;

  }

  const prev = sel.value;

  sel.innerHTML = sorted.map(s => {

    const label = `${s.name} — ${formatRelativeTime(s.last_seen)}`;

    return `<option value="${escapeHtml(s.name)}">${escapeHtml(label)}</option>`;

  }).join('');

  if (prev && sorted.some(s => s.name === prev)) sel.value = prev;

}



// v1.1.1 — Activity Timeline. Load /timeline, render task history

// per session, paint task pills positioned on a shared time axis.

async function loadTimeline(projectId) {

  const wrap = document.getElementById(`timeline-wrap-${projectId}`);

  if (!wrap) return;

  wrap.innerHTML = `<div class="timeline-empty">loading…</div>`;

  let data;

  try {

    data = await api(`/projects/${projectId}/timeline`);

  } catch (e) {

    wrap.innerHTML = `<div class="timeline-empty">timeline failed: ${escapeHtml(e.message)}</div>`;

    return;

  }

  renderTimeline(projectId, data);

  const axisBtn = document.getElementById(`timeline-axis-${projectId}`);

  if (axisBtn) axisBtn.style.display = 'none';

  const refreshBtn = document.getElementById(`timeline-refresh-${projectId}`);

  if (refreshBtn) refreshBtn.onclick = () => loadTimeline(projectId);

}



// function renderTimeline -- moved to dashboard-demo.js / dashboard-timeline.js



// function _heatmapPieces -- moved to dashboard-demo.js / dashboard-timeline.js



// function _heatmapMaxFor -- moved to dashboard-demo.js / dashboard-timeline.js



// function _renderTimelineHeatmap -- moved to dashboard-demo.js / dashboard-timeline.js



// function _renderTimelineGantt -- moved to dashboard-demo.js / dashboard-timeline.js



function _renderTimelineLog(projectId, data) {

  /** Fallback text log when vis-timeline isn't available. */

  const wrap = document.getElementById(`timeline-wrap-${projectId}`);

  if (!wrap) return;

  const { tasks = [], goal_events = [] } = data || {};

  const isAbs = !!(state.panels[projectId] && state.panels[projectId]._timelineAbsolute);

  const fmtTs = ts => {

    if (!ts) return '';

    const iso = ts.includes('T') ? ts : ts.replace(' ', 'T') + 'Z';

    return isAbs ? new Date(iso).toISOString().replace('T',' ').slice(0,16) : formatRelativeTime(ts);

  };

  const events = [];

  tasks.forEach(t => {

    const icon = { done: '✅', failed: '❌' }[t.status] || '•';

    events.push({ ts: t.created_at, actor: t.session_name || '(unknown)', desc: `${icon} ${(t.description || '').slice(0, 100)}` });

  });

  const goalByField = new Map();

  goal_events.forEach(g => {

    const key = g.field + (g.updated_at || '').slice(0, 13);

    if (!goalByField.has(key) || g.version > (goalByField.get(key).version || 0)) goalByField.set(key, g);

  });

  goalByField.forEach(g => events.push({ ts: g.updated_at || '', actor: 'goal', desc: `📋 ${g.field} → v${g.version}` }));

  events.sort((a, b) => (b.ts || '').localeCompare(a.ts || ''));

  wrap.innerHTML = `<div class="timeline-log">${events.map(e =>

    `<div class="timeline-log-entry"><span class="timeline-log-ts">${escapeHtml(fmtTs(e.ts))}</span><span class="timeline-log-actor">${escapeHtml(e.actor)}</span><span class="timeline-log-desc">${escapeHtml(e.desc)}</span></div>`

  ).join('')}</div>`;

}



// const _HUMAN_COLORS, function _colorForHuman -- moved to dashboard-utils.js



// Tool categories for the Docs vtab grouping

const _TOOL_CATEGORIES = {

  goal: ['set_goal', 'get_goal', 'set_north_star', 'set_sprint'],

  task: ['log_task', 'complete_task', 'fail_sprint_item', 'add_sprint_item', 'get_tasks',

         'complete_sprint_item', 'skip_sprint_item', 'push_sprint_item', 'get_sprint_items'],

  session: ['start_session', 'register_session', 'get_sessions', 'start_worker_session',

             'heartbeat', 'get_context_block', 'claim_task', 'release_task', 'enqueue_claude_task'],

  hitl: ['request_hitl', 'get_hitl_request'],

  notes: ['add_note', 'get_notes', 'delete_note'],

  decisions: ['pin_decision', 'get_pinned_decisions', 'set_decision', 'update_decision'],

  project: ['create_project', 'list_projects', 'get_project_by_name', 'generate_handoff'],

};

const _CATEGORY_LABELS = {

  goal: 'Goal Tools', task: 'Task & Sprint Tools', session: 'Session Tools',

  hitl: 'HITL Tools', notes: 'Notes Tools', decisions: 'Decision Tools', project: 'Project Tools',

};



async function loadDocsTab(projectId) {

  const body = document.getElementById(`docs-body-${projectId}`);

  if (!body) return;

  if (body.dataset.loaded) return;

  body.dataset.loaded = '1';

  try {

    const tools = await api('/tools');

    if (!tools || !tools.length) { body.innerHTML = '<div class="empty" style="color:var(--muted)">No tools returned.</div>'; return; }

    // Build lookup

    const byName = {};

    tools.forEach(t => { byName[t.name] = t; });

    // Render by category

    let html = '';

    // Determine category for each tool

    const categorized = new Set();

    for (const [cat, names] of Object.entries(_TOOL_CATEGORIES)) {

      const catTools = names.map(n => byName[n]).filter(Boolean);

      if (!catTools.length) continue;

      html += `<div style="margin-bottom:18px"><div style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:8px;padding-bottom:4px;border-bottom:1px solid var(--border)">${_CATEGORY_LABELS[cat]}</div>`;

      catTools.forEach(tool => {

        categorized.add(tool.name);

        html += _renderToolEntry(tool);

      });

      html += '</div>';

    }

    // Catch-all for uncategorized tools

    const rest = tools.filter(t => !categorized.has(t.name));

    if (rest.length) {

      html += `<div style="margin-bottom:18px"><div style="color:var(--accent);font-size:10px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:8px;padding-bottom:4px;border-bottom:1px solid var(--border)">Other</div>`;

      rest.forEach(tool => { html += _renderToolEntry(tool); });

      html += '</div>';

    }

    const _toolSearch = `<div style="position:sticky;top:0;background:var(--surface-1,#10131a);padding:0 0 8px;margin-bottom:6px;z-index:2"><input type="text" id="docs-search-${projectId}" placeholder="Search tools by name or description…" style="width:100%;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:12px;font-family:var(--font-mono);padding:5px 9px;outline:none"></div>`;

    body.innerHTML = _toolSearch + html;

    _wireTabSearch(`docs-search-${projectId}`, `docs-body-${projectId}`, '.tool-entry');

  } catch (e) {

    body.innerHTML = `<div style="color:var(--error)">Failed to load tools: ${escapeHtml(String(e))}</div>`;

  }

}



// Code Intel tab — show only when tunnel:code socket is live.
// Called once per project panel after vtab strip is wired.
async function _initCodeIntelTabVisibility(projectId) {
  if (!window.MERIDIAN_HOSTED) return;
  try {
    const data = await api('/tunnel/plugins');
    const btn = document.getElementById(`vtab-codeintel-${projectId}`);
    if (!btn) return;
    const isActive = !!(data && data.active && data.active.code);
    btn.style.display = isActive ? '' : 'none';
  } catch (_) {}
}

// Code-intel projects are keyed by a slug derived from the repo's root path:
// drive-colon and path separators (\\ / :) collapse to single dashes, e.g.
// C:\Users\13144\Documents\Meridian\repository -> C-Users-13144-Documents-Meridian-repository.
// The index_status / get_architecture tools take this `project` slug, NOT a raw repo_path.
function _repoPathToProject(repoPath) {
  return String(repoPath || '').replace(/[\\/:]+/g, '-').replace(/^-+|-+$/g, '');
}

async function loadCodeIntelTab(projectId) {
  const body = document.getElementById(`codeintel-body-${projectId}`);
  if (!body) return;

  body.innerHTML = '<div class="empty" style="color:var(--muted)">loading…</div>';

  try {
    const [pluginsData, meData, settingsData] = await Promise.all([
      api('/tunnel/plugins'),
      api('/me'),
      loadProjectSettings(projectId),
    ]);

    if (!pluginsData?.active?.code) {
      body.innerHTML = '<div class="empty" style="color:var(--muted)">Code intel tunnel is not active. Run <code>meridian --tunnel</code> to connect it.</div>';
      return;
    }

    const tenantId = meData?.tenant_id;
    if (!tenantId) {
      body.innerHTML = '<div class="empty" style="color:var(--error)">Could not resolve tenant ID from /me.</div>';
      return;
    }

    const codeBase = `/code/mcp/${tenantId}/mcp`;

    async function _codeMcpCall(method, params) {
      const r = await fetch(codeBase, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'},
        body: JSON.stringify({jsonrpc: '2.0', id: 1, method, params: params || {}}),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const text = await r.text();
      let parsed = null;
      if (text.trim().startsWith('{')) {
        parsed = JSON.parse(text);
      } else {
        for (const line of text.split('\n')) {
          if (line.startsWith('data:')) {
            try { parsed = JSON.parse(line.slice(5).trim()); } catch (_) {}
          }
        }
      }
      if (!parsed) throw new Error('empty response from code MCP');
      if (parsed.error) throw new Error(parsed.error.message || String(parsed.error));
      return parsed.result;
    }

    // Verify live + count available tools
    let toolCount = 0;
    try {
      const tlResult = await _codeMcpCall('tools/list', {});
      toolCount = (tlResult?.tools || []).length;
    } catch (_) {}

    const execCfg = settingsData?.executor_config || {};
    const repoPaths = Array.isArray(execCfg.repo_paths) ? execCfg.repo_paths : [];

    let html = '';

    // Live indicator
    html += `<div style="display:flex;align-items:center;gap:8px;margin-bottom:14px">
      <span style="width:8px;height:8px;border-radius:50%;background:#22c55e;display:inline-block;flex-shrink:0"></span>
      <span style="font-size:11px;color:var(--text);font-weight:600">Code Intel Live</span>
      ${toolCount ? `<span style="font-size:10px;color:var(--muted)">${toolCount} tool${toolCount !== 1 ? 's' : ''}</span>` : ''}
    </div>`;

    // Index status per repo path
    html += `<div style="margin-bottom:16px"><div style="font-size:10px;color:var(--accent);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px;padding-bottom:4px;border-bottom:1px solid var(--border)">Index Status</div>`;
    if (repoPaths.length) {
      for (const rp of repoPaths) {
        const cwd = typeof rp === 'string' ? rp : (rp.cwd || '');
        const hostname = typeof rp === 'object' ? (rp.hostname || '') : '';
        if (!cwd) continue;
        try {
          const result = await _codeMcpCall('tools/call', {name: 'index_status', arguments: {project: _repoPathToProject(cwd)}});
          const text = (result?.content || []).map(c => c.text || '').join('').trim();
          html += `<div style="margin-bottom:10px">
            <div style="font-size:10px;color:var(--text);font-weight:600;margin-bottom:4px">${escapeHtml(cwd)}${hostname ? `<span style="color:var(--muted);font-weight:400"> · ${escapeHtml(hostname)}</span>` : ''}</div>
            <pre style="font-size:10px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:8px;white-space:pre-wrap;word-break:break-all;color:var(--text);margin:0;line-height:1.5">${escapeHtml(text || '(no status returned)')}</pre>
          </div>`;
        } catch (e) {
          html += `<div style="margin-bottom:10px">
            <div style="font-size:10px;color:var(--text);font-weight:600;margin-bottom:4px">${escapeHtml(cwd)}</div>
            <div style="font-size:10px;color:var(--error)">index_status failed: ${escapeHtml(String(e))}</div>
          </div>`;
        }
      }
    } else {
      html += `<div style="font-size:10px;color:var(--muted)">No repo paths configured. Add them in Settings → Executor Config to see index status.</div>`;
    }
    html += '</div>';

    // Architecture summary
    html += `<div><div style="font-size:10px;color:var(--accent);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px;padding-bottom:4px;border-bottom:1px solid var(--border)">Architecture Summary</div>`;
    try {
      const archPath = repoPaths.length ? (typeof repoPaths[0] === 'string' ? repoPaths[0] : (repoPaths[0].cwd || '')) : '';
      const archArgs = archPath ? {project: _repoPathToProject(archPath)} : {};
      const archResult = await _codeMcpCall('tools/call', {name: 'get_architecture', arguments: archArgs});
      const archText = (archResult?.content || []).map(c => c.text || '').join('').trim();
      html += `<pre style="font-size:10px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:10px;white-space:pre-wrap;word-break:break-all;color:var(--text);margin:0;line-height:1.5">${escapeHtml(archText || '(no architecture returned)')}</pre>`;
    } catch (e) {
      html += `<div style="font-size:10px;color:var(--error)">get_architecture failed: ${escapeHtml(String(e))}</div>`;
    }
    html += `<div style="margin-top:8px;display:flex;gap:6px">
      <button class="secondary" style="font-size:10px;padding:3px 10px" onclick="loadCodeIntelTab(${JSON.stringify(projectId)})">↺ Refresh</button>
    </div></div>`;

    body.innerHTML = html;

  } catch (e) {
    body.innerHTML = `<div style="color:var(--error)">Failed to load code intel: ${escapeHtml(String(e))}</div>`;
  }
}



// Turn a bare ntfy topic name ("my-alerts") into a full URL. Leaves real URLs,

// email addresses, and anything containing a slash untouched.

function normalizeNotifyTarget(raw) {

  const v = (raw || '').trim();

  if (!v) return '';

  if (v.includes('://') || v.includes('@') || v.includes('/')) return v;

  return `https://ntfy.sh/${v}`;

}



// Inverse of normalizeNotifyTarget for display: strip the implied ntfy.sh

// prefix so the field shows just the topic ("the prefix is added for you").

// Emails and non-ntfy webhooks pass through untouched.

function displayNotifyTarget(raw) {

  const v = (raw || '').trim();

  if (!v) return '';

  const lower = v.toLowerCase();

  for (const prefix of ['https://ntfy.sh/', 'http://ntfy.sh/', 'ntfy.sh/']) {

    if (lower.startsWith(prefix)) return v.slice(prefix.length).replace(/\/+$/, '');

  }

  return v;

}



// G1.7 — Suggest the project slug as the ntfy topic. The server will

// suffix with -2/-3/… if another project in this DB already uses it,

// so we no longer need a client-side random tail. Note: ntfy topics

// are publicly subscribable, so a guessable topic = anyone can listen.

// Users who want stronger privacy can paste a longer, custom value.

function osExecutorHintBanner(projectId) {
  // ITEM 2 — Settings hooks banner: tell the user which shell/Python their
  // executor will use, based on the browser's OS. Dismiss persists in localStorage.
  try { if (localStorage.getItem('meridian.hooks.osbanner.dismissed') === '1') return ''; } catch (e) {}
  const ua = String(navigator.userAgentData?.platform || navigator.platform || navigator.userAgent || '').toLowerCase();
  const isWin = ua.includes('win');
  const msg = isWin
    ? 'Windows detected — executors use <strong>PowerShell</strong>; run Python with <code>pixi run python</code>.'
    : 'Mac / Linux detected — executors use <strong>bash</strong>; run Python with <code>python3</code>.';
  return `<div data-os-hint style="display:flex;align-items:flex-start;gap:8px;background:var(--surface-1);border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:4px;padding:8px 10px;margin-bottom:10px;font-size:10px;color:var(--text);line-height:1.5">`
    + `<span style="flex:1">${msg}</span>`
    + `<button title="Dismiss" onclick="try{localStorage.setItem('meridian.hooks.osbanner.dismissed','1')}catch(e){}; var _b=this.closest('[data-os-hint]'); if(_b)_b.remove();" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:13px;line-height:1;padding:0 2px;flex-shrink:0">×</button>`
    + `</div>`;
}

function showFailoverBannerIfNeeded() {
  // ITEM 7 — failover banner: poll /failover-status once on load; if the server
  // reports failover mode, show a dismissible yellow bar (sessionStorage dismiss).
  try { if (sessionStorage.getItem('meridian.failover.dismissed') === '1') return; } catch (e) {}
  fetch('/failover-status').then(r => r.ok ? r.json() : null).then(data => {
    if (!data || !data.is_failover) return;
    if (document.getElementById('failover-banner')) return;
    const bar = document.createElement('div');
    bar.id = 'failover-banner';
    bar.style.cssText = 'position:sticky;top:0;z-index:9999;background:#fef3c7;color:#92400e;font-size:12px;font-weight:600;padding:8px 14px;display:flex;align-items:center;gap:10px;border-bottom:1px solid #f59e0b';
    const label = document.createElement('span');
    label.style.flex = '1';
    label.textContent = '⚠ Meridian is running in failover mode — some data may be read-only or delayed.';
    const btn = document.createElement('button');
    btn.textContent = '×';
    btn.title = 'Dismiss';
    btn.style.cssText = 'background:none;border:none;color:#92400e;font-size:16px;font-weight:700;cursor:pointer;line-height:1;padding:0 4px';
    btn.onclick = () => { try { sessionStorage.setItem('meridian.failover.dismissed', '1'); } catch (e) {} bar.remove(); };
    bar.appendChild(label);
    bar.appendChild(btn);
    document.body.insertBefore(bar, document.body.firstChild);
  }).catch(() => {});
}




// function _renderToolEntry -- moved to dashboard-mcp.js






async function loadHitlTab(projectId) {

  const body = document.getElementById(`hitl-body-${projectId}`);

  const statusFilter = document.getElementById(`hitl-status-filter-${projectId}`);

  const refreshBtn = document.getElementById(`hitl-refresh-${projectId}`);

  if (!body) return;



  const urgencyColor = { blocking: 'var(--red,#e05252)', high: 'var(--yellow,#d4a017)', normal: 'var(--muted)' };

  const statusBadge = { pending: '#f59e0b', answered: '#22c55e', dismissed: 'var(--muted)' };



  const render = async () => {

    body.innerHTML = `<div class="empty" style="color:var(--muted)">loading…</div>`;

    const status = (statusFilter && statusFilter.value) || 'pending';

    const qs = status === 'all' ? '?status=all' : `?status=${status}`;

    try {

      const rows = await api(`/projects/${projectId}/hitl${qs}&limit=50`);

      if (!rows || rows.length === 0) {

        body.innerHTML = `<div style="color:var(--muted);padding:12px;text-align:center;border:1px dashed var(--border);border-radius:4px">

          ${status === 'pending' ? 'No pending HITL requests — queue is clear ✓' : 'No items found'}

        </div>`;

        return;

      }

      const pending = rows.filter(r => r.status === 'pending');

      const resolved = rows.filter(r => r.status !== 'pending');

      const renderDiff = (diffText) => {

        const lines = String(diffText || '').split('\n').map(ln => {

          let color = 'var(--text)';

          if (ln.startsWith('+++') || ln.startsWith('---')) color = 'var(--muted)';

          else if (ln.startsWith('+')) color = '#22c55e';

          else if (ln.startsWith('-')) color = '#e05252';

          else if (ln.startsWith('@@')) color = '#38bdf8';

          return `<span style="color:${color};display:block;white-space:pre-wrap">${escapeHtml(ln)}</span>`;

        });

        return `<pre style="margin-top:8px;padding:8px;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;font-size:10px;font-family:var(--font-mono);max-height:260px;overflow:auto">${lines.join('')}</pre>`;

      };

      const renderCard = (r) => {

        const urg = r.urgency || 'normal';

        const st = r.status || 'pending';

        const dt = (r.created_at || '').slice(0, 16).replace('T', ' ');

        const isMd = r.kind === 'md_section_update';

        let pl = null;

        if (isMd && r.payload) { try { pl = JSON.parse(r.payload); } catch (e) { pl = null; } }

        const mdMeta = (isMd && pl) ? `<div style="margin-top:6px;font-size:10px;color:var(--accent)"><b>${escapeHtml(pl.file || '')}</b> § ${escapeHtml(pl.anchor || '')}</div>` : '';

        const diffHtml = (isMd && pl && pl.diff) ? renderDiff(pl.diff) : '';

        const answerHtml = r.answer ? `<div style="margin-top:8px;padding:6px 8px;background:var(--surface-1);border-radius:3px;border-left:3px solid #22c55e;color:var(--text);font-size:11px"><b>Answer:</b> ${escapeHtml(r.answer)}</div>` : '';

        const applyErr = r.apply_error ? `<div style="margin-top:6px;color:#e05252;font-size:10px"><b>Not applied:</b> ${escapeHtml(r.apply_error)}</div>` : '';

        const ctxHtml = r.context ? `<div style="margin-top:6px;color:var(--muted);font-size:11px;font-style:italic">${escapeHtml(r.context.slice(0, 200))}</div>` : '';

        // cd134cf1 — selectable answer options + a highlighted recommended default.
        let optPayload = null;
        try { optPayload = r.payload ? JSON.parse(r.payload) : null; } catch (e) { optPayload = null; }
        const hitlOpts = (optPayload && Array.isArray(optPayload.options)) ? optPayload.options : [];
        const hitlRec = (optPayload && typeof optPayload.recommended === 'string') ? optPayload.recommended : null;

        // Dual-channel indicator for pending blocking/high questions
        const dualChannelHint = (st === 'pending' && !isMd && (urg === 'blocking' || urg === 'high'))
          ? `<div style="margin-top:6px;display:flex;align-items:center;gap:6px;font-size:10px;color:var(--accent)">
               <span title="Also displayed inline in Claude Code — first answer (dashboard or chat) wins">📟 Dual-channel — also shown in Claude Code chat</span>
               <button class="secondary hitl-copy-id-btn" data-hitl-id="${escapeHtml(r.id)}" title="Copy HITL ID to clipboard" style="padding:1px 7px;font-size:9px">Copy ID</button>
             </div>`
          : '';

        let actionBtns = '';

        if (st === 'pending' && isMd) {

          actionBtns = `

          <div style="display:flex;gap:6px;margin-top:8px;align-items:center">

            <button class="primary hitl-approve-btn" data-hitl-id="${escapeHtml(r.id)}" style="padding:3px 12px;font-size:10px">Approve &amp; write</button>

            <button class="secondary hitl-reject-btn" data-hitl-id="${escapeHtml(r.id)}" style="padding:3px 10px;font-size:10px">Reject</button>

          </div>`;

        } else if (st === 'pending' && hitlOpts.length) {

          const optBtns = hitlOpts.map((o, i) => {
            const isRec = hitlRec !== null && String(o) === hitlRec;
            const recStyle = isRec
              ? 'border:1px solid var(--accent);background:var(--accent)1a;font-weight:600'
              : '';
            const recBadge = isRec
              ? ' <span style="font-size:8px;color:var(--accent);font-weight:700;text-transform:uppercase;letter-spacing:.04em">(recommended)</span>'
              : '';
            return `<button class="secondary hitl-opt-btn" data-hitl-id="${escapeHtml(r.id)}" data-answer="${escapeHtml(o)}"${isRec ? ' data-recommended="1" autofocus' : ''} style="padding:3px 10px;font-size:10px;text-align:left;${recStyle}">${i+1}. ${escapeHtml(o)}${recBadge}</button>`;
          }).join('\n');
          const kbHint = hitlOpts.length
            ? `<div style="font-size:9px;color:var(--muted);margin-top:2px">Press <b>1–${Math.min(9, hitlOpts.length)}</b> to choose${hitlRec !== null ? ', <b>Enter</b> for the recommended option' : ''}.</div>`
            : '';
          actionBtns = `

          <div class="hitl-opts" data-hitl-id="${escapeHtml(r.id)}" tabindex="0" style="display:flex;flex-direction:column;gap:4px;margin-top:8px;outline:none">

            ${optBtns}

            ${kbHint}

            <button class="secondary hitl-dismiss-btn" data-hitl-id="${escapeHtml(r.id)}" style="padding:3px 10px;font-size:10px;margin-top:2px;align-self:flex-start">Dismiss</button>

          </div>`;

        } else if (st === 'pending') {

          actionBtns = `

          <div style="display:flex;gap:6px;margin-top:8px;align-items:center">

            <input type="text" placeholder="Answer…" id="hitl-ans-${r.id}" style="flex:1;background:var(--surface-1);border:1px solid var(--border);border-radius:3px;color:var(--text);font-size:11px;font-family:var(--font-mono);padding:4px 8px;outline:none">

            <button class="primary hitl-answer-btn" data-hitl-id="${escapeHtml(r.id)}" style="padding:3px 10px;font-size:10px">Answer</button>

            <button class="secondary hitl-dismiss-btn" data-hitl-id="${escapeHtml(r.id)}" style="padding:3px 10px;font-size:10px">Dismiss</button>

          </div>`;

        }

        return `<div class="hitl-row" data-search="${escapeHtml((r.question || '') + ' ' + (r.status || '') + ' ' + (r.context || ''))}" style="background:var(--surface-2);border:1px solid var(--border);border-left:3px solid ${urgencyColor[urg] || 'var(--accent)'};border-radius:0 4px 4px 0;padding:10px 12px;margin-bottom:8px">

          <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:4px">

            <div style="font-weight:600;font-size:12px;color:var(--text)">${escapeHtml(r.question || '')}</div>

            <div style="display:flex;gap:4px;flex-shrink:0">

              ${r.answered_by === 'auto' ? `<span title="Auto-answered — no human reviewed this" style="font-size:9px;font-weight:600;background:var(--accent)22;color:var(--accent);padding:1px 6px;border-radius:3px">auto</span>` : ''}

              ${r.kind === 'correction' ? `<span title="Mid-run correction — non-blocking; the executor applies it at the next item boundary" style="font-size:9px;font-weight:600;background:#f59e0b22;color:#f59e0b;padding:1px 6px;border-radius:3px">✎ correction</span>` : ''}

              <span style="font-size:9px;font-weight:600;background:${urgencyColor[urg] || 'var(--accent)'}22;color:${urgencyColor[urg] || 'var(--accent)'};padding:1px 6px;border-radius:3px">${escapeHtml(urg)}</span>

              <span style="font-size:9px;font-weight:600;background:${statusBadge[st] || 'var(--muted)'}22;color:${statusBadge[st] || 'var(--muted)'};padding:1px 6px;border-radius:3px">${escapeHtml(st)}</span>

            </div>

          </div>

          <div style="color:var(--muted);font-size:10px">${escapeHtml(dt)}${r.assigned_to ? ' · @' + escapeHtml(r.assigned_to) : ''}</div>

          ${mdMeta}${ctxHtml}${dualChannelHint}${diffHtml}${answerHtml}${applyErr}${actionBtns}

        </div>`;

      };

      let html = pending.map(renderCard).join('');

      if (resolved.length > 0) {

        html += `<div style="color:var(--muted);font-size:10px;margin:12px 0 6px;border-top:1px solid var(--border);padding-top:8px">RESOLVED (${resolved.length})</div>`;

        html += resolved.map(renderCard).join('');

      }

      body.innerHTML = html;

      _wireTabSearch(`hitl-search-${projectId}`, `hitl-body-${projectId}`, '.hitl-row');

      body.querySelectorAll('.hitl-answer-btn').forEach(btn => {

        btn.onclick = async () => {

          const id = btn.dataset.hitlId;

          const inp = document.getElementById(`hitl-ans-${id}`);

          const answer = (inp && inp.value || '').trim();

          if (!answer) { toast('answer required', true); return; }

          try {

            await api(`/hitl/${id}`, { method: 'PATCH', body: JSON.stringify({ action: 'answer', answer }) });

            toast('answered ✓');

            render();

          } catch (e) { toast('failed: ' + e.message, true); }

        };

      });

      body.querySelectorAll('.hitl-opt-btn').forEach(btn => {

        btn.onclick = async () => {

          const id = btn.dataset.hitlId;

          const answer = btn.dataset.answer || '';

          try {

            await api(`/hitl/${id}`, { method: 'PATCH', body: JSON.stringify({ action: 'answer', answer }) });

            toast('answered ✓');

            render();

          } catch (e) { toast('failed: ' + e.message, true); }

        };

      });

      // cd134cf1 — keyboard: digits 1-9 pick an option, Enter picks recommended.
      body.querySelectorAll('.hitl-opts').forEach(box => {

        box.addEventListener('keydown', (e) => {

          const btns = Array.from(box.querySelectorAll('.hitl-opt-btn'));

          if (!btns.length) return;

          if (e.key === 'Enter') {

            const rec = box.querySelector('.hitl-opt-btn[data-recommended="1"]');

            const target = rec || (document.activeElement && document.activeElement.classList.contains('hitl-opt-btn') ? document.activeElement : null);

            if (target) { e.preventDefault(); target.click(); }

          } else if (/^[1-9]$/.test(e.key)) {

            const idx = parseInt(e.key, 10) - 1;

            if (idx < btns.length) { e.preventDefault(); btns[idx].click(); }

          }

        });

      });

      body.querySelectorAll('.hitl-dismiss-btn').forEach(btn => {

        btn.onclick = async () => {

          if (!confirm('Dismiss this HITL request?')) return;

          try {

            await api(`/hitl/${btn.dataset.hitlId}`, { method: 'PATCH', body: JSON.stringify({ action: 'dismiss' }) });

            toast('dismissed');

            render();

          } catch (e) { toast('failed: ' + e.message, true); }

        };

      });

      body.querySelectorAll('.hitl-approve-btn').forEach(btn => {

        btn.onclick = async () => {

          if (!confirm('Approve and write this markdown change? It will be committed at the next checkpoint.')) return;

          try {

            const res = await api(`/hitl/${btn.dataset.hitlId}`, { method: 'PATCH', body: JSON.stringify({ action: 'answer', answer: 'approved' }) });

            if (res && res.applied === false) toast('not applied: ' + (res.apply_error || 'see card'), true);

            else toast('approved ✓ — section written, staged for checkpoint');

            render();

          } catch (e) { toast('failed: ' + e.message, true); }

        };

      });

      body.querySelectorAll('.hitl-reject-btn').forEach(btn => {

        btn.onclick = async () => {

          if (!confirm('Reject this proposed change?')) return;

          try {

            await api(`/hitl/${btn.dataset.hitlId}`, { method: 'PATCH', body: JSON.stringify({ action: 'dismiss' }) });

            toast('rejected');

            render();

          } catch (e) { toast('failed: ' + e.message, true); }

        };

      });

      body.querySelectorAll('.hitl-copy-id-btn').forEach(btn => {

        btn.onclick = () => {

          const id = btn.dataset.hitlId;

          navigator.clipboard.writeText(id).then(() => toast('HITL ID copied ✓')).catch(() => {

            // fallback for browsers without clipboard API
            const tmp = document.createElement('textarea');
            tmp.value = id;
            document.body.appendChild(tmp);
            tmp.select();
            document.execCommand('copy');
            document.body.removeChild(tmp);
            toast('HITL ID copied ✓');

          });

        };

      });

    } catch (e) {

      body.innerHTML = `<div style="color:var(--muted)">failed to load HITL queue: ${escapeHtml(String(e))}</div>`;

    }

  };



  if (statusFilter) statusFilter.onchange = render;

  if (refreshBtn) refreshBtn.onclick = render;

  render();

}



async function loadTeamTab(projectId) {

  /** v2.4 — Team tab: per-human presence cards + standup digest +

   * activity summary. Pulls /team/summary?project_id=&days=N once per

   * load. Re-renders on day-range change or refresh button. */

  const body = document.getElementById(`team-body-${projectId}`);

  const daySel = document.getElementById(`team-days-${projectId}`);

  const refreshBtn = document.getElementById(`team-refresh-${projectId}`);

  if (!body) return;



  const render = async () => {

    body.innerHTML = `<div class="empty" style="color:var(--muted)">loading team summary…</div>`;

    const days = parseInt((daySel && daySel.value) || '14', 10);

    try {

      const data = await projectApi(projectId, `/team/summary?project_id=${encodeURIComponent(projectId)}&days=${days}`);

      const humans = data.humans || [];

      if (humans.length === 0) {

        body.innerHTML = `<div style="color:var(--muted);padding:10px;text-align:center;border:1px dashed var(--border);border-radius:4px">

          (no human-attributed activity in the last ${data.period_days}d — set <code>MERIDIAN_HUMAN_ID</code> or pass <code>human_id</code> to register_session)

        </div>`;

        return;

      }

      // Presence cards.

      const dotColor = { active: '#4ade80', recent: '#fbbf24', idle: '#6b7280' };

      const cards = humans.map(h => {

        const c = _colorForHuman(h.human_id);

        const dc = dotColor[h.presence] || dotColor.idle;

        const fw = (h.agent_framework && h.agent_framework !== 'claude_code')

          ? `<span style="background:var(--surface-2);color:var(--accent);font-size:9px;font-weight:600;padding:1px 5px;border-radius:3px;margin-left:4px">${escapeHtml(h.agent_framework)}</span>`

          : '';

        const tasksLine = `${h.tasks_done} done · ${h.tasks_pending} pending${h.tasks_failed ? ' · ' + h.tasks_failed + ' failed' : ''}`;

        const lastSeen = h.last_seen ? formatRelativeTime(h.last_seen) : 'never';

        const recent = (h.recent || []).slice(0, 3).map(t => {

          const s = (t.status || '?').toUpperCase();

          const desc = (t.description || '').slice(0, 90);

          return `<div style="color:var(--muted);font-size:10px;padding:1px 0">[${escapeHtml(s)}] ${escapeHtml(desc)}</div>`;

        }).join('');

        return `<div class="team-card" data-search="${escapeHtml((h.human_id || '') + ' ' + (h.active_session || '') + ' ' + (h.agent_framework || ''))}" style="background:var(--surface-2);border:1px solid var(--border);border-left:3px solid ${c};border-radius:4px;padding:10px 12px;margin-bottom:8px">

          <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">

            <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${dc}"></span>

            <span style="color:${c};font-weight:600">${escapeHtml(h.human_id)}</span>${fw}

            <span style="color:var(--muted);font-size:10px;margin-left:auto">${escapeHtml(lastSeen)}</span>

          </div>

          <div style="color:var(--text);font-size:11px;margin-bottom:2px">${escapeHtml(h.active_session || '(no active session)')}</div>

          <div style="color:var(--accent);font-size:10px;margin-bottom:4px">${escapeHtml(tasksLine)}</div>

          ${recent}

        </div>`;

      }).join('');



      // Goal/sprint/north-star change markers — from goal-history, sorted oldest→newest.

      // Compare adjacent entries to detect *which* field changed, so each marker

      // gets the correct colour (sprint=blue, north-star=amber, content=purple).

      let goalMarkers = [];

      try {

        const windowStart = Date.now() - days * 86400 * 1000;

        const history = await api(`/projects/${projectId}/goal-history`);

        // Sort oldest → newest so we can diff against the previous entry

        const sorted = [...(history || [])].sort((a, b) =>

          Date.parse((a.created_at||'').replace(' ','T')+'Z') -

          Date.parse((b.created_at||'').replace(' ','T')+'Z')

        );

        sorted.forEach((entry, i) => {

          const ts = entry.created_at;

          if (!ts) return;

          const t = Date.parse(ts.replace(' ','T') + 'Z');

          if (!isFinite(t) || t < windowStart) return;

          const prev = sorted[i - 1];

          if (!prev) {

            // Oldest visible entry — mark as goal content change

            goalMarkers.push({ ts, field: 'content_updated_at', label: '' });

            return;

          }

          // Push a marker for each field that changed

          if ((entry.sprint || '') !== (prev.sprint || '')) {

            goalMarkers.push({ ts, field: 'sprint_updated_at',

              label: (entry.sprint || '').split(/[\n—]/)[0].trim().slice(0, 22) });

          }

          if ((entry.north_star || '') !== (prev.north_star || '')) {

            goalMarkers.push({ ts, field: 'ns_updated_at', label: '' });

          }

          if ((entry.version_goal || '') !== (prev.version_goal || '')) {

            goalMarkers.push({ ts, field: 'content_updated_at', label: '' });

          }

        });

        // Dedupe by timestamp proximity (within 60 s) and same field

        goalMarkers = goalMarkers.filter((m, i) => !goalMarkers.slice(0, i).some(p =>

          p.field === m.field &&

          Math.abs(Date.parse((m.ts||'').replace(' ','T')+'Z') -

                   Date.parse((p.ts||'').replace(' ','T')+'Z')) < 60000

        ));

      } catch (_) { /* goal markers optional */ }



      // Activity timeline — one row per human, dots colored by task status.



      // Standup digest — one line per person with their most recent

      // descriptions concatenated.

      const standup = humans.map(h => {

        const c = _colorForHuman(h.human_id);

        const last = (h.recent || []).map(t => (t.description || '').slice(0, 60)).slice(0, 4).join('; ');

        return `<div style="padding:3px 0;border-left:2px solid ${c};padding-left:8px;font-size:11px">

          <span style="color:${c};font-weight:600">${escapeHtml(h.human_id)}</span> · ${h.tasks_done} done — <span style="color:var(--muted)">${escapeHtml(last) || '—'}</span>

        </div>`;

      }).join('');



      // Active pinned decisions for standup context.

      let decisionsHtml = '';

      try {

        const pinned = await api(`/projects/${projectId}/decisions-pinned`);

        if (pinned && pinned.length) {

          const rows = pinned.slice(0, 8).map(d => {

            const cat = d.category ? `<span style="font-size:9px;color:var(--muted);margin-left:4px">${escapeHtml(d.category)}</span>` : '';

            return `<div style="padding:4px 0;border-bottom:1px solid var(--border)">

              <div style="font-size:11px;font-weight:600;color:var(--text)">${escapeHtml(d.title)}${cat}</div>

              <div style="font-size:10px;color:var(--muted);margin-top:1px;white-space:pre-wrap">${escapeHtml((d.body || '').slice(0, 160))}</div>

            </div>`;

          }).join('');

          decisionsHtml = `<section style="margin-top:18px;padding-top:10px;border-top:1px solid var(--border)">

            <div style="color:var(--accent);font-weight:600;margin-bottom:8px">📌 Active decisions (${pinned.length})</div>

            ${rows}

          </section>`;

        }

      } catch (_) { /* decisions optional */ }



      body.innerHTML = `

        <section>

          <div style="color:var(--accent);font-weight:600;margin-bottom:8px">👥 Live (${data.active_count} active)</div>

          ${cards}

        </section>

        <section style="margin-top:18px;padding-top:10px;border-top:1px solid var(--border)">

          <div style="color:var(--accent);font-weight:600;margin-bottom:8px">🗞 Standup digest</div>

          ${standup}

        </section>

        ${decisionsHtml}`;

      _wireTabSearch(`team-search-${projectId}`, `team-body-${projectId}`, '.team-card');

    } catch (e) {

      body.innerHTML = renderProjectLoadError(projectId, 'Team summary unavailable', `/team/summary?project_id=${encodeURIComponent(projectId)}&days=${days}`, e);

      wireProjectLoadRetry(body, projectId);

    }

  };



  if (daySel) daySel.onchange = render;

  if (refreshBtn) refreshBtn.onclick = render;

  render();

}



async function updateLiveFeed(projectId) {

  /**v2.3 — live "Currently Running" section at top of Queue tab.

   * Shows active session name + started_at + last 5 task_log entries.

   * Collapses when no active session exists. Polls every 5s. */

  const el = document.getElementById(`live-session-${projectId}`);

  if (!el) return;

  const panel = getPanelState(projectId);

  try {

    const sessions = await api(`/projects/${projectId}/sessions?active_only=true`);

    const active = sessions && sessions.filter(s => s.status === 'active');

    if (!active || active.length === 0) {

      panel.liveSessionId = null;

      el.style.display = 'none';

      return;

    }

    const sess = active[0];

    panel.liveSessionId = sess.id;

    const tasks = await api(`/projects/${projectId}/sessions/${sess.id}/tasks/live?limit=5`).catch(() => []);

    const elapsed = sess.last_seen

      ? Math.round((Date.now() - new Date(sess.last_seen + 'Z').getTime()) / 60000)

      : null;

    const elapsedStr = elapsed !== null ? (elapsed < 2 ? 'just now' : `${elapsed}m ago`) : '';

    const taskRows = (tasks || []).map(t => {

      const icon = t.status === 'done' ? '✓' : t.status === 'failed' ? '✗' : t.status === 'in_progress' ? '▶' : '·';

      const color = t.status === 'done' ? 'var(--status-done)' : t.status === 'failed' ? 'var(--status-failed)' : t.status === 'in_progress' ? 'var(--accent)' : 'var(--muted)';

      const desc = (t.description || '').length > 80 ? t.description.slice(0, 80) + '…' : t.description;

      return `<div style="display:flex;gap:6px;align-items:baseline;padding:1px 0">

        <span style="color:${color};font-size:10px;flex-shrink:0">${icon}</span>

        <span style="color:var(--text);font-size:10px;word-break:break-word">${escapeHtml(desc || '')}</span>

      </div>`;

    }).join('');

    const extraCount = active.length - 1;

    const extraRows = active.slice(1).map(s => {

      const age = s.last_seen ? Math.round((Date.now() - new Date(s.last_seen + 'Z').getTime()) / 60000) : null;

      const ageStr = age !== null ? (age < 2 ? 'just now' : `${age}m ago`) : '';

      return `<div style="display:flex;align-items:center;gap:6px;padding:2px 0">
        <span style="font-size:9px;color:var(--accent)">●</span>
        <span style="font-size:10px;color:var(--text);font-family:var(--font-mono)">${escapeHtml(s.name || 'unnamed')}</span>
        ${s.human_id ? `<span style="font-size:9px;color:var(--muted)">${escapeHtml(s.human_id)}</span>` : ''}
        ${ageStr ? `<span style="font-size:9px;color:var(--muted);margin-left:auto">${ageStr}</span>` : ''}
      </div>`;

    }).join('');

    el.innerHTML = `

      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">

        <span style="font-size:9px;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);font-weight:600;background:var(--accent)1a;border:1px solid var(--accent)44;border-radius:3px;padding:1px 5px">● LIVE</span>

        <span style="font-size:11px;font-weight:600;color:var(--text);font-family:var(--font-mono)">${escapeHtml(sess.name || 'unnamed session')}</span>

        ${sess.human_id
          ? `<span style="font-size:10px;color:var(--muted)">${escapeHtml(sess.human_id)}</span>`
          : (sess.name ? `<span style="font-size:10px;color:var(--muted);font-style:italic">${escapeHtml(sess.name)}</span>` : '')}

        ${extraCount > 0 ? `<button class="secondary" id="live-feed-extra-toggle-${projectId}" style="font-size:9px;padding:1px 6px;margin-left:4px">+${extraCount} more ▸</button>` : ''}

        ${elapsedStr ? `<span style="font-size:10px;color:var(--muted);margin-left:auto">${elapsedStr}</span>` : ''}

      </div>

      ${extraCount > 0 ? `<div id="live-feed-extra-${projectId}" style="display:none;margin-bottom:6px;padding:4px 8px;background:var(--surface-2);border-radius:3px">${extraRows}</div>` : ''}

      <div style="font-family:var(--font-mono)">

        ${taskRows || '<div style="color:var(--muted);font-size:10px">no recent tasks</div>'}

      </div>`;

    if (extraCount > 0) {

      const toggleBtn = el.querySelector(`#live-feed-extra-toggle-${projectId}`);

      const extraEl = el.querySelector(`#live-feed-extra-${projectId}`);

      if (toggleBtn && extraEl) {

        toggleBtn.onclick = () => {

          const open = extraEl.style.display !== 'none';

          extraEl.style.display = open ? 'none' : 'block';

          toggleBtn.textContent = open ? `+${extraCount} more ▸` : `${extraCount} others ▾`;

        };

      }

    }

    el.style.display = 'block';

  } catch(e) {

    panel.liveSessionId = null;

    el.style.display = 'none';

  }

}



async function loadRecentSessions(projectId, sessions = null) {

  /** Recent Sessions list for non-live sessions with a copyable start_session(). */

  const el = document.getElementById(`recent-sessions-${projectId}`);

  if (!el) return;

  try {

    const panel = getPanelState(projectId);

    const allSessions = Array.isArray(sessions)

      ? sessions

      : await api(`/projects/${projectId}/sessions?active_only=false`);

    const recent = (allSessions || [])

      .filter(s => s.id !== panel.liveSessionId && !isLiveSession(s))

      .sort((a, b) => String(b.last_seen || b.created_at || '').localeCompare(String(a.last_seen || a.created_at || '')))

      .slice(0, 5);

    if (!recent.length) { el.style.display = 'none'; return; }

    el.innerHTML = `

      <div style="font-size:9px;font-weight:700;color:var(--muted);letter-spacing:.07em;text-transform:uppercase;margin-bottom:6px">Recent Sessions</div>

      ${recent.map(s => {

        const seenAt = s.last_seen || s.created_at || '';

        const dt = seenAt ? formatRelativeTime(seenAt) : '';

        const name = escapeHtml(s.name || s.id || 'session');

        const status = s.status === 'idle' ? 'idle' : s.status === 'closed' ? 'done' : (s.status || 'session');

        const _rawSummary = s.session_summary;
        const summaryText = typeof _rawSummary === 'string'
          ? _rawSummary
          : (_rawSummary && _rawSummary.summary ? _rawSummary.summary : '');
        const summaryPreview = summaryText ? escapeHtml(summaryText.slice(0, 90)) : '';
        const hasSummary = !!summaryText;

        const humanClause = s.human_id ? `, human_id="${String(s.human_id).replace(/"/g, '\\"')}"` : '';

        const cmd = `start_session(project_id="${projectId}", session_name="${String(s.name || 'resume-session').replace(/"/g, '\\"')}"${humanClause})`;

        const safeCmd = escapeHtml(cmd);

        return `<div class="recent-session-row" data-session-id="${escapeHtml(s.id)}" style="border:1px solid var(--border);border-radius:3px;padding:5px 8px;margin-bottom:4px;background:var(--surface-1);cursor:pointer">

          <div style="display:flex;justify-content:space-between;align-items:center;gap:6px">

            <span style="font-weight:600;font-size:10px;color:var(--text);font-family:var(--font-mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(s.name || '')}">${name}</span>

            <div style="display:flex;gap:4px;align-items:center;flex-shrink:0">

              <span style="font-size:9px;color:var(--muted)">${escapeHtml(status)}${dt ? ` · ${escapeHtml(dt)}` : ''}</span>

              <button class="secondary resume-session-btn" data-cmd="${safeCmd}"

                style="padding:1px 6px;font-size:9px" title="Copy start_session() to clipboard">Resume</button>
              <button class="secondary recent-session-timeline-btn" data-session-id="${escapeHtml(s.id)}"
                style="padding:1px 6px;font-size:9px" title="Open filtered timeline">Timeline</button>
              <span class="recent-session-chevron" style="font-size:9px;color:var(--muted);margin-left:2px">▼</span>

            </div>

          </div>

          ${summaryPreview ? `<div style="font-size:9px;color:var(--muted);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${escapeHtml(summaryText)}">${summaryPreview}</div>` : ''}
          <div class="recent-session-tasks" data-full-summary="${escapeHtml(summaryText)}" style="display:none;margin-top:6px;padding-top:5px;border-top:1px solid var(--border);font-size:10px;color:var(--muted)"></div>

        </div>`;

      }).join('')}`;

    el.querySelectorAll('.resume-session-btn').forEach(btn => {

      btn.onclick = (event) => {
        event.stopPropagation();

        const cmd = btn.dataset.cmd || '';

        navigator.clipboard.writeText(cmd).then(() => toast('Copied start_session() to clipboard')).catch(() => toast('copy failed', true));

      };

    });
    el.querySelectorAll('.recent-session-timeline-btn').forEach(btn => {
      btn.onclick = (event) => {
        event.stopPropagation();
        openTimelineForSession(projectId, btn.dataset.sessionId);
      };
    });
    el.querySelectorAll('.recent-session-row').forEach(row => {
      row.onclick = async (evt) => {
        if (evt.target.closest('.resume-session-btn, .recent-session-timeline-btn')) return;
        const target = row.querySelector('.recent-session-tasks');
        const chevron = row.querySelector('.recent-session-chevron');
        const sid = row.dataset.sessionId;
        if (!target || !sid) return;
        if (target.style.display !== 'none') {
          target.style.display = 'none';
          if (chevron) chevron.textContent = '▼';
          return;
        }
        if (!target.dataset.loaded) {
          target.textContent = 'loading...';
          try {
            const fullSummary = target.dataset.fullSummary || '';
            const taskRows = await api(`/projects/${projectId}/sessions/${sid}/tasks/live?limit=20`);
            const summaryHtml = fullSummary
              ? `<div style="color:var(--text-dim);margin-bottom:5px;white-space:pre-wrap;word-break:break-word">${escapeHtml(fullSummary)}</div>`
              : '';
            const tasksHtml = taskRows && taskRows.length
              ? taskRows.map(t => `<div style="padding:2px 0"><span style="color:var(--accent)">${escapeHtml((t.status || '').toUpperCase())}</span> ${escapeHtml((t.description || '').slice(0, 180))}</div>`).join('')
              : '<div>(no task log for this session)</div>';
            target.innerHTML = summaryHtml + tasksHtml;
            target.dataset.loaded = '1';
          } catch(e) {
            target.textContent = 'failed to load tasks';
          }
        }
        target.style.display = 'block';
        if (chevron) chevron.textContent = '▲';
      };
    });

    el.style.display = 'block';

  } catch (_) {

    el.style.display = 'none';

  }

}



async function loadMilestones(projectId) {

  const el = document.getElementById(`milestones-strip-${projectId}`);

  if (!el) return;

  try {

    const all = await api(`/projects/${projectId}/sprint-items`);

    // Show explicit milestone items + any done/failed/skipped/pushed sprint items

    const doneStatuses = new Set(['done', 'skipped', 'failed', 'pushed']);

    const milestones = (all || []).filter(i =>

      i.milestone_type === 'milestone' || doneStatuses.has(i.status)

    ).sort((a, b) => {

      // Done items first, then by completed_at desc

      const aTs = a.completed_at || a.added_at || '';

      const bTs = b.completed_at || b.added_at || '';

      return bTs.localeCompare(aTs);

    });

    if (!milestones.length) { el.style.display = 'none'; return; }

    const statusIcon = s => s === 'done' ? '✓' : s === 'failed' ? '✗' : s === 'pushed' ? '→' : s === 'skipped' ? '—' : s === 'in_progress' ? '▶' : '◦';

    const statusColor = s => s === 'done' ? 'var(--accent-green,#34d399)' : s === 'failed' ? '#e05' : s === 'pushed' ? 'var(--accent)' : s === 'in_progress' ? 'var(--accent)' : 'var(--muted)';

    el.innerHTML = `

      <div style="font-size:9px;font-weight:700;color:var(--muted);letter-spacing:.07em;text-transform:uppercase;margin-bottom:6px">Completed (${milestones.length})</div>

      <div style="display:flex;flex-wrap:wrap;gap:6px">

        ${milestones.slice(0, 20).map(m => {

          const date = (m.completed_at || m.added_at || '').slice(0, 10);

          const ic = statusIcon(m.status);

          const col = statusColor(m.status);

          return `<div style="display:flex;align-items:center;gap:4px;border:1px solid var(--border);border-radius:3px;padding:3px 7px;background:var(--surface-1);opacity:${m.status === 'done' ? 1 : 0.7}">

            <span style="color:${col};font-size:11px">${ic}</span>

            <span style="font-family:var(--font-mono);font-size:10px;color:var(--text)">${escapeHtml((m.title || '').slice(0, 40))}</span>

            ${date ? `<span style="font-size:9px;color:var(--muted)">${escapeHtml(date)}</span>` : ''}

          </div>`;

        }).join('')}

        ${milestones.length > 20 ? `<span style="font-size:10px;color:var(--muted);padding:3px 4px">+${milestones.length - 20} more</span>` : ''}

      </div>`;

    el.style.display = 'block';

  } catch (_) {

    el.style.display = 'none';

  }

}



async function loadRecentRuns(projectId) {

  const body = document.getElementById(`recent-runs-body-${projectId}`);

  const toggle = document.getElementById(`recent-runs-toggle-${projectId}`);

  const chevron = document.getElementById(`recent-runs-chevron-${projectId}`);

  if (!body) return;



  // Toggle collapse

  let collapsed = false;

  if (toggle) {

    toggle.onclick = () => {

      collapsed = !collapsed;

      body.style.display = collapsed ? 'none' : '';

      if (chevron) chevron.textContent = collapsed ? '▼' : '▲';

    };

  }



  try {

    const runs = await api(`/projects/${projectId}/runs?limit=10`);

    if (!runs || !runs.length) {

      body.innerHTML = '<div style="color:var(--muted);font-size:10px">No runs yet.</div>';

      return;

    }

    body.innerHTML = runs.map(run => {

      const sid = (run.session_id || '').slice(0, 8);
      const runLabel = run.session_name || sid;

      const ts = (run.started_at || '').slice(0, 16).replace('T', ' ');

      const dur = run.duration_s != null

        ? (run.duration_s < 60 ? `${run.duration_s}s` : `${Math.round(run.duration_s / 60)}m`)

        : (run.status === 'running' ? 'live' : '—');

      const cnt = run.task_count || 0;

      // A run stuck in "running" whose session is no longer active should display as "done"
      const displayRunStatus = (run.status === 'running' && run.session_status && run.session_status !== 'active')
        ? 'done' : run.status;

      const statusColor = displayRunStatus === 'running' ? 'var(--accent)' : displayRunStatus === 'failed' ? 'var(--danger,#e05)' : 'var(--muted)';

      const dots = displayRunStatus === 'running' ? ' ·' : '';

      return `<div class="run-row" data-run-id="${escapeHtml(run.id)}" data-project-id="${escapeHtml(projectId)}"

          style="border:1px solid var(--border);border-radius:3px;padding:5px 8px;margin-bottom:4px;background:var(--surface-1);cursor:pointer">

        <div style="display:flex;justify-content:space-between;align-items:center;gap:6px">

          <span style="font-size:10px;color:var(--text);font-family:var(--font-mono);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(run.session_id || '')}">${escapeHtml(runLabel)}${dots}</span>

          <span style="font-size:9px;color:var(--muted)">${cnt} tasks · ${dur} · ${ts}${run.session_name && sid ? ` · ${escapeHtml(sid)}` : ''}</span>

          <span style="font-size:9px;color:${statusColor}">${displayRunStatus}</span>

        </div>

        <div class="run-transcript-${escapeHtml(run.id)}" style="display:none;margin-top:6px;padding:6px 8px;background:var(--surface-2);border-radius:3px;font-size:10px;white-space:pre-wrap;color:var(--muted)"></div>

      </div>`;

    }).join('');



    body.querySelectorAll('.run-row').forEach(row => {

      row.addEventListener('click', async () => {

        const runId = row.dataset.runId;

        const pid = row.dataset.projectId;

        const transcript = row.querySelector(`.run-transcript-${runId}`);

        if (!transcript) return;

        if (transcript.style.display !== 'none') {

          transcript.style.display = 'none';

          return;

        }

        if (!transcript.textContent.trim()) {

          try {

            const full = await api(`/projects/${pid}/runs/${runId}`);

            transcript.textContent = full.transcript || '(empty)';

          } catch {

            transcript.textContent = 'failed to load';

          }

        }

        transcript.style.display = 'block';

      });

    });

  } catch (_) {

    body.innerHTML = '<div style="color:var(--muted);font-size:10px">Could not load runs.</div>';

  }

}



async function loadQueue(projectId) {

  /** Sprint queue panel sourced from sprint_items, not task_log history. */

  const body = document.getElementById(`queue-body-${projectId}`);

  if (!body) return;

  const panel = getPanelState(projectId);

  if (!panel.queueDoneLimit) panel.queueDoneLimit = QUEUE_DONE_PAGE_SIZE;

  body.innerHTML = '<div class="empty" style="color:var(--muted)">loading…</div>';

  try {

    const [sessions, sprintItems] = await Promise.all([

      projectApi(projectId, `/projects/${projectId}/sessions?active_only=false`).catch(() => []),

      projectApi(projectId, `/projects/${projectId}/sprint-items?with_counts=true`),

    ]);

    const liveSession = (sessions || []).find(s => isLiveSession(s));

    panel.liveSessionId = liveSession ? liveSession.id : null;

    const sprintPayload = sprintItems || [];
    panel.queueSprintItems = Array.isArray(sprintPayload) ? sprintPayload : (sprintPayload.items || []);
    panel.queueTotalDoneCount = Array.isArray(sprintPayload)
      ? panel.queueSprintItems.filter(it => it.status === 'done').length
      : (sprintPayload.total_done_count || 0);



    const renderCurrentQueue = () => {

      body.innerHTML = renderQueue(projectId, panel.queueSprintItems || []);

      wireQueueSectionToggles(projectId);

      const moreBtn = document.getElementById(`queue-done-more-${projectId}`);

      if (moreBtn) {

        moreBtn.onclick = () => {

          panel.queueDoneLimit = (panel.queueDoneLimit || QUEUE_DONE_PAGE_SIZE) + QUEUE_DONE_PAGE_SIZE;

          renderCurrentQueue();

        };

      }

    };



    renderCurrentQueue();

    loadRecentSessions(projectId, sessions || []);



    const refreshBtn = document.getElementById(`queue-refresh-${projectId}`);

    if (refreshBtn) refreshBtn.onclick = () => loadQueue(projectId);

    const reconcileBtn = document.getElementById(`queue-reconcile-${projectId}`);

    if (reconcileBtn) reconcileBtn.onclick = () => runReconcile(projectId);

    // Wire search input (debounced 300ms) — universal search across all tables

    const searchInput = document.getElementById(`task-search-${projectId}`);

    if (searchInput) {

      searchInput.placeholder = 'Search sprint items, notes, decisions…';

      if (!searchInput._wired) {

        searchInput._wired = true;

        let _searchTimer = null;

        searchInput.addEventListener('input', function() {

          clearTimeout(_searchTimer);

          const q = this.value.trim();

          _searchTimer = setTimeout(async () => {

            if (!q) { renderCurrentQueue(); return; }

            try {

              const results = await api(`/projects/${projectId}/search?q=${encodeURIComponent(q)}&limit=10`);

              body.innerHTML = renderSearchResults(q, results);

            } catch (e) { body.innerHTML = `<div class="empty">search failed: ${escapeHtml(e.message)}</div>`; }

          }, 300);

        });

      }

    }

  } catch (e) {

    body.innerHTML = renderProjectLoadError(projectId, 'Queue unavailable', `/projects/${projectId}/sprint-items`, e);

    wireProjectLoadRetry(body, projectId);

  }

}



async function runReconcile(projectId) {

  /** Fetch reconcile results and show inline in the Queue tab header area. */

  const container = document.getElementById(`reconcile-results-${projectId}`);

  const btn = document.getElementById(`queue-reconcile-${projectId}`);

  if (!container) return;

  if (btn) { btn.disabled = true; btn.textContent = 'checking…'; }

  container.style.display = 'block';

  container.innerHTML = '<span style="color:var(--muted)">Checking commits against sprint board…</span>';

  try {

    const data = await projectApi(projectId, `/projects/${projectId}/reconcile`);

    if (!data.matches || data.matches.length === 0) {

      container.innerHTML = `<span style="color:var(--muted)">✓ No drift detected (checked ${data.commit_count || 0} commits against ${data.pending_count || 0} pending items)</span>
        <button onclick="document.getElementById('reconcile-results-${projectId}').style.display='none'" style="margin-left:10px;background:none;border:none;color:var(--muted);cursor:pointer;font-size:10px">✕</button>`;

    } else {

      const n = data.matches.length;

      let html = `<div style="margin-bottom:6px;color:var(--warning,#f59e0b);font-weight:600">${n} item${n !== 1 ? 's' : ''} may already be shipped — verify before executing</div>`;

      data.matches.forEach(m => {

        const confidence = m.confidence === 'high' ? '🔴 high' : '🟡 medium';

        const commits = (m.matching_commits || []).slice(0, 2).map(c =>
          `<span style="color:var(--muted)">${escapeHtml(c.sha)} — ${escapeHtml(c.message)}</span>`
        ).join('<br>');

        html += `<div style="border:1px solid var(--border);border-radius:4px;padding:6px 8px;margin-bottom:6px;background:var(--surface-3,var(--surface-2))">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
            <div>
              <span style="color:var(--text)">${escapeHtml(m.title.slice(0, 80))}${m.title.length > 80 ? '…' : ''}</span>
              <span style="margin-left:6px;font-size:9px;opacity:0.7">${confidence}</span>
              <div style="margin-top:3px;font-size:9px">${commits}</div>
            </div>
            <div style="display:flex;gap:4px;flex-shrink:0">
              <button class="primary" style="padding:2px 7px;font-size:9px"
                onclick="reconcileMarkDone('${projectId}','${m.item_id}',this)">Mark done</button>
              <button class="secondary" style="padding:2px 7px;font-size:9px"
                onclick="this.closest('div[style]').remove()">Keep</button>
            </div>
          </div>
        </div>`;

      });

      html += `<button onclick="document.getElementById('reconcile-results-${projectId}').style.display='none'" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:10px;margin-top:2px">Dismiss</button>`;

      container.innerHTML = html;

    }

  } catch (e) {

    container.innerHTML = `<span style="color:var(--danger,#ef4444)">Reconcile failed: ${escapeHtml(e.message)}</span>
      <button onclick="document.getElementById('reconcile-results-${projectId}').style.display='none'" style="margin-left:10px;background:none;border:none;color:var(--muted);cursor:pointer;font-size:10px">✕</button>`;

  } finally {

    if (btn) { btn.disabled = false; btn.textContent = 'reconcile'; }

  }

}


async function reconcileMarkDone(projectId, itemId, btnEl) {

  /** Mark a sprint item done from the reconcile panel. */

  try {

    btnEl.disabled = true;

    btnEl.textContent = '…';

    await projectApi(projectId, `/projects/${projectId}/sprint-items/${itemId}/complete`, { method: 'POST', body: JSON.stringify({}) });

    const row = btnEl.closest('div[style]');

    if (row) {

      row.style.opacity = '0.4';

      row.innerHTML = `<span style="color:var(--muted)">✓ marked done</span>`;

    }

    loadQueue(projectId);

  } catch (e) {

    btnEl.disabled = false;

    btnEl.textContent = 'Mark done';

    toast(`Failed: ${e.message}`, true);

  }

}


function renderSearchResults(query, results) {

  /** Render universal search results grouped by type. */

  if (!results || results.total === 0) {

    return `<div class="empty" style="color:var(--muted);padding:12px 14px">No results for "${escapeHtml(query)}"</div>`;

  }

  const section = (label, items, renderFn) => {

    if (!items || !items.length) return '';

    return `<div style="padding:10px 14px 0">

      <div style="font-size:9px;font-weight:700;color:var(--muted);letter-spacing:.07em;text-transform:uppercase;margin-bottom:6px">${label}</div>

      ${items.map(renderFn).join('')}

    </div>`;

  };

  const taskRow = t => `<div style="border:1px solid var(--border);border-radius:3px;padding:5px 8px;margin-bottom:4px;background:var(--surface-2)">

    <div style="display:flex;justify-content:space-between;gap:6px">

      <span style="font-size:11px;color:var(--text);font-family:var(--font-mono);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(t.description || '')}">${escapeHtml((t.description || '').slice(0, 100))}</span>

      <span style="font-size:9px;color:var(--muted);flex-shrink:0">${escapeHtml(t.status || '')}</span>

    </div>

  </div>`;

  const noteRow = n => `<div style="border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:0 3px 3px 0;padding:5px 8px;margin-bottom:4px;background:var(--surface-2)" title="${escapeHtml(n.body || '')}">

    <div style="font-size:11px;font-weight:600;color:var(--accent)" title="${escapeHtml(n.title || '')}">${escapeHtml((n.title || '').slice(0, 80))}</div>

    <div style="font-size:10px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escapeHtml((n.body || '').slice(0, 80))}</div>

  </div>`;

  const decisionRow = d => `<div style="border:1px solid var(--border);border-left:3px solid var(--warning,#fa0);border-radius:0 3px 3px 0;padding:5px 8px;margin-bottom:4px;background:var(--surface-2)" title="${escapeHtml(d.body || '')}">

    <div style="font-size:11px;font-weight:600;color:var(--text)" title="${escapeHtml(d.title || '')}">${escapeHtml((d.title || '').slice(0, 80))}</div>

    <div style="font-size:10px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escapeHtml((d.body || '').slice(0, 80))}</div>

  </div>`;

  const sprintRow = s => `<div style="border:1px solid var(--border);border-radius:3px;padding:5px 8px;margin-bottom:4px;background:var(--surface-2)">

    <div style="display:flex;justify-content:space-between;gap:6px">

      <span style="font-size:11px;color:var(--text);font-family:var(--font-mono);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(s.title || '')}">${escapeHtml((s.title || '').slice(0, 100))}</span>

      <span style="font-size:9px;color:var(--muted);flex-shrink:0">${escapeHtml(s.version || '')} · ${escapeHtml(s.status || '')}</span>

    </div>

  </div>`;

  return `<div style="padding-bottom:10px">

    ${section('Tasks', results.tasks, taskRow)}

    ${section('Notes', results.notes, noteRow)}

    ${section('Decisions', results.decisions, decisionRow)}

    ${section('Sprint Items', results.sprint_items, sprintRow)}

  </div>`;

}



// function renderQueue -- moved to dashboard-sprint.js



function wireQueueSectionToggles(projectId) {

  const body = document.getElementById(`queue-body-${projectId}`);

  if (!body) return;

  const panel = getPanelState(projectId);

  const sectionState = panel.queueSectionState || (panel.queueSectionState = {

    backburner: true,

    pending: false,

    in_progress: false,

    done: true,

    failed: true,

  });



  body.querySelectorAll('.queue-section').forEach(section => {

    const header = section.querySelector('.queue-section-header');

    const sectionBody = section.querySelector('.queue-section-body');

    const key = section.dataset.section || header?.dataset.sectionKey || '';

    if (!header || !sectionBody || !key) return;



    const applyState = (collapsed, animate) => {

      section.dataset.collapsed = collapsed ? 'true' : 'false';

      header.setAttribute('aria-expanded', String(!collapsed));

      sectionBody.setAttribute('aria-hidden', String(collapsed));

      sectionState[key] = collapsed;



      if (sectionBody._queueTransitionEnd) {

        sectionBody.removeEventListener('transitionend', sectionBody._queueTransitionEnd);

        sectionBody._queueTransitionEnd = null;

      }



      if (!animate) {

        sectionBody.style.height = collapsed ? '0px' : 'auto';

        return;

      }



      if (collapsed) {

        const currentHeight = sectionBody.getBoundingClientRect().height;

        sectionBody.style.height = `${currentHeight}px`;

        sectionBody.offsetHeight;

        sectionBody.style.height = '0px';

      } else {

        sectionBody.style.height = '0px';

        sectionBody.offsetHeight;

        const targetHeight = sectionBody.scrollHeight;

        sectionBody.style.height = `${targetHeight}px`;

        const onEnd = (ev) => {

          if (ev.target !== sectionBody || ev.propertyName !== 'height') return;

          if (section.dataset.collapsed !== 'true') sectionBody.style.height = 'auto';

          sectionBody.removeEventListener('transitionend', onEnd);

          sectionBody._queueTransitionEnd = null;

        };

        sectionBody._queueTransitionEnd = onEnd;

        sectionBody.addEventListener('transitionend', onEnd);

      }

    };



    if (!header._queueWired) {

      header._queueWired = true;

      const toggle = () => applyState(section.dataset.collapsed !== 'true', true);

      header.onclick = toggle;

      header.onkeydown = (e) => {

        if (e.key === 'Enter' || e.key === ' ') {

          e.preventDefault();

          toggle();

        }

      };

    }



    applyState(section.dataset.collapsed === 'true', false);

  });

}



// Global queue action handler — called from inline onclick in renderQueue

window._queueAction = async function(taskId, action) {

  try {

    if (action === 'delete') {

      if (!confirm('Delete this task?')) return;

      await api(`/tasks/${taskId}`, { method: 'DELETE' });

    } else if (action === 'done') {

      await api(`/tasks/${taskId}`, { method: 'PATCH', body: JSON.stringify({ status: 'done' }) });

    } else if (action === 'backlog') {

      await api(`/tasks/${taskId}`, { method: 'PATCH', body: JSON.stringify({ status: 'backlog' }) });

    }

    // Reload all queue panels

    document.querySelectorAll('[id^="queue-body-"]').forEach(el => {

      const pid = el.id.replace('queue-body-', '');

      loadQueue(pid);

    });

  } catch(e) { toast('Action failed: ' + e.message, true); }

};















async function refreshTab(projectId) {

  await Promise.all([

    refreshGoal(projectId),

    refreshSessions(projectId),

    refreshTasks(projectId),

  ]);

}



async function refreshGoal(projectId) {

  const ta = document.getElementById(`goal-${projectId}`);

  const v = document.getElementById(`goal-version-${projectId}`);

  if (!ta) return;

  const goalPath = `/projects/${projectId}/goal`;

  try {

    const goal = await projectApi(projectId, goalPath);

    state.panels[projectId].goalRaw = goal.content;

    let text;

    if (typeof goal.content === 'string') {

      state.panels[projectId].goalIsJson = false;

      text = goal.content;

    } else {

      state.panels[projectId].goalIsJson = true;

      text = JSON.stringify(goal.content, null, 2);

    }

    // Split out title line + AUTO BLOCKS zone

    const AUTO_SPLIT = '--- AUTO BLOCKS BELOW ---';

    const splitIdx = text.indexOf(AUTO_SPLIT);

    const mainText = splitIdx !== -1 ? text.slice(0, splitIdx).trimEnd() : text;



    // Zone 1: title (line 0) — read-only

    // Zone 2: SHIPPED block — read-only

    // Zone 3: CURRENT FOCUS onwards — editable

    const allLines = mainText.split('\n');

    // Only use goal-title as blue header if the first line looks like a version label
    // (e.g. "v1.0.0", "v2.3 — auth sprint"). Otherwise everything goes in the textarea.
    const _firstLine = allLines[0] || '';
    const _isVersionLabel = /^v\d+\.\d+/.test(_firstLine.trim()) || _firstLine.trim().length === 0;
    const titleLine = _isVersionLabel ? _firstLine : '';
    const titleEl = document.getElementById(`goal-title-${projectId}`);
    if (titleEl) {
      titleEl.textContent = titleLine;
      // 06d57fef — when there's no version label, hide the title bar entirely.
      // Otherwise the empty div renders as a stray gray bar above the textarea
      // (its background/border/padding show even with no text). Round the
      // textarea's top corners when the bar is gone so it doesn't look clipped.
      const hasTitle = !!titleLine.trim();
      titleEl.style.display = hasTitle ? 'block' : 'none';
      const taEl = document.getElementById(`goal-${projectId}`);
      if (taEl) taEl.style.borderRadius = hasTitle ? '0 0 4px 4px' : '4px';
    }

    const body = (_isVersionLabel ? allLines.slice(1) : allLines).join('\n').replace(/^\n/, '');

    // Find CURRENT FOCUS as the start of editable zone

    const editStart = body.search(/^(CURRENT FOCUS|KEY FILES)/m);

    if (editStart > 0) {

      const shippedEl = document.getElementById(`goal-shipped-${projectId}`);

      if (shippedEl) {

        shippedEl.textContent = body.slice(0, editStart).trimEnd();

        shippedEl.style.display = shippedEl.textContent.trim() ? 'block' : 'none';

      }

      ta.value = body.slice(editStart);

    } else {

      const shippedEl = document.getElementById(`goal-shipped-${projectId}`);

      if (shippedEl) shippedEl.style.display = 'none';

      ta.value = body;

    }

    const shippedEl = document.getElementById(`goal-shipped-${projectId}`);

    if (shippedEl && !shippedEl.textContent.trim()) shippedEl.style.display = 'none';

    autosizeGoalField(ta);



    const autoBlocksEl = document.getElementById(`goal-autoblocks-${projectId}`);

    if (autoBlocksEl) {

      if (splitIdx !== -1) {

        const abWrapper = document.getElementById(`goal-autoblocks-wrapper-${projectId}`);

        if (abWrapper) abWrapper.style.display = 'block';

        autoBlocksEl.style.display = 'block';

        autoBlocksEl.textContent = text.slice(splitIdx + '--- AUTO BLOCKS BELOW ---'.length).trimStart();

      } else {

        const abWrapper2 = document.getElementById(`goal-autoblocks-wrapper-${projectId}`);

        if (abWrapper2) abWrapper2.style.display = 'none';

        autoBlocksEl.style.display = 'none';

      }

    }

    v.textContent = `v${goal.version}`;

    const vState = document.getElementById(`goal-state-${projectId}`);

    if (vState) vState.textContent = `v${goal.version}`;

    // v0.5.2 — north star and sprint textareas

    // v0.9 — guard against partial responses: only overwrite when the

    // server returned an explicit value (string or null). undefined

    // means "field absent from this response" — keep what the user has.

    const nsTA = document.getElementById(`goal-north-star-${projectId}`);

    const spTA = document.getElementById(`goal-sprint-${projectId}`);

    if (nsTA && 'north_star' in goal) {

      nsTA.value = goal.north_star || '';

      autosizeGoalField(nsTA);

    }

    if (spTA && 'sprint' in goal) {

      spTA.value = goal.sprint || '';

      if (_sprintSelectSyncers[projectId]) _sprintSelectSyncers[projectId](goal.sprint || '');

    }

    // v0.6.4 — store server values for dirty tracking; clear dirty state

    const p = state.panels[projectId];

    p._serverNorthStar = goal.north_star || '';

    p._serverSprint = goal.sprint || '';

    const nsLock = document.getElementById(`goal-ns-lock-${projectId}`);

    if (nsLock) nsLock.textContent = goal.north_star ? 'locked' : 'unlocked';

    p._lastSaved = text;

    if (nsTA) { nsTA.classList.remove('dirty'); }

    if (spTA) { spTA.classList.remove('dirty'); }

    ta.classList.remove('dirty');

    // v0.6.4 — last-modified timestamps

    const tsNs = document.getElementById(`goal-ns-ts-${projectId}`);

    const tsVg = document.getElementById(`goal-vg-ts-${projectId}`);

    const tsSp = document.getElementById(`goal-sp-ts-${projectId}`);

    const updAt = goal.updated_at ? formatRelativeTime(goal.updated_at) : '';

    if (tsNs) tsNs.textContent = updAt ? `· ${updAt}` : '';

    if (tsVg) tsVg.textContent = updAt ? `· ${updAt}` : '';

    if (tsSp) tsSp.textContent = updAt ? `· ${updAt}` : '';

    // v2.3 — decisions table subtab. goal.decisions is the append-only

    // blob: "[YYYY-MM-DD] text\n\n[YYYY-MM-DD] text\n\n..." (newest first).

    renderDecisionsTable(projectId, goal.decisions || '');

    // v2.4 — pinned decisions (editable constitution) above the log.

    loadPinnedDecisions(projectId);

  } catch (e) {

    ta.value = '';

    ta.placeholder = 'Goal state failed to load.';

    v.textContent = '(load failed)';

    const titleEl = document.getElementById(`goal-title-${projectId}`);

    if (titleEl) titleEl.textContent = 'Goal state unavailable';

  }

}



function parseDecisionsBlob(blob) {

  /** v2.3 — split the append-only decisions log into [{date, text}] rows.

   *

   * Format: "[YYYY-MM-DD] text...\n\n[YYYY-MM-DD] more text...\n\n"

   * Entries without a bracketed date keep `date: ''` and show their full

   * text (so legacy / malformed rows are still visible, not dropped).

   * Newest first — preserves the on-disk ordering.

   */

  if (!blob || typeof blob !== 'string') return [];

  const chunks = blob.split(/\n\s*\n/).map(c => c.trim()).filter(Boolean);

  return chunks.map(chunk => {

    const m = chunk.match(/^\[(\d{4}-\d{2}-\d{2})\]\s*(.*)$/s);

    if (m) return { date: m[1], text: m[2].trim() };

    return { date: '', text: chunk };

  });

}



const _DECISION_CATEGORY_COLORS = {

  STRATEGIC:     '#a78bfa',

  COMPETITIVE:   '#f87171',

  TECHNICAL:     '#6c8fff',

  TACTICAL:      '#fbbf24',

  BUSINESS:      '#4ade80',

  PRODUCT:       '#22d3ee',

  ARCHITECTURAL: '#fb923c',

};

// 366317e9 — decision priority badge colors + cycle order. Clicking the badge
// cycles urgent → normal → low → urgent. urgent decisions sort to the top.
const _DECISION_PRIORITY_COLORS = {

  urgent: '#f87171',

  normal: '#94a3b8',

  low:    '#64748b',

};

const _DECISION_PRIORITY_ORDER = ['urgent', 'normal', 'low'];



function renderConstitutionWarning(projectId) {

  const host = document.getElementById(`constitution-warning-${projectId}`);

  if (!host) return;

  const items = state.panels[projectId]?._pinnedDecisions || [];

  const count = items.length;

  if (!count) {

    host.innerHTML = '';

    return;

  }

  const limit = getConstitutionLimit(projectId);

  const warn = count >= limit;

  const archiveCount = Math.max(1, count - limit + 1);

  const border = warn ? '#f59e0b' : 'var(--border)';

  const fg = warn ? '#fbbf24' : 'var(--muted)';

  const tone = warn

    ? `Constitution has ${count} items — consider consolidating.`

    : `Constitution: ${count}/${limit} pinned decisions.`;

  host.innerHTML = `

    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;background:var(--surface-2);border:1px solid ${border};border-radius:4px;padding:8px 10px">

      <div style="font-size:10px;color:${fg}">${escapeHtml(tone)}</div>

      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">

        ${warn ? `<button class="secondary" id="constitution-archive-${projectId}" style="padding:2px 8px;font-size:10px">Archive oldest ${archiveCount}</button>` : ''}

        <button class="secondary" id="constitution-consolidate-${projectId}" style="padding:2px 8px;font-size:10px">Consolidate now</button>

      </div>

    </div>`;

  const consolidateBtn = document.getElementById(`constitution-consolidate-${projectId}`);

  if (consolidateBtn) consolidateBtn.onclick = () => consolidateDecisions(projectId);

  const archiveBtn = document.getElementById(`constitution-archive-${projectId}`);

  if (archiveBtn) {

    archiveBtn.onclick = async () => {

      if (!confirm(`Archive the oldest ${archiveCount} pinned decisions?`)) return;

      try {

        await api(`/projects/${projectId}/decisions-pinned/archive-oldest`, {

          method: 'POST',

          body: JSON.stringify({ count: archiveCount }),

        });

        toast(`Archived ${archiveCount} pinned decision${archiveCount === 1 ? '' : 's'}`);

        loadPinnedDecisions(projectId);

      } catch (e) { toast('archive failed: ' + e.message, true); }

    };

  }

}



// ---------------------------------------------------------------------------

// v2.4 — HITL (human-in-the-loop) queue panel

// ---------------------------------------------------------------------------



const _HITL_URGENCY_COLOR = {

  blocking: '#f87171',  // red — session paused, answer now

  high:     '#fbbf24',  // amber — should answer soon

  normal:   '#6c8fff',  // blue — nice-to-have

};



let _hitlPollTimer = null;



function _hitlBadgeClick() {

  /** Click handler for the hitl-count badge in the top bar — switches the

   * active project to the HITL vtab and opens the panel. */

  const pid = state.activeTab;

  if (pid) {

    const hitlBtn = document.querySelector(`#vtab-strip-${pid} [data-vtab="hitl"]`);

    if (hitlBtn) hitlBtn.click();

  }

  // Also open the inline panel if currently closed

  const panel = document.getElementById('hitl-panel');

  const toggleBtn = document.getElementById('hitl-toggle-btn');

  if (panel && panel.style.display === 'none') {

    panel.style.display = 'block';

    if (toggleBtn) toggleBtn.textContent = 'Close';

  }

}



function initHitlPanel() {

  /** v2.4 — boot the HITL polling loop + toggle wire-up. The bar at the

   * top of <main> auto-shows when there's at least one pending request

   * across any project. Idle state stays hidden so it doesn't add visual

   * noise when nothing's waiting. */

  const toggleBtn = document.getElementById('hitl-toggle-btn');

  if (toggleBtn) {

    toggleBtn.onclick = () => {

      const panel = document.getElementById('hitl-panel');

      if (!panel) return;

      const isOpen = panel.style.display !== 'none';

      panel.style.display = isOpen ? 'none' : 'block';

      toggleBtn.textContent = isOpen ? 'Open' : 'Close';

    };

  }

  refreshHitl();

  // 20591f72 — restore 60s fallback poll: WS push is the fast path but if the
  // project panel isn't open yet (no WS listener) the event is dropped silently.
  // 60s keeps the bar fresh without noticeable lag for the human.
  if (_hitlPollTimer) clearInterval(_hitlPollTimer);
  _hitlPollTimer = setInterval(refreshHitl, 60000);

}



function setVtabCountBadge(selector, count) {

  /** G1.2 — single source of truth for vtab/gtab count chip display.

   * Used by HITL, Notes, and Decisions badges. */

  document.querySelectorAll(selector).forEach(badge => {

    badge.textContent = String(count);

    badge.style.display = count > 0 ? 'inline-block' : 'none';

  });

}



async function refreshProjectCountBadges(projectId) {

  /** G1.2 — populate the muted count chips on the Notes vtab and the

   * Decisions goal-subtab for a single project. Called on tab open;

   * loadNotesTab / loadPinnedDecisions also refresh their own badge

   * from the data they fetch, so no extra round trip while tabs are

   * already active. Failures hide silently. */

  if (!projectId) return;

  const [notesRes, pinnedRes] = await Promise.allSettled([

    projectApi(projectId, `/projects/${projectId}/notes`),

    api(`/projects/${projectId}/decisions-pinned`),

  ]);

  if (notesRes.status === 'fulfilled') {

    const visible = (notesRes.value || []).filter(n => {

      const title = String(n.title || '').trim().toLowerCase();

      const tags = String(n.tags || '').split(',').map(t => t.trim().toLowerCase()).filter(Boolean);

      return !title.startsWith('checkpoint:') && !tags.includes('checkpoint');

    });

    setVtabCountBadge(`.notes-vtab-badge[data-pid="${projectId}"]`, visible.length);

  }

  if (pinnedRes.status === 'fulfilled') {

    setVtabCountBadge(`.decisions-gtab-badge[data-pid="${projectId}"]`, (pinnedRes.value || []).length);

  }

}



async function refreshHitl() {

  /** v2.4 — fetch pending HITL requests across all projects + repaint. */

  const bar = document.getElementById('hitl-bar');

  const countEl = document.getElementById('hitl-count');

  const list = document.getElementById('hitl-list');

  if (!bar || !countEl || !list) return;

  try {

    const items = await api('/hitl?status=pending&limit=50');

    const n = items.length;

    countEl.textContent = String(n);

    // Sync per-project vtab badges — each badge shows ITS project's pending

    // count, not the global, so a project with zero pending never shows "2".

    const perProject = new Map();

    for (const r of items) {

      const pid = r && r.project_id;

      if (!pid) continue;

      perProject.set(pid, (perProject.get(pid) || 0) + 1);

    }

    document.querySelectorAll('.hitl-vtab-badge').forEach(badge => {

      const pid = badge.getAttribute('data-pid');

      setVtabCountBadge(`.hitl-vtab-badge[data-pid="${pid}"]`, perProject.get(pid) || 0);

    });

    if (n === 0) {

      bar.style.display = 'none';

      document.getElementById('hitl-panel').style.display = 'none';

      return;

    }

    bar.style.display = 'flex';

    list.innerHTML = items.map(r => {

      const color = _HITL_URGENCY_COLOR[r.urgency] || _HITL_URGENCY_COLOR.normal;

      const ts = formatRelativeTime(r.created_at);

      const ctx = r.context

        ? `<details style="margin-top:4px"><summary style="cursor:pointer;color:var(--muted);font-size:10px">context</summary><pre style="margin:6px 0 0;padding:6px 8px;background:var(--surface-1);border-radius:3px;font-size:11px;white-space:pre-wrap;word-break:break-word">${escapeHtml(r.context)}</pre></details>`

        : '';

      const assigned = r.assigned_to

        ? `<span style="color:var(--muted);font-size:10px">→ ${escapeHtml(r.assigned_to)}</span>`

        : '';

      return `<div data-hitl-id="${escapeHtml(r.id)}" style="border-left:3px solid ${color};background:var(--surface-1);padding:10px 12px;margin-bottom:8px;border-radius:0 4px 4px 0">

        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap">

          <div style="display:flex;align-items:center;gap:6px;min-width:0;flex:1">

            <span style="background:${color}22;color:${color};font-size:9px;font-weight:700;letter-spacing:.5px;padding:2px 6px;border-radius:3px">${escapeHtml((r.urgency || 'normal').toUpperCase())}</span>

            ${r.kind === 'correction' ? `<span title="Mid-run correction — non-blocking" style="background:#f59e0b22;color:#f59e0b;font-size:9px;font-weight:700;letter-spacing:.5px;padding:2px 6px;border-radius:3px">✎ CORRECTION</span>` : ''}

            ${assigned}

            <span style="color:var(--muted);font-size:10px">${escapeHtml(ts)}</span>

          </div>

          <div style="display:flex;gap:4px">

            <button class="secondary hitl-dismiss-btn" data-hitl-id="${escapeHtml(r.id)}" style="padding:2px 8px;font-size:10px">Dismiss</button>

          </div>

        </div>

        <div style="color:var(--text);white-space:pre-wrap;word-break:break-word;line-height:1.5;font-size:12px;margin-bottom:8px">${escapeHtml(r.question || '')}</div>

        ${ctx}

        <div style="display:flex;gap:6px;margin-top:8px">

          <input type="text" class="hitl-answer-input" data-hitl-id="${escapeHtml(r.id)}" placeholder="Type answer and hit Enter…" style="flex:1;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:6px 10px;color:var(--text);font-family:var(--font-mono);font-size:11px;outline:none">

          <button class="primary hitl-answer-btn" data-hitl-id="${escapeHtml(r.id)}" style="padding:5px 12px;font-size:11px">Answer</button>

        </div>

      </div>`;

    }).join('');

    list.querySelectorAll('.hitl-answer-btn').forEach(btn => {

      btn.onclick = () => _hitlAnswer(btn.dataset.hitlId);

    });

    list.querySelectorAll('.hitl-dismiss-btn').forEach(btn => {

      btn.onclick = () => _hitlDismiss(btn.dataset.hitlId);

    });

    list.querySelectorAll('.hitl-answer-input').forEach(inp => {

      inp.onkeydown = (ev) => {

        if (ev.key === 'Enter') _hitlAnswer(inp.dataset.hitlId);

      };

    });

  } catch (e) {

    // Silent fail on poll — don't toast every 30s when offline.

  }

}



async function _hitlAnswer(id) {

  const inp = document.querySelector(`.hitl-answer-input[data-hitl-id="${id}"]`);

  const answer = (inp && inp.value || '').trim();

  if (!answer) { toast('answer required', true); return; }

  try {

    await api(`/hitl/${id}`, {

      method: 'PATCH',

      body: JSON.stringify({ action: 'answer', answer }),

    });

    toast('HITL answered');

    refreshHitl();

  } catch (e) { toast('answer failed: ' + e.message, true); }

}



async function _hitlDismiss(id) {

  if (!confirm('Dismiss this HITL request without answering?')) return;

  try {

    await api(`/hitl/${id}`, {

      method: 'PATCH',

      body: JSON.stringify({ action: 'dismiss' }),

    });

    refreshHitl();

  } catch (e) { toast('dismiss failed: ' + e.message, true); }

}



async function loadPinnedDecisions(projectId, { showArchived = false } = {}) {

  /** v2.4 — fetch the active pinned decisions for this project and render

   * them as colored category cards. The decisions tab also lists the

   * append-only log below in a collapsible <details>; pinned shows above

   * because it's the current authoritative truth. */

  const host = document.getElementById(`pinned-decisions-${projectId}`);

  if (!host) return;

  try {

    await loadProjectSettings(projectId);

    const url = showArchived
      ? `/projects/${projectId}/decisions-pinned?include_superseded=true`
      : `/projects/${projectId}/decisions-pinned`;
    const allItems = await api(url);
    const items = showArchived
      ? (allItems || [])
      : (allItems || []).filter(d => d.status !== 'superseded');

    getPanelState(projectId)._pinnedDecisions = items || [];

    setVtabCountBadge(`.decisions-gtab-badge[data-pid="${projectId}"]`, (items || []).length);

    renderConstitutionWarning(projectId);

    if (!items || items.length === 0) {

      host.innerHTML = `<div style="color:var(--muted);padding:10px;text-align:center;border:1px dashed var(--border);border-radius:4px">(no pinned decisions yet — call <code>pin_decision</code> from MCP)</div>`;

      return;

    }

    host.innerHTML = items.map(d => {

      const cat = d.category || 'TECHNICAL';

      const color = _DECISION_CATEGORY_COLORS[cat] || _DECISION_CATEGORY_COLORS.TECHNICAL;

      const prio = _DECISION_PRIORITY_ORDER.includes(d.priority) ? d.priority : 'normal';

      const prioColor = _DECISION_PRIORITY_COLORS[prio] || _DECISION_PRIORITY_COLORS.normal;

      const editCount = Array.isArray(d.edit_log) ? d.edit_log.length : 0;

      const dateStr = (d.created_at || '').slice(0, 10);

      return `<div data-decision-card="${escapeHtml(d.id)}" style="background:var(--surface-2);border:1px solid var(--border);border-left:4px solid ${color};border-radius:4px;padding:10px 12px;margin-bottom:8px">

        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;gap:8px">

          <div style="display:flex;align-items:center;gap:8px;min-width:0;flex:1">

            <span class="decision-cat-tag" data-id="${escapeHtml(d.id)}" data-cat="${escapeHtml(cat)}" title="Click to change category" style="display:inline-block;background:${color}22;color:${color};font-size:9px;font-weight:700;letter-spacing:.5px;padding:2px 6px;border-radius:3px;flex-shrink:0;cursor:pointer">${escapeHtml(cat)} ▾</span>

            <span class="decision-prio-tag" data-id="${escapeHtml(d.id)}" data-prio="${escapeHtml(prio)}" data-project="${escapeHtml(projectId)}" title="Click to change priority (urgent → normal → low)" style="display:inline-block;background:${prioColor}22;color:${prioColor};font-size:9px;font-weight:700;letter-spacing:.5px;padding:2px 6px;border-radius:3px;flex-shrink:0;cursor:pointer">${prio === 'urgent' ? '⚡ ' : ''}${escapeHtml(prio.toUpperCase())} ▾</span>

            <span class="decision-title-view" data-id="${escapeHtml(d.id)}" title="Click to edit title" style="color:var(--accent);font-weight:600;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer">${escapeHtml(d.title || '')}</span>

          </div>

          <div style="display:flex;gap:6px;flex-shrink:0;align-items:center">

            ${editCount ? `<span class="decision-edit-count" data-id="${escapeHtml(d.id)}" title="${editCount} previous ${editCount === 1 ? 'revision' : 'revisions'} — body has been edited" style="color:var(--muted);font-size:9px;border:1px solid var(--border);border-radius:3px;padding:1px 5px;cursor:default">✎ ${editCount}</span>` : ''}

            <span style="color:var(--muted);font-size:10px">${escapeHtml(dateStr)}</span>

            <button class="secondary" data-supersede="${escapeHtml(d.id)}" style="padding:1px 6px;font-size:9px">Supersede</button>

            <button class="secondary guest-hidden" data-archive-decision="${escapeHtml(d.id)}" title="Archive this decision (soft-delete; visible via 'View archived')" style="padding:1px 6px;font-size:9px;color:var(--muted)">Archive</button>

          </div>

        </div>

        <div class="decision-body-view" data-id="${escapeHtml(d.id)}" title="Click to edit" style="color:var(--text);white-space:pre-wrap;word-break:break-word;line-height:1.5;font-size:12px;cursor:pointer">${escapeHtml(d.body || '')}</div>

        <div class="decision-edit-area" data-id="${escapeHtml(d.id)}" style="display:none;margin-top:6px">

          <input type="text" class="decision-edit-title" data-id="${escapeHtml(d.id)}"

            value="${escapeHtml(d.title || '')}"

            style="width:100%;padding:4px 6px;background:var(--surface-1);border:1px solid var(--accent);border-radius:3px;color:var(--text);font-size:12px;font-family:var(--font-mono);outline:none;margin-bottom:4px">

          <textarea class="decision-edit-body" data-id="${escapeHtml(d.id)}" rows="4"

            style="width:100%;padding:4px 6px;background:var(--surface-1);border:1px solid var(--accent);border-radius:3px;color:var(--text);font-size:12px;font-family:var(--font-mono);resize:vertical;outline:none">${escapeHtml(d.body || '')}</textarea>

          <div style="display:flex;gap:6px;justify-content:flex-end;margin-top:4px">

            <button class="secondary decision-edit-cancel" data-id="${escapeHtml(d.id)}" style="padding:2px 8px;font-size:10px">Cancel</button>

            <button class="primary decision-edit-save" data-id="${escapeHtml(d.id)}" data-project="${escapeHtml(projectId)}" style="padding:2px 8px;font-size:10px">Save</button>

          </div>

        </div>

      </div>`;

    }).join('') + `<div id="decisions-view-archived-${escapeHtml(projectId)}" style="margin-top:8px;font-size:10px"></div>`;



    const showEdit = (id) => {

      const card = host.querySelector(`[data-decision-card="${id}"]`);

      if (!card) return;

      card.querySelector('.decision-body-view').style.display = 'none';

      card.querySelector('.decision-title-view').style.display = 'none';

      card.querySelector('.decision-edit-area').style.display = 'block';

    };

    const hideEdit = (id) => {

      const card = host.querySelector(`[data-decision-card="${id}"]`);

      if (!card) return;

      card.querySelector('.decision-body-view').style.display = '';

      card.querySelector('.decision-title-view').style.display = '';

      card.querySelector('.decision-edit-area').style.display = 'none';

    };



    host.querySelectorAll('.decision-body-view, .decision-title-view').forEach(el => {

      el.onclick = () => showEdit(el.dataset.id);

    });

    // Category tag — inline dropdown to change category
    const _CATS = ['TECHNICAL','STRATEGIC','ARCHITECTURAL','PRODUCT','TACTICAL','BUSINESS','COMPETITIVE'];
    host.querySelectorAll('.decision-cat-tag').forEach(tag => {
      tag.onclick = (e) => {
        e.stopPropagation();
        const id = tag.dataset.id;
        const cur = tag.dataset.cat;
        // Remove any existing dropdown
        document.querySelectorAll('.decision-cat-dropdown').forEach(d => d.remove());
        const sel = document.createElement('select');
        sel.className = 'decision-cat-dropdown';
        sel.style.cssText = 'position:absolute;z-index:9999;background:#1a1a1a;color:#f0f0f0;font-size:10px;font-weight:700;border:1px solid var(--border);border-radius:4px;padding:3px 5px;cursor:pointer';
        _CATS.forEach(c => { const o = document.createElement('option'); o.value=c; o.textContent=c; if(c===cur) o.selected=true; sel.appendChild(o); });
        const rect = tag.getBoundingClientRect();
        sel.style.left = (rect.left + window.scrollX) + 'px';
        sel.style.top = (rect.bottom + window.scrollY) + 'px';
        sel.style.minWidth = Math.max(rect.width, 140) + 'px';
        document.body.appendChild(sel);
        sel.focus({ preventScroll: true });
        if (typeof sel.showPicker === 'function') {
          try { sel.showPicker(); }
          catch (_) { sel.click(); }
        } else {
          sel.click();
        }
        sel.onblur = () => sel.remove();
        sel.onchange = async () => {
          const newCat = sel.value;
          sel.remove();
          try {
            const pid = host.closest('[data-project-tab]')?.dataset.projectTab || host.dataset.projectId || '';
            await api(`/projects/${pid}/decisions-pinned/${id}`, { method: 'PATCH', body: JSON.stringify({ category: newCat }) });
            await loadPinnedDecisions(pid);
          } catch(err) { toast('category update failed: ' + err.message, true); }
        };
      };
    });

    host.querySelectorAll('.decision-edit-cancel').forEach(btn => {

      btn.onclick = () => hideEdit(btn.dataset.id);

    });

    host.querySelectorAll('.decision-edit-save').forEach(btn => {

      btn.onclick = async () => {

        const id = btn.dataset.id;

        const pid = btn.dataset.project;

        const card = host.querySelector(`[data-decision-card="${id}"]`);

        const newTitle = card.querySelector('.decision-edit-title').value.trim();

        const newBody = card.querySelector('.decision-edit-body').value.trim();

        if (!newTitle || !newBody) return toast('title and body required', true);

        try {

          await api(`/projects/${pid}/decisions-pinned/${id}`, {

            method: 'PATCH',

            body: JSON.stringify({ title: newTitle, body: newBody }),

          });

          toast('decision saved');

          loadPinnedDecisions(pid);

        } catch (e) { toast('save failed: ' + e.message, true); }

      };

    });

    host.querySelectorAll('[data-supersede]').forEach(btn => {

      btn.onclick = () => supersedePinnedDecision(projectId, btn.dataset.supersede);

    });

    // 366317e9 — click the priority badge to cycle urgent → normal → low.
    host.querySelectorAll('.decision-prio-tag').forEach(tag => {

      tag.onclick = async () => {

        const id = tag.dataset.id;

        const pid = tag.dataset.project;

        const cur = _DECISION_PRIORITY_ORDER.includes(tag.dataset.prio) ? tag.dataset.prio : 'normal';

        const next = _DECISION_PRIORITY_ORDER[(_DECISION_PRIORITY_ORDER.indexOf(cur) + 1) % _DECISION_PRIORITY_ORDER.length];

        try {

          await api(`/projects/${pid}/decisions-pinned/${id}`, {

            method: 'PATCH',

            body: JSON.stringify({ priority: next }),

          });

          toast(`priority → ${next}`);

          loadPinnedDecisions(pid);

        } catch (e) { toast('priority change failed: ' + e.message, true); }

      };

    });

    host.querySelectorAll('[data-archive-decision]').forEach(btn => {

      btn.onclick = async () => {

        const id = btn.dataset.archiveDecision;

        try {

          await api(`/projects/${projectId}/decisions-pinned/${id}`, {
            method: 'PATCH',
            body: JSON.stringify({ status: 'superseded' }),
          });

          toast('decision archived');

          loadPinnedDecisions(projectId);

        } catch (e) { toast('archive failed: ' + e.message, true); }

      };

    });

    // View archived toggle — wired into the placeholder div we added to host.innerHTML
    const toggleEl = document.getElementById(`decisions-view-archived-${projectId}`);
    if (toggleEl) {
      if (showArchived) {
        const archivedCount = (allItems || []).filter(d => d.status === 'superseded').length;
        toggleEl.innerHTML = `<button class="secondary" style="padding:2px 8px;font-size:10px" onclick="loadPinnedDecisions('${escapeHtml(projectId)}', {showArchived:false})">← Hide archived</button> <span style="color:var(--muted)">${archivedCount} archived</span>`;
      } else {
        api(`/projects/${projectId}/decisions-pinned?include_superseded=true`).then(all => {
          const n = (all || []).filter(d => d.status === 'superseded').length;
          const el2 = document.getElementById(`decisions-view-archived-${projectId}`);
          if (el2) el2.innerHTML = n > 0
            ? `<button class="secondary" style="padding:2px 8px;font-size:10px" onclick="loadPinnedDecisions('${escapeHtml(projectId)}', {showArchived:true})">View archived (${n}) ▸</button>`
            : '';
        }).catch(() => {});
      }
    }



    // Superseded section — collapsible <details> below active cards

    let supersededEl = document.getElementById(`superseded-decisions-${projectId}`);

    if (!supersededEl) {

      supersededEl = document.createElement('div');

      supersededEl.id = `superseded-decisions-${projectId}`;

      host.parentElement.insertBefore(supersededEl, host.nextSibling);

    }

    try {

      const all = await api(`/projects/${projectId}/decisions-pinned?include_superseded=true`);

      const superseded = (all || []).filter(d => d.status === 'superseded');

      if (superseded.length > 0) {

        supersededEl.innerHTML = `<details style="margin-top:8px;margin-bottom:6px">

          <summary style="cursor:pointer;color:var(--muted);font-size:10px;font-family:var(--font-mono);letter-spacing:.05em;user-select:none">

            Superseded (${superseded.length})

          </summary>

          <div style="margin-top:6px">

            ${superseded.map(d => {

              const cat = d.category || 'TECHNICAL';

              const color = _DECISION_CATEGORY_COLORS[cat] || _DECISION_CATEGORY_COLORS.TECHNICAL;

              const dateStr = (d.created_at || '').slice(0, 10);

              return `<div style="background:var(--surface-1);border:1px solid var(--border);border-left:4px solid ${color}55;border-radius:4px;padding:8px 12px;margin-bottom:6px;opacity:0.6">

                <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">

                  <span style="display:inline-block;background:${color}11;color:${color}88;font-size:9px;font-weight:700;padding:1px 5px;border-radius:3px">${escapeHtml(cat)}</span>

                  <span style="color:var(--muted);font-weight:600;font-size:11px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(d.title || '')}">${escapeHtml(d.title || '')}</span>

                  <span style="color:var(--muted);font-size:9px;flex-shrink:0">${escapeHtml(dateStr)}</span>

                  <span style="background:var(--surface-2);color:var(--muted);font-size:8px;font-weight:700;padding:1px 5px;border-radius:3px;letter-spacing:.04em;flex-shrink:0">SUPERSEDED</span>

                </div>

                <div style="color:var(--muted);font-size:11px;white-space:pre-wrap;word-break:break-word;line-height:1.5">${escapeHtml((d.body || '').slice(0, 200))}</div>

              </div>`;

            }).join('')}

          </div>

        </details>`;

      } else {

        supersededEl.innerHTML = '';

      }

    } catch (_) { /* non-fatal — superseded section is optional */ }

  } catch (e) {

    if (state.panels[projectId]) state.panels[projectId]._pinnedDecisions = [];

    renderConstitutionWarning(projectId);

    host.innerHTML = `<div style="color:var(--muted)">failed to load pinned decisions: ${escapeHtml(String(e))}</div>`;

  }

}



async function supersedePinnedDecision(projectId, decisionId) {

  /** v2.4 — supersede flow: prompt for new title + body, atomic call to

   * the supersede endpoint (creates new active row, marks old superseded

   * with back-link). */

  const newTitle = prompt('New decision title (replaces this one):');

  if (!newTitle) return;

  const newBody = prompt('New decision body:');

  if (!newBody) return;

  try {

    await api(`/projects/${projectId}/decisions-pinned/${decisionId}`, {

      method: 'PATCH',

      body: JSON.stringify({ new_title: newTitle, new_body: newBody }),

    });

    toast('decision superseded');

    loadPinnedDecisions(projectId);

  } catch (e) { toast('supersede failed: ' + e.message, true); }

}



async function addPinnedDecision(projectId) {

  /** v2.4 — quick-add flow from the [+ Pin] button. */

  const title = prompt('Decision title:');

  if (!title) return;

  const body = prompt('Decision body:');

  if (!body) return;

  const category = prompt('Category (STRATEGIC/COMPETITIVE/TECHNICAL/TACTICAL/BUSINESS/PRODUCT/ARCHITECTURAL):', 'TECHNICAL');

  if (!category) return;

  try {

    await api(`/projects/${projectId}/decisions-pinned`, {

      method: 'POST',

      body: JSON.stringify({ title, body, category: category.toUpperCase() }),

    });

    toast('decision pinned');

    loadPinnedDecisions(projectId);

  } catch (e) { toast('pin failed: ' + e.message, true); }

}



async function consolidateDecisions(projectId) {

  /** v2.8 — AI consolidation flow for pinned decisions.

   * Shows API key + model modal → calls /decisions/consolidate → shows preview

   * → on confirm calls /decisions-pinned/replace-all. */

  // Step 1: API key + model input modal

  const overlay = document.createElement('div');

  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.72);z-index:9998;display:flex;align-items:center;justify-content:center';

  overlay.innerHTML = `

    <div style="background:var(--surface-1);border:1px solid var(--border);border-radius:6px;padding:20px;width:480px;max-width:92vw;display:flex;flex-direction:column;gap:12px;box-shadow:0 8px 32px #0008">

      <div style="font-weight:600;font-size:13px;color:var(--accent)">✨ AI Decision Consolidation</div>

      <div style="font-size:11px;color:var(--muted)">Sends your pinned decisions to an LLM to deduplicate and merge. Preview before applying. API key is never stored.</div>

      <div>

        <div style="font-size:10px;color:var(--muted);margin-bottom:4px;font-family:var(--font-mono)">MODEL</div>

        <select id="_consolidate-model" style="width:100%;padding:6px 8px;background:var(--surface-2);border:1px solid var(--border);color:var(--text);border-radius:4px;font-family:var(--font-mono);font-size:11px">

          <option value="claude-haiku-4-5-20251001">Claude Haiku 4.5 (fast, cheap)</option>

          <option value="claude-sonnet-4-6">Claude Sonnet 4.6 (better)</option>

          <option value="gpt-4o-mini">GPT-4o mini</option>

          <option value="gpt-4o">GPT-4o</option>

          <option value="deepseek-chat">DeepSeek Chat</option>

        </select>

      </div>

      <div>

        <div style="font-size:10px;color:var(--muted);margin-bottom:4px;font-family:var(--font-mono)">API KEY</div>

        <input id="_consolidate-key" type="password" placeholder="sk-ant-... / sk-... / sk-..." style="width:100%;padding:6px 8px;background:var(--surface-2);border:1px solid var(--border);color:var(--text);border-radius:4px;font-family:var(--font-mono);font-size:11px;outline:none">

      </div>

      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:4px">

        <button class="secondary" id="_consolidate-cancel" style="padding:5px 14px;font-size:11px">Cancel</button>

        <button class="primary" id="_consolidate-run" style="padding:5px 14px;font-size:11px">Consolidate →</button>

      </div>

    </div>`;

  document.body.appendChild(overlay);

  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });

  document.getElementById('_consolidate-cancel').onclick = () => overlay.remove();



  document.getElementById('_consolidate-run').onclick = async () => {

    const apiKey = document.getElementById('_consolidate-key').value.trim();

    const model = document.getElementById('_consolidate-model').value;

    if (!apiKey) { toast('API key required', true); return; }

    const runBtn = document.getElementById('_consolidate-run');

    runBtn.textContent = 'Working…'; runBtn.disabled = true;

    try {

      const result = await api(`/projects/${projectId}/decisions/consolidate`, {

        method: 'POST',

        body: JSON.stringify({ api_key: apiKey, model }),

      });

      overlay.remove();

      // Step 2: Preview modal

      const consolidated = result.consolidated || [];

      const previewOverlay = document.createElement('div');

      previewOverlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.72);z-index:9999;display:flex;align-items:center;justify-content:center';

      const previewHtml = consolidated.map(d => {

        const cat = (d.category || 'TECHNICAL').toUpperCase();

        const color = _DECISION_CATEGORY_COLORS[cat] || _DECISION_CATEGORY_COLORS.TECHNICAL;

        return `<div style="background:var(--surface-2);border-left:4px solid ${color};border-radius:4px;padding:8px 10px;margin-bottom:8px">

          <div style="display:flex;gap:8px;align-items:center;margin-bottom:4px">

            <span style="background:${color}22;color:${color};font-size:9px;font-weight:700;padding:1px 5px;border-radius:3px">${escapeHtml(cat)}</span>

            <span style="color:var(--accent);font-weight:600;font-size:12px">${escapeHtml(d.title || '')}</span>

          </div>

          <div style="color:var(--text);font-size:11px;white-space:pre-wrap">${escapeHtml(d.body || '')}</div>

        </div>`;

      }).join('');

      previewOverlay.innerHTML = `

        <div style="background:var(--surface-1);border:1px solid var(--border);border-radius:6px;padding:20px;width:620px;max-width:92vw;max-height:80vh;display:flex;flex-direction:column;gap:12px;box-shadow:0 8px 32px #0008">

          <div style="font-weight:600;font-size:13px;color:var(--accent)">Preview — ${consolidated.length} decisions (was ${result.original_count})</div>

          <div style="flex:1;overflow-y:auto;font-family:var(--font-mono)">${previewHtml}</div>

          <div style="color:var(--muted);font-size:10px">This will supersede all ${result.original_count} existing decisions and create ${consolidated.length} new ones.</div>

          <div style="display:flex;gap:8px;justify-content:flex-end">

            <button class="secondary" id="_preview-cancel" style="padding:5px 14px;font-size:11px">Cancel</button>

            <button class="primary" id="_preview-apply" style="padding:5px 14px;font-size:11px">Apply →</button>

          </div>

        </div>`;

      document.body.appendChild(previewOverlay);

      previewOverlay.addEventListener('click', e => { if (e.target === previewOverlay) previewOverlay.remove(); });

      document.getElementById('_preview-cancel').onclick = () => previewOverlay.remove();

      document.getElementById('_preview-apply').onclick = async () => {

        const applyBtn = document.getElementById('_preview-apply');

        applyBtn.textContent = 'Applying…'; applyBtn.disabled = true;

        try {

          await api(`/projects/${projectId}/decisions-pinned/replace-all`, {

            method: 'POST',

            body: JSON.stringify({ decisions: consolidated }),

          });

          previewOverlay.remove();

          toast(`Consolidated: ${consolidated.length} decisions applied`);

          loadPinnedDecisions(projectId);

        } catch (e) { toast('Apply failed: ' + e.message, true); applyBtn.textContent = 'Apply →'; applyBtn.disabled = false; }

      };

    } catch (e) {

      runBtn.textContent = 'Consolidate →'; runBtn.disabled = false;

      toast('Consolidation failed: ' + e.message, true);

    }

  };

}



function renderDecisionsTable(projectId, blob) {

  /** v2.3 — render the decisions blob as a proper accent-bordered table.

   *

   * Read-only: there is no edit affordance here. Decisions are append-only

   * via the `set_decision` MCP tool. The accent border-left on each row

   * matches the visual language of recent-tasks and sprint cards.

   */

  const host = document.getElementById(`decisions-table-${projectId}`);

  if (!host) return;

  const rows = parseDecisionsBlob(blob);

  if (rows.length === 0) {

    host.innerHTML = `<div style="color:var(--muted);padding:10px;text-align:center;border:1px dashed var(--border);border-radius:4px">(no decisions logged yet — call <code>set_decision</code> from MCP to record one)</div>`;

    return;

  }

  const html = rows.map(row => {

    const date = row.date ? escapeHtml(row.date) : '—';

    // Text may include long single-line decisions; preserve whitespace

    // (set_decision lets users embed paragraphs) but allow wrap.

    return `<div style="display:grid;grid-template-columns:96px 1fr;gap:10px;padding:8px 10px;border-left:3px solid var(--accent);background:var(--surface-2);border-radius:0 4px 4px 0;margin-bottom:6px;align-items:start">

      <div style="color:var(--accent);font-weight:600;white-space:nowrap">${date}</div>

      <div style="color:var(--text);white-space:pre-wrap;word-break:break-word;line-height:1.5">${escapeHtml(row.text)}</div>

    </div>`;

  }).join('');

  host.innerHTML = html;

}



function wireGoalPreviewToggle(taEl, previewEl) {

  /**v1.4.1 — repurposed as generic edit/preview toggle for a single

   * textarea + preview div pair.  Used by the Files tab editor.

   * Old per-goal-field wiring removed — goal fields are structured data,

   * not markdown documents.  Called as wireGoalPreviewToggle(taEl, prevEl). */

  if (!taEl || !previewEl) return;

  const row = document.createElement('div');

  row.className = 'preview-toggle-row';

  row.innerHTML =

    `<button class="preview-btn active" data-mode="edit">edit</button>` +

    `<button class="preview-btn"        data-mode="preview">preview</button>`;

  taEl.parentNode.insertBefore(row, taEl);

  row.querySelectorAll('.preview-btn').forEach(btn => {

    btn.onclick = () => {

      const mode = btn.dataset.mode;

      row.querySelectorAll('.preview-btn').forEach(b => {

        b.classList.toggle('active', b.dataset.mode === mode);

      });

      if (mode === 'preview') {

        const md = taEl.value || '';

        const html = (typeof marked !== 'undefined')

          ? marked.parse(md)

          : escapeHtml(md);

        previewEl.innerHTML = html;

        taEl.style.display = 'none';

        previewEl.style.display = '';

      } else {

        previewEl.style.display = 'none';

        taEl.style.display = '';

      }

    };

  });

}



async function saveGoal(projectId) {

  const ta = document.getElementById(`goal-${projectId}`);

  if (!ta) return;

  // Reattach auto blocks before saving so they aren't lost

  const autoBlocksEl = document.getElementById(`goal-autoblocks-${projectId}`);

  const autoBlocksText = (autoBlocksEl && autoBlocksEl.style.display !== 'none')

    ? '\n--- AUTO BLOCKS BELOW ---\n' + autoBlocksEl.textContent : '';

  const titleEl = document.getElementById(`goal-title-${projectId}`);

  const titleLine = (titleEl && titleEl.textContent) ? titleEl.textContent + '\n' : '';

  const shippedEl2 = document.getElementById(`goal-shipped-${projectId}`);

  const shippedText = (shippedEl2 && shippedEl2.style.display !== 'none' && shippedEl2.textContent)

    ? '\n' + shippedEl2.textContent + '\n' : '';

  const raw = titleLine + shippedText + ta.value + autoBlocksText;

  if (raw === state.panels[projectId]._lastSaved) return;

  let content = raw;

  if (state.panels[projectId].goalIsJson) {

    try { content = JSON.parse(raw); } catch(e) { /* fall back to string */ }

  }

  try {

    await api(`/projects/${projectId}/goal`, { method: 'POST', body: JSON.stringify({ content }) });

    state.panels[projectId]._lastSaved = raw;

    toast('version goal saved');

    refreshGoal(projectId);

  } catch (e) {

    toast('save failed: ' + e.message, true);

  }

}



async function saveNorthStar(projectId) {

  const ta = document.getElementById(`goal-north-star-${projectId}`);

  if (!ta) return;

  const val = ta.value.trim();

  if (!val) return;

  const saved = state.panels[projectId]?._serverNorthStar || '';

  if (saved && val !== saved && !confirm('North star is intended to be stable. Save changes?')) {

    ta.value = saved;

    autosizeGoalField(ta);

    ta.classList.remove('dirty');

    return;

  }

  try {

    const humanInput = document.getElementById('new-project-human');

    const humanId = humanInput ? humanInput.value.trim() : '';

    await api(`/projects/${projectId}/goal/north-star`, {

      method: 'POST',

      body: JSON.stringify({ north_star: val, human_id: humanId || 'owner' }),

    });

    toast('north star saved');

    refreshGoal(projectId);

  } catch (e) {

    toast('save failed: ' + e.message, true);

  }

}



async function saveSprint(projectId) {

  const ta = document.getElementById(`goal-sprint-${projectId}`);
  const sel = document.getElementById(`goal-sprint-select-${projectId}`);

  if (!ta) return;

  const rawVal = (ta.style.display === 'none' && sel && sel.value && sel.value !== '__custom__')
    ? sel.value
    : ta.value;
  const val = rawVal.trim();

  if (!val) return;

  try {

    await api(`/projects/${projectId}/goal/sprint`, {

      method: 'POST',

      body: JSON.stringify({ sprint: val }),

    });

    toast('sprint saved');

    refreshGoal(projectId);

  } catch (e) {

    toast('save failed: ' + e.message, true);

  }

}



function _sessionPresenceDot(last_seen) {

  if (!last_seen) return '⚫';

  const mins = (Date.now() - new Date(last_seen.replace(' ', 'T') + 'Z')) / 60000;

  if (mins < 6) return '🟢';

  if (mins < 30) return '🟡';

  return '⚫';

}



async function refreshSessions(projectId) {

  const root = document.getElementById(`sessions-${projectId}`);

  if (!root) return;

  const sessionsPath = `/projects/${projectId}/sessions`;

  try {

    const sessions = await projectApi(projectId, sessionsPath);

    populateSessionDropdown(projectId, sessions);

    if (!sessions.length) {

      root.innerHTML = '<div class="session-row meta">(no active sessions)</div>';

      return;

    }

    // Group by human_id, ungrouped into own bucket

    const groups = {};

    const order = [];

    for (const s of sessions) {

      const h = s.human_id || '\x00unknown';

      if (!groups[h]) { groups[h] = []; order.push(h); }

      groups[h].push(s);

    }

    for (const g of Object.values(groups)) {

      g.sort((a, b) => (b.last_seen || '').localeCompare(a.last_seen || ''));

    }

    const rows = order.map(h => {

      const humanSessions = groups[h];

      const label = h === '\x00unknown'
        ? (humanSessions.length === 1 ? humanSessions[0].name : 'unknown')
        : h;

      const topDot = _sessionPresenceDot(humanSessions[0]?.last_seen);

      const header = `<div class="session-row" style="font-weight:600;padding-top:4px">` +

        `<span class="name">${topDot} ${escapeHtml(label)}</span>` +

        `<span class="meta">${humanSessions.length} session${humanSessions.length > 1 ? 's' : ''}</span>` +

        `</div>`;

      const children = humanSessions.map(s => {

        let ageMs = 0;

        try {

          const ts = s.last_seen ? s.last_seen.replace(' ', 'T') + 'Z' : '';

          if (ts) ageMs = Date.now() - new Date(ts).getTime();

        } catch(e) {}

        const ageH = ageMs / 3_600_000;

        const opacity = ageH < 1 ? 1 : ageH < 24 ? 0.7 : 0.4;

        const clientBadge = s.client_type

          ? `<span style="font-size:9px;color:var(--muted);margin-left:4px">${escapeHtml(s.client_type)}</span>`

          : '';

        return `<div class="session-row" style="opacity:${opacity};padding-left:18px;font-size:11px">` +

          `<span class="name">${escapeHtml(s.name)}${clientBadge}</span>` +

          `<span class="meta">${escapeHtml(s.status)} · ${escapeHtml(formatRelativeTime(s.last_seen))}</span>` +

          `</div>`;

      }).join('');

      return header + children;

    }).join('');

    root.innerHTML = rows;

  } catch(e) {

    root.innerHTML = renderProjectLoadError(projectId, 'Sessions unavailable', sessionsPath, e);

    wireProjectLoadRetry(root, projectId);

  }

}



async function refreshTasks(projectId) {

  const tasksPath = `/projects/${projectId}/tasks?limit=100`;

  try {

    const tasks = await projectApi(projectId, tasksPath);

    state.panels[projectId].taskCache = tasks;

    state.panels[projectId].taskOffset = tasks.length;

    renderTasks(projectId);

  } catch(e) {

    const root = document.getElementById(`tasks-${projectId}`);

    const hitlRoot = document.getElementById(`hitl-queue-${projectId}`);

    const banner = document.getElementById(`hitl-banner-${projectId}`);

    if (banner) banner.style.display = 'none';

    if (hitlRoot) hitlRoot.innerHTML = '';

    if (root) {

      root.innerHTML = renderProjectLoadError(projectId, 'Task log unavailable', tasksPath, e);

      wireProjectLoadRetry(root, projectId);

    }

  }

}



function renderTasks(projectId) {

  const tasks = state.panels[projectId].taskCache || [];

  const root = document.getElementById(`tasks-${projectId}`);

  const hitlRoot = document.getElementById(`hitl-queue-${projectId}`);

  const banner = document.getElementById(`hitl-banner-${projectId}`);

  if (!root || !hitlRoot) return;

  const hitl = tasks.filter(t => t.status === 'pending-hitl');

  banner.style.display = hitl.length ? 'block' : 'none';

  hitlRoot.innerHTML = hitl.map(t => renderHitlRow(projectId, t)).join('');

  hitl.forEach(t => wireHitlRow(projectId, t));

  root.innerHTML = tasks.map(t => renderTaskRow(t)).join('');

  _wireTabSearch(`devlog-search-${projectId}`, `tasks-${projectId}`, '.task');

  // Pagination: show "Load more" button if we got a full page

  const existingBtn = document.getElementById(`devlog-load-more-${projectId}`);

  if (existingBtn) existingBtn.remove();

  if (tasks.length === 100) {

    const btn = document.createElement('button');

    btn.id = `devlog-load-more-${projectId}`;

    btn.className = 'secondary';

    btn.style = 'width:100%;margin-top:8px;padding:5px;font-size:11px;font-family:var(--font-mono)';

    btn.textContent = 'Load 100 more ↓';

    btn.onclick = () => _loadMoreTasks(projectId, btn);

    root.parentElement.appendChild(btn);

  }

}



async function _loadMoreTasks(projectId, btn) {

  const p = state.panels[projectId];

  const offset = p.taskOffset || 0;

  btn.disabled = true;

  btn.textContent = 'loading…';

  try {

    const more = await api(`/projects/${projectId}/tasks?limit=100&offset=${offset}`);

    p.taskCache = [...(p.taskCache || []), ...more];

    p.taskOffset = offset + more.length;

    const root = document.getElementById(`tasks-${projectId}`);

    if (root) root.innerHTML += more.map(t => renderTaskRow(t)).join('');

    if (more.length < 100) {

      btn.remove();

    } else {

      btn.disabled = false;

      btn.textContent = 'Load 100 more ↓';

    }

  } catch(e) {

    btn.disabled = false;

    btn.textContent = 'Load 100 more ↓ (retry)';

  }

}



function renderTaskRow(t) {

  const claimBadge = t.claimed_by

    ? `<span class="claim-badge" title="claimed at ${escapeHtml(t.claimed_at || '')}">\U0001f512 ${escapeHtml((t.claimed_by_human_id || t.claimed_by_session_name || t.claimed_by || '').slice(0, 16))}</span>`

    : '';

  const deleteBtn = `<button class="guest-hidden" title="Delete from task log (permanent)" onclick="deleteTaskRow(event,'${t.id}','${t.status}')" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:13px;padding:0 4px;flex-shrink:0;line-height:1" onmouseenter="this.style.color='var(--status-failed)'" onmouseleave="this.style.color='var(--muted)'">\u00d7</button>`;

  return `

    <div class="task ${t.status}" id="task-row-${t.id}" data-search="${escapeHtml((t.description || '') + ' ' + (t.session_name || '') + ' ' + (t.claimed_by_session_name || '') + ' ' + (t.status || ''))}" style="display:flex;align-items:flex-start;gap:4px">

      <span class="status-badge">${t.status}</span>

      <div style="flex:1;min-width:0">

        <div class="desc">${escapeHtml(t.description)}</div>

        <div class="meta">${escapeHtml(t.created_at)} ${claimBadge}</div>

      </div>

      ${deleteBtn}

    </div>`;

}



async function deleteTaskRow(e, taskId, status) {

  e.stopPropagation();

  const warn = (status === 'pending' || status === 'in_progress')

    ? 'This task is ' + status + '. Deleting it is permanent. Continue?'

    : 'Permanently delete this task from the log?';

  if (!confirm(warn)) return;

  try {

    await api('/tasks/' + taskId, { method: 'DELETE' });

    const row = document.getElementById('task-row-' + taskId);

    if (row) row.remove();

  } catch(e2) { console.error('Delete failed:', e2); }

}



function renderHitlRow(projectId, t) {

  const isExecute = t.description.startsWith('[EXECUTE]');

  const label = isExecute ? 'EXECUTE REQUEST' : 'QUESTION';

  const body = t.description.replace(/^\[(ASK|EXECUTE)\]:?\s*/, '');

  if (isExecute) {

    return `

      <div class="hitl-row" data-task="${t.id}">

        <div class="prompt"><strong>${label}</strong> · ${escapeHtml(body)}</div>

        <div class="controls">

          <button class="execute" data-action="confirm" data-task="${t.id}">EXECUTE</button>

          <button class="danger"  data-action="reject"  data-task="${t.id}">REJECT</button>

        </div>

      </div>`;

  }

  return `

    <div class="hitl-row" data-task="${t.id}">

      <div class="prompt"><strong>${label}</strong> · ${escapeHtml(body)}</div>

      <div class="controls">

        <input type="text" placeholder="your reply" data-input="${t.id}">

        <button class="primary" data-action="reply" data-task="${t.id}">reply</button>

      </div>

    </div>`;

}



function wireHitlRow(projectId, t) {

  const row = document.querySelector(`#hitl-queue-${projectId} [data-task="${t.id}"]`);

  if (!row) return;

  row.querySelectorAll('button[data-action]').forEach(btn => {

    btn.onclick = () => {

      const action = btn.dataset.action;

      if (action === 'reply') {

        const inp = row.querySelector(`input[data-input="${t.id}"]`);

        const text = (inp && inp.value || '').trim();

        if (!text) { toast('enter a reply first', true); return; }

        hitlReply(projectId, t.id, text);

      } else if (action === 'confirm') {

        hitlExecute(projectId, t.id, true);

      } else if (action === 'reject') {

        hitlExecute(projectId, t.id, false);

      }

    };

  });

}



async function appendToGoal(projectId, line) {

  // Pull latest goal, append, push back. String-only for HITL markers.

  let current = '';

  try {

    const goal = await api(`/projects/${projectId}/goal`);

    current = typeof goal.content === 'string' ? goal.content : JSON.stringify(goal.content, null, 2);

  } catch(e) { /* unset goal is fine */ }

  const next = current ? current.trimEnd() + '\n' + line : line;

  await api(`/projects/${projectId}/goal`, { method: 'POST', body: JSON.stringify({ content: next }) });

}



async function hitlReply(projectId, taskId, text) {

  try {

    await appendToGoal(projectId, `[HITL-REPLY:${taskId}:] ${text}`);

    await api(`/tasks/${taskId}`, { method: 'PATCH', body: JSON.stringify({ status: 'done', description: `[ANSWERED] ${text}` }) });

    toast('reply sent');

  } catch(e) { toast('reply failed: ' + e.message, true); }

}



async function hitlExecute(projectId, taskId, confirmed) {

  try {

    if (confirmed) {

      await appendToGoal(projectId, `[EXECUTE-CONFIRMED:${taskId}]`);

      await api(`/tasks/${taskId}`, { method: 'PATCH', body: JSON.stringify({ status: 'done' }) });

      toast('execute confirmed');

    } else {

      await appendToGoal(projectId, `[EXECUTE-REJECTED:${taskId}]`);

      await api(`/tasks/${taskId}`, { method: 'PATCH', body: JSON.stringify({ status: 'failed' }) });

      toast('execute rejected');

    }

  } catch(e) { toast('execute failed: ' + e.message, true); }

}



function connectWs(projectId) {

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';

  const ws = new WebSocket(`${proto}//${location.host}/ws/${projectId}`);

  const dot = document.getElementById(`ws-${projectId}`);

  ws.onopen = () => { dot && dot.classList.add('connected'); };

  ws.onclose = () => {

    dot && dot.classList.remove('connected');

    // Reconnect after a beat if the tab is still open.

    setTimeout(() => {

      if (state.panels[projectId]) connectWs(projectId);

    }, 1500);

  };

  ws.onerror = () => { dot && dot.classList.remove('connected'); };

  ws.onmessage = (ev) => {

    try {

      const event = JSON.parse(ev.data);

      handleWsEvent(projectId, event);

    } catch(e){}

  };

  state.panels[projectId].ws = ws;

}



function handleWsEvent(projectId, event) {

  if (event.type === 'update_available') {

    if (isDemoMode()) {

      hideDemoAdminControls();

      return;

    }

    const banner = document.getElementById('update-banner');

    if (banner) banner.style.display = 'block';

    return;

  }

  // v1.9.x — sync tab label and project state on rename from any session.

  if (event.type === 'project_renamed') {

    const tab = state.tabs.find(t => t.id === event.project_id);

    if (tab) {

      tab.project = { ...tab.project, name: event.name };

      const hdr = document.querySelector(`#drawer-status-${event.project_id} .drawer-header span:first-child`);

      if (hdr) hdr.textContent = 'STATUS · ' + event.name;

      renderTabs();

    }

    const proj = state.projects.find(p => p.id === event.project_id);

    if (proj) { proj.name = event.name; loadProjects(); }

    return;

  }

  // v2.6 — sprint item / goal / session events broadcast live from server

  if (event.type === 'sprint_item_updated') {

    const panel = state.panels[projectId];

    if (panel && panel.activeVtab === 'queue') loadQueue(projectId);

    scheduleLiveRefresh(projectId);

    return;

  }

  if (event.type === 'goal_updated') {

    refreshGoal(projectId);

    return;

  }

  if (event.type === 'session_started') {

    const panel = state.panels[projectId];

    if (panel && panel.activeVtab === 'queue') loadQueue(projectId);

    scheduleLiveRefresh(projectId);

    return;

  }

  // ITEM 6 — live mutation pushes (replace 10s/30s polling)

  if (event.type === 'sprint_item_added') {

    const panel = state.panels[projectId];

    if (panel && panel.activeVtab === 'queue') loadQueue(projectId);

    scheduleLiveRefresh(projectId);

    refreshProjectCountBadges(projectId);

    return;

  }

  if (event.type === 'note_added') {

    const panel = state.panels[projectId];

    if (panel && panel.activeVtab === 'notes') loadNotesTab(projectId);

    refreshProjectCountBadges(projectId);

    return;

  }

  if (event.type === 'decision_pinned') {

    if (state.panels[projectId]) loadPinnedDecisions(projectId);

    refreshProjectCountBadges(projectId);

    return;

  }

  if (event.type === 'hitl_filed') {

    refreshHitl();

    refreshProjectCountBadges(projectId);

    // 20591f72 — also reload the drawer panel if the user is on the HITL vtab.
    const _hitlPanel = state.panels[projectId];
    if (_hitlPanel && _hitlPanel.activeVtab === 'hitl') loadHitlTab(projectId);

    return;

  }



  const cache = state.panels[projectId].taskCache;

  if (event.type === 'task_created') {

    cache.unshift(event.task);

  } else if (event.type === 'task_updated') {

    const i = cache.findIndex(t => t.id === event.task.id);

    if (i >= 0) cache[i] = event.task;

    else cache.unshift(event.task);

  }

  renderTasks(projectId);

  // A goal change is often triggered by a HITL reply — re-pull.

  refreshGoal(projectId);

  // v2.3 — refresh live feed on task events when queue tab is visible.

  const panel = state.panels[projectId];

  if (panel && panel.activeVtab === 'queue' && (event.type === 'task_created' || event.type === 'task_updated')) {

    updateLiveFeed(projectId);

  }

  // v1.6.x — keep the LIVE tab fresh when it's the visible panel.

  // v1.7.0 — throttle WS bursts via scheduleLiveRefresh (10s floor).

  if (panel && panel.activeVtab === 'live') {

    scheduleLiveRefresh(projectId);

  }

}



document.getElementById('new-project-btn').onclick = async () => {

  const inp = document.getElementById('new-project-name');

  const humanInp = document.getElementById('new-project-human');

  const name = inp.value.trim();

  if (!name) return;

  const body = { name };

  const humanId = (humanInp && humanInp.value || '').trim();

  if (humanId) body.human_id = humanId;

  try {

    const p = await api('/projects', { method: 'POST', body: JSON.stringify(body) });

    inp.value = '';

    if (humanInp) humanInp.value = '';

    await loadProjects();

    openTab(p);

  } catch(e) { toast('create failed: ' + e.message, true); }

};



// Sidebar dropdown — switch active project (opens the tab if not

// already open, otherwise just activates it).

{

  const switcher = document.getElementById('project-switcher');

  if (switcher) {

    switcher.addEventListener('change', (ev) => {

      const id = ev.target.value;

      if (!id) return;

      const p = state.projects.find(x => x.id === id);

      if (p) openTab(p);

    });

  }

}



async function restoreTabs() {

  let saved = [];

  // Read preferred before the loop -- openTab calls activateTab which would
  // overwrite ACTIVE_PROJECT_KEY with each successive tab opened.
  let preferred = null;
  try { preferred = localStorage.getItem(STORAGE_KEY(ACTIVE_PROJECT_KEY)); } catch(e) {}

  try { saved = JSON.parse(localStorage.getItem(STORAGE_KEY(TABS_KEY)) || '[]'); } catch(e){}

  for (const id of saved) {

    const p = state.projects.find(x => x.id === id);

    if (p) openTab(p);

  }

  if (state.tabs.length === 0 && state.projects.length > 0) {

    // No tabs to restore -- open the preferred project or the first one.
    const fallback = state.projects.find(p => p.id === preferred) || state.projects[0];

    if (fallback) openTab(fallback);

  } else if (preferred && state.tabs.find(t => t.id === preferred)) {

    // Tabs were restored -- activate the one that was last active before reload.
    activateTab(preferred);

  }

}



(async function init() {

  // 90de5ac9 — invited users land with ?ws=<owner_tenant_id> after OAuth login.
  // Set activeWorkspaceTenantId before any API calls so loadProjects() sends
  // X-Workspace-Tenant-Id and the owner's project list loads directly.
  const _wsParam = new URLSearchParams(window.location.search).get('ws');
  if (_wsParam && !state.activeWorkspaceTenantId) {
    state.activeWorkspaceTenantId = _wsParam;
    try { history.replaceState(null, '', window.location.pathname); } catch (_) {}
  }

  await loadServerConfig();

  showFailoverBannerIfNeeded();

  // v1.9.x — show connection setup modal if no meridian.toml exists

  if (typeof window._showConnSetupIfNeeded === 'function') {

    window._showConnSetupIfNeeded(state.serverConfig);

  }

  await loadConfig();

  await loadProjects();

  // 90de5ac9 (follow-up) — existing invited members who have no own projects:
  // silently switch to their first accepted workspace membership so they land
  // in the right context without being shown the 'create first project' wizard.
  if (state.projects.length === 0 && !state.activeWorkspaceTenantId && isHostedMode() && !isDemoMode()) {
    try {
      const wss = await fetch('/me/workspaces').then(r => r.ok ? r.json() : null);
      const first = wss && wss.find(w => !w.is_own);
      if (first) {
        state.activeWorkspaceTenantId = first.tenant_id;
        await loadProjects();
      }
    } catch (_) {}
  }

  if (isDemoMode()) hideDemoAdminControls();

  if (isHostedMode()) hideHostedAdminControls();

  // abd58a10 — render the workspace switcher independently of _renderPlanBadge.
  // It used to be reached only at the tail of _renderPlanBadge(me), so any /me
  // hiccup (slow, empty, missing plan) silently dropped the switcher for invited
  // members. Calling it directly here guarantees it appears whenever the user
  // belongs to ≥2 workspaces. ensureWorkspaceSwitcher() is idempotent.
  if (isHostedMode() && !isDemoMode()) ensureWorkspaceSwitcher();

  _refreshGuestMode();

  showLocalServerControls();

  ensureTourButton();

  ensureFeedbackButton();

  // v0.6.6 — EZ first-run wizard: if no projects exist, show the overlay

  // Skip in demo mode — demo DB always has projects seeded.

  if (state.projects.length === 0 && !isDemoMode()) {

    document.getElementById('ez-wizard').style.display = 'flex';

    return; // don't restore tabs until wizard completes

  }

  await restoreTabs();

  const dashboardParams = new URLSearchParams(window.location.search);

  const requestedProjectId = dashboardParams.get('project_id') || '';

  const requestedTab = dashboardParams.get('tab') || '';

  if (requestedProjectId) {

    const requestedProject = state.projects.find(p => p.id === requestedProjectId);

    if (requestedProject) {

      openTab(requestedProject);

      if (requestedTab && requestedTab !== 'status') {

        setTimeout(() => {

          document.querySelector(`#vtab-strip-${requestedProject.id} .vtab-btn[data-vtab="${requestedTab}"]`)?.click();

        }, 0);

      }

    }

  }

  // v1.5.x — polling removed. WebSocket pushes task/goal updates; sessions

  // refresh on initial page load + explicit user action only (tab switch,

  // worker start, etc). Idle dropdowns no longer hammer /sessions every 1s.



  // v2.4 — HITL queue: poll for pending requests every 30s + initial load.

  initHitlPanel();



  // v1.7.0 — stop server button (self-hosted only — never wired on usemeridian.us)

  if (!isHostedMode()) {

    const stopBtn = document.getElementById('stop-server-btn');

    if (stopBtn) {

      stopBtn.onclick = async () => {

        if (!confirm('Stop the Meridian server? You will need to run `pixi run start` to restart.')) return;

        try {

          await api('/admin/shutdown', { method: 'POST' });

          stopBtn.textContent = 'Stopped — run pixi run start';

          stopBtn.disabled = true;

          const restartBtn = document.getElementById('restart-server-btn');

          if (restartBtn) restartBtn.style.display = 'none';

        } catch(e) {

          toast('Shutdown request sent.', false);

        }

      };

    }



    // v1.9.x — restart button in sidebar footer (self-hosted only)

    const restartBtn = document.getElementById('restart-server-btn');

    if (restartBtn) {

      restartBtn.onclick = async () => {

        await _doRestart();

      };

    }

  }



  // v1.9.x — restart button in update banner

  const bannerRestartBtn = document.getElementById('banner-restart-btn');

  if (bannerRestartBtn) {

    bannerRestartBtn.onclick = async () => { await _doRestart(); };

  }



  // v1.9.x — git remote warning: poll every 60s, show yellow banner if remote ahead

  async function _checkGitStatus() {

    try {

      const data = await api('/admin/git-status');

      const banner = document.getElementById('git-banner');

      const msg = document.getElementById('git-banner-msg');

      if (banner && data.warning) {

        if (msg) msg.textContent = data.warning;

        banner.style.display = 'block';

      }

    } catch(_) {}

  }

  _checkGitStatus();

  setInterval(_checkGitStatus, 60000);

  const workspaceEntry = document.getElementById('workspace-entry');

  if (workspaceEntry) {

    workspaceEntry.onclick = () => {

      const targetId = state.activeTab || state.projects[0]?.id;

      const project = state.projects.find(p => p.id === targetId);

      if (!project) return;

      if (!document.getElementById(`tab-body-${targetId}`)) openTab(project);

      document.querySelector(`#vtab-strip-${targetId} [data-vtab="settings"]`)?.click();

    };

  }

})();



const _sprintBoardReloaders = {};

const _sprintSelectSyncers = {};



async function _deleteSprintItem(projectId, itemId) {

  if (!confirm('Remove this sprint item?')) return;

  try {

    await api(`/projects/${projectId}/sprint-items/${itemId}`, { method: 'DELETE' });

    if (_sprintBoardReloaders[projectId]) _sprintBoardReloaders[projectId]();

  } catch(e) { console.error('Delete sprint item failed:', e); }

}



async function _sprintAction(projectId, itemId, action) {

  try {

    await api(`/projects/${projectId}/sprint-items/${itemId}/${action}`, { method: 'POST' });

    if (_sprintBoardReloaders[projectId]) _sprintBoardReloaders[projectId]();

  } catch(e) { console.error('Sprint action failed:', action, e); }

}



async function completeSprintItem(projectId, itemId) {

  try {

    await api(`/projects/${projectId}/sprint-items/${itemId}/complete`, { method: 'POST' });

    if (_sprintBoardReloaders[projectId]) _sprintBoardReloaders[projectId]();

  } catch(e) { console.error('Complete sprint item failed:', e); }

}



async function failSprintItem(projectId, itemId) {

  try {

    await api(`/projects/${projectId}/sprint-items/${itemId}/fail`, { method: 'POST' });

    if (_sprintBoardReloaders[projectId]) _sprintBoardReloaders[projectId]();

  } catch(e) { console.error('Fail sprint item failed:', e); }

}





// --- v0.6.6 EZ wizard ---

// v0.6.6 — EZ wizard logic

document.getElementById('ez-create-btn').onclick = async () => {

  const nameEl = document.getElementById('ez-project-name');

  const humanEl = document.getElementById('ez-human-name');

  const errEl = document.getElementById('ez-error');

  const name = nameEl.value.trim();

  if (!name) { errEl.textContent = 'project name is required'; errEl.style.display = 'block'; return; }

  errEl.style.display = 'none';

  try {

    const body = { name };

    if (humanEl.value.trim()) body.human_id = humanEl.value.trim();

    const p = await api('/projects', { method: 'POST', body: JSON.stringify(body) });

    document.getElementById('ez-wizard').style.display = 'none';

    await loadProjects();

    await restoreTabs();

    openTab(p);

  } catch(e) { errEl.textContent = 'create failed: ' + e.message; errEl.style.display = 'block'; }

};

document.getElementById('ez-project-name').addEventListener('keydown', (e) => {

  if (e.key === 'Enter') document.getElementById('ez-create-btn').click();

});

document.getElementById('ez-advanced-link').onclick = (e) => {

  e.preventDefault();

  document.getElementById('ez-wizard').style.display = 'none';

  // Show the sidebar new-project form and focus it

  document.getElementById('new-project-name').focus();

  restoreTabs();

};



// v1.9.x — connection setup modal

(function() {

  const modal = document.getElementById('conn-setup-modal');

  const localBtn = document.getElementById('conn-local-btn');

  const sqliteForm = document.getElementById('conn-sqlite-form');

  const sqlitePath = document.getElementById('conn-sqlite-path');

  const sqliteName = document.getElementById('conn-sqlite-name');

  const sqliteSave = document.getElementById('conn-sqlite-save-btn');

  const pgToggle = document.getElementById('conn-pg-toggle-btn');

  const pgForm = document.getElementById('conn-pg-form');

  const pgSave = document.getElementById('conn-pg-save-btn');

  const pgUrl = document.getElementById('conn-pg-url');

  const pgName = document.getElementById('conn-pg-name');

  const errEl = document.getElementById('conn-setup-err');

  if (!modal) return;



  function showErr(msg) { if (errEl) { errEl.textContent = msg; errEl.style.display = msg ? 'block' : 'none'; } }



  document.addEventListener('keydown', (e) => {

    if (e.key === 'Escape') {

      const m = document.getElementById('conn-setup-modal');

      if (m && m.style.display !== 'none') m.style.display = 'none';

    }

  });



  window._showConnSetupIfNeeded = (cfg) => {

    // G3.13 — demo mode never sets up its own connection; suppressing the

    // wizard there avoids a confusing modal on /demo with the seeded repo.

    if (typeof isDemoMode === 'function' && isDemoMode()) return;

    if (!cfg?.toml_exists && cfg?.db !== 'postgres') modal.style.display = 'flex';

    // Show config file path

    const pathEl = document.getElementById('conn-toml-path');

    if (pathEl && cfg?.toml_path) {

      pathEl.innerHTML = '📄 Config: <span style="color:var(--text)">' + escapeHtml(cfg.toml_path) + '</span>';

    }

  };



  const PG_BTN_STYLE = 'padding:12px;font-size:12px;text-align:left;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);cursor:pointer;font-family:\'IBM Plex Mono\',monospace';

  const PRIMARY_BTN_STYLE = 'padding:12px;font-size:12px;text-align:left;border-radius:4px;cursor:pointer;font-family:\'IBM Plex Mono\',monospace;background:var(--accent);border:1px solid var(--accent);color:#001020;font-weight:600';



  function setActiveBtn(which) {

    // which: 'sqlite' | 'postgres' | null

    if (localBtn) localBtn.style.cssText = which === 'sqlite' ? PRIMARY_BTN_STYLE : PG_BTN_STYLE;

    if (pgToggle) pgToggle.style.cssText = which === 'postgres' ? PRIMARY_BTN_STYLE : PG_BTN_STYLE;

  }



  // Local SQLite toggle — show path form

  if (localBtn) localBtn.onclick = () => {

    if (!sqliteForm) return;

    const open = sqliteForm.style.display === 'flex';

    sqliteForm.style.display = open ? 'none' : 'flex';

    if (!open && sqlitePath && !sqlitePath.value) sqlitePath.value = 'data/meridian.db';

    setActiveBtn(open ? null : 'sqlite');

    // Close postgres form

    if (pgForm) pgForm.style.display = 'none';

  };



  if (sqliteSave) sqliteSave.onclick = async () => {

    const path = sqlitePath?.value.trim() || 'data/meridian.db';

    const name = sqliteName?.value.trim() || 'local';

    showErr('');

    try {

      sqliteSave.textContent = 'Saving…'; sqliteSave.disabled = true;

      await api('/config/connections', {

        method: 'POST',

        body: JSON.stringify({ name, type: 'sqlite', path, activate: true }),

      });

      modal.style.display = 'none';

      toast('Saved — restarting…');

      await _doRestart(false);

    } catch(e) {

      showErr('Failed: ' + e.message);

      sqliteSave.textContent = 'Save & Restart →'; sqliteSave.disabled = false;

    }

  };



  if (pgToggle) pgToggle.onclick = () => {

    if (pgForm) {

      const open = pgForm.style.display === 'flex';

      pgForm.style.display = open ? 'none' : 'flex';

      setActiveBtn(open ? null : 'postgres');

    }

    // Close sqlite form

    if (sqliteForm) sqliteForm.style.display = 'none';

  };



  if (pgSave) pgSave.onclick = async () => {

    const url = pgUrl?.value.trim() || '';

    const name = pgName?.value.trim() || 'postgres';

    showErr('');

    if (!url) { showErr('Postgres URL is required'); return; }

    try {

      pgSave.textContent = 'Saving…'; pgSave.disabled = true;

      await api('/config/connections', {

        method: 'POST',

        body: JSON.stringify({ name, type: 'postgres', url, activate: true }),

      });

      modal.style.display = 'none';

      toast('Saved — restarting…');

      await _doRestart(false);

    } catch(e) {

      showErr('Failed: ' + e.message);

      pgSave.textContent = 'Save & Restart →'; pgSave.disabled = false;

    }

  };

})();



// ---------------------------------------------------------------------------

// v1.3.0 — Rewind tab. Renders "Last X days" project recaps. The window

// buttons live in the drawer header (see buildTabBody); the panel here

// just wires their clicks + the "copy shareable link" / load handlers.

// ---------------------------------------------------------------------------






function toggleExpand(id) {

  const el = document.getElementById(id);

  if (!el) return;

  const open = el.style.display !== 'none';

  el.style.display = open ? 'none' : '';

  const trigger = el.previousElementSibling;

  if (trigger) {

    const arrow = trigger.querySelector('.expand-arrow');

    if (arrow) arrow.textContent = open ? '▶' : '▼';

  }

}

































// --- ITEM 4 esbuild: re-expose top-level symbols as globals so inline
// handlers and cross-file references keep resolving after IIFE bundling.
try { Object.assign(window, { loadCodeIntelTab, _initCodeIntelTabVisibility, hideHostedAdminControls, ensureSignOutLink, ensureWorkspaceSwitcher, getActiveWorkspaceRole, showConnectDbModal, showLocalServerControls, _summarizeApiErrorText, _projectLoadErrorInfo, wireProjectLoadRetry, renderProjectLoadError, recordProjectLoadError, clearProjectLoadError, renderProjectLoadAlert, retryProjectSurface, syncSidebarActiveProject, autosizeGoalField, githubIconSvg, getConstitutionLimit, loadProjectSettings, saveProjectSettings, loadExecutorRulesSection, loadTunnelPluginsSection, _demoTourDone, _demoTourSavedStep, _demoTourSaveStep, _demoTourMarkDone, _demoTourClose, _tourActivateVtab, startDemoTour, resumeDemoTour, api, projectApi, loadServerConfig, _armAccountSwitchWatch, _refreshOnFocus, _checkAccountSwitch, _showAccountSwitchBanner, updateGitHubConnectionIndicator, _updateConnectionIndicator, checkGitStatus, _doRestart, loadConfig, loadProjects, openTab, closeTab, saveTabs, renderTabs, _makeTabEl, _openTabMenu, _setProjectIcon, _renameProject, _deleteProject, activateTab, buildTabBody, scheduleLiveRefresh, initLiveAutoRefresh, loadLiveTab, refreshLiveTab, wireSprintAddEnter, sprintAction, sprintArchive, filterBackburner, sprintPushPrompt, sprintFeedback, sprintFeedbackNote, sprintItemEdit, addSprintItemFromInput, cacheMostRecentSession, renderLiveSessions, endLiveSession, openTimelineForSession, renderLiveQueue, addLiveTask, cancelLiveTask, showCopyPreview, wireClaudeLaunchPanel, stampHandoffTs, populateSessionDropdown, loadTimeline, _renderTimelineLog, loadDocsTab, normalizeNotifyTarget, displayNotifyTarget, osExecutorHintBanner, showFailoverBannerIfNeeded, suggestNtfyTopic, loadHitlTab, loadTeamTab, updateLiveFeed, loadRecentSessions, loadMilestones, loadRecentRuns, loadQueue, renderSearchResults, wireQueueSectionToggles, refreshTab, refreshGoal, parseDecisionsBlob, renderConstitutionWarning, _hitlBadgeClick, initHitlPanel, setVtabCountBadge, refreshProjectCountBadges, refreshHitl, _hitlAnswer, _hitlDismiss, loadPinnedDecisions, supersedePinnedDecision, addPinnedDecision, consolidateDecisions, renderDecisionsTable, wireGoalPreviewToggle, saveGoal, saveNorthStar, saveSprint, _sessionPresenceDot, refreshSessions, refreshTasks, renderTasks, _loadMoreTasks, renderTaskRow, deleteTaskRow, renderHitlRow, wireHitlRow, appendToGoal, hitlReply, hitlExecute, connectWs, handleWsEvent, restoreTabs, _deleteSprintItem, _sprintAction, completeSprintItem, failSprintItem, toggleExpand, state }); } catch (e) {}
