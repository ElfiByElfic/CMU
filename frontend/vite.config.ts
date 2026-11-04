import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vývoj: `npm run dev` a backend na :8000 (cmu-backend --config backend/config/demo.yaml).
// Produkce: `npm run build` → dist/, které servíruje backend (staticDir) nebo jakýkoli web server.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: process.env.CMU_BACKEND ?? "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
  build: { outDir: "dist", sourcemap: true },
});
