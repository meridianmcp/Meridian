// Ported from Meridian's own docparse/latex_intel.py's `_expand_section_macros`
// (packages/docparse/docparse/latex_intel.py lines 197-242, plus its supporting
// regexes _NEWCOMMAND_HEAD_RE/_SECTION_MACRO_ALT/_SECTION_IN_BODY_RE at lines
// 116-121 and _brace_match at lines 124-139 -- a DIFFERENT project's
// document-intelligence package, read for the algorithm only; this file's code
// is original), adapted to meridian-latex's real problem: outline.js's heading
// walker (`extractOutline`) only recognizes a FIXED macro-name set as
// section-like headings (its own `SECTION_MACROS` constant: part, chapter,
// section, subsection, subsubsection, paragraph, subparagraph). A real paper
// that defines `\newcommand{\mysection}[1]{\section{#1}}` and then writes
// `\mysection{Foo}` is completely invisible to outline.js today -- `Foo` never
// becomes a heading node. This module is a PRE-PROCESSING text transform meant
// to run on raw .tex source BEFORE outline.js/unified-latex parses it; wiring
// it into outline.js's actual parse pipeline is a separate, later integration
// step -- NOT done here. This module only proves the transform itself is
// correct, via tests against raw strings.

import { matchBraceIndex } from "./range-locate.js";

/** Every sectioning macro name outline.js's own `SECTION_MACROS` constant
 * recognizes (see outline.js, near its top) -- kept in sync by hand since
 * outline.js doesn't export it. */
const SECTION_MACRO_NAMES = [
  "part",
  "chapter",
  "section",
  "subsection",
  "subsubsection",
  "paragraph",
  "subparagraph",
];

/** A `\newcommand`/`\renewcommand` definition head, up to the opening brace of
 * its body: captures the defined macro name in group 1. Handles the star
 * form, an optional `[nargs]` count and an optional `[default]` first-arg
 * value -- direct translation of the Python port's `_NEWCOMMAND_HEAD_RE`;
 * this particular pattern uses no Python-only regex constructs, so the
 * structure carries over unchanged. Must stay `g` (global) so repeated
 * `.exec()` calls advance through the whole source. */
const NEWCOMMAND_HEAD_RE = /\\(?:re)?newcommand\*?\s*\{\s*\\([A-Za-z@]+)\s*\}\s*(?:\[\d+\])?\s*(?:\[[^\]]*\])?\s*\{/g;

/** Matches any of `SECTION_MACRO_NAMES` as a macro use, e.g. `\section`.
 * Translation of the Python port's `_SECTION_MACRO_ALT` + `_SECTION_IN_BODY_RE`
 * (there built dynamically from `_SECTION_LEVELS`; hardcoded here since
 * `SECTION_MACRO_NAMES` above is already the single source of truth). */
const SECTION_IN_BODY_RE = new RegExp(`\\\\(?:${SECTION_MACRO_NAMES.join("|")})\\b`);

/** Escapes a string for safe interpolation into a `RegExp` -- `name` below is
 * user-controlled text (a macro name pulled from the document being
 * processed), matching the Python port's own use of `re.escape(name)`. */
function escapeRegExp(str: string): string {
  return str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Given `text[openIdx] === "{"`, returns `{ body, end }`: `body` is the
 * brace group's inner content (exclusive of the braces) and `end` is the
 * index just past the closing brace. Depth-aware (via `matchBraceIndex`,
 * reused rather than reimplemented) so nested `{...}` inside the group are
 * handled correctly. If the brace is never closed (malformed source),
 * matches the Python port's own `_brace_match` fallback: returns everything
 * after `openIdx` as `body` and `text.length` as `end`, rather than failing. */
function braceMatch(text: string, openIdx: number): { body: string; end: number } {
  const closeIdx = matchBraceIndex(text, openIdx);
  if (closeIdx === -1) {
    return { body: text.slice(openIdx + 1), end: text.length };
  }
  return { body: text.slice(openIdx + 1, closeIdx), end: closeIdx + 1 };
}

/**
 * Rewrites section-aliasing `\newcommand` macros to their base section macro.
 *
 * Detects `\newcommand{\name}[1]{...}` (or `\renewcommand`, optionally
 * starred) definitions whose body contains a sectioning macro (one of
 * `SECTION_MACRO_NAMES`) AND the literal substring `"#1"` -- a single-argument
 * section-alias, matching the Python port's own documented scope: "Full TeX
 * macro expansion is out of scope; this handles the common single-argument
 * section-alias case." For each one found, every definition of `\name` is
 * removed from the source and every remaining use of `\name` is rewritten to
 * `\target` (word-boundary-safe, so `\namex` is left alone).
 *
 * Pure and synchronous. Never throws -- any internal error returns the
 * ORIGINAL, unmodified `source`, matching the Python port's own defensive,
 * best-effort contract exactly. Returns `source` unchanged (the identical
 * string reference) when no alias-shaped `\newcommand` is found at all.
 *
 * @param source raw .tex source text.
 * @returns the source with section-alias macros expanded.
 */
export function expandSectionAliases(source: string): string {
  try {
    const aliases = new Map<string, string>();
    NEWCOMMAND_HEAD_RE.lastIndex = 0;
    let headMatch: RegExpExecArray | null;
    while ((headMatch = NEWCOMMAND_HEAD_RE.exec(source)) !== null) {
      const name = headMatch[1];
      // The trailing "{" the head pattern ends on is the last character of
      // the match -- headMatch.index + headMatch[0].length - 1, mirroring
      // the Python port's own `m.end() - 1`.
      const openIdx = headMatch.index + headMatch[0].length - 1;
      const { body } = braceMatch(source, openIdx);
      const bodyMatch = SECTION_IN_BODY_RE.exec(body);
      if (bodyMatch && body.includes("#1")) {
        // bodyMatch[0] is like "\section"; strip the leading backslash.
        aliases.set(name, bodyMatch[0].slice(1));
      }
    }
    if (aliases.size === 0) return source;

    let result = source;
    for (const [name, target] of aliases) {
      // 1. Remove this macro's definition(s) so its self-reference inside
      //    \newcommand{\name}{...} isn't rewritten into \newcommand{\section}.
      //    Loop until no more matches remain (there could be more than one
      //    \newcommand{\name}... in the source), searching from the position
      //    left off after the previous definition's body -- mirroring the
      //    Python port's own `defpat.search(result, i)` loop.
      const defRe = new RegExp(
        `\\\\(?:re)?newcommand\\*?\\s*\\{\\s*\\\\${escapeRegExp(name)}\\s*\\}` +
          `\\s*(?:\\[\\d+\\])?\\s*(?:\\[[^\\]]*\\])?\\s*\\{`,
        "g",
      );
      const pieces: string[] = [];
      let i = 0;
      while (true) {
        defRe.lastIndex = i;
        const defMatch = defRe.exec(result);
        if (!defMatch) {
          pieces.push(result.slice(i));
          break;
        }
        pieces.push(result.slice(i, defMatch.index));
        const openIdx = defMatch.index + defMatch[0].length - 1;
        const { end } = braceMatch(result, openIdx);
        i = end;
      }
      result = pieces.join("");
      // 2. Rewrite uses \name -> \target (word-boundary so \namex is safe).
      //    Translation of the Python port's negative-lookahead word-boundary
      //    check: `re.sub(r"\\" + re.escape(name) + r"(?![A-Za-z@])", ...)`.
      const useRe = new RegExp(`\\\\${escapeRegExp(name)}(?![A-Za-z@])`, "g");
      result = result.replace(useRe, `\\${target}`);
    }
    return result;
  } catch {
    // Best-effort, matching the Python port's own `except Exception: return source`.
    return source;
  }
}
