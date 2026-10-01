// A pre-write safety net for the OT write path (write.js's applyFieldEdit
// will call this before every write -- Wave 2, not wired up here). Adam's
// own steer, 2026-09-25: "bare minimum functionality... robust and
// solid... most people use this via Overleaf" -- i.e. write.js's OT ops
// are the only thing that actually reaches Overleaf's real document, so a
// bug in range-locate.js's occurrence-counting (or anywhere else in the
// write path) has no local undo. This module writes a one-directional,
// timestamped local copy of a doc's full text BEFORE that write happens,
// purely so a human has a last-known-good copy on disk even in a
// genuinely-uncertain failure scenario -- these snapshots are never read
// back by this engine and never synced back to Overleaf.

import { homedir } from "node:os";
import { join } from "node:path";
import { mkdirSync, writeFileSync, existsSync, readdirSync } from "node:fs";

export const CONFIG_DIR = join(homedir(), ".meridian-latex");
export const SNAPSHOTS_DIR = join(CONFIG_DIR, "snapshots");

export interface SnapshotDocOptions {
  /** Overleaf's real project id (used as a path segment only -- format not validated) */
  projectId: string;
  /** Overleaf's real doc id (used as a path segment only -- format not validated) */
  docId: string;
  /** the doc's current lines array; the full text snapshotted is `lines.join("\n")` */
  lines: string[];
  /** overrides the base snapshots directory (defaults to the real `SNAPSHOTS_DIR`) -- for tests only, so the real `~/.meridian-latex` is never touched */
  dir?: string;
}

/**
 * Writes a full-text local snapshot of one doc, purely as a pre-write
 * safety net -- never read back by this engine, never synced back to
 * Overleaf. `lines` is exactly the shape
 * `OverleafProjectSession.joinDoc()` returns (see write.js's own
 * `docText()` for the same `lines.join("\n")` convention).
 *
 * Writes to `<dir>/<projectId>/<docId>/<timestamp>.tex`, where `timestamp`
 * is `new Date().toISOString()` with every `:` replaced by `-` (ISO
 * timestamps contain `:`, which is not a valid path character on
 * Windows). Directories are created recursively as needed.
 *
 * This is a small, honest function: it does not swallow a genuine I/O
 * error (a directory that truly can't be created, a disk that's truly
 * full) -- that throws naturally, same as calling mkdirSync/writeFileSync
 * directly would. It has no safe fallback for that case, so it doesn't
 * pretend to.
 *
 * @returns the absolute path written
 */
export function snapshotDoc({ projectId, docId, lines, dir = SNAPSHOTS_DIR }: SnapshotDocOptions): string {
  const docDir = join(dir, projectId, docId);
  mkdirSync(docDir, { recursive: true });
  const timestamp = new Date().toISOString().replace(/:/g, "-");
  const filePath = join(docDir, `${timestamp}.tex`);
  writeFileSync(filePath, lines.join("\n"));
  return filePath;
}

export interface ListSnapshotsOptions {
  projectId: string;
  docId: string;
  /** overrides the base snapshots directory (defaults to the real `SNAPSHOTS_DIR`) -- for tests only */
  dir?: string;
}

/**
 * The existing snapshot file paths for one project+doc, sorted newest
 * first -- e.g. for a future "show me recent snapshots" feature. Returns
 * `[]` if that project/doc has no snapshots directory yet (never throws
 * for that case). Deliberately simple: just `readdirSync` + sort by
 * filename, no metadata parsing -- the ISO-timestamp-with-dashes filenames
 * `snapshotDoc` writes already sort correctly as plain strings.
 *
 * @returns absolute paths, newest first
 */
export function listSnapshots({ projectId, docId, dir = SNAPSHOTS_DIR }: ListSnapshotsOptions): string[] {
  const docDir = join(dir, projectId, docId);
  if (!existsSync(docDir)) return [];
  return readdirSync(docDir)
    .sort()
    .reverse()
    .map((name) => join(docDir, name));
}
