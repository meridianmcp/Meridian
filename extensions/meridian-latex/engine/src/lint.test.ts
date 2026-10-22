import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { lintText, lintFile, type Finding } from "./lint.js";

function findingsFor(rule: string, findings: Finding[]): Finding[] {
  return findings.filter((f) => f.rule === rule);
}

function assertShape(finding: Finding): void {
  assert.equal(typeof finding.rule, "string");
  assert.ok(["error", "warning", "info"].includes(finding.severity), `unexpected severity ${finding.severity}`);
  assert.equal(typeof finding.message, "string");
  assert.ok(finding.message.length > 0);
  assert.ok(finding.line === null || typeof finding.line === "number", "line must be a number or null");
}

// --- citation-missing-bib-entry ----------------------------------------------

test("citation-missing-bib-entry: a \\cite{} key with no matching bib entry is flagged", () => {
  const source = "\\section{Intro}\nSee \\cite{nope2020}.\n";
  const findings = lintText(source);
  const hits = findingsFor("citation-missing-bib-entry", findings);
  assert.equal(hits.length, 1);
  assert.equal(hits[0].severity, "error");
  assert.match(hits[0].message, /nope2020/);
  assertShape(hits[0]);
});

test("citation-missing-bib-entry: a key WITH a matching \\bibitem is not flagged", () => {
  const source =
    "See \\cite{alice2020}.\n" +
    "\\begin{thebibliography}{9}\n\\bibitem{alice2020} Alice, 2020.\n\\end{thebibliography}\n";
  const findings = lintText(source);
  assert.deepEqual(findingsFor("citation-missing-bib-entry", findings), []);
});

test("citation-missing-bib-entry: a key resolved via bib_text (external .bib) is not flagged", () => {
  const source = "See \\cite{bob2021}.\n\\bibliography{refs}\n";
  const bibText = "@article{bob2021,\n  title = {A Bob Paper},\n  year = {2021}\n}\n";
  const findings = lintText(source, { bibText });
  assert.deepEqual(findingsFor("citation-missing-bib-entry", findings), []);
});

test("citation-missing-bib-entry: a '(no key)' bare \\cite{} is NOT double-reported here (empty-cite-key covers it)", () => {
  const findings = lintText("\\cite{}\n");
  assert.deepEqual(findingsFor("citation-missing-bib-entry", findings), []);
});

// --- ref-target-missing --------------------------------------------------------

test("ref-target-missing: \\ref/\\Cref/\\cref/\\eqref/\\autoref to a key with no \\label{} anywhere are all flagged", () => {
  const source = "\\ref{a}\n\\Cref{b}\n\\cref{c}\n\\eqref{d}\n\\autoref{e}\n";
  const findings = lintText(source);
  const hits = findingsFor("ref-target-missing", findings);
  assert.equal(hits.length, 5);
  for (const h of hits) assertShape(h);
  assert.deepEqual(
    hits.map((h) => h.message.match(/^\\(\w+)\{/)![1]),
    ["ref", "Cref", "cref", "eqref", "autoref"],
  );
});

test("ref-target-missing: a \\ref{} to a \\label{} that exists ANYWHERE (not just inside a table/figure/equation) resolves", () => {
  // \label{} sits on a bare \section, outside any STRUCTURAL_ENVIRONMENTS
  // node -- collectLabels() itself would never see this one; the new
  // document-wide walk must.
  const source = "\\section{Intro}\n\\label{sec:intro}\nSee \\ref{sec:intro}.\n";
  const findings = lintText(source);
  assert.deepEqual(findingsFor("ref-target-missing", findings), []);
});

test("ref-target-missing: a \\ref{} to a \\label{} inside a table/figure/equation also resolves", () => {
  const source = "\\begin{figure}\n\\label{fig:a}\n\\caption{x}\n\\end{figure}\nSee \\ref{fig:a}.\n";
  const findings = lintText(source);
  assert.deepEqual(findingsFor("ref-target-missing", findings), []);
});

// --- duplicate-label -----------------------------------------------------------

test("duplicate-label: the same \\label{} key defined twice is flagged once, pointing at the second occurrence", () => {
  const source = "\\label{dup}\ntext\n\\label{dup}\n";
  const findings = lintText(source);
  const hits = findingsFor("duplicate-label", findings);
  assert.equal(hits.length, 1);
  assert.equal(hits[0].severity, "error");
  assert.equal(hits[0].line, 3);
  assert.match(hits[0].message, /first defined on line 1/);
});

test("duplicate-label: a key defined three times produces two findings (one per occurrence after the first)", () => {
  const source = "\\label{k}\n\\label{k}\n\\label{k}\n";
  const findings = lintText(source);
  const hits = findingsFor("duplicate-label", findings);
  assert.equal(hits.length, 2);
  assert.deepEqual(hits.map((h) => h.line), [2, 3]);
});

test("duplicate-label: distinct labels are never flagged", () => {
  const source = "\\label{a}\n\\label{b}\n\\label{c}\n";
  assert.deepEqual(findingsFor("duplicate-label", lintText(source)), []);
});

// --- section-hierarchy-skip -----------------------------------------------------

test("section-hierarchy-skip: \\section straight to \\subsubsection (skipping \\subsection) is flagged", () => {
  const source = "\\section{A}\n\\subsubsection{B}\n";
  const findings = lintText(source);
  const hits = findingsFor("section-hierarchy-skip", findings);
  assert.equal(hits.length, 1);
  assert.equal(hits[0].severity, "warning");
  assert.equal(hits[0].line, 2);
  assert.match(hits[0].message, /skips 2 levels down from \\section/);
});

test("section-hierarchy-skip: a proper step-by-step descent is never flagged", () => {
  const source = "\\section{A}\n\\subsection{B}\n\\subsubsection{C}\n";
  assert.deepEqual(findingsFor("section-hierarchy-skip", lintText(source)), []);
});

test("section-hierarchy-skip: going UP the hierarchy (e.g. subsection back to section) is never flagged -- only downward skips count", () => {
  const source = "\\section{A}\n\\subsection{B}\n\\section{C}\n";
  assert.deepEqual(findingsFor("section-hierarchy-skip", lintText(source)), []);
});

test("section-hierarchy-skip: \\part straight to \\section (skipping \\chapter) is flagged", () => {
  const source = "\\part{A}\n\\section{B}\n";
  const hits = findingsFor("section-hierarchy-skip", lintText(source));
  assert.equal(hits.length, 1);
});

// --- unclosed-environment-or-parse-failure --------------------------------------

test("unclosed-environment-or-parse-failure: a thrown parse() error becomes the single finding, with no other checks running", () => {
  const parseImpl = (): never => {
    const err: Error & { location?: { start: { line: number; column: number } } } = new Error(
      'Expected "}" but end of input found.',
    );
    err.location = { start: { line: 7, column: 3 } };
    throw err;
  };
  const findings = lintText("\\section{unterminated\n", { parseImpl });
  assert.equal(findings.length, 1);
  assert.equal(findings[0].rule, "unclosed-environment-or-parse-failure");
  assert.equal(findings[0].severity, "error");
  assert.equal(findings[0].line, 7);
  assert.match(findings[0].message, /end of input/);
  assertShape(findings[0]);
});

test("unclosed-environment-or-parse-failure: a thrown error with no location/message still degrades to a clean finding, never a throw", () => {
  const parseImpl = (): never => {
    throw new Error();
  };
  assert.doesNotThrow(() => lintText("x", { parseImpl }));
  const findings = lintText("x", { parseImpl });
  assert.equal(findings.length, 1);
  assert.equal(findings[0].rule, "unclosed-environment-or-parse-failure");
  assert.equal(findings[0].line, null);
});

test("unclosed-environment-or-parse-failure: an error-shaped RECOVERED node (no throw) is also caught as the single finding", () => {
  // Simulates a hypothetical future parser version that recovers from a
  // malformed document by emitting an error-shaped node instead of throwing
  // -- this parser version doesn't do that today (see below), but the check
  // must handle it defensively either way.
  const parseImpl = () => ({
    content: [
      { type: "macro", content: "section", args: [{ content: [{ type: "string", content: "ok" }] }] },
      { type: "parseerror", message: "unexpected token", position: { start: { line: 3 } } },
    ],
  });
  const findings = lintText("irrelevant -- parseImpl is stubbed", { parseImpl });
  assert.equal(findings.length, 1);
  assert.equal(findings[0].rule, "unclosed-environment-or-parse-failure");
  assert.equal(findings[0].line, 3);
  assert.match(findings[0].message, /unexpected token/);
});

test("unclosed-environment-or-parse-failure: a non-string 'source' argument is a clean finding, never a throw", () => {
  assert.doesNotThrow(() => lintText(12345));
  const findings = lintText(undefined);
  assert.equal(findings.length, 1);
  assert.equal(findings[0].rule, "unclosed-environment-or-parse-failure");
});

test("unclosed-environment-or-parse-failure: never-throws-on-malformed-input contract -- the REAL parser on a battery of genuinely malformed .tex, no injected parseImpl", () => {
  // Confirmed empirically (not assumed) against this exact parser version:
  // it is extremely tolerant and recovers from every one of these without
  // throwing -- it just produces a different, non-"environment"-typed AST
  // shape instead (e.g. \begin{table} with no matching \end{table} decomposes
  // to a raw macro+group pair, not an environment node). This test's actual
  // job, regardless of which shape comes back, is the "never throws" contract
  // itself: lintText must always return an array of findings, never propagate
  // an exception, for any of these.
  const malformedInputs = [
    "\\begin{table}\n\\caption{x}\n", // unclosed environment, no \end at all
    "\\begin{table}\\end{figure}", // mismatched \begin/\end names
    "\\section{Intro", // unterminated mandatory-arg group
    "\\section{Intro}}", // stray unbalanced closing brace
    "This is $x + y broken\n", // unclosed inline math
    "\\[ x + y \n", // unclosed display math
    "\\begin{verbatim}\nhello\n", // unclosed verbatim
    "text \\", // lone trailing backslash at EOF
    "{{{{{{{{{{{{{{{{{{{{{{", // deeply unbalanced open braces
    "", // empty document
  ];
  for (const src of malformedInputs) {
    let findings: Finding[] = [];
    assert.doesNotThrow(() => {
      findings = lintText(src);
    }, `lintText threw on: ${JSON.stringify(src)}`);
    assert.ok(Array.isArray(findings), `lintText must always return an array for: ${JSON.stringify(src)}`);
    for (const f of findings) assertShape(f);
  }
});

// --- duplicate-bib-key -----------------------------------------------------------

test("duplicate-bib-key: the same \\bibitem key appearing twice is flagged, with line: null (bib entries carry no line info)", () => {
  const source =
    "\\begin{thebibliography}{9}\n" +
    "\\bibitem{dup} First.\n" +
    "\\bibitem{dup} Second.\n" +
    "\\end{thebibliography}\n";
  const hits = findingsFor("duplicate-bib-key", lintText(source));
  assert.equal(hits.length, 1);
  assert.equal(hits[0].severity, "warning");
  assert.equal(hits[0].line, null);
  assert.match(hits[0].message, /dup/);
});

test("duplicate-bib-key: a bibtex key duplicated across two @-entries in bib_text is flagged", () => {
  const bibText = "@article{dup, title = {A}, year = {2020}}\n@book{dup, title = {B}, year = {2021}}\n";
  const hits = findingsFor("duplicate-bib-key", lintText("\\bibliography{refs}\n", { bibText }));
  assert.equal(hits.length, 1);
});

test("duplicate-bib-key: distinct keys are never flagged", () => {
  const source = "\\begin{thebibliography}{9}\n\\bibitem{a} A.\n\\bibitem{b} B.\n\\end{thebibliography}\n";
  assert.deepEqual(findingsFor("duplicate-bib-key", lintText(source)), []);
});

// --- unused-bib-entry --------------------------------------------------------------

test("unused-bib-entry: a bib entry never cited anywhere is flagged as info", () => {
  const source = "\\begin{thebibliography}{9}\n\\bibitem{neverused} Nobody, 2020.\n\\end{thebibliography}\n";
  const hits = findingsFor("unused-bib-entry", lintText(source));
  assert.equal(hits.length, 1);
  assert.equal(hits[0].severity, "info");
  assert.match(hits[0].message, /neverused/);
  assert.equal(hits[0].line, null);
});

test("unused-bib-entry: a cited entry is never flagged", () => {
  const source =
    "See \\cite{alice2020}.\n" +
    "\\begin{thebibliography}{9}\n\\bibitem{alice2020} Alice, 2020.\n\\end{thebibliography}\n";
  assert.deepEqual(findingsFor("unused-bib-entry", lintText(source)), []);
});

test("unused-bib-entry: a duplicated-but-uncited key is only reported once here (de-duped), not once per duplicate occurrence", () => {
  const source = "\\begin{thebibliography}{9}\n\\bibitem{dup} A.\n\\bibitem{dup} B.\n\\end{thebibliography}\n";
  const hits = findingsFor("unused-bib-entry", lintText(source));
  assert.equal(hits.length, 1);
});

// --- empty-caption-or-label -----------------------------------------------------

test("empty-caption-or-label: a table/figure/equation environment with neither a \\caption nor a \\label is flagged", () => {
  const source = "\\begin{table}\nsome content, no caption or label\n\\end{table}\n";
  const hits = findingsFor("empty-caption-or-label", lintText(source));
  assert.equal(hits.length, 1);
  assert.equal(hits[0].severity, "warning");
  assert.match(hits[0].message, /\\begin\{table\}/);
});

test("empty-caption-or-label: a \\caption{} alone (no label) is enough to NOT be flagged", () => {
  const source = "\\begin{figure}\n\\caption{a figure}\n\\end{figure}\n";
  assert.deepEqual(findingsFor("empty-caption-or-label", lintText(source)), []);
});

test("empty-caption-or-label: a \\label{} alone (no caption) is also enough to NOT be flagged", () => {
  const source = "\\begin{equation}\n\\label{eq:a}\nx = y\n\\end{equation}\n";
  assert.deepEqual(findingsFor("empty-caption-or-label", lintText(source)), []);
});

test("empty-caption-or-label: a bare inline/display equation ($...$) is never flagged -- it has no caption/label concept at all", () => {
  const source = "This is $x + y$ inline math with nothing else.\n";
  assert.deepEqual(findingsFor("empty-caption-or-label", lintText(source)), []);
});

// --- empty-cite-key -----------------------------------------------------------

test("empty-cite-key: a bare \\cite{} with nothing between the braces is flagged as an error", () => {
  const hits = findingsFor("empty-cite-key", lintText("\\cite{}\n"));
  assert.equal(hits.length, 1);
  assert.equal(hits[0].severity, "error");
});

test("empty-cite-key: a \\cite{key} with a real key is never flagged", () => {
  assert.deepEqual(findingsFor("empty-cite-key", lintText("\\cite{realkey}\n")), []);
});

// --- stray-todo-fixme -----------------------------------------------------------

test("stray-todo-fixme: TODO/FIXME/XXX in plain prose are each found with the correct line number", () => {
  const source = "line one\nTODO: revisit this\nline three\nFIXME later\nXXX check this\n";
  const findings = lintText(source);
  const hits = findingsFor("stray-todo-fixme", findings);
  assert.equal(hits.length, 3);
  assert.deepEqual(hits.map((h) => h.line), [2, 4, 5]);
  for (const h of hits) assert.equal(h.severity, "info");
});

test("stray-todo-fixme: a marker inside a LaTeX %-comment is still found -- the parser strips comments from the AST entirely, so this must not rely on an AST string-leaf walk alone", () => {
  const source = "\\section{Intro}\n% TODO: rewrite this section\nBody text.\n";
  const hits = findingsFor("stray-todo-fixme", lintText(source));
  assert.equal(hits.length, 1);
  assert.equal(hits[0].line, 2);
});

test("stray-todo-fixme: 'todo' lowercase or as a substring of another word is NOT matched (word-boundary only)", () => {
  const source = "This is a todolist, not a marker.\nSee the methodology.\n";
  assert.deepEqual(findingsFor("stray-todo-fixme", lintText(source)), []);
});

test("stray-todo-fixme: multiple markers on the SAME line are each reported separately", () => {
  const hits = findingsFor("stray-todo-fixme", lintText("TODO and FIXME on one line\n"));
  assert.equal(hits.length, 2);
  assert.equal(hits[0].line, 1);
  assert.equal(hits[1].line, 1);
});

// --- unresolved-sub-label ---------------------------------------------------------

test("unresolved-sub-label: a \\subfigure's own \\label{} nested inside an outer \\figure is flagged as info", () => {
  const source =
    "\\begin{figure}\n" +
    "\\label{fig:outer}\n" +
    "\\begin{subfigure}\n" +
    "\\label{fig:sub}\n" +
    "\\end{subfigure}\n" +
    "\\caption{outer}\n" +
    "\\end{figure}\n";
  const hits = findingsFor("unresolved-sub-label", lintText(source));
  assert.equal(hits.length, 1);
  assert.equal(hits[0].severity, "info");
  assert.match(hits[0].message, /fig:sub/);
  assert.equal(hits[0].line, 4);
});

test("unresolved-sub-label: an ordinary figure with no nested same-kind environment is never flagged", () => {
  const source = "\\begin{figure}\n\\label{fig:a}\n\\caption{x}\n\\end{figure}\n";
  assert.deepEqual(findingsFor("unresolved-sub-label", lintText(source)), []);
});

test("unresolved-sub-label: subtable-in-table behaves the same way as subfigure-in-figure", () => {
  const source = "\\begin{table}\n\\begin{subtable}\n\\label{tab:sub}\n\\end{subtable}\n\\caption{x}\n\\end{table}\n";
  const hits = findingsFor("unresolved-sub-label", lintText(source));
  assert.equal(hits.length, 1);
});

// --- combined / integration -----------------------------------------------------

test("lintText: a document with several real issues produces the corresponding findings together, each with a well-formed shape", () => {
  const source =
    "\\section{Intro}\n" +
    "\\subsubsection{Too Deep}\n" +
    "See \\cite{missing2021}.\n" +
    "\\ref{fig:nope}\n" +
    "\\label{dup}\n" +
    "\\label{dup}\n" +
    "% TODO: fix this later\n" +
    "\\begin{figure}\n\\end{figure}\n" +
    "\\begin{thebibliography}{9}\n" +
    "\\bibitem{alice2020} Alice, 2020.\n" +
    "\\bibitem{alice2020} Alice again, 2020.\n" +
    "\\end{thebibliography}\n";
  const findings = lintText(source);
  const rules = new Set(findings.map((f) => f.rule));
  assert.ok(rules.has("citation-missing-bib-entry"));
  assert.ok(rules.has("ref-target-missing"));
  assert.ok(rules.has("duplicate-label"));
  assert.ok(rules.has("section-hierarchy-skip"));
  assert.ok(rules.has("duplicate-bib-key"));
  assert.ok(rules.has("empty-caption-or-label"));
  assert.ok(rules.has("stray-todo-fixme"));
  for (const f of findings) assertShape(f);
});

test("lintText: a clean, well-formed document produces no findings at all", () => {
  const source =
    "\\section{Intro}\n" +
    "\\subsection{Background}\n" +
    "See \\cite{alice2020}.\n" +
    "\\begin{figure}\n\\label{fig:a}\n\\caption{A figure}\n\\end{figure}\n" +
    "As shown in \\ref{fig:a}.\n" +
    "\\begin{thebibliography}{9}\n\\bibitem{alice2020} Alice, 2020.\n\\end{thebibliography}\n";
  assert.deepEqual(lintText(source), []);
});

// --- lintFile -----------------------------------------------------------------

test("lintFile: lints a real local .tex file on disk via readFileSync + lintText", () => {
  const dir = mkdtempSync(join(tmpdir(), "meridian-latex-lint-test-"));
  try {
    const texPath = join(dir, "paper.tex");
    writeFileSync(texPath, "\\cite{}\n");
    const findings = lintFile(texPath);
    assert.ok(findings.some((f) => f.rule === "empty-cite-key"));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("lintFile: passes options (e.g. bibText) through to lintText unchanged", () => {
  const dir = mkdtempSync(join(tmpdir(), "meridian-latex-lint-test-"));
  try {
    const texPath = join(dir, "paper.tex");
    writeFileSync(texPath, "See \\cite{bob2021}.\n\\bibliography{refs}\n");
    const bibText = "@article{bob2021,\n  title = {A Bob Paper},\n  year = {2021}\n}\n";
    const findings = lintFile(texPath, { bibText });
    assert.deepEqual(findingsFor("citation-missing-bib-entry", findings), []);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("lintFile: a nonexistent file's readFileSync ENOENT propagates as a real throw (unlike lintText's own tolerant contract -- matches outlineFile's identical, already-established behavior; the MCP tool layer is what converts this to a structured error, see mcp-server.test.js)", () => {
  assert.throws(() => lintFile(join(tmpdir(), "definitely-does-not-exist-meridian-latex-lint.tex")));
});
