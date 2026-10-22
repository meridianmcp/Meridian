# Meridian LaTeX Standalone Reconciliation

**Audit date:** 2026-10-02
**Scope:** Sprint item `1b4aa8d0-905e-445d-978d-a454bd4ea096` only. Read-only audit of the standalone `meridian-latex` repository against the canonical `extensions/meridian-latex/` tree and its Meridian Docs/Zotero interfaces.

## Result

No LaTeX engine source needs to be carried forward. The standalone engine snapshot is already reconciled into canonical `origin/dev` by commit `8add21fbae64f71340540018f1e1b6f560a26449`. The subtree object at that commit and at current `origin/dev` is identical: `275e50a4adca208e109c7531e4d12816d034295d`.

The source repository is clean at `master` / `origin/master`, SHA `b77fb99ad4d74c48eb07dec09192a0402bedfaf4`. Its separate `meridian-latex-stable` worktree is also clean at the same SHA. Neither checkout was edited.

## Provenance and parity

The original merge `2e9c6d403e15c2ce496d939d46aa6f1ab8748436` has second parent `119b38dbf8b9f2e9e06742686bacfc4946383fba`, preserving the imported standalone history through that split. Standalone `master` now has 19 commits after that split, through the six TypeScript migration batches and the final cutover `b77fb99`.

The canonicalization commit `8add21f` is a single-parent snapshot commit, not a merge of those 19 later standalone commits. It records the source changes in its commit message, but the later individual commit ancestry is not present in the monorepo history. The standalone remote remains the source for that detailed history. Do not replay the commits: the content is already present.

At current `origin/dev` (`52bc8614e802cd1fb4583bfe83ef076b0f726d1a)):

| Comparison | Result |
| --- | --- |
| Canonical subtree files / standalone root files | 68 / 70 |
| Canonical-only path | `SYNC.md` |
| Standalone-only paths | `.github/workflows/publish.yml`, `.mcp.json`, `engine/package-lock.json` |
| Same path, different content | `README.md`, `engine/package.json` |
| Remaining paths with identical Git blob IDs | 65 |
| Canonical engine files / standalone engine files | 57 / 58 |
| Engine parity | Same source/test files; only standalone `engine/package-lock.json` is absent and `engine/package.json` has intentional workspace packaging changes |

The `origin/dev` subtree hash is identical to the subtree at `8add21f`; `git diff 8add21f origin/dev -- extensions/meridian-latex` is empty. Thus later dev commits did not change this extension tree.

## Change groups and carry-forward decision

| Standalone commits | Content | Audit disposition |
| --- | --- | --- |
| `9b5c677` through `ec36887` | Login/browser lifecycle fixes, Overleaf OT writing, local pull, write snapshot/reconcile | Present in the canonical engine snapshot; no port. |
| `771da24`, `890523e`, `cd652f0` | MCP server, claim-aware writes, safe-tier tools and CLI wiring | Present; no port. |
| `ef6563a`, `cda7ae3`, `79c3e2a` | Static LaTeX lint, style guidance/citation lookup, 23-tool README inventory | Present, with the README adapted to monorepo paths; no port. |
| `7de72dd` through `b77fb99` | Six JS-to-TS migration batches, build/typecheck setup, new modules and paired tests | Present; canonical engine source/test blobs match the standalone snapshot except the intentional package manifest. No port. |

Keep the canonical integration differences:

- `engine/package.json` marks the workspace package private, removes independent publish configuration, requires Node 22, and documents the bundled distribution.
- `npm/scripts/bundle-latex.mjs` copies compiled `engine/dist`, not raw TypeScript source. The root `npm-publish.yml` job installs and builds the engine before bundling.
- Do not copy the standalone publish workflow, local `.mcp.json`, or nested engine lockfile into the monorepo. The latter uses the root workspace lock.
- Retain the monorepo `SYNC.md` and README path/runtime updates.

**Decision-log conflict found:** workspace decision `1c1481b0-a916-495c-b353-d0e6754803c6` says the standalone `@meridianmcp/latex` npm publishing path is unaffected. Current canonical `engine/package.json` explicitly says the engine is not independently published and ships as a subpath/CLI of `@meridianmcp/mcp`; it is `private: true`. Treat only that distribution claim in the older decision as stale and reconcile the decision record before planning a package release. This audit does not change publishing configuration or the decision record.

## Meridian Docs and shared citation resolution

The engines share citation identity concerns, but their document operations are format-specific:

- Meridian Docs exposes DOCX CSL citation field insertion and citation-key scanning. Its `sync_bibliography` accepts a citation-key-to-CSL-JSON mapping, inserts or updates matching bibliography entries, and reports missing data for keys the caller still needs to fetch.
- Meridian core already has `meridian.zotero_client.resolve_citation_ref`, which normalizes DOI, `zotero:<key>`, and bare citekey lookups against the local Zotero API. `meridian.pointers._zotero_citation_backend` adapts that resolver to the citation backend interface.
- LaTeX `lookupCitationKey` resolves Better BibTeX tags through Zotero's local API. This can adapt to the shared normalized reference contract; do not duplicate the resolver inside Meridian Docs.
- Keep TeX AST/macro identity, BibTeX/natbib behavior, Overleaf auth/OT/sync and compiler receipts with Meridian LaTeX. Keep DOCX CSL fields, OOXML writes, styles and render gates with Meridian Docs.

The follow-on port item `929697f2-d5b8-46b3-9be2-c3bf661077a0` is still pending and explicitly depends on this audit plus shared paper-workflow contract `fe4d6738-d775-4e3d-8801-9eab9274fae1`, which is currently `in_progress`. The audit clears its audit prerequisite; implementation should wait until that contract is accepted.

Legacy items `3bad2920` and `6160d667` remain marked `in_progress` under archived session `93aec27a`. Per their item notes, this audit did not mutate those records or infer that every live Overleaf validation goal is complete from tree parity alone.

## Verification limits

The `8add21f` commit message records a local engine run of 451/451 tests and a successful npm bundle/smoke-import check with 28 exports. A GitHub Actions run lookup for that exact SHA returned no runs, so this audit treats those as commit-recorded results, not an independently confirmed CI result. No tests were run during this read-only audit; a shared full-suite run was already active.

Codebase Memory graph discovery was used for the canonical LaTeX architecture, Zotero resolver, and Meridian Docs citation APIs. Serena tools were not exposed in this session. The Docs graph snippet returned mismatched source ranges, so exact DOCX function behavior was checked from the known canonical `origin/dev` file after graph discovery.
