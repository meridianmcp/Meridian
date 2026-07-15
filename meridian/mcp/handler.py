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


def _check_file_only_resources_warning(
    touches_resources: Any,
    title: str,
    description: str = "",
) -> str | None:
    """7fe57cea — surface a non-blocking hint when a caller declares a bare
    ``file:path.py`` resource without a symbol suffix, even though a
    ``file:path.py:symbol_name`` (or ``symbol:`` form) would give finer-grained
    parallelism control.

    Algorithm:
    1. Walk every ``touches_resources`` entry and collect file-only entries
       (prefix ``file:`` with exactly one path component, no trailing ``:symbol``).
       Entries already scoped to a symbol (``file:path.py:sym`` or ``symbol:...``)
       and inferred/db/route entries are skipped.
    2. Extract likely Python identifier tokens from the title + description using a
       light regex (snake_case / CamelCase with length >= 3) that are plausible
       symbol names in the affected files.
    3. If there are file-only entries, return an informational warning string
       listing them and suggesting the symbol-scoped form. When identifiers were
       extracted from the text, include the top candidates as concrete examples.

    Non-fatal by construction — callers add this to a ``symbol_scope_hint`` field
    only; filing never blocks. Returns ``None`` when everything is already
    symbol-scoped or no file: entries are present.
    """
    import re as _re  # noqa: PLC0415 — lazy import keeps module-level clean

    entries = _parse_touches_files(touches_resources)
    file_only: list[str] = []
    for entry in entries:
        raw = entry
        # Skip inferred: entries entirely — they are auto-generated server-side,
        # not caller declarations, so warning would be noise.
        if raw.startswith("inferred:"):
            continue
        # Only care about explicit file: entries
        if not raw.startswith("file:"):
            continue
        path_part = raw[len("file:"):]
        # Split on ":" — if there's a second segment, the symbol is already declared
        segments = path_part.split(":", 1)
        if len(segments) == 1 or not segments[1]:
            # Bare file:path.py — no symbol declared
            file_only.append(raw)

    if not file_only:
        return None  # All resources are already symbol-scoped (or no file: entries)

    # Extract plausible symbol names from title + description
    combined = f"{title} {description}".strip()
    # Capture snake_case (likely functions/methods) and PascalCase (classes).
    # Require length >= 3 to skip noise; exclude pure lowercase common words by
    # requiring an underscore OR uppercase letter OR digit inside the token.
    candidates: list[str] = []
    seen_cands: set[str] = set()
    for tok in _re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]{2,})\b", combined):
        low = tok.lower()
        # Skip stopwords already used elsewhere
        if low in _PROSPECT_STOPWORDS:
            continue
        # Require the token to look like a code identifier:
        # contains underscore OR has uppercase after first char OR has digit
        if not ("_" in tok or any(c.isupper() for c in tok[1:]) or any(c.isdigit() for c in tok)):
            continue
        if tok not in seen_cands:
            seen_cands.add(tok)
            candidates.append(tok)
        if len(candidates) >= 5:
            break

    affected = ", ".join(f"``{e}``" for e in file_only)
    if candidates:
        examples = " or ".join(
            f"``{e}:{c}``" for e, c in zip(file_only[:2], candidates[:2])
        )
        hint = (
            f"SYMBOL_SCOPE_HINT: {affected} declared at file level. "
            f"Prefer symbol-scoped ids when items touch different functions in the "
            f"same file — e.g. {examples}. "
            f"This allows co-batching in the same parallel wave. "
            f"(Non-fatal: item filed as-is. Use file:path.py:symbol_name format.)"
        )
    else:
        hint = (
            f"SYMBOL_SCOPE_HINT: {affected} declared at file level. "
            f"Prefer symbol-scoped ids (file:path.py:symbol_name or symbol:path::Name) "
            f"when two items touch different functions in the same file — "
            f"this allows them to co-batch in the same parallel wave. "
            f"(Non-fatal: item filed as-is.)"
        )
    return hint


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


async def _code_notes_for_item_resources(
    db: "Any",
    project_id: str,
    item: "dict[str, Any]",
) -> "list[dict[str, Any]]":
    """c37ea630 — proactively surface code-anchored notes for a claimed item's files.

    When ``claim_sprint_item`` succeeds, this helper looks up project notes with
    ``note_kind='code'`` for every ``file:`` entry in the item's
    ``touches_resources``.  The result mirrors the ``code_notes`` list already
    returned by ``claim_file`` / ``get_file_claims`` (same shape, same
    :func:`get_code_notes_for_file` call) so executors see the same warnings at
    claim time rather than only when they later call ``claim_file`` per-file.

    Returns a list of dicts, each with a ``file_path`` key and a ``notes`` list::

        [
            {"file_path": "meridian/db/__init__.py", "notes": [{...}, ...]},
            ...
        ]

    Only files that actually have code-anchored notes appear in the list; files
    with zero notes are omitted so the field stays compact.  Fail-open: any
    per-file DB error is suppressed and that file is silently skipped — the claim
    itself is never blocked.
    """
    if not project_id or not isinstance(item, dict):
        return []
    file_paths: list[str] = []
    for entry in _parse_touches_files(item.get("touches_resources")):
        if entry.startswith("file:"):
            file_paths.append(entry[len("file:"):])
        elif entry.startswith("inferred:file:"):
            file_paths.append(entry[len("inferred:file:"):])
    # Also extract symbol paths so the file portion can be included.
    for entry in _parse_touches_files(item.get("touches_resources")):
        if entry.startswith("symbol:"):
            # symbol entries are "file_path::symbol_name" — extract the file
            sym_val = entry[len("symbol:"):]
            if "::" in sym_val:
                sym_file = sym_val.split("::")[0]
            else:
                sym_file = sym_val
            if sym_file and sym_file not in file_paths:
                file_paths.append(sym_file)
    if not file_paths:
        return []
    results: list[dict[str, Any]] = []
    for fp in file_paths:
        try:
            notes = await db_module.get_code_notes_for_file(db, project_id, fp)
        except Exception:  # noqa: BLE001 — never block the claim
            notes = []
        if notes:
            results.append({"file_path": fp, "notes": notes})
    return results


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


async def _wave_assignment_hint(db: Any, project_id: str) -> str | None:
    """a389b95d — nudge callers to run assign_sprint_waves when unassigned items exist.

    Returns a non-blocking ``wave_assignment_hint`` string when:
      1. One or more pending/todo items have ``wave IS NULL`` (no wave label assigned), AND
      2. No executor session has been seen active in the last 10 minutes (i.e. it is safe
         to re-run assign_sprint_waves without risking desync against a mid-flight /goal
         that already references specific wave labels).

    Returns ``None`` when waves are fully assigned or an active session is in-flight
    (in which case automatic mutation would be unsafe).

    Non-fatal by design — any error returns ``None`` so the calling add/fan-out path
    is never blocked.  Mirrors the ``drift_warning`` / ``symbol_scope_hint`` /
    ``active_session_warning`` response-field pattern already used by ``add_sprint_item``.
    """
    try:
        # Count pending/todo items with no wave label yet.
        async with db.execute(
            "SELECT COUNT(*) FROM sprint_items "
            "WHERE project_id = ? AND wave IS NULL AND status IN ('todo', 'pending')",
            (project_id,),
        ) as _cur:
            _row = await _cur.fetchone()
        _unassigned = (_row[0] if _row else 0) if _row is not None else 0
        if not _unassigned:
            return None
        # Only emit the nudge when no executor is currently mid-flight — if sessions
        # are active we stay silent (auto-mutation could desync them from their /goal).
        _active = await _active_executor_session_warnings(db, project_id)
        if _active:
            return None
        return (
            f"WAVE_ASSIGNMENT_HINT: {_unassigned} pending/todo item(s) have no wave "
            "label (wave IS NULL). Run assign_sprint_waves() to assign parallelization "
            "waves before executors start claiming — labels cannot safely be reassigned "
            "once sessions are in-flight."
        )
    except Exception:  # noqa: BLE001 — best-effort; never block add/fan-out
        return None


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
    """Dispatch group: create_project, set_parent_project, rename_project,
    register_session, start_session, list_projects, get_project_by_name,
    get_goal, set_goal, set_north_star, merge_project.

    97d695c4 — the original if/elif chain has been replaced with a per-tool
    dispatch table (dict mapping tool name -> handler function).  Each tool's
    logic lives in its own named function in
    ``meridian.mcp.handlers.project_tools``.  This is a PURELY MECHANICAL
    extraction: zero behaviour change, same tool names, same arguments, same
    return values.
    """
    from .handlers.project_tools import (  # noqa: PLC0415
        handle_create_project,
        handle_set_parent_project,
        handle_rename_project,
        handle_register_session,
        handle_start_session,
        handle_list_projects,
        handle_get_project_by_name,
        handle_get_goal,
        handle_set_goal,
        handle_set_north_star,
        handle_merge_project,
    )

    # Tools that need no extra context beyond the standard five parameters.
    _standard_dispatch: dict[str, Any] = {
        "create_project": handle_create_project,
        "set_parent_project": handle_set_parent_project,
        "rename_project": handle_rename_project,
        "register_session": handle_register_session,
        "list_projects": handle_list_projects,
        "get_project_by_name": handle_get_project_by_name,
        "get_goal": handle_get_goal,
        "set_goal": handle_set_goal,
        "set_north_star": handle_set_north_star,
        "merge_project": handle_merge_project,
    }

    if name in _standard_dispatch:
        return await _standard_dispatch[name](args, db, data_dir, tenant, _mcp_tenant_id)

    # start_session needs the handler-level _EXECUTOR_SESSIONS set so the
    # bf51b12e planner-nudge gate works without a circular import.
    if name == "start_session":
        return await handle_start_session(
            args, db, data_dir, tenant, _mcp_tenant_id,
            executor_sessions=_EXECUTOR_SESSIONS,
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
    """Dispatch group: log_task, get_tasks, search_tasks, generate_handoff, load_handoff, verify_handoff_token."""
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
        _handoff_amended = False
        try:
            path, content, _handoff_amended = await asyncio.wait_for(
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
            "amended": _handoff_amended,
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
    if name == "verify_handoff_token":
        # dd07ece0 — verify a provenance token extracted from a pasted /goal block.
        # Delegates to the in-process token store in handoff.py; no DB access needed.
        from .. import handoff as handoff_module_local  # noqa: PLC0415
        _token = (args.get("token") or "").strip()
        if not _token:
            return {"valid": False, "reason": "not_found"}
        _pid = args.get("project_id") or ""
        return handoff_module_local.verify_handoff_token(_token, _pid)
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
    """Dispatch group: pin_decision, update_decision, validate_assumption,
    get_pinned_decisions, archive_decision, add_note, ingest_document,
    get_document_structure, get_latex_structure, get_citation_edges,
    resolve_citations, index_equation, find_similar_equation, insert_equation,
    update_paragraph, find_symbol_usages, index_figure, find_similar_figure,
    link_figure_caption, index_table, find_similar_table,
    ingest_document_structure, add_insight, get_insights, save_finding,
    capture_research_finding, get_notes, read_note, delete_note,
    add_workspace_note, get_workspace_notes, pin_workspace_decision,
    get_workspace_decisions, get_workspace_settings, update_workspace_settings,
    save_blog_post, get_blog_posts, add_workspace_sprint_item,
    get_workspace_sprint_items, update_workspace_sprint_item,
    complete_workspace_sprint_item, add_workspace_proposal,
    get_workspace_proposals, advance_proposal_status, promote_proposal.

    ac4df52f — the original if/elif chain has been replaced with a per-tool
    dispatch table (dict mapping tool name -> handler function).  Each tool's
    logic lives in its own named function in
    ``meridian.mcp.handlers.notes_decisions``.  This is a PURELY MECHANICAL
    extraction: zero behaviour change, same tool names, same arguments, same
    return values.
    """
    from .handlers.notes_decisions import (  # noqa: PLC0415
        handle_pin_decision,
        handle_update_decision,
        handle_validate_assumption,
        handle_get_pinned_decisions,
        handle_archive_decision,
        handle_add_note,
        handle_get_notes,
        handle_read_note,
        handle_delete_note,
        handle_ingest_document,
        handle_get_document_structure,
        handle_get_latex_structure,
        handle_ingest_document_structure,
        handle_get_citation_edges,
        handle_resolve_citations,
        handle_index_equation,
        handle_find_similar_equation,
        handle_insert_equation,
        handle_update_paragraph,
        handle_find_symbol_usages,
        handle_index_figure,
        handle_find_similar_figure,
        handle_link_figure_caption,
        handle_index_table,
        handle_find_similar_table,
        handle_add_insight,
        handle_get_insights,
        handle_save_finding,
        handle_capture_research_finding,
        handle_add_workspace_note,
        handle_get_workspace_notes,
        handle_pin_workspace_decision,
        handle_get_workspace_decisions,
        handle_get_workspace_settings,
        handle_update_workspace_settings,
        handle_save_blog_post,
        handle_get_blog_posts,
        handle_add_workspace_sprint_item,
        handle_get_workspace_sprint_items,
        handle_update_workspace_sprint_item,
        handle_complete_workspace_sprint_item,
        handle_add_workspace_proposal,
        handle_get_workspace_proposals,
        handle_advance_proposal_status,
        handle_promote_proposal,
    )

    # All 45 tools map directly to handler functions with the standard five
    # parameters — no extra context needed beyond (args, db, data_dir, tenant,
    # _mcp_tenant_id).
    _standard_dispatch: dict[str, Any] = {
        "pin_decision": handle_pin_decision,
        "update_decision": handle_update_decision,
        "validate_assumption": handle_validate_assumption,
        "get_pinned_decisions": handle_get_pinned_decisions,
        "archive_decision": handle_archive_decision,
        "add_note": handle_add_note,
        "get_notes": handle_get_notes,
        "read_note": handle_read_note,
        "delete_note": handle_delete_note,
        "ingest_document": handle_ingest_document,
        "get_document_structure": handle_get_document_structure,
        "get_latex_structure": handle_get_latex_structure,
        "ingest_document_structure": handle_ingest_document_structure,
        "get_citation_edges": handle_get_citation_edges,
        "resolve_citations": handle_resolve_citations,
        "index_equation": handle_index_equation,
        "find_similar_equation": handle_find_similar_equation,
        "insert_equation": handle_insert_equation,
        "update_paragraph": handle_update_paragraph,
        "find_symbol_usages": handle_find_symbol_usages,
        "index_figure": handle_index_figure,
        "find_similar_figure": handle_find_similar_figure,
        "link_figure_caption": handle_link_figure_caption,
        "index_table": handle_index_table,
        "find_similar_table": handle_find_similar_table,
        "add_insight": handle_add_insight,
        "get_insights": handle_get_insights,
        "save_finding": handle_save_finding,
        "capture_research_finding": handle_capture_research_finding,
        "add_workspace_note": handle_add_workspace_note,
        "get_workspace_notes": handle_get_workspace_notes,
        "pin_workspace_decision": handle_pin_workspace_decision,
        "get_workspace_decisions": handle_get_workspace_decisions,
        "get_workspace_settings": handle_get_workspace_settings,
        "update_workspace_settings": handle_update_workspace_settings,
        "save_blog_post": handle_save_blog_post,
        "get_blog_posts": handle_get_blog_posts,
        "add_workspace_sprint_item": handle_add_workspace_sprint_item,
        "get_workspace_sprint_items": handle_get_workspace_sprint_items,
        "update_workspace_sprint_item": handle_update_workspace_sprint_item,
        "complete_workspace_sprint_item": handle_complete_workspace_sprint_item,
        "add_workspace_proposal": handle_add_workspace_proposal,
        "get_workspace_proposals": handle_get_workspace_proposals,
        "advance_proposal_status": handle_advance_proposal_status,
        "promote_proposal": handle_promote_proposal,
    }

    if name in _standard_dispatch:
        return await _standard_dispatch[name](args, db, data_dir, tenant, _mcp_tenant_id)

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
    """Dispatch group: checkpoint, get_context_block, list_sessions, get_session_log,
    get_session_activity, get_agent_instructions, set_agent_instructions,
    set_executor_config, idle_until_session_done, search_all, search_synthesis,
    paper_search, get_session_brief.

    81abd31f — the original if/elif chain has been replaced with a per-tool
    dispatch table (dict mapping tool name -> handler function).  Each tool's
    logic lives in its own named function in
    ``meridian.mcp.handlers.session_tools``.  This is a PURELY MECHANICAL
    extraction: zero behaviour change, same tool names, same arguments, same
    return values.
    """
    from .handlers.session_tools import (  # noqa: PLC0415
        handle_checkpoint,
        handle_get_context_block,
        handle_list_sessions,
        handle_get_session_log,
        handle_get_session_activity,
        handle_get_connection_log,
        handle_get_agent_instructions,
        handle_set_agent_instructions,
        handle_set_executor_config,
        handle_idle_until_session_done,
        handle_search_all,
        handle_search_synthesis,
        handle_paper_search,
        handle_get_session_brief,
    )

    # Tools that need no extra context beyond the standard five parameters.
    _standard_dispatch: dict[str, Any] = {
        "get_context_block": handle_get_context_block,
        "list_sessions": handle_list_sessions,
        "get_session_log": handle_get_session_log,
        "get_session_activity": handle_get_session_activity,
        "get_connection_log": handle_get_connection_log,
        "get_agent_instructions": handle_get_agent_instructions,
        "set_agent_instructions": handle_set_agent_instructions,
        "set_executor_config": handle_set_executor_config,
        "idle_until_session_done": handle_idle_until_session_done,
        "search_all": handle_search_all,
        "search_synthesis": handle_search_synthesis,
        "paper_search": handle_paper_search,
        "get_session_brief": handle_get_session_brief,
    }

    if name in _standard_dispatch:
        return await _standard_dispatch[name](args, db, data_dir, tenant, _mcp_tenant_id)

    # checkpoint needs handler-level _fetch_recent_commits and
    # _resolve_caller_identity passed explicitly to keep the import graph acyclic.
    if name == "checkpoint":
        return await handle_checkpoint(
            args, db, data_dir, tenant, _mcp_tenant_id,
            fetch_recent_commits=_fetch_recent_commits,
            resolve_caller_identity=_resolve_caller_identity,
        )

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
    """Dispatch group: add_sprint_note, get_sprint_notes, add_sprint_item,
    fan_out_sprint_items, update_sprint_item, set_sprint, get_sprint_progress,
    get_sprint_items, get_parallelizable_groups, assign_sprint_waves,
    analyze_sprint, claim_sprint_item, add_subtask, split_sprint_item,
    merge_sprint_items, complete_sprint_item, add_sprint_item_pointer,
    get_sprint_item_pointers, resolve_sprint_item_pointers,
    delete_sprint_item_pointer.

    ba4f879b — the original if/elif chain has been replaced with a per-tool
    dispatch table (dict mapping tool name -> handler function).  Each tool's
    logic lives in its own named function in
    ``meridian.mcp.handlers.sprint_tools``.  This is a PURELY MECHANICAL
    extraction: zero behaviour change, same tool names, same arguments, same
    return values.
    """
    from .handlers.sprint_tools import (  # noqa: PLC0415
        handle_add_sprint_note,
        handle_get_sprint_notes,
        handle_add_sprint_item,
        handle_fan_out_sprint_items,
        handle_update_sprint_item,
        handle_set_sprint,
        handle_get_sprint_progress,
        handle_get_sprint_items,
        handle_get_parallelizable_groups,
        handle_assign_sprint_waves,
        handle_analyze_sprint,
        handle_claim_sprint_item,
        handle_add_subtask,
        handle_split_sprint_item,
        handle_merge_sprint_items,
        handle_complete_sprint_item,
        handle_add_sprint_item_pointer,
        handle_get_sprint_item_pointers,
        handle_resolve_sprint_item_pointers,
        handle_delete_sprint_item_pointer,
    )

    _standard_dispatch: dict[str, Any] = {
        "add_sprint_note": handle_add_sprint_note,
        "get_sprint_notes": handle_get_sprint_notes,
        "add_sprint_item": handle_add_sprint_item,
        "fan_out_sprint_items": handle_fan_out_sprint_items,
        "update_sprint_item": handle_update_sprint_item,
        "set_sprint": handle_set_sprint,
        "get_sprint_progress": handle_get_sprint_progress,
        "get_sprint_items": handle_get_sprint_items,
        "get_parallelizable_groups": handle_get_parallelizable_groups,
        "assign_sprint_waves": handle_assign_sprint_waves,
        "analyze_sprint": handle_analyze_sprint,
        "claim_sprint_item": handle_claim_sprint_item,
        "add_subtask": handle_add_subtask,
        "split_sprint_item": handle_split_sprint_item,
        "merge_sprint_items": handle_merge_sprint_items,
        "complete_sprint_item": handle_complete_sprint_item,
        "add_sprint_item_pointer": handle_add_sprint_item_pointer,
        "get_sprint_item_pointers": handle_get_sprint_item_pointers,
        "resolve_sprint_item_pointers": handle_resolve_sprint_item_pointers,
        "delete_sprint_item_pointer": handle_delete_sprint_item_pointer,
    }

    if name in _standard_dispatch:
        return await _standard_dispatch[name](args, db, data_dir, tenant, _mcp_tenant_id)

    return _MISS


async def _handle_file_claims(
    name: str,
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """Dispatch group: claim_file, get_file_claims, get_symbol_claims, get_symbol_hotspots, release_file, get_graph_diff, snapshot_graph_metrics, claim_docx_region, get_docx_region_claims, release_docx_region_claims."""
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
            tenant_id=_mcp_tenant_id,  # 342dd15f — Redis cost-guard
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
    if name == "claim_docx_region":
        # f7ee1ba7 — Model B scoped-region claiming for .docx files.
        return await db_module.claim_docx_region(
            db,
            session_id=args["session_id"],
            file_path=args["file_path"],
            element_id=args["element_id"],
        )
    if name == "get_docx_region_claims":
        # f7ee1ba7 — read-only: active scoped region claims on a .docx file.
        return {
            "file_path": args["file_path"],
            "claims": await db_module.get_docx_region_claims(db, args["file_path"]),
        }
    if name == "release_docx_region_claims":
        # f7ee1ba7 — release scoped docx-region claims for a session.
        released = await db_module.release_docx_region_claims(
            db, args["session_id"],
            file_path=args.get("file_path"),
            element_id=args.get("element_id"),
        )
        return {
            "released": released,
            "session_id": args["session_id"],
            "file_path": args.get("file_path"),
            "element_id": args.get("element_id"),
        }
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


# a749f87c — _select_active_tool_set lives in mcp_tools.py (pure, no I/O,
# importable without server.py's circular-import chain). Re-export here so
# the handler dispatch can call it, and so any handler-level caller works
# without changing its import path.
from ..mcp_tools import _select_active_tool_set  # noqa: E402


# 8eea5a9a — Deferral/blocker language in notes without matching structured
# fields.  A prior session correctly diagnosed 5 items as deferred/blocked in
# PROSE (notes) but never called the structured field update, so
# get_parallelizable_groups / generate_handoff kept surfacing them as ordinary
# claimable work.  This check is the SYSTEMIC prevention: scan every pending
# item (blocker_kind IS NULL, deferred_until IS NULL) for well-known
# deferral/blocker keywords in its notes and surface mismatches as drift
# warnings so a planner or executor can apply the structured field deliberately.
# It is intentionally READ-ONLY — auto-guessing blocker_kind or deferred_until
# would be presumptuous and wrong.
_DEFERRAL_NOTES_KEYWORDS: tuple[str, ...] = (
    "flagged",
    "deferred",
    "blocked",
    "not implementable",
    "not fixable here",
    "not for executor",
)


def _detect_notes_blocker_drift(
    pending_items: list[dict],
) -> list[dict]:
    """Return drift findings for pending items whose notes describe a deferral
    or blocker but whose structured fields (blocker_kind, deferred_until) are
    both unset.

    Only items that are status='pending' AND blocker_kind IS NULL AND
    deferred_until IS NULL are candidates.  This is a read-only check; it does
    NOT set any field.

    Returns a list of dicts:
        {item_id, title, matched_keyword, snippet, warning}
    """
    findings: list[dict] = []
    for item in pending_items:
        # Only candidates: truly unblocked-by-structure pending items.
        if item.get("blocker_kind") is not None:
            continue
        if item.get("deferred_until") is not None:
            continue
        notes_raw: str = (item.get("notes") or "").strip()
        if not notes_raw:
            continue
        notes_lower = notes_raw.lower()
        for kw in _DEFERRAL_NOTES_KEYWORDS:
            if kw in notes_lower:
                # Surface a short snippet around the matched keyword for context.
                idx = notes_lower.find(kw)
                snippet_start = max(0, idx - 20)
                snippet_end = min(len(notes_raw), idx + len(kw) + 60)
                snippet = notes_raw[snippet_start:snippet_end].strip()
                findings.append({
                    "item_id": item.get("id"),
                    "title": item.get("title", ""),
                    "matched_keyword": kw,
                    "snippet": snippet,
                    "warning": (
                        f"Item notes describe a deferral or blocker "
                        f"(keyword: '{kw}') but blocker_kind and deferred_until "
                        f"are both unset. Call update_sprint_item with "
                        f"blocker_kind='manual' or deferred_until=<ISO timestamp> "
                        f"so this item stops surfacing as ordinary claimable work."
                    ),
                })
                break  # one finding per item is enough
    return findings


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
        # include_manual_blocker=True so we can inspect ALL pending items,
        # including those already tagged manual — the notes-drift check only
        # fires on items where blocker_kind IS NULL, so tagged items are
        # skipped automatically inside _detect_notes_blocker_drift.
        pending_items = await db_module.get_sprint_items(
            db, args["project_id"], status="pending", include_manual_blocker=True
        )
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
        # 8eea5a9a — surface items whose notes describe a deferral/blocker
        # but whose structured fields (blocker_kind, deferred_until) are unset.
        # Read-only: callers must apply the structured field themselves.
        notes_drift = _detect_notes_blocker_drift(pending_items)
        return {
            "pending_item_count": len(pending_items),
            "commit_count": len(commits),
            "drift_count": len(matches),
            "high_confidence": sum(1 for m in matches if m["confidence"] == "high"),
            "medium_confidence": sum(1 for m in matches if m["confidence"] == "medium"),
            "matches": action_items,
            "notes_blocker_drift": notes_drift,
            "notes_blocker_drift_count": len(notes_drift),
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
            # 8c147109 — activity heartbeat: record a compact one-liner into the
            # session_activity ring-buffer so a remote planner session can observe
            # signs of life via get_session_log even before the executor calls
            # log_task(). Only fires for executor sessions; never for the observer
            # tools (get_session_log/get_session_activity/heartbeat) themselves.
            _ACTIVITY_SKIP_TOOLS = frozenset({
                "heartbeat", "get_session_log", "get_session_activity",
                "update_session_seen",
            })
            try:
                _act_sid = args.get("session_id")
                if (
                    _act_sid
                    and _act_sid in _EXECUTOR_SESSIONS
                    and name not in _ACTIVITY_SKIP_TOOLS
                ):
                    # Build a compact one-line summary from the tool args.
                    _act_parts: list[str] = []
                    for _k in ("project_id", "item_id", "file_path", "path",
                               "description", "title", "query"):
                        _v = args.get(_k)
                        if _v and isinstance(_v, str):
                            _act_parts.append(f"{_k}={_v[:60]}")
                            break  # one key is enough
                    _act_summary = (
                        (", ".join(_act_parts)) if _act_parts else name
                    )
                    await db_module.record_session_activity(
                        db, _act_sid, name, _act_summary
                    )
            except Exception:  # noqa: BLE001 — activity recording must never fail a call
                pass
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
                                # edd9c54b — amend path: suppress the
                                # trigger-based nudge for generate_handoff when
                                # the call merely amended an unconsumed prior
                                # handoff (nothing genuinely new to report).
                                _amended_handoff = (
                                    name == "generate_handoff"
                                    and isinstance(_result, dict)
                                    and _result.get("amended") is True
                                )
                                _fire = (
                                    (name in enabled_triggers and not _amended_handoff)
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
