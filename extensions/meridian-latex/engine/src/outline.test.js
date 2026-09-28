import { test } from "node:test";
import assert from "node:assert/strict";
import { outlineText } from "./outline.js";

// outlineText (not a raw `parse` from unified-latex-util-parse + a separate
// extractOutline call) so every test here goes through the SAME natbib-aware
// parser outline.js's real callers (server.js's /outline) actually use --
// the plain unified-latex parse() doesn't know \citep/\citet at all (see
// outline.js's own header comment), and testing against it would silently
// validate a parse path nothing in production ever takes.
function outlineOf(source) {
  return outlineText(source);
}

// --- Heading title extraction (\section*, \section[short]{long}) --------
//
// Real, ALREADY-LIVE bug found 2026-09-18 via independent code review, then
// confirmed against dnabert_test_dummy's actual stored outline: every one
// of its 25 real headings (100% starred, per the earlier Phase-1 finding)
// had a corrupted title with a spurious leading "*" ("*Abstract", not
// "Abstract"). Every SECTION_MACROS member has ctan signature "s o m" (star
// flag, optional short title, mandatory title), so argText() -- which
// concatenates every arg -- folded the star flag's content into the title.
// Worse than cosmetic: the corrupted title seeds the popup's edit input, so
// submitting an unedited heading edit would write the stray "*" into the
// live document.

test("\\section*{...} (starred heading) extracts a clean title, no leading '*'", () => {
  const nodes = outlineOf("\\section*{Abstract}\n");
  const heading = nodes.find((n) => n.kind === "heading");
  assert.equal(heading.title, "Abstract");
});

test("\\subsection*{...} and \\chapter*{...} are also clean, not just \\section*", () => {
  const nodes = outlineOf("\\subsection*{Discussion}\n\\chapter*{Preface}\n");
  const titles = nodes.filter((n) => n.kind === "heading").map((n) => n.title);
  assert.deepEqual(titles, ["Discussion", "Preface"]);
});

test("\\section[Short]{Long Title} extracts the LONG title, not both concatenated", () => {
  const nodes = outlineOf("\\section[Short]{Long Discussion Title}\n");
  const heading = nodes.find((n) => n.kind === "heading");
  assert.equal(heading.title, "Long Discussion Title");
});

test("a starred heading WITH a short-title optional arg combines neither into the title", () => {
  const nodes = outlineOf("\\section*[Short]{Long Discussion Title}\n");
  const heading = nodes.find((n) => n.kind === "heading");
  assert.equal(heading.title, "Long Discussion Title");
});

test("an ordinary, unstarred heading with no optional arg is unaffected (no regression)", () => {
  const nodes = outlineOf("\\section{Discussion}\n");
  const heading = nodes.find((n) => n.kind === "heading");
  assert.equal(heading.title, "Discussion");
});

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

// Real bug found 2026-09-18 via independent code review, confirmed by
// reproduction: collectLabels incremented the shared "table"/"figure"
// counter for EVERY STRUCTURAL_ENVIRONMENTS match, including a nested
// tabular/subfigure sharing the SAME kind as its enclosing float -- so
// every real table (which always wraps a tabular, per both test papers)
// inflated the counter by 2 instead of 1, corrupting every \ref{} number
// after the first table/figure in the document.
test("a table wrapping a tabular does NOT inflate the table counter -- \\ref{} numbers stay correct", () => {
  const source = [
    "\\begin{table}",
    "\\begin{tabular}{lc}",
    "a & b \\\\",
    "\\end{tabular}",
    "\\caption{First table}",
    "\\label{tab:first}",
    "\\end{table}",
    "\\begin{table}",
    "\\begin{tabular}{lc}",
    "c & d \\\\",
    "\\end{tabular}",
    "\\caption{Second table, see Table~\\ref{tab:first} above.}",
    "\\label{tab:second}",
    "\\end{table}",
  ].join("\n");
  const nodes = outlineOf(source);
  const outerTables = nodes.filter((n) => n.kind === "table" && n.env === "table");
  assert.equal(outerTables.length, 2);
  // Without the fix this resolves to "Table~2" (each table counted twice --
  // once for `table`, once for its nested `tabular`).
  assert.match(outerTables[1].caption, /see Table~1 above\./);
});

test("a figure wrapping a subfigure does NOT inflate the figure counter", () => {
  const source = [
    "\\begin{figure}",
    "\\begin{subfigure}{0.4\\textwidth}",
    "\\caption{Sub A}",
    "\\label{fig:a}",
    "\\end{subfigure}",
    "\\caption{First figure}",
    "\\label{fig:first}",
    "\\end{figure}",
    "\\begin{figure}",
    "\\caption{Second figure, see Figure~\\ref{fig:first} above.}",
    "\\label{fig:second}",
    "\\end{figure}",
  ].join("\n");
  const nodes = outlineOf(source);
  const outerFigures = nodes.filter((n) => n.kind === "figure" && n.env === "figure");
  assert.equal(outerFigures.length, 2);
  assert.match(outerFigures[1].caption, /see Figure~1 above\./);
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

// --- subfigure/subtable (subcaption/subfig packages) ----------------------
//
// Real bug found 2026-09-18 via independent code review, confirmed by
// reproduction: subfigure/subtable were entirely missing from
// STRUCTURAL_ENVIRONMENTS, so a nested `\begin{subfigure}` inside an outer
// `\begin{figure}` was not recognized as a scope boundary -- the outer
// float's real caption/label were silently OVERWRITTEN by the subfigure's
// own (data loss, not a display quirk: the real outer caption/label never
// appeared anywhere in the output). Multi-panel figures/tables are common
// in real papers.

test("a subfigure's own caption/label is never attributed to the outer figure float (data-loss regression)", () => {
  const source = [
    "\\begin{figure}",
    "\\begin{subfigure}{0.4\\textwidth}",
    "\\caption{Sub A}",
    "\\label{fig:a}",
    "\\end{subfigure}",
    "\\caption{Overall figure}",
    "\\label{fig:overall}",
    "\\end{figure}",
  ].join("\n");
  const nodes = outlineOf(source);
  const figures = nodes.filter((n) => n.kind === "figure");
  assert.equal(figures.length, 2, "both the outer figure AND the subfigure must be their own nodes");
  const outer = figures.find((n) => n.env === "figure");
  const sub = figures.find((n) => n.env === "subfigure");
  assert.ok(outer, "outer figure node not found");
  assert.ok(sub, "subfigure node not found");
  assert.equal(outer.caption, "Overall figure", "the outer float's REAL caption must survive");
  assert.equal(outer.label, "fig:overall");
  assert.equal(sub.caption, "Sub A");
  assert.equal(sub.label, "fig:a");
});

test("a \\ref{} to the OUTER figure's label still resolves correctly with a nested subfigure present", () => {
  const source = [
    "\\begin{figure}",
    "\\begin{subfigure}{0.4\\textwidth}",
    "\\caption{Sub A}",
    "\\label{fig:a}",
    "\\end{subfigure}",
    "\\caption{Overall figure, see Figure~\\ref{fig:overall} for itself.}",
    "\\label{fig:overall}",
    "\\end{figure}",
  ].join("\n");
  const nodes = outlineOf(source);
  const outer = nodes.find((n) => n.kind === "figure" && n.env === "figure");
  assert.match(outer.caption, /see Figure~1 for itself/);
});

test("subtable behaves the same way as subfigure (not attributed to the outer table)", () => {
  const source = [
    "\\begin{table}",
    "\\begin{subtable}{0.4\\textwidth}",
    "\\caption{Sub table}",
    "\\label{tab:sub}",
    "\\end{subtable}",
    "\\caption{Overall table}",
    "\\label{tab:overall}",
    "\\end{table}",
  ].join("\n");
  const nodes = outlineOf(source);
  const tables = nodes.filter((n) => n.kind === "table");
  const outer = tables.find((n) => n.env === "table");
  const sub = tables.find((n) => n.env === "subtable");
  assert.equal(outer.caption, "Overall table");
  assert.equal(sub.caption, "Sub table");
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

// --- natbib author-year citation family (\citep, \citet, ...) -------------
//
// Real bug found 2026-09-18 via multi-project robustness testing against a
// genuinely different real paper (ooxml-graph-paper, which uses natbib
// throughout, unlike the original dnabert manuscript's bare \cite): every
// \citep{}/\citet{} call was SILENTLY invisible to the outline -- 0 citation
// nodes extracted from a document with 15+ real citations. Root cause: the
// old code only matched macro name "cite" (`node.content === "cite"`), and
// unified-latex's default parser doesn't know natbib's macros at all, so
// \citep{key}'s `{key}` group wasn't even attached to the macro node as an
// arg. It failed SILENTLY (0 results, no error, no partial/garbled output)
// rather than safely like the earlier starred-heading bug did -- worse, not
// better, since nothing signals anything went wrong.

test("\\citep (natbib) is recognized as a citation macro, same as \\cite", () => {
  const nodes = outlineOf("\\section{Intro}\nAs shown by \\citep{alice2020}.\n");
  const citations = nodes.filter((n) => n.kind === "citation");
  assert.equal(citations.length, 1);
  assert.equal(citations[0].key, "alice2020");
});

test("\\citet (natbib) is recognized as a citation macro", () => {
  const nodes = outlineOf("\\section{Intro}\nAlice \\citet{alice2020} showed...\n");
  const citations = nodes.filter((n) => n.kind === "citation");
  assert.equal(citations.length, 1);
  assert.equal(citations[0].key, "alice2020");
});

test("a multi-key \\citep{a,b,c} splits into one citation node per key, same as \\cite", () => {
  const nodes = outlineOf("\\section{Intro}\nSee \\citep{alice2020,bob2021,carol2022}.\n");
  const citations = nodes.filter((n) => n.kind === "citation");
  assert.deepEqual(citations.map((c) => c.key), ["alice2020", "bob2021", "carol2022"]);
});

test("\\citep[pre][post]{key} -- the optional pre/post note text is NOT concatenated into the key", () => {
  // Regression for a second, related latent bug this fix also had to avoid:
  // naively reusing argText() (which concatenates every arg) on a natbib
  // macro would fold the optional note's text into the key list itself
  // (e.g. "seealice2020"). lastArgText() must read only the final,
  // mandatory arg regardless of how many optional notes precede it.
  const nodes = outlineOf("\\section{Intro}\nSee \\citep[see][p.\\ 2]{alice2020}.\n");
  const citations = nodes.filter((n) => n.kind === "citation");
  assert.equal(citations.length, 1);
  assert.equal(citations[0].key, "alice2020");
});

test("the rest of the natbib author-year family is recognized: citealp, citealt, citeauthor, citeyear, citeyearpar", () => {
  const source = [
    "\\section{Intro}",
    "\\citealp{a2020} \\citealt{b2020} \\citeauthor{c2020} \\citeyear{d2020} \\citeyearpar{e2020}.",
  ].join("\n");
  const nodes = outlineOf(source);
  const keys = nodes.filter((n) => n.kind === "citation").map((c) => c.key);
  assert.deepEqual(keys, ["a2020", "b2020", "c2020", "d2020", "e2020"]);
});

test("capitalized natbib sentence-start variants (\\Citep, \\Citet, ...) are recognized too", () => {
  const source = "\\section{Intro}\n\\Citep{a2020} \\Citet{b2020} \\Citealp{c2020} \\Citealt{d2020} \\Citeauthor{e2020}.";
  const nodes = outlineOf(source);
  const keys = nodes.filter((n) => n.kind === "citation").map((c) => c.key);
  assert.deepEqual(keys, ["a2020", "b2020", "c2020", "d2020", "e2020"]);
});

test("a starred natbib variant (\\citep*{key}) still resolves to the plain key, not garbage from the star", () => {
  const nodes = outlineOf("\\section{Intro}\nSee \\citep*{alice2020}.\n");
  const citations = nodes.filter((n) => n.kind === "citation");
  assert.equal(citations.length, 1);
  assert.equal(citations[0].key, "alice2020");
});

test("bare \\cite is completely unaffected by the natbib macro registration (no regression)", () => {
  const nodes = outlineOf("\\section{Intro}\nSee \\cite{alice2020}.\n");
  const citations = nodes.filter((n) => n.kind === "citation");
  assert.equal(citations.length, 1);
  assert.equal(citations[0].key, "alice2020");
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

test("a nested tabular's OWN caption/label is never attributed to the outer table float", () => {
  const source = [
    "\\begin{table}",
    "\\caption{Outer float caption.}",
    "\\label{tab:outer}",
    "\\begin{tabular}{cc}",
    "\\caption{Inner tabular caption.}",
    "\\label{tab:inner}",
    "a & b \\\\",
    "\\end{tabular}",
    "\\end{table}",
  ].join("\n");
  const nodes = outlineOf(source);
  const tables = nodes.filter((n) => n.kind === "table");
  assert.equal(tables.length, 2);
  const outer = tables.find((n) => n.label === "tab:outer");
  const inner = tables.find((n) => n.label === "tab:inner");
  assert.ok(outer, "outer table node not found");
  assert.ok(inner, "inner tabular node not found");
  // The real bug this guards against: findFirstMacro's unscoped recursion
  // would have handed the OUTER node the INNER tabular's caption/label
  // (whichever comes first in a depth-first walk of the outer's content),
  // since both live inside the outer table's own `content` array.
  assert.equal(outer.caption, "Outer float caption.");
  assert.equal(inner.caption, "Inner tabular caption.");
  assert.notEqual(outer.caption, inner.caption);
});

test("captionLine/labelLine point at the field's OWN source line, not the environment's start line", () => {
  const source = [
    "\\begin{table}",       // line 1
    "\\centering",          // line 2
    "\\begin{tabular}{c}",  // line 3
    "x \\\\",               // line 4
    "\\end{tabular}",       // line 5
    "\\caption{A caption several lines in.}", // line 6
    "\\label{tab:deep}",    // line 7
    "\\end{table}",         // line 8
  ].join("\n");
  const nodes = outlineOf(source);
  const outer = nodes.find((n) => n.kind === "table" && n.label === "tab:deep");
  assert.ok(outer);
  assert.equal(outer.line, 1);
  assert.equal(outer.captionLine, 6);
  assert.equal(outer.labelLine, 7);
});

test("captionLine/labelLine are null when the field itself doesn't exist", () => {
  const source = ["\\begin{figure}", "\\includegraphics{plot.png}", "\\end{figure}"].join("\n");
  const nodes = outlineOf(source);
  const fig = nodes.find((n) => n.kind === "figure");
  assert.ok(fig);
  assert.equal(fig.caption, null);
  assert.equal(fig.captionLine, null);
  assert.equal(fig.label, null);
  assert.equal(fig.labelLine, null);
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
