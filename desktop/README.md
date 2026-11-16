# Meridian Desktop

A local coordination desktop for Meridian, built with PySide6. It shows the
Meridian areas (Projects, Writing, Zotero, Tunnel, Services, Code, Logs,
Settings) as tabs. The hosted site at usemeridian.us already provides the web
UI, so this app does not reproduce it. It has no web views.

## Run

The environment is managed by pixi (Python 3.12, PySide6 6.12.0). Install it
once with `pixi install`. From the repo root:

    pixi run app

Smoke check. It builds the window and tray, prints "smoke ok" and exits 0
without starting the event loop:

    pixi run smoke

Build the folder-mode bundle, then verify it against the LGPL manifest:

    pixi run build
    pixi run verify

Or run both in one step, build then verify:

    pixi run release

Python 3.12 is the same line the Meridian core uses (3.11 to 3.12 supported).

Settings are stored in `%LOCALAPPDATA%\meridian-desktop\settings.json`, or in
`~/.meridian-desktop/settings.json` when LOCALAPPDATA is unset. The mode is
`local` by default, or `hosted`.

## Tests and boundary check

    pixi run test
    pixi run boundary

## Qt boundary rule

Qt (PySide6) may be imported only in `meridian_desktop/ui/` and in
`meridian_desktop/tray.py`. tray.py is a listed UI exception in
`tools/check_boundary.py`. All other code, including `settings_store.py`, must
not import Qt.

The checker also fails if any file references the meridian-latex engine path.
meridian-latex is not bundled or vendored. A later wave connects the desktop
to a user-installed engine over loopback only.

## Waves (from PLAN.md)

1. Foundation (this wave): Qt shell with eight placeholder tabs, settings
   store, boundary checker, LGPL manifest, bundle verifier, licence drafts, tests.
2. Projects and settings: project list and detail, Hosted/Local switch, client setup.
3. Services, tunnel, logs: local service health, tunnel slots, logs, diagnostics.
4. Zotero: local API status, library search, citation lookup.
5. Writing: DOCX panel, LaTeX panel (engine connection), Google Docs panel.
6. Research, outputs, code: research runs, outputs registry, code search.
7. Release: PyInstaller folder build, then `verify_bundle.py` against the real output.
8. Regression: per-tab smoke tests, boundary check in CI, leak mutation tests.

## Packaging

`pixi run build` runs `packaging/build.py`, which builds a folder (ONEDIR)
bundle with PyInstaller. `pixi run verify` runs `packaging/verify_bundle.py`,
which checks it against `packaging/lgpl_manifest.toml`. `pixi run release` runs
build, then verify. Licence and notice files are in `LICENSES/`, `NOTICE.md`,
`OFFER-SOURCE.md` and `EULA.txt`. The last two are drafts.

Status: wave 1 only. Placeholders make no network calls.
