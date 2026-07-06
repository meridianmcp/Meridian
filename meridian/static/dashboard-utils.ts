// dashboard-utils.ts — utility functions extracted from dashboard.js.
// Loaded first; symbols are also re-exposed as window globals so inline onclick
// handlers and cross-file references keep resolving after IIFE bundling.
//
// 8e29733e — first legacy module fully typed under strict mode (no @ts-nocheck).
// Strict surfaced a real bug: toast() dereferenced getElementById('toast') without
// a null check, so a missing #toast node threw instead of no-op'ing.

export const _PLAN_LABELS: Record<string, string> = {
  solo: 'Standard', free: 'Free Trial', standard: 'Standard', pro: 'Pro', trial: 'Trial', admin: 'Admin',
};

export const QUEUE_DONE_PAGE_SIZE = 10;
export const SESSION_LIVE_WINDOW_MS = 10 * 60 * 1000;
export const DEFAULT_MAX_PINNED_DECISIONS = 20;
export const DEFAULT_CONTEXT_THRESHOLD = 40;
// 47af402c — /goal "Stop after N turns" default; slider ceiling raised to 400
// to support megasprints (warnings surface at 200+/300+).
export const DEFAULT_MAX_TURNS = 200;

/** Minimal shape of a session row used by the age/live helpers. */
export interface SessionLike {
  last_seen?: string | null;
  status?: string | null;
}

export function getPanelState(projectId: string): Record<string, any> {
  window.state.panels[projectId] = window.state.panels[projectId] || {};
  return window.state.panels[projectId];
}

let _toastTimer: ReturnType<typeof setTimeout> | undefined;

export function toast(msg: string, isError = false): void {
  const el = document.getElementById('toast');
  if (!el) return; // 8e29733e — #toast may not be mounted; no-op instead of throwing.
  el.textContent = msg;
  el.classList.toggle('error', isError);
  el.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), 2600);
}

export function escapeHtml(s: unknown): string {
  const map: Record<string, string> = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  return String(s).replace(/[&<>"']/g, (c) => map[c] ?? c);
}

export function formatRelativeTime(ts: string | null | undefined): string {
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

export function sessionAgeMs(session: SessionLike | null | undefined): number {
  const raw = session && session.last_seen ? String(session.last_seen) : '';
  if (!raw) return Number.POSITIVE_INFINITY;
  const iso = raw.includes('T') ? raw : raw.replace(' ', 'T') + 'Z';
  const parsed = new Date(iso).getTime();
  return Number.isFinite(parsed) ? Date.now() - parsed : Number.POSITIVE_INFINITY;
}

export function isLiveSession(session: SessionLike | null | undefined, ageMs?: number | null): boolean {
  const age = ageMs == null ? sessionAgeMs(session) : ageMs;
  return !!session && session.status === 'active' && age >= 0 && age <= SESSION_LIVE_WINDOW_MS;
}

export const _HUMAN_COLORS = ['#6c8fff', '#a78bfa', '#22d3ee', '#4ade80', '#fbbf24', '#f87171', '#fb923c', '#e879f9'];

export function _colorForHuman(humanId: string): string {
  // Stable hash → palette index so each human keeps the same activity color.
  let h = 0;
  const id = humanId || '';
  for (let i = 0; i < id.length; i++) h = ((h << 5) - h + id.charCodeAt(i)) | 0;
  return _HUMAN_COLORS[Math.abs(h) % _HUMAN_COLORS.length] as string;
}

/**
 * Repo-path directories a project already tracks that aren't yet filesystem
 * roots — surfaced in Project Config as one-click "add" chips so a user never
 * has to retype a path Meridian already knows (77999d60). Pulls from
 * executor_config.repo_paths[].cwd plus the legacy single repo_path, trims,
 * dedupes, and drops anything already present in `currentRoots`.
 */
export function suggestedFsRoots(execCfg: any, currentRoots: any): string[] {
  const have = new Set(
    (Array.isArray(currentRoots) ? currentRoots : [])
      .map((r: any) => String(r ?? '').trim())
      .filter(Boolean),
  );
  const out: string[] = [];
  const seen = new Set<string>();
  const add = (raw: any) => {
    const v = String(raw ?? '').trim();
    if (!v || have.has(v) || seen.has(v)) return;
    seen.add(v);
    out.push(v);
  };
  const cfg = execCfg && typeof execCfg === 'object' ? execCfg : {};
  if (Array.isArray(cfg.repo_paths)) {
    for (const p of cfg.repo_paths) add(p && typeof p === 'object' ? p.cwd : p);
  }
  add(cfg.repo_path);
  return out;
}

// --- ITEM 4 esbuild: re-expose top-level symbols as globals so inline handlers
// and cross-file references keep resolving after IIFE bundling.
try {
  Object.assign(window, {
    getPanelState, toast, escapeHtml, formatRelativeTime, sessionAgeMs, isLiveSession,
    _colorForHuman, _PLAN_LABELS, QUEUE_DONE_PAGE_SIZE, SESSION_LIVE_WINDOW_MS,
    _HUMAN_COLORS, DEFAULT_MAX_PINNED_DECISIONS, DEFAULT_CONTEXT_THRESHOLD,
    DEFAULT_MAX_TURNS, suggestedFsRoots,
  });
} catch (e) { /* window unavailable (non-browser) */ }
