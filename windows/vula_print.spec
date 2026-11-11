# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Vula Print on Windows.

Onedir mode. Produces dist/VulaPrint/ containing VulaPrint.exe and its
_internal/ runtime. Inno Setup (vula_print.iss) wraps that tree into a
single installer exe.

Build from the repo root:
    pyinstaller --clean --noconfirm windows/vula_print.spec
"""
import os
from PyInstaller.utils.hooks import collect_submodules

# SPECPATH is provided by PyInstaller and points to the directory
# containing this spec file (windows/). Do not use relative paths —
# they resolve against CWD, not the spec's location.
ROOT = os.path.dirname(SPECPATH)

hiddenimports = [
    "win32print",             # Phase B - spooler submission
    "win32crypt",             # Phase D - DPAPI at-rest encryption
    "win32com",               # Phase G - autostart shortcut
    "win32com.client",        # Phase G - WScript.Shell dispatch
    "pythoncom",              # Phase G - COM initialisation
    "pywintypes",             # Phase G - COM exception base
    "serial.tools.list_ports",
    "serial.tools.list_ports_windows",
]
# Some win32com submodules are loaded reflectively by pywin32's own
# dispatch mechanism and cannot be enumerated statically.
hiddenimports += collect_submodules("win32com")


a = Analysis(
    [os.path.join(ROOT, "vula_print_app.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[
        # VERSION is read via Path(__file__).parent / "VERSION" inside
        # the bundle, so it must be in the bundle root.
        (os.path.join(ROOT, "VERSION"), "."),
        # assets/Vula_Logo.png is used for the brand logo fallback
        # and for the tray icon fallback.
        (os.path.join(ROOT, "assets"), "assets"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "matplotlib", "numpy", "scipy", "pandas",
        "pytest", "setuptools", "pip",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VulaPrint",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,           # TEMPORARY: shows crash traceback over SSH
    disable_windowed_traceback=False,
    icon=os.path.join(SPECPATH, "VulaPrint.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="VulaPrint",
)