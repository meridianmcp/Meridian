// codegraph/render.ts — ed5512b6
//
// A DETERMINISTIC, framework-light renderer for the CodeGraphModel. It takes a
// model + a mount element and renders a collapsible drill-down tree:
//   folder-level by default -> drill to files -> drill to functions,
// colored by role, showing the static metadata (signature / docstring /
// complexity / callers / callees) in a details pane on click.
//
// DECOUPLED by design — the module's ONLY inputs are the model + a mount
// element (plus optional pure callbacks). It never reads dashboard.ts globals,
// never fetches, and never calls an LLM in the deterministic path. That keeps
// the whole codegraph/ dir extractable to a standalone repo (see README.md).
//
// The LLM summary is a SEPARATE, last-resort, on-click action: if (and only if)
// the host passes `onRequestSummary`, a "✨ LLM summary" button appears in the
// detail pane and calls that host-provided function on demand. The deterministic
// render works fully without it.

import type { CodeGraphModel, CodeGraphNode } from "./model";
import { colorForRole } from "./roles";

export interface CodeGraphRenderOptions {
  /**
   * OPTIONAL last-resort LLM summary action. When provided, a button in the
   * detail pane invokes it with the selected node and resolves to summary text.
   * This is the ONLY LLM touchpoint and it is never part of the deterministic
   * render — omit it and the visualizer is fully static. Host-owned so the
   * module stays decoupled from any specific LLM/backend.
   */
  onRequestSummary?: (node: CodeGraphNode) => Promise<string>;
  /** OPTIONAL selection callback (host analytics / linking). Pure side effect. */
  onSelect?: (node: CodeGraphNode) => void;
  /** Roles that start collapsed. Defaults to everything below the top level
   * being collapsed (folder-level default view). */
  initiallyExpandedDepth?: number;
}

/** Handle returned by render() so a host can tear the view down cleanly. */
export interface CodeGraphView {
  /** Remove all rendered DOM + listeners from the mount element. */
  destroy(): void;
  /** The mount element the view was rendered into. */
  element: HTMLElement;
}

// ---------------------------------------------------------------------------
// Pure helpers — no DOM. Exported so they're unit-testable in isolation.
// ---------------------------------------------------------------------------

/** A short, deterministic count summary for a node's subtree, e.g. "3 files ·
 * 12 fns". Pure. */
export function subtreeSummary(node: CodeGraphNode): string {
  let files = 0;
  let symbols = 0;
  const walk = (n: CodeGraphNode) => {
    for (const c of n.children) {
      if (c.role === "file") files += 1;
      else if (c.children.length === 0 && c.role !== "folder" && c.role !== "package") symbols += 1;
      walk(c);
    }
  };
  walk(node);
  const parts: string[] = [];
  if (files) parts.push(`${files} file${files === 1 ? "" : "s"}`);
  if (symbols) parts.push(`${symbols} fn${symbols === 1 ? "" : "s"}`);
  return parts.join(" · ");
}

/** True when a node can be drilled into (has children). Pure. */
export function isDrillable(node: CodeGraphNode): boolean {
  return node.children.length > 0;
}

/** A one-line label for a node used in the tree row. Pure. */
export function nodeRowLabel(node: CodeGraphNode): string {
  const summary = subtreeSummary(node);
  return summary ? `${node.label}  ${summary}` : node.label;
}

/** Whether a node should start expanded given the initial-expand depth. Pure. */
export function startsExpanded(node: CodeGraphNode, expandDepth: number): boolean {
  return node.depth < expandDepth;
}

/** Format the metadata block for the detail pane as plain, safe text lines.
 * Pure — returns strings, no DOM. */
export function metadataLines(node: CodeGraphNode): string[] {
  const m = node.meta;
  const lines: string[] = [];
  if (m.qualifiedName) lines.push(`qualified_name: ${m.qualifiedName}`);
  if (m.file) lines.push(`file: ${m.file}`);
  if (m.signature) lines.push(`signature: ${m.signature}`);
  if (typeof m.complexity === "number") lines.push(`complexity: ${m.complexity}`);
  if (m.callers && m.callers.length) lines.push(`callers (${m.callers.length}): ${m.callers.join(", ")}`);
  else if (typeof m.fanIn === "number") lines.push(`callers (fan-in): ${m.fanIn}`);
  if (m.callees && m.callees.length) lines.push(`callees (${m.callees.length}): ${m.callees.join(", ")}`);
  else if (typeof m.fanOut === "number") lines.push(`callees (fan-out): ${m.fanOut}`);
  if (m.docstring) lines.push(`docstring: ${m.docstring}`);
  return lines;
}

// ---------------------------------------------------------------------------
// DOM renderer. Kept dependency-free (raw DOM) so it lifts cleanly to any host.
// ---------------------------------------------------------------------------

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  style?: Partial<CSSStyleDeclaration>,
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (style) Object.assign(node.style, style);
  if (text != null) node.textContent = text;
  return node;
}

function roleBadge(role: string): HTMLElement {
  const badge = el("span", {
    display: "inline-block",
    fontSize: "8px",
    lineHeight: "1",
    padding: "2px 5px",
    borderRadius: "3px",
    color: "#0b0e14",
    background: colorForRole(role),
    fontFamily: "var(--font-mono, monospace)",
    textTransform: "uppercase",
    letterSpacing: ".03em",
    flexShrink: "0",
  });
  badge.textContent = role;
  return badge;
}

/**
 * Render the model into `mount`. Deterministic: identical model + options
 * produce identical DOM. Returns a view handle. Never throws on empty input
 * (renders an empty-state hint instead).
 */
export function renderCodeGraph(
  mount: HTMLElement,
  model: CodeGraphModel,
  options: CodeGraphRenderOptions = {},
): CodeGraphView {
  const expandDepth = options.initiallyExpandedDepth ?? 1; // folder-level default
  mount.textContent = "";

  const root = el("div", {
    display: "flex",
    gap: "10px",
    alignItems: "flex-start",
    fontFamily: "var(--font-mono, monospace)",
  });
  root.className = "codegraph-root";

  const treePane = el("div", {
    flex: "1 1 auto",
    minWidth: "0",
    maxHeight: "420px",
    overflow: "auto",
    border: "1px solid var(--border, #334155)",
    borderRadius: "4px",
    padding: "6px",
    background: "var(--surface-1, #0f172a)",
  });
  treePane.className = "codegraph-tree";

  const detailPane = el("div", {
    flex: "0 0 300px",
    maxHeight: "420px",
    overflow: "auto",
    border: "1px solid var(--border, #334155)",
    borderRadius: "4px",
    padding: "10px",
    background: "var(--surface-1, #0f172a)",
    fontSize: "11px",
    color: "var(--text, #e2e8f0)",
  });
  detailPane.className = "codegraph-detail";
  renderEmptyDetail(detailPane);

  if (!model.roots.length) {
    treePane.appendChild(
      el("div", { fontSize: "11px", color: "var(--muted, #94a3b8)", padding: "12px" },
        "No code graph yet — index the repo to populate it."),
    );
  } else {
    for (const node of model.roots) {
      treePane.appendChild(renderNode(node, detailPane, expandDepth, options));
    }
  }

  root.appendChild(treePane);
  root.appendChild(detailPane);
  mount.appendChild(root);

  return {
    element: mount,
    destroy() {
      mount.textContent = "";
    },
  };
}

function renderNode(
  node: CodeGraphNode,
  detailPane: HTMLElement,
  expandDepth: number,
  options: CodeGraphRenderOptions,
): HTMLElement {
  const wrap = el("div", { marginLeft: node.depth > 0 ? "12px" : "0" });
  wrap.className = "codegraph-node";
  wrap.dataset.role = node.role;
  wrap.dataset.id = node.id;

  const row = el("div", {
    display: "flex",
    alignItems: "center",
    gap: "6px",
    padding: "2px 4px",
    borderRadius: "3px",
    cursor: "pointer",
    fontSize: "11px",
    color: "var(--text, #e2e8f0)",
    borderLeft: `3px solid ${colorForRole(node.role)}`,
  });
  row.className = "codegraph-row";

  const drillable = isDrillable(node);
  const caret = el("span", {
    width: "10px",
    flexShrink: "0",
    color: "var(--muted, #94a3b8)",
    fontSize: "9px",
  });
  caret.textContent = drillable ? "▸" : "·";

  const label = el("span", {
    whiteSpace: "nowrap",
    overflow: "hidden",
    textOverflow: "ellipsis",
    flex: "1 1 auto",
    minWidth: "0",
  });
  label.textContent = node.label;

  const summary = subtreeSummary(node);
  const summaryEl = el("span", { fontSize: "9px", color: "var(--muted, #94a3b8)", flexShrink: "0" });
  summaryEl.textContent = summary;

  row.appendChild(caret);
  row.appendChild(roleBadge(node.role));
  row.appendChild(label);
  if (summary) row.appendChild(summaryEl);
  wrap.appendChild(row);

  const childrenWrap = el("div", { display: startsExpanded(node, expandDepth) ? "block" : "none" });
  childrenWrap.className = "codegraph-children";
  let built = false;
  const buildChildren = () => {
    if (built) return;
    for (const c of node.children) {
      childrenWrap.appendChild(renderNode(c, detailPane, expandDepth, options));
    }
    built = true;
  };
  if (startsExpanded(node, expandDepth)) buildChildren();
  wrap.appendChild(childrenWrap);

  row.addEventListener("click", (ev) => {
    ev.stopPropagation();
    // Always show details for the clicked node.
    renderDetail(detailPane, node, options);
    if (options.onSelect) {
      try { options.onSelect(node); } catch { /* host callback must not break render */ }
    }
    // Drill toggle for containers.
    if (drillable) {
      const open = childrenWrap.style.display !== "none";
      if (!open) buildChildren();
      childrenWrap.style.display = open ? "none" : "block";
      caret.textContent = open ? "▸" : "▾";
    }
  });

  return wrap;
}

function renderEmptyDetail(pane: HTMLElement): void {
  pane.textContent = "";
  pane.appendChild(
    el("div", { color: "var(--muted, #94a3b8)", fontSize: "10px" },
      "Select a node to see its static metadata."),
  );
}

function renderDetail(pane: HTMLElement, node: CodeGraphNode, options: CodeGraphRenderOptions): void {
  pane.textContent = "";

  const header = el("div", { display: "flex", gap: "6px", alignItems: "center", marginBottom: "8px" });
  header.appendChild(roleBadge(node.role));
  header.appendChild(el("span", { fontWeight: "600", fontSize: "12px", wordBreak: "break-all" }, node.label));
  pane.appendChild(header);

  const lines = metadataLines(node);
  if (!lines.length) {
    pane.appendChild(
      el("div", { color: "var(--muted, #94a3b8)", fontSize: "10px" },
        "No static metadata on this node."),
    );
  } else {
    for (const line of lines) {
      pane.appendChild(
        el("div", {
          fontSize: "10px",
          color: "var(--text, #e2e8f0)",
          padding: "2px 0",
          borderBottom: "1px solid var(--border, #1f2937)",
          wordBreak: "break-word",
          whiteSpace: "pre-wrap",
        }, line),
      );
    }
  }

  // LLM summary — SEPARATE, last-resort, on-demand action. Only when the host
  // supplied onRequestSummary; never part of the deterministic render above.
  if (options.onRequestSummary) {
    const btn = el("button", {
      marginTop: "10px",
      fontSize: "10px",
      padding: "3px 10px",
      border: "1px solid var(--border, #334155)",
      borderRadius: "3px",
      background: "none",
      color: "var(--accent, #7dd3fc)",
      cursor: "pointer",
      fontFamily: "var(--font-mono, monospace)",
    });
    btn.textContent = "✨ LLM summary";
    btn.title = "On-demand LLM summary — not part of the deterministic view";
    const out = el("div", { marginTop: "8px", fontSize: "10px", color: "var(--muted, #94a3b8)", whiteSpace: "pre-wrap" });
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = "summarizing…";
      try {
        const summary = await options.onRequestSummary!(node);
        out.style.color = "var(--text, #e2e8f0)";
        out.textContent = summary;
      } catch (e) {
        out.style.color = "var(--error, #ef4444)";
        out.textContent = `Summary failed: ${e instanceof Error ? e.message : String(e)}`;
      } finally {
        btn.disabled = false;
        btn.textContent = "✨ LLM summary";
      }
    });
    pane.appendChild(btn);
    pane.appendChild(out);
  }
}
