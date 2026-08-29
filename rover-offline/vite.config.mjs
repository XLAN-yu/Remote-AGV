import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const root = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({
  root,
  base: "./",
  publicDir: "public",
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    assetsDir: "assets",
    target: ["es2020", "safari14"],
    sourcemap: false,
    rollupOptions: {
      output: {
        entryFileNames: "assets/rover-[hash].js",
        chunkFileNames: "assets/chunk-[hash].js",
        assetFileNames: "assets/rover-[hash][extname]",
      },
    },
  },
});
