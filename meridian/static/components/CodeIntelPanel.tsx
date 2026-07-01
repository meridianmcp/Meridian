// CodeIntelPanel — Preact rewrite of the Code Intel package graph (ff8ff615).
//
// Replaces the old ECharts force-graph ("floating circles") with a hierarchical
// layered DAG: packages are grouped by their architecture `layer` and stacked
// top-to-bottom (highest layer first), so dependency direction reads downward.
// Owns three explicit view states (loading / error / ready) plus an empty-ready
// state, zoom controls, and a self-contained Generate Map lifecycle.
//
// Presentational by design: all data + the async map generator arrive via props
// so each state is deterministically testable (see CodeIntelPanel.test.tsx). The
// host (dashboard.js) fetches architecture over the code MCP tunnel and mounts
// this with mountCodeIntelPanel().
import { render } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";
import type { Architecture, ArchPackage } from "./types";

export interface CodeIntelPanelProps {
  /** Top-level fetch state for the panel. */
  status: "loading" | "error" | "ready";
  /** Error text shown when status === 'error'. */
  error?: string;
  /** Parsed get_architecture payload (packages drive the DAG). */
  architecture?: Architecture | null;
  /**
   * Async map generator: POSTs the graph and resolves to an <img> src (a PNG
   * data URI). Reject with an Error to surface its message. When omitted the
   * Generate Map button is hidden.
   */
  onGenerateMap?: () => Promise<string>;
}

interface LayerRow {
  rank: string;
  label: string;
  packages: ArchPackage[];
}

const PALETTE = ["#7dd3fc", "#a78bfa", "#34d399", "#fbbf24", "#fb7185", "#60a5fa", "#f472b6"];

/** Group packages by `layer`, ordered highest-layer-first; 'other' sinks last. */
export function buildLayers(arch?: Architecture | null): LayerRow[] {
  const packages = arch && Array.isArray(arch.packages)
    ? arch.packages.filter((p): p is ArchPackage => !!p && !!p.name)
    : [];
  if (!packages.length) return [];

  const byRank = new Map<string, ArchPackage[]>();
  for (const p of packages) {
    const key = String(p.layer ?? "other");
    const bucket = byRank.get(key);
    if (bucket) bucket.push(p);
    else byRank.set(key, [p]);
  }

  const ranks = Array.from(byRank.keys()).sort((a, b) => {
    const na = Number(a);
    const nb = Number(b);
    const aNum = a !== "" && !Number.isNaN(na);
    const bNum = b !== "" && !Number.isNaN(nb);
    if (aNum && bNum) return nb - na; // higher layer renders on top
    if (aNum) return -1; // numbered layers before the 'other' bucket
    if (bNum) return 1;
    return a.localeCompare(b);
  });

  return ranks.map((rank) => ({
    rank,
    label: labelForRank(arch, rank),
    packages: byRank.get(rank)!.slice().sort((a, b) => (b.node_count ?? 0) - (a.node_count ?? 0)),
  }));
}

function labelForRank(arch: Architecture | null | undefined, rank: string): string {
  const named = arch && Array.isArray(arch.layers)
    ? arch.layers.find((l) => l && String(l.layer) === rank && l.name)
    : null;
  if (named && named.name) return `${named.name} · layer ${rank}`;
  return rank === "other" ? "unlayered" : `layer ${rank}`;
}

// ---------------------------------------------------------------------------
// 455b7970 — Cytoscape.js compound-node model.
//
// Containment (Layer ⊃ Package ⊃ File ⊃ Class ⊃ Method) is expressed as the
// compound *parent* relationship, so it reads as nesting rather than an edge
// tangle. The four relationship edge types below are colored and independently
// filterable. `buildCytoscapeElements` degrades to Layer→Package from the
// package-level get_architecture payload available today, and emits the deeper
// File/Class/Method levels automatically when a package carries a `children`
// tree — so the renderer scales without a rewrite once that data is surfaced.
// ---------------------------------------------------------------------------

export type CiEdgeType = "contains" | "imports" | "inherits" | "invokes";

export const CI_EDGE_TYPES: CiEdgeType[] = ["contains", "imports", "inherits", "invokes"];

export const CI_EDGE_COLORS: Record<CiEdgeType, string> = {
  contains: "#64748b",
  imports: "#38bdf8",
  inherits: "#a78bfa",
  invokes: "#f59e0b",
};

/** An optional deeper node (file/class/method) hanging off a package. */
export interface CiChild {
  name?: string;
  kind?: "file" | "class" | "method";
  children?: CiChild[];
  imports?: string[];
  inherits?: string[];
}

export interface CyElement {
  group: "nodes" | "edges";
  data: Record<string, any>;
}

function _emitChildren(els: CyElement[], parentId: string, children?: CiChild[]): void {
  if (!Array.isArray(children)) return;
  for (const c of children) {
    if (!c || !c.name) continue;
    const id = `${parentId}/${c.name}`;
    els.push({ group: "nodes", data: { id, parent: parentId, label: c.name, kind: c.kind || "node" } });
    for (const imp of c.imports || []) {
      els.push({ group: "edges", data: { id: `imports:${id}->${imp}`, source: id, target: imp, etype: "imports", color: CI_EDGE_COLORS.imports } });
    }
    for (const base of c.inherits || []) {
      els.push({ group: "edges", data: { id: `inherits:${id}->${base}`, source: id, target: base, etype: "inherits", color: CI_EDGE_COLORS.inherits } });
    }
    _emitChildren(els, id, c.children);
  }
}

/** Build Cytoscape compound elements (nodes + typed edges) from an Architecture. */
export function buildCytoscapeElements(arch?: Architecture | null): CyElement[] {
  const rows = buildLayers(arch);
  const els: CyElement[] = [];
  const pkgIds = new Set<string>();
  for (const layer of rows) {
    const parentId = `layer:${layer.rank}`;
    els.push({ group: "nodes", data: { id: parentId, label: layer.label, kind: "layer" } });
    for (const p of layer.packages) {
      const pid = `pkg:${p.name}`;
      pkgIds.add(String(p.name));
      els.push({
        group: "nodes",
        data: { id: pid, parent: parentId, label: p.name, kind: "package", node_count: p.node_count ?? 0 },
      });
      _emitChildren(els, pid, (p as ArchPackage & { children?: CiChild[] }).children);
    }
  }
  const boundaries = arch && Array.isArray(arch.boundaries) ? arch.boundaries : [];
  for (const b of boundaries) {
    if (!b || !b.from || !b.to) continue;
    if (!pkgIds.has(String(b.from)) || !pkgIds.has(String(b.to))) continue;
    els.push({
      group: "edges",
      data: {
        id: `invokes:${b.from}->${b.to}`,
        source: `pkg:${b.from}`,
        target: `pkg:${b.to}`,
        etype: "invokes",
        weight: b.call_count ?? 1,
        color: CI_EDGE_COLORS.invokes,
      },
    });
  }
  return els;
}

/** Drop edges whose type is not in `enabled` (compound parent nodes are kept). */
export function filterCyElements(elements: CyElement[], enabled: Set<CiEdgeType>): CyElement[] {
  return elements.filter((el) =>
    el.group === "nodes" || enabled.has(el.data.etype as CiEdgeType),
  );
}

/**
 * Mount a Cytoscape compound graph into `container`. Uses the global
 * `window.cytoscape` (loaded via CDN) + the fcose layout when present. Returns
 * the cy instance, or null when Cytoscape isn't available (e.g. jsdom / SSR) so
 * the caller can fall back to the accessible layered DAG. Never throws.
 */
export function mountCytoscapeGraph(
  container: HTMLElement,
  elements: CyElement[],
  opts: { edgeFilter?: Set<CiEdgeType> } = {},
): any | null {
  const w: any = typeof window !== "undefined" ? window : undefined;
  const cy = w && w.cytoscape;
  if (!cy || !container) return null;
  try {
    const fcose = w.cytoscapeFcose;
    if (fcose && !cy.__meridianFcose) {
      cy.use(fcose);
      cy.__meridianFcose = true;
    }
  } catch { /* fcose optional — fall back to built-in cose */ }
  const els = opts.edgeFilter ? filterCyElements(elements, opts.edgeFilter) : elements;
  try {
    return cy({
      container,
      elements: els.map((e) => ({ group: e.group, data: e.data })),
      style: [
        { selector: "node", style: { "background-color": "#334155", label: "data(label)", color: "#cbd5e1", "font-size": 8, "text-valign": "center" } },
        { selector: ":parent", style: { "background-opacity": 0.12, "border-color": "#475569", label: "data(label)", "text-valign": "top" } },
        { selector: "edge", style: { width: 1, "line-color": "data(color)", "target-arrow-color": "data(color)", "target-arrow-shape": "triangle", "curve-style": "bezier" } },
      ],
      layout: { name: w.cytoscapeFcose ? "fcose" : "cose", animate: false },
    });
  } catch {
    return null;
  }
}

export function CodeIntelPanel(props: CodeIntelPanelProps) {
  const { status, error, architecture, onGenerateMap } = props;
  const [zoom, setZoom] = useState(1);
  const [mapStatus, setMapStatus] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [mapUrl, setMapUrl] = useState("");
  const [mapError, setMapError] = useState("");
  // 455b7970 — Cytoscape compound view: per-edge-type filters + a container that
  // upgrades to the interactive graph when window.cytoscape is present, falling
  // back to the accessible layered DAG otherwise (jsdom/SSR, or CDN blocked).
  const [enabled, setEnabled] = useState<Record<CiEdgeType, boolean>>({
    contains: true, imports: true, inherits: true, invokes: true,
  });
  const cyRef = useRef<HTMLDivElement | null>(null);
  const [cyMounted, setCyMounted] = useState(false);
  const cyAvailable = typeof window !== "undefined" && !!(window as any).cytoscape;

  useEffect(() => {
    if (status !== "ready" || !cyRef.current) return;
    const elements = buildCytoscapeElements(architecture);
    const edgeFilter = new Set<CiEdgeType>(CI_EDGE_TYPES.filter((t) => enabled[t]));
    const cy = mountCytoscapeGraph(cyRef.current, elements, { edgeFilter });
    setCyMounted(!!cy);
    return () => { try { if (cy) cy.destroy(); } catch { /* noop */ } };
  }, [status, architecture, enabled]);

  if (status === "loading") {
    return (
      <div class="ci-state ci-loading" role="status" style={STATE_STYLE}>
        Loading code intelligence…
      </div>
    );
  }
  if (status === "error") {
    return (
      <div class="ci-state ci-error" role="alert" style={{ ...STATE_STYLE, color: "var(--error,#ef4444)" }}>
        Failed to load code intelligence: {error || "unknown error"}
      </div>
    );
  }

  const layers = buildLayers(architecture);
  const hasGraph = layers.length > 0;

  async function handleGenerate() {
    if (!onGenerateMap) return;
    setMapStatus("loading");
    setMapError("");
    try {
      const url = await onGenerateMap();
      setMapUrl(url);
      setMapStatus("ready");
    } catch (e) {
      setMapError(e instanceof Error ? e.message : String(e));
      setMapStatus("error");
    }
  }

  return (
    <div class="ci-panel">
      <div
        class="ci-toolbar"
        style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "6px" }}
      >
        <span
          class="ci-title"
          style={{ fontSize: "10px", color: "var(--accent)", textTransform: "uppercase", letterSpacing: ".06em" }}
        >
          Package Graph
        </span>
        <div class="ci-controls" style={{ display: "flex", gap: "4px", alignItems: "center" }}>
          <button
            class="ci-zoom-out"
            aria-label="Zoom out"
            onClick={() => setZoom((z) => Math.max(0.5, +(z - 0.1).toFixed(2)))}
            style={CTRL_STYLE}
          >
            −
          </button>
          <span class="ci-zoom-label" style={{ fontSize: "9px", color: "var(--muted)", minWidth: "30px", textAlign: "center" }}>
            {Math.round(zoom * 100)}%
          </span>
          <button
            class="ci-zoom-in"
            aria-label="Zoom in"
            onClick={() => setZoom((z) => Math.min(2, +(z + 0.1).toFixed(2)))}
            style={CTRL_STYLE}
          >
            +
          </button>
          {onGenerateMap ? (
            <button
              class="ci-genmap"
              disabled={mapStatus === "loading"}
              onClick={handleGenerate}
              title="Render a static PNG map via Graphviz"
              style={CTRL_STYLE}
            >
              {mapStatus === "loading" ? "Generating…" : "Generate Map"}
            </button>
          ) : null}
        </div>
      </div>

      {cyAvailable && hasGraph ? (
        <div class="ci-edge-filters" style={{ display: "flex", gap: "8px", flexWrap: "wrap", margin: "0 0 6px" }}>
          {CI_EDGE_TYPES.map((t) => (
            <label
              key={t}
              class="ci-edge-filter"
              style={{ display: "flex", alignItems: "center", gap: "3px", fontSize: "9px", color: CI_EDGE_COLORS[t], fontFamily: "var(--font-mono)" }}
            >
              <input type="checkbox" checked={enabled[t]} aria-label={t} onChange={() => setEnabled((e) => ({ ...e, [t]: !e[t] }))} />
              {t}
            </label>
          ))}
        </div>
      ) : null}

      {cyAvailable ? (
        <div
          class="ci-cy"
          ref={cyRef}
          style={{
            width: "100%",
            height: hasGraph ? "360px" : "0",
            background: "var(--surface-1)",
            border: hasGraph ? "1px solid var(--border)" : "none",
            borderRadius: "4px",
          }}
        />
      ) : null}

      {hasGraph && (!cyAvailable || !cyMounted) ? (
        <div
          class="ci-dag"
          style={{
            width: "100%",
            background: "var(--surface-1)",
            border: "1px solid var(--border)",
            borderRadius: "4px",
            padding: "10px",
            overflow: "auto",
          }}
        >
          <div style={{ transform: `scale(${zoom})`, transformOrigin: "top left", display: "flex", flexDirection: "column", gap: "10px" }}>
            {layers.map((layer, i) => (
              <div class="ci-layer" key={layer.rank} style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                <div
                  class="ci-layer-label"
                  style={{ width: "120px", flexShrink: 0, fontSize: "9px", color: "var(--muted)", textAlign: "right", fontFamily: "var(--font-mono)" }}
                >
                  {layer.label}
                </div>
                <div class="ci-layer-nodes" style={{ display: "flex", flexWrap: "wrap", gap: "6px", flex: 1, borderLeft: "2px solid var(--border)", paddingLeft: "8px" }}>
                  {layer.packages.map((p) => (
                    <div
                      class="ci-node"
                      key={p.name}
                      title={`${p.name} · ${p.node_count ?? 0} nodes`}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: "5px",
                        background: "var(--surface-2)",
                        border: `1px solid ${PALETTE[i % PALETTE.length]}55`,
                        borderLeft: `3px solid ${PALETTE[i % PALETTE.length]}`,
                        borderRadius: "3px",
                        padding: "3px 7px",
                        fontSize: "10px",
                        color: "var(--text)",
                        fontFamily: "var(--font-mono)",
                      }}
                    >
                      <span class="ci-node-name">{p.name}</span>
                      <span class="ci-node-count" style={{ fontSize: "8px", color: "var(--muted)" }}>
                        {p.node_count ?? 0}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {!hasGraph ? (
        <div class="ci-state ci-empty" style={{ ...STATE_STYLE, color: "var(--muted)" }}>
          No package graph yet — index the repo to populate it.
        </div>
      ) : null}

      {mapStatus === "ready" && mapUrl ? (
        <div class="ci-map" style={{ marginTop: "8px" }}>
          <img src={mapUrl} alt="Codebase package map" style={{ maxWidth: "100%", border: "1px solid var(--border)", borderRadius: "4px", background: "#0b0e14" }} />
        </div>
      ) : null}
      {mapStatus === "error" ? (
        <div class="ci-state ci-map-error" role="alert" style={{ ...STATE_STYLE, color: "var(--error,#ef4444)", marginTop: "8px" }}>
          Map generation failed: {mapError}
        </div>
      ) : null}
    </div>
  );
}

const STATE_STYLE = { fontSize: "11px", padding: "16px", textAlign: "center" as const, color: "var(--muted)" };
const CTRL_STYLE = {
  background: "none",
  border: "1px solid var(--border)",
  borderRadius: "3px",
  color: "var(--muted)",
  fontSize: "9px",
  fontFamily: "var(--font-mono)",
  padding: "2px 8px",
  cursor: "pointer",
};

/** Mount (or re-render) the panel into a host element. Idempotent per container. */
export function mountCodeIntelPanel(container: HTMLElement, props: CodeIntelPanelProps): void {
  render(<CodeIntelPanel {...props} />, container);
}
