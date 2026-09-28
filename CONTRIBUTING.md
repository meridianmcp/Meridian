# Contributing to Meridian

Thanks for looking. Meridian is built with Meridian — every sprint item in this repo was tracked via its own MCP tools. If you contribute, you can do the same.

## Local setup

**Requirements:** Python 3.11–3.12, [pixi](https://pixi.sh)

```bash
git clone https://github.com/meridianmcp/Meridian
cd Meridian
cp .env.local.example .env.local   # fill in test credentials (optional)
pixi run start                      # installs deps, starts server at localhost:7878
```

Open `http://localhost:7878` — you should see the dashboard.

## Running tests

```bash
pixi run test          # full suite (535+ expected)
pixi run test -k foo   # single test filter
```

All tests must pass before pushing. No exceptions.

## Dev rules

- **Push to `dev`, not `main`.** `main` triggers a production deploy. Work on a feature branch or push to `dev` and PR.
- **Read before editing.** Key files are large. Always `Read` the relevant file before making changes.
- **Surgical edits.** Don't rewrite whole files — find the exact location and edit just what needs changing.
- **Commit often.** One logical unit per commit. Format: `feat: description` / `fix: description`.
- **pixi run test passes** before every push. If you break tests, fix them before pushing.

## Tagging and CI conventions

Meridian has two separate tag patterns with different CI effects:

| Tag pattern | Example | What it triggers |
|-------------|---------|-----------------|
| Exact semver `vX.Y.Z` | `v1.2.0` | Tests → deploy-preview → **production deploy** + **binary builds** (Windows / Linux / mac-arm64) + GitHub Release |
| Pre-release suffix | `v1.2.0-alpha`, `v1.2.0-rc1` | Tests → deploy-preview only — **no binary builds, no GitHub Release** |

**Use pre-release tags for CI/deploy-only pushes.** Binary builds on Windows runners are slow (~10–15 min). Burning a build slot for a one-line config fix is wasteful. Reserve bare `vX.Y.Z` tags for actual user-facing releases.

Binary builds can also be triggered manually via GitHub Actions → **Build & Release Binaries** → Run workflow.

## psycopg3 rules (non-negotiable)

The DB layer uses psycopg3, not asyncpg or sqlite3 directly. These rules prevent silent bugs:

| Rule | Correct | Wrong |
|------|---------|-------|
| Placeholders | `%s` | `?` |
| LIKE patterns | `%%foo%%` | `%foo%` |
| Pool usage | `async with self._pool.connection() as conn:` | don't hold connections manually |
| Fetch result | `row["col_name"]` | `row[0]` |
| Commit | never call `conn.commit()` — autocommit=True | `await conn.commit()` |

Violations cause silent data corruption or crashes on Postgres. The adapter converts `?` → `%s` for SQLite compatibility but not the reverse.

## Key files

| File | What it is |
|------|------------|
| `meridian/server.py` | FastAPI app, all HTTP routes, MCP tool dispatch |
| `meridian/db/__init__.py` | All DB operations, SQLite + Postgres compatible |
| `meridian/pg_adapter.py` | psycopg3 pool wrapper — read before writing SQL |
| `meridian/hosted.py` | OAuth, Stripe, tenant routing |
| `meridian/routes/decisions.py` | Decisions CRUD — example of route module pattern |
| `meridian/static/dashboard.js` | All frontend JS — single file |
| `meridian/static/dashboard.css` | CSS variables + component styles |
| `meridian/handoff.py` | Handoff generation (L0/L1/L2) |
| `tests/test_core.py` | Full test suite (~540 tests) |

## Local Linux verification via WSL (a70e6a33)

Manual/interactive verification of Linux desktop-integration work -- tray
icons (pystray backend availability), `.desktop` file validity, `systemd
--user` units -- that a headless CI runner can't meaningfully exercise.
**This is not a substitute for CI's own Linux runners**: `test-core`/
`test-postgres` already run the full suite on `ubuntu-latest` in GitHub
Actions on every push. This is for the narrower, genuinely-manual slice CI
can't cover.

**Two separate WSL distros -- don't confuse them:**

| Distro | Purpose |
|--------|---------|
| `Ubuntu` | The daily-driver WSL environment. Do not repurpose it for verification runs -- confirmed live that running the wrong distro leaves real, unwanted side effects (a stray clone + `pixi install`) in its `$HOME`. |
| `Ubuntu-20.04` | Dedicated, disposable Meridian test environment with its own checkout at `~/meridian-test` (set up during the 2026-09-22 clean-room validation, pinned decision b6a8f317). |

**Run it**, from a Windows shell at the repo root:

```powershell
wsl.exe -d Ubuntu-20.04 -- bash scripts/wsl-linux-check.sh
# with a targeted test subset afterwards:
wsl.exe -d Ubuntu-20.04 -- bash scripts/wsl-linux-check.sh tests/test_install_linux_launcher.py
```

Always pass `-d Ubuntu-20.04` explicitly. Do not rely on a bare `bash` command
resolving to the right place: confirmed live that a bare `bash` invoked from
a Windows process can resolve to `%SystemRoot%\System32\bash.exe` (the
legacy WSL launcher, which opens the *default* distro) even when
`shutil.which("bash")`/`$PATH` would point at Git for Windows' own bash --
Windows' process-creation search checks the fixed system directories before
consulting `PATH` at all. `wsl.exe -d Ubuntu-20.04 --` sidesteps this
ambiguity entirely by naming the distro directly.

**Why this needs a script at all:** `~/meridian-test` is a `--depth 1`
shallow clone that only tracks `main`, so `git fetch origin dev` alone does
**not** create an `origin/dev` ref, and a plain `git checkout -b x
origin/dev` fails outright. `scripts/wsl-linux-check.sh` fixes this at the
root (`git remote set-branches --add origin dev` before fetching, so
`origin/dev` becomes a real, persistent tracking ref) instead of the
one-off `git checkout -B x FETCH_HEAD` workaround that has to be
rediscovered by hand every time. It also works around a second, separately
-confirmed gotcha: pixi's `PATH` export lives in `~/.bashrc`, but Ubuntu's
default `~/.bashrc` returns immediately for non-interactive shells --
exactly how `wsl.exe -- bash script.sh` invokes it -- so the script sets
`PATH` itself rather than trusting shell rc files. See the script's own
header comment for the full detail on both, plus the safety behavior around
uncommitted local changes (`MERIDIAN_WSL_FORCE=1` to override) and the
available env var overrides. Regression tests: `tests/test_wsl_linux_check.py`.

## Adding a new MCP tool

1. **Add the tool definition** to `_MCP_TOOLS_LIST` in `meridian/server.py` — name, description, inputSchema.
2. **Add an example** to `TOOL_EXAMPLES` dict in `server.py`.
3. **Add the handler** in the `if name == "..."` dispatch block (search for `if name == "pin_decision"` to find the pattern).
4. **Add a DB function** in `meridian/db/__init__.py` if needed.
5. **Write tests** in `tests/test_core.py` — at minimum one HTTP round-trip test and one MCP dispatch test.
6. **Regenerate the docs**: `pixi run python -c "import asyncio; from meridian import server as s; open('docs/mcp-tools.md','w').write(asyncio.run(s.mcp_tools_doc()))"`

The test `test_docs_mcp_tools_matches_live_tool_doc` will fail if you add a tool without regenerating docs.

## Dogfood your contributions

Meridian tracks its own development. If you're making a significant contribution:

```python
# In Claude Code / Cursor connected to a Meridian MCP server:
start_session(project_id="<your_project_id>", session_name="feature-x")
log_task(session_id="...", project_id="...", description="Implemented X")
checkpoint(session_id="...", project_id="...")
```

You don't have to use the Meridian project's own MCP server — spin up your own local instance and use it to track your PR work. It's the best way to understand the product.

## What makes a good PR

- Fixes one thing or adds one coherent feature
- Has tests (existing or new)
- Doesn't break the test suite
- Includes a description of what you tested and how
- References any related issue or sprint item if applicable

## Questions

Open a GitHub issue. Or post in the HN thread — I check it.
