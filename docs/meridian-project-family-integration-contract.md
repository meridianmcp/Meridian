# Project Family Integration Contract (Sprint Item ea49362c)

**Status:** Design only. This document defines an *integration contract* —
where and how project-family/template-revision state (as designed in sibling
item 5060eea1) would surface across the rest of the system if and when it is
implemented. **No behavior described here is wired today.** No route,
handler, MCP tool, or DB function changes as a result of this item.
`meridian/handoff.py`, `meridian/mcp/handlers/*.py`, `meridian/db/workspace.py`,
and `meridian/db/__init__.py::get_insights` are byte-for-byte unmodified by
this item — see the "Regression proof" section and
`tests/test_family_integration_contract_ea49362c.py` for the enforced,
not just claimed, guarantee.

**Builds on, does not duplicate:**
`docs/meridian-project-family-template-revisions-design.md` (item 5060eea1),
read in full before writing this contract. That document owns: the
revision-id scheme, content-hash algorithm, the two-layer effective-config
resolution, the diff format, versioning/supersession/rollback/conflict
semantics, the (unimplemented) schema sketch, the 16 Pydantic API shapes,
and the composition statement against legacy `parent_project_id`/north_star.
This document does not re-derive or restate any of that; it only asks and
answers: *if that design were implemented, how would every OTHER surface in
this codebase learn about it, safely and opt-in?*

**Parent item:** ddcf6984 ("project-family-templates"). **This item
(ea49362c)** scopes to the cross-cutting integration mapping only —
handoffs, proposals, insights, pointers, sprint items, executor refresh
context, and tenant authorization — per its own acceptance notes (quoted in
full at the end of this document).

**Grounding:** written in worktree `.claude/worktrees/a0c162f5` (branch
`worktree/ea49362c`) against dev tip `b0deb335` — the same commit item
5060eea1's design doc was merged at. A prior read-only discovery pass
(summarized inline throughout this document, with file/line citations)
confirmed the exact extension points this contract proposes already exist
as an established house pattern; nothing here invents a new mechanism.

---

## Vocabulary carried over from 5060eea1 (not redefined here)

`ProjectTemplate`, `TemplateRevisionSnapshot`, `ChildTemplateOverride`,
`ChildTemplateSnapshot`, `ProjectFamilyView`, `ConfigDiffEntry` — all from
`meridian/models.py`, all still unwired. See 5060eea1's design doc for full
field-level detail. This document refers to them by name and composes with
them; it does not add competing shapes for the same concepts.

One naming rule inherited from discovery and enforced throughout this
document: **the bare name `family_id` is never reused.**
`meridian/db/proposal_lineage.py` (module docstring, ~lines 40-44) already
defines `workspace_proposals.family_id` as a pre-existing, tenant-scoped,
*unrelated* proposal-lineage grouping tag —
*"`family_id` in particular remains a plain compatibility grouping field
[for proposal lineage]; it is never read or written by [the lineage]
module."* Everywhere this contract needs "the identifier of a project
family," it uses **`template_id`** instead — the family *is* "one template
plus its children" (5060eea1, `ProjectFamilyView`), so `template_id` is
already the correct, unambiguous identifier and requires inventing nothing
new.

---

## (a) How family state surfaces to a handoff receiver

**Mechanism: reuse the existing `emit_manifest` opt-in splice point.
A new opt-in flag, never a default-on change.**

`meridian/handoff.py` already has exactly the pattern this needs, used
twice today:

1. **`build_effective_capability_contract`** (handoff.py:8442) and
   **`build_effective_profile_binding`** (handoff.py:8541) — both thin,
   fully-`try/except`-guarded `async (db, project_id, ...) -> dict | None`
   wrappers. Each is called once inside `generate_handoff`'s dispatch
   (`mcp/handler.py`) and once inside `start_session`'s orientation
   response (`mcp/handlers/project_tools.py::handle_start_session`); the
   *caller*, not `generate_handoff` itself, attaches the result as a
   sibling field next to `content`. `generate_handoff`'s own return type
   (`tuple[path, content, amended]`, handoff.py:10655) is untouched by
   either wrapper's existence.
2. **`evidence_status`/`trusted_pointers`** (MDE-5, handoff.py:9691-9692,
   9710-9720) — an existing precedent for an *optional trailing block*
   inside `build_handoff_manifest`'s fixed-order dict: `None`/omitted by
   default, "byte-for-byte unchanged for every existing caller that
   doesn't pass them" (verbatim from that docstring).
3. **`emit_manifest`** itself (handoff.py:10651, default `False`) is
   already opt-in and, per its own docstring, "currently wired for
   `mode="goal"` only... a caller wanting the same guarantee for
   `full`/`delta`/`starter`/`compact` should build on the same primitives
   directly; this first pass intentionally covers one mode end-to-end
   rather than four modes partially." This item's own eventual
   implementation should follow that identical incremental path, not try
   to land family context in every mode simultaneously.

**Proposed future shape (illustrative, not implemented — see `models.py`
addition below):** a fourth wrapper,
`build_effective_family_binding(db, project_id, *, session_id=None) ->
dict | None`, added to `meridian/handoff.py` in the same file, next to the
other three wrappers, following the identical signature/guard/return
convention. Internally it would:

- Look up whether `project_id` has a `ChildTemplateSnapshot` row (future
  `db.get_child_template_snapshot`, not implemented — 5060eea1 section j).
- If none: return `None` — the "no family" case, handled identically to
  how `build_effective_profile_binding` returns `None` on any failure.
- If present: build and return a dict shaped like `HandoffFamilyContext`
  (see the models.py section below) — `template_id`, `adopted_revision_id`,
  `latest_revision_id`, `inherited_vs_local` (a `list[ConfigDiffEntry]`,
  reused verbatim from 5060eea1, never a new diff shape),
  `executable_capability_status`/`executable_reasons` (reusing the SAME
  vocabulary `build_effective_capability_contract` already emits — not a
  new status enum), and `pending_promotion_revision_ids`.

**Call-site wiring (future work, NOT this item):**

- `generate_handoff` gains one new keyword-only parameter,
  **`include_family_context: bool = False`** — mirroring `emit_manifest`'s
  own default-`False`, opt-in shape exactly. `mcp/handler.py`'s
  `generate_handoff` dispatch calls
  `build_effective_family_binding` only when the caller passed
  `include_family_context=True`, then attaches the result as a sibling
  field (`family_binding`) next to `content` — exactly how
  `capability_contract`/`profile_binding` are attached today. `generate_handoff`
  itself never grows a `family_binding` entry in its own return tuple.
- `build_handoff_manifest` gains one new optional trailing kwarg,
  **`family_binding: dict[str, Any] | None = None`**, defaulting to `None`
  and rendered as `dict(family_binding or {})` in the returned dict —
  the identical pattern `evidence_status`/`trusted_pointers` already use
  two lines above where this key would sit. `serialize_handoff_manifest_xml`
  gains one new optional trailing XML block, emitted only when the dict key
  is non-empty (again mirroring the evidence/pointers precedent).
- `start_session`'s orientation response gains the same
  `include_family_context` param, calling the same
  `build_effective_family_binding` wrapper, for parity between the two
  trusted channels — exactly how capability-contract and profile-binding
  are already surfaced identically at both call sites today.

---

## (b) Default (no flag) handoff output is byte-identical to today

**Statement:** with `include_family_context` omitted (or explicitly
`False`), `generate_handoff`'s returned `(path, content, amended)`, the
`start_session` orientation response, and `build_handoff_manifest`'s
returned dict and its XML serialization are **byte-for-byte identical** to
their pre-this-item behavior — for a project with a family, without one,
and everywhere in between.

**Justification:**

1. **No existing default changes.** `include_family_context` (future work)
   would be a *new* keyword-only parameter with default `False`. A
   parameter that does not exist today cannot be passed by any existing
   caller; once it exists, every caller that never learns about it keeps
   passing nothing, which resolves to the default. This is the exact
   mechanism `emit_manifest` itself already relies on (default `False`,
   "zero behaviour change for every existing caller" — handoff.py:10658).
2. **The wrapper only runs when asked.** `build_effective_family_binding`
   (future work) would only be *called* when `include_family_context=True`
   — unlike `capability_contract`/`profile_binding`, which run
   unconditionally today (they were unconditional from day one, so there
   was never a "before" to preserve byte-for-byte). Family context is
   being introduced into an *existing, shipped* surface, so it must be
   strictly additive from its first line of code — hence gating the call
   itself, not just gating what happens with the result.
3. **The manifest's fixed key order is preserved.** `build_handoff_manifest`
   returns a dict with a fixed, enumerable key set (handoff.py:9726-9747).
   Adding `"family_binding": dict(family_binding or {})` as the LAST key
   (after `trusted_pointers`) means: (i) every existing key stays at its
   existing position, (ii) the new key's value is `{}` whenever the caller
   doesn't pass `family_binding` (which is every existing caller, by
   construction), and (iii) `serialize_handoff_manifest_xml`'s existing
   `esc()`-and-fixed-order emission only appends a new XML element when
   that dict value is non-empty — an empty dict must render nothing, not
   an empty tag, to stay byte-identical to the pre-family XML for a
   family-less project. **Concrete regression check (see `test_family_integration_contract_ea49362c.py`):**
   this item asserts the *current* `build_handoff_manifest`'s returned key
   set is exactly `{schema_version, handoff_mode, project_id, project_name,
   sprint_version, session_id, origin_identity, generated_at,
   board_revision, selected_item_ids, closure_item_ids, items,
   items_truncated, items_total, waves, stop_conditions, deploy_policy,
   evidence_status, trusted_pointers}` — no `family_binding` key exists yet.
   A future implementation item that adds the key must update that exact
   assertion, which makes an accidental default-on change to the manifest
   shape fail CI immediately rather than silently ship.
4. **Enforced, not just claimed:** this item makes ZERO edits to
   `meridian/handoff.py`. `tests/test_family_integration_contract_ea49362c.py`
   includes a `git diff <dev-tip> -- meridian/handoff.py` check (and the
   same for `meridian/mcp/handlers/`, `meridian/db/workspace.py`, and
   `meridian/db/__init__.py`) that fails the moment any of those files
   differ from the commit this contract was written against. Combined with
   a real, executed call to `generate_handoff` against a project with no
   family (mirroring `tests/test_handoff_amend_vs_fresh.py`'s existing
   fixture pattern), this is a *behavioral* proof, not a documentation-only
   promise — see "Regression proof" below.

---

## (c) Proposals / insights integration — the authorization boundary

**Two structurally different data stores, two different exposure risks:**

1. **`get_insights(db, project_id, horizon=None)`** (db/__init__.py:8739)
   and `add_insight`/`get_insights` MCP handlers
   (`mcp/handlers/notes_decisions.py:2083`/`2103`) are already strictly
   `project_id`-scoped — one table, no tenant/family join today. This is
   the *safe* case: a future `include_family_context` param on
   `handle_get_insights` (mirroring `get_sprint_items`'s `version=`
   precedent, see (d) below) could, at most, ALSO surface a read-only
   family summary (`template_id`, `adopted_revision_id`, sibling count —
   never sibling insight bodies) alongside the still-`project_id`-scoped
   insight rows. **It must never resolve or join against a sibling child's
   `project_id`** — an insights call for child A stays scoped to child A's
   own insights, full stop; family membership is metadata attached to the
   response, never a key used to fetch more rows than the caller asked for.
2. **`workspace_proposals`** (`db/workspace.py`: `add_workspace_proposal`
   L539, `get_workspace_proposals` L747, `promote_workspace_proposal`
   L936) are **tenant-scoped, not project-scoped at all** — no
   `project_id` column exists on this table today. This is the *dangerous*
   case flagged by discovery: `workspace_proposals` already has an
   unrelated `family_id` column (proposal-lineage grouping, see vocabulary
   section above). **A naive future implementation that filters
   `workspace_proposals` by matching `family_id` to a project-family
   `template_id` would be a real cross-tenant-concept bug**, not a data
   leak by itself (the column means something else entirely), but it would
   silently produce nonsense results and must not happen. The correct
   integration point, if a family-scoped proposal view is ever built, is
   `promote_workspace_proposal` — discovery confirms this is already "the
   one explicit, one-directional bridge from workspace scope into project
   scope: it creates a concrete `sprint_items` row under one `project_id`."
   A future "promote this proposal into every child of family X" operation
   would call this existing bridge once per resolved child `project_id`
   (each promotion still one-directional, workspace → one project), never
   introduce a new ambient workspace-to-family join.

**The authorization check itself — reusing this project's real permission
model, not inventing one:**

- `meridian/roles.py::has_perm(role, perm)` (line 86) against
  `PERM_READ`/`PERM_WRITE`/`PERM_SETTINGS` (lines 41-47) is the single
  source of truth for what a workspace role may do. This contract
  introduces no new permission constant. Reading family context
  (`template_id`, `adopted_revision_id`, diff, capability status) is a
  **read** — gated by `PERM_READ`, same as any other project-data GET.
  Any future *mutating* family action (adopt/reject/override/rollback,
  from 5060eea1 section k) is a **write** — gated by `PERM_WRITE` at
  minimum, following `_deps.py::_required_perm_for_request`'s existing
  default ("ordinary project-data writes → member allowed, viewer
  blocked", line ~872-874) rather than inventing a new tier. A future
  family *administration* action that could affect every child of a
  template at once (e.g., forcing every child onto a new revision) is the
  one case that plausibly deserves `PERM_SETTINGS` instead of plain
  `PERM_WRITE` — analogous to how `_required_perm_for_request` already
  elevates workspace/project *configuration* changes to `PERM_SETTINGS`
  while ordinary data writes stay at `PERM_WRITE` (line ~863-868). This
  contract flags that choice as open (see "Deferred" section (h)) rather
  than deciding it, since no such bulk-template-push operation is even
  designed yet (5060eea1 has no such API shape).
- **Cross-workspace enforcement is `_enforcement_context`/
  `_require_workspace_perm`** (`meridian/_deps.py:877`/`802`) for HTTP
  routes, and **`scoped_project_ids`** (`mcp/handler.py:1163`,
  `tools/call` dispatch ~lines 1343-1358) for MCP/API-token callers. Both
  are **single-`project_id`-per-call** checks today. **This is the genuine
  gap this contract must flag, not paper over:** any future tool or route
  that returns data spanning *multiple* child projects in one call (a
  `ProjectFamilyView`-shaped response, or a "family proposals" aggregate)
  cannot rely on either existing mechanism as-is — `scoped_project_ids`
  checks exactly one `args["project_id"]`, and a family aggregate by
  definition touches several. **Required fix for any such future tool
  (not implemented here):** resolve every child `project_id` the
  aggregate would touch (via `ChildTemplateSnapshot.child_project_id`,
  5060eea1 section j — the only place child membership is recorded), then
  check EACH one against `scoped_project_ids` (when not `None`) before
  including that child's data in the response — filtering out, not
  erroring on, any child outside scope, matching how `get_sprint_items`
  filters rather than 403s when a status filter excludes rows. A
  project-scoped API token must see the family's *shape* (it can learn a
  family boundary exists) without ever receiving a sibling project's
  decisions, notes, insights, or proposal bodies it could not fetch
  directly.
- **The second, unenforced path** (`_scoped_project_ids_for_request`,
  `_deps.py:428`, backing the cookie-session dashboard's project-scoped
  `workspace_members.project_id` members) is explicitly **listing-only,
  not airtight** today — its own docstring says so, gated on open product
  decision `pin b11c7cf6`. **This contract does not close that gap and
  does not pretend a family view built on top of it would be safe from
  direct-by-ID access** — a family aggregate surfaced through the
  dashboard for a project-scoped cookie-session member inherits the
  SAME pre-existing weakness as everything else behind that path, not a
  new one this item introduces. Any future work that wants family
  aggregates to be safe for project-scoped dashboard members must wait on
  (or explicitly complete) `pin b11c7cf6` first — this contract flags
  that dependency rather than silently assuming the read is safe.

**Net leakage statement:** with the design above, a caller who can only
reach one child project (by either enforcement mechanism) can learn (a)
that a family exists, (b) that child's own place in it (`template_id`,
`adopted_revision_id`, its own diff/capability status), and (c) the
template's `latest_revision_id`/name (both already `ProjectTemplate`-level,
not another project's data). They can never receive another sibling
child's `ChildTemplateSnapshot`, override, notes, decisions, or insights
through any family-shaped surface — only through whatever direct access to
that sibling's `project_id` they already had or lacked before this item
existed.

---

## (d) MCP tool surfaces gain new OPTIONAL params — the `version=` precedent

**Exact precedent to model:** `get_sprint_items(db, project_id, status=None,
..., version: str | None = None, ...)` (db/sprint_items.py:4338) —
`version=None` preserves 100% of current behavior (all versions returned);
an explicit value narrows scope. This is the established "None = unchanged
today, explicit = opt-in" shape this contract reuses verbatim for family
context, on every surface below:

| Surface | New optional param | Default | Behavior when omitted/default |
|---|---|---|---|
| `generate_handoff` (handoff.py:10619) | `include_family_context: bool` | `False` | Identical to today (see (a)/(b)) |
| `start_session` orientation (`project_tools.py::handle_start_session`) | `include_family_context: bool` | `False` | No family field attached |
| `handle_get_insights` (`notes_decisions.py:2103`) | `include_family_context: bool` | `False` | Returns exactly today's insight list, no wrapper field |
| `handle_add_note` / `handle_get_notes` (`notes_decisions.py:339` / nearby) | *(none proposed)* | n/a | Notes are already never family-aware in this design — see (c); no param needed because no behavior changes |
| `handle_get_sprint_items` / `handle_get_parallelizable_groups` (`sprint_tools.py:745`/`784`) | `include_family_context: bool` | `False` | Item list unaffected; when `True`, each item's response MAY additionally carry `blocked_by_family` context only for items already flagged as blocked for template/config reasons — not implemented, illustrative only |
| `refresh_context` / `checkpoint` | `include_family_context: bool` | `False` | Refresh payload unaffected by default |

**Existing inconsistency noted, not fixed here:** discovery confirms
`handle_get_sprint_items` (sprint_tools.py:745) does not even forward the
already-shipped `version=` kwarg today (only `handle_get_parallelizable_groups`,
line 784, does). This contract explicitly does NOT fix that inconsistency —
out of scope for a design-only item — but flags it as a concrete reason a
future family-context implementation must audit each handler's kwarg
forwarding individually rather than assuming "the pattern is already
applied uniformly everywhere it should be."

---

## (e) Graceful behavior for a project with no family

For every surface in (a)/(d), when `include_family_context=True` is passed
for a project with **no** `ChildTemplateSnapshot` row:

- The wrapper (`build_effective_family_binding`, future work) returns
  `None` — same convention as `build_effective_profile_binding` on any
  failure, and the SAME convention 5060eea1 section (c) rule 5 already
  establishes for effective-config resolution ("a child with no adopted
  revision at all... is a precondition failure for resolution, not
  `resolve against nothing`" — the *wrapper* layer converts that
  precondition failure into a clean `None`, exactly like every other
  best-effort wrapper in `handoff.py` already does for its own failure
  modes).
- The caller (handoff dispatch / start_session / an MCP handler) omits the
  field entirely rather than attaching `null` or `{}` — matching how
  `capability_contract`/`profile_binding` are already omitted, not
  nulled, when their own wrapper returns `None`.
- **No different code path is taken for the common case.** The `if
  include_family_context:` branch itself is the only new branch; whether
  the project has a family or not is decided *inside* the wrapper, which
  is exactly how `build_effective_profile_binding` already decides
  "workspace-only vs. project+workspace+hosted_default" internally without
  the caller needing to know in advance which case applies.
- `HandoffFamilyContext` (illustrative model, see below) itself is never
  even constructed for a family-less project — there is no "empty"
  instance serialized; the field is simply absent from the response,
  matching (b)'s byte-identical guarantee for the truly-common,
  overwhelming-majority case of a project that never touches any
  family/template concept.

---

## (f) API / tool backward-compatibility statement

Every parameter this contract proposes is **new, keyword-only, and
defaults to `False`/`None`**. No existing parameter changes name, type,
position, or default. No existing return shape drops, renames, or
reorders a field. Concretely:

- An old MCP client that has never heard of `include_family_context` sends
  no such argument; every proposed handler treats a missing keyword
  identically to an explicit `False`/`None` — standard Python keyword-arg
  semantics, not a new compatibility shim.
- An old REST client parsing `generate_handoff`'s JSON response, or
  `build_handoff_manifest`'s XML, sees no new key/element unless it
  explicitly opted in — see (b)'s manifest key-set assertion.
- `get_sprint_items`/`get_insights`/etc.'s *existing* optional parameters
  (`status`, `horizon`, `version`, ...) are untouched — position, name,
  and default all identical before and after this item, and before and
  after any future implementation that follows this contract.
- This item itself changes zero of the above — see "Regression proof."

---

## (g) Test matrix

One row per (surface × scenario). All rows describe tests a future
*implementation* item would need; none of them exist as passing tests for
new behavior today, because no new behavior exists today (rows marked
**[ENFORCED NOW]** describe assertions that ARE already in
`tests/test_family_integration_contract_ea49362c.py`, proving the *current*
baseline these future tests would diff against).

| # | Surface | Scenario | Expected outcome |
|---|---|---|---|
| 1 | `generate_handoff` | No family, default call (no flag) | **[ENFORCED NOW]** Byte-identical to pre-this-item baseline (git-diff-verified zero change to `handoff.py`; real call succeeds, returns `(path, content, amended)` with no family-shaped keys anywhere in `content`) |
| 2 | `generate_handoff` | No family, `include_family_context=True` | `family_binding` absent/`None`; no error; no different item-selection code path taken |
| 3 | `generate_handoff` | IS a family child, default call (flag omitted) | Output identical to scenario 1 — family membership alone must never change default output |
| 4 | `generate_handoff` | IS a family child, `include_family_context=True` | `family_binding` present: `template_id`, `adopted_revision_id`, `latest_revision_id`, `inherited_vs_local`, `executable_capability_status`, `pending_promotion_revision_ids` all populated and consistent with the child's `ChildTemplateSnapshot`/`ProjectTemplate` rows |
| 5 | `generate_handoff`, `mode="goal"`, `emit_manifest=True` | No family | **[ENFORCED NOW]** `build_handoff_manifest`'s returned key set is exactly the current 19 keys — no `family_binding` key exists; XML serialization unaffected |
| 6 | `generate_handoff`, `mode="goal"`, `emit_manifest=True` | Family child, `include_family_context=True` | Manifest gains exactly one new trailing key/XML element (`family_binding`); every existing key/element keeps its current position; `body_hash`/goal-token verification still covers the new block (spliced before token minting, same as `evidence_status`/`trusted_pointers`) |
| 7 | `start_session` orientation | No family | No family field present, response identical to pre-item baseline |
| 8 | `start_session` orientation | Family child, `include_family_context=True` | Family field present, SAME shape `generate_handoff` emits (parity between the two trusted channels) |
| 9 | `load_handoff` / stored `pending_goal` | Family child, generated WITHOUT `include_family_context` | Retrieved handoff bytes contain no family data — opting in is a generation-time choice, not inferred at read time |
| 10 | `get_insights` | No family, default call | **[ENFORCED NOW]** Unaffected — `db.get_insights` signature/behavior byte-identical (git-diff-verified) |
| 11 | `get_insights` | Family child, caller has `PERM_READ` (any role) | Family summary metadata (`template_id`, `adopted_revision_id`) MAY be attached; the insight rows themselves are still exactly this project's own, never a sibling's |
| 12 | `get_insights` via a family-aggregate path (hypothetical future tool) | Caller's `scoped_project_ids = ["A"]`; family also contains sibling child `"B"` | Child `B`'s insights are excluded from any aggregate response — never returned via a family join the caller could not reach directly |
| 13 | `workspace_proposals.family_id` (pre-existing, proposal-lineage) | Any family-context code path is introduced | Column's existing read/write behavior in `db/workspace.py`/`proposal_lineage.py` is untouched — **[ENFORCED NOW]** git-diff-verified zero change to `db/workspace.py`; a future implementation must never filter/join this column against a project-family `template_id` |
| 14 | `promote_workspace_proposal` | A proposal promoted to every child of a family (future op, not implemented) | Bridge is called once per resolved child `project_id`; each call remains the existing one-directional workspace→project promotion; no new ambient workspace-to-family join is introduced |
| 15 | `handle_add_note` / `handle_get_notes` | `include_family_context` never added to this surface (per (d)) | Behavior and signature identical before/after this item and any future family work — **[ENFORCED NOW]** no new param exists on these handlers today |
| 16 | `get_sprint_item_pointers` / `resolve_sprint_item_pointers` | No family | Unaffected |
| 17 | `get_sprint_item_pointers` | Family child, hypothetical future `include_family_context=True` | Pointer resolution stays scoped to THIS child's `project_id` only — never resolves a pointer belonging to a sibling child sharing the same `template_id` |
| 18 | `get_sprint_items(project_id, version=None)` | Combined with a hypothetical future `family_scope=None` | Returns identical set to `version=None` alone — a new family-scoped kwarg must follow the identical "`None` = unchanged" contract `version=` already established |
| 19 | `handle_get_sprint_items` | Before any future family-context change | **[ENFORCED NOW]** documented existing gap: this handler does not forward `version=` today (unlike `handle_get_parallelizable_groups`) — a future patch adding `include_family_context` must not silently "fix" `version`-forwarding as an undocumented side effect |
| 20 | `claim_sprint_item` / `complete_sprint_item` | Family-scoped project, `code_intel_prospecting` capability opted in | Capability-manifest/prospecting-receipt logic is completely orthogonal to family context; item behaves identically whether or not the project has a family |
| 21 | `refresh_context` / `checkpoint` | No family | Unaffected |
| 22 | `refresh_context` | Family child whose `latest_revision_id` has advanced past `adopted_revision_id` | MAY surface a non-blocking informational note ("template X has a newer revision available"); never auto-adopts, never blocks or fails the refresh |
| 23 | MCP `tools/call` dispatch | `scoped_project_ids=["A"]`, tool call targets `project_id="A"` (a family child) | Passes today's existing single-project check unchanged — no new gate needed for a single-project read |
| 24 | MCP `tools/call` dispatch | `scoped_project_ids=["A"]`, hypothetical future family-aggregate tool call spanning `["A","B"]` | Must filter out `B`'s data (the flagged gap in (c)) — a test asserting this is the one case existing enforcement does NOT already cover, so any future aggregate tool must add the per-child scope check explicitly before shipping |
| 25 | HTTP route, cross-workspace header, `role="viewer"` | Requests read-only family context on a handoff/insights endpoint | Allowed — `PERM_READ` suffices, matching `_required_perm_for_request`'s existing read/write split |
| 26 | HTTP route, cross-workspace header, `role="viewer"` | Requests a hypothetical future mutating family action (adopt/reject/override) | Denied — mutating family actions require `PERM_WRITE` at minimum, same as any other project-data write today |
| 27 | Cookie-session dashboard, project-scoped `workspace_members.project_id` member | Requests a family aggregate view | Contract explicitly does NOT claim this is airtight — inherits the SAME pre-existing `pin b11c7cf6` listing-only gap as every other project-scoped dashboard read; a test documents this as an open dependency, not a false "safe" claim |

**Rows marked [ENFORCED NOW]** (1, 5, 10, 13, 15, 19) are executable today
because they assert facts about the *current, unmodified* codebase — they
are exactly the tests this item ships in
`tests/test_family_integration_contract_ea49362c.py`. Every other row
describes a test a future *implementation* item must add once the
corresponding behavior actually exists; none of them can be written
meaningfully before that behavior is wired, since there is nothing yet to
call.

---

## (h) Deferred / explicitly NOT decided by this contract

This contract intentionally leaves the following open for whichever future
item implements against it:

1. **Exact new MCP tool names** for `create`/`fork`/`override`/`preview`/
   `adopt`/`reject`/`rollback` (5060eea1 section k already names the
   request/response *models*, not the tool names that would dispatch them).
2. **Exact parameter name/type for the opt-in flag** — this document uses
   `include_family_context: bool = False` throughout as the working name,
   but a future item could reasonably choose an enum
   (`"none"|"summary"|"full"`) instead of a bool if a partial-detail mode
   turns out to be useful. Not decided here.
3. **Whether `HandoffFamilyContext` (below) is the actual shape shipped**,
   or purely illustrative. In particular `executable_capability_status`'s
   three-value vocabulary (`executable`/`non_executable`/`unknown`) is a
   sketch, not verified against whatever the real
   `build_effective_capability_contract` availability states turn out to
   need once capability checking is extended to cover template-level
   requirements (if it ever is — not designed here either).
4. **Rollout order** — whether `include_family_context` lands for
   `generate_handoff` first (mirroring `emit_manifest`'s own
   `mode="goal"`-only first pass) or for `get_insights` first, or
   simultaneously. Not decided; house precedent (emit_manifest) favors one
   surface at a time.
5. **The exact mechanic for the multi-child authorization filter** in (c)/
   row 24 — whether `scoped_project_ids` is checked per-child inline
   during aggregation, or family membership is resolved once and
   intersected with `scoped_project_ids` up front. Both are correct;
   which is more efficient depends on typical family size, which is
   unknown (no families exist yet).
6. **Whether a bulk "push every child onto a new revision" operation ever
   gets `PERM_SETTINGS` instead of `PERM_WRITE`** (see (c)) — flagged as
   open because no such operation is designed in 5060eea1 at all.
7. **Payload size caps, migration specifics, and everything else 5060eea1
   section (j)/(i) already marks as future work** — this document does
   not re-decide any of that; it only adds the cross-cutting surfaces
   5060eea1 explicitly deferred to a sibling item.
8. **Whether `refresh_context`/`checkpoint` ever actually gain family
   awareness at all** — row 21/22 in the test matrix describe a plausible
   future behavior, not a committed one.

---

## Regression proof — this item changes no existing behavior

Enforced by `tests/test_family_integration_contract_ea49362c.py`:

1. **File-level zero-diff assertions** (`git diff b0deb335 -- <path>`)
   against `meridian/handoff.py`, every file under `meridian/mcp/handlers/`,
   and `meridian/db/workspace.py` — all must show zero changes relative to
   the dev tip this contract and 5060eea1 were both written against.
2. **`meridian/db/__init__.py::get_insights`** — the function's source text
   is extracted and compared verbatim against the same function at
   `b0deb335`, isolating the specific carve-out this item's own scope
   names (rather than requiring the entire, large `db/__init__.py` file to
   be untouched by every other concurrent item).
3. **A real, executed `generate_handoff` call** (mirroring
   `tests/test_handoff_amend_vs_fresh.py`'s fixture pattern: real `db`
   fixture, `db_module.create_project`, `db_module.set_goal`,
   `handoff_module.generate_handoff(..., skip_ai_summary=True)`) against a
   project with no family, in THIS worktree's code, asserting the call
   succeeds and returns the expected `(path, content, amended)` shape with
   no family-shaped substrings anywhere in the rendered content.
4. **A direct call to `build_handoff_manifest`** with minimal required
   arguments, asserting its returned dict's key set is exactly the 19 keys
   enumerated in (b) above — no more, no fewer.
5. **Existing handoff test files** (`tests/test_handoff_amend_vs_fresh.py`,
   `tests/test_682005f4_goal_only_handoff.py`, `tests/test_capability_contract.py`)
   were re-run, unmodified, alongside this item's own new test file — see
   the session's own test-run report for pass counts.

---

## Item ea49362c's acceptance notes (verbatim, for traceability)

> Map integration into proposals, insights, pointers, sprint items,
> handoffs, executor refresh context, and tenant authorization. The
> default handoff must remain project-scoped; family context is opt-in and
> bounded, with no workspace-wide decision/note leakage. Define how a
> receiver sees family_id, child_id, template_revision, inherited-vs-local
> provenance, executable capability status, and pending promotion
> decisions. Include API/tool compatibility and graceful behavior for
> projects with no family. Do not change handoff behavior in this planning
> item; produce an integration contract and test matrix.
