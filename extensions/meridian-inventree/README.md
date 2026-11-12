# meridian-inventree

An MCP server for one InvenTree instance, over its REST API.

- **Read tools** search and fetch parts, categories, stock items and locations, and report the InvenTree version and the token's roles. They never write.
- **Write planning** stores a plan of changes and writes nothing. A plan runs only after the owner approves it in a terminal.

**Agents cannot execute writes.** No tool in this server approves a plan or runs one. The tool list is fixed, and `tests/test_server.py` fails if an execute or approve tool appears.

**Read this before deploying.** Approval here is a control against accidental or drifting writes. It is not a barrier against a hostile process running as the owner. The real boundary is the write-capable token. See [What approval does and does not protect against](#what-approval-does-and-does-not-protect-against).

## Environment

| Variable | Required | Purpose |
| --- | --- | --- |
| `INVENTREE_URL` | yes | Base URL of the instance. Must be `https`, or `http` for `127.0.0.1` or `localhost`. Requests go only to this origin. |
| `INVENTREE_TOKEN` | yes | API token for an InvenTree user. Environment only in v1. Never write it to a file in the repo or paste it into chat. Use a read-only user's token for the agent-facing server, and the write-capable token only in the owner's executor. See [the boundary](#what-approval-does-and-does-not-protect-against). |
| `MERIDIAN_INVENTREE_STATE` | no | Folder for plans and the audit log. Default: `%LOCALAPPDATA%\meridian\inventree` on Windows, `$XDG_STATE_HOME/meridian/inventree` (or `~/.local/state/meridian/inventree`) elsewhere. It must be outside the repository, and the server refuses a path inside one. |

A missing `INVENTREE_URL` or `INVENTREE_TOKEN` prints one line to stderr and exits with code 2. Nothing is served.

## Run

From `extensions/meridian-inventree`:

```
python -m meridian_inventree
```

This starts the MCP server over stdio. At startup it prints one line to stderr with the InvenTree version and whether writes are allowed. An installed copy has the same entry point as `meridian-inventree-mcp`.

Example host entry. No secret is in the file. The host starts the server with `INVENTREE_URL` and `INVENTREE_TOKEN` set in its environment, and that token must belong to a user who cannot write:

```json
{
  "mcpServers": {
    "inventree": {
      "command": "python",
      "args": ["-m", "meridian_inventree"],
      "cwd": "<path to extensions/meridian-inventree>"
    }
  }
}
```

This package is standalone. It is not part of the core Meridian server, so it can be installed or removed on its own.

## Propose and approve

1. An agent calls `inventree_propose` with a list of operations. Each operation is `{"op": ..., "client_key": ..., "params": {...}}`, where `op` is one of `create_part`, `update_part`, `create_location`, `create_stock_item`, `transfer_stock` or `adjust_stock_quantity`. A plan holds at most 50 operations, and each `client_key` is unique within its plan. A quantity must have an adjusted exponent from -9 to 9 and at most 15 significant digits. Any other quantity, such as `1E+999999999`, is refused before any request is built.
2. The server checks each operation, reads the current state it would change, and stores the plan as `<state>/plans/<plan_id>.json`. Nothing is written to InvenTree. The stored preview for each operation holds the exact `method`, `path` and `body` the run will send, along with the before and after values.
3. The owner reviews the plan and approves it in a terminal:

   ```
   python -m meridian_inventree.approve_cli --plans-dir <state>/plans --audit-file <state>/audit.jsonl <plan_id>
   ```

   The command needs a real terminal. It prints every operation with its request, its before and after values, and the `plan_hash`. Long values wrap onto indented lines. None is cut, so read the whole request before you type the `plan_hash` exactly to approve. Anything else refuses. Each decision is written to the audit file.

   Before anything is printed, the command checks each plain field: `plan_id` and `plan_hash` must be hex, `status` and each operation name must be a plain identifier, and `created_at` must be ISO 8601. The plan hash does not cover these fields, so a file edited on disk can change them. A plan with a field that fails is refused with `unsafe_display`. Only the field's name is shown, never its value, and nothing is approved. The JSON values (params, request, before, after, client_key) are printed with control characters escaped.
4. Running an approved plan goes through `write_tools.run_plan`, called by the owner. The steps are listed under [Plan states](#plan-states).
5. `inventree_plan_status` shows the state of a plan and its results. It never shows the plan hash.

Agents cannot approve a plan, cannot see the hash that approval needs, and cannot run one.

## Plan states

```
proposed -> approved -> running -> executed
                                -> failed
                                -> needs_reconcile
```

- `approve` (the terminal prompt) moves a plan from `proposed` to `approved`.
- `run_plan` moves it from `approved` to `running`, under the plan lock, before the first request is sent.
- When the run finishes, `run_plan` records `executed` or `failed` with the results. If one operation has an [unknown outcome](#recovery-an-unknown-outcome), or a write succeeded but its `op_done` event could not be recorded, it records `needs_reconcile` instead.

`run_plan` reloads the plan from disk and ignores the copy the caller passes, except for its `plan_id`. It runs only when the stored plan is `approved`, its approval names the owner, and its hash matches both `plan_hash` and the approved hash. Before any request, it also checks that every operation is valid, that the InvenTree version is inside the supported range, and that the audit log can be read. Only then does it move the plan to `running`.

Before each write, `run_plan` reads again the live state that the operation changes, and compares it with the before values stored at approval. If they differ, or the re-read fails a check that passed at proposal, the operation is refused with `state_changed`. Nothing is sent, the run stops, and the plan is recorded as `failed`.

A plan runs once. A `running`, `executed`, `failed` or `needs_reconcile` plan is refused by `run_plan` every time. Remaining work needs a new plan.

On Windows, another process that has the plan file open makes the save fail. The save is retried 5 times, 50 ms apart. If the file is still locked, the change is refused with `plan_file_locked` and the plan file is left as it was.

## Recovery: a running plan

A plan stays `running` when `run_plan` does not reach its final record. That happens when the run is interrupted, or when the final record cannot be written (the error reports `result_not_recorded`). A request whose response is lost is handled differently: `run_plan` records it as `op_unknown` and the plan becomes `needs_reconcile`. See [Recovery: an unknown outcome](#recovery-an-unknown-outcome).

A running plan needs the owner to inspect the audit log and the InvenTree state before any manual change. For each operation, look for an `op_started` event with no matching `op_done`, `op_failed` or `op_unknown`, then check in InvenTree whether that change exists. Only after that decide what to do. Nothing in this package moves a running plan forward or back.

## Recovery: an unknown outcome

A write has an unknown outcome when its request may have reached InvenTree but no usable answer came back. That is:

- a timeout, or an unreachable error after the request was sent;
- an HTTP 502, 503 or 504 from a gateway in front of InvenTree (other 5xx answers are ordinary failures);
- a 2xx answer too large or too deeply nested for the client to read;
- a 2xx answer with no JSON body, or with no usable primary key. A 2xx answer is success only when it names the object it changed.

The audit log records it as `op_unknown`, not `op_failed`. The run stops there, the operations after it are reported `not_run`, and the plan moves to `needs_reconcile`.

A write that succeeded but whose `op_done` event could not be appended is handled the same way. The plan moves to `needs_reconcile` and the run stops. The audit log then does not show the key as succeeded, so a retry could repeat the write.

Nothing retries it, and `run_plan` refuses a `needs_reconcile` plan every time. Only the owner can settle it:

1. Read the audit log for the plan. Find the `op_unknown` event and its `client_key`.
2. Check InvenTree for the change that operation would have made. The stored `request` in the plan shows the part, location, stock item or quantity it named.
3. Record the outcome by hand before any further plan runs. Nothing in this package stops another plan from repeating the write while the outcome is unresolved, so do this first. Append one line through `AuditLog`, which applies the timestamp and redaction:
   - the change exists in InvenTree: record `op_done`. Later plans skip that `client_key`.
   - the change does not exist: record `op_failed` with `"manual": true`. Only that record lets a later plan use the `client_key` again, and that plan may run the write again.

   Until one of these is recorded, `propose` and `approve` refuse any plan that uses the `client_key` (`key_unresolved`). This is why recording the outcome comes first.

   ```
   python -c "from meridian_inventree.audit import AuditLog; AuditLog(r'<state>/audit.jsonl').append({'event': 'op_done', 'plan_id': '<plan_id>', 'client_key': '<client_key>', 'manual': True, 'note': '<what you checked in InvenTree>'})"
   ```

   The record for the "does not exist" case is the same command with `'event': 'op_failed'`.

   A write that succeeded but whose `op_done` could not be recorded is settled the same way. Check InvenTree, then record `op_done` if the change exists.

4. Leave the `needs_reconcile` plan as it is. Operations that never ran have no `op_done`, so a new plan for the remaining work runs them. An operation you recorded as `op_done` is skipped.

## Idempotency and client keys

Before it sends an operation, `run_plan` reads the audit log. If any earlier plan or run has an `op_done` event for that operation's `client_key`, the operation is skipped and reported as `skipped`. The check is across all plans, not only the current one. Only `op_done` counts. `op_failed` and `op_unknown` do not.

So `client_key` is a global idempotency key. Give each intended write its own key. Reuse a key only to repeat a write that must not happen twice. A new, different write that reuses a key that already succeeded will be skipped.

A key that the audit log would redact (text such as `token <value>`, `bearer <value>` or `sk_meridian_...`) is refused, so the log never hides a key that the check needs.

The audit check is made once, when a run starts. Two runs of different plans in different processes that start at the same moment can both pass it. Run one plan at a time.

A client_key whose write has an unknown outcome is also refused at proposal and at approval, with `key_unresolved`. The key stays refused until the owner settles the write by hand, as described under [the unknown-outcome recovery](#recovery-an-unknown-outcome). Settled means one of two things:

- a later `op_done` event for the key, so the key counts as succeeded, or
- a later `op_failed` event for the key that the owner recorded by hand with `"manual": true`.

An automatic `op_failed` does not settle it, because the earlier request may still have been applied. `propose` and `approve` both need the audit log, and refuse with `audit_unavailable` or `audit_unreadable` when they cannot read it.

## Stored request

At proposal, each preview stores the method, path and body the operation will send. At run time, `run_plan` rebuilds the request from the operation and compares it with the stored one. A difference refuses the operation with `request_mismatch` before anything is sent.

Just before the write, `run_plan` also re-reads the live state and compares its before values with the ones stored at approval. A difference refuses the operation with `state_changed`. See [Plan states](#plan-states).

## Version gate

Before the first write, `run_plan` reads `GET /api/` and refuses unless the version is inside the supported range. Propose uses the same check. The range is set by `SUPPORTED_VERSION_PREFIXES` in `meridian_inventree/server.py`. Pass that value to `run_plan` as `supported_prefixes`. With no range set, every run is refused.

## What approval does and does not protect against

A process running as the same OS user can read the plan file. It can also write the approval block itself. The plan hash check catches edits to operations and previews, but not to the status, running or approval blocks. So a same-user process can approve a plan without the owner typing anything, and then run it.

Approval in this package is therefore a control against accidental or drifting writes. It is not a barrier against a hostile process running as the owner.

The real boundary is the write-capable `INVENTREE_TOKEN`. It must not be available to agent sessions, in their environment, in any MCP configuration they start, or in any file they can read. It belongs only in the owner's executor, the process that the owner starts to run approved plans. An agent session that holds only a read-only token cannot write, whatever the plan file says. Check the token's rights with `inventree_roles`.

## Limits in this version

- **Writes are refused until the InvenTree version is named.** `SUPPORTED_VERSION_PREFIXES` in `meridian_inventree/server.py` is empty. Every write is refused until the owner sets the version range for their instance. `inventree_propose` refuses before it reads anything. The read tools keep working.
- **No command runs an approved plan yet.** Running is a Python call to `write_tools.run_plan(plan, client, audit, store=store, supported_prefixes=...)`, made by the owner. A command-line runner is still to be built.
- **No deletes, no user or permission changes, no bulk file import** in v1. Those need a separate plan.
- **Body field names are unconfirmed.** Each one is marked `VERIFY-LIVE` in `write_tools.py` and must be checked against the owner's instance before writes are enabled.
- **Answers to `transfer_stock` and `adjust_stock_quantity` are unconfirmed.** A 2xx answer counts as success only when it carries a usable primary key. The write path takes that key from the answer's `pk`. If InvenTree does not answer those two endpoints with the stock item's `pk`, every such write is recorded as `op_unknown` and its plan needs reconciling. Check the live answer shape before you use these operations.

## Tests

From the repository root:

```
pixi run python -m pytest extensions/meridian-inventree/tests -q -p no:cacheprovider
```

The suite makes no network calls. Every HTTP request goes through an injected fake transport.
