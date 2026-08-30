import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright configuration for Janus E2E tests. Boots the FastAPI backend on
 * :3001 and the Vite frontend on :3000.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: !!process.env["CI"],
  retries: process.env["CI"] ? 2 : 1,
  workers: 1,
  reporter: "html",
  timeout: 60_000,

  use: {
    baseURL: "http://localhost:3000",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },

  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],

  webServer: [
    {
      name: "backend",
      command:
        "cd ../server && .venv/bin/uvicorn janus.main:app --host 127.0.0.1 --port 3001",
      url: "http://localhost:3001/health",
      reuseExistingServer: true,
      timeout: 30_000,
    },
    {
      name: "frontend",
      command: "npx vite --port 3000",
      url: "http://localhost:3000",
      reuseExistingServer: true,
      timeout: 30_000,
    },
  ],
});
