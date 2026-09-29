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
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HOOK_SH = _REPO / ".claude" / "hooks" / "hitl_guard.sh"
_SETTINGS = _REPO / ".claude" / "settings.json"


def _hitl_bash() -> str | None:
    """Git Bash, preferred over a bare ``bash`` PATH lookup.

    On Windows, ``C:\\Windows\\System32\\bash.exe`` (the WSL launcher) can shadow
    Git's own bash.exe on PATH, and even when a bare ``bash`` lookup resolves to
    Git's bash.EXE by path string, the WSL launcher can still be what actually runs
    -- WSL's interop layer does not reliably forward a custom ``env=`` dict passed to
    ``subprocess.run`` (confirmed: an explicit real Git bash.exe path receives it,
    a bare ``"bash"`` does not, on the same host). Since the tests below need to hand
    the hook a `MERIDIAN_URL` pointing at a local stub server, this needs to be the
    real Git bash -- same fix as test_sprint_guard.py's ``_sg_bash()``."""
    for c in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
        if os.name == "nt" and Path(c).exists():
            return c
    return shutil.which("bash")


_HITL_BASH = _hitl_bash()

_needs_bash = pytest.mark.skipif(
    not _HOOK_SH.exists() or _HITL_BASH is None,
    reason="hitl_guard.sh or bash unavailable",
)


def _run_hook(payload: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    # Invoke via a RELATIVE path with cwd=repo root. An absolute Windows path (str or
    # even C:/ posix form) breaks git-bash's usr/bin/bash (it wants /c/... MSYS form),
    # and `bash -c <script>` doesn't reliably deliver stdin to `cat` on git-bash. A
    # relative path from the repo root works identically on Linux CI and Windows.
    return subprocess.run(
        [_HITL_BASH, ".claude/hooks/hitl_guard.sh"],
        input=payload, cwd=str(_REPO), capture_output=True, text=True, timeout=15,
        env=env,
    )


class _HealthOk(BaseHTTPRequestHandler):
    """Answers every GET (including /health) with 200 -- a reachable Meridian."""

    def log_message(self, *_a: object) -> None:  # noqa: D401 -- silence request logging
        pass

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.end_headers()


@pytest.fixture
def meridian_reachable():
    """51f5120f (55d48d69 fix round 2): hitl_guard's enforce-mode block is
    conditional on a reachable Meridian /health -- an unreachable server now
    fails OPEN instead of trapping the session with no way to ask the human
    (see tests/test_legacy_hooks_fix_round1.py's
    test_hitl_guard_fails_open_when_meridian_unreachable /
    test_hitl_guard_names_the_plain_text_fallback, the pinned tests for this
    contract). Any test that wants to see the actual block must give the hook
    a reachable stub, the same way those tests do."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _HealthOk)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield {**os.environ, "MERIDIAN_URL": f"http://127.0.0.1:{srv.server_address[1]}"}
    finally:
        srv.shutdown()
        srv.server_close()


@_needs_bash
def test_hook_blocks_native_askuserquestion(meridian_reachable):
    r = _run_hook('{"tool_name":"AskUserQuestion","tool_input":{}}', env=meridian_reachable)
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
