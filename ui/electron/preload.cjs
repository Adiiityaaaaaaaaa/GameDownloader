const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("electronAPI", {
  platform: process.platform,
  // Native folder picker for the download directory (returns a path or null).
  pickFolder: () => ipcRenderer.invoke("pick-folder"),
});
