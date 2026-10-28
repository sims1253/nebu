// Full-text screening (DESIGN.md, "Reviewer workbench"): the report on the left, the criteria on
// the right with one active. Mentions of the active criterion come in document order and carry no
// verdict colour; the reviewer adopts the ones that are evidence.
import { useRouteContext } from "@tanstack/react-router";
import { useCallback, useMemo, useState } from "react";

import type {
  Anchor,
  Box,
  CriterionStatus,
  FullTextAssignment,
  Outcome,
} from "../../data/schema.ts";
import { run, WorkbenchError } from "../../data/workbench.ts";
import { Key, KeyHint } from "../../ui/Key.tsx";
import { useKeys } from "../../ui/keys.ts";
import { ADOPTED, Mark, OUTCOMES, Status, VERDICTS } from "../../ui/Status.tsx";
import { PdfViewer } from "../pdf/PdfViewer.tsx";
import type { PageHighlight, TextSelection } from "../pdf/PdfViewer.tsx";
import { useProject } from "../project.tsx";

interface Evidence {
  readonly id: string;
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

export function FullTextScreen({ assignment }: { assignment: FullTextAssignment | null }) {
  const { labels } = useProject();
  if (!assignment) {
    return (
      <div className="mx-auto max-w-[640px] px-10 py-16">
        <h1 className="text-xl font-semibold">No full texts to screen</h1>
        <p className="mt-2 text-md text-ink-2">
          Full texts arrive here once records pass title and abstract screening and a{" "}
          {labels.report.toLowerCase()} is acquired.
        </p>
      </div>
    );
  }
  return <Screen assignment={assignment} />;
}

function Screen({ assignment }: { assignment: FullTextAssignment }) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { labels, criteriaFor } = useProject();
  const criteria = useMemo(() => criteriaFor("full_text"), [criteriaFor]);
  const mentions = useMemo(() => new Map(assignment.mentions), [assignment.mentions]);
  const [active, setActive] = useState(0);
  const [states, setStates] = useState<Record<string, CriterionState>>({});
  const [selection, setSelection] = useState<TextSelection | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<{ outcome: Outcome; reason: string | null } | null>(null);
  const [busy, setBusy] = useState(false);

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

  const adopt = () => {
    if (!criterion) return;
    const evidence: Evidence | null = selection
      ? {
          id: `selection-${selection.start}-${selection.end}`,
          quote: selection.quote,
          boxes: selection.boxes,
        }
      : currentMention
        ? { id: currentMention.id, quote: currentMention.quote.exact, boxes: currentMention.boxes }
        : null;
    if (!evidence) {
      setNotice("Jump to a mention with ] or select text on the page first.");
      return;
    }
    const already = stateOf(criterion.id).evidence.some((e) => e.id === evidence.id);
    update(criterion.id, (s) => ({
      ...s,
      evidence: already
        ? s.evidence.filter((e) => e.id !== evidence.id)
        : [...s.evidence, evidence],
    }));
    setNotice(already ? "Removed from the evidence." : "Adopted as evidence.");
    if (selection) {
      window.getSelection()?.removeAllRanges();
      setSelection(null);
    }
  };

  const decide = async (chosen: Outcome) => {
    if (pending > 0) {
      setNotice(`Give every criterion a verdict first: ${pending} left.`);
      return;
    }
    const notMet = criteria.filter((c) => stateOf(c.id).verdict === "not_met");
    const reason = notMet.find((c) => stateOf(c.id).evidence.length > 0) ?? null;
    if (chosen === "include" && notMet.length > 0) {
      setNotice("A criterion is not met. Exclude, or change that verdict.");
      return;
    }
    if (chosen === "exclude" && !reason) {
      setNotice("Exclude needs a criterion marked not met, with adopted evidence.");
      return;
    }
    const anchors = criteria.flatMap((c) => stateOf(c.id).evidence.map((e) => e.id));
    if (anchors.length === 0) {
      setNotice("Every outcome cites evidence. Adopt at least one span with Enter.");
      return;
    }
    const now = new Date().toISOString();
    setBusy(true);
    try {
      await run(
        workbench.decide({
          assignmentId: assignment.assignment_id,
          decision: {
            outcome: chosen,
            reason_criterion_id: chosen === "exclude" ? (reason?.id ?? null) : null,
            evidence_anchor_ids: anchors,
            comment: null,
          },
          phase: "provisional",
          expectedRevision: assignment.revision,
          timing: { shown_at: now, first_key_at: now, decided_at: now, reveal_shown_at: null },
        }),
      );
      setOutcome({ outcome: chosen, reason: chosen === "exclude" ? (reason?.id ?? null) : null });
      setNotice(null);
    } catch (e) {
      setNotice(
        e instanceof WorkbenchError ? e.message : "The decision was not recorded. Try again.",
      );
    } finally {
      setBusy(false);
    }
  };

  useKeys((event) => {
    if (busy || !criterion) return false;
    const key = event.key;
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
      adopt();
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
                    adopted={new Set(state.evidence.map((e) => e.id))}
                    selections={state.evidence.filter((e) => e.id.startsWith("selection-"))}
                  />
                ) : null}
              </li>
            );
          })}
        </ol>
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
                . No more full texts in your queue.
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

const tail = (text: string) => {
  const flat = text.replace(/\s+/g, " ");
  return flat.length > 28 ? `…${flat.slice(-28)}` : flat;
};
const head = (text: string) => {
  const flat = text.replace(/\s+/g, " ");
  return flat.length > 28 ? `${flat.slice(0, 28)}…` : flat;
};
