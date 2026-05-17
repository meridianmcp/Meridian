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
* Static files — v1.0.2: the dashboard HTML/JS/CSS are served from
  ``meridian/templates/dashboard.html``, ``meridian/static/dashboard.js``,
  and ``meridian/static/dashboard.css`` via FastAPI's StaticFiles mount.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

# Regex that picks the Claude CLI session ID line out of an output
# stream so we can persist it and pass ``--resume <uuid>`` on the next
# message. The CLI emits a line such as "Session ID: <uuid>"; we
# accept any 20+ char hex/dash sequence after the marker.
_SESSION_ID_RE = re.compile(
    r"Session\s*ID:\s*([0-9a-fA-F][0-9a-fA-F\-]{19,})"
)

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
    *,
    resume_session_id: str | None = None,
    on_session_id: Callable[[str], Awaitable[None]] | None = None,
) -> AsyncIterator[bytes]:
    """Spawn the ``claude`` CLI and stream its text output back as SSE.

    The signature matches :func:`stream_anthropic_chat` so the
    endpoint can dispatch between them on a per-request basis. ``model``
    and ``max_tokens`` are accepted for parity but only ``model`` is
    forwarded (``--model``) — the CLI manages its own token budget via
    the user's plan.

    Multi-turn (v0.4.1): when ``resume_session_id`` is provided we
    insert ``--resume <id>`` into the argv so the CLI rejoins an
    existing conversation. When ``on_session_id`` is provided we scan
    the streamed output for a ``Session ID: <uuid>`` marker and call
    the callback once with the captured id, so the server can persist
    it for the next turn.

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
    cmd = list(argv)
    if resume_session_id:
        cmd += ["--resume", resume_session_id]
    cmd += [safe_prompt]
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
    captured_id = False
    tail_buffer = ""  # last ~2 KB of stdout, scanned for the Session ID line
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
            # Look for the Session ID marker exactly once. Keeping a
            # rolling tail buffer means the regex still matches if the
            # marker is split across two chunks.
            if not captured_id and on_session_id is not None:
                tail_buffer = (tail_buffer + text)[-2048:]
                match = _SESSION_ID_RE.search(tail_buffer)
                if match:
                    captured_id = True
                    try:
                        await on_session_id(match.group(1))
                    except Exception:  # noqa: BLE001 — never fail the stream
                        pass
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
    # Some CLI versions emit the session-id line to stderr instead of
    # stdout. Scan it too before declaring the capture done.
    if not captured_id and on_session_id is not None and stderr_bytes:
        match = _SESSION_ID_RE.search(
            stderr_bytes.decode("utf-8", errors="replace")
        )
        if match:
            try:
                await on_session_id(match.group(1))
            except Exception:  # noqa: BLE001
                pass
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

