# Meridian launch-readiness plan

**Date:** 2026-08-31  
**Scope:** Meridian core, local MCP runners, tunnel/UI configuration, Meridian Docs,
Meridian Outputs, provenance, object storage, and repository rollout.  
**Status:** planner audit; no version bump, deployment, credential rotation, or
destructive worktree action is authorized by this document.

## Executive status

Meridian is not yet a one-command launch. The pieces exist, but they have
different readiness states:

| Area | Current truth | Launch implication |
|---|---|---|
| Workspace defaults | The live workspace row is set to autonomous execution, Code Intel enabled, `/loop` enabled, automatic refresh enabled, refresh interval 50, and inline handoff pointers enabled. | Good default baseline. Existing projects still need an explicit inheritance/reconciliation audit. |
| Local code intelligence | Serena and the local proxy have been independently smoke-tested, but the Codex connector currently reports no active/invocable tunnel slots. | Local stdio should be the fast-path. Tunnel status must distinguish “hosted control plane reachable” from “local slot callable.” |
| Tunnel | `meridian/tunnel_client.py` and the plugin registry support many slots, but current live status has been false/stale in this session. The runtime has ownership, timeout, and memory-risk surfaces that need a bounded preflight. | Do not advertise a plugin as ready from configuration alone. Every slot needs a bounded cold-start and warm-call receipt. |
| Meridian Docs | Local DOCX/OOXML intelligence is a real standalone package. It is separate from the authoring/Word slot. | Keep Docs local-first and out of the hot tunnel loop for development. |
| Meridian Outputs | Local output walking, DuckDB/BM25 search, fingerprints, provenance, and JSON/XML envelope round-tripping exist. | Outputs can run locally, but its local directory, ledger, index, and provenance receipts need one explicit run manifest and bounded-walk contract. |
| Generic XML/JSON inspection | No generic, tunnel-independent, single-file MCP inspector exists yet. The gap is recorded as `LOCAL-FILE-INSPECTION` (`2ffd763d-9d1d-4928-82f7-ff4fb67a5113`). | Add one small local capability instead of overloading Docs or Outputs. |
| Tigris/S3 | `meridian/object_store.py:TigrisObjectStoreBackend` is an explicit `NotImplementedError` stub. The default backend remains local disk. | Secrets alone do not activate anything. Keep Tigris optional and inactive until the client, adapter, sync worker, and live smoke test exist. |
| Provenance | Outputs has a durable local JSON ledger and lossless JSON/XML envelope projection; core artifacts are content-addressed. | Keep provenance metadata and receipts durable in Postgres/local ledgers. Object storage may hold immutable payloads or bundles, never be the only provenance authority. |
| Repository state | `dev` is ahead of `origin/dev` and the shared checkout has tracked edits plus untracked diagnostic/report files; several sibling worktrees remain registered. | A release candidate must be assembled in a clean, pinned integration worktree after ownership is audited. Do not wipe the shared root. |

## Newly confirmed P0 release blocker: credential-bearing local configs

The parallel configuration audit found live-looking credentials in local
client/config files and process-argument paths, including the repository's
ignored `.mcp.json`, `.cursor/mcp.json`, `.meridian/config`, `.codex/config.toml`,
and the global Claude Desktop configuration. The audit did not copy or print
any values. Ignored does not mean safe: values can remain in backups, logs,
shell history, or process inspection.

Before any public launch or submission validation:

1. Revoke/rotate any exposed Meridian and payment credentials using the
   provider dashboards. This is a human-only security action; do not paste
   replacement values into chat or commit them.
2. Remove literal credentials from generated configs and command arguments.
   Use environment/keychain injection and a credential-free config template.
3. Stop accepting query-string tokens for new generated flows; keep bearer
   headers or an equivalent protected channel, and redact tokens in logs.
4. Add a launch test that scans tracked files, generated snippets, process
   arguments, and diagnostic output for secret-shaped values without printing
   matches.

This gate takes precedence over UI polish, Tigris activation, and a production
deploy. A release candidate is not ready while these credentials remain live
or while the client topology is ambiguous.

## Architecture decision for launch

Use three explicit runtime tiers:

1. **Local-first:** Claude Desktop and local Claude Code use stdio launchers for
   Meridian, Serena, codebase-memory, Meridian Docs, Meridian Outputs, and any
   bounded local-file inspector. This is the development and recovery path.
2. **Hosted coordination:** hosted Meridian provides project state, handoffs,
   sprint state, capability manifests, and optional remote APIs. It does not
   pretend to read a caller's local path unless a local bridge is actually
   connected.
3. **Tunnel bridge:** use only when a browser/hosted session needs a local
   capability. The bridge must be capability-scoped, repo-scoped, budgeted, and
   independently health-checked. A tunnel control connection is not proof that
   every plugin slot is callable.

This preserves fast iteration and makes a tunnel outage a degraded local
capability rather than a total Meridian outage.

## Required launch waves

### Wave 0 — truth and preflight (planner-owned gate)

- Reconcile every existing project’s execution mode, loop setting, refresh
  setting, Code Intel setting, and executor configuration against the live
  workspace defaults. Preserve explicit project overrides; flag mismatches.
- Inventory `.mcp.json`, `.cursor/mcp.json`, Claude Desktop configuration, and
  per-project executor configuration without copying bearer tokens or secrets.
- Produce a machine-readable launch matrix with one row per project and one row
  per capability: launcher, cwd/repo scope, availability, health result, owner,
  and fallback.
- Audit all registered worktrees and dirty paths. Partition by owner/branch;
  preserve unknown changes; create a reversible manifest before any merge or
  removal.

### Wave 1 — local runner and plugin health

- Implement the existing `LOCAL-RUNNER-FOUNDATION` and `TUNNEL-P0-HARDENING`
  items (`899936dd-46eb-4e8a-9ae3-c3323d8ede98`,
  `7b457c55-60f5-4b3a-8560-f40c3d9a5916`).
- Make process ownership, memory ceilings, stdout/stderr caps, idle cleanup,
  restart backoff, and per-slot health receipts real rather than advisory.
- Make `active`, `invocable`, `tunnel_active`, `stale_override`, and
  `last_health_check` semantically consistent in the UI and MCP responses.
- Keep the existing `SerenaDaemonPool`/process-lifecycle primitives; do not
  rebuild or reinstall Serena per request. Key the pool by normalized repo and
  reconcile with a host-local lease.

### Wave 2 — local capability bundle

- Implement `LOCAL-FILE-INSPECTION` as a local-only, bounded MCP capability for
  one XML/JSON file at a time.
- JSON: deterministic key ordering, bounded bytes/depth/items, explicit parse
  errors, no writes.
- XML: bounded bytes/depth/nodes, DTD/external-entity rejection, safe namespace
  handling, deterministic summary, no writes.
- Keep it separate from Meridian Docs (DOCX/OOXML semantics) and Meridian
  Outputs (output indexing/provenance). Expose it in local Claude Desktop and
  Claude Code bundles only after a smoke test.

### Wave 3 — Outputs/provenance integration

- Add a canonical run manifest joining project, repo identity, input/output
  fingerprints, tool/package versions, command identity, wall-clock/resource
  bounds, artifact IDs, provenance ledger location, and receipt status.
- Ensure `search_outputs`, `file_fingerprint`, provenance envelope operations,
  and output walking return bounded/degraded states instead of hanging or
  silently claiming completeness.
- Keep the durable record in Postgres or the local ledger. Store content and
  export bundles separately; every remote object must be content-addressed and
  linked to a durable receipt.
- Preserve the existing lossless JSON/XML envelope. Markdown is a presentation
  projection, not the canonical storage or interchange format.

### Wave 4 — optional Tigris activation

- Treat current Tigris state as **inactive**, regardless of whether environment
  variables or hosted secrets exist.
- Review and approve one S3-compatible client; implement the adapter behind the
  existing `ObjectStoreBackend` protocol; translate auth, not-found, conflict,
  precondition, quota, and transient failures into the existing error taxonomy.
- Add idempotent local-first sync, per-project/tenant key prefixes, retry/outbox
  state, deletion/retention semantics, and signed URL policy.
- Start with `object_storage_sync` as optional/degraded; pilot one project; run
  a real bucket smoke test; only then consider broader hosted rollout.
- Never move the authoritative provenance ledger into Tigris. Tigris can hold
  immutable payloads, exports, render bundles, and large receipts; Postgres or
  the local ledger owns identity, lineage, access state, and reconciliation.

### Wave 5 — UI and distribution

- Add one runtime-health view showing local/hosted/tunnel state per capability,
  current repo scope, last check, failure class, fallback, and remediation.
- Add a safe “copy local MCP bundle” flow that generates config from a template,
  uses project-relative placeholders, and never embeds secrets in committed
  files or shared handoff text.
- Add explicit project-scope guards to prevent a child/subproject from silently
  activating a parent repo or another project’s worktree.
- Add a Windows packaged runner only after the local runner contract is stable;
  the UI is a distribution layer, not a substitute for lifecycle correctness.

## Release acceptance gate

The launch candidate is ready only when all of the following are true:

- A clean pinned worktree and commit SHA are recorded.
- Every shipped capability has a cold-start, warm-call, bounded-failure, and
  shutdown receipt on the target operating system.
- Local Docs and Outputs work without a tunnel against an explicit test fixture
  directory/repo; hosted calls return an honest unavailable/degraded result when
  a local path cannot be reached.
- Generic XML/JSON inspection rejects unsafe XML constructs and produces stable
  summaries.
- Provenance survives a process crash, partial output walk, index rebuild, and
  remote outage; no result is marked complete without its required receipt.
- Tigris is either still explicitly inactive or has passed its separate pilot
  gate. Secrets being present is never accepted as activation evidence.
- Local MCP configuration is validated for JSON shape, launcher existence,
  correct working directory, project scope, and absence of embedded credentials.
- No unrelated dirty path, thesis/document artifact, or sibling worktree is
  silently included in the release candidate.
- Version remains unchanged until the human explicitly requests a release.

## Manual actions versus code actions

### Manual only

- Decide which existing project overrides should be preserved versus reset to
  workspace defaults.
- Review the dirty-path/worktree ownership manifest before integration.
- Approve the S3 client and Tigris pilot scope.
- If activating Tigris later, create/verify the bucket, access policy, and
  hosted secret bindings outside the repository, then run the live smoke test.
- Restart Claude Desktop/Code after a validated local MCP configuration change.

### Code/config work

- Local runner lifecycle and tunnel health contract.
- Generic bounded XML/JSON inspector.
- Per-project MCP bundle generation and scope validation.
- Outputs run manifest and provenance/receipt reconciliation.
- Optional Tigris adapter, sync worker, and capability-manifest wiring.
- UI health/readiness views and project-settings reconciliation.

## Existing records to reuse

- Research/control-plane proposal: `a08defbc-481f-4354-915d-462a254e5e75`.
- Local runner investigation: `docs/meridian-local-runner-tunnel-investigation-2026-08-31.md`.
- Object-storage boundary contract: `docs/object-storage-backend.md`.
- Local generic inspector item: `2ffd763d-9d1d-4928-82f7-ff4fb67a5113`.
- Local runner foundation: `899936dd-46eb-4e8a-9ae3-c3323d8ede98`.
- Tunnel hardening: `7b457c55-60f5-4b3a-8560-f40c3d9a5916`.
- Desktop runner packaging: `76f4b38d-7b08-4800-8f8c-3b6053eeb1e7`.
- Tigris/S3 build boundary: `1d34c076` and follow-on activation item `7197907f`.

## Explicit non-goals

- No blanket deletion of registered worktrees or dirty files.
- No replacement of Postgres/local provenance with an object store.
- No automatic activation of Tigris merely because credentials exist.
- No requirement that local development use a tunnel.
- No universal parser that duplicates Meridian Docs or Meridian Outputs.
- No version increase or production deployment as part of this planning pass.

## Related storage and inspection contract

The storage placement, provenance authority, Tigris boundary, and bounded
XML/JSON/CSV/XLSX inspector design are specified in
`docs/meridian-storage-and-file-inspector-contract-2026-08-31.md`. That
contract is the implementation source for `LOCAL-FILE-INSPECTION` and the
follow-on tabular adapter item; it intentionally keeps both capabilities
local-first and tunnel-independent.
