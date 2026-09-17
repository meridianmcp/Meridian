# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Windows tray/GUI installer (meridian-tray.exe).

4e4c3817 -- see meridian/tray_main.py's own module docstring for the full
design (a single binary that is BOTH the tray icon and, via the internal
--run-server flag, the real Meridian HTTP server LocalRunner spawns as its
own child process).

Unlike meridian.spec (the slim tunnel-only client, which deliberately
EXCLUDES tkinter/PIL/the whole server stack), this build needs the FULL
server -- fastapi/uvicorn/psycopg/etc. -- plus tkinter and PIL for the tray
UI itself. No excludes list here for that reason; the tray exe is
necessarily heavier than the tunnel client.
"""

from pathlib import Path

block_cipher = None

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
        # submodules (httpx/websockets transports below).
        'pystray._win32',
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
