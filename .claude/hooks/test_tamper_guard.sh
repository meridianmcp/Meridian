#!/usr/bin/env bash
# 43539c70 - PostToolUse test-tamper guard (structural). Cross-platform partner of
# test_tamper_guard.ps1 (the .ps1 runs on the maintainer's Windows box; this .sh
# covers Linux/macOS executors + is what the regression test exercises).
#
# Named failure mode the other hooks DON'T catch: an agent makes a FAILING test
# pass by editing the test (loosening an assertion / deleting a case) instead of
# fixing the code under test. This is distinct from and complementary to:
#   - the Stop hook  (sprint_guard.*, early-stop while items pending)
#   - the PreToolUse protected-files / HITL guards (hitl_guard.*, hooks.* installer)
# NONE of those look at WHAT changed in a test file.
#
# Contract (Claude Code PostToolUse): JSON on stdin with {tool_name, tool_input}.
# tool_input.file_path is the edited path. We FLAG (warn to stderr) whenever a test
# file is edited, so the human/agent is reminded to confirm the change fixes the
# CODE, not the assertion. Default posture is NON-BLOCKING: exit 0 always, UNLESS
# MERIDIAN_TEST_TAMPER_BLOCK=1 is set, in which case a flagged edit exits 2 (block).
#
# Exemption: legitimate feature work adds/updates tests. If the current in-progress
# sprint item's own text explicitly calls for test/coverage work, we stay SILENT.
# That signal is fetched best-effort from Meridian; any failure to reach it just
# means we fall back to flagging (a warn is harmless). Fails OPEN on every parse /
# network error - it must never trap the executor.
#
# This is NOT hooks.sh (the token-rotation installer).
set -uo pipefail

PROJECT_ID="5787cc92-ba7d-4788-b17c-28ab7938b839"
MERIDIAN_URL="${MERIDIAN_URL:-http://localhost:7878}"

payload="$(cat 2>/dev/null || true)"
[ -z "$payload" ] && exit 0

# Only act on file-writing tools; anything else is irrelevant.
tool="$(printf '%s' "$payload" | grep -oE '"tool_name"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')"
case "$tool" in
  Edit|Write|MultiEdit|NotebookEdit) : ;;
  *) exit 0 ;;
esac

# Extract the edited path from tool_input.file_path (fail open if absent).
path="$(printf '%s' "$payload" | grep -oE '"file_path"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')"
[ -z "$path" ] && exit 0

# Normalise separators so the match works whether the path is posix or windows.
norm="$(printf '%s' "$path" | tr '\\' '/')"
base="${norm##*/}"

# Is this a test file? Matches: test_*.py, *_test.py, *.test.ts/.tsx/.js/.jsx,
# *.spec.ts/.js, or any path segment that is a `tests`/`__tests__` dir.
is_test=0
case "$base" in
  test_*.py|*_test.py|*.test.ts|*.test.tsx|*.test.js|*.test.jsx|*.spec.ts|*.spec.tsx|*.spec.js|*.spec.jsx) is_test=1 ;;
esac
case "$norm" in
  */tests/*|tests/*|*/__tests__/*|__tests__/*) is_test=1 ;;
esac
[ "$is_test" -eq 0 ] && exit 0

# Exemption: if the in-progress sprint item explicitly calls for test/coverage work,
# stay silent. Best-effort; any failure leaves exempt=0 (we still flag - safe).
exempt=0
url="$MERIDIAN_URL/projects/$PROJECT_ID/sprint/test_coverage_expected"
resp="$(curl -sf --max-time 3 "$url" 2>/dev/null || true)"
if printf '%s' "$resp" | grep -Eq '"test_coverage_expected"[[:space:]]*:[[:space:]]*true'; then
  exempt=1
fi
[ "$exempt" -eq 1 ] && exit 0

msg="Meridian test-tamper guard (43539c70): '$base' is a TEST file. If this edit \
makes a failing test pass by changing the assertion/expectation rather than fixing \
the code under test, that is the test-tampering anti-pattern - confirm the change \
fixes the CODE, not the test. (Legitimate new/updated coverage for a sprint item \
that calls for it is fine; set MERIDIAN_TEST_TAMPER_BLOCK=1 to make this a hard block.)"

echo "$msg" >&2

# Default: non-blocking flag (exit 0). Opt-in hard block via env var.
if [ "${MERIDIAN_TEST_TAMPER_BLOCK:-}" = "1" ]; then
  exit 2
fi
exit 0
