// Local SQLite-backed storage: registered projects (auto-registered on their
// first /outline request) and node/whole-document claims.
//
// Deliberately self-contained -- see docs/write-back-spec.md's header note.
// No dependency on Meridian's own document-locking machinery; this is one
// local file (`engine/data/meridian-latex.db`, gitignored, created on first
// run) opened by one local Node process via `better-sqlite3`, the one new
// dependency this needs (nothing already in package.json covers embedded
// SQL).
//
// Schema is exactly docs/write-back-spec.md section 1. `store.js` owns the
// `projects` table; conflict-rule logic against the `claims` table lives in
// `claims.js`, which imports the constants/helpers below rather than
// duplicating the schema.

import Database from "better-sqlite3";
import { mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));

export const DEFAULT_DB_PATH = join(__dirname, "..", "data", "meridian-latex.db");

/** Reserved `claims.node_id` sentinel for a whole-document lease -- same
 * pattern as (and named in the same spirit as) meridian-docs' locks.py
 * DOCX_WHOLE_DOCUMENT_ELEMENT: one table handles both scoped and whole-doc
 * claims instead of a second schema for what is conflict-rule-wise a
 * special case of the same thing. */
export const WHOLE_DOCUMENT_LEASE_NODE_ID = "__meridian_latex_whole_document_lease__";

/** A claim is "live" if `released_at IS NULL AND claimed_at > now() -
 * CLAIM_TTL_MINUTES`. 30 minutes: a popup session is short-lived: this only
 * needs to survive someone reading a node before they edit it, not a
 * multi-hour absence -- see docs/write-back-spec.md section 1. */
export const CLAIM_TTL_MINUTES = 30;

const SCHEMA = `
CREATE TABLE IF NOT EXISTS projects (
  project_id   TEXT PRIMARY KEY,
  last_seen_at TEXT NOT NULL,
  last_outline TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
  id           TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL REFERENCES projects(project_id),
  node_id      TEXT NOT NULL,
  holder_token TEXT NOT NULL,
  claimed_at   TEXT NOT NULL,
  released_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_claims_project ON claims (project_id, node_id);

-- A durable, local audit trail of every edit actually dispatched through
-- applyEdits (see provenance.js) -- distinct from Overleaf's own version
-- history, and distinct from the claims table above (a claim is "who's
-- allowed to write right now"; a provenance row is "what was actually
-- written, and when"). This is the engine's OWN ledger, not a live
-- meridian-outputs record: the engine is a headless local Node process with
-- no MCP client of its own, so it cannot call meridian-outputs directly (see
-- provenance.js's header comment). synced_to_meridian_outputs lets a later
-- agent session pull unsynced rows via GET /provenance and push them into
-- meridian-outputs itself, then mark them synced -- a pull-based bridge
-- rather than the engine attempting to push.
CREATE TABLE IF NOT EXISTS provenance (
  id                         TEXT PRIMARY KEY,
  project_id                 TEXT NOT NULL REFERENCES projects(project_id),
  node_id                    TEXT NOT NULL,
  kind                       TEXT NOT NULL,
  field                      TEXT NOT NULL,
  old_value                  TEXT,
  new_value                  TEXT NOT NULL,
  holder_token               TEXT NOT NULL,
  recorded_at                TEXT NOT NULL,
  synced_to_meridian_outputs INTEGER NOT NULL DEFAULT 0,
  synced_at                  TEXT
);

CREATE INDEX IF NOT EXISTS idx_provenance_project ON provenance (project_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_provenance_unsynced ON provenance (synced_to_meridian_outputs);
`;

/**
 * Open (or create) the local SQLite store and ensure the schema exists.
 * Defaults to the real on-disk file; pass `:memory:` (tests do this) for a
 * throwaway, isolated database with no shared state between test runs.
 */
export function openStore(dbPath = DEFAULT_DB_PATH) {
  if (dbPath !== ":memory:") {
    mkdirSync(dirname(dbPath), { recursive: true });
  }
  const db = new Database(dbPath);
  db.exec(SCHEMA);
  return db;
}

/** Look up a registered project's row, or `null` if it's never been seen. */
export function getProject(db, projectId) {
  return db.prepare("SELECT * FROM projects WHERE project_id = ?").get(projectId) || null;
}

/**
 * Insert-or-update a project's `last_outline` (and `last_seen_at`). This is
 * the ONLY registration path for a project -- there is no manual add/remove
 * step anywhere (docs/write-back-spec.md section 1 and section 5): a
 * project_id becomes known to the store purely by showing up in a /outline
 * request.
 */
export function upsertProject(db, projectId, outlineNodes) {
  const now = new Date().toISOString();
  db.prepare(
    `INSERT INTO projects (project_id, last_seen_at, last_outline)
     VALUES (@project_id, @last_seen_at, @last_outline)
     ON CONFLICT(project_id) DO UPDATE SET
       last_seen_at = excluded.last_seen_at,
       last_outline = excluded.last_outline`
  ).run({ project_id: projectId, last_seen_at: now, last_outline: JSON.stringify(outlineNodes) });
}

/**
 * Register a project row if it doesn't exist yet, WITHOUT touching
 * last_seen_at/last_outline if it already does. Used by claims.js so a
 * /claim or /lease call that arrives before any /outline call for that
 * project_id (an edge case the popup flow never actually produces, since
 * outline always runs first -- see write-back-spec.md section 4 -- but one
 * a direct API caller could still hit) still satisfies the `claims.project_id
 * REFERENCES projects(project_id)` relationship rather than leaving an
 * orphaned claims row, and so a later GET /claims for that project_id works
 * normally either way.
 */
export function ensureProjectRow(db, projectId) {
  const now = new Date().toISOString();
  db.prepare(
    `INSERT INTO projects (project_id, last_seen_at, last_outline)
     VALUES (?, ?, '[]')
     ON CONFLICT(project_id) DO NOTHING`
  ).run(projectId, now);
}
