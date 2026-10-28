// The methods lead's two ways to add a search: import a database's export file, or run PubMed or
// ClinicalTrials.gov live through the server's gateway. Either way Kittum records the query
// exactly as it ran, as PRISMA-S asks.
import { useRouteContext } from "@tanstack/react-router";
import { useState } from "react";

import type { SearchExecution } from "../../data/schema.ts";
import { run } from "../../data/workbench.ts";
import {
  failure,
  Field,
  FileInput,
  FormDialog,
  Select,
  TextArea,
  TextInput,
} from "../../ui/Form.tsx";
import { useProject } from "../project.tsx";

const FORMATS: ReadonlyArray<readonly [string, string]> = [
  ["ris", "RIS, as most databases export it"],
  ["central", "Cochrane Library (CENTRAL), RIS"],
  ["embase", "Embase or Ovid, RIS"],
  ["pubmed-xml", "PubMed XML (Send to, File, XML)"],
  ["csv", "CSV: CTIS, EU-CTR, or another registry"],
];

const LIVE: ReadonlyArray<readonly [string, string]> = [
  ["pubmed", "PubMed"],
  ["ctgov", "ClinicalTrials.gov"],
];

const lines = (text: string) =>
  text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);

function useTopics() {
  const { protocol } = useProject();
  return protocol?.protocol.topics ?? [];
}

export function ImportSearch({
  open,
  onOpenChange,
  onDone,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDone: (execution: SearchExecution) => void;
}) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me, labels } = useProject();
  const topics = useTopics();
  const [topicId, setTopicId] = useState(topics[0]?.id ?? "");
  const topic = topics.find((t) => t.id === topicId) ?? topics[0];
  const [source, setSource] = useState("");
  const [platform, setPlatform] = useState("");
  const [strategy, setStrategy] = useState("");
  const [limits, setLimits] = useState("");
  const [format, setFormat] = useState("ris");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    if (!topic || !source.trim() || !platform.trim() || !strategy.trim() || !file) {
      setError("Fill in every field except the limits. The report shows each one.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const execution = await run(
        workbench.importSearch({
          projectId: project.id,
          as: me.actor_id,
          plan: {
            topic_id: topic.id,
            source: source.trim(),
            platform: platform.trim(),
            strategy: strategy.trim(),
            limits: lines(limits),
          },
          data: await file.text(),
          format,
        }),
      );
      setSource("");
      setPlatform("");
      setStrategy("");
      setLimits("");
      setFile(null);
      onDone(execution);
    } catch (e) {
      setError(failure(e, "Kittum could not import the file."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      wide
      title="Import a search file"
      description="The export file of one database's search, with the query as you ran it there. Kittum keeps the file as the search's raw record and counts the records in it."
      submit="Import the search"
      busy={busy}
      error={error}
      onSubmit={() => void submit()}
    >
      <div className="grid grid-cols-2 gap-4">
        <Field label={labels.search_topic}>
          <Select value={topic?.id ?? ""} onChange={(e) => setTopicId(e.currentTarget.value)}>
            {topics.map((t) => (
              <option key={t.id} value={t.id}>
                {t.label}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="File format">
          <Select value={format} onChange={(e) => setFormat(e.currentTarget.value)}>
            {FORMATS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </Select>
        </Field>
        <Field
          label="Source"
          hint="Its ID in the protocol's source plan, or your own. The PRISMA flow diagram counts per source."
        >
          <TextInput
            list="planned-sources"
            value={source}
            onChange={(e) => {
              const value = e.currentTarget.value;
              setSource(value);
              const planned = topic?.sources.find((s) => s.id === value);
              if (planned && !platform) setPlatform(planned.name);
            }}
            placeholder="central"
          />
          <datalist id="planned-sources">
            {topic?.sources.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </datalist>
        </Field>
        <Field
          label="Platform"
          hint="Where you searched it, for example Cochrane Library (Wiley) or Ovid."
        >
          <TextInput value={platform} onChange={(e) => setPlatform(e.currentTarget.value)} />
        </Field>
      </div>
      <Field label="Query" hint="Exactly as you ran it, one line per search line.">
        <TextArea
          rows={4}
          value={strategy}
          onChange={(e) => setStrategy(e.currentTarget.value)}
          className="font-mono text-[13px]"
        />
      </Field>
      <Field
        label="Limits"
        hint="Optional. One limit per line, such as a date range, a language, or a filter."
      >
        <TextArea rows={2} value={limits} onChange={(e) => setLimits(e.currentTarget.value)} />
      </Field>
      <Field label="File">
        <FileInput label="Search file" accept=".ris,.txt,.xml,.csv,.nbib" onFile={setFile} />
      </Field>
    </FormDialog>
  );
}

export function RunSearch({
  open,
  onOpenChange,
  onDone,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDone: (execution: SearchExecution) => void;
}) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me, labels } = useProject();
  const topics = useTopics();
  const [topicId, setTopicId] = useState(topics[0]?.id ?? "");
  const topic = topics.find((t) => t.id === topicId) ?? topics[0];
  const [source, setSource] = useState("pubmed");
  const [strategy, setStrategy] = useState("");
  const [limits, setLimits] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    if (!topic || !strategy.trim()) {
      setError("Write the query.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const execution = await run(
        workbench.runSearch({
          projectId: project.id,
          as: me.actor_id,
          plan: {
            topic_id: topic.id,
            source,
            platform: LIVE.find(([id]) => id === source)?.[1] ?? source,
            strategy: strategy.trim(),
            limits: lines(limits),
          },
        }),
      );
      setStrategy("");
      setLimits("");
      onDone(execution);
    } catch (e) {
      setError(failure(e, "Kittum could not run the search."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      wide
      title="Run a live search"
      description="Kittum sends the query to the source and keeps every raw response, with the source's own reading of the query. A large PubMed search can take several minutes."
      submit="Run the search"
      busy={busy}
      error={error}
      onSubmit={() => void submit()}
    >
      <div className="grid grid-cols-2 gap-4">
        <Field label={labels.search_topic}>
          <Select value={topic?.id ?? ""} onChange={(e) => setTopicId(e.currentTarget.value)}>
            {topics.map((t) => (
              <option key={t.id} value={t.id}>
                {t.label}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Source">
          <Select value={source} onChange={(e) => setSource(e.currentTarget.value)}>
            {LIVE.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </Select>
        </Field>
      </div>
      <Field
        label="Query"
        hint={
          source === "pubmed"
            ? 'In PubMed\'s syntax, for example "nicotine patch"[tiab] AND randomized controlled trial[pt].'
            : "In ClinicalTrials.gov's syntax."
        }
      >
        <TextArea
          rows={4}
          value={strategy}
          onChange={(e) => setStrategy(e.currentTarget.value)}
          className="font-mono text-[13px]"
        />
      </Field>
      <Field label="Limits" hint="Optional. One limit per line, as the report should state it.">
        <TextArea rows={2} value={limits} onChange={(e) => setLimits(e.currentTarget.value)} />
      </Field>
    </FormDialog>
  );
}
