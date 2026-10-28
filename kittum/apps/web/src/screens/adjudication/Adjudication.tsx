// Adjudication (DESIGN.md, "Reviewer workbench"): the record with the reviewers' final decisions
// side by side, pseudonymized as A, B, ... The adjudicator adopts a column or decides themselves;
// at full text they read the PDF and adopt evidence as a reviewer does. Only then does the AI's
// reading open, so it cannot anchor them. Each criterion of it shows its
// verdict, rationale, and quote; `,` and `.` step through them, and the record marks the verified
// quote of the one in view. The queue holds human disagreements only, so being in it says nothing
// about the AI.
import { useRouteContext } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import type {
  AdjudicationItem,
  Anchor,
  LayerSummary,
  AdjudicationResult,
  Criterion,
  CriterionVerdict,
  Outcome,
  QuoteSpan,
  ReviewerColumn,
  StageInfo,
} from "../../data/schema.ts";
import { run, WorkbenchError } from "../../data/workbench.ts";
import { codePointIndex } from "../../lib/codepoints.ts";
import { Key, KeyHint } from "../../ui/Key.tsx";
import { useKeys } from "../../ui/keys.ts";
import { OUTCOMES, Status, VERDICTS } from "../../ui/Status.tsx";
import { useProject } from "../project.tsx";
import { PdfViewer } from "../pdf/PdfViewer.tsx";
import type { PageHighlight } from "../pdf/PdfViewer.tsx";
import { AiReading } from "../screening/AiReading.tsx";
import { Mentions, useEvidence } from "../screening/evidence.tsx";
import { Marked, orderSections, placeQuote } from "../screening/RecordText.tsx";

interface Conflict {
  readonly stage: StageInfo;
  readonly item: AdjudicationItem;
}

const STAGE_LABEL = { title_abstract: "Title and abstract", full_text: "Full text" } as const;

export function AdjudicationQueue() {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me, adjudicatedStages } = useProject();
  const [conflicts, setConflicts] = useState<ReadonlyArray<Conflict> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let live = true;
    Promise.all(
      adjudicatedStages.map((stage) =>
        run(
          workbench.adjudications({
            projectId: project.id,
            as: me.actor_id,
            scopeId: stage.scope_id,
            stage: stage.stage,
          }),
        ).then((items) => items.map((item) => ({ stage, item }))),
      ),
    ).then(
      (lists) => live && setConflicts(lists.flat()),
      (e: unknown) =>
        live &&
        setError(e instanceof WorkbenchError ? e.message : "Kittum could not load the conflicts."),
    );
    return () => {
      live = false;
    };
  }, [workbench, project.id, me.actor_id, adjudicatedStages, reload]);

  if (adjudicatedStages.length === 0) {
    return (
      <Message title="Nothing to adjudicate">
        You adjudicate no screening stage of this review. The methods lead names the adjudicators
        when opening a stage.
      </Message>
    );
  }
  if (error) return <Message title="Kittum could not load the conflicts">{error}</Message>;
  if (conflicts === null) return <Message title="Loading the conflicts" />;
  const [first] = conflicts;
  if (!first) {
    return (
      <Message title="No open conflicts">
        A record comes here when two people screen it differently. You see their decisions side by
        side, and the AI&rsquo;s reading only after you decide.
      </Message>
    );
  }
  return (
    <ConflictView
      key={first.item.conflict_id}
      conflict={first}
      waiting={conflicts.length}
      onNext={() => setReload((n) => n + 1)}
    />
  );
}

function ConflictView({
  conflict,
  waiting,
  onNext,
}: {
  conflict: Conflict;
  waiting: number;
  onNext: () => void;
}) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me, labels, criteriaFor, scopeLabel } = useProject();
  const { item, stage } = conflict;
  const criteria = useMemo(
    () => criteriaFor(stage.stage, stage.scope_id),
    [criteriaFor, stage.stage, stage.scope_id],
  );
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [result, setResult] = useState<AdjudicationResult | null>(null);
  // The AI criterion in view once its column is open; its quote is marked in the record.
  const [aiIndex, setAiIndex] = useState(0);
  const [shownAt] = useState(() => new Date().toISOString());
  const nameOf = (c: Criterion) => criterionLabel(c.id, criteria, labels);

  // At full text: the PDF, its mentions, and, once decided, the AI's quotes on the page.
  const fullText = stage.stage === "full_text";
  const [source, setSource] = useState<{
    readonly layer: LayerSummary;
    readonly mentions: ReadonlyMap<string, ReadonlyArray<Anchor>>;
    readonly aiQuotes: Readonly<Record<string, Anchor>>;
  } | null>(null);
  const decided = result !== null;
  useEffect(() => {
    if (!fullText) return;
    let live = true;
    run(
      workbench.adjudicationFullText({
        projectId: project.id,
        as: me.actor_id,
        conflictId: item.conflict_id,
        text: item.text,
      }),
    ).then(
      (found) => live && setSource({ ...found, mentions: new Map(found.mentions) }),
      (e: unknown) =>
        live &&
        setNotice(e instanceof WorkbenchError ? e.message : "Kittum could not load the full text."),
    );
    return () => {
      live = false;
    };
  }, [fullText, decided, workbench, project.id, me.actor_id, item.conflict_id, item.text]);
  const noMentions = useMemo(() => new Map<string, ReadonlyArray<Anchor>>(), []);
  const evidence = useEvidence({
    workbench,
    layerId: source?.layer.id ?? "",
    criteria: fullText ? criteria : [],
    mentions: source?.mentions ?? noMentions,
    nameOf,
  });

  const commit = useCallback(
    async (choice: { adopted: string } | { outcome: Outcome; reason: string | null }) => {
      if (busy || result) return;
      const own = "outcome" in choice;
      // At full text an own exclusion cites evidence for its criterion: what the adjudicator
      // adopted on the page, or else what the reviewers adopted for it.
      const adopted = own && choice.reason ? evidence.stateOf(choice.reason).evidence : [];
      const cited =
        own && choice.reason
          ? item.columns.flatMap((c) =>
              c.criteria
                .filter((r) => r.criterion_id === choice.reason && r.status === "not_met")
                .flatMap((r) => r.evidence_anchor_ids ?? []),
            )
          : [];
      const anchors = adopted.length ? adopted.map((e) => e.id) : [...new Set(cited)];
      if (own && fullText && choice.outcome === "exclude" && !anchors.length) {
        const index = criteria.findIndex((c) => c.id === choice.reason);
        if (index >= 0) evidence.setActive(index);
        setNotice(
          `To exclude on ${criterionLabel(choice.reason ?? "", criteria, labels)}, adopt evidence for it first. Go to a mention with . and press Enter.`,
        );
        return;
      }
      const now = new Date().toISOString();
      setBusy(true);
      setNotice(null);
      try {
        const decided = await run(
          workbench.adjudicate({
            projectId: project.id,
            as: me.actor_id,
            conflictId: item.conflict_id,
            expectedEtag: item.etag,
            adopted: own ? null : choice.adopted,
            decision: own
              ? {
                  outcome: choice.outcome,
                  reason_criterion_id: choice.reason,
                  evidence_anchor_ids: anchors,
                  comment: null,
                  criteria:
                    fullText && choice.reason
                      ? [
                          {
                            criterion_id: choice.reason,
                            status: "not_met",
                            evidence_anchor_ids: anchors,
                          },
                        ]
                      : [],
                }
              : null,
            timing: {
              shown_at: shownAt,
              first_key_at: now,
              decided_at: now,
              reveal_shown_at: null,
            },
          }),
        );
        setAiIndex(firstToShow(decided.ai.criteria, decided.ai.quote_spans));
        evidence.setNotice(null);
        setResult(decided);
      } catch (e) {
        setNotice(e instanceof WorkbenchError ? e.message : "Kittum did not record the decision.");
      } finally {
        setBusy(false);
      }
    },
    [
      busy,
      result,
      workbench,
      project.id,
      me.actor_id,
      item,
      shownAt,
      fullText,
      criteria,
      labels,
      evidence,
    ],
  );

  useKeys((event) => {
    if (event.repeat) return true;
    const key = event.key;
    if (result) {
      if (key === " ") {
        onNext();
        return true;
      }
      if (key === "," || key === ".") {
        const count = result.ai.criteria.length;
        if (count > 0) setAiIndex((i) => (i + (key === "." ? 1 : count - 1)) % count);
        return true;
      }
      return false;
    }
    if (armed) {
      setArmed(false);
      if (key === "Escape") return true;
      const criterion = /^[1-9]$/.test(key) ? criteria[Number(key) - 1] : undefined;
      if (criterion) void commit({ outcome: "exclude", reason: criterion.id });
      else if (/^[1-9]$/.test(key)) setNotice(`There is no criterion ${key}.`);
      return true;
    }
    if (
      fullText &&
      !evidence.busy &&
      ["ArrowDown", "j", "ArrowUp", "k", ",", ".", "Enter"].includes(key)
    ) {
      // The page: the active criterion, its mentions, and adopting evidence for it. What it says
      // replaces the screen's last notice.
      setNotice(null);
      if (key === "ArrowDown" || key === "j") {
        evidence.setActive((a) => Math.min(a + 1, criteria.length - 1));
        return true;
      }
      if (key === "ArrowUp" || key === "k") {
        evidence.setActive((a) => Math.max(a - 1, 0));
        return true;
      }
      if (key === "," || key === ".") {
        evidence.jump(key === "." ? 1 : -1);
        return true;
      }
      if (key === "Enter") {
        void evidence.adopt();
        return true;
      }
    }
    const column = /^[1-9]$/.test(key) ? item.columns[Number(key) - 1] : undefined;
    if (column) {
      void commit({ adopted: column.label });
      return true;
    }
    switch (key) {
      case "i":
        void commit({ outcome: "include", reason: null });
        return true;
      case "u":
        if (stage.stage === "full_text") {
          setNotice("At full text, include, exclude on a criterion, or adopt a column.");
          return true;
        }
        void commit({ outcome: "unclear", reason: null });
        return true;
      case "e":
        setArmed(true);
        return true;
      default:
        return false;
    }
  });

  // Before the decision the page shows the adjudicator's own evidence work; after it, the AI's
  // verified quote of the criterion in view.
  const aiQuote = result
    ? source?.aiQuotes[result.ai.criteria[aiIndex]?.criterion_id ?? ""]
    : undefined;
  const pageHighlights: PageHighlight[] = result
    ? aiQuote
      ? [{ id: aiQuote.id, boxes: aiQuote.boxes, kind: "ai", label: "The AI judge's quote" }]
      : []
    : evidence.highlights;
  const pageBox = (aiQuote ?? evidence.currentMention)?.boxes[0];
  const pageTarget =
    pageBox && (aiQuote ?? evidence.currentMention)
      ? { key: (aiQuote ?? evidence.currentMention)?.id ?? "", box: pageBox }
      : null;

  return (
    <div className="grid h-full grid-rows-[auto_minmax(0,1fr)_auto]">
      <div className="flex h-9 items-center gap-4 border-b border-rule bg-paper px-4 text-xs">
        <span className="text-ink-2">
          {STAGE_LABEL[stage.stage]}, {labels.scope.toLowerCase()} {scopeLabel(stage.scope_id)}
        </span>
        <span className="ml-auto text-ink-3">
          {waiting} {waiting === 1 ? "conflict" : "conflicts"} waiting for you
        </span>
      </div>
      <div className="grid min-h-0 grid-cols-[minmax(0,1fr)_minmax(420px,40%)]">
        {!fullText ? (
          <RecordPane
            item={item}
            quote={
              result
                ? (result.ai.quote_spans[result.ai.criteria[aiIndex]?.criterion_id ?? ""] ?? null)
                : null
            }
          />
        ) : source ? (
          <PdfViewer
            workbench={workbench}
            layer={source.layer}
            highlights={pageHighlights}
            target={pageTarget}
            onSelection={evidence.setSelection}
            label={[
              item.record.registry_ids[0] ?? item.record.doi ?? "Record",
              [item.record.journal, item.record.year].filter(Boolean).join(" "),
            ]
              .filter(Boolean)
              .join(", ")}
          />
        ) : (
          <p className="px-10 py-10 text-ink-3">Loading the full text.</p>
        )}
        <aside
          className="flex min-h-0 flex-col overflow-y-auto border-l border-rule-strong bg-sheet"
          aria-label="Decisions"
        >
          {fullText ? (
            <header className="border-b border-rule px-4 pt-3 pb-2.5">
              <h1
                className="line-clamp-2 text-sm font-semibold"
                title={item.title}
                data-testid="record-title"
              >
                {item.title}
              </h1>
            </header>
          ) : null}
          <div
            className="grid border-b border-rule"
            style={{ gridTemplateColumns: `repeat(${item.columns.length}, minmax(0, 1fr))` }}
          >
            {item.columns.map((column, index) => (
              <Column
                key={column.label}
                column={column}
                index={index}
                criteria={criteria}
                labels={labels}
                chosen={result !== null && sameAs(result, column)}
              />
            ))}
          </div>
          {fullText && !result ? (
            <YourEvidence
              criteria={criteria}
              labels={labels}
              active={evidence.active}
              stateOf={evidence.stateOf}
              mentions={source?.mentions ?? noMentions}
              onActivate={evidence.setActive}
            />
          ) : null}
          {result ? (
            <AiColumn
              result={result}
              criteria={criteria}
              labels={labels}
              active={aiIndex}
              onPage={fullText ? (source?.aiQuotes ?? {}) : null}
            />
          ) : null}
        </aside>
      </div>
      <footer className="border-t border-rule-strong bg-sheet-2">
        {result ? (
          <div className="flex h-10 items-center gap-4 px-4 text-sm" data-testid="adjudicated">
            <Status term={OUTCOMES[result.outcome]}>
              {OUTCOMES[result.outcome].past}
              {result.reason_criterion_id
                ? ` on ${criterionLabel(result.reason_criterion_id, criteria, labels)}`
                : ""}
            </Status>
            <span className="text-ink-2">The decision is final.</span>
            <span className="ml-auto flex items-center gap-4">
              <KeyHint keys={[",", "."]}>The AI&rsquo;s criteria</KeyHint>
              <KeyHint keys={[" "]}>Next conflict</KeyHint>
            </span>
          </div>
        ) : (
          <div className="flex h-10 items-center gap-5 px-4 text-sm">
            {item.columns.map((column, index) => (
              <KeyHint key={column.label} keys={[String(index + 1)]}>
                Adopt {column.label}
              </KeyHint>
            ))}
            <span className="text-ink-3">or your own:</span>
            <KeyHint keys={["i"]}>{OUTCOMES.include.label}</KeyHint>
            <span className="text-exclude">
              <KeyHint keys={["e"]} on={armed}>
                {armed
                  ? `Now press 1 to ${criteria.length} for the criterion`
                  : "Exclude on a criterion"}
              </KeyHint>
            </span>
            {stage.stage === "title_abstract" ? (
              <KeyHint keys={["u"]}>{OUTCOMES.unclear.label}</KeyHint>
            ) : (
              <>
                <KeyHint keys={[",", "."]}>Mentions</KeyHint>
                <KeyHint keys={["Enter"]}>
                  {evidence.selection ? "Adopt selection" : "Adopt"}
                </KeyHint>
              </>
            )}
            <span
              className="ml-auto truncate text-ink-2"
              aria-live="polite"
              data-testid="adjudication-notice"
            >
              {notice ??
                evidence.notice ??
                (busy ? "Recording." : "The AI's reading appears after you decide.")}
            </span>
          </div>
        )}
      </footer>
    </div>
  );
}

const sameAs = (result: AdjudicationResult, column: ReviewerColumn) =>
  result.outcome === column.outcome && result.reason_criterion_id === column.reason_criterion_id;

const criterionLabel = (
  id: string,
  criteria: ReadonlyArray<Criterion>,
  labels: Record<string, string>,
) => {
  const index = criteria.findIndex((c) => c.id === id);
  const criterion = criteria[index];
  return criterion ? `${index + 1} ${labels[criterion.slot] ?? criterion.slot}` : id;
};

function RecordPane({ item, quote }: { item: AdjudicationItem; quote: QuoteSpan | null }) {
  const at = useMemo(() => codePointIndex(item.text), [item.text]);
  const sections = orderSections(item.record.sections);
  const facts = [item.record.journal, item.record.year].filter(Boolean).join(", ");
  const placed = placeQuote(item.title, item.text, quote);
  // A full text is long: show its start, and as far as the marked quote.
  const shown = Math.max(4000, (placed.text?.[1] ?? 0) + 400);
  useEffect(() => {
    if (!quote) return;
    document.querySelector("[data-reveal-quote]")?.scrollIntoView({ block: "center" });
  }, [quote]);
  return (
    <article className="overflow-y-auto bg-sheet px-10 pt-7 pb-10" aria-label="Record">
      <p className="text-xs text-ink-2">{facts}</p>
      <h1
        className="mt-2 max-w-[68ch] text-xl font-semibold tracking-[-0.01em] text-balance"
        data-testid="record-title"
      >
        <Marked text={item.title} highlights={[]} quote={placed.title} describe={() => ""} />
      </h1>
      {sections.length > 0 ? (
        <div className="mt-5 grid max-w-[72ch] gap-y-3">
          {sections.map((section) => (
            <p key={section.start} className={`text-md ${section.trailing ? "text-ink-3" : ""}`}>
              <span className="mr-2 text-xs font-semibold text-ink-2">{section.label}</span>
              <Marked
                text={item.text}
                from={at(section.start)}
                to={at(section.end)}
                highlights={[]}
                quote={placed.text}
                describe={() => ""}
              />
            </p>
          ))}
        </div>
      ) : item.text ? (
        <p className="mt-5 max-w-[72ch] text-md whitespace-pre-line">
          <Marked
            text={item.text}
            to={Math.min(item.text.length, shown)}
            highlights={[]}
            quote={placed.text}
            describe={() => ""}
          />
          {item.text.length > shown ? "…" : ""}
        </p>
      ) : (
        <p className="mt-5 text-md text-ink-3">This record has no abstract.</p>
      )}
    </article>
  );
}

function Column({
  column,
  index,
  criteria,
  labels,
  chosen,
}: {
  column: ReviewerColumn;
  index: number;
  criteria: ReadonlyArray<Criterion>;
  labels: Record<string, string>;
  chosen: boolean;
}) {
  return (
    <section
      className={`border-r border-rule px-4 pt-3 pb-4 last:border-r-0 ${chosen ? "bg-petrol-wash" : ""}`}
      aria-label={`Reviewer ${column.label}`}
      data-testid={`column-${column.label}`}
    >
      <div className="flex items-center gap-2">
        <Key k={String(index + 1)} />
        <span className="text-sm font-semibold">Reviewer {column.label}</span>
      </div>
      <p className="mt-2">
        <Status term={OUTCOMES[column.outcome]} />
      </p>
      {column.reason_criterion_id ? (
        <p className="mt-1 text-xs text-ink-2">
          on {criterionLabel(column.reason_criterion_id, criteria, labels)}
        </p>
      ) : null}
      {column.criteria.length > 0 ? (
        <ul className="mt-2 grid gap-0.5 text-xs">
          {column.criteria.map((c) => (
            <li key={c.criterion_id} className="flex items-center gap-2">
              <Status term={VERDICTS[c.status]}>
                {criterionLabel(c.criterion_id, criteria, labels)}
              </Status>
              {c.evidence_anchor_ids?.length ? (
                <span className="text-ink-3">
                  {c.evidence_anchor_ids.length} evidence{" "}
                  {c.evidence_anchor_ids.length === 1 ? "span" : "spans"}
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
      {column.comment ? (
        <p className="mt-2 border-l-2 border-rule pl-2 text-xs text-ink-2">{column.comment}</p>
      ) : null}
    </section>
  );
}

/** At full text, before the decision: the evidence the adjudicator adopts, per criterion. */
function YourEvidence({
  criteria,
  labels,
  active,
  stateOf,
  mentions,
  onActivate,
}: {
  criteria: ReadonlyArray<Criterion>;
  labels: Record<string, string>;
  active: number;
  stateOf: ReturnType<typeof useEvidence>["stateOf"];
  mentions: ReadonlyMap<string, ReadonlyArray<Anchor>>;
  onActivate: (index: number) => void;
}) {
  return (
    <section className="px-4 pt-3 pb-4" aria-label="Your evidence" data-testid="your-evidence">
      <h2 className="text-sm font-semibold">Your evidence</h2>
      <p className="mt-0.5 text-xs text-ink-3">
        To exclude on your own, adopt evidence for the criterion the report fails. Choose it with ↑
        and ↓.
      </p>
      <ol className="mt-2 grid">
        {criteria.map((c, index) => {
          const state = stateOf(c.id);
          const isActive = index === active;
          return (
            <li
              key={c.id}
              aria-current={isActive || undefined}
              className={`border-b border-rule px-2 py-2 last:border-b-0 ${isActive ? "bg-sheet-2 shadow-[inset_3px_0_0_var(--petrol)]" : ""}`}
              onClick={() => onActivate(index)}
            >
              <div className="flex items-center gap-2 text-xs">
                <span className="text-ink-2">
                  {index + 1} {labels[c.slot] ?? c.slot}
                </span>
                {state.evidence.length > 0 ? (
                  <span className="ml-auto text-ink-2">
                    {state.evidence.length} evidence{" "}
                    {state.evidence.length === 1 ? "span" : "spans"}
                  </span>
                ) : null}
              </div>
              <p className={`mt-0.5 text-xs ${isActive ? "" : "line-clamp-1 text-ink-3"}`}>
                {c.text_en}
              </p>
              {isActive ? (
                <Mentions
                  mentions={mentions.get(c.id) ?? []}
                  cursor={state.cursor}
                  adopted={new Set(state.evidence.map((e) => e.key))}
                  selections={state.evidence.filter((e) => e.key.startsWith("selection-"))}
                />
              ) : null}
            </li>
          );
        })}
      </ol>
    </section>
  );
}

/** The criterion to show first: a verified reason to exclude, then any placed quote. */
const firstToShow = (
  criteria: ReadonlyArray<CriterionVerdict>,
  spans: Readonly<Record<string, QuoteSpan>>,
) => {
  const placed = (c: CriterionVerdict) => c.criterion_id in spans;
  const index = criteria.findIndex((c) => c.status === "not_met" && placed(c));
  return index >= 0 ? index : Math.max(0, criteria.findIndex(placed));
};

function AiColumn({
  result,
  criteria,
  labels,
  active,
  onPage,
}: {
  result: AdjudicationResult;
  criteria: ReadonlyArray<Criterion>;
  labels: Record<string, string>;
  active: number;
  /** At full text, the quotes on the page by criterion; null when the record is text. */
  onPage: Readonly<Record<string, Anchor>> | null;
}) {
  return (
    <section className="reveal-enter px-4 pt-3 pb-4" aria-label="AI judge" data-testid="ai-column">
      <h2 className="text-sm font-semibold">The AI judge&rsquo;s reading</h2>
      <p className="mt-0.5 text-xs text-ink-3">
        {result.ai.excludes
          ? "It found a reason to exclude, with a quote Kittum verified."
          : "It found no reason to exclude that Kittum could verify."}
      </p>
      <ol className="mt-2 grid gap-1">
        {result.ai.criteria.map((c, i) => {
          const index = criteria.findIndex((x) => x.id === c.criterion_id);
          const criterion = criteria[index];
          const span = result.ai.quote_spans[c.criterion_id];
          const page = onPage?.[c.criterion_id]?.boxes[0]?.page;
          const where = onPage
            ? page
              ? `Marked on page ${page}.`
              : null
            : span?.part === "title"
              ? "Marked in the title."
              : "Marked in the record.";
          return (
            <li
              key={c.criterion_id}
              aria-current={i === active || undefined}
              className={`rounded-[2px] px-2 py-1.5 ${i === active ? "evidence-item" : ""}`}
            >
              <AiReading
                criterion={{
                  number: criterion ? index + 1 : null,
                  label: criterion ? (labels[criterion.slot] ?? criterion.slot) : c.criterion_id,
                  text: criterion?.text_en ?? null,
                }}
                status={c.status}
                downgradedFrom={c.downgraded_from}
                rationale={c.rationale}
                quote={c.quote ?? null}
                verified={c.quote_verified}
                markedAt={
                  !span || where === null ? null : i !== active ? "Press , or . to mark it." : where
                }
                marked={i === active}
                compact
              />
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function Message({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="mx-auto max-w-[640px] px-10 py-16">
      <h1 className="text-xl font-semibold">{title}</h1>
      {children ? <p className="mt-2 text-md text-ink-2">{children}</p> : null}
    </div>
  );
}
