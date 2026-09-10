import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The build lands in app/static, which FastAPI serves. The dev server proxies the API routes to
// `make run` so the page stays same-origin; the lookahead keeps /index.html out of the proxy.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: { outDir: "../app/static", emptyOutDir: true },
  server: {
    proxy: { "^/(health|repos|index|ask)(?![.\\w])": "http://localhost:8000" },
  },
});
