// BibTeX / \bibitem bibliography extraction -- a genuinely new capability
// for this engine (zero .bib/bibliography support existed here before this
// file). Ported from Meridian's own document-intelligence package,
// packages/docparse/docparse/latex_intel.py lines 537-712 (functions
// _parse_bibitems, _split_bibtex_fields, parse_bibtex, get_bibliography),
// read in full before this port -- same regexes (translated to JS RegExp:
// Python's re.DOTALL isolation of the thebibliography body becomes a
// `[\s\S]*?` non-greedy span instead of relying on a dotAll flag), same
// brace/quote-aware field parsing, same tolerant-of-malformed-input
// contract (never throws; partial results on bad input).
//
// I/O-free adaptation: this engine has no local filesystem in its data
// model (all doc content is fetched over the network via project-tree.js +
// OverleafProjectSession.joinDoc), unlike the Python source's
// os.path.isfile/open against a base_dir. getBibliography therefore takes
// an optional, injected SYNCHRONOUS `resolveBibText(bibFileName)` resolver
// in place of base_dir + local file reads -- resolving the actual network
// fetch for a referenced .bib file is the CALLER's responsibility (e.g. via
// project-tree.js's resolveDocIdByPath + session.joinDoc), not this
// module's job. Omitting resolveBibText mirrors the Python's own
// base_dir=None fallback: external \bibliography{}/\addbibresource{}
// references are silently skipped and only inline \bibitem entries surface.

import { matchBraceIndex } from "./range-locate.js";

/**
 * matchBraceIndex (range-locate.js) returns -1 for an unbalanced/never-
 * closing brace. Both of latex_intel.py's own brace-matching loops this
 * file ports (_split_bibtex_fields' field-value loop and parse_bibtex's
 * entry-body loop) do NOT treat that as an error: their `while j < n` loop
 * simply exits with `j === n`, and the caller takes `body[start:n]` -- i.e.
 * "run to the end of the string" -- rather than failing or skipping. This
 * wrapper is a clean, correct adaptation that reuses matchBraceIndex's real
 * depth-counting (rather than reimplementing it) while preserving that
 * exact end-of-string fallback, so malformed/truncated bibtex (a real,
 * expected case for hand-edited .bib files -- an unterminated brace should
 * degrade gracefully, not vanish) behaves identically to the Python source.
 */
function closeBraceOrEnd(text: string, openIdx: number): number {
  const closeIdx = matchBraceIndex(text, openIdx);
  return closeIdx === -1 ? text.length : closeIdx;
}

/** One `\bibitem{key} text` entry (inline `thebibliography` mechanism). */
export interface BibitemEntry {
  key: string;
  type: "bibitem";
  raw: string;
}

/**
 * Extract `\bibitem{key} text` entries from a thebibliography block.
 *
 * Regex-based (not AST) because a `\bibitem` body is free-form LaTeX text
 * that runs until the next `\bibitem` or `\end{thebibliography}`; this is
 * the robust, standard way to slice them. Isolates the thebibliography
 * environment body when present, else scans the whole source. Returns
 * `{key, type: "bibitem", raw}` per entry, whitespace-normalized (internal
 * whitespace runs collapsed to a single space, then trimmed). Malformed
 * input yields whatever entries parsed -- never throws.
 */
export function parseBibitems(source: string): BibitemEntry[] {
  const entries: BibitemEntry[] = [];
  const env = source.match(
    /\\begin\{thebibliography\}(?:\{[^}]*\})?([\s\S]*?)\\end\{thebibliography\}/,
  );
  const body = env ? env[1] : source;
  // Split on \bibitem, keeping the (optional [label]) and mandatory {key}.
  const pattern = /\\bibitem\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}/g;
  const matches = [...body.matchAll(pattern)];
  for (let i = 0; i < matches.length; i++) {
    const m = matches[i];
    const key = m[1].trim();
    const start = (m.index ?? 0) + m[0].length;
    const end = i + 1 < matches.length ? matches[i + 1].index ?? body.length : body.length;
    const raw = body
      .slice(start, end)
      .replace(/\s+/g, " ")
      .trim();
    entries.push({ key, type: "bibitem", raw });
  }
  return entries;
}

/** Bare `field = value` map parsed from one bibtex entry body, keys
 * lowercased -- see `splitBibtexFields`. */
type BibtexFields = Record<string, string>;

/**
 * Parse `field = {value}` / `field = "value"` / bare-value pairs from a
 * bibtex entry body. Brace-aware: a `{...}` value may contain nested
 * balanced braces (common in titles, e.g. `{A {Bold} Title}`); a `"..."`
 * value runs to the next unescaped quote; anything else is a bare value
 * (a number, a macro, a cross-ref) read up to the next comma. Internal
 * whitespace in every value is collapsed to a single space and trimmed.
 * Keys are lowercased. Tolerant of junk between fields (skips to the next
 * comma and retries) rather than aborting. Internal helper -- not exported,
 * matching the Python source's own leading-underscore-as-private
 * convention for `_split_bibtex_fields`.
 */
function splitBibtexFields(body: string): BibtexFields {
  const fields: BibtexFields = {};
  const n = body.length;
  let i = 0;
  while (i < n) {
    const m = body.slice(i).match(/^\s*([A-Za-z][A-Za-z0-9_-]*)\s*=\s*/);
    if (!m) {
      const comma = body.indexOf(",", i);
      if (comma === -1) break;
      i = comma + 1;
      continue;
    }
    const name = m[1].toLowerCase();
    i += m[0].length;
    if (i >= n) break;
    const ch = body[i];
    let value = "";
    if (ch === "{") {
      const j = closeBraceOrEnd(body, i);
      value = body.slice(i + 1, j);
      i = j + 1;
    } else if (ch === '"') {
      let j = i + 1;
      while (j < n && body[j] !== '"') j++;
      value = body.slice(i + 1, j);
      i = j + 1;
    } else {
      // Bare value (e.g. a number or a macro) up to the next comma.
      const comma = body.indexOf(",", i);
      const j = comma !== -1 ? comma : n;
      value = body.slice(i, j).trim();
      i = j;
    }
    fields[name] = value.replace(/\s+/g, " ").trim();
    // Advance past a trailing comma between fields.
    const nxt = body.indexOf(",", i);
    if (nxt === -1) break;
    i = nxt + 1;
  }
  return fields;
}

/** One `@type{key, field=..., ...}` entry (external .bib mechanism). */
export interface BibtexEntry {
  key: string;
  type: string;
  title: string;
  author: string;
  year: string;
  raw: string;
}

/**
 * Parse bibtex/biblatex `@type{key, field=..., ...}` entries.
 *
 * Returns `[{key, type, title, author, year, raw}]`. Entry bodies are
 * brace-matched via `closeBraceOrEnd` (see its own comment for why this
 * isn't a bare call to `matchBraceIndex`) so nested braces (e.g. inside a
 * title) don't truncate the entry early. Tolerant: `@comment`/`@preamble`/
 * `@string` entries and malformed bodies are skipped, never thrown.
 */
export function parseBibtexEntries(source: string): BibtexEntry[] {
  const entries: BibtexEntry[] = [];
  for (const m of source.matchAll(/@([A-Za-z]+)\s*\{/g)) {
    const etype = m[1].toLowerCase();
    if (etype === "comment" || etype === "preamble" || etype === "string") continue;
    const start = (m.index ?? 0) + m[0].length - 1; // index of the opening '{'
    const j = closeBraceOrEnd(source, start);
    const raw = source.slice(start + 1, j);
    // First comma separates the citation key from the fields.
    const comma = raw.indexOf(",");
    let key: string;
    let fields: BibtexFields;
    if (comma === -1) {
      key = raw.trim();
      fields = {};
    } else {
      key = raw.slice(0, comma).trim();
      fields = splitBibtexFields(raw.slice(comma + 1));
    }
    entries.push({
      key,
      type: etype,
      title: fields.title || "",
      author: fields.author || "",
      year: fields.year || "",
      raw: raw.replace(/\s+/g, " ").trim(),
    });
  }
  return entries;
}

export type BibliographyEntry = BibitemEntry | BibtexEntry;

export interface GetBibliographyOptions {
  /** Synchronous resolver for an external `\bibliography{}`/`\addbibresource{}`
   * name (already `.bib`-suffixed) -> that file's raw text, or `null`/
   * `undefined` if not found. Omitting this skips mechanism 2 entirely. */
  resolveBibText?: (bibFileName: string) => string | null | undefined;
}

/**
 * Extract bibliography entries from a LaTeX `source`.
 *
 * Handles two mechanisms:
 *
 * 1. An inline `thebibliography` environment with `\bibitem{key} ...`
 *    entries (via `parseBibitems`).
 * 2. `\bibliography{refs}` / `\addbibresource{refs.bib}` references to
 *    external `.bib` files -- each referenced name (comma-split, trimmed,
 *    `.bib` appended if missing) is resolved through the caller-supplied,
 *    SYNCHRONOUS `resolveBibText(bibFileName)` option (returning the file's
 *    text, or `null`/`undefined` if not found) and parsed with
 *    `parseBibtexEntries`. This is the I/O-free stand-in for the Python
 *    source's `base_dir` + local file read -- resolving the real network
 *    fetch (e.g. via project-tree.js + OverleafProjectSession.joinDoc) is
 *    the caller's job, not this module's. Omitting `resolveBibText` skips
 *    mechanism 2 entirely, matching the Python's own `base_dir=None`
 *    fallback.
 *
 * Returns a list of `{key, type, title, author, year, raw}` dicts (bibitem
 * entries omit title/author/year, leaving `raw` -- the same shape
 * `parseBibitems` itself returns). Both mechanisms' entries are appended
 * together if a source somehow has both. Robust: returns whatever entries
 * were successfully parsed so far on any error and never throws to the
 * caller.
 */
export function getBibliography(source: unknown, { resolveBibText }: GetBibliographyOptions = {}): BibliographyEntry[] {
  if (!source || typeof source !== "string") return [];
  const entries: BibliographyEntry[] = [];
  try {
    // 1. Inline thebibliography.
    if (source.includes("thebibliography") || source.includes("\\bibitem")) {
      entries.push(...parseBibitems(source));
    }

    // 2. External .bib via \bibliography{...} or \addbibresource{...}.
    const bibNames: string[] = [];
    for (const m of source.matchAll(/\\bibliography\s*\{([^}]*)\}/g)) {
      bibNames.push(...m[1].split(",").map((part) => part.trim()));
    }
    for (const m of source.matchAll(/\\addbibresource\s*\{([^}]*)\}/g)) {
      bibNames.push(...m[1].split(",").map((part) => part.trim()));
    }

    if (resolveBibText) {
      for (const name of bibNames) {
        if (!name) continue;
        const candidate = name.toLowerCase().endsWith(".bib") ? name : `${name}.bib`;
        try {
          const text = resolveBibText(candidate);
          if (text) {
            entries.push(...parseBibtexEntries(text));
          }
        } catch {
          // One bad/unresolvable .bib reference must not sink the rest.
          continue;
        }
      }
    }
  } catch {
    // Bibliography extraction is best-effort -- never crash the caller.
    return entries;
  }
  return entries;
}
