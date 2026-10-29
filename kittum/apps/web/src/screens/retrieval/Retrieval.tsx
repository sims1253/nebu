// Full-text retrieval (DESIGN.md, "Full text"). The methods lead sees every report with its
// outcome at title and abstract, has a worker walk the open-access waterfall for the reports
// sought (every attempt listed under its report), retries the sources that failed, uploads the
// PDFs open access did not give (one at a time or a folder at once), exports the reports still
// without a PDF for whoever can send them, and adds the full-text sentinels that full-text queues
// get unmarked. The list is the lead's alone: it names every record, and a reviewer who saw it
// could tell sentinels.
import { LegendList } from "@legendapp/list/react";
import type { LegendListRef } from "@legendapp/list/react";
import { useRouteContext } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type {
  AttemptView,
  EnrichmentRun,
  HeldUpload,
  OpenAccessRun,
  ReportRetrieval,
  RetrievalItem,
  RetryPreview,
} from "../../data/schema.ts";
import { run } from "../../data/workbench.ts";
import {
  Button,
  failure,
  Field,
  FileInput,
  FormDialog,
  Select,
  TextInput,
} from "../../ui/Form.tsx";
import { KeyHint } from "../../ui/Key.tsx";
import { useKeys } from "../../ui/keys.ts";
import { usePoll } from "../../ui/poll.ts";
import { LEAD_ONLY, plural, useProject } from "../project.tsx";
import { failureLine, runActive, sourceName } from "../studies/Enrichment.tsx";

const OUTCOME: Record<string, { label: string; className: string }> = {
  include: { label: "Included", className: "text-include" },
  exclude: { label: "Excluded", className: "text-exclude" },
  conflict: { label: "In conflict", className: "text-unclear" },
};

/** Sought for retrieval: included at title and abstract in at least one scope. */
const sought = (item: RetrievalItem) =>
  Object.values(item.outcomes).some((stages) => stages["title_abstract"] === "include");

/** "1 report", "3 reports". */
const count = (word: string, n: number) => `${n} ${n === 1 ? word : plural(word)}`;

const NO_FAILURES: RetryPreview = { occurrences: 0, reports: 0, sources: [] };

export function RetrievalScreen() {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me, seesLeadData, labels } = useProject();
  const [items, setItems] = useState<ReadonlyArray<RetrievalItem> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [all, setAll] = useState(false);
  const [selected, setSelected] = useState(0);
  const [uploading, setUploading] = useState(false);
  const [uploadingMany, setUploadingMany] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [addingSentinel, setAddingSentinel] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [openAccess, setOpenAccess] = useState<OpenAccessRun | null>(null);
  const [enrichment, setEnrichment] = useState<EnrichmentRun | null>(null);
  const [failed, setFailed] = useState<RetryPreview>(NO_FAILURES);
  const [held, setHeld] = useState<ReadonlyArray<HeldUpload>>([]);
  const [busy, setBusy] = useState(false);
  const list = useRef<LegendListRef>(null);

  const caller = useMemo(() => ({ projectId: project.id, as: me.actor_id }), [project.id, me]);
  const load = useCallback(
    () =>
      Promise.all([
        run(workbench.fullTexts(caller)),
        run(workbench.openAccess(caller)),
        run(workbench.enrichment(caller)),
        run(workbench.retryPreview(caller)),
        run(workbench.heldUploads(caller)),
      ]).then(
        ([found, latest, enriched, preview, waiting]) => {
          setItems(found);
          setOpenAccess(latest);
          setEnrichment(enriched);
          setFailed(preview);
          setHeld(waiting);
        },
        (e) => setError(failure(e, "Kittum could not load the reports.")),
      ),
    [workbench, caller],
  );
  useEffect(() => {
    if (seesLeadData && project.protocol) void load();
  }, [load, seesLeadData, project.protocol]);
  const working = runActive(openAccess) || runActive(enrichment);
  usePoll(seesLeadData && working, () => void load());

  const byReport = useMemo(
    () =>
      new Map(
        [...(openAccess?.earlier ?? []), ...(openAccess?.reports ?? [])].map((r) => [
          r.report_id,
          r,
        ]),
      ),
    [openAccess],
  );

  const act = async (what: () => Promise<string | null>, fallback: string) => {
    setBusy(true);
    setNotice(null);
    setError(null);
    try {
      setNotice(await what());
      await load();
    } catch (e) {
      setError(failure(e, fallback));
    } finally {
      setBusy(false);
    }
  };

  const fetchOpenAccess = () =>
    act(async () => {
      setOpenAccess(await run(workbench.startOpenAccess(caller)));
      setAll(false);
      return null;
    }, "Kittum could not start fetching.");

  const retry = () =>
    act(async () => {
      const plan = await run(workbench.retryFailedSources(caller));
      const names = plan.sources.map(sourceName).join(" and ");
      return plan.enrichment_run_id
        ? `Retry started for ${names}. Kittum enriches ${count("record", plan.occurrences)} again, then fetches ${count("report", plan.reports)}.`
        : `Retry started for ${names}. Kittum fetches ${count("report", plan.reports)} again.`;
    }, "Kittum could not start the retry.");

  const screened = (items ?? []).some((i) => Object.keys(i.outcomes).length > 0);
  const shown = useMemo(
    () => (items ?? []).filter((i) => all || !screened || sought(i)),
    [items, all, screened],
  );
  const current = shown[Math.min(selected, shown.length - 1)];
  const retrieved = shown.filter((i) => i.full_text).length;
  const missing = (items ?? []).filter((i) => (screened ? sought(i) : true) && !i.full_text);
  const canFetch = screened && !busy && !working;
  const canRetry = failed.sources.length > 0 && !busy && !working;

  useEffect(() => {
    if (shown.length)
      void list.current?.scrollIndexIntoView({ index: Math.min(selected, shown.length - 1) });
  }, [selected, shown.length]);

  useKeys((event) => {
    if (!items) return false;
    switch (event.key) {
      case "j":
      case "ArrowDown":
        setSelected((i) => Math.min(i + 1, shown.length - 1));
        return true;
      case "k":
      case "ArrowUp":
        setSelected((i) => Math.max(i - 1, 0));
        return true;
      case "u":
        if (!current) return false;
        if (current.full_text) {
          setNotice("This report has a PDF already.");
          return true;
        }
        setUploading(true);
        return true;
      case "b":
        setUploadingMany(true);
        return true;
      case "e":
        if (!screened || missing.length === 0) return false;
        setExporting(true);
        return true;
      case "s":
        setAddingSentinel(true);
        return true;
      case "o":
        if (!screened) return false;
        if (working) {
          setNotice("Kittum is still fetching. Each report lists its attempts as they happen.");
          return true;
        }
        void fetchOpenAccess();
        return true;
      case "r":
        if (!canRetry) return false;
        void retry();
        return true;
      case "f":
        if (!screened) return false;
        setAll((a) => !a);
        setSelected(0);
        return true;
      default:
        return false;
    }
  });

  if (!seesLeadData) {
    return (
      <Shell>
        <h1 className="text-xl font-semibold tracking-[-0.01em]">Retrieval</h1>
        <p className="mt-4 max-w-[60ch] text-ink-2" data-testid="lead-only">
          {LEAD_ONLY}
        </p>
      </Shell>
    );
  }

  return (
    <div className="grid h-full grid-rows-[auto_minmax(0,1fr)_auto] bg-paper">
      <header className="mx-auto w-full max-w-[1180px] px-10 pt-8 pb-4">
        <div className="flex items-baseline gap-6">
          <h1 className="text-xl font-semibold tracking-[-0.01em]">Retrieval</h1>
          <span className="text-sm text-ink-2" data-testid="retrieval-counts">
            {items
              ? `${retrieved} of ${shown.length} ${screened && !all ? "reports sought" : plural(labels.report).toLowerCase()} have a PDF`
              : ""}
          </span>
          <span className="ml-auto flex flex-wrap gap-x-5 text-sm">
            <KeyHint keys={["j", "k"]}>Move</KeyHint>
            <KeyHint keys={["u"]} muted={!current || Boolean(current.full_text)}>
              Upload its PDF
            </KeyHint>
            <KeyHint keys={["b"]}>Upload many PDFs</KeyHint>
            <KeyHint keys={["s"]}>Add a full-text sentinel</KeyHint>
            {screened ? (
              <KeyHint keys={["f"]}>{all ? "Only those sought" : "Every report"}</KeyHint>
            ) : null}
          </span>
        </div>
        <p className="mt-2 max-w-[80ch] text-sm text-ink-2">
          {!project.protocol
            ? "Publish the protocol and build the corpus first."
            : screened
              ? `${all ? "These are all reports in the corpus." : "These are the reports included at title and abstract in at least one scope."} Full-text screening opens on a report once it has a PDF.`
              : "No stage is screened yet, so the list shows every report in the corpus. A PDF you attach now waits until its report passes title and abstract."}
        </p>
        {screened ? (
          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1">
            <Button primary keys="o" disabled={!canFetch} onClick={() => void fetchOpenAccess()}>
              {runActive(openAccess) ? "Fetching open-access PDFs" : "Fetch open-access PDFs"}
            </Button>
            <span
              className="max-w-[80ch] text-sm text-ink-2"
              data-testid="open-access-status"
              aria-live="polite"
            >
              {openAccess
                ? openAccessLine(openAccess)
                : "Kittum tries Europe PMC, PubMed Central, Unpaywall, OpenAlex, the publisher, medRxiv and bioRxiv, arXiv, Semantic Scholar, and CORE, in that order."}
            </span>
          </div>
        ) : null}
        {(openAccess?.skipped ?? []).length > 0 ? (
          <p className="mt-1 text-sm text-ink-2" data-testid="skipped-sources">
            {(openAccess?.skipped ?? [])
              .map((s) => `Kittum skipped ${sourceName(s.source)} because ${s.reason}.`)
              .join(" ")}
          </p>
        ) : null}
        <Failures
          failed={failed}
          enrichment={enrichment}
          canRetry={canRetry}
          onRetry={() => void retry()}
        />
        {screened && items && missing.length > 0 ? (
          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1">
            <Button keys="e" onClick={() => setExporting(true)}>
              Export the reports without a PDF
            </Button>
            <span className="text-sm text-ink-2" data-testid="missing-count">
              {`${count("report", missing.length)} sought ${missing.length === 1 ? "has" : "have"} no PDF. Send the list to whoever can get the PDFs, then upload them with b.`}
            </span>
          </div>
        ) : null}
        {error || notice ? (
          <p
            role={error ? "alert" : "status"}
            className={`mt-2 text-sm ${error ? "text-exclude" : ""}`}
            data-testid="retrieval-notice"
          >
            {error ?? notice}
          </p>
        ) : null}
        {held.length > 0 ? (
          <Held
            held={held}
            items={items ?? []}
            onDone={(message) => {
              setNotice(message);
              void load();
            }}
            onError={(message) => setError(message)}
          />
        ) : null}
      </header>
      <section
        aria-label="Reports"
        className="mx-auto min-h-0 w-full max-w-[1180px] border-t border-rule-strong px-10"
      >
        {items === null ? (
          <p className="pt-4 text-ink-3">{project.protocol ? "Loading the reports." : ""}</p>
        ) : shown.length === 0 ? (
          <p className="pt-4 text-ink-2">
            {screened
              ? "No report is included at title and abstract yet. Press f to see every report."
              : "The corpus has no reports yet."}
          </p>
        ) : (
          <LegendList
            ref={list}
            data={shown}
            extraData={current?.item_ref}
            keyExtractor={(item) => item.item_ref}
            estimatedItemSize={64}
            style={{ height: "100%" }}
            renderItem={({ item, index }) => (
              <Row
                item={item}
                retrieval={byReport.get(item.report_id)}
                selected={item.item_ref === current?.item_ref}
                onSelect={() => setSelected(index)}
              />
            )}
          />
        )}
      </section>
      <footer className="h-6" />
      {current && uploading ? (
        <UploadFullText
          item={current}
          onClose={() => setUploading(false)}
          onDone={(message) => {
            setUploading(false);
            setNotice(message);
            void load();
          }}
        />
      ) : null}
      {uploadingMany ? (
        <UploadMany
          onClose={() => setUploadingMany(false)}
          onDone={(message) => {
            setUploadingMany(false);
            setNotice(message);
            void load();
          }}
        />
      ) : null}
      {exporting ? (
        <ExportMissing
          reports={missing.length}
          onClose={() => setExporting(false)}
          onDone={(message) => {
            setExporting(false);
            setNotice(message);
          }}
        />
      ) : null}
      <AddSentinel
        open={addingSentinel}
        onOpenChange={setAddingSentinel}
        onDone={(message) => {
          setAddingSentinel(false);
          setNotice(message);
        }}
      />
    </div>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <section className="h-full overflow-y-auto bg-paper">
      <div className="mx-auto max-w-[980px] px-10 pt-10 pb-16">{children}</div>
    </section>
  );
}

/** The sources that failed, in plain words, and the one action that mends an outage. */
function Failures({
  failed,
  enrichment,
  canRetry,
  onRetry,
}: {
  failed: RetryPreview;
  enrichment: EnrichmentRun | null;
  canRetry: boolean;
  onRetry: () => void;
}) {
  const retrying = enrichment?.trigger === "retry" && runActive(enrichment);
  if (retrying) {
    return (
      <p className="mt-3 text-sm text-ink-2" data-testid="retry-status" aria-live="polite">
        {`Kittum is enriching ${count("record", enrichment.records)} again (${enrichment.done} done). Fetching follows.`}
      </p>
    );
  }
  if (failed.sources.length === 0) return null;
  return (
    <section
      aria-label="Sources that failed"
      data-testid="retry"
      className="mt-4 flex max-w-[1000px] items-start gap-5 border-l-[3px] border-exclude bg-exclude-wash py-2.5 pr-4 pl-4"
    >
      <ul className="grid min-w-0 flex-1 gap-1 text-sm">
        {failed.sources.map((s) => {
          const counts = [
            s.records ? count("record", s.records) : null,
            s.reports ? count("report", s.reports) : null,
          ]
            .filter(Boolean)
            .join(" and ");
          return (
            <li key={s.source} data-testid="retry-line" data-source={s.source}>
              {failureLine(s.source, s, counts)}
            </li>
          );
        })}
      </ul>
      <Button primary keys="r" disabled={!canRetry} onClick={onRetry} className="shrink-0">
        Retry failed sources
      </Button>
    </section>
  );
}

const OUTCOME_LINE: Record<string, string> = {
  acquired: "Fetched from open access.",
  not_acquired: "No source gave a usable PDF. Upload it with u.",
  no_location: "No source has an open copy. Upload it with u.",
  had_full_text: "Uploaded by hand while the run waited.",
};

const serverTrouble = (status: number | null | undefined) =>
  status != null && (status >= 500 || status === 429);

const ATTEMPT: Record<string, (a: AttemptView) => string> = {
  acquired: () => "Fetched",
  http_error: (a) => (serverTrouble(a.status) ? "No answer" : "Refused") + `, HTTP ${a.status}`,
  not_pdf: () => "Web page, not a PDF",
  unreadable_pdf: () => "PDF does not open",
  unavailable: (a) => (a.status ? `No answer, HTTP ${a.status}` : "No answer"),
  wrong_document: () => "Wrong document",
  found: (a) => `Named ${count("file", a.found ?? 0)}`,
  known: () => "Named a file already listed",
  none: () => "No open copy",
  other_work: () => "Answered for another work",
};

function openAccessLine(r: OpenAccessRun) {
  const tried = r.reports.filter((x) => x.outcome).length;
  const reports = count("report", r.reports.length);
  const again = r.trigger === "retry" ? "again " : "";
  if (r.status === "queued")
    return `Waiting for a worker to try ${reports} ${again}.`.replace(" .", ".");
  if (r.status === "running") return `Tried ${tried} of ${reports} ${again}so far.`;
  if (r.status === "failed") return `Stopped after ${tried} of ${reports}. ${r.error ?? ""}`.trim();
  const got = r.reports.filter((x) => x.outcome === "acquired").length;
  return `Fetched ${got} of ${reports}${r.trigger === "retry" ? " on the retry" : ""}. The others need a PDF from you.`;
}

function Attempts({ retrieval }: { retrieval: ReportRetrieval }) {
  return (
    <div
      className="col-span-3 mt-1.5"
      data-testid="open-access"
      data-outcome={retrieval.outcome ?? "pending"}
    >
      <p className={`text-xs ${retrieval.outcome === "acquired" ? "text-include" : "text-ink-2"}`}>
        {retrieval.outcome ? OUTCOME_LINE[retrieval.outcome] : "Waiting its turn."}
      </p>
      {retrieval.attempts.length > 0 ? (
        <table
          aria-label="Open-access attempts"
          className="mt-1 w-full max-w-[56rem] table-fixed text-xs"
        >
          <tbody>
            {retrieval.attempts.map((a, i) => (
              <tr
                key={`${i}-${a.url}`}
                data-testid="attempt"
                data-kind={a.kind}
                className="text-ink-2"
              >
                <td className="w-6 py-0.5 text-ink-3 tabular-nums">{i + 1}</td>
                <td className="w-36 py-0.5">{sourceName(a.source)}</td>
                <td className="py-0.5">
                  <span title={a.url}>{a.kind === "lookup" ? `Asked ${a.host}` : a.host}</span>
                </td>
                <td
                  className={`w-52 py-0.5 ${
                    a.outcome === "acquired"
                      ? "font-medium text-include"
                      : a.outcome === "found" || a.outcome === "known" || a.outcome === "none"
                        ? "text-ink-2"
                        : "text-exclude"
                  }`}
                  title={[a.check, a.message, a.error].filter(Boolean).join(" ") || undefined}
                >
                  {(ATTEMPT[a.outcome] ?? (() => a.outcome))(a)}
                </td>
                <td className="w-32 py-0.5 text-right">
                  {a.kind === "lookup" ? "" : (a.licence ?? "No licence stated")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  );
}

function Row({
  item,
  retrieval,
  selected,
  onSelect,
}: {
  item: RetrievalItem;
  retrieval: ReportRetrieval | undefined;
  selected: boolean;
  onSelect: () => void;
}) {
  const { scopeLabel } = useProject();
  const facts = [item.journal, item.year].filter(Boolean).join(" ");
  const ids = [item.doi, ...item.registry_ids].filter(Boolean).join(", ");
  return (
    <article
      onClick={onSelect}
      aria-current={selected || undefined}
      data-testid="retrieval-row"
      className={`grid grid-cols-[minmax(0,1fr)_14rem_11rem] items-baseline gap-x-5 border-b border-rule py-2.5 pr-3 pl-4 ${
        selected ? "bg-sheet shadow-[inset_3px_0_0_var(--petrol)]" : ""
      }`}
    >
      <div className="min-w-0">
        <h2 className="truncate font-medium" title={item.title}>
          {item.title}
        </h2>
        <p className="narrow truncate text-xs text-ink-3">
          {[facts, ids].filter(Boolean).join(", ") || "No journal or identifier recorded"}
        </p>
      </div>
      <p className="narrow text-xs">
        {Object.entries(item.outcomes).length === 0 ? (
          <span className="text-ink-3">Not screened yet</span>
        ) : (
          Object.entries(item.outcomes).map(([scope, stages]) => {
            const outcome = stages["title_abstract"];
            const shown = outcome ? OUTCOME[outcome] : null;
            return (
              <span key={scope} className="block">
                <span className="text-ink-3">{scopeLabel(scope)}: </span>
                <span className={shown?.className ?? "text-ink-2"}>
                  {shown?.label ?? "Being screened"}
                </span>
              </span>
            );
          })
        )}
      </p>
      <p className="narrow text-xs" data-testid="full-text-status">
        {item.full_text ? (
          <span className="text-include">
            PDF, {item.full_text.page_count ?? "?"} pages
            <span className="block text-ink-3">
              {item.full_text.licence ?? "No licence recorded"}
              {retrieval?.outcome === "acquired" && retrieval.source
                ? `, from ${sourceName(retrieval.source)}`
                : ""}
            </span>
          </span>
        ) : (
          <span className="text-ink-3">No PDF</span>
        )}
      </p>
      {item.wrong_document ? (
        <p className="col-span-3 mt-1.5 text-xs text-unclear" data-testid="wrong-document">
          {`${item.wrong_document.reviewer} said the earlier PDF was the wrong document.`}
          {item.wrong_document.note ? (
            <span className="text-ink-2">{` Their note: "${item.wrong_document.note}"`}</span>
          ) : null}
        </p>
      ) : null}
      {retrieval ? <Attempts retrieval={retrieval} /> : null}
    </article>
  );
}

/** Uploads waiting for the lead: each with why Kittum could not place it, and a report to pick. */
function Held({
  held,
  items,
  onDone,
  onError,
}: {
  held: ReadonlyArray<HeldUpload>;
  items: ReadonlyArray<RetrievalItem>;
  onDone: (message: string) => void;
  onError: (message: string) => void;
}) {
  return (
    <section aria-labelledby="held-heading" className="mt-4" data-testid="held-uploads">
      <h2 id="held-heading" className="text-sm font-semibold">
        {count("PDF", held.length)} to assign by hand
      </h2>
      <ul className="mt-1.5 grid gap-1.5">
        {held.map((upload) => (
          <HeldRow
            key={upload.upload_id}
            upload={upload}
            items={items}
            onDone={onDone}
            onError={onError}
          />
        ))}
      </ul>
    </section>
  );
}

function HeldRow({
  upload,
  items,
  onDone,
  onError,
}: {
  upload: HeldUpload;
  items: ReadonlyArray<RetrievalItem>;
  onDone: (message: string) => void;
  onError: (message: string) => void;
}) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me } = useProject();
  const candidates = upload.candidates.map((c) => c.report_id);
  const others = items.filter((i) => !i.full_text && !candidates.includes(i.report_id));
  const [report, setReport] = useState(candidates[0] ?? "");
  const [busy, setBusy] = useState(false);
  const caller = { projectId: project.id, as: me.actor_id, uploadId: upload.upload_id };
  const title = (id: string) =>
    upload.candidates.find((c) => c.report_id === id)?.title ??
    items.find((i) => i.report_id === id)?.title ??
    id;
  const go = async (assign: boolean) => {
    setBusy(true);
    try {
      if (assign) {
        await run(workbench.assignUpload({ ...caller, reportId: report }));
        onDone(`Attached ${upload.file_name} to "${title(report)}".`);
      } else {
        await run(workbench.discardUpload(caller));
        onDone(`Discarded ${upload.file_name}.`);
      }
    } catch (e) {
      onError(failure(e, `Kittum could not ${assign ? "attach" : "discard"} ${upload.file_name}.`));
    } finally {
      setBusy(false);
    }
  };
  return (
    <li
      data-testid="held-upload"
      className="grid grid-cols-[minmax(0,1fr)_22rem_auto] items-center gap-x-4 border-b border-rule py-1.5 text-sm"
    >
      <div className="min-w-0">
        <p className="truncate font-medium" title={upload.file_name}>
          {upload.file_name}
          <span className="font-normal text-ink-3">
            {upload.page_count ? `, ${count("page", upload.page_count)}` : ""}
          </span>
        </p>
        <p className="text-xs text-ink-2">{upload.reason}</p>
      </div>
      <Select
        aria-label={`Report for ${upload.file_name}`}
        value={report}
        onChange={(e) => setReport(e.currentTarget.value)}
      >
        <option value="" disabled>
          Choose its report
        </option>
        {candidates.length > 0 ? (
          <optgroup label="Kittum suggests">
            {candidates.map((id) => (
              <option key={id} value={id}>
                {title(id)}
              </option>
            ))}
          </optgroup>
        ) : null}
        <optgroup label="Reports without a PDF">
          {others.map((i) => (
            <option key={i.report_id} value={i.report_id}>
              {i.title}
            </option>
          ))}
        </optgroup>
      </Select>
      <span className="flex gap-2">
        <Button disabled={busy || !report} onClick={() => void go(true)}>
          Attach
        </Button>
        <Button disabled={busy} onClick={() => void go(false)}>
          Discard
        </Button>
      </span>
    </li>
  );
}

const readPdf = async (file: File) => new Uint8Array(await file.arrayBuffer());

function UploadFullText({
  item,
  onClose,
  onDone,
}: {
  item: RetrievalItem;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me } = useProject();
  const [file, setFile] = useState<File | null>(null);
  const [licence, setLicence] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    if (!file) {
      setError("Choose the PDF.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const attached = await run(
        workbench.attachFullText({
          projectId: project.id,
          as: me.actor_id,
          itemRef: item.item_ref,
          pdf: await readPdf(file),
          licence: licence.trim() || null,
        }),
      );
      onDone(`Attached a ${attached.page_count ?? "?"}-page PDF to "${item.title}".`);
    } catch (e) {
      setError(failure(e, "Kittum could not attach the PDF."));
    } finally {
      setBusy(false);
    }
  };
  return (
    <FormDialog
      open
      onOpenChange={(open) => !open && onClose()}
      title="Upload the full text"
      description={item.title}
      submit="Attach the PDF"
      busy={busy}
      error={error}
      onSubmit={() => void submit()}
    >
      <Field
        label="PDF"
        hint="Kittum stores the file and reads its text for screening and extraction."
      >
        <FileInput label="PDF file" accept="application/pdf,.pdf" onFile={setFile} />
      </Field>
      <Field
        label="Licence"
        hint="As the publisher states it, for example CC BY 4.0. Reports cite it."
      >
        <TextInput value={licence} onChange={(e) => setLicence(e.currentTarget.value)} />
      </Field>
    </FormDialog>
  );
}

function UploadMany({
  onClose,
  onDone,
}: {
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me } = useProject();
  const [files, setFiles] = useState<ReadonlyArray<File>>([]);
  const [licence, setLicence] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    if (files.length === 0) {
      setError("Choose the PDFs.");
      return;
    }
    setBusy(true);
    setError(null);
    let attached = 0;
    let waiting = 0;
    const refused: string[] = [];
    for (const [i, file] of files.entries()) {
      try {
        const result = await run(
          workbench.uploadFullText({
            projectId: project.id,
            as: me.actor_id,
            fileName: file.name,
            pdf: await readPdf(file),
            licence: licence.trim() || null,
          }),
        );
        if (result.status === "attached") attached += 1;
        else waiting += 1;
      } catch (e) {
        refused.push(`Kittum refused ${file.name}. ${failure(e, "It was not uploaded.")}`);
      }
      setDone(i + 1);
    }
    setBusy(false);
    const parts = [`Attached ${attached} of ${count("PDF", files.length)}.`];
    if (waiting) parts.push(`${waiting} ${waiting === 1 ? "waits" : "wait"} for you to assign.`);
    parts.push(...refused);
    onDone(parts.join(" "));
  };
  return (
    <FormDialog
      open
      onOpenChange={(open) => !open && !busy && onClose()}
      title="Upload many PDFs"
      description="Kittum matches each PDF to a report without one by the DOI or PMID in its file name, or by the DOI or title on its first pages. You assign the rest by hand."
      submit={files.length ? `Upload ${count("PDF", files.length)}` : "Upload"}
      busy={busy}
      error={error}
      onSubmit={() => void submit()}
    >
      <Field
        label="PDFs"
        hint="A file named after its DOI (10.1186_s12916-026-04943-4.pdf) or PMID matches by name."
      >
        <input
          type="file"
          multiple
          aria-label="PDF files"
          accept="application/pdf,.pdf"
          onChange={(e) => setFiles([...(e.currentTarget.files ?? [])])}
          className="block w-full text-sm text-ink-2 file:mr-3 file:rounded-[3px] file:border file:border-rule-strong file:bg-sheet-2 file:px-2.5 file:py-1 file:text-sm file:text-ink hover:file:bg-sheet"
        />
      </Field>
      <Field
        label="Licence"
        hint="Applies to every PDF of this upload. Leave it empty if they differ."
      >
        <TextInput value={licence} onChange={(e) => setLicence(e.currentTarget.value)} />
      </Field>
      {busy ? (
        <p className="text-sm text-ink-2" aria-live="polite">
          {`Uploaded ${done} of ${files.length}.`}
        </p>
      ) : null}
    </FormDialog>
  );
}

function ExportMissing({
  reports,
  onClose,
  onDone,
}: {
  reports: number;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me } = useProject();
  const [format, setFormat] = useState<"csv" | "ris">("csv");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const bytes = await run(
        workbench.exportMissing({ projectId: project.id, as: me.actor_id, format }),
      );
      const name = `missing-full-texts.${format}`;
      const url = URL.createObjectURL(
        new Blob([bytes as BlobPart], {
          type: format === "csv" ? "text/csv" : "application/x-research-info-systems",
        }),
      );
      const link = document.createElement("a");
      link.href = url;
      link.download = name;
      link.click();
      URL.revokeObjectURL(url);
      onDone(`Saved ${name} with ${count("report", reports)}.`);
    } catch (e) {
      setError(failure(e, "Kittum could not export the list."));
    } finally {
      setBusy(false);
    }
  };
  return (
    <FormDialog
      open
      onOpenChange={(open) => !open && onClose()}
      title="Export the reports without a PDF"
      description={`The file lists ${count("report", reports)} with DOI, PMID, title, authors, journal, year, the sources Kittum tried, and why each attempt failed. It also gives each PDF a file name that Upload many matches.`}
      submit="Download"
      busy={busy}
      error={error}
      onSubmit={() => void submit()}
    >
      <Field label="Format">
        <Select value={format} onChange={(e) => setFormat(e.currentTarget.value as "csv" | "ris")}>
          <option value="csv">CSV, for a spreadsheet</option>
          <option value="ris">RIS, for a reference manager</option>
        </Select>
      </Field>
    </FormDialog>
  );
}

function AddSentinel({
  open,
  onOpenChange,
  onDone,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDone: (message: string) => void;
}) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me } = useProject();
  const [title, setTitle] = useState("");
  const [outcome, setOutcome] = useState<"include" | "exclude">("exclude");
  const [file, setFile] = useState<File | null>(null);
  const [licence, setLicence] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    if (!title.trim() || !file) {
      setError("Give the report's title and its PDF.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await run(
        workbench.attachSentinelFullText({
          projectId: project.id,
          as: me.actor_id,
          title: title.trim(),
          expectedOutcome: outcome,
          pdf: await readPdf(file),
          licence: licence.trim() || null,
        }),
      );
      setTitle("");
      setFile(null);
      onDone(
        `Added the sentinel "${title.trim()}". Every full-text stage you open from now on puts it in each queue.`,
      );
    } catch (e) {
      setError(failure(e, "Kittum could not add the sentinel."));
    } finally {
      setBusy(false);
    }
  };
  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Add a full-text sentinel"
      description="A report whose outcome you know. Every full-text queue gets it like any other report. A reviewer who decides it wrongly sets off the drift alarm. It does not show in this list."
      submit="Add the sentinel"
      busy={busy}
      error={error}
      onSubmit={() => void submit()}
    >
      <Field label="Title">
        <TextInput value={title} onChange={(e) => setTitle(e.currentTarget.value)} />
      </Field>
      <Field label="Expected outcome">
        <Select
          value={outcome}
          onChange={(e) => setOutcome(e.currentTarget.value as "include" | "exclude")}
        >
          <option value="exclude">Exclude</option>
          <option value="include">Include</option>
        </Select>
      </Field>
      <Field label="PDF">
        <FileInput label="Sentinel PDF" accept="application/pdf,.pdf" onFile={setFile} />
      </Field>
      <Field label="Licence">
        <TextInput value={licence} onChange={(e) => setLicence(e.currentTarget.value)} />
      </Field>
    </FormDialog>
  );
}
