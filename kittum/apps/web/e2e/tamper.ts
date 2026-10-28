// Edits the ledger as the database superuser would: with the append-only trigger out of the way
// (`session_replication_role = replica`), straight in Postgres. Only the audit journey uses it,
// to show that the Audit screen pinpoints the edit.
import { execFileSync } from "node:child_process";

import { SERVER, stackEnv } from "./stack.ts";

const SCRIPT = `
import json, os, sys
import psycopg
with psycopg.connect(os.environ["KITTUM_DATABASE_URL"], autocommit=True) as c:
    c.execute("set session_replication_role = replica")
    c.execute(sys.argv[1], json.loads(sys.argv[2]))
`;

/** Runs one SQL statement with `%s` parameters as the superuser, triggers off. */
const superuser = (statement: string, params: ReadonlyArray<unknown>) =>
  execFileSync("uv", ["run", "python", "-c", SCRIPT, statement, JSON.stringify(params)], {
    cwd: SERVER,
    env: stackEnv(),
  });

/** Overwrites one key of an event's payload and leaves every hash as it was. */
export const forgePayload = (projectId: string, seq: number, key: string, value: unknown) =>
  superuser(
    "update audit_event set payload = jsonb_set(payload, %s::text[], %s::jsonb)" +
      " where project_id = %s and seq = %s",
    [`{${key}}`, JSON.stringify(value), projectId, seq],
  );
