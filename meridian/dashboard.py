"""Dashboard surface for Meridian.

Four pieces:

* :class:`WebSocketBroadcaster` — bridges the in-process pub/sub in
  :mod:`meridian.db` to live WebSocket clients. One instance lives on
  ``app.state.ws_broadcaster``.
* :func:`stream_anthropic_chat` — async generator that proxies the
  Anthropic streaming API directly. Bills API credits, needs an API
  key. Kept as the "API mode" option.
* :func:`stream_claude_cli_chat` — async generator that shells out to
  the ``claude`` CLI binary and streams its stdout back as SSE. Uses
  the OAuth token already on disk (``~/.claude/.credentials.json`` via
  the Claude Code login), so this draws from the user's Max-plan
  allowance instead of metered API credits. This is the default mode
  for the dashboard chat in v0.3.0.
* :data:`DASHBOARD_HTML` — the entire single-file dashboard, served by
  ``GET /dashboard``. No build step, no external assets except the IBM
  Plex Mono font loaded from Google Fonts.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import WebSocket

from . import db as db_module


# ---------------------------------------------------------------------------
# OAuth / API-key auth helpers
# ---------------------------------------------------------------------------


def load_oauth_token() -> str | None:
    """Read the Claude Max OAuth access token from ~/.claude/.credentials.json.

    Returns the access token string, or None if the file is absent, unreadable,
    or does not contain a claudeAiOauth.accessToken entry. Never raises.
    """
    creds_path = Path.home() / ".claude" / ".credentials.json"
    try:
        data = json.loads(creds_path.read_text(encoding="utf-8"))
        oauth = data.get("claudeAiOauth")
        if isinstance(oauth, dict):
            token = oauth.get("accessToken")
            if token and isinstance(token, str):
                return token
    except Exception:  # noqa: BLE001
        pass
    return None


def get_auth_token() -> tuple[str | None, str | None]:
    """Return ``(token, method)`` for authenticating to the Anthropic API.

    Tries OAuth first (from ~/.claude/.credentials.json), then falls back to
    the ``ANTHROPIC_API_KEY`` environment variable. Returns ``(None, None)``
    when neither is available.

    ``method`` is one of ``"oauth"``, ``"api_key"``, or ``None``.
    """
    token = load_oauth_token()
    if token:
        return token, "oauth"
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return api_key, "api_key"
    return None, None


# ---------------------------------------------------------------------------
# WebSocket broadcaster
# ---------------------------------------------------------------------------


class WebSocketBroadcaster:
    """Forward task-log pub/sub events to subscribed WebSocket clients.

    One :class:`asyncio.Queue` is registered per WebSocket via
    :func:`meridian.db.subscribe_tasks`. A pump task drains the queue and
    writes JSON frames to the socket. The broadcaster is created once at
    app startup and torn down on shutdown.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    async def serve(self, ws: WebSocket, project_id: str) -> None:
        """Accept a WebSocket and pipe task events to it until it closes.

        Reads from the socket are drained on a separate task so we notice
        when the client disconnects. We don't act on inbound messages
        (the dashboard pushes via HTTP, not WS) but we have to read them
        for the close handshake to land.
        """
        await ws.accept()
        queue = db_module.subscribe_tasks(project_id)

        async def reader() -> None:
            try:
                while True:
                    await ws.receive_text()
            except Exception:
                pass

        reader_task = asyncio.create_task(reader())
        try:
            while True:
                event = await queue.get()
                try:
                    await ws.send_text(json.dumps(event, default=str))
                except Exception:
                    break
        finally:
            reader_task.cancel()
            db_module.unsubscribe_tasks(project_id, queue)
            try:
                await ws.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Anthropic chat proxy (Server-Sent Events)
# ---------------------------------------------------------------------------


async def stream_anthropic_chat(
    messages: list[dict[str, str]],
    system_prompt: str | None,
    model: str,
    max_tokens: int,
) -> AsyncIterator[bytes]:
    """Yield Server-Sent-Event lines for an Anthropic streaming response.

    Tries OAuth from ~/.claude/.credentials.json first, then falls back to
    ``ANTHROPIC_API_KEY``. The token never reaches the browser. Each text
    delta is emitted as a ``data: {...}`` line; a terminating
    ``data: [DONE]`` line is sent on success or a single
    ``data: {"error": "..."}`` line on failure.
    """
    token, method = get_auth_token()
    if not token:
        payload = json.dumps(
            {"error": "No auth configured: set ANTHROPIC_API_KEY or connect Claude Max"}
        )
        yield f"data: {payload}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"
        return

    # Import the SDK lazily so test runs that mock this function don't
    # pay the import cost.
    try:
        from anthropic import AsyncAnthropic
    except ImportError as exc:
        payload = json.dumps(
            {"error": f"anthropic SDK not installed: {exc}"}
        )
        yield f"data: {payload}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"
        return

    if method == "oauth":
        client = AsyncAnthropic(auth_token=token)
    else:
        client = AsyncAnthropic(api_key=token)

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system_prompt:
        kwargs["system"] = system_prompt

    try:
        async with client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                payload = json.dumps({"delta": text})
                yield f"data: {payload}\n\n".encode("utf-8")
    except Exception as exc:  # noqa: BLE001 — surface to browser
        payload = json.dumps(
            {"error": f"{type(exc).__name__}: {exc}"}
        )
        yield f"data: {payload}\n\n".encode("utf-8")

    yield b"data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Claude CLI chat proxy (uses Max plan, no API key)
# ---------------------------------------------------------------------------


def _default_claude_cli_argv() -> list[str]:
    """Resolve the argv prefix for the ``claude`` CLI binary.

    Override via ``MERIDIAN_CLAUDE_CLI`` (shell-split). Tests use this to
    substitute a Python stub so the suite never actually calls Anthropic.
    The prompt is appended as a single argument after the prefix.
    """
    env = os.environ.get("MERIDIAN_CLAUDE_CLI")
    if env:
        return shlex.split(env)
    windows_claude = Path.home() / "AppData" / "Roaming" / "npm" / "claude.cmd"
    if windows_claude.exists():
        return ["cmd", "/c", str(windows_claude), "-p"]
    return ["claude", "-p"]


def _format_cli_prompt(
    messages: list[dict[str, str]], system_prompt: str | None
) -> str:
    """Flatten a chat history into a single prompt string for ``claude -p``.

    The CLI takes one prompt, not a structured conversation, so we
    serialise the system prompt + each prior turn as labelled blocks.
    The model has been trained on this conventional format and responds
    to the last ``User:`` turn.
    """
    parts: list[str] = []
    if system_prompt:
        parts.append(f"System:\n{system_prompt.strip()}")
    for msg in messages:
        role = "User" if msg.get("role") == "user" else "Assistant"
        content = (msg.get("content") or "").strip()
        if content:
            parts.append(f"{role}:\n{content}")
    return "\n\n".join(parts)


async def stream_claude_cli_chat(
    messages: list[dict[str, str]],
    system_prompt: str | None,
    model: str,
    max_tokens: int,
) -> AsyncIterator[bytes]:
    """Spawn the ``claude`` CLI and stream its text output back as SSE.

    The signature matches :func:`stream_anthropic_chat` so the
    endpoint can dispatch between them on a per-request basis. ``model``
    and ``max_tokens`` are accepted for parity but only ``model`` is
    forwarded (``--model``) — the CLI manages its own token budget via
    the user's plan.

    Emits one ``data: {"delta": "..."}`` line per stdout chunk and
    terminates with ``data: [DONE]``. Errors surface as a single
    ``data: {"error": "..."}`` line before ``[DONE]``.
    """
    argv = _default_claude_cli_argv()
    prompt = _format_cli_prompt(messages, system_prompt)
    if not prompt.strip():
        payload = json.dumps({"error": "empty prompt"})
        yield f"data: {payload}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"
        return

    # Build full argv. Only append --model + --output-format when the
    # user hasn't supplied a custom worker command — otherwise tests
    # using a Python stub stay simple.
    # Only pass the last user message - no system prompt for CLI to stay under Windows 8191 char limit
    last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "hello")
    safe_prompt = last_user[:2000]
    cmd = list(argv) + [safe_prompt]
    # Always append output format flags
    cmd += ["--output-format", "text"]
    if model:
        cmd += ["--model", model]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        payload = json.dumps(
            {"error": f"claude CLI not found: {exc}. Install Claude Code or set MERIDIAN_CLAUDE_CLI."}
        )
        yield f"data: {payload}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"
        return
    except Exception as exc:  # noqa: BLE001 — surface spawn errors
        payload = json.dumps(
            {"error": f"failed to spawn claude CLI: {type(exc).__name__}: {exc}"}
        )
        yield f"data: {payload}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"
        return

    assert proc.stdout is not None
    try:
        # Read stdout in moderate chunks rather than line-by-line so a
        # word that crosses a chunk boundary still streams promptly.
        while True:
            chunk = await proc.stdout.read(256)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            if not text:
                continue
            payload = json.dumps({"delta": text})
            yield f"data: {payload}\n\n".encode("utf-8")
    except Exception as exc:  # noqa: BLE001 — surface read errors
        payload = json.dumps(
            {"error": f"stdout read failed: {type(exc).__name__}: {exc}"}
        )
        yield f"data: {payload}\n\n".encode("utf-8")

    # Drain remaining stderr and check exit code.
    stderr_bytes = b""
    if proc.stderr is not None:
        try:
            stderr_bytes = await proc.stderr.read()
        except Exception:  # noqa: BLE001
            stderr_bytes = b""
    rc = await proc.wait()
    if rc != 0:
        msg = (
            stderr_bytes.decode("utf-8", errors="replace").strip()
            or f"exit code {rc}"
        )
        payload = json.dumps({"error": f"claude CLI failed: {msg[:500]}"})
        yield f"data: {payload}\n\n".encode("utf-8")

    yield b"data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Meridian Dashboard</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🧭</text></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0d0f12;
  --surface: #13161b;
  --surface-2: #1a1e25;
  --border: #232830;
  --text: #d8dde6;
  --muted: #7a8190;
  --accent: #4a9eff;
  --accent-green: #00d4aa;
  --status-pending: #f0c674;
  --status-done: #00d4aa;
  --status-failed: #ff6b6b;
  --status-hitl: #ff9f43;
}
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0; height: 100%; overflow: hidden;
  background: var(--bg); color: var(--text);
  font-family: 'IBM Plex Sans', -apple-system, BlinkMacSystemFont, sans-serif;
  font-size: 13px;
}
.mono { font-family: 'IBM Plex Mono', ui-monospace, monospace; }
.app { display: grid; grid-template-columns: 220px 1fr; height: 100vh; }

.sidebar {
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column;
}
.sidebar-header {
  padding: 14px 16px; border-bottom: 1px solid var(--border);
  font-family: 'IBM Plex Mono', monospace; font-weight: 600;
  letter-spacing: 0.04em; color: var(--accent);
}
.sidebar-header small { display: block; color: var(--muted); font-weight: 400; font-size: 11px; }
#api-warn {
  margin: 10px; padding: 8px 10px;
  background: rgba(255,107,107,0.1); border: 1px solid rgba(255,107,107,0.3);
  border-radius: 4px; color: var(--status-failed); font-size: 11px;
  display: none;
}
.projects-label {
  padding: 10px 16px 4px; color: var(--muted); font-size: 11px;
  letter-spacing: 0.06em; text-transform: uppercase;
  font-family: 'IBM Plex Mono', monospace;
}
.project-list { flex: 1; overflow-y: auto; padding: 4px 8px 16px; }
.project-item {
  padding: 8px 10px; border-radius: 4px; cursor: pointer;
  font-family: 'IBM Plex Mono', monospace; font-size: 12px;
  display: flex; justify-content: space-between; align-items: center;
}
.project-item:hover { background: var(--surface-2); }
.project-item .id { color: var(--muted); font-size: 10px; margin-left: 8px; }

.new-project {
  padding: 8px 16px; border-top: 1px solid var(--border);
  display: flex; gap: 6px;
}
.new-project input { flex: 1; }

.main { display: flex; flex-direction: column; min-width: 0; }
.tabs {
  background: var(--surface); border-bottom: 1px solid var(--border);
  display: flex; align-items: stretch; overflow-x: auto; min-height: 40px;
}
.tab {
  padding: 10px 14px; border-right: 1px solid var(--border);
  cursor: pointer; display: flex; gap: 8px; align-items: center;
  font-family: 'IBM Plex Mono', monospace; font-size: 12px;
  background: var(--surface); color: var(--muted);
  white-space: nowrap;
}
.tab.active { background: var(--bg); color: var(--text); border-bottom: 2px solid var(--accent); }
.tab .close {
  color: var(--muted); border: none; background: transparent;
  cursor: pointer; padding: 0 2px; font-size: 14px;
}
.tab .close:hover { color: var(--status-failed); }
.tab-bodies { flex: 1; overflow: hidden; position: relative; }
.tab-body {
  position: absolute; inset: 0;
  display: none; flex-direction: row; overflow: hidden;
}
.tab-body.active { display: flex; }

.vtab-strip {
  width: 44px; flex-shrink: 0; background: var(--bg);
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column; align-items: center;
  padding-top: 6px; gap: 2px; z-index: 20; position: relative;
}
.vtab-btn {
  width: 36px; height: 36px; border-radius: 6px;
  background: transparent; border: none; cursor: pointer;
  color: var(--muted); font-size: 15px;
  display: flex; align-items: center; justify-content: center;
  transition: background 0.15s, color 0.15s;
}
.vtab-btn:hover { background: var(--surface-2); color: var(--text); }
.vtab-btn.active { background: var(--surface-2); color: var(--accent); }

.vtab-drawer {
  position: absolute; left: 44px; top: 0; bottom: 0; width: 360px;
  background: var(--bg); border-right: 1px solid var(--border);
  z-index: 10; transform: translateX(-360px);
  transition: transform 0.18s ease; display: flex; flex-direction: column;
  overflow: hidden;
}
.vtab-drawer.open { transform: translateX(0); }

.drawer-panel { display: none; flex-direction: column; flex: 1; overflow: hidden; }
.drawer-panel.active { display: flex; }
.drawer-header {
  padding: 10px 14px; border-bottom: 1px solid var(--border);
  font-family: 'IBM Plex Mono', monospace; font-size: 11px;
  color: var(--muted); letter-spacing: 0.06em; text-transform: uppercase;
  display: flex; justify-content: space-between; align-items: center;
  flex-shrink: 0; gap: 6px;
}
.chat-full {
  flex: 1; display: flex; flex-direction: column;
  overflow: hidden; min-width: 0; background: var(--surface);
}
.file-list { display: flex; flex-direction: column; gap: 4px; padding: 12px 14px; }
.file-item {
  padding: 7px 10px; background: var(--surface-2);
  border: 1px solid var(--border); border-radius: 4px;
  font-family: 'IBM Plex Mono', monospace; font-size: 11px;
  cursor: pointer; color: var(--text);
}
.file-item:hover { border-color: var(--accent); color: var(--accent); }

.empty {
  display: flex; align-items: center; justify-content: center;
  height: 100%; color: var(--muted);
  font-family: 'IBM Plex Mono', monospace; font-size: 12px;
}

.panel { display: flex; flex-direction: column; overflow: hidden; min-width: 0; }
.panel.left { border-right: 1px solid var(--border); background: var(--bg); }
.panel.right { background: var(--surface); }
.panel-header {
  padding: 10px 14px; border-bottom: 1px solid var(--border);
  font-family: 'IBM Plex Mono', monospace; font-size: 11px;
  color: var(--muted); letter-spacing: 0.06em; text-transform: uppercase;
  display: flex; justify-content: space-between; align-items: center;
}
.panel-header .ws-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--status-failed); display: inline-block;
}
.panel-header .ws-dot.connected { background: var(--status-done); }
.chat-mode { display: inline-flex; gap: 0; border: 1px solid var(--border); border-radius: 4px; overflow: hidden; }
.chat-mode .mode-btn {
  background: transparent; color: var(--muted); border: none; padding: 3px 8px;
  font-family: 'IBM Plex Mono', monospace; font-size: 10px; font-weight: 600;
  letter-spacing: 0.06em; cursor: pointer;
}
.chat-mode .mode-btn:hover { color: var(--text); }
.chat-mode .mode-btn.active { background: var(--accent); color: #001020; }

.section {
  padding: 12px 14px; border-bottom: 1px solid var(--border);
}
.section h3 {
  margin: 0 0 8px; font-size: 11px; color: var(--muted);
  letter-spacing: 0.06em; text-transform: uppercase;
  font-family: 'IBM Plex Mono', monospace; font-weight: 600;
}
.goal-area {
  width: 100%; min-height: 90px; max-height: 280px;
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 4px; padding: 8px; color: var(--text);
  font-family: 'IBM Plex Mono', monospace; font-size: 12px;
  resize: vertical;
}
.goal-actions { display: flex; gap: 6px; margin-top: 6px; align-items: center; }
.goal-version { color: var(--muted); font-size: 11px; font-family: 'IBM Plex Mono', monospace; }

button.primary, button.secondary, button.danger {
  border: 1px solid var(--border); background: var(--surface-2);
  color: var(--text); padding: 6px 12px; border-radius: 4px;
  cursor: pointer; font-family: 'IBM Plex Mono', monospace; font-size: 11px;
}
button.primary { background: var(--accent); border-color: var(--accent); color: #001020; font-weight: 600; }
button.primary:hover { filter: brightness(1.1); }
button.secondary:hover { background: var(--border); }
button.danger { background: rgba(255,107,107,0.15); border-color: var(--status-failed); color: var(--status-failed); }
button.danger:hover { background: rgba(255,107,107,0.25); }
button.execute { background: var(--status-hitl); border-color: var(--status-hitl); color: #1a0f00; font-weight: 700; }
button.execute:hover { filter: brightness(1.1); }

input[type=text] {
  background: var(--surface-2); border: 1px solid var(--border);
  color: var(--text); padding: 6px 8px; border-radius: 4px;
  font-family: 'IBM Plex Mono', monospace; font-size: 12px;
}

.sessions-list { display: flex; flex-direction: column; gap: 4px; }
.session-row {
  padding: 4px 8px; background: var(--surface-2); border-radius: 4px;
  font-family: 'IBM Plex Mono', monospace; font-size: 11px;
  display: flex; justify-content: space-between;
}
.session-row .name { color: var(--text); }
.session-row .meta { color: var(--muted); }

.scroll-area { flex: 1; overflow-y: auto; }

.task-list { display: flex; flex-direction: column; }
.task {
  padding: 8px 14px; border-bottom: 1px solid var(--border);
  font-family: 'IBM Plex Mono', monospace; font-size: 12px;
  display: flex; gap: 10px; align-items: flex-start;
}
.task .status-badge {
  font-size: 10px; padding: 2px 6px; border-radius: 3px;
  text-transform: uppercase; letter-spacing: 0.04em;
  font-weight: 600; white-space: nowrap; flex-shrink: 0;
}
.task.pending .status-badge { background: rgba(240,198,116,0.15); color: var(--status-pending); }
.task.done .status-badge    { background: rgba(0,212,170,0.15); color: var(--status-done); }
.task.failed .status-badge  { background: rgba(255,107,107,0.15); color: var(--status-failed); }
.task.pending-hitl {
  background: rgba(255,159,67,0.08);
  border-left: 3px solid var(--status-hitl);
}
.task.pending-hitl .status-badge { background: rgba(255,159,67,0.2); color: var(--status-hitl); }
.task.pending-hitl .desc { font-weight: 600; }
.task .desc { flex: 1; word-break: break-word; }
.task .meta { font-size: 10px; color: var(--muted); }

.hitl-banner {
  margin: 0; padding: 10px 14px;
  background: rgba(255,159,67,0.1); border-bottom: 1px solid var(--status-hitl);
  font-family: 'IBM Plex Mono', monospace; font-size: 11px;
  color: var(--status-hitl); font-weight: 600;
  letter-spacing: 0.06em; text-transform: uppercase;
}
.hitl-row {
  padding: 10px 14px; border-bottom: 1px solid var(--border);
  background: rgba(255,159,67,0.04);
}
.hitl-row .prompt {
  font-family: 'IBM Plex Mono', monospace; font-size: 12px;
  margin-bottom: 8px; word-break: break-word;
}
.hitl-row .controls { display: flex; gap: 6px; align-items: center; }
.hitl-row input[type=text] { flex: 1; }

.chat-history {
  flex: 1; overflow-y: auto; padding: 14px 18px;
  display: flex; flex-direction: column; gap: 12px;
}
.msg {
  padding: 10px 12px; border-radius: 6px; max-width: 90%;
  font-size: 13px; line-height: 1.5; word-wrap: break-word; white-space: pre-wrap;
}
.msg.user { background: var(--surface-2); border: 1px solid var(--border); align-self: flex-end; }
.msg.assistant { background: rgba(74,158,255,0.07); border: 1px solid rgba(74,158,255,0.25); align-self: flex-start; }
.msg.system { background: rgba(0,212,170,0.05); border: 1px dashed var(--accent-green); color: var(--muted); font-size: 11px; align-self: stretch; font-family: 'IBM Plex Mono', monospace; }

.chat-input-row {
  display: flex; gap: 6px; padding: 10px 14px;
  border-top: 1px solid var(--border); background: var(--surface);
}
.chat-input-row textarea {
  flex: 1; background: var(--surface-2); border: 1px solid var(--border);
  color: var(--text); padding: 8px 10px; border-radius: 4px;
  font-family: 'IBM Plex Sans', sans-serif; font-size: 13px;
  resize: none; max-height: 160px; min-height: 36px;
}

.toast {
  position: fixed; bottom: 16px; right: 16px; z-index: 100;
  background: var(--surface-2); border: 1px solid var(--border);
  padding: 10px 14px; border-radius: 4px; font-size: 12px;
  font-family: 'IBM Plex Mono', monospace;
  display: none;
}
.toast.show { display: block; }
.toast.error { border-color: var(--status-failed); color: var(--status-failed); }
</style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="sidebar-header">MERIDIAN<small>v0.3.1 dashboard</small></div>
    <div id="api-warn">No auth configured — chat disabled. Set ANTHROPIC_API_KEY or connect Claude Max.</div>
    <div id="auth-method" style="margin:6px 10px 0;padding:5px 10px;background:rgba(0,212,170,0.08);border:1px solid rgba(0,212,170,0.25);border-radius:4px;color:var(--accent-green);font-size:10px;display:none"></div>
    <div class="projects-label">Projects</div>
    <div id="project-list" class="project-list"></div>
    <div class="new-project">
      <input id="new-project-name" type="text" placeholder="new project name">
      <button class="primary" id="new-project-btn">+</button>
    </div>
  </aside>
  <main class="main">
    <div class="tabs" id="tabs"></div>
    <div class="tab-bodies" id="tab-bodies">
      <div class="empty">no project open — pick one on the left</div>
    </div>
  </main>
</div>
<div class="toast" id="toast"></div>

<script>
const TABS_KEY = 'meridian.openTabs';
const state = {
  projects: [],
  tabs: [], // [{id, project}]
  activeTab: null,
  panels: {}, // tabId -> { ws, taskCache, sessionName, chatHistory, goalRaw, goalIsJson }
  apiKeyConfigured: false,
};

function toast(msg, isError=false) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.toggle('error', isError);
  el.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove('show'), 2600);
}

async function api(path, opts={}) {
  const r = await fetch(path, { headers: {'Content-Type': 'application/json'}, ...opts });
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`${r.status}: ${text}`);
  }
  return r.status === 204 ? null : r.json();
}

async function loadConfig() {
  try {
    const cfg = await api('/config/api-key');
    state.apiKeyConfigured = !!cfg.configured;
    document.getElementById('api-warn').style.display = cfg.configured ? 'none' : 'block';
    const methodEl = document.getElementById('auth-method');
    if (cfg.method === 'oauth') {
      methodEl.textContent = 'Auth: Claude Max OAuth';
      methodEl.style.display = 'block';
    } else if (cfg.method === 'api_key') {
      methodEl.textContent = 'Auth: API key';
      methodEl.style.display = 'block';
    } else {
      methodEl.style.display = 'none';
    }
  } catch (e) { /* ignore */ }
}

async function loadProjects() {
  state.projects = await api('/projects');
  const list = document.getElementById('project-list');
  list.innerHTML = '';
  state.projects.forEach(p => {
    const div = document.createElement('div');
    div.className = 'project-item';
    div.innerHTML = `<span>${escapeHtml(p.name)}</span><span class="id">${p.id.slice(0,6)}</span>`;
    div.onclick = () => openTab(p);
    list.appendChild(div);
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function openTab(project) {
  const existing = state.tabs.find(t => t.id === project.id);
  if (existing) { activateTab(project.id); return; }
  state.tabs.push({ id: project.id, project });
  saveTabs();
  renderTabs();
  buildTabBody(project);
  activateTab(project.id);
}

function closeTab(id) {
  state.tabs = state.tabs.filter(t => t.id !== id);
  const panel = state.panels[id];
  if (panel) {
    try { panel.ws && panel.ws.close(); } catch(e){}
    delete state.panels[id];
  }
  document.getElementById(`tab-body-${id}`)?.remove();
  saveTabs();
  renderTabs();
  if (state.activeTab === id) {
    const next = state.tabs[state.tabs.length - 1];
    state.activeTab = next ? next.id : null;
    if (next) activateTab(next.id);
    else document.getElementById('tab-bodies').innerHTML = '<div class="empty">no project open — pick one on the left</div>';
  }
}

function saveTabs() {
  try {
    localStorage.setItem(TABS_KEY, JSON.stringify(state.tabs.map(t => t.id)));
  } catch(e) {}
}

function renderTabs() {
  const bar = document.getElementById('tabs');
  bar.innerHTML = '';
  state.tabs.forEach(t => {
    const div = document.createElement('div');
    div.className = 'tab' + (state.activeTab === t.id ? ' active' : '');
    div.innerHTML = `<span>${escapeHtml(t.project.name)}</span>`;
    div.onclick = () => activateTab(t.id);
    const close = document.createElement('button');
    close.className = 'close';
    close.textContent = '×';
    close.onclick = (e) => { e.stopPropagation(); closeTab(t.id); };
    div.appendChild(close);
    bar.appendChild(div);
  });
}

function activateTab(id) {
  state.activeTab = id;
  renderTabs();
  document.querySelectorAll('.tab-body').forEach(el => el.classList.remove('active'));
  const body = document.getElementById(`tab-body-${id}`);
  if (body) body.classList.add('active');
  // clear empty placeholder
  const empty = document.querySelector('.tab-bodies > .empty');
  if (empty) empty.remove();
}

function buildTabBody(project) {
  const root = document.getElementById('tab-bodies');
  const empty = root.querySelector(':scope > .empty');
  if (empty) empty.remove();

  const body = document.createElement('div');
  body.className = 'tab-body';
  body.id = `tab-body-${project.id}`;
  body.innerHTML = `
    <div class="vtab-strip" id="vtab-strip-${project.id}">
      <button class="vtab-btn active" data-vtab="status" title="Status &amp; Sessions">≡</button>
      <button class="vtab-btn" data-vtab="goal" title="Goal State">◎</button>
      <button class="vtab-btn" data-vtab="files" title="Files">⊞</button>
      <button class="vtab-btn" data-vtab="devlog" title="Dev Log">≋</button>
    </div>
    <div class="vtab-drawer open" id="drawer-${project.id}">
      <div class="drawer-panel active" id="drawer-status-${project.id}">
        <div class="drawer-header">
          <span>STATUS · ${escapeHtml(project.name)}</span>
          <span class="ws-dot" id="ws-${project.id}"></span>
        </div>
        <div class="section">
          <h3>Active Sessions</h3>
          <div class="sessions-list" id="sessions-${project.id}"></div>
        </div>
        <div class="hitl-banner" id="hitl-banner-${project.id}" style="display:none">HITL queue</div>
        <div id="hitl-queue-${project.id}"></div>
      </div>
      <div class="drawer-panel" id="drawer-goal-${project.id}">
        <div class="drawer-header">GOAL · ${escapeHtml(project.name)}</div>
        <div style="flex:1;display:flex;flex-direction:column;padding:12px 14px;gap:8px;overflow:hidden">
          <div style="display:flex;align-items:center;justify-content:space-between;flex-shrink:0">
            <span class="goal-version" id="goal-version-${project.id}"></span>
            <button class="primary" id="save-goal-${project.id}">save</button>
          </div>
          <textarea class="goal-area mono" id="goal-${project.id}" placeholder="(no goal set)" style="flex:1;max-height:none;resize:none"></textarea>
          <span class="goal-version" id="goal-state-${project.id}"></span>
        </div>
      </div>
      <div class="drawer-panel" id="drawer-files-${project.id}">
        <div class="drawer-header">FILES · ${escapeHtml(project.name)}</div>
        <div id="files-browse-${project.id}" style="flex:1;overflow-y:auto">
          <div class="file-list" id="files-list-${project.id}"></div>
        </div>
        <div id="file-editor-wrap-${project.id}" style="display:none;flex:1;flex-direction:column;overflow:hidden">
          <div class="drawer-header" style="flex-shrink:0">
            <button class="secondary" id="file-back-${project.id}" style="padding:2px 8px;font-size:10px">← back</button>
            <span id="file-name-${project.id}" style="flex:1;color:var(--accent);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span>
            <button class="primary" id="file-save-${project.id}" style="padding:2px 8px;font-size:10px">save</button>
          </div>
          <textarea id="file-content-${project.id}" style="flex:1;background:var(--surface-2);border:none;border-top:1px solid var(--border);color:var(--text);padding:10px 14px;font-family:'IBM Plex Mono',monospace;font-size:12px;resize:none;outline:none;overflow-y:auto"></textarea>
        </div>
      </div>
      <div class="drawer-panel" id="drawer-devlog-${project.id}">
        <div class="drawer-header">DEV LOG · ${escapeHtml(project.name)}</div>
        <div class="scroll-area"><div class="task-list" id="tasks-${project.id}"></div></div>
      </div>
    </div>
    <section class="chat-full">
      <div class="panel-header">
        <span>CHAT · claude-sonnet-4.6</span>
        <span style="display:flex;gap:6px;align-items:center">
          <select id="model-select-${project.id}" style="background:var(--surface-2);border:1px solid var(--border);color:var(--text);padding:2px 6px;border-radius:4px;font-family:'IBM Plex Mono',monospace;font-size:10px;cursor:pointer">
            <option value="claude-sonnet-4-6">sonnet-4.6</option>
            <option value="claude-opus-4-6">opus-4.6</option>
            <option value="claude-opus-4-7">opus-4.7</option>
            <option value="claude-haiku-4-5-20251001">haiku-4.5</option>
          </select>
          <button id="clear-chat-${project.id}" title="Clear chat history" style="background:transparent;border:1px solid var(--border);color:var(--muted);padding:2px 8px;border-radius:4px;font-family:'IBM Plex Mono',monospace;font-size:10px;cursor:pointer">clear</button>
          <span class="chat-mode" id="chat-mode-${project.id}">
            <button class="mode-btn active" data-mode="cli" title="Use the claude CLI on this machine (draws from Max plan, no API credits)">CLI</button>
            <button class="mode-btn" data-mode="api" title="Call api.anthropic.com directly (bills API credits)">API</button>
          </span>
        </span>
      </div>
      <div class="chat-history" id="chat-${project.id}"></div>
      <div class="chat-input-row">
        <textarea id="chat-input-${project.id}" placeholder="message claude (enter to send, shift+enter for newline)"></textarea>
        <button class="primary" id="chat-send-${project.id}">send</button>
      </div>
    </section>
  `;
  root.appendChild(body);

  // Per-tab state. chatMode restored from localStorage so the user's choice
  // persists across reloads. activeVtab tracks which drawer panel is open.
  let initialMode = 'api';
  try {
    const saved = localStorage.getItem('meridian.chatMode');
    if (saved === 'api' || saved === 'cli') initialMode = saved;
  } catch(e) {}
  state.panels[project.id] = {
    ws: null, taskCache: [], goalRaw: null, goalIsJson: false,
    chatHistory: [], chatMode: initialMode, activeVtab: 'status',
  };

  // Vtab drawer toggle — same tab again collapses; different tab switches.
  const vtabStrip = document.getElementById(`vtab-strip-${project.id}`);
  const drawer = document.getElementById(`drawer-${project.id}`);
  if (vtabStrip && drawer) {
    vtabStrip.querySelectorAll('.vtab-btn').forEach(btn => {
      btn.onclick = () => {
        const vtab = btn.dataset.vtab;
        const p = state.panels[project.id];
        if (p.activeVtab === vtab && drawer.classList.contains('open')) {
          drawer.classList.remove('open');
          vtabStrip.querySelectorAll('.vtab-btn').forEach(b => b.classList.remove('active'));
        } else {
          drawer.classList.add('open');
          vtabStrip.querySelectorAll('.vtab-btn').forEach(b => {
            b.classList.toggle('active', b.dataset.vtab === vtab);
          });
          drawer.querySelectorAll('.drawer-panel').forEach(dp => {
            dp.classList.toggle('active', dp.id === `drawer-${vtab}-${project.id}`);
          });
          p.activeVtab = vtab;
          if (vtab === 'files') loadFilesTab(project.id);
          if (vtab === 'devlog') refreshTasks(project.id);
        }
      };
    });
  }

  document.getElementById(`save-goal-${project.id}`).onclick = () => saveGoal(project.id);

  // Clear chat button
  document.getElementById(`clear-chat-${project.id}`).onclick = async () => {
    if (!confirm('Clear chat history for this project?')) return;
    try {
      await fetch(`/projects/${project.id}/chat/history`, { method: 'DELETE' });
      const history = document.getElementById(`chat-${project.id}`);
      if (history) history.innerHTML = '';
      if (state.panels[project.id]) state.panels[project.id].chatHistory = [];
      toast('chat cleared');
    } catch(e) { toast('clear failed: ' + e.message, true); }
  };

  // Model selector
  const modelSel = document.getElementById(`model-select-${project.id}`);
  if (modelSel) {
    const VALID_MODELS = ['claude-sonnet-4-6','claude-opus-4-6','claude-opus-4-7','claude-haiku-4-5-20251001'];
    let savedModel = localStorage.getItem('meridian.chatModel') || 'claude-sonnet-4-6';
    if (!VALID_MODELS.includes(savedModel)) savedModel = 'claude-sonnet-4-6'; // evict stale
    modelSel.value = savedModel;
    if (state.panels[project.id]) state.panels[project.id].chatModel = savedModel;
    modelSel.onchange = () => {
      const m = modelSel.value;
      if (state.panels[project.id]) state.panels[project.id].chatModel = m;
      localStorage.setItem('meridian.chatModel', m);
      toast('model: ' + m);
    };
  }
  document.getElementById(`goal-${project.id}`).addEventListener('blur', () => saveGoal(project.id));
  document.getElementById(`chat-send-${project.id}`).onclick = () => sendChat(project.id);
  const input = document.getElementById(`chat-input-${project.id}`);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(project.id); }
  });

  // Mode toggle — updates panel state, active-button class, and localStorage.
  const modeRoot = document.getElementById(`chat-mode-${project.id}`);
  if (modeRoot) {
    modeRoot.querySelectorAll('.mode-btn').forEach(btn => {
      if (btn.dataset.mode === initialMode) btn.classList.add('active');
      else btn.classList.remove('active');
      btn.onclick = () => {
        const mode = btn.dataset.mode;
        state.panels[project.id].chatMode = mode;
        try { localStorage.setItem('meridian.chatMode', mode); } catch(e) {}
        modeRoot.querySelectorAll('.mode-btn').forEach(b => {
          b.classList.toggle('active', b.dataset.mode === mode);
        });
        toast(mode === 'cli'
          ? 'CLI mode — uses Max plan, no API credits'
          : 'API mode — bills metered API credits');
      };
    });
  }

  // Files tab: back button returns to browse view; save button persists edits.
  const fileBackBtn = document.getElementById(`file-back-${project.id}`);
  const fileSaveBtn = document.getElementById(`file-save-${project.id}`);
  if (fileBackBtn) fileBackBtn.onclick = () => {
    const browse = document.getElementById(`files-browse-${project.id}`);
    const editorWrap = document.getElementById(`file-editor-wrap-${project.id}`);
    if (browse) browse.style.display = '';
    if (editorWrap) editorWrap.style.display = 'none';
  };
  if (fileSaveBtn) fileSaveBtn.onclick = () => saveFile(project.id);

  refreshTab(project.id);

  // Restore persisted chat history so conversations survive page refresh.
  (async () => {
    try {
      const history = await api(`/projects/${project.id}/chat/history`);
      if (history && history.length) {
        const chatDiv = document.getElementById(`chat-${project.id}`);
        const panel = state.panels[project.id];
        if (chatDiv && panel) {
          history.forEach(msg => {
            appendChatMessage(chatDiv, msg.role, msg.content);
            panel.chatHistory.push({ role: msg.role, content: msg.content });
          });
        }
      }
    } catch(e) { /* no history yet or endpoint not reachable */ }
  })();

  connectWs(project.id);
}

async function loadFilesTab(projectId) {
  /**Load the list of editable files from the server and render them as
   * clickable items in the files drawer panel. */
  const listEl = document.getElementById(`files-list-${projectId}`);
  if (!listEl) return;
  try {
    const files = await api(`/projects/${projectId}/files`);
    if (!files || !files.length) {
      listEl.innerHTML = `<div style="padding:14px;color:var(--muted);font-family:'IBM Plex Mono',monospace;font-size:11px">No editable files found.</div>`;
      return;
    }
    listEl.innerHTML = files.map(f =>
      `<div class="file-item" data-filename="${escapeHtml(f)}">${escapeHtml(f)}</div>`
    ).join('');
    listEl.querySelectorAll('.file-item').forEach(item => {
      item.onclick = () => openFileEditor(projectId, item.dataset.filename);
    });
  } catch(e) {
    listEl.innerHTML = `<div style="padding:14px;color:var(--status-failed);font-family:'IBM Plex Mono',monospace;font-size:11px">Error: ${escapeHtml(e.message)}</div>`;
  }
}

async function openFileEditor(projectId, filename) {
  /**Fetch file content and switch the files panel into editor mode. */
  const browseEl = document.getElementById(`files-browse-${projectId}`);
  const editorEl = document.getElementById(`file-editor-wrap-${projectId}`);
  const nameEl = document.getElementById(`file-name-${projectId}`);
  const contentEl = document.getElementById(`file-content-${projectId}`);
  if (!browseEl || !editorEl || !contentEl || !nameEl) return;
  try {
    const data = await api(`/projects/${projectId}/files/${encodeURIComponent(filename)}`);
    contentEl.value = data.content || '';
    nameEl.textContent = filename;
    browseEl.style.display = 'none';
    editorEl.style.display = 'flex';
  } catch(e) { toast('open failed: ' + e.message, true); }
}

async function saveFile(projectId) {
  /**Write the current editor content back to the server. */
  const nameEl = document.getElementById(`file-name-${projectId}`);
  const contentEl = document.getElementById(`file-content-${projectId}`);
  if (!nameEl || !contentEl) return;
  const filename = nameEl.textContent.trim();
  if (!filename) return;
  try {
    await api(`/projects/${projectId}/files/${encodeURIComponent(filename)}`, {
      method: 'PUT',
      body: JSON.stringify({ content: contentEl.value }),
    });
    toast(`saved ${filename}`);
  } catch(e) { toast('save failed: ' + e.message, true); }
}

async function refreshTab(projectId) {
  await Promise.all([
    refreshGoal(projectId),
    refreshSessions(projectId),
    refreshTasks(projectId),
  ]);
}

async function refreshGoal(projectId) {
  const ta = document.getElementById(`goal-${projectId}`);
  const v = document.getElementById(`goal-version-${projectId}`);
  if (!ta) return;
  try {
    const goal = await api(`/projects/${projectId}/goal`);
    state.panels[projectId].goalRaw = goal.content;
    let text;
    if (typeof goal.content === 'string') {
      state.panels[projectId].goalIsJson = false;
      text = goal.content;
    } else {
      state.panels[projectId].goalIsJson = true;
      text = JSON.stringify(goal.content, null, 2);
    }
    ta.value = text;
    v.textContent = `v${goal.version}`;
  } catch (e) {
    ta.value = '';
    v.textContent = '(unset)';
  }
}

async function saveGoal(projectId) {
  const ta = document.getElementById(`goal-${projectId}`);
  if (!ta) return;
  const raw = ta.value;
  if (raw === state.panels[projectId]._lastSaved) return;
  let content = raw;
  if (state.panels[projectId].goalIsJson) {
    try { content = JSON.parse(raw); } catch(e) { /* fall back to string */ }
  }
  try {
    await api(`/projects/${projectId}/goal`, { method: 'POST', body: JSON.stringify({ content }) });
    state.panels[projectId]._lastSaved = raw;
    toast('goal saved');
    refreshGoal(projectId);
  } catch (e) {
    toast('save failed: ' + e.message, true);
  }
}

async function refreshSessions(projectId) {
  const root = document.getElementById(`sessions-${projectId}`);
  if (!root) return;
  try {
    const sessions = await api(`/projects/${projectId}/sessions`);
    root.innerHTML = sessions.map(s =>
      `<div class="session-row"><span class="name">${escapeHtml(s.name)}</span><span class="meta">${escapeHtml(s.status)} · ${escapeHtml(s.last_seen)}</span></div>`
    ).join('') || '<div class="session-row meta">(no active sessions)</div>';
  } catch(e) {}
}

async function refreshTasks(projectId) {
  try {
    const tasks = await api(`/projects/${projectId}/tasks?limit=50`);
    state.panels[projectId].taskCache = tasks;
    renderTasks(projectId);
  } catch(e) {}
}

function renderTasks(projectId) {
  const tasks = state.panels[projectId].taskCache || [];
  const root = document.getElementById(`tasks-${projectId}`);
  const hitlRoot = document.getElementById(`hitl-queue-${projectId}`);
  const banner = document.getElementById(`hitl-banner-${projectId}`);
  if (!root || !hitlRoot) return;
  const hitl = tasks.filter(t => t.status === 'pending-hitl');
  banner.style.display = hitl.length ? 'block' : 'none';
  hitlRoot.innerHTML = hitl.map(t => renderHitlRow(projectId, t)).join('');
  hitl.forEach(t => wireHitlRow(projectId, t));
  root.innerHTML = tasks.map(t => renderTaskRow(t)).join('');
}

function renderTaskRow(t) {
  return `
    <div class="task ${t.status}">
      <span class="status-badge">${t.status}</span>
      <div>
        <div class="desc">${escapeHtml(t.description)}</div>
        <div class="meta">${escapeHtml(t.created_at)}</div>
      </div>
    </div>`;
}

function renderHitlRow(projectId, t) {
  const isExecute = t.description.startsWith('[EXECUTE]');
  const label = isExecute ? 'EXECUTE REQUEST' : 'QUESTION';
  const body = t.description.replace(/^\[(ASK|EXECUTE)\]:?\s*/, '');
  if (isExecute) {
    return `
      <div class="hitl-row" data-task="${t.id}">
        <div class="prompt"><strong>${label}</strong> · ${escapeHtml(body)}</div>
        <div class="controls">
          <button class="execute" data-action="confirm" data-task="${t.id}">EXECUTE</button>
          <button class="danger"  data-action="reject"  data-task="${t.id}">REJECT</button>
        </div>
      </div>`;
  }
  return `
    <div class="hitl-row" data-task="${t.id}">
      <div class="prompt"><strong>${label}</strong> · ${escapeHtml(body)}</div>
      <div class="controls">
        <input type="text" placeholder="your reply" data-input="${t.id}">
        <button class="primary" data-action="reply" data-task="${t.id}">reply</button>
      </div>
    </div>`;
}

function wireHitlRow(projectId, t) {
  const row = document.querySelector(`#hitl-queue-${projectId} [data-task="${t.id}"]`);
  if (!row) return;
  row.querySelectorAll('button[data-action]').forEach(btn => {
    btn.onclick = () => {
      const action = btn.dataset.action;
      if (action === 'reply') {
        const inp = row.querySelector(`input[data-input="${t.id}"]`);
        const text = (inp && inp.value || '').trim();
        if (!text) { toast('enter a reply first', true); return; }
        hitlReply(projectId, t.id, text);
      } else if (action === 'confirm') {
        hitlExecute(projectId, t.id, true);
      } else if (action === 'reject') {
        hitlExecute(projectId, t.id, false);
      }
    };
  });
}

async function appendToGoal(projectId, line) {
  // Pull latest goal, append, push back. String-only for HITL markers.
  let current = '';
  try {
    const goal = await api(`/projects/${projectId}/goal`);
    current = typeof goal.content === 'string' ? goal.content : JSON.stringify(goal.content, null, 2);
  } catch(e) { /* unset goal is fine */ }
  const next = current ? current.trimEnd() + '\n' + line : line;
  await api(`/projects/${projectId}/goal`, { method: 'POST', body: JSON.stringify({ content: next }) });
}

async function hitlReply(projectId, taskId, text) {
  try {
    await appendToGoal(projectId, `[HITL-REPLY:${taskId}:] ${text}`);
    await api(`/tasks/${taskId}`, { method: 'PATCH', body: JSON.stringify({ status: 'done', description: `[ANSWERED] ${text}` }) });
    toast('reply sent');
  } catch(e) { toast('reply failed: ' + e.message, true); }
}

async function hitlExecute(projectId, taskId, confirmed) {
  try {
    if (confirmed) {
      await appendToGoal(projectId, `[EXECUTE-CONFIRMED:${taskId}]`);
      await api(`/tasks/${taskId}`, { method: 'PATCH', body: JSON.stringify({ status: 'done' }) });
      toast('execute confirmed');
    } else {
      await appendToGoal(projectId, `[EXECUTE-REJECTED:${taskId}]`);
      await api(`/tasks/${taskId}`, { method: 'PATCH', body: JSON.stringify({ status: 'failed' }) });
      toast('execute rejected');
    }
  } catch(e) { toast('execute failed: ' + e.message, true); }
}

function connectWs(projectId) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws/${projectId}`);
  const dot = document.getElementById(`ws-${projectId}`);
  ws.onopen = () => { dot && dot.classList.add('connected'); };
  ws.onclose = () => {
    dot && dot.classList.remove('connected');
    // Reconnect after a beat if the tab is still open.
    setTimeout(() => {
      if (state.panels[projectId]) connectWs(projectId);
    }, 1500);
  };
  ws.onerror = () => { dot && dot.classList.remove('connected'); };
  ws.onmessage = (ev) => {
    try {
      const event = JSON.parse(ev.data);
      handleWsEvent(projectId, event);
    } catch(e){}
  };
  state.panels[projectId].ws = ws;
}

function handleWsEvent(projectId, event) {
  const cache = state.panels[projectId].taskCache;
  if (event.type === 'task_created') {
    cache.unshift(event.task);
  } else if (event.type === 'task_updated') {
    const i = cache.findIndex(t => t.id === event.task.id);
    if (i >= 0) cache[i] = event.task;
    else cache.unshift(event.task);
  }
  renderTasks(projectId);
  // A goal change is often triggered by a HITL reply — re-pull.
  refreshGoal(projectId);
}

async function sendChat(projectId) {
  const input = document.getElementById(`chat-input-${projectId}`);
  const text = input.value.trim();
  if (!text) return;
  const panel = state.panels[projectId];
  const mode = (panel && panel.chatMode) || 'cli';
  // CLI mode uses the claude binary's own auth; only API mode needs
  // ANTHROPIC_API_KEY / OAuth wired into the Anthropic SDK.
  if (mode === 'api' && !state.apiKeyConfigured) {
    toast('No auth configured — set ANTHROPIC_API_KEY or switch to CLI mode', true);
    return;
  }
  input.value = '';
  panel.chatHistory.push({ role: 'user', content: text });
  const history = document.getElementById(`chat-${projectId}`);
  appendChatMessage(history, 'user', text);

  // Build the system prompt from current goal + last 20 tasks so the
  // model has shared context, but don't show it in the UI.
  let systemPrompt = `You are assisting on a Meridian project.`;
  try {
    const goal = await api(`/projects/${projectId}/goal`).catch(() => null);
    if (goal) systemPrompt += `\n\n# Goal (v${goal.version})\n${typeof goal.content === 'string' ? goal.content : JSON.stringify(goal.content, null, 2)}`;
  } catch(e) {}
  try {
    const tasks = await api(`/projects/${projectId}/tasks?limit=20`);
    if (tasks.length) {
      systemPrompt += `\n\n# Recent task log (newest first)\n` + tasks.map(t => `[${t.status}] ${t.description}`).join('\n');
    }
  } catch(e) {}

  const assistantNode = appendChatMessage(history, 'assistant', '');
  try {
    const resp = await fetch('/dashboard/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        project_id: projectId,
        messages: panel.chatHistory,
        system_prompt: systemPrompt,
        mode: mode,
        model: (panel && panel.chatModel) || 'claude-sonnet-4-6',
      }),
    });
    if (!resp.ok || !resp.body) {
      assistantNode.textContent = `error: ${resp.status}`;
      assistantNode.classList.add('error');
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let acc = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf('\n\n')) >= 0) {
        const chunk = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const line = chunk.split('\n').find(l => l.startsWith('data:'));
        if (!line) continue;
        const payload = line.slice(5).trim();
        if (payload === '[DONE]') break;
        try {
          const obj = JSON.parse(payload);
          if (obj.error) {
            const is429 = /rate.?limit/i.test(obj.error) && obj.error.includes('429');
            acc += is429
              ? '\n\n⚠️ Rate limited — wait ~60s then retry, or switch to Haiku model'
              : `\n\n⚠️ API error: ${obj.error}`;
          } else if (obj.delta) { acc += obj.delta; }
          assistantNode.textContent = acc;
          history.scrollTop = history.scrollHeight;
        } catch(e){}
      }
    }
    panel.chatHistory.push({ role: 'assistant', content: acc });
  } catch(e) {
    assistantNode.textContent = 'error: ' + e.message;
  }
}

function appendChatMessage(history, role, text) {
  const node = document.createElement('div');
  node.className = 'msg ' + role;
  node.textContent = text;
  history.appendChild(node);
  history.scrollTop = history.scrollHeight;
  return node;
}

document.getElementById('new-project-btn').onclick = async () => {
  const inp = document.getElementById('new-project-name');
  const name = inp.value.trim();
  if (!name) return;
  try {
    const p = await api('/projects', { method: 'POST', body: JSON.stringify({ name }) });
    inp.value = '';
    await loadProjects();
    openTab(p);
  } catch(e) { toast('create failed: ' + e.message, true); }
};

async function restoreTabs() {
  let saved = [];
  try { saved = JSON.parse(localStorage.getItem(TABS_KEY) || '[]'); } catch(e){}
  for (const id of saved) {
    const p = state.projects.find(x => x.id === id);
    if (p) openTab(p);
  }
}

(async function init() {
  await loadConfig();
  await loadProjects();
  await restoreTabs();
  // Periodic session refresh on the active tab — sessions don't generate
  // pub/sub events so polling fills that gap.
  setInterval(() => {
    if (state.activeTab) refreshSessions(state.activeTab);
  }, 10000);
})();
</script>
</body>
</html>
"""
