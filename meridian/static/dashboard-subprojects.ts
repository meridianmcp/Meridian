// dashboard-subprojects.ts — client-side helpers for the one-level-deep
// subproject hierarchy (0fed6a42).
//
// The DB has projects.parent_project_id (set via the set_parent_project MCP tool
// / the POST /projects/{id}/parent REST route) and enforces a strict *one level
// deep* invariant server-side (see db.set_parent_project): a parent must itself be
// top-level, a project cannot be its own parent, and a project that already has
// subprojects cannot become a subproject. These helpers mirror that invariant on
// the client so the "Make subproject of…" picker only offers legal parents and so
// the sidebar can render subprojects nested under their parent.
//
// This module is intentionally standalone (no dashboard imports) so the logic is
// unit-testable in isolation and carries zero Meridian coupling. The dashboard is
// one consumer via loadProjects() / the project kebab menu in dashboard.ts.

/** Minimal project shape these helpers need (matches the /projects rows). */
export interface HierProject {
  id: string;
  name?: string;
  /** Parent project id, or null/undefined for a top-level project. */
  parent_project_id?: string | null;
  [k: string]: unknown;
}

/** A project decorated with its render depth in the sidebar tree. */
export interface HierRow<P extends HierProject = HierProject> {
  project: P;
  /** 0 for a top-level project, 1 for a subproject. */
  depth: number;
}

/** Normalize a possibly-empty parent id to a real id or null. */
function normParent(pid: unknown): string | null {
  if (pid == null) return null;
  const s = String(pid).trim();
  return s ? s : null;
}

/**
 * Flatten a list of projects into render rows where every subproject sits
 * immediately after its parent and carries depth=1 (parent is depth=0).
 *
 * The incoming order of the *top-level* projects is preserved; a parent's
 * children follow it in incoming order. Because the DB guarantees a one-level
 * hierarchy, a project whose parent is one of these projects is a child; anything
 * else — a top-level project, a project whose parent is not in the visible set
 * (scoped out / deleted), or a would-be grandchild — is rendered at the top level
 * so nothing is ever dropped.
 */
export function flattenHierarchy<P extends HierProject>(
  projects: readonly P[],
): HierRow<P>[] {
  const byId = new Map<string, P>();
  for (const p of projects) byId.set(p.id, p);

  // A project is a *real* child only when its parent is present AND that parent
  // is itself top-level (parent has no parent). This mirrors the server's
  // one-level-deep rule and keeps orphans/grandchildren at the top level.
  const childrenOf = new Map<string, P[]>();
  const topLevel: P[] = [];
  for (const p of projects) {
    const parentId = normParent(p.parent_project_id);
    const parent = parentId ? byId.get(parentId) : undefined;
    if (parent && !normParent(parent.parent_project_id)) {
      let bucket = childrenOf.get(parentId!);
      if (!bucket) {
        bucket = [];
        childrenOf.set(parentId!, bucket);
      }
      bucket.push(p);
    } else {
      topLevel.push(p);
    }
  }

  const rows: HierRow<P>[] = [];
  for (const p of topLevel) {
    rows.push({ project: p, depth: 0 });
    const kids = childrenOf.get(p.id);
    if (kids) {
      for (const kid of kids) rows.push({ project: kid, depth: 1 });
    }
  }
  return rows;
}

/** True when `projectId` currently has at least one subproject in the set. */
export function hasSubprojects<P extends HierProject>(
  projects: readonly P[],
  projectId: string,
): boolean {
  return projects.some((p) => normParent(p.parent_project_id) === projectId);
}

/**
 * Candidate parents for the "Make subproject of…" picker for `projectId`.
 *
 * Enforces the same guards as db.set_parent_project so the UI never offers an
 * illegal move (the server would 400 anyway):
 *   - a project cannot be its own parent,
 *   - the parent must be top-level (subprojects are one level deep),
 *   - a project that already HAS subprojects cannot become a subproject,
 *   - a project's *current* parent is excluded (no-op move).
 *
 * Returns [] when `projectId` itself has children (it is ineligible to move).
 * Preserves incoming project order.
 */
export function eligibleParents<P extends HierProject>(
  projects: readonly P[],
  projectId: string,
): P[] {
  // A project that already parents others cannot itself become a subproject.
  if (hasSubprojects(projects, projectId)) return [];
  const self = projects.find((p) => p.id === projectId);
  const currentParent = self ? normParent(self.parent_project_id) : null;
  return projects.filter((p) => {
    if (p.id === projectId) return false; // not self
    if (normParent(p.parent_project_id)) return false; // parent must be top-level
    if (p.id === currentParent) return false; // no-op (already the parent)
    return true;
  });
}

/** Reason a subproject move is disallowed, or null when it is allowed. */
export type MoveBlockReason =
  | "self"
  | "has-children"
  | "parent-not-toplevel"
  | "parent-missing"
  | "no-op";

/**
 * Validate a proposed "make `projectId` a child of `parentId`" move against the
 * one-level-deep invariant. Returns null when the move is legal, otherwise the
 * blocking reason. `parentId=null` (detach → top level) is always legal for a
 * known project. Mirrors db.set_parent_project's ValueError conditions so the UI
 * can refuse early with a precise message.
 */
export function validateSubprojectMove<P extends HierProject>(
  projects: readonly P[],
  projectId: string,
  parentId: string | null,
): MoveBlockReason | null {
  if (parentId == null) return null; // detach is always allowed
  if (parentId === projectId) return "self";
  if (hasSubprojects(projects, projectId)) return "has-children";
  const parent = projects.find((p) => p.id === parentId);
  if (!parent) return "parent-missing";
  if (normParent(parent.parent_project_id)) return "parent-not-toplevel";
  const self = projects.find((p) => p.id === projectId);
  if (self && normParent(self.parent_project_id) === parentId) return "no-op";
  return null;
}
