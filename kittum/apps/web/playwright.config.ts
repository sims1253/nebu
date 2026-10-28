// The end-to-end journey on the real stack: Postgres (KITTUM_DATABASE_URL, migrated), `kittum
// serve` with the built workbench, a `kittum worker`, and the AI judge replaying the demo's
// recorded answers, so every run is the same. `pnpm e2e` builds the workbench first.
import { defineConfig } from "@playwright/test";

const PORT = Number(process.env["KITTUM_E2E_PORT"] ?? 8199);

export default defineConfig({
  testDir: "e2e",
  timeout: 240_000,
  expect: { timeout: 15_000 },
  workers: 1,
  reporter: process.env["CI"] ? [["list"], ["html", { open: "never" }]] : "list",
  globalSetup: "./e2e/stack.ts",
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    viewport: { width: 1440, height: 900 },
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
