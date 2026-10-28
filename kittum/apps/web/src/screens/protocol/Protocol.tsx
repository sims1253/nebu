// The published protocol (DESIGN.md, "Protocol"): the questions, the outcomes, and per search
// topic its screening mode, audit plan, sources, and criteria. Every heading uses the profile's
// words for core concepts. The methods lead writes versions in the form or as YAML, and publishes
// them.
import { useRouteContext } from "@tanstack/react-router";
import { useCallback, useRef, useState } from "react";
import type { ReactNode } from "react";

import type { Criterion, ProfileLabels, ProtocolRevision, SearchTopic } from "../../data/schema.ts";
import { run } from "../../data/workbench.ts";
import { failure } from "../../ui/Form.tsx";
import { KeyHint } from "../../ui/Key.tsx";
import { useKeys } from "../../ui/keys.ts";
import { useProject } from "../project.tsx";
import { Assistant } from "./Assistant.tsx";
import type { Working } from "./Assistant.tsx";
import { forSaving } from "./document.ts";
import type { Doc } from "./document.ts";
import { ProtocolEditor } from "./ProtocolEditor.tsx";

export const MODES: Record<SearchTopic["mode"], string> = {
  M1: "Two people screen every record. The AI screens it too, as a third screener.",
  M2: "One person screens every record and the AI is the second screener. Every disagreement gets a blind extra screen.",
  M3: "Two people screen in the AI's order and stop when a statistical test set in advance passes.",
};

const STAGES: Record<string, string> = {
  title_abstract: "Title and abstract",
  full_text: "Full text",
};

const date = (iso: string | null) =>
  iso
    ? new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" })
    : "Not published";

type Writing =
  | { readonly mode: "form"; readonly handover: Working | null }
  | { readonly mode: "yaml"; readonly base: ProtocolRevision | null; readonly text: string | null };

export function ProtocolScreen() {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { protocol, has } = useProject();
  const [editing, setEditing] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [writing, setWriting] = useState<Writing>({ mode: "form", handover: null });
  const [switching, setSwitching] = useState<string | null>(null);
  // What each view holds now, so the other one opens the same protocol.
  const form = useRef<Working | null>(null);
  const yaml = useRef<{ readonly text: string; readonly changed: boolean } | null>(null);
  const onCurrent = useCallback((current: Working | null) => {
    form.current = current;
  }, []);
  const lead = has("lead");
  useKeys((event) => {
    if (event.key !== "v" || !lead || editing || !protocol) return false;
    setEditing(true);
    setNotice(null);
    return true;
  });
  if (lead && (editing || !protocol)) {
    const published = (message: string) => {
      setEditing(false);
      setWriting({ mode: "form", handover: null });
      setNotice(message);
    };
    const toYaml = async () => {
      setSwitching(null);
      const current = form.current;
      if (!current) return setWriting({ mode: "yaml", base: null, text: null });
      try {
        const text = await run(workbench.protocolYaml(forSaving(current.doc)));
        setWriting({ mode: "yaml", base: current.base, text });
      } catch (e) {
        setSwitching(failure(e, "Kittum could not turn the form into YAML."));
      }
    };
    const toForm = async () => {
      setSwitching(null);
      const text = yaml.current;
      if (writing.mode !== "yaml" || !text?.changed || !text.text.trim()) {
        return setWriting({ mode: "form", handover: null });
      }
      try {
        const parsed = await run(workbench.parseProtocol(text.text));
        if (!parsed.source) {
          setSwitching(
            `The form cannot show this YAML. ${parsed.issues?.[0]?.message ?? ""} Fix it, or reload the page to drop it.`,
          );
          return;
        }
        setWriting({
          mode: "form",
          handover: { doc: parsed.source as unknown as Doc, base: writing.base },
        });
      } catch (e) {
        setSwitching(failure(e, "Kittum could not read the YAML."));
      }
    };
    return (
      <div className="flex h-full min-h-0 flex-col">
        <div
          className="flex items-center gap-1 border-b border-rule-strong bg-sheet-2 px-4 py-1.5 text-sm"
          role="tablist"
          aria-label="How to write"
        >
          {(
            [
              ["form", "Form"],
              ["yaml", "YAML"],
            ] as const
          ).map(([mode, label]) => (
            <button
              key={mode}
              type="button"
              role="tab"
              aria-selected={writing.mode === mode}
              onClick={() =>
                writing.mode === mode ? undefined : void (mode === "yaml" ? toYaml() : toForm())
              }
              className={`rounded-[3px] px-2.5 py-1 ${writing.mode === mode ? "bg-sheet font-semibold text-ink shadow-[inset_0_-2px_0_var(--petrol)]" : "text-ink-2 hover:text-ink"}`}
            >
              {label}
            </button>
          ))}
          <span className="ml-3 text-xs text-ink-3">
            {writing.mode === "form"
              ? "YAML shows the same protocol as text, for people who know the format."
              : "The form shows the same protocol as fields."}
          </span>
          {switching ? (
            <span role="alert" className="ml-3 text-xs text-exclude">
              {switching}
            </span>
          ) : null}
          {protocol ? (
            <button
              type="button"
              onClick={() => setEditing(false)}
              className="ml-auto text-sm text-ink-2 hover:text-ink"
            >
              Back to the published version
            </button>
          ) : null}
        </div>
        <div className="min-h-0 flex-1">
          {writing.mode === "form" ? (
            <Assistant
              key={writing.handover ? "handover" : "form"}
              handover={writing.handover}
              onCurrent={onCurrent}
              onPublished={published}
              onYaml={() => void toYaml()}
            />
          ) : (
            <ProtocolEditor
              key={writing.base?.id ?? "text"}
              initialText={writing.text}
              base={writing.base}
              onText={(text, changed) => {
                yaml.current = { text, changed };
              }}
              onRevisionSaved={() => {
                yaml.current = null;
                setWriting({ mode: "form", handover: null });
              }}
              onCancel={null}
              onPublished={published}
            />
          )}
        </div>
      </div>
    );
  }
  if (!protocol) {
    return (
      <section className="h-full overflow-y-auto bg-paper">
        <div className="mx-auto max-w-[720px] px-8 py-14">
          <h1 className="text-xl font-semibold">No protocol is published yet</h1>
          <p className="mt-2 max-w-[62ch] text-md text-ink-2">
            Every decision reads the published protocol, so screening waits for it. The methods lead
            writes and publishes it here, and the ledger records their approval.
          </p>
        </div>
      </section>
    );
  }
  return <Published notice={notice} lead={lead} />;
}

function Published({ notice, lead }: { notice: string | null; lead: boolean }) {
  const { protocol, labels } = useProject();
  if (!protocol) return null;
  const { protocol: review, profile } = protocol;
  return (
    <section className="h-full overflow-y-auto bg-paper">
      <article className="mx-auto max-w-[980px] px-10 pt-10 pb-16" aria-label="Protocol">
        {notice ? (
          <p
            role="status"
            className="mb-5 border-l-2 border-petrol pl-3 text-sm"
            data-testid="protocol-notice"
          >
            {notice}
          </p>
        ) : null}
        <div className="flex items-baseline gap-6">
          <h1 className="max-w-[60ch] text-xl font-semibold tracking-[-0.01em] text-balance">
            {review.title}
          </h1>
          {lead ? (
            <span className="ml-auto shrink-0 text-sm">
              <KeyHint keys={["v"]}>Write a new version</KeyHint>
            </span>
          ) : null}
        </div>
        <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-6 gap-y-1 text-sm">
          <dt className="text-ink-3">Version</dt>
          <dd>{review.version_label}</dd>
          <dt className="text-ink-3">Published</dt>
          <dd>{date(protocol.published_at)}</dd>
          <dt className="text-ink-3">Content hash</dt>
          <dd title={protocol.content_hash} className="text-ink-2">
            {protocol.content_hash.slice(0, 16)}
          </dd>
          <dt className="text-ink-3">Profile</dt>
          <dd>{profile.id ?? "None. Kittum uses its own words and rules."}</dd>
        </dl>

        <Section title={`${labels.scope}s`}>
          <ol className="grid gap-5">
            {review.questions.map((q) => (
              <li key={q.id} className="border-l-2 border-petrol pl-4">
                <h3 className="text-md font-semibold">{q.label}</h3>
                <dl className="mt-1.5 grid grid-cols-[9rem_minmax(0,68ch)] gap-x-5 gap-y-1 text-md">
                  <dt className="narrow pt-[2px] text-right text-xs font-semibold text-ink-2">
                    {labels.population}
                  </dt>
                  <dd>{q.population}</dd>
                  <dt className="narrow pt-[2px] text-right text-xs font-semibold text-ink-2">
                    {labels.intervention}
                  </dt>
                  <dd>{q.intervention}</dd>
                  {q.comparators.length > 0 ? (
                    <>
                      <dt className="narrow pt-[2px] text-right text-xs font-semibold text-ink-2">
                        {labels.comparator}
                      </dt>
                      <dd>{q.comparators.join("; ")}</dd>
                    </>
                  ) : null}
                </dl>
              </li>
            ))}
          </ol>
        </Section>

        <Section title={`${labels.outcome}s`}>
          <ul className="grid gap-1 text-md">
            {review.outcomes.map((o) => (
              <li key={o.id} className="flex gap-3">
                <span>{o.name}</span>
                <span className="text-sm text-ink-3">{o.category}</span>
              </li>
            ))}
          </ul>
        </Section>

        {review.topics.map((topic) => (
          <Topic key={topic.id} topic={topic} labels={labels} />
        ))}
      </article>
    </section>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mt-10 border-t border-rule-strong pt-4">
      <h2 className="mb-4 text-lg font-semibold">{title}</h2>
      {children}
    </section>
  );
}

function Topic({ topic, labels }: { topic: SearchTopic; labels: ProfileLabels }) {
  return (
    <Section title={`${labels.search_topic}: ${topic.label}`}>
      <dl className="grid grid-cols-[9rem_minmax(0,68ch)] gap-x-5 gap-y-1.5 text-md">
        <dt className="narrow pt-[2px] text-right text-xs font-semibold text-ink-2">
          Screening mode
        </dt>
        <dd>
          <span className="font-semibold">{topic.mode}</span>{" "}
          <span className="text-ink-2">{MODES[topic.mode]}</span>
          {topic.mode_rationale ? (
            <span className="mt-1 block text-sm text-ink-2">Why: {topic.mode_rationale}</span>
          ) : null}
        </dd>
        <dt className="narrow pt-[2px] text-right text-xs font-semibold text-ink-2">Audit</dt>
        <dd>
          {topic.sentinel_percent}% of records as sentinels with known labels,{" "}
          {topic.random_audit_percent}% re-screened by the auditor
        </dd>
        <dt className="narrow pt-[2px] text-right text-xs font-semibold text-ink-2">
          {labels.scope}s
        </dt>
        <dd>{topic.scopes.map((s) => s.label).join("; ")}</dd>
        <dt className="narrow pt-[2px] text-right text-xs font-semibold text-ink-2">Sources</dt>
        <dd>
          <ul>
            {topic.sources.map((s) => (
              <li key={s.id}>
                {s.name} <span className="text-sm text-ink-3">{s.kind}</span>
              </li>
            ))}
          </ul>
        </dd>
      </dl>
      <h3 className="mt-6 text-md font-semibold">Criteria</h3>
      <p className="mt-1 max-w-[70ch] text-sm text-ink-2">
        A reviewer who excludes a record names one of these criteria. At title and abstract, the
        reviewer presses <span className="font-medium">e</span> and the criterion&rsquo;s number.
      </p>
      <table className="narrow mt-3 w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-rule-strong text-left text-xs text-ink-2">
            <th className="py-1.5 pr-3 font-semibold">No.</th>
            <th className="py-1.5 pr-3 font-semibold">Slot</th>
            <th className="py-1.5 pr-3 font-semibold">Criterion</th>
            <th className="py-1.5 pr-3 font-semibold">Decided at</th>
            <th className="py-1.5 font-semibold">Keywords</th>
          </tr>
        </thead>
        <tbody>
          {topic.criteria.map((c, index) => (
            <CriterionRow key={c.id} criterion={c} index={index} labels={labels} />
          ))}
        </tbody>
      </table>
    </Section>
  );
}

function CriterionRow({
  criterion: c,
  index,
  labels,
}: {
  criterion: Criterion;
  index: number;
  labels: ProfileLabels;
}) {
  return (
    <tr className="border-b border-rule align-baseline">
      <td className="py-2 pr-3 text-ink-3">{index + 1}</td>
      <td className="py-2 pr-3 whitespace-nowrap">
        {labels[c.slot]}
        <span className="block text-xs text-ink-3">
          {c.polarity === "include" ? "Inclusion" : "Exclusion"}, {c.id}
        </span>
      </td>
      <td className="max-w-[56ch] py-2 pr-3">
        {c.text_en}
        {c.text_de !== c.text_en ? (
          <span className="mt-0.5 block text-ink-2" lang="de">
            {c.text_de}
          </span>
        ) : null}
      </td>
      <td className="py-2 pr-3 whitespace-nowrap text-ink-2">
        {c.decidable_at.map((s) => STAGES[s] ?? s).join(", ")}
      </td>
      <td className="py-2 text-ink-2">{c.keywords.length ? c.keywords.join(", ") : "None"}</td>
    </tr>
  );
}
