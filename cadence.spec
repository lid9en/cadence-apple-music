# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Cadence.

Produces a single-file Cadence.exe. The web UI is bundled as data, and
pywebview / pythonnet / winrt all need explicit collection because they
load their pieces dynamically at runtime.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

datas = [("cadence/web", "cadence/web")]
binaries = []
hiddenimports = [
    # Imported lazily inside threads, so the analyser cannot see them.
    "winrt.runtime",
    "winrt.system",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.media",
    "winrt.windows.media.control",
    "winrt.windows.storage.streams",
    "PIL.Image",
    "qrcode",
    "qrcode.image.svg",
]

for package in ("webview", "clr_loader", "pythonnet", "winrt", "qrcode"):
    try:
        d, b, h = collect_all(package)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

icon_path = "assets/cadence.ico"
if not Path(icon_path).exists():
    icon_path = None

a = Analysis(
    ["run_cadence.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Cadence",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path,
)
