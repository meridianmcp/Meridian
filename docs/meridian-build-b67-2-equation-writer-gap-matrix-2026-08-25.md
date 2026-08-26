# B67-2: Equation writer / OMML gap matrix

**Date:** 2026-08-25
**Author:** session `4dfd2a59-bfd5-43e4-9e05-3ba60a78e140`
**Scope:** planning/investigation only. No sprint-item code claim, no
implementation, no mutation of any DOCX (real or fixture). This report reads
code only.

## 0. Provenance — where each symbol was actually read from

Per this item's own instructions (depends on B67-1, `c85f08ab`), the
equation/OMML work (MDE-B1/B2/8) is real but unlanded: it lives only on
branch `mde-rework-44fc1ffe-536-2` (tip `b7e9b172`), checked out at worktree
`.claude/worktrees/wf_bf94f275-830-1`. `dev` (this repo's current checkout,
tip `12368304`) does **not** have that work. This was independently
re-confirmed, not assumed:

| File | dev HEAD vs. worktree | What differs |
|---|---|---|
| `extensions/meridian-docs/meridian_docs/docs_intel.py` | **20095 vs 18065 lines** — worktree is ~2030 lines longer | Worktree adds `audit_equation_integrity` / `compare_equation_structures` / `repair_equation_batch` (MDE-B1/B2, lines 19127+) plus supporting code. Every symbol this item asks about that lives in `docs_intel.py` (`_build_omath_paragraph`, `_validate_omml_structure`, `_verify_equation_write`, `_atomic_write_docx_bytes`, `_check_artifact_provenance_binding`, `_enforce_render_verification`, `copy_section`, `insert_equation_local`, `edit_equation_local`, `remove_equation_local`, `_new_para_id`) **already exists on dev too** (dev already has prior equation-contract commits `4bcb7d7c`/`0c2820e3`) — the worktree version is the same functions, further along, not new-in-worktree-only. This report cites worktree line numbers and, where relevant, calls out worktree-only additions explicitly. |
| `extensions/meridian-docs/meridian_docs/render_gate.py` | **1734 vs 1179 lines** | Worktree adds the entire MDE-7 (`1e6150ef`) durable-receipt subsystem: `RenderReceipt`, `render_with_receipt`, `list_render_receipts`, `check_release_render_gate`, `RENDER_TEMPDIR_PREFIX`. **None of this exists on `dev`.** `dev`'s `render_gate.py` (and the worktree's) both already have `check_render_capability` and `check_word_com_render_receipt` (a same-call, non-durable, Word-COM-only variant despite the "_receipt" in its name — it persists nothing). |
| `meridian/doc_store.py` | **byte-identical** (`diff` returns 0 lines) | `_validate_omml_structure`, `_write_docx_transaction`, `_verify_paragraph_write`, `_mint_para_id` are exactly what's on `dev` today — the MDE branch did not touch this file. |
| `meridian/docx_integrity_gate.py` | **byte-identical** (`diff` returns 0 lines) | Not touched by the MDE branch. Still only wires `check_render_capability` (live, single-call) — has **no** integration with MDE-7's receipt ledger, which is unsurprising since that ledger doesn't exist on `dev`. |
| `extensions/meridian-docs/meridian_docs/ooxml_integrity.py` | **untracked in the main checkout, absent from every branch** (`git log --all` returns zero commits) | Confirms B67-1's own finding. It is mentioned only in a prose comment in the worktree's `docs_intel.py` (line 18882); the real `from . import ooxml_integrity, render_gate` wiring exists **only** as an uncommitted diff in the main checkout's working tree (`_save_docx_xml_stdlib` namespace-preserving serialization, `_atomic_write_docx_bytes` pre-write comment-attribute normalization) — not on the MDE branch, not on `dev`. Treated here as a third, separate provenance bucket, not conflated with either. |

Everything below is read from the worktree (`wf_bf94f275-830-1`) for the
equation-family symbols, and from the main checkout for `doc_store.py` /
`docx_integrity_gate.py` (identical to `dev` for both) and `ooxml_integrity.py`.

---

## 1. Symbol-by-symbol findings

### `_validate_omml_structure` — TWO independent implementations, not shared

- **`docs_intel.py:7750`** (stdlib `xml.etree.ElementTree`). **`doc_store.py:1131`** (lxml, `_LET`).
- Both: parse the raw OMML string; reject a root that isn't exactly
  `m:oMath` (explicitly rejects `m:oMathPara` as an insertion root — "the
  ambiguous wrapper used by a different insertion contract"); walk every
  descendant and check `_OMML_REQUIRED_CHILDREN` per-tag (e.g. `num`/`den`
  must contain `m:e`); flatten the OMML to text and check it against
  `_OMML_FALLBACK_MARKERS` — a flattened LaTeX-ish marker (`\hat`, `argmax`,
  etc.) present in the flattened text **without** a corresponding OMML
  structural element is rejected as "flattened fallback text… without its
  structural element."
- **Enforced:** root-tag identity, required-child presence for known
  container types, and the flattened-fallback-text heuristic.
- **Optional/absent:** no schema (XSD) validation against the OOXML `CT_OMath`
  content model — this is a hand-written subset check ("deliberately covers
  the structural subset emitted by our converter"), not a full schema
  validator. No check that the OMML actually round-trips through Word — that
  is `_enforce_render_verification`'s job, a separate later stage.
- **Gap:** the two implementations are independently maintained (confirmed
  by the module comment on `_mint_para_id`, which explicitly states the
  no-cross-import boundary is deliberate). A future fix to one validator's
  `_OMML_REQUIRED_CHILDREN`/`_OMML_FALLBACK_MARKERS` table will **not**
  propagate to the other unless a human remembers to mirror it by hand.

### `_build_omath_paragraph` — `docs_intel.py:8539`

- Wraps a raw OMML string in a new `<w:p>`. IDs: `w14:paraId`/`w14:textId`
  are **UUID-derived** when the caller doesn't supply them
  (`uuid.uuid4().hex[:8].upper()`), not deterministic/sequential. Its own
  docstring says as much: "direct unit callers may omit them and receive
  UUID-derived values" — the *production* caller (`insert_equation_local`)
  always supplies explicit values it minted itself via `_new_para_id`
  (below), so in practice every id ends up UUID-derived either way, just
  minted one call frame higher.
- Alignment/indent (`pPr`) are omitted entirely when neither is set —
  matches pre-4efc63fd output exactly (a deliberate compatibility
  constraint, not an oversight).
- **Enforced:** paragraph structure, alignment/indent from a resolved style
  policy. **Absent:** no validation of the `omml_raw` it's given — it calls
  `ET.fromstring(omml_raw)` directly with no try/except and no call to
  `_validate_omml_structure`. Validation is the **caller's** responsibility
  (and `insert_equation_local` does call `_resolve_omml`/OMML validation
  before this, but `_build_omath_paragraph` itself has no defense if called
  directly with malformed OMML).

### `_verify_equation_write` — `docs_intel.py:8583`

- Post-write, re-reads the docx **fresh from disk** (never the in-memory
  tree) and confirms: (a) for `position="append"`, the anchor paragraph
  still exists and contains an `<m:oMath>`; (b) for `before`/`after`, the
  paragraph at the recorded body index is present and is a `<w:p>`; (c) the
  flattened `<m:t>` text of the **last** `<m:oMath>` in that paragraph
  matches `expected_flat_text`; (d) for `before`/`after` only, the new
  paragraph's `w14:paraId`/`w14:textId` match what was minted.
- **Enforced:** presence + flattened-text identity + paraId/textId identity
  (display-mode only). **Absent:** no re-validation of the *OMML structure*
  of what actually landed on disk (no second `_validate_omml_structure`
  call here) — only the flattened text is compared, so a structurally
  different OMML tree that happens to flatten to the same text string would
  pass this check silently. **Absent:** no check for `position="append"`
  that the paraId/textId of the (pre-existing) anchor paragraph are
  unchanged — only checked for the two display-mode positions.

### `_new_para_id` (`docs_intel.py:10543`) / `_mint_para_id` (`doc_store.py:2022`)

- **Identical algorithm, independently implemented** (confirmed by
  `_mint_para_id`'s own docstring: "Independently implemented rather than
  imported… mirrors extensions/meridian-docs/…/docs_intel.py's `_new_para_id`
  (same convention) but this module has no dependency on that standalone
  package, and that package correspondingly has none on this one.")
- Both: `uuid.uuid4().hex[:8].upper()` in a loop, checked against and
  immediately added to a caller-supplied `taken: set[str]` so repeated calls
  in the same batch don't collide with each other, not just with what's
  already on disk.
- **Deterministic vs. UUID — confirmed UUID everywhere.** There is no
  deterministic/sequential/counter-based paraId minting scheme anywhere in
  either module. Every fresh `w14:paraId`/`w14:textId` minted by the
  equation-writer or DocStructureStore write paths is a random UUID4 hex
  fragment, collision-checked only against in-batch + pre-scanned on-disk
  ids, never derived from content or position.

### `_atomic_write_docx_bytes` (`docs_intel.py:2335`) vs. `_write_docx_transaction` (`doc_store.py:1643`)

Two independently-implemented but nearly-identical stage/verify/promote
transactions — same "disposable-worker-artifact" pattern (`dccc2311`), same
three steps (STAGE to a `tempfile.NamedTemporaryFile`/`mkstemp` sibling in
`dest`'s own directory → VERIFY the staged bytes fresh from disk against a
`pre_manifest` structural count over `protected_keys` → PROMOTE via
`os.replace` inside `_docx_promotion_lock`, an in-process-only
`threading.RLock` keyed by canonical path). Both compute `promoted_sha256`
(full-body SHA-256 of exactly what was promoted, read fresh from disk) used
downstream by `_safe_restore_after_verification_failure`'s compare-and-swap
logic. Both back up a pre-existing `dest` to `dest + ".bak"`
(best-effort, non-fatal).

**One real, load-bearing difference — where provenance binding is gated:**

| | `docs_intel.py::_atomic_write_docx_bytes` | `doc_store.py::_write_docx_transaction` |
|---|---|---|
| `artifact_provenance` parameter | **Does not exist on this function at all.** | First-class keyword param, default `None`. |
| Where provenance is checked | Not here — a *separate* function, `_check_artifact_provenance_binding` (below), checked **after promotion**, only by callers of `_verify_docx_write` that explicitly pass `artifact_provenance=`. | **Before promotion**, inside the same transaction, via `_check_artifact_provenance(artifact_provenance)` — raises `DocxWriteVerificationError` and the file is never even swapped in if the binding isn't `all_clear`. |
| Fail-closed timing | Post-write (promotion already happened; a failure triggers the CAS-safe restore-from-backup path). | Pre-promotion (the live file is never touched at all on a provenance failure — strictly stronger). |

This is a genuine architectural divergence between the two "equivalent"
transaction primitives, not a documentation gap — `doc_store.py`'s version is
strictly more conservative for provenance binding specifically.

Also present only in `_atomic_write_docx_bytes`'s docstring/behavior: when
`changed_parts` is supplied, every changed `.xml`/`.rels` member is
independently re-parsed for well-formedness from the staged-and-flushed
file (a ZIP-valid but XML-corrupt member would otherwise slip past the
structural-manifest count check alone).

### `_check_artifact_provenance_binding` (`docs_intel.py:3015`) — wired to exactly one real call path for the symbols in this item's scope

- Fail-closed gate on a **caller-supplied, pre-computed** provenance-binding
  verdict dict (`{"all_clear": bool, "bindings": [...]}`) — the shape
  `meridian_outputs.provenance.bind_artifact_provenance` returns. This
  module deliberately never imports `meridian_outputs` — duck-typed, by
  design (mirrors `doc_store.py`'s `_check_artifact_provenance`, which the
  same item, `6d02f343`, added on the other side of the package boundary).
- `None` (the default everywhere) is a pure no-op — "the caller did not ask
  for this check."
- **Call sites (grepped exhaustively):** `_check_artifact_provenance_binding(...)` is called from exactly **two** places in `docs_intel.py`: (1) unconditionally at the tail of `_verify_docx_write` (line 3183, always invoked, but with whatever `artifact_provenance` that shared verifier's *own* caller passed — default `None`); (2) one explicit `artifact_provenance=artifact_provenance` keyword forward at line 13729.
- **Confirmed absent from every symbol in this item's exact scope.** None of
  `insert_equation_local`, `edit_equation_local`, `remove_equation_local`,
  `append_text_run_after_math`, or `copy_section` accept an
  `artifact_provenance` parameter at all (checked every signature) — so for
  the equation-writer family specifically, **this gate never fires**, not
  even the post-write, best-effort way `_verify_docx_write`'s other callers
  get it. `copy_section` in particular calls `_verify_docx_write` (line
  13353) but does not pass `artifact_provenance=`, so its own post-copy
  verification silently no-ops this check.

### `_enforce_render_verification` (`docs_intel.py:2860`)

- Wraps `render_gate.check_render_capability(write_dest)` and enforces its
  three-state contract as a **write-time** gate: `"rendered"` → success,
  `render_verified=True`. `"failed"` → CAS-safe restore-from-backup + error,
  never reported as rendered. `"unavailable-with-reason"` (no render backend
  in this environment) → **also fails closed by default** (restore + error)
  unless the caller passes `allow_degraded_render=True` **and** a non-empty
  `degraded_render_reason` — an explicit, audited degrade, never silent.
- A broken/exception-raising `check_render_capability` call is caught and
  turned into a synthetic `"failed"` result — "a broken checker must fail
  closed, never crash the write or masquerade as verified."
- **Call sites:** 12 in `docs_intel.py` (grepped) — includes
  `insert_equation_local` (line 8908) but **not** `edit_equation_local`,
  **not** `remove_equation_local`, **not** `append_text_run_after_math`,
  **not** `copy_section`. So for the six named write operations in this
  item's scope, only `insert_equation_local` gets a real render-capability
  check at write time.
- **Documented, not-yet-wired gap (in the function's own docstring):** this
  function's `render_result` is only ever kept in the caller's in-memory
  success payload — it does not itself survive process restart or the
  render backend's temp-dir cleanup. The docstring explicitly names the
  fix (`render_gate.render_with_receipt(write_dest, check_result=render_result,
  receipts_path=...)`) as "a deliberate follow-up, not part of this change."
  That follow-up (MDE-7, `1e6150ef`) has since landed **on the worktree
  branch** as a standalone capability (`render_with_receipt`,
  `check_release_render_gate`) but **is not called from
  `_enforce_render_verification` or `insert_equation_local` anywhere in the
  worktree either** — confirmed by grep: neither name appears as a call
  inside `docs_intel.py` at all, only inside `render_gate.py`'s own
  definitions. The write-time equation gate and the durable-receipt ledger
  are two live, tested, but **still fully disconnected** subsystems.

### `copy_section` (`docs_intel.py:12951`, thin dispatch wrapper at `server.py:2223`)

- **Deep-copy guarantee: confirmed real.** Uses Python's `copy.deepcopy(el)`
  per top-level copied element (line 13230) — not a shallow/reference copy.
- Every `<w:p>` in the copied range gets a **fresh** `w14:paraId` via
  `_new_para_id` (UUID-derived, per above); the old→new id map is recorded.
  Every bookmark name in the copied range is renamed to a fresh unique name
  (`_rename_bookmark_for_copy`) — explicitly to prevent two live bookmarks
  answering to the same name, which would make Word's/`find_references_to`'s
  field resolution nondeterministic. A `REF`/`PAGEREF`/`NOTEREF` field
  **inside** the copied range that targets a bookmark **also inside** it is
  repointed at the copy's own renamed bookmark (an internal self-reference
  stays internally consistent); a field targeting something outside the
  copied range is left alone.
- **Equation handling in copy_section is real but bounded:** before any
  mutation, it builds `_equation_semantic_manifest([body])` over the whole
  source document and **aborts closed** if any existing equation already has
  `issues` (malformed/flattened OMML) — "repair the equations before copying
  a section." After the write, its post-write check
  (`_verify_docx_write(..., expected_equation_manifest=...)`, line 13353)
  compares an `expected` manifest (baseline − removed + copied entries)
  against a fresh re-read — this **does** exercise
  `_validate_omml_structure` transitively (via `_omml_semantic_record`,
  which calls it per equation) as part of manifest construction, so a
  structurally-broken copy would be caught. It does **not** call
  `_check_artifact_provenance_binding` (no `artifact_provenance=` argument is
  passed to `_verify_docx_write` here — see above) and it does **not** call
  `_enforce_render_verification` at all (not in the 12-site grep list) — no
  real Word/COM render check ever runs after a section copy, unlike
  `insert_equation_local`.
- **Explicitly documented, not-fixed gap in the function's own docstring
  (`679c86f4`):** a copied image paragraph's `r:embed` relationship id is
  **not** rewritten and the underlying `word/media/*` part is **not**
  duplicated — the copy shares the same image relationship as the original.
  Detected post-write via `_verify_image_ownership` and rejected
  (restore-from-backup) **unless** `allow_relationship_reuse=True` is passed
  — the caller's explicit acknowledgment, not a fix. "Actually duplicating
  the media part… is out of scope here."
- Numbering: `renumber_sequences` runs as the final step (same as
  `move_section`), refreshing SEQ-field caption numbers and any REF display
  text for the copy — but this is figure/table caption numbering, **not**
  the table-numbered-equation `"(1)"/"(2a)"` scheme `audit_equation_style`
  checks (see the cross-cutting section below); `copy_section` does not
  independently verify equation-number contiguity/uniqueness for whatever
  table-numbered equations it may have just duplicated.

### `insert_equation_local` / `edit_equation_local` / `remove_equation_local` / `append_text_run_after_math` — side-by-side

| | `insert_equation_local` | `edit_equation_local` | `remove_equation_local` | `append_text_run_after_math` |
|---|---|---|---|---|
| Line (worktree) | 8703 | 8952 | 9211 | 9082 |
| OMML validated on input | Yes — via `_resolve_omml` (LaTeX-or-raw-OMML resolution, which validates) before touching the file | Yes — same `_resolve_omml` path | N/A (no new OMML) | N/A (no OMML payload) |
| Write path | `_save_docx_xml_stdlib` → `_atomic_write_docx_bytes`, held inside `_docx_promotion_lock` across write+verify+render-check | `_save_docx_xml_stdlib` directly, **no explicit promotion-lock context held by this function around verify** (relies entirely on `_save_docx_xml_stdlib`'s own internal reentrant lock during the write itself) | Same as edit | Same as edit |
| Post-write structural re-verify | **Yes** — `_verify_equation_write` (flattened-text + paraId/textId identity, fresh re-read from disk) | **No.** No re-read-from-disk check that the new OMML actually landed; no call to `_verify_equation_write` or `_verify_docx_write` | **No** equivalent verification that the equation is actually gone / the paragraph is intact | **No** verification that the run actually landed |
| Render-capability gate | **Yes** — `_enforce_render_verification`, fail-closed unless explicitly degraded | **No** | **No** | **No** |
| Provenance binding | No (param doesn't exist on this function) | No | No | No |
| Restore-on-failure (CAS-safe) | **Yes**, via `_safe_restore_after_verification_failure` on both the structural-verify and render-verify failure paths | **No** — nothing to restore *from* since nothing is verified after write | **No** | **No** |
| ID minting | Fresh `_new_para_id`/`_new_para_id` for paraId+textId (display modes only; append mode reuses the anchor's existing id) | N/A — edits in place, no new paragraph/ids | N/A — removes in place | N/A |
| Multi-equation-per-paragraph handling | N/A (always inserts a new element/paragraph) | **Required** `equation_index` when the paragraph holds >1 equation — fails closed rather than guessing (a documented `b6a9ec99` hardening of a prior silent-data-loss bug) | Removes **all** `<m:oMath>` in the paragraph if the paragraph is equation-only, or all `<m:oMath>` children if mixed — does not support removing just one of several stacked equations | **Required** `math_index` when >1 equation present; explicitly refuses to guess an insertion point inside a nested container (hyperlink/content-control/textbox) or a multi-equation `m:oMathPara` |
| Sidecar invalidation | `_invalidate_sidecar_mtime` | Same | Same | Same |

**Net finding:** of the four single-equation write primitives, only
`insert_equation_local` gets the full stage→verify→render-gate→CAS-restore
pipeline. `edit_equation_local`, `remove_equation_local`, and
`append_text_run_after_math` all route through the same underlying
`_atomic_write_docx_bytes`/`_docx_promotion_lock`/backup-before-promote
machinery (so a write-time ZIP/well-formedness failure still fails closed
and leaves `dest` untouched), but **none of the three re-read the document
after the write to confirm the edit/removal/append actually took effect as
intended**, and **none run a render-capability check**. This is a real,
asymmetric gap across an otherwise-parallel API surface, not a difference in
risk profile that would obviously justify it (an edit that silently fails to
apply is exactly the kind of bug `_verify_equation_write` exists to catch
for inserts).

### `_write_docx_transaction` / `_verify_paragraph_write` / `_mint_para_id` — core `DocStructureStore` insertion path

`meridian/doc_store.py`'s `class DocStructureStore` (line 2418) is the "core
DocStructureStore insertion" the acceptance criteria names; its
paragraph-level writers are `update_paragraph` (line 4456) and
`merge_paragraph_draft` (line 4744) — both async methods routing through
`_save_docx_xml` → `_write_docx_transaction` (described above) plus, per
`_verify_paragraph_write`'s own docstring, a **mandatory post-write
verification specific to `update_paragraph`/`merge_paragraph_draft`**:
re-reads the file fresh from disk, re-locates the target paragraph by
`para_id` (via the same resolution rule used pre-write), and confirms its
**plain text** now matches `expected_text`. An `AmbiguousParagraphIdError`
surfacing on re-read (only reachable if a genuine concurrent writer
introduced a duplicate id) is folded into the same mismatch-string return
path rather than raised, "keeping the never-raises-itself contract intact."

- **Enforced:** structural manifest (media/style/relationship counts)
  unchanged across the write (`_write_docx_transaction`); paragraph text
  content equals expectation post-write (`_verify_paragraph_write`);
  optional pre-promotion provenance-binding gate (`artifact_provenance`,
  strictly stronger timing than the `docs_intel.py` equivalent — see above).
- **Optional:** `artifact_provenance` — plumbed as a keyword parameter
  through `_write_docx_transaction`/`_save_docx_xml`, but this investigation
  did not find a confirmed call site in `doc_store.py` that supplies a
  real (non-`None`) value from `update_paragraph`/`merge_paragraph_draft`
  themselves — the parameter exists and fires correctly *if* supplied, but
  whether any current production caller actually supplies it is unverified
  here (would require tracing every caller of `update_paragraph` across
  `meridian/server.py`/MCP handlers, out of this item's read scope).
- **Absent:** no OMML/equation-specific verification in
  `_verify_paragraph_write` itself — it only compares flattened *plain*
  text, so a paragraph-text edit that happens to touch an equation's
  surrounding text is checked, but the equation's own OMML structure is not
  independently re-validated by this path (equation-specific checks live
  only in the `docs_intel.py` writers above, a separate module with no
  shared code path into `doc_store.py`'s paragraph editor).
- **ID minting:** `_mint_para_id` — UUID4-derived, identical algorithm to
  `docs_intel.py::_new_para_id` (see above), used by
  `repair_duplicate_para_ids` (a separate, explicit, opt-in repair utility —
  never invoked as a side effect of reading or addressing a document; read
  paths detect a duplicate id and either report or fail closed, but never
  self-heal it).

### `render_gate` promotion checks

Two genuinely distinct mechanisms exist, and they are not the same maturity
level:

1. **`check_render_capability` / `check_word_com_render_receipt`** (both on
   `dev` already, `render_gate.py:755`/`681` main-checkout line numbers) —
   a single live call, three-state (`rendered`/`unavailable-with-reason`/
   `failed`), **not persisted anywhere**. `check_word_com_render_receipt`'s
   name is misleading: despite "_receipt", it persists nothing — it's just
   `check_render_capability` scoped to the Word-COM backend only. This is
   what `_enforce_render_verification` and `docx_integrity_gate.py` both
   actually call today.
2. **`render_with_receipt` / `check_release_render_gate` /
   `list_render_receipts`** (worktree-only, MDE-7 `1e6150ef`, not on `dev`
   at all) — a durable, atomic-JSON-ledger-backed receipt system.
   `render_with_receipt` builds a `RenderReceipt` (backend, backend
   version, process identity, PDF hash/size/page-count, field-refresh
   status, an explicit `visual_qa` state defaulting to `"not_reviewed"` —
   backend-conversion success is **never** implicitly treated as a human
   visual QA pass) and persists it via an atomic `os.replace`-based JSON
   write. `check_release_render_gate` is the release-time enforcement
   point: refuses release (`release_ready=False`) unless the **newest
   matching receipt** for that exact `docx_path` has `status="rendered"`,
   its `source_docx_sha256` matches the file's **current** on-disk content
   (so a receipt for a since-edited file cannot authorize releasing the new
   content), and its age is within `max_age_seconds` (default 24h) —
   otherwise refuses unless a human passes
   `allow_degraded_override=True` + non-empty `override_reason`, itself
   recorded as a second, durable, auditable `kind="degraded_override"`
   receipt in the same ledger (never a silent bypass).
- **Current vs. historical distinction: real, but confined to mechanism 2
  only, and mechanism 2 is unlanded.** `list_render_receipts` returns every
  receipt (renders *and* overrides) for a document sorted newest-first —
  this is the only place in the whole audited surface that can answer "what
  did prior render attempts on this document look like," and it doesn't
  exist on `dev`. Mechanism 1 (what's actually wired into every equation
  writer and into `docx_integrity_gate.py` today) is stateless — each call
  is a fresh probe with no memory of any prior attempt, on `dev` or the
  worktree alike.

### `docx_integrity_gate` composition (`meridian/docx_integrity_gate.py`)

- Composes exactly three sources, per its own docstring, into one verdict:
  live `render_gate.check_render_capability` (never `render_with_receipt`),
  live `docs_intel.audit_equation_style` (numbering/punctuation/notation —
  see below), and a **self-computed, read-time** provenance fingerprint
  (`_compute_provenance_manifest`: SHA-256 over `{byte_size,
  paragraph_count, heading_count, sorted xml_part list}` from
  `read_document_snapshot`) — explicitly **not** a reuse of
  `_atomic_write_docx_bytes`'s write-time `manifest_hash`/`promoted_sha256`,
  because (module docstring, verbatim) "that machinery is WRITE-time only
  and nothing durably persists its output anywhere this gate could read."
- Discovers candidate `.docx` artifacts from two durable sources (sprint
  item pointers, proposal-evidence `artifact` links) — never from a live
  filesystem scan.
- `RECIPE_CHECK_REGISTRY` (lines 551-571) documents three named checks —
  `structural_check_required` → `render_gate.verify_promotion_readiness`,
  `word_com_render_check_required` → `render_gate.check_word_com_render_receipt`,
  `outputs_provenance_check_required` → the `meridian-outputs` MCP server —
  and this investigation confirmed **both named `render_gate` functions do
  exist** at the paths cited (`verify_promotion_readiness` and
  `check_word_com_render_receipt`, both present on `dev`'s
  `render_gate.py`, not just the worktree). The registry is explicitly
  **documentation-only, by the module's own comment**: "Deliberately NOT
  wired into `build_docx_integrity_gate`'s own execution… out of this
  item's touches_resources scope to touch." A per-item `artifact_recipe`
  can *declare* which checks it wants (`describe_required_checks`), but
  nothing in this file (or, as far as this item's scope covers, anywhere
  else) actually dispatches to `verify_promotion_readiness` /
  `check_word_com_render_receipt` / an Outputs provenance call based on
  that declaration — `build_docx_integrity_gate` always runs the same
  three fixed checks for every discovered artifact regardless of what any
  item's recipe says it wants.
- **Never touches `_check_artifact_provenance_binding` /
  `_check_artifact_provenance` / `meridian_outputs` provenance at all** —
  despite `outputs_provenance_check_required` existing in the registry as a
  *documented* check name, the gate's actual execution has zero source/
  package-hash comparison against any canonical Outputs-registered
  provenance record; its only "hash" is the self-computed structural
  fingerprint above, which has no relationship to `meridian_outputs`'
  `bind_artifact_provenance`/`record_provenance` machinery at all.
- **Current vs. historical render receipts: absent entirely.** No
  `receipts_path` parameter, no call to `list_render_receipts` or
  `check_release_render_gate` — every gate evaluation is a fresh live probe
  with no memory. This is consistent with mechanism 2 not existing on
  `dev` (the branch this file lives on), but means that even once MDE-7
  lands, `docx_integrity_gate.py` needs its own follow-up change to
  actually consume it — landing the branch alone does not wire this file
  up to receipts.

---

## 2. Cross-cutting gap matrix, by acceptance dimension

| Dimension | Finding |
|---|---|
| **Deterministic vs. UUID IDs** | 100% UUID4-derived across every writer audited (`_new_para_id`, `_mint_para_id`, `_build_omath_paragraph`'s own fallback). No deterministic/sequential/content-derived id scheme exists anywhere in this surface. Collision avoidance is a `taken: set[str]` re-roll loop, checked against both on-disk ids and in-batch mints — real, but purely probabilistic, not structural. |
| **Deep-copy guarantees** | Confirmed real for `copy_section` — genuine `copy.deepcopy`, fresh paraIds, fresh bookmark names, self-reference repointing. **Explicitly, documentedly NOT extended to image relationships** (`r:embed` and the underlying media part are shared, not duplicated, unless `allow_relationship_reuse=True` is passed to accept the shared-reference state) — a real, acknowledged, opt-out-required gap, not an oversight. |
| **OMML container checks** | Real but non-uniform. `_validate_omml_structure` exists in two independently-maintained copies (one per module) and is exercised on every *new* equation payload (`insert_equation_local`/`edit_equation_local`, transitively via `_resolve_omml`) and transitively on every equation already in a document that `copy_section` touches (via `_equation_semantic_manifest`). It is a hand-authored structural-subset check, not a schema validator, and it is never re-run on the *post-write* on-disk bytes for anything except `insert_equation_local` (`_verify_equation_write` checks flattened text identity only, not structure) and `copy_section` (via its equation-manifest post-write comparison). `remove_equation_local` and `append_text_run_after_math` never invoke it at all (nothing to validate for remove; append only adds a plain `<w:r>`, not OMML). |
| **Numbering / punctuation / notation checks** | Exist (`audit_equation_style`, `docs_intel.py:8354` — misaligned-equation, missing/incorrect trailing punctuation, duplicate/gap table-numbered-equation numbering) but are **entirely read-time and opt-in** — never called by any of `insert_equation_local`/`edit_equation_local`/`remove_equation_local`/`append_text_run_after_math`/`copy_section` themselves. The only production wiring found is `docx_integrity_gate.py`'s `equation_auditor` slot, itself best-effort and non-blocking unless an item's artifact policy is `"strict"`. A caller who inserts/edits/copies equations gets **zero** inline feedback on alignment, trailing punctuation, or numbering contiguity — those issues surface only later, if at all, through a separate handoff-time gate. |
| **Source / package hashes** | Three distinct, non-unified hash concepts coexist: (1) `promoted_sha256` — write-time, full-body SHA-256 of exactly what a given transaction promoted, used only for the CAS-safe restore-vs-concurrent-writer decision, never persisted beyond the in-memory transaction result; (2) `manifest_hash` — write-time, SHA-256 over `changed_parts` only (the intended delta), also transient; (3) `docx_integrity_gate`'s `provenance_manifest.manifest_hash` — a wholly separate, read-time structural fingerprint (byte size / paragraph / heading / xml-part-list counts) with no cryptographic or structural relationship to (1) or (2), computed because (per the module's own docstring) "nothing durably persists" (1)/(2) anywhere this gate could read them. None of the three is the `meridian_outputs`-style source-script/package provenance hash the `outputs_provenance_check_required` registry entry implies exists — that check is named but not wired in (see above). |
| **Current vs. historical render receipts** | A real durable-receipt subsystem exists (`RenderReceipt`/`render_with_receipt`/`list_render_receipts`/`check_release_render_gate`) with a genuine current-vs-historical distinction (`list_render_receipts` returns full history newest-first; `check_release_render_gate` requires the *newest matching* receipt to be fresh + content-hash-matched) — but it is **unlanded** (worktree-only, absent from `dev`), and even on the worktree it is **called from nowhere** in either the equation-writer family or `docx_integrity_gate.py`. Every write-time and handoff-time render check actually exercised today is the older, stateless, single-call `check_render_capability`/`check_word_com_render_receipt` — no memory of any prior render attempt, on `dev` or on the worktree. |

---

## 3. Side-by-side operation matrix

| Operation | Symbol | Fresh IDs minted | Post-write structural re-verify | Render-capability gate | Provenance-binding gate | Restore-on-failure (CAS-safe) | Numbering/punctuation audit |
|---|---|:---:|:---:|:---:|:---:|:---:|:---:|
| Insert | `insert_equation_local` | Yes (UUID) | Yes (`_verify_equation_write`) | Yes (`_enforce_render_verification`) | No | Yes | No (opt-in, out-of-band only) |
| Edit | `edit_equation_local` | N/A | **No** | **No** | No | **No** | No |
| Remove | `remove_equation_local` | N/A | **No** | **No** | No | **No** | No |
| Append text after math | `append_text_run_after_math` | N/A | **No** | **No** | No | **No** | No |
| Section clone | `copy_section` | Yes (UUID, every copied `<w:p>`) | Yes (`_verify_docx_write` w/ equation manifest — transitively validates OMML structure) | **No** | **No** (param not threaded through) | Yes | No |
| Core `DocStructureStore` paragraph write | `update_paragraph`/`merge_paragraph_draft` via `_write_docx_transaction` + `_verify_paragraph_write` | Only via separate opt-in `repair_duplicate_para_ids` | Yes (plain-text identity only, not OMML-aware) | No (not this module's concern) | Optional, pre-promotion (stronger timing than `docs_intel.py`), unconfirmed live caller | Yes | No |

---

## 4. Summary — ranked by how surprising/risky the gap is

1. **Asymmetric verification across the equation-writer family.** Insert
   gets a full verify+render+restore pipeline; edit, remove, and
   append-text-after-math get none of the three, despite sharing the same
   underlying atomic-write machinery that *could* support them. This is the
   most actionable, narrowly-scoped gap in this audit.
2. **`_enforce_render_verification` and the MDE-7 receipt ledger are two
   live but disconnected subsystems**, even after MDE-7 lands — the
   docstring names the intended integration explicitly as a deliberate,
   still-unstarted follow-up.
3. **`docx_integrity_gate.py`'s `RECIPE_CHECK_REGISTRY` names three checks
   it does not run** (`verify_promotion_readiness`,
   `check_word_com_render_receipt`, an Outputs provenance call) — the
   functions exist, but nothing dispatches to them based on a declared
   recipe; every artifact gets the same fixed three checks regardless.
4. **Provenance binding (`_check_artifact_provenance_binding` /
   `_check_artifact_provenance`) never reaches the equation-writer family**
   — none of the six audited write operations accept or forward an
   `artifact_provenance` argument, so `6d02f343`'s fail-closed binding gate
   is architecturally present in the codebase but structurally unreachable
   from this surface.
5. **Two independently-maintained `_validate_omml_structure` copies** and
   two independently-maintained UUID-paraId-minting functions
   (`_new_para_id`/`_mint_para_id`) — both pairs are deliberate,
   documented, package-boundary decisions (not accidents), but both are a
   standing double-maintenance risk for future OMML-contract changes.
6. **The `copy_section` image-relationship-sharing gap is real but already
   fully acknowledged** in-code (`679c86f4`, opt-out via
   `allow_relationship_reuse`) — listed for completeness, not as a new
   finding.

No DOCX (real or fixture) was created, opened for writing, or mutated in the
course of this investigation. No sprint-item code was claimed or edited.
