import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { lineInfoForLine, docText, computeFieldEditOps, applyFieldEdit, type WriteSession } from "./write.js";
import { listSnapshots } from "./local-snapshot.js";
import { openStore } from "./store.js";
import { claimNode, getLiveClaims } from "./claims.js";
import type { OutlineNodeLike } from "./range-locate.js";

const LINES: string[] = ["\\documentclass{article}", "\\begin{document}", "\\section{Intro}", "Hello.", "\\end{document}"];

function withTempSnapshotDir<T>(fn: (dir: string) => T | Promise<T>): Promise<T> {
  const dir = mkdtempSync(join(tmpdir(), "meridian-latex-write-test-"));
  return Promise.resolve(fn(dir)).finally(() => rmSync(dir, { recursive: true, force: true }));
}

test("lineInfoForLine: offsets match CM6 Line semantics (to excludes the newline)", () => {
  const info = lineInfoForLine(LINES, 3)!;
  assert.equal(info.text, "\\section{Intro}");
  assert.equal(docText(LINES).slice(info.from, info.to), "\\section{Intro}");
  // char right after `to` is the newline separating line 3 from line 4
  assert.equal(docText(LINES)[info.to], "\n");
});

test("lineInfoForLine: line 1 starts at offset 0", () => {
  assert.equal(lineInfoForLine(LINES, 1)!.from, 0);
});

test("lineInfoForLine: null for an out-of-range line number, never throws", () => {
  assert.equal(lineInfoForLine(LINES, 0), null);
  assert.equal(lineInfoForLine(LINES, 999), null);
  assert.equal(lineInfoForLine(LINES, 1.5), null);
});

test("docText: exactly lines.join(\"\\n\")", () => {
  assert.equal(docText(LINES), LINES.join("\n"));
});

test("computeFieldEditOps: a heading title edit produces a delete+insert pair anchored at the SAME position", () => {
  const target: OutlineNodeLike = { id: "h1", kind: "heading", level: "section", line: 3, title: "Intro" };
  const nodes: OutlineNodeLike[] = [target];
  const result = computeFieldEditOps(LINES, nodes, target, "title", "Introduction");
  assert.equal(result.ok, true);
  if (!result.ok) throw new Error("expected ok:true");
  assert.equal(result.oldText, "Intro");
  assert.deepEqual(result.ops, [
    { d: "Intro", p: result.from },
    { i: "Introduction", p: result.from },
  ]);
  assert.equal(result.ops[0].p, result.ops[1].p, "delete and insert must share one anchor position");
});

test("computeFieldEditOps: propagates a range-locate failure without throwing", () => {
  const target: OutlineNodeLike = { id: "h1", kind: "heading", level: "section", line: 3, title: "Intro" };
  // Two outline nodes claim the same line, but the live text only has one heading -- count mismatch.
  const nodes: OutlineNodeLike[] = [target, { id: "h2", kind: "heading", level: "section", line: 3, title: "Intro" }];
  const result = computeFieldEditOps(LINES, nodes, target, "title", "X");
  assert.equal(result.ok, false);
  assert.match(!result.ok ? result.reason : "", /count mismatch/);
});

test("computeFieldEditOps: a missing line number for the field is reported, not thrown", () => {
  const target: OutlineNodeLike = { id: "t1", kind: "table", captionLine: null, labelLine: null };
  const result = computeFieldEditOps(LINES, [target], target, "caption", "X");
  assert.equal(result.ok, false);
  assert.match(!result.ok ? result.reason : "", /no caption/);
});

test("applyFieldEdit: joins fresh, computes ops against that content, and submits via applyUpdate", async () => {
  const target: OutlineNodeLike = { id: "h1", kind: "heading", level: "section", line: 3, title: "Intro" };
  const nodes: OutlineNodeLike[] = [target];
  const calls: unknown[][] = [];
  const fakeSession: WriteSession = {
    async joinDoc(docId: string) {
      calls.push(["joinDoc", docId]);
      return { lines: LINES, version: 42 };
    },
    async applyUpdate(docId, version, ops, trackChanges) {
      calls.push(["applyUpdate", docId, version, ops, trackChanges]);
      return { version: version + 1 };
    },
  };

  const result = await applyFieldEdit(fakeSession, "doc123", nodes, target, "title", "Introduction", true);

  assert.equal(result.ok, true);
  assert.equal(result.version, 43);
  assert.equal(!("reason" in result) && result.oldText, "Intro");
  assert.equal(!("reason" in result) && result.newText, "Introduction");
  assert.deepEqual(calls[0], ["joinDoc", "doc123"]);
  assert.equal(calls[1][0], "applyUpdate");
  assert.equal(calls[1][1], "doc123");
  assert.equal(calls[1][2], 42, "must submit against the version joinDoc just returned");
  assert.equal(calls[1][4], true, "trackChanges must be forwarded");
});

test("applyFieldEdit: never calls applyUpdate when the range can't be safely located", async () => {
  let applyUpdateCalled = false;
  const fakeSession: WriteSession = {
    async joinDoc() {
      return { lines: LINES, version: 1 };
    },
    async applyUpdate() {
      applyUpdateCalled = true;
      return { version: 2 };
    },
  };

  // Heading title mismatches are tolerated (rendered vs raw), so force a
  // real abort via a field the node doesn't have a line for instead.
  const badTarget: OutlineNodeLike = { id: "t1", kind: "table", captionLine: null };
  const result = await applyFieldEdit(fakeSession, "doc123", [badTarget], badTarget, "caption", "X");

  assert.equal(result.ok, false);
  assert.equal(applyUpdateCalled, false);
});

// --- Robustness: pre-write snapshot + reconcile-on-failure (2026-09-25) ---

test("applyFieldEdit: snapshots the doc BEFORE a successful write, and reports the path", async () => {
  const target: OutlineNodeLike = { id: "h1", kind: "heading", level: "section", line: 3, title: "Intro" };
  const fakeSession: WriteSession = {
    projectId: "proj1",
    async joinDoc() {
      return { lines: LINES, version: 5 };
    },
    async applyUpdate(docId, version) {
      return { version: version + 1 };
    },
  };

  await withTempSnapshotDir(async (dir) => {
    const result = await applyFieldEdit(fakeSession, "doc123", [target], target, "title", "Introduction", false, dir);
    assert.equal(result.ok, true);
    assert.equal(result.reconciled, false);
    if (result.reconciled !== false) throw new Error("expected reconciled:false");
    assert.ok(result.snapshotPath, "a snapshot path must be returned on a normal successful write");
    assert.equal(readFileSync(result.snapshotPath!, "utf-8"), LINES.join("\n"), "the snapshot must hold the PRE-write content");
    assert.deepEqual(listSnapshots({ projectId: "proj1", docId: "doc123", dir }).length, 1);
  });
});

test("applyFieldEdit: a snapshot failure never blocks the real write -- ok:true with snapshotPath:null", async () => {
  const target: OutlineNodeLike = { id: "h1", kind: "heading", level: "section", line: 3, title: "Intro" };
  const fakeSession: WriteSession = {
    projectId: "proj1",
    async joinDoc() {
      return { lines: LINES, version: 5 };
    },
    async applyUpdate(docId, version) {
      return { version: version + 1 };
    },
  };
  // A file (not a directory) at the snapshots base path makes mkdirSync
  // inside snapshotDoc throw -- a real, if contrived, I/O failure.
  await withTempSnapshotDir(async (dir) => {
    const { writeFileSync } = await import("node:fs");
    const blockedDir = join(dir, "blocked");
    writeFileSync(blockedDir, "not a directory");
    const result = await applyFieldEdit(fakeSession, "doc123", [target], target, "title", "Introduction", false, blockedDir);
    assert.equal(result.ok, true, "the real edit must still succeed even though snapshotting failed");
    assert.equal("snapshotPath" in result ? result.snapshotPath : undefined, null);
  });
});

test("applyFieldEdit: applyUpdate fails, but reconciliation finds the edit DID land -- ok:true, reconciled, reconcileStatus:applied", async () => {
  const target: OutlineNodeLike = { id: "h1", kind: "heading", level: "section", line: 3, title: "Intro" };
  const AFTER = ["\\documentclass{article}", "\\begin{document}", "\\section{Introduction}", "Hello.", "\\end{document}"];
  let joinDocCalls = 0;
  const fakeSession: WriteSession = {
    projectId: "proj1",
    async joinDoc() {
      joinDocCalls += 1;
      // First joinDoc (the real pre-write fetch) sees the OLD content;
      // the reconciliation's fresh joinDoc sees the NEW content -- exactly
      // matching the real live-tested scenario where the edit landed
      // despite the client never getting confirmation.
      return joinDocCalls === 1 ? { lines: LINES, version: 5 } : { lines: AFTER, version: 6 };
    },
    async applyUpdate() {
      throw new Error("timed out waiting for otUpdateApplied");
    },
  };

  await withTempSnapshotDir(async (dir) => {
    const result = await applyFieldEdit(fakeSession, "doc123", [target], target, "title", "Introduction", false, dir);
    assert.equal(result.ok, true);
    assert.equal(result.reconciled, true);
    if (!result.reconciled) throw new Error("expected reconciled:true");
    assert.equal(result.reconcileStatus, "applied");
    assert.equal(result.version, 6);
    assert.match(result.originalError, /timed out/);
    assert.ok(result.snapshotPath, "the pre-write snapshot must still have been taken");
  });
});

test("applyFieldEdit: applyUpdate fails, reconciliation confirms it did NOT land -- ok:false, reconcileStatus:not_applied", async () => {
  const target: OutlineNodeLike = { id: "h1", kind: "heading", level: "section", line: 3, title: "Intro" };
  const fakeSession: WriteSession = {
    projectId: "proj1",
    async joinDoc() {
      // Every joinDoc (including the reconciliation's) sees the SAME,
      // unchanged content -- the edit genuinely never landed.
      return { lines: LINES, version: 5 };
    },
    async applyUpdate() {
      throw new Error("connection closed");
    },
  };

  await withTempSnapshotDir(async (dir) => {
    const result = await applyFieldEdit(fakeSession, "doc123", [target], target, "title", "Introduction", false, dir);
    assert.equal(result.ok, false);
    assert.equal(result.reconciled, true);
    if (!result.reconciled) throw new Error("expected reconciled:true");
    assert.equal(result.reconcileStatus, "not_applied");
  });
});

test("applyFieldEdit: applyUpdate fails, reconciliation itself can't even fetch -- rethrows the ORIGINAL error, not a reconciliation error", async () => {
  const target: OutlineNodeLike = { id: "h1", kind: "heading", level: "section", line: 3, title: "Intro" };
  let joinDocCalls = 0;
  const fakeSession: WriteSession = {
    projectId: "proj1",
    async joinDoc() {
      joinDocCalls += 1;
      if (joinDocCalls === 1) return { lines: LINES, version: 5 };
      throw new Error("still no connection");
    },
    async applyUpdate() {
      throw new Error("original write failure");
    },
  };

  await withTempSnapshotDir(async (dir) => {
    await assert.rejects(
      () => applyFieldEdit(fakeSession, "doc123", [target], target, "title", "Introduction", false, dir),
      /original write failure/,
    );
  });
});

// --- Optional claim coordination (2026-09-25) ---
//
// Uses a REAL in-memory better-sqlite3 db via store.js's openStore(":memory:")
// for every test below, not a mock of claims.js -- the whole point is
// testing the real claim/release interaction (INSERT/UPDATE rows, not just
// that the right function names got called).

test("applyFieldEdit: with claims options, a successful write claims the node and releases it afterward", async () => {
  const target: OutlineNodeLike = { id: "h1", kind: "heading", level: "section", line: 3, title: "Intro" };
  const fakeSession: WriteSession = {
    projectId: "proj1",
    async joinDoc() {
      return { lines: LINES, version: 5 };
    },
    async applyUpdate(docId, version) {
      return { version: version + 1 };
    },
  };
  const db = openStore(":memory:");
  try {
    await withTempSnapshotDir(async (dir) => {
      const result = await applyFieldEdit(fakeSession, "doc123", [target], target, "title", "Introduction", false, dir, {
        db,
        holder_token: "holder-a",
      });
      assert.equal(result.ok, true);
      assert.equal(result.reconciled, false);
      // Nothing left claimed once the write completes -- the claim taken at
      // the start of this call must have been released, not left stuck.
      assert.deepEqual(getLiveClaims(db, "proj1"), []);
    });
  } finally {
    db.close();
  }
});

test("applyFieldEdit: rejects a write against a node already claimed by a different holder_token, before any applyUpdate call", async () => {
  const target: OutlineNodeLike = { id: "h1", kind: "heading", level: "section", line: 3, title: "Intro" };
  let applyUpdateCalled = false;
  let joinDocCalled = false;
  const fakeSession: WriteSession = {
    projectId: "proj1",
    async joinDoc() {
      joinDocCalled = true;
      return { lines: LINES, version: 5 };
    },
    async applyUpdate() {
      applyUpdateCalled = true;
      return { version: 6 };
    },
  };
  const db = openStore(":memory:");
  try {
    // holder-b claims the node first, exactly as another caller (a sibling
    // CLI invocation, a parallel agent, the browser extension's popup flow)
    // actively editing this same node would have.
    const preClaim = claimNode(db, { project_id: "proj1", node_id: "h1", holder_token: "holder-b" });
    assert.equal(preClaim.claimed, true);

    const result = await applyFieldEdit(fakeSession, "doc123", [target], target, "title", "Introduction", false, undefined, {
      db,
      holder_token: "holder-a",
    });

    assert.equal(result.ok, false);
    assert.equal("reason" in result ? result.reason : "", "node already claimed");
    assert.equal("reason" in result ? result.claimedBy : "", "holder-b");
    // The whole point: never even attempt the write against a node someone
    // else is actively editing.
    assert.equal(joinDocCalled, false);
    assert.equal(applyUpdateCalled, false);
    // holder-b's claim must be untouched -- a rejected claim attempt by
    // holder-a must never release (or otherwise disturb) another holder's
    // real claim.
    assert.deepEqual(
      getLiveClaims(db, "proj1").map((c) => c.holder_token),
      ["holder-b"],
    );
  } finally {
    db.close();
  }
});

test("applyFieldEdit: with no claims option, behaves exactly as today -- no claim check even if another holder holds the node in that same db", async () => {
  const target: OutlineNodeLike = { id: "h1", kind: "heading", level: "section", line: 3, title: "Intro" };
  const fakeSession: WriteSession = {
    projectId: "proj1",
    async joinDoc() {
      return { lines: LINES, version: 5 };
    },
    async applyUpdate(docId, version) {
      return { version: version + 1 };
    },
  };
  const db = openStore(":memory:");
  try {
    // Someone else holds a real, live claim on this exact node...
    claimNode(db, { project_id: "proj1", node_id: "h1", holder_token: "holder-b" });

    // ...but this caller never opted into claim coordination (no 9th
    // argument at all) -- must behave exactly as it did before this option
    // existed: no claims.js call, write proceeds and succeeds.
    const result = await applyFieldEdit(fakeSession, "doc123", [target], target, "title", "Introduction");

    assert.equal(result.ok, true);
    assert.equal(result.reconciled, false);
    // holder-b's claim is completely undisturbed -- this call never touched
    // claims.js at all.
    assert.deepEqual(
      getLiveClaims(db, "proj1").map((c) => c.holder_token),
      ["holder-b"],
    );
  } finally {
    db.close();
  }
});

test("applyFieldEdit: a write that throws (reconciliation itself can't even fetch) still releases its claim rather than leaving it stuck", async () => {
  const target: OutlineNodeLike = { id: "h1", kind: "heading", level: "section", line: 3, title: "Intro" };
  let joinDocCalls = 0;
  const fakeSession: WriteSession = {
    projectId: "proj1",
    async joinDoc() {
      joinDocCalls += 1;
      if (joinDocCalls === 1) return { lines: LINES, version: 5 };
      throw new Error("still no connection");
    },
    async applyUpdate() {
      throw new Error("original write failure");
    },
  };
  const db = openStore(":memory:");
  try {
    await withTempSnapshotDir(async (dir) => {
      await assert.rejects(
        () =>
          applyFieldEdit(fakeSession, "doc123", [target], target, "title", "Introduction", false, dir, {
            db,
            holder_token: "holder-a",
          }),
        /original write failure/,
      );
      // Even though the call threw, the claim taken at the start must not
      // be left stuck.
      assert.deepEqual(getLiveClaims(db, "proj1"), []);
    });
  } finally {
    db.close();
  }
});
