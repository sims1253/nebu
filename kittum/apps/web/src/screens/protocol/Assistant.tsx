// Writing the protocol in a form (DESIGN.md, "Protocol"). The lead fills in the questions, the
// criteria, the sources and their queries in place, or asks a model for a draft of the whole
// protocol or for changes to one part. Every save, draft, suggestion, accepted selection, and
// restore is a revision in one history, with its author: a person or a model. `,` and `.` step
// through the history without changing anything; `r` restores the revision on screen as a new
// one. Fields a model wrote carry an AI mark that names the model until a person changes them, and
// the publication records them for the AI disclosure. Hit counts run no search.
import { useRouteContext, useRouter } from "@tanstack/react-router";
import { useCallback, useEffect, useEffectEvent, useMemo, useState } from "react";
import type { ReactNode } from "react";

import type {
  AiMark,
  AssistModel,
  AssistOutcome,
  ProtocolDiff,
  ProtocolIssue,
  ProtocolRevision,
  QueryCount,
  QueryDialect,
  SuggestedChange,
} from "../../data/schema.ts";
import { run, WorkbenchError } from "../../data/workbench.ts";
import { Button, Field, Select, TextArea, TextInput, failure } from "../../ui/Form.tsx";
import { Key, KeyHint } from "../../ui/Key.tsx";
import { useKeys } from "../../ui/keys.ts";
import { useProject } from "../project.tsx";
import {
  blankDoc,
  CTGOV,
  dialectOf,
  docOf,
  forSaving,
  freshId,
  marksOn,
  partName,
  PUBMED,
  SOURCE_NAME,
  strategyPath,
  valueAt,
} from "./document.ts";
import type { Criterion, Doc, Localized, Question, Source, Topic } from "./document.ts";

/** An unsaved protocol in the form: edited from `base`, or new when `base` is null. */
export interface Working {
  readonly doc: Doc;
  readonly base: ProtocolRevision | null;
}

const KIND: Record<ProtocolRevision["kind"], string> = {
  draft: "Draft",
  suggestion: "Suggested changes",
  accepted: "Changes accepted",
  edit: "Written by hand",
  restore: "Restored",
};

const SLOTS: Record<string, string> = {
  population: "Population",
  intervention: "Intervention",
  comparator: "Comparator",
  outcome: "Outcome",
  study_type: "Study type",
  other: "Other",
};

const STAGES: ReadonlyArray<readonly [string, string]> = [
  ["title_abstract", "Title and abstract"],
  ["full_text", "Full text"],
];

const MODES: ReadonlyArray<readonly [string, string]> = [
  ["M1", "M1, two people screen every record and the AI is a third screener"],
  ["M2", "M2, one person screens every record and the AI is the second screener"],
  ["M3", "M3, two people screen in the AI's order and stop early"],
];

/** "45 s", "2 min 5 s". */
const elapsed = (seconds: number) =>
  seconds < 60 ? `${seconds} s` : `${Math.floor(seconds / 60)} min ${seconds % 60} s`;

function useElapsed(running: boolean) {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    if (!running) return;
    const started = Date.now();
    const timer = window.setInterval(
      () => setSeconds(Math.floor((Date.now() - started) / 1000)),
      500,
    );
    return () => {
      window.clearInterval(timer);
      setSeconds(0);
    };
  }, [running]);
  return seconds;
}

/** Who wrote a revision: its model, or the person who saved it. */
const authorOf = (r: ProtocolRevision, people: ReadonlyMap<string, string>) =>
  r.model ? r.model.recipe_id : (people.get(r.actor_id) ?? "Someone");

/** A problem the server found, in the words a person reads. */
const plainIssue = (message: string) =>
  /at least 1 character|needs text in the default language/.test(message)
    ? "This field is empty."
    : message;

/** Where a problem is, in the form's words: "Question 1, population", "Criterion C2". */
const whereIn = (doc: Doc, path: string) => {
  const q = /^questions\[(\d+)\](?:\.(label|population|intervention|comparators\[(\d+)\]))?/.exec(
    path,
  );
  if (q) {
    const n = Number(q[1]) + 1;
    if (q[3] !== undefined) return `Question ${n}, comparator ${Number(q[3]) + 1}`;
    return q[2] ? `Question ${n}, ${q[2] === "label" ? "question" : q[2]}` : `Question ${n}`;
  }
  const o = /^outcomes\[(\d+)\]/.exec(path);
  if (o) return `Outcome ${Number(o[1]) + 1}`;
  const t =
    /^search_topics\[(\d+)\](?:\.(criteria|source_plan\.sources)\[(\d+)\](?:\.(\w+))?)?/.exec(path);
  const topic = t ? doc.search_topics[Number(t[1])] : undefined;
  if (t && topic) {
    if (t[2] === "criteria")
      return `Criterion ${topic.criteria[Number(t[3])]?.id ?? Number(t[3]) + 1}`;
    if (t[2]) {
      const source = topic.source_plan.sources[Number(t[3])];
      const name = source?.name || `source ${Number(t[3]) + 1}`;
      return t[4] === "strategy" ? `${name}, query` : name;
    }
    return path.includes(".criteria") ? "Eligibility criteria" : "Search topic";
  }
  return path === "title" || path.startsWith("title.") ? "Title" : path || "The protocol";
};

/** The issues at a field, by the index path the compiler gives them (`questions[0].label.en`). */
const issuesAt = (issues: ReadonlyArray<ProtocolIssue>, at: string) =>
  issues.filter((i) => i.path === at || i.path.startsWith(`${at}.`) || i.path.startsWith(`${at}[`));

type Counted =
  | { readonly state: "counting" }
  | { readonly state: "done"; readonly count: QueryCount }
  | { readonly state: "failed"; readonly message: string };

type Mutable<T> = { -readonly [K in keyof T]: T[K] };

export function Assistant({
  handover,
  onYaml,
  onCurrent,
  onPublished,
}: {
  /** A protocol from the YAML view, not saved yet. */
  handover: Working | null;
  onYaml: (doc: Doc, base: ProtocolRevision | null) => void;
  /** What the form shows now, so the YAML view opens the same protocol. */
  onCurrent: (current: Working | null) => void;
  onPublished: (message: string) => void;
}) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const router = useRouter();
  const { project, me, protocol } = useProject();
  const caller = useMemo(
    () => ({ projectId: project.id, as: me.actor_id }),
    [project.id, me.actor_id],
  );
  const people = useMemo(
    () => new Map(project.members.map((m) => [m.actor_id, m.display_name])),
    [project.members],
  );
  const [revisions, setRevisions] = useState<ReadonlyArray<ProtocolRevision> | null>(null);
  const [at, setAt] = useState(0);
  const [working, setWorking] = useState<Working | null>(handover);
  const [issues, setIssues] = useState<ReadonlyArray<ProtocolIssue>>([]);
  const [saving, setSaving] = useState(false);
  const [model, setModel] = useState<AssistModel | null>(null);
  const [drafting, setDrafting] = useState(false);
  const [describing, setDescribing] = useState(false);
  const [failed, setFailed] = useState<AssistOutcome | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [counts, setCounts] = useState<ReadonlyMap<string, Counted>>(new Map());

  const reload = useCallback(
    async (select?: string) => {
      const found = await run(workbench.assistRevisions(caller));
      setRevisions(found);
      const index = select ? found.findIndex((r) => r.id === select) : found.length - 1;
      setAt(Math.max(0, index));
    },
    [workbench, caller],
  );

  useEffect(() => {
    let live = true;
    run(workbench.assistRevisions(caller)).then(
      (found) => {
        if (!live) return;
        setRevisions(found);
        setAt(Math.max(0, found.length - 1));
      },
      (e) => live && setError(failure(e, "Kittum could not load the revisions.")),
    );
    run(workbench.assistModel(caller)).then(
      (found) => live && setModel(found),
      () => live && setModel({ configured: false }),
    );
    return () => {
      live = false;
    };
  }, [workbench, caller]);

  const count = useCallback(
    (dialect: QueryDialect, strategy: string) => {
      const key = `${dialect}|${strategy}`;
      setCounts((c) => new Map(c).set(key, { state: "counting" }));
      run(workbench.countHits({ ...caller, dialect, strategy })).then(
        (found) => setCounts((c) => new Map(c).set(key, { state: "done", count: found })),
        (e) =>
          setCounts((c) =>
            new Map(c).set(key, {
              state: "failed",
              message:
                e instanceof WorkbenchError && e.message
                  ? e.message
                  : `Kittum could not count the hits in ${SOURCE_NAME[dialect]}.`,
            }),
          ),
      );
    },
    [workbench, caller],
  );
  const countOf = (dialect: QueryDialect, strategy: string) => counts.get(`${dialect}|${strategy}`);

  const current = revisions?.[at] ?? null;
  const newest = revisions ? at === revisions.length - 1 : true;
  const shown: Working | null =
    working ?? (current ? { doc: docOf(current), base: current } : null);
  useEffect(
    () => onCurrent(working ?? (current ? { doc: docOf(current), base: current } : null)),
    [onCurrent, working, current],
  );

  const step = (by: number) => {
    if (!revisions?.length || working) return;
    setAt((i) => Math.min(revisions.length - 1, Math.max(0, i + by)));
    setError(null);
  };

  const act = async (work: () => Promise<string | undefined>, what: string) => {
    setError(null);
    try {
      const select = await work();
      await reload(select);
    } catch (e) {
      setError(failure(e, what));
    }
  };

  const restore = () =>
    current &&
    act(
      async () => (await run(workbench.restoreRevision({ ...caller, revisionId: current.id }))).id,
      "Kittum could not restore the revision.",
    );

  const edit = (doc: Doc) => setWorking({ doc, base: working ? working.base : current });

  const save = async () => {
    if (!working || saving) return;
    const source = forSaving(working.doc);
    setSaving(true);
    setError(null);
    try {
      const report = await run(workbench.validateProtocol(source));
      if (!report.ok) {
        setIssues(report.issues ?? []);
        return;
      }
      const saved = await run(
        workbench.saveRevision({ ...caller, source, baseId: working.base?.id ?? null }),
      );
      setWorking(null);
      setIssues([]);
      await reload(saved.id);
    } catch (e) {
      setError(failure(e, "Kittum could not save the revision."));
    } finally {
      setSaving(false);
    }
  };

  const discard = () => {
    setWorking(null);
    setIssues([]);
    setError(null);
  };

  // Ctrl+Enter saves the form from anywhere on the screen, a field included.
  const onSaveKey = useEffectEvent((event: KeyboardEvent) => {
    if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey) || !working) return;
    event.preventDefault();
    void save();
  });
  useEffect(() => {
    window.addEventListener("keydown", onSaveKey);
    return () => window.removeEventListener("keydown", onSaveKey);
  }, []);

  useKeys((event) => {
    if (drafting || describing || !current || working) return false;
    if (event.key === ",") return (step(-1), true);
    if (event.key === ".") return (step(1), true);
    if (event.key === "r" && !newest) return (void restore(), true);
    return false;
  });

  const draft = async (description: string) => {
    setDrafting(true);
    setFailed(null);
    setError(null);
    try {
      const outcome = await run(workbench.draftProtocol({ ...caller, description }));
      if (outcome.revision) {
        setDescribing(false);
        setWorking(null);
        await reload(outcome.revision.id);
      } else setFailed(outcome);
    } catch (e) {
      setError(failure(e, "The model did not answer."));
    } finally {
      setDrafting(false);
    }
  };

  const fromPublished = async () => {
    setError(null);
    try {
      const parsed = await run(
        workbench.parseProtocol(await run(workbench.protocolSource(project.id))),
      );
      if (parsed.source) setWorking({ doc: parsed.source as unknown as Doc, base: null });
    } catch (e) {
      setError(failure(e, "Kittum could not load the published version."));
    }
  };

  if (revisions === null) {
    return <p className="p-8 text-sm text-ink-2">{error ?? "Loading the revisions."}</p>;
  }
  if (drafting || failed || describing) {
    return (
      <DraftPrompt
        busy={drafting}
        failed={failed}
        error={error}
        model={model}
        onDraft={draft}
        onCancel={() => {
          setFailed(null);
          setDescribing(false);
        }}
      />
    );
  }
  if (!shown) {
    return (
      <Start
        model={model}
        published={protocol !== null}
        error={error}
        onBlank={() => setWorking({ doc: blankDoc(), base: null })}
        onPublished={() => void fromPublished()}
        onDescribe={() => setDescribing(true)}
      />
    );
  }

  return (
    <div className="grid h-full min-h-0 grid-cols-[210px_minmax(0,1fr)_minmax(360px,32%)]">
      <Rail
        revisions={revisions}
        at={at}
        people={people}
        locked={working !== null}
        onSelect={(i) => {
          if (working) return;
          setAt(i);
          setError(null);
        }}
        onDescribe={model?.configured ? () => setDescribing(true) : null}
      />
      <FormView
        key={working ? "working" : (current?.id ?? "new")}
        working={working}
        shown={shown}
        revisions={revisions}
        current={current}
        newest={newest}
        people={people}
        model={model}
        issues={issues}
        saving={saving}
        countOf={countOf}
        onCount={count}
        onEdit={edit}
        onSave={() => void save()}
        onDiscard={discard}
        onStep={step}
        onRestore={() => void restore()}
        onNewest={() => setAt(revisions.length - 1)}
        onYaml={() => onYaml(shown.doc, shown.base)}
        onImproved={(outcome) => {
          if (outcome.revision) return reload(outcome.revision.id);
          setError(
            outcome.unchanged
              ? "The model suggests no change to this part."
              : `The model's answer still had problems after ${outcome.attempts} tries. ${(outcome.issues ?? []).map((i) => `${i.path}: ${i.message}`).join(". ")}`,
          );
          return Promise.resolve();
        }}
        onError={setError}
      />
      <aside
        className="min-h-0 overflow-y-auto border-l border-rule-strong bg-paper px-5 py-5"
        aria-label="Revision details"
      >
        {error ? (
          <p
            role="alert"
            className="mb-4 border-l-2 border-exclude pl-3 text-sm text-exclude"
            data-testid="assist-error"
          >
            {error}
          </p>
        ) : null}
        {working ? (
          <Unsaved
            working={working}
            next={revisions.length + 1}
            issues={issues}
            saving={saving}
            onSave={() => void save()}
            onDiscard={discard}
          />
        ) : current?.kind === "suggestion" && current.suggestion && current.parent_id ? (
          <SuggestionReview
            key={current.id}
            revision={current}
            base={revisions.find((r) => r.id === current.parent_id) ?? null}
            countOf={countOf}
            onCount={count}
            onAccept={(ids) =>
              act(
                async () =>
                  (
                    await run(
                      workbench.acceptChanges({
                        ...caller,
                        suggestionId: current.id,
                        changeIds: ids,
                      }),
                    )
                  ).id,
                "Kittum could not accept the changes.",
              )
            }
            onReject={() => setAt(revisions.findIndex((r) => r.id === current.parent_id))}
          />
        ) : current ? (
          <>
            <Provenance revision={current} revisions={revisions} people={people} />
            <Compare key={current.id} revision={current} revisions={revisions} />
            <Publish
              revision={current}
              onPublish={async (reason) => {
                const stored = await run(
                  workbench.publishRevision({ ...caller, revisionId: current.id, reason }),
                );
                await router.invalidate();
                const written = current.ai_parts?.length ?? 0;
                onPublished(
                  `Published version ${stored.version.protocol.version_label} from revision ${current.seq}. The ledger records your approval` +
                    (written
                      ? ` and the ${written} ${written === 1 ? "field" : "fields"} a model wrote.`
                      : "."),
                );
              }}
            />
          </>
        ) : null}
      </aside>
    </div>
  );
}

function Start({
  model,
  published,
  error,
  onBlank,
  onPublished,
  onDescribe,
}: {
  model: AssistModel | null;
  published: boolean;
  error: string | null;
  onBlank: () => void;
  onPublished: () => void;
  onDescribe: () => void;
}) {
  const choice =
    "grid w-full gap-1 rounded-[3px] border border-rule-strong bg-sheet px-4 py-3 text-left hover:border-petrol focus-visible:border-petrol";
  return (
    <section className="h-full overflow-y-auto bg-paper">
      <div className="mx-auto max-w-[720px] px-8 pt-12 pb-16">
        <h1 className="text-xl font-semibold">Write the protocol</h1>
        <p className="mt-2 max-w-[62ch] text-md text-ink-2">
          Each time you save, Kittum adds a revision to the history with your name on it. You
          publish the revision you want.
        </p>
        <div className="mt-6 grid gap-3" data-testid="start">
          <button type="button" className={choice} onClick={onBlank}>
            <span className="font-semibold">Fill in the form</span>
            <span className="text-sm text-ink-2">
              Start with empty fields for the question, the criteria, and a PubMed query.
            </span>
          </button>
          {published ? (
            <button type="button" className={choice} onClick={onPublished}>
              <span className="font-semibold">Start from the published version</span>
              <span className="text-sm text-ink-2">
                Change the version every decision reads now.
              </span>
            </button>
          ) : null}
          {model?.configured ? (
            <button type="button" className={choice} onClick={onDescribe}>
              <span className="font-semibold">Ask {model.recipe_id} for a draft</span>
              <span className="text-sm text-ink-2">
                Describe the review in a few sentences. The model drafts every field, and you check
                each one.
              </span>
            </button>
          ) : model ? (
            <p className="text-sm text-ink-3">
              No model is set up for drafts. Set KITTUM_ASSIST_MODEL to ask one.
            </p>
          ) : null}
        </div>
        {error ? (
          <p role="alert" className="mt-5 text-sm text-exclude">
            {error}
          </p>
        ) : null}
      </div>
    </section>
  );
}

const hostOf = (url: string) => {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
};

/** What Kittum waits for while a model writes, and for how long so far. */
function Waiting({
  seconds,
  model,
  what,
}: {
  seconds: number;
  model: AssistModel | null;
  what: string;
}) {
  const name = model?.recipe_id ?? "the model";
  return (
    <div
      role="status"
      className="border-l-2 border-petrol bg-petrol-wash/40 py-2 pr-3 pl-3 text-sm"
      data-testid="waiting"
    >
      <p>
        Waiting for {name}
        {model?.endpoint ? ` at ${hostOf(model.endpoint)}` : ""} to write {what}.{" "}
        <span className="font-semibold tabular-nums" data-testid="waited">
          {elapsed(seconds)}
        </span>{" "}
        so far.
      </p>
      <p className="mt-0.5 text-xs text-ink-2">
        A model that reasons at length can take up to 10 minutes. Kittum then checks the answer and
        sends any problems back to the model, at most twice. You can leave this page open and wait.
      </p>
    </div>
  );
}

function DraftPrompt({
  busy,
  failed,
  error,
  model,
  onDraft,
  onCancel,
}: {
  busy: boolean;
  failed: AssistOutcome | null;
  error: string | null;
  model: AssistModel | null;
  onDraft: (description: string) => void;
  onCancel: () => void;
}) {
  const [text, setText] = useState("");
  const seconds = useElapsed(busy);
  const issues = failed?.issues ?? [];
  return (
    <section className="h-full overflow-y-auto bg-paper">
      <div className="mx-auto max-w-[760px] px-8 pt-12 pb-16">
        <h1 className="text-xl font-semibold">Describe the review</h1>
        <p className="mt-2 max-w-[64ch] text-md text-ink-2">
          Write a few sentences about the question, the people studied, and what is compared with
          what. Name the outcomes, study designs, languages, and dates that count. The model drafts
          the protocol with a PubMed query and a ClinicalTrials.gov query. You check and change
          every field before anyone screens with it.
        </p>
        <form
          className="mt-6 grid gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (!busy && text.trim()) onDraft(text);
          }}
        >
          <TextArea
            aria-label="Description of the review"
            value={text}
            rows={9}
            disabled={busy}
            onChange={(e) => setText(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && text.trim() && !busy) {
                e.preventDefault();
                onDraft(text);
              }
            }}
            placeholder="Does nicotine replacement therapy help adults who smoke to stop, compared with placebo or no treatment? …"
            className="!px-4 !py-3 text-md leading-[25px]"
          />
          <div className="flex items-center gap-4">
            <Button primary type="submit" disabled={busy || !text.trim()}>
              {busy ? "Drafting" : "Draft the protocol"}
              <span className="inline-flex gap-0.5">
                <Key k="Ctrl" />
                <Key k="Enter" />
              </span>
            </Button>
            {!busy ? (
              <button
                type="button"
                onClick={onCancel}
                className="ml-auto text-sm text-ink-2 hover:text-ink"
              >
                Back
              </button>
            ) : null}
          </div>
          {busy ? <Waiting seconds={seconds} model={model} what="a draft of the protocol" /> : null}
        </form>
        {error ? (
          <p role="alert" className="mt-5 text-sm text-exclude">
            {error}
          </p>
        ) : null}
        {failed ? (
          <div className="mt-8 border-t border-rule-strong pt-4" data-testid="draft-failed">
            <h2 className="text-md font-semibold text-exclude">
              The model's answer still had {issues.length}{" "}
              {issues.length === 1 ? "problem" : "problems"} after {failed.attempts} tries
            </h2>
            <p className="mt-1 text-sm text-ink-2">
              Kittum saved no revision. The ledger records each try for the AI disclosure.
            </p>
            <Issues issues={issues} />
            {failed.raw_answer ? (
              <details className="mt-4 text-sm">
                <summary className="cursor-pointer text-ink-2">The model's last answer</summary>
                <pre className="mt-2 max-h-[320px] overflow-auto bg-sheet p-3 text-[12px] leading-[18px] whitespace-pre-wrap">
                  {failed.raw_answer}
                </pre>
              </details>
            ) : null}
          </div>
        ) : null}
      </div>
    </section>
  );
}

function Issues({ issues, doc }: { issues: ReadonlyArray<ProtocolIssue>; doc?: Doc }) {
  return (
    <ol className="mt-3 grid gap-2.5">
      {issues.map((issue, i) => (
        <li key={i} className="border-l-2 border-exclude pl-3 text-sm" data-testid="protocol-issue">
          {issue.path ? (
            <span className="block text-xs font-semibold break-words text-ink-2">
              {doc ? whereIn(doc, issue.path) : issue.path}
            </span>
          ) : null}
          {plainIssue(issue.message)}
        </li>
      ))}
    </ol>
  );
}

/** The small solid mark on what a model wrote. Pointing at it or focusing it names the model. */
function AiTag({ label }: { label: string }) {
  return (
    <button
      type="button"
      aria-label={label}
      data-testid="ai-mark"
      className="group/ai relative inline-flex cursor-help rounded-[2px] bg-petrol px-1 text-[10.5px] leading-[15px] font-semibold tracking-[0.02em] text-petrol-ink outline-offset-2 focus-visible:outline-2 focus-visible:outline-petrol"
    >
      AI
      <span
        role="tooltip"
        className="pointer-events-none invisible absolute top-full left-0 z-20 mt-1 w-max max-w-[300px] rounded-[3px] border border-rule-strong bg-sheet px-2 py-1 text-xs font-normal tracking-normal text-ink shadow-[0_6px_16px_rgb(0_0_0/0.14)] group-hover/ai:visible group-focus/ai:visible"
      >
        {label}
      </span>
    </button>
  );
}

/** Who wrote a field, as the AI mark says it: the model, and the person who accepted it. */
function useMarkLabel(
  revisions: ReadonlyArray<ProtocolRevision>,
  people: ReadonlyMap<string, string>,
) {
  return (marks: ReadonlyArray<AiMark>) => {
    const mark = marks[0];
    if (!mark) return null;
    const by = revisions.find((r) => r.id === mark.revision_id);
    const kept = mark.accepted_in ? revisions.find((r) => r.id === mark.accepted_in) : null;
    if (by?.kind === "suggestion" && kept)
      return `Suggested by ${mark.recipe_id} in revision ${by.seq}. Accepted by ${people.get(kept.actor_id) ?? "a person"} in revision ${kept.seq}.`;
    return `Written by ${mark.recipe_id}${by ? ` in revision ${by.seq}` : ""}.`;
  };
}

function Rail({
  revisions,
  at,
  people,
  locked,
  onSelect,
  onDescribe,
}: {
  revisions: ReadonlyArray<ProtocolRevision>;
  at: number;
  people: ReadonlyMap<string, string>;
  locked: boolean;
  onSelect: (index: number) => void;
  onDescribe: (() => void) | null;
}) {
  const seqOf = (id: string | null | undefined) => revisions.find((r) => r.id === id)?.seq;
  return (
    <nav
      className="flex min-h-0 flex-col border-r border-rule-strong bg-sheet-2"
      aria-label="Revisions"
    >
      <div className="flex items-center justify-between border-b border-rule px-3 py-2.5">
        <span className="text-xs font-semibold text-ink-2">History</span>
        <span className="flex gap-0.5" title="Previous and next revision">
          <Key k="," />
          <Key k="." />
        </span>
      </div>
      <ol className="min-h-0 flex-1 overflow-y-auto py-1" data-testid="revision-rail">
        {revisions.length === 0 ? (
          <li className="px-3 py-1.5 text-xs text-ink-3">
            No revisions yet. Save to add the first.
          </li>
        ) : null}
        {[...revisions].reverse().map((r) => {
          const index = revisions.indexOf(r);
          const selected = index === at;
          return (
            <li key={r.id}>
              <button
                type="button"
                onClick={() => onSelect(index)}
                disabled={locked && !selected}
                aria-current={selected ? "true" : undefined}
                data-testid="revision"
                className={`grid w-full grid-cols-[2rem_1fr] px-3 py-1.5 text-left text-sm disabled:opacity-60 ${selected ? "bg-petrol-wash" : "hover:bg-sheet"}`}
              >
                <span className={`text-ink-3 ${selected ? "font-semibold text-ink" : ""}`}>
                  {r.seq}
                </span>
                <span className="min-w-0">
                  <span className="block">
                    {KIND[r.kind]}
                    {r.kind === "restore" ? ` ${seqOf(r.parent_id)}` : ""}
                  </span>
                  <span
                    className="flex items-center gap-1 text-2xs text-ink-2"
                    data-testid="author"
                  >
                    {r.model ? <AiTag label={`Written by ${r.model.recipe_id}`} /> : null}
                    <span className="truncate">
                      {authorOf(r, people)},{" "}
                      {new Date(r.created_at).toLocaleTimeString("en-GB", {
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </span>
                  </span>
                </span>
              </button>
            </li>
          );
        })}
      </ol>
      {onDescribe ? (
        <div className="border-t border-rule p-3">
          <button
            type="button"
            onClick={onDescribe}
            className="text-sm text-petrol hover:underline"
          >
            Ask the model for a new draft
          </button>
        </div>
      ) : null}
    </nav>
  );
}

function FormView({
  working,
  shown,
  revisions,
  current,
  newest,
  people,
  model,
  issues,
  saving,
  countOf,
  onCount,
  onEdit,
  onSave,
  onDiscard,
  onStep,
  onRestore,
  onNewest,
  onYaml,
  onImproved,
  onError,
}: {
  working: Working | null;
  shown: Working;
  revisions: ReadonlyArray<ProtocolRevision>;
  current: ProtocolRevision | null;
  newest: boolean;
  people: ReadonlyMap<string, string>;
  model: AssistModel | null;
  issues: ReadonlyArray<ProtocolIssue>;
  saving: boolean;
  countOf: (dialect: QueryDialect, strategy: string) => Counted | undefined;
  onCount: (dialect: QueryDialect, strategy: string) => void;
  onEdit: (doc: Doc) => void;
  onSave: () => void;
  onDiscard: () => void;
  onStep: (by: number) => void;
  onRestore: () => void;
  onNewest: () => void;
  onYaml: () => void;
  onImproved: (outcome: AssistOutcome) => Promise<void>;
  onError: (message: string | null) => void;
}) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me } = useProject();
  const [improving, setImproving] = useState<string | null>(null);
  const seconds = useElapsed(improving !== null);
  const d = shown.doc;
  const base = shown.base;
  const lang = d.default_language;
  const t = (text: Localized | undefined) =>
    text ? (text[lang] ?? Object.values(text)[0] ?? "") : "";
  const label = useMarkLabel(revisions, people);
  const latest = revisions.at(-1);
  const viewing = !working && current !== null;
  const suggestion = current?.kind === "suggestion" && !working;
  const canAsk = Boolean(model?.configured) && !working && !suggestion;

  /** The model's marks on a field, while it still says what the model wrote. */
  const marks = (part: string) =>
    base && JSON.stringify(valueAt(d, part)) === JSON.stringify(valueAt(docOf(base), part))
      ? marksOn(base.ai_parts ?? [], part)
      : [];

  const change = (fn: (doc: Mutable<Doc> & Record<string, unknown>) => void) => {
    if (suggestion) return;
    const next = structuredClone(d) as Mutable<Doc> & Record<string, unknown>;
    fn(next);
    onEdit(next);
  };
  const localized = (value: Localized, text: string): Localized => ({ ...value, [lang]: text });
  const topicAt = (doc: Mutable<Doc>, ti: number) => doc.search_topics[ti] as Mutable<Topic>;
  const allCriteria = d.search_topics.flatMap((x) => x.criteria.map((c) => c.id));
  const scopeIds = [...d.questions.map((q) => q.id), ...d.search_topics.map((x) => x.id)];

  const improve = async (part: string) => {
    if (!current) return;
    setImproving(part);
    onError(null);
    try {
      const outcome = await run(
        workbench.improvePart({
          projectId: project.id,
          as: me.actor_id,
          revisionId: current.id,
          part,
        }),
      );
      await onImproved(outcome);
    } catch (e) {
      onError(failure(e, "The model did not answer."));
    } finally {
      setImproving(null);
    }
  };

  const ask = (part: string, name: string) =>
    canAsk ? (
      <button
        type="button"
        disabled={improving !== null}
        onClick={() => void improve(part)}
        aria-label={`Ask the model to improve the ${name}`}
        className="shrink-0 rounded-[3px] border border-rule-strong bg-sheet px-2 py-0.5 text-xs text-ink-2 hover:border-petrol hover:text-petrol disabled:cursor-not-allowed disabled:opacity-50"
      >
        {improving === part ? "Asking the model" : "Suggest changes"}
      </button>
    ) : null;

  const field = (props: {
    id: string;
    label: string;
    part: string;
    at: string;
    value: string;
    onChange: (text: string) => void;
    multiline?: boolean;
    mono?: boolean;
    placeholder?: string;
    big?: boolean;
    after?: ReactNode;
  }) => (
    <FormField
      key={props.id}
      {...props}
      mark={label(marks(props.part))}
      model={marks(props.part)[0]?.recipe_id ?? null}
      issues={issuesAt(issues, props.at)}
      readOnly={suggestion}
    />
  );

  return (
    <section className="min-h-0 overflow-y-auto bg-sheet" aria-label="Protocol form">
      <header className="sticky top-0 z-10 border-b border-rule bg-sheet/95 backdrop-blur">
        <div className="flex items-center gap-3 px-8 py-2.5 text-sm">
          {working ? (
            <>
              <span className="font-semibold" data-testid="unsaved">
                {base
                  ? `Your changes to revision ${base.seq}, not saved yet`
                  : "A new protocol, not saved yet"}
              </span>
              <span className="ml-auto flex items-center gap-3">
                <Button primary onClick={onSave} disabled={saving}>
                  {saving ? "Saving" : `Save as revision ${revisions.length + 1}`}
                  <span className="inline-flex gap-0.5">
                    <Key k="Ctrl" />
                    <Key k="Enter" />
                  </span>
                </Button>
                <button type="button" onClick={onDiscard} className="text-ink-2 hover:text-ink">
                  Discard
                </button>
              </span>
            </>
          ) : current ? (
            <>
              <span className="font-semibold">
                Revision {current.seq} of {revisions.length}
              </span>
              <span className="text-ink-2">
                {KIND[current.kind]} by {authorOf(current, people)}
              </span>
              <span className="ml-auto flex items-center gap-4">
                <button
                  type="button"
                  onClick={() => onStep(-1)}
                  className="text-ink-2 hover:text-ink"
                >
                  <KeyHint keys={[","]}>Previous</KeyHint>
                </button>
                <button
                  type="button"
                  onClick={() => onStep(1)}
                  className="text-ink-2 hover:text-ink"
                >
                  <KeyHint keys={["."]}>Next</KeyHint>
                </button>
              </span>
            </>
          ) : null}
          <button type="button" onClick={onYaml} className="text-ink-2 hover:text-ink">
            Open as YAML
          </button>
        </div>
        {viewing && !newest && latest ? (
          <div
            className="flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-unclear/40 bg-unclear-wash px-8 py-2.5 text-sm"
            data-testid="older-revision"
          >
            <p className="max-w-[60ch]">
              You are looking at revision {current.seq}. Looking does not change anything. Revision{" "}
              {latest.seq} stays the newest, and the history keeps every revision.
            </p>
            <span className="ml-auto flex items-center gap-3">
              <Button onClick={onRestore} keys="r">
                Restore this revision
              </Button>
              <button type="button" onClick={onNewest} className="text-ink-2 hover:text-ink">
                Go to revision {latest.seq}
              </button>
            </span>
            <p className="w-full text-xs text-ink-2">
              Restoring saves a copy of revision {current.seq} as revision {latest.seq + 1}, under
              your name. To change revision {current.seq} instead, edit any field and save.
            </p>
          </div>
        ) : null}
        {improving ? (
          <div className="px-8 pb-2.5">
            <Waiting
              seconds={seconds}
              model={model}
              what={`suggestions for the ${partName(d, improving)}`}
            />
          </div>
        ) : null}
      </header>

      <article className="max-w-[880px] px-8 pt-6 pb-16" data-testid="revision-view">
        {field({
          id: "title",
          label: "Title",
          part: "title",
          at: "title",
          value: t(d.title),
          big: true,
          placeholder: "The title of the review",
          onChange: (v) => change((x) => (x.title = localized(x.title, v))),
        })}
        <p className="mt-1 text-xs text-ink-3">
          Version label {d.version_label}
          {!working && current ? `, content hash ${current.content_hash.slice(0, 12)}` : ""}
        </p>

        <SectionHead title="Questions" />
        <ol className="grid gap-6">
          {d.questions.map((q, qi) => {
            const part = `questions[${q.id}]`;
            const at = `questions[${qi}]`;
            return (
              <li key={q.id} className="grid gap-2" data-testid="question">
                <div className="flex items-baseline gap-3">
                  <span className="text-xs text-ink-3">Question {qi + 1}</span>
                  <span className="ml-auto flex items-center gap-2">
                    {ask(part, `question ${q.id}`)}
                    {d.questions.length > 1 && !suggestion ? (
                      <RemoveButton
                        label={`Remove question ${qi + 1}`}
                        onClick={() =>
                          change((x) => {
                            x.questions = x.questions.filter((y) => y.id !== q.id);
                            x.search_topics = x.search_topics.map((tp) => ({
                              ...tp,
                              scopes: tp.scopes.filter(
                                (s) => (typeof s === "string" ? s : s.id) !== q.id,
                              ),
                              criteria: tp.criteria.map((c) => {
                                const left = c.applies_to.filter((a) => a !== q.id);
                                return { ...c, applies_to: left.length ? left : [tp.id] };
                              }),
                            }));
                          })
                        }
                      />
                    ) : null}
                  </span>
                </div>
                {field({
                  id: `${q.id}-label`,
                  label: "Question",
                  part: `${part}.label`,
                  at: `${at}.label`,
                  value: t(q.label),
                  placeholder: "A short name for the question",
                  onChange: (v) =>
                    change((x) => {
                      const y = x.questions[qi] as Mutable<Question>;
                      y.label = localized(y.label, v);
                    }),
                })}
                {(["population", "intervention"] as const).map((k) =>
                  field({
                    id: `${q.id}-${k}`,
                    label: SLOTS[k]!,
                    part: `${part}.${k}`,
                    at: `${at}.${k}`,
                    value: t(q[k]),
                    multiline: true,
                    onChange: (v) =>
                      change((x) => {
                        const y = x.questions[qi] as Mutable<Question>;
                        y[k] = localized(y[k], v);
                      }),
                  }),
                )}
                {q.comparators.map((c, ci) =>
                  field({
                    id: `${q.id}-${c.id}`,
                    label: `Comparator ${ci + 1}`,
                    part: `${part}.comparators[${c.id}]`,
                    at: `${at}.comparators[${ci}]`,
                    value: t(c.text),
                    onChange: (v) =>
                      change((x) => {
                        const y = x.questions[qi] as Mutable<Question>;
                        y.comparators = y.comparators.map((z) =>
                          z.id === c.id ? { ...z, text: localized(z.text, v) } : z,
                        );
                      }),
                    after: suggestion ? null : (
                      <RemoveButton
                        label={`Remove comparator ${ci + 1}`}
                        onClick={() =>
                          change((x) => {
                            const y = x.questions[qi] as Mutable<Question>;
                            y.comparators = y.comparators.filter((z) => z.id !== c.id);
                          })
                        }
                      />
                    ),
                  }),
                )}
                {suggestion ? null : (
                  <AddButton
                    onClick={() =>
                      change((x) => {
                        const y = x.questions[qi] as Mutable<Question>;
                        const id = freshId(
                          "c",
                          y.comparators.map((z) => z.id),
                        );
                        y.comparators = [...y.comparators, { id, text: { [lang]: "" } }];
                      })
                    }
                  >
                    Add a comparator
                  </AddButton>
                )}
              </li>
            );
          })}
        </ol>
        {suggestion ? null : (
          <AddButton
            onClick={() =>
              change((x) => {
                const id = freshId("q", scopeIds);
                x.questions = [
                  ...x.questions,
                  {
                    id,
                    label: { [lang]: "" },
                    population: { [lang]: "" },
                    intervention: { [lang]: "" },
                    comparators: [],
                  },
                ];
                const first = topicAt(x, 0);
                first.scopes = [...first.scopes, id];
              })
            }
          >
            Add a question
          </AddButton>
        )}

        <SectionHead title="Outcomes" />
        <ul className="grid gap-2">
          {d.outcomes.map((o, oi) =>
            field({
              id: `outcome-${o.id}`,
              label: o.category === "primary" ? "Primary outcome" : "Secondary outcome",
              part: `outcomes[${o.id}].name`,
              at: `outcomes[${oi}]`,
              value: t(o.name),
              onChange: (v) =>
                change((x) => {
                  x.outcomes = x.outcomes.map((y) =>
                    y.id === o.id ? { ...y, name: localized(y.name, v) } : y,
                  );
                }),
              after:
                suggestion || d.outcomes.length < 2 ? null : (
                  <RemoveButton
                    label={`Remove outcome ${oi + 1}`}
                    onClick={() =>
                      change((x) => {
                        x.outcomes = x.outcomes.filter((y) => y.id !== o.id);
                        x.questions = x.questions.map((y) =>
                          y.outcomes ? { ...y, outcomes: y.outcomes.filter((z) => z !== o.id) } : y,
                        );
                      })
                    }
                  />
                ),
            }),
          )}
        </ul>
        {suggestion ? null : (
          <AddButton
            onClick={() =>
              change((x) => {
                const id = freshId(
                  "o",
                  x.outcomes.map((y) => y.id),
                );
                x.outcomes = [...x.outcomes, { id, category: "secondary", name: { [lang]: "" } }];
              })
            }
          >
            Add a secondary outcome
          </AddButton>
        )}

        {d.search_topics.map((topic, ti) => {
          const tat = `search_topics[${ti}]`;
          const editTopic = (fn: (x: Mutable<Topic>) => void) => change((x) => fn(topicAt(x, ti)));
          return (
            <section key={topic.id} data-testid="topic">
              <SectionHead title={`Search topic: ${t(topic.label)}`} />
              <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-4">
                {field({
                  id: `${topic.id}-label`,
                  label: "Name of the search topic",
                  part: `search_topics[${topic.id}].label`,
                  at: `${tat}.label`,
                  value: t(topic.label),
                  onChange: (v) => editTopic((x) => (x.label = localized(x.label, v))),
                })}
                <label className="grid gap-1">
                  <span className="text-xs font-semibold text-ink-2">Screening mode</span>
                  <Select
                    value={topic.mode}
                    disabled={suggestion}
                    onChange={(e) => {
                      const mode = e.currentTarget.value;
                      editTopic((x) => (x.mode = mode));
                    }}
                  >
                    {MODES.map(([value, text]) => (
                      <option key={value} value={value}>
                        {text}
                      </option>
                    ))}
                  </Select>
                  <FieldIssues
                    issues={issuesAt(issues, `${tat}.mode`).concat(
                      issuesAt(issues, `${tat}.mode_rationale`),
                      issuesAt(issues, `${tat}.validation_pack`),
                      issuesAt(issues, `${tat}.stopping`),
                    )}
                  />
                </label>
              </div>

              <h3 className="mt-6 text-md font-semibold">Sources and their queries</h3>
              <ul className="mt-2 grid gap-5">
                {topic.source_plan.sources.map((s, si) => {
                  const dialect = dialectOf(s);
                  const part = strategyPath(topic, s);
                  const sat = `${tat}.source_plan.sources[${si}]`;
                  const query = s.strategy ?? "";
                  const counted = dialect && query.trim() ? countOf(dialect, query) : undefined;
                  const notes = [
                    ...(!working && current
                      ? (current.warnings ?? [])
                          .filter((w) => w.path === `${sat}.strategy`)
                          .map((w) => w.message)
                      : []),
                    ...(counted?.state === "done" ? (counted.count.warnings ?? []) : []),
                  ];
                  const editSource = (fn: (x: Mutable<Source>) => void) =>
                    editTopic((x) => {
                      x.source_plan = {
                        ...x.source_plan,
                        sources: x.source_plan.sources.map((y, i) => {
                          if (i !== si) return y;
                          const next = { ...y } as Mutable<Source>;
                          fn(next);
                          return next;
                        }),
                      };
                    });
                  return (
                    <li key={s.id} data-testid="source" data-source={s.id} className="grid gap-2">
                      <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] items-start gap-3">
                        {field({
                          id: `${s.id}-name`,
                          label: "Source",
                          part: `search_topics[${topic.id}].source_plan.sources[${s.id}].name`,
                          at: `${sat}.name`,
                          value: s.name,
                          onChange: (v) => editSource((x) => (x.name = v)),
                        })}
                        {field({
                          id: `${s.id}-platform`,
                          label: "Platform",
                          part: `search_topics[${topic.id}].source_plan.sources[${s.id}].platform`,
                          at: `${sat}.platform`,
                          value: s.platform ?? "",
                          placeholder: "PubMed, Ovid, the registry's site",
                          onChange: (v) => editSource((x) => (x.platform = v || null)),
                        })}
                        {suggestion || topic.source_plan.sources.length < 2 ? (
                          <span />
                        ) : (
                          <span className="pt-5">
                            <RemoveButton
                              label={`Remove the source ${s.name || si + 1}`}
                              onClick={() =>
                                editTopic((x) => {
                                  x.source_plan = {
                                    ...x.source_plan,
                                    sources: x.source_plan.sources.filter((_, i) => i !== si),
                                  };
                                })
                              }
                            />
                          </span>
                        )}
                      </div>
                      {field({
                        id: `${s.id}-query`,
                        label: dialect ? `Query in ${SOURCE_NAME[dialect]}'s syntax` : "Query",
                        part,
                        at: `${sat}.strategy`,
                        value: query,
                        multiline: true,
                        mono: true,
                        placeholder: dialect
                          ? dialect === "pubmed"
                            ? 'For example "nicotine replacement"[tiab] AND "smoking cessation"[tiab]'
                            : 'For example "nicotine replacement" AND "smoking cessation"'
                          : "Leave it empty if you search this source on its own site.",
                        onChange: (v) => editSource((x) => (x.strategy = v)),
                        after: (
                          <span className="flex w-[190px] shrink-0 flex-col items-end gap-1.5">
                            {dialect && query.trim() ? (
                              <CountButton
                                counted={counted}
                                onCount={() => onCount(dialect, query)}
                                dialect={dialect}
                              />
                            ) : null}
                            {query.trim() ? ask(part, `${s.name} query`) : null}
                          </span>
                        ),
                      })}
                      {dialect ? <CountResult counted={counted} dialect={dialect} /> : null}
                      <Notes notes={notes} />
                    </li>
                  );
                })}
              </ul>
              {suggestion ? null : (
                <div className="mt-2 flex gap-4">
                  {(
                    [
                      [
                        "Add PubMed",
                        (ids: string[]) =>
                          PUBMED(ids.includes("medline") ? freshId("medline", ids) : "medline"),
                      ],
                      [
                        "Add ClinicalTrials.gov",
                        (ids: string[]) =>
                          CTGOV(
                            ids.includes("clinicaltrials_gov")
                              ? freshId("clinicaltrials_gov", ids)
                              : "clinicaltrials_gov",
                          ),
                      ],
                      [
                        "Add another source",
                        (ids: string[]) => ({
                          id: freshId("source", ids),
                          kind: "bibliographic",
                          name: "",
                        }),
                      ],
                    ] as const
                  ).map(([text, make]) => (
                    <AddButton
                      key={text}
                      onClick={() =>
                        editTopic((x) => {
                          const ids = x.source_plan.sources.map((y) => y.id);
                          x.source_plan = {
                            ...x.source_plan,
                            sources: [...x.source_plan.sources, make(ids)],
                          };
                        })
                      }
                    >
                      {text}
                    </AddButton>
                  ))}
                </div>
              )}

              <h3 className="mt-6 text-md font-semibold">Eligibility criteria</h3>
              <ul className="mt-2 grid gap-4">
                {topic.criteria.map((c, ci) => {
                  const part = `search_topics[${topic.id}].criteria[${c.id}]`;
                  const cat = `${tat}.criteria[${ci}]`;
                  const editCriterion = (fn: (x: Mutable<Criterion>) => void) =>
                    editTopic((x) => {
                      x.criteria = x.criteria.map((y, i) => {
                        if (i !== ci) return y;
                        const next = { ...y } as Mutable<Criterion>;
                        fn(next);
                        return next;
                      });
                    });
                  return (
                    <li
                      key={c.id}
                      className="grid gap-1.5"
                      data-testid="criterion"
                      data-criterion={c.id}
                    >
                      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
                        <span className="font-semibold text-ink-2">Criterion {c.id}</span>
                        <Select
                          aria-label={`Kind of criterion ${c.id}`}
                          value={c.polarity}
                          disabled={suggestion}
                          onChange={(e) => {
                            const v = e.currentTarget.value;
                            editCriterion((x) => (x.polarity = v));
                          }}
                          className="!w-auto !py-0.5 !text-xs"
                        >
                          <option value="include">Inclusion</option>
                          <option value="exclude">Exclusion</option>
                        </Select>
                        <Select
                          aria-label={`Part of the question for criterion ${c.id}`}
                          value={c.slot}
                          disabled={suggestion}
                          onChange={(e) => {
                            const v = e.currentTarget.value;
                            editCriterion((x) => (x.slot = v));
                          }}
                          className="!w-auto !py-0.5 !text-xs"
                        >
                          {Object.entries(SLOTS).map(([value, text]) => (
                            <option key={value} value={value}>
                              {text}
                            </option>
                          ))}
                        </Select>
                        <span className="text-ink-2">Decided at</span>
                        {STAGES.map(([stage, text]) => (
                          <label key={stage} className="inline-flex items-center gap-1">
                            <input
                              type="checkbox"
                              disabled={suggestion}
                              checked={c.decidable_at.includes(stage)}
                              onChange={(e) => {
                                const on = e.currentTarget.checked;
                                editCriterion(
                                  (x) =>
                                    (x.decidable_at = STAGES.map(([s]) => s).filter((s) =>
                                      s === stage ? on : x.decidable_at.includes(s),
                                    )),
                                );
                              }}
                              className="accent-[var(--petrol)]"
                            />
                            {text}
                          </label>
                        ))}
                        <span className="ml-auto flex items-center gap-2">
                          {ask(part, `criterion ${c.id}`)}
                          {suggestion || topic.criteria.length < 2 ? null : (
                            <RemoveButton
                              label={`Remove criterion ${c.id}`}
                              onClick={() =>
                                editTopic(
                                  (x) => (x.criteria = x.criteria.filter((_, i) => i !== ci)),
                                )
                              }
                            />
                          )}
                        </span>
                      </div>
                      <FieldIssues
                        issues={issuesAt(issues, `${cat}.decidable_at`).concat(
                          issuesAt(issues, `${cat}.applies_to`),
                        )}
                      />
                      {field({
                        id: `${c.id}-text`,
                        label: "Text of the criterion",
                        part: `${part}.text`,
                        at: `${cat}.text`,
                        value: t(c.text),
                        multiline: true,
                        placeholder:
                          c.polarity === "include"
                            ? "What a record must show to stay in, in one sentence"
                            : "What makes a record go out, in one sentence",
                        onChange: (v) => editCriterion((x) => (x.text = localized(x.text, v))),
                      })}
                    </li>
                  );
                })}
              </ul>
              <FieldIssues issues={issues.filter((i) => i.path === `${tat}.criteria`)} />
              {suggestion ? null : (
                <AddButton
                  onClick={() =>
                    editTopic((x) => {
                      x.criteria = [
                        ...x.criteria,
                        {
                          id: freshId("C", allCriteria),
                          slot: "other",
                          polarity: "include",
                          text: { [lang]: "" },
                          decidable_at: ["title_abstract", "full_text"],
                          applies_to: [topic.id],
                        },
                      ];
                    })
                  }
                >
                  Add a criterion
                </AddButton>
              )}
            </section>
          );
        })}
      </article>
    </section>
  );
}

function SectionHead({ title }: { title: string }) {
  return (
    <h2 className="mt-9 mb-3 border-t border-rule-strong pt-3 text-lg font-semibold">{title}</h2>
  );
}

function AddButton({ onClick, children }: { onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="mt-1 justify-self-start text-sm text-petrol hover:underline"
    >
      {children}
    </button>
  );
}

function RemoveButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className="rounded-[3px] px-1.5 text-sm text-ink-3 hover:bg-exclude-wash hover:text-exclude"
    >
      Remove
    </button>
  );
}

function FieldIssues({ issues }: { issues: ReadonlyArray<ProtocolIssue> }) {
  if (!issues.length) return null;
  return (
    <ul className="grid gap-0.5 text-xs text-exclude" data-testid="field-issues">
      {issues.map((i, n) => (
        <li key={n}>{plainIssue(i.message)}</li>
      ))}
    </ul>
  );
}

/** One field of the form, edited in place. A field a model wrote has a dashed rule and an AI mark
 * beside its label. `multiline` lets Enter start a new line. */
function FormField({
  id,
  label,
  value,
  onChange,
  multiline,
  mono,
  placeholder,
  big,
  after,
  mark,
  model,
  issues,
  readOnly,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (text: string) => void;
  multiline?: boolean;
  mono?: boolean;
  placeholder?: string;
  big?: boolean;
  after?: ReactNode;
  mark: string | null;
  model: string | null;
  issues: ReadonlyArray<ProtocolIssue>;
  readOnly: boolean;
}) {
  const control = `w-full rounded-[3px] border bg-transparent px-2 py-1 text-ink outline-none placeholder:font-normal placeholder:text-ink-3 hover:border-rule-strong focus:border-petrol focus:bg-sheet ${
    issues.length ? "border-exclude" : "border-rule"
  } ${mono ? "font-mono text-[12.5px] leading-[19px]" : big ? "text-xl font-semibold" : "text-sm"} [field-sizing:content] resize-none read-only:hover:border-transparent`;
  const inputId = `field-${id}`;
  return (
    <div
      className={`grid gap-0.5 ${mark ? "border-l-2 border-dashed border-petrol pl-2.5" : "pl-3"}`}
      data-testid={mark ? "ai-written" : "field"}
      data-model={model ?? undefined}
    >
      <span className="flex items-center gap-1.5 pl-2">
        <label htmlFor={inputId} className="text-xs font-semibold text-ink-2">
          {label}
        </label>
        {mark ? <AiTag label={mark} /> : null}
      </span>
      <span className="flex items-start gap-3">
        {/* A textarea for every field, so a long value wraps instead of hiding. Enter adds a line
        only where a line break means something. */}
        <textarea
          id={inputId}
          value={value}
          readOnly={readOnly}
          rows={1}
          spellCheck={!mono}
          placeholder={placeholder}
          onChange={(e) => onChange(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !multiline && !e.ctrlKey && !e.metaKey) e.preventDefault();
          }}
          className={`${control} min-h-[30px] min-w-0`}
        />
        {after}
      </span>
      <span className="pl-2">
        <FieldIssues issues={issues} />
      </span>
    </div>
  );
}

/** What a source or the compiler said about a query: a word PubMed found nowhere, a field tag it
 * does not know. Usually a typo. Nothing stops on them. */
function Notes({ notes }: { notes: ReadonlyArray<string> }) {
  if (!notes.length) return null;
  return (
    <ul className="grid gap-0.5 pl-5 text-xs text-unclear" data-testid="strategy-notes">
      {notes.map((n) => (
        <li key={n} className="border-l-2 border-unclear pl-2">
          {n}
        </li>
      ))}
    </ul>
  );
}

function CountButton({
  counted,
  onCount,
  dialect,
}: {
  counted: Counted | undefined;
  onCount: () => void;
  dialect: QueryDialect;
}) {
  return (
    <button
      type="button"
      onClick={onCount}
      disabled={counted?.state === "counting"}
      className="rounded-[3px] border border-rule-strong bg-sheet px-2 py-0.5 text-xs whitespace-nowrap text-ink-2 hover:border-petrol hover:text-petrol disabled:opacity-50"
    >
      {counted?.state === "counting"
        ? `Asking ${SOURCE_NAME[dialect]}`
        : counted?.state === "failed"
          ? "Retry"
          : `Count hits in ${SOURCE_NAME[dialect]}`}
    </button>
  );
}

/** What the last count of a query found, or why it found nothing, under the query. */
function CountResult({
  counted,
  dialect,
}: {
  counted: Counted | undefined;
  dialect: QueryDialect;
}) {
  if (counted?.state === "done")
    return (
      <p
        className="pl-5 text-xs text-ink-2"
        data-testid="hit-count"
        title={counted.count.query_translation ?? undefined}
      >
        <span className="text-sm font-semibold text-ink">
          {counted.count.count.toLocaleString("en-GB")}
        </span>{" "}
        hits in {SOURCE_NAME[dialect]} today
      </p>
    );
  if (counted?.state === "failed")
    return (
      <p
        className="pl-5 text-xs [overflow-wrap:anywhere] text-exclude"
        role="alert"
        data-testid="count-failed"
      >
        {counted.message} Use Retry to ask again.
      </p>
    );
  return null;
}

function Unsaved({
  working,
  next,
  issues,
  saving,
  onSave,
  onDiscard,
}: {
  working: Working;
  next: number;
  issues: ReadonlyArray<ProtocolIssue>;
  saving: boolean;
  onSave: () => void;
  onDiscard: () => void;
}) {
  const { me } = useProject();
  return (
    <section data-testid="unsaved-panel">
      <h2 className="text-md font-semibold">Not saved yet</h2>
      <p className="mt-1 text-sm text-ink-2">
        {working.base
          ? `You are changing revision ${working.base.seq}.`
          : "You are writing a new protocol."}{" "}
        Saving adds revision {next} to the history, written by {me.display_name}. You can count a
        query's hits before you save.
      </p>
      {issues.length ? (
        <>
          <h3 className="mt-4 text-sm font-semibold text-exclude">
            Fix {issues.length} {issues.length === 1 ? "problem" : "problems"} before you save
          </h3>
          <p className="mt-0.5 text-xs text-ink-2">The form marks each one in red at its field.</p>
          <Issues issues={issues} doc={working.doc} />
        </>
      ) : null}
      <div className="mt-4 flex items-center gap-3">
        <Button primary onClick={onSave} disabled={saving}>
          {saving ? "Saving" : `Save as revision ${next}`}
        </Button>
        <Button onClick={onDiscard}>Discard</Button>
      </div>
    </section>
  );
}

/** The query as a proofreader marks it: each change at its place, numbered, with the chosen ones
 * struck and inserted and the others left as they are. */
function StrategyMarkup({
  strategy,
  changes,
  chosen,
}: {
  strategy: string;
  changes: ReadonlyArray<SuggestedChange>;
  chosen: ReadonlySet<string>;
}) {
  const parts: ReactNode[] = [];
  let at = 0;
  const ordered = [...changes].sort((a, b) => (a.start ?? 0) - (b.start ?? 0));
  for (const [n, c] of ordered.entries()) {
    const start = c.start ?? 0;
    const before = String(c.before ?? "");
    parts.push(strategy.slice(at, start));
    const on = chosen.has(c.id);
    parts.push(
      <span key={c.id} data-change={c.id}>
        <sup className="mr-0.5 font-sans text-[10px] font-semibold text-petrol">{n + 1}</sup>
        <del className={on ? "text-exclude decoration-exclude" : "no-underline"}>{before}</del>
        {on ? (
          <ins className="bg-include-wash text-include underline decoration-include decoration-1 underline-offset-2">
            {String(c.after ?? "")}
          </ins>
        ) : null}
      </span>,
    );
    at = start + before.length;
  }
  parts.push(strategy.slice(at));
  return (
    <pre
      className="text-[12.5px] leading-[21px] whitespace-pre-wrap [&_del]:no-underline [&_del.text-exclude]:line-through"
      data-testid="strategy-markup"
    >
      {parts}
    </pre>
  );
}

const applyChosen = (
  strategy: string,
  changes: ReadonlyArray<SuggestedChange>,
  chosen: ReadonlySet<string>,
) =>
  [...changes]
    .filter((c) => chosen.has(c.id))
    .sort((a, b) => (b.start ?? 0) - (a.start ?? 0))
    .reduce((text, c) => {
      const start = c.start ?? 0;
      return (
        text.slice(0, start) +
        String(c.after ?? "") +
        text.slice(start + String(c.before ?? "").length)
      );
    }, strategy);

const show = (value: unknown) =>
  value === null || value === undefined
    ? "Nothing"
    : typeof value === "string"
      ? value
      : JSON.stringify(value);

function SuggestionReview({
  revision,
  base,
  countOf,
  onCount,
  onAccept,
  onReject,
}: {
  revision: ProtocolRevision;
  base: ProtocolRevision | null;
  countOf: (dialect: QueryDialect, strategy: string) => Counted | undefined;
  onCount: (dialect: QueryDialect, strategy: string) => void;
  onAccept: (ids: ReadonlyArray<string>) => Promise<void>;
  onReject: () => void;
}) {
  const suggestion = revision.suggestion;
  const changes = suggestion?.changes ?? [];
  const [chosen, setChosen] = useState<ReadonlySet<string>>(
    () => new Set(changes.map((c) => c.id)),
  );
  const [busy, setBusy] = useState(false);
  const toggle = (id: string) =>
    setChosen((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const accept = async () => {
    if (!chosen.size || busy) return;
    setBusy(true);
    try {
      await onAccept(changes.filter((c) => chosen.has(c.id)).map((c) => c.id));
    } finally {
      setBusy(false);
    }
  };
  useKeys((event) => {
    const n = Number(event.key);
    if (Number.isInteger(n) && n >= 1 && n <= changes.length)
      return (toggle(changes[n - 1]!.id), true);
    if (event.key === "a") return (void accept(), true);
    if (event.key === "Escape") return (onReject(), true);
    return false;
  });
  if (!suggestion || !base) return null;

  const strategySource = (() => {
    if (suggestion.kind !== "strategy") return null;
    for (const t of docOf(base).search_topics)
      for (const s of t.source_plan.sources) if (strategyPath(t, s) === suggestion.part) return s;
    return null;
  })();
  const dialect = strategySource ? dialectOf(strategySource) : null;
  const before = strategySource?.strategy ?? "";
  // The test IDs stay as they were: count-Now, count-change, count-choice.
  const variants: ReadonlyArray<readonly [string, string, string]> = strategySource
    ? [
        ["As it is now", "Now", before],
        [
          "With all suggestions",
          "change",
          applyChosen(before, changes, new Set(changes.map((c) => c.id))),
        ],
        ["With your selection", "choice", applyChosen(before, changes, chosen)],
      ]
    : [];
  const unreached = dialect
    ? variants.map(([, , s]) => countOf(dialect, s)).find((c) => c?.state === "failed")
    : undefined;
  const model = revision.model;

  return (
    <div data-testid="suggestion">
      <h2 className="text-md font-semibold">
        {changes.length} suggested {changes.length === 1 ? "change" : "changes"} to the{" "}
        {partName(docOf(base), suggestion.part)}
      </h2>
      <p className="mt-1 text-xs text-ink-2">
        Suggested by {model?.recipe_id}
        {model?.model_identity && model.model_identity !== model.recipe_id
          ? ` (${model.model_identity})`
          : ""}
        {model?.endpoint ? ` at ${model.endpoint}` : ""}. Tick the changes you want and accept them.
        Kittum saves them as a new revision under your name.
      </p>

      {strategySource ? (
        <div className="mt-4 border-y border-rule bg-sheet px-3 py-2.5">
          <StrategyMarkup strategy={before} changes={changes} chosen={chosen} />
        </div>
      ) : null}

      <ol className="mt-4 grid gap-3">
        {changes.map((c, i) => (
          <li key={c.id}>
            <label
              className="grid cursor-pointer grid-cols-[auto_1fr] gap-x-2.5 text-sm"
              data-testid="change"
            >
              <input
                type="checkbox"
                checked={chosen.has(c.id)}
                onChange={() => toggle(c.id)}
                className="mt-[3px] accent-[var(--petrol)]"
                aria-label={`Change ${i + 1}`}
              />
              <span>
                <span className="font-semibold text-petrol">{i + 1}</span>{" "}
                {c.rationale || "The model gave no reason."}
                {suggestion.kind !== "strategy" ? (
                  <span className="mt-1 block text-xs">
                    <span className="block text-ink-2">{c.field}</span>
                    <span className="block text-ink-3 line-through decoration-rule-strong">
                      {show(c.before)}
                    </span>
                    <span className="block">{show(c.after)}</span>
                  </span>
                ) : null}
              </span>
              <span />
              <span className="text-2xs text-ink-3">
                Press <Key k={String(i + 1)} /> to tick or untick it
              </span>
            </label>
          </li>
        ))}
      </ol>

      {dialect ? (
        <div className="mt-5" data-testid="counts">
          <h3 className="text-sm font-semibold">How many hits each version of the query finds</h3>
          <p className="mt-0.5 text-xs text-ink-2">
            Kittum asks {SOURCE_NAME[dialect]} how many records match today. A count runs no search
            and adds nothing to the ledger.
          </p>
          <table className="mt-2 w-full text-sm">
            <tbody>
              {variants.map(([label, testId, s]) => {
                const counted = countOf(dialect, s);
                const notes = counted?.state === "done" ? (counted.count.warnings ?? []) : [];
                return (
                  <tr key={label} className="border-b border-rule align-baseline">
                    <td className="py-1 text-ink-2">
                      {label}
                      <Notes notes={notes} />
                    </td>
                    <td className="py-1 text-right font-semibold" data-testid={`count-${testId}`}>
                      {counted?.state === "done" ? (
                        counted.count.count.toLocaleString("en-GB")
                      ) : counted?.state === "counting" ? (
                        <span className="font-normal text-ink-3">Counting</span>
                      ) : counted?.state === "failed" ? (
                        <span className="font-normal text-exclude" title={counted.message}>
                          Not reached.{" "}
                          <button
                            type="button"
                            onClick={() => onCount(dialect, s)}
                            className="text-petrol underline"
                          >
                            Retry
                          </button>
                        </span>
                      ) : (
                        ""
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {unreached?.state === "failed" ? (
            <p className="mt-1 text-xs text-exclude" data-testid="count-failed">
              {unreached.message}
            </p>
          ) : null}
          <Button
            onClick={() => variants.forEach(([, , s]) => onCount(dialect, s))}
            className="mt-2"
          >
            Count the hits of all three versions
          </Button>
        </div>
      ) : null}

      <div className="mt-5 flex items-center gap-3">
        <Button primary onClick={() => void accept()} disabled={!chosen.size || busy} keys="a">
          {busy ? "Accepting" : `Accept ${chosen.size} of ${changes.length}`}
        </Button>
        <Button onClick={onReject} keys="Esc">
          Leave all
        </Button>
      </div>
    </div>
  );
}

function Provenance({
  revision,
  revisions,
  people,
}: {
  revision: ProtocolRevision;
  revisions: ReadonlyArray<ProtocolRevision>;
  people: ReadonlyMap<string, string>;
}) {
  const seqOf = (id: string | null | undefined) => revisions.find((r) => r.id === id)?.seq;
  const model = revision.model;
  const written = revision.ai_parts ?? [];
  const person = people.get(revision.actor_id) ?? "Someone";
  const n = revision.accepted?.change_ids.length ?? 0;
  return (
    <section data-testid="provenance">
      <h2 className="text-md font-semibold">Revision {revision.seq}</h2>
      <p className="mt-1 text-sm text-ink-2">
        {revision.kind === "draft"
          ? `${model?.recipe_id ?? "A model"} drafted this protocol from the description ${person} wrote.`
          : revision.kind === "accepted" && revision.accepted
            ? `${person} accepted ${n} ${n === 1 ? "change" : "changes"} from revision ${seqOf(revision.accepted.suggestion_id)} into revision ${seqOf(revision.parent_id)}.`
            : revision.kind === "edit"
              ? `${person} wrote this revision by hand${revision.parent_id ? `, starting from revision ${seqOf(revision.parent_id)}` : ""}.`
              : revision.kind === "restore"
                ? `${person} restored revision ${seqOf(revision.parent_id)}. This revision is a copy of it.`
                : `${model?.recipe_id ?? "A model"} suggested these changes.`}
      </p>
      {model ? (
        <dl className="mt-3 grid grid-cols-[6.5rem_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
          <dt className="text-ink-3">Model</dt>
          <dd>
            {model.recipe_id}
            {model.model_identity ? (
              <span className="block text-ink-3">{model.model_identity}</span>
            ) : null}
          </dd>
          <dt className="text-ink-3">Where</dt>
          <dd>
            {model.endpoint
              ? `${model.serving} at ${model.endpoint}`
              : "Recorded answers, replayed"}
          </dd>
          <dt className="text-ink-3">Prompt</dt>
          <dd title={model.prompt_hash}>
            {model.prompt_version}, SHA-256 {model.prompt_hash.slice(0, 12)}
          </dd>
          <dt className="text-ink-3">Tries</dt>
          <dd>
            <ol className="grid gap-1">
              {model.attempts.map((a, i) => (
                <li key={i} data-testid="attempt">
                  {(a.issues ?? []).length ? (
                    <span className="text-exclude">
                      {i + 1}. Sent back. {(a.issues ?? []).map((x) => x.message).join(". ")}
                    </span>
                  ) : (
                    <span className="text-include">{i + 1}. Compiled.</span>
                  )}
                  <span className="block text-ink-3" title={a.raw_response_hash ?? undefined}>
                    Raw answer {a.raw_response_hash?.slice(0, 12) ?? "missing"}
                  </span>
                </li>
              ))}
            </ol>
          </dd>
        </dl>
      ) : null}
      <p className="mt-3 flex items-center gap-1.5 text-xs text-ink-2" data-testid="written-count">
        {written.length ? (
          <>
            <AiTag label="Written by a model" />
            {written.length} {written.length === 1 ? "field was" : "fields were"} written by a
            model. Point at a mark to see which.
          </>
        ) : (
          "A person wrote every field."
        )}
      </p>
    </section>
  );
}

function Compare({
  revision,
  revisions,
}: {
  revision: ProtocolRevision;
  revisions: ReadonlyArray<ProtocolRevision>;
}) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me } = useProject();
  const others = revisions.filter((r) => r.id !== revision.id);
  const [other, setOther] = useState(revision.parent_id ?? others.at(-1)?.id ?? "");
  const [diff, setDiff] = useState<ProtocolDiff | null>(null);
  useEffect(() => {
    if (!other) return;
    let live = true;
    run(
      workbench.compareRevisions({
        projectId: project.id,
        as: me.actor_id,
        from: other,
        to: revision.id,
      }),
    ).then(
      (d) => live && setDiff(d),
      () => live && setDiff(null),
    );
    return () => {
      live = false;
    };
  }, [workbench, project.id, me.actor_id, other, revision.id]);
  if (!others.length) return null;
  const changes = diff?.changes ?? [];
  return (
    <section className="mt-6 border-t border-rule-strong pt-4">
      <Field label="Compare with">
        <Select
          value={other}
          onChange={(e) => setOther(e.currentTarget.value)}
          aria-label="Compare with revision"
        >
          {others.map((r) => (
            <option key={r.id} value={r.id}>
              Revision {r.seq}, {KIND[r.kind].toLowerCase()}
            </option>
          ))}
        </Select>
      </Field>
      <ol className="mt-3 grid gap-2 text-sm" data-testid="revision-diff">
        {changes.length === 0 ? (
          <li className="text-ink-2">The two revisions are the same.</li>
        ) : null}
        {changes.slice(0, 40).map((c) => (
          <li
            key={c.path}
            className="border-l-2 border-rule-strong pl-3"
            data-testid="revision-change"
          >
            <span className="block text-xs font-semibold break-words text-ink-2">{c.path}</span>
            {typeof c.before === "string" && typeof c.after === "string" ? (
              <WordDiff before={c.before} after={c.after} />
            ) : (
              <>
                <span className="block text-ink-3 line-through decoration-rule-strong break-words">
                  {show(c.before)}
                </span>
                <span className="block break-words">{show(c.after)}</span>
              </>
            )}
          </li>
        ))}
        {changes.length > 40 ? (
          <li className="text-ink-2">{changes.length - 40} more changes</li>
        ) : null}
      </ol>
    </section>
  );
}

/** Two texts as one, word by word: removed words struck, added ones underlined. */
function WordDiff({ before, after }: { before: string; after: string }) {
  const a = before.split(/(\s+)/);
  const b = after.split(/(\s+)/);
  // The longest common subsequence of the tokens, filled from the end.
  const lcs = Array.from({ length: a.length + 1 }, () =>
    Array.from({ length: b.length + 1 }, () => 0),
  );
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      lcs[i]![j] =
        a[i] === b[j] ? lcs[i + 1]![j + 1]! + 1 : Math.max(lcs[i + 1]![j]!, lcs[i]![j + 1]!);
    }
  }
  const out: ReactNode[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) {
      out.push(a[i]);
      i++;
      j++;
    } else if (j < b.length && (i === a.length || lcs[i]![j + 1]! >= lcs[i + 1]![j]!)) {
      out.push(
        <ins key={`b${j}`} className="bg-include-wash text-include no-underline">
          {b[j]}
        </ins>,
      );
      j++;
    } else {
      out.push(
        <del key={`a${i}`} className="text-exclude">
          {a[i]}
        </del>,
      );
      i++;
    }
  }
  return <span className="block break-words whitespace-pre-wrap">{out}</span>;
}

function Publish({
  revision,
  onPublish,
}: {
  revision: ProtocolRevision;
  onPublish: (reason: string | null) => Promise<void>;
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <form
      className="mt-6 grid gap-3 border-t border-rule-strong pt-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (busy) return;
        setBusy(true);
        setError(null);
        onPublish(reason.trim() || null)
          .catch((x) => setError(failure(x, "Kittum could not publish the protocol.")))
          .finally(() => setBusy(false));
      }}
    >
      <Field
        label="Reason"
        hint="Why this version, in one sentence. The ledger keeps it with your approval and the fields a model wrote."
      >
        <TextInput value={reason} onChange={(e) => setReason(e.currentTarget.value)} />
      </Field>
      <div>
        <Button primary type="submit" disabled={busy}>
          {busy ? "Publishing" : `Publish revision ${revision.seq}`}
        </Button>
      </div>
      {error ? (
        <p role="alert" className="text-sm text-exclude" data-testid="form-error">
          {error}
        </p>
      ) : null}
    </form>
  );
}
