// A durable, local audit trail of edits actually dispatched through
// applyEdits (extension/injected.js) -- item 6160d667's "meridian-outputs
// provenance wiring" piece.
//
// Why this lives entirely in the engine's own SQLite store instead of
// calling meridian-outputs directly: the engine (server.js) is a headless
// local Node process, not an agent session -- it has no MCP client and
// cannot call register_output_paths/annotate_outputs itself. So this module
// is deliberately NOT a meridian-outputs integration; it's the durable local
// buffer a real integration needs. recordEdit() is called synchronously at
// write time (from popup.js's applyBatch(), right after a write is dispatched
// and readback-verified) so nothing is lost even if no agent session is
// running to sync it. listProvenance()/markSynced() exist so a LATER agent
// session -- one that genuinely does have meridian-outputs MCP access -- can
// pull unsynced rows, push them into meridian-outputs itself, and mark them
// synced. That sync step is intentionally not built here: it belongs in an
// agent session, not this local server.
//
// Never-raises convention, matching claims.js: every exported function
// returns a plain result object instead of throwing.

import { randomUUID } from "node:crypto";
import { ensureProjectRow } from "./store.js";

function nowIso() {
  return new Date().toISOString();
}

/**
 * Record one applied edit. `old_value` may be `null` (a field that had no
 * prior value is not expected today -- every editable field always has SOME
 * current text per popup.js's EDITABLE_FIELDS -- but this is not enforced
 * here, since a future editable-field kind might genuinely have none).
 * Returns `{recorded: true, id}` or `{recorded: false, reason}`. Never throws.
 */
export function recordEdit(db, { project_id, node_id, kind, field, old_value = null, new_value, holder_token }) {
  try {
    if (!project_id || !node_id || !kind || !field || !holder_token) {
      return {
        recorded: false,
        reason: "project_id, node_id, kind, field, and holder_token are all required",
      };
    }
    if (typeof new_value !== "string") {
      return { recorded: false, reason: "new_value must be a string" };
    }
    // Mirrors claims.js's insertClaimRow: better-sqlite3 enforces
    // PRAGMA foreign_keys=ON by default (confirmed live: db.pragma(
    // "foreign_keys", {simple:true}) === 1 with no explicit opt-in
    // anywhere in store.js), so provenance.project_id's REFERENCES
    // projects(project_id) really is enforced -- inserting against an
    // unregistered project_id throws SQLITE_CONSTRAINT_FOREIGNKEY without
    // this. In practice an edit is never dispatched before /outline has
    // already registered the project, but this costs nothing and avoids
    // relying on that ordering, matching store.js's own documented reason
    // for ensureProjectRow existing at all.
    ensureProjectRow(db, project_id);
    const id = randomUUID();
    db.prepare(
      `INSERT INTO provenance
         (id, project_id, node_id, kind, field, old_value, new_value, holder_token, recorded_at, synced_to_meridian_outputs, synced_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)`
    ).run(id, project_id, node_id, kind, field, old_value, new_value, holder_token, nowIso());
    return { recorded: true, id };
  } catch (err) {
    return { recorded: false, reason: `internal error: ${(err && err.message) || err}` };
  }
}

/**
 * List provenance rows for a project, newest first. `unsynced_only: true`
 * restricts to rows a meridian-outputs sync hasn't consumed yet -- the query
 * an agent session's sync step should actually run. Returns `[]` (never
 * throws) on any internal error or missing project_id, matching
 * getLiveClaims' own "nothing here can fail more sharply than an empty
 * result" convention.
 */
export function listProvenance(db, { project_id, unsynced_only = false } = {}) {
  try {
    if (!project_id) return [];
    const sql = unsynced_only
      ? "SELECT * FROM provenance WHERE project_id = ? AND synced_to_meridian_outputs = 0 ORDER BY recorded_at DESC"
      : "SELECT * FROM provenance WHERE project_id = ? ORDER BY recorded_at DESC";
    return db.prepare(sql).all(project_id);
  } catch {
    return [];
  }
}

/**
 * Mark a set of provenance rows as synced (after an agent session has
 * actually pushed them into meridian-outputs). Idempotent -- re-marking an
 * already-synced id changes nothing extra, just refreshes synced_at.
 * Returns `{marked: <count>}`. Never throws.
 */
export function markSynced(db, ids) {
  try {
    if (!Array.isArray(ids) || ids.length === 0) return { marked: 0 };
    const now = nowIso();
    const stmt = db.prepare("UPDATE provenance SET synced_to_meridian_outputs = 1, synced_at = ? WHERE id = ?");
    let marked = 0;
    for (const id of ids) {
      const result = stmt.run(now, id);
      marked += result.changes;
    }
    return { marked };
  } catch (err) {
    return { marked: 0, reason: `internal error: ${(err && err.message) || err}` };
  }
}
