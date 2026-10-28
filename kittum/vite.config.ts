import { defineConfig } from "vite-plus";

const GENERATED = [
  "**/routeTree.gen.ts",
  "**/*.gen.ts",
  "packages/contracts/src/openapi.json",
  // Written by apps/web/scripts from recorded sources.
  "apps/web/src/data/mock/fixtures/**",
];

export default defineConfig({
  fmt: {
    ignorePatterns: [
      // Prose keeps its hand formatting: padded tables make diffs unreadable.
      "**/*.md",
      "apps/server/**",
      "dist",
      "node_modules",
      "pnpm-lock.yaml",
      "*.tsbuildinfo",
      ...GENERATED,
    ],
    sortPackageJson: {},
  },
  lint: {
    ignorePatterns: ["apps/server/**", "dist", "node_modules", ...GENERATED],
    plugins: ["eslint", "oxc", "react", "unicorn", "typescript"],
    rules: {
      "eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
    },
  },
  test: {
    environment: "node",
    allowOnly: !process.env.CI,
    retry: 0,
  },
});
