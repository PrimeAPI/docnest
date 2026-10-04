/// <reference types="vitest/config" />
import { cpSync, existsSync, mkdirSync } from "node:fs";
import { resolve } from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

/** Copies pdf.js runtime assets (wasm decoders, cmaps, fonts) so they are served same-origin (CSP). */
function pdfjsAssets(): Plugin {
  return {
    name: "docnest-pdfjs-assets",
    buildStart() {
      const src = resolve(__dirname, "node_modules/pdfjs-dist");
      const dst = resolve(__dirname, "public/pdfjs");
      if (!existsSync(dst)) mkdirSync(dst, { recursive: true });
      for (const dir of ["wasm", "cmaps", "standard_fonts", "iccs"]) {
        if (existsSync(`${src}/${dir}`)) cpSync(`${src}/${dir}`, `${dst}/${dir}`, { recursive: true });
      }
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), pdfjsAssets()],
  resolve: { alias: { "@": resolve(__dirname, "src") } },
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://localhost:8000", changeOrigin: false } },
  },
  build: {
    outDir: "dist",
    assetsDir: "assets",
    sourcemap: false,
    chunkSizeWarningLimit: 2000,
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
