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
  - `src/provenance.js` — a durable LOCAL audit trail (same SQLite store) of
    every edit actually dispatched through `applyEdits`: which project,
    which node, which field, old/new value, when. Distinct from `claims.js`
    (a claim is "who's allowed to write right now"; a provenance row is
    "what was actually written"). Deliberately does NOT call meridian-outputs
    directly — `server.js` is a headless local Node process with no MCP
    client of its own, so it can't. Instead it's a pull-based buffer: a
    `synced_to_meridian_outputs` flag per row lets a LATER agent session (one
    that genuinely has MCP access) pull unsynced rows and push them into
    meridian-outputs itself. See "What's real" below for what's built vs.
    what's still a manual/future step.
  - `src/zotero.js` — citation-key validation against the LOCAL Zotero
    desktop app's HTTP API (127.0.0.1:23119, not the Meridian tunnel).
    `lookupCitationKey()` returns a three-way resolved/not-resolved/
    couldn't-check result via the library's own `:key:` tag convention.
  - `src/server.js` — plain Node `http`, no framework. `POST /outline`,
    `POST /claim`, `POST /lease`, `POST /release`, `GET /claims`,
    `POST /provenance`, `GET /provenance`, `POST /provenance/mark-synced`,
    `GET /zotero-lookup`, `GET /extension-version` (for the extension's
    self-reload poll), `GET /health`. See its own header comment for the
    full contract.
  - `docs/write-back-spec.md` — the original write-back design doc (storage
    schema, conflict rules, HTTP contract). Still accurate for what's
    described; see the pinned Meridian decisions for what changed during
    implementation review (the whole-doc-lease-vs-scoped-claim symmetry fix)
    and live validation (confirmed-safe write path, batched/track-changes as
    the intended real UX instead of per-write confirmation).
  - `src/socketio09/` — original (not forked) encode/decode + transport for
    Overleaf's real-time backend's actual wire protocol: a fork of the
    legacy, pre-spec Socket.IO 0.9.x framing. Written from protocol facts
    read out of Overleaf's own open-source server and cross-checked twice
    against `github.com/overleaf/overleaf`'s real source — not copied from
    any AGPL-licensed client (see `src/overleaf-ot-client.js`'s own header
    for why forking one was rejected). `packet.js` is pure encode/decode;
    `client.js` is the WebSocket transport + heartbeat/ack handling.
  - `src/overleaf-ot-client.js` — `connectToProject`/`joinDoc`/`applyUpdate`
    built on `socketio09/`: a direct, no-browser-tab connection to a live
    Overleaf project's document. `applyUpdate()` deliberately waits for both
    the ack AND the async `otUpdateApplied`/`otUpdateError` broadcast before
    resolving (the ack alone doesn't confirm the write landed — confirmed by
    reading the server's own callback wiring, not assumed).
    `trackChangesOnForUser()` drives `meta.tc` for routing an edit through
    Overleaf's native tracked-changes mode.
  - `src/overleaf-login.js` — the ONLY place in this codebase that ever
    touches a real Overleaf session cookie. Spawns a dedicated, isolated
    Chrome profile via CDP, lets a human log in themselves (2FA/SSO/captcha
    all just work, since it's a real browser window), reads the resulting
    cookie back. Must be run directly by a human (`node src/overleaf-login.js
    login`) — never invoked from inside an agent session.

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
- The full claim→edit→write→release flow, for **`heading` titles,
  `citation` keys, and — new, 2026-09-18 — `table`/`figure` captions and
  any environment-shaped equation's (`equation`, `align`, ...) label.**
  Engine-side fix that made the last three safe to add: `outline.js`'s
  caption/label extraction now stops at a nested structural boundary
  (`findFirstMacroInOwnScope`) instead of recursing through it, so a
  `tabular` float's own caption inside an outer `table` is never
  misattributed to the outer node — the exact ambiguity this section used
  to cite as the reason those three fields were left unbuilt. Each node now
  also carries its field's own exact source line (`captionLine`/
  `labelLine`), so the extension only ever reads and edits the ONE line the
  engine already resolved unambiguously — the same safe single-line pattern
  heading/citation editing already used. Live-tested against the real
  running engine server (a real nested-tabular-inside-table document, both
  captions/labels extracted independently and correctly) and against a
  faithful replica of `popup.js`'s new locate functions using that real
  engine output (correct extraction, correct post-edit text, correct
  concurrent-edit-abort behavior) — see the 2026-09-18 pinned decision for
  the full log and for what remains open (see below).
- **A real, more fundamental bug found live during that same pass, in the
  ALREADY-shipped code, not the new work**: Overleaf's CM6 editor
  virtualizes `.cm-content`'s rendered `.cm-line` children — only lines
  near the current scroll position actually exist in the DOM (confirmed
  live: a real 405-line/69756-char manuscript had only 17 lines rendered).
  `content_script.js`'s original `readEditorText()` (a `.cm-content
  .cm-line` DOM-text-join) silently returned ONLY that virtualized subset
  with no error or truncation signal — meaning `getOutline()`'s `/outline`
  POST (and thus the ENTIRE claim/edit/write/release flow, for every node
  kind including headings/citations) was silently scoped to whatever
  happened to be scrolled into view, not the real document. Fixed by adding
  a new `getFullText()` primitive to `injected.js` that reads CM6's actual
  `state.doc` model (never the DOM, exactly like `applyEdits`/`getLineInfo`
  already do on the write side) and rewiring `popup.js`'s `getEditorText()`
  to use it. Live-confirmed: the new path returns the full 69756/405 real
  document, matching `getDocInfo()`'s own ground truth exactly.
- One real bug found and fixed via live testing (Phase 1), not
  hypothetically: the heading matcher originally only recognized
  `\section{...}`, but PLOS's own unnumbered-heading convention is
  `\section*{...}` — and *every single heading in the real manuscript*
  (25/25) is starred. It failed safely every time (the occurrence-count
  check correctly aborted rather than writing anything wrong) but was
  completely non-functional until fixed. Now star-tolerant,
  live-reconfirmed.
- The extension self-reloads on its own file changes (`chrome.alarms` poll
  against the engine's `/extension-version` hash) — no more manual
  `chrome://extensions` clicks after the one-time bootstrap reload.
- **Batched multi-node/multi-field edit UI (2026-09-18)**: "Queue edit"
  replaces the old immediate-dispatch "Save edit" on every editable field
  — queuing several fields, across one or many claimed nodes, accumulates
  them; one "Apply queued edits" click dispatches every queued edit as a
  SINGLE CM6 transaction (`applyEdits` has accepted an edit array since it
  was written — this closes the `popup.js`-side orchestration gap, not a
  new engine/`injected.js` capability). All-or-nothing: a fresh outline is
  re-fetched ONCE right before dispatch, and if ANY queued entry fails to
  re-match (deleted, or its fingerprint changed since claiming) or fails to
  locate its field on the live line, the WHOLE batch aborts before
  anything is dispatched — named by kind+field so the user knows which
  queued entry to fix or remove, not a vague "batch failed." Verified via
  a logic-replica test (9/9 checks: correct multi-edit resolution +
  document-order sorting, and all three all-or-nothing abort paths — see
  the meridian-build project's pinned decisions for the full log). Closes
  the first half of the README's own former "what's next" item on this
  exact gap.

**Unit-tested (113/113 passing), NOT yet live-verified:**
- **A second, independent way to write into a live Overleaf document: a
  direct Socket.IO 0.9.x + OT-protocol client (`src/socketio09/` +
  `src/overleaf-ot-client.js`), talking straight to Overleaf's real-time
  backend over WebSocket — no browser tab, no extension, no CM6 DOM
  involved at all.** This is a different write path from everything above
  (which all goes through the extension driving a real editor tab); the two
  are independent, not layered. Every unit not requiring a live server
  connection is tested and passing (packet framing, transport/heartbeat/ack
  handling, join/apply-update flow including the ack-vs-`otUpdateApplied`
  distinction, tracked-changes `meta.tc` wiring) using dependency-injected
  fakes — no live network or real credentials touched during development.
  **What's NOT yet confirmed: an actual connection to a real Overleaf
  project.** That requires a human running `node src/overleaf-login.js
  login` themselves (see `src/overleaf-login.js` above — an agent session
  must never do this step) and then exercising `overleaf-ot-client.js`
  against the resulting cookie. Until that happens, treat this path as
  logically sound and thoroughly tested in isolation, not as proven against
  the real service — the same honesty standard this README already applies
  to every other capability above.
- **A durable local audit trail of every applied edit (`src/provenance.js`,
  new `provenance` table in `store.js`'s SQLite schema, `POST /provenance` /
  `GET /provenance` / `POST /provenance/mark-synced` on `server.js`), wired
  into `popup.js`'s `applyBatch()`.** Every queued edit's old value is
  captured at queue time (before the user's typed replacement overwrites the
  input); after a batch is dispatched and readback-verified, one provenance
  row is recorded per edit (project, node, field, old/new value, holder,
  timestamp) — best-effort and non-fatal, so a provenance-write failure can
  never make a genuinely successful Overleaf edit look like it failed, and
  never blocks releasing the claim. 10 new unit tests (record/list/mark-
  synced logic, including a real FK-constraint interaction found and fixed
  during implementation — see below). **Closes the design question the
  README used to flag as unresolved** ("engine has no MCP access") by NOT
  trying to make the engine call meridian-outputs directly: it can't (no MCP
  client in a headless local Node process), so instead it buffers durably and
  exposes `synced_to_meridian_outputs`/`GET /provenance?unsynced_only=true`
  for a LATER agent session to pull and push through, then mark synced.
  **Correction (2026-09-18, after actually reading the real meridian-outputs
  tool catalog rather than assuming the item's own guess was right):**
  `annotate_outputs`/`search_outputs`/`register_output_paths` are scoped to
  a FILESYSTEM `outputs_dir` tree of research artifacts (CSV/JSON/NPY
  experiment outputs, annotated and BM25-searched by path) — there is no
  natural filesystem path for "a citation key was edited in a live Overleaf
  document" to attach to, and forcing one in just to satisfy a tool
  signature would produce misleading data. **The local ledger itself already
  satisfies this item's actual stated goal** ("a durable, queryable audit
  trail... distinct from Overleaf's own version history") on its own,
  without needing to push anywhere. If Meridian-side visibility is ever
  wanted later, `log_task`/a project note (generic, project-scoped, no
  filesystem-path assumption) is the right target, not meridian-outputs —
  but there is no real provenance data yet to sync regardless (confirmed:
  zero rows in the live local DB, since dispatching a real edit needs the
  same popup click-through this README already can't complete in this
  environment — see below). The `popup.js` wiring itself also hasn't been
  exercised through an actual live click-through (same gap this README
  already notes for the caption/label editing UI below — `applyBatch()`'s
  own logic was reasoned through and syntax-checked, not driven end-to-end
  in a real popup).
  A real bug caught while building this: SQLite's `REFERENCES` clause in
  `store.js`'s schema turned out to be genuinely ENFORCED (better-sqlite3
  defaults `PRAGMA foreign_keys=ON` — confirmed live, `db.pragma("foreign_keys",
  {simple:true}) === 1`), not merely documentation-only as an earlier draft
  of this same change assumed; `recordEdit()` calls `ensureProjectRow()`
  first (mirroring `claims.js`'s `insertClaimRow`) so provenance can still be
  recorded for a project row that doesn't exist yet, rather than throwing.

**Multi-project robustness (2026-09-18) — real, live-verified:**
- **A genuinely second, structurally different disposable test Overleaf
  project now exists**: `ooxml-graph-paper-overleaf`
  (`overleaf.com/project/6aadcce27d9be38465624f22`), uploaded from a real
  paper's actual `main.tex`/`refs.bib`/figures — unstarred `\section{}`
  headings (vs. dnabert's starred PLOS convention), `natbib`'s
  `\citep`/`\citet` (vs. dnabert's bare `\cite`), 4 tables, 5 figures, zero
  equations. Compiles cleanly. This is exactly the "catch what's overfit to
  one document's shape" test the README has called for since Phase 1 — and
  it worked immediately:
- **Real bug found and fixed**: natbib's whole citation family
  (`\citep`/`\citet`/`\citealp`/`\citealt`/`\citeauthor`/`\citeyear`/
  `\citeyearpar`, plus capitalized sentence-start variants) was completely
  invisible to the outline — 0 citation nodes extracted from a real document
  with 15+ real citations, silently, no error. Root cause went one level
  deeper than a simple macro-name check: `unified-latex`'s own bundled
  macro-info database doesn't know natbib at all, so without an explicit
  signature registration, `\citep{key}`'s `{key}` group doesn't even attach
  to the macro node as an arg. Fixed with a custom parser registering the
  whole family (xparse signature `"s o o m"` — the leading `s` for the
  starred forms needed its own real investigation: TeX always lexes
  `\citep*` as macro name "citep" plus a separate "*" token, and an earlier,
  wrong attempt without the `s` spec let that stray "*" silently satisfy the
  mandatory key-list argument on its own). 8 new tests. Bare `\cite`
  unaffected (verified, not assumed).
- **Real UX bug found and fixed**: a nested `\tabular` inside every real
  `\table` (100% of occurrences in BOTH real test papers) produces a second,
  caption-less outline node that `popup.js` offered a dead-end "Claim to
  edit" button for. The outline data itself is unchanged on purpose (an
  existing, deliberate prior decision keeps that node addressable for a
  future cell/column-edit feature — see `outline.test.js`'s own "documented
  behavior change" test); fixed at the actual point of confusion instead —
  `popup.js` now shows "(no editable fields yet)" instead of a working-
  looking button that does nothing once clicked.
- **Citation-key validation against the local Zotero library** (`src/zotero.js`,
  `GET /zotero-lookup`, a non-blocking status indicator in `popup.js`).
  Confirmed live before building anything: the Meridian-hosted `zotero-mcp`
  tunnel slot is disabled, Better BibTeX is not installed, but a real,
  already-in-use manual tag convention exists in the actual Zotero library
  (`<project-prefix>:key:<citekey>`, e.g. `P1:key:margulies2005454`).
  `lookupCitationKey()` is a three-way result — resolved / not-resolved /
  couldn't-check-at-all (Zotero not running) — deliberately not a boolean,
  since those are different situations a user shouldn't have collapsed into
  one. 14 unit tests; live-verified against the real local Zotero API: a
  tagged real key resolves true with the item's title, a real-but-untagged
  key (from the new ooxml-graph-paper project, which hasn't had the tagging
  convention applied to it yet) correctly resolves false, not a false
  positive or a crash.
- A stray literal NUL byte was found in `outline.js`'s `fingerprint()`
  function (pre-existing, not introduced this session) — makes git treat
  the whole file as binary. NOT fixed here on purpose: naively replacing it
  would change every fingerprint's hash input, silently invalidating every
  currently-persisted node id across both live test projects. Flagged as
  its own follow-up task requiring a real migration plan, not a one-line
  edit.

**Deliberately not built yet, not silently assumed fine:**
- **A bare inline/display-math `equation` node (CM6 "mathenv", no
  environment) still has no editable label** — it has no `label` field in
  the outline data at all, and INSERTING new `\label{...}` syntax is a
  fundamentally different (and riskier) operation than replacing an
  existing field's text, out of scope for this pass same as before.
- **Concurrency and failure-mode testing.** Two tabs editing the same
  document simultaneously, a mid-edit network drop/reconnect, and Overleaf's
  own server-side "out of sync" recovery under a genuinely malformed op that
  reaches the server (today's malformed-edit tests were all rejected
  client-side, before dispatch — they never exercised that path at all) are
  all still open. Decision `c01875cf` has the full list. **Now blocked
  specifically on a human step, not on more engine work**: the new OT client
  (`overleaf-ot-client.js`) is the actual right tool to exercise this (two
  independent OT-client connections, no browser tab needed at all, could
  simulate real concurrent writers), but every path to it needs a real
  session cookie, which only `node src/overleaf-login.js login` (human-only,
  see above) can produce.
- **Routing through Overleaf's own native track-changes/Review panel** —
  superseded, not abandoned: the OT client's `meta.tc`/`trackChangesOnForUser`
  (built this session) is the actual, more direct mechanism this item was
  really asking for (we construct the OT update ourselves now, so `meta.tc`
  is set directly rather than inferred/observed via the old extension-based
  approach) — see pinned decision `3ee2b454`. What remains is exactly the
  same live-connection verification blocker as the OT client generally.
- **The caption/label editing (and every other UI added since) was NOT
  verified through an actual click-through of the popup UI in a live
  browser.** Re-confirmed independently TWICE now that this is a genuine
  environment constraint, not something more code can fix: browser
  automation can reach a real Overleaf tab and drive injected.js directly
  (proving the outline/getFullText fixes genuinely live) via two DIFFERENT
  tools tried this session (an in-app browser pane, and Claude-in-Chrome
  driving a real, already-logged-in Chrome) — but BOTH tools mangle
  `chrome://extensions`/`chrome-extension://` URLs identically (silently
  prepending `https://`, producing an invalid URL Chrome simply ignores),
  so neither can load or drive `popup.html` itself. Treat a real
  click-through by a human, or a future session with a different automation
  approach entirely, as the only way to close this specific gap.

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

**2026-09-18: the extension's id is now pinned, not random.** `manifest.json`
declares a `"key"` (a public key -- safe to commit, not the private signing
key, which this project never generates or needs) so Chrome assigns the SAME
id every time the extension is loaded unpacked, instead of a fresh random one
per load. This closes a real gap `server.js`'s CORS check had (it used to
accept any `chrome-extension://` origin, not just this one). **If you already
had the extension loaded from before this change, remove it and re-load it
unpacked** so Chrome picks up the new fixed id — otherwise the popup's calls
to the local engine will get silently blocked by CORS (no error dialog, just
requests failing).

## Manual steps only a human can do

- Chrome Web Store Developer Dashboard registration (one-time $5 fee) — only
  needed at actual publish time.
- Anything involving a real (non-disposable) manuscript: per decision
  `c01875cf`, the write path is live-confirmed safe on a disposable test
  copy, but concurrency/network-drop/server-desync behavior is still
  untested — treat a real document as higher-stakes than the test project
  until those gaps close.
- **Running `node src/overleaf-login.js login`** to capture a real Overleaf
  session cookie, and then confirming `overleaf-ot-client.js` actually
  connects and joins a real document with it. Hard rule, not a convenience
  choice: an agent session must never capture, handle, or transmit a real
  account credential or session cookie itself — this step only ever runs
  interactively, driven by a human, in a real visible browser window.
