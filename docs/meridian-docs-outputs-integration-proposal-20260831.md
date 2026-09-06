# Meridian Docs ↔ Meridian Outputs integration proposal

**Status:** scoped investigation complete; proposal only  
**Date:** 2026-08-31  
**Branch:** `codex/meridian-docs-equation-graph-20260831b`  
**Worktree:** `C:\Users\13144\Documents\Meridian\worktrees\meridian-docs-equation-graph-20260831`  
**Revision:** `cec0b66f`

## Scope guard

This proposal does not authorize a version bump, push, deploy, merge into
shared `dev`/`main`, database migration, canonical/OneDrive thesis access, or
DOCX mutation. Native OMML remains authoritative. Equation operations remain
read-only until a separate review approves a mutation workflow.

## Executive decision

The equation graph and the provenance components are individually useful, but
they do not yet form one reproducible research-document chain. The next build
should add an explicit analysis receipt and a bridge between the stable
artifact registry and the typed research-evidence envelope.

It should not make `meridian-docs` import `meridian-outputs` as a mandatory
runtime dependency, and it should not hide graph hashes inside free-form
metadata without a contract.

The intended chain is:

```text
DOCX bytes
  └─ source_docx_sha256
       └─ equation graph
            ├─ graph_sha256
            ├─ equation/placement/reference findings
            └─ notation-manifest hash, when supplied
                 └─ analysis receipt
                      ├─ output artifact IDs/source edges
                      ├─ typed evidence envelope
                      └─ render receipt ID/hash, when rendering occurred
```

## Investigation evidence

The required focused suite was rerun in this isolated worktree:

```text
84 passed in 3.53s
```

The deterministic graph stress path was benchmarked without writing to a
source document:

| Synthetic equations | Graph nodes | Wall time |
|---:|---:|---:|
| 256 | 580 | 0.0739 s |
| 1,024 | 2,308 | 0.2886 s |
| 2,048 | 4,612 | 0.3853 s |

This indicates healthy bounded parser performance. It does not prove
Word-openability or end-to-end provenance because the fixture is not a complete
DOCX package.

The integration probe then registered the generated stress document in
`artifact_registry`, bound a source edge, and resolved the artifact
successfully. Calling `provenance_status.get_provenance_status` for that same
path returned `provenance_type="unknown"`. This confirms that the stable
registry and the legacy/indexed status path are currently separate ledgers.

## Current component map

| Component | Current capability | Confirmed gap | Risk |
|---|---|---|---|
| `equation_graph` | Read-only OMML graph, placements, references, conflicts, DAG, `source_fingerprint`, `graph_sha256` | No typed Outputs artifact/evidence record | High |
| Docs MCP | Exposes graph, audits, workspace, and render tools | No single composition operation | High |
| `DocumentWorkspace` | Immutable source hash, scope/profile, parent/supersedes lineage | Analysis hashes can sit in arbitrary scope and are not lineage-validated | Medium-high |
| `render_gate` | Durable DOCX/PDF/backend/field-refresh/visual-QA receipt | No graph or notation hash binding | High |
| Outputs registry | Relocation-safe artifact ID, content hash, source edges, lifecycle | Not consulted by typed status/envelope builder | High |
| Outputs status/envelope | Exact/indexed/manifest status and typed evidence records | Cannot consume registry IDs as primary evidence | High |
| Stress suite | Deterministic mixed-placement graph stress | Only one-part XML ZIP, not a Word-valid DOCX; no provenance integration | High |

## Confirmed gaps and pointers

### 1. Outputs has parallel provenance ledgers

Stable registry entry points:

- `extensions/meridian-outputs/meridian_outputs/artifact_registry.py:340`
  — `register_artifact`
- `artifact_registry.py:534` — `resolve_artifact`
- `artifact_registry.py:796` — `bind_source_edge`

Typed/legacy status entry points:

- `extensions/meridian-outputs/meridian_outputs/provenance_status.py:436`
  — manifest-backed status
- `provenance_status.py:793` — `build_provenance_envelope`
- `research_evidence.py:325` — `EvidenceRecord`
- `research_evidence.py:380` — `EvidenceLink`
- `research_evidence.py:426` — `ProvenanceEnvelope`

`build_provenance_envelope` starts from paths and the status resolver. It does
not consult `artifact_registry`. A file can therefore be `resolved` by stable
artifact ID while the same path is `unknown` through the typed status path.

**Proposal:** make a read-only registry-to-evidence projection. Registry
identity should be primary durable identity; local path and legacy/index status
should remain explicitly labeled supplementary evidence.

### 2. Graph output is not an analysis receipt

- `extensions/meridian-docs/meridian_docs/equation_graph.py:343`
  — `build_equation_graph`
- `extensions/meridian-docs/meridian_docs/server.py:3020`
  — MCP registration
- `docs/meridian-docs-equation-graph-contract.md`
  — graph semantics and limitations

The graph exposes `source_fingerprint` and `graph_sha256`, but does not bind
them to a stable document artifact ID, workspace lineage revision, notation
manifest hash, render receipt, Outputs source edge, or typed evidence envelope.
The caller must manually copy these fields between systems.

**Proposal:** add a read-only analysis-receipt adapter while preserving the
existing graph JSON and MCP signature.

### 3. Render receipts prove bytes, not analysis state

- `extensions/meridian-docs/meridian_docs/render_gate.py:1195`
  — `render_with_receipt`
- `render_gate.py:1339` — `check_release_render_gate`
- `extensions/meridian-docs/meridian_docs/server.py:421,484`
  — MCP wrappers

The receipt correctly records `source_docx_sha256`, PDF details, backend,
field-refresh state, and visual-QA status. It does not identify the graph or
notation audit performed for those bytes. A fresh render can therefore coexist
with stale analysis, and a fresh graph can coexist with an older render.

**Proposal:** add optional `analysis_binding` containing the source DOCX hash,
graph hash, and notation-manifest hash. Compare only when supplied; never
infer that rendering performed equation analysis.

### 4. Workspace lineage does not validate analysis lineage

- `extensions/meridian-docs/meridian_docs/document_workspace.py:105`
  — `DocumentWorkspace`
- `document_workspace.py:239` — `validate_lineage`

Workspace lineage correctly validates IDs, project boundaries, relations, and
cycles. It does not validate that graph/notation/render hashes placed in
`scope` belong to the workspace source snapshot.

**Proposal:** use a typed analysis binding or separate receipt that references
the workspace; do not overload the workspace model with unvalidated analysis
fields.

### 5. The stress “DOCX” is not a complete DOCX package

- `extensions/meridian-docs/tests/test_equation_graph_stress.py:15`
  — minimal ZIP builder
- `test_equation_graph_stress.py:63` — `_stress_docx`
- `test_equation_graph_stress.py:119` — large mixed test
- `test_equation_graph_stress.py:159` — node-boundary test

The generated ZIP contains only:

```text
['word/document.xml']
```

That is ideal for fast XML traversal/determinism testing, but cannot exercise
content types, relationships, styles, fields, media, Word package validity, or
render receipts. Retain it and add a second deterministic fixture built from a
minimal real DOCX template.

### 6. Individual APIs are registered; composition is absent

Docs exposes `extract_equations` around `server.py:1450`, integrity/notation
audits around `server.py:2890-2975`, graph construction at `server.py:3020`,
and render receipts at `server.py:421`/`484`. Outputs separately exposes the
registry, source-edge, status, and envelope operations.

There is no read-only `build_document_analysis_receipt`-type operation. This
must be added explicitly rather than hidden inside the graph or renderer.

## Proposed receipt shape

This is a proposal, not a production schema:

```json
{
  "receipt_type": "document_analysis",
  "receipt_version": "1",
  "receipt_id": "deterministic-or-explicit-id",
  "document": {
    "kind": "document",
    "artifact_id": "optional-stable-outputs-id",
    "locator": "portable-or-redacted-locator",
    "source_docx_sha256": "..."
  },
  "workspace": {
    "workspace_id": "optional",
    "project_id": "...",
    "lineage_status": "valid|not_supplied|invalid"
  },
  "equation_analysis": {
    "graph_sha256": "...",
    "equation_count": 0,
    "notation_manifest_sha256": "optional",
    "contract": "meridian-docs-equation-graph-v1",
    "native_omml_authoritative": true
  },
  "output_evidence": {
    "envelope_id": "optional",
    "artifact_ids": [],
    "source_edges": [],
    "status": "complete|partial|not_supplied"
  },
  "render": {
    "receipt_id": "optional",
    "receipt_source_docx_sha256": "optional",
    "status": "matched|mismatched|not_supplied"
  },
  "integrity": {
    "status": "verified|partial|blocked",
    "reasons": []
  }
}
```

Rules: the DOCX hash is the join key; graph hash is structural identity, not
mathematical equivalence; absent notation/render/Outputs evidence is explicit;
partial and degraded states remain visible; registry IDs and legacy status are
not collapsed; native OMML remains authoritative; the adapter is read-only.

## Dependency-aware sprint items

### MDE-INT-01 — Freeze the cross-package analysis-receipt contract

**Depends on:** none. **Priority:** blocker.

Define field schema, status mapping, deterministic identity, partial/degraded
semantics, relation vocabulary, and portable/redacted locator policy.

**Acceptance:** deterministic fixture receipts round-trip through JSON/XML,
reject source-hash mismatches, preserve unknown fields, and distinguish absent
from partial analysis.

### MDE-INT-02 — Build the Docs-side read-only analysis adapter

**Depends on:** MDE-INT-01. **Pointers:** `equation_graph.py:343`,
`server.py:3020`, `document_workspace.py:105`.

Return the receipt shape from existing graph/audit results without changing
their contracts, mutating DOCX/sidecars, or invoking a renderer implicitly.

**Acceptance:** identical DOCX bytes/options produce identical analysis content;
changed bytes produce stale/blocked binding.

### MDE-INT-03 — Bridge Outputs registry into typed evidence

**Depends on:** MDE-INT-01. **Pointers:** `artifact_registry.py:340,534,796`,
`provenance_status.py:436,793`, `research_evidence.py:325,380,426`.

Project stable registry records and source edges into typed records/links while
preserving legacy status as supplementary evidence. Do not treat basename
matches or unresolved states as authoritative.

**Acceptance:** the confirmed probe produces one explicit envelope containing
registry identity and any separate legacy status, without contradictory silent
success.

### MDE-INT-04 — Bind render receipts to analysis receipts

**Depends on:** MDE-INT-01 and MDE-INT-02. **Pointers:** `render_gate.py:1195,1339`.

Add optional analysis binding and freshness checks. Do not rerender implicitly.

### MDE-INT-05 — Add a package-valid stress DOCX

**Depends on:** MDE-INT-01; parallel with MDE-INT-02/03 after the contract is
frozen. **Pointers:** `test_equation_graph_stress.py:15,63,119,159`.

Retain the fast one-part XML fixture and add a minimal package-valid DOCX with
mixed equation placements, fields/bookmarks, and representative malformed OMML.
Exercise graph, integrity, notation, registry, envelope, and receipt binding.

**Acceptance:** package-valid fixture passes integrity checks; both fixtures
agree on graph semantics; all tests prove source bytes remain unchanged.

### MDE-INT-06 — End-to-end read-only promotion gate

**Depends on:** MDE-INT-02 through MDE-INT-05. **Pointers:**
`render_gate.py:1471,1647` and existing Docs/Outputs gate code.

Report graph, notation, Outputs, workspace, and render states separately with
machine-readable failure reasons. Never repair or promote a DOCX.

## Parallel execution waves

```text
Wave 0: MDE-INT-01 contract freeze
   ├─ Wave 1A: MDE-INT-02 Docs analysis adapter
   ├─ Wave 1B: MDE-INT-03 Outputs evidence bridge
   └─ Wave 1C: MDE-INT-05 package-valid stress fixture
          └─ Wave 2: MDE-INT-04 render/workspace binding
                 └─ Wave 3: MDE-INT-06 executor gate and handoff
```

## Non-goals and review decision

No automatic notation renaming, LaTeX-first authority, implicit rendering,
mandatory cross-package dependency, Outputs database migration, DOCX sidecar
schema migration, or canonical/user document access is included.

The next executor must approve one identity decision: use the stable registry
artifact ID as durable identity with the DOCX hash as current-content evidence,
or use the DOCX hash as primary with the registry ID as a linked identity. Do
not implement the bridge until that choice is explicit.
