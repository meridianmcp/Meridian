"""189fde6b — red-team leads RT-LEAD-004 and RT-LEAD-003 regression tests.

RT-LEAD-004: the hosted server can send a ``run_cmd`` control message to any
connected tunnel client. The client must not spawn it unless the local user has
consented in the client's own environment (MERIDIAN_TUNNEL_ALLOW_RUN_CMD).

RT-LEAD-003: the Overleaf CLI ``write`` subcommand edits a live document and must
refuse to run without an interactive terminal. The gate lives in the engine's
TypeScript source; the static check below pins that it runs inside ``writeField``
before the saved session cookie is loaded.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest
import websockets

from meridian import tunnel_client as tc

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_TS = REPO_ROOT / "extensions" / "meridian-latex" / "engine" / "src" / "cli.ts"
CONSENT_ENV = "MERIDIAN_TUNNEL_ALLOW_RUN_CMD"


class _RecordingWS:
    """In-process fake tunnel socket: yields queued frames and records sends."""

    def __init__(self, messages):
        self._msgs = list(messages)
        self.sent: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._msgs:
            raise StopAsyncIteration
        return self._msgs.pop(0)

    async def send(self, data):
        self.sent.append(json.loads(data))


class _FakeHttpClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _drive_fs_socket(monkeypatch, frames):
    """Feed frames through the real FS-slot message loop; return what the client sent."""
    ws = _RecordingWS(frames)
    monkeypatch.setattr(websockets, "connect", lambda url, **kw: ws)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeHttpClient())
    proxy = tc.SlotProxy(["x"], 8808, "fs")
    asyncio.run(tc._run_connection_lazy("wss://x/tunnel/t", proxy, "fs"))
    return ws.sent


def _run_cmd_frame(req_id: str, marker: Path) -> str:
    # Inert command: writes one marker file and exits. Sent as a token list, so no shell runs.
    code = f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')"
    return json.dumps(
        {"type": "run_cmd", "id": req_id, "cmd": [sys.executable, "-c", code], "cwd": ""}
    )


def _run_cmd_results(sent):
    return [m for m in sent if m.get("type") == "run_cmd_result"]


def test_run_cmd_refused_without_local_consent(monkeypatch, tmp_path):
    monkeypatch.delenv(CONSENT_ENV, raising=False)
    marker = tmp_path / "ran.txt"

    async def _must_not_spawn(*args, **kwargs):
        raise AssertionError("refused run_cmd must never reach _handle_run_cmd")

    monkeypatch.setattr(tc, "_handle_run_cmd", _must_not_spawn)

    sent = _drive_fs_socket(monkeypatch, [_run_cmd_frame("r1", marker)])

    assert not marker.exists()
    results = _run_cmd_results(sent)
    assert len(results) == 1
    assert results[0]["id"] == "r1"
    assert results[0]["status"] == "error"
    assert results[0]["refused"] is True
    assert results[0]["exit_code"] is None
    assert CONSENT_ENV in results[0]["message"]


@pytest.mark.parametrize("value", ["", "0", "false", "no", "2"])
def test_run_cmd_refused_for_non_affirmative_consent_values(monkeypatch, tmp_path, value):
    monkeypatch.setenv(CONSENT_ENV, value)
    marker = tmp_path / "ran.txt"

    sent = _drive_fs_socket(monkeypatch, [_run_cmd_frame("r3", marker)])

    assert not marker.exists()
    results = _run_cmd_results(sent)
    assert len(results) == 1
    assert results[0]["refused"] is True


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_run_cmd_runs_with_explicit_local_consent(monkeypatch, tmp_path, value):
    monkeypatch.setenv(CONSENT_ENV, value)
    marker = tmp_path / "ran.txt"

    sent = _drive_fs_socket(monkeypatch, [_run_cmd_frame("r2", marker)])

    assert marker.read_text(encoding="utf-8") == "ran"
    results = _run_cmd_results(sent)
    assert len(results) == 1
    assert results[0]["id"] == "r2"
    assert results[0]["status"] == "ok"
    assert results[0]["exit_code"] == 0
    assert "refused" not in results[0]


def _write_field_body() -> str:
    text = CLI_TS.read_text(encoding="utf-8")
    start = text.index("async function writeField(")
    end = text.index("interface PullDocArgs", start)
    return text[start:end]


def test_overleaf_cli_write_refuses_without_tty_before_loading_session():
    body = _write_field_body()
    gate_start = body.index("if (!process.stdin.isTTY) {")
    cookie_load = body.index("const cookie = loadSavedCookie();")
    assert gate_start < cookie_load, "TTY gate must run before the saved session is loaded"
    assert "process.exit(1)" in body[gate_start:cookie_load]
