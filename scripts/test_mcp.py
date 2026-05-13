"""Smoke-test every Meridian MCP tool over stdio.

Usage:
    pixi run test-mcp

This script spawns ``python -m meridian --mcp`` as a subprocess, connects
to it as an MCP client, and exercises each of the eight tools in
sequence. Prints PASS/FAIL per tool and exits non-zero if any tool fails.

The MCP server uses whatever DB path ``MERIDIAN_DB`` points at. Set it to
a throwaway file if you don't want the test to mingle with your real
state — the script does so by default.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path


async def _run() -> int:
    """Drive the MCP server through every tool and report results."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    # Throwaway DB + data dir so this never collides with real data.
    tmp = Path(tempfile.mkdtemp(prefix="meridian-mcp-test-"))
    env = os.environ.copy()
    env["MERIDIAN_DB"] = str(tmp / "meridian.db")
    env["MERIDIAN_DATA_DIR"] = str(tmp)

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "meridian", "--mcp"],
        env=env,
    )

    failures = 0

    def _result_payload(result) -> dict:
        text = result.content[0].text if result.content else "{}"
        return json.loads(text)

    def _record(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {label}{(' — ' + detail) if detail else ''}")
        if not ok:
            failures += 1

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            tool_names = {t.name for t in tools.tools}
            expected = {
                "create_project",
                "register_session",
                "get_goal",
                "set_goal",
                "log_task",
                "get_tasks",
                "get_sessions",
                "generate_handoff",
            }
            _record(
                "list_tools advertises all eight",
                expected.issubset(tool_names),
                f"got {sorted(tool_names)}",
            )

            project_name = f"mcp-test-{uuid.uuid4().hex[:6]}"
            r = await session.call_tool(
                "create_project", {"name": project_name}
            )
            project = _result_payload(r)
            _record(
                "create_project", "id" in project, f"project={project}"
            )
            project_id = project.get("id", "")

            r = await session.call_tool(
                "register_session",
                {"project_id": project_id, "session_name": "mcp-session"},
            )
            sess = _result_payload(r)
            _record("register_session", "id" in sess, f"session={sess}")
            session_id = sess.get("id", "")

            r = await session.call_tool(
                "set_goal",
                {"project_id": project_id, "content": "verify MCP wiring"},
            )
            goal = _result_payload(r)
            _record(
                "set_goal",
                goal.get("version") == 1
                and goal.get("content") == "verify MCP wiring",
                f"goal={goal}",
            )

            r = await session.call_tool("get_goal", {"project_id": project_id})
            goal = _result_payload(r)
            _record(
                "get_goal",
                goal.get("content") == "verify MCP wiring",
            )

            r = await session.call_tool(
                "log_task",
                {
                    "session_id": session_id,
                    "project_id": project_id,
                    "description": "smoke task",
                    "status": "done",
                },
            )
            task = _result_payload(r)
            _record("log_task", "id" in task and task.get("status") == "done")

            r = await session.call_tool(
                "get_tasks", {"project_id": project_id, "limit": 5}
            )
            tasks = _result_payload(r)
            _record(
                "get_tasks",
                isinstance(tasks, list) and len(tasks) >= 1,
                f"count={len(tasks) if isinstance(tasks, list) else 'n/a'}",
            )

            r = await session.call_tool(
                "get_sessions", {"project_id": project_id}
            )
            sessions = _result_payload(r)
            _record(
                "get_sessions",
                isinstance(sessions, list)
                and any(s.get("id") == session_id for s in sessions),
            )

            r = await session.call_tool(
                "generate_handoff", {"project_id": project_id}
            )
            handoff = _result_payload(r)
            _record(
                "generate_handoff",
                "path" in handoff and "MERIDIAN_CONTEXT" in handoff.get(
                    "content", ""
                ),
            )

    print()
    if failures:
        print(f"{failures} tool(s) failed.")
        return 1
    print("All MCP tools passed.")
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
