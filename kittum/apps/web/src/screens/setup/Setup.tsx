// Setting up a review (DESIGN.md, "The pipeline"): the methods lead's steps from the people to
// extraction, in the order the review needs them. Each step says what is done, from the server's
// own records, and what comes next; the work itself happens here or on the step's screen.
import { Checkbox } from "@base-ui/react/checkbox";
import { useNavigate, useRouteContext, useRouter } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

import type { Progress, Role } from "../../data/schema.ts";
import { run } from "../../data/workbench.ts";
import { Button, Field, TextInput, failure } from "../../ui/Form.tsx";
import { Key, KeyHint } from "../../ui/Key.tsx";
import { useKeys } from "../../ui/keys.ts";
import { plural, ROLE_LABELS, useProject } from "../project.tsx";
import { BuildCorpus } from "./BuildCorpus.tsx";
import { OpenStage } from "./OpenStage.tsx";

type StepId =
  | "people"
  | "protocol"
  | "searches"
  | "corpus"
  | "screening"
  | "retrieval"
  | "extraction";

type Screen =
  | "/p/$projectId/protocol"
  | "/p/$projectId/search"
  | "/p/$projectId/studies"
  | "/p/$projectId/retrieval"
  | "/p/$projectId/extraction"
  | "/p/$projectId/progress";

interface Step {
  readonly id: StepId;
  readonly title: string;
  readonly done: boolean;
  readonly summary: string;
  readonly screen?: { readonly to: Screen; readonly label: string };
}

const ROLES: ReadonlyArray<Role> = ["reviewer", "auditor", "adjudicator", "lead"];

const date = (iso: string | null | undefined) =>
  iso
    ? new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" })
    : "";

export function SetupScreen() {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const navigate = useNavigate();
  const { project, me, protocol, has, seesLeadData, labels, scopeLabel } = useProject();
  const [progress, setProgress] = useState<Progress | null>(null);
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [chosen, setChosen] = useState<StepId | null>(null);

  useEffect(() => {
    if (!seesLeadData) return;
    let live = true;
    run(workbench.progress({ projectId: project.id, as: me.actor_id })).then(
      (p) => live && setProgress(p),
      () => live && setProgress(null),
    );
    return () => {
      live = false;
    };
  }, [workbench, project.id, me.actor_id, seesLeadData, tick, project.stages.length]);

  const topics = protocol?.protocol.topics ?? [];
  const needed = topics.some((t) => t.mode !== "M2") || topics.length === 0 ? 2 : 1;
  const count = (role: Role) => project.members.filter((m) => m.roles.includes(role)).length;
  const reviewers = count("reviewer");
  const peopleDone = reviewers >= needed && count("auditor") > 0 && count("adjudicator") > 0;
  const searches = progress?.searches ?? 0;
  const identified = progress?.identification;
  const taOpen = project.stages.filter((s) => s.stage === "title_abstract");

  const steps: Step[] = [
    {
      id: "people",
      title: "People",
      done: peopleDone,
      summary: peopleDone
        ? `${project.members.length} people: ${reviewers} ${reviewers === 1 ? "reviewer" : "reviewers"}, ${count("auditor")} auditor, ${count("adjudicator")} ${count("adjudicator") === 1 ? "adjudicator" : "adjudicators"}`
        : `Add ${needed === 2 ? "two reviewers" : "a reviewer"}, an auditor, and an adjudicator`,
    },
    {
      id: "protocol",
      title: "Protocol",
      done: protocol !== null,
      summary: protocol
        ? `Version ${protocol.protocol.version_label}, published ${date(protocol.published_at)}`
        : "Write it, check it, and publish it: every decision reads it",
      screen: {
        to: "/p/$projectId/protocol",
        label: protocol ? "Open the protocol" : "Write the protocol",
      },
    },
    {
      id: "searches",
      title: "Searches",
      done: searches > 0,
      summary:
        searches > 0
          ? `${searches} ${searches === 1 ? "search" : "searches"}`
          : "Import each database's export, or run PubMed and ClinicalTrials.gov live",
      screen: { to: "/p/$projectId/search", label: "Go to the searches" },
    },
    {
      id: "corpus",
      title: "Corpus",
      done: Boolean(progress?.identity_revision_id),
      summary: identified
        ? `${identified.total_records_identified} records identified, ${identified.duplicates_removed} duplicates removed so far`
        : "Deduplicate the searches into records and studies",
      screen: { to: "/p/$projectId/studies", label: `Confirm links on ${plural(labels.study)}` },
    },
    {
      id: "screening",
      title: "Title and abstract screening",
      done: taOpen.length > 0,
      summary: taOpen.length
        ? `Open for ${taOpen.map((s) => scopeLabel(s.scope_id)).join(", ")}`
        : `Open a stage per ${labels.scope.toLowerCase()}, with its sentinels`,
      screen: { to: "/p/$projectId/progress", label: "Watch progress" },
    },
    {
      id: "retrieval",
      title: "Full texts",
      done: (progress?.full_texts ?? 0) > 0,
      summary:
        (progress?.full_texts ?? 0) > 0
          ? `${progress?.full_texts} full texts, sentinels included`
          : "Upload the PDF of each record that passes title and abstract",
      screen: { to: "/p/$projectId/retrieval", label: "Go to retrieval" },
    },
    {
      id: "extraction",
      title: "Extraction",
      done: false,
      summary: "Request runs on included reports; reviewers accept each value",
      screen: { to: "/p/$projectId/extraction", label: "Go to extraction" },
    },
  ];
  const next = steps.find((s) => !s.done) ?? steps.at(-1)!;
  const current = steps.find((s) => s.id === chosen) ?? next;
  const index = steps.indexOf(current);

  useKeys((event) => {
    if (!has("lead")) return false;
    if (event.key === "j" || event.key === "ArrowDown") {
      setChosen(steps[Math.min(index + 1, steps.length - 1)]!.id);
      setNotice(null);
      return true;
    }
    if (event.key === "k" || event.key === "ArrowUp") {
      setChosen(steps[Math.max(index - 1, 0)]!.id);
      setNotice(null);
      return true;
    }
    if (event.key === "Enter" && current.screen) {
      void navigate({ to: current.screen.to, params: { projectId: project.id } });
      return true;
    }
    return false;
  });

  if (!has("lead")) {
    return (
      <Shell>
        <h1 className="text-xl font-semibold tracking-[-0.01em]">Setup</h1>
        <p className="mt-2 max-w-[62ch] text-md text-ink-2">
          The methods lead sets the review up: the people, the protocol, the searches, and the
          screening stages. Your queue fills once a stage you screen is open and the AI has read its
          records.
        </p>
        <People editable={false} onChanged={() => {}} />
      </Shell>
    );
  }

  return (
    <Shell>
      <header className="flex items-baseline gap-6">
        <h1 className="text-xl font-semibold tracking-[-0.01em]">Set up the review</h1>
        <span className="ml-auto flex gap-5 text-sm">
          <KeyHint keys={["j", "k"]}>Next and previous step</KeyHint>
          {current.screen ? <KeyHint keys={["Enter"]}>{current.screen.label}</KeyHint> : null}
        </span>
      </header>
      {!seesLeadData ? (
        <p className="mt-2 max-w-[70ch] text-sm text-ink-2">
          You screen, audit, or adjudicate a stage yourself, so the counts that only the methods
          lead sees stay hidden from you here.
        </p>
      ) : null}
      <div className="mt-8 grid grid-cols-[minmax(0,330px)_minmax(0,1fr)] gap-x-12">
        <ol aria-label="Setup steps" className="relative">
          {steps.map((step, i) => (
            <StepRow
              key={step.id}
              step={step}
              number={i + 1}
              selected={step.id === current.id}
              next={step.id === next.id && !step.done}
              last={i === steps.length - 1}
              reached={steps.slice(0, i).every((s) => s.done)}
              onSelect={() => {
                setChosen(step.id);
                setNotice(null);
              }}
            />
          ))}
        </ol>
        <section aria-label={current.title} data-testid="setup-step" className="min-w-0">
          <h2 className="text-lg font-semibold">{current.title}</h2>
          <p className="mt-0.5 text-sm text-ink-2">{current.summary}</p>
          {notice ? (
            <p
              role="status"
              className="mt-4 max-w-[64ch] border-l-2 border-petrol pl-3 text-sm"
              data-testid="setup-notice"
            >
              {notice}
            </p>
          ) : null}
          <div className="mt-5">
            <Panel
              step={current.id}
              searches={progress ? searches : null}
              onChanged={(message) => {
                // Stay on the step just worked on; the spine shows what is next.
                setChosen(current.id);
                setTick((t) => t + 1);
                if (message) setNotice(message);
              }}
            />
          </div>
          {current.screen ? (
            <p className="mt-8 border-t border-rule pt-4">
              <Button
                keys="Enter"
                onClick={() =>
                  void navigate({ to: current.screen!.to, params: { projectId: project.id } })
                }
              >
                {current.screen.label}
              </Button>
            </p>
          ) : null}
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

/** One step on the spine: the rule runs solid through the steps already done. */
function StepRow({
  step,
  number,
  selected,
  next,
  last,
  reached,
  onSelect,
}: {
  step: Step;
  number: number;
  selected: boolean;
  next: boolean;
  last: boolean;
  reached: boolean;
  onSelect: () => void;
}) {
  return (
    <li
      className="relative grid grid-cols-[28px_minmax(0,1fr)] gap-x-3 pb-5"
      data-testid="setup-row"
      data-state={step.done ? "done" : next ? "next" : "later"}
    >
      {!last ? (
        <span
          aria-hidden="true"
          className={`absolute top-7 bottom-0 left-[13px] w-[2px] ${step.done ? "bg-petrol" : "bg-rule"}`}
        />
      ) : null}
      <span
        aria-hidden="true"
        className={`relative z-[1] mt-0.5 flex h-7 w-7 items-center justify-center rounded-full text-xs font-semibold ${
          step.done
            ? "bg-petrol text-petrol-ink"
            : next
              ? "border-2 border-petrol bg-sheet text-petrol"
              : "border border-rule-strong bg-paper text-ink-3"
        }`}
      >
        {step.done ? (
          <svg width="12" height="10" viewBox="0 0 12 10" aria-hidden="true">
            <path d="M1 5.2l3.4 3.3L11 1.5" fill="none" stroke="currentColor" strokeWidth="1.8" />
          </svg>
        ) : (
          number
        )}
      </span>
      <button
        type="button"
        onClick={onSelect}
        aria-current={selected || undefined}
        className={`rounded-[3px] px-2 py-1 text-left ${selected ? "bg-sheet shadow-[inset_3px_0_0_var(--petrol)]" : "hover:bg-sheet"}`}
      >
        <span className={`block font-semibold ${reached || step.done ? "text-ink" : "text-ink-2"}`}>
          {step.title}
          <span className="sr-only">{step.done ? ", done" : next ? ", next" : ""}</span>
        </span>
        <span className="block text-xs text-ink-2">{step.summary}</span>
      </button>
    </li>
  );
}

function Panel({
  step,
  searches,
  onChanged,
}: {
  step: StepId;
  searches: number | null;
  onChanged: (message: string | null) => void;
}) {
  const { protocol, labels } = useProject();
  switch (step) {
    case "people":
      return <People editable onChanged={onChanged} />;
    case "protocol":
      return (
        <Guide>
          {protocol
            ? "Every screening decision reads the published version. A change is a new version: the editor shows what it changes before you publish it, and a change of screening mode needs a reason."
            : "Start from an example protocol or your own YAML file. Kittum checks it, shows any problem beside the field at fault, and publishes it with your approval in the ledger."}
        </Guide>
      );
    case "searches":
      return (
        <>
          <Guide>
            One search per database and strategy, as PRISMA-S asks. Import the export file of every
            database without a free interface, with the strategy as you ran it.
          </Guide>
          {protocol ? (
            <dl className="mt-4 grid grid-cols-[9rem_minmax(0,1fr)] gap-x-4 gap-y-1 text-sm">
              {protocol.protocol.topics.map((t) => (
                <div key={t.id} className="contents">
                  <dt className="text-xs font-semibold text-ink-2">{t.label}</dt>
                  <dd className="text-ink-2">{t.sources.map((s) => s.name).join("; ")}</dd>
                </div>
              ))}
            </dl>
          ) : null}
        </>
      );
    case "corpus":
      return (
        <BuildOrDone
          searches={searches}
          onBuilt={() =>
            onChanged(
              "The corpus is built. Confirm duplicates and study links before you open screening; links confirmed later count from the next stage.",
            )
          }
        />
      );
    case "screening":
      return <OpenStage onOpened={(message) => onChanged(message)} />;
    case "retrieval":
      return (
        <Guide>
          Upload the PDF of each record that passes title and abstract, with its licence. Add
          full-text sentinels there too: reports with a known outcome that every full-text queue
          gets unmarked. Full-text screening opens once the {plural(labels.report).toLowerCase()}{" "}
          are in.
        </Guide>
      );
    case "extraction":
      return (
        <Guide>
          Request a run on a {labels.report.toLowerCase()} with its full text and an extraction
          schema. The Janus path needs no model; the LLM path runs where a model server is set up.
          Reviewers then accept each value against its source.
        </Guide>
      );
  }
}

function BuildOrDone({ searches, onBuilt }: { searches: number | null; onBuilt: () => void }) {
  const { project } = useProject();
  const [built, setBuilt] = useState(false);
  if (built || project.stages.length > 0) {
    return <Guide>The corpus is built; screening opens on its newest revision.</Guide>;
  }
  return (
    <BuildCorpus
      searches={searches}
      onBuilt={() => {
        setBuilt(true);
        onBuilt();
      }}
    />
  );
}

const Guide = ({ children }: { children: ReactNode }) => (
  <p className="max-w-[64ch] text-md text-ink-2">{children}</p>
);

/** The project's people, and for the lead a form to add one. */
function People({
  editable,
  onChanged,
}: {
  editable: boolean;
  onChanged: (message: string | null) => void;
}) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const router = useRouter();
  const { project, me } = useProject();
  const [name, setName] = useState("");
  const [roles, setRoles] = useState<ReadonlyArray<Role>>(["reviewer"]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);

  useKeys((event) => {
    if (!editable || event.key !== "a") return false;
    input.current?.focus();
    return true;
  });

  const add = async () => {
    if (!name.trim() || roles.length === 0) {
      setError("Give a name and at least one role.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const member = await run(
        workbench.addMember({
          projectId: project.id,
          as: me.actor_id,
          displayName: name.trim(),
          roles,
        }),
      );
      await router.invalidate();
      setName("");
      onChanged(`Added ${member.display_name}.`);
      input.current?.focus();
    } catch (e) {
      setError(failure(e, "The person was not added."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="max-w-[64ch]">
      <table className="narrow w-full border-collapse text-sm" aria-label="People on this review">
        <tbody>
          {project.members.map((m) => (
            <tr key={m.actor_id} className="border-b border-rule">
              <td className="py-1.5 pr-4 font-medium">{m.display_name}</td>
              <td className="py-1.5 text-ink-2">{m.roles.map((r) => ROLE_LABELS[r]).join(", ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {editable ? (
        <form
          aria-label="Add a person"
          className="mt-5 grid gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (!busy) void add();
          }}
        >
          <div className="flex items-end gap-3">
            <div className="min-w-0 flex-1">
              <Field label="Name">
                <TextInput
                  ref={input}
                  value={name}
                  onChange={(e) => setName(e.currentTarget.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Escape") e.currentTarget.blur(); // back to the screen's keys
                  }}
                  placeholder="Sam Okafor"
                />
              </Field>
            </div>
            <Button primary type="submit" disabled={busy}>
              Add
            </Button>
          </div>
          <fieldset className="flex flex-wrap gap-x-5 gap-y-1.5">
            <legend className="mb-1 text-xs font-semibold text-ink-2">Roles</legend>
            {ROLES.map((role) => (
              <label key={role} className="flex cursor-default items-center gap-2 text-sm">
                <Checkbox.Root
                  checked={roles.includes(role)}
                  onCheckedChange={(on) =>
                    setRoles((all) => (on ? [...all, role] : all.filter((r) => r !== role)))
                  }
                  className="flex h-4 w-4 items-center justify-center rounded-[3px] border border-rule-strong bg-sheet data-[checked]:border-petrol data-[checked]:bg-petrol"
                >
                  <Checkbox.Indicator className="text-petrol-ink">
                    <svg width="10" height="8" viewBox="0 0 10 8" aria-hidden="true">
                      <path
                        d="M1 4.2l2.6 2.5L9 1.2"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="1.6"
                      />
                    </svg>
                  </Checkbox.Indicator>
                </Checkbox.Root>
                {ROLE_LABELS[role]}
              </label>
            ))}
          </fieldset>
          <p className="text-xs text-ink-3">
            <Key k="a" /> jumps to the name, <Key k="Escape" /> leaves it. There is no sign-in yet:
            each person picks themselves when they open the review, or follows a link with{" "}
            <code>?as=</code> and their ID.
          </p>
          {error ? (
            <p role="alert" className="text-sm text-exclude">
              {error}
            </p>
          ) : null}
        </form>
      ) : null}
    </div>
  );
}
