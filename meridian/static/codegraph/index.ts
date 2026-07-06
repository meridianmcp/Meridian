// codegraph/index.ts — ed5512b6
//
// Public barrel for the standalone codegraph visualizer. A host imports from
// here and nothing else. This is the entire surface that would ship as a
// standalone package (see README.md): a pure data-shaping fn, a deterministic
// role->color map, and a framework-light DOM renderer.
//
// The module has ZERO Meridian coupling: its only inputs are the graph payload
// (a plain object) and a mount element. Lifting codegraph/ to its own repo is a
// copy of this directory.

export type {
  Role,
  NodeMetadata,
  CodeGraphNode,
  CodeGraphModel,
  GraphNodeInput,
  ArchPackageInput,
  ArchitectureInput,
  CodeGraphPayload,
} from "./model";
export {
  buildCodeGraphModel,
  normalizeRole,
  splitPath,
} from "./model";

export { ROLE_COLORS, DEFAULT_ROLE_COLOR, colorForRole } from "./roles";

export type { CodeGraphRenderOptions, CodeGraphView } from "./render";
export {
  renderCodeGraph,
  subtreeSummary,
  isDrillable,
  nodeRowLabel,
  startsExpanded,
  metadataLines,
} from "./render";
