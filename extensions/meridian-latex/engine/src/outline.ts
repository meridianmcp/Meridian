import { getParser } from "@unified-latex/unified-latex-util-parse";
import { readFileSync } from "node:fs";
import type * as Ast from "@unified-latex/unified-latex-types";

/**
 * unified-latex's bundled CTAN macro-info database (used unconditionally by
 * its own default `parse()`) only knows base LaTeX2e's `\cite{o}{m}` --
 * confirmed empirically, not assumed: natbib's `\citep`/`\citet` family is
 * NOT in it, so without this, `\citep{key}` parses with the macro node's
 * `args` left `undefined` and the `{key}` group ends up as an unrelated
 * SIBLING ast node instead -- silently making every natbib-style citation
 * invisible to `extractOutline` below (found via real testing against a
 * genuinely different real paper's actual LaTeX, not a synthetic case).
 * Signature `"o o m"` (xparse notation: two optional bracket args, then one
 * mandatory brace arg) matches natbib's real `\citep[pre][post]{keys}` shape;
 * registering it uniformly for every family member is correct even for the
 * ones that don't support a second optional note in real natbib, since an
 * absent optional arg just yields an empty slot either way -- the mandatory
 * key-list arg is always the LAST slot regardless of how many optional notes
 * preceded it (see CITATION_MACROS / lastArgText below).
 *
 * The leading `s` matters and was NOT obvious: TeX's own tokenizer stops a
 * control word at the first non-letter, so `\citep*` is ALWAYS lexed as
 * macro name "citep" plus a separate literal "*" token -- no macro-info
 * registration can make "citep*" its own recognized name. Confirmed
 * empirically that signature "o o m" (no `s`) then treats that bare "*" as
 * satisfying the MANDATORY "m" slot on its own (TeX allows a single
 * un-braced token as a valid argument), leaving the real `{key}` group
 * completely unconsumed as an unrelated sibling node -- i.e. a starred
 * citation's key would silently resolve to the literal string "*". The `s`
 * spec absorbs an optional leading "*" into its OWN slot first, so the "m"
 * slot correctly still lands on the real `{key}` group whether or not a
 * star is present.
 */
const NATBIB_CITATION_SIGNATURE = { signature: "s o o m" };
const parser = getParser({
  macros: {
    citep: NATBIB_CITATION_SIGNATURE,
    citet: NATBIB_CITATION_SIGNATURE,
    citealp: NATBIB_CITATION_SIGNATURE,
    citealt: NATBIB_CITATION_SIGNATURE,
    citeauthor: NATBIB_CITATION_SIGNATURE,
    citeyear: NATBIB_CITATION_SIGNATURE,
    citeyearpar: NATBIB_CITATION_SIGNATURE,
    Citep: NATBIB_CITATION_SIGNATURE,
    Citet: NATBIB_CITATION_SIGNATURE,
    Citealp: NATBIB_CITATION_SIGNATURE,
    Citealt: NATBIB_CITATION_SIGNATURE,
    Citeauthor: NATBIB_CITATION_SIGNATURE,
  },
});
/** Exported so other modules that need the SAME natbib-aware parser
 * configuration (see NATBIB_CITATION_SIGNATURE above) -- e.g. lint.js's
 * unclosed-environment-or-parse-failure check -- reuse this one instance
 * rather than standing up a second, differently-configured getParser() call
 * that would silently regress the citep/citet/etc. fix this file documents
 * at its own top. */
export function parse(source: string): Ast.Root {
  return parser.parse(source);
}

/** Every citation-shaped macro this engine recognizes: base LaTeX's `\cite`
 * (ctan-provided signature) plus the natbib author-year family registered
 * above. The mandatory key-list argument is always the LAST arg for every
 * member of this set -- see lastArgText. */
const CITATION_MACROS = new Set([
  "cite", "citep", "citet", "citealp", "citealt", "citeauthor", "citeyear", "citeyearpar",
  "Citep", "Citet", "Citealp", "Citealt", "Citeauthor",
]);

const SECTION_MACROS = new Set([
  "part", "chapter", "section", "subsection", "subsubsection", "paragraph", "subparagraph",
]);
// Real bug found 2026-09-18 via independent code review, confirmed by
// reproduction: `subfigure`/`subtable` (the subcaption/subfig packages'
// multi-panel environments) were missing here entirely, so a nested
// `\begin{subfigure}...\end{subfigure}` inside an outer `\begin{figure}`
// was NOT recognized as a scope boundary by findFirstMacroInOwnScope --
// its search leaked straight through, so the OUTER float's real
// \caption/\label were silently overwritten by the FIRST subfigure's own
// \caption/\label (confirmed: the outer "Overall figure"/"fig:overall"
// vanished entirely, replaced by the subfigure's "Sub A"/"fig:a", with no
// trace of the real outer caption/label anywhere in the output -- data
// loss, not just a display quirk). This is the SAME misattribution class
// `findFirstMacroInOwnScope` was built to prevent for tabular-in-table;
// subfigure/subtable were simply never added to the boundary set. Multi-
// panel figures/tables (subcaption/subfig) are extremely common in real
// papers, making this a frequent bug, not an edge case.
const STRUCTURAL_ENVIRONMENTS = new Set([
  "table", "table*", "figure", "figure*", "tabular", "subfigure", "subtable",
  "equation", "equation*", "align", "align*", "eqnarray", "eqnarray*",
]);

/**
 * A tiny, dependency-free FNV-1a hash (32-bit). Not security-sensitive --
 * node ids only need to be deterministic and collision-unlikely for a
 * realistic document (see README: ~100 structural nodes for the actual
 * manuscript this engine was built against).
 */
function fnv1a32(str: string): string {
  let hash = 0x811c9dc5;
  for (let i = 0; i < str.length; i++) {
    hash ^= str.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}

/** The `{start:{offset,line,column}, end:{...}}` position span every
 * unified-latex AST node optionally carries -- structurally identical to
 * (but locally declared instead of imported from) the shape inline in
 * `@unified-latex/unified-latex-types`'s own `BaseNode`, which that package
 * declares but does not export. */
interface NodePosition {
  start: { offset: number; line: number; column: number };
  end: { offset: number; line: number; column: number };
}

/**
 * v1 node ids: a content fingerprint, not a position. This replaces the v0
 * `${kind}:L${line}:${index}` scheme, which reassigned every id after any
 * upstream edit (added/removed lines, or a new same-kind node inserted
 * earlier) -- making it useless as a stable handle for a claim/lease/
 * write-back system built on top of this outline.
 *
 * `contentParts` should be the pieces of a node's OWN stable content (e.g.
 * [level, title] for a heading) -- never anything position-derived. When
 * every part is empty/missing -- the one real case: a table/figure/equation
 * with neither a caption nor a label -- there is no stable content left to
 * hash, so this falls back to the old position-derived id. That fallback is
 * a known, documented limitation, not hidden behind a fake-stable-looking
 * id: it can still drift across upstream edits exactly like v0 did, but
 * only for the nodes that have no distinguishing content of their own.
 */
function fingerprint(
  kind: string,
  contentParts: Array<string | null | undefined>,
  position: NodePosition | undefined,
  index: number,
): string {
  const joined = contentParts
    .filter((part) => part !== null && part !== undefined && part !== "")
    .join("");
  if (joined) {
    return `${kind}:${fnv1a32(`${kind} ${joined}`)}`;
  }
  const line = position && position.start ? position.start.line : "?";
  return `${kind}:pos:L${line}:${index}`;
}

/**
 * One addressable structural node in the outline (heading, citation,
 * table/figure/equation float, or a bare mathenv equation). Deliberately
 * ALL-optional beyond `id`/`kind`/`line` rather than a strict per-kind
 * discriminated union: an "equation" node from a bare `$...$`/`\[...\]`
 * mathenv carries NONE of the caption/label/env fields a `\begin{equation}`
 * float does (see extractOutline's two separate equation-producing
 * branches below), so `kind` alone doesn't determine which fields exist --
 * an honest reflection of that, not a shortcut.
 */
export interface OutlineNode {
  id: string;
  kind: "heading" | "citation" | "table" | "figure" | "equation";
  line: number | null;
  // heading-only
  level?: string;
  title?: string;
  // citation-only
  key?: string;
  // table/figure/equation(-environment)-only
  env?: string;
  caption?: string | null;
  label?: string | null;
  captionLine?: number | null;
  labelLine?: number | null;
  end_line?: number | null;
}

/**
 * Second pass over the finished node list: if two nodes ended up with the
 * identical id (a genuine hash collision -- e.g. two untitled same-level
 * headings, which really do hash the same kind+level+"(untitled)" content),
 * disambiguate rather than silently merge them into one logical node. The
 * first occurrence keeps its plain id; later ones get `-dup2`, `-dup3`,
 * etc., in document order.
 */
function disambiguateIds(nodes: OutlineNode[]): void {
  const seen: Map<string, number> = new Map();
  for (const node of nodes) {
    const base = node.id;
    const count = (seen.get(base) || 0) + 1;
    seen.set(base, count);
    if (count > 1) node.id = `${base}-dup${count}`;
  }
}

// v0 numbering approximation: sequential per-kind count in document order,
// matching plain LaTeX auto-numbering (\thetable/\thefigure/\theequation)
// for the common case with no manual numbering overrides, no subfigures, and
// no per-section restart. Real LaTeX numbering can differ from this in those
// cases -- this is a documented approximation, not a claim of exact parity
// with a compiled PDF's numbers.
const REF_KIND_FOR_ENV = (env: string): "table" | "figure" | "equation" =>
  env.startsWith("table") || env === "tabular" || env === "subtable" ? "table"
  : env.startsWith("figure") || env === "subfigure" ? "figure"
  : "equation";

const EMPTY_MAP: Map<string, number> = new Map();

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
function renderText(content: Ast.Node[] | undefined, labelToNumber: Map<string, number>): string {
  if (!Array.isArray(content)) return "";
  const parts: string[] = [];
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
    } else if ("content" in c && Array.isArray(c.content)) {
      // Best-effort: recurse into other inline macros/groups (e.g. \emph{...})
      // so their text content still contributes, without special-casing
      // every possible macro.
      parts.push(renderText(c.content, labelToNumber));
    }
  }
  return parts.join("").trim();
}

function argText(macroNode: Ast.Macro, labelToNumber?: Map<string, number>): string {
  if (!macroNode.args) return "";
  const parts: string[] = [];
  for (const arg of macroNode.args) {
    parts.push(renderText(arg.content || [], labelToNumber || EMPTY_MAP));
  }
  return parts.join("").trim();
}

/**
 * Like argText, but returns only the LAST arg's text -- the correct choice
 * for a citation-family macro's key list, which is always the final
 * (mandatory) arg regardless of how many optional `[...]` notes precede it
 * (`\cite{key}` has 2 arg slots, `\citep[see][]{key}` has 3 -- see
 * CITATION_MACROS above). Using plain argText here would concatenate an
 * optional pre/post note's text INTO the key list (e.g. `\citep[see][]{key}`
 * -> "seekey"), silently corrupting it -- a real bug this engine hasn't
 * shipped only because no document tested so far happens to use a citation
 * note; this is deliberately correct now rather than left latent.
 */
export function lastArgText(macroNode: Ast.Macro, labelToNumber?: Map<string, number>): string {
  if (!macroNode.args || macroNode.args.length === 0) return "";
  const lastArg = macroNode.args[macroNode.args.length - 1];
  return renderText(lastArg.content || [], labelToNumber || EMPTY_MAP).trim();
}

function findFirstMacro(content: Ast.Node[] | undefined, name: string): Ast.Macro | null {
  if (!Array.isArray(content)) return null;
  for (const n of content) {
    if (n && n.type === "macro" && n.content === name) return n;
    if (n && "content" in n && Array.isArray(n.content)) {
      const found = findFirstMacro(n.content, name);
      if (found) return found;
    }
  }
  return null;
}

/**
 * Same as `findFirstMacro`, except it does NOT descend into a nested
 * structural node's own content -- a child `environment` whose `env` is in
 * `STRUCTURAL_ENVIRONMENTS`, or a child `mathenv` -- since that content
 * belongs to a node `extractOutline` visits and addresses independently
 * (e.g. a `tabular` nested inside a `table` float; see the "documented
 * behavior change" test in outline.test.js). Without this boundary, a
 * `\caption{}`/`\label{}` that legitimately belongs to the NESTED node gets
 * misattributed to the OUTER one whenever it physically appears inside the
 * outer node's own `content` array -- exactly the ambiguity the README
 * flagged as the reason table/figure/equation field-editing was deferred.
 * `findFirstMacro` above is left as-is (still used by `argText`'s own
 * recursion needs and anywhere the boundary genuinely doesn't matter);
 * every NEW caller that resolves an editable field for a specific
 * table/figure/equation node should use this one instead.
 */
function findFirstMacroInOwnScope(content: Ast.Node[] | undefined, name: string): Ast.Macro | null {
  if (!Array.isArray(content)) return null;
  for (const n of content) {
    if (!n || typeof n !== "object") continue;
    if (n.type === "macro" && n.content === name) return n;
    const isNestedStructuralBoundary =
      (n.type === "environment" && typeof n.env === "string" && STRUCTURAL_ENVIRONMENTS.has(n.env)) ||
      n.type === "mathenv";
    if (isNestedStructuralBoundary) continue;
    if ("content" in n && Array.isArray(n.content)) {
      const found = findFirstMacroInOwnScope(n.content, name);
      if (found) return found;
    }
  }
  return null;
}

/** The 1-indexed source line a macro AST node starts on, or `null` if the
 * parser didn't attach position info (shouldn't happen for a real parsed
 * document, but never assumed). */
export function macroLine(macroNode: Ast.Macro | null): number | null {
  return macroNode && macroNode.position && macroNode.position.start
    ? macroNode.position.start.line
    : null;
}

/** Every \label{} found on a nested same-kind structural node (the case
 * collectLabels' own numbering deliberately skips, see its comment below),
 * recorded instead of silently dropped -- for lint.js's unresolved-sub-label
 * check. */
export interface NestedSameKindLabel {
  key: string;
  kind: "table" | "figure" | "equation";
  line: number | null;
}

/**
 * First pass: walk the whole AST purely to assign a document-order,
 * sequential-per-kind number to every \label{} found directly inside a
 * structural environment (table/figure/equation-like). This must run to
 * completion BEFORE any caption is rendered, so a \ref{} to a label that
 * appears LATER in the document still resolves (a real, common case --
 * "as shown in Table~\ref{tab:later}" before that table appears).
 *
 * Returns `{ labelToNumber, nestedSameKindLabels }`. `nestedSameKindLabels`
 * is new (added for lint.js's unresolved-sub-label check, exported alongside
 * this function): every \label{} found on a nested same-kind structural node
 * (the case the comment below documents as "deliberately left unresolved")
 * gets recorded here as `{key, kind, line}` instead of silently vanishing --
 * this function's own numbering behavior for that case is UNCHANGED (still
 * no fabricated number, still absent from `labelToNumber`), this is purely
 * an additional, non-invasive observation of what got skipped and why.
 */
export function collectLabels(root: Ast.Node[] | Ast.Node | undefined): {
  labelToNumber: Map<string, number>;
  nestedSameKindLabels: NestedSameKindLabel[];
} {
  const labelToNumber: Map<string, number> = new Map();
  const nestedSameKindLabels: NestedSameKindLabel[] = [];
  const counters: Record<"table" | "figure" | "equation", number> = { table: 0, figure: 0, equation: 0 };

  // Real bug found 2026-09-18 via independent code review: this counter
  // used to increment for EVERY STRUCTURAL_ENVIRONMENTS match, including a
  // `tabular` nested inside its own enclosing `table` (both map to kind
  // "table" via REF_KIND_FOR_ENV) -- unlike real LaTeX, where
  // `\begin{tabular}` never touches `\thetable` (only the outer float's own
  // `\caption` does, via its internal `\refstepcounter`). Every real table
  // in both test papers wraps a tabular, so EVERY table's number was
  // inflated (table 1 counted as 2, table 2 as 4, ...), corrupting every
  // `\ref{}` to a table -- and, after today's subfigure/subtable fix added
  // them to STRUCTURAL_ENVIRONMENTS, a subfigure-in-figure would have hit
  // the identical bug for figures. Fixed by only incrementing (and only
  // registering a \label{} against) the OUTERMOST occurrence of a given
  // kind -- `insideKind` tracks which kind we're already nested inside, so
  // a nested tabular/subfigure/subtable is walked (for further nested
  // citations etc.) but never double-counted. A nested same-kind
  // environment's OWN \label{} (the legitimate multi-sub-table/subfigure
  // case) is deliberately left unresolved (renders as `[?key]`) rather than
  // assigned a fabricated number -- matching this file's own documented v0
  // limitation that subfigure/subfigure-style sub-numbering isn't modeled.
  function visit(node: Ast.Node[] | Ast.Node | undefined, insideKind: "table" | "figure" | "equation" | null = null): void {
    if (Array.isArray(node)) {
      for (const n of node) visit(n, insideKind);
      return;
    }
    if (!node || typeof node !== "object") return;

    if (node.type === "environment" && typeof node.env === "string" && STRUCTURAL_ENVIRONMENTS.has(node.env)) {
      const kind = REF_KIND_FOR_ENV(node.env);
      if (kind !== insideKind) {
        counters[kind] += 1;
        const labelMacro = findFirstMacroInOwnScope(node.content, "label");
        if (labelMacro) {
          const key = argText(labelMacro, EMPTY_MAP);
          if (key) labelToNumber.set(key, counters[kind]);
        }
      } else {
        // The nested-same-kind case this function's own numbering
        // deliberately skips (see the comment above) -- record its own
        // \label{}, if any, separately rather than just dropping it, so a
        // caller like lint.js can still surface it as a finding.
        const nestedLabelMacro = findFirstMacroInOwnScope(node.content, "label");
        if (nestedLabelMacro) {
          const key = argText(nestedLabelMacro, EMPTY_MAP);
          if (key) nestedSameKindLabels.push({ key, kind, line: macroLine(nestedLabelMacro) });
        }
      }
      // Still descend for nested structural environments/citations counted
      // elsewhere; labels are only expected at the top level of the env here.
      // Propagate `kind` (not the possibly-different `insideKind`) so a
      // grandchild of the SAME kind is also suppressed correctly.
      if (Array.isArray(node.content)) visit(node.content, kind);
      return;
    }
    if (node.type === "mathenv") {
      counters.equation += 1;
      return;
    }

    if ("content" in node && Array.isArray(node.content)) visit(node.content, insideKind);
    if ("args" in node && node.args) for (const a of node.args) visit(a.content, insideKind);
  }

  visit(root);
  return { labelToNumber, nestedSameKindLabels };
}

/**
 * Walk a unified-latex AST and produce a flat list of addressable structural
 * nodes: headings, tables, figures, equations, citations. Each node carries
 * a stable, content-derived id (see `fingerprint` above), its kind, a short
 * label, and its source line range so a caller (human or agent) can target
 * it for a future edit operation -- and can still find the SAME logical
 * node in a later re-parse of an edited document (see `matching.js`'s
 * `matchOutlines` for the re-matching pass built on top of that).
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
 *  - Node ids are a content fingerprint (kind + title/key/caption/label),
 *    not a claim of global uniqueness: a table/figure/equation with no
 *    caption AND no label falls back to a position-derived id (still
 *    subject to the same drift-on-upstream-edit problem v0 had); and any
 *    two nodes that genuinely hash the same get a `-dup2`/`-dup3`/... id
 *    suffix rather than being silently merged (see `disambiguateIds`).
 */
export function extractOutline(ast: Ast.Root): OutlineNode[] {
  const nodes: OutlineNode[] = [];
  let counter = 0;
  const { labelToNumber } = collectLabels(ast.content);

  function visit(node: Ast.Node[] | Ast.Node | undefined): void {
    if (Array.isArray(node)) {
      for (const n of node) visit(n);
      return;
    }
    if (!node || typeof node !== "object") return;

    if (node.type === "macro" && SECTION_MACROS.has(node.content)) {
      // Real, ALREADY-LIVE bug found 2026-09-18 via independent code review,
      // confirmed against the real stored dnabert outline: every one of its
      // 25 real headings had a corrupted title with a spurious leading "*"
      // ("*Abstract", "*Introduction", ...). Root cause: every SECTION_MACROS
      // member has ctan signature "s o m" (star flag, optional short title,
      // mandatory title -- confirmed in unified-latex-ctan's own
      // latex2e/index.js), so `node.args` has 3 slots, and argText()
      // concatenates ALL of them -- exactly the same class of bug already
      // fixed for CITATION_MACROS via lastArgText (see its own comment) but
      // never applied here. Impact was worse than cosmetic: the corrupted
      // title seeds the popup's edit input (EDITABLE_FIELDS' heading
      // descriptor `get`), so submitting a heading edit without noticing/
      // stripping the stray "*" would write it INTO the document
      // (`\section*{Abstract}` -> `\section*{*Abstract}`) -- the write-
      // dispatch path itself (locateHeadingRange) independently re-scans the
      // live line by regex and was never affected, but the edit's REPLACEMENT
      // TEXT would have been wrong on every starred heading edit.
      const title = lastArgText(node, labelToNumber) || "(untitled)";
      nodes.push({
        id: fingerprint("heading", [node.content, title], node.position, counter++),
        kind: "heading",
        level: node.content,
        title,
        line: node.position && node.position.start ? node.position.start.line : null,
      });
    } else if (node.type === "macro" && CITATION_MACROS.has(node.content)) {
      // Split a multi-key \cite{a,b,c} into one citation node per key,
      // rather than one blob node with key="a,b,c" (the v0 behavior).
      const raw = lastArgText(node, EMPTY_MAP);
      const keys = raw ? raw.split(",").map((k) => k.trim()).filter(Boolean) : [];
      const line = node.position && node.position.start ? node.position.start.line : null;
      if (keys.length === 0) {
        nodes.push({
          id: fingerprint("citation", ["(no key)"], node.position, counter++),
          kind: "citation",
          key: "(no key)",
          line,
        });
      } else {
        for (const key of keys) {
          nodes.push({ id: fingerprint("citation", [key], node.position, counter++), kind: "citation", key, line });
        }
      }
    } else if (node.type === "environment" && typeof node.env === "string" && STRUCTURAL_ENVIRONMENTS.has(node.env)) {
      // Scoped (not `findFirstMacro`): a caption/label physically inside a
      // NESTED structural node (e.g. a `tabular` float's own caption inside
      // an outer `table`) must never be attributed to this outer node -- see
      // `findFirstMacroInOwnScope`'s own comment.
      const captionMacro = findFirstMacroInOwnScope(node.content, "caption");
      const labelMacro = findFirstMacroInOwnScope(node.content, "label");
      const kind = REF_KIND_FOR_ENV(node.env);
      const caption = captionMacro ? argText(captionMacro, labelToNumber) : null;
      const label = labelMacro ? argText(labelMacro, EMPTY_MAP) : null;
      // Tables/figures fingerprint on caption + label (either is enough to
      // be stable content); equation-like environments fingerprint on
      // label alone, per the design doc -- a bare, unlabeled equation has
      // no caption concept at all, so it falls through to the positional
      // fallback below.
      const fingerprintParts = kind === "equation" ? [label] : [caption, label];
      nodes.push({
        id: fingerprint(kind, fingerprintParts, node.position, counter++),
        kind,
        env: node.env,
        caption,
        label,
        // The exact source line each field's OWN macro starts on -- distinct
        // from `line` (the environment's own start) since a caption/label
        // commonly sits on its own line, possibly many lines into a
        // multi-line table/figure block. Lets a caller (popup.js) fetch and
        // edit just that one line, the same safe single-line-scan pattern
        // already used for heading/citation editing, instead of guessing
        // across the whole `[line, end_line]` span. `null` when the field
        // itself doesn't exist on this node.
        captionLine: macroLine(captionMacro),
        labelLine: macroLine(labelMacro),
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
      // Inline/display math (`$...$`, `\[...\]`) carries no label of its
      // own in this v0 node shape, so it always uses the positional
      // fallback id -- there is no stable content to hash for it at all.
      nodes.push({
        id: fingerprint("equation", [], node.position, counter++),
        kind: "equation",
        line: node.position && node.position.start ? node.position.start.line : null,
      });
      return;
    }

    if ("content" in node && Array.isArray(node.content)) visit(node.content);
    if ("args" in node && node.args) for (const a of node.args) visit(a.content);
  }

  visit(ast.content);
  disambiguateIds(nodes);
  return nodes;
}

export function outlineFile(path: string): OutlineNode[] {
  const source = readFileSync(path, "utf-8");
  const ast = parse(source);
  return extractOutline(ast);
}

/** Same as outlineFile, but for raw .tex source text (e.g. read live from
 * an editor DOM) instead of a path on disk. */
export function outlineText(source: string): OutlineNode[] {
  const ast = parse(source);
  return extractOutline(ast);
}
