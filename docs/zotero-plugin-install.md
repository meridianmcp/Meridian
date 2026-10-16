# Zotero bridge plugin: install guide

The Meridian Zotero Bridge adds two commands to Zotero's right-click menus: import a BibTeX file into a collection, and attach PDFs from a folder by BibTeX key. It also adds a preferences pane.

**Status: not yet run inside Zotero.** The add-on has not been run in a live Zotero install, and the automatic install has not been verified on a real one. Before you use it on a library you care about, run the disposable-profile checklist in `extensions/zotero-bridge/README.md`.

## 1. What it is, and why it never prompts

The bridge runs inside Zotero. It has no server and no file watcher, and it makes no network calls of its own. Zotero itself checks the `update_url` in `manifest.json` for add-on updates. That URL is a placeholder, and no real update manifest is published there yet. Every write starts from a menu command you choose. It does not call Zotero's local HTTP API (`127.0.0.1:23119`), so Zotero's Always Allow option does not apply to it. The bridge never asks for permission at runtime. Meridian asks you once whether to install it (section 3); that is the only prompt.

## 2. Requirements

- Zotero 8 or later (the manifest's minimum). `manifest.json` sets `strict_min_version` to `8.0`.
- Windows, for the automatic install through the Meridian tray app. This guide covers Windows only.
- For the manual install: a source checkout of this repository. Meridian builds the `.xpi` from `extensions/zotero-bridge`.

## 3. Automatic install (Windows)

The Meridian tray app asks once, on first launch:

> Install the Zotero bridge plugin now? Meridian will close Zotero if it is open, install the plugin into Zotero's extensions folder, and restart Zotero if Meridian closed it. Zotero's own add-on list can remove it later.

- The prompt defaults to **Install (recommended)**. Enter also chooses it.
- **Not now** opts out. Closing the window also counts as Not now.
- If the tray cannot show the prompt, no answer is saved and the prompt returns on the next launch.

Nothing is installed until you have answered the prompt. Answering **Install** installs the plugin at once, unless `MERIDIAN_ZOTERO_PLUGIN_AUTOINSTALL` turns automatic installs off (see below). The packaged app does this itself. Closing Zotero is part of that install by default: if Zotero is open, Meridian asks it to close, installs the plugin, and restarts Zotero, but only when Meridian closed it. An install that finds the plugin already current does not close Zotero. Meridian never force-closes Zotero. If Zotero does not close within about 20 seconds, for example because it shows a dialog or is busy, nothing is installed and nothing is written. Close Zotero yourself, then choose **Install Zotero plugin now** in the tray menu. If Meridian cannot check whether Zotero is running, nothing is written either, and the dialog says so. The choice is saved either way.

When Meridian starts Zotero from a source checkout, it places the plugin first, while Zotero is closed. This route never closes Zotero: if Zotero is open, nothing is written. Starting Zotero by hand does not install it. The packaged app does not start Zotero, so this route does not apply there. Section 4 is the manual route.

**Close Zotero to install the plugin** is the tray menu item that controls the close step. A tick means on, which is the default. Turning it off means an install with Zotero open is refused, and you close Zotero yourself. The choice is saved as `close_zotero_for_install` in the same settings file. It does not change whether auto-install is on, and it does not change your answer to the first-run prompt. Closing happens only during an install you start: answering **Install** to the first-run prompt, choosing **Install Zotero plugin now**, or running the manual command in section 4. `MERIDIAN_ZOTERO_CLOSE_ON_INSTALL` can only turn closing off. `0`, `false`, `no` and `off` turn it off even when the saved choice is on. `1`, `true`, `yes` and `on` change nothing, so a saved off stays off. Any other value is ignored. While the variable holds an off value, the menu item says that the variable overrides it. Its checkbox still shows your saved choice. Meridian never force-closes Zotero, whatever this setting says. It sends the same close request as Zotero's own close button, and the taskkill argument list has no force flag. A close request can still time out, for example when Zotero shows a dialog or is busy. Then nothing is installed and nothing is written.

To change the choice later, use the tray menu item **Auto-install Zotero bridge plugin**. A tick means on. Turning it off stops future automatic installs. It does not remove a plugin that is already installed; see section 6. **Install Zotero plugin now** (Windows) runs one install on demand and reports the result in a dialog. It ignores the saved choice and `MERIDIAN_ZOTERO_PLUGIN_AUTOINSTALL`, because you asked for it. It follows the close choice above.

The saved choice is in `%USERPROFILE%\.meridian\local_runner\zotero_plugin.json`, or in the folder named by `MERIDIAN_LOCAL_RUNNER_STATE_DIR` when that is set.

`MERIDIAN_ZOTERO_PLUGIN_AUTOINSTALL` controls automatic installs only: the install that runs before Meridian starts Zotero from a source checkout, and the install that follows the first-run **Install** answer. It does not change **Install Zotero plugin now** or the manual command in section 4, which run because you asked for them. The value is trimmed of spaces and matched without regard to case. These values force automatic installs **off**: `0`, `false`, `no`, `off`. These values force them **on**, but only after you have answered the prompt: `1`, `true`, `yes`, `on`. Any other value, or an empty one, is ignored and the tray setting applies. When the variable forces automatic installs off, answering **Install** still saves your choice, but nothing is installed until you choose **Install Zotero plugin now**. While the variable holds a recognised value, the tray menu item says that the variable overrides it. Its checkbox still shows your saved choice.

The packaged Windows tray app includes the bridge source, so the Install answer and **Install Zotero plugin now** work from the installed app. The manual install in section 4 still needs a source checkout.

## 4. Manual install

Use this if you declined, or if the automatic install did not appear.

1. Close Zotero, or let the command close it. The command below closes a running Zotero by itself, unless the saved **Close Zotero to install the plugin** choice in section 3 is off, the `MERIDIAN_ZOTERO_CLOSE_ON_INSTALL` override is set to an off value, or you add `--keep-zotero-open`. With closing off, the command refuses while Zotero runs, so close Zotero first. A close request can still time out, and then nothing is installed.
2. From a source checkout, run this in the repository root:

   ```
   pixi run python -m meridian zotero install-plugin
   ```

   It builds the unsigned bundle `meridian-zotero@usemeridian.us.xpi` into `%USERPROFILE%\.meridian\local_runner\zotero_plugin\`. The unsigned build writes nothing to the Zotero profile. The command copies a file into the profile only when the Mozilla-signed file for this plugin version is stored (section 4a). Without one, it prints the signing command and the path of the unsigned file, and you can install that file by hand in steps 3 and 4. This command installs even if you declined, because the saved setting only affects automatic installs. It copies the file only into a folder that holds `prefs.js` or `zotero.sqlite`, so a wrong `--profile` path writes nothing. It follows the close choice in section 3. Add `--keep-zotero-open` to refuse the install while Zotero is open, for this run only.
3. In Zotero, open **Tools > Plugins**, click the gear icon, and choose **Install Plugin From File**.
4. Select `meridian-zotero@usemeridian.us.xpi` from the folder in step 2.
5. Restart Zotero if asked.

## 4a. Signing the plugin (Mozilla, unlisted)

Firefox and Zotero load add-ons that Mozilla has signed. Meridian signs the bridge once per plugin version, as a separate step. Installing never signs, and an install never waits on Mozilla.

1. Create a Mozilla developer account on addons.mozilla.org (the developer hub).
2. In the developer hub, open the **API Keys** page and create a key pair. Mozilla shows the key (the JWT issuer) and the secret (the JWT secret). Copy both now.
3. Set both as **user** environment variables, never in a file or in chat: `WEB_EXT_API_KEY` for the key and `WEB_EXT_API_SECRET` for the secret. On Windows, open **Edit environment variables for your account**, add both under **User variables**, then open a new terminal.
4. From a source checkout, with Node.js and `npx` on your PATH, run this in the repository root:

   ```
   pixi run python -m meridian zotero sign-plugin
   ```

   `--dry-run` prints the plugin version, whether the two variables are set (yes or no, never the values), and whether a signed file is already stored. It signs nothing, uploads nothing, and writes nothing. `--timeout-minutes N` changes the default wait of 60 minutes. N must be more than 0 and at most 1440. Any other value is refused before anything is staged or uploaded. A negative value may be written with an equals sign, as `--timeout-minutes=-5`.
5. Wait for Mozilla. The command returns once Mozilla has signed the file, or stops at the timeout. A timeout or a failure stores no signed file. It may still have submitted this version to Mozilla, so the version number is used up. The message says: Bump the version in manifest.json to sign a new build.
6. Run `pixi run python -m meridian zotero install-plugin` (section 4). It copies the signed file into Zotero's profile.

What signing does, and what it does not:

- **Signing does not close Zotero.** Its network use is this. web-ext is pinned to 8.10.0, but the first run fetches that package, and its npm dependencies, from the npm registry, and those dependencies are not pinned. The signing call then uploads the bundle to Mozilla's addons.mozilla.org API. Signing writes to two places: Meridian's state folder, `%USERPROFILE%\.meridian\local_runner\zotero_plugin\` (the signed file in `signed\` and the record in `signed.json`), and the system temporary folder, which holds the staged copy and web-ext's output while it runs. npm and npx write their own files to the npm cache.
- **Installing does close Zotero**, under the rules in section 3. It copies only the signed file, and only when that file was signed for this plugin version and this exact source. The copy reads the file once, checks its sha256 against the record, and writes those same bytes. With no matching signed file, the install writes nothing, closes nothing, and prints the signing command. A stored file that no longer matches its record gets its own message. When the check catches it before Zotero is closed, the install stops there. A version that was submitted without a verified signed file gets the bump message instead, and nothing is closed. Each copy writes only bytes that match the record.
- **One submission per version number.** Before it uploads anything, `sign-plugin` writes a `submitted` record for the version, with the source it was given. A run for a version that already has a signed file for this same source reports it as already signed, exits 0, and uploads nothing. Any other later run for that version is refused with: This version has already been submitted to Mozilla. Bump the version in manifest.json to sign a new build. That refusal applies when the source has changed since the signed file was made, and after a failure or a timeout whatever the source, because Mozilla may already hold the version. So the version number is used up. A new build needs a new version number in `manifest.json`.
- **Deleting `signed.json` removes the guarantee.** Deleting `signed.json` removes the guarantee against resubmitting a version that was submitted without a signed file. Meridian cannot detect a fully deleted record, so it cannot refuse a second submission of that version. A stored signed file with no record is refused too, so a signed version stays refused while its file is kept. Install is also blocked after the record is deleted, while the signed file is kept: the install reports the version as used and says to bump it. To sign again, bump the version in `manifest.json`.
- **The one-submission guarantee lives in the state folder.** The record and the signed file are in the state folder, `%USERPROFILE%\.meridian\local_runner\zotero_plugin\`, or in the folder named by `MERIDIAN_LOCAL_RUNNER_STATE_DIR` when that is set. A different machine, a different Windows user, or a `MERIDIAN_LOCAL_RUNNER_STATE_DIR` override starts with no record, so the same version can be submitted to Mozilla again there.
- **Version numbers are matched exactly.** A version number is matched exactly as written, so `0.1` and `0.1.0` are different versions. Mozilla's behaviour for a near-duplicate version is not verified, so do not sign a new build under a number that differs only by trailing zeros.
- **Earlier builds are not protected.** A version that was submitted to Mozilla before this record existed, in an earlier build, is not protected, because Meridian has no record of that submission. Bump that version before you sign again.
- **Two runs at the same time are not guarded.** `sign-plugin` checks the records, and then writes the `submitted` record, as two steps. Two `sign-plugin` runs for the same version at the same time can both pass the check before either writes its record, so both can submit. This build does not lock against it. Run one signing at a time.
- **The key and the secret are never written to a file, printed, or put on a command line.** The signing call receives them in its environment only. Other `WEB_EXT_*` variables are not passed to that call, so none of them can redirect the credentials to another server.
- **One forced kill, for the signing child only.** If Mozilla does not finish within the wait, `sign-plugin` ends the signing child's own process tree, with `taskkill /PID <child> /T /F` on Windows, so that a hung web-ext cannot block the command. This is the only forced kill in the signing path. It touches only the signing child's process tree, and never Zotero or any other process. Zotero's own close step is a plain close request and never forces anything.
- **Fallback: install the unsigned file yourself.** The install prints the path of the unsigned file. In Zotero, open **Tools > Plugins**, click the gear icon, choose **Install Plugin From File**, and select that file. Zotero may refuse an unsigned add-on in a normal build. The menu names used in this guide and that refusal are unverified on Zotero 10.0.6.

**Not yet verified:** Mozilla's unlisted signing of this bridge has not been run against Mozilla. Zotero accepting a signed `.xpi` copied into a profile's `extensions` folder is not yet verified on a real Zotero install.

## 5. Verify

1. **Tools > Plugins** lists **Meridian Zotero Bridge**, and it is enabled.
2. Right-click a collection in the left pane. The menu has **Import BibTeX here...**.
3. Select a regular item and right-click it. The menu has **Attach PDFs from folder...**.
4. Zotero's settings include a pane named **Meridian Zotero Bridge**.

## 6. Uninstall

1. **Turn off auto-install first.** In the tray menu, clear **Auto-install Zotero bridge plugin**. If `MERIDIAN_ZOTERO_PLUGIN_AUTOINSTALL` is set, unset it too, because it overrides the menu. Otherwise Meridian puts the file back the next time it starts Zotero while Zotero is closed. Do not choose **Install Zotero plugin now** either, since it puts the file back too.
2. In Zotero, open **Tools > Plugins**, select **Meridian Zotero Bridge**, and remove it. Restart Zotero.
3. Close Zotero. Delete `meridian-zotero@usemeridian.us.xpi` from the `extensions` folder of your Zotero profile. Meridian finds that profile through `profiles.ini` in `%APPDATA%\Zotero\Zotero`.

## 7. Troubleshooting

- **Refused while Zotero is running.** Nothing is written while Zotero is open. The command says "Zotero is running. Close Zotero, then run this command again." This happens when closing is turned off (the tray item, the saved setting, the environment variable, or `--keep-zotero-open`), and on macOS or Linux, where Meridian does not close Zotero. Close Zotero and retry. Close Zotero, then choose **Install Zotero plugin now** in the tray menu. An install skipped at Zotero's start from a source checkout is tried again the next time Meridian starts Zotero while it is closed.
- **Zotero did not close.** Meridian asked Zotero to close and waited about 20 seconds, but Zotero is still open. Nothing was installed and nothing was written. Close Zotero yourself, saving or dismissing any dialog it shows, then choose **Install Zotero plugin now** in the tray menu. Meridian does not force-close Zotero.
- **Zotero closed but did not restart.** The plugin was installed, and Zotero was closed for it. Start Zotero yourself.
- **Could not check whether Zotero is running.** Meridian reads Zotero's process list before it writes. If that read fails, nothing is written, and the dialog or command says it could not check. This is not the same as Zotero running. Make sure Zotero is closed, then try again with **Install Zotero plugin now** in the tray menu.
- **Your choice could not be saved.** Meridian shows the error, and nothing is installed. The prompt asks again on the next launch. Once the settings folder is writable, you can choose **Install Zotero plugin now** in the tray menu.
- **No profile found.** Meridian reads `profiles.ini` from the Zotero data folder, which defaults to `%APPDATA%\Zotero\Zotero`. If your Zotero data is somewhere else, set `MERIDIAN_ZOTERO_DATA_DIR` to that folder (the one that contains `profiles.ini`) and retry. You can also point the command at a profile folder with `--profile "<profile folder>"`.
- **Not a Zotero profile.** The command refuses a folder that holds neither `prefs.js` nor `zotero.sqlite`, and writes nothing. Point `--profile` at the profile folder itself.
- **Nothing happens on macOS or Linux.** The tray prompt, the immediate install, the tray menu items and the close step are Windows only in this build. Use the manual install in section 4.
- **Not listed after restart.** The automatic route has not been verified on a real Zotero install. If Meridian Zotero Bridge is not in **Tools > Plugins** after a restart, use the manual install in section 4, and tell us what **Tools > Plugins** showed.
