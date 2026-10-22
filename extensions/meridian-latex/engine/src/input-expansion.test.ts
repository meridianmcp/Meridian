import { test } from "node:test";
import assert from "node:assert/strict";
import { expandInputs, joinDocExpanded, MAX_INPUT_DEPTH, type ExpandableSession } from "./input-expansion.js";
import type { FileTreeFolder, FileTreeDoc } from "./project-tree.js";

/** A minimal fake rootFolder matching project-tree.js's confirmed shape:
 * {_id, name, folders, docs, fileRefs}, each doc {_id, name}. */
function makeTree(docs: FileTreeDoc[]): FileTreeFolder {
  return { _id: "root", name: "rootFolder", folders: [], fileRefs: [], docs };
}

function makeSession(docsById: Record<string, string>): ExpandableSession {
  return {
    joinDoc: async (docId: string) => {
      if (!(docId in docsById)) throw new Error(`no such doc: ${docId}`);
      return { lines: docsById[docId].split("\n"), version: 0 };
    },
  };
}

test("expandInputs: a single \\input{} is spliced in verbatim", async () => {
  const rootFolder = makeTree([
    { _id: "main", name: "main.tex" },
    { _id: "intro", name: "intro.tex" },
  ]);
  const session = makeSession({ intro: "Introduction body." });
  const result = await expandInputs({ session, rootFolder, source: "Before\n\\input{intro}\nAfter" });
  assert.equal(result.source, "Before\nIntroduction body.\nAfter");
  assert.deepEqual(result.unexpanded, []);
});

test("expandInputs: \\include{} works identically to \\input{}", async () => {
  const rootFolder = makeTree([{ _id: "ch1", name: "ch1.tex" }]);
  const session = makeSession({ ch1: "Chapter one." });
  const result = await expandInputs({ session, rootFolder, source: "\\include{ch1}" });
  assert.equal(result.source, "Chapter one.");
});

test("expandInputs: a name already ending in .tex is not double-suffixed", async () => {
  const rootFolder = makeTree([{ _id: "intro", name: "intro.tex" }]);
  const session = makeSession({ intro: "Body." });
  const result = await expandInputs({ session, rootFolder, source: "\\input{intro.tex}" });
  assert.equal(result.source, "Body.");
});

test("expandInputs: multiple inputs in one source are all spliced, in order", async () => {
  const rootFolder = makeTree([
    { _id: "a", name: "a.tex" },
    { _id: "b", name: "b.tex" },
  ]);
  const session = makeSession({ a: "AAA", b: "BBB" });
  const result = await expandInputs({ session, rootFolder, source: "\\input{a} middle \\input{b}" });
  assert.equal(result.source, "AAA middle BBB");
});

test("expandInputs: recursion -- an included doc's own \\input is also expanded", async () => {
  const rootFolder = makeTree([
    { _id: "a", name: "a.tex" },
    { _id: "b", name: "b.tex" },
  ]);
  const session = makeSession({ a: "start \\input{b} end", b: "MIDDLE" });
  const result = await expandInputs({ session, rootFolder, source: "\\input{a}" });
  assert.equal(result.source, "start MIDDLE end");
});

test("expandInputs: an unresolvable name is left in place and recorded in unexpanded", async () => {
  const rootFolder = makeTree([]);
  const session = makeSession({});
  const result = await expandInputs({ session, rootFolder, source: "\\input{missing}" });
  assert.equal(result.source, "\\input{missing}");
  assert.deepEqual(result.unexpanded, ["missing"]);
});

test("expandInputs: a doc that fails to fetch is left in place and recorded, does not throw", async () => {
  const rootFolder = makeTree([{ _id: "broken", name: "broken.tex" }]);
  const session: ExpandableSession = {
    joinDoc: async () => {
      throw new Error("network error");
    },
  };
  const result = await expandInputs({ session, rootFolder, source: "\\input{broken}" });
  assert.equal(result.source, "\\input{broken}");
  assert.deepEqual(result.unexpanded, ["broken"]);
});

test("expandInputs: a cycle (doc A includes doc B which includes doc A again) drops the re-include silently, never hangs", async () => {
  const rootFolder = makeTree([
    { _id: "a", name: "a.tex" },
    { _id: "b", name: "b.tex" },
  ]);
  const session = makeSession({ a: "A-start \\input{b} A-end", b: "B-start \\input{a} B-end" });
  const result = await expandInputs({ session, rootFolder, source: "\\input{a}" });
  // The re-include of "a" from inside "b" is dropped (already in `seen`), not re-fetched.
  assert.equal(result.source, "A-start B-start  B-end A-end");
});

test("expandInputs: an unresolved name is recorded only once even if referenced twice", async () => {
  const rootFolder = makeTree([]);
  const session = makeSession({});
  const result = await expandInputs({ session, rootFolder, source: "\\input{missing} and \\input{missing}" });
  assert.deepEqual(result.unexpanded, ["missing"]);
});

test("expandInputs: exceeding MAX_INPUT_DEPTH returns the source unexpanded rather than recursing further", async () => {
  const rootFolder = makeTree([{ _id: "x", name: "x.tex" }]);
  const session = makeSession({ x: "X" });
  const result = await expandInputs({
    session,
    rootFolder,
    source: "\\input{x}",
    depth: MAX_INPUT_DEPTH + 1,
  });
  assert.equal(result.source, "\\input{x}");
});

test("expandInputs: a source with no \\input/\\include at all is returned unchanged", async () => {
  const rootFolder = makeTree([]);
  const session = makeSession({});
  const result = await expandInputs({ session, rootFolder, source: "Just plain text, no macros." });
  assert.equal(result.source, "Just plain text, no macros.");
  assert.deepEqual(result.unexpanded, []);
});

test("joinDocExpanded: joins the root doc and expands its inputs, seeding `seen` with the root's own id", async () => {
  const rootFolder = makeTree([
    { _id: "main", name: "main.tex" },
    { _id: "intro", name: "intro.tex" },
  ]);
  const session = makeSession({ main: "Start \\input{intro} End", intro: "MIDDLE" });
  session.rootFolder = rootFolder;
  const result = await joinDocExpanded(session, "main");
  assert.equal(result.source, "Start MIDDLE End");
  assert.equal(result.version, 0);
});

test("joinDocExpanded: a doc that (directly) \\inputs itself is caught by the seeded `seen` set, not infinitely recursed", async () => {
  const rootFolder = makeTree([{ _id: "main", name: "main.tex" }]);
  const session = makeSession({ main: "Self \\input{main} reference" });
  session.rootFolder = rootFolder;
  const result = await joinDocExpanded(session, "main");
  assert.equal(result.source, "Self  reference");
});
