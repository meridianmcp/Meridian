# Tigris tenant/project access policy — PLAN, not activation

**Status: Tigris/S3 is INACTIVE (unchanged).** This document is the
prospecting/planning deliverable for sprint item `54fcf116-602d-4e28-9cae-db145f992a88`.
It designs the tenant/project rights model, provisioning flow, quotas, and
lifecycle boundaries that a *future* `TigrisObjectStoreBackend` activation
would need — it does not implement any of it, does not touch credentials,
and does not write production data. Nothing in this document changes any
runtime behavior.

It builds directly on `docs/object-storage-backend.md` (the accepted
design for the Protocol/backends/sync-state that already shipped) rather
than re-deriving that material. Read that document first; this one adds
the tenant/project access-control layer it deliberately left as `[OPEN]`
or out of scope.

## 0. Baseline: how the existing Postgres-backed tenant/project boundary
   actually works today

Any access-control design for Tigris has to either match or explicitly
diverge from the enforcement this codebase already relies on for its
Postgres-backed resources (`projects`, `sprint_items`, `workspace_*`, …).
Traced via `meridian/roles.py`, `meridian/db/workspace.py`, `meridian/_deps.py`,
and `meridian/hosted.py`:

1. **Physical isolation, hosted mode.** Each tenant gets its *own* Neon
   Postgres database (`tenants.neon_db_url`, provisioned in `hosted.py`,
   decrypted per-tenant via `meridian/tenant_crypto.py`'s HKDF-derived Fernet
   key salted with `tenant_id`). `_db(request)` in `_deps.py` resolves which
   physical database a request hits. This is the *strongest* isolation
   layer in the system today, and it is physical, not row-level: one
   tenant's project data lives in bytes a different tenant's connection
   string cannot even address.
2. **Shared control-plane DB, row-scoped.** `workspace_notes`,
   `workspace_decisions`, `workspace_proposals`, `workspace_members` live in
   one shared auth/control-plane DB, scoped by a `tenant_id` column via
   `_ws_tenant_clause` (`db/workspace.py`) — `tenant_id = ? OR tenant_id IS
   NULL` (the `NULL` branch exists only for pre-isolation rows on a
   dedicated per-tenant DB, per that function's docstring).
3. **Role-based write gating, cross-workspace only.** `roles.py` defines
   the only 4 real roles (`owner`/`admin`/`member`/`viewer`) and their
   permission sets (`ROLE_PERMS`). `_deps.py::_required_perm_for_request`
   maps a mutating path to the permission a role needs; `_enforcement_context`
   resolves `(caller_email, active_workspace_tenant_id, role)` **only when**
   an `X-Workspace-Tenant-Id` header names a workspace different from the
   caller's own (`_enforcement_context` docstring: "Role enforcement only
   matters cross-workspace... every tenant is implicitly owner of their own
   workspace"). Self-hosted, demo, same-workspace, and solo-owner requests
   pay no role-check cost and see no gate — they're already the owner.
4. **Project-level scoping is listing-only, not a hard boundary.**
   `get_scoped_project_ids_for_member` / `_scoped_project_ids_for_request`
   (`db/workspace.py`) filter what a *project-scoped invited member* sees in
   a **list**. Both functions' docstrings say this explicitly: "This is
   listing-only scoping. It does NOT block direct-by-ID access to other
   projects in the same workspace — airtight per-request enforcement is
   deferred pending the open product decision (pin `b11c7cf6`)." Writes are
   still gated by role (`393eed0a`), but a project-scoped member can address
   a sibling project by id today and nothing in `roles.py`/`_deps.py` stops
   the read.

**Net effect that matters for Tigris:** cross-*tenant* isolation for
project data is primarily **physical** (separate Neon DB per tenant, not a
shared table anyone could theoretically mis-scope) plus a role check that
only activates when someone crosses into another tenant's workspace.
Cross-*project* isolation within one tenant is **not** physical and is not
even a hard logical boundary yet — it's an acknowledged, open gap
(`b11c7cf6`). A Tigris design that puts every tenant's objects in one
shared bucket under path prefixes is architecturally closer to case 4
(logical, prefix-based, not physically separate) than to case 1 (physically
separate databases) — see §1 and §5.

## 1. Tenant/project rights model

**Decision: reuse `roles.py` verbatim. No new permission.**

Object read/write/delete map onto the existing `PERM_READ` / `PERM_WRITE`
exactly like every other ordinary project-data mutation already does via
`_required_perm_for_request`'s default branch (any path not matching one of
the special-cased prefixes — team management, settings, whole-project
delete — falls through to `PERM_WRITE` for POST/PUT/PATCH/DELETE). A future
`/projects/{project_id}/objects/...`-shaped route needs **no new entry** in
`_required_perm_for_request` to get write-gated correctly; it would only
need one if object routes should require something *stricter* than the
default (e.g. gating delete at `PERM_SETTINGS` instead of `PERM_WRITE` —
an open question, see §6).

| Role | Read object | Write/put object | Delete object |
|---|---|---|---|
| owner | yes | yes | yes |
| admin | yes | yes | yes |
| member | yes (`PERM_READ`) | yes (`PERM_WRITE`) | yes (`PERM_WRITE`) |
| viewer | yes (`PERM_READ`) | **no** | **no** |

Caveats, both **inherited, not introduced** by this design:

- **GET/list routes have no established role-check precedent in this
  codebase to begin with.** `roles.py`'s permission model is enforced by
  `_deps.py` only for *mutating* requests (`_required_perm_for_request`
  early-returns `None` for anything but POST/PUT/PATCH/DELETE). Existing GET
  routes (e.g. `routes/files.py::get_project_file`) check only "does the
  project exist," not role. A future object GET/list route would, by
  precedent, get the same treatment — but this is a policy choice this
  document surfaces rather than resolves (§6, open item 7).
- **Project-scoped members inherit the `b11c7cf6` gap unchanged.** Nothing
  about object storage lets this design close that gap; a project-scoped
  member's request naming a sibling project's `project_id` in an object key
  would be served exactly as readily as the existing gap allows for any
  other project-scoped resource today. Do not describe a Tigris route as
  "more secure" than the rest of the API — it would inherit the identical
  listing-only enforcement unless a *new*, object-storage-specific hard
  check is deliberately built (out of scope here; flagged as an open
  decision in §6).

**Tenant boundary — key construction, not row filtering.** Every key is
built through `object_store.build_object_key(project_id, artifact_class,
content_hash, tenant_id=...)`. The tenant boundary is enforced by *who is
allowed to construct which key*, not by a queryable column:

- `tenant_id` must always be resolved **server-side** via
  `db.get_tenant_id_for_project(db, project_id)` — the same function
  `routes/projects.py` already uses for tunnel-slot resolution — never
  accepted as a client-supplied argument. **This is not yet enforced by the
  Protocol itself**: `build_object_key`'s `tenant_id` parameter is only
  validated for *character safety* (`_safe_component`), not for "does this
  tenant actually own this project." Nothing in `object_store.py` today
  calls `build_object_key` from a network-facing path (correctly — no
  caller exists yet), so this is a **requirement for whoever wires the
  first real caller**, not a bug in what shipped. Flagged as open item 6 in
  §6.
- Self-hosted (no tenant concept, no `workspace_members` table populated) —
  keys have no `tenants/` prefix, identical to today's `LocalObjectStoreBackend`
  behavior. No change needed.

## 2. Provisioning flow

**There is no per-tenant bucket, credential, or ACL to provision under the
prefix-per-tenant design already recommended (but still `[OPEN]`) in
`docs/object-storage-backend.md`.** This is a deliberate simplification,
not an oversight, and it is architecturally different from how this same
codebase provisions Neon:

- **Neon (existing, for comparison):** `hosted.py` provisions a whole new
  Neon *project* + database + role + connection string per tenant on
  signup, and `_drop_tenant_neon_database` tears down that same real,
  separately-addressable resource on account deletion. Each tenant's
  Postgres data is physically unreachable from another tenant's credentials.
- **Tigris (prefix-per-tenant, this design):** one bucket, one set of
  access-key credentials, shared by every tenant. "Provisioning" a new
  tenant/project is a no-op beyond `build_object_key` producing a
  well-formed `tenants/{tenant_id}/{project_id}/...` prefix the first time
  that tenant's project ever writes an object — there is no discrete
  "create tenant" step to run, because a prefix isn't a resource that has
  to exist before something is written under it.

This is the direct consequence of choosing prefix- over bucket-per-tenant,
and it is the reason §1's "reuse `roles.py`, resolve `tenant_id`
server-side" requirement is load-bearing rather than a nice-to-have: with
no per-tenant credential to lean on, the *only* thing standing between one
tenant's objects and another's is application-layer key construction plus
the standard role/session auth already gating the request. A leaked
app-level Tigris credential, or a bug that lets a caller influence
`tenant_id`/`project_id` going into `build_object_key`, exposes **every**
tenant's objects in one shot — a materially different blast radius than a
single leaked Neon connection string today. This asymmetry is exactly why
investigation `549e66c6` left bucket-vs-prefix an explicit human
`[OPEN]` decision rather than defaulting it, and why activation-gate step 1
in `docs/object-storage-backend.md` (human sign-off) has to happen before
step 5 (a real `TigrisObjectStoreBackend` implementation) — not after.

**If/when this activates**, provisioning is folded into the existing
10-step activation gate in `docs/object-storage-backend.md` (§"Activation
gate") unchanged; this document adds exactly one concrete requirement to
step 9 ("sync worker + MCP tool"): the sync worker's entry point (and any
MCP tool that triggers it) must resolve `tenant_id` via
`get_tenant_id_for_project` inside the trusted request/session context,
never accept it as a caller-supplied field — see §1.

## 3. Quotas

**No byte-based storage quota exists anywhere in this codebase today** —
neither for local `artifact_store.py` content nor (obviously) for a remote
backend that doesn't exist yet. `meridian/limits.py` — the only quota-shaped
module in the repo — is exclusively **count**-based safety guardrails
(projects/sprint-items/notes/decisions/sessions/tasks/open-HITL *per
tenant or per project*, plus a request-body byte cap), explicitly
documented as "guard-rails, not quotas" against runaway loops, not a
billing mechanism.

**A real precedent for a byte-based, plan-tiered, billed quota already
exists — for Neon, not object storage** (`meridian/hosted.py`):

- `PLAN_LIMITS: dict[str, dict[str, float]]` carries a `storage_gb` figure
  per plan (`free: 0.1`, `standard: 1.0`, `pro: 10.0`, `admin: inf`).
- `run_storage_overage_check` polls each tenant's *real* Neon usage via
  `get_neon_storage_gb`, and `report_storage_overage_to_stripe` bills
  metered overage (`storage_overage_gb`) through Stripe when a tenant
  exceeds its plan's figure.

**Recommendation for Tigris (design only — no code in this item):**

1. Mirror `PLAN_LIMITS` with a new `object_storage_gb` key per tier.
   **Values are a business/pricing decision, not something this
   investigation sets** — see open item 4 in §6.
2. A byte counter needs a real source of truth, and **the closest existing
   table doesn't carry one today**: `object_sync_state`'s columns (`id`,
   `project_id`, `content_hash`, `backend`, `artifact_class`, `state`,
   `remote_key`, `remote_etag`, `queued_at`, `synced_at`, `last_error`,
   `retry_count`, `updated_at` — see `meridian/db/object_sync_state.py`) have
   **no `size` column**. Two options, neither implemented here: (a) add a
   `size` column to `object_sync_state` at sync-enqueue time (the size is
   already known locally via `artifact_store.get_artifact_metadata`'s
   `size` field, so this is a cheap join, not a new I/O path), or (b) poll
   the bucket's own usage API the way `get_neon_storage_gb` polls Neon's —
   unconfirmed whether Tigris exposes one (investigation `549e66c6`'s
   doc-site fetches for Tigris-specific behavior 404'd; see
   `docs/object-storage-backend.md`).
3. Enforcement should reuse `limits.py`'s existing shape — a
   `check_object_storage_bytes_per_tenant`-style guard raising the same
   `LimitExceeded` → 429 contract every other guardrail in this codebase
   already uses — rather than inventing a parallel quota mechanism.
4. **Not urgent for correctness today.** Per the local-first contract in
   `docs/object-storage-backend.md`, local disk is always authoritative and
   a remote tier is purely additive — nothing can overflow past what local
   disk already holds. A byte quota only becomes load-bearing once a real
   remote backend starts actually storing (and billing for) bytes, i.e.
   after activation-gate step 6 in that document.

## 4. Lifecycle — deletion & export boundaries

**Export — a usable building block already ships.**
`artifact_store.export_artifacts` (project-scoped, read-only, receipted:
`{project_id, exported_at, artifact_count, total_size, artifacts,
export_hash}`, base64 content, never deletes) is exactly the shape a
Tigris-aware export needs to compose with. `routes/export.py::export_my_data`
→ `db.export_tenant_data` already iterates every project row from the
tenant's project DB (`pdb`) to build a GDPR export; a future extension that
includes object-storage content for a tenant with real Tigris backing
should call an equivalent per-project export over the same project list —
no new enumeration primitive is needed, only wiring `export_artifacts`'s
existing output shape into that same tenant-level loop.

**Deletion — two existing paths, and BOTH have a pre-existing local-artifact
gap that Tigris activation would make materially worse.**

1. **Account deletion** (`routes/export.py::delete_account`, gated by
   `PERM_DELETE_TENANT` via `_require_workspace_perm` — owner-only, admins
   explicitly excluded by `ROLE_PERMS`):
   - `db.delete_tenant_records` deletes only shared **control-plane** rows
     (`user_sessions`, `api_tokens`, `workspace_members`, `tenants`).
   - `hosted._drop_tenant_neon_database` is a **separate**, fire-and-forget
     background task that drops the tenant's actual Neon project database.
   - **Neither path touches local `artifact_store.py` content** for that
     tenant's projects. This is a **pre-existing gap, independent of
     Tigris** — self-hosted local artifacts already outlive account/tenant
     deletion today. It becomes a real-money problem once a remote backend
     stores billed bytes rather than merely leftover local disk: a
     tenant-deletion flow that doesn't clean up its Tigris prefix leaves
     permanently-billed orphaned bytes in a shared bucket forever.
   - **Requirement for real activation** (not built here): an explicit
     "delete every object under `tenants/{tenant_id}/`" step, run at the
     same point `_drop_tenant_neon_database` fires, using
     `ObjectStoreBackend.list()`'s existing cursor-paginated `ListPage` +
     `delete()` in a bounded sweep loop — the Protocol already supports
     this without any new interface.
2. **Project deletion** (`DELETE /projects/{id}`, `PERM_SETTINGS`-gated per
   `_required_perm_for_request`, with the explicit `3f4ba195`
   authorization-bypass fix already covering the batch-delete route too):
   - `db.delete_project` removes DB rows only. Same gap at the project
     scope: nothing deletes that project's `artifact_store.py` files or
     (later) its Tigris objects today.
   - **Requirement for real activation:** an equivalent prefix-delete keyed
     on `{tenant_id}/{project_id}/` (or bare `{project_id}/` self-hosted),
     at the same call site as the DB-row delete. `purge_artifacts_before`
     already establishes the "bulk, project-scoped, cutoff-based delete"
     pattern locally; a "delete everything for this project" variant (or a
     `cutoff_iso` one second in the future, which already works today with
     zero new code for the *local* side only) is the natural local-side
     analogue — the remote side needs the real backend to exist first.
3. **Retention/GC parity.** `purge_artifacts_before` has no remote
   equivalent yet. Whoever implements the real backend needs a matching
   remote purge, and every `object_sync_state` row for a purged
   `content_hash` needs to be deleted in lockstep — a dangling
   `object_sync_state` row pointing at bytes that no longer exist anywhere
   is a metadata leak (harmless on its own, since the table holds no
   content, but confusing for any future audit/reconciliation tooling).

## 5. Summary table — Postgres baseline vs. this Tigris design

| Property | Neon Postgres (today) | Tigris, prefix-per-tenant (this design) |
|---|---|---|
| Cross-tenant isolation mechanism | Physical (separate DB per tenant) | Logical (shared bucket, key-prefix discipline) |
| Cross-project isolation within a tenant | Row-level (`project_id` columns); listing-only member scoping, `b11c7cf6` open | Same: key-prefix discipline only, `b11c7cf6` gap inherited unchanged |
| Blast radius of one leaked credential | One tenant's DB | **Every tenant's objects** (shared bucket, shared credential) |
| Provisioning per tenant | Real: new Neon project + DB + role | None: a prefix comes into existence on first write |
| Deletion of tenant's data | `_drop_tenant_neon_database` (exists) | **Not implemented** — no filed sprint item as of this investigation |
| Byte quota tied to billing | Yes (`PLAN_LIMITS.storage_gb` + Stripe overage) | **Not implemented** — no `size` column to key it on yet |

## 6. What is NOT yet resolved — needs a human decision before activation

1. **Bucket-per-tenant vs. prefix-per-tenant is still explicitly `[OPEN]`**
   in `docs/object-storage-backend.md` §"Bucket-per-tenant vs.
   prefix-per-tenant" — this document assumes prefix-per-tenant throughout
   (matching the recommendation on file) but that recommendation still
   needs the human sign-off that activation-gate step 1 requires. If a
   human instead chooses bucket-per-tenant, §1's "blast radius" analysis in
   §5 changes materially (a leaked per-tenant credential would then be
   scoped to one tenant, much closer to the Neon baseline) — but Tigris's
   own credential-scoping granularity (bucket-level vs. prefix-level) was
   never confirmed in the investigation's doc fetches, so bucket-per-tenant
   isn't guaranteed to buy that isolation even if chosen.
2. **`workspace_proposals.project_id` is not a hard, fully-migrated
   scoping boundary yet.** Traced to `meridian/db/migrations.py`'s
   `_migrate_proposal_project_scope` (`a8afd8f9`): `project_id` is a
   nullable column with **no DB-level FK**, added via `ALTER TABLE`, with
   **no automatic backfill/reclassification of pre-existing rows**
   (explicitly deferred to a separate, still-pending item `4eedeef8`).
   `NULL` means "workspace-wide" by convention, not "unscoped/broken," but
   there is no way today to distinguish "deliberately workspace-wide" from
   "created before this column existed and never classified." This is not
   directly load-bearing for object-storage access itself (proposals don't
   reference object keys), but it is the concrete example the sprint item
   flagged as "mid-migration" — anything built later that surfaces
   proposals alongside object-storage content (e.g. "attach an exported
   bundle to a proposal") must not assume every proposal's project scope is
   resolved.
3. **Direct-by-ID cross-project access for project-scoped invited members
   is unenforced today, and a Tigris route would inherit this unchanged**
   (pin `b11c7cf6`, `db/workspace.py::get_scoped_project_ids_for_member`
   and `_deps.py::_scoped_project_ids_for_request`, both explicit about the
   gap in their own docstrings). Do not represent a future object-storage
   route as more tightly scoped than the rest of the API unless someone
   deliberately builds a stricter, object-storage-specific check — nothing
   proposed here does that.
4. **No byte quota mechanism exists, and quota *values* are a pricing
   decision this investigation does not make.** See §3. Needs: (a) a
   decision on per-tier GB figures, (b) a `size` column or equivalent
   source of truth added to `object_sync_state` (or a bucket-usage-API
   poll, if Tigris exposes one — unconfirmed).
5. **Tigris's conditional-write (412/409) and presigned-URL behavior is
   unconfirmed against a real bucket** (`docs/object-storage-backend.md`,
   inherited from investigation `549e66c6` §2 — the AWS S3 facts used as a
   working assumption were not independently verified for Tigris
   specifically, because the relevant doc-site pages 404'd). Every
   assumption in this document about atomic, race-free writes for
   tenant-prefix isolation inherits that same unconfirmed status.
6. **Nothing structurally prevents a future caller from passing an
   arbitrary `tenant_id` into `build_object_key`.** The parameter is
   validated for character-safety only (`_safe_component`), not ownership.
   §1/§2 state the requirement ("resolve `tenant_id` server-side via
   `get_tenant_id_for_project`, never accept it from a caller") as a rule
   for whoever writes the first real caller — nothing in `object_store.py`
   enforces it today, and nothing needs to, since no caller exists yet.
7. **No product decision on whether object GET/list routes need a role
   check at all**, versus following the `routes/files.py` precedent
   (project-existence check only, no role gate on GET). The two existing
   precedents in this codebase disagree with each other; this document
   does not pick one.
8. **Account/project deletion has no object-storage cleanup step, and no
   sprint item for it was found in this investigation.** See §4. This
   needs to be filed and built before real activation, or deleted
   tenants/projects will leave permanently-billed orphaned bytes in a
   shared bucket.
9. **Documentation gap unrelated to this item's own findings:**
   `docs/infra-storage-cost-resilience-runbook.md`, named in this sprint
   item's `touches_resources`, does not exist in this repository as of this
   investigation — only `docs/backup-runbook.md` and
   `docs/meridian-local-resilience.md` are present. This may be a stale
   resource reference, or a runbook that still needs to be written
   alongside real activation; this document does not create it, and no
   conclusions here depend on its contents.

## Non-goals of this document (restated)

- No Tigris activation code. `TigrisObjectStoreBackend` remains the
  `NotImplementedError` stub it is today.
- No credentials touched, no bucket created, no `.env`/`meridian.toml`
  edits.
- No production data written, read, or migrated.
- Does not resolve any of the nine open items in §6 — it names them so a
  human can decide, per `AGENTS.md`'s capability-manifest contract: "If no
  approved fallback exists for a `required` capability, that is a signal to
  stop and request human input, not to invent one." The same discipline
  applies here at the design level.
