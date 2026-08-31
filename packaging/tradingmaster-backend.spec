# -*- mode: python ; coding: utf-8 -*-
# Build with: cd backend && .venv/Scripts/python.exe -m PyInstaller ../packaging/tradingmaster-backend.spec
# (paths are relative to this file / the repo root, not to wherever PyInstaller is invoked from)

import os

# SPECPATH is the directory containing this file (packaging/), injected by
# PyInstaller itself -- not the file path, so only one dirname() to the repo root.
REPO_ROOT = os.path.dirname(SPECPATH)
BACKEND_DIR = os.path.join(REPO_ROOT, "backend")

a = Analysis(
    ['backend_entry.py'],
    pathex=[BACKEND_DIR],
    binaries=[],
    datas=[(os.path.join(BACKEND_DIR, 'alembic'), 'alembic'), (os.path.join(BACKEND_DIR, 'alembic.ini'), '.')],
    hiddenimports=['app.services.indicators.momentum', 'app.services.indicators.structure', 'app.services.indicators.trend', 'app.services.indicators.volatility', 'app.services.indicators.volume', 'app.services.strategy.sandbox_worker', 'aiosqlite', 'asyncpg', 'sqlalchemy.dialects.sqlite.aiosqlite', 'sqlalchemy.dialects.postgresql.asyncpg', 'jinja2', 'passlib.handlers.bcrypt'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='tradingmaster-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
