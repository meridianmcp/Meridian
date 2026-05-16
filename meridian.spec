# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Meridian single-file executable."""

import sys
from pathlib import Path

block_cipher = None

a = Analysis(
    ['meridian/__main__entry.py'],
    pathex=[str(Path('.').resolve())],
    binaries=[],
    datas=[
        ('meridian/templates', 'meridian/templates'),
    ],
    hiddenimports=[
        'meridian',
        'meridian.server',
        'meridian.db',
        'meridian.models',
        'meridian.handoff',
        'meridian.dashboard',
        'meridian.enqueue',
        'meridian.goal_md',
        'aiosqlite',
        'uvicorn',
        'fastapi',
        'jinja2',
        'pydantic',
        'fpdf',
        'watchfiles',
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
    name='meridian',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    onefile=True,
)
