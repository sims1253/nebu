// Enrichment from the open sources (DESIGN.md, "Enrichment and federation"): the run's progress
// per source, and one record as enrichment completed it, with the source of every field. The
// sources share one column order everywhere, so a source's row in the run lines up in the mind
// with its column in a record's provenance. A retraction is the one loud thing on the screen. A
// source that failed says so in plain words, and the lead retries it from here or Retrieval.
import { useRouteContext } from "@tanstack/react-router";
import { useState } from "react";
import type { ReactNode } from "react";

import type {
  EnrichmentRun,
  EntityTag,
  RecordEnrichment,
  SourceFailure,
  SourceProgress,
} from "../../data/schema.ts";
import { run as call } from "../../data/workbench.ts";
import { Button, failure } from "../../ui/Form.tsx";
import { useProject } from "../project.tsx";

const SOURCES: ReadonlyArray<readonly [string, string]> = [
  ["openalex", "OpenAlex"],
  ["europepmc", "Europe PMC"],
  ["crossref", "Crossref"],
  ["unpaywall", "Unpaywall"],
  ["pubtator", "PubTator 3"],
];
// The full-text sources the waterfall asks one report at a time, after enrichment.
const SOURCE_NAMES = new Map([
  ...SOURCES,
  ["pmc", "PubMed Central"],
  ["biorxiv", "medRxiv or bioRxiv"],
  ["arxiv", "arXiv"],
  ["semanticscholar", "Semantic Scholar"],
  ["core", "CORE"],
]);
export const sourceName = (source: string) => SOURCE_NAMES.get(source) ?? source;

const TROUBLE: Record<number, string> = {
  503: "the service was down",
  502: "a server in front of it failed",
  504: "a server in front of it timed out",
  500: "it had an internal error",
  429: "it was asked too often",
};

/** A source's failure in plain words, optionally with what it failed for ("24 records"), and
 * what the source said about it, in its own words. */
export function failureLine(
  source: string,
  f: Pick<SourceFailure, "status" | "message"> & { readonly kind?: string },
  counts = "",
): string {
  const name = sourceName(source);
  const forWhat = counts ? ` for ${counts}` : "";
  let line: string;
  if (f.kind === "other_work") line = `${name} answered for another work${forWhat}.`;
  else if (f.kind === "budget") line = `${name} has no requests left today${forWhat}.`;
  else if (f.kind === "malformed") line = `${name} sent an answer Kittum cannot read${forWhat}.`;
  else if (f.status && f.status < 500 && f.status !== 429)
    line = `${name} refused the request (HTTP ${f.status})${forWhat}.`;
  else if (f.status) {
    const why = TROUBLE[f.status];
    line = `${name} did not answer (HTTP ${f.status}${why ? `, ${why}` : ""})${forWhat}.`;
  } else line = `Kittum could not reach ${name}${forWhat}.`;
  return f.message ? `${line} It said: "${f.message}"` : line;
}

const FIELDS: ReadonlyArray<readonly [string, string]> = [
  ["doi", "DOI"],
  ["pmid", "PMID"],
  ["pmcid", "PMCID"],
  ["abstract", "Abstract"],
  ["retraction", "Retraction"],
  ["clinical_trial_numbers", "Trial numbers"],
  ["oa_locations", "Open-access locations"],
  ["entities", "Entity tags"],
];

const NOTICES: Record<string, string> = {
  retraction: "Retraction",
  correction: "Correction",
  erratum: "Erratum",
  expression_of_concern: "Expression of concern",
};
export const noticeName = (kind: string) => NOTICES[kind] ?? kind.replaceAll("_", " ");

// Entity tags are highlights from PubTator, never evidence: they keep off the marker yellow.
const ENTITY_COLOURS: Record<string, string> = {
  drug: "var(--petrol)",
  disease: "var(--unclear)",
  gene: "var(--insufficient)",
  mutation: "var(--insufficient)",
};
const ENTITY_NAMES: Record<string, string> = {
  drug: "Drug",
  disease: "Disease",
  gene: "Gene",
  mutation: "Mutation",
  species: "Species",
  cell_line: "Cell line",
  virus: "Virus",
};

const FORMATS: Record<string, string> = {
  pdf: "PDF",
  xml: "Full-text XML",
  landing: "Landing page",
};

const time = (iso: string) =>
  new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });

const TRIGGER: Record<string, string> = {
  corpus_built: "after the corpus was built",
  requested: "at your request",
  retry: "again on a retry",
};

export const runActive = (run: { readonly status: string } | null | undefined) =>
  run?.status === "queued" || run?.status === "running";

const runLine = (run: EnrichmentRun) => {
  const records = `${run.records} ${run.records === 1 ? "record" : "records"}`;
  switch (run.status) {
    case "queued":
      return `Waiting for a worker to enrich ${records}.`;
    case "running":
      return `Enriched ${run.done} of ${records} so far.`;
    case "failed":
      return `Enrichment stopped after ${run.done} of ${records}. ${run.error ?? ""} Press r to run it again.`.replace(
        ".  ",
        ". ",
      );
    case "done":
      return `Enriched ${records} ${TRIGGER[run.trigger]}, finished at ${run.finished_at ? time(run.finished_at) : "?"}.`;
  }
};

export function EnrichmentPanel({
  run,
  busy,
  error,
}: {
  run: EnrichmentRun | null;
  busy: boolean;
  error: string | null;
}) {
  return (
    <section aria-labelledby="enrichment-heading" className="mt-8" data-testid="enrichment">
      <h2
        id="enrichment-heading"
        className="flex items-baseline border-b border-rule-strong pb-1.5 text-lg font-semibold"
      >
        Enrichment from the open sources
      </h2>
      {run === null ? (
        <p className="mt-3 max-w-[70ch] text-ink-2">
          Not enriched yet. Press r to ask the open sources about every record for identifiers,
          abstracts, open-access files, retractions, and drug and disease tags.
        </p>
      ) : (
        <>
          <p className="mt-3 text-sm" data-testid="enrichment-status" aria-live="polite">
            {runLine(run)}
            {run.retracted > 0 ? (
              <strong className="ml-2 font-semibold text-exclude">
                {run.retracted} {run.retracted === 1 ? "record is" : "records are"} retracted.
              </strong>
            ) : null}
          </p>
          <SourceTable sources={run.sources} />
          <RetryFailed sources={run.sources} active={runActive(run)} />
        </>
      )}
      {busy || error ? (
        <p
          role={error ? "alert" : "status"}
          className={`mt-2 text-sm ${error ? "text-exclude" : "text-ink-2"}`}
        >
          {error ?? "Starting the enrichment."}
        </p>
      ) : null}
    </section>
  );
}

/** The sources that failed in the run, in plain words, with the action that asks them again. */
function RetryFailed({
  sources,
  active,
}: {
  sources: ReadonlyArray<SourceProgress>;
  active: boolean;
}) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me } = useProject();
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const failed = sources.filter((s) => s.failed > 0 && s.last_failure?.kind !== "other_work");
  if (failed.length === 0 && !note) return null;
  const retry = async () => {
    setBusy(true);
    try {
      const plan = await call(
        workbench.retryFailedSources({ projectId: project.id, as: me.actor_id }),
      );
      setNote(
        `Asking ${plan.sources.map(sourceName).join(" and ")} again. Retrieval shows the progress.`,
      );
    } catch (e) {
      setNote(failure(e, "Kittum could not start the retry."));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div
      className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1"
      data-testid="enrichment-retry"
    >
      {failed.length > 0 && !note ? (
        <Button disabled={busy || active} onClick={() => void retry()}>
          Retry failed sources
        </Button>
      ) : null}
      <span className="text-sm text-ink-2" aria-live="polite">
        {note ??
          failed
            .map((s) => failureLine(s.source, s.last_failure ?? {}, `${s.failed} records`))
            .join(" ")}
      </span>
    </div>
  );
}

function SourceTable({ sources }: { sources: ReadonlyArray<SourceProgress> }) {
  return (
    <table aria-label="Enrichment by source" className="mt-3 w-full text-sm">
      <thead className="text-left text-xs text-ink-3">
        <tr>
          <th className="w-32 py-1 font-normal">Source</th>
          <th className="w-20 py-1 text-right font-normal">Asked</th>
          <th className="w-20 py-1 text-right font-normal">Filled</th>
          <th className="w-24 py-1 text-right font-normal">No record</th>
          <th className="w-20 py-1 text-right font-normal">Failed</th>
          <th className="py-1 pl-5 font-normal">
            <span className="sr-only">Share of records</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {SOURCES.map(([id, name]) => {
          const s = sources.find((x) => x.source === id);
          const asked = s?.asked ?? 0;
          const answered = s?.answered ?? 0;
          const failed = s?.failed ?? 0;
          const none = Math.max(asked - answered - failed, 0);
          return (
            <tr key={id} data-testid="source-row" data-source={id} className="border-t border-rule">
              <th scope="row" className="py-1.5 text-left font-medium">
                {name}
              </th>
              <td className="py-1.5 text-right tabular-nums">{asked}</td>
              <td className="py-1.5 text-right tabular-nums">{answered}</td>
              <td className="py-1.5 text-right tabular-nums text-ink-2">{none}</td>
              <td
                className={`py-1.5 text-right tabular-nums ${failed ? "font-semibold text-exclude" : "text-ink-2"}`}
              >
                {failed}
              </td>
              <td className="py-1.5 pl-5">
                {asked === 0 ? (
                  <span className="text-xs text-ink-3">No record to ask it about</span>
                ) : (
                  <span className="flex items-center gap-3">
                    <span
                      className="flex h-2 w-40 shrink-0 overflow-hidden rounded-[1px] bg-rule"
                      aria-hidden
                    >
                      <span
                        className="bg-include"
                        style={{ width: `${(answered / asked) * 100}%` }}
                      />
                      <span
                        className="bg-exclude"
                        style={{ width: `${(failed / asked) * 100}%` }}
                      />
                    </span>
                    {s?.last_failure && failed ? (
                      <span
                        className="text-xs whitespace-nowrap text-exclude"
                        title={failureLine(id, s.last_failure)}
                      >
                        {s.last_failure.status ? `HTTP ${s.last_failure.status}` : "No answer"}
                      </span>
                    ) : null}
                  </span>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/** Text with PubTator's entities underlined in their label's colour. `offset` is where `text`
 * starts in the title-plus-abstract string the tags index into. */
function Tagged({
  text,
  offset,
  entities,
}: {
  text: string;
  offset: number;
  entities: ReadonlyArray<EntityTag>;
}) {
  const inside = entities
    .filter((e) => e.start >= offset && e.end <= offset + text.length)
    .toSorted((a, b) => a.start - b.start);
  const parts: ReactNode[] = [];
  let at = offset;
  for (const e of inside) {
    if (e.start < at) continue; // overlapping tags: the first wins
    parts.push(text.slice(at - offset, e.start - offset));
    parts.push(
      <mark
        key={`${e.start}-${e.label}`}
        data-testid="entity"
        data-label={e.label}
        title={[ENTITY_NAMES[e.label] ?? e.label, e.normalized_id].filter(Boolean).join(", ")}
        className="bg-transparent text-inherit underline decoration-2 underline-offset-[3px]"
        style={{ textDecorationColor: ENTITY_COLOURS[e.label] ?? "var(--ink-3)" }}
      >
        {text.slice(e.start - offset, e.end - offset)}
      </mark>,
    );
    at = e.end;
  }
  parts.push(text.slice(at - offset));
  return <>{parts}</>;
}

const host = (url: string) => {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
};

export function RecordDetail({
  record,
  searchSource,
}: {
  record: RecordEnrichment | null | undefined;
  searchSource: string;
}) {
  if (record === undefined) return <p className="text-sm text-ink-3">Loading the record.</p>;
  if (record === null) {
    return <p className="text-sm text-ink-2">This record is not enriched yet.</p>;
  }
  const notices = record.updates.filter(
    (u) =>
      u.direction === "updated_by" &&
      !(u.kind === "retraction" && u.doi === record.retraction_notice_doi),
  );
  const columns: ReadonlyArray<readonly [string, string]> = [
    [record.search_source, "The search"],
    ...SOURCES,
  ];
  const fromSource = new Set(record.field_sources.map((f) => `${f.field}|${f.source}`));
  const fields = FIELDS.filter(([field]) => record.field_sources.some((f) => f.field === field));
  const labels = [...new Set(record.entities.map((e) => e.label))];
  return (
    <article aria-label="Record enrichment" data-testid="record-enrichment" className="text-sm">
      {record.retracted ? (
        <section
          data-testid="retraction"
          className="-mx-6 mb-5 border-y-2 border-exclude bg-exclude-wash px-6 py-3 text-exclude"
        >
          <p className="text-md font-semibold">Retracted</p>
          <p className="mt-1 text-ink">
            {record.retraction_notice_doi
              ? `Crossref links the retraction notice ${record.retraction_notice_doi} to this article.`
              : "OpenAlex marks this article as retracted."}{" "}
            Check whether its study still belongs in the review.
          </p>
        </section>
      ) : null}
      <h3 className="text-md leading-snug font-semibold">
        <Tagged text={record.title} offset={0} entities={record.entities} />
      </h3>
      <p className="mt-1 text-xs text-ink-3">Found by {searchSource}</p>

      {notices.length > 0 ? (
        <ul className="mt-3 grid gap-1" aria-label="Notices">
          {notices.map((u) => (
            <li key={`${u.kind}-${u.doi}`} data-testid="notice" className="text-unclear">
              <span className="font-medium">{noticeName(u.kind)}</span>{" "}
              <span className="text-ink-2">{u.doi}</span>
            </li>
          ))}
        </ul>
      ) : null}

      <dl className="mt-4 grid grid-cols-[7rem_minmax(0,1fr)] gap-x-3 gap-y-1">
        {(
          [
            ["DOI", record.doi],
            ["PMID", record.pmid],
            ["PMCID", record.pmcid],
            ["Trial numbers", record.clinical_trial_numbers.join(", ") || null],
          ] as const
        ).map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-ink-3">{label}</dt>
            <dd className={value ? "" : "text-ink-3"}>{value ?? "Not found"}</dd>
          </div>
        ))}
      </dl>

      <h4 className="mt-6 text-sm font-semibold">Where each field came from</h4>
      <table aria-label="Where each field came from" className="mt-2 w-full text-xs">
        <thead>
          <tr>
            <th className="py-1 text-left font-normal text-ink-3">
              <span className="sr-only">Field</span>
            </th>
            {columns.map(([id, name]) => (
              <th
                key={id}
                scope="col"
                className="w-[4.2rem] px-1 py-1 text-center align-bottom font-normal text-ink-2"
              >
                {name}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {fields.map(([field, label]) => (
            <tr
              key={field}
              data-testid="provenance-row"
              data-field={field}
              className="border-t border-rule"
            >
              <th scope="row" className="py-1.5 pr-2 text-left font-medium">
                {label}
              </th>
              {columns.map(([id, name]) => {
                const supplied = fromSource.has(`${field}|${id}`);
                return (
                  <td
                    key={id}
                    className="px-1 py-1.5 text-center"
                    data-source={supplied ? id : undefined}
                  >
                    {supplied ? (
                      <>
                        <span
                          aria-hidden
                          className="inline-block size-2.5 rounded-[2px] bg-petrol"
                        />
                        <span className="sr-only">{name}</span>
                      </>
                    ) : null}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {record.failures.length > 0 ? (
        <ul className="mt-2 grid gap-0.5 text-xs text-exclude" data-testid="record-failures">
          {record.failures.map((f) => (
            <li key={f.source} title={f.reason}>
              {failureLine(f.source, f)}
            </li>
          ))}
        </ul>
      ) : null}

      <h4 className="mt-6 text-sm font-semibold">Abstract</h4>
      {record.abstract ? (
        <p className="mt-2 max-w-[68ch] text-md leading-relaxed" data-testid="enriched-abstract">
          <Tagged
            text={record.abstract}
            offset={record.title.length + 1}
            entities={record.entities}
          />
        </p>
      ) : (
        <p className="mt-2 text-ink-3">No source had an abstract.</p>
      )}
      {labels.length > 0 ? (
        <p className="mt-2 flex flex-wrap gap-x-4 text-xs text-ink-2" aria-label="Entity tags">
          {labels.map((label) => (
            <span key={label} className="inline-flex items-center gap-1.5">
              <span
                aria-hidden
                className="inline-block h-0.5 w-4"
                style={{ background: ENTITY_COLOURS[label] ?? "var(--ink-3)" }}
              />
              {ENTITY_NAMES[label] ?? label},{" "}
              {record.entities.filter((e) => e.label === label).length} from PubTator 3
            </span>
          ))}
        </p>
      ) : null}

      <h4 className="mt-6 text-sm font-semibold">Open-access locations</h4>
      {record.oa_locations.length === 0 ? (
        <p className="mt-2 text-ink-3">No source has an open-access copy.</p>
      ) : (
        <table aria-label="Open-access locations" className="mt-2 w-full text-xs">
          <tbody>
            {record.oa_locations.map((o) => (
              <tr key={o.url} data-testid="oa-location" className="border-t border-rule">
                <td className="w-24 py-1.5 pr-2 text-ink-2">{sourceName(o.source ?? "")}</td>
                <td className="w-24 py-1.5 pr-2">{FORMATS[o.format ?? ""] ?? "Unknown"}</td>
                <td className="py-1.5 pr-2">
                  <a
                    href={o.url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-petrol underline-offset-2 hover:underline"
                  >
                    {host(o.url)}
                  </a>
                </td>
                <td className="w-24 py-1.5 text-right text-ink-2">
                  {o.licence ?? "No licence stated"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </article>
  );
}
