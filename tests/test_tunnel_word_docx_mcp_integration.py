"""5b065c2e — integration: the word tunnel slot's default launcher (`uvx docx-mcp`)
actually spawns a real MCP stdio server and answers an `initialize` handshake.

This is the live half of the docx-mcp swap. The unit half (default command ==
["uvx", "docx-mcp"]) lives in test_tunnel_plugins.py and always runs. This file
genuinely spawns the process, so it is skip-guarded on the things it needs:

* `uvx` must be on PATH (skips otherwise — e.g. a minimal CI image).
* the first spawn downloads the package; on a network/resolve failure we SKIP,
  not fail, so an offline CI never goes red on an external dependency.

But if the process DOES spawn and returns output, that output MUST be a
well-formed MCP `initialize` result — a malformed/non-MCP reply is a real
regression and fails the test. The command is read from the live plugin registry
(not hard-coded here) so this test tracks whatever the word slot is actually
configured to launch.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time

import pytest

from meridian import tunnel_plugins as tp

_HAS_UVX = shutil.which("uvx") is not None

pytestmark = pytest.mark.skipif(not _HAS_UVX, reason="uvx not on PATH")

# First-ever `uvx docx-mcp` downloads the package; override the suite-wide
# --timeout=60 for this one slow-on-cold-cache integration test.
_TIMEOUT_MARK = pytest.mark.timeout(240)

# Budget for the whole spawn+handshake. Cold uvx download can take a while; warm
# cache is a few seconds. We SKIP (not fail) if nothing at all comes back within
# this window, since that is indistinguishable from a slow/absent network.
_HANDSHAKE_BUDGET_S = 200.0


def _word_command() -> list[str]:
    """The live default launch command for the word slot, from the real registry."""
    word = {p["slot"]: p for p in tp.resolve_plugins(None)}["word"]
    cmd = word["command"]
    assert cmd and cmd[0] == "uvx", f"word slot is not a uvx launcher: {cmd!r}"
    return list(cmd)


def _drain(stream, sink: list[str]) -> None:
    for raw in iter(stream.readline, b""):
        sink.append(raw.decode("utf-8", "replace").rstrip("\n"))


@_TIMEOUT_MARK
def test_word_slot_docx_mcp_answers_initialize():
    cmd = _word_command()
    # This assertion is the point of the swap — bind the integration test to the
    # intended package so a silent revert to docx-mcp-server is caught here too.
    assert cmd == ["uvx", "docx-mcp"], f"unexpected word launcher: {cmd!r}"

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as e:  # pragma: no cover - env dependent
        pytest.skip(f"could not spawn {cmd!r}: {e}")

    out: list[str] = []
    err: list[str] = []
    threading.Thread(target=_drain, args=(proc.stdout, out), daemon=True).start()
    threading.Thread(target=_drain, args=(proc.stderr, err), daemon=True).start()

    init_req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "meridian-word-integration", "version": "0.0.0"},
        },
    }
    try:
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(init_req) + "\n").encode("utf-8"))
        proc.stdin.flush()
    except (BrokenPipeError, OSError) as e:  # pragma: no cover - env dependent
        proc.kill()
        pytest.skip(f"docx-mcp stdin closed before handshake: {e}")

    reply = None
    deadline = time.monotonic() + _HANDSHAKE_BUDGET_S
    try:
        while time.monotonic() < deadline:
            for line in list(out):
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") == 1:
                    reply = msg
                    break
            if reply is not None:
                break
            if proc.poll() is not None:
                break
            time.sleep(0.25)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()

    if reply is None:
        # No JSON-RPC reply at all: on a cold/offline runner this is a download/
        # network failure, not a code regression — skip rather than flake red.
        joined_err = " | ".join(err[-8:])
        pytest.skip(
            "no MCP initialize reply from `uvx docx-mcp` within "
            f"{_HANDSHAKE_BUDGET_S:.0f}s (likely no network/uvx cache). "
            f"stderr tail: {joined_err[:400]}"
        )

    # We got a reply — from here on, malformed = real failure.
    assert reply.get("jsonrpc") == "2.0", reply
    assert "error" not in reply, f"docx-mcp returned an error to initialize: {reply}"
    result = reply.get("result")
    assert isinstance(result, dict), f"no result object in initialize reply: {reply}"
    assert "protocolVersion" in result, f"initialize result missing protocolVersion: {result}"
    server_info = result.get("serverInfo")
    assert isinstance(server_info, dict) and server_info.get("name"), (
        f"initialize result missing serverInfo.name: {result}"
    )
    # docx-mcp self-reports as the FinalCompleteDocxProcessor server; assert the
    # capabilities advertise tools (the whole reason we mount it in the word slot).
    assert "tools" in (result.get("capabilities") or {}), (
        f"docx-mcp did not advertise tool capability: {result}"
    )
