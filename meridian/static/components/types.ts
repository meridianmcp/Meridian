// Shared TypeScript types for Preact dashboard components (0a88d328).
//
// These mirror the codebase-memory-mcp `get_architecture` tool's JSON schema as
// rendered today by dashboard.js `_codeArchSection` — the same shape the Code
// Intel panel (ff8ff615) consumes to build its layered DAG. Kept intentionally
// permissive (every field optional) because the renderer is defensive: a schema
// mismatch must degrade to a raw view, never throw.

export interface NodeLabelCount {
  label?: string;
  count?: number;
}

export interface EdgeTypeCount {
  type?: string;
  count?: number;
}

export interface Hotspot {
  name?: string;
  fan_in?: number;
}

/** A code package/module. `layer` orders it vertically in the layered DAG. */
export interface ArchPackage {
  name?: string;
  node_count?: number;
  layer?: number | string;
}

/** A named architectural layer with its vertical rank (`layer`). */
export interface ArchLayer {
  name?: string;
  layer?: number | string;
}

/** A directed call/dependency between two packages, weighted by call_count. */
export interface ArchBoundary {
  from?: string;
  to?: string;
  call_count?: number;
}

/** Full `get_architecture` response. All fields optional by design. */
export interface Architecture {
  node_labels?: NodeLabelCount[];
  edge_types?: EdgeTypeCount[];
  hotspots?: Hotspot[];
  packages?: ArchPackage[];
  layers?: ArchLayer[];
  boundaries?: ArchBoundary[];
}

/** Async fetch lifecycle states shared by data-driven panels. */
export type LoadState = "idle" | "loading" | "ready" | "error";
