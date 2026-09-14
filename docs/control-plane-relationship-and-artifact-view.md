# Control Plane: Relationship & Artifact/Manifest Browser

Sprint item `07f753b2` (item_group `control-plane-ui`), implementing the
accepted CONTROL-PLANE-UI-PLAN — the design/data-contract deliverable filed
as workspace note `8e06a475`, attached to sprint item `ba61cc39`.

## What shipped

Three new, read-only HTTP routes (`meridian/routes/control_plane.py`),
registered in `meridian/server.py`, plus a thin dashboard integration
(`meridian/static/dashboard-control-plane.ts` + a new subsection of the
existing Settings → Workspace pane in `meridian/static/dashboard-settings.ts`):

| Route | Purpose |
|---|---|
| `GET /control-plane/relationships` | Workspace → project → one-level subproject, with an explicit `relationship_scope` label per row and an overall `scope` label (`workspace` vs `project`). |
| `GET /control-plane/proposals` | Paginated, redacted, read-only view of project/workspace proposals. |
| `GET /control-plane/artifacts` | Paginated, read-only view of run manifests, provenance status, and artifact-registry records from the `meridian-outputs` extension's local ledgers. Self-hosted only in this pass — see "Hosted mode" below. |

All three reuse existing data paths — `db.list_projects`, the existing
listing-only scope helper `_scoped_project_ids_for_request`, `db.get_workspace_proposals`,
and (lazily, optionally) the `meridian_outputs` extension's own
`run_manifest`/`annotate`/`artifact_registry` modules. No new ledger, no new
proposal store, no new mutation action was created — every existing
move/promote/delete action stays exactly where it already lives
(`POST /projects/{id}/parent`, `promote_proposal`, the existing delete
routes).

## Why hosted-mode artifact/manifest browsing is deferred

Design note `8e06a475` filed a SECURITY FINDING during the design pass: the
per-tenant WebSocket tunnel proxies (`meridian/routes/tunnel.py`) resolve
`tenant_id` purely from the URL path, with no check that the authenticated
caller actually belongs to that tenant. Since a hosted-mode artifact/manifest
browser would have to ride exactly that proxy to reach a user's local
`.meridian-outputs-cache/`, the note explicitly recommended **not** shipping
that combination until the proxy's tenant binding is fixed — routed as its
own, higher-priority security-review item, independent of this UI work.

This implementation honors that: `GET /control-plane/artifacts` returns a
clear, structured `{"available": false, "mode": "hosted", "reason": "..."}`
envelope in hosted mode — it never attempts to reach through the flagged
proxy path. Self-hosted mode needs no tunnel at all (the dashboard process
already runs on the same machine as the repo) and is fully functional today.

## Integration point: `meridian_outputs` is optional, and not project-linked

Two things were confirmed by direct inspection while building this (not
assumed):

1. **`meridian_outputs` is a genuinely separate, optionally-installed
   package.** `extensions/meridian-outputs/pyproject.toml` is its own
   project; it is *not* a dependency of the core `meridian` pixi
   environment. `import meridian_outputs` raises `ModuleNotFoundError` in a
   plain core checkout (confirmed by direct import in this environment).
   `GET /control-plane/artifacts` therefore imports it lazily, inside the
   request handler, and degrades to `{"available": false, "mode":
   "self_hosted", "reason": "...not installed..."}` rather than ever
   hard-failing the dashboard process over an optional extension.

2. **There is currently no stored link between a Meridian `project_id` and
   an `outputs_dir`/run/artifact.** The `meridian_outputs` ledgers
   (`run_manifest_ledger.json`, `provenance_ledger.json`, the artifact
   registry) are keyed by `run_id` / `path` / `artifact_id` — none carry a
   Meridian project id. `GET /control-plane/artifacts` therefore returns a
   workspace-wide list of records (each tagged with `record_kind`), not one
   attached to a specific project row in the relationship explorer. Sibling
   item `c4c74141` (a planned local-first temporary-script/artifact
   registry) had not landed as of this item's implementation pass (confirmed
   via `get_sprint_items` — still `pending`); this is the integration point
   a future pass should use to establish that link, rather than one this
   item invents or blocks on.

## Scope enforcement — what changed, and a bug this item's own prospecting caught

`GET /projects` already applies a listing-only scope filter
(`_scoped_project_ids_for_request`) for a project-scoped workspace member.
`GET /control-plane/relationships` reuses that exact same helper and the
exact same tenant-isolated `_db(request)` connection, so it shows precisely
what `/projects` would show the same caller — no new scoping policy was
invented for the (still explicitly open, per note `8e06a475`) product
question of whether that listing-only scope should behave differently here.

For **role gating**, the first implementation of `_require_read` resolved
the caller's role by checking `has_perm` against the caller's OWN tenant id
(via `_get_tenant_from_request` alone). Prospecting + a regression test
(`TestRoleMatrix::test_cross_workspace_viewer_role_is_resolved_against_the_TARGET_workspace`
in `tests/test_control_plane_ui.py`) caught that this is wrong: a caller
viewing another workspace via `X-Workspace-Tenant-Id` is still the *owner*
of their own personal workspace, so checking their role against their own
tenant id always passes — silently defeating cross-workspace scope
enforcement. The fix reuses `meridian/_deps.py::_enforcement_context` (the
same helper the existing generic write-role-enforcement middleware uses),
which correctly resolves the caller's role **in the workspace named by the
header**, matching exactly which physical/tenant database `_db(request)`
itself will open for the same request.

## A confirmed, pre-existing, out-of-scope finding

While writing `tests/test_control_plane_ui.py`'s cross-workspace tests, a
second, separate, **pre-existing** gap was confirmed by inspection (not
introduced by this item, and not attempted here): the `projects` table has
**no `tenant_id` column at all** (`db.create_project`'s `INSERT INTO
projects` statement carries no such column). Tenant isolation for `projects`
relies entirely on each tenant having a genuinely separate physical database
in production. Any tenant that falls back to sharing the auth database
(`app.state.db`) — e.g. the `admin`-plan / no-dedicated-Neon-URL fallback
this very test suite's own `_seed_tenant_session` helper relies on for
hermetic testing, mirroring `tests/test_cov_route_export.py` — would see
every such tenant's projects through `db.list_projects()`, since that
function has no per-tenant filter to apply. This affects the pre-existing
`GET /projects` identically; `GET /control-plane/relationships` inherits it
exactly, not additionally. Flagged here for its own dedicated review, the
same way design note `8e06a475` routed the tunnel-proxy finding to its own
item rather than blocking this one on it.

## Testing

* `tests/test_control_plane_ui.py` — role matrix, parent/child visibility,
  workspace/project scope enforcement, pagination, redaction, missing/stale
  artifact display, hosted-mode/extension-unavailable degradation. Run
  serially (`pixi run python -m pytest tests/test_control_plane_ui.py -p
  no:xdist -q`) per this item's own instruction.
* `meridian/static/dashboard-control-plane.test.ts` — the pure client-side
  helpers (relationship nesting/scope labels, role gating, display-safety
  redaction, artifact health classification, pagination arithmetic).
