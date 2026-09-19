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
If/when the standalone repo is ever pushed to a real git remote (GitHub,
etc.), swap the local path for that remote URL in both commands above.
