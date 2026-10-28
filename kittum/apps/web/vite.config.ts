import tailwindcss from "@tailwindcss/vite";
import { tanstackRouter } from "@tanstack/router-plugin/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite-plus";

const apiTarget = process.env.KITTUM_API_URL ?? "http://127.0.0.1:8100";

export default defineConfig({
  plugins: [tanstackRouter({ target: "react", autoCodeSplitting: true }), react(), tailwindcss()],
  server: {
    port: Number(process.env.PORT ?? 5180),
    proxy: { "/api": apiTarget },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
