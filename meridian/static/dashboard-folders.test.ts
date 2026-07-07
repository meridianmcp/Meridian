// Unit tests for the client-side sidebar folders/spheres helper (d6b7da48).
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  FOLDER_ASSIGN_KEY,
  FOLDER_COLLAPSE_KEY,
  UNGROUPED_LABEL,
  normalizeFolderName,
  loadFolderAssignments,
  saveFolderAssignments,
  assignProjectToFolder,
  groupProjectsByFolder,
  knownFolderNames,
  loadCollapsedFolders,
  saveCollapsedFolders,
  toggleFolderCollapsed,
  type FolderProject,
} from "./dashboard-folders";

const P = (id: string, name = id): FolderProject => ({ id, name });

beforeEach(() => localStorage.clear());
afterEach(() => localStorage.clear());

describe("normalizeFolderName", () => {
  it("trims and collapses whitespace; empty → ''", () => {
    expect(normalizeFolderName("  Grad   School ")).toBe("Grad School");
    expect(normalizeFolderName("   ")).toBe("");
    expect(normalizeFolderName(null)).toBe("");
    expect(normalizeFolderName(undefined)).toBe("");
  });
});

describe("assignProjectToFolder", () => {
  it("assigns a project to a folder without mutating the input", () => {
    const before: Record<string, string> = {};
    const after = assignProjectToFolder(before, "a", "Grad");
    expect(after).toEqual({ a: "Grad" });
    expect(before).toEqual({}); // pure
  });
  it("clears the assignment when folder is blank/null (→ ungrouped)", () => {
    const start = { a: "Grad", b: "Work" };
    expect(assignProjectToFolder(start, "a", "")).toEqual({ b: "Work" });
    expect(assignProjectToFolder(start, "a", "   ")).toEqual({ b: "Work" });
    expect(assignProjectToFolder(start, "a", null)).toEqual({ b: "Work" });
  });
  it("normalizes the folder name on assign", () => {
    expect(assignProjectToFolder({}, "a", "  My  Folder ")).toEqual({ a: "My Folder" });
  });
});

describe("groupProjectsByFolder — assign → grouped render", () => {
  it("groups assigned projects and drops an empty ungrouped catch-all", () => {
    const projects = [P("a"), P("b")];
    let assignments: Record<string, string> = {};
    assignments = assignProjectToFolder(assignments, "a", "School");
    assignments = assignProjectToFolder(assignments, "b", "School");
    const groups = groupProjectsByFolder(projects, assignments);
    expect(groups).toHaveLength(1);
    expect(groups[0].folder).toBe("School");
    expect(groups[0].label).toBe("School");
    expect(groups[0].key).toBe("School");
    expect(groups[0].projects.map((p) => p.id)).toEqual(["a", "b"]);
  });

  it("emits the ungrouped catch-all last with label + null folder", () => {
    const projects = [P("a"), P("b"), P("c")];
    const assignments = assignProjectToFolder({}, "a", "School");
    const groups = groupProjectsByFolder(projects, assignments);
    expect(groups).toHaveLength(2);
    expect(groups[0].folder).toBe("School");
    const catchAll = groups[groups.length - 1];
    expect(catchAll.folder).toBeNull();
    expect(catchAll.label).toBe(UNGROUPED_LABEL);
    expect(catchAll.key).toBe("");
    expect(catchAll.projects.map((p) => p.id)).toEqual(["b", "c"]);
  });

  it("yields a single ungrouped group when nothing is assigned (flat parity)", () => {
    const projects = [P("a"), P("b")];
    const groups = groupProjectsByFolder(projects, {});
    expect(groups).toHaveLength(1);
    expect(groups[0].folder).toBeNull();
    expect(groups[0].projects.map((p) => p.id)).toEqual(["a", "b"]);
  });

  it("preserves incoming order both across folders (first-seen) and within", () => {
    const projects = [P("a"), P("b"), P("c"), P("d")];
    let assignments: Record<string, string> = {};
    // b -> Beta first, then a -> Alpha: Beta should come before Alpha (first-seen).
    assignments = assignProjectToFolder(assignments, "b", "Beta");
    assignments = assignProjectToFolder(assignments, "a", "Alpha");
    assignments = assignProjectToFolder(assignments, "d", "Beta");
    const groups = groupProjectsByFolder(projects, assignments);
    // Order determined by project iteration order: a(Alpha) seen before b(Beta).
    expect(groups.map((g) => g.folder)).toEqual(["Alpha", "Beta", null]);
    expect(groups[1].projects.map((p) => p.id)).toEqual(["b", "d"]);
    expect(groups[2].projects.map((p) => p.id)).toEqual(["c"]);
  });

  it("returns no groups for an empty project list", () => {
    expect(groupProjectsByFolder([], {})).toEqual([]);
  });
});

describe("knownFolderNames", () => {
  it("lists distinct folder names in first-seen order", () => {
    const projects = [P("a"), P("b"), P("c")];
    const assignments = { a: "School", b: "Work", c: "School" };
    expect(knownFolderNames(projects, assignments)).toEqual(["School", "Work"]);
  });
});

describe("localStorage round-trip — assignments", () => {
  it("saves and reloads assignments", () => {
    const assignments = assignProjectToFolder({}, "a", "School");
    saveFolderAssignments(assignments);
    expect(loadFolderAssignments()).toEqual({ a: "School" });
  });

  it("persists only non-empty assignments (ungrouped stays implicit)", () => {
    saveFolderAssignments({ a: "School", b: "  ", c: "" });
    expect(loadFolderAssignments()).toEqual({ a: "School" });
  });

  it("uses the documented default key", () => {
    saveFolderAssignments({ a: "School" });
    expect(localStorage.getItem(FOLDER_ASSIGN_KEY)).toBe(JSON.stringify({ a: "School" }));
  });

  it("supports a caller-supplied namespaced key", () => {
    saveFolderAssignments({ a: "School" }, "meridian_demo.folders");
    expect(loadFolderAssignments("meridian_demo.folders")).toEqual({ a: "School" });
    expect(loadFolderAssignments()).toEqual({}); // default key untouched
  });

  it("returns {} for missing / corrupt / non-object storage", () => {
    expect(loadFolderAssignments()).toEqual({});
    localStorage.setItem(FOLDER_ASSIGN_KEY, "{not json");
    expect(loadFolderAssignments()).toEqual({});
    localStorage.setItem(FOLDER_ASSIGN_KEY, JSON.stringify(["a", "b"]));
    expect(loadFolderAssignments()).toEqual({});
  });

  it("survives a full assign → save → load → group cycle", () => {
    const projects = [P("a"), P("b")];
    saveFolderAssignments(assignProjectToFolder({}, "a", " School "));
    const reloaded = loadFolderAssignments();
    const groups = groupProjectsByFolder(projects, reloaded);
    expect(groups[0].folder).toBe("School");
    expect(groups[0].projects.map((p) => p.id)).toEqual(["a"]);
    expect(groups[1].label).toBe(UNGROUPED_LABEL);
  });
});

describe("collapsed-folder persistence", () => {
  it("round-trips the collapsed set", () => {
    saveCollapsedFolders(new Set(["School", ""]));
    const loaded = loadCollapsedFolders();
    expect(loaded.has("School")).toBe(true);
    expect(loaded.has("")).toBe(true);
    expect(loaded.has("Work")).toBe(false);
  });

  it("uses the documented default collapse key", () => {
    saveCollapsedFolders(new Set(["School"]));
    expect(localStorage.getItem(FOLDER_COLLAPSE_KEY)).toBe(JSON.stringify(["School"]));
  });

  it("returns an empty set for missing / corrupt storage", () => {
    expect(loadCollapsedFolders().size).toBe(0);
    localStorage.setItem(FOLDER_COLLAPSE_KEY, "{bad");
    expect(loadCollapsedFolders().size).toBe(0);
    localStorage.setItem(FOLDER_COLLAPSE_KEY, JSON.stringify({ not: "array" }));
    expect(loadCollapsedFolders().size).toBe(0);
  });

  it("toggles collapse state and persists both directions", () => {
    const set = new Set<string>();
    expect(toggleFolderCollapsed(set, "School")).toBe(true);
    expect(set.has("School")).toBe(true);
    expect(loadCollapsedFolders().has("School")).toBe(true);
    expect(toggleFolderCollapsed(set, "School")).toBe(false);
    expect(set.has("School")).toBe(false);
    expect(loadCollapsedFolders().has("School")).toBe(false);
  });
});
