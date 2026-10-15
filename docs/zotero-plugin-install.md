# Zotero bridge plugin: install guide

The Meridian Zotero Bridge adds two commands to Zotero's right-click menus: import a BibTeX file into a collection, and attach PDFs from a folder by BibTeX key. It also adds a preferences pane.

**Status: not yet run inside Zotero.** The add-on has not been run in a live Zotero install, and the automatic install has not been verified on a real one. Before you use it on a library you care about, run the disposable-profile checklist in `extensions/zotero-bridge/README.md`.

## 1. What it is, and why it never prompts

The bridge runs inside Zotero. It has no server, no file watcher, and no network calls. Every write starts from a menu command you choose. It does not call Zotero's local HTTP API (`127.0.0.1:23119`), so Zotero's Always Allow option does not apply to it. The bridge never asks for permission at runtime. Meridian asks you once whether to install it (section 3); that is the only prompt.

## 2. Requirements

- Zotero 8.0 or later. `manifest.json` sets `strict_min_version` to `8.0`.
- Windows, for the automatic install through the Meridian tray app. This guide covers Windows only.
- For the manual install: a source checkout of this repository. Meridian builds the `.xpi` from `extensions/zotero-bridge`.

## 3. Automatic install (on by default, Windows)

The Meridian tray app asks once, on first launch:

> Install the Zotero bridge plugin? Meridian will place it in Zotero's extensions folder when it starts Zotero, only while Zotero is closed. Zotero's own add-on list can remove it.

- **Install (recommended)** is the default. Enter also chooses it.
- **Not now** opts out. Closing the window also counts as Not now.
- If the tray cannot show the prompt, no answer is saved and the prompt returns on the next launch.

Meridian places the plugin only when it starts Zotero while Zotero is closed. Starting Zotero by hand does not install it. For an immediate install, use section 4.

To change the choice later, use the tray menu item **Auto-install Zotero bridge plugin**. A tick means on. Turning it off stops future automatic installs. It does not remove a plugin that is already installed; see section 6.

The saved choice is in `%USERPROFILE%\.meridian\local_runner\zotero_plugin.json`, or in the folder named by `MERIDIAN_LOCAL_RUNNER_STATE_DIR` when that is set.

Setting `MERIDIAN_ZOTERO_PLUGIN_AUTOINSTALL=0` forces automatic install off, whatever the tray says. `1`, `true`, `yes` and `on` force it on. Any other value is ignored and the tray setting applies. While the variable holds a recognised value, the tray checkbox shows that value.

**Known gap:** the packaged tray build does not include the bridge source. `meridian-tray.spec` bundles only `meridian/static` and `meridian/templates`, so the automatic install cannot build the `.xpi` in that build. Until that is fixed, use the manual install from a source checkout.

## 4. Manual install

Use this if you declined, or if the automatic install did not appear.

1. Close Zotero.
2. From a source checkout, run this in the repository root:

   ```
   pixi run python -m meridian zotero install-plugin
   ```

   It builds `meridian-zotero@usemeridian.us.xpi` into `%USERPROFILE%\.meridian\local_runner\zotero_plugin\`. If it finds your Zotero profile, it also copies the file there. If it does not, it prints the file's path. This command installs even if you declined, because the saved setting only affects automatic installs.
3. In Zotero, open **Tools > Add-ons**, click the gear icon, and choose **Install Add-on From File**.
4. Select `meridian-zotero@usemeridian.us.xpi` from the folder in step 2.
5. Restart Zotero if asked.

## 5. Verify

1. **Tools > Add-ons** lists **Meridian Zotero Bridge**, and it is enabled.
2. Right-click a collection in the left pane. The menu has **Import BibTeX here...**.
3. Select a regular item and right-click it. The menu has **Attach PDFs from folder...**.
4. Zotero's settings include a pane named **Meridian Zotero Bridge**.

## 6. Uninstall

1. In Zotero, open **Tools > Add-ons**, select **Meridian Zotero Bridge**, and remove it. Restart Zotero.
2. Close Zotero. Delete `meridian-zotero@usemeridian.us.xpi` from the `extensions` folder of your Zotero profile. Meridian finds that profile through `profiles.ini` in `%APPDATA%\Zotero\Zotero`.

## 7. Troubleshooting

- **Refused while Zotero is running.** Nothing is written while Zotero is open. The command says "Zotero is running. Close Zotero, then run this command again." Close Zotero and retry. An automatic install skipped for this reason is tried again the next time Meridian starts Zotero while it is closed.
- **No profile found.** Meridian reads `profiles.ini` from the Zotero data folder, which defaults to `%APPDATA%\Zotero\Zotero`. If your Zotero data is somewhere else, set `MERIDIAN_ZOTERO_DATA_DIR` to that folder (the one that contains `profiles.ini`) and retry. You can also point the command at a profile folder with `--profile "<profile folder>"`.
- **Not listed after restart.** The automatic route has not been verified on a real Zotero install. If Meridian Zotero Bridge is not in **Tools > Add-ons** after a restart, use the manual install in section 4, and tell us what **Tools > Add-ons** showed.
