// Unit tests for the CONTROL-PLANE-UI client-side helpers (07f753b2).
import { describe, expect, it } from "vitest";
import {
  buildRelationshipRows,
  canMutateControlPlane,
  canViewControlPlane,
  classifyArtifactHealth,
  displaySafe,
  isArtifactsUnavailable,
  nextOffset,
  prevOffset,
  type RelationshipProject,
} from "./dashboard-control-plane";

const P = (id: string, parent: string | null = null, name = id): RelationshipProject => ({
  id,
  name,
  parent_project_id: parent,
});

// ---------------------------------------------------------------------------
// Role matrix
// ---------------------------------------------------------------------------

describe("canViewControlPlane", () => {
  it("allows all four real roles to view", () => {
    expect(canViewControlPlane("owner")).toBe(true);
    expect(canViewControlPlane("admin")).toBe(true);
    expect(canViewControlPlane("member")).toBe(true);
    expect(canViewControlPlane("viewer")).toBe(true);
  });

  it("denies a non-member (no role at all)", () => {
    expect(canViewControlPlane(null)).toBe(false);
    expect(canViewControlPlane(undefined)).toBe(false);
  });

  it("denies an unrecognized role string", () => {
    // @ts-expect-error — deliberately passing a role outside the real 4-role model
    expect(canViewControlPlane("support")).toBe(false);
    // @ts-expect-error
    expect(canViewControlPlane("operator")).toBe(false);
  });
});

describe("canMutateControlPlane", () => {
  it("allows owner/admin/member", () => {
    expect(canMutateControlPlane("owner")).toBe(true);
    expect(canMutateControlPlane("admin")).toBe(true);
    expect(canMutateControlPlane("member")).toBe(true);
  });

  it("denies viewer (read-only role)", () => {
    expect(canMutateControlPlane("viewer")).toBe(false);
  });

  it("denies a non-member", () => {
    expect(canMutateControlPlane(null)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Relationship explorer — parent/child nesting + scope labels
// ---------------------------------------------------------------------------

describe("buildRelationshipRows", () => {
  it("labels top-level projects 'workspace' scope for an unscoped (owner) caller", () => {
    const rows = buildRelationshipRows("workspace", [P("a"), P("b")]);
    expect(rows.map((r) => [r.project.id, r.depth, r.scopeLabel])).toEqual([
      ["a", 0, "workspace"],
      ["b", 0, "workspace"],
    ]);
  });

  it("labels top-level projects 'project (scoped)' for a project-scoped caller", () => {
    const rows = buildRelationshipRows("project", [P("a")]);
    expect(rows[0].scopeLabel).toBe("project (scoped)");
  });

  it("nests a subproject under its parent at depth 1 with a 'subproject' label", () => {
    const rows = buildRelationshipRows("workspace", [P("parent"), P("child", "parent")]);
    expect(rows.map((r) => [r.project.id, r.depth, r.scopeLabel])).toEqual([
      ["parent", 0, "workspace"],
      ["child", 1, "subproject"],
    ]);
  });

  it("keeps every row's own project data intact (no field is dropped by nesting)", () => {
    const proj: RelationshipProject = {
      id: "p1",
      name: "Paper",
      status: "active",
      priority: "P1",
      parent_project_id: null,
      relationship_scope: "project",
      created_at: "2026-01-01",
    };
    const rows = buildRelationshipRows("workspace", [proj]);
    expect(rows[0].project).toEqual(proj);
  });
});

// ---------------------------------------------------------------------------
// Display-safety guard
// ---------------------------------------------------------------------------

describe("displaySafe", () => {
  it("passes an ordinary string through unchanged", () => {
    expect(displaySafe("run-42")).toBe("run-42");
  });

  it("reduces a Windows absolute path to its basename", () => {
    expect(displaySafe("C:\\Users\\alice\\project\\outputs\\fig1.png")).toBe("fig1.png");
  });

  it("reduces a POSIX absolute path to its basename", () => {
    expect(displaySafe("/home/bob/project/run.json")).toBe("run.json");
  });

  it("reduces a UNC path to its basename", () => {
    expect(displaySafe("\\\\server\\share\\secret.txt")).toBe("secret.txt");
  });

  it("never crashes on null/undefined/non-string values", () => {
    expect(displaySafe(null)).toBe("");
    expect(displaySafe(undefined)).toBe("");
    expect(displaySafe(42)).toBe("42");
  });

  it("leaves a relative path untouched", () => {
    expect(displaySafe("outputs/fig1.png")).toBe("outputs/fig1.png");
  });
});

// ---------------------------------------------------------------------------
// Artifact health classification
// ---------------------------------------------------------------------------

describe("classifyArtifactHealth", () => {
  it("maps artifact_registry.py's RESOLVED/complete/verified/active to ok", () => {
    expect(classifyArtifactHealth({ record_kind: "registry", status: "resolved" })).toBe("ok");
    expect(classifyArtifactHealth({ record_kind: "run_manifest", status: "complete" })).toBe("ok");
  });

  it("maps UNRESOLVED/ORPHANED/missing to missing", () => {
    expect(classifyArtifactHealth({ record_kind: "registry", status: "unresolved" })).toBe("missing");
    expect(classifyArtifactHealth({ record_kind: "registry", status: "orphaned" })).toBe("missing");
  });

  it("maps HASH_MISMATCH/stale/superseded to stale", () => {
    expect(classifyArtifactHealth({ record_kind: "registry", status: "hash_mismatch" })).toBe("stale");
  });

  it("maps AMBIGUOUS/quarantined/retired to quarantined", () => {
    expect(classifyArtifactHealth({ record_kind: "registry", status: "ambiguous" })).toBe("quarantined");
  });

  it("falls back to unknown for an unrecognized or missing status", () => {
    expect(classifyArtifactHealth({ record_kind: "provenance" })).toBe("unknown");
    expect(classifyArtifactHealth({ record_kind: "provenance", status: "???" })).toBe("unknown");
  });

  it("reads lifecycle_state when status is absent (run_manifest/registry shape)", () => {
    expect(classifyArtifactHealth({ record_kind: "registry", lifecycle_state: "active" })).toBe("ok");
  });
});

// ---------------------------------------------------------------------------
// Pagination arithmetic
// ---------------------------------------------------------------------------

describe("nextOffset / prevOffset", () => {
  it("returns null for next when there is no more data", () => {
    expect(nextOffset({ limit: 20, offset: 0, hasMore: false })).toBeNull();
  });

  it("advances by limit when there is more data", () => {
    expect(nextOffset({ limit: 20, offset: 20, hasMore: true })).toBe(40);
  });

  it("returns null for prev at the first page", () => {
    expect(prevOffset({ limit: 20, offset: 0, hasMore: true })).toBeNull();
  });

  it("steps back by limit, never below zero", () => {
    expect(prevOffset({ limit: 20, offset: 20, hasMore: true })).toBe(0);
    expect(prevOffset({ limit: 20, offset: 10, hasMore: true })).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// Hosted/unavailable envelope guard
// ---------------------------------------------------------------------------

describe("isArtifactsUnavailable", () => {
  it("is unavailable when the envelope is missing entirely", () => {
    expect(isArtifactsUnavailable(null)).toBe(true);
    expect(isArtifactsUnavailable(undefined)).toBe(true);
  });

  it("is unavailable when available is false (hosted mode / extension missing)", () => {
    expect(
      isArtifactsUnavailable({
        available: false, mode: "hosted", items: [], total: 0, limit: 50, offset: 0, has_more: false,
      }),
    ).toBe(true);
  });

  it("is available when the server says so", () => {
    expect(
      isArtifactsUnavailable({
        available: true, mode: "self_hosted", items: [], total: 0, limit: 50, offset: 0, has_more: false,
      }),
    ).toBe(false);
  });
});
