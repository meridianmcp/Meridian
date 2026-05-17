"""Render the handoff markdown file for a Meridian project.

The handoff file is the bridge between sessions: a new Claude session reads
it and resumes work with full context. The template lives in
``meridian/templates/handoff.md.j2`` and is rendered with Jinja2.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import db as db_module

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(disabled_extensions=("md", "j2")),
    keep_trailing_newline=True,
)


def _slugify(name: str) -> str:
    """Turn a project name into a safe filename fragment."""
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-")
    return slug or "project"


def _format_content(content) -> str:
    """Pretty-print goal content for the handoff body."""
    if isinstance(content, str):
        return content
    return json.dumps(content, indent=2)


async def generate_handoff(
    db: aiosqlite.Connection,
    project_id: str,
    output_dir: str,
    *,
    summarizer: object | None = None,
) -> tuple[str, str]:
    """Fetch all state, render the template, write the file, return both.

    Returns ``(path, content)`` where ``path`` is the absolute path to the
    rendered file on disk.

    v1.2.1 — before rendering, summarise any session that shipped >=3
    tasks via :func:`db_module.summarize_session`. The summary lands
    in the ``session_summary`` column and is included in the timeline
    response so dashboard tooltips render. Pass an explicit
    ``summarizer`` to inject a stub during tests; defaults to the
    haiku-backed implementation in db.py.
    """
    project = await db_module.get_project(db, project_id)
    if project is None:
        raise ValueError(f"project not found: {project_id}")

    goal = await db_module.get_goal(db, project_id)
    if goal is None:
        goal = {
            "version": 0,
            "content": "(no goal set yet)",
        }
    else:
        goal = {**goal, "content": _format_content(goal["content"])}

    sessions = await db_module.get_sessions(db, project_id, active_only=False)
    # v1.2.1 — summarise eligible sessions before reading tasks so
    # the rendered handoff can reference fresh session_summary
    # values when the template wants them.
    for s in sessions:
        try:
            await db_module.summarize_session(
                db, s["id"], summarizer=summarizer
            )
        except Exception:  # noqa: BLE001 — never break handoff on summary fail
            continue
    tasks = await db_module.get_tasks(db, project_id, limit=50)

    session_names = {s["id"]: s["name"] for s in sessions}
    # Defensive: if a task references a session that's gone, label it clearly.
    for t in tasks:
        session_names.setdefault(t["session_id"], "(unknown-session)")

    template = _env.get_template("handoff.md.j2")
    content = template.render(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        project=project,
        goal=goal,
        sessions=sessions,
        tasks=tasks,
        session_names=session_names,
    )

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{_slugify(project['name'])}_handoff.md"
    out_path.write_text(content, encoding="utf-8")
    return str(out_path.resolve()), content
