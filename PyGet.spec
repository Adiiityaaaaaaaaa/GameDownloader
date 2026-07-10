# -*- mode: python ; coding: utf-8 -*-
import os
import glob
from PyInstaller.utils.hooks import collect_all

datas = [('C:/Program Files/7-Zip/License.txt', '7-zip')]
binaries = [('C:/Program Files/7-Zip/7z.exe', '.'), ('C:/Program Files/7-Zip/7z.dll', '.')]
hiddenimports = []
tmp_ret = collect_all('curl_cffi')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('tkinterdnd2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pystray')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('PIL')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('playwright')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# requests (used by the stream-extract backend) needs certifi's CA bundle.
tmp_ret = collect_all('certifi')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# Stream-extract backend: bundle the native libarchive DLL + its dependencies
# into a 'native' folder (streaming._locate_native_lib looks in sys._MEIPASS/
# native), and include the vendored patched binding + streaming module. We list
# the libarchive submodules explicitly rather than collect_all() them, because
# importing libarchive.ffi at build time would try to LoadLibrary(None) and fail.
for _dll in glob.glob(os.path.join(SPECPATH, 'native', '*.dll')):
    binaries += [(_dll, 'native')]
hiddenimports += [
    'streaming', 'requests', 'urllib3',
    'libarchive', 'libarchive.ffi', 'libarchive.checkpoint',
    'libarchive.read', 'libarchive.write', 'libarchive.entry',
    'libarchive.extract', 'libarchive.flags', 'libarchive.exception',
]


a = Analysis(
    ['downloader.py'],
    pathex=[SPECPATH],
    binaries=binaries,
    datas=datas,
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
    a.binaries,
    a.datas,
    [],
    name='PyGet',
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
