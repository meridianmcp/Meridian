# Provider-Native Session Recovery and Local Context-Pack Contract

**Discovery item:** `b6a82b7c-1dd1-4c71-a08b-daa74c850015`
**Sprint:** `hosted-workstation-megasprint-2026-10-01`
**Evidence checked:** 2026-10-02
**Status:** evidence-backed discovery and proposed contract; no implementation change

## Scope and terms

This report maps provider-owned resume/history surfaces and locally observed
history paths on the current Windows workstation, then defines a local-only
continuation-pack shape. Provider binaries, account access, and resume commands
were not smoke-tested on this workstation; the command and export claims below
come from provider documentation or source.
The workstation inventory below used filesystem metadata only: file paths,
counts, sizes, and modification times. No conversation or transcript contents
were opened, parsed, or uploaded. The measurements describe this workstation at
the time of the scan; they are not product-wide retention guarantees.

Keep four recovery outcomes distinct:

1. **Native resume** asks the original provider client to continue a session
   using its own session/thread identifier.
2. **Meridian board recovery** rebuilds current sprint state and active claims.
   It is task coordination, not conversation history.
3. **A local context pack** reconstructs a new session from a compact,
   provenance-linked task summary. It is not a transcript replay.
4. **An archive** preserves raw provider history for inspection where the
   provider supports export. An archive does not imply that the provider can
   import or resume it.

## Support matrix

| Surface | Native continuation or history source | What can be verified | Adapter guidance |
|---|---|---|---|
| **Claude Code CLI / Agent SDK** | `claude -r <session-id>` / `claude --resume <session-id>` resumes by ID; `claude -c` / `--continue` resumes the most recent conversation in the current directory. The Agent SDK exposes `list_sessions` / `get_session_messages`, `resume`, and `fork`. | Anthropic documents local session files at `~/.claude/projects/<encoded-cwd>/*.jsonl` (or `$CLAUDE_CONFIG_DIR/projects/`), and says the file must still exist on the same machine. Agent SDK interfaces are the supported catalog/read surface; the docs describe the path but not a stable general-purpose JSONL schema. The workstation's observed `.claude\projects` tree matches that documented location. | Prefer the native CLI or Agent SDK catalog/read/resume interfaces. Use direct file parsing only in a versioned adapter. Cross-host SDK `SessionStore` or transcript-file mirroring is a separate opt-in and must not send history through Meridian. Do not infer that `history.jsonl` is resumable. [Anthropic CLI reference](https://docs.anthropic.com/en/docs/claude-code/cli-usage), [Agent SDK session guide](https://code.claude.com/docs/en/agent-sdk/sessions) |
| **Claude web / Claude Desktop account chats** | Provider UI history; active Free/Pro/Max individuals and Primary Owners of Team/Enterprise workspaces can request account or organization exports from Settings. | Anthropic documents a downloadable export and a 24-hour download link. This is an account export workflow, not a documented import/resume interface for Claude Code local sessions. | Keep UI history and manual export separate from the Claude Code local-session adapter. Do not automate export requests or treat exported history as a restorable session. [Anthropic data export](https://support.anthropic.com/en/articles/9450526-how-can-i-export-my-claude-data) |
| **Codex CLI** | `codex resume [SESSION_ID]` resumes a recorded session; the CLI can also offer a picker or latest-session option. | The official Codex implementation defines local `sessions` and `archived_sessions` rollout stores, JSONL rollout records, and lookup by thread/session ID. The current workstation has both stores plus `session_index.jsonl`. These are local implementation surfaces and can evolve with Codex versions. | Prefer the installed CLI's `codex resume` path. A local catalog adapter may index bounded metadata and IDs, but should version its parser and retain unknown records unchanged. Do not assume every cloud/app thread has a local rollout. [Codex CLI source](https://github.com/openai/codex/blob/main/codex-rs/exec/src/cli.rs), [rollout storage source](https://github.com/openai/codex/blob/main/codex-rs/rollout/src/lib.rs), [rollout lookup source](https://github.com/openai/codex/blob/main/codex-rs/rollout/src/list.rs) |
| **Codex app / app-server threads** | The app-server protocol resumes a thread with its saved thread ID. This Codex host also exposes app-mediated thread/chat listing and reading tools. | A provider-owned thread ID and app-mediated access are available; those surfaces are not equivalent to a portable local file API. | Use the app/server resume or read interface when it is available. Preserve provider and surface alongside the ID; do not resolve an app thread by scanning unrelated local rollout files. [Codex app-server example](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server) |
| **ChatGPT web / account chats** | Provider UI history. Archived conversations can be unarchived. Eligible accounts can request a ZIP export containing chat history. | OpenAI says archived chats remain saved and searchable; deleted chats cannot be restored. Self-service export is unavailable for Business, Enterprise, and Healthcare workspaces, and may be restricted for Edu by role/settings. Export delivery can take up to seven days and its link expires after 24 hours. | Use the UI to resume or unarchive. Treat export as a user-requested archive, not a local session catalog or import path. Never claim deleted-chat recovery. [Archive and delete behavior](https://help.openai.com/en/articles/8809935-deleting-and-archiving-chats-in-chatgpt), [data export](https://help.openai.com/en/articles/7260999-exporting-your-chatgpt-history-and-data) |
| **Meridian recovery registry** | Project-scoped session record, live board snapshot, current file claims, and local resume mapping. | The hosted record is keyed by Meridian `sessions.id`; provider IDs, bridge/environment IDs, transcript paths, and command arguments belong in a local snapshot. Continuation re-derives the live board for the recorded sprint version instead of replaying an old `/goal`. | Use this alongside a provider surface. `pending_goal` / `load_handoff` and board state provide task context, not raw conversation history. See [Meridian handoff contract](meridian-handoff-contract.md) and [handoff mode contract](meridian-handoff-mode-contract-2026-08-26.md). |

The phrase **native resume** in this matrix means a provider's own documented
client or protocol. It does not promise portability across machines, accounts,
workspaces, working directories, or provider versions.

## Workstation inventory (metadata only)

The scan checked default roots and did not inspect file contents. `Bytes` is the
sum of file lengths. Dates are UTC file modification times, so they are evidence
of files present, not proof of conversation coverage or retention policy.

| Root pattern | Metadata observed | Interpretation |
|---|---:|---|
| `%USERPROFILE%\.claude\projects` | 17,894 files; 8,710 `.jsonl`; 7,194,886,949 bytes; oldest observed 2026-04-11, newest 2026-10-01 | Local Claude Code project history candidate. Path and JSONL presence are observed locally; schema and completeness are not established by this scan. |
| `%USERPROFILE%\.claude\history.jsonl` | 52,175 bytes; modified 2026-08-30 | Present, but no assumption is made that it contains complete or resumable session history. |
| `%USERPROFILE%\.codex\sessions` | 707 `.jsonl`; 10,431,714,552 bytes; oldest observed 2026-06-12, newest 2026-10-02 | Local Codex rollout store matches the upstream implementation's `sessions` directory concept. |
| `%USERPROFILE%\.codex\archived_sessions` | 148 `.jsonl`; 534,167,013 bytes; oldest observed 2026-02-10, newest 2026-10-01 | Archived Codex rollout store; keep it in catalog scope. |
| `%USERPROFILE%\.codex\session_index.jsonl` | 58,051 bytes; modified 2026-10-01 | Present; contents were not read. |
| `%APPDATA%\Claude\claude-code-sessions` | 29 files; 18,302,025 bytes; no `.jsonl` files | Present but purpose/schema was not established; exclude from automatic ingestion until documented and explicitly supported. |

The Claude Code and Codex trees contain multiple gigabytes. A recovery catalog
must therefore be bounded and incremental: inventory paths and metadata first,
avoid repeatedly parsing the full tree, and open content only after the user
selects a provider session and the adapter has validated its version and source
format.

## Existing Meridian recovery boundary

The current code already establishes a useful privacy boundary:

- `register_session_recovery` records a Meridian session ID, transport,
  client type, lifecycle, a non-identifying `verified_resumable` boolean,
  an opaque `local_ref_id`, sprint version, and checkpoint/handoff references.
  The database API does not accept a provider transcript ID as its session ID.
- Self-hosted servers may keep the mapping under
  `<data_dir>/session_recovery/<safe-project-id>.json`, because that process
  runs on the same machine as its caller. Hosted servers must never write this
  mapping to Fly's ephemeral `data_dir`.
- Hosted Claude Code callers use `.claude/hooks/session_recovery_hook.py`:
  `PreToolUse` writes the provider identity and resume recipe to
  `~/.meridian/session_recovery/client_local.json` (override with
  `MERIDIAN_SESSION_RECOVERY_STATE_DIR`) and removes `local_identity` before
  sending the tool request. The hosted row receives only an opaque random
  `local_ref_id` and a non-identifying resumability hint. A hosted call that
  still contains `local_identity` is rejected. `PostToolUse` adds a local
  recipe only when both the opaque reference and requested Meridian session
  id match this workstation's record.
- `SubagentStart` and `SubagentStop` update only the caller-local lifecycle
  map: provider session id, agent id/type, state, and timestamps. The hook
  ignores transcript paths and `last_assistant_message`. The mapping may hold
  `local_session_id`, `bridge_id`, `environment_id`, `local_transcript_path`,
  `argv`, and the computed recipe or blocked reason; none of these are sent to
  hosted Meridian. `local_ref_id` is random and is not derived from those values.
- Clients without the local hook should omit `local_identity`. Hosted recovery
  still stores the safe registry row, but the caller receives no resume recipe.
- `reject_local_only_keys` rejects known host-local identity keys recursively
  when they appear inside hosted metadata. Keep this denylist in step with any
  future provider-specific identifier fields.
- `build_resume_recipe` only formats identity already held by the caller. It
  requires a local session ID for `stdio`, an environment ID for
  `remote_control`, both an environment ID and local session ID for
  `cloud_environment`, and returns no argv recipe for `tunnel` because tunnel
  reconnection is reconnect-based. These are Meridian transport cases, not a
  provider support matrix.
- `build_recovery_continuation` re-reads the live board for the recovery
  record's sprint version and reports this Meridian session's active file
  claims. It explicitly does not replay the stored `/goal` body or release,
  reassign, or complete work.

Source: [session recovery implementation](../meridian/session_recovery.py),
[registry and continuation implementation](../meridian/db/session_recovery.py).
The existing [typed pointer contract](../meridian/pointers.py) distinguishes
structural validity, target resolution, provenance, and freshness. It is a
useful model for evidence status, but this discovery does not establish that
current pointer targets can encode provider event ranges directly.

## Proposed local context-pack contract

This is a follow-on design contract, not shipped behavior. Store the pack in a
local application data directory, separate from tracked repository files and
separate from the provider's source files. Store the provider identity map and
the compact pack locally; only an opaque correlation reference may cross into
the hosted recovery registry.

```json
{
  "schema_version": 1,
  "pack_id": "random-opaque-id",
  "generated_at": "UTC timestamp",
  "project": { "project_id": "...", "sprint_version": "..." },
  "source": {
    "provider": "claude_code | codex_cli | codex_app | chatgpt | other",
    "surface": "cli | app_server | app_ui | account_export",
    "native_session_id": "provider-owned identifier, local only",
    "client_version": "observed version or null",
    "local_root_ref": "configured root alias or null",
    "source_locator": "local locator or provider UI locator",
    "source_sha256": "full source hash or null if not computed",
    "source_size_bytes": "observed size or null",
    "source_mtime_utc": "observed timestamp or null",
    "selected_range_sha256": "hash of selected bounded bytes/records or null",
    "adapter": { "id": "adapter-name", "version": "..." },
    "range": {
      "kind": "event_id | turn_id | line_range | timestamp_window | none",
      "start": "...",
      "end": "...",
      "selector_quality": "exact | coarse | unavailable"
    }
  },
  "task": {
    "objective": { "text": "compact task statement", "source_refs": ["src-0"] },
    "current_step": { "text": "...", "source_refs": ["src-1"] },
    "constraints": [{ "text": "...", "source_refs": ["src-1"] }],
    "decisions": [{ "text": "...", "source_refs": ["src-2"] }],
    "failed_approaches": [{ "text": "...", "source_refs": ["src-3"] }],
    "next_action": { "text": "...", "source_refs": ["src-6"] },
    "recency_tail": [{ "summary": "...", "source_refs": ["src-4"] }]
  },
  "verified_state": {
    "observed_at": "UTC timestamp",
    "repo_root_ref": "local alias or null",
    "git_head": "commit or null",
    "worktree_state": "clean | dirty | unknown",
    "artifact_refs": [{ "uri": "...", "sha256": "...", "status": "..." }],
    "source_refs": ["src-5"]
  },
  "source_refs": [{
    "id": "src-1",
    "provider": "...",
    "native_session_id": "local only",
    "source_locator": "local locator or provider UI locator",
    "source_sha256": "... or null",
    "source_size_bytes": "... or null",
    "source_mtime_utc": "... or null",
    "selected_range_sha256": "... or null",
    "range": { "kind": "turn_id", "start": "...", "end": "..." },
    "captured_at": "UTC timestamp",
    "resolution": "verified | stale | unavailable | coarse"
  }],
  "coverage": {
    "complete": false,
    "truncated": false,
    "omissions": [],
    "redaction": "reviewed | automatic | not_checked"
  },
  "integrity": { "canonical_sha256": "hash excluding this field" }
}
```

Contract rules:

1. **Native resume first.** If a provider's own supported resume command or UI
   works, use it. If the provider source cannot be resumed, construct a new
   session from the pack and mark the outcome `reconstructed`; do not describe
   it as a provider-native resume or complete replay.
2. **Provenance per assertion.** The objective, current step, constraints,
   decisions, failed approaches, next action, recency summaries, and verified
   state each carry one or more source refs.
   Prefer stable provider message/turn/event IDs. If none exist, record an
   explicit coarse selector (time window or line range) and mark its quality;
   never fabricate exactness. A full-file hash is optional for very large
   sources; the bounded selected range may be hashed instead.
3. **Point-in-time state.** Repository, worktree, and artifact facts include an
   observation time and evidence source. Old state becomes stale; a saved branch
   or worktree label alone is not proof that it still exists.
4. **Local-only content.** Keep native IDs, local paths, event ranges, source
   hashes, summaries, and pack contents on the user's machine by default. Do
   not send raw transcripts, prompts, tool outputs, source line excerpts,
   authentication values, or absolute local paths into Meridian notes, tasks,
   handoff bodies, recovery metadata, or artifact services.
5. **Bounded extraction.** Cataloging begins with paths, sizes, times, and
   provider IDs available from supported indexes or filenames. Parse only the
   selected session and bounded ranges, with byte/line limits and a pinned
   adapter version. Unknown schemas, invalid encodings, partial files, and
   compressed formats produce explicit `unavailable` or `partial` coverage;
   they are not silently skipped or rewritten.
6. **Source preservation.** The adapter is read-only against provider history.
   It may create a separate local pack and integrity manifest, but it does not
   move, rename, truncate, or delete provider files. Archive export remains a
   separate user-initiated operation.
7. **Hosted correlation only.** A hosted Meridian record may hold its existing
   opaque `local_ref_id`, provider/client type, transport, verified-resumable
   boolean, sprint version, and approved checkpoint/handoff references. Never
   encode the provider session ID, local path, environment/bridge ID, argv, or
   conversation content in free-form hosted metadata.

## Recovery flow and follow-on work

The recovery caller should attempt, in order:

1. Resolve the opaque local ref and check that the recorded provider, client,
   native identifier, configured root, and adapter version still agree.
2. Ask the provider-native CLI/UI to resume and record the result locally.
3. If native resume is unavailable, load the local context pack and start a
   new session with its compact task state and source pointers.
4. Independently recover Meridian's current board, sprint version, and active
   claims. Never replay an old `/goal` as authoritative board state.
5. Offer account exports only as a user-driven archive path with the provider's
   own access and retention rules.

Implementation should be coordinated with the pending local session catalog and
context-pack build item `71fb64fe`, the external chat/artifact pointer item
`2197fb73`, the local artifact capture item `58491f23`, and the pre-specified
recovery evaluation item `83762f4a`. These are separate implementation and
evaluation scopes; this report does not claim them complete. No distinct Deep
Research run was launched or relied on for these findings.

## Evidence references

- Anthropic: [Claude Code CLI session commands](https://docs.anthropic.com/en/docs/claude-code/cli-usage); [Agent SDK session storage, enumeration, and resume](https://code.claude.com/docs/en/agent-sdk/sessions); [Claude account data export](https://support.anthropic.com/en/articles/9450526-how-can-i-export-my-claude-data).
- OpenAI: [ChatGPT archive/delete and retention](https://help.openai.com/en/articles/8809935-deleting-and-archiving-chats-in-chatgpt); [ChatGPT data export](https://help.openai.com/en/articles/7260999-exporting-your-chatgpt-history-and-data); [Codex `resume` command source](https://github.com/openai/codex/blob/main/codex-rs/exec/src/cli.rs); [Codex rollout stores](https://github.com/openai/codex/blob/main/codex-rs/rollout/src/lib.rs); [Codex rollout lookup](https://github.com/openai/codex/blob/main/codex-rs/rollout/src/list.rs); [Codex app-server thread resume](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server).
- Meridian: [handoff receiver runbook](meridian-handoff-contract.md); [per-mode handoff persistence](meridian-handoff-mode-contract-2026-08-26.md); [local identity boundary](../meridian/session_recovery.py); [registry and live-board continuation](../meridian/db/session_recovery.py); [typed evidence pointers](../meridian/pointers.py).

## Local CLI catalog and context packs

The first local adapter is exposed as `meridian recovery catalog` and
`meridian recovery pack`:

```powershell
meridian recovery catalog --limit 200
meridian recovery pack --provider claude_code --session-id <uuid> `
  --project-id <project-id> --sprint-version <version> --context-file .\recovery-context.json `
  --repo-root .
```

The catalog enumerates Claude Code project JSONL filenames and Codex CLI
rollout filenames under `sessions` and `archived_sessions`. It reports the
provider-native resume argv as a **candidate**, and marks it unverified until
the provider CLI is actually invoked by the user. Cataloging reads filesystem
metadata only; it does not read transcript content or Codex's session index.
Traversal and result counts are bounded. `not_cataloged_surfaces` explicitly
directs Claude Agent SDK and Codex app sessions to their host APIs, and web
account chats to the provider UI or a user-requested export.

The `pack` command builds a compact, integrity-hashed local JSON file under
Meridian's app-state directory. Its required context file contains a `task`
object with short `objective` and `next_action` summaries; optional fields may
include `current_step`, `recent_transcript`, `constraints`, `decisions`,
`failed_approaches`, `recency_tail`, `command_error_ledger`, `source_refs`,
`meridian_recovery`, and `artifact_refs`. Compact summaries can reference
`provider-range-0` so the note remains linked to the selected local range.
When a range is selected, the adapter hashes it after checking that the source
file still matches the cataloged identity and time. Line contents are never
copied into the pack; the caller supplies a short, secret-checked summary.
The pack reports whether live board state, a transcript summary, and a verified
repository snapshot were supplied, and whether reconstruction is complete. It
never claims byte restoration, provider-native resume, or successful
restoration without the corresponding evidence. These commands do not upload
the pack or provider locators to Meridian.
