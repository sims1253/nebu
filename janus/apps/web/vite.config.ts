import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { TanStackRouterVite } from "@tanstack/router-plugin/vite";
import { resolve } from "path";

const backendProxy = {
  "/api": {
    target: "http://localhost:3001",
    changeOrigin: true,
  },
  "/health": {
    target: "http://localhost:3001",
    changeOrigin: true,
  },
};

export default defineConfig({
  plugins: [
    TanStackRouterVite({ quoteStyle: "double" }),
    react(),
    tailwindcss(),
  ],
  resolve: {
    alias: {
      "~": resolve(import.meta.dirname, "./src"),
      "@janus/contracts": resolve(
        import.meta.dirname,
        "../../packages/contracts/src",
      ),
      "@janus/shared": resolve(
        import.meta.dirname,
        "../../packages/shared/src",
      ),
    },
  },
  server: { port: 3002, proxy: backendProxy },
  preview: { port: 3002, proxy: backendProxy },
});
