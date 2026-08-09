"""
MeridianCheckpointer — drop-in LangGraph checkpointer that persists
state to Meridian's DB and surfaces task progress in the dashboard.

Usage:
    from meridian.integrations.langgraph import MeridianCheckpointer

    checkpointer = MeridianCheckpointer(
        project_id="your-project-id",
        api_url="http://localhost:7878",  # or https://usemeridian.us
        api_token="your-bearer-token",
    )

    graph = StateGraph(...).compile(checkpointer=checkpointer)

Each LangGraph node completion is logged to Meridian's task_log and
appears in the dashboard timeline. HITL interrupts (when the graph
raises an Interrupt) are surfaced to Meridian's HITL queue so a human
can answer and the graph can resume.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Iterator

import httpx


class MeridianCheckpointer:
    """Implements LangGraph's BaseCheckpointSaver interface.

    Stores checkpoints in Meridian's task_log with:
    - agent_framework='langgraph'
    - HITL interrupts surfaced to Meridian's HITL queue

    The get/list methods return None/empty stubs — Meridian stores
    progress, not full graph state. Use a real stateful checkpointer
    (e.g. MemorySaver) alongside this for state persistence; wire
    MeridianCheckpointer for visibility only via LangGraph's
    ``checkpointer`` parameter.
    """

    def __init__(
        self,
        project_id: str,
        api_url: str,
        api_token: str = "",
    ) -> None:
        self.project_id = project_id
        self.api_url = api_url.rstrip("/")
        self.headers: dict[str, str] = {}
        if api_token:
            self.headers["Authorization"] = f"Bearer {api_token}"
        self._session_id: str | None = None
        # Same class of check-then-act race as _deps._tenant_db_cache /
        # doc_store._doc_store_cache (instance-scoped here instead of a
        # module-level dict): two graph nodes calling _log_task/_ensure_session
        # concurrently before the first POST completes both see
        # self._session_id as None and both register a session -- the
        # loser's session_id is discarded, leaving an orphaned session row.
        self._session_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> str:
        if self._session_id:
            return self._session_id
        async with self._session_lock:
            # Double-checked: another concurrent caller may have already
            # registered the session while we were blocked on the lock.
            if self._session_id:
                return self._session_id
            async with httpx.AsyncClient(timeout=10) as http:
                resp = await http.post(
                    f"{self.api_url}/sessions/register",
                    headers={**self.headers, "Content-Type": "application/json"},
                    json={
                        "project_id": self.project_id,
                        "name": "langgraph-worker",
                        "agent_framework": "langgraph",
                    },
                )
                resp.raise_for_status()
                self._session_id = resp.json()["id"]
        return self._session_id  # type: ignore[return-value]

    async def _log_task(self, description: str, status: str = "done") -> None:
        try:
            session_id = await self._ensure_session()
            async with httpx.AsyncClient(timeout=10) as http:
                await http.post(
                    f"{self.api_url}/tasks",
                    headers={**self.headers, "Content-Type": "application/json"},
                    json={
                        "session_id": session_id,
                        "project_id": self.project_id,
                        "description": description,
                        "status": status,
                    },
                )
        except Exception:  # noqa: BLE001 — never block graph execution on logging
            pass

    async def _request_hitl(self, question: str, context: str = "") -> None:
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                await http.post(
                    f"{self.api_url}/projects/{self.project_id}/hitl",
                    headers={**self.headers, "Content-Type": "application/json"},
                    json={
                        "question": question,
                        "context": context,
                        "urgency": "blocking",
                    },
                )
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # LangGraph BaseCheckpointSaver interface
    # ------------------------------------------------------------------

    async def put(
        self,
        config: dict[str, Any],
        checkpoint: dict[str, Any],
        metadata: dict[str, Any],
        new_versions: dict[str, Any],
    ) -> dict[str, Any]:
        """Called by LangGraph after each node completes — log to Meridian."""
        node = config.get("configurable", {}).get("thread_id", "unknown-node")
        step = metadata.get("step", "?")
        # Surface HITL interrupts to the Meridian queue
        writes = checkpoint.get("pending_sends") or []
        for w in writes:
            if isinstance(w, dict) and w.get("__interrupt__"):
                await self._request_hitl(
                    question=str(w.get("value", "Graph interrupted — awaiting human input")),
                    context=f"node={node} step={step}",
                )
        await self._log_task(f"[langgraph] {node} — step {step}")
        return config

    async def get(self, config: dict[str, Any]) -> dict[str, Any] | None:
        return None

    async def get_tuple(self, config: dict[str, Any]) -> tuple | None:
        return None

    async def list(
        self,
        config: dict[str, Any],
        *,
        filter: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        return
        yield  # make this an async generator

    # Sync stubs (LangGraph may call these in some environments)
    def put_writes(self, *args: Any, **kwargs: Any) -> None:
        pass

    # Async aliases
    async def aput(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return await self.put(*args, **kwargs)

    async def aget(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return await self.get(*args, **kwargs)

    async def aget_tuple(self, *args: Any, **kwargs: Any) -> tuple | None:
        return await self.get_tuple(*args, **kwargs)

    async def alist(self, *args: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        return self.list(*args, **kwargs)
