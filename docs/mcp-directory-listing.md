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
