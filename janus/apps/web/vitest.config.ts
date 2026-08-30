import { defineConfig } from "vitest/config";
import { resolve } from "path";

export default defineConfig({
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
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    exclude: ["e2e/**", "node_modules/**"],
  },
});
