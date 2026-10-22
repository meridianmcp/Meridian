import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { snapshotDoc, listSnapshots } from "./local-snapshot.js";

// Supports both sync and async `fn` -- the async tests below (which need to
// `await delay(...)` between snapshots so consecutive timestamps can never
// collide) must not have their temp dir removed until the returned promise
// itself settles, not just once `fn(dir)` has synchronously returned it.
async function withTempDir<T>(fn: (dir: string) => T | Promise<T>): Promise<T> {
  const dir = mkdtempSync(join(tmpdir(), "meridian-latex-snapshot-test-"));
  try {
    return await fn(dir);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test("snapshotDoc: writes the expected file with the expected content", async () => {
  await withTempDir((dir) => {
    const lines = ["\\documentclass{article}", "\\begin{document}", "Hello.", "\\end{document}"];
    const filePath = snapshotDoc({ projectId: "proj1", docId: "doc1", lines, dir });

    assert.ok(existsSync(filePath));
    assert.equal(readFileSync(filePath, "utf-8"), lines.join("\n"));
    // path shape: <dir>/<projectId>/<docId>/<timestamp>.tex
    assert.equal(join(dir, "proj1", "doc1"), join(filePath, ".."));
    assert.match(filePath, /\.tex$/);
  });
});

test("snapshotDoc: creates nested directories that don't exist yet", async () => {
  await withTempDir((dir) => {
    const base = join(dir, "does", "not", "exist", "yet");
    const filePath = snapshotDoc({ projectId: "p", docId: "d", lines: ["x"], dir: base });
    assert.ok(existsSync(filePath));
  });
});

test("snapshotDoc: two snapshots of the same doc get two distinct, both-readable files", async () => {
  await withTempDir(async (dir) => {
    const first = snapshotDoc({ projectId: "proj1", docId: "doc1", lines: ["v1"], dir });
    // ISO timestamps used as filenames are millisecond-precision -- wait
    // past a millisecond boundary so the two snapshots can never collide,
    // rather than looping/retrying on a flaky same-millisecond result.
    await delay(5);
    const second = snapshotDoc({ projectId: "proj1", docId: "doc1", lines: ["v2"], dir });

    assert.notEqual(first, second);
    assert.equal(readFileSync(first, "utf-8"), "v1");
    assert.equal(readFileSync(second, "utf-8"), "v2");
  });
});

test("listSnapshots: returns paths newest-first", async () => {
  await withTempDir(async (dir) => {
    const first = snapshotDoc({ projectId: "proj1", docId: "doc1", lines: ["v1"], dir });
    await delay(5);
    const second = snapshotDoc({ projectId: "proj1", docId: "doc1", lines: ["v2"], dir });
    await delay(5);
    const third = snapshotDoc({ projectId: "proj1", docId: "doc1", lines: ["v3"], dir });

    const result = listSnapshots({ projectId: "proj1", docId: "doc1", dir });
    assert.deepEqual(result, [third, second, first]);
  });
});

test("listSnapshots: returns [] for a project/doc with no snapshots yet, without throwing", async () => {
  await withTempDir((dir) => {
    assert.deepEqual(listSnapshots({ projectId: "never-snapshotted", docId: "doc1", dir }), []);
    // Base dir itself doesn't even exist yet -- still shouldn't throw.
    assert.deepEqual(
      listSnapshots({ projectId: "p", docId: "d", dir: join(dir, "does-not-exist") }),
      [],
    );
  });
});
