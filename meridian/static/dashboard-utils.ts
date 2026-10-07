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
  created_at?: string | null;
  status?: string | null;
}

/**
 * Recency key for a session: its last_seen, falling back to created_at (a session
 * that has never heartbeat still sorts by when it was created), then ''. Timestamps
 * are ISO-ish strings that sort lexicographically in chronological order, so callers
 * compare these keys directly. Kept as a tiny helper so every session-list render
 * agrees on the fallback chain (241b0d3b).
 */
export function sessionRecencyKey(session: SessionLike | null | undefined): string {
  if (!session) return '';
  return String(session.last_seen || session.created_at || '');
}

/**
 * Sort sessions MOST-RECENT-FIRST by last_seen (fallback created_at), descending.
 * Returns a new array — never mutates the caller's list. This is the single sort
 * used by every active/recent-sessions render so the ordering is consistent
 * everywhere the list appears (241b0d3b). Sessions with no timestamp at all sink
 * to the bottom.
 */
export function sortSessionsMostRecentFirst<T extends SessionLike>(sessions: readonly T[] | null | undefined): T[] {
  return (sessions ? sessions.slice() : []).sort(
    (a, b) => sessionRecencyKey(b).localeCompare(sessionRecencyKey(a)),
  );
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

// ---------------------------------------------------------------------------
// 8a665a03 -- a repaint must never throw away what the user typed.
//
// A list view that is rebuilt from a fresh fetch (innerHTML = ...) replaces every node in
// it, including the input the user is typing into: the half-written answer, the sprint item
// title in an open inline editor, the text of the add-item box. paintKeepingDrafts() is the
// one way those views repaint. It carries the live node (and so its text, its undo history,
// its handlers, and -- once re-focused -- its caret) across the repaint instead of the
// freshly rendered twin, for every "draft unit" the caller describes.
// ---------------------------------------------------------------------------
export interface DraftUnit {
  /** CSS selector (searched under the repainted root) for a node that may hold a draft. */
  selector: string;
  /** Identity the live node shares with its repainted twin (an id, a data attribute). */
  key: (el: HTMLElement) => string | null | undefined;
  /** Does this live node hold something worth keeping? Default: fieldHoldsDraft. */
  keep?: (el: HTMLElement) => boolean;
}

const _DRAFT_FIELDS =
  'textarea, input:not([type]), input[type="text"], input[type="search"], input[type="url"], ' +
  'input[type="email"], input[type="tel"], input[type="number"], input[type="password"]';

/** True when `el` is (or contains) a text field that has the focus or holds text the user
 * entered (its value differs from the value the markup rendered it with). */
export function fieldHoldsDraft(el: Element): boolean {
  const fields: any[] = el.matches(_DRAFT_FIELDS) ? [el] : Array.from(el.querySelectorAll(_DRAFT_FIELDS));
  return fields.some((f) => f === document.activeElement || f.value !== f.defaultValue);
}

interface _KeptDraft {
  unit: DraftUnit;
  key: string;
  /** Position among the live nodes of this unit that share the key (a row can be drawn twice). */
  ordinal: number;
  el: HTMLElement;
}

function _collectDrafts(root: Element, units: DraftUnit[]): _KeptDraft[] {
  const kept: _KeptDraft[] = [];
  for (const unit of units) {
    const seen = new Map<string, number>();
    root.querySelectorAll<HTMLElement>(unit.selector).forEach((el) => {
      const key = unit.key(el);
      if (!key) return;
      const ordinal = seen.get(key) || 0;
      seen.set(key, ordinal + 1);
      if ((unit.keep || fieldHoldsDraft)(el)) kept.push({ unit, key, ordinal, el });
    });
  }
  return kept;
}

function _findTwin(root: Element, k: _KeptDraft): HTMLElement | null {
  let n = 0;
  for (const el of Array.from(root.querySelectorAll<HTMLElement>(k.unit.selector))) {
    if (k.unit.key(el) !== k.key) continue;
    if (n++ === k.ordinal) return el;
  }
  return null;
}

/** Does anything under `root` hold an unsent draft right now? Used to skip a transient
 * "loading..." placeholder that would otherwise wipe it before the fetch even returns. */
export function hasUnsentDrafts(root: Element | null, units: DraftUnit[]): boolean {
  return !!root && _collectDrafts(root, units).length > 0;
}

/**
 * Replace the content of `root` with `html`, keeping every draft unit that is live in `root`.
 * Judged at paint time (after any fetch returned), so text typed while a request was in
 * flight survives. A kept node takes the place of its repainted twin; one whose twin is
 * gone (the row was deleted, the request was answered elsewhere) has nothing left to attach
 * to and is dropped. Focus, caret and a textarea's scroll are restored on the kept node.
 * Returns how many live nodes were carried over.
 */
export function paintKeepingDrafts(root: Element, html: string, units: DraftUnit[]): number {
  const kept = _collectDrafts(root, units);
  if (!kept.length) {
    root.innerHTML = html;
    return 0;
  }
  const active = document.activeElement as any;
  const focused = active && kept.some((k) => k.el === active || k.el.contains(active)) ? active : null;
  let caret: { start: number | null; end: number | null; dir: string | null } | null = null;
  if (focused) {
    try { caret = { start: focused.selectionStart, end: focused.selectionEnd, dir: focused.selectionDirection }; } catch (_) { /* a field with no selection API */ }
  }
  const scrolls = new Map<any, number>();
  for (const k of kept) {
    const fields: any[] = k.el.matches('textarea') ? [k.el] : Array.from(k.el.querySelectorAll('textarea'));
    for (const f of fields) scrolls.set(f, f.scrollTop);
  }

  root.innerHTML = html;

  let carried = 0;
  for (const k of kept) {
    const twin = _findTwin(root, k);
    if (!twin) continue;
    twin.replaceWith(k.el);
    carried += 1;
  }
  scrolls.forEach((top, f) => { if (f.isConnected) f.scrollTop = top; });
  if (focused && focused.isConnected) {
    try { focused.focus({ preventScroll: true }); } catch (_) { /* detached or not focusable */ }
    if (caret && caret.start != null && caret.end != null) {
      try { focused.setSelectionRange(caret.start, caret.end, caret.dir || undefined); } catch (_) { /* no selection API */ }
    }
  }
  return carried;
}

// --- ITEM 4 esbuild: re-expose top-level symbols as globals so inline handlers
// and cross-file references keep resolving after IIFE bundling.
try {
  Object.assign(window, {
    getPanelState, toast, escapeHtml, formatRelativeTime, sessionAgeMs, isLiveSession,
    sessionRecencyKey, sortSessionsMostRecentFirst,
    _colorForHuman, _PLAN_LABELS, QUEUE_DONE_PAGE_SIZE, SESSION_LIVE_WINDOW_MS,
    _HUMAN_COLORS, DEFAULT_MAX_PINNED_DECISIONS, DEFAULT_CONTEXT_THRESHOLD,
    DEFAULT_MAX_TURNS, suggestedFsRoots,
    fieldHoldsDraft, hasUnsentDrafts, paintKeepingDrafts,
  });
} catch (e) { /* window unavailable (non-browser) */ }
