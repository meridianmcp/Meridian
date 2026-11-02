// dashboard-workspace.ts -- "Local overview" view (workspace-overview-spec-r2, section 7).
//
// Pure grouping and labelling functions (covered by dashboard-workspace.test.ts) plus
// mountWorkspaceView, which dashboard.ts calls once at boot. The module never fetches on
// its own: the caller passes fetchOverview, and the view fetches once per open and once per
// Refresh click. There is no polling.
//
// Session liveness is not a second rule here. The backend `live` field is used when present,
// and dashboard-utils isLiveSession is the fallback only when that field is absent.
import { escapeHtml, isLiveSession } from "./dashboard-utils";

// ---------------------------------------------------------------------------
// Types. Field names follow the JSON contract of GET /workspace/overview (spec section 2).
// ---------------------------------------------------------------------------

export type SnapshotState = 'ok' | 'missing' | 'corrupt' | 'unreadable';

export type RepoGroup = 'enforced_fresh' | 'guard_not_enforcing' | 'installed_no_index' | 'index_only' | 'not_installed';

export interface ClassifyContext {
  serverGuardMode: 'off' | 'advisory' | 'enforce' | string;
  snapshotState: SnapshotState;
}

export interface GuardFacts {
  settings_state: 'present' | 'absent' | 'unreadable' | 'not_scanned';
  registered: boolean | null;
  components: string[];
  complete?: boolean | null;
  installed_mode: 'enforce' | 'advisory' | null;
  scope?: 'project' | 'user' | null;
  runtime_ok: boolean | null;
  problems: string[];
  record?: { installed_at: string | null; mode: string | null } | null;
  record_mode_matches?: boolean | null;
}

export interface IndexFacts {
  // null: the index was not read (the scan budget stopped first). It is unknown, never 'none'.
  mode: 'own' | 'pin' | 'canonical' | 'ancestor' | 'none' | null;
  why?: string;
  pinned?: boolean | null;
  project?: string | null;
  index_root?: string | null;
  shadowed?: string[];
  partial: boolean | null;
  present: boolean | null;
  fresh: boolean | null;
  age_days: number | null;
  last_index_write?: string | null;
  nodes: number | null;
  files?: number | null;
  db_bytes: number | null;
  covers_repo_root: boolean | null;
  slug_match?: boolean | null;
  snapshot_state?: SnapshotState;
}

export interface CodebaseMemoryFacts {
  managed_entry: boolean | null;
  index: IndexFacts;
}

export interface ActivityFacts {
  last_guard_event_at: string | null;
  last_guard_event_reason: string | null;
  last_prospect_receipt_at: string | null;
  last_session_seen_at: string | null;
  receipt_window_days?: number;
}

export interface RepoFacts {
  key: string;
  path: string;
  name: string;
  sources?: string[];
  source_paths?: string[];
  hostnames?: string[];
  location?: { match: 'same_machine' | 'other_machine' | 'unknown'; hostnames?: string[] };
  projects?: Array<{ id: string; name: string }>;
  canonical?: unknown;
  guard: GuardFacts;
  codebase_memory: CodebaseMemoryFacts;
  activity?: ActivityFacts;
}

export interface ProjectFacts {
  id: string;
  name: string;
  status?: string | null;
  priority?: number | null;
  repo_keys: string[];
  session_counts?: { total: number; live: number; status_active: number; status_idle: number };
  last_session_seen_at?: string | null;
  activity: 'live' | 'recent' | 'idle' | 'none';
}

export interface SessionFacts {
  id: string;
  name: string;
  project_id?: string;
  project_name?: string;
  human_id?: string | null;
  client_type?: string | null;
  agent_framework?: string | null;
  session_type?: string | null;
  status: string;
  last_seen?: string | null;
  last_seen_utc?: string | null;
  age_s: number | null;
  live?: boolean;
  location?: string;
  worktree?: { path: string; branch: string | null; repo_key: string } | null;
}

export interface RuntimeFacts {
  version: string | null;
  hostname: string | null;
  location: string;
  deployment?: string;
  database?: { backend: string; connection_name: string | null };
  guard: {
    mode: string;
    mode_scope?: string;
    disabled_rules?: string[];
    user_scope_installed?: boolean | null;
    guard_dir?: string | null;
  };
  tunnel: {
    client_process: string;
    client_process_reason?: string;
    live_slot_claims?: number | null;
    hosted_connection: string;
    hosted_connection_reason?: string;
  };
}

export interface SnapshotFacts {
  path?: string;
  present?: boolean;
  error: 'missing' | 'corrupt' | 'unreadable' | null;
  schema?: string | null;
  built_at?: number | null;
  built_age_s: number | null;
  stats: Record<string, number> | null;
  rows_total?: number;
  stale_after_days?: number;
}

export interface ScanFacts {
  complete: boolean;
  bytes_read?: number;
  budget_bytes?: number;
  elapsed_s?: number;
  stopped_reason: 'bytes' | 'time' | null;
}

export interface GapFacts {
  code: string;
  message: string;
  source?: string;
}

export interface WorkspaceOverview {
  schema?: string;
  generated_at?: string | null;
  runtime: RuntimeFacts;
  repos: {
    snapshot: SnapshotFacts;
    items: RepoFacts[];
    total?: number;
    truncated: boolean;
    limit?: number;
    scan?: ScanFacts;
  };
  sessions: {
    live_window_s?: number;
    items: SessionFacts[];
    total?: number;
    truncated: boolean;
    totals?: { total: number; live: number; status_active: number; status_idle: number };
  };
  projects: { items: ProjectFacts[]; recent_window_s?: number };
  gaps: GapFacts[];
  errors: Array<{ section: string; code: string }>;
}

// ---------------------------------------------------------------------------
// Constants and labels (fixed template text from spec sections 3.3 and 7).
// ---------------------------------------------------------------------------

export const REPO_GROUP_ORDER: readonly RepoGroup[] = [
  'enforced_fresh',
  'guard_not_enforcing',
  'installed_no_index',
  'index_only',
  'not_installed',
];

export const REPO_GROUP_LABEL: Readonly<Record<RepoGroup, string>> = {
  enforced_fresh: 'Enforced and fresh',
  guard_not_enforcing: 'Guard not enforcing',
  installed_no_index: 'Installed, no index',
  index_only: 'Index only, no guard',
  not_installed: 'Nothing registered',
};

// Hook components a guard install carries (hook_paths.GUARD_COMPONENTS, spec section 2.2).
const GUARD_COMPONENTS = ['pre', 'post', 'brief'] as const;

// Reused backend limit (cbm_registry.STALE_DAYS, spec section 3.1). Only used in reason text.
const STALE_DAYS = 7;

// ---------------------------------------------------------------------------
// Repo classification (spec section 3.3). Rules run in order; the first match wins.
// ---------------------------------------------------------------------------

function guardStateUnknown(g: GuardFacts): boolean {
  return g.settings_state === 'unreadable' || g.settings_state === 'not_scanned';
}

// A null index mode means the index was never read: the scan budget stopped first (backend
// _index_unknown, why "not read: scan budget reached"). It is unknown. It is never 'none' and it
// is never a known mode, so each rule below checks it on its own.
function indexUnread(i: IndexFacts): boolean {
  return i.mode == null;
}

// The backend's visible reason for an unread index. "not recorded" stands in when none came back.
function unreadIndexReason(i: IndexFacts): string {
  return i.why ? i.why : 'not recorded';
}

function isEnforcedFresh(r: RepoFacts, ctx: ClassifyContext): boolean {
  const g = r.guard;
  const i = r.codebase_memory.index;
  return g.settings_state === 'present'
    && g.registered === true
    && g.components.includes('pre')
    && g.installed_mode === 'enforce'
    && g.runtime_ok === true
    && ctx.serverGuardMode === 'enforce'
    && !indexUnread(i)
    && (i.mode === 'own' || i.mode === 'pin')
    && i.present === true
    && i.fresh === true
    && i.partial !== true
    && i.covers_repo_root === true
    && ctx.snapshotState === 'ok';
}

function isGuardNotEnforcing(r: RepoFacts): boolean {
  const i = r.codebase_memory.index;
  // An unread index (null) is not 'none', so it stays in this group: the guard is not shown to be enforcing.
  return (r.guard.registered === true || guardStateUnknown(r.guard))
    && (indexUnread(i) || i.mode !== 'none');
}

function isInstalledNoIndex(r: RepoFacts): boolean {
  return (r.guard.registered === true || r.codebase_memory.managed_entry === true)
    && r.codebase_memory.index.mode === 'none';
}

// Rule 4 (spec 3.3). The spec names only settings_state "present". Decision F2: "absent" (no
// .claude/settings.json at all, the default no-guard state) is treated the same way, because the
// backend reports registered=false for it. Without this, a repo with an index and no guard file
// fell through to "Nothing registered" even though its index is there.
function isIndexOnly(r: RepoFacts): boolean {
  const i = r.codebase_memory.index;
  return (r.guard.settings_state === 'present' || r.guard.settings_state === 'absent')
    && r.guard.registered === false
    && !indexUnread(i)
    && i.mode !== 'none';
}

// Suffixes for rules 3 to 5 (spec section 3.3, "Suffixes for rules 3 to 5").
function classifySuffixes(r: RepoFacts, ctx: ClassifyContext): string {
  let text = '';
  const i = r.codebase_memory.index;
  if (indexUnread(i)) text += `; index unknown (${unreadIndexReason(i)})`;
  if (guardStateUnknown(r.guard)) text += `; guard state unknown (settings.json ${r.guard.settings_state})`;
  if (r.codebase_memory.managed_entry == null) text += '; .mcp.json unreadable';
  if (r.guard.registered === true && ctx.serverGuardMode !== 'enforce') {
    text += `; server guard mode is ${ctx.serverGuardMode}`;
  }
  return text;
}

// Rule 2 reasons, first match (spec section 3.3 table).
function notEnforcingReason(r: RepoFacts, ctx: ClassifyContext): string {
  const g = r.guard;
  const i = r.codebase_memory.index;
  if (g.installed_mode === 'advisory') return 'Installed in advisory mode; denies are advisory only';
  if (g.registered === true && g.runtime_ok === false) {
    return g.problems.length > 0
      ? `Guard runtime missing: ${g.problems.join('; ')}`
      : 'Guard runtime check failed';
  }
  if (g.registered === true) {
    const missing = GUARD_COMPONENTS.filter((c) => !g.components.includes(c));
    if (missing.length > 0) return `Guard hooks incomplete: missing ${missing.join(', ')}`;
  }
  if (ctx.serverGuardMode !== 'enforce') {
    return `Server guard mode is ${ctx.serverGuardMode} (server process); not enforcing`;
  }
  if (guardStateUnknown(g)) return `Guard state unknown: settings.json ${g.settings_state}`;
  if (indexUnread(i)) return `Index unknown (${unreadIndexReason(i)})`;
  if (i.partial === true) return 'Index is partial (unfinished or broken build)';
  if (i.present === false) return 'Index database missing; snapshot row is stale';
  if (i.mode === 'canonical') return 'Index inherited from the canonical checkout';
  if (i.mode === 'ancestor') return 'Index inherited from a parent directory';
  if (i.covers_repo_root == null) return 'Index coverage unknown (index read failed)';
  if (i.covers_repo_root === false) return 'Index does not cover the repo root';
  if (i.fresh === false) {
    return i.age_days == null
      ? `index stale, last write time unknown (limit ${STALE_DAYS})`
      : `index stale, last write ${i.age_days} days ago (limit ${STALE_DAYS})`;
  }
  return 'Guard not enforcing; no single cause recorded';
}

function installedNoIndexReason(r: RepoFacts, ctx: ClassifyContext): string {
  let text: string;
  if (ctx.snapshotState !== 'ok') {
    text = `Index state unknown: snapshot ${ctx.snapshotState}`;
  } else if (r.guard.registered === true) {
    text = 'Installed, no index covers this repo';
  } else {
    text = 'Configured in .mcp.json; no index covers this repo';
  }
  return text + classifySuffixes(r, ctx);
}

function indexOnlyReason(r: RepoFacts): string {
  const i = r.codebase_memory.index;
  // Rule 4 never passes a null mode, so this branch is defensive: an unread index never reads as "Index present".
  if (indexUnread(i)) return `Index unknown (${unreadIndexReason(i)}); no guard registered in this repo`;
  switch (i.mode) {
    case 'own':
      return 'Index present (own); no guard registered in this repo';
    case 'pin':
      return 'Index pinned to this repo (own); no guard registered in this repo';
    case 'canonical':
      return 'Index inherited from the canonical checkout; no guard registered in this repo';
    case 'ancestor':
      return 'Index inherited from a parent directory; no guard registered in this repo';
    default:
      return 'Index present; no guard registered in this repo';
  }
}

function notInstalledReason(r: RepoFacts, ctx: ClassifyContext): string {
  let text = 'Nothing registered for this repo';
  if (ctx.snapshotState !== 'ok') text += `; index state unknown (snapshot ${ctx.snapshotState})`;
  return text + classifySuffixes(r, ctx);
}

/** Classify one repo into exactly one group, with the reason text for that group. */
export function classifyRepo(repo: RepoFacts, ctx: ClassifyContext): { group: RepoGroup; reason: string } {
  if (isEnforcedFresh(repo, ctx)) {
    const age = repo.codebase_memory.index.age_days;
    return {
      group: 'enforced_fresh',
      reason: age == null
        ? 'Guard enforcing; index fresh (age unknown)'
        : `Guard enforcing; index fresh (age ${age} days)`,
    };
  }
  if (isGuardNotEnforcing(repo)) return { group: 'guard_not_enforcing', reason: notEnforcingReason(repo, ctx) };
  if (isInstalledNoIndex(repo)) return { group: 'installed_no_index', reason: installedNoIndexReason(repo, ctx) };
  if (isIndexOnly(repo)) return { group: 'index_only', reason: indexOnlyReason(repo) };
  return { group: 'not_installed', reason: notInstalledReason(repo, ctx) };
}

export interface GroupedRepos {
  groups: Record<RepoGroup, Array<RepoFacts & { reason: string }>>;
  counts: Record<RepoGroup, number>;
}

/** Partition repos into the five groups. The group counts always sum to the input length. */
export function groupRepos(repos: readonly RepoFacts[], ctx: ClassifyContext): GroupedRepos {
  const groups: Record<RepoGroup, Array<RepoFacts & { reason: string }>> = {
    enforced_fresh: [],
    guard_not_enforcing: [],
    installed_no_index: [],
    index_only: [],
    not_installed: [],
  };
  const counts: Record<RepoGroup, number> = {
    enforced_fresh: 0,
    guard_not_enforcing: 0,
    installed_no_index: 0,
    index_only: 0,
    not_installed: 0,
  };
  for (const repo of repos) {
    const { group, reason } = classifyRepo(repo, ctx);
    groups[group].push({ ...repo, reason });
    counts[group] += 1;
  }
  return { groups, counts };
}

/** Projects with at least one repo key whose repo has a registered guard. Each project counts once. */
export function countProjectsWithGuard(projects: readonly ProjectFacts[], repos: readonly RepoFacts[]): number {
  const registered = new Set<string>();
  for (const repo of repos) {
    if (repo.guard.registered === true) registered.add(repo.key);
  }
  let count = 0;
  for (const project of projects) {
    if (project.repo_keys.some((key) => registered.has(key))) count += 1;
  }
  return count;
}

// ---------------------------------------------------------------------------
// Labels shared by the grouping and the renderer.
// ---------------------------------------------------------------------------

/** "unknown: <reason>". Never returns a blank. */
export function unknownText(reason: string): string {
  return `unknown: ${reason}`;
}

/**
 * Compact age for a number of seconds: "42s", "5m", "2h", "2d". null (or a non-finite value)
 * gives "unknown". A negative age (server clock behind last_seen) keeps its sign.
 */
export function formatAgeSeconds(s: number | null | undefined): string {
  if (typeof s !== 'number' || !Number.isFinite(s)) return 'unknown';
  const magnitude = Math.abs(s);
  const sign = s < 0 && magnitude >= 1 ? '-' : '';
  if (magnitude < 60) return `${sign}${Math.floor(magnitude)}s`;
  if (magnitude < 3600) return `${sign}${Math.floor(magnitude / 60)}m`;
  if (magnitude < 86400) return `${sign}${Math.floor(magnitude / 3600)}h`;
  return `${sign}${Math.floor(magnitude / 86400)}d`;
}

// ---------------------------------------------------------------------------
// Rendering. Every dynamic value goes through escapeHtml. chip() takes raw text and escapes it.
// ---------------------------------------------------------------------------

function h(value: unknown): string {
  return escapeHtml(value);
}

/** Escaped text for a value that may be unknown: the fallback when absent, never a blank. */
function show(value: unknown, fallback = unknownText('not recorded')): string {
  return value === null || value === undefined || value === '' ? h(fallback) : h(value);
}

function chip(text: string, unknown = false, title?: string): string {
  const cls = unknown ? 'ws-chip ws-chip--unknown' : 'ws-chip';
  const tip = title ? ` title="${h(title)}"` : '';
  return `<span class="${cls}"${tip}>${h(text)}</span>`;
}

function note(text: string): string {
  return `<p class="ws-note">${h(text)}</p>`;
}

function locationChip(r: RepoFacts): string {
  const loc = r.location;
  if (loc && (loc.match === 'same_machine' || loc.match === 'other_machine')) {
    const names = (loc.hostnames ?? r.hostnames ?? []).filter((n) => n !== '').join(', ');
    return chip(`recorded hostname: ${names || 'unknown'} (unverified)`);
  }
  return chip('location unknown: no recorded hostname for this repo', true);
}

function guardChip(g: GuardFacts): string {
  if (g.registered === true) {
    const problems = g.problems.length > 0
      ? `<span class="ws-problems">${g.problems.map(h).join('; ')}</span>`
      : '';
    const mode = g.installed_mode == null
      ? chip(unknownText('guard install mode not recorded'), true)
      : chip(`guard: ${g.installed_mode}`);
    return mode + problems;
  }
  if (g.registered === false) return chip('guard: none');
  return chip(`guard: unknown (settings.json ${g.settings_state})`, true);
}

function managedChip(managed: boolean | null): string {
  if (managed === true) return chip('MCP configured');
  if (managed === false) return chip('MCP not configured');
  return chip(unknownText('.mcp.json unreadable'), true);
}

function indexChip(i: IndexFacts): string {
  // An unread index is unknown with its visible reason, never a bare "index: unknown" or "index: null".
  if (indexUnread(i)) return chip(`index: unknown (${unreadIndexReason(i)})`, true);
  if (i.mode === 'none') return chip('index: none');
  const parts = [
    `index: ${i.mode}`,
    i.age_days == null ? unknownText('no last-write time recorded') : `${i.age_days} days`,
    i.nodes == null ? unknownText('node count not recorded') : `${i.nodes} nodes`,
    i.db_bytes == null ? unknownText('database size not read') : `${(i.db_bytes / 1048576).toFixed(1)} MB`,
  ];
  if (i.partial === true) parts.push('partial');
  if (i.present === false) parts.push('database file missing');
  return chip(parts.join(' · '), i.age_days == null && i.nodes == null && i.db_bytes == null);
}

/**
 * Why a null last-receipt time is unknown, from the receipt gaps (spec 2.5, 3.6, 7.5). Returns
 * null when neither gap is present, which is the only case where "none attributed" is shown.
 */
function receiptUnknownReason(gaps: readonly GapFacts[]): string | null {
  const reasons = gaps
    .filter((g) => g.code === 'receipts_default_root_excluded' || g.code === 'receipts_window_truncated')
    .map((g) => g.message);
  return reasons.length > 0 ? reasons.join(' ') : null;
}

function activityItems(a: ActivityFacts | undefined, receiptReason: string | null): string {
  const guardText = a?.last_guard_event_at
    ?? a?.last_guard_event_reason
    ?? unknownText('no guard event reason reported');
  let receiptText: string;
  if (a?.last_prospect_receipt_at) receiptText = a.last_prospect_receipt_at;
  else if (receiptReason !== null) receiptText = unknownText(receiptReason);
  else receiptText = `none attributed in the last ${a?.receipt_window_days ?? 7} days`;
  const sessionText = a?.last_session_seen_at ?? 'no active or idle session';
  return `<li>last guard event: ${h(guardText)}</li>`
    + `<li>last prospect receipt: ${h(receiptText)}</li>`
    + `<li>last session seen (project-level): ${h(sessionText)}</li>`;
}

function renderRepoRow(r: RepoFacts & { reason: string }, receiptReason: string | null): string {
  return `<li class="ws-row">`
    + `<div class="ws-row-head"><strong>${show(r.name)}</strong></div>`
    + `<div class="ws-path">${show(r.path)}</div>`
    + `<div class="ws-chips">${locationChip(r)}${guardChip(r.guard)}`
    + `${managedChip(r.codebase_memory.managed_entry)}${indexChip(r.codebase_memory.index)}</div>`
    + `<ul class="ws-activity">${activityItems(r.activity, receiptReason)}</ul>`
    + `<div class="ws-reason">${h(r.reason)}</div>`
    + `</li>`;
}

function renderHeader(model: WorkspaceOverview): string {
  const rt = model.runtime;
  const mode = rt.guard.mode;
  const banner = mode === 'enforce'
    ? ''
    : `<p class="ws-banner" role="status">${h(`Server guard mode is ${mode}. Repos show installed mode, not effective enforcement.`)}</p>`;
  const tunnel = rt.tunnel.client_process === 'running' ? 'tunnel process found' : 'tunnel process not detected';
  const hosted = rt.tunnel.hosted_connection || 'unknown';
  const hostChip = rt.hostname == null
    ? chip(unknownText('hostname not reported'), true)
    : chip(`host: ${rt.hostname}`);
  const versionChip = rt.version == null
    ? chip(unknownText('version not reported'), true)
    : chip(`version: ${rt.version}`);
  return '<div class="ws-header">'
    + `<div class="ws-chips">${hostChip}${versionChip}${chip(`location: ${rt.location}`)}</div>`
    + `<div class="ws-chips">${chip(`guard mode: ${mode} (server process)`)}</div>`
    + banner
    + `<p class="ws-meta">${h(tunnel)} · hosted connection: ${show(hosted)}</p>`
    + '</div>';
}

function renderErrors(model: WorkspaceOverview): string {
  return model.errors
    .map((e) => note(`${e.section}: This section could not be read (${e.code}).`))
    .join('');
}

function renderCounts(model: WorkspaceOverview, counts: Record<RepoGroup, number>): string {
  const repos = model.repos;
  // Truncation is labelled on each chip (spec 7.2 item 2), not only in the heading.
  const shownLimit = repos.limit ?? repos.items.length;
  const shownNote = repos.truncated ? ` (of the ${shownLimit} shown)` : '';
  const repoChips = REPO_GROUP_ORDER
    .map((g) => chip(`${REPO_GROUP_LABEL[g]}: ${counts[g]}${shownNote}`))
    .join('');

  const sessions = model.sessions.totals ?? {
    total: model.sessions.items.length,
    live: model.sessions.items.filter(sessionIsLive).length,
    status_active: model.sessions.items.filter((s) => s.status === 'active').length,
    status_idle: model.sessions.items.filter((s) => s.status === 'idle').length,
  };
  const sessionChips = chip(`live: ${sessions.live}`)
    + chip(`status active: ${sessions.status_active}`)
    + chip(`status idle: ${sessions.status_idle}`);

  const activity = { live: 0, recent: 0, idle: 0, none: 0 };
  for (const p of model.projects.items) {
    if (p.activity in activity) activity[p.activity] += 1;
  }
  const projectChips = chip(`live: ${activity.live}`)
    + chip(`recent: ${activity.recent}`)
    + chip(`idle: ${activity.idle}`)
    + chip(`none: ${activity.none}`);

  // The project count is taken from the repo list the server returned, which can be capped.
  // The chip says so (F9): it is a count over the shown repos, not over every repo.
  const guardProjects = countProjectsWithGuard(model.projects.items, repos.items);
  const guardNote = repos.truncated ? ` (counted over the ${shownLimit} repos shown)` : '';
  return '<section class="ws-section">'
    + '<h3 class="ws-heading">Counts</h3>'
    + `<p class="ws-muted">Repo groups</p><div class="ws-chips">${repoChips}</div>`
    + `<p class="ws-muted">Sessions</p><div class="ws-chips">${sessionChips}</div>`
    + `<p class="ws-muted">Projects</p><div class="ws-chips">${projectChips}</div>`
    + `<div class="ws-chips">${chip(`projects with a registered guard: ${guardProjects}${guardNote}`)}</div>`
    + '</section>';
}

function renderReposSection(model: WorkspaceOverview, grouped: GroupedRepos): string {
  const repos = model.repos;
  const receiptReason = receiptUnknownReason(model.gaps);
  const parts: string[] = ['<section class="ws-section"><h3 class="ws-heading">Repos</h3>'];
  if (repos.truncated) {
    parts.push(note(`Showing ${repos.limit ?? repos.items.length} of ${repos.total ?? repos.items.length} repos, sorted by path.`));
  }
  // The scan-budget notice is rendered once, in the Gaps section (F10).
  if (repos.items.length === 0) {
    parts.push(note('No repos known on this machine yet. Repos appear once a project reports a working directory, a worktree is registered, or a guard is installed.'));
  } else {
    if (!repos.items.some((r) => r.guard.registered === true)) {
      parts.push(note('No Meridian guard registered in any known repo.'));
    }
    for (const group of REPO_GROUP_ORDER) {
      const rows = grouped.groups[group];
      const open = rows.length > 0 ? ' open' : '';
      const body = rows.length > 0
        ? `<ul class="ws-list">${rows.map((r) => renderRepoRow(r, receiptReason)).join('')}</ul>`
        : '<p class="ws-muted">No repos in this group.</p>';
      parts.push(
        `<details class="ws-group"${open}><summary>${h(REPO_GROUP_LABEL[group])} `
        + `<span class="ws-count">${rows.length}</span></summary>${body}</details>`,
      );
    }
  }
  parts.push('</section>');
  return parts.join('');
}

/**
 * Session liveness for the overview (spec 7.3). The backend `live` flag is used when present;
 * otherwise the shared isLiveSession rule decides. The counts, the row order and the row chip all use it.
 */
function sessionIsLive(s: SessionFacts): boolean {
  return typeof s.live === 'boolean'
    ? s.live
    : isLiveSession({ status: s.status, last_seen: s.last_seen ?? null });
}

// Sort rank: live first, then active but not live, then idle, then everything else.
function sessionRank(s: SessionFacts): number {
  if (sessionIsLive(s)) return 0;
  if (s.status === 'active') return 1;
  if (s.status === 'idle') return 2;
  return 3;
}

function renderSessionRow(s: SessionFacts): string {
  const live = sessionRank(s) === 0;
  const framework = s.client_type ?? s.agent_framework ?? null;
  // show() and h() return escaped text; the worktree text is already escaped when it is built.
  const branch = s.worktree
    ? show(s.worktree.branch, unknownText('branch not recorded'))
    : h('no worktree');
  const seen = s.age_s == null
    ? `last seen ${unknownText('last_seen did not parse')}`
    : s.age_s < 0
      ? 'last seen ahead of server clock'
      : `last seen ${formatAgeSeconds(s.age_s)} ago`;
  const stateChip = live ? chip('live') : chip(s.status === 'active' ? 'active, not live' : s.status);
  return '<li class="ws-row">'
    + `<div class="ws-row-head"><strong>${show(s.name, 'unnamed session')}</strong></div>`
    + `<div class="ws-chips">${stateChip}${chip('location unknown: sessions record no working directory or hostname', true)}</div>`
    + `<div class="ws-meta">project: ${show(s.project_name)} · human: ${show(s.human_id)} · client: ${show(framework)}</div>`
    + `<div class="ws-meta">${h(seen)} · worktree: ${branch}</div>`
    + '</li>';
}

function renderSessionsSection(model: WorkspaceOverview): string {
  const sessions = model.sessions;
  const parts: string[] = ['<section class="ws-section"><h3 class="ws-heading">Sessions</h3>'];
  if (sessions.truncated) {
    parts.push(note(`Showing ${sessions.items.length} of ${sessions.total ?? sessions.items.length} sessions, newest first.`));
  }
  if (sessions.items.length === 0) {
    parts.push(note('No sessions recorded.'));
  } else {
    const ordered = sessions.items
      .map((s, index) => ({ s, index, rank: sessionRank(s) }))
      .sort((a, b) => a.rank - b.rank || a.index - b.index);
    parts.push(`<ul class="ws-list">${ordered.map((x) => renderSessionRow(x.s)).join('')}</ul>`);
  }
  parts.push('</section>');
  return parts.join('');
}

function renderProjectRow(p: ProjectFacts): string {
  const c = p.session_counts;
  const counts = c
    ? `sessions: ${c.total} total, ${c.live} live, ${c.status_active} active, ${c.status_idle} idle`
    : `sessions: ${unknownText('session counts not reported')}`;
  const manualChip = p.status == null
    ? chip(unknownText('manual status not reported'), true, 'Manual project flag, not derived from activity.')
    : chip(`manual: ${p.status}`, false, 'Manual project flag, not derived from activity.');
  return '<li class="ws-row">'
    + `<div class="ws-row-head"><strong>${show(p.name)}</strong></div>`
    + `<div class="ws-chips">${manualChip}`
    + `${chip(`activity: ${p.activity}`)}</div>`
    + `<div class="ws-meta">${h(counts)} · ${h(`${p.repo_keys.length} repos`)}</div>`
    + '</li>';
}

function renderProjectsSection(model: WorkspaceOverview): string {
  const parts: string[] = ['<section class="ws-section"><h3 class="ws-heading">Projects</h3>'];
  if (model.projects.items.length === 0) {
    parts.push(note('No projects.'));
  } else {
    parts.push(`<ul class="ws-list">${model.projects.items.map(renderProjectRow).join('')}</ul>`);
  }
  parts.push('</section>');
  return parts.join('');
}

function renderSnapshotSection(model: WorkspaceOverview): string {
  const s = model.repos.snapshot;
  const parts: string[] = ['<section class="ws-section"><h3 class="ws-heading">Index snapshot</h3>'];
  if (s.error === 'missing') {
    parts.push(note('No codebase-memory index snapshot yet. It is written when a Claude Code session starts in a repo with the guard installed.'));
  } else if (s.error === 'corrupt' || s.error === 'unreadable') {
    parts.push(`<p class="ws-banner">${h(`The index snapshot could not be read (${s.error}). Index state is unknown.`)}</p>`);
  } else {
    const built = s.built_age_s == null
      ? unknownText('snapshot built time not recorded')
      : `built ${formatAgeSeconds(s.built_age_s)} ago`;
    const stats = s.stats ?? {};
    const statLine = ['read', 'reused', 'dropped_missing_root', 'kept_last_good']
      .map((key) => `${key} ${show(stats[key])}`)
      .join(', ');
    parts.push(`<p class="ws-path">${show(s.path)}</p>`);
    parts.push(`<p class="ws-meta">${h(built)} · ${h(statLine)}</p>`);
    parts.push(note('Machine-wide, written at SessionStart.'));
  }
  parts.push('</section>');
  return parts.join('');
}

function renderGapsSection(model: WorkspaceOverview): string {
  const parts: string[] = ['<section class="ws-section"><h3 class="ws-heading">Gaps</h3>'];
  const scan = model.repos.scan;
  if (scan && !scan.complete) {
    parts.push(note(`The filesystem scan stopped at its budget (${scan.stopped_reason ?? 'unknown'}). Repos after the stop show guard state not scanned.`));
  }
  if (model.gaps.length === 0) {
    parts.push(note('No gaps reported.'));
  } else {
    parts.push('<ul class="ws-list ws-gaps">');
    for (const gap of model.gaps) {
      parts.push(`<li><code>${h(gap.code)}</code> ${h(gap.message)}</li>`);
    }
    parts.push('</ul>');
  }
  parts.push('</section>');
  return parts.join('');
}

function contextFor(model: WorkspaceOverview): ClassifyContext {
  return {
    serverGuardMode: model.runtime.guard.mode,
    snapshotState: model.repos.snapshot.error ?? 'ok',
  };
}

/** The whole overview as HTML. Every dynamic value is escaped. */
export function renderWorkspaceOverview(model: WorkspaceOverview): string {
  const grouped = groupRepos(model.repos.items, contextFor(model));
  return renderHeader(model)
    + renderErrors(model)
    + renderCounts(model, grouped.counts)
    + renderReposSection(model, grouped)
    + renderSessionsSection(model)
    + renderProjectsSection(model)
    + renderSnapshotSection(model)
    + renderGapsSection(model);
}

/**
 * Render-failure panel with a Retry button (S3). Used when the response arrived but the page could
 * not build it. It shows no HTTP status: the fetch succeeded, so a status would mislead. The error
 * itself is logged by the caller.
 */
export function renderWorkspaceRenderError(): string {
  return '<div class="ws-banner ws-banner--error" role="alert">'
    + `<p>${h('Could not render this overview. Details are in the browser console.')}</p>`
    + '<button type="button" class="ws-btn" data-ws-action="retry">Retry</button>'
    + '</div>';
}

/** Fetch-failure panel with a Retry button. status null means no response reached the client. */
export function renderWorkspaceFetchError(status: number | null): string {
  const detail = status == null ? 'no response' : `HTTP ${status}`;
  return '<div class="ws-banner ws-banner--error" role="alert">'
    + `<p>${h(`Could not read the workspace overview (${detail}).`)}</p>`
    + '<button type="button" class="ws-btn" data-ws-action="retry">Retry</button>'
    + '</div>';
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Structural check on a response body. Anything that fails it renders as a fetch error. */
export function isWorkspaceOverview(value: unknown): value is WorkspaceOverview {
  if (!isObject(value)) return false;
  const repos = value.repos;
  const sessions = value.sessions;
  const projects = value.projects;
  return isObject(value.runtime)
    && isObject(repos) && Array.isArray(repos.items) && isObject(repos.snapshot)
    && isObject(sessions) && Array.isArray(sessions.items)
    && isObject(projects) && Array.isArray(projects.items)
    && Array.isArray(value.gaps)
    && Array.isArray(value.errors);
}

// ---------------------------------------------------------------------------
// DOM wiring. dashboard.ts calls mountWorkspaceView once at boot and close() from openTab.
// ---------------------------------------------------------------------------

export interface WorkspaceViewOptions {
  hosted: boolean;
  fetchOverview: () => Promise<{ status: number; body: unknown }>;
}

export interface WorkspaceViewControls {
  open(): void;
  close(): void;
  toggle(): void;
  refresh(): Promise<void>;
}

const BUTTON_ID = 'workspace-overview-btn';
const VIEW_ID = 'workspace-overview-view';
const VIEW_CHROME = '<div class="ws-view-bar">'
  + '<h2 class="ws-view-title">Local overview</h2>'
  + '<div class="ws-view-actions">'
  + '<button type="button" class="ws-btn" data-ws-action="refresh">Refresh</button>'
  + '<button type="button" class="ws-btn" data-ws-action="close">Close</button>'
  + '</div></div>'
  + '<div class="ws-overview-body" data-ws-body></div>';

/**
 * Wire the sidebar button and the view. Existing #workspace-overview-btn and
 * #workspace-overview-view elements are reused. When absent, they are created next to
 * #workspace-entry and before #tab-bodies. In hosted mode the button is hidden and the view
 * never opens. The view hides #tab-bodies only, so #topbar stays visible.
 */
export function mountWorkspaceView(doc: Document, opts: WorkspaceViewOptions): WorkspaceViewControls {
  const tabBodies = doc.getElementById('tab-bodies');
  const anchor = doc.getElementById('workspace-entry');

  let button = doc.getElementById(BUTTON_ID) as HTMLButtonElement | null;
  if (!button && anchor) {
    button = doc.createElement('button');
    button.id = BUTTON_ID;
    button.type = 'button';
    button.className = 'ws-sidebar-btn';
    button.title = 'Repos, indexes, sessions and guard state on this machine.';
    button.textContent = 'Local overview';
    anchor.after(button);
  }
  if (button) button.hidden = opts.hosted;

  let view = doc.getElementById(VIEW_ID) as HTMLElement | null;
  if (!view && tabBodies) {
    view = doc.createElement('section');
    view.id = VIEW_ID;
    view.className = 'ws-overview ws-overview-view';
    view.hidden = true;
    view.setAttribute('aria-label', 'Local overview');
    tabBodies.before(view);
  }
  if (view && !view.querySelector('[data-ws-body]')) view.innerHTML = VIEW_CHROME;

  const bodyEl = (): HTMLElement | null => view?.querySelector<HTMLElement>('[data-ws-body]') ?? null;
  const refreshButton = (): HTMLButtonElement | null =>
    view?.querySelector<HTMLButtonElement>('[data-ws-action="refresh"]') ?? null;

  let isOpen = false;
  let savedBodiesDisplay = '';
  let inFlight: Promise<void> | null = null;

  const setButtonState = (on: boolean): void => {
    button?.classList.toggle('active', on);
    button?.setAttribute('aria-pressed', on ? 'true' : 'false');
  };

  // A render exception is not a fetch error: the response was fine, the page could not build it.
  // Log it and show the render panel with Retry, without an HTTP status.
  const renderSafely = (model: WorkspaceOverview): string => {
    try {
      return renderWorkspaceOverview(model);
    } catch (error) {
      console.error('Local overview: render failed', error);
      return renderWorkspaceRenderError();
    }
  };

  const refresh = (): Promise<void> => {
    if (inFlight) return inFlight;
    const target = bodyEl();
    if (!target || opts.hosted) return Promise.resolve();
    const refreshBtn = refreshButton();
    if (refreshBtn) refreshBtn.disabled = true;
    if (!target.hasChildNodes()) {
      target.innerHTML = '<p class="ws-muted">Loading the workspace overview…</p>';
    }
    const pending = (async (): Promise<void> => {
      let html: string;
      try {
        const result = await opts.fetchOverview();
        const body = result.body;
        html = result.status === 200 && isWorkspaceOverview(body)
          ? renderSafely(body)
          : renderWorkspaceFetchError(result.status);
      } catch {
        html = renderWorkspaceFetchError(null);
      }
      target.innerHTML = html;
    })();
    inFlight = pending.finally(() => {
      inFlight = null;
      if (refreshBtn) refreshBtn.disabled = false;
    });
    return inFlight;
  };

  const open = (): void => {
    if (opts.hosted || !view || isOpen) return;
    isOpen = true;
    view.hidden = false;
    if (tabBodies) {
      savedBodiesDisplay = tabBodies.style.display;
      tabBodies.style.display = 'none';
    }
    setButtonState(true);
    void refresh();
  };

  const close = (): void => {
    if (!isOpen) return;
    isOpen = false;
    if (view) view.hidden = true;
    if (tabBodies) tabBodies.style.display = savedBodiesDisplay;
    setButtonState(false);
  };

  const toggle = (): void => {
    if (isOpen) close();
    else open();
  };

  button?.addEventListener('click', () => toggle());
  view?.addEventListener('click', (event: Event) => {
    const origin = event.target as Element | null;
    const action = origin?.closest?.('[data-ws-action]')?.getAttribute('data-ws-action');
    if (action === 'close') close();
    else if (action === 'refresh' || action === 'retry') void refresh();
  });

  return { open, close, toggle, refresh };
}
