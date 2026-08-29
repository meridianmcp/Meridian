# Project Family / Template Revisions — Design (Sprint Item 5060eea1)

**Status:** Design only. Nothing in this document is implemented. No migration,
route, handler, or DB function exists yet for any concept named here. This
doc, plus the additive Pydantic data contracts in `meridian/models.py` and the
design-validation tests in
`tests/test_project_family_template_design_5060eea1.py`, are the entire
deliverable for this sprint item.

**Parent item:** ddcf6984 ("project-family-templates"). **This item (5060eea1)**
scopes down to one slice of that parent: immutable template revision and
child snapshot semantics — stable revision IDs, content hashing, effective
configuration resolution, diffing, compatibility/versioning, supersession,
rollback, and conflict handling.

**Grounding:** written against dev tip `8bdd6c18` in worktree
`.claude/worktrees/a0c162f5`. A prior read-only investigation confirmed there
is nothing to reconcile against: no code, tests, docs, or branches anywhere in
this repo currently reference `ProjectFamily`, `ProjectTemplate`,
`TemplateRevision`, `ChildSnapshot`, `TemplateOverride`, or
`template_revision`/`child_snapshot` in this sense. This design starts on a
clean slate and deliberately follows two existing house patterns rather than
inventing new ones:

- **`meridian/db/profile_layers.py`** (PROFILE-1/2 contract) for revision
  numbering, content-hash canonicalization, `reset_fields`-style retraction,
  and effective-configuration resolution across layers.
- **`meridian/db/board_snapshot.py`**'s `board_snapshot_revisions` idiom for
  the append-only audit ledger (one row per revision *actually written*;
  idempotent no-op resaves never grow the ledger).

---

## Concepts and vocabulary

| Term | Meaning |
|---|---|
| **Project template** (`ProjectTemplate`) | A named, long-lived lineage of configuration. Has an identity that never changes and a mutable "latest revision" pointer. |
| **Template revision** (`TemplateRevisionSnapshot`) | One **immutable** snapshot of a template's full configuration payload, numbered and content-hashed. Revisions are never edited or deleted; a template evolves by appending new revisions. |
| **Child project** | Any `Project` (see `meridian/models.py::Project`) that has adopted a template revision. A child is a completely ordinary project row — this design adds no new column to `projects`. |
| **Child override** (`ChildTemplateOverride`) | A child-local layer of field values that shadow the adopted template revision's payload for that one child, plus an explicit `reset_fields` retraction list. Mirrors `profile_layers`'s per-scope override layer exactly. |
| **Child snapshot** (`ChildTemplateSnapshot`) | The durable, per-child record of *which* revision a child has adopted, when, and the content hash of the effective configuration that was in force at adoption time. This is the audit/rollback anchor — see "Rollback" below. |
| **Project family** (`ProjectFamilyView`) | Not a new stored entity. A family is simply "one template plus every project that has ever adopted one of its revisions" — a read-time join, described fully under "Composition with the legacy `parent_project_id` mechanism" below. |
| **Effective configuration** | The fully-resolved configuration for one child: template revision payload with the child's override layered on top, per the resolution algorithm in section (c). |

---

## (a) Stable revision ID scheme

Two identifiers exist per revision, matching the distinction `profile_layers`
already draws between a row's opaque primary key and its human-meaningful
revision number:

- **`id`** — an opaque `uuid4` string (`meridian.db._new_id()`), the row's
  literal primary key. Never displayed as "the" identity of a revision;
  exists only because every table in this codebase has one.
- **`revision_id`** — the **stable, addressable identity** referenced
  everywhere else in this design (adopt requests, rollback targets, diffs,
  audit logs). Format:

  ```
  {template_id}:r{revision_number}
  ```

  e.g. `3fa1...c9:r1`, `3fa1...c9:r2`, `3fa1...c9:r7`. `revision_number` is a
  **monotonically increasing integer scoped to one `template_id`**, starting
  at 1, assigned exactly once, and never reused — the same counter discipline
  `profile_layers.revision` already uses per `(scope_type, scope_id)`.

  **Why a composite string rather than a bare uuid:** a bare opaque id would
  be just as unique, but `revision_id` needs to be *stable and
  human-legible* across the surfaces that reference it — a `/goal` block, a
  dashboard URL, a diff report, a HITL prompt ("child X is 3 revisions
  behind template Y, currently pinned to `{template_id}:r4`, latest is
  `{template_id}:r7`"). The composite form makes "how far behind" visually
  obvious without a lookup, and makes a copy-pasted id trivially traceable
  back to its template even out of context.

  **Immutability guarantee:** once minted, a `revision_id` is permanently
  bound to one exact `(fields, schema_version, content_hash)` tuple. No
  operation in this design — including template rollback (section g) —
  ever rewrites an existing revision's payload or reassigns its
  `revision_id`. "Rolling back a template" always **mints a new
  `revision_id`** whose payload happens to match an old one; it never
  resurrects or mutates the old `revision_id` in place.

A template's own `id` is a plain `uuid4`, matching every other top-level
entity (`Project.id`, `Session.id`, etc.).

---

## (b) Content hash / canonical serialization

Reuses `meridian.db.profile_layers._content_hash` verbatim — not a new
algorithm:

```python
canonical = json.dumps(
    {"fields": fields, "reset_fields": sorted(reset_fields)},
    sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str,
)
content_hash = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

Applied at two points, with two different input shapes:

1. **Per template revision:** `reset_fields` is always `[]` for a template
   revision (a template has nothing to "reset" — it *is* the base layer), so
   in practice this reduces to a canonical hash of `{"fields": fields,
   "reset_fields": []}`. Kept in the same two-key shape as the override hash
   below (rather than hashing `fields` alone) so both hash families are
   produced by one shared helper with no branching.
2. **Per child override:** `fields` is the override's declared field values
   and `reset_fields` is its retraction list — identical shape to a
   `profile_layers` row.

A third, derived hash — **`effective_content_hash`** — covers the *merged
result* (template payload + override applied, per section c), computed the
same way over the merged `fields` dict with `reset_fields: []` (resets have
already been applied by the time a dict reaches this hash; nothing is left
to record at this layer). This is the value stored on `ChildTemplateSnapshot`
at adopt time — see section (g), Rollback.

**Determinism properties this buys, matching `capability_manifest.manifest_hash`
and `profile_layers._content_hash`'s existing guarantees:**

- Identical field sets hash identically regardless of caller's dict key
  order (`sort_keys=True`).
- `ensure_ascii=True` + `default=str` means the hash never raises on
  non-ASCII text or non-JSON-native values (dates, etc. get stringified
  deterministically) and never varies by platform locale.
- A no-op resave (new payload hashes identically to the current one) is
  detected by simple string comparison before any revision-number bump or
  ledger row — see `set_profile_layer`'s existing idempotent-no-op path,
  which this design's `create_template_revision` (future work, section j)
  would replicate exactly.

See section (i) for the large/structured-payload and no-secrets angle on
this same hash.

---

## (c) Effective configuration resolution algorithm

Two layers only, precedence low → high:

```
effective_fields = deepcopy(template_revision.fields)
for key in child_override.reset_fields:
    effective_fields.pop(key, None)          # explicit retraction, applied first
effective_fields.update(child_override.fields)  # override always wins for a declared key
```

Rules, stated explicitly because they are easy to get backwards:

1. **Any key the override declares wins**, full stop, regardless of what the
   template revision says about that key (including if the template does not
   have that key at all — an override may introduce a child-only field the
   template never mentioned).
2. **`reset_fields` only matters for keys the override does *not* also
   declare.** If a key appears in both `child_override.reset_fields` and
   `child_override.fields`, the declared value in `fields` wins (`fields`
   is applied *after* resets in the algorithm above) — this mirrors
   `profile_contract`'s existing "declaring a field always beats resetting
   it" precedence for the identical ambiguity in the PROFILE-1 layer stack,
   so a caller migrating between the two concepts sees the same rule.
3. **Resolution is always exactly two layers** — template base, child
   override — never more. This is a deliberate simplification relative to
   `profile_layers`'s 5-layer hosted_default→workspace→user→project→session
   chain: a template/child relationship is a *provisioning* relationship,
   not a scope-inheritance chain, so there is no "workspace template" or
   "user template" layer to thread through here. If a future item wants
   template inheritance (a template itself based on another template), that
   is new scope for a later item, not implied by this one.
4. **A child with no override row at all** (never called `override`) has
   `effective_fields == template_revision.fields` exactly — the empty
   override is the identity element, matching `_empty_layer_dict`'s "a
   scope with no persisted row gets an empty profile back, never an error"
   contract.
5. **A child with no adopted revision at all** (never called `adopt`) has no
   effective configuration to resolve — this is a precondition failure for
   any resolution/preview call, not a "resolve against nothing" case; see
   `ChildTemplateSnapshot.adopted_revision_id: str | None` in section (k).

This composes with, and never touches, the existing 5-layer
`profile_layers`/`ProjectSettings` resolution in `get_effective_profile` —
see "Composition with the legacy mechanism" below for why these are
deliberately kept as two independent resolvers rather than merged into one
wider chain.

---

## (d) Diff format

One shared diff shape (`ConfigDiffEntry`, see section k) serves both
documented use cases:

- **Revision-to-revision:** `diff(template_revision_A.fields,
  template_revision_B.fields)` — "what changed in the template between two
  revisions."
- **Child-effective-vs-template-base:** `diff(template_revision.fields,
  child_effective_fields)` — "how does this child's actual effective
  configuration differ from the template it's nominally based on." This is
  the shape `preview` (section k) returns.

Both cases are the same structural operation: a flat diff over two
already-merged `dict[str, Any]` field sets, one entry per top-level key that
differs (nested dicts are compared as whole values, not recursively
diffed — see "Non-goals" below for why).

Each `ConfigDiffEntry` carries:

```
path: str            # top-level field name, e.g. "build.timeout_seconds"
op: "added" | "removed" | "changed"
base_value: Any | None    # value on the "from" side (None + op=="added" => key didn't exist)
new_value: Any | None     # value on the "to" side   (None + op=="removed" => key no longer exists)
source: "template" | "override"
```

`source` is what makes this diff format useful for the child-vs-template
case specifically (it is meaningless/always `"template"` for the
revision-to-revision case, and implementations should simply omit it or
leave every entry `"template"` there): for a child's effective-vs-base diff,
each differing key is attributed to *why* it differs — because the child's
override layer declares it (`"override"`), or because the diff is being run
against a stale base and the template revision itself changed under the
child's feet (`"template"`, only possible when comparing the child's
currently-*adopted* revision against a newer candidate — see `preview` in
section k, which surfaces exactly this partition so a human can tell "this
delta is my own override" apart from "this delta is new template behavior I
haven't reviewed yet").

**Non-goals (explicitly out of scope for this item):** a recursive/nested
JSON-patch-style diff (RFC 6902) for deeply nested structures; a
line-oriented text diff for string-valued fields; diff *rendering* (HTML/CLI
formatting) — the `ConfigDiffEntry` list is the wire contract, presentation
is a separate later concern once this ships for real.

---

## (e) Compatibility / versioning rules

Two independent counters, deliberately not conflated:

- **`revision_number`** (section a) — bumped on **every** content change to a
  template's payload, compatible or not. This is "how many times has this
  template been edited," full stop.
- **`schema_version`** — bumped **only** when a revision's payload changes
  in a way that is not safely mergeable against an *arbitrary* existing
  child override — i.e., a structural/contract change to the payload shape
  itself (a field is renamed, a field's expected type changes, a field is
  removed that overrides may depend on). An ordinary content update (new
  default value, new *additional* optional field) does **not** bump
  `schema_version`.

  This mirrors `profile_contract.SCHEMA_VERSION`'s role exactly: revisions
  within the same `schema_version` are always safe to preview/adopt without
  extra scrutiny; a `schema_version` bump is the signal that gates the
  conflict-detection path in section (h) — `preview` (section k) sets
  `schema_version_change: true` whenever the candidate revision's
  `schema_version` differs from the revision the child currently has
  adopted, independent of whether any actual field-level conflict is
  detected.

- **Template-level `schema_version`** (on `ProjectTemplate`) tracks the
  *current* value; each `TemplateRevisionSnapshot.schema_version` is frozen
  at whatever the template's schema_version was when that revision was
  created — so a revision's own `schema_version` never changes after the
  fact, same immutability guarantee as its `content_hash`.
- **`changelog`** (free-text, optional, on `TemplateRevisionCreate` /
  `TemplateRevisionSnapshot`) is the human-authored "what changed and why"
  note attached to a revision — not machine-parsed, purely for the audit
  trail and for surfacing in a `preview` response alongside the structural
  diff.

No semver-style `MAJOR.MINOR.PATCH` string is introduced — `revision_number`
already gives total order, and `schema_version` already gives the
compatibility-gate signal; a third versioning scheme layered on top would be
redundant surface area.

---

## (f) Supersession semantics

- `ProjectTemplate.latest_revision_id` is the only mutable pointer in this
  entire design — it advances to point at the newest revision every time
  `TemplateRevisionCreate` succeeds.
- Every `TemplateRevisionSnapshot` carries `superseded_by_revision_id: str |
  None`. Creating revision N+1 sets revision N's `superseded_by_revision_id
  = N+1`'s `revision_id`; revision N's own `fields`/`content_hash`/
  everything else is untouched. `superseded_by_revision_id is None` is the
  definition of "this is currently the latest revision."
- **A template update never silently rewrites, re-resolves, or reassigns any
  existing child.** `ChildTemplateSnapshot.adopted_revision_id` is set only
  by an explicit `adopt` call (section k) and stays exactly what it was
  through any number of subsequent `TemplateRevisionCreate` calls on that
  template. A child that adopted `{template}:r3` five revisions ago is still
  resolved against `{template}:r3`'s frozen payload today, forever, until
  something calls `adopt` again on that same `child_project_id`.
- "Is this child behind?" is a **derived, read-time** fact, never stored:
  `child_snapshot.adopted_revision_id != template.latest_revision_id`. No
  background job, trigger, or push notification is implied by this design —
  a caller (dashboard, executor session start, `preview`) checks this
  lazily, on demand.

---

## (g) Rollback

Two distinct operations, kept as two distinct request shapes (section k)
because they target different resources and have different immutability
consequences — collapsing them into one polymorphic "rollback" body would
hide that difference behind a discriminator field, which this codebase's
existing convention avoids (compare `ClaimTaskRequest`/`ClaimTaskResponse`
as separate typed shapes rather than one overloaded body).

### Child rollback (`ChildTemplateRollbackRequest`)

"Point this child back at a revision it previously adopted." This is a
**pure re-pointing** of `ChildTemplateSnapshot.adopted_revision_id` — no
template data is touched, no new `TemplateRevisionSnapshot` is created.
Constraints:

- `target_revision_id` must belong to the same `template_id` the child is
  already associated with (a child cannot "roll back" onto an unrelated
  template's revision — that is a `reject`-then-`adopt`-elsewhere operation
  on a different template, not a rollback).
- `target_revision_id` is not required to be the *immediately preceding*
  revision — a child may roll back arbitrarily far, to any revision number
  less than its current one, or even forward again to a revision it had
  previously rolled back away from (rollback is a re-pointing, not a
  destructive truncation of history).
- The child's override layer (`ChildTemplateOverride`) is **never** touched
  by a child rollback — overrides are an independent layer (section c);
  rolling back the template-base half of the effective configuration does
  not implicitly discard child-local customizations layered on top of it.
  (Whether the override still makes semantic sense against the older base is
  exactly the conflict question in section h — a rollback that reintroduces
  a conflict is surfaced the same way an adopt would be, not silently
  allowed.)
- Bumps `ChildTemplateSnapshot.snapshot_revision` (its own optimistic-
  concurrency counter — see `expected_snapshot_revision` in section k) and
  recomputes `effective_content_hash` against the (older) target revision's
  payload plus the still-current override.

### Template rollback (`TemplateRevisionRollbackRequest`)

"Make the template's *latest* revision have the same payload as some older
revision again." Given the hard immutability guarantee in section (a),
this **cannot** mean resurrecting or mutating the old `revision_id` — that
would retroactively change what `{template_id}:r{N}` has always meant, which
this design forbids outright. Instead:

- A template rollback **creates a brand-new revision** (next
  `revision_number`, brand-new `revision_id`) whose `fields` are a byte-for-
  byte copy of `target_revision_id`'s `fields`.
  `TemplateRevisionSnapshot.rollback_of_revision_id` is set to
  `target_revision_id` so the ledger records *why* this revision's content
  happens to match an older one, distinguishing "coincidentally identical
  edit" from "intentional revert" in the audit trail.
  `content_hash` will therefore equal the target revision's `content_hash`
  exactly (same canonical `fields`) even though `revision_id` and
  `revision_number` are new — this is expected and is how a caller can
  detect "this looks like a revert" purely from hash equality even without
  reading `rollback_of_revision_id`.
- `schema_version` for the new revision defaults to the **target**
  revision's `schema_version` (reverting content plausibly also means
  reverting away from whatever schema change happened since), but this is a
  default, not forced — an explicit `schema_version` may still be passed if
  the caller knows better.
- Every currently-adopted child is completely unaffected by a template
  rollback, per the supersession guarantee in section (f) — a template
  rollback is just "create a new revision," and creating a revision never
  touches any child.

This "revert-by-appending" pattern is the same one git and this codebase's
own `profile_layers` module both use for exactly this reason: an
append-only ledger that can be reasoned about by replaying it in order, with
no operation that requires trusting that history was never quietly edited.

---

## (h) Conflict handling

"Conflict" in this design means specifically: **a child's local override
declares a field that a newly-available template revision has changed in a
way that crosses a `schema_version` boundary for that field's shape.**
Ordinary content changes within the same `schema_version` are never
conflicts — the override simply continues to shadow the template's value
for that key, exactly per the resolution algorithm in section (c); this is
normal, expected, silent, and requires no acknowledgment.

A conflict is only *computed*, never silently resolved:

1. `preview` (section k) is the one place conflicts surface. Given a
   child's current override and a `candidate_revision_id`:
   - Diff the child's **currently-adopted** revision's `fields` against the
     **candidate** revision's `fields` (section d's revision-to-revision
     diff).
   - For every top-level key that both (i) appears in that diff, and (ii)
     is also a key the child's override declares in `fields` — that key
     is a **candidate conflict**.
   - A candidate conflict is only escalated to `conflicts: list[str]`
     (the field paths) in the `TemplateOverridePreview` response when the
     candidate revision's `schema_version` differs from the currently-
     adopted revision's `schema_version`. A same-`schema_version` content
     change to an overridden key is reported in the plain `diff` list
     (with `source: "template"`, informational) but is **not** added to
     `conflicts` — same-schema_version changes are, by the definition in
     section (e), always safe to layer under an existing override.
2. `adopt` (section k) **refuses** (returns/raises rather than silently
   proceeding) when `preview`-equivalent logic finds a non-empty
   `conflicts` list for the requested `revision_id`, **unless** the caller
   passes `force_accept_conflicts=True` **and** a non-empty
   `override_reason` — the same acknowledged-override pattern this codebase
   already uses for `override_merge_approval`, `override_code_intel_receipt`,
   and `override_strict_evidence`. The reason is persisted (future work,
   section j: an `adopt_conflicts_acknowledged` column or an
   `action_audit_log` entry) so an audit trail exists for every forced
   adoption over a live conflict.
3. There is no automatic "merge" or "resolve" step — a conflict is a human
   decision point. The two ways out are: (a) force-adopt with an
   acknowledged reason (above), or (b) update the override first (drop or
   change the conflicting key via `override`, section k) so the next
   `preview` comes back clean, then adopt normally.
4. **Concurrent-write conflicts** (two callers racing to `adopt`/`reject`/
   `rollback`/`override` the same child, or two callers racing to
   `create`/`fork` the same template) are a *different* kind of conflict,
   handled the ordinary optimistic-concurrency way already established by
   `profile_layers.ProfileStaleRevisionError`: every mutating request in
   section (k) accepts an `expected_*_revision` field, and a stale value
   raises rather than silently overwriting a concurrent write. This is
   orthogonal to the schema-level conflict detection in points 1–3 above.

---

## (i) Determinism and auditability for large/structured payloads, without secrets or machine-local paths

Two separate concerns, both already solved elsewhere in this codebase and
reused verbatim rather than reinvented:

**Determinism regardless of payload size or nesting.** The canonical-JSON
content hash in section (b) (`sort_keys=True, separators=(",", ":"),
ensure_ascii=True, default=str`) is insensitive to payload size — a template
`fields` dict with 3 keys or 300 hashes exactly as deterministically,
because the guarantee comes from canonical *serialization*, not from any
size-dependent property. The one practical implication for large payloads
that this design flags as a genuine open question for the real
implementation (section j), rather than silently ignoring: this design does
not itself impose a payload size cap. A future implementation should decide
one (mirroring whatever cap, if any, `goal_states.content` or `notes` bodies
already use) rather than accepting unbounded `fields` dicts into shared,
multi-project state.

**No secrets or machine-local paths in shared state.** `template_revision`
and `child_override` payloads are exactly the kind of project-shared,
multi-machine state that `meridian/capability_manifest.py`'s existing
provenance validation was written to protect
(`_check_no_secrets_or_local_paths`, `_ABSOLUTE_PATH_RE`,
`_SECRET_LIKE_RE`): a value that's fine in a developer's local `.env` is not
fine once it is written into a template that every project adopting it will
receive, or into a child override that syncs across every machine that
project's sessions run on. The real implementation (section j) should call
this exact validator — recursively, over both `TemplateRevisionCreate.fields`
and `TemplateOverrideSet.fields`, at write time, the same way
`capability_manifest.normalize_capability` already does — rather than
re-implementing path/secret detection a third time. This is a reuse
decision, not a new rule: this design adds no new regex or detection logic
of its own.

`provenance` fields on `TemplateRevisionCreate`/`ProjectTemplateCreate`
follow the identical typing convention as
`capability_manifest`'s `provenance` (`str | dict | None`, validated by the
same helper) for the same reason: provenance is metadata *about* where a
template or revision came from (a URL, a doc section, an admin's note) and
must never itself become a vector for smuggling a secret or local path into
shared state.

---

## (j) Schema sketch — future work, NOT implemented

Nothing below exists. No migration has been written in this worktree for
any of it, and per this item's own scope, none should be until a follow-up
implementation item picks this up. Sketched here only so the eventual
migration has a concrete starting point that already accounts for the rules
above (immutability, append-only ledger, guarded index creation).

```sql
-- One row per template lineage. `latest_revision_id` is the ONLY mutable
-- pointer anywhere in this schema.
CREATE TABLE IF NOT EXISTS project_templates (
    id                       TEXT PRIMARY KEY,      -- uuid4
    name                     TEXT NOT NULL,
    description              TEXT,
    schema_version           INTEGER NOT NULL DEFAULT 1,
    latest_revision_id       TEXT,                  -- FK -> project_template_revisions.revision_id
    forked_from_template_id  TEXT,
    forked_from_revision_id  TEXT,
    created_by_human_id      TEXT,
    created_at               TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Append-only. A row here is NEVER UPDATEd except to set
-- superseded_by_revision_id when a newer revision is created — every other
-- column is written once, at INSERT, and never touched again.
CREATE TABLE IF NOT EXISTS project_template_revisions (
    id                        TEXT PRIMARY KEY,      -- uuid4, opaque
    revision_id               TEXT NOT NULL UNIQUE,  -- "{template_id}:r{revision_number}"
    template_id               TEXT NOT NULL,
    revision_number           INTEGER NOT NULL,
    schema_version            INTEGER NOT NULL,
    fields                    TEXT NOT NULL,          -- JSON
    content_hash              TEXT NOT NULL,
    changelog                 TEXT,
    provenance                TEXT,                   -- JSON, no secrets/paths (section i)
    superseded_by_revision_id TEXT,
    rollback_of_revision_id   TEXT,
    created_by_human_id       TEXT,
    created_at                TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (template_id, revision_number)
);
-- Guarded index, added INSIDE the migration function per this repo's
-- 2026-07-04-outage lesson — never inline in an unguarded CREATE_TABLES
-- literal:
-- CREATE INDEX IF NOT EXISTS idx_template_revisions_template
--     ON project_template_revisions (template_id, revision_number);

-- One row per (child_project_id, template_id) — a child's local override
-- layer. Mirrors profile_layers' shape exactly.
CREATE TABLE IF NOT EXISTS child_template_overrides (
    child_project_id TEXT NOT NULL,
    template_id       TEXT NOT NULL,
    fields            TEXT NOT NULL DEFAULT '{}',      -- JSON
    reset_fields      TEXT NOT NULL DEFAULT '[]',      -- JSON list
    override_revision INTEGER NOT NULL DEFAULT 0,
    content_hash      TEXT NOT NULL,
    updated_at        TEXT,
    PRIMARY KEY (child_project_id, template_id)
);

-- One row per (child_project_id, template_id) — the durable "what did this
-- child adopt" record. This is the rollback/audit anchor (section g).
CREATE TABLE IF NOT EXISTS child_template_snapshots (
    child_project_id       TEXT NOT NULL,
    template_id            TEXT NOT NULL,
    adopted_revision_id    TEXT,
    adopted_at             TEXT,
    snapshot_revision      INTEGER NOT NULL DEFAULT 0,
    effective_content_hash TEXT,
    declined_revision_ids  TEXT NOT NULL DEFAULT '[]', -- JSON list
    last_action            TEXT,                        -- adopted|rejected|rolled_back
    updated_at             TEXT,
    PRIMARY KEY (child_project_id, template_id)
);

-- Append-only audit ledger, mirroring board_snapshot_revisions /
-- profile_layer_revisions: one row per adopt/reject/rollback actually
-- performed (never for a no-op).
CREATE TABLE IF NOT EXISTS child_template_snapshot_events (
    id                TEXT PRIMARY KEY,   -- uuid4
    child_project_id  TEXT NOT NULL,
    template_id       TEXT NOT NULL,
    action            TEXT NOT NULL,      -- adopted|rejected|rolled_back
    revision_id       TEXT,
    conflicts         TEXT,               -- JSON list, only for a forced adopt
    override_reason   TEXT,
    actor             TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Integration points identified (also not touched, description only):

- `meridian/db/migrations.py` — a new `_migrate_project_templates` function,
  appended to the flat ordered `await _migrate_*(db)` sequence in `init_db`
  after `_migrate_profile_layers`, with any index creation **guarded**
  inside the function per this repo's own migration convention.
- `meridian/pg_adapter.py` — a mirrored `_migrate_pg_project_templates`
  registered in the parallel PG migration list, same convention as
  `_migrate_pg_profile_layers`.
- `meridian/handoff.py` — `generate_handoff`'s existing `emit_manifest`
  splice point (currently wired only for `mode="goal"`) is the natural place
  to surface "this project is a child of template X, currently at revision Y
  (latest is Z)" into a handoff's `<handoff_manifest>` block, analogous to
  how `build_effective_capability_contract` / `build_effective_profile_binding`
  already surface other "effective X for this project" state there.
- `tests/test_core.py` migration-count assertion — any real migration lands
  with the usual `test_core` count bump, per this repo's own convention
  (not touched by this design-only item).

---

## (k) API shapes

All of the following are pure Pydantic `BaseModel` data contracts, added to
`meridian/models.py` in this same change, wired to **nothing** — no route, no
MCP tool, no handler. They express the request/response shape each future
operation would use; the operations themselves are future work (section j).

| Operation | Request model | Response model |
|---|---|---|
| create (new template) | `ProjectTemplateCreate` | `ProjectTemplate` |
| create (new revision on an existing template) | `TemplateRevisionCreate` | `TemplateRevisionSnapshot` |
| fork (branch a revision into a new template lineage) | `TemplateForkRequest` | `ProjectTemplate` |
| override (a child's local override) | `TemplateOverrideSet` | `ChildTemplateOverride` |
| preview (dry-run effective-config diff before adopting) | `TemplateAdoptionPreviewRequest` | `TemplateOverridePreview` |
| adopt (a child adopts a specific revision) | `TemplateAdoptRequest` | `ChildTemplateSnapshot` |
| reject (a child declines a proposed revision) | `TemplateRejectRequest` | `ChildTemplateSnapshot` |
| rollback — child | `ChildTemplateRollbackRequest` | `ChildTemplateSnapshot` |
| rollback — template | `TemplateRevisionRollbackRequest` | `TemplateRevisionSnapshot` |
| (read) one config diff entry | — | `ConfigDiffEntry` |
| (read) family aggregate, see composition note below | — | `ProjectFamilyView` |

Field-level detail for each is in `meridian/models.py` itself (with
docstrings cross-referencing the relevant section letter above); the table
above is the index. Every request model that mutates shared per-child or
per-template state carries an optional `expected_*_revision` optimistic-
concurrency field and an optional `actor`, matching `profile_layers`'s
`set_profile_layer(expected_revision=..., actor=...)` convention.

---

## Composition with the legacy `parent_project_id` / north_star mechanism

This is the compatibility constraint called out explicitly in this item's
acceptance notes, so it is stated here in one place rather than scattered:

**The existing mechanism, unchanged by this design:**
`ProjectCreate.parent_project_id` / `set_parent_project` enforce a hard
one-level-deep subproject invariant (`meridian/db/__init__.py:create_project`
line ~1151, `set_parent_project` line ~1377): a project may become a
subproject only of a top-level project, and a project with children cannot
itself become a child. `get_goal` (line ~2623) falls back to a subproject's
parent's `goal_north_star` when the subproject has none of its own, stamping
`north_star_inherited: bool` / `north_star_source_project_id: str` on the
result (`GoalState` in `meridian/models.py`, fixed to actually surface over
HTTP in sibling item 106519eb). `set_goal` never writes an inherited value
back into a child's own row.

**Why this design does not touch, extend, or overload any of that:**

1. **Different relationship shape.** `parent_project_id` is a *hierarchy*
   relationship between two projects (one is literally the parent of the
   other, one level deep, mutually exclusive with having its own children).
   A template/child relationship is a *provisioning* relationship: a
   template is not a project at all, has no `parent_project_id`, is not
   subject to the one-level-deep constraint, and a single project could in
   principle be a child of a template while *also* being a
   `parent_project_id` subproject of some unrelated top-level project — the
   two relationships are orthogonal axes, not alternatives to each other.
2. **Different inheritance semantics.** `north_star_inherited` describes a
   *live, always-current* fallback (the child's `get_goal` re-reads the
   parent's `goal_north_star` fresh every call — there is no "adopt" step,
   no revision, no snapshot; if the parent's north_star changes, every
   child sees the new value on its very next `get_goal`). The
   template/child relationship in this design is the deliberate opposite: a
   child is pinned to whatever revision it explicitly adopted until it
   explicitly adopts again (section f) — the entire point of "immutable
   template revision and child snapshot semantics" per this item's own
   acceptance text is that a child's configuration must **not** silently
   track the template's latest state the way `north_star_inherited` tracks
   a parent's latest north_star.
3. **No field reuse.** `Project` gains zero new columns/fields in this
   design. `child_template_snapshots`/`child_template_overrides` (section j)
   are new, separate tables keyed by `child_project_id` — a foreign
   reference *to* a `Project.id`, never a mutation *of* the `Project` or
   `GoalState` rows/models themselves. `north_star_inherited` /
   `north_star_source_project_id` on `GoalState` are completely unaffected;
   nothing here changes what `get_goal`/`set_goal` compute or return.
4. **A project family is a strict superset concept, not a replacement.**
   `ProjectFamilyView` (section k) is read-only and additive: it answers "if
   I also happen to have `parent_project_id` subprojects, do those show up
   here?" — no. A `ProjectFamilyView` lists a template's children (by
   `child_template_snapshots` membership) exactly, with no reference to
   `parent_project_id` at all. A project can appear in a `ProjectFamilyView`
   without ever having a `parent_project_id`, and can have a
   `parent_project_id` without ever appearing in any `ProjectFamilyView`.
   The two groupings can coincide in practice (a team might provision a
   top-level project and its subprojects all from the same template) but
   nothing in this design assumes or requires that they do.

**Net statement for the acceptance criterion:** current one-level
`parent_project_id` projects remain fully legacy-compatible because this
design adds no new field to `Project`/`GoalState`, changes no existing
function's behavior, and defines the family/template relationship as an
orthogonal, opt-in join over new tables — a project that never calls
`create`/`fork`/`override`/`adopt` is completely unaffected and indeed
cannot even be observed to differ from a project in a codebase where this
design was never written.
