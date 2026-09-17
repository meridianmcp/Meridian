# Meridian LaTeX

A structural LaTeX editing engine + Overleaf browser extension — a `.tex`
analogue of Meridian's existing DOCX structural-editing tooling
(`extensions/meridian-docs`). Built toward the Digital Science 2026 Catalyst
Grant ("Agentic Workflows You Can Trust", deadline Oct 5 2026). See
`CATALYST_GRANT_DRAFT.txt` in the `dnabert-error-correction` repo root for the
full pitch; Meridian proposal `29551f1a-d0eb-4df6-9804-48729314c051` (project
`meridian-build`) tracks this work.

Standalone on purpose: reusable across every paper repo (dnabert-error-correction,
OOXML-Graph, the MS thesis), not owned by any one of them.

## Layout

- **`engine/`** — Node package that parses a `.tex` file into a real AST
  (via `@unified-latex/unified-latex-util-parse`, the same parser LaTeX
  Workshop has wired in but never uses to write structure back — see the
  grant draft's own competitive audit) and extracts addressable structural
  nodes: headings, citations, tables, figures, equations, each with a stable
  id and source line range. `node src/cli.js outline <path.tex>` is the
  first real, working operation — most recently re-verified against the
  actual, live `main_plos.tex` (2026-09-15) at 102 nodes: 25 headings, 50
  citations, 13 figures, 12 tables, 2 equations, real captions/labels/line
  numbers, ~1s. (An earlier pass reported 69 nodes on 2026-09-14; both the
  manuscript's own content and this engine's extraction logic have changed
  since then — see `npm test` and the notes below, not the raw counts, for
  what actually changed and why.)

  Fixed since the first pass (all covered by `src/outline.test.js`, run via
  `npm test`):
  - **Multi-key citations now split.** `\cite{a,b,c}` used to produce one
    citation node with `key: "a,b,c"`; it now produces one node per key.
  - **`\ref{}` inside a caption now resolves** to the referenced
    table/figure/equation's sequential number (a two-pass walk: all labels
    are numbered before any caption is rendered, so a forward reference —
    "see Table~\ref{tab:later}" — still resolves). An unresolved ref renders
    as a marked `[?key]` placeholder, never silently drops.
  - **Citations nested inside a table/figure/equation environment are found.**
    The v0 pass returned immediately after recording a structural
    environment's own caption/label, so any `\cite{}` inside e.g. a table
    footnote was silently skipped entirely; it's walked now.
  - **Known, documented behavior change from the above:** a `tabular`
    nested inside a `table` float (the common `\begin{table}...
    \begin{tabular}...\end{tabular}...\end{table}` shape) is now its own
    addressable node distinct from the outer float — this is why the table
    count roughly doubled. This is intentional: a future cell/column-edit
    operation needs to address the `tabular` layout specifically, separate
    from the float's caption/label.
  - **Still a v0 approximation, not hidden:** \ref{} numbering is a
    sequential-per-kind count in document order (matching plain
    `\thetable`/`\thefigure`/`\theequation` auto-numbering for the common
    case) — it does not model manual `\setcounter`, subfigures, or
    per-section numbering restarts, and only labels living directly inside
    a table/figure/equation environment are numbered at all.

- **`extension/`** — Chrome MV3 extension, loadable unpacked
  (`chrome://extensions` → Developer mode → Load unpacked → select this
  folder). Content script reads Overleaf's live CodeMirror 6 editor DOM
  (`.cm-content .cm-line`). **Verified against a real, logged-in Overleaf
  session (2026-09-17)** — the `.cm-content .cm-line` selector guess was
  correct on the first try; the popup's "Re-check" button reported a real
  line/char count against a live project.

  The popup's **"Get structural outline"** button now does the real
  end-to-end loop: reads the live editor text via the content script, POSTs
  it to the local engine server (`engine/src/server.js`, see below), and
  shows a real per-kind node count back in the popup. This is the first
  actual read-path proof that extension → engine → structured output works
  against live Overleaf content, not just a static file on disk.

## What's real right now vs. what's next

Real: the parser, the outline extraction, running against the actual
manuscript. Real: the extension reads Overleaf's live editor DOM — verified
against a real, logged-in session. Real (2026-09-17): the full read-path
loop — extension → local engine server (`engine/src/server.js`, plain
Node `http`, zero new dependencies) → structured outline → back in the
popup — proven end-to-end against live Overleaf content, not just a file on
disk.

Not yet built: the write-back half (claim a node, edit it, verify the hash
— mirroring meridian-docs' region claim + lease model). The engine server
only has one endpoint (`POST /outline`, read-only) — no write/edit
operations exist yet at any layer (engine, server, or extension).

## Running the local engine server

The popup's "Get structural outline" button needs `engine/src/server.js`
running locally first:
```
cd engine
node src/server.js
```
Listens on `http://127.0.0.1:8471` by default (override with
`MERIDIAN_LATEX_PORT`). The extension's `manifest.json` already declares
`host_permissions` for that origin. If the popup shows a fetch error, this
is almost always "the server isn't running" — start it and click
"Get structural outline" again (no need to reload the extension itself for
that, only for content_script.js/background.js/manifest.json changes).

## Manual steps only you can do

- Load the extension unpacked in your own Chrome (done, 2026-09-17 — ID
  `nccfjgpohlompmimgpiemjnpelehiilg` on this machine) and reload it at
  `chrome://extensions` whenever `content_script.js`/`background.js`/
  `manifest.json` change (a content-script change also needs the target
  Overleaf tab refreshed to re-inject — the extension doesn't retroactively
  patch an already-open tab). `popup.html`/`popup.js` need no reload —
  popups rebuild fresh from disk every time they're opened.
- Start `engine/src/server.js` locally before using "Get structural
  outline" (see above) — it does not run automatically.
- Chrome Web Store Developer Dashboard registration (one-time $5 fee, Google
  account) — only needed at actual publish time, not for local dev/testing.
- Nothing else needs a sign-in yet: the engine only touches local `.tex`
  files / live-read Overleaf DOM text, and `unified-latex` is a public,
  no-auth npm package.
