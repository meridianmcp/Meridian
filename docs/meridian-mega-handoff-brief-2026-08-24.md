# Meridian implementation mega-handoff brief

Date: 2026-08-24  
Project: `meridian-build` (`5787cc92-ba7d-4788-b17c-28ab7938b839`)  
Scope: `current` only. No version increase is authorized.

The canonical Meridian `/goal` handoff was generated for this scope. This file
is the human-readable execution brief; the canonical project-scoped handoff
and live board remain authoritative.

## First execution wave

Work in isolated worktrees. Start a Meridian session, read the live board, and
claim only items that are not already owned. Existing live ownership claims
must be reconciled through the board; do not overwrite another executor's
files or assume its work shipped.

1. MDE-1 `6d49ef1f-bd7a-4263-b659-f63a620b8c35` — capability manifests and
   fail-closed handoff executability.
2. MDE-2 `8982bdea-a2aa-41a4-a392-06d340da0fd9` — code-intel prospect receipts
   and worktree identity.
3. MDE-6 `77cdfd87-568c-48fc-a70b-fe5d554db204` — BM25/Tantivy cold start,
   dependency drift, fallback state, and truthful convergence.
4. Anthropic Fix #2 `68b7bd9a-f3b8-4994-a63d-4cf9fff43424` — explicit storage
   disclosure in generated MCP descriptions.
5. Anthropic Fix #3 `f1c6dd63-8c9b-4006-8dcc-3845e3915cd2` — explicit connected
   GitHub repository/GitHub Actions disclosure for all six GitHub tools.

Then continue MDE-3 through MDE-9 in the dependency order recorded in the
investigation report. Do not mark an item complete without focused tests,
prospecting evidence, and exact changed-file notes.

## Required tools and fallback rules

- Meridian MCP: `start_session`, `get_sprint_items`, `claim_sprint_item`,
  `log_task`, `complete_sprint_item`, `generate_handoff`.
- Code discovery: codebase-memory graph first; Serena when available; use
  scoped direct reads only when those connectors are unavailable and record
  degraded prospecting.
- Meridian Docs and Meridian Outputs for document/provenance/index work.
- Local verification: focused `pixi run python -m pytest ... -q`; run the full
  `pixi run test -n 3` gate at the end, recording the known Windows worker
  termination honestly if it recurs.

## Hard constraints

- No version bump, release tag, push, merge-to-main, or production deploy.
- No API-key/secret injection, external-account creation, or public submission.
- Cloud-facing adapters and deployment-safe scaffolding are allowed; credentials
  and production actions remain human-gated.
- Temporary drafts, caches, prestage, and QA artifacts stay on local
  non-synced disk, never OneDrive.
- Hosted Meridian is authoritative when reachable; local resilience is the
  explicit degraded fallback. Never silently claim hosted reconciliation.
- Redis is optional acceleration only; Postgres/Neon remains authoritative.
- Tigris/S3 is deferred until hosted artifact volume or customer demand justifies
  the additional maintenance surface.

## Grounding report

Read before implementation:

`docs/meridian-docs-research-grade-investigation-2026-08-23.md`

The report contains the MDE-1..MDE-9 acceptance criteria, resource pointers,
cloud-cost boundary, Word/Overleaf architecture, provenance/XML requirements,
BM25 fallback contract, and manual approval gates.

## Current verified local state

- Redis runtime dependency is declared in `pyproject.toml`; Redis-focused tests
  pass.
- Capability contracts now expose `availability.unverified` and fail closed for
  required capabilities whose availability cannot be verified; empty manifests
  remain backward-compatible.
- Combined focused regression result: 102 passed, 1 skipped.
- Hosted Fly health is passing on both Machines at deployed version `0.2.6`;
  local changes are not deployed.
- Anthropic and BM25 code changes remain board-owned/in progress until the live
  owners release or complete them. Do not report them as shipped yet.

## Human gates outside this handoff

Only the following require Adam's explicit action: secret injection, external
account/provider setup, production deployment, public directory submission,
release/version changes, and legal/employment actions. These are not executor
work and must remain visible as gates.
