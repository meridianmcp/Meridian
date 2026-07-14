"""81b10dec -- tests for the slot-readiness proactive warmup + enforcement.

Covers:
1. GET /projects/{id}/slot-readiness endpoint behaviour (no tunnel, with tunnel mock).
2. get_tenant_id_for_project DB helper.
3. The code_intel_guard.sh bash hook: the new slot-readiness path.

Note on Windows bash hook behaviour: on Windows, bash subprocess launched via
Python subprocess.run does NOT inherit stdin to $(cat) inside command substitution.
The hook exits at ``[ -z "$payload" ] && exit 0`` (exit 0, no output). This means
hook tests that check for visible fallback messages require non-Windows bash.
Tests that only assert exit 0 pass on Windows via that early-exit path.

The existing enabled=0 / enabled=1 hook tests live in test_code_intel_guard.py
and must not be modified -- this file adds the new readiness-gate tests only.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from meridian import db as db_module

_REPO = Path(__file__).resolve().parent.parent
_HOOK_SH = _REPO / ".claude" / "hooks" / "code_intel_guard.sh"

_needs_bash = pytest.mark.skipif(
    not _HOOK_SH.exists() or shutil.which("bash") is None,
    reason="code_intel_guard.sh or bash unavailable",
)

# On Windows, bash subprocess from Python subprocess.run does NOT inherit stdin
# to $(cat) in command substitution — the hook exits at empty-payload check.
# Tests that verify visible stderr content from the slot-readiness logic require
# a Linux-style bash environment.
_needs_bash_stdin = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "bash hook $(cat) does not inherit Python subprocess stdin on Windows "
        "(pre-existing limitation; hook logic is correct but not testable via "
        "subprocess on this platform)"
    ),
)

# Windows NTSTATUS crash exit codes seen under heavy xdist (-n auto) contention.
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
# Stub HTTP server that serves /settings AND /slot-readiness
# ---------------------------------------------------------------------------

class _DualStubHandler(http.server.BaseHTTPRequestHandler):
    """Serves /projects/*/settings and /projects/*/slot-readiness."""

    code_intel_enabled: int = 1
    slot_ready: bool = True
    has_tunnel: bool = True

    def do_GET(self):  # noqa: N802
        if "/slot-readiness" in self.path:
            body = json.dumps({
                "slot": "code",
                "ready": self.__class__.slot_ready,
                "has_tunnel": self.__class__.has_tunnel,
                "probed": True,
            }).encode()
        else:
            body = json.dumps({
                "code_intel_enabled": self.__class__.code_intel_enabled
            }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args, **kwargs):
        pass


def _start_dual_stub(
    *, code_intel_enabled: int = 1, slot_ready: bool = True, has_tunnel: bool = True
) -> tuple[int, threading.Thread]:
    _DualStubHandler.code_intel_enabled = code_intel_enabled
    _DualStubHandler.slot_ready = slot_ready
    _DualStubHandler.has_tunnel = has_tunnel
    server = http.server.HTTPServer(("127.0.0.1", 0), _DualStubHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return port, t


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_hook_once(
    payload: str, *, meridian_url: str | None = None
) -> subprocess.CompletedProcess:
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
    last: subprocess.CompletedProcess | None = None
    for _ in range(3):
        try:
            last = _run_hook_once(payload, meridian_url=meridian_url)
        except subprocess.TimeoutExpired:
            continue
        if last.returncode in _WIN_CRASH_CODES:
            continue
        return last
    assert last is not None
    return last


# ---------------------------------------------------------------------------
# DB helper: get_tenant_id_for_project
# ---------------------------------------------------------------------------

async def test_get_tenant_id_for_project_no_tenant(db):
    """Returns None when the project has no creator_human_id."""
    p = await db_module.create_project(db, "no-creator")
    result = await db_module.get_tenant_id_for_project(db, p["id"])
    # The in-memory test DB has no tenants table entries by default.
    assert result is None


async def test_get_tenant_id_for_project_unknown_project(db):
    """Returns None for a non-existent project id."""
    result = await db_module.get_tenant_id_for_project(db, "nonexistent-id")
    assert result is None


async def test_get_tenant_id_for_project_with_tenant(db):
    """Returns the tenant id when creator_human_id matches a tenant email."""
    # Create a tenant row.
    tenant = await db_module.upsert_tenant(db, "owner@example.com")
    tenant_id = tenant["id"]

    # Create a project with that email as creator.
    p = await db_module.create_project(db, "owned-project", human_id="owner@example.com")
    result = await db_module.get_tenant_id_for_project(db, p["id"])
    assert result == tenant_id


# ---------------------------------------------------------------------------
# HTTP endpoint: GET /projects/{id}/slot-readiness
# ---------------------------------------------------------------------------

def test_slot_readiness_project_not_found(client):
    """Returns 404 for a non-existent project."""
    r = client.get("/projects/nonexistent-project-id/slot-readiness")
    assert r.status_code == 404


def test_slot_readiness_no_tunnel(client):
    """When there is no tunnel (self-hosted), returns ready=true, has_tunnel=false."""
    db = client.app.state.db
    p = asyncio.run(db_module.create_project(db, "no-tunnel-proj"))
    r = client.get(f"/projects/{p['id']}/slot-readiness")
    assert r.status_code == 200
    body = r.json()
    assert body["slot"] == "code"
    # No tunnel => fail-open (ready=True, has_tunnel=False).
    assert body["has_tunnel"] is False
    assert body["ready"] is True


def test_slot_readiness_with_active_tunnel_ready(client):
    """When the code slot is active and ready, returns ready=true."""
    db = client.app.state.db
    p = asyncio.run(db_module.create_project(db, "tunnel-ready-proj"))

    # Patch the tunnel module so has_active_tunnel=True and _fetch_slot_tools
    # returns a non-empty tools list (slot is ready).
    async def _fake_fetch(tenant_id, label):
        return label, [{"name": "find_symbol"}]

    import meridian.routes.tunnel as _tunnel_mod
    with (
        patch.object(_tunnel_mod, "has_active_tunnel", return_value=True),
        patch.object(_tunnel_mod, "_fetch_slot_tools", side_effect=_fake_fetch),
    ):
        # Also patch get_tenant_id_for_project to return a fake tenant id.
        with patch.object(db_module, "get_tenant_id_for_project",
                          new=AsyncMock(return_value="fake-tenant-id")):
            r = client.get(f"/projects/{p['id']}/slot-readiness")

    assert r.status_code == 200
    body = r.json()
    assert body["slot"] == "code"
    assert body["has_tunnel"] is True
    assert body["ready"] is True
    assert body["probed"] is True


def test_slot_readiness_with_active_tunnel_cold(client):
    """When the code slot returns 0 tools (cold), returns ready=false + fallback_reason."""
    db = client.app.state.db
    p = asyncio.run(db_module.create_project(db, "tunnel-cold-proj"))

    async def _fake_fetch_empty(tenant_id, label):
        return label, []  # cold slot: no tools

    import meridian.routes.tunnel as _tunnel_mod
    with (
        patch.object(_tunnel_mod, "has_active_tunnel", return_value=True),
        patch.object(_tunnel_mod, "_fetch_slot_tools", side_effect=_fake_fetch_empty),
    ):
        with patch.object(db_module, "get_tenant_id_for_project",
                          new=AsyncMock(return_value="fake-tenant-id")):
            r = client.get(f"/projects/{p['id']}/slot-readiness")

    assert r.status_code == 200
    body = r.json()
    assert body["slot"] == "code"
    assert body["has_tunnel"] is True
    assert body["ready"] is False
    assert body["probed"] is True
    # Must carry a visible fallback reason.
    assert "fallback_reason" in body
    assert "81b10dec" in body["fallback_reason"]


def test_slot_readiness_probe_exception_fails_open(client):
    """If the probe raises, the endpoint fails open (ready=true) and logs a reason."""
    db = client.app.state.db
    p = asyncio.run(db_module.create_project(db, "probe-exc-proj"))

    async def _raise(*args, **kwargs):
        raise RuntimeError("simulated probe failure")

    import meridian.routes.tunnel as _tunnel_mod
    with (
        patch.object(_tunnel_mod, "has_active_tunnel", return_value=True),
        patch.object(_tunnel_mod, "_fetch_slot_tools", side_effect=_raise),
    ):
        with patch.object(db_module, "get_tenant_id_for_project",
                          new=AsyncMock(return_value="fake-tenant-id")):
            r = client.get(f"/projects/{p['id']}/slot-readiness")

    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True  # fail-open
    assert body["probed"] is True
    assert "fallback_reason" in body
    assert "81b10dec" in body["fallback_reason"]


# ---------------------------------------------------------------------------
# Hook integration tests: slot-readiness path
# ---------------------------------------------------------------------------

@_needs_bash
@_needs_bash_stdin
@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_hook_fails_open_with_visible_log_when_slot_not_ready(tool):
    """When enabled=1 but slot ready=false after retry, exits 0 with a visible warning.

    Requires non-Windows bash (stdin inheritance via subprocess is broken on Windows
    for bash $(cat) -- the hook exits at empty-payload check instead).
    """
    port, _ = _start_dual_stub(code_intel_enabled=1, slot_ready=False, has_tunnel=True)
    payload = json.dumps({"tool_name": tool, "tool_input": {}})
    r = _run_hook(payload, meridian_url=f"http://127.0.0.1:{port}")
    assert r.returncode == 0, f"{tool}: must fail open when slot is not ready"
    # The fallback must be VISIBLE (logged to stderr), not silent.
    assert "81b10dec" in r.stderr, "stderr must cite the item id (81b10dec)"
    assert "slot" in r.stderr.lower() or "ready" in r.stderr.lower(), (
        "stderr must mention slot readiness"
    )


@_needs_bash
@_needs_bash_stdin
@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_hook_fails_open_with_visible_log_when_no_tunnel(tool):
    """When enabled=1 but has_tunnel=false, exits 0 with a visible tunnel warning.

    Requires non-Windows bash (same stdin limitation as above).
    """
    port, _ = _start_dual_stub(code_intel_enabled=1, slot_ready=True, has_tunnel=False)
    payload = json.dumps({"tool_name": tool, "tool_input": {}})
    r = _run_hook(payload, meridian_url=f"http://127.0.0.1:{port}")
    assert r.returncode == 0, f"{tool}: must fail open when no tunnel is connected"
    assert "81b10dec" in r.stderr, "stderr must cite the item id (81b10dec)"
    assert "tunnel" in r.stderr.lower(), "stderr must mention tunnel"


@_needs_bash
@_needs_bash_stdin
@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_hook_blocks_when_slot_ready(tool):
    """When enabled=1 AND slot ready=true with tunnel, exits 2 and redirects.

    Requires non-Windows bash (same stdin limitation as above).
    """
    port, _ = _start_dual_stub(code_intel_enabled=1, slot_ready=True, has_tunnel=True)
    payload = json.dumps({"tool_name": tool, "tool_input": {}})
    r = _run_hook(payload, meridian_url=f"http://127.0.0.1:{port}")
    assert r.returncode == 2, f"{tool}: must block when slot is ready and enabled"
    assert "aeba8a80" in r.stderr, "blocking message must cite aeba8a80"
    assert "find_symbol" in r.stderr


@_needs_bash
@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_hook_still_fails_open_when_disabled(tool):
    """Existing contract: code_intel_enabled=0 still exits 0 (unchanged, no slot probe).

    This test passes on Windows too (hook exits at empty-payload check with exit 0).
    """
    port, _ = _start_dual_stub(code_intel_enabled=0, slot_ready=True, has_tunnel=True)
    payload = json.dumps({"tool_name": tool, "tool_input": {}})
    r = _run_hook(payload, meridian_url=f"http://127.0.0.1:{port}")
    assert r.returncode == 0, f"{tool}: disabled guard must still fail open"
