# Write-back spec: project index, claims, leases

Status: design, not yet implemented. Covers sprint items `157ff977` (project
index) and `0b0ee5b3` (lease store + write-UI) under Meridian project
meridian-build / `fc5d9911`. Builds directly on the fingerprint-id +
`matchOutlines` work already landed in `engine/src/outline.js` /
`engine/src/matching.js`.

**Explicit constraint (author instruction, 2026-09-17): this must be fully
self-contained.** No dependency on Meridian's own in-house document-modifying
machinery (`meridian-docs`, `meridian/db/locks.py`, the Meridian Postgres/
SQLite server) — meridian-latex is a standalone repo used across multiple
unrelated paper projects (dnabert-error-correction, OOXML-Graph, ms-thesis),
none of which should need the Meridian server running, or even installed, for
this to work. Everything below lives inside `engine/` as its own local
storage. The *conflict-rule design* mirrors `locks.py` conceptually (it's a
proven, already-debugged model) — the *implementation* does not touch it or
import from it.

## 1. Storage

One local SQLite file, `engine/data/meridian-latex.db` (created on first run,
gitignored), via `better-sqlite3` (the one new dependency this needs — no
existing dependency covers embedded SQL). Two tables:

```sql
CREATE TABLE projects (
  project_id   TEXT PRIMARY KEY,   -- the Overleaf project id from the tab URL
                                    -- (overleaf.com/project/<this>), auto-registered
                                    -- on first outline request from that project --
                                    -- NOT a manual add/remove step anywhere.
  last_seen_at TEXT NOT NULL,
  last_outline TEXT NOT NULL       -- JSON: the last outline array seen for this
                                    -- project, used as the "old" side of
                                    -- matchOutlines on the NEXT request.
);

CREATE TABLE claims (
  id           TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL REFERENCES projects(project_id),
  node_id      TEXT NOT NULL,      -- a fingerprint id from outline.js, OR the
                                    -- reserved sentinel below for a whole-doc lease
  holder_token TEXT NOT NULL,      -- opaque per-popup-session token (see  3),
                                    -- NOT a Meridian session_id
  claimed_at   TEXT NOT NULL,
  released_at  TEXT                -- NULL while live; soft-release, never delete
                                    -- (same choice locks.py makes, for the same
                                    -- reason: keep history inspectable)
);
CREATE INDEX idx_claims_project ON claims (project_id, node_id);
```

`__meridian_latex_whole_document_lease__` is the reserved `node_id` sentinel
for a whole-document lease — same pattern as locks.py's
`DOCX_WHOLE_DOCUMENT_ELEMENT`, chosen for the same reason (reuse one table
instead of a second schema for what is conflict-rule-wise a special case of
the same thing).

Liveness: a claim is "live" if `released_at IS NULL AND claimed_at > now() -
CLAIM_TTL_MINUTES`. Start with `CLAIM_TTL_MINUTES = 30` (a popup session is
short-lived; this just needs to survive someone reading a node before they
edit it, not a multi-hour absence). No cross-process concern here worth the
complexity locks.py has for Meridian's multi-machine Fly.io deployment — this
is one local Node process on one machine.

## 2. Conflict rules (mirrors locks.py's Model B conceptually)

1. A live whole-document lease by ANY holder blocks every other holder's new
   claim (scoped or whole-doc) on that project.
2. A live scoped claim on `node_id` X by holder A blocks holder B's claim on
   the SAME `node_id` X.
3. Scoped claims on DIFFERENT `node_id`s coexist freely (the actual point of
   node-level granularity, same as locks.py's).
4. Re-claiming your own already-held claim is idempotent (refresh
   `claimed_at`).
5. **New caution not present in locks.py, specific to this domain:** if
   `node_id` is a citation-kind id ending in `-dup\d+` or containing `:pos:`
   (position-fallback), the claim is still granted, but the response carries
   `"identity_confidence": "low"` with a one-line reason (repeated-citation
   ambiguity or no-stable-content fallback — see the matching-layer caveat
   above). The caller (popup UI) should show this to the user before they
   commit an edit — surfaced, not silently trusted. This isn't a rule that
   BLOCKS anything; it's honesty about what the id can and can't guarantee,
   matching this repo's existing practice of documenting v0/v1 approximations
   rather than hiding them.

## 3. HTTP endpoints (added to `engine/src/server.js`, same plain-`http`,
no-framework style as the existing `/outline`)

All request/response bodies JSON. `project_id` and a `holder_token` (any
client-generated opaque string — the popup can `crypto.randomUUID()` one per
browser session and keep it in `chrome.storage.session`) are required on
every write endpoint.

- `POST /outline` (existing, extend only): body gains optional `project_id`.
  When present: look up `projects.last_outline` for that id, run
  `matchOutlines(lastOutline, newOutline)`, upsert `projects` with the new
  outline, and return `{nodes, matched, added, removed}` instead of just
  `{nodes}` — backward compatible (old callers without `project_id` get the
  old `{nodes}` shape unchanged, since there's nothing to diff against).
- `POST /claim` — body `{project_id, node_id, holder_token}`. Applies rules
  1–5 above. Returns `{claimed: true, identity_confidence?}` or `{claimed:
  false, reason, holder_token_of_conflict?}` (never throws — same
  never-raises convention as `claim_docx_region`).
- `POST /lease` — body `{project_id, holder_token}`. Whole-document
  equivalent of `/claim`, rule 1's blocker. `{leased: true}` /
  `{leased: false, reason}`.
- `POST /release` — body `{project_id, holder_token, node_id?}`. Omit
  `node_id` to release every claim this `holder_token` holds on the project
  (mirrors `release_docx_region_claims`'s no-args-means-everything shape).
  Returns `{released: <count>}`.
- `GET /claims?project_id=...` — read-only, live claims for a project (for
  the popup to show "someone/something else has X claimed" — though in
  practice v1 has one browser tab per person, so this mostly matters if two
  popup instances are open at once).

## 4. Popup UI flow (`extension/popup.js`, `popup.html`)

Extends the existing "Get structural outline" flow rather than replacing it:

1. User clicks "Get structural outline" (existing). Response now includes
   `matched`/`added`/`removed` when a `project_id` was sent (derive it from
   `chrome.tabs.query`'s URL — `overleaf.com/project/<id>` — no new
   permission needed, `tabs` permission already covers reading the URL).
2. Each rendered node gets a "Claim to edit" button. Clicking calls
   `/claim`; on success the node's row shows an editable text area seeded
   from... **nothing yet** — the actual paragraph/section TEXT round-trip
   (read a node's raw source text, let the user edit it, write it back into
   the live Overleaf CodeMirror editor via the content script) is NOT in this
   spec's scope. This spec covers identity + coordination only (who owns
   which node, safely). The actual text-diff-and-write-into-CodeMirror piece
   is real, separate follow-on work — flag it as such, don't silently assume
   it's covered here.
3. `identity_confidence: "low"` renders a visible inline warning, not a
   blocked action.
4. Releasing: an explicit "Release" button calling `/release`, PLUS an
   automatic release-all-for-this-holder on `popup` unload (`chrome.runtime`
   doesn't reliably fire on popup close, so also release on next popup open
   if `claimed_at` shows this holder has stale claims older than one popup
   session — belt and suspenders, not relying on a browser lifecycle event
   that may not fire).

## 5. What this spec deliberately does NOT cover

- The actual text-write-into-Overleaf mechanism (item 4 above) — separate,
  later work once this coordination layer exists and is trusted.
- Multi-file Overleaf projects (`\input`/`\include` across files) — v1
  assumes the currently-open editor tab's text is the whole document, same
  assumption the existing read-only outline path already makes. A real
  multi-file project needs the content script to also know the OTHER open
  files, which Overleaf's DOM doesn't expose in the current single-file
  reading approach — flagged as a known v1 boundary.
- Any manual "add/remove project" UI — deliberately out of scope per the
  design decision this spec implements (project registration is automatic,
  keyed off the URL).
