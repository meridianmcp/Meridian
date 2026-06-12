// dashboard-utils.js — utility functions extracted from dashboard.js
// Loaded first; all symbols are plain globals (no import/export).

const _PLAN_LABELS = { solo: 'Standard', free: 'Free Trial', standard: 'Standard', pro: 'Pro', trial: 'Trial', admin: 'Admin' };

const QUEUE_DONE_PAGE_SIZE = 10;
const SESSION_LIVE_WINDOW_MS = 10 * 60 * 1000;

function getPanelState(projectId) {

  state.panels[projectId] = state.panels[projectId] || {};

  return state.panels[projectId];

}

function toast(msg, isError=false) {

  const el = document.getElementById('toast');

  el.textContent = msg;

  el.classList.toggle('error', isError);

  el.classList.add('show');

  clearTimeout(toast._t);

  toast._t = setTimeout(() => el.classList.remove('show'), 2600);

}

function escapeHtml(s) {

  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

}

function formatRelativeTime(ts) {

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

function sessionAgeMs(session) {
  const raw = session && session.last_seen ? String(session.last_seen) : '';
  if (!raw) return Number.POSITIVE_INFINITY;
  const iso = raw.includes('T') ? raw : raw.replace(' ', 'T') + 'Z';
  const parsed = new Date(iso).getTime();
  return Number.isFinite(parsed) ? Date.now() - parsed : Number.POSITIVE_INFINITY;
}

function isLiveSession(session, ageMs) {
  const age = ageMs == null ? sessionAgeMs(session) : ageMs;
  return session && session.status === 'active' && age >= 0 && age <= SESSION_LIVE_WINDOW_MS;
}

const _HUMAN_COLORS = ['#6c8fff', '#a78bfa', '#22d3ee', '#4ade80', '#fbbf24', '#f87171', '#fb923c', '#e879f9'];

function _colorForHuman(humanId) {

  /** Stable hash → palette index so each human keeps the same activity color. */

  let h = 0;

  for (let i = 0; i < (humanId || '').length; i++) h = ((h << 5) - h + humanId.charCodeAt(i)) | 0;

  return _HUMAN_COLORS[Math.abs(h) % _HUMAN_COLORS.length];

}

