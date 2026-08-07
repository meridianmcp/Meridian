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
from meridian import code_intel_receipt as _cir_mod
from meridian.mcp.handlers import sprint_tools as _st_mod

_REPO = Path(__file__).resolve().parent.parent
_HOOK_SH = _REPO / ".claude" / "hooks" / "code_intel_guard.sh"
_HOOK_PS1 = _REPO / ".claude" / "hooks" / "code_intel_guard.ps1"
_SETTINGS = _REPO / ".claude" / "settings.json"

_needs_bash = pytest.mark.skipif(
    not _HOOK_SH.exists() or shutil.which("bash") is None,
    reason="code_intel_guard.sh or bash unavailable",
)


def _powershell_exe() -> str | None:
    """883ce543 -- resolve a PowerShell interpreter (pwsh preferred, then
    Windows PowerShell), mirroring test_w5_5fb084fe_ps1_components.py."""
    for exe in ("pwsh", "powershell"):
        found = shutil.which(exe)
        if found:
            return found
    return None


_needs_powershell = pytest.mark.skipif(
    _powershell_exe() is None or not _HOOK_PS1.exists(),
    reason="no PowerShell interpreter (pwsh/powershell) available, or code_intel_guard.ps1 missing",
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


# ---------------------------------------------------------------------------
# 883ce543 -- flexible slot-readiness stub: an arbitrary HTTP status + body on
# the /slot-readiness route (settings always reports code_intel_enabled as
# given). Used to reproduce the exact gap this item fixes -- the endpoint
# unreachable/erroring (curl -sf fails -> empty slot_resp) or returning a
# 200 body with no extractable ready/has_tunnel fields -- which previously
# fell through to BLOCK instead of failing open. Mirrors _StubServer's
# WSL2-awareness (same network-namespace problem applies here).
# ---------------------------------------------------------------------------

def _custom_slot_stub_script(code_intel_enabled: int, slot_status: int, slot_body: str) -> str:
    return f"""\
import http.server, sys

SLOT_STATUS = {slot_status}
SLOT_BODY = {slot_body!r}.encode('utf-8')
SETTINGS_BODY = b'{{"code_intel_enabled": {code_intel_enabled}}}'

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if 'slot-readiness' in self.path:
            body = SLOT_BODY
            self.send_response(SLOT_STATUS)
        else:
            body = SETTINGS_BODY
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


def _start_custom_slot_stub(code_intel_enabled: int, slot_status: int, slot_body: str):
    """Start a stub whose /slot-readiness route returns an arbitrary status +
    body, in the correct network namespace for bash. Returns
    (url, handle, tmpfile_or_None); pass both to _stop_custom_slot_stub."""
    if _bash_is_wsl2():
        script = _custom_slot_stub_script(code_intel_enabled, slot_status, slot_body)
        tmp = tempfile.NamedTemporaryFile(
            suffix=".py", mode="w", delete=False, encoding="utf-8"
        )
        tmp.write(script)
        tmp.close()
        wsl_path = _windows_path_to_wsl(tmp.name)
        proc = subprocess.Popen(
            ["bash", "-c", f"python3 '{wsl_path}'"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdout is not None
        port_line = proc.stdout.readline().decode("utf-8", "replace").strip()
        if not port_line.isdigit():
            proc.kill()
            stderr = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", "replace")
            raise RuntimeError(
                f"WSL2 stub server failed to start (got port={port_line!r}, stderr={stderr!r})"
            )
        return f"http://127.0.0.1:{port_line}", proc, tmp.name

    slot_body_bytes = slot_body.encode("utf-8")
    settings_bytes = json.dumps({"code_intel_enabled": code_intel_enabled}).encode()

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if "slot-readiness" in self.path:
                body = slot_body_bytes
                self.send_response(slot_status)
            else:
                body = settings_bytes
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a, **kw):  # noqa: N802
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return f"http://127.0.0.1:{port}", t, None


def _stop_custom_slot_stub(handle, tmpfile: str | None) -> None:
    if isinstance(handle, subprocess.Popen):
        handle.kill()
    if tmpfile is not None:
        try:
            os.unlink(tmpfile)
        except OSError:
            pass


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
# 883ce543 -- Tests: slot-readiness itself unreachable or unparseable must
# fail open, not fall through to BLOCK. This is the exact regression the
# item fixes: code_intel_enabled=1 confirms an index exists, but slot
# readiness could not be positively confirmed, so the hook must never
# escalate to exit 2.
# ---------------------------------------------------------------------------

@_needs_bash
@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_hook_fails_open_when_slot_readiness_unreachable_but_settings_ok(tool):
    """settings reports code_intel_enabled=1, but /slot-readiness errors
    (HTTP 500 -> curl -sf fails -> empty slot_resp). Before 883ce543 this fell
    through the `if [ -n "$slot_resp" ]` guard straight to the block path --
    the opposite of the documented fail-open policy."""
    url, handle, tmpfile = _start_custom_slot_stub(
        code_intel_enabled=1, slot_status=500, slot_body="internal error"
    )
    try:
        payload = json.dumps({"tool_name": tool, "tool_input": {}})
        r = _run_hook(payload, meridian_url=url)
        assert r.returncode == 0, (
            f"{tool}: must fail open when the slot-readiness endpoint errors, "
            f"not fall through to block (883ce543 regression). stderr={r.stderr!r}"
        )
    finally:
        _stop_custom_slot_stub(handle, tmpfile)


@_needs_bash
@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_hook_fails_open_when_slot_readiness_body_unparseable(tool):
    """settings reports code_intel_enabled=1, /slot-readiness returns HTTP 200
    but a body with no extractable ready/has_tunnel fields (neither jq nor the
    regex fallback can populate them). Before 883ce543 this also fell through
    to block instead of failing open."""
    url, handle, tmpfile = _start_custom_slot_stub(
        code_intel_enabled=1, slot_status=200, slot_body="not json at all"
    )
    try:
        payload = json.dumps({"tool_name": tool, "tool_input": {}})
        r = _run_hook(payload, meridian_url=url)
        assert r.returncode == 0, (
            f"{tool}: must fail open when the slot-readiness body is unparseable, "
            f"not fall through to block (883ce543 regression). stderr={r.stderr!r}"
        )
    finally:
        _stop_custom_slot_stub(handle, tmpfile)


@_needs_bash
@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_hook_fails_open_when_slot_readiness_json_missing_fields(tool):
    """/slot-readiness returns valid JSON (200) but omits ready/has_tunnel
    entirely -- jq's `.ready | tostring` yields "null" (not true/false) and
    the regex finds no match either way, so both stay unconfirmed. Must fail
    open, never block on an unconfirmed value."""
    url, handle, tmpfile = _start_custom_slot_stub(
        code_intel_enabled=1, slot_status=200, slot_body="{}"
    )
    try:
        payload = json.dumps({"tool_name": tool, "tool_input": {}})
        r = _run_hook(payload, meridian_url=url)
        assert r.returncode == 0, (
            f"{tool}: must fail open when ready/has_tunnel are missing from an "
            f"otherwise-valid slot-readiness body. stderr={r.stderr!r}"
        )
    finally:
        _stop_custom_slot_stub(handle, tmpfile)


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


# ===========================================================================
# 883ce543 -- PowerShell path: .claude/settings.json wires code_intel_guard.ps1
# for the real Claude Code client on Windows (see the "powershell" shell
# entries in settings.json), while the tests above only ever exercise
# code_intel_guard.sh. These tests run the ACTUAL .ps1 hook as a subprocess
# (never source it in-process -- `exit` inside a dot-sourced/`&`-invoked
# script would terminate the CURRENT PowerShell host, not just return), proving
# the .ps1 and .sh variants share one fail-open/block decision table for the
# same inputs. Stub server runs a plain (non-WSL2) HTTP listener in-process --
# PowerShell on Windows talks to 127.0.0.1 directly, no WSL2 network-namespace
# boundary applies here (that boundary is specific to bash resolving to WSL2).
# ===========================================================================

def _start_plain_stub(
    *, code_intel_enabled: int, slot_status: int = 200, slot_body: str | None = None
) -> tuple[str, http.server.HTTPServer, threading.Thread]:
    """Start a plain in-process HTTP stub for the PS1 hook (native Invoke-RestMethod,
    no WSL2 namespace concerns). Returns (url, server, thread)."""
    if slot_body is None:
        slot_body = json.dumps({"ready": True, "has_tunnel": True})
    slot_bytes = slot_body.encode("utf-8")
    settings_bytes = json.dumps({"code_intel_enabled": code_intel_enabled}).encode()

    class _H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if "slot-readiness" in self.path:
                body = slot_bytes
                self.send_response(slot_status)
            else:
                body = settings_bytes
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a, **kw):  # noqa: N802
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return f"http://127.0.0.1:{port}", srv, t


def _run_ps1_hook(payload: str, *, meridian_url: str) -> subprocess.CompletedProcess:
    """Run code_intel_guard.ps1 as a real child process with *payload* on
    stdin and MERIDIAN_URL set in its environment."""
    ps = _powershell_exe()
    env = dict(os.environ)
    env["MERIDIAN_URL"] = meridian_url
    r = subprocess.run(
        [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(_HOOK_PS1)],
        input=payload.encode("utf-8"),
        cwd=str(_REPO),
        capture_output=True,
        timeout=30,
        env=env,
    )
    return subprocess.CompletedProcess(
        r.args,
        r.returncode,
        stdout=(r.stdout or b"").decode("utf-8", "replace"),
        stderr=(r.stderr or b"").decode("utf-8", "replace"),
    )


@_needs_powershell
@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_ps1_hook_fails_open_when_code_intel_disabled(tool):
    url, srv, _t = _start_plain_stub(code_intel_enabled=0)
    try:
        payload = json.dumps({"tool_name": tool, "tool_input": {}})
        r = _run_ps1_hook(payload, meridian_url=url)
        assert r.returncode == 0, f"{tool}: ps1 hook must fail open when disabled"
    finally:
        srv.shutdown()


@_needs_powershell
@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_ps1_hook_fails_open_when_server_unreachable(tool):
    payload = json.dumps({"tool_name": tool, "tool_input": {}})
    r = _run_ps1_hook(payload, meridian_url=_free_url())
    assert r.returncode == 0, f"{tool}: ps1 hook must fail open when server is unreachable"


@_needs_powershell
@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_ps1_hook_fails_open_when_slot_readiness_unreachable(tool):
    """883ce543 (PS1 side): code_intel_enabled=1 but /slot-readiness errors
    (HTTP 500 -> Invoke-RestMethod throws, caught -> $slotResp stays $null).
    Must fail open, not fall through to block."""
    url, srv, _t = _start_plain_stub(code_intel_enabled=1, slot_status=500, slot_body="err")
    try:
        payload = json.dumps({"tool_name": tool, "tool_input": {}})
        r = _run_ps1_hook(payload, meridian_url=url)
        assert r.returncode == 0, (
            f"{tool}: ps1 hook must fail open when slot-readiness errors "
            f"(883ce543 regression). stderr={r.stderr!r}"
        )
    finally:
        srv.shutdown()


@_needs_powershell
@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_ps1_hook_fails_open_when_slot_readiness_malformed(tool):
    """883ce543 (PS1 side): code_intel_enabled=1, /slot-readiness returns 200
    with a non-JSON body (Invoke-RestMethod throws parsing it, caught). Must
    fail open, not fall through to block."""
    url, srv, _t = _start_plain_stub(
        code_intel_enabled=1, slot_status=200, slot_body="not json at all"
    )
    try:
        payload = json.dumps({"tool_name": tool, "tool_input": {}})
        r = _run_ps1_hook(payload, meridian_url=url)
        assert r.returncode == 0, (
            f"{tool}: ps1 hook must fail open when slot-readiness body is "
            f"malformed (883ce543 regression). stderr={r.stderr!r}"
        )
    finally:
        srv.shutdown()


@_needs_powershell
@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_ps1_hook_fails_open_when_slot_readiness_missing_fields(tool):
    """883ce543 (PS1 side): valid JSON (200) but missing ready/has_tunnel
    entirely -- $slotResp.ready and $slotResp.has_tunnel resolve to $null,
    which is neither -eq $true nor -eq $false. Must fail open."""
    url, srv, _t = _start_plain_stub(code_intel_enabled=1, slot_status=200, slot_body="{}")
    try:
        payload = json.dumps({"tool_name": tool, "tool_input": {}})
        r = _run_ps1_hook(payload, meridian_url=url)
        assert r.returncode == 0, (
            f"{tool}: ps1 hook must fail open when ready/has_tunnel are "
            f"missing. stderr={r.stderr!r}"
        )
    finally:
        srv.shutdown()


@_needs_powershell
@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_ps1_hook_fails_open_when_ready_false(tool):
    url, srv, _t = _start_plain_stub(
        code_intel_enabled=1,
        slot_body=json.dumps({"ready": False, "has_tunnel": True}),
    )
    try:
        payload = json.dumps({"tool_name": tool, "tool_input": {}})
        r = _run_ps1_hook(payload, meridian_url=url)
        assert r.returncode == 0, f"{tool}: ps1 hook must fail open when ready=false"
        assert "NOT ready" in r.stderr, "stderr must explain the not-ready fail-open"
    finally:
        srv.shutdown()


@_needs_powershell
@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_ps1_hook_fails_open_when_has_tunnel_false(tool):
    url, srv, _t = _start_plain_stub(
        code_intel_enabled=1,
        slot_body=json.dumps({"ready": True, "has_tunnel": False}),
    )
    try:
        payload = json.dumps({"tool_name": tool, "tool_input": {}})
        r = _run_ps1_hook(payload, meridian_url=url)
        assert r.returncode == 0, f"{tool}: ps1 hook must fail open when has_tunnel=false"
        assert "no tunnel" in r.stderr.lower(), "stderr must explain the no-tunnel fail-open"
    finally:
        srv.shutdown()


@_needs_powershell
@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_ps1_hook_blocks_when_validated_ready_and_tunnel(tool):
    """The ONLY case that should block: positively confirmed ready=true AND
    has_tunnel=true. Mirrors test_hook_blocks_grep_glob_when_code_intel_enabled
    for the .sh hook -- same contract, same message content, different shell."""
    url, srv, _t = _start_plain_stub(
        code_intel_enabled=1,
        slot_body=json.dumps({"ready": True, "has_tunnel": True}),
    )
    try:
        payload = json.dumps({"tool_name": tool, "tool_input": {}})
        r = _run_ps1_hook(payload, meridian_url=url)
        assert r.returncode == 2, f"{tool}: ps1 hook must block when ready+tunnel are confirmed"
        combined = r.stdout + r.stderr
        assert "find_symbol" in combined, "must mention find_symbol as the alternative"
        assert "search_graph" in combined, "must mention search_graph as the alternative"
        assert "aeba8a80" in combined, "must cite the item id"
        assert tool in combined, f"stderr must name the blocked tool ({tool})"
    finally:
        srv.shutdown()


@_needs_powershell
@pytest.mark.parametrize("payload", ["", "not json at all", "{}", '{"foo":"bar"}'])
def test_ps1_hook_fails_open_on_unparseable_payload(payload):
    r = _run_ps1_hook(payload, meridian_url=_free_url())
    assert r.returncode == 0, "ps1 hook must fail open on unparseable/missing stdin"


@_needs_powershell
@pytest.mark.parametrize("tool", ["Bash", "Edit", "Write", "Read", "AskUserQuestion"])
def test_ps1_hook_allows_all_other_tools(tool):
    url, srv, _t = _start_plain_stub(
        code_intel_enabled=1,
        slot_body=json.dumps({"ready": True, "has_tunnel": True}),
    )
    try:
        payload = json.dumps({"tool_name": tool, "tool_input": {}})
        r = _run_ps1_hook(payload, meridian_url=url)
        assert r.returncode == 0, f"{tool} must not be blocked by the code-intel guard"
    finally:
        srv.shutdown()


def test_ps1_hook_is_pure_ascii_and_parses_with_zero_errors():
    """883ce543 gotcha: the Edit tool writes BOM-less UTF-8, and PowerShell 5.1
    reads a BOM-less .ps1 as cp1252, so any non-ASCII byte silently corrupts
    em-dashes/smart-quotes and can break the parser. Verify the real hook file
    is pure ASCII and parses cleanly via PowerShell's own AST parser."""
    raw = _HOOK_PS1.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "code_intel_guard.ps1 must not have a UTF-8 BOM"
    non_ascii = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
    assert not non_ascii, f"code_intel_guard.ps1 has non-ASCII bytes at {non_ascii[:10]}"

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
    assert proc.returncode == 0, f"code_intel_guard.ps1 failed to parse:\n{proc.stdout}\n{proc.stderr}"
    assert "PARSE_OK" in proc.stdout


# ===========================================================================
# a8c0f3b7 -- CODE-INTEL PROSPECTING RECEIPT
#
# The shell-hook tests above block raw Grep/Glob for ONE surface (Claude
# Code's PreToolUse hook) when code-intel is enabled. They do NOT cover a
# Read tool call, a raw `git show` / PowerShell `Get-Content`, or a sub-agent
# spawned outside this MCP connection entirely -- all structurally invisible
# to that hook. The tests below cover the DIFFERENT, complementary mechanism
# in meridian/code_intel_receipt.py: a durable, server-written receipt
# checked at complete_sprint_item time, which can't be evaded just because a
# bypass path exists (see TestStructuralBypassCannotEvadeGate below).
# ===========================================================================


def _cap_receipt(**overrides):
    """A normalized 'code_intel_prospecting' capability for test manifests."""
    base = {
        "id": _cir_mod.CODE_INTEL_CAPABILITY_ID,
        "purpose": "verify semantic code-intel prospecting happened before code edits",
        "required_tools": ["prospect_symbol"],
        "fallback_chain": [],
        "availability_policy": "required",
    }
    base.update(overrides)
    return base


def _inv(**overrides):
    """A live-inventory snapshot (meridian.capability_availability shape)."""
    base = {
        "tunnel_reachable": True,
        "builtin_tools": {"prospect_symbol", "start_session"},
        "plugins": {},
        "stdio_registry": {},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tool classification -- the structural gate deciding which tool calls write
# a receipt. Read/Grep/Bash/etc. must NEVER match, no matter how prefixed --
# that's what makes those bypass paths structurally invisible, not just
# undocumented.
# ---------------------------------------------------------------------------

class TestCodeIntelReceiptToolClassification:
    @pytest.mark.parametrize("name", [
        "prospect_symbol", "search_graph", "codebase__search_graph",
        "find_symbol", "extractor__find_symbol",
        "find_referencing_symbols", "extractor__find_referencing_symbols",
        "search_code_semantic", "trace_path", "get_architecture",
    ])
    def test_recognized_code_intel_tools(self, name):
        assert _cir_mod.is_code_intel_receipt_tool(name)

    @pytest.mark.parametrize("name", [
        "Read", "Grep", "Glob", "Bash", "Write", "Edit", "git show",
        "Get-Content", "filesystem__read_file", "filesystem__write_file",
        "get_sprint_items", "claim_sprint_item", "",
    ])
    def test_non_code_intel_tools_never_match(self, name):
        assert not _cir_mod.is_code_intel_receipt_tool(name)

    def test_bare_tool_name_strips_slot_prefix(self):
        assert _cir_mod.bare_tool_name("codebase__search_graph") == "search_graph"
        assert _cir_mod.bare_tool_name("search_graph") == "search_graph"
        assert _cir_mod.bare_tool_name("") == ""


class TestExtractQueryHint:
    def test_prefers_symbol_key(self):
        assert _cir_mod.extract_query_hint({"symbol": "foo_bar"}) == "foo_bar"

    def test_falls_back_to_query_key(self):
        assert _cir_mod.extract_query_hint({"query": "handle_thing"}) == "handle_thing"

    def test_no_recognized_key_returns_empty(self):
        assert _cir_mod.extract_query_hint({"unrelated": "x"}) == ""

    def test_non_dict_returns_empty(self):
        assert _cir_mod.extract_query_hint(None) == ""


class TestResolveReceiptProjectId:
    def test_prefers_toml_default(self, monkeypatch):
        monkeypatch.setattr(
            "meridian.toml_config.get_default_project_id", lambda: "default-proj-id"
        )
        assert _cir_mod.resolve_receipt_project_id({"project_id": "not-a-uuid"}) == "default-proj-id"

    def test_falls_back_to_uuid_shaped_arg(self, monkeypatch):
        monkeypatch.setattr("meridian.toml_config.get_default_project_id", lambda: None)
        uid = "5787cc92-ba7d-4788-b17c-28ab7938b839"
        assert _cir_mod.resolve_receipt_project_id({"project_id": uid}) == uid

    def test_non_uuid_arg_and_no_default_resolves_none(self, monkeypatch):
        monkeypatch.setattr("meridian.toml_config.get_default_project_id", lambda: None)
        assert _cir_mod.resolve_receipt_project_id({"project_id": "C-Users-repo-slug"}) is None

    def test_missing_args_resolves_none(self, monkeypatch):
        monkeypatch.setattr("meridian.toml_config.get_default_project_id", lambda: None)
        assert _cir_mod.resolve_receipt_project_id(None) is None


# ---------------------------------------------------------------------------
# record_prospect_receipt / find_recent_prospect_receipt -- the durable,
# reused action_audit_log-backed store.
# ---------------------------------------------------------------------------

class TestRecordAndFindReceipt:
    @pytest.mark.asyncio
    async def test_record_writes_durable_action_audit_log_row(self, db):
        project = await db_module.create_project(db, "receipt-write-proj")
        row = await _cir_mod.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="prospect_symbol", query="MyClass",
        )
        assert row is not None
        assert row["event_type"] == _cir_mod.RECEIPT_EVENT_TYPE
        assert row["project_id"] == project["id"]
        assert row["actor"] == "sess-1"
        log = await db_module.get_action_audit_log(
            db, project_id=project["id"], event_type=_cir_mod.RECEIPT_EVENT_TYPE,
        )
        assert len(log) == 1

    @pytest.mark.asyncio
    async def test_record_without_project_id_is_a_noop(self, db):
        row = await _cir_mod.record_prospect_receipt(
            db, tenant_id=None, project_id=None, session_id="sess-1",
            tool_name="prospect_symbol", query="X",
        )
        assert row is None

    @pytest.mark.asyncio
    async def test_find_recent_returns_none_when_nothing_recorded(self, db):
        project = await db_module.create_project(db, "receipt-find-empty-proj")
        found = await _cir_mod.find_recent_prospect_receipt(db, project_id=project["id"])
        assert found is None

    @pytest.mark.asyncio
    async def test_find_recent_returns_recorded_receipt(self, db):
        project = await db_module.create_project(db, "receipt-find-proj")
        await _cir_mod.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="search_graph", query="thing",
        )
        found = await _cir_mod.find_recent_prospect_receipt(db, project_id=project["id"])
        assert found is not None
        assert found["project_id"] == project["id"]

    @pytest.mark.asyncio
    async def test_find_recent_respects_since_freshness_filter(self, db):
        """A receipt recorded before the 'since' floor must not count as
        evidence for the current claim -- mirrors sprint_evidence_guard's
        EVIDENCE_STALE freshness check."""
        project = await db_module.create_project(db, "receipt-stale-proj")
        await _cir_mod.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-1",
            tool_name="search_graph", query="thing",
        )
        found = await _cir_mod.find_recent_prospect_receipt(
            db, project_id=project["id"], since="2999-01-01 00:00:00",
        )
        assert found is None


# ---------------------------------------------------------------------------
# verify_code_intel_prospecting -- the completion-time gate itself.
# ---------------------------------------------------------------------------

class TestVerifyCodeIntelProspecting:
    @pytest.mark.asyncio
    async def test_not_applicable_when_item_has_no_touches_resources(self, db):
        project = await db_module.create_project(db, "verify-no-resources-proj")
        await db_module.set_project_capability_manifest(db, project["id"], [_cap_receipt()])
        item = {"touches_resources": None, "claimed_at": None}
        result = await _cir_mod.verify_code_intel_prospecting(
            db, None, project["id"], item, live_inventory=_inv(),
        )
        assert result["applicable"] is False
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_not_applicable_when_prospect_bypass_set(self, db):
        project = await db_module.create_project(db, "verify-bypass-proj")
        await db_module.set_project_capability_manifest(db, project["id"], [_cap_receipt()])
        item = {
            "touches_resources": '["file:meridian/db/sprint_items.py"]',
            "prospect_bypass": 1,
            "claimed_at": None,
        }
        result = await _cir_mod.verify_code_intel_prospecting(
            db, None, project["id"], item, live_inventory=_inv(),
        )
        assert result["applicable"] is False

    @pytest.mark.asyncio
    async def test_not_applicable_when_no_capability_declared(self, db):
        """No manifest / no matching capability -> zero behavior change."""
        project = await db_module.create_project(db, "verify-no-manifest-proj")
        item = {"touches_resources": '["file:meridian/db/sprint_items.py"]', "claimed_at": None}
        result = await _cir_mod.verify_code_intel_prospecting(
            db, None, project["id"], item, live_inventory=_inv(),
        )
        assert result["applicable"] is False
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_required_and_unavailable_fails_closed(self, db):
        project = await db_module.create_project(db, "verify-required-unavail-proj")
        await db_module.set_project_capability_manifest(
            db, project["id"], [_cap_receipt(availability_policy="required")],
        )
        item = {"touches_resources": '["file:meridian/db/sprint_items.py"]', "claimed_at": None}
        result = await _cir_mod.verify_code_intel_prospecting(
            db, None, project["id"], item,
            live_inventory=_inv(tunnel_reachable=False, builtin_tools=set()),
        )
        assert result["applicable"] is True
        assert result["ok"] is False
        assert result["code"] == "CODE_INTEL_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_degraded_ok_and_unavailable_degrades_instead_of_blocking(self, db):
        project = await db_module.create_project(db, "verify-degraded-unavail-proj")
        await db_module.set_project_capability_manifest(
            db, project["id"], [_cap_receipt(availability_policy="degraded_ok")],
        )
        item = {"touches_resources": '["file:meridian/db/sprint_items.py"]', "claimed_at": None}
        result = await _cir_mod.verify_code_intel_prospecting(
            db, None, project["id"], item,
            live_inventory=_inv(tunnel_reachable=False, builtin_tools=set()),
        )
        assert result["applicable"] is True
        assert result["ok"] is True
        assert result["degraded"] is True
        assert result["warning"]

    @pytest.mark.asyncio
    async def test_required_and_available_but_no_receipt_blocks(self, db):
        project = await db_module.create_project(db, "verify-required-noreceipt-proj")
        await db_module.set_project_capability_manifest(
            db, project["id"], [_cap_receipt(availability_policy="required")],
        )
        item = {"touches_resources": '["file:meridian/db/sprint_items.py"]', "claimed_at": None}
        result = await _cir_mod.verify_code_intel_prospecting(
            db, None, project["id"], item, live_inventory=_inv(),
        )
        assert result["applicable"] is True
        assert result["ok"] is False
        assert result["code"] == "CODE_INTEL_RECEIPT_MISSING"

    @pytest.mark.asyncio
    async def test_required_and_available_with_fresh_receipt_passes(self, db):
        project = await db_module.create_project(db, "verify-required-receipt-proj")
        await db_module.set_project_capability_manifest(
            db, project["id"], [_cap_receipt(availability_policy="required")],
        )
        await _cir_mod.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-x",
            tool_name="prospect_symbol", query="thing",
        )
        item = {"touches_resources": '["file:meridian/db/sprint_items.py"]', "claimed_at": None}
        result = await _cir_mod.verify_code_intel_prospecting(
            db, None, project["id"], item, live_inventory=_inv(),
        )
        assert result["applicable"] is True
        assert result["ok"] is True
        assert result["receipt"] is not None

    @pytest.mark.asyncio
    async def test_stale_receipt_before_claim_does_not_satisfy_required(self, db):
        project = await db_module.create_project(db, "verify-stale-receipt-proj")
        await db_module.set_project_capability_manifest(
            db, project["id"], [_cap_receipt(availability_policy="required")],
        )
        await _cir_mod.record_prospect_receipt(
            db, tenant_id=None, project_id=project["id"], session_id="sess-x",
            tool_name="prospect_symbol", query="thing",
        )
        item = {
            "touches_resources": '["file:meridian/db/sprint_items.py"]',
            "claimed_at": "2999-01-01 00:00:00",
        }
        result = await _cir_mod.verify_code_intel_prospecting(
            db, None, project["id"], item, live_inventory=_inv(),
        )
        assert result["ok"] is False
        assert result["code"] == "CODE_INTEL_RECEIPT_MISSING"

    @pytest.mark.asyncio
    async def test_optional_and_available_but_no_receipt_degrades(self, db):
        project = await db_module.create_project(db, "verify-optional-noreceipt-proj")
        await db_module.set_project_capability_manifest(
            db, project["id"], [_cap_receipt(availability_policy="optional")],
        )
        item = {"touches_resources": '["file:meridian/db/sprint_items.py"]', "claimed_at": None}
        result = await _cir_mod.verify_code_intel_prospecting(
            db, None, project["id"], item, live_inventory=_inv(),
        )
        assert result["ok"] is True
        assert result["degraded"] is True
        assert result["warning"]


class TestRecordProspectReceiptOverride:
    @pytest.mark.asyncio
    async def test_empty_reason_raises(self, db):
        project = await db_module.create_project(db, "override-empty-reason-proj")
        with pytest.raises(ValueError):
            await _cir_mod.record_prospect_receipt_override(
                db, project["id"], "item-1", actor="sess-1", reason="   ",
                check={"code": "CODE_INTEL_RECEIPT_MISSING"},
            )

    @pytest.mark.asyncio
    async def test_valid_override_is_audited(self, db):
        project = await db_module.create_project(db, "override-valid-proj")
        row = await _cir_mod.record_prospect_receipt_override(
            db, project["id"], "item-1", actor="sess-1",
            reason="manually verified via ad-hoc review, acceptable this once",
            check={"code": "CODE_INTEL_RECEIPT_MISSING", "capability": {"id": "code_intel_prospecting"}},
        )
        assert row["event_type"] == _cir_mod.OVERRIDE_EVENT_TYPE
        log = await db_module.get_action_audit_log(
            db, project_id=project["id"], event_type=_cir_mod.OVERRIDE_EVENT_TYPE,
        )
        assert len(log) == 1
        detail = json.loads(log[0]["detail"])
        assert detail["reason"].startswith("manually verified")


# ---------------------------------------------------------------------------
# End-to-end wiring: handle_complete_sprint_item consults
# verify_code_intel_prospecting BEFORE marking an item done.
# ---------------------------------------------------------------------------

async def _make_prospected_item(db, pid, title):
    """A touches_resources item with real durable pointer evidence (passes
    the UNPROSPECTED claim gate) and NO prospect_bypass, so the completion-time
    receipt gate is free to evaluate on its own merits."""
    item = await db_module.add_sprint_item(
        db, pid, "v1", title,
        touches_resources=["file:meridian/db/sprint_items.py"],
    )
    await db_module.add_sprint_item_pointer(
        db, pid, item["id"], "code",
        [{
            "uri": "file:meridian/db/sprint_items.py",
            "selector": {"type": "symbol", "qualified_name": "meridian.db.sprint_items.claim_sprint_item"},
        }],
    )
    claimed = await db_module.claim_sprint_item(db, pid, item["id"])
    assert not (isinstance(claimed, dict) and claimed.get("blocked")), claimed
    return item


class TestCompleteSprintItemCodeIntelGate:
    @pytest.mark.asyncio
    async def test_no_manifest_declared_zero_behavior_change(self, db):
        project = await db_module.create_project(db, "e2e-no-manifest-proj")
        pid = project["id"]
        sess = await db_module.register_session(db, pid, "e2e-sess")
        item = await _make_prospected_item(db, pid, "Touches a real file")
        result = await _st_mod.handle_complete_sprint_item(
            {"project_id": pid, "item_id": item["id"], "session_id": sess["id"]},
            db, "/tmp/meridian-test", None, None,
        )
        assert result.get("status") == "done", result
        assert "code_intel_receipt_warning" not in result
        assert "code_intel_receipt" not in result

    @pytest.mark.asyncio
    async def test_required_capability_declared_and_missing_receipt_blocks_completion(self, db):
        project = await db_module.create_project(db, "e2e-blocked-proj")
        pid = project["id"]
        sess = await db_module.register_session(db, pid, "e2e-sess-2")
        await db_module.set_project_capability_manifest(
            db, pid, [_cap_receipt(availability_policy="required")],
        )
        item = await _make_prospected_item(db, pid, "Needs prospecting")
        # prospect_symbol is a native (always-available) builtin tool -> the
        # capability's availability status resolves "available"; no receipt
        # was ever recorded for this project.
        result = await _st_mod.handle_complete_sprint_item(
            {"project_id": pid, "item_id": item["id"], "session_id": sess["id"]},
            db, "/tmp/meridian-test", None, None,
        )
        assert result.get("error") == "CODE_INTEL_RECEIPT_MISSING", result
        fresh = await db_module.get_sprint_item(db, item["id"])
        assert fresh["status"] != "done"

    @pytest.mark.asyncio
    async def test_receipt_present_allows_completion(self, db):
        project = await db_module.create_project(db, "e2e-allowed-proj")
        pid = project["id"]
        sess = await db_module.register_session(db, pid, "e2e-sess-3")
        await db_module.set_project_capability_manifest(
            db, pid, [_cap_receipt(availability_policy="required")],
        )
        item = await _make_prospected_item(db, pid, "Prospected properly")
        # Simulate the receipt a real prospect_symbol/search_graph call would
        # have written via meridian/mcp/handler.py's dispatch wiring.
        await _cir_mod.record_prospect_receipt(
            db, tenant_id=None, project_id=pid, session_id=sess["id"],
            tool_name="prospect_symbol", query="claim_sprint_item",
        )
        result = await _st_mod.handle_complete_sprint_item(
            {"project_id": pid, "item_id": item["id"], "session_id": sess["id"]},
            db, "/tmp/meridian-test", None, None,
        )
        assert result.get("status") == "done", result
        assert result.get("code_intel_receipt") is not None

    @pytest.mark.asyncio
    async def test_override_with_reason_completes_and_is_audited(self, db):
        project = await db_module.create_project(db, "e2e-override-proj")
        pid = project["id"]
        sess = await db_module.register_session(db, pid, "e2e-sess-4")
        await db_module.set_project_capability_manifest(
            db, pid, [_cap_receipt(availability_policy="required")],
        )
        item = await _make_prospected_item(db, pid, "Override this one")
        result = await _st_mod.handle_complete_sprint_item(
            {
                "project_id": pid, "item_id": item["id"], "session_id": sess["id"],
                "override_code_intel_receipt": True,
                "override_reason": "manually verified via structured code review",
            },
            db, "/tmp/meridian-test", None, None,
        )
        assert result.get("status") == "done", result
        assert result.get("code_intel_receipt_override") is not None
        log = await db_module.get_action_audit_log(
            db, project_id=pid, event_type=_cir_mod.OVERRIDE_EVENT_TYPE,
        )
        assert len(log) == 1

    @pytest.mark.asyncio
    async def test_override_without_reason_is_refused(self, db):
        project = await db_module.create_project(db, "e2e-noreason-proj")
        pid = project["id"]
        sess = await db_module.register_session(db, pid, "e2e-sess-5")
        await db_module.set_project_capability_manifest(
            db, pid, [_cap_receipt(availability_policy="required")],
        )
        item = await _make_prospected_item(db, pid, "No reason override")
        result = await _st_mod.handle_complete_sprint_item(
            {
                "project_id": pid, "item_id": item["id"], "session_id": sess["id"],
                "override_code_intel_receipt": True,
            },
            db, "/tmp/meridian-test", None, None,
        )
        assert result.get("error") == "CODE_INTEL_RECEIPT_MISSING", result
        fresh = await db_module.get_sprint_item(db, item["id"])
        assert fresh["status"] != "done"


class TestStructuralBypassCannotEvadeGate:
    """The core 'cannot silently evade' property (acceptance criterion #5):
    a Read-tool-only pass, a raw git show/Get-Content read, or a sub-agent
    spawned outside this MCP connection never reaches record_prospect_receipt
    (none of those tool names satisfy is_code_intel_receipt_tool), so
    completion is still correctly blocked -- the check is structural, not a
    trust of the calling agent's self-report."""

    @pytest.mark.asyncio
    async def test_read_grep_bash_bypass_never_creates_a_receipt(self, db):
        project = await db_module.create_project(db, "e2e-bypass-proj")
        pid = project["id"]
        sess = await db_module.register_session(db, pid, "e2e-sess-6")
        await db_module.set_project_capability_manifest(
            db, pid, [_cap_receipt(availability_policy="required")],
        )
        item = await _make_prospected_item(db, pid, "Bypass attempt")
        for fake_tool in ("Read", "Grep", "Bash", "git show", "Get-Content"):
            assert not _cir_mod.is_code_intel_receipt_tool(fake_tool)
        result = await _st_mod.handle_complete_sprint_item(
            {"project_id": pid, "item_id": item["id"], "session_id": sess["id"]},
            db, "/tmp/meridian-test", None, None,
        )
        assert result.get("error") == "CODE_INTEL_RECEIPT_MISSING", result

    @pytest.mark.asyncio
    async def test_subagent_under_different_session_id_produces_no_receipt_either(self, db):
        """A sub-agent that DOES call a code-intel tool, but never routes it
        through THIS Meridian connection (e.g. a locally-configured, separate
        Serena/codebase-memory-mcp server the sub-agent talks to directly),
        writes no row anywhere this project can see -- verify_code_intel_
        prospecting still finds nothing and blocks."""
        project = await db_module.create_project(db, "e2e-subagent-proj")
        pid = project["id"]
        sess = await db_module.register_session(db, pid, "e2e-sess-7")
        await db_module.set_project_capability_manifest(
            db, pid, [_cap_receipt(availability_policy="required")],
        )
        item = await _make_prospected_item(db, pid, "Subagent bypass attempt")
        # No record_prospect_receipt call happens anywhere in this test --
        # simulating a sub-agent that never touched this MCP connection.
        result = await _st_mod.handle_complete_sprint_item(
            {"project_id": pid, "item_id": item["id"], "session_id": sess["id"]},
            db, "/tmp/meridian-test", None, None,
        )
        assert result.get("error") == "CODE_INTEL_RECEIPT_MISSING", result


# ---------------------------------------------------------------------------
# DOCS (AGENTS.md) vs ENFORCEMENT: acceptance criterion #6 keeps these
# distinct. This section verifies the WORDS only -- runtime behavior is
# covered exhaustively above.
# ---------------------------------------------------------------------------

class TestAgentsMdDocumentsReceiptMechanism:
    def test_agents_md_documents_capability_opt_in(self):
        text = _REPO.joinpath("AGENTS.md").read_text(encoding="utf-8")
        assert "code_intel_prospecting" in text
        assert "set_capability_manifest" in text

    def test_agents_md_names_the_item_id(self):
        text = _REPO.joinpath("AGENTS.md").read_text(encoding="utf-8")
        assert "a8c0f3b7" in text
