# Meridian Zotero Bridge

Menu actions and a local write API for Zotero:

- **Import BibTeX here...** on a collection's context menu. Pick one `.bib` file. The entries are checked for
  duplicates against the whole library, and the new ones are imported into that collection.
- **Attach PDFs from folder...** on an item's context menu, for the selected items. Pick a folder. For each item
  whose Extra field has a `BibTeX key: <key>` line, the plugin looks for `<key>.pdf` in that folder and attaches it.
- **Local write API** on `127.0.0.1:23125`, for scripts and agents. It creates, edits, trashes and restores items,
  notes, attachments, relations, collections and tags, and it imports BibTeX. Reads stay on Zotero's own local API.
  See "Local write API" below.

Plugin ID: `meridian-zotero@usemeridian.us`. Version `0.1.5` (source; not yet signed or released).
Earlier builds: `0.1.4` (source; not signed or released), `0.1.3` (released; it had the write API, but accepted any
readable file as a linked attachment, which 0.1.4 closes), `0.1.2` (Mozilla-signed, menus only, no write API), `0.1.1` (on Mozilla, uploaded by the owner, with
the old placeholder `update_url`, so it does not auto-update from GitHub), `0.1.0` (Mozilla-approved, no
`strict_max_version`).

## Status

- **Verified in Zotero 10.0.6.** On 2026-10-17, against a throwaway profile with its own data folder, the write API
  passed 52 live checks: admission (token, Host, Origin), item, note, attachment, relation, collection, tag, batch
  and BibTeX routes, and error responses. A missing token file left the API off while Zotero ran normally.
  Those checks ran before the limit, collection, restore, linked-file, batch rollback, BibTeX key and creator-role
  changes listed below, so those changes are not live-checked yet. They also predate write grants, so writes in those
  checks would now need a grant key.
- **Write grants, verified live on 2026-10-19 in a throwaway Zotero 10.0.6 profile.** A write without a grant key was
  refused with `grant_required`. The desktop prompt showed the app name and the self-reported warning. Allow for 1 hour
  gave a grant that expired about an hour later, and Allow until Zotero restarts gave one that expired about eight
  hours later. Deny, Escape and Enter all refused. A refused app name was turned away with `grant_cooldown` when it asked
  again at once. The key came back once. A
  write with the key succeeded, one without it was refused, and a revoked key was refused. The live run found one
  defect, which is fixed: Escape came back as the second button and granted a session, so Deny is now that button.
- **Standing approval (30 days) and `DELETE /v1/standing`: not yet verified in Zotero.** The node tests cover the
  logic. The desktop's fourth button, and the revoke, still need a live check in a throwaway profile. The button also
  depends on `Zotero.Prompt.confirm` showing four buttons, which the node tests cannot check.
- **Open local writes: not yet verified in Zotero.** The node tests cover the logic, including the token and the write
  gate. The pref read at startup, and the restart that turns it off, still need a live check in a throwaway profile.
- **Linking, verified live on the real Windows resolver.** On 2026-10-17, in a throwaway Zotero 10.0.6 profile with
  the local API on, the link policy gave the expected result for 13 of 13 cases: a plain allowed file, a trailing dot
  on a folder and an 8.3 alias of the allowed folder (all stored at the real path), an allowed folder written with
  forward slashes, a junction out of the allowed folder, a junction into a sensitive folder, a hard link, a device
  name, a file outside the allowed folders, a missing file, an alternate data stream, a folder named like a document,
  a path that ends in a separator, and an allowed list that also names a folder resolving to the drive root, which
  must not allow the drive. A linked file's path was also read back through Zotero's local API, which answered with a
  redirect to the path, not the bytes. Not covered live: a symbolic link (creating one needs an administrator or
  Developer Mode), a network drive, and the PDF menu, which needs the Zotero window.
- **Verified in node.** The unit tests cover the HTTP parser, admission, validation, the link policy against a fake
  resolver, the write logic (against a Zotero-shaped fake) and the design-line checks in `test/static-checks.test.js`.
- **Not verified in Zotero:** the two menu commands, the preferences pane, and the file pickers. These run only
  through the Zotero window, which the checks above did not drive. The BibTeX import they share with the API was
  verified through the API.
- **Not yet done:** a Mozilla-signed `0.1.5` build, and its release to the update channel. Until that happens,
  Meridian's install step prints the signing command.

Two bugs were found and fixed while verifying. The BibTeX duplicate check called `Zotero.Items.getAll` with its first
two arguments swapped, so the import failed before it wrote anything. The menu import and the API import share that
code, so both were affected. Restoring a collection now restores the subcollections that were trashed with it, as
Zotero's own restore does. That restore is API-only.

## Design line

- A write comes from one of two places: a menu command you choose inside Zotero, or the local write API.
- The write API listens on `127.0.0.1` only. It answers only a `Host` header of `127.0.0.1:<port>`. It refuses any
  request with an `Origin` header, so a browser page cannot drive it. It needs `Authorization: Bearer <token>`,
  compared in constant time. A write also needs a grant key that a person approved at the desktop (see Write grants).
- **Local trust.** The token admits a request, but it does not grant a write. A write needs a grant that a person
  approves at the desktop. The API cannot tell that person from another process of the same user, so the desktop
  prompt is the protection, and it shows the app name as self-reported. A standing approval (30 days) lets any process
  get a write without a prompt, so it widens this (see Standing approval below). Open local writes does the same for as
  long as the owner has it on (see Open local writes below). The processes share one connection limit of
  eight. A connection that sends no complete request is closed after ten seconds, so one process can hold a slot for
  up to ten seconds that way. After an answer, the socket stays open for up to five more
  seconds. This affects local availability only: a local process can keep other callers from connecting.
- The plugin makes no outbound network calls. There is no file watcher and no timer that picks up outside files.
- Only `api/listener.js` opens a socket, starts a timer, or names the loopback address. Everything else is pure
  logic in `lib/`, which node tests cover.
- Nothing deletes data permanently. Items and collections go to the trash, and restore brings them back.
  `test/static-checks.test.js` fails the build if code calls `erase`, `emptyTrash`, or an SQL `DELETE`.
- `test/static-checks.test.js` also fails the build on a URL literal, a `fetch` call, an observer, a localhost name,
  an email address other than the plugin ID, a secret-shaped string, or a machine-local path.

## Local write API

### Start, stop, and the token

- The API starts when Zotero starts, but only if the profile's `user.js` names a token file
  (`extensions.zotero.meridianZoteroBridge.apiTokenPath`) and the file holds a usable token. It stops at shutdown.
  Without a usable token the API stays off, the menus still work, and Zotero logs the reason.
- The port is `extensions.zotero.meridianZoteroBridge.apiPort`, default `23125`. If another program holds the port,
  the API stays off and Zotero logs that.
- The Meridian install writes both prefs into the profile's `user.js`. It creates the token file once, in
  `%USERPROFILE%\.meridian\local_runner\zotero_bridge\api_token` (or under the folder named by
  `MERIDIAN_LOCAL_RUNNER_STATE_DIR`). A reinstall keeps an existing usable token. The token is a credential. Do not
  print it, paste it into a chat, or commit it.

### Calling it

Every request needs these headers:

- `Host: 127.0.0.1:23125`
- `Authorization: Bearer <token>`
- `Meridian-Write-Grant: <write_key>`, for any write. Reads and grant requests do not need it. See Write grants.
- `Content-Type: application/json`, for any request with a body. The body is one JSON object, at most 40 MiB.

Every response is JSON. Errors have the shape `{"error": {"code": "...", "message": "...", "details": {...}}}`.
Each connection carries one request, and the server closes it after the answer.

```bash
TOKEN=$(cat "$HOME/.meridian/local_runner/zotero_bridge/api_token")
# 1. Ask for a grant. A person at the desktop answers the prompt.
curl -s -X POST http://127.0.0.1:23125/v1/grants \
  -H "Host: 127.0.0.1:23125" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"app_name": "My script"}'
# 2. Poll the grant until its status is granted. The write_key comes back once: keep it.
curl -s http://127.0.0.1:23125/v1/grants/<grant_id> \
  -H "Host: 127.0.0.1:23125" -H "Authorization: Bearer $TOKEN"
# 3. Write with the grant key.
curl -s -X POST http://127.0.0.1:23125/v1/items \
  -H "Host: 127.0.0.1:23125" -H "Authorization: Bearer $TOKEN" -H "Meridian-Write-Grant: <write_key>" \
  -H "Content-Type: application/json" \
  -d '{"item_type": "book", "fields": {"title": "A title"}, "tags": ["reading"]}'
```

### Write grants

A write needs a grant key, and a person at the desktop has to approve the grant. A process that holds only the token
cannot write on its own.

- `POST /v1/grants` with `{"app_name": "..."}` asks for a grant. The answer is `202` and a `grant_id`. Zotero shows a
  prompt that names the app. Its buttons are **Allow for 1 hour**, **Allow until Zotero restarts**, **Allow for 30
  days** and **Deny**. Deny is the default, so Enter and Escape both refuse. While a standing approval is in force, or
  open local writes is on, the answer already says `granted`, with no prompt (see below).
- `GET /v1/grants/<grant_id>` reports `pending`, `granted`, `denied`, `expired` or `revoked`. When a grant is first seen
  as granted, the reply carries `write_key` once. A later request does not return it again.
- Send the key as `Meridian-Write-Grant: <write_key>` on every write. A write without a live key is refused with
  `403 grant_required`. The batch route is a write, so each operation in a batch needs the same key.
- `DELETE /v1/grants/<grant_id>` ends a grant at once.
- An hour grant ends after one hour. A session grant ends when Zotero restarts, or after eight hours at most. Nothing is
  written to disk, so a restart clears every grant.
- Limits: one request can wait for an answer at a time (a second is refused with `grant_busy`). An app name that was
  refused waits five minutes before it can ask again (`grant_cooldown`). Six requests in ten minutes is the most
  (`grant_rate_limited`). If Zotero cannot show the prompt, the request is refused. An error never grants anything.
- The app name is what the requester says it is, and the prompt says so.
- **What this does not stop.** A grant key goes back to whichever process asked, so approving a request also gives that
  process its key. The prompt cannot tell the desktop app from another process of the same user, and it cannot see
  which process asked. Read the app name, and approve only a request you made yourself.

Reads: use Zotero's own local API (`http://127.0.0.1:23119/api/users/0/...`), which Zotero serves when
"Allow other applications on this computer to communicate with Zotero" is on. The write API does not serve reads.
Linked attachments are allowed only from the folders you choose, with the switch on or off (see Limits). While the switch
is on, Zotero tells local programs the path of each linked attachment.

### Standing approval for 30 days

**Allow for 30 days** at the desktop saves a standing approval that runs for 30 days from that click. While it lasts,
every `POST /v1/grants`, from any app, is granted at once with no prompt. The key it gives is a one-hour key, and it
comes back on the poll as usual. The busy, cooldown and rate limits still apply.

- Only that desktop answer makes a standing approval. A request body cannot make or extend one.
- It is saved as the preference `extensions.zotero.meridianZoteroBridge.standingGrantUntil`, so it survives a restart
  of Zotero. It ends when it runs out, or when you revoke it.
- **To revoke it,** send `DELETE /v1/standing` with the bearer token. It answers `200`, and the next grant request
  prompts again. Grants already made are not ended, and each runs to the end of its own hour.
- This is a wider grant than the others. Any process that holds the token can ask, and while the approval lasts it is
  granted without a prompt. Turn it on only on a machine where you trust every process that can read the token file.

### Open local writes (off by default)

Open local writes turns off the prompt for this Zotero. While it is on, every `POST /v1/grants` is granted at once, with
no dialog, and the key comes back on the poll as usual. Each key is a one-hour key, as with a standing approval. The
busy, cooldown and rate limits still apply.

Meridian can set this for you: `python -m meridian zotero local-writes on --restart` writes the setting into the default
profile's `user.js`, then closes Zotero gracefully (never a force kill) and starts it again, so the change takes effect.
`off` turns it back off the same way. Without `--restart` the command says that Zotero must restart first. With
`--restart`, a run that did not restart Zotero exits with 1. If `user.js` already has a line that names this setting in
any other form (a statement split over two lines, two statements on one line, a statement inside a `/* ... */` block
comment, a statement inside a string or template literal (including one that a backslash line continuation leaves open),
a pref name written with an escape sequence such as `local\x57ritesOpen`, or a comment that names it), the command
changes nothing and says so. Meridian reads `user.js` as JavaScript and decodes its string literals. A file it cannot
read safely is refused as a whole, and the command changes nothing. Limits: a pref name built at runtime, such as by
joining string pieces with +, or inside a template literal's ${ } substitution, is not detected. Anything in the file
that is not valid JavaScript is refused where the scanner cannot tell what Zotero reads, but the scanner does not parse
the whole file. A / outside a comment or a string, which is a division or a regular expression, is refused, because the
scanner does not parse expressions. The tray's Zotero local writes item asks you to
confirm before it turns the setting on. The shipped default stays off: the plugin ships with this preference set to
`false`, and installing it changes nothing unless you pass `--local-writes`.

- **It is off by default.** To turn it on, set `extensions.zotero.meridianZoteroBridge.localWritesOpen` to `true` in
  Zotero's Config Editor (about:config), then restart Zotero. The bridge reads it at startup, so a change takes effect
  only after a restart.
- **The token is still required.** A request without the bearer token is refused with `401`, as before. A write still
  needs a grant key in the `Meridian-Write-Grant` header. Only the prompt is skipped.
- **A request body cannot turn it on.** Only the preference does.
- **It does not expire and saves nothing.** It makes no standing approval, so it needs no saved expiry, and nothing is
  left behind when it is turned off.
- **To turn it off,** set `extensions.zotero.meridianZoteroBridge.localWritesOpen` to `false` and restart Zotero. A
  restart also ends every grant, since grants are kept only in memory. `DELETE /v1/standing` does not turn it off: it
  clears only a standing approval.
- This is wider than the prompted grants. While it is on, any process that holds the token can write without a prompt.
  Turn it on only on a machine where you trust every process that can read the token file.

### Endpoints

Keys are Zotero's 8-character keys. Every item response has `key`, `version`, `date_modified`, and `library_id`.
Collection responses have the same fields except `date_modified`, because Zotero collections do not keep one.

| Method and path | Body | Notes |
| --- | --- | --- |
| `GET /v1/status` | none | Plugin, API and Zotero versions, and the library id. |
| `POST /v1/items` | `item_type`, optional `fields`, `creators`, `tags`, `collection_keys`, `relations` | Answers 201. Unknown fields are refused, and the reply lists them. |
| `PATCH /v1/items/{key}` | `fields`, `creators`, `set_tags`, `add_tags`, `remove_tags`, `set_collections`, `add_collections`, `remove_collections`, `add_relations`, `remove_relations`, and the precondition fields | Only the fields you send change. `item_type` cannot change. |
| `POST /v1/items/{key}/trash` | precondition fields, optional | Moves the item to the trash. Works for notes and attachments too. |
| `POST /v1/items/{key}/restore` | precondition fields, optional | Takes the item out of the trash. |
| `POST /v1/items/{key}/notes` | `html` | Creates a child note of a regular item. Answers 201. |
| `PATCH /v1/notes/{key}` | `html`, precondition fields | Replaces the note's HTML. |
| `POST /v1/items/{key}/attachments` | `mode`, and either `filename` plus `data_base64` (`imported_file`) or `path` (`linked_file`), plus optional `title` and `content_type` | Answers 201. A linked file must lie inside a folder listed in `extensions.zotero.meridianZoteroBridge.linkFolders`, and Zotero stores its real path. Refused with 422 `linking_refused`, `file_not_found` or `file_check_failed`, or `linking_unavailable` off Windows (see Limits). Not allowed inside a batch. |
| `POST /v1/items/{key}/relations` | `related_key`, or `predicate` plus `object` | Adds a relation. |
| `DELETE /v1/items/{key}/relations` | the same as the POST | Removes the relation. |
| `POST /v1/collections` | `name`, optional `parent_key` | Answers 201. |
| `PATCH /v1/collections/{key}` | `name`, `parent_key` (`null` moves it to the top), `expected_version` (synced libraries only) | Refuses a move that would make a collection its own ancestor. Sending `expected_modified` is refused with 422. |
| `POST /v1/collections/{key}/trash` and `/restore` | `expected_version`, optional (synced libraries only) | Restoring also restores every trashed subcollection under it, including ones trashed earlier, as Zotero's own Restore does. A collection whose parent is still in the trash cannot be restored: restore the parent first (422). |
| `POST /v1/collections/{key}/items` | `item_keys` | Adds items. Refuses the whole list if any key is unknown or trashed. |
| `DELETE /v1/collections/{key}/items` | `item_keys` | Removes items from the collection. The items stay in the library. |
| `POST /v1/tags/rename` | `from`, `to` | Renames a tag across the user library. Not allowed inside a batch. |
| `POST /v1/import/bibtex` | `bibtex`, optional `collection_key`, optional `dry_run` | Returns the plan: what would import, the exact duplicates, and the conflicts. `dry_run: true` writes nothing. A file whose library changes during the parse is refused with 409 `library_changed`, and nothing is imported. Not allowed inside a batch. |
| `POST /v1/batch` | `operations`: a list of `{method, path, body}` | Runs the operations in one transaction. Any failure rolls the whole batch back and reports `failed_index`. Needs a write grant key. |
| `POST /v1/grants` | `app_name` | Asks a person at the desktop for a write grant. Answers 202 with a `grant_id`, and `granted` at once while a standing approval is in force or open local writes is on. The token is enough here. |
| `GET /v1/grants/{id}` | none | Reports the grant's status. Returns `write_key` once, when the grant is first granted. The token is enough here. |
| `DELETE /v1/grants/{id}` | none | Ends the grant at once. The token is enough here. |
| `DELETE /v1/standing` | none | Clears the standing approval, so the next grant request prompts again, unless open local writes is on. It does not turn open local writes off. Answers 200. The token is enough here. |

Relation objects are the ones Zotero stores. `related_key` links two items in the library. Any other predicate
needs the `predicate:name` form (for example `dc:replaces`) and an `object` with no spaces.

### Concurrency

- Send back the `date_modified` you read as `expected_modified`. If the item has changed since, the write is refused
  with `409 changed_since_read`, and nothing is written. Zotero keeps `date_modified` to the second, so two saves in
  the same second look the same to this check.
- Local items stay at `version: 0` until a sync, so `expected_version` gives no protection for them. It is checked
  when you send it, for synced libraries.
- Collections have no `date_modified`, so their replies omit it. For a collection, send `expected_version` only for a
  synced library. A collection write that sends `expected_modified` is refused with 422. A local library keeps version
  at 0, so a collection in one has no version check to send.
- Writes run one at a time. Two requests that arrive together do not interleave.

### Batches

- A batch runs its operations in order, inside one transaction. If any fails, the batch is rolled back and the reply
  names the operation that failed. The Zotero objects that the batch changed in
  memory are reloaded from the database, so the running Zotero matches what is stored. Writes inside a batch do not
  wait on a transaction Zotero opened for the UI.
- These can run inside a batch: item create, update, trash and restore; note create and update; relation add and
  remove; collection create, update, trash and restore; and adding or removing items in a collection.
- These cannot run inside a batch, and a batch that names one is refused before anything is written: attachments, tag
  rename and BibTeX import. Each opens its own transaction, and Zotero does not nest them. Send them on their own.

### Limits and rules

- Request: headers 16 KiB, body 40 MiB. Attachment content 24 MiB after decoding. Note HTML 2 MB. Field value
  100 KB. BibTeX text 8 MiB. A batch holds at most 200 operations. The body limit leaves room for a 24 MiB attachment,
  which is 32 MiB in base64, plus the rest of the JSON.
- Writes go to the user library only. Group libraries are not writable through this API.
- An imported attachment's file name may not contain a colon, a slash, a backslash or a control character, and it may
  not be a Windows device name such as `CON`. A colon would name an alternate data stream.
- A creator role that the item type does not allow is refused with 422. The message names the role and the item type.
- A BibTeX duplicate check counts trashed items as existing, so a re-import does not bring back something the user
  trashed.
- An imported attachment is copied into Zotero storage. A linked attachment stores a path, and linking follows these rules:
  - **Only folders you allow.** A file can be linked only from a folder listed in
    `extensions.zotero.meridianZoteroBridge.linkFolders`, separated by vertical bars (`|`), which Windows forbids in
    names, so a folder name such as `Work;Archive` is kept whole. The list is empty by default, so nothing can be
    linked until you add a folder. Each entry must be a drive path, such as `D:/Papers`. A drive root cannot be
    listed, and nor can a folder that resolves to one. Any other file is refused with 422 `linking_refused`. The write
    API cannot change this list. Only Zotero's preferences can.
  - **Real paths, not the paths the caller sent.** The bridge asks Windows for the real path of the file and of each
    allowed folder, and it compares the real paths exactly, as the disk spells them. A short name such as `MERIDI~1`,
    or a trailing dot or space, resolves to the real name, so it cannot move a file out of an allowed folder. Zotero
    stores the real path. Containment is checked before the other file checks, so a file outside the folders is
    refused with the same message whether it exists or not.
  - **A file, on a local drive, with one name, and with no link on its path.** A folder, a path that ends in a
    separator, a network share, and a file with a second name (a hard link) are each refused with 422
    `linking_refused`. So is a link, a junction or a cloud placeholder (an online-only OneDrive file, for example)
    anywhere on the path, including a folder on it. The bridge checks each folder for a link before it goes below it,
    and it recognises a network drive from the drive mapping before any call touches the path. A folder you allow must
    therefore be listed by its real path, not through a link.
  - **Document, image or media files only.** The file name must end in `pdf`, `epub`, `djvu`, `doc`, `docx`, `odt`,
    `rtf`, `txt`, `md`, `csv`, `tsv`, `xls`, `xlsx`, `ods`, `ppt`, `pptx`, `odp`, `png`, `jpg`, `jpeg`, `gif`, `tif`,
    `tiff`, `webp`, `bmp`, `mp3`, `m4a`, `wav`, `mp4` or `mov`. HTML and SVG are left out, because they can carry
    active content. A dot-file, a colon after the drive letter (an alternate data stream), and a device name such as
    `CON`, `NUL`, `CONIN$` or `COM1` in any part of the path are refused.
  - **No credential, profile or app-data folder.** A folder on the path named `AppData`, `.meridian`, `.ssh`,
    `.gnupg`, `.aws`, `.azure`, `.kube`, `.docker`, `.git` or `.password-store` is refused, in any letter case, with
    trailing dots and spaces ignored as Windows ignores them.
  - **Errors.** A missing file is 422 `file_not_found`, and so is a file the bridge cannot read, because Windows does not
    report the reason through the bridge. A check the OS cannot complete is 422 `file_check_failed`. Off Windows, linking
    is 422 `linking_unavailable`. Nothing is linked on any of these. The checks have no time limit, because the Windows
    calls cannot be interrupted. A network drive, and a link anywhere on the path, are refused before the bridge goes
    below them, so a slow share is not entered.
  - **What an allowed folder exposes.** Zotero's local API, while its switch is on (`extensions.zotero.httpServer.localAPI.enabled`),
    tells any local program that can reach it the path of every linked attachment. It does not send the file's bytes.
    The file request gets a redirect to the path (checked against Zotero 10.0.6), so a program then reads the file only
    if its own operating-system permissions allow it. Linking a file in an allowed folder therefore makes its path
    visible to local programs while that switch is on. With the switch off, Zotero serves nothing. Keep allowed folders
    to ones whose paths you are willing to have seen.
  - **What the checks cannot stop.** They run when a file is linked, and nothing re-checks the stored path afterwards.
    Someone who can write to an allowed folder can later replace the file, or move the folder and put a junction in its
    place, so the stored path would then reach another file. Files linked before this version, by this bridge or any
    other tool, are not re-checked. Only an administrator, or a user with Developer Mode, can create a symbolic link,
    and the check refuses any link on the path, so a link made that way is refused when a file is linked through it.
- Errors go to Zotero's error console. Request bodies and the token are never logged.

### Not in this version

Permanent delete, group libraries, any reads (use Zotero's local API), PDF text extraction, and tag management beyond
rename, add and remove.

## Install

1. From a source checkout of Meridian, run `pixi run python -m meridian zotero install-plugin`.
   If Zotero is open, the command closes it first, unless the saved setting, the
   `MERIDIAN_ZOTERO_CLOSE_ON_INSTALL` override (off values only), or `--keep-zotero-open`
   turns closing off. With closing off, close Zotero before you run it.
   A close request can still time out, and then nothing is installed.
   An install that finds the plugin already current does not close Zotero.
   The command packs this folder into the unsigned build (the plugin ID with the `.xpi` extension) in Meridian's
   state folder. That build writes nothing to the Zotero profile. The command copies a file into your
   profile only when a Mozilla-signed file for this version is stored. With signing credentials set and no
   signed file stored, it first fetches the approved file for this version from addons.mozilla.org. That fetch
   only reads and submits nothing. Otherwise it prints the signing command and the unsigned file's path. See
   `docs/zotero-plugin-install.md`, section 4a.
2. In Zotero, open **Tools > Plugins**, click the gear icon, and choose **Install Plugin From File**.
3. Select the `.xpi`, then restart Zotero if asked.

Signing is a separate step, described in `docs/zotero-plugin-install.md`, section 4a. A timeout or a
failure may already have submitted this version to Mozilla, so the version number is used up. The message
says: Bump the version in manifest.json to sign a new build. Running it again for a version that already has a signed file for the same source reports it as already signed, exits 0, and uploads nothing.

Deleting `signed.json` removes the guarantee against resubmitting a version that was submitted without a
signed file. Meridian cannot detect a fully deleted record, so it cannot refuse a second submission of that
version. A stored signed file with no record is refused too. Install is also blocked after the record is deleted, while the signed file is kept: the install reports the version as used and says to bump it. To sign again, bump the version in `manifest.json`.

The one-submission guarantee lives in the state folder, `%USERPROFILE%\.meridian\local_runner\zotero_plugin\`, or in the folder named by `MERIDIAN_LOCAL_RUNNER_STATE_DIR` when that is set. A different machine, a different Windows user, or a `MERIDIAN_LOCAL_RUNNER_STATE_DIR` override starts with no record.

A version number is matched exactly as written, so `0.1` and `0.1.0` are different versions.
Mozilla's behaviour for a near-duplicate version is not verified.
A version that was submitted to Mozilla before this record existed, in an earlier build, is not protected,
because Meridian has no record of that submission. Bump that version before you sign again.

Scope: the automatic install through the Meridian tray app is Windows only. It needs Zotero 8 or later
(the manifest's minimum). See `docs/zotero-plugin-install.md`.

The install also writes the local write API's two prefs into the profile's `user.js`, and creates the token file
once, in `%USERPROFILE%\.meridian\local_runner\zotero_bridge\api_token`. A reinstall keeps an existing usable token.
Section 5 of the install guide has a status check that uses the token without printing it.

Beyond the file copy and the two API prefs, the install also writes the scan line to `user.js`
(`extensions.startupScanScopes = 1`), enables the add-on in `extensions.json`, and, when the add-on is not yet
registered, starts Zotero, waits for it to register the add-on, and closes Zotero again. Meridian enables the plugin
only in Zotero's default profile. A rerun on an already-current install repairs the scan line, the token and the two
API prefs, and writes nothing else.

## Duplicate rules (BibTeX import)

Each parsed entry is checked against every item already in the library, trashed items included. Checks run before
anything is written.

| Situation | Result |
| --- | --- |
| DOI matches an existing item's DOI (case-insensitive, `doi.org` prefix ignored) | **Exact**: skipped |
| Neither side has a DOI, and the normalised title matches | **Exact**: skipped |
| Titles match and only one side has a DOI | **Conflict**: not imported, listed in the report for review |
| Titles match and both DOIs are present and differ | **Conflict**: not imported, listed in the report for review |
| Anything else | **New**: imported |

Normalised title: lowercase, BibTeX braces removed, whitespace collapsed, leading and trailing punctuation trimmed.
Punctuation inside the title is kept.

If a title matches both an exact item and a conflicting item, the entry counts as exact. An entry that appears twice
in the same file counts as exact the second time.

Citation keys. The citation key of an imported entry comes from the translator's `itemID` field, which the BibTeX
import translator keeps. An explicit `citationKey` field, or a `Citation Key:` line in Extra, is used first when
present. Each imported item gets a `BibTeX key: <key>` line in Extra, which the PDF step reads. If the library's item
count changes while the file is parsed, the import is refused with 409 `library_changed`, and nothing is imported.

## Attach PDFs (storage)

- **Default: copy.** Each PDF is copied into Zotero storage.
- **Link instead:** turn on "Link PDFs instead of copying" in the preferences pane, and list the folder in "Folders
  allowed for linked files". The file stays where it is and Zotero links to it. The same rules as the write API apply
  (see Limits and rules). A PDF outside those folders is not linked and not copied. The summary reports it, so the
  user can add the folder or copy the file by turning linking off.

**Cloud quota note.** If Zotero file sync is on, copied PDFs count toward the Zotero cloud storage quota. Linked files
do not. The link option avoids that cost, but linked files break if the source folder moves. The default is copy (the
link option is off).

A PDF is attached only when the item has no existing PDF attachment, the file exists, and the file starts with `%PDF-`.
A BibTeX key that cannot be a file name is skipped with a message: a key with a colon (Windows reads it as a stream
name), or one that names a Windows device, such as `CON`, `AUX` or `COM1`. Two selected items whose keys differ only in
case are both skipped, because Windows names one file for both. A file that fails is reported against its own item, and
the run goes on, so the summary always shows every item.

## Preferences

- `extensions.zotero.meridianZoteroBridge.linkPdfs`, default `false`. Shown in the preferences pane.
- `extensions.zotero.meridianZoteroBridge.linkFolders`, default empty, which allows no linked files. The folders that
  linked files may come from, separated by vertical bars (`|`). Shown in the preferences pane. The write API cannot set it.
- `extensions.zotero.meridianZoteroBridge.apiPort`, default `23125`. Set by the install.
- `extensions.zotero.meridianZoteroBridge.apiTokenPath`, default empty, which keeps the API off. Set by the install.
- `extensions.zotero.meridianZoteroBridge.standingGrantUntil`, default empty, which means no standing approval. Set only
  by the desktop's **Allow for 30 days** answer, and cleared by `DELETE /v1/standing` or when it runs out.
- `extensions.zotero.meridianZoteroBridge.localWritesOpen`, default `false`. Set `true` in about:config to grant every write
  request at once, with no prompt. See Open local writes. It is read at startup, so a restart is needed after a change.

## Compatibility

- `applications.zotero.strict_min_version` is `8.0`.
- `strict_max_version` is **`"99.*"`**. The owner's decision is no practical maximum, and `99.*` is that value. It was
  added in 0.1.1 because Zotero 10.0.6 rejects a `zotero` add-on without the key. The parser reports
  "applications.zotero.strict_max_version not provided", and in a fresh profile the file was deleted at startup. The
  0.1.0 build lacked the key. Tested 2026-10-16 with throwaway profiles.

## Updates

`update_url` points at `update.json` on the `main` branch of `meridianmcp/zotero-bridge-updates`, read through
`raw.githubusercontent.com`. That repo is separate from `meridianmcp/Meridian`, so the update file never touches the
Meridian deploy branches. The file does not exist yet. A release step will write it after each signed release, with
the signed file's URL and SHA-256. Until then the URL returns 404, and automatic updates do not work. The manifest
states this in `update_url_status`.

## Out of scope for this build

- AI summaries and notes. These are a later phase and will be off by default.
- Outbound network calls of any kind, including the Zotero Web API.
- Group library writes, permanent deletion, and reads through the write API.

## Verify before first real use

Verified, in a throwaway Zotero 10.0.6 profile with its own data folder:

1. **Translator does not save during parse.** The BibTeX dry run parsed three entries and wrote none. The count guard
   in `bootstrap.js` checks this on every import and found no change.
2. **Item, collection, tag, note, attachment and relation APIs** used by `lib/api/` behave as the 52 live checks
   expect. The checks read the stored state back from the database.
3. **BibTeX translator.** The import found a BibTeX translator and saved the new entries. The lookup in `bootstrap.js` tries
   the ID first and the label `BibTeX` second. The live run does not record which one matched.

Not verified yet:

4. **`Zotero.MenuManager.registerMenu`** shape, and the `context` fields the menus read (`collectionTreeRow.ref`,
   `items`). The menus were not opened in the Zotero window.
5. **`Zotero.PreferencePanes.register`** options and the XUL bindings in `content/preferences.xhtml`.
6. **File pickers.** `Components.classes['@mozilla.org/filepicker;1']` is used. Check whether Zotero 8 still exposes
   it, or swap in its current picker helper.
7. **Citation key on imported items.** The BibTeX import reads the key from the translator's itemID field and writes a BibTeX key: <key> line into Extra, which the PDF step reads. Verified live on 2026-10-17: an imported entry's Extra holds BibTeX key: <key>.

## Disposable-profile test checklist

Use a fresh profile that is not your working profile, with its own data folder, and a test library with a few items.
Do not use your real library.

1. Install the built `.xpi` into the disposable profile. Zotero starts without error, and the error console is clean.
2. Preferences pane: "Meridian Zotero Bridge" appears. The link checkbox is off.
3. Collection menu: right-click a collection, and "Import BibTeX here..." appears. Right-click a nested collection with
   the same name as another, and check the ambiguity message.
4. BibTeX import, with one `.bib` file containing an entry whose DOI matches an existing item, an entry with the same
   title and no DOI, an entry with the same title and a different DOI, and an entry with a new title. Expect the first
   two to be skipped, the third listed as a conflict, and the last imported into the collection.
5. Item menu: select items with and without `BibTeX key: <key>` in Extra. Run "Attach PDFs from folder..." and check
   that only the valid PDFs attach and the summary lists the rest.
6. Turn on "Link PDFs instead of copying", list the folder in "Folders allowed for linked files", repeat step 5, and
   check that the attachment is a linked file. Then remove the folder from the list and check that the PDFs in it are
   reported as not linked, and not copied.
7. Run "Attach PDFs from folder..." again on the same items. Expect "already has a PDF" for each.
8. Write API: `GET /v1/status` answers with the token, and a request without the token gets 401.
9. Disable and re-enable the plugin, then restart Zotero. Menus, the pane and the API still work once, with no duplicate
   menu entries.
10. Remove the plugin. Menus and the pane are gone, the API stops, and no plugin errors appear.

## Tests

```
node --test extensions/zotero-bridge/test/*.test.js
```

On Node 21 and later the directory form `node --test extensions/zotero-bridge/test/` does not expand to the test
files. Use the glob form above, or `npm test` from this folder.

Files:

- `lib/normalize.js`, `lib/duplicates.js`, `lib/bibkey.js`, `lib/pdf.js`, `lib/collections.js`: the pure logic of the
  menus.
- `lib/api/http.js`: the HTTP/1.1 parser and the response builder. `test/api-http.test.js`.
- `lib/api/auth.js`: Host, Origin, token shape and constant-time compare. `test/api-http.test.js`.
- `lib/api/validate.js`: input checks, base64, file names, keys, preconditions. `test/api-validate.test.js`.
- `lib/api/items.js`, `lib/api/collections.js`: the write logic. `test/api-items.test.js` and
  `test/api-collections.test.js`, both run against `test/support/fake-zotero.js`, which is not Zotero.
- `lib/api/app.js`: routes, JSON bodies, the write queue, and batches. `test/api-app.test.js`.
- `api/listener.js`: the socket. Covered by the live checks, not by node.
- `bootstrap.js`: Zotero glue (install, startup, shutdown, menus, pane, translator, attachments, the API).
- `test/static-checks.test.js`: the design line.
- `prefs.js`, `content/preferences.xhtml`, `manifest.json`: plugin settings and manifest.
- `package.json`: marks this folder as CommonJS for the tests. It is not part of the plugin manifest.
