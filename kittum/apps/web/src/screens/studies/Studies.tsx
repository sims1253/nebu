// Records, reports, and studies (DESIGN.md, "Corpus and identities"). Kittum proposes which
// records are duplicates and which report the same study; the methods lead confirms each link,
// and every confirmation, merge, and split is a new identity revision with a ledger event. Once
// the corpus is built, a worker enriches it from the open sources; the lead follows the run per
// source and opens any record to see where each of its fields came from.
import { useRouteContext } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type {
  CorpusReport,
  CorpusView,
  EnrichmentRun,
  LinkProposal,
  RecordEnrichment,
  RecordFlag,
  SourceRecord,
} from "../../data/schema.ts";
import { run, WorkbenchError } from "../../data/workbench.ts";
import { Key, KeyHint } from "../../ui/Key.tsx";
import { useKeys } from "../../ui/keys.ts";
import { usePoll } from "../../ui/poll.ts";
import { LEAD_ONLY, plural, useProject } from "../project.tsx";
import { BuildCorpus } from "../setup/BuildCorpus.tsx";
import { EnrichmentPanel, noticeName, RecordDetail, runActive } from "./Enrichment.tsx";

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
  const [searches, setSearches] = useState<number | null>(null);
  const [selected, setSelected] = useState(0);
  const [marked, setMarked] = useState<ReadonlySet<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [enrichment, setEnrichment] = useState<EnrichmentRun | null>(null);
  const [flags, setFlags] = useState<ReadonlyMap<string, RecordFlag>>(new Map());
  const [enriching, setEnriching] = useState(false);
  const [enrichError, setEnrichError] = useState<string | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  /** The open record's enrichment, under the occurrence it belongs to. */
  const [detail, setDetail] = useState<{
    readonly id: string;
    readonly record: RecordEnrichment | null;
  } | null>(null);
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

  const loadEnrichment = useCallback(
    () =>
      Promise.all([
        run(workbench.enrichment({ projectId: project.id, as: me.actor_id })),
        run(workbench.enrichmentFlags({ projectId: project.id, as: me.actor_id })),
      ]).then(
        ([latest, found]) => {
          setEnrichment(latest);
          setFlags(new Map(found.map((f) => [f.occurrence_id, f])));
        },
        (e: unknown) =>
          setEnrichError(
            e instanceof WorkbenchError ? e.message : "The enrichment could not load.",
          ),
      ),
    [workbench, project.id, me.actor_id],
  );
  useEffect(() => {
    if (lead && corpus) void loadEnrichment();
  }, [loadEnrichment, lead, corpus]);
  usePoll(lead && runActive(enrichment), () => void loadEnrichment());

  const enrich = async () => {
    setEnriching(true);
    setEnrichError(null);
    try {
      setEnrichment(
        await run(workbench.startEnrichment({ projectId: project.id, as: me.actor_id })),
      );
    } catch (e) {
      setEnrichError(e instanceof WorkbenchError ? e.message : "The enrichment was not started.");
    } finally {
      setEnriching(false);
    }
  };

  // Before the corpus, the lead builds it here from the searches.
  useEffect(() => {
    if (!lead || corpus !== null) return;
    let live = true;
    run(workbench.searches({ projectId: project.id, as: me.actor_id })).then(
      (found) => live && setSearches(found.length),
      () => live && setSearches(0),
    );
    return () => {
      live = false;
    };
  }, [workbench, project.id, me.actor_id, lead, corpus]);

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
  const openRecord = current?.kind === "record" ? current.located.record : null;
  const openId = detailOpen ? openRecord?.occurrence_id : undefined;
  // The detail follows the selection; a finished run refreshes it.
  useEffect(() => {
    if (!openId) return;
    let live = true;
    run(
      workbench.recordEnrichment({ projectId: project.id, as: me.actor_id, occurrenceId: openId }),
    ).then(
      (found) => live && setDetail({ id: openId, record: found }),
      () => live && setDetail({ id: openId, record: null }),
    );
    return () => {
      live = false;
    };
  }, [workbench, project.id, me.actor_id, openId, enrichment?.status, enrichment?.id]);

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
    if (event.key === "r" && lead && corpus && !enriching) {
      if (runActive(enrichment)) {
        setNotice("The enrichment is still running; it shows here as it goes.");
        return true;
      }
      void enrich();
      return true;
    }
    if (event.key === "Escape" && detailOpen) {
      setDetailOpen(false);
      return true;
    }
    if (busy || !current) return false;
    switch (event.key) {
      case "o":
        if (current.kind !== "record") return false;
        setDetailOpen((open) => !open);
        return true;
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
        // Records of one study are linked already, perhaps closer (one report found twice):
        // a study merge would add nothing but a revision that says it merged them.
        if (new Set([...marked].map((id) => byOccurrence.get(id)?.report.study_id)).size === 1) {
          setNotice(
            `These records are one ${labels.study.toLowerCase()} already. Split one out with s to unlink it.`,
          );
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

  const showDetail = lead && detailOpen && openRecord !== null;
  return (
    <div className="grid h-full grid-rows-[minmax(0,1fr)_auto]">
      <div
        className={`grid min-h-0 ${showDetail ? "grid-cols-[minmax(0,1fr)_minmax(26rem,34rem)]" : "grid-cols-1"}`}
      >
        <section className="overflow-y-auto bg-paper">
          <div className="mx-auto max-w-[980px] px-10 pt-10 pb-16">
            <h1 className="text-xl font-semibold tracking-[-0.01em]">{plural(labels.study)}</h1>
            {project.stages.length > 0 ? (
              <p className="mt-2 max-w-[70ch] text-sm text-ink-2">
                Screening that is already open keeps reading the identity revision it opened on;
                links you confirm now count from the next stage on.
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
              <>
                <p className="mt-2 text-sm text-ink-2">The corpus is not built yet.</p>
                <BuildCorpus searches={searches} onBuilt={() => void load()} />
              </>
            ) : (
              <>
                <p className="mt-2 text-sm text-ink-2">
                  {corpus.reports.length} {plural(labels.report).toLowerCase()} in {studies.length}{" "}
                  {plural(labels.study).toLowerCase()}, as of ledger event{" "}
                  {corpus.revision.event_seq ?? 0}.{" "}
                  {lead ? null : "Only the methods lead confirms study links."}
                </p>

                <EnrichmentPanel run={enrichment} busy={enriching} error={enrichError} />

                <h2 className="mt-10 border-b border-rule-strong pb-1.5 text-lg font-semibold">
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
                            <span className="text-sm text-ink-2">
                              {proposal.evidence.join("; ")}
                            </span>
                          </p>
                          <ul className="mt-2 grid gap-1.5">
                            {proposal.occurrence_ids.map((id) => (
                              <RecordLine
                                key={id}
                                located={byOccurrence.get(id)}
                                flag={flags.get(id)}
                              />
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
                                flag={flags.get(record.occurrence_id)}
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
                          flag={flags.get(record.occurrence_id)}
                        />
                      )),
                    ),
                  )}
                </ol>
              </>
            )}
          </div>
        </section>
        {showDetail && openRecord ? (
          <aside
            aria-label="Enrichment of the selected record"
            className="overflow-y-auto border-l border-rule-strong bg-sheet px-6 pt-8 pb-16"
          >
            <RecordDetail
              record={detail?.id === openRecord.occurrence_id ? detail.record : undefined}
              searchSource={openRecord.source}
            />
          </aside>
        ) : null}
      </div>
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
        <KeyHint keys={["o"]} muted={!lead || current?.kind !== "record"}>
          {detailOpen ? "Close its enrichment" : "Open its enrichment"}
        </KeyHint>
        <KeyHint keys={["r"]} muted={!lead || !corpus || runActive(enrichment)}>
          Enrich again
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

function RecordLine({
  located,
  flag,
}: {
  located: Located | undefined;
  flag: RecordFlag | undefined;
}) {
  if (!located) return <li className="text-sm text-ink-3">A record outside this revision</li>;
  const { record } = located;
  return (
    <li className="grid grid-cols-[7rem_minmax(0,1fr)] gap-3 text-sm">
      <span className="narrow text-ink-3">{record.source}</span>
      <span>
        <Flags flag={flag} />
        {record.title}
        <span className="ml-2 text-ink-3">{ids(record)}</span>
      </span>
    </li>
  );
}

/** What enrichment found that the lead must not miss: a retraction first, then other notices. */
function Flags({ flag }: { flag: RecordFlag | undefined }) {
  if (!flag || (!flag.retracted && flag.updates.length === 0)) return null;
  return (
    <span className="mr-2" data-testid="record-flag">
      {flag.retracted ? (
        <span className="mr-2 rounded-[2px] bg-exclude px-1.5 py-px text-xs font-semibold text-sheet">
          Retracted
        </span>
      ) : null}
      {flag.updates.map((kind) => (
        <span key={kind} className="mr-2 text-xs font-medium text-unclear">
          {noticeName(kind)}
        </span>
      ))}
    </span>
  );
}

function StudyRecord({
  located: { record },
  current,
  marked,
  rowRef,
  onSelect,
  twin,
  flag,
}: {
  located: Located;
  current: boolean;
  marked: boolean;
  rowRef: (el: HTMLElement | null) => void;
  onSelect: () => void;
  /** Another record of the same report: a duplicate. */
  twin: boolean;
  flag: RecordFlag | undefined;
}) {
  return (
    <li
      ref={rowRef}
      aria-current={current || undefined}
      data-marked={marked || undefined}
      data-testid="study-record"
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
        <Flags flag={flag} />
        {record.title}
        <span className="ml-2 text-ink-3">{ids(record)}</span>
        {flag?.open_access ? (
          <span className="ml-2 text-xs text-include">Open-access location</span>
        ) : null}
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
