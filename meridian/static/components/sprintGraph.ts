// Sprint dependency DAG + historical gantt model (233bae67).
//
// Pure, framework-free builders so the graph/gantt logic is unit-testable
// without a browser. The dashboard mounts the current-sprint DAG with
// mountSprintDag() (a guarded window.cytoscape wrapper that degrades to no-op
// when the CDN global is absent) and renders the historical gantt from
// buildGanttModel(). depends_on already ships (_migrate_sprint_item_dependencies)
// so no schema change is needed.

export interface SprintItemLike {
  id: string;
  title?: string;
  status?: string | null;
  depends_on?: string | null;
  touches_resources?: string | string[] | null;
  added_at?: string | null;
  claimed_at?: string | null;
  completed_at?: string | null;
}

export interface CyEl {
  group: "nodes" | "edges";
  data: Record<string, any>;
}

/** Status → node color for the DAG (matches the board's status semantics). */
export const SPRINT_STATUS_COLORS: Record<string, string> = {
  pending: "#64748b",
  todo: "#64748b",
  in_progress: "#38bdf8",
  provisional_complete: "#a78bfa",
  done: "#22c55e",
  failed: "#ef4444",
  skipped: "#475569",
  pushed: "#14b8a6",
  indeterminate: "#f59e0b",
};

export function sprintStatusColor(status?: string | null): string {
  return SPRINT_STATUS_COLORS[String(status || "pending")] || "#64748b";
}

/** Normalize touches_resources (JSON string / CSV / array) to a string list. */
export function parseResources(tr: SprintItemLike["touches_resources"]): string[] {
  if (!tr) return [];
  if (Array.isArray(tr)) return tr.map(String);
  const s = String(tr).trim();
  if (!s) return [];
  if (s.startsWith("[")) {
    try {
      const arr = JSON.parse(s);
      return Array.isArray(arr) ? arr.map(String) : [];
    } catch { /* fall through to CSV */ }
  }
  return s.split(",").map((x) => x.trim()).filter(Boolean);
}

/** Longest depends_on chain (the critical path), returned as item ids in order. */
export function criticalPath(items: SprintItemLike[]): string[] {
  const byId = new Map(items.map((i) => [i.id, i]));
  const memo = new Map<string, string[]>();
  function chainTo(id: string, seen: Set<string>): string[] {
    const cached = memo.get(id);
    if (cached) return cached;
    if (seen.has(id)) return [id]; // cycle guard
    seen.add(id);
    const it = byId.get(id);
    const dep = it && it.depends_on && byId.has(it.depends_on) ? it.depends_on : null;
    const path = dep ? [...chainTo(dep, seen), id] : [id];
    seen.delete(id);
    memo.set(id, path);
    return path;
  }
  let best: string[] = [];
  for (const it of items) {
    const p = chainTo(it.id, new Set());
    if (p.length > best.length) best = p;
  }
  return best;
}

/**
 * Build Cytoscape elements for the current sprint: one node per item (status
 * colored), directed depends_on edges, dashed undirected file-conflict edges
 * (items sharing a touches_resources id), and a critical-path flag on the
 * longest dependency chain.
 */
export function buildSprintDagElements(items: SprintItemLike[]): CyEl[] {
  const els: CyEl[] = [];
  const ids = new Set(items.map((i) => i.id));
  const critical = new Set(criticalPath(items));
  for (const it of items) {
    els.push({
      group: "nodes",
      data: {
        id: `item:${it.id}`,
        label: (it.title || it.id).slice(0, 40),
        status: it.status || "pending",
        color: sprintStatusColor(it.status),
        critical: critical.has(it.id) ? 1 : 0,
      },
    });
  }
  // depends_on edges: dependency → dependent.
  for (const it of items) {
    if (it.depends_on && ids.has(it.depends_on)) {
      els.push({
        group: "edges",
        data: {
          id: `dep:${it.depends_on}->${it.id}`,
          source: `item:${it.depends_on}`,
          target: `item:${it.id}`,
          etype: "depends",
          color: "#94a3b8",
          critical: critical.has(it.id) && critical.has(it.depends_on) ? 1 : 0,
        },
      });
    }
  }
  // file-conflict edges: unordered pairs of items that share a resource.
  const byResource = new Map<string, string[]>();
  for (const it of items) {
    for (const r of parseResources(it.touches_resources)) {
      const arr = byResource.get(r) || [];
      arr.push(it.id);
      byResource.set(r, arr);
    }
  }
  const seenPair = new Set<string>();
  for (const [resource, memberIds] of byResource) {
    if (memberIds.length < 2) continue;
    for (let i = 0; i < memberIds.length; i++) {
      for (let j = i + 1; j < memberIds.length; j++) {
        const pair = [memberIds[i], memberIds[j]].sort();
        const key = pair.join("|");
        if (seenPair.has(key)) continue;
        seenPair.add(key);
        els.push({
          group: "edges",
          data: {
            id: `conflict:${key}`,
            source: `item:${pair[0]}`,
            target: `item:${pair[1]}`,
            etype: "conflict",
            color: "#f59e0b",
            resource,
          },
        });
      }
    }
  }
  return els;
}

export interface GanttRow {
  id: string;
  title: string;
  status: string;
  startMs: number;
  endMs: number;
}

export interface GanttModel {
  rows: GanttRow[];
  minMs: number;
  maxMs: number;
}

function toMs(ts?: string | null): number | null {
  if (!ts) return null;
  const t = Date.parse(String(ts).replace(" ", "T"));
  return Number.isNaN(t) ? null : t;
}

/**
 * Build a gantt model of historical sprint execution: each item with a start
 * (claimed_at, else added_at) becomes a bar from start → end (completed_at, else
 * start for still-open items), sorted by start. minMs/maxMs give the scale.
 */
export function buildGanttModel(items: SprintItemLike[]): GanttModel {
  const rows: GanttRow[] = [];
  for (const it of items) {
    const start = toMs(it.claimed_at) ?? toMs(it.added_at);
    if (start == null) continue;
    const end = toMs(it.completed_at);
    rows.push({
      id: it.id,
      title: (it.title || it.id).slice(0, 60),
      status: String(it.status || "pending"),
      startMs: start,
      endMs: end == null ? start : Math.max(end, start),
    });
  }
  rows.sort((a, b) => a.startMs - b.startMs || a.id.localeCompare(b.id));
  const minMs = rows.length ? Math.min(...rows.map((r) => r.startMs)) : 0;
  const maxMs = rows.length ? Math.max(...rows.map((r) => r.endMs)) : 0;
  return { rows, minMs, maxMs };
}

export interface GanttBar {
  id: string;
  title: string;
  status: string;
  leftPct: number;
  widthPct: number;
}

/**
 * Reduce a gantt model to positioned bars (percent of the total time span) so a
 * CSS/SSR gantt can render without a charting lib. Zero-duration (still-open)
 * items get a small minimum width so they remain visible. Pure + testable.
 */
export function ganttBars(items: SprintItemLike[], minWidthPct = 1.5): GanttBar[] {
  const { rows, minMs, maxMs } = buildGanttModel(items);
  const span = Math.max(1, maxMs - minMs);
  return rows.map((r) => {
    // Clamp left so an item that starts at the very end of the span (e.g. a
    // still-unclaimed item at maxMs) still keeps a visible minimum-width bar.
    const left = Math.min(((r.startMs - minMs) / span) * 100, 100 - minWidthPct);
    const rawWidth = ((r.endMs - r.startMs) / span) * 100;
    const width = Math.min(100 - left, Math.max(minWidthPct, rawWidth));
    return {
      id: r.id,
      title: r.title,
      status: r.status,
      leftPct: +left.toFixed(2),
      widthPct: +width.toFixed(2),
    };
  });
}

/**
 * Mount the sprint DAG into `container` via the global window.cytoscape (+ fcose
 * when present). Returns the cy instance, or null when Cytoscape isn't available
 * (jsdom/SSR/CDN blocked) so the caller can fall back to the list view. Never
 * throws.
 */
export function mountSprintDag(container: HTMLElement, elements: CyEl[]): any | null {
  const w: any = typeof window !== "undefined" ? window : undefined;
  const cy = w && w.cytoscape;
  if (!cy || !container) return null;
  try {
    const fcose = w.cytoscapeFcose;
    if (fcose && !cy.__meridianFcose) {
      cy.use(fcose);
      cy.__meridianFcose = true;
    }
  } catch { /* fcose optional */ }
  try {
    return cy({
      container,
      elements: elements.map((e) => ({ group: e.group, data: e.data })),
      style: [
        { selector: "node", style: { "background-color": "data(color)", label: "data(label)", color: "#e2e8f0", "font-size": 7, "text-wrap": "wrap", "text-max-width": 80, "text-valign": "center" } },
        { selector: "node[critical = 1]", style: { "border-width": 2, "border-color": "#fde047" } },
        { selector: "edge", style: { width: 1, "line-color": "data(color)", "target-arrow-color": "data(color)", "target-arrow-shape": "triangle", "curve-style": "bezier" } },
        { selector: "edge[etype = 'conflict']", style: { "line-style": "dashed", "target-arrow-shape": "none" } },
      ],
      layout: { name: w.cytoscapeFcose ? "fcose" : "breadthfirst", directed: true, animate: false },
    });
  } catch {
    return null;
  }
}
