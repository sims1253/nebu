// Starts the stack once for the journey: seeds the demo project, then runs `kittum serve` and a
// `kittum worker` beside it, both with the recorded judge and with the source gateway answering
// from the responses recorded in the server's tests (KITTUM_GATEWAY_RECORDINGS), so enrichment and
// open-access retrieval run as they would and nothing touches the network. Returns the teardown.
import { execFileSync, spawn } from "node:child_process";
import type { ChildProcess } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

export const SERVER = join(import.meta.dirname, "..", "..", "server");
export const STACK_FILE = join(import.meta.dirname, ".stack.json");
const DIST = join(import.meta.dirname, "..", "dist");
export const RECORDINGS = join(SERVER, "tests", "enrich", "fixtures", "stack");
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

/** The settings every process of the stack shares, for the CLI steps the journeys take too. */
export const stackEnv = () => ({
  ...process.env,
  // Artifacts belong with their database: a store that lost them breaks every reader of it.
  KITTUM_ARTIFACT_DIR: process.env["KITTUM_ARTIFACT_DIR"] ?? join(tmpdir(), "kittum-e2e-artifacts"),
  KITTUM_JUDGE_BACKEND: "demo",
  KITTUM_WEB_DIST: DIST,
  KITTUM_GATEWAY_RECORDINGS: RECORDINGS,
  // The address the recordings were made with; it is part of each Crossref and Unpaywall URL.
  KITTUM_CONTACT_EMAIL: "test@kittum.dev",
});

export default async function stack() {
  if (!process.env["KITTUM_DATABASE_URL"]) {
    throw new Error("Set KITTUM_DATABASE_URL to a migrated Postgres database for the e2e run.");
  }
  const env = stackEnv();
  // The key that signs the bundle's checkpoint; a real deployment holds it apart from the database.
  const key = join(mkdtempSync(join(tmpdir(), "kittum-e2e-key-")), "checkpoint.pem");
  writeFileSync(
    key,
    execFileSync(
      "uv",
      [
        "run",
        "python",
        "-c",
        "import sys; from cryptography.hazmat.primitives import serialization as s;" +
          " from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey as K;" +
          " sys.stdout.buffer.write(K.generate().private_bytes(s.Encoding.PEM," +
          " s.PrivateFormat.PKCS8, s.NoEncryption()))",
      ],
      { cwd: SERVER },
    ),
  );
  (env as Record<string, string>)["KITTUM_CHECKPOINT_KEY"] = key;
  const name = `End-to-end ${new Date().toISOString()}`;
  // The worker judges, as slowly as a small model, while the reviewers already screen: records
  // enter their queues as the AI reads them, some behind the place they have reached.
  const seed = ["run", "kittum", "demo", "seed", "--json", "--no-wait", "--name", name];
  const seeded = execFileSync("uv", seed, {
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
      env: { ...env, KITTUM_DEMO_JUDGE_DELAY: "0.3" },
      stdio: "inherit",
    }),
  ];
  const [server] = children;
  if (server) await waitFor(`http://127.0.0.1:${PORT}/api/health`, server);
  return () => {
    for (const child of children) child.kill("SIGTERM");
  };
}
