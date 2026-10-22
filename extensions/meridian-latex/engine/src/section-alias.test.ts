import { test } from "node:test";
import assert from "node:assert/strict";
import { expandSectionAliases } from "./section-alias.js";

test("expandSectionAliases: basic single-alias case -- definition gone, use rewritten", () => {
  const source = "\\newcommand{\\mysection}[1]{\\section{#1}}\n\\mysection{Foo}\n";
  const result = expandSectionAliases(source);
  assert.equal(result.includes("\\newcommand{\\mysection}"), false);
  assert.equal(result.includes("\\mysection{"), false);
  assert.equal(result.includes("\\section{Foo}"), true);
});

test("expandSectionAliases: an alias used multiple times -- every use rewritten", () => {
  const source =
    "\\newcommand{\\mysection}[1]{\\section{#1}}\n" +
    "\\mysection{Intro}\nSome text.\n\\mysection{Methods}\nMore text.\n\\mysection{Results}\n";
  const result = expandSectionAliases(source);
  assert.equal(result.includes("\\mysection"), false);
  assert.equal((result.match(/\\section\{/g) || []).length, 3);
  assert.equal(result.includes("\\section{Intro}"), true);
  assert.equal(result.includes("\\section{Methods}"), true);
  assert.equal(result.includes("\\section{Results}"), true);
});

test("expandSectionAliases: no alias-shaped \\newcommand at all -- returned completely unchanged", () => {
  const source = "\\section{Introduction}\nSome plain text with no macros of interest.\n";
  const result = expandSectionAliases(source);
  assert.strictEqual(result, source);
});

test("expandSectionAliases: a \\newcommand not referencing #1 (zero-arg macro) is left alone", () => {
  const source = "\\newcommand{\\mysection}{\\section{Fixed Title}}\n\\mysection\n";
  const result = expandSectionAliases(source);
  assert.strictEqual(result, source);
});

test(
  "expandSectionAliases: best-effort #1 substring check -- matches the Python port's own " +
    "looseness rather than being stricter. The Python source's own doc comment for " +
    "_expand_section_macros says plainly: 'Full TeX macro expansion is out of scope; this " +
    "handles the common single-argument section-alias case.' Its alias detection is a bare " +
    "`\"#1\" in body` substring check, not a check that #1 is actually threaded into the " +
    "\\section{} argument itself -- so a body where #1 appears elsewhere (e.g. in a comment " +
    "or unrelated text) still counts as long as some section macro also appears in the body. " +
    "This test documents that intentional looseness being preserved, not a bug in this port.",
  () => {
    const source = "\\newcommand{\\weird}[1]{\\section{Fixed} % note: #1 is unused here\n}\n\\weird{ignored}\n";
    const result = expandSectionAliases(source);
    // Per the Python port's loose substring check, this DOES count as an alias
    // (body contains both a section macro and the literal "#1" somewhere).
    assert.equal(result.includes("\\weird"), false);
    assert.equal(result.includes("\\section{ignored}"), true);
  },
);

test("expandSectionAliases: word-boundary safety -- \\mysectionx is not affected by a \\mysection alias", () => {
  const source = "\\newcommand{\\mysection}[1]{\\section{#1}}\n\\mysectionx{Untouched}\n\\mysection{Touched}\n";
  const result = expandSectionAliases(source);
  assert.equal(result.includes("\\mysectionx{Untouched}"), true);
  assert.equal(result.includes("\\section{Touched}"), true);
  assert.equal(result.includes("\\mysection{Touched}"), false);
});

test("expandSectionAliases: starred \\newcommand* form is recognized", () => {
  const source = "\\newcommand*{\\mysection}[1]{\\section{#1}}\n\\mysection{Starred}\n";
  const result = expandSectionAliases(source);
  assert.equal(result.includes("\\newcommand*{\\mysection}"), false);
  assert.equal(result.includes("\\section{Starred}"), true);
});

test("expandSectionAliases: \\renewcommand form (redefining an existing macro) is recognized", () => {
  const source = "\\renewcommand{\\mysection}[1]{\\subsection{#1}}\n\\mysection{Redefined}\n";
  const result = expandSectionAliases(source);
  assert.equal(result.includes("\\renewcommand{\\mysection}"), false);
  assert.equal(result.includes("\\subsection{Redefined}"), true);
});

test("expandSectionAliases: nested braces inside the newcommand body still brace-match correctly", () => {
  const source =
    "\\newcommand{\\mysection}[1]{\\section{#1 (\\textbf{bold} and {grouped} text)}}\n\\mysection{Title}\n";
  const result = expandSectionAliases(source);
  assert.equal(result.includes("\\newcommand{\\mysection}"), false);
  // The whole aliased body -- including its own nested braces -- is removed
  // as ONE balanced unit; only the actual use site remains, rewritten.
  assert.equal(result.includes("\\textbf{bold}"), false);
  assert.equal(result.includes("\\section{Title}"), true);
});

test("expandSectionAliases: multiple different aliases in one source, each rewritten to its own target", () => {
  const source =
    "\\newcommand{\\mychapter}[1]{\\chapter{#1}}\n" +
    "\\newcommand{\\mysubsection}[1]{\\subsection{#1}}\n" +
    "\\mychapter{Intro}\n\\mysubsection{Details}\n";
  const result = expandSectionAliases(source);
  assert.equal(result.includes("\\mychapter"), false);
  assert.equal(result.includes("\\mysubsection"), false);
  assert.equal(result.includes("\\chapter{Intro}"), true);
  assert.equal(result.includes("\\subsection{Details}"), true);
});

test("expandSectionAliases: a definition with an unbalanced/unterminated body does not throw", () => {
  const source = "\\newcommand{\\mysection}[1]{\\section{#1}\nNo closing brace for the definition body";
  assert.doesNotThrow(() => expandSectionAliases(source));
});
