// dashboard-waffle.ts (90952bad)
// ---------------------------------------------------------------------------
// Outlook-style "waffle" launcher for the dashboard tabs.
//
// Why this exists: the left vtab rail carries ~19 tabs in 5 groups, and the
// order it shows them in is the order they were built, not the order anyone
// uses them (Status/Active sessions sits at the very top; Goal, Notes and
// Insights are buried under group headers). The owner asked for a 3x3 "waffle"
// button toward the top-left that pops up a grid of icon tiles, ordered by
// usefulness (most useful first, least used toward the bottom), so the rail
// stops being the only way to move around.
//
// Design rules this module keeps:
//   * ADDITIVE. Choosing a tile runs the same code path as clicking the
//     matching `.vtab-btn` (revealGroupInStrip, then the button's own click),
//     so the project's tab state, localStorage restore and loaders all behave
//     exactly as before. Every `.vtab-btn` stays in the DOM; the waffle only
//     reads the strip to learn which tabs exist right now (files is absent for
//     hosted projects without a repo, codeintel is hidden until the tunnel is up).
//   * The ordering is a PURE model (buildWaffleModel and friends) so it is unit
//     tested without a DOM. The default rank is one constant (WAFFLE_DEFAULT_RANK)
//     that is easy to change.
//   * Usage adapts the order, but only the middle of it: the owner's top picks
//     (WAFFLE_DEFAULT_PINS) stay pinned until the user unpins them, and the
//     re-sort does not start until WAFFLE_USAGE_THRESHOLD activations, so a new
//     user sees the owner's order, not the noise of their first clicks. Every
//     way of opening a tab counts (the rail's shared onclick calls
//     recordWaffleUse), not only waffle activations; scripted navigation (restore
//     on load, the tour) runs inside withoutWaffleUse and does not.
//   * Per-user state (pins, click counts, recents) lives in localStorage ONLY,
//     every access wrapped in try/catch; the launcher works with storage blocked.
//   * Several tabs share that one localStorage value, so every write is a
//     read-modify-write: re-read the latest stored value, apply only the one
//     change (one pin added or removed, one usage count, the recents), save. A
//     window 'storage' listener (and a re-read on open) keeps an open popover
//     and the in-memory state current with what other tabs wrote.
//
// Standalone on purpose: it imports only the group model from
// dashboard-tabgroups. dashboard.ts supplies the strip and the badge data.

import { groupForTab, revealGroupInStrip } from "./dashboard-tabgroups";

// ---------------------------------------------------------------------------
// Tab metadata, default order, defaults
// ---------------------------------------------------------------------------

export type WaffleIconName =
  | "status" | "live" | "goal" | "insights" | "blog" | "queue" | "experiments"
  | "hitl" | "team" | "sessions" | "files" | "notes" | "devlog" | "documents"
  | "docs" | "codeintel" | "timeline" | "rewind" | "settings" | "generic";

export interface WaffleTabMeta {
  /** data-vtab id of the rail button this tile activates. */
  id: string;
  /** Short tile label (the rail's own titles are sentences, too long for a tile). */
  label: string;
  icon: WaffleIconName;
  /** Extra words the type-to-filter box matches besides the label and id. */
  keywords: readonly string[];
}

/** Metadata for every tab the rail can render. Display order is NOT here: see WAFFLE_DEFAULT_RANK. */
export const WAFFLE_TABS: readonly WaffleTabMeta[] = [
  { id: "status", label: "Status", icon: "status", keywords: ["overview", "active sessions", "health"] },
  { id: "live", label: "Live", icon: "live", keywords: ["right now", "in progress", "parallel", "waves"] },
  { id: "goal", label: "Goal", icon: "goal", keywords: ["north star", "target", "targets", "version", "sprint", "focus", "decisions"] },
  { id: "insights", label: "Insights", icon: "insights", keywords: ["strategy", "understanding", "ideas"] },
  { id: "blog", label: "Blog", icon: "blog", keywords: ["posts", "drafts", "publish", "writing"] },
  { id: "queue", label: "Queue", icon: "queue", keywords: ["active work", "sprint items", "todo", "backlog", "tasks"] },
  { id: "experiments", label: "Experiments", icon: "experiments", keywords: ["trials", "runs", "registry", "research"] },
  { id: "hitl", label: "HITL", icon: "hitl", keywords: ["review", "approvals", "questions", "human in the loop", "researcher"] },
  { id: "team", label: "Team", icon: "team", keywords: ["people", "humans", "members", "activity"] },
  { id: "sessions", label: "Sessions", icon: "sessions", keywords: ["run history", "runs", "history"] },
  { id: "files", label: "Files", icon: "files", keywords: ["repo", "code", "browse", "folders"] },
  { id: "notes", label: "Notes", icon: "notes", keywords: ["wiki", "memory", "write"] },
  { id: "devlog", label: "Dev log", icon: "devlog", keywords: ["tasks", "log", "journal", "terminal"] },
  { id: "documents", label: "Documents", icon: "documents", keywords: ["ingested", "structure", "word", "pdf", "docx"] },
  { id: "docs", label: "Tool docs", icon: "docs", keywords: ["mcp", "tool reference", "reference", "manual"] },
  { id: "codeintel", label: "Code intel", icon: "codeintel", keywords: ["codebase", "index", "architecture", "graph", "symbols"] },
  { id: "timeline", label: "Timeline", icon: "timeline", keywords: ["activity", "history", "swimlane", "events"] },
  { id: "rewind", label: "Rewind", icon: "rewind", keywords: ["last days", "charts", "history", "replay"] },
  { id: "settings", label: "Settings", icon: "settings", keywords: ["notifications", "hooks", "integrations", "config", "preferences"] },
];

/**
 * Default usefulness rank, most useful first (the owner's order, 90952bad).
 * Targets are not a tab: they live inside Goal (see WAFFLE_SUBTABS). The
 * Experiments tab was not in the owner's list; it sits after Team, which is
 * where its usage (a research-heavy workflow) puts it. A tab the rail renders
 * that is missing here sorts after every ranked tab, in rail order.
 */
export const WAFFLE_DEFAULT_RANK: readonly string[] = [
  "goal", "notes", "insights", "queue", "live", "hitl", "status", "timeline",
  "team", "experiments", "documents", "docs", "files", "codeintel", "devlog",
  "rewind", "blog", "sessions", "settings",
];

/** The owner's top picks. Pinned until the user unpins them. */
export const WAFFLE_DEFAULT_PINS: readonly string[] = ["goal", "notes", "insights"];

/**
 * Total activations before usage starts re-sorting the non-pinned tabs. Below
 * this the order is exactly WAFFLE_DEFAULT_RANK, so a fresh user (and the demo)
 * always sees the owner's order.
 */
export const WAFFLE_USAGE_THRESHOLD = 25;

/** How many most-recently-used (non-pinned) tabs the Recent section shows. */
export const WAFFLE_RECENT_MAX = 3;

/** The grid is 3x3: three tiles per row at every viewport width. */
export const WAFFLE_COLUMNS = 3;

export interface WaffleSubMeta {
  /** data-gtab id of the Goal sub-tab button. */
  id: string;
  /** data-vtab id of the tab it lives in. */
  parent: string;
  label: string;
  keywords: readonly string[];
}

/**
 * Sub-entries offered under a tab. Today only Goal has them: its North Star /
 * Version Goal / Current Focus / Decisions sub-tabs are where the owner's
 * "targets" live. Same order as the Goal tab's own sub-tab strip.
 */
export const WAFFLE_SUBTABS: readonly WaffleSubMeta[] = [
  { id: "north-star", parent: "goal", label: "North Star", keywords: ["vision", "target", "targets"] },
  { id: "version-goal", parent: "goal", label: "Version Goal", keywords: ["milestone", "release", "target", "targets"] },
  { id: "sprint", parent: "goal", label: "Current Focus", keywords: ["sprint", "now", "target", "targets"] },
  { id: "decisions", parent: "goal", label: "Decisions", keywords: ["pinned", "constitution", "decide", "log"] },
];

// ---------------------------------------------------------------------------
// Icons (drawn here: 24x24 grid, 1.75 stroke, round caps, currentColor)
// ---------------------------------------------------------------------------

const ICON_PATHS: Record<WaffleIconName, string> = {
  status: '<polyline points="3 12 7 12 9.5 6 14.5 18 17 12 21 12"/>',
  live: '<path d="M13 3 6 13.5h5.5L10.5 21 18 10.5h-5.5z"/>',
  goal: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="1" fill="currentColor"/>',
  insights:
    '<path d="M9.5 17.5h5"/><path d="M10.5 20.5h3"/>' +
    '<path d="M12 3.5a5.5 5.5 0 0 0-3.2 10c.7.5 1.2 1.3 1.2 2.2v1.8h4v-1.8c0-.9.5-1.7 1.2-2.2A5.5 5.5 0 0 0 12 3.5z"/>',
  blog: '<path d="M4 20l.8-4L16.2 4.6a2 2 0 0 1 2.8 2.8L7.6 18.8z"/><path d="M14.5 6.4l3.1 3.1"/>',
  queue:
    '<path d="M9 6.5h11M9 12h11M9 17.5h11"/>' +
    '<circle cx="4.8" cy="6.5" r=".9" fill="currentColor"/><circle cx="4.8" cy="12" r=".9" fill="currentColor"/>' +
    '<circle cx="4.8" cy="17.5" r=".9" fill="currentColor"/>',
  experiments:
    '<path d="M9.5 3.5h5"/>' +
    '<path d="M10.5 3.5v5.2L5 18.6a1.6 1.6 0 0 0 1.4 2.4h11.2a1.6 1.6 0 0 0 1.4-2.4l-5.5-9.9V3.5"/>' +
    '<path d="M7.8 15h8.4"/>',
  hitl:
    '<circle cx="12" cy="12" r="8.5"/><path d="M9.7 9.6a2.4 2.4 0 1 1 3.4 2.2c-.7.4-1.1.9-1.1 1.7"/>' +
    '<circle cx="12" cy="16.6" r=".9" fill="currentColor"/>',
  team:
    '<circle cx="9" cy="8.5" r="3"/><path d="M3.5 19.5a5.5 5.5 0 0 1 11 0"/>' +
    '<circle cx="17" cy="9.5" r="2.3"/><path d="M16.2 14.2a4.6 4.6 0 0 1 4.8 4.3"/>',
  sessions: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  files: '<path d="M3.5 7.5a2 2 0 0 1 2-2h4l2 2.2h7a2 2 0 0 1 2 2v7.8a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>',
  notes: '<path d="M5 4.5h14v10l-5.5 5.5H5z"/><path d="M13.5 20v-5.5H19"/><path d="M8.5 9h7M8.5 12.5h4"/>',
  devlog: '<rect x="3.5" y="5" width="17" height="14" rx="2"/><path d="M7.5 10l3 2.2-3 2.2M12.5 15h4"/>',
  documents: '<path d="M6.5 3.5h7.5l4.5 4.5v12.5h-12z"/><path d="M14 3.5V8h4.5"/><path d="M9.2 12.5h5.6M9.2 16h5.6"/>',
  docs:
    '<path d="M3.5 6c2.6-1.1 5.6-.9 8.5.9 2.9-1.8 5.9-2 8.5-.9v12.5c-2.6-1.1-5.6-.9-8.5.9-2.9-1.8-5.9-2-8.5-.9z"/>' +
    '<path d="M12 6.9v12.5"/>',
  codeintel: '<path d="M8.5 8l-4 4 4 4M15.5 8l4 4-4 4M13.3 6.5l-2.6 11"/>',
  timeline:
    '<path d="M7 4.5v15"/><circle cx="7" cy="6.5" r="1.7"/><circle cx="7" cy="12" r="1.7"/><circle cx="7" cy="17.5" r="1.7"/>' +
    '<path d="M11.5 6.5H20M11.5 12H17M11.5 17.5H19"/>',
  rewind: '<path d="M4.5 12a7.5 7.5 0 1 0 2.4-5.5"/><path d="M4.2 4.5v3.9h3.9"/><path d="M12 8.5V12l2.4 1.6"/>',
  settings:
    '<path d="M4 7h9M18 7h2M4 17h2M11 17h9"/><circle cx="15.5" cy="7" r="2.3"/><circle cx="8.5" cy="17" r="2.3"/>',
  generic:
    '<rect x="4" y="4" width="6.5" height="6.5" rx="1.5"/><rect x="13.5" y="4" width="6.5" height="6.5" rx="1.5"/>' +
    '<rect x="4" y="13.5" width="6.5" height="6.5" rx="1.5"/><rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1.5"/>',
};

/** Inline SVG for a tab icon. Stroke icons inherit `color` via currentColor. */
export function waffleIconSvg(name: WaffleIconName, size = 20): string {
  const body = ICON_PATHS[name] ?? ICON_PATHS.generic;
  return (
    `<svg class="waffle-icon" viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" ` +
    `stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" ` +
    `aria-hidden="true" focusable="false">${body}</svg>`
  );
}

/** The 9-dot glyph on the launcher button. */
function waffleGlyphSvg(): string {
  const dots: string[] = [];
  for (const cy of [6, 12, 18]) {
    for (const cx of [6, 12, 18]) dots.push(`<circle cx="${cx}" cy="${cy}" r="1.9"/>`);
  }
  return (
    `<svg class="waffle-glyph" viewBox="0 0 24 24" width="20" height="20" fill="currentColor" ` +
    `aria-hidden="true" focusable="false">${dots.join("")}</svg>`
  );
}

function pinSvg(filled: boolean): string {
  return (
    `<svg class="waffle-pin-icon" viewBox="0 0 24 24" width="13" height="13" ` +
    `fill="${filled ? "currentColor" : "none"}" stroke="currentColor" stroke-width="1.9" ` +
    `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">` +
    `<path d="M12 21v-6"/><path d="M8 4h8l-1.2 5.4L18 13v2H6v-2l3.2-3.6z"/></svg>`
  );
}

function searchSvg(): string {
  return (
    `<svg class="waffle-search-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" ` +
    `stroke="currentColor" stroke-width="1.9" stroke-linecap="round" aria-hidden="true" focusable="false">` +
    `<circle cx="10.5" cy="10.5" r="6"/><path d="M15 15l5 5"/></svg>`
  );
}

// ---------------------------------------------------------------------------
// Per-user state (localStorage only) + the pure ordering model
// ---------------------------------------------------------------------------

export interface WaffleState {
  /**
   * null = the user never touched a pin: the owner's WAFFLE_DEFAULT_PINS apply.
   * An array (even an empty one) is the user's own list, so unpinning a default
   * sticks even if the defaults change later.
   */
  pins: string[] | null;
  /** Activation count per tab id. */
  usage: Record<string, number>;
  /** Most recently activated tab ids, newest first. */
  recent: string[];
}

export function emptyState(): WaffleState {
  return { pins: null, usage: {}, recent: [] };
}

const MAX_RECENT_KEPT = 8;
const MAX_USAGE_KEYS = 64;
const MAX_COUNT = 1_000_000;

function cleanId(v: unknown): string | null {
  return typeof v === "string" && v.length > 0 && v.length <= 40 && /^[\w.-]+$/.test(v) ? v : null;
}

function uniqueIds(list: unknown[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const item of list) {
    const id = cleanId(item);
    if (id && !seen.has(id)) {
      seen.add(id);
      out.push(id);
    }
  }
  return out;
}

/** Defensive parse of whatever was in storage: never throws, never trusts shapes. */
export function parseState(raw: unknown): WaffleState {
  const out = emptyState();
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return out;
  const r = raw as Record<string, unknown>;
  if (Array.isArray(r.pins)) out.pins = uniqueIds(r.pins).slice(0, 64);
  if (r.usage && typeof r.usage === "object" && !Array.isArray(r.usage)) {
    let kept = 0;
    for (const [k, v] of Object.entries(r.usage as Record<string, unknown>)) {
      const id = cleanId(k);
      const n = Number(v);
      if (id && Number.isFinite(n) && n > 0 && kept < MAX_USAGE_KEYS) {
        out.usage[id] = Math.min(Math.floor(n), MAX_COUNT);
        kept += 1;
      }
    }
  }
  if (Array.isArray(r.recent)) out.recent = uniqueIds(r.recent).slice(0, MAX_RECENT_KEPT);
  return out;
}

/** Storage handle that never throws, even when site data is blocked. */
export function safeLocalStorage(): Storage | null {
  try {
    return typeof window !== "undefined" && window.localStorage ? window.localStorage : null;
  } catch {
    return null;
  }
}

/**
 * The raw stored string: null when nothing is stored under `key`, undefined when
 * there is no storage to ask (none supplied, blocked, or getItem throws). Callers
 * that merge with other tabs need that difference: "empty" is an answer,
 * "unreadable" is not.
 */
export function readRawState(storage: Storage | null, key: string): string | null | undefined {
  if (!storage) return undefined;
  try {
    return storage.getItem(key);
  } catch {
    return undefined;
  }
}

/** Defensive parse of a raw stored string (or its absence): never throws. */
export function parseRawState(raw: string | null | undefined): WaffleState {
  if (!raw) return emptyState();
  try {
    return parseState(JSON.parse(raw));
  } catch {
    return emptyState();
  }
}

export function loadState(storage: Storage | null, key: string): WaffleState {
  return parseRawState(readRawState(storage, key));
}

/** The exact string saveState writes. */
export function serializeState(state: WaffleState): string {
  return JSON.stringify({ v: 1, pins: state.pins, usage: state.usage, recent: state.recent });
}

/** Write the state. Returns whether it was stored: false when storage is absent, full or blocked. */
export function saveState(storage: Storage | null, key: string, state: WaffleState): boolean {
  if (!storage) return false;
  try {
    storage.setItem(key, serializeState(state));
    return true;
  } catch {
    // Storage full or blocked: the launcher still works for this page load.
    return false;
  }
}

/** The pins in effect: the user's list if they have one, else the owner's defaults. */
export function effectivePins(state: WaffleState): string[] {
  return state.pins ? [...state.pins] : [...WAFFLE_DEFAULT_PINS];
}

/** Count one activation of `tab`: bumps its usage and moves it to the front of Recent. */
export function recordUse(state: WaffleState, tab: string): WaffleState {
  const id = cleanId(tab);
  if (!id) return state;
  return {
    pins: state.pins ? [...state.pins] : null,
    usage: { ...state.usage, [id]: Math.min((state.usage[id] || 0) + 1, MAX_COUNT) },
    recent: [id, ...state.recent.filter((t) => t !== id)].slice(0, MAX_RECENT_KEPT),
  };
}

/**
 * Make `tab` pinned (or unpinned): adds or removes exactly that one pin and
 * leaves every other pin, the usage counts and Recent alone. A new pin goes
 * last. Already in the wanted state returns the state unchanged.
 */
export function setPinned(state: WaffleState, tab: string, pinned: boolean): WaffleState {
  const id = cleanId(tab);
  if (!id) return state;
  const pins = effectivePins(state);
  if (pins.includes(id) === pinned) return state;
  return { ...state, pins: pinned ? [...pins, id] : pins.filter((t) => t !== id) };
}

/** Pin the tab if it is not pinned, unpin it if it is. A new pin goes last. */
export function togglePin(state: WaffleState, tab: string): WaffleState {
  const id = cleanId(tab);
  if (!id) return state;
  return setPinned(state, id, !effectivePins(state).includes(id));
}

export function usageTotal(usage: Record<string, number>): number {
  let total = 0;
  for (const n of Object.values(usage)) total += n;
  return total;
}

export interface OrderOptions {
  rank?: readonly string[];
  threshold?: number;
}

/**
 * Split the available tabs into the pinned list (user pin order) and the rest.
 * The rest follows the default rank until WAFFLE_USAGE_THRESHOLD activations,
 * then most-used first with the default rank as the tie-break, so the least
 * used tabs sink toward the bottom. Pinned tabs are never re-sorted by usage.
 */
export function orderTabs(
  available: readonly string[],
  state: WaffleState,
  opts: OrderOptions = {},
): { pinned: string[]; rest: string[] } {
  const rank = opts.rank ?? WAFFLE_DEFAULT_RANK;
  const threshold = opts.threshold ?? WAFFLE_USAGE_THRESHOLD;
  const avail = uniqueIds([...available]);
  const availSet = new Set(avail);
  const pinned = effectivePins(state).filter((id, i, all) => availSet.has(id) && all.indexOf(id) === i);
  const pinnedSet = new Set(pinned);
  const position = (id: string): number => {
    const i = rank.indexOf(id);
    return i >= 0 ? i : rank.length + avail.indexOf(id);
  };
  const adaptive = usageTotal(state.usage) >= threshold;
  const rest = avail.filter((id) => !pinnedSet.has(id));
  rest.sort((a, b) => {
    if (adaptive) {
      const byUse = (state.usage[b] || 0) - (state.usage[a] || 0);
      if (byUse !== 0) return byUse;
    }
    return position(a) - position(b);
  });
  return { pinned, rest };
}

/** Most recently used tabs that are available and not already pinned. */
export function recentTabs(
  state: WaffleState,
  available: readonly string[],
  pinned: readonly string[],
  max = WAFFLE_RECENT_MAX,
): string[] {
  const availSet = new Set(available);
  const pinnedSet = new Set(pinned);
  return state.recent.filter((id) => availSet.has(id) && !pinnedSet.has(id)).slice(0, max);
}

// ---------------------------------------------------------------------------
// The view model (sections of rows of entries) and its text filter
// ---------------------------------------------------------------------------

export interface WaffleEntry {
  /** Stable key: "tab:goal" or "sub:goal:north-star". */
  key: string;
  kind: "tab" | "sub";
  /** The tab this entry activates (a sub-entry's parent tab). */
  tab: string;
  sub?: string;
  label: string;
  icon: WaffleIconName;
  group: string | null;
  /** Pending-work count shown on the tile (0 = no badge). */
  badge: number;
  pinned: boolean;
  active: boolean;
}

export interface WaffleRow {
  kind: "tiles" | "subs";
  /** For a "subs" row, the tab whose sub-entries these are. */
  parent?: string;
  parentLabel?: string;
  entries: WaffleEntry[];
}

export interface WaffleSection {
  id: "pinned" | "recent" | "all" | "results";
  label: string;
  rows: WaffleRow[];
}

export interface WaffleModel {
  sections: WaffleSection[];
  /** Entry keys per visual row, in reading order: what arrow-key navigation walks. */
  keys: string[][];
  /** Number of entries (tiles plus sub-entries). */
  count: number;
  query: string;
}

export interface WaffleModelInput {
  /** data-vtab ids the active project's rail currently renders (and shows). */
  available: readonly string[];
  state: WaffleState;
  query?: string;
  /** Pending-work counts keyed by tab id (hitl, queue). */
  badges?: Record<string, number>;
  activeTab?: string | null;
  /** Fallback labels for tabs this module has no metadata for. */
  labels?: Record<string, string>;
  rank?: readonly string[];
  threshold?: number;
}

const META_BY_ID = new Map<string, WaffleTabMeta>(WAFFLE_TABS.map((m) => [m.id, m]));

function humanize(id: string): string {
  const s = id.replace(/[^A-Za-z0-9]+/g, " ").trim();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : id;
}

function metaFor(id: string, labels?: Record<string, string>): WaffleTabMeta {
  return (
    META_BY_ID.get(id) ?? { id, label: labels?.[id] || humanize(id), icon: "generic", keywords: [] }
  );
}

function tokenize(query: string): string[] {
  return query.toLowerCase().split(/\s+/).filter(Boolean);
}

function matchesTokens(haystack: string, tokens: readonly string[]): boolean {
  const h = haystack.toLowerCase();
  return tokens.every((t) => h.includes(t));
}

/** True when the tab's label, id or keywords contain every word of the query. */
export function tabMatches(meta: WaffleTabMeta, query: string): boolean {
  const tokens = tokenize(query);
  if (!tokens.length) return true;
  return matchesTokens(`${meta.label} ${meta.id} ${meta.keywords.join(" ")}`, tokens);
}

export function subMatches(sub: WaffleSubMeta, query: string): boolean {
  const tokens = tokenize(query);
  if (!tokens.length) return true;
  return matchesTokens(`${sub.label} ${sub.id} ${sub.keywords.join(" ")}`, tokens);
}

function cleanBadge(n: unknown): number {
  const v = Number(n);
  return Number.isFinite(v) && v > 0 ? Math.floor(v) : 0;
}

/**
 * Build what the popover renders: Pinned, Recent and All tabs (no tab appears
 * twice), or one flat Results section while the filter has text. Sub-entries
 * (Goal's sub-tabs) follow the row holding their parent tile.
 */
export function buildWaffleModel(input: WaffleModelInput): WaffleModel {
  const query = (input.query ?? "").trim();
  const badges = input.badges ?? {};
  const { pinned, rest } = orderTabs(input.available, input.state, {
    rank: input.rank,
    threshold: input.threshold,
  });
  const pinnedSet = new Set(pinned);
  const availSet = new Set([...pinned, ...rest]);
  const recent = recentTabs(input.state, [...availSet], pinned);

  const tabEntry = (id: string): WaffleEntry => {
    const meta = metaFor(id, input.labels);
    return {
      key: `tab:${id}`,
      kind: "tab",
      tab: id,
      label: meta.label,
      icon: meta.icon,
      group: groupForTab(id),
      badge: cleanBadge(badges[id]),
      pinned: pinnedSet.has(id),
      active: input.activeTab === id,
    };
  };

  const subEntry = (sub: WaffleSubMeta): WaffleEntry => ({
    key: `sub:${sub.parent}:${sub.id}`,
    kind: "sub",
    tab: sub.parent,
    sub: sub.id,
    label: sub.label,
    icon: metaFor(sub.parent, input.labels).icon,
    group: groupForTab(sub.parent),
    badge: 0,
    pinned: false,
    active: false,
  });

  // The sub-entries to show under a tab. While filtering, a tab whose own label
  // matched keeps all its sub-entries; otherwise only the matching ones appear.
  const subsFor = (tab: string, parentMatched: boolean): WaffleEntry[] =>
    WAFFLE_SUBTABS.filter((s) => s.parent === tab)
      .filter((s) => !query || parentMatched || subMatches(s, query))
      .map(subEntry);

  const buildRows = (ids: readonly string[], parentMatched: (id: string) => boolean): WaffleRow[] => {
    const rows: WaffleRow[] = [];
    for (let i = 0; i < ids.length; i += WAFFLE_COLUMNS) {
      const chunk = ids.slice(i, i + WAFFLE_COLUMNS);
      rows.push({ kind: "tiles", entries: chunk.map(tabEntry) });
      for (const id of chunk) {
        const subs = subsFor(id, parentMatched(id));
        for (let j = 0; j < subs.length; j += WAFFLE_COLUMNS) {
          rows.push({
            kind: "subs",
            parent: id,
            parentLabel: metaFor(id, input.labels).label,
            entries: subs.slice(j, j + WAFFLE_COLUMNS),
          });
        }
      }
    }
    return rows;
  };

  let sections: WaffleSection[];
  if (query) {
    const everyone = [...pinned, ...recent, ...rest.filter((id) => !recent.includes(id))];
    const matched = everyone.filter((id) => tabMatches(metaFor(id, input.labels), query));
    const rows = buildRows(matched, () => true);
    // A sub-entry can match on its own while its parent tile does not (typing
    // "decisions" finds the Goal sub-tab): show those after the tiles.
    const matchedSet = new Set(matched);
    for (const tab of new Set(WAFFLE_SUBTABS.map((s) => s.parent))) {
      if (!availSet.has(tab) || matchedSet.has(tab)) continue;
      const subs = subsFor(tab, false);
      for (let j = 0; j < subs.length; j += WAFFLE_COLUMNS) {
        rows.push({
          kind: "subs",
          parent: tab,
          parentLabel: metaFor(tab, input.labels).label,
          entries: subs.slice(j, j + WAFFLE_COLUMNS),
        });
      }
    }
    sections = rows.length ? [{ id: "results", label: "Results", rows }] : [];
  } else {
    const recentSet = new Set(recent);
    const allIds = rest.filter((id) => !recentSet.has(id));
    sections = [];
    if (pinned.length) sections.push({ id: "pinned", label: "Pinned", rows: buildRows(pinned, () => true) });
    if (recent.length) sections.push({ id: "recent", label: "Recent", rows: buildRows(recent, () => true) });
    if (allIds.length) {
      sections.push({
        id: "all",
        label: pinned.length || recent.length ? "All tabs" : "Tabs",
        rows: buildRows(allIds, () => true),
      });
    }
  }

  const keys = sections.flatMap((s) => s.rows.map((r) => r.entries.map((e) => e.key)));
  return { sections, keys, count: keys.reduce((n, row) => n + row.length, 0), query };
}

// ---------------------------------------------------------------------------
// Keyboard navigation (pure) and popover placement (pure)
// ---------------------------------------------------------------------------

/**
 * Where an arrow/Home/End key moves the roving focus. Left/Right walk the
 * entries in reading order (across row ends, so focus never gets stuck);
 * Up/Down keep the column and clamp it on a shorter row; Home/End go to the
 * row's ends and Ctrl+Home/End to the first/last entry. Returns the current key
 * when the move would leave the grid, and null for keys it does not handle.
 */
export function moveFocus(
  rows: readonly (readonly string[])[],
  current: string | null,
  key: string,
  ctrl = false,
): string | null {
  const flat = rows.flat();
  if (!flat.length) return null;
  const r = current === null ? -1 : rows.findIndex((row) => row.includes(current));
  if (r < 0) {
    return ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(key) ? flat[0] : null;
  }
  const cur = current as string;
  const c = rows[r].indexOf(cur);
  const at = flat.indexOf(cur);
  switch (key) {
    case "ArrowRight":
      return flat[Math.min(at + 1, flat.length - 1)];
    case "ArrowLeft":
      return flat[Math.max(at - 1, 0)];
    case "ArrowDown":
      return r + 1 >= rows.length ? cur : rows[r + 1][Math.min(c, rows[r + 1].length - 1)];
    case "ArrowUp":
      return r === 0 ? cur : rows[r - 1][Math.min(c, rows[r - 1].length - 1)];
    case "Home":
      return ctrl ? flat[0] : rows[r][0];
    case "End":
      return ctrl ? flat[flat.length - 1] : rows[r][rows[r].length - 1];
    default:
      return null;
  }
}

export interface PopoverPlacement {
  left: number;
  top: number;
  width: number;
  maxHeight: number;
}

/**
 * Keep the popover fully inside the viewport: under the anchor, shifted left
 * as far as needed, narrowed on small screens, and height-limited (it scrolls
 * inside) when the viewport is short.
 */
export function computePopoverPlacement(args: {
  anchor: { left: number; bottom: number };
  width: number;
  viewport: { width: number; height: number };
  margin?: number;
  gap?: number;
  minHeight?: number;
}): PopoverPlacement {
  const margin = args.margin ?? 8;
  const gap = args.gap ?? 6;
  const minHeight = args.minHeight ?? 120;
  const width = Math.max(0, Math.min(args.width, args.viewport.width - 2 * margin));
  const left = Math.max(margin, Math.min(args.anchor.left, args.viewport.width - margin - width));
  const top = Math.max(margin, Math.min(args.anchor.bottom + gap, args.viewport.height - margin - minHeight));
  return { left, top, width, maxHeight: Math.max(0, args.viewport.height - top - margin) };
}

// ---------------------------------------------------------------------------
// Data the page already has: badges + sprint counts
// ---------------------------------------------------------------------------

const ACTIVE_SPRINT_STATUSES = new Set(["pending", "todo", "in_progress"]);

/** Sprint items that are still open: the same statuses the Goal tab's sprint board counts as active. */
export function countActiveSprintItems(items: unknown): number {
  if (!Array.isArray(items)) return 0;
  return items.filter((it) => it && ACTIVE_SPRINT_STATUSES.has((it as { status?: string }).status ?? "")).length;
}

/**
 * Pending-work counts for the tiles, from data already on the page: the HITL
 * count chip the rail keeps current, and the active sprint-item count the
 * Goal/Live/Queue loaders store on the project's panel state.
 */
export function readWaffleBadges(
  strip: ParentNode | null,
  panel?: { sprintActiveCount?: unknown } | null,
): Record<string, number> {
  const out: Record<string, number> = {};
  const hitl = strip?.querySelector<HTMLElement>(".hitl-vtab-badge");
  if (hitl && hitl.style.display !== "none") {
    const n = parseInt(hitl.textContent || "0", 10);
    if (n > 0) out.hitl = n;
  }
  const q = cleanBadge(panel?.sprintActiveCount);
  if (q > 0) out.queue = q;
  return out;
}

// ---------------------------------------------------------------------------
// Activation: the SAME path as clicking the rail button
// ---------------------------------------------------------------------------

/**
 * Navigate to `tab` (and optionally one of its sub-entries) the way a click on
 * the rail does: reveal the tab's group, then click its `.vtab-btn`, whose
 * onclick owns the drawer switch, the persisted last tab and the loaders. A
 * sub-entry then clicks the Goal sub-tab button, as the demo tour does.
 * Returns false when the strip has no such button.
 */
export function activateWaffleTab(strip: HTMLElement, tab: string, sub?: string): boolean {
  const btn = Array.from(strip.querySelectorAll<HTMLElement>(".vtab-btn")).find((b) => b.dataset.vtab === tab);
  if (!btn) return false;
  revealGroupInStrip(strip, tab);
  btn.click();
  if (sub) {
    const pid = strip.id.replace(/^vtab-strip-/, "");
    const drawer = document.getElementById(`drawer-${tab}-${pid}`);
    const subBtn = Array.from(drawer?.querySelectorAll<HTMLElement>(".goal-subtab-btn") ?? []).find(
      (b) => b.dataset.gtab === sub,
    );
    subBtn?.click();
  }
  return true;
}

interface RailTabs {
  ids: string[];
  labels: Record<string, string>;
  active: string | null;
}

/** The tabs the rail offers right now: its buttons, minus any feature-gated (display:none) ones. */
export function readRailTabs(strip: ParentNode | null): RailTabs {
  const out: RailTabs = { ids: [], labels: {}, active: null };
  if (!strip) return out;
  strip.querySelectorAll<HTMLElement>(".vtab-btn[data-vtab]").forEach((btn) => {
    const id = btn.dataset.vtab;
    if (!id || btn.style.display === "none") return;
    out.ids.push(id);
    const title = (btn.getAttribute("title") || "").split(/\s+[—-]\s+/)[0].trim();
    if (title) out.labels[id] = title;
    if (btn.classList.contains("active")) out.active = id;
  });
  return out;
}

// ---------------------------------------------------------------------------
// The launcher: button + popover
// ---------------------------------------------------------------------------

export interface WaffleDeps {
  /** Element the launcher slot is inserted into, as its first child (the top bar). */
  host: HTMLElement | null;
  /** The ACTIVE project's vtab strip, or null when no project is open. */
  getStrip: () => HTMLElement | null;
  /** Pending-work counts for the active project (see readWaffleBadges). */
  getBadges?: () => Record<string, number>;
  /** Defaults to window.localStorage, guarded. Pass null to run without persistence. */
  storage?: Storage | null;
  storageKey?: string;
  rank?: readonly string[];
  threshold?: number;
}

export interface WaffleController {
  button: HTMLButtonElement;
  popover: HTMLElement;
  open(): void;
  close(returnFocus?: boolean): void;
  isOpen(): boolean;
  /** Re-read badges/availability: refreshes the dot and, when open, the grid. */
  refresh(): void;
  /** Count one use of `tab` (see recordWaffleUse, the module-level entry point). */
  recordUse(tab: string): void;
  getModel(): WaffleModel;
  getState(): WaffleState;
  destroy(): void;
}

const DEFAULT_STORAGE_KEY = "meridian_waffle.v1";

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function tileHtml(e: WaffleEntry): string {
  const badge =
    e.badge > 0
      ? `<span class="waffle-badge" data-badge-tab="${esc(e.tab)}" aria-hidden="true">${e.badge > 99 ? "99+" : e.badge}</span>`
      : "";
  const name = e.label + (e.pinned ? ", pinned" : "") + (e.badge > 0 ? `, ${e.badge} pending` : "");
  return (
    `<button type="button" class="waffle-tile" role="menuitem" tabindex="-1" ` +
    `data-waffle-key="${esc(e.key)}" data-waffle-tab="${esc(e.tab)}" data-group="${esc(e.group ?? "")}" ` +
    `data-pinned="${e.pinned}" aria-label="${esc(name)}" aria-keyshortcuts="P"` +
    `${e.active ? ' aria-current="true"' : ""}>` +
    `<span class="waffle-pin" data-waffle-pin="${esc(e.tab)}" title="${e.pinned ? "Unpin" : "Pin"} ${esc(e.label)}" aria-hidden="true">${pinSvg(e.pinned)}</span>` +
    `<span class="waffle-icon-wrap">${waffleIconSvg(e.icon, 20)}</span>` +
    `<span class="waffle-label">${esc(e.label)}</span>${badge}</button>`
  );
}

function subHtml(e: WaffleEntry): string {
  return (
    `<button type="button" class="waffle-sub" role="menuitem" tabindex="-1" ` +
    `data-waffle-key="${esc(e.key)}" data-waffle-tab="${esc(e.tab)}" data-waffle-sub="${esc(e.sub ?? "")}" ` +
    `data-group="${esc(e.group ?? "")}">${esc(e.label)}</button>`
  );
}

function sectionHtml(section: WaffleSection): string {
  let html = "";
  let openParent: string | null = null;
  const closeSubs = () => {
    if (openParent !== null) html += "</div>";
    openParent = null;
  };
  for (const row of section.rows) {
    if (row.kind === "subs") {
      if (openParent !== row.parent) {
        closeSubs();
        openParent = row.parent ?? "";
        html +=
          `<div class="waffle-subs" role="group" aria-label="${esc(row.parentLabel ?? "")} sections" ` +
          `data-waffle-subs-of="${esc(row.parent ?? "")}">` +
          `<div class="waffle-subs-title" aria-hidden="true">${esc(row.parentLabel ?? "")}</div>`;
      }
      html += `<div class="waffle-row" role="none">${row.entries.map(subHtml).join("")}</div>`;
    } else {
      closeSubs();
      html += `<div class="waffle-row" role="none">${row.entries.map(tileHtml).join("")}</div>`;
    }
  }
  closeSubs();
  return (
    `<div class="waffle-section" role="group" aria-label="${esc(section.label)}" data-waffle-section="${section.id}">` +
    `<div class="waffle-heading" aria-hidden="true">${esc(section.label)}</div>${html}</div>`
  );
}

let current: WaffleController | null = null;

/** Refresh the mounted launcher (badge dot, open grid). No-op when none is mounted. */
export function refreshWaffle(): void {
  current?.refresh();
}

// Depth counter, not a boolean: scripted navigation can nest (a tour step that
// calls a helper that also wraps its own click).
let scriptedDepth = 0;

/**
 * Run `fn` (a scripted navigation: restoring the last tab on page load, the demo
 * tour) without counting the `.vtab-btn` clicks it makes as uses. The launcher's
 * own activation uses it too, because it already counted the use itself.
 */
export function withoutWaffleUse<T>(fn: () => T): T {
  scriptedDepth += 1;
  try {
    return fn();
  } finally {
    scriptedDepth -= 1;
  }
}

/**
 * Count one use of the tab `tab`. dashboard.ts calls this from the rail's shared
 * `.vtab-btn` onclick, so every way of opening a tab trains the order (the rail,
 * HITL/timeline jumps, deep links, and the waffle itself), not only the waffle.
 * No-op while a scripted navigation runs or when no launcher is mounted.
 */
export function recordWaffleUse(tab: string): void {
  if (scriptedDepth > 0) return;
  current?.recordUse(tab);
}

/**
 * Mount the launcher: a 9-dot button in `host` and a popover on <body>.
 * Idempotent: mounting again tears the previous launcher down first.
 */
export function mountWaffle(deps: WaffleDeps): WaffleController | null {
  const host = deps.host;
  if (!host) return null;
  current?.destroy();

  const storage = deps.storage === undefined ? safeLocalStorage() : deps.storage;
  const storageKey = deps.storageKey ?? DEFAULT_STORAGE_KEY;
  // `synced` is what storage held the last time THIS page read or wrote it (null =
  // nothing stored). It lets a mutation tell "another tab changed storage since
  // I looked" (adopt that) from "storage is unchanged" (keep the in-memory state,
  // which is the only complete copy when storage is blocked or full).
  const initialRaw = readRawState(storage, storageKey);
  let synced: string | null = initialRaw === undefined ? null : initialRaw;
  let state = parseRawState(initialRaw);
  let isOpen = false;
  let query = "";
  let focusKey: string | null = null;
  let model: WaffleModel = { sections: [], keys: [], count: 0, query: "" };

  const slot = document.createElement("div");
  slot.className = "waffle-slot";
  const button = document.createElement("button");
  button.type = "button";
  button.id = "waffle-btn";
  button.className = "waffle-btn";
  button.setAttribute("aria-haspopup", "dialog");
  button.setAttribute("aria-expanded", "false");
  button.setAttribute("aria-controls", "waffle-popover");
  button.setAttribute("aria-label", "Dashboard tabs");
  button.title = "Jump to a tab";
  button.innerHTML = `${waffleGlyphSvg()}<span class="waffle-dot" hidden></span>`;
  slot.appendChild(button);
  host.insertBefore(slot, host.firstChild);
  const dot = button.querySelector<HTMLElement>(".waffle-dot")!;

  const popover = document.createElement("div");
  popover.id = "waffle-popover";
  popover.className = "waffle-popover";
  popover.setAttribute("role", "dialog");
  popover.setAttribute("aria-modal", "true");
  popover.setAttribute("aria-label", "Dashboard tabs");
  popover.hidden = true;
  // Focusable container: a click on a heading or gap keeps key events (Esc) inside the dialog.
  popover.tabIndex = -1;
  popover.innerHTML =
    `<div class="waffle-search">${searchSvg()}` +
    `<input type="search" class="waffle-filter" placeholder="Filter tabs" aria-label="Filter tabs" ` +
    `aria-controls="waffle-menu" autocomplete="off" spellcheck="false" enterkeyhint="go"></div>` +
    `<div class="waffle-body" id="waffle-menu" role="menu" aria-label="Dashboard tabs"></div>` +
    `<div class="waffle-sr" role="status" aria-live="polite"></div>` +
    `<div class="waffle-hint">Arrows move, Enter opens, P pins, Esc closes</div>`;
  document.body.appendChild(popover);
  const input = popover.querySelector<HTMLInputElement>(".waffle-filter")!;
  const body = popover.querySelector<HTMLElement>(".waffle-body")!;
  const live = popover.querySelector<HTMLElement>(".waffle-sr")!;

  /**
   * Bring `state` up to date with storage when another tab (or window) has
   * written since this page last read or wrote it. Returns true when it did.
   * Storage that cannot be read, or that is unchanged since our own last sync,
   * leaves the in-memory state alone.
   */
  const syncFromStorage = (): boolean => {
    const raw = readRawState(storage, storageKey);
    if (raw === undefined || raw === synced) return false;
    synced = raw;
    state = parseRawState(raw);
    return true;
  };

  /**
   * Every write goes through here as a read-modify-write: re-read the LATEST
   * stored value, apply only this one change to it, then save. Writing the state
   * this page loaded at mount would silently drop whatever another open tab has
   * pinned or counted since (the whole blob is one localStorage value).
   */
  const mutate = (change: (latest: WaffleState) => WaffleState): void => {
    syncFromStorage();
    state = change(state);
    if (saveState(storage, storageKey, state)) synced = serializeState(state);
  };

  // A badge source that throws must never take the launcher down with it.
  const readBadges = (): Record<string, number> => {
    try {
      return deps.getBadges ? deps.getBadges() : {};
    } catch {
      return {};
    }
  };

  const compute = (): WaffleModel => {
    const strip = deps.getStrip();
    const rail = readRailTabs(strip);
    return buildWaffleModel({
      available: rail.ids,
      state,
      query,
      badges: readBadges(),
      activeTab: rail.active,
      labels: rail.labels,
      rank: deps.rank,
      threshold: deps.threshold,
    });
  };

  const updateDot = () => {
    const hitl = cleanBadge(readBadges().hitl);
    dot.hidden = hitl <= 0;
    button.setAttribute(
      "aria-label",
      hitl > 0 ? `Dashboard tabs, ${hitl} review request${hitl === 1 ? "" : "s"} pending` : "Dashboard tabs",
    );
  };

  const entryEl = (key: string | null): HTMLElement | null => {
    if (!key) return null;
    return Array.from(body.querySelectorAll<HTMLElement>("[data-waffle-key]")).find(
      (el) => el.dataset.waffleKey === key,
    ) ?? null;
  };

  const setRoving = (key: string | null) => {
    focusKey = key;
    body.querySelectorAll<HTMLElement>("[data-waffle-key]").forEach((el) => {
      el.tabIndex = el.dataset.waffleKey === key ? 0 : -1;
    });
  };

  // The HTML last written into the grid. Refreshes arrive constantly (the HITL
  // fallback poll fires every 10 s, every sprint loader calls refreshWaffle) and
  // most change nothing visible: skipping an identical swap keeps the tile nodes,
  // hover, focus and a half-finished click alive instead of rebuilding them.
  let lastHtml = "";

  /** Key of the grid entry that holds DOM focus right now (however it got it), else null. */
  const heldFocusKey = (): string | null => {
    const a = document.activeElement as HTMLElement | null;
    if (!a || !body.contains(a)) return null;
    return a.closest<HTMLElement>("[data-waffle-key]")?.dataset.waffleKey ?? null;
  };

  const render = () => {
    // Read focus BEFORE touching the DOM: replacing the grid removes the focused
    // tile, and focus then falls to <body>, where arrows, Enter, P and Esc go
    // dead while the popover is still open.
    const hadGridFocus = !!document.activeElement && body.contains(document.activeElement);
    const heldKey = heldFocusKey();
    if (heldKey) focusKey = heldKey; // keep the roving tab stop on what really has focus

    model = compute();
    updateDot();
    let html: string;
    if (!model.sections.length) {
      const strip = deps.getStrip();
      html = query
        ? `<div class="waffle-empty" role="none">No tabs match &quot;${esc(query)}&quot;</div>`
        : `<div class="waffle-empty" role="none">${strip ? "No tabs available" : "Open a project to jump between its tabs"}</div>`;
    } else {
      html = model.sections.map(sectionHtml).join("");
    }
    if (html !== lastHtml) {
      const scroll = body.scrollTop;
      body.innerHTML = html;
      body.scrollTop = scroll;
      lastHtml = html;
    }
    const flat = model.keys.flat();
    setRoving(focusKey && flat.includes(focusKey) ? focusKey : flat[0] ?? null);
    live.textContent = query ? (model.count === 1 ? "1 match" : `${model.count} matches`) : "";

    if (hadGridFocus && !body.contains(document.activeElement)) {
      // The swap dropped focus. Put it back on the same entry, or on the first one
      // when that tab has left the rail, or on the filter when nothing is left.
      const target = heldKey && flat.includes(heldKey) ? heldKey : flat[0] ?? null;
      if (target) focusEntry(target);
      else input.focus();
    }
  };

  const place = () => {
    const r = button.getBoundingClientRect();
    popover.style.maxHeight = "";
    const placement = computePopoverPlacement({
      anchor: { left: r.left, bottom: r.bottom },
      width: popover.offsetWidth || 352,
      viewport: { width: window.innerWidth, height: window.innerHeight },
    });
    popover.style.left = `${placement.left}px`;
    popover.style.top = `${placement.top}px`;
    popover.style.maxHeight = `${placement.maxHeight}px`;
  };

  const focusEntry = (key: string | null) => {
    const el = entryEl(key);
    if (!el) return;
    setRoving(key);
    el.focus();
  };

  const onDocPointer = (ev: Event) => {
    const t = ev.target as Node | null;
    if (t && (popover.contains(t) || button.contains(t))) return;
    close(false);
  };
  const onFocusIn = (ev: FocusEvent) => {
    const t = ev.target as Node | null;
    if (t && (popover.contains(t) || button.contains(t))) return;
    close(false);
  };
  const onResize = () => {
    if (isOpen) place();
  };
  // Another tab changed our key (or cleared storage, key === null): adopt it and,
  // when the grid is open, redraw so its Pinned section, Recent section and
  // usage order follow. A change to some other key is none of our business.
  const onStorage = (ev: StorageEvent) => {
    if (ev.key !== null && ev.key !== storageKey) return;
    if (syncFromStorage() && isOpen) render();
  };
  // Escape is handled at the document, not on the popover: if focus ever ends up
  // on <body> while the dialog is open (an element removed under it, a click on a
  // gap) a popover-level listener would never hear it and Esc would be dead.
  const onDocKey = (ev: KeyboardEvent) => {
    // isComposing: Esc cancels an IME composition in the filter box, not the dialog.
    if (ev.key !== "Escape" || ev.isComposing) return;
    ev.preventDefault();
    ev.stopPropagation();
    close(true);
  };

  function close(returnFocus = true): void {
    if (!isOpen) return;
    isOpen = false;
    popover.hidden = true;
    button.setAttribute("aria-expanded", "false");
    document.removeEventListener("keydown", onDocKey, true);
    document.removeEventListener("mousedown", onDocPointer, true);
    document.removeEventListener("touchstart", onDocPointer, true);
    document.removeEventListener("focusin", onFocusIn, true);
    window.removeEventListener("resize", onResize);
    query = "";
    input.value = "";
    if (returnFocus) button.focus();
  }

  function open(): void {
    if (isOpen) return;
    isOpen = true;
    query = "";
    input.value = "";
    focusKey = null;
    popover.hidden = false;
    button.setAttribute("aria-expanded", "true");
    // A page restored from the back/forward cache, or one whose tab was frozen,
    // can have missed storage events: look at storage again before showing it.
    syncFromStorage();
    render();
    place();
    document.addEventListener("keydown", onDocKey, true);
    document.addEventListener("mousedown", onDocPointer, true);
    document.addEventListener("touchstart", onDocPointer, true);
    document.addEventListener("focusin", onFocusIn, true);
    window.addEventListener("resize", onResize);
    // Desktop: focus the filter so typing filters at once. Touch: land on a tile
    // so the on-screen keyboard does not cover the grid.
    const fine = typeof window.matchMedia !== "function" || window.matchMedia("(pointer: fine)").matches;
    if (fine) input.focus();
    else focusEntry(focusKey);
  }

  // One use of a tab, from any path (see recordWaffleUse). A scripted use can land
  // while the grid is open, so keep an open grid's Recent section current.
  const noteUse = (tab: string) => {
    mutate((latest) => recordUse(latest, tab));
    if (isOpen) render();
  };

  const activateKey = (key: string | null) => {
    if (!key) return;
    const entry = model.sections.flatMap((s) => s.rows.flatMap((r) => r.entries)).find((e) => e.key === key);
    if (!entry) return;
    const strip = deps.getStrip();
    if (!strip) return;
    close(true);
    // The rail button's onclick also calls recordWaffleUse; the scripted guard makes
    // it skip, so this activation counts exactly once, and only if a button was there.
    const activated = withoutWaffleUse(() => activateWaffleTab(strip, entry.tab, entry.sub));
    if (activated) noteUse(entry.tab);
    updateDot();
  };

  const togglePinFor = (tab: string, keyToFocus: string) => {
    // The user acted on what the grid showed: decide pin-or-unpin from that, then
    // apply that one pin change to the latest stored list. (Flipping whatever the
    // latest list says could turn "unpin" into "pin" if another tab got there first.)
    const wantPinned = !effectivePins(state).includes(tab);
    mutate((latest) => setPinned(latest, tab, wantPinned));
    // Re-rendering replaces the tile under the cursor (and may move it to another
    // section): if focus was inside the popover, put it back on the same tile so
    // keyboard users do not fall out of the dialog.
    const hadFocus = popover.contains(document.activeElement);
    render();
    if (hadFocus) focusEntry(keyToFocus);
  };

  button.addEventListener("click", () => (isOpen ? close(true) : open()));
  button.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowDown" && !isOpen) {
      ev.preventDefault();
      open();
    }
  });

  input.addEventListener("input", () => {
    query = input.value;
    focusKey = null;
    render();
  });

  body.addEventListener("click", (ev) => {
    const target = ev.target as HTMLElement;
    const pin = target.closest<HTMLElement>("[data-waffle-pin]");
    if (pin) {
      ev.preventDefault();
      ev.stopPropagation();
      const pinTab = pin.dataset.wafflePin || "";
      togglePinFor(pinTab, `tab:${pinTab}`);
      return;
    }
    const el = target.closest<HTMLElement>("[data-waffle-key]");
    if (el) activateKey(el.dataset.waffleKey || null);
  });

  popover.addEventListener("keydown", (ev) => {
    const target = ev.target as HTMLElement;
    if (ev.key === "Tab") {
      // Tab trap: the popover has two tab stops (filter, roving tile). Wrap at both ends.
      const stops = [input, body.querySelector<HTMLElement>('[data-waffle-key][tabindex="0"]')].filter(
        (el): el is HTMLElement => !!el,
      );
      const idx = stops.indexOf(document.activeElement as HTMLElement);
      if (ev.shiftKey && idx <= 0) {
        ev.preventDefault();
        stops[stops.length - 1].focus();
      } else if (!ev.shiftKey && idx === stops.length - 1) {
        ev.preventDefault();
        stops[0].focus();
      }
      return;
    }
    if (target === input) {
      const first = model.keys[0]?.[0] ?? null;
      if (ev.key === "ArrowDown") {
        ev.preventDefault();
        focusEntry(first);
      } else if (ev.key === "Enter") {
        ev.preventDefault();
        activateKey(first);
      }
      return;
    }
    const el = target.closest<HTMLElement>("[data-waffle-key]");
    if (!el) return;
    const key = el.dataset.waffleKey || null;
    if (ev.key === "ArrowUp" && model.keys[0]?.includes(key as string)) {
      // Up from the first row returns to the filter box.
      ev.preventDefault();
      input.focus();
      return;
    }
    const next = moveFocus(model.keys, key, ev.key, ev.ctrlKey || ev.metaKey);
    if (next !== null) {
      ev.preventDefault();
      focusEntry(next);
      return;
    }
    if ((ev.key === "p" || ev.key === "P") && !ev.ctrlKey && !ev.metaKey && !ev.altKey) {
      const tab = el.dataset.waffleTab;
      if (tab && el.dataset.waffleSub === undefined) {
        ev.preventDefault();
        togglePinFor(tab, key as string);
      }
    }
  });

  updateDot();
  // Listening from mount, not only while open: a closed launcher in a long-lived
  // tab should already hold the other tabs' pins when it is next opened.
  window.addEventListener("storage", onStorage);

  const controller: WaffleController = {
    button,
    popover,
    open,
    close,
    isOpen: () => isOpen,
    refresh: () => {
      if (isOpen) render();
      else updateDot();
    },
    recordUse: noteUse,
    getModel: () => model,
    getState: () => state,
    destroy: () => {
      close(false);
      window.removeEventListener("storage", onStorage);
      slot.remove();
      popover.remove();
      if (current === controller) current = null;
    },
  };
  current = controller;
  return controller;
}
