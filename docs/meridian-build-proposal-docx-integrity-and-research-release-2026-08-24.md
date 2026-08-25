# Meridian build proposal: research-grade DOCX integrity and release workflow

**Date:** 2026-08-24  
**Scope:** Meridian Docs, Meridian Outputs, core handoff/capability contracts, and the staged JCSHM manuscript workflow  
**Safety boundary:** investigation only. The canonical dissertation was read as a reference and was not edited.

## Executive decision

Meridian has enough working DOCX primitives to support this workflow, but it does
not yet have a safe, single-command research-document release transaction. The
missing layer must bind the source material, generator scripts, parameters,
document anchors, embedded artifacts, equation structures, provenance, and visual
render receipt before a draft can be promoted.

The immediate incident demonstrates why this is needed. The current staged
JCSHM manuscript contains equation corruption that ordinary paragraph extraction
cannot reliably distinguish from legitimate inline math:

1. In JCSHM section **2.2.2**, four equations remain as OMML, but their formula
   strings have also been written into ordinary Word text runs. Word therefore
   has both the visible prose/text copy and the math copy in the same paragraph.
2. In JCSHM section **3.2.2**, the intended five separate inline math objects
   have been collapsed into one OMML object whose flattened content is
   `ffbffbrminαscalermin`; the same flattened tail also appears in ordinary
   text runs.
3. In JCSHM section **3.3.3**, `R(p)` is duplicated in ordinary text and in
   OMML after the colon introducing the traversal-cost equation.

These are not merely style warnings. They are loss-of-structure defects. A
future repair must be generated in a staged candidate, compared structurally
against the reference, rendered, and only then offered for approval.

## Two independent workstreams

This document deliberately separates the immediate manuscript work from the
Meridian product work.

### Track A — dissertation/JCSHM candidate work

This track concerns only the staged manuscript and supplementary-information
documents. It can be completed with one-off scripts or existing Meridian
primitives; it does not wait for the full Meridian build proposed below.

Track A includes:

- repair and visually verify the three confirmed equation incidents;
- scan all remaining equations for duplicate plaintext, missing OMML, merged
  structures, and numbering-policy violations;
- decide and fix the manuscript equation-number sequence;
- reconcile the 45 orphan-image and 3 duplicate-paragraph-ID findings;
- convert or deliberately retain the ten plaintext table captions according to
  the JCSHM/Springer submission policy;
- verify every figure, caption, table, cross-reference, bibliography entry,
  heading, TOC entry, and supplementary-information link;
- perform strict Word visual QA on the actual candidate and supplementary file;
- produce an advisor-review draft plus a disposition log for every unresolved
  warning.

Track A must not edit the canonical dissertation reference. Its safe working
outputs are isolated candidate files, repair manifests, rendered previews, and
review reports.

### Track B — Meridian product/build work

This track concerns reusable product capabilities: equation-integrity auditing,
artifact provenance, staged document changes, release gates, render receipts,
and documentation synchronization. It should be tested with the manuscript as
one fixture, but it is not the manuscript’s repair plan and does not authorize
editing either Word document.

The standalone product-only version is maintained at
`docs/meridian-build-proposal-track-b-2026-08-24.md`. The present document keeps
the evidence boundary and the Track A/Track B separation; the standalone file is
the canonical place for Meridian build planning.

The rest of this proposal labels product requirements as `MDE-*` and manuscript
actions as `DISS-*` so the two backlogs cannot be conflated.

## Evidence and reproducibility

The live `meridian-docs` MCP surface was callable and returned results for the
following read-only operations:

- `read_document_snapshot`
- `document_outline`
- `locate_anchors` / stable section and paragraph resolution
- `extract_equations`
- `audit_equation_style`
- `audit_document`
- `get_document_review`

The saved-byte fingerprints from that run were:

| File | SHA-256 | OMML equation records | Relevant result |
|---|---|---:|---|
| Canonical dissertation reference | `4cfda7e4faa20755d63cb6186e4071b70ebe081db92b2eec7e4d3f1c6b19d4e0` | 408 | Reference structure retained |
| Staged JCSHM manuscript v12 | `4074c54ddf139c31330ed9b667bd8fdf920d978860db67d3d20f8947c8730746` | 151 | Candidate has the corruption findings below |

The canonical dissertation remains at its private location outside this repo
(identified by SHA-256 above, not by path).

The candidate examined was a staged manuscript revision in the external
research workspace's staging area (identified by SHA-256 above, not by path).

### Equation incident details

#### 2.2.2 — duplicate plain text alongside intact OMML

Meridian resolved the candidate paragraph to section `2.2.2`, stable paragraph
ID `56387FCD`, document order 68. It contains four `m:oMath` objects:

```text
RS=s∈SBs,rs
P(li,f(li)
wi=1-A(RS\Pli,fliARS
On2
```

The candidate also contains those formula strings in `w:t` runs. The canonical
counterpart, paragraph `6BA76536` in the dissertation, has the same four OMML
objects but does not contain those formula strings in ordinary `w:t` text. The
correct repair classification is therefore **remove the duplicate plaintext
runs while preserving the reference OMML objects**, subject to visual QA.

The `extract_equations` flat text is not by itself evidence that an equation is
plaintext: it deliberately flattens OMML for indexing. The additional raw-XML
check is what exposed the duplicate `w:t` runs.

#### 3.2.2 — merged OMML plus duplicate plaintext

Meridian resolved the candidate to section `3.2.2`, stable paragraph ID
`3165EFB8`, document order 164. The final sentence ends with:

```text
... bound below by rmin and above by h:ffbffbrminαscalermin
```

Raw OOXML shows one `m:oMath` object containing all five flattened tokens:

```text
ffbffbrminαscalermin
```

The candidate's ordinary `w:t` content contains the same tail. The canonical
counterpart, paragraph `42CFEFC0` in dissertation section `8.2.2.2`, ends at
the colon and contains five separate OMML objects: `ffb`, `ffb`, `rmin`,
`αscale`, and `rmin`. This is a **structure-reconstruction repair**, not a
plain-text deletion alone.

#### 3.3.3 — confirmed third duplicate

Meridian resolved the candidate traversal-cost paragraph to section `3.3.3`,
stable paragraph ID `43367118`, document order 255. Its XML has:

```text
w:t:  ... corresponding traversal cost is:R(p)
m:t:  R(p)
```

The canonical counterpart, dissertation paragraph `7A236C42` in section
`8.3.3`, has the ordinary text ending after the colon and the equation in
OMML. This is another **duplicate plaintext alongside OMML** defect.

### Broader candidate review

The current candidate review returned:

- `get_document_review`: 43 findings total;
  - 33 equation-number-gap errors;
  - 10 legacy plaintext table-caption warnings.
- `audit_equation_style`: 151 equation records and 33 numbering-gap findings.
- `audit_document`: status `ok`, but 48 ownership findings;
  - 45 `orphan_image` findings;
  - 3 `duplicate_para_id` findings.

The numbering-gap findings are not necessarily all equation mutilation. The
candidate contains only `(34)` and `(35)` as explicit table-numbered equation
numbers, so the current audit correctly reports missing leading integers under
its global sequence policy. The release workflow must make the numbering scope
explicit: either renumber the manuscript equations from 1, or declare and test
a valid inherited numbering policy. It must not silently accept a dissertation
sequence in a journal manuscript.

The ten caption warnings are known legacy plaintext `Table 1`–`Table 10`
captions without sequence fields. They are separate from the equation incident,
but must be included in the publication release gate. The orphan-image findings
also require a deliberate figure/caption reconciliation; they should not be
silenced by changing audit severity.

## Track B: Meridian product status

### What works now

Meridian Docs already has a useful primitive layer:

- saved-byte snapshots and source fingerprints;
- stable paragraph/heading anchor resolution;
- stale-anchor rejection;
- document outline and section-scoped reads;
- OMML extraction and targeted OMML editing primitives;
- equation alignment/punctuation/numbering checks;
- figure, caption, citation, bibliography, section, table, and media helpers;
- read-only figure/table/caption ownership auditing;
- staged section movement/copy and draft promotion;
- atomic ZIP/XML writes and rollback-oriented render gates.

Meridian Outputs also has meaningful components for file fingerprints,
generating-script hashes, provenance status, output search, typed evidence
envelopes, and JSON/XML round trips. Core Meridian now has research-graph
identity and handoff/capability-contract work that can provide the coordination
layer.

The current strengths are valuable precisely because the incident can be
diagnosed without opening or mutating the canonical dissertation. The issue is
that callers must currently know which individual tools to compose and which
checks are absent from each tool.

### What is incomplete, misleading, or not yet safe for a Meridian release

### 1. No mandatory document release transaction

`write_section`, equation writers, figure writers, table movers, provenance
helpers, and `merge_docx_draft` are separate contracts. A caller can perform a
successful file merge without having proved that every changed figure, table,
equation, caption, source file, and render receipt belongs to the same release.

Required outcome:

```text
prepare -> resolve anchors -> stage candidate -> run structural gates
        -> bind provenance -> render exact candidate -> human review
        -> promote with compare-and-swap -> persist receipt
```

### 2. Equation auditing is too shallow

`extract_equations` correctly sees OMML but intentionally flattens it. The
current `audit_equation_style` checks alignment, punctuation, and explicit
numbering, but it does not detect:

- mathematical content duplicated in ordinary `w:t` runs;
- a missing OMML object replaced by equation-like plaintext;
- several expected inline equations merged into one `m:oMath`;
- a candidate equation whose structure differs from a reference equation while
  its flattened text looks similar;
- a formula tail appended after a colon in both `w:t` and OMML.

This is the direct cause of the current incident escaping a normal style audit.

### 3. Artifact ownership is not yet a research artifact block

A figure is not only an image relationship. For publication work it is an
artifact block containing image, caption, numbering, cross-references, source
output, generating script, parameters, and render evidence. The same applies to
tables and equations. `relocate_table` intentionally moves a bare table and
does not move an adjacent caption, which is safe as a narrow primitive but
unsafe as a default publication operation.

### 4. Visual validation is not a single authoritative capability

Generic rendering may use LibreOffice while strict Word visual QA may rely on
Word COM or an external receipt. Historical receipts can match exact bytes
without proving that a fresh render succeeded. A slow or timed-out COM probe
must be reported as `degraded`/`failed`, never as `rendered`.

The release contract needs an explicit backend policy:

- `word_strict` for a Word-submission release;
- `libreoffice_conversion` for a non-Word conversion check;
- `historical_receipt_only` for evidence, never as fresh visual approval.

### 5. Provenance and completion gates can remain advisory

The fallback package and Outputs components contain much of the desired logic,
but the existing architecture documents that callers can omit the gates unless
the high-level promotion path wires them in. A release command must fail closed
when required provenance, source hashes, or render evidence are unavailable.

### 6. Artifact registry is not yet the universal source of truth

The typed evidence envelope is useful and round-trips across JSON/XML, but the
cross-envelope artifact registry/resolver is a separate capability that is not
fully built. Figure/table/equation IDs therefore need one canonical registry
with reciprocal document-to-output and output-to-document resolution.

### 7. Cold-start/search state must be explicit

Outputs search has convergence, pending, lock, and degraded states, but the
document release path must consume those states rather than interpreting a
cold zero-hit search as proof of absence. Exact output paths should be
registerable immediately for a release run.

### 8. Documentation and release metadata are out of sync

The standalone `extensions/meridian-docs/README.md` still describes the package
as a skeleton with only the original parser tools even though the live server
has a much larger surface. Repository/package version declarations also need a
single source of truth. Stale documentation is a functional risk when an agent
chooses tools from the README instead of the live manifest.

## Track B: proposed Meridian build

### MDE-DOC-1 — equation integrity auditor (P0)

Add a read-only `audit_equation_integrity` operation, implemented over raw DOCX
OOXML rather than flattened paragraph text. It should return stable records for
each paragraph/equation:

```json
{
  "document_fingerprint": "...",
  "section_path": "3.2.2",
  "para_id": "3165EFB8",
  "equation_index": 0,
  "w_text_math_overlap": true,
  "omml_count": 1,
  "omml_structure_hash": "...",
  "flat_text": "ffbffbrminαscalermin",
  "findings": ["plaintext_math_duplicate", "reference_structure_mismatch"]
}
```

Detection rules should include:

- math-bearing `w:t` runs overlapping `m:t` content in the same paragraph;
- equation-like plaintext in a paragraph with no OMML;
- math appended after a colon in both plain text and OMML;
- suspicious merged adjacent structures, using a reference/golden fixture when
  available rather than guessing from symbols alone;
- equations in tables, display equations, and inline equations as separate
  patterns;
- section-scoped numbering policy and explicit inherited-numbering state.

The auditor must never call an equation “healthy” solely because OMML exists.

### MDE-DOC-2 — reference-aware equation diff and staged repair (P0)

Add a `compare_equation_structures` operation that accepts a candidate and a
read-only reference document or a stored equation fixture. Compare:

- section/paragraph anchor;
- equation ordinal within the paragraph;
- OMML tree shape and token sequence;
- plain-text overlap;
- punctuation and display/inline placement;
- equation-number policy;
- source fingerprint.

Add a staged `repair_equation_batch` operation that writes only to an explicit
draft output. It must produce a patch manifest before writing, with each change
classified as one of:

- `remove_duplicate_plaintext`;
- `split_merged_omml`;
- `restore_missing_omml`;
- `renumber_equation`;
- `manual_review_required`.

For the current candidate, the initial proposed patch would be:

| Candidate location | Proposed operation | Authority |
|---|---|---|
| 2.2.2 / `56387FCD` | Remove duplicate ordinary-text math runs; preserve four OMML objects | Dissertation counterpart `6BA76536` plus visual check |
| 3.2.2 / `3165EFB8` | Remove plain tail and split/rebuild the merged OMML into the canonical five-object sequence | Dissertation counterpart `42CFEFC0` |
| 3.3.3 / `43367118` | Remove duplicate ordinary-text `R(p)` after the colon; preserve OMML | Dissertation counterpart `7A236C42` |

No repair should be applied automatically to the canonical dissertation or to
the current manuscript without an explicit draft path and a user approval
boundary.

### MDE-DOC-3 — artifact-block model (P0)

Define one typed model for figure, table, equation, caption, and cross-reference
blocks. Every block needs:

- stable document identity and revision;
- section/anchor identity;
- package relationship IDs where applicable;
- caption/number identity;
- source output path and content hash;
- generating script and script hash;
- parameters/schema hash;
- citations or claim links;
- render/visual-QA status;
- candidate/held/superseded/promoted lifecycle state.

Figure relocation should move image plus caption plus cross-reference metadata.
Table relocation should offer a caption-aware block mode while retaining the
existing bare-table primitive for expert callers.

### MDE-DOC-4 — document change set and release manifest (P0)

Create a first-class `DocumentChangeSet` / `ReleaseManifest` that declares all
requested operations before staging. It should include:

- base document fingerprint;
- source/reference fingerprints;
- selected section/anchor scope;
- ordered operations and expected affected identities;
- preconditions and conflict claims;
- expected package relationship changes;
- artifact/provenance bindings;
- numbering and style policies;
- render backend policy;
- human approval state;
- rollback/recovery data;
- final source and render hashes.

The promotion path should be the only path allowed to update a named release
destination. A failed gate must leave the destination byte-identical.

### MDE-DOC-5 — release gates and receipts (P0)

Compose the existing Docs, Outputs, fallback, and core contracts into one
release verifier. Required gates:

1. source fingerprint and stale-anchor check;
2. DOCX package/XML/relationship/ID integrity;
3. heading/section and TOC policy;
4. equation integrity and numbering policy;
5. caption/figure/table ownership;
6. citation and bibliography consistency;
7. artifact provenance and generator-script freshness;
8. output-index convergence or exact-path registration;
9. strict render receipt if visual validation is required;
10. explicit human approval before promotion.

The receipt must state `verified`, `held`, `stale`, `ambiguous`, `degraded`, or
`unavailable` for each gate. Unknown is not an acceptable release state.

### MDE-DOC-6 — fast batch transforms and Word-safe render barrier (P1)

For mechanical DOCX changes, use one staged ZIP/XML batch rather than repeated
Word/COM round trips. The intended sequence is:

```text
read saved bytes -> plan all XML changes -> write one draft -> structural audit
-> one strict render attempt -> page/image inspection -> approval
```

COM should be bounded, isolated, cleaned up, and treated as a renderer rather
than as the document database. A timeout must leave a typed receipt and no
orphaned Word process. Word is still the authoritative visual check for a Word
submission when available; fast XML checks do not replace that check.

### MDE-DOC-7 — research-output binding and evidence graph (P0/P1)

Connect each document artifact to Meridian Outputs and the research graph:

```text
claim/citation/run -> source data -> generator script -> output file
                  -> document artifact -> paragraph/section anchor
                  -> render receipt -> release decision
```

Use deterministic identity keys and immutable revisions. A changed source
schema, generator hash, or parameter set must create a new revision or block
promotion; it must not overwrite the old evidence record.

### MDE-DOC-8 — capability and documentation synchronization (P1)

Generate the standalone Docs README and MCP-tool catalog from the live
capability manifest, or add a tested synchronization check. The manifest should
declare each tool's read/write behavior, render dependency, strictness,
fallbacks, and whether it is safe for a release transaction. Align package
version metadata across the repository before describing a release as complete.

### MDE-DOC-9 — run isolation and cleanup (P1)

Every document run should have a run ID, explicit staging root, source list,
owner, time bounds, process-group record, cleanup receipt, and retention state.
Shared manifests should use portable relative pointers instead of machine-local
absolute paths. Canonical documents should be outside ordinary staging roots
and protected by the final compare-and-swap gate.

## Acceptance test plan

### Equation fixtures

Create golden fixtures for:

1. four intact inline OMML equations with no duplicate `w:t`;
2. the 2.2.2 duplicate-plaintext defect;
3. the 3.2.2 merged-OMML defect;
4. the 3.3.3 `R(p)` duplicate defect;
5. a legitimate prose mention of an equation variable that must not be
   flagged;
6. a missing-OMML plaintext equation;
7. a table-numbered equation sequence with a valid explicit starting number;
8. a sequence with a real duplicate/gap;
9. two equivalent OMML structures with different XML whitespace/prefixes;
10. a deliberately changed subscript/fraction/limit that must fail reference
    comparison.

Required assertions:

- the auditor catches all three confirmed current defects;
- it does not flag legitimate variable mentions as corruption;
- repaired drafts have no duplicate math in `w:t`;
- 3.2.2 has the expected five OMML objects, not one flattened object;
- the canonical dissertation's SHA-256 is unchanged before and after every
  test run;
- a stale candidate fingerprint blocks repair/promotion;
- no failed repair changes the destination bytes.

### Publication-document fixtures

Add fixtures for:

- image plus caption plus cross-reference relocation;
- table plus caption relocation;
- orphan image and duplicate paragraph ID detection;
- plaintext caption retrofit;
- bibliography synchronization with missing/stale data;
- section move with references and bookmarks;
- a candidate containing comments/notes and a staged draft;
- Word lock-file presence and unsaved-on-disk caveat;
- strict render timeout and cleanup.

### End-to-end release test

The end-to-end test should:

1. ingest the saved candidate and record its fingerprint;
2. resolve the three equation incidents;
3. generate a patch manifest without mutating the canonical file;
4. write an isolated draft;
5. run equation, caption, ownership, provenance, and numbering audits;
6. bind exact output paths and script hashes;
7. render under the declared backend policy;
8. require an explicit approval token;
9. promote with compare-and-swap;
10. verify that the promoted package hash matches the release receipt.

The failure path must prove byte-identical preservation of the destination and
must leave a recoverable staged run rather than guessing at rollback.

## Recommended implementation order

The fastest safe order is:

1. MDE-DOC-1 equation integrity auditor and fixtures;
2. MDE-DOC-2 reference-aware equation diff/staged repair;
3. MDE-DOC-3 artifact blocks and MDE-DOC-4 release manifest in parallel only
   until their shared schema is fixed;
4. MDE-DOC-5 mandatory release gates;
5. MDE-DOC-7 provenance/research-graph binding;
6. MDE-DOC-6 fast batch transforms and strict render receipt;
7. MDE-DOC-8 documentation/capability synchronization;
8. MDE-DOC-9 run isolation and cleanup hardening.

MDE-DOC-4 is the serialization point. Once its manifest schema is stable,
artifact binding, evidence envelopes, rendering, and promotion can be tested
against the same release object instead of growing parallel contracts.

## Track A: immediate dissertation/JCSHM work plan

### DISS-1 — isolated equation repair

Do not repair the manuscript by manually retyping equations in Word. First
create an isolated repair candidate from the current staged manuscript, use the
canonical dissertation only as a read-only structural reference, and produce a
three-change patch manifest for sections 2.2.2, 3.2.2, and 3.3.3. After that,
run the broader equation-integrity scan and the ownership/caption review before
deciding whether to repair numbering, captions, figures, or bibliography in
the same draft.

### DISS-2 — complete candidate audit

The present evidence is sufficient to begin the staged repair, but it is not a
publication-ready clearance. The candidate still has 33 numbering-gap errors,
10 legacy table-caption warnings, and 45 orphan-image findings. Those findings
must be resolved or explicitly dispositioned in a dissertation/JCSHM review
log. This is a manuscript deliverable, not a requirement to finish the
Meridian product build.

### DISS-3 — advisor-review package

Produce a separate advisor-review package containing the repaired manuscript,
repaired supplementary information, a before/after change manifest, rendered
page previews for every changed page, and a short list of decisions requiring
human approval. Only after that review should a final JCSHM submission package
be assembled.

## Explicit non-conflation rule

The dissertation can be repaired now using a disposable candidate and a
targeted script. Meridian’s reusable fixes can be built later and tested
against reduced fixtures derived from this incident. Conversely, building a
new Meridian auditor does not itself repair, approve, or clear the manuscript.
No `MDE-*` item should be marked complete merely because the dissertation was
manually fixed, and no `DISS-*` item should be deferred solely because a
Meridian product capability is not yet implemented.
