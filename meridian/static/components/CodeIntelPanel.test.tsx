// Vitest + @testing-library/preact coverage for CodeIntelPanel (ff8ff615).
// One test per view state plus the Generate Map lifecycle and zoom controls.
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/preact";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CodeIntelPanel, buildLayers, mountCodeIntelPanel } from "./CodeIntelPanel";
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
