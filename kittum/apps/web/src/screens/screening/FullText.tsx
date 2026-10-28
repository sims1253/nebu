// Full-text screening (DESIGN.md, "Reviewer workbench"): the report on the left, the criteria on
// the right with one active. Mentions of the active criterion come in document order and carry no
// verdict colour; the reviewer adopts the ones that are evidence.
import { useRouteContext } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

import type {
  Anchor,
  Box,
  CriterionStatus,
  Decision,
  FullTextAssignment,
  Outcome,
  Reveal,
} from "../../data/schema.ts";
import { run, WorkbenchError } from "../../data/workbench.ts";
import { Key, KeyHint } from "../../ui/Key.tsx";
import { useKeys } from "../../ui/keys.ts";
import { ADOPTED, Mark, OUTCOMES, Status, VERDICTS } from "../../ui/Status.tsx";
import { PdfViewer } from "../pdf/PdfViewer.tsx";
import type { PageHighlight, TextSelection } from "../pdf/PdfViewer.tsx";
import { useProject } from "../project.tsx";

interface Evidence {
  /** The saved anchor's ID. */
  readonly id: string;
  /** The mention or selection it came from, so Enter on it again removes it. */
  readonly key: string;
  readonly quote: string;
  readonly boxes: ReadonlyArray<Box>;
}

interface CriterionState {
  readonly verdict: CriterionStatus | null;
  readonly evidence: ReadonlyArray<Evidence>;
  readonly cursor: number; // index into the mentions, -1 before the first jump
}

const VERDICT_KEYS: Record<string, CriterionStatus> = { y: "met", n: "not_met", "?": "unclear" };
const OUTCOME_KEYS: Record<string, Outcome> = {
  i: "include",
  e: "exclude",
  x: "insufficient_information",
};

/** The reviewer's full-text queue, one report at a time. */
export function FullTextQueue() {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me, labels, screeningStage } = useProject();
  const scopeId = screeningStage("full_text")?.scope_id ?? null;
  const [assignment, setAssignment] = useState<FullTextAssignment | null | undefined>(undefined);
  const [done, setDone] = useState(0);
  const [error, setError] = useState<string | null>(null);
  // Full texts still unread by the AI judge, while the queue is empty.
  const [unread, setUnread] = useState(0);
  const [poll, setPoll] = useState(0);

  useEffect(() => {
    if (!scopeId) return;
    let live = true;
    const caller = { projectId: project.id, as: me.actor_id };
    run(workbench.fullText({ ...caller, scopeId }))
      .then(async (next) => {
        const status =
          next === null
            ? await run(workbench.status({ ...caller, scopeId, stage: "full_text" }))
            : null;
        if (!live) return;
        setUnread(status?.remaining ?? 0);
        setAssignment(next);
      })
      .catch(
        (e: unknown) =>
          live &&
          setError(e instanceof WorkbenchError ? e.message : "The full text could not load."),
      );
    return () => {
      live = false;
    };
  }, [workbench, project.id, me.actor_id, scopeId, done, poll]);

  useEffect(() => {
    if (assignment !== null || unread === 0) return;
    const timer = window.setTimeout(() => setPoll((n) => n + 1), 3000);
    return () => window.clearTimeout(timer);
  }, [assignment, unread, poll]);

  if (error) return <Message title="The full text could not load">{error}</Message>;
  if (!scopeId) {
    return (
      <Message title="No full texts to screen">
        Full texts arrive here once records pass title and abstract screening, a{" "}
        {labels.report.toLowerCase()} is acquired, and the methods lead opens full-text screening.
      </Message>
    );
  }
  if (assignment === undefined) return <Message title="Loading the next full text" />;
  if (assignment === null && unread > 0) {
    return (
      <Message title="The AI judge is reading">
        {unread} of your full texts are waiting for the AI judge. They appear here as soon as it has
        read them; it reads every report before anyone screens it.
      </Message>
    );
  }
  if (assignment === null) {
    return (
      <Message title="Full texts finished">
        Every full text assigned to you has a decision{done > 0 ? `: ${done} in this session` : ""}.
      </Message>
    );
  }
  return (
    <FullTextScreen
      key={assignment.assignment_id}
      assignment={assignment}
      onDecided={() => setDone((n) => n + 1)}
    />
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

function FullTextScreen({
  assignment,
  onDecided,
}: {
  assignment: FullTextAssignment;
  onDecided?: () => void;
}) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me, labels, criteriaFor } = useProject();
  const criteria = useMemo(
    () => criteriaFor("full_text", assignment.scope_id),
    [criteriaFor, assignment.scope_id],
  );
  const mentions = useMemo(() => new Map(assignment.mentions), [assignment.mentions]);
  const [active, setActive] = useState(0);
  const [states, setStates] = useState<Record<string, CriterionState>>({});
  const [selection, setSelection] = useState<TextSelection | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<{ outcome: Outcome; reason: string | null } | null>(null);
  const [busy, setBusy] = useState(false);
  // The server's token, and the AI's reading once a provisional decision revealed it.
  const [etag, setEtag] = useState(assignment.etag);
  const [reveal, setReveal] = useState<Reveal | null>(assignment.reveal);
  const [provisional, setProvisional] = useState<Decision | null>(null);
  // DESIGN.md "Metrics": when the report appeared and when the reviewer first acted on it.
  const [shownAt] = useState(() => new Date().toISOString());
  const firstKeyAt = useRef<string | null>(null);
  // A reveal that came with the item was shown when the item was.
  const revealedAt = useRef<string | null>(assignment.reveal ? shownAt : null);

  const criterion = criteria[active];
  const stateOf = useCallback(
    (id: string): CriterionState => states[id] ?? { verdict: null, evidence: [], cursor: -1 },
    [states],
  );
  const update = (id: string, change: (s: CriterionState) => CriterionState) =>
    setStates((all) => ({
      ...all,
      [id]: change(all[id] ?? { verdict: null, evidence: [], cursor: -1 }),
    }));

  const activeMentions = useMemo(
    (): ReadonlyArray<Anchor> => (criterion ? (mentions.get(criterion.id) ?? []) : []),
    [criterion, mentions],
  );

  const activeState = criterion ? stateOf(criterion.id) : null;
  const currentMention =
    activeState && activeState.cursor >= 0 ? activeMentions[activeState.cursor] : undefined;
  const pending = criteria.filter((c) => stateOf(c.id).verdict === null).length;

  const highlights = useMemo(
    (): PageHighlight[] => [
      ...criteria.flatMap((c) =>
        stateOf(c.id).evidence.map((e) => ({
          id: `adopted-${c.id}-${e.id}`,
          boxes: e.boxes,
          kind: "adopted" as const,
          label: "Adopted evidence",
        })),
      ),
      ...activeMentions
        .filter((m) => m !== currentMention)
        .map((m) => ({ id: m.id, boxes: m.boxes, kind: "mention" as const, label: "Mention" })),
      ...(currentMention
        ? [
            {
              id: currentMention.id,
              boxes: currentMention.boxes,
              kind: "current" as const,
              label: "Current mention",
            },
          ]
        : []),
    ],
    [criteria, stateOf, activeMentions, currentMention],
  );
  const firstBox = currentMention?.boxes[0];
  const target = currentMention && firstBox ? { key: currentMention.id, box: firstBox } : null;

  const jump = (step: 1 | -1) => {
    if (!criterion) return;
    if (activeMentions.length === 0) {
      setNotice("The protocol terms do not occur in this report for this criterion.");
      return;
    }
    update(criterion.id, (s) => ({
      ...s,
      cursor:
        s.cursor < 0
          ? step === 1
            ? 0
            : activeMentions.length - 1
          : (s.cursor + step + activeMentions.length) % activeMentions.length,
    }));
    setNotice(null);
  };

  const adopt = async () => {
    if (!criterion) return;
    const source = selection
      ? {
          key: `selection-${selection.start}-${selection.end}`,
          quote: selection.quote,
          start: selection.start,
          boxes: selection.boxes,
        }
      : currentMention
        ? {
            key: currentMention.id,
            quote: currentMention.quote.exact,
            start: currentMention.start,
            boxes: currentMention.boxes,
          }
        : null;
    if (!source) {
      setNotice("Jump to a mention with ] or select text on the page first.");
      return;
    }
    const already = stateOf(criterion.id).evidence.find((e) => e.key === source.key);
    if (already) {
      update(criterion.id, (s) => ({
        ...s,
        evidence: s.evidence.filter((e) => e.key !== source.key),
      }));
      setNotice("Removed from the evidence.");
      return;
    }
    setBusy(true);
    try {
      // The span becomes an anchor on the layer: the decision cites it by ID.
      const anchor = await run(
        workbench.adopt({ layerId: assignment.layer.id, quote: source.quote, start: source.start }),
      );
      const evidence: Evidence = {
        id: anchor.id,
        key: source.key,
        quote: anchor.quote.exact,
        boxes: source.boxes,
      };
      update(criterion.id, (s) => ({ ...s, evidence: [...s.evidence, evidence] }));
      setNotice("Adopted as evidence.");
    } catch (e) {
      setNotice(
        e instanceof WorkbenchError
          ? `Not adopted: ${e.message}`
          : "Not adopted. Select whole words and try again.",
      );
    } finally {
      setBusy(false);
    }
    if (selection) {
      window.getSelection()?.removeAllRanges();
      setSelection(null);
    }
  };

  const send = async (decision: Decision, phase: "provisional" | "final") => {
    const now = new Date().toISOString();
    setBusy(true);
    try {
      const result = await run(
        workbench.decide({
          projectId: project.id,
          as: me.actor_id,
          assignmentId: assignment.assignment_id,
          decision,
          phase,
          expectedEtag: etag,
          timing: {
            shown_at: shownAt,
            first_key_at: firstKeyAt.current ?? now,
            decided_at: now,
            reveal_shown_at: revealedAt.current,
          },
        }),
      );
      setEtag(result.etag);
      if (result.phase === "provisional" && result.reveal) {
        // The AI read the report differently: show its reading, then keep or revise.
        revealedAt.current = new Date().toISOString();
        setReveal(result.reveal);
        setProvisional(decision);
        setNotice(null);
        return;
      }
      setOutcome({ outcome: decision.outcome, reason: decision.reason_criterion_id });
      setNotice(null);
      if (onDecided) window.setTimeout(onDecided, 900);
    } catch (e) {
      setNotice(
        e instanceof WorkbenchError ? e.message : "The decision was not recorded. Try again.",
      );
    } finally {
      setBusy(false);
    }
  };

  const decide = async (chosen: Outcome) => {
    if (pending > 0) {
      setNotice(`Give every criterion a verdict first: ${pending} left.`);
      return;
    }
    const notMet = criteria.filter((c) => stateOf(c.id).verdict === "not_met");
    const reason = notMet.find((c) => stateOf(c.id).evidence.length > 0) ?? null;
    if (chosen === "include" && criteria.some((c) => stateOf(c.id).verdict !== "met")) {
      setNotice("Include needs every criterion met. Exclude, or change a verdict.");
      return;
    }
    if (chosen === "exclude" && !reason) {
      setNotice("Exclude needs a criterion marked not met, with adopted evidence.");
      return;
    }
    if (
      chosen === "insufficient_information" &&
      !criteria.some((c) => stateOf(c.id).verdict === "unclear")
    ) {
      setNotice("Insufficient information needs at least one criterion marked unclear.");
      return;
    }
    const anchors = criteria.flatMap((c) => stateOf(c.id).evidence.map((e) => e.id));
    if (anchors.length === 0) {
      setNotice("Every outcome cites evidence. Adopt at least one span with Enter.");
      return;
    }
    await send(
      {
        outcome: chosen,
        reason_criterion_id: chosen === "exclude" ? (reason?.id ?? null) : null,
        evidence_anchor_ids: anchors,
        comment: null,
        criteria: criteria.map((c) => ({
          criterion_id: c.id,
          status: stateOf(c.id).verdict ?? "unclear",
          evidence_anchor_ids: stateOf(c.id).evidence.map((e) => e.id),
        })),
      },
      reveal && !outcome ? "final" : "provisional",
    );
  };

  /** Space after a reveal: the provisional decision stands as the final one. */
  const keep = () => {
    if (!reveal || !provisional || outcome) return false;
    void send(provisional, "final");
    return true;
  };

  function handleKey(event: KeyboardEvent): boolean {
    if (busy || !criterion || outcome) return false;
    const key = event.key;
    if (key === " ") return keep();
    // A held key repeats; adopting toggles and verdicts advance, so act on the first press only.
    if (event.repeat && (key === "Enter" || key in VERDICT_KEYS || key in OUTCOME_KEYS))
      return true;
    if (key === "ArrowDown" || key === "j") {
      setActive((a) => Math.min(a + 1, criteria.length - 1));
      return true;
    }
    if (key === "ArrowUp" || key === "k") {
      setActive((a) => Math.max(a - 1, 0));
      return true;
    }
    if (key === "]" || key === "[") {
      jump(key === "]" ? 1 : -1);
      return true;
    }
    if (key === "Enter") {
      void adopt();
      return true;
    }
    const verdict = VERDICT_KEYS[key];
    if (verdict) {
      update(criterion.id, (s) => ({ ...s, verdict }));
      const next = criteria.findIndex((c, i) => i > active && stateOf(c.id).verdict === null);
      if (next >= 0) setActive(next);
      setNotice(null);
      return true;
    }
    const chosen = OUTCOME_KEYS[key];
    if (chosen) {
      void decide(chosen);
      return true;
    }
    return false;
  }

  useKeys((event) => {
    const at = new Date().toISOString();
    const handled = handleKey(event);
    if (handled) firstKeyAt.current ??= at;
    return handled;
  });

  return (
    <div className="grid h-full grid-cols-[minmax(0,1fr)_minmax(380px,34%)]">
      <PdfViewer
        workbench={workbench}
        layer={assignment.layer}
        highlights={highlights}
        target={target}
        onSelection={setSelection}
        label={`${assignment.report.study_label}, ${assignment.report.citation}`}
      />
      <aside
        className="flex min-h-0 flex-col border-l border-rule-strong bg-sheet"
        aria-label="Criteria"
      >
        <header className="border-b border-rule px-5 pt-4 pb-3">
          <p className="text-xs text-ink-3">
            {labels.report} {assignment.report.study_label}, {assignment.report.licence}
          </p>
          <h1 className="mt-1 line-clamp-2 text-sm font-semibold" title={assignment.report.title}>
            {assignment.report.title}
          </h1>
        </header>
        <ol className="min-h-0 flex-1 overflow-y-auto" data-testid="criteria-list">
          {criteria.map((c, index) => {
            const state = stateOf(c.id);
            const isActive = index === active;
            const list = mentions.get(c.id) ?? [];
            return (
              <li
                key={c.id}
                aria-current={isActive || undefined}
                className={`border-b border-rule px-5 py-3 ${isActive ? "bg-sheet-2 shadow-[inset_3px_0_0_var(--petrol)]" : ""}`}
                onClick={() => setActive(index)}
              >
                <div className="flex items-center gap-2">
                  <span className="text-2xs text-ink-3">{labels[c.slot] ?? c.slot}</span>
                  <span className="ml-auto flex items-center gap-2 text-xs">
                    {state.evidence.length > 0 ? (
                      <span className="text-ink-2">
                        {state.evidence.length} evidence{" "}
                        {state.evidence.length === 1 ? "span" : "spans"}
                      </span>
                    ) : null}
                    {state.verdict ? (
                      <Status term={VERDICTS[state.verdict]} />
                    ) : (
                      <span className="text-ink-3">No verdict</span>
                    )}
                  </span>
                </div>
                <p
                  className={`mt-1 text-sm ${isActive ? "" : "line-clamp-2 text-ink-2"}`}
                  title={c.text_de}
                >
                  {c.text_en}
                </p>
                {isActive ? (
                  <Mentions
                    mentions={list}
                    cursor={state.cursor}
                    adopted={new Set(state.evidence.map((e) => e.key))}
                    selections={state.evidence.filter((e) => e.key.startsWith("selection-"))}
                  />
                ) : null}
              </li>
            );
          })}
        </ol>
        {reveal ? (
          <FullTextReveal
            reveal={reveal}
            label={(() => {
              const c = criteria.find((x) => x.id === reveal.criterion_id);
              return c ? (labels[c.slot] ?? c.slot) : reveal.criterion_id;
            })()}
            settled={outcome !== null}
          />
        ) : null}
        <footer className="border-t border-rule-strong bg-sheet-2 px-5 py-3 text-sm">
          <div className="flex flex-wrap gap-x-4 gap-y-1.5">
            <KeyHint keys={["y"]}>{VERDICTS.met.label}</KeyHint>
            <KeyHint keys={["n"]}>{VERDICTS.not_met.label}</KeyHint>
            <KeyHint keys={["?"]}>{VERDICTS.unclear.label}</KeyHint>
            <KeyHint keys={["[", "]"]}>Mentions</KeyHint>
            <KeyHint keys={["Enter"]}>{selection ? "Adopt selection" : "Adopt"}</KeyHint>
          </div>
          <div className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1.5 border-t border-rule pt-2.5">
            {(["include", "exclude", "insufficient_information"] as const).map((o) => (
              <KeyHint key={o} keys={[OUTCOMES[o].glyph]} muted={pending > 0}>
                {o === "insufficient_information" ? "Insufficient" : OUTCOMES[o].label}
              </KeyHint>
            ))}
          </div>
          <p
            className="mt-2 min-h-5 text-xs text-ink-2"
            aria-live="polite"
            data-testid="fulltext-notice"
          >
            {outcome ? (
              <Status term={OUTCOMES[outcome.outcome]}>
                {OUTCOMES[outcome.outcome].past}
                {outcome.reason
                  ? ` on ${labels[criteria.find((c) => c.id === outcome.reason)?.slot ?? "other"]}`
                  : ""}
                .
              </Status>
            ) : (
              (notice ??
              (pending > 0
                ? `${pending} of ${criteria.length} criteria need a verdict before you decide.`
                : "Every criterion has a verdict. Decide with i, e, or x."))
            )}
          </p>
        </footer>
      </aside>
    </div>
  );
}

function Mentions({
  mentions,
  cursor,
  adopted,
  selections,
}: {
  mentions: ReadonlyArray<Anchor>;
  cursor: number;
  adopted: ReadonlySet<string>;
  selections: ReadonlyArray<Evidence>;
}) {
  if (mentions.length === 0 && selections.length === 0) {
    return (
      <p className="mt-2 text-xs text-ink-3">No mentions. Select text on the page to cite it.</p>
    );
  }
  return (
    <ol className="mt-2.5 grid gap-1" aria-label="Mentions in document order">
      {mentions.map((mention, index) => {
        const page = mention.boxes[0]?.page ?? 0;
        const current = index === cursor;
        return (
          <li
            key={mention.id}
            aria-current={current || undefined}
            data-testid="mention"
            className={`grid grid-cols-[2.6rem_minmax(0,1fr)_auto] items-baseline gap-2 rounded-[2px] py-1 pr-2 pl-2 text-xs ${current ? "evidence-item" : ""}`}
          >
            <span className="text-ink-3">p. {page}</span>
            <span className="line-clamp-2 text-ink-2">
              {tail(mention.quote.prefix)}
              <span className="font-medium text-ink">
                {mention.quote.exact.replace(/\s+/g, " ")}
              </span>
              {head(mention.quote.suffix)}
            </span>
            {adopted.has(mention.id) ? <Mark term={ADOPTED} /> : <span />}
          </li>
        );
      })}
      {selections.map((selection) => (
        <li
          key={selection.id}
          className="grid grid-cols-[2.6rem_minmax(0,1fr)_auto] gap-2 py-1 pr-2 pl-2 text-xs"
        >
          <span className="text-ink-3">p. {selection.boxes[0]?.page}</span>
          <span className="line-clamp-2 font-medium">{selection.quote.replace(/\s+/g, " ")}</span>
          <Mark term={ADOPTED} />
        </li>
      ))}
      <li className="mt-1 flex gap-3 text-2xs text-ink-3">
        <span>
          {cursor < 0 ? "–" : cursor + 1} of {mentions.length}
        </span>
        <span>
          <Key k="[" /> <Key k="]" /> to jump, <Key k="Enter" /> to adopt
        </span>
      </li>
    </ol>
  );
}

// Context around a mention, cut at a word boundary.
const tail = (text: string) => {
  const flat = text.replace(/\s+/g, " ");
  if (flat.length <= 28) return flat;
  return `…${flat.slice(-28).replace(/^\S*\s/, "")}`;
};
const head = (text: string) => {
  const flat = text.replace(/\s+/g, " ");
  if (flat.length <= 28) return flat;
  return `${flat.slice(0, 28).replace(/\s\S*$/, "")}…`;
};

function FullTextReveal({
  reveal,
  label,
  settled,
}: {
  reveal: Reveal;
  label: string;
  settled: boolean;
}) {
  return (
    <section
      aria-label="AI judge"
      data-testid="ai-reveal"
      className="reveal-enter evidence-item border-t border-rule px-5 py-2.5 text-sm"
    >
      <p className="flex items-baseline gap-2">
        <span className="font-semibold">The AI judge read this differently</span>
        <span className="text-ink-2">{label}:</span>
        <Status term={VERDICTS[reveal.status]} />
      </p>
      {reveal.quote ? (
        <p className="mt-1 text-xs text-ink-2">&ldquo;{reveal.quote}&rdquo;</p>
      ) : null}
      <p className="mt-1 text-xs text-ink-2">{reveal.rationale}</p>
      {settled ? null : (
        <p className="mt-1.5 flex gap-4 text-xs">
          <KeyHint keys={[" "]}>Keep yours</KeyHint>
          <KeyHint keys={["i", "e", "x"]}>Revise</KeyHint>
        </p>
      )}
    </section>
  );
}
