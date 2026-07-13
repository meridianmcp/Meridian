"""MCP tool dispatcher — extracted from server.py.

Routes ``tools/call`` requests to the appropriate db_module function and the
session composites. This is the core MCP surface: every tool call from every
AI session (HTTP /mcp, /mcp/sse, remote-MCP, and stdio) funnels through
``_dispatch_mcp_tool`` / ``_handle_mcp_request`` here.

Server-module helpers/constants are reached through ``_server`` (bound at call
time, never at import) to keep the server<->handler relationship non-circular.
server.py re-exports the public names so existing importers keep working.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

import meridian.server as _server
from .. import db as db_module
from .. import goal_md as goal_md_module
from .. import md_anchors as md_anchors_module
from .._deps import _hosted_mode, validate_input_size, _MANUAL_NOTE_LINT


def _json_default(o: Any) -> Any:
    """JSON fallback for MCP tool results. On Postgres the timestamp columns
    (completed_at / added_at / claimed_at / ...) come back as real ``datetime``
    objects, so the universal ``json.dumps(result)`` below raised "Object of type
    datetime is not JSON serializable" — which surfaced as a -32603 error on EVERY
    tool that returns a row carrying a timestamp (generate_handoff hit it in all
    modes on hosted PG). Coerce date/datetime/time to ISO strings; anything else to
    ``str`` so serialization can never hard-fail a tool call. (SQLite already returns
    these columns as strings, so this is a no-op there.)
    """
    import datetime as _dt  # noqa: PLC0415
    if isinstance(o, (_dt.datetime, _dt.date, _dt.time)):
        return o.isoformat()
    return str(o)


_MCP_PROMPTS: list[dict[str, Any]] = [
    {
        "name": "start-executor",
        "description": "Paste-ready instructions for starting an executor session on a project.",
        "arguments": [
            {"name": "project_id", "description": "Meridian project ID", "required": True},
        ],
    },
    {
        "name": "daily-standup",
        "description": "Standup summary prompt — shows shipped, in-progress, blocked, and next items.",
        "arguments": [
            {"name": "project_id", "description": "Meridian project ID", "required": True},
        ],
    },
    {
        "name": "planning-session-start",
        "description": (
            "Planning-session scaffold — the get_planning_brief protocol and "
            "tool-call order for a claude.ai planning chat."
        ),
        "arguments": [
            {"name": "project_id", "description": "Meridian project ID", "required": False},
            {"name": "project_name", "description": "Project name (used in the header when no id is given)", "required": False},
        ],
    },
    {
        "name": "executor-goal",
        "description": (
            "A /goal-style executor prompt built from the project's live pending "
            "sprint items. Pass project_id (or project_name) for the real items; "
            "omit both for a fill-in template."
        ),
        "arguments": [
            {"name": "project_id", "description": "Meridian project ID — renders the live pending sprint items", "required": False},
            {"name": "project_name", "description": "Project name — resolved to an id when project_id is omitted", "required": False},
        ],
    },
    {
        "name": "hotfix-loop",
        "description": (
            "The read → edit → push hotfix protocol for a fast, test-gated fix on "
            "an existing Meridian project."
        ),
        "arguments": [
            {"name": "project_id", "description": "Meridian project ID", "required": False},
        ],
    },
]


def _one_user_message(text: str) -> list[dict[str, Any]]:
    """Wrap a single prompt body in the MCP user-message envelope."""
    return [{"role": "user", "content": {"type": "text", "text": text}}]


async def _build_prompt_messages_async(
    name: str, args: dict[str, Any], db: Any,
) -> list[dict[str, Any]]:
    """Async prompt builder — resolves dynamic prompts against the DB.

    ``executor-goal`` pulls the project's live pending sprint items (reusing the
    same goal builder generate_handoff uses) so the slash-command template
    materialises a ready-to-run /goal string. Every other prompt is static and
    falls through to the synchronous :func:`_build_prompt_messages`. All DB work
    is guarded — a missing/unknown project degrades to the instructional
    template instead of raising.
    """
    if name == "executor-goal":
        return await _build_executor_goal_messages(args, db)
    return _build_prompt_messages(name, args)


async def _build_executor_goal_messages(
    args: dict[str, Any], db: Any,
) -> list[dict[str, Any]]:
    """Build the executor-goal prompt from live pending sprint items.

    Resolves the project from ``project_id`` (preferred) or ``project_name``.
    When neither resolves to a project — or the project has no pending items —
    returns a clear fill-in template rather than an error, so the slash command
    is always useful even on a cold/empty project.
    """
    from ..handoff import _build_quick_start_goal, _prepare_pending_sprint_items

    project: dict[str, Any] | None = None
    project_id = (args.get("project_id") or "").strip()
    project_name = (args.get("project_name") or "").strip()
    if db is not None:
        try:
            if project_id:
                project = await db_module.get_project(db, project_id)
            elif project_name:
                project = await db_module.get_project_by_name(db, project_name)
        except Exception:  # noqa: BLE001 — degrade to template, never crash the prompt
            project = None

    if project is None:
        # No project resolved → instructional template, not an error.
        label = project_id or project_name or "<project_id>"
        return _one_user_message(
            f"Run an executor session for Meridian project `{label}`.\n\n"
            "This is the template form — pass a real project_id (or project_name) "
            "to materialise the live pending sprint items here.\n\n"
            f"1. `start_session(project_id=\"{label}\", session_name=\"<brief task>\")` "
            "— returns sprint focus, pending items, and recent tasks.\n"
            "2. For each pending item: "
            f"`claim_sprint_item(project_id=\"{label}\", item_id=\"<id>\")`, do the work, "
            "run the test suite, then `complete_sprint_item(...)`. Do NOT ask what to work on.\n"
            "3. `log_task(session_id=..., project_id=..., description=...)` after each meaningful step.\n"
            "4. Between items call "
            f"`get_sprint_progress(project_id=\"{label}\", session_id=...)` to pick up new items.\n"
            "5. Before ending: `checkpoint(session_id=..., project_id=...)`.\n\n"
            "/goal Complete the pending sprint items. Done when every claimed item is "
            "marked complete via complete_sprint_item(), the test suite passes, and "
            "generate_handoff() is called at the end. Stop after 200 turns or if HITL triggered.\n\n"
            "Push to dev when tests pass. Never push broken code."
        )

    pid = project["id"]
    pname = project.get("name") or pid
    pending: list[dict[str, Any]] = []
    # a76cb7c0 — scope the /goal template to the active sprint-version bucket so
    # it matches what start_session(version=None) will auto-scope to (the bucket
    # with the most pending items). Best-effort: None → no filter.
    scoped_version: str | None = None
    try:
        scoped_version = await db_module.infer_active_sprint_version(db, pid)
    except Exception:  # noqa: BLE001 — degrade to unscoped
        scoped_version = None
    try:
        # Treat 'todo' + 'pending' as the pending bucket, exactly like
        # generate_handoff, so freshly added items (status 'pending') and any
        # 'todo' items both surface. Human-typed items are excluded. The version
        # filter (when known) keeps the template focused on one bucket.
        all_items = await db_module.get_sprint_items(
            db, pid, include_human=False, version=scoped_version,
        )
        pending = [
            it for it in all_items
            if it.get("status") in ("todo", "pending")
        ]
        pending = _prepare_pending_sprint_items(pending)
    except Exception:  # noqa: BLE001 — empty list still renders a valid goal
        pending = []

    # d2c47f43 — read the project's max_turns override for the /goal ceiling.
    from ..handoff import _max_turns_from_settings  # noqa: PLC0415
    try:
        _goal_settings = await db_module.get_project_settings(db, pid)
    except Exception:  # noqa: BLE001
        _goal_settings = None
    quick_start_goal = _build_quick_start_goal(
        pending,
        version=scoped_version,
        # ecf69de8 — the project's executor posture selects the /goal framing.
        execution_mode=db_module.normalize_execution_mode(
            project.get("execution_mode")
        ),
        max_turns=_max_turns_from_settings(_goal_settings),
    )
    if pending:
        item_lines = "\n".join(
            f"  {i}. [{it['id']}] {(it.get('title') or '').strip()}"
            for i, it in enumerate(pending, 1)
        )
        items_block = f"Pending sprint items ({len(pending)}):\n{item_lines}\n\n"
    else:
        items_block = (
            "No pending sprint items right now — verify the sprint is complete "
            "or ask the planner for the next batch.\n\n"
        )

    return _one_user_message(
        f"Run an executor session for Meridian project `{pname}` (`{pid}`).\n\n"
        f"1. `start_session(project_id=\"{pid}\", session_name=\"<brief task>\")`.\n"
        "2. Work the pending items below in order — "
        f"`claim_sprint_item(project_id=\"{pid}\", item_id=...)`, do the work, run the "
        "test suite, then `complete_sprint_item(...)`. Do NOT ask what to work on.\n"
        "3. `log_task(session_id=..., project_id=..., description=...)` after each meaningful step.\n"
        "4. Between items call "
        f"`get_sprint_progress(project_id=\"{pid}\", session_id=...)` to pick up new items.\n"
        "5. Before ending: `checkpoint(session_id=..., project_id=...)`.\n\n"
        f"{items_block}"
        f"{quick_start_goal}\n\n"
        "Push to dev when tests pass. Never push broken code."
    )


def _build_prompt_messages(name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    pid = args.get("project_id", "<project_id>")
    if name == "planning-session-start":
        pid_arg = (args.get("project_id") or "").strip()
        pname = (args.get("project_name") or "").strip()
        header = pname or pid_arg or "this project"
        pid_for_calls = pid_arg or "<project_id>"
        return _one_user_message(
            f"You are the **planner** for Meridian project {header}. Your job is "
            "not to write code — it is to decide *what* should be built next and "
            "in what order, then record those decisions in Meridian so executor "
            "sessions can pick them up.\n\n"
            "Planning protocol — call these tools in this order:\n"
            f"1. `get_planning_brief(project_id=\"{pid_for_calls}\")` — load the compact "
            "current state (sprint, north star, pending + in-progress items, recent "
            "tasks, active sessions, pending HITLs).\n"
            f"2. `get_sprint_progress(project_id=\"{pid_for_calls}\")` — see how much of the "
            "current sprint is done vs. outstanding.\n"
            f"3. `list_hitl_requests(project_id=\"{pid_for_calls}\")` — review open human "
            "decisions blocking progress; answer or supersede them.\n"
            f"4. `get_pinned_decisions(project_id=\"{pid_for_calls}\")` — re-read the "
            "constitution so new items don't contradict prior choices.\n"
            "5. Think through the scaffold below (current state → gaps & risks → "
            "priorities → proposed items → open questions).\n"
            f"6. `add_sprint_item(project_id=\"{pid_for_calls}\", title=\"...\", group=\"...\")` "
            "— record each proposed next item.\n"
            f"7. `pin_decision(project_id=\"{pid_for_calls}\", title=\"...\", body=\"...\", "
            "category=\"...\")` — record any architectural / scope decisions you made.\n\n"
            "Thinking scaffold — fill in before recording items/decisions:\n"
            "- **Current state** — what's shipped, what's in flight.\n"
            "- **Gaps & risks** — what's missing, fragile, or blocking.\n"
            "- **Priorities** — the 1-3 things that matter most next, ranked.\n"
            "- **Proposed next sprint items** — concrete items (title + group each).\n"
            "- **Open questions** — anything needing a human decision (request_hitl if blocking)."
        )
    if name == "hotfix-loop":
        pid_arg = (args.get("project_id") or "").strip() or "<project_id>"
        return _one_user_message(
            f"Run a hotfix loop on Meridian project `{pid_arg}` — a fast, "
            "test-gated read → edit → push cycle for a single focused fix.\n\n"
            f"1. `start_session(project_id=\"{pid_arg}\", session_name=\"hotfix: <what>\")` "
            "to register and load current context.\n"
            "2. **Read** — locate the bug. Read the failing file(s) and any test that "
            "covers the area BEFORE editing; reproduce the failure first so you know "
            "the fix worked.\n"
            "3. `claim_file(session_id=..., file_path=...)` for each file you'll touch so "
            "a parallel session doesn't collide.\n"
            "4. **Edit** — make the minimal change that fixes the issue. No scope creep; "
            "a hotfix touches as little as possible.\n"
            "5. Run the full test suite and confirm it is green (do not reduce coverage). "
            "Add or update a regression test for the bug.\n"
            "6. `log_task(session_id=..., project_id=..., description=\"hotfix: ...\")` and "
            "`release_file(...)` the files you claimed.\n"
            "7. **Push** — commit and push to the dev branch only. Never push broken code, "
            "and never push straight to main.\n"
            "8. If anything is ambiguous or risky, "
            f"`request_hitl(project_id=\"{pid_arg}\", question=...)` before pushing."
        )
    if name == "start-executor":
        return [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        f"Start an executor session for Meridian project `{pid}`.\n\n"
                        f"1. Call `start_session(project_id=\"{pid}\", session_name=\"<brief task>\")`"
                        " — returns sprint focus, pending items, and recent tasks.\n"
                        "2. If there are pending sprint items, immediately call "
                        f"`claim_sprint_item(project_id=\"{pid}\", item_id=\"<first pending item id>\")`"
                        " and start working. Do NOT ask what to work on.\n"
                        "3. After each meaningful action call "
                        f"`log_task(session_id=..., project_id=\"{pid}\", description=...)`.\n"
                        "4. When an item is done: run tests, call `complete_sprint_item`, then call "
                        f"`get_sprint_progress(project_id=\"{pid}\", session_id=...)` to pick up any new items.\n"
                        "5. Before ending: call "
                        f"`checkpoint(session_id=..., project_id=\"{pid}\")` — snapshots progress "
                        "and returns the next `/goal` string.\n\n"
                        "Push to dev branch when tests pass. Never push broken code."
                    ),
                },
            }
        ]
    if name == "daily-standup":
        return [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        f"Provide a standup summary for Meridian project `{pid}`.\n\n"
                        f"Call `get_planning_brief(project_id=\"{pid}\")` to get current state, "
                        "then format as:\n\n"
                        "**Shipped** (done since yesterday)\n"
                        "**In Progress** (currently in_progress)\n"
                        "**Blocked** (failed items, pending HITLs)\n"
                        "**Next** (top 3 pending items)\n\n"
                        "Keep it concise — one line per item."
                    ),
                },
            }
        ]
    raise ValueError(f"unknown prompt: {name}")


async def _dispatch_github_tool(name: str, args: dict[str, Any], tenant: dict, db: Any) -> Any:
    # Guard: check if project has a GitHub repo connected
    project_id = args.get("project_id")
    if project_id:
        try:
            proj = await db_module.get_project(db, project_id)
            if proj and not proj.get("github_repo"):
                return {
                    "error": "no_github_repo",
                    "message": f"No GitHub repo connected for project {project_id}. "
                               f"Go to Settings → Connect GitHub repo to connect one.",
                }
        except Exception:
            pass
    """Dispatch a GitHub MCP tool call using the tenant's PAT and per-project repo."""
    import httpx as _httpx
    import base64 as _b64
    pat = db_module.decrypt_field(tenant.get("github_pat"))
    if not pat:
        return {"error": "GitHub not connected — connect via Settings > Connect Claude Code > GitHub"}
    project_id = (args.get("project_id") or "").strip()
    if not project_id:
        return {"error": "project_id is required — pass the project whose GitHub repo you want to read"}
    project = await db_module.get_project(db, project_id)
    if project is None:
        return {"error": f"project '{project_id}' not found"}
    repo = (project.get("github_repo") or "").strip()
    branch = (project.get("github_branch") or "main").strip()
    if not repo:
        return {"error": f"No GitHub repo connected for project {project_id} — use POST /projects/{project_id}/github/connect"}
    gh_headers = {"Authorization": f"token {pat}", "Accept": "application/vnd.github+json"}
    async with _httpx.AsyncClient(timeout=15.0) as http:
        if name == "read_file":
            path = args.get("path", "")
            ref = args.get("ref") or branch
            r = await http.get(
                f"https://api.github.com/repos/{repo}/contents/{path}",
                headers=gh_headers,
                params={"ref": ref},
            )
            if r.status_code == 404:
                return {"error": f"File not found: {path}"}
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                return {"entries": [{"name": e["name"], "type": e["type"], "path": e["path"]} for e in data]}
            content_b64 = data.get("content", "")
            content = _b64.b64decode(content_b64).decode("utf-8", errors="replace")
            return {"path": data["path"], "sha": data["sha"], "size": data["size"], "content": content}

        if name == "patch_file":
            # Targeted write: exact, unique substring replacement committed back via
            # the contents API. Only the changed snippet crosses the wire, so it
            # edits very large files trivially — the write counterpart to read_file.
            path = (args.get("file_path") or args.get("path") or "").strip()
            old_str = args.get("old_str")
            new_str = args.get("new_str")
            if not path:
                return {"error": "file_path is required"}
            if not isinstance(old_str, str) or not isinstance(new_str, str):
                return {"error": "old_str and new_str are required strings"}
            if old_str == new_str:
                return {"error": "old_str and new_str are identical — nothing to change"}
            ref = (args.get("branch") or branch).strip()
            r = await http.get(
                f"https://api.github.com/repos/{repo}/contents/{path}",
                headers=gh_headers, params={"ref": ref},
            )
            if r.status_code == 404:
                return {"error": f"File not found: {path} (branch {ref})"}
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                return {"error": f"{path} is a directory, not a file"}
            try:
                content = _b64.b64decode(data.get("content", "")).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return {"error": f"{path} is not valid UTF-8 text — patch_file only supports text files"}
            # 6efe7649 — lock enforcement: reject if file is held by another session.
            _caller_session = (args.get("session_id") or "").strip()
            if _caller_session:
                async with db.execute(
                    "SELECT session_id FROM file_locks "
                    "WHERE file_path = ? AND expires_at > datetime('now')",
                    (path,),
                ) as _lk_cur:
                    _lk_row = await _lk_cur.fetchone()
                if _lk_row is not None:
                    _holder = (
                        _lk_row["session_id"]
                        if hasattr(_lk_row, "keys")
                        else _lk_row[0]
                    )
                    if _holder and _holder != _caller_session:
                        return {"error": f"file locked by session {_holder[:8]}"}
            occurrences = content.count(old_str)
            if occurrences == 0:
                return {"error": "old_str not found — it must match the file contents exactly (including whitespace)"}
            if occurrences > 1:
                return {"error": f"old_str is not unique ({occurrences} matches) — add surrounding context to target a single location"}
            new_content = content.replace(old_str, new_str, 1)
            message = (args.get("message") or f"patch_file: update {path}").strip()
            put = await http.put(
                f"https://api.github.com/repos/{repo}/contents/{path}",
                headers=gh_headers,
                json={
                    "message": message,
                    "content": _b64.b64encode(new_content.encode("utf-8")).decode(),
                    "sha": data.get("sha"),
                    "branch": ref,
                },
            )
            if put.status_code not in (200, 201):
                return {"error": f"write failed ({put.status_code}): {put.text[:200]}"}
            commit = (put.json() or {}).get("commit", {})
            return {
                "patched": True,
                "path": path,
                "branch": ref,
                "commit_sha": (commit.get("sha") or "")[:12],
                "bytes_before": len(content.encode("utf-8")),
                "bytes_after": len(new_content.encode("utf-8")),
            }

        if name == "list_files":
            path = args.get("path") or ""
            r = await http.get(
                f"https://api.github.com/repos/{repo}/git/trees/HEAD",
                headers=gh_headers,
                params={"recursive": "1"},
            )
            r.raise_for_status()
            tree = r.json().get("tree", [])
            files = [e["path"] for e in tree if e.get("type") == "blob"]
            if path:
                files = [f for f in files if f.startswith(path)]
            return {"repo": repo, "count": len(files), "files": files}

        if name == "search_code":
            query = args.get("query", "")
            r = await http.get(
                "https://api.github.com/search/code",
                headers=gh_headers,
                params={"q": f"{query} repo:{repo}"},
            )
            r.raise_for_status()
            items = r.json().get("items", [])
            return {
                "total_count": r.json().get("total_count", 0),
                "items": [{"path": i["path"], "sha": i["sha"], "url": i.get("html_url", "")} for i in items[:20]],
            }

        if name == "get_commits":
            limit = min(int(args.get("limit") or 50), 50)
            params: dict[str, str] = {"per_page": str(limit)}
            if args.get("since"):
                params["since"] = args["since"]
            r = await http.get(
                f"https://api.github.com/repos/{repo}/commits",
                headers=gh_headers,
                params=params,
            )
            r.raise_for_status()
            commits = r.json()
            return {
                "commits": [
                    {
                        "sha": c["sha"][:12],
                        "message": c["commit"]["message"].split("\n")[0],
                        "author": c["commit"]["author"]["name"],
                        "date": c["commit"]["author"]["date"],
                    }
                    for c in commits
                ]
            }

        if name == "search_commits":
            query = (args.get("query") or "").lower()
            limit = min(int(args.get("limit") or 20), 100)
            r = await http.get(
                f"https://api.github.com/repos/{repo}/commits",
                headers=gh_headers,
                params={"per_page": "100"},
            )
            r.raise_for_status()
            all_commits = r.json()
            matched = [
                {
                    "sha": c["sha"][:12],
                    "message": c["commit"]["message"].split("\n")[0],
                    "author": c["commit"]["author"]["name"],
                    "date": c["commit"]["author"]["date"],
                }
                for c in all_commits
                if query in c["commit"]["message"].lower()
            ][:limit]
            return {"query": args.get("query"), "count": len(matched), "commits": matched}

        if name == "get_commit":
            sha = args.get("sha", "")
            r = await http.get(
                f"https://api.github.com/repos/{repo}/commits/{sha}",
                headers=gh_headers,
            )
            if r.status_code == 404:
                return {"error": f"Commit not found: {sha}"}
            r.raise_for_status()
            c = r.json()
            files = [{"filename": f["filename"], "status": f["status"], "additions": f.get("additions", 0), "deletions": f.get("deletions", 0)} for f in c.get("files", [])[:50]]
            return {
                "sha": c["sha"],
                "message": c["commit"]["message"],
                "author": c["commit"]["author"]["name"],
                "date": c["commit"]["author"]["date"],
                "files_changed": len(c.get("files", [])),
                "files": files,
            }

        if name == "get_workflow_runs":
            limit = min(int(args.get("limit") or 10), 50)
            wf = (args.get("workflow_name") or "").strip()
            url = (
                f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/runs"
                if wf else
                f"https://api.github.com/repos/{repo}/actions/runs"
            )
            r = await http.get(url, headers=gh_headers, params={"per_page": str(limit)})
            if r.status_code == 404:
                return {"error": f"Workflow not found: {wf}" if wf else "No Actions runs found"}
            r.raise_for_status()
            runs = r.json().get("workflow_runs", [])
            return {
                "repo": repo,
                "count": len(runs),
                "runs": [
                    {
                        "id": run["id"],
                        "name": run.get("name"),
                        "status": run.get("status"),
                        "conclusion": run.get("conclusion"),
                        "created_at": run.get("created_at"),
                        "html_url": run.get("html_url"),
                    }
                    for run in runs
                ],
            }

        if name == "get_workflow_run_logs":
            run_id = str(args.get("run_id") or "").strip()
            r = await http.get(
                f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs",
                headers=gh_headers,
            )
            if r.status_code == 404:
                return {"error": f"Run not found: {run_id}"}
            r.raise_for_status()
            jobs = r.json().get("jobs", [])
            failed = [j for j in jobs if j.get("conclusion") == "failure"]
            out = []
            for j in failed:
                failed_steps = [
                    {"name": s.get("name"), "number": s.get("number")}
                    for s in j.get("steps", [])
                    if s.get("conclusion") == "failure"
                ]
                log_excerpt = ""
                try:
                    lr = await http.get(
                        f"https://api.github.com/repos/{repo}/actions/jobs/{j['id']}/logs",
                        headers=gh_headers,
                        follow_redirects=True,
                    )
                    if lr.status_code == 200:
                        lines = lr.text.splitlines()
                        log_excerpt = "\n".join(lines[-50:])
                except Exception:  # noqa: BLE001 — logs are best-effort
                    log_excerpt = ""
                out.append({
                    "job": j.get("name"),
                    "job_id": j.get("id"),
                    "html_url": j.get("html_url"),
                    "failed_steps": failed_steps,
                    "log_tail": log_excerpt,
                })
            return {"run_id": run_id, "failed_job_count": len(failed), "failed_jobs": out}

        if name == "trigger_workflow":
            wf = (args.get("workflow_name") or "").strip()
            ref = (args.get("ref") or branch).strip()
            inputs = args.get("inputs") or {}
            body: dict[str, Any] = {"ref": ref}
            if inputs:
                body["inputs"] = inputs
            r = await http.post(
                f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/dispatches",
                headers=gh_headers,
                json=body,
            )
            if r.status_code == 404:
                return {"error": f"Workflow not found: {wf}"}
            if r.status_code not in (201, 204):
                return {"error": f"Dispatch failed ({r.status_code}): {r.text[:200]}"}
            return {"dispatched": True, "workflow": wf, "ref": ref, "inputs": inputs}

        if name == "git_diff":
            base = (args.get("base") or "").strip()
            head = (args.get("head") or "").strip()
            r = await http.get(
                f"https://api.github.com/repos/{repo}/compare/{base}...{head}",
                headers=gh_headers,
            )
            if r.status_code == 404:
                return {"error": f"Refs not found: {base}...{head}"}
            r.raise_for_status()
            data = r.json()
            files = [
                {
                    "filename": f["filename"],
                    "status": f.get("status"),
                    "additions": f.get("additions", 0),
                    "deletions": f.get("deletions", 0),
                    "patch": f.get("patch", ""),
                }
                for f in data.get("files", [])[:50]
            ]
            return {
                "base": base,
                "head": head,
                "total_commits": data.get("total_commits", 0),
                "files": files,
            }

        if name == "list_branches":
            r = await http.get(
                f"https://api.github.com/repos/{repo}/branches",
                headers=gh_headers,
                params={"per_page": "100"},
            )
            r.raise_for_status()
            branches = r.json()
            return {
                "repo": repo,
                "count": len(branches),
                "branches": [
                    {
                        "name": b["name"],
                        "sha": b.get("commit", {}).get("sha", "")[:12],
                        "protected": b.get("protected", False),
                    }
                    for b in branches
                ],
            }

        if name == "list_issues":
            state = (args.get("state") or "open").strip()
            limit = min(int(args.get("limit") or 20), 50)
            params = {"state": state, "per_page": str(limit)}
            if args.get("labels"):
                params["labels"] = args["labels"]
            r = await http.get(
                f"https://api.github.com/repos/{repo}/issues",
                headers=gh_headers,
                params=params,
            )
            r.raise_for_status()
            issues = r.json()
            return {
                "repo": repo,
                "state": state,
                "issues": [
                    {
                        "number": i["number"],
                        "title": i.get("title"),
                        "state": i.get("state"),
                        "labels": [lbl["name"] for lbl in i.get("labels", [])],
                        "created_at": i.get("created_at"),
                        "html_url": i.get("html_url"),
                        "body_preview": (i.get("body") or "")[:200],
                    }
                    for i in issues
                    if "pull_request" not in i  # exclude PRs
                ],
            }

        if name == "create_issue":
            title = (args.get("title") or "").strip()
            if not title:
                return {"error": "title is required"}
            body_payload: dict[str, Any] = {"title": title, "body": args.get("body") or ""}
            if args.get("labels"):
                body_payload["labels"] = args["labels"]
            r = await http.post(
                f"https://api.github.com/repos/{repo}/issues",
                headers=gh_headers,
                json=body_payload,
            )
            if r.status_code not in (200, 201):
                return {"error": f"Create issue failed ({r.status_code}): {r.text[:200]}"}
            i = r.json()
            return {
                "number": i["number"],
                "title": i.get("title"),
                "state": i.get("state"),
                "html_url": i.get("html_url"),
            }

        if name == "get_issue":
            number = str(args.get("number") or "").strip()
            r = await http.get(
                f"https://api.github.com/repos/{repo}/issues/{number}",
                headers=gh_headers,
            )
            if r.status_code == 404:
                return {"error": f"Issue not found: {number}"}
            r.raise_for_status()
            i = r.json()
            comments = []
            try:
                cr = await http.get(
                    f"https://api.github.com/repos/{repo}/issues/{number}/comments",
                    headers=gh_headers,
                    params={"per_page": "30"},
                )
                if cr.status_code == 200:
                    comments = [
                        {
                            "author": c.get("user", {}).get("login"),
                            "created_at": c.get("created_at"),
                            "body": (c.get("body") or "")[:500],
                        }
                        for c in cr.json()
                    ]
            except Exception:  # noqa: BLE001 — comments are best-effort
                comments = []
            return {
                "number": i["number"],
                "title": i.get("title"),
                "state": i.get("state"),
                "labels": [lbl["name"] for lbl in i.get("labels", [])],
                "body": i.get("body") or "",
                "html_url": i.get("html_url"),
                "comments": comments,
            }

    return {"error": f"Unknown GitHub tool: {name}"}


_MERIDIAN_TOOL_NAMES_CACHE: "frozenset[str] | None" = None


def _meridian_tool_names() -> "frozenset[str]":
    """Set of native Meridian MCP tool names (cached after first build).

    Used by the tunnel bridge to tell native tools apart from tunneled ones so
    native tools always take precedence and only genuinely-new tunnel tools are
    routed over the WebSocket relay.
    """
    global _MERIDIAN_TOOL_NAMES_CACHE
    if _MERIDIAN_TOOL_NAMES_CACHE is None:
        _MERIDIAN_TOOL_NAMES_CACHE = frozenset(
            t["name"] for t in _server._MCP_TOOLS_LIST if t.get("name")
        )
    return _MERIDIAN_TOOL_NAMES_CACHE


async def _handle_mcp_request(
    body: dict[str, Any], db: Any, data_dir: str,
    tenant: dict[str, Any] | None = None,
    token_type: str = "readwrite",
    enforce_role: str | None = None,
    scoped_project_ids: "list[str] | None" = None,
) -> dict[str, Any]:
    """Dispatch one JSON-RPC 2.0 MCP request and return the response dict.

    ``enforce_role`` (393eed0a) — when set, the caller is acting in a workspace
    they were INVITED to under that role; write tools are denied unless the role
    carries PERM_WRITE. Defaults to None (owner / self-host) → no gate, so the
    live claude.ai connector path is unaffected.
    """
    req_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params") or {}

    if method == "initialize":
        return _server._jsonrpc_ok(req_id, {
            "protocolVersion": _server._MCP_PROTOCOL_VERSION,
            "serverInfo": _server._MCP_SERVER_INFO,
            "capabilities": {"tools": {}, "prompts": {}},
        })

    if method in ("notifications/initialized", "ping"):
        return _server._jsonrpc_ok(req_id, {})

    if method == "tools/list":
        tools = list(_server._MCP_TOOLS_LIST)
        if tenant:
            tools = tools + _server._github_tools_for_tenant(tenant)
        # 7033c8e2 — when a tunnel is connected but its tools don't load, DON'T fail
        # silently (the old behaviour: user's filesystem/code/office tools vanish
        # with zero explanation, confirmed live across claude.ai + Claude Desktop).
        # Attach a machine-readable degraded/error signal to the result `_meta` so
        # a client or the dashboard can surface "connected but tools unavailable"
        # instead of an unexplained short list. The native tools/list itself is
        # never broken by a tunnel hiccup.
        tunnel_health = None
        # Single-connector bridge: when this tenant has a live `meridian --tunnel`,
        # surface its filesystem / code-intel / extractor tools here so the user's
        # existing Meridian connector gains them with zero extra config. Reserve
        # native + GitHub names so the merged list has no duplicates.
        if tenant and tenant.get("id"):
            from ..routes import tunnel as _tunnel_mod  # noqa: PLC0415
            if not _tunnel_mod.has_active_tunnel(tenant["id"]) and \
                    _tunnel_mod.tunnel_cross_instance_miss(tenant):
                # a19538fe — DB says the tunnel is active but its socket is on a
                # sibling Fly instance; this instance can't list its tools. Signal
                # it legibly (reconnecting) instead of silently omitting them.
                tunnel_health = {
                    "status": "reconnecting",
                    "message": _tunnel_mod.CROSS_INSTANCE_MISS_MESSAGE,
                }
            if _tunnel_mod.has_active_tunnel(tenant["id"]):
                try:
                    # Reserve native names AND the full GitHub name set: tools/call
                    # always routes GitHub names (e.g. read_file) to the GitHub
                    # dispatch when a tenant is set, so the tunnel must not advertise
                    # them here or list/call would disagree.
                    reserved = {t.get("name") for t in tools} | set(_server._GITHUB_TOOL_NAMES)
                    tunnel_tools = await _tunnel_mod.list_tunnel_tools(
                        tenant["id"], reserved,
                    )
                    tools = tools + tunnel_tools
                    if not tunnel_tools:
                        # Tunnel is up but advertised nothing — a slot is still
                        # starting or failed its pre-flight health check.
                        tunnel_health = {
                            "status": "degraded",
                            "message": (
                                "Meridian tunnel is connected but returned 0 plugin tools "
                                "(filesystem / code-intel / office). A slot may still be "
                                "starting or failed its pre-flight health check — retry "
                                "shortly or check the `meridian --tunnel` process."
                            ),
                        }
                except Exception as exc:  # noqa: BLE001
                    # tunnel hiccup must never break native tools/list — but signal it.
                    import sys as _sys  # noqa: PLC0415
                    print(
                        f"tools/list: tunnel tool fetch failed for tenant "
                        f"{tenant.get('id')}: {exc!r}",
                        file=_sys.stderr, flush=True,
                    )
                    tunnel_health = {
                        "status": "error",
                        "message": (
                            "Meridian tunnel is connected but its plugin tools could not "
                            "be loaded this request; only native tools are listed. Retry, "
                            "or check the tunnel process."
                        ),
                        "detail": str(exc)[:200],
                    }
        result: dict = {"tools": tools}
        if tunnel_health is not None:
            result["_meta"] = {"meridian/tunnelHealth": tunnel_health}
        return _server._jsonrpc_ok(req_id, result)

    if method == "prompts/list":
        return _server._jsonrpc_ok(req_id, {"prompts": _MCP_PROMPTS})

    if method == "prompts/get":
        prompt_name = params.get("name", "")
        prompt_args = params.get("arguments") or {}
        try:
            messages = await _build_prompt_messages_async(prompt_name, prompt_args, db)
        except ValueError as exc:
            return _server._jsonrpc_err(req_id, -32602, str(exc))
        return _server._jsonrpc_ok(req_id, {"description": next(
            (p["description"] for p in _MCP_PROMPTS if p["name"] == prompt_name), ""
        ), "messages": messages})

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        if token_type == "readonly" and name not in _server._mcp_readonly_tools:
            return _server._jsonrpc_err(req_id, -32603, f"tool '{name}' not allowed for read-only tokens")
        # 95499c3e / decision 6fe5210c — Option A project-scope enforcement for
        # API tokens (mirrors the HTTP middleware). A scoped member may only touch
        # projects in their scope; resolve the tool's target project (by project_id,
        # else project_name → id) and deny when it's out of scope.
        if scoped_project_ids is not None:
            _tgt_pid = (args.get("project_id") or "").strip()
            if not _tgt_pid and args.get("project_name"):
                try:
                    _sp = await db_module.get_project_by_name(db, str(args["project_name"]))
                    _tgt_pid = (_sp or {}).get("id", "") if _sp else ""
                except Exception:  # noqa: BLE001
                    _tgt_pid = ""
            if _tgt_pid and _tgt_pid not in scoped_project_ids:
                return _server._jsonrpc_err(
                    req_id, -32603, "project is outside your access scope",
                )
        # 393eed0a — workspace-role gate (defense in depth for cross-workspace MCP).
        if enforce_role is not None and name not in _server._mcp_readonly_tools \
                and name not in _server._GITHUB_TOOL_NAMES:
            from ..roles import has_perm, PERM_WRITE  # noqa: PLC0415
            if not has_perm(enforce_role, PERM_WRITE):
                return _server._jsonrpc_err(
                    req_id, -32603,
                    f"workspace role '{enforce_role}' is read-only; tool '{name}' denied",
                )
        try:
            _is_github = bool(name in _server._GITHUB_TOOL_NAMES and tenant)
            # Single-connector bridge: a tool that is neither a native Meridian tool
            # nor a GitHub tool may belong to an active tunnel (fs/code/extractor).
            # Native + GitHub tools keep precedence so existing behaviour is unchanged.
            if not _is_github and name not in _meridian_tool_names() and tenant and tenant.get("id"):
                from ..routes import tunnel as _tunnel_mod  # noqa: PLC0415
                if _tunnel_mod.has_active_tunnel(tenant["id"]):
                    # 73d233e4 — pass db + the caller's session_id so the word/office
                    # (docx) write path can consult file claims and refuse a
                    # concurrent-write conflict instead of silently last-save-wins
                    # overwriting another session's edit to the same document.
                    tunnel_result = await _tunnel_mod.call_tunnel_tool(
                        tenant["id"], name, args,
                        db=db, session_id=(args.get("session_id") or "").strip() or None,
                    )
                    if tunnel_result is not None:
                        # Pass the tunneled server's result through verbatim — it
                        # already carries the MCP `content` envelope.
                        return _server._jsonrpc_ok(req_id, tunnel_result)
                elif _tunnel_mod.tunnel_cross_instance_miss(tenant):
                    # a19538fe — the tunnel IS active (DB flag) but its socket is
                    # on a sibling Fly instance, so THIS instance can't forward.
                    # Fail legibly instead of falling through to a misleading
                    # "unknown tool" (a non-native/non-GitHub name here is very
                    # likely one of that tunnel's tools we just can't reach).
                    return _server._jsonrpc_err(
                        req_id, -32002, _tunnel_mod.CROSS_INSTANCE_MISS_MESSAGE,
                    )
            if _is_github:
                result = await _dispatch_github_tool(name, args, tenant, db)
            else:
                result = await _dispatch_mcp_tool(name, args, db, data_dir, tenant=tenant)
                # 4b698ea5 — implicit last_seen bump on the HOSTED path, mirroring
                # the stdio handler. Previously ONLY stdio tool calls refreshed a
                # session's last_seen; a hosted/tunnel executor's session went
                # stale between the sparse tools that happen to write it. Any
                # native Meridian tool carrying a session_id now keeps the session
                # alive, and marks it "connected" so the keepalive loop holds it
                # fresh through quiet, non-MCP work. Best-effort: never fail the call.
                _session_id = args.get("session_id")
                if _session_id and name != "heartbeat":
                    try:
                        await db_module.update_session_seen(db, _session_id)
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        _server._mark_session_connected(_session_id)
                    except Exception:  # noqa: BLE001
                        pass
            return _server._jsonrpc_ok(req_id, {"content": [{"type": "text", "text": json.dumps(result, default=_json_default)}]})
        except Exception as exc:
            return _server._jsonrpc_err(req_id, -32603, str(exc))

    return _server._jsonrpc_err(req_id, -32601, f"method not found: {method}")


async def _maybe_add_log_task_nudge(db: Any, task: dict[str, Any]) -> dict[str, Any]:
    """Append a soft nudge to log_task result when session logs many tasks with no sprint work."""
    try:
        settings = await db_module.get_workspace_settings(db)
        threshold = settings.get("log_task_sprint_nudge_threshold", 5)
        if not threshold:
            return task
        session_id = task.get("session_id")
        project_id = task.get("project_id")
        if not session_id or not project_id:
            return task
        async with db.execute(
            "SELECT COUNT(*) AS cnt FROM task_log WHERE session_id = ? AND status != 'failed'",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
        task_count = int(row["cnt"]) if row else 0
        if task_count < threshold:
            return task
        async with db.execute(
            "SELECT COUNT(*) AS cnt FROM sprint_items WHERE project_id = ? "
            "AND claimed_at >= (SELECT created_at FROM sessions WHERE id = ?)",
            (project_id, session_id),
        ) as cur:
            row = await cur.fetchone()
        sprint_count = int(row["cnt"]) if row else 0
        if sprint_count > 0:
            return task
        task = dict(task)
        task["nudge"] = (
            f"You have logged {task_count} tasks inline with no sprint items. "
            "If this is coordinated work, consider filing sprint items for better tracking. "
            "Set log_task_sprint_nudge_threshold=0 in workspace settings to disable."
        )
    except Exception:  # noqa: BLE001
        pass
    return task


async def _build_context_refresh(db: Any, project_id: str) -> dict[str, Any] | None:
    """Build the compact context-refresh dict for a project.

    d8bd59c4 / bf51b12e — shared builder for both the explicit ``refresh_context``
    tool and the planner context-refresh dispatch hook. COMPACT by design: counts
    + ids + slugs, not full bodies. Returns None when the project does not exist.
    """
    project = await db_module.get_project(db, project_id)
    if project is None:
        return None
    goal = await db_module.get_goal(db, project_id)
    all_items = await db_module.get_sprint_items(db, project_id)
    done = sum(1 for it in all_items if it.get("status") == "done")
    total = len(all_items)
    pending = [it for it in all_items if it.get("status") == "pending"]
    active_sessions = await db_module.get_sessions(db, project_id, active_only=True)
    recent_handoffs = await db_module.get_handoffs(db, project_id, limit=3)
    pinned = await db_module.get_pinned_decisions(db, project_id)
    high_priority_decisions = [
        {"id": d.get("id"), "title": (d.get("title") or "")[:120]}
        for d in pinned if d.get("priority") == "urgent"
    ][:5]
    unvalidated_assumptions = [
        {
            "decision_id": d.get("id"),
            "title": (d.get("title") or "")[:120],
            "assumption_status": d.get("assumption_status"),
        }
        for d in pinned
        if d.get("assumption") and d.get("assumption_status") != "confirmed"
    ]
    notes = await db_module.get_project_notes(db, project_id)
    _rank = {"high": 0, "normal": 1, "low": 2}
    key_notes = sorted(
        [n for n in notes if n.get("slug")],
        key=lambda n: _rank.get(n.get("priority"), 1),
    )[:10]
    key_note_slugs = [
        {
            "slug": n.get("slug"),
            "title": (n.get("title") or "")[:100],
            "kind": n.get("note_kind") or n.get("kind"),
        }
        for n in key_notes
    ]
    return {
        "project_id": project_id,
        "project_name": project.get("name"),
        "sprint": (goal.get("sprint") or "") if goal else "",
        "north_star": (goal.get("north_star") or "") if goal else "",
        "sprint_progress": {
            "done": done,
            "total": total,
            "pending": len(pending),
            "percent_complete": round(100 * done / total) if total else 0,
        },
        "next_pending_items": [
            {"id": it["id"], "title": (it.get("title") or "")[:100]}
            for it in pending[:5]
        ],
        "active_session_id": (
            active_sessions[0]["id"] if active_sessions else None
        ),
        "active_sessions": [
            {"id": s.get("id"), "name": s.get("name")}
            for s in active_sessions[:5]
        ],
        "recent_handoffs": [
            {
                "id": h.get("id"),
                "session_id": h.get("session_id"),
                "mode": h.get("mode"),
                "created_at": h.get("created_at"),
            }
            for h in recent_handoffs
        ],
        "high_priority_decisions": high_priority_decisions,
        "unvalidated_assumptions": unvalidated_assumptions,
        "key_note_slugs": key_note_slugs,
        # 3d7b7aca — re-inject a timezone-aware time signal so a long session that
        # refreshes after compaction re-anchors to the real wall-clock date/time.
        "current_time": _server._wall_clock_now(_server._executor_config_tz(project)),
    }


def _parse_touches_files(raw: Any) -> list[str]:
    """Decode a sprint item's touches_files field into normalized file paths."""
    if raw is None:
        return []
    if isinstance(raw, list):
        values = raw
    else:
        text = str(raw).strip()
        if not text:
            return []
        try:
            decoded = json.loads(text)
            values = decoded if isinstance(decoded, list) else [decoded]
        except Exception:  # noqa: BLE001
            values = [part.strip() for part in text.split(",")]
    paths: list[str] = []
    for value in values:
        path = str(value or "").strip().replace("\\", "/")
        if path.startswith("./"):
            path = path[2:]
        if path:
            paths.append(path)
    return paths


async def _sprint_item_file_claim_conflicts(
    db: Any,
    project_id: str,
    item_id: str,
    *,
    exclude_session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return active file locks overlapping a sprint item's touches_files."""
    item = await db_module.get_sprint_item(db, item_id)
    if item is None or item.get("project_id") != project_id:
        return []
    touches = {path.lower() for path in _parse_touches_files(item.get("touches_files"))}
    if not touches:
        return []
    await db_module.expire_file_locks(db)
    params: list[Any] = [project_id]
    exclude_clause = ""
    if exclude_session_id:
        exclude_clause = "AND fl.session_id != ? "
        params.append(exclude_session_id)
    async with db.execute(
        "SELECT fl.file_path, fl.session_id, s.name AS session_name, s.last_seen "
        "FROM file_locks fl "
        "JOIN sessions s ON s.id = fl.session_id "
        "WHERE s.project_id = ? "
        f"{exclude_clause}"
        "AND s.status IN ('active', 'live') "
        "AND (s.last_seen IS NULL OR s.last_seen > datetime('now', '-10 minutes'))",
        tuple(params),
    ) as cur:
        rows = await cur.fetchall()
    conflicts: list[dict[str, Any]] = []
    for row in rows:
        r = dict(row)
        path = str(r.get("file_path") or "").strip().replace("\\", "/")
        if path.startswith("./"):
            path = path[2:]
        if path.lower() not in touches:
            continue
        conflicts.append({
            "file_path": path,
            "session_id": r.get("session_id"),
            "session_name": r.get("session_name"),
            "last_seen": r.get("last_seen"),
            "sprint_item_id": item_id,
        })
    return conflicts


async def _board_change_for_session(
    db: Any,
    project_id: str,
    session_id: str | None,
) -> dict[str, Any] | None:
    """Count sprint items added since a session started (live-queue signal).

    Returns ``{"new_items_since_session_start": N, "message": "..."}`` when N>0,
    else None. Attached to between-task MCP responses (complete_sprint_item,
    claim_sprint_item, get_sprint_progress) so a single-terminal executor sees
    items a planner injected mid-run and picks them up at the next item
    boundary — without interrupting the current task. (d01a74bf)
    """
    if not session_id:
        return None
    try:
        _all_sess = await db_module.get_sessions(db, project_id, active_only=False)
        _curr = next((s for s in _all_sess if s.get("id") == session_id), None)
        if not _curr or not _curr.get("created_at"):
            return None
        started = str(_curr["created_at"])
        items = await db_module.get_sprint_items(db, project_id)
        new_count = sum(1 for it in items if (it.get("added_at") or "") > started)
        if new_count <= 0:
            return None
        return {
            "new_items_since_session_start": new_count,
            "message": (
                f"{new_count} new sprint item{'s' if new_count != 1 else ''} "
                "added since this session started — call get_sprint_progress() "
                "to review and pick them up after the current item."
            ),
        }
    except Exception:  # noqa: BLE001
        return None


_HOTSPOT_RULES: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"server", "route", "api", "endpoint", "fastapi", "lifespan", "oauth"}), "meridian/server.py"),
    (frozenset({"dashboard", "ui", "frontend", "javascript", "css", "button", "tab", "panel"}), "meridian/static/dashboard.js"),
    (frozenset({"db", "database", "schema", "migration", "query", "sql", "table", "column"}), "meridian/db/__init__.py"),
    (frozenset({"mcp", "tool", "handler", "dispatch", "stdio"}), "meridian/mcp/handler.py"),
    (frozenset({"pg", "postgres", "postgresql", "neon", "adapter"}), "meridian/pg_adapter.py"),
    (frozenset({"claude.md", "agents.md", "meridian.md", "docs", "instructions", "rules"}), "CLAUDE.md"),
)


def _suggest_files_for_title(title: str) -> list[str]:
    """Return hotspot files likely touched by a sprint item based on its title keywords.

    Each hotspot fires when at least one keyword matches a word in the title.
    (f5726fd0)
    """
    words = frozenset(title.lower().split())
    suggested: list[str] = []
    for keywords, path in _HOTSPOT_RULES:
        if words & keywords:
            suggested.append(path)
    return suggested


def _infer_touches_resources(title: str) -> list[str]:
    """07bdfdbb — auto-populate touches_resources from a sprint item's title when
    none is supplied, so the item isn't left undeclared (and thus forced into its
    own sequential group — see de730a25). Reuses the :func:`_suggest_files_for_title`
    keyword rules. Each guess is prefixed ``inferred:`` so it's distinguishable
    from an explicit declaration and can be overridden, while still normalizing to
    the same id for conflict detection.
    """
    return [f"inferred:file:{path}" for path in _suggest_files_for_title(title or "")]


# 84d255af — tokens stripped before keyword extraction: sprint-item labels,
# punctuation-glue, and generic filler that would produce useless search queries.
_PROSPECT_STOPWORDS = frozenset({
    "feat", "bug", "gap", "ux", "fix", "refactor", "urgent", "confirmed", "product",
    "feature", "confirm", "high", "urgency", "likely", "corrected", "investigate",
    "the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "is", "are", "not",
    "no", "it", "its", "with", "without", "via", "real", "code", "tonight", "adam",
    "should", "need", "needs", "add", "added", "from", "into", "that", "this", "when",
    "where", "which", "only", "still", "just", "does", "doesn", "here", "there",
    "currently", "actually", "instead", "already", "would", "could", "than", "then",
})


def _keyword_prospect_fallback(title: str) -> dict[str, Any] | None:
    """84d255af — an index-free prospecting fallback for items with neither a
    declared ``touches_resources`` nor a hotspot-keyword match (the common shape
    of planning-chat-authored items, whose auto-prospect otherwise dead-ends at
    ``no_targets``). Extracts salient keywords from the title and emits
    server-side ``search_code`` queries: unlike ``codebase__search_graph`` these
    hit the connected GitHub repo and need no executor tunnel / code index, so
    they actually work from a planning chat. Returns None only when the title has
    no usable keyword (e.g. empty), preserving the old ``no_targets`` for that."""
    import re as _re
    keywords: list[str] = []
    seen: set[str] = set()
    for tok in _re.findall(r"[A-Za-z_][A-Za-z0-9_./]{2,}", title or ""):
        low = tok.lower().strip(":._/")
        if not low or low in _PROSPECT_STOPWORDS or low in seen:
            continue
        seen.add(low)
        keywords.append(tok)
        if len(keywords) >= 5:
            break
    if not keywords:
        return None
    return {
        "source": "keyword_fallback",
        "keywords": keywords,
        "search_code_calls": [f'search_code(query="{k}")' for k in keywords[:3]],
        "hint": (
            "No declared resources and no hotspot match. These keywords were "
            "extracted from the item title; run search_code (GitHub-backed — needs "
            "no code tunnel/index, so it works from a planning chat) to locate the "
            "relevant files before editing."
        ),
    }


def _prospect_code_context(item: dict[str, Any]) -> dict[str, Any] | None:
    """04a15d3f — best-effort code-prospecting context for a freshly-claimed item.

    Parses the item's typed ``touches_resources`` into the files and symbols an
    executor should prospect before editing and emits the exact code-intel calls
    to run — ``codebase__search_graph`` for ``file:`` entries, Serena
    ``find_symbol`` for ``symbol:`` entries. With no declared resources it falls
    back to title-keyword file inference (:func:`_suggest_files_for_title`).

    The code index lives behind the executor's tunnel, so the server surfaces
    the prospecting *targets and commands* rather than inlining graph results:
    this never touches the tunnel and returns ``None`` when there's nothing to
    suggest, so it can't fail (or block) a claim — non-fatal by construction.
    """
    files: list[str] = []
    symbols: list[str] = []
    for entry in _parse_touches_files(item.get("touches_resources")):
        if entry.startswith("file:"):
            files.append(entry[len("file:"):])
        elif entry.startswith("symbol:"):
            symbols.append(entry[len("symbol:"):])
        elif entry.startswith("inferred:file:"):
            files.append(entry[len("inferred:file:"):])
    if files or symbols:
        ctx: dict[str, Any] = {"source": "touches_resources"}
        if files:
            ctx["files"] = files
            ctx["search_graph_calls"] = [
                f'codebase__search_graph(query="{f}")' for f in files
            ]
        if symbols:
            ctx["symbols"] = symbols
            ctx["find_symbol_calls"] = [
                f'find_symbol(name_path="{s}")' for s in symbols
            ]
        ctx["hint"] = (
            "Prospect before editing: run the listed code-intel calls "
            "(no-op if the code tunnel/index isn't connected)."
        )
        return ctx
    inferred = _suggest_files_for_title(item.get("title") or "")
    if inferred:
        return {
            "source": "title_inference",
            "files": inferred,
            "search_graph_calls": [
                f'codebase__search_graph(query="{f}")' for f in inferred
            ],
            "hint": (
                "No touches_resources declared; these files were inferred from "
                "the item title. Prospect before editing."
            ),
        }
    # 84d255af — last resort: an index-free keyword search hint so planning-chat
    # items (no resources, no hotspot match) still get an actionable pointer
    # instead of dead-ending at no_targets.
    return _keyword_prospect_fallback(item.get("title") or "")


def _prospecting_result(
    item: "dict[str, Any] | None",
) -> "tuple[dict[str, Any] | None, str]":
    """926bf221 / a8550238 — run add/update-time code prospecting and return an
    EXPLICIT status alongside the context, so a caller can tell "prospected" from
    "nothing to prospect" from "never attempted / errored". Previously only
    claim_sprint_item prospected, and no status field existed, so success,
    silent-failure, and never-attempted were indistinguishable to the caller.

    Never raises — _prospect_code_context is non-fatal by construction, but the
    manual-item check and the guard keep this safe for the hot add/update path.
    """
    if not isinstance(item, dict):
        return None, "no_item"
    # MANUAL/human maintainer items get no code prospecting (prospecting "form an
    # LLC" only attaches noise) — matches the handoff enrichment policy.
    try:
        from .. import handoff as _handoff_manual  # noqa: PLC0415
        if _handoff_manual._is_manual_sprint_item(item):
            return None, "skipped_manual"
    except Exception:  # noqa: BLE001
        pass
    try:
        ctx = _prospect_code_context(item)
    except Exception:  # noqa: BLE001 — best-effort; never break add/update
        return None, "error"
    if ctx:
        return ctx, "prospected"
    return None, "no_targets"


def _prospected_pointer_targets(item: "dict[str, Any]") -> list[dict[str, Any]]:
    """691f4e1c — DURABLE, resolvable ``symbol`` pointer targets from an item's declared
    ``touches_resources``, so add/update prospecting can persist a real, queryable
    pointer instead of leaving only a one-shot ``code_context`` hint.

    Delegates to :func:`handoff.build_declared_symbol_targets` — the shared source of
    truth (reused, not duplicated: handoff.py already owns the touches_resources parse
    and the graph-match ``symbol`` shape). The server can't reach the code index
    (04a15d3f — behind the executor's tunnel), so an exact line ``range`` can't be
    resolved at write time; a ``symbol`` selector carrying the declared
    ``qualified_name`` is the tunnel-free equivalent, best-matched against the graph
    LATER by ``resolve_sprint_item_pointers``. Never raises.
    """
    try:
        from .. import handoff as _handoff  # noqa: PLC0415 — avoid import cycle
        return _handoff.build_declared_symbol_targets(item)
    except Exception:  # noqa: BLE001 — best-effort; never break add/update
        return []


async def _persist_prospected_pointer(
    db: Any,
    project_id: str,
    item: "dict[str, Any] | None",
    status: str,
) -> "dict[str, Any] | None":
    """691f4e1c — at add/update time, PERSIST the prospected code context as a real
    ``symbol``-source sprint-item pointer (durable + queryable), not just the inline
    ``code_context`` hint. Returns the stored pointer, or ``None`` when nothing durable
    could be persisted (no declared symbol / non-code item / not prospected / error).

    Idempotent-ish for update: skips creation when the item already carries a code /
    symbol pointer, so a re-run of update_sprint_item doesn't stack duplicate pointers.
    Best-effort + fully guarded — a pointer-persist failure must NEVER break the hot
    add/update path (the item is already written; the inline hint still ships).
    """
    if status != "prospected" or not isinstance(item, dict):
        return None
    item_id = item.get("id")
    if not item_id or not project_id:
        return None
    targets = _prospected_pointer_targets(item)
    if not targets:
        return None
    try:
        # Don't stack duplicates: if a durable code/symbol pointer already exists on
        # this item, leave it (update re-runs prospecting but shouldn't pile pointers).
        existing = await db_module.get_sprint_item_pointers(db, item_id)
        if any(
            isinstance(p, dict) and p.get("source_type") in ("code", "symbol")
            for p in (existing or [])
        ):
            return None
        return await db_module.add_sprint_item_pointer(
            db, project_id, item_id, "symbol", targets,
            label="auto-prospected (add/update-time)",
        )
    except Exception:  # noqa: BLE001 — best-effort; never break add/update
        return None


async def _active_executor_session_warnings(db: Any, project_id: str) -> list[str]:
    """fd86aacc — names of executor sessions seen active in the last 10 minutes.

    Used to warn when sprint items are added/fanned-out while executors are
    mid-run, so a board change isn't silently injected behind their back.
    Best-effort: any error yields an empty list. (586eeda9 — shared by
    add_sprint_item + fan_out_sprint_items.)
    """
    warnings: list[str] = []
    try:
        from datetime import datetime, timezone as _tz
        _now = datetime.now(_tz.utc)
        for _sess in await db_module.get_sessions(db, project_id):
            _ls = _sess.get("last_seen")
            if not _ls:
                continue
            try:
                _dt = datetime.fromisoformat(str(_ls).replace("Z", "+00:00"))
                if _dt.tzinfo is None:
                    _dt = _dt.replace(tzinfo=_tz.utc)
                if (_now - _dt).total_seconds() < 600:
                    warnings.append(
                        f"session '{_sess.get('name', _sess.get('id', '?'))}' is active"
                    )
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return warnings


async def _unclaimed_file_warnings(
    db: Any,
    session_id: str,
) -> list[str]:
    """Return warnings for files modified without a lock claim.

    Runs ``git diff --name-only HEAD`` + ``git diff --name-only --cached``
    to find modified files, then compares against this session's file_locks.
    Non-fatal on any git or DB error — returns []. (02cd3992)
    """
    try:
        # Get files claimed by this session.
        cur = await db.execute(
            "SELECT file_path FROM file_locks WHERE session_id = ?",
            (session_id,),
        )
        rows = await cur.fetchall()
        claimed = {(r["file_path"] if isinstance(r, dict) else r[0]) for r in rows}

        # Get modified files via git (uncommitted + staged).
        proc = await asyncio.create_subprocess_exec(
            "git", "diff", "--name-only", "HEAD",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        unstaged = set(stdout.decode().splitlines()) if stdout else set()

        proc2 = await asyncio.create_subprocess_exec(
            "git", "diff", "--name-only", "--cached",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=5.0)
        staged = set(stdout2.decode().splitlines()) if stdout2 else set()

        modified = {p for p in (unstaged | staged) if p}
        unclaimed = modified - claimed
        return [
            f"⚠️ You modified {p} without claiming it — another session may have conflicted."
            for p in sorted(unclaimed)
        ]
    except Exception:  # noqa: BLE001
        return []


# ab2ba5fe — cache recent commits per project for a short TTL. add_sprint_item's
# drift check calls _fetch_recent_commits on every add; a burst of adds (Adam saw
# 30+ in a night) previously hit the GitHub API (or a `git log` subprocess) each
# time — the dominant per-call cost. One fetch is now shared for the TTL window.
_RECENT_COMMITS_TTL = 60.0  # seconds
_recent_commits_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}


async def _fetch_recent_commits(
    project: dict[str, Any],
    tenant: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """ab2ba5fe — thin TTL cache over :func:`_fetch_recent_commits_uncached`,
    keyed by project id, so a burst of add_sprint_item drift checks shares a
    single fetch instead of a GitHub round-trip / subprocess per call."""
    import time as _time  # noqa: PLC0415
    pid = (project or {}).get("id") or ""
    if pid:
        cached = _recent_commits_cache.get(pid)
        if cached and (_time.monotonic() - cached[0]) < _RECENT_COMMITS_TTL:
            return cached[1]
    commits = await _fetch_recent_commits_uncached(project, tenant)
    if pid:
        _recent_commits_cache[pid] = (_time.monotonic(), commits)
    return commits


async def _fetch_recent_commits_uncached(
    project: dict[str, Any],
    tenant: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """Fetch last 20 commits for a project as ``{"sha", "message"}`` dicts.

    Tries GitHub API if tenant has github_pat and project has github_repo;
    falls back to local ``git log --oneline -20``. The SHA is preserved so
    reconcile-drift output can cite the matching commit (callers that only
    need messages can map ``c["message"]``).
    Non-fatal — returns empty list on any failure.
    """
    import subprocess as _sp  # noqa: PLC0415
    commits: list[dict[str, str]] = []
    try:
        if tenant:
            pat = db_module.decrypt_field(tenant.get("github_pat"))
            repo = (project.get("github_repo") or "").strip()
            if pat and repo:
                import httpx as _httpx  # noqa: PLC0415
                gh_headers = {
                    "Authorization": f"token {pat}",
                    "Accept": "application/vnd.github+json",
                }
                async with _httpx.AsyncClient(timeout=8.0) as http:
                    r = await http.get(
                        f"https://api.github.com/repos/{repo}/commits",
                        headers=gh_headers,
                        params={"per_page": "20"},
                    )
                    if r.status_code == 200:
                        for c in r.json():
                            msg = c["commit"]["message"].split("\n")[0]
                            commits.append({"sha": (c.get("sha") or "")[:12], "message": msg})
                if commits:
                    return commits
    except Exception:  # noqa: BLE001
        pass
    try:
        result = _sp.run(
            ["git", "log", "--oneline", "-20"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if line and " " in line:
                sha, _, msg = line.partition(" ")
                commits.append({"sha": sha, "message": msg})
    except Exception:  # noqa: BLE001
        pass
    return commits


# Sentinel returned by a group handler when the tool name is not in that
# group, so the dispatcher falls through to the next group. Distinct from
# None, which is a legitimate handler return value (e.g. get_goal).
_MISS: Any = object()

# bf51b12e — planner context-refresh nudge.
# Default set of tool names that trigger a compact context-refresh being
# attached to a planner (non-executor) tool result. A workspace can override
# this via workspace_settings.refresh_triggers (a JSON list). NOTE: add_insight,
# not capture_insight — capture_insight was removed by b5ed8a61.
_PLANNER_REFRESH_TRIGGERS = frozenset({
    "add_insight",
    "pin_decision",
    "pin_workspace_decision",
    "set_north_star",
    "set_goal",
    "generate_handoff",
})

# In-memory, best-effort per-process state for the planner context-refresh nudge.
# This is intentionally NOT persisted: turn tracking + gating for a UX refresh
# nudge is fine as best-effort per-process state (avoids a sessions-table
# migration + a per-call DB write).
#   _EXECUTOR_SESSIONS  — session_ids that started with role='executor'; the
#                         nudge is planner-only, so these are skipped.
#   _SESSION_REFRESH_STATE — session_id -> {"calls": int, "last_refresh": int};
#                         "calls" counts tool calls seen, "last_refresh" is the
#                         call index at which we last attached a refresh.
_EXECUTOR_SESSIONS: set[str] = set()
_SESSION_REFRESH_STATE: dict[str, dict] = {}


async def _handle_project_tools(
    name: str,
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """Dispatch group: create_project, register_session, start_session, list_projects, get_project_by_name, get_goal, set_goal, set_north_star."""
    if name == "create_project":
        existing = await db_module.get_project_by_name(db, args["name"])
        if existing is not None:
            return {"error": f"project '{args['name']}' already exists", "project": existing}
        # 3b6ff466 — optional parent_project_id makes this a one-level-deep
        # subproject; an invalid/nested parent raises ValueError → error dict.
        try:
            return await db_module.create_project(
                db, args["name"], execution_mode=args.get("execution_mode"),
                # 0bf67524 — seed from workspace cascade defaults when authenticated.
                tenant_id=(tenant.get("id") if tenant else None),
                parent_project_id=args.get("parent_project_id"),
            )
        except ValueError as exc:
            return {"error": str(exc)}
    if name == "set_parent_project":
        # 7acb8563 — set / change / clear parent_project_id on an EXISTING project
        # (create_project only accepted it at creation time). Resolve the project and
        # parent by id or name; an invalid / nested / self / has-children parent
        # raises ValueError in the db layer -> surfaced as {error}. Omitting the
        # parent (or passing empty) DETACHES the project (makes it top-level).
        _pid = (args.get("project_id") or "").strip()
        if not _pid and args.get("project_name"):
            _p = await db_module.get_project_by_name(db, str(args["project_name"]))
            _pid = (_p or {}).get("id", "") if _p else ""
        if not _pid:
            return {"error": "project_id (or project_name) is required"}
        _parent = args.get("parent_project_id")
        if not _parent and args.get("parent_project_name"):
            _pp = await db_module.get_project_by_name(db, str(args["parent_project_name"]))
            _parent = (_pp or {}).get("id") if _pp else None
        if not _parent:  # "" / missing -> detach
            _parent = None
        try:
            updated = await db_module.set_parent_project(db, _pid, _parent)
        except ValueError as exc:
            return {"error": str(exc)}
        if updated is None:
            return {"error": f"project '{_pid}' not found"}
        return updated
    if name == "rename_project":
        # 7acb8563 — MCP wrapper over the existing db.rename_project (previously only
        # reachable through the HTTP route, so an agent had to use raw SQL).
        _pid = (args.get("project_id") or "").strip()
        if not _pid and args.get("project_name"):
            _p = await db_module.get_project_by_name(db, str(args["project_name"]))
            _pid = (_p or {}).get("id", "") if _p else ""
        if not _pid:
            return {"error": "project_id (or project_name) is required"}
        _new = (args.get("new_name") or "").strip()
        if not _new:
            return {"error": "new_name is required"}
        updated = await db_module.rename_project(db, _pid, _new)
        if updated is None:
            return {"error": f"project '{_pid}' not found"}
        return updated
    if name == "register_session":
        hid = args.get("human_id")
        if not hid and not _hosted_mode():
            hid = db_module.get_default_human_id()
        return await db_module.register_session(
            db, args["project_id"], args["session_name"],
            hid,
            agent_framework=args.get("agent_framework", "claude_code"),
            client_type=args.get("client"),
        )
    if name == "start_session":
        # 3689f680 — MCP start_session defaults to a compact response so an
        # executor's context isn't blown by the full goal/instructions payload.
        # Pass compact=False explicitly for the full block.
        # a76cb7c0 — optional `version` scopes the session to a sprint-version
        # bucket (orientation counts/items + /goal filter to it).
        # 599d0097 — session_name is optional: when omitted/blank, generate a
        # meaningful default from the first pending item title + timestamp.
        # ce3693e4 — resolve project_name → project_id HERE too (the central
        # _dispatch_mcp_tool resolver already does this for the HTTP surface, but
        # start_session must never index args["project_id"] blind: if only a
        # project_name reached this handler it would raise a bare
        # KeyError('project_id') that leaks as a cryptic -32603. Mirror the
        # set_parent_project / rename_project pattern: resolve, then guard.
        _pid = (args.get("project_id") or "").strip()
        if not _pid and args.get("project_name"):
            _p = await db_module.get_project_by_name(db, str(args["project_name"]))
            _pid = (_p or {}).get("id", "") if _p else ""
        if not _pid:
            return {"error": "project_id (or project_name) is required"}
        _sname = (args.get("session_name") or "").strip()
        if not _sname:
            _sname = await db_module.generate_default_session_name(
                db, _pid
            )
        result = await _server._start_session_composite(
            db,
            _pid,
            _sname,
            data_dir,
            human_id=args.get("human_id"),
            client_type=args.get("client"),
            role=args.get("role"),
            compact=args.get("compact", True),
            version=args.get("version"),
            mode=args.get("mode"),
        )
        # bf51b12e — register executor sessions in-memory so the planner
        # context-refresh hook skips them (the nudge is planner-only). Best-effort:
        # any failure here must never break start_session.
        try:
            if args.get("role") == "executor" and isinstance(result, dict):
                _sid = result.get("session_id") or result.get("session", {}).get("id")
                if _sid:
                    _EXECUTOR_SESSIONS.add(_sid)
        except Exception:  # noqa: BLE001 — non-fatal
            pass
        # b9d1b606 — expand fs proxy roots so the executor's repo_path is
        # accessible without a separate set_active_repo call.
        # b2a417ad — also point the Serena daemon pool at this project's repo so
        # claude.ai chat sessions (which never set the X-Meridian-Repo-Path
        # header) route code-intel requests to the right daemon. start_session is
        # the per-session signal of "which project am I working on now"; both
        # control messages are best-effort and never fail the orientation.
        # bc2e5ff0 — serve repo_path AND filesystem_roots (e.g. a separate
        # Outputs dir). Previously only repo_path was sent, so the tunnel never
        # served the extra roots even after set_executor_config persisted them.
        if tenant is not None:
            try:
                tenant_id = tenant.get("id", "")
                exec_cfg = await db_module.get_executor_config(db, _pid)
                repo_path = exec_cfg.get("repo_path") if exec_cfg else None
                fs_roots = (exec_cfg.get("filesystem_roots") if exec_cfg else None) or []
                # Union repo_path + filesystem_roots, deduped, order-preserved.
                candidates = []
                if isinstance(repo_path, str):
                    candidates.append(repo_path)
                candidates.extend(r for r in fs_roots if isinstance(r, str))
                all_roots: list[str] = []
                seen: set[str] = set()
                for r in candidates:
                    s = r.strip()
                    if s and s not in seen:
                        seen.add(s)
                        all_roots.append(s)
                if all_roots:
                    from ..routes import tunnel as _tunnel_mod
                    await _tunnel_mod.send_add_fs_roots_control(tenant_id, all_roots)
                    # Serena pool tracks one active repo: prefer repo_path, else
                    # the first root (Serena indexes a single project at a time).
                    serena_target = (
                        repo_path.strip()
                        if isinstance(repo_path, str) and repo_path.strip()
                        else all_roots[0]
                    )
                    await _tunnel_mod.send_active_repo_control(tenant_id, serena_target)
            except Exception:  # noqa: BLE001 — non-fatal background expansion
                pass
            # 9f6aec5f — inject a codebase-architecture summary so the executor
            # starts already knowing the code's shape. No-op without a healthy,
            # indexed code-intel tunnel; never fails the orientation.
            # 2c645647 — when that index is available, also prepend the
            # CODEBASE INDEX directive to agent_instructions so graph-tool usage
            # is a protocol-level instruction, not just advisory tool text.
            try:
                if isinstance(result, dict) and "continuation" not in result:
                    _cc = await _server._build_codebase_context(
                        tenant.get("id", ""), args["project_id"],
                        compact=args.get("compact", True),
                    )
                    if _cc:
                        result["codebase_context"] = _cc
                        _existing_ai = result.get("agent_instructions")
                        result["agent_instructions"] = (
                            f"{_server.CODEBASE_INDEX_DIRECTIVE}\n\n{_existing_ai}"
                            if _existing_ai else _server.CODEBASE_INDEX_DIRECTIVE
                        )
            except Exception:  # noqa: BLE001 — orientation must not break
                pass
        # 5efe254b — deliver any pending handoff /goal through this trusted tool
        # result (keyed on project_id) rather than as a spoofable copy-pasted
        # chat string. Read-once: pop clears it so it surfaces exactly once.
        # Outside the tenant gate so self-hosted sessions receive it too.
        # Guarded so a pre-migration DB never breaks the orientation.
        try:
            if isinstance(result, dict):
                _pg = await db_module.pop_pending_goal(db, args["project_id"])
                if _pg:
                    result["pending_goal"] = _pg
        except Exception:  # noqa: BLE001
            pass
        return result
    if name == "list_projects":
        return await db_module.list_project_summaries(db)
    if name == "get_project_by_name":
        project = await db_module.get_project_by_name(db, args["name"])
        if project is None:
            raise ValueError(f"no project found matching '{args['name']}'")
        return {
            "id": project["id"],
            "name": project["name"],
            "sprint": project.get("sprint"),
        }
    if name == "get_goal":
        goal = await db_module.get_goal(db, args["project_id"])
        if goal and goal.get("decisions") and len(goal["decisions"]) > 3000:
            goal["decisions"] = goal["decisions"][-3000:]
        return goal
    if name == "set_goal":
        return await db_module.set_goal(db, args["project_id"], args["content"])
    if name == "set_north_star":
        return await db_module.set_north_star(db, args["project_id"], args["north_star"])
    if name == "merge_project":
        # d6bd60e0 — merge a phantom-duplicate project INTO another. Re-parents the
        # source's child rows to the target (pure UPDATEs, never a delete) and
        # soft-archives the source unless archive_source is explicitly false. The db
        # layer returns an {error} dict on self-merge / unknown project, which we
        # surface verbatim. Resolve BOTH sides by id or name — a name-only arg for
        # the source never touches the central project_id resolver (that only maps
        # project_id/project_name), so mirror the set_parent_project resolve-then-
        # guard pattern for each side.
        _src = (args.get("source_project_id") or "").strip()
        if not _src and args.get("source_project_name"):
            _sp = await db_module.get_project_by_name(db, str(args["source_project_name"]))
            _src = (_sp or {}).get("id", "") if _sp else ""
        _tgt = (args.get("target_project_id") or "").strip()
        if not _tgt and args.get("target_project_name"):
            _tp = await db_module.get_project_by_name(db, str(args["target_project_name"]))
            _tgt = (_tp or {}).get("id", "") if _tp else ""
        if not _src or not _tgt:
            return {"error": "source_project_id and target_project_id are both required"}
        return await db_module.merge_project(
            db, _src, _tgt,
            archive_source=bool(args.get("archive_source", True)),
        )
    return _MISS


# e726810d — identity alias collapsing. bdc251ec derived a human_id handle from
# whichever tenant field was populated (explicit name → else email local-part), so
# the SAME person surfaced under two different handles depending on the field used:
# e.g. the email local-part "ajc123private" from one auth path and the display name
# "adam" from another. The dashboard active-sessions / standup aggregation
# (get_team_summary) buckets sessions by their raw human_id, so those two handles
# split one human into two people. We collapse a known alias set to a single
# canonical display identity BEFORE it becomes a human_id.
#
# The alias map is data-driven (no schema column): the default seeds the one known
# alias set (ajc123private / adam → Adam), and the whole map is overridable via the
# MERIDIAN_IDENTITY_ALIASES env var (JSON object of {alias: canonical}). Matching is
# case-insensitive on the trimmed handle; unknown handles pass through unchanged, so
# this never invents or merges identities it wasn't told about.
# Seed only the concrete alias set from the bug report (the email local-part
# "ajc123private" and the display name "adam" for the same person). Deliberately
# NARROW: we do not fold in near-miss handles like "ajc123" or "Adam Camerer",
# because those are distinct handles the caller may legitimately use as-is (and the
# existing bdc251ec contract tests assert they pass through unchanged). Add more
# people / aliases via the MERIDIAN_IDENTITY_ALIASES override rather than widening
# this seed.
_DEFAULT_IDENTITY_ALIASES: "dict[str, str]" = {
    "ajc123private": "Adam",
    "adam": "Adam",
}


def _load_identity_alias_map() -> "dict[str, str]":
    """Return the effective ``{alias_lower: canonical}`` identity alias map.

    Starts from :data:`_DEFAULT_IDENTITY_ALIASES` (the one seeded, known alias set)
    and overlays any ``MERIDIAN_IDENTITY_ALIASES`` env override — a JSON object whose
    keys are aliases and whose values are the canonical display identity. The
    override can extend the map with new people or re-point an existing alias; it is
    additive, never destructive of the seed (so the default keeps working when the
    env var only adds a second person). Keys are lowercased+trimmed for
    case-insensitive lookup. A malformed / non-object env value is ignored so a bad
    config can never break identity resolution."""
    aliases: dict[str, str] = {
        str(k).strip().lower(): str(v).strip()
        for k, v in _DEFAULT_IDENTITY_ALIASES.items()
        if str(k).strip() and str(v).strip()
    }
    raw = os.environ.get("MERIDIAN_IDENTITY_ALIASES", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            for k, v in parsed.items():
                key = str(k).strip().lower()
                val = str(v).strip()
                if key and val:
                    aliases[key] = val
    return aliases


def _canonicalize_identity(identity: "str | None") -> "str | None":
    """Collapse a raw human handle to its canonical display identity (e726810d).

    Looks the trimmed, case-insensitive handle up in :func:`_load_identity_alias_map`;
    returns the canonical form when the handle is a known alias, otherwise the
    original handle unchanged. ``None``/empty passes through as ``None`` so the
    handoff keeps its generic placeholder for an unknown caller."""
    ident = (identity or "").strip()
    if not ident:
        return None
    return _load_identity_alias_map().get(ident.lower(), ident)


def _resolve_caller_identity(tenant: "dict[str, Any] | None") -> "str | None":
    """bdc251ec — a human_id handle for the authenticated caller, derived from the
    tenant the auth token already resolved. Prefer an explicit name, else the email
    local-part. Returns None on the owner / self-host path (no tenant) so the
    handoff keeps its generic placeholder rather than inventing an identity.

    e726810d — the derived handle is then run through :func:`_canonicalize_identity`
    so a person's known aliases (e.g. email local-part ``ajc123private`` and display
    name ``adam``) collapse to ONE canonical identity. Without this, the dashboard
    active-sessions / standup aggregation (which buckets by human_id) shows the same
    human twice."""
    if not tenant:
        return None
    name = str(tenant.get("name") or "").strip()
    if name:
        return _canonicalize_identity(name)
    email = str(tenant.get("email") or "").strip()
    if "@" in email:
        return _canonicalize_identity(email.split("@", 1)[0] or None)
    return _canonicalize_identity(email or None)


async def _handle_task_tools(
    name: str,
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """Dispatch group: log_task, get_tasks, search_tasks, generate_handoff."""
    if name == "log_task":
        validate_input_size(args.get("description"), "description", 50_000)
        _log_sid = args.get("session_id", "")
        async with db.execute(
            "SELECT id FROM sessions WHERE id = ?", (_log_sid,)
        ) as _cur:
            if not await _cur.fetchone():
                raise ValueError(
                    "session not found — call start_session first to register "
                    "your session before calling log_task"
                )
        task = await db_module.log_task(
            db, args["session_id"], args["project_id"],
            args["description"], args.get("status", "done"),
            parent_task_id=args.get("parent_task_id"),
            kind=args.get("kind"),
        )
        return await _maybe_add_log_task_nudge(db, task)
    if name == "get_tasks":
        return await db_module.get_tasks(db, args["project_id"], args.get("limit", 20))
    if name == "search_tasks":
        return await db_module.search_tasks(
            db, args["project_id"], args["query"], args.get("limit", 5)
        )
    if name == "generate_handoff":
        from .. import handoff as handoff_module_local
        session_id = args.get("session_id")
        if not isinstance(session_id, str):
            session_id = None
        mode = handoff_module_local.resolve_handoff_mode(
            args.get("mode"),
            session_id,
        )
        # Fetch recent commits for reconcile annotations (non-fatal)
        _gh_project = await db_module.get_project(db, args["project_id"])
        _gh_commits = await _fetch_recent_commits(_gh_project or {}, tenant)
        # 4cfaecc2 — wire code-pointer enrichment to the tunnel code-intel slot.
        # Build a tunnel-backed graph searcher when a tunnel is active; None
        # otherwise (enrichment degrades to no pointers). Fully guarded — never
        # break the mandatory handoff over an enrichment convenience.
        _graph_searcher = None
        try:
            from ..routes import tunnel as _tunnel_mod  # noqa: PLC0415
            _graph_searcher = _tunnel_mod.build_graph_searcher(
                tenant.get("id") if tenant else None
            )
        except Exception:  # noqa: BLE001
            _graph_searcher = None
        try:
            path, content = await asyncio.wait_for(
                handoff_module_local.generate_handoff(
                    db,
                    args["project_id"],
                    data_dir,
                    mode=mode,
                    session_id=session_id,
                    commit_messages=[c["message"] for c in _gh_commits],
                    graph_searcher=_graph_searcher,
                    identity=_resolve_caller_identity(tenant),
                ),
                timeout=90.0,
            )
        except asyncio.TimeoutError:
            path, content = await handoff_module_local._generate_handoff_l0(
                db, args["project_id"], data_dir
            )
            mode = "full"
        # 99e50a1d — surface a machine-readable staleness flag so the dashboard /
        # caller can offer a one-click sync when the project's stored executor
        # rules predate the current standard.
        _tpl_stale = False
        try:
            from ..agent_defaults import agent_instructions_stale  # noqa: PLC0415
            _stored_ai = await db_module.get_agent_instructions(db, args["project_id"])
            _tpl_stale = agent_instructions_stale(_stored_ai)
        except Exception:  # noqa: BLE001
            _tpl_stale = False
        # 2d932f60 — scan this session's task log for insight candidates so the
        # handoff prompts an add_insight()/pin_decision() before context is lost.
        _insight_hints: list[dict[str, str]] = []
        try:
            _ih_tasks = await db_module.get_tasks(db, args["project_id"], limit=15)
            for _t in _ih_tasks:
                _desc = _t.get("description") or ""
                _sig = handoff_module_local.detect_insight_candidate(_desc)
                if _sig:
                    _insight_hints.append({"signal": _sig, "text": _desc[:160]})
        except Exception:  # noqa: BLE001
            _insight_hints = []
        # 0fe01e93 — warn when the project's /goal text is over the 4000-char soft
        # limit (a bloated goal crowds out sprint context); the dashboard live
        # counter + auto-bury handle the editing UX.
        _goal_warn = None
        try:
            _gh_goal = await db_module.get_goal(db, args["project_id"])
            _goal_len = len((_gh_goal or {}).get("sprint") or "") + len(
                (_gh_goal or {}).get("north_star") or ""
            )
            if _goal_len > 4000:
                _goal_warn = (
                    f"/goal text is {_goal_len} chars (>4000) — bury step detail into "
                    "sprint-item notes so the goal stays scannable."
                )
        except Exception:  # noqa: BLE001
            _goal_warn = None
        # 642b1818 — hotfix: return the handoff as one plain-text copyable block
        # (strip markdown code-fence markers anywhere they appear — incl. inline in
        # rendered note bodies — so it pastes cleanly into a fenced chat / a
        # dashboard textarea without breaking the surrounding fence).
        import re as _re_fence  # noqa: PLC0415
        _plain_content = _re_fence.sub(r"```[A-Za-z0-9_+.-]*", "", content)
        # 5abf3e12 — surface the stored per-session goal-compliance metric
        # (generate_handoff computed & persisted it) so the caller / dashboard
        # sees whether this session's /goal item list was fully completed.
        _goal_compliance = None
        if session_id:
            try:
                _goal_compliance = await db_module.get_session_goal_compliance(
                    db, session_id
                )
            except Exception:  # noqa: BLE001
                _goal_compliance = None
        return {
            "file_path": path,
            "content": _plain_content,
            "mode": mode,
            "template_stale": _tpl_stale,
            "insight_hints": _insight_hints[:5],
            "goal_length_warning": _goal_warn,
            "goal_compliance": _goal_compliance,
        }
    if name == "load_handoff":
        # 5efe254b — trusted retrieval of the latest stored handoff for a project
        # as an MCP tool result: an explicit, idempotent alternative to
        # start_session's pending_goal delivery. Read-only — it does NOT clear
        # pending_goal (start_session's pop owns read-once consumption), so it is
        # safe to call repeatedly.
        _pid = args["project_id"]
        _latest = None
        try:
            _rows = await db_module.get_handoffs(db, _pid, limit=1)
            _latest = _rows[0] if _rows else None
        except Exception:  # noqa: BLE001
            _latest = None
        _pending = None
        try:
            _pending = await db_module.get_pending_goal(db, _pid)
        except Exception:  # noqa: BLE001
            _pending = None
        return {
            "pending_goal": _pending,
            "handoff": (
                {
                    "content": _latest.get("body"),
                    "mode": _latest.get("mode"),
                    "session_id": _latest.get("session_id"),
                    "created_at": _latest.get("created_at"),
                }
                if _latest else None
            ),
            "has_handoff": bool(_latest) or bool(_pending),
        }
    return _MISS


def _doc_type_for_path(file_path: str | None) -> str | None:
    """Map a file path to a doc_store doc_type, or None if unsupported.

    Only the formats whose *structure* the server can parse today qualify:
    ``.docx`` (docs_intel) and ``.tex`` (latex_intel). Everything else (plain
    text, PDF-as-content, …) has no structure to persist, so ingest just stores
    the flat note and skips the structure store.
    """
    if not file_path:
        return None
    lowered = file_path.strip().lower()
    if lowered.endswith(".docx"):
        return "docx"
    if lowered.endswith(".tex"):
        return "latex"
    return None


async def _resolve_ingest_doc_store(
    db: Any, data_dir: str, tenant: dict[str, Any] | None
) -> Any:
    """Resolve the document-structure store to persist an ingest into, or None.

    Best-effort + fully guarded: any failure returns None so ingest itself never
    breaks. Prefers the tier-resolved store (pro/admin tenants with a cloud PG
    url get their tenant Postgres; everyone else the local sidecar). A tenant
    ``neon_db_url`` is decrypted here exactly like ``_deps._open_tenant_db_by_id``
    does. Falls back to the process-default ``app.state.doc_store``.
    """
    try:
        from ..doc_store import open_doc_store_for  # noqa: PLC0415

        plan: str | None = None
        tenant_pg_url: str | None = None
        if tenant:
            plan = tenant.get("plan")
            enc = tenant.get("neon_db_url")
            tid = tenant.get("id")
            if enc and tid:
                try:
                    from ..tenant_crypto import decrypt_tenant_db_url  # noqa: PLC0415
                    tenant_pg_url = decrypt_tenant_db_url(tid, enc) or None
                except Exception:  # noqa: BLE001 — degrade to local sidecar
                    tenant_pg_url = None

        return await open_doc_store_for(
            plan=plan,
            hosted=_hosted_mode(),
            data_dir=data_dir,
            tenant_pg_url=tenant_pg_url,
            override_url=os.environ.get("MERIDIAN_DOC_STORE_URL"),
        )
    except Exception:  # noqa: BLE001 — never let store resolution break ingest
        store = getattr(getattr(_server, "app", None), "state", None)
        return getattr(store, "doc_store", None) if store is not None else None


async def _persist_ingest_structure(
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    project_id: str,
    file_path: str | None,
    source: str | None,
    title: str | None,
) -> None:
    """Best-effort: parse + persist a docx/latex structure into the doc store.

    Called AFTER a successful ``ingest_document``. Wrapped so any failure
    (unsupported type, parse error, store unavailable, backend error) is
    swallowed — structure persistence is additive and must never regress ingest.
    """
    doc_type = _doc_type_for_path(file_path)
    if doc_type is None:
        return
    try:
        from ..doc_store import (  # noqa: PLC0415
            elements_from_docx_outline,
            elements_from_latex_analysis,
        )

        if doc_type == "docx":
            from ..docs_intel import document_outline  # noqa: PLC0415
            outline = document_outline(file_path)
            elements = elements_from_docx_outline(outline)
        else:  # latex
            from ..latex_intel import analyze_latex  # noqa: PLC0415
            analysis = analyze_latex(file_path)
            elements = elements_from_latex_analysis(analysis)

        store = await _resolve_ingest_doc_store(db, data_dir, tenant)
        if store is None:
            return
        stored_source = (source or file_path or "").strip() or None
        await store.put_document(
            project_id,
            doc_type,
            elements,
            source=stored_source,
            title=title,
        )
    except Exception:  # noqa: BLE001 — persistence is best-effort, never break ingest
        import logging as _ds_log  # noqa: PLC0415
        _ds_log.getLogger(__name__).debug(
            "doc-structure persistence skipped for %s", file_path, exc_info=True
        )


# --- 22c274bd — workspace-scope leak heuristic ------------------------------
# Workspace notes/decisions are meant to be tenant-global (cross-project). In
# practice they accumulate PROJECT-specific content: a thesis post-mortem, one
# project's CI patterns, a personal absolute filesystem path, a commit sha. This
# is a *soft* nudge only — we never block the write, we just attach a
# `scope_warning` to the result so the caller is reminded the content may belong
# in a project note instead. Pure heuristic; no schema change, no DB touch.

# Absolute filesystem paths — POSIX (/home/…, /Users/…, /mnt/…) and Windows
# (C:\Users\…). A bare "/" or a leading "/mcp"-style route would over-fire, so we
# require at least one more path segment after a recognised root-ish prefix.
_WS_ABS_PATH_RE = re.compile(
    r"""(?:
          [A-Za-z]:[\\/]                    # Windows drive:  C:\  or  C:/
        | /(?:home|Users|mnt|opt|srv|var|tmp|root|etc|usr|repository)/  # POSIX root dirs
        | ~/[\w.\-]                          # home-relative:  ~/something
    )""",
    re.VERBOSE,
)
# A 7–40 char lowercase hex run that looks like a git commit sha. Bounded by
# non-hex/word edges so it doesn't fire inside a longer hex/base token.
_WS_COMMIT_SHA_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{7,40}(?![0-9a-fA-F])")
# Project-anaphora phrasing that only makes sense scoped to one project.
_WS_PROJECT_PHRASE_RE = re.compile(
    r"\b(?:this project|the project|our project|this repo(?:sitory)?|"
    r"the thesis|my thesis|this codebase|the codebase|this sprint)\b",
    re.IGNORECASE,
)


def _looks_like_commit_sha(token: str) -> bool:
    """A hex token is only sha-ish if it's got both a letter and a digit — pure
    words ('deface', 'facade') and pure numbers ('1234567', a year, a port) are
    common false positives that shouldn't trip the nudge."""
    has_alpha = any(c in "abcdef" for c in token)
    has_digit = any(c.isdigit() for c in token)
    return has_alpha and has_digit


def _workspace_scope_warning(
    title: str | None, body: str | None
) -> str | None:
    """Return a soft warning if *title*/*body* look PROJECT-specific, else None.

    Heuristic signals (any one trips it):
      * an absolute filesystem path (personal/machine-local),
      * a git commit sha,
      * project-anaphora phrasing ("this project", "the thesis", …).

    Deliberately conservative and never authoritative — the write always
    proceeds; this only populates a ``scope_warning`` hint on the result.
    """
    text = f"{title or ''}\n{body or ''}"
    if not text.strip():
        return None

    signals: list[str] = []
    if _WS_ABS_PATH_RE.search(text):
        signals.append("an absolute filesystem path")
    if any(
        _looks_like_commit_sha(m.group(0))
        for m in _WS_COMMIT_SHA_RE.finditer(text)
    ):
        signals.append("a commit sha")
    if _WS_PROJECT_PHRASE_RE.search(text):
        signals.append("project-specific phrasing")

    if not signals:
        return None

    joined = signals[0] if len(signals) == 1 else (
        ", ".join(signals[:-1]) + " and " + signals[-1]
    )
    return (
        f"This looks project-specific (detected {joined}). Workspace "
        "notes/decisions are meant to be tenant-global and cross-project — "
        "consider recording this as a project note/decision instead. "
        "(Saved anyway.)"
    )


async def _handle_notes_decisions(
    name: str,
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """Dispatch group: pin_decision, update_decision, get_pinned_decisions, archive_decision, add_note, ingest_document, get_document_structure, get_latex_structure, get_citation_edges, resolve_citations, index_equation, find_similar_equation, insert_equation, update_paragraph, find_symbol_usages, index_figure, find_similar_figure, get_notes, read_note, delete_note, add_workspace_note, get_workspace_notes, pin_workspace_decision, get_workspace_decisions, get_workspace_settings, update_workspace_settings, save_blog_post, get_blog_posts, add_workspace_sprint_item, get_workspace_sprint_items, update_workspace_sprint_item, complete_workspace_sprint_item, add_workspace_proposal, get_workspace_proposals, advance_proposal_status, promote_proposal."""
    if name == "pin_decision":
        validate_input_size(args.get("title"), "decision title", 500)
        validate_input_size(args.get("body"), "decision body", 100_000)
        category = args.get("category", "TECHNICAL")
        result = await db_module.pin_decision(
            db, args["project_id"], args["title"], args["body"], category,
            priority=args.get("priority", "normal"),
            assumption=args.get("assumption"),
        )
        await _server._append_decision_to_md(args["title"], args["body"], category)
        return result
    if name == "update_decision":
        new_title = args.get("new_title")
        new_body = args.get("new_body")
        if new_title and new_body:
            return await db_module.supersede_pinned_decision(
                db, args["decision_id"], new_title, new_body, args.get("category"),
                priority=args.get("priority"),
            )
        result = await db_module.update_pinned_decision(
            db, args["decision_id"],
            body=args.get("body"),
            title=args.get("title"),
            category=args.get("category"),
            status=args.get("status"),
            superseded_by=args.get("superseded_by"),
            priority=args.get("priority"),
            assumption=args.get("assumption"),
            assumption_status=args.get("assumption_status"),
        )
        if result is None:
            raise ValueError("decision not found")
        return result
    if name == "validate_assumption":
        # 8ec5493b — one-call assumption validation: stamp the decision's
        # assumption_status, save a code-anchored finding note, and fire a
        # blocking HITL on invalidation.
        if "confirmed" not in args:
            return {"error": "validate_assumption requires 'confirmed' (bool)"}
        validate_input_size(args.get("finding"), "finding", 100_000)
        validate_input_size(args.get("file_path"), "file_path", 2_000)
        validate_input_size(args.get("symbol"), "symbol", 500)
        try:
            return await db_module.validate_assumption(
                db, args["decision_id"], args.get("finding") or "",
                bool(args.get("confirmed")),
                file_path=args.get("file_path"), symbol=args.get("symbol"),
                session_id=args.get("session_id"),
            )
        except ValueError as exc:
            return {"error": str(exc)}
    if name == "get_pinned_decisions":
        return await db_module.get_pinned_decisions(
            db, args["project_id"],
            include_superseded=bool(args.get("include_superseded", False)),
        )
    if name == "archive_decision":
        deleted = await db_module.delete_pinned_decision(db, args["decision_id"])
        if not deleted:
            raise ValueError("decision not found")
        return {"deleted": True, "decision_id": args["decision_id"]}
    if name == "add_note":
        validate_input_size(args.get("title"), "note title", 500)
        validate_input_size(args.get("body"), "note body", 10_000_000)
        validate_input_size(args.get("file_path"), "note file_path", 2_000)
        validate_input_size(args.get("symbol"), "note symbol", 500)
        validate_input_size(args.get("source"), "note source", 2_000)
        # 41b8a927 — recognise #hashtags in the title/body as tags so a note is
        # searchable by tag without a separate tags argument.
        import re as _re_ht  # noqa: PLC0415
        _ht = _re_ht.findall(
            r"(?<!\w)#([A-Za-z][\w-]{1,40})",
            f"{args.get('title') or ''} {args.get('body') or ''}",
        )
        _tags_arg = args.get("tags")
        if _ht:
            _have = {t.strip().lower() for t in (_tags_arg or "").split(",") if t.strip()}
            _add = [h for h in _ht if h.lower() not in _have]
            if _add:
                _tags_arg = ", ".join(
                    [p for p in [(_tags_arg or "").strip()] if p] + _add
                )
        try:
            result = await db_module.add_project_note(
                db, args["project_id"], args["title"], args["body"],
                _tags_arg, kind=args.get("kind"),
                priority=args.get("priority", "normal"),
                file_path=args.get("file_path"), symbol=args.get("symbol"),
                source=args.get("source"),
            )
        except ValueError as exc:
            return {"error": str(exc)}
        await _server._append_note_to_roadmap(
            args["title"], args["body"], args.get("tags"), args.get("category"),
        )
        # e5592013 — lint: "MANUAL" notes are usually human tasks, not wiki.
        if isinstance(result, dict) and "MANUAL" in (args.get("title") or ""):
            result = {**result, "lint": _MANUAL_NOTE_LINT}
        # 6e4e2371 — warn (never block) when a near-duplicate note already exists,
        # so notes don't accumulate repetitive near-copies. Advisory: any failure
        # here must not fail the write.
        if isinstance(result, dict) and not result.get("error"):
            try:
                import difflib as _difflib  # noqa: PLC0415
                _new_title = (args.get("title") or "").strip().lower()
                if _new_title:
                    _new_id = result.get("id")
                    _new_slug = result.get("slug")
                    _existing = await db_module.get_project_notes(
                        db, args["project_id"], limit=200
                    )
                    _similar = []
                    for _n in (_existing or []):
                        if (_new_id and _n.get("id") == _new_id) or (
                            _new_slug and _n.get("slug") == _new_slug
                        ):
                            continue  # skip the note we just created
                        _et = (_n.get("title") or "").strip().lower()
                        if not _et:
                            continue
                        _ratio = _difflib.SequenceMatcher(None, _new_title, _et).ratio()
                        if _ratio >= 0.82:
                            _similar.append({
                                "slug": _n.get("slug"),
                                "title": _n.get("title"),
                                "similarity": round(_ratio, 2),
                            })
                    if _similar:
                        _similar.sort(key=lambda s: s["similarity"], reverse=True)
                        result = {
                            **result,
                            "similar_notes": _similar[:3],
                            "similar_notes_warning": (
                                "A similar note already exists — consider updating it "
                                "instead of accumulating near-duplicates."
                            ),
                        }
            except Exception:  # noqa: BLE001 — dedup is advisory
                pass
        return result
    if name == "ingest_document":
        # e3f150d0 — extract a Word/PDF/text document into a kind='document'
        # note. Extraction (.txt/.md/.docx) is stdlib-only and server-side;
        # PDFs/unsupported types must be pre-extracted by the caller and passed
        # as `content`. The body cap is applied inside db.ingest_document.
        validate_input_size(args.get("title"), "document title", 500)
        validate_input_size(args.get("file_path"), "document file_path", 2_000)
        validate_input_size(args.get("source"), "document source", 2_000)
        validate_input_size(args.get("content"), "document content", 50_000_000)
        # 832d67af — when only a file_path is given (no inline content) the server
        # extracts the text from its OWN filesystem (doc_ingest.extract_text), so on
        # hosted Meridian (Fly.io) it has ZERO access to a caller's local path and
        # the open fails with a misleading "[Errno 2] No such file or directory".
        # Mirror get_document_structure's honest guard: fail clearly, telling the
        # caller why and what to do. `content` (pre-extracted text) needs no
        # filesystem and DOES work hosted, so only guard the path-only case.
        _fp = args.get("file_path")
        _content = args.get("content")
        _has_content = _content is not None and str(_content).strip() != ""
        if _hosted_mode() and _fp and not _has_content:
            return {
                "error": (
                    "ingest_document reads the file from the Meridian server's own "
                    "filesystem, so on hosted Meridian it cannot open a path on your "
                    "machine (that is what surfaces as a misleading '[Errno 2] No "
                    "such file or directory'). Run Meridian self-hosted so the server "
                    "shares a filesystem with the file, pass the already-extracted "
                    "text as `content` instead of a `file_path`, or read the document "
                    "through your tunnel's local document tools, which proxy to your "
                    "machine."
                ),
                "hosted": True,
                "file_path": _fp,
            }
        from ..doc_ingest import DocExtractionError  # local import: optional dep-free
        try:
            _ingest_result = await db_module.ingest_document(
                db, args["project_id"],
                file_path=args.get("file_path"),
                content=args.get("content"),
                title=args.get("title"),
                source=args.get("source"),
                tags=args.get("tags"),
            )
        except (ValueError, DocExtractionError, FileNotFoundError) as exc:
            return {"error": str(exc)}
        # 9ee6d2ec — best-effort: persist the parsed docx/latex STRUCTURE into the
        # tiered doc-structure store. Fully guarded inside — a persistence failure
        # never touches _ingest_result (no regression to the flat-note ingest).
        await _persist_ingest_structure(
            db,
            data_dir,
            tenant,
            args["project_id"],
            args.get("file_path"),
            args.get("source"),
            args.get("title"),
        )
        return _ingest_result
    if name == "get_document_structure":
        # 13462df2 — stateless docs_intel: heading outline of a server-side .docx
        # (no sidecar index). Same server-side file access as ingest_document
        # (self-hosted / tunnel).
        validate_input_size(args.get("file_path"), "document file_path", 2_000)
        fp = args.get("file_path")
        if not fp:
            return {"error": "file_path is required"}
        # 79ee73e8 — record this stateless peek in the tenant-scoped "recently
        # viewed (not saved)" log so the Documents tab can surface it. Peeks were
        # invisible there (only ingested docs showed), silently conflating the two.
        _peek_scope = (tenant or {}).get("id") if tenant else None

        def _record_peek(ok: bool) -> None:
            try:
                from .. import doc_peeks  # noqa: PLC0415
                doc_peeks.record_peek(_peek_scope, fp, ok=ok)
            except Exception:  # noqa: BLE001 — the recent-peeks log is best-effort
                pass

        # b43bab91 — this reads the .docx from the SERVER's own filesystem
        # (zipfile.ZipFile), so it only works self-hosted, where the server and the
        # files share a machine. On hosted Meridian (Fly.io) the server has ZERO
        # access to a caller's local path, so the read would fail with a misleading
        # "file not found" regardless of tunnel/file state. Fail honestly instead:
        # tell the caller why and what to do (self-host, or read via the tunnel's
        # word-document tools, which proxy to their machine — unlike this native
        # tool, which does not).
        if _hosted_mode():
            _record_peek(ok=False)
            return {
                "error": (
                    "get_document_structure reads the .docx from the Meridian "
                    "server's own filesystem, so on hosted Meridian it cannot open a "
                    "path on your machine (that is what surfaces as a misleading "
                    "'file not found'). Run Meridian self-hosted so the server shares "
                    "a filesystem with the file, or read the document through your "
                    "tunnel's word-document tools, which proxy to your machine."
                ),
                "hosted": True,
                "file_path": fp,
            }
        try:
            from ..docs_intel import document_outline  # noqa: PLC0415
            from .. import hardening as _hardening  # noqa: PLC0415
            # document_outline is a synchronous zipfile/OOXML parse — previously
            # run directly on the event loop with no deadline, so a huge/malformed
            # .docx could block the whole loop (e5f96adf). Run it in the bulkhead
            # under a hard timeout: fail fast + keep the loop responsive.
            _outline = await _hardening.run_in_bulkhead(
                document_outline, fp, label="get_document_structure",
            )
        except _hardening.HeavyToolTimeout as exc:
            _record_peek(ok=False)
            return {"error": str(exc), "timed_out": True, "file_path": fp}
        except FileNotFoundError:
            return {"error": f"file not found: {fp}"}
        except Exception as exc:  # noqa: BLE001
            return {"error": f"could not parse document: {exc}"}
        _record_peek(ok=True)
        return _outline
    if name == "get_latex_structure":
        # 106118cd — docs_intel Phase 3: native LaTeX (.tex) structure + biblio,
        # no PDF intermediary. Accepts a server-side file_path (like
        # get_document_structure) OR raw `source` inline. latex_intel never
        # raises — malformed LaTeX yields a partial/empty tree, not a crash.
        validate_input_size(args.get("file_path"), "latex file_path", 2_000)
        validate_input_size(args.get("source"), "latex source", 5_000_000)
        fp = args.get("file_path")
        src = args.get("source")
        if not fp and not src:
            return {"error": "file_path or source is required"}
        # b43bab91 — a file_path is read from the SERVER filesystem and is
        # unreadable on hosted Meridian (same root cause as get_document_structure).
        # But get_latex_structure ALSO accepts inline `source`, which DOES work
        # hosted — so on hosted prefer source, and fail honestly when only an
        # unreadable path was given.
        if _hosted_mode() and fp:
            if src:
                fp = None  # server can't open the caller's path; use inline source
            else:
                return {
                    "error": (
                        "get_latex_structure reads the .tex from the Meridian "
                        "server's filesystem, so on hosted Meridian it cannot open a "
                        "path on your machine. Pass the file contents inline via "
                        "`source`, or run Meridian self-hosted."
                    ),
                    "hosted": True,
                    "file_path": fp,
                }
        from ..latex_intel import analyze_latex  # noqa: PLC0415
        try:
            if fp:
                if not os.path.isfile(fp):
                    return {"error": f"file not found: {fp}"}
                return analyze_latex(fp)
            return analyze_latex(src)
        except Exception as exc:  # noqa: BLE001 — defense in depth; analyze_latex is already safe
            return {"error": f"could not parse latex: {exc}"}
    if name == "get_citation_edges":
        # fefb596a — read the citation graph: every kind='citation' marker in a
        # project (optionally scoped to one document via source/document_id) with
        # its intra-doc bibentry edges AND cross-doc zotero_item edges. Reads the
        # tier-resolved doc-structure store; returns an empty graph (never an
        # error) when no structure has been persisted yet.
        validate_input_size(args.get("source"), "citation source", 2_000)
        validate_input_size(args.get("document_id"), "citation document_id", 200)
        if not args.get("project_id"):
            return {"error": "project_id is required"}
        store = await _resolve_ingest_doc_store(db, data_dir, tenant)
        if store is None:
            return {"project_id": args["project_id"], "markers": []}
        try:
            graph = await store.get_citation_graph(
                args["project_id"],
                source=args.get("source"),
                document_id=args.get("document_id"),
            )
        except Exception as exc:  # noqa: BLE001 — read must not crash the tool call
            return {"error": f"could not read citation graph: {exc}"}
        return {"project_id": args["project_id"], **graph}
    if name == "resolve_citations":
        # fefb596a — opt-in cross-document resolve pass: walk unresolved citation
        # markers and link each to a canonical Zotero item via Zotero's LOCAL API
        # (zotero_client.resolve_citation_ref). NETWORK — deliberately a separate
        # tool, never in ingest/put_document. Idempotent: only fills gaps. When
        # Zotero is closed / its local API is disabled every marker just stays
        # unresolved (the resolver returns None, never raises).
        if not args.get("project_id"):
            return {"error": "project_id is required"}
        store = await _resolve_ingest_doc_store(db, data_dir, tenant)
        if store is None:
            return {"error": "document-structure store unavailable"}
        _max = args.get("max_items")
        try:
            _max = int(_max) if _max is not None else None
        except (TypeError, ValueError):
            _max = None
        try:
            summary = await store.resolve_zotero_edges(
                args["project_id"], max_items=_max,
            )
        except Exception as exc:  # noqa: BLE001 — the pass is best-effort
            return {"error": f"could not resolve citations: {exc}"}
        return {"project_id": args["project_id"], **summary}
    if name == "index_equation":
        # 06df6ab3 — index ONE Word equation (OMML) against a document already
        # stored in the doc-structure store. Mirrors get_citation_edges' shape
        # (resolve the store, then look up the document by its stored source).
        validate_input_size(args.get("doc"), "equation doc", 2_000)
        validate_input_size(args.get("omml_or_latex"), "omml_or_latex", 100_000)
        validate_input_size(args.get("semantic_label"), "semantic_label", 500)
        if not args.get("project_id"):
            return {"error": "project_id is required"}
        doc_source = args.get("doc")
        omml_or_latex = args.get("omml_or_latex")
        if not doc_source:
            return {"error": "doc is required"}
        if not omml_or_latex:
            return {"error": "omml_or_latex is required"}
        store = await _resolve_ingest_doc_store(db, data_dir, tenant)
        if store is None:
            return {"error": "document-structure store unavailable"}
        try:
            doc_row = await store.get_document(args["project_id"], doc_source)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"could not resolve doc: {exc}"}
        if doc_row is None:
            return {
                "error": (
                    f"no stored document for doc={doc_source!r} — ingest_document "
                    "it first (that MCP tool populates the doc-structure store; "
                    "there is no separate reindex_document tool)"
                ),
            }
        try:
            result = await store.add_equation(
                doc_row["id"], omml_or_latex,
                semantic_label=args.get("semantic_label"),
            )
        except Exception as exc:  # noqa: BLE001 — indexing is best-effort
            return {"error": f"could not index equation: {exc}"}
        return {
            "project_id": args["project_id"],
            "document_id": doc_row["id"],
            **result,
        }
    if name == "find_similar_equation":
        # 06df6ab3 — fuzzy-match a LaTeX string against a document's already-
        # stored equations (read-only counterpart of index_equation).
        validate_input_size(args.get("doc"), "equation doc", 2_000)
        validate_input_size(args.get("latex"), "latex", 100_000)
        if not args.get("project_id"):
            return {"error": "project_id is required"}
        doc_source = args.get("doc")
        latex = args.get("latex")
        if not doc_source:
            return {"error": "doc is required"}
        if not latex:
            return {"error": "latex is required"}
        store = await _resolve_ingest_doc_store(db, data_dir, tenant)
        if store is None:
            return {"project_id": args["project_id"], "document_id": None, "matches": []}
        try:
            doc_row = await store.get_document(args["project_id"], doc_source)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"could not resolve doc: {exc}"}
        if doc_row is None:
            return {"project_id": args["project_id"], "document_id": None, "matches": []}
        _limit = args.get("limit")
        try:
            _limit = int(_limit) if _limit is not None else 5
        except (TypeError, ValueError):
            _limit = 5
        try:
            matches = await store.find_similar_equations(
                doc_row["id"], latex, limit=_limit,
            )
        except Exception as exc:  # noqa: BLE001 — read must not crash the tool call
            return {"error": f"could not find similar equations: {exc}"}
        return {
            "project_id": args["project_id"],
            "document_id": doc_row["id"],
            "matches": matches,
        }
    if name == "insert_equation":
        # 51a595e7 — write an OMML equation straight into a stored document's
        # source .docx (direct OOXML write-back), then resync the sidecar
        # equation index. Mirrors index_equation's shape (resolve the store, then
        # the document by its stored source) but MUTATES the underlying file.
        validate_input_size(args.get("doc"), "equation doc", 2_000)
        validate_input_size(args.get("para_id"), "para_id", 500)
        validate_input_size(
            args.get("equation_id_or_omml"), "equation_id_or_omml", 100_000
        )
        if not args.get("project_id"):
            return {"error": "project_id is required"}
        doc_source = args.get("doc")
        para_id = args.get("para_id")
        equation_id_or_omml = args.get("equation_id_or_omml")
        if not doc_source:
            return {"error": "doc is required"}
        if not para_id:
            return {"error": "para_id is required"}
        if not equation_id_or_omml:
            return {"error": "equation_id_or_omml is required"}
        position = args.get("position") or "append"
        store = await _resolve_ingest_doc_store(db, data_dir, tenant)
        if store is None:
            return {"error": "document-structure store unavailable"}
        try:
            result = await store.insert_equation(
                args["project_id"], doc_source, para_id, equation_id_or_omml,
                position=position,
            )
        except Exception as exc:  # noqa: BLE001 — write-back is best-effort
            return {"error": f"could not insert equation: {exc}"}
        if "error" in result:
            return result
        return {"project_id": args["project_id"], **result}
    if name == "update_paragraph":
        # f978e588 — ID-addressable docx WRITE (the write counterpart of the
        # get_element_by_id read primitive). Mirrors index_equation's resolution:
        # resolve the tier store, look up the stored document by its source, then
        # rewrite ONE paragraph in the on-disk .docx by its w14:paraId (never by
        # text match) and resync the doc_elements row.
        validate_input_size(args.get("doc"), "doc", 2_000)
        validate_input_size(args.get("para_id"), "para_id", 500)
        if not args.get("project_id"):
            return {"error": "project_id is required"}
        doc_source = args.get("doc")
        para_id = args.get("para_id")
        if not doc_source:
            return {"error": "doc is required"}
        if not para_id:
            return {"error": "para_id is required"}
        # new_text_or_runs: EITHER a plain string OR a list of runs. Exactly one
        # of new_text / runs must be provided.
        new_text = args.get("new_text")
        runs = args.get("runs")
        if new_text is None and runs is None:
            return {"error": "provide either new_text (string) or runs (list)"}
        if new_text is not None and runs is not None:
            return {"error": "provide only one of new_text or runs, not both"}
        new_text_or_runs: Any = runs if runs is not None else new_text
        if new_text is not None:
            validate_input_size(new_text, "new_text", 1_000_000)
        elif not isinstance(runs, list):
            return {"error": "runs must be a list of strings or run objects"}
        store = await _resolve_ingest_doc_store(db, data_dir, tenant)
        if store is None:
            return {"error": "document-structure store unavailable"}
        try:
            result = await store.update_paragraph(
                args["project_id"], doc_source, para_id, new_text_or_runs,
            )
        except ValueError as exc:
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 — the write is best-effort
            return {"error": f"could not update paragraph: {exc}"}
        return {"project_id": args["project_id"], **result}
    if name == "find_symbol_usages":
        # 9605edb0 — READ-ONLY cross-reference tracking: resolve a symbol /
        # normalized-LaTeX string OR a doc_equations id to one target and return
        # every paragraph/equation where it reappears, classified definition vs
        # reuse (earliest ordinal = definition). Mirrors find_similar_equation's
        # shape (resolve the store, then look up the document by its stored source).
        validate_input_size(args.get("doc"), "symbol usages doc", 2_000)
        validate_input_size(args.get("symbol_or_equation_id"), "symbol_or_equation_id", 100_000)
        if not args.get("project_id"):
            return {"error": "project_id is required"}
        doc_source = args.get("doc")
        symbol_or_equation_id = args.get("symbol_or_equation_id")
        if not doc_source:
            return {"error": "doc is required"}
        if not symbol_or_equation_id:
            return {"error": "symbol_or_equation_id is required"}
        store = await _resolve_ingest_doc_store(db, data_dir, tenant)
        if store is None:
            return {"project_id": args["project_id"], "document_id": None, "target": "", "hits": []}
        try:
            doc_row = await store.get_document(args["project_id"], doc_source)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"could not resolve doc: {exc}"}
        if doc_row is None:
            return {"project_id": args["project_id"], "document_id": None, "target": "", "hits": []}
        try:
            usages = await store.find_symbol_usages(
                doc_row["id"], symbol_or_equation_id,
            )
        except Exception as exc:  # noqa: BLE001 — read must not crash the tool call
            return {"error": f"could not find symbol usages: {exc}"}
        return {
            "project_id": args["project_id"],
            "document_id": doc_row["id"],
            **usages,
        }
    if name == "index_figure":
        # c623e648 — index ONE figure into the SEMANTIC figure index (dedup +
        # similarity on a normalized caption), the direct parallel of
        # index_equation. Complementary to the structural kind='figure'
        # doc_elements placement, not a duplicate of it.
        validate_input_size(args.get("doc"), "figure doc", 2_000)
        validate_input_size(args.get("file_path"), "file_path", 4_000)
        validate_input_size(args.get("caption"), "caption", 10_000)
        validate_input_size(args.get("semantic_label"), "semantic_label", 500)
        if not args.get("project_id"):
            return {"error": "project_id is required"}
        doc_source = args.get("doc")
        file_path = args.get("file_path")
        caption = args.get("caption")
        if not doc_source:
            return {"error": "doc is required"}
        if not file_path and not caption:
            return {"error": "at least one of file_path or caption is required"}
        store = await _resolve_ingest_doc_store(db, data_dir, tenant)
        if store is None:
            return {"error": "document-structure store unavailable"}
        try:
            doc_row = await store.get_document(args["project_id"], doc_source)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"could not resolve doc: {exc}"}
        if doc_row is None:
            return {
                "error": (
                    f"no stored document for doc={doc_source!r} — ingest_document "
                    "it first (that MCP tool populates the doc-structure store; "
                    "there is no separate reindex_document tool)"
                ),
            }
        try:
            result = await store.add_figure(
                doc_row["id"], file_path,
                caption=caption,
                semantic_label=args.get("semantic_label"),
            )
        except Exception as exc:  # noqa: BLE001 — indexing is best-effort
            return {"error": f"could not index figure: {exc}"}
        return {
            "project_id": args["project_id"],
            "document_id": doc_row["id"],
            **result,
        }
    if name == "find_similar_figure":
        # c623e648 — fuzzy-match a description OR a path against a document's
        # already-indexed figures (read-only counterpart of index_figure).
        validate_input_size(args.get("doc"), "figure doc", 2_000)
        validate_input_size(args.get("description_or_path"), "description_or_path", 10_000)
        if not args.get("project_id"):
            return {"error": "project_id is required"}
        doc_source = args.get("doc")
        query = args.get("description_or_path")
        if not doc_source:
            return {"error": "doc is required"}
        if not query:
            return {"error": "description_or_path is required"}
        store = await _resolve_ingest_doc_store(db, data_dir, tenant)
        if store is None:
            return {"project_id": args["project_id"], "document_id": None, "matches": []}
        try:
            doc_row = await store.get_document(args["project_id"], doc_source)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"could not resolve doc: {exc}"}
        if doc_row is None:
            return {"project_id": args["project_id"], "document_id": None, "matches": []}
        _limit = args.get("limit")
        try:
            _limit = int(_limit) if _limit is not None else 5
        except (TypeError, ValueError):
            _limit = 5
        # d2a3537a — cross-store resolve-through: when the caller names an
        # outputs_dir, each matched figure that carries a file_path is resolved
        # THROUGH to its outputs_index row (linked_output). Building the DuckDB
        # index is CPU-bound, so do it once off the event loop and hand the store
        # a resolver closure over the built index; skipped entirely when no
        # outputs_dir is given (the tool stays a pure fuzzy match by default).
        outputs_dir = str(args.get("outputs_dir") or "").strip()
        _resolver = None
        _index = None
        # Workspace decision 0dedff91 — the outputs resolve-through stats/walks
        # `outputs_dir` on THIS process's own filesystem (os.path.isdir + a
        # DuckDB rebuild over the tree). On hosted Meridian that path is on the
        # caller's machine, which the server can never reach, so skip the
        # resolve-through entirely (the figure match itself is DB-only and
        # still works). Never touch a caller's local dir server-side hosted.
        if _hosted_mode():
            outputs_dir = ""
        if outputs_dir and os.path.isdir(outputs_dir):
            from .. import outputs_indexer as _outputs_indexer  # noqa: PLC0415
            _index = _outputs_indexer.OutputsFtsIndex(outputs_dir)
            try:
                await asyncio.to_thread(_index.rebuild)
                _resolver = _index.resolve_output
            except Exception:  # noqa: BLE001 — resolve-through is advisory glue
                _resolver = None
        try:
            matches = await store.find_similar_figures(
                doc_row["id"], query, limit=_limit, output_resolver=_resolver,
            )
        except Exception as exc:  # noqa: BLE001 — read must not crash the tool call
            return {"error": f"could not find similar figures: {exc}"}
        finally:
            if _index is not None:
                _index.close()
        return {
            "project_id": args["project_id"],
            "document_id": doc_row["id"],
            "matches": matches,
        }
    if name == "index_table":
        # 2622182d — index ONE table into the SEMANTIC table index (dedup +
        # similarity on a normalized caption), the direct parallel of
        # index_figure. Complementary to the structural kind='table'
        # doc_elements placement, not a duplicate of it.
        validate_input_size(args.get("doc"), "table doc", 2_000)
        validate_input_size(args.get("caption"), "caption", 10_000)
        validate_input_size(args.get("semantic_label"), "semantic_label", 500)
        if not args.get("project_id"):
            return {"error": "project_id is required"}
        doc_source = args.get("doc")
        caption = args.get("caption")
        table_index_raw = args.get("table_index")
        table_index: int | None = None
        if table_index_raw is not None:
            try:
                table_index = int(table_index_raw)
            except (TypeError, ValueError):
                return {"error": f"table_index must be an integer, got {table_index_raw!r}"}
        if not doc_source:
            return {"error": "doc is required"}
        if table_index is None and not caption:
            return {"error": "at least one of table_index or caption is required"}
        store = await _resolve_ingest_doc_store(db, data_dir, tenant)
        if store is None:
            return {"error": "document-structure store unavailable"}
        try:
            doc_row = await store.get_document(args["project_id"], doc_source)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"could not resolve doc: {exc}"}
        if doc_row is None:
            return {
                "error": (
                    f"no stored document for doc={doc_source!r} — ingest_document "
                    "it first (that MCP tool populates the doc-structure store; "
                    "there is no separate reindex_document tool)"
                ),
            }
        try:
            result = await store.add_table(
                doc_row["id"], table_index,
                caption=caption,
                semantic_label=args.get("semantic_label"),
                paired_figure_id=args.get("paired_figure_id"),
            )
        except Exception as exc:  # noqa: BLE001 — indexing is best-effort
            return {"error": f"could not index table: {exc}"}
        return {
            "project_id": args["project_id"],
            "document_id": doc_row["id"],
            **result,
        }
    if name == "find_similar_table":
        # 2622182d — fuzzy-match a description against a document's
        # already-indexed tables (read-only counterpart of index_table).
        validate_input_size(args.get("doc"), "table doc", 2_000)
        validate_input_size(args.get("description"), "description", 10_000)
        if not args.get("project_id"):
            return {"error": "project_id is required"}
        doc_source = args.get("doc")
        query = args.get("description")
        if not doc_source:
            return {"error": "doc is required"}
        if not query:
            return {"error": "description is required"}
        store = await _resolve_ingest_doc_store(db, data_dir, tenant)
        if store is None:
            return {"project_id": args["project_id"], "document_id": None, "matches": []}
        try:
            doc_row = await store.get_document(args["project_id"], doc_source)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"could not resolve doc: {exc}"}
        if doc_row is None:
            return {"project_id": args["project_id"], "document_id": None, "matches": []}
        _limit = args.get("limit")
        try:
            _limit = int(_limit) if _limit is not None else 5
        except (TypeError, ValueError):
            _limit = 5
        try:
            matches = await store.find_similar_tables(
                doc_row["id"], query, limit=_limit,
            )
        except Exception as exc:  # noqa: BLE001 — read must not crash the tool call
            return {"error": f"could not find similar tables: {exc}"}
        return {
            "project_id": args["project_id"],
            "document_id": doc_row["id"],
            "matches": matches,
        }
    if name == "add_insight":
        # 0b711a9d — durable strategic insight (dedicated table, not a note).
        validate_input_size(args.get("title"), "insight title", 500)
        validate_input_size(args.get("body"), "insight body", 1_000_000)
        return await db_module.create_insight(
            db, args["project_id"], args["title"], args.get("body") or "",
            horizon=args.get("horizon", "quarter"),
            tags=args.get("tags"),
        )
    if name == "get_insights":
        return await db_module.get_insights(
            db, args["project_id"], horizon=args.get("horizon")
        )
    if name == "save_finding":
        # e1f43ee7 — phase-agnostic capture primitive (decoupled from search).
        validate_input_size(args.get("summary"), "finding summary", 1_000_000)
        validate_input_size(args.get("source_url"), "source_url", 2_000)
        if not (args.get("summary") or "").strip():
            return {"error": "save_finding requires a non-empty summary"}
        try:
            return await db_module.save_finding(
                db, args["project_id"], args.get("summary") or "",
                source_url=args.get("source_url"),
                source_type=args.get("source_type", "web"),
                decision_id=args.get("decision_id"),
            )
        except ValueError as exc:
            return {"error": str(exc)}
    if name == "capture_research_finding":
        # b1d36e93 — web/paper-shaped wrapper over save_finding; arXiv URLs are
        # auto-tagged source_type=arxiv.
        validate_input_size(args.get("summary"), "finding summary", 1_000_000)
        validate_input_size(args.get("url"), "url", 2_000)
        _url = (args.get("url") or "").strip()
        if not _url:
            return {"error": "capture_research_finding requires a url"}
        if not (args.get("summary") or "").strip():
            return {"error": "capture_research_finding requires a non-empty summary"}
        _st = "arxiv" if "arxiv.org" in _url.lower() else "web"
        try:
            return await db_module.save_finding(
                db, args["project_id"], args.get("summary") or "",
                source_url=_url, source_type=_st,
                decision_id=args.get("related_decision_id"),
            )
        except ValueError as exc:
            return {"error": str(exc)}
    if name == "get_notes":
        # 5a5bba43 — pull model: default to the lightweight list (no bodies) so
        # bulk note injection can't overflow context. Agents fetch one body via
        # read_note(slug). Pass bodies=true to opt back into full rows.
        # 9fa119dd — cursor pagination, opt-in (mirrors get_sprint_items, whose
        # MCP tool stays a bare list while the HTTP route paginates): pass
        # ``cursor`` and/or ``limit`` to get the {notes, has_more, next_cursor}
        # envelope, then re-call with cursor=next_cursor for the next page.
        # Without either arg the legacy bare list is returned for back-compat.
        # 98890df1 — relevance sort (reference_count/recency/decision-link) takes
        # precedence over cursor/limit paging.
        if args.get("sort") == "relevance":
            return await db_module.get_project_notes_ranked(
                db, args["project_id"], tag=args.get("tag"), query=args.get("query"),
                bodies=bool(args.get("bodies", False)),
                limit=(int(args["limit"]) if "limit" in args else None),
            )
        if "cursor" in args or "limit" in args:
            return await db_module.get_project_notes_page(
                db, args["project_id"], tag=args.get("tag"), query=args.get("query"),
                bodies=bool(args.get("bodies", False)),
                limit=int(args.get("limit", 100)),
                cursor=int(args.get("cursor", 0)),
            )
        return await db_module.get_project_notes(
            db, args["project_id"], tag=args.get("tag"), query=args.get("query"),
            bodies=bool(args.get("bodies", False)),
        )
    if name == "read_note":
        # 5a5bba43 — the pull half of the list→read model: fetch one note's full
        # body by its per-project slug (returned in the get_notes list).
        note = await db_module.get_project_note_by_slug(
            db, args["project_id"], args["slug"],
        )
        if note is None:
            return {"error": f"note '{args['slug']}' not found in project {args['project_id']}"}
        return note
    if name == "delete_note":
        ok = await db_module.delete_project_note(db, args["note_id"])
        return {"deleted": ok}
    if name == "add_workspace_note":
        validate_input_size(args.get("title"), "note title", 500)
        validate_input_size(args.get("body"), "note body", 10_000_000)
        result = await db_module.add_workspace_note(
            db, args["title"], args["body"], args.get("tags"),
            tenant_id=_mcp_tenant_id,
        )
        # 22c274bd — soft scope nudge; never blocks the write.
        warning = _workspace_scope_warning(args.get("title"), args.get("body"))
        if warning and isinstance(result, dict):
            result["scope_warning"] = warning
        return result
    if name == "get_workspace_notes":
        return await db_module.get_workspace_notes(
            db, tag=args.get("tag"), tenant_id=_mcp_tenant_id,
        )
    if name == "pin_workspace_decision":
        validate_input_size(args.get("title"), "decision title", 500)
        validate_input_size(args.get("body"), "decision body", 100_000)
        result = await db_module.pin_workspace_decision(
            db, args["title"], args["body"],
            category=args.get("category", "TECHNICAL"),
            tenant_id=_mcp_tenant_id,
        )
        # 22c274bd — soft scope nudge; never blocks the write.
        warning = _workspace_scope_warning(args.get("title"), args.get("body"))
        if warning and isinstance(result, dict):
            result["scope_warning"] = warning
        return result
    if name == "get_workspace_decisions":
        return await db_module.get_workspace_decisions(
            db, include_superseded=args.get("include_superseded", False),
            tenant_id=_mcp_tenant_id,
        )
    if name == "get_workspace_settings":
        return await db_module.get_workspace_settings(db, tenant_id=_mcp_tenant_id)
    if name == "update_workspace_settings":
        return await db_module.update_workspace_settings(
            db,
            hitl_auto_answer_default=args.get("hitl_auto_answer_default"),
            sprint_name_default=args.get("sprint_name_default"),
            handoff_template=args.get("handoff_template"),
            # 0bf67524 — cascade defaults for new projects.
            execution_mode_default=args.get("execution_mode_default"),
            code_intel_enabled_default=args.get("code_intel_enabled_default"),
            # 76cf8bda — /loop auto-continue workspace default.
            loop_enabled_default=args.get("loop_enabled_default"),
            # 36fea6ca — inline resolved sprint-item pointers in the handoff.
            handoff_inline_pointers=args.get("handoff_inline_pointers"),
            tenant_id=_mcp_tenant_id,
        )
    if name == "save_blog_post":
        validate_input_size(args.get("title"), "blog title", 500)
        validate_input_size(args.get("body"), "blog body", 1_000_000)
        return await db_module.save_blog_post(
            db, args["title"], args.get("body", ""),
            status=args.get("status", "draft"),
            slug=args.get("slug"),
            post_id=args.get("id"),
            tenant_id=_mcp_tenant_id,
        )
    if name == "get_blog_posts":
        return await db_module.get_blog_posts(
            db, tenant_id=_mcp_tenant_id, status=args.get("status"),
        )
    if name == "add_workspace_sprint_item":
        validate_input_size(args.get("title"), "sprint item title", 500)
        return await db_module.add_workspace_sprint_item(
            db, args["title"],
            item_group=args.get("group"),
            human_id=args.get("human_id"),
            tenant_id=_mcp_tenant_id,
        )
    if name == "get_workspace_sprint_items":
        return await db_module.get_workspace_sprint_items(
            db, status=args.get("status"), item_group=args.get("group"),
            tenant_id=_mcp_tenant_id,
        )
    if name == "update_workspace_sprint_item":
        validate_input_size(args.get("title"), "sprint item title", 500)
        item = await db_module.update_workspace_sprint_item(
            db, args["item_id"],
            title=args.get("title"),
            status=args.get("status"),
            item_group=args.get("group"),
            human_id=args.get("human_id"),
            tenant_id=_mcp_tenant_id,
        )
        return item or {"error": "workspace sprint item not found"}
    if name == "complete_workspace_sprint_item":
        item = await db_module.complete_workspace_sprint_item(
            db, args["item_id"], tenant_id=_mcp_tenant_id,
        )
        return item or {"error": "workspace sprint item not found"}
    # 5c4dcc0f — workspace proposals lifecycle
    if name == "add_workspace_proposal":
        validate_input_size(args.get("title"), "proposal title", 500)
        validate_input_size(args.get("body"), "proposal body", 100_000)
        return await db_module.add_workspace_proposal(
            db, args["title"], args["body"],
            tags=args.get("tags"),
            tenant_id=_mcp_tenant_id,
        )
    if name == "get_workspace_proposals":
        return await db_module.get_workspace_proposals(
            db, status=args.get("status"), tag=args.get("tag"),
            tenant_id=_mcp_tenant_id,
        )
    if name == "advance_proposal_status":
        try:
            result = await db_module.advance_workspace_proposal_status(
                db, args["proposal_id"], args["status"],
                tenant_id=_mcp_tenant_id,
            )
        except ValueError as exc:
            return {"error": str(exc)}
        return result or {"error": "proposal not found"}
    if name == "promote_proposal":
        _promo_project_id = args.get("project_id") or ""
        if not _promo_project_id:
            return {"error": "project_id (or project_name) is required for promote_proposal"}
        try:
            result = await db_module.promote_workspace_proposal(
                db, args["proposal_id"], _promo_project_id,
                sprint_item_title=args.get("sprint_item_title"),
                sprint_item_version=args.get("sprint_item_version"),
                tenant_id=_mcp_tenant_id,
            )
        except ValueError as exc:
            return {"error": str(exc)}
        return result
    return _MISS


async def _handle_hitl_tools(
    name: str,
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """Dispatch group: request_hitl, get_hitl_request, list_hitl_requests, answer_hitl, dismiss_hitl, update_md_section."""
    if name == "request_hitl":
        validate_input_size(args.get("question"), "question", 10_000)
        validate_input_size(args.get("context"), "context", 50_000)
        _hitl_kind = args.get("kind", "question")
        if _hitl_kind not in ("question", "correction"):
            _hitl_kind = "question"
        _hitl_options = args.get("options")
        if _hitl_options is not None and not isinstance(_hitl_options, list):
            _hitl_options = None
        result = await db_module.request_hitl(
            db, args["project_id"], args["question"],
            session_id=args.get("session_id"),
            context=args.get("context"),
            urgency=args.get("urgency", "normal"),
            assigned_to=args.get("assigned_to"),
            kind=_hitl_kind,
            options=_hitl_options,
            recommended=args.get("recommended"),
            require_human=bool(args.get("require_human", False)),
        )
        # v3.4 — auto-answered requests need no human; skip the notification.
        if result.get("answered_by") != "auto":
            # Notify via configured notify_url — best-effort, non-blocking
            _hitl_urgency = args.get("urgency", "normal").upper()
            _hitl_q = args["question"][:200]
            _hitl_base = os.environ.get("MERIDIAN_BASE_URL", "https://usemeridian.us").rstrip("/")
            await _server._maybe_notify(
                db, args["project_id"],
                f"Action needed ({_hitl_urgency})",
                f"{_hitl_q}\n\nAnswer at: {_hitl_base}/dashboard",
                event="hitl",
                tenant=tenant,
                pref_key="hitl",
            )
        # Dual-channel: enrich response so Claude Code displays the question inline.
        # The auto-answer short-circuit above already returned if answered_by='auto'.
        _dc_result = dict(result)
        if _dc_result.get("status") == "pending":
            _dc_q = _dc_result.get("question", "")
            _dc_urg = _dc_result.get("urgency", "normal")
            _dc_rid = _dc_result.get("id", "")
            _dc_opts: list[str] = []
            _dc_rec: str | None = None
            try:
                _dc_pl = json.loads(_dc_result.get("payload") or "{}")
                _dc_opts = _dc_pl.get("options") or []
                _dc_rec = _dc_pl.get("recommended")
            except (TypeError, ValueError):
                pass
            _opts_text = ""
            if _dc_opts:
                _opts_text = "\n\nOptions:\n" + "\n".join(
                    f"  {i + 1}. {o}" + (" (recommended)" if o == _dc_rec else "")
                    for i, o in enumerate(_dc_opts)
                )
            _dc_result["chat_prompt"] = (
                f"[HITL {_dc_urg.upper()}] {_dc_q}{_opts_text}\n\n"
                f"Filed in Meridian dashboard AND shown here — first answer wins.\n"
                f"HITL ID: {_dc_rid}"
            )
            if _dc_urg == "blocking":
                _dc_result["poll_instruction"] = (
                    f"Show the question above to the user now. Then poll "
                    f"get_hitl_request(request_id='{_dc_rid}') every 30 s. "
                    f"If the user answers here in chat, call "
                    f"answer_hitl(request_id='{_dc_rid}', answer='<their reply>'). "
                    f"Whichever arrives first (dashboard or chat) unblocks you."
                )
        return _dc_result
    if name == "get_hitl_request":
        result = await db_module.get_hitl_request(db, args["request_id"])
        if result is None:
            raise ValueError("hitl request not found")
        return result
    if name == "list_hitl_requests":
        # dcf1e428 — default 'recent' (pending + answered last 24h) so dismissed HITLs
        # don't give false "no pending HITLs" confidence to planning sessions.
        status_filter = args.get("status", "recent")
        if status_filter == "all":
            status_filter = None
        # project_id is optional — None lists across all projects (like the
        # dashboard), so cross-project HITLs aren't missed (277567dc).
        return await db_module.list_hitl_requests(
            db, args.get("project_id"),
            status=status_filter,
            limit=args.get("limit", 50),
        )
    if name == "answer_hitl":
        result = await _server._answer_hitl_and_apply(
            db, args["request_id"], args["answer"],
            answered_by=args.get("answered_by"), approved=True,
        )
        if result is None:
            raise ValueError("hitl request not found")
        return result
    if name == "dismiss_hitl":
        result = await db_module.dismiss_hitl_request(db, args["request_id"])
        if result is not None:
            await _server._on_hitl_answered(db, result, approved=False)
        if result is None:
            raise ValueError("hitl request not found")
        return result
    if name == "update_md_section":
        md_file = args["file"]
        anchor = args["anchor"]
        content = args["content"]
        # Raises ValueError for non-replace anchors / unknown files / README.
        md_anchors_module.assert_replace_target(md_file, anchor)
        # v1.1 — force=true: human planning sessions (claude.ai) skip the HITL
        # round-trip and apply the section replacement directly. Executor sessions
        # omit force (default False) so the diff stays human-gated as before.
        if args.get("force") in (True, 1, "true", "1", "yes"):
            try:
                path = await md_anchors_module.apply_replace(md_file, anchor, content)
            except md_anchors_module.AnchorError as exc:
                return {"applied": False, "apply_error": str(exc)}
            except Exception as exc:  # noqa: BLE001 — never crash the tool call
                return {"applied": False, "apply_error": f"write failed: {exc}"}
            if path is None:
                return {"applied": False, "reason": "no-op-or-hosted"}
            return {"applied": True, "forced": True, "file": md_file, "anchor": anchor}
        diff = md_anchors_module.build_diff(md_file, anchor, content)
        payload = json.dumps({
            "file": md_file,
            "anchor": anchor,
            "content": content,
            "base_hash": md_anchors_module.anchor_content_hash(md_file, anchor),
            "diff": diff,
        })
        return await db_module.request_hitl(
            db, args["project_id"],
            question=f"Approve update to {md_file} § {anchor}?",
            session_id=args.get("session_id"),
            context=(
                f"Proposed section replacement for {md_file} (anchor: {anchor}). "
                "Review the diff in the dashboard, then Approve or Reject."
            ),
            urgency=args.get("urgency", "normal"),
            kind="md_section_update",
            payload=payload,
        )
    return _MISS


async def _handle_session_tools(
    name: str,
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """Dispatch group: checkpoint, get_context_block, list_sessions, get_session_log, get_agent_instructions, set_agent_instructions, set_executor_config, idle_until_session_done, search_all, get_session_brief."""
    if name == "checkpoint":
        session_id = args["session_id"]
        project_id = args["project_id"]
        await db_module.auto_capture_session(db, project_id, session_id)
        await _server._finalize_session_md(db, project_id, session_id)
        from .. import handoff as handoff_module_local
        # Fetch recent commits for reconcile annotations (non-fatal)
        _ckpt_project = await db_module.get_project(db, project_id)
        _commits = await _fetch_recent_commits(_ckpt_project or {}, tenant)
        try:
            _, content = await asyncio.wait_for(
                handoff_module_local.generate_handoff(
                    db, project_id, data_dir, mode="delta", session_id=session_id,
                    commit_messages=[c["message"] for c in _commits],
                    identity=_resolve_caller_identity(tenant),
                ),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            content = "delta handoff timed out"
        pending_items = await db_module.get_sprint_items(db, project_id, status="pending")
        # Log drift warnings for high-confidence reconcile matches (non-fatal)
        if _commits and pending_items:
            try:
                _matches = handoff_module_local.reconcile_sprint_items(
                    pending_items, _commits
                )
                for _m in _matches:
                    if _m.get("confidence") == "high":
                        _first_sha = (_m["matching_commits"][0].get("sha") or "")[:8]
                        await db_module.log_task(
                            db, session_id, project_id,
                            f"Sprint board drift detected: {_m['item_id'][:8]} "
                            f"may already be done (matches commit {_first_sha})",
                            status="pending",
                        )
            except Exception:  # noqa: BLE001
                pass
        ids_str = ", ".join(it["id"][:8] for it in pending_items[:8])
        next_goal = (
            f'/goal Complete sprint items: {", ".join(it["id"] for it in pending_items[:8])}. '
            f"Done when complete_sprint_item()\'d, tests pass, generate_handoff called."
        ) if pending_items else "/goal Continue work — all sprint items done."
        # 04f03ee4 — include start_session one-liner so next session can resume immediately
        # 11a91d31 — default to project_name (idiomatic per 8a449ec0); project_id as a comment.
        try:
            _ck_proj = await db_module.get_project(db, project_id)
            _ck_pname = (_ck_proj or {}).get("name") or project_id
        except Exception:  # noqa: BLE001
            _ck_pname = project_id
        start_fresh = (
            f'start_session(project_name="{_ck_pname}", session_name="describe-what-youre-doing")'
            f'  # project_id={project_id}'
        )
        # fa595ad8 — store snapshot for Recent Sessions dashboard panel (non-fatal)
        # v3.1 — snapshot now lives on sessions.checkpoint_data, not a checkpoint:* note.
        try:
            from datetime import datetime as _ckpt_dt, timezone as _ckpt_tz
            async with db.execute(
                "SELECT name FROM sessions WHERE id = ?", (session_id,)
            ) as _sc:
                _sr = await _sc.fetchone()
            _session_name = (_sr["name"] if _sr else None) or session_id[:8]
            async with db.execute(
                "SELECT COUNT(*) AS n FROM task_log "
                "WHERE session_id = ? AND status = 'done'",
                (session_id,),
            ) as _tc:
                _tr = await _tc.fetchone()
            _items_done = int(_tr["n"]) if _tr else 0
            _summary_line = (content or "").split("\n")[0][:140]
            await db_module.set_session_checkpoint(
                db, session_id,
                {
                    "session_id": session_id,
                    "session_name": _session_name,
                    "items_done": _items_done,
                    "summary_line": _summary_line,
                    "next_goal": next_goal,
                    "start_fresh": start_fresh,
                    "checkpointed_at": _ckpt_dt.now(_ckpt_tz.utc).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                },
            )
        except Exception:
            pass  # non-fatal — checkpoint still returns normally
        # Write plain-text session_summary for RECENT RUNS panel display
        try:
            _shipped_titles: list[str] = []
            async with db.execute(
                "SELECT si.title FROM sprint_items si "
                "JOIN task_log tl ON tl.id = si.task_id "
                "WHERE tl.session_id = ? AND si.status = 'done'",
                (session_id,),
            ) as _si_cur:
                for _si_row in await _si_cur.fetchall():
                    _t = _si_row["title"] if hasattr(_si_row, "__getitem__") else _si_row[0]
                    if _t not in _shipped_titles:
                        _shipped_titles.append(_t)
            async with db.execute(
                "SELECT DISTINCT si.title FROM sprint_items si "
                "JOIN task_log tl ON tl.sprint_item_id = si.id "
                "WHERE tl.session_id = ? AND tl.status = 'done' AND si.status = 'done'",
                (session_id,),
            ) as _si_cur2:
                for _si_row2 in await _si_cur2.fetchall():
                    _t2 = _si_row2["title"] if hasattr(_si_row2, "__getitem__") else _si_row2[0]
                    if _t2 not in _shipped_titles:
                        _shipped_titles.append(_t2)
            _shipped_str = ", ".join(_shipped_titles) if _shipped_titles else "none"
            _plain_summary = (
                f"Shipped: {_shipped_str}. "
                f"Tasks done: {_items_done}. "
                f"Deploy: no."
            )
            await db.execute(
                "UPDATE sessions SET session_summary = ? WHERE id = ?",
                (_plain_summary, session_id),
            )
        except Exception:
            pass  # non-fatal
        # 1c4fdd6c — sprint-drift guard: sweep in_progress items and surface them
        # so the executor confirms done/not-done before the handoff is final
        # (the #1 cause of board drift is forgetting complete_sprint_item).
        _in_progress = await db_module.get_sprint_items(
            db, project_id, status="in_progress"
        )
        _ckpt_resp = {
            "summary": content,
            "pending_count": len(pending_items),
            "pending_ids": ids_str,
            "next_goal": next_goal,
            "start_fresh": start_fresh,
        }
        if _in_progress:
            _ckpt_resp["in_progress_items"] = [
                {"id": it["id"], "title": (it.get("title") or "")[:80]}
                for it in _in_progress
            ]
            _n = len(_in_progress)
            _ckpt_resp["action_required"] = (
                f"You have {_n} in_progress sprint item{'s' if _n != 1 else ''}. "
                "Before this handoff is final, confirm each: call "
                "complete_sprint_item(item_id) for any that shipped, or leave it "
                "in_progress / fail_sprint_item(item_id, reason) if not done. "
                "These are NOT auto-reconciled from git — you must mark them."
            )
        return _ckpt_resp
    if name == "get_context_block":
        # v2.3 — assemble the same shape as /projects/{id}/context-block but
        # return both the rendered text AND the source dict so MCP clients
        # can choose to render their own variant.
        # v2.5 — wrap in semantic XML for better Claude Code parsing.
        project_id = args["project_id"]
        mode = args.get("mode", "full")
        project = await db_module.get_project(db, project_id)
        if project is None:
            raise ValueError("project not found")
        goal = await db_module.get_goal(db, project_id)
        sprint_items = await db_module.get_sprint_items(
            db, project_id, status="pending"
        )
        all_tasks = await db_module.get_tasks(db, project_id, limit=20)
        pending_tasks = [
            t for t in all_tasks if t.get("status") in ("pending", "in_progress", "done")
        ][:10]
        sessions = await db_module.get_sessions(db, project_id, active_only=True)
        decisions_raw = (project.get("decisions") or "").strip()
        recent_decisions = [
            l.strip() for l in decisions_raw.splitlines() if l.strip()
        ][-5:]
        text = _server._render_context_block(
            project, goal, sprint_items, pending_tasks, sessions, recent_decisions,
            mode=mode,
        )
        # v3.1 — workspace decisions + notes apply across all projects; surface
        # them at the very top so a fresh session sees org-wide truth first.
        ws_decisions = await db_module.get_workspace_decisions(db, tenant_id=_mcp_tenant_id)
        ws_notes = await db_module.get_workspace_notes(db, tenant_id=_mcp_tenant_id)
        ws_block = _server._render_workspace_block(ws_decisions, ws_notes)
        if ws_block:
            text = f"{ws_block}\n\n{text}"
        xml_text = f'<meridian_context project_id="{project_id}" mode="{mode}">\n{text}\n</meridian_context>'
        return {"mode": mode, "text": xml_text, "project_id": project_id}
    if name == "list_sessions":
        active_only = args.get("status", "active") != "all"
        return await db_module.get_sessions(db, args["project_id"], active_only=active_only)
    if name == "get_session_log":
        run = await db_module.get_executor_run_by_session(db, args.get("session_id", ""))
        if run is None:
            return {"error": "no run found for session"}
        return {
            "run_id": run["id"],
            "session_id": run["session_id"],
            "started_at": run["started_at"],
            "ended_at": run.get("ended_at"),
            "status": run["status"],
            "task_count": run["task_count"],
            "transcript": run["transcript"],
        }
    if name == "get_agent_instructions":
        instructions = await db_module.get_agent_instructions(db, args["project_id"])
        return {"project_id": args["project_id"], "agent_instructions": instructions}
    if name == "set_agent_instructions":
        validate_input_size(args.get("instructions"), "agent_instructions", 100_000)
        instructions = (args.get("instructions") or "").strip() or None
        return await db_module.set_agent_instructions(db, args["project_id"], instructions)
    if name == "set_executor_config":
        from ..executor_config import merge_repo_paths  # local — avoid import cycle
        # Merge onto the existing config so we never wipe other keys (repo_paths,
        # hostnames, filesystem_roots, …). repo_paths is merged entry-by-entry so
        # a manual {cwd, hostname} coexists with hook-registered ones.
        existing = await db_module.get_executor_config(db, args["project_id"])
        cfg = dict(existing) if isinstance(existing, dict) else {}
        # 3adbc954 — filesystem_roots, hostnames, context_threshold and isolation
        # were missing from this copy list, so passing them was silently dropped
        # (the call returned filesystem_roots: []). All scalar/list keys overwrite;
        # repo_paths alone is merged entry-by-entry below.
        for k in ("repo_path", "env_file", "test_cmd", "test_min",
                  "deploy_cmd", "shell_type", "branch",
                  "filesystem_roots", "hostnames", "context_threshold",
                  "isolation", "max_turns",
                  # b970fe07 — dashboard-configurable Serena default repo + code-intel
                  # index dirs (mirror filesystem_roots: scalar/list overwrite).
                  "serena_repo_path", "codebase_code_dirs"):
            if k in args:
                cfg[k] = args[k]
        if "repo_paths" in args:
            cfg["repo_paths"] = merge_repo_paths(cfg.get("repo_paths"), args["repo_paths"])
        return await db_module.set_executor_config(db, args["project_id"], cfg)
    if name == "idle_until_session_done":
        _idle_kwargs: dict[str, Any] = {}
        if args.get("timeout_seconds") is not None:
            _idle_kwargs["timeout_seconds"] = float(args["timeout_seconds"])
        return await _server._idle_until_session_done(
            db, args["watching_session_id"], **_idle_kwargs
        )
    if name == "search_all":
        return await db_module.search_all(
            db, args["project_id"], args["query"],
            limit=args.get("limit", 10),
        )
    if name == "search_synthesis":
        # ebc242ad — a natural-language query gets a short, CITED answer on top of
        # the same tsvector/LIKE retrieval as search_all, not just a list of hits.
        # Reuses the Haiku-tier pattern (deterministic fallback to raw results when
        # ANTHROPIC_API_KEY is unset or the call fails).
        if not args.get("query"):
            return {"error": "query is required"}
        results = await db_module.search_all(
            db, args["project_id"], args["query"], limit=args.get("limit", 10),
        )
        from ..handoff import synthesize_search_answer  # noqa: PLC0415
        synth = await synthesize_search_answer(args["query"], results)
        return {"query": args["query"], **synth, "results": results}
    if name == "paper_search":
        # 811881c6 — real callable arXiv search so the research-routing protocol's
        # "use the paper-search MCP first" finally points at a tool that exists (it was
        # instruction-only before). Keyless external lookup; degrades to {error}, never
        # raises. No project scope needed — it's an external search.
        # f65f6111 — 'source' routes between two keyless sources: arxiv (default) and
        # openalex. Both return the same {query, count, results} shape.
        from ..paper_search import arxiv_search, openalex_search  # noqa: PLC0415
        source = str(args.get("source", "arxiv") or "arxiv").strip().lower()
        search = openalex_search if source == "openalex" else arxiv_search
        return await search(
            args.get("query", ""),
            limit=args.get("limit", 10),
            sort_by=args.get("sort_by", "relevance"),
        )
    if name == "get_session_brief":
        # v2.5 — single-call orientation, <500 tokens, XML output.
        project_id = args["project_id"]
        role = args.get("role", "worker")
        session_id_for_notes = args.get("session_id")
        goal = await db_module.get_goal(db, project_id)
        tasks = await db_module.get_tasks(db, project_id, limit=5)
        # dcf1e428 — use 'recent' to surface pending + answered last 24h for planning sessions
        hitl_rows = await db_module.list_hitl_requests(db, project_id, status="recent")
        sprint_items = await db_module.get_sprint_items(db, project_id, status="pending")
        # 0507f4a1 — sprint progress summary for session brief
        _all_items_for_progress = await db_module.get_sprint_items(db, project_id)
        _done_count = sum(1 for it in _all_items_for_progress if it.get("status") == "done")
        _total_count = len(_all_items_for_progress)
        _pct = round(100 * _done_count / _total_count) if _total_count else 0
        blocking = [t for t in tasks if t.get("status") == "failed"]
        sprint_str = (goal.get("sprint") or "") if goal else ""
        tasks_xml = "\n".join(
            f'  <task status="{t.get("status","?")}">{(t.get("description") or "")[:80]}</task>'
            for t in tasks
        )
        sprint_items_xml = "\n".join(
            f'  <item version="{it.get("version","")}">{(it.get("title") or "")[:80]}</item>'
            for it in sprint_items[:5]
        )
        # 277567dc — surface pending HITLs so session sees what needs a human decision.
        # dcf1e428 — also surface recently answered HITLs so planning sessions can see what was decided.
        _pending_hitls = [h for h in hitl_rows if h.get("status") == "pending"]
        _answered_hitls = [h for h in hitl_rows if h.get("status") != "pending"]
        if _pending_hitls:
            hitl_xml = (
                f'<hitl_pending count="{len(_pending_hitls)}">\n'
                + "\n".join(
                    f'  <request id="{h.get("id","")}" urgency="{h.get("urgency","normal")}">'
                    f'{(h.get("question") or "")[:140]}</request>'
                    for h in _pending_hitls[:5]
                )
                + "\n</hitl_pending>"
            )
        else:
            hitl_xml = ""
        if _answered_hitls:
            hitl_xml += (
                f'\n<hitl_recent count="{len(_answered_hitls)}">\n'
                + "\n".join(
                    f'  <request status="{h.get("status","?")}">'
                    f'Q: {(h.get("question") or "")[:80]} '
                    f'A: {(h.get("answer") or "")[:80]}</request>'
                    for h in _answered_hitls[:3]
                )
                + "\n</hitl_recent>"
            )
        blocking_xml = f'<blocking>{(blocking[0].get("description") or "")[:100]}</blocking>' if blocking else ""
        # v2.6 — include session scratch-pad notes at top of brief
        notes_xml = ""
        new_items_xml = ""
        if session_id_for_notes:
            try:
                session_notes = await db_module.get_session_notes(db, session_id_for_notes)
                if session_notes:
                    notes_xml = "<session_notes>\n" + "\n".join(
                        f'  <note title="{n.get("title","")}">{(n.get("body") or "")[:120]}</note>'
                        for n in session_notes
                    ) + "\n</session_notes>\n"
            except Exception:
                pass
            # fd86aacc — show items added to the board since this session started
            try:
                _all_sess = await db_module.get_sessions(db, project_id, active_only=False)
                _curr_sess = next(
                    (s for s in _all_sess if s.get("id") == session_id_for_notes), None
                )
                if _curr_sess and _curr_sess.get("created_at"):
                    _sess_started = str(_curr_sess["created_at"])
                    _all_items = await db_module.get_sprint_items(db, project_id)
                    _new_count = sum(
                        1 for it in _all_items
                        if (it.get("added_at") or "") >= _sess_started
                    )
                    if _new_count > 0:
                        new_items_xml = (
                            f'<board_change>{_new_count} item{"s" if _new_count != 1 else ""}'
                            f' added since this session started</board_change>\n'
                        )
            except Exception:
                pass
        _progress_xml = (
            f'<progress done="{_done_count}" total="{_total_count}" pct="{_pct}%"/>\n'
            if _total_count else ""
        )
        # Sprint-4: planner role gets richer context — all decisions + all notes + active sessions.
        planner_extra_xml = ""
        if role == "planner":
            try:
                _decisions = await db_module.get_pinned_decisions(db, project_id, include_superseded=False)
                if _decisions:
                    # 366317e9 — already ordered urgent → normal → low by the DB
                    # layer; surface the priority so the planner weights them.
                    planner_extra_xml += "<decisions>\n" + "\n".join(
                        f'  <decision priority="{d.get("priority","normal")}" category="{d.get("category","")}">{(d.get("title") or "")}: {(d.get("body") or "")[:120]}</decision>'
                        for d in _decisions[:20]
                    ) + "\n</decisions>\n"
            except Exception:
                pass
            try:
                # 5a5bba43 — planner context renders note bodies, so ask for them.
                _wiki_notes = await db_module.get_project_notes(db, project_id, bodies=True)
                # High-priority notes first
                _wiki_notes = sorted(_wiki_notes, key=lambda n: {"high": 0, "normal": 1, "low": 2}.get(n.get("priority", "normal"), 1))
                if _wiki_notes:
                    planner_extra_xml += "<project_notes>\n" + "\n".join(
                        f'  <note priority="{n.get("priority","normal")}" kind="{n.get("note_kind","wiki")}" tags="{n.get("tags","")}">'
                        f'{(n.get("title") or "")}: {(n.get("body") or "")[:120]}</note>'
                        for n in _wiki_notes[:30]
                    ) + "\n</project_notes>\n"
            except Exception:
                pass
            try:
                _active_sessions = await db_module.get_sessions(db, project_id, active_only=True)
                if _active_sessions:
                    planner_extra_xml += "<active_sessions>\n" + "\n".join(
                        f'  <session id="{s.get("id","")}" name="{s.get("name","")}" human="{s.get("human_id","")}" last_seen="{s.get("last_seen","")[:19]}"/>'
                        for s in _active_sessions[:10]
                    ) + "\n</active_sessions>\n"
            except Exception:
                pass
        # 1750dccf — role-specific enrichment so executor and planner briefs differ.
        role_extra_xml = ""
        if role == "executor":
            # Version-scoped pending items + this session's file claims + the
            # decisions code-anchored to the files it holds.
            _sess_row = None
            if session_id_for_notes:
                _sess_all_x = await db_module.get_sessions(
                    db, project_id, active_only=False
                )
                _sess_row = next(
                    (s for s in _sess_all_x if s.get("id") == session_id_for_notes),
                    None,
                )
            _ver = (_sess_row or {}).get("sprint_version")
            if _ver:
                _scoped = [it for it in sprint_items if it.get("version") == _ver]
                role_extra_xml += (
                    f'<version_scope version="{_ver}" pending="{len(_scoped)}">\n'
                    + "\n".join(
                        f'  <item>{(it.get("title") or "")[:80]}</item>'
                        for it in _scoped[:5]
                    )
                    + "\n</version_scope>\n"
                )
            if session_id_for_notes:
                try:
                    _claimed = await db_module.get_session_file_claims(
                        db, session_id_for_notes
                    )
                except Exception:
                    _claimed = []
                if _claimed:
                    role_extra_xml += (
                        "<my_file_claims>\n"
                        + "\n".join(f"  <file>{c}</file>" for c in _claimed[:20])
                        + "\n</my_file_claims>\n"
                    )
                    _rel: list[dict[str, Any]] = []
                    _seen_dec: set[Any] = set()
                    for _fp in _claimed[:20]:
                        try:
                            for _d in await db_module.get_decisions_for_file(
                                db, project_id, _fp
                            ):
                                if _d.get("id") not in _seen_dec:
                                    _seen_dec.add(_d.get("id"))
                                    _rel.append(_d)
                        except Exception:
                            pass
                    if _rel:
                        role_extra_xml += (
                            "<relevant_decisions>\n"
                            + "\n".join(
                                f'  <decision anchor="{d.get("code_anchor","")}">'
                                f'{(d.get("title") or "")}: '
                                f'{(d.get("body") or "")[:100]}</decision>'
                                for d in _rel[:10]
                            )
                            + "\n</relevant_decisions>\n"
                        )
        elif role == "planner":
            # Last session summary + decisions sitting on unconfirmed assumptions
            # (what needs revisiting).
            try:
                _ls = await db_module.get_last_session_brief(
                    db, project_id, exclude_session_id=session_id_for_notes
                )
            except Exception:
                _ls = None
            if _ls:
                role_extra_xml += (
                    f'<last_session name="{_ls.get("name","")}" '
                    f'status="{_ls.get("status","")}">\n'
                    + "".join(
                        f'  <completed>{(ci.get("title") or "")[:80]}</completed>\n'
                        for ci in (_ls.get("completed_items") or [])[:8]
                    )
                    + "</last_session>\n"
                )
            try:
                _pin_r = await db_module.get_pinned_decisions(db, project_id)
                _revisit = [
                    d for d in _pin_r
                    if d.get("assumption")
                    and d.get("assumption_status") != "confirmed"
                ]
                if _revisit:
                    role_extra_xml += (
                        "<decisions_needing_revisit>\n"
                        + "\n".join(
                            f'  <decision status="{d.get("assumption_status")}">'
                            f'{(d.get("title") or "")}: assumption='
                            f'{(d.get("assumption") or "")[:80]}</decision>'
                            for d in _revisit[:10]
                        )
                        + "\n</decisions_needing_revisit>\n"
                    )
            except Exception:
                pass
        brief = (
            f'<session_brief project_id="{project_id}" role="{role}">\n'
            f'{notes_xml}'
            f'{new_items_xml}'
            f'{_progress_xml}'
            f'<sprint>{sprint_str[:200]}</sprint>\n'
            f'<pending_items>\n{sprint_items_xml}\n</pending_items>\n'
            f'<last_tasks>\n{tasks_xml}\n</last_tasks>\n'
            f'{blocking_xml}\n'
            f'{hitl_xml}\n'
            f'{planner_extra_xml}'
            f'{role_extra_xml}'
            f'</session_brief>'
        )
        return {"text": brief, "project_id": project_id, "role": role}
    return _MISS


async def _verify_item_ci(
    db: Any,
    project_id: str,
    tenant: dict[str, Any] | None,
    text: str,
) -> dict[str, Any] | None:
    """Independent GitHub Actions CI check for the commit referenced in ``text``.

    Returns the ``verify_commit_ci`` result dict (``{sha, repo, state, total,
    failed}``) or ``None`` when there is nothing to check (no commit SHA in the
    text, no ``github_repo`` configured, or any error). Best-effort and fully
    guarded — this NEVER raises; the caller decides whether a ``failure`` state
    hard-blocks or is merely advisory. Shared by ``complete_sprint_item`` so the
    pre-completion hard-block and the post-completion advisory reuse one lookup.
    """
    try:
        from .. import github_ci  # noqa: PLC0415

        _ci_sha = github_ci.extract_commit_sha(text)
        if not _ci_sha:
            return None
        _ci_project = await db_module.get_project(db, project_id)
        _ci_repo = ((_ci_project or {}).get("github_repo") or "").strip()
        if not _ci_repo:
            return None
        _ci_token = None
        _ci_tid = (tenant or {}).get("id") if tenant else None
        if _ci_tid:
            try:
                _ci_token, _ = await db_module.get_github_token_for_project(
                    db, _ci_tid, project_id
                )
            except Exception:  # noqa: BLE001
                _ci_token = None
        return await github_ci.verify_commit_ci(_ci_repo, _ci_sha, token=_ci_token)
    except Exception:  # noqa: BLE001 — advisory/best-effort; never raise
        return None


async def _handle_sprint_tools(
    name: str,
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """Dispatch group: add_sprint_note, get_sprint_notes, add_sprint_item, fan_out_sprint_items, update_sprint_item, set_sprint, get_sprint_progress, get_sprint_items, claim_sprint_item, add_subtask, split_sprint_item, merge_sprint_items, complete_sprint_item."""
    if name == "add_sprint_note":
        validate_input_size(args.get("title"), "note title", 500)
        validate_input_size(args.get("body"), "note body", 10_000_000)
        return await db_module.add_session_note(
            db, args["session_id"], args["title"], args["body"],
            note_kind=args.get("note_kind"),
        )
    if name == "get_sprint_notes":
        return await db_module.get_session_notes(
            db, args["session_id"], note_kind=args.get("note_kind")
        )
    if name == "add_sprint_item":
        # 8f01cdfe — project_name is an accepted alternative to project_id (the
        # dispatcher resolver at _dispatch_mcp_tool resolves a present, resolvable
        # project_name → project_id before we get here). If neither a project_id
        # nor a resolvable project_name reached us, return a clean, descriptive
        # error instead of letting the direct args["project_id"] reads below raise
        # a raw KeyError that leaks as a cryptic JSON-RPC -32603.
        if not args.get("project_id"):
            return {"error": "project_id is required (or pass project_name)"}
        validate_input_size(args.get("title"), "sprint item title", 500)
        # 7e212375 — codebase drift check: if the title looks already-implemented
        # (3+ keyword overlap with a specific migration or a recent commit),
        # block with a warning unless force=true. Closes the "adding items for
        # already-shipped work" gap. Migration check is offline (cached file);
        # the commit check degrades to empty if git isn't reachable.
        if not bool(args.get("force", False)):
            from .. import handoff as _handoff_drift
            try:
                _proj_for_drift = await db_module.get_project(db, args["project_id"])
                _drift_commits = (
                    await _fetch_recent_commits(_proj_for_drift, tenant)
                    if _proj_for_drift else []
                )
            except Exception:  # noqa: BLE001
                _drift_commits = []
            _drift = _handoff_drift.detect_sprint_item_drift(
                args.get("title") or "", _drift_commits,
            )
            if _drift:
                return {
                    "drift_warning": True,
                    "title": args.get("title"),
                    "matches": _drift[:5],
                    "message": (
                        "This may already be implemented — "
                        + "; ".join(f"{m['kind']}:{m['ref']}" for m in _drift[:3])
                        + ". Pass force=true to add it anyway."
                    ),
                }
        # fd86aacc — warn if active executor sessions exist when adding a new item
        _active_session_warnings = await _active_executor_session_warnings(db, args["project_id"])
        # 07bdfdbb — auto-infer touches_resources from the title when the caller
        # supplied none, so the item can parallelize instead of falling into its
        # own sequential undeclared group (de730a25).
        _touches = args.get("touches_resources")
        if not _touches:
            _touches = _infer_touches_resources(args.get("title") or "") or None
        try:
            _new_item = await db_module.add_sprint_item(
                db, args["project_id"], args["version"], args["title"],
                group=args.get("group"),
                human_id=args.get("human_id"),
                depends_on=args.get("depends_on"),
                failure_mode=args.get("failure_mode"),
                milestone_type=args.get("milestone_type", "task"),
                touches_resources=_touches,
                force=bool(args.get("force", False)),
                deferred_until=args.get("deferred_until"),
                track=args.get("track"),
                priority=args.get("priority"),
                blocker_kind=args.get("blocker_kind"),
                wave=args.get("wave"),
            )
        except ValueError as exc:
            # 501ec93f — malformed touches_resources identifier; also e08fee30 /
            # 2282a636 bad priority / blocker_kind. Surface, don't crash.
            return {"error": str(exc)}
        # b0d42ef6 — duplicate guard blocked the insert: surface the error as-is
        # (no item was created, so the warnings below don't apply).
        if isinstance(_new_item, dict) and _new_item.get("error") == "duplicate":
            return _new_item
        _extra: dict[str, Any] = {}
        if _active_session_warnings:
            _extra["active_session_warning"] = (
                "WARNING: " + "; ".join(_active_session_warnings)
                + " — new item added but may not be picked up until next session start."
            )
        # 2d932f60 — if the title reads like a decision/insight, propose capturing
        # it (no auto-write; the planner confirms).
        from .. import handoff as _handoff_ins  # noqa: PLC0415
        _ins_sig = _handoff_ins.detect_insight_candidate(args.get("title") or "")
        if _ins_sig:
            _extra["insight_hint"] = (
                f"The wording ('{_ins_sig}') looks like a decision/insight — consider "
                "add_insight() or pin_decision() so it isn't lost at session end."
            )
        # a8550238 — surface code-prospecting context INLINE at add time, so the
        # planner sees which real files/symbols the item touches (and can prospect
        # them) while the sprint is still being shaped — not only later at claim
        # time. 926bf221 — always report an explicit prospecting_status so the
        # caller can distinguish prospected / no-targets / skipped / errored.
        _pc_ctx, _pc_status = _prospecting_result(_new_item)
        _extra["prospecting_status"] = _pc_status
        if _pc_ctx:
            _extra["code_context"] = _pc_ctx
        # 691f4e1c — persist a DURABLE symbol pointer (not just the inline hint) when
        # the item declares real symbols, so prospecting is queryable later via
        # get_sprint_item_pointers / resolvable through the tunnel, instead of a
        # one-shot code_context the caller has to act on manually. Fully guarded.
        _ptr = await _persist_prospected_pointer(
            db, args["project_id"], _new_item, _pc_status
        )
        if _ptr:
            _extra["prospected_pointer"] = _ptr
        if _extra:
            _new_item = {**_new_item, **_extra}
        return _new_item
    if name == "fan_out_sprint_items":
        items = args.get("items")
        if not isinstance(items, list) or not items:
            return {"error": "items must be a non-empty list of {title, ...} dicts"}
        _enriched: list[dict] = []
        for spec in items:
            validate_input_size(spec.get("title"), "sprint item title", 500)
            validate_input_size(spec.get("description"), "sprint item description", 10_000)
            # 07bdfdbb — auto-infer per-item touches_resources when none supplied,
            # so a fanned-out batch doesn't become an all-undeclared pile-up.
            if isinstance(spec, dict) and not spec.get("touches_resources"):
                _inf = _infer_touches_resources(spec.get("title") or "")
                if _inf:
                    spec = {**spec, "touches_resources": _inf}
            _enriched.append(spec)
        ids = await db_module.fan_out_sprint_items(
            db, args["project_id"], _enriched
        )
        _result: dict[str, Any] = {"item_ids": ids, "count": len(ids)}
        # 586eeda9 — same active-session warning as add_sprint_item: a fanned-out
        # batch injected mid-run is a board change executors should know about.
        _fo_warnings = await _active_executor_session_warnings(db, args["project_id"])
        if _fo_warnings:
            _result["active_session_warning"] = (
                "WARNING: " + "; ".join(_fo_warnings)
                + " — items added but may not be picked up until next session start."
            )
        return _result
    if name == "update_sprint_item":
        validate_input_size(args.get("title"), "sprint item title", 500)
        validate_input_size(args.get("notes"), "sprint item notes", 50_000)
        # 586eeda9 — hard-block mutating an in_progress item: an executor owns it
        # and a concurrent title/notes/resources change is undefined behaviour.
        # force=true overrides (destructive). Mirrors the set_sprint force guard.
        if not (args.get("force") in (True, 1, "true", "1", "yes")):
            _cur = await db_module.get_sprint_item(db, args["item_id"])
            if _cur is not None and _cur.get("status") == "in_progress":
                _owner = ""
                _ca = _cur.get("claimed_at")
                if _ca:
                    _owner = f" (claimed {_ca})"
                return {
                    "error": "IN_PROGRESS",
                    "message": (
                        f"Item is in_progress and owned by an active session{_owner} — "
                        "cannot mutate while claimed. Wait for completion, call "
                        "fail_sprint_item first, or pass force=true to override."
                    ),
                    "item_id": args["item_id"],
                    "claimed_at": _ca,
                }
        _patch_kwargs: dict[str, Any] = dict(
            title=args.get("title"),
            version=args.get("version"),
            notes=args.get("notes"),
            human_id=args.get("human_id"),
            item_group=args.get("group"),
        )
        # 501ec93f — only forward touches_resources when the caller supplied it,
        # so omitting the key leaves the stored value untouched (_UNSET sentinel).
        if "touches_resources" in args:
            _patch_kwargs["touches_resources"] = args.get("touches_resources")
        # 5823db0b — allow flagging an item as requiring completion evidence.
        if "required_notes" in args:
            _patch_kwargs["required_notes"] = args.get("required_notes")
        # dec69708 — set/clear the enforced deferral + track. Only forward when the
        # caller supplied the key, so omitting it leaves the stored value untouched
        # (patch_sprint_item uses the _UNSET sentinel); pass "" / null to clear.
        if "deferred_until" in args:
            _patch_kwargs["deferred_until"] = args.get("deferred_until")
        if "track" in args:
            _patch_kwargs["track"] = args.get("track")
        # e08fee30 — set the priority enum. Only forward when supplied (None leaves
        # the stored value untouched in patch_sprint_item).
        if args.get("priority") is not None:
            _patch_kwargs["priority"] = args.get("priority")
        # 2282a636 — set/clear blocker_kind. Only forward when the caller supplied
        # the key, so omitting it leaves the stored value untouched (_UNSET
        # sentinel); pass "" / null to clear (ordinary item), or 'manual' to set.
        if "blocker_kind" in args:
            _patch_kwargs["blocker_kind"] = args.get("blocker_kind")
        # 58a45b92 — set/clear the stored wave label. Only forward when the caller
        # supplied the key (_UNSET sentinel), so omitting it leaves the stored value
        # untouched; pass "" / null to clear (unassigned).
        if "wave" in args:
            _patch_kwargs["wave"] = args.get("wave")
        try:
            item = await db_module.patch_sprint_item(
                db, args["project_id"], args["item_id"], **_patch_kwargs
            )
        except ValueError as exc:
            return {"error": str(exc)}
        if not item:
            return {"error": "sprint item not found"}
        # 926bf221 — update_sprint_item now auto-prospects too (previously only
        # add/claim did), so a substantive edit that names real files re-derives
        # code context instead of staying null. Explicit prospecting_status keeps
        # success / no-targets / skipped distinguishable to the caller.
        _u_ctx, _u_status = _prospecting_result(item)
        item = {**item, "prospecting_status": _u_status}
        if _u_ctx:
            item["code_context"] = _u_ctx
        # 691f4e1c — persist the durable symbol pointer on update too (skips if the
        # item already carries a code/symbol pointer, so re-editing doesn't stack
        # duplicates). Guarded; a persist failure never breaks the update.
        _u_ptr = await _persist_prospected_pointer(
            db, args["project_id"], item, _u_status
        )
        if _u_ptr:
            item["prospected_pointer"] = _u_ptr
        return item
    if name == "set_sprint":
        # 62d321dd — guard: warn if pending items were never started before rolling sprint
        if not args.get("force"):
            _unstarted = [
                it for it in await db_module.get_sprint_items(db, args["project_id"], status="pending")
                if it.get("claimed_at") is None
            ]
            if _unstarted:
                _list = "\n".join(
                    f'  [{it["id"][:8]}] {it.get("title","")[:100]}'
                    for it in _unstarted[:10]
                )
                return {
                    "warning": (
                        f"WARNING: {len(_unstarted)} item(s) from the current sprint were never "
                        f"started:\n{_list}\n"
                        "Proceeding will leave these orphaned. Move them to the new sprint version "
                        "or push to backlog first. Call set_sprint again with force=true to override."
                    ),
                    "unstarted_count": len(_unstarted),
                    "unstarted_ids": [it["id"] for it in _unstarted],
                    "sprint_not_updated": True,
                }
        result = await db_module.set_sprint(db, args["project_id"], args["sprint"])
        await goal_md_module.sync_db_to_goal_md(db, args["project_id"])
        return result
    if name == "get_sprint_progress":
        # 0507f4a1 — sprint progress summary
        _version_filter = args.get("version")
        _group_filter = args.get("item_group")
        # 10s cache: parallel executors polling between tasks share one DB query.
        _all = await db_module.get_sprint_items_cached(db, args["project_id"])
        if _version_filter:
            _all = [it for it in _all if it.get("version") == _version_filter]
        if _group_filter:
            _all = [it for it in _all if it.get("item_group") == _group_filter]
        _counts: dict[str, int] = {}
        for _it in _all:
            _st = _it.get("status") or "pending"
            _counts[_st] = _counts.get(_st, 0) + 1
        _done_n = _counts.get("done", 0)
        _total = len(_all)
        _pct = round(100 * _done_n / _total) if _total else 0
        _resp_progress = {
            "total": _total,
            "done": _done_n,
            "in_progress": _counts.get("in_progress", 0),
            "provisional_complete": _counts.get("provisional_complete", 0),
            "pending": _counts.get("pending", 0),
            "failed": _counts.get("failed", 0),
            "skipped": _counts.get("skipped", 0),
            "percent_complete": _pct,
            "by_status": _counts,
        }
        # 1da83459 — summary-only: the per-item list scaled ~100 chars/item and
        # bloated every between-item progress poll on a large board. Counts +
        # by_status + board_change are what an executor needs; call
        # get_sprint_items(status="pending") for the live item list.
        _bc = await _board_change_for_session(db, args["project_id"], args.get("session_id"))
        if _bc:
            _resp_progress["board_change"] = _bc
        # 5abf3e12 — when scoped to a session, report that session's live goal
        # compliance (N items it took on via actor= vs M complete_sprint_item()'d)
        # so an executor can see mid-run whether it's on track to fully complete
        # its /goal list. Guarded: never break the progress poll.
        _sess_id = args.get("session_id")
        if _sess_id:
            try:
                _resp_progress["goal_compliance"] = (
                    await db_module.compute_session_goal_compliance(
                        db, args["project_id"], _sess_id
                    )
                )
            except Exception:  # noqa: BLE001
                pass
        return _resp_progress
    if name == "get_sprint_items":
        include_human = args.get("human", True)
        if isinstance(include_human, bool):
            pass
        else:
            include_human = str(include_human).lower() not in ("false", "0", "no")
        _items = await db_module.get_sprint_items(
            db, args["project_id"],
            status=args.get("status"),
            include_human=include_human,
        )
        # 10c0f6a0 — stale-session warning: in_progress items claimed >2h ago
        from datetime import datetime as _dt_cls
        _now_utc = _dt_cls.utcnow()
        for _i, _it in enumerate(_items):
            if _it.get("status") == "in_progress" and _it.get("claimed_at"):
                try:
                    _ca = _dt_cls.fromisoformat(_it["claimed_at"].split(".")[0].replace("Z", ""))
                    _age_h = (_now_utc - _ca).total_seconds() / 3600
                    if _age_h > 2:
                        _items[_i] = {**_it, "stale_warning": True, "stale_age_hours": round(_age_h, 1)}
                except Exception:  # noqa: BLE001
                    pass
        return _items
    if name == "get_parallelizable_groups":
        # 255096d9 — cluster pending items safe to fan out simultaneously.
        _grp = await db_module.get_parallelizable_groups(
            db, args["project_id"], version=args.get("version")
        )
        # de730a25 — flag undeclared items prominently. They each run in their own
        # sequential group now, but the orchestrator should know parallel safety
        # couldn't be proven for them.
        _und = _grp.get("undeclared_count", 0)
        if _und:
            _grp["warning"] = (
                f"{_und} item(s) lack resource declarations — parallel safety not "
                "guaranteed; each is scheduled in its own sequential group. Add "
                "touches_resources to let them parallelize."
            )
        return _grp
    if name == "assign_sprint_waves":
        # 58a45b92 — persist the parallelizable grouping onto each eligible item's
        # stored `wave` field so parallelism is deterministic + inspectable, then
        # hand-editable via update_sprint_item(wave=...).
        return await db_module.assign_sprint_waves(
            db, args["project_id"], version=args.get("version")
        )
    if name == "analyze_sprint":
        # e77f09d1 — one-call planning brief: parallelism + dependency chains +
        # resource conflicts + stalls synthesized for a planning session.
        return await db_module.analyze_sprint(
            db, args["project_id"], version=args.get("version")
        )
    if name == "claim_sprint_item":
        # ITEM 3 — protect installer scripts: refuse to claim a sprint item whose
        # touches_files includes hooks.ps1 / hooks.sh unless force=true is passed.
        _force = args.get("force") in (True, 1, "true", "1", "yes")
        if not _force:
            _pitem = await db_module.get_sprint_item(db, args["item_id"])
            if _pitem is not None:
                _touched = [p.lower() for p in _parse_touches_files(_pitem.get("touches_files"))]
                _hits = sorted({fn for fn in ("hooks.ps1", "hooks.sh")
                                if any(t == fn or t.endswith("/" + fn) for t in _touched)})
                if _hits:
                    return {
                        "error": "PROTECTED",
                        "message": ("Sprint item touches protected installer scripts "
                                    f"({', '.join(_hits)}). Pass force=true to override."),
                        "protected_files": _hits,
                    }

        # 0716c9e0 — parallel safety: load project settings once for both
        # auto_worktrees (suggest worktree by default) and isolation=worktree.
        _suggest_worktree = False
        _exec_cfg: dict[str, Any] = {}
        _proj_settings_claim: dict[str, Any] = {}
        try:
            _ps = await db_module.get_project_settings(db, args["project_id"])
            _proj_settings_claim = _ps or {}
            _raw_cfg = _proj_settings_claim.get("executor_config")
            if _raw_cfg:
                _exec_cfg = json.loads(_raw_cfg) if isinstance(_raw_cfg, str) else (_raw_cfg or {})
        except Exception:  # noqa: BLE001
            pass
        _isolation = (_exec_cfg or {}).get("isolation", "")
        _aw_raw = _proj_settings_claim.get("auto_worktrees")
        _auto_worktrees = bool(int(_aw_raw) if _aw_raw is not None else 1)
        # Compute file-claim overlaps once. In worktree mode they're a soft
        # informational warning (worktrees isolate the edit); otherwise a hard
        # block. (d01a74bf part 2)
        _file_conflicts = await _sprint_item_file_claim_conflicts(
            db,
            args["project_id"],
            args["item_id"],
            exclude_session_id=args.get("session_id"),
        )
        if _isolation == "worktree" or _auto_worktrees:
            _suggest_worktree = True
        else:
            if _file_conflicts:
                return {
                    "error": "CONFLICT",
                    "message": "Cannot claim sprint item: active session has overlapping claimed files.",
                    "conflicts": _file_conflicts,
                }

        try:
            # 5823db0b — actor attribution: record who claimed the item (explicit
            # actor arg, else the claiming session id).
            _claim_actor = args.get("actor") or args.get("session_id")
            item = await db_module.claim_sprint_item(
                db, args["project_id"], args["item_id"], actor=_claim_actor
            )
        except ValueError:
            # 10c0f6a0 — if already in_progress, check for stale claim and surface info
            _stale_item = await db_module.get_sprint_item(db, args["item_id"])
            if _stale_item and _stale_item.get("status") == "in_progress" and _stale_item.get("claimed_at"):
                from datetime import datetime as _dt_cls
                try:
                    _ca = _dt_cls.fromisoformat(_stale_item["claimed_at"].split(".")[0].replace("Z", ""))
                    _age_h = (_dt_cls.utcnow() - _ca).total_seconds() / 3600
                    if _age_h > 2:
                        return {
                            "error": "STALE_CLAIM",
                            "message": (
                                f"Item is in_progress but claimed {round(_age_h, 1)}h ago with no recent "
                                "activity — the claiming session may have ended. Safe to force-reclaim "
                                "by updating status to 'pending' first via update_sprint_item."
                            ),
                            "stale_age_hours": round(_age_h, 1),
                            "claimed_at": _stale_item["claimed_at"],
                            "item": _stale_item,
                        }
                except Exception:  # noqa: BLE001
                    pass
            # df573218 — claim race: another session grabbed this item between
            # planning and claiming. Instead of crashing the worker with a hard
            # error, point it at the next claimable item so it can keep going.
            _next_item = None
            try:
                _grp = await db_module.get_parallelizable_groups(
                    db, args["project_id"], version=_stale_item.get("version") if _stale_item else None
                )
                for _group in _grp.get("groups", []):
                    for _cand in _group:
                        if _cand.get("id") != args["item_id"]:
                            _next_item = _cand
                            break
                    if _next_item is not None:
                        break
            except Exception:  # noqa: BLE001
                pass
            return {
                "status": "already_claimed",
                "item_id": args["item_id"],
                "current_status": (_stale_item or {}).get("status"),
                "next_available_id": (_next_item or {}).get("id"),
                "next_available_title": (_next_item or {}).get("title"),
                "message": (
                    "Item was already claimed by another session. "
                    + (
                        f"Claim next_available_id ({(_next_item or {}).get('id')}) instead."
                        if _next_item else
                        "No other claimable items remain in this version."
                    )
                ),
            }
        # dec69708 — ENFORCED deferral: claim_sprint_item returns a blocked dict
        # (not a real item) when deferred_until is in the future. Surface it as-is
        # and stop; the item was NOT claimed, so the worktree plumbing below (which
        # assumes a real item row) must be skipped.
        if isinstance(item, dict) and item.get("blocked"):
            return item
        if item is None:
            raise ValueError("sprint item not found")

        if _suggest_worktree:
            item_id_short = item["id"][:8]
            _session_id_claim = args.get("session_id") or ""
            if _auto_worktrees and _isolation != "worktree" and _session_id_claim:
                # Default path: .claude/worktrees/{session_id_short} (gitignored)
                wt_branch = f"worktree/{item_id_short}"
                wt_path = f".claude/worktrees/{_session_id_claim[:8]}"
            else:
                # Legacy isolation=worktree path: repo-relative sibling dir
                repo_path = ""
                _repo_paths = _exec_cfg.get("repo_paths")
                if _repo_paths and isinstance(_repo_paths, list) and _repo_paths:
                    _first = _repo_paths[0]
                    repo_path = (_first.get("cwd") or "") if isinstance(_first, dict) else str(_first)
                if not repo_path:
                    repo_path = _exec_cfg.get("repo_path") or ""
                repo_name = os.path.basename(repo_path.rstrip("/\\")) if repo_path else "repo"
                wt_branch = f"worktree/{item_id_short}"
                wt_path = f"../{repo_name}-worktree-{item_id_short}"
            item = dict(item)
            item.update({
                "worktree_suggested": True,
                "worktree_branch": wt_branch,
                "worktree_path": wt_path,
                "worktree_setup_cmd": f"git worktree add {wt_path} -b {wt_branch}",
                "worktree_cleanup_cmd": f"git worktree remove {wt_path} --force",
                "worktree_merge_cmd": (
                    f"git checkout dev && git merge {wt_branch} --no-edit "
                    f"&& git branch -d {wt_branch}"
                ),
            })

        # Soft, non-blocking overlap warning (e.g. in worktree mode where the
        # hard CONFLICT check is intentionally skipped). (d01a74bf part 2)
        if _file_conflicts:
            item = dict(item)
            _paths = ", ".join(sorted({c["file_path"] for c in _file_conflicts}))
            item["file_overlap_warning"] = {
                "message": (
                    f"Heads up: {_paths} also claimed by another live session. "
                    "Your worktree isolates the edit, but coordinate before merging."
                ),
                "conflicts": _file_conflicts,
            }

        _bc_claim = await _board_change_for_session(
            db, args["project_id"], args.get("session_id")
        )
        if _bc_claim:
            item = dict(item)
            item["board_change"] = _bc_claim

        # f5726fd0 — suggest files to claim based on item title keywords.
        _sug = _suggest_files_for_title(item.get("title") or "")
        if _sug:
            item = dict(item)
            item["suggested_files"] = _sug

        # 04a15d3f — auto-prospect: surface the code-intel targets (files/symbols)
        # the executor should search before editing, derived from touches_resources
        # (or inferred from the title). Best-effort; never blocks the claim.
        try:
            _code_ctx = _prospect_code_context(item)
        except Exception:  # noqa: BLE001
            _code_ctx = None
        if _code_ctx:
            item = dict(item)
            item["code_context"] = _code_ctx

        return item
    if name == "add_subtask":
        return await db_module.add_subtask(
            db, args["project_id"], args["parent_id"], args["title"],
            owner=args.get("owner"),
        )
    if name == "split_sprint_item":
        return await db_module.split_sprint_item(
            db, args["project_id"], args["item_id"], args["titles"]
        )
    if name == "merge_sprint_items":
        return await db_module.merge_sprint_items(
            db, args["project_id"], args["item_ids"], args["new_title"]
        )
    if name == "complete_sprint_item":
        # 0716c9e0 — check active worktree before marking done.
        _complete_session_id = args.get("session_id") or ""
        _merge_warning: dict[str, Any] | None = None
        if _complete_session_id:
            try:
                _ps_complete = await db_module.get_project_settings(db, args["project_id"])
                _req_merge = bool(int((_ps_complete or {}).get("require_merge_approval") or 1))
                if _req_merge:
                    _wt = await db_module.get_active_worktree_for_session(db, _complete_session_id)
                    if _wt:
                        _hitl = await db_module.request_hitl(
                            db, args["project_id"],
                            f"Session has active worktree on branch '{_wt['branch']}' "
                            f"at '{_wt['path']}'. Merge to main before closing. "
                            f"Run: git checkout dev && git merge {_wt['branch']} --no-edit",
                            session_id=_complete_session_id,
                            urgency="normal", kind="correction",
                        )
                        _merge_warning = {
                            "worktree_branch": _wt["branch"],
                            "worktree_path": _wt["path"],
                            "hitl_id": (_hitl or {}).get("id"),
                            "message": "Merge reminder filed — see HITL queue.",
                        }
            except Exception:  # noqa: BLE001
                pass

        # 427b7902 — HARD CI GATE: refuse to complete when GitHub Actions CI for the
        # commit named in the notes is GENUINELY FAILING. This upgrades the b121348e
        # advisory (which only WARNED post-completion) to a real gate, mirroring the
        # EVIDENCE_REQUIRED refusal: computed BEFORE marking done, returns a clean
        # error, and does NOT flip the item to done. Guardrails:
        #   * ONLY a real "failure" state blocks. "unknown" (no repo configured, no
        #     check-runs yet, self-hosted / no-GitHub) and "pending" (CI still
        #     running — the normal push-then-complete race) are ALWAYS allowed
        #     through, so this never blocks on absent/unknown CI.
        #   * Escape hatch consistent with existing force= patterns: pass
        #     override_ci=true to complete anyway (records that the failing CI was
        #     acknowledged). The result is cached so the advisory block below reuses
        #     it without a second GitHub round-trip.
        _ci_pre: dict[str, Any] | None = None
        _ci_checked = False
        _override_ci = bool(args.get("override_ci"))
        try:
            _pre_item = await db_module.get_sprint_item(db, args["item_id"])
        except Exception:  # noqa: BLE001
            _pre_item = None
        if _pre_item is not None and _pre_item.get("project_id") == args["project_id"]:
            _ci_text = f"{args.get('notes') or ''} {(_pre_item.get('notes') or '')}"
            _ci_pre = await _verify_item_ci(db, args["project_id"], tenant, _ci_text)
            _ci_checked = True
            if (
                not _override_ci
                and _ci_pre is not None
                and _ci_pre.get("state") == "failure"
            ):
                return {
                    "error": "CI_FAILING",
                    "item_id": args["item_id"],
                    "ci_verification": _ci_pre,
                    "message": (
                        f"Refusing to complete {args['item_id']}: GitHub Actions CI "
                        f"is FAILING for commit {_ci_pre.get('sha')} "
                        f"({_ci_pre.get('failed')}/{_ci_pre.get('total')} checks failed). "
                        "Fix CI and re-push, or pass override_ci=true to acknowledge "
                        "and complete anyway. (Unknown/pending CI is never blocked — "
                        "only a real failing status.)"
                    ),
                }

        # 5823db0b — quality gate + actor attribution. Pass evidence notes and
        # the completing actor; surface the required_notes gate as a clean error.
        _complete_actor = args.get("actor") or _complete_session_id or None
        try:
            item = await db_module.complete_sprint_item(
                db, args["project_id"], args["item_id"],
                task_id=args.get("task_id"),
                notes=args.get("notes"),
                actor=_complete_actor,
            )
        except db_module.SprintItemEvidenceRequired as exc:
            return {
                "error": "EVIDENCE_REQUIRED",
                "item_id": args["item_id"],
                "message": str(exc),
            }
        if item is None:
            raise ValueError("sprint item not found")
        if _merge_warning:
            item = dict(item)
            item["merge_warning"] = _merge_warning
        # 02cd3992 — unclaimed-file warning: flag files modified without a lock.
        # Non-blocking; surfaces the open-door problem so the executor can act.
        if _complete_session_id:
            try:
                _unclaimed_warnings = await _unclaimed_file_warnings(
                    db, _complete_session_id
                )
                if _unclaimed_warnings:
                    item = dict(item)
                    item["unclaimed_file_warnings"] = _unclaimed_warnings
            except Exception:  # noqa: BLE001
                pass
        # d01a74bf — surface board additions at the item boundary so a planner's
        # mid-run injections get picked up before the next claim.
        _bc_complete = await _board_change_for_session(
            db, args["project_id"], args.get("session_id")
        )
        if _bc_complete:
            item = dict(item)
            item["board_change"] = _bc_complete
        # b121348e / 427b7902 — INDEPENDENT CI verification. The 427b7902 hard gate
        # above already looked this up (and REFUSED completion on a real failing
        # state unless override_ci=true), so reuse that result — no second GitHub
        # round-trip. Attach ``ci_verification`` for transparency, and when CI is
        # genuinely FAILING (i.e. this was an acknowledged override_ci completion),
        # attach a ``ci_warning`` so the closed-on-red item stays visible.
        try:
            _ci = _ci_pre if _ci_checked else await _verify_item_ci(
                db, args["project_id"], tenant,
                f"{args.get('notes') or ''} {item.get('notes') or ''}",
            )
            if _ci is not None:
                item = dict(item)
                item["ci_verification"] = _ci
                if _ci.get("state") == "failure":
                    item["ci_warning"] = (
                        f"⚠ GitHub Actions CI is FAILING for commit {_ci.get('sha')} "
                        f"({_ci.get('failed')}/{_ci.get('total')} checks) — this item "
                        f"was closed on a commit whose CI did not pass"
                        + (" (override_ci=true)." if _override_ci else ".")
                        + " Verify before trusting 'done'."
                    )
        except Exception:  # noqa: BLE001 — advisory only; completion already succeeded
            pass
        # bb29a06f — ADVISORY completion sanity-check. Extends the required_notes
        # gate (evidence EXISTS) with a check that evidence is PLAUSIBLE: when the
        # completion looks weakly-supported (no linked task, no notes anywhere) and
        # no recent commit/migration shares keywords with the title, add a soft
        # nudge. NEVER blocks, never raises — the completion already succeeded. The
        # drift heuristic is noisy, so this is a hint, not a gate.
        try:
            _weakly_supported = not (
                args.get("task_id")
                or (args.get("notes") or "").strip()
                or (item.get("notes") or "").strip()
                or item.get("task_id")
            )
            if _weakly_supported:
                from .. import handoff as _handoff_advisory
                try:
                    _adv_project = await db_module.get_project(db, args["project_id"])
                    _adv_commits = (
                        await _fetch_recent_commits(_adv_project, tenant)
                        if _adv_project else []
                    )
                except Exception:  # noqa: BLE001 — never let commit-fetch break completion
                    _adv_commits = []
                _adv_matches = _handoff_advisory.detect_sprint_item_drift(
                    item.get("title") or "", _adv_commits,
                )
                if not _adv_matches:
                    item = dict(item)
                    item["completion_advisory"] = (
                        "No recent commit or linked evidence appears to reference "
                        "this item — double-check it actually shipped (this is a "
                        "heuristic; ignore if you completed it via docs/config/decision)."
                    )
        except Exception:  # noqa: BLE001 — advisory must never affect completion
            pass
        # Notify only when the sprint is fully complete.
        active_statuses = {"pending", "todo", "in_progress"}
        remaining_items = await db_module.get_sprint_items(db, args["project_id"])
        if not any((it.get("status") or "") in active_statuses for it in remaining_items):
            await _server._maybe_notify(
                db, args["project_id"],
                "Sprint done ✓",
                "All sprint items are complete.",
                event="sprint_done",
                tenant=tenant,
                pref_key="sprint",
            )
        return item
    if name == "add_sprint_item_pointer":
        # 2976e168 — attach a GENERIC POINTER to a sprint item. Validation lives in
        # db.add_sprint_item_pointer (via meridian.pointers.validate_pointer); a
        # malformed pointer raises ValueError, surfaced here as a clean {error}.
        if not args.get("project_id"):
            return {"error": "project_id is required (or pass project_name)"}
        if not args.get("sprint_item_id"):
            return {"error": "sprint_item_id is required"}
        validate_input_size(args.get("label"), "pointer label", 500)
        source_type = args.get("source_type") or ""
        targets = args.get("targets") or []
        if source_type == "web" and isinstance(targets, list):
            # 1d3f6e71 — archive the exact cited passage at CITATION TIME so it
            # survives link-rot / content-drift. Best-effort + guarded: an archiving
            # failure must NEVER block pointer creation — the pointer still stores the
            # live URL + exact quote, and drift is detected on resolve regardless.
            from .. import web_archive  # noqa: PLC0415
            for t in targets:
                if not isinstance(t, dict):
                    continue
                sel = t.get("selector")
                uri = t.get("uri")
                if not (
                    isinstance(sel, dict)
                    and sel.get("type") == "text_quote"
                    and isinstance(uri, str) and uri
                    and not sel.get("archived_url")
                ):
                    continue
                try:
                    res = await web_archive.save_page_now(uri)
                except Exception:  # noqa: BLE001 — belt-and-suspenders
                    res = None
                if isinstance(res, dict) and res.get("archived_url"):
                    sel["archived_url"] = res["archived_url"]
                    if res.get("archived_at"):
                        sel["archived_at"] = res["archived_at"]
                else:
                    # Fallback: the deterministic Wayback "latest snapshot" URL.
                    sel["archived_url"] = web_archive.wayback_latest_url(uri)
        try:
            return await db_module.add_sprint_item_pointer(
                db,
                args["project_id"],
                args["sprint_item_id"],
                source_type,
                targets,
                label=args.get("label"),
            )
        except ValueError as exc:
            return {"error": str(exc)}
    if name == "get_sprint_item_pointers":
        if not args.get("sprint_item_id"):
            return {"error": "sprint_item_id is required"}
        pointers = await db_module.get_sprint_item_pointers(
            db, args["sprint_item_id"]
        )
        return {"sprint_item_id": args["sprint_item_id"], "pointers": pointers}
    if name == "resolve_sprint_item_pointers":
        # 2976e168 — resolve EVERY pointer on an item, dispatching by selector.type.
        # Best-effort + guarded: unresolvable targets become {resolved:false}; the
        # pass NEVER raises. node_id targets need the doc-structure store, resolved
        # via the same tier-aware helper the citation tools use; symbol/zotero use
        # the pointers module's default seams (db.search_graph_entities /
        # zotero_client). project_id scopes the code-graph search.
        if not args.get("project_id"):
            return {"error": "project_id is required (or pass project_name)"}
        if not args.get("sprint_item_id"):
            return {"error": "sprint_item_id is required"}
        from ..pointers import resolve_pointer  # noqa: PLC0415

        # Resolve the doc-structure store once for node_id lookups (best-effort;
        # None → node_id targets degrade to {resolved:false}).
        _ptr_store = await _resolve_ingest_doc_store(db, data_dir, tenant)

        async def _node_resolver(element_id: str) -> Any:
            if _ptr_store is None:
                return None
            try:
                return await _ptr_store.get_element_by_id(element_id)
            except Exception:  # noqa: BLE001 — resolver seam must never raise
                return None

        pointers = await db_module.get_sprint_item_pointers(
            db, args["sprint_item_id"]
        )
        resolved: list[dict[str, Any]] = []
        for ptr in pointers:
            resolved.append(
                await resolve_pointer(
                    db, ptr,
                    project_id=args["project_id"],
                    node_resolver=_node_resolver,
                )
            )
        return {"sprint_item_id": args["sprint_item_id"], "pointers": resolved}
    if name == "delete_sprint_item_pointer":
        # 98c71a42 — the DELETE (edit-via-replace) half of the pointer CRUD. The DB
        # layer (db.delete_sprint_item_pointer) has existed since 2976e168, but no
        # MCP tool wrapped it — a pointer could be created / listed / resolved yet
        # never removed. Idempotent: {deleted:false} when no pointer had that id,
        # rather than an error.
        if not args.get("pointer_id"):
            return {"error": "pointer_id is required"}
        removed = await db_module.delete_sprint_item_pointer(db, args["pointer_id"])
        return {"pointer_id": args["pointer_id"], "deleted": removed}
    return _MISS


async def _handle_file_claims(
    name: str,
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """Dispatch group: claim_file, get_file_claims, get_symbol_claims, get_symbol_hotspots, release_file, get_graph_diff, snapshot_graph_metrics."""
    if name == "claim_file":
        # 4bac57ff — symbol-level claim when both `symbol` and `content` are
        # supplied; otherwise the coarse whole-file lock. Falls back to a
        # whole-file lock when the content can't be parsed into symbols.
        _symbol = args.get("symbol")
        _content = args.get("content")
        if _symbol and _content is not None:
            result = await db_module.claim_symbol(
                db, args["session_id"], args["file_path"], _symbol, _content
            )
            if result.get("reason") == "unparseable":
                # 771c00d7 — still surface code-anchored notes (symbol-scoped) on
                # the whole-file fallback so the executor never misses a warning.
                return await db_module.claim_file(
                    db, args["file_path"], args["session_id"], symbol=_symbol
                )
            # 771c00d7 — attach symbol-scoped code notes to the symbol claim too,
            # resolving the project from the session (same as claim_file does).
            result["code_notes"] = await db_module._code_notes_for_session_file(
                db, args["session_id"], args["file_path"], _symbol,
            )
            return result
        # ffa03655 — read|write claim grain (default write/exclusive).
        return await db_module.claim_file(
            db, args["file_path"], args["session_id"], symbol=_symbol,
            mode=args.get("mode", "write"),
        )
    if name == "get_file_claims":
        return await db_module.get_file_claims(
            db, args["file_path"], args.get("project_id"), args.get("symbol")
        )
    if name == "store_finding":
        validate_input_size(args.get("content"), "finding content", 1_000_000)
        if not (args.get("content") or "").strip():
            return {"error": "store_finding requires non-empty content"}
        return await db_module.store_finding(
            db, args["project_id"], args["content"],
            session_id=args.get("session_id"), key=args.get("key"),
            title=args.get("title"), task_id=args.get("task_id"),
        )
    if name == "get_findings":
        return await db_module.get_findings(
            db, args["project_id"], key=args.get("key"),
            session_id=args.get("session_id"), limit=int(args.get("limit", 50)),
        )
    if name == "send_message":
        validate_input_size(args.get("payload"), "message payload", 1_000_000)
        return await db_module.send_message(
            db, args["project_id"], args["to_session_id"], args.get("payload", ""),
            from_session_id=args.get("from_session_id") or args.get("session_id"),
            kind=args.get("kind"),
        )
    if name == "receive_messages":
        return await db_module.receive_messages(
            db, args["session_id"],
            mark_read=args.get("mark_read", True),
            limit=int(args.get("limit", 50)),
        )
    if name == "idle_until_all_done":
        _sids = args.get("session_ids") or []
        if isinstance(_sids, str):
            _sids = [s.strip() for s in _sids.split(",") if s.strip()]
        return await db_module.idle_until_all_done(db, _sids)
    if name == "get_symbol_claims":
        return {"claims": await db_module.get_symbol_claims(db, args["file_path"])}
    if name == "get_symbol_hotspots":
        _min_s = int(args.get("min_sessions") or 3)
        _days = int(args.get("days") or 14)
        hotspots = await db_module.get_symbol_hotspots(
            db, args.get("file_path"),
            min_sessions=_min_s,
            days=_days,
        )
        # 1b4760a9 — file-level suggestions when any hotspot exceeds threshold.
        suggestions: list[dict] = []
        if any(h.get("session_count", 0) > 5 for h in hotspots):
            suggestions = await db_module.get_hotspot_suggestions(
                db, min_sessions=_min_s, days=_days
            )
        return {"hotspots": hotspots, "suggestions": suggestions}
    if name == "get_graph_diff":
        # f773a99a — compare graph snapshots between two sessions.
        sa = (args.get("session_a") or "").strip()
        sb = (args.get("session_b") or "").strip()
        if not sa or not sb:
            return {"error": "session_a and session_b are required"}
        return await db_module.get_graph_diff(db, sa, sb)
    if name == "snapshot_graph_metrics":
        # f773a99a — take a graph snapshot for the calling session.
        sid = (args.get("session_id") or "").strip()
        pid = (args.get("project_id") or "").strip()
        if not sid:
            return {"error": "session_id is required"}
        # Fall back to the session's own project_id if not explicitly provided.
        if not pid:
            async with db.execute(
                "SELECT project_id FROM sessions WHERE id = ?", (sid,)
            ) as _sc:
                _sr = await _sc.fetchone()
            if _sr:
                pid = (_sr["project_id"] if hasattr(_sr, "keys") else _sr[0]) or ""
        if not pid:
            return {"error": "project_id is required (or pass project_name)"}
        return await db_module.snapshot_graph_metrics(db, sid, pid)
    if name == "release_file":
        released = await db_module.release_file(db, args["file_path"], args["session_id"])
        return {"released": released, "file_path": args["file_path"]}
    return _MISS


# 0fba4cb6 — MECHANICAL model-efficiency classifier.
#
# A zero-token, pure/deterministic heuristic that suggests the cheapest model
# tier likely sufficient for a task, from signals already available on a sprint
# item / task descriptor (title keywords, file count, touched-resource shape,
# explicit size). It mirrors how the ultracode orchestration script spends ZERO
# model tokens on routing — no LLM call, no DB, no network. A future "semantic"
# mode (an LLM-backed second opinion) is deliberately OUT OF SCOPE here; see the
# follow-up note in the tool schema.
#
# Design notes:
#   * Purely additive scoring: cheap-leaning signals push the score down toward
#     "haiku", expensive-leaning signals push it up toward "opus". The final tier
#     is a threshold cut on the accumulated score, so it is stable and testable.
#   * Every signal that fires is reported (name + human-readable detail + the
#     delta it contributed) so the rationale is fully auditable — nothing about
#     the suggestion is a black box.
#   * The classifier NEVER raises on bad input: missing/oddly-typed fields simply
#     contribute no signal. It is safe to call on a raw, partially-filled
#     descriptor.

# Keyword -> (score_delta, tier-lean label). Positive = more expensive.
# Matched as whole words against a lowercased title/description token set.
_TIER_CHEAP_KEYWORDS: dict[str, str] = {
    "typo": "trivial-text-fix",
    "typos": "trivial-text-fix",
    "rename": "mechanical-rename",
    "comment": "comment-only",
    "docstring": "docs-only",
    "readme": "docs-only",
    "wording": "copy-edit",
    "copy": "copy-edit",
    "lint": "lint-cleanup",
    "format": "formatting",
    "formatting": "formatting",
    "whitespace": "formatting",
    "bump": "version-bump",
    "changelog": "docs-only",
}
_TIER_EXPENSIVE_KEYWORDS: dict[str, str] = {
    "refactor": "structural-refactor",
    "refactoring": "structural-refactor",
    "migration": "schema-migration",
    "migrate": "schema-migration",
    "architecture": "architectural",
    "architect": "architectural",
    "redesign": "architectural",
    "rewrite": "large-rewrite",
    "concurrency": "concurrency-hazard",
    "async": "concurrency-hazard",
    "security": "security-sensitive",
    "auth": "auth-sensitive",
    "authentication": "auth-sensitive",
    "cryptography": "security-sensitive",
    "crypto": "security-sensitive",
    "algorithm": "algorithmic",
    "optimize": "performance-tuning",
    "performance": "performance-tuning",
    "distributed": "distributed-systems",
}


def _classify_task_tier(descriptor: dict[str, Any]) -> dict[str, Any]:
    """Deterministically suggest a model tier for a task/sprint-item descriptor.

    Pure function — NO model call, NO DB, NO network. Given a descriptor with any
    of the optional keys below, returns a dict:
        {"tier": "haiku"|"sonnet"|"opus",
         "score": <int>,
         "signals": [{"signal", "detail", "weight"}...],
         "rationale": "<one-line human summary>",
         "mode": "mechanical"}

    Recognised descriptor keys (all optional):
        title / description  — text; scanned for cheap/expensive keyword signals.
        file_count           — int; more files touched -> more expensive.
        files                — list; used to derive file_count when absent.
        touches_resources    — list (or int count); shape/count of touched
                               resources (DB/schema/infra), an expensive signal.
        size                 — one of "xs"|"s"|"m"|"l"|"xl" (case-insensitive),
                               an explicit sprint-item size estimate.

    The score starts at a neutral 0. Cheap signals subtract, expensive signals
    add; the tier is a threshold cut on the total, so the mapping is stable.
    """
    if not isinstance(descriptor, dict):
        descriptor = {}
    signals: list[dict[str, Any]] = []
    score = 0

    def _add(signal: str, detail: str, weight: int) -> None:
        nonlocal score
        score += weight
        signals.append({"signal": signal, "detail": detail, "weight": weight})

    # --- Text keyword signals (title + description) ---------------------------
    text_parts = []
    for key in ("title", "description"):
        val = descriptor.get(key)
        if isinstance(val, str):
            text_parts.append(val)
    text = " ".join(text_parts).lower()
    tokens = set(re.findall(r"[a-z0-9]+", text))
    for kw, label in _TIER_CHEAP_KEYWORDS.items():
        if kw in tokens:
            _add("keyword:" + kw, f"cheap-leaning keyword '{kw}' ({label})", -2)
    for kw, label in _TIER_EXPENSIVE_KEYWORDS.items():
        if kw in tokens:
            _add("keyword:" + kw, f"expensive-leaning keyword '{kw}' ({label})", 3)

    # --- File count -----------------------------------------------------------
    file_count = descriptor.get("file_count")
    if not isinstance(file_count, int) or isinstance(file_count, bool):
        files = descriptor.get("files")
        file_count = len(files) if isinstance(files, (list, tuple)) else None
    if isinstance(file_count, int) and not isinstance(file_count, bool):
        if file_count <= 1:
            _add("file_count", f"{file_count} file(s) touched — narrow blast radius", -1)
        elif file_count <= 3:
            _add("file_count", f"{file_count} files touched — small change", 0)
        elif file_count <= 8:
            _add("file_count", f"{file_count} files touched — multi-file change", 3)
        else:
            _add("file_count", f"{file_count} files touched — broad, cross-cutting change", 5)

    # --- Touched resources (DB/schema/infra shape + count) --------------------
    touched = descriptor.get("touches_resources")
    touched_count: int | None = None
    if isinstance(touched, (list, tuple)):
        touched_count = len(touched)
    elif isinstance(touched, int) and not isinstance(touched, bool):
        touched_count = touched
    if isinstance(touched_count, int):
        if touched_count == 0:
            _add("touches_resources", "no external resources touched", -1)
        elif touched_count <= 2:
            _add("touches_resources", f"{touched_count} resource(s) touched", 2)
        else:
            _add("touches_resources",
                 f"{touched_count} resources touched — wide integration surface", 4)

    # --- Explicit sprint-item size -------------------------------------------
    size = descriptor.get("size")
    if isinstance(size, str):
        size_weights = {"xs": -2, "s": -1, "m": 0, "l": 3, "xl": 5}
        w = size_weights.get(size.strip().lower())
        if w is not None:
            _add("size", f"declared size '{size.strip().lower()}'", w)

    # --- Threshold cut -> tier ------------------------------------------------
    if score <= -2:
        tier = "haiku"
    elif score >= 5:
        tier = "opus"
    else:
        tier = "sonnet"

    tier_reason = {
        "haiku": "mechanical / low-complexity work — a cheap tier is sufficient",
        "sonnet": "moderate complexity — a mid tier is a safe default",
        "opus": "high complexity / broad blast radius — reserve the top tier",
    }[tier]
    rationale = (
        f"Suggested tier '{tier}' (score {score:+d}): {tier_reason}. "
        + (f"{len(signals)} signal(s) fired." if signals else
           "No strong signals; defaulting on a neutral score.")
    )
    return {
        "tier": tier,
        "score": score,
        "signals": signals,
        "rationale": rationale,
        "mode": "mechanical",
    }


async def _handle_planning_tools(
    name: str,
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """Dispatch group: reconcile_sprint_drift, get_planning_brief, analyze_model_efficiency."""
    if name == "analyze_model_efficiency":
        # 0fba4cb6 — MECHANICAL (zero-token) model-tier suggestion. Pure
        # heuristic over signals already on the descriptor; no model call, no DB,
        # no network. See _classify_task_tier for the scoring model.
        return _classify_task_tier(args)
    if name == "reconcile_sprint_drift":
        from .. import handoff as handoff_module_local
        project = await db_module.get_project(db, args["project_id"])
        if project is None:
            raise ValueError("project not found")
        pending_items = await db_module.get_sprint_items(db, args["project_id"], status="pending")
        commits = await _fetch_recent_commits(project, tenant)
        matches = handoff_module_local.reconcile_sprint_items(pending_items, commits)
        action_items = [
            {
                "item_id": m["item_id"],
                "title": m["title"],
                "confidence": m["confidence"],
                "matching_commits": m["matching_commits"],
                "suggested_action": (
                    f"complete_sprint_item(project_id='{args['project_id']}', item_id='{m['item_id']}')"
                    if m["confidence"] == "high"
                    else "verify manually before marking done"
                ),
            }
            for m in matches
        ]
        return {
            "pending_item_count": len(pending_items),
            "commit_count": len(commits),
            "drift_count": len(matches),
            "high_confidence": sum(1 for m in matches if m["confidence"] == "high"),
            "medium_confidence": sum(1 for m in matches if m["confidence"] == "medium"),
            "matches": action_items,
        }
    if name == "get_planning_brief":
        project_id = args["project_id"]
        project = await db_module.get_project(db, project_id)
        if project is None:
            raise ValueError("project not found")
        goal = await db_module.get_goal(db, project_id)
        pending_items = await db_module.get_sprint_items(db, project_id, status="pending")
        in_progress = await db_module.get_sprint_items(db, project_id, status="in_progress")
        recent_tasks = await db_module.get_tasks(db, project_id, limit=5)
        sessions = await db_module.get_sessions(db, project_id, active_only=True)
        hitls = await db_module.list_hitl_requests(db, project_id, status="pending")
        decisions_raw = (project.get("decisions") or "").strip()
        recent_decisions = [ln.strip() for ln in decisions_raw.splitlines() if ln.strip()][-3:]
        # 2b39549d — surface decisions resting on assumptions that aren't yet
        # confirmed (unvalidated or invalidated) so the planner always sees what
        # needs validating / what just broke.
        pinned = await db_module.get_pinned_decisions(db, project_id)
        unvalidated_assumptions = [
            {
                "decision_id": d.get("id"),
                "title": (d.get("title") or "")[:120],
                "assumption": (d.get("assumption") or "")[:200],
                "assumption_status": d.get("assumption_status"),
            }
            for d in pinned
            if d.get("assumption") and d.get("assumption_status") != "confirmed"
        ]
        # 0b711a9d — permanent strategic insights ALWAYS surface in the planning
        # brief (durable understanding that shapes future decisions). Guarded so a
        # pre-migration DB never breaks the brief; never truncated by a list cap.
        permanent_insights: list[dict[str, Any]] = []
        try:
            for _ins in await db_module.get_insights(db, project_id, horizon="permanent"):
                permanent_insights.append({
                    "id": _ins.get("id"),
                    "title": (_ins.get("title") or "")[:120],
                    "body": (_ins.get("body") or "")[:400],
                })
        except Exception:  # noqa: BLE001
            permanent_insights = []
        # 81170c84 — "what did the last session do": surface the most recent
        # session's completed items + task log + recent decisions so a planner
        # sees executor output without manual copy-paste.
        last_session = await db_module.get_last_session_brief(db, project_id)
        # aef94e4a — surface the latest auto-generated sprint retrospective so a
        # planner sees the strategic through-line (what shipped / patterns /
        # direction) without opening the note.
        _retro_notes = await db_module.get_project_notes(
            db, project_id, tag="retrospective", bodies=True, limit=1
        )
        latest_retrospective = None
        if _retro_notes:
            _r = _retro_notes[0]
            latest_retrospective = {
                "title": _r.get("title"),
                "body": (_r.get("body") or "")[:800],
                "updated_at": _r.get("updated_at") or _r.get("created_at"),
                "slug": _r.get("slug"),
            }
        # ab514e43 — "new handoff available" signal. ``generated_at`` lets the
        # planner pass it back as ``since`` next call; a handoff filed after
        # ``since`` (or any handoff when ``since`` is omitted) flags as new.
        from datetime import datetime, timezone  # local: avoid top-level churn
        brief_generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        since = args.get("since")
        latest_h = await db_module.get_latest_handoff(db, project_id)
        latest_handoff = None
        new_handoff_available = False
        handoff_signal = None
        if latest_h:
            _sessions_all = await db_module.get_sessions(
                db, project_id, active_only=False
            )
            _name_by_id = {s.get("id"): s.get("name") for s in _sessions_all}
            _hc = str(latest_h.get("created_at") or "")
            _sname = _name_by_id.get(latest_h.get("session_id"))
            latest_handoff = {
                "id": latest_h.get("id"),
                "session_id": latest_h.get("session_id"),
                "session_name": _sname,
                "mode": latest_h.get("mode"),
                "created_at": _hc,
                "body_preview": (latest_h.get("body") or "")[:600],
            }
            new_handoff_available = (since is None) or (_hc > str(since))
            if new_handoff_available:
                handoff_signal = (
                    f"New handoff available: session "
                    f"'{_sname or latest_h.get('session_id')}' filed a "
                    f"{latest_h.get('mode')} handoff at {_hc} — read it via "
                    f"get_session_brief(project_id, session_id="
                    f"'{latest_h.get('session_id')}')."
                )
        return {
            "project_id": project_id,
            "project_name": project.get("name"),
            "sprint": (goal.get("sprint") or "") if goal else "",
            "north_star": (goal.get("north_star") or "") if goal else "",
            "pending_count": len(pending_items),
            "pending_items": [
                {"id": it["id"], "title": (it.get("title") or "")[:120], "version": it.get("version")}
                for it in pending_items[:10]
            ],
            "in_progress": [
                {"id": it["id"], "title": (it.get("title") or "")[:80]}
                for it in in_progress
            ],
            "recent_tasks": [
                {"description": (t.get("description") or "")[:80], "status": t.get("status")}
                for t in recent_tasks
            ],
            "active_sessions": [
                {"name": s.get("name"), "human_id": s.get("human_id")}
                for s in sessions
            ],
            "recent_decisions": recent_decisions,
            "unvalidated_assumptions": unvalidated_assumptions,
            "permanent_insights": permanent_insights,
            "last_session": last_session,
            "latest_retrospective": latest_retrospective,
            "generated_at": brief_generated_at,
            # de193a81 — explicit "now" so a planner spanning multiple calendar
            # days never guesses at "today"/"yesterday".
            "current_timestamp": brief_generated_at,
            "latest_handoff": latest_handoff,
            "new_handoff_available": new_handoff_available,
            "handoff_signal": handoff_signal,
            "pending_hitls": [
                {
                    "id": h.get("id"),
                    "question": (h.get("question") or "")[:120],
                    "urgency": h.get("urgency"),
                }
                for h in (hitls if isinstance(hitls, list) else [])[:5]
            ],
        }
    if name == "refresh_context":
        # d8bd59c4 — single-call post-compaction recovery for planning chats.
        # COMPACT by design: counts + ids + slugs, not full bodies, so it can be
        # called the moment a chat feels disoriented without overflowing context.
        # bf51b12e — the body now lives in the module-level _build_context_refresh
        # builder so both the explicit tool and the dispatch hook share one path.
        project_id = args["project_id"]
        result = await _build_context_refresh(db, project_id)
        if result is None:
            raise ValueError("project not found")
        return result
    return _MISS


async def _handle_plugin_tools(
    name: str,
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """Dispatch group: list_plugins, get_plugin_details.

    list_plugins returns a lightweight index (~500 tokens) so an executor can
    see what tunnel plugins are active without loading 50k+ tokens of schemas.
    get_plugin_details fetches the full schema for one named plugin on demand.
    Both surface stored skill-guide notes (kind='plugin') when present.

    These are non-fatal if no tunnel is active (returns empty list/error).
    """
    if name == "refresh_tool_manifest":
        # 142808f3 — plain tool CALL to re-discover the built-in tool set, for
        # clients that ignore notifications/tools/list_changed. Best-effort re-fire
        # of the signal too, so a client that DOES honour it also re-lists.
        from ..mcp_tools import _MCP_TOOLS_LIST  # noqa: PLC0415
        from ..tool_manifest import build_tool_manifest  # noqa: PLC0415

        manifest = build_tool_manifest(_MCP_TOOLS_LIST)
        _tid = tenant.get("id") if tenant else None
        if _tid:
            try:
                from ..routes.tunnel import (  # noqa: PLC0415
                    notify_tools_list_changed as _notify_tlc,
                )
                _notify_tlc(_tid)
                manifest["list_changed_refired"] = True
            except Exception:  # noqa: BLE001 — signal is a bonus, manifest is the point
                manifest["list_changed_refired"] = False
        return manifest
    if name == "list_plugins":
        from ..tunnel_plugins import BUILTIN_PLUGINS  # noqa: PLC0415
        from ..routes import tunnel as _tunnel_mod  # noqa: PLC0415
        _slot_display = _tunnel_mod.SLOT_DISPLAY_NAMES

        builtin_by_slot = {p["slot"]: p for p in BUILTIN_PLUGINS}

        # Fetch tunnel tool counts + live tool names per slot (parallel, non-fatal).
        #
        # 90d04961 — this is re-queried on EVERY call (never a session-start
        # snapshot): _fetch_slot_tools hits each slot's live tools/list, so a slot
        # that only became active MID-session (e.g. the word slot connecting after
        # session start and now serving 42 real tools) is picked up here on the
        # next list_plugins/tool-search call rather than being invisible until a
        # reconnect. A slot is treated as active/invocable ONLY when it actually
        # returns ≥1 live tool this fetch — a slot that returns 0 (never connected,
        # dead inner server, or still starting) must NOT be flagged active, or a
        # tool-search consumer would surface a plugin whose tools 503 on first call.
        slot_tool_counts: dict[str, int] = {}
        slot_tool_names: dict[str, list[str]] = {}
        tenant_id = tenant.get("id") if tenant else None
        if tenant_id and _tunnel_mod.has_active_tunnel(tenant_id):
            try:
                slot_results = await asyncio.gather(
                    *[
                        _tunnel_mod._fetch_slot_tools(tenant_id, label)  # type: ignore[attr-defined]
                        for label in _tunnel_mod._TUNNEL_LABELS  # type: ignore[attr-defined]
                    ]
                )
                for label, tools in slot_results:
                    # Only record slots that surfaced live tools — a bare count of
                    # 0 means the slot is not actually serving, so it stays out of
                    # the active/invocable set (and off the tool-search index).
                    if tools:
                        slot_tool_counts[label] = len(tools)
                        slot_tool_names[label] = [
                            str(t.get("name"))
                            for t in tools
                            if isinstance(t, dict) and t.get("name")
                        ]
            except Exception:  # noqa: BLE001
                pass

        # Fetch stored skill notes (tagged 'plugin-skill') from workspace notes
        skill_notes: dict[str, dict] = {}
        try:
            all_notes = await db_module.get_workspace_notes(
                db,
                tag="plugin-skill",
                tenant_id=_mcp_tenant_id,
            )
            for n in (all_notes or []):
                title = (n.get("title") or "").lower().strip()
                tags = n.get("tags") or ""
                # Derive the plugin key from the title or tags
                # e.g. title "skill/filesystem" or tags "plugin-skill,filesystem"
                for part in [title] + [t.strip() for t in tags.split(",")]:
                    if part.startswith("skill/"):
                        key = part[6:]
                        skill_notes[key] = n
                        break
                    if part not in ("plugin-skill", ""):
                        skill_notes[part] = n
        except Exception:  # noqa: BLE001
            pass

        result_plugins = []
        for plugin in BUILTIN_PLUGINS:
            slot = plugin["slot"]
            display_name = _slot_display.get(slot, slot)
            tool_count = slot_tool_counts.get(slot, 0)
            skill_key = plugin.get("name", "")
            skill = skill_notes.get(skill_key) or skill_notes.get(display_name)
            entry: dict[str, Any] = {
                "name": plugin["name"],
                "slot": slot,
                "enabled": plugin.get("enabled", False),
                "description": plugin.get("description", ""),
                "tool_count": tool_count,
                "active": slot in slot_tool_counts,
                # 8f66d85e — plugin tools are connector-proxied; they are only
                # invocable while the tunnel is live (otherwise they appear in the
                # list but calling one returns "unknown tool").
                "invocable": slot in slot_tool_counts,
                # 90d04961 — surface the slot's LIVE tool names (slot-prefixed, as
                # the connector advertises them) so a tool-search consumer can match
                # a specific tunnel-bridged tool that only appeared after the initial
                # session snapshot. Empty for inactive slots.
                "tools": [
                    f"{display_name}__{tn}"
                    for tn in slot_tool_names.get(slot, [])
                ],
            }
            if skill:
                entry["skill_note"] = {
                    "title": skill.get("title", ""),
                    "slug": skill.get("slug", ""),
                    "body_preview": (skill.get("body") or "")[:200],
                }
            result_plugins.append(entry)

        return {
            "plugins": result_plugins,
            "tunnel_active": bool(tenant_id and _tunnel_mod.has_active_tunnel(tenant_id)),
            "hint": "Call get_plugin_details(name=<plugin_name>) for full tool schema.",
            # 8f66d85e — clarify HOW plugin tools are called so agents stop trying
            # to invoke them as native Meridian tools.
            "invocation_note": (
                "Plugin tools are invoked through the tunnel connector (claude.ai) "
                "using slot-prefixed names like 'filesystem__read_file' — not as "
                "native Meridian MCP tools. Each is callable only while the tunnel "
                "is connected (see each plugin's 'invocable' flag)."
            ),
        }

    if name == "get_plugin_details":
        from ..tunnel_plugins import BUILTIN_PLUGINS  # noqa: PLC0415
        from ..routes import tunnel as _tunnel_mod  # noqa: PLC0415
        _slot_display = _tunnel_mod.SLOT_DISPLAY_NAMES

        plugin_name = (args.get("name") or "").strip()
        if not plugin_name:
            return {"error": "name is required"}

        # Find the builtin plugin
        target = next(
            (p for p in BUILTIN_PLUGINS if p["name"] == plugin_name),
            None,
        )
        if target is None:
            return {"error": f"unknown plugin '{plugin_name}'. Call list_plugins to see available plugins."}

        slot = target["slot"]
        tenant_id = tenant.get("id") if tenant else None

        # Fetch full tools list for this slot
        tools: list[dict] = []
        if tenant_id and _tunnel_mod.has_active_tunnel(tenant_id):
            try:
                _, tools = await _tunnel_mod._fetch_slot_tools(tenant_id, slot)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass

        # Fetch skill note if present (workspace-level notes tagged 'plugin-skill')
        skill_note: dict | None = None
        try:
            all_ws_notes = await db_module.get_workspace_notes(
                db,
                tag="plugin-skill",
                tenant_id=_mcp_tenant_id,
            )
            display_name = _slot_display.get(slot, slot)
            for n in (all_ws_notes or []):
                title = (n.get("title") or "").lower().strip()
                tags_str = n.get("tags") or ""
                tag_parts = [t.strip() for t in tags_str.split(",")]
                if (plugin_name in tag_parts or display_name in tag_parts
                        or title in (f"skill/{plugin_name}", f"skill/{display_name}",
                                     plugin_name, display_name)):
                    skill_note = n
                    break
        except Exception:  # noqa: BLE001
            pass

        result: dict[str, Any] = {
            "name": target["name"],
            "slot": slot,
            "enabled": target.get("enabled", False),
            "description": target.get("description", ""),
            "tools": tools,
            "tool_count": len(tools),
        }
        if skill_note:
            result["skill_guide"] = {
                "title": skill_note.get("title", ""),
                "body": skill_note.get("body") or "",
                "slug": skill_note.get("slug", ""),
            }
        return result

    return _MISS


async def _handle_tunnel_tools(
    name: str,
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """Dispatch group: set_active_repo (tunnel control), run_verification (0e973e52)."""
    if name == "set_active_repo":
        repo_path = str(args.get("repo_path") or "").strip()
        if not repo_path:
            raise ValueError("repo_path is required")
        if tenant is None:
            raise ValueError("set_active_repo requires an authenticated tenant (tunnel mode)")
        tenant_id = tenant.get("id", "")
        from ..routes import tunnel as _tunnel_mod  # noqa: PLC0415
        result = await _tunnel_mod.send_active_repo_control(tenant_id, repo_path)
        if result.get("status") == "not_connected":
            raise ValueError(
                "tunnel not connected — run `meridian --tunnel` in your terminal "
                "to start the tunnel, then retry"
            )
        if result.get("status") == "error":
            raise ValueError(
                f"tunnel error while switching repo: {result.get('message', 'unknown error')}"
            )
        # Also expand the FS connector's allowed roots so filesystem tools can
        # access the new repo path without requiring --repo to be set at startup.
        await _tunnel_mod.send_add_fs_roots_control(tenant_id, [repo_path])
        return result

    if name == "run_verification":
        # 0e973e52 — run the project's stored test_cmd on the caller's local
        # machine via the FS tunnel and return a structured real result.
        # ARCHITECTURAL REQUIREMENT (decision 0dedff91): this MUST run on the
        # caller's machine via the tunnel — the hosted server can never reach a
        # caller's test suite. This is the exact same class of mistake that
        # search_code_semantic / ingest_document made and were fixed for.
        project_id = args.get("project_id", "")
        if not project_id:
            raise ValueError("project_id is required")

        # Load the project's stored executor config to get test_cmd and cwd.
        exec_cfg = await db_module.get_executor_config(db, project_id) or {}
        test_cmd = (exec_cfg.get("test_cmd") or "").strip()
        repo_path = (exec_cfg.get("repo_path") or "").strip()

        if not test_cmd:
            # Clean, honest "not configured" — never an error or fabricated pass.
            return {
                "status": "not_configured",
                "message": (
                    "No test_cmd is configured for this project. "
                    "Call set_executor_config(project_id=..., test_cmd='...') to set one."
                ),
                "project_id": project_id,
                "exit_code": None,
                "passed": None,
                "failed": None,
                "stdout_tail": "",
                "stderr_tail": "",
            }

        if tenant is None:
            raise ValueError("run_verification requires an authenticated tenant (tunnel mode)")
        tenant_id = tenant.get("id", "")

        from ..routes import tunnel as _tunnel_mod  # noqa: PLC0415

        # Hosted guard: the hosted server can never spawn a process on the caller's
        # machine directly. We ALWAYS require the tunnel — same class of fix as
        # ingest_document / search_code_semantic / search_outputs (decision 0dedff91).
        if _hosted_mode() and not _tunnel_mod.has_active_tunnel(tenant_id):
            return {
                "status": "not_connected",
                "message": (
                    "run_verification must run on YOUR local machine via the tunnel "
                    "(hosted Meridian has no access to your machine or its test suite). "
                    "Start `meridian --tunnel` in your terminal to enable it."
                ),
                "project_id": project_id,
                "test_cmd": test_cmd,
                "exit_code": None,
                "passed": None,
                "failed": None,
                "stdout_tail": "",
                "stderr_tail": "",
                "hosted": True,
            }

        result = await _tunnel_mod.send_run_cmd_control(
            tenant_id,
            cmd=test_cmd,
            cwd=repo_path or None,
        )

        # Enrich the result with project context so the caller has everything.
        result["project_id"] = project_id
        result["test_cmd"] = test_cmd
        if repo_path:
            result["cwd"] = repo_path
        return result

    return _MISS


async def _handle_outputs_tools(
    name: str,
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """Dispatch group: search_outputs + annotate_outputs (9e02e448 / a0e9133e)."""
    if name == "annotate_outputs":
        from .. import outputs_indexer as _outputs_indexer  # noqa: PLC0415
        outputs_dir = str(args.get("outputs_dir") or "").strip()
        path = str(args.get("path") or "").strip()
        note = str(args.get("note") or "").strip()
        run_params = args.get("run_params")
        if not outputs_dir:
            raise ValueError("outputs_dir is required")
        if not path:
            raise ValueError("path is required")
        if not note:
            raise ValueError("note is required")
        # 9e02e448 — local-only annotation, same guard as search_outputs:
        # hosted Meridian can't reach the caller's machine.
        if _hosted_mode():
            return {
                "error": (
                    "annotate_outputs stores annotations in a local DuckDB index "
                    "on YOUR filesystem and cannot run on hosted Meridian. "
                    "Run Meridian self-hosted."
                ),
                "hosted": True,
            }
        return _outputs_indexer.annotate_outputs(
            outputs_dir, path, note, run_params=run_params,
        )
    if name == "search_outputs":
        from .. import outputs_indexer as _outputs_indexer  # noqa: PLC0415
        from .. import hardening as _hardening  # noqa: PLC0415
        outputs_dir = str(args.get("outputs_dir") or "").strip()
        query = str(args.get("query") or "").strip()
        if not outputs_dir:
            raise ValueError("outputs_dir is required")
        if not query:
            raise ValueError("query is required")
        # Workspace decision 0dedff91 — outputs_indexer.search_outputs walks
        # `outputs_dir` off THIS process's own filesystem (os.walk + hash +
        # DuckDB FTS). On hosted Meridian that's the server, which can never
        # reach a caller's own machine, so os.walk finds nothing (or silently
        # mis-resolves a Windows path against the server cwd). Fail honestly
        # instead — same guard as ingest_document / get_document_structure /
        # search_code_semantic.
        if _hosted_mode():
            return {
                "outputs_dir": outputs_dir,
                "query": query,
                "hits": [],
                "total_indexed": 0,
                "error": (
                    "search_outputs walks a directory on YOUR local filesystem "
                    "and cannot run on hosted Meridian -- the server has no "
                    "access to your machine. Run Meridian self-hosted, or use "
                    "the tunnel-routed local file/search tools, which proxy to "
                    "your machine."
                ),
                "hosted": True,
            }
        limit = args.get("limit", 10)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 10
        include_archival = args.get("include_archival", True)
        # The walk + hash + DuckDB FTS build is synchronous/CPU-bound. Run it in
        # the bulkhead pool under a hard deadline (e5f96adf / 1d021501): it now
        # also has an internal incremental budget (5116078b), so this backstop
        # only fires on a genuinely pathological cold tree — and fails fast with
        # a clear error instead of the ~4-minute silent hang that motivated this.
        try:
            return await _hardening.run_in_bulkhead(
                _outputs_indexer.search_outputs,
                outputs_dir,
                query,
                limit=limit,
                include_archival=bool(include_archival),
                label="search_outputs",
            )
        except _hardening.HeavyToolTimeout as exc:
            return {
                "outputs_dir": outputs_dir,
                "query": query,
                "hits": [],
                "total_indexed": 0,
                "timed_out": True,
                "error": str(exc),
            }
    return _MISS


async def _handle_code_index_tools(
    name: str,
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """Dispatch group: search_code_semantic (93fce816 — Cursor-style local code
    index: tree-sitter chunks + Merkle-incremental reindex + hybrid BM25/VSS)
    and prospect_symbol (2ce5bc76 — three-rung fallback chain for robust
    symbol prospecting)."""
    if name == "prospect_symbol":
        symbol = str(args.get("symbol") or "").strip()
        if not symbol:
            raise ValueError("symbol is required")
        project_id = str(args.get("project_id") or "").strip()
        root_dir = str(args.get("root_dir") or "").strip()
        limit = args.get("limit", 5)
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = 5
        kind = args.get("kind")
        kind = str(kind).strip() if kind else None
        stale_graph = bool(args.get("stale_graph", False))
        from .. import prospect as _prospect  # noqa: PLC0415
        return await _prospect.prospect_symbol_impl(
            symbol=symbol,
            project_id=project_id,
            root_dir=root_dir,
            limit=limit,
            kind=kind,
            stale_graph=stale_graph,
            tenant=tenant,
            data_dir=data_dir,
        )
    if name == "search_code_semantic":
        from .. import code_index as _code_index  # noqa: PLC0415
        from .. import hardening as _hardening  # noqa: PLC0415
        root_dir = str(args.get("root_dir") or "").strip()
        query = str(args.get("query") or "").strip()
        if not root_dir:
            raise ValueError("root_dir is required")
        if not query:
            raise ValueError("query is required")
        limit = args.get("limit", 10)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 10
        kind = args.get("kind")
        kind = str(kind).strip() if kind else None
        reindex = args.get("reindex", True)
        # Persist the sidecar per project dir so incremental Merkle reindex is
        # cheap across calls; falls back to in-memory if data_dir is unset.
        db_path = ":memory:"
        if data_dir:
            db_path = os.path.join(data_dir, "code_index.duckdb")
        # The walk + hash + tree-sitter chunk + DuckDB FTS build is CPU-bound.
        # Run it in the bulkhead pool under a hard deadline (e5f96adf / 1d021501)
        # so a pathological repo fails fast + stays isolated instead of blocking
        # unrelated MCP calls by exhausting the shared to_thread executor.
        try:
            return await _hardening.run_in_bulkhead(
                _code_index.search_code_semantic,
                root_dir,
                query,
                limit=limit,
                kind=kind,
                db_path=db_path,
                reindex=bool(reindex),
                label="search_code_semantic",
            )
        except _hardening.HeavyToolTimeout as exc:
            return {
                "root_dir": root_dir,
                "query": query,
                "hits": [],
                "total_indexed": 0,
                "timed_out": True,
                "error": str(exc),
            }
    return _MISS


async def _dispatch_mcp_tool(
    name: str,
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None = None,
) -> Any:
    """Route a tools/call to the appropriate db_module function."""
    # Tenant scope for the workspace layer (notes/decisions/settings). None for
    # self-host / unauthenticated; the db functions then skip isolation.
    _mcp_tenant_id = tenant.get("id") if tenant else None
    # b6ab6e83 — project_name resolver: accept project_name as alternative to
    # project_id, and resolve non-UUID project_id values as human-readable names.
    _pid_raw = args.get("project_id", "")
    _pname_raw = args.get("project_name", "")
    _is_uuid = bool(re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        _pid_raw, re.I,
    ))
    if _pname_raw or (_pid_raw and not _is_uuid):
        _lookup = _pname_raw or _pid_raw
        _resolved_proj = await db_module.get_project_by_name(db, _lookup)
        if _resolved_proj:
            args = {**args, "project_id": _resolved_proj["id"]}
        elif _pname_raw and not _pid_raw:
            raise ValueError(f"no project found matching name '{_lookup}'")
    _groups = (
        _handle_project_tools,
        _handle_task_tools,
        _handle_notes_decisions,
        _handle_hitl_tools,
        _handle_session_tools,
        _handle_sprint_tools,
        _handle_file_claims,
        _handle_planning_tools,
        _handle_plugin_tools,
        _handle_tunnel_tools,
        _handle_outputs_tools,
        _handle_code_index_tools,
    )
    for _grp in _groups:
        _result = await _grp(name, args, db, data_dir, tenant, _mcp_tenant_id)
        if _result is not _MISS:
            # bf51b12e — planner context-refresh nudge. Fully defensive: any error
            # falls through to the untouched _result. In-memory turn tracking +
            # gating (best-effort, per-process). Skips executor sessions entirely.
            try:
                if isinstance(_result, dict):
                    session_id = args.get("session_id")
                    if session_id:
                        _state = _SESSION_REFRESH_STATE.setdefault(
                            session_id, {"calls": 0, "last_refresh": 0}
                        )
                        _state["calls"] += 1
                        if session_id not in _EXECUTOR_SESSIONS:
                            settings = await db_module.get_workspace_settings(
                                db, tenant_id=_mcp_tenant_id
                            )
                            if settings.get("auto_refresh_enabled"):
                                _triggers = settings.get("refresh_triggers")
                                enabled_triggers = (
                                    _triggers if isinstance(_triggers, list)
                                    else _PLANNER_REFRESH_TRIGGERS
                                )
                                _interval = settings.get("refresh_interval_turns") or 10
                                _calls = _state["calls"]
                                _last = _state["last_refresh"]
                                _fire = (
                                    name in enabled_triggers
                                    or (_calls - _last) >= _interval
                                )
                                # One-per-call: only fire if we haven't already
                                # refreshed at this call index.
                                if _fire and _last < _calls:
                                    project_id = args.get("project_id")
                                    if project_id:
                                        _refresh = await _build_context_refresh(
                                            db, project_id
                                        )
                                        if _refresh is not None:
                                            _state["last_refresh"] = _calls
                                            r = dict(_result)
                                            r["_context_refresh"] = _refresh
                                            return r
            except Exception:  # noqa: BLE001 — nudge must never break a tool call
                pass
            return _result
    raise ValueError(f"unknown tool: {name}")


_MCP_RATE_LIMIT = "100/minute"
