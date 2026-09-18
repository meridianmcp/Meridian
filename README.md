# Meridian LaTeX

A structural LaTeX editing engine + Overleaf browser extension — a `.tex`
analogue of Meridian's existing DOCX structural-editing tooling
(`extensions/meridian-docs`). Built toward the Digital Science 2026 Catalyst
Grant ("Agentic Workflows You Can Trust", deadline Oct 5 2026). See
`CATALYST_GRANT_DRAFT.txt` in the `dnabert-error-correction` repo root for the
full pitch; Meridian proposal `29551f1a-d0eb-4df6-9804-48729314c051` (project
`meridian-build`, sprint item `fc5d9911`) tracks this work — see that item's
pinned decisions for the full design rationale behind everything below
(`df3491e3` matching/project-index design, `28ebe1f0`/`c01875cf` the
write-back safety research and live validation, `9ce6420e` is unrelated
Meridian-repo hook work, not this project).

Standalone on purpose: reusable across every paper repo (dnabert-error-correction,
OOXML-Graph, the MS thesis), not owned by any one of them. The disposable
`dnabert_test_dummy` Overleaf project (uploaded from a copy of
`paper/venues/plos_compbiol/{main_plos.tex,refs.bib,plos2025.bst}`, NOT the
live synced original) is what real-content testing has used so far — a
convenient stand-in, not a sign this tool is dnabert-specific.

## Status as of 2026-09-18: initial stages complete

The full loop — read a live Overleaf document's structure, coordinate who's
editing what, write a change back in, verify it landed — is built, committed,
and **live-verified against real paper content**, not just reasoned about or
unit-tested in isolation. See "What's real" below for the precise boundary of
what that covers. This is a natural handoff point: what exists is coherent
and tested, not a work-in-progress snapshot.

## Layout

- **`engine/`** — Node package, zero new runtime dependencies except
  `better-sqlite3` (bundled prebuild, no compile toolchain needed — see
  `store.js`'s own comment if `npm install` tries to compile from source).
  - `src/outline.js` — parses `.tex` via `@unified-latex/unified-latex-util-parse`
    into addressable structural nodes (headings, citations, tables, figures,
    equations), each with a **content-fingerprint id** (not positional —
    survives unrelated upstream edits) and source line range.
  - `src/matching.js` — `matchOutlines(old, new)`: diffs two parses of the
    same document (id-join for the easy case, kind-aware LCS alignment for
    content that changed enough to change its fingerprint) so a node's
    identity survives being retitled or shifted.
  - `src/store.js` / `src/claims.js` — local SQLite project index (auto-
    registered per Overleaf project id, no manual setup) and node/whole-
    document claim-lease conflict rules, mirroring meridian-docs'
    region-claim model conceptually but fully self-contained (no dependency
    on the Meridian server).
  - `src/server.js` — plain Node `http`, no framework. `POST /outline`,
    `POST /claim`, `POST /lease`, `POST /release`, `GET /claims`,
    `GET /extension-version` (for the extension's self-reload poll),
    `GET /health`. See its own header comment for the full contract.
  - `docs/write-back-spec.md` — the original write-back design doc (storage
    schema, conflict rules, HTTP contract). Still accurate for what's
    described; see the pinned Meridian decisions for what changed during
    implementation review (the whole-doc-lease-vs-scoped-claim symmetry fix)
    and live validation (confirmed-safe write path, batched/track-changes as
    the intended real UX instead of per-write confirmation).

- **`extension/`** — Chrome MV3 extension, loadable unpacked. Self-reloading
  as of 2026-09-17 (see "Running it locally" below) — after the one-time
  bootstrap reload, `extension/*.js` changes no longer need a manual
  `chrome://extensions` click.
  - `content_script.js` — isolated-world half of the MAIN-world bridge
    (`relayToMainWorld`), plus the original DOM-based `.cm-content .cm-line`
    text reader.
  - `injected.js` — MAIN-world half. `recoverEditorView()` (the `.cm-content`
    → `cmView.view` → class → `EditorView.findFromDOM` chain — the one piece
    the original design left genuinely uncertain, now **live-confirmed
    working**), `getDocInfo()` (read), `getLineInfo()` (read one line's exact
    offsets), `applyEdits()` (the write-dispatch primitive: batched, offset-
    revalidated against the live document immediately before dispatch,
    overlap-rejecting, readback-verified — never partial, never silent).
  - `popup.js`/`popup.html` — "Get structural outline" (read), "Check CM6
    access" (read-only diagnostic), "Insert test edit" (raw write-dispatch
    diagnostic — do not point at a real manuscript, it's unclaimed and
    unscoped), and the real flow: **claim a node → edit its field → save**.
    Save re-fetches the live outline, re-matches the node by fingerprint id
    (aborts if it changed since claiming), locates the field's exact
    character range against the *current* live line, dispatches, verifies,
    releases the claim, refreshes.

## What's real right now vs. what's next

**Real, live-verified against actual paper content (not just unit-tested):**
- Outline extraction + fingerprint ids + re-matching across edits.
- The claim/lease coordination layer (SQLite-backed, auto-registers any
  Overleaf project by URL — no per-paper setup).
- The write-dispatch primitive itself: insert, multi-edit batches, offset
  validation, overlap rejection, and readback verification all confirmed
  working against a real live Overleaf document (see decision `c01875cf`
  for the full test log — clean insert, clean revert, both malformed-batch
  rejection cases tested and confirmed non-destructive).
- The full claim→edit→write→release flow, for **`heading` titles and
  `citation` keys only** (see below for why those two and not the other
  three kinds). Live-tested against the real dnabert manuscript, including
  the hard case: disambiguating between multiple citations of the *same key*
  repeated on one line — confirmed it edits the correct occurrence and
  leaves siblings untouched.
- One real bug found and fixed via that live testing, not hypothetically:
  the heading matcher originally only recognized `\section{...}`, but PLOS's
  own unnumbered-heading convention is `\section*{...}` — and *every single
  heading in the real manuscript* (25/25) is starred. It failed safely every
  time (the occurrence-count check correctly aborted rather than writing
  anything wrong) but was completely non-functional until fixed. Now
  star-tolerant, live-reconfirmed.
- The extension self-reloads on its own file changes (`chrome.alarms` poll
  against the engine's `/extension-version` hash) — no more manual
  `chrome://extensions` clicks after the one-time bootstrap reload.

**Deliberately not built yet, not silently assumed fine:**
- **`table`/`figure` captions and `equation` labels are not editable.**
  Locating a caption/label safely needs a multi-line scan that risks
  matching a *nested* environment's own caption (e.g. a `tabular` inside a
  `table` float) instead of the intended one — narrower-but-correct (headings
  and citations) was chosen over broader-but-unreliable for this pass.
- **Concurrency and failure-mode testing.** Two tabs editing the same
  document simultaneously, a mid-edit network drop/reconnect, and Overleaf's
  own server-side "out of sync" recovery under a genuinely malformed op that
  reaches the server (today's malformed-edit tests were all rejected
  client-side, before dispatch — they never exercised that path at all) are
  all still open. Decision `c01875cf` has the full list.
- **Per-write confirmation is a validation-phase artifact, not the intended
  design.** The real UX should be a batched multi-edit confirmation (CM6's
  `dispatch` natively takes an array of changes — one confirmation for N
  edits) and/or routing through Overleaf's own native track-changes/Review
  panel (real prior art exists for this exact trick — see decision
  `28ebe1f0`) instead of a custom per-write dialog. Not implemented yet.
- **No batched multi-node edit UI** — one node at a time, even though the
  underlying primitive supports a batch.

## Running it locally

The engine server auto-starts on Windows login (Startup-folder shortcut →
`engine/start_server_hidden.vbs`, hidden window, no visible terminal). To
start it manually:
```
cd engine
node src/server.js
```
Listens on `http://127.0.0.1:8471` (override with `MERIDIAN_LATEX_PORT`).

Load the extension unpacked at `chrome://extensions` (Developer mode → Load
unpacked). **After that one-time load, you should not need to manually
reload it again** — it polls its own file-change fingerprint and reloads
itself (and refreshes any open Overleaf tab) automatically. If you ever see
stale behavior after an edit, the manual fallback is still: reload at
`chrome://extensions`, refresh the Overleaf tab.

Known local-dev gotcha (not a bug in this code): if `chrome://extensions` is
open in another tab while testing, Chrome can hang resolving this
extension's own resources from a different tab for up to its full timeout
window. Close that tab if `chrome-extension://` resource loads start
mysteriously hanging.

## Manual steps only a human can do

- Chrome Web Store Developer Dashboard registration (one-time $5 fee) — only
  needed at actual publish time.
- Anything involving a real (non-disposable) manuscript: per decision
  `c01875cf`, the write path is live-confirmed safe on a disposable test
  copy, but concurrency/network-drop/server-desync behavior is still
  untested — treat a real document as higher-stakes than the test project
  until those gaps close.
