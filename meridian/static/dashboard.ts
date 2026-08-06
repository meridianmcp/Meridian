// --- ITEM 4 esbuild: pull module scripts into the bundle graph ---
// 423f5929 — modules migrated .js → .ts; imports are extensionless so esbuild
// (resolveExtensions) and tsc (bundler resolution) both resolve the .ts files.
import "./dashboard-core";
import "./dashboard-utils";
import "./dashboard-demo";
import "./dashboard-timeline";
import "./dashboard-mcp";
import "./dashboard-sprint";
// e3355ccb — live parallel-execution wave/batch progress panel. Recomputes the
// server's conflict-free batches (get_parallelizable_groups) client-side from
// the already-fetched sprint-items payload; rendered in the Live tab.
import "./dashboard-waves";
import "./dashboard-settings";
import "./dashboard-plugins";

import "./dashboard-notes";
import "./dashboard-documents";
import "./dashboard-files";
import "./dashboard-rewind";
// e553fa7a — workspace Blog tab editor/card builders (edit-in-place for drafts).
import {
  blogEditorFormHtml,
  blogPostCardHtml,
  populateBlogEditor,
  resetBlogEditor,
} from "./dashboard-blog";
// ff8ff615 — Preact Code Intel panel (layered package DAG, replaces ECharts circles).
import { mountCodeIntelPanel } from "./components/CodeIntelPanel";
// ed5512b6 — standalone, decoupled codegraph visualizer (folder->file->function
// drill-down, color by role, static metadata on click). The dashboard is ONE
// consumer via a thin adapter; the module itself has zero Meridian coupling.
import { buildCodeGraphModel, renderCodeGraph } from "./codegraph";
import type { GraphNodeInput } from "./codegraph";
import { createStore } from "zustand/vanilla";
// 2d3b8424 — group the ~18 flat vtabs into logical groups (Overview/Planning/
// Work/Content/History). Pure IA: every data-vtab still renders, unchanged.
// The grouped button markup lives inline in buildTabBody (literal buttons, so
// the source-scanning UI tests keep matching); this module owns the grouping
// model + the collapse/reveal wiring.
import { wireVtabGroups } from "./dashboard-tabgroups";
// d6b7da48 — client-side sidebar "folders/spheres" (localStorage-only grouping).
import {
  loadFolderAssignments,
  saveFolderAssignments,
  assignProjectToFolder,
  groupProjectsByFolder,
  knownFolderNames,
  loadCollapsedFolders,
  toggleFolderCollapsed,
  UNGROUPED_LABEL,
  type FolderProject,
} from "./dashboard-folders";
// 0fed6a42 — one-level-deep subproject hierarchy (parent_project_id). Pure helpers
// mirror the server's set_parent_project guards so the picker only offers legal
// parents and the sidebar can render subprojects nested under their parent.
import {
  flattenHierarchy,
  eligibleParents,
  type HierProject,
} from "./dashboard-subprojects";
﻿const TABS_KEY = 'meridian.openTabs';

const ACTIVE_PROJECT_KEY = 'meridian.activeProject';

// const _PLAN_LABELS -- moved to dashboard-utils.js

// 88e66aa9 — Phase 1: back window.state with a Zustand (vanilla) store so all
// dashboard state flows through one store. UI panels are untouched — they still
// read/write window.state.xxx; a Proxy bridges the legacy object API to the
// store (reassignments call setState; in-place mutations share the same refs).
interface DashboardState {
  projects: any[];
  tabs: any[];
  activeTab: any;
  panels: Record<string, any>;
  apiKeyConfigured: boolean;
  serverConfig: { server_url: string; host: string; port: number; version: string };
  activeWorkspaceTenantId: any;
  [k: string]: any;
}

const _initialDashboardState: DashboardState = {
  projects: [],
  tabs: [],            // [{id, project}]
  activeTab: null,
  panels: {},          // tabId -> { ws, taskCache, sessionName, goalRaw, goalIsJson }
  apiKeyConfigured: false,
  // v0.6.5 — server runtime config fetched from /config on startup.
  serverConfig: { server_url: '', host: '', port: 0, version: '' },
  // workspace switcher — tenant_id of the active workspace (null = own)
  activeWorkspaceTenantId: null,
};

const dashboardStore = createStore<DashboardState>(() => ({ ..._initialDashboardState }));

// Proxy bridging the legacy `state.xxx` object API to the Zustand store.
const state: DashboardState = new Proxy(_initialDashboardState, {
  get: (_t, prop) => (dashboardStore.getState() as any)[prop as any],
  set: (_t, prop, value) => { dashboardStore.setState({ [prop as any]: value } as any); return true; },
  has: (_t, prop) => (prop as any) in dashboardStore.getState(),
  deleteProperty: (_t, prop) => {
    const next: any = { ...dashboardStore.getState() };
    delete next[prop as any];
    dashboardStore.setState(next, true);
    return true;
  },
  ownKeys: () => Reflect.ownKeys(dashboardStore.getState()),
  getOwnPropertyDescriptor: (_t, prop) => ({
    enumerable: true, configurable: true,
    value: (dashboardStore.getState() as any)[prop as any],
  }),
}) as DashboardState;

// Expose state (+ the underlying store, for later slice migration) on window so
// esbuild IIFE modules can access it via window.state.
window.state = state;
(window as any).dashboardStore = dashboardStore;



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

  } catch (e: any) { /* not hosted / not logged in */ }



  // Rename "advanced setup ↗" → "Close" in first-run wizard (no local config on hosted)

  const advLink = document.getElementById('ez-advanced-link');

  if (advLink) advLink.textContent = 'Close';

}



function ensureSignOutLink(emailHint?: any) {

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

    if (emailHint) document.getElementById('signout-link')!.title = `Signed in as ${emailHint}`;

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

  workspaces.forEach((ws: any) => {

    const opt = document.createElement('option');

    opt.value = ws.tenant_id;

    opt.textContent = ws.is_own ? 'My workspace' : ws.owner_email;

    if (!state.activeWorkspaceTenantId && ws.is_own) opt.selected = true;

    if (state.activeWorkspaceTenantId === ws.tenant_id) opt.selected = true;

    sel.appendChild(opt);

  });



  sel.onchange = async () => {

    const chosen = sel.value;

    const own = workspaces.find((w: any) => w.is_own);

    state.activeWorkspaceTenantId = (own && chosen === own.tenant_id) ? null : chosen;

    // Close all open tabs — they belong to the old workspace.

    [...state.tabs].forEach(t => { try { closeTab(t.id); } catch (_) {} });

    await loadProjects();

    // Show which workspace is active.

    const active = workspaces.find((w: any) => w.tenant_id === chosen);

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
    const ws = (wss || []).find((w: any) => w.tenant_id === state.activeWorkspaceTenantId);
    return (ws && ws.role) || 'owner';
  } catch (_) { return 'owner'; }
}


// fcb02a6d — plan/role badge in the sidebar. On your own workspace it shows your
// plan (Free/Trial/Standard/Pro); on a workspace you were invited to it shows an
// "invite · {role}" badge. Re-rendered on every workspace switch.
function _renderWorkspaceContextBadge(wrap: any, workspaces: any) {
  if (!wrap) return;
  let badge = wrap.querySelector('.ws-context-badge');
  if (!badge) {
    badge = document.createElement('div');
    badge.className = 'ws-context-badge';
    badge.style.cssText = 'display:inline-block;margin-top:6px;padding:2px 8px;border-radius:10px;font-size:9px;font-weight:700;letter-spacing:.05em;font-family:var(--font-mono);text-transform:uppercase';
    wrap.appendChild(badge);
  }
  const active = (workspaces || []).find((w: any) =>
    state.activeWorkspaceTenantId ? w.tenant_id === state.activeWorkspaceTenantId : w.is_own);
  const colors: Record<string, string> = { free: '#3b82f6', trial: '#059669', standard: '#3b82f6', pro: '#7c3aed', admin: '#9ca3af', invite: '#f59e0b' };
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
function _filterTabRows(query: any, container: any, rowSelector: any) {
  if (!container) return;
  const q = (query || '').trim().toLowerCase();
  container.querySelectorAll(rowSelector).forEach((row: any) => {
    const hay = (row.dataset.search || row.textContent || '').toLowerCase();
    row.style.display = (!q || hay.includes(q)) ? '' : 'none';
  });
}

function _wireTabSearch(inputId: any, containerId: any, rowSelector: any) {
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

  box.querySelector('#connect-db-cancel')!.onclick = () => overlay.remove();

  overlay.onclick = e => { if (e.target === overlay) overlay.remove(); };

  box.querySelector('#connect-db-save')!.onclick = async () => {

    const url = urlInput!.value.trim();

    if (!url) { statusEl!.textContent = 'Enter a connection string.'; statusEl!.style.color = 'var(--danger,#dc2626)'; return; }

    statusEl!.textContent = 'Connecting…'; statusEl!.style.color = 'var(--muted)';

    try {

      await api('/workspace/connect-db', { method: 'POST', body: JSON.stringify({ url }) });

      statusEl!.textContent = 'Connected! Reloading…'; statusEl!.style.color = '#059669';

      setTimeout(() => { overlay.remove(); loadProjects(); }, 800);

    } catch (e: any) {

      statusEl!.textContent = e.message || 'Connection failed — check the URL and credentials.';

      statusEl!.style.color = 'var(--danger,#dc2626)';

    }

  };

  urlInput!.focus();

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



const STORAGE_KEY = (k: any) => (isDemoMode() ? 'meridian_demo_' : 'meridian_') + k.replace(/^meridian[._]/, '');

// const QUEUE_DONE_PAGE_SIZE, SESSION_LIVE_WINDOW_MS, DEFAULT_MAX_PINNED_DECISIONS, DEFAULT_CONTEXT_THRESHOLD -- moved to dashboard-utils.js

const NORTH_STAR_MIN_HEIGHT_PX = 180;

const GITHUB_OCTICON_PATH = 'M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z';



// function getPanelState -- moved to dashboard-utils.js



function _summarizeApiErrorText(raw: any) {

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



function _projectLoadErrorInfo(path: any, error: any) {

  const status = Number.isFinite(Number(error?.status))

    ? Number(error.status)

    : (String(error?.message || '').match(/^(\d{3})\s*:/) ? parseInt(String(error.message).match(/^(\d{3})\s*:/)![1], 10) : null);

  const rawText = error?.responseText || error?.message || String(error || 'Request failed');

  return {

    endpoint: path,

    status,

    summary: _summarizeApiErrorText(rawText),

    at: Date.now(),

  };

}



function wireProjectLoadRetry(container: any, projectId: any) {

  container?.querySelectorAll('[data-project-retry]').forEach((btn: any) => {

    btn.onclick = () => retryProjectSurface(projectId);

  });

}



function renderProjectLoadError(projectId: any, title: any, path: any, error: any) {

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



function recordProjectLoadError(projectId: any, path: any, error: any) {

  const panel = getPanelState(projectId);

  panel.loadErrors = panel.loadErrors || {};

  const info = _projectLoadErrorInfo(path, error);

  panel.loadErrors[path] = info;

  renderProjectLoadAlert(projectId);

  return info;

}



function clearProjectLoadError(projectId: any, path: any) {

  const panel = getPanelState(projectId);

  if (!panel.loadErrors || !panel.loadErrors[path]) return;

  delete panel.loadErrors[path];

  renderProjectLoadAlert(projectId);

}



function renderProjectLoadAlert(projectId: any) {

  const host = document.getElementById(`project-fetch-alert-${projectId}`);

  if (!host) return;

  const panel = getPanelState(projectId);

  const errors = Object.values(panel.loadErrors || {}).sort((a: any, b: any) => b.at - a.at);

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

      ${visible.map((info: any) => {

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



async function retryProjectSurface(projectId: any) {

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



function autosizeGoalField(el: any, minPx = NORTH_STAR_MIN_HEIGHT_PX) {

  if (!el) return;

  // Use 'auto' not '0px' — avoids the collapse flash before recalculating scrollHeight (97bfb153)

  el.style.height = 'auto';

  el.style.height = `${Math.max(el.scrollHeight, minPx)}px`;

}



function githubIconSvg(size = 12, color = 'currentColor') {

  return `<svg width="${size}" height="${size}" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true" focusable="false" style="color:${color};flex-shrink:0"><path d="${GITHUB_OCTICON_PATH}"></path></svg>`;

}



function getConstitutionLimit(projectId: any) {

  const panel = getPanelState(projectId);

  const parsed = parseInt(String(panel._projectSettings?.max_pinned_decisions || ''), 10);

  return Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_MAX_PINNED_DECISIONS;

}



async function loadProjectSettings(projectId: any, opts: any = {}) {

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



async function saveProjectSettings(projectId: any, patch: any) {

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
async function loadExecutorRulesSection(projectId: any) {

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

    ta!.addEventListener('input', () => {

      charEl!.textContent = `${ta!.value.length} chars`;

    });

    document.getElementById(`agent-instructions-save-${projectId}`)!.onclick = async () => {

      try {

        await api(`/projects/${projectId}/agent-instructions`, {

          method: 'PATCH',

          body: JSON.stringify({ agent_instructions: ta!.value }),

        });

        toast('Executor rules saved');

      } catch (e: any) { toast('Save failed: ' + e.message, true); }

    };

    document.getElementById(`agent-instructions-reset-${projectId}`)!.onclick = async () => {

      if (!confirm('Reset to Meridian default executor rules? Your custom rules will be replaced.')) return;

      try {

        const r = await api(`/projects/${projectId}/agent-instructions`, {

          method: 'PATCH',

          body: JSON.stringify({ agent_instructions: null }),

        });

        ta!.value = r.agent_instructions || defaultText;

        charEl!.textContent = `${ta!.value.length} chars`;

        toast('Reset to defaults');

      } catch (e: any) { toast('Reset failed: ' + e.message, true); }

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

    ciToggle!.onchange = async () => {

      const enabled = ciToggle!.checked ? 1 : 0;

      ciInfo!.style.display = enabled ? 'block' : 'none';

      try {

        await api(`/projects/${projectId}/settings`, {

          method: 'PATCH',

          body: JSON.stringify({ code_intel_enabled: enabled }),

        });

        toast(enabled ? 'Code Intelligence enabled' : 'Code Intelligence disabled');

      } catch (e: any) { toast('Save failed: ' + e.message, true); }

    };

  } catch (e: any) { section.innerHTML = `<div class="empty" style="color:var(--error)">Failed to load executor rules: ${escapeHtml(e.message)}</div>`; }

}


// Tunnel Plugins — moved to dashboard-plugins.js (sprint item b11c2d8b)
// loadTunnelPluginsSection, _TUNNEL_DEFAULT_PORTS, _OPTIN_SLOT_HINTS,
// _CURATED_TUNNEL_PLUGINS, _detectTunnelOs, _TUNNEL_INSTALL_CMDS,
// _tunnelCopyToClipboard, _renderPluginBrowseSection, _wireRegistryCopyButtons,
// _wireRegistryBrowse are all in dashboard-plugins.js.

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

  try { return localStorage.getItem(STORAGE_KEY('tour.done')) === '1'; } catch(e: any) { return false; }

}

function _demoTourSavedStep() {

  try { return parseInt(localStorage.getItem(STORAGE_KEY('tour.step')) || '0', 10) || 0; } catch(e: any) { return 0; }

}

function _demoTourSaveStep(step: any) {

  try { localStorage.setItem(STORAGE_KEY('tour.step'), String(step)); } catch(e: any) {}

}

function _demoTourMarkDone() {

  try {

    localStorage.setItem(STORAGE_KEY('tour.done'), '1');

    localStorage.removeItem(STORAGE_KEY('tour.step'));

  } catch(e: any) {}

}

function _demoTourClose() {

  document.getElementById('demo-tour-tooltip')?.remove();

  document.getElementById('demo-tour-highlight')?.remove();

}



// Open a main vtab (and optional goal subtab) on the active project so the

// step's tip lands on the right surface. No-op if the panel isn't mounted yet.

function _tourActivateVtab(vtab: any, gtab: any) {

  const pid = state.activeTab;

  if (!pid || !vtab) return;

  const btn = document.querySelector(`#vtab-strip-${pid} .vtab-btn[data-vtab="${vtab}"]`);

  if (btn) btn.click();

  if (gtab) {

    const gbtn = document.querySelector(`#drawer-goal-${pid} .goal-subtab-btn[data-gtab="${gtab}"]`);

    if (gbtn) gbtn.click();

  }

}



function startDemoTour(step: any) {

  _demoTourClose();



  if (step < 0) step = 0;

  if (step >= _DEMO_TOUR_STEPS.length) { _demoTourMarkDone(); return; }

  // Persist progress so closing the tooltip / reopening the demo resumes here.

  _demoTourSaveStep(step);

  const s = _DEMO_TOUR_STEPS[step];



  // Surface the tab/subtab this step describes, then let it render before

  // measuring the highlight target.

  try { _tourActivateVtab(s.vtab, s.gtab); } catch(e: any) {}



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

  document.getElementById('demo-tour-next')!.onclick = () => {

    if (isLast) { _demoTourClose(); _demoTourMarkDone(); }

    else startDemoTour(step + 1);

  };

  const backBtn = document.getElementById('demo-tour-back');

  if (backBtn) backBtn.onclick = () => startDemoTour(step - 1);

  // Explicit Finish: mark done so the tour never auto-shows again.

  document.getElementById('demo-tour-finish')!.onclick = () => {

    _demoTourClose();

    _demoTourMarkDone();

  };

}



// Resume the tour at the saved step (or step 0). Does nothing if finished.

function resumeDemoTour() {

  if (_demoTourDone()) return;

  startDemoTour(_demoTourSavedStep());

}



// function api -- moved to dashboard-core.js (sprint item ea0eda34)
// function projectApi -- moved to dashboard-core.js
// _staleProjectsHandled -- moved to dashboard-core.js


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

  } catch (e: any) { /* offline / older server — ignore */ }

  // Show demo overlay whenever on /demo path (regardless of MERIDIAN_DEMO env var)

  if (window.location.pathname.startsWith('/demo')) {

    // Clear stale project IDs from prior logins so /demo never 404s on a stale project.

    try { localStorage.removeItem(STORAGE_KEY(TABS_KEY)); } catch(e: any) {}

    try { localStorage.removeItem(STORAGE_KEY(ACTIVE_PROJECT_KEY)); } catch(e: any) {}

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

  } catch (e: any) { /* not hosted or not logged in */ }

}



// function _renderPlanBadge -- moved to dashboard-sprint.js



// Detect when the active session belongs to a different account than the one

// this page loaded as (e.g. the user signed into another account in a second

// tab). Re-auth replaces the session cookie underneath the loaded page, so its

// in-flight API calls would start 404ing against the wrong workspace. Rather

// than let that happen silently, watch /me and prompt a refresh.

function _armAccountSwitchWatch(loadedEmail: any) {

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



function _showAccountSwitchBanner(newEmail: any) {

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

function updateGitHubConnectionIndicator(source: any) {

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
function updateTunnelConnectionIndicator(me: any) {

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



function _updateConnectionIndicator(cfg: any) {

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

    dot!.style.background = 'var(--accent-green)';

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

  dot!.style.background = dbType === 'postgres' ? 'var(--accent)' : 'var(--accent-green)';

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

      let displayConns = (conns || []).map((c: any) => ({...c, active: c.name === activeName}));

      if (hosted) {

        // Both normal hosted users and hosted admins only see postgres connections.

        // Local/sqlite options would break tenant isolation or are simply not

        // meaningful on a hosted server.

        displayConns = displayConns.filter((c: any) => (c.type || 'sqlite') === 'postgres');

      }

      if (!displayConns.find((c: any) => c.active)) {

        displayConns.unshift({name: activeName, type: cfg.db, active: true});

      }

      displayConns.forEach((c: any) => {

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

            } catch(ex: any) { toast('Remove failed: ' + ex.message, true); }

          };

          item.appendChild(del);

        }

        item.onmouseenter = () => { if (!c.active) left.style.color = 'var(--accent)'; item.style.background = 'var(--surface-3)'; };

        item.onmouseleave = () => { left.style.color = ''; item.style.background = ''; };

        item.onclick = async (e) => {

          if (e.target!.tagName === 'BUTTON') return; // don't activate on delete click

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

          } catch(e: any) { console.error('Switch failed:', e); toast('Switch failed: ' + e.message, true); }

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

        addItem.onclick = () => { popup.remove(); document.getElementById('conn-setup-modal')!.style.display = 'flex'; };

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

    switcher.innerHTML = conns.map((c: any) =>

      `<option value="${c.name}" ${c.active ? 'selected' : ''}>${c.name}</option>`

    ).join('');

    switcher.onchange = async () => {

      try {

        const sel = switcher.value;

        const conn = (cfg.connections || []).find((c: any) => c.name === sel) || {};

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

      } catch(e: any) { toast('Switch failed: ' + e.message, true); }

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

  } catch(e: any) {

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

      methodEl!.textContent = 'Auth: Claude Max OAuth';

      methodEl!.style.display = 'block';

    } else if (cfg.method === 'api_key') {

      methodEl!.textContent = 'Auth: API key';

      methodEl!.style.display = 'block';

    } else {

      methodEl!.style.display = 'none';

    }

  } catch (e: any) { /* ignore */ }

}



async function loadProjects() {

  const list = document.getElementById('project-list');

  try {

    state.projects = await api('/projects');

  } catch (e: any) {

    state.projects = [];

    if (list) {

      list.innerHTML = `<div class="empty" style="color:var(--status-failed);padding:6px 4px">projects failed: ${escapeHtml(e.message)}</div>`;

    }

    return;

  }

  list!.innerHTML = '';

  // d6b7da48 — group the flat project list into client-side "folders/spheres".
  // Membership + collapse state are localStorage-only (no backend/schema). When
  // no project is assigned to any folder, groupProjectsByFolder yields a single
  // ungrouped catch-all and rendering is behavior-identical to the old flat list
  // (the catch-all header is suppressed — see below).

  const assignments = loadFolderAssignments(STORAGE_KEY('projectFolders'));

  const collapsed = loadCollapsedFolders(STORAGE_KEY('projectFolderCollapsed'));

  const groups = groupProjectsByFolder<FolderProject>(state.projects as FolderProject[], assignments);

  // Only decorate with folder chrome when at least one named folder exists.

  const hasFolders = groups.some(g => g.folder !== null);

  groups.forEach(group => {

    // Suppress the "Ungrouped" header entirely when nothing is foldered — the

    // list then looks exactly like the pre-folders flat list.

    if (hasFolders) {

      const isCollapsed = collapsed.has(group.key);

      const header = document.createElement('div');

      header.className = 'project-folder-header';

      header.dataset.folderKey = group.key;

      header.style.cssText = 'display:flex;align-items:center;gap:6px;padding:4px 4px;margin-top:4px;cursor:pointer;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:0.04em;user-select:none;';

      const caret = document.createElement('span');

      caret.textContent = isCollapsed ? '▸' : '▾';

      caret.style.cssText = 'flex-shrink:0;font-size:9px;width:9px;';

      const label = document.createElement('span');

      label.style.cssText = 'flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';

      label.textContent = group.label;

      const count = document.createElement('span');

      count.style.cssText = 'flex-shrink:0;opacity:0.7;';

      count.textContent = String(group.projects.length);

      header.appendChild(caret);

      header.appendChild(label);

      header.appendChild(count);

      header.onclick = () => {

        toggleFolderCollapsed(collapsed, group.key, STORAGE_KEY('projectFolderCollapsed'));

        loadProjects();

      };

      list!.appendChild(header);

      if (isCollapsed) return; // don't render members of a collapsed folder

    }

    // 0fed6a42 — within each folder group, nest subprojects under their parent.
    // flattenHierarchy places each subproject right after its parent and tags it
    // depth=1; a subproject whose parent lives in a different folder (or is scoped
    // out) falls back to depth=0 so nothing is dropped.
    flattenHierarchy<HierProject>(group.projects as unknown as HierProject[])
      .forEach(row => list!.appendChild(_makeProjectItem(row.project as any, row.depth)));

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



// d6b7da48 — one sidebar project row. Extracted verbatim from loadProjects so the
// same row markup/behavior renders whether the list is flat or grouped by folder.

function _makeProjectItem(p: any, depth: number = 0) {

  const div = document.createElement('div');

  const isSub = depth > 0;

  div.className = 'project-item' + (state.activeTab === p.id ? ' active' : '') + (isSub ? ' project-subitem' : '');

  div.dataset.projectId = p.id;

  // 0fed6a42 — indent subprojects so the parent→child relationship reads at a
  // glance in the sidebar. Depth is 0 (top-level) or 1 (subproject).
  div.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:4px;'
    + (isSub ? `padding-left:${14 + depth * 12}px;` : '');

  const nameSpan = document.createElement('span');

  nameSpan.style.cssText = 'flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';

  // Subprojects get a small tree connector glyph so the nesting is visible even
  // when the sidebar is narrow enough to clip the indentation.
  nameSpan.textContent = (isSub ? '└ ' : '') + p.name;

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

  return div;

}



// d6b7da48 — kebab-menu action: assign a project to a named folder (or clear it).
// Prompt-based like _setProjectIcon/_renameProject; membership is localStorage-only.

function _moveProjectToFolder(t: any) {

  const assignments = loadFolderAssignments(STORAGE_KEY('projectFolders'));

  const current = assignments[t.id] || '';

  const existing = knownFolderNames(state.projects as FolderProject[], assignments);

  const hint = existing.length ? `\n\nExisting folders: ${existing.join(', ')}` : '';

  const next = window.prompt(

    `Move "${t.project.name}" to a folder (leave blank for ${UNGROUPED_LABEL}).${hint}`,

    current,

  );

  if (next === null) return; // cancelled

  const updated = assignProjectToFolder(assignments, t.id, next);

  saveFolderAssignments(updated, STORAGE_KEY('projectFolders'));

  loadProjects();

  const folder = (next || '').trim();

  toast(folder ? `Moved to folder "${folder}"` : `Moved to ${UNGROUPED_LABEL}`);

}



// 0fed6a42 — kebab-menu action: make a project a one-level-deep subproject of
// another. Presents a numbered picker of *eligible* parents (eligibleParents
// enforces the same guards as the server: not self, parent must be top-level, a
// project that already has children can't be moved). Persists via the
// POST /projects/{id}/parent REST route, then re-renders the (now nested) list.
async function _makeSubproject(t: any) {

  const candidates = eligibleParents(state.projects as HierProject[], t.id);

  if (!candidates.length) {
    // Either this project already parents others, or there is no legal parent.
    if (state.projects.some((p: any) => p.parent_project_id === t.id)) {
      toast('This project has subprojects of its own — subprojects are one level deep.', true);
    } else {
      toast('No eligible parent project — add another top-level project first.', true);
    }
    return;
  }

  const lines = candidates.map((p, i) => `${i + 1}. ${p.name}`).join('\n');
  const raw = window.prompt(
    `Make "${t.project.name}" a subproject of which project?\n\n${lines}\n\nEnter a number (or leave blank to cancel):`,
    '',
  );
  if (raw === null) return; // cancelled
  const idx = parseInt(raw.trim(), 10) - 1;
  if (Number.isNaN(idx) || idx < 0 || idx >= candidates.length) {
    if (raw.trim() !== '') toast('Invalid selection', true);
    return;
  }
  const parent = candidates[idx];

  try {
    await api(`/projects/${t.id}/parent`, {
      method: 'POST', body: JSON.stringify({ parent_project_id: parent.id }),
    });
    // Reflect the change on the local row + reload so the nested render applies.
    t.project = { ...t.project, parent_project_id: parent.id };
    const proj = state.projects.find((p: any) => p.id === t.id);
    if (proj) proj.parent_project_id = parent.id;
    await loadProjects();
    toast(`"${t.project.name}" is now a subproject of "${parent.name}"`);
  } catch (e: any) {
    toast('Could not set parent: ' + e.message, true);
  }

}



// 0fed6a42 — kebab-menu action: detach a subproject back to top-level (clears
// parent_project_id). Same REST route with a null parent.
async function _detachSubproject(t: any) {

  try {
    await api(`/projects/${t.id}/parent`, {
      method: 'POST', body: JSON.stringify({ parent_project_id: null }),
    });
    t.project = { ...t.project, parent_project_id: null };
    const proj = state.projects.find((p: any) => p.id === t.id);
    if (proj) proj.parent_project_id = null;
    await loadProjects();
    toast(`"${t.project.name}" detached to top level`);
  } catch (e: any) {
    toast('Could not detach: ' + e.message, true);
  }

}



// function escapeHtml -- moved to dashboard-utils.js



// v0.5.1 — sessions render with relative timestamps so the user can

// see at a glance which workers are actually alive. SQLite stores

// timestamps in UTC without timezone markers; we treat them as UTC.

// function formatRelativeTime, sessionAgeMs, isLiveSession -- moved to dashboard-utils.js



function openTab(project: any) {

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



function closeTab(id: any) {

  state.tabs = state.tabs.filter(t => t.id !== id);

  const panel = state.panels[id];

  if (panel) {

    try { panel.ws && panel.ws.close(); } catch(e: any){}

    delete state.panels[id];

  }

  document.getElementById(`tab-body-${id}`)?.remove();

  saveTabs();

  renderTabs();

  if (state.activeTab === id) {

    const next = state.tabs[state.tabs.length - 1];

    state.activeTab = next ? next.id : null;

    if (next) activateTab(next.id);

    else document.getElementById('tab-bodies')!.innerHTML = '<div class="empty">no project open — pick one on the left</div>';

  }

  syncSidebarActiveProject();

}



function saveTabs() {

  try {

    localStorage.setItem(STORAGE_KEY(TABS_KEY), JSON.stringify(state.tabs.map(t => t.id)));

  } catch(e: any) {}

}



const TAB_OVERFLOW_THRESHOLD = 10;



function renderTabs() {

  const bar = document.getElementById('tabs');

  bar!.innerHTML = '';



  // v1.9.x — overflow: show first (N-1) tabs + ">>" button if 10+

  const overflow = state.tabs.length >= TAB_OVERFLOW_THRESHOLD;

  const visible = overflow ? state.tabs.slice(0, TAB_OVERFLOW_THRESHOLD - 1) : state.tabs;

  const hidden  = overflow ? state.tabs.slice(TAB_OVERFLOW_THRESHOLD - 1) : [];



  visible.forEach(t => bar!.appendChild(_makeTabEl(t)));



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

    bar!.appendChild(more);

  }

}



function _makeTabEl(t: any) {

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



function _openTabMenu(t: any, anchor: any) {

  // Close any existing menu.

  document.querySelectorAll('.tab-context-menu').forEach(m => m.remove());



  const menu = document.createElement('div');

  menu.className = 'tab-context-menu';

  menu.style.cssText = 'position:fixed;background:var(--surface-2);border:1px solid var(--border);border-radius:4px;z-index:1001;min-width:150px;box-shadow:0 4px 12px rgba(0,0,0,0.4);font-size:11px;font-family:var(--font-mono)';



  function menuItem(label: any, fn: any) {

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

  uuidDiv.style.cssText = 'padding:6px 12px;color:var(--muted);font-size:10px;border-bottom:1px solid var(--border);user-select:all;cursor:pointer';

  uuidDiv.title = 'Click to copy project ID';

  uuidDiv.textContent = t.id;

  // c1a2c6ee — click-to-copy the project id. Previously the id was select-only
  // (no copy affordance), and any click bubbled to the menu's click-outside
  // handler and closed the popup before a manual copy finished. stopPropagation
  // keeps the menu open; brief inline feedback confirms the copy.

  uuidDiv.addEventListener('click', (e) => {

    e.stopPropagation();

    const _restore = () => setTimeout(() => { uuidDiv.textContent = t.id; }, 1200);

    const _cb = (navigator as any).clipboard;

    if (_cb && _cb.writeText) {

      _cb.writeText(t.id).then(

        () => { uuidDiv.textContent = '✓ Copied project ID'; _restore(); },

        () => { uuidDiv.textContent = 'Copy failed — select manually'; _restore(); },

      );

    } else {

      uuidDiv.textContent = 'Copy failed — select manually'; _restore();

    }

  });

  menu.appendChild(uuidDiv);

  menuItem('\u270f Rename', () => _renameProject(t));

  menuItem('\ud83c\udfa8 Change icon\u2026', () => _setProjectIcon(t));

  menuItem('\ud83d\udcc1 Move to folder\u2026', () => _moveProjectToFolder(t));

  // 0fed6a42 \u2014 subproject hierarchy. Show "Make subproject of\u2026" for a top-level
  // project, and "Detach from parent" for one that already has a parent.
  if (t.project && t.project.parent_project_id) {
    menuItem('\u2934 Detach from parent', () => _detachSubproject(t));
  } else {
    menuItem('\ud83d\udd17 Make subproject of\u2026', () => _makeSubproject(t));
  }

  menuItem('\u2b07 Download DB', () => window.open('/admin/snapshot', '_blank'));

  menuItem('🗑 Delete project…', () => _deleteProject(t));



  const rect = anchor.getBoundingClientRect();

  menu.style.top = (rect.bottom + 4) + 'px';

  menu.style.left = rect.left + 'px';

  document.body.appendChild(menu);

  const dismiss = () => { menu.remove(); document.removeEventListener('click', dismiss); };

  setTimeout(() => document.addEventListener('click', dismiss), 0);

}



async function _setProjectIcon(t: any) {

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

  } catch (e: any) {

    toast('Update failed: ' + e.message, true);

  }

}



async function _renameProject(t: any) {

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

  } catch(e: any) { toast('Rename failed: ' + e.message, true); }

}



async function _deleteProject(t: any) {
  await new Promise<void>((resolve) => {
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
    box.querySelector('#del-proj-cancel')!.onclick = () => { overlay.remove(); resolve(); };
    overlay.onclick = e => { if (e.target === overlay) { overlay.remove(); resolve(); } };
    box.querySelector('#del-proj-confirm')!.onclick = async () => {
      overlay.remove();
      try {
        await api(`/projects/${t.id}`, { method: 'DELETE' });
        closeTab(t.id);
        state.projects = state.projects.filter(p => p.id !== t.id);
        await loadProjects();
        toast('Project deleted');
      } catch(e: any) {
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



function activateTab(id: any) {

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

  try { localStorage.setItem(STORAGE_KEY(ACTIVE_PROJECT_KEY), id); } catch(e: any) {}

  // Keep the sidebar dropdown in sync with whichever tab the user is on.

  const switcher = document.getElementById('project-switcher');

  if (switcher) switcher.value = id;

  // 73907f9e — re-init the Settings panel on project-tab switch. Its renderer has
  // a 30s TTL cache + a MutationObserver lifecycle, so switching to an already-built
  // project tab whose active vtab is Settings can show a blank panel (the observer
  // was disconnected and the cache marked fresh while the DOM was empty). Other
  // vtabs re-fetch on focus; Settings did not. Force a fresh render here.
  try {
    const _panel = getPanelState(id);
    let _activeVtab = _panel && _panel.activeVtab;
    if (!_activeVtab) { try { _activeVtab = localStorage.getItem('meridian_last_tab_' + id); } catch (_) {} }
    if (_activeVtab === 'settings' && typeof loadSettingsTab === 'function') {
      loadSettingsTab(id, { force: true });
    }
  } catch (_) {}

}



function buildTabBody(project: any) {

  const root = document.getElementById('tab-bodies');

  const empty = root!.querySelector(':scope > .empty');

  if (empty) empty.remove();



  const body = document.createElement('div');

  body.className = 'tab-body';

  body.id = `tab-body-${project.id}`;

  body.innerHTML = `

    <div class="vtab-strip" id="vtab-strip-${project.id}">

      <div class="vtab-group" data-vgroup="overview" style="display:flex;flex-direction:column;align-items:center;gap:2px;width:100%">

        <button class="vtab-group-header" data-vgroup-toggle="overview" title="Overview" aria-label="Overview group" aria-expanded="true" style="width:36px;height:20px;border:none;background:transparent;cursor:pointer;font-size:11px;line-height:20px;opacity:0.5;padding:0;border-radius:6px;display:flex;align-items:center;justify-content:center">📊</button>

        <div class="vtab-group-tabs" style="display:flex;flex-direction:column;align-items:center;gap:2px;width:100%">

          <button class="vtab-btn active" data-vtab="status" title="Status &amp; Sessions" aria-label="Status and sessions">📊</button>

          <button class="vtab-btn" data-vtab="live" title="Live — right-now view">⚡</button>

        </div>

      </div>

      <div class="vtab-group" data-vgroup="planning" style="display:flex;flex-direction:column;align-items:center;gap:2px;width:100%">

        <button class="vtab-group-header" data-vgroup-toggle="planning" title="Planning" aria-label="Planning group" aria-expanded="true" style="width:36px;height:20px;border:none;background:transparent;cursor:pointer;font-size:11px;line-height:20px;opacity:0.5;padding:0;border-radius:6px;display:flex;align-items:center;justify-content:center">🎯</button>

        <div class="vtab-group-tabs" style="display:flex;flex-direction:column;align-items:center;gap:2px;width:100%">

          <button class="vtab-btn" data-vtab="goal" title="Goal State">🎯</button>

          <button class="vtab-btn" data-vtab="insights" title="Insights — durable strategic understanding">💡</button>

          <button class="vtab-btn" data-vtab="blog" title="Blog — workspace posts (draft/published/archived)">✍️</button>

        </div>

      </div>

      <div class="vtab-group" data-vgroup="work" style="display:flex;flex-direction:column;align-items:center;gap:2px;width:100%">

        <button class="vtab-group-header" data-vgroup-toggle="work" title="Work" aria-label="Work group" aria-expanded="true" style="width:36px;height:20px;border:none;background:transparent;cursor:pointer;font-size:11px;line-height:20px;opacity:0.5;padding:0;border-radius:6px;display:flex;align-items:center;justify-content:center">👷</button>

        <div class="vtab-group-tabs" style="display:flex;flex-direction:column;align-items:center;gap:2px;width:100%">

          <button class="vtab-btn" data-vtab="queue" title="Work Queue">👷</button>

          <button class="vtab-btn" data-vtab="hitl" title="HITL — Human-in-the-Loop queue" style="position:relative">❓<span class="hitl-vtab-badge vtab-count-badge" data-pid="${project.id}" style="display:none;position:absolute;top:2px;right:2px;background:#f87171;color:#fff;font-size:8px;font-weight:700;padding:0 3px;border-radius:6px;line-height:14px;pointer-events:none">0</span></button>

          <button class="vtab-btn" data-vtab="team" title="Team — per-human activity">👥</button>

          <button class="vtab-btn" data-vtab="sessions" title="Sessions — executor session timeline (done / failed / stopped-ambiguously)">🕒</button>

        </div>

      </div>

      <div class="vtab-group" data-vgroup="content" style="display:flex;flex-direction:column;align-items:center;gap:2px;width:100%">

        <button class="vtab-group-header" data-vgroup-toggle="content" title="Content" aria-label="Content group" aria-expanded="true" style="width:36px;height:20px;border:none;background:transparent;cursor:pointer;font-size:11px;line-height:20px;opacity:0.5;padding:0;border-radius:6px;display:flex;align-items:center;justify-content:center">📁</button>

        <div class="vtab-group-tabs" style="display:flex;flex-direction:column;align-items:center;gap:2px;width:100%">

          ${(window.MERIDIAN_HOSTED && !(project.github_repo || project.repo)) ? '' : '<button class="vtab-btn" data-vtab="files" title="Files">📁</button>'}

          <button class="vtab-btn" data-vtab="notes" title="Notes — per-project wiki" style="position:relative">📝<span class="notes-vtab-badge vtab-count-badge muted" data-pid="${project.id}" style="display:none;position:absolute;top:2px;right:2px;background:var(--surface-3,#2a2f3a);color:var(--muted);font-size:8px;font-weight:700;padding:0 3px;border-radius:6px;line-height:14px;pointer-events:none">0</span></button>

          <button class="vtab-btn" data-vtab="devlog" title="Dev Log">📓</button>

          <button class="vtab-btn" data-vtab="documents" title="Documents — ingested docs &amp; structure">📄</button>

          <button class="vtab-btn" data-vtab="docs" title="MCP Tool Reference">📖</button>

          <button class="vtab-btn" data-vtab="codeintel" title="Code Intel — codebase index &amp; architecture" id="vtab-codeintel-${project.id}" style="display:none">🔍</button>

        </div>

      </div>

      <div class="vtab-group" data-vgroup="history" style="display:flex;flex-direction:column;align-items:center;gap:2px;width:100%">

        <button class="vtab-group-header" data-vgroup-toggle="history" title="History" aria-label="History group" aria-expanded="true" style="width:36px;height:20px;border:none;background:transparent;cursor:pointer;font-size:11px;line-height:20px;opacity:0.5;padding:0;border-radius:6px;display:flex;align-items:center;justify-content:center">📅</button>

        <div class="vtab-group-tabs" style="display:flex;flex-direction:column;align-items:center;gap:2px;width:100%">

          <button class="vtab-btn" data-vtab="timeline" title="Activity Timeline">📅</button>

          <button class="vtab-btn" data-vtab="rewind" title="Rewind — Last X days">↻</button>

          <button class="vtab-btn" data-vtab="settings" title="Notification Settings">⚙</button>

        </div>

      </div>

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

            <div class="live-section-label">In progress · by session</div>

            <div id="live-inprogress-by-session-${project.id}" class="live-inprogress-by-session">

              <div class="live-empty">Nothing in progress right now.</div>

            </div>

          </div>

          <hr class="live-divider">

          <div class="live-section">

            <div class="live-section-label" title="Conflict-free batches the orchestrator can fan out — successive waves run in sequence (mirrors get_parallelizable_groups)">Parallel waves</div>

            <div id="live-wave-progress-${project.id}" class="live-wave-progress">

              <div class="live-empty">No parallelizable work right now.</div>

            </div>

          </div>

          <hr class="live-divider">

          <div class="live-section">

            <details class="live-section-collapse" open>

              <summary class="live-section-label" style="cursor:pointer;list-style:none">Active sessions</summary>

              <div class="live-sessions" id="live-sessions-${project.id}">

                <div class="live-empty">No active sessions.</div>

              </div>

            </details>

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

            <!-- 42e9f7b5 — the "summaries" toggle is replaced by the Notes/Log/Archive tab bar below -->
            <input type="checkbox" id="notes-show-auto-${project.id}" style="display:none">
            <span data-notes-tab-active="notes" id="notes-active-tab-${project.id}" style="display:none">notes</span>

          </span>

        </div>

        <div class="notes-tabbar" style="display:flex;gap:6px;padding:6px 14px 0;border-bottom:1px solid var(--border)">
          <button data-notes-tab="notes" id="notes-tab-notes-${project.id}" class="notes-tab-btn" style="background:none;border:none;border-bottom:2px solid var(--accent);color:var(--text);font-size:10px;font-weight:600;padding:3px 6px;cursor:pointer">Notes</button>
          <button data-notes-tab="log" id="notes-tab-log-${project.id}" class="notes-tab-btn" style="background:none;border:none;border-bottom:2px solid transparent;color:var(--muted);font-size:10px;font-weight:600;padding:3px 6px;cursor:pointer">Log</button>
          <button data-notes-tab="archive" id="notes-tab-archive-${project.id}" class="notes-tab-btn" style="background:none;border:none;border-bottom:2px solid transparent;color:var(--muted);font-size:10px;font-weight:600;padding:3px 6px;cursor:pointer">Archive</button>
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

      <div class="drawer-panel" id="drawer-documents-${project.id}">

        <div class="drawer-header">

          <span>DOCUMENTS · ${escapeHtml(project.name)}</span>

        </div>

        <div style="flex:1;overflow-y:auto;padding:14px" id="documents-body-${project.id}">

          <div class="empty" style="color:var(--muted)">loading…</div>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-insights-${project.id}">

        <div class="drawer-header">

          <span>INSIGHTS · ${escapeHtml(project.name)}</span>

        </div>

        <div style="flex:1;overflow-y:auto;padding:14px" id="insights-body-${project.id}">

          <div class="empty" style="color:var(--muted)">loading…</div>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-sessions-${project.id}">

        <div class="drawer-header">

          <span>SESSIONS · ${escapeHtml(project.name)}</span>

        </div>

        <div style="flex:1;overflow-y:auto;padding:14px" id="sessions-body-${project.id}">

          <div class="empty" style="color:var(--muted)">loading…</div>

        </div>

      </div>

      <div class="drawer-panel" id="drawer-blog-${project.id}">

        <div class="drawer-header">

          <span>BLOG · workspace</span>

        </div>

        <div style="flex:1;overflow-y:auto;padding:14px" id="blog-body-${project.id}">

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

  root!.appendChild(body);



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

    // 2d3b8424 — wire the collapsible group headers; revealGroupForTab re-expands
    // a (possibly user-collapsed) group before we activate one of its tabs, so no
    // navigation path can land on a hidden button.
    const { revealGroupForTab } = wireVtabGroups(vtabStrip);

    vtabStrip.querySelectorAll('.vtab-btn').forEach(btn => {

      btn.onclick = () => {

        const vtab = btn.dataset.vtab;

        const p = state.panels[project.id];

        // Keep the clicked tab's group expanded so it stays visible/measurable.
        revealGroupForTab(vtab);

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

        if (vtab === 'documents') loadDocumentsTab(project.id);

        if (vtab === 'insights') loadInsightsTab(project.id);

        if (vtab === 'blog') loadBlogTab(project.id);

        if (vtab === 'sessions') loadSessionTimelineTab(project.id);

      };

    });

    // Restore last active vtab from localStorage
    try {
      const saved = localStorage.getItem('meridian_last_tab_' + project.id);
      if (saved) {
        revealGroupForTab(saved);
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
  if (decFormCancel) decFormCancel.onclick = () => { decForm!.style.display = 'none'; };
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
        decForm!.style.display = 'none';
        toast('decision pinned');
        loadPinnedDecisions(project.id);
      } catch(e: any) {
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
      if (el) el.addEventListener('keydown', (e: any) => { if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') doAddDecision(); });
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

      const activeVersions = new Set(items.filter((it: any) => activeStatuses.has(it.status)).map((it: any) => it.version));

      const scopeItems = items.filter((it: any) =>

        activeStatuses.has(it.status) || (it.version && activeVersions.has(it.version))

      );

      const doneCount = scopeItems.filter((i: any) => i.status === 'done' || i.status === 'skipped').length;

      const activeCount = scopeItems.filter((i: any) => activeStatuses.has(i.status)).length;

      const total = scopeItems.length;

      const pct = total > 0 ? Math.round((doneCount / total) * 100) : 0;

      const pctColor = doneCount === 0 ? 'var(--muted)' : doneCount === total ? 'var(--accent-green)' : '#fbbf24';

      const pendingItems = scopeItems.filter((i: any) => activeStatuses.has(i.status));
      const statusColors: Record<string, string> = { pending: 'var(--muted)', todo: 'var(--muted)', in_progress: '#fbbf24' };
      const itemsHtml = pendingItems.slice(0, 10).map((it: any) => {
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

    } catch(e: any) {

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

      const active = (sessions || []).filter((s: any) => s.status !== 'closed' && s.status !== 'archived');

      const opts = active.map((s: any) => `<option value="${escapeHtml(s.name)}">${escapeHtml(s.name)}</option>`).join('');

      sel.innerHTML = opts + '<option value="__custom__">Custom…</option>';

      _sprintSelectSyncers[project.id] = function(val: any) {

        if (!sel) return;

        const match = Array.from(sel.options).find((o: any) => o.value === val && o.value !== '__custom__');

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

      } catch(e: any) { console.error('Add sprint item failed:', e); }

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
        } catch(e: any) {
          if (appendStatus) appendStatus.textContent = `Error: ${escapeHtml(String(e))}`;
        } finally {
          appendBtn.disabled = false;
        }
      };
      appendText.addEventListener('keydown', (e: any) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') appendBtn.click();
      });
    }
  }

  // v1.5.x — Claude launch control panel (4 sections).

  wireClaudeLaunchPanel(project.id);

  document.getElementById(`goal-${project.id}`)!.addEventListener('blur', () => saveGoal(project.id));

  document.getElementById(`goal-north-star-${project.id}`)!.addEventListener('blur', () => saveNorthStar(project.id));

  document.getElementById(`goal-sprint-${project.id}`)!.addEventListener('blur', () => saveSprint(project.id));

  // v0.6.4 — dirty state: highlight textarea border when unsaved changes exist

  document.getElementById(`goal-${project.id}`)!.addEventListener('input', function() {

    const p = state.panels[project.id];

    this.classList.toggle('dirty', this.value !== (p._lastSaved || ''));

  });

  document.getElementById(`goal-north-star-${project.id}`)!.addEventListener('input', function() {

    const p = state.panels[project.id];

    this.classList.toggle('dirty', this.value !== (p._serverNorthStar || ''));

    autosizeGoalField(this);

  });

  document.getElementById(`goal-sprint-${project.id}`)!.addEventListener('input', function() {

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

const liveRefreshState: Record<string, any> = {}; // keyed by projectId



// e19c3ca2 — live parallelization: group the CURRENTLY in_progress sprint items by
// the session (actor) that owns each, color-coded per session, so concurrent
// execution is visible at a glance. Data is already fetched by refreshLiveTab; this
// only reshapes + paints it. Distinct from the historical Sessions timeline (1e1bd6b0).
function renderInProgressBySession(projectId: any, sprintItems: any[], sessions: any[]) {
  const root = document.getElementById(`live-inprogress-by-session-${projectId}`);
  if (!root) return;
  const sessById: Record<string, any> = {};
  for (const s of (sessions || [])) sessById[String(s.id)] = s;

  const inProgress = (sprintItems || []).filter((it: any) => String(it.status) === 'in_progress');
  if (!inProgress.length) {
    root.innerHTML = '<div class="live-empty">Nothing in progress right now.</div>';
    return;
  }

  const byActor: Record<string, any[]> = {};
  const unassigned: any[] = [];
  for (const it of inProgress) {
    const actor = it.actor ? String(it.actor) : '';
    if (actor) (byActor[actor] = byActor[actor] || []).push(it);
    else unassigned.push(it);
  }

  const _hue = (s: string) => { let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360; return h; };
  const _elapsed = (ts: any) => {
    if (!ts) return '';
    const t = Date.parse(String(ts).replace(' ', 'T'));
    if (isNaN(t)) return '';
    const mins = Math.max(0, Math.floor((Date.now() - t) / 60000));
    return mins < 60 ? `${mins}m` : `${Math.floor(mins / 60)}h${mins % 60}m`;
  };
  const _item = (it: any, color: string) => `<div class="live-ip-item" style="display:flex;gap:6px;align-items:center;margin-top:4px">
      <span style="width:5px;height:5px;border-radius:50%;background:${color};flex:none"></span>
      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px">${escapeHtml(String(it.nickname || it.title || it.id || ''))}</span>
      ${_elapsed(it.claimed_at) ? `<span style="font-size:9px;color:var(--muted);flex:none">${_elapsed(it.claimed_at)}</span>` : ''}
    </div>`;

  // Busiest session first so the most-active executor leads.
  const actors = Object.keys(byActor).sort((a, b) => byActor[b].length - byActor[a].length);
  let html = '';
  for (const actor of actors) {
    const sess = sessById[actor] || {};
    const color = `hsl(${_hue(actor)} 70% 55%)`;
    const name = sess.name || `${actor.slice(0, 8)}…`;
    const status = String(sess.status || 'unknown');
    html += `<div class="live-ip-owner" data-session="${escapeHtml(actor)}" style="border-left:3px solid ${color};padding:4px 0 4px 8px;margin-bottom:8px">
      <div style="display:flex;gap:6px;align-items:center;justify-content:space-between">
        <span style="font-size:11px;font-weight:600;color:var(--text)">${escapeHtml(String(name))}</span>
        <span style="font-size:9px;padding:0 5px;border-radius:3px;border:1px solid ${color};color:${color}">${status === 'active' ? 'ACTIVE' : escapeHtml(status.toUpperCase())} · ${byActor[actor].length}</span>
      </div>
      ${byActor[actor].map((it: any) => _item(it, color)).join('')}
    </div>`;
  }
  if (unassigned.length) {
    html += `<div class="live-ip-owner" style="border-left:3px solid var(--muted);padding:4px 0 4px 8px">
      <div style="font-size:10px;font-weight:600;color:var(--muted)">Unassigned · ${unassigned.length}</div>
      ${unassigned.map((it: any) => _item(it, 'var(--muted)')).join('')}
    </div>`;
  }
  root.innerHTML = html;
}


function scheduleLiveRefresh(projectId: any) {

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



function initLiveAutoRefresh(projectId: any) {

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



async function loadLiveTab(projectId: any) {

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

        addToggle!.textContent = '+ Expand';

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

          addArea!.style.display = 'none';

          addToggle!.textContent = '+ Expand';

        } catch (e: any) { toast('Failed: ' + e.message, true); }

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

        overlay.querySelector('#_new-sprint-cancel')!.onclick = close;

        overlay.onclick = (e) => { if (e.target === overlay) close(); };

        const submit = async () => {

          const name = (inp!.value || '').trim();

          if (!name) { errEl!.textContent = 'Sprint name is required'; errEl!.style.display = ''; return; }

          try {

            overlay.querySelector('#_new-sprint-submit')!.disabled = true;

            await api(`/projects/${projectId}/goal/sprint`, { method: 'POST', body: JSON.stringify({ sprint: name }) });

            toast(`Sprint set: ${name}`);

            close();

          } catch (e: any) { errEl!.textContent = e.message || 'Failed'; errEl!.style.display = ''; overlay.querySelector('#_new-sprint-submit')!.disabled = false; }

        };

        overlay.querySelector('#_new-sprint-submit')!.onclick = submit;

        inp!.addEventListener('keydown', (e: any) => { if (e.key === 'Enter') submit(); if (e.key === 'Escape') close(); });

        setTimeout(() => inp!.focus(), 50);

      };

    }



    panel.liveWired = true;

  }

  await refreshLiveTab(projectId);

  initLiveAutoRefresh(projectId);

}



async function refreshLiveTab(projectId: any) {

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

      // e3355ccb — live parallel-execution waves. Recomputes the server's
      // conflict-free batches from the same sprint-items payload (no extra fetch)
      // and repaints the wave-progress panel on the existing refresh cadence.
      renderWaveProgress(projectId, sprintItemsResult.value || []);

    } else {

      const sprintRoot = document.getElementById(`live-sprint-progress-${projectId}`);

      if (sprintRoot) {

        sprintRoot.innerHTML = renderProjectLoadError(projectId, 'Sprint progress unavailable', sprintItemsPath, sprintItemsResult.reason);

        wireProjectLoadRetry(sprintRoot, projectId);

      }

    }



    // e19c3ca2 — live parallelization: group the currently-in_progress sprint items
    // by the session that owns each (needs both the sprint items and the sessions).
    if (sprintItemsResult.status === 'fulfilled' && sessionsResult.status === 'fulfilled') {

      renderInProgressBySession(projectId, sprintItemsResult.value || [], sessionsResult.value || []);

    }



    if (sessionsResult.status === 'fulfilled' && tasksResult.status === 'fulfilled') {

      const worktrees = worktreesResult.status === 'fulfilled' ? (worktreesResult.value || []) : [];

      const sessions = sessionsResult.value || [];

      renderLiveSessions(projectId, sessions, tasksResult.value || [], worktrees);

      cacheMostRecentSession(projectId, sessions);

      // Sprint notes: load for the most recently active session.

      const activeSession = sessions.find((s: any) => s.status === 'active') || sessions[0];

      if (activeSession && activeSession.id) {

        loadSprintNotesPanel(projectId, activeSession.id).catch(() => {});

      }

    } else {

      const sessionsRoot = document.getElementById(`live-sessions-${projectId}`);

      if (sessionsRoot) {

        const liveError = sessionsResult.status === 'rejected' ? (sessionsResult as any).reason : (tasksResult as any).reason;

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

  } catch(e: any) { /* ignore — WS will retry on next event */ }

}



// function renderSprintProgress -- moved to dashboard-sprint.js



function wireSprintAddEnter(projectId: any, root: any) {

  /** Allow Enter in the sprint-add input to submit. */

  const inp = root.querySelector(`#sprint-add-input-${projectId}`);

  if (inp) inp.onkeydown = (e: any) => { if (e.key === 'Enter') addSprintItemFromInput(projectId); };

}



async function sprintAction(projectId: any, itemId: any, action: any) {

  /** POST to one of the sprint-item action endpoints. */

  try {

    await api(`/projects/${projectId}/sprint-items/${itemId}/${action}`,

      { method: 'POST', body: JSON.stringify({}) });

    toast(`Sprint item ${action}d`);

    await refreshLiveTab(projectId);

  } catch(e: any) { toast(`Failed: ${e.message}`, true); }

}


async function sprintArchive(projectId: any, itemId: any) {

  /** e62ce019 — permanently delete a backburner item decided against. */

  if (!confirm('Permanently delete this backburner item? This cannot be undone.')) return;

  try {

    const r = await fetch(`/projects/${projectId}/sprint-items/${itemId}`, { method: 'DELETE' });

    if (!r.ok && r.status !== 204) throw new Error(`${r.status}`);

    toast('Backburner item deleted');

    await refreshLiveTab(projectId);

  } catch(e: any) { toast(`Delete failed: ${e.message}`, true); }

}


function filterBackburner(projectId: any, value: any) {

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



async function sprintPushPrompt(projectId: any, itemId: any) {

  /** Prompt for a target version then push the item. */

  const toVersion = window.prompt('Push to version (e.g. v2.0):');

  if (!toVersion) return;

  try {

    await api(`/projects/${projectId}/sprint-items/${itemId}/push`,

      { method: 'POST', body: JSON.stringify({ to_version: toVersion }) });

    toast('Sprint item pushed to ' + toVersion);

    await refreshLiveTab(projectId);

  } catch(e: any) { toast(`Push failed: ${e.message}`, true); }

}



async function sprintFeedback(projectId: any, itemId: any, thumb: any, currentThumb: any, event: any) {

  event && event.stopPropagation();

  const newThumb = currentThumb === thumb ? null : thumb;

  try {

    await api(`/projects/${projectId}/sprint-items/${itemId}`,

      { method: 'PATCH', body: JSON.stringify({ feedback_thumb: newThumb }) });

    await refreshLiveTab(projectId);

  } catch(e: any) { toast('Feedback failed: ' + e.message, true); }

}



async function sprintFeedbackNote(projectId: any, itemId: any, note: any) {

  if (!note || !note.trim()) return;

  try {

    await api(`/projects/${projectId}/sprint-items/${itemId}`,

      { method: 'PATCH', body: JSON.stringify({ feedback_note: note.trim() }) });

    await refreshLiveTab(projectId);

  } catch(e: any) { toast('Note save failed: ' + e.message, true); }

}



async function sprintItemEdit(projectId: any, itemId: any) {

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

    } catch(e: any) { toast(`Save failed: ${e.message}`, true); cancel(); }

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



async function sprintItemNotesEdit(projectId: any, itemId: any) {

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

    if (titleSpan) titleSpan.parentNode!.insertBefore(textarea, titleSpan.nextSibling);

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

    } catch(e: any) { toast(`Save failed: ${e.message}`, true); cancel(); }

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


async function sprintItemResourcesEdit(projectId: any, itemId: any, rawJson: any) {

  /** Inline-edit the touches_resources field of a sprint item. */

  const row = document.querySelector(`.sprint-item-row[data-item="${CSS.escape(itemId)}"]`);

  if (!row) return;

  if (row.querySelector('.sprint-resources-textarea')) return;

  let current = [];

  try { current = JSON.parse(rawJson || '[]'); } catch { current = []; }

  const existingEl = row.querySelector('.sprint-item-resources');

  const textarea = document.createElement('textarea');

  textarea.className = 'sprint-resources-textarea';

  textarea.value = current.join('\n');

  textarea.placeholder = 'One resource per line, e.g.\nfile:meridian/db/__init__.py\nnote:my-note\ndecision:abc123';

  textarea.style.cssText = 'width:100%;min-height:56px;background:var(--surface-1);border:1px solid var(--accent);border-radius:3px;padding:3px 5px;color:var(--text);font-size:10px;font-family:var(--font-mono);line-height:1.4;resize:vertical;box-sizing:border-box;margin-top:3px';

  if (existingEl) {

    existingEl.replaceWith(textarea);

  } else {

    const notesEl = row.querySelector('.sprint-item-notes');

    const anchor = notesEl || row.querySelector('.sprint-item-title');

    if (anchor) anchor.parentNode!.insertBefore(textarea, anchor.nextSibling);

    else row.appendChild(textarea);

  }

  textarea.focus();

  const save = async () => {

    const lines = textarea.value.split('\n').map(s => s.trim()).filter(Boolean);

    try {

      await api(`/projects/${projectId}/sprint-items/${itemId}`, {

        method: 'PATCH',

        body: JSON.stringify({ touches_resources: lines.length ? lines : null }),

      });

      await refreshLiveTab(projectId);

    } catch(e: any) { toast(`Save failed: ${e.message}`, true); cancel(); }

  };

  const cancel = () => {

    if (existingEl) textarea.replaceWith(existingEl);

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


async function resourceChipClick(projectId: any, resourceId: any) {

  /** Show a small popover listing sprint items that declare this resource. */

  const existing = document.getElementById('resource-chip-popover');

  if (existing) existing.remove();

  let items = [];

  try {

    items = await api(`/projects/${projectId}/resources/sprint-items?resource=${encodeURIComponent(resourceId)}`);

  } catch(e: any) {

    toast(`Lookup failed: ${e.message}`, true);

    return;

  }

  const pop = document.createElement('div');

  pop.id = 'resource-chip-popover';

  pop.style.cssText = 'position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:var(--surface-0,#1a1a1a);border:1px solid var(--border);border-radius:6px;padding:12px 14px;z-index:9999;min-width:280px;max-width:480px;max-height:320px;overflow-y:auto;box-shadow:0 8px 32px rgba(0,0,0,.6)';

  const esc = (s: any) => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

  pop.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <span style="font-size:11px;font-weight:600;color:var(--text)">Sprint items touching <code style="font-size:10px">${esc(resourceId)}</code></span>
      <button onclick="document.getElementById('resource-chip-popover')!.remove()" style="background:none;border:none;cursor:pointer;color:var(--muted);font-size:14px;line-height:1;padding:0 2px">✕</button>
    </div>
    ${items.length === 0
      ? `<div style="font-size:10px;color:var(--muted)">No sprint items reference this resource.</div>`
      : items.map((it: any) => `<div style="font-size:10px;padding:4px 0;border-top:1px solid var(--border);color:var(--text)"><span style="color:var(--muted);margin-right:4px">${esc(it.status)}</span>${esc(it.title)}</div>`).join('')
    }
  `;

  document.body.appendChild(pop);

  const close = (e: any) => { if (!pop.contains(e.target)) { pop.remove(); document.removeEventListener('click', close); } };

  setTimeout(() => document.addEventListener('click', close), 50);

}


async function loadSprintNotesPanel(projectId: any, sessionId: any) {

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

    container.innerHTML = notes.map((n: any) => `

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



async function addSprintItemFromInput(projectId: any) {

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

  } catch(e: any) { toast('Add failed: ' + e.message, true); }

}



function cacheMostRecentSession(projectId: any, sessions: any) {

  /** Pick the most recent active session id for "add task" attribution. */

  const panel = state.panels[projectId];

  if (!panel) return;

  const sorted = sortSessionsMostRecentFirst(sessions);

  const top = sorted.find((s: any) => isLiveSession(s)) || sorted.find((s: any) => s.status !== 'closed') || sorted[0];

  if (top) panel.liveLastSessionId = top.id;

}



function renderLiveSessions(projectId: any, sessions: any, tasks: any, worktrees: any) {

  const root = document.getElementById(`live-sessions-${projectId}`);

  if (!root) return;

  // Build a map of session_id → active worktree branches
  const worktreeMap = new Map();
  (worktrees || []).forEach((wt: any) => {
    if (!worktreeMap.has(wt.session_id)) worktreeMap.set(wt.session_id, []);
    worktreeMap.get(wt.session_id).push(wt.branch);
  });

  const claimMap = new Map();
  const taskMap = new Map();

  tasks.forEach((t: any) => {

    if (t.claimed_by && (t.status === 'pending' || t.status === 'in_progress')) {

      claimMap.set(t.claimed_by, t);

    }

    const sid = t.session_id || t.claimed_by;
    if (sid) {
      if (!taskMap.has(sid)) taskMap.set(sid, []);
      taskMap.get(sid).push(t);
    }

  });
  taskMap.forEach(rows => rows.sort((a: any, b: any) => String(b.created_at || '').localeCompare(String(a.created_at || ''))));

  const rows = sessions

    .map((s: any) => {

      const ageMs = sessionAgeMs(s);

      return { s, ageMs };

    })

    .filter(({ ageMs }: any) => ageMs > 0 && ageMs <= 24 * 3600 * 1000)

    .sort((a: any, b: any) => a.ageMs - b.ageMs);

  // dc234d4e — hide the Active sessions panel entirely when nothing is live in
  // the last 10 min, instead of showing an empty "No active sessions" block
  // (clutter on camera). It reappears on the next refresh once a session is live.
  const LIVE_PRESENCE_MS = 10 * 60 * 1000;
  const liveSection = root.closest('.live-section');
  const sectionDivider = liveSection ? liveSection.nextElementSibling : null;
  const anyLivePresence = rows.some(({ ageMs }: any) => ageMs <= LIVE_PRESENCE_MS);
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

  root.innerHTML = rows.map(({ s, ageMs }: any) => {

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
    const taskRows = sessionTasks.slice(0, 3).map((t: any) =>
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

    const worktreeBadges = sessionWorktrees.map((branch: any) =>
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

async function endLiveSession(projectId: any, sessionId: any) {
  if (!sessionId) return;
  try {
    await api(`/sessions/${sessionId}`, {
      method: 'PATCH',
      body: JSON.stringify({ status: 'idle' }),
    });
    toast('Session marked idle');
    await refreshLiveTab(projectId);
  } catch(e: any) {
    toast(`End session failed: ${e.message}`, true);
  }
}

function openTimelineForSession(projectId: any, sessionId: any) {
  const panel = getPanelState(projectId);
  panel.timelineSessionFilter = sessionId || null;
  try { localStorage.setItem('meridian_tl_view_' + projectId, 'tasks'); } catch(_) {}
  const btn = document.querySelector(`#vtab-strip-${projectId} .vtab-btn[data-vtab="timeline"]`);
  if (btn) btn.click();
  else loadTimeline(projectId);
}



function renderLiveQueue(projectId: any, tasks: any) {

  const root = document.getElementById(`live-queue-${projectId}`);

  if (!root) return;

  const live = tasks.filter((t: any) => t.status === 'pending' || t.status === 'in_progress');

  if (!live.length) {

    root.innerHTML = '<div class="live-empty">Queue is empty. Add a task above.</div>';

    return;

  }

  live.sort((a: any, b: any) => {

    if (a.status !== b.status) return a.status === 'in_progress' ? -1 : 1;

    return (b.created_at || '').localeCompare(a.created_at || '');

  });

  root.innerHTML = live.map((t: any) => {

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



async function addLiveTask(projectId: any, description: any) {

  /** POST /tasks with the most recent active session as attribution. */

  const panel = state.panels[projectId];

  const sessionId = panel && panel.liveLastSessionId;

  if (!sessionId) {

    // Try to discover one synchronously.

    try {

      const sessions = await api(`/projects/${projectId}/sessions`);

      cacheMostRecentSession(projectId, sessions || []);

    } catch(e: any) {}

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

  } catch(e: any) {

    toast('add task failed: ' + e.message, true);

    return false;

  }

}



async function cancelLiveTask(projectId: any, taskId: any) {

  /** PATCH /tasks/{id} → status=done. WebSocket broadcast triggers refresh. */

  try {

    await api(`/tasks/${taskId}`, {

      method: 'PATCH',

      body: JSON.stringify({ status: 'done' }),

    });

    toast('task closed');

    await refreshLiveTab(projectId);

  } catch(e: any) {

    toast('cancel failed: ' + e.message, true);

  }

}



// v1.5.x — Claude launch control panel. Wires the 4 sections:

function showCopyPreview(title: any, content: any) {

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

      await navigator.clipboard.writeText(ta!.value);

    } catch(_) {

      ta!.select();

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

function wireClaudeLaunchPanel(projectId: any) {

  const PROJECT_QUOTE = projectId.replace(/"/g, '\\"');
  const sequentialKey = `meridian.sequentialMode.${projectId}`;

  function normalizeTouchesFile(path: any) {
    return String(path || '').trim().replace(/\\/g, '/').replace(/^\.\//, '');
  }

  function parseTouchesFiles(raw: any) {
    if (!raw) return [];
    if (Array.isArray(raw)) return raw.map(normalizeTouchesFile).filter(Boolean);
    const text = String(raw).trim();
    if (!text) return [];
    try {
      const parsed = JSON.parse(text);
      if (Array.isArray(parsed)) return parsed.map(normalizeTouchesFile).filter(Boolean);
      return [normalizeTouchesFile(parsed)].filter(Boolean);
    } catch(e: any) {
      return text.split(',').map(normalizeTouchesFile).filter(Boolean);
    }
  }

  function findTouchesFilesConflicts(items: any) {
    const active = (items || []).filter((it: any) => ['pending', 'todo', 'in_progress'].includes(it.status || 'pending'));
    const byFile = new Map();
    active.forEach((item: any) => {
      parseTouchesFiles(item.touches_files).forEach((file) => {
        const key = file.toLowerCase();
        const list = byFile.get(key) || [];
        list.push({ file, item });
        byFile.set(key, list);
      });
    });
    return Array.from(byFile.values())
      .filter(list => list.length > 1 && list.some((entry: any) => entry.item.status === 'in_progress'))
      .flat();
  }

  function applySequentialMode(text: any) {
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
    } catch(e: any) {
      return true;
    }
  }

  const sequentialToggle = document.getElementById(`sequential-mode-${projectId}`);
  if (sequentialToggle) {
    try { sequentialToggle.checked = localStorage.getItem(sequentialKey) === '1'; } catch(e: any) {}
    sequentialToggle.onchange = () => {
      try { localStorage.setItem(sequentialKey, sequentialToggle.checked ? '1' : '0'); } catch(e: any) {}
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

    } catch(e: any) { toast('handoff failed: ' + e.message, true); }

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

    } catch(e: any) { toast('start worker failed: ' + e.message, true); }

  };

  const copyWorkerBtn = document.getElementById(`copy-worker-${projectId}`);

  if (copyWorkerBtn) copyWorkerBtn.onclick = async () => {

    const xmlEl = document.getElementById(`worker-xml-${projectId}`);

    const text = xmlEl ? xmlEl.textContent : '';

    if (!text) { toast('nothing to copy', true); return; }

    try {

      await navigator.clipboard.writeText(text);

      toast('worker context copied');

    } catch(e: any) { toast('copy failed: ' + e.message, true); }

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

    } catch(e: any) { toast('handoff failed: ' + e.message, true); }

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

    } catch(e: any) { toast('copy context failed: ' + e.message, true); }

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

    } catch(e: any) { toast('regenerate failed: ' + e.message, true); }

    finally {

      regenBtn.disabled = false;

      regenBtn.textContent = orig;

    }

  };

}



function stampHandoffTs(projectId: any, when: any) {

  const tsEl = document.getElementById(`handoff-ts-${projectId}`);

  if (!tsEl) return;

  const iso = when.toISOString().replace('T', ' ').slice(0, 19);

  tsEl.textContent = 'Last generated: ' + formatRelativeTime(iso);

}



function populateSessionDropdown(projectId: any, sessions: any) {

  /** v1.5.x — fill the "Continue session" dropdown with the last 5 sessions

   * (newest first by last_seen). Each option label: "{name} — {age} ago". */

  const sel = document.getElementById(`continue-session-${projectId}`);

  if (!sel) return;

  const sorted = sortSessionsMostRecentFirst(sessions).slice(0, 5);

  if (!sorted.length) {

    sel.innerHTML = '<option value="">(no sessions yet)</option>';

    return;

  }

  const prev = sel.value;

  sel.innerHTML = sorted.map((s: any) => {

    const label = `${s.name} — ${formatRelativeTime(s.last_seen)}`;

    return `<option value="${escapeHtml(s.name)}">${escapeHtml(label)}</option>`;

  }).join('');

  if (prev && sorted.some((s: any) => s.name === prev)) sel.value = prev;

}



// v1.1.1 — Activity Timeline. Load /timeline, render task history

// per session, paint task pills positioned on a shared time axis.

async function loadTimeline(projectId: any) {

  const wrap = document.getElementById(`timeline-wrap-${projectId}`);

  if (!wrap) return;

  wrap.innerHTML = `<div class="timeline-empty">loading…</div>`;

  let data;

  try {

    data = await api(`/projects/${projectId}/timeline`);

  } catch (e: any) {

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



function _renderTimelineLog(projectId: any, data: any) {

  /** Fallback text log when vis-timeline isn't available. */

  const wrap = document.getElementById(`timeline-wrap-${projectId}`);

  if (!wrap) return;

  const { tasks = [], goal_events = [] } = data || {};

  const isAbs = !!(state.panels[projectId] && state.panels[projectId]._timelineAbsolute);

  const fmtTs = (ts: any) => {

    if (!ts) return '';

    const iso = ts.includes('T') ? ts : ts.replace(' ', 'T') + 'Z';

    return isAbs ? new Date(iso).toISOString().replace('T',' ').slice(0,16) : formatRelativeTime(ts);

  };

  const events: any[] = [];

  tasks.forEach((t: any) => {

    const icon = ({ done: '✅', failed: '❌' } as any)[t.status] || '•';

    events.push({ ts: t.created_at, actor: t.session_name || '(unknown)', desc: `${icon} ${(t.description || '').slice(0, 100)}` });

  });

  const goalByField = new Map();

  goal_events.forEach((g: any) => {

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

const _CATEGORY_LABELS: Record<string, string> = {

  goal: 'Goal Tools', task: 'Task & Sprint Tools', session: 'Session Tools',

  hitl: 'HITL Tools', notes: 'Notes Tools', decisions: 'Decision Tools', project: 'Project Tools',

};



async function loadDocsTab(projectId: any) {

  const body = document.getElementById(`docs-body-${projectId}`);

  if (!body) return;

  if (body.dataset.loaded) return;

  body.dataset.loaded = '1';

  try {

    const tools = await api('/tools');

    if (!tools || !tools.length) { body.innerHTML = '<div class="empty" style="color:var(--muted)">No tools returned.</div>'; return; }

    // Render by category as collapsible <details> sections (70ac52e4): 90+
    // tools no longer render as one flat wall. Grouping/section markup lives in

    // dashboard-mcp (_renderToolSections) so it is unit-testable; the 'other'

    // catch-all there keeps any uncategorized tool from being dropped.

    const _catLabels = { ..._CATEGORY_LABELS, other: 'Other' };

    const html = _renderToolSections(tools, _TOOL_CATEGORIES, _catLabels);

    const _toolSearch = `<div style="position:sticky;top:0;background:var(--surface-1,#10131a);padding:0 0 8px;margin-bottom:6px;z-index:2"><input type="text" id="docs-search-${projectId}" placeholder="Search tools by name or description…" style="width:100%;box-sizing:border-box;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:12px;font-family:var(--font-mono);padding:5px 9px;outline:none"></div>`;

    body.innerHTML = _toolSearch + html;

    _wireTabSearch(`docs-search-${projectId}`, `docs-body-${projectId}`, '.tool-entry');

  } catch (e: any) {

    body.innerHTML = `<div style="color:var(--error)">Failed to load tools: ${escapeHtml(String(e))}</div>`;

  }

}



// Code Intel tab — show when tunnel:code OR tunnel:outputs socket is live.
// e2688fc1 — extended to also surface the meridian-outputs indexing state.
// Called once per project panel after vtab strip is wired.
async function _initCodeIntelTabVisibility(projectId: any) {
  if (!window.MERIDIAN_HOSTED) return;
  try {
    const data = await api('/tunnel/plugins');
    const btn = document.getElementById(`vtab-codeintel-${projectId}`);
    if (!btn) return;
    const isActive = !!(data && data.active && (data.active.code || data.active.outputs));
    btn.style.display = isActive ? '' : 'none';
  } catch (_) {}
}

// Code-intel projects are keyed by a slug derived from the repo's root path:
// drive-colon and path separators (\\ / :) collapse to single dashes, e.g.
// C:\Users\13144\Documents\Meridian\repository -> C-Users-13144-Documents-Meridian-repository.
// The index_status / get_architecture tools take this `project` slug, NOT a raw repo_path.
function _repoPathToProject(repoPath: any) {
  return String(repoPath || '').replace(/[\\/:]+/g, '-').replace(/^-+|-+$/g, '');
}

// Render get_architecture output as visualizations (0aca014f). Defensive by
// design: each section renders only when its field is present and well-shaped,
// so a schema mismatch degrades to a raw view instead of breaking the tab.
// Returns { html, charts:[{id,config}] }; charts are instantiated by the caller
// once their <canvas> elements are in the DOM. Schema (codebase-memory-mcp):
//   node_labels[{label,count}] edge_types[{type,count}] hotspots[{name,fan_in}]
//   packages[{name,node_count}] layers[{name,layer}] boundaries[{from,to,call_count}]
function _codeArchSection(archText: any) {
  const rawPre = (t: any) => `<pre style="font-size:10px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:10px;white-space:pre-wrap;word-break:break-all;color:var(--text);margin:0;line-height:1.5">${escapeHtml(t || '(no architecture returned)')}</pre>`;
  let arch = null;
  try { arch = JSON.parse(archText); } catch (_) { arch = null; }
  // Not JSON (older server returns formatted text) → keep the honest raw view.
  if (!arch || typeof arch !== 'object') return { html: rawPre(archText), charts: [] };

  const arr = (v: any) => Array.isArray(v) ? v : [];
  const num = (v: any) => (typeof v === 'number' ? v : parseFloat(v));
  const fin = (v: any) => Number.isFinite(num(v));
  const charts = [];
  let html = '';

  // (1) node_labels → bar chart
  const nodes = arr(arch.node_labels).filter((d) => d && d.label != null && fin(d.count));
  if (nodes.length) {
    html += `<div style="margin-bottom:14px"><div style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Node types</div><canvas id="ci-nodes" height="120"></canvas></div>`;
    charts.push({ id: 'ci-nodes', config: {
      type: 'bar',
      data: { labels: nodes.map((d) => String(d.label)), datasets: [{ data: nodes.map((d) => num(d.count)), backgroundColor: 'rgba(96,165,250,0.7)', borderRadius: 2 }] },
      options: { responsive: true, plugins: { legend: { display: false } }, scales: {
        x: { ticks: { color: '#9ca3af', font: { size: 9 }, maxRotation: 45 }, grid: { color: '#1f2937' } },
        y: { beginAtZero: true, ticks: { color: '#9ca3af', font: { size: 9 } }, grid: { color: '#1f2937' } } } },
    } });
  }

  // (2) edge_types → donut of top 6
  const edges = arr(arch.edge_types).filter((d) => d && d.type != null && fin(d.count)).sort((a, b) => num(b.count) - num(a.count)).slice(0, 6);
  if (edges.length) {
    const palette = ['#60a5fa', '#34d399', '#fbbf24', '#f87171', '#a78bfa', '#f472b6'];
    html += `<div style="margin-bottom:14px"><div style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Top edge types</div><div style="max-width:230px;margin:0 auto"><canvas id="ci-edges" height="200"></canvas></div></div>`;
    charts.push({ id: 'ci-edges', config: {
      type: 'doughnut',
      data: { labels: edges.map((d) => String(d.type)), datasets: [{ data: edges.map((d) => num(d.count)), backgroundColor: palette, borderWidth: 0 }] },
      options: { responsive: true, plugins: { legend: { position: 'right', labels: { color: '#9ca3af', font: { size: 9 }, boxWidth: 10 } } } },
    } });
  }

  // (3) hotspots → ranked list with fan-in bars
  const hot = arr(arch.hotspots).filter((d) => d && d.name != null && fin(d.fan_in)).sort((a, b) => num(b.fan_in) - num(a.fan_in)).slice(0, 10);
  if (hot.length) {
    const maxFan = Math.max(...hot.map((d) => num(d.fan_in)), 1);
    html += `<div style="margin-bottom:14px"><div style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">Hotspots (fan-in)</div>`;
    for (const h of hot) {
      const pct = Math.round((num(h.fan_in) / maxFan) * 100);
      html += `<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
        <div style="flex:1;min-width:0;font-size:10px;color:var(--text);font-family:var(--font-mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${escapeHtml(String(h.name))}">${escapeHtml(String(h.name))}</div>
        <div style="flex:1;background:var(--surface-1);border-radius:3px;height:10px;overflow:hidden"><div style="width:${pct}%;height:100%;background:var(--accent)"></div></div>
        <div style="width:30px;text-align:right;font-size:10px;color:var(--muted)">${num(h.fan_in)}</div>
      </div>`;
    }
    html += `</div>`;
  }

  // (4) packages → table
  const pkgs = arr(arch.packages).filter((d) => d && d.name != null && fin(d.node_count)).sort((a, b) => num(b.node_count) - num(a.node_count)).slice(0, 15);
  if (pkgs.length) {
    html += `<div style="margin-bottom:14px"><div style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">Packages</div><table style="width:100%;border-collapse:collapse;font-size:10px">`;
    for (const pk of pkgs) {
      html += `<tr style="border-bottom:1px solid var(--border)"><td style="padding:3px 6px;color:var(--text);font-family:var(--font-mono)">${escapeHtml(String(pk.name))}</td><td style="padding:3px 6px;text-align:right;color:var(--muted)">${num(pk.node_count)}</td></tr>`;
    }
    html += `</table></div>`;
  }

  // (5) layers → CSS flex diagram (top layer first)
  const layers = arr(arch.layers).filter((d) => d && d.name != null);
  if (layers.length) {
    const sorted = [...layers].sort((a, b) => (a.layer > b.layer ? -1 : a.layer < b.layer ? 1 : 0));
    html += `<div style="margin-bottom:14px"><div style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px">Layers</div><div style="display:flex;flex-direction:column;gap:4px">`;
    for (const ly of sorted) {
      html += `<div style="padding:6px 10px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;font-size:10px;color:var(--text);display:flex;justify-content:space-between">
        <span style="font-family:var(--font-mono)">${escapeHtml(String(ly.name))}</span>
        <span style="color:var(--muted)">layer ${escapeHtml(String(ly.layer))}</span>
      </div>`;
    }
    html += `</div></div>`;
  }

  // Parsed JSON but no expected field matched → fall back to raw so nothing is lost.
  if (!html) return { html: rawPre(archText), charts: [] };
  // A collapsible raw view alongside the visualizations.
  html += `<details style="margin-top:4px"><summary style="cursor:pointer;list-style:none;font-size:10px;color:var(--accent)">&#9656; raw JSON</summary>${rawPre(archText)}</details>`;
  return { html, charts };
}


// 65742e42 — normalize a codebase__query_graph result (package-edge Cypher) into
// a list of {source, target, value} edges. Defensive: tolerates row arrays
// ([a, b, count]) and a few common object shapes; bad input → []. Pure + tested.
function _normalizeGraphEdges(queryResult: any) {
  let rows = [];
  try {
    const txt = ((queryResult && queryResult.content) || []).map((c: any) => c.text || '').join('').trim();
    if (!txt) return [];
    const obj = JSON.parse(txt);
    rows = Array.isArray(obj) ? obj : (obj.rows || obj.data || obj.results || obj.records || []);
  } catch (_) { return []; }
  const edges = [];
  for (const row of (Array.isArray(rows) ? rows : [])) {
    if (Array.isArray(row) && row.length >= 2) {
      edges.push({ source: String(row[0]), target: String(row[1]), value: Number(row[2]) || 1 });
    } else if (row && typeof row === 'object') {
      const src = row.source ?? row.from ?? row['a.package'] ?? row.a;
      const tgt = row.target ?? row.to ?? row['b.package'] ?? row.b;
      const val = row.value ?? row.count ?? row['count(r)'] ?? row.weight ?? 1;
      if (src != null && tgt != null) {
        edges.push({ source: String(src), target: String(tgt), value: Number(val) || 1 });
      }
    }
  }
  return edges;
}
if (typeof window !== 'undefined') window._normalizeGraphEdges = _normalizeGraphEdges;

// 20d17e08 — packages from get_architecture can arrive WITHOUT a `layer` field while
// the arch payload carries a separate `layers` list, so the force-graph colours every
// package as a single "other" bucket. When the two can be joined, stamp each package's
// layer. Pure + defensive: assigns a layer only when the data actually supports a join
// (a layer's explicit member list, or an exact/prefix name match); otherwise the
// package is returned unchanged (no regression, honest "other"). Never mutates input.
// NB: get_architecture is proxied from an external code-intel MCP, so if that server
// emits neither package.layer nor a joinable membership/name relationship, this is a
// safe no-op and the root fix belongs upstream in that MCP.
function _resolvePackageLayers(packages: any, layers: any): any[] {
  const pkgs = Array.isArray(packages) ? packages : [];
  const lys = Array.isArray(layers) ? layers : [];
  if (!pkgs.length || !lys.length) return pkgs;
  // If any package already carries a layer, trust the upstream data as-is.
  if (pkgs.some((p: any) => p && p.layer != null)) return pkgs;

  const memberToLayer: Record<string, any> = {};
  const layerNames: Array<{ name: string; layer: any }> = [];
  for (const ly of lys) {
    if (!ly || ly.name == null) continue;
    const rank = ly.layer != null ? ly.layer : ly.name;
    layerNames.push({ name: String(ly.name), layer: rank });
    const members = ly.members || ly.packages || ly.package_names || ly.modules;
    if (Array.isArray(members)) {
      for (const m of members) {
        const key = String(typeof m === 'string' ? m : (m && m.name) || '').trim();
        if (key) memberToLayer[key] = rank;
      }
    }
  }
  // Longest layer name first so the most specific prefix wins.
  layerNames.sort((a, b) => b.name.length - a.name.length);

  const resolve = (name: string): any => {
    if (name in memberToLayer) return memberToLayer[name];
    for (const ly of layerNames) {
      if (name === ly.name || name.startsWith(ly.name + '.') || name.startsWith(ly.name + '/')) {
        return ly.layer;
      }
    }
    return null;
  };

  let matched = false;
  const out = pkgs.map((p: any) => {
    if (!p || p.name == null) return p;
    const lyr = resolve(String(p.name));
    if (lyr == null) return p;
    matched = true;
    return { ...p, layer: lyr };
  });
  return matched ? out : pkgs;
}
if (typeof window !== 'undefined') window._resolvePackageLayers = _resolvePackageLayers;

// 65742e42 — build an ECharts force-graph option from architecture packages +
// cross-package edges. Nodes are packages (sized by node_count, colored by
// layer); the 'hotspots' view instead sizes by connection degree to surface the
// most-connected packages. Returns null when there are no packages. Pure + tested.
function _buildCodebaseForceGraph(packages: any, edges: any, view: any) {
  const pkgs = (packages || []).filter((p: any) => p && p.name != null);
  if (!pkgs.length) return null;
  const layers = [...new Set(pkgs.map((p: any) => String(p.layer != null ? p.layer : 'other')))];
  const palette = ['#60a5fa', '#34d399', '#fbbf24', '#f87171', '#a78bfa', '#f472b6', '#22d3ee'];
  const layerColor: Record<string, string> = {};
  layers.forEach((l: any, i: any) => { layerColor[l] = palette[i % palette.length]; });
  const degree: Record<string, number> = {};
  (edges || []).forEach((e: any) => {
    if (!e) return;
    degree[e.source] = (degree[e.source] || 0) + (Number(e.value) || 1);
    degree[e.target] = (degree[e.target] || 0) + (Number(e.value) || 1);
  });
  const metric = (p: any) => view === 'hotspots'
    ? (degree[String(p.name)] || 0)
    : (Number(p.node_count) || 0);
  const maxMetric = Math.max(1, ...pkgs.map(metric));
  const nodes = pkgs.map((p: any) => {
    const lyr = String(p.layer != null ? p.layer : 'other');
    return {
      name: String(p.name),
      value: metric(p),
      symbolSize: 8 + 42 * (metric(p) / maxMetric),
      category: layers.indexOf(lyr),
      itemStyle: { color: layerColor[lyr] },
    };
  });
  const names = new Set(nodes.map((n: any) => n.name));
  const links = (edges || [])
    .filter((e: any) => e && names.has(e.source) && names.has(e.target) && e.source !== e.target)
    .map((e: any) => ({
      source: e.source, target: e.target, value: Number(e.value) || 1,
      lineStyle: { width: Math.min(6, 1 + (Number(e.value) || 1) / 3) },
    }));
  return {
    tooltip: { confine: true },
    legend: [{ data: layers, textStyle: { color: '#9ca3af', fontSize: 9 }, top: 0 }],
    series: [{
      type: 'graph', layout: 'force', roam: true, draggable: true,
      categories: layers.map(l => ({ name: l })),
      label: { show: true, fontSize: 9, color: '#e5e7eb', position: 'right' },
      force: { repulsion: 140, edgeLength: 90, gravity: 0.08 },
      data: nodes, links,
      emphasis: { focus: 'adjacency' },
      lineStyle: { color: 'source', opacity: 0.5, curveness: 0.1 },
    }],
  };
}
if (typeof window !== 'undefined') window._buildCodebaseForceGraph = _buildCodebaseForceGraph;

// Render the force-graph into a container + wire the Packages/Hotspots toggle.
// Best-effort: missing echarts / no packages → leaves the placeholder text.
function _renderCodebaseGraph(containerId: any, packages: any, edges: any) {
  if (typeof window === 'undefined' || !window.echarts) return;
  const el = document.getElementById(containerId);
  if (!el) return;
  const opt = _buildCodebaseForceGraph(packages, edges, 'packages');
  if (!opt) return;
  let chart;
  try { chart = window.echarts.init(el); } catch (_) { return; }
  chart.setOption(opt);
  const setView = (view: any) => {
    const o = _buildCodebaseForceGraph(packages, edges, view);
    if (o) chart.setOption(o, true);
    document.querySelectorAll(`[data-cg-toggle][data-cg-for="${containerId}"]`).forEach(b => {
      const on = b.getAttribute('data-cg-toggle') === view;
      b.style.color = on ? 'var(--text)' : 'var(--muted)';
      b.style.borderColor = on ? 'var(--accent)' : 'var(--border)';
    });
  };
  document.querySelectorAll(`[data-cg-toggle][data-cg-for="${containerId}"]`).forEach(b => {
    b.onclick = () => setView(b.getAttribute('data-cg-toggle'));
  });
  setView('packages');
}
if (typeof window !== 'undefined') window._renderCodebaseGraph = _renderCodebaseGraph;

// 5813affe — POST the (already-fetched) package graph to the server, which
// renders a static Graphviz PNG and returns it as a base64 data URI. Surfaces an
// actionable hint when Graphviz isn't installed (503 graphviz_missing).
async function _generateCodebaseMap(projectId: any) {
  const data = (window._codeGraphData || {})[projectId];
  const out = data && document.getElementById(`${data.cgId}-map`);
  if (!data || !out) return;
  if (!(data.packages || []).length) {
    out.innerHTML = `<div style="font-size:10px;color:var(--muted)">No packages to map yet — index the repo first.</div>`;
    return;
  }
  out.innerHTML = `<div style="font-size:10px;color:var(--muted)">rendering map…</div>`;
  try {
    const r = await fetch(`/projects/${encodeURIComponent(projectId)}/codebase-map`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ packages: data.packages, edges: data.edges, hotspots: false }),
    });
    if (r.status === 503) {
      const body = await r.json().catch(() => ({}));
      out.innerHTML = `<div style="font-size:10px;color:#e0b400;border:1px solid #c79a00;border-radius:4px;padding:6px 8px;background:rgba(199,154,0,0.10)">&#9888; ${escapeHtml(body.message || 'Graphviz is not installed on the server.')}</div>`;
      return;
    }
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const body = await r.json();
    if (body.image) {
      out.innerHTML = `<img src="${body.image}" alt="Codebase package map" style="max-width:100%;border:1px solid var(--border);border-radius:4px;background:#0b0e14">`;
    } else {
      out.innerHTML = `<div style="font-size:10px;color:var(--error)">No image returned.</div>`;
    }
  } catch (e: any) {
    out.innerHTML = `<div style="font-size:10px;color:var(--error)">Map generation failed: ${escapeHtml(String(e))}</div>`;
  }
}
if (typeof window !== 'undefined') window._generateCodebaseMap = _generateCodebaseMap;


// 3f596f81 — Project Documents panel. Lists ingested documents (project_notes
// note_kind='document') and, per doc, an on-demand heading-tree structure view
// via GET /projects/{id}/document-structure (docs_intel.document_outline).
// Honest scope: structure = heading tree + paragraph/heading counts only;
// figures/cross-refs/equations/comments are not extracted by docs_intel Phase 1.
// 0b711a9d — Project Insights panel. Lists durable strategic insights (the
// dedicated insights table) grouped by horizon (permanent / year / quarter).
// Permanent insights always surface in the planning brief; add via add_insight.
async function loadInsightsTab(projectId: any) {
  const body = document.getElementById(`insights-body-${projectId}`);
  if (!body) return;
  body.innerHTML = '<div class="empty" style="color:var(--muted)">loading…</div>';

  let insights: any[] = [];
  try {
    insights = ((await api(`/projects/${projectId}/insights`)) as any[]) || [];
  } catch (e: any) {
    body.innerHTML = `<div class="empty" style="color:var(--error)">Could not load insights: ${escapeHtml(String(e))}</div>`;
    return;
  }

  // aefebc9c — horizon buckets rendered as labelled sub-sections (below), so the
  // header count and the visible list agree instead of an unseparated mix.
  const HORIZONS: Array<{ key: string; label: string; color: string }> = [
    { key: 'permanent', label: 'PERMANENT', color: 'var(--accent)' },
    { key: 'year', label: 'YEAR', color: 'var(--warning, #d29922)' },
    { key: 'quarter', label: 'QUARTER', color: 'var(--muted)' },
  ];

  let html = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
    <div style="font-size:11px;color:var(--text)"><b>${insights.length}</b> insight${insights.length === 1 ? '' : 's'}</div>
  </div>
  <div style="font-size:9px;color:var(--muted);margin-bottom:10px">Durable strategic understanding — separate from decisions (choices) and notes (reference). Add via the <code>add_insight</code> MCP tool. Permanent insights always appear in the planning brief.</div>`;

  if (!insights.length) {
    html += `<div class="empty" style="color:var(--muted);padding:8px 0">No insights yet. Capture accumulated understanding with <code>add_insight(project_id, title, body, horizon)</code>.</div>`;
  } else {
    // aefebc9c — bucket by horizon; unknown horizons fall into QUARTER. The badge
    // above counts insights.length (all shown) and each section prints its own
    // count, so the total and the sum of the sections always agree.
    const buckets: Record<string, any[]> = { permanent: [], year: [], quarter: [] };
    for (const ins of insights) {
      const h = String(ins.horizon || 'quarter').toLowerCase();
      (buckets[h] || buckets.quarter).push(ins);
    }
    const _renderCard = (ins: any) => {
      const tags = String(ins.tags || '').split(',').map((t: string) => t.trim()).filter(Boolean);
      return `<div style="border:1px solid var(--border);border-radius:4px;padding:8px 10px;margin-bottom:8px;background:var(--surface-1)">
        <div style="font-size:11px;color:var(--text);font-weight:600">${escapeHtml(String(ins.title || ''))}</div>
        ${ins.body ? `<div style="font-size:10px;color:var(--muted);margin-top:4px;white-space:pre-wrap">${escapeHtml(String(ins.body))}</div>` : ''}
        ${tags.length ? `<div style="margin-top:4px;display:flex;gap:3px;flex-wrap:wrap">${tags.map((t: string) => `<span style="font-size:8px;padding:1px 4px;border-radius:3px;background:var(--surface-1);border:1px solid var(--border);color:var(--muted)">#${escapeHtml(t)}</span>`).join('')}</div>` : ''}
      </div>`;
    };
    for (const hz of HORIZONS) {
      const items = buckets[hz.key];
      if (!items.length) continue;
      html += `<div class="insights-horizon-section" data-horizon="${hz.key}" style="display:flex;align-items:center;gap:6px;margin:12px 0 6px;padding-bottom:3px;border-bottom:1px solid ${hz.color}">
        <span style="font-size:9px;font-weight:700;letter-spacing:.05em;color:${hz.color}">${hz.label}</span>
        <span style="font-size:9px;color:var(--muted)">${items.length}</span>
      </div>`;
      for (const ins of items) html += _renderCard(ins);
    }
  }
  body.innerHTML = html;
}


// 1e1bd6b0 — Executor session timeline: per session, its start/end + the sprint
// items it worked, grouped by item_group, each tagged done / failed /
// stopped-ambiguously (session ended while the item was still claimed).
async function loadSessionTimelineTab(projectId: any) {
  const body = document.getElementById(`sessions-body-${projectId}`);
  if (!body) return;
  body.innerHTML = '<div class="empty" style="color:var(--muted)">loading…</div>';

  let data: any = null;
  try {
    data = await api(`/projects/${projectId}/session-timeline`);
  } catch (e: any) {
    body.innerHTML = `<div class="empty" style="color:var(--error)">Could not load session timeline: ${escapeHtml(String(e))}</div>`;
    return;
  }

  const sessions: any[] = (data && data.sessions) || [];
  const OUTCOME: Record<string, { label: string; color: string }> = {
    'done': { label: 'DONE', color: 'var(--accent)' },
    'failed': { label: 'FAILED', color: 'var(--status-failed, #f85149)' },
    'stopped-ambiguously': { label: 'STOPPED?', color: 'var(--warning, #d29922)' },
    'in_progress': { label: 'RUNNING', color: 'var(--muted)' },
  };
  const _pill = (o: any) => {
    const m = OUTCOME[String(o)] || { label: String(o || '').toUpperCase(), color: 'var(--muted)' };
    return `<span style="font-size:8px;padding:1px 5px;border-radius:3px;border:1px solid ${m.color};color:${m.color};letter-spacing:.04em" title="${escapeHtml(String(o))}">${m.label}</span>`;
  };

  let html = `<div style="font-size:9px;color:var(--muted);margin-bottom:10px">Per executor session: start/end + the sprint items it worked, grouped by item group. <b>STOPPED?</b> = the session ended while it still had an item claimed (a silent stop) — distinct from <b>FAILED</b> (the item actively errored).</div>`;

  if (!sessions.length) {
    html += `<div class="empty" id="session-timeline-empty" style="color:var(--muted);padding:8px 0">No executor sessions yet.</div>`;
  } else {
    for (const s of sessions) {
      const when = s.ended_at
        ? `${escapeHtml(String(s.started_at || ''))} → ${escapeHtml(String(s.ended_at))}`
        : `${escapeHtml(String(s.started_at || ''))} → (${escapeHtml(String(s.status || 'active'))})`;
      html += `<div class="session-timeline-row" style="border:1px solid var(--border);border-radius:4px;padding:8px 10px;margin-bottom:8px;background:var(--surface-1)">
        <div style="display:flex;gap:8px;align-items:center;justify-content:space-between">
          <span style="font-size:11px;color:var(--text);font-weight:600">${escapeHtml(String(s.name || s.id || ''))}</span>
          <span style="font-size:8px;color:var(--muted)">${s.item_count || 0} item${(s.item_count === 1) ? '' : 's'}</span>
        </div>
        <div style="font-size:9px;color:var(--muted);margin-top:2px">${when}</div>`;
      const groups: any[] = s.groups || [];
      if (!groups.length) {
        html += `<div style="font-size:9px;color:var(--muted);margin-top:6px">no sprint items attributed</div>`;
      } else {
        for (const g of groups) {
          html += `<div style="margin-top:6px"><div style="font-size:8px;color:var(--muted);letter-spacing:.05em;text-transform:uppercase;margin-bottom:2px">${escapeHtml(String(g.item_group || ''))}</div>`;
          for (const it of (g.items || [])) {
            html += `<div style="display:flex;gap:6px;align-items:center;justify-content:space-between;padding:2px 0">
              <span style="font-size:10px;color:var(--text)">${escapeHtml(String(it.nickname || it.title || ''))}</span>
              ${_pill(it.outcome)}
            </div>`;
          }
          html += `</div>`;
        }
      }
      html += `</div>`;
    }
  }
  body.innerHTML = html;
}


async function loadBlogTab(projectId: any) {
  // Blog is WORKSPACE-scoped, not per-project — read the workspace endpoint.
  const body = document.getElementById(`blog-body-${projectId}`);
  if (!body) return;
  body.innerHTML = '<div class="empty" style="color:var(--muted)">loading…</div>';

  let posts: any[] = [];
  try {
    posts = ((await api('/workspace/blog')) as any[]) || [];
  } catch (e: any) {
    body.innerHTML = `<div class="empty" style="color:var(--error)">Could not load blog posts: ${escapeHtml(String(e))}</div>`;
    return;
  }

  const GROUPS: Array<{ key: string; label: string; color: string }> = [
    { key: 'draft', label: 'DRAFTS', color: 'var(--muted)' },
    { key: 'published', label: 'PUBLISHED', color: 'var(--accent)' },
    { key: 'archived', label: 'ARCHIVED', color: 'var(--warning, #d29922)' },
  ];

  const pid = String(projectId);
  // e553fa7a — editable blog: an inline editor form (create + edit-in-place) at
  // the top, and an "Edit" affordance on every post that repopulates the form
  // from that post so a draft's title/body can be changed and re-saved.
  let html = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
    <div style="font-size:11px;color:var(--text)"><b>${posts.length}</b> post${posts.length === 1 ? '' : 's'}</div>
  </div>
  <div style="font-size:9px;color:var(--muted);margin-bottom:10px">Workspace-scoped blog. Edit a draft below (or author via the <code>save_blog_post</code> MCP tool); published posts are live at <code>/blog/&lt;slug&gt;</code>.</div>`;

  html += blogEditorFormHtml(pid, null);

  if (!posts.length) {
    html += `<div class="empty" style="color:var(--muted);padding:8px 0">No posts yet. Create one above, or with <code>save_blog_post(title, body, status="published")</code>.</div>`;
  } else {
    for (const g of GROUPS) {
      const inGroup = posts.filter(p => String(p.status || 'draft') === g.key);
      if (!inGroup.length) continue;
      html += `<div style="font-size:9px;color:${g.color};letter-spacing:.06em;margin:12px 0 6px">${g.label} · ${inGroup.length}</div>`;
      for (const p of inGroup) {
        html += blogPostCardHtml(p);
      }
    }
  }
  body.innerHTML = html;

  // --- Wire the editor form + per-post Edit buttons. ------------------------
  const byId = (suffix: string) => document.getElementById(`blog-editor-${suffix}-${pid}`);
  const saveBtn = byId('save') as HTMLButtonElement | null;
  const resetBtn = byId('reset') as HTMLButtonElement | null;
  const msgEl = document.getElementById(`blog-editor-status-msg-${pid}`);

  const doSave = async () => {
    const idEl = document.getElementById(`blog-editor-id-${pid}`) as HTMLInputElement | null;
    const titleEl = document.getElementById(`blog-editor-title-input-${pid}`) as HTMLInputElement | null;
    const bodyEl = document.getElementById(`blog-editor-body-${pid}`) as HTMLTextAreaElement | null;
    const statusEl = document.getElementById(`blog-editor-status-${pid}`) as HTMLSelectElement | null;
    const title = (titleEl?.value || '').trim();
    if (!title) { if (msgEl) msgEl.textContent = 'Title required'; return; }
    if (saveBtn) saveBtn.disabled = true;
    if (msgEl) msgEl.textContent = 'Saving…';
    try {
      await api('/workspace/blog', {
        method: 'POST',
        body: JSON.stringify({
          id: idEl?.value || undefined,          // present ⇒ update in place (upsert by id)
          title,
          body: bodyEl?.value || '',
          status: statusEl?.value || 'draft',
        }),
      });
      loadBlogTab(projectId);                      // re-render (also resets the form)
    } catch (e: any) {
      if (saveBtn) saveBtn.disabled = false;
      if (msgEl) msgEl.textContent = `Error: ${escapeHtml(String(e))}`;
    }
  };
  if (saveBtn) saveBtn.onclick = doSave;
  if (resetBtn) resetBtn.onclick = () => resetBlogEditor(pid);

  body.querySelectorAll('.blog-edit-btn').forEach((btn) => {
    (btn as HTMLElement).onclick = () => {
      const bid = (btn as HTMLElement).getAttribute('data-blog-id') || '';
      const post = posts.find(p => String(p.id || '') === bid);
      if (!post) return;
      populateBlogEditor(pid, post);
      document.getElementById(`blog-editor-${pid}`)?.scrollIntoView({ block: 'nearest' });
      (document.getElementById(`blog-editor-title-input-${pid}`) as HTMLInputElement | null)?.focus();
    };
  });
}


async function loadDocumentsTab(projectId: any) {
  const body = document.getElementById(`documents-body-${projectId}`);
  if (!body) return;
  body.innerHTML = '<div class="empty" style="color:var(--muted)">loading…</div>';

  let docs: any[] = [];
  try {
    const dp = await api(`/projects/${projectId}/notes?paginate=true&limit=200`);
    docs = (((dp && dp.notes) || []) as any[])
      .filter(n => String(n.note_kind || '').toLowerCase() === 'document');
  } catch (e: any) {
    body.innerHTML = `<div class="empty" style="color:var(--error)">Could not load documents: ${escapeHtml(String(e))}</div>`;
    return;
  }

  // 79ee73e8 — stateless get_document_structure peeks (viewed, NOT ingested).
  let peeks: any[] = [];
  try {
    const pk = await api('/document-peeks');
    peeks = (((pk && pk.peeks) || []) as any[]);
  } catch (_) { peeks = []; }

  const _srcBadge = (src: any) => {
    const s = String(src || 'local').toLowerCase();
    const label = s.includes('onedrive') ? 'OneDrive'
      : (s.includes('gdrive') || s.includes('google')) ? 'GDrive'
      : (src || 'local');
    return `<span style="font-size:9px;padding:1px 5px;border-radius:3px;background:var(--surface-1);border:1px solid var(--border);color:var(--muted)">${escapeHtml(String(label))}</span>`;
  };

  let html = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
    <div style="font-size:11px;color:var(--text)"><b>${docs.length}</b> document${docs.length === 1 ? '' : 's'} <span style="color:var(--muted)">ingested</span></div>
  </div>`;

  // f1c7e7d1 — tunnel-free upload: pick a local .txt/.md, read it client-side,
  // POST {filename, content} to /documents/upload (reuses ingest_document's
  // content path). No tunnel / server-side file access needed.
  html += `<div style="display:flex;gap:6px;align-items:center;margin-bottom:12px;padding:8px 10px;border:1px dashed var(--border);border-radius:4px;background:var(--surface-1)">
    <input type="file" id="doc-upload-input-${escapeHtml(String(projectId))}" accept=".txt,.md" style="font-size:10px;flex:1;min-width:0" />
    <button id="doc-upload-btn-${escapeHtml(String(projectId))}" class="secondary" style="font-size:10px;padding:3px 10px" disabled>Upload .txt/.md</button>
  </div>
  <div id="doc-upload-status-${escapeHtml(String(projectId))}" style="font-size:9px;color:var(--muted);margin-bottom:4px">Plain .txt / .md only — read in your browser, stored as a searchable document note.</div>
  <div style="font-size:9px;color:var(--muted);margin-bottom:10px;padding:6px 8px;border-left:2px solid var(--accent);background:var(--surface-1)">📄 <b>Word / PDF?</b> Ingest it with the <code>ingest_document</code> MCP tool (<code>file_path</code> or <code>content</code>) — it saves as a searchable <code>kind=document</code> note and appears above. A <code>get_document_structure</code> peek only reads the outline; it does <b>not</b> save the doc (those show under “Recently viewed” below).</div>`;

  if (!docs.length) {
    html += `<div class="empty" style="color:var(--muted);padding:8px 0">No documents ingested yet. Upload a .txt/.md above, or ingest a Word/PDF doc with the <code>ingest_document</code> MCP tool (file_path or content) — it is stored as a project note with kind=document and appears here.</div>`;
  } else {
    for (const d of docs) {
      const title = d.title || d.slug || d.id;
      const fp = d.file_path || '';
      const tags = Array.isArray(d.tags) ? d.tags : [];
      const did = String(d.id);
      html += `<div style="border:1px solid var(--border);border-radius:4px;padding:8px 10px;margin-bottom:8px;background:var(--surface-1)">
        <div style="display:flex;gap:8px;align-items:center;justify-content:space-between">
          <span style="font-size:11px;color:var(--text);font-weight:600">${escapeHtml(String(title))}</span>
          ${_srcBadge(d.source)}
        </div>
        ${fp ? `<div style="font-size:9px;color:var(--muted);font-family:var(--font-mono);margin-top:3px;word-break:break-all">${escapeHtml(String(fp))}</div>` : ''}
        ${tags.length ? `<div style="margin-top:4px;display:flex;gap:3px;flex-wrap:wrap">${tags.map((t: any) => `<span style="font-size:8px;padding:1px 4px;border-radius:3px;background:var(--surface-1);border:1px solid var(--border);color:var(--muted)">#${escapeHtml(String(t))}</span>`).join('')}</div>` : ''}
        ${fp
          ? `<div style="margin-top:6px;display:flex;gap:6px">
              <button class="doc-struct-btn" data-fp="${escapeHtml(String(fp))}" data-did="${escapeHtml(did)}" style="font-size:9px;padding:2px 8px">View structure</button>
              ${String(fp).toLowerCase().endsWith('.docx')
                ? `<button class="doc-review-btn" data-fp="${escapeHtml(String(fp))}" data-did="${escapeHtml(did)}" style="font-size:9px;padding:2px 8px">Review findings</button>`
                : ''}
            </div><div id="doc-struct-${escapeHtml(did)}" style="margin-top:6px"></div><div id="doc-review-${escapeHtml(did)}" data-fp="${escapeHtml(String(fp))}" style="margin-top:6px"></div>`
          : '<div style="font-size:9px;color:var(--muted);margin-top:4px">No server-side file_path — structure view unavailable.</div>'}
      </div>`;
    }
    html += `<div style="font-size:9px;color:var(--muted);margin-top:6px">Structure = heading tree + paragraph/heading counts (docs_intel Phase 1). Figures, cross-references, equations and comments are not yet extracted. Structure needs the file on the tunnel/self-host server.</div>`;
  }

  // 79ee73e8 — "Recently viewed (not saved)": stateless get_document_structure
  // peeks. They are NOT ingested/searchable — surface them here with a one-click
  // "Ingest this" that copies the ingest_document command, so the ingest/peek
  // distinction is visible exactly where it confused people.
  // 6b88a22b — these peeks are TENANT/workspace-global, NOT project-scoped: the
  // backend peek log (doc_peeks) is keyed by tenant only, so the SAME list renders
  // in every project's Documents tab. Adam saw these as "funny floating notes and
  // resources files" not attached to any project. Until the peek store is
  // project-scoped server-side, be explicit that this section is global so the
  // entries don't masquerade as documents belonging to the current project.
  if (peeks.length) {
    html += `<div id="doc-peeks-section-${escapeHtml(String(projectId))}" style="margin-top:16px;border-top:1px dashed var(--border);padding-top:12px">
      <div style="display:flex;align-items:center;gap:6px">
        <span style="font-size:10px;font-weight:600;color:var(--accent)">Recently viewed (not saved) — ${peeks.length}</span>
        <span title="These outline peeks are recorded per workspace, not per project, so the same list shows in every project's Documents tab." style="font-size:8px;font-weight:600;padding:1px 5px;border-radius:3px;background:var(--surface-1);border:1px solid var(--border);color:var(--muted);text-transform:uppercase;letter-spacing:0.4px">workspace-wide</span>
      </div>
      <div style="font-size:9px;color:var(--muted);margin:2px 0 8px">Peeked with <code>get_document_structure</code> (a stateless outline read) but never ingested — so they are NOT searchable here, and they are <b>not attached to this project</b> (this list is shared across every project in your workspace). Ingest one to save it as a document on <i>this</i> project.</div>`;
    for (const pk of peeks) {
      const fp = String(pk.file_path || '');
      const failed = pk.ok === false;
      html += `<div style="border:1px dashed var(--border);border-radius:4px;padding:6px 10px;margin-bottom:6px;background:var(--surface-1)">
        <div style="display:flex;gap:8px;align-items:center;justify-content:space-between">
          <span style="font-size:9px;color:var(--muted);font-family:var(--font-mono);word-break:break-all">${escapeHtml(fp)}</span>
          <button class="doc-peek-ingest-btn" data-fp="${escapeHtml(fp)}" style="font-size:9px;padding:2px 8px;white-space:nowrap">Ingest this</button>
        </div>
        <div style="font-size:8px;color:var(--muted);margin-top:2px">viewed ${escapeHtml(String(pk.viewed_at || ''))}${failed ? ' · peek failed (hosted — no file access)' : ''}</div>
      </div>`;
    }
    html += `</div>`;
  }
  body.innerHTML = html;

  // Wire the peek "Ingest this" buttons: copy the ingest_document command (a real
  // one-click action that works regardless of hosted/self-hosted file access).
  body.querySelectorAll('.doc-peek-ingest-btn').forEach((el: any) => {
    el.addEventListener('click', () => {
      const fp = el.getAttribute('data-fp') || '';
      const cmd = `ingest_document(file_path="${fp}")`;
      try { navigator.clipboard.writeText(cmd); } catch (_) {}
      const prev = el.textContent;
      el.textContent = 'Copied ✓';
      setTimeout(() => { el.textContent = prev || 'Ingest this'; }, 1500);
    });
  });

  // f1c7e7d1 — wire the upload picker: enable the button once a .txt/.md is
  // chosen, then read text client-side and POST it to the upload route.
  const _fileInput = document.getElementById(`doc-upload-input-${projectId}`) as HTMLInputElement | null;
  const _uploadBtn = document.getElementById(`doc-upload-btn-${projectId}`) as HTMLButtonElement | null;
  const _uploadStatus = document.getElementById(`doc-upload-status-${projectId}`);
  const _setStatus = (msg: string, color: string) => {
    if (_uploadStatus) { _uploadStatus.textContent = msg; (_uploadStatus as HTMLElement).style.color = color; }
  };
  if (_fileInput && _uploadBtn) {
    _fileInput.addEventListener('change', () => {
      _uploadBtn.disabled = !(_fileInput.files && _fileInput.files.length > 0);
    });
    _uploadBtn.addEventListener('click', async () => {
      const file = _fileInput.files && _fileInput.files[0];
      if (!file) return;
      const name = file.name || '';
      if (!/\.(txt|md)$/i.test(name)) {
        _setStatus('Only .txt and .md files are supported.', 'var(--error)');
        return;
      }
      _uploadBtn.disabled = true;
      _setStatus(`Reading ${name}…`, 'var(--muted)');
      try {
        const text = await file.text();
        const res = await api(`/projects/${projectId}/documents/upload`, {
          method: 'POST',
          body: JSON.stringify({ filename: name, content: text }),
        });
        if (res && res.error) throw new Error(String(res.error));
        _setStatus(`Uploaded "${name}".`, 'var(--accent)');
        await loadDocumentsTab(projectId);  // refresh the list
      } catch (e: any) {
        // api() throws on 4xx; try to surface the FastAPI `detail` message.
        let msg = String(e && e.message ? e.message : e);
        try {
          const rt = e && e.responseText;
          if (rt) { const j = JSON.parse(rt); if (j && j.detail) msg = String(j.detail); }
        } catch (_) { /* keep raw message */ }
        _setStatus(`Upload failed: ${msg}`, 'var(--error)');
        _uploadBtn.disabled = false;
      }
    });
  }

  body.querySelectorAll('.doc-struct-btn').forEach(btn => {
    (btn as HTMLElement).addEventListener('click', async () => {
      const fp = (btn as HTMLElement).dataset.fp || '';
      const did = (btn as HTMLElement).dataset.did || '';
      const target = document.getElementById(`doc-struct-${did}`);
      if (!target) return;
      target.innerHTML = '<span style="font-size:9px;color:var(--muted)">loading structure…</span>';
      try {
        const st = await api(`/projects/${projectId}/document-structure?path=${encodeURIComponent(fp)}`);
        if (st && st.error) {
          target.innerHTML = `<span style="font-size:9px;color:var(--warning,#d29922)">${escapeHtml(String(st.error))}</span>`;
          return;
        }
        const headings = (st && st.headings) || [];
        const tree = headings.map((h: any) => {
          const lvl = Math.max(1, Math.min(6, parseInt(h.level, 10) || 1));
          return `<div style="font-size:10px;color:var(--text);padding-left:${(lvl - 1) * 12}px">${escapeHtml(String(h.text || ''))}</div>`;
        }).join('');
        target.innerHTML = `<div style="font-size:9px;color:var(--muted);margin-bottom:3px">${st.paragraph_count} paragraphs · ${st.heading_count} headings</div>${tree || '<span style="font-size:9px;color:var(--muted)">No headings found.</span>'}`;
      } catch (e: any) {
        target.innerHTML = `<span style="font-size:9px;color:var(--error)">Failed: ${escapeHtml(String(e))}</span>`;
      }
    });
  });

  // b67ec6b5 — "Review findings": non-mutating DOCX review panel (grouped
  // findings + human-readable locators). Toggle-loads on first click so a
  // Documents tab with many docs doesn't eagerly review every one of them.
  wireDocumentReviewButtons(projectId, body);
}


async function loadCodeIntelTab(projectId: any) {
  const body = document.getElementById(`codeintel-body-${projectId}`);
  if (!body) return;

  body.innerHTML = '<div class="empty" style="color:var(--muted)">loading…</div>';

  try {
    const [pluginsData, meData, settingsData] = await Promise.all([
      api('/tunnel/plugins'),
      api('/me'),
      loadProjectSettings(projectId),
    ]);

    // e2688fc1 — gate on EITHER code OR outputs being active; each section
    // degrades independently if its own slot is not connected.
    const _codeActive = !!(pluginsData?.active?.code);
    const _outputsActive = !!(pluginsData?.active?.outputs);
    if (!_codeActive && !_outputsActive) {
      body.innerHTML = '<div class="empty" style="color:var(--muted)">No tunnel slots are active. Run <code>meridian --tunnel</code> to connect code-intel and/or outputs indexing.</div>';
      return;
    }

    const tenantId = meData?.tenant_id;
    if (!tenantId) {
      body.innerHTML = '<div class="empty" style="color:var(--error)">Could not resolve tenant ID from /me.</div>';
      return;
    }

    const codeBase = `/code/mcp/${tenantId}/mcp`;
    const _outputsBase = `/outputs/mcp/${tenantId}/mcp`;

    async function _codeMcpCall(method: any, params: any) {
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

    // e2688fc1 — identical JSON-RPC helper for the meridian-outputs tunnel proxy.
    async function _outputsMcpCall(method: any, params: any) {
      const r = await fetch(_outputsBase, {
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
      if (!parsed) throw new Error('empty response from outputs MCP');
      if (parsed.error) throw new Error(parsed.error.message || String(parsed.error));
      return parsed.result;
    }

    // Verify live + count available tools (only when code slot is active).
    let toolCount = 0;
    if (_codeActive) {
      try {
        const tlResult = await _codeMcpCall('tools/list', {});
        toolCount = (tlResult?.tools || []).length;
      } catch (_) {}
    }

    const execCfg = settingsData?.executor_config || {};
    const repoPaths = Array.isArray(execCfg.repo_paths) ? execCfg.repo_paths : [];

    let html = '';
    let archCharts: any[] = [];  // {id, config} pairs instantiated after body.innerHTML
    // 65742e42 — codebase force-graph data, populated in the Architecture section
    // below and rendered after body.innerHTML into the container added here.
    let _graphPackages = [];
    let _graphEdges: any[] = [];
    let _graphArch = null;   // ff8ff615 — full get_architecture payload (packages+layers)
    let _archError = '';     // ff8ff615 — arch fetch error → panel error state
    const _cgId = `ci-forcegraph-${projectId}`;

    // Live indicator — show separate dots for each active slot.
    html += `<div style="display:flex;align-items:center;gap:12px;margin-bottom:14px">`;
    if (_codeActive) {
      html += `<span style="display:inline-flex;align-items:center;gap:6px">
        <span style="width:8px;height:8px;border-radius:50%;background:#22c55e;display:inline-block;flex-shrink:0"></span>
        <span style="font-size:11px;color:var(--text);font-weight:600">Code Intel Live</span>
        ${toolCount ? `<span style="font-size:10px;color:var(--muted)">${toolCount} tool${toolCount !== 1 ? 's' : ''}</span>` : ''}
      </span>`;
    }
    if (_outputsActive) {
      html += `<span style="display:inline-flex;align-items:center;gap:6px">
        <span style="width:8px;height:8px;border-radius:50%;background:#22c55e;display:inline-block;flex-shrink:0"></span>
        <span style="font-size:11px;color:var(--text);font-weight:600">Outputs Live</span>
      </span>`;
    }
    html += `</div>`;

    // 3e28f593 — Resources: unified codebase + documents summary so the scattered
    // repo-path / document fields have one coherent view (which repo(s) this
    // project indexes + how many ingested documents).
    let _docsCount = 0;
    try {
      const _dp = await api(`/projects/${projectId}/notes?paginate=true&limit=200`);
      _docsCount = (((_dp && _dp.notes) || []) as any[])
        .filter(n => String(n.note_kind || '').toLowerCase() === 'document').length;
    } catch (_) { /* docs count is best-effort */ }
    const _cwds = repoPaths.map((rp: any) => typeof rp === 'string' ? rp : (rp.cwd || '')).filter(Boolean);
    html += `<div style="margin-bottom:16px;padding:10px 12px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px">
      <div style="font-size:10px;color:var(--accent);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px">Resources</div>
      <div style="font-size:11px;color:var(--text);line-height:1.7">
        <div>Codebases: <b>${_cwds.length}</b> repo path${_cwds.length !== 1 ? 's' : ''}${_cwds.length ? ` — ${escapeHtml(_cwds.join(', '))}` : ' <span style="color:var(--muted)">(add in Settings → Executor Config)</span>'}</div>
        <div>Documents: <b>${_docsCount}</b> ingested <span style="color:var(--muted)">(kind=document)</span></div>
      </div>
    </div>`;

    // e2688fc1 — code-intel sections are gated on _codeActive; degraded gracefully when
    // only the outputs slot is live (tunnel:code not connected).
    if (_codeActive) {
      // fa9c9abd — the new hierarchical drill-down (ed5512b6) LEADS: injected first so
      // it renders above the older flat layered panel. A live screenshot suggested the
      // new codegraph was "unreachable"; it was actually wired and rendering, just below
      // the old panel. Both mounts below resolve their container by id, so DOM order is
      // set purely by this html-build order — reordering here is safe.
      // ed5512b6 — the standalone codegraph visualizer (folder->file->function
      // drill-down, color by role, static metadata on click). Mounted after
      // body.innerHTML via the thin adapter below; the module never reaches back
      // into the dashboard — data flows in only.
      html += `<div style="margin-bottom:16px">
        <div style="font-size:10px;color:var(--accent);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px;padding-bottom:4px;border-bottom:1px solid var(--border)">Code Tree</div>
        <div id="${_cgId}-codegraph"></div>
      </div>`;

      // ff8ff615 — the Preact CodeIntelPanel renders the layered package DAG +
      // zoom + Generate Map into this mount point (mounted after body.innerHTML).
      html += `<div style="margin-bottom:16px"><div id="${_cgId}-panel"></div></div>`;

      // Index status per repo path
      html += `<div style="margin-bottom:16px"><div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;padding-bottom:4px;border-bottom:1px solid var(--border)"><span style="font-size:10px;color:var(--accent);text-transform:uppercase;letter-spacing:.06em">Index Status</span><button id="${_cgId}-reindex" class="secondary" style="padding:2px 10px;font-size:10px" title="Re-run index_repository for each repo path (31d0caa6)">&#8635; Reindex</button></div>`;
      if (repoPaths.length) {
        for (const rp of repoPaths) {
          const cwd = typeof rp === 'string' ? rp : (rp.cwd || '');
          const hostname = typeof rp === 'object' ? (rp.hostname || '') : '';
          if (!cwd) continue;
          try {
            const result = await _codeMcpCall('tools/call', {name: 'index_status', arguments: {project: _repoPathToProject(cwd)}});
            const text = (result?.content || []).map((c: any) => c.text || '').join('').trim();
            html += `<div style="margin-bottom:10px">
              <div style="font-size:10px;color:var(--text);font-weight:600;margin-bottom:4px">${escapeHtml(cwd)}${hostname ? `<span style="color:var(--muted);font-weight:400"> · ${escapeHtml(hostname)}</span>` : ''}</div>
              <pre style="font-size:10px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:8px;white-space:pre-wrap;word-break:break-all;color:var(--text);margin:0;line-height:1.5">${escapeHtml(text || '(no status returned)')}</pre>
            </div>`;
          } catch (e: any) {
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
      html += `<div style="margin-bottom:16px"><div style="font-size:10px;color:var(--accent);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px;padding-bottom:4px;border-bottom:1px solid var(--border)">Architecture Summary</div>`;
      try {
        const archPath = repoPaths.length ? (typeof repoPaths[0] === 'string' ? repoPaths[0] : (repoPaths[0].cwd || '')) : '';
        const archArgs = archPath ? {project: _repoPathToProject(archPath)} : {};
        const archResult = await _codeMcpCall('tools/call', {name: 'get_architecture', arguments: archArgs});
        const archText = (archResult?.content || []).map((c: any) => c.text || '').join('').trim();
        const archSection = _codeArchSection(archText);
        html += archSection.html;
        archCharts = archSection.charts;
        // 65742e42 — capture packages + fetch cross-package edges for the force-graph.
        try {
          const _arch = JSON.parse(archText);
          _graphArch = _arch;
          // 20d17e08 — join the separate layers list onto packages so the force-graph
          // colours by layer instead of one "other" bucket (no-op if no join exists).
          _graphPackages = _resolvePackageLayers(_arch?.packages, _arch?.layers);
        } catch (_) { _graphPackages = []; }
        if (_graphPackages.length) {
          try {
            const _edgeRes = await _codeMcpCall('tools/call', {name: 'query_graph', arguments: {
              ...archArgs,
              cypher: 'MATCH (a)-[r]->(b) WHERE a.package <> b.package RETURN a.package, b.package, count(r)',
            }});
            _graphEdges = _normalizeGraphEdges(_edgeRes);
          } catch (_) { _graphEdges = []; }  // nodes-only graph still renders
        }
      } catch (e: any) {
        _archError = String(e);
        html += `<div style="font-size:10px;color:var(--error)">get_architecture failed: ${escapeHtml(String(e))}</div>`;
      }
      html += `</div>`;
    } else {
      // Only outputs is active — show a friendly note that code-intel is disconnected.
      html += `<div style="margin-bottom:16px;padding:10px 12px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;font-size:11px;color:var(--muted)">
        Code-intel tunnel not connected. Run <code>meridian --tunnel</code> with the code slot enabled to see architecture visualization and index status.
      </div>`;
    }

    // e2688fc1 — Outputs Index section: fetches indexing state from the meridian-outputs
    // tunnel proxy. Always rendered; degrades gracefully when the outputs slot is
    // not active. Reads outputs_dirs from executor_config (set via set_executor_config).
    html += `<div style="margin-bottom:16px"><div style="font-size:10px;color:var(--accent);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px;padding-bottom:4px;border-bottom:1px solid var(--border)">Outputs Index</div>`;
    if (_outputsActive) {
      // Probe the outputs tunnel: list tools to confirm live.
      let _outputsToolCount = 0;
      try {
        const _otl = await _outputsMcpCall('tools/list', {});
        _outputsToolCount = (_otl?.tools || []).length;
      } catch (_) {}
      html += `<div style="margin-bottom:8px;font-size:10px;color:var(--muted)">${_outputsToolCount} tool${_outputsToolCount !== 1 ? 's' : ''} available via meridian-outputs</div>`;

      const _outputsDirs: string[] = Array.isArray(execCfg.outputs_dirs)
        ? (execCfg.outputs_dirs as any[]).map(d => String(d || '').trim()).filter(Boolean)
        : [];

      if (_outputsDirs.length) {
        for (const odir of _outputsDirs) {
          html += `<div style="margin-bottom:12px">
            <div style="font-size:10px;color:var(--text);font-weight:600;margin-bottom:4px">${escapeHtml(odir)}</div>`;
          try {
            // search_outputs with an empty query returns total_indexed without scanning content.
            const _osResult = await _outputsMcpCall('tools/call', {
              name: 'search_outputs',
              arguments: {outputs_dir: odir, query: '', limit: 0},
            });
            const _osText = (_osResult?.content || []).map((c: any) => c.text || '').join('').trim();
            let _totalIndexed: number | null = null;
            try {
              const _osParsed = JSON.parse(_osText);
              if (typeof _osParsed.total_indexed === 'number') _totalIndexed = _osParsed.total_indexed;
            } catch (_) {}
            if (_totalIndexed !== null) {
              html += `<div style="font-size:11px;color:var(--text)"><b>${_totalIndexed}</b> file${_totalIndexed !== 1 ? 's' : ''} indexed</div>`;
            } else {
              html += `<pre style="font-size:10px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;padding:8px;white-space:pre-wrap;word-break:break-all;color:var(--text);margin:0;line-height:1.5">${escapeHtml(_osText || '(no status returned)')}</pre>`;
            }
          } catch (e: any) {
            html += `<div style="font-size:10px;color:var(--error)">search_outputs failed: ${escapeHtml(String(e))}</div>`;
          }
          html += `</div>`;
        }
      } else {
        html += `<div style="font-size:10px;color:var(--muted)">No outputs directories configured. Set <code>outputs_dirs</code> in your executor config (<code>set_executor_config</code>) to see indexing state.</div>`;
      }
    } else {
      html += `<div style="font-size:10px;color:var(--muted)">Outputs tunnel not connected. Run <code>meridian --tunnel</code> with the outputs slot enabled (meridian-outputs-mcp) to see indexing state.</div>`;
    }
    html += `</div>`;

    html += `<div style="margin-top:8px;display:flex;gap:6px">
      <button class="secondary" style="font-size:10px;padding:3px 10px" onclick="loadCodeIntelTab(${JSON.stringify(projectId)})">↺ Refresh</button>
    </div>`;

    body.innerHTML = html;

    // Instantiate architecture charts now that their canvases are in the DOM.
    // Best-effort: a missing canvas or Chart.js just leaves that section's other
    // (pure-HTML) parts intact.
    if (window.Chart && archCharts.length) {
      for (const c of archCharts) {
        const el = document.getElementById(c.id);
        if (el) { try { new Chart(el, c.config); } catch (_) { /* skip a bad chart */ } }
      }
    }

    // 65742e42 — render the package force-graph (or show the empty hint).
    // 5813affe — stash the graph so "Generate Map" can POST it for a PNG render.
    window._codeGraphData = window._codeGraphData || {};
    window._codeGraphData[projectId] = { packages: _graphPackages, edges: _graphEdges, cgId: _cgId };
    // ff8ff615 — mount the Preact Code Intel panel (layered DAG + zoom + Generate Map).
    const _panelEl = document.getElementById(`${_cgId}-panel`);
    if (_panelEl) {
      mountCodeIntelPanel(_panelEl, {
        status: _archError ? 'error' : 'ready',
        error: _archError,
        architecture: _graphArch || { packages: _graphPackages },
        onGenerateMap: async () => {
          const r = await fetch(`/projects/${encodeURIComponent(projectId)}/codebase-map`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ packages: _graphPackages, edges: _graphEdges, hotspots: false }),
          });
          if (r.status === 503) {
            const b = await r.json().catch(() => ({}));
            throw new Error(b.message || 'Graphviz is not installed on the server.');
          }
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          const b = await r.json();
          if (!b.image) throw new Error('No image returned.');
          return b.image;
        },
      });
    }

    // ed5512b6 — thin adapter: the dashboard is ONE consumer of the standalone
    // codegraph module. It fetches per-symbol graph nodes via search_graph
    // (folder->file->function detail beyond the package-level get_architecture),
    // shapes them with the pure buildCodeGraphModel, and hands the model + a
    // mount element to renderCodeGraph. The module never reaches back here.
    const _codegraphEl = document.getElementById(`${_cgId}-codegraph`);
    if (_codegraphEl) {
      const archArgs = repoPaths.length
        ? { project: _repoPathToProject(typeof repoPaths[0] === 'string' ? repoPaths[0] : (repoPaths[0].cwd || '')) }
        : {};
      // Pull real per-symbol nodes (signature/docstring/complexity/callers-callees
      // live on these). Best-effort: on failure we still render the package-level
      // tree from get_architecture alone.
      let _nodes: GraphNodeInput[] = [];
      try {
        const _res = await _codeMcpCall('tools/call', {
          name: 'search_graph',
          arguments: { ...archArgs, limit: 1000, min_degree: 0 },
        });
        const _txt = (_res?.content || []).map((c: any) => c.text || '').join('').trim();
        if (_txt) {
          const _parsed = JSON.parse(_txt);
          const _rows = Array.isArray(_parsed)
            ? _parsed
            : (_parsed.results || _parsed.nodes || _parsed.data || []);
          if (Array.isArray(_rows)) _nodes = _rows as GraphNodeInput[];
        }
      } catch (_) { _nodes = []; }  // package-level tree still renders

      try {
        const _model = buildCodeGraphModel({
          architecture: _graphArch || { packages: _graphPackages },
          nodes: _nodes,
        });
        renderCodeGraph(_codegraphEl, _model, {
          // LLM summary — SEPARATE, last-resort on-click action wired to the
          // existing code-intel get_code_snippet path. Only invoked on demand;
          // never part of the deterministic render.
          onRequestSummary: async (node) => {
            const qn = node.meta.qualifiedName;
            if (!qn) throw new Error('No qualified_name to summarize.');
            const _snip = await _codeMcpCall('tools/call', {
              name: 'get_code_snippet',
              arguments: { ...archArgs, qualified_name: qn },
            });
            const _snipTxt = (_snip?.content || []).map((c: any) => c.text || '').join('').trim();
            return _snipTxt || 'No snippet returned.';
          },
        });
      } catch (e: any) {
        _codegraphEl.innerHTML = `<div style="font-size:10px;color:var(--error)">Code tree failed: ${escapeHtml(String(e))}</div>`;
      }
    }

    // 31d0caa6 — manual reindex button: re-run index_repository for each repo
    // path via the code MCP, then reload the tab to show fresh index status.
    const _reindexBtn = document.getElementById(`${_cgId}-reindex`) as HTMLButtonElement | null;
    if (_reindexBtn) {
      _reindexBtn.addEventListener('click', async () => {
        _reindexBtn.disabled = true;
        _reindexBtn.textContent = 'Reindexing…';
        try {
          for (const rp of repoPaths) {
            const cwd = typeof rp === 'string' ? rp : (rp.cwd || '');
            if (cwd) await _codeMcpCall('tools/call', { name: 'index_repository', arguments: { path: cwd } });
          }
          loadCodeIntelTab(projectId);
        } catch (e: any) {
          _reindexBtn.disabled = false;
          _reindexBtn.textContent = 'Reindex failed — retry';
        }
      });
    }

  } catch (e: any) {
    body.innerHTML = `<div style="color:var(--error)">Failed to load code intel: ${escapeHtml(String(e))}</div>`;
  }
}



// Turn a bare ntfy topic name ("my-alerts") into a full URL. Leaves real URLs,

// email addresses, and anything containing a slash untouched.

function normalizeNotifyTarget(raw: any) {

  const v = (raw || '').trim();

  if (!v) return '';

  if (v.includes('://') || v.includes('@') || v.includes('/')) return v;

  return `https://ntfy.sh/${v}`;

}



// Inverse of normalizeNotifyTarget for display: strip the implied ntfy.sh

// prefix so the field shows just the topic ("the prefix is added for you").

// Emails and non-ntfy webhooks pass through untouched.

function displayNotifyTarget(raw: any) {

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

function osExecutorHintBanner(projectId: any) {
  // ITEM 2 — Settings hooks banner: tell the user which shell/Python their
  // executor will use, based on the browser's OS. Dismiss persists in localStorage.
  try { if (localStorage.getItem('meridian.hooks.osbanner.dismissed') === '1') return ''; } catch (e: any) {}
  const ua = String((navigator as any).userAgentData?.platform || navigator.platform || navigator.userAgent || '').toLowerCase();
  const isWin = ua.includes('win');
  const msg = isWin
    ? 'Windows detected — executors use <strong>PowerShell</strong>; run Python with <code>pixi run python</code>.'
    : 'Mac / Linux detected — executors use <strong>bash</strong>; run Python with <code>python3</code>.';
  return `<div data-os-hint style="display:flex;align-items:flex-start;gap:8px;background:var(--surface-1);border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:4px;padding:8px 10px;margin-bottom:10px;font-size:10px;color:var(--text);line-height:1.5">`
    + `<span style="flex:1">${msg}</span>`
    + `<button title="Dismiss" onclick="try{localStorage.setItem('meridian.hooks.osbanner.dismissed','1')}catch(e: any){}; var _b=this.closest('[data-os-hint]'); if(_b)_b.remove();" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:13px;line-height:1;padding:0 2px;flex-shrink:0">×</button>`
    + `</div>`;
}

function showFailoverBannerIfNeeded() {
  // ITEM 7 — failover banner: poll /failover-status once on load; if the server
  // reports failover mode, show a dismissible yellow bar (sessionStorage dismiss).
  try { if (sessionStorage.getItem('meridian.failover.dismissed') === '1') return; } catch (e: any) {}
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
    btn.onclick = () => { try { sessionStorage.setItem('meridian.failover.dismissed', '1'); } catch (e: any) {} bar.remove(); };
    bar.appendChild(label);
    bar.appendChild(btn);
    document.body.insertBefore(bar, document.body.firstChild);
  }).catch(() => {});
}




// function _renderToolEntry -- moved to dashboard-mcp.js






function _removeHitlCard(id: any) {

  // 83b517d1 — optimistic UI: instantly drop a HITL card from the DOM the
  // moment its answer/dismiss/approve/reject PATCH succeeds, instead of
  // waiting on the follow-up render()/refreshHitl() background refresh to
  // reflect it — that's a real server re-fetch, and read-after-write lag can
  // still show the just-answered item as "pending" for 30-60s. Handles both
  // HITL card markups in this file: the per-project tab's `.hitl-row`
  // wrapper (the id lives on its nested buttons, not the row itself) and the
  // global hitl bar's `#hitl-list` card (the id lives on the wrapper itself).
  document.querySelectorAll(`[data-hitl-id="${id}"]`).forEach(el => {

    (el.closest('.hitl-row') || el).remove();

  });

}
window._removeHitlCard = _removeHitlCard;

async function loadHitlTab(projectId: any) {

  const body = document.getElementById(`hitl-body-${projectId}`);

  const statusFilter = document.getElementById(`hitl-status-filter-${projectId}`);

  const refreshBtn = document.getElementById(`hitl-refresh-${projectId}`);

  if (!body) return;



  const urgencyColor: Record<string, string> = { blocking: 'var(--red,#e05252)', high: 'var(--yellow,#d4a017)', normal: 'var(--muted)' };

  const statusBadge: Record<string, string> = { pending: '#f59e0b', answered: '#22c55e', dismissed: 'var(--muted)' };



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

      const pending = rows.filter((r: any) => r.status === 'pending');

      const resolved = rows.filter((r: any) => r.status !== 'pending');

      const renderDiff = (diffText: any) => {

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

      const renderCard = (r: any) => {

        const urg = r.urgency || 'normal';

        const st = r.status || 'pending';

        const dt = (r.created_at || '').slice(0, 16).replace('T', ' ');

        const isMd = r.kind === 'md_section_update';

        let pl = null;

        if (isMd && r.payload) { try { pl = JSON.parse(r.payload); } catch (e: any) { pl = null; } }

        const mdMeta = (isMd && pl) ? `<div style="margin-top:6px;font-size:10px;color:var(--accent)"><b>${escapeHtml(pl.file || '')}</b> § ${escapeHtml(pl.anchor || '')}</div>` : '';

        const diffHtml = (isMd && pl && pl.diff) ? renderDiff(pl.diff) : '';

        const answerHtml = r.answer ? `<div style="margin-top:8px;padding:6px 8px;background:var(--surface-1);border-radius:3px;border-left:3px solid #22c55e;color:var(--text);font-size:11px"><b>Answer:</b> ${escapeHtml(r.answer)}</div>` : '';

        const applyErr = r.apply_error ? `<div style="margin-top:6px;color:#e05252;font-size:10px"><b>Not applied:</b> ${escapeHtml(r.apply_error)}</div>` : '';

        const ctxHtml = r.context ? `<div style="margin-top:6px;color:var(--muted);font-size:11px;font-style:italic">${escapeHtml(r.context.slice(0, 200))}</div>` : '';

        // cd134cf1 — selectable answer options + a highlighted recommended default.
        let optPayload = null;
        try { optPayload = r.payload ? JSON.parse(r.payload) : null; } catch (e: any) { optPayload = null; }
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

          const optBtns = hitlOpts.map((o: any, i: any) => {
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

            // 83b517d1 — remove the card immediately; don't wait on the
            // background render() below to reflect a state that already
            // succeeded (read-after-write lag can leave it "pending" 30-60s).
            _removeHitlCard(id);

            render();

          } catch (e: any) { toast('failed: ' + e.message, true); }

        };

      });

      body.querySelectorAll('.hitl-opt-btn').forEach(btn => {

        btn.onclick = async () => {

          const id = btn.dataset.hitlId;

          const answer = btn.dataset.answer || '';

          try {

            await api(`/hitl/${id}`, { method: 'PATCH', body: JSON.stringify({ action: 'answer', answer }) });

            toast('answered ✓');

            // 83b517d1 — optimistic removal; see .hitl-answer-btn above.
            _removeHitlCard(id);

            render();

          } catch (e: any) { toast('failed: ' + e.message, true); }

        };

      });

      // cd134cf1 — keyboard: digits 1-9 pick an option, Enter picks recommended.
      body.querySelectorAll('.hitl-opts').forEach(box => {

        box.addEventListener('keydown', (e: any) => {

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

            // 83b517d1 — optimistic removal; see .hitl-answer-btn above.
            _removeHitlCard(btn.dataset.hitlId);

            render();

          } catch (e: any) { toast('failed: ' + e.message, true); }

        };

      });

      body.querySelectorAll('.hitl-approve-btn').forEach(btn => {

        btn.onclick = async () => {

          if (!confirm('Approve and write this markdown change? It will be committed at the next checkpoint.')) return;

          try {

            const res = await api(`/hitl/${btn.dataset.hitlId}`, { method: 'PATCH', body: JSON.stringify({ action: 'answer', answer: 'approved' }) });

            if (res && res.applied === false) toast('not applied: ' + (res.apply_error || 'see card'), true);

            else toast('approved ✓ — section written, staged for checkpoint');

            // 83b517d1 — optimistic removal; see .hitl-answer-btn above.
            _removeHitlCard(btn.dataset.hitlId);

            render();

          } catch (e: any) { toast('failed: ' + e.message, true); }

        };

      });

      body.querySelectorAll('.hitl-reject-btn').forEach(btn => {

        btn.onclick = async () => {

          if (!confirm('Reject this proposed change?')) return;

          try {

            await api(`/hitl/${btn.dataset.hitlId}`, { method: 'PATCH', body: JSON.stringify({ action: 'dismiss' }) });

            toast('rejected');

            // 83b517d1 — optimistic removal; see .hitl-answer-btn above.
            _removeHitlCard(btn.dataset.hitlId);

            render();

          } catch (e: any) { toast('failed: ' + e.message, true); }

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

    } catch (e: any) {

      body.innerHTML = `<div style="color:var(--muted)">failed to load HITL queue: ${escapeHtml(String(e))}</div>`;

    }

  };



  if (statusFilter) statusFilter.onchange = render;

  if (refreshBtn) refreshBtn.onclick = render;

  render();

}



async function loadTeamTab(projectId: any) {

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

      const dotColor: Record<string, string> = { active: '#4ade80', recent: '#fbbf24', idle: '#6b7280' };

      const cards = humans.map((h: any) => {

        const c = _colorForHuman(h.human_id);

        const dc = dotColor[h.presence] || dotColor.idle;

        const fw = (h.agent_framework && h.agent_framework !== 'claude_code')

          ? `<span style="background:var(--surface-2);color:var(--accent);font-size:9px;font-weight:600;padding:1px 5px;border-radius:3px;margin-left:4px">${escapeHtml(h.agent_framework)}</span>`

          : '';

        const tasksLine = `${h.tasks_done} done · ${h.tasks_pending} pending${h.tasks_failed ? ' · ' + h.tasks_failed + ' failed' : ''}`;

        const lastSeen = h.last_seen ? formatRelativeTime(h.last_seen) : 'never';

        const recent = (h.recent || []).slice(0, 3).map((t: any) => {

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

      let goalMarkers: any[] = [];

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

      const standup = humans.map((h: any) => {

        const c = _colorForHuman(h.human_id);

        const last = (h.recent || []).map((t: any) => (t.description || '').slice(0, 60)).slice(0, 4).join('; ');

        return `<div style="padding:3px 0;border-left:2px solid ${c};padding-left:8px;font-size:11px">

          <span style="color:${c};font-weight:600">${escapeHtml(h.human_id)}</span> · ${h.tasks_done} done — <span style="color:var(--muted)">${escapeHtml(last) || '—'}</span>

        </div>`;

      }).join('');



      // Active pinned decisions for standup context.

      let decisionsHtml = '';

      try {

        const pinned = await api(`/projects/${projectId}/decisions-pinned`);

        if (pinned && pinned.length) {

          const rows = pinned.slice(0, 8).map((d: any) => {

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

    } catch (e: any) {

      body.innerHTML = renderProjectLoadError(projectId, 'Team summary unavailable', `/team/summary?project_id=${encodeURIComponent(projectId)}&days=${days}`, e);

      wireProjectLoadRetry(body, projectId);

    }

  };



  if (daySel) daySel.onchange = render;

  if (refreshBtn) refreshBtn.onclick = render;

  render();

}



async function updateLiveFeed(projectId: any) {

  /**v2.3 — live "Currently Running" section at top of Queue tab.

   * Shows active session name + started_at + last 5 task_log entries.

   * Collapses when no active session exists. Polls every 5s. */

  const el = document.getElementById(`live-session-${projectId}`);

  if (!el) return;

  const panel = getPanelState(projectId);

  try {

    const sessions = await api(`/projects/${projectId}/sessions?active_only=true`);

    const active = sessions && sessions.filter((s: any) => s.status === 'active');

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

    const taskRows = (tasks || []).map((t: any) => {

      const icon = t.status === 'done' ? '✓' : t.status === 'failed' ? '✗' : t.status === 'in_progress' ? '▶' : '·';

      const color = t.status === 'done' ? 'var(--status-done)' : t.status === 'failed' ? 'var(--status-failed)' : t.status === 'in_progress' ? 'var(--accent)' : 'var(--muted)';

      const desc = (t.description || '').length > 80 ? t.description.slice(0, 80) + '…' : t.description;

      return `<div style="display:flex;gap:6px;align-items:baseline;padding:1px 0">

        <span style="color:${color};font-size:10px;flex-shrink:0">${icon}</span>

        <span style="color:var(--text);font-size:10px;word-break:break-word">${escapeHtml(desc || '')}</span>

      </div>`;

    }).join('');

    const extraCount = active.length - 1;

    const extraRows = active.slice(1).map((s: any) => {

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

  } catch(e: any) {

    panel.liveSessionId = null;

    el.style.display = 'none';

  }

}



async function loadRecentSessions(projectId: any, sessions = null) {

  /** Recent Sessions list for non-live sessions with a copyable start_session(). */

  const el = document.getElementById(`recent-sessions-${projectId}`);

  if (!el) return;

  try {

    const panel = getPanelState(projectId);

    const allSessions = Array.isArray(sessions)

      ? sessions

      : await api(`/projects/${projectId}/sessions?active_only=false`);

    const recent = sortSessionsMostRecentFirst(
      (allSessions || []).filter((s: any) => s.id !== panel.liveSessionId && !isLiveSession(s)),
    ).slice(0, 5);

    if (!recent.length) { el.style.display = 'none'; return; }

    el.innerHTML = `

      <div style="font-size:9px;font-weight:700;color:var(--muted);letter-spacing:.07em;text-transform:uppercase;margin-bottom:6px">Recent Sessions</div>

      ${recent.map((s: any) => {

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

      btn.onclick = (event: any) => {
        event.stopPropagation();

        const cmd = btn.dataset.cmd || '';

        navigator.clipboard.writeText(cmd).then(() => toast('Copied start_session() to clipboard')).catch(() => toast('copy failed', true));

      };

    });
    el.querySelectorAll('.recent-session-timeline-btn').forEach(btn => {
      btn.onclick = (event: any) => {
        event.stopPropagation();
        openTimelineForSession(projectId, btn.dataset.sessionId);
      };
    });
    el.querySelectorAll('.recent-session-row').forEach(row => {
      row.onclick = async (evt: any) => {
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
              ? taskRows.map((t: any) => `<div style="padding:2px 0"><span style="color:var(--accent)">${escapeHtml((t.status || '').toUpperCase())}</span> ${escapeHtml((t.description || '').slice(0, 180))}</div>`).join('')
              : '<div>(no task log for this session)</div>';
            target.innerHTML = summaryHtml + tasksHtml;
            target.dataset.loaded = '1';
          } catch(e: any) {
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



async function loadMilestones(projectId: any) {

  const el = document.getElementById(`milestones-strip-${projectId}`);

  if (!el) return;

  try {

    const all = await api(`/projects/${projectId}/sprint-items`);

    // Show explicit milestone items + any done/failed/skipped/pushed sprint items

    const doneStatuses = new Set(['done', 'skipped', 'failed', 'pushed']);

    const milestones = (all || []).filter((i: any) =>

      i.milestone_type === 'milestone' || doneStatuses.has(i.status)

    ).sort((a: any, b: any) => {

      // Done items first, then by completed_at desc

      const aTs = a.completed_at || a.added_at || '';

      const bTs = b.completed_at || b.added_at || '';

      return bTs.localeCompare(aTs);

    });

    if (!milestones.length) { el.style.display = 'none'; return; }

    const statusIcon = (s: any) => s === 'done' ? '✓' : s === 'failed' ? '✗' : s === 'pushed' ? '→' : s === 'skipped' ? '—' : s === 'in_progress' ? '▶' : '◦';

    const statusColor = (s: any) => s === 'done' ? 'var(--accent-green,#34d399)' : s === 'failed' ? '#e05' : s === 'pushed' ? 'var(--accent)' : s === 'in_progress' ? 'var(--accent)' : 'var(--muted)';

    el.innerHTML = `

      <div style="font-size:9px;font-weight:700;color:var(--muted);letter-spacing:.07em;text-transform:uppercase;margin-bottom:6px">Completed (${milestones.length})</div>

      <div style="display:flex;flex-wrap:wrap;gap:6px">

        ${milestones.slice(0, 20).map((m: any) => {

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



async function loadRecentRuns(projectId: any) {

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

    body.innerHTML = runs.map((run: any) => {

      const sid = (run.session_id || '').slice(0, 8);
      const runLabel = run.session_name || sid;

      const ts = (run.started_at || '').slice(0, 16).replace('T', ' ');

      // 59ab2f9f — the "live" fallback must match displayRunStatus's liveness
      // logic: cross-check the session's status AND a recency window (via
      // isLiveSession), not just the raw run.status. Otherwise a run whose
      // duration_s is null (never finalized) shows "live" forever even after
      // its session went inactive / the run went stale. Map the run onto the
      // SessionLike shape isLiveSession expects: session_status (same field
      // displayRunStatus checks) + started_at as the recency timestamp.
      const dur = run.duration_s != null

        ? (run.duration_s < 60 ? `${run.duration_s}s` : `${Math.round(run.duration_s / 60)}m`)

        : (isLiveSession({ status: run.session_status, last_seen: run.started_at }) ? 'live' : '—');

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



async function loadQueue(projectId: any) {

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

    const liveSession = (sessions || []).find((s: any) => isLiveSession(s));

    panel.liveSessionId = liveSession ? liveSession.id : null;

    const sprintPayload = sprintItems || [];
    panel.queueSprintItems = Array.isArray(sprintPayload) ? sprintPayload : (sprintPayload.items || []);
    panel.queueTotalDoneCount = Array.isArray(sprintPayload)
      ? panel.queueSprintItems.filter((it: any) => it.status === 'done').length
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

        let _searchTimer: any = null;

        searchInput.addEventListener('input', function() {

          clearTimeout(_searchTimer);

          const q = this.value.trim();

          _searchTimer = setTimeout(async () => {

            if (!q) { renderCurrentQueue(); return; }

            try {

              const results = await api(`/projects/${projectId}/search?q=${encodeURIComponent(q)}&limit=10`);

              body.innerHTML = renderSearchResults(q, results);

            } catch (e: any) { body.innerHTML = `<div class="empty">search failed: ${escapeHtml(e.message)}</div>`; }

          }, 300);

        });

      }

    }

  } catch (e: any) {

    body.innerHTML = renderProjectLoadError(projectId, 'Queue unavailable', `/projects/${projectId}/sprint-items`, e);

    wireProjectLoadRetry(body, projectId);

  }

}



async function runReconcile(projectId: any) {

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
        <button onclick="document.getElementById('reconcile-results-${projectId}')!.style.display='none'" style="margin-left:10px;background:none;border:none;color:var(--muted);cursor:pointer;font-size:10px">✕</button>`;

    } else {

      const n = data.matches.length;

      let html = `<div style="margin-bottom:6px;color:var(--warning,#f59e0b);font-weight:600">${n} item${n !== 1 ? 's' : ''} may already be shipped — verify before executing</div>`;

      data.matches.forEach((m: any) => {

        const confidence = m.confidence === 'high' ? '🔴 high' : '🟡 medium';

        const commits = (m.matching_commits || []).slice(0, 2).map((c: any) =>
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

      html += `<button onclick="document.getElementById('reconcile-results-${projectId}')!.style.display='none'" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:10px;margin-top:2px">Dismiss</button>`;

      container.innerHTML = html;

    }

  } catch (e: any) {

    container.innerHTML = `<span style="color:var(--danger,#ef4444)">Reconcile failed: ${escapeHtml(e.message)}</span>
      <button onclick="document.getElementById('reconcile-results-${projectId}')!.style.display='none'" style="margin-left:10px;background:none;border:none;color:var(--muted);cursor:pointer;font-size:10px">✕</button>`;

  } finally {

    if (btn) { btn.disabled = false; btn.textContent = 'reconcile'; }

  }

}


async function reconcileMarkDone(projectId: any, itemId: any, btnEl: any) {

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

  } catch (e: any) {

    btnEl.disabled = false;

    btnEl.textContent = 'Mark done';

    toast(`Failed: ${e.message}`, true);

  }

}


function renderSearchResults(query: any, results: any) {

  /** Render universal search results grouped by type. */

  if (!results || results.total === 0) {

    return `<div class="empty" style="color:var(--muted);padding:12px 14px">No results for "${escapeHtml(query)}"</div>`;

  }

  const section = (label: any, items: any, renderFn: any) => {

    if (!items || !items.length) return '';

    return `<div style="padding:10px 14px 0">

      <div style="font-size:9px;font-weight:700;color:var(--muted);letter-spacing:.07em;text-transform:uppercase;margin-bottom:6px">${label}</div>

      ${items.map(renderFn).join('')}

    </div>`;

  };

  const taskRow = (t: any) => `<div style="border:1px solid var(--border);border-radius:3px;padding:5px 8px;margin-bottom:4px;background:var(--surface-2)">

    <div style="display:flex;justify-content:space-between;gap:6px">

      <span style="font-size:11px;color:var(--text);font-family:var(--font-mono);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(t.description || '')}">${escapeHtml((t.description || '').slice(0, 100))}</span>

      <span style="font-size:9px;color:var(--muted);flex-shrink:0">${escapeHtml(t.status || '')}</span>

    </div>

  </div>`;

  const noteRow = (n: any) => `<div style="border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:0 3px 3px 0;padding:5px 8px;margin-bottom:4px;background:var(--surface-2)" title="${escapeHtml(n.body || '')}">

    <div style="font-size:11px;font-weight:600;color:var(--accent)" title="${escapeHtml(n.title || '')}">${escapeHtml((n.title || '').slice(0, 80))}</div>

    <div style="font-size:10px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escapeHtml((n.body || '').slice(0, 80))}</div>

  </div>`;

  const decisionRow = (d: any) => `<div style="border:1px solid var(--border);border-left:3px solid var(--warning,#fa0);border-radius:0 3px 3px 0;padding:5px 8px;margin-bottom:4px;background:var(--surface-2)" title="${escapeHtml(d.body || '')}">

    <div style="font-size:11px;font-weight:600;color:var(--text)" title="${escapeHtml(d.title || '')}">${escapeHtml((d.title || '').slice(0, 80))}</div>

    <div style="font-size:10px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escapeHtml((d.body || '').slice(0, 80))}</div>

  </div>`;

  const sprintRow = (s: any) => `<div style="border:1px solid var(--border);border-radius:3px;padding:5px 8px;margin-bottom:4px;background:var(--surface-2)">

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



function wireQueueSectionToggles(projectId: any) {

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



    const applyState = (collapsed: any, animate: any) => {

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

        const onEnd = (ev: any) => {

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

      header.onkeydown = (e: any) => {

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

window._queueAction = async function(taskId: any, action: any) {

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

  } catch(e: any) { toast('Action failed: ' + e.message, true); }

};















async function refreshTab(projectId: any) {

  await Promise.all([

    refreshGoal(projectId),

    refreshSessions(projectId),

    refreshTasks(projectId),

  ]);

}



async function refreshGoal(projectId: any) {

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

    v!.textContent = `v${goal.version}`;

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

  } catch (e: any) {

    ta.value = '';

    ta.placeholder = 'Goal state failed to load.';

    v!.textContent = '(load failed)';

    const titleEl = document.getElementById(`goal-title-${projectId}`);

    if (titleEl) titleEl.textContent = 'Goal state unavailable';

  }

}



function parseDecisionsBlob(blob: any) {

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



const _DECISION_CATEGORY_COLORS: Record<string, string> = {

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
const _DECISION_PRIORITY_COLORS: Record<string, string> = {

  urgent: '#f87171',

  normal: '#94a3b8',

  low:    '#64748b',

};

const _DECISION_PRIORITY_ORDER = ['urgent', 'normal', 'low'];



function renderConstitutionWarning(projectId: any) {

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

      } catch (e: any) { toast('archive failed: ' + e.message, true); }

    };

  }

}



// ---------------------------------------------------------------------------

// v2.4 — HITL (human-in-the-loop) queue panel

// ---------------------------------------------------------------------------



const _HITL_URGENCY_COLOR: Record<string, string> = {

  blocking: '#f87171',  // red — session paused, answer now

  high:     '#fbbf24',  // amber — should answer soon

  normal:   '#6c8fff',  // blue — nice-to-have

};



let _hitlPollTimer: any = null;



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

  // 20591f72 — fallback poll: WS push is the fast path but if the project panel
  // isn't open yet (no WS listener) the event is dropped silently.
  // 0b9b12c8 — 10s (was 60s) so a blocking HITL appears promptly even when WS
  // push is missed.
  if (_hitlPollTimer) clearInterval(_hitlPollTimer);
  _hitlPollTimer = setInterval(refreshHitl, 10000);

}



function setVtabCountBadge(selector: any, count: any) {

  /** G1.2 — single source of truth for vtab/gtab count chip display.

   * Used by HITL, Notes, and Decisions badges. */

  document.querySelectorAll(selector).forEach(badge => {

    badge.textContent = String(count);

    badge.style.display = count > 0 ? 'inline-block' : 'none';

  });

}



async function refreshProjectCountBadges(projectId: any) {

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

    const visible = (notesRes.value || []).filter((n: any) => {

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



// 0b9b12c8 — render recently auto-answered HITLs greyed out so the human can
// see what the auto-answer resolved vs. what's genuinely pending. Pure +
// exported for the UI test. Returns '' when there are none.
function _renderAutoAnsweredHitls(answered: any) {
  const auto = (answered || []).filter((r: any) => r && r.answered_by === 'auto').slice(0, 5);
  if (!auto.length) return '';
  const rows = auto.map((r: any) => {
    const ts = formatRelativeTime(r.answered_at || r.created_at);
    return `<div data-hitl-auto-id="${escapeHtml(r.id)}" style="opacity:0.55;border-left:3px solid var(--muted);background:var(--surface-1);padding:8px 12px;margin-bottom:6px;border-radius:0 4px 4px 0">
        <div style="display:flex;justify-content:space-between;gap:8px;margin-bottom:4px">
          <span style="background:var(--surface-2);color:var(--muted);font-size:9px;font-weight:700;letter-spacing:.5px;padding:2px 6px;border-radius:3px">AUTO-ANSWERED</span>
          <span style="color:var(--muted);font-size:10px">${escapeHtml(ts)}</span>
        </div>
        <div style="color:var(--text);white-space:pre-wrap;word-break:break-word;font-size:11px;margin-bottom:3px">${escapeHtml(r.question || '')}</div>
        <div style="color:var(--muted);font-size:11px"><span style="font-weight:600">→</span> ${escapeHtml(r.answer || '')}</div>
      </div>`;
  }).join('');
  return `<div data-hitl-auto-section style="margin:10px 0 4px;font-size:9px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">Recently auto-answered</div>${rows}`;
}
window._renderAutoAnsweredHitls = _renderAutoAnsweredHitls;

async function refreshHitl(_pid?: any) {

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

      document.getElementById('hitl-panel')!.style.display = 'none';

      return;

    }

    bar.style.display = 'flex';

    list.innerHTML = items.map((r: any) => {

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

      inp.onkeydown = (ev: any) => {

        if (ev.key === 'Enter') _hitlAnswer(inp.dataset.hitlId);

      };

    });

    // 0b9b12c8 — append a greyed "recently auto-answered" section (best-effort).
    try {

      const answered = await api('/hitl?status=answered&limit=10');

      const autoHtml = _renderAutoAnsweredHitls(answered);

      if (autoHtml) list.insertAdjacentHTML('beforeend', autoHtml);

    } catch (e: any) {

      console.error('[meridian] auto-answered HITL fetch failed:', e);

    }

  } catch (e: any) {

    // 0b9b12c8 — don't toast every poll (noisy when offline), but DO log to the
    // console so a non-200 /hitl (HITLs silently never appearing) is debuggable
    // in devtools instead of vanishing.
    console.error('[meridian] refreshHitl failed — HITL bar may be stale:', e);

  }

}



async function _hitlAnswer(id: any) {

  const inp = document.querySelector(`.hitl-answer-input[data-hitl-id="${id}"]`);

  const answer = (inp && inp.value || '').trim();

  if (!answer) { toast('answer required', true); return; }

  try {

    await api(`/hitl/${id}`, {

      method: 'PATCH',

      body: JSON.stringify({ action: 'answer', answer }),

    });

    toast('HITL answered');

    // 83b517d1 — remove the card immediately; the follow-up refreshHitl()
    // below is a reconciling background refresh, not the source of truth
    // for "did this disappear" (server read-after-write can lag 30-60s).
    _removeHitlCard(id);

    refreshHitl();

  } catch (e: any) { toast('answer failed: ' + e.message, true); }

}



async function _hitlDismiss(id: any) {

  if (!confirm('Dismiss this HITL request without answering?')) return;

  try {

    await api(`/hitl/${id}`, {

      method: 'PATCH',

      body: JSON.stringify({ action: 'dismiss' }),

    });

    // 83b517d1 — optimistic removal; see _hitlAnswer above.
    _removeHitlCard(id);

    refreshHitl();

  } catch (e: any) { toast('dismiss failed: ' + e.message, true); }

}



async function loadPinnedDecisions(projectId: any, { showArchived = false } = {}) {

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
      : (allItems || []).filter((d: any) => d.status !== 'superseded');

    getPanelState(projectId)._pinnedDecisions = items || [];

    setVtabCountBadge(`.decisions-gtab-badge[data-pid="${projectId}"]`, (items || []).length);

    renderConstitutionWarning(projectId);

    if (!items || items.length === 0) {

      host.innerHTML = `<div style="color:var(--muted);padding:10px;text-align:center;border:1px dashed var(--border);border-radius:4px">(no pinned decisions yet — call <code>pin_decision</code> from MCP)</div>`;

      return;

    }

    host.innerHTML = items.map((d: any) => {

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



    const showEdit = (id: any) => {

      const card = host.querySelector(`[data-decision-card="${id}"]`);

      if (!card) return;

      card.querySelector('.decision-body-view')!.style.display = 'none';

      card.querySelector('.decision-title-view')!.style.display = 'none';

      card.querySelector('.decision-edit-area')!.style.display = 'block';

    };

    const hideEdit = (id: any) => {

      const card = host.querySelector(`[data-decision-card="${id}"]`);

      if (!card) return;

      card.querySelector('.decision-body-view')!.style.display = '';

      card.querySelector('.decision-title-view')!.style.display = '';

      card.querySelector('.decision-edit-area')!.style.display = 'none';

    };



    host.querySelectorAll('.decision-body-view, .decision-title-view').forEach(el => {

      el.onclick = () => showEdit(el.dataset.id);

    });

    // Category tag — inline dropdown to change category
    const _CATS = ['TECHNICAL','STRATEGIC','ARCHITECTURAL','PRODUCT','TACTICAL','BUSINESS','COMPETITIVE'];
    host.querySelectorAll('.decision-cat-tag').forEach(tag => {
      tag.onclick = (e: any) => {
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
          } catch(err: any) { toast('category update failed: ' + err.message, true); }
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

        const newTitle = card!.querySelector('.decision-edit-title')!.value.trim();

        const newBody = card!.querySelector('.decision-edit-body')!.value.trim();

        if (!newTitle || !newBody) return toast('title and body required', true);

        try {

          await api(`/projects/${pid}/decisions-pinned/${id}`, {

            method: 'PATCH',

            body: JSON.stringify({ title: newTitle, body: newBody }),

          });

          toast('decision saved');

          loadPinnedDecisions(pid);

        } catch (e: any) { toast('save failed: ' + e.message, true); }

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

        } catch (e: any) { toast('priority change failed: ' + e.message, true); }

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

        } catch (e: any) { toast('archive failed: ' + e.message, true); }

      };

    });

    // View archived toggle — wired into the placeholder div we added to host.innerHTML
    const toggleEl = document.getElementById(`decisions-view-archived-${projectId}`);
    if (toggleEl) {
      if (showArchived) {
        const archivedCount = (allItems || []).filter((d: any) => d.status === 'superseded').length;
        toggleEl.innerHTML = `<button class="secondary" style="padding:2px 8px;font-size:10px" onclick="loadPinnedDecisions('${escapeHtml(projectId)}', {showArchived:false})">← Hide archived</button> <span style="color:var(--muted)">${archivedCount} archived</span>`;
      } else {
        api(`/projects/${projectId}/decisions-pinned?include_superseded=true`).then(all => {
          const n = (all || []).filter((d: any) => d.status === 'superseded').length;
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

      host.parentElement!.insertBefore(supersededEl, host.nextSibling);

    }

    try {

      const all = await api(`/projects/${projectId}/decisions-pinned?include_superseded=true`);

      const superseded = (all || []).filter((d: any) => d.status === 'superseded');

      if (superseded.length > 0) {

        supersededEl.innerHTML = `<details style="margin-top:8px;margin-bottom:6px">

          <summary style="cursor:pointer;color:var(--muted);font-size:10px;font-family:var(--font-mono);letter-spacing:.05em;user-select:none">

            Superseded (${superseded.length})

          </summary>

          <div style="margin-top:6px">

            ${superseded.map((d: any) => {

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

  } catch (e: any) {

    if (state.panels[projectId]) state.panels[projectId]._pinnedDecisions = [];

    renderConstitutionWarning(projectId);

    host.innerHTML = `<div style="color:var(--muted)">failed to load pinned decisions: ${escapeHtml(String(e))}</div>`;

  }

}



async function supersedePinnedDecision(projectId: any, decisionId: any) {

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

  } catch (e: any) { toast('supersede failed: ' + e.message, true); }

}



async function addPinnedDecision(projectId: any) {

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

  } catch (e: any) { toast('pin failed: ' + e.message, true); }

}



async function consolidateDecisions(projectId: any) {

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

  document.getElementById('_consolidate-cancel')!.onclick = () => overlay.remove();



  document.getElementById('_consolidate-run')!.onclick = async () => {

    const apiKey = document.getElementById('_consolidate-key')!.value.trim();

    const model = document.getElementById('_consolidate-model')!.value;

    if (!apiKey) { toast('API key required', true); return; }

    const runBtn = document.getElementById('_consolidate-run');

    runBtn!.textContent = 'Working…'; runBtn!.disabled = true;

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

      const previewHtml = consolidated.map((d: any) => {

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

      document.getElementById('_preview-cancel')!.onclick = () => previewOverlay.remove();

      document.getElementById('_preview-apply')!.onclick = async () => {

        const applyBtn = document.getElementById('_preview-apply');

        applyBtn!.textContent = 'Applying…'; applyBtn!.disabled = true;

        try {

          await api(`/projects/${projectId}/decisions-pinned/replace-all`, {

            method: 'POST',

            body: JSON.stringify({ decisions: consolidated }),

          });

          previewOverlay.remove();

          toast(`Consolidated: ${consolidated.length} decisions applied`);

          loadPinnedDecisions(projectId);

        } catch (e: any) { toast('Apply failed: ' + e.message, true); applyBtn!.textContent = 'Apply →'; applyBtn!.disabled = false; }

      };

    } catch (e: any) {

      runBtn!.textContent = 'Consolidate →'; runBtn!.disabled = false;

      toast('Consolidation failed: ' + e.message, true);

    }

  };

}



function renderDecisionsTable(projectId: any, blob: any) {

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



function wireGoalPreviewToggle(taEl: any, previewEl: any) {

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



async function saveGoal(projectId: any) {

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

    try { content = JSON.parse(raw); } catch(e: any) { /* fall back to string */ }

  }

  try {

    await api(`/projects/${projectId}/goal`, { method: 'POST', body: JSON.stringify({ content }) });

    state.panels[projectId]._lastSaved = raw;

    toast('version goal saved');

    refreshGoal(projectId);

  } catch (e: any) {

    toast('save failed: ' + e.message, true);

  }

}



async function saveNorthStar(projectId: any) {

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

  } catch (e: any) {

    toast('save failed: ' + e.message, true);

  }

}



async function saveSprint(projectId: any) {

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

  } catch (e: any) {

    toast('save failed: ' + e.message, true);

  }

}



function _sessionPresenceDot(last_seen: any) {

  if (!last_seen) return '⚫';

  const mins = (Date.now() - new Date(last_seen.replace(' ', 'T') + 'Z').getTime()) / 60000;

  if (mins < 6) return '🟢';

  if (mins < 30) return '🟡';

  return '⚫';

}



async function refreshSessions(projectId: any) {

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

    const groups: Record<string, any> = {};

    const order = [];

    for (const s of sessions) {

      const h = s.human_id || '\x00unknown';

      if (!groups[h]) { groups[h] = []; order.push(h); }

      groups[h].push(s);

    }

    for (const h of order) {

      groups[h] = sortSessionsMostRecentFirst(groups[h]);

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

      const children = humanSessions.map((s: any) => {

        let ageMs = 0;

        try {

          const ts = s.last_seen ? s.last_seen.replace(' ', 'T') + 'Z' : '';

          if (ts) ageMs = Date.now() - new Date(ts).getTime();

        } catch(e: any) {}

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

  } catch(e: any) {

    root.innerHTML = renderProjectLoadError(projectId, 'Sessions unavailable', sessionsPath, e);

    wireProjectLoadRetry(root, projectId);

  }

}



async function refreshTasks(projectId: any) {

  const tasksPath = `/projects/${projectId}/tasks?limit=100`;

  try {

    const tasks = await projectApi(projectId, tasksPath);

    state.panels[projectId].taskCache = tasks;

    state.panels[projectId].taskOffset = tasks.length;

    renderTasks(projectId);

  } catch(e: any) {

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



function renderTasks(projectId: any) {

  const tasks = state.panels[projectId].taskCache || [];

  const root = document.getElementById(`tasks-${projectId}`);

  const hitlRoot = document.getElementById(`hitl-queue-${projectId}`);

  const banner = document.getElementById(`hitl-banner-${projectId}`);

  if (!root || !hitlRoot) return;

  const hitl = tasks.filter((t: any) => t.status === 'pending-hitl');

  banner!.style.display = hitl.length ? 'block' : 'none';

  hitlRoot.innerHTML = hitl.map((t: any) => renderHitlRow(projectId, t)).join('');

  hitl.forEach((t: any) => wireHitlRow(projectId, t));

  root.innerHTML = tasks.map((t: any) => renderTaskRow(t)).join('');

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

    root.parentElement!.appendChild(btn);

  }

}



async function _loadMoreTasks(projectId: any, btn: any) {

  const p = state.panels[projectId];

  const offset = p.taskOffset || 0;

  btn.disabled = true;

  btn.textContent = 'loading…';

  try {

    const more = await api(`/projects/${projectId}/tasks?limit=100&offset=${offset}`);

    p.taskCache = [...(p.taskCache || []), ...more];

    p.taskOffset = offset + more.length;

    const root = document.getElementById(`tasks-${projectId}`);

    if (root) root.innerHTML += more.map((t: any) => renderTaskRow(t)).join('');

    if (more.length < 100) {

      btn.remove();

    } else {

      btn.disabled = false;

      btn.textContent = 'Load 100 more ↓';

    }

  } catch(e: any) {

    btn.disabled = false;

    btn.textContent = 'Load 100 more ↓ (retry)';

  }

}



function renderTaskRow(t: any) {

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



async function deleteTaskRow(e: any, taskId: any, status: any) {

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



function renderHitlRow(projectId: any, t: any) {

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



function wireHitlRow(projectId: any, t: any) {

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



async function appendToGoal(projectId: any, line: any) {

  // Pull latest goal, append, push back. String-only for HITL markers.

  let current = '';

  try {

    const goal = await api(`/projects/${projectId}/goal`);

    current = typeof goal.content === 'string' ? goal.content : JSON.stringify(goal.content, null, 2);

  } catch(e: any) { /* unset goal is fine */ }

  const next = current ? current.trimEnd() + '\n' + line : line;

  await api(`/projects/${projectId}/goal`, { method: 'POST', body: JSON.stringify({ content: next }) });

}



async function hitlReply(projectId: any, taskId: any, text: any) {

  try {

    await appendToGoal(projectId, `[HITL-REPLY:${taskId}:] ${text}`);

    await api(`/tasks/${taskId}`, { method: 'PATCH', body: JSON.stringify({ status: 'done', description: `[ANSWERED] ${text}` }) });

    toast('reply sent');

  } catch(e: any) { toast('reply failed: ' + e.message, true); }

}



async function hitlExecute(projectId: any, taskId: any, confirmed: any) {

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

  } catch(e: any) { toast('execute failed: ' + e.message, true); }

}



function connectWs(projectId: any) {

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

    } catch(e: any){}

  };

  state.panels[projectId].ws = ws;

}



function handleWsEvent(projectId: any, event: any) {

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

    const i = cache.findIndex((t: any) => t.id === event.task.id);

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



document.getElementById('new-project-btn')!.onclick = async () => {

  const inp = document.getElementById('new-project-name');

  const humanInp = document.getElementById('new-project-human');

  const name = inp!.value.trim();

  if (!name) return;

  const body: any = { name };

  const humanId = (humanInp && humanInp.value || '').trim();

  if (humanId) body.human_id = humanId;

  try {

    const p = await api('/projects', { method: 'POST', body: JSON.stringify(body) });

    inp!.value = '';

    if (humanInp) humanInp.value = '';

    await loadProjects();

    openTab(p);

  } catch(e: any) { toast('create failed: ' + e.message, true); }

};



// Sidebar dropdown — switch active project (opens the tab if not

// already open, otherwise just activates it).

{

  const switcher = document.getElementById('project-switcher');

  if (switcher) {

    switcher.addEventListener('change', (ev) => {

      const id = ev.target!.value;

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
  try { preferred = localStorage.getItem(STORAGE_KEY(ACTIVE_PROJECT_KEY)); } catch(e: any) {}

  try { saved = JSON.parse(localStorage.getItem(STORAGE_KEY(TABS_KEY)) || '[]'); } catch(e: any){}

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
      const first = wss && wss.find((w: any) => !w.is_own);
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

    document.getElementById('ez-wizard')!.style.display = 'flex';

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

        } catch(e: any) {

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



const _sprintBoardReloaders: Record<string, any> = {};

const _sprintSelectSyncers: Record<string, any> = {};



async function _deleteSprintItem(projectId: any, itemId: any) {

  if (!confirm('Remove this sprint item?')) return;

  try {

    await api(`/projects/${projectId}/sprint-items/${itemId}`, { method: 'DELETE' });

    if (_sprintBoardReloaders[projectId]) _sprintBoardReloaders[projectId]();

  } catch(e: any) { console.error('Delete sprint item failed:', e); }

}



async function _sprintAction(projectId: any, itemId: any, action: any) {

  try {

    await api(`/projects/${projectId}/sprint-items/${itemId}/${action}`, { method: 'POST' });

    if (_sprintBoardReloaders[projectId]) _sprintBoardReloaders[projectId]();

  } catch(e: any) { console.error('Sprint action failed:', action, e); }

}



async function completeSprintItem(projectId: any, itemId: any) {

  try {

    await api(`/projects/${projectId}/sprint-items/${itemId}/complete`, { method: 'POST' });

    if (_sprintBoardReloaders[projectId]) _sprintBoardReloaders[projectId]();

  } catch(e: any) { console.error('Complete sprint item failed:', e); }

}



async function failSprintItem(projectId: any, itemId: any) {

  try {

    await api(`/projects/${projectId}/sprint-items/${itemId}/fail`, { method: 'POST' });

    if (_sprintBoardReloaders[projectId]) _sprintBoardReloaders[projectId]();

  } catch(e: any) { console.error('Fail sprint item failed:', e); }

}





// --- v0.6.6 EZ wizard ---

// v0.6.6 — EZ wizard logic

document.getElementById('ez-create-btn')!.onclick = async () => {

  const nameEl = document.getElementById('ez-project-name');

  const humanEl = document.getElementById('ez-human-name');

  const errEl = document.getElementById('ez-error');

  const name = nameEl!.value.trim();

  if (!name) { errEl!.textContent = 'project name is required'; errEl!.style.display = 'block'; return; }

  errEl!.style.display = 'none';

  try {

    const body: any = { name };

    if (humanEl!.value.trim()) body.human_id = humanEl!.value.trim();

    const p = await api('/projects', { method: 'POST', body: JSON.stringify(body) });

    document.getElementById('ez-wizard')!.style.display = 'none';

    await loadProjects();

    await restoreTabs();

    openTab(p);

  } catch(e: any) { errEl!.textContent = 'create failed: ' + e.message; errEl!.style.display = 'block'; }

};

document.getElementById('ez-project-name')!.addEventListener('keydown', (e: any) => {

  if (e.key === 'Enter') document.getElementById('ez-create-btn')!.click();

});

document.getElementById('ez-advanced-link')!.onclick = (e) => {

  e.preventDefault();

  document.getElementById('ez-wizard')!.style.display = 'none';

  // Show the sidebar new-project form and focus it

  document.getElementById('new-project-name')!.focus();

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



  function showErr(msg: any) { if (errEl) { errEl.textContent = msg; errEl.style.display = msg ? 'block' : 'none'; } }



  document.addEventListener('keydown', (e: any) => {

    if (e.key === 'Escape') {

      const m = document.getElementById('conn-setup-modal');

      if (m && m.style.display !== 'none') m.style.display = 'none';

    }

  });



  window._showConnSetupIfNeeded = (cfg: any) => {

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



  function setActiveBtn(which: any) {

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

    } catch(e: any) {

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

    } catch(e: any) {

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






function toggleExpand(id: any) {

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
try { Object.assign(window, { loadCodeIntelTab, _initCodeIntelTabVisibility, hideHostedAdminControls, ensureSignOutLink, ensureWorkspaceSwitcher, getActiveWorkspaceRole, showConnectDbModal, showLocalServerControls, _summarizeApiErrorText, _projectLoadErrorInfo, wireProjectLoadRetry, renderProjectLoadError, recordProjectLoadError, clearProjectLoadError, renderProjectLoadAlert, retryProjectSurface, syncSidebarActiveProject, autosizeGoalField, githubIconSvg, getConstitutionLimit, loadProjectSettings, saveProjectSettings, loadExecutorRulesSection, loadTunnelPluginsSection, _demoTourDone, _demoTourSavedStep, _demoTourSaveStep, _demoTourMarkDone, _demoTourClose, _tourActivateVtab, startDemoTour, resumeDemoTour, api, projectApi, loadServerConfig, _armAccountSwitchWatch, _refreshOnFocus, _checkAccountSwitch, _showAccountSwitchBanner, updateGitHubConnectionIndicator, _updateConnectionIndicator, checkGitStatus, _doRestart, loadConfig, loadProjects, _makeProjectItem, openTab, closeTab, saveTabs, renderTabs, _makeTabEl, _openTabMenu, _setProjectIcon, _renameProject, _makeSubproject, _detachSubproject, _deleteProject, activateTab, buildTabBody, scheduleLiveRefresh, initLiveAutoRefresh, loadLiveTab, refreshLiveTab, wireSprintAddEnter, sprintAction, sprintArchive, filterBackburner, sprintPushPrompt, sprintFeedback, sprintFeedbackNote, sprintItemEdit, addSprintItemFromInput, cacheMostRecentSession, renderLiveSessions, endLiveSession, openTimelineForSession, renderLiveQueue, addLiveTask, cancelLiveTask, showCopyPreview, wireClaudeLaunchPanel, stampHandoffTs, populateSessionDropdown, loadTimeline, _renderTimelineLog, loadDocsTab, normalizeNotifyTarget, displayNotifyTarget, osExecutorHintBanner, showFailoverBannerIfNeeded, suggestNtfyTopic, loadHitlTab, loadTeamTab, updateLiveFeed, loadRecentSessions, loadMilestones, loadRecentRuns, loadQueue, renderSearchResults, wireQueueSectionToggles, refreshTab, refreshGoal, parseDecisionsBlob, renderConstitutionWarning, _hitlBadgeClick, initHitlPanel, setVtabCountBadge, refreshProjectCountBadges, refreshHitl, _hitlAnswer, _hitlDismiss, loadPinnedDecisions, supersedePinnedDecision, addPinnedDecision, consolidateDecisions, renderDecisionsTable, wireGoalPreviewToggle, saveGoal, saveNorthStar, saveSprint, _sessionPresenceDot, refreshSessions, refreshTasks, renderTasks, _loadMoreTasks, renderTaskRow, deleteTaskRow, renderHitlRow, wireHitlRow, appendToGoal, hitlReply, hitlExecute, connectWs, handleWsEvent, restoreTabs, _deleteSprintItem, _sprintAction, completeSprintItem, failSprintItem, toggleExpand, flattenHierarchy, eligibleParents, state }); } catch (e: any) {}
