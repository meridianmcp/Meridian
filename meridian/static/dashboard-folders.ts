// dashboard-folders.ts — client-side "folders/spheres" for the sidebar project list (d6b7da48).
//
// Pure UI grouping across otherwise-unrelated top-level projects. The DB has
// project.status (active|parked|archived) + project.priority but NO folder
// grouping, and this feature deliberately adds NONE: folder membership and
// collapse state live entirely in localStorage. No backend / schema change.
//
// This module is intentionally standalone (no dashboard imports) so the grouping
// helpers are unit-testable in isolation and carry zero Meridian coupling. The
// dashboard is one consumer via loadProjects() in dashboard.ts.

/** localStorage keys. Callers may namespace via a prefix (demo vs. real). */
export const FOLDER_ASSIGN_KEY = "meridian.projectFolders";
export const FOLDER_COLLAPSE_KEY = "meridian.projectFolderCollapsed";

/** Sentinel folder label for projects with no folder assignment. */
export const UNGROUPED_LABEL = "Ungrouped";

/** Minimal project shape the grouping needs (matches the /projects rows). */
export interface FolderProject {
  id: string;
  name?: string;
  [k: string]: unknown;
}

/** Map of projectId -> folder name. A missing/empty value means "ungrouped". */
export type FolderAssignments = Record<string, string>;

/** One rendered group: a named folder (or the ungrouped catch-all) + members. */
export interface ProjectGroup<P extends FolderProject = FolderProject> {
  /** Folder name, or null for the ungrouped catch-all. */
  folder: string | null;
  /** Display label — the folder name, or UNGROUPED_LABEL for the catch-all. */
  label: string;
  /** Stable key usable in DOM ids / localStorage (folder name or ""). */
  key: string;
  projects: P[];
}

/** Trim + collapse whitespace; empty/whitespace-only becomes "" (= ungrouped). */
export function normalizeFolderName(name: string | null | undefined): string {
  if (name == null) return "";
  return String(name).trim().replace(/\s+/g, " ");
}

/**
 * Read folder assignments from localStorage. Always returns a plain object,
 * even when storage is empty, corrupt, or unavailable (private mode / SSR).
 */
export function loadFolderAssignments(
  key: string = FOLDER_ASSIGN_KEY,
  storage: Storage | undefined = safeStorage(),
): FolderAssignments {
  if (!storage) return {};
  try {
    const raw = storage.getItem(key);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    const out: FolderAssignments = {};
    for (const [pid, folder] of Object.entries(parsed as Record<string, unknown>)) {
      const norm = normalizeFolderName(typeof folder === "string" ? folder : "");
      if (norm) out[pid] = norm; // drop ungrouped entries — absence == ungrouped
    }
    return out;
  } catch {
    return {};
  }
}

/** Persist folder assignments. Silently no-ops if storage is unavailable. */
export function saveFolderAssignments(
  assignments: FolderAssignments,
  key: string = FOLDER_ASSIGN_KEY,
  storage: Storage | undefined = safeStorage(),
): void {
  if (!storage) return;
  try {
    // Only persist non-empty assignments so the ungrouped catch-all stays implicit.
    const clean: FolderAssignments = {};
    for (const [pid, folder] of Object.entries(assignments)) {
      const norm = normalizeFolderName(folder);
      if (norm) clean[pid] = norm;
    }
    storage.setItem(key, JSON.stringify(clean));
  } catch {
    /* private mode / quota — folders are best-effort UI state */
  }
}

/**
 * Return a NEW assignments map with `projectId` moved to `folder`. Passing an
 * empty / whitespace-only / null folder removes the assignment (→ ungrouped).
 * Pure — does not mutate the input.
 */
export function assignProjectToFolder(
  assignments: FolderAssignments,
  projectId: string,
  folder: string | null | undefined,
): FolderAssignments {
  const next: FolderAssignments = { ...assignments };
  const norm = normalizeFolderName(folder);
  if (norm) next[projectId] = norm;
  else delete next[projectId];
  return next;
}

/**
 * Group projects by their folder assignment, preserving the incoming project
 * order within each group. Named folders come first (in first-seen order),
 * then the ungrouped catch-all last. The catch-all is only emitted when it has
 * members, but named folders are always emitted in project order.
 */
export function groupProjectsByFolder<P extends FolderProject>(
  projects: readonly P[],
  assignments: FolderAssignments,
): ProjectGroup<P>[] {
  const named = new Map<string, P[]>();
  const ungrouped: P[] = [];

  for (const p of projects) {
    const folder = normalizeFolderName(assignments[p.id]);
    if (!folder) {
      ungrouped.push(p);
      continue;
    }
    let bucket = named.get(folder);
    if (!bucket) {
      bucket = [];
      named.set(folder, bucket);
    }
    bucket.push(p);
  }

  const groups: ProjectGroup<P>[] = [];
  for (const [folder, members] of named) {
    groups.push({ folder, label: folder, key: folder, projects: members });
  }
  if (ungrouped.length) {
    groups.push({ folder: null, label: UNGROUPED_LABEL, key: "", projects: ungrouped });
  }
  return groups;
}

/** Distinct folder names currently in use, in first-seen (project) order. */
export function knownFolderNames<P extends FolderProject>(
  projects: readonly P[],
  assignments: FolderAssignments,
): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const p of projects) {
    const folder = normalizeFolderName(assignments[p.id]);
    if (folder && !seen.has(folder)) {
      seen.add(folder);
      out.push(folder);
    }
  }
  return out;
}

/** Read the set of collapsed folder keys from localStorage. */
export function loadCollapsedFolders(
  key: string = FOLDER_COLLAPSE_KEY,
  storage: Storage | undefined = safeStorage(),
): Set<string> {
  if (!storage) return new Set();
  try {
    const raw = storage.getItem(key);
    if (!raw) return new Set();
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.filter((x): x is string => typeof x === "string"));
  } catch {
    return new Set();
  }
}

/** Persist the collapsed-folder set. Silently no-ops if storage is unavailable. */
export function saveCollapsedFolders(
  collapsed: Set<string>,
  key: string = FOLDER_COLLAPSE_KEY,
  storage: Storage | undefined = safeStorage(),
): void {
  if (!storage) return;
  try {
    storage.setItem(key, JSON.stringify([...collapsed]));
  } catch {
    /* best-effort */
  }
}

/** Toggle a folder's collapsed state in-place and persist. Returns new state. */
export function toggleFolderCollapsed(
  collapsed: Set<string>,
  folderKey: string,
  key: string = FOLDER_COLLAPSE_KEY,
  storage: Storage | undefined = safeStorage(),
): boolean {
  let nowCollapsed: boolean;
  if (collapsed.has(folderKey)) {
    collapsed.delete(folderKey);
    nowCollapsed = false;
  } else {
    collapsed.add(folderKey);
    nowCollapsed = true;
  }
  saveCollapsedFolders(collapsed, key, storage);
  return nowCollapsed;
}

/** Resolve a usable Storage, or undefined when localStorage is inaccessible. */
function safeStorage(): Storage | undefined {
  try {
    return typeof localStorage !== "undefined" ? localStorage : undefined;
  } catch {
    return undefined; // access to localStorage can throw in sandboxed frames
  }
}
