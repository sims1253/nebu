// Audit (DESIGN.md, "The ledger"): the hash-chained ledger as an assessor questions it. Trace a
// PRISMA count to its records and a record to every event that touched it; check the chain and
// see exactly which event fails which check; look at the review as it stood at an earlier event.
// The ledger holds every reviewer's decisions, so the server answers the methods lead only.
import { useRouteContext } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import type {
  AsOf,
  ChainAudit,
  Finding,
  Milestone,
  PrismaFlow,
  RecordTrace,
  RenderedReport,
  TracedEvent,
} from "../../data/schema.ts";
import { run, WorkbenchError } from "../../data/workbench.ts";
import { KeyHint } from "../../ui/Key.tsx";
import { useKeys } from "../../ui/keys.ts";
import { LEAD_ONLY, useProject } from "../project.tsx";

type View = "trace" | "integrity" | "cutoff";

const VIEWS: ReadonlyArray<readonly [View, string, string]> = [
  ["trace", "1", "Trace a number"],
  ["integrity", "2", "Integrity"],
  ["cutoff", "3", "As of an event"],
];

/** The PRISMA counts a lead can open, in flow order, each with the `members` key behind it. */
const BOXES: ReadonlyArray<readonly [string, string, number]> = [
  ["records_identified", "Records identified", 0],
  ["unique_records", "Records after duplicates removed", 1],
  ["records_screened", "Records screened", 1],
  ["records_excluded", "Records excluded", 2],
  ["title_abstract:excluded_with_ai_involvement", "Excluded with the AI involved", 3],
  ["records_awaiting_screening", "Records not yet screened", 2],
  ["records_unresolved", "Records in conflict", 2],
  ["reports_sought", "Reports sought for retrieval", 1],
  ["reports_not_retrieved", "Reports not retrieved", 2],
  ["reports_assessed", "Reports assessed for eligibility", 1],
  ["reports_excluded", "Reports excluded", 2],
  ["full_text:excluded_with_ai_involvement", "Excluded with the AI involved", 3],
  ["reports_awaiting_classification", "Insufficient information", 2],
  ["reports_included", "Reports included", 1],
  ["studies_included", "Studies included", 1],
];

const FLOW_ROWS: ReadonlyArray<readonly [keyof PrismaFlow & string, string]> = [
  ["records_identified", "Records identified"],
  ["duplicates_removed", "Duplicates removed"],
  ["records_screened", "Records screened"],
  ["records_excluded", "Records excluded"],
  ["records_awaiting_screening", "Records not yet screened"],
  ["records_unresolved", "Records in conflict"],
  ["reports_sought", "Reports sought for retrieval"],
  ["reports_not_retrieved", "Reports not retrieved"],
  ["reports_assessed", "Reports assessed for eligibility"],
  ["reports_excluded", "Reports excluded"],
  ["reports_included", "Reports included"],
  ["studies_included", "Studies included"],
];

const CHECKS: Record<Finding["check"], string> = {
  seq: "Sequence",
  payload_hash: "Payload hash",
  prev_hash: "Link to the event before",
  event_hash: "Event hash",
  checkpoint: "Signed checkpoint",
};

const STAGES: Record<string, string> = {
  title_abstract: "Title and abstract",
  full_text: "Full text",
};

/** A hash's first 16 digits in groups of four, so two hashes can be compared at a glance. */
const short = (hash: string, digits = 16) =>
  hash
    .slice(0, digits)
    .replace(/(.{4})/g, "$1 ")
    .trim();

const when = (iso: string) =>
  new Date(iso).toLocaleString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });

/** A server reason as a sentence of its own. */
const sentence = (text: string) => `${text.charAt(0).toUpperCase()}${text.slice(1)}.`;

const seconds = (from: string, to: string) =>
  Math.round((new Date(to).getTime() - new Date(from).getTime()) / 100) / 10;

export function AuditScreen() {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me, seesLeadData, scopeLabel } = useProject();
  const caller = useMemo(() => ({ projectId: project.id, as: me.actor_id }), [project.id, me]);
  const [view, setView] = useState<View>("trace");
  const [head, setHead] = useState<AsOf | null>(null);
  const [audit, setAudit] = useState<ChainAudit | null>(null);
  const [milestones, setMilestones] = useState<ReadonlyArray<Milestone>>([]);
  const [verifying, setVerifying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [trace, setTrace] = useState<RecordTrace | null>(null);
  const [cutoff, setCutoff] = useState<number | null>(null);

  const fail = (e: unknown, what: string) =>
    setError(e instanceof WorkbenchError ? e.message : `${what} failed.`);

  const verify = useCallback(async () => {
    setVerifying(true);
    try {
      setAudit(await run(workbench.auditLedger(caller)));
      setError(null);
    } catch (e) {
      fail(e, "Verifying the ledger");
    } finally {
      setVerifying(false);
    }
  }, [workbench, caller]);

  useEffect(() => {
    if (!seesLeadData || !workbench.live) return;
    let live = true;
    Promise.all([
      run(workbench.asOf({ ...caller, cutoffSeq: null })),
      run(workbench.milestones(caller)),
      run(workbench.auditLedger(caller)),
    ]).then(
      ([now, named, checked]) => {
        if (!live) return;
        setHead(now);
        setMilestones(named);
        setAudit(checked);
      },
      (e: unknown) => live && fail(e, "Loading the ledger"),
    );
    return () => {
      live = false;
    };
  }, [workbench, caller, seesLeadData]);

  const openTrace = useCallback(
    async (ref: string) => {
      try {
        setTrace(await run(workbench.trace({ ...caller, ref })));
        setView("trace");
      } catch (e) {
        fail(e, "Tracing the record");
      }
    },
    [workbench, caller],
  );

  useKeys((event) => {
    const picked = VIEWS.find(([, key]) => key === event.key);
    if (picked) {
      setView(picked[0]);
      return true;
    }
    if (event.key === "v" && seesLeadData && !verifying) {
      setView("integrity");
      void verify();
      return true;
    }
    return false;
  });

  if (!seesLeadData) {
    return (
      <Shell>
        <h1 className="text-xl font-semibold tracking-[-0.01em]">Audit</h1>
        <p className="mt-4 max-w-[60ch] text-ink-2">
          The ledger holds every reviewer&rsquo;s decisions and the AI&rsquo;s readings. {LEAD_ONLY}
        </p>
      </Shell>
    );
  }

  const highlight = new Set(view === "trace" ? (trace?.events.map((e) => e.seq) ?? []) : []);
  const findingAt = new Map<number, Finding>();
  for (const f of audit?.findings ?? []) if (!findingAt.has(f.seq)) findingAt.set(f.seq, f);

  return (
    <div className="flex h-full flex-col bg-paper">
      <header className="flex flex-none items-baseline gap-6 px-8 pt-6">
        <h1 className="text-xl font-semibold tracking-[-0.01em]">Audit</h1>
        <nav aria-label="Audit views" className="flex gap-5 text-sm">
          {VIEWS.map(([id, key, label]) => (
            <button
              key={id}
              type="button"
              onClick={() => setView(id)}
              aria-pressed={view === id}
              className={`rounded-[3px] px-1 ${view === id ? "font-medium text-petrol" : ""}`}
            >
              <KeyHint keys={[key]} on={view === id}>
                {label}
              </KeyHint>
            </button>
          ))}
        </nav>
        <span className="ml-auto text-sm">
          <KeyHint keys={["v"]}>Verify the chain again</KeyHint>
        </span>
      </header>
      <ChainStrip
        head={audit?.head_seq ?? head?.head_seq ?? 0}
        audit={audit}
        highlight={highlight}
        cutoff={view === "cutoff" ? cutoff : null}
        onPick={view === "cutoff" ? setCutoff : null}
        verifying={verifying}
      />
      {error ? (
        <p role="alert" className="flex-none px-8 pt-2 text-sm text-exclude">
          {error}
        </p>
      ) : null}
      <div className="min-h-0 flex-1">
        {view === "trace" ? (
          <TraceView
            head={head}
            trace={trace}
            findings={findingAt}
            onOpen={openTrace}
            scopeLabel={scopeLabel}
            caller={caller}
          />
        ) : view === "integrity" ? (
          <IntegrityView
            audit={audit}
            verifying={verifying}
            onTrace={openTrace}
            onSigned={() => void verify()}
            caller={caller}
          />
        ) : (
          <CutoffView
            head={head}
            milestones={milestones}
            cutoff={cutoff}
            onCutoff={setCutoff}
            scopeLabel={scopeLabel}
            caller={caller}
          />
        )}
      </div>
    </div>
  );
}

function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="h-full overflow-y-auto bg-paper">
      <div className="mx-auto max-w-[980px] px-10 pt-10 pb-16">{children}</div>
    </div>
  );
}

/**
 * The whole chain on one line: a tick per event, a notch per signed checkpoint, the traced
 * record's events in petrol, a failing event in red. In the cutoff view a click picks the event,
 * and what came after it fades.
 */
function ChainStrip({
  head,
  audit,
  highlight,
  cutoff,
  onPick,
  verifying,
}: {
  head: number;
  audit: ChainAudit | null;
  highlight: ReadonlySet<number>;
  cutoff: number | null;
  onPick: ((seq: number) => void) | null;
  verifying: boolean;
}) {
  const [hover, setHover] = useState<number | null>(null);
  if (head === 0) return <div className="h-[76px] flex-none" />;
  const x = (seq: number) => `${((seq - 0.5) / head) * 100}%`;
  const bad = new Set(
    (audit?.findings ?? []).filter((f) => f.check !== "checkpoint").map((f) => f.seq),
  );
  const ticks: string[] = [];
  const lit: string[] = [];
  for (let seq = 1; seq <= head; seq++) {
    const at = seq - 0.5;
    if (highlight.has(seq)) lit.push(`M${at} 2V26`);
    else if (!bad.has(seq)) ticks.push(`M${at} 9V19`);
  }
  const seqAt = (clientX: number, rect: DOMRect) =>
    Math.min(head, Math.max(1, Math.ceil(((clientX - rect.left) / rect.width) * head)));
  const status = verifying
    ? "Checking every event."
    : !audit
      ? ""
      : audit.ok
        ? `The chain holds through event ${audit.verified_through_seq} of ${audit.head_seq}.`
        : `The chain breaks at event ${audit.verified_through_seq + 1}; it holds through ${audit.verified_through_seq}.`;
  return (
    <section aria-label="The ledger chain" className="flex-none px-8 pt-4 pb-1">
      <div
        className={`relative h-7 ${onPick ? "cursor-crosshair" : ""}`}
        data-testid="chain-strip"
        onMouseMove={(e) => setHover(seqAt(e.clientX, e.currentTarget.getBoundingClientRect()))}
        onMouseLeave={() => setHover(null)}
        onClick={(e) => onPick?.(seqAt(e.clientX, e.currentTarget.getBoundingClientRect()))}
      >
        <svg
          className="absolute inset-0 h-full w-full"
          viewBox={`0 0 ${head} 28`}
          preserveAspectRatio="none"
          aria-hidden="true"
        >
          <path d={`M0 14H${head}`} stroke="var(--rule-strong)" vectorEffect="non-scaling-stroke" />
          <path
            d={ticks.join("")}
            stroke="var(--rule-strong)"
            strokeWidth="1"
            vectorEffect="non-scaling-stroke"
          />
          <path
            d={lit.join("")}
            stroke="var(--petrol)"
            strokeWidth="2"
            vectorEffect="non-scaling-stroke"
          />
          {cutoff !== null ? (
            <rect
              x={cutoff}
              y="0"
              width={head - cutoff}
              height="28"
              fill="var(--paper)"
              opacity="0.72"
            />
          ) : null}
        </svg>
        {(audit?.checkpoints ?? []).map((c) => (
          <span
            key={c.checkpoint.id}
            title={`Checkpoint through event ${c.checkpoint.seq}, signed by key ${short(c.checkpoint.key_id)}`}
            className={`absolute -top-1 h-9 w-[3px] -translate-x-1/2 rounded-[1px] ${
              c.signature === "valid" && c.head_matches !== false ? "bg-petrol" : "bg-exclude"
            }`}
            style={{ left: x(c.checkpoint.seq) }}
          />
        ))}
        {[...bad].map((seq) => (
          <span
            key={seq}
            data-testid="strip-break"
            title={`Event ${seq} fails a check`}
            className="absolute -top-1 h-9 w-[3px] -translate-x-1/2 bg-exclude"
            style={{ left: x(seq) }}
          />
        ))}
        {cutoff !== null ? (
          <span
            className="absolute -top-2 h-11 w-[2px] -translate-x-1/2 bg-ink"
            style={{ left: x(cutoff) }}
          />
        ) : null}
      </div>
      <div className="mt-1.5 flex items-baseline gap-4 text-xs text-ink-3 tabular-nums">
        <span>Event 1</span>
        <span
          className={`flex-1 text-center ${audit && !audit.ok ? "font-medium text-exclude" : "text-ink-2"}`}
          data-testid="chain-status"
          aria-live="polite"
        >
          {hover !== null ? `Event ${hover}` : status}
        </span>
        <span>Event {head}</span>
      </div>
    </section>
  );
}

// Trace a number.

function TraceView({
  head,
  trace,
  findings,
  onOpen,
  scopeLabel,
  caller,
}: {
  head: AsOf | null;
  trace: RecordTrace | null;
  findings: ReadonlyMap<number, Finding>;
  onOpen: (ref: string) => Promise<void>;
  scopeLabel: (scope: string) => string;
  caller: { projectId: string; as: string };
}) {
  const flows = head?.flows.filter((f) => f.records_identified > 0) ?? [];
  const [scope, setScope] = useState<string | null>(null);
  const flow = flows.find((f) => f.scope_id === scope) ?? flows[0] ?? null;
  const [box, setBox] = useState("reports_included");
  const [index, setIndex] = useState(0);
  const members = flow?.members[box] ?? [];
  const boxes = flow ? BOXES.filter(([key]) => key in flow.members) : [];

  const pick = (i: number) => {
    const ref = members[i];
    if (ref === undefined) return;
    setIndex(i);
    const target = head?.items[ref]?.trace_ref;
    if (target) void onOpen(target);
  };

  useKeys((event) => {
    if (event.key === "j" || event.key === "k") {
      const next = Math.min(members.length - 1, Math.max(0, index + (event.key === "j" ? 1 : -1)));
      pick(next);
      return true;
    }
    if (event.key === "]" || event.key === "[") {
      const at = boxes.findIndex(([key]) => key === box);
      const next =
        boxes[Math.min(boxes.length - 1, Math.max(0, at + (event.key === "]" ? 1 : -1)))];
      if (next) {
        setBox(next[0]);
        setIndex(0);
      }
      return true;
    }
    return false;
  });

  if (!head) return <p className="px-8 pt-6 text-ink-2">Loading the review.</p>;
  if (!flow) return <p className="px-8 pt-6 text-ink-2">No searches are recorded yet.</p>;
  return (
    <div className="grid h-full grid-cols-[17.5rem_19rem_minmax(0,1fr)] border-t border-rule">
      <section aria-label="PRISMA flow" className="overflow-y-auto border-r border-rule px-5 py-4">
        {flows.length > 1 ? (
          <div className="mb-3 flex flex-wrap gap-2 text-sm">
            {flows.map((f) => (
              <button
                key={f.scope_id}
                type="button"
                aria-pressed={f.scope_id === flow.scope_id}
                onClick={() => setScope(f.scope_id)}
                className={`rounded-[3px] px-1.5 py-0.5 ${f.scope_id === flow.scope_id ? "bg-petrol-wash text-petrol" : "text-ink-2"}`}
              >
                {scopeLabel(f.scope_id)}
              </button>
            ))}
          </div>
        ) : null}
        <p className="text-xs text-ink-3">
          Every count is a list of records. Open one to see them.{" "}
          <KeyHint keys={["[", "]"]}>Count</KeyHint>
        </p>
        <ol className="mt-3">
          {boxes.map(([key, label, depth]) => {
            const n = flow.members[key]?.length ?? 0;
            const active = key === box;
            return (
              <li key={key}>
                <button
                  type="button"
                  data-testid={`box-${key}`}
                  aria-pressed={active}
                  onClick={() => {
                    setBox(key);
                    setIndex(0);
                  }}
                  className={`grid w-full grid-cols-[3rem_minmax(0,1fr)] items-baseline gap-x-3 border-l-2 py-1.5 pr-2 text-left ${
                    active ? "border-petrol bg-sheet" : "border-transparent hover:bg-sheet"
                  }`}
                  style={{ paddingLeft: `${0.5 + depth * 0.6}rem` }}
                >
                  <span
                    className={`text-right text-lg font-semibold tabular-nums ${n === 0 ? "text-ink-3" : ""}`}
                  >
                    {n}
                  </span>
                  <span className={`text-sm ${active ? "text-ink" : "text-ink-2"}`}>{label}</span>
                </button>
              </li>
            );
          })}
        </ol>
      </section>
      <section
        aria-label="Records behind the count"
        className="overflow-y-auto border-r border-rule py-4"
      >
        <p className="px-4 text-xs text-ink-3">
          {members.length} {members.length === 1 ? "record" : "records"}.{" "}
          <KeyHint keys={["j", "k"]}>Next, previous</KeyHint>
        </p>
        <ol className="mt-2">
          {members.map((ref, i) => {
            const item = head.items[ref];
            const open =
              trace !== null && (trace.ref === item?.trace_ref || trace.report_id === ref);
            return (
              <li key={ref}>
                <button
                  type="button"
                  data-testid="member"
                  disabled={!item?.trace_ref}
                  onClick={() => pick(i)}
                  className={`block w-full border-l-2 px-4 py-2 text-left text-sm ${
                    open
                      ? "border-petrol bg-sheet text-ink"
                      : "border-transparent text-ink-2 hover:bg-sheet"
                  }`}
                >
                  <span className="line-clamp-2">{item?.title ?? ref}</span>
                  {!item?.trace_ref ? (
                    <span className="text-xs text-ink-3">
                      A sentinel; it has no history of its own
                    </span>
                  ) : null}
                </button>
              </li>
            );
          })}
        </ol>
      </section>
      <section aria-label="Record history" className="overflow-y-auto px-8 py-5">
        {trace ? (
          <Timeline trace={trace} findings={findings} caller={caller} />
        ) : (
          <p className="max-w-[52ch] text-ink-2">
            Choose a record to see every ledger event that touched it: the searches that found it,
            its duplicates, each person&rsquo;s blind decision, when the AI&rsquo;s reading was
            shown, the full text, and the values taken from it.
          </p>
        )}
      </section>
    </div>
  );
}

function Timeline({
  trace,
  findings,
  caller,
}: {
  trace: RecordTrace;
  findings: ReadonlyMap<number, Finding>;
  caller: { projectId: string; as: string };
}) {
  const [open, setOpen] = useState<ReadonlySet<number>>(new Set());
  const toggle = (seq: number) =>
    setOpen((now) => {
      const next = new Set(now);
      if (next.has(seq)) next.delete(seq);
      else next.add(seq);
      return next;
    });
  return (
    <article data-testid="timeline">
      <h2 className="max-w-[70ch] text-lg font-semibold" data-testid="trace-title">
        {trace.title}
      </h2>
      <p className="mt-1 text-xs text-ink-3 tabular-nums">
        Report {trace.report_id}
        {trace.study_id ? `, study ${trace.study_id}` : ""}
      </p>
      <section className="mt-4" aria-label="Found by">
        <h3 className="text-sm font-semibold text-ink-2">
          Found {trace.occurrences.length === 1 ? "once" : `${trace.occurrences.length} times`}
        </h3>
        <ul className="mt-1.5 grid gap-1 text-sm">
          {trace.occurrences.map((o) => (
            <li key={o.occurrence_id} className="flex flex-wrap items-baseline gap-x-3">
              <span>{o.source}</span>
              <span className="text-ink-3">record {o.provider_record_id}</span>
              {o.search_seq !== null ? (
                <span className="text-ink-3">search at event {o.search_seq}</span>
              ) : null}
              <ArtifactLink caller={caller} sha256={o.raw_hash} label="Raw response" />
            </li>
          ))}
        </ul>
      </section>
      {trace.reveals.map((r) => (
        <p
          key={r.decision_seq}
          className="mt-4 max-w-[72ch] border-l-2 border-petrol pl-3 text-sm"
          data-testid="reveal"
        >
          {r.reviewer} decided blind at event {r.decision_seq} ({STAGES[r.stage] ?? r.stage}). The
          AI&rsquo;s reading was shown only afterwards
          {r.shown_at && r.decided_at
            ? `, ${seconds(r.decided_at, r.shown_at)} s after the decision`
            : ""}
          {r.final_seq !== null
            ? `; ${r.kept ? "they kept their decision" : "they revised it"} at event ${r.final_seq}.`
            : "; the final decision is still open."}
        </p>
      ))}
      <ol className="mt-6" aria-label="Events">
        {trace.events.map((e, i) => {
          const before = trace.events[i - 1];
          const gap = before ? e.seq - before.seq - 1 : 0;
          return (
            <li key={e.seq}>
              {gap > 0 ? (
                <div className="grid grid-cols-[4rem_1.25rem_minmax(0,1fr)] text-xs text-ink-3">
                  <span />
                  <span className="mx-auto h-5 w-px border-l border-dashed border-rule-strong" />
                  <span className="self-center pl-3">
                    {gap} other {gap === 1 ? "event" : "events"}
                  </span>
                </div>
              ) : null}
              <EventRow
                event={e}
                finding={findings.get(e.seq) ?? null}
                expanded={open.has(e.seq)}
                onToggle={() => toggle(e.seq)}
                caller={caller}
              />
            </li>
          );
        })}
      </ol>
    </article>
  );
}

const KIND_LABEL: Record<TracedEvent["kind"], string> = {
  search: "Search",
  identity: "Identity",
  stage: "Stage",
  ai: "AI judge",
  decision: "Decision",
  conflict: "Conflict",
  adjudication: "Adjudication",
  full_text: "Full text",
  extraction: "Extraction",
  report: "Report",
  ledger: "Ledger",
  project: "Project",
};

function EventRow({
  event: e,
  finding,
  expanded,
  onToggle,
  caller,
}: {
  event: TracedEvent;
  finding: Finding | null;
  expanded: boolean;
  onToggle: () => void;
  caller: { projectId: string; as: string };
}) {
  const broken = finding !== null;
  return (
    <div
      className="grid grid-cols-[4rem_1.25rem_minmax(0,1fr)]"
      data-testid="trace-event"
      data-seq={e.seq}
      data-kind={e.kind}
      data-broken={broken || undefined}
    >
      <span
        className={`pt-2.5 pr-3 text-right text-md font-semibold tabular-nums ${broken ? "text-exclude" : ""}`}
      >
        {e.seq}
      </span>
      <span className="relative flex justify-center" aria-hidden="true">
        <span className={`absolute inset-y-0 w-0.5 ${broken ? "bg-exclude" : "bg-petrol-wash"}`} />
        <span
          className={`relative mt-3.5 size-2.5 rounded-full border-2 ${
            broken ? "border-exclude bg-exclude-wash" : "border-petrol bg-sheet"
          }`}
        />
      </span>
      <div
        className={`mb-1 ml-2 rounded-[3px] px-3 py-2 ${broken ? "bg-exclude-wash" : "hover:bg-sheet"}`}
      >
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={expanded}
          className="block w-full text-left"
        >
          <span className="flex items-baseline gap-3">
            <span className="w-[5.5rem] flex-none text-xs text-ink-3">
              {KIND_LABEL[e.kind]}
              {e.stage ? <span className="block">{STAGES[e.stage] ?? e.stage}</span> : null}
            </span>
            <span className="min-w-0">
              <span className="block text-md leading-snug">{e.summary}</span>
              <span className="mt-0.5 flex flex-wrap gap-x-4 text-xs text-ink-2">
                <span>{e.actor}</span>
                <span className="tabular-nums">{when(e.recorded_at)}</span>
                <span className="text-ink-3">{e.type}</span>
              </span>
            </span>
          </span>
        </button>
        {broken ? (
          <p
            className="mt-1.5 ml-[6.25rem] text-sm font-medium text-exclude"
            data-testid="event-finding"
          >
            Fails its {CHECKS[finding.check].toLowerCase()} check: {finding.reason}.
          </p>
        ) : null}
        <dl className="narrow mt-1.5 ml-[6.25rem] grid grid-cols-[4.5rem_minmax(0,1fr)] gap-x-3 text-xs text-ink-3 tabular-nums">
          <dt>Links to</dt>
          <dd>{short(e.prev_hash)}</dd>
          <dt>Hash</dt>
          <dd className="text-ink-2">{short(e.event_hash)}</dd>
        </dl>
        {expanded ? (
          <div className="mt-2 ml-[6.25rem] grid gap-2 text-sm">
            {e.facts.length > 0 ? (
              <dl className="grid grid-cols-[minmax(7rem,auto)_minmax(0,1fr)] gap-x-4 gap-y-0.5">
                {e.facts.map((f) => (
                  <div key={f.label} className="contents">
                    <dt className="text-ink-2">{f.label}</dt>
                    <dd className="break-words">{f.value}</dd>
                  </div>
                ))}
              </dl>
            ) : null}
            <dl className="narrow grid grid-cols-[7rem_minmax(0,1fr)] gap-x-4 text-xs tabular-nums">
              <dt className="text-ink-2">Payload hash</dt>
              <dd className="break-all">{e.payload_hash}</dd>
              <dt className="text-ink-2">Event hash</dt>
              <dd className="break-all">{e.event_hash}</dd>
              <dt className="text-ink-2">Previous hash</dt>
              <dd className="break-all">{e.prev_hash}</dd>
              <dt className="text-ink-2">Event ID</dt>
              <dd className="break-all">{e.event_id}</dd>
            </dl>
            <details>
              <summary className="cursor-default text-xs text-ink-2">Payload as recorded</summary>
              <pre className="mt-1 max-h-72 overflow-auto bg-sheet-2 p-2 text-xs whitespace-pre-wrap">
                {JSON.stringify(e.payload, null, 2)}
              </pre>
            </details>
          </div>
        ) : null}
        {e.artifacts.length > 0 ? (
          <span className="mt-1 ml-[6.25rem] flex flex-wrap gap-x-4 text-xs">
            {e.artifacts.map((a) => (
              <ArtifactLink
                key={`${a.sha256}-${a.label}`}
                caller={caller}
                sha256={a.sha256}
                label={a.label}
                stored={a.stored}
              />
            ))}
          </span>
        ) : null}
      </div>
    </div>
  );
}

/** Opens an artifact the ledger names, fetched as the lead (the request needs the header). */
function ArtifactLink({
  caller,
  sha256,
  label,
  stored = true,
}: {
  caller: { projectId: string; as: string };
  sha256: string;
  label: string;
  stored?: boolean;
}) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const [failed, setFailed] = useState(false);
  if (!stored) {
    return (
      <span className="text-ink-3" title={sha256}>
        {label} {short(sha256, 8)}, not in this store
      </span>
    );
  }
  const openIt = async () => {
    const tab = window.open("", "_blank");
    try {
      const bytes = await run(workbench.auditArtifact({ ...caller, sha256 }));
      const pdf = bytes[0] === 0x25 && bytes[1] === 0x50;
      const url = URL.createObjectURL(
        new Blob([bytes as BlobPart], {
          type: pdf ? "application/pdf" : "text/plain;charset=utf-8",
        }),
      );
      if (tab) tab.location.href = url;
    } catch {
      tab?.close();
      setFailed(true);
    }
  };
  return (
    <button
      type="button"
      onClick={() => void openIt()}
      title={sha256}
      className="text-petrol underline decoration-petrol/40 underline-offset-2 hover:decoration-petrol"
    >
      {label} {short(sha256, 8)}
      {failed ? <span className="text-exclude"> could not be opened</span> : null}
    </button>
  );
}

// Integrity.

function IntegrityView({
  audit,
  verifying,
  onTrace,
  onSigned,
  caller,
}: {
  audit: ChainAudit | null;
  verifying: boolean;
  onTrace: (ref: string) => Promise<void>;
  onSigned: () => void;
  caller: { projectId: string; as: string };
}) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const [notice, setNotice] = useState<string | null>(null);
  const [shown, setShown] = useState<TracedEvent | null>(null);

  const sign = async () => {
    try {
      const signed = await run(workbench.signCheckpoint(caller));
      setNotice(`Signed a checkpoint through event ${signed.checkpoint.seq}.`);
      onSigned();
    } catch (e) {
      setNotice(e instanceof WorkbenchError ? e.message : "Signing failed.");
    }
  };
  const show = async (seq: number) => {
    try {
      const page = await run(workbench.ledgerEvents({ ...caller, afterSeq: seq - 1, limit: 1 }));
      setShown(page.events[0]?.seq === seq ? page.events[0] : null);
    } catch (e) {
      setNotice(e instanceof WorkbenchError ? e.message : "The event could not be read.");
    }
  };

  useKeys((event) => {
    if (event.key === "c") {
      void sign();
      return true;
    }
    return false;
  });

  if (!audit)
    return <p className="px-8 pt-6 text-ink-2">{verifying ? "Checking every event." : ""}</p>;
  const shownRef = shown
    ? ((shown.payload["item_ref"] ?? shown.payload["report_id"]) as string | undefined)
    : undefined;
  return (
    <div className="h-full overflow-y-auto border-t border-rule">
      <div className="mx-auto max-w-[1080px] px-8 pt-6 pb-14">
        <div className="flex items-baseline gap-6">
          <h2
            className={`text-xl font-semibold tracking-[-0.01em] ${audit.ok ? "" : "text-exclude"}`}
            data-testid="integrity-verdict"
          >
            {audit.ok
              ? `Every one of ${audit.events} events matches its hashes and links.`
              : `The chain breaks at event ${audit.verified_through_seq + 1}.`}
          </h2>
          <span className="ml-auto text-sm">
            <KeyHint keys={["c"]}>Sign a checkpoint at the head</KeyHint>
          </span>
        </div>
        <p className="mt-2 max-w-[78ch] text-sm text-ink-2">
          Each event&rsquo;s payload is hashed again, then the event with that hash, then checked
          against the event before it. A hash chain alone cannot tell a chain rewritten from an
          event onwards; the signed checkpoints can, because their key is kept away from the
          database.
          {verifying ? " Checking again now." : ""}
        </p>
        {notice ? (
          <p className="mt-2 text-sm" aria-live="polite">
            {notice}
          </p>
        ) : null}

        {audit.findings.length > 0 ? (
          <section className="mt-6" aria-label="Failed checks">
            <h3 className="text-sm font-semibold text-ink-2">
              {audit.findings.length} failed {audit.findings.length === 1 ? "check" : "checks"}
            </h3>
            <ol className="mt-2 border-t border-rule">
              {audit.findings.map((f) => (
                <li
                  key={`${f.seq}-${f.check}`}
                  className="grid grid-cols-[5rem_minmax(0,1fr)] gap-x-5 border-b border-rule py-3"
                  data-testid="finding"
                  data-seq={f.seq}
                  data-check={f.check}
                >
                  <span className="text-right text-[1.6rem] leading-none font-semibold text-exclude tabular-nums">
                    {f.seq}
                  </span>
                  <div>
                    <p className="text-md">
                      <span className="font-semibold">{CHECKS[f.check]}</span>
                      {f.type ? <span className="text-ink-2">, {f.type}</span> : null}
                    </p>
                    <p className="mt-0.5 text-sm text-ink-2">{sentence(f.reason)}</p>
                    {f.stored || f.expected ? (
                      <dl className="narrow mt-2 grid grid-cols-[8rem_minmax(0,1fr)] gap-x-4 text-xs tabular-nums">
                        <dt className="text-ink-2">Stored</dt>
                        <dd className="break-all">{f.stored ?? "Nothing"}</dd>
                        <dt className="text-ink-2">
                          {f.check === "checkpoint" ? "Signed" : "Recomputed"}
                        </dt>
                        <dd className="break-all">{f.expected ?? "Nothing"}</dd>
                      </dl>
                    ) : null}
                    {f.check !== "checkpoint" && f.check !== "seq" ? (
                      <button
                        type="button"
                        onClick={() => void show(f.seq)}
                        className="mt-2 text-sm text-petrol underline decoration-petrol/40 underline-offset-2"
                      >
                        Show event {f.seq} as stored
                      </button>
                    ) : null}
                  </div>
                </li>
              ))}
            </ol>
          </section>
        ) : null}

        {shown ? (
          <section
            className="mt-6 border-l-2 border-exclude pl-4"
            aria-label={`Event ${shown.seq}`}
            data-testid="shown-event"
          >
            <h3 className="text-md font-semibold">
              Event {shown.seq}: {shown.summary}
            </h3>
            <p className="text-xs text-ink-2">
              {shown.actor}, {when(shown.recorded_at)}
            </p>
            {shownRef ? (
              <button
                type="button"
                onClick={() => void onTrace(shownRef)}
                className="mt-1 text-sm text-petrol underline decoration-petrol/40 underline-offset-2"
              >
                Trace the record it concerns
              </button>
            ) : null}
            <pre className="mt-2 max-h-80 overflow-auto bg-sheet p-2 text-xs whitespace-pre-wrap">
              {JSON.stringify(shown.payload, null, 2)}
            </pre>
          </section>
        ) : null}

        <section className="mt-8" aria-label="Checkpoints">
          <h3 className="text-sm font-semibold text-ink-2">Signed checkpoints</h3>
          {audit.checkpoints.length === 0 ? (
            <p className="mt-2 max-w-[70ch] text-sm text-ink-2">
              No checkpoint is signed yet, so a chain rewritten from some event onwards would still
              verify. Sign one now, or seal a bundle on Reports, which signs one.
            </p>
          ) : (
            <table className="narrow mt-2 w-full text-sm tabular-nums" data-testid="checkpoints">
              <thead className="text-left text-xs text-ink-3">
                <tr className="border-b border-rule">
                  <th className="py-1 font-normal">Through event</th>
                  <th className="py-1 font-normal">Signed</th>
                  <th className="py-1 font-normal">Key</th>
                  <th className="py-1 font-normal">Signature</th>
                  <th className="py-1 font-normal">Chain at that event</th>
                </tr>
              </thead>
              <tbody>
                {audit.checkpoints.map((c) => (
                  <tr key={c.checkpoint.id} className="border-b border-rule">
                    <td className="py-1.5 font-semibold">{c.checkpoint.seq}</td>
                    <td>{when(c.checkpoint.created_at)}</td>
                    <td title={c.checkpoint.key_id}>{short(c.checkpoint.key_id)}</td>
                    <td
                      className={
                        c.signature === "valid"
                          ? "text-include"
                          : c.signature === "invalid"
                            ? "text-exclude"
                            : "text-ink-2"
                      }
                    >
                      {
                        {
                          valid: "Valid",
                          invalid: "Does not verify",
                          unknown_key: "Another key",
                          unchecked: "No key to check",
                        }[c.signature]
                      }
                    </td>
                    <td className={c.head_matches === false ? "text-exclude" : ""}>
                      {c.head_matches === null
                        ? "The event is gone"
                        : c.head_matches
                          ? `Matches ${short(c.checkpoint.head_hash, 8)}`
                          : "Differs from what was signed"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {audit.trusted_key_id ? (
            <p className="mt-2 text-xs text-ink-3">
              Checked against the server&rsquo;s checkpoint key {short(audit.trusted_key_id)}.
            </p>
          ) : (
            <p className="mt-2 text-xs text-ink-3">
              The server has no checkpoint key, so signatures cannot be checked here.
            </p>
          )}
        </section>
      </div>
    </div>
  );
}

// The review as of an event.

function CutoffView({
  head,
  milestones,
  cutoff,
  onCutoff,
  scopeLabel,
  caller,
}: {
  head: AsOf | null;
  milestones: ReadonlyArray<Milestone>;
  cutoff: number | null;
  onCutoff: (seq: number) => void;
  scopeLabel: (scope: string) => string;
  caller: { projectId: string; as: string };
}) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const [then, setThen] = useState<AsOf | null>(null);
  const [cursor, setCursor] = useState(0);
  const [typed, setTyped] = useState("");
  const [kept, setKept] = useState<{
    seq: number;
    reports: RenderedReport[];
    svg: string | null;
  } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    if (cutoff === null) return;
    let live = true;
    run(workbench.asOf({ ...caller, cutoffSeq: cutoff })).then(
      (a) => {
        if (!live) return;
        setThen(a);
        setNotice(null);
      },
      (e: unknown) =>
        live &&
        setNotice(e instanceof WorkbenchError ? e.message : "The review could not be read."),
    );
    return () => {
      live = false;
    };
  }, [workbench, caller, cutoff]);

  useEffect(
    () => () => {
      if (kept?.svg) URL.revokeObjectURL(kept.svg);
    },
    [kept],
  );

  const keep = async () => {
    if (!then) return;
    setNotice(`Rendering at event ${then.cutoff_seq}.`);
    try {
      const reports = await Promise.all(
        ["prisma", "ai_disclosure"].map((target) =>
          run(workbench.renderReport({ ...caller, target, cutoffSeq: then.cutoff_seq })),
        ),
      );
      const diagram = reports[0]?.artifact.files.find(
        (f) =>
          f.media_type === "image/svg+xml" &&
          f.path.includes(then.flows.find((x) => x.records_identified > 0)?.scope_id ?? ""),
      );
      const svg = diagram
        ? URL.createObjectURL(
            new Blob(
              [
                (await run(
                  workbench.reportFile({ ...caller, sha256: diagram.sha256 }),
                )) as BlobPart,
              ],
              {
                type: "image/svg+xml",
              },
            ),
          )
        : null;
      setKept({ seq: then.cutoff_seq, reports, svg });
      setNotice(null);
    } catch (e) {
      setNotice(e instanceof WorkbenchError ? e.message : "Rendering failed.");
    }
  };

  useKeys((event) => {
    if (event.key === "j" || event.key === "k") {
      setCursor((c) =>
        Math.min(milestones.length - 1, Math.max(0, c + (event.key === "j" ? 1 : -1))),
      );
      return true;
    }
    if (event.key === "Enter" && milestones[cursor]) {
      onCutoff(milestones[cursor].seq);
      return true;
    }
    if (event.key === "p" && then) {
      void keep();
      return true;
    }
    return false;
  });

  const named = new Map(milestones.map((m) => [m.seq, m.label]));
  return (
    <div className="grid h-full grid-cols-[21rem_minmax(0,1fr)] border-t border-rule">
      <section
        aria-label="Pick an event"
        className="overflow-y-auto border-r border-rule px-5 py-4"
      >
        <form
          className="flex items-center gap-2 text-sm"
          onSubmit={(e) => {
            e.preventDefault();
            const n = Number(typed);
            if (Number.isInteger(n) && n >= 0) onCutoff(n);
          }}
        >
          <label htmlFor="cutoff-seq" className="text-ink-2">
            Event
          </label>
          <input
            id="cutoff-seq"
            inputMode="numeric"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            className="w-24 rounded-[3px] border border-rule-strong bg-sheet px-2 py-1 tabular-nums"
          />
          <button type="submit" className="rounded-[3px] bg-petrol px-2.5 py-1 text-petrol-ink">
            Look back
          </button>
        </form>
        <p className="mt-2 text-xs text-ink-3">
          Or click the chain, or pick a named event. <KeyHint keys={["j", "k"]}>Move</KeyHint>{" "}
          <KeyHint keys={["Enter"]}>Pick</KeyHint>
        </p>
        <ol className="mt-3" aria-label="Named events">
          {milestones.map((m, i) => (
            <li key={`${m.seq}-${m.label}`}>
              <button
                type="button"
                data-testid="milestone"
                aria-pressed={cutoff === m.seq}
                onClick={() => {
                  setCursor(i);
                  onCutoff(m.seq);
                }}
                className={`grid w-full grid-cols-[3.25rem_minmax(0,1fr)] gap-x-3 border-l-2 py-1.5 pr-2 text-left text-sm ${
                  cutoff === m.seq
                    ? "border-petrol bg-sheet"
                    : i === cursor
                      ? "border-rule-strong bg-sheet"
                      : "border-transparent hover:bg-sheet"
                }`}
              >
                <span className="text-right font-semibold tabular-nums">{m.seq}</span>
                <span className={m.kind === "stage_closed" ? "font-medium" : "text-ink-2"}>
                  {m.label}
                </span>
              </button>
            </li>
          ))}
        </ol>
      </section>
      <section aria-label="The review as of the event" className="overflow-y-auto px-8 py-5">
        {notice ? (
          <p className="mb-3 text-sm" role="status">
            {notice}
          </p>
        ) : null}
        {!then || !head ? (
          <p className="max-w-[56ch] text-ink-2">
            Pick an event to see the PRISMA flow and the AI disclosure exactly as the ledger stood
            then, beside how they stand now.
          </p>
        ) : (
          <AsOfReport then={then} now={head} named={named} scopeLabel={scopeLabel} />
        )}
        {then ? (
          <div className="mt-6 border-t border-rule pt-4">
            <KeyHint keys={["p"]}>
              Render and keep the PRISMA flow and the AI disclosure at event {then.cutoff_seq}
            </KeyHint>
            {kept ? (
              <div className="mt-3" data-testid="kept-report">
                <p className="text-sm">
                  Rendered at event {kept.seq} and recorded in the ledger:{" "}
                  {kept.reports.map((r) => (
                    <span key={r.sha256} className="mr-3 text-ink-2 tabular-nums">
                      {r.artifact.target} {short(r.sha256, 8)}
                    </span>
                  ))}
                </p>
                {kept.svg ? (
                  <img
                    src={kept.svg}
                    alt={`PRISMA 2020 flow diagram at event ${kept.seq}`}
                    className="mt-3 max-w-[760px] bg-sheet"
                  />
                ) : null}
              </div>
            ) : null}
          </div>
        ) : null}
      </section>
    </div>
  );
}

function AsOfReport({
  then,
  now,
  named,
  scopeLabel,
}: {
  then: AsOf;
  now: AsOf;
  named: ReadonlyMap<number, string>;
  scopeLabel: (scope: string) => string;
}) {
  const flows = then.flows.filter(
    (f) =>
      f.records_identified > 0 ||
      now.flows.some((n) => n.scope_id === f.scope_id && n.records_identified > 0),
  );
  return (
    <article data-testid="as-of" data-cutoff={then.cutoff_seq}>
      <h2 className="text-lg font-semibold">
        As of event {then.cutoff_seq}
        {named.get(then.cutoff_seq) ? `, ${named.get(then.cutoff_seq)}` : ""}
      </h2>
      <p className="text-sm text-ink-2">
        {then.cutoff_type
          ? `${then.cutoff_type}, recorded ${when(then.cutoff_recorded_at ?? "")}. `
          : ""}
        {then.head_seq - then.cutoff_seq} events came after it.
      </p>
      {flows.map((flow) => {
        const latest = now.flows.find(
          (n) => n.scope_id === flow.scope_id && n.topic_id === flow.topic_id,
        );
        return (
          <section
            key={`${flow.topic_id}-${flow.scope_id}`}
            className="mt-5"
            data-scope={flow.scope_id}
            data-testid="asof-flow"
          >
            <h3 className="text-sm font-semibold text-ink-2">
              PRISMA flow, {scopeLabel(flow.scope_id)}
            </h3>
            <table className="mt-2 w-full max-w-[640px] text-sm tabular-nums">
              <thead className="text-left text-xs text-ink-3">
                <tr className="border-b border-rule">
                  <th className="py-1 font-normal" />
                  <th className="w-24 py-1 text-right font-normal">At event {then.cutoff_seq}</th>
                  <th className="w-24 py-1 text-right font-normal">Now</th>
                </tr>
              </thead>
              <tbody>
                {FLOW_ROWS.map(([key, label]) => {
                  const a = flow[key] as number;
                  const b = latest ? (latest[key] as number) : null;
                  return (
                    <tr key={key} className="border-b border-rule">
                      <td className="py-1.5 text-ink-2">{label}</td>
                      <td className="text-right font-semibold" data-testid={`asof-${key}`}>
                        {a}
                      </td>
                      <td
                        className={`text-right ${b !== a ? "text-petrol" : "text-ink-3"}`}
                        data-testid={`now-${key}`}
                      >
                        {b ?? ""}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {flow.checks.some((c) => !c.holds) ? (
              <p className="mt-2 text-sm text-exclude">
                Sums that do not hold:{" "}
                {flow.checks
                  .filter((c) => !c.holds)
                  .map((c) => c.name)
                  .join("; ")}
                .
              </p>
            ) : null}
          </section>
        );
      })}
      <section className="mt-6" aria-label="AI disclosure at the event">
        <h3 className="text-sm font-semibold text-ink-2">
          AI disclosure at event {then.cutoff_seq}
        </h3>
        {then.stages.length === 0 ? (
          <p className="mt-1 text-sm text-ink-2">
            No screening stage had opened yet: the AI had read nothing.
          </p>
        ) : (
          <table
            className="narrow mt-2 w-full max-w-[760px] text-sm tabular-nums"
            data-testid="asof-disclosure"
          >
            <thead className="text-left text-xs text-ink-3">
              <tr className="border-b border-rule">
                <th className="py-1 font-normal">Stage</th>
                <th className="py-1 font-normal">Records</th>
                <th className="py-1 font-normal">Read by the AI</th>
                <th className="py-1 font-normal">AI proposed exclusion</th>
                <th className="py-1 font-normal">Readings shown</th>
                <th className="py-1 font-normal">Revised after one</th>
                <th className="py-1 font-normal">Extra screens</th>
              </tr>
            </thead>
            <tbody>
              {then.stages.map((s) => (
                <tr
                  key={`${s.stage}-${s.scope_id}`}
                  className="border-b border-rule"
                  data-stage={s.stage}
                >
                  <td className="py-1.5">
                    {STAGES[s.stage] ?? s.stage}, {scopeLabel(s.scope_id)}
                    <span className="block text-xs text-ink-3">
                      {s.mode}, reveal {s.reveal.replace("_", " ")}
                    </span>
                  </td>
                  <td>{s.records}</td>
                  <td>{s.records_read_by_ai}</td>
                  <td>{s.ai_proposed_exclusions}</td>
                  <td>{s.reveals_shown}</td>
                  <td>{s.revised_after_reveal}</td>
                  <td>{s.rescreens_sent}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="narrow mt-2 text-xs text-ink-3 tabular-nums">
          Disclosure record {short(then.disclosure_hash)}
          {then.disclosure_hash === now.disclosure_hash ? ", the same as now" : ", changed since"}
        </p>
      </section>
    </article>
  );
}
