# scripts/

One-off maintenance and migration scripts. Unlike `tmp/` (gitignored scratch space),
these are committed because they document a deliberate, reproducible change.

| Script | Purpose | Date | Safe to delete after |
|--------|---------|------|----------------------|

Guidelines:
- Scripts here should be self-documenting (module docstring stating what, why, when).
- Never commit credentials, tokens, or connection strings. Reference env var names only.
- Delete entries from this table when their script is removed.
