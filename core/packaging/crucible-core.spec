# -*- mode: python ; coding: utf-8 -*-
"""Deterministic PyInstaller onedir spec for crucible-core (Windows).

Build only via ``core/packaging/build_runtime.py`` with ``cwd`` set to
this directory, so the relative paths below resolve deterministically.
No network, no cross-compilation: run on the target OS with the
project ``.venv`` active.
"""

block_cipher = None

a = Analysis(
    ["core_entry.py"],
    pathex=[],
    binaries=[],
    datas=[
        (
            "../src/crucible_core/migrations",
            "crucible_core/migrations",
        ),
    ],
    hiddenimports=[
        "alembic",
        "alembic.config",
        "anyio",
        "crucible_core.migrations.env",
        "fastapi",
        "platformdirs",
        "sqlalchemy",
        "sqlalchemy.dialects.sqlite",
        "starlette",
        "uvicorn",
        "uvicorn.lifespan.on",
        "uvicorn.loops.asyncio",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.http.httptools_impl",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.protocols.websockets.wsproto_impl",
        "yaml",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="crucible-core",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="crucible-core",
)
