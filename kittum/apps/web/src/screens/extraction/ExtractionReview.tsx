// Extraction review (DESIGN.md, "Extraction"): one run's fields grouped by endpoint, arm, and
// timepoint beside the report. The selected field and its source region share the marker, and
// Enter accepts only once that region has been on screen. An edit needs a span: the reviewer
// selects the value on the page, confirms the number Kittum read from it, and accepts.
import { Link, useRouteContext } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ExtractionReview, Proposal, ReviewField } from "../../data/schema.ts";
import { run, WorkbenchError } from "../../data/workbench.ts";
import { Key, KeyHint } from "../../ui/Key.tsx";
import { useKeys } from "../../ui/keys.ts";
import { ACCEPTED, Mark, PROPOSAL_STATUS, Status } from "../../ui/Status.tsx";
import { PdfViewer } from "../pdf/PdfViewer.tsx";
import type { PageHighlight, TextSelection } from "../pdf/PdfViewer.tsx";
import { useProject } from "../project.tsx";

/** How long the source must be on screen before Enter accepts; the server checks it too. */
export const VIEW_MS = 400;

type Path = "janus" | "llm";
const PATH_NAME: Record<Path, string> = { janus: "Janus", llm: "LLM" };

interface FieldState {
  readonly picked: Path | null;
  readonly accepted: Path | null;
  /** The accepted value, when it is the reviewer's own. */
  readonly value: string | null;
  readonly edited: TextSelection | null;
}

/** An edit in progress: the selection and the value the reviewer confirms from it. */
interface Editing {
  readonly selection: TextSelection;
  readonly value: string;
}

const agreement = (field: ReviewField) =>
  field.agreement === "disagree" && field.proposals.length > 1
    ? ("differ" as const)
    : field.agreement === "agree"
      ? ("agree" as const)
      : ("single" as const);

const SINGLE: Record<ReviewField["agreement"], string> = {
  janus_only: "Janus only",
  llm_only: "LLM only",
  neither: "Neither path",
  agree: "Both agree",
  disagree: "The paths disagree",
};

/** The first number in a selection, as the value an edit proposes. */
const firstNumber = (text: string) => /[-−–]?\d+(?:[.,·]\d+)*/u.exec(text)?.[0] ?? text.trim();

const failedChecks = (p: Proposal | undefined) =>
  (p?.validators ?? []).filter((v) => !v.passed).map((v) => v.message ?? v.validator);

export function ExtractionScreen({ review }: { review: ExtractionReview }) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me, labels } = useProject();
  const anchors = useMemo(() => new Map(review.anchors.map((a) => [a.id, a])), [review.anchors]);
  const [selected, setSelected] = useState(0);
  const [fields, setFields] = useState<Record<string, FieldState>>(() =>
    Object.fromEntries(
      review.fields.map((f) => [
        f.id,
        {
          picked: null,
          accepted: f.accepted,
          value: f.acceptance?.edited ? f.acceptance.value : null,
          edited: null,
        },
      ]),
    ),
  );
  const [selection, setSelection] = useState<TextSelection | null>(null);
  const [editing, setEditing] = useState<Editing | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // On-screen time per source identity (field, path, and anchor or edited span), so time spent on
  // one source never counts for another.
  const viewed = useRef(new Map<string, number>());
  const visible = useRef<{ key: string; since: number } | null>(null);
  // On-screen time of each row that may be accepted as a group: only rows the reviewer has had
  // in view for the floor go into a group, with the time they were seen.
  const rowsSeen = useRef(new Map<string, { ms: number; since: number | null }>());
  const list = useRef<HTMLDivElement>(null);
  const [nudge, setNudge] = useState(0);
  const rows = useRef(new Map<string, HTMLElement>());

  const field = review.fields[selected];
  const state = field ? fields[field.id] : undefined;
  const status = field ? agreement(field) : "single";
  const proposal = (f: ReviewField, s: FieldState | undefined): Proposal | undefined => {
    const chosen = s?.accepted ?? s?.picked;
    return (
      f.proposals.find((p) => p.path === chosen) ??
      f.proposals.find((p) => p.anchor_ids.length > 0) ??
      f.proposals[0]
    );
  };
  const shown = field ? proposal(field, state) : undefined;
  const source = shown?.anchor_ids.map((id) => anchors.get(id)).find(Boolean);
  const other =
    field && status === "differ"
      ? field.proposals
          .find((p) => p !== shown)
          ?.anchor_ids.map((id) => anchors.get(id))
          .find(Boolean)
      : undefined;

  const edited = editing?.selection ?? state?.edited ?? null;
  const sourceBoxes = edited?.boxes ?? source?.boxes;
  const highlights = useMemo((): PageHighlight[] => {
    const out: PageHighlight[] = [];
    if (other && other.id !== source?.id) {
      out.push({
        id: `other-${other.id}`,
        boxes: other.boxes,
        kind: "adopted",
        label: "The other path's source",
      });
    }
    if (sourceBoxes)
      out.push({ id: `source-${field?.id}`, boxes: sourceBoxes, kind: "current", label: "Source" });
    return out;
  }, [other, source?.id, sourceBoxes, field?.id]);
  const firstBox = sourceBoxes?.[0];
  const target =
    field && firstBox ? { key: `${field.id}-${shown?.path}-${nudge}`, box: firstBox } : null;

  const sourceKey = field
    ? `${field.id}|${shown?.path}|${edited ? `edit:${edited.start}-${edited.end}` : (source?.id ?? "none")}`
    : "";

  // Record how long the selected source is on screen. A new identity gets a new callback, so the
  // viewer re-observes and reports afresh; reports for an old identity are ignored. The time runs
  // only while the page is visible. On its return it waits for the viewer's fresh report (the
  // viewer forgets what it saw while the page was hidden), never resuming on what it last said.
  const onCurrentVisible = useCallback(
    (isVisible: boolean) => {
      const now = performance.now();
      const open = visible.current;
      if (isVisible) {
        if (open?.key !== sourceKey && !document.hidden)
          visible.current = { key: sourceKey, since: now };
      } else if (open?.key === sourceKey) {
        viewed.current.set(sourceKey, (viewed.current.get(sourceKey) ?? 0) + now - open.since);
        visible.current = null;
      }
    },
    [sourceKey],
  );
  useEffect(() => {
    const onVisibility = () => {
      const open = visible.current;
      if (!document.hidden || !open) return;
      viewed.current.set(
        open.key,
        (viewed.current.get(open.key) ?? 0) + performance.now() - open.since,
      );
      visible.current = null;
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);
  useEffect(() => {
    rows.current.get(field?.id ?? "")?.scrollIntoView({ block: "nearest" });
  }, [field?.id]);
  const viewedMs = () =>
    (viewed.current.get(sourceKey) ?? 0) +
    (visible.current?.key === sourceKey ? performance.now() - visible.current.since : 0);

  const move = (step: number) => {
    setSelected((s) => Math.max(0, Math.min(review.fields.length - 1, s + step)));
    setEditing(null);
    setNotice(null);
  };

  const accept = async ({ override = false }: { override?: boolean } = {}) => {
    if (!field || !shown || busy) return;
    if (state?.accepted) {
      setNotice("This value is accepted already.");
      return;
    }
    if (status === "differ" && !state?.picked && !editing) {
      setNotice("The paths disagree. Pick one with 1 or 2 after checking both sources.");
      return;
    }
    if (!sourceBoxes) {
      setNotice(
        "This value has no source on the page. Select it on the page and press e to edit it with its span; a grounding exception is not built yet.",
      );
      return;
    }
    const failed = editing ? [] : failedChecks(shown);
    if (failed.length && !override) {
      setNotice(
        `A check failed: ${failed.join(" ")} Confirm the value against the source and press o to accept it anyway, or edit it.`,
      );
      return;
    }
    const ms = viewedMs();
    if (ms < VIEW_MS) {
      setNotice("Look at the highlighted source before accepting.");
      setNudge((n) => n + 1);
      return;
    }
    setBusy(true);
    try {
      const acceptance = await run(
        workbench.accept({
          projectId: project.id,
          as: me.actor_id,
          fieldId: field.id,
          path: shown.path,
          edit: editing
            ? {
                value: editing.value.trim(),
                start: editing.selection.start,
                end: editing.selection.end,
                quote: editing.selection.quote,
                boxes: editing.selection.boxes,
              }
            : null,
          sourceViewedMs: Math.round(ms),
          override,
        }),
      );
      setFields((all) => ({
        ...all,
        [field.id]: {
          ...all[field.id]!,
          accepted: acceptance.path,
          value: acceptance.edited ? acceptance.value : null,
          edited: editing?.selection ?? null,
        },
      }));
      setEditing(null);
      setNotice(null);
      move(1);
    } catch (e) {
      setNotice(e instanceof WorkbenchError ? e.message : "The value was not accepted. Try again.");
    } finally {
      setBusy(false);
    }
  };

  // Agreeing, validated standard fields may be accepted together, each once it was on screen.
  const groupable = review.fields.filter(
    (f) => f.gate === "group_accept" && !fields[f.id]?.accepted,
  );
  const groupKey = groupable.map((f) => f.id).join(",");
  useEffect(() => {
    const root = list.current;
    if (!root || !groupKey) return;
    const seen = rowsSeen.current;
    // A row's time runs only while the page is visible and a fresh report says it is wholly in
    // the list's view. On the page's return every row waits for a report computed after it: a
    // report from before (the rows may have scrolled away meanwhile) starts nothing.
    let shownSince = document.hidden ? Infinity : 0;
    const start = (id: string, now: number) => {
      const row = seen.get(id) ?? { ms: 0, since: null };
      if (row.since === null) seen.set(id, { ...row, since: now });
    };
    const stop = (id: string, now: number) => {
      const row = seen.get(id);
      if (row && row.since !== null) seen.set(id, { ms: row.ms + now - row.since, since: null });
    };
    const observer = new IntersectionObserver(
      (entries) => {
        const now = performance.now();
        for (const entry of entries) {
          const id = (entry.target as HTMLElement).dataset["fieldId"] ?? "";
          const whole = entry.isIntersecting && entry.intersectionRatio >= 0.99;
          if (!whole) stop(id, now);
          else if (!document.hidden && entry.time >= shownSince) start(id, now);
        }
      },
      { root, threshold: [0, 0.99, 1] },
    );
    const tracked = () =>
      groupKey
        .split(",")
        .map((id) => rows.current.get(id))
        .filter((el): el is HTMLElement => el !== undefined);
    const onVisibility = () => {
      const now = performance.now();
      if (document.hidden) {
        shownSince = Infinity;
        for (const id of groupKey.split(",")) stop(id, now);
        return;
      }
      // Observing afresh makes the observer report each row as it is now.
      shownSince = now;
      for (const el of tracked()) {
        observer.unobserve(el);
        observer.observe(el);
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    for (const id of groupKey.split(",")) {
      const el = rows.current.get(id);
      if (el) observer.observe(el);
    }
    return () => {
      observer.disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [groupKey]);
  const rowViewedMs = (id: string) => {
    const row = rowsSeen.current.get(id);
    if (!row) return 0;
    return row.ms + (row.since !== null ? performance.now() - row.since : 0);
  };
  const acceptGroup = async () => {
    if (!groupable.length || busy) return;
    const seen = groupable.filter((f) => rowViewedMs(f.id) >= VIEW_MS);
    if (!seen.length) {
      setNotice(
        "None of the agreeing standard fields has been on screen long enough. Scroll to them and look them over first.",
      );
      return;
    }
    const ms = Math.round(Math.min(...seen.map((f) => rowViewedMs(f.id))));
    const unseen = groupable.length - seen.length;
    setBusy(true);
    try {
      const accepted = await run(
        workbench.groupAccept({
          projectId: project.id,
          as: me.actor_id,
          runId: review.run.id,
          fieldIds: seen.map((f) => f.id),
          listViewedMs: ms,
        }),
      );
      setFields((all) => {
        const next = { ...all };
        for (const a of accepted) next[a.field_id] = { ...next[a.field_id]!, accepted: a.path };
        return next;
      });
      setNotice(
        `Accepted ${accepted.length} agreeing standard ${accepted.length === 1 ? "field" : "fields"} as a group. A random tenth of them go to a re-check.${
          unseen ? ` ${unseen} more ${unseen === 1 ? "has" : "have"} not been on screen yet.` : ""
        }`,
      );
    } catch (e) {
      setNotice(e instanceof WorkbenchError ? e.message : "The group was not accepted.");
    } finally {
      setBusy(false);
    }
  };

  useKeys((event) => {
    if (!field) return false;
    const key = event.key;
    if (key === "Tab") {
      move(event.shiftKey ? -1 : 1);
      return true;
    }
    if (key === "ArrowDown" || key === "j") {
      move(1);
      return true;
    }
    if (key === "ArrowUp" || key === "k") {
      move(-1);
      return true;
    }
    if (key === "Enter") {
      void accept();
      return true;
    }
    if (key === "o") {
      void accept({ override: true });
      return true;
    }
    if (key === "a") {
      void acceptGroup();
      return true;
    }
    if ((key === "1" || key === "2") && status === "differ") {
      const path = field.proposals[Number(key) - 1]?.path ?? null;
      setFields((all) => ({ ...all, [field.id]: { ...all[field.id]!, picked: path } }));
      setNotice(null);
      return true;
    }
    if (key === "e") {
      if (state?.accepted) {
        setNotice("This value is accepted already.");
        return true;
      }
      if (!selection) {
        setNotice("Select the value on the page first; an edit needs a source span.");
        return true;
      }
      setEditing({ selection, value: firstNumber(selection.quote) });
      setNotice(`Edited to “${firstNumber(selection.quote)}”. Press Enter to accept.`);
      return true;
    }
    return false;
  });

  const groups = useMemo(() => {
    const out: Array<{
      endpoint: string;
      set: string;
      parts: Array<{ title: string; fields: ReviewField[] }>;
    }> = [];
    for (const f of review.fields) {
      let group = out.find((g) => g.endpoint === f.labels.endpoint && g.set === f.key.analysis_set);
      if (!group)
        out.push((group = { endpoint: f.labels.endpoint, set: f.key.analysis_set, parts: [] }));
      const title = `${f.labels.arm}, ${f.labels.timepoint.toLowerCase()}`;
      let part = group.parts.find((p) => p.title === title);
      if (!part) group.parts.push((part = { title, fields: [] }));
      part.fields.push(f);
    }
    return out;
  }, [review.fields]);

  const done = review.fields.filter((f) => fields[f.id]?.accepted).length;

  return (
    <div className="grid h-full grid-cols-[minmax(600px,47%)_minmax(0,1fr)]">
      <section
        className="flex min-h-0 flex-col border-r border-rule-strong bg-sheet"
        aria-label="Extracted fields"
      >
        <header className="flex items-baseline gap-4 border-b border-rule px-5 pt-4 pb-3">
          <div className="min-w-0">
            <p className="text-xs text-ink-3">
              <Link
                to="/p/$projectId/extraction"
                params={{ projectId: project.id }}
                className="text-petrol hover:underline"
              >
                Runs
              </Link>{" "}
              {labels.report} {review.report.study_label}, data cut {field?.key.data_cut}
            </p>
            <h1 className="mt-0.5 truncate text-sm font-semibold" title={review.report.title}>
              {review.report.title}
            </h1>
          </div>
          <span
            className="ml-auto text-xs whitespace-nowrap text-ink-2"
            data-testid="accepted-count"
          >
            {done} of {review.fields.length} accepted
          </span>
        </header>
        <div ref={list} className="min-h-0 flex-1 overflow-y-auto" data-testid="fields">
          <div className="narrow sticky top-0 z-10 grid grid-cols-[minmax(0,1fr)_7.5rem_8.5rem_5.5rem] gap-3 border-b border-rule bg-sheet px-5 py-1.5 text-2xs text-ink-3">
            <span>Field</span>
            <span className="text-right">Value</span>
            <span>Paths</span>
            <span>Review</span>
          </div>
          {groups.map((group) => (
            <section key={`${group.endpoint}-${group.set}`} className="border-b border-rule pb-1">
              <h2 className="flex items-baseline gap-3 px-5 pt-3 pb-1 text-sm font-semibold">
                {group.endpoint}
                <span className="text-xs font-normal text-ink-3">{group.set}</span>
              </h2>
              {group.parts.map((part) => (
                <div key={part.title}>
                  <h3 className="px-5 pt-1.5 pb-0.5 text-xs text-ink-2">{part.title}</h3>
                  {part.fields.map((f) => (
                    <FieldRow
                      key={f.id}
                      field={f}
                      state={fields[f.id]}
                      editing={f.id === field?.id ? editing : null}
                      shown={proposal(f, fields[f.id])}
                      selected={f.id === field?.id}
                      onSelect={() => {
                        setSelected(review.fields.indexOf(f));
                        setEditing(null);
                      }}
                      rowRef={(el) => {
                        if (el) rows.current.set(f.id, el);
                        else rows.current.delete(f.id);
                      }}
                    />
                  ))}
                </div>
              ))}
            </section>
          ))}
        </div>
        <footer className="border-t border-rule-strong bg-sheet-2 px-5 py-2.5 text-sm">
          {editing ? (
            <label className="mb-2 flex items-center gap-3">
              <span className="text-xs font-semibold text-ink-2">Value from your selection</span>
              <input
                autoFocus
                aria-label="Edited value"
                value={editing.value}
                onChange={(e) => setEditing({ ...editing, value: e.currentTarget.value })}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void accept();
                  } else if (e.key === "Escape") {
                    setEditing(null);
                    setNotice("Edit dropped.");
                  }
                }}
                className="w-32 rounded-[3px] border border-petrol bg-sheet px-2 py-0.5 text-sm outline-none"
              />
              <span className="min-w-0 truncate text-xs text-ink-3" title={editing.selection.quote}>
                from “{editing.selection.quote.trim()}”
              </span>
            </label>
          ) : null}
          <div className="flex flex-wrap gap-x-4 gap-y-1.5">
            <KeyHint keys={["Tab"]}>Next field</KeyHint>
            <KeyHint keys={["Enter"]}>Accept</KeyHint>
            <KeyHint keys={["1", "2"]} muted={status !== "differ"}>
              Pick a path
            </KeyHint>
            <KeyHint keys={["e"]} muted={!selection}>
              Edit from selection
            </KeyHint>
            <KeyHint keys={["o"]} muted={!failedChecks(shown).length}>
              Accept despite a failed check
            </KeyHint>
            <KeyHint keys={["a"]} muted={!groupable.length}>
              Accept {groupable.length || "no"} agreeing standard fields
            </KeyHint>
          </div>
          <p
            className="mt-1.5 min-h-5 text-xs text-ink-2"
            aria-live="polite"
            data-testid="extraction-notice"
          >
            {notice ?? (field ? describe(field, shown) : "")}
          </p>
        </footer>
      </section>
      <PdfViewer
        workbench={workbench}
        layer={review.layer}
        highlights={highlights}
        target={target}
        onSelection={setSelection}
        onCurrentVisible={onCurrentVisible}
        label={`${review.report.study_label}, ${review.report.citation || review.report.licence}`}
      />
    </div>
  );
}

const describe = (field: ReviewField, shown: Proposal | undefined) => {
  const failed = field.proposals.flatMap((p) =>
    failedChecks(p).map((message) => `${PATH_NAME[p.path]}: ${message}`),
  );
  if (failed.length) return failed.join(" ");
  const policy =
    field.spec.policy === "critical"
      ? "Critical: verify against the highlighted source."
      : field.spec.policy === "standard"
        ? "Standard: agreeing, validated values may be accepted together after viewing the list."
        : "Descriptive: one review of the value and its span.";
  const why = shown?.message ? ` ${shown.message}.` : "";
  return shown && shown.status !== "extracted"
    ? `${PROPOSAL_STATUS[shown.status].label}.${why} ${policy}`
    : policy;
};

function FieldRow({
  field,
  state,
  editing,
  shown,
  selected,
  onSelect,
  rowRef,
}: {
  field: ReviewField;
  state: FieldState | undefined;
  editing: Editing | null;
  shown: Proposal | undefined;
  selected: boolean;
  onSelect: () => void;
  rowRef: (el: HTMLElement | null) => void;
}) {
  const status = agreement(field);
  const failed = field.proposals.some((p) => failedChecks(p).length > 0);
  const own = editing?.value ?? state?.value ?? null;
  const value = own ?? (shown?.status === "extracted" ? shown.value : null);
  const unit = own ? field.spec.unit : shown?.unit;
  return (
    <div
      ref={rowRef}
      role="row"
      aria-selected={selected}
      data-testid="field-row"
      data-field-id={field.id}
      onClick={onSelect}
      className={`grid grid-cols-[minmax(0,1fr)_7.5rem_8.5rem_5.5rem] items-center gap-3 px-5 py-[5px] text-sm ${selected ? "evidence-item" : ""}`}
    >
      <span className="truncate">
        {field.spec.description}
        {field.spec.policy !== "critical" ? (
          <span className="ml-2 text-2xs text-ink-3">{field.spec.policy}</span>
        ) : null}
      </span>
      <span className="text-right whitespace-nowrap" data-testid="field-value">
        {value ??
          (shown ? (
            <Status term={PROPOSAL_STATUS[shown.status]} />
          ) : (
            <span className="text-ink-3">Empty</span>
          ))}
        {value && unit ? <span className="ml-1 text-ink-3">{unit}</span> : null}
      </span>
      <span className="narrow flex items-center gap-1.5 text-xs whitespace-nowrap">
        {status === "differ" ? (
          field.proposals.map((p, i) => (
            <span
              key={p.path}
              className={`flex items-center gap-1 ${state?.picked === p.path || state?.accepted === p.path ? "font-semibold text-ink" : "text-unclear"}`}
            >
              <Key k={String(i + 1)} />
              {p.value ?? PROPOSAL_STATUS[p.status].label}
              {/* Same number, different unit: the unit is the disagreement. */}
              {field.proposals.some((q) => q.unit !== p.unit) && p.unit ? ` ${p.unit}` : ""}
            </span>
          ))
        ) : (
          <span className={status === "agree" ? "text-ink-2" : "text-ink-3"}>
            {SINGLE[field.agreement]}
          </span>
        )}
        {failed ? <Mark term={PROPOSAL_STATUS.invalid} title="A validator failed" /> : null}
      </span>
      <span className="text-xs">
        {state?.accepted ? (
          <Status term={ACCEPTED}>{state.value !== null ? "Edited" : "Accepted"}</Status>
        ) : (
          <span className="text-ink-3">
            {status === "differ" && !state?.picked ? "Pick a path" : "To verify"}
          </span>
        )}
      </span>
    </div>
  );
}
