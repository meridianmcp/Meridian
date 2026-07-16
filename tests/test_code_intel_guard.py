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
import functools
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
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
# Network topology detection
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _bash_is_wsl2() -> bool:
    """Return True if the 'bash' on PATH is WSL2 bash (Linux kernel in Hyper-V).

    WSL2 bash runs in a separate network namespace from Windows.  The Windows
    Hyper-V firewall blocks inbound connections from WSL2 to Windows on
    ephemeral ports, so a stub server bound to 127.0.0.1 or 0.0.0.0 on Windows
    is unreachable from WSL2 bash.  On Git Bash / native Linux CI there is no
    such boundary.
    """
    try:
        r = subprocess.run(
            ["bash", "-c",
             "grep -qi 'microsoft.*wsl2\\|wsl2.*microsoft' /proc/version 2>/dev/null"
             " && echo yes || echo no"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return r.stdout.strip() == "yes"
    except Exception:
        return False


def _windows_path_to_wsl(path: str) -> str:
    """Convert a Windows path (C:\\...) to a WSL2-accessible /mnt/c/... path."""
    path = path.replace("\\", "/")
    path = re.sub(r"^([A-Za-z]):/", lambda m: f"/mnt/{m.group(1).lower()}/", path)
    return path


# ---------------------------------------------------------------------------
# Stub server inline script (used by both native and WSL2 modes)
# ---------------------------------------------------------------------------

def _stub_script(code_intel_enabled: int) -> str:
    """Return a self-contained Python script that starts an HTTP stub server.

    The script writes its bound port number to stdout before entering
    serve_forever(), allowing the parent process to read it back.

    For /slot-readiness requests the stub returns ready=true + has_tunnel=true
    so the guard sees a ready slot and proceeds to block (exit 2) when
    code_intel_enabled=1, rather than failing open due to "no tunnel".
    """
    return f"""\
import http.server, json, sys

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if 'slot-readiness' in self.path:
            body = json.dumps({{"ready": True, "has_tunnel": True}}).encode()
        else:
            body = json.dumps({{"code_intel_enabled": {code_intel_enabled}}}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a, **k): pass

s = http.server.HTTPServer(('127.0.0.1', 0), H)
sys.stdout.write(str(s.server_address[1]) + '\\n')
sys.stdout.flush()
s.serve_forever()
"""


# ---------------------------------------------------------------------------
# Stub server helpers
# ---------------------------------------------------------------------------

class _StubServer:
    """Minimal HTTP stub that lives in the same network namespace as bash.

    Root cause: on Windows, 'bash' resolves to WSL2 (C:\\Windows\\System32\\
    bash.exe).  WSL2 runs a full Linux kernel under Hyper-V with its own
    network namespace.  A Python HTTP server started on the Windows side (even
    bound to 0.0.0.0) is blocked by the Windows Hyper-V firewall and is NOT
    reachable from inside WSL2 on ephemeral ports.

    Fix: when WSL2 bash is detected, start the stub server as a python3
    subprocess INSIDE WSL2, by writing the server script to a Windows temp file
    (accessible from WSL2 via /mnt/c/...) and running it via bash.  Both the
    stub server and the hook then run in WSL2's network namespace and reach each
    other on 127.0.0.1.

    On Git Bash / Linux CI (no WSL2 boundary) the classic Python-side server
    bound to 127.0.0.1 works as before.
    """

    def __init__(self, code_intel_enabled: int) -> None:
        self.code_intel_enabled = code_intel_enabled
        self._thread: threading.Thread | None = None
        self._wsl_proc: "subprocess.Popen[bytes] | None" = None
        self._tmpfile: str | None = None
        self.url: str = ""

    def start(self) -> None:
        if _bash_is_wsl2():
            self._start_in_wsl2()
        else:
            self._start_native()

    def _start_native(self) -> None:
        """Start the stub on the Windows / Linux CI side."""
        script = _stub_script(self.code_intel_enabled)
        # We can't use the script helper directly in threading, so start a
        # plain HTTPServer the old way.
        class _H(http.server.BaseHTTPRequestHandler):
            ci_val = self.code_intel_enabled

            def do_GET(self):  # noqa: N802
                if "slot-readiness" in self.path:
                    body = json.dumps({"ready": True, "has_tunnel": True}).encode()
                else:
                    body = json.dumps({"code_intel_enabled": self.__class__.ci_val}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a, **kw): pass  # silence

        srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
        port = srv.server_address[1]
        self._thread = threading.Thread(target=srv.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{port}"

    def _start_in_wsl2(self) -> None:
        """Start the stub as a python3 process inside WSL2.

        Writes the server script to a Windows temp file, converts the path to
        WSL2's /mnt/c/... form, and runs it via 'bash -c python3 <path>'.
        The server immediately prints its bound port to stdout so we know when
        it's ready.
        """
        script = _stub_script(self.code_intel_enabled)
        # Write to Windows temp; WSL2 can read it via /mnt/<drive>/...
        tmp = tempfile.NamedTemporaryFile(
            suffix=".py", mode="w", delete=False, encoding="utf-8"
        )
        tmp.write(script)
        tmp.close()
        self._tmpfile = tmp.name
        wsl_path = _windows_path_to_wsl(tmp.name)

        proc = subprocess.Popen(
            ["bash", "-c", f"python3 '{wsl_path}'"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdout is not None
        # Server writes port then blocks in serve_forever.
        port_line = proc.stdout.readline().decode("utf-8", "replace").strip()
        if not port_line.isdigit():
            proc.kill()
            stderr = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", "replace")
            raise RuntimeError(
                f"WSL2 stub server failed to start (got port={port_line!r}, "
                f"stderr={stderr!r})"
            )
        self._wsl_proc = proc
        self.url = f"http://127.0.0.1:{port_line}"

    def stop(self) -> None:
        if self._wsl_proc is not None:
            self._wsl_proc.kill()
            self._wsl_proc = None
        if self._tmpfile is not None:
            try:
                os.unlink(self._tmpfile)
            except OSError:
                pass
            self._tmpfile = None


def _start_stub_server(code_intel_enabled: int) -> tuple[str, _StubServer]:
    """Start a stub HTTP server in the correct network namespace for bash.

    Returns (url, stub).  Pass *url* directly as MERIDIAN_URL to the hook --
    it is already set to a 127.0.0.1 address reachable by the bash process
    that will run the hook (WSL2 or native).
    """
    stub = _StubServer(code_intel_enabled)
    stub.start()
    return stub.url, stub


def _free_url() -> str:
    """Return a URL on an unbound port in the same namespace as bash.

    Used by the 'server unreachable' tests: the port must be free (no listener)
    *in the namespace where curl runs*, so WSL2 gets a free WSL2 port.
    """
    if _bash_is_wsl2():
        r = subprocess.run(
            ["bash", "-c",
             "python3 -c \""
             "import socket; s=socket.socket(); s.bind(('127.0.0.1',0));"
             " p=s.getsockname()[1]; s.close(); print(p)"
             "\""],
            capture_output=True,
            text=True,
            timeout=10,
        )
        port = r.stdout.strip()
        if port.isdigit():
            return f"http://127.0.0.1:{port}"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}"


# ---------------------------------------------------------------------------
# Hook runner
# ---------------------------------------------------------------------------

def _run_hook_once(
    payload: str, *, meridian_url: str | None = None
) -> subprocess.CompletedProcess:
    """Run code_intel_guard.sh from cwd=repo root. Mirrors test_hitl_guard.py."""
    # MERIDIAN_URL is exported inside bash so it survives the msys env-drop on
    # Windows (same technique as test_test_tamper_guard.py). Default to a port
    # that is not bound so curl hits "connection refused" quickly (not a timeout
    # hang as port 9 can cause on Windows).
    if meridian_url is None:
        meridian_url = _free_url()
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
    payload = json.dumps({"tool_name": tool, "tool_input": {}})
    r = _run_hook(payload, meridian_url=_free_url())
    assert r.returncode == 0, f"{tool}: must fail open when server is unreachable"


# ---------------------------------------------------------------------------
# Tests: fails open when code_intel_enabled == 0
# ---------------------------------------------------------------------------

@_needs_bash
@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_hook_fails_open_when_code_intel_disabled(tool):
    """code_intel_enabled=0 -- no index, hook exits 0 (nothing to redirect to)."""
    url, stub = _start_stub_server(code_intel_enabled=0)
    try:
        payload = json.dumps({"tool_name": tool, "tool_input": {}})
        r = _run_hook(payload, meridian_url=url)
        assert r.returncode == 0, f"{tool}: must fail open when code_intel_enabled=0"
    finally:
        stub.stop()


# ---------------------------------------------------------------------------
# Tests: blocks when code_intel_enabled == 1
# ---------------------------------------------------------------------------

@_needs_bash
@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_hook_blocks_grep_glob_when_code_intel_enabled(tool):
    """code_intel_enabled=1 -- hook exits 2 and stderr names code-intel tools."""
    url, stub = _start_stub_server(code_intel_enabled=1)
    try:
        payload = json.dumps({"tool_name": tool, "tool_input": {}})
        r = _run_hook(payload, meridian_url=url)
        assert r.returncode == 2, f"{tool}: must exit 2 when code-intel index exists"
        assert "find_symbol" in r.stderr, "must mention find_symbol as the alternative"
        assert "search_graph" in r.stderr, "must mention search_graph as the alternative"
        assert "aeba8a80" in r.stderr, "must cite the item id"
    finally:
        stub.stop()


@_needs_bash
@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_hook_stderr_names_the_tool_that_was_blocked(tool):
    """The error message names the specific blocked tool (Grep or Glob)."""
    url, stub = _start_stub_server(code_intel_enabled=1)
    try:
        payload = json.dumps({"tool_name": tool, "tool_input": {}})
        r = _run_hook(payload, meridian_url=url)
        assert r.returncode == 2
        assert tool in r.stderr, f"stderr must name the blocked tool ({tool})"
    finally:
        stub.stop()


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
    url, stub = _start_stub_server(code_intel_enabled=1)
    try:
        payload = json.dumps({"tool_name": tool, "tool_input": {}})
        r = _run_hook(payload, meridian_url=url)
        assert r.returncode == 0, f"{tool} must not be blocked by the code-intel guard"
    finally:
        stub.stop()


# ---------------------------------------------------------------------------
# Tests: fail open on garbage / missing stdin
# ---------------------------------------------------------------------------

@_needs_bash
@pytest.mark.parametrize("payload", ["", "not json at all", "{}", '{"foo":"bar"}'])
def test_hook_fails_open_on_unparseable(payload):
    """Malformed or missing stdin: hook must never trap the executor."""
    url, stub = _start_stub_server(code_intel_enabled=1)
    try:
        r = _run_hook(payload, meridian_url=url)
        assert r.returncode == 0, "must fail open on unparseable payload"
    finally:
        stub.stop()


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
