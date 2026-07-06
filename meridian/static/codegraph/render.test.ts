// codegraph/render.test.ts — ed5512b6
// Pure render helpers + a jsdom smoke test of the deterministic renderer and the
// SEPARATE on-demand LLM summary action.
import { afterEach, describe, expect, it, vi } from "vitest";
import { buildCodeGraphModel } from "./model";
import { colorForRole } from "./roles";
import {
  renderCodeGraph,
  subtreeSummary,
  isDrillable,
  nodeRowLabel,
  startsExpanded,
  metadataLines,
} from "./render";
import type { CodeGraphNode } from "./model";

// jsdom serializes an inline hex color as `rgb(r, g, b)` — mirror that so the
// color-by-role assertion compares like-for-like.
function hexToRgb(hex: string): string {
  const m = /^#([0-9a-fA-F]{2})([0-9a-fA-F]{2})([0-9a-fA-F]{2})$/.exec(hex);
  if (!m) return hex;
  return `rgb(${parseInt(m[1], 16)}, ${parseInt(m[2], 16)}, ${parseInt(m[3], 16)})`;
}

const MODEL = buildCodeGraphModel({
  nodes: [
    {
      qualified_name: "auth.login",
      file: "src/auth/login.py",
      label: "Function",
      signature: "def login(u, p)",
      docstring: "Log in.",
      complexity: 3,
      callers: ["routes.x"],
      callees: ["db.y"],
    },
    { qualified_name: "auth.LoginForm", file: "src/auth/login.py", kind: "Class" },
  ],
});

function fileNode(): CodeGraphNode {
  const stack = [...MODEL.roots];
  while (stack.length) {
    const n = stack.pop()!;
    if (n.role === "file") return n;
    stack.push(...n.children);
  }
  throw new Error("no file node");
}

function leaf(qn: string): CodeGraphNode {
  const stack = [...MODEL.roots];
  while (stack.length) {
    const n = stack.pop()!;
    if (n.meta.qualifiedName === qn) return n;
    stack.push(...n.children);
  }
  throw new Error(`no leaf ${qn}`);
}

afterEach(() => { document.body.innerHTML = ""; });

describe("pure helpers", () => {
  it("subtreeSummary counts files and functions", () => {
    const src = MODEL.roots.find((r) => r.id === "src")!;
    expect(subtreeSummary(src)).toContain("1 file");
    expect(subtreeSummary(src)).toContain("2 fns");
  });

  it("isDrillable reflects children", () => {
    expect(isDrillable(fileNode())).toBe(true);
    expect(isDrillable(leaf("auth.login"))).toBe(false);
  });

  it("nodeRowLabel appends a summary for containers only", () => {
    expect(nodeRowLabel(fileNode())).toContain("login.py");
    expect(nodeRowLabel(leaf("auth.login"))).toBe("login");
  });

  it("startsExpanded honors the expand depth (folder-level default)", () => {
    const src = MODEL.roots.find((r) => r.id === "src")!;
    expect(startsExpanded(src, 1)).toBe(true); // depth 0 < 1
    expect(startsExpanded(fileNode(), 1)).toBe(false); // depth >= 1 collapsed
  });

  it("metadataLines renders the static fields", () => {
    const lines = metadataLines(leaf("auth.login"));
    const joined = lines.join("\n");
    expect(joined).toContain("qualified_name: auth.login");
    expect(joined).toContain("signature: def login(u, p)");
    expect(joined).toContain("complexity: 3");
    expect(joined).toContain("callers (1): routes.x");
    expect(joined).toContain("callees (1): db.y");
    expect(joined).toContain("docstring: Log in.");
  });

  it("metadataLines is empty for a bare node", () => {
    expect(metadataLines({ id: "x", label: "x", role: "folder", depth: 0, children: [], meta: {} })).toEqual([]);
  });
});

describe("renderCodeGraph (jsdom)", () => {
  it("renders folder-level by default and colors nodes by role", () => {
    const mount = document.createElement("div");
    document.body.appendChild(mount);
    renderCodeGraph(mount, MODEL);
    // Top-level folder row present, colored by role.
    const rows = mount.querySelectorAll(".codegraph-node");
    expect(rows.length).toBeGreaterThan(0);
    const src = mount.querySelector('.codegraph-node[data-id="src"]') as HTMLElement;
    expect(src).toBeTruthy();
    const row = src.querySelector(".codegraph-row") as HTMLElement;
    // jsdom normalizes the hex color to rgb(); assert the color is applied via
    // the deterministic map (folder → its stable color).
    expect(row.style.borderLeftColor).toBe(hexToRgb(colorForRole("folder")));
  });

  it("drills into files then functions on click", () => {
    const mount = document.createElement("div");
    document.body.appendChild(mount);
    renderCodeGraph(mount, MODEL);
    // src/auth is depth 1 so its children (the file) are collapsed + not yet
    // built until drilled — the folder-level default.
    expect(mount.querySelector('.codegraph-node[data-id="src/auth/login.py"]')).toBeNull();
    const authRow = (mount.querySelector('.codegraph-node[data-id="src/auth"] .codegraph-row')) as HTMLElement;
    authRow.click(); // expand auth → reveals login.py
    const file = mount.querySelector('.codegraph-node[data-id="src/auth/login.py"]') as HTMLElement;
    expect(file).toBeTruthy();
    const fileRow = file.querySelector(".codegraph-row") as HTMLElement;
    fileRow.click(); // drill into functions
    const fnLabels = Array.from(file.querySelectorAll(".codegraph-node")).map(
      (n) => (n as HTMLElement).dataset.id,
    );
    expect(fnLabels.some((id) => id?.includes("auth.login"))).toBe(true);
  });

  it("shows static metadata in the detail pane on click (no LLM)", () => {
    const mount = document.createElement("div");
    document.body.appendChild(mount);
    renderCodeGraph(mount, MODEL);
    const authRow = mount.querySelector('.codegraph-node[data-id="src/auth"] .codegraph-row') as HTMLElement;
    authRow.click();
    const fileRow = mount.querySelector('.codegraph-node[data-id="src/auth/login.py"] .codegraph-row') as HTMLElement;
    fileRow.click();
    const fnRow = mount.querySelector(
      '.codegraph-node[data-id="src/auth/login.py::auth.login"] .codegraph-row',
    ) as HTMLElement;
    fnRow.click();
    const detail = mount.querySelector(".codegraph-detail") as HTMLElement;
    expect(detail.textContent).toContain("signature: def login(u, p)");
    // No LLM button when onRequestSummary isn't supplied.
    expect(detail.querySelector("button")).toBeNull();
  });

  it("LLM summary is a separate on-demand action only when onRequestSummary is given", async () => {
    const mount = document.createElement("div");
    document.body.appendChild(mount);
    const onRequestSummary = vi.fn().mockResolvedValue("a summary");
    renderCodeGraph(mount, MODEL, { onRequestSummary });
    const authRow = mount.querySelector('.codegraph-node[data-id="src/auth"] .codegraph-row') as HTMLElement;
    authRow.click();
    const fileRow = mount.querySelector('.codegraph-node[data-id="src/auth/login.py"] .codegraph-row') as HTMLElement;
    fileRow.click();
    const fnRow = mount.querySelector(
      '.codegraph-node[data-id="src/auth/login.py::auth.login"] .codegraph-row',
    ) as HTMLElement;
    fnRow.click();
    const detail = mount.querySelector(".codegraph-detail") as HTMLElement;
    const btn = detail.querySelector("button") as HTMLButtonElement;
    expect(btn).toBeTruthy();
    // Not called until the user clicks — it's last-resort, not in the render path.
    expect(onRequestSummary).not.toHaveBeenCalled();
    btn.click();
    await vi.waitFor(() => expect(detail.textContent).toContain("a summary"));
    expect(onRequestSummary).toHaveBeenCalledTimes(1);
  });

  it("invokes onSelect and renders an empty-state for an empty model", () => {
    const mount = document.createElement("div");
    document.body.appendChild(mount);
    const onSelect = vi.fn();
    renderCodeGraph(mount, MODEL, { onSelect });
    const src = mount.querySelector('.codegraph-node[data-id="src"] .codegraph-row') as HTMLElement;
    src.click();
    expect(onSelect).toHaveBeenCalled();

    const empty = document.createElement("div");
    renderCodeGraph(empty, buildCodeGraphModel(null));
    expect(empty.textContent).toContain("No code graph yet");
  });

  it("destroy() clears the mount", () => {
    const mount = document.createElement("div");
    document.body.appendChild(mount);
    const view = renderCodeGraph(mount, MODEL);
    expect(mount.children.length).toBeGreaterThan(0);
    view.destroy();
    expect(mount.children.length).toBe(0);
  });
});
