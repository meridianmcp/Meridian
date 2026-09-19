import { test } from "node:test";
import assert from "node:assert/strict";
import { openStore, WHOLE_DOCUMENT_LEASE_NODE_ID, CLAIM_TTL_MINUTES } from "./store.js";
import {
  claimNode,
  leaseWholeDocument,
  releaseClaims,
  getLiveClaims,
  identityConfidenceFor,
} from "./claims.js";

// Every test opens its own `:memory:` database -- fully isolated, no shared
// state, no disk file, matching store.js's own doc comment on why tests pass
// `:memory:` instead of the real on-disk path.
function freshStore() {
  return openStore(":memory:");
}

// --- Rule 1: a live whole-document lease blocks every OTHER holder's new
// claim (scoped or whole-doc) on that project. ---------------------------

test("rule 1: a live whole-document lease blocks another holder's new lease attempt", () => {
  const db = freshStore();
  const alice = leaseWholeDocument(db, { project_id: "p1", holder_token: "alice" });
  assert.deepEqual(alice, { leased: true });

  const bob = leaseWholeDocument(db, { project_id: "p1", holder_token: "bob" });
  assert.equal(bob.leased, false);
  assert.equal(bob.holder_token_of_conflict, "alice");
});

test("rule 1: a live whole-document lease blocks another holder's new SCOPED claim too", () => {
  const db = freshStore();
  leaseWholeDocument(db, { project_id: "p1", holder_token: "alice" });

  const bob = claimNode(db, { project_id: "p1", node_id: "heading:abc123", holder_token: "bob" });
  assert.equal(bob.claimed, false);
  assert.equal(bob.reason, "a whole-document lease is held by another holder");
  assert.equal(bob.holder_token_of_conflict, "alice");

  // The scoped claim must genuinely not have been recorded.
  assert.equal(getLiveClaims(db, "p1").length, 1); // only the whole-doc lease
});

test("rule 1 (documented asymmetry): the SAME holder who holds the whole-doc lease can still take a scoped claim", () => {
  const db = freshStore();
  leaseWholeDocument(db, { project_id: "p1", holder_token: "alice" });

  const aliceClaim = claimNode(db, { project_id: "p1", node_id: "heading:abc123", holder_token: "alice" });
  assert.equal(aliceClaim.claimed, true);
});

test("rule 2 (gap fixed in review): a live SCOPED claim by another holder DOES block a new whole-document lease request", () => {
  const db = freshStore();
  claimNode(db, { project_id: "p1", node_id: "heading:abc123", holder_token: "alice" });

  // Mirrors locks.py's acquire_docx_document_lease: any other holder's live
  // claim -- lease or scoped -- blocks a new whole-document lease. The
  // original write-back-spec.md draft omitted this as its own numbered
  // rule; fixed here rather than left as a real coordination hole.
  const bob = leaseWholeDocument(db, { project_id: "p1", holder_token: "bob" });
  assert.equal(bob.leased, false);
  assert.equal(bob.holder_token_of_conflict, "alice");

  // alice's scoped claim must be untouched by bob's rejected attempt.
  const claims = getLiveClaims(db, "p1");
  assert.equal(claims.length, 1);
  assert.equal(claims[0].node_id, "heading:abc123");
});

test("the SAME holder who holds a scoped claim CAN still take the whole-document lease (their own claim doesn't block themselves)", () => {
  const db = freshStore();
  claimNode(db, { project_id: "p1", node_id: "heading:abc123", holder_token: "alice" });

  const alice = leaseWholeDocument(db, { project_id: "p1", holder_token: "alice" });
  assert.equal(alice.leased, true);
});

// --- Rule 2: a live scoped claim on node_id X by holder A blocks holder
// B's claim on the SAME node_id X. -----------------------------------------

test("rule 2: a live scoped claim on node X by holder A blocks holder B's claim on the SAME node X", () => {
  const db = freshStore();
  const alice = claimNode(db, { project_id: "p1", node_id: "heading:abc123", holder_token: "alice" });
  assert.equal(alice.claimed, true);

  const bob = claimNode(db, { project_id: "p1", node_id: "heading:abc123", holder_token: "bob" });
  assert.equal(bob.claimed, false);
  assert.equal(bob.reason, "node already claimed by another holder");
  assert.equal(bob.holder_token_of_conflict, "alice");

  // Still exactly one live claim on that node -- bob's rejected attempt must
  // not have been recorded as a second row.
  const claims = getLiveClaims(db, "p1").filter((c) => c.node_id === "heading:abc123");
  assert.equal(claims.length, 1);
  assert.equal(claims[0].holder_token, "alice");
});

// --- Rule 3: scoped claims on DIFFERENT node_ids coexist freely. ---------

test("rule 3: scoped claims on DIFFERENT node_ids coexist freely, even for different holders", () => {
  const db = freshStore();
  const alice = claimNode(db, { project_id: "p1", node_id: "heading:abc123", holder_token: "alice" });
  const bob = claimNode(db, { project_id: "p1", node_id: "table:def456", holder_token: "bob" });

  assert.equal(alice.claimed, true);
  assert.equal(bob.claimed, true);
  assert.equal(getLiveClaims(db, "p1").length, 2);
});

// --- Rule 4: re-claiming your own already-held claim is idempotent. ------

test("rule 4: re-claiming your own already-held claim is idempotent (refreshes claimed_at, no duplicate row)", () => {
  const db = freshStore();
  const first = claimNode(db, { project_id: "p1", node_id: "heading:abc123", holder_token: "alice" });
  assert.equal(first.claimed, true);

  const rowsBefore = db
    .prepare("SELECT * FROM claims WHERE project_id = ? AND node_id = ?")
    .all("p1", "heading:abc123");
  assert.equal(rowsBefore.length, 1);

  const second = claimNode(db, { project_id: "p1", node_id: "heading:abc123", holder_token: "alice" });
  assert.equal(second.claimed, true);

  const rowsAfter = db
    .prepare("SELECT * FROM claims WHERE project_id = ? AND node_id = ?")
    .all("p1", "heading:abc123");
  assert.equal(rowsAfter.length, 1, "idempotent re-claim must not insert a second row");
  assert.equal(rowsAfter[0].id, rowsBefore[0].id, "must refresh the SAME row, not swap identity");
});

test("rule 4 also applies to a whole-document lease: re-leasing your own lease is idempotent", () => {
  const db = freshStore();
  leaseWholeDocument(db, { project_id: "p1", holder_token: "alice" });
  const second = leaseWholeDocument(db, { project_id: "p1", holder_token: "alice" });
  assert.equal(second.leased, true);

  const rows = db
    .prepare("SELECT * FROM claims WHERE project_id = ? AND node_id = ?")
    .all("p1", WHOLE_DOCUMENT_LEASE_NODE_ID);
  assert.equal(rows.length, 1);
});

test("an expired claim (past CLAIM_TTL_MINUTES) is no longer live -- a DIFFERENT holder can then claim the same node_id", () => {
  const db = freshStore();
  const first = claimNode(db, { project_id: "p1", node_id: "heading:abc123", holder_token: "alice" });
  assert.equal(first.claimed, true);

  // Back-date the row past the TTL to simulate an abandoned popup session
  // without waiting CLAIM_TTL_MINUTES of real wall-clock time.
  const staleIso = new Date(Date.now() - (CLAIM_TTL_MINUTES + 5) * 60 * 1000).toISOString();
  db.prepare("UPDATE claims SET claimed_at = ? WHERE project_id = ? AND node_id = ?").run(
    staleIso,
    "p1",
    "heading:abc123"
  );

  const bob = claimNode(db, { project_id: "p1", node_id: "heading:abc123", holder_token: "bob" });
  assert.equal(bob.claimed, true, "an expired claim must not block a new holder");
});

// --- Rule 5: identity_confidence: "low" for citation-dup / positional-
// fallback ids. This never blocks the claim. -------------------------------

test("rule 5: a citation-kind -dupN id is granted but flagged identity_confidence: low", () => {
  const db = freshStore();
  const result = claimNode(db, { project_id: "p1", node_id: "citation:abcd1234-dup2", holder_token: "alice" });
  assert.equal(result.claimed, true);
  assert.equal(result.identity_confidence, "low");
  assert.match(result.identity_confidence_reason, /hash collision/);
});

test("rule 5: a :pos: position-fallback id is granted but flagged identity_confidence: low", () => {
  const db = freshStore();
  const result = claimNode(db, { project_id: "p1", node_id: "figure:pos:L12:3", holder_token: "alice" });
  assert.equal(result.claimed, true);
  assert.equal(result.identity_confidence, "low");
  assert.match(result.identity_confidence_reason, /position-derived fallback/);
});

test("rule 5: an ordinary content-fingerprinted id carries no identity_confidence field at all", () => {
  const db = freshStore();
  const result = claimNode(db, { project_id: "p1", node_id: "heading:abc123", holder_token: "alice" });
  assert.equal(result.claimed, true);
  assert.equal("identity_confidence" in result, false);
});

test("identityConfidenceFor combines both reasons when an id is both positional AND disambiguated", () => {
  const combined = identityConfidenceFor("figure:pos:L12:3-dup2");
  assert.equal(combined.identity_confidence, "low");
  assert.match(combined.identity_confidence_reason, /position-derived fallback/);
  assert.match(combined.identity_confidence_reason, /hash collision/);
});

test("identityConfidenceFor returns null for an id with neither caveat", () => {
  assert.equal(identityConfidenceFor("heading:abc123"), null);
});

// --- /release semantics --------------------------------------------------

test("/release: releasing a specific node_id only releases that claim, not the holder's others", () => {
  const db = freshStore();
  claimNode(db, { project_id: "p1", node_id: "heading:abc123", holder_token: "alice" });
  claimNode(db, { project_id: "p1", node_id: "table:def456", holder_token: "alice" });

  const result = releaseClaims(db, { project_id: "p1", holder_token: "alice", node_id: "heading:abc123" });
  assert.equal(result.released, 1);

  const claims = getLiveClaims(db, "p1");
  assert.equal(claims.length, 1);
  assert.equal(claims[0].node_id, "table:def456");

  // The released node is claimable again, including by a different holder.
  const bob = claimNode(db, { project_id: "p1", node_id: "heading:abc123", holder_token: "bob" });
  assert.equal(bob.claimed, true);
});

test("/release: omitting node_id releases every live claim this holder holds, including a whole-doc lease", () => {
  const db = freshStore();
  leaseWholeDocument(db, { project_id: "p1", holder_token: "alice" });
  claimNode(db, { project_id: "p1", node_id: "table:def456", holder_token: "alice" });

  const result = releaseClaims(db, { project_id: "p1", holder_token: "alice" });
  assert.equal(result.released, 2);
  assert.equal(getLiveClaims(db, "p1").length, 0);

  // The whole-doc lease slot is free again for another holder.
  const bob = leaseWholeDocument(db, { project_id: "p1", holder_token: "bob" });
  assert.equal(bob.leased, true);
});

test("/release: releasing a claim you don't hold changes nothing (released count is 0)", () => {
  const db = freshStore();
  claimNode(db, { project_id: "p1", node_id: "heading:abc123", holder_token: "alice" });

  const result = releaseClaims(db, { project_id: "p1", holder_token: "bob", node_id: "heading:abc123" });
  assert.equal(result.released, 0);
  assert.equal(getLiveClaims(db, "p1").length, 1, "alice's claim must be untouched");
});

// --- Never-throws convention ----------------------------------------------

test("missing required fields never throws -- returns a structured failure instead", () => {
  const db = freshStore();
  assert.doesNotThrow(() => {
    const r1 = claimNode(db, { project_id: "p1", node_id: "", holder_token: "alice" });
    assert.equal(r1.claimed, false);
    const r2 = leaseWholeDocument(db, { project_id: "", holder_token: "alice" });
    assert.equal(r2.leased, false);
    const r3 = releaseClaims(db, { project_id: "p1", holder_token: "" });
    assert.equal(r3.released, 0);
  });
});

// Real bug found 2026-09-18 via independent code review: these checks used
// to be bare truthiness (`!x`), which only rejects falsy values -- a JSON
// request body can carry a NUMBER for any of these fields (truthy, and
// truthy sails past a `!x` check), which would then get silently coerced to
// TEXT by SQLite's own type affinity rather than rejected outright.
test("a non-string (e.g. a JSON number) project_id/node_id/holder_token is rejected, not silently coerced", () => {
  const db = freshStore();
  const r1 = claimNode(db, { project_id: 12345, node_id: "heading:abc", holder_token: "alice" });
  assert.equal(r1.claimed, false);
  const r2 = claimNode(db, { project_id: "p1", node_id: 12345, holder_token: "alice" });
  assert.equal(r2.claimed, false);
  const r3 = claimNode(db, { project_id: "p1", node_id: "heading:abc", holder_token: 12345 });
  assert.equal(r3.claimed, false);
  const r4 = leaseWholeDocument(db, { project_id: 12345, holder_token: "alice" });
  assert.equal(r4.leased, false);
  const r5 = releaseClaims(db, { project_id: 12345, holder_token: "alice" });
  assert.equal(r5.released, 0);
  // node_id is optional on releaseClaims, but if PROVIDED must still be a
  // real string -- a numeric node_id must not silently release everything
  // (which omitting node_id entirely would do) nor silently coerce.
  const r6 = releaseClaims(db, { project_id: "p1", holder_token: "alice", node_id: 12345 });
  assert.equal(r6.released, 0);
});

// --- Sentinel routing -------------------------------------------------

test("a /claim aimed at the reserved whole-document sentinel node_id is routed to lease semantics, same response shape", () => {
  const db = freshStore();
  const alice = claimNode(db, { project_id: "p1", node_id: WHOLE_DOCUMENT_LEASE_NODE_ID, holder_token: "alice" });
  assert.equal(alice.claimed, true);

  const bob = claimNode(db, { project_id: "p1", node_id: WHOLE_DOCUMENT_LEASE_NODE_ID, holder_token: "bob" });
  assert.equal(bob.claimed, false);
  assert.equal(bob.holder_token_of_conflict, "alice");
});

// --- Project auto-registration (store.js) ---------------------------------

test("claiming a node before any /outline call for that project still works -- auto-registers the project row", () => {
  const db = freshStore();
  const result = claimNode(db, { project_id: "never-seen-project", node_id: "heading:abc123", holder_token: "alice" });
  assert.equal(result.claimed, true);
  // The FK-shaped relationship holds: a projects row now exists.
  const project = db.prepare("SELECT * FROM projects WHERE project_id = ?").get("never-seen-project");
  assert.ok(project);
  assert.equal(project.last_outline, "[]");
});
