# Fast-lane sync with the standalone meridian-latex repo

`extensions/meridian-latex/` was merged into this monorepo from a standalone
repo via `git subtree add` (full history preserved). The standalone repo
still exists and is still where fast, exploratory iteration on just this
subdirectory can happen without redoing the full subtree-add ceremony.

These two commands are the fast lane: use them directly, as needed, instead
of re-running `git subtree add`.

## Pull (standalone repo -> monorepo)

Bring changes made in the standalone repo into
`extensions/meridian-latex/` here:

```
git subtree pull --prefix=extensions/meridian-latex "C:/Users/13144/Documents/meridian-latex" master -m "sync: pull meridian-latex standalone updates"
```

Use this when you (or someone else) committed directly to the standalone
repo's `master` branch and want those commits reflected in the monorepo.
Safe to run speculatively — if there's nothing new, it's a no-op ("Already
up to date").

## Push (monorepo -> standalone repo)

Send commits made to `extensions/meridian-latex/` in this monorepo back out
to the standalone repo:

```
git subtree push --prefix=extensions/meridian-latex "C:/Users/13144/Documents/meridian-latex" master
```

Use this when you want the standalone repo to pick up work done here —
e.g. keeping it usable as an independent checkout, or before iterating on
it directly outside the monorepo.

**Warning:** `push` mutates the external standalone repo's `master`
branch. It is a deliberate, occasional, human-run operation — never run it
automatically or blindly, and never wire it into CI. Always know what
you're pushing before you run it (e.g. `git log` on
`extensions/meridian-latex/` since the last sync).

## Machine-local path

The standalone repo path above (`C:\Users\13144\Documents\meridian-latex`)
is machine-local — it only works on the machine where that checkout lives.

**Update (2026-09-25): the standalone repo now has a real remote** —
`https://github.com/meridianmcp/meridian-latex.git`, branch `master`. The
local path still works fine on this machine and is faster for the fast-lane
use case described above; swap in the remote URL instead if you're running
these commands from a different machine, or if the local checkout ever
moves/is removed.

## Known divergence (2026-09-25) — read before running `pull`

The two copies have drifted independently since roughly commit `119b38d`
("publish as @meridianmcp/latex") and are **not** a simple ahead/behind
pair — both sides grew real, non-overlapping feature work:

- **This monorepo's `extensions/meridian-latex/`** gained its own MCP
  server (`d9fb0358`) and was repackaged to ship as a subpath of
  `@meridianmcp/mcp` instead of a separate package (`afb9bc5c`). Neither
  commit exists in the standalone repo's history (checked: `git log --all
  --grep` on the standalone repo turns up nothing for either).
- **The standalone repo's `master`** independently grew write-path
  robustness (snapshot+reconcile), the real-time OT write primitive,
  doc-tree path resolution, docparse ports, and — as of a 2026-09-25
  session — its *own* MCP server (ported from a stale copy of this
  monorepo's `mcp-server.js`, then extended to 15 tools) plus claim-aware
  writes wired into `applyFieldEdit`/`cli.js write`. None of that exists in
  this monorepo's copy.

Net effect: **two different `mcp-server.js` implementations now exist**,
with different tool counts and no common recent ancestor for either to
diff cleanly against. Running `git subtree pull` here right now would not
be a clean fast-forward — it would need a real three-way merge with manual
conflict resolution on `mcp-server.js` specifically, deciding which tool
set is authoritative (or unioning them).

**Do not run `pull` or `push` here without first deciding which
`mcp-server.js` is canonical.** That reconciliation is tracked as ongoing
work in the standalone repo (see its own commit history from 2026-09-25
onward); once it lands there, `pull` becomes safe again and this note
should come out.
