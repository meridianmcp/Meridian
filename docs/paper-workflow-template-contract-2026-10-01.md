# Shared Paper Workflow Template and Format Adapter Contract

**Status:** design contract for the local workstation manager; this document does not claim that the paper-specific adapter API or every receipt described here is already implemented.

## 1. Purpose and boundaries

A paper project needs one shared workflow across its manuscript, references, evidence, review checkpoints, and outputs. The workflow can be projected onto a DOCX manuscript or a LaTeX project, while each format keeps its own source model, editing operations, and user experience.

The local workstation manager is the integration point. It reads the paper profile and template binding, asks the selected format adapter what it can do on this machine, and records references to native artifacts and receipts. Meridian's hosted dashboard remains the project coordination surface; the workstation manager does not reproduce it.

The contract has four boundaries:

1. **Shared workflow data** describes paper-level identity, section intent, citation policy, evidence requirements, review stages, and promotion rules.
2. **Template data** is versioned through the existing project-family/template revision and child-snapshot semantics. This contract does not create a second inheritance or revision engine.
3. **Format adapters** project the shared workflow onto native DOCX or LaTeX structures. They do not convert one manuscript format into the other, and the contract does not prescribe a unified editor.
4. **Machine-local state** holds paths, provider credentials, local Zotero keys, raw provider histories, renderer installation details, and adapter caches. It is not copied into a shared template, child override, or hosted Meridian record.

The contract is deliberately stricter about *what a receipt proves* than about any one renderer or editor. Structural validity, a successful render or compile, citation resolution, provenance resolution, visual review, and approval are separate facts. None implies the others.

## 2. Reuse template revision and override semantics

Bind a paper profile to a project template revision using the established project-family/template model:

- `template_id` identifies the stable template lineage; `revision_id` identifies one immutable, content-hashed revision.
- A paper's Meridian child project pins an adopted revision in its child snapshot. The snapshot is the audit point for the profile in force when a checkpoint was created.
- Effective shared fields are the selected template revision plus that child project's override. The child override wins for each declared field; an explicit reset retracts an inherited field. Do not silently deep-merge a field's JSON value.
- Preview an incoming revision and its conflicts before adoption. Adoption is explicit and uses the existing optimistic-concurrency and acknowledged-conflict rules. A template update never silently changes a paper's adopted revision.
- Treat a change to the template as a new immutable revision. Treat a change to the paper's override as a new override revision. Do not rewrite an earlier checkpoint's resolved profile or template binding.

The paper contract adds **namespaced profile fields**, not a new layer stack. Each independently overrideable value is a distinct profile field, so existing whole-field precedence remains unambiguous. The following names are illustrative profile keys, not a promise about model, route, or MCP tool names:

| Profile field | Shared meaning |
|---|---|
| `paper.workflow` | Ordered stage IDs, required transitions, and checkpoint policy. |
| `paper.section_plan` | Required/optional sections and stable shared section IDs. |
| `paper.reference_policy` | Citation style intent, acceptable identifiers, and unresolved-reference policy. |
| `paper.evidence_policy` | Required evidence links, output provenance rules, and source-freshness policy. |
| `paper.review_policy` | Required structural checks, render/compile policy, visual review, and approval requirements. |
| `paper.adapter.docx.*` | DOCX-specific style, field, numbering, and render requirements. |
| `paper.adapter.latex.*` | LaTeX-specific root-file, citation, macro, engine, and compile requirements. |

An adapter-specific override applies only to that adapter. For example, a DOCX style override cannot alter the LaTeX engine policy. If a format-specific field is absent, the adapter uses its explicit native default and reports that choice in the snapshot; it must not infer a value from the other format.

The manager may keep a separate **local runtime overlay** for machine-specific values such as a project folder, Overleaf connection handle, Zotero local API availability, and installed renderers. This overlay is not a third shared template-inheritance layer. It is keyed locally, is not uploaded as profile content, and cannot alter shared review or release policy without an explicit shared-profile change.

Shared profile payloads must follow the existing template validator's rule against secrets and machine-local paths. In particular, no absolute paths, access tokens, Zotero item keys, Overleaf credentials, or provider session history belong in template fields, overrides, provenance notes, or hosted records.

See [Project Family / Template Revisions](meridian-project-family-template-revisions-design.md) and [Project Family Integration Contract](meridian-project-family-integration-contract.md) for the underlying revision, snapshot, override, and conflict semantics.

## 3. Shared paper workflow model

The manager works with a logical paper project and one or more native artifacts. The exact API class names are open; the following records define the information that must cross the manager/adapter boundary.

### 3.1 Paper binding

```text
PaperBinding
  contract_version
  paper_ref                 opaque paper identity local to the manager
  meridian_project_id       optional project-family child binding
  template_id               optional shared template lineage
  adopted_revision_id       exact immutable template revision, when bound
  effective_profile_hash    hash of the resolved shared fields
```

`paper_ref` is independent of a DOCX path, a `.tex` path, or an Overleaf project. A paper can have a DOCX artifact and a LaTeX artifact during a deliberate migration or parallel submission workflow, but neither adapter manufactures the other artifact. Each artifact has its own source revision and receipts.

### 3.2 Shared section and anchor references

The shared profile may assign stable `section_id` values to logical manuscript sections, such as `intro`, `methods`, or `limitations`. Each adapter returns a mapping from a shared section ID to one or more native anchors for a particular source revision. The mapping is a projection, not a claim that DOCX and LaTeX have identical structure.

An anchor reference includes its format, native identity, containing artifact revision, and resolution state. A positional or inferred anchor is valid only for the exact source fingerprint from which it was derived. Missing, duplicate, or ambiguous native anchors stay explicit; the manager must not select the first match silently.

### 3.3 References and citations

The common layer uses a manager-owned opaque `citation_id` for a reference within a paper project. It may carry public bibliographic identity fields such as DOI, ISBN, normalized title, authors, and year. If no public identifier is available, resolution uses a metadata fingerprint that can be marked ambiguous; the fingerprint never pretends to be a verified identity.

Each adapter reports native citation occurrences separately from shared reference identity:

```text
CitationOccurrence
  citation_id              optional until resolved
  native_key               local to this artifact and source revision
  native_command_or_field   e.g. natbib command or DOCX citation field kind
  anchor_ref
  resolution               resolved | unresolved | ambiguous | stale
  resolution_basis         DOI, metadata match, local provider lookup, or none
```

Native citation keys remain native. In particular, BibTeX/BibLaTeX citekeys and Zotero field payloads are not rewritten to make the two formats look alike. Zotero item keys and the mapping from a local `citation_id` to a Zotero item stay in the local reference adapter. The manager may call the local Zotero API to fill unresolved links; resolving is an explicit, idempotent operation. An unavailable local provider leaves the occurrence unresolved rather than causing an ingest or edit to fail or inventing a match.

Citation style intent is shared as policy. Rendering and field syntax are format-specific: DOCX uses native citation fields where available, while LaTeX preserves the project's `natbib` or `biblatex` commands and bibliography style. Citation and bibliography consistency checks report keys that are missing, duplicated, stale, or unresolved; they do not silently delete or rename native entries.

### 3.4 Evidence, provenance, and output references

Evidence and generated outputs are referenced by identity and fingerprint, not copied into a shared workflow record. A paper-level evidence link can bind a claim or section anchor to a citation, source artifact, Meridian Outputs artifact, or other named provenance record.

```text
EvidenceLink
  evidence_id
  claim_or_section_ref
  source_ref                artifact, citation, decision, or output pointer
  source_fingerprint        typed hash of the exact source revision
  relation                  supports | derives_from | cites | contradicts
  resolution                resolved | unresolved | ambiguous | stale
```

```text
OutputPointer
  output_id                 existing Outputs identity when available
  local_uri                 machine-local URI; never shared profile content
  content_sha256
  generator_ref             script/tool identity and content hash, if known
  parameters_hash           hash of the generation inputs, when applicable
  provenance_status         resolved | orphaned | hash_mismatch | unresolved
```

Use the existing Outputs provenance and source-pointer records when they exist. A local path is a locator for the local manager, not a cross-machine artifact identity. A source hash must state what it hashes (source file, source manifest, package, PDF, or generated data); never serialize an unqualified `hash` field.

The manager records only the metadata needed to reconnect a checkpoint to its sources and outputs. It does not upload raw Overleaf history, Zotero keys, whole provider responses, or manuscript bytes as an implicit side effect of inspecting or resolving them.

### 3.5 Stages and immutable checkpoints

The shared profile declares an ordered workflow. An illustrative pipeline is:

```text
draft -> structural_check -> citation_and_evidence_check -> render_or_compile
      -> visual_review -> approval -> release
```

Profiles may omit or add named stages, but each stage declares its required capabilities, inputs, and completion predicate. The manager owns stage orchestration; adapters own the checks and receipts that only their native format can produce. A stage does not complete merely because its adapter returned without throwing an error.

Every checkpoint binds all of the following:

- the selected `template_id`, `adopted_revision_id`, and `effective_profile_hash`;
- the adapter ID/version and native artifact revision;
- an exact source-manifest fingerprint (the root source plus relevant included files and bibliography for LaTeX; the package fingerprint and changed-part manifest for DOCX);
- typed gate receipts, output pointers, and unresolved/ambiguous references;
- reviewer/approver identity and explicit decision where the stage requires a person.

Use this common gate vocabulary:

| Gate state | Meaning |
|---|---|
| `pass` | The named predicate ran against the pinned inputs and passed. |
| `fail` | The named predicate ran and found a failure. |
| `unavailable` | A required checker or provider could not run; the document itself has not been judged by that check. |
| `unknown` | No matching receipt exists, or its scope cannot be established. |
| `stale` | A receipt exists but its input fingerprint, profile revision, or freshness window does not match. |
| `ambiguous` | The check found multiple plausible identities or anchors and could not choose safely. |
| `degraded` | A policy-authorized exception was accepted with a recorded actor and reason; it is not a pass. |
| `blocked` | A prerequisite or required gate prevents the next transition. |

The checkpoint stores the gate states individually. It does not collapse them into a generic `verified=true`. `unknown`, `unavailable`, `stale`, `ambiguous`, or `fail` cannot satisfy a required gate. A degraded override can advance only if the adopted profile permits that exact exception and a named actor records a reason; the checkpoint remains visibly degraded. A successful render/compile is not visual QA, and visual QA is not approval.

Keep the existing DOCX renderer's native receipt states (`rendered`, `failed`, and `unavailable-with-reason`) at the adapter boundary and map them to the common gate vocabulary without changing their meaning. Absence of a fresh, source-matching receipt maps to `unknown` or `stale`; it never maps to `pass`. Keep human visual review separate from backend conversion.

Promotion writes a new immutable artifact revision and changes the selected output pointer only after every required gate and required human approval has passed. A failed or held candidate leaves the current promoted revision intact. Any future degraded promotion must be explicit, auditable, and permitted by profile policy.

## 4. Adapter protocol consumed by the workstation manager

An adapter implements the following **contract operations**. These names describe responsibilities only; they do not reserve MCP tool names or current package APIs.

1. `describe_capabilities(context)` returns the capability manifest for this adapter and machine.
2. `inspect(binding, source_ref)` returns a revision-scoped structural projection, native anchors, citations, inputs, and source fingerprint.
3. `plan_change(binding, expected_revision, operations)` validates anchor freshness and native preconditions, then returns a reviewable candidate plan without promoting it.
4. `apply_candidate(plan)` applies one declared change set transactionally and returns a new candidate revision plus integrity receipts.
5. `verify(candidate, required_gates)` runs applicable structural, reference, evidence, provenance, sync, and render/compile checks and returns typed receipts.
6. `review(candidate, reviewer_decision)` records an explicit human review result where policy requires it. An automated adapter cannot manufacture a human decision.
7. `promote(candidate, checkpoint)` atomically selects the candidate as the current released artifact only if required gates, expected revisions, and approval all still match.
8. For provider-backed sources, `sync(candidate, expected_remote_revision)` performs a version-aware exchange and returns the provider's acknowledged revision or a typed conflict/failure.

The manager supplies expected source and profile revisions to every mutating operation. An adapter refuses stale anchors, unexpected source changes, and incompatible profile revisions rather than applying a plan against a different artifact. It returns the candidate diff and native changed-part identities before promotion.

### 4.1 Capability manifest

An adapter reports both what its implementation supports and what the current host can actually use. A capability being declared does not mean it is available now; an installed integration does not prove authorization; authorization does not prove a check succeeded.

```text
AdapterCapabilityManifest
  contract_version
  adapter_id
  adapter_version
  document_format
  observed_at
  declared_capabilities[]
    capability_id
    support                 supported | unsupported
    availability            available | not_configured | unavailable | unknown
    authorization           authorized | not_authorized | not_required | unknown
    scope                   exact operations/formats/artifact classes covered
    reason                  typed reason when not available
    evidence_ref            optional local probe or setup receipt
  limitations[]
```

Capability IDs are stable, versioned names, such as `docx.ooxml.read`, `docx.ooxml.transactional_write`, `docx.render.word_com`, `latex.source.ast_read`, `latex.overleaf.auth`, `latex.overleaf.ot_sync`, `latex.bibtex.read`, `latex.biblatex.read`, `latex.natbib.resolve`, and `latex.compile`. Implementations may add IDs without changing existing meanings. Unknown capability IDs must be preserved as unknown by the manager; a client must not interpret an unrecognized ID as available.

The manifest reports requirements and observed availability; it contains no credential, Zotero key, local path, raw provider response, or manuscript content. A local adapter may retain those values in its protected store and return a non-secret connection alias and a typed authorization result.

## 5. DOCX adapter projection

DOCX remains an OOXML package. The adapter projects the shared workflow onto Word structures and preserves native behavior that cannot be represented by flattened text.

### 5.1 Structure and identity

- Read headings, paragraphs, tables, equations, figures, captions, fields, styles, numbering, media, and package relationships from the DOCX structure, using the existing Meridian Docs parser/index surfaces where available.
- Prefer an unambiguous native `w14:paraId` for paragraph-owned anchors. Reuse the existing `p{index}` synthesized fallback only when native identity is absent or ambiguous, and mark it positional and snapshot-scoped. Runs have no durable OOXML ID; any run-level position is valid only for the exact document revision.
- Preserve package-part and relationship identity when moving or editing media and structured blocks. A valid ZIP/XML package alone does not prove that styles, numbering, fields, relationships, cross-references, or content are correct.
- Preserve Word field instructions and cached results. A Zotero/CSL citation field is not ordinary text and must not be replaced by a visible string as part of a routine profile or style projection.

The detailed OOXML identity proposal in [B67-3](meridian-build-b67-3-ooxml-omml-document-graph-2026-08-25.md) is planning-only in parts. This adapter contract reuses its identity and render invariants; it does not assert that the proposed `ooxml_graph` persistence layer has shipped.

### 5.2 Write transaction and render gate

A DOCX change set declares its base package fingerprint, selected anchors, ordered operations, expected changed parts, protected structures, relationship changes, and provenance bindings before writing. The adapter stages a candidate beside the destination, reopens the staged bytes from disk, validates package integrity and protected structure, and promotes through the existing serialized/atomic DOCX transaction only after those checks pass. A failure discards the candidate and leaves the prior destination intact.

The local transaction's structural manifest and atomic replace do not by themselves produce a durable render receipt or human visual review. Render capability and render result are separate checks. If the adopted profile requires a particular renderer or visual QA, unavailable, failed, unknown, or stale evidence holds promotion unless the profile explicitly permits a recorded degraded exception. `rendered` says that the backend produced output; `visual_qa=not_reviewed` is not a reviewed pass.

The adapter reports field/style/relationship preservation and changed package parts in its receipt. It keeps the source artifact, candidate artifact, and promoted artifact distinct until the promotion decision is made. It links images, tables, figures, equations, and citations to their source/evidence references without embedding a duplicate copy of the research/output registry in the DOCX workflow profile.

## 6. LaTeX and Overleaf adapter projection

LaTeX remains a source tree plus its build configuration and generated artifacts. The adapter projects the shared section plan and citation/evidence policy onto TeX source, bibliography files, labels, macros, and compiler outputs; it does not convert the source tree to DOCX.

### 6.1 Source tree, AST, and macro identity

- Inspect a declared root `.tex` and its reachable `\input` / `\include` files. Return the root and complete resolved-source manifest, and report unresolved includes, external macro files, class files, and generated dependencies.
- Use a LaTeX-aware parse tree for sections, environments, labels, citations, and macro definitions. Preserve the source spelling and macro expansion context; do not reduce editable structure to a rendered outline or plain text.
- Prefer a stable explicit `\label{...}` for a section, figure, table, or equation when it is unique in the source closure. For unlabeled nodes, return a revision-scoped anchor from file identity, AST path/context, and source-manifest fingerprint. Offsets and line numbers are navigation hints, not durable identities. Duplicate labels or unresolved macro expansion are `ambiguous`/`unknown`, not silently matched.
- Keep source macros and their definitions versioned as native source. Macro expansion is an inspection aid; an edit to a macro must declare all affected use sites and cannot silently replace expanded text at each use.

The shipped `latex_intel` parser can return an ordered outline, expand resolvable includes and section aliases for that outline, extract citation markers, and parse BibTeX/BibLaTeX entries. These are useful projections, not a promise of a complete edit-safe source AST or stable macro identity. A manifest must advertise only the parser and edit capabilities actually available on the current machine.

### 6.2 Overleaf authentication and operational transform sync

Overleaf is a provider transport for the native LaTeX source; it is not a second workflow authority. The Overleaf adapter owns login, authorization, project selection, remote version tracking, OT/session coordination, retry, and conflict reporting. It uses a local protected credential store. Credentials, provider project handles, socket payloads, OT histories, and raw provider responses stay local.

Every sync plan is based on a known remote revision and the local source-manifest fingerprint. The adapter must:

1. record the remote base revision and exact local base fingerprint before applying edits;
2. submit OT operations against that base and retain the provider's acknowledgment/rebased revision;
3. detect remote changes that cannot be safely rebased, duplicate file names, deleted inputs, and provider authorization loss;
4. return a typed conflict or unavailable receipt without overwriting either side when it cannot prove the rebase;
5. re-read the acknowledged source closure and compare its manifest before claiming that local and remote state are synchronized.

An HTTP success, live connection, or successful login is not evidence that a particular edit reached Overleaf. A sync receipt names the before/after remote revisions, source manifest hashes, operation set, acknowledgment, and any conflict resolution. Manual conflict resolution is an explicit edit on a new revision.

The current LaTeX package includes Overleaf login/status/logout helpers. This contract does not assert that its auth helper currently implements OT or the version-aware sync sequence above; those are adapter responsibilities to expose and verify before the manager advertises `latex.overleaf.ot_sync` as available.

### 6.3 BibTeX, BibLaTeX, natbib, and compiler receipts

- Preserve each project's own bibliography engine, `.bib` source, bibliography style, citation keys, and citation commands. `natbib` commands and `biblatex` commands are not normalized by rewriting manuscript text.
- Extract citation occurrences and bibliography entries, then map native keys to the local `citation_id` registry. Report citekeys with no entry, entries with no occurrence, duplicate keys, unsupported commands, and unresolved `\input`/bibliography dependencies. An unavailable local Zotero resolver leaves those mappings unresolved.
- A compile receipt binds the exact root `.tex`, all resolved included files, bibliography files, class/style files when their hashes are available, macro definitions, compiler/engine and version, flags, environment, exit status, and relevant log/output hashes. It records warnings/errors separately from compile status and links the produced PDF/log through local output pointers.
- A successful compile proves only that the selected compiler returned its reported result for the captured input closure. It does not prove citation correctness, visual quality, review, approval, or freshness after any source or dependency changes.

## 7. Manager integration and event flow

The local workstation manager is a thin workflow coordinator:

1. Resolve the selected paper profile and exact adopted template revision. Show pending template revisions for explicit preview/adoption; never update a paper's binding as a side effect of opening a document.
2. Discover DOCX and LaTeX adapter manifests locally. Present supported capabilities, present-machine availability, authorization, and limitations as separate facts.
3. Ask the chosen native adapter for a revision-scoped projection. Keep DOCX anchors and LaTeX source anchors in adapter-owned maps to the shared section/citation/evidence identities.
4. Create a reviewable candidate change set with expected source/profile revisions. The adapter performs native writes and returns a candidate receipt; the manager keeps the current promoted artifact unchanged until gates finish.
5. Resolve citations or provenance through their declared local tools and persist only stable shared identities, typed results, and fingerprints required by the checkpoint.
6. Run the stages declared in the shared profile. Store each receipt against its exact input hashes. Obtain explicit review/approval where required.
7. Promote the output pointer only after the configured gates pass or a specifically permitted degraded decision is recorded.

The adapter manager contract may be represented by this logical exchange; these property names are illustrative until an implementation item chooses an API surface:

```text
manager -> adapter: binding, profile_revision, expected_source_revision,
                    requested_operation, required_capabilities
adapter -> manager: capability_manifest, source_projection,
                    candidate_revision, diff, typed_receipts,
                    unresolved_items, local_output_pointers
manager -> adapter: explicit review decision and promote request
adapter -> manager: promoted_revision or typed held/conflict result
```

The hosted project can retain the shared template/profile revision and concise checkpoint/provenance references when authorized. Local paths, credential aliases, provider handles, Zotero keys, raw provider histories, and unredacted logs remain on the workstation. Access-denied, offline, or partial-hosted states must not be mistaken for an empty project or a successful sync.

## 8. Failure, conflict, and recovery rules

- **Stale template or source revision:** refuse the write or promotion and return the expected/current revision identifiers. Re-preview or re-plan against the current state.
- **Ambiguous native anchor or citation identity:** hold the affected change and return the candidates; require explicit mapping. Never use first-match behavior.
- **Required capability unavailable:** mark the relevant gate unavailable and hold. Optional capability absence is reported with reason and does not become a positive receipt.
- **DOCX package or protected structure failure:** discard the staged package and keep the previous file. Render or provenance failure follows the profile's gate policy; do not report a render as successful because the structural write passed.
- **LaTeX parse/include uncertainty:** preserve source; mark affected anchors and checks unknown or ambiguous. Do not apply a source edit through a stale or unresolved macro expansion.
- **Overleaf OT conflict or auth loss:** retain the local candidate and the last acknowledged remote base. Do not retry against a new remote revision without rebuilding/reviewing the operation set.
- **Citation or source-provider unavailable:** retain unresolved links and receipts. Do not infer a missing reference from an incomplete provider query.
- **Checkpoint restore:** restore only from an artifact revision whose content hash and source closure verify. Restoring a file does not roll back the adopted template, reference metadata, or an external provider; those are separately versioned and require their own explicit operation.

## 9. Acceptance criteria for implementations of this contract

An implementation conforms when it demonstrates all of the following:

1. A DOCX-bound and a LaTeX-bound paper can resolve the same shared profile revision while retaining separate native artifact revisions and editors.
2. Template update preview, child override precedence/reset, explicit adoption, and stale-write rejection follow the existing project-family/template semantics. Earlier checkpoints remain pinned to their original effective profile hash.
3. A DOCX structural projection preserves stable native anchors when available and marks positional/ambiguous anchors with their revision scope.
4. A LaTeX projection reports root/include closure, native labels and citation commands, and unresolved/ambiguous macros or includes without presenting an outline parser as a complete safe editing AST.
5. References map to manager-owned citation IDs without persisting Zotero keys to shared state; local resolution is idempotent and exposes unresolved, ambiguous, and stale results.
6. DOCX writes are staged, reopened, checked, and promoted transactionally; a failed candidate leaves the prior artifact unchanged.
7. Overleaf auth and sync advertise separate capabilities. Sync is versioned, acknowledged, conflict-aware, and cannot claim success from login or transport status alone.
8. Compile/render receipts bind exact inputs and backend; a missing or stale receipt is unknown/stale; conversion or compilation never implies visual review or human approval.
9. Checkpoints carry the exact template/profile and artifact fingerprints, typed gate results, evidence/output pointers, and required reviewer decision.
10. No common-layer write converts formats, embeds secrets or local paths in shared profile data, or creates a second hosted dashboard.

## 10. Open implementation choices

This contract intentionally leaves the following implementation decisions open:

- exact Python/TypeScript types, RPC/tool names, storage tables, and local manager event schema;
- the canonical citation-ID creation and duplicate-resolution policy for references without DOI/ISBN;
- durable checkpoint storage and reconciliation when a workstation is offline;
- the AST library and stable-anchor algorithm for arbitrary LaTeX macros and generated source;
- the Overleaf API/OT client, retry budget, remote revision token, and conflict UI;
- renderer/compiler allowlists, freshness windows, and the set of profile-controlled degraded overrides;
- whether/when the proposed DOCX structural graph and durable render ledger are adopted by the production Docs path.

An implementation must resolve these choices in a focused design or code change, measure real capability availability on each host, and update this contract when its behavior changes. A proposal or a parser's partial output is not evidence that the corresponding capability has shipped.

## References

- [Project Family / Template Revisions](meridian-project-family-template-revisions-design.md)
- [Project Family Integration Contract](meridian-project-family-integration-contract.md)
- [B67-3: OOXML/OMML document graph](meridian-build-b67-3-ooxml-omml-document-graph-2026-08-25.md)
- [B67-5: DOCX capability matrix and render truth table](meridian-build-b67-5-capability-matrix-and-render-truth-table-2026-08-25.md)
- [DOCX integrity and research release proposal](meridian-build-proposal-docx-integrity-and-research-release-2026-08-24.md)
- `meridian/db/profile_layers.py` — canonical scoped profile layer behavior.
- `meridian/doc_store.py` — DOCX structure and transactional write behavior.
- `extensions/meridian-docs/meridian_docs/docs_intel.py` — DOCX native operations and render enforcement.
- `packages/docparse/docparse/latex_intel.py` — current LaTeX outline, citation, and BibTeX/BibLaTeX projections.
- `extensions/meridian-latex/engine/src/overleaf-login.ts` — current Overleaf login/status/logout helpers.
