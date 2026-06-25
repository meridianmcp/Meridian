# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the slim Meridian tunnel-client executable.

The downloadable ``meridian`` binary (``meridian.exe`` / ``meridian-linux`` /
``meridian-mac-*``) is only ever used as the Pro filesystem tunnel client
(``meridian --tunnel --repo .``). It does NOT need to embed the FastAPI/uvicorn
server, psycopg3, langgraph, the hosted/billing routes or any DB stack.

So this spec builds from the minimal ``meridian/tunnel_main.py`` entry point and
aggressively excludes the server-only deps + heavy transitive libraries, with
UPX compression on top. Target: under 20 MB. The full desktop/server binary, if
ever needed, has its own entry point (``meridian/__main__entry.py``).
"""

from pathlib import Path

block_cipher = None

# Server-only / heavy modules the tunnel client never touches. Excluding them
# keeps PyInstaller from following imports into the whole server dependency tree.
_EXCLUDES = [
    # Meridian server-side modules
    'meridian.server',
    'meridian.pg_adapter',
    'meridian.hosted',
    'meridian.db',
    'meridian.dashboard',
    'meridian.goal_md',
    'meridian.handoff',
    'meridian.enqueue',
    'meridian.models',
    # Web server stack
    'fastapi',
    'uvicorn',
    'starlette',
    'jinja2',
    'slowapi',
    # Auth / billing / email
    'authlib',
    'itsdangerous',
    'resend',
    'bcrypt',
    'stripe',
    # Databases
    'psycopg',
    'psycopg_pool',
    'aiosqlite',
    'sqlite3',
    # LLM / agent graph
    'langgraph',
    'langchain',
    'langchain_core',
    'anthropic',
    'mcp',
    # Tree-sitter grammars (server-side claim_file parsing only)
    'tree_sitter',
    'tree_sitter_python',
    'tree_sitter_javascript',
    'tree_sitter_typescript',
    'tree_sitter_c',
    'tree_sitter_cpp',
    'tree_sitter_go',
    'tree_sitter_rust',
    'tree_sitter_java',
    'tree_sitter_c_sharp',
    # Misc heavy / unused
    'fpdf',
    'watchfiles',
    'pydantic',
    'tkinter',
    'matplotlib',
    'numpy',
    'pandas',
    'PIL',
    'PyQt5',
    'PySide2',
]

a = Analysis(
    ['meridian/tunnel_main.py'],
    pathex=[str(Path('.').resolve())],
    binaries=[],
    datas=[],
    hiddenimports=[
        'meridian',
        'meridian.tunnel_main',
        'meridian.tunnel_client',
        'meridian.serena_pool',
        # httpx / websockets are imported lazily inside run_tunnel; declare the
        # transport submodules so the frozen binary can find them at runtime.
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
    excludes=_EXCLUDES,
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
    name='meridian',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    onefile=True,
)
