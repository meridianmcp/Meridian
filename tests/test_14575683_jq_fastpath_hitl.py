"""14575683 — jq fast-path tests for hitl_guard.sh.

The sprint item added an optional jq-based JSON extraction path to hitl_guard.sh
(Linux/macOS only, falls back to the pre-existing regex chain when jq is absent
or on Windows/Git-Bash).  These tests:

1. Verify the hook source contains every structural element of the fast path
   (environment-independent static check — always runs, never skips).

2. Use a "decoy" payload that distinguishes jq's structural extraction from the
   naive first-match regex, proving the jq path is genuinely exercised on
   platforms where jq is available.

3. Confirm a jq-specific invalid-JSON variant still fails open (exit 0).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HOOK_SH = _REPO / ".claude" / "hooks" / "hitl_guard.sh"

# ---------------------------------------------------------------------------
# Helpers shared with the rest of the hook test suite
# ---------------------------------------------------------------------------

def _run_hook(payload: str) -> subprocess.CompletedProcess:
    """Run hitl_guard.sh with *payload* on stdin, mirroring the established pattern."""
    return subprocess.run(
        ["bash", ".claude/hooks/hitl_guard.sh"],
        input=payload,
        cwd=str(_REPO),
        capture_output=True,
        text=True,
        timeout=15,
    )


def _jq_available_on_linux_darwin() -> bool:
    """Return True iff the bash environment that runs the hook tests has jq AND
    uname reports Linux or Darwin — i.e. the same condition the hook itself
    checks for its fast path."""
    if shutil.which("bash") is None:
        return False
    try:
        result = subprocess.run(
            ["bash", "-c",
             'command -v jq >/dev/null 2>&1 && case "$(uname -s 2>/dev/null)" in Linux|Darwin) echo yes ;; esac'],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        # This probe runs unconditionally at COLLECTION time (it decides a
        # module-level skipif). A slow/contended host can make even a
        # trivial subprocess spawn exceed the timeout or fail with a
        # resource error (observed: WinError 1455, paging file too small,
        # under heavy concurrent load) -- letting either propagate turns
        # into an uncaught collection ERROR that masks every test in this
        # module, which is worse than the skip this probe exists to decide.
        # Fail closed toward "assume unavailable" instead.
        return False
    return result.stdout.strip() == "yes"


_needs_bash = pytest.mark.skipif(
    not _HOOK_SH.exists() or shutil.which("bash") is None,
    reason="hitl_guard.sh or bash unavailable",
)

_needs_jq_linux_darwin = pytest.mark.skipif(
    not _jq_available_on_linux_darwin(),
    reason="jq not available or platform not Linux/Darwin — jq fast path not active",
)

# ---------------------------------------------------------------------------
# 1. Static source-text guard — always runs, never skips
# ---------------------------------------------------------------------------

def test_hook_source_contains_jq_fastpath_elements():
    """The hook file must contain all structural elements of the jq fast path
    AND keep the original regex fallback intact."""
    assert _HOOK_SH.exists(), "hitl_guard.sh must exist"
    src = _HOOK_SH.read_text(encoding="utf-8")

    # Fast-path gating logic
    assert "command -v jq" in src, "must probe for jq availability"
    assert "uname -s" in src, "must probe uname -s for platform detection"
    assert "Linux" in src and "Darwin" in src, "must gate on Linux|Darwin"

    # The actual jq extraction expression
    assert ".tool_name // empty" in src, "must use .tool_name // empty jq filter"

    # Original regex fallback must be present (never removed by the additive change)
    assert re.search(
        r'grep -oE\s+[\'"]"tool_name"',
        src,
    ), "original regex grep fallback must still be present in the hook source"

    assert "sed -E" in src, "original sed extraction must still be present"


# ---------------------------------------------------------------------------
# 2. Decoy payload tests — structural proof the jq path runs correctly
# ---------------------------------------------------------------------------

@_needs_bash
@_needs_jq_linux_darwin
def test_jq_fastpath_reads_toplevel_not_nested_decoy_allows():
    """Decoy nested tool_name="AskUserQuestion" before real top-level tool_name="Bash".

    - jq reads the TOP-LEVEL .tool_name -> "Bash" -> must NOT block (exit 0).
    - A naive first-match regex would grab the nested "AskUserQuestion" first
      and incorrectly block (exit 2).  exit 0 here proves jq is in effect.
    """
    # Construct the payload so the nested "tool_name" key is serialized FIRST
    # (Python's json.dumps preserves insertion order in 3.7+).
    payload = json.dumps({
        "tool_input": {
            "decoy": {"tool_name": "AskUserQuestion"}
        },
        "tool_name": "Bash",
    })
    # Sanity-check: the nested decoy really does appear before the top-level key
    # in the raw string, so regex would get it wrong.
    assert payload.index('"AskUserQuestion"') < payload.index('"Bash"'), (
        "payload construction error: decoy must appear before real tool_name in raw string"
    )
    r = _run_hook(payload)
    assert r.returncode == 0, (
        "jq fast path must read top-level .tool_name='Bash' and allow the call; "
        "a non-zero exit means the hook incorrectly grabbed the nested decoy "
        "'AskUserQuestion' (regex first-match bug) instead of using jq structurally"
    )


@_needs_bash
@_needs_jq_linux_darwin
def test_jq_fastpath_reads_toplevel_not_nested_decoy_blocks():
    """Mirror-image decoy: nested tool_name="Bash" before real top-level tool_name="AskUserQuestion".

    - jq reads the TOP-LEVEL .tool_name -> "AskUserQuestion" -> must BLOCK (exit 2).
    - A naive first-match regex would grab the nested "Bash" and incorrectly allow.
    """
    payload = json.dumps({
        "tool_input": {
            "decoy": {"tool_name": "Bash"}
        },
        "tool_name": "AskUserQuestion",
    })
    # Sanity-check: nested "Bash" appears before real "AskUserQuestion" in raw string
    assert payload.index('"Bash"') < payload.index('"AskUserQuestion"'), (
        "payload construction error: decoy must appear before real tool_name in raw string"
    )
    r = _run_hook(payload)
    assert r.returncode == 2, (
        "jq fast path must read top-level .tool_name='AskUserQuestion' and block (exit 2); "
        "exit 0 would mean the hook incorrectly grabbed the nested 'Bash' decoy "
        "instead of using jq's structural extraction"
    )
    assert "request_hitl" in r.stderr, "blocking message must redirect to request_hitl"


# ---------------------------------------------------------------------------
# 3. jq-specific fail-open test — invalid JSON with decoy-shaped content
# ---------------------------------------------------------------------------

@_needs_bash
@_needs_jq_linux_darwin
def test_jq_fastpath_fails_open_on_invalid_json_with_decoy_shape():
    """A string that is not valid JSON but would look like it might contain tool_name.

    jq will error on this payload; the hook must fall through to the regex chain.
    The payload does NOT contain a syntactically-extractable "tool_name": "..."
    match (no quotes around a valid name), so the regex also produces an empty
    tool string, and the hook exits 0 (fail-open).

    This confirms: jq errors are silently swallowed (|| true), and the fallback
    regex path handles the unparseable-but-jq-specific case gracefully.
    """
    # Invalid JSON: curly braces and "tool_name" keyword present, but no valid
    # "tool_name": "<value>" pattern the regex can extract (the value is not quoted).
    # jq fails to parse it; the regex grep finds no match; hook exits 0.
    payload = '{ GARBAGE "tool_name": UNQUOTED_VALUE {{ not valid json at all'
    r = _run_hook(payload)
    assert r.returncode == 0, (
        "hook must fail open (exit 0) on malformed JSON even when jq is present; "
        "jq errors must be silently swallowed and the hook must fall through to regex, "
        "which also finds no extractable tool_name, resulting in exit 0"
    )


# ---------------------------------------------------------------------------
# 883ce543 -- PowerShell path parity: .claude/settings.json wires
# hitl_guard.ps1 for the real Claude Code client on Windows, while every test
# above only ever exercises hitl_guard.sh. These tests run the ACTUAL .ps1
# hook as a subprocess (never in-process -- `exit` inside a dot-sourced/
# `&`-invoked script would kill the CURRENT PowerShell host, not just return),
# proving hitl_guard.sh and hitl_guard.ps1 already share one fail-open/block
# decision table: block ONLY AskUserQuestion (exit 2, redirect to
# request_hitl), allow every other tool, fail open on any parse error.
# Unlike code_intel_guard, hitl_guard has no readiness probe to get wrong --
# this is a parity/coverage gap fix, not a behavior change.
# ---------------------------------------------------------------------------

_HOOK_PS1 = _REPO / ".claude" / "hooks" / "hitl_guard.ps1"


def _powershell_exe() -> str | None:
    for exe in ("pwsh", "powershell"):
        found = shutil.which(exe)
        if found:
            return found
    return None


_needs_powershell = pytest.mark.skipif(
    _powershell_exe() is None or not _HOOK_PS1.exists(),
    reason="no PowerShell interpreter (pwsh/powershell) available, or hitl_guard.ps1 missing",
)


def _run_ps1_hook(payload: str) -> subprocess.CompletedProcess:
    """Run hitl_guard.ps1 with *payload* on stdin, mirroring _run_hook above."""
    ps = _powershell_exe()
    return subprocess.run(
        [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(_HOOK_PS1)],
        input=payload,
        cwd=str(_REPO),
        capture_output=True,
        text=True,
        timeout=15,
    )


@_needs_powershell
def test_ps1_hook_blocks_native_askuserquestion():
    """Same contract as the .sh hook's test_hook_blocks_native_askuserquestion."""
    r = _run_ps1_hook('{"tool_name":"AskUserQuestion","tool_input":{}}')
    assert r.returncode == 2, "exit 2 blocks the tool call"
    assert "request_hitl" in r.stdout + r.stderr, "must redirect to request_hitl"


@_needs_powershell
@pytest.mark.parametrize("tool", ["Bash", "Edit", "Write", "Read", "request_hitl", "Grep"])
def test_ps1_hook_allows_every_other_tool(tool):
    r = _run_ps1_hook(json.dumps({"tool_name": tool, "tool_input": {}}))
    assert r.returncode == 0, f"{tool} must not be blocked"


@_needs_powershell
@pytest.mark.parametrize("payload", ["", "not json at all", "{}", '{"foo":"bar"}'])
def test_ps1_hook_fails_open_on_unparseable(payload):
    assert _run_ps1_hook(payload).returncode == 0, "must fail open, never trap the executor"


def test_ps1_hook_is_pure_ascii_and_parses_with_zero_errors():
    """883ce543 gotcha: BOM-less UTF-8 .ps1 files are read as cp1252 by
    PowerShell 5.1, so a stray non-ASCII byte can silently corrupt the parse.
    Verify the shipped hitl_guard.ps1 is pure ASCII and parses cleanly."""
    raw = _HOOK_PS1.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "hitl_guard.ps1 must not have a UTF-8 BOM"
    non_ascii = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
    assert not non_ascii, f"hitl_guard.ps1 has non-ASCII bytes at {non_ascii[:10]}"

    ps = _powershell_exe()
    if ps is None:
        pytest.skip("no PowerShell interpreter available on this host")
    ps_script = (
        "$tokens=$null;$errors=$null;"
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{_HOOK_PS1.as_posix()}',[ref]$tokens,[ref]$errors)|Out-Null;"
        "if($errors){$errors|ForEach-Object{Write-Output $_.Message};exit 1}"
        "else{Write-Output 'PARSE_OK';exit 0}"
    )
    proc = subprocess.run(
        [ps, "-NoProfile", "-NonInteractive", "-Command", ps_script],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert proc.returncode == 0, f"hitl_guard.ps1 failed to parse:\n{proc.stdout}\n{proc.stderr}"
    assert "PARSE_OK" in proc.stdout
