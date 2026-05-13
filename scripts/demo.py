"""End-to-end demo: two Claude sessions coordinating via Meridian.

Run a Meridian HTTP server first (e.g. ``pixi run start``), then in
another shell ``pixi run demo``. The script talks to the live server on
port 7878 over HTTP and prints a clearly-labeled story so it is obvious
what each session is doing.
"""

from __future__ import annotations

import os
import sys
import time

import httpx

# Windows consoles default to cp1252; the handoff template uses em-dashes.
# Force stdout/stderr to UTF-8 so the demo output prints cleanly everywhere.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

BASE_URL = os.environ.get("MERIDIAN_URL", "http://127.0.0.1:7878")


def _step(title: str) -> None:
    """Print a clearly delimited step header."""
    print()
    print(f"-- {title} " + "-" * max(0, 64 - len(title)))


def _wait_for_server(client: httpx.Client, timeout: float = 10.0) -> None:
    """Block until the server's /health endpoint responds, or raise."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = client.get("/health", timeout=1.0)
            if r.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"Meridian server not reachable at {BASE_URL}")


def run() -> int:
    """Execute the demo flow, returning a process exit code."""
    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
        _wait_for_server(client)

        _step("1. Create project 'demo-project'")
        project_name = "demo-project"
        r = client.post("/projects", json={"name": project_name})
        if r.status_code == 409:
            # Demo is idempotent: reuse the existing project on rerun.
            r = client.get("/projects")
            r.raise_for_status()
            project = next(p for p in r.json() if p["name"] == project_name)
            print("   (reusing existing demo-project)")
        else:
            r.raise_for_status()
            project = r.json()
        project_id = project["id"]
        print(f"   created project_id = {project_id}")
        print(f"   project name       = {project['name']}")

        _step("2. Register session-alpha")
        r = client.post(
            "/sessions/register",
            json={"project_id": project_id, "name": "session-alpha"},
        )
        r.raise_for_status()
        alpha = r.json()
        print(f"   session-alpha id = {alpha['id']}")

        _step("3. Set goal: 'Build a Python web scraper with async support'")
        goal_text = "Build a Python web scraper with async support"
        r = client.post(
            f"/projects/{project_id}/goal",
            json={"content": goal_text},
        )
        r.raise_for_status()
        goal = r.json()
        print(f"   goal version = {goal['version']}")
        print(f"   goal content = {goal['content']}")

        _step("4. session-alpha logs: 'Set up project structure'")
        r = client.post(
            "/tasks",
            json={
                "session_id": alpha["id"],
                "project_id": project_id,
                "description": "Set up project structure",
                "status": "done",
            },
        )
        r.raise_for_status()
        print(f"   task id = {r.json()['id']}")

        _step("5. session-alpha logs: 'Wrote async HTTP client'")
        r = client.post(
            "/tasks",
            json={
                "session_id": alpha["id"],
                "project_id": project_id,
                "description": "Wrote async HTTP client",
                "status": "done",
            },
        )
        r.raise_for_status()
        print(f"   task id = {r.json()['id']}")

        _step("6. Register session-beta")
        r = client.post(
            "/sessions/register",
            json={"project_id": project_id, "name": "session-beta"},
        )
        r.raise_for_status()
        beta = r.json()
        print(f"   session-beta id = {beta['id']}")

        _step("7. session-beta reads goal state")
        r = client.get(f"/projects/{project_id}/goal")
        r.raise_for_status()
        seen_goal = r.json()
        print(f"   beta sees goal v{seen_goal['version']}: {seen_goal['content']}")

        _step("8. session-beta reads task log — sees session-alpha's work")
        r = client.get(f"/projects/{project_id}/tasks")
        r.raise_for_status()
        tasks = r.json()
        for t in tasks:
            print(f"   [{t['status'].upper():7s}] {t['description']}")

        _step("9. session-beta logs: 'Reviewing session-alpha's HTTP client'")
        r = client.post(
            "/tasks",
            json={
                "session_id": beta["id"],
                "project_id": project_id,
                "description": "Reviewing session-alpha's HTTP client",
                "status": "done",
            },
        )
        r.raise_for_status()
        print(f"   task id = {r.json()['id']}")

        _step("10. Generate handoff file")
        r = client.post(f"/projects/{project_id}/handoff")
        r.raise_for_status()
        handoff = r.json()
        print(f"   handoff path = {handoff['path']}")

        _step("11. Full handoff content")
        print()
        print(handoff["content"])

        _step("12. Demo complete")
        print("   Demo complete. Two sessions coordinated successfully.")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except httpx.HTTPStatusError as exc:
        print(
            f"HTTP error from Meridian: {exc.response.status_code} "
            f"{exc.response.text}",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 — friendly CLI error
        print(f"Demo failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
