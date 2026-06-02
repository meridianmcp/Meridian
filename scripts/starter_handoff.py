"""Print a ≤20-line starter block for paste-after-/compact or cold start.

Usage:
    pixi run starter                         # uses local meridian.db
    pixi run python scripts/starter_handoff.py --project-id <uuid>

Output is printed to stdout — paste it as the first message in a new session.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Allow running from project root without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def main(project_id: str | None) -> None:
    from meridian import db as db_module
    from meridian.handoff import _generate_starter_handoff

    db_path = os.environ.get("MERIDIAN_DB", str(
        Path(__file__).resolve().parent.parent / "data" / "meridian.db"
    ))
    if db_path.startswith(("postgresql://", "postgres://")):
        from meridian.pg_adapter import init_pg_db
        db = await init_pg_db(db_path)
    else:
        import aiosqlite
        db = await aiosqlite.connect(db_path)
        db.row_factory = aiosqlite.Row
        await db_module.init_db(db_path)

    try:
        if project_id:
            project = await db_module.get_project(db, project_id)
            if not project:
                print(f"ERROR: project {project_id!r} not found", file=sys.stderr)
                sys.exit(1)
        else:
            # Pick the first project
            projects = await db_module.list_projects(db)
            if not projects:
                print("ERROR: no projects found", file=sys.stderr)
                sys.exit(1)
            if len(projects) > 1:
                names = ", ".join(f"{p['name']} ({p['id'][:8]})" for p in projects)
                print(f"Multiple projects — pass --project-id. Found: {names}", file=sys.stderr)
                sys.exit(1)
            project = projects[0]

        data_dir = str(Path(__file__).resolve().parent.parent / "data")
        _, content = await _generate_starter_handoff(db, project, data_dir)
        print(content)
    finally:
        if hasattr(db, "close"):
            await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Print starter handoff block")
    parser.add_argument("--project-id", help="Project UUID (optional if only one project)")
    args = parser.parse_args()
    asyncio.run(main(args.project_id))
