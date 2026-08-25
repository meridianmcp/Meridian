# Post-handoff research submodule and evidence-pipeline proposal

**Date:** 2026-08-25  
**Purpose:** capture the reusable capabilities added or improvised after the
Wave 27 handoff, distinguish local research code from Meridian product work,
and define the promotion path.  
**Safety boundary:** this is a proposal and inventory. It does not authorize
editing the canonical dissertation or promoting any research artifact.

## Executive finding

After the last substantive handoff, the workflow acquired a working but
uncoordinated research-document stack:

```text
research computation and evaluation
    -> typed diagnostics and aggregation
    -> source-bound figures/tables
    -> staged DOCX repair and QA
    -> provenance/release evidence
```

The additions solved real incidents, but most are still thesis-local scripts
or fallback modules. They should not be described as Meridian features until
they have product-level contracts, tests, package boundaries, and promotion
gates.

## What was actually added

### 1. Research-evaluation hardening in `CURRENT_PROJECT_CODE`

These changes address scientific ambiguity and reproducibility problems rather
than Word formatting:

- `helpers/metrics.py:50` adds per-run width-baseline-root resolution instead
  of assuming one fixed directory depth.
- `helpers/metrics.py:2323-2565` now exposes support counts, candidate counts,
  assignment reasons, post-domain counts, and per-row gate outcomes for
  support projection. This distinguishes support absence, no candidate,
  LAPJV rejection, and a valid assignment.
- `helpers/metrics.py:6408-8718` adds independent MAT Raw versus MAT DSE
  projection modes, input hashes, raw/DSE failure attribution, and optional
  projection diagnostics. It also preserves separate DT and MAT attribution
  streams rather than treating exporter truncation as a matching failure.
- `helpers/metrics.py:14343-14418` writes MAT and DT failure-attribution CSVs.
- `metrics_engine.py:4370-4473` carries and exports the new DT attribution
  stream.
- `helpers/metrics.py:486-499` adds standard deviation to orthogonal-deviation
  summaries.
- `width_baseline_generator/dsepruning/dsepruning.py:73-126` makes DSE
  fail closed if reconstruction creates pixels outside the Raw input
  skeleton; it no longer interpolates graph edges into new support pixels.
- `helpers/summarize_metrics.py:56-121` adds shared technical-token-safe
  title-case label normalization.
- `helpers/summarize_metrics.py:147-219` and the width aggregation section
  centralize numeric aggregation, including length-weighted outputs.
- `helpers/summarize_metrics.py:5906-6450` consolidates paired-difference,
  confidence-interval, and comparison-figure rendering so figures do not
  independently invent sign conventions, colors, or labels.
- `helpers/summarize_metrics.py:3695-4710` corrects GT-supervision timing
  attribution so the complete atomic-plus-combined compute stage is not
  confused with an incomplete legacy subtotal.
- `helpers/supervision.py:7182-7210` persists actual end-to-end wall-clock
  timing separately from inclusive component sums.
- `pixi.toml` now exposes a discoverable serial test command and an explicit
  opt-in parallel command, with known unrelated collection failures excluded
  rather than hidden.

These are the most important research-facing additions. They should become a
small reusable `research_evidence` package, not remain a collection of
environment-variable branches inside one 15,000-line metrics module.

### 2. Local document/release fallback package

`CURRENT_PROJECT_CODE/tools/meridian_fallbacks/` became the practical local
reference implementation for capabilities that were missing or unsafe in the
live Meridian surface. Important reusable boundaries include:

- `artifact_reference_registry.py`: one lifecycle-aware registry for figure,
  table, equation, and caption artifacts, including source/script/output hashes
  and supersession links.
- `reference_binding_gate.py`: freezes and rechecks document-anchor, CSV-schema,
  generator-script, and base-DOCX bindings.
- `prestage_registry.py`, `prestage_artifact_registry.py`, and
  `final_prestage_release_gate.py`: materialized staging-package and
  review-readiness checks.
- `staging_lint_cli.py`: repeatable heading, cross-reference, caption-style,
  and residue audit that emits JSON and Markdown reports.
- `document_release_manifest.py` and `transactional_merge.py`: typed staged
  operations, base-hash checks, rollback, and promotion records.
- `output_provenance_gate.py`: SHA-256 and convergence-aware output checks.
- `docx_package_hardening.py` and `docx_completion_gate.py`: package, XML,
  relationship, paragraph-ID, media, equation, caption, and completion checks.
- `omml_builders.py`, `omml_validation.py`, and `docx_style_contract.py`:
  native equation construction/validation and controlled temporary-document
  styling.
- `table_caption_contract.py`: caption/table ownership and adjacency checks,
  including protection against formatting equation-layout tables as data
  tables.
- `render_word_com.ps1`: isolated, disposable-copy Word render receipts with
  timeout and cleanup behavior.

The package's own integration note explicitly says these are local fallbacks,
not shipped Meridian backend capabilities, and that several gates remain
advisory unless a caller wires them into promotion.

### 3. Meridian repository changes now present in the working tree

The current Meridian changes address product-level incidents discovered while
using the thesis workflow:

- `extensions/meridian-docs/meridian_docs/ooxml_integrity.py:86-152` adds a
  Heading-3 capitalization audit, while
  `:231-316` incorporates package validation and
  `:316-401` provides namespace-preserving serialization, media pruning, and
  comment-attribute normalization.
- `extensions/meridian-docs/meridian_docs/docs_intel.py` now uses the
  namespace-safe serializer and normalizes legacy Word comment attributes
  before atomic writes.
- `meridian/capability_contract.py:872-939` exposes `unverified` capability
  state and fails closed when a required capability cannot be verified.
- `meridian/handoff.py:1699-1800` treats executable goal payloads as atomic
  when a byte budget would otherwise truncate a token-bound body.

These are real working-tree changes with tests, but they should remain marked
as **candidate product changes** until the focused tests and full repository
gate pass and the changes are committed/reviewed.

## What should not be merged as-is

The following are useful incident-specific scripts but are not good permanent
Meridian APIs:

- wave-numbered table/figure patch scripts under `tools/`;
- one-off `repair_*` scripts under `scripts/` that encode a single dissertation
  paragraph, caption, or page location;
- environment-variable combinations that silently alter the scientific
  comparison regime;
- generated QA JSON/CSV, scratch XML, SQLite sidecars, caches, and rendered
  candidates;
- assumptions that a filename such as `final`, `current`, or `title_free`
  establishes artifact authority.

Those artifacts should remain fixtures, regression cases, or migration tools.
The durable API must consume explicit schemas and manifests.

## Proposed productization

### R-1 — Research evidence model (P0)

Create a typed `ResearchEvidenceRun` containing source roots and hashes,
generator/script hashes, configuration/environment values, dataset and image
counts, geometry/domain/emission modes, aggregation, statistical procedure,
and output artifact IDs. A run must be reproducible or explicitly marked
`diagnostic`, `archival`, `held`, or `unverified`.

### R-2 — Typed correspondence and failure attribution (P0)

Promote the projection diagnostics into a reusable schema with one row per
query point and explicit states such as `assigned`, `support_empty`,
`no_candidate_within_gate`, `lapjv_unassigned`, `lapjv_rejected_by_row_gate`,
and `invalid_query`. The schema must preserve method-specific support domains
and never silently replace independent Raw/DSE evaluation with shared-validity
rows.

### R-3 — Shared aggregation and paired-comparison contract (P0)

Define typed aggregation objects for per-image mean, length-weighted mean,
pooled pointwise, paired difference, confidence interval, and wins/denominator.
Figures, tables, captions, and manuscript claims must consume the same object.
The sign convention and reference method must be explicit.

### R-4 — Research artifact registry (P0)

Unify source CSV, generated figure/table, script, configuration, document
anchor, caption/number slot, render receipt, and supersession status under one
artifact ID. Current-artifact lookup must reject duplicate live slots and
missing or drifted provenance.

### R-5 — DOCX release transaction (P0)

Use one staged `DocumentChangeSet`/`ReleaseManifest` to bind source evidence,
anchors, operations, package integrity, equation/figure/table audits,
provenance, rendering, human approval, and compare-and-swap promotion. The
existing DOCX proposal in
`docs/meridian-build-proposal-track-b-2026-08-24.md` is the detailed product
plan for this lane.

### R-6 — Fast batch execution with explicit render barriers (P1)

Run package-level transformations in one isolated batch, then render once under
an explicit backend policy. Word/COM is a bounded verification backend, not
the inner-loop document database. Every timeout needs a cleanup receipt.

### R-7 — Capability and fallback synchronization (P1)

Every local fallback must declare its upstream replacement, limitation,
tests, and migration status. Generate capability docs from the live manifest or
fail a synchronization test when docs, exports, versions, and registrations
drift.

## Acceptance criteria

1. A synthetic Raw/DSE correspondence fixture produces identical, stable
   attribution rows across repeated runs and distinct independent/shared modes.
2. Changing a source CSV schema, generator script, configuration, or base DOCX
   blocks promotion and identifies the changed binding.
3. A figure and table with the same comparison object agree on sign, method
   order, aggregation, confidence interval, and wins denominator.
4. A staged DOCX with duplicate paragraph IDs, malformed OMML, external
   relationships, or an orphaned artifact cannot be promoted.
5. Word/COM unavailable, timed out, or only historically verified is reported
   as `unavailable`, `degraded`, or `historical`, never `verified`.
6. Re-running a transform is idempotent or reports only declared volatile
   metadata changes.
7. The canonical dissertation remains byte-identical during all automated
   staging tests.

## Recommended implementation order

1. Freeze the local schemas and golden fixtures for R-1 through R-3.
2. Extract the research-evidence and aggregation code from the thesis helpers
   without changing current result semantics.
3. Connect R-4 to the existing artifact/reference and provenance fallback
   modules.
4. Implement the release manifest and mandatory gates from R-5.
5. Add the Meridian Docs namespace/package hardening and capability-contract
   changes behind focused tests.
6. Add bounded batch transforms, render receipts, and fallback/documentation
   synchronization.

## Canonical evidence pointers

- Thesis research source: `C:\Users\13144\Documents\Masters_Thesis\CURRENT_PROJECT_CODE`
- Research reconciliation: `C:\Users\13144\Documents\Masters_Thesis\CURRENT_PROJECT_CODE\wave50_results_reconciliation_report.md`
- Local fallback contract: `C:\Users\13144\Documents\Masters_Thesis\CURRENT_PROJECT_CODE\tools\meridian_fallbacks\MERIDIAN_INTEGRATION_NOTE.md`
- Reliability findings: `C:\Users\13144\Documents\Masters_Thesis\CURRENT_PROJECT_CODE\provenance\meridian_reliability_audit_20260821.md`
- Table/OMML contract: `C:\Users\13144\Documents\Masters_Thesis\CURRENT_PROJECT_CODE\proposals\meridian_docs_table_contract_wave53_20260818.md`
- Existing document-release proposal: `C:\Users\13144\Documents\Meridian\repository\docs\meridian-build-proposal-track-b-2026-08-24.md`
- Current Meridian OOXML integrity implementation: `C:\Users\13144\Documents\Meridian\repository\extensions\meridian-docs\meridian_docs\ooxml_integrity.py`

## Status

This proposal records the post-handoff delta and the proposed reusable
boundaries. It does not claim that R-1 through R-7 are implemented in
Meridian. The thesis-local code and fallback package are evidence and fixtures;
the Meridian build must still provide the product contracts, tests, and
promotion wiring.
