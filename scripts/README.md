# scripts/

One-off maintenance and migration scripts. Unlike `tmp/` (gitignored scratch space),
these are committed because they document a deliberate, reproducible change.

| Script | Purpose | Date | Safe to delete after |
|--------|---------|------|----------------------|
| `fix_claude_json.py` | Remove stale meridian Bearer token from `~/.claude.json` (one-time local fix). | 2026-06-13 | 2026-07-01 |

Guidelines:
- Scripts here should be self-documenting (module docstring stating what, why, when).
- Never commit credentials, tokens, or connection strings. Reference env var names only.
- Delete entries from this table when their script is removed.
