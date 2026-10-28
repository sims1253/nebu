// Full-text screening (DESIGN.md, "Reviewer workbench"): the report on the left, the criteria on
// the right with one active. Mentions of the active criterion come in document order and carry no
// verdict colour; the reviewer adopts the ones that are evidence. One reason decides: exclude on
// the criterion the report fails, with evidence for it, or include, which says every criterion is
// met. A verdict on each criterion is optional. A PDF that is not the record's report is marked
// as the wrong document, which takes it out of screening and is not an exclusion.
import { useRouteContext } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

import type {
  Anchor,
  Box,
  Criterion,
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
import { AiReading } from "./AiReading.tsx";
import type { CriterionName } from "./AiReading.tsx";

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
/** Outcomes that name one criterion: the key, then the criterion's number. */
const ON_A_CRITERION: Record<string, "exclude" | "insufficient_information"> = {
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
          setError(
            e instanceof WorkbenchError ? e.message : "Kittum could not load the full text.",
          ),
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

  if (error) return <Message title="Kittum could not load the full text">{error}</Message>;
  if (!scopeId) {
    return (
      <Message title="No full texts to screen">
        A record comes here after it passes title and abstract screening and has a full text, once
        the methods lead opens full-text screening for its {labels.scope.toLowerCase()}.
      </Message>
    );
  }
  if (assignment === undefined) return <Message title="Loading the next full text" />;
  if (assignment === null && unread > 0) {
    return (
      <Message title="The AI judge is reading">
        The AI judge has not read {unread} of your full texts yet. Each one appears here once the
        judge has read it.
      </Message>
    );
  }
  if (assignment === null) {
    return (
      <Message title="Full texts finished">
        You have decided every full text assigned to you
        {done > 0 ? `, ${done} in this session` : ""}.
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
  // `e` or `x` waits for the criterion's number.
  const [armed, setArmed] = useState<"exclude" | "insufficient_information" | null>(null);
  // `w` opens the wrong-document form; once sent, the report is out of screening.
  const [wrong, setWrong] = useState<"asking" | "sent" | null>(null);
  const [busy, setBusy] = useState(false);
  // The server's token, and the AI's reading once a provisional decision revealed it.
  const [etag, setEtag] = useState(assignment.etag);
  const [reveal, setReveal] = useState<Reveal | null>(assignment.reveal);
  const [aiQuote, setAiQuote] = useState<Anchor | null>(null);
  const [showQuote, setShowQuote] = useState(0);
  // The page follows the current mention, or the AI's quote when it was asked for last.
  const [follow, setFollow] = useState<"mention" | "ai">("mention");
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
  const nameOf = (c: Criterion) => `${criteria.indexOf(c) + 1} ${labels[c.slot] ?? c.slot}`;

  // The AI's quote on the page, once its reading is shown: the server places it.
  const revealed = reveal !== null;
  useEffect(() => {
    if (!revealed) return;
    let live = true;
    void run(
      workbench.aiQuote({
        projectId: project.id,
        as: me.actor_id,
        assignmentId: assignment.assignment_id,
      }),
    ).then(
      (found) => {
        if (!live) return;
        setAiQuote(found);
        setShowQuote((n) => n + 1);
        setFollow("ai");
      },
      () => live && setAiQuote(null),
    );
    return () => {
      live = false;
    };
  }, [revealed, workbench, project.id, me.actor_id, assignment.assignment_id]);

  const activeMentions = useMemo(
    (): ReadonlyArray<Anchor> => (criterion ? (mentions.get(criterion.id) ?? []) : []),
    [criterion, mentions],
  );

  const activeState = criterion ? stateOf(criterion.id) : null;
  const currentMention =
    activeState && activeState.cursor >= 0 ? activeMentions[activeState.cursor] : undefined;
  const verdicts = criteria.filter((c) => stateOf(c.id).verdict !== null).length;

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
      ...(aiQuote
        ? [
            {
              id: aiQuote.id,
              boxes: aiQuote.boxes,
              kind: "ai" as const,
              label: "The AI judge's quote",
            },
          ]
        : []),
    ],
    [criteria, stateOf, activeMentions, currentMention, aiQuote],
  );
  const aiBox = aiQuote?.boxes[0];
  const mentionBox = currentMention?.boxes[0];
  const target =
    follow === "ai" && aiQuote && aiBox
      ? { key: `${aiQuote.id}-${showQuote}`, box: aiBox }
      : currentMention && mentionBox
        ? { key: currentMention.id, box: mentionBox }
        : null;

  const jump = (step: 1 | -1) => {
    if (!criterion) return;
    if (activeMentions.length === 0) {
      setNotice("This report never uses the protocol terms of this criterion.");
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
    setFollow("mention");
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
      setNotice("Go to a mention with . first, or select text on the page.");
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
      setNotice(`Adopted as evidence for ${nameOf(criterion)}.`);
    } catch (e) {
      setNotice(
        e instanceof WorkbenchError
          ? `Not adopted. ${e.message}`
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
        // The AI read the report differently: show its reading, then keep or change.
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
        e instanceof WorkbenchError ? e.message : "Kittum did not record the decision. Try again.",
      );
    } finally {
      setBusy(false);
    }
  };

  /** The reviewer's readings so far, with each one's evidence. */
  const readings = (override?: { id: string; status: CriterionStatus }) =>
    criteria.flatMap((c) => {
      const state = stateOf(c.id);
      const status = override?.id === c.id ? override.status : state.verdict;
      return status
        ? [
            {
              criterion_id: c.id,
              status,
              evidence_anchor_ids: state.evidence.map((e) => e.id),
            },
          ]
        : [];
    });
  const phase = reveal && !outcome ? "final" : "provisional";

  /** `i`: one action that says every criterion is met. Evidence is optional. */
  const include = async () => {
    const against = criteria.filter((c) => {
      const verdict = stateOf(c.id).verdict;
      return verdict !== null && verdict !== "met";
    });
    const [first] = against;
    if (first) {
      setNotice(
        `Include means every criterion is met, but you marked ${nameOf(first)} ${VERDICTS[stateOf(first.id).verdict ?? "unclear"].label.toLowerCase()}. Change that verdict, or exclude.`,
      );
      return;
    }
    await send(
      {
        outcome: "include",
        reason_criterion_id: null,
        evidence_anchor_ids: criteria.flatMap((c) => stateOf(c.id).evidence.map((e) => e.id)),
        comment: null,
        criteria: readings(),
      },
      phase,
    );
  };

  /** `e` or `x` and a number: the one criterion the report fails, or lacks information on. */
  const decideOn = async (chosen: "exclude" | "insufficient_information", c: Criterion) => {
    const evidence = stateOf(c.id).evidence;
    const all = criteria.flatMap((x) => stateOf(x.id).evidence.map((e) => e.id));
    if (chosen === "exclude" && evidence.length === 0) {
      setActive(criteria.indexOf(c));
      setNotice(
        `To exclude on ${nameOf(c)}, adopt evidence for it first. Go to a mention with . and press Enter.`,
      );
      return;
    }
    if (chosen === "insufficient_information" && all.length === 0) {
      setActive(criteria.indexOf(c));
      setNotice(
        "Insufficient information needs at least one evidence span. Adopt one with Enter first.",
      );
      return;
    }
    const status = chosen === "exclude" ? "not_met" : "unclear";
    update(c.id, (s) => ({ ...s, verdict: status }));
    await send(
      {
        outcome: chosen,
        reason_criterion_id: chosen === "exclude" ? c.id : null,
        evidence_anchor_ids: chosen === "exclude" ? evidence.map((e) => e.id) : all,
        comment: null,
        criteria: readings({ id: c.id, status }),
      },
      phase,
    );
  };

  /** Space after a reveal: the provisional decision stands as the final one. */
  const keep = () => {
    if (!reveal || !provisional || outcome) return false;
    void send(provisional, "final");
    return true;
  };

  const markWrong = async (note: string) => {
    setBusy(true);
    try {
      await run(
        workbench.wrongDocument({
          projectId: project.id,
          as: me.actor_id,
          assignmentId: assignment.assignment_id,
          expectedEtag: etag,
          documentId: assignment.document.id,
          note: note.trim() || null,
        }),
      );
      setWrong("sent");
      setNotice(null);
      if (onDecided) window.setTimeout(onDecided, 1400);
    } catch (e) {
      setWrong(null);
      setNotice(e instanceof WorkbenchError ? e.message : "Kittum did not record it. Try again.");
    } finally {
      setBusy(false);
    }
  };

  function handleKey(event: KeyboardEvent): boolean {
    if (busy || !criterion || outcome || wrong) return false;
    const key = event.key;
    // A held key repeats; adopting toggles and verdicts advance, so act on the first press only.
    if (
      event.repeat &&
      (key === "Enter" || key in VERDICT_KEYS || key in ON_A_CRITERION || key === "i")
    )
      return true;
    if (armed) {
      setArmed(null);
      if (/^[1-9]$/.test(key)) {
        const chosen = criteria[Number(key) - 1];
        if (chosen) void decideOn(armed, chosen);
        else setNotice(`There is no criterion ${key}. Press 1 to ${criteria.length}.`);
        return true;
      }
      if (key === "Escape" || key === "Backspace" || key in ON_A_CRITERION) return true;
    }
    if (key === " ") return keep();
    if (key === "ArrowDown" || key === "j") {
      setActive((a) => Math.min(a + 1, criteria.length - 1));
      return true;
    }
    if (key === "ArrowUp" || key === "k") {
      setActive((a) => Math.max(a - 1, 0));
      return true;
    }
    if (key === "." || key === ",") {
      jump(key === "." ? 1 : -1);
      return true;
    }
    if (key === "Enter") {
      void adopt();
      return true;
    }
    if (key === "q") {
      if (aiQuote) {
        setShowQuote((n) => n + 1);
        setFollow("ai");
        setNotice(null);
      } else {
        setNotice(
          reveal
            ? "The AI judge's quote is not on the page, so there is nothing to show."
            : "The AI judge's reading appears after you decide.",
        );
      }
      return true;
    }
    if (key === "w") {
      setWrong("asking");
      return true;
    }
    if (/^[1-9]$/.test(key)) {
      setNotice("To decide on a criterion, press e or x first, then its number.");
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
    if (key === "i") {
      void include();
      return true;
    }
    const chosen = ON_A_CRITERION[key];
    if (chosen) {
      setArmed(chosen);
      setNotice(null);
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

  const revealedCriterion = reveal ? criteria.find((x) => x.id === reveal.criterion_id) : undefined;
  const aiPage = aiQuote?.boxes[0]?.page;
  const reasonCriterion = outcome?.reason
    ? criteria.find((c) => c.id === outcome.reason)
    : undefined;

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
            const chosen = outcome?.reason === c.id;
            return (
              <li
                key={c.id}
                aria-current={isActive || undefined}
                data-revealed={reveal?.criterion_id === c.id || undefined}
                className={`border-b border-rule px-5 py-3 ${isActive ? "bg-sheet-2 shadow-[inset_3px_0_0_var(--petrol)]" : ""} ${armed ? "shadow-[inset_0_2px_0_var(--exclude)]" : ""}`}
                onClick={() => setActive(index)}
              >
                <div className="flex items-center gap-2">
                  <span className={armed === "exclude" ? "text-exclude" : ""}>
                    <Key k={String(index + 1)} on={armed !== null} />
                  </span>
                  <span className="text-2xs text-ink-3">{labels[c.slot] ?? c.slot}</span>
                  {chosen ? <Mark term={OUTCOMES.exclude} title="Your exclusion reason" /> : null}
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
                  className={`mt-1 text-sm ${isActive || armed ? "" : "line-clamp-2 text-ink-2"}`}
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
            criterion={{
              number: revealedCriterion ? criteria.indexOf(revealedCriterion) + 1 : null,
              label: revealedCriterion
                ? (labels[revealedCriterion.slot] ?? revealedCriterion.slot)
                : reveal.criterion_id,
              text: revealedCriterion?.text_en ?? null,
            }}
            markedAt={aiPage ? `Marked on page ${aiPage}.` : null}
            agrees={
              provisional !== null &&
              (provisional.outcome === "exclude") === (reveal.status === "not_met")
            }
            settled={outcome !== null}
          />
        ) : null}
        {wrong ? (
          <WrongDocumentForm
            sent={wrong === "sent"}
            busy={busy}
            report={labels.report.toLowerCase()}
            onCancel={() => setWrong(null)}
            onSend={(note) => void markWrong(note)}
          />
        ) : (
          <footer className="border-t border-rule-strong bg-sheet-2 px-5 py-3 text-sm">
            <div className="flex flex-wrap gap-x-4 gap-y-1.5">
              <KeyHint keys={[",", "."]}>Mentions</KeyHint>
              <KeyHint keys={["Enter"]}>{selection ? "Adopt selection" : "Adopt"}</KeyHint>
              <KeyHint keys={["y", "n", "?"]}>Verdict, if you want one</KeyHint>
              {aiQuote ? <KeyHint keys={["q"]}>The AI&rsquo;s quote</KeyHint> : null}
            </div>
            <div className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1.5 border-t border-rule pt-2.5">
              <KeyHint keys={["i"]}>Include, every criterion met</KeyHint>
              <span className="text-exclude">
                <KeyHint keys={["e"]} on={armed === "exclude"}>
                  Exclude on a criterion
                </KeyHint>
              </span>
              <KeyHint keys={["x"]} on={armed === "insufficient_information"}>
                Insufficient information
              </KeyHint>
              <KeyHint keys={["w"]}>Wrong document</KeyHint>
            </div>
            <p
              className="mt-2 min-h-5 text-xs text-ink-2"
              aria-live="polite"
              data-testid="fulltext-notice"
            >
              {outcome ? (
                <Status term={OUTCOMES[outcome.outcome]}>
                  {OUTCOMES[outcome.outcome].past}
                  {reasonCriterion ? ` on ${nameOf(reasonCriterion)}` : ""}.
                </Status>
              ) : armed ? (
                `Now press 1 to ${criteria.length} for the criterion. Esc cancels.`
              ) : (
                (notice ??
                (verdicts > 0
                  ? `${verdicts} of ${criteria.length} criteria have your verdict. Decide when you are sure.`
                  : "Decide when you are sure. You do not need a verdict on every criterion."))
              )}
            </p>
          </footer>
        )}
      </aside>
    </div>
  );
}

function WrongDocumentForm({
  sent,
  busy,
  report,
  onCancel,
  onSend,
}: {
  sent: boolean;
  busy: boolean;
  report: string;
  onCancel: () => void;
  onSend: (note: string) => void;
}) {
  const [note, setNote] = useState("");
  const field = useRef<HTMLInputElement>(null);
  useEffect(() => field.current?.focus(), []);
  if (sent) {
    return (
      <footer
        className="border-t border-rule-strong bg-sheet-2 px-5 py-3 text-sm"
        data-testid="wrong-document-sent"
        aria-live="polite"
      >
        <p className="font-semibold">Marked as the wrong document</p>
        <p className="mt-1 text-xs text-ink-2">
          Kittum took this {report} out of screening and detached the PDF. The record now shows as
          missing its full text. This is not an exclusion.
        </p>
      </footer>
    );
  }
  return (
    <form
      className="border-t border-rule-strong bg-sheet-2 px-5 py-3 text-sm"
      aria-label="Wrong document"
      data-testid="wrong-document"
      onSubmit={(event) => {
        event.preventDefault();
        if (!busy) onSend(note);
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          onCancel();
        }
      }}
    >
      <p className="font-semibold">Is this PDF not the record&rsquo;s {report}?</p>
      <p className="mt-1 text-xs text-ink-2">
        Kittum takes the {report} out of screening and detaches the PDF from the record. The record
        then shows as missing its full text, and the methods lead can attach the right one. This is
        not an exclusion.
      </p>
      <label className="mt-2.5 block text-xs text-ink-2" htmlFor="wrong-document-note">
        What is the PDF instead? Optional.
      </label>
      <input
        id="wrong-document-note"
        ref={field}
        value={note}
        onChange={(event) => setNote(event.target.value)}
        className="mt-1 w-full rounded-[3px] border border-rule-strong bg-sheet px-2 py-1 text-sm"
        placeholder="For example, the journal's reporting checklist"
        maxLength={500}
      />
      <p className="mt-2.5 flex gap-4 text-xs">
        <KeyHint keys={["Enter"]}>Mark as the wrong document</KeyHint>
        <KeyHint keys={["Escape"]}>Cancel</KeyHint>
      </p>
    </form>
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
      <p className="mt-2 text-xs text-ink-3">
        The protocol terms of this criterion do not occur in this report. Select text on the page to
        cite it.
      </p>
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
          <Key k="," /> <Key k="." /> to go through them, <Key k="Enter" /> to adopt
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
  criterion,
  markedAt,
  agrees,
  settled,
}: {
  reveal: Reveal;
  criterion: CriterionName;
  markedAt: string | null;
  agrees: boolean;
  settled: boolean;
}) {
  return (
    <section
      aria-label="AI judge"
      data-testid="ai-reveal"
      className="reveal-enter evidence-item max-h-[45%] overflow-y-auto border-t border-rule px-5 py-2.5"
    >
      <p className="text-sm font-semibold">
        {agrees ? "The AI judge agrees" : "The AI judge disagrees"}
      </p>
      <div className="mt-1">
        <AiReading
          criterion={criterion}
          status={reveal.status}
          downgradedFrom={reveal.downgraded_from}
          rationale={reveal.rationale}
          quote={reveal.quote || null}
          verified={reveal.quote_verified ?? false}
          markedAt={markedAt}
          compact
        />
      </div>
      {settled ? null : (
        <p className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-xs">
          <KeyHint keys={[" "]}>Keep yours</KeyHint>
          <KeyHint keys={["i", "e", "x"]}>Change it</KeyHint>
          {markedAt ? <KeyHint keys={["q"]}>Show the quote</KeyHint> : null}
        </p>
      )}
    </section>
  );
}
