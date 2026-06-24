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
// Core fetch wrapper — adds workspace tenant header + demo-mode 403 handling.
// ---------------------------------------------------------------------------

async function api(path, opts={}) {
  const state = window.state || {};
  const headers = {'Content-Type': 'application/json'};
  if (state.activeWorkspaceTenantId) {
    headers['X-Workspace-Tenant-Id'] = state.activeWorkspaceTenantId;
  }
  const r = await fetch(path, { headers, ...opts });
  if (!r.ok) {
    if (r.status === 403 && (typeof isDemoMode === 'function' ? isDemoMode() : false)) {
      if (typeof showDemoReadonlyToast === 'function') showDemoReadonlyToast();
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
window.api = api;

// ---------------------------------------------------------------------------
// Project-scoped fetch wrapper — records errors into the project load-error
// surface and auto-closes stale tabs when the active account changes.
// ---------------------------------------------------------------------------

const _staleProjectsHandled = new Set();
window._staleProjectsHandled = _staleProjectsHandled;

async function projectApi(projectId, path, opts={}) {
  const state = window.state || {};
  try {
    const data = await api(path, opts);
    if (typeof clearProjectLoadError === 'function') clearProjectLoadError(projectId, path);
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
