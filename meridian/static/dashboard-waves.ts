// dashboard-waves.ts — real-time parallel-execution wave/batch progress (e3355ccb).
//
// Meridian's server already clusters pending sprint items into conflict-free
// batches (db.get_parallelizable_groups): a greedy first-fit graph coloring on
// each item's touches_resources, where every batch is a set of items an
// orchestrator can fan out simultaneously and successive batches run in
// sequence. Those batches ARE the "phases" of a Dynamic-Workflow-style run.
//
// This module recomputes the SAME batches on the client from the sprint-items
// payload the Live tab already fetches (/projects/{id}/sprint-items returns full
// rows incl. status/claimed_at/depends_on/touches_resources/priority), so no new
// backend route is needed, and renders a live wave-progress panel: per batch,
// the running / done / pending counts + elapsed since the earliest claim.
//
// Data gap (honest): there is NO token/cost/duration telemetry stored on
// sprint_items or task_log anywhere in the schema. So per-wave *token* cost is
// not available and is NOT invented — we surface elapsed wall-clock (from
// claimed_at, which does exist) and item/status counts, and the panel notes the
// token gap in-place rather than showing fabricated numbers.
//
// The computation is a pure, exported helper (computeWaveProgress) so it unit
// tests without a DOM. Rendering (renderWaveProgress) is a thin, best-effort DOM
// layer wired into the existing Live-tab refresh loop.

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface WaveItem {
  id: string;
  title?: string;
  status?: string;
  claimed_at?: string | null;
  depends_on?: string | null;
  touches_resources?: unknown;
  failure_mode?: string | null;
  blocker_kind?: string | null;
  milestone_type?: string | null;
  priority?: string | null;
  added_at?: string | null;
  actor?: string | null;
}

export interface WaveBatch {
  index: number;            // 0-based batch/phase number
  items: WaveItem[];        // items colored into this batch
  running: number;          // items in this batch currently claimed / in_progress
  pending: number;          // eligible-but-unclaimed items in this batch
  total: number;            // items.length
  parallel: boolean;        // true when the batch can fan out >1 item at once
  earliestClaim: string | null; // oldest claimed_at among running items (for elapsed)
}

export interface WaveProgress {
  batches: WaveBatch[];
  groupCount: number;       // number of batches (= phases)
  eligibleCount: number;    // pending/todo, unclaimed, dependency-satisfied
  runningCount: number;     // items currently in flight (in_progress OR claimed pending)
  blockedCount: number;     // waiting on a non-terminal parent
  undeclaredCount: number;  // eligible items with no touches_resources
  doneCount: number;        // items already done (context, not a batch)
  maxFanOut: number;        // largest batch size (peak simultaneous parallelism)
  activeBatchIndex: number; // first batch holding a running item, else first non-empty, else -1
  strategy: "parallel" | "sequential";
  hasTokenTelemetry: false; // documented gap — never true (no such column exists)
}

// ---------------------------------------------------------------------------
// Resource parsing + conflict rules — faithful port of the backend
// (db.parse_touches_resources + db._two_resources_conflict, 63b030a6).
//
// touches_resources is STORED already-normalized (serialize_touches_resources
// canonicalizes at write time), so we only decode — no re-normalization needed.
// ---------------------------------------------------------------------------

const _TERMINAL = new Set(["done", "skipped", "failed", "pushed"]);
const _CLAIMABLE = new Set(["pending", "todo"]);
const _PRIORITY_RANK: Record<string, number> = { urgent: 0, high: 1, normal: 2, low: 3 };
const _PRIORITY_DEFAULT = _PRIORITY_RANK.normal;

/** Decode a touches_resources field (JSON list, array, or CSV) into resource
 * ids. Mirrors db.parse_touches_resources, minus re-normalization (values are
 * stored canonical). Bad values are skipped; duplicates collapse first-seen. */
export function parseTouchesResources(raw: unknown): string[] {
  if (raw == null) return [];
  let values: unknown[];
  if (Array.isArray(raw)) {
    values = raw;
  } else {
    const text = String(raw).trim();
    if (!text) return [];
    try {
      const decoded = JSON.parse(text);
      values = Array.isArray(decoded) ? decoded : [decoded];
    } catch {
      values = text.split(",");
    }
  }
  const out: string[] = [];
  const seen = new Set<string>();
  for (const v of values) {
    const candidate = String(v ?? "").trim();
    if (!candidate) continue;
    // Preserve the underlying id but strip a leading `inferred:` provenance
    // marker so it compares/dedupes on the canonical id (matches storage).
    const norm = candidate.toLowerCase().startsWith("inferred:")
      ? candidate.slice("inferred:".length).trim()
      : candidate;
    if (!norm) continue;
    if (!seen.has(norm)) {
      seen.add(norm);
      out.push(norm);
    }
  }
  return out;
}

/** File path a file:/symbol: resource id refers to (else null). */
function _resourceFileOf(rid: string): string | null {
  if (rid.startsWith("file:")) return rid.slice("file:".length);
  if (rid.startsWith("symbol:")) return rid.slice("symbol:".length).split("::")[0];
  return null;
}

/** True if two resource ids conflict under the file⊃symbol hierarchy
 * (port of db._two_resources_conflict / 63b030a6):
 *   - identical ids conflict;
 *   - a whole-file lock conflicts with ANY symbol on that file;
 *   - two DISTINCT symbols on the same file do NOT conflict;
 *   - everything else conflicts only on exact equality. */
function _twoResourcesConflict(r1: string, r2: string): boolean {
  if (r1 === r2) return true;
  const f1 = _resourceFileOf(r1);
  const f2 = _resourceFileOf(r2);
  if (f1 !== null && f1 === f2) {
    const bothSymbols = r1.startsWith("symbol:") && r2.startsWith("symbol:");
    return !bothSymbols;
  }
  return false;
}

/** True if any resource in `a` conflicts with any in `b`. */
function _resourceSetsConflict(a: string[], b: Iterable<string>): boolean {
  for (const ra of a) {
    for (const rb of b) {
      if (_twoResourcesConflict(ra, rb)) return true;
    }
  }
  return false;
}

// ---------------------------------------------------------------------------
// Core: compute conflict-free batches + live progress from a sprint-items list.
// Mirrors db.get_parallelizable_groups so the client view matches the server's
// orchestration plan.
// ---------------------------------------------------------------------------

export function computeWaveProgress(sprintItems: WaveItem[] | null | undefined): WaveProgress {
  const items = Array.isArray(sprintItems) ? sprintItems : [];
  const statusOf = (it: WaveItem) => String(it.status || "pending");
  const byId: Record<string, WaveItem> = {};
  for (const it of items) if (it && it.id) byId[String(it.id)] = it;

  const doneCount = items.filter((it) => statusOf(it) === "done").length;

  // Walk depends_on to the first non-terminal blocking parent (mirrors the
  // server's get_blocking_dependency_for_sprint_item, honoring failure_mode).
  const blockingParent = (it: WaveItem): WaveItem | null => {
    const seen = new Set<string>();
    let dep = it.depends_on ? String(it.depends_on) : "";
    while (dep && !seen.has(dep)) {
      seen.add(dep);
      const parent = byId[dep];
      if (!parent) return null; // unknown parent → treat as satisfied (server fetches it; absent ⇒ not blocking here)
      const ps = statusOf(parent);
      if (!_TERMINAL.has(ps)) return parent; // parent still open → blocks
      if (ps === "failed" && (it.failure_mode || "continue") !== "continue") return parent;
      // terminal + non-blocking → keep walking the chain upward
      dep = parent.depends_on ? String(parent.depends_on) : "";
    }
    return null;
  };

  const eligible: WaveItem[] = [];
  let runningCount = 0;
  let blockedCount = 0;

  for (const it of items) {
    if (!it || !it.id) continue;
    // Manual-blocker items are not executor-claimable — excluded (2282a636).
    if (it.blocker_kind === "manual") continue;
    const st = statusOf(it);
    const isRunning = st === "in_progress" || (_CLAIMABLE.has(st) && !!it.claimed_at);
    if (isRunning) runningCount++;
    if (!_CLAIMABLE.has(st)) continue;
    if (it.claimed_at) continue; // already in flight — not offered to a new batch
    const parent = blockingParent(it);
    if (parent !== null) {
      const ps = statusOf(parent);
      if (ps === "failed" && (it.failure_mode || "continue") === "continue") {
        // parent failed but this item continues — still eligible
      } else {
        blockedCount++;
        continue;
      }
    }
    eligible.push(it);
  }

  // Stable order: highest-priority first, then oldest, then id (matches server).
  const sorted = eligible.slice().sort((a, b) => {
    const pa = _PRIORITY_RANK[String(a.priority || "normal")] ?? _PRIORITY_DEFAULT;
    const pb = _PRIORITY_RANK[String(b.priority || "normal")] ?? _PRIORITY_DEFAULT;
    if (pa !== pb) return pa - pb;
    const aa = String(a.added_at || "");
    const ab = String(b.added_at || "");
    if (aa !== ab) return aa < ab ? -1 : 1;
    return String(a.id) < String(b.id) ? -1 : 1;
  });

  // Separate declared from undeclared, then greedy first-fit color the declared
  // items; each undeclared item gets its own singleton batch (de730a25).
  const withRes = sorted.map((it) => ({ it, res: parseTouchesResources(it.touches_resources) }));
  const declared = withRes.filter((x) => x.res.length > 0);
  const undeclared = withRes.filter((x) => x.res.length === 0);

  const groups: WaveItem[][] = [];
  const groupResources: Set<string>[] = [];
  for (const { it, res } of declared) {
    let placed = false;
    for (let gi = 0; gi < groupResources.length; gi++) {
      if (!_resourceSetsConflict(res, groupResources[gi])) {
        groups[gi].push(it);
        for (const r of res) groupResources[gi].add(r);
        placed = true;
        break;
      }
    }
    if (!placed) {
      groups.push([it]);
      groupResources.push(new Set(res));
    }
  }
  for (const { it } of undeclared) groups.push([it]);

  // Per-batch live progress. "running" for a batch = its member items that are
  // currently claimed (a batch member is eligible/unclaimed by construction, so
  // this is 0 until a session grabs one — but we look it up live by id so a
  // just-claimed item is reflected on the next refresh).
  const batches: WaveBatch[] = groups.map((groupItems, index) => {
    let running = 0;
    let earliest: string | null = null;
    for (const it of groupItems) {
      const live = byId[String(it.id)] || it;
      const st = statusOf(live);
      const isRunning = st === "in_progress" || (_CLAIMABLE.has(st) && !!live.claimed_at);
      if (isRunning) {
        running++;
        const c = live.claimed_at ? String(live.claimed_at) : null;
        if (c && (earliest === null || c < earliest)) earliest = c;
      }
    }
    return {
      index,
      items: groupItems,
      running,
      pending: groupItems.length - running,
      total: groupItems.length,
      parallel: groupItems.length > 1,
      earliestClaim: earliest,
    };
  });

  const maxFanOut = batches.reduce((m, b) => Math.max(m, b.total), 0);
  // Active batch: the first with a running member; else the first non-empty
  // batch (the next phase to fan out); else -1 when nothing is eligible.
  let activeBatchIndex = batches.findIndex((b) => b.running > 0);
  if (activeBatchIndex < 0) activeBatchIndex = batches.findIndex((b) => b.total > 0);

  return {
    batches,
    groupCount: groups.length,
    eligibleCount: eligible.length,
    runningCount,
    blockedCount,
    undeclaredCount: undeclared.length,
    doneCount,
    maxFanOut,
    activeBatchIndex,
    strategy: maxFanOut > 1 ? "parallel" : "sequential",
    hasTokenTelemetry: false,
  };
}

// ---------------------------------------------------------------------------
// Elapsed-since-claim formatting (shared shape with dashboard's other timers).
// ---------------------------------------------------------------------------

/** Minutes → compact "Nm" / "Hh Mm". Returns '' for missing/invalid input.
 * `nowMs` is injectable for deterministic tests. */
export function formatElapsedSince(claimedAt: string | null | undefined, nowMs?: number): string {
  if (!claimedAt) return "";
  const t = Date.parse(String(claimedAt).replace(" ", "T"));
  if (isNaN(t)) return "";
  const now = nowMs == null ? Date.now() : nowMs;
  const mins = Math.max(0, Math.floor((now - t) / 60000));
  if (mins < 60) return `${mins}m`;
  return `${Math.floor(mins / 60)}h ${mins % 60}m`;
}

// ---------------------------------------------------------------------------
// Rendering — thin best-effort DOM layer wired into the Live-tab refresh loop.
// ---------------------------------------------------------------------------

function _esc(s: unknown): string {
  try {
    return typeof escapeHtml === "function" ? escapeHtml(String(s ?? "")) : String(s ?? "");
  } catch {
    return String(s ?? "");
  }
}

/** Build the wave-progress panel HTML from a computed WaveProgress. Pure string
 * builder (exported for the UI test); no DOM side effects. */
export function buildWaveProgressHtml(wp: WaveProgress, nowMs?: number): string {
  if (!wp.batches.length) {
    // Nothing eligible to batch. Distinguish "all done" from "all blocked".
    if (wp.blockedCount > 0) {
      return `<div class="live-empty">No parallel batch ready — ${wp.blockedCount} item(s) blocked on dependencies.</div>`;
    }
    return `<div class="live-empty">No parallelizable work right now.</div>`;
  }

  const stratColor = wp.strategy === "parallel" ? "var(--accent-green)" : "var(--muted)";
  const header =
    `<div class="wave-progress-summary" style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:8px;font-size:11px">` +
    `<span style="font-weight:600;color:${stratColor}">${_esc(wp.strategy.toUpperCase())}</span>` +
    `<span style="color:var(--muted)">${wp.groupCount} wave${wp.groupCount !== 1 ? "s" : ""}</span>` +
    `<span style="color:var(--muted)">· ${wp.eligibleCount} eligible</span>` +
    (wp.runningCount ? `<span style="color:var(--accent-green)">· ${wp.runningCount} running</span>` : "") +
    (wp.blockedCount ? `<span style="color:#fbbf24">· ${wp.blockedCount} blocked</span>` : "") +
    (wp.maxFanOut > 1 ? `<span style="color:var(--accent)">· up to ${wp.maxFanOut}× parallel</span>` : "") +
    `</div>`;

  const rows = wp.batches
    .map((b) => {
      const isActive = b.index === wp.activeBatchIndex;
      const accent = b.running > 0 ? "var(--accent-green)" : isActive ? "var(--accent)" : "var(--border)";
      const elapsed = formatElapsedSince(b.earliestClaim, nowMs);
      const parallelTag = b.parallel
        ? `<span title="These items touch no shared resources — safe to run at once" style="font-size:9px;color:var(--accent);border:1px solid var(--accent)55;border-radius:3px;padding:0 4px">∥ ${b.total}×</span>`
        : `<span title="Runs on its own — no proven parallel-safe peer" style="font-size:9px;color:var(--muted)">seq</span>`;
      const counts =
        (b.running ? `<span style="color:var(--accent-green)">${b.running} running</span> · ` : "") +
        `<span style="color:var(--muted)">${b.pending} pending</span>`;
      const items = b.items
        .map((it) => {
          const live = String(it.status || "pending");
          const dot = live === "in_progress" || it.claimed_at ? "var(--accent-green)" : "var(--muted)";
          return `<div style="display:flex;gap:6px;align-items:center;margin-top:3px">
            <span style="width:5px;height:5px;border-radius:50%;background:${dot};flex:none"></span>
            <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px" title="${_esc(it.title || it.id)}">${_esc(it.title || it.id)}</span>
          </div>`;
        })
        .join("");
      return `<div class="wave-batch" data-batch="${b.index}" style="border-left:3px solid ${accent};padding:4px 0 6px 8px;margin-bottom:8px">
        <div style="display:flex;gap:6px;align-items:center;justify-content:space-between">
          <span style="font-size:11px;font-weight:600;color:var(--text)">Wave ${b.index + 1} ${parallelTag}${isActive ? ' <span style="font-size:9px;color:var(--accent)">● active</span>' : ""}</span>
          <span style="font-size:9px;color:var(--muted);flex:none">${counts}${elapsed ? ` · ${_esc(elapsed)}` : ""}</span>
        </div>
        ${items}
      </div>`;
    })
    .join("");

  // Honest data-gap note: no token/time-cost telemetry exists server-side.
  const gapNote =
    `<div class="wave-progress-note" style="margin-top:4px;font-size:9px;color:var(--muted);line-height:1.4">` +
    `Cost shown is wall-clock elapsed since an item was claimed. Per-wave token/\$ cost isn't tracked server-side yet, so it's omitted rather than estimated.` +
    `</div>`;

  return header + rows + gapNote;
}

/** Repaint the wave-progress panel for a project from its sprint items.
 * Best-effort: a missing mount point or any failure leaves the DOM untouched. */
export function renderWaveProgress(projectId: string, sprintItems: WaveItem[]): void {
  try {
    if (typeof document === "undefined") return;
    const root = document.getElementById(`live-wave-progress-${projectId}`);
    if (!root) return;
    const wp = computeWaveProgress(sprintItems);
    root.innerHTML = buildWaveProgressHtml(wp);
  } catch {
    /* best-effort — never break the Live tab */
  }
}

// Re-expose on window so the bundled Live-tab refresh loop (dashboard.ts) can
// call renderWaveProgress bare, matching the codebase's global-function style.
try {
  Object.assign(window, {
    computeWaveProgress,
    buildWaveProgressHtml,
    renderWaveProgress,
    formatElapsedSince,
    parseTouchesResources,
  });
} catch { /* non-browser (test) context */ }
