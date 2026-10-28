// Starts the stack once for the journey: seeds the demo project, then runs `kittum serve` and a
// `kittum worker` beside it, both with the recorded judge. Returns the teardown.
import { execFileSync, spawn } from "node:child_process";
import type { ChildProcess } from "node:child_process";
import { writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

export const SERVER = join(import.meta.dirname, "..", "..", "server");
export const STACK_FILE = join(import.meta.dirname, ".stack.json");
const DIST = join(import.meta.dirname, "..", "dist");
const PORT = Number(process.env["KITTUM_E2E_PORT"] ?? 8199);

const waitFor = async (url: string, child: ChildProcess) => {
  for (let i = 0; i < 120; i++) {
    if (child.exitCode !== null) throw new Error(`the server exited with ${child.exitCode}`);
    try {
      if ((await fetch(url)).ok) return;
    } catch {
      // not up yet
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`${url} did not answer`);
};

export default async function stack() {
  if (!process.env["KITTUM_DATABASE_URL"]) {
    throw new Error("Set KITTUM_DATABASE_URL to a migrated Postgres database for the e2e run.");
  }
  const env = {
    ...process.env,
    // Artifacts belong with their database: a store that lost them breaks every reader of it.
    KITTUM_ARTIFACT_DIR:
      process.env["KITTUM_ARTIFACT_DIR"] ?? join(tmpdir(), "kittum-e2e-artifacts"),
    KITTUM_JUDGE_BACKEND: "demo",
    KITTUM_WEB_DIST: DIST,
  };
  const name = `End-to-end ${new Date().toISOString()}`;
  const seeded = execFileSync("uv", ["run", "kittum", "demo", "seed", "--json", "--name", name], {
    cwd: SERVER,
    env,
    encoding: "utf8",
  });
  writeFileSync(STACK_FILE, seeded);

  const children = [
    spawn("uv", ["run", "kittum", "serve", "--port", String(PORT)], {
      cwd: SERVER,
      env,
      stdio: "inherit",
    }),
    spawn("uv", ["run", "kittum", "worker", "--name", "e2e"], {
      cwd: SERVER,
      env,
      stdio: "inherit",
    }),
  ];
  const [server] = children;
  if (server) await waitFor(`http://127.0.0.1:${PORT}/api/health`, server);
  return () => {
    for (const child of children) child.kill("SIGTERM");
  };
}
