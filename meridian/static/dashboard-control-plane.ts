// dashboard-control-plane.ts — client-side helpers for the CONTROL-PLANE-UI
// relationship / proposal / artifact-manifest browser (07f753b2), built from
// the accepted CONTROL-PLANE-UI-PLAN (workspace note 8e06a475, attached to
// sprint item ba61cc39).
//
// Standalone and dependency-light (only imports the already-unit-tested
// dashboard-subprojects.ts tree logic) so the data shaping, role gating, and
// pagination math here are unit-testable without a DOM or a fetch mock —
// mirrors dashboard-subprojects.ts's own stated design goal. The DOM/fetch
// wiring in dashboard-settings.ts is intentionally thin and calls straight
// into these functions rather than re-deriving any of this logic inline.

import { flattenHierarchy, type HierProject, type HierRow } from "./dashboard-subprojects";

// ---------------------------------------------------------------------------
// Role gating — mirrors meridian/roles.py's 4-role model exactly (owner /
// admin / member / viewer). The accepted plan's own roles finding: the
// spec's proposed "support"/"operator" roles do not exist anywhere in the
// codebase, so they are not invented here either.
// ---------------------------------------------------------------------------

export type ControlPlaneRole = "owner" | "admin" | "member" | "viewer" | null | undefined;

/** Every real role holds PERM_READ (meridian/roles.py) — this is a pure read
 * surface, so any recognized role may view it. A caller with no role at all
 * (not a member) is what the server's own PERM_READ check 403s; this mirrors
 * that so the dashboard doesn't render a panel behind a doomed fetch. */
export function canViewControlPlane(role: ControlPlaneRole): boolean {
  return role === "owner" || role === "admin" || role === "member" || role === "viewer";
}

/** 07f753b2's own instruction: "only allow EXISTING supported move/promote/
 * delete actions, gated by real role checks" — no new capability is invented
 * here. viewer holds only PERM_READ (meridian/roles.py), so it is the one
 * role that may look but never trigger a reused mutation action. */
export function canMutateControlPlane(role: ControlPlaneRole): boolean {
  return role === "owner" || role === "admin" || role === "member";
}

// ---------------------------------------------------------------------------
// Relationship explorer
// ---------------------------------------------------------------------------

export type RelationshipScope = "workspace" | "project";

export interface RelationshipProject extends HierProject {
  id: string;
  name?: string;
  status?: string;
  priority?: string;
  parent_project_id?: string | null;
  relationship_scope?: "project" | "subproject";
  created_at?: string;
}

export interface RelationshipRow {
  project: RelationshipProject;
  depth: number;
  /** Always-visible scope label — 07f753b2 requires "explicit scope
   * labels" on every row, never an implicit/inferred one. */
  scopeLabel: string;
}

/**
 * Build the nested (workspace -> project -> one-level subproject) rows for
 * the relationship explorer from the flat, already-scoped project list
 * ``GET /control-plane/relationships`` returns.
 *
 * Reuses ``flattenHierarchy`` (dashboard-subprojects.ts) — the SAME
 * already-unit-tested tree logic the sidebar and the "Make subproject of…"
 * picker already rely on — per the accepted plan's explicit reuse
 * recommendation, rather than re-implementing hierarchy math here.
 */
export function buildRelationshipRows(
  workspaceScope: RelationshipScope,
  projects: readonly RelationshipProject[],
): RelationshipRow[] {
  return flattenHierarchy(projects).map((row: HierRow<RelationshipProject>) => ({
    project: row.project,
    depth: row.depth,
    scopeLabel:
      row.depth === 0
        ? (workspaceScope === "workspace" ? "workspace" : "project (scoped)")
        : "subproject",
  }));
}

// ---------------------------------------------------------------------------
// Display-safety guard (defense in depth, second layer)
// ---------------------------------------------------------------------------

const _ABS_PATH_RE = /^(?:[A-Za-z]:[\\/]|\/|\\\\)/;

/**
 * Never render a string that LOOKS like a raw local filesystem path. The
 * server (meridian/routes/control_plane.py::_redact_local_paths) already
 * strips these before the response leaves the process — this is a SECOND,
 * client-side layer, not the only line of defense, matching the sprint
 * item's "never expose... arbitrary local paths" requirement with
 * defense-in-depth rather than a single trust boundary.
 */
export function displaySafe(value: unknown): string {
  if (typeof value !== "string") return value == null ? "" : String(value);
  if (_ABS_PATH_RE.test(value)) {
    const parts = value.replace(/\\/g, "/").split("/");
    return parts[parts.length - 1] || "(redacted path)";
  }
  return value;
}

// ---------------------------------------------------------------------------
// Artifact / manifest / provenance health classification
// ---------------------------------------------------------------------------

export type ArtifactRecordKind = "run_manifest" | "provenance" | "registry";

export interface ArtifactRecord {
  record_kind: ArtifactRecordKind;
  status?: string;
  lifecycle_state?: string;
  [k: string]: unknown;
}

export type ArtifactHealth = "ok" | "missing" | "stale" | "quarantined" | "unknown";

/**
 * Normalizes a run-manifest phase, a provenance status, or an
 * artifact_registry.py resolution state (RESOLVED / AMBIGUOUS /
 * HASH_MISMATCH / UNRESOLVED / ORPHANED — reused verbatim per the accepted
 * plan's IA recommendation, never re-invented) into one small display
 * vocabulary so the browser can render a single status pill regardless of
 * which of the three record kinds it is looking at.
 */
export function classifyArtifactHealth(record: ArtifactRecord): ArtifactHealth {
  const status = String(record.status || record.lifecycle_state || "").toLowerCase();
  if (["resolved", "complete", "verified", "active"].includes(status)) return "ok";
  if (["unresolved", "orphaned", "missing", "not_found"].includes(status)) return "missing";
  if (["hash_mismatch", "stale", "superseded"].includes(status)) return "stale";
  if (["ambiguous", "quarantined", "retired"].includes(status)) return "quarantined";
  return "unknown";
}

// ---------------------------------------------------------------------------
// Pagination — pure arithmetic, no DOM/fetch, so it is trivially testable
// ---------------------------------------------------------------------------

export interface PageInfo {
  limit: number;
  offset: number;
  hasMore: boolean;
}

/** Offset for a "next page" request, or ``null`` when there isn't one. */
export function nextOffset(page: PageInfo): number | null {
  return page.hasMore ? page.offset + page.limit : null;
}

/** Offset for a "previous page" request, or ``null`` at the first page. */
export function prevOffset(page: PageInfo): number | null {
  return page.offset > 0 ? Math.max(0, page.offset - page.limit) : null;
}

// ---------------------------------------------------------------------------
// Server-envelope helpers — the artifacts endpoint degrades to
// ``available: false`` (hosted mode pending the tunnel-proxy security fix,
// or the meridian-outputs extension not being installed) rather than
// erroring; these keep that distinction out of the rendering code.
// ---------------------------------------------------------------------------

export interface ArtifactsEnvelope {
  available: boolean;
  mode?: "hosted" | "self_hosted";
  reason?: string;
  items: ArtifactRecord[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

export function isArtifactsUnavailable(envelope: ArtifactsEnvelope | null | undefined): boolean {
  return !envelope || envelope.available !== true;
}
