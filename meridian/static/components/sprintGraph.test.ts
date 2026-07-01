// Coverage for the sprint dependency DAG + gantt model (233bae67).
import { describe, expect, it } from "vitest";
import {
  buildSprintDagElements, buildGanttModel, criticalPath, parseResources,
  sprintStatusColor, mountSprintDag, SPRINT_STATUS_COLORS, ganttBars,
} from "./sprintGraph";
import type { SprintItemLike } from "./sprintGraph";

const ITEMS: SprintItemLike[] = [
  { id: "a", title: "root", status: "done", touches_resources: '["file:server.py"]',
    added_at: "2026-06-01 10:00:00", claimed_at: "2026-06-01 10:05:00", completed_at: "2026-06-01 11:00:00" },
  { id: "b", title: "mid", status: "in_progress", depends_on: "a", touches_resources: ["file:server.py"],
    added_at: "2026-06-01 11:00:00", claimed_at: "2026-06-01 11:05:00" },
  { id: "c", title: "leaf", status: "pending", depends_on: "b", touches_resources: "file:db.py",
    added_at: "2026-06-01 12:00:00" },
];

describe("sprint DAG elements", () => {
  it("colors nodes by status", () => {
    const els = buildSprintDagElements(ITEMS);
    const nodeA = els.find((e) => e.data.id === "item:a");
    expect(nodeA?.data.color).toBe(SPRINT_STATUS_COLORS.done);
    const nodeB = els.find((e) => e.data.id === "item:b");
    expect(nodeB?.data.color).toBe(SPRINT_STATUS_COLORS.in_progress);
  });

  it("emits directed depends_on edges", () => {
    const els = buildSprintDagElements(ITEMS);
    const dep = els.find((e) => e.group === "edges" && e.data.etype === "depends" && e.data.target === "item:b");
    expect(dep?.data.source).toBe("item:a");
  });

  it("emits a dashed conflict edge for items sharing a resource", () => {
    const els = buildSprintDagElements(ITEMS);
    const conflicts = els.filter((e) => e.data.etype === "conflict");
    // a & b both touch file:server.py → exactly one undirected conflict edge.
    expect(conflicts).toHaveLength(1);
    expect(conflicts[0].data.resource).toBe("file:server.py");
    expect([conflicts[0].data.source, conflicts[0].data.target].sort()).toEqual(["item:a", "item:b"]);
  });

  it("marks the critical path a → b → c", () => {
    expect(criticalPath(ITEMS)).toEqual(["a", "b", "c"]);
    const els = buildSprintDagElements(ITEMS);
    expect(els.find((e) => e.data.id === "item:c")?.data.critical).toBe(1);
  });

  it("survives a dependency cycle without hanging", () => {
    const cyc: SprintItemLike[] = [
      { id: "x", depends_on: "y" }, { id: "y", depends_on: "x" },
    ];
    expect(() => buildSprintDagElements(cyc)).not.toThrow();
  });
});

describe("gantt model", () => {
  it("builds bars sorted by start with a global min/max scale", () => {
    const g = buildGanttModel(ITEMS);
    expect(g.rows.map((r) => r.id)).toEqual(["a", "b", "c"]);
    // 'a' is complete → a real duration; 'c' has no claim → zero-width at its start.
    const rowA = g.rows[0];
    expect(rowA.endMs).toBeGreaterThan(rowA.startMs);
    expect(g.minMs).toBe(rowA.startMs);
    // maxMs spans the whole set — 'c' starts latest (12:00), past 'a's end (11:00).
    expect(g.maxMs).toBe(Math.max(...g.rows.map((r) => r.endMs)));
    expect(g.maxMs).toBe(g.rows[2].endMs);
  });

  it("skips items with no start timestamp", () => {
    const g = buildGanttModel([{ id: "z", status: "pending" }]);
    expect(g.rows).toHaveLength(0);
  });

  it("ganttBars positions bars within [0,100]% of the span", () => {
    const bars = ganttBars(ITEMS);
    expect(bars[0].leftPct).toBe(0); // earliest item starts at 0%
    for (const b of bars) {
      expect(b.leftPct).toBeGreaterThanOrEqual(0);
      expect(b.leftPct + b.widthPct).toBeLessThanOrEqual(100.01);
      expect(b.widthPct).toBeGreaterThan(0);
    }
  });
});

describe("helpers + mount guard", () => {
  it("parseResources handles JSON, CSV, array, and empty", () => {
    expect(parseResources('["file:a","db:x"]')).toEqual(["file:a", "db:x"]);
    expect(parseResources("file:a, db:x")).toEqual(["file:a", "db:x"]);
    expect(parseResources(["file:a"])).toEqual(["file:a"]);
    expect(parseResources(null)).toEqual([]);
  });

  it("sprintStatusColor falls back for unknown status", () => {
    expect(sprintStatusColor("nonsense")).toBe("#64748b");
  });

  it("mountSprintDag returns null without window.cytoscape", () => {
    expect((window as any).cytoscape).toBeUndefined();
    expect(mountSprintDag(document.createElement("div"), buildSprintDagElements(ITEMS))).toBeNull();
  });

  it("mountSprintDag builds a cy instance with status-colored nodes when a global is present", () => {
    const captured: any = {};
    (window as any).cytoscape = (cfg: any) => { captured.cfg = cfg; return { destroy() {} }; };
    try {
      const inst = mountSprintDag(document.createElement("div"), buildSprintDagElements(ITEMS));
      expect(inst).not.toBeNull();
      const nodes = captured.cfg.elements.filter((e: any) => e.group === "nodes");
      // The 'done' item 'a' carries its status color into the cy element data.
      const nodeA = nodes.find((n: any) => n.data.id === "item:a");
      expect(nodeA.data.color).toBe(SPRINT_STATUS_COLORS.done);
      // fcose is optional; without it the layout falls back to breadthfirst.
      expect(captured.cfg.layout.name).toBe("breadthfirst");
    } finally {
      delete (window as any).cytoscape;
    }
  });
});
