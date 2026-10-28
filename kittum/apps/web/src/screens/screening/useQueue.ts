// The title and abstract queue: one record at a time, five more already loaded.
//
// The reviewer decides first. A decision goes to the server as provisional; the server either
// settles it (the AI agrees, or the project never reveals) or returns a reveal, which stays on
// screen until the reviewer keeps (Space) or revises (another decision key). Backspace reopens the
// previous record; a new decision on it takes the old one back on the server first (undo), and
// Space leaves it as it was.
import { useCallback, useEffect, useRef, useState } from "react";

import type { Decision, Outcome, QueueItem, Reveal, Stage } from "../../data/schema.ts";
import { run, WorkbenchError } from "../../data/workbench.ts";
import type { Caller, Workbench } from "../../data/workbench.ts";

const AHEAD = 5;

export interface Entry {
  readonly item: QueueItem;
  /** The server's token for the item's current state. */
  readonly etag: string;
  readonly decision: Decision | null;
  /** The decision is recorded as final; the record may still be reopened. */
  readonly settled: boolean;
  /** Once shown, the AI's reading stays with the record. */
  readonly reveal: Reveal | null;
  readonly revealedAt: string | null;
  readonly shownAt: string;
  readonly shownMs: number;
  readonly firstKeyAt: string | null;
}

export interface SessionStats {
  readonly decided: number;
  readonly medianSeconds: number | null;
  readonly revealsShown: number;
  readonly revealsFollowed: number;
}

const enter = (item: QueueItem): Entry => ({
  item,
  etag: item.etag,
  decision: null,
  settled: false,
  // An item that comes back awaiting a final decision brings the reviewer's own reveal.
  reveal: item.reveal,
  revealedAt: item.reveal ? new Date().toISOString() : null,
  shownAt: new Date().toISOString(),
  shownMs: performance.now(),
  firstKeyAt: null,
});

const median = (values: ReadonlyArray<number>) => {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? (sorted[mid] ?? 0) : ((sorted[mid - 1] ?? 0) + (sorted[mid] ?? 0)) / 2;
};

export function useQueue(
  workbench: Workbench,
  caller: Caller,
  scopeId: string | null,
  stage: Stage = "title_abstract",
) {
  // The current record and the buffer change together, so they are one state. `forward` holds
  // records that undo stepped back over, whole, so they return with their decision and revision.
  const [view, setView] = useState<{
    current: Entry | null;
    forward: ReadonlyArray<Entry>;
    ahead: ReadonlyArray<QueueItem>;
  }>({ current: null, forward: [], ahead: [] });
  const { current, forward, ahead } = view;
  const [history, setHistory] = useState<ReadonlyArray<Entry>>([]);
  const [exhausted, setExhausted] = useState(scopeId === null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const loading = useRef(false);
  const timings = useRef(new Map<string, number>());
  const reveals = useRef({ shown: 0, followed: 0 });
  const [stats, setStats] = useState<SessionStats>({
    decided: 0,
    medianSeconds: null,
    revealsShown: 0,
    revealsFollowed: 0,
  });

  // Keep the current record and five more loaded.
  useEffect(() => {
    if (scopeId === null || exhausted || loading.current) return;
    const buffered = (current ? 1 : 0) + forward.length + ahead.length;
    if (buffered > AHEAD) return;
    const last =
      ahead.at(-1)?.assignment_id ??
      forward.at(-1)?.item.assignment_id ??
      current?.item.assignment_id ??
      history.at(-1)?.item.assignment_id ??
      null;
    loading.current = true;
    run(
      workbench.queue({
        ...caller,
        scopeId,
        stage,
        after: last,
        limit: AHEAD + 1 - buffered,
      }),
    )
      .then((items) => {
        if (items.length === 0) setExhausted(true);
        setView((v) => {
          const known = new Set([
            ...v.ahead.map((q) => q.assignment_id),
            ...v.forward.map((e) => e.item.assignment_id),
            ...(v.current ? [v.current.item.assignment_id] : []),
          ]);
          const queued = [...v.ahead, ...items.filter((i) => !known.has(i.assignment_id))];
          if (v.current) return { ...v, ahead: queued };
          // Nothing on screen yet: show the first record as soon as it arrives.
          const [next, ...rest] = queued;
          return { current: next ? enter(next) : null, forward: v.forward, ahead: rest };
        });
      })
      .catch((e: unknown) =>
        setError(e instanceof WorkbenchError ? e.message : "The queue could not load."),
      )
      .finally(() => {
        loading.current = false;
      });
  }, [workbench, caller, scopeId, stage, exhausted, current, forward, ahead, history]);

  const advance = useCallback((settled: Entry) => {
    setHistory((h) => [...h, settled]);
    if (!timings.current.has(settled.item.assignment_id)) {
      timings.current.set(settled.item.assignment_id, (performance.now() - settled.shownMs) / 1000);
    }
    setStats({
      decided: timings.current.size,
      medianSeconds: median([...timings.current.values()]),
      revealsShown: reveals.current.shown,
      revealsFollowed: reveals.current.followed,
    });
    setView((v) => {
      const [back, ...forward] = v.forward;
      if (back)
        return { current: { ...back, shownMs: performance.now() }, forward, ahead: v.ahead };
      const [next, ...rest] = v.ahead;
      return { current: next ? enter(next) : null, forward, ahead: rest };
    });
  }, []);

  const touch = useCallback(() => {
    setView((v) =>
      v.current && !v.current.firstKeyAt
        ? { ...v, current: { ...v.current, firstKeyAt: new Date().toISOString() } }
        : v,
    );
  }, []);

  const decide = useCallback(
    async (outcome: Outcome, reasonCriterionId: string | null = null) => {
      if (!current || busy) return;
      const decision: Decision = {
        outcome,
        reason_criterion_id: outcome === "exclude" ? reasonCriterionId : null,
        evidence_anchor_ids: [],
        comment: null,
      };
      const revising = current.reveal !== null && !current.settled;
      const now = new Date().toISOString();
      setBusy(true);
      setNotice(null);
      try {
        let etag = current.etag;
        if (current.settled) {
          // A reopened record: take the decision back, then decide afresh.
          etag = (
            await run(
              workbench.undo({
                ...caller,
                assignmentId: current.item.assignment_id,
                expectedEtag: etag,
              }),
            )
          ).etag;
        }
        const result = await run(
          workbench.decide({
            ...caller,
            assignmentId: current.item.assignment_id,
            decision,
            phase: revising ? "final" : "provisional",
            expectedEtag: etag,
            timing: {
              shown_at: current.shownAt,
              first_key_at: current.firstKeyAt ?? now,
              decided_at: now,
              reveal_shown_at: current.revealedAt,
            },
          }),
        );
        const updated: Entry = {
          ...current,
          decision,
          settled: false,
          etag: result.etag,
          firstKeyAt: current.firstKeyAt ?? now,
        };
        if (revising && current.decision && !same(current.decision, decision))
          reveals.current.followed++;
        if (result.phase === "provisional" && result.reveal) {
          reveals.current.shown++;
          const reveal = result.reveal;
          setView((v) => ({
            ...v,
            current: { ...updated, reveal, revealedAt: new Date().toISOString() },
          }));
        } else {
          advance({ ...updated, settled: true });
        }
      } catch (e) {
        setError(
          e instanceof WorkbenchError ? e.message : "The decision was not recorded. Try again.",
        );
      } finally {
        setBusy(false);
      }
    },
    [current, busy, workbench, caller, advance],
  );

  /** Space: keep the decision shown with a reveal, or move on from a reopened record. */
  const keep = useCallback(async () => {
    if (!current || busy) return;
    if (current.decision === null) {
      setNotice("Decide this record first.");
      return;
    }
    if (current.settled) {
      advance(current);
      return;
    }
    await decide(current.decision.outcome, current.decision.reason_criterion_id);
  }, [current, busy, decide, advance]);

  /** Backspace: reopen the previous record. */
  const undo = useCallback(() => {
    if (busy) return;
    if (current?.reveal && !current.settled) {
      setNotice("The AI's reading is already shown. Keep your decision with Space, or revise it.");
      return;
    }
    const previous = history.at(-1);
    if (!previous) {
      setNotice("Nothing to undo in this session.");
      return;
    }
    setHistory((h) => h.slice(0, -1));
    setView((v) => ({
      current: { ...previous, shownMs: performance.now() },
      forward: v.current ? [v.current, ...v.forward] : v.forward,
      ahead: v.ahead,
    }));
    setNotice("Reopened. Decide again, or press Space to keep the decision.");
  }, [busy, current, history]);

  /** Asks the server again after the queue ran dry, e.g. while the AI judge is still reading. */
  const retry = useCallback(() => setExhausted(false), []);

  return {
    current,
    ahead,
    retry,
    previous: history.at(-1) ?? null,
    finished: exhausted && current === null && forward.length === 0 && ahead.length === 0,
    busy,
    notice,
    error,
    stats,
    decide,
    keep,
    undo,
    touch,
    setNotice,
  };
}

const same = (a: Decision, b: Decision) =>
  a.outcome === b.outcome && a.reason_criterion_id === b.reason_criterion_id;
