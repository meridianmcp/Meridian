// Unit tests for the client-side subproject-hierarchy helpers (0fed6a42).
import { describe, expect, it } from "vitest";
import {
  flattenHierarchy,
  hasSubprojects,
  eligibleParents,
  validateSubprojectMove,
  type HierProject,
} from "./dashboard-subprojects";

const P = (id: string, parent: string | null = null, name = id): HierProject => ({
  id,
  name,
  parent_project_id: parent,
});

describe("flattenHierarchy", () => {
  it("keeps top-level projects flat (depth 0) when nothing is nested", () => {
    const rows = flattenHierarchy([P("a"), P("b"), P("c")]);
    expect(rows.map((r) => [r.project.id, r.depth])).toEqual([
      ["a", 0],
      ["b", 0],
      ["c", 0],
    ]);
  });

  it("places a subproject immediately after its parent at depth 1", () => {
    // Incoming order deliberately does NOT keep child adjacent to parent.
    const rows = flattenHierarchy([P("parent"), P("other"), P("child", "parent")]);
    expect(rows.map((r) => [r.project.id, r.depth])).toEqual([
      ["parent", 0],
      ["child", 1],
      ["other", 0],
    ]);
  });

  it("groups multiple children under one parent, preserving their order", () => {
    const rows = flattenHierarchy([
      P("p"),
      P("c2", "p"),
      P("c1", "p"),
    ]);
    expect(rows.map((r) => r.project.id)).toEqual(["p", "c2", "c1"]);
    expect(rows.map((r) => r.depth)).toEqual([0, 1, 1]);
  });

  it("renders an orphan (parent not in the set) at top level, never dropping it", () => {
    const rows = flattenHierarchy([P("a"), P("orphan", "missing")]);
    expect(rows.map((r) => [r.project.id, r.depth])).toEqual([
      ["a", 0],
      ["orphan", 0],
    ]);
  });

  it("never nests a grandchild (one-level-deep guard) — flattens to top level", () => {
    // c's parent (b) is itself a subproject of a; c must NOT render under b.
    const rows = flattenHierarchy([P("a"), P("b", "a"), P("c", "b")]);
    // a -> b (child), then c falls back to top level since b is not top-level.
    expect(rows.map((r) => [r.project.id, r.depth])).toEqual([
      ["a", 0],
      ["b", 1],
      ["c", 0],
    ]);
  });

  it("treats empty-string parent ids as top-level", () => {
    const rows = flattenHierarchy([{ id: "a", parent_project_id: "" }]);
    expect(rows).toEqual([{ project: { id: "a", parent_project_id: "" }, depth: 0 }]);
  });

  it("returns [] for an empty list", () => {
    expect(flattenHierarchy([])).toEqual([]);
  });
});

describe("hasSubprojects", () => {
  it("detects a project that parents at least one other", () => {
    const projects = [P("p"), P("c", "p")];
    expect(hasSubprojects(projects, "p")).toBe(true);
    expect(hasSubprojects(projects, "c")).toBe(false);
  });
});

describe("eligibleParents", () => {
  it("offers every other top-level project as a candidate parent", () => {
    const projects = [P("a"), P("b"), P("c")];
    expect(eligibleParents(projects, "a").map((p) => p.id)).toEqual(["b", "c"]);
  });

  it("excludes the project itself", () => {
    const projects = [P("a"), P("b")];
    expect(eligibleParents(projects, "a").map((p) => p.id)).toEqual(["b"]);
  });

  it("excludes projects that are themselves subprojects (parent must be top-level)", () => {
    const projects = [P("a"), P("b"), P("child", "b")];
    // For a: b is top-level (eligible), child is a subproject (excluded).
    expect(eligibleParents(projects, "a").map((p) => p.id)).toEqual(["b"]);
  });

  it("excludes the project's current parent (no-op move)", () => {
    const projects = [P("a"), P("b"), P("child", "a")];
    // child's current parent a is excluded; b would be the only *new* option,
    // but b is top-level → eligible; a is filtered out.
    expect(eligibleParents(projects, "child").map((p) => p.id)).toEqual(["b"]);
  });

  it("returns [] when the project already has subprojects of its own", () => {
    const projects = [P("parent"), P("child", "parent"), P("other")];
    expect(eligibleParents(projects, "parent")).toEqual([]);
  });
});

describe("validateSubprojectMove", () => {
  const projects = [P("a"), P("b"), P("child", "a"), P("grand")];

  it("allows a legal move (top-level child under a top-level parent)", () => {
    expect(validateSubprojectMove(projects, "b", "a")).toBeNull();
  });

  it("always allows detach (null parent)", () => {
    expect(validateSubprojectMove(projects, "child", null)).toBeNull();
  });

  it("blocks self-parenting", () => {
    expect(validateSubprojectMove(projects, "a", "a")).toBe("self");
  });

  it("blocks a project that already has children from becoming a subproject", () => {
    expect(validateSubprojectMove(projects, "a", "b")).toBe("has-children");
  });

  it("blocks nesting under a non-top-level parent", () => {
    expect(validateSubprojectMove(projects, "grand", "child")).toBe("parent-not-toplevel");
  });

  it("blocks nesting under a missing parent", () => {
    expect(validateSubprojectMove(projects, "b", "nope")).toBe("parent-missing");
  });

  it("reports a no-op when parent is already the current parent", () => {
    expect(validateSubprojectMove(projects, "child", "a")).toBe("no-op");
  });
});
