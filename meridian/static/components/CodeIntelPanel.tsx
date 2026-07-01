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
import { useState } from "preact/hooks";
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

export function CodeIntelPanel(props: CodeIntelPanelProps) {
  const { status, error, architecture, onGenerateMap } = props;
  const [zoom, setZoom] = useState(1);
  const [mapStatus, setMapStatus] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [mapUrl, setMapUrl] = useState("");
  const [mapError, setMapError] = useState("");

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

      {hasGraph ? (
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
      ) : (
        <div class="ci-state ci-empty" style={{ ...STATE_STYLE, color: "var(--muted)" }}>
          No package graph yet — index the repo to populate it.
        </div>
      )}

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
