// Extraction runs (DESIGN.md, "Extraction"): each run takes one report's full text through the
// Janus path, and the LLM path where a model server is set up, against an extraction schema. The
// methods lead requests them; a worker fills them; reviewers open one and accept its values. The
// accepted values, the latest per semantic key, are what the reports use.
import { useNavigate, useRouteContext } from "@tanstack/react-router";
import { useCallback, useEffect, useState } from "react";

import type { Assertion, ExtractionSchema, RetrievalItem, RunListing } from "../../data/schema.ts";
import { run } from "../../data/workbench.ts";
import { failure, Field, FileInput, FormDialog, Select, TextInput } from "../../ui/Form.tsx";
import { KeyHint } from "../../ui/Key.tsx";
import { useKeys } from "../../ui/keys.ts";
import { plural, useProject } from "../project.tsx";

const STATUS: Record<string, string> = {
  queued: "Waiting for a worker",
  running: "Running",
  succeeded: "Ready to review",
  failed: "Failed; it will be retried",
  dead: "Failed for good",
};

const when = (iso: string) =>
  new Date(iso).toLocaleString("en-GB", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });

export function ExtractionRunsScreen() {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const navigate = useNavigate();
  const { project, me, has, seesLeadData, labels } = useProject();
  const [runs, setRuns] = useState<ReadonlyArray<RunListing> | null>(null);
  const [assertions, setAssertions] = useState<ReadonlyArray<Assertion>>([]);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState(0);
  const [requesting, setRequesting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const canRequest = has("lead") && seesLeadData && workbench.live;

  const load = useCallback(
    () =>
      Promise.all([
        run(workbench.extractionRuns({ projectId: project.id, as: me.actor_id })),
        run(workbench.assertions({ projectId: project.id, as: me.actor_id })),
      ]).then(
        ([found, values]) => {
          setRuns(found);
          setAssertions(values);
        },
        (e: unknown) => setError(failure(e, "The runs could not load.")),
      ),
    [workbench, project.id, me.actor_id],
  );
  useEffect(() => {
    void load();
  }, [load]);
  // A run in the queue finishes on a worker: look again until none is waiting.
  const pending = (runs ?? []).some((r) => r.run.status === "queued" || r.run.status === "running");
  useEffect(() => {
    if (!pending) return;
    const timer = setInterval(() => void load(), 1500);
    return () => clearInterval(timer);
  }, [pending, load]);

  const current = runs?.[Math.min(selected, runs.length - 1)];
  const open = (listing: RunListing | undefined) => {
    if (!listing) return;
    if (listing.run.status !== "succeeded") {
      setNotice("This run has no fields yet. It opens once a worker has finished it.");
      return;
    }
    void navigate({
      to: "/p/$projectId/extraction/$runId",
      params: { projectId: project.id, runId: listing.run.id },
    });
  };

  useKeys((event) => {
    if (event.key === "n" && canRequest) {
      setRequesting(true);
      return true;
    }
    if (!runs?.length) return false;
    switch (event.key) {
      case "j":
      case "ArrowDown":
        setSelected((i) => Math.min(i + 1, runs.length - 1));
        return true;
      case "k":
      case "ArrowUp":
        setSelected((i) => Math.max(i - 1, 0));
        return true;
      case "Enter":
        open(current);
        return true;
      default:
        return false;
    }
  });

  return (
    <section className="h-full overflow-y-auto bg-paper">
      <div className="mx-auto max-w-[1080px] px-10 pt-10 pb-16">
        <div className="flex items-baseline gap-6">
          <h1 className="text-xl font-semibold tracking-[-0.01em]">Extraction</h1>
          <span className="ml-auto flex gap-5 text-sm">
            {runs?.length ? (
              <>
                <KeyHint keys={["j", "k"]}>Move</KeyHint>
                <KeyHint keys={["Enter"]}>Review the run</KeyHint>
              </>
            ) : null}
            {canRequest ? (
              <button type="button" onClick={() => setRequesting(true)}>
                <KeyHint keys={["n"]}>Request a run</KeyHint>
              </button>
            ) : null}
          </span>
        </div>
        {!workbench.live ? (
          <p className="mt-2 text-sm text-ink-3">Example data: one recorded run.</p>
        ) : null}
        {notice || error ? (
          <p
            role={error ? "alert" : "status"}
            className={`mt-3 max-w-[70ch] border-l-2 pl-3 text-sm ${error ? "border-exclude text-exclude" : "border-petrol"}`}
            data-testid="extraction-runs-notice"
          >
            {error ?? notice}
          </p>
        ) : null}

        {runs === null ? (
          <p className="mt-4 text-ink-3">Loading the runs.</p>
        ) : runs.length === 0 ? (
          <p className="mt-4 max-w-[64ch] text-md text-ink-2">
            {canRequest
              ? `No runs yet. Request one on a ${labels.report.toLowerCase()} included at full text, with an extraction schema: the arms, comparisons, endpoints, and quantities to take from it.`
              : `No runs yet. The methods lead requests them on included ${plural(labels.report).toLowerCase()}; they show here once they are ready to review.`}
          </p>
        ) : (
          <ol className="mt-6 border-t border-rule-strong" aria-label="Extraction runs">
            {runs.map((listing, index) => (
              <li
                key={listing.run.id}
                data-testid="extraction-run"
                aria-current={index === selected || undefined}
                onClick={() => setSelected(index)}
                onDoubleClick={() => open(listing)}
                className={`grid grid-cols-[minmax(0,1fr)_12rem_9rem] items-baseline gap-x-5 border-b border-rule py-3 pr-4 pl-5 ${
                  index === selected ? "bg-sheet shadow-[inset_3px_0_0_var(--petrol)]" : ""
                }`}
              >
                <div className="min-w-0">
                  <h2 className="truncate font-semibold" title={listing.report.title}>
                    {listing.report.title || "Untitled report"}
                  </h2>
                  <p className="text-xs text-ink-3">
                    {[listing.report.study_label, listing.report.licence]
                      .filter(Boolean)
                      .join(", ")}
                    {listing.report.study_label || listing.report.licence ? ". " : ""}
                    {listing.run.model_identity
                      ? `Janus and ${listing.run.model_identity}`
                      : "Janus path"}
                    , requested {when(listing.run.created_at)}
                  </p>
                </div>
                <p className="text-sm" data-testid="run-status">
                  {STATUS[listing.run.status] ?? listing.run.status}
                </p>
                <p className="text-sm text-ink-2" data-testid="run-progress">
                  {listing.run.status === "succeeded"
                    ? `${listing.accepted} of ${listing.fields} accepted`
                    : ""}
                </p>
              </li>
            ))}
          </ol>
        )}

        <h2 className="mt-12 border-b border-rule-strong pb-1.5 text-lg font-semibold">
          Accepted values
          <span className="ml-3 text-sm font-normal text-ink-3">
            The latest acceptance per semantic key; reports use these
          </span>
        </h2>
        {assertions.length === 0 ? (
          <p className="mt-3 text-ink-2">None yet.</p>
        ) : (
          <table
            className="narrow mt-2 w-full border-collapse text-sm"
            aria-label="Accepted values"
          >
            <thead>
              <tr className="border-b border-rule text-left text-xs text-ink-2">
                <th className="py-1.5 pr-3 font-semibold">Endpoint</th>
                <th className="py-1.5 pr-3 font-semibold">Arm or comparison</th>
                <th className="py-1.5 pr-3 font-semibold">Timepoint</th>
                <th className="py-1.5 pr-3 font-semibold">Quantity</th>
                <th className="py-1.5 pr-3 text-right font-semibold">Value</th>
                <th className="py-1.5 font-semibold">Policy</th>
              </tr>
            </thead>
            <tbody>
              {assertions.map((a) => (
                <tr key={a.acceptance_id} className="border-b border-rule" data-testid="assertion">
                  <td className="py-1.5 pr-3">{a.key.endpoint}</td>
                  <td className="py-1.5 pr-3">{a.key.arm}</td>
                  <td className="py-1.5 pr-3">{a.key.timepoint}</td>
                  <td className="py-1.5 pr-3">{a.key.quantity}</td>
                  <td className="py-1.5 pr-3 text-right font-medium">
                    {a.value ?? "Not reported"}
                    {a.value && a.unit ? <span className="ml-1 text-ink-3">{a.unit}</span> : null}
                  </td>
                  <td className="py-1.5 text-ink-2">{a.policy}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {canRequest ? (
        <RequestRun
          open={requesting}
          onOpenChange={setRequesting}
          onDone={(message) => {
            setRequesting(false);
            setNotice(message);
            setSelected(0);
            void load();
          }}
        />
      ) : null}
    </section>
  );
}

function RequestRun({
  open,
  onOpenChange,
  onDone,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDone: (message: string) => void;
}) {
  const { workbench } = useRouteContext({ from: "/p/$projectId" });
  const { project, me, labels } = useProject();
  const [reports, setReports] = useState<ReadonlyArray<RetrievalItem> | null>(null);
  const [models, setModels] = useState<ReadonlyArray<string>>([]);
  const [itemRef, setItemRef] = useState("");
  const [schema, setSchema] = useState<ExtractionSchema | null>(null);
  const [schemaError, setSchemaError] = useState<string | null>(null);
  const [model, setModel] = useState("");
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let live = true;
    run(workbench.fullTexts({ projectId: project.id, as: me.actor_id })).then(
      (found) => {
        if (!live) return;
        // Only a report included at full text in some scope, with its PDF, as the server asks.
        const ready = found.filter(
          (r) =>
            r.full_text &&
            Object.values(r.outcomes).some((stages) => stages["full_text"] === "include"),
        );
        setReports(ready);
        setItemRef((ref) => ref || (ready[0]?.item_ref ?? ""));
      },
      (e: unknown) => live && setError(failure(e, "The reports could not load.")),
    );
    run(workbench.extractionModels(project.id)).then(
      (found) => live && setModels(found),
      () => live && setModels([]),
    );
    return () => {
      live = false;
    };
  }, [open, workbench, project.id, me.actor_id]);

  const report = reports?.find((r) => r.item_ref === itemRef);
  const readSchema = async (file: File | null) => {
    setSchema(null);
    setSchemaError(null);
    if (!file) return;
    try {
      const text = await file.text();
      setSchema(
        await run(
          workbench.parseExtractionSchema({ projectId: project.id, as: me.actor_id, text }),
        ),
      );
    } catch (e) {
      setSchemaError(failure(e, "The schema could not be read."));
    }
  };

  const submit = async () => {
    if (!report?.full_text || !schema) {
      setError(
        `Choose a ${labels.report.toLowerCase()} with a full text and an extraction schema.`,
      );
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await run(
        workbench.requestExtraction({
          projectId: project.id,
          as: me.actor_id,
          layerId: report.full_text.layer_id,
          studyId: report.study_id,
          reportId: report.report_id,
          schema,
          modelId: model || null,
          report: {
            study_label: label.trim() || report.registry_ids[0] || report.doi || "",
            title: report.title,
            citation: [report.journal, report.year].filter(Boolean).join(" "),
            licence: report.full_text.licence ?? "",
          },
        }),
      );
      setSchema(null);
      onDone(
        `Requested a run on “${report.title}”. A worker takes it from the queue; it opens for review when it is ready.`,
      );
    } catch (e) {
      setError(failure(e, "The run was not requested."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      wide
      title="Request an extraction run"
      description="Both paths read the report's full text: Janus finds the tables and labelled values, the LLM path quotes what it reads, and every value is checked against its source. Reviewers then accept each one."
      submit="Request the run"
      busy={busy}
      error={error}
      onSubmit={() => void submit()}
    >
      <Field
        label={labels.report}
        hint={
          reports && reports.length === 0
            ? "No report is included at full text yet. Extraction starts once one is."
            : "Reports included at full text, with their PDF."
        }
      >
        <Select value={itemRef} onChange={(e) => setItemRef(e.currentTarget.value)}>
          {(reports ?? []).map((r) => (
            <option key={r.item_ref} value={r.item_ref}>
              {r.title}
            </option>
          ))}
        </Select>
      </Field>
      <Field label="Study label" hint="How the review names it: its registry ID by default.">
        <TextInput
          value={label}
          placeholder={report?.registry_ids[0] ?? report?.doi ?? ""}
          onChange={(e) => setLabel(e.currentTarget.value)}
        />
      </Field>
      <Field
        label="Extraction schema"
        hint="YAML or JSON: arms, comparisons, endpoints with their timepoints and quantities."
      >
        <FileInput
          label="Schema file"
          accept=".yaml,.yml,.json"
          onFile={(f) => void readSchema(f)}
        />
      </Field>
      {schemaError ? (
        <p role="alert" className="text-sm text-exclude">
          {schemaError}
        </p>
      ) : schema ? (
        <p className="text-sm text-ink-2" data-testid="schema-preview">
          {schema.name}: {schema.arms.length} arms, {schema.endpoints.length} endpoints (
          {schema.endpoints.map((e) => e.label).join(", ")}).
        </p>
      ) : null}
      <Field
        label="Paths"
        hint={
          models.length
            ? "The LLM path runs on the worker's model server."
            : "No model server is configured, so the run takes the Janus path only."
        }
      >
        <Select value={model} onChange={(e) => setModel(e.currentTarget.value)}>
          <option value="">Janus path only</option>
          {models.map((m) => (
            <option key={m} value={m}>
              Janus and the LLM path on {m}
            </option>
          ))}
        </Select>
      </Field>
    </FormDialog>
  );
}
