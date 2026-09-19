import { test } from "node:test";
import assert from "node:assert/strict";
import { openStore } from "./store.js";
import { recordEdit, listProvenance, markSynced } from "./provenance.js";

function freshStore() {
  return openStore(":memory:");
}

test("recordEdit(): a well-formed edit is recorded and comes back from listProvenance", () => {
  const db = freshStore();
  const result = recordEdit(db, {
    project_id: "p1",
    node_id: "heading:abc123",
    kind: "heading",
    field: "title",
    old_value: "Old Title",
    new_value: "New Title",
    holder_token: "alice",
  });
  assert.equal(result.recorded, true);
  assert.ok(result.id);

  const rows = listProvenance(db, { project_id: "p1" });
  assert.equal(rows.length, 1);
  assert.equal(rows[0].id, result.id);
  assert.equal(rows[0].old_value, "Old Title");
  assert.equal(rows[0].new_value, "New Title");
  assert.equal(rows[0].synced_to_meridian_outputs, 0);
  assert.equal(rows[0].synced_at, null);
});

test("recordEdit(): old_value defaults to null when omitted", () => {
  const db = freshStore();
  const result = recordEdit(db, {
    project_id: "p1",
    node_id: "citation:abc123",
    kind: "citation",
    field: "key",
    new_value: "smith2024",
    holder_token: "alice",
  });
  assert.equal(result.recorded, true);
  const rows = listProvenance(db, { project_id: "p1" });
  assert.equal(rows[0].old_value, null);
});

test("recordEdit(): missing required fields never throws -- returns a structured failure instead", () => {
  const db = freshStore();
  assert.doesNotThrow(() => {
    const r1 = recordEdit(db, { project_id: "p1", node_id: "", kind: "heading", field: "title", new_value: "x", holder_token: "alice" });
    assert.equal(r1.recorded, false);
    const r2 = recordEdit(db, { project_id: "p1", node_id: "heading:abc", kind: "heading", field: "title", new_value: 42, holder_token: "alice" });
    assert.equal(r2.recorded, false);
  });
  assert.equal(listProvenance(db, { project_id: "p1" }).length, 0);
});

test("recordEdit(): auto-registers the project row, same as claimNode -- provenance can be recorded even if /outline never ran for this project_id", () => {
  const db = freshStore();
  const result = recordEdit(db, {
    project_id: "never-registered",
    node_id: "heading:abc123",
    kind: "heading",
    field: "title",
    new_value: "x",
    holder_token: "alice",
  });
  assert.equal(result.recorded, true);
  const project = db.prepare("SELECT * FROM projects WHERE project_id = ?").get("never-registered");
  assert.ok(project);
});

test("listProvenance(): unsynced_only filters out rows already marked synced", () => {
  const db = freshStore();
  db.prepare("INSERT INTO projects (project_id, last_seen_at, last_outline) VALUES ('p1', datetime('now'), '[]')").run();
  const a = recordEdit(db, { project_id: "p1", node_id: "heading:a", kind: "heading", field: "title", new_value: "A", holder_token: "alice" });
  const b = recordEdit(db, { project_id: "p1", node_id: "heading:b", kind: "heading", field: "title", new_value: "B", holder_token: "alice" });

  assert.equal(listProvenance(db, { project_id: "p1", unsynced_only: true }).length, 2);

  markSynced(db, [a.id]);

  const unsynced = listProvenance(db, { project_id: "p1", unsynced_only: true });
  assert.equal(unsynced.length, 1);
  assert.equal(unsynced[0].id, b.id);

  // The synced row still shows up in the unfiltered list, with its flag set.
  const all = listProvenance(db, { project_id: "p1" });
  assert.equal(all.length, 2);
  const synced = all.find((r) => r.id === a.id);
  assert.equal(synced.synced_to_meridian_outputs, 1);
  assert.ok(synced.synced_at);
});

test("listProvenance(): newest first", () => {
  const db = freshStore();
  db.prepare("INSERT INTO projects (project_id, last_seen_at, last_outline) VALUES ('p1', datetime('now'), '[]')").run();
  db.prepare(
    "INSERT INTO provenance (id, project_id, node_id, kind, field, new_value, holder_token, recorded_at, synced_to_meridian_outputs) VALUES ('r1', 'p1', 'heading:a', 'heading', 'title', 'A', 'alice', '2026-01-01T00:00:00.000Z', 0)"
  ).run();
  db.prepare(
    "INSERT INTO provenance (id, project_id, node_id, kind, field, new_value, holder_token, recorded_at, synced_to_meridian_outputs) VALUES ('r2', 'p1', 'heading:b', 'heading', 'title', 'B', 'alice', '2026-01-02T00:00:00.000Z', 0)"
  ).run();

  const rows = listProvenance(db, { project_id: "p1" });
  assert.deepEqual(rows.map((r) => r.id), ["r2", "r1"]);
});

test("listProvenance(): missing project_id returns [] rather than throwing", () => {
  const db = freshStore();
  assert.doesNotThrow(() => {
    assert.deepEqual(listProvenance(db, {}), []);
    assert.deepEqual(listProvenance(db), []);
  });
});

test("markSynced(): marking an unknown id is a no-op (marked: 0), never throws", () => {
  const db = freshStore();
  const result = markSynced(db, ["does-not-exist"]);
  assert.equal(result.marked, 0);
});

test("markSynced(): an empty or non-array ids argument is a no-op", () => {
  const db = freshStore();
  assert.deepEqual(markSynced(db, []), { marked: 0 });
  assert.deepEqual(markSynced(db, undefined), { marked: 0 });
});

test("markSynced(): re-marking an already-synced row is idempotent -- still counts as marked, refreshes synced_at", () => {
  const db = freshStore();
  db.prepare("INSERT INTO projects (project_id, last_seen_at, last_outline) VALUES ('p1', datetime('now'), '[]')").run();
  const a = recordEdit(db, { project_id: "p1", node_id: "heading:a", kind: "heading", field: "title", new_value: "A", holder_token: "alice" });
  markSynced(db, [a.id]);
  const firstSyncedAt = listProvenance(db, { project_id: "p1" })[0].synced_at;

  const second = markSynced(db, [a.id]);
  assert.equal(second.marked, 1);
  assert.ok(listProvenance(db, { project_id: "p1" })[0].synced_at >= firstSyncedAt);
});
