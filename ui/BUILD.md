# Building the standalone RIPTIDE desktop app

RIPTIDE is an Electron front-end (this `ui/` folder) driving PyGet's Python
download engine through a local HTTP+SSE bridge (`../api_server.py`). The
packaged app bundles a **headless backend exe** so end users need neither
Python nor Node installed.

## Prerequisites (build machine only)
- Python 3.11+ with the engine's deps (`pip install -r ../requirements.txt`,
  plus `pyinstaller`)
- Node 18+ (`npm install` in this folder)
- 7-Zip installed at `C:\Program Files\7-Zip` (bundled into the backend)

## One-time / on backend changes: build the backend exe
From the repo root (`../`):

```
python -m PyInstaller --noconfirm --distpath dist_backend pyget-backend.spec
```

This produces `dist_backend/pyget-backend.exe` (the engine + bridge + libarchive
+ 7-Zip). Stage it for packaging:

```
mkdir -p ui/backend && cp dist_backend/pyget-backend.exe ui/backend/
```

## Build the desktop app
From `ui/`:

```
npm install          # first time
npm run package:win  # vite build + electron-packager (bundles backend/ as a resource)
```

Output: `ui/electron-release/RIPTIDE-win32-x64/RIPTIDE.exe` — a self-contained
app. On launch it starts `resources/backend/pyget-backend.exe` on 127.0.0.1:8787,
waits for `/api/health`, then loads the UI.

## Dev loop (no packaging)
```
npm run electron:dev   # vite build + electron; spawns `python ../api_server.py`
```
(Requires Python on PATH. The window won't appear if `ELECTRON_RUN_AS_NODE` is
set in your shell — unset it.)
