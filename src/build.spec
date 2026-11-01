# -*- mode: python ; coding: utf-8 -*-
import os
import sys

HERE = os.path.abspath(SPECPATH)

datas = [
    (os.path.join(HERE, "orig_entry.bin"), "."),
    (os.path.join(HERE, "get_qq_key.ps1"), "."),
]
binaries = []
hiddenimports = []

try:
    from PyInstaller.utils.hooks import collect_all
    d, b, h = collect_all("sqlcipher3")
    datas += d
    binaries += b
    hiddenimports += h
except Exception as e:
    print("collect_all(sqlcipher3) failed:", e)

hiddenimports += ["tkinter", "tkinter.ttk", "tkinter.messagebox", "tkinter.filedialog"]
hiddenimports += ["pypdf"]  # _merge_pdfs 函数内懒 import，显式打进 exe，保证并行 PDF 路径生效

a = Analysis(
    [os.path.join(HERE, "entry.py")],
    pathex=[HERE],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["numpy", "pandas", "matplotlib", "PIL", "unittest", "pydoc_data"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="QQ聊天记录导出工具_v2.1",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(HERE, "app.ico"),
)
