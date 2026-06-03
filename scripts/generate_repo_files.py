"""generate_repo_files.py — sync DEVLOG.md, DECISIONS.md, ROADMAP.md from DB.

Reads the meridian-build project DB and regenerates three files from live state:

  DEVLOG.md    — last 500 task_log rows, newest first (append-only style)
  DECISIONS.md — active decisions_pinned rows, grouped by category
  ROADMAP.md   — pending sprint items grouped by version (appends new sections only)

Run manually before a release or after a large batch of work:
    pixi run generate-files

NEVER run automatically on server start — these are human-audited documents.
ROADMAP.md is append-only: existing version sections are preserved and new ones
are appended. DEVLOG.md and DECISIONS.md are fully regenerated from DB state.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
DEFAULT_DB = ROOT / "data" / "meridian.db"
MERIDIAN_BUILD_PROJECT = "5787cc92-ba7d-4788-b17c-28ab7938b839"


def _db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# DEVLOG.md — last 500 task_log rows, newest first
# ---------------------------------------------------------------------------

def generate_devlog(conn: sqlite3.Connection, project_id: str) -> str:
    rows = conn.execute(
        "SELECT t.description, t.status, t.created_at, "
        "s.name AS session_name, s.human_id "
        "FROM task_log t "
        "LEFT JOIN sessions s ON s.id = t.session_id "
        "WHERE t.project_id = ? "
        "AND t.description NOT LIKE '%GOAL.md conflict%' "
        "AND t.description NOT LIKE '%skipped sync%' "
        "ORDER BY t.created_at DESC, t.rowid DESC LIMIT 500",
        (project_id,),
    ).fetchall()

    lines = [
        "# Dev Log\n",
        "_Auto-generated from task_log. Do not edit — run `pixi run generate-files` to refresh._\n",
        "",
    ]
    for r in rows:
        ts = (r["created_at"] or "")[:16]
        status = r["status"] or "done"
        actor = r["session_name"] or ""
        if r["human_id"]:
            actor = f"{r['human_id']}/{actor}" if actor else r["human_id"]
        status_icon = {"done": "✓", "failed": "✗", "pending": "·", "in_progress": "▶"}.get(status, "·")
        desc = (r["description"] or "").replace("\n", " ").strip()
        lines.append(f"- `{ts}` **{status_icon}** {desc}  _(_{actor}_)_")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# DECISIONS.md — active decisions_pinned, grouped by category
# ---------------------------------------------------------------------------

def generate_decisions(conn: sqlite3.Connection, project_id: str) -> str:
    # Try decisions_pinned table (v2.4+)
    try:
        rows = conn.execute(
            "SELECT title, body, category, created_at "
            "FROM decisions_pinned "
            "WHERE project_id = ? AND status = 'active' "
            "ORDER BY category ASC, created_at DESC",
            (project_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []

    lines = [
        "# Decisions\n",
        "_Auto-generated from decisions_pinned. Use `pin_decision` MCP tool to add entries._\n",
        "",
    ]

    if not rows:
        lines.append("_(no active decisions yet)_\n")
        return "\n".join(lines)

    # Group by category
    by_cat: dict[str, list] = {}
    for r in rows:
        cat = r["category"] or "TECHNICAL"
        by_cat.setdefault(cat, []).append(r)

    for cat, items in sorted(by_cat.items()):
        lines.append(f"\n## {cat}\n")
        for r in items:
            ts = (r["created_at"] or "")[:10]
            lines.append(f"### {r['title']}  _{ts}_")
            lines.append("")
            lines.append((r["body"] or "").strip())
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ROADMAP.md — pending sprint items grouped by version
# ---------------------------------------------------------------------------

def generate_roadmap(conn: sqlite3.Connection, project_id: str, existing: str) -> str:
    """Append-only: preserve existing content, append new version sections."""
    rows = conn.execute(
        "SELECT version, title, status, item_group, added_at "
        "FROM sprint_items "
        "WHERE project_id = ? AND status IN ('pending','todo','in_progress','pushed') "
        "ORDER BY version ASC, added_at ASC",
        (project_id,),
    ).fetchall()

    # Group by version
    by_version: dict[str, list] = {}
    for r in rows:
        v = r["version"] or "backlog"
        by_version.setdefault(v, []).append(r)

    if not by_version:
        return existing  # nothing to append

    # Detect which version sections already exist in the file
    existing_sections = set()
    for line in existing.splitlines():
        if line.startswith("## "):
            existing_sections.add(line[3:].strip())

    # Append new version sections only
    new_sections = []
    for version, items in by_version.items():
        if version in existing_sections:
            continue
        section = [f"\n## {version}\n"]
        for r in items:
            status_tag = "" if r["status"] == "pending" else f" _{r['status']}_"
            group = f" `{r['item_group']}`" if r["item_group"] else ""
            section.append(f"- [ ] {r['title']}{group}{status_tag}")
        new_sections.append("\n".join(section))

    if not new_sections:
        return existing  # all versions already documented

    return existing.rstrip() + "\n" + "\n".join(new_sections) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB),
                        help="Path to SQLite DB (default: data/meridian.db)")
    parser.add_argument("--project-id", default=MERIDIAN_BUILD_PROJECT,
                        help="Project ID to generate files for")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print output instead of writing files")
    parser.add_argument("--only", choices=["devlog", "decisions", "roadmap"],
                        help="Generate only one file")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB not found at {db_path}. Use --db to specify a path.", file=sys.stderr)
        sys.exit(1)

    conn = _db(db_path)

    def _write(path: Path, content: str, label: str) -> None:
        if args.dry_run:
            preview = content[:2000].encode("ascii", "replace").decode("ascii")
            print(f"\n{'='*60}\n{label}\n{'='*60}\n{preview}")
        else:
            path.write_text(content, encoding="utf-8")
            print(f"OK {label} -> {path.relative_to(ROOT)}")

    pid = args.project_id

    if not args.only or args.only == "devlog":
        _write(ROOT / "DEVLOG.md", generate_devlog(conn, pid), "DEVLOG.md")

    if not args.only or args.only == "decisions":
        _write(ROOT / "DECISIONS.md", generate_decisions(conn, pid), "DECISIONS.md")

    if not args.only or args.only == "roadmap":
        existing = (ROOT / "ROADMAP.md").read_text(encoding="utf-8") if (ROOT / "ROADMAP.md").exists() else "# Roadmap\n\n"
        _write(ROOT / "ROADMAP.md", generate_roadmap(conn, pid, existing), "ROADMAP.md")

    conn.close()


if __name__ == "__main__":
    main()
