# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).resolve()
datas = [
    (str(ROOT / "alembic.ini"), "."),
    (str(ROOT / "alembic"), "alembic"),
    (str(ROOT / "app" / "builtin_plugins" / "audit_export_archive" / "migrations"), "app/builtin_plugins/audit_export_archive/migrations"),
    (str(ROOT / "console" / "dist"), "console/dist"),
    (str(ROOT / "md-skills"), "md-skills"),
    (str(ROOT / "build" / "runtime"), "runtime"),
]
binaries = []
hiddenimports = []

# These packages use entry-point discovery, dialect imports or runtime-loaded submodules.
for package in (
    "fastapi",
    "starlette",
    "uvicorn",
    "sqlalchemy",
    "alembic",
    "pydantic",
    "pydantic_core",
    "pydantic_settings",
    "cryptography",
    "jsonschema",
    "referencing",
    "yaml",
    "multipart",
    "httpx",
    "pypdf",
    "openpyxl",
    "pg8000",
    "pymysql",
    "dotenv",
):
    try:
        package_datas, package_binaries, package_hidden = collect_all(package)
    except Exception:
        continue
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

hiddenimports += collect_submodules("app")
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("sqlalchemy.dialects")

a = Analysis(
    [str(ROOT / "desktop_entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
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
    name="Z-Torque",
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
    name="Z-Torque",
)
