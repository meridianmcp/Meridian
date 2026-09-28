# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the tray/GUI installer (meridian-tray.exe / meridian-tray.app).

4e4c3817 -- see meridian/tray_main.py's own module docstring for the full
design (a single binary that is BOTH the tray icon and, via the internal
--run-server flag, the real Meridian HTTP server LocalRunner spawns as its
own child process).

Unlike meridian.spec (the slim tunnel-only client, which deliberately
EXCLUDES tkinter/PIL/the whole server stack), this build needs the FULL
server -- fastapi/uvicorn/psycopg/etc. -- plus tkinter and PIL for the tray
UI itself. No excludes list here for that reason; the tray exe is
necessarily heavier than the tunnel client.

73257801 -- macOS build added alongside the existing Windows one.
meridian/tray_main.py itself has ZERO platform-specific code (confirmed by
grep for win32/sys.platform/platform.system/darwin) -- this spec is the only
place platform actually matters, since PyInstaller builds natively per-OS
(each CI runner invokes this SAME file with its own sys.platform, so one
conditional spec covers both runners -- no second meridian-tray-mac.spec
file needed, matching meridian.spec/meridian-connect.spec's existing
precedent of one spec reused across every platform's CI job). Two real
platform differences PyInstaller itself can't paper over:

1. pystray's platform backend is a HARD import-time choice --
   ``pystray._win32`` doesn't exist to import on macOS and vice versa, so
   the hiddenimports list below must be platform-conditional or
   PyInstaller's own Analysis step fails outright on whichever platform
   doesn't have the OTHER platform's backend module.
2. On macOS, pystray's menu-bar icon needs to run inside a real ``.app``
   bundle -- a bare onefile Mach-O binary (the Windows shape below) never
   shows a menu-bar item at all. So the macOS build additionally runs
   PyInstaller's onedir + COLLECT + BUNDLE steps to produce
   ``dist/meridian-tray.app`` instead of Windows' single-file onefile EXE.

Ships UNSIGNED / not notarized for now, matching decision 8460f167's
Windows precedent -- signing/notarization (and a real .icns app icon, see
the BUNDLE() call below) are deliberate follow-ups, not launch blockers.
macOS Gatekeeper will quarantine an unsigned downloaded .app (right-click ->
Open bypasses it); that UX gap is tracked separately, not fixed by this spec.
"""

import sys
from pathlib import Path

block_cipher = None

_IS_MACOS = sys.platform == "darwin"

a = Analysis(
    ['meridian/tray_main.py'],
    pathex=[str(Path('.').resolve())],
    binaries=[],
    datas=[
        ('meridian/static/meridian-tray.ico', '.'),
        # meridian/server.py mounts meridian/static/ as a StaticFiles
        # directory at import time (_NoCacheStaticFiles(directory=
        # _resource_path("meridian/static"))) -- confirmed live: the first
        # build of this spec crashed the --run-server child with
        # RuntimeError: Directory '...\meridian\static' does not exist,
        # since only the single .ico file above was bundled. This build
        # embeds the FULL server (unlike the slim meridian.spec, which never
        # serves static files), so the whole directory must come along.
        ('meridian/static', 'meridian/static'),
        # meridian/_deps.py's Jinja2Templates(directory=_resource_path(
        # "meridian/templates")) needs the same treatment -- confirmed live:
        # the second build's server started fine (health check passed) but
        # GET / (landing.html) 500'd until this was added.
        ('meridian/templates', 'meridian/templates'),
    ],
    hiddenimports=[
        'meridian',
        'meridian.tray_main',
        'meridian.__main__',
        'meridian.server',
        'meridian.local_runner',
        # pystray's platform backend is selected by a conditional import
        # inside pystray/__init__.py -- PyInstaller's static analysis should
        # already catch this, but declare it explicitly as a safety net,
        # matching meridian-connect.spec's own precedent for lazily-resolved
        # submodules (httpx/websockets transports below). Platform-
        # conditional (73257801): pystray._darwin doesn't exist to import on
        # Windows and pystray._win32 doesn't exist to import on macOS --
        # declaring the wrong one as a hiddenimport makes PyInstaller's own
        # Analysis step fail outright on that platform, so exactly one of
        # the two is ever listed, never both.
        'pystray._darwin' if _IS_MACOS else 'pystray._win32',
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
        # Uvicorn's own import-string dispatch (uvicorn.Config("meridian.server:app", ...))
        # needs its protocol/loop implementations declared explicitly, same
        # class of gap as httpx/websockets below.
        'uvicorn.loops.auto',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan.on',
        'httpx',
        'httpx._transports.default',
        'httpx._client',
        'websockets',
        'websockets.asyncio',
        'websockets.asyncio.client',
        'websockets.legacy',
        'websockets.legacy.client',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

if _IS_MACOS:
    # onedir + COLLECT + BUNDLE (73257801) -- pystray needs a real .app
    # bundle on macOS for its menu-bar icon to work at all; a bare onefile
    # Mach-O binary (Windows' shape below) never shows a menu-bar item.
    # exclude_binaries=True hands binaries/zipfiles/datas to COLLECT instead
    # of embedding them directly in the EXE -- PyInstaller's own documented
    # onedir-before-BUNDLE pattern.
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='meridian-tray',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        # No console window -- this is a tray/GUI app, not a CLI tool.
        console=False,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name='meridian-tray',
    )
    app = BUNDLE(
        coll,
        name='meridian-tray.app',
        # No .icns yet -- meridian-tray.ico is a Windows icon format PyInstaller's
        # BUNDLE() can't use directly on macOS. A real macOS app icon is a
        # reasonable follow-up (flagged on sprint item 73257801), not required
        # for an unsigned v1 .app -- omitting it just means the generic
        # PyInstaller/Python rocket-ship icon shows in the menu bar/Finder.
        icon=None,
        bundle_identifier='us.usemeridian.tray',
        info_plist={
            # Menu-bar-only app: no Dock icon, no Cmd-Tab entry -- this IS a
            # tray app, it should look like one on macOS too.
            'LSUIElement': True,
            'NSHighResolutionCapable': True,
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name='meridian-tray',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        # No console window -- this is a tray/GUI app, not a CLI tool. Errors
        # before the tray icon itself can show (e.g. a corrupt install) fall
        # back to tray_main.py's own tkinter error dialog rather than a
        # console window nobody will see.
        console=False,
        icon='meridian/static/meridian-tray.ico',
        onefile=True,
    )
