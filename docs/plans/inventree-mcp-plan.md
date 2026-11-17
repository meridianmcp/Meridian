# InvenTree MCP server: read and write, with approval

> `extensions/meridian-inventree/README.md` is the source of truth for what is built. This file is the original plan. Where the two differ, the README is right.

Status: built. The read tools, plan store, terminal approval (`approve_cli.py`), audit file and `run_plan` are in the package. Two things are still open: writes are refused until the owner sets the supported version range, and `run_plan` has no command-line runner yet. The original request was a read/write MCP server for InvenTree, with writes done by a subagent swarm after planning.

## What it is

A standalone package, `extensions/meridian-inventree`, in the same style as `extensions/meridian-outputs`. It is an MCP server that talks to one InvenTree instance over its REST API. It is not part of the core server, so it can be installed and removed on its own.

## Connection

- Base URL from `INVENTREE_URL`. Writes are refused for any other host.
- Token from `INVENTREE_TOKEN` (env only in v1). Never written to a file in the repo, never logged. Desktop secret-store integration comes later.
- Auth header: `Authorization: Token <token>`.
- Version check on start: read `GET /api/` and refuse writes if the server version is not in the supported range.
- Permissions follow the token user's role. The server reads `GET /api/user/roles/` and reports what it can do.

## Read tools (no approval needed)

- `inventree_search_parts(query, category?, limit)`: `GET /api/part/` with `search=`.
- `inventree_get_part(pk)`
- `inventree_list_categories(parent?)`: `GET /api/part/category/`.
- `inventree_search_stock(query?, location?, cascade?)`: `GET /api/stock/`, with `cascade` to include child locations.
- `inventree_get_stock_item(pk)`
- `inventree_list_locations(parent?)`: `GET /api/stock/location/`.
- `inventree_roles()`: `GET /api/user/roles/`.

Read tools are paginated and capped in size. Results are truncated with a note, never silently.

## Write tools (approval required)

Writes are never executed by the tool call that proposes them.

1. `inventree_propose(operations[])`: validates each operation, fetches the current state of every object it would change, and stores a plan with a preview (before and after for each field). Returns a `plan_id`. Nothing is written.
2. Approval: the owner approves the plan in a terminal with `python -m meridian_inventree.approve_cli`. The command shows each operation and the plan hash, and the owner types the hash to approve. No MCP tool approves a plan, and an agent cannot pass an approval flag.
3. Running: there is no execute tool. The owner runs an approved plan from Python with `write_tools.run_plan`. It runs only if the plan is approved, unchanged since approval (hash check), and not already run. It runs operations in order and stops on the first failure, then reports what was done.

Write operations in v1:
- `create_part`, `update_part`
- `create_location`
- `create_stock_item`, `transfer_stock`, `adjust_stock_quantity`

Not in v1: any delete, any user or permission change, any bulk import from a file. These need a separate plan.

## Safety rules

- Every write op is recorded before and after it runs, in an append-only audit file under the Meridian state folder. There is no task-log mirror.
- Idempotency: each op carries a client key. A retry of the same key is a no-op after the first success.
- Batch limit: a plan holds at most 50 operations. Larger work is split into plans.
- Propose is a dry run: it stores a plan and writes nothing. There is no `confirm` flag. Approval in the terminal is the gate before a run.
- Stock quantity changes never go below zero, and a transfer must name a real destination.
- Subagents may propose freely. Only an approved plan can run. The swarm's job is to produce plans, not to write.

## Testing

- Unit tests against a fake HTTP transport. No network in the test suite.
- Contract fixtures recorded from the pinned InvenTree version's `/api-doc/`. Create and update request bodies are unverified until checked against the live schema, so the first build step is to fetch and pin them.
- An optional live test, gated by `INVENTREE_URL` and `INVENTREE_TOKEN` in the environment, read-only.
- Coverage target 85% for the package.

## Open items

1. Create and update request bodies for parts, locations and stock. Must be taken from the live `/api-doc/` of the target instance. Not in the docs I could reach.
2. Which InvenTree version the owner runs. Decides the fixtures and the supported range.
3. Resolved: the owner's approval comes from the terminal approval CLI (`approve_cli.py`), not from Meridian HITL or the MCP host.

## Build order for the swarm

1. Client: HTTP, auth, pagination, errors. Fake transport tests.
2. Read tools and the version and role checks.
3. Plan store, preview, approval hash, audit file.
4. Write tools, one operation at a time, each with its own preview and tests.
5. MCP server wiring over stdio, and a registration note for Meridian connections.
6. Independent review of the write path before anything runs against a real instance.

## Estimate

Read path: 6 to 8 hours. Write path with approval and audit: 10 to 14 hours. The swarm runs steps 1 to 2 and 3 to 4 in parallel once the client exists.
