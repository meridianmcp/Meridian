// dashboard-utils.js — utility functions extracted from dashboard.js
// Loaded first; all symbols are plain globals (no import/export).

export const _PLAN_LABELS = { solo: 'Standard', free: 'Free Trial', standard: 'Standard', pro: 'Pro', trial: 'Trial', admin: 'Admin' };

export const QUEUE_DONE_PAGE_SIZE = 10;
export const SESSION_LIVE_WINDOW_MS = 10 * 60 * 1000;
export const DEFAULT_MAX_PINNED_DECISIONS = 20;
export const DEFAULT_CONTEXT_THRESHOLD = 40;

export function getPanelState(projectId) {

  window.state.panels[projectId] = window.state.panels[projectId] || {};

  return window.state.panels[projectId];

}

export function toast(msg, isError=false) {

  const el = document.getElementById('toast');

  el.textContent = msg;

  el.classList.toggle('error', isError);

  el.classList.add('show');

  clearTimeout(toast._t);

  toast._t = setTimeout(() => el.classList.remove('show'), 2600);

}

export function escapeHtml(s) {

  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

}

export function formatRelativeTime(ts) {

  if (!ts) return '';

  const iso = ts.includes('T') ? ts : ts.replace(' ', 'T') + 'Z';

  const then = new Date(iso);

  const seconds = Math.max(0, Math.floor((Date.now() - then.getTime()) / 1000));

  if (seconds < 60) return `${seconds}s ago`;

  const minutes = Math.floor(seconds / 60);

  if (minutes < 60) return `${minutes}m ago`;

  const hours = Math.floor(minutes / 60);

  if (hours < 24) return `${hours}h ago`;

  const days = Math.floor(hours / 24);

  return `${days}d ago`;

}

export function sessionAgeMs(session) {
  const raw = session && session.last_seen ? String(session.last_seen) : '';
  if (!raw) return Number.POSITIVE_INFINITY;
  const iso = raw.includes('T') ? raw : raw.replace(' ', 'T') + 'Z';
  const parsed = new Date(iso).getTime();
  return Number.isFinite(parsed) ? Date.now() - parsed : Number.POSITIVE_INFINITY;
}

export function isLiveSession(session, ageMs) {
  const age = ageMs == null ? sessionAgeMs(session) : ageMs;
  return session && session.status === 'active' && age >= 0 && age <= SESSION_LIVE_WINDOW_MS;
}

export const _HUMAN_COLORS = ['#6c8fff', '#a78bfa', '#22d3ee', '#4ade80', '#fbbf24', '#f87171', '#fb923c', '#e879f9'];

export function _colorForHuman(humanId) {

  /** Stable hash → palette index so each human keeps the same activity color. */

  let h = 0;

  for (let i = 0; i < (humanId || '').length; i++) h = ((h << 5) - h + humanId.charCodeAt(i)) | 0;

  return _HUMAN_COLORS[Math.abs(h) % _HUMAN_COLORS.length];

}



// --- ITEM 4 esbuild: re-expose top-level symbols as globals so inline
// handlers and cross-file references keep resolving after IIFE bundling.
try { Object.assign(window, { getPanelState, toast, escapeHtml, formatRelativeTime, sessionAgeMs, isLiveSession, _colorForHuman, _PLAN_LABELS, QUEUE_DONE_PAGE_SIZE, SESSION_LIVE_WINDOW_MS, _HUMAN_COLORS, DEFAULT_MAX_PINNED_DECISIONS, DEFAULT_CONTEXT_THRESHOLD }); } catch (e) {}
