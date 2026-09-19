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
meridian-latex serve                start the local engine server (http://127.0.0.1:8471) --
                                     this is what the Chrome extension's popup talks to
meridian-latex mcp                  start the MCP server (stdio) -- this is what an AI agent
                                     session (Claude Code, etc.) connects to
meridian-latex login                open a dedicated browser window to capture your Overleaf
                                     session (human-only -- never run this from an agent/CI)
meridian-latex status               show whether a saved Overleaf session exists
meridian-latex logout               remove the saved Overleaf session
```

## Library

```js
import { outlineText, matchOutlines, connectToProject } from "@meridianmcp/latex";

const nodes = outlineText(texSource);
// [{ id: "heading:abc123", kind: "heading", level: "section", title: "Introduction", line: 12 }, ...]
```

See `src/index.js` for the full exported surface: outline extraction
(`outline.js`), re-matching across edits (`matching.js`), the claim/lease
coordination layer (`claims.js`/`store.js`), a local provenance ledger
(`provenance.js`), Zotero citation-key validation (`zotero.js`), and an
original Socket.IO 0.9.x + OT-protocol client for direct (no-browser-tab)
Overleaf writes (`overleaf-ot-client.js` + `overleaf-login.js`).

## MCP (Model Context Protocol) server

For an AI agent session (Claude Code, etc.) to call this engine's outline and
write-back-coordination capabilities directly as tools, instead of shelling
out to the CLI or an agent driving the HTTP server itself. Connect with:

```json
{
  "mcpServers": {
    "meridian-latex": {
      "command": "node",
      "args": ["/absolute/path/to/extensions/meridian-latex/engine/src/mcp-server.js"]
    }
  }
}
```

(or, once installed from npm, `"command": "npx", "args": ["-y", "@meridianmcp/latex", "mcp"]` --
same server, started via the `mcp` CLI subcommand above instead of the file directly.)

Tools exposed:

| Tool | What it does |
|------|--------------|
| `outline_tex` | Parse raw `.tex` source text into its structural outline; with `project_id`, also diffs against and updates that project's stored outline (matched/added/removed). |
| `outline_tex_file` | Parse a **local `.tex` file on disk** into its structural outline -- no server or browser tab involved at all. |
| `claim_node` | Claim a single addressable node (heading, table, figure, equation, citation, ...) for exclusive editing. |
| `lease_document` | Take a whole-document lease (mutually exclusive with any other holder's live claim). |
| `release_claim` | Release a holder's claim(s) -- one node, or every live claim that holder has on a project. |
| `get_live_claims` | List every currently-live claim (scoped or whole-document) on a project. |
| `record_provenance` | Record one applied edit in the local, durable provenance ledger. |
| `list_provenance` | List a project's provenance rows, optionally restricted to ones not yet synced into meridian-outputs. |
| `mark_provenance_synced` | Mark provenance rows as synced, after actually pushing them into meridian-outputs yourself. |
| `lookup_citation_key` | Validate a citation key against the local Zotero library's `:key:` tag convention. |

**Safety scoping (deliberate, not a gap to fill in):** these tools are
read/coordination-only. There is **no MCP tool that can write into a live
Overleaf document.** `overleaf-ot-client.js`'s direct WebSocket write path
(`connectToProject`/`OverleafProjectSession`) is intentionally not exposed
here -- see `src/mcp-server.js`'s own header comment for the full reasoning.
A live write requires either a live browser tab running the Chrome
extension's own CM6-dispatch path (unreachable from a pure Node MCP server),
or a saved human Overleaf login cookie -- and this project's hard rule is
that an automated/agent process must never handle that cookie or perform a
live write on a human's behalf without an explicit, separate confirmation
gate. That stays Chrome-extension-only and human-driven; the MCP server only
parses text/files and reads/writes the same local claim/provenance SQLite
state the Chrome extension's own HTTP server (`serve`) already uses -- both
processes share one fixed on-disk database file, so an agent session and a
human's browser tab stay coordinated through the same claims/provenance data.

## What each part is for

- **CLI `outline`** and the library exports work with zero setup — just a
  `.tex` file on disk. No Overleaf account, no browser, no server needed.
- **`mcp`** is how an AI agent session calls the same outline/claim/
  provenance/citation-lookup logic directly as tools, in-process — no HTTP
  hop to `serve`, and (deliberately) no path to a live Overleaf write.
- **`serve` + the Chrome extension** is the interactive, human-in-the-loop
  editing flow: open a live Overleaf tab, read its real structure, claim a
  node, edit it, write it back — verified against the live document at
  every step.
- **`overleaf-ot-client.js`** is a separate, independent write path: a
  direct WebSocket connection to Overleaf's real-time backend, no browser
  tab required at all. Needs a real session cookie, captured only via the
  human-only `login` command above — an agent/automated process must never
  handle that cookie itself.

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
