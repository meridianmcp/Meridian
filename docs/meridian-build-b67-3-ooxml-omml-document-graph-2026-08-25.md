# B67-3: OOXML/OMML document graph — schema and invariants (planning only)

**Date:** 2026-08-25
**Author:** session `4dfd2a59-bfd5-43e4-9e05-3ba60a78e140`
**Scope:** planning/specification only. No sprint-item code claim beyond this
document, no implementation, no graph is created, no DOCX (real or fixture)
is opened, read for content, or mutated. This is a schema + invariants
proposal for a future implementation item, grounded in the real code read
for this item and in B67-2's independently-verified findings.

---

## 0. Where this reads from

Read in full for this item, per its instructions:

- `meridian/research_graph.py` (231 lines) — the cross-artifact research
  graph's closed vocabularies, edge directionality table, and identity-key
  builders.
- `meridian/db/research_graph.py` (823 lines) — that graph's persistence
  layer: two tables (`research_nodes`, `research_edges`), append-only +
  explicit-supersede write semantics, unresolved-edge resolution.
- `extensions/meridian-outputs/meridian_outputs/provenance.py` (622 lines) —
  `resolve_figure_output` / `find_outputs_by_source` / `bind_artifact_provenance`,
  the join between a structural DOCX artifact and meridian-outputs'
  provenance index.

Additionally read, because the acceptance criteria requires placing this
graph relative to the *existing* intra-document store, not just the two
named modules, and because B67-2's findings (equation writers, render gate,
`docx_integrity_gate.py`) are the concrete evidence this spec must model
correctly:

- `meridian/doc_store.py` (schema block + `_check_artifact_provenance` /
  `_write_docx_transaction` region) — the existing intra-document structural
  store: `doc_documents`, `doc_elements`, `doc_edges`, `doc_equations`,
  `doc_figures`, `doc_tables`, `doc_flag_links`.
- `meridian/docx_integrity_gate.py` (601 lines, full) — how the three DOCX
  integrity signals (render status, equation-style audit, a read-time
  structural fingerprint) are composed today, and its `RECIPE_CHECK_REGISTRY`
  gap.
- `docs/meridian-build-b67-2-equation-writer-gap-matrix-2026-08-25.md` — the
  independently-verified findings this item was told to use directly.

No code on the `mde-rework-44fc1ffe-536-2` branch was re-read directly for
this item; B67-2's findings about it (MDE-7's durable render-receipt ledger,
the equation-writer asymmetry, the three non-unified hash concepts) are used
as given, per this item's instructions.

---

## 1. Placement decision (acceptance criteria's first question, answered first)

**This is a new, dedicated module pair — `meridian/ooxml_graph.py` +
`meridian/db/ooxml_graph.py` — mirroring `research_graph.py` /
`db/research_graph.py`'s own split exactly (closed vocabularies + identity
builders in the leaf module; persistence, append-only writes, and
unresolved-edge resolution in the db module). It is not an extension of
`research_graph`'s enum, and it is not a rewrite of `doc_store.py`.**

Three existing things already partially cover this space, and each has a
real reason it cannot simply be widened to do the whole job:

| Existing module | What it already does | Why it isn't "just extend this" |
|---|---|---|
| `research_graph.py` / `db/research_graph.py` | A small, closed, cross-artifact vocabulary (`claim`, `citation`, `code`, `run`, `output`, `document`, `decision`) with append-only + explicit-supersede revisions and lazily-resolved cross-identity edges. Its own docstring names `doc_store`'s `doc_edges` as one of the "NARROW, single-domain graphs" it deliberately does **not** replace. | Its `document` node type is coarse by design — `document_identity_key(source, element_id)` addresses at most one structural element per node, not a full paragraph/run/table/equation tree with containment order, anchoring, and render state. Adding 15 new node kinds and 13 new edge kinds to `NODE_TYPES`/`EDGE_TYPES` would turn a small, curated, project-wide vocabulary meant for `get_claim_evidence`/`get_lineage_subgraph`-style cross-artifact queries into an OOXML parser's internal type system — every existing consumer of that enum would have to learn to ignore paragraph/run-level noise it was never built to reason about. |
| `doc_store.py` | Already parses and persists exactly the structural facts this item needs: `doc_elements` (ordered tree: `parent_id`, `ordinal`, `level`, `kind`, `text`, `ref` — `ref` is the `w14:paraId`, with a documented synthesized-`p{index}` fallback for missing/duplicate ids), `doc_equations` (OMML + normalized-LaTeX dedup key), `doc_figures`/`doc_tables` (semantic dedup index + `caption_element_id` linkage), `doc_edges` (intra-document `cites`-style edges), `doc_flag_links` (provenance anchored to a `doc_elements` id). | It has no revision/identity model at all in the sense this item needs: `reindex_document` **overwrites** a document's rows in place (no append-only history, no `seq`, no supersede/conflict tracking), its `doc_edges.edge_kind` is a free string (not a closed, directionality-documented vocabulary), and it has no node type at all for `DocumentSnapshot`, `source binding`, `revision`, or `render receipt` — those concepts don't exist anywhere in this module. Rewriting `doc_store.py` to add all of that would mean touching seven already-shipped, already-tested tables and every one of their ~15 write paths (`update_paragraph`, `merge_paragraph_draft`, `put_equations`, `link_figure_caption`, …) for a planning item whose acceptance criteria is a schema, not a migration. |
| `provenance.py` (meridian-outputs) | `bind_artifact_provenance` already computes exactly the right four-state verdict (`resolved` / `orphaned` / `hash_mismatch` / `unresolved`) for a structural artifact against the outputs index. | It is a **pure, stateless function call** — its result is never persisted anywhere. There is no history of "what did this artifact's binding verdict look like last time," which is precisely the "source drift" concept this item asks for. |

The layering this spec proposes:

```
Layer 0 (fact sources — UNCHANGED by this item):
  doc_store.py        — parses/stores OOXML structure (doc_elements, doc_equations, …)
  provenance.py        — resolves a structural artifact's binding to the outputs index
  render_gate.py        — stateless render probe today; MDE-7's durable ledger, unlanded

Layer 1 (THIS ITEM — new):
  ooxml_graph.py / db/ooxml_graph.py
    — typed nodes+edges, closed vocab, append-only + explicit-supersede revisions,
      degraded/unknown states as first-class values, never re-parses OOXML itself —
      it PROJECTS Layer 0's already-computed facts into an auditable graph.

Layer 2 (research_graph.py — UNCHANGED, single attachment point):
  one ooxml_graph DocumentSnapshot's identity_key == one research_graph
  `document` node's identity_key (same document_identity_key(source, element_id)
  builder, reused verbatim) — the coarse cross-artifact graph anchors here;
  it never needs to know what's inside.
```

`ooxml_graph.py` reads from `doc_store.py`'s rows, from `provenance.py`'s
return dict shape, and from `render_gate.py`'s three-state (and, once
landed, MDE-7's receipt) contracts — duck-typed, exactly like
`docx_integrity_gate.py`'s own "compose, never re-derive" discipline (see
that module's docstring: it explicitly refuses to recompute
`_write_docx_transaction`'s write-time manifest and instead builds its own
read-time fingerprint because "nothing durably persists" the write-time one
— this item is the first module positioned to actually close that gap, by
being the durable, append-only store the write-time facts were always
missing). It never imports OOXML-parsing code and never duplicates
`_validate_omml_structure`, `_mint_para_id`, or any hashing logic that
already exists.

---

## 2. Identity model

### 2.1 Document-local identity

Within one `DocumentSnapshot`, a structural node's **document-local anchor**
is:

1. Its native OOXML persistent id when one exists and is unambiguous — for
   a paragraph/table-row-owning paragraph, that is `w14:paraId`; for a
   package part, its ZIP member name (`word/document.xml`,
   `word/media/image3.png`); for an equation, the `element_id` `doc_equations`
   already records (the containing paragraph's `w14:paraId`, or `None` when
   there is no anchoring paragraph, per that table's own schema).
2. `doc_store`'s existing synthesized fallback (`p{index}`, i.e. positional)
   when no native id exists or the native id is ambiguous (duplicated across
   more than one paragraph — `doc_store.py` already detects and names this
   case, `AmbiguousParagraphIdError`). This spec reuses that exact fallback
   convention rather than inventing a second one — one synthesized-id scheme
   for the whole codebase, not two.
3. For nodes with genuinely no persistent id of any kind (a `run` — OOXML
   gives runs no stable identity at all) — a **positional key**:
   `{containing_paragraph_anchor}::run[{index}]`. This is deliberately
   fragile (an edit that inserts a run before it changes every later run's
   index) and callers must treat a `run` node's document-local anchor as
   valid only within the `DocumentSnapshot` revision it was computed against
   — see §2.3.

### 2.2 Graph-global identity

A graph-global `identity_key` is:

```
{document_identity_key}::{node_kind}::{document_local_anchor}
```

where `document_identity_key` is `research_graph.document_identity_key`'s
own output for this DOCX's `source` (path/uri) — reused verbatim, not
recomputed. This is the concrete mechanism that makes Layer 1 and Layer 2
(§1) interoperate without duplication: the SAME string a `research_graph`
`document` node uses as its identity is the SAME string this graph's
`DocumentSnapshot` node uses as *its* identity (with `node_kind="snapshot"`
and no document-local anchor — the whole-document case). A `paragraph` node
inside that same DOCX is
`{document_identity_key}::paragraph::{w14:paraId}` — same document, same
prefix, disjoint from every other node kind's key space by construction
(the `::{node_kind}::` segment), so two different kinds can never collide
even if their document-local anchors happen to be textually identical (a
`table` and a `paragraph` sharing a coincidental synthesized `p3`, for
example).

`node_kind` is one of the 15 values in §4 (`snapshot`, `package_part`,
`section`, `paragraph`, `table`, `table_row`, `table_cell`, `run`,
`equation`, `caption`, `anchor`, `reference`, `source_binding`, `revision`,
`render_receipt`).

### 2.3 Revision — the axis identity is stable against

Mirrors `research_graph`'s `identity_key` (stable) / `revision` (changes)
split exactly, because that split, plus its two write primitives, is
already the right shape for this problem and this item should not invent a
second one:

- **`create_node`-equivalent (pure append)**: recording "this identity, at
  this revision, exists" without asserting anything about what came before.
  Used for the common case — re-indexing a document after an edit produces
  a new revision of every touched node; untouched nodes are **not**
  re-written (their existing row is still the current one for their
  identity — no need to touch nodes a given write didn't affect).
- **`replace_node_revision`-equivalent (explicit atomic supersede)**: an
  EXACT prior node id is retired in the same write as the new one is
  created. This is what a verified, successful write (e.g.
  `insert_equation_local`'s full verify→render→(no supersede needed, it's a
  fresh paragraph) or `edit_equation_local`'s in-place OMML change, WERE it
  to adopt this graph) should use for the paragraph/equation node it
  changed — not a bare append, because an append alone would leave two
  "active" revisions of the same paragraph identity sitting side by side
  with no recorded relationship, which is exactly the silent-conflict gap
  §5.4 below closes.

**Revision identity for a `run` node is position-scoped, not content-scoped**
(§2.1 point 3): a `run`'s graph-global identity already bakes in its index
within its paragraph at a specific snapshot, so "the same run, edited" and
"a different run that happens to now sit at the same index" are
indistinguishable from the identity alone. This is a deliberate, named
limitation (§7) rather than a false precision claim — run-level history
should be read through the *paragraph*'s revision chain (which anchor
resolves to which paragraph content, §2.4), not by tracking individual runs
across edits.

### 2.4 Stable `w14:paraId` anchoring — the `anchor` node type

The acceptance criteria calls out `w14:paraId` anchoring as a first-class
concept, not just a field on `paragraph`. This spec makes it a first-class
**node type** (`anchor`) plus an **`anchors` edge**, for one structural
reason: a native id and the structural node it currently labels are two
different things that can drift independently, and collapsing them into one
field (as `doc_elements.ref` does today) makes that drift invisible.

- An `anchor` node's identity_key is the bare native-id value
  (`{document_identity_key}::anchor::{paraId}`) — stable across every
  `DocumentSnapshot` revision, by definition (it IS the thing that's
  supposed to stay stable).
- An `anchors` edge points `anchor node -> paragraph/table/run node` for
  the CURRENT snapshot, meaning "this anchor identity currently resolves to
  this structural node." When a new `DocumentSnapshot` revision is created,
  the `anchors` edge for an unchanged paragraph is left untouched (the
  target's `identity_key` didn't change, even though its `revision` did —
  `anchors` edges resolve against identity, exactly like `research_graph`
  edges resolve against `(node_type, identity_key)` rather than a specific
  row id, so this "just works" via the same lazy-resolution pattern
  `db/research_graph.py` already implements).
- **Ambiguous anchor state, first-class**: when `doc_store`'s own duplicate-
  paraId detection fires, the corresponding `anchor` node's
  `resolution` field is `"ambiguous"` (not silently pointed at whichever
  candidate a query happened to find first) and it fans out `anchors` edges
  to **every** candidate paragraph node, each carrying `candidate_rank`
  metadata — mirrors `provenance.py`'s own `candidate_count`-on-ambiguous-
  match pattern (§2.5) rather than inventing a different ambiguity
  representation for anchors specifically.
- **Anchor identity is never reused across a `clones` edge** (§5, `clones`):
  every `w14:paraId`/`w14:textId` minted anywhere in the audited write
  surface is UUID4-derived (B67-2, confirmed with no exceptions) — a
  `copy_section` clone always gets a fresh anchor node with a fresh
  identity, connected to its origin only via the explicit `clones` edge,
  never by sharing an `anchors` target. This is an invariant this graph can
  state confidently precisely because B67-2 already confirmed there is no
  deterministic/content-derived id scheme anywhere that could accidentally
  produce a collision that looked like reuse.

### 2.5 Semantic fingerprints — a fourth, explicitly-labeled hash concept

B67-2 found **three** non-unified hash concepts already in production:
`promoted_sha256` (write-time, full-body, CAS-restore-decision only,
never persisted beyond one transaction), `manifest_hash` (write-time, over
`changed_parts` only, also transient), and `docx_integrity_gate`'s own
read-time structural fingerprint (`{byte_size, paragraph_count,
heading_count, xml_part_count}` → SHA-256, because "nothing durably persists"
the first two anywhere that gate could read them).

This graph needs a **fourth** concept — a per-node **semantic fingerprint**,
distinct from all three above and from the `w14:paraId` anchor identity
(§2.4) — because "same anchor, different content" (an edit) and "different
anchor, same content" (a clone, or a relocated figure) are genuinely
different facts a caller needs to tell apart, and none of the three
existing hashes are computed at node granularity or track content
independent of position:

| Concept | Scope | Computed by (reused, not reinvented) | What changing it means |
|---|---|---|---|
| `promoted_sha256` | whole staged payload | `doc_store._write_docx_transaction` (existing, write-time, transient) | the bytes actually promoted differ from a prior promotion |
| `manifest_hash` (write-time) | `changed_parts` only | `doc_store._write_docx_transaction` (existing, write-time, transient) | the intended delta of one write |
| `docx_integrity_gate`'s structural fingerprint | whole document, read-time | `docx_integrity_gate._compute_provenance_manifest` (existing) | coarse document-level drift (paragraph/heading/part counts) |
| **`semantic_fingerprint`** (new, this item) | **one node** | Reuses each kind's existing normalization where one exists — `doc_equations.latex_normalized` for `equation`, `doc_figures`/`doc_tables.normalized_caption` for `caption`/labeled nodes; a new, explicitly-scoped flattened-text hash (same flattening `_validate_omml_structure`/`_verify_equation_write` already do, reused not reinvented, for anything without an existing normalized key — `paragraph`, `run`) | this specific node's *content* changed, independent of whether its anchor/position did |

Every node/edge that carries a hash in this graph names **which** of these
four concepts it is (a `hash_kind` field, one of
`{promoted_sha256, manifest_hash, structural_manifest, semantic_fingerprint}`)
— never a bare `hash` field with no scope attached. This is a direct,
concrete response to B67-2's own recommendation that a future unification
effort needs "one place to look"; this table *is* that place, extended by
one row rather than left for a fifth ad-hoc hash to appear the next time
someone needs node-level content identity.

### 2.6 Source drift

A `source_binding` node's status is `provenance.ARTIFACT_STATUSES`, reused
**verbatim** (`resolved` / `orphaned` / `hash_mismatch` / `unresolved`) —
this graph never recomputes or approximates that classification; it only
persists what `bind_artifact_provenance` already decided, exactly like
`docx_integrity_gate.py`'s own "compose, never re-derive" rule for render
status and equation-style findings.

**Drift** is the append-only history this adds that `bind_artifact_provenance`
itself cannot provide (it's a pure function — every call is independent,
with no memory of a prior call): a `source_binding` node's identity_key is
stable per structural artifact (`{document_identity_key}::source_binding::{artifact_id}`);
each time it's re-evaluated, a new revision is created via the
`replace_node_revision`-equivalent path (§2.3), with a `revises` edge to
the prior revision. Drift is then a pure, auditable graph query — "list this
identity's revision history, newest first, and find the last revision whose
status was `resolved`" — never a live re-probe required just to answer "did
this drift, and when." A binding that starts `resolved` and re-evaluates to
`hash_mismatch` is exactly the failure mode `bind_artifact_provenance`'s own
docstring calls out (§`_bind_one_artifact`'s hash-mismatch branch) — this
graph is what turns that from "the current answer, if you happen to ask
again" into "a recorded fact with a timestamp and a predecessor."

---

## 3. Promotion / review state — `revision.promotion_state`

`doc_store._write_docx_transaction` today is binary: a write is either
*staged* (in a temp file, not yet promoted) or *promoted* (swapped in via
`os.replace`, inside `_docx_promotion_lock`) — there is no persisted
intermediate state. This graph's `revision` node makes that transaction
lifecycle, plus one genuinely new intermediate state, explicit and durable:

| `promotion_state` | Meaning | Existing analog |
|---|---|---|
| `staged` | Payload staged to a temp file; not yet promoted. A `revision` node that stays at `staged` forever (no later `promoted_from` edge ever created *from* a `promoted` revision *to* it) is the graph's durable record of an abandoned/failed transaction — auditable without needing to have observed the crash or the restore-on-failure path live. | `_write_docx_transaction`'s stage step (transient, in-process only today) |
| `provisional` | Promoted to disk, but not yet accepted as final — e.g. written under a temporary-review workflow, or promoted with `allow_degraded_render=True` (an explicit, audited degrade, per `_enforce_render_verification`'s own contract) rather than a clean pass. Mirrors `meridian_outputs.classify_temp_output_ownership`'s existing "archival/temporary, not yet canonical" concept, reused as a state name rather than invented fresh. | No direct existing analog — new state this item adds, because today "promoted" and "final" are conflated. |
| `promoted` | Accepted as the current, final revision for its identity. Only a `promoted` revision may be the target of a `supersedes` edge from a later revision (§5) — a `staged` or `provisional` revision cannot be superseded because it was never current. | `_write_docx_transaction`'s promote step |

A `promoted_from` edge (§5) always points `revision(promoted) -> revision(staged)`
— the concrete stage→promote transition, one edge per real transaction,
never inferred from timestamps alone.

---

## 4. Node types (15)

Every node shares the same base shape as `research_graph`'s `research_nodes`
row (`id`, `project_id`, `node_type`, `identity_key`, `revision`,
`status ∈ {active, superseded}`, `seq`, `supersedes_id`, `superseded_by`,
`created_by`, `created_at`, `updated_at`) plus one JSON `payload` column for
kind-specific fields — the same generic-table-plus-JSON-payload shape
`research_nodes.external_ref` already uses, deliberately, rather than 15
dedicated side tables (`doc_store.py` already has 7 tables and growing;
mirroring that per-kind-table sprawl at 15 kinds would be a much larger
migration surface for no query-shape benefit `research_graph`'s own pattern
doesn't already provide). `payload` never duplicates raw content
`doc_store`/`doc_equations` already stores (no second copy of OMML/paragraph
text) — it carries identity/fingerprint/state plus a **back-reference** to
the `doc_store` row this node was projected from (`doc_store_ref`:
`{table, id}`), so a consumer needing the actual XML/text always goes back
to `doc_store` for it, never to this graph.

| `node_type` | Document-local anchor (§2.1) | Key `payload` fields (in addition to `doc_store_ref`) |
|---|---|---|
| `snapshot` | *(none — whole-document)* | `docx_path`, `structural_fingerprint` (§2.5, `structural_manifest` kind, reused from `docx_integrity_gate._compute_provenance_manifest`), `element_count`, `content_hash` (from `doc_documents.content_hash`, reused not recomputed) |
| `package_part` | ZIP member name | `part_name`, `content_type`, `relationship_ids` (the `.rels` edges touching this part — informational; `r:embed` sharing per the `copy_section` image-relationship gap, §5's `clones`, is tracked here) |
| `section` | heading element's anchor, or synthesized | `heading_level`, `title_text_fingerprint` (semantic fingerprint, §2.5) |
| `paragraph` | `w14:paraId` / synthesized `p{index}` | `text_fingerprint` (semantic fingerprint), `style_name` |
| `table` | table's own anchor (first row's paraId, or synthesized) | `table_index` (from `doc_tables.table_index`), `row_count`, `column_count` |
| `table_row` | `{table anchor}::row[{index}]` | `row_index` |
| `table_cell` | `{table anchor}::row[{index}]::cell[{index}]` | `cell_index`, `text_fingerprint` |
| `run` | `{paragraph anchor}::run[{index}]` (§2.3 — position-scoped) | `text_fingerprint`, `formatting_summary` |
| `equation` | containing paragraph's anchor (matches `doc_equations.element_id`, `None` when unanchored) | `latex_normalized` (semantic fingerprint, reused from `doc_equations`), `omml_structurally_well_formed` (bool — see §6.2's naming rule; **never** a field named `verified` or `correct`) |
| `caption` | its own element anchor (`doc_figures`/`doc_tables.caption_element_id`) | `normalized_caption` (semantic fingerprint, reused) |
| `anchor` | *(is itself the identity, §2.4)* | `native_id_kind` (`paraId`\|`textId`\|`bookmark`), `resolution` (`resolved`\|`ambiguous`\|`missing`) |
| `reference` | the field's own containing-run anchor | `reference_kind` (`REF`\|`PAGEREF`\|`NOTEREF`\|`citation`), `target_identity_key` (may be unresolved, §5's `references`) |
| `source_binding` | the bound structural artifact's `artifact_id` (matches `provenance.bind_artifact_provenance`'s input shape) | `status` (`provenance.ARTIFACT_STATUSES`, verbatim, §2.6), `evidence`, `resolved_sha256`, `canonical_path` |
| `revision` | *(scoped to the node/snapshot it revises — carries that node's identity_key as a payload field, not its own)* | `promotion_state` (§3), `hash_kind` + `hash_value` (§2.5's labeled-hash rule), `transaction_id` |
| `render_receipt` | *(scoped to the snapshot it verifies)* | `tier` (`stateless_probe`\|`durable_ledger`, §6.1), `status` (§6.2), `visual_qa` (passed through from a `durable_ledger`-tier receipt only; `None` for `stateless_probe` — that tier has no such concept), `source_docx_sha256`, `max_age_seconds`, `backend` |

---

## 5. Edge types (13)

Directionality documented the same way `research_graph.EDGE_DIRECTIONALITY`
does — a table, never enforced structurally, so a future dashboard or query
helper has one place to read meaning from without re-deriving it.

| `edge_kind` | Direction | Meaning | Notes |
|---|---|---|---|
| `contains` | parent structural node → child structural node | The containment backbone: `snapshot → package_part → section → paragraph/table → run`, `table → table_row → table_cell`. | Mirrors `doc_elements.parent_id`, expressed as an edge instead of a foreign key so the same lazy cross-identity resolution `db/research_graph.py` already implements (§2.4) applies here too. |
| `precedes` | node → next sibling node, same containment level | Explicit document order, independent of a stored `ordinal` integer. | Lets an insertion between two existing siblings be expressed as "rewire two `precedes` edges" — a real graph mutation with its own history — rather than an in-place ordinal renumber that erases the fact a document order change happened at all. |
| `anchors` | `anchor` node → structural node | "This stable native id currently resolves to this node." (§2.4) | Resolved lazily against identity, same as `research_graph`'s unresolved-edge pattern; fans out to multiple targets only in the `ambiguous` case. |
| `labels` | `caption` node → figure/table/equation node | "This caption labels that artifact." | Mirrors `doc_figures`/`doc_tables.caption_element_id`. |
| `references` | `reference` node → its target (anchor / caption / section) | Internal cross-reference (`REF`/`PAGEREF`/`NOTEREF`) or citation. | Generalizes `doc_edges`' existing `cites` `edge_kind` to a closed, directionality-documented vocabulary; may be unresolved (target not yet a node) exactly like a `research_graph` edge with a `NULL` endpoint. |
| `binds_to_source` | `source_binding` node → external artifact identity | The persisted analog of one `bind_artifact_provenance` call. | **Cross-graph edge**: the target identity is a `research_graph` `output` or `code` node's `identity_key` (via `research_graph.output_identity_key`/`code_identity_key`, reused verbatim) — this is the one edge kind in this graph whose target routinely lives in Layer 2, not Layer 1. Unresolved when the target has no `research_graph` node yet, resolved lazily the same way. |
| `generated_by` | structural node → the process/run that produced it | Which executor run / tool produced this equation, figure, or table. | **Also cross-graph**: target is a `research_graph` `run` node's identity_key. Carries the `hash_kind`-labeled hash (§2.5) that ties the generated content to `promoted_sha256`/`manifest_hash` when the write transaction that created it is known. |
| `clones` | new node → origin node | Records a `copy_section`-style deep copy. | Carries `shares_media_relationship: bool` — the direct, explicit encoding of B67-2's confirmed `copy_section` gap (an image `r:embed` relationship is shared, not duplicated, unless `allow_relationship_reuse=True` was passed) — never silently omitted. |
| `revises` | new revision → prior revision (same identity) | Passive, append-only chronological history. | Always created alongside a fresh revision; does **not** retire the prior revision's `active` status by itself (§5.10 distinguishes this from `supersedes`). |
| `supersedes` | new revision → prior revision (same identity) | Explicit, atomic retirement — the prior revision's `status` flips to `superseded` in the same write. | Mirrors `replace_node_revision`'s `supersedes_id`/`superseded_by` pair exactly. A revision reachable only via `revises` (never `supersedes`) and still `active` is a legitimate, informative state (§2.3's "unmanaged append-only history," inherited on purpose) — it is `conflicts_with` (below), not `supersedes`, that flags when that same situation should instead be treated as an anomaly. |
| `conflicts_with` | revision ↔ revision (symmetric) | Two `active` revisions of the *same identity*, neither superseding the other, detected by an invariant check (§6.4) — e.g. two sibling sessions both wrote a new revision of the same paragraph without coordinating. | New capability; `research_graph.create_node` has no equivalent — it allows this situation to exist "unmanaged." This edge is what turns it into an explicit, queryable fact instead of a silent last-`seq`-wins resolution. |
| `verified_by_render` | `snapshot`/`revision` node → `render_receipt` node | "A render attempt exists and reported this status for this content." | **Absence of this edge is `unknown` render state, always** — see §6.2; never inferred as pass or fail. |
| `promoted_from` | `revision(promoted)` node → `revision(staged)` node | The concrete stage→promote transition of one write transaction (§3). | One edge per real `_write_docx_transaction`-equivalent promotion; a `staged` revision with no incoming `promoted_from` edge ever created is an abandoned-transaction record. |

---

## 6. Render verification — degraded/unknown state, first-class

This is the part of the acceptance criteria this item was told to get
right using B67-2's real findings, not a generic "add a status field."

### 6.1 Two tiers, modeled explicitly (not collapsed)

`render_receipt.tier` is `stateless_probe` or `durable_ledger`, directly
encoding B67-2's finding that these are two live-but-disconnected
subsystems:

- `stateless_probe` — `render_gate.check_render_capability` /
  `check_word_com_render_receipt`. The only tier actually wired into
  production today (`_enforce_render_verification`, `docx_integrity_gate.py`).
  A fresh probe with no memory of any prior attempt; a `render_receipt` node
  of this tier is this graph's own memory of it, not a reflection of any
  persistence the probe itself has (it has none).
- `durable_ledger` — MDE-7's `RenderReceipt`/`render_with_receipt` (worktree
  `mde-rework-44fc1ffe-536-2`, unlanded, called from nowhere in production
  per B67-2). A `render_receipt` node of this tier, once that ledger lands
  and is wired to write here, would carry `source_docx_sha256` +
  `max_age_seconds` (mirroring `check_release_render_gate`'s own staleness
  contract exactly) and the `visual_qa` field — which defaults to
  `not_reviewed` and must **never** be conflated with a passing render
  result (§6.3).

A caller asking "has this content ever been verified by the durable ledger
specifically" gets a real, distinguishable answer (`tier="durable_ledger"`
edges only) rather than a mixed pool of both tiers indistinguishable from
each other.

### 6.2 `render_receipt.status` — four states, `unknown` is structural not optional

`{rendered, failed, unavailable_with_reason, unknown}`. The first three are
`render_gate`'s existing three-state contract, reused verbatim (never
reinvented). `unknown` is new and is **not** a value this graph ever writes
onto a `render_receipt` node — it is the answer a query gives when **no**
`verified_by_render` edge exists at all for a given `snapshot`/`revision`
identity. This must be enforced as a query-layer invariant, not left to
convention: any function this graph exposes that answers "is this verified"
must return `unknown` for "no receipt", and must never let a caller's `if
status == "failed"` branch silently absorb the "never checked" case by
defaulting a missing lookup to `False`/falsy. This directly matches the
existing discipline: `render_gate`'s own `unavailable-with-reason` state
already refuses to let "couldn't check" collapse into "passed" — `unknown`
is that same discipline applied to "never even asked," at the graph layer.

Separately, `equation.omml_structurally_well_formed` (§4) is **deliberately
never named `verified`** — see §7.2.

### 6.3 `visual_qa` is never implied by `status="rendered"`

A `durable_ledger`-tier receipt's `visual_qa` field (from MDE-7, `not_reviewed`
by default) is carried through unchanged. Any derived "is this document
release-ready" query this graph might expose must treat
`status="rendered" AND visual_qa="not_reviewed"` as **not** equivalent to a
human-reviewed pass — a backend successfully converting a file proves it
opens, not that a person looked at the result. This mirrors
`check_release_render_gate`'s own design intent (a receipt records
conversion success and *separately* records whether a human reviewed it)
and must not be flattened away by this graph's own summarization.

### 6.4 `conflicts_with` detection is an explicit function, not implicit

Detecting the `active`+`active`+no-`supersedes`-between-them state (§5) is a
named, callable invariant check over this graph's own data — not something
inferred silently every time a node is read. This mirrors
`get_unresolved_edges` in `db/research_graph.py`: a dedicated, explicit
"list every anomaly of this kind" query, not a side effect baked into every
read path.

---

## 7. Invariants — auditable, never inferring manuscript truth from XML validity alone

These are the properties a future implementation's tests should assert
directly; several are worded as explicit *negative* constraints because the
acceptance criteria's core requirement is what this graph must **not**
claim.

1. **Structural well-formedness is necessary, never sufficient, for
   `promotion_state="promoted"`.** A `revision` may only be `promoted` when
   its underlying write passed the existing structural-manifest check
   (`_write_docx_transaction`'s `protected_keys` count comparison) — but
   passing that check alone never implies the *content* is correct, only
   that the package didn't lose parts it shouldn't have.
2. **No field in this graph is named `verified`, `correct`, or `valid`
   standing alone.** Every boolean or status field is scoped to exactly what
   was checked: `omml_structurally_well_formed` (§4 — `_validate_omml_structure`
   passing; says nothing about whether the equation is the *right* equation),
   `render_receipt.status` (§6.2 — a backend could open/convert it; says
   nothing about whether a human reviewed it, §6.3), `source_binding.status`
   (§2.6 — the outputs index has a matching record; says nothing about
   whether that record is itself correct). A query that wants to answer "is
   this document trustworthy" must compose several of these named,
   narrowly-scoped fields explicitly — this graph never manufactures one
   collapsed verdict field for a human to over-trust.
3. **`equation.omml_structurally_well_formed=True` does not imply
   `verified_by_render` exists, and vice versa.** They are different
   invariants over different evidence (hand-written structural-subset
   checker vs. an actual render backend) and B67-2 confirmed the codepaths
   that populate them are already independent (`insert_equation_local` gets
   both; `edit_equation_local`/`remove_equation_local`/
   `append_text_run_after_math` get neither, despite being just as capable
   of producing structurally-broken output) — this graph must preserve that
   independence, not paper over it with a combined field.
4. **Absence is not evidence of absence for `source_binding` (§2.6, "index
   not converged").** `provenance.bind_artifact_provenance`'s own
   `_bind_one_artifact` already refuses to call an artifact `orphaned` when
   the outputs index has not finished converging (`evidence: "index_not_converged"`,
   folded into `UNRESOLVED`, never `ORPHANED`) — this graph's `source_binding`
   node must persist that same distinction (`unresolved` due to
   non-convergence vs. `orphaned` due to confirmed absence are different
   revisions with different `status`, never merged into one "not found"
   bucket).
5. **A `conflicts_with` pair is a fact to surface, never a fact to
   auto-resolve.** This graph records the conflict; it does not pick a
   winner. Resolution (an explicit `supersedes` edge created by a human or
   an executor decision) is a separate, later write.
6. **`clones.shares_media_relationship` must be recorded on every `clones`
   edge, defaulting to the conservative (`True`, i.e. "assume shared until
   proven duplicated") value when unknown** — never silently omitted, per
   B67-2's confirmed `copy_section` gap.
7. **Every hash-bearing field names its `hash_kind` (§2.5).** A bare
   `hash`/`sha256` field with no scope label is a schema violation under
   this spec — the whole point of the four-row table in §2.5 is that a
   reader never has to guess which of the (now four) concepts a given hash
   is.
8. **`unknown` render state can only be produced by absence, never
   written.** No code path in a future implementation should ever `INSERT`
   a `render_receipt` row with `status="unknown"` — that value only ever
   comes from a query finding zero `verified_by_render` edges (§6.2).

---

## 8. Worked examples, tied to B67-2's findings

Illustrative only — no code is being claimed by this item.

- **`insert_equation_local` (the one operation B67-2 found gets the full
  pipeline).** Produces: a new `paragraph` revision (`promoted_from` its
  `staged` predecessor, §3), a new `equation` node
  (`omml_structurally_well_formed=True`, checked pre-write via
  `_resolve_omml` and post-write via `_verify_equation_write`'s flattened-
  text identity check — §7.3 notes these are *not* the same check), a fresh
  `anchor` node (`anchors → paragraph`, UUID-derived per §2.4), and — because
  `_enforce_render_verification` runs — a `verified_by_render` edge to a
  new `render_receipt(tier="stateless_probe")` node.
- **`edit_equation_local` (B67-2's confirmed gap: no post-write re-verify,
  no render gate).** Produces: a new `equation` revision via `revises`
  (§5) — but, honestly, **no** `verified_by_render` edge and **no**
  re-checked `omml_structurally_well_formed` for the new content, because
  neither check runs today. A query against this graph after such an edit
  correctly reports render state `unknown` (§6.2) and
  `omml_structurally_well_formed` as *whatever was last actually checked*
  (stale, from before the edit, not silently re-stamped `True`) — which is
  precisely the asymmetry B67-2 flagged, now visible as a graph fact instead
  of something only discoverable by reading source.
- **`copy_section`.** Produces: a `clones` edge per copied top-level
  element (fresh `anchor`/`paragraph`/`table` node on the new side,
  `shares_media_relationship=True` for any copied image per the confirmed
  gap), transitively re-validated `equation` nodes (via the pre-copy
  manifest abort-if-`issues` check and the post-write manifest comparison —
  both real, per B67-2), but **no** `verified_by_render` edge (not in
  B67-2's 12-site grep list) and **no** `binds_to_source` edge for the
  cloned artifacts (the `artifact_provenance` parameter isn't threaded
  through `copy_section` today).
- **A source-drift case.** A figure's `source_binding` resolves `resolved`
  on first bind. The script that generated it is later re-run and produces
  a byte-different file at the same canonical path. The *next*
  `bind_artifact_provenance` call returns `hash_mismatch`; this graph
  records that as a new `source_binding` revision, `revises`-linked to the
  prior `resolved` one — `list this identity's history` now shows exactly
  when the drift happened, without anyone having had to notice it live.

---

## 9. Non-goals / explicitly out of scope for this item

- No implementation, no migration, no new table, no code claim — this is a
  schema and invariants proposal for a follow-up sprint item.
- No opinion on which specific executor/tool call sites should be modified
  to *populate* this graph (e.g. whether `insert_equation_local` itself
  should write `ooxml_graph` nodes, or whether a separate indexer should
  project `doc_store` state into it after the fact, analogous to
  `DocStructureStore.reindex_document`). Both are compatible with this
  schema; picking between them is an implementation-item decision, not a
  schema decision.
- No opinion on whether/how `docx_integrity_gate.py`'s
  `RECIPE_CHECK_REGISTRY` should eventually dispatch through this graph
  instead of (or in addition to) its current three fixed checks — flagged
  as a natural follow-up, not decided here.
- Does not attempt to unify the four hash concepts in §2.5 into one — it
  names and labels all four so a *future* unification item has a complete,
  accurate map to start from, per B67-2's own recommendation.

No DOCX (real or fixture) was created, opened for writing, or mutated in
the course of this investigation. No graph was implemented. No sprint-item
code beyond this document was claimed.
