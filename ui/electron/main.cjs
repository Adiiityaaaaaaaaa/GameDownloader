const { app, BrowserWindow } = require("electron");
const path = require("path");
const http = require("http");
const { spawn } = require("child_process");

const PORT = 8787;
let mainWindow;
let backend;

// --- start the PyGet Python backend (api_server.py / bundled exe) ----------
function startBackend() {
  const downloadsDir = path.join(app.getPath("downloads"), "RIPTIDE");
  if (app.isPackaged) {
    // Bundled headless backend exe shipped under resources/backend/.
    const exe = path.join(process.resourcesPath, "backend", "pyget-backend.exe");
    backend = spawn(exe, ["--port", String(PORT), "--dest", downloadsDir],
      { stdio: "ignore", windowsHide: true });
  } else {
    // Dev: run the Python source from the downloader project (two levels up).
    const projectDir = path.join(__dirname, "..", "..");
    backend = spawn("python", ["api_server.py", "--port", String(PORT),
      "--dest", downloadsDir],
      { cwd: projectDir, stdio: "inherit", windowsHide: true });
  }
  backend.on("error", (e) => console.error("[backend] failed to start:", e));
}

// Poll /api/health until the backend answers (or we give up).
function waitForBackend(tries = 40) {
  return new Promise((resolve) => {
    const attempt = (n) => {
      const req = http.get(
        { host: "127.0.0.1", port: PORT, path: "/api/health", timeout: 1000 },
        (res) => { res.resume(); resolve(true); },
      );
      req.on("error", () => {
        if (n <= 0) return resolve(false);
        setTimeout(() => attempt(n - 1), 300);
      });
      req.on("timeout", () => { req.destroy(); });
    };
    attempt(tries);
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 960,
    minHeight: 600,
    title: "RIPTIDE",
    backgroundColor: "#0b0f14",
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.cjs"),
    },
    icon: path.join(__dirname, "..", "public", "favicon.ico"),
  });

  const devUrl = process.env.RIPTIDE_VITE; // e.g. http://localhost:5173
  if (devUrl) {
    mainWindow.loadURL(devUrl);
  } else {
    mainWindow.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  }
  mainWindow.on("closed", () => { mainWindow = null; });
}

app.whenReady().then(async () => {
  startBackend();
  await waitForBackend();
  createWindow();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});

app.on("before-quit", () => {
  if (backend && !backend.killed) {
    try { backend.kill(); } catch (_) { /* ignore */ }
  }
});
