// codegraph/model.ts — ed5512b6
//
// PURE data contract + a deterministic shaping function for a standalone
// tree/graph visualizer built on codebase-memory-mcp's *static* graph output.
//
// This module is DECOUPLED by design: it has NO DOM access, NO Meridian
// globals, NO network, and NO LLM. Its only input is the graph payload
// (get_architecture packages/layers + optional graph entity nodes); its only
// output is a hierarchical, framework-free node model. That makes the whole
// codegraph/ directory liftable to a standalone repo as a copy — see README.md.
//
// ---------------------------------------------------------------------------
// The REAL upstream shapes this consumes (codebase-memory-mcp):
//
//   get_architecture(project) -> {
//     packages:   [{ name, node_count, layer }],
//     layers:     [{ name, layer }],
//     boundaries: [{ from, to, call_count }],
//     node_labels:[{ label, count }], edge_types:[{ type, count }],
//     hotspots:   [{ name, fan_in }], clusters?: [...],
//   }
//     — this is PACKAGE/LAYER-level only; it has no per-file/function detail.
//
//   search_graph(project, ...) -> { results: [ GraphNode, ... ] } where a
//   GraphNode carries the deterministic static metadata:
//     { qualified_name, name?, file, label|kind (role: Function/Method/Route/
//       Class/Interface/Variable/File/Folder/Module), signature?, docstring?,
//       complexity?, callers?/callees? (or fan_in/fan_out/degree) }
//
// The shaping function accepts EITHER (or both) and derives the folder->file->
// function hierarchy: folders from the file path segments, files from `file`,
// functions/classes from the graph nodes. No fields are invented — every value
// on an output node comes straight from the payload (or is a documented default
// like role="unknown").
// ---------------------------------------------------------------------------

/** A role/kind label as it appears on an upstream graph node (case-insensitive
 * on input; normalized to a canonical lowercase token on output). */
export type Role =
  | "folder"
  | "file"
  | "module"
  | "package"
  | "class"
  | "interface"
  | "function"
  | "method"
  | "route"
  | "variable"
  | "unknown";

/** The static, deterministic metadata carried by a leaf (function/class) node.
 * Every field is optional — a partial payload must degrade, never throw. */
export interface NodeMetadata {
  /** Fully-qualified name from the graph (e.g. "auth.login"). */
  qualifiedName?: string;
  /** Source file this node lives in. */
  file?: string;
  /** Signature line, verbatim from the graph. */
  signature?: string;
  /** Docstring / leading comment, verbatim from the graph. */
  docstring?: string;
  /** Cyclomatic (or other) complexity metric, verbatim. */
  complexity?: number;
  /** Inbound callers (qualified names). May be a count-only via fanIn. */
  callers?: string[];
  /** Outbound callees (qualified names). May be a count-only via fanOut. */
  callees?: string[];
  /** Fan-in degree when the full caller list isn't surfaced. */
  fanIn?: number;
  /** Fan-out degree when the full callee list isn't surfaced. */
  fanOut?: number;
}

/** A node in the hierarchical model: folder -> file -> function/class. */
export interface CodeGraphNode {
  /** Stable, deterministic id (path-derived; same input -> same id). */
  id: string;
  /** Display label (last path segment / short symbol name). */
  label: string;
  /** Canonical role, drives coloring. */
  role: Role;
  /** Tree depth: 0 = top-level folder/package. */
  depth: number;
  /** Child nodes, deterministically ordered (folders, then files, then symbols;
   * each group sorted by label). */
  children: CodeGraphNode[];
  /** Static metadata (populated on file + symbol nodes). */
  meta: NodeMetadata;
}

/** The root of the shaped model. `roots` are the top-level folders/packages. */
export interface CodeGraphModel {
  roots: CodeGraphNode[];
  /** Total number of leaf (symbol) nodes shaped. */
  symbolCount: number;
  /** Total number of file nodes shaped. */
  fileCount: number;
}

/** One upstream graph node as delivered by search_graph. Kept permissive: every
 * field optional so a schema drift degrades rather than throws. */
export interface GraphNodeInput {
  qualified_name?: string;
  qualifiedName?: string;
  name?: string;
  file?: string;
  path?: string;
  label?: string;
  kind?: string;
  role?: string;
  signature?: string;
  docstring?: string;
  doc?: string;
  complexity?: number | string;
  callers?: unknown;
  callees?: unknown;
  fan_in?: number | string;
  fan_out?: number | string;
  fanIn?: number | string;
  fanOut?: number | string;
  degree?: number | string;
}

/** An architecture package (get_architecture.packages[]). */
export interface ArchPackageInput {
  name?: string;
  node_count?: number;
  layer?: number | string;
}

/** The subset of the get_architecture payload the shaper reads. */
export interface ArchitectureInput {
  packages?: ArchPackageInput[];
  layers?: Array<{ name?: string; layer?: number | string }>;
}

/**
 * The full input to the shaper. Both fields optional — pass architecture alone
 * (package-level view), graph nodes alone (full folder->file->function view),
 * or both (packages seed empty top-level folders, nodes fill them in).
 */
export interface CodeGraphPayload {
  architecture?: ArchitectureInput | null;
  nodes?: GraphNodeInput[] | null;
}

// ---------------------------------------------------------------------------
// Role normalization — total + deterministic.
// ---------------------------------------------------------------------------

const ROLE_ALIASES: Record<string, Role> = {
  folder: "folder",
  dir: "folder",
  directory: "folder",
  file: "file",
  module: "module",
  package: "package",
  pkg: "package",
  class: "class",
  interface: "interface",
  trait: "interface",
  protocol: "interface",
  function: "function",
  func: "function",
  fn: "function",
  method: "method",
  route: "route",
  endpoint: "route",
  variable: "variable",
  var: "variable",
  const: "variable",
  constant: "variable",
};

/** Map an arbitrary upstream role/kind/label string to a canonical Role.
 * Total: any unrecognized (or empty) input -> "unknown". Pure. */
export function normalizeRole(raw: unknown): Role {
  if (typeof raw !== "string") return "unknown";
  const key = raw.trim().toLowerCase();
  return ROLE_ALIASES[key] ?? "unknown";
}

// ---------------------------------------------------------------------------
// Path helpers — deterministic, POSIX + Windows separators.
// ---------------------------------------------------------------------------

/** Split a file path into clean segments, tolerating "/" and "\" and stray
 * separators. Returns [] for empty/invalid input. */
export function splitPath(p: unknown): string[] {
  if (typeof p !== "string") return [];
  return p
    .replace(/\\/g, "/")
    .split("/")
    .map((s) => s.trim())
    .filter((s) => s.length > 0 && s !== ".");
}

function toNumber(v: unknown): number | undefined {
  if (typeof v === "number") return Number.isFinite(v) ? v : undefined;
  if (typeof v === "string" && v.trim() !== "") {
    const n = Number(v);
    return Number.isFinite(n) ? n : undefined;
  }
  return undefined;
}

function toStringList(v: unknown): string[] | undefined {
  if (!Array.isArray(v)) return undefined;
  const out = v.map((x) => String(x)).filter((s) => s.length > 0);
  return out.length ? out : undefined;
}

/** Derive a leaf (symbol) role from an upstream node, defaulting sensibly.
 * A node with a role/kind/label wins; otherwise a qualified_name that looks
 * like a dotted symbol is treated as a function. */
function symbolRole(n: GraphNodeInput): Role {
  const explicit = normalizeRole(n.role ?? n.kind ?? n.label);
  if (explicit !== "unknown") return explicit;
  return "function";
}

/** Short display name for a symbol: the last dotted segment of the qualified
 * name, else `name`, else the qualified name itself. */
function symbolLabel(n: GraphNodeInput): string {
  const qn = String(n.qualified_name ?? n.qualifiedName ?? "").trim();
  if (qn) {
    const parts = qn.split(".");
    const last = parts[parts.length - 1];
    if (last) return last;
    return qn;
  }
  const nm = String(n.name ?? "").trim();
  return nm || "(anonymous)";
}

function extractMeta(n: GraphNodeInput): NodeMetadata {
  const meta: NodeMetadata = {};
  const qn = String(n.qualified_name ?? n.qualifiedName ?? "").trim();
  if (qn) meta.qualifiedName = qn;
  const file = String(n.file ?? n.path ?? "").trim();
  if (file) meta.file = file;
  const sig = String(n.signature ?? "").trim();
  if (sig) meta.signature = sig;
  const doc = String(n.docstring ?? n.doc ?? "").trim();
  if (doc) meta.docstring = doc;
  const cx = toNumber(n.complexity);
  if (cx !== undefined) meta.complexity = cx;
  const callers = toStringList(n.callers);
  if (callers) meta.callers = callers;
  const callees = toStringList(n.callees);
  if (callees) meta.callees = callees;
  const fanIn = toNumber(n.fanIn ?? n.fan_in);
  if (fanIn !== undefined) meta.fanIn = fanIn;
  const fanOut = toNumber(n.fanOut ?? n.fan_out);
  if (fanOut !== undefined) meta.fanOut = fanOut;
  return meta;
}

// A mutable builder node used only while assembling the tree.
interface BuildNode {
  id: string;
  label: string;
  role: Role;
  children: Map<string, BuildNode>;
  symbols: CodeGraphNode[];
  meta: NodeMetadata;
}

function newBuildNode(id: string, label: string, role: Role): BuildNode {
  return { id, label, role, children: new Map(), symbols: [], meta: {} };
}

/**
 * Shape a codebase-memory-mcp graph payload into a hierarchical folder ->
 * file -> function model. PURE, SYNCHRONOUS, DETERMINISTIC:
 *   - same input always yields the same output (stable ids + sorted children),
 *   - handles empty/partial payloads gracefully (returns an empty model),
 *   - invents no fields (every value traces to the input or a documented default).
 *
 * Nodes without a `file` are grouped under a deterministic "(unfiled)" folder so
 * nothing is silently dropped. Architecture packages with no matching nodes are
 * still emitted as empty top-level folders (so the package view isn't blank).
 */
export function buildCodeGraphModel(payload?: CodeGraphPayload | null): CodeGraphModel {
  const roots = new Map<string, BuildNode>();
  let symbolCount = 0;
  let fileCount = 0;

  const ensureFolderChain = (segments: string[]): BuildNode | null => {
    if (!segments.length) return null;
    let level = roots;
    let node: BuildNode | null = null;
    let idPrefix = "";
    for (const seg of segments) {
      idPrefix = idPrefix ? `${idPrefix}/${seg}` : seg;
      let child = level.get(seg);
      if (!child) {
        child = newBuildNode(idPrefix, seg, "folder");
        level.set(seg, child);
      }
      node = child;
      level = child.children;
    }
    return node;
  };

  // (1) Seed top-level folders from architecture packages so the package view is
  // never blank even when no per-symbol nodes are available.
  const arch = payload && payload.architecture;
  const packages = arch && Array.isArray(arch.packages) ? arch.packages : [];
  for (const p of packages) {
    const name = String(p && p.name != null ? p.name : "").trim();
    if (!name) continue;
    const segments = splitPath(name).length ? splitPath(name) : [name];
    const folder = ensureFolderChain(segments);
    if (folder) folder.role = "package";
  }

  // (2) Fold each graph node into the tree.
  const nodes = payload && Array.isArray(payload.nodes) ? payload.nodes : [];
  for (const n of nodes) {
    if (!n || typeof n !== "object") continue;
    // Skip truly-unidentifiable nodes (no qualified_name AND no name) — nothing
    // to label or key them by, so they're noise rather than droppable data.
    const hasIdentity = !!String(n.qualified_name ?? n.qualifiedName ?? n.name ?? "").trim();
    if (!hasIdentity) continue;
    const meta = extractMeta(n);
    const file = meta.file;
    const segments = file ? splitPath(file) : [];

    if (!segments.length) {
      // No file → park under a deterministic "(unfiled)" bucket.
      let unfiled = roots.get("(unfiled)");
      if (!unfiled) {
        unfiled = newBuildNode("(unfiled)", "(unfiled)", "folder");
        roots.set("(unfiled)", unfiled);
      }
      unfiled.symbols.push({
        id: `${unfiled.id}::${meta.qualifiedName ?? symbolLabel(n)}`,
        label: symbolLabel(n),
        role: symbolRole(n),
        depth: 1,
        children: [],
        meta,
      });
      symbolCount += 1;
      continue;
    }

    // Folder chain = all but the last segment; the last segment is the file.
    const fileSeg = segments[segments.length - 1];
    const folderSegs = segments.slice(0, -1);
    // Files always live under at least one folder node so drill-down has a root
    // even for a top-level file.
    const parentSegs = folderSegs.length ? folderSegs : ["(root)"];
    const folder = ensureFolderChain(parentSegs);
    if (!folder) continue;

    let fileNode = folder.children.get(fileSeg);
    if (!fileNode) {
      fileNode = newBuildNode(`${folder.id}/${fileSeg}`, fileSeg, "file");
      fileNode.meta = { file };
      folder.children.set(fileSeg, fileNode);
      fileCount += 1;
    }

    const role = symbolRole(n);
    // A node that IS the file itself (role file/module) enriches the file node
    // rather than adding a child.
    if (role === "file" || role === "module") {
      fileNode.role = "file";
      fileNode.meta = { ...fileNode.meta, ...meta, file };
      continue;
    }

    fileNode.symbols.push({
      id: `${fileNode.id}::${meta.qualifiedName ?? symbolLabel(n)}`,
      label: symbolLabel(n),
      role,
      depth: 2,
      children: [],
      meta,
    });
    symbolCount += 1;
  }

  const finalized = Array.from(roots.values()).map((r) => finalize(r, 0));
  return { roots: finalized, symbolCount, fileCount };
}

/** Freeze a BuildNode into an immutable, deterministically-ordered CodeGraphNode.
 * Children order: folders first, then files, then symbols; each group sorted by
 * label (stable, locale-independent). */
function finalize(b: BuildNode, depth: number): CodeGraphNode {
  const folderChildren: CodeGraphNode[] = [];
  const fileChildren: CodeGraphNode[] = [];
  for (const child of b.children.values()) {
    const fc = finalize(child, depth + 1);
    if (fc.role === "file") fileChildren.push(fc);
    else folderChildren.push(fc);
  }
  const byLabel = (a: CodeGraphNode, c: CodeGraphNode) =>
    a.label < c.label ? -1 : a.label > c.label ? 1 : (a.id < c.id ? -1 : a.id > c.id ? 1 : 0);
  folderChildren.sort(byLabel);
  fileChildren.sort(byLabel);
  const symbols = b.symbols
    .map((s) => ({ ...s, depth: depth + 1 }))
    .sort(byLabel);
  return {
    id: b.id,
    label: b.label,
    role: b.role,
    depth,
    children: [...folderChildren, ...fileChildren, ...symbols],
    meta: b.meta,
  };
}
