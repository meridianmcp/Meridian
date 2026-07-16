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
    result = subprocess.run(
        ["bash", "-c",
         'command -v jq >/dev/null 2>&1 && case "$(uname -s 2>/dev/null)" in Linux|Darwin) echo yes ;; esac'],
        capture_output=True, text=True, timeout=10,
    )
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
