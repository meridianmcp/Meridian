"""Clean GOAL.md sync-skip noise from task_log.

These rows are created by goal_md.py when the local GOAL.md file is older
than the DB version and the sync is skipped. They fill the devlog with noise
that doesn't represent real work.

Usage:
    pixi run python scripts/clean_goal_md_noise.py [--dry-run] [--db PATH]

Defaults to data/meridian.db in the repo root. Pass --db to override.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be deleted without actually deleting")
    parser.add_argument("--db", default=str(ROOT / "data" / "meridian.db"),
                        help="Path to SQLite DB (default: data/meridian.db)")
    parser.add_argument("--project-id", default=None,
                        help="Limit deletion to a specific project ID")
    args = parser.parse_args()

    import sqlite3
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB not found at {db_path}. Use --db to specify a path.", file=sys.stderr)
        sys.exit(1)

    noise_patterns = [
        "%GOAL.md conflict%",
        "%skipped sync%",
        "%file is older than DB%",
        "%file_mtime < db_updated_at%",
    ]

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Count rows that match
    where_clauses = " OR ".join(f"description LIKE ?" for _ in noise_patterns)
    params: list = list(noise_patterns)

    if args.project_id:
        where_clauses = f"project_id = ? AND ({where_clauses})"
        params = [args.project_id] + params

    count_sql = f"SELECT COUNT(*) AS n FROM task_log WHERE {where_clauses}"
    row = cur.execute(count_sql, params).fetchone()
    n = row["n"] if row else 0

    if n == 0:
        print("No GOAL.md noise rows found — already clean.")
        conn.close()
        return

    if args.dry_run:
        print(f"[dry-run] Would delete {n} GOAL.md noise row(s)")
        # Show sample
        sample = cur.execute(
            f"SELECT id, created_at, description FROM task_log WHERE {where_clauses} LIMIT 5",
            params,
        ).fetchall()
        for r in sample:
            print(f"  [{r['created_at']}] {r['description'][:100]}")
        conn.close()
        return

    delete_sql = f"DELETE FROM task_log WHERE {where_clauses}"
    cur.execute(delete_sql, params)
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    print(f"Deleted {deleted} GOAL.md noise row(s) from task_log.")


if __name__ == "__main__":
    main()
