// Adjudication (DESIGN.md, "Reviewer workbench"): the record with the reviewers' final decisions
// side by side, pseudonymized as A, B, ... The adjudicator adopts a column or decides themselves;
// only then does the AI's reading open, so it cannot anchor them. The queue holds human
// disagreements only, so being in it says nothing about the AI.
import { useRouteContext } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import type {
  AdjudicationItem,
  AdjudicationResult,
  Criterion,
  Outcome,
  ReviewerColumn,
  StageInfo,
} from "../../data/schema.ts";
import { run, WorkbenchError } from "../../data/workbench.ts";
import { codePointIndex } from "../../lib/codepoints.ts";
import { Key, KeyHint } from "../../ui/Key.tsx";
import { useKeys } from "../../ui/keys.ts";
import { OUTCOMES, Status, VERDICTS } from "../../ui/Status.tsx";
import { useProject } from "../project.tsx";
import { Marked, orderSections } from "../screening/RecordText.tsx";

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
        live && setError(e instanceof WorkbenchError ? e.message : "The conflicts could not load."),
    );
    return () => {
      live = false;
    };
  }, [workbench, project.id, me.actor_id, adjudicatedStages, reload]);

  if (adjudicatedStages.length === 0) {
    return (
      <Message title="Nothing to adjudicate">
        You adjudicate no screening stage of this review. The methods lead names the adjudicators
        when a stage opens.
      </Message>
    );
  }
  if (error) return <Message title="The conflicts could not load">{error}</Message>;
  if (conflicts === null) return <Message title="Loading the conflicts" />;
  const [first] = conflicts;
  if (!first) {
    return (
      <Message title="No open conflicts">
        When two people screen a record differently, it comes here, with their decisions side by
        side and without the AI&rsquo;s reading.
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
  const [shownAt] = useState(() => new Date().toISOString());

  const commit = useCallback(
    async (choice: { adopted: string } | { outcome: Outcome; reason: string | null }) => {
      if (busy || result) return;
      const now = new Date().toISOString();
      setBusy(true);
      setNotice(null);
      try {
        const own = "outcome" in choice;
        setResult(
          await run(
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
                    evidence_anchor_ids: [],
                    comment: null,
                  }
                : null,
              timing: {
                shown_at: shownAt,
                first_key_at: now,
                decided_at: now,
                reveal_shown_at: null,
              },
            }),
          ),
        );
      } catch (e) {
        setNotice(e instanceof WorkbenchError ? e.message : "The decision was not recorded.");
      } finally {
        setBusy(false);
      }
    },
    [busy, result, workbench, project.id, me.actor_id, item, shownAt],
  );

  useKeys((event) => {
    if (event.repeat) return true;
    const key = event.key;
    if (result) {
      if (key === " ") {
        onNext();
        return true;
      }
      return false;
    }
    if (armed) {
      setArmed(false);
      const criterion = /^[1-9]$/.test(key) ? criteria[Number(key) - 1] : undefined;
      if (criterion) void commit({ outcome: "exclude", reason: criterion.id });
      else if (/^[1-9]$/.test(key)) setNotice(`There is no criterion ${key}.`);
      return true;
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
          setNotice("At full text, decide include or exclude, or adopt a column.");
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
        <RecordPane item={item} />
        <aside
          className="flex min-h-0 flex-col overflow-y-auto border-l border-rule-strong bg-sheet"
          aria-label="Decisions"
        >
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
          {result ? <AiColumn result={result} criteria={criteria} labels={labels} /> : null}
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
            <span className="ml-auto">
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
                {armed ? `Exclude: now 1 to ${criteria.length}` : "Exclude on a criterion"}
              </KeyHint>
            </span>
            {stage.stage === "title_abstract" ? (
              <KeyHint keys={["u"]}>{OUTCOMES.unclear.label}</KeyHint>
            ) : null}
            <span className="ml-auto truncate text-ink-2" aria-live="polite">
              {notice ?? (busy ? "Recording." : "The AI's reading opens after you decide.")}
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

function RecordPane({ item }: { item: AdjudicationItem }) {
  const at = useMemo(() => codePointIndex(item.text), [item.text]);
  const sections = orderSections(item.record.sections);
  const facts = [item.record.journal, item.record.year].filter(Boolean).join(", ");
  return (
    <article className="overflow-y-auto bg-sheet px-10 pt-7 pb-10" aria-label="Record">
      <p className="text-xs text-ink-2">{facts}</p>
      <h1
        className="mt-2 max-w-[68ch] text-xl font-semibold tracking-[-0.01em] text-balance"
        data-testid="record-title"
      >
        {item.title}
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
                quote={null}
                describe={() => ""}
              />
            </p>
          ))}
        </div>
      ) : (
        <p className="mt-5 max-w-[72ch] text-md whitespace-pre-line">
          {item.text.length > 4000 ? `${item.text.slice(0, 4000)}…` : item.text || "No abstract."}
        </p>
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

function AiColumn({
  result,
  criteria,
  labels,
}: {
  result: AdjudicationResult;
  criteria: ReadonlyArray<Criterion>;
  labels: Record<string, string>;
}) {
  return (
    <section className="reveal-enter px-4 pt-3 pb-4" aria-label="AI judge" data-testid="ai-column">
      <h2 className="text-sm font-semibold">The AI judge&rsquo;s reading</h2>
      <p className="mt-0.5 text-xs text-ink-3">
        {result.ai.excludes
          ? "It proposed an exclusion with a verified quote."
          : "It proposed no verified exclusion."}
      </p>
      <ul className="mt-2 grid gap-2">
        {result.ai.criteria.map((c) => (
          <li key={c.criterion_id} className="text-xs">
            <Status term={VERDICTS[c.status]}>
              {criterionLabel(c.criterion_id, criteria, labels)}
            </Status>
            {c.downgraded_from ? (
              <span className="ml-2 text-ink-3">
                said {VERDICTS[c.downgraded_from].label.toLowerCase()}; the quote check did not hold
              </span>
            ) : null}
            {c.quote ? <p className="mt-0.5 text-ink-2">&ldquo;{c.quote}&rdquo;</p> : null}
            <p className="mt-0.5 text-ink-3">{c.rationale}</p>
          </li>
        ))}
      </ul>
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
