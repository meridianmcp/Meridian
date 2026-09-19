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

## What each part is for

- **CLI `outline`** and the library exports work with zero setup — just a
  `.tex` file on disk. No Overleaf account, no browser, no server needed.
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
