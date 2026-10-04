# Local Zotero sync

`meridian zotero sync` runs one bounded pass over unresolved citation markers in
a hosted Meridian project. The workstation reads the markers through
authenticated MCP, resolves them against the local Zotero Desktop API, and
sends only validated marker/item identity fields back to Meridian. The hosted
write rechecks each marker id and reference inside the requested project before
creating its `doc_edges` row.

The pass has no timer or polling loop. It reads the selected collection keys
saved by the local Zotero setup dialog. An empty selection means whole-library
scope. Zotero API keys and attachment paths stay on the workstation; citation
requests use the local Zotero API, which does not need a web API key. Meridian
authentication comes from `MERIDIAN_API_KEY` or `BEARER_TOKEN`, sent only as an
HTTP Authorization header.

```powershell
$env:MERIDIAN_API_KEY = '<load from your local secret store>'
meridian zotero sync --project-id '<project-id>' --max-items 100 --dry-run
meridian zotero sync --project-id '<project-id>' --max-items 100 `
  --zotero-data-dir "$env:USERPROFILE\Zotero" `
  --outputs-dir 'D:\Meridian\outputs'
```

`--max-items` accepts 1–500 citation markers per pass. Re-running is safe:
already linked markers are omitted from the next page, and the Outputs artifact
registry uses stable attachment identity and idempotent source edges. Use
`--dry-run` to count local matches and attachment availability without writing
hosted citation edges or registering artifacts.

When a local Zotero attachment file is available, the command computes SHA-256
locally and registers the file through the public Outputs artifact registry.
The registry stores its local path in its local ledger and records a portable
source locator of the form
`zotero:user/0/item/<parent-key>/attachment/<attachment-key>`. The command does
not send attachment bytes, filenames, or paths to hosted Meridian. A missing
file, missing Zotero data directory for a `storage:` path, unavailable Outputs
registry, or hash verification failure is reported as unverified.

## Host scheduler examples

In Windows Task Scheduler, create an action with the Meridian executable as
the program and these arguments:

```text
zotero sync --project-id <project-id> --max-items 100 --outputs-dir D:\Meridian\outputs --zotero-data-dir %USERPROFILE%\Zotero
```

Run the task as the same Windows account that configured Zotero and its
collection preferences. Configure `MERIDIAN_API_KEY` in that account's host
environment or a protected wrapper that reads from its secret store; do not
put the token in the task arguments.

For cron or another host scheduler, invoke the same one-shot command and make
`MERIDIAN_API_KEY` available from the scheduler's protected environment. The
host owns the schedule; Meridian does not schedule or repeat the operation.
