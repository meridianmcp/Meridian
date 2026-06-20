a = Analysis(
    ['scripts/meridian_connect.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=[
        'meridian.tunnel_client',
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
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas'],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name='meridian-connect',
    debug=False,
    strip=False,
    upx=True,
    console=True,
)
