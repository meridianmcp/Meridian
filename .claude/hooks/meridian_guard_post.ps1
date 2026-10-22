# meridian_guard_post.ps1 -- Meridian guard, Claude Code PostToolUse hook (sprint item 55d48d69).
#
#   G12 web capture reminder  WebSearch/WebFetch with nothing captured in 15 min -> additionalContext
#                             (at most once per 15 minutes)
#   G13 receipts              code-intel / Meridian research / capture calls are recorded (ok or
#                             error) in the per-session state file; 2 code-intel errors within
#                             10 minutes mark code-intel degraded for 20 minutes (G1/G3/G4 then allow)
#   G14 directive quarantine  start_session / load_handoff / get_sprint_items / ... output that
#                             carries execution_policy, no_confirmation, execute_immediately or
#                             OVERRIDE, or is over 60K chars -> additionalContext naming it as data
#
# The engine is meridian_guard.ps1 (-Mode post); this wrapper only selects the mode so the
# PreToolUse and PostToolUse registrations stay separate files. Never blocks: PostToolUse
# output is additionalContext only, exit code is always 0, any error => no output.
# Must stay pure ASCII. This is NOT hooks.ps1 (the token-rotation installer).
trap { exit 0 }
try {
    # Path.Combine, not Join-Path: no module auto-load on the hot path
    & ([System.IO.Path]::Combine($PSScriptRoot, 'meridian_guard.ps1')) -Mode post
} catch { }
exit 0
