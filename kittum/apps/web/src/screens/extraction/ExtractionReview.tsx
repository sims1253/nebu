// Extraction review (DESIGN.md, "Extraction"): fields grouped by endpoint, arm, and timepoint
// beside the report. The selected field and its source region share the marker, and Enter
// accepts only once that region has been on screen.
import { useRouteContext } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { Anchor, ExtractionReview, Proposal, ReviewField } from "../../data/schema.ts";
import { run, WorkbenchError } from "../../data/workbench.ts";
import { Key, KeyHint } from "../../ui/Key.tsx";
import { useKeys } from "../../ui/keys.ts";
import { ACCEPTED, Mark, PROPOSAL_STATUS, Status } from "../../ui/Status.tsx";
import { PdfViewer } from "../pdf/PdfViewer.tsx";
import type { PageHighlight, TextSelection } from "../pdf/PdfViewer.tsx";
import { useProject } from "../project.tsx";

/** How long the source must be on screen before Enter accepts. */
export const VIEW_MS = 400;

type Path = "janus" | "llm";
const PATH_NAME: Record<Path, string> = { janus: "Janus", llm: "LLM" };

interface FieldState {
  readonly picked: Path | null;
  readonly accepted: Path | null;
  readonly edited: TextSelection | null;
}

const agreement = (field: ReviewField) => {
  const [a, b] = field.proposals;
  if (!a || !b) return "single" as const;
  return a.value === b.value && a.unit === b.unit ? ("agree" as const) : ("differ" as const);
};

export function ExtractionScreen({ review }: { review: ExtractionReview | null }) {
  if (!review) {
    return (
      <div className="mx-auto max-w-[640px] px-10 py-16">
        <h1 className="text-xl font-semibold">Nothing to review</h1>
        <p className="mt-2 text-md text-ink-2">
          Extraction starts once a full text is included and both extraction paths have run.
        </p>
      </div>
    );
  }
  return <Review review={review} />;
}

function Review({ review }: { review: ExtractionReview }) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { labels } = useProject();
  const anchors = useMemo(() => new Map(review.anchors.map((a) => [a.id, a])), [review.anchors]);
  const [selected, setSelected] = useState(0);
  const [fields, setFields] = useState<Record<string, FieldState>>(() =>
    Object.fromEntries(
      review.fields.map((f) => [f.id, { picked: null, accepted: f.accepted, edited: null }]),
    ),
  );
  const [selection, setSelection] = useState<TextSelection | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const viewed = useRef(new Map<string, number>());
  const visibleSince = useRef<number | null>(null);
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
  const source: Anchor | undefined = shown?.anchor_ids.map((id) => anchors.get(id)).find(Boolean);
  const other =
    field && status === "differ"
      ? field.proposals
          .find((p) => p !== shown)
          ?.anchor_ids.map((id) => anchors.get(id))
          .find(Boolean)
      : undefined;

  const sourceBoxes = state?.edited?.boxes ?? source?.boxes;
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

  // Record how long the selected field's source is on screen.
  const onCurrentVisible = useCallback(
    (visible: boolean) => {
      if (!field) return;
      if (visible) {
        visibleSince.current ??= performance.now();
      } else if (visibleSince.current !== null) {
        viewed.current.set(
          field.id,
          (viewed.current.get(field.id) ?? 0) + performance.now() - visibleSince.current,
        );
        visibleSince.current = null;
      }
    },
    [field],
  );
  useEffect(() => {
    visibleSince.current = null;
    rows.current.get(field?.id ?? "")?.scrollIntoView({ block: "nearest" });
  }, [field?.id]);
  const viewedMs = () =>
    (field ? (viewed.current.get(field.id) ?? 0) : 0) +
    (visibleSince.current === null ? 0 : performance.now() - visibleSince.current);

  const move = (step: number) => {
    setSelected((s) => Math.max(0, Math.min(review.fields.length - 1, s + step)));
    setNotice(null);
  };

  const accept = async () => {
    if (!field || !shown) return;
    if (status === "differ" && !state?.picked && !state?.accepted) {
      setNotice("The paths disagree. Pick one with 1 or 2 after checking both sources.");
      return;
    }
    if (!sourceBoxes) {
      setNotice(
        "This value has no source on the page. It needs a grounding exception, which is not built yet.",
      );
      return;
    }
    const ms = viewedMs();
    if (ms < VIEW_MS) {
      setNotice("Look at the highlighted source before accepting.");
      setNudge((n) => n + 1);
      return;
    }
    try {
      await run(
        workbench.accept({ fieldId: field.id, path: shown.path, sourceViewedMs: Math.round(ms) }),
      );
      setFields((all) => ({ ...all, [field.id]: { ...all[field.id]!, accepted: shown.path } }));
      setNotice(null);
      move(1);
    } catch (e) {
      setNotice(e instanceof WorkbenchError ? e.message : "The value was not accepted. Try again.");
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
    if ((key === "1" || key === "2") && status === "differ") {
      const path = field.proposals[Number(key) - 1]?.path ?? null;
      setFields((all) => ({
        ...all,
        [field.id]: { ...all[field.id]!, picked: path, accepted: null },
      }));
      viewed.current.delete(field.id);
      setNotice(null);
      return true;
    }
    if (key === "e") {
      if (!selection) {
        setNotice("Select the value on the page first; an edit needs a source span.");
        return true;
      }
      setFields((all) => ({
        ...all,
        [field.id]: { ...all[field.id]!, edited: selection, accepted: null },
      }));
      viewed.current.delete(field.id);
      setNotice(`Edited to “${selection.quote.trim()}”. Press Enter to accept.`);
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
      let group = out.find((g) => g.endpoint === f.key.endpoint && g.set === f.key.analysis_set);
      if (!group)
        out.push((group = { endpoint: f.key.endpoint, set: f.key.analysis_set, parts: [] }));
      const title = `${f.key.arm}, ${f.key.timepoint.toLowerCase()}`;
      let part = group.parts.find((p) => p.title === title);
      if (!part) group.parts.push((part = { title, fields: [] }));
      part.fields.push(f);
    }
    return out;
  }, [review.fields]);

  const done = review.fields.filter((f) => fields[f.id]?.accepted).length;

  return (
    <div className="grid h-full grid-cols-[minmax(560px,46%)_minmax(0,1fr)]">
      <section
        className="flex min-h-0 flex-col border-r border-rule-strong bg-sheet"
        aria-label="Extracted fields"
      >
        <header className="flex items-baseline gap-4 border-b border-rule px-5 pt-4 pb-3">
          <div className="min-w-0">
            <p className="text-xs text-ink-3">
              {labels.report} {review.report.study_label}, data cut {field?.key.data_cut}
            </p>
            <h1 className="mt-0.5 truncate text-sm font-semibold" title={review.report.title}>
              {review.report.title}
            </h1>
          </div>
          <span className="ml-auto text-xs whitespace-nowrap text-ink-2">
            {done} of {review.fields.length} accepted
          </span>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto" data-testid="fields">
          <div className="narrow sticky top-0 z-10 grid grid-cols-[minmax(0,1fr)_6.5rem_5.5rem_8.5rem_5.5rem] gap-3 border-b border-rule bg-sheet px-5 py-1.5 text-2xs text-ink-3">
            <span>Field</span>
            <span className="text-right">Value</span>
            <span>Status</span>
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
                      shown={proposal(f, fields[f.id])}
                      selected={f.id === field?.id}
                      onSelect={() => setSelected(review.fields.indexOf(f))}
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
          <div className="flex flex-wrap gap-x-4 gap-y-1.5">
            <KeyHint keys={["Tab"]}>Next field</KeyHint>
            <KeyHint keys={["Enter"]}>Accept</KeyHint>
            <KeyHint keys={["1", "2"]} muted={status !== "differ"}>
              Pick a path
            </KeyHint>
            <KeyHint keys={["e"]} muted={!selection}>
              Edit from selection
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
        label={`${review.report.study_label}, ${review.report.citation}`}
      />
    </div>
  );
}

const describe = (field: ReviewField, shown: Proposal | undefined) => {
  const failed = field.proposals.flatMap((p) =>
    p.validators.filter((v) => !v.passed).map((v) => `${PATH_NAME[p.path]}: ${v.message}`),
  );
  if (failed.length) return failed.join(" ");
  const policy =
    field.spec.policy === "critical"
      ? "Critical: verify against the highlighted source."
      : field.spec.policy === "standard"
        ? "Standard: agreeing, validated values may be accepted after viewing the list."
        : "Descriptive: one review of the value and its span.";
  return shown?.status === "missing" ? `Reported as missing. ${policy}` : policy;
};

function FieldRow({
  field,
  state,
  shown,
  selected,
  onSelect,
  rowRef,
}: {
  field: ReviewField;
  state: FieldState | undefined;
  shown: Proposal | undefined;
  selected: boolean;
  onSelect: () => void;
  rowRef: (el: HTMLElement | null) => void;
}) {
  const status = agreement(field);
  const failed = field.proposals.some((p) => p.validators.some((v) => !v.passed));
  const value = state?.edited ? state.edited.quote.trim() : shown?.value;
  const unit = state?.edited ? field.spec.unit : shown?.unit;
  return (
    <div
      ref={rowRef}
      role="row"
      aria-selected={selected}
      data-testid="field-row"
      onClick={onSelect}
      className={`grid grid-cols-[minmax(0,1fr)_6.5rem_5.5rem_8.5rem_5.5rem] items-center gap-3 px-5 py-[5px] text-sm ${selected ? "evidence-item" : ""}`}
    >
      <span className="truncate">
        {field.spec.description}
        {field.spec.policy !== "critical" ? (
          <span className="ml-2 text-2xs text-ink-3">{field.spec.policy}</span>
        ) : null}
      </span>
      <span className="text-right whitespace-nowrap">
        {value ?? <span className="text-ink-3">empty</span>}
        {value && unit && unit !== "patients" ? (
          <span className="ml-1 text-ink-3">{unit}</span>
        ) : null}
      </span>
      <span>{shown ? <Status term={PROPOSAL_STATUS[shown.status]} /> : null}</span>
      <span className="narrow flex items-center gap-1.5 text-xs whitespace-nowrap">
        {status === "agree" ? (
          <span className="text-ink-2">Both agree</span>
        ) : status === "single" ? (
          <span className="text-ink-3">{PATH_NAME[field.proposals[0]?.path ?? "janus"]} only</span>
        ) : (
          field.proposals.map((p, i) => (
            <span
              key={p.path}
              className={`flex items-center gap-1 ${state?.picked === p.path || state?.accepted === p.path ? "font-semibold text-ink" : "text-unclear"}`}
            >
              <Key k={String(i + 1)} />
              {p.value}
            </span>
          ))
        )}
        {failed ? <Mark term={PROPOSAL_STATUS.invalid} title="A validator failed" /> : null}
      </span>
      <span className="text-xs">
        {state?.accepted ? (
          <Status term={ACCEPTED} />
        ) : (
          <span className="text-ink-3">
            {status === "differ" && !state?.picked ? "Pick a path" : "To verify"}
          </span>
        )}
      </span>
    </div>
  );
}
