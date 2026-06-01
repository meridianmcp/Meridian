#!/usr/bin/env python3
"""Waitlist DB integrity check — 0fe7544c.

Verifies the waitlist table against the live auth DB.
Run the morning of HN launch to confirm the list is clean and ready.

Usage:
    pixi run python scripts/test_waitlist_db.py [--db data/meridian.db]
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import uuid
from pathlib import Path

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"

EMAIL_RE = re.compile(r"^[^@]+@[^@]+\.[^@]+$")


def check(label: str, ok: bool, detail: str = "") -> bool:
    status = PASS if ok else FAIL
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    return ok


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/meridian.db", help="SQLite DB path (or MERIDIAN_DB env var)")
    args = parser.parse_args()

    import os
    db_path = os.environ.get("MERIDIAN_DB", args.db)

    if db_path.startswith(("postgresql://", "postgres://")):
        print("Postgres DB — using psycopg3 via meridian pg_adapter")
        from meridian.pg_adapter import open_pg_connection  # noqa: PLC0415
        db = await open_pg_connection(db_path)
    else:
        if not Path(db_path).exists():
            print(f"\n[{SKIP}] DB not found at {db_path} — skipping (run `pixi run start` first)\n")
            return 0
        import aiosqlite  # noqa: PLC0415
        db = await aiosqlite.connect(db_path)
        db.row_factory = aiosqlite.Row

    failures = 0
    print(f"\nWaitlist DB integrity check — {db_path}\n")

    # 1. Count total waitlist rows
    async with db.execute("SELECT COUNT(*) FROM waitlist") as cur:
        row = await cur.fetchone()
    total = row[0] if row else 0
    check(f"Waitlist row count", True, f"{total} entries")

    # 2. No duplicate emails
    async with db.execute("SELECT email, COUNT(*) as cnt FROM waitlist GROUP BY email HAVING cnt > 1") as cur:
        dupes = await cur.fetchall()
    ok = len(dupes) == 0
    if not check("No duplicate emails", ok, f"{len(dupes)} duplicate(s)" if not ok else "clean"):
        failures += 1

    # 3. All emails valid format
    async with db.execute("SELECT email FROM waitlist") as cur:
        all_emails = [r[0] for r in await cur.fetchall()]
    bad_emails = [e for e in all_emails if not EMAIL_RE.match(e or "")]
    ok = len(bad_emails) == 0
    if not check("All emails valid format", ok, f"bad: {bad_emails[:3]}" if not ok else "all valid"):
        failures += 1

    # 4. All rows have created_at
    async with db.execute("SELECT COUNT(*) FROM waitlist WHERE created_at IS NULL OR created_at = ''") as cur:
        row = await cur.fetchone()
    missing_ts = row[0] if row else 0
    ok = missing_ts == 0
    if not check("All rows have created_at", ok, f"{missing_ts} missing"):
        failures += 1

    # 5. Insert test row, verify it appears, delete it
    test_email = f"test-wl-check-{uuid.uuid4().hex[:8]}@meridian-test.invalid"
    test_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO waitlist (id, email, note) VALUES (?, ?, ?)",
        (test_id, test_email, "test-run"),
    )
    await db.commit()
    async with db.execute("SELECT id FROM waitlist WHERE id = ?", (test_id,)) as cur:
        found = await cur.fetchone()
    ok = found is not None
    if not check("Test row inserted + found", ok):
        failures += 1
    await db.execute("DELETE FROM waitlist WHERE id = ?", (test_id,))
    await db.commit()
    async with db.execute("SELECT id FROM waitlist WHERE id = ?", (test_id,)) as cur:
        gone = await cur.fetchone()
    ok = gone is None
    if not check("Test row deleted cleanly", ok):
        failures += 1

    # 6. Schema columns check
    async with db.execute("PRAGMA table_info(waitlist)") as cur:
        cols = [r[1] for r in await cur.fetchall()]
    required = {"id", "email", "created_at"}
    missing_cols = required - set(cols)
    ok = len(missing_cols) == 0
    if not check("waitlist schema has required columns", ok, f"missing: {missing_cols}" if not ok else str(cols)):
        failures += 1

    await db.close()

    print(f"\n{'All checks passed.' if failures == 0 else f'{failures} check(s) FAILED.'}\n")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
