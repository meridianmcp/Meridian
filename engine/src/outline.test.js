import { test } from "node:test";
import assert from "node:assert/strict";
import { parse } from "@unified-latex/unified-latex-util-parse";
import { extractOutline } from "./outline.js";

function outlineOf(source) {
  return extractOutline(parse(source));
}

test("splits a multi-key \\cite into one citation node per key", () => {
  const nodes = outlineOf("\\section{Intro}\nSee \\cite{alice2020,bob2021,carol2022}.\n");
  const citations = nodes.filter((n) => n.kind === "citation");
  assert.equal(citations.length, 3);
  assert.deepEqual(citations.map((c) => c.key), ["alice2020", "bob2021", "carol2022"]);
});

test("single-key \\cite still produces exactly one citation node", () => {
  const nodes = outlineOf("\\section{Intro}\nSee \\cite{alice2020}.\n");
  const citations = nodes.filter((n) => n.kind === "citation");
  assert.equal(citations.length, 1);
  assert.equal(citations[0].key, "alice2020");
});

test("resolves a \\ref{} inside a caption to the referenced table's sequential number", () => {
  const source = [
    "\\begin{table}",
    "\\caption{See Table~\\ref{tab:second} for more.}",
    "\\label{tab:first}",
    "\\end{table}",
    "\\begin{table}",
    "\\caption{Second table.}",
    "\\label{tab:second}",
    "\\end{table}",
  ].join("\n");
  const nodes = outlineOf(source);
  const tables = nodes.filter((n) => n.kind === "table");
  assert.equal(tables.length, 2);
  // The first table's caption refers FORWARD to the second table (label
  // defined later in the document) -- this must still resolve, since
  // collectLabels runs a full pass before any caption is rendered.
  assert.match(tables[0].caption, /See Table~2 for more\./);
});

test("an unresolved \\ref{} renders as a marked placeholder, never silently drops", () => {
  const source = [
    "\\begin{figure}",
    "\\caption{See Figure~\\ref{fig:does-not-exist}.}",
    "\\end{figure}",
  ].join("\n");
  const nodes = outlineOf(source);
  const figures = nodes.filter((n) => n.kind === "figure");
  assert.equal(figures.length, 1);
  assert.match(figures[0].caption, /See Figure~\[\?fig:does-not-exist\]\./);
});

test("a citation nested inside a table/figure environment is still found, not dropped", () => {
  const source = [
    "\\begin{table}",
    "\\caption{A table.}",
    "Data from \\cite{someone2020}.",
    "\\end{table}",
  ].join("\n");
  const nodes = outlineOf(source);
  const tables = nodes.filter((n) => n.kind === "table");
  const citations = nodes.filter((n) => n.kind === "citation");
  assert.equal(tables.length, 1);
  assert.equal(citations.length, 1);
  assert.equal(citations[0].key, "someone2020");
});

test("a nested tabular inside a table float is now its own addressable node (documented behavior change)", () => {
  const source = [
    "\\begin{table}",
    "\\caption{Outer float.}",
    "\\begin{tabular}{cc}",
    "a & b \\\\",
    "\\end{tabular}",
    "\\end{table}",
  ].join("\n");
  const nodes = outlineOf(source);
  const tableKindNodes = nodes.filter((n) => n.kind === "table");
  // One node for the outer `table` float, one for the inner `tabular` layout
  // -- both are real, distinct AST nodes, and a future cell/column-edit
  // operation needs to address the tabular specifically.
  assert.equal(tableKindNodes.length, 2);
});

test("two untitled same-level headings collide on content fingerprint and get disambiguated, not merged", () => {
  const source = ["\\section{}", "\\section{}"].join("\n");
  const nodes = outlineOf(source);
  const headings = nodes.filter((n) => n.kind === "heading");
  assert.equal(headings.length, 2);
  assert.equal(headings[0].title, "(untitled)");
  assert.equal(headings[1].title, "(untitled)");
  // Both hash identically (same kind + level + "(untitled)"), so they are
  // genuinely the same content fingerprint -- the second must get a
  // disambiguating suffix rather than silently sharing an id with the
  // first (which would make them indistinguishable to any caller).
  assert.notEqual(headings[0].id, headings[1].id);
  assert.equal(headings[1].id, `${headings[0].id}-dup2`);
});

test("a table/figure with neither caption nor label falls back to a position-derived id", () => {
  const source = ["\\begin{figure}", "\\includegraphics{plot.png}", "\\end{figure}"].join("\n");
  const nodes = outlineOf(source);
  const figures = nodes.filter((n) => n.kind === "figure");
  assert.equal(figures.length, 1);
  assert.equal(figures[0].caption, null);
  assert.equal(figures[0].label, null);
  // Documented v1 fallback: no stable content to hash, so the id carries
  // the positional-fallback marker instead of a real fingerprint.
  assert.match(figures[0].id, /^figure:pos:L\d+:\d+$/);
});

test("headings, equations, and figures without refs are unaffected", () => {
  const source = [
    "\\section{Methods}",
    "\\begin{equation}",
    "x = y",
    "\\end{equation}",
    "\\begin{figure}",
    "\\caption{A figure with no cross-references.}",
    "\\end{figure}",
  ].join("\n");
  const nodes = outlineOf(source);
  assert.equal(nodes.filter((n) => n.kind === "heading").length, 1);
  assert.equal(nodes.filter((n) => n.kind === "equation").length, 1);
  const figures = nodes.filter((n) => n.kind === "figure");
  assert.equal(figures.length, 1);
  assert.equal(figures[0].caption, "A figure with no cross-references.");
});
