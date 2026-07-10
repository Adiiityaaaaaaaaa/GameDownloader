# -*- mode: python ; coding: utf-8 -*-
# Headless backend for the RIPTIDE (Electron) UI: the api_server HTTP+SSE bridge
# plus the full download engine. Bundles the same native bits as the GUI build
# (7-Zip, libarchive) but skips GUI-only packages (tkinterdnd2/pystray/PIL) and
# playwright -- browser-gated hosts need a real interactive browser anyway.
import os
import glob
from PyInstaller.utils.hooks import collect_all

datas = [('C:/Program Files/7-Zip/License.txt', '7-zip')]
binaries = [('C:/Program Files/7-Zip/7z.exe', '.'), ('C:/Program Files/7-Zip/7z.dll', '.')]
hiddenimports = ['downloader', 'streaming', 'requests', 'urllib3',
                 'libarchive', 'libarchive.ffi', 'libarchive.checkpoint',
                 'libarchive.read', 'libarchive.write', 'libarchive.entry',
                 'libarchive.extract', 'libarchive.flags', 'libarchive.exception']

tmp_ret = collect_all('curl_cffi')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('certifi')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# Native libarchive DLL + its dependencies, into a bundled 'native' folder.
for _dll in glob.glob(os.path.join(SPECPATH, 'native', '*.dll')):
    binaries += [(_dll, 'native')]


a = Analysis(
    ['api_server.py'],
    pathex=[SPECPATH],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinterdnd2', 'pystray', 'playwright'],
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
    name='pyget-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
