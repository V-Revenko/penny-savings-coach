# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['openpyxl']
hiddenimports += collect_submodules('uvicorn')
hiddenimports += collect_submodules('anthropic')


a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[('C:/Users/Vlad Revenko/OneDrive - Washington State University (email.wsu.edu)/GESA Hackathon/gesa-savings-coach/gesa-savings-coach/data', 'data'), ('C:/Users/Vlad Revenko/OneDrive - Washington State University (email.wsu.edu)/GESA Hackathon/gesa-savings-coach/gesa-savings-coach/static', 'static')],
    hiddenimports=hiddenimports,
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
    [],
    exclude_binaries=True,
    name='SavingsCoach',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
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
    upx=True,
    upx_exclude=[],
    name='SavingsCoach',
)
