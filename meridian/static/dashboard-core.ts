// dashboard-core.js — Core API layer for the Meridian dashboard.
// Extracted from dashboard.js (sprint item ea0eda34).
// Provides: api(), projectApi(), and the _staleProjectsHandled cache.
// All other modules call api() as a global; this module exposes it on window.
//
// Dependencies (resolved at runtime via window globals):
//   - window.state (set in dashboard.js before this runs)
//   - isDemoMode() — dashboard-demo.js
//   - showDemoReadonlyToast() — dashboard-demo.js
//   - closeTab() — dashboard.js
//   - _checkAccountSwitch() — dashboard.js
//   - clearProjectLoadError(), recordProjectLoadError() — dashboard.js

// ---------------------------------------------------------------------------
// bb16f9a7 — connection-health indicator.
// The dashboard has no persistent SSE/WebSocket to the server; it polls REST via
// api()/fetch(). We drive a small header dot off that traffic: every successful
// api() call marks the connection healthy (green) and stamps the time; a failed
// call (network error / 5xx) marks it degraded (red). A cheap interval flips the
// dot amber when no successful call has landed within the stale window — i.e. the
// UI is idle or a poll is hanging — without opening any new connection.
// ---------------------------------------------------------------------------

const _HEALTH_STALE_MS = 45000;   // amber if no success within this window
let _lastApiOkAt = 0;             // epoch ms of the last successful api() call
let _lastApiFailed = false;       // true when the most recent api() call errored

function _setHealthDot(color: string, title: string): void {
  const dot = document.getElementById('connection-health-dot');
  if (!dot) return;
  dot.style.background = color;
  dot.title = title;
}

/** Recompute the health dot from the last api() outcome + staleness. */
function _refreshHealthDot(): void {
  if (_lastApiFailed) {
    _setHealthDot('#ef4444', 'Disconnected — last request failed');
    return;
  }
  if (_lastApiOkAt === 0) {
    _setHealthDot('#6b7280', 'Connecting…');
    return;
  }
  const age = Date.now() - _lastApiOkAt;
  if (age > _HEALTH_STALE_MS) {
    _setHealthDot('#f59e0b', 'Idle / stale — no recent server response');
  } else {
    _setHealthDot('#22c55e', 'Connected');
  }
}
window._refreshHealthDot = _refreshHealthDot;

function _markApiOk(): void {
  _lastApiOkAt = Date.now();
  _lastApiFailed = false;
  _refreshHealthDot();
}

function _markApiFail(): void {
  _lastApiFailed = true;
  _refreshHealthDot();
}

// Periodic re-evaluation so the dot goes amber during idle periods even when no
// api() call is in flight. Guarded so tests / repeated loads don't stack timers.
if (typeof window !== 'undefined' && !(window as any)._healthDotTimer) {
  (window as any)._healthDotTimer = setInterval(_refreshHealthDot, 5000);
}

// ---------------------------------------------------------------------------
// Core fetch wrapper — adds workspace tenant header + demo-mode 403 handling.
// ---------------------------------------------------------------------------

async function api(path: string, opts: RequestInit = {}): Promise<any> {
  const state = window.state || {};
  const headers: Record<string, string> = {'Content-Type': 'application/json'};
  if (state.activeWorkspaceTenantId) {
    headers['X-Workspace-Tenant-Id'] = state.activeWorkspaceTenantId;
  }
  let r: Response;
  try {
    r = await fetch(path, { headers, ...opts });
  } catch (netErr) {
    // Network-level failure (server down / offline) — flip the health dot red.
    _markApiFail();
    throw netErr;
  }
  if (!r.ok) {
    if (r.status === 403 && (typeof isDemoMode === 'function' ? isDemoMode() : false)) {
      if (typeof showDemoReadonlyToast === 'function') showDemoReadonlyToast();
      throw new Error('demo_readonly');
    }
    // 4xx are client/permission errors, not connectivity loss; only 5xx (and
    // network errors above) count as an unhealthy connection.
    if (r.status >= 500) _markApiFail(); else _markApiOk();
    const text = await r.text();
    // Augmented Error (extra fields consumed by projectApi + callers).
    const err: any = new Error(`${r.status}: ${text}`);
    err.status = r.status;
    err.endpoint = path;
    err.responseText = text;
    throw err;
  }
  _markApiOk();
  return r.status === 204 ? null : r.json();
}
window.api = api;

// ---------------------------------------------------------------------------
// Project-scoped fetch wrapper — records errors into the project load-error
// surface and auto-closes stale tabs when the active account changes.
// ---------------------------------------------------------------------------

const _staleProjectsHandled = new Set();
window._staleProjectsHandled = _staleProjectsHandled;

async function projectApi(projectId: string, path: string, opts: RequestInit = {}): Promise<any> {
  const state = window.state || {};
  try {
    const data = await api(path, opts);
    if (typeof clearProjectLoadError === 'function') clearProjectLoadError(projectId, path);
    return data;
  } catch (e: any) {
    // Self-heal stale tabs: a "project not found" 404 for a project that isn't
    // in the current account's project list means the signed-in account changed
    // (often in another tab). Close the orphaned tab and prompt a refresh once,
    // instead of spamming every panel with 404s until the user reloads.
    if (e && e.status === 404 && /project not found/i.test(e.responseText || '')
        && !(state.projects || []).some((p: any) => p.id === projectId)
        && !_staleProjectsHandled.has(projectId)) {
      _staleProjectsHandled.add(projectId);
      try { if (typeof closeTab === 'function') closeTab(projectId); } catch (_) {}
      try { if (typeof _checkAccountSwitch === 'function') _checkAccountSwitch(); } catch (_) {}
      throw e;
    }
    if (typeof recordProjectLoadError === 'function') recordProjectLoadError(projectId, path, e);
    throw e;
  }
}
window.projectApi = projectApi;

try { Object.assign(window, { api, projectApi, _staleProjectsHandled }); } catch (e) {}
