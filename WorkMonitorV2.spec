# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


project_dir = Path(SPECPATH)

# build.ps1 stages a copy of the interpreter's Tcl/Tk runtime here. Bundle it
# explicitly so a frozen build works on machines without Python installed.
tcl_root = project_dir / "build_resources" / "tcl"
tcl_datas = [
    (str(tcl_root / folder), f"tcl/{folder}")
    for folder in ("tcl8.6", "tk8.6")
    if (tcl_root / folder).is_dir()
]

a = Analysis(
    [str(project_dir / "main.py")],
    pathex=[str(project_dir)],
    binaries=[],
    datas=[
        (str(project_dir / "assets" / "default_config.json"), "assets"),
        (str(project_dir / "assets" / "work_monitor.ico"), "assets"),
        *tcl_datas,
    ],
    hiddenimports=["PIL._tkinter_finder", "pystray._win32"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="WorkMonitorV2",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX-packed binaries that install global input hooks are frequently
    # flagged by antivirus heuristics.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(project_dir / "assets" / "work_monitor.ico")],
)
