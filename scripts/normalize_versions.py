"""Normalize sprint_items version labels on local SQLite DB.

Run: pixi run python scripts/normalize_versions.py
"""
import sqlite3
import os
import sys

db_path = os.path.join(os.path.dirname(__file__), "..", "data", "meridian.db")
db_path = os.path.normpath(db_path)

if not os.path.exists(db_path):
    print(f"DB not found at {db_path}")
    sys.exit(1)

conn = sqlite3.connect(db_path)
cur = conn.cursor()

# Show current distinct versions
cur.execute("SELECT DISTINCT version FROM sprint_items ORDER BY version")
print("Current versions:", [r[0] for r in cur.fetchall()])

# Normalize pre-launch era → alpha-launch
cur.execute(
    "UPDATE sprint_items SET version='alpha-launch' WHERE version IN ('pre-launch', 'v1.0')"
)
n1 = conn.total_changes
print(f"  -> alpha-launch: {n1} rows updated")

# Normalize post-launch labels
cur.execute(
    "UPDATE sprint_items SET version='post-launch' WHERE version IN ('v1.1', 'v2.0', 'v1.x')"
)
n2 = conn.total_changes - n1
print(f"  -> post-launch: {n2} rows updated")

conn.commit()

# Show updated versions
cur.execute("SELECT DISTINCT version FROM sprint_items ORDER BY version")
print("Updated versions:", [r[0] for r in cur.fetchall()])

# Update version_goal in goal_states if it contains stale version labels
cur.execute("SELECT id, content FROM goal_states")
rows = cur.fetchall()
for row_id, content in rows:
    if content and ("v1.9.x" in content or "MERIDIAN v1.9" in content):
        new_content = content.replace("v1.9.x", "v1.0.0-alpha").replace(
            "MERIDIAN v1.9", "MERIDIAN v1.0.0-alpha"
        )
        cur.execute("UPDATE goal_states SET content=? WHERE id=?", (new_content, row_id))
        print(f"Updated goal_states row {row_id[:8]}: replaced stale version labels")

conn.commit()
conn.close()
print("Done.")
