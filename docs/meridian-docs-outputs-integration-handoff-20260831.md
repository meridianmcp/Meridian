# Executor handoff: Meridian Docs ↔ Meridian Outputs integration

**Status:** ready for scoped implementation after contract review  
**Date:** 2026-08-31  
**Branch:** `codex/meridian-docs-equation-graph-20260831b`  
**Worktree:** `C:\Users\13144\Documents\Meridian\worktrees\meridian-docs-equation-graph-20260831`  
**Starting revision:** `cec0b66f`

## Assignment

Investigate and then implement the smallest read-only integration that joins
the Meridian Docs equation graph, notation audits, workspace lineage, render
receipts, and Meridian Outputs artifact/evidence records. The companion
proposal is the decision record:

`docs/meridian-docs-outputs-integration-proposal-20260831.md`

Native OMML is authoritative. Existing equation operations remain read-only.
This handoff does not authorize DOCX mutation, production changes, a version
bump, push, deployment, database migration, or access to OneDrive/canonical
thesis files.

## Verified starting state

- Focused equation-graph suite: **84 passed in 3.53 s**.
- Stress benchmark: 256 equations/580 nodes in 0.0739 s; 1,024/2,308 in
  0.2886 s; 2,048/4,612 in 0.3853 s.
- Registry probe: registration, source-edge binding, and artifact resolution
  succeeded.
- The same probe through the existing typed/legacy provenance status path
  returned `provenance_type="unknown"`; this is the primary integration gap.
- Render receipts bind source DOCX/PDF/backend/QA state but do not bind graph
  or notation hashes.
- Workspace lineage validates workspace relationships but not hashes placed
  in free-form scope metadata.
- The current stress fixture is a ZIP containing only
  `word/document.xml`; it is intentionally fast but not a Word-valid package.

## Source pointers

### Meridian Docs

- `extensions/meridian-docs/meridian_docs/equation_graph.py:343` —
  `build_equation_graph`
- `extensions/meridian-docs/meridian_docs/server.py:1450` — equation
  extraction; `:2890-2975` — integrity/notation audits; `:3020` — graph API
- `extensions/meridian-docs/meridian_docs/document_workspace.py:105` —
  `DocumentWorkspace`; `:239` — lineage validation
- `extensions/meridian-docs/meridian_docs/render_gate.py:1195` — receipt
  creation; `:1339` — release gate; `:1471,1647` — promotion checks
- `extensions/meridian-docs/tests/test_equation_graph_stress.py:15,63,119,159`
  — fast stress fixture and boundary tests
- `docs/meridian-docs-equation-graph-contract.md` — graph authority and
  read-only semantics

### Meridian Outputs

- `extensions/meridian-outputs/meridian_outputs/artifact_registry.py:340` —
  `register_artifact`; `:534` — resolve; `:796` — source-edge binding
- `extensions/meridian-outputs/meridian_outputs/provenance_status.py:436` —
  status; `:793` — envelope construction
- `extensions/meridian-outputs/meridian_outputs/research_evidence.py:325,380,426`
  — evidence records, links, and typed envelope
- `extensions/meridian-outputs/meridian_outputs/server.py:465,641,683,720,759,818,849,871`
  — registered provenance, artifact, and source-edge APIs

## Ordered sprint items

### MDE-INT-01 — Freeze the analysis-receipt contract

**Depends on:** none. **Priority:** blocker.

Define the JSON/XML fields, status vocabulary, deterministic identity,
partial/degraded semantics, relation vocabulary, and portable/redacted
locator rules. Use the DOCX SHA-256 as the content join key. Keep stable
registry artifact identity and current-content hash distinct until the identity
choice is explicitly approved.

**Acceptance:** deterministic fixture receipts round-trip; source-hash
mismatches are rejected; unknown fields survive; absent and partial evidence
are distinguishable.

### MDE-INT-02 — Add the Docs-side read-only adapter

**Depends on:** MDE-INT-01.

Compose existing graph/audit/workspace results into an analysis receipt without
changing existing graph JSON or MCP signatures. Do not invoke rendering,
rewrite DOCX, or write sidecars.

**Acceptance:** same bytes/options yield identical analysis content; changed
bytes produce stale/blocked binding; native OMML remains the only equation
authority.

### MDE-INT-03 — Project registry records into typed evidence

**Depends on:** MDE-INT-01.

Build a read-only registry-to-evidence projection. Preserve registry identity,
source edges, and legacy/index status as separately labeled facts. Never turn a
basename match or an `unknown` status into authoritative success.

**Acceptance:** the confirmed probe yields one explicit envelope containing
registry identity plus separate legacy status, with no silent contradiction.

### MDE-INT-04 — Bind render receipts to analysis

**Depends on:** MDE-INT-01 and MDE-INT-02.

Add optional graph/notation/source binding and freshness comparison to the
existing render receipt. Rendering remains explicit and is never triggered by
an audit or adapter call.

### MDE-INT-05 — Add a package-valid stress DOCX

**Depends on:** MDE-INT-01; parallel with INT-02 and INT-03 after contract
freeze.

Retain the minimal XML fixture for speed. Add a deterministic minimal valid
DOCX containing mixed inline/line-separated/table equations, fields or
bookmarks, relationships, styles, and representative malformed OMML. Exercise
graph, integrity, notation, registry, envelope, and receipt binding while
asserting source-byte immutability.

### MDE-INT-06 — Add the read-only promotion gate

**Depends on:** MDE-INT-02 through MDE-INT-05.

Report equation graph, notation, Outputs, workspace, and render states
separately with machine-readable reasons. The gate must never repair or
promote a DOCX.

## Required regression matrix

1. Registry-to-envelope projection preserves artifact IDs and source edges.
2. Registry status and legacy/index status can disagree without being merged.
3. Source-hash mismatch is stale/blocked, not successful.
4. Missing notation is `not_supplied`, not an empty successful audit.
5. Missing Outputs evidence is `partial` or `not_supplied`, as appropriate.
6. Valid and minimal stress fixtures agree on graph semantics.
7. JSON and XML receipt round-trips preserve unknown fields and status detail.
8. Analysis, audit, and gate calls leave source DOCX bytes unchanged.
9. Existing 84-test suite remains green.
10. No graph or adapter path rasterizes pages or launches Word/COM.

## Execution and stop rules

Use the existing focused tests first; add focused contract tests before any
broader suite. Keep graph parsing, receipt composition, and registry projection
in-process and hash-based. Rendering is an explicit final-stage operation only.

Stop and return for review if implementation would require a mandatory
Docs→Outputs runtime import, a database or sidecar schema migration, a change
to native OMML authority, automatic notation renaming, implicit rendering,
DOCX mutation, or a change to shared branches/canonical files.

## Completion evidence

The executor must return:

- exact files and line-level pointers changed;
- contract and status mapping summary;
- test command/output, including the original 84-test suite;
- benchmark comparison showing no material regression;
- one valid-package stress receipt and one minimal-fixture receipt;
- a source-byte immutability result;
- explicit unresolved decisions, if any;
- no claim of production readiness until the identity decision and review are
  complete.
