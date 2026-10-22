#!/usr/bin/env bash
# meridian_guard_post.sh -- Meridian guard, Claude Code PostToolUse hook (sprint item 55d48d69).
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
# The engine is meridian_guard.sh --post (meridian_guard.awk). Never blocks: PostToolUse
# output is additionalContext only, the exit code is always 0, any error => no output.
# This is NOT hooks.sh (the token-rotation installer).

# Under a non-bash sh, re-run under bash first (see meridian_guard.sh: dash would
# exit 2 on the bash-only syntax below, and exit 2 means "block").
if [ -z "${BASH_VERSION:-}" ]; then
    command -v bash >/dev/null 2>&1 || exit 0
    exec bash "$0" "$@"
    exit 0
fi
_mg_post_self=${BASH_SOURCE[0]:-$0}
case $_mg_post_self in
    */* | *\\*) _mg_post_dir=${_mg_post_self%[/\\]*} ;;
    *) _mg_post_dir=. ;;
esac
[ -f "$_mg_post_dir/meridian_guard.sh" ] || exit 0
. "$_mg_post_dir/meridian_guard.sh" --post
exit 0
