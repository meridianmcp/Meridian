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
            "generate_handoff() is called at the end. Stop after 40 turns or if HITL triggered.\n\n"
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

    quick_start_goal = _build_quick_start_goal(
        pending,
        version=scoped_version,
        # ecf69de8 — the project's executor posture selects the /goal framing.
        execution_mode=db_module.normalize_execution_mode(
            project.get("execution_mode")
        ),
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
        # Single-connector bridge: when this tenant has a live `meridian --tunnel`,
        # surface its filesystem / code-intel / extractor tools here so the user's
        # existing Meridian connector gains them with zero extra config. Reserve
        # native + GitHub names so the merged list has no duplicates.
        if tenant and tenant.get("id"):
            from ..routes import tunnel as _tunnel_mod  # noqa: PLC0415
            if _tunnel_mod.has_active_tunnel(tenant["id"]):
                try:
                    # Reserve native names AND the full GitHub name set: tools/call
                    # always routes GitHub names (e.g. read_file) to the GitHub
                    # dispatch when a tenant is set, so the tunnel must not advertise
                    # them here or list/call would disagree.
                    reserved = {t.get("name") for t in tools} | set(_server._GITHUB_TOOL_NAMES)
                    tools = tools + await _tunnel_mod.list_tunnel_tools(
                        tenant["id"], reserved,
                    )
                except Exception:  # noqa: BLE001
                    pass  # tunnel hiccup must never break native tools/list
        return _server._jsonrpc_ok(req_id, {"tools": tools})

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
                    tunnel_result = await _tunnel_mod.call_tunnel_tool(
                        tenant["id"], name, args,
                    )
                    if tunnel_result is not None:
                        # Pass the tunneled server's result through verbatim — it
                        # already carries the MCP `content` envelope.
                        return _server._jsonrpc_ok(req_id, tunnel_result)
            if _is_github:
                result = await _dispatch_github_tool(name, args, tenant, db)
            else:
                result = await _dispatch_mcp_tool(name, args, db, data_dir, tenant=tenant)
            return _server._jsonrpc_ok(req_id, {"content": [{"type": "text", "text": json.dumps(result)}]})
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


async def _fetch_recent_commits(
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
        return await db_module.create_project(
            db, args["name"], execution_mode=args.get("execution_mode"),
        )
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
        return await _server._start_session_composite(
            db,
            args["project_id"],
            args["session_name"],
            data_dir,
            human_id=args.get("human_id"),
            client_type=args.get("client"),
            role=args.get("role"),
            compact=args.get("compact", True),
            version=args.get("version"),
        )
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
    return _MISS


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
        try:
            path, content = await asyncio.wait_for(
                handoff_module_local.generate_handoff(
                    db,
                    args["project_id"],
                    data_dir,
                    mode=mode,
                    session_id=session_id,
                    commit_messages=[c["message"] for c in _gh_commits],
                ),
                timeout=90.0,
            )
        except asyncio.TimeoutError:
            path, content = await handoff_module_local._generate_handoff_l0(
                db, args["project_id"], data_dir
            )
            mode = "full"
        return {"file_path": path, "content": content, "mode": mode}
    return _MISS


async def _handle_notes_decisions(
    name: str,
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """Dispatch group: pin_decision, update_decision, get_pinned_decisions, archive_decision, add_note, ingest_document, capture_insight, get_notes, read_note, delete_note, add_workspace_note, get_workspace_notes, pin_workspace_decision, get_workspace_decisions, get_workspace_settings, update_workspace_settings, add_workspace_sprint_item, get_workspace_sprint_items, update_workspace_sprint_item, complete_workspace_sprint_item."""
    if name == "pin_decision":
        validate_input_size(args.get("title"), "decision title", 500)
        validate_input_size(args.get("body"), "decision body", 100_000)
        category = args.get("category", "TECHNICAL")
        result = await db_module.pin_decision(
            db, args["project_id"], args["title"], args["body"], category,
            priority=args.get("priority", "normal"),
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
        )
        if result is None:
            raise ValueError("decision not found")
        return result
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
        try:
            result = await db_module.add_project_note(
                db, args["project_id"], args["title"], args["body"],
                args.get("tags"), kind=args.get("kind"),
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
        from ..doc_ingest import DocExtractionError  # local import: optional dep-free
        try:
            return await db_module.ingest_document(
                db, args["project_id"],
                file_path=args.get("file_path"),
                content=args.get("content"),
                title=args.get("title"),
                source=args.get("source"),
                tags=args.get("tags"),
            )
        except (ValueError, DocExtractionError, FileNotFoundError) as exc:
            return {"error": str(exc)}
    if name == "capture_insight":
        # db9edba3 — one-call insight capture for planning (claude.ai) sessions:
        # persists a kind='insight' note (prominent in the dashboard + surfaced in
        # the planner handoff) WITHOUT the auto-capture "Session summary" noise.
        validate_input_size(args.get("title"), "insight title", 500)
        _ins_body = args.get("body")
        _bullets = args.get("bullet_points")
        if (not _ins_body) and isinstance(_bullets, list) and _bullets:
            _ins_body = "\n".join(f"- {str(b).strip()}" for b in _bullets if str(b).strip())
        _ins_body = _ins_body or ""
        validate_input_size(_ins_body, "insight body", 10_000_000)
        if not _ins_body:
            return {"error": "capture_insight requires body or non-empty bullet_points"}
        _ins_tags = args.get("tags")
        _ins_tags = f"{_ins_tags},insight" if _ins_tags else "insight"
        return await db_module.add_project_note(
            db, args["project_id"], args["title"], _ins_body,
            _ins_tags, kind="insight",
            priority=args.get("priority", "normal"),
        )
    if name == "get_notes":
        # 5a5bba43 — pull model: default to the lightweight list (no bodies) so
        # bulk note injection can't overflow context. Agents fetch one body via
        # read_note(slug). Pass bodies=true to opt back into full rows.
        # 9fa119dd — cursor pagination, opt-in (mirrors get_sprint_items, whose
        # MCP tool stays a bare list while the HTTP route paginates): pass
        # ``cursor`` and/or ``limit`` to get the {notes, has_more, next_cursor}
        # envelope, then re-call with cursor=next_cursor for the next page.
        # Without either arg the legacy bare list is returned for back-compat.
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
        return await db_module.add_workspace_note(
            db, args["title"], args["body"], args.get("tags"),
            tenant_id=_mcp_tenant_id,
        )
    if name == "get_workspace_notes":
        return await db_module.get_workspace_notes(
            db, tag=args.get("tag"), tenant_id=_mcp_tenant_id,
        )
    if name == "pin_workspace_decision":
        validate_input_size(args.get("title"), "decision title", 500)
        validate_input_size(args.get("body"), "decision body", 100_000)
        return await db_module.pin_workspace_decision(
            db, args["title"], args["body"],
            category=args.get("category", "TECHNICAL"),
            tenant_id=_mcp_tenant_id,
        )
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
            tenant_id=_mcp_tenant_id,
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
        start_fresh = f'start_session(project_id="{project_id}", session_name="describe-what-youre-doing")'
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
        for k in ("repo_path", "env_file", "test_cmd", "test_min",
                  "deploy_cmd", "shell_type", "branch"):
            if k in args:
                cfg[k] = args[k]
        if "repo_paths" in args:
            cfg["repo_paths"] = merge_repo_paths(cfg.get("repo_paths"), args["repo_paths"])
        return await db_module.set_executor_config(db, args["project_id"], cfg)
    if name == "idle_until_session_done":
        return await _server._idle_until_session_done(db, args["watching_session_id"])
    if name == "search_all":
        return await db_module.search_all(
            db, args["project_id"], args["query"],
            limit=args.get("limit", 10),
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
            f'</session_brief>'
        )
        return {"text": brief, "project_id": project_id, "role": role}
    return _MISS


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
            db, args["session_id"], args["title"], args["body"]
        )
    if name == "get_sprint_notes":
        return await db_module.get_session_notes(db, args["session_id"])
    if name == "add_sprint_item":
        validate_input_size(args.get("title"), "sprint item title", 500)
        # fd86aacc — warn if active executor sessions exist when adding a new item
        _active_session_warnings: list[str] = []
        try:
            from datetime import datetime, timezone as _tz
            _active_sessions = await db_module.get_sessions(db, args["project_id"])
            _now_ts = datetime.now(_tz.utc)
            for _sess in _active_sessions:
                _ls = _sess.get("last_seen")
                if _ls:
                    try:
                        _ls_dt = datetime.fromisoformat(str(_ls).replace("Z", "+00:00"))
                        if _ls_dt.tzinfo is None:
                            _ls_dt = _ls_dt.replace(tzinfo=_tz.utc)
                        if (_now_ts - _ls_dt).total_seconds() < 600:
                            _active_session_warnings.append(
                                f"session '{_sess.get('name', _sess.get('id','?'))}' is active"
                            )
                    except Exception:
                        pass
        except Exception:
            pass
        _new_item = await db_module.add_sprint_item(
            db, args["project_id"], args["version"], args["title"],
            group=args.get("group"),
            human_id=args.get("human_id"),
            depends_on=args.get("depends_on"),
            failure_mode=args.get("failure_mode"),
            milestone_type=args.get("milestone_type", "task"),
            force=bool(args.get("force", False)),
        )
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
        if _extra:
            _new_item = {**_new_item, **_extra}
        return _new_item
    if name == "fan_out_sprint_items":
        items = args.get("items")
        if not isinstance(items, list) or not items:
            return {"error": "items must be a non-empty list of {title, ...} dicts"}
        for spec in items:
            validate_input_size(spec.get("title"), "sprint item title", 500)
            validate_input_size(spec.get("description"), "sprint item description", 10_000)
        ids = await db_module.fan_out_sprint_items(
            db, args["project_id"], items
        )
        return {"item_ids": ids, "count": len(ids)}
    if name == "update_sprint_item":
        validate_input_size(args.get("title"), "sprint item title", 500)
        validate_input_size(args.get("notes"), "sprint item notes", 50_000)
        item = await db_module.patch_sprint_item(
            db, args["project_id"], args["item_id"],
            title=args.get("title"),
            version=args.get("version"),
            notes=args.get("notes"),
            human_id=args.get("human_id"),
            item_group=args.get("group"),
        )
        return item or {"error": "sprint item not found"}
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
            "items": [
                {"id": it["id"], "title": (it.get("title") or "")[:80], "status": it.get("status")}
                for it in _all
            ],
        }
        _bc = await _board_change_for_session(db, args["project_id"], args.get("session_id"))
        if _bc:
            _resp_progress["board_change"] = _bc
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
            item = await db_module.claim_sprint_item(db, args["project_id"], args["item_id"])
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
            raise
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

        return item
    if name == "add_subtask":
        return await db_module.add_subtask(
            db, args["project_id"], args["parent_id"], args["title"]
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

        item = await db_module.complete_sprint_item(
            db, args["project_id"], args["item_id"],
            task_id=args.get("task_id"),
        )
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
    return _MISS


async def _handle_file_claims(
    name: str,
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """Dispatch group: claim_file, get_file_claims, get_symbol_claims, get_symbol_hotspots, release_file."""
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
        return await db_module.claim_file(
            db, args["file_path"], args["session_id"], symbol=_symbol
        )
    if name == "get_file_claims":
        return await db_module.get_file_claims(
            db, args["file_path"], args.get("project_id"), args.get("symbol")
        )
    if name == "get_symbol_claims":
        return {"claims": await db_module.get_symbol_claims(db, args["file_path"])}
    if name == "get_symbol_hotspots":
        return {"hotspots": await db_module.get_symbol_hotspots(
            db, args.get("file_path"),
            min_sessions=args.get("min_sessions", 3),
            days=args.get("days", 14),
        )}
    if name == "release_file":
        released = await db_module.release_file(db, args["file_path"], args["session_id"])
        return {"released": released, "file_path": args["file_path"]}
    return _MISS


async def _handle_planning_tools(
    name: str,
    args: dict[str, Any],
    db: Any,
    data_dir: str,
    tenant: dict[str, Any] | None,
    _mcp_tenant_id: Any,
) -> Any:
    """Dispatch group: reconcile_sprint_drift, get_planning_brief."""
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
            "pending_hitls": [
                {
                    "id": h.get("id"),
                    "question": (h.get("question") or "")[:120],
                    "urgency": h.get("urgency"),
                }
                for h in (hitls if isinstance(hitls, list) else [])[:5]
            ],
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
    )
    for _grp in _groups:
        _result = await _grp(name, args, db, data_dir, tenant, _mcp_tenant_id)
        if _result is not _MISS:
            return _result
    raise ValueError(f"unknown tool: {name}")


_MCP_RATE_LIMIT = "100/minute"
