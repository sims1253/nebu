// Writing the protocol with the assistant (DESIGN.md, "Protocol"): a model drafts it from the
// lead's description, or suggests changes to one part (a question, a criterion, a source's
// strategy), each with its reason. The lead accepts each change or not; every draft, suggestion,
// selection, edit, and restore is a revision on the rail, `[` and `]` step through them, and only
// the lead publishes one. Fields a model wrote carry its name and revision until a person changes
// them, and the publication records them for the AI disclosure. Counts are informational.
import { useRouteContext, useRouter } from "@tanstack/react-router";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import type {
  AiMark,
  AssistOutcome,
  ProtocolDiff,
  ProtocolIssue,
  ProtocolRevision,
  QueryCount,
  QueryDialect,
  SuggestedChange,
} from "../../data/schema.ts";
import { run } from "../../data/workbench.ts";
import { Button, Field, Select, TextArea, TextInput, failure } from "../../ui/Form.tsx";
import { Key, KeyHint } from "../../ui/Key.tsx";
import { useKeys } from "../../ui/keys.ts";
import { useProject } from "../project.tsx";

// The document as the server stores it: the protocol's JSON, read only as far as this screen shows.
type Localized = Readonly<Record<string, string>>;
interface Question {
  readonly id: string;
  readonly label: Localized;
  readonly population: Localized;
  readonly intervention: Localized;
  readonly comparators: ReadonlyArray<{ readonly id: string; readonly text: Localized }>;
}
interface Criterion {
  readonly id: string;
  readonly slot: string;
  readonly polarity: string;
  readonly text: Localized;
  readonly decidable_at: ReadonlyArray<string>;
}
interface Source {
  readonly id: string;
  readonly kind: string;
  readonly name: string;
  readonly platform?: string | null;
  readonly strategy?: string;
}
interface Topic {
  readonly id: string;
  readonly label: Localized;
  readonly mode: string;
  readonly criteria: ReadonlyArray<Criterion>;
  readonly source_plan: { readonly sources: ReadonlyArray<Source> };
}
interface Doc {
  readonly title: Localized;
  readonly version_label: string;
  readonly default_language: string;
  readonly questions: ReadonlyArray<Question>;
  readonly outcomes: ReadonlyArray<{
    readonly id: string;
    readonly category: string;
    readonly name: Localized;
  }>;
  readonly search_topics: ReadonlyArray<Topic>;
}

const doc = (r: ProtocolRevision) => r.document as unknown as Doc;

const KIND: Record<ProtocolRevision["kind"], string> = {
  draft: "Model draft",
  suggestion: "Model suggestion",
  accepted: "Your selection",
  edit: "Your edit",
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

/** The interface a strategy is written for, as the server decides it. */
const dialectOf = (s: Source): QueryDialect | null => {
  const platform = (s.platform ?? "").toLowerCase();
  if (platform === "pubmed" || s.id.toLowerCase() === "pubmed") return "pubmed";
  const names = [s.id, platform, s.name].map((n) => n.toLowerCase());
  return names.some((n) => ["clinicaltrials_gov", "ctgov", "clinicaltrials.gov"].includes(n))
    ? "ctgov"
    : null;
};

const SOURCE_NAME: Record<QueryDialect, string> = {
  pubmed: "PubMed",
  ctgov: "ClinicalTrials.gov",
};

const strategyPath = (t: Topic, s: Source) =>
  `search_topics[${t.id}].source_plan.sources[${s.id}].strategy`;

/** The marks on a part: the field itself or anything inside it. */
const marksOn = (marks: ReadonlyArray<AiMark>, part: string) =>
  marks.filter(
    (m) => m.path === part || m.path.startsWith(`${part}.`) || m.path.startsWith(`${part}[`),
  );

/** "MEDLINE strategy", "criterion POP", "question nrt-vs-control", from a part's path. */
const partName = (revision: ProtocolRevision, part: string) => {
  const d = doc(revision);
  for (const t of d.search_topics) {
    for (const s of t.source_plan.sources) {
      if (strategyPath(t, s) === part) return `${s.name} strategy`;
    }
    for (const c of t.criteria)
      if (`search_topics[${t.id}].criteria[${c.id}]` === part) return `criterion ${c.id}`;
  }
  const q = d.questions.find((x) => `questions[${x.id}]` === part);
  return q ? `question ${q.id}` : part;
};

const elapsedLabel = (seconds: number) => (seconds < 2 ? "" : ` (${seconds} s)`);

function useElapsed(running: boolean) {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    if (!running) return;
    const started = Date.now();
    const timer = window.setInterval(
      () => setSeconds(Math.round((Date.now() - started) / 1000)),
      500,
    );
    return () => {
      window.clearInterval(timer);
      setSeconds(0);
    };
  }, [running]);
  return seconds;
}

type Counted =
  | { readonly state: "counting" }
  | { readonly state: "done"; readonly count: QueryCount }
  | { readonly state: "failed"; readonly message: string };

export function Assistant({
  onEditText,
  onPublished,
}: {
  onEditText: (text: string, base: ProtocolRevision) => void;
  onPublished: (message: string) => void;
}) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const router = useRouter();
  const { project, me } = useProject();
  const caller = useMemo(
    () => ({ projectId: project.id, as: me.actor_id }),
    [project.id, me.actor_id],
  );
  const [revisions, setRevisions] = useState<ReadonlyArray<ProtocolRevision> | null>(null);
  const [at, setAt] = useState(0);
  const [drafting, setDrafting] = useState(false);
  const [prompting, setPrompting] = useState(false);
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
      (e) => live && setError(failure(e, "The revisions could not be read.")),
    );
    return () => {
      live = false;
    };
  }, [workbench, caller]);

  const count = useCallback(
    (dialect: QueryDialect, strategy: string) => {
      setCounts((c) => new Map(c).set(`${dialect}|${strategy}`, { state: "counting" }));
      run(workbench.countHits({ ...caller, dialect, strategy })).then(
        (found) =>
          setCounts((c) =>
            new Map(c).set(`${dialect}|${strategy}`, { state: "done", count: found }),
          ),
        (e) =>
          setCounts((c) =>
            new Map(c).set(`${dialect}|${strategy}`, {
              state: "failed",
              message: failure(e, "The source did not answer."),
            }),
          ),
      );
    },
    [workbench, caller],
  );
  const countOf = (dialect: QueryDialect, strategy: string) => counts.get(`${dialect}|${strategy}`);

  const current = revisions?.[at] ?? null;
  const step = (by: number) => {
    if (!revisions?.length) return;
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

  useKeys((event) => {
    if (drafting || !current) return false;
    if (event.key === "[") return (step(-1), true);
    if (event.key === "]") return (step(1), true);
    if (event.key === "r" && revisions && at < revisions.length - 1) {
      void act(
        async () =>
          (await run(workbench.restoreRevision({ ...caller, revisionId: current.id }))).id,
        "The revision was not restored.",
      );
      return true;
    }
    return false;
  });

  const draft = async (description: string) => {
    setDrafting(true);
    setFailed(null);
    setError(null);
    try {
      const outcome = await run(workbench.draftProtocol({ ...caller, description }));
      if (outcome.revision) {
        setPrompting(false);
        await reload(outcome.revision.id);
      } else setFailed(outcome);
    } catch (e) {
      setError(failure(e, "The model did not answer."));
    } finally {
      setDrafting(false);
    }
  };

  if (revisions === null) {
    return <p className="p-8 text-sm text-ink-2">{error ?? "Reading the revisions"}</p>;
  }
  if (revisions.length === 0 || drafting || failed || prompting) {
    return (
      <DraftPrompt
        busy={drafting}
        failed={failed}
        error={error}
        onDraft={draft}
        onCancel={
          revisions.length
            ? () => {
                setFailed(null);
                setPrompting(false);
              }
            : null
        }
      />
    );
  }
  if (!current) return null;

  return (
    <Suggestions.Provider
      value={new Set(revisions.filter((r) => r.kind === "suggestion").map((r) => r.id))}
    >
      <div className="grid h-full min-h-0 grid-cols-[200px_minmax(0,1fr)_minmax(360px,32%)]">
        <Rail
          revisions={revisions}
          at={at}
          onSelect={setAt}
          onNewDraft={() => setPrompting(true)}
        />
        <RevisionView
          key={current.id}
          revision={current}
          newest={at === revisions.length - 1}
          position={`${current.seq} of ${revisions.length}`}
          countOf={countOf}
          onCount={count}
          onStep={step}
          onEditText={() => onEditText(JSON.stringify(current.document, null, 2), current)}
          onImproved={(outcome) => {
            if (outcome.revision) return reload(outcome.revision.id);
            setError(
              outcome.unchanged
                ? "The model suggests no change to this part."
                : `The model's answer did not compile after ${outcome.attempts} attempts: ${(outcome.issues ?? []).map((i) => `${i.path} ${i.message}`).join("; ")}`,
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
          {current.kind === "suggestion" && current.suggestion && current.parent_id ? (
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
                  "The changes were not accepted.",
                )
              }
              onReject={() => setAt(revisions.findIndex((r) => r.id === current.parent_id))}
            />
          ) : (
            <>
              <Provenance revision={current} revisions={revisions} />
              {at < revisions.length - 1 ? (
                <div className="mt-4">
                  <Button
                    onClick={() =>
                      void act(
                        async () =>
                          (
                            await run(
                              workbench.restoreRevision({ ...caller, revisionId: current.id }),
                            )
                          ).id,
                        "The revision was not restored.",
                      )
                    }
                    keys="r"
                  >
                    Restore revision {current.seq}
                  </Button>
                </div>
              ) : null}
              <Compare revision={current} revisions={revisions} />
              <Publish
                revision={current}
                onPublish={async (reason) => {
                  const stored = await run(
                    workbench.publishRevision({ ...caller, revisionId: current.id, reason }),
                  );
                  await router.invalidate();
                  const written = current.ai_parts?.length ?? 0;
                  onPublished(
                    `Published version ${stored.version.protocol.version_label} from revision ${current.seq}. Your approval is in the ledger` +
                      (written
                        ? `, with the ${written} ${written === 1 ? "field" : "fields"} a model wrote.`
                        : "."),
                  );
                }}
              />
            </>
          )}
        </aside>
      </div>
    </Suggestions.Provider>
  );
}

function DraftPrompt({
  busy,
  failed,
  error,
  onDraft,
  onCancel,
}: {
  busy: boolean;
  failed: AssistOutcome | null;
  error: string | null;
  onDraft: (description: string) => void;
  onCancel: (() => void) | null;
}) {
  const [text, setText] = useState("");
  const seconds = useElapsed(busy);
  const issues = failed?.issues ?? [];
  return (
    <section className="h-full overflow-y-auto bg-paper">
      <div className="mx-auto max-w-[760px] px-8 pt-12 pb-16">
        <h1 className="text-xl font-semibold">Describe the review</h1>
        <p className="mt-2 max-w-[64ch] text-md text-ink-2">
          In a few sentences: the question, who is studied, what is compared with what, which
          outcomes count, which study designs, languages, and dates. A model drafts the protocol
          with a search strategy for PubMed and for ClinicalTrials.gov; you check and change every
          part before anyone screens with it.
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
              {busy ? `Drafting${elapsedLabel(seconds)}` : "Draft the protocol"}
              <span className="inline-flex gap-0.5">
                <Key k="Ctrl" />
                <Key k="Enter" />
              </span>
            </Button>
            {busy ? (
              <span className="text-sm text-ink-2">
                The model writes the whole protocol; Kittum checks it and sends any problem back.
              </span>
            ) : null}
            {onCancel && !busy ? (
              <button
                type="button"
                onClick={onCancel}
                className="ml-auto text-sm text-ink-2 hover:text-ink"
              >
                Back to the revisions
              </button>
            ) : null}
          </div>
        </form>
        {error ? (
          <p role="alert" className="mt-5 text-sm text-exclude">
            {error}
          </p>
        ) : null}
        {failed ? (
          <div className="mt-8 border-t border-rule-strong pt-4" data-testid="draft-failed">
            <h2 className="text-md font-semibold text-exclude">
              No draft: the answer still had {issues.length}{" "}
              {issues.length === 1 ? "problem" : "problems"} after {failed.attempts} attempts
            </h2>
            <p className="mt-1 text-sm text-ink-2">
              Nothing was saved. The ledger records each attempt for the disclosure.
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

function Issues({ issues }: { issues: ReadonlyArray<ProtocolIssue> }) {
  return (
    <ol className="mt-3 grid gap-2.5">
      {issues.map((issue, i) => (
        <li key={i} className="border-l-2 border-exclude pl-3 text-sm" data-testid="protocol-issue">
          {issue.path ? (
            <span className="block text-xs font-semibold text-ink-2">{issue.path}</span>
          ) : null}
          {issue.message}
        </li>
      ))}
    </ol>
  );
}

function Rail({
  revisions,
  at,
  onSelect,
  onNewDraft,
}: {
  revisions: ReadonlyArray<ProtocolRevision>;
  at: number;
  onSelect: (index: number) => void;
  onNewDraft: () => void;
}) {
  return (
    <nav
      className="flex min-h-0 flex-col border-r border-rule-strong bg-sheet-2"
      aria-label="Revisions"
    >
      <div className="flex items-center justify-between border-b border-rule px-3 py-2.5">
        <span className="text-xs font-semibold text-ink-2">Revisions</span>
        <span className="flex gap-0.5">
          <Key k="[" />
          <Key k="]" />
        </span>
      </div>
      <ol className="min-h-0 flex-1 overflow-y-auto py-1" data-testid="revision-rail">
        {[...revisions].reverse().map((r) => {
          const index = revisions.indexOf(r);
          const selected = index === at;
          const byModel = r.kind === "draft" || r.kind === "suggestion";
          return (
            <li key={r.id}>
              <button
                type="button"
                onClick={() => onSelect(index)}
                aria-current={selected ? "true" : undefined}
                data-testid="revision"
                className={`grid w-full grid-cols-[2.2rem_1fr] px-3 py-1.5 text-left text-sm ${selected ? "bg-petrol-wash" : "hover:bg-sheet"}`}
              >
                <span className={`text-ink-3 ${selected ? "font-semibold text-ink" : ""}`}>
                  {r.seq}
                </span>
                <span>
                  <span className={byModel ? "border-b border-dashed border-petrol" : ""}>
                    {KIND[r.kind]}
                  </span>
                  <span className="block text-2xs text-ink-3">
                    {new Date(r.created_at).toLocaleTimeString("en-GB", {
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                    {r.accepted ? `, ${r.accepted.change_ids.join(" ")}` : ""}
                  </span>
                </span>
              </button>
            </li>
          );
        })}
      </ol>
      <div className="border-t border-rule p-3">
        <button type="button" onClick={onNewDraft} className="text-sm text-petrol hover:underline">
          Draft anew from a description
        </button>
      </div>
    </nav>
  );
}

/** The dashed petrol rule on what a model wrote, naming the model; a caption where the lead
 * kept a model's suggestion, since a draft is the model's throughout. */
function Written({ marks, children }: { marks: ReadonlyArray<AiMark>; children: ReactNode }) {
  const suggested = useContext(Suggestions);
  const mark = marks[0];
  if (!mark) return <>{children}</>;
  const accepted = marks.find((m) => m.accepted_in && suggested.has(m.revision_id));
  return (
    <div
      className="border-l-2 border-dashed border-petrol pl-3"
      data-testid="ai-written"
      data-model={mark.recipe_id}
      data-accepted={accepted ? "true" : "false"}
      title={`Written by ${mark.recipe_id}${mark.model_identity ? ` (${mark.model_identity})` : ""}`}
    >
      {children}
      {accepted ? (
        <span className="mt-0.5 block text-2xs text-petrol">
          Suggested by {accepted.recipe_id}, accepted by you
        </span>
      ) : null}
    </div>
  );
}

/** The IDs of the model's suggestion revisions, so a mark can tell a suggestion from a draft. */
const Suggestions = createContext<ReadonlySet<string>>(new Set());

function RevisionView({
  revision,
  newest,
  position,
  countOf,
  onCount,
  onStep,
  onEditText,
  onImproved,
  onError,
}: {
  revision: ProtocolRevision;
  newest: boolean;
  position: string;
  countOf: (dialect: QueryDialect, strategy: string) => Counted | undefined;
  onCount: (dialect: QueryDialect, strategy: string) => void;
  onStep: (by: number) => void;
  onEditText: () => void;
  onImproved: (outcome: AssistOutcome) => Promise<void>;
  onError: (message: string | null) => void;
}) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me } = useProject();
  const [improving, setImproving] = useState<string | null>(null);
  const seconds = useElapsed(improving !== null);
  const d = doc(revision);
  const lang = d.default_language;
  const t = (text: Localized | undefined) =>
    text ? (text[lang] ?? Object.values(text)[0] ?? "") : "";
  const marks = revision.ai_parts ?? [];

  const improve = async (part: string) => {
    setImproving(part);
    onError(null);
    try {
      const outcome = await run(
        workbench.improvePart({
          projectId: project.id,
          as: me.actor_id,
          revisionId: revision.id,
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

  const ImproveButton = ({ part, label }: { part: string; label: string }) => (
    <button
      type="button"
      disabled={improving !== null || revision.kind === "suggestion"}
      onClick={() => void improve(part)}
      aria-label={`Improve ${label}`}
      className="shrink-0 rounded-[3px] border border-rule-strong bg-sheet px-2 py-0.5 text-xs text-ink-2 hover:border-petrol hover:text-petrol disabled:cursor-not-allowed disabled:opacity-50"
    >
      {improving === part ? `Asking the model${elapsedLabel(seconds)}` : "Improve"}
    </button>
  );

  return (
    <section className="min-h-0 overflow-y-auto bg-sheet" aria-label="Revision">
      <header className="sticky top-0 z-10 flex items-center gap-3 border-b border-rule bg-sheet/95 px-8 py-2.5 text-sm backdrop-blur">
        <span className="font-semibold">Revision {position}</span>
        <span className="text-ink-2">{KIND[revision.kind]}</span>
        {!newest ? <span className="text-unclear">An earlier revision</span> : null}
        <span className="ml-auto flex items-center gap-4">
          <button type="button" onClick={() => onStep(-1)} className="text-ink-2 hover:text-ink">
            <KeyHint keys={["["]}>Back</KeyHint>
          </button>
          <button type="button" onClick={() => onStep(1)} className="text-ink-2 hover:text-ink">
            <KeyHint keys={["]"]}>Forward</KeyHint>
          </button>
          <button type="button" onClick={onEditText} className="text-ink-2 hover:text-ink">
            Edit as text
          </button>
        </span>
      </header>
      <article className="max-w-[860px] px-8 pt-6 pb-16" data-testid="revision-view">
        <Written marks={marksOn(marks, "title")}>
          <h1 className="text-xl font-semibold text-balance">{t(d.title)}</h1>
        </Written>
        <p className="mt-1 text-xs text-ink-3" title={revision.content_hash}>
          Version label {d.version_label}, content hash {revision.content_hash.slice(0, 12)}
        </p>

        <h2 className="mt-8 border-t border-rule-strong pt-3 text-lg font-semibold">Questions</h2>
        <ol className="mt-3 grid gap-5">
          {d.questions.map((q) => {
            const part = `questions[${q.id}]`;
            return (
              <li key={q.id}>
                <Written marks={marksOn(marks, part)}>
                  <div className="flex items-baseline gap-3">
                    <h3 className="text-md font-semibold">{t(q.label)}</h3>
                    <span className="ml-auto">
                      <ImproveButton part={part} label={`question ${q.id}`} />
                    </span>
                  </div>
                  <dl className="mt-1 grid grid-cols-[7.5rem_minmax(0,1fr)] gap-x-4 gap-y-0.5 text-sm">
                    <dt className="text-right text-xs font-semibold text-ink-2">Population</dt>
                    <dd>{t(q.population)}</dd>
                    <dt className="text-right text-xs font-semibold text-ink-2">Intervention</dt>
                    <dd>{t(q.intervention)}</dd>
                    {q.comparators.length ? (
                      <>
                        <dt className="text-right text-xs font-semibold text-ink-2">Comparators</dt>
                        <dd>{q.comparators.map((c) => t(c.text)).join("; ")}</dd>
                      </>
                    ) : null}
                  </dl>
                </Written>
              </li>
            );
          })}
        </ol>

        <h2 className="mt-8 border-t border-rule-strong pt-3 text-lg font-semibold">Outcomes</h2>
        <ul className="mt-2 grid gap-1 text-sm">
          {d.outcomes.map((o) => (
            <li key={o.id}>
              <Written marks={marksOn(marks, `outcomes[${o.id}]`)}>
                {t(o.name)} <span className="text-ink-3">{o.category}</span>
              </Written>
            </li>
          ))}
        </ul>

        {d.search_topics.map((topic, ti) => (
          <section key={topic.id}>
            <h2 className="mt-8 border-t border-rule-strong pt-3 text-lg font-semibold">
              {t(topic.label)} <span className="text-sm font-normal text-ink-3">{topic.mode}</span>
            </h2>
            <h3 className="mt-4 text-md font-semibold">Search strategies</h3>
            <ul className="mt-2 grid gap-4">
              {topic.source_plan.sources.map((s, si) => {
                const dialect = dialectOf(s);
                const part = strategyPath(topic, s);
                const counted = dialect && s.strategy ? countOf(dialect, s.strategy) : undefined;
                // What may be wrong though it compiles, and what the source said when it counted.
                const notes = [
                  ...(revision.warnings ?? [])
                    .filter(
                      (w) => w.path === `search_topics[${ti}].source_plan.sources[${si}].strategy`,
                    )
                    .map((w) => w.message),
                  ...(counted?.state === "done" ? (counted.count.warnings ?? []) : []),
                ];
                return (
                  <li key={s.id} data-testid="source" data-source={s.id}>
                    <Written marks={marksOn(marks, part)}>
                      <div className="flex items-baseline gap-3">
                        <span className="text-sm font-semibold">{s.name}</span>
                        <span className="text-xs text-ink-3">{s.platform ?? s.kind}</span>
                        {s.strategy ? (
                          <span className="ml-auto flex items-center gap-2">
                            {dialect ? (
                              <CountButton
                                counted={countOf(dialect, s.strategy)}
                                onCount={() => onCount(dialect, s.strategy ?? "")}
                                dialect={dialect}
                              />
                            ) : null}
                            <ImproveButton part={part} label={`${s.name} strategy`} />
                          </span>
                        ) : null}
                      </div>
                      {s.strategy ? (
                        <>
                          <pre
                            className="mt-1 text-[12.5px] leading-[19px] whitespace-pre-wrap text-ink"
                            data-testid="strategy"
                          >
                            {s.strategy}
                          </pre>
                          <Notes notes={notes} />
                        </>
                      ) : (
                        <p className="mt-0.5 text-xs text-ink-3">
                          No strategy: searched on its own platform.
                        </p>
                      )}
                    </Written>
                  </li>
                );
              })}
            </ul>
            <h3 className="mt-6 text-md font-semibold">Criteria</h3>
            <ul className="mt-2 grid gap-3">
              {topic.criteria.map((c) => {
                const part = `search_topics[${topic.id}].criteria[${c.id}]`;
                return (
                  <li key={c.id}>
                    <Written marks={marksOn(marks, part)}>
                      <div className="flex items-baseline gap-3 text-sm">
                        <span className="w-[8.5rem] shrink-0 text-xs text-ink-2">
                          {SLOTS[c.slot] ?? c.slot},{" "}
                          {c.polarity === "include" ? "inclusion" : "exclusion"}
                          <span className="block text-ink-3">{c.id}</span>
                        </span>
                        <span className="min-w-0 flex-1">{t(c.text)}</span>
                        <ImproveButton part={part} label={`criterion ${c.id}`} />
                      </div>
                    </Written>
                  </li>
                );
              })}
            </ul>
          </section>
        ))}
      </article>
    </section>
  );
}

/** What a source or the compiler said about a strategy: a word it found nowhere, a tag it does
 * not know. Not problems that stop anything, but usually a typo. */
function Notes({ notes }: { notes: ReadonlyArray<string> }) {
  if (!notes.length) return null;
  return (
    <ul className="mt-1 grid gap-0.5 text-xs text-unclear" data-testid="strategy-notes">
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
    <span className="flex items-baseline gap-2 text-xs">
      {counted?.state === "done" ? (
        <span data-testid="hit-count" title={counted.count.query_translation ?? undefined}>
          <span className="text-sm font-semibold text-ink">
            {counted.count.count.toLocaleString("en-GB")}
          </span>{" "}
          <span className="text-ink-2">in {SOURCE_NAME[dialect]} today</span>
        </span>
      ) : counted?.state === "failed" ? (
        <span className="text-exclude">{counted.message}</span>
      ) : null}
      <button
        type="button"
        onClick={onCount}
        disabled={counted?.state === "counting"}
        className="rounded-[3px] border border-rule-strong bg-sheet px-2 py-0.5 text-ink-2 hover:border-petrol hover:text-petrol disabled:opacity-50"
      >
        {counted?.state === "counting" ? "Counting" : "Count"}
      </button>
    </span>
  );
}

/** The strategy as a proofreader marks it: each change at its place, numbered, with the chosen
 * ones struck and inserted and the others left as they are. */
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
    for (const t of doc(base).search_topics)
      for (const s of t.source_plan.sources) if (strategyPath(t, s) === suggestion.part) return s;
    return null;
  })();
  const dialect = strategySource ? dialectOf(strategySource) : null;
  const before = strategySource?.strategy ?? "";
  const variants: ReadonlyArray<readonly [string, string]> = strategySource
    ? [
        ["Now", before],
        ["With every change", applyChosen(before, changes, new Set(changes.map((c) => c.id)))],
        ["With your choice", applyChosen(before, changes, chosen)],
      ]
    : [];
  const model = revision.model;

  return (
    <div data-testid="suggestion">
      <h2 className="text-md font-semibold">
        {changes.length} suggested {changes.length === 1 ? "change" : "changes"} to the{" "}
        {partName(base, suggestion.part)}
      </h2>
      <p className="mt-1 text-xs text-ink-2">
        By {model?.recipe_id}{" "}
        {model?.model_identity && model.model_identity !== model.recipe_id
          ? `(${model.model_identity})`
          : ""}
        {model?.endpoint ? ` at ${model.endpoint}` : ""}. Take each change or leave it.
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
                {c.rationale || "No reason given."}
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
                <Key k={String(i + 1)} /> to toggle
              </span>
            </label>
          </li>
        ))}
      </ol>

      {dialect ? (
        <div className="mt-5" data-testid="counts">
          <div className="flex items-baseline justify-between">
            <h3 className="text-sm font-semibold">Hits in {SOURCE_NAME[dialect]} today</h3>
            <button
              type="button"
              onClick={() => variants.forEach(([, s]) => onCount(dialect, s))}
              className="text-xs text-petrol hover:underline"
            >
              Count all three
            </button>
          </div>
          <table className="mt-1.5 w-full text-sm">
            <tbody>
              {variants.map(([label, s]) => {
                const counted = countOf(dialect, s);
                const notes = counted?.state === "done" ? (counted.count.warnings ?? []) : [];
                return (
                  <tr key={label} className="border-b border-rule align-baseline">
                    <td className="py-1 text-ink-2">
                      {label}
                      <Notes notes={notes} />
                    </td>
                    <td
                      className="py-1 text-right font-semibold"
                      data-testid={`count-${label.split(" ").pop()}`}
                    >
                      {counted?.state === "done"
                        ? counted.count.count.toLocaleString("en-GB")
                        : counted?.state === "counting"
                          ? "counting"
                          : counted?.state === "failed"
                            ? "failed"
                            : ""}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p className="mt-1 text-2xs text-ink-3">
            Counts inform; they run no search and record none.
          </p>
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
}: {
  revision: ProtocolRevision;
  revisions: ReadonlyArray<ProtocolRevision>;
}) {
  const seqOf = (id: string | null | undefined) => revisions.find((r) => r.id === id)?.seq;
  const model = revision.model;
  const written = revision.ai_parts ?? [];
  return (
    <section data-testid="provenance">
      <h2 className="text-md font-semibold">Revision {revision.seq}</h2>
      <p className="mt-1 text-sm text-ink-2">
        {revision.kind === "draft"
          ? "A model drafted this protocol from your description."
          : revision.kind === "accepted" && revision.accepted
            ? `You accepted ${revision.accepted.change_ids.length} change${revision.accepted.change_ids.length === 1 ? "" : "s"} of revision ${seqOf(revision.accepted.suggestion_id)} onto revision ${seqOf(revision.parent_id)}.`
            : revision.kind === "edit"
              ? "Your own text."
              : revision.kind === "restore"
                ? `Revision ${seqOf(revision.parent_id)}, restored.`
                : "A model's suggestion."}
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
            {model.endpoint ? `${model.serving} at ${model.endpoint}` : "Replayed from a recording"}
          </dd>
          <dt className="text-ink-3">Prompt</dt>
          <dd title={model.prompt_hash}>
            {model.prompt_version}, SHA-256 {model.prompt_hash.slice(0, 12)}
          </dd>
          <dt className="text-ink-3">Attempts</dt>
          <dd>
            <ol className="grid gap-1">
              {model.attempts.map((a, i) => (
                <li key={i} data-testid="attempt">
                  {(a.issues ?? []).length ? (
                    <span className="text-exclude">
                      {i + 1}: sent back, {(a.issues ?? []).map((x) => x.message).join("; ")}
                    </span>
                  ) : (
                    <span className="text-include">{i + 1}: compiled</span>
                  )}
                  <span className="block text-ink-3" title={a.raw_response_hash ?? undefined}>
                    raw answer {a.raw_response_hash?.slice(0, 12) ?? "none"}
                  </span>
                </li>
              ))}
            </ol>
          </dd>
        </dl>
      ) : null}
      <p className="mt-3 text-xs text-ink-2" data-testid="written-count">
        {written.length
          ? `${written.length} ${written.length === 1 ? "field" : "fields"} written by a model, marked with a dashed rule.`
          : "No field written by a model."}
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
        {changes.length === 0 ? <li className="text-ink-2">The same protocol.</li> : null}
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
          <li className="text-ink-2">and {changes.length - 40} more</li>
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
          .catch((x) => setError(failure(x, "The protocol was not published.")))
          .finally(() => setBusy(false));
      }}
    >
      <Field
        label="Reason"
        hint="Why this version, in a sentence. The ledger keeps it with your approval and the fields a model wrote."
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
