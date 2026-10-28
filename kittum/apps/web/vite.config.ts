import tailwindcss from "@tailwindcss/vite";
import { tanstackRouter } from "@tanstack/router-plugin/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite-plus";
import { playwright } from "vite-plus/test/browser-playwright";

const apiTarget = process.env.KITTUM_API_URL ?? "http://127.0.0.1:8100";

export default defineConfig({
  plugins: [tanstackRouter({ target: "react", autoCodeSplitting: true }), react(), tailwindcss()],
  server: {
    port: Number(process.env.PORT ?? 5180),
    proxy: { "/api": apiTarget },
  },
  test: {
    include: ["src/**/*.test.{ts,tsx}"],
    // The flows run in a real Chromium at a laptop size, driven by real key events.
    browser: {
      enabled: true,
      headless: true,
      provider: playwright(),
      instances: [{ browser: "chromium", viewport: { width: 1440, height: 900 } }],
    },
  },
});
