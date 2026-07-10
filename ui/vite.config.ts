import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Electron loads dist/ over file://, where a `crossorigin` module script fails
// its CORS check (null origin) and the app renders blank. Strip it.
function stripCrossorigin(): Plugin {
  return {
    name: "strip-crossorigin",
    transformIndexHtml(html) {
      return html.replace(/\s+crossorigin/g, "");
    },
  };
}

// Plain Vite + React SPA (Electron loads the static dist/ build). base "./" so
// asset URLs are relative, which is required when loaded from file:// in Electron.
export default defineConfig({
  base: "./",
  plugins: [react(), tailwindcss(), stripCrossorigin()],
  build: { outDir: "dist", emptyOutDir: true },
  server: { port: 5173 },
});
