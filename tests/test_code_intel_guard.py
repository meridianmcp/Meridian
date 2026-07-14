"""aeba8a80 -- PreToolUse code-intel guard structurally blocks Grep/Glob when
code-intel is enabled for this project.

Prose guidance (DEFAULT_AGENT_INSTRUCTIONS v10) failed: a live session used raw
grep exclusively instead of code-intel tools. This tests the ACTUAL hook BEHAVIOR:

1. The hook (code_intel_guard.sh via bash) -- primary deliverable.
   - Blocks Grep and Glob (exit 2) when the Meridian server reports
     code_intel_enabled=1 for the project.
   - Fails open (exit 0) when code_intel_enabled=0.
   - Fails open (exit 0) when the server is unreachable (curl --max-time).
   - Fails open (exit 0) on any other tool (passthrough).
   - Fails open (exit 0) on garbage/missing stdin.

2. settings.json actually registers the hook under PreToolUse with
   matcher "Grep|Glob" -- structural wiring, not just file presence.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from meridian import db as db_module

_REPO = Path(__file__).resolve().parent.parent
_HOOK_SH = _REPO / ".claude" / "hooks" / "code_intel_guard.sh"
_SETTINGS = _REPO / ".claude" / "settings.json"

_needs_bash = pytest.mark.skipif(
    not _HOOK_SH.exists() or shutil.which("bash") is None,
    reason="code_intel_guard.sh or bash unavailable",
)

# Windows NTSTATUS crash exit codes seen under heavy xdist (-n auto) contention.
_WIN_CRASH_CODES = frozenset(
    {
        0xC0000005 & 0xFFFFFFFF,  # ACCESS_VIOLATION
        0xC000007B & 0xFFFFFFFF,  # INVALID_IMAGE_FORMAT
        0xC0000135 & 0xFFFFFFFF,  # DLL_NOT_FOUND
        0xC0000142 & 0xFFFFFFFF,  # DLL_INIT_FAILED
        0xC000013A & 0xFFFFFFFF,  # CONTROL_C_EXIT / kill
        3221225773,               # observed under -n auto
    }
)


# ---------------------------------------------------------------------------
# Minimal HTTP stub for the /projects/{id}/settings endpoint
# ---------------------------------------------------------------------------

class _StubHandler(http.server.BaseHTTPRequestHandler):
    """Serves a single canned JSON response for any GET /projects/*/settings."""

    code_intel_enabled: int = 1  # class-level, set per test via the fixture

    def do_GET(self):  # noqa: N802
        body = json.dumps({"code_intel_enabled": self.__class__.code_intel_enabled}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args, **kwargs):
        pass  # silence access log


def _start_stub_server(code_intel_enabled: int) -> tuple[int, threading.Thread]:
    """Start a stub HTTP server; return (port, thread)."""
    _StubHandler.code_intel_enabled = code_intel_enabled
    server = http.server.HTTPServer(("127.0.0.1", 0), _StubHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return port, t


def _free_port() -> int:
    """Return a port number that is currently not bound (best-effort)."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_hook_once(
    payload: str, *, meridian_url: str | None = None
) -> subprocess.CompletedProcess:
    """Run code_intel_guard.sh from cwd=repo root. Mirrors test_hitl_guard.py."""
    # MERIDIAN_URL is exported inside bash so it survives the msys env-drop on
    # Windows (same technique as test_test_tamper_guard.py). Default to a port
    # that is not bound so curl hits "connection refused" quickly (not a timeout
    # hang as port 9 can cause on Windows).
    if meridian_url is None:
        port = _free_port()
        meridian_url = f"http://127.0.0.1:{port}"
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


def _run_hook(
    payload: str, *, meridian_url: str | None = None
) -> subprocess.CompletedProcess:
    """Retry on Windows subprocess-teardown crashes (harness artifact, not hook)."""
    last: subprocess.CompletedProcess | None = None
    for _ in range(3):
        try:
            last = _run_hook_once(payload, meridian_url=meridian_url)
        except subprocess.TimeoutExpired:
            continue
        if last.returncode in _WIN_CRASH_CODES:
            continue
        return last
    assert last is not None, "git-bash never produced a result (all attempts crashed)"
    return last


# ---------------------------------------------------------------------------
# Tests: fail open when server is unreachable
# ---------------------------------------------------------------------------

@_needs_bash
@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_hook_fails_open_when_server_unreachable(tool):
    """MERIDIAN_URL on an unbound port -- connection refused, hook exits 0."""
    port = _free_port()
    payload = json.dumps({"tool_name": tool, "tool_input": {}})
    r = _run_hook(payload, meridian_url=f"http://127.0.0.1:{port}")
    assert r.returncode == 0, f"{tool}: must fail open when server is unreachable"


# ---------------------------------------------------------------------------
# Tests: fails open when code_intel_enabled == 0
# ---------------------------------------------------------------------------

@_needs_bash
@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_hook_fails_open_when_code_intel_disabled(tool):
    """code_intel_enabled=0 -- no index, hook exits 0 (nothing to redirect to)."""
    port, _ = _start_stub_server(code_intel_enabled=0)
    payload = json.dumps({"tool_name": tool, "tool_input": {}})
    r = _run_hook(payload, meridian_url=f"http://127.0.0.1:{port}")
    assert r.returncode == 0, f"{tool}: must fail open when code_intel_enabled=0"


# ---------------------------------------------------------------------------
# Tests: blocks when code_intel_enabled == 1
# ---------------------------------------------------------------------------

@_needs_bash
@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_hook_blocks_grep_glob_when_code_intel_enabled(tool):
    """code_intel_enabled=1 -- hook exits 2 and stderr names code-intel tools."""
    port, _ = _start_stub_server(code_intel_enabled=1)
    payload = json.dumps({"tool_name": tool, "tool_input": {}})
    r = _run_hook(payload, meridian_url=f"http://127.0.0.1:{port}")
    assert r.returncode == 2, f"{tool}: must exit 2 when code-intel index exists"
    assert "find_symbol" in r.stderr, "must mention find_symbol as the alternative"
    assert "search_graph" in r.stderr, "must mention search_graph as the alternative"
    assert "aeba8a80" in r.stderr, "must cite the item id"


@_needs_bash
@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_hook_stderr_names_the_tool_that_was_blocked(tool):
    """The error message names the specific blocked tool (Grep or Glob)."""
    port, _ = _start_stub_server(code_intel_enabled=1)
    payload = json.dumps({"tool_name": tool, "tool_input": {}})
    r = _run_hook(payload, meridian_url=f"http://127.0.0.1:{port}")
    assert r.returncode == 2
    assert tool in r.stderr, f"stderr must name the blocked tool ({tool})"


# ---------------------------------------------------------------------------
# Tests: passthrough for every other tool
# ---------------------------------------------------------------------------

@_needs_bash
@pytest.mark.parametrize(
    "tool",
    ["Bash", "Edit", "Write", "Read", "AskUserQuestion", "find_symbol",
     "search_graph", "MultiEdit", "NotebookEdit"],
)
def test_hook_allows_all_other_tools(tool):
    """Tools other than Grep/Glob must never be blocked, even with index enabled."""
    port, _ = _start_stub_server(code_intel_enabled=1)
    payload = json.dumps({"tool_name": tool, "tool_input": {}})
    r = _run_hook(payload, meridian_url=f"http://127.0.0.1:{port}")
    assert r.returncode == 0, f"{tool} must not be blocked by the code-intel guard"


# ---------------------------------------------------------------------------
# Tests: fail open on garbage / missing stdin
# ---------------------------------------------------------------------------

@_needs_bash
@pytest.mark.parametrize("payload", ["", "not json at all", "{}", '{"foo":"bar"}'])
def test_hook_fails_open_on_unparseable(payload):
    """Malformed or missing stdin: hook must never trap the executor."""
    port, _ = _start_stub_server(code_intel_enabled=1)
    r = _run_hook(payload, meridian_url=f"http://127.0.0.1:{port}")
    assert r.returncode == 0, "must fail open on unparseable payload"


# ---------------------------------------------------------------------------
# Test: settings.json actually wires the guard
# ---------------------------------------------------------------------------

def test_settings_wires_grep_glob_matcher():
    """The hook must be registered -- structural wiring, not just file presence."""
    cfg = json.loads(_SETTINGS.read_text(encoding="utf-8"))
    pre = cfg.get("hooks", {}).get("PreToolUse", [])
    entry = next(
        (e for e in pre if e.get("matcher") == "Grep|Glob"), None
    )
    assert entry is not None, "PreToolUse must have a Grep|Glob matcher entry"
    cmds = " ".join(h.get("command", "") for h in entry.get("hooks", []))
    assert "code_intel_guard" in cmds, "the Grep|Glob matcher must run code_intel_guard"


# ---------------------------------------------------------------------------
# Test: project settings endpoint returns code_intel_enabled
# ---------------------------------------------------------------------------

def test_project_settings_endpoint_exposes_code_intel_enabled(client):
    """The /projects/{id}/settings endpoint returns code_intel_enabled (0 or 1)."""
    db = client.app.state.db
    p = asyncio.run(db_module.create_project(db, "ci-guard-endpoint"))
    # Default should be 0 (not enabled).
    r = client.get(f"/projects/{p['id']}/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "code_intel_enabled" in body, "settings must expose code_intel_enabled"
    assert body["code_intel_enabled"] in (0, 1), "value must be 0 or 1"


def test_code_intel_enabled_flag_is_patchable(client):
    """PATCH /settings sets code_intel_enabled; GET reflects the change."""
    db = client.app.state.db
    p = asyncio.run(db_module.create_project(db, "ci-guard-patch"))
    r = client.patch(
        f"/projects/{p['id']}/settings",
        json={"code_intel_enabled": 1},
    )
    assert r.status_code == 200, r.text
    r2 = client.get(f"/projects/{p['id']}/settings")
    assert r2.json()["code_intel_enabled"] == 1


# ---------------------------------------------------------------------------
# Test: agent_defaults version bump
# ---------------------------------------------------------------------------

def test_agent_defaults_version_bumped_for_structural_hook():
    """aeba8a80 ships a structural hook -- the standard version must be >= 11."""
    from meridian.agent_defaults import (
        AGENT_INSTRUCTIONS_STANDARD_VERSION,
        DEFAULT_AGENT_INSTRUCTIONS,
        parse_standard_version,
    )

    assert AGENT_INSTRUCTIONS_STANDARD_VERSION >= 11, (
        "v11 adds the structural enforcement note for the code_intel_guard hook"
    )
    embedded = parse_standard_version(DEFAULT_AGENT_INSTRUCTIONS)
    assert embedded == AGENT_INSTRUCTIONS_STANDARD_VERSION, (
        f"Embedded marker v{embedded} != constant v{AGENT_INSTRUCTIONS_STANDARD_VERSION}; "
        "bump AGENT_INSTRUCTIONS_STANDARD_VERSION and update the <!-- marker --> together."
    )
    # The instructions must mention the hook ID and structural enforcement.
    assert "aeba8a80" in DEFAULT_AGENT_INSTRUCTIONS, (
        "DEFAULT_AGENT_INSTRUCTIONS must mention the hook item id (aeba8a80)"
    )
    assert "code_intel_guard" in DEFAULT_AGENT_INSTRUCTIONS, (
        "DEFAULT_AGENT_INSTRUCTIONS must mention the hook name"
    )
