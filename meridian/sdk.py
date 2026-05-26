"""Meridian Python SDK — thin synchronous wrapper around the HTTP API.

Usage::

    from meridian.sdk import Meridian

    m = Meridian(api_key="sk_meridian_...", project_id="<uuid>")
    session = m.start_session("my-session", human_id="alice")
    m.log_task("Implemented auth module", status="done")
    print(m.get_context())
"""

from __future__ import annotations

import json
from typing import Any

try:
    import httpx
except ImportError as exc:  # pragma: no cover
    raise ImportError("httpx is required: pip install httpx") from exc


class Meridian:
    """Synchronous Meridian API client."""

    def __init__(
        self,
        api_key: str,
        project_id: str,
        base_url: str = "https://usemeridian.us",
        timeout: float = 30.0,
    ) -> None:
        self.project_id = project_id
        self.base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._timeout = timeout
        self._session_id: str | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _post(self, path: str, **payload: Any) -> Any:
        r = httpx.post(
            f"{self.base_url}{path}",
            headers=self._headers,
            content=json.dumps(payload),
            timeout=self._timeout,
        )
        r.raise_for_status()
        return r.json()

    def _get(self, path: str, **params: Any) -> Any:
        r = httpx.get(
            f"{self.base_url}{path}",
            headers=self._headers,
            params={k: v for k, v in params.items() if v is not None},
            timeout=self._timeout,
        )
        r.raise_for_status()
        return r.json()

    def _delete(self, path: str) -> None:
        r = httpx.delete(
            f"{self.base_url}{path}",
            headers=self._headers,
            timeout=self._timeout,
        )
        r.raise_for_status()

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    def start_session(self, session_name: str, human_id: str = "") -> dict[str, Any]:
        """Register session and return goal + recent tasks."""
        result = self._post(
            "/sessions",
            project_id=self.project_id,
            name=session_name,
            human_id=human_id,
        )
        self._session_id = result.get("id") or result.get("session_id")
        return result

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    def log_task(self, description: str, status: str = "done") -> dict[str, Any]:
        return self._post(
            "/tasks",
            project_id=self.project_id,
            session_id=self._session_id,
            description=description,
            status=status,
        )

    def get_tasks(self, limit: int = 10) -> list[dict[str, Any]]:
        result = self._get(
            f"/projects/{self.project_id}/tasks",
            limit=limit,
        )
        if isinstance(result, list):
            return result
        return result.get("tasks", [])

    # ------------------------------------------------------------------
    # Context / goal
    # ------------------------------------------------------------------

    def get_context(self, mode: str = "full") -> str:
        """Return a plain-text context block for pasting into a new session."""
        result = self._get(
            f"/projects/{self.project_id}/context-block",
            mode=mode,
        )
        return result.get("text", "")

    def get_goal(self) -> dict[str, Any]:
        return self._get(f"/projects/{self.project_id}/goal")

    def set_goal(self, content: str, north_star: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {"content": content}
        if north_star:
            payload["north_star"] = north_star
        return self._post(f"/projects/{self.project_id}/goal", **payload)

    # ------------------------------------------------------------------
    # HITL
    # ------------------------------------------------------------------

    def request_hitl(
        self,
        question: str,
        context: str = "",
        urgency: str = "normal",
    ) -> dict[str, Any]:
        return self._post(
            f"/projects/{self.project_id}/hitl",
            session_id=self._session_id,
            question=question,
            context=context,
            urgency=urgency,
        )

    # ------------------------------------------------------------------
    # Decisions
    # ------------------------------------------------------------------

    def pin_decision(
        self,
        title: str,
        body: str,
        category: str = "TECHNICAL",
    ) -> dict[str, Any]:
        return self._post(
            f"/projects/{self.project_id}/decisions-pinned",
            title=title,
            body=body,
            category=category,
        )

    def get_decisions(self) -> list[dict[str, Any]]:
        return self._get(f"/projects/{self.project_id}/decisions-pinned")

    # ------------------------------------------------------------------
    # Notes
    # ------------------------------------------------------------------

    def add_note(
        self,
        title: str,
        body: str,
        tags: str = "",
    ) -> dict[str, Any]:
        return self._post(
            f"/projects/{self.project_id}/notes",
            title=title,
            body=body,
            tags=tags,
        )

    def get_notes(self, tag: str = "") -> list[dict[str, Any]]:
        return self._get(f"/projects/{self.project_id}/notes", tag=tag or None)

    # ------------------------------------------------------------------
    # Handoff
    # ------------------------------------------------------------------

    def generate_handoff(self) -> dict[str, Any]:
        return self._post(f"/projects/{self.project_id}/handoff")
