# Meridian Track B build proposal: research-grade document release system

**Date:** 2026-08-24  
**Scope:** Meridian Docs, Meridian Outputs, core handoff/capability contracts, and reusable research-document automation  
**Out of scope:** repairing, approving, or editing any particular dissertation or JCSHM manuscript

## Executive decision

Meridian already has useful DOCX primitives, provenance helpers, research-graph
identity, and handoff/capability work. The product gap is composition: callers
still have to manually combine document mutation, artifact ownership, provenance,
rendering, and promotion checks.

Meridian needs one mandatory, typed release transaction:

```text
source data + generator code + parameters + document anchors + DOCX package
  -> structural gates -> provenance gates -> render receipt -> approval -> promotion
```

The product must remain useful for general-purpose Word work and research-grade
document production. A particular manuscript may provide regression fixtures,
but it must not become the product contract.

## Verified current capability

The live Meridian Docs MCP surface successfully provided read-only results for:

- saved-byte snapshots and SHA-256 source fingerprints;
- document outlines and section paths;
- stable anchor resolution with freshness metadata;
- OMML equation extraction;
- equation style/numbering audits;
- figure/table/caption ownership audits;
- grouped document review findings.

Existing product primitives also cover captions, figures, equations, citations,
bibliography, section movement, tables, staged drafts, render gates, Outputs
fingerprints, generating-script hashes, typed evidence envelopes, and core
handoff/capability contracts.

These primitives are valuable but intentionally separate. The product should
make the safe composition the normal path instead of requiring every caller or
agent to remember the correct sequence.

## Product gaps

### B-1 — equation integrity is not equivalent to OMML presence

Current extraction sees OMML and flattens it for indexing. Current style auditing
checks alignment, punctuation, and explicit numbering. It does not reliably detect:

- mathematical content duplicated in ordinary `w:t` runs;
- equation-like plaintext replacing missing OMML;
- multiple expected inline equations merged into one `m:oMath`;
- structural differences hidden by similar flattened text;
- inconsistent equation-numbering scope.

This needs a raw-OOXML integrity layer plus optional reference-aware comparison.

### B-2 — no universal research artifact block

Figure, caption, table, equation, cross-reference, source output, generating
script, parameters, and render evidence are currently separate concepts. A
publication-safe artifact needs one identity and lifecycle across all of them.

Table relocation is deliberately narrow and does not necessarily move an
adjacent caption. That remains a useful low-level operation, but the product
needs a caption-aware artifact-block operation for normal research workflows.

### B-3 — no mandatory release manifest

Individual writers and `merge_docx_draft` do not, by themselves, prove that all
changed artifacts, source hashes, provenance, and render evidence belong to the
same candidate release. A typed `DocumentChangeSet` / `ReleaseManifest` is the
missing composition layer.

### B-4 — provenance and completion gates can be bypassed

Fallback and Outputs components contain much of the desired logic, but the
high-level promotion path must make required provenance, source, package, and
render checks enforcing rather than advisory.

### B-5 — visual validation needs an explicit backend contract

Generic conversion, strict Word visual QA, and validation of a historical render
receipt are different states. A COM timeout or unavailable backend must be
reported as `degraded` or `failed`, never as fresh visual success.

The contract should distinguish:

- `word_strict`;
- `libreoffice_conversion`;
- `historical_receipt_only`.

### B-6 — search/index convergence must be consumed by release logic

Cold-start, pending, lock-contention, and partial-index states must not be
interpreted as a definitive zero-hit result. Exact paths need immediate
registration for a release run.

### B-7 — artifact registry and evidence graph are not fully unified

Typed provenance envelopes are useful, but document artifacts still need one
canonical registry with deterministic IDs, immutable revisions, reciprocal
document-to-output resolution, and explicit resolver states.

### B-8 — documentation and metadata drift

The standalone Docs README describes an older skeleton surface while the live
server exposes substantially more functionality. Tool documentation, capability
manifest entries, and package version declarations need a tested synchronization
source.

### B-9 — run isolation and cleanup are not a first-class product contract

Document runs need explicit roots, owners, process groups, budgets, cleanup
receipts, retention states, and portable references. Canonical destinations must
be protected from ordinary staging and garbage collection.

## Proposed build items

### MDE-B1 — equation integrity auditor (P0)

Add a read-only `audit_equation_integrity` operation over raw DOCX OOXML. Return
stable paragraph/equation records containing section path, anchor, OMML count,
token sequence, structure hash, plain-text overlap, numbering scope, and typed
findings such as:

- `plaintext_math_duplicate`;
- `missing_omml`;
- `merged_omml_suspected`;
- `equation_number_gap`;
- `equation_number_scope_ambiguous`;
- `reference_structure_mismatch`.

Do not call an equation healthy solely because an `m:oMath` element exists.

### MDE-B2 — reference-aware equation diff and staged repair (P0)

Add `compare_equation_structures` and a draft-only `repair_equation_batch`.
Every repair must first emit a patch manifest, classify the operation, and write
only to an explicit isolated draft.

Supported operation classes:

- `remove_duplicate_plaintext`;
- `split_merged_omml`;
- `restore_missing_omml`;
- `renumber_equation`;
- `manual_review_required`.

No operation may modify a canonical source implicitly.

### MDE-B3 — typed artifact-block model (P0)

Define one identity model for figure, table, equation, caption, and
cross-reference blocks. Each block should include:

- document identity and immutable revision;
- section/anchor identity;
- package relationships;
- caption/number identity;
- source output and content hash;
- generator script and script hash;
- parameter/schema hash;
- citations/claim links;
- render status;
- candidate/held/superseded/promoted lifecycle state.

Retain narrow low-level operations, but make block-aware operations the default
for research-document workflows.

### MDE-B4 — DocumentChangeSet / ReleaseManifest (P0)

Create a release object that declares all changes and preconditions before
staging:

- base document fingerprint;
- source/reference fingerprints;
- selected scope and anchors;
- ordered operations and expected identities;
- conflict claims and stale checks;
- expected package changes;
- artifact/provenance bindings;
- numbering/style policies;
- render backend policy;
- human approval state;
- rollback/recovery data;
- final source/render hashes.

### MDE-B5 — mandatory release gates and receipts (P0)

Compose the current primitives into one prepare/verify/promote/resume flow:

1. source and stale-anchor verification;
2. package/XML/relationship/ID integrity;
3. structure, TOC, style, and numbering policy;
4. equation integrity;
5. figure/table/caption ownership;
6. citation and bibliography consistency;
7. provenance and generator freshness;
8. Outputs convergence or exact-path registration;
9. strict rendering when required;
10. explicit human approval.

Every gate must return `verified`, `held`, `stale`, `ambiguous`, `degraded`, or
`unavailable`. Unknown is not a releasable state.

Promotion must be compare-and-swap protected. A failed gate must leave the named
destination byte-identical.

### MDE-B6 — batch transforms and render barrier (P1)

Support declarative mechanical XML batches that perform all writes in one
staged package, then perform one render barrier. COM/Word should be an isolated,
bounded renderer, not the document database. Timeouts need cleanup and typed
receipts.

### MDE-B7 — research-output binding and evidence graph (P0/P1)

Bind claims, citations, runs, source data, scripts, Outputs files, document
artifacts, anchors, render receipts, and release decisions with deterministic
identity keys and immutable revisions.

Changed source schemas, generator hashes, or parameters must create a new
revision or block promotion; they must not overwrite prior evidence.

### MDE-B8 — capability/documentation synchronization (P1)

Generate the Docs README and MCP tool catalog from the live capability manifest,
or add a synchronization test. The manifest must state read/write behavior,
render dependency, strictness, fallback, and release safety for every tool.
Align package-version metadata before claiming a release.

### MDE-B9 — run lifecycle and cleanup (P1)

Give every run a root, owner, ID, budget, timestamps, process-group record,
cleanup receipt, retention state, and safe garbage-collection path. Keep
canonical files outside ordinary staging roots and use portable relative paths
in shared manifests.

## Implemented incident response: Word-safe OOXML integrity

The Word “unreadable content” incident exposed a concrete gap that should be
promoted into Track B rather than handled as a one-off manuscript repair.
Meridian could produce packages that passed ZIP/XML and LibreOffice-oriented
checks but were rejected by Word. Two independent defects were involved:

- full-document `xml.etree.ElementTree` reserialization changed the namespace
  map of otherwise valid OOXML, including legitimate high-numbered prefixes;
- generated comment parts and comment markers used unqualified attributes
  (`id`, `author`, `initials`, `date`) instead of the required WordprocessingML
  namespace-qualified attributes.

The reusable implementation now lives in
`extensions/meridian-docs/meridian_docs/ooxml_integrity.py` and is integrated
into the normal DOCX write path. It provides:

- fail-closed package validation for ZIP integrity, required parts, XML,
  relationships, content types, duplicate relationship IDs, duplicate
  paragraph IDs, and comment-namespace defects;
- namespace-preserving document XML serialization through `lxml`, with a
  guard that rejects namespace-map drift;
- normalization of legacy/unqualified Word comment attributes before an
  artifact is staged;
- heading-case auditing for the declared document convention, currently
  warning on non-title-case Heading 3 text while preserving the journal-specific
  all-caps Heading 1/2 policy and detecting hidden bold heading-like remnants;
- an explicitly optional media-pruning utility, kept separate from default
  writes because “unreferenced” media requires provenance-aware policy.

The candidate manuscript and supplementary-information outputs were rebuilt
with these safeguards and opened in Microsoft Word through the no-repair path.
The canonical dissertation remained byte-identical. The permanent Track B
follow-up is to add a real Word no-repair regression gate where Word is
available, retain the package validator as the portable gate, and require the
release receipt to record which integrity checks actually ran. A successful
ZIP/XML or LibreOffice check alone must not be presented as Word-submission
readiness.

## Acceptance tests

### Equation fixtures

The product test suite needs golden fixtures for:

- intact inline OMML with no duplicate plaintext;
- duplicate `w:t` plus OMML;
- merged OMML objects;
- missing OMML plaintext equations;
- legitimate prose variable mentions;
- valid explicit numbering scopes;
- genuine duplicate/gap numbering;
- equivalent OMML with different XML prefixes/whitespace;
- changed subscripts, fractions, limits, or operators.

The auditor must catch structural defects without flagging legitimate prose.
Staged repairs must leave the source byte-identical and produce a deterministic
patch manifest.

### Release fixtures

Add tests for:

- figure plus caption plus cross-reference movement;
- table plus caption movement;
- orphan media and duplicate IDs;
- plaintext-caption conversion;
- bibliography/citation synchronization;
- stale anchors and source changes;
- provenance drift and changed generator scripts;
- cold Outputs indexing and exact-path registration;
- strict render timeout and process cleanup;
- failed promotion preserving destination bytes.

### End-to-end release test

The end-to-end test should ingest a fixture, create a change set, stage a draft,
run all gates, bind provenance, render under the declared policy, require an
approval token, promote with compare-and-swap, and verify that the final package
hash matches the persisted receipt.

## Implementation order

1. MDE-B1 equation integrity auditor and fixtures;
2. MDE-B2 reference-aware equation diff/staged repair;
3. MDE-B3 artifact-block schema and MDE-B4 release-manifest schema;
4. MDE-B5 mandatory release gates;
5. MDE-B7 research-output/evidence binding;
6. MDE-B6 batch transforms and strict render receipt;
7. MDE-B8 documentation and capability synchronization;
8. MDE-B9 run lifecycle and cleanup.

MDE-B4 is the schema serialization point. After it is stable, all product
components should consume the same release object rather than creating parallel
promotion contracts.

## Non-goals

- automatic editing of a user's canonical Word document;
- treating a single dissertation as the product's semantic truth;
- replacing Word as the final visual authority for Word submissions;
- claiming that a successful XML mutation is equivalent to publication approval;
- silently converting degraded or historical evidence into verified evidence.
