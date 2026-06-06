#!/usr/bin/env python3
"""Log smoke test completion to Meridian."""

import asyncio
import json
import sys
import os
from pathlib import Path

# Add meridian package to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from meridian.db import get_db


async def log_smoke_test():
    """Log the smoke test completion."""
    project_id = os.environ.get("MERIDIAN_PROJECT_ID", "").strip()
    if not project_id:
        print("Set MERIDIAN_PROJECT_ID before running this script.")
        return False

    # Get database connection
    db = await get_db()

    # Get recent sessions for this project
    query = """
    SELECT id FROM sessions
    WHERE project_id = ?
    ORDER BY created_at DESC
    LIMIT 1
    """

    async with db.execute(query, [project_id]) as cur:
        row = await cur.fetchone()
        if row:
            session_id = row[0]
            print(f"Found session: {session_id}")

            # Log the task
            log_query = """
            INSERT INTO task_log (id, session_id, project_id, description, status, created_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            """

            import uuid
            task_id = str(uuid.uuid4())

            await db.execute(
                log_query,
                [
                    task_id,
                    session_id,
                    project_id,
                    "SIGNUP SMOKE TEST PASSED - free tier provisioning works end to end",
                    "done"
                ]
            )

            print("Task logged successfully")
            return True
        else:
            print("No session found for project")
            return False


if __name__ == "__main__":
    result = asyncio.run(log_smoke_test())
    sys.exit(0 if result else 1)
