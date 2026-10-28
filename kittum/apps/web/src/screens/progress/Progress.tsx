// Progress (DESIGN.md, "Reviewer workbench"): the live PRISMA flow, and for the methods lead the
// agreement, the extra screens, and the audit gauge. Screeners see only their own counts, as
// the server shows them; nothing here is computed from data the viewer may not see.
import { useRouteContext, useRouter } from "@tanstack/react-router";
import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";

import type { LedgerSummary, Progress, StageInfo, StageProgressView } from "../../data/schema.ts";
import { run, WorkbenchError } from "../../data/workbench.ts";
import { KeyHint } from "../../ui/Key.tsx";
import { useKeys } from "../../ui/keys.ts";
import { useProject } from "../project.tsx";

const SOURCES: Record<string, string> = {
  pubmed: "PubMed",
  central: "CENTRAL",
  ctgov: "ClinicalTrials.gov",
  citation_search: "Citation search",
};

const STAGE_LABEL = { title_abstract: "Title and abstract", full_text: "Full text" } as const;

export function ProgressScreen() {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const router = useRouter();
  const { project, me, has, labels, scopeLabel } = useProject();
  const [progress, setProgress] = useState<Progress | null>(null);
  const [ledger, setLedger] = useState<LedgerSummary | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const lead = has("lead");

  useEffect(() => {
    let live = true;
    run(workbench.progress({ projectId: project.id, as: me.actor_id })).then(
      (p) => live && setProgress(p),
      (e: unknown) =>
        live && setNotice(e instanceof WorkbenchError ? e.message : "Progress could not load."),
    );
    if (lead && workbench.live) {
      run(workbench.ledger(project.id)).then(
        (l) => live && setLedger(l),
        () => live && setLedger(null),
      );
    }
    return () => {
      live = false;
    };
  }, [workbench, project.id, me.actor_id, lead, tick]);

  // Scopes screened at title and abstract whose full text is not open yet.
  const openable = lead
    ? project.stages.filter(
        (s) =>
          s.stage === "title_abstract" &&
          !project.stages.some((f) => f.stage === "full_text" && f.scope_id === s.scope_id),
      )
    : [];

  const openFullText = useCallback(
    async (stage: StageInfo) => {
      setNotice(null);
      try {
        await run(
          workbench.openStage({
            projectId: project.id,
            as: me.actor_id,
            scopeId: stage.scope_id,
            stage: "full_text",
          }),
        );
        // The project's stages changed: load them again.
        await router.invalidate();
        setTick((t) => t + 1);
      } catch (e) {
        setNotice(e instanceof WorkbenchError ? e.message : "Full-text screening did not open.");
      }
    },
    [workbench, router, project.id, me.actor_id],
  );

  useKeys((event) => {
    if (event.key === "n" && openable[0]) {
      void openFullText(openable[0]);
      return true;
    }
    if (event.key === "r") {
      setTick((t) => t + 1);
      return true;
    }
    return false;
  });

  if (!progress) {
    return <Shell>{notice ? <p role="alert">{notice}</p> : <p>Loading progress.</p>}</Shell>;
  }
  const byStage = (stage: "title_abstract" | "full_text") =>
    progress.stages.filter((s) => s.stage.stage === stage);
  const names = new Map(project.members.map((m) => [m.actor_id, m.display_name]));

  return (
    <Shell>
      <header className="flex items-baseline gap-4">
        <h1 className="text-xl font-semibold tracking-[-0.01em]">Progress</h1>
        <span className="text-sm text-ink-2">{lead ? "The whole review" : "Your own work"}</span>
        <span className="ml-auto flex gap-5 text-sm">
          <KeyHint keys={["r"]}>Refresh</KeyHint>
          {openable[0] ? (
            <KeyHint keys={["n"]}>
              Open full-text screening, {labels.scope.toLowerCase()}{" "}
              {scopeLabel(openable[0].scope_id)}
            </KeyHint>
          ) : null}
        </span>
      </header>
      {notice ? (
        <p role="alert" className="mt-3 text-sm text-exclude">
          {notice}
        </p>
      ) : null}
      <div className="mt-8 grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-x-14">
        <section aria-label="PRISMA flow">
          <h2 className="text-sm font-semibold text-ink-2">Flow of records</h2>
          <Flow progress={progress} byStage={byStage} lead={lead} />
        </section>
        <section aria-label="Quality">
          {lead ? (
            progress.stages.map((s) => (
              <StageQuality key={s.stage.stage_id} view={s} names={names} />
            ))
          ) : (
            <p className="max-w-[48ch] text-sm text-ink-2">
              Agreement, the extra screens, and the audit gauge are the methods lead&rsquo;s.
              Reviewers see only their own counts, so nothing here can hint at another
              person&rsquo;s decisions.
            </p>
          )}
          {ledger ? <LedgerLine ledger={ledger} /> : null}
        </section>
      </div>
    </Shell>
  );
}

function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="h-full overflow-y-auto bg-paper">
      <div className="mx-auto max-w-[1180px] px-10 pt-8 pb-14">{children}</div>
    </div>
  );
}

/**
 * The PRISMA 2020 flow as a column of steps: each step a number on a rule, the records that
 * leave the flow set off to the right of the step they leave at.
 */
function Flow({
  progress,
  byStage,
  lead,
}: {
  progress: Progress;
  byStage: (stage: "title_abstract" | "full_text") => ReadonlyArray<StageProgressView>;
  lead: boolean;
}) {
  const id = progress.identification;
  const steps: Array<{
    key: string;
    label: string;
    value: number | null;
    side?: ReactNode;
    detail?: ReactNode;
  }> = [];
  if (id) {
    steps.push({
      key: "identified",
      label: "Records identified",
      value: id.total_records_identified,
      detail: (
        <span className="text-ink-3">
          {Object.entries(id.records_identified_per_source)
            .map(([source, n]) => `${SOURCES[source] ?? source} ${n}`)
            .join(", ")}
          {`, from ${progress.searches} ${progress.searches === 1 ? "search" : "searches"}`}
        </span>
      ),
      side: <Side label="Duplicates removed" value={id.duplicates_removed} testId="duplicates" />,
    });
  }
  for (const stage of ["title_abstract", "full_text"] as const) {
    for (const view of byStage(stage)) {
      const o = view.outcomes;
      steps.push({
        key: `${stage}-${view.stage.scope_id}-screened`,
        label: `${STAGE_LABEL[stage]}${lead ? ", records screened" : ", your screens"}`,
        value: view.status.screened + view.status.remaining,
        detail: (
          <span className="text-ink-3">
            {view.status.screened} done, {view.status.remaining} to go
            {view.status.conflicts !== null ? `, ${view.status.conflicts} open conflicts` : ""}
          </span>
        ),
        side: o ? (
          <>
            <Side
              label="Excluded"
              value={o["exclude"] ?? 0}
              testId={`${stage}-${view.stage.scope_id}-excluded`}
            />
            <Side
              label="In conflict"
              value={o["conflict"] ?? 0}
              testId={`${stage}-${view.stage.scope_id}-conflict`}
            />
            <Side
              label="Not yet decided"
              value={o["pending"] ?? 0}
              testId={`${stage}-${view.stage.scope_id}-pending`}
            />
          </>
        ) : null,
      });
      if (o) {
        steps.push({
          key: `${stage}-${view.stage.scope_id}-included`,
          label: stage === "title_abstract" ? "Included for full text" : "Included in the review",
          value: o["include"] ?? 0,
        });
      }
    }
  }
  if (lead) {
    steps.push({ key: "full-texts", label: "Full texts acquired", value: progress.full_texts });
  }
  if (id) {
    steps.push({ key: "studies", label: "Studies identified", value: id.studies_identified });
  }
  if (steps.length === 0) {
    return <p className="mt-3 text-sm text-ink-2">No searches are recorded yet.</p>;
  }
  return (
    <ol className="mt-3">
      {steps.map((step) => (
        <li
          key={step.key}
          className="grid grid-cols-[6.5rem_minmax(0,1fr)_minmax(0,13rem)] items-start gap-x-5 border-t border-rule py-3"
        >
          <span
            className="text-right text-[2rem] leading-none font-semibold tracking-[-0.02em] tabular-nums"
            data-testid={`flow-${step.key}`}
          >
            {step.value ?? "None"}
          </span>
          <span className="pt-1">
            <span className="block text-md">{step.label}</span>
            {step.detail ? <span className="mt-0.5 block text-xs">{step.detail}</span> : null}
          </span>
          <span
            className={`grid gap-1 pt-1 ${step.side ? "border-l border-rule-strong pl-4" : ""}`}
          >
            {step.side}
          </span>
        </li>
      ))}
    </ol>
  );
}

function Side({ label, value, testId }: { label: string; value: number; testId: string }) {
  return (
    <span className="flex items-baseline justify-between gap-3 text-sm">
      <span className="text-ink-2">{label}</span>
      <span className="font-semibold tabular-nums" data-testid={testId}>
        {value}
      </span>
    </span>
  );
}

const fixed = (n: number | null) => (n === null ? "Undefined" : n.toFixed(2));

function StageQuality({ view, names }: { view: StageProgressView; names: Map<string, string> }) {
  const metrics = view.status.metrics;
  if (!metrics) return null;
  const rater = (id: string) => (id === "ai" ? "AI judge" : (names.get(id) ?? "Someone"));
  const audit = metrics.audit;
  const kinds = {
    human_human: "People",
    human_ai: "Person and AI, model's reading",
    human_ai_verified: "Person and AI, verified reading",
  } as const;
  return (
    <section className="mb-10" aria-label={`${STAGE_LABEL[view.stage.stage]} quality`}>
      <h2 className="text-sm font-semibold text-ink-2">
        {STAGE_LABEL[view.stage.stage]}, {view.stage.mode}, reveal{" "}
        {view.stage.reveal.replace("_", " ")}
      </h2>
      <table className="narrow mt-3 w-full text-sm tabular-nums">
        <thead className="text-left text-xs text-ink-3">
          <tr className="border-b border-rule">
            <th className="py-1 font-normal">Agreement on blind decisions</th>
            <th className="py-1 font-normal">Records</th>
            <th className="py-1 font-normal">Kappa</th>
            <th className="py-1 font-normal">PABAK</th>
          </tr>
        </thead>
        <tbody>
          {metrics.agreement.map((pair) => (
            <tr key={`${pair.kind}-${pair.raters.join()}`} className="border-b border-rule">
              <td className="py-1.5">
                {rater(pair.raters[0])} and {rater(pair.raters[1])}
                <span className="block text-xs text-ink-3">{kinds[pair.kind]}</span>
              </td>
              <td>{pair.items}</td>
              <td data-testid={pair.kind === "human_human" ? "kappa" : undefined}>
                {fixed(pair.kappa)}
              </td>
              <td>{fixed(pair.pabak)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <dl className="mt-5 grid grid-cols-[minmax(0,1fr)_auto] gap-x-6 gap-y-1.5 text-sm tabular-nums">
        <dt className="text-ink-2">AI readings shown after a decision</dt>
        <dd>{metrics.reveals_shown}</dd>
        <dt className="text-ink-2">Decisions revised after one</dt>
        <dd>{metrics.revised_after_reveal}</dd>
        <dt className="text-ink-2">Conflicts adjudicated</dt>
        <dd data-testid={`${view.stage.stage}-adjudicated`}>{metrics.conflicts_resolved}</dd>
        <dt className="text-ink-2">Extra screens of AI-only disagreements</dt>
        <dd data-testid={`${view.stage.stage}-rescreens`}>
          {metrics.rescreens.triggered} sent, {metrics.rescreens.pending} waiting,{" "}
          {metrics.rescreens.upheld} upheld, {metrics.rescreens.adjudicated} adjudicated
        </dd>
        <dt className="text-ink-2">Sentinels screened correctly</dt>
        <dd>
          {audit.sentinels_correct} of {audit.sentinels_decided} decided ({audit.sentinels} placed)
        </dd>
        <dt className="text-ink-2">Audit sample screened</dt>
        <dd data-testid={`${view.stage.stage}-audit`}>
          {audit.audit_decided} of {audit.audit_samples}
        </dd>
      </dl>
      <AuditGauge view={view} />
    </section>
  );
}

/** Estimated relevant records among the excluded ones, with the one-sided 95% upper bound. */
function AuditGauge({ view }: { view: StageProgressView }) {
  const audit = view.status.metrics?.audit;
  if (!audit) return null;
  const population = Math.max(audit.excluded_population, 1);
  const pct = (n: number) => `${Math.min(100, (n / population) * 100)}%`;
  return (
    <figure className="mt-5" aria-label="Audit gauge">
      <figcaption className="text-xs text-ink-2">
        Missed relevant records among {audit.excluded_population} excluded
      </figcaption>
      {audit.elusion ? (
        <>
          <div className="relative mt-2 h-2 bg-sheet-2" aria-hidden="true">
            <div
              className="absolute inset-y-0 left-0 bg-petrol-wash"
              style={{ width: pct(audit.elusion.upper) }}
            />
            <div
              className="absolute inset-y-[-3px] w-[2px] bg-petrol"
              style={{ left: pct(audit.elusion.estimate) }}
            />
          </div>
          <p className="mt-1.5 text-sm tabular-nums">
            Estimate {audit.elusion.estimate.toFixed(1)}, at most {audit.elusion.upper.toFixed(1)}{" "}
            (one-sided {Math.round(audit.elusion.confidence * 100)}%)
          </p>
        </>
      ) : (
        <p className="mt-1.5 text-sm text-ink-2">
          Not estimable yet: a stratum with excluded records has no screened audit sample.
        </p>
      )}
    </figure>
  );
}

function LedgerLine({ ledger }: { ledger: LedgerSummary }) {
  return (
    <p className="mt-2 border-t border-rule pt-3 text-sm" data-testid="ledger">
      {ledger.verify.ok
        ? `Ledger verified: ${ledger.events} events, the hash chain holds through event ${ledger.verify.checked_through_seq}.`
        : `Ledger FAILED at event ${ledger.verify.first_bad_seq}: ${ledger.verify.reason ?? ledger.verify.problem}.`}
    </p>
  );
}
