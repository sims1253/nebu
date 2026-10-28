// Reports (DESIGN.md, "Reports"): rendered from the ledger, never typed. The methods lead renders
// the PRISMA 2020 flow, reads the AI-use disclosure with every prompt the AI roles sent, downloads
// the accepted extraction values, and seals the handover bundle; the server refuses anyone else.
import { Dialog } from "@base-ui/react/dialog";
import { useRouteContext } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import type { ReactNode } from "react";

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

/** A rendered file, ready to show and to download. */
interface Download {
  readonly name: string;
  readonly url: string;
}

interface Agreement {
  readonly kind: "human_human" | "human_ai" | "human_ai_verified";
  readonly raters: readonly [string, string];
  readonly items: number;
  readonly agreed: number;
  readonly table: Record<
    "include_include" | "include_exclude" | "exclude_include" | "exclude_exclude",
    number
  >;
  readonly kappa: number | null;
  readonly pabak: number | null;
}

interface PromptCard {
  readonly role: "judge" | "assistant" | "extraction";
  readonly label: string;
  readonly prompt_version: string | null;
  readonly prompt_hash: string | null;
  readonly recipe_id: string | null;
  readonly model: Record<string, unknown>;
  readonly request_settings: Record<string, unknown>;
  readonly system_prompt: string | null;
  readonly user_prompt_template: string | null;
  readonly first_user_prompt: string | null;
  readonly json_schema: unknown;
  readonly request_sha256: string | null;
  readonly request_form: string | null;
  readonly calls: number;
}

/** The disclosure record, as the `ai_disclosure` target writes it (the parts shown here). */
interface Disclosure {
  readonly cutoff_seq: number;
  readonly prompts: ReadonlyArray<PromptCard>;
  readonly stages: ReadonlyArray<{
    readonly scope_id: string;
    readonly stage: "title_abstract" | "full_text";
    readonly mode: string;
    readonly reveal: string;
    readonly records: number;
    readonly records_read_by_ai: number;
    readonly ai_proposed_exclusions: number;
    readonly excluded: number;
    readonly agreement: ReadonlyArray<Agreement>;
  }>;
}

interface Extracted {
  readonly cutoff_seq: number;
  readonly values: ReadonlyArray<{
    readonly acceptance_id: string;
    readonly study: string;
    readonly field: string;
    readonly value: string | null;
    readonly unit: string | null;
    readonly produced_by: "janus" | "llm" | "manual_edit";
    readonly pages: ReadonlyArray<number>;
    readonly quote: string | null;
    readonly accepted_by: string;
  }>;
}

const STAGES = { title_abstract: "Title and abstract", full_text: "Full text" } as const;
const ROLES = { judge: "AI judge", assistant: "Protocol assistant", extraction: "Extraction" };
const PRODUCED_BY = { janus: "Janus", llm: "LLM", manual_edit: "Edited by hand" } as const;
const AI_READING = {
  human_human: "",
  human_ai: "the AI's own reading",
  human_ai_verified: "the AI's checked reading",
} as const;

const fixed = (n: number | null) => (n === null ? "not defined" : n.toFixed(2));

export function ReportsScreen() {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me, seesLeadData, scopeLabel } = useProject();
  const lead = seesLeadData;
  const caller = { projectId: project.id, as: me.actor_id };
  const [flows, setFlows] = useState<ReadonlyArray<{ flow: Flow; svg: Download | null }> | null>(
    null,
  );
  const [disclosure, setDisclosure] = useState<{
    record: Disclosure;
    files: ReadonlyArray<Download>;
  } | null>(null);
  const [extracted, setExtracted] = useState<{
    record: Extracted;
    files: ReadonlyArray<Download>;
  } | null>(null);
  const [bundle, setBundle] = useState<SealedBundle | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Object URLs for the rendered files live as long as the screen shows them.
  useEffect(
    () => () => {
      for (const f of flows ?? []) if (f.svg) URL.revokeObjectURL(f.svg.url);
    },
    [flows],
  );
  useEffect(
    () => () => {
      for (const f of [...(disclosure?.files ?? []), ...(extracted?.files ?? [])])
        URL.revokeObjectURL(f.url);
    },
    [disclosure, extracted],
  );

  const act = async <A,>(what: string, failed: string, task: () => Promise<A>) => {
    setBusy(what);
    setError(null);
    try {
      return await task();
    } catch (e) {
      setError(e instanceof WorkbenchError ? e.message : failed);
      return null;
    } finally {
      setBusy(null);
    }
  };

  /** Renders a target and fetches every file it wrote. */
  const renderTarget = async (target: string) => {
    const rendered: RenderedReport = await run(workbench.renderReport({ ...caller, target }));
    return Promise.all(
      rendered.artifact.files.map(async (f) => {
        const bytes = await run(workbench.reportFile({ ...caller, sha256: f.sha256 }));
        const type = f.media_type.split(";")[0] ?? "application/octet-stream";
        const name = f.path.split("/").pop() ?? f.path;
        return {
          path: f.path,
          bytes,
          download: { name, url: URL.createObjectURL(new Blob([bytes as BlobPart], { type })) },
        };
      }),
    );
  };
  const json = <T,>(bytes: Uint8Array) => JSON.parse(new TextDecoder().decode(bytes)) as T;

  const renderPrisma = () =>
    act("Rendering the PRISMA flow", "Kittum could not render the PRISMA flow.", async () => {
      const files = await renderTarget("prisma");
      setFlows(
        files
          .filter((f) => f.path.endsWith(".json"))
          .map((f) => ({
            flow: json<Flow>(f.bytes),
            svg: files.find((d) => d.path === f.path.replace(/\.json$/, ".svg"))?.download ?? null,
          })),
      );
    });

  const renderDisclosure = () =>
    act(
      "Rendering the AI-use disclosure",
      "Kittum could not render the AI-use disclosure.",
      async () => {
        const files = await renderTarget("ai_disclosure");
        const record = files.find((f) => f.path.endsWith(".json"));
        if (record)
          setDisclosure({
            record: json<Disclosure>(record.bytes),
            files: files.map((f) => f.download),
          });
      },
    );

  const renderExtracted = () =>
    act(
      "Rendering the extracted values",
      "Kittum could not render the extracted values.",
      async () => {
        const files = await renderTarget("extraction");
        const record = files.find((f) => f.path.endsWith(".json"));
        if (record)
          setExtracted({
            record: json<Extracted>(record.bytes),
            files: files.map((f) => f.download),
          });
      },
    );

  const seal = () =>
    act("Sealing the bundle", "Kittum could not seal the bundle.", async () =>
      setBundle(await run(workbench.sealBundle(caller))),
    );

  useKeys((event) => {
    if (!lead || busy) return false;
    const action = {
      p: renderPrisma,
      a: renderDisclosure,
      e: renderExtracted,
      s: seal,
    }[event.key];
    if (!action) return false;
    void action();
    return true;
  });

  return (
    <section className="h-full overflow-y-auto bg-paper">
      <div className="mx-auto max-w-[1180px] px-10 pt-10 pb-16">
        <div className="flex items-baseline gap-6">
          <h1 className="text-xl font-semibold tracking-[-0.01em]">Reports</h1>
          {lead ? (
            <span className="ml-auto flex flex-wrap justify-end gap-x-5 gap-y-1 text-sm">
              <KeyHint keys={["p"]}>Render the PRISMA flow</KeyHint>
              <KeyHint keys={["a"]}>Show the AI-use disclosure</KeyHint>
              <KeyHint keys={["e"]}>Download the extracted values</KeyHint>
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
            Kittum computes every number from the ledger when you render. Nobody types one.
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
          <PrismaSection
            key={`${flow.topic_id}-${flow.scope_id}`}
            flow={flow}
            svg={svg}
            scope={scopeLabel(flow.scope_id)}
          />
        ))}
        {disclosure ? (
          <DisclosureSection record={disclosure.record} files={disclosure.files} />
        ) : null}
        {extracted ? <ExtractedSection record={extracted.record} files={extracted.files} /> : null}
        {bundle ? (
          <section className="mt-8 border-t border-rule pt-4" data-testid="bundle">
            <h2 className="text-md font-semibold">Handover bundle sealed</h2>
            <dl className="mt-2 grid grid-cols-[9rem_minmax(0,1fr)] gap-y-1 text-sm">
              <dt className="text-ink-2">On the server at</dt>
              <dd className="break-all" data-testid="bundle-path">
                {bundle.path}
              </dd>
              <dt className="text-ink-2">At event</dt>
              <dd className="tabular-nums">{bundle.cutoff_seq}</dd>
              <dt className="text-ink-2">Manifest</dt>
              <dd className="break-all text-ink-2">{bundle.manifest_hash}</dd>
              <dt className="text-ink-2">Signing key</dt>
              <dd className="break-all text-ink-2">{bundle.key_id}</dd>
            </dl>
            <p className="mt-3 text-sm text-ink-2">
              Anyone can check the bundle offline with <code>kittum verify {bundle.path}</code>. Add
              the public key of this key ID to confirm who signed it.
            </p>
          </section>
        ) : null}
      </div>
    </section>
  );
}

function DownloadLink({ file, children }: { file: Download; children?: ReactNode }) {
  return (
    <a
      href={file.url}
      download={file.name}
      className="text-petrol underline decoration-petrol/40 underline-offset-2 hover:decoration-petrol"
    >
      {children ?? file.name}
    </a>
  );
}

/**
 * One scope's PRISMA flow: the diagram at the full width of the page, so its labels read at the
 * default zoom; a click enlarges it, and it downloads as SVG. The counts follow as a list.
 */
function PrismaSection({ flow, svg, scope }: { flow: Flow; svg: Download | null; scope: string }) {
  const [large, setLarge] = useState(false);
  const alt = `PRISMA 2020 flow diagram for ${scope}`;
  return (
    <section
      className="mt-6 border-t border-rule pt-4"
      data-testid="prisma-flow"
      data-scope={flow.scope_id}
    >
      <div className="flex items-baseline gap-4">
        <h2 className="text-md font-semibold">PRISMA 2020 flow, {scope}</h2>
        <span className="text-xs text-ink-3">at ledger event {flow.cutoff_seq}</span>
        {svg ? (
          <span className="ml-auto text-sm">
            <DownloadLink file={{ ...svg, name: `prisma-${flow.scope_id}.svg` }}>
              Download as SVG
            </DownloadLink>
          </span>
        ) : null}
      </div>
      {svg ? (
        <Dialog.Root open={large} onOpenChange={setLarge}>
          <Dialog.Trigger
            className="mt-3 block w-full cursor-zoom-in rounded-[3px] border border-rule bg-white p-2"
            aria-label={`Enlarge the ${alt}`}
          >
            <img src={svg.url} alt={alt} className="w-full" data-testid="prisma-diagram" />
          </Dialog.Trigger>
          <Dialog.Portal>
            <Dialog.Backdrop className="fixed inset-0 bg-[rgb(10_20_24/0.5)]" />
            <Dialog.Popup className="fixed inset-4 overflow-auto rounded-[4px] bg-white p-4 outline-none">
              <Dialog.Title className="sr-only">{alt}</Dialog.Title>
              <div className="flex items-baseline justify-end gap-5 pb-2 text-sm">
                <DownloadLink file={{ ...svg, name: `prisma-${flow.scope_id}.svg` }}>
                  Download as SVG
                </DownloadLink>
                <Dialog.Close className="text-ink-2 hover:text-ink">Close</Dialog.Close>
              </div>
              <img src={svg.url} alt={alt} className="mx-auto min-w-[1400px]" />
            </Dialog.Popup>
          </Dialog.Portal>
        </Dialog.Root>
      ) : null}
      <dl className="narrow mt-4 grid grid-cols-[repeat(2,minmax(0,1fr)_auto)] gap-x-8 gap-y-1.5 text-sm tabular-nums">
        {ROWS.map(([key, label]) => (
          <div key={key} className="contents">
            <dt className="text-ink-2">{label}</dt>
            <dd className="text-right font-semibold" data-testid={`prisma-${key}`}>
              {String(flow[key] ?? "")}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function Text({ label, children }: { label: string; children: string }) {
  return (
    <div className="mt-3">
      <h4 className="text-xs font-medium text-ink-2">{label}</h4>
      <pre className="mt-1 max-h-96 overflow-auto rounded-[3px] bg-sheet-2 p-2.5 text-xs leading-relaxed whitespace-pre-wrap">
        {children}
      </pre>
    </div>
  );
}

/** The AI-use disclosure: what the AI did, agreement as raw counts and kappa, every prompt. */
function DisclosureSection({
  record,
  files,
}: {
  record: Disclosure;
  files: ReadonlyArray<Download>;
}) {
  return (
    <section className="mt-8 border-t border-rule pt-4" data-testid="disclosure">
      <div className="flex items-baseline gap-4">
        <h2 className="text-md font-semibold">AI-use disclosure</h2>
        <span className="text-xs text-ink-3">at ledger event {record.cutoff_seq}</span>
        <span className="ml-auto flex gap-4 text-sm">
          {files.map((f) => (
            <DownloadLink key={f.name} file={f} />
          ))}
        </span>
      </div>
      <p className="mt-2 max-w-[75ch] text-sm text-ink-2">
        People screen every record. The AI never excludes a record. Each reviewer decides before
        Kittum shows them any AI output.
      </p>
      <table className="narrow mt-3 w-full text-sm tabular-nums">
        <thead className="text-left text-xs text-ink-3">
          <tr className="border-b border-rule">
            <th className="py-1 font-normal">Stage</th>
            <th className="py-1 font-normal">Records</th>
            <th className="py-1 font-normal">Read by the AI</th>
            <th className="py-1 font-normal">AI proposed to exclude</th>
            <th className="py-1 font-normal">Excluded by people</th>
          </tr>
        </thead>
        <tbody>
          {record.stages.map((s) => (
            <tr key={`${s.stage}-${s.scope_id}`} className="border-b border-rule">
              <td className="py-1.5">
                {STAGES[s.stage]}, {s.scope_id}
                <span className="block text-xs text-ink-3">
                  {s.mode}, reveal {s.reveal.replace("_", " ")}
                </span>
              </td>
              <td>{s.records}</td>
              <td>{s.records_read_by_ai}</td>
              <td>{s.ai_proposed_exclusions}</td>
              <td>{s.excluded}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3 className="mt-6 text-sm font-semibold">Agreement</h3>
      <p className="mt-1 max-w-[75ch] text-sm text-ink-2">
        Kittum compares the decisions each person made before any AI output was shown. Kappa and
        PABAK cover every record both sides decided, agreed or not.
      </p>
      {record.stages.map((s) => (
        <div key={`${s.stage}-${s.scope_id}-agreement`} className="mt-3">
          <h4 className="text-xs font-medium text-ink-2">
            {STAGES[s.stage]}, {s.scope_id}
          </h4>
          <ul>
            {s.agreement.map((a) => (
              <AgreementRow key={`${a.kind}-${a.raters.join()}`} agreement={a} />
            ))}
          </ul>
        </div>
      ))}

      <h3 className="mt-6 text-sm font-semibold">Prompts</h3>
      <p className="mt-1 max-w-[75ch] text-sm text-ink-2">
        Each AI role this review used, read from the first request it sent. The Audit screen shows
        the exact request and response of every call.
      </p>
      {record.prompts.length === 0 ? (
        <p className="mt-2 text-sm text-ink-2">No AI role has sent a request yet.</p>
      ) : (
        record.prompts.map((card) => (
          <PromptDetails key={`${card.role}-${card.label}`} card={card} />
        ))
      )}
    </section>
  );
}

function AgreementRow({ agreement: a }: { agreement: Agreement }) {
  const second = AI_READING[a.kind] || a.raters[1];
  const cell = "px-2 py-1 text-right tabular-nums";
  return (
    <li
      className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-6 border-b border-rule py-2.5"
      data-testid="disclosure-agreement"
    >
      <div className="text-sm">
        <p>
          {a.raters[0]} and {second}
        </p>
        <p className="text-ink-2 tabular-nums">
          {a.items === 0
            ? "No record decided by both yet."
            : `Agreed on ${a.agreed} of ${a.items} records. Kappa ${fixed(a.kappa)}, PABAK ${fixed(a.pabak)}, both over all ${a.items}.`}
        </p>
      </div>
      <table className="self-start text-xs" aria-label={`${a.raters[0]} against ${second}`}>
        <thead className="text-ink-3">
          <tr>
            <th />
            <th colSpan={2} className="px-2 text-center font-normal">
              {second.charAt(0).toUpperCase() + second.slice(1)}
            </th>
          </tr>
          <tr>
            <th className="px-2 text-left font-normal">{a.raters[0]}</th>
            <th className="px-2 text-right font-normal">include</th>
            <th className="px-2 text-right font-normal">exclude</th>
          </tr>
        </thead>
        <tbody>
          <tr className="border-t border-rule">
            <th className="px-2 text-left font-normal text-ink-2">include</th>
            <td className={`${cell} bg-sheet-2 font-semibold`}>{a.table.include_include}</td>
            <td className={cell}>{a.table.include_exclude}</td>
          </tr>
          <tr className="border-t border-rule">
            <th className="px-2 text-left font-normal text-ink-2">exclude</th>
            <td className={cell}>{a.table.exclude_include}</td>
            <td className={`${cell} bg-sheet-2 font-semibold`}>{a.table.exclude_exclude}</td>
          </tr>
        </tbody>
      </table>
    </li>
  );
}

function PromptDetails({ card }: { card: PromptCard }) {
  const model = card.model;
  const facts: ReadonlyArray<readonly [string, unknown]> = [
    ["Recipe", card.recipe_id],
    ["Model", model["repository"] ?? model["model_as_sent"]],
    ["Revision", model["revision"] ?? "Not recorded for this role"],
    ["Serving", model["serving"]],
    ["Endpoint", (model["endpoints"] as string[] | undefined)?.join(", ")],
    [
      "Model as reported by the server",
      (model["reported_model_identities"] as string[] | undefined)?.join(", "),
    ],
    ["Server options from the recipe", model["extra_body"]],
    ["Prompt version", card.prompt_version],
    ["Calls", card.calls],
  ];
  return (
    <details className="mt-2 border-b border-rule pb-2" data-testid="prompt-card">
      <summary className="cursor-default py-1 text-sm">
        <span className="font-medium">{ROLES[card.role]}</span>, {card.label}
      </summary>
      <dl className="mt-2 grid grid-cols-[minmax(10rem,auto)_minmax(0,1fr)] gap-x-4 gap-y-0.5 text-sm">
        {facts
          .filter(([, v]) => v !== null && v !== undefined && v !== "")
          .filter(([, v]) => typeof v !== "object" || Object.keys(v as object).length > 0)
          .map(([label, v]) => (
            <div key={label} className="contents">
              <dt className="text-ink-2">{label}</dt>
              <dd className="break-all tabular-nums">
                {typeof v === "object" ? JSON.stringify(v) : String(v)}
              </dd>
            </div>
          ))}
      </dl>
      {card.request_sha256 === null ? (
        <p className="mt-2 text-sm text-ink-2">
          Kittum has no stored request for this role, so it cannot show the prompts.
        </p>
      ) : (
        <>
          {Object.keys(card.request_settings).length > 0 ? (
            <Text label="Request settings as sent">
              {JSON.stringify(card.request_settings, null, 2)}
            </Text>
          ) : null}
          {card.system_prompt !== null ? (
            <Text label="System prompt">{card.system_prompt}</Text>
          ) : null}
          {card.user_prompt_template !== null ? (
            <Text label="User prompt template. Kittum fills in the parts in braces for each record.">
              {card.user_prompt_template}
            </Text>
          ) : null}
          {card.first_user_prompt !== null ? (
            <Text label="User prompt of the first call, as sent">{card.first_user_prompt}</Text>
          ) : null}
          {card.json_schema ? (
            <Text label="JSON schema the answer must follow">
              {JSON.stringify(card.json_schema, null, 2)}
            </Text>
          ) : null}
        </>
      )}
    </details>
  );
}

/** The accepted extraction values, one row per study and field, with both files to download. */
function ExtractedSection({
  record,
  files,
}: {
  record: Extracted;
  files: ReadonlyArray<Download>;
}) {
  return (
    <section className="mt-8 border-t border-rule pt-4" data-testid="extracted">
      <div className="flex items-baseline gap-4">
        <h2 className="text-md font-semibold">Extracted values</h2>
        <span className="text-xs text-ink-3">at ledger event {record.cutoff_seq}</span>
        <span className="ml-auto flex gap-4 text-sm">
          {files.map((f) => (
            <DownloadLink key={f.name} file={f} />
          ))}
        </span>
      </div>
      <p className="mt-2 max-w-[75ch] text-sm text-ink-2">
        {record.values.length === 0
          ? "Nobody has accepted an extracted value yet."
          : `${record.values.length} accepted ${record.values.length === 1 ? "value" : "values"}, one row per study and field. Each row has the path that produced the value, its page and quote, and who accepted it when. The sealed bundle carries the same two files.`}
      </p>
      {record.values.length > 0 ? (
        <table className="mt-3 w-full text-sm" data-testid="extracted-values">
          <thead className="text-left text-xs text-ink-3">
            <tr className="border-b border-rule">
              <th className="py-1 font-normal">Study</th>
              <th className="py-1 font-normal">Field</th>
              <th className="py-1 text-right font-normal">Value</th>
              <th className="py-1 pl-4 font-normal">From</th>
              <th className="py-1 font-normal">Source</th>
              <th className="py-1 font-normal">Accepted by</th>
            </tr>
          </thead>
          <tbody>
            {record.values.map((v) => (
              <tr key={v.acceptance_id} className="border-b border-rule align-baseline">
                <td className="py-1.5 pr-3">{v.study}</td>
                <td className="pr-3">{v.field}</td>
                <td className="text-right font-semibold tabular-nums">
                  {v.value ?? "Empty"}
                  {v.unit ? <span className="font-normal text-ink-2"> {v.unit}</span> : null}
                </td>
                <td className="pl-4">{PRODUCED_BY[v.produced_by]}</td>
                <td className="max-w-[22rem] pr-3 text-xs text-ink-2">
                  {v.pages.length ? `Page ${v.pages.join(", ")}. ` : ""}
                  {v.quote ? `"${v.quote}"` : ""}
                </td>
                <td>{v.accepted_by}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </section>
  );
}
