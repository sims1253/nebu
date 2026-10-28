// Records, reports, and studies (DESIGN.md, "Corpus and identities"). Kittum proposes which
// records are duplicates and which report the same study; the methods lead confirms each link,
// and every confirmation, merge, and split is a new identity revision with a ledger event.
import { useRouteContext } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { CorpusReport, CorpusView, LinkProposal, SourceRecord } from "../../data/schema.ts";
import { run, WorkbenchError } from "../../data/workbench.ts";
import { Key, KeyHint } from "../../ui/Key.tsx";
import { useKeys } from "../../ui/keys.ts";
import { LEAD_ONLY, plural, useProject } from "../project.tsx";

const RELATIONS: Record<LinkProposal["relation"], string> = {
  same_record: "The same record, found twice",
  same_report: "The same report, in two records",
  same_study: "Two reports of the same study",
};

interface Located {
  readonly record: SourceRecord;
  readonly report: CorpusReport;
}

interface Study {
  readonly studyId: string;
  readonly reports: ReadonlyArray<CorpusReport>;
}

type LinkAction =
  | { readonly action: "confirm"; readonly proposalId: string }
  | {
      readonly action: "merge";
      readonly occurrenceIds: ReadonlyArray<string>;
      readonly relation: "same_record" | "same_report" | "same_study";
    }
  | {
      readonly action: "split";
      readonly occurrenceIds: ReadonlyArray<string>;
      readonly level: "report" | "study";
    };

type Target =
  | { readonly kind: "proposal"; readonly key: string; readonly proposal: LinkProposal }
  | {
      readonly kind: "record";
      readonly key: string;
      readonly located: Located;
      readonly study: Study;
    };

export function StudiesScreen() {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me, seesLeadData, labels } = useProject();
  const lead = seesLeadData;
  const [corpus, setCorpus] = useState<CorpusView | null | undefined>(undefined);
  const [proposals, setProposals] = useState<ReadonlyArray<LinkProposal>>([]);
  const [selected, setSelected] = useState(0);
  const [marked, setMarked] = useState<ReadonlySet<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const rows = useRef(new Map<string, HTMLElement>());

  const load = useCallback(
    () =>
      Promise.all([
        run(workbench.corpus({ projectId: project.id, as: me.actor_id })),
        run(workbench.proposals({ projectId: project.id, as: me.actor_id })),
      ]).then(
        ([view, found]) => {
          setCorpus(view);
          setProposals(found);
        },
        (e: unknown) =>
          setError(e instanceof WorkbenchError ? e.message : "The corpus could not load."),
      ),
    [workbench, project.id, me.actor_id],
  );

  useEffect(() => {
    if (lead) void load();
  }, [load, lead]);

  const byOccurrence = useMemo(() => {
    const map = new Map<string, Located>();
    for (const report of corpus?.reports ?? []) {
      for (const record of report.records) map.set(record.occurrence_id, { record, report });
    }
    return map;
  }, [corpus]);

  const studies = useMemo(() => {
    const map = new Map<string, CorpusReport[]>();
    for (const report of corpus?.reports ?? []) {
      map.set(report.study_id, [...(map.get(report.study_id) ?? []), report]);
    }
    return [...map].map(([studyId, reports]): Study => ({ studyId, reports }));
  }, [corpus]);

  const pending = proposals.filter((p) => (p.status ?? "pending") === "pending");
  const settled = proposals.filter((p) => (p.status ?? "pending") !== "pending");
  const linked = studies.filter((s) => s.reports.reduce((n, r) => n + r.records.length, 0) > 1);
  const single = studies.filter((s) => s.reports.reduce((n, r) => n + r.records.length, 0) === 1);

  const targets = useMemo((): Target[] => {
    const recordTargets = (group: ReadonlyArray<Study>) =>
      group.flatMap((study) =>
        study.reports.flatMap((report) =>
          report.records.map((record): Target => ({
            kind: "record",
            key: record.occurrence_id,
            located: { record, report },
            study,
          })),
        ),
      );
    return [
      ...pending.map((proposal, i): Target => ({
        kind: "proposal",
        key: proposal.id ?? `proposal-${i}`,
        proposal,
      })),
      ...recordTargets(linked),
      ...recordTargets(single),
    ];
  }, [pending, linked, single]);

  const current = targets[Math.min(selected, targets.length - 1)];

  useEffect(() => {
    if (current) rows.current.get(current.key)?.scrollIntoView({ block: "nearest" });
  }, [current]);

  const act = async (action: LinkAction, done: string) => {
    if (!corpus) return;
    if (!lead) {
      setNotice("Only the methods lead confirms study links.");
      return;
    }
    setBusy(true);
    setNotice(null);
    try {
      await run(
        workbench.link({
          projectId: project.id,
          as: me.actor_id,
          revisionId: corpus.revision.id,
          ...action,
        }),
      );
      setMarked(new Set());
      setNotice(done);
      await load();
    } catch (e) {
      setNotice(e instanceof WorkbenchError ? e.message : "The change was not recorded.");
    } finally {
      setBusy(false);
    }
  };

  useKeys((event) => {
    if (busy || !current) return false;
    switch (event.key) {
      case "j":
      case "ArrowDown":
        setSelected((i) => Math.min(i + 1, targets.length - 1));
        return true;
      case "k":
      case "ArrowUp":
        setSelected((i) => Math.max(i - 1, 0));
        return true;
      case "Enter":
        if (current.kind !== "proposal" || !current.proposal.id) return false;
        void act(
          { action: "confirm", proposalId: current.proposal.id },
          "Confirmed. A new identity revision records the link.",
        );
        return true;
      case "x":
        if (current.kind !== "record") return false;
        setMarked((m) => {
          const next = new Set(m);
          if (next.has(current.key)) next.delete(current.key);
          else next.add(current.key);
          return next;
        });
        return true;
      case "m":
        if (marked.size < 2) {
          setNotice("Mark at least two records with x, then merge them into one study with m.");
          return true;
        }
        void act(
          { action: "merge", occurrenceIds: [...marked], relation: "same_study" },
          `Merged ${marked.size} records into one ${labels.study.toLowerCase()}.`,
        );
        return true;
      case "s": {
        if (current.kind !== "record") return false;
        const { study, located } = current;
        const records = study.reports.reduce((n, r) => n + r.records.length, 0);
        if (records < 2) {
          setNotice("This record stands alone already.");
          return true;
        }
        // A study of several reports: take this record's report out of it. One report found
        // twice: take this record out of the report.
        const level = study.reports.length > 1 ? "study" : "report";
        void act(
          { action: "split", occurrenceIds: [located.record.occurrence_id], level },
          level === "study"
            ? `Split out into its own ${labels.study.toLowerCase()}.`
            : `Split out into its own ${labels.report.toLowerCase()}.`,
        );
        return true;
      }
      default:
        return false;
    }
  });

  const rowRef = (key: string) => (el: HTMLElement | null) => {
    if (el) rows.current.set(key, el);
    else rows.current.delete(key);
  };
  const isCurrent = (key: string) => current?.key === key;
  const select = (key: string) => {
    const index = targets.findIndex((t) => t.key === key);
    if (index >= 0) setSelected(index);
  };
  const selectedClass = (key: string) =>
    isCurrent(key) ? "bg-sheet shadow-[inset_3px_0_0_var(--petrol)]" : "";

  return (
    <div className="grid h-full grid-rows-[minmax(0,1fr)_auto]">
      <section className="overflow-y-auto bg-paper">
        <div className="mx-auto max-w-[980px] px-10 pt-10 pb-16">
          <h1 className="text-xl font-semibold tracking-[-0.01em]">{plural(labels.study)}</h1>
          {project.stages.length > 0 ? (
            <p className="mt-2 max-w-[70ch] text-sm text-ink-2">
              Screening that is already open keeps reading the identity revision it opened on; links
              you confirm now count from the next stage on.
            </p>
          ) : null}
          {!lead ? (
            <p className="mt-4 max-w-[60ch] text-ink-2" data-testid="lead-only">
              {LEAD_ONLY}
            </p>
          ) : error ? (
            <p role="alert" className="mt-4 text-exclude">
              {error}
            </p>
          ) : corpus === undefined ? (
            <p className="mt-4 text-ink-3">Loading the corpus.</p>
          ) : corpus === null ? (
            <p className="mt-4 max-w-[62ch] text-md text-ink-2">
              The corpus is not built yet. Once the searches are in, the methods lead runs{" "}
              <code>kittum project ingest</code> to deduplicate them into records and{" "}
              {plural(labels.study).toLowerCase()}.
            </p>
          ) : (
            <>
              <p className="mt-2 text-sm text-ink-2">
                {corpus.reports.length} {plural(labels.report).toLowerCase()} in {studies.length}{" "}
                {plural(labels.study).toLowerCase()}, as of ledger event{" "}
                {corpus.revision.event_seq ?? 0}.{" "}
                {lead ? null : "Only the methods lead confirms study links."}
              </p>

              <h2 className="mt-8 border-b border-rule-strong pb-1.5 text-lg font-semibold">
                Proposed links
                <span className="ml-3 text-sm font-normal text-ink-3">{pending.length} open</span>
              </h2>
              {pending.length === 0 ? (
                <p className="mt-3 text-ink-2">Every proposed link is decided.</p>
              ) : (
                <ol aria-label="Proposed links">
                  {pending.map((proposal, i) => {
                    const key = proposal.id ?? `proposal-${i}`;
                    return (
                      <li
                        key={key}
                        ref={rowRef(key)}
                        data-testid="proposal"
                        aria-current={isCurrent(key) || undefined}
                        onClick={() => select(key)}
                        className={`border-b border-rule py-4 pr-4 pl-5 ${selectedClass(key)}`}
                      >
                        <p className="flex flex-wrap items-baseline gap-x-4">
                          <span className="font-semibold">{RELATIONS[proposal.relation]}</span>
                          <span className="text-sm text-ink-2">{proposal.evidence.join("; ")}</span>
                        </p>
                        <ul className="mt-2 grid gap-1.5">
                          {proposal.occurrence_ids.map((id) => (
                            <RecordLine key={id} located={byOccurrence.get(id)} />
                          ))}
                        </ul>
                      </li>
                    );
                  })}
                </ol>
              )}
              {settled.length > 0 ? (
                <p className="mt-3 text-sm text-ink-3">
                  {settled.length} {settled.length === 1 ? "link" : "links"} already decided:{" "}
                  {settled.map((p) => RELATIONS[p.relation].toLowerCase()).join("; ")}.
                </p>
              ) : null}

              <h2 className="mt-10 border-b border-rule-strong pb-1.5 text-lg font-semibold">
                {plural(labels.study)} with more than one record
                <span className="ml-3 text-sm font-normal text-ink-3">{linked.length}</span>
              </h2>
              {linked.length === 0 ? (
                <p className="mt-3 text-ink-2">
                  Every record is its own {labels.study.toLowerCase()} so far.
                </p>
              ) : (
                <ol aria-label={`Linked ${plural(labels.study).toLowerCase()}`}>
                  {linked.map((study) => (
                    <li key={study.studyId} className="border-b border-rule py-2">
                      <ul>
                        {study.reports.flatMap((report) =>
                          report.records.map((record) => (
                            <StudyRecord
                              key={record.occurrence_id}
                              located={{ record, report }}
                              current={isCurrent(record.occurrence_id)}
                              marked={marked.has(record.occurrence_id)}
                              rowRef={rowRef(record.occurrence_id)}
                              onSelect={() => select(record.occurrence_id)}
                              twin={report.records.length > 1}
                            />
                          )),
                        )}
                      </ul>
                    </li>
                  ))}
                </ol>
              )}

              <h2 className="mt-10 border-b border-rule-strong pb-1.5 text-lg font-semibold">
                Single-record {plural(labels.study).toLowerCase()}
                <span className="ml-3 text-sm font-normal text-ink-3">{single.length}</span>
              </h2>
              <ol aria-label={`Single-record ${plural(labels.study).toLowerCase()}`}>
                {single.map((study) =>
                  study.reports.flatMap((report) =>
                    report.records.map((record) => (
                      <StudyRecord
                        key={record.occurrence_id}
                        located={{ record, report }}
                        current={isCurrent(record.occurrence_id)}
                        marked={marked.has(record.occurrence_id)}
                        rowRef={rowRef(record.occurrence_id)}
                        onSelect={() => select(record.occurrence_id)}
                        twin={false}
                      />
                    )),
                  ),
                )}
              </ol>
            </>
          )}
        </div>
      </section>
      <footer className="flex h-10 items-center gap-5 border-t border-rule-strong bg-sheet-2 px-4 text-sm">
        <KeyHint keys={["j", "k"]}>Move</KeyHint>
        <KeyHint keys={["Enter"]} muted={!lead || current?.kind !== "proposal"}>
          Confirm the link
        </KeyHint>
        <KeyHint keys={["x"]} muted={current?.kind !== "record"}>
          Mark
        </KeyHint>
        <KeyHint keys={["m"]} muted={!lead || marked.size < 2}>
          Merge marked into one {labels.study.toLowerCase()}
        </KeyHint>
        <KeyHint keys={["s"]} muted={!lead || current?.kind !== "record"}>
          Split out
        </KeyHint>
        <span
          className="ml-auto truncate text-ink-2"
          aria-live="polite"
          data-testid="studies-notice"
        >
          {busy ? "Recording." : notice}
        </span>
      </footer>
    </div>
  );
}

function RecordLine({ located }: { located: Located | undefined }) {
  if (!located) return <li className="text-sm text-ink-3">A record outside this revision</li>;
  const { record } = located;
  return (
    <li className="grid grid-cols-[7rem_minmax(0,1fr)] gap-3 text-sm">
      <span className="narrow text-ink-3">{record.source}</span>
      <span>
        {record.title}
        <span className="ml-2 text-ink-3">{ids(record)}</span>
      </span>
    </li>
  );
}

function StudyRecord({
  located: { record },
  current,
  marked,
  rowRef,
  onSelect,
  twin,
}: {
  located: Located;
  current: boolean;
  marked: boolean;
  rowRef: (el: HTMLElement | null) => void;
  onSelect: () => void;
  /** Another record of the same report: a duplicate. */
  twin: boolean;
}) {
  return (
    <li
      ref={rowRef}
      aria-current={current || undefined}
      data-marked={marked || undefined}
      onClick={onSelect}
      className={`grid grid-cols-[1.5rem_7rem_minmax(0,1fr)] items-baseline gap-3 py-1.5 pr-4 pl-5 text-sm ${
        current ? "bg-sheet shadow-[inset_3px_0_0_var(--petrol)]" : ""
      } ${marked ? "bg-petrol-wash" : ""}`}
    >
      <span>{marked ? <Key k="x" on /> : null}</span>
      <span className="narrow text-ink-3">
        {record.source}
        {twin ? <span className="block text-2xs">duplicate</span> : null}
      </span>
      <span>
        {record.title}
        <span className="ml-2 text-ink-3">{ids(record)}</span>
      </span>
    </li>
  );
}

const ids = (record: SourceRecord) =>
  [
    record.year ? String(record.year) : null,
    record.pmid ? `PMID ${record.pmid}` : null,
    record.doi ? `DOI ${record.doi}` : null,
    ...(record.registry_ids ?? []),
  ]
    .filter(Boolean)
    .join(", ");
