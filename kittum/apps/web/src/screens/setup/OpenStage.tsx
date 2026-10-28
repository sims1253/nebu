// Opens a screening stage for one scope. The protocol sets the mode and the audit plan; the lead
// chooses when the AI is revealed and gives the sentinels, the records with known outcomes that
// enter every queue unmarked. Who screens follows from the mode and the project's people.
import { useRouteContext, useRouter } from "@tanstack/react-router";
import { useState } from "react";

import type { RevealSetting, Role, SearchTopic, Sentinel, Stage } from "../../data/schema.ts";
import { run } from "../../data/workbench.ts";
import { Button, Field, FileInput, Select, failure } from "../../ui/Form.tsx";
import { MODES } from "../protocol/Protocol.tsx";
import { useProject } from "../project.tsx";

const STAGES: Record<Stage, string> = {
  title_abstract: "Title and abstract",
  full_text: "Full text",
};

const REVEALS: Record<RevealSetting, string> = {
  on_disagreement:
    "After a reviewer's own decision, and only when the AI reads the record differently (the default)",
  always: "After every decision of a reviewer's own",
  never: "Never: the AI reads beside the reviewers but they never see its reading",
};

const names = (people: ReadonlyArray<string>) =>
  people.length <= 1
    ? (people[0] ?? "nobody")
    : `${people.slice(0, -1).join(", ")} and ${people.at(-1)}`;

export function OpenStage({ onOpened }: { onOpened: (message: string) => void }) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const router = useRouter();
  const { project, me, protocol, labels, scopeLabel } = useProject();
  const topics = protocol?.protocol.topics ?? [];
  const open = (scopeId: string, stage: Stage) =>
    project.stages.some((s) => s.scope_id === scopeId && s.stage === stage);
  const choices = topics.flatMap((topic) =>
    topic.scopes.flatMap((scope) =>
      (["title_abstract", "full_text"] as const)
        .filter((stage) =>
          stage === "title_abstract"
            ? !open(scope.id, stage)
            : open(scope.id, "title_abstract") && !open(scope.id, stage),
        )
        .map((stage) => ({ topic, scopeId: scope.id, stage, key: `${scope.id}|${stage}` })),
    ),
  );
  const [choice, setChoice] = useState(choices[0]?.key ?? "");
  const [reveal, setReveal] = useState<RevealSetting>("on_disagreement");
  const [sentinels, setSentinels] = useState<ReadonlyArray<Sentinel> | null>(null);
  const [sentinelError, setSentinelError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const chosen = choices.find((c) => c.key === choice) ?? choices[0];
  if (!protocol) {
    return <p className="text-ink-2">Publish the protocol first: it sets each scope's mode.</p>;
  }
  if (!chosen) {
    return (
      <p className="text-ink-2">
        Every {labels.scope.toLowerCase()} is open at title and abstract. Full-text screening opens
        for a {labels.scope.toLowerCase()} here or on Progress once its full texts are in.
      </p>
    );
  }

  const readSentinels = async (file: File | null) => {
    setSentinels(null);
    setSentinelError(null);
    if (!file) return;
    try {
      const text = await file.text();
      setSentinels(
        await run(workbench.parseSentinels({ projectId: project.id, as: me.actor_id, text })),
      );
    } catch (e) {
      setSentinelError(failure(e, "The sentinel file could not be read."));
    }
  };

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await run(
        workbench.openStage({
          projectId: project.id,
          as: me.actor_id,
          scopeId: chosen.scopeId,
          stage: chosen.stage,
          reveal,
          ...(chosen.stage === "title_abstract" && sentinels ? { sentinels } : {}),
        }),
      );
      await router.invalidate();
      setSentinels(null);
      onOpened(
        `${STAGES[chosen.stage]} screening is open for ${scopeLabel(chosen.scopeId)}. The AI judge reads every record first; the reviewers' queues fill as it reads.`,
      );
    } catch (e) {
      setError(failure(e, "The stage did not open."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="grid max-w-[64ch] gap-4"
      aria-label="Open a screening stage"
      onSubmit={(e) => {
        e.preventDefault();
        if (!busy) void submit();
      }}
    >
      <Field label={`${labels.scope} and stage`}>
        <Select value={chosen.key} onChange={(e) => setChoice(e.currentTarget.value)}>
          {choices.map((c) => (
            <option key={c.key} value={c.key}>
              {scopeLabel(c.scopeId)}, {STAGES[c.stage].toLowerCase()}
            </option>
          ))}
        </Select>
      </Field>
      <StagePlan topic={chosen.topic} />
      <Field label="When reviewers see the AI's reading">
        <Select value={reveal} onChange={(e) => setReveal(e.currentTarget.value as RevealSetting)}>
          {(Object.keys(REVEALS) as RevealSetting[]).map((r) => (
            <option key={r} value={r}>
              {REVEALS[r]}
            </option>
          ))}
        </Select>
      </Field>
      {chosen.stage === "title_abstract" ? (
        <Field
          label="Sentinels"
          hint={
            Number(chosen.topic.sentinel_percent) > 0
              ? `The protocol seeds ${chosen.topic.sentinel_percent}% of each queue with sentinels: a YAML or JSON list, as in examples/sentinels/. Reviewers cannot tell them from other records.`
              : "Optional: the protocol asks for none. A YAML or JSON list, as in examples/sentinels/."
          }
        >
          <FileInput
            label="Sentinel file"
            accept=".yaml,.yml,.json"
            onFile={(file) => void readSentinels(file)}
          />
        </Field>
      ) : (
        <p className="text-sm text-ink-2">
          Full-text stages draw their sentinels from the full-text sentinels added on Retrieval.
        </p>
      )}
      {sentinelError ? (
        <p role="alert" className="text-sm text-exclude">
          {sentinelError}
        </p>
      ) : sentinels ? (
        <div className="text-sm" data-testid="sentinel-preview">
          <p className="text-ink-2">
            {sentinels.length} {sentinels.length === 1 ? "sentinel" : "sentinels"}, expected to be{" "}
            {sentinels.filter((s) => (s.expected_outcome ?? "include") === "include").length}{" "}
            included and {sentinels.filter((s) => s.expected_outcome === "exclude").length}{" "}
            excluded:
          </p>
          <ul className="mt-1 grid gap-0.5 border-l-2 border-rule pl-3 text-ink-2">
            {sentinels.map((s) => (
              <li key={s.record.item_ref}>{s.record.title}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {error ? (
        <p role="alert" className="text-sm text-exclude" data-testid="form-error">
          {error}
        </p>
      ) : null}
      <div>
        <Button primary type="submit" disabled={busy}>
          {busy ? "Opening the stage" : `Open ${STAGES[chosen.stage].toLowerCase()} screening`}
        </Button>
      </div>
    </form>
  );
}

/** What the protocol decides for the stage, and who it puts on it. */
function StagePlan({ topic }: { topic: SearchTopic }) {
  const { project } = useProject();
  const people = (role: Role) =>
    project.members.filter((m) => m.roles.includes(role)).map((m) => m.display_name);
  const needed = topic.mode === "M2" ? 1 : 2;
  const reviewers = people("reviewer");
  const auditors = people("auditor");
  const adjudicators = people("adjudicator");
  const short = reviewers.length < needed || !auditors.length || !adjudicators.length;
  return (
    <dl className="grid grid-cols-[7.5rem_minmax(0,1fr)] gap-x-4 gap-y-1 border-y border-rule py-3 text-sm">
      <dt className="text-xs font-semibold text-ink-2">Mode</dt>
      <dd>
        <span className="font-semibold">{topic.mode}</span>{" "}
        <span className="text-ink-2">{MODES[topic.mode]}</span>
      </dd>
      <dt className="text-xs font-semibold text-ink-2">Audit</dt>
      <dd className="text-ink-2">
        {topic.sentinel_percent}% sentinels, {topic.random_audit_percent}% re-screened by the
        auditor
      </dd>
      <dt className="text-xs font-semibold text-ink-2">People</dt>
      <dd className={short ? "text-exclude" : "text-ink-2"} data-testid="stage-people">
        {short
          ? `${topic.mode} needs ${needed === 1 ? "a reviewer" : "two reviewers"}, an auditor, and an adjudicator. Add them under People.`
          : `${names(reviewers.slice(0, needed))} ${needed === 1 ? "screens" : "screen"}; ${names(auditors.slice(0, 1))} audits; ${names(adjudicators)} ${adjudicators.length === 1 ? "adjudicates" : "adjudicate"}.`}
      </dd>
    </dl>
  );
}
