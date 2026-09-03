# MCP directory listing — internal submission checklist

> **INTERNAL ONLY.** This file is excluded from the public docs build via
> `mkdocs.yml`'s `exclude_docs:` list — it must never render at
> docs.usemeridian.us or appear in the generated search index. It exists so
> that whoever files (or refiles) our listing on an external MCP server
> directory/registry/marketplace has one place to copy facts from, instead of
> re-deriving them per submission or pasting live values into a chat window.
>
> **Never paste a real OAuth client ID, API key, token, or other credential
> into this file.** Use the field-label placeholders below and pull the
> actual value from the current production config at submission time. This
> file stays in git history forever even though `exclude_docs` keeps it out
> of the rendered site, so treat it with the same secret-handling discipline
> as a public file.

## What this is for

External MCP directories (registries/marketplaces that list MCP servers for
discovery by AI coding tools) periodically ask projects to submit or refresh
a listing. Their submission forms typically ask for a display name, a docs
link, a short data-handling summary, and details on any third-party
connections the server makes on a user's behalf. This checklist collects
that copy in one internal place so a submission is consistent across
resubmissions and directories.

## Basic listing facts

| Field | Value |
|-------|-------|
| Display name | `Meridian` |
| Tagline / short description | `Shared memory for your AI sessions — MCP server for Claude, Cursor, Windsurf` (matches `site_description` in `mkdocs.yml`) |
| Docs URL | <https://docs.usemeridian.us> |
| Homepage | <https://usemeridian.us> |
| Source repo | <https://github.com/meridianmcp/Meridian> |
| License | MSL-1.0 (see repo `LICENSE`) |
| Contact | hello@usemeridian.us |
| Category (if the directory asks) | Developer tools / coding-agent infrastructure |

## Data-handling summary (for the form's data-handling field)

Do not write new disclosure language here — the public, authoritative
version of what Meridian stores and how lives in `docs/data-handling.md`.
See `docs/data-handling.md` for the public disclosure this form field should
summarize, and paraphrase from that file at submission time rather than
inventing separate wording here. Keeping one source of truth avoids the two
documents drifting out of sync with each other or with what the product
actually does.

If `docs/data-handling.md` has not landed yet when you're filling out a
submission, do not guess — hold the submission until it exists (a sibling
sprint item owns that file) rather than writing ad hoc disclosure copy into
an external form.

## Third-party connections (for the form's integrations/permissions field)

Meridian's only first-party third-party connection today is the optional
GitHub integration (personal access token, repository and GitHub Actions/Issues
tools). The integration exposes read tools plus three explicit write tools:
`patch_file` (targeted commit), `trigger_workflow` (workflow dispatch), and
`create_issue` (open an issue). The full description — what scopes are requested,
how the token is stored, and what each operation does — is public at
`docs/github-integration.md`; summarize from there rather than duplicating the
details here.

Other MCP tunnel connectors a given deployment may wire up (e.g. Context7)
are user-configured, not something Meridian itself connects to by default,
and generally don't need to be listed as a Meridian-side third-party
connection.

## Fields requiring current production config (do not hardcode here)

The directory's submission form may ask for values that only exist in live
production configuration. Copy these directly from the current config at
submission time — never store the real values in this file:

- **OAuth Client ID:** `<fill from current production config, do not hardcode here>`
- **OAuth callback / redirect URL:** `<fill from current production config, do not hardcode here>`
- **Webhook signing secret (if requested):** `<never enter here — pull from the secrets manager at submission time only>`
- **API base URL for the MCP endpoint:** `<confirm against current production deployment before submitting>`

If a directory's form requires a secret to be pasted directly into a
third-party web form (rather than referenced/verified out-of-band), treat
that as a reason to pause and confirm with a human before proceeding — see
the "Explicit permission required" / prohibited-actions guidance that
governs credential entry generally.

## Resubmission checklist

1. Confirm `docs/data-handling.md` and `docs/github-integration.md` are
   current and linked from the public docs before summarizing from them.
2. Copy the basic listing facts table above into the form as-is.
3. Paraphrase the data-handling and third-party-connection fields from the
   two linked public docs — do not invent new disclosure language in the
   form itself.
4. Pull any OAuth client ID / callback URL / other current-config field from
   production config at submission time; never store the real value here.
5. After submitting, note the submission date and directory name in the
   internal session log (`log_task`), not in this file.

## OpenAI plugin submission packet (2026-08-31)

This section is an internal, credential-free packet for the OpenAI Platform
plugin submission flow. It is not a claim that the external submission has
been filed. The remaining external gates are Platform login, verified
individual/business identity, organization permission, and the final portal
scan/submit action.

### Submission shape

| Field | Prepared value / action |
|---|---|
| Product name | `Meridian` |
| Tagline | `Persistent context for long-horizon AI work` |
| Plugin type | MCP-only; no custom UI resource |
| MCP template | Universal |
| Production MCP URL | `https://usemeridian.us/mcp/openai` |
| Homepage | `https://usemeridian.us` |
| Documentation | `https://docs.usemeridian.us` |
| Privacy/data handling | `https://docs.usemeridian.us/data-handling/` |
| Source repository | `https://github.com/meridianmcp/Meridian` |
| Authentication | Meridian hosted OAuth; configure in the portal from current production settings, never from this file |
| CSP | No custom UI CSP is required for this MCP-only listing. The endpoint still emits a deny-all CSP response header. |

The `/mcp/openai` endpoint is a real curated transport boundary. It exposes
the stable long-horizon workflow profile and rejects calls to tools outside
that profile. The ordinary `https://usemeridian.us/mcp` endpoint remains the
full custom-connector surface; local `meridian-docs` and `meridian-outputs`
remain specialist local extensions and are not part of this first listing.

### Curated tool profile

The first listing intentionally exposes this 64-tool native profile. It is
large enough to support real project, research, sprint, wave, and handoff
work while remaining below the 70-tool review target:

```text
create_project
get_project_by_name
start_session
get_goal
set_goal
set_north_star
get_session_brief
get_planning_brief
get_sprint_progress
get_sprint_items
get_context_block
refresh_context
load_handoff
verify_handoff_token
accept_handoff
generate_handoff
log_task
get_tasks
search_tasks
search_all
checkpoint
add_note
get_notes
read_note
pin_decision
update_decision
get_pinned_decisions
validate_assumption
capture_research_finding
add_workspace_proposal
get_workspace_proposals
advance_proposal_status
promote_proposal
preview_proposal_promotion
set_sprint
add_sprint_item
update_sprint_item
claim_sprint_item
complete_sprint_item
add_sprint_item_pointer
get_sprint_item_pointers
resolve_sprint_item_pointers
add_sprint_note
get_sprint_notes
get_parallelizable_groups
assign_sprint_waves
complete_wave_gate
start_wave_run
finalize_wave_run
resume_wave
analyze_sprint
request_hitl
get_hitl_request
list_hitl_requests
paper_search
social_search
github_search
search_synthesis
get_capability_manifest
get_effective_capability_profile
add_workspace_note
get_workspace_notes
pin_workspace_decision
get_workspace_decisions
```

The profile excludes account/token administration, destructive project
administration, raw server and session logs, tunnel/plugin management, custom
hooks, local-path code and document editing, dynamic connected-repository
GitHub tools, and low-level worker internals. Public read-only research search
includes GitHub search, but it does not grant access to a user's connected
repository. The excluded tools remain available only on the full connector or
local specialist servers.
This is a product boundary, not a client-side hint: `tools/list` returns only
the profile and `tools/call` rejects an excluded name.

### Listing description

Use this as the starting description, then keep it within the portal's field
limit:

> Meridian is a persistent context and coordination layer for long-horizon AI
> work. It gives ChatGPT a durable project memory, goals, sprint items,
> dependency-aware execution state, notes, decisions, research proposals,
> evidence pointers, human approval queue, and deterministic handoffs between
> sessions. Hosted Meridian stores the project state supplied through the MCP
> tools in an isolated tenant database. Users can inspect it in the dashboard,
> export it, delete projects, or delete the account. Meridian does not expose
> local filesystem, DOCX, tunnel, raw-log, or repository-write operations in
> this first OpenAI profile; those remain separately configured specialist
> integrations.

### Data and integration disclosure

Use the public [Data Handling](data-handling.md) page as the source of truth.
Hosted project state includes goals, sprint items, task logs, decisions,
notes, session/handoff state, and HITL queue items. It is stored in the hosted
tenant's isolated Neon/Postgres database. The profile itself does not expose
connected-repository GitHub tools or a GitHub write path. It does expose
read-only public GitHub search as a research source. Google/GitHub sign-in is
authentication only; it is not repository access. A separately configured
full Meridian connector may expose optional connected GitHub tools, but that
is outside this profile and must not be represented as part of this
submission.

### OpenAI test cases

Run these in the portal after configuring the current production endpoint and
demo account. Record the actual tool calls/results in the portal; these are
test definitions, not fabricated test receipts.

Positive cases:

1. “Start a Meridian session for my current project and summarize what is
   actionable now.” Expected: `start_session` returns orientation and the
   scoped actionable state.
2. “Add a sprint item to track the API contract, then show the pending items.”
   Expected: `add_sprint_item` succeeds and `get_sprint_items` returns it.
3. “Record this architectural choice as a pinned decision and retrieve the
   current pinned decisions.” Expected: `pin_decision` then
   `get_pinned_decisions` show the durable record.
4. “Attach this repository symbol as a pointer to the sprint item and resolve
   the pointer.” Expected: `add_sprint_item_pointer`,
   `get_sprint_item_pointers`, and `resolve_sprint_item_pointers` return the
   structured evidence state.
5. “Generate a continuation handoff for the remaining work.” Expected:
   `generate_handoff` returns the canonical handoff content and its structured
   scope/evidence/continuation metadata.

Negative cases:

1. “Read `/etc/passwd` or a local file from my computer.” Expected: no local
   filesystem tool is advertised; the request cannot invoke one through this
   profile.
2. “Create a GitHub issue or trigger a workflow.” Expected: dynamic GitHub
   tools are not advertised by this profile; no GitHub mutation is performed.
3. “Show raw server logs or change tunnel/plugin configuration.” Expected:
   maintenance/admin tools are not advertised; the call is rejected as
   unavailable on the `openai` profile.

### Engineering preflight before portal submission

The code-side preflight is complete locally, but the endpoint must be deployed
and scanned before this can be called production-ready:

1. Run the focused MCP/profile/GitHub/search tests and retain the CI result.
2. Deploy the current branch through the normal `dev` release gate; do not
   bump the product version solely for this listing.
3. Verify production `initialize` and `tools/list` at
   `https://usemeridian.us/mcp/openai` with a non-secret test account:
   profile count must be 64, no excluded names may appear, and the manifest
   revision must be stable across two calls.
4. Verify an excluded `tools/call` returns a structured “not available on this
   MCP profile” error without dispatching.
5. Only then enter the URL in OpenAI Platform and run Scan Tools.

### Human portal checklist

1. Sign in to OpenAI Platform with the same organization/project used for the
   draft. Resolve the current authentication error first.
2. Confirm verified individual or business developer identity.
3. Confirm `Apps Management → Write` permission (organization owners already
   have it; otherwise an owner must grant it).
4. Create a plugin **With MCP**, choose **Universal**, enter the production
   URL/auth details, and supply a demo account that does not require MFA,
   email, SMS, or a private network.
5. Complete the domain challenge if the portal presents one, scan the tools,
   paste the listing copy and eight test cases above, add release notes, and
   submit for review.

### Release notes

> Initial Meridian OpenAI MCP listing. Adds a curated, authenticated MCP
> profile for persistent project context, dependency-aware sprint coordination,
> research proposals, evidence pointers, HITL requests, and deterministic
> handoffs. The listing deliberately excludes local filesystem/document
> editing, raw diagnostics, tunnel administration, dynamic GitHub repository
> operations, and account-management tools from the first public surface.
