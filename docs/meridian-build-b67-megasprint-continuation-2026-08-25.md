# B67 megasprint: continuation handoff

**Date:** 2026-08-25
**Umbrella item:** ee73d6ee (proposal b67b0139)
**Author:** session `4dfd2a59-bfd5-43e4-9e05-3ba60a78e140`

## Completed investigations (wave 0 + most of wave 1)

| Item | Deliverable | Verification |
|---|---|---|
| B67-1 (`c85f08ab`) | `docs/meridian-build-b67-1-canonical-reconciliation-2026-08-25.md` — discrepancy matrix, product boundary, R-1..R-7 map, planner continuation | 2 independent PASS passes (1st caught and the implementer fixed a real factual error re: `ooxml_integrity.py`'s commit status) |
| B67-2 (`bcdc00bd`) | `docs/meridian-build-b67-2-equation-writer-gap-matrix-2026-08-25.md` — 16-symbol gap matrix for equation writers/OMML builders | Independent PASS, 15 symbols spot-checked line-by-line against real code |
| B67-3 (`54337dc8`) | `docs/meridian-build-b67-3-ooxml-omml-document-graph-2026-08-25.md` — 15 node types, 13 edge types, identity/revision model, placement decision (new `meridian/ooxml_graph.py` pair) | Independent PASS, every schema claim checked against `research_graph.py`/`db/research_graph.py`/`provenance.py`/`doc_store.py` |
| B67-5 (`e0667e43`) | `docs/meridian-build-b67-5-capability-matrix-and-render-truth-table-2026-08-25.md` — 19-row capability matrix, live LibreOffice/Word-COM probe | Independent PASS, probe reproduced live (LibreOffice installed but undetected — PATH gap; Word-COM genuinely working) |

Wave 0 (reconciliation) is fully done. Wave 1 (investigation) is done **except B67-4**, which is explicitly out of scope this megasprint (see below).

## Explicitly not done, and why (do not treat as gaps)

| Item | Reason held |
|---|---|
| B67-4 (`d15b51cd`, paper claims/baselines/datasets) | Its own stored notes: "permanently backburnered to 2030 by Adam; do not reactivate without explicit human approval." This proposal's own wave 3 language repeats the same boundary. Not attempted. |
| B67-6 (`89e2a2cb`, numbered-equation writer code) | Its own stored notes: "explicitly not claimed in this planning session... implement only after B67-1/B67-2 identify the canonical owner." B67-1/B67-2 identified real *gaps* (asymmetric render verification, disconnected receipt ledger, dual hash concepts, provenance binding unreachable from the equation-writer family) but did not name a single canonical owner module for the numbered-equation writer path — that's a genuine open decision, not mine to make. B67-6 also must first reconcile with existing items `29fe401f` and `ef8875a9` per its own notes, which this session did not do. |
| B67-7 (`68431839`) | Depends on B67-6, transitively blocked. |
| B67-8 (`60c3ebfe`) | Depends on B67-7, transitively blocked. |
| B67-9 (`a8604735`) | Depends on B67-8, transitively blocked. |
| B67-12 (`e77c38d4`) | Depends on B67-9, transitively blocked. |
| B67-10 (`a5b6014b`, HUMAN GATE: production render authority) | Named "HUMAN GATE" in its own title — explicitly for Adam, not an executor. |
| B67-11 (`846a515a`, HUMAN GATE: 2030 reactivation approval) | Same — explicitly human-only. |

## Unresolved renderer/data limitations (carried forward from B67-2/B67-5)

- LibreOffice is physically installed (`C:\Program Files\LibreOffice\program\soffice.exe`) but `render_gate.py`'s `shutil.which`-based detection reports it unavailable because the install directory isn't on PATH — a fixable environment gap, not a LibreOffice defect.
- MDE-7's durable render-receipt ledger (`RenderReceipt`/`render_with_receipt`/`list_render_receipts`/`check_release_render_gate`) is real and tested but exists only on the unlanded `mde-rework-44fc1ffe-536-2` branch, and nothing in production code calls it yet — everything wired today uses the older stateless single-call check with no memory of prior render attempts.
- Only `insert_equation_local` gets full post-write re-verification + render-gate + CAS-restore among the equation-writer family; `edit_equation_local`, `remove_equation_local`, `append_text_run_after_math`, and `copy_section` skip all three. `artifact_provenance` is accepted only by `insert_caption`/`relocate_figure`, never by any equation-writer op.
- Three non-unified hash concepts coexist (`promoted_sha256` write-time CAS fingerprint, `manifest_hash` over changed parts, `docx_integrity_gate`'s own read-time structural fingerprint) with no relationship to `meridian_outputs`' actual provenance/hash registry.
- `docx_integrity_gate.py`'s `RECIPE_CHECK_REGISTRY` names real functions it never dispatches to — every artifact gets the same fixed 3 checks regardless of declared recipe.

## Safe next action

Nothing in the B67 track is currently safely actionable by an executor. The two remaining live threads are:

1. **B67-10/B67-11** — genuinely need Adam's decision (render-authority provisioning; 2030-reactivation approval/hold).
2. **B67-6's canonical-owner question** — needs a planner (not an executor) to read B67-1/B67-2's evidence and decide where the numbered-equation writer belongs relative to `29fe401f`/`ef8875a9`, before any implementation is claimable.

Separately, unrelated to B67 specifically: the broader MDE megasprint's completed work (MDE-1..10, B1, B2, plus `455cfc36`/`aec043cb`/`ec91e311`/`06bcaca2`) is sprint-board-complete and independently verified, but sits on three unlanded branches, blocked from reaching `dev` by a legitimate git safety refusal (the main checkout has independent uncommitted candidate work). See `docs/meridian-build-b67-1-canonical-reconciliation-2026-08-25.md` §1/§5 for the full branch/commit inventory and the human decision this also needs.

## Explicit no-reactivation boundary

The DocBank/arXiv-recovery/Pandoc-conversion/LayoutLMv3-baseline paper-evaluation chain (B67-4's full scope) remains backburnered to 2030. No dataset acquisition, GPU provisioning, or benchmark execution against that chain may occur without Adam's explicit, separate approval — this boundary was not crossed by any item in this megasprint and should not be inferred as cleared by anything above.
