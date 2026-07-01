// Vitest + @testing-library/preact coverage for CodeIntelPanel (ff8ff615).
// One test per view state plus the Generate Map lifecycle and zoom controls.
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  CodeIntelPanel, buildLayers, mountCodeIntelPanel,
  buildCytoscapeElements, filterCyElements, mountCytoscapeGraph,
  CI_EDGE_COLORS, CI_EDGE_TYPES,
} from "./CodeIntelPanel";
import type { CiEdgeType } from "./CodeIntelPanel";
import type { Architecture } from "./types";

afterEach(() => cleanup());

const ARCH: Architecture = {
  packages: [
    { name: "routes", node_count: 12, layer: 2 },
    { name: "db", node_count: 30, layer: 0 },
    { name: "services", node_count: 8, layer: 1 },
    { name: "misc", node_count: 3 }, // no layer → 'other'
  ],
  layers: [
    { name: "api", layer: 2 },
    { name: "domain", layer: 1 },
    { name: "data", layer: 0 },
  ],
  boundaries: [{ from: "routes", to: "db", call_count: 5 }],
};

describe("CodeIntelPanel states", () => {
  it("renders the loading state", () => {
    render(<CodeIntelPanel status="loading" />);
    expect(screen.getByRole("status")).toHaveTextContent(/loading code intelligence/i);
  });

  it("renders the error state with the message", () => {
    render(<CodeIntelPanel status="error" error="boom: tunnel down" />);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/failed to load/i);
    expect(alert).toHaveTextContent(/boom: tunnel down/);
  });

  it("renders the empty-ready state when there are no packages", () => {
    render(<CodeIntelPanel status="ready" architecture={{ packages: [] }} />);
    expect(screen.getByText(/no package graph yet/i)).toBeInTheDocument();
  });

  it("renders a layered DAG of packages in the ready state", () => {
    render(<CodeIntelPanel status="ready" architecture={ARCH} />);
    // Every package surfaces as a node.
    for (const name of ["routes", "db", "services", "misc"]) {
      expect(screen.getByText(name)).toBeInTheDocument();
    }
    // Named layers are labelled; the unlayered bucket is shown too.
    expect(screen.getByText(/api · layer 2/)).toBeInTheDocument();
    expect(screen.getByText(/unlayered/)).toBeInTheDocument();
  });
});

describe("buildLayers ordering", () => {
  it("orders layers highest-first and sinks the unlayered bucket last", () => {
    const rows = buildLayers(ARCH);
    expect(rows.map((r) => r.rank)).toEqual(["2", "1", "0", "other"]);
    // Within a layer, packages sort by descending node_count.
    const data = rows.find((r) => r.rank === "0");
    expect(data?.packages[0]?.name).toBe("db");
  });

  it("returns no rows for missing/empty architecture", () => {
    expect(buildLayers(null)).toEqual([]);
    expect(buildLayers({ packages: [] })).toEqual([]);
  });
});

describe("zoom controls", () => {
  it("increments and decrements the zoom label", () => {
    render(<CodeIntelPanel status="ready" architecture={ARCH} />);
    expect(screen.getByText("100%")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Zoom in"));
    expect(screen.getByText("110%")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Zoom out"));
    expect(screen.getByText("100%")).toBeInTheDocument();
  });
});

describe("Generate Map lifecycle", () => {
  it("shows the image on success", async () => {
    const onGenerateMap = vi.fn().mockResolvedValue("data:image/png;base64,AAAA");
    render(<CodeIntelPanel status="ready" architecture={ARCH} onGenerateMap={onGenerateMap} />);
    fireEvent.click(screen.getByText("Generate Map"));
    await waitFor(() => {
      const img = screen.getByAltText("Codebase package map") as HTMLImageElement;
      expect(img.getAttribute("src")).toBe("data:image/png;base64,AAAA");
    });
    expect(onGenerateMap).toHaveBeenCalledOnce();
  });

  it("surfaces the error message on failure", async () => {
    const onGenerateMap = vi.fn().mockRejectedValue(new Error("Graphviz is not installed on the server."));
    render(<CodeIntelPanel status="ready" architecture={ARCH} onGenerateMap={onGenerateMap} />);
    fireEvent.click(screen.getByText("Generate Map"));
    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/graphviz is not installed/i);
    });
  });

  it("hides the Generate Map button when no generator is provided", () => {
    render(<CodeIntelPanel status="ready" architecture={ARCH} />);
    expect(screen.queryByText("Generate Map")).toBeNull();
  });
});

describe("mountCodeIntelPanel", () => {
  it("renders the panel into a host element", () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    mountCodeIntelPanel(host, { status: "ready", architecture: ARCH });
    expect(host.textContent).toContain("routes");
    expect(host.textContent).toContain("Package Graph");
    document.body.removeChild(host);
  });
});

describe("buildCytoscapeElements (455b7970 compound model)", () => {
  it("emits a compound layer parent per rank and package children under it", () => {
    const els = buildCytoscapeElements(ARCH);
    const layerNodes = els.filter((e) => e.group === "nodes" && e.data.kind === "layer");
    // One compound parent per distinct layer (2,1,0,other).
    expect(layerNodes.map((n) => n.data.id).sort()).toEqual(
      ["layer:0", "layer:1", "layer:2", "layer:other"].sort(),
    );
    // Packages are children of their layer parent.
    const routes = els.find((e) => e.data.id === "pkg:routes");
    expect(routes?.data.parent).toBe("layer:2");
    expect(routes?.data.kind).toBe("package");
  });

  it("emits boundary call edges as typed/colored 'invokes' edges", () => {
    const els = buildCytoscapeElements(ARCH);
    const edge = els.find((e) => e.group === "edges");
    expect(edge?.data.source).toBe("pkg:routes");
    expect(edge?.data.target).toBe("pkg:db");
    expect(edge?.data.etype).toBe("invokes");
    expect(edge?.data.color).toBe(CI_EDGE_COLORS.invokes);
  });

  it("descends into File/Class/Method children with inherits/imports edges when present", () => {
    const arch: Architecture = {
      packages: [{
        name: "svc", layer: 1,
        // deeper hierarchy the get_architecture payload will surface later
        children: [{ name: "a.py", kind: "file", children: [
          { name: "Base", kind: "class" },
          { name: "Impl", kind: "class", inherits: ["pkg:svc/a.py/Base"] },
        ] }],
      } as any],
    };
    const els = buildCytoscapeElements(arch);
    const impl = els.find((e) => e.data.label === "Impl");
    expect(impl?.data.parent).toBe("pkg:svc/a.py");
    const inh = els.find((e) => e.group === "edges" && e.data.etype === "inherits");
    expect(inh?.data.target).toBe("pkg:svc/a.py/Base");
  });
});

describe("filterCyElements + edge-type model", () => {
  it("declares all four edge types with distinct colors", () => {
    expect(CI_EDGE_TYPES).toEqual(["contains", "imports", "inherits", "invokes"]);
    const colors = new Set(CI_EDGE_TYPES.map((t) => CI_EDGE_COLORS[t]));
    expect(colors.size).toBe(4);
  });

  it("drops edges of disabled types but keeps every node", () => {
    const els = buildCytoscapeElements(ARCH);
    const nodeCount = els.filter((e) => e.group === "nodes").length;
    const enabled = new Set<CiEdgeType>(["contains"]); // invokes disabled
    const filtered = filterCyElements(els, enabled);
    expect(filtered.filter((e) => e.group === "nodes").length).toBe(nodeCount);
    expect(filtered.some((e) => e.group === "edges" && e.data.etype === "invokes")).toBe(false);
  });
});

describe("mountCytoscapeGraph guard", () => {
  it("returns null when window.cytoscape is unavailable (jsdom/SSR)", () => {
    const host = document.createElement("div");
    expect((window as any).cytoscape).toBeUndefined();
    expect(mountCytoscapeGraph(host, buildCytoscapeElements(ARCH))).toBeNull();
  });

  it("mounts and passes filtered elements when a cytoscape global is present", () => {
    const captured: any = {};
    const fakeCy = (cfg: any) => { captured.cfg = cfg; return { destroy() {} }; };
    (window as any).cytoscape = fakeCy;
    try {
      const enabled = new Set<CiEdgeType>(["contains"]); // invokes filtered out
      const inst = mountCytoscapeGraph(document.createElement("div"),
        buildCytoscapeElements(ARCH), { edgeFilter: enabled });
      expect(inst).not.toBeNull();
      const edgeEls = captured.cfg.elements.filter((e: any) => e.group === "edges");
      expect(edgeEls.every((e: any) => e.data.etype !== "invokes")).toBe(true);
    } finally {
      delete (window as any).cytoscape;
    }
  });
});
