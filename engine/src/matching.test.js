import { test } from "node:test";
import assert from "node:assert/strict";
import { parse } from "@unified-latex/unified-latex-util-parse";
import { extractOutline } from "./outline.js";
import { matchOutlines } from "./matching.js";

function outlineOf(source) {
  return extractOutline(parse(source));
}

test("identical document: every node matches, nothing added or removed", () => {
  const source = [
    "\\section{Introduction}",
    "See \\cite{alice2020}.",
    "\\begin{table}",
    "\\caption{Results.}",
    "\\label{tab:results}",
    "\\end{table}",
    "\\section{Methods}",
  ].join("\n");
  const oldNodes = outlineOf(source);
  const newNodes = outlineOf(source);

  const { matched, added, removed } = matchOutlines(oldNodes, newNodes);

  assert.equal(matched.length, oldNodes.length);
  assert.equal(added.length, 0);
  assert.equal(removed.length, 0);
  for (const { oldId, newId } of matched) {
    assert.equal(oldId, newId, "an unchanged node's id should be byte-identical across both parses");
  }
});

test("a retitled heading matches via LCS alignment, not delete+add", () => {
  const oldSource = [
    "\\section{Introduction}",
    "\\section{Methods}",
    "\\section{Results}",
  ].join("\n");
  const newSource = [
    "\\section{Background}", // retitled from "Introduction"
    "\\section{Methods}",
    "\\section{Results}",
  ].join("\n");

  const oldNodes = outlineOf(oldSource);
  const newNodes = outlineOf(newSource);
  const { matched, added, removed } = matchOutlines(oldNodes, newNodes);

  // All three headings should be recognized as matched -- including the
  // retitled one -- with nothing left over as a spurious add/remove.
  assert.equal(matched.length, 3);
  assert.equal(added.length, 0);
  assert.equal(removed.length, 0);

  const retitled = matched.find((m) => m.node.title === "Background");
  assert.ok(retitled, "the retitled heading should appear in matched, not removed+added");
  const oldIntro = oldNodes.find((n) => n.title === "Introduction");
  assert.equal(retitled.oldId, oldIntro.id);
  assert.notEqual(retitled.oldId, retitled.newId, "content fingerprint should differ after a retitle");
});

test("a new section inserted in the middle does not cascade into delete+add for later nodes", () => {
  const oldSource = [
    "\\section{Introduction}",
    "\\section{Methods}",
    "\\section{Results}",
  ].join("\n");
  const newSource = [
    "\\section{Introduction}",
    "\\section{Background}", // newly inserted, shifts everything after it
    "\\section{Methods}",
    "\\section{Results}",
  ].join("\n");

  const oldNodes = outlineOf(oldSource);
  const newNodes = outlineOf(newSource);
  const { matched, added, removed } = matchOutlines(oldNodes, newNodes);

  // This is the exact v0 bug: a purely positional id would have reassigned
  // Methods/Results' ids too (their line numbers shifted), making this look
  // like 3 deletions + 4 additions. With content-fingerprinted ids, only
  // the genuinely new section should show up as added.
  assert.equal(added.length, 1);
  assert.equal(added[0].title, "Background");
  assert.equal(removed.length, 0);
  assert.equal(matched.length, 3);
  // Introduction/Methods/Results must have matched via the id join alone
  // (identical fingerprint before and after), not the LCS fallback.
  for (const { oldId, newId } of matched) {
    assert.equal(oldId, newId);
  }
});

test("a genuinely deleted node is reported as removed, not matched", () => {
  const oldSource = [
    "\\section{Introduction}",
    "\\section{Methods}",
    "\\section{Results}",
  ].join("\n");
  const newSource = [
    "\\section{Introduction}",
    "\\section{Results}",
  ].join("\n");

  const oldNodes = outlineOf(oldSource);
  const newNodes = outlineOf(newSource);
  const { matched, added, removed } = matchOutlines(oldNodes, newNodes);

  assert.equal(matched.length, 2);
  assert.equal(added.length, 0);
  assert.equal(removed.length, 1);
  assert.equal(removed[0].title, "Methods");
});

test("a genuinely added node is reported as added, not matched", () => {
  const oldSource = ["\\section{Introduction}"].join("\n");
  const newSource = [
    "\\section{Introduction}",
    "\\section{Conclusion}",
  ].join("\n");

  const oldNodes = outlineOf(oldSource);
  const newNodes = outlineOf(newSource);
  const { matched, added, removed } = matchOutlines(oldNodes, newNodes);

  assert.equal(matched.length, 1);
  assert.equal(removed.length, 0);
  assert.equal(added.length, 1);
  assert.equal(added[0].title, "Conclusion");
});

test("mixed kinds: a citation and a table changing independently don't cross-contaminate alignment", () => {
  const oldSource = [
    "\\section{Intro}",
    "See \\cite{alice2020}.",
    "\\begin{table}",
    "\\caption{Old caption.}",
    "\\end{table}",
  ].join("\n");
  const newSource = [
    "\\section{Intro}",
    "See \\cite{bob2021}.", // citation key changed
    "\\begin{table}",
    "\\caption{New caption.}", // table caption changed, no label either time
    "\\end{table}",
  ].join("\n");

  const oldNodes = outlineOf(oldSource);
  const newNodes = outlineOf(newSource);
  const { matched, added, removed } = matchOutlines(oldNodes, newNodes);

  // The heading matches by id (unchanged). The citation and the table both
  // changed their only fingerprinted content, so neither matches by id --
  // but each is the sole remaining node of its kind, so LCS alignment
  // (kind-scoped) should still pair citation-with-citation and
  // table-with-table, not cross the kinds or report both as delete+add.
  assert.equal(added.length, 0);
  assert.equal(removed.length, 0);
  assert.equal(matched.length, 3);

  const citationMatch = matched.find((m) => m.node.kind === "citation");
  assert.equal(citationMatch.node.key, "bob2021");
  const tableMatch = matched.find((m) => m.node.kind === "table");
  assert.equal(tableMatch.node.caption, "New caption.");
});
