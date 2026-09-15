import { parse } from "@unified-latex/unified-latex-util-parse";
import { readFileSync } from "node:fs";

const SECTION_MACROS = new Set([
  "part", "chapter", "section", "subsection", "subsubsection", "paragraph", "subparagraph",
]);
const STRUCTURAL_ENVIRONMENTS = new Set([
  "table", "table*", "figure", "figure*", "tabular", "equation", "equation*",
  "align", "align*", "eqnarray", "eqnarray*",
]);

function nodeId(kind, position, index) {
  const line = position && position.start ? position.start.line : "?";
  return `${kind}:L${line}:${index}`;
}

// v0 numbering approximation: sequential per-kind count in document order,
// matching plain LaTeX auto-numbering (\thetable/\thefigure/\theequation)
// for the common case with no manual numbering overrides, no subfigures, and
// no per-section restart. Real LaTeX numbering can differ from this in those
// cases -- this is a documented approximation, not a claim of exact parity
// with a compiled PDF's numbers.
const REF_KIND_FOR_ENV = (env) =>
  env.startsWith("table") || env === "tabular" ? "table"
  : env.startsWith("figure") ? "figure"
  : "equation";

/**
 * Render a macro/environment argument's content to text, resolving nested
 * \ref{key} macros against a label->number map instead of silently dropping
 * them (the v0 behavior). An unresolved ref (label never seen, or seen only
 * later in a two-pass sense that this single forward pass can't reach --
 * see collectLabels below, which runs a full pass first so all labels are
 * known before any caption is rendered) is rendered as a clearly-marked
 * `[?key]` token rather than silently vanishing, so a caller can tell "no
 * cross-reference here" apart from "cross-reference to something unresolved".
 */
function renderText(content, labelToNumber) {
  if (!Array.isArray(content)) return "";
  const parts = [];
  for (const c of content) {
    if (!c || typeof c !== "object") continue;
    if (c.type === "string") {
      parts.push(c.content);
    } else if (c.type === "whitespace") {
      parts.push(" ");
    } else if (c.type === "macro" && c.content === "ref") {
      const key = argText(c, labelToNumber);
      const resolved = labelToNumber.get(key);
      parts.push(resolved !== undefined ? String(resolved) : `[?${key || "ref"}]`);
    } else if (c.content && Array.isArray(c.content)) {
      // Best-effort: recurse into other inline macros/groups (e.g. \emph{...})
      // so their text content still contributes, without special-casing
      // every possible macro.
      parts.push(renderText(c.content, labelToNumber));
    }
  }
  return parts.join("").trim();
}

function argText(macroNode, labelToNumber) {
  if (!macroNode.args) return "";
  const parts = [];
  for (const arg of macroNode.args) {
    parts.push(renderText(arg.content || [], labelToNumber || EMPTY_MAP));
  }
  return parts.join("").trim();
}

const EMPTY_MAP = new Map();

function findFirstMacro(content, name) {
  if (!Array.isArray(content)) return null;
  for (const n of content) {
    if (n && n.type === "macro" && n.content === name) return n;
    if (n && n.content && Array.isArray(n.content)) {
      const found = findFirstMacro(n.content, name);
      if (found) return found;
    }
  }
  return null;
}

/**
 * First pass: walk the whole AST purely to assign a document-order,
 * sequential-per-kind number to every \label{} found directly inside a
 * structural environment (table/figure/equation-like). This must run to
 * completion BEFORE any caption is rendered, so a \ref{} to a label that
 * appears LATER in the document still resolves (a real, common case --
 * "as shown in Table~\ref{tab:later}" before that table appears).
 */
function collectLabels(root) {
  const labelToNumber = new Map();
  const counters = { table: 0, figure: 0, equation: 0 };

  function visit(node) {
    if (Array.isArray(node)) {
      for (const n of node) visit(n);
      return;
    }
    if (!node || typeof node !== "object") return;

    if (node.type === "environment" && typeof node.env === "string" && STRUCTURAL_ENVIRONMENTS.has(node.env)) {
      const kind = REF_KIND_FOR_ENV(node.env);
      counters[kind] += 1;
      const labelMacro = findFirstMacro(node.content, "label");
      if (labelMacro) {
        const key = argText(labelMacro, EMPTY_MAP);
        if (key) labelToNumber.set(key, counters[kind]);
      }
      // Still descend for nested structural environments/citations counted
      // elsewhere; labels are only expected at the top level of the env here.
      if (Array.isArray(node.content)) visit(node.content);
      return;
    }
    if (node.type === "mathenv") {
      counters.equation += 1;
      return;
    }

    if (node.content && Array.isArray(node.content)) visit(node.content);
    if (node.args) for (const a of node.args) visit(a.content);
  }

  visit(root);
  return labelToNumber;
}

/**
 * Walk a unified-latex AST and produce a flat list of addressable structural
 * nodes: headings, tables, figures, equations, citations. Each node carries
 * a stable-ish id, its kind, a short label, and its source line range so a
 * caller (human or agent) can target it for a future edit operation.
 *
 * v0 known limitations, documented rather than hidden:
 *  - Numbering used to resolve \ref{} is a sequential-per-kind approximation
 *    (see REF_KIND_FOR_ENV above), not a real LaTeX-numbering-engine parity
 *    claim (manual \setcounter, subfigures, and per-section restarts are not
 *    modeled).
 *  - A \ref{} to a \label{} that lives OUTSIDE a table/figure/equation
 *    environment (e.g. a \label{} on a \section) never resolves (unresolved
 *    refs render as `[?key]`); only table/figure/equation labels are
 *    numbered in this v0 pass, matching this engine's current structural
 *    node set.
 */
export function extractOutline(ast) {
  const nodes = [];
  let counter = 0;
  const labelToNumber = collectLabels(ast.content);

  function visit(node) {
    if (Array.isArray(node)) {
      for (const n of node) visit(n);
      return;
    }
    if (!node || typeof node !== "object") return;

    if (node.type === "macro" && SECTION_MACROS.has(node.content)) {
      nodes.push({
        id: nodeId("heading", node.position, counter++),
        kind: "heading",
        level: node.content,
        title: argText(node, labelToNumber) || "(untitled)",
        line: node.position && node.position.start ? node.position.start.line : null,
      });
    } else if (node.type === "macro" && node.content === "cite") {
      // Split a multi-key \cite{a,b,c} into one citation node per key,
      // rather than one blob node with key="a,b,c" (the v0 behavior).
      const raw = argText(node, EMPTY_MAP);
      const keys = raw ? raw.split(",").map((k) => k.trim()).filter(Boolean) : [];
      const line = node.position && node.position.start ? node.position.start.line : null;
      if (keys.length === 0) {
        nodes.push({ id: nodeId("citation", node.position, counter++), kind: "citation", key: "(no key)", line });
      } else {
        for (const key of keys) {
          nodes.push({ id: nodeId("citation", node.position, counter++), kind: "citation", key, line });
        }
      }
    } else if (node.type === "environment" && typeof node.env === "string" && STRUCTURAL_ENVIRONMENTS.has(node.env)) {
      const captionMacro = findFirstMacro(node.content, "caption");
      const labelMacro = findFirstMacro(node.content, "label");
      nodes.push({
        id: nodeId(node.env, node.position, counter++),
        kind: REF_KIND_FOR_ENV(node.env),
        env: node.env,
        caption: captionMacro ? argText(captionMacro, labelToNumber) : null,
        label: labelMacro ? argText(labelMacro, EMPTY_MAP) : null,
        line: node.position && node.position.start ? node.position.start.line : null,
        end_line: node.position && node.position.end ? node.position.end.line : null,
      });
      // Don't re-emit this block itself as a second top-level node, but DO
      // still walk its content so citations nested inside a table/figure
      // (e.g. a citation in a table footnote or figure caption) are found --
      // the v0 behavior returned here unconditionally and silently dropped
      // those citations entirely.
      if (Array.isArray(node.content)) visit(node.content);
      return;
    } else if (node.type === "mathenv") {
      nodes.push({
        id: nodeId("equation", node.position, counter++),
        kind: "equation",
        line: node.position && node.position.start ? node.position.start.line : null,
      });
      return;
    }

    if (node.content && Array.isArray(node.content)) visit(node.content);
    if (node.args) for (const a of node.args) visit(a.content);
  }

  visit(ast.content);
  return nodes;
}

export function outlineFile(path) {
  const source = readFileSync(path, "utf-8");
  const ast = parse(source);
  return extractOutline(ast);
}
