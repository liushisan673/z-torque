# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent

datas = [
    (str(ROOT / "console" / "dist"), "console/dist"),
    (str(ROOT / "alembic"), "alembic"),
    (str(ROOT / "alembic.ini"), "."),
    (str(ROOT / "md-skills"), "md-skills"),
    (
        str(ROOT / "app" / "builtin_plugins" / "audit_export_archive" / "migrations"),
        "app/builtin_plugins/audit_export_archive/migrations",
    ),
]
datas += collect_data_files("app")

packages = (
    "app",
    "fastapi",
    "uvicorn",
    "sqlalchemy",
    "alembic",
    "pydantic_settings",
    "sqlglot",
    "pg8000",
    "pypdf",
    "openpyxl",
    "cryptography",
    "jsonschema",
    "yaml",
    "httpx",
    "multipart",
)
hiddenimports = []
for package in packages:
    hiddenimports += [
        name for name in collect_submodules(package)
        if not name.startswith("alembic.testing")
    ]

a = Analysis(
    [str(ROOT / "desktop_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tests"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ZTorque",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ZTorque",
)
