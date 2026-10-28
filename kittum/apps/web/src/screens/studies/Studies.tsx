// Records, reports, and studies (DESIGN.md, "Corpus and identities"). Kittum proposes which
// records are duplicates and which reports belong to one study. The methods lead confirms each
// link, and every confirmation, merge, and split is a new identity revision with a ledger event.
// The screen counts what the searches returned, shows each study with its reports, the searches
// that found each record, and why linked records are together. Once the corpus is built, a worker
// enriches it from the open sources; the lead follows the run per source and opens any record to
// see where each of its fields came from.
import { useRouteContext } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type {
  CorpusLink,
  CorpusReport,
  CorpusSearch,
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

/** Search sources whose records are registry entries, not publications. */
const REGISTRIES = new Set(["ctgov", "clinicaltrials", "euctr", "ctis", "ictrp"]);

const day = (iso: string) =>
  new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });

interface Located {
  readonly record: SourceRecord;
  readonly report: CorpusReport;
  /** The search that found the record, when the corpus view names it. */
  readonly search: CorpusSearch | undefined;
}

interface Study {
  readonly studyId: string;
  readonly reports: ReadonlyArray<CorpusReport>;
}

type LinkAction =
  | { readonly action: "confirm"; readonly proposalId: string }
  | { readonly action: "confirm_exact" }
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

const recordCount = (study: Study) => study.reports.reduce((n, r) => n + r.records.length, 0);
const isRegistry = (report: CorpusReport) => report.records.every((r) => REGISTRIES.has(r.source));

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

  const study = labels.study.toLowerCase();
  const studies_ = plural(labels.study).toLowerCase();
  const report_ = labels.report.toLowerCase();
  const reports_ = plural(labels.report).toLowerCase();

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
          setError(
            e instanceof WorkbenchError
              ? e.message
              : "Kittum could not load the corpus. Reload the page to try again.",
          ),
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
            e instanceof WorkbenchError ? e.message : "Kittum could not load the enrichment.",
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
      setEnrichError(
        e instanceof WorkbenchError ? e.message : "Kittum could not start the enrichment.",
      );
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
    const searchById = new Map((corpus?.searches ?? []).map((s) => [s.id, s]));
    const map = new Map<string, Located>();
    for (const report of corpus?.reports ?? []) {
      report.records.forEach((record, i) => {
        const searchId = report.search_ids?.[i];
        map.set(record.occurrence_id, {
          record,
          report,
          search: searchId ? searchById.get(searchId) : undefined,
        });
      });
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
  const exact = pending.filter((p) => p.exact && p.relation === "same_record");
  const decided = proposals.filter((p) => p.status === "confirmed").length;
  const linked = studies.filter((s) => recordCount(s) > 1);
  const single = studies.filter((s) => recordCount(s) === 1);

  const targets = useMemo((): Target[] => {
    const recordTargets = (group: ReadonlyArray<Study>) =>
      group.flatMap((study) =>
        study.reports.flatMap((report) =>
          report.records.map((record): Target => ({
            kind: "record",
            key: record.occurrence_id,
            located: byOccurrence.get(record.occurrence_id) ?? {
              record,
              report,
              search: undefined,
            },
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
  }, [pending, linked, single, byOccurrence]);

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
      setNotice("Only the methods lead confirms links.");
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
      setNotice(e instanceof WorkbenchError ? e.message : "Kittum could not save the change.");
    } finally {
      setBusy(false);
    }
  };

  useKeys((event) => {
    if (event.key === "r" && lead && corpus && !enriching) {
      if (runActive(enrichment)) {
        setNotice("The enrichment is still running. Its progress shows above.");
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
        void act({ action: "confirm", proposalId: current.proposal.id }, "Confirmed the link.");
        return true;
      case "a":
        if (exact.length === 0) {
          setNotice("No open duplicate has the same DOI, PMID, or PMCID.");
          return true;
        }
        void act(
          { action: "confirm_exact" },
          `Confirmed ${exact.length} ${exact.length === 1 ? "duplicate" : "duplicates"}.`,
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
          setNotice(
            `Mark two or more records with x, then press m to merge them into one ${study}.`,
          );
          return true;
        }
        // Records of one study are linked already, perhaps closer (one report found twice):
        // a study merge would add nothing but a revision that says it merged them.
        if (new Set([...marked].map((id) => byOccurrence.get(id)?.report.study_id)).size === 1) {
          setNotice(`These records are one ${study} already. Press s on a record to split it out.`);
          return true;
        }
        void act(
          { action: "merge", occurrenceIds: [...marked], relation: "same_study" },
          `Merged ${marked.size} records into one ${study}.`,
        );
        return true;
      case "s": {
        if (current.kind !== "record") return false;
        const { study: group, located } = current;
        if (recordCount(group) < 2) {
          setNotice("This record is not linked to another record.");
          return true;
        }
        // A study of several reports: take this record's report out of it. One report found
        // twice: take this record out of the report.
        const level = group.reports.length > 1 ? "study" : "report";
        void act(
          { action: "split", occurrenceIds: [located.record.occurrence_id], level },
          level === "study"
            ? `Moved the ${report_} into a ${study} of its own.`
            : `Moved the record into a ${report_} of its own.`,
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

  const relations: Record<LinkProposal["relation"], string> = {
    same_record: "Duplicate records",
    same_report: `Two records of one ${report_}`,
    same_study: `${plural(labels.report)} of one ${study}`,
  };

  const records = corpus?.reports.reduce((n, r) => n + r.records.length, 0) ?? 0;
  const duplicates = records - (corpus?.reports.length ?? 0);
  const recordRow = (located: Located, titled = true) => (
    <StudyRecord
      key={located.record.occurrence_id}
      located={located}
      titled={titled}
      current={isCurrent(located.record.occurrence_id)}
      marked={marked.has(located.record.occurrence_id)}
      rowRef={rowRef(located.record.occurrence_id)}
      onSelect={() => select(located.record.occurrence_id)}
      flag={flags.get(located.record.occurrence_id)}
    />
  );
  const locate = (report: CorpusReport, record: SourceRecord): Located =>
    byOccurrence.get(record.occurrence_id) ?? { record, report, search: undefined };

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
                Screening that is open already uses the links as they were when it opened. Links you
                confirm now count from the next stage.
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
                <CorpusCount
                  searches={corpus.searches ?? []}
                  records={records}
                  duplicates={duplicates}
                  reports={corpus.reports.length}
                  studies={studies.length}
                  openDuplicates={pending.filter((p) => p.relation === "same_record").length}
                  eventSeq={corpus.revision.event_seq ?? 0}
                  reportWord={[report_, reports_]}
                  studyWord={[study, studies_]}
                />

                <EnrichmentPanel run={enrichment} busy={enriching} error={enrichError} />

                <h2 className="mt-10 border-b border-rule-strong pb-1.5 text-lg font-semibold">
                  Proposed links
                  <span className="ml-3 text-sm font-normal text-ink-3">{pending.length} open</span>
                </h2>
                {exact.length > 0 ? (
                  <p className="mt-3 text-sm text-ink-2" data-testid="confirm-exact">
                    <Key k="a" /> confirms all {exact.length}{" "}
                    {exact.length === 1 ? "duplicate" : "duplicates"} with the same DOI, PMID, or
                    PMCID. <Key k="Enter" /> confirms the selected link only.
                  </p>
                ) : null}
                {pending.length === 0 ? (
                  <p className="mt-3 text-ink-2">No proposed link is open.</p>
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
                            <span className="font-semibold">{relations[proposal.relation]}</span>
                            <span className="text-sm text-ink-2">
                              {proposal.evidence.join(". ")}
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
                {decided > 0 ? (
                  <p className="mt-3 text-sm text-ink-3">
                    You confirmed {decided} proposed {decided === 1 ? "link" : "links"}.
                  </p>
                ) : null}

                <h2 className="mt-10 border-b border-rule-strong pb-1.5 text-lg font-semibold">
                  {plural(labels.study)} with more than one record
                  <span className="ml-3 text-sm font-normal text-ink-3">{linked.length}</span>
                </h2>
                {linked.length === 0 ? (
                  <p className="mt-3 text-ink-2">No {study} has more than one record yet.</p>
                ) : (
                  <ol aria-label={`Linked ${studies_}`}>
                    {linked.map((group) => (
                      <LinkedStudy
                        key={group.studyId}
                        group={group}
                        links={corpus.links ?? []}
                        studyWord={study}
                        reportWord={[report_, reports_]}
                        locate={locate}
                        recordRow={recordRow}
                      />
                    ))}
                  </ol>
                )}

                <h2 className="mt-10 border-b border-rule-strong pb-1.5 text-lg font-semibold">
                  Single-record {studies_}
                  <span className="ml-3 text-sm font-normal text-ink-3">{single.length}</span>
                </h2>
                <ol aria-label={`Single-record ${studies_}`}>
                  {single.map((group) =>
                    group.reports.flatMap((report) =>
                      report.records.map((record) => recordRow(locate(report, record))),
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
        <KeyHint keys={["a"]} muted={!lead || exact.length === 0}>
          Confirm all duplicates on an ID
        </KeyHint>
        <KeyHint keys={["x"]} muted={current?.kind !== "record"}>
          Mark
        </KeyHint>
        <KeyHint keys={["m"]} muted={!lead || marked.size < 2}>
          Merge into one {study}
        </KeyHint>
        <KeyHint keys={["s"]} muted={!lead || current?.kind !== "record"}>
          Split out
        </KeyHint>
        <KeyHint keys={["o"]} muted={!lead || current?.kind !== "record"}>
          {detailOpen ? "Close the enrichment" : "Open the enrichment"}
        </KeyHint>
        <KeyHint keys={["r"]} muted={!lead || !corpus || runActive(enrichment)}>
          Enrich again
        </KeyHint>
        <span
          className="ml-auto truncate text-ink-2"
          aria-live="polite"
          data-testid="studies-notice"
        >
          {busy ? "Saving." : notice}
        </span>
      </footer>
    </div>
  );
}

/** What the searches returned and what deduplication made of it, per search and in total. */
function CorpusCount({
  searches,
  records,
  duplicates,
  reports,
  studies,
  openDuplicates,
  eventSeq,
  reportWord: [report, reportsWord],
  studyWord: [study, studiesWord],
}: {
  searches: ReadonlyArray<CorpusSearch>;
  records: number;
  duplicates: number;
  reports: number;
  studies: number;
  openDuplicates: number;
  eventSeq: number;
  reportWord: readonly [string, string];
  studyWord: readonly [string, string];
}) {
  const n = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;
  return (
    <div className="mt-4">
      <p className="max-w-[64ch] text-lg leading-snug" data-testid="corpus-count">
        The searches returned {n(records, "record", "records")}.{" "}
        {duplicates === 0
          ? "None is a duplicate yet."
          : `${duplicates} ${duplicates === 1 ? "is a duplicate" : "are duplicates"}.`}{" "}
        That leaves {n(reports, report, reportsWord)} in {n(studies, study, studiesWord)}.
      </p>
      {openDuplicates > 0 ? (
        <p className="mt-1 text-sm text-unclear" data-testid="open-duplicates">
          Kittum proposes {n(openDuplicates, "more duplicate", "more duplicates")}. Confirm them
          below to count them.
        </p>
      ) : null}
      {searches.length > 0 ? (
        <table className="mt-4 text-sm" aria-label="Searches in the corpus">
          <tbody>
            {searches.map((search) => (
              <tr key={search.id} data-testid="corpus-search">
                <td className="py-0.5 pr-6">{search.platform}</td>
                <td className="py-0.5 pr-6 text-ink-3">{day(search.started_at)}</td>
                <td className="py-0.5 text-right tabular-nums">
                  {n(search.retrieved_count, "record", "records")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      <p className="mt-2 text-xs text-ink-3">As of ledger event {eventSeq}.</p>
    </div>
  );
}

/** Why linked records are together, as one line: the evidence and who decided. */
function reason(link: CorpusLink) {
  if (link.linked_by === "merged_by_hand") {
    return link.evidence.length > 0
      ? `The methods lead merged them by hand. ${link.evidence.join(". ")}.`
      : "The methods lead merged them by hand.";
  }
  return `${link.evidence.join(". ")}. The methods lead confirmed it.`;
}

/** A study of several records: its study links, then each report with its records. */
function LinkedStudy({
  group,
  links,
  studyWord,
  reportWord: [report, reportsWord],
  locate,
  recordRow,
}: {
  group: Study;
  links: ReadonlyArray<CorpusLink>;
  studyWord: string;
  reportWord: readonly [string, string];
  locate: (report: CorpusReport, record: SourceRecord) => Located;
  recordRow: (located: Located, titled?: boolean) => React.ReactNode;
}) {
  const inStudy = new Set(group.reports.flatMap((r) => r.records.map((x) => x.occurrence_id)));
  const studyLinks = links.filter(
    (l) => l.relation === "same_study" && l.occurrence_ids.some((id) => inStudy.has(id)),
  );
  const records = recordCount(group);
  return (
    <li className="border-b border-rule py-4" data-testid="linked-study">
      <p className="pl-5 text-sm text-ink-3">
        One {studyWord},{" "}
        {group.reports.length === 1 ? `1 ${report}` : `${group.reports.length} ${reportsWord}`},{" "}
        {records} records
      </p>
      {studyLinks.map((l) => (
        <Why key={`${l.revision_id}-${l.occurrence_ids.join()}`} label="Linked">
          {reason(l)}
        </Why>
      ))}
      <ul className="mt-2 grid gap-3">
        {group.reports.map((r) => {
          const inReport = new Set(r.records.map((x) => x.occurrence_id));
          const reportLinks = links.filter(
            (l) =>
              l.relation !== "same_study" &&
              l.occurrence_ids.filter((id) => inReport.has(id)).length >= 2,
          );
          const [first] = r.records;
          return (
            <li
              key={r.report_id}
              data-testid="study-report"
              data-registry={isRegistry(r) || undefined}
              className="ml-5 border-l-2 border-rule pl-3"
            >
              <p className="pr-4 text-sm">
                {isRegistry(r) ? (
                  <span className="mr-2 rounded-[2px] bg-sheet-2 px-1.5 py-px text-xs font-medium text-ink-2">
                    Registry entry
                  </span>
                ) : null}
                <span className="font-medium">{first?.title}</span>
                {r.records.length > 1 ? (
                  <span className="ml-2 text-ink-3" data-testid="found-by">
                    Found by {r.records.length} searches
                  </span>
                ) : null}
              </p>
              {reportLinks.map((l) => (
                <Why key={`${l.revision_id}-${l.occurrence_ids.join()}`} label="Merged" inset>
                  {reason(l)}
                </Why>
              ))}
              <ul>{r.records.map((record) => recordRow(locate(r, record), false))}</ul>
            </li>
          );
        })}
      </ul>
    </li>
  );
}

function Why({
  label,
  inset,
  children,
}: {
  label: string;
  inset?: boolean;
  children: React.ReactNode;
}) {
  return (
    <p
      data-testid="link-reason"
      className={`mt-1 grid grid-cols-[4.5rem_minmax(0,1fr)] gap-2 pr-4 text-sm ${inset ? "" : "pl-5"}`}
    >
      <span className="text-ink-3">{label}</span>
      <span className="text-ink-2">{children}</span>
    </p>
  );
}

function RecordLine({
  located,
  flag,
}: {
  located: Located | undefined;
  flag: RecordFlag | undefined;
}) {
  if (!located) {
    return <li className="text-sm text-ink-3">This record is not in the current revision.</li>;
  }
  const { record, search } = located;
  return (
    <li className="grid grid-cols-[8rem_minmax(0,1fr)] gap-3 text-sm">
      <span className="narrow text-ink-3">{search?.platform ?? record.source}</span>
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

/** One search result: the search that found it, then the record as that search returned it.
 * Under its report's title, a record shows its identifiers only. */
function StudyRecord({
  located: { record, search },
  titled,
  current,
  marked,
  rowRef,
  onSelect,
  flag,
}: {
  located: Located;
  titled: boolean;
  current: boolean;
  marked: boolean;
  rowRef: (el: HTMLElement | null) => void;
  onSelect: () => void;
  flag: RecordFlag | undefined;
}) {
  return (
    <li
      ref={rowRef}
      aria-current={current || undefined}
      data-marked={marked || undefined}
      data-testid="study-record"
      onClick={onSelect}
      className={`grid grid-cols-[1.5rem_8rem_minmax(0,1fr)] items-baseline gap-3 py-1.5 pr-4 pl-5 text-sm ${
        current ? "bg-sheet shadow-[inset_3px_0_0_var(--petrol)]" : ""
      } ${marked ? "bg-petrol-wash" : ""}`}
    >
      <span>{marked ? <Key k="x" on /> : null}</span>
      <span className="narrow text-ink-3" data-testid="record-search">
        {search?.platform ?? record.source}
        {search ? <span className="block text-2xs">{day(search.started_at)}</span> : null}
      </span>
      <span>
        <Flags flag={flag} />
        {titled ? record.title : null}
        <span className={titled ? "ml-2 text-ink-3" : "text-ink-2"}>{ids(record)}</span>
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
    record.pmcid ?? null,
    record.doi ? `DOI ${record.doi}` : null,
    ...(record.registry_ids ?? []),
  ]
    .filter(Boolean)
    .join(", ");
