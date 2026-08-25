# B67-5 — DOCX Capability Matrix and Render Truth Table

Sprint item `e0667e43-45a6-47b4-b929-73dc21792879` (planning/investigation only — no
code changed, no software installed, no OneDrive file rendered). Grounds itself in
B67-2's independently-verified gap matrix
(`docs/meridian-build-b67-2-equation-writer-gap-matrix-2026-08-25.md`) and adds a live
environment probe run in this session on 2026-08-25.

## 1. Live environment probe (run just now, not assumed)

Probe script: throwaway `.docx` built in the session scratchpad (never a canonical or
fixture file), calling the exact shipped functions in
`extensions/meridian-docs/meridian_docs/render_gate.py` — `_soffice_unavailable_reason`,
`_word_com_unavailable_reason`, `detect_backend`, `check_render_capability`,
`check_word_com_render_receipt`. Full JSON output captured below; nothing in this
section is inferred from code reading alone.

| Check | Result |
|---|---|
| `soffice.exe` on disk | **Found**: `C:\Program Files\LibreOffice\program\soffice.exe` |
| `shutil.which("soffice")` / `("libreoffice")` (what the shipped code actually calls) | **`None` / `None`** — not on `PATH` |
| Direct `soffice --headless --convert-to pdf` probe (bypassing the shipped PATH-only lookup) | **Works**: exit 0, produced a real PDF in ~6–24s |
| `render_gate._soffice_unavailable_reason()` (the shipped capability check) | `"LibreOffice ('soffice'/'libreoffice') not found on PATH"` |
| `render_gate._word_com_unavailable_reason()` | `None` (pywin32/`win32com.client` imports cleanly) |
| Word install on disk | **Found**: `C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE` |
| `detect_backend()` winner | `word-com` (soffice loses the PATH-lookup, word-com wins) |
| `render_gate.check_render_capability(scratch.docx)` | **`status: "rendered"`**, `backend: "word-com"` — a real Word COM automation pass produced a PDF, ~8.7s |
| `render_gate.check_word_com_render_receipt(scratch.docx)` | **`status: "rendered"`**, same backend, ~5.9s (isolated child-process COM path) |

**Honest bottom line for this machine, right now:**
- **LibreOffice is physically installed and functionally usable**, but the shipped
  detection code (`shutil.which`, `PATH`-only) cannot see it — so every caller of
  `check_render_capability`/`detect_backend` in this environment gets `soffice` reported
  as *unavailable*, not because the binary is missing but because `PATH` was never
  configured to include `C:\Program Files\LibreOffice\program`. This is a real,
  fixable environment gap (add the directory to `PATH`, or a future enhancement could
  probe well-known install locations), not a statement about LibreOffice itself.
- **Word-COM visual verification is NOT degraded here** — it was just confirmed live and
  working (`"rendered"`, real PDF produced via actual Word COM automation). This is the
  opposite of "assume it's degraded because Word-COM is fragile in general" — this
  session's own probe is current, positive evidence for *this* machine at *this*
  moment. That positive result itself expires the instant the environment changes
  (pywin32 uninstalled, Word uninstalled/deauthorized, a future headless/service-account
  context with no interactive window station) — see §4 for why a receipt, even a
  passing one, is never evidence beyond the moment it was taken.
- Because `detect_backend()` always returns the *first* available backend in
  `KNOWN_BACKENDS = (soffice, word-com)` order and soffice loses on `PATH` here, every
  general `check_render_capability` call in this environment silently uses word-com even
  when a caller only wanted a cheap/general capability signal — worth flagging as a
  possible follow-up (either fix `PATH`, or teach the soffice probe to also check
  well-known Program Files locations), not something this planning item changes.

## 2. Capability matrix

Legend — **Status**: `required` (blocks the write/promotion if unmet and no accepted
fallback), `preferred` (tried first, degrading is allowed and audited), `fallback`
(only reached after a preferred check is unavailable). **Class**: `local` (needs
nothing but this process/filesystem), `local-mutation` (local, but mutates a DOCX and
so carries the write-time guarantees below), `tunnel` (needs the hosted Meridian
service / a network connector), `manual` (a human decision or provisioning step, not
something an executor can satisfy by calling a tool).

| # | Capability | Status | Class | Backing code | Current honest availability (this env, this session) |
|---|---|---|---|---|---|
| 1 | ZIP package integrity (`[Content_Types].xml`, `_rels`, required parts, dangling relationships) | required | local | `extensions/meridian-docs/meridian_docs/ooxml_integrity.py::validate_docx_package` | **Available.** Pure stdlib `zipfile`/`xml.etree`; no external process. |
| 2 | XML well-formedness of every part | required | local | same module, `_xml_parts` walk inside `validate_docx_package` | **Available.** |
| 3 | Relationship / content-type bijection (`dangling_relationship`, `dangling_relationship_reference`, `dangling_content_type`) | required | local | same module | **Available.** |
| 4 | Comment-attribute qualification (`w:id` vs bare `id`) | required | local | `ooxml_integrity.validate_docx_package` | **Available.** |
| 5 | Namespace-prefix-preserving serialization on write-back | required | local-mutation | `docs_intel.py::serialize_document_xml_preserving_namespaces` / `_assert_namespace_prefixes_preserved` | **Available** (needs `lxml`, already a hard dependency of the extension). |
| 6 | Structural manifest / write-transaction hash (`_docx_manifest_hash`, `promoted_sha256`) | required | local-mutation | `docs_intel.py::_atomic_write_docx_bytes`, `_docx_structural_manifest` | **Available.** Pure hashlib/stdlib, no subprocess. |
| 7 | Atomic promotion (stage → verify → `os.replace`) with CAS-guarded restore-on-failure | required | local-mutation | `docs_intel.py::_atomic_write_docx_bytes`, `_safe_restore_after_verification_failure` | **Available**, but process-local only (see B67-2 finding #1 — `_docx_promotion_lock` is an in-process `threading.RLock`, not a cross-process lease). |
| 8 | OMML equation structural validation on write | required | local-mutation | `docs_intel.py::_validate_omml_structure`, `_equation_semantic_manifest` | **Available.** |
| 9 | Equation-style audit (findings, never free text) | preferred | local | `docs_intel.py::audit_equation_style` | **Available.** |
| 10 | LibreOffice headless render (`soffice --convert-to pdf`) | fallback (general capability signal only, never the Word-COM-only receipt) | local, **binary-dependent** | `render_gate.py::_soffice_render`, `_SOFFICE_BACKEND` | **Installed but UNDETECTED** by the shipped code in this environment — `PATH` gap (see §1). Direct-path probe confirms the binary itself works. |
| 11 | Word COM automation render (`Word.Application` via pywin32) | preferred (and the ONLY backend `check_word_com_render_receipt` / `word_com_render_check_required` accept) | local, **Windows + Office-license-dependent** | `render_gate.py::_word_com_render`/`_word_com_render_isolated`, `_WORD_COM_BACKEND` | **Available and confirmed working** right now (see §1) — `status: "rendered"`. |
| 12 | `check_render_capability` three-state gate (general, either backend) | required (at write-time, for the callers listed in §3) | local, backend-dependent | `render_gate.py::check_render_capability` | **Functions correctly**; resolves to word-com in this environment per §1. |
| 13 | Durable render-receipt ledger (`RenderReceipt`, `render_with_receipt`, `list_render_receipts`, `check_release_render_gate`) | n/a — not yet load-bearing anywhere | local | `render_gate.py` **on branch `mde-rework-44fc1ffe-536-2` (tip `b7e9b172`) only** | **Not present on `dev`.** Real, tested code exists on the branch but nothing in production (`docx_integrity_gate.py`, `docs_intel.py` on dev) calls it — see §4. |
| 14 | Hosted `DocStructureStore` / connector-backed document retrieval | required (when a document is only reachable via the hosted tier) | tunnel | (hosted Meridian service, outside this repo's local code) | **Not probed** — out of scope for a local-repo planning session; treat as tunnel-dependent by construction, never assume local availability. |
| 15 | `meridian-outputs` provenance registry (`record_provenance`/`get_provenance_status`) cross-check for a DOCX artifact | preferred | tunnel (MCP server call) | `docx_integrity_gate.py::RECIPE_CHECK_REGISTRY["outputs_provenance_check_required"]` | **Not independently probed this session** — registry entry documents the reference but nothing in `docx_integrity_gate.py` dispatches to it yet (see B67-2 finding: `RECIPE_CHECK_REGISTRY` names checks it never actually calls). |
| 16 | `docx_integrity_gate.build_docx_integrity_gate` composite verdict (render + equation-style + provenance-manifest) for `generate_handoff` | required-if-item-declares-`strict` policy, else advisory | local, orchestrates #9/#10-12 | `meridian/docx_integrity_gate.py` | **Available**, best-effort-imports the extension (`_import_optional_meridian_docs_submodule`); degrades to `available=False` (never a block) if the extension isn't installed in a given deployment. |
| 17 | Render-backend worker provisioning (installing/licensing LibreOffice or Office on a fresh host, fixing the `PATH` gap found in §1) | manual | manual | n/a | **Human decision** — not something this session may act on (explicit instruction: do not install software). |
| 18 | Renderer-selection policy (which backend is authoritative for a given release gate; whether `PATH` gets fixed vs. code learns well-known install paths) | manual | manual | n/a | **Human/architectural decision**, not yet made anywhere in the codebase. |
| 19 | Paper/2030-scope decisions referenced in the B67 proposal thread | manual | manual | n/a | **Human decision**, outside this item's scope. |

## 3. The equation-writer render-gating asymmetry (confirmed by line number, not just cited)

B67-2's finding — "only `insert_equation_local` gets the full post-write structural
re-verify + render-capability gate + CAS-safe restore-on-failure; edit/remove/append
siblings skip all three" — was independently re-confirmed this session by grepping every
call site of `_enforce_render_verification` in `extensions/meridian-docs/meridian_docs/docs_intel.py`
(dev) and mapping each line number back to its enclosing function:

| Line | Enclosing function | Family |
|---|---|---|
| 4651 | `insert_figure_block` | figure writer |
| 5270 | `remove_docx_package_part` | package-part writer |
| 5754 | `insert_docx_media_part` | media writer |
| 6201 | `insert_caption` | caption writer |
| **8778** | **`insert_equation_local`** | **equation writer** |
| 11645 | `insert_highlighted_note` | note/comment writer |
| 12389 | `merge_draft_into_canonical` | draft-merge writer |
| 14490 | `_write_table_mutation` (shared tail for `insert_column`/`split_cell`/`transpose_table`) | table writer |
| 15477 | `_set_page_header_or_footer` (shared tail for `set_page_header`/`set_page_footer`) | header/footer writer |
| 16053 | `highlight_document_matches` | highlight writer |
| 16245 | `insert_word_comment` | comment writer |

`edit_equation_local` (8822), `append_text_run_after_math` (8952), and
`remove_equation_local` (9081) — the other three members of the five-op equation-writer
family — have **no** call to `_enforce_render_verification` anywhere in their bodies.
`copy_section` (12806–13354) likewise has none (the nearest calls, 12389 and 14490,
belong to `merge_draft_into_canonical` and `_write_table_mutation` respectively, both
outside `copy_section`'s line range). This is the asymmetric gap exactly as B67-2
described it, now pinned to exact line numbers on the current `dev` HEAD.

`artifact_provenance` forwarding shows the same pattern: the parameter is accepted by
`insert_caption` (param at 5993) and `relocate_figure` (param at 13363), but **none** of
the five equation-writer ops or `copy_section` accept or forward it — so
`_check_artifact_provenance_binding` (2885), while real and callable, is structurally
unreachable from that family, exactly as B67-2 reported.

## 4. Render truth table — the three-state contract, and why a passing receipt is not forever

`render_gate.check_render_capability` (dev, unconditionally used by every
`_enforce_render_verification` call site above) returns **exactly one** of three states
— never a fourth, never a blend:

| Status | Meaning | Who can produce it |
|---|---|---|
| `rendered` | A real backend actually opened the file and produced visual output. **The only status that means "verified."** | Either backend, on success. |
| `unavailable-with-reason` | No backend exists in this environment. Says nothing about the document — an environment gap, not a document defect. | `detect_backend` finds nothing (e.g., this environment's `PATH`-blind soffice check plus a hypothetical machine with no Office/pywin32 either). |
| `failed` | A backend WAS available but this specific render attempt errored (timeout, transport, corruption, or unknown — `c44d245d`'s `error_class` classification). Never silently reported as `rendered`, never folded into `unavailable-with-reason`. | Either backend, on a real failure. |

`docs_intel.py::_enforce_render_verification` (2751) is the write-time enforcement of
this contract for the callers listed in §3:

- `rendered` → write stands, `render_verified: True`.
- `failed` → **fail closed**: `_safe_restore_after_verification_failure` restores from
  the pre-write backup (or, if a concurrent writer's own promotion has since landed,
  leaves the file untouched rather than destroy that work) and returns an error —
  `render_verified` is never set `True`.
- `unavailable-with-reason` → **also fails closed by default**, exactly like `failed`,
  for canonical/production promotion. The *only* escape is an explicit, audited pair:
  `allow_degraded_render=True` **and** a non-empty `degraded_render_reason` — and even
  then `render_verified` stays `False`; the payload is stamped `render_degraded: True`
  plus the reason, so no downstream caller can mistake a degraded acceptance for a real
  verification.

`meridian/docx_integrity_gate.py` composes the same three-state result (never
re-derives it) into a handoff-level verdict: `unresolved` is set **only** when
`render_status == "failed"` (a confirmed problem) — an `unavailable-with-reason` or a
missing checker never manufactures a finding, matching the module's stated philosophy
("can't confirm must never manufacture a finding").

### Receipt schema (MDE-7, branch `mde-rework-44fc1ffe-536-2`, tip `b7e9b172` — absent from `dev`)

A durable, append-only, atomic-JSON-write ledger (`_persist_receipt` → `os.replace`),
one `RenderReceipt` row per attempt:

```
receipt_id            str (uuid4)
docx_path             str
source_docx_sha256    str | None   — content hash of the file AT RENDER TIME
status                "rendered" | "unavailable-with-reason" | "failed"
backend                str | None
backend_version        str | None  — best-effort (soffice only)
backend_order          list[str]   — full priority order that was consulted
process_identity        dict | None — {"pid", "owned"} for word-com; None for soffice
pdf_sha256 / pdf_size_bytes / page_count   — None unless status == "rendered"
duration_seconds        float
attempts                int
timed_out                bool
error_class              str | None  — timeout / transport / corruption / unknown
field_refresh_status     str
visual_qa                dict        — {"status": "not_reviewed"|"not_applicable"|"verified", ...}
reason                    str | None
created_at / created_at_epoch
kind                      "render" | "degraded_override"
```

`check_release_render_gate(docx_path, receipts_path, max_age_seconds=86400, ...)` is the
consumer contract this ledger was built for: it only sets `release_ready=True` from a
receipt that is **simultaneously** (a) `status == "rendered"`, (b) `source_docx_sha256`
equal to the file's **current** on-disk content, and (c) newer than `max_age_seconds`.
Any one of those three failing means "no fresh matching receipt" — release refuses,
unless a human explicitly passes `allow_degraded_override=True` with a non-empty
`override_reason`, which itself writes a second, durably audited `kind:
"degraded_override"` receipt into the same ledger (never a silent bypass). It also
exposes a deliberately **separate, stricter** `visually_verified` flag — true only when
the matched receipt's own `visual_qa.status == "verified"` — so "a backend converted
this to a PDF" is never confused with "a human/automated visual QA pass actually
happened."

**This ledger is real, tested, and exactly matches the never-silently-verified rule
below — but as B67-2 already found, nothing on `dev` calls `render_with_receipt` or
`check_release_render_gate` today.** `docx_integrity_gate.py`'s `_enforce_render_verification`
equivalent on `dev` calls the older, stateless `check_render_capability` directly, with
no memory of prior attempts and no persisted evidence trail. Wiring MDE-7's ledger into
the production write/promotion paths is real, scoped follow-up work — explicitly **not**
implemented by this planning item.

## 5. The never-silently-verified rule

Stated once, for both today's stateless gate and MDE-7's not-yet-wired durable ledger,
because they are the same contract at two different points in the pipeline:

> **`unavailable` and `degraded` are never silently promoted to `verified`.**
> A render/visual-QA verdict is `verified` (or `render_verified: True`, or
> `visually_verified: True`) **only** when a real backend, in THIS environment, on the
> CURRENT content of THIS document, just produced real visual output. Every other
> outcome — no backend present, a backend that errored, a receipt that is stale, a
> receipt whose content hash no longer matches the file, or a receipt that only proves
> "a backend converted this" without a human/automated visual pass — must surface as
> `unavailable-with-reason`, `failed`, `render_degraded`, or `visually_verified: False`,
> **never** as an unqualified pass. The only way to *proceed* despite a non-`rendered`
> result is an explicit, non-empty, durably audited override
> (`allow_degraded_render` + `degraded_render_reason` today; `allow_degraded_override` +
> `override_reason` on MDE-7's ledger) — and even that override is recorded as
> `degraded`, never rewritten to look like a real verification after the fact.

Grounding, concretely:

- `docx_integrity_gate.py` already implements the read side of this rule today:
  `unresolved` is set only on a confirmed `render_status == "failed"`; a missing
  checker or an `unavailable-with-reason` result is a caveat, never a fabricated
  finding, and never a fabricated pass either — it is surfaced as `render_status` on
  the artifact entry, not silently dropped.
- `docs_intel.py::_enforce_render_verification` implements the write side: `unavailable`
  fails closed by default and only proceeds via an explicit, audited,
  reason-carrying override — never a default/implicit degrade.
- MDE-7's `RenderReceipt`/`check_release_render_gate` (branch-only, unconsumed today)
  extends the SAME rule across time: a receipt that was once `rendered` stops counting
  as fresh evidence the moment the document's content hash changes, or the moment it
  ages past `max_age_seconds` — an old exact-hash receipt is **historical evidence
  only**, never re-read as "currently verified" without a fresh check. This session's
  own §1 probe is a direct illustration: the Word-COM `rendered` result captured today
  is valid evidence for *today's* environment and *that exact scratch file's* content —
  it says nothing about tomorrow's environment, and nothing at all about any other
  document.
- The one place this rule is not yet enforced end-to-end is exactly the gap B67-2 named
  and §3 re-confirmed by line number: `edit_equation_local`, `append_text_run_after_math`,
  `remove_equation_local`, and `copy_section` never call `_enforce_render_verification`
  at all, so a write through any of those four paths carries **no** render-status field
  of any kind on its result — not `unavailable-with-reason`, not `failed`, not a degraded
  flag. That is a stronger gap than "degraded is mislabeled verified" (which does not
  happen for these four ops): it is "no render verdict is produced or recorded here at
  all," which is itself a violation of the spirit of the rule (a caller downstream has no
  way to know a check never ran) even though it does not violate the letter of the
  three-state contract for the checks that DO run elsewhere. Closing it means adding the
  same `_enforce_render_verification` call these four ops already have direct siblings
  for (`insert_equation_local`, `insert_caption`, etc.) — implementation work, out of
  scope for this planning item.

## 6. Summary for whoever picks up the next equation-writer/render item

- Fix candidates, in the order this matrix surfaces them: (1) extend
  `_enforce_render_verification` to the three equation-writer siblings and
  `copy_section` (closes the §3/§5 gap); (2) thread `artifact_provenance` through the
  same four ops so `_check_artifact_provenance_binding` becomes reachable; (3) wire
  MDE-7's `render_with_receipt`/`check_release_render_gate` into `dev`'s write/promotion
  paths so a durable, fresh, hash-matched receipt — not a one-shot stateless check —
  becomes the release evidence; (4) either add `C:\Program Files\LibreOffice\program`
  to `PATH` on hosts like this one, or extend `_soffice_unavailable_reason` to also
  probe well-known install locations, so a genuinely-installed LibreOffice stops being
  invisible to `detect_backend`.
- None of the above is implemented here — this item is the honest map, not the fix.
