import { test } from "node:test";
import assert from "node:assert/strict";
import { unwrapRootFolder, resolveDocIdByPath, listDocPaths, type FileTreeFolder } from "./project-tree.js";

// Tree shape below matches the CONFIRMED Overleaf field names exactly --
// see project-tree.js's own header comment for the citations (services/web/types/folder.ts,
// services/web/types/doc.ts, file-tree-data-context.tsx's rootFolder[0] unwrap).
function makeTree(): FileTreeFolder {
  return {
    _id: "root0000000000000000000",
    name: "rootFolder",
    docs: [{ _id: "maindoc000000000000000a", name: "main.tex" }],
    fileRefs: [{ _id: "fileref00000000000000b", name: "figure.png" }],
    folders: [
      {
        _id: "chapters0000000000000c",
        name: "chapters",
        docs: [
          { _id: "introdoc0000000000000d", name: "intro.tex" },
          { _id: "conclusiondoc00000000e", name: "conclusion.tex" },
        ],
        fileRefs: [],
        folders: [
          {
            _id: "nested000000000000000f",
            name: "nested",
            docs: [{ _id: "deepdoc0000000000000g", name: "deep.tex" }],
            fileRefs: [],
            folders: [],
          },
        ],
      },
    ],
  };
}

// --- unwrapRootFolder() ----------------------------------------------------

test("unwrapRootFolder(): takes element [0] of project.rootFolder, matching file-tree-data-context.tsx's own `rootFolder?.[0]`", () => {
  const tree = makeTree();
  assert.equal(unwrapRootFolder({ rootFolder: [tree] }), tree);
});

test("unwrapRootFolder(): returns null for a project with no rootFolder at all", () => {
  assert.equal(unwrapRootFolder({}), null);
  assert.equal(unwrapRootFolder(undefined), null);
  assert.equal(unwrapRootFolder({ rootFolder: [] }), null);
});

// --- resolveDocIdByPath() ---------------------------------------------------

test("resolveDocIdByPath(): resolves a top-level doc", () => {
  const result = resolveDocIdByPath(makeTree(), "main.tex");
  assert.deepEqual(result, { ok: true, docId: "maindoc000000000000000a" });
});

test("resolveDocIdByPath(): resolves a doc one folder deep", () => {
  const result = resolveDocIdByPath(makeTree(), "chapters/intro.tex");
  assert.deepEqual(result, { ok: true, docId: "introdoc0000000000000d" });
});

test("resolveDocIdByPath(): resolves a doc nested two folders deep", () => {
  const result = resolveDocIdByPath(makeTree(), "chapters/nested/deep.tex");
  assert.deepEqual(result, { ok: true, docId: "deepdoc0000000000000g" });
});

test("resolveDocIdByPath(): tolerates a leading slash, matching Overleaf's own tolerant path handling", () => {
  const result = resolveDocIdByPath(makeTree(), "/chapters/intro.tex");
  assert.deepEqual(result, { ok: true, docId: "introdoc0000000000000d" });
});

test("resolveDocIdByPath(): reports a missing intermediate folder by name, not just 'not found'", () => {
  const result = resolveDocIdByPath(makeTree(), "no-such-folder/intro.tex");
  assert.equal(result.ok, false);
  assert.match((result as { ok: false; reason: string }).reason, /No folder named "no-such-folder"/);
});

test("resolveDocIdByPath(): reports a missing doc by name once the folder itself resolved", () => {
  const result = resolveDocIdByPath(makeTree(), "chapters/no-such-doc.tex");
  assert.equal(result.ok, false);
  assert.match((result as { ok: false; reason: string }).reason, /No doc named "no-such-doc.tex"/);
});

test("resolveDocIdByPath(): a fileRef path gets a distinct, explicit reason -- not a generic 'not found'", () => {
  const result = resolveDocIdByPath(makeTree(), "figure.png");
  assert.equal(result.ok, false);
  assert.match((result as { ok: false; reason: string }).reason, /binary file \(fileRef\)/);
});

test("resolveDocIdByPath(): a folder-shaped path (no trailing file) gets its own distinct reason", () => {
  const result = resolveDocIdByPath(makeTree(), "chapters");
  assert.equal(result.ok, false);
  assert.match((result as { ok: false; reason: string }).reason, /is a folder, not a doc/);
});

test("resolveDocIdByPath(): empty path is rejected without walking anything", () => {
  const result = resolveDocIdByPath(makeTree(), "");
  assert.equal(result.ok, false);
  assert.match((result as { ok: false; reason: string }).reason, /Empty path/);
});

test("resolveDocIdByPath(): a null rootFolder (no joinProjectResponse yet) is reported, not thrown", () => {
  const result = resolveDocIdByPath(null, "main.tex");
  assert.equal(result.ok, false);
  assert.match((result as { ok: false; reason: string }).reason, /No project file tree available/);
});

// --- listDocPaths() ----------------------------------------------------------

test("listDocPaths(): lists every doc's resolved path + docId, depth-first", () => {
  const results = listDocPaths(makeTree());
  assert.deepEqual(results, [
    { path: "main.tex", docId: "maindoc000000000000000a" },
    { path: "chapters/intro.tex", docId: "introdoc0000000000000d" },
    { path: "chapters/conclusion.tex", docId: "conclusiondoc00000000e" },
    { path: "chapters/nested/deep.tex", docId: "deepdoc0000000000000g" },
  ]);
});

test("listDocPaths(): a null rootFolder returns an empty list, not a throw", () => {
  assert.deepEqual(listDocPaths(null), []);
});
