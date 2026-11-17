// Linear-time scanners for LaTeX command arguments.
//
// Each one replaces a regular expression that CodeQL flagged as polynomial
// ReDoS (js/polynomial-redos). Those patterns found an opener and then scanned
// forward to a closer with a lazy or negated class; when the closer was missing,
// every opener rescanned to the end of the source. The scanners here match the
// short, unambiguous head with a regex and locate the closer with indexOf or a
// binary search, so a failing head never rescans the rest of the source.
//
// Results keep the RegExpMatchArray shape (`index`, `[0]`, `[1]`), so callers
// read them the same way they read `matchAll` output.

/** A match in `RegExpMatchArray` shape: `index`, the whole match and the first capture. */
export interface ScanMatch {
  index: number;
  0: string;
  1: string;
}

const SPACE_RE = /\s/;
const BIBITEM_HEAD = "\\\\bibitem";

/** Index of the first character at or after `from` that is not whitespace. */
function skipSpace(text: string, from: number): number {
  let i = from;
  while (i < text.length && SPACE_RE.test(text[i])) i++;
  return i;
}

/** Sorted positions of every occurrence of `ch` in `text`. */
function positionsOf(text: string, ch: string): number[] {
  const out: number[] = [];
  for (let p = text.indexOf(ch); p !== -1; p = text.indexOf(ch, p + 1)) out.push(p);
  return out;
}

/** The first entry of the sorted `positions` at or after `from`, or -1. */
function firstAtOrAfter(positions: number[], from: number): number {
  let lo = 0;
  let hi = positions.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (positions[mid] < from) lo = mid + 1;
    else hi = mid;
  }
  return lo < positions.length ? positions[lo] : -1;
}

/**
 * Finds `\cmd{...}` arguments. `headPattern` must match through the opening
 * brace (for example `/\\bibliography\s*\{/g`). The argument runs to the first
 * `}` after that brace, like `\{([^}]*)\}`.
 *
 * Only whitespace separates a head from its brace, so no later head can start
 * inside this one. When a brace has no `}` after it, no later brace does either,
 * so scanning can stop.
 */
export function scanBraceArguments(source: string, headPattern: RegExp): ScanMatch[] {
  const head = new RegExp(headPattern.source, "g");
  const out: ScanMatch[] = [];
  let m: RegExpExecArray | null;
  while ((m = head.exec(source)) !== null) {
    const open = m.index + m[0].length - 1;
    const close = source.indexOf("}", open + 1);
    if (close === -1) break;
    out.push({ index: m.index, 0: source.slice(m.index, close + 1), 1: source.slice(open + 1, close) });
    head.lastIndex = close + 1;
  }
  return out;
}

/**
 * Finds `\bibitem[label]{key}` heads and captures each key, like
 * `/\\bibitem\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}/g`.
 *
 * A head that fails does not cost a rescan of the body. The label's closing
 * `]` comes from a sorted index, the brace after it is memoised, and a `}` is
 * only searched for when one is known to exist. A failed head resumes the
 * search just after its own `\bibitem`, because an optional label can contain
 * a later `\bibitem` head.
 */
export function scanBibitems(body: string): ScanMatch[] {
  const brackets = positionsOf(body, "]");
  const lastBrace = body.lastIndexOf("}");
  const braceAfterClose = new Map<number, number>();
  const braceAfter = (close: number): number => {
    let brace = braceAfterClose.get(close);
    if (brace === undefined) {
      const at = skipSpace(body, close + 1);
      brace = body[at] === "{" ? at : -1;
      braceAfterClose.set(close, brace);
    }
    return brace;
  };

  const head = new RegExp(BIBITEM_HEAD, "g");
  const out: ScanMatch[] = [];
  let m: RegExpExecArray | null;
  while ((m = head.exec(body)) !== null) {
    const start = m.index;
    const first = skipSpace(body, start + m[0].length);
    let open = -1;
    if (body[first] === "[") {
      const close = firstAtOrAfter(brackets, first + 1);
      if (close !== -1) open = braceAfter(close);
    } else if (body[first] === "{") {
      open = first;
    }
    if (open === -1 || lastBrace <= open) {
      head.lastIndex = start + 1;
      continue;
    }
    const close = body.indexOf("}", open + 1);
    out.push({ index: start, 0: body.slice(start, close + 1), 1: body.slice(open + 1, close) });
    head.lastIndex = close + 1;
  }
  return out;
}

/** Finds `\newcommand`/`\renewcommand` definition heads for one source text. */
export interface NewcommandScanner {
  /**
   * The first definition head at or after `from`. Its `[0]` runs through the
   * body's opening brace, and its `[1]` is the macro name. `nameSource` is a
   * regex source for the name; by default any `[A-Za-z@]+` name matches.
   */
  next(from: number, nameSource?: string): ScanMatch | null;
}

/**
 * Creates a scanner for `\newcommand`/`\renewcommand` heads in `text`. A head is
 * the optional star, `{\name}`, optional `[n]`, optional `[default]`, and then
 * the body's `{`, as in `\\(?:re)?newcommand\*?\s*\{\s*\\([A-Za-z@]+)\s*\}\s*(?:\[\d+\])?\s*(?:\[[^\]]*\])?\s*\{`.
 *
 * The original pattern had back-to-back whitespace runs and optional groups,
 * which backtracked polynomially in the length of a whitespace run. The tail is
 * parsed here in explicit steps instead, and each optional `[...]` closer is
 * found with a binary search.
 */
export function createNewcommandScanner(text: string): NewcommandScanner {
  let brackets: number[] | undefined;
  const braceAfterClose = new Map<number, number>();
  const braceAfter = (close: number): number => {
    let brace = braceAfterClose.get(close);
    if (brace === undefined) {
      const at = skipSpace(text, close + 1);
      brace = text[at] === "{" ? at : -1;
      braceAfterClose.set(close, brace);
    }
    return brace;
  };

  // The `{` that opens the body, given the position just after the name's `}`.
  const bodyBrace = (afterName: number): number => {
    const first = skipSpace(text, afterName);
    // The optional `[\d+]` is tried first, as the regex does, and then skipped.
    const continuations: number[] = [];
    if (text[first] === "[") {
      let d = first + 1;
      while (d < text.length && text[d] >= "0" && text[d] <= "9") d++;
      if (d > first + 1 && text[d] === "]") continuations.push(d + 1);
    }
    continuations.push(first);
    for (const after of continuations) {
      const at = skipSpace(text, after);
      if (text[at] === "[") {
        brackets ??= positionsOf(text, "]");
        const close = firstAtOrAfter(brackets, at + 1);
        const brace = close === -1 ? -1 : braceAfter(close);
        if (brace !== -1) return brace;
      } else if (text[at] === "{") {
        return at;
      }
    }
    return -1;
  };

  return {
    next(from: number, nameSource = "[A-Za-z@]+"): ScanMatch | null {
      const head = new RegExp(`\\\\(?:re)?newcommand\\*?\\s*\\{\\s*\\\\(${nameSource})\\s*\\}`, "g");
      head.lastIndex = from;
      let m: RegExpExecArray | null;
      while ((m = head.exec(text)) !== null) {
        const brace = bodyBrace(m.index + m[0].length);
        if (brace !== -1) {
          return { index: m.index, 0: text.slice(m.index, brace + 1), 1: m[1] };
        }
        head.lastIndex = m.index + 1;
      }
      return null;
    },
  };
}
