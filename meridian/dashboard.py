"""Dashboard surface for Meridian.

Two pieces:

* :class:`WebSocketBroadcaster` — bridges the in-process pub/sub in
  :mod:`meridian.db` to live WebSocket clients. One instance lives on
  ``app.state.ws_broadcaster``.
* Static files — v1.0.2: the dashboard HTML/JS/CSS are served from
  ``meridian/templates/dashboard.html``, ``meridian/static/dashboard.js``,
  and ``meridian/static/dashboard.css`` via FastAPI's StaticFiles mount.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

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
# Dashboard HTML
# ---------------------------------------------------------------------------

