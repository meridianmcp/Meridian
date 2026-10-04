# Local agent artifact capture discovery — 2026-10-02

## Summary

The repository already has separate primitives for content-addressed bytes, output-file identity, session recovery metadata, and MCP tool-call event metadata. The missing link is an opted-in local capture bridge that observes a provider event, resolves a user-approved output file, verifies it, stores its bytes, and records which project/subproject/run and source event it belongs to.

Correction to the sprint item's initial premise: store_artifact is already called by LocalObjectStoreBackend.put and by the oversized experiment-receipt spill path. Those callers cover object-store writes and experiment receipts. The graph contains no caller that turns Claude Code or Codex file/tool lifecycle events into captured agent output files. This discovery is read-only; it made no Tigris API calls and uploaded nothing.

## Event and file-source inventory

| Surface | Available event/evidence | What it can establish | Current Meridian workstation wiring |
|---|---|---|---|
| Claude Code local hooks | Official events include PostToolUse and PostToolUseFailure, SubagentStart/Stop, TaskCreated/Completed, Stop, SessionStart/End, Pre/PostCompact, WorktreeCreate/Remove, and FileChanged. PostToolUse supplies tool input and tool response after a successful tool call. FileChanged supplies an absolute path and change/add/unlink event for watched paths. | Tool events can identify a likely write operation; FileChanged can notice a selected file changed even when a shell process or outside process wrote it. Neither event proves the resulting bytes are a valid artifact. Read the path locally and verify it. | The repo's .claude/settings.json wires PreToolUse, PostToolUse, Stop, SessionStart, and SubagentStart. The configured scripts are guard/context/checkpoint paths; no file-capture hook or FileChanged, SubagentStop, or SessionEnd registration is present. Searches of these hook/config paths found no store_artifact call. |
| Codex CLI local hooks | Current official docs describe SessionStart/End, PreToolUse, PermissionRequest, PostToolUse, Pre/PostCompact, UserPromptSubmit, SubagentStart/Stop, Stop, and Interrupt. PostToolUse can observe supported Bash, apply_patch, MCP, and local function calls and receives tool name, call ID, input, and model-facing response. | A PostToolUse adapter can recognize a tool call and use its arguments/response as an untrusted hint to a permitted output path. It does not itself verify bytes. The documented event set has no FileChanged event, so external process writes need another opted-in local watcher or explicit artifact registration. | No project-local .codex config exists in this checkout. The observed user-level Codex config has only a SessionStart hook that injects the codebase-search preference; no artifact-capture hook is configured in that profile. This Codex task connection also exposes no lifecycle-event stream. Verify support and configuration on the actual local CLI/app host before relying on an event. |
| Meridian MCP tool-call log | The live MCP dispatch success path records tool.completed with tool name, success, duration, and error type. capture_tool_invoked exists but is explicitly not wired. | Auditable MCP activity metadata only; it does not include tool arguments, tool responses, or generated file bytes. | This event path is server-side and is not a provider filesystem watcher. It should not be expanded to upload raw tool output as a shortcut to artifact capture. |
| Meridian Outputs registry | tag_output fingerprints a local output file and its generator script. register_artifact can attach kind, expected/content hash, generator, run_id, source locator, role, lifecycle state, and redacted local path sightings. verify_artifact_hash re-hashes an on-disk file. | Stable identity and integrity checks for a file that already exists. This registry does not store the file's bytes. | Useful provenance companion to byte storage; not an agent event listener and not a substitute for store_artifact. |
| Session recovery snapshot | write_local_recovery_snapshot atomically stores local session/bridge/environment identity mappings, transcript path and resume-recipe data. | A local mapping from hosted opaque reference to local recovery identity. | Not an artifact byte store. Keep it separate from output capture and do not copy its local-only identities into hosted artifact metadata. |

Sources: [Claude Code hooks reference](https://code.claude.com/docs/en/hooks) and [Codex hooks documentation](https://developers.openai.com/codex/hooks/), consulted 2026-10-02. Provider support varies by host mode and version. In particular, do not assume that a local hook runs for a cloud-orchestrated session.

## Existing storage boundaries

### Content-addressed bytes

meridian/artifact_store.py stores bytes under a project-scoped content hash. Text-decodable UTF-8 bytes pass through the secret redactor before hashing and storage; binary/non-UTF-8 bytes are left unchanged. Identical post-redaction content deduplicates within the project. Its metadata covers content hash, project id, size, content type, created time, and whether redaction changed the bytes. It has no provider event id, subproject, run id, source path, or user-consent record.

That means capture provenance must live in a separate local manifest keyed to the returned content hash. Record each occurrence even when the bytes deduplicate: two different runs may produce identical content but have different sources and retention needs. The first stored artifact's metadata is unchanged on a deduplicated write, so do not treat it as an occurrence ledger.

The store has project-scoped list, read, explicit delete, receipted export, and cutoff-based purge operations. Export is read-only; purge is the bulk-deletion path. Retention and deletion policy should be explicit in the local capture manifest and user controls.

### File identity and provenance

extensions/meridian-outputs/meridian_outputs/artifact_registry.py records local file identity and provenance, including generator, run id, source locator, role, expected/content hash, lifecycle state, and redacted local paths. Its verifier fails closed when a registered hash or readable source file is missing. fingerprint.tag_output additionally stamps the generating script's hash. These functions observe an existing file and its generator metadata; they do not copy its bytes into artifact_store.

### Other uses of the byte store

meridian/object_store.py's LocalObjectStoreBackend.put validates a hash-shaped key and delegates the bytes to artifact_store. meridian/tigris_adapter.py can spill oversized experiment result receipts to Tigris when that backend is constructed, with local artifact_store fallback. The current spill caller is meridian.db.experiments.complete_experiment_run, not a provider output event. These are valid store_artifact production call paths, but neither is the agent-file capture bridge this item is meant to design.

## Safe capture path

Use a local adapter/bridge, enabled per selected project root. Keep hosted Meridian as the coordination layer; keep bytes, absolute paths, provider session ids, transcript paths, and raw hook payloads local by default.

1. **Opt in and scope.** The user selects project/subproject roots and output directories or file patterns. Capture is off outside those roots. Use configured canonical root identity and current worktree/Git metadata where present; do not infer that every chat belongs to a repository.
2. **Observe metadata, not arbitrary content.** For a provider with local PostToolUse, parse the event as untrusted input and use an allowlisted tool name plus candidate path as a trigger. For Claude FileChanged, accept only watched paths within the selected output roots. Use SubagentStop, TaskCompleted, Stop, or SessionEnd as bounded flush/reconciliation points, not as permission to archive an entire transcript.
3. **Resolve and validate locally.** Canonicalize the candidate path, prove it remains inside the selected root after symlink resolution, reject missing/unreadable files and disallowed extensions/types, and enforce byte limits. Exclude secret-named files, credential/config files, VCS metadata, provider histories, caches, dependency trees, and ignored paths unless the user explicitly selected a supported output.
4. **Store only approved bytes.** Read the current file locally after the write is complete, compute its source hash and size, and call the project-scoped byte store. Text redaction is best effort; binary files are unchanged by that redactor, so only an explicit type/extension allowlist or a separate content scanner can authorize binary capture. Never use a tool response or transcript as a substitute for reading and hashing the actual output file.
5. **Write a local occurrence manifest.** Record provider/surface/version, local event and tool-use ids, opaque local session mapping, selected project/subproject, optional run id, relative source path, source mtime/size/hash, stored content hash, content type, redaction result, capture time, consent/trigger, and retention state. Keep provider ids and absolute local paths only in the local manifest. Hosted state may receive only a minimal approved reference or opaque id.
6. **Verify and register.** Read back the stored bytes and confirm their content hash before marking capture successful. When an output is part of a research/run workflow, also register or update its Meridian Outputs artifact identity and verify the current source file hash. Keep the byte-store hash and the output registry's identity/hash as linked but distinct records.
7. **Deduplicate without losing lineage.** Let artifact_store deduplicate byte-identical files. Append a separate occurrence record for each event/run so the user can tell where a stored blob came from and which project/run may delete it.
8. **Retain and delete deliberately.** Expose retention status, project-scoped export and purge, and per-occurrence unlink/deletion behavior. Do not automatically mirror captures to Tigris or another hosted object store; that requires a separate explicit opt-in and privacy review.

If no provider hook is available, report capture capability as unavailable or manual-only. A missing hook event is not evidence that no output was created. A user can explicitly register a stable output with the existing Outputs tools, or a later local watcher can be added for a selected directory. Do not silently fall back to scanning the whole workspace.

## Downstream acceptance cases

The implementation item should cover, at minimum: capture disabled by default; selected-root allow and outside-root rejection including symlinks; file still being written or disappearing; secret-shaped UTF-8 redaction; binary allowlist and unchanged bytes; same hash in two runs with separate occurrence lineage; output-registry hash mismatch; duplicate event replay; provider hook unavailable; no repository/worktree; explicit per-project deletion/export/purge; and proof that no remote upload occurs without opt-in.

This report defines the capture boundary only. It does not implement hooks, write artifacts, start a filesystem watcher, or call Tigris.
