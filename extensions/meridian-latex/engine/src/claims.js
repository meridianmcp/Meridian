// Conflict rules for node/whole-document claims -- docs/write-back-spec.md
// section 2, mirroring meridian-docs' locks.py Model B *conceptually*
// (proven, already-debugged design). The implementation below is
// independent: no import from, or runtime dependency on, locks.py or any
// other Meridian server code -- see write-back-spec.md's header note on why
// (meridian-latex is a standalone repo used across multiple unrelated paper
// projects, none of which should need Meridian installed for this to work).
//
// Never-raises convention, matching this codebase's `claim_docx_region`-style
// tools: every exported function here returns a plain result object
// (`{claimed: false, reason, ...}` etc.) instead of throwing, including for
// unexpected internal/DB errors -- a caller (the HTTP layer) never needs a
// try/catch around these.

import { randomUUID } from "node:crypto";
import { WHOLE_DOCUMENT_LEASE_NODE_ID, CLAIM_TTL_MINUTES, ensureProjectRow } from "./store.js";

function nowIso() {
  return new Date().toISOString();
}

/**
 * Real bug found 2026-09-18 via independent code review: every validation
 * check below used to be a bare truthiness check (`!project_id`), which
 * only rejects falsy values (`""`, `null`, `undefined`, `0`) -- a JSON
 * request body can carry a NUMBER for any of these fields (a client bug, or
 * a project id that happens to look numeric), which is truthy and would
 * sail past the check, then get bound into a better-sqlite3 TEXT column via
 * SQLite's own numeric-to-TEXT affinity conversion. Not a SQL-injection risk
 * (still a prepared-statement bind, not string concatenation), but a real
 * correctness one: a huge numeric holder_token can silently lose precision
 * in JSON.parse before it even reaches here, and a numeric vs. string
 * node_id could compare inconsistently depending on which callers use the
 * request-derived value versus a str `node_id` typed into a follow-up
 * request. Reject anything that isn't a real, non-empty string outright.
 */
function isNonEmptyString(value) {
  return typeof value === "string" && value.length > 0;
}

function isLive(row, nowMs) {
  if (!row || row.released_at) return false;
  const claimedMs = Date.parse(row.claimed_at);
  if (Number.isNaN(claimedMs)) return false;
  return nowMs - claimedMs < CLAIM_TTL_MINUTES * 60 * 1000;
}

/** The live claim (if any) for one exact (project_id, node_id) pair. Several
 * non-live rows can share this pair (soft-released history, or rows that
 * simply timed out without ever being explicitly released -- see store.js's
 * liveness note) so this filters by `isLive`, not just "most recent row". */
function findLiveClaim(db, projectId, nodeId, nowMs) {
  const rows = db
    .prepare(
      "SELECT * FROM claims WHERE project_id = ? AND node_id = ? AND released_at IS NULL ORDER BY claimed_at DESC"
    )
    .all(projectId, nodeId);
  for (const row of rows) {
    if (isLive(row, nowMs)) return row;
  }
  return null;
}

/** Any live SCOPED claim (never the whole-doc sentinel) on this project held
 * by someone OTHER than `excludeHolder`. Used by leaseWholeDocument's rule 2
 * (see its own comment) -- the whole-doc-vs-whole-doc case is already
 * handled separately by leaseWholeDocument's own `existing` check above it,
 * so this only needs to look at scoped rows. */
function findAnyOtherLiveScopedClaim(db, projectId, excludeHolder, nowMs) {
  const rows = db
    .prepare(
      "SELECT * FROM claims WHERE project_id = ? AND holder_token != ? AND node_id != ? AND released_at IS NULL ORDER BY claimed_at DESC"
    )
    .all(projectId, excludeHolder, WHOLE_DOCUMENT_LEASE_NODE_ID);
  for (const row of rows) {
    if (isLive(row, nowMs)) return row;
  }
  return null;
}

function insertClaimRow(db, { project_id, node_id, holder_token }) {
  ensureProjectRow(db, project_id);
  db.prepare(
    "INSERT INTO claims (id, project_id, node_id, holder_token, claimed_at, released_at) VALUES (?, ?, ?, ?, ?, NULL)"
  ).run(randomUUID(), project_id, node_id, holder_token, nowIso());
}

// Rule 5's two triggers: a `-dupN` disambiguation suffix (outline.js's
// disambiguateIds, for a genuine hash collision -- most commonly two
// occurrences of the same repeated citation key) and a `:pos:` positional
// fallback (outline.js's fingerprint(), for a table/figure/equation with
// neither caption nor label). A single id can carry both (a colliding
// positional id also gets disambiguated), so both checks run independently
// and their reasons combine rather than short-circuiting on the first hit.
const DUP_SUFFIX_RE = /-dup\d+$/;
const POS_FALLBACK_RE = /:pos:/;

/**
 * Rule 5: identity_confidence for a node_id, or `null` when the id carries
 * neither caveat (the common case). This never blocks a claim -- it's
 * honesty about what the id can and can't guarantee, surfaced to the caller
 * rather than silently trusted (write-back-spec.md section 2, rule 5).
 */
export function identityConfidenceFor(nodeId) {
  const isPos = POS_FALLBACK_RE.test(nodeId);
  const isDup = DUP_SUFFIX_RE.test(nodeId);
  if (!isPos && !isDup) return null;

  const reasons = [];
  if (isPos) {
    reasons.push(
      "this node has no caption or label to fingerprint, so its id is a position-derived fallback that can drift on unrelated upstream edits"
    );
  }
  if (isDup) {
    reasons.push(
      "this id was disambiguated from a hash collision with another node sharing the exact same content (most commonly a repeated citation key) -- the specific occurrence cannot be distinguished beyond document order"
    );
  }
  return { identity_confidence: "low", identity_confidence_reason: reasons.join("; ") };
}

/**
 * POST /claim's logic. Applies rules 1, 2, 4, 5 (rule 3 -- different
 * node_ids coexist freely -- needs no code: this function only ever looks
 * at rows for the exact `node_id` requested, plus the whole-doc sentinel).
 *
 * Returns `{claimed: true, identity_confidence?, identity_confidence_reason?}`
 * or `{claimed: false, reason, holder_token_of_conflict?}`. Never throws.
 */
export function claimNode(db, { project_id, node_id, holder_token }) {
  try {
    if (!isNonEmptyString(project_id) || !isNonEmptyString(node_id) || !isNonEmptyString(holder_token)) {
      return { claimed: false, reason: "project_id, node_id, and holder_token are all required" };
    }
    if (node_id === WHOLE_DOCUMENT_LEASE_NODE_ID) {
      // A scoped /claim aimed at the reserved sentinel is really a /lease
      // request that came in the wrong door -- route it there rather than
      // let it fall through into the ordinary scoped-claim path below,
      // which isn't rule-1-aware about ITSELF being the whole-doc lease.
      // Translated back into /claim's own `{claimed: ...}` response shape
      // so callers of claimNode() never have to branch on which node_id
      // they happened to pass.
      const leaseResult = leaseWholeDocument(db, { project_id, holder_token });
      return leaseResult.leased
        ? { claimed: true }
        : {
            claimed: false,
            reason: leaseResult.reason,
            ...(leaseResult.holder_token_of_conflict
              ? { holder_token_of_conflict: leaseResult.holder_token_of_conflict }
              : {}),
          };
    }

    const nowMs = Date.now();

    // Rule 1: a live whole-document lease by ANY OTHER holder blocks every
    // new claim (scoped or whole-doc) on this project.
    const liveLease = findLiveClaim(db, project_id, WHOLE_DOCUMENT_LEASE_NODE_ID, nowMs);
    if (liveLease && liveLease.holder_token !== holder_token) {
      return {
        claimed: false,
        reason: "a whole-document lease is held by another holder",
        holder_token_of_conflict: liveLease.holder_token,
      };
    }

    const confidence = identityConfidenceFor(node_id) || {};

    // Rules 2 & 4: a live scoped claim on this exact node_id.
    const existing = findLiveClaim(db, project_id, node_id, nowMs);
    if (existing) {
      if (existing.holder_token !== holder_token) {
        return {
          claimed: false,
          reason: "node already claimed by another holder",
          holder_token_of_conflict: existing.holder_token,
        };
      }
      // Rule 4: re-claiming your own already-held claim is idempotent --
      // refresh claimed_at rather than inserting a second row for the same
      // (project_id, node_id, holder_token).
      db.prepare("UPDATE claims SET claimed_at = ? WHERE id = ?").run(nowIso(), existing.id);
      return { claimed: true, ...confidence };
    }

    // Rule 3 (implicit): a different node_id never conflicts with anything
    // already checked above, so there's nothing else to validate -- grant.
    insertClaimRow(db, { project_id, node_id, holder_token });
    return { claimed: true, ...confidence };
  } catch (err) {
    return { claimed: false, reason: `internal error: ${(err && err.message) || err}` };
  }
}

/**
 * POST /lease's logic -- the whole-document equivalent of claimNode, rule
 * 1's blocker. `{leased: true}` / `{leased: false, reason,
 * holder_token_of_conflict?}`. Never throws.
 */
export function leaseWholeDocument(db, { project_id, holder_token }) {
  try {
    if (!isNonEmptyString(project_id) || !isNonEmptyString(holder_token)) {
      return { leased: false, reason: "project_id and holder_token are both required" };
    }

    const nowMs = Date.now();
    const existing = findLiveClaim(db, project_id, WHOLE_DOCUMENT_LEASE_NODE_ID, nowMs);
    if (existing) {
      if (existing.holder_token !== holder_token) {
        return {
          leased: false,
          reason: "a whole-document lease is already held by another holder",
          holder_token_of_conflict: existing.holder_token,
        };
      }
      // Rule 4: idempotent re-lease.
      db.prepare("UPDATE claims SET claimed_at = ? WHERE id = ?").run(nowIso(), existing.id);
      return { leased: true };
    }

    // Rule 2 (gap found in review, 2026-09-17 -- write-back-spec.md's 5
    // numbered rules never stated this, even though the spec's own header
    // says the conflict model should mirror locks.py conceptually, and
    // locks.py's acquire_docx_document_lease enforces exactly this): ANY
    // other holder's live SCOPED claim also blocks a new whole-document
    // lease. "I may rewrite the entire package" is incompatible with anyone
    // else holding any claim on any part of it -- the same reasoning
    // locks.py's own module comment gives for its identical rule.
    const otherScoped = findAnyOtherLiveScopedClaim(db, project_id, holder_token, nowMs);
    if (otherScoped) {
      return {
        leased: false,
        reason:
          "another holder has a live scoped claim on this project; a whole-document lease requires the project to be free of every other holder's claims first",
        holder_token_of_conflict: otherScoped.holder_token,
      };
    }

    insertClaimRow(db, { project_id, node_id: WHOLE_DOCUMENT_LEASE_NODE_ID, holder_token });
    return { leased: true };
  } catch (err) {
    return { leased: false, reason: `internal error: ${(err && err.message) || err}` };
  }
}

/**
 * POST /release's logic. Omitting `node_id` releases every live claim this
 * holder_token holds on the project (mirrors release_docx_region_claims'
 * no-args-means-everything shape) -- including the whole-doc lease, since it
 * lives in the same table under the reserved sentinel node_id. Soft-release
 * only (`released_at` set, row kept) -- same choice, same reason, as
 * store.js's schema comment: keep history inspectable. Returns
 * `{released: <count>}`. Never throws.
 */
export function releaseClaims(db, { project_id, holder_token, node_id }) {
  try {
    if (!isNonEmptyString(project_id) || !isNonEmptyString(holder_token)) {
      return { released: 0, reason: "project_id and holder_token are both required" };
    }
    if (node_id !== undefined && !isNonEmptyString(node_id)) {
      return { released: 0, reason: "node_id, if provided, must be a non-empty string" };
    }
    const now = nowIso();
    const result = node_id
      ? db
          .prepare(
            "UPDATE claims SET released_at = ? WHERE project_id = ? AND holder_token = ? AND node_id = ? AND released_at IS NULL"
          )
          .run(now, project_id, holder_token, node_id)
      : db
          .prepare(
            "UPDATE claims SET released_at = ? WHERE project_id = ? AND holder_token = ? AND released_at IS NULL"
          )
          .run(now, project_id, holder_token);
    return { released: result.changes };
  } catch (err) {
    return { released: 0, reason: `internal error: ${(err && err.message) || err}` };
  }
}

/**
 * GET /claims's logic: every LIVE claim (scoped or whole-doc) on a project,
 * for the popup to show "someone/something else has X claimed". Each entry
 * carries `identity_confidence`/`identity_confidence_reason` when
 * applicable, same as a fresh /claim response would, so the popup doesn't
 * need to re-derive rule 5 itself just to render an existing claim's
 * warning. Never throws (a bad/missing project_id just yields an empty
 * list, matching "no claims exist for this project" -- there is nothing
 * here that can fail more sharply than that).
 */
export function getLiveClaims(db, projectId) {
  try {
    const nowMs = Date.now();
    const rows = db
      .prepare("SELECT * FROM claims WHERE project_id = ? AND released_at IS NULL ORDER BY claimed_at DESC")
      .all(projectId);
    return rows.filter((row) => isLive(row, nowMs)).map((row) => ({
      node_id: row.node_id,
      holder_token: row.holder_token,
      claimed_at: row.claimed_at,
      ...(identityConfidenceFor(row.node_id) || {}),
    }));
  } catch {
    return [];
  }
}
