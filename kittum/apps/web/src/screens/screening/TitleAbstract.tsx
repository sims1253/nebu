// The title and abstract queue (DESIGN.md, "Reviewer workbench"): one record, the criteria strip,
// and nothing from the AI until the reviewer has decided.
import { useRouteContext } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import type { Criterion, Decision, QueueItem, Reveal, ScopeStatus } from "../../data/schema.ts";
import { run } from "../../data/workbench.ts";
import { codePointIndex } from "../../lib/codepoints.ts";
import { Key, KeyHint } from "../../ui/Key.tsx";
import { useKeys } from "../../ui/keys.ts";
import { Mark, OUTCOMES, Status, VERDICTS } from "../../ui/Status.tsx";
import { useProject } from "../project.tsx";
import { locateQuote, Marked, orderSections } from "./RecordText.tsx";
import { useQueue } from "./useQueue.ts";
import type { SessionStats } from "./useQueue.ts";

export function TitleAbstractQueue() {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me, labels, criteriaFor, screeningStage, scopeLabel, has } = useProject();
  const stage = screeningStage("title_abstract");
  const scopeId = stage?.scope_id ?? null;
  const criteria = useMemo(() => criteriaFor("title_abstract", scopeId), [criteriaFor, scopeId]);
  const caller = useMemo(
    () => ({ projectId: project.id, as: me.actor_id }),
    [project.id, me.actor_id],
  );
  const queue = useQueue(workbench, caller, scopeId);
  const [armed, setArmed] = useState(false);
  const [status, setStatus] = useState<ScopeStatus | null>(null);
  const [poll, setPoll] = useState(0);
  const { current } = queue;
  // The queue ran dry but records remain: the AI judge has not read them yet, or they joined
  // behind the place the queue was read from. Ask again, from the start.
  const waiting = queue.finished && status !== null && status.remaining > 0;
  const { retry } = queue;
  useEffect(() => {
    if (!waiting) return;
    const timer = window.setTimeout(() => {
      retry();
      setPoll((n) => n + 1);
    }, 3000);
    return () => window.clearTimeout(timer);
  }, [waiting, retry, poll]);

  useEffect(() => {
    if (!scopeId) return;
    let live = true;
    void run(workbench.status({ ...caller, scopeId, stage: "title_abstract" })).then(
      (s) => live && setStatus(s),
    );
    return () => {
      live = false;
    };
  }, [workbench, caller, scopeId, queue.stats.decided, current?.item.assignment_id, poll]);

  useKeys((event) => {
    if (event.repeat) return true;
    const key = event.key;
    if (!current) {
      // Nothing on screen (finished, or waiting for the AI): the last decision can still reopen.
      if (key !== "Backspace") return false;
      queue.undo();
      return true;
    }
    if (armed) {
      setArmed(false);
      if (/^[1-9]$/.test(key)) {
        const criterion = criteria[Number(key) - 1];
        if (criterion) void queue.decide("exclude", criterion.id);
        else
          queue.setNotice(`There is no criterion ${key}. Press e, then 1 to ${criteria.length}.`);
        return true;
      }
      if (key === "Escape" || key === "Backspace" || key === "e") return true;
    }
    switch (key) {
      case "i":
        void queue.decide("include");
        return true;
      case "u":
        void queue.decide("unclear");
        return true;
      case "e":
        queue.touch();
        setArmed(true);
        return true;
      case " ":
        void queue.keep();
        return true;
      case "Backspace":
        queue.undo();
        return true;
      default:
        if (/^[1-9]$/.test(key)) {
          queue.setNotice("To exclude, press e first, then the criterion number.");
          return true;
        }
        return false;
    }
  });

  if (!scopeId) {
    return (
      <Empty title="No records to screen">
        {has("reviewer") || has("auditor")
          ? `You screen no ${labels.scope.toLowerCase()} at title and abstract yet. The methods lead opens screening once the protocol is published and the searches are in.`
          : "Title and abstract screening is for the reviewers and the auditor. Choose another person, or another stage."}
      </Empty>
    );
  }

  return (
    <div className="grid h-full grid-rows-[auto_minmax(0,1fr)_auto]">
      <QueueBar scopeLabel={`${labels.scope} ${scopeLabel(scopeId)}`} status={status} />
      <div className="overflow-y-auto bg-sheet" data-testid="record-scroll">
        {current ? (
          <RecordView
            key={current.item.assignment_id}
            item={current.item}
            reveal={current.reveal}
            criteria={criteria}
            labels={labels}
          />
        ) : waiting ? (
          <p className="px-10 py-10 text-ink-2" data-testid="queue-waiting">
            The AI judge is still reading {status?.remaining} of your records. They appear here as
            soon as it has; it reads every record before anyone screens it.
          </p>
        ) : queue.finished && status !== null ? (
          // Finished only once the counts say nothing is left, not before they arrive.
          <Finished stats={queue.stats} />
        ) : queue.error ? null : (
          <p className="px-10 py-10 text-ink-3">Loading the queue.</p>
        )}
      </div>
      <footer className="border-t border-rule-strong bg-sheet-2">
        {current?.reveal ? (
          <RevealBanner
            key={`${current.item.assignment_id}-${current.revealedAt}`}
            reveal={current.reveal}
            decision={current.decision}
            criteria={criteria}
            labels={labels}
            settled={current.settled}
          />
        ) : null}
        <CriteriaStrip
          criteria={criteria}
          labels={labels}
          armed={armed}
          decision={current?.decision ?? null}
          revealed={current?.reveal?.criterion_id ?? null}
        />
        <div className="flex h-10 items-center gap-5 border-t border-rule px-4 text-sm">
          <KeyHint keys={["i"]}>{OUTCOMES.include.label}</KeyHint>
          <span className="text-exclude">
            <KeyHint keys={["e"]} on={armed}>
              {armed
                ? `Exclude: now 1 to ${criteria.length}`
                : `${OUTCOMES.exclude.label} on a criterion`}
            </KeyHint>
          </span>
          <KeyHint keys={["u"]}>{OUTCOMES.unclear.label}</KeyHint>
          <KeyHint keys={["Backspace"]}>Undo</KeyHint>
          <KeyHint keys={[" "]}>
            {current?.reveal && !current.settled ? "Keep and next" : "Next"}
          </KeyHint>
          <span className="ml-auto flex min-w-0 items-center gap-3" aria-live="polite">
            {queue.error ? (
              <span role="alert" className="text-exclude">
                {queue.error}
              </span>
            ) : queue.notice ? (
              <span className="truncate text-ink-2">{queue.notice}</span>
            ) : null}
            {current?.decision ? (
              <DecisionNote label="Your decision" decision={current.decision} criteria={criteria} />
            ) : queue.previous?.decision && !queue.notice ? (
              <DecisionNote
                label="Previous record"
                decision={queue.previous.decision}
                criteria={criteria}
                muted
              />
            ) : null}
          </span>
        </div>
      </footer>
    </div>
  );
}

function QueueBar({ scopeLabel, status }: { scopeLabel: string; status: ScopeStatus | null }) {
  return (
    <div className="flex h-9 items-center gap-4 border-b border-rule bg-paper px-4 text-xs">
      <span className="truncate text-ink-2" title={scopeLabel}>
        {scopeLabel}
      </span>
      <span className="ml-auto whitespace-nowrap text-ink-3">
        {status ? `${status.screened} screened, ${status.remaining} left` : ""}
      </span>
    </div>
  );
}

function RecordView({
  item,
  reveal,
  criteria,
  labels,
}: {
  item: QueueItem;
  reveal: Reveal | null;
  criteria: ReadonlyArray<Criterion>;
  labels: Record<string, string>;
}) {
  const { record } = item;
  const quote = locateQuote(item.title, item.text, reveal?.quote ?? null);
  const at = useMemo(() => codePointIndex(item.text), [item.text]);
  const describe = (id: string) => {
    const index = criteria.findIndex((c) => c.id === id);
    const criterion = criteria[index];
    return criterion
      ? `Protocol term for ${index + 1}, ${labels[criterion.slot] ?? criterion.slot}`
      : "Protocol term";
  };
  const sections = orderSections(record.sections);
  return (
    <article className="mx-auto max-w-[900px] px-10 pt-7 pb-10" aria-label="Record">
      <p className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-2">
        {record.journal ? <span className="italic">{record.journal}</span> : null}
        {record.year ? <span>{record.year}</span> : null}
        <span>{publicationTypes(record.publication_types)}</span>
        {record.registry_ids.map((id) => (
          <span key={id}>{id}</span>
        ))}
        {record.pmid ? <span className="text-ink-3">PMID {record.pmid}</span> : null}
        {record.doi && !record.pmid ? <span className="text-ink-3">DOI {record.doi}</span> : null}
      </p>
      <h1
        className="mt-2 max-w-[68ch] text-xl font-semibold tracking-[-0.01em] text-balance"
        data-testid="record-title"
      >
        <Marked
          text={item.title}
          highlights={record.title_highlights}
          quote={quote.title}
          describe={describe}
        />
      </h1>
      {sections.length === 0 || item.text === "" ? (
        <p className="mt-6 text-md text-ink-3">
          This record has no abstract. Decide on the title, or mark it unclear.
        </p>
      ) : (
        <div className="mt-5 grid grid-cols-[8.5rem_minmax(0,72ch)] gap-x-6 gap-y-3.5">
          {sections.map((section) => (
            <section key={section.start} className="contents">
              <h2
                className={`narrow pt-[3px] text-right text-xs font-semibold ${section.trailing ? "text-ink-3" : "text-ink-2"}`}
              >
                {section.label}
              </h2>
              <p className={`text-md ${section.trailing ? "text-ink-3" : ""}`}>
                <Marked
                  text={item.text}
                  from={at(section.start)}
                  to={at(section.end)}
                  highlights={item.highlights}
                  quote={quote.text}
                  describe={describe}
                />
              </p>
            </section>
          ))}
        </div>
      )}
      <p
        className={`mt-8 flex gap-5 text-2xs text-ink-3 ${item.text ? "ml-[calc(8.5rem+1.5rem)]" : ""}`}
      >
        <span>
          <span className="term">Underlined</span>: protocol term
        </span>
        <span>
          <span className="entity">Dotted</span>: drug, disease, or gene
        </span>
      </p>
    </article>
  );
}

function CriteriaStrip({
  criteria,
  labels,
  armed,
  decision,
  revealed,
}: {
  criteria: ReadonlyArray<Criterion>;
  labels: Record<string, string>;
  armed: boolean;
  decision: Decision | null;
  revealed: string | null;
}) {
  return (
    <ol
      aria-label="Criteria"
      className="grid"
      style={{ gridTemplateColumns: `repeat(${criteria.length}, minmax(0, 1fr))` }}
    >
      {criteria.map((criterion, index) => {
        const chosen =
          decision?.outcome === "exclude" && decision.reason_criterion_id === criterion.id;
        return (
          <li
            key={criterion.id}
            data-revealed={revealed === criterion.id || undefined}
            className={`border-r border-rule px-3 pt-2 pb-2.5 last:border-r-0 ${
              revealed === criterion.id ? "evidence-item" : ""
            } ${armed ? "shadow-[inset_0_2px_0_var(--exclude)]" : ""}`}
          >
            <div className="flex items-center gap-2">
              <span className={armed ? "text-exclude" : ""}>
                <Key k={String(index + 1)} on={armed} />
              </span>
              <span className="text-2xs text-ink-3">
                {labels[criterion.slot] ?? criterion.slot}
              </span>
              {chosen ? <Mark term={OUTCOMES.exclude} title="Your exclusion reason" /> : null}
            </div>
            <p
              className={`narrow mt-1 text-xs ${armed ? "text-ink" : "line-clamp-2 text-ink-2"}`}
              title={`${criterion.text_en}\n${criterion.text_de}`}
            >
              {criterion.text_en}
            </p>
          </li>
        );
      })}
    </ol>
  );
}

function RevealBanner({
  reveal,
  decision,
  criteria,
  labels,
  settled,
}: {
  reveal: Reveal;
  decision: Decision | null;
  criteria: ReadonlyArray<Criterion>;
  labels: Record<string, string>;
  settled: boolean;
}) {
  const index = criteria.findIndex((c) => c.id === reveal.criterion_id);
  const criterion = criteria[index];
  // With `reveal: always` the judge can also agree; say which.
  const excludes = reveal.status === "not_met";
  const agrees = decision !== null && (decision.outcome === "exclude") === excludes;
  return (
    <section
      aria-label="AI judge"
      data-testid="ai-reveal"
      className="reveal-enter evidence-item flex items-baseline gap-3 border-b border-rule px-4 py-2 text-sm"
    >
      <span className="font-semibold whitespace-nowrap">
        {agrees ? "The AI judge agrees" : "The AI judge disagrees"}
      </span>
      <span className="whitespace-nowrap text-ink-2">
        {index + 1} {criterion ? (labels[criterion.slot] ?? criterion.slot) : reveal.criterion_id}:
      </span>
      <Status term={VERDICTS[reveal.status]} />
      <span className="min-w-0 flex-1 truncate text-ink-2" title={reveal.rationale}>
        {reveal.rationale}
      </span>
      {settled ? null : (
        <span className="flex flex-none items-center gap-4">
          <KeyHint keys={[" "]}>Keep yours</KeyHint>
          <KeyHint keys={["i", "e", "u"]}>Revise</KeyHint>
        </span>
      )}
    </section>
  );
}

function DecisionNote({
  label,
  decision,
  criteria,
  muted,
}: {
  label: string;
  decision: Decision;
  criteria: ReadonlyArray<Criterion>;
  muted?: boolean;
}) {
  const term = OUTCOMES[decision.outcome];
  const index = criteria.findIndex((c) => c.id === decision.reason_criterion_id);
  return (
    <span
      className={`flex items-center gap-2 ${muted ? "opacity-75" : ""}`}
      data-testid="decision-note"
    >
      <span className="text-ink-3">{label}</span>
      <Status term={term}>
        {term.past}
        {index >= 0 ? ` on ${index + 1}` : ""}
      </Status>
    </span>
  );
}

function Finished({ stats }: { stats: SessionStats }) {
  return (
    <div className="mx-auto max-w-[640px] px-10 py-16">
      <h1 className="text-xl font-semibold">Queue finished</h1>
      <p className="mt-2 text-md text-ink-2">
        Every record assigned to you at title and abstract has a decision. Your session, shown only
        to you:
      </p>
      <dl className="mt-6 grid grid-cols-[auto_1fr] gap-x-8 gap-y-2 text-md">
        <dt className="text-ink-2">Records decided</dt>
        <dd>{stats.decided}</dd>
        <dt className="text-ink-2">Median time per record</dt>
        <dd>{stats.medianSeconds === null ? "None" : `${Math.round(stats.medianSeconds)} s`}</dd>
        <dt className="text-ink-2">AI disagreements shown</dt>
        <dd>{stats.revealsShown}</dd>
        <dt className="text-ink-2">Decisions you revised after one</dt>
        <dd>{stats.revealsFollowed}</dd>
      </dl>
    </div>
  );
}

function Empty({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="mx-auto max-w-[640px] px-10 py-16">
      <h1 className="text-xl font-semibold">{title}</h1>
      <p className="mt-2 text-md text-ink-2">{children}</p>
    </div>
  );
}

// MEDLINE adds funding and indexing types; the reviewer needs the design types.
const NOISE =
  /^(Journal Article|Research Support|Comparative Study|Multicenter Study|English Abstract)/;
const publicationTypes = (types: ReadonlyArray<string>) =>
  types.filter((t) => !NOISE.test(t)).join(", ") || "Journal article";
