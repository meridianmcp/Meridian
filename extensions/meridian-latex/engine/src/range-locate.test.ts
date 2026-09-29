import { test } from "node:test";
import assert from "node:assert/strict";
import {
  matchBraceIndex,
  findAllBraceArgs,
  findMacroBraceArgs,
  findCiteSegments,
  siblingsOnLine,
  siblingsOnFieldLine,
  locateHeadingRange,
  locateCitationRange,
  locateCaptionRange,
  locateLabelRange,
  fieldLineNumber,
  resolveFieldRange,
} from "./range-locate.js";

test("matchBraceIndex: finds the OUTER closing brace across nested braces", () => {
  const text = "\\section{A \\textbf{B}}";
  const openIdx = text.indexOf("{");
  assert.equal(matchBraceIndex(text, openIdx), text.length - 1);
});

test("matchBraceIndex: -1 for an unbalanced brace", () => {
  const text = "\\section{unterminated";
  assert.equal(matchBraceIndex(text, text.indexOf("{")), -1);
});

test("findMacroBraceArgs: matches both starred and unstarred occurrences", () => {
  const text = "\\section*{Abstract} then \\subsection{Methods}";
  const abs = findMacroBraceArgs(text, "section");
  assert.equal(abs.length, 1);
  assert.equal(text.slice(abs[0].start, abs[0].end), "Abstract");
});

test("findMacroBraceArgs: does not false-match a longer macro name sharing a prefix", () => {
  const text = "\\subsectionfoo{X}";
  assert.deepEqual(findMacroBraceArgs(text, "subsection"), []);
});

test("findAllBraceArgs: multiple \\caption{...} occurrences left to right", () => {
  const text = "\\caption{First} ... \\caption{Second}";
  const occ = findAllBraceArgs(text, "\\caption{");
  assert.equal(occ.length, 2);
  assert.equal(text.slice(occ[0].start, occ[0].end), "First");
  assert.equal(text.slice(occ[1].start, occ[1].end), "Second");
});

test("findCiteSegments: splits a multi-key \\cite{} and trims whitespace, keeping offsets", () => {
  const text = "See \\cite{foo, bar , baz}.";
  const segs = findCiteSegments(text);
  assert.deepEqual(
    segs.map((s) => s.text),
    ["foo", "bar", "baz"],
  );
  for (const seg of segs) {
    assert.equal(text.slice(seg.start, seg.end), seg.text);
  }
});

test("findCiteSegments: multiple \\cite{} occurrences flatten into one ordered list", () => {
  const text = "\\cite{a} and \\cite{b,c}";
  const segs = findCiteSegments(text);
  assert.deepEqual(
    segs.map((s) => s.text),
    ["a", "b", "c"],
  );
});

test("siblingsOnLine: same kind + line, headings also require matching level", () => {
  const nodes = [
    { id: "h1", kind: "heading", level: "section", line: 5 },
    { id: "h2", kind: "heading", level: "subsection", line: 5 },
    { id: "h3", kind: "heading", level: "section", line: 5 },
    { id: "c1", kind: "citation", line: 5 },
  ];
  const result = siblingsOnLine(nodes, { kind: "heading", level: "section", line: 5 });
  assert.deepEqual(
    result.map((n) => n.id),
    ["h1", "h3"],
  );
});

test("siblingsOnFieldLine: matches nodes sharing the same non-null field line", () => {
  const nodes = [
    { id: "t1", captionLine: 10 },
    { id: "t2", captionLine: 10 },
    { id: "t3", captionLine: null },
    { id: "t4", captionLine: 20 },
  ];
  const result = siblingsOnFieldLine(nodes, { captionLine: 10 }, "captionLine");
  assert.deepEqual(
    result.map((n) => n.id),
    ["t1", "t2"],
  );
});

test("locateHeadingRange: resolves the Nth heading occurrence to its absolute offset, tolerates rendered/raw mismatch", () => {
  const lineInfo = { from: 100, to: 140, text: "\\section{A \\textit{B}}" };
  const target = { id: "h1", kind: "heading", level: "section", line: 7, title: "A B" };
  const siblings = [target];
  const range = locateHeadingRange(lineInfo, target, siblings);
  assert.equal(range.ok, true);
  const argStart = lineInfo.text.indexOf("{") + 1;
  const argEnd = lineInfo.text.length - 1;
  assert.equal(range.from, lineInfo.from + argStart);
  assert.equal(range.to, lineInfo.from + argEnd);
});

test("locateHeadingRange: aborts on a heading-count mismatch instead of guessing", () => {
  const lineInfo = { from: 0, to: 20, text: "\\section{Only one}" };
  const target = { id: "h1", kind: "heading", level: "section", line: 1 };
  const siblings = [target, { id: "h2", kind: "heading", level: "section", line: 1 }]; // outline claims 2, text has 1
  const range = locateHeadingRange(lineInfo, target, siblings);
  assert.equal(range.ok, false);
  assert.match(range.reason, /count mismatch/);
});

test("locateCitationRange: resolves the correct key by position and hard-aborts on text mismatch", () => {
  const lineInfo = { from: 50, to: 80, text: "\\cite{foo,bar}" };
  const target = { id: "c2", kind: "citation", line: 3, key: "bar" };
  const siblings = [
    { id: "c1", kind: "citation", line: 3, key: "foo" },
    target,
  ];
  const ok = locateCitationRange(lineInfo, target, siblings);
  assert.equal(ok.ok, true);
  assert.equal(lineInfo.text.slice(ok.from - lineInfo.from, ok.to - lineInfo.from), "bar");

  const mismatched = locateCitationRange(lineInfo, { ...target, key: "WRONG" }, [
    { id: "c1", kind: "citation", line: 3, key: "foo" },
    { ...target, key: "WRONG" },
  ]);
  assert.equal(mismatched.ok, false);
  assert.match(mismatched.reason, /key mismatch/);
});

test("locateCaptionRange: resolves by scoped occurrence count and node property text", () => {
  const lineInfo = { from: 200, to: 230, text: "  \\caption{A neat table}" };
  const target = { id: "t1", kind: "table", captionLine: 12, caption: "A neat table" };
  const nodes = [target];
  const range = locateCaptionRange(lineInfo, target, nodes);
  assert.equal(range.ok, true);
  assert.equal(
    lineInfo.text.slice(range.from - lineInfo.from, range.to - lineInfo.from),
    "A neat table",
  );
});

test("locateLabelRange: hard-aborts when the live text no longer matches the outline's recorded label", () => {
  const lineInfo = { from: 0, to: 30, text: "\\label{eq:stale}" };
  const target = { id: "e1", kind: "equation", labelLine: 4, label: "eq:fresh" };
  const range = locateLabelRange(lineInfo, target, [target]);
  assert.equal(range.ok, false);
  assert.match(range.reason, /text mismatch/);
});

test("fieldLineNumber: heading/citation always use `line`, others use the field-specific line", () => {
  assert.equal(fieldLineNumber({ kind: "heading", line: 9 }, "caption"), 9);
  assert.equal(fieldLineNumber({ kind: "citation", line: 4 }, "label"), 4);
  assert.equal(fieldLineNumber({ kind: "table", captionLine: 11, labelLine: null }, "caption"), 11);
  assert.equal(fieldLineNumber({ kind: "table", captionLine: 11, labelLine: null }, "label"), null);
});

test("resolveFieldRange: rejects an unsupported field/kind combination without throwing", () => {
  const lineInfo = { from: 0, to: 10, text: "\\label{x}" };
  const target = { kind: "equation", labelLine: 1, label: "x" };
  const result = resolveFieldRange(lineInfo, target, [target], "title");
  assert.equal(result.ok, false);
  assert.match(result.reason, /not supported/);
});
