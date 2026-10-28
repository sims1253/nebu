// Reports (DESIGN.md, "Reports"): rendered from the ledger, never typed. The methods lead renders
// the PRISMA 2020 flow and seals the handover bundle; the server refuses anyone else.
import { useRouteContext } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import type { RenderedReport, SealedBundle } from "../../data/schema.ts";
import { run, WorkbenchError } from "../../data/workbench.ts";
import { KeyHint } from "../../ui/Key.tsx";
import { useKeys } from "../../ui/keys.ts";
import { LEAD_ONLY, useProject } from "../project.tsx";

/** The counts of one PRISMA flow, as the `prisma` target writes them. */
interface Flow {
  readonly topic_id: string;
  readonly scope_id: string;
  readonly cutoff_seq: number;
  readonly [count: string]: unknown;
}

const ROWS: ReadonlyArray<readonly [string, string]> = [
  ["records_identified", "Records identified"],
  ["duplicates_removed", "Duplicates removed"],
  ["records_screened", "Records screened"],
  ["records_excluded", "Records excluded"],
  ["records_awaiting_screening", "Records not yet screened"],
  ["reports_sought", "Reports sought for retrieval"],
  ["reports_assessed", "Reports assessed for eligibility"],
  ["reports_excluded", "Reports excluded"],
  ["reports_included", "Reports included"],
  ["studies_included", "Studies included"],
];

export function ReportsScreen() {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me, has, scopeLabel } = useProject();
  const lead = has("lead");
  const caller = { projectId: project.id, as: me.actor_id };
  const [flows, setFlows] = useState<ReadonlyArray<{ flow: Flow; svg: string | null }> | null>(
    null,
  );
  const [bundle, setBundle] = useState<SealedBundle | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Object URLs for the diagrams live as long as the screen shows them.
  useEffect(
    () => () => {
      for (const f of flows ?? []) if (f.svg) URL.revokeObjectURL(f.svg);
    },
    [flows],
  );

  const act = async <A,>(what: string, task: () => Promise<A>): Promise<A | null> => {
    setBusy(what);
    setError(null);
    try {
      return await task();
    } catch (e) {
      setError(e instanceof WorkbenchError ? e.message : `${what} failed.`);
      return null;
    } finally {
      setBusy(null);
    }
  };

  const renderPrisma = () =>
    act("Rendering the PRISMA flow", async () => {
      const rendered: RenderedReport = await run(
        workbench.renderReport({ ...caller, target: "prisma" }),
      );
      const files = rendered.artifact.files;
      const found = await Promise.all(
        files
          .filter((f) => f.media_type === "application/json")
          .map(async (json) => {
            const bytes = await run(workbench.reportFile({ ...caller, sha256: json.sha256 }));
            const flow = JSON.parse(new TextDecoder().decode(bytes)) as Flow;
            const diagram = files.find((f) => f.path === json.path.replace(/\.json$/, ".svg"));
            const svg = diagram
              ? URL.createObjectURL(
                  new Blob(
                    [
                      (await run(
                        workbench.reportFile({ ...caller, sha256: diagram.sha256 }),
                      )) as BlobPart,
                    ],
                    { type: "image/svg+xml" },
                  ),
                )
              : null;
            return { flow, svg };
          }),
      );
      setFlows(found);
    });

  const seal = () =>
    act("Sealing the bundle", async () => setBundle(await run(workbench.sealBundle(caller))));

  useKeys((event) => {
    if (!lead || busy) return false;
    if (event.key === "p") {
      void renderPrisma();
      return true;
    }
    if (event.key === "s") {
      void seal();
      return true;
    }
    return false;
  });

  return (
    <section className="h-full overflow-y-auto bg-paper">
      <div className="mx-auto max-w-[980px] px-10 pt-10 pb-16">
        <div className="flex items-baseline gap-6">
          <h1 className="text-xl font-semibold tracking-[-0.01em]">Reports</h1>
          {lead ? (
            <span className="ml-auto flex gap-5 text-sm">
              <KeyHint keys={["p"]}>Render the PRISMA flow</KeyHint>
              <KeyHint keys={["s"]}>Seal the handover bundle</KeyHint>
            </span>
          ) : null}
        </div>
        {!lead ? (
          <p className="mt-4 max-w-[60ch] text-ink-2">
            Reports show every reviewer&rsquo;s decisions and the AI&rsquo;s readings. {LEAD_ONLY}
          </p>
        ) : (
          <p className="mt-2 max-w-[70ch] text-sm text-ink-2">
            Every count comes from the ledger at the moment of rendering; nobody types one.
          </p>
        )}
        <p className="mt-3 min-h-5 text-sm" aria-live="polite">
          {error ? (
            <span role="alert" className="text-exclude">
              {error}
            </span>
          ) : busy ? (
            <span className="text-ink-2">{busy}.</span>
          ) : null}
        </p>
        {flows?.map(({ flow, svg }) => (
          <section
            key={`${flow.topic_id}-${flow.scope_id}`}
            className="mt-6 border-t border-rule pt-4"
            data-testid="prisma-flow"
            data-scope={flow.scope_id}
          >
            <h2 className="text-md font-semibold">
              PRISMA 2020 flow, {scopeLabel(flow.scope_id)}
              <span className="ml-3 text-xs font-normal text-ink-3">
                as of ledger event {flow.cutoff_seq}
              </span>
            </h2>
            <div className="mt-3 grid grid-cols-[minmax(0,22rem)_minmax(0,1fr)] gap-x-10">
              <dl className="narrow grid grid-cols-[minmax(0,1fr)_auto] gap-x-6 gap-y-1.5 self-start text-sm tabular-nums">
                {ROWS.map(([key, label]) => (
                  <div key={key} className="contents">
                    <dt className="text-ink-2">{label}</dt>
                    <dd className="text-right font-semibold" data-testid={`prisma-${key}`}>
                      {String(flow[key] ?? "")}
                    </dd>
                  </div>
                ))}
              </dl>
              {svg ? (
                <img
                  src={svg}
                  alt={`PRISMA 2020 flow diagram for ${scopeLabel(flow.scope_id)}`}
                  className="w-full bg-sheet"
                />
              ) : null}
            </div>
          </section>
        ))}
        {bundle ? (
          <section className="mt-8 border-t border-rule pt-4" data-testid="bundle">
            <h2 className="text-md font-semibold">Handover bundle sealed</h2>
            <dl className="mt-2 grid grid-cols-[9rem_minmax(0,1fr)] gap-y-1 text-sm">
              <dt className="text-ink-2">On the server at</dt>
              <dd className="break-all" data-testid="bundle-path">
                {bundle.path}
              </dd>
              <dt className="text-ink-2">Through event</dt>
              <dd className="tabular-nums">{bundle.cutoff_seq}</dd>
              <dt className="text-ink-2">Manifest</dt>
              <dd className="break-all text-ink-2">{bundle.manifest_hash}</dd>
              <dt className="text-ink-2">Signing key</dt>
              <dd className="break-all text-ink-2">{bundle.key_id}</dd>
            </dl>
            <p className="mt-3 text-sm text-ink-2">
              Anyone can check it offline: <code>kittum verify {bundle.path}</code>, with the public
              key of this key ID to pin the signer.
            </p>
          </section>
        ) : null}
      </div>
    </section>
  );
}
