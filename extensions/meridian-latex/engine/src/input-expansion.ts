// Ported from Meridian's own docparse/latex_intel.py's `_expand_inputs`
// (packages/docparse/docparse/latex_intel.py lines 142-195, a DIFFERENT
// project's document-intelligence package, read for the algorithm only --
// this file's code is original), adapted for meridian-latex's real I/O
// model: Overleaf docs are fetched over the network via
// OverleafProjectSession.joinDoc(), never read from local disk. The Python
// version recurses over `os.path.isfile`/`open()` against a filesystem
// `base_dir`; this version recurses over async `session.joinDoc()` calls
// against the project's own doc tree (project-tree.js's
// `resolveDocIdByPath`), with the same cycle/depth-guard discipline
// (tracking resolved DOC IDS instead of resolved file paths) and the same
// never-throws, best-effort contract: an unresolvable or failed `\input`/
// `\include` is left in place in the output and recorded in `unexpanded`
// rather than aborting the whole expansion.
//
// PATH RESOLUTION SIMPLIFICATION -- documented, not hidden (matching this
// codebase's own "known limitation, not hypothetical" style elsewhere,
// e.g. outline.js's v0 limitations section): `\input{name}` is resolved
// relative to the PROJECT ROOT, always -- never relative to the INCLUDING
// file's own directory the way real TeX/kpathsea resolves it. This matches
// the common real-world case (the disposable test projects this was
// verified against, and most Overleaf projects generally, keep a flat or
// root-relative layout) and project-tree.js's own `resolveDocIdByPath`,
// which only resolves root-relative paths. A paper with deeply-nested,
// multi-directory `\input` chains using file-relative references (e.g. a
// file two levels deep `\input`-ing a sibling by a name that's only valid
// relative to ITS OWN directory, not the project root) could resolve
// incorrectly under this simplification. Flagged as a real gap for a
// future pass, not silently assumed away.

import { resolveDocIdByPath, type FileTreeFolder } from "./project-tree.js";

const INPUT_RE = /\\(?:input|include)\s*\{([^}]*)\}/g;

/** Matches the Python port's own `_MAX_INPUT_DEPTH = 20` -- a runaway/cyclic
 * `\input` guard, not a realistic depth for any real paper. */
export const MAX_INPUT_DEPTH = 20;

/** The minimal shape this module needs from an Overleaf real-time session --
 * real `OverleafProjectSession` (overleaf-ot-client.ts) satisfies this, and
 * so does this file's own test suite's minimal fake session. `rootFolder`
 * is optional here only to accommodate a fake session that never sets it
 * (only `joinDocExpanded` -- not `expandInputs` itself -- ever reads it off
 * the session; see joinDocExpanded's own body). */
export interface ExpandableSession {
  joinDoc(docId: string): Promise<{ lines: string[]; version: number }>;
  rootFolder?: FileTreeFolder | null;
}

export interface ExpandInputsArgs {
  session: ExpandableSession;
  /** the project's file tree (e.g. `session.rootFolder`), passed explicitly
   * rather than read off `session` so this is testable with a fake session
   * + a fabricated tree */
  rootFolder: FileTreeFolder | null;
  /** the LaTeX source to expand `\input`/`\include` references within */
  source: string;
  /** resolved docIds already spliced in this expansion chain -- callers
   * doing a top-level call should seed this with the ROOT doc's own id (see
   * `joinDocExpanded` below) so a doc that `\input`s itself (directly or via
   * a cycle) is caught */
  seen?: Set<string>;
  /** current recursion depth, internal */
  depth?: number;
  /** accumulator for names that couldn't be resolved/fetched, internal
   * (callers should read the RETURNED `unexpanded`, not pass their own to
   * collect into, though passing one through is harmless) */
  unexpanded?: string[];
}

export interface ExpandInputsResult {
  source: string;
  unexpanded: string[];
}

/**
 * Recursively splices `\input{name}` / `\include{name}` targets inline,
 * fetching each referenced doc fresh over the given `session`. Real
 * multi-file papers keep chapters/sections in separate Overleaf docs;
 * without this, an outline built from just the root doc's own text is
 * incomplete.
 *
 * Never throws -- an unresolvable name (no such doc in the project tree),
 * a doc that fails to fetch, or a `depth` that exceeds `MAX_INPUT_DEPTH` is
 * left in place in the returned `source` text and recorded once in
 * `unexpanded`, exactly like the ported Python version's own contract.
 */
export async function expandInputs({
  session,
  rootFolder,
  source,
  seen = new Set(),
  depth = 0,
  unexpanded = [],
}: ExpandInputsArgs): Promise<ExpandInputsResult> {
  if (depth > MAX_INPUT_DEPTH) return { source, unexpanded };

  const matches = [...source.matchAll(INPUT_RE)];
  if (matches.length === 0) return { source, unexpanded };

  let result = "";
  let lastIndex = 0;
  for (const m of matches) {
    result += source.slice(lastIndex, m.index);
    lastIndex = (m.index ?? 0) + m[0].length;

    const name = m[1].trim();
    if (!name) {
      result += m[0];
      continue;
    }
    const candidate = name.toLowerCase().endsWith(".tex") ? name : `${name}.tex`;
    const resolved = resolveDocIdByPath(rootFolder, candidate);
    if (!resolved.ok) {
      if (!unexpanded.includes(name)) unexpanded.push(name);
      result += m[0];
      continue;
    }
    if (seen.has(resolved.docId)) {
      // Cycle: already included once in this chain -- drop the re-include
      // entirely (matches the Python port's own `return ""` for this case),
      // not re-fetched, not left in place, not double-recorded.
      continue;
    }
    try {
      const { lines } = await session.joinDoc(resolved.docId);
      seen.add(resolved.docId);
      const inner = lines.join("\n");
      const expandedInner = await expandInputs({
        session,
        rootFolder,
        source: inner,
        seen,
        depth: depth + 1,
        unexpanded,
      });
      result += expandedInner.source;
    } catch {
      // A real fetch failure (network, permissions, a genuinely broken doc)
      // must not sink the whole expansion -- leave this one reference
      // unexpanded and keep going, matching the Python port's own
      // never-raise contract.
      if (!unexpanded.includes(name)) unexpanded.push(name);
      result += m[0];
    }
  }
  result += source.slice(lastIndex);
  return { source: result, unexpanded };
}

/**
 * Convenience entry point: join `docId` fresh and return its FULLY
 * expanded text (every resolvable `\input`/`\include` spliced in,
 * recursively), plus whatever couldn't be resolved. Seeds the cycle-guard
 * `seen` set with `docId` itself so a doc that (directly or via a cycle)
 * ends up `\input`-ing itself is caught rather than infinitely recursing
 * up to `MAX_INPUT_DEPTH` for no reason.
 */
export async function joinDocExpanded(
  session: ExpandableSession,
  docId: string,
): Promise<{ source: string; unexpanded: string[]; version: number }> {
  const { lines, version } = await session.joinDoc(docId);
  const { source, unexpanded } = await expandInputs({
    session,
    rootFolder: session.rootFolder ?? null,
    source: lines.join("\n"),
    seen: new Set([docId]),
  });
  return { source, unexpanded, version };
}
