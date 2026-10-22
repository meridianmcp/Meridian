# Local agent artifact capture

`meridian artifacts` stores selected local output files in Meridian's existing
project-scoped content-addressed artifact store and keeps a separate occurrence
manifest for event/run lineage. Capture is **off by default**. It does not send
files, local paths, event payloads, or chat transcripts to hosted Meridian, and
it does not activate Tigris, S3, or another remote store.

## Select what may be captured

Choose existing output directories inside the project root. The initial default
extension allowlist is `.bib`, `.csv`, `.docx`, `.html`, `.jpeg`, `.jpg`,
`.json`, `.md`, `.pdf`, `.png`, `.pptx`, `.svg`, `.tex`, `.txt`, `.webp`,
`.xlsx`, and `.zip`. Text is passed through Meridian's secret redactor before
storage. Binary files are stored unchanged, so select roots and file extensions
carefully.

```powershell
meridian artifacts configure `
  --project-id meridian `
  --root "C:\work\meridian" `
  --output-root "C:\work\meridian\outputs" `
  --subproject "reports"

meridian artifacts status --project-id meridian
```

Pass `--extension pdf --extension docx` to replace the default allowlist with a
smaller one. `--max-bytes` defaults to 25 MiB and is capped at 100 MiB. An
optional `--outputs-dir` adds local registration and hash verification through
the Meridian Outputs registry. That registry is a local file operation.

The capture config and occurrence manifest live under
`$MERIDIAN_DATA_DIR/artifact_capture/` when `MERIDIAN_DATA_DIR` is set, or under
`~/.meridian/local_runner/artifact_capture/` by default. The content-addressed
bytes remain in Meridian's local artifact store. Config and manifest files are
written with owner-only permissions where the host supports them.

## Provider event support

This repository registers a quiet Claude Code `PostToolUse` hook for
`Write`, `Edit`, `MultiEdit`, and `NotebookEdit`. The hook reads only a bounded
event payload, extracts an explicit output path, and then applies the selected
project roots, extension, size, ignored-path, and file-stability checks. It
discards the event body and never reads a provider transcript. If a project has
not been configured, the hook is a no-op.

Codex CLI's `PostToolUse` hook can provide `cwd`, `turn_id`, `tool_use_id`, and
tool input. The adapter accepts explicit file paths and extracts `Add File`,
`Update File`, and `Move to` paths from an `apply_patch` input. It reads only the
resulting files and never saves the patch text. To opt in on a host where the
`meridian` command is installed, add this to Codex's project or user
`config.toml`:

```toml
[[hooks.PostToolUse]]
matcher = "Write|Edit|NotebookEdit|^apply_patch$"

[[hooks.PostToolUse.hooks]]
type = "command"
command = "meridian artifacts hook"
async = true
timeout = 30
```

The command hook must run in a local host with access to the same local output
directory and Meridian data directory. Codex tool coverage can vary by tool and
host mode; shell-created files, cloud sessions, and tools that do not expose a
file path remain manual-only. Neither hook watches a workspace, captures
shell stdout, nor archives chats. See the [Claude Code hook reference](https://code.claude.com/docs/en/hooks)
and [Codex hook reference](https://developers.openai.com/codex/hooks/) for the
provider event contracts.

Manual capture is available from either CLI after a Codex or other local
workflow:

```powershell
meridian artifacts capture `
  --project-id meridian `
  --source "C:\work\meridian\outputs\summary.pdf" `
  --provider codex_cli `
  --event-id "local-export-2026-10-03" `
  --run-id "run-42"
```

`meridian artifacts list --project-id meridian` shows occurrence metadata.
Each occurrence records the project and optional subproject/run, provider and
event identifiers, relative source path, media type, source size/hash, stored
content hash/size, redaction result, capture time, consent trigger, registry
result, and capture verification. Identical bytes can share one stored blob
while separate occurrences preserve event/run lineage.

## Restore, export, and retention

Restore is limited to a currently configured output root, refuses to overwrite
by default, verifies the stored blob before writing, and re-hashes the restored
file before returning success:

```powershell
meridian artifacts restore --project-id meridian --occurrence-id OCCURRENCE_ID
```

Use `--destination` for another path under a selected output root and
`--overwrite` only when replacing an existing file is intended. Export writes a
local ZIP with the active occurrence manifest and verified blobs:

```powershell
meridian artifacts export --project-id meridian --output "C:\backup\artifacts.zip"
```

`unlink` removes one occurrence from the active lineage view and leaves its
stored bytes in place. Project purge requires the exact project id as
`--confirm-project`; it deletes blobs referenced by active capture occurrences
from the project's artifact-store namespace and marks those occurrences
purged. The artifact store deduplicates bytes within a project, so purging a
capture can also remove an identical blob that another local workflow in that
same project refers to. Export first if those bytes are needed elsewhere.

```powershell
meridian artifacts unlink --project-id meridian --occurrence-id OCCURRENCE_ID
meridian artifacts purge --project-id meridian --confirm-project meridian
meridian artifacts disable --project-id meridian
```

No remote copy is made by any command in this workflow. A future remote tier
requires a separate explicit opt-in and authorization design.
