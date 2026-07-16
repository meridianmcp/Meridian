#!/usr/bin/env python3
"""MCP tool smoke test -- db68159c.

Calls all 27 tools via the HTTP /mcp endpoint against a running Meridian server.
Each tool is called with minimal valid args. Verifies non-error response.
Prints PASS/FAIL per tool, exits non-zero if any fail.

Usage:
    pixi run python scripts/test_mcp_tools.py [--url http://localhost:7878]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
import urllib.error
import urllib.request
from typing import Any

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"

TEST_PROJECT_NAME = f"mcp-smoke-{uuid.uuid4().hex[:8]}"

# Known production hostnames -- the script refuses to run against these unless
# MERIDIAN_SMOKE_ALLOW_PROD=1 is set, because it creates real projects in the DB.
_PROD_HOSTNAMES = ("usemeridian.us", "meridian.us")


def _guard_not_prod(base_url: str) -> None:
    """Abort if the target URL looks like a known production hostname.

    This script creates real fixture data in whatever DB the server is connected
    to.  Running it against production risks polluting the live database.  Pass
    --url explicitly with a local or staging URL to proceed.  Set
    MERIDIAN_SMOKE_ALLOW_PROD=1 to override (unsafe).
    """
    lower = base_url.lower()
    for hostname in _PROD_HOSTNAMES:
        if hostname in lower:
            print(
                f"\nERROR: refusing to run smoke test against production URL"
                f" ({base_url!r} contains {hostname!r}).\n"
                f"Point --url at a local or staging instance instead.\n"
                f"Set MERIDIAN_SMOKE_ALLOW_PROD=1 to override (unsafe).\n"
            )
            if not os.environ.get("MERIDIAN_SMOKE_ALLOW_PROD"):
                sys.exit(1)


def _delete_project_rest(base_url: str, project_id: str) -> None:
    """Delete *project_id* via the REST endpoint DELETE /projects/{id}.

    Called in the finally block so the test project is removed even when the
    smoke test exits mid-run due to a check failure or an exception.
    This prevents test-fixture projects from accumulating in the database.
    """
    url = base_url.rstrip("/") + f"/projects/{project_id}"
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
        print(f"  [cleanup] Deleted test project {project_id}")
    except urllib.error.HTTPError as e:
        print(f"  [cleanup] DELETE /projects/{project_id} -> HTTP {e.code} (may already be gone)")
    except Exception as exc:
        print(f"  [cleanup] DELETE /projects/{project_id} failed: {exc}")


def rpc_call(url: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }
    raw = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=raw,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": {"code": e.code, "message": e.read().decode()}}
    except Exception as exc:
        return {"error": {"code": -1, "message": str(exc)}}


def is_ok(resp: dict[str, Any]) -> bool:
    if "error" in resp:
        return False
    content = resp.get("result", {}).get("content", [])
    if content:
        text = content[0].get("text", "")
        try:
            parsed = json.loads(text)
            # An error embedded in the content text
            if isinstance(parsed, dict) and "error" in parsed and "message" not in parsed:
                return False
        except Exception:
            pass
    return True


def get_result(resp: dict[str, Any]) -> Any:
    try:
        return json.loads(resp["result"]["content"][0]["text"])
    except Exception:
        return None


def check(label: str, ok: bool, detail: str = "") -> bool:
    status = PASS if ok else FAIL
    print(f"  [{status}] {label}" + (f" -- {detail}" if detail else ""))
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:7878", help="Meridian server base URL")
    args = parser.parse_args()

    _guard_not_prod(args.url)

    mcp_url = args.url.rstrip("/") + "/mcp"
    failures = 0
    project_id = None
    session_id = None
    hitl_id = None
    note_id = None
    decision_id = None
    sprint_note_session_id = None

    print(f"\nMCP tool smoke test (27 tools) -- {mcp_url}\n")

    # Initialize
    init_payload = json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "clientInfo": {"name": "smoke-test", "version": "1.0"}, "capabilities": {}}}).encode()
    req = urllib.request.Request(mcp_url, data=init_payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            pass
    except Exception:
        pass

    # 1. create_project
    resp = rpc_call(mcp_url, "create_project", {"name": TEST_PROJECT_NAME})
    ok = is_ok(resp)
    result = get_result(resp)
    if ok and result:
        project_id = result.get("id")
    if not check("create_project", ok, f"id={project_id}"):
        failures += 1

    if not project_id:
        print("  Cannot proceed without project_id -- aborting\n")
        return 1

    # All remaining checks run inside a try/finally so the project is always
    # deleted even when an assertion fails partway through or an exception is
    # raised.  This prevents test-fixture data from leaking into the database.
    try:
        # 2. start_session
        resp = rpc_call(mcp_url, "start_session", {"project_id": project_id, "session_name": "smoke-test"})
        ok = is_ok(resp)
        result = get_result(resp)
        if ok and result:
            session_id = result.get("session", {}).get("id") if isinstance(result.get("session"), dict) else None
        if not check("start_session", ok, f"session_id={session_id}"):
            failures += 1

        # 3. get_goal
        resp = rpc_call(mcp_url, "get_goal", {"project_id": project_id})
        if not check("get_goal", is_ok(resp)):
            failures += 1

        # 4. set_goal
        resp = rpc_call(mcp_url, "set_goal", {"project_id": project_id, "content": "smoke test goal"})
        if not check("set_goal", is_ok(resp)):
            failures += 1

        # 5. log_task
        if session_id:
            resp = rpc_call(mcp_url, "log_task", {"session_id": session_id, "project_id": project_id, "description": "smoke test task"})
            if not check("log_task", is_ok(resp)):
                failures += 1
        else:
            print(f"  [{SKIP}] log_task (no session_id)")

        # 6. get_tasks
        resp = rpc_call(mcp_url, "get_tasks", {"project_id": project_id})
        if not check("get_tasks", is_ok(resp)):
            failures += 1

        # 7. search_tasks
        resp = rpc_call(mcp_url, "search_tasks", {"project_id": project_id, "query": "smoke"})
        if not check("search_tasks", is_ok(resp)):
            failures += 1

        # 8. pin_decision
        resp = rpc_call(mcp_url, "pin_decision", {"project_id": project_id, "title": "smoke decision", "body": "test body", "category": "TECHNICAL"})
        ok = is_ok(resp)
        result = get_result(resp)
        if ok and result:
            decision_id = result.get("id")
        if not check("pin_decision", ok, f"id={decision_id}"):
            failures += 1

        # 9. get_pinned_decisions
        resp = rpc_call(mcp_url, "get_pinned_decisions", {"project_id": project_id})
        if not check("get_pinned_decisions", is_ok(resp)):
            failures += 1

        # 10. update_decision
        if decision_id:
            resp = rpc_call(mcp_url, "update_decision", {"decision_id": decision_id, "body": "updated body"})
            if not check("update_decision", is_ok(resp)):
                failures += 1
        else:
            print(f"  [{SKIP}] update_decision (no decision_id)")

        # 11. delete_decision
        if decision_id:
            resp = rpc_call(mcp_url, "delete_decision", {"decision_id": decision_id})
            if not check("delete_decision", is_ok(resp)):
                failures += 1

        # 12. request_hitl
        resp = rpc_call(mcp_url, "request_hitl", {"project_id": project_id, "question": "smoke test question"})
        ok = is_ok(resp)
        result = get_result(resp)
        if ok and result:
            hitl_id = result.get("id")
        if not check("request_hitl", ok, f"id={hitl_id}"):
            failures += 1

        # 13. list_hitl_requests
        resp = rpc_call(mcp_url, "list_hitl_requests", {"project_id": project_id})
        if not check("list_hitl_requests", is_ok(resp)):
            failures += 1

        # 14. get_hitl_request
        if hitl_id:
            resp = rpc_call(mcp_url, "get_hitl_request", {"request_id": hitl_id})
            if not check("get_hitl_request", is_ok(resp)):
                failures += 1
        else:
            print(f"  [{SKIP}] get_hitl_request (no hitl_id)")

        # 15. answer_hitl
        if hitl_id:
            resp = rpc_call(mcp_url, "answer_hitl", {"request_id": hitl_id, "answer": "smoke answer"})
            if not check("answer_hitl", is_ok(resp)):
                failures += 1

        # 16. dismiss_hitl (create a new one first)
        resp2 = rpc_call(mcp_url, "request_hitl", {"project_id": project_id, "question": "dismiss me"})
        dismiss_id = None
        r2 = get_result(resp2)
        if is_ok(resp2) and r2:
            dismiss_id = r2.get("id")
        if dismiss_id:
            resp = rpc_call(mcp_url, "dismiss_hitl", {"request_id": dismiss_id})
            if not check("dismiss_hitl", is_ok(resp)):
                failures += 1
        else:
            print(f"  [{SKIP}] dismiss_hitl")

        # 17. add_note
        resp = rpc_call(mcp_url, "add_note", {"project_id": project_id, "title": "smoke note", "body": "note body", "tags": "test"})
        ok = is_ok(resp)
        result = get_result(resp)
        if ok and result:
            note_id = result.get("id")
        if not check("add_note", ok, f"id={note_id}"):
            failures += 1

        # 18. get_notes
        resp = rpc_call(mcp_url, "get_notes", {"project_id": project_id})
        if not check("get_notes", is_ok(resp)):
            failures += 1

        # 19. delete_note
        if note_id:
            resp = rpc_call(mcp_url, "delete_note", {"note_id": note_id})
            if not check("delete_note", is_ok(resp)):
                failures += 1

        # 20. list_sessions
        resp = rpc_call(mcp_url, "list_sessions", {"project_id": project_id})
        if not check("list_sessions", is_ok(resp)):
            failures += 1

        # 21. add_sprint_note
        if session_id:
            resp = rpc_call(mcp_url, "add_sprint_note", {"session_id": session_id, "title": "smoke note", "body": "body"})
            sprint_note_session_id = session_id
            if not check("add_sprint_note", is_ok(resp)):
                failures += 1
        else:
            print(f"  [{SKIP}] add_sprint_note (no session_id)")

        # 22. get_sprint_notes
        if sprint_note_session_id:
            resp = rpc_call(mcp_url, "get_sprint_notes", {"session_id": sprint_note_session_id})
            if not check("get_sprint_notes", is_ok(resp)):
                failures += 1
        else:
            print(f"  [{SKIP}] get_sprint_notes (no session_id)")

        # 23. get_context_block
        resp = rpc_call(mcp_url, "get_context_block", {"project_id": project_id})
        if not check("get_context_block", is_ok(resp)):
            failures += 1

        # 24. get_session_brief
        resp = rpc_call(mcp_url, "get_session_brief", {"project_id": project_id, "role": "worker"})
        if not check("get_session_brief", is_ok(resp)):
            failures += 1

        # 25. checkpoint
        if session_id:
            resp = rpc_call(mcp_url, "checkpoint", {"session_id": session_id, "project_id": project_id})
            if not check("checkpoint", is_ok(resp)):
                failures += 1
        else:
            print(f"  [{SKIP}] checkpoint (no session_id)")

        # 26. generate_handoff
        resp = rpc_call(mcp_url, "generate_handoff", {"project_id": project_id, "mode": "delta"})
        if not check("generate_handoff", is_ok(resp)):
            failures += 1

        # 27. register_session
        resp = rpc_call(mcp_url, "register_session", {"project_id": project_id, "session_name": "register-smoke"})
        if not check("register_session", is_ok(resp)):
            failures += 1

        print(f"\n{'All checks passed.' if failures == 0 else f'{failures} check(s) FAILED.'}\n")
        return 0 if failures == 0 else 1

    finally:
        # Always delete the test project so fixture data never accumulates in
        # the database, even when the script exits mid-run due to a failure.
        print()
        _delete_project_rest(args.url, project_id)


if __name__ == "__main__":
    sys.exit(main())
