# meridian-latex

A structural LaTeX editing engine for Overleaf: parses `.tex` into
addressable structural nodes (headings, citations, tables, figures,
equations) instead of treating a document as raw text, coordinates who's
editing what, and writes changes back in — safely, with readback
verification, never partial or silent.

This is the `engine/` half of the meridian-latex project (the other half is
a companion Chrome extension that drives a live Overleaf tab). This package
works standalone as a CLI/library even without the extension — see "What
each part is for" below.

## Install

```bash
npx @meridianmcp/latex outline paper.tex
```

or install it globally (this also gives you the shorter `meridian-latex`
command directly, matching the CLI reference below) / as a project
dependency:

```bash
npm install -g @meridianmcp/latex
```

## CLI

```
meridian-latex outline <path.tex>   parse a .tex file, print its structural outline as JSON
meridian-latex lint <path.tex>      run the static-AST lint suite against a .tex file,
                                     print its findings as JSON (see src/lint.ts)
meridian-latex serve                start the local engine server (http://127.0.0.1:8471) --
                                     this is what the Chrome extension's popup talks to
meridian-latex login                open a dedicated browser window to capture your Overleaf
                                     session (human-only -- never run this from an agent/CI)
meridian-latex status               show whether a saved Overleaf session exists
meridian-latex logout               remove the saved Overleaf session
meridian-latex mcp                  start the MCP server on stdio, for an AI agent
                                     session (Claude Code, etc.) -- see "MCP Server" below
meridian-latex style-guide [type]   print the rhetorical move-sequence reference (see
                                     src/style-guide.ts), full or filtered to one section-type
meridian-latex style-check <path.tex> <type>
                                     heuristically compare a .tex file's section text against
                                     style-guide's expected moves -- structural heuristic, not
                                     ground truth
meridian-latex style-lookup <type> <topic>
                                     STRETCH, off by default (MERIDIAN_LATEX_ENABLE_PUBLISHED_FRAMING=1) --
                                     live, attributed excerpt fetch from a published paper
```

## Library

```js
import { outlineText, matchOutlines, connectToProject } from "@meridianmcp/latex";

const nodes = outlineText(texSource);
// [{ id: "heading:abc123", kind: "heading", level: "section", title: "Introduction", line: 12 }, ...]
```

See `src/index.ts` for the full exported surface: outline extraction
(`outline.ts`), re-matching across edits (`matching.ts`), the claim/lease
coordination layer (`claims.ts`/`store.ts`), a local provenance ledger
(`provenance.ts`), Zotero citation-key validation (`zotero.ts`), and an
original Socket.IO 0.9.x + OT-protocol client for direct (no-browser-tab)
Overleaf writes (`overleaf-ot-client.ts` + `overleaf-login.ts`). The
published package also ships full TypeScript declarations
(`dist/index.d.ts`, via the `types` field) for editor/type-checker support
in consuming projects.

## MCP Server

`meridian-latex` also runs as an MCP (Model Context Protocol) server over
stdio, exposing this engine's outline/claim/provenance/citation/snapshot
capabilities directly as tools an AI agent session can call. Connect an MCP
client (Claude Code, Claude Desktop, etc.) with a config block like:

```json
{
  "mcpServers": {
    "meridian-latex": {
      "command": "npx",
      "args": ["-y", "@meridianmcp/latex", "mcp"]
    }
  }
}
```

Or, running from a local checkout of this repo instead of the published npm
package (run `npm run build` in `engine/` first — the source is TypeScript
in `src/`, and this launches the compiled `dist/mcp-server.js`, not the
source file directly):

```json
{
  "mcpServers": {
    "meridian-latex": {
      "command": "node",
      "args": ["engine/dist/mcp-server.js"]
    }
  }
}
```

(Both forms start the exact same server — `mcp` just dynamically imports and
runs the compiled `mcp-server.js`'s own stdio entry point, so which one to
use is purely a question of whether you have this package installed/
published or are working from source.)

**Safety scoping (read this before assuming a tool does more than it does):**
this server deliberately has **no tool that can perform a live write into a
real Overleaf document** — nothing here can reach `applyFieldEdit` (the
direct WebSocket/OT write path) or construct an `OverleafProjectSession`.
An MCP tool call is exactly the kind of automated invocation an agent session
can make unattended, with no human clicking anything, so a live-write
capability is deliberately kept out of this surface — see `mcp-server.ts`'s
own "CRITICAL SAFETY SCOPING" header comment for the full rationale. The one
way to actually write into a live Overleaf document remains the CLI's own
`write` command (a human typing a command in a terminal) or the Chrome
extension's claim→edit→save flow.

**Current tool list (23):**

| Tool | What it does |
|------|---------------|
| `outline_tex` | Parse raw `.tex` source text into its structural outline. |
| `outline_tex_file` | Parse a local `.tex` file on disk into its structural outline. |
| `claim_node` | Claim a single addressable node for exclusive editing (local claims store only). |
| `lease_document` | Take a whole-document lease. |
| `release_claim` | Release a holder's claim(s). |
| `get_live_claims` | List every currently-live claim on a project. |
| `record_provenance` | Record one applied edit in the local provenance ledger. |
| `list_provenance` | List provenance rows for a project. |
| `mark_provenance_synced` | Mark provenance rows as synced (after pushing them elsewhere yourself). |
| `lookup_citation_key` | Validate a citation key against the local Zotero library's `:key:` tag convention. |
| `list_project_docs` | **Read-only, live Overleaf.** List a live project's file tree (doc paths + ids). |
| `pull_doc_expanded` | **Read-only, live Overleaf.** Fetch one doc's current, fully-expanded text. |
| `get_bibliography` | Parse bibliography entries out of `.tex`/`.bib` source text (pure, local). |
| `expand_section_aliases` | Rewrite aliased sectioning macros to their underlying `\section`/etc. (pure, local). |
| `list_local_snapshots` | List existing local pre-write snapshot files for a project/doc. |
| `overleaf_login_status` | Check whether a saved Overleaf session exists (never returns the cookie itself). |
| `list_citation_keys` | List every tag string in the local Zotero library. |
| `snapshot_document` | Save a full-text local snapshot of a document under `~/.meridian-latex/snapshots`. |
| `lint_tex` | Run the static-AST lint suite (11 checks — see `src/lint.ts`) against raw `.tex` source text. |
| `lint_tex_file` | Run the same lint suite against a local `.tex` file on disk. |
| `get_style_guide` | Pure data lookup of the 8-category rhetorical move-sequence reference (`src/style-guide.ts`). |
| `check_section_style` | Heuristically compare a section's text against its declared type's expected move sequence. **Structural heuristic, not ground truth** — see below. |
| `lookup_published_framing` | **STRETCH, off by default.** Live, attributed excerpt fetch from a real published paper — see below. |

`list_project_docs`/`pull_doc_expanded` are the only two tools that connect
to a live Overleaf project, and both are read-only — see `mcp-server.ts`'s
own header comment for exactly why that's safe.

**Style-guide tier note:** `get_style_guide` and `check_section_style` are
pure, local, no-network tools — same safe tier as everything else above.
`check_section_style`'s result is a **structural heuristic based on
keyword/cue matching**, not a ground-truth or compiler-verified check of a
section's actual rhetorical content — its own `disclaimer` field repeats
this. `lookup_published_framing` is deliberately different and separate: an
opt-in, **off-by-default** tool that performs a live fetch of a short,
attributed excerpt from a real published paper via a research/paper-search
capability the calling session must wire in (never a hardcoded provider,
never cached or bundled into this npm package). It's gated behind the
`MERIDIAN_LATEX_ENABLE_PUBLISHED_FRAMING` environment variable, set by
whoever configures this server's own startup environment — not a per-call
tool argument — and fails closed with a clear error if no research-tool
dependency is available, rather than a silent empty result. See
`src/style-guide.ts`'s own header comment for the full rationale.

## What each part is for

- **CLI `outline`** and the library exports work with zero setup — just a
  `.tex` file on disk. No Overleaf account, no browser, no server needed.
- **`serve` + the Chrome extension** is the interactive, human-in-the-loop
  editing flow: open a live Overleaf tab, read its real structure, claim a
  node, edit it, write it back — verified against the live document at
  every step.
- **`overleaf-ot-client.ts`** is a separate, independent write path: a
  direct WebSocket connection to Overleaf's real-time backend, no browser
  tab required at all. Needs a real session cookie, captured only via the
  human-only `login` command above — an agent/automated process must never
  handle that cookie itself.

## Development

Source lives in `src/` as TypeScript; the published package ships only the
compiled output in `dist/` (plain JS + `.d.ts` declarations) — `src/` is
never published (see `files` in `package.json`). Building from a checkout:

```bash
npm install
npm run build       # esbuild: per-file transpile of src/**/*.ts -> dist/**/*.js
                     # (bundle:false, platform:node, format:esm; also runs
                     # tsc --emitDeclarationOnly for dist/**/*.d.ts)
npm run typecheck   # tsc --noEmit -- the strict type-check gate (0 errors,
                     # no @ts-nocheck)
npm test            # pretest rebuilds dist/, then runs
                     # `node --test dist/**/*.test.js`
```

Compiled test files (`dist/**/*.test.js` and their declarations/sourcemaps)
are excluded from the published tarball via `.npmignore`, even though they
live in the same `dist/` tree as everything else — see `build.mjs`'s header
comment for why tests compile alongside real source instead of being built
separately.

## License

Meridian Source License 1.0 (MSL-1.0) — free for local/internal use of any
kind and any team size (including commercial use within your own
organization); a separate commercial license is needed to host this as a
service for third parties. Converts automatically to plain MIT five years
after each version's release. See `LICENSE` for the full text.

## Full project docs

This README is a focused excerpt for CLI/library users of the published
npm package. The complete design rationale, live-verification log, and
Chrome extension setup instructions live in the main project README, one
directory up from `engine/` in the source repository.
