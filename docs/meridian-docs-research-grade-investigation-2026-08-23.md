# Meridian Docs research-grade extensibility investigation

Date: 2026-08-23  
Status: investigation complete; implementation sprint proposed  
Version policy: no version increase was made or authorized

## Scope and method

This report operationalizes the local investigation brief at
`C:\Users\13144\Documents\Masters_Thesis\CURRENT_PROJECT_CODE\meridian_docs_investigation_megaprompt.md`.
It evaluates Meridian Docs, Meridian Outputs, the local fallback package, the
research/thesis pipeline, and the handoff/tooling boundary. The brief was
treated as investigation criteria, not as executable instructions.

Evidence was separated into verified facts, inferences, open questions, and
proposed work. No canonical DOCX, OneDrive file, `.env`, `meridian.toml`, or
release version was modified. The only durable artifact from this pass is
this report under the repository `docs/` directory; temporary/prestage work
remains on local disk.

## Executive conclusion

Meridian Docs is not merely a collection of isolated DOCX tools. The current
implementation already contains meaningful safety primitives: byte-level
source fingerprints, stable paragraph identities, stale-anchor rejection,
atomic staged ZIP/XML writes, per-destination promotion locks, compare-and-
swap checks, package manifests, structural verification, tri-state render
gates, and composed operations such as figure-plus-caption insertion and
section movement.

The central gap is a missing mandatory composition layer. The safety pieces
are distributed across `meridian-docs`, `meridian-outputs`, the core Meridian
merge/claim layer, and `tools/meridian_fallbacks/`. A caller can still combine
them inconsistently, and some high-level operations explicitly rely on a
separate core manifest/claim gate. Research outputs therefore need a single
release transaction that binds:

```text
source data + generator code + parameters + document anchors + DOCX package
  -> structural checks -> provenance checks -> render receipt -> promotion
```

The release transaction must preserve the complete typed evidence graph and
emit Markdown only as a human-readable projection. Markdown notes remain
useful for narrative context, but must not be the canonical provenance store.

## Verified findings

### 1. The Docs MCP is live and useful, but its layers are separate

The live `meridian-docs` server successfully parsed the Missouri S&T thesis
template into 265 paragraph records. The live read-only ownership audit also
ran successfully against the disposable staged candidate
`outputs/shadow/wave36_hampel_remove/candidate_no_hampel.docx`.

The current server exposes distinct surfaces for:

- document parsing/outline/indexing and structural freshness;
- exact anchor lookup using stable IDs and source fingerprints;
- structural audit of headings, paragraphs, captions, tables, media, and
  relationships;
- figure, caption, equation, citation, bibliography, section, and table
  primitives;
- staged section movement/copy and draft promotion;
- render-capability detection.

The separation is healthy at the primitive level, but the normal user path
still requires callers to know which combination is safe.

Pointers:

- `extensions/meridian-docs/meridian_docs/server.py:229` — saved-byte snapshot
  and source fingerprint boundary.
- `extensions/meridian-docs/meridian_docs/server.py:337` — read-only ownership
  audit wrapper.
- `extensions/meridian-docs/meridian_docs/server.py:403` — flat local ingest.
- `extensions/meridian-docs/meridian_docs/server.py:453` — separate structural
  ingest path.
- `extensions/meridian-docs/meridian_docs/server.py:547` — atomic figure plus
  caption operation.
- `extensions/meridian-docs/meridian_docs/server.py:1927` — atomic structured
  section write.
- `extensions/meridian-docs/meridian_docs/server.py:1990` — section move with
  draft/wave hooks.
- `extensions/meridian-docs/meridian_docs/server.py:2509` — physical draft
  promotion; ownership/overlap/staleness are explicitly delegated elsewhere.

### 2. The high-level composition gap is real

`write_section` validates and inserts a complete section specification, but its
own contract says actual image/table insertion is not part of that operation.
`merge_docx_draft` performs physical promotion and verification but does not
enforce ownership, overlap, or staleness by itself. Those facts are not bugs in
the individual functions; they are evidence that a first-class release
orchestrator is missing.

The semantic model is also not yet unified: paragraph identity, headings,
captions, media, equations, tables, and sidecars are separate structures.
`relocate_table` moves the bare table and deliberately does not move an
adjacent caption or renumber it. That is a safe narrow primitive, but it is not
the research-level artifact block abstraction needed for a table-plus-caption-
plus-source binding.

The intended target is a typed `DocumentChangeSet`/`ReleaseManifest` that
declares all operations, preconditions, affected anchors, source bindings,
expected package changes, and required evidence before any candidate is
promoted.

### 3. The local fallback package already contains much of the target contract

`tools/meridian_fallbacks/` is a tracked, tested package with a capability
manifest at version 1.10.0. It already includes transactional merge helpers,
output provenance gates, DOCX completion gates, artifact-reference lifecycle
records, reference-binding snapshots, pre-stage registries, and a staging-lint
CLI.

Its documented limitations are decisive:

- provenance and DOCX completion gates are advisory unless a caller wires them
  into the promotion path;
- the fallback package does not itself perform a real Word/COM render check;
- render receipt validation accepts receipts produced by another renderer;
- the package has repeatedly found “real, tested code not registered in the
  manifest” gaps, so capability registration is part of the acceptance bar.

Pointers:

- `C:\Users\13144\Documents\Masters_Thesis\CURRENT_PROJECT_CODE\tools\meridian_fallbacks\MERIDIAN_INTEGRATION_NOTE.md:98`
  — known gaps.
- `C:\Users\13144\Documents\Masters_Thesis\CURRENT_PROJECT_CODE\tools\meridian_fallbacks\capability_manifest.json`
  — authoritative module/capability registration and limitations.
- `tools/meridian_fallbacks/transactional_merge.py`
- `tools/meridian_fallbacks/output_provenance_gate.py`
- `tools/meridian_fallbacks/docx_completion_gate.py`
- `tools/meridian_fallbacks/artifact_reference_registry.py`
- `tools/meridian_fallbacks/reference_binding_gate.py`
- `tools/meridian_fallbacks/staging_lint_cli.py`

### 4. Structural safety is materially covered; fresh visual evidence is not

Focused Meridian Docs tests passed:

```text
181 passed
```

The suite covered atomic figure/caption writes, rollback, stale caches,
concurrent-write envelopes, render-gate states, draft merge, provenance
binding, and table structural edits.

Focused Meridian Outputs/provenance tests passed:

```text
88 passed
```

The fallback package tests passed:

```text
213 passed
```

These are strong primitive-level signals, not proof that the cross-package
release transaction is complete.

On the disposable candidate, the read-only Docs audit returned `status=ok`
and a source SHA-256, but it also found 41 `orphan_image` findings. That is a
useful result: the audit is detecting structural ownership problems instead of
silently declaring the package ready.

The candidate also has an older Word-COM receipt whose source hash exactly
matches the current DOCX bytes:

```text
DOCX SHA-256: 52f29328e798bedf20b5678ba6f071fb2b3852d8006859a39ffa5d28ad8ae886
PDF  SHA-256: 3f8153ed72000cbc74cbdc1e82b9575f523506d9e408c38bc83cf29089b4e023
```

A fresh `check_render_capability` call against the same disposable candidate
timed out after 60 seconds. No `WINWORD.EXE` process remained afterward.
Therefore the correct state is “historical receipt matches exact bytes; fresh
visual capability currently degraded,” not “rendering is working.”

The in-process render probe writes into a temporary directory, so its generated
PDF is not itself a durable release artifact. Durable receipts must therefore
be produced by an explicit release/render command that persists the exact
source hash, PDF hash, renderer identity/version, page count, and failure
classification.

The generic render gate currently tries backends in the order
LibreOffice/soffice, then Word COM. The MCP surface accepts only `docx_path`,
so callers cannot request strict Word rendering or provide a backend policy.
Consequently a generic `rendered` result can mean successful LibreOffice PDF
conversion rather than Word-compatible visual QA. The dedicated
`check_word_com_render_receipt` surface is stricter, but it validates an
external receipt rather than persisting one during a Docs promotion.

Pointers:

- `extensions/meridian-docs/meridian_docs/server.py:372` — render capability
  boundary.
- `extensions/meridian-docs/meridian_docs/render_gate.py` — tri-state render
  contract.
- `outputs/shadow/wave36_hampel_remove/word_render_v1/candidate_no_hampel.receipt.json`
  — source-hash-pinned historical receipt.
- `extensions/meridian-docs/tests/test_docx_render_gate.py`
- `extensions/meridian-docs/tests/test_docx_word_com_regression.py`

### 5. Outputs convergence is fail-closed in code, but the real local index is cold

The live `meridian-outputs` convergence snapshot for
`C:\Users\13144\Documents\Masters_Thesis\CURRENT_PROJECT_CODE\outputs` returned:

```json
{
  "converged": false,
  "walk_complete": true,
  "indexed_count": 0,
  "expected_count": null,
  "never_walked": true,
  "pending_count": 0,
  "last_error": null
}
```

That is the correct conservative state. It is not evidence that the tree is
empty. `get_provenance_status` correctly returned `provenance_type=unknown`
plus `inconclusive=true` for the staged DOCX, receipt, and PDF because the
index has never discovered the tree.

The typed research-evidence envelope also behaved correctly: three records
were emitted as `partial=true`, each with resolver status `ambiguous`, and the
envelope round-tripped through XML without losing its canonical keys. The
current XML is a lossless projection of the typed envelope; Markdown should
remain a presentation layer only.

Pointers:

- `extensions/meridian-outputs/meridian_outputs/outputs_local.py:1823` —
  convergence state model.
- `extensions/meridian-outputs/meridian_outputs/outputs_local.py:5090` —
  read-only convergence snapshot.
- `extensions/meridian-outputs/meridian_outputs/provenance_status.py:817` —
  partial-envelope rule.
- `extensions/meridian-outputs/README.md:75` — typed evidence model and
  JSON/XML round-trip contract.

### 6. BM25 is implemented, but environment and warm-start verification are incomplete

`meridian-outputs` documents `search_outputs` as BM25 search and the current
implementation uses Tantivy-backed indexing with DuckDB metadata and explicit
convergence/lock state. It also has a pure/local fallback path and tests for
partial/degraded results.

The repository `pixi` environment currently reports:

```text
tantivy=True; duckdb=True; pyarrow=False; xxhash=True; portalocker=False
```

The extension declares `pyarrow` as a dependency and has graceful fallback
behavior when it is absent. This is a reproducibility issue: the MCP package
environment and the repository test environment can take different indexing
paths. The handoff must require a cold-start install check, a warm-start
convergence check, a BM25 query check, and an explicit degraded-backend
receipt.

This pass did not rebuild the 92k-file local tree or install dependencies into
the active environment, because that would be an operational mutation rather
than an evidence-only investigation.

### 7. Some freshness failures are represented as “not stale”

The paragraph and structural freshness helpers return `stale=false` with
`reason=source-unreadable` when a tracked source cannot currently be read.
That is not evidence of freshness; it is an inconclusive state and should be
mapped to `unavailable`/`ambiguous` in a release manifest. Sidecar invalidation
after writes is also best effort and currently clears paragraph-index mtime
metadata rather than committing one cross-index freshness revision.

Pointers:

- `extensions/meridian-docs/meridian_docs/docs_intel.py:917`
- `extensions/meridian-docs/meridian_docs/docs_intel.py:1495`
- `extensions/meridian-docs/meridian_docs/docs_intel.py:3401`

### 8. The code-intelligence graph has a verified trust defect and worktree noise

The graph architecture snapshot contains 126,821 nodes and 980,840 edges, but
also includes a very large `.codex/worktrees` population. Current-root searches
therefore need explicit path scoping.

More seriously, current graph metadata and current live files disagreed on
several `get_code_snippet` bodies. Examples included `ingest_local_document`,
`check_render_capability`, `merge_docx_draft`, and `move_section`: metadata
reported the correct symbol/range while the returned body was a neighboring
function or mixed source window. This independently verifies the local Wave
27 remediation finding about wrong snippet bodies.

Until fixed, code-intel output must carry a resolution receipt containing the
repository identity, canonical path, symbol, line range, source hash, and
resolution rung. A receiver must cross-check the body hash against the live
file before treating it as an implementation instruction.

### 9. Tunnel claim protection is not uniformly fail-closed

The live tunnel source explicitly documents the word-write and scoped DOCX
claim guards as fail-open when the database, target identity, or claim lookup
is unavailable. That may be a deliberate availability choice for generic
authoring, but it is not acceptable as the default for a research release
promotion. A release capability must declare whether claim evidence is
required; if required and unavailable, promotion must stop. The lower-level
writer may remain available in an explicitly degraded authoring mode, but the
receipt must say that no ownership guarantee was established.

Pointers:

- `meridian/routes/tunnel.py:4227` — word-write claim guard.
- `meridian/routes/tunnel.py:4361` — scoped Docs claim guard.
- `meridian/routes/tunnel.py:4924` — tunnel write relay guard.

### 10. The live Meridian capability profile is empty, causing a false operational block

The current project capability manifest returned an empty capability list with
hash `4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945`.
The effective profile for the directly relevant C84-W3 item was also empty.
The session start consequently reported `executable=false` because required
tool availability could not be resolved, even though the local Docs and
Outputs tools are callable in this session.

This is an infrastructure/configuration gap, not a reason to silently skip
verification. The capability manifest needs explicit entries for:

- semantic/code prospecting;
- meridian-docs structural audit and safe promotion;
- meridian-outputs provenance and convergence;
- BM25/Tantivy plus deterministic fallback;
- render receipt production/validation;
- pytest and disposable-fixture verification.

Each entry needs an availability policy, fallback chain, verification command,
and no secret or machine-local absolute path.

### 11. Provenance and research identity still lose lineage at boundaries

The typed evidence model is a strong foundation, but several surrounding
boundaries are not yet research-grade:

- `meridian-outputs.record_provenance` stores one JSON record per normalized
  path and overwrites the prior record; it is not an append-only revision log.
- the DOCX integrity provenance manifest hashes package size, counts, and XML
  part names, while the complete promoted DOCX bytes are hashed separately;
  the release manifest must carry both and never treat the structural summary
  as a full-package identity.
- output-derived envelopes contain output records but do not automatically
  create `run -> output`, `output -> document`, `claim -> evidence`, or
  `claim -> citation` edges. Missing edges must be explicit rather than
  silently inferred.
- the extension envelope currently uses the output path as the record identity,
  while core research-graph identity is designed to be stable across revisions.
  A shared identity-key rule is needed to prevent one artifact becoming two
  graph nodes merely because it crossed an MCP boundary.
- `build_zotero_citation_field` derives `citationID` from Python's randomized
  `hash(tuple(citation_keys))`, so identical inputs can get different IDs in
  different processes. Citation IDs must use a deterministic digest.

These are not arguments to discard the existing envelope. They are the reason
MDE-4/MDE-5 need immutable revisions, canonical identity keys, explicit edge
creation, and deterministic serialization as acceptance criteria.

Pointers:

- `extensions/meridian-outputs/meridian_outputs/server.py:314`
- `meridian/docx_integrity_gate.py:301`
- `meridian/research_graph.py:30`
- `extensions/meridian-docs/meridian_docs/docs_intel.py:6988`

## Target architecture

### Canonical typed objects

Add a single cross-package contract, implemented first in the local fallback
package and then wired through the MCP layer:

1. `DocumentSnapshot`: canonical path, source SHA-256, package-part hashes,
   stable element IDs, structure/index freshness, and read timestamp.
2. `DocumentChangeSet`: typed operations, target anchors, preconditions,
   expected source snapshot, source/output bindings, and affected package
   parts.
3. `ArtifactBinding`: figure/table/equation/caption identity, source files,
   source hashes, schema hash, generator script hash, document anchor, and
   lifecycle status.
4. `RenderReceipt`: exact DOCX hash, renderer/backend, PDF hash, page count,
   timestamp, environment identity, and failure class.
5. `ReleaseManifest`: candidate identity, all gate results, unresolved items,
   provenance envelope, render receipt, promotion decision, and recovery data.

The C84-W3 audit narrows the P0 risk further. The individual filesystem
envelope is real: same-directory staging, fsync, structural verification,
atomic replacement, post-write re-read, and compare-and-swap restore checks
exist. The missing invariant is the cross-store state machine:

```text
PREPARED -> STAGED -> PROMOTED -> VERIFIED -> DB_COMMITTED -> RELEASED
```

Current gaps include opt-in rather than mandatory expected-base hashes on some
write paths, process-local locks that do not cover another process, fail-open
claim lookup errors, best-effort backups without pre-image verification, no
durable operation journal/scavenger, and separate file promotion versus DB
ledger/index commits. A crash after `os.replace` but before the DB record can
leave a successful file with an uncommitted merge record. A second writer can
also pass a stale pre-check in another process unless the complete package
base hash and fencing lease are required immediately before promotion.

The release orchestrator must recover by comparing the current package hash to
the recorded base and post-promotion hashes. If it matches the post hash, it
finishes the DB commit; if it matches the base, it aborts; any other hash is
`RECOVERY_REQUIRED` and must not be overwritten or auto-restored.

### One mandatory release path

The intended command/tool should be an all-in-one operation, tentatively:

```text
meridian docs prepare-release --manifest <change-set> \
  --candidate <local-draft.docx> --outputs-root <local-outputs>
```

It should:

1. snapshot exact bytes and acquire the document/run lease;
2. resolve anchors and reject stale/ambiguous targets;
3. validate source/output bindings and generator hashes;
4. apply all compatible mechanical edits in one package-level transform;
5. run package/XML/relationship/ID/field/OMML/ownership checks;
6. run convergence/provenance checks with explicit partial states;
7. produce a real render receipt or fail with a typed degraded reason;
8. emit JSON and lossless XML envelopes plus a concise Markdown report;
9. promote only when the release policy permits it; otherwise preserve the
   candidate and a recovery receipt.

The operation must support `inspect`, `prepare`, `verify`, `promote`,
`rollback`, and `resume` phases. A failed phase must not leave the canonical
DOCX half-written.

### Research-native evidence graph

Claims, citations, datasets, code, runs, outputs, figures, tables, equations,
documents, and reviews should be typed records linked by explicit edges. Each
record needs a resolver state of `verified`, `stale`, `held`, `ambiguous`,
`unavailable`, or `degraded`. Every partial record must carry a reason. XML and
JSON are equivalent serializations; Markdown is a projection.

## Proposed mega-sprint (no new version)

These are proposed items for the existing Meridian workstream. They do not
authorize a `v0.2.8` or any other version increase.

### MDE-1 — capability and execution-profile repair (P0)

Populate the project capability manifest with the required Docs, Outputs,
BM25, render, code-intel, and pytest capabilities. Add deterministic fallback
chains and a machine-readable `executable` decision to handoffs.

Acceptance: the project profile is non-empty; the relevant C84-W3 item
resolves required tools; missing render or Tantivy produces an explicit
degraded/blocked result rather than an unknown-tools block.

Pointers: capability manifest APIs; `tools/meridian_fallbacks/capability_manifest.json`;
`meridian/handoff.py`.

### MDE-2 — code-intel resolution receipt and worktree isolation (P0)

Fix wrong-body snippet resolution, scope graph queries to the active repository,
and return path/symbol/range/source-hash/project identity in every code-intel
receipt. Add short/long symbol fixtures and timeout/project-reversion tests.

Acceptance: no current-root symbol can return a neighboring body; graph and
live-file hashes agree; worktree duplicates are excluded or explicitly labeled.

Pointers: codebase-memory `search_graph` → `get_code_snippet`; Wave 27
remediation proposal; `C:\Users\13144\Documents\Masters_Thesis\CURRENT_PROJECT_CODE\wave27_meridian_build_remediation_proposal_2026-08-09.txt`.

### MDE-3 — canonical change-set/release-manifest orchestrator (P0)

Compose Docs atomic writers, core claims/leases, fallback transaction gates,
Outputs binding, and render receipts into one prepare/verify/promote/resume
contract. Make provenance and package gates enforcing at promotion time.

Acceptance: a failed gate leaves the canonical DOCX byte-identical; a stale
anchor, source hash, output hash, or package relationship fails closed; every
promotion produces JSON, XML, and Markdown projections with the same evidence;
required claim lookup failure blocks promotion instead of silently degrading;
crash recovery distinguishes base-hash, post-hash, and unknown-hash states
without guessing or restoring a stale backup.

Pointers: `server.py:547`, `server.py:1927`, `server.py:2509`,
`tools/meridian_fallbacks/transactional_merge.py`,
`tools/meridian_fallbacks/reference_binding_gate.py`.

### MDE-4 — research artifact registry and source binding (P0)

Make figure/table/equation/caption artifacts use one registry and binding model
with candidate/held/superseded/promoted states, reciprocal supersession, source
hashes, schema hashes, generator hashes, and document-anchor resolution.

Acceptance: a changed source schema or generator blocks promotion; a missing
or fallback-only provenance record is visible as partial/ambiguous; promoted
drift is an error; registry IDs resolve from the DOCX to Outputs and back;
prior provenance revisions remain queryable and do not get overwritten.

Pointers: `artifact_reference_registry.py`, `reference_binding_gate.py`,
`meridian_outputs.bind_artifact_provenance`, and the Wave 53 table contract.

### MDE-5 — lossless research-evidence XML/JSON and handoff integration (P0)

Make the typed provenance envelope the canonical interchange object. Add
schema/version validation, deterministic serialization, XML namespace/schema
documentation, round-trip fixtures, and handoff rendering that preserves
partial/degraded reasons.

Acceptance: JSON→canonical→XML→canonical is byte-stable at the semantic level;
no XML or Markdown projection drops hashes, links, resolver states, or partial
reasons; the handoff includes an evidence summary and machine-readable status;
claim/citation/run/document edges are explicit; citation IDs and node IDs are
deterministic across processes.

Pointers: `extensions/meridian-outputs/meridian_outputs/research_evidence.py`,
`provenance_status.py`, `serialize_provenance_envelope`,
`parse_provenance_envelope`.

### MDE-6 — BM25/Tantivy cold-start and fallback hardening (P1)

Make BM25 installation and warm-start behavior explicit. Verify Tantivy,
DuckDB, Arrow, hashing, and lock backends in the MCP runtime; expose the
selected backend and degraded reason; guarantee deterministic fallback search
when Tantivy/Arrow/portalocker is unavailable; persist/resume convergence state.

Acceptance: a cold local Outputs tree cannot claim converged; a known path can
be registered and searched immediately; a cold-start/restart/lock-contention
test proves no false zero-hit result; BM25 results carry convergence and
backend metadata.

Pointers: `extensions/meridian-outputs/README.md`,
`meridian_outputs/outputs_local.py:2072`, `:5090`, `:5332`,
`tests/test_degraded_labeling.py`.

### MDE-7 — render receipt and visual-QA capability contract (P1)

Separate historical receipt validation from fresh render capability. Add
bounded Word-COM/LibreOffice worker ownership, cleanup, retry classification,
page-count/PDF hashing, and a clear `VISUAL_VALIDATION_REQUIRED` state.

Acceptance: a historical receipt with matching source hash is accepted only as
historical evidence; a new timeout is `failed` or `degraded`, never rendered;
no orphan Word process remains; the persisted receipt includes renderer,
source/PDF hashes, page metadata, duration, and retry classification; the
backend policy distinguishes generic conversion from strict Word QA; promotion
policy blocks visual-required artifacts without a fresh receipt.

Pointers: `render_gate.py`, `render_word_com.ps1`,
`test_docx_render_gate.py`, `test_docx_word_com_regression.py`.

### MDE-8 — batch/declarative research transforms and conflict-safe drafts (P1)

Add declarative batches for mechanical XML transformations, preserve the
render barrier (all writes first, one render afterward), and make draft merges
consume the canonical change-set/manifest instead of trusting a standalone
file-level merge.

Acceptance: mechanical transforms beat repeated round trips in benchmark
fixtures; non-overlapping region claims merge; stale/overlapping claims fail
closed; one render receipt covers the exact promoted package.

Pointers: `reference/mst_formatting_package_20260820/PIPELINE_ARCHITECTURE.md`,
`server.py:1990`, `server.py:2509`,
`tests/test_5988a5bb_docx_intel_concurrent_write_envelope.py`.

### MDE-9 — lifecycle, temp-run, and local-disk hygiene (P1)

Give every temporary run a run ID, owner, root, byte budget, timestamps,
retention state, process group, cleanup receipt, and safe garbage-collection
path. Keep prestage/draft outputs out of OneDrive by policy and record only
portable relative pointers in shared manifests.

Acceptance: abandoned runs are discoverable and reclaimable without touching
canonical files; disk quota prevents new work before exhaustion; handoffs list
run roots and cleanup state; no machine-local absolute path is put in a shared
capability manifest.

Pointers: `provenance/meridian_reliability_audit_20260821.md:158`,
`tools/meridian_fallbacks/document_release_manifest.py`,
`tools/meridian_fallbacks/final_prestage_release_gate.py`.

## Parallel execution order

```text
MDE-1 ─┬─> MDE-3 ─> MDE-4 ─> MDE-5 ─> release integration
MDE-2 ─┘       └─> MDE-7 ─┘
MDE-6 ───────────────────────> MDE-4/MDE-5
MDE-8 ───────────────────────> MDE-3
MDE-9 ───────────────────────> all release work
```

Safe parallel discovery lanes are MDE-1/MDE-2/MDE-6/MDE-9 because their
primary resources are distinct. MDE-3 is the serialization point. MDE-4 and
MDE-5 should then proceed sequentially because both define the cross-package
evidence contract. High-contention files remain sequential under the existing
Meridian claim rules.

## Handoff requirements

The canonical handoff must be generated with Meridian `generate_handoff`, not
hand-written as a substitute. It must include:

- current project ID and session ID;
- explicit statement that no version increase is authorized;
- the local investigation report path above;
- current board caveat: stale goal/capability profile and existing C84-W3
  overlap;
- all MDE items with IDs once created, dependencies, pointers, tools, and
  acceptance tests;
- required tools and fallback chains;
- “investigation only” exclusions until a new executor claims an item;
- the render timeout and cold Outputs-index state;
- the subagent-pool limitation: the requested multi-agent fan-out could not
  start because the pool reported `agent thread limit reached`;
- the exact next action: repair capability profile, then claim MDE-2 or MDE-6
  in a separate worktree and re-run the focused gates.

Do not paste a free-floating `/goal` body as authoritative. Prefer the
project-scoped `pending_goal`/`load_handoff` channel and cross-check every
listed item against the live board, including `pending`, `in_progress`, and
other non-done statuses.

## Open questions for the implementation sprint

## Hosted control plane and paid-tier boundary (2026-08-23)

The product boundary should be explicit: local Meridian remains useful and
free/self-hostable, while Meridian Hosted is the paid control plane for
durability, coordination, cloud connections, and hosted execution.

### What remains local/free

- local MCP execution and offline resilience;
- local document/code work, temporary drafts, and prestage artifacts;
- local provenance generation and verification;
- local BM25/indexing fallback;
- local LaTeX/Word tooling where the machine has the required software.

### What customers pay Meridian Hosted for

- tenant/project authority, cross-device state, and trusted handoffs;
- hosted tunnel aggregation and remote MCP access;
- provider OAuth/connection state for SharePoint, OneDrive, and Overleaf;
- hosted sync, publish/reconciliation, render/compile workers, and receipts;
- durable indexes, quotas, audit/recovery, browser dashboard, and team access.

The repository already has the beginnings of this boundary: Stripe Checkout
and an optional metered overage price in `meridian/hosted.py`, signed Stripe
webhook handling and dunning in `meridian/routes/billing.py`, a Pro-only hosted
tunnel refresh route in `meridian/routes/tunnel.py`, and an explicit hosted
local-filesystem refusal in `meridian/_deps.py`. The missing abstraction is a
central entitlement/capability registry so hosted-only routes do not each
invent their own `plan == "pro"` check.

### Recommended Fly shape

```text
public API/control plane
  -> durable tenant/project database
  -> durable artifact/object storage
  -> bounded job queue
       -> sync workers
       -> render/compile workers
       -> receipt/index workers
```

Keep the API/control plane and workers as separate Fly process groups or apps.
Do not create one Fly app or persistent volume per customer initially. Use
bounded concurrency, per-tenant quotas, idempotency keys, retry/dead-letter
states, and an operator kill switch. Fly autostop/autostart is suitable for
bursty workers; stopped/suspended Machines release CPU/RAM billing, while
volumes continue to incur storage charges. See the official [Fly resource
pricing] and [Fly autostop/autostart] documentation.

The current deployment is still a single public `meridian-hosted` Fly app:
`fly.toml` exposes one Uvicorn HTTP service, keeps one Machine running in the
primary region, allows up to 40 Machines, and enables autostop/autostart.
`Dockerfile` starts only `uvicorn meridian.server:app`; it does not yet define
a durable worker process group. The repository has an in-process dispatcher,
but that is not equivalent to a separately restartable, quota-controlled
hosted job worker. Treat worker separation and queue durability as required
before promising hosted render/compile or cloud-publish SLAs.

Fly Volumes should not become the cross-region canonical artifact store: they
are hardware-local and Fly does not automatically replicate data between
volumes. Use the existing durable Postgres layer for coordination and add an
object-storage abstraction for artifacts/receipts; reserve Fly Volumes for
cache, scratch, or explicitly replicated worker state.

### Billing model

Stripe should be the billing authority, but not the runtime authorization
check. Stripe Products/Features map to Meridian capabilities; signed webhooks
update a local entitlement snapshot; every hosted-only route/tool uses one
central guard. Stripe documents Entitlements for mapping product features to
grant/revoke events, and Meter Events for reporting usage. See [Stripe
Entitlements] and [Stripe usage-based billing].

Meter Meridian-owned costs: hosted compute, render/compile time, durable
storage, indexing, and high-volume sync. Do not add opaque per-click charges
for ordinary Microsoft Graph file operations or Overleaf Git; disclose any
provider endpoint that is separately metered and keep it behind an explicit
allowlist. A successful subscription event grants capability; it does not
prove a provider connection or cloud publish succeeded—those require separate
health and receipt state.

[Fly resource pricing]: https://fly.io/docs/about/pricing/
[Fly autostop/autostart]: https://fly.io/docs/launch/autostop-autostart/
[Stripe Entitlements]: https://docs.stripe.com/billing/entitlements
[Stripe usage-based billing]: https://docs.stripe.com/billing/subscriptions/usage-based/how-it-works

## Cloud integration and API-cost boundary (2026-08-23)

The cloud adapters should be designed around explicit provider contracts, not
browser scraping. Browser links remain useful for human review and fallback,
but the durable path needs provider IDs, consent state, synchronization
receipts, and recoverable publish operations.

### Microsoft Graph / OneDrive / SharePoint

- Meridian-owned drafts/configuration should prefer the Microsoft Graph App
  Folder with the least-privilege `Files.ReadWrite.AppFolder` permission. A
  customer-selected SharePoint site or library requires a separate
  permission/consent path and must not be silently treated as an App Folder.
- Delegated access is the default for an individual user acting on files they
  can access. Application permissions are an enterprise/admin-consent mode and
  should be an explicit tenant deployment choice, never an implicit fallback.
- The adapter should store provider-neutral connection metadata, DriveItem IDs,
  ETags, content hashes, delta-link state, consent scope, and last successful
  receipt. It should not store access tokens in shared project state or
  manifests.
- Standard Graph file operations are generally covered by the customer's
  Microsoft 365 subscription within service limits; Microsoft also documents
  separately metered APIs. The current metered list includes the SharePoint and
  OneDrive for Business `assignSensitivityLabel` API at $0.00185 per call, so
  Meridian must maintain an endpoint allowlist and never call metered APIs as a
  hidden implementation detail. See the official [metered API list] and
  [metered API overview].
- Throttling is an operational constraint even where calls are not separately
  billed: respect `Retry-After`, exponential backoff, delta queries, batching,
  and bounded concurrency. A cloud publish is not complete until the remote
  identity, response metadata, and verification receipt are durable.

### Overleaf / LaTeX

- The first integration should be a local Git-based adapter using an
  explicitly supplied Overleaf Git token. Meridian should never ask users to
  paste that token into a proposal, handoff, shared note, or capability
  manifest.
- Overleaf's Git integration is a linear project history, not a normal
  multi-branch merge service. Meridian therefore owns local sharding, draft
  isolation, semantic merge, compile verification, and publish ordering; the
  Overleaf adapter is a controlled pull/push endpoint with explicit conflict
  states.
- A LaTeX verification receipt should bind the source snapshot, included
  files, bibliography inputs, compiler/toolchain identity, log, generated PDF
  hash, and warnings/errors. Browser navigation to Overleaf is a review UX,
  not proof that a local source package compiled or that a publish converged.

### Meridian charging and packaging boundary

For the initial product, external provider costs should be pass-through
responsibility of the customer's account/plan, while Meridian meters its own
resources: hosted execution, indexing, storage, render/compile workers, and
optional high-volume sync. The user-facing contract should expose provider
consent, provider subscription prerequisites, Meridian quota consumption, and
any metered provider endpoint before execution. There should be no opaque
per-click API surcharge for ordinary Graph file sync or Overleaf Git use.

The proposed adapter policy is therefore:

```text
local disk draft/prestage
  -> explicit provider connection + consent
  -> provider-neutral snapshot/delta state
  -> local validation/merge/compile/render
  -> staged publish
  -> remote receipt + Meridian handoff
```

This keeps temporary material off OneDrive, makes browser use optional, and
lets Meridian sell the durable work layer rather than reselling Microsoft or
Overleaf access.

[metered API list]: https://learn.microsoft.com/en-us/graph/metered-api-list
[metered API overview]: https://learn.microsoft.com/en-us/graph/metered-api-overview

### Board status after investigation

The live project board (`5787cc92-ba7a-4788-b17c-28ab7938b839`) now resolves
the promoted investigation parent and its child items. MDE-1 through MDE-9
have durable line-range pointers, resource scopes, required tools, acceptance
criteria, and the `current` no-version-bump policy. The six CLOUD children are
also tracked under the cloud addendum, and their durable report pointers are
now attached in the live board. Resource scopes and tool requirements still
need to be written after the hosted project foreign-key routing is repaired;
the update endpoint currently rejects the project even though pointer and
handoff reads succeed. An existing canonical handoff is persisted, but a fresh
handoff cannot be generated until that session/project mismatch is fixed.

1. Should the release manifest live in Meridian core, the fallback package, or
   a small shared contract package imported by both?
2. Which renderer is the required production backend: Word COM, LibreOffice,
   or an explicitly provisioned external render worker?
3. Is Tantivy the required primary search backend with DuckDB/FTS fallback, or
   should a deterministic BM25 implementation be retained as a second local
   fallback independent of both native extensions?
4. What exact research-evidence XML schema/namespace should be versioned for
   external consumers?
5. Which existing v0.2.6 items are duplicates of MDE-3/MDE-4 and should be
   merged rather than re-added?

## Investigation disposition

This report is ready to drive a no-version-bump implementation sprint. It is
not a claim that the mega-sprint is shipped. The next safe state transition is
to record the proposal and pointers, repair the empty capability profile, and
then execute only the explicitly claimed items with per-item tests and a fresh
canonical handoff.

## Codebase implementation audit (2026-08-23)

This is the current repository reality behind the strategy above. The audit
used the codebase graph and direct working-tree inspection. Serena prospecting
was attempted through the Meridian wrapper but could not run because the
Serena tunnel is disconnected and the current graph project is not available
to that wrapper; graph results were therefore cross-checked against the local
files before treating them as evidence.

### Already real enough to build on

- **Paid hosted boundary:** Stripe Checkout, Billing Portal, webhook handling,
  dunning state, plan metadata, metered-overage hooks, and a Pro-only tunnel
  gate exist in `meridian/hosted.py` and `meridian/routes/billing.py`. This is
  the strongest near-term monetization surface, but it still needs an end-to-
  end entitlement/receipt audit before being marketed as production-grade.
- **Tenant isolation:** Neon provisioning is implemented as pooled Neon
  projects with customer-specific databases and an atomic slot claim. Failed
  provisioning has exponential retry plus a durable `provision_queue`. This is
  a useful early hosted boundary; it is not a reason to create one Fly app per
  customer.
- **Local research/output moat:** `meridian-outputs` has BM25 output search,
  content hashes, staleness states, artifact/provenance binding, typed research
  evidence, and lossless JSON/XML envelope serialization. The planned artifact
  registry and semantic output validation are explicitly still missing.
- **BM25 fallback:** `meridian-codeindex` is a standalone MIT package with
  DuckDB FTS/Okapi BM25 as the complete baseline, optional vector second stage,
  incremental indexing, and exact path/hash lookup. It is installed in the
  main environment and separately installable; it is not merely a placeholder.
- **Document/LaTeX parsing:** DOCX/OOXML structural parsing and native LaTeX
  structural parsing/bibliography inspection exist. The standalone
  `meridian-docs` package still describes itself as a skeleton and the package
  remains primarily a parser, not a full cloud document workflow.
- **Worker primitives:** `dispatcher.py` and `enqueue.py` implement bounded
  async Claude subprocess dispatch, leases, completion reconciliation, process
  death handling, deterministic worker routing, and a durable provisioning
  queue. This is a credible prototype, not a hosted job platform.

### Not present as production connectors

- **OneDrive/SharePoint:** Microsoft OAuth currently authenticates the user via
  `/v1.0/me`; there is no provider-backed DriveItem/delta/publish adapter in the
  working tree. The strategy is ahead of the implementation here.
- **Overleaf:** LaTeX source parsing exists, but there is no Overleaf Git/API
  connector or compile/publish receipt pipeline.
- **Tigris/S3-compatible storage:** `artifact_store.py` is intentionally
  local-first, content-addressed filesystem storage. It has no S3/Tigris
  adapter. Adding an adapter is comparatively easy, but it is supporting
  infrastructure, not the product moat.
- **Hosted render/compile workers:** the Docker image starts one Uvicorn API
  process. LibreOffice is used in CI for render coverage, but the Fly image has
  no Word COM backend and no separate durable render/compile worker service.
- **Redis:** Redis has real optional session Pub/Sub and a revision-keyed
  profile-cache primitive with budget guardrails and a Neon fallback. The
  profile cache currently has no production request-path callers, so it is not
  yet reducing Neon traffic; shared rate limiting intentionally remains on
  Postgres. Redis is not the queue or state authority.
- **Tigris/S3:** No adapter exists, and none is needed for this investigation.
  Treat it as a later thin object-storage implementation adapter when hosted
  artifact durability creates a concrete requirement; Postgres/manifests stay
  authoritative and local disk remains the fallback.

### Difficulty versus monetization

| Surface | Current state | Difficulty | Revenue potential | Disposition |
|---|---|---:|---:|---|
| Hosted entitlements, tunnels, trusted handoffs | partial but real | medium | very high | Fix and sell first |
| Provenance/output/evidence receipts | unusually strong local base | high | very high | Core moat; harden now |
| Durable hosted workers/rendering | prototype, default-off | very high | high | Build only after paid demand |
| OneDrive/SharePoint convergence | OAuth only | high | high | First premium connector after core hardening |
| Overleaf Git/compile convergence | parser only | high | medium | Research niche; later |
| Tigris/S3 artifact adapter | absent | low-medium | low-direct, enabling | Add when hosted artifacts need it |
| Redis-backed acceleration | optional fallback already exists | low-medium | low-direct | Keep optional; do not make authoritative |
| BM25/code indexing | real standalone package | medium | medium-high as a bundle | Use as free wedge and paid reliability feature |
| Real-time collaborative editing | absent | very high | uncertain | Defer; do not compete with Word/Overleaf |

The product conclusion is sharper than “Meridian needs everything”: sell a
**long-horizon production control plane** whose first wedge is research-grade
code/document/output work. Hosted billing, trusted handoffs, resilient local
fallback, evidence/provenance receipts, and provider-neutral publish/reconcile
are what customers pay for. Microsoft Graph, Overleaf, Tigris, Redis, and
dedicated workers are replaceable adapters or later capacity layers.

The hardest item is not adding another MCP tool. It is making a hosted worker /
render / sync system restart-safe, tenant-safe, receipt-backed, and economical.
The highest-return sequence is therefore: repair the hosted project/board
identity, finish entitlement and handoff executability checks, harden the
provenance/output registry and receipts, then validate one paid connector with
real users before adding worker or storage complexity. No version increase is
authorized by this audit.
