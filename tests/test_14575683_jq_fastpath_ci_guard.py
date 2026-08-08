"""14575683 -- jq fast-path correctness tests for code_intel_guard.sh.

Sprint item 14575683 added an additive jq-based JSON extraction fast path to
code_intel_guard.sh and hitl_guard.sh (commit 090ca5a / cherry-picked as
70d44af).  When jq is present AND uname -s reports Linux or Darwin, three
fields are extracted via structural jq rather than naive regex:

  - tool_name      via  jq -r '.tool_name // empty'
  - ready          via  jq -r '.ready | tostring'
  - has_tunnel     via  jq -r '.has_tunnel | tostring'

These tests prove the jq path is engaged and correct WITHOUT touching the
buggy exit-2 blocking path (sprint item 994c5b67, separately tracked, pre-
existing baseline: 4 failures in test_code_intel_guard.py).

Skip conditions for jq-dependent tests:
  _needs_jq_linux -- jq must be present AND uname must be Linux/Darwin in the
    bash subprocess environment (degrades gracefully on Windows Git-Bash where
    uname returns MINGW64_*).
  _needs_bash_http_reachable -- the bash subprocess must be able to connect to
    a Python-side HTTP server on 127.0.0.1 (fails on WSL2 where Python runs on
    Windows but bash runs in the Linux guest, giving network isolation; passes
    on native Linux CI where both share the same loopback).

Together these two marks make the jq tests run in the intended CI environment
(native Linux with real jq) and skip gracefully on WSL2 dev machines, without
ever producing false failures.

The structural regression guard (test 4) has NO subprocess dependency and
always runs.
"""
from __future__ import annotations

import http.server
import json
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Repo / hook paths
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parent.parent
_HOOK_SH = _REPO / ".claude" / "hooks" / "code_intel_guard.sh"

# ---------------------------------------------------------------------------
# Environment probes (evaluated once at collection time)
# ---------------------------------------------------------------------------

_needs_bash = pytest.mark.skipif(
    not _HOOK_SH.exists() or shutil.which("bash") is None,
    reason="code_intel_guard.sh or bash unavailable",
)


def _jq_and_linux_available() -> bool:
    """True iff the bash subprocess environment has jq AND Linux/Darwin uname."""
    try:
        r = subprocess.run(
            ["bash", "-c", "command -v jq >/dev/null 2>&1 && uname -s"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode != 0:
            return False
        uname = r.stdout.strip()
        return uname in ("Linux", "Darwin")
    except Exception:
        return False


def _bash_can_reach_python_http() -> bool:
    """True iff a bash subprocess can curl a Python HTTP server on 127.0.0.1.

    This probe catches WSL2 environments where Python runs on the Windows host
    but bash runs in the Linux guest -- they have separate loopback interfaces
    and the bash curl cannot reach the Python server even on 127.0.0.1.  On
    native Linux CI (and macOS) both processes share the same loopback, so the
    probe succeeds.
    """
    try:
        class _ProbeHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args, **kwargs):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), _ProbeHandler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        r = subprocess.run(
            ["bash", "-c", f"curl -sf --max-time 2 http://127.0.0.1:{port}/probe"],
            capture_output=True,
            timeout=8,
        )
        srv.shutdown()
        return r.returncode == 0
    except Exception:
        return False


# Compute once at import time so multiple tests share the result.
_JQ_LINUX_OK: bool = _jq_and_linux_available()
_HTTP_REACHABLE: bool = _bash_can_reach_python_http() if _JQ_LINUX_OK else False

_needs_jq_linux = pytest.mark.skipif(
    not _JQ_LINUX_OK,
    reason="jq not found or uname is not Linux/Darwin in the bash subprocess environment",
)

_needs_bash_http_reachable = pytest.mark.skipif(
    not _HTTP_REACHABLE,
    reason=(
        "bash subprocess cannot reach Python HTTP server on 127.0.0.1 "
        "(WSL2 network isolation -- tests designed for native Linux CI)"
    ),
)

# ---------------------------------------------------------------------------
# Windows NTSTATUS crash codes (same set as test_code_intel_guard.py)
# ---------------------------------------------------------------------------

_WIN_CRASH_CODES = frozenset(
    {
        0xC0000005 & 0xFFFFFFFF,
        0xC000007B & 0xFFFFFFFF,
        0xC0000135 & 0xFFFFFFFF,
        0xC0000142 & 0xFFFFFFFF,
        0xC000013A & 0xFFFFFFFF,
        3221225773,
    }
)

# ---------------------------------------------------------------------------
# Hook runner (mirrors test_code_intel_guard.py's _run_hook pattern)
# ---------------------------------------------------------------------------


def _run_hook_once(
    payload: str,
    *,
    meridian_url: str,
) -> subprocess.CompletedProcess:
    """Run code_intel_guard.sh with MERIDIAN_URL set via bash export."""
    setup = f'export MERIDIAN_URL="{meridian_url}"; '
    cmd = setup + "exec bash .claude/hooks/code_intel_guard.sh"
    r = subprocess.run(
        ["bash", "-c", cmd],
        input=payload.encode("utf-8"),
        cwd=str(_REPO),
        capture_output=True,
        timeout=30,
    )
    return subprocess.CompletedProcess(
        r.args,
        r.returncode,
        stdout=(r.stdout or b"").decode("utf-8", "replace"),
        stderr=(r.stderr or b"").decode("utf-8", "replace"),
    )


def _run_hook(payload: str, *, meridian_url: str) -> subprocess.CompletedProcess:
    """Retry on Windows subprocess-teardown crashes."""
    last: subprocess.CompletedProcess | None = None
    for _ in range(3):
        try:
            last = _run_hook_once(payload, meridian_url=meridian_url)
        except subprocess.TimeoutExpired:
            continue
        if last.returncode in _WIN_CRASH_CODES:
            continue
        return last
    assert last is not None, "bash never produced a result (all attempts crashed)"
    return last


# ---------------------------------------------------------------------------
# Multi-route HTTP stub (settings + slot-readiness on different paths)
# ---------------------------------------------------------------------------


class _MultiRouteHandler(http.server.BaseHTTPRequestHandler):
    """Returns different bodies based on the URL path fragment.

    Class-level attributes are set per-test:
      settings_body      -- JSON string for .../settings requests
      slot_body          -- JSON string for .../slot-readiness requests
      request_counter    -- list used as a counter (thread-safe append)
    """

    settings_body: str = json.dumps({"code_intel_enabled": 0})
    slot_body: str = json.dumps({"ready": True, "has_tunnel": True})
    request_counter: list[str] = []

    def do_GET(self):  # noqa: N802
        self.__class__.request_counter.append(self.path)
        if "slot-readiness" in self.path:
            body = self.__class__.slot_body.encode()
        else:
            body = self.__class__.settings_body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args, **kwargs):
        pass  # silence access log


def _start_multi_stub(
    *,
    settings_body: str,
    slot_body: str,
) -> tuple[int, threading.Thread, list[str]]:
    """Start a multi-route stub; return (port, thread, request_counter)."""
    counter: list[str] = []
    _MultiRouteHandler.settings_body = settings_body
    _MultiRouteHandler.slot_body = slot_body
    _MultiRouteHandler.request_counter = counter
    server = http.server.HTTPServer(("127.0.0.1", 0), _MultiRouteHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return port, t, counter


# ---------------------------------------------------------------------------
# Test 1: tool_name jq structural correctness via fail-open path
#
# Key insight: the pre-existing naive regex scans raw text left-to-right, so
# the FIRST occurrence of "tool_name" wins -- including one buried inside a
# nested object.  jq's structural extraction reads only the TOP-LEVEL
# .tool_name key.
#
# Payload: nested decoy "Bash" appears BEFORE the real top-level "Grep" in
# serialised JSON (tool_input serialises first alphabetically, and the nested
# key "tool_name" inside tool_input appears earlier in the raw text).
#
#   {"tool_input": {"decoy": {"tool_name": "Bash"}}, "tool_name": "Grep"}
#
# Server returns code_intel_enabled=0 so the hook deterministically exits 0
# regardless of tool name -- avoiding the buggy exit-2 path entirely.
#
# Proof mechanism: the hook only calls /settings if tool resolved to "Grep" or
# "Glob" (case "$tool" in Grep|Glob) ;; *) exit 0 ;; esac).  If jq correctly
# extracted "Grep" (top-level), the stub gets hit.  If naive regex wrongly
# grabbed the nested "Bash" decoy, the hook exits 0 at the case-gate without
# ever calling curl, so request_counter stays 0.
# ---------------------------------------------------------------------------


@pytest.mark.subprocess_isolated
@_needs_bash
@_needs_jq_linux
@_needs_bash_http_reachable
def test_jq_tool_name_structural_extraction_ignores_nested_decoy():
    """jq reads top-level .tool_name='Grep', not nested decoy 'Bash'.

    Asserts:
      (a) hook exits 0 (deterministic fail-open: code_intel_enabled=0)
      (b) stub received >= 1 request (proves hook hit /settings, meaning
          tool resolved as 'Grep', not the nested 'Bash' decoy)
    """
    port, _, counter = _start_multi_stub(
        settings_body=json.dumps({"code_intel_enabled": 0}),
        slot_body=json.dumps({"ready": True, "has_tunnel": True}),
    )

    # Build a payload where the nested decoy "Bash" appears first in the raw
    # JSON text (Python's json.dumps preserves insertion order: tool_input
    # comes before tool_name at the top level, so the decoy key is earlier).
    # Verify this ordering assumption holds before using it as a test basis.
    payload_dict = {
        "tool_input": {"decoy": {"tool_name": "Bash"}},
        "tool_name": "Grep",
    }
    raw_payload = json.dumps(payload_dict)
    # Sanity: confirm the decoy "Bash" really does appear BEFORE the top-level
    # "Grep" in the serialised text (so the regex fallback would pick it up
    # first, but jq would not).
    bash_pos = raw_payload.index('"Bash"')
    grep_pos = raw_payload.index('"Grep"')
    assert bash_pos < grep_pos, (
        f"Test assumption violated: 'Bash' ({bash_pos}) must precede "
        f"'Grep' ({grep_pos}) in serialised JSON for the decoy to be meaningful"
    )

    url = f"http://127.0.0.1:{port}"
    r = _run_hook(raw_payload, meridian_url=url)

    # (a) exit 0: code_intel_enabled=0 -> deterministic fail-open
    assert r.returncode == 0, (
        f"Hook must exit 0 (fail-open, code_intel_enabled=0), got {r.returncode}"
    )

    # (b) /settings was called: proves tool resolved as 'Grep', not 'Bash'
    settings_hits = [p for p in counter if "settings" in p]
    assert len(settings_hits) >= 1, (
        "Stub received no /settings request -- this means the hook exited "
        "early at the 'Grep|Glob' case gate, implying tool resolved as 'Bash' "
        "(the nested decoy) rather than the correct top-level 'Grep'.  "
        "The jq fast path is NOT being engaged or is broken.  "
        f"Raw payload: {raw_payload!r}"
    )


# ---------------------------------------------------------------------------
# Test 2a: ready=false jq correctness -> explicit exit 0 + NOT ready stderr
#
# Sets code_intel_enabled=1 (to enter the slot-readiness branch) but
# slot-readiness returns ready=false, has_tunnel=true on EVERY request
# (including the retry). The hook must:
#   - exit 0  (the explicit `exit 0` at line 102 of the hook)
#   - emit "NOT ready after warmup probe" to stderr
# This is independent of the buggy exit-2 path.
# ---------------------------------------------------------------------------


@pytest.mark.subprocess_isolated
@_needs_bash
@_needs_jq_linux
@_needs_bash_http_reachable
def test_jq_ready_false_triggers_failopen_not_ready_warning():
    """jq extracts ready=false -> hook fails open with 'NOT ready' warning.

    slot-readiness returns {"ready": false, "has_tunnel": true} on all probes.
    Both the initial probe and the retry see ready=false, so the hook takes the
    explicit `exit 0` branch with a VISIBLE stderr message.
    """
    port, _, counter = _start_multi_stub(
        settings_body=json.dumps({"code_intel_enabled": 1}),
        slot_body=json.dumps({"ready": False, "has_tunnel": True}),
    )
    url = f"http://127.0.0.1:{port}"
    payload = json.dumps({"tool_name": "Grep", "tool_input": {}})

    r = _run_hook(payload, meridian_url=url)

    assert r.returncode == 0, (
        f"Hook must exit 0 when slot not ready, got {r.returncode}"
    )
    assert "NOT ready after warmup probe" in r.stderr, (
        f"stderr must contain 'NOT ready after warmup probe'; got: {r.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Test 2b: has_tunnel=false jq correctness -> explicit exit 0 + no tunnel stderr
# ---------------------------------------------------------------------------


@pytest.mark.subprocess_isolated
@_needs_bash
@_needs_jq_linux
@_needs_bash_http_reachable
def test_jq_has_tunnel_false_triggers_failopen_no_tunnel_warning():
    """jq extracts has_tunnel=false -> hook fails open with 'no tunnel' warning.

    slot-readiness returns {"ready": true, "has_tunnel": false}.
    The hook hits the explicit `elif [ "$has_tunnel" = "false" ]` branch.
    """
    port, _, counter = _start_multi_stub(
        settings_body=json.dumps({"code_intel_enabled": 1}),
        slot_body=json.dumps({"ready": True, "has_tunnel": False}),
    )
    url = f"http://127.0.0.1:{port}"
    payload = json.dumps({"tool_name": "Grep", "tool_input": {}})

    r = _run_hook(payload, meridian_url=url)

    assert r.returncode == 0, (
        f"Hook must exit 0 when has_tunnel=false, got {r.returncode}"
    )
    assert "no tunnel slot is connected" in r.stderr, (
        f"stderr must contain 'no tunnel slot is connected'; got: {r.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Test 2c: decoy variant -- nested contradicting ready/has_tunnel is ignored
#
# Confirms that jq reads TOP-LEVEL ready/has_tunnel, not a nested decoy.
# Slot body: nested object with ready=true/has_tunnel=true as decoys, but
# top-level ready=false (so the hook should still take the NOT-ready path).
# ---------------------------------------------------------------------------


@pytest.mark.subprocess_isolated
@_needs_bash
@_needs_jq_linux
@_needs_bash_http_reachable
def test_jq_slot_readiness_ignores_nested_decoy_ready_field():
    """jq reads top-level .ready, not a nested decoy.

    Slot body has nested { "inner": { "ready": true, "has_tunnel": true } }
    as a decoy, but top-level ready=false.  If jq is engaged, hook sees
    ready=false and fails open with 'NOT ready' warning.  If naive regex
    grabbed the nested 'true' first, the hook would proceed to block (exit 2,
    buggy path) -- but our test asserts exit 0, so a wrong answer FAILS.
    """
    # Build a slot-readiness body where decoy true appears BEFORE top-level false.
    # Nested key "ready" with value true comes first in serialised order.
    slot_dict = {
        "inner": {"ready": True, "has_tunnel": True},  # decoy -- nested
        "ready": False,     # top-level (correct value)
        "has_tunnel": True,
    }
    slot_body = json.dumps(slot_dict)

    # Sanity: 'true' must appear before 'false' in raw text for the decoy to matter.
    true_pos = slot_body.index("true")
    false_pos = slot_body.index("false")
    assert true_pos < false_pos, (
        f"Decoy assumption violated: 'true' ({true_pos}) must precede "
        f"'false' ({false_pos}) in slot body"
    )

    port, _, _ = _start_multi_stub(
        settings_body=json.dumps({"code_intel_enabled": 1}),
        slot_body=slot_body,
    )
    url = f"http://127.0.0.1:{port}"
    payload = json.dumps({"tool_name": "Grep", "tool_input": {}})

    r = _run_hook(payload, meridian_url=url)

    assert r.returncode == 0, (
        f"Hook must exit 0 (top-level ready=false wins over decoy), got {r.returncode}"
    )
    assert "NOT ready after warmup probe" in r.stderr, (
        f"stderr must contain 'NOT ready after warmup probe'; got: {r.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 (structural, no subprocess): hook source must contain jq fast path
# AND original regex fallbacks -- both must coexist.
# ---------------------------------------------------------------------------


def test_hook_source_contains_jq_fastpath_and_regex_fallbacks():
    """Structural: hook source must have jq fast path AND original regex fallbacks."""
    source = _HOOK_SH.read_text(encoding="utf-8")

    # --- jq fast-path structural markers ---
    assert "command -v jq" in source, (
        "Hook must check 'command -v jq' to gate the jq fast path"
    )
    assert "uname -s" in source, (
        "Hook must check 'uname -s' to gate the jq fast path (Linux/Darwin only)"
    )
    # Linux/Darwin gate -- both must appear in the source
    assert "Linux" in source, (
        "Hook must include 'Linux' in the uname-based platform gate"
    )
    assert "Darwin" in source, (
        "Hook must include 'Darwin' in the uname-based platform gate"
    )

    # --- jq filter strings ---
    assert ".tool_name // empty" in source, (
        "Hook must use jq filter '.tool_name // empty' for tool_name extraction"
    )
    assert ".ready | tostring" in source, (
        "Hook must use jq filter '.ready | tostring' for ready extraction"
    )
    assert ".has_tunnel | tostring" in source, (
        "Hook must use jq filter '.has_tunnel | tostring' for has_tunnel extraction"
    )

    # --- original regex fallback lines still present ---
    # These are the exact pre-existing tolerant regex extractions that the jq
    # path falls through to when jq is absent, uname is wrong, or jq returns empty.
    assert (
        'grep -oE \'"tool_name"[[:space:]]*:[[:space:]]*"[^"]*"\''
        in source
    ), "Original tool_name regex fallback must remain in the hook source"

    assert (
        'grep -oE \'"ready"[[:space:]]*:[[:space:]]*(true|false)\''
        in source
    ), "Original ready regex fallback must remain in the hook source"

    assert (
        'grep -oE \'"has_tunnel"[[:space:]]*:[[:space:]]*(true|false)\''
        in source
    ), "Original has_tunnel regex fallback must remain in the hook source"


# ---------------------------------------------------------------------------
# Test 5: jq path, slot-readiness JSON missing ready/has_tunnel entirely --
# must fail open (883ce543). `jq -r '.ready | tostring'` on a body with no
# "ready" key yields the string "null", which the hook's
# `case "$slot_ready" in true|false) ;; *) slot_ready="" ;; esac` guard must
# reset to empty (unconfirmed) rather than ever letting "null" be treated as
# a positive confirmation. This is the jq-path sibling of the
# unreachable/malformed tests in test_code_intel_guard.py -- same contract,
# proven specifically through the jq extraction path this time.
# ---------------------------------------------------------------------------


@pytest.mark.subprocess_isolated
@_needs_bash
@_needs_jq_linux
@_needs_bash_http_reachable
def test_jq_slot_readiness_missing_fields_fails_open_not_block():
    """jq's `.ready | tostring` on a body without "ready" yields "null", which
    must be normalized to unconfirmed (empty), not treated as a block signal.
    Before 883ce543, an unconfirmed value here fell through to BLOCK."""
    port, _, counter = _start_multi_stub(
        settings_body=json.dumps({"code_intel_enabled": 1}),
        slot_body=json.dumps({}),  # valid JSON, no ready/has_tunnel keys at all
    )
    url = f"http://127.0.0.1:{port}"
    payload = json.dumps({"tool_name": "Grep", "tool_input": {}})

    r = _run_hook(payload, meridian_url=url)

    assert r.returncode == 0, (
        f"Hook must fail open when slot-readiness JSON is missing ready/has_tunnel "
        f"(jq path), got {r.returncode}. This is the exact 883ce543 regression: "
        f"an unconfirmed value must never satisfy the block condition."
    )
    # /slot-readiness was genuinely reached (proves the jq path parsed the
    # top-level tool_name correctly and progressed past the settings gate).
    slot_hits = [p for p in counter if "slot-readiness" in p]
    assert len(slot_hits) >= 1, "stub never received a /slot-readiness request"


# ---------------------------------------------------------------------------
# Test 6 (structural, no subprocess): the fixed contract requires a
# POSITIVE confirmation of both ready=true and has_tunnel=true to block --
# it must no longer be possible for the block branch to run on merely "not
# explicitly false" (883ce543 regression guard, keeps the fix from silently
# reverting to the old fall-through-to-block shape).
# ---------------------------------------------------------------------------


def test_hook_block_condition_requires_positive_ready_and_tunnel_confirmation():
    """Structural regression guard: `exit 2` must be gated by an explicit
    `"$slot_ready" = "true"` AND `"$has_tunnel" = "true"` check, not merely
    reachable by falling out of an `if [ -n "$slot_resp" ]` block."""
    source = _HOOK_SH.read_text(encoding="utf-8")
    assert (
        'if [ "$slot_ready" = "true" ] && [ "$has_tunnel" = "true" ]; then'
        in source
    ), (
        "The block path (exit 2) must be gated by a positive confirmation of "
        "BOTH slot_ready and has_tunnel being exactly 'true' -- an unconfirmed "
        "(empty/unparseable) or explicitly false value must never reach exit 2."
    )
