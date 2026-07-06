# codegraph — deterministic code tree/graph visualizer

A **standalone, framework-light** visualizer for [codebase-memory-mcp]'s static
graph output. It renders a codebase as a **drill-down tree** — folder-level by
default, drilling into files then functions — **colored by role**, showing the
**static metadata** (signature / docstring / complexity / callers / callees) on
click. **No LLM in the deterministic path.**

This directory is written to be **lifted out to its own repo as a copy**. Its
only inputs are a **graph payload** (a plain JS object) and a **mount element**.
It imports nothing from Meridian, touches no globals, does no network I/O, and
calls no LLM. The Meridian dashboard is just **one consumer** (see "Consumers").

Sprint item: `ed5512b6`.

---

## Files

| File | Responsibility | Pure? |
|------|----------------|-------|
| `model.ts` | Data contract + `buildCodeGraphModel(payload)` shaping fn | ✅ pure/sync |
| `roles.ts` | Deterministic, total role→color map | ✅ pure |
| `render.ts` | `renderCodeGraph(mount, model, opts)` DOM renderer + pure helpers | DOM-only |
| `index.ts` | Public barrel — the entire package surface | — |

---

## The data contract

The shaper accepts a `CodeGraphPayload`. **Both fields are optional** — pass
architecture alone (package view), graph nodes alone (full folder→file→function
view), or both.

```ts
interface CodeGraphPayload {
  // codebase-memory-mcp get_architecture(project) — the package/layer level.
  architecture?: {
    packages?: { name?: string; node_count?: number; layer?: number | string }[];
    layers?:   { name?: string; layer?: number | string }[];
  } | null;

  // codebase-memory-mcp search_graph(project, ...) results — per-symbol nodes
  // carrying the STATIC metadata. Every field optional; unknown fields ignored.
  nodes?: {
    qualified_name?: string;   // e.g. "auth.login"
    file?: string;             // e.g. "src/auth/login.py"
    label?: string;            // role/kind: Function|Method|Route|Class|...
    kind?: string; role?: string;
    signature?: string;
    docstring?: string;
    complexity?: number | string;
    callers?: string[]; callees?: string[];
    fan_in?: number; fan_out?: number;
  }[] | null;
}
```

These mirror the **real** upstream shapes:

- **`get_architecture`** returns `packages[{name,node_count,layer}]`,
  `layers[{name,layer}]`, `boundaries[{from,to,call_count}]`,
  `node_labels`, `edge_types`, `hotspots`, `clusters`. It is **package/layer-
  level only** — no per-function detail.
- **`search_graph`** returns graph nodes carrying `qualified_name`, `file`, a
  role via `label`/`kind`, and the static metadata (`signature`, `docstring`,
  `complexity`, caller/callee degree). This is where the file→function detail
  comes from.

`buildCodeGraphModel` returns a `CodeGraphModel`:

```ts
interface CodeGraphModel {
  roots: CodeGraphNode[];  // top-level folders/packages
  symbolCount: number;
  fileCount: number;
}
interface CodeGraphNode {
  id: string;              // stable, path-derived (same input → same id)
  label: string;
  role: Role;              // "folder"|"file"|"function"|"class"|"route"|... |"unknown"
  depth: number;
  children: CodeGraphNode[];
  meta: NodeMetadata;      // signature/docstring/complexity/callers/callees/...
}
```

**Guarantees** (all covered by `*.test.ts`):

- **Deterministic** — same input → byte-identical output (stable ids + children
  sorted folders→files→symbols, each by label).
- **Total / defensive** — empty or partial payloads return a valid (possibly
  empty) model; a node with no `file` is parked under a deterministic
  `(unfiled)` bucket; nothing throws.
- **No invented fields** — every value on an output node traces to the input or
  a documented default (`role: "unknown"`, `label: "(anonymous)"`).

The **role→color map** (`roles.ts`) is deterministic and **total**: every known
role has a stable hex color, and any unknown role maps to `DEFAULT_ROLE_COLOR`.

---

## Usage

```ts
import { buildCodeGraphModel, renderCodeGraph } from "./codegraph";

const model = buildCodeGraphModel({ architecture, nodes });
const view = renderCodeGraph(mountElement, model, {
  // OPTIONAL — the ONLY LLM touchpoint. A last-resort, on-demand button in the
  // detail pane. Omit it and the visualizer is fully deterministic/static.
  onRequestSummary: async (node) => fetchLlmSummary(node.meta.qualifiedName),
});
// later: view.destroy();
```

The renderer:

- shows **folder-level by default**, drills to **files** then **functions**;
- **colors every node by role** via the deterministic map;
- shows **signature / docstring / complexity / callers / callees** in the
  detail pane on click;
- has **no LLM in the deterministic render** — the LLM summary is a separate
  `onRequestSummary` button that only appears when the host supplies it.

---

## Consumers

### Meridian dashboard (one consumer, thin adapter)

`meridian/static/dashboard.ts` `loadCodeIntelTab` already fetches
`get_architecture` (and cross-package edges). The adapter passes that payload
straight into `buildCodeGraphModel` + `renderCodeGraph`, and wires
`onRequestSummary` to the existing code-intel `get_code_snippet` path. The module
never reaches back into the dashboard — data flows **in** only, so the coupling
is a single call site.

### Any other host

Feed it any object matching the data contract and a DOM element. That's the
whole integration.

---

## Lifting this to a standalone repo

Because the module's only inputs are the payload + a mount element, extraction is
a **copy**:

1. `cp -r meridian/static/codegraph <new-repo>/src`.
2. Keep `model.ts`, `roles.ts`, `render.ts`, `index.ts`, and the `*.test.ts`
   files. There are **no Meridian imports** to strip.
3. Add a `package.json` (`type: module`), `tsconfig.json` (strict), and a
   `vitest` config — the tests are already pure and jsdom-friendly.
4. Ship `index.ts` as the entry point.

### Honest scope

The **in-repo extractable module + data contract + README + dashboard wiring**
are done here. The **actual standalone GitHub repo, npm publish, and the
"free tool → acquisition funnel"** are the **maintainer's follow-on step** and
are intentionally **not** done (and not faked) in this change — same pattern as
the codebase-memory-mcp extraction. This directory is built so that follow-on is
a mechanical copy, nothing more.

[codebase-memory-mcp]: https://docs.usemeridian.us
