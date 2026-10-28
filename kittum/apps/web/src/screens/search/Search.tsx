// The searches as run (DESIGN.md, "Search"): one execution per database and strategy, with what
// PRISMA-S asks for: the platform, the verbatim strategy, limits, dates, and both counts.
import { useRouteContext } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";

import type { SearchExecution } from "../../data/schema.ts";
import { run, WorkbenchError } from "../../data/workbench.ts";
import { KeyHint } from "../../ui/Key.tsx";
import { useKeys } from "../../ui/keys.ts";
import { LEAD_ONLY, useProject } from "../project.tsx";
import { ImportSearch, RunSearch } from "./SearchForms.tsx";

const when = (iso: string) =>
  new Date(iso).toLocaleString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
    timeZoneName: "short",
  });

export function SearchScreen() {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me, seesLeadData, protocol } = useProject();
  const lead = seesLeadData;
  const [executions, setExecutions] = useState<ReadonlyArray<SearchExecution> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState(0);
  const [form, setForm] = useState<"import" | "run" | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const items = useRef<Array<HTMLElement | null>>([]);
  const canAdd = lead && protocol !== null;

  const added = (execution: SearchExecution) => {
    setForm(null);
    setNotice(
      `Added the ${execution.plan.platform} search: ${execution.retrieved_count} records. Add the next database's search, or build the corpus on Studies once every search is in.`,
    );
    setTick((t) => t + 1);
  };

  useEffect(() => {
    if (!lead) return;
    let live = true;
    run(workbench.searches({ projectId: project.id, as: me.actor_id })).then(
      // The server lists the newest first; read them in the order they were run.
      (found) => live && setExecutions([...found].reverse()),
      (e: unknown) =>
        live && setError(e instanceof WorkbenchError ? e.message : "The searches could not load."),
    );
    return () => {
      live = false;
    };
  }, [workbench, project.id, me.actor_id, lead, tick]);

  useEffect(() => {
    items.current[selected]?.scrollIntoView({ block: "nearest" });
  }, [selected]);

  useKeys((event) => {
    if (canAdd && (event.key === "i" || event.key === "n")) {
      setForm(event.key === "i" ? "import" : "run");
      return true;
    }
    if (!executions?.length) return false;
    if (event.key === "j" || event.key === "ArrowDown") {
      setSelected((i) => Math.min(i + 1, executions.length - 1));
      return true;
    }
    if (event.key === "k" || event.key === "ArrowUp") {
      setSelected((i) => Math.max(i - 1, 0));
      return true;
    }
    return false;
  });

  return (
    <section className="h-full overflow-y-auto bg-paper">
      <div className="mx-auto max-w-[980px] px-10 pt-10 pb-16">
        <div className="flex items-baseline gap-6">
          <h1 className="text-xl font-semibold tracking-[-0.01em]">Searches</h1>
          <span className="ml-auto flex gap-5 text-sm">
            {executions?.length ? (
              <KeyHint keys={["j", "k"]}>Next and previous search</KeyHint>
            ) : null}
            {canAdd ? (
              <>
                <button type="button" onClick={() => setForm("import")}>
                  <KeyHint keys={["i"]}>Import a search file</KeyHint>
                </button>
                <button type="button" onClick={() => setForm("run")}>
                  <KeyHint keys={["n"]}>Run a live search</KeyHint>
                </button>
              </>
            ) : null}
          </span>
        </div>
        {notice ? (
          <p
            role="status"
            className="mt-3 max-w-[70ch] border-l-2 border-petrol pl-3 text-sm"
            data-testid="search-notice"
          >
            {notice}
          </p>
        ) : null}
        {canAdd ? (
          <>
            <ImportSearch
              open={form === "import"}
              onOpenChange={(o) => setForm(o ? "import" : null)}
              onDone={added}
            />
            <RunSearch
              open={form === "run"}
              onOpenChange={(o) => setForm(o ? "run" : null)}
              onDone={added}
            />
          </>
        ) : null}
        {!lead ? (
          <p className="mt-4 max-w-[60ch] text-ink-2" data-testid="lead-only">
            {LEAD_ONLY}
          </p>
        ) : error ? (
          <p role="alert" className="mt-4 text-exclude">
            {error}
          </p>
        ) : executions === null ? (
          <p className="mt-4 text-ink-3">Loading the searches.</p>
        ) : executions.length === 0 ? (
          <p className="mt-4 max-w-[62ch] text-md text-ink-2">
            {protocol
              ? "No searches yet. Import the export file of each database's search, or run PubMed and ClinicalTrials.gov live. One search per database and strategy."
              : "Publish the protocol first: each search belongs to one of its search topics."}
          </p>
        ) : (
          <>
            <p className="mt-2 text-sm text-ink-2">
              {executions.length} {executions.length === 1 ? "search" : "searches"},{" "}
              {executions.reduce((n, e) => n + e.retrieved_count, 0)} records retrieved before
              deduplication.
            </p>
            <ol className="mt-6 border-t border-rule-strong" aria-label="Search executions">
              {executions.map((execution, index) => (
                <li
                  key={execution.id}
                  ref={(el) => {
                    items.current[index] = el;
                  }}
                  aria-current={index === selected || undefined}
                  onClick={() => setSelected(index)}
                  className={`border-b border-rule py-5 pr-4 pl-5 ${
                    index === selected ? "bg-sheet shadow-[inset_3px_0_0_var(--petrol)]" : ""
                  }`}
                >
                  <Execution execution={execution} />
                </li>
              ))}
            </ol>
          </>
        )}
      </div>
    </section>
  );
}

function Execution({ execution: e }: { execution: SearchExecution }) {
  const lines = e.plan.strategy.split("\n");
  const skipped = e.skipped_record_ids?.length ?? 0;
  const differs = e.reported_count !== e.retrieved_count;
  return (
    <article aria-label={`${e.plan.platform} search`}>
      <header className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <h2 className="text-md font-semibold">{e.plan.platform}</h2>
        <span className="text-sm text-ink-2">
          {e.kind === "live_api" ? "Run live through the API" : "Imported from an export file"}
        </span>
        <span className="text-sm text-ink-3">{e.plan.source}</span>
      </header>
      <dl className="mt-3 grid grid-cols-[8.5rem_minmax(0,70ch)] gap-x-5 gap-y-1 text-sm">
        <dt className="narrow text-right text-xs font-semibold text-ink-2">Run</dt>
        <dd>
          {when(e.started_at)}
          {e.ended_at !== e.started_at ? ` to ${when(e.ended_at)}` : ""}
        </dd>
        <dt className="narrow text-right text-xs font-semibold text-ink-2">Records</dt>
        <dd data-testid="search-counts">
          {e.reported_count} reported, {e.retrieved_count} retrieved
          {differs ? (
            <span className="ml-2 text-exclude">
              {skipped > 0
                ? `${skipped} the source cannot map yet (book records)`
                : "The counts differ: this search is incomplete"}
            </span>
          ) : null}
        </dd>
        {e.plan.limits?.length ? (
          <>
            <dt className="narrow text-right text-xs font-semibold text-ink-2">Limits</dt>
            <dd>{e.plan.limits.join("; ")}</dd>
          </>
        ) : null}
        <dt className="narrow pt-1 text-right text-xs font-semibold text-ink-2">Strategy</dt>
        <dd>
          <ol className="border-l border-rule pl-3">
            {lines.map((line, i) => (
              <li key={i} className="grid grid-cols-[2rem_minmax(0,1fr)] gap-2">
                <span className="text-right text-ink-3">{i + 1}</span>
                <span className="break-words">{line}</span>
              </li>
            ))}
          </ol>
        </dd>
        {e.query_translation ? (
          <>
            <dt className="narrow pt-1 text-right text-xs font-semibold text-ink-2">
              As the platform read it
            </dt>
            <dd className="text-ink-2 break-words">{e.query_translation}</dd>
          </>
        ) : null}
      </dl>
    </article>
  );
}
