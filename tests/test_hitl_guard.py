"""b8fbb4cb — the PreToolUse HITL guard structurally blocks the native ask-UI.

The executor kept using Claude Code's native AskUserQuestion instead of request_hitl,
so human-in-the-loop questions never reached Meridian's hitl_requests table (confirmed
absent 3x). Prior "fixes" (36edd005, d261ea2e) only asserted agent_instructions TEXT and
did not hold under a marathon. This tests the ACTUAL hook BEHAVIOR — it blocks
AskUserQuestion (exit 2), allows every other tool, fails open on garbage, and
settings.json genuinely wires it — the same structural-enforcement pattern as the
file-claim guard.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HOOK_SH = _REPO / ".claude" / "hooks" / "hitl_guard.sh"
_SETTINGS = _REPO / ".claude" / "settings.json"

_needs_bash = pytest.mark.skipif(
    not _HOOK_SH.exists() or shutil.which("bash") is None,
    reason="hitl_guard.sh or bash unavailable",
)


def _run_hook(payload: str) -> subprocess.CompletedProcess:
    # Invoke via a RELATIVE path with cwd=repo root. An absolute Windows path (str or
    # even C:/ posix form) breaks git-bash's usr/bin/bash (it wants /c/... MSYS form),
    # and `bash -c <script>` doesn't reliably deliver stdin to `cat` on git-bash. A
    # relative path from the repo root works identically on Linux CI and Windows.
    return subprocess.run(
        ["bash", ".claude/hooks/hitl_guard.sh"],
        input=payload, cwd=str(_REPO), capture_output=True, text=True, timeout=15,
    )


@_needs_bash
def test_hook_blocks_native_askuserquestion():
    r = _run_hook('{"tool_name":"AskUserQuestion","tool_input":{}}')
    assert r.returncode == 2, "exit 2 blocks the tool call"
    assert "request_hitl" in r.stderr, "must redirect to request_hitl"


@_needs_bash
@pytest.mark.parametrize("tool", ["Bash", "Edit", "Write", "Read", "request_hitl", "Grep"])
def test_hook_allows_every_other_tool(tool):
    r = _run_hook(json.dumps({"tool_name": tool, "tool_input": {}}))
    assert r.returncode == 0, f"{tool} must not be blocked"


@_needs_bash
@pytest.mark.parametrize("payload", ["", "not json at all", "{}", '{"foo":"bar"}'])
def test_hook_fails_open_on_unparseable(payload):
    assert _run_hook(payload).returncode == 0, "must fail open, never trap the executor"


def test_settings_actually_wires_the_guard():
    """Text guidance failed 3x — verify the hook is really wired, not just present."""
    cfg = json.loads(_SETTINGS.read_text(encoding="utf-8"))
    pre = cfg.get("hooks", {}).get("PreToolUse", [])
    entry = next((e for e in pre if e.get("matcher") == "AskUserQuestion"), None)
    assert entry is not None, "PreToolUse must guard AskUserQuestion"
    cmds = " ".join(h.get("command", "") for h in entry.get("hooks", []))
    assert "hitl_guard" in cmds, "the AskUserQuestion matcher must run hitl_guard"
