// Static-AST lint suite for a .tex document -- 11 checks over structure,
// cross-references, and bibliography hygiene. Reuses outline.js's existing
// parser/traversal helpers (parse(), extractOutline(), collectLabels(),
// lastArgText(), macroLine()) and bibliography.js's getBibliography()
// rather than reimplementing AST logic -- see each check's own comment for
// exactly which of those it builds on and why.
//
// Findings shape: `{rule, severity, message, line, nodeId?}`. `severity` is
// one of "error" | "warning" | "info". `line` is a 1-indexed source line, or
// `null` when the underlying data has no line info attached (e.g. a bib
// entry parsed by bibliography.js, which doesn't track source position --
// see that file's own header for why it's a regex-based, not AST-based,
// extractor).
//
// Tolerant-of-malformed-input contract, matching bibliography.js's own
// (see its header): this module NEVER throws to the caller. A parse()
// failure -- a thrown SyntaxError, or (defensively, should this parser
// version ever start emitting one) an error-shaped recovered node -- becomes
// the SINGLE `unclosed-environment-or-parse-failure` finding, and every
// other check is skipped for that call (there is no valid AST left to run
// them against). Anything else that throws partway through is caught by the
// same outer try/catch and degrades to that identical finding, rather than
// propagating.

import { readFileSync } from "node:fs";
import type * as Ast from "@unified-latex/unified-latex-types";
import { parse, extractOutline, collectLabels, lastArgText, macroLine, type OutlineNode, type NestedSameKindLabel } from "./outline.js";
import { getBibliography, type BibliographyEntry } from "./bibliography.js";

/** One lint finding. `line` is `null` when the underlying data carries no
 * source position (see this file's own header). `nodeId` is only present
 * for findings anchored to a specific outline node. */
export interface Finding {
  rule: string;
  severity: "error" | "warning" | "info";
  message: string;
  line: number | null;
  nodeId?: string;
}

/** \ref-family macros this engine checks cross-references for. Confirmed by
 * direct inspection of unified-latex-ctan's bundled macro-info database
 * (the same one outline.js's own header comment describes having to PATCH
 * for natbib's \citep/\citet family) that \ref/\Cref/\cref/\eqref/\autoref
 * are ALL already registered there with a real, working signature -- each
 * parses to a proper `macro` node with the key as its LAST arg slot, same
 * convention CITATION_MACROS/lastArgText already rely on -- so, unlike
 * citep/citet, none of these needed a NATBIB_CITATION_SIGNATURE-style patch
 * here. lastArgText (not argText) is still the right extraction call: it is
 * the last-slot-wins convention this whole file already uses for
 * citations/headings, and it stays correct even if a future macro-info
 * update ever adds real content to one of \Cref/\cref's leading optional
 * slots (argText would silently start concatenating it into the key, the
 * exact class of bug lastArgText was introduced to prevent -- see
 * outline.js's own comment on lastArgText). */
const REF_MACROS = new Set(["ref", "Cref", "cref", "eqref", "autoref"]);

/**
 * A document-wide macro walk: every macro node anywhere in the AST whose
 * name is in `names`, with no STRUCTURAL_ENVIRONMENTS scoping boundary --
 * deliberately NEW logic (not a call to outline.js's findFirstMacro/
 * findFirstMacroInOwnScope), because both of those either stop at the FIRST
 * match or stop at a structural-environment boundary, and ref-target-missing/
 * duplicate-label need EVERY \label{}/\ref{}-family occurrence in the whole
 * document, including ones that live outside any table/figure/equation (a
 * \label{} on a \section, or a bare \ref{} in running prose) -- exactly the
 * gap collectLabels() itself documents as out of its own scope. Traversal
 * shape (content + args) mirrors collectLabels'/extractOutline's own `visit`
 * functions in outline.js, reusing their established walk pattern rather
 * than inventing a third one.
 */
function collectMacrosDocumentWide(root: Ast.Node[] | Ast.Node | undefined, names: Set<string>): Ast.Macro[] {
  const found: Ast.Macro[] = [];
  function visit(node: Ast.Node[] | Ast.Node | undefined): void {
    if (Array.isArray(node)) {
      for (const n of node) visit(n);
      return;
    }
    if (!node || typeof node !== "object") return;
    if (node.type === "macro" && names.has(node.content)) {
      found.push(node);
    }
    if ("content" in node && Array.isArray(node.content)) visit(node.content);
    if ("args" in node && node.args) for (const a of node.args) visit(a.content);
  }
  visit(root);
  return found;
}

function documentWideLabelsAndRefs(ast: Ast.Root): {
  labels: { key: string; line: number | null }[];
  refs: { key: string; line: number | null; macro: string }[];
} {
  const labelNodes = collectMacrosDocumentWide(ast.content, new Set(["label"]));
  const refNodes = collectMacrosDocumentWide(ast.content, REF_MACROS);
  return {
    labels: labelNodes.map((n) => ({ key: lastArgText(n), line: macroLine(n) })),
    refs: refNodes.map((n) => ({ key: lastArgText(n), line: macroLine(n), macro: n.content })),
  };
}

/** Defensive detector for "an error-shaped recovered node" -- this parser
 * version (probed directly against every malformed-input shape this file's
 * own tests exercise: unclosed environments, mismatched \begin/\end,
 * unbalanced braces, unterminated math) never actually produces one; it
 * silently recovers into a different, non-"environment" node shape instead
 * (see lint.test.js's own comment on this). This check exists anyway, ahead
 * of that ever being observed, so a future parser upgrade that DOES start
 * emitting a `{type: "parseerror", ...}`-shaped node (or similarly named)
 * degrades to a clean finding instead of silently flowing through every
 * other check as if it were a normal AST node. */
function findErrorShapedNode(root: Ast.Node[] | Ast.Node | undefined): Ast.Node | null {
  let found: Ast.Node | null = null;
  function visit(node: Ast.Node[] | Ast.Node | undefined): void {
    if (found) return;
    if (Array.isArray(node)) {
      for (const n of node) {
        visit(n);
        if (found) return;
      }
      return;
    }
    if (!node || typeof node !== "object") return;
    if (typeof node.type === "string" && /error/i.test(node.type)) {
      found = node;
      return;
    }
    if ("content" in node && Array.isArray(node.content)) visit(node.content);
    if ("args" in node && node.args) for (const a of node.args) visit(a.content);
  }
  visit(root);
  return found;
}

/** The loosely-typed shape of a parse failure this function normalizes --
 * either a thrown Error-like object (`.message`/`.location`, e.g. a pegjs
 * SyntaxError) or a recovered AST node (`.type`/`.position`). Genuinely
 * heterogeneous, externally-shaped input, so it's read via `unknown` +
 * narrowing rather than assumed. */
interface ParseFailureLocation {
  start?: { line?: number };
}
interface ParseFailureLike {
  location?: ParseFailureLocation;
  position?: ParseFailureLocation;
  message?: string;
  type?: string;
}

/** Builds the single unclosed-environment-or-parse-failure finding, from
 * either a thrown Error (has `.message`/`.location`, e.g. a pegjs
 * SyntaxError) or a recovered AST node (has `.type`/`.position`) -- one
 * function for both call sites in lintText below. */
function parseFailureFinding(errorOrNode: unknown): Finding {
  let line: number | null = null;
  let message = "Unknown parse failure.";
  if (errorOrNode && typeof errorOrNode === "object") {
    const e = errorOrNode as ParseFailureLike;
    const loc = e.location || e.position || null;
    if (loc && loc.start && typeof loc.start.line === "number") line = loc.start.line;
    if (typeof e.message === "string" && e.message) {
      message = e.message;
    } else if (typeof e.type === "string") {
      message = `Parser produced an error-shaped "${e.type}" node.`;
    }
  }
  return { rule: "unclosed-environment-or-parse-failure", severity: "error", message, line };
}

// citation-missing-bib-entry (error): every citation node's key must have a
// matching bibliography entry. "(no key)" citations are skipped here --
// empty-cite-key below is the dedicated check for those, and reporting them
// again as "missing" would just be redundant noise for the same macro.
function checkCitationMissingBibEntry(nodes: OutlineNode[], bibEntries: BibliographyEntry[]): Finding[] {
  const bibKeys = new Set(bibEntries.map((e) => e.key).filter(Boolean));
  const findings: Finding[] = [];
  for (const n of nodes) {
    if (n.kind !== "citation" || n.key === "(no key)") continue;
    // A citation-kind node always carries a real (possibly "(no key)",
    // already excluded above) `key` string -- see extractOutline's citation
    // branch -- OutlineNode just declares it optional across every kind.
    if (!bibKeys.has(n.key as string)) {
      findings.push({
        rule: "citation-missing-bib-entry",
        severity: "error",
        message: `Citation key "${n.key}" has no matching bibliography entry.`,
        line: n.line,
        nodeId: n.id,
      });
    }
  }
  return findings;
}

// ref-target-missing (error): every \ref/\Cref/\cref/\eqref/\autoref key
// must match some \label{} found anywhere in the document (documentWide,
// not collectLabels' structural-environment-scoped map).
function checkRefTargetMissing(
  labels: { key: string; line: number | null }[],
  refs: { key: string; line: number | null; macro: string }[],
): Finding[] {
  const labelKeys = new Set(labels.map((l) => l.key).filter(Boolean));
  const findings: Finding[] = [];
  for (const r of refs) {
    if (!r.key) continue;
    if (!labelKeys.has(r.key)) {
      findings.push({
        rule: "ref-target-missing",
        severity: "error",
        message: `\\${r.macro}{${r.key}} has no matching \\label{${r.key}} anywhere in the document.`,
        line: r.line,
      });
    }
  }
  return findings;
}

// duplicate-label (error): the same document-wide label walk as
// ref-target-missing, flagging any key seen 2+ times. One finding per
// occurrence AFTER the first (so n occurrences of the same key produce n-1
// findings), each pointing at the actual duplicate line, with the first
// occurrence's line named in the message for context.
function checkDuplicateLabel(labels: { key: string; line: number | null }[]): Finding[] {
  const byKey = new Map<string, { key: string; line: number | null }[]>();
  for (const l of labels) {
    if (!l.key) continue;
    if (!byKey.has(l.key)) byKey.set(l.key, []);
    byKey.get(l.key)!.push(l);
  }
  const findings: Finding[] = [];
  for (const [key, occurrences] of byKey) {
    if (occurrences.length < 2) continue;
    for (let i = 1; i < occurrences.length; i++) {
      findings.push({
        rule: "duplicate-label",
        severity: "error",
        message: `Duplicate \\label{${key}} (first defined on line ${occurrences[0].line}).`,
        line: occurrences[i].line,
      });
    }
  }
  return findings;
}

// section-hierarchy-skip (warning): walk extractOutline()'s heading nodes in
// document order and flag a depth jump of more than one level (e.g.
// \section straight to \subsubsection, with no \subsection in between).
// Depth is each level name's index in SECTION_ORDER (part=0, chapter=1,
// section=2, ...) -- the same canonical LaTeX sectioning order outline.js's
// own SECTION_MACROS is declared in (part, chapter, section, subsection,
// subsubsection, paragraph, subparagraph), kept as a local literal rather
// than importing SECTION_MACROS itself since a Set's insertion-order
// iteration used as an implicit ordering contract is easy to accidentally
// break with an unrelated future edit to that Set -- an explicit array here
// is the more obviously-correct thing for a caller to read and rely on.
const SECTION_ORDER = ["part", "chapter", "section", "subsection", "subsubsection", "paragraph", "subparagraph"];

function checkSectionHierarchySkip(nodes: OutlineNode[]): Finding[] {
  const headings = nodes.filter((n) => n.kind === "heading");
  const findings: Finding[] = [];
  let prevDepth: number | null = null;
  for (const h of headings) {
    // A heading node always carries `.level` (extractOutline's heading
    // branch always assigns it from SECTION_MACROS) -- the cast reflects
    // that guarantee without weakening OutlineNode's own optional typing,
    // which stays honest about table/figure/equation/citation nodes that
    // have no `level` at all.
    const depth = SECTION_ORDER.indexOf(h.level as string);
    if (depth === -1) {
      // An unrecognized level name shouldn't happen (extractOutline only
      // ever assigns node.level from SECTION_MACROS), but never assumed --
      // just don't let it corrupt the running depth comparison.
      prevDepth = null;
      continue;
    }
    if (prevDepth !== null && depth - prevDepth > 1) {
      findings.push({
        rule: "section-hierarchy-skip",
        severity: "warning",
        message: `Heading "${h.title}" (\\${h.level}) skips ${depth - prevDepth} levels down from \\${SECTION_ORDER[prevDepth]}.`,
        line: h.line,
        nodeId: h.id,
      });
    }
    prevDepth = depth;
  }
  return findings;
}

// duplicate-bib-key (warning): parseBibitems/parseBibtexEntries entries
// (both folded together by getBibliography) sharing a key. Same
// one-finding-per-occurrence-after-the-first shape as duplicate-label.
// `line` is always null here: neither bibitem nor bibtex parsing in
// bibliography.js tracks source line numbers (it's a regex-based extractor
// over entry TEXT, not an AST walk -- see that file's own header) --
// reported honestly as null/unknown rather than a fabricated guess.
function checkDuplicateBibKey(bibEntries: BibliographyEntry[]): Finding[] {
  const byKey = new Map<string, BibliographyEntry[]>();
  for (const e of bibEntries) {
    if (!e.key) continue;
    if (!byKey.has(e.key)) byKey.set(e.key, []);
    byKey.get(e.key)!.push(e);
  }
  const findings: Finding[] = [];
  for (const [key, occurrences] of byKey) {
    if (occurrences.length < 2) continue;
    for (let i = 1; i < occurrences.length; i++) {
      findings.push({
        rule: "duplicate-bib-key",
        severity: "warning",
        message: `Bibliography key "${key}" is defined ${occurrences.length} times.`,
        line: null,
      });
    }
  }
  return findings;
}

// unused-bib-entry (info): bib entries (bibitem + bibtex, via
// getBibliography) never referenced by any citation node -- a plain set
// difference. De-dupes by key first so a key already flagged by
// duplicate-bib-key doesn't also produce N redundant "never cited" findings.
function checkUnusedBibEntry(nodes: OutlineNode[], bibEntries: BibliographyEntry[]): Finding[] {
  const citedKeys = new Set(nodes.filter((n) => n.kind === "citation").map((n) => n.key));
  const seen = new Set<string>();
  const findings: Finding[] = [];
  for (const e of bibEntries) {
    if (!e.key || seen.has(e.key)) continue;
    seen.add(e.key);
    if (!citedKeys.has(e.key)) {
      findings.push({
        rule: "unused-bib-entry",
        severity: "info",
        message: `Bibliography entry "${e.key}" is never cited.`,
        line: null,
      });
    }
  }
  return findings;
}

// empty-caption-or-label (warning): a STRUCTURAL_ENVIRONMENTS
// (table/figure/equation-like) node with neither a caption nor a label.
// Filtered on `node.env` being set -- extractOutline only attaches that
// field to nodes it built from an actual \begin{...}\end{...} environment
// match (the STRUCTURAL_ENVIRONMENTS branch), never to a bare mathenv
// ($...$/\[...\]) node, which has no caption/label concept at all and would
// otherwise false-positive on every single inline equation in the document.
function checkEmptyCaptionOrLabel(nodes: OutlineNode[]): Finding[] {
  const findings: Finding[] = [];
  for (const n of nodes) {
    if (!n.env) continue;
    if (!n.caption && !n.label) {
      findings.push({
        rule: "empty-caption-or-label",
        severity: "warning",
        message: `\\begin{${n.env}} has neither a \\caption nor a \\label.`,
        line: n.line,
        nodeId: n.id,
      });
    }
  }
  return findings;
}

// empty-cite-key (error): citation nodes extractOutline() already marks
// with the sentinel key "(no key)" -- e.g. a bare \cite{} with nothing
// between the braces.
function checkEmptyCiteKey(nodes: OutlineNode[]): Finding[] {
  return nodes
    .filter((n) => n.kind === "citation" && n.key === "(no key)")
    .map((n) => ({
      rule: "empty-cite-key",
      severity: "error" as const,
      message: "Citation macro has no key (e.g. \\cite{}).",
      line: n.line,
      nodeId: n.id,
    }));
}

// stray-todo-fixme (info): TODO/FIXME/XXX markers, found via a raw-text
// regex line scan rather than an AST string-leaf walk -- deliberately, not
// as a fallback-of-last-resort: a %-comment ("% TODO: revisit this") is the
// single most common place a real paper actually has one of these markers,
// and unified-latex's parser strips %-comments out of the AST entirely
// (confirmed empirically -- see lint.test.js) rather than keeping them as
// "string" leaf content, so an AST-only walk would systematically MISS
// exactly the case this check most needs to catch. Scanning raw source text
// line-by-line catches comments, verbatim blocks, and ordinary prose
// uniformly, with no risk of the parser's own recovery/normalization
// silently hiding a marker from this check.
function checkStrayTodoFixme(source: string): Finding[] {
  const findings: Finding[] = [];
  const lines = source.split(/\r\n|\r|\n/);
  const pattern = /\b(TODO|FIXME|XXX)\b/g;
  lines.forEach((lineText, idx) => {
    pattern.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = pattern.exec(lineText))) {
      findings.push({
        rule: "stray-todo-fixme",
        severity: "info",
        message: `Found "${m[1]}" marker.`,
        line: idx + 1,
      });
    }
  });
  return findings;
}

// unresolved-sub-label (info): a nested same-kind structural node's own
// \label{} (e.g. \subfigure inside \figure) that collectLabels() already
// documents, in outline.js, as a v0 limitation it deliberately leaves
// unresolved (no fabricated \ref{} number) rather than a bug to fix here --
// this check just surfaces collectLabels()'s own `nestedSameKindLabels`
// list (added alongside this feature) as findings instead of letting it
// silently disappear.
function checkUnresolvedSubLabel(nestedSameKindLabels: NestedSameKindLabel[]): Finding[] {
  return nestedSameKindLabels.map(({ key, kind, line }) => ({
    rule: "unresolved-sub-label",
    severity: "info" as const,
    message:
      `\\label{${key}} is on a nested ${kind}-in-${kind} node (e.g. \\subfigure/\\subtable) and is not ` +
      "resolved to a \\ref{} number -- a documented v0 limitation, not a bug in this lint check.",
    line,
  }));
}

export interface LintOptions {
  /** optional text of an already-fetched external .bib file, resolving any
   * \bibliography{}/\addbibresource{} reference in `source` -- same shape
   * and purpose as the get_bibliography MCP tool's own `bib_text` argument
   * (mcp-server.js), passed straight through to getBibliography(). */
  bibText?: string;
  /** optional override for outline.js's `parse`, used ONLY by lint.test.js
   * to exercise the unclosed-environment-or-parse-failure path
   * deterministically (this parser version does not, in practice, throw on
   * any malformed input this file's own tests could construct -- see that
   * check's own comment above) -- the same "swap the real implementation
   * for a test double via an argument" shape mcp-server.js's callTool()
   * already uses for its `deps` parameter. Production callers never pass
   * this. Typed to return `unknown` (not `Ast.Root`) so a test double can
   * return a deliberately malformed/fake shape (or throw) to exercise the
   * defensive checks below -- lintText validates the actual shape itself
   * rather than trusting the declared return type. */
  parseImpl?: (source: string) => unknown;
}

/** The one structural fact `lintText` relies on before trusting a parse
 * result is a real `Ast.Root` -- see the `unknown`-typed `parseImpl` above. */
interface ParsedLike {
  content?: unknown;
}

/**
 * Lint raw .tex source text against all 11 checks. Never throws -- see this
 * module's own header for the full tolerant-of-malformed-input contract.
 *
 * `source` is typed `unknown`, not `string`, so this file's own test suite
 * can exercise the "non-string source" runtime guard below without that
 * call being rejected at compile time first.
 *
 * Returns a flat array of findings (not wrapped in an object) -- the same
 * shape extractOutline()/getBibliography() themselves return; the MCP tools
 * below wrap it as `{findings}`, mirroring outline_tex's own `{nodes}` shape.
 */
export function lintText(source: unknown, options: LintOptions = {}): Finding[] {
  const { bibText, parseImpl = parse } = options;
  if (typeof source !== "string") {
    return [parseFailureFinding({ message: "lintText requires a string 'source' argument." })];
  }

  try {
    const parsed = parseImpl(source);
    if (!parsed || typeof parsed !== "object" || !Array.isArray((parsed as ParsedLike).content)) {
      return [parseFailureFinding({ message: "Parser returned a malformed AST (no content array)." })];
    }
    // Validated above (a real content array is present) -- the rest of this
    // function trusts the shape the same way the untyped original code did.
    const ast = parsed as Ast.Root;

    const errorNode = findErrorShapedNode(ast.content);
    if (errorNode) {
      return [parseFailureFinding(errorNode)];
    }

    const nodes = extractOutline(ast);
    const { nestedSameKindLabels } = collectLabels(ast.content);
    const { labels, refs } = documentWideLabelsAndRefs(ast);
    const bibEntries = getBibliography(source, bibText ? { resolveBibText: () => bibText } : undefined);

    return [
      ...checkCitationMissingBibEntry(nodes, bibEntries),
      ...checkRefTargetMissing(labels, refs),
      ...checkDuplicateLabel(labels),
      ...checkSectionHierarchySkip(nodes),
      ...checkDuplicateBibKey(bibEntries),
      ...checkUnusedBibEntry(nodes, bibEntries),
      ...checkEmptyCaptionOrLabel(nodes),
      ...checkEmptyCiteKey(nodes),
      ...checkStrayTodoFixme(source),
      ...checkUnresolvedSubLabel(nestedSameKindLabels),
    ];
  } catch (err) {
    // Never throw to the caller -- a throw from parse() itself, or from
    // anything downstream (extractOutline, a check helper, etc.), degrades
    // to the single unclosed-environment-or-parse-failure finding, matching
    // bibliography.js's own tolerant-of-malformed-input contract.
    return [parseFailureFinding(err)];
  }
}

/** Same as lintText, but for a LOCAL .tex file on disk -- mirrors
 * outline.js's own outlineFile()/outlineText() split. */
export function lintFile(path: string, options: LintOptions = {}): Finding[] {
  const source = readFileSync(path, "utf-8");
  return lintText(source, options);
}
