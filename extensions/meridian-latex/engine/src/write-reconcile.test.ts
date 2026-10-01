import { test } from "node:test";
import assert from "node:assert/strict";
import { reconcileFieldEdit } from "./write-reconcile.js";
import type { OutlineNodeLike } from "./range-locate.js";

const LINES: string[] = ["placeholder"]; // freshLines isn't read by reconcileFieldEdit itself -- see its own doc comment.

test("reconcileFieldEdit: heading edit that clearly applied -- newText present at the target's line", () => {
  const target: OutlineNodeLike = { id: "h1", kind: "heading", level: "section", line: 3, title: "Intro" };
  const freshNodes: OutlineNodeLike[] = [{ id: "h1", kind: "heading", level: "section", line: 3, title: "Introduction" }];
  const result = reconcileFieldEdit({
    freshLines: LINES,
    freshNodes,
    target,
    fieldName: "title",
    oldText: "Intro",
    newText: "Introduction",
  });
  assert.equal(result.status, "applied");
  assert.equal(result.status === "applied" && result.node.title, "Introduction");
});

test("reconcileFieldEdit: heading edit that clearly did NOT apply -- oldText still present, unchanged", () => {
  const target: OutlineNodeLike = { id: "h1", kind: "heading", level: "section", line: 3, title: "Intro" };
  const freshNodes: OutlineNodeLike[] = [{ id: "h1", kind: "heading", level: "section", line: 3, title: "Intro" }];
  const result = reconcileFieldEdit({
    freshLines: LINES,
    freshNodes,
    target,
    fieldName: "title",
    oldText: "Intro",
    newText: "Introduction",
  });
  assert.equal(result.status, "not_applied");
  assert.equal(result.status === "not_applied" && result.node.title, "Intro");
});

test("reconcileFieldEdit: citation key edit -- anchor line comes from target.line, applied case", () => {
  const target: OutlineNodeLike = { id: "c1", kind: "citation", line: 12, key: "oldkey" };
  const freshNodes: OutlineNodeLike[] = [{ id: "c1", kind: "citation", line: 12, key: "newkey" }];
  const result = reconcileFieldEdit({
    freshLines: LINES,
    freshNodes,
    target,
    fieldName: "key",
    oldText: "oldkey",
    newText: "newkey",
  });
  assert.equal(result.status, "applied");
  assert.equal(result.status === "applied" && result.node.key, "newkey");
});

test("reconcileFieldEdit: citation key edit -- not_applied case", () => {
  const target: OutlineNodeLike = { id: "c1", kind: "citation", line: 12, key: "oldkey" };
  const freshNodes: OutlineNodeLike[] = [{ id: "c1", kind: "citation", line: 12, key: "oldkey" }];
  const result = reconcileFieldEdit({
    freshLines: LINES,
    freshNodes,
    target,
    fieldName: "key",
    oldText: "oldkey",
    newText: "newkey",
  });
  assert.equal(result.status, "not_applied");
});

test("reconcileFieldEdit: table caption edit -- anchor line is captionLine, not the table's own `line`", () => {
  const target: OutlineNodeLike = { id: "t1", kind: "table", line: 5, captionLine: 9, labelLine: null, caption: "Old caption", label: null };
  const freshNodes: OutlineNodeLike[] = [
    // A different table node sitting at the SAME `line` (the environment's
    // start) but a DIFFERENT captionLine must not be picked up -- proves
    // the match is against fieldLineNumber(candidate, "caption"), i.e.
    // captionLine, not against `line`.
    { id: "t0", kind: "table", line: 5, captionLine: 40, labelLine: null, caption: "Unrelated", label: null },
    { id: "t1", kind: "table", line: 5, captionLine: 9, labelLine: null, caption: "New caption", label: null },
  ];
  const result = reconcileFieldEdit({
    freshLines: LINES,
    freshNodes,
    target,
    fieldName: "caption",
    oldText: "Old caption",
    newText: "New caption",
  });
  assert.equal(result.status, "applied");
  assert.equal(result.status === "applied" && result.node.id, "t1");
});

test("reconcileFieldEdit: figure label edit -- anchor line is labelLine, applied case", () => {
  const target: OutlineNodeLike = { id: "f1", kind: "figure", line: 20, captionLine: 21, labelLine: 22, caption: "A figure", label: "fig:old" };
  const freshNodes: OutlineNodeLike[] = [
    { id: "f1", kind: "figure", line: 20, captionLine: 21, labelLine: 22, caption: "A figure", label: "fig:new" },
  ];
  const result = reconcileFieldEdit({
    freshLines: LINES,
    freshNodes,
    target,
    fieldName: "label",
    oldText: "fig:old",
    newText: "fig:new",
  });
  assert.equal(result.status, "applied");
  assert.equal(result.status === "applied" && result.node.label, "fig:new");
});

test("reconcileFieldEdit: equation label edit -- not_applied case", () => {
  const target: OutlineNodeLike = { id: "e1", kind: "equation", line: 30, captionLine: null, labelLine: 30, caption: null, label: "eq:old" };
  const freshNodes: OutlineNodeLike[] = [
    { id: "e1", kind: "equation", line: 30, captionLine: null, labelLine: 30, caption: null, label: "eq:old" },
  ];
  const result = reconcileFieldEdit({
    freshLines: LINES,
    freshNodes,
    target,
    fieldName: "label",
    oldText: "eq:old",
    newText: "eq:new",
  });
  assert.equal(result.status, "not_applied");
  assert.equal(result.status === "not_applied" && result.node.label, "eq:old");
});

test("reconcileFieldEdit: ambiguous -- no node of that kind exists at that line at all (deleted/restructured)", () => {
  const target: OutlineNodeLike = { id: "h1", kind: "heading", level: "section", line: 3, title: "Intro" };
  const freshNodes: OutlineNodeLike[] = [{ id: "h2", kind: "heading", level: "section", line: 8, title: "Somewhere Else" }];
  const result = reconcileFieldEdit({
    freshLines: LINES,
    freshNodes,
    target,
    fieldName: "title",
    oldText: "Intro",
    newText: "Introduction",
  });
  assert.equal(result.status, "ambiguous");
  assert.match(result.status === "ambiguous" ? result.reason : "", /No "heading" node found at line 3/);
  assert.match(result.status === "ambiguous" ? result.reason : "", /deleted|restructured/);
});

test("reconcileFieldEdit: ambiguous -- a different, unexpected value is present; reason names it", () => {
  const target: OutlineNodeLike = { id: "h1", kind: "heading", level: "section", line: 3, title: "Intro" };
  const freshNodes: OutlineNodeLike[] = [{ id: "h1", kind: "heading", level: "section", line: 3, title: "Something Completely Different" }];
  const result = reconcileFieldEdit({
    freshLines: LINES,
    freshNodes,
    target,
    fieldName: "title",
    oldText: "Intro",
    newText: "Introduction",
  });
  assert.equal(result.status, "ambiguous");
  const reason = result.status === "ambiguous" ? result.reason : "";
  // The reason string must actually surface what's there, not a generic message.
  assert.match(reason, /Something Completely Different/);
  assert.match(reason, /Intro/);
  assert.match(reason, /Introduction/);
});

test("reconcileFieldEdit: multiple candidates at the same kind+line -- applied if ANY of them holds newText", () => {
  const target: OutlineNodeLike = { id: "c1", kind: "citation", line: 7, key: "old" };
  const freshNodes: OutlineNodeLike[] = [
    { id: "c1", kind: "citation", line: 7, key: "unrelated-sibling" },
    { id: "c2", kind: "citation", line: 7, key: "new" },
  ];
  const result = reconcileFieldEdit({
    freshLines: LINES,
    freshNodes,
    target,
    fieldName: "key",
    oldText: "old",
    newText: "new",
  });
  assert.equal(result.status, "applied");
  assert.equal(result.status === "applied" && result.node.id, "c2");
});

test("reconcileFieldEdit: multiple candidates at the same kind+line -- not_applied if none hold newText but one holds oldText", () => {
  const target: OutlineNodeLike = { id: "c1", kind: "citation", line: 7, key: "old" };
  const freshNodes: OutlineNodeLike[] = [
    { id: "c1", kind: "citation", line: 7, key: "old" },
    { id: "c2", kind: "citation", line: 7, key: "unrelated-sibling" },
  ];
  const result = reconcileFieldEdit({
    freshLines: LINES,
    freshNodes,
    target,
    fieldName: "key",
    oldText: "old",
    newText: "new",
  });
  assert.equal(result.status, "not_applied");
  assert.equal(result.status === "not_applied" && result.node.id, "c1");
});

test("reconcileFieldEdit: multiple candidates, none matching old or new -- ambiguous lists every value found", () => {
  const target: OutlineNodeLike = { id: "c1", kind: "citation", line: 7, key: "old" };
  const freshNodes: OutlineNodeLike[] = [
    { id: "c1", kind: "citation", line: 7, key: "totally-unrelated-one" },
    { id: "c2", kind: "citation", line: 7, key: "totally-unrelated-two" },
  ];
  const result = reconcileFieldEdit({
    freshLines: LINES,
    freshNodes,
    target,
    fieldName: "key",
    oldText: "old",
    newText: "new",
  });
  assert.equal(result.status, "ambiguous");
  const reason = result.status === "ambiguous" ? result.reason : "";
  assert.match(reason, /totally-unrelated-one/);
  assert.match(reason, /totally-unrelated-two/);
});

test("reconcileFieldEdit: never throws when the target itself has no anchor line for the field", () => {
  const target: OutlineNodeLike = { id: "t1", kind: "table", line: 5, captionLine: null, labelLine: null, caption: null, label: null };
  const freshNodes: OutlineNodeLike[] = [{ id: "t1", kind: "table", line: 5, captionLine: null, labelLine: null, caption: null, label: null }];
  const result = reconcileFieldEdit({
    freshLines: LINES,
    freshNodes,
    target,
    fieldName: "caption",
    oldText: "X",
    newText: "Y",
  });
  assert.equal(result.status, "ambiguous");
  assert.match(result.status === "ambiguous" ? result.reason : "", /no line number for field/);
});
