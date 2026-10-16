# Meridian Zotero Bridge

Two menu actions for Zotero, plus a preferences pane:

- **Import BibTeX here...** on a collection's context menu. Pick one `.bib` file. The
  entries are checked for duplicates against the whole library, and the new ones are
  imported into that collection.
- **Attach PDFs from folder...** on an item's context menu, for the selected items. Pick
  a folder. For each item whose Extra field has a `BibTeX key: <key>` line, the plugin
  looks for `<key>.pdf` in that folder and attaches it.

Plugin ID: `meridian-zotero@usemeridian.us`. Version `0.1.0`.

## Status: source, packaged by Meridian, untested in Zotero

This folder is plugin **source**. Meridian's install command packs it into an `.xpi`
(see `docs/zotero-plugin-install.md`), but the packed file has not been installed into a
real Zotero profile or run inside Zotero. The pure logic in `lib/` is covered by
`node --test`. The Zotero glue in `bootstrap.js` and `content/preferences.xhtml` is
written against the APIs listed under "Verify before first real use" and has not been
exercised. Do not point it at a library you care about until the disposable-profile
checklist below passes.

## Design line

- No HTTP endpoint, no server, no file watcher, no timer that picks up outside files,
  and no code path that another program can trigger.
- A menu command or the preferences pane is the only way in. Every write starts from a
  user action inside Zotero.
- No network calls. The plugin code does not call the local Zotero API, the Zotero Web
  API, or any other endpoint.
- `test/static-checks.test.js` enforces this in code. It fails if the runtime files gain
  a network call, a server, a watcher, a timer, an observer, a URL literal, a local host
  address, a secret-shaped string, a machine-local path, or any email address other than
  the plugin ID.

The bridge makes no network calls of its own. Zotero itself checks the `update_url` in
`manifest.json` for updates when automatic updates are on. That request is Zotero's, not
plugin code. The URL is a placeholder (see "Updates" below).

## Install

1. From a source checkout of Meridian, run `pixi run python -m meridian zotero install-plugin`.
   If Zotero is open, the command closes it first, unless the saved setting, the
   `MERIDIAN_ZOTERO_CLOSE_ON_INSTALL` override (off values only), or `--keep-zotero-open`
   turns closing off. With closing off, close Zotero before you run it.
   A close request can still time out, and then nothing is installed.
   An install that finds the plugin already current does not close Zotero.
   The command packs this folder into the unsigned build `meridian-zotero@usemeridian.us.xpi` in Meridian's
   state folder. That build writes nothing to the Zotero profile. The command copies a file into your
   profile only when the Mozilla-signed file for this version is stored. Otherwise it prints the signing
   command and the unsigned file's path. See `docs/zotero-plugin-install.md`, section 4a.
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

## Duplicate rules (BibTeX import)

Each parsed entry is checked against every item already in the library. Checks run
before anything is written.

| Situation | Result |
| --- | --- |
| DOI matches an existing item's DOI (case-insensitive, `doi.org` prefix ignored) | **Exact**: skipped |
| Neither side has a DOI, and the normalised title matches | **Exact**: skipped |
| Titles match and only one side has a DOI | **Conflict**: not imported, listed in the report for review |
| Titles match and both DOIs are present and differ | **Conflict**: not imported, listed in the report for review |
| Anything else | **New**: imported |

Normalised title: lowercase, BibTeX braces removed, whitespace collapsed, leading and
trailing punctuation trimmed. Punctuation inside the title is kept.

If a title matches both an exact item and a conflicting item, the entry counts as exact.
An entry that appears twice in the same file counts as exact the second time.

## Attach PDFs (storage)

- **Default: copy.** Each PDF is copied into Zotero storage.
- **Link instead:** turn on "Link PDFs instead of copying" in the preferences pane. The
  file stays where it is and Zotero links to it.

**Cloud quota note.** If Zotero file sync is on, copied PDFs count toward the Zotero cloud
storage quota. Linked files do not. The link option avoids that cost, but linked files
break if the source folder moves. The default is copy (the link option is off).

A PDF is attached only when the item has no existing PDF attachment, the file exists,
and the file starts with `%PDF-`.

## Preferences

One preference, `extensions.zotero.meridianZoteroBridge.linkPdfs`, default `false`. The
pane shows the duplicate policy as text. Nothing else is configurable.

## Compatibility

- `applications.zotero.strict_min_version` is `8.0`.
- `strict_max_version` is **not set**, per the owner's decision for no maximum and
  automatic updates. Zotero's developer documentation says plugin authors "should" set
  it to the latest minor version they tested, but does not say it is required. If a
  future Zotero release refuses to load the plugin without it, set it to `"99.*"` and
  record the change here.

## Updates

`update_url` is a **placeholder and is not yet published**. Nothing is served at that
address yet. Do not treat automatic updates as working until usemeridian.us serves a
real update manifest at that URL. The manifest marks the placeholder in
`update_url_status`.

## Out of scope for this build

- AI summaries and notes. These are a later phase and will be off by default.
- Any network call of any kind, including the Zotero Web API.
- Installing the plugin into a profile or running it against a real library.

## Verify before first real use

These points come from the design and are not confirmed against a running Zotero build.

1. **Translator does not save during parse.** `bootstrap.js` runs the BibTeX translator with
   an `itemDone` handler that collects items, and passes `{ libraryID: false }`. The
   translator's save behaviour with that option is not verified. The code counts library
   entries before and after the parse, and aborts the import with a message if the count
   changed. Confirm in a disposable profile that the count does not change.
2. **`Zotero.MenuManager.registerMenu`** shape: `menuID`, `pluginID`, `target`
   (`main/library/collection`, `main/library/item`), `menus[].onCommand(event, context)`,
   and the `context` fields used (`collectionTreeRow.ref`, `items`). The Zotero developer
   documentation fetched for this build does not cover MenuManager.
3. **`Zotero.PreferencePanes.register`** options (`pluginID`, `src`, `label`) and the
   XUL `<preference>` binding in `content/preferences.xhtml`.
4. **File pickers.** `Components.classes['@mozilla.org/filepicker;1']` is used. Check
   whether Zotero 8 still exposes it, or swap in its current picker helper.
5. **Item APIs.** `Zotero.Items.getAll(true, libraryID)`, `Zotero.Items.getAsync`,
   `Zotero.Collections.getByLibrary(libraryID, true)`, `Zotero.Attachments.importFromFile`
   and `linkFromFile` with `{ file, parentItemID }`, `Zotero.File.pathToFile`.
6. **BibTeX translator ID** `9cb70025-a888-4a29-a210-93ec52da40d4`, with a fallback that
   finds the translator by label `BibTeX`.
7. **Citation key on imported items.** The import writes `BibTeX key: <key>` into Extra
   only when the translator output carries a citation key (`citationKey`, or a
   `Citation Key:` line in Extra). If it does not, imported items have no key line and
   the PDF attach step will skip them.

## Disposable-profile test checklist

Use a fresh profile that is not your working profile, and a test library with a few
items. Do not use your real library.

1. Install the built `.xpi` into the disposable profile. Zotero starts without error, and
   the Zotero error console is clean.
2. Preferences pane: "Meridian Zotero Bridge" appears. The link checkbox is off. The
   duplicate-policy text is shown.
3. Collection menu: right-click a collection, and "Import BibTeX here..." appears.
   Right-click a nested collection with the same name as another, and check the
   ambiguity message.
4. BibTeX import, with one `.bib` file containing:
   - an entry whose DOI matches an existing item (expect: skipped as exact),
   - an entry with the same title and no DOI on either side (expect: skipped),
   - an entry with the same title and a different DOI (expect: listed as conflict, not
     imported),
   - an entry with a new title (expect: imported into the collection).
   The item count before and after should match the expected number of imports.
5. Item menu: select items with and without `BibTeX key: <key>` in Extra. Put matching
   `<key>.pdf` files in a folder, including one file that is not a PDF. Run "Attach PDFs
   from folder..." and check that the copy default attaches only the valid PDFs and the
   summary lists the rest.
6. Turn on "Link PDFs instead of copying", repeat step 5, and check that the attachment
   is a linked file.
7. Run "Attach PDFs from folder..." again on the same items. Expect "already has a PDF"
   for each.
8. Disable and re-enable the plugin, then restart Zotero. Menus and pane still work
   once, with no duplicate menu entries.
9. Remove the plugin. Menus and the pane are gone, and no plugin errors appear.

## Tests

```
node --test extensions/zotero-bridge/test/*.test.js
```

On Node 21 and later the directory form `node --test extensions/zotero-bridge/test/` does
not expand to the test files. On Node 24 it fails with a test-failed entry for the
directory path. Use the glob form above, or `npm test` from this folder.

Files:

- `lib/normalize.js`: normalised title and DOI.
- `lib/duplicates.js`: `classify` and `planImport`.
- `lib/bibkey.js`: reads and validates `BibTeX key: <key>` from Extra.
- `lib/pdf.js`: checks the first five bytes are `%PDF-`.
- `lib/collections.js`: picks a collection by exact name.
- `bootstrap.js`: Zotero glue (install, startup, shutdown, uninstall; menus, pane,
  translator, attachments).
- `prefs.js`: default preference.
- `content/preferences.xhtml`: preferences pane.
- `manifest.json`: plugin manifest.
- `package.json`: marks this folder as CommonJS for the tests. It is not part of the
  plugin manifest.
