// Pure, framework-agnostic text-range location for the editable fields the
// outline engine exposes (heading title, citation key, table/figure
// caption, equation-like label) -- ported from extension/popup.js's own
// write-dispatch primitive (locateHeadingRange/locateCitationRange/
// locateCaptionOrLabelRange and their computeFieldRange dispatcher),
// UNCHANGED in logic, so the OT write path (write.js) gets the exact same
// occurrence-count and text-match safety nets the already-live,
// human-verified browser-extension edit flow relies on.
//
// Deliberately duplicated here rather than imported from one shared module:
// extension/popup.js is a plain (non-ESM, no bundler) browser script loaded
// via `<script src="popup.js">`, and per Adam's 2026-09-24 direction the
// browser-extension write path is being DEprioritized in favor of this OT
// path -- restructuring popup.js's script loading into an ES module to
// share code with a Node package is a real change to a still-working path
// that isn't worth making for something being sidelined. If the extension
// path is ever fully retired, these two copies can be unified then.
//
// Every function here is pure: given a `{from, to, text}` lineInfo (the
// line-relative character range's absolute doc offsets plus its raw text --
// in the extension this comes from CodeMirror's own `state.doc.line(n)` via
// injected.js's getLineInfo(); here it comes from write.js's
// `lineInfoForLine()` reading Overleaf's own `lines` array instead), plus
// the outline node(s) involved, and returns either `{ok:true, from, to}`
// (absolute doc-offset range of the field's raw text, exclusive of any
// surrounding braces) or `{ok:false, reason}`. Never throws.

/** A line-relative-to-absolute-offset window: the line's own raw text plus
 * where it starts/ends in the whole document (see write.js's
 * `lineInfoForLine`). */
export interface LineInfo {
  from: number;
  to: number;
  text: string;
}

/** One `{...}` argument's content span, exclusive of the braces
 * themselves -- shared by findAllBraceArgs/findMacroBraceArgs. */
export interface BraceArg {
  start: number;
  end: number;
}

/** One comma-separated `\cite{}` key segment: its trimmed span plus the
 * trimmed text itself (see findCiteSegments). */
export interface CiteSegment extends BraceArg {
  text: string;
}

export type RangeResult = { ok: true; from: number; to: number } | { ok: false; reason: string };

/**
 * The loosely-typed shape of an outline node (as produced by outline.js's
 * `extractOutline`) that this file's functions read from -- deliberately
 * ALL-optional (plus an index signature) rather than a strict discriminated
 * union: real callers (and this file's own test suite) pass minimal,
 * per-function-relevant slices of a node (e.g. `{ captionLine: 10 }` alone
 * for `siblingsOnFieldLine`), never the full node shape, and outline.js
 * itself stays untouched JS in this batch so there is no shared, importable
 * node type to depend on yet (see the file-level comment above on why the
 * two modules stay independently defined rather than sharing one import).
 */
export interface OutlineNodeLike {
  id?: string;
  kind?: string;
  line?: number | null;
  level?: string;
  key?: string;
  caption?: string | null;
  label?: string | null;
  captionLine?: number | null;
  labelLine?: number | null;
  [extra: string]: unknown;
}

/** A heading-kind OutlineNodeLike, narrowed to require `level` -- the one
 * field locateHeadingRange truly cannot proceed without (it feeds
 * findMacroBraceArgs, which needs a real macro name to search for). */
type HeadingLikeNode = OutlineNodeLike & { level: string };

/** Index of the first `}` that closes the `{` at `text[openIdx]`, tracking
 * nested-brace depth (so `\section{A \textbf{B}}` still resolves to the
 * OUTER closing brace, not the first `}` encountered). Returns -1 if the
 * text never balances back to depth 0 (an unclosed brace on this line --
 * e.g. a macro argument that wraps onto the next line, which this v1 does
 * not follow across lines). `text[openIdx]` must be `{`. */
export function matchBraceIndex(text: string, openIdx: number): number {
  let depth = 0;
  for (let i = openIdx; i < text.length; i++) {
    if (text[i] === "{") depth++;
    else if (text[i] === "}") {
      depth--;
      if (depth === 0) return i;
    }
  }
  return -1;
}

/** Every `{...}` argument immediately following each occurrence of
 * `macroPrefix` (a literal string ending in `{`, e.g. `"\\section{"`) on
 * `text`, left to right. Each result is `{start, end}` -- the argument's
 * content span, exclusive of the braces themselves. A macro occurrence with
 * no balanced closing brace on this line is skipped (not reported) rather
 * than guessed at. */
export function findAllBraceArgs(text: string, macroPrefix: string): BraceArg[] {
  const results: BraceArg[] = [];
  let searchFrom = 0;
  while (true) {
    const idx = text.indexOf(macroPrefix, searchFrom);
    if (idx === -1) break;
    const braceOpen = idx + macroPrefix.length - 1;
    const closeIdx = matchBraceIndex(text, braceOpen);
    if (closeIdx === -1) {
      searchFrom = idx + macroPrefix.length;
      continue;
    }
    results.push({ start: braceOpen + 1, end: closeIdx });
    searchFrom = closeIdx + 1;
  }
  return results;
}

/** Every `{...}` argument immediately following each occurrence of a macro
 * named `macroName` on `text`, left to right -- tolerant of an optional `*`
 * between the macro name and its argument brace (`\section{...}` AND
 * `\section*{...}` both match a search for macroName="section"). Each
 * result is `{start, end}` -- the argument's content span, exclusive of the
 * braces themselves. A macro occurrence with no balanced closing brace on
 * this line, or whose character immediately after the (optional) `*` isn't
 * `{`, is skipped (not reported) rather than guessed at. */
export function findMacroBraceArgs(text: string, macroName: string): BraceArg[] {
  const results: BraceArg[] = [];
  const anchor = `\\${macroName}`;
  let searchFrom = 0;
  while (true) {
    const idx = text.indexOf(anchor, searchFrom);
    if (idx === -1) break;
    let braceOpen = idx + anchor.length;
    if (text[braceOpen] === "*") braceOpen += 1;
    if (text[braceOpen] !== "{") {
      // Not a real match at all (e.g. this "\section" is actually the start
      // of "\subsectionfoo" or some other longer macro name, or a bare macro
      // with no argument on this line) -- move past just the anchor, not the
      // whole remaining line, so a genuine later occurrence is still found.
      searchFrom = idx + anchor.length;
      continue;
    }
    const closeIdx = matchBraceIndex(text, braceOpen);
    if (closeIdx === -1) {
      searchFrom = braceOpen + 1;
      continue;
    }
    results.push({ start: braceOpen + 1, end: closeIdx });
    searchFrom = closeIdx + 1;
  }
  return results;
}

/** Every comma-separated key segment inside every `\cite{...}` occurrence on
 * `text`, left to right, flattened into one ordered list -- mirroring
 * outline.js's own `raw.split(",").map(k => k.trim())` extraction exactly,
 * but keeping each segment's untrimmed `{start, end}` offsets (trimmed down
 * to the key's own span, so the returned range excludes surrounding
 * whitespace) alongside its trimmed `text`. A `\cite{}` with no balanced
 * closing brace on this line is skipped, same as findAllBraceArgs. */
export function findCiteSegments(text: string): CiteSegment[] {
  const segments: CiteSegment[] = [];
  const prefix = "\\cite{";
  let searchFrom = 0;
  while (true) {
    const idx = text.indexOf(prefix, searchFrom);
    if (idx === -1) break;
    const braceOpen = idx + prefix.length - 1;
    const closeIdx = matchBraceIndex(text, braceOpen);
    if (closeIdx === -1) {
      searchFrom = idx + prefix.length;
      continue;
    }
    const argStart = braceOpen + 1;
    const argText = text.slice(argStart, closeIdx);
    let partOffset = 0;
    for (const part of argText.split(",")) {
      const rawStart = argStart + partOffset;
      const leadWs = part.match(/^\s*/)![0].length;
      const trailWs = part.match(/\s*$/)![0].length;
      segments.push({
        start: rawStart + leadWs,
        end: rawStart + part.length - trailWs,
        text: part.trim(),
      });
      partOffset += part.length + 1; // +1 for the comma consumed by split()
    }
    searchFrom = closeIdx + 1;
  }
  return segments;
}

/** Every node in `nodes` that shares `target`'s `kind` and `line` -- and,
 * for a heading, its `level` too (so a `\section{}` on the same line as an
 * unrelated `\subsection{}` never gets confused for a sibling). Order
 * matches `nodes`' own array order, which is document order (see
 * outline.js's visit()) -- the same left-to-right order the locate*
 * functions below scan the raw line text in, so the Nth sibling here should
 * always line up with the Nth occurrence found in the text. Used to
 * disambiguate WHICH occurrence on a line is this specific node, for the
 * (rare, but real) case of more than one same-kind node on one physical
 * line. */
export function siblingsOnLine(nodes: OutlineNodeLike[], target: OutlineNodeLike): OutlineNodeLike[] {
  return nodes.filter(
    (n) => n.kind === target.kind && n.line === target.line && (target.kind !== "heading" || n.level === target.level),
  );
}

/** Every node in `nodes` whose OWN `lineField` (e.g. "captionLine") equals
 * `target`'s -- the caption/label analogue of `siblingsOnLine` above, used
 * to disambiguate which occurrence on that line is this specific node's
 * field when more than one node's same-named field happens to land on the
 * identical physical source line. `null`-valued fields never match. */
export function siblingsOnFieldLine(
  nodes: OutlineNodeLike[],
  target: OutlineNodeLike,
  lineField: "captionLine" | "labelLine",
): OutlineNodeLike[] {
  return nodes.filter((n) => n[lineField] != null && n[lineField] === target[lineField]);
}

/** Computes `{from, to}` (line-relative, added to `lineInfo.from` by the
 * caller) for a heading node's title argument. Does NOT hard-abort on a
 * text mismatch between the outline's `title` and the raw source at that
 * span -- a title's rendered text can legitimately differ from raw source
 * when it contains nested macros (e.g. `\section{A \textit{B}}` renders as
 * title "A B"), so an exact-text check would false-positive on real,
 * unremarkable documents. The occurrence-COUNT check (siblings vs. brace
 * occurrences found) is the real safety net here. */
export function locateHeadingRange(lineInfo: LineInfo, target: HeadingLikeNode, siblings: OutlineNodeLike[]): RangeResult {
  const idx = siblings.findIndex((n) => n.id === target.id);
  if (idx === -1) {
    return { ok: false, reason: "Internal error: node not found among its own line siblings." };
  }
  const occurrences = findMacroBraceArgs(lineInfo.text, target.level);
  if (occurrences.length !== siblings.length) {
    return {
      ok: false,
      reason:
        `Heading count mismatch on line ${target.line}: the outline reports ${siblings.length} ` +
        `"${target.level}" heading(s) there, but ${occurrences.length} "\\${target.level}" (optionally ` +
        `starred) occurrence(s) were found scanning the live line text. Aborting edit for safety.`,
    };
  }
  const occ = occurrences[idx];
  return { ok: true, from: lineInfo.from + occ.start, to: lineInfo.from + occ.end };
}

/** Computes `{from, to}` (line-relative) for a citation node's key. Unlike
 * the heading case above, THIS one hard-aborts on a text mismatch: a
 * citation key inside `\cite{...}` has no legitimate reason to render
 * differently from its raw source (no nested macros expected there), so a
 * mismatch means this function's reconstruction of the line has diverged
 * from what outline.js actually saw -- safer to abort than guess. */
export function locateCitationRange(lineInfo: LineInfo, target: OutlineNodeLike, siblings: OutlineNodeLike[]): RangeResult {
  const idx = siblings.findIndex((n) => n.id === target.id);
  if (idx === -1) {
    return { ok: false, reason: "Internal error: node not found among its own line siblings." };
  }
  const segments = findCiteSegments(lineInfo.text);
  if (segments.length !== siblings.length) {
    return {
      ok: false,
      reason:
        `Citation count mismatch on line ${target.line}: the outline reports ${siblings.length} ` +
        `citation(s) there, but ${segments.length} were found scanning the live line text. ` +
        `Aborting edit for safety.`,
    };
  }
  const seg = segments[idx];
  if (seg.text !== target.key) {
    return {
      ok: false,
      reason:
        `Citation key mismatch at position ${idx} on line ${target.line}: expected "${target.key}", ` +
        `found "${seg.text}" in the live document. Aborting edit for safety.`,
    };
  }
  return { ok: true, from: lineInfo.from + seg.start, to: lineInfo.from + seg.end };
}

/** Which macro/field/property triple `locateCaptionOrLabelRange` resolves
 * for -- shared by locateCaptionRange (caption) and locateLabelRange
 * (label) below. */
interface CaptionOrLabelSpec {
  lineField: "captionLine" | "labelLine";
  nodeProp: "caption" | "label";
  macroName: string;
}

/** Computes `{from, to}` (line-relative) for a table/figure node's caption,
 * or an equation-like node's label -- shared by locateCaptionRange and
 * locateLabelRange below, parameterized by which macro/line-field/node
 * property to use. Same occurrence-COUNT safety net as
 * locateHeadingRange/locateCitationRange: if the number of `\macroName{`
 * occurrences found scanning the live line doesn't match the number of
 * outline nodes that claim that exact line for this field, abort rather
 * than guess. Unlike the heading case (which tolerates a rendered/raw text
 * mismatch for legitimate nested-macro reasons) this hard-aborts on a text
 * mismatch too, same as citation. */
export function locateCaptionOrLabelRange(
  lineInfo: LineInfo,
  target: OutlineNodeLike,
  nodes: OutlineNodeLike[],
  { lineField, nodeProp, macroName }: CaptionOrLabelSpec,
): RangeResult {
  const siblings = siblingsOnFieldLine(nodes, target, lineField);
  const idx = siblings.findIndex((n) => n.id === target.id);
  if (idx === -1) {
    return { ok: false, reason: "Internal error: node not found among its own field-line siblings." };
  }
  const occurrences = findAllBraceArgs(lineInfo.text, `\\${macroName}{`);
  if (occurrences.length !== siblings.length) {
    return {
      ok: false,
      reason:
        `${macroName} count mismatch on line ${target[lineField]}: the outline reports ${siblings.length} ` +
        `node(s) with a ${macroName} there, but ${occurrences.length} "\\${macroName}{" occurrence(s) were ` +
        `found scanning the live line text. Aborting edit for safety.`,
    };
  }
  const occ = occurrences[idx];
  const found = lineInfo.text.slice(occ.start, occ.end);
  if (found !== target[nodeProp]) {
    return {
      ok: false,
      reason:
        `${macroName} text mismatch at position ${idx} on line ${target[lineField]}: expected ` +
        `"${target[nodeProp]}", found "${found}" in the live document. Aborting edit for safety.`,
    };
  }
  return { ok: true, from: lineInfo.from + occ.start, to: lineInfo.from + occ.end };
}

export function locateCaptionRange(lineInfo: LineInfo, target: OutlineNodeLike, nodes: OutlineNodeLike[]): RangeResult {
  return locateCaptionOrLabelRange(lineInfo, target, nodes, {
    lineField: "captionLine",
    nodeProp: "caption",
    macroName: "caption",
  });
}

export function locateLabelRange(lineInfo: LineInfo, target: OutlineNodeLike, nodes: OutlineNodeLike[]): RangeResult {
  return locateCaptionOrLabelRange(lineInfo, target, nodes, {
    lineField: "labelLine",
    nodeProp: "label",
    macroName: "label",
  });
}

/** The source line number (1-indexed, matching outline.js/CodeMirror's own
 * convention) that holds `fieldName` for `target`, or `null`/`undefined` if
 * this node/field combination has no line to read at all (see
 * EDITABLE_FIELDS' own `present` gate in popup.js -- the same absence this
 * mirrors). Heading and citation nodes ignore `fieldName` and always
 * resolve to the node's own `line` (title/key is their only editable
 * field); table/figure/equation nodes resolve `captionLine`/`labelLine`
 * depending on `fieldName`. */
export function fieldLineNumber(target: OutlineNodeLike, fieldName: string): number | null | undefined {
  if (target.kind === "heading" || target.kind === "citation") return target.line;
  if (fieldName === "caption") return target.captionLine;
  if (fieldName === "label") return target.labelLine;
  return null;
}

/** Resolves `target` down to an absolute `{from, to}` document offset for
 * ONE of its editable fields (`fieldName`), given `lineInfo` already read
 * for the correct line (see `fieldLineNumber`) -- the pure dispatch core of
 * popup.js's own `computeFieldRange`, with the DOM/tab I/O split out so
 * both the browser extension and this engine's OT write path (write.js)
 * can share it. `nodes` is the full fresh outline array (needed to compute
 * `target`'s siblings for occurrence disambiguation). Returns
 * `{ok: true, from, to}` or `{ok: false, reason}`. Never throws. */
export function resolveFieldRange(
  lineInfo: LineInfo,
  target: OutlineNodeLike,
  nodes: OutlineNodeLike[],
  fieldName: string,
): RangeResult {
  if (target.kind === "heading" || target.kind === "citation") {
    const siblings = siblingsOnLine(nodes, target);
    return target.kind === "heading"
      ? locateHeadingRange(lineInfo, target as HeadingLikeNode, siblings)
      : locateCitationRange(lineInfo, target, siblings);
  }
  if (fieldName === "caption") return locateCaptionRange(lineInfo, target, nodes);
  if (fieldName === "label") return locateLabelRange(lineInfo, target, nodes);
  return { ok: false, reason: `Editing field "${fieldName}" on kind "${target.kind}" is not supported.` };
}
